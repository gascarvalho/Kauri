#!/usr/bin/env python3
"""One-shot local launcher for the W19 scheduled fixed-E0 control.

This is intentionally a narrow execution boundary: it consumes a sealed
``sustained_role_local`` fixed-E0 plan, an *external* exact approval, and an
E0 identity receipt derived by the native helper.  It never retries, never
constructs a successor, and seals either the complete raw layout or an abort.
The receipt is a no-claim raw bundle; independent replay decides usability.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
from typing import Any, Callable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
KAURI = HERE.parents[2]
PLAN = Path("runtime/sustained-role-execution-plan.json")
REQUEST = Path("runtime/sustained-role-authorization-request.json")
APPROVAL = Path("runtime/sustained-role-fixed-e0-launch-authorization.json")
ATTESTATION = Path("runtime/sustained-role-fixed-e0-prearm-attestation.json")
FINALIZATION = Path("runtime/sustained-role-fixed-e0-finalization.json")
CLEANUP = Path("runtime/cleanup-receipt.json")
RECEIPT = Path("sustained-role-fixed-e0-raw-bundle-receipt.json")
ABORT = Path("sustained-role-fixed-e0-abort.json")
KIND = "kauri-n7-sustained-role-raw-bundle-receipt-v1"
AUTH_KIND = "kauri-n7-sustained-role-fixed-e0-launch-authorization-v1"
FINAL_KIND = "kauri-n7-sustained-role-fixed-e0-launch-finalization-v1"
_SOURCES = ("adaptive-manager", *(f"replica-{i}" for i in range(7)))
_HEX = frozenset("0123456789abcdef")


class LaunchError(RuntimeError):
    pass


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise LaunchError(f"cannot load runtime adapter {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plan_digest(plan: Mapping[str, Any]) -> str:
    return _sha(_canonical({key: value for key, value in plan.items() if key != "plan_sha256"}))


def _argv_digest(argv: Sequence[str]) -> str:
    if not argv or any(not isinstance(value, str) or not value for value in argv):
        raise LaunchError("approved process argv is malformed")
    return _sha(_canonical({"schema_version": 1, "argv": list(argv)}))


def _verified_launch_file(path_value: object, expected_hash: object, *,
                          label: str, root: Path | None = None) -> Path:
    if not isinstance(path_value, str) or not path_value:
        raise LaunchError(f"{label} path is missing")
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink():
        raise LaunchError(f"{label} is not an absolute regular launch input")
    try:
        resolved = path.resolve(strict=True)
        info = path.stat()
        if root is not None:
            resolved.relative_to(root.resolve())
        if not stat.S_ISREG(info.st_mode) or _sha(path.read_bytes()) != _hex(expected_hash, label):
            raise LaunchError(f"{label} differs from approved launch bytes")
    except (OSError, ValueError) as exc:
        raise LaunchError(f"{label} is not a pinned launch input") from exc
    return path


def _verify_approved_launch_inputs(root: Path, plan: Mapping[str, Any]) -> None:
    """Reopen every approved executable/configuration immediately before spawn."""
    commands = plan.get("commands")
    config = plan.get("configuration")
    if not isinstance(commands, Mapping) or not isinstance(config, Mapping):
        raise LaunchError("approved launch commands or configuration are missing")
    manager = commands.get("manager")
    replicas = commands.get("replicas")
    config_rows = config.get("replicas")
    if (not isinstance(manager, Mapping) or not isinstance(manager.get("argv"), list) or
            not isinstance(replicas, list) or len(replicas) != 7 or
            not isinstance(config_rows, list) or len(config_rows) != 7):
        raise LaunchError("approved process command set is malformed")
    if _argv_digest(manager["argv"]) != _hex(manager.get("sha256"), "manager argv"):
        raise LaunchError("manager argv differs from approved command")
    _verified_launch_file(manager["argv"][0], manager.get("executable_sha256"),
                          label="manager executable", root=root)
    for i, row in enumerate(replicas):
        if (not isinstance(row, Mapping) or row.get("replica_id") != i or
                not isinstance(row.get("argv"), list) or
                _argv_digest(row["argv"]) != _hex(row.get("sha256"), f"replica-{i} argv")):
            raise LaunchError(f"replica-{i} argv differs from approved command")
        _verified_launch_file(row["argv"][0], row.get("executable_sha256"),
                              label=f"replica-{i} executable", root=root)
    for key, descriptor in (
        ("Epoch-0 tree", plan.get("epoch0", {}).get("tree")),
        ("main config", config.get("main")),
        *((f"replica-{i} config", config_rows[i]) for i in range(7)),
    ):
        if not isinstance(descriptor, Mapping):
            raise LaunchError(f"{key} descriptor is missing")
        _verified_launch_file(descriptor.get("path"), descriptor.get("sha256"),
                              label=key, root=root)
    for key, section in (
        ("native profile", plan.get("native_fault_schedule")),
        ("selection policy", plan.get("manager_selection_policy")),
    ):
        descriptor = section.get("descriptor") if isinstance(section, Mapping) else None
        if not isinstance(descriptor, Mapping):
            raise LaunchError(f"{key} descriptor is missing")
        _verified_launch_file(descriptor.get("path"), descriptor.get("sha256"), label=key)


def _prepare_exclusive_output_dirs(root: Path) -> None:
    """Create the raw/log sinks only at the authorized one-shot launch boundary."""
    for name in ("raw", "logs"):
        path = root / name
        if path.exists() or path.is_symlink():
            raise LaunchError(f"{name} output directory already exists before launch")
        path.mkdir(mode=0o700)


def _read(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
        raise LaunchError(f"{label} is not a bounded regular file")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LaunchError(f"{label} is not JSON") from exc
    if not isinstance(value, dict) or raw != _canonical(value):
        raise LaunchError(f"{label} is not canonical JSON")
    return value, raw


def _write_once(path: Path, value: object) -> None:
    if path.exists() or path.is_symlink():
        raise LaunchError(f"refusing to replace {path.name}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(_canonical(value)); stream.flush(); os.fsync(stream.fileno())


def _descriptor(root: Path, path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise LaunchError(f"missing raw artifact: {path.name}")
    try:
        relative = str(path.resolve().relative_to(root))
    except ValueError as exc:
        raise LaunchError("raw artifact escapes run root") from exc
    return {"path": relative, "sha256": _sha(path.read_bytes())}


def _archive_input(root: Path, source: Path, name: str) -> Path:
    """Copy one source-bound input into the sealed run root exactly once."""
    if source.is_symlink() or not source.is_file():
        raise LaunchError(f"launch input {name} is not a regular file")
    destination = root / "runtime" / "launch-inputs" / name
    if destination.exists() or destination.is_symlink():
        raise LaunchError(f"launch input archive already exists: {name}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as incoming, os.fdopen(
        os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb"
    ) as outgoing:
        shutil.copyfileobj(incoming, outgoing)
        outgoing.flush(); os.fsync(outgoing.fileno())
    if name == "e0-identity-helper":
        # The independent validator re-executes this archived, hash-bound
        # native helper against the archived Epoch-0 tree.
        destination.chmod(0o700)
    if _sha(source.read_bytes()) != _sha(destination.read_bytes()):
        raise LaunchError(f"launch input archive hash drift: {name}")
    return destination


def _hex(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in _HEX for c in value):
        raise LaunchError(f"{label} is not a lower-case SHA-256")
    return value


def _option(argv: Sequence[str], option: str) -> str:
    if argv.count(option) != 1:
        raise LaunchError(f"manager argv must contain exactly one {option}")
    index = argv.index(option)
    if index + 1 == len(argv) or not argv[index + 1]:
        raise LaunchError(f"manager argv lacks {option} value")
    return argv[index + 1]


def _first_omission_tree(plan: Mapping[str, Any]) -> int | None:
    """Read the optional v1-compatible actor phase gate from frozen argv."""
    replicas = plan.get("commands", {}).get("replicas", [])
    if not isinstance(replicas, list) or len(replicas) != 7 or not isinstance(replicas[1], Mapping):
        raise LaunchError("plan lacks actor-1 replica command")
    argv = replicas[1].get("argv")
    if not isinstance(argv, list):
        raise LaunchError("actor-1 replica argv is malformed")
    option = "--experiment-byzantine-first-omission-tree"
    if option not in argv:
        tree = None
    else:
        if argv.count(option) != 1:
            raise LaunchError("actor-1 first-omission tree option is ambiguous")
        index = argv.index(option)
        if index + 1 >= len(argv) or not isinstance(argv[index + 1], str) or not argv[index + 1].isdigit():
            raise LaunchError("actor-1 first-omission tree option is malformed")
        tree = int(argv[index + 1])
        if tree != 4 or argv[index + 1] != str(tree):
            raise LaunchError("actor-1 first-omission tree differs from frozen tree 4")
    native = plan.get("native_fault_schedule")
    values = native.get("values") if isinstance(native, Mapping) else None
    fault = values.get("fault") if isinstance(values, Mapping) else None
    declared = fault.get("first_omission_tree") if isinstance(fault, Mapping) else None
    if declared != tree:
        raise LaunchError("actor-1 first-omission tree differs from frozen profile")
    return tree


def _scheduled_manager(plan: Mapping[str, Any], *, e0_digest: str) -> tuple[str, ...]:
    commands = plan.get("commands")
    schedule = plan.get("scheduled_window")
    native = plan.get("native_fault_schedule")
    if not isinstance(commands, Mapping) or not isinstance(schedule, Mapping) or not isinstance(native, Mapping):
        raise LaunchError("plan launcher bindings are missing")
    manager = commands.get("manager")
    if not isinstance(manager, Mapping) or not isinstance(manager.get("argv"), list):
        raise LaunchError("plan manager argv is invalid")
    argv = tuple(manager["argv"])
    forbidden = {"--transition-request", "--bundle-output", "--fault-window-arm-control-only", "--scheduled-fixed-e0-control"}
    if any(flag in argv for flag in forbidden):
        raise LaunchError("fixed-E0 source plan has successor or legacy control options")
    run_id = _option(argv, "--structured-event-run-id")
    descriptor = native.get("descriptor")
    if not isinstance(descriptor, Mapping):
        raise LaunchError("native profile descriptor is missing")
    profile_sha = _hex(descriptor.get("sha256"), "native profile")
    start, end = schedule.get("start_monotonic_ns"), schedule.get("end_monotonic_ns")
    if type(start) is not int or type(end) is not int or end - start < 60_000_000_000:
        raise LaunchError("scheduled window cannot cover the full 60-second horizon")
    return argv + ("--scheduled-fixed-e0-control", "--scheduled-fixed-e0-run-id", run_id,
                   "--scheduled-fixed-e0-profile-sha256", profile_sha,
                   "--scheduled-fixed-e0-epoch-zero-digest", e0_digest,
                   "--scheduled-fixed-e0-window-start-monotonic-ns", str(start),
                   "--scheduled-fixed-e0-window-end-monotonic-ns", str(end))


def _source_e0(root: Path) -> tuple[str, Path]:
    path = root / "runtime/e0-identity-receipt.json"
    receipt, _ = _read(path, "E0 identity receipt")
    if receipt.get("state") != "DERIVED_READ_ONLY" or receipt.get("epoch_number") != 0:
        raise LaunchError("E0 identity is not source-derived Epoch 0")
    return _hex(receipt.get("epoch_digest"), "E0 digest"), path


def _utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").tzinfo == timezone.utc
    except ValueError:
        return False


def _e0_identity_helper(root: Path, identity_path: Path) -> Path:
    """Reopen the helper named by the source-derived E0 receipt.

    The raw receipt is not self-authenticating: retain the exact helper bytes
    so an independent validator can replay the digest from the archived tree.
    """
    receipt, _ = _read(identity_path, "E0 identity receipt")
    helper_raw = receipt.get("helper_binary")
    helper_sha = _hex(receipt.get("helper_binary_sha256"), "E0 helper digest")
    if not isinstance(helper_raw, str) or not helper_raw:
        raise LaunchError("E0 identity receipt lacks helper binary")
    helper = Path(helper_raw).resolve()
    if helper.is_symlink() or not helper.is_file() or _sha(helper.read_bytes()) != helper_sha:
        raise LaunchError("E0 identity helper is absent or hash-drifted")
    return helper


def _exact_approval(root: Path, supplied: Path, plan: Mapping[str, Any], request_bytes: bytes,
                    *, kind: str = AUTH_KIND, archive_path: Path = APPROVAL,
                    expected_authorization_sha256: str) -> dict[str, Any]:
    approval, raw = _read(supplied.resolve(), "external authorization")
    if (not isinstance(expected_authorization_sha256, str) or
            len(expected_authorization_sha256) != 64 or
            any(character not in _HEX for character in expected_authorization_sha256) or
            _sha(raw) != expected_authorization_sha256):
        raise LaunchError("external authorization changed after campaign verification")
    expected = {"schema_version", "kind", "request_sha256", "plan_sha256", "approval_reference", "approved_utc", "no_retry"}
    if (set(approval) != expected or approval.get("schema_version") != 1 or approval.get("kind") != kind or
            approval.get("request_sha256") != _sha(request_bytes) or approval.get("plan_sha256") != plan.get("plan_sha256") or
            approval.get("no_retry") is not True or not isinstance(approval.get("approval_reference"), str) or not approval["approval_reference"].strip() or
            not _utc_timestamp(approval.get("approved_utc"))):
        raise LaunchError("external authorization is not exact for this no-retry plan")
    if supplied.resolve() == (root / archive_path).resolve():
        raise LaunchError("authorization must be external to the run archive")
    _write_once(root / archive_path, approval)
    return approval


def _all_seven_e0_common(
    streams: Mapping[str, Sequence[Mapping[str, Any]]], before_ns: int,
    *, designated_observer: int = 2,
) -> bool:
    """Require one native E0 decision and seven matching local observations.

    ``block.committed`` carries the designated observer's authenticated
    decision proof; all replicas emit the deliberately identity-light
    ``block.commit_observed`` callback.  Both native event timestamps use the
    structured-event CLOCK_MONOTONIC_RAW clock, which is also the scheduled
    window's contract.
    """
    # ``write_runtime_inputs`` freezes replica-2 as the authoritative
    # observer.  Do not silently select replica-0 just because it is an easy
    # convention for synthetic examples: the native config binds the observer
    # identity, and a wrong prearm source would make a real run abort.
    if designated_observer not in range(7):
        raise LaunchError("designated commit observer is outside the N=7 membership")
    observer_source = f"replica-{designated_observer}"
    for committed in streams.get(observer_source, ()):
        payload = committed.get("payload") if isinstance(committed, Mapping) else None
        if (committed.get("event_type") != "block.committed" or
                committed.get("source_monotonic_ns", before_ns) >= before_ns or
                not isinstance(payload, Mapping) or payload.get("designated_observer") is not True):
            continue
        proof = payload.get("decision_proof")
        height, block_hash = payload.get("block_height"), payload.get("block_hash")
        if (type(height) is not int or height <= 0 or not isinstance(block_hash, str) or
                not isinstance(proof, Mapping) or proof.get("epoch_number") != 0 or
                proof.get("block_hash") != block_hash):
            continue
        if all(any(
            observed.get("event_type") == "block.commit_observed" and
            observed.get("source_monotonic_ns", before_ns) < before_ns and
            isinstance(observed.get("payload"), Mapping) and
            observed["payload"].get("block_height") == height and
            observed["payload"].get("block_hash") == block_hash
            for observed in streams.get(f"replica-{replica}", ())
        ) for replica in range(7)):
            return True
    return False


def _clean_exit_codes(receipt: Mapping[str, Any]) -> dict[str, int]:
    processes = receipt.get("processes")
    if receipt.get("complete") is not True or not isinstance(processes, list) or len(processes) != 8:
        raise LaunchError("cleanup does not prove all eight process closures")
    result: dict[str, int] = {}
    for process in processes:
        if not isinstance(process, Mapping) or not isinstance(process.get("source_id"), str) or type(process.get("returncode")) is not int:
            raise LaunchError("cleanup process exit schema drift")
        result[process["source_id"]] = process["returncode"]
    if set(result) != set(_SOURCES) or any(code != 0 for code in result.values()):
        raise LaunchError("fixed-E0 pilot did not exit cleanly")
    return result


def _persist_cleanup(root: Path, receipt: Mapping[str, Any]) -> None:
    """Accept the production helper's atomic receipt, or persist fake-adapter output."""
    path = root / CLEANUP
    payload = _canonical(receipt)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise LaunchError("cleanup receipt differs from the reaped process outcome")
        return
    _write_once(path, receipt)


def _physical_anchor_and_coverage(root: Path, *, first_omission_tree: int | None = None) -> dict[str, Any]:
    """Bind both arms to the same actor-1 internal aggregate opportunity."""
    raw = (root / "raw/replica-1.jsonl").read_bytes()
    anchor: dict[str, Any] | None = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LaunchError("replica-1 raw stream is not JSONL") from exc
        payload = event.get("payload") if isinstance(event, Mapping) else None
        proposal = payload.get("proposal") if isinstance(payload, Mapping) else None
        if (first_omission_tree is not None and event.get("event_type") == "fault.contribution_opportunity" and
                isinstance(payload, Mapping) and payload.get("actor") == 1):
            if not (payload.get("fault_mode") == "role_scoped_persistent_selected_omission_v1" and
                    payload.get("physical_role") == "internal" and
                    payload.get("scheduled_action") == "omit_aggregate" and
                    isinstance(proposal, Mapping) and proposal.get("epoch_number") == 0 and
                    proposal.get("tree_id") == first_omission_tree):
                raise LaunchError("first actor-1 physical omission does not bind frozen tree 4 internal aggregate")
        if (event.get("event_type") == "fault.contribution_opportunity" and isinstance(payload, Mapping)
                and payload.get("actor") == 1
                and payload.get("fault_mode") == "role_scoped_persistent_selected_omission_v1"
                and payload.get("physical_role") == "internal"
                and payload.get("scheduled_action") == "omit_aggregate"):
            anchor = {"source_id": "replica-1", "source_sequence": event.get("source_sequence"), "line_sha256": _sha(line), "monotonic_ns": event.get("source_monotonic_ns")}
            break
    if anchor is None or type(anchor["source_sequence"]) is not int or type(anchor["monotonic_ns"]) is not int:
        raise LaunchError("no native actor-1 scheduled omission opportunity was recorded")
    horizon = anchor["monotonic_ns"] + 60_000_000_000
    for source in (f"replica-{replica}" for replica in range(7)):
        path = root / f"raw/{source}.jsonl"
        if not path.is_file():
            raise LaunchError(f"missing raw stream for {source}")
        try:
            events = [json.loads(line) for line in path.read_bytes().splitlines()]
        except json.JSONDecodeError as exc:
            raise LaunchError(f"raw stream for {source} is not JSONL") from exc
        if not events or not isinstance(events[-1], Mapping) or events[-1].get("source_monotonic_ns", -1) < horizon:
            raise LaunchError("raw streams do not cover anchor plus full 60-second horizon")
    return anchor


def _fixed_manager_terminal(root: Path, *, run_id: str, profile_sha256: str,
                           e0_digest: str, start: int, end: int) -> None:
    """Fixed-E0 has a manager terminal, but it is not a 60-second peer stream."""
    path = root / "raw/adaptive-manager.jsonl"
    if path.is_symlink() or not path.is_file():
        raise LaunchError("missing fixed-E0 manager raw stream")
    try:
        events = [json.loads(line) for line in path.read_bytes().splitlines()]
    except json.JSONDecodeError as exc:
        raise LaunchError("fixed-E0 manager raw stream is not JSONL") from exc
    for event in events:
        payload = event.get("payload") if isinstance(event, Mapping) else None
        if (event.get("event_type") == "scheduled_fixed_e0_control.terminal" and
                isinstance(payload, Mapping) and payload == {
                    "run_id": run_id, "profile_sha256": profile_sha256,
                    "epoch_zero_digest": e0_digest,
                    "window_start_monotonic_ns": start,
                    "window_end_monotonic_ns": end,
                }):
            return
    raise LaunchError("native fixed-E0 manager terminal is absent or does not bind the scheduled control")


def _await_fixed_manager_completion(
    root: Path, manager_record: Any, *, run_id: str, profile_sha256: str,
    e0_digest: str, start: int, end: int, deadline: float,
    monotonic: Callable[[], float], sleep: Callable[[float], None],
) -> None:
    """Let the native one-second timer seal its terminal before cleanup signals it."""
    process = getattr(manager_record, "process", None)
    if process is None or not callable(getattr(process, "poll", None)):
        raise LaunchError("fixed-E0 manager process handle is missing")
    until = min(deadline, monotonic() + 8.0)
    while True:
        terminal_seen = False
        try:
            _fixed_manager_terminal(
                root, run_id=run_id, profile_sha256=profile_sha256,
                e0_digest=e0_digest, start=start, end=end,
            )
            terminal_seen = True
        except LaunchError as exc:
            if "terminal is absent" not in str(exc):
                raise
        status = process.poll()
        if status is not None and status != 0:
            raise LaunchError(f"fixed-E0 manager exited before clean terminal with status {status}")
        if terminal_seen and status == 0:
            return
        if monotonic() >= until:
            raise LaunchError("fixed-E0 manager did not seal terminal and exit cleanly before cleanup")
        sleep(0.05)


def execute_fixed_e0_pilot(run_directory: Path, authorization_path: Path, *,
                           spawn: Callable[..., Any], event_streams: Callable[[Path], Mapping[str, Sequence[Mapping[str, Any]]]],
                           cleanup: Callable[[Path, Sequence[Any]], Mapping[str, Any]], raw_clock: Callable[[], int],
                           expected_authorization_sha256: str,
                           monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Launch exactly once, preserving a complete receipt or a sealed abort."""
    root = Path(run_directory).resolve(); records: list[Any] = []
    try:
        plan, plan_bytes = _read(root / PLAN, "sustained-role plan")
        request, request_bytes = _read(root / REQUEST, "sustained-role request")
        if (plan.get("state") != "PREPARED_DRY_RUN_EXTERNAL_APPROVAL_REQUIRED" or plan.get("comparison", {}).get("arm") != "fixed_e0" or
                plan.get("no_retry") is not True or _plan_digest(plan) != plan.get("plan_sha256") or
                request.get("execution_plan_sha256") != plan.get("plan_sha256") or request.get("no_retry") is not True):
            raise LaunchError("fixed-E0 plan/request identity drift")
        approval = _exact_approval(root, authorization_path, plan, request_bytes,
                                   expected_authorization_sha256=expected_authorization_sha256)
        _verify_approved_launch_inputs(root, plan)
        e0_digest, e0_path = _source_e0(root)
        e0_helper = _e0_identity_helper(root, e0_path)
        first_omission_tree = _first_omission_tree(plan)
        manager = _scheduled_manager(plan, e0_digest=e0_digest)
        replicas = plan.get("commands", {}).get("replicas", [])
        if not isinstance(replicas, list) or len(replicas) != 7:
            raise LaunchError("plan does not contain exactly seven replicas")
        start = plan["scheduled_window"]["start_monotonic_ns"]; end = plan["scheduled_window"]["end_monotonic_ns"]
        if raw_clock() >= start:
            raise LaunchError("scheduled window has already begun; no retry permitted")
        _prepare_exclusive_output_dirs(root)
        records.append(spawn("adaptive-manager", manager, root / "logs/adaptive-manager.log", root, replica_id=None))
        for i, row in enumerate(replicas):
            argv = row.get("argv") if isinstance(row, Mapping) else None
            if not isinstance(argv, list) or not argv:
                raise LaunchError("replica argv schema drift")
            records.append(spawn(f"replica-{i}", tuple(argv), root / f"logs/replica-{i}.log", root, replica_id=i))
        deadline = monotonic() + plan["hard_timeout_seconds"]
        while not _all_seven_e0_common(event_streams(root), start):
            if monotonic() >= deadline or raw_clock() >= start:
                raise LaunchError("all-seven E0 common commit was not observed before scheduled start")
            sleep(0.02)
        _write_once(root / ATTESTATION, {"schema_version": 1, "kind": "kauri-n7-sustained-role-prearm-v1", "run_id": _option(manager, "--structured-event-run-id"), "epoch_zero_digest": e0_digest, "prearm_monotonic_ns": raw_clock(), "scheduled_start_monotonic_ns": start, "no_retry": True})
        while raw_clock() < end:
            if monotonic() >= deadline: raise LaunchError("hard timeout expired before fixed horizon")
            sleep(0.05)
        _await_fixed_manager_completion(
            root, records[0], run_id=_option(manager, "--structured-event-run-id"),
            profile_sha256=plan["native_fault_schedule"]["descriptor"]["sha256"],
            e0_digest=e0_digest, start=start, end=end, deadline=deadline,
            monotonic=monotonic, sleep=sleep,
        )
        receipt_cleanup = cleanup(root, records); records = []
        exit_codes = _clean_exit_codes(receipt_cleanup)
        _persist_cleanup(root, receipt_cleanup)
        final = {"schema_version": 1, "kind": FINAL_KIND, "state": "FIXED_E0_HORIZON_COMPLETED_NO_SUCCESSOR", "plan_sha256": plan["plan_sha256"], "authorization_sha256": _sha(_canonical(approval)), "e0_identity_sha256": _sha(e0_path.read_bytes()), "no_retry": True}
        _write_once(root / FINALIZATION, final)
        _fixed_manager_terminal(
            root, run_id=_option(manager, "--structured-event-run-id"),
            profile_sha256=plan["native_fault_schedule"]["descriptor"]["sha256"],
            e0_digest=e0_digest, start=start, end=end,
        )
        archived = {
            "profile": _archive_input(root, HERE / "sustained_role_profile.py", "sustained-role-profile.py"),
            "epoch0_tree": _archive_input(root, Path(plan["epoch0"]["tree"]["path"]), "epoch0.tree"),
            "main_config": _archive_input(root, Path(plan["configuration"]["main"]["path"]), "main.conf"),
            "hotstuff_app": _archive_input(root, Path(plan["commands"]["replicas"][0]["argv"][0]), "hotstuff-app"),
            "adaptation_manager": _archive_input(root, Path(plan["commands"]["manager"]["argv"][0]), "adaptation-manager"),
            "e0_identity_helper": _archive_input(root, e0_helper, "e0-identity-helper"),
        }
        replica_config_archives = [_archive_input(root, Path(row["path"]), f"replica-{i}.conf") for i, row in enumerate(plan["configuration"]["replicas"])]
        anchor = _physical_anchor_and_coverage(root, first_omission_tree=first_omission_tree)
        artifacts: dict[str, Any] = {
            "profile": _descriptor(root, archived["profile"]), "epoch0_tree": _descriptor(root, archived["epoch0_tree"]),
            "main_config": _descriptor(root, archived["main_config"]), "execution_plan": _descriptor(root, root / PLAN),
            "authorization_request": _descriptor(root, root / REQUEST), "approved_authorization": _descriptor(root, root / APPROVAL),
            "finalization_receipt": _descriptor(root, root / FINALIZATION), "fault_window_attestation": _descriptor(root, root / ATTESTATION),
            "e0_identity_receipt": _descriptor(root, e0_path),
            "e0_identity_helper": _descriptor(root, archived["e0_identity_helper"]),
            "manager_events": _descriptor(root, root / "raw/adaptive-manager.jsonl"), "manager_log": _descriptor(root, root / "logs/adaptive-manager.log"),
            "cleanup": _descriptor(root, root / CLEANUP),
            "replica_events": [_descriptor(root, root / f"raw/replica-{i}.jsonl") for i in range(7)],
            "replica_logs": [_descriptor(root, root / f"logs/replica-{i}.log") for i in range(7)],
            "replica_configs": [_descriptor(root, path) for path in replica_config_archives],
            "executables": {"hotstuff_app": _descriptor(root, archived["hotstuff_app"]), "adaptation_manager": _descriptor(root, archived["adaptation_manager"])},
        }
        binding = {"request_sha256": _sha(request_bytes), "approval_sha256": _sha(_canonical(approval)), "e0_digest": e0_digest, "scheduled_window": {"start_monotonic_ns": start, "end_monotonic_ns": end}, "manager_argv_sha256": _sha(_canonical({"argv": list(manager)})), "manager_executable_sha256": plan["commands"]["manager"]["executable_sha256"], "replica_argv_sha256": [row["sha256"] for row in plan["commands"]["replicas"]], "replica_executable_sha256": plan["commands"]["replicas"][0]["executable_sha256"], "native_profile_sha256": plan["native_fault_schedule"]["descriptor"]["sha256"], "selection_profile_sha256": plan["manager_selection_policy"]["descriptor"]["sha256"], "exit_codes": exit_codes}
        if first_omission_tree is not None:
            binding["first_omission_tree"] = first_omission_tree
        receipt = {"schema_version": 1, "kind": KIND, "state": "SEALED_RAW_BUNDLE_NO_CLAIM", "arm": "fixed_e0", "run_id": _option(manager, "--structured-event-run-id"), "plan_sha256": plan["plan_sha256"], "anchor": anchor, "horizon": {"clock": "CLOCK_MONOTONIC_RAW", "duration_ns": 60_000_000_000, "late_offset_ns": 20_000_000_000}, "artifacts": artifacts, "launch_binding": binding}
        receipt["receipt_sha256"] = _sha(_canonical(receipt)); _write_once(root / RECEIPT, receipt)
        return {"status": "SEALED_RAW_BUNDLE_NO_CLAIM", "receipt_sha256": receipt["receipt_sha256"]}
    except BaseException as exc:
        if records:
            try: cleanup(root, records)
            except BaseException: pass
        if not (root / ABORT).exists():
            _write_once(root / ABORT, {"schema_version": 1, "state": "ABORTED_NO_RETRY", "reason": str(exc)[:512], "no_retry": True, "claim_boundary": "No figure or thesis claim."})
        raise LaunchError(str(exc)) from exc


def _production_cleanup(root: Path, records: Sequence[Any]) -> Mapping[str, Any]:
    """Use the established process-group cleaner; the plan has no port lease."""
    local = _load("w19_sustained_local_runtime", HERE / "run_local.py")
    return local._cleanup_receipt(root, _run_id_from_plan(root), records, ())


def _run_id_from_plan(root: Path) -> str:
    plan, _ = _read(root / PLAN, "sustained-role plan")
    manager = plan.get("commands", {}).get("manager", {})
    argv = manager.get("argv") if isinstance(manager, Mapping) else None
    if not isinstance(argv, list):
        raise LaunchError("plan manager argv is invalid")
    return _option(argv, "--structured-event-run-id")


def execute_production_fixed_e0_pilot(run_directory: Path, authorization_path: Path,
                                      *, expected_authorization_sha256: str) -> dict[str, Any]:
    """Production adapter used solely by the explicit CLI execution command."""
    base = _load("w19_sustained_base_runtime", KAURI / "experiments/adaptive/n7-crash-recovery/run.py")
    return execute_fixed_e0_pilot(
        run_directory, authorization_path,
        spawn=base.spawn_process,
        event_streams=base._event_streams,
        cleanup=_production_cleanup,
        raw_clock=lambda: time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW),
        expected_authorization_sha256=expected_authorization_sha256,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one authorized W19 fixed-E0 local pilot")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--expected-authorization-sha256", required=True)
    parser.add_argument("--execute", action="store_true", help="explicitly permit the one-shot local launch")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("refusing to launch without --execute")
    try:
        result = execute_production_fixed_e0_pilot(
            args.run_root, args.authorization,
            expected_authorization_sha256=args.expected_authorization_sha256)
    except (LaunchError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
