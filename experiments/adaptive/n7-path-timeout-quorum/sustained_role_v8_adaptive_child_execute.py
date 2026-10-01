"""Pre-spawn-only v8 adaptive child execution seam.

It deliberately constructs no subprocess and exports no execution function.
Its sole result is a fail-closed, fake-process-testable scope assembled from
immutable, archived v8 input bytes.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


PLAN = Path("runtime/sustained-role-v8-full-input-plan.json")
REQUEST = Path("runtime/sustained-role-v8-full-input-request.json")
APPROVAL = Path("runtime/sustained-role-v8-adaptive-child-launch-authorization.json")
INTENT = Path("runtime/sustained-role-v8-adaptive-child-launch-intent.json")
_SOURCES = ("adaptive-manager", *(f"replica-{replica}" for replica in range(7)))
_PROFILE_SPEC = importlib.util.spec_from_file_location("w19_v8_child_execute_profile", Path(__file__).with_name("sustained_role_v8_profile.py"))
assert _PROFILE_SPEC and _PROFILE_SPEC.loader
profile = importlib.util.module_from_spec(_PROFILE_SPEC)
_PROFILE_SPEC.loader.exec_module(profile)
_PRODUCER_SPEC = importlib.util.spec_from_file_location(
    "w19_v8_child_execute_producer",
    Path(__file__).with_name("sustained_role_v8_full_input_producer.py"),
)
assert _PRODUCER_SPEC and _PRODUCER_SPEC.loader
producer = importlib.util.module_from_spec(_PRODUCER_SPEC)
_PRODUCER_SPEC.loader.exec_module(producer)
_BUILD_BINDING_SPEC = importlib.util.spec_from_file_location(
    "w19_v8_child_execute_build_binding",
    Path(__file__).with_name("sustained_role_v8_build_binding.py"),
)
assert _BUILD_BINDING_SPEC and _BUILD_BINDING_SPEC.loader
build_binding = importlib.util.module_from_spec(_BUILD_BINDING_SPEC)
_BUILD_BINDING_SPEC.loader.exec_module(build_binding)
_ABORT_MARKERS = (
    "runtime/sustained-role-abort-finalization-v8.json", "sustained-role-v8-abort.json",
    "runtime/sustained-role-campaign-staging-abort.json", "runtime/sustained-role-campaign-launch-abort.json",
    "sustained-role-fixed-e0-abort.json", "sustained-role-adaptive-e1-abort.json",
    "runtime/sustained-role-v8-campaign-abort.json", "runtime/sustained-role-v8-abort.json",
    "runtime/sustained-role-v8-materialization-abort.json", "runtime/sustained-role-v8-full-input-abort.json",
)


class V8AdaptiveChildExecuteError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("ascii") + b"\n"
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise V8AdaptiveChildExecuteError("value is not canonical JSON") from exc


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _object(raw: bytes, label: str) -> dict[str, Any]:
    def no_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out: raise V8AdaptiveChildExecuteError(f"{label} has duplicate JSON keys")
            out[key] = value
        return out
    try: value = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise V8AdaptiveChildExecuteError(f"{label} is not JSON") from exc
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise V8AdaptiveChildExecuteError(f"{label} is not canonical JSON bytes")
    return value


def _root(path: Path) -> Path:
    root = Path(path)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise V8AdaptiveChildExecuteError("run root must be an existing absolute regular directory")
    return root


def _child(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise V8AdaptiveChildExecuteError("artifact path escapes run root")
    path = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink(): raise V8AdaptiveChildExecuteError("artifact path has a symlink ancestor")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise V8AdaptiveChildExecuteError("artifact resolved path escapes run root") from exc
    return path


def _read(root: Path, relative: Path, label: str) -> tuple[dict[str, Any], bytes]:
    path = _child(root, relative)
    if path.is_symlink() or not path.is_file(): raise V8AdaptiveChildExecuteError(f"{label} is not a regular file")
    raw = path.read_bytes()
    return _object(raw, label), raw


def _external_bytes(path: Path, expected_sha256: str) -> tuple[bytes, tuple[int, int]]:
    if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64 or
            any(character not in "0123456789abcdef" for character in expected_sha256)):
        raise V8AdaptiveChildExecuteError("expected external approval SHA-256 is invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            identity = os.fstat(stream.fileno())
            raw = stream.read()
    except OSError as exc:
        raise V8AdaptiveChildExecuteError("external approval cannot be reopened without symlinks") from exc
    if _sha(raw) != expected_sha256:
        raise V8AdaptiveChildExecuteError("external approval differs from its pinned SHA-256")
    return raw, (identity.st_dev, identity.st_ino)


def _descriptor(root: Path, descriptor: object, label: str) -> Path:
    if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "size_bytes", "sha256"}:
        raise V8AdaptiveChildExecuteError(f"{label} descriptor schema drifted")
    path = descriptor.get("path")
    if (not isinstance(path, str) or not path or type(descriptor.get("size_bytes")) is not int or
            descriptor["size_bytes"] < 0 or not isinstance(descriptor.get("sha256"), str) or len(descriptor["sha256"]) != 64):
        raise V8AdaptiveChildExecuteError(f"{label} descriptor is invalid")
    candidate = _child(root, Path(path))
    if candidate.is_symlink() or not candidate.is_file(): raise V8AdaptiveChildExecuteError(f"{label} artifact is not regular")
    raw = candidate.read_bytes()
    if len(raw) != descriptor["size_bytes"] or _sha(raw) != descriptor["sha256"]:
        raise V8AdaptiveChildExecuteError(f"{label} descriptor hash drifted")
    return candidate


def _argv_digest(argv: Sequence[str]) -> str:
    """Match the producer's canonical JSON-list argv binding exactly."""
    return _sha(_canonical(list(argv)))


def _option(argv: Sequence[str], option: str) -> str:
    positions = [index for index, value in enumerate(argv) if value == option]
    if len(positions) != 1 or positions[0] + 1 >= len(argv) or not argv[positions[0] + 1]:
        raise V8AdaptiveChildExecuteError(f"argv lacks one {option}")
    return argv[positions[0] + 1]


def _argv_executable(argv: Sequence[str], descriptor: object, root: Path, label: str) -> None:
    archived = _descriptor(root, descriptor, label)
    candidate = Path(argv[0])
    if (candidate.is_symlink() or not candidate.is_file() or candidate.resolve() != archived.resolve() or
            _sha(candidate.read_bytes()) != _sha(archived.read_bytes())):
        raise V8AdaptiveChildExecuteError(f"{label} argv executable differs from archived descriptor")


def _scope_from_launch_arguments(root: Path, descriptor: object, *, run_id: str,
                                 process_rows: object, artifacts: Mapping[str, object]) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    path = _descriptor(root, descriptor, "launch arguments")
    document = _object(path.read_bytes(), "launch arguments")
    if set(document) != {"schema_version", "processes"} or document.get("schema_version") != 1 or not isinstance(document.get("processes"), list):
        raise V8AdaptiveChildExecuteError("launch arguments schema drifted")
    if (not isinstance(process_rows, list) or len(process_rows) != 8 or
            any(not isinstance(row, Mapping) or set(row) != {"source_kind", "source_id", "source_instance", "argv", "argv_sha256"}
                for row in process_rows)):
        raise V8AdaptiveChildExecuteError("producer process closure schema drifted")
    producer = {(row["source_kind"], row["source_id"]): row for row in process_rows}
    commands: dict[str, tuple[str, ...]] = {}; instances: dict[str, str] = {}
    for item in document["processes"]:
        if not isinstance(item, Mapping) or set(item) != {"source_kind", "source_id", "argv", "effective_options"}:
            raise V8AdaptiveChildExecuteError("launch process descriptor schema drifted")
        source = item.get("source_id")
        expected_kind = "adaptation_manager" if source == "adaptive-manager" else "replica"
        argv = item.get("argv")
        row = producer.get((item.get("source_kind"), source))
        if (source not in _SOURCES or item.get("source_kind") != expected_kind or not isinstance(row, Mapping) or
                row.get("source_id") != source or not isinstance(row.get("source_instance"), str) or not row["source_instance"].startswith(f"{run_id}-") or
                not isinstance(argv, list) or not argv or any(not isinstance(value, str) or not value for value in argv) or
                row.get("argv") != argv or row.get("argv_sha256") != _argv_digest(argv)):
            raise V8AdaptiveChildExecuteError("launch process identity or argv drifted")
        if source in commands: raise V8AdaptiveChildExecuteError("launch arguments duplicate a source")
        options = item.get("effective_options")
        if not isinstance(options, Mapping): raise V8AdaptiveChildExecuteError("launch effective options are invalid")
        executable = artifacts.get("adaptation_manager" if source == "adaptive-manager" else "hotstuff_app")
        executable_path = _descriptor(root, executable, f"{source} executable")
        _argv_executable(argv, executable, root, f"{source} executable")
        if options.get("binary_sha256") != hashlib.sha256(executable_path.read_bytes()).hexdigest():
            raise V8AdaptiveChildExecuteError("launch executable identity drifted")
        if source != "adaptive-manager":
            replica = int(source.split("-", 1)[1]); configs = artifacts.get("replica_configs")
            if not isinstance(configs, list) or len(configs) != 7: raise V8AdaptiveChildExecuteError("replica config closure is invalid")
            config_path = _descriptor(root, configs[replica], f"{source} config")
            if options.get("replica_config_sha256") != hashlib.sha256(config_path.read_bytes()).hexdigest():
                raise V8AdaptiveChildExecuteError("launch replica config identity drifted")
            instance = next((line.split("=", 1)[1].strip() for line in config_path.read_text().splitlines()
                             if line.startswith("structured-event-source-instance = ")), None)
            if instance != row["source_instance"]: raise V8AdaptiveChildExecuteError("replica source instance differs from archived config")
            main = _descriptor(root, artifacts.get("main_config"), "main config")
            conf_positions = [index for index, value in enumerate(argv) if value == "--conf"]
            if len(conf_positions) != 2 or [argv[index + 1] for index in conf_positions] != [str(main), str(config_path)]:
                raise V8AdaptiveChildExecuteError("replica argv config paths differ from archived descriptors")
        commands[source], instances[source] = tuple(argv), row["source_instance"]
    if set(commands) != set(_SOURCES): raise V8AdaptiveChildExecuteError("launch arguments must bind manager and exactly seven replicas")
    manager = commands["adaptive-manager"]
    if manager.count("--convergence-deadline-seconds") != 1 or manager[manager.index("--convergence-deadline-seconds") + 1] != "12":
        raise V8AdaptiveChildExecuteError("manager argv lacks v8 12-second deadline")
    for option, expected in (("--structured-event-run-id", run_id), ("--structured-event-source-instance", instances["adaptive-manager"]), ("--required-nonresponsive", "1")):
        if _option(manager, option) != expected: raise V8AdaptiveChildExecuteError("manager selection or structured identity differs from plan")
    return commands, instances


def prepare_pre_spawn_scope(root: Path, *, approval_path: Path,
                            observed_revision: str, target_host: str,
                            linux_boot_id: str, expected_approval_sha256: str) -> dict[str, Any]:
    """Replay exact inputs into a fake-process scope; never launch anything."""
    root = _root(root)
    if any(_child(root, marker).exists() or _child(root, marker).is_symlink() for marker in
           (INTENT, Path("raw"), Path("runtime/cleanup-receipt.json"), *(Path(item) for item in _ABORT_MARKERS))):
        raise V8AdaptiveChildExecuteError("root is not a fresh no-retry pre-spawn scope")
    plan, plan_raw = _read(root, PLAN, "full-input plan")
    request, request_raw = _read(root, REQUEST, "full-input request")
    required_plan = {"schema_version", "kind", "state", "run_id", "repository_revision", "profile_id", "profile_sha256", "hard_timeout_seconds", "scheduled_window", "processes", "artifacts", "no_retry", "claim_eligible", "figure_eligible", "build_provenance", "plan_sha256"}
    required_request = {"schema_version", "kind", "plan_sha256", "run_id", "no_launch", "no_retry"}
    if (set(plan) != required_plan or set(request) != required_request or
            plan.get("kind") != "kauri-n7-sustained-role-v8-full-input-plan-v1" or plan.get("state") != "PREPARED_INPUTS_NO_LAUNCH" or
            plan.get("plan_sha256") != _sha(_canonical({key: value for key, value in plan.items() if key != "plan_sha256"})) or
            request.get("kind") != "kauri-n7-sustained-role-v8-full-input-request-v1" or request.get("plan_sha256") != plan.get("plan_sha256") or
            request.get("run_id") != plan.get("run_id") or request.get("no_launch") is not True or request.get("no_retry") is not True or
            plan.get("no_retry") is not True or plan.get("claim_eligible") is not False or plan.get("figure_eligible") is not False):
        raise V8AdaptiveChildExecuteError("plan/request binding is incomplete or drifted")
    external_approval = Path(approval_path)
    if external_approval.is_symlink() or not external_approval.is_file():
        raise V8AdaptiveChildExecuteError("external approval is not a regular file")
    try:
        external_approval.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise V8AdaptiveChildExecuteError("approval must be external to the immutable run root")
    approval_raw, approval_identity = _external_bytes(external_approval, expected_approval_sha256)
    approval = _object(approval_raw, "external approval")
    expected_approval = {"schema_version", "kind", "request_sha256", "plan_sha256", "run_id", "no_retry"}
    if (set(approval) != expected_approval or approval.get("schema_version") != 1 or approval.get("kind") != "kauri-n7-sustained-role-v8-adaptive-child-launch-authorization-v1" or
            approval.get("request_sha256") != _sha(request_raw) or approval.get("plan_sha256") != plan["plan_sha256"] or
            approval.get("run_id") != plan["run_id"] or approval.get("no_retry") is not True):
        raise V8AdaptiveChildExecuteError("external approval does not bind exact plan/request")
    try:
        producer.verify(root, expected_request_sha256=approval["request_sha256"])
    except Exception as exc:
        raise V8AdaptiveChildExecuteError("producer closure verification rejected sealed inputs") from exc
    schedule = plan.get("scheduled_window")
    if (not isinstance(schedule, Mapping) or set(schedule) != {"clock", "start_ns", "end_ns", "minimum_duration_ns"} or schedule.get("clock") != "CLOCK_MONOTONIC_RAW" or
            type(schedule.get("start_ns")) is not int or type(schedule.get("end_ns")) is not int or schedule.get("minimum_duration_ns") != 82_000_000_000 or schedule["end_ns"] - schedule["start_ns"] < 82_000_000_000):
        raise V8AdaptiveChildExecuteError("plan scheduled window does not reserve v8 82 seconds")
    artifacts = plan.get("artifacts")
    if not isinstance(artifacts, Mapping) or "launch_arguments" not in artifacts:
        raise V8AdaptiveChildExecuteError("full-input plan lacks exact launch-arguments closure")
    try:
        build = build_binding.bind(
            root=root, plan=plan, observed_revision=observed_revision,
            target_host=target_host, linux_boot_id=linux_boot_id,
        )
    except build_binding.V8BuildBindingError as exc:
        raise V8AdaptiveChildExecuteError("strict W19 build binding rejected") from exc
    commands, instances = _scope_from_launch_arguments(root, artifacts["launch_arguments"], run_id=str(plan["run_id"]), process_rows=plan.get("processes"), artifacts=artifacts)
    if (plan.get("hard_timeout_seconds") != 210 or plan.get("profile_id") != "n7-sustained-role-proposal-boundary-v8" or
            not isinstance(plan.get("profile_sha256"), str) or len(plan["profile_sha256"]) != 64):
        raise V8AdaptiveChildExecuteError("full-input plan lacks exact v8 launch closure")
    profile_path = _descriptor(root, artifacts.get("v8_profile"), "v8 materialization profile")
    if _sha(profile_path.read_bytes()) != plan["profile_sha256"]:
        raise V8AdaptiveChildExecuteError("v8 profile hash differs from archived profile bytes")
    expected_overlay = profile.native_actor_overlay(
        scheduled_start_monotonic_ns=schedule["start_ns"],
        scheduled_end_monotonic_ns=schedule["end_ns"],
    )
    if commands["replica-1"][-len(expected_overlay):] != expected_overlay:
        raise V8AdaptiveChildExecuteError("actor-1 argv lacks the exact frozen v8 native overlay")
    if any("--experiment-byzantine-mode" in commands[source]
           for source in _SOURCES if source != "replica-1"):
        raise V8AdaptiveChildExecuteError("v8 native overlay appears outside actor 1")
    approval_raw_after, approval_identity_after = _external_bytes(external_approval, expected_approval_sha256)
    if (approval_raw_after != approval_raw or approval_identity_after != approval_identity or
            _read(root, PLAN, "full-input plan")[1] != plan_raw or _read(root, REQUEST, "full-input request")[1] != request_raw):
        raise V8AdaptiveChildExecuteError("sealed inputs or external approval changed after verification")
    archived = _child(root, APPROVAL)
    archived.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(archived, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream: stream.write(approval_raw)
    intent = {"schema_version": 1, "kind": "kauri-n7-sustained-role-v8-adaptive-child-launch-intent-v1", "state": "PRESPAWN_INTENT_SEALED_NO_LAUNCH", "plan_sha256": plan["plan_sha256"], "request_sha256": _sha(request_raw), "approval_sha256": _sha(approval_raw), "build_binding": build, "run_id": plan["run_id"], "no_retry": True, "hard_scope_seconds": 210, "claim_eligible": False, "figure_eligible": False}
    intent["intent_sha256"] = _sha(_canonical(intent))
    with os.fdopen(os.open(_child(root, INTENT), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream: stream.write(_canonical(intent))
    return {"state": intent["state"], "run_id": plan["run_id"],
            "plan_sha256": plan["plan_sha256"], "request_sha256": _sha(request_raw),
            "approval_sha256": _sha(approval_raw),
            "scheduled_window": {"clock": schedule["clock"], "start_ns": schedule["start_ns"],
                                 "end_ns": schedule["end_ns"]},
            "commands": commands, "source_instances": instances,
            "build_binding": build, "hard_scope_seconds": 210, "no_retry": True,
            "no_launch": True, "claim_eligible": False, "figure_eligible": False}
