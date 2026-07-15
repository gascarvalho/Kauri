#!/usr/bin/env python3
"""Run and validate the isolated four-replica adaptive epoch demo."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from typing import Iterable, Mapping, Sequence
import uuid


REPLICA_IDS = tuple(range(4))
MARKER_TOKEN = "KAURI_DEMO"
MANAGER_PORT = 50500
FORBIDDEN_COMMAND_TOKENS = frozenset(
    {"killall", "pkill", "sudo", "ssh"}
)
FATAL_TEXT = re.compile(
    r"(?:terminate called|segmentation fault|uncaught exception|"
    r"fatal error|\[fatal\]|assertion .* failed|abort(?:ed)?)",
    re.IGNORECASE,
)
PROTOCOL_REJECTION_TEXT = re.compile(
    r"(?:(?:invalid|malformed) adaptive epoch consensus message|"
    r"rejecting (?:malformed|invalid)\b[^\n]*|dropping invalid block\b)",
    re.IGNORECASE,
)


class DemoError(RuntimeError):
    """A deterministic demo precondition or validation failure."""


@dataclasses.dataclass(frozen=True)
class Marker:
    event: str
    fields: Mapping[str, str]
    line_number: int
    raw: str


@dataclasses.dataclass(frozen=True)
class Verdict:
    passed: bool
    reasons: tuple[str, ...]
    evidence: Mapping[str, object]


@dataclasses.dataclass
class ProcessRecord:
    name: str
    pid: int
    pgid: int
    command: tuple[str, ...]
    log_path: Path
    process: subprocess.Popen[bytes]
    log_handle: object

    def manifest_entry(self) -> dict[str, object]:
        return_code = self.process.poll()
        entry: dict[str, object] = {
            "name": self.name,
            "pid": self.pid,
            "pgid": self.pgid,
            "command": list(self.command),
            "log": str(self.log_path),
            "running": return_code is None,
        }
        if return_code is not None:
            entry["exit"] = process_exit_status(return_code)
        return entry


def process_exit_status(return_code: int) -> dict[str, object]:
    """Describe both Python's return code and the equivalent shell status."""
    signaled = return_code < 0
    signal_number = -return_code if signaled else None
    signal_name: str | None = None
    if signal_number is not None:
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = f"UNKNOWN_{signal_number}"
    return {
        "popen_return_code": return_code,
        "shell_return_code": 128 + signal_number if signaled else return_code,
        "signaled": signaled,
        "signal_number": signal_number,
        "signal_name": signal_name,
    }


def observe_process_exits(
    records: Sequence[ProcessRecord],
    observations: list[dict[str, object]],
    *,
    phase: str = "runtime",
) -> list[dict[str, object]]:
    """Append newly observed exits, preserving the first polling order."""
    already_observed = {
        str(observation["name"]) for observation in observations
    }
    newly_observed: list[dict[str, object]] = []
    for record in records:
        return_code = record.process.poll()
        if return_code is None or record.name in already_observed:
            continue
        observation: dict[str, object] = {
            "name": record.name,
            "pid": record.pid,
            "pgid": record.pgid,
            "command": list(record.command),
            "observed_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "phase": phase,
            "first_observed": (
                phase != "cleanup"
                and not any(
                    observation.get("phase") != "cleanup"
                    for observation in observations + newly_observed
                )
            ),
            **process_exit_status(return_code),
        }
        newly_observed.append(observation)
        already_observed.add(record.name)
    observations.extend(newly_observed)
    return newly_observed


def format_exit_observation(observation: Mapping[str, object]) -> str:
    signal_name = observation.get("signal_name") or "none"
    return (
        f"{observation['name']} "
        f"popen_return_code={observation['popen_return_code']} "
        f"shell_return_code={observation['shell_return_code']} "
        f"signaled={str(observation['signaled']).lower()} "
        f"signal={signal_name}"
    )


def parse_marker(line: str, line_number: int = 0) -> Marker | None:
    """Parse one marker even when a logger prefix precedes it."""
    offset = line.find(MARKER_TOKEN)
    if offset < 0:
        return None
    payload = line[offset + len(MARKER_TOKEN) :].strip()
    try:
        tokens = shlex.split(payload)
    except ValueError as exc:
        raise DemoError(f"invalid marker quoting on line {line_number}: {exc}") from exc
    fields: dict[str, str] = {}
    positional_event: str | None = None
    if tokens and "=" not in tokens[0]:
        positional_event = tokens.pop(0)
    for token in tokens:
        if "=" not in token:
            raise DemoError(
                f"invalid marker token on line {line_number}: {token!r}"
            )
        key, value = token.split("=", 1)
        if not key or not value or key in fields:
            raise DemoError(
                f"invalid marker field on line {line_number}: {token!r}"
            )
        fields[key] = value
    event = positional_event or fields.get("event")
    if not event:
        raise DemoError(f"marker on line {line_number} has no event")
    return Marker(event, fields, line_number, line.rstrip("\n"))


def parse_log(text: str) -> list[Marker]:
    markers: list[Marker] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        marker = parse_marker(line, line_number)
        if marker is not None:
            markers.append(marker)
    return markers


def _int_field(marker: Marker, key: str) -> int | None:
    value = marker.fields.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _matches(
    marker: Marker,
    *,
    event: str,
    replica: int,
    epoch: int,
    tree: int,
    root: int,
) -> bool:
    return (
        marker.event == event
        and _int_field(marker, "replica") == replica
        and _int_field(marker, "epoch") == epoch
        and _int_field(marker, "tree") == tree
        and _int_field(marker, "root") == root
    )


def _first_after(
    markers: Sequence[Marker],
    start: int,
    predicate: object,
) -> tuple[int, Marker] | None:
    check = predicate
    for index in range(start, len(markers)):
        if check(markers[index]):  # type: ignore[operator]
            return index, markers[index]
    return None


def evaluate_logs(logs: Mapping[int, str]) -> Verdict:
    """Require the exact, height-bound epoch-0 -> epoch-1 sequence."""
    reasons: list[str] = []
    evidence: dict[str, object] = {"replicas": {}}
    phase_digests: dict[str, list[str]] = {
        "bootstrap": [],
        "activation": [],
    }
    activation_heights: list[int] = []

    for replica in REPLICA_IDS:
        text = logs.get(replica)
        if text is None:
            reasons.append(f"replica {replica}: missing log")
            continue
        if FATAL_TEXT.search(text):
            reasons.append(f"replica {replica}: fatal process text")
        if PROTOCOL_REJECTION_TEXT.search(text):
            reasons.append(f"replica {replica}: protocol rejection text")
        try:
            markers = parse_log(text)
        except DemoError as exc:
            reasons.append(f"replica {replica}: {exc}")
            continue
        if any(marker.event == "fatal" for marker in markers):
            reasons.append(f"replica {replica}: fatal marker")

        bootstrap = _first_after(
            markers,
            0,
            lambda marker, replica=replica: (
                marker.event == "bootstrap_staged"
                and _int_field(marker, "replica") == replica
                and _int_field(marker, "active_epoch") == 0
                and _int_field(marker, "active_root") == 0
                and _int_field(marker, "successor_epoch") == 1
                and _int_field(marker, "successor_root") == 1
            ),
        )
        activation = _first_after(
            markers,
            0 if bootstrap is None else bootstrap[0] + 1,
            lambda marker, replica=replica: _matches(
                marker,
                event="epoch_activated",
                replica=replica,
                epoch=1,
                tree=0,
                root=1,
            ),
        )
        epoch0_start = 0 if bootstrap is None else bootstrap[0] + 1
        epoch0_end = len(markers) if activation is None else activation[0]
        epoch0_candidates = [
            (index, markers[index])
            for index in range(epoch0_start, epoch0_end)
            if _matches(
                markers[index],
                event="commit",
                replica=replica,
                epoch=0,
                tree=0,
                root=0,
            )
        ]
        epoch0 = epoch0_candidates[-1] if epoch0_candidates else None
        epoch1 = _first_after(
            markers,
            0 if activation is None else activation[0] + 1,
            lambda marker, replica=replica: _matches(
                marker,
                event="commit",
                replica=replica,
                epoch=1,
                tree=0,
                root=1,
            ),
        )

        selected = {
            "bootstrap": bootstrap[1] if bootstrap else None,
            "epoch0_commit": epoch0[1] if epoch0 else None,
            "activation": activation[1] if activation else None,
            "epoch1_commit": epoch1[1] if epoch1 else None,
        }
        for phase, marker in selected.items():
            if marker is None:
                reasons.append(f"replica {replica}: missing ordered {phase}")

        configured_height = (
            None
            if bootstrap is None
            else _int_field(bootstrap[1], "activation_height")
        )
        epoch0_height = (
            None if epoch0 is None else _int_field(epoch0[1], "height")
        )
        activated_height = (
            None
            if activation is None
            else _int_field(activation[1], "height")
        )
        epoch1_height = (
            None if epoch1 is None else _int_field(epoch1[1], "height")
        )
        if bootstrap is not None:
            if configured_height is None or configured_height <= 0:
                reasons.append(
                    f"replica {replica}: bootstrap activation_height is invalid"
                )
            else:
                activation_heights.append(configured_height)
        if epoch0 is not None:
            if epoch0_height is None:
                reasons.append(
                    f"replica {replica}: epoch0 boundary commit height is invalid"
                )
            elif configured_height is not None and epoch0_height != configured_height:
                reasons.append(
                    f"replica {replica}: epoch0 boundary height {epoch0_height} "
                    f"does not equal activation height {configured_height}"
                )
        if activation is not None:
            if activated_height is None:
                reasons.append(
                    f"replica {replica}: epoch activation marker height is invalid"
                )
            elif (
                configured_height is not None
                and activated_height != configured_height
            ):
                reasons.append(
                    f"replica {replica}: activated height {activated_height} "
                    f"does not equal activation height {configured_height}"
                )
        if epoch1 is not None:
            if epoch1_height is None:
                reasons.append(
                    f"replica {replica}: epoch1 commit height is invalid"
                )
            elif (
                configured_height is not None
                and epoch1_height <= configured_height
            ):
                reasons.append(
                    f"replica {replica}: epoch1 commit height {epoch1_height} "
                    f"must be greater than activation height {configured_height}"
                )
        for phase in ("bootstrap", "activation"):
            marker = selected[phase]
            if marker is None:
                continue
            digest = marker.fields.get("epoch_digest")
            if not digest:
                reasons.append(f"replica {replica}: {phase} has no digest")
            else:
                phase_digests[phase].append(digest)

        evidence["replicas"][str(replica)] = {
            phase: None if marker is None else dict(marker.fields)
            for phase, marker in selected.items()
        }
        evidence["replicas"][str(replica)]["boundary_heights"] = {
            "configured": configured_height,
            "epoch0_commit": epoch0_height,
            "activation": activated_height,
            "epoch1_commit": epoch1_height,
        }

    for phase, digests in phase_digests.items():
        if len(digests) != len(REPLICA_IDS):
            continue
        if len(set(digests)) != 1:
            reasons.append(f"{phase}: replicas do not share one digest")
    if (
        len(phase_digests["bootstrap"]) == len(REPLICA_IDS)
        and len(phase_digests["activation"]) == len(REPLICA_IDS)
        and set(phase_digests["bootstrap"])
        != set(phase_digests["activation"])
    ):
        reasons.append("bootstrap and activation epoch digests differ")
    if (
        len(activation_heights) == len(REPLICA_IDS)
        and len(set(activation_heights)) != 1
    ):
        reasons.append("replicas do not share one activation height")

    evidence["shared_digests"] = {
        phase: sorted(set(digests))
        for phase, digests in phase_digests.items()
    }
    evidence["activation_heights"] = sorted(set(activation_heights))
    return Verdict(not reasons, tuple(reasons), evidence)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_revision(repository: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_run_directory(results_root: Path) -> Path:
    results_root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_directory = results_root / f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    run_directory.mkdir()
    return run_directory


def _assert_safe_command(command: Sequence[str]) -> None:
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise DemoError("process command contains an empty argument")
    lowered = {Path(part).name.lower() for part in command}
    forbidden = lowered & FORBIDDEN_COMMAND_TOKENS
    if forbidden:
        raise DemoError(f"unsafe process command token: {sorted(forbidden)[0]}")


def build_replica_command(
    app_binary: Path,
    main_config: Path,
    replica_config: Path,
) -> tuple[str, ...]:
    command = (
        str(app_binary),
        "--conf",
        str(main_config),
        "--conf",
        str(replica_config),
    )
    _assert_safe_command(command)
    return command


def build_manager_command(
    manager_binary: Path,
    main_config: Path,
    epoch0_file: Path,
    timeout_file: Path,
) -> tuple[str, ...]:
    command = (
        str(manager_binary),
        "--conf",
        str(main_config),
        "--idx",
        "0",
        "--default_epoch",
        str(epoch0_file),
        "--timeouts",
        str(timeout_file),
    )
    _assert_safe_command(command)
    return command


def _extract_key_material(
    repository: Path,
) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    main_source = repository / "hotstuff.conf"
    public: list[tuple[str, str]] = []
    for line in main_source.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("replica"):
            continue
        fields = line.split("=", 1)[1].split(",")
        if len(fields) != 3:
            raise DemoError(f"invalid replica key line in {main_source}")
        public.append((fields[1].strip(), fields[2].strip()))
    private: list[dict[str, str]] = []
    required_private_fields = ("privkey", "tls-privkey", "tls-cert")
    for replica in REPLICA_IDS:
        path = repository / f"hotstuff-sec{replica}.conf"
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if key in required_private_fields and value:
                values[key] = value
        missing = [key for key in required_private_fields if key not in values]
        if missing:
            raise DemoError(
                f"missing {', '.join(missing)} identity material in {path}"
            )
        private.append(values)
    if len(public) != len(REPLICA_IDS):
        raise DemoError("the source config must contain exactly four replicas")
    return public, private


def write_configs(
    repository: Path,
    run_directory: Path,
    peer_port: int,
    client_port: int,
    activation_height: int,
) -> tuple[Path, list[Path], Path, Path]:
    config_directory = run_directory / "config"
    config_directory.mkdir()
    local_directory = Path(__file__).resolve().parent
    epoch0 = config_directory / "epoch0.tree"
    epoch1 = config_directory / "epoch1.tree"
    epoch0.write_text(
        (local_directory / "epoch0.tree").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    epoch1.write_text(
        (local_directory / "epoch1.tree").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    timeout_file = config_directory / "timeouts.empty"
    timeout_file.write_text("# no injected reports\n", encoding="utf-8")

    public, private = _extract_key_material(repository)
    main = config_directory / "hotstuff.gen.conf"
    lines = [
        "block-size = 1",
        "nworker = 2",
        "pace-maker = dummy",
        "proposer = 0",
        "fan-out = 2",
        "piped_latency = 1",
        "async_blocks = 4",
        "base-timeout = 2.0",
        "prop-delay = 0.1",
        "aggregation-timeout = 0.5",
        "leader-progress-timeout = 5",
        "leader-activation-grace = 1",
        "client-ip = 127.0.0.1",
        "tree-generation = file",
        f"tree-generation-fpath = {epoch0}",
        "tree-switch-period = 100000",
        "epoch-protocol-mode = adaptive_v1",
        f"adaptive-epoch-file = {epoch1}",
        f"adaptive-activation-height = {activation_height}",
    ]
    for replica, (public_key, peer_id) in enumerate(public):
        lines.append(
            "replica = "
            f"127.0.0.1:{peer_port + replica};{client_port + replica}, "
            f"{public_key}, {peer_id}"
        )
    main.write_text("\n".join(lines) + "\n", encoding="utf-8")

    replicas: list[Path] = []
    for replica, identity in enumerate(private):
        path = config_directory / f"hotstuff-sec{replica}.conf"
        path.write_text(
            "".join(
                f"{key} = {identity[key]}\n"
                for key in ("privkey", "tls-privkey", "tls-cert")
            )
            + f"idx = {replica}\n",
            encoding="utf-8",
        )
        replicas.append(path)
    return main, replicas, epoch0, timeout_file


def required_ports(peer_port: int, client_port: int) -> tuple[int, ...]:
    ports = tuple(peer_port + replica for replica in REPLICA_IDS)
    ports += tuple(client_port + replica for replica in REPLICA_IDS)
    ports += (MANAGER_PORT,)
    if len(set(ports)) != len(ports):
        raise DemoError("configured demo ports overlap")
    return ports


def isolated_process_cwd(
    run_directory: Path,
    repository: Path,
    main_config: Path,
) -> Path:
    """Choose a cwd where Config cannot auto-load an explicit config twice."""
    working_directory = run_directory.resolve()
    if working_directory in {
        repository.resolve(),
        main_config.parent.resolve(),
    }:
        raise DemoError("process cwd is not isolated from a default config")
    if (working_directory / "hotstuff.gen.conf").exists():
        raise DemoError("isolated process cwd contains hotstuff.gen.conf")
    return working_directory


def check_ports_free(ports: Iterable[int]) -> list[int]:
    unavailable: list[int] = []
    for port in ports:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            unavailable.append(port)
        finally:
            probe.close()
    return unavailable


def check_ports_listening(ports: Iterable[int]) -> list[int]:
    listening: list[int] = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                listening.append(port)
    return listening


def wait_for_listeners_stopped(
    ports: Sequence[int], timeout: float
) -> list[int]:
    deadline = time.monotonic() + timeout
    listening = list(ports)
    while listening and time.monotonic() < deadline:
        listening = check_ports_listening(ports)
        if listening:
            time.sleep(0.05)
    return listening


def wait_for_port_listening(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def spawn_process(
    name: str,
    command: Sequence[str],
    log_path: Path,
    cwd: Path,
) -> ProcessRecord:
    _assert_safe_command(command)
    log_handle = log_path.open("wb")
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pgid = os.getpgid(process.pid)
        if pgid != process.pid:
            raise DemoError(
                f"{name} did not receive an isolated process group"
            )
        return ProcessRecord(
            name,
            process.pid,
            pgid,
            tuple(command),
            log_path,
            process,
            log_handle,
        )
    except Exception:
        log_handle.close()
        raise


def terminate_recorded_groups(
    records: Sequence[ProcessRecord],
    *,
    int_grace: float = 2.0,
    term_grace: float = 2.0,
) -> None:
    """Stop only the isolated process groups recorded by this run."""
    groups = {record.pgid for record in records if record.pgid > 1}
    own_group = os.getpgrp()
    if own_group in groups:
        raise DemoError("refusing to signal the launcher's process group")

    def alive(record: ProcessRecord) -> bool:
        return record.process.poll() is None

    for sig, grace in (
        (signal.SIGINT, int_grace),
        (signal.SIGTERM, term_grace),
        (signal.SIGKILL, 0.2),
    ):
        active = {record.pgid for record in records if alive(record)}
        for pgid in sorted(groups & active):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace
        while any(alive(record) for record in records) and time.monotonic() < deadline:
            time.sleep(0.05)
    for record in records:
        try:
            record.process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        record.log_handle.close()


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_replica_logs(run_directory: Path) -> dict[int, str]:
    return {
        replica: (run_directory / f"replica-{replica}.log").read_text(
            encoding="utf-8", errors="replace"
        )
        for replica in REPLICA_IDS
        if (run_directory / f"replica-{replica}.log").exists()
    }


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    local_directory = Path(__file__).resolve().parent
    repository = local_directory.parents[2]
    parser = argparse.ArgumentParser(
        description="Run the isolated four-replica adaptive epoch demo"
    )
    parser.add_argument("--repository", type=Path, default=repository)
    parser.add_argument(
        "--app-binary",
        type=Path,
        default=repository / "build-adaptive/examples/hotstuff-app",
    )
    parser.add_argument(
        "--manager-binary",
        type=Path,
        default=repository / "build-adaptive/examples/hotstuff-client",
    )
    parser.add_argument(
        "--results-root",
        type=Path,
        default=repository / "results/adaptive-local-demo",
    )
    parser.add_argument("--peer-port", type=int, default=23100)
    parser.add_argument("--client-port", type=int, default=24100)
    parser.add_argument("--activation-height", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--startup-timeout", type=float, default=10.0)
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    repository = args.repository.resolve()
    app_binary = args.app_binary.resolve()
    manager_binary = args.manager_binary.resolve()
    if not app_binary.is_file() or not os.access(app_binary, os.X_OK):
        raise DemoError(f"app binary is not executable: {app_binary}")
    if not manager_binary.is_file() or not os.access(manager_binary, os.X_OK):
        raise DemoError(f"manager binary is not executable: {manager_binary}")
    if args.activation_height <= 0:
        raise DemoError("activation height must be positive")
    if args.timeout <= 0 or args.startup_timeout <= 0:
        raise DemoError("timeouts must be positive")

    ports = required_ports(args.peer_port, args.client_port)
    occupied = check_ports_free(ports)
    if occupied:
        raise DemoError(f"preflight ports are in use: {occupied}")

    run_directory = create_run_directory(args.results_root.resolve())
    main_config, replica_configs, epoch0, timeout_file = write_configs(
        repository,
        run_directory,
        args.peer_port,
        args.client_port,
        args.activation_height,
    )
    process_cwd = isolated_process_cwd(
        run_directory, repository, main_config
    )
    revision = git_revision(repository)
    manifest: dict[str, object] = {
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "repository": str(repository),
        "revision": revision,
        "runner_pid": os.getpid(),
        "runner_pgid": os.getpgrp(),
        "binaries": {
            "app": {
                "path": str(app_binary),
                "sha256": sha256_file(app_binary),
            },
            "manager": {
                "path": str(manager_binary),
                "sha256": sha256_file(manager_binary),
            },
        },
        "ports": list(ports),
        "processes": [],
        "exit_observations": [],
    }
    _write_json(run_directory / "manifest.json", manifest)
    (run_directory / "launcher.log").write_text(
        f"{MARKER_TOKEN} launcher_start revision={revision} "
        f"binary_sha256={sha256_file(app_binary)}\n",
        encoding="utf-8",
    )

    records: list[ProcessRecord] = []
    exit_observations: list[dict[str, object]] = []
    verdict = Verdict(False, ("demo did not reach a verdict",), {})
    postflight_listeners: list[int] = list(ports)
    interrupted = False
    interruption_signal: int | None = None

    def request_shutdown(signum: int, _frame: object) -> None:
        nonlocal interrupted, interruption_signal
        interruption_signal = signum
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    previous_handlers = {
        signum: signal.signal(signum, request_shutdown)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        manager = spawn_process(
            "manager",
            build_manager_command(
                manager_binary, main_config, epoch0, timeout_file
            ),
            run_directory / "manager.log",
            process_cwd,
        )
        records.append(manager)
        manifest["processes"] = [record.manifest_entry() for record in records]
        _write_json(run_directory / "manifest.json", manifest)
        if not wait_for_port_listening(MANAGER_PORT, args.startup_timeout):
            raise DemoError("reputation manager did not listen on port 50500")

        for replica in REPLICA_IDS:
            record = spawn_process(
                f"replica-{replica}",
                build_replica_command(
                    app_binary,
                    main_config,
                    replica_configs[replica],
                ),
                run_directory / f"replica-{replica}.log",
                process_cwd,
            )
            records.append(record)
            manifest["processes"] = [
                process.manifest_entry() for process in records
            ]
            _write_json(run_directory / "manifest.json", manifest)

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            newly_exited = observe_process_exits(records, exit_observations)
            if newly_exited:
                manifest["exit_observations"] = exit_observations
                manifest["processes"] = [
                    process.manifest_entry() for process in records
                ]
                _write_json(run_directory / "manifest.json", manifest)
                first_exit = exit_observations[0]
                same_poll = "; ".join(
                    format_exit_observation(observation)
                    for observation in newly_exited[1:]
                )
                reason = (
                    "process exited before success: first observed "
                    + format_exit_observation(first_exit)
                )
                if same_poll:
                    reason += f"; additional exits in same poll: {same_poll}"
                partial = evaluate_logs(_read_replica_logs(run_directory))
                verdict = Verdict(
                    False,
                    (reason,),
                    {
                        "exit_observations": exit_observations,
                        "partial_log_evidence": partial.evidence,
                    },
                )
                break
            logs = _read_replica_logs(run_directory)
            verdict = evaluate_logs(logs)
            if verdict.passed or any("fatal" in reason for reason in verdict.reasons):
                break
            time.sleep(0.2)
        else:
            verdict = evaluate_logs(_read_replica_logs(run_directory))
            if verdict.passed:
                pass
            else:
                verdict = Verdict(
                    False,
                    verdict.reasons + ("timed out waiting for adaptive proof",),
                    verdict.evidence,
                )
    except (DemoError, OSError, subprocess.SubprocessError) as exc:
        verdict = Verdict(False, (str(exc),), {})
    except KeyboardInterrupt:
        signal_name = (
            signal.Signals(interruption_signal).name
            if interruption_signal is not None
            else "interrupt"
        )
        verdict = Verdict(False, (f"demo interrupted by {signal_name}",), {})
    finally:
        try:
            exits_before_cleanup = observe_process_exits(
                records, exit_observations, phase="pre_cleanup"
            )
            if exits_before_cleanup and not any(
                "process exited before success" in reason
                for reason in verdict.reasons
            ):
                first_exit = next(
                    observation
                    for observation in exit_observations
                    if observation.get("phase") != "cleanup"
                )
                verdict = Verdict(
                    False,
                    verdict.reasons
                    + (
                        "process exited before cleanup: first observed "
                        + format_exit_observation(first_exit),
                    ),
                    {
                        **verdict.evidence,
                        "exit_observations": exit_observations,
                    },
                )
            terminate_recorded_groups(records)
            observe_process_exits(
                records, exit_observations, phase="cleanup"
            )
            manifest["finished_utc"] = dt.datetime.now(
                dt.timezone.utc
            ).isoformat()
            manifest["exit_observations"] = exit_observations
            manifest["processes"] = [
                process.manifest_entry() for process in records
            ]
            _write_json(run_directory / "manifest.json", manifest)
            postflight_listeners = wait_for_listeners_stopped(ports, 5.0)
            if postflight_listeners:
                verdict = Verdict(
                    False,
                    verdict.reasons
                    + (
                        "postflight listeners are still active: "
                        f"{postflight_listeners}",
                    ),
                    verdict.evidence,
                )
            _write_json(
                run_directory / "verdict.json",
                {
                    "passed": verdict.passed,
                    "reasons": list(verdict.reasons),
                    "evidence": verdict.evidence,
                    "listeners_stopped": not postflight_listeners,
                },
            )
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    print(f"results: {run_directory}")
    print("PASS" if verdict.passed else "FAIL")
    for reason in verdict.reasons:
        print(f"- {reason}")
    return 0 if verdict.passed else 1


def main() -> None:
    try:
        raise SystemExit(run())
    except DemoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
