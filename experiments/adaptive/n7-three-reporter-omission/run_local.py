#!/usr/bin/env python3
"""No-retry local producer for the frozen N=7 three-reporter study.

PREPARE materializes the final process argv and an authorization request but
does not launch.  FINALIZE accepts only a separately supplied, exact-plan
authorization receipt and freezes the approved preflight.  EXECUTE remains an
explicit subcommand; importing this module or running PREPARE/FINALIZE cannot
start a process.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from typing import Any, Mapping, Sequence
import uuid


HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


adapter = _load("n7_three_local_adapter_for_producer", HERE / "local_adapter.py")
runner = adapter.runner
base = adapter.base
validator = _load("n7_three_validator_for_producer", HERE / "validator.py")

EXECUTION_PLAN = Path("runtime/n7-local-execution-plan.json")
AUTHORIZATION_REQUEST = Path("runtime/execution-authorization-request.json")
APPROVED_AUTHORIZATION = Path("runtime/approved-execution-authorization.json")
APPROVED_PREFLIGHT = Path("runtime/approved-preflight.json")
FINAL_LAUNCH_ARGUMENTS = Path("runtime/final-launch-arguments.json")
FAULT_WINDOW_ARM = Path("runtime/fault-window-arm.json")
OMISSION_GATE = Path("runtime/static-omission-gate.json")
MANAGER_EVENTS = Path("raw/adaptive-manager.jsonl")

PLAN_KIND = "kauri-n7-local-execution-plan-v1"
REQUEST_KIND = "kauri-n7-local-execution-authorization-request-v1"
AUTHORIZATION_KIND = "kauri-n7-local-execution-authorization-v1"
ARGV_HASH_DOMAIN = "kauri-n7-replica-argv-without-self-hash-v1"
GATE_KIND = "kauri-n7-static-aggregate-omission-gate-v1"
ARM_KIND = "kauri-focused-fault-window-arm-v4"
PROFILE_ID = runner.SCENARIO
PROFILE_SHA256 = adapter.PROFILE_V2_SHA256
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


class ProducerError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise ProducerError("document is not canonical JSON") from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _execution_plan_digest(plan: Mapping[str, Any]) -> str:
    semantic = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return _sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode("ascii")
    )


def _hex64(value: object, label: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ProducerError(f"{label} is not a lower-case SHA-256")
    return value


def _safe_child(run_directory: Path, relative: Path | str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ProducerError("producer path is not a safe relative child")
    root = run_directory.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ProducerError("producer path escapes the run root") from exc
    return candidate


def _write_exclusive(path: Path, payload: bytes) -> None:
    if path.is_symlink() or path.exists() or not path.parent.is_dir():
        raise ProducerError(f"refusing to replace producer artifact {path.name}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _publish_atomic_once(path: Path, payload: bytes) -> None:
    """Publish complete bytes atomically without replacing an existing file."""
    if path.is_symlink() or path.exists() or not path.parent.is_dir():
        raise ProducerError(f"refusing to replace one-shot artifact {path.name}")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError as exc:
        raise ProducerError(f"one-shot artifact already exists: {path.name}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_canonical_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ProducerError(f"{label} is not a bounded regular file")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProducerError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict) or raw != _canonical(value):
        raise ProducerError(f"{label} bytes are not canonical newline JSON")
    return value, raw


def _one_option(command: Sequence[str], option: str) -> str:
    if command.count(option) != 1:
        raise ProducerError(f"command must contain exactly one {option}")
    position = command.index(option)
    if position + 1 >= len(command) or not command[position + 1]:
        raise ProducerError(f"command has no value for {option}")
    return command[position + 1]


def _artifact(plan: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    matches = [
        item for item in plan.get("runtime_artifacts", ())
        if isinstance(item, Mapping) and item.get("kind") == kind
    ]
    if len(matches) != 1:
        raise ProducerError(f"base plan must bind exactly one {kind} artifact")
    return matches[0]


def _without_self_hash(command: Sequence[str]) -> tuple[str, ...]:
    option = "--experiment-omission-activation-gate-launch-argv-sha256"
    if command.count(option) > 1:
        raise ProducerError("replica command repeats the launch argv hash option")
    if option not in command:
        return tuple(command)
    position = command.index(option)
    if position + 1 >= len(command):
        raise ProducerError("replica command has no launch argv hash value")
    return tuple((*command[:position], *command[position + 2 :]))


def replica_launch_argv_sha256(command_without_hash: Sequence[str]) -> str:
    command = _without_self_hash(command_without_hash)
    payload = _canonical(
        {"schema_version": 1, "domain": ARGV_HASH_DOMAIN, "argv": list(command)}
    )
    return _sha256(payload)


def _final_commands(
    run_directory: Path,
    base_plan: Mapping[str, Any],
    manager_command: Sequence[str],
    replica_commands: Sequence[Sequence[str]],
    *,
    hard_timeout_seconds: int,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...], dict[str, Any]]:
    if type(hard_timeout_seconds) is not int or not 30 <= hard_timeout_seconds <= 1800:
        raise ProducerError("hard timeout must be an integer from 30 to 1800 seconds")
    run_id = _one_option(manager_command, "--structured-event-run-id")
    manager_instance = _one_option(
        manager_command, "--structured-event-source-instance"
    )
    identity = base_plan.get("e0_identity")
    if not isinstance(identity, Mapping):
        raise ProducerError("base plan lacks E0 identity")
    epoch_digest = _hex64(identity.get("epoch_digest"), "E0 digest")
    tree_sha256 = _hex64(identity.get("tree_file_sha256"), "E0 tree hash")
    request_artifact = _artifact(base_plan, "transition_requests")
    request_sha256 = _hex64(
        request_artifact.get("sha256"), "transition request artifact hash"
    )

    manager_extra = (
        "--fault-window-arm-path", str(_safe_child(run_directory, FAULT_WINDOW_ARM)),
        "--fault-window-arm-schema-version", "4",
        "--fault-window-arm-domain", ARM_KIND,
        "--fault-window-arm-run-id", run_id,
        "--fault-window-arm-profile-id", PROFILE_ID,
        "--fault-window-arm-profile-sha256", PROFILE_SHA256,
        "--fault-window-arm-topology-proof-sha256", tree_sha256,
        "--fault-window-arm-request-sha256", request_sha256,
        "--fault-window-arm-epoch-number", "0",
        "--fault-window-arm-epoch-digest", epoch_digest,
        "--fault-window-arm-prefault-tree-id", "4",
        "--fault-window-arm-required-tree-positions", "3",
        "--fault-window-arm-deadline-seconds", str(hard_timeout_seconds),
        "--fault-window-arm-timeout-evidence-basis", "exact_timeout_attempt_id_v1",
        "--fault-window-arm-required-observation-schema", "3",
        "--fault-window-arm-clock-domain", "same_host_clock_monotonic_raw",
        "--fault-window-arm-snapshot-evidence-basis", "exact_post_fault_attempt_start_v1",
        "--fault-window-arm-selection-cardinality-policy", "all_guarded_up_to_fault_bound_v1",
    )
    for option in manager_extra[::2]:
        if option in manager_command:
            raise ProducerError(f"base manager command already contains {option}")
    final_manager = tuple((*manager_command, *manager_extra))

    if len(replica_commands) != 7:
        raise ProducerError("base plan must contain exactly seven replica commands")
    gate_without_hash = (
        "--experiment-omission-activation-gate-path", str(_safe_child(run_directory, OMISSION_GATE)),
        "--experiment-omission-activation-gate-manager-events", str(_safe_child(run_directory, MANAGER_EVENTS)),
        "--experiment-omission-activation-gate-run-id", run_id,
        "--experiment-omission-activation-gate-manager-source-instance", manager_instance,
        "--experiment-omission-activation-gate-profile-sha256", PROFILE_SHA256,
        "--experiment-omission-activation-gate-tree-sha256", tree_sha256,
    )
    commands = [tuple(command) for command in replica_commands]
    actor = commands[runner.OMITTING_REPLICA]
    if any(option in actor for option in gate_without_hash[::2]):
        raise ProducerError("base actor command already contains an activation gate")
    actor_without_hash = tuple((*actor, *gate_without_hash))
    launch_hash = replica_launch_argv_sha256(actor_without_hash)
    commands[runner.OMITTING_REPLICA] = (
        *actor_without_hash,
        "--experiment-omission-activation-gate-launch-argv-sha256",
        launch_hash,
    )
    for replica_id, command in enumerate(commands):
        if replica_id != runner.OMITTING_REPLICA and any(
            value.startswith("--experiment-omission-activation-gate-")
            for value in command
        ):
            raise ProducerError("a non-actor replica contains an activation gate")
    binding = {
        "run_id": run_id,
        "manager_source_instance": manager_instance,
        "epoch_digest": epoch_digest,
        "tree_file_sha256": tree_sha256,
        "topology_proof_sha256": tree_sha256,
        "transition_request_sha256": request_sha256,
        "replica_1_launch_argv_sha256": launch_hash,
        "replica_1_launch_argv_sha256_domain": ARGV_HASH_DOMAIN,
    }
    return final_manager, tuple(commands), binding


def _declared_ports(
    run_directory: Path, base_plan: Mapping[str, Any], manager_command: Sequence[str]
) -> tuple[int, ...]:
    main_path = _safe_child(run_directory, base_plan["main_config"])
    try:
        lines = main_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ProducerError(f"cannot read main configuration ports: {exc}") from exc
    peer_client = []
    pattern = re.compile(r"replica = 127\.0\.0\.1:(\d+);(\d+), ")
    for line in lines:
        match = pattern.match(line)
        if match is not None:
            peer_client.append((int(match.group(1)), int(match.group(2))))
    if len(peer_client) != 7:
        raise ProducerError("main configuration does not declare exactly seven port pairs")
    listen = _one_option(manager_command, "--listen")
    match = re.fullmatch(r"127\.0\.0\.1:(\d+)", listen)
    if match is None:
        raise ProducerError("manager listen address is not exact loopback")
    ports = tuple(port for pair in peer_client for port in pair) + (int(match.group(1)),)
    if len(set(ports)) != 15 or any(port <= 1024 or port > 65535 for port in ports):
        raise ProducerError("declared local ports overlap or are out of bounds")
    return ports


def prepare(
    run_directory: Path,
    *,
    hard_timeout_seconds: int = 300,
    e0_helper_invoke=None,
) -> dict[str, Any]:
    """Freeze exact launch inputs and emit an unsigned approval request only."""
    run_directory = run_directory.resolve()
    plan_path = _safe_child(run_directory, "local-launch-plan.json")
    try:
        base_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProducerError(f"cannot read base local plan: {exc}") from exc
    if (
        not isinstance(base_plan, Mapping)
        or base_plan.get("state") != "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED"
        or base_plan.get("scenario") != PROFILE_ID
        or base_plan.get("plan_sha256") != adapter._plan_digest(base_plan)
    ):
        raise ProducerError("base local plan is not integrity-bound")
    repository_revision = base_plan.get("repository_revision")
    if (
        not isinstance(repository_revision, str)
        or len(repository_revision) != 40
        or any(character not in "0123456789abcdef" for character in repository_revision)
    ):
        raise ProducerError("base local plan lacks one exact repository revision")
    verify_kwargs = {}
    if e0_helper_invoke is not None:
        verify_kwargs["e0_helper_invoke"] = e0_helper_invoke
    manager, replicas = adapter._verify_executable_local_plan(
        run_directory, base_plan, **verify_kwargs
    )
    issuer_relative = base_plan.get("issuer_public_key")
    issuer_sha256 = _hex64(
        base_plan.get("issuer_public_key_sha256"), "issuer public key hash"
    )
    if not isinstance(issuer_relative, str):
        raise ProducerError("base plan lacks archived issuer public key")
    issuer_path = _safe_child(run_directory, issuer_relative)
    if base.sha256_file(issuer_path) != issuer_sha256:
        raise ProducerError("archived issuer public key hash changed")

    final_manager, final_replicas, binding = _final_commands(
        run_directory,
        base_plan,
        manager,
        replicas,
        hard_timeout_seconds=hard_timeout_seconds,
    )
    declared_ports = _declared_ports(run_directory, base_plan, final_manager)
    app_binary = Path(final_replicas[0][0]).resolve()
    manager_binary = Path(final_manager[0]).resolve()
    for binary, label in ((app_binary, "hotstuff-app"), (manager_binary, "adaptation-manager")):
        try:
            base._assert_executable(binary, label)
        except base.RunnerError as exc:
            raise ProducerError(str(exc)) from exc
    executables = {
        "hotstuff_app": {"path": str(app_binary), "sha256": base.sha256_file(app_binary)},
        "adaptation_manager": {"path": str(manager_binary), "sha256": base.sha256_file(manager_binary)},
    }
    original_launch = _safe_child(run_directory, "runtime/launch-arguments.json")
    try:
        launch = json.loads(original_launch.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProducerError(f"cannot read archived launch arguments: {exc}") from exc
    processes = launch.get("processes") if isinstance(launch, Mapping) else None
    if not isinstance(processes, list) or len(processes) != 8:
        raise ProducerError("archived launch arguments have schema drift")
    for process in processes:
        source = process.get("source_id") if isinstance(process, dict) else None
        if source == "adaptive-manager":
            process["argv"] = base.normalized_manager_argv(final_manager)
            process.setdefault("effective_options", {})["fault_window_arm_v4"] = True
        elif isinstance(source, str) and source.startswith("replica-"):
            try:
                replica_id = int(source.removeprefix("replica-"))
            except ValueError as exc:
                raise ProducerError("launch arguments contain an invalid replica source") from exc
            process["argv"] = list(final_replicas[replica_id])
            process.setdefault("effective_options", {})["post_baseline_omission_gate"] = (
                replica_id == runner.OMITTING_REPLICA
            )
    final_launch_path = _safe_child(run_directory, FINAL_LAUNCH_ARGUMENTS)
    final_launch_bytes = _canonical(launch)
    _write_exclusive(final_launch_path, final_launch_bytes)

    execution_plan = {
        "schema_version": 1,
        "kind": PLAN_KIND,
        "scenario": PROFILE_ID,
        "run_id": binding["run_id"],
        "state": "PREPARED_EXTERNAL_APPROVAL_REQUIRED",
        "claim_boundary": "no process launched; no approval created by producer",
        "base_plan_sha256": base_plan["plan_sha256"],
        "repository_revision": repository_revision,
        "profile_sha256": PROFILE_SHA256,
        "issuer_public_key": issuer_relative,
        "issuer_public_key_sha256": issuer_sha256,
        "approved_preflight": str(APPROVED_PREFLIGHT),
        "authorization_request": str(AUTHORIZATION_REQUEST),
        "approved_authorization": str(APPROVED_AUTHORIZATION),
        "final_launch_arguments": str(FINAL_LAUNCH_ARGUMENTS),
        "final_launch_arguments_sha256": _sha256(final_launch_bytes),
        "fault_window_arm": str(FAULT_WINDOW_ARM),
        "omission_gate": str(OMISSION_GATE),
        "manager_events": str(MANAGER_EVENTS),
        "hard_timeout_seconds": hard_timeout_seconds,
        "declared_ports": list(declared_ports),
        "executables": executables,
        "no_retry": True,
        "bindings": binding,
        "manager_command": list(final_manager),
        "replica_commands": [list(command) for command in final_replicas],
    }
    execution_plan["plan_sha256"] = _execution_plan_digest(execution_plan)
    execution_plan_bytes = _canonical(execution_plan)
    execution_plan_sha256 = execution_plan["plan_sha256"]
    plan_output = _safe_child(run_directory, EXECUTION_PLAN)
    _write_exclusive(plan_output, execution_plan_bytes)
    request = {
        "schema_version": 1,
        "kind": REQUEST_KIND,
        "scenario": PROFILE_ID,
        "execution_plan_sha256": execution_plan_sha256,
        "base_plan_sha256": base_plan["plan_sha256"],
        "repository_revision": repository_revision,
        "final_launch_arguments_sha256": execution_plan["final_launch_arguments_sha256"],
        "issuer_public_key_sha256": issuer_sha256,
        "replica_1_launch_argv_sha256": binding["replica_1_launch_argv_sha256"],
        "hard_timeout_seconds": hard_timeout_seconds,
        "no_retry": True,
    }
    request_bytes = _canonical(request)
    _write_exclusive(_safe_child(run_directory, AUTHORIZATION_REQUEST), request_bytes)
    return {
        "state": execution_plan["state"],
        "execution_plan_sha256": execution_plan_sha256,
        "authorization_request_sha256": _sha256(request_bytes),
        "authorization_request": str(AUTHORIZATION_REQUEST),
        "claim_boundary": execution_plan["claim_boundary"],
    }


def prepare_inputs(
    run_directory: Path,
    *,
    app_binary: Path,
    manager_binary: Path,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    e0_helper_binary: Path,
    peer_port: int,
    client_port: int,
    manager_port: int,
    hard_timeout_seconds: int = 300,
    verify_repository=base.verify_repository_state,
    generate_identities=base.generate_identities,
    prepare_adapter=adapter.prepare_local_inputs,
    prepare_execution=prepare,
) -> dict[str, Any]:
    """Create one fresh, fully reproducible PREPARE root without launching."""
    run_directory = run_directory.resolve()
    if run_directory.exists() or run_directory.is_symlink():
        raise ProducerError("prepare-inputs requires an absent run root")
    run_id = run_directory.name
    if (
        not run_id
        or len(run_id) > 128
        or re.fullmatch(r"[A-Za-z0-9_.-]+", run_id) is None
    ):
        raise ProducerError("run-root basename is not a safe structured-event run ID")
    binaries = {
        "hotstuff-app": app_binary.resolve(),
        "adaptation-manager": manager_binary.resolve(),
        "hotstuff-keygen": keygen_binary.resolve(),
        "hotstuff-tls-keygen": tls_keygen_binary.resolve(),
        "n7-epoch0-treefile-digest": e0_helper_binary.resolve(),
    }
    for label, path in binaries.items():
        try:
            base._assert_executable(path, label)
        except base.RunnerError as exc:
            raise ProducerError(str(exc)) from exc
    try:
        snapshot = verify_repository(adapter.KAURI)
        ports = base.required_ports(peer_port, client_port, manager_port)
    except base.RunnerError as exc:
        raise ProducerError(str(exc)) from exc
    occupied = base.ports_in_use(ports)
    if occupied:
        raise ProducerError(f"prepare-inputs ports are already occupied: {occupied}")

    run_directory.mkdir(parents=True, mode=0o700)
    for child in ("raw", "logs", "config"):
        (run_directory / child).mkdir(mode=0o700)
    profile_bytes = adapter.PROFILE_V2_FILE.read_bytes()
    if _sha256(profile_bytes) != PROFILE_SHA256:
        raise ProducerError("frozen profile changed before input preparation")
    base._write_private(run_directory / "profile.json", profile_bytes)
    source_instances = {
        f"replica-{replica}": f"{run_id}-replica-{replica}-{uuid.uuid4().hex}"
        for replica in range(7)
    }
    source_instances["adaptive-manager"] = f"{run_id}-manager-{uuid.uuid4().hex}"
    try:
        bls, tls, issuer = generate_identities(
            binaries["hotstuff-keygen"],
            binaries["hotstuff-tls-keygen"],
            run_directory / "config",
        )
        profile = adapter._load_frozen_v2_profile()
        prepare_adapter(
            run_directory,
            profile,
            bls,
            tls,
            issuer,
            peer_port=peer_port,
            client_port=client_port,
            manager_port=manager_port,
            run_id=run_id,
            repository_revision=snapshot.revision,
            source_instances=source_instances,
            app_binary=binaries["hotstuff-app"],
            manager_binary=binaries["adaptation-manager"],
            e0_helper_binary=binaries["n7-epoch0-treefile-digest"],
        )
        result = prepare_execution(
            run_directory, hard_timeout_seconds=hard_timeout_seconds
        )
    except (OSError, base.RunnerError, adapter.AdapterError) as exc:
        raise ProducerError(f"prepare-inputs failed after preserving {run_directory}: {exc}") from exc
    return {
        **result,
        "run_root": str(run_directory),
        "repository_revision": snapshot.revision,
        "declared_ports": list(ports),
    }


def _valid_approved_utc(value: object) -> bool:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def finalize(run_directory: Path, authorization_path: Path) -> dict[str, Any]:
    """Verify an external approval receipt and freeze the approved preflight."""
    run_directory = run_directory.resolve()
    plan, plan_bytes = _read_canonical_object(
        _safe_child(run_directory, EXECUTION_PLAN), "execution plan"
    )
    request, request_bytes = _read_canonical_object(
        _safe_child(run_directory, AUTHORIZATION_REQUEST), "authorization request"
    )
    authorization, authorization_bytes = _read_canonical_object(
        authorization_path.resolve(), "external authorization"
    )
    expected_authorization_fields = {
        "schema_version", "kind", "request_sha256", "execution_plan_sha256",
        "approval_reference", "approved_utc", "no_retry",
    }
    if (
        set(authorization) != expected_authorization_fields
        or authorization.get("schema_version") != 1
        or authorization.get("kind") != AUTHORIZATION_KIND
        or authorization.get("request_sha256") != _sha256(request_bytes)
        or authorization.get("execution_plan_sha256") != plan.get("plan_sha256")
        or authorization.get("no_retry") is not True
        or not isinstance(authorization.get("approval_reference"), str)
        or not authorization["approval_reference"].strip()
        or not _valid_approved_utc(authorization.get("approved_utc"))
    ):
        raise ProducerError("external authorization is not bound to this exact no-retry plan")
    if (
        plan.get("state") != "PREPARED_EXTERNAL_APPROVAL_REQUIRED"
        or plan.get("plan_sha256") != _execution_plan_digest(plan)
        or request.get("kind") != REQUEST_KIND
        or request.get("execution_plan_sha256") != plan.get("plan_sha256")
        or request.get("no_retry") is not True
    ):
        raise ProducerError("prepared request and execution plan are inconsistent")
    archived_authorization = _safe_child(run_directory, APPROVED_AUTHORIZATION)
    if authorization_path.resolve() == archived_authorization:
        raise ProducerError("authorization must be supplied from outside its archive destination")
    _write_exclusive(archived_authorization, authorization_bytes)

    try:
        base_plan = json.loads(
            _safe_child(run_directory, "local-launch-plan.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProducerError(f"cannot reread base plan: {exc}") from exc
    preflight = base_plan.get("preflight") if isinstance(base_plan, Mapping) else None
    if not isinstance(preflight, Mapping):
        raise ProducerError("base plan lacks preflight")
    approved_preflight = dict(preflight)
    approved_preflight.update(
        {
            "approved_issuer_public_key_sha256": plan["issuer_public_key_sha256"],
            "approved_plan_authorization_sha256": _sha256(authorization_bytes),
            "approved_plan_request_sha256": _sha256(request_bytes),
        }
    )
    approved_preflight_bytes = _canonical(approved_preflight)
    _write_exclusive(
        _safe_child(run_directory, APPROVED_PREFLIGHT), approved_preflight_bytes
    )
    receipt = {
        "schema_version": 1,
        "scenario": PROFILE_ID,
        "state": "FINALIZED_EXECUTION_EXPLICITLY_REQUIRED",
        "execution_plan_sha256": plan["plan_sha256"],
        "authorization_request_sha256": _sha256(request_bytes),
        "authorization_sha256": _sha256(authorization_bytes),
        "approved_preflight_sha256": _sha256(approved_preflight_bytes),
        "claim_boundary": "external approval verified; no process launched",
    }
    _write_exclusive(
        _safe_child(run_directory, "runtime/finalization-receipt.json"),
        _canonical(receipt),
    )
    return receipt


def build_fault_window_arm(
    execution_plan: Mapping[str, Any],
    *,
    authorization_sha256: str,
    evidence_start_monotonic_ns: int,
) -> dict[str, Any]:
    binding = execution_plan.get("bindings")
    if not isinstance(binding, Mapping):
        raise ProducerError("execution plan lacks bindings")
    if type(evidence_start_monotonic_ns) is not int or evidence_start_monotonic_ns <= 0:
        raise ProducerError("arm evidence start is not a positive RAW timestamp")
    return {
        "schema_version": 4,
        "kind": ARM_KIND,
        "run_id": binding["run_id"],
        "profile_id": PROFILE_ID,
        "profile_sha256": PROFILE_SHA256,
        "topology_proof_sha256": binding["topology_proof_sha256"],
        "request_sha256": binding["transition_request_sha256"],
        "epoch_number": 0,
        "epoch_digest": binding["epoch_digest"],
        "fault_receipt_sha256": _hex64(authorization_sha256, "authorization hash"),
        "evidence_start_monotonic_ns": evidence_start_monotonic_ns,
        "prefault_tree_id": 4,
        "required_tree_positions": 3,
        "required_tree_ids": [4, 5, 6],
        "clock_domain": "same_host_clock_monotonic_raw",
        "required_observation_schema": 3,
        "timeout_evidence_basis": "exact_timeout_attempt_id_v1",
        "snapshot_evidence_basis": "exact_post_fault_attempt_start_v1",
        "selection_cardinality_policy": "all_guarded_up_to_fault_bound_v1",
    }


def build_omission_gate(
    execution_plan: Mapping[str, Any],
    *,
    manager_source_sequence: int,
    manager_event_line_sha256: str,
    activation_monotonic_ns: int,
) -> dict[str, Any]:
    binding = execution_plan.get("bindings")
    if not isinstance(binding, Mapping):
        raise ProducerError("execution plan lacks bindings")
    if type(manager_source_sequence) is not int or manager_source_sequence <= 0:
        raise ProducerError("manager source sequence is invalid")
    if type(activation_monotonic_ns) is not int or activation_monotonic_ns <= 0:
        raise ProducerError("gate activation timestamp is invalid")
    return {
        "schema_version": 1,
        "kind": GATE_KIND,
        "profile_sha256": PROFILE_SHA256,
        "tree_file_sha256": binding["tree_file_sha256"],
        "epoch_digest": binding["epoch_digest"],
        "replica_id": runner.OMITTING_REPLICA,
        "launch_argv_sha256": binding["replica_1_launch_argv_sha256"],
        "manager_run_id": binding["run_id"],
        "manager_source_instance": binding["manager_source_instance"],
        "manager_source_sequence": manager_source_sequence,
        "fault_window_arm_event_sha256": _hex64(
            manager_event_line_sha256, "manager arm event line hash"
        ),
        "activation_monotonic_ns": activation_monotonic_ns,
    }


def gate_bytes(gate: Mapping[str, Any]) -> bytes:
    """Encode the native gate's deliberately ordered canonical wire format."""
    expected = (
        "schema_version", "kind", "profile_sha256", "tree_file_sha256",
        "epoch_digest", "replica_id", "launch_argv_sha256", "manager_run_id",
        "manager_source_instance", "manager_source_sequence",
        "fault_window_arm_event_sha256", "activation_monotonic_ns",
    )
    if tuple(gate) != expected:
        raise ProducerError("omission gate field order or schema drifted")
    return json.dumps(gate, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"


def _descriptor(run_directory: Path, path: Path) -> dict[str, str]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(run_directory.resolve())
    except ValueError as exc:
        raise ProducerError("receipt artifact escapes the run root") from exc
    if path.is_symlink() or not path.is_file():
        raise ProducerError(f"receipt artifact is unavailable: {relative}")
    return {"path": str(relative), "sha256": base.sha256_file(path)}


def _raw_clock_ns() -> int:
    clock = getattr(time, "CLOCK_MONOTONIC_RAW", None)
    if clock is None:
        raise ProducerError("CLOCK_MONOTONIC_RAW is unavailable on this host")
    value = time.clock_gettime_ns(clock)
    if value <= 0:
        raise ProducerError("CLOCK_MONOTONIC_RAW returned an invalid timestamp")
    return value


def _check_live(records: Sequence[Any], allow_clean_manager_exit=None) -> None:
    for record in records:
        returncode = record.process.poll()
        if returncode is not None:
            if (
                record.name == "adaptive-manager"
                and returncode == 0
                and allow_clean_manager_exit is not None
                and allow_clean_manager_exit(record)
            ):
                continue
            raise ProducerError(
                f"{record.name} exited before acceptance with status {returncode}"
            )


def _wait_for(
    records: Sequence[Any], deadline: float, probe, label: str,
    *, allow_clean_manager_exit=None,
):
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        _check_live(records, allow_clean_manager_exit)
        try:
            value = probe()
            if value is not None:
                return value
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
        time.sleep(0.05)
    detail = f": {last_error}" if last_error is not None else ""
    raise ProducerError(f"hard timeout waiting for {label}{detail}")


def _jsonl_with_hashes(path: Path) -> list[tuple[dict[str, Any], str]]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ProducerError(f"structured event stream is unavailable: {path.name}")
    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise ProducerError(f"structured event stream is not complete JSONL: {path.name}")
    rows: list[tuple[dict[str, Any], str]] = []
    for line in raw.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProducerError(f"structured event stream is malformed: {path.name}") from exc
        if not isinstance(value, dict):
            raise ProducerError(f"structured event is not an object: {path.name}")
        rows.append((value, _sha256(line)))
    return rows


def _one_event_line(
    path: Path, event_type: str, *, payload_sha_field: str | None = None,
    payload_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    matches = []
    for event, line_sha256 in _jsonl_with_hashes(path):
        if event.get("event_type") != event_type:
            continue
        payload = event.get("payload")
        if payload_sha_field is not None and (
            not isinstance(payload, Mapping)
            or payload.get(payload_sha_field) != payload_sha256
        ):
            continue
        matches.append((event, line_sha256))
    if len(matches) != 1:
        raise ProducerError(f"expected exactly one source-bound {event_type} event")
    return matches[0]


def _cleanup_receipt(
    run_directory: Path, run_id: str, records: Sequence[Any], declared_ports: Sequence[int]
) -> dict[str, Any]:
    outcomes = adapter.comparison._shutdown_records(records)
    groups = {int(record.pgid) for record in records}
    if any(group <= 1 for group in groups) or os.getpgrp() in groups:
        raise ProducerError("refusing unsafe cleanup process group")

    def live_groups() -> set[int]:
        live = set()
        for group in groups:
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            live.add(group)
        return live

    for signum, grace_seconds in ((signal.SIGTERM, 0.5), (signal.SIGKILL, 0.5)):
        remaining = live_groups()
        if not remaining:
            break
        for group in remaining:
            try:
                os.killpg(group, signum)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Treat as still live; the completeness check below will fail.
                pass
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline and live_groups():
            time.sleep(0.02)
    closure_deadline = time.monotonic() + 2.0
    occupied = base.ports_in_use(declared_ports)
    remaining = live_groups()
    while time.monotonic() < closure_deadline and (occupied or remaining):
        time.sleep(0.02)
        occupied = base.ports_in_use(declared_ports)
        remaining = live_groups()
    by_source = {
        outcome.get("source_id"): outcome for outcome in outcomes
        if isinstance(outcome, Mapping)
    }
    expected = ["adaptive-manager", *(f"replica-{replica}" for replica in range(7))]
    processes = []
    for source in expected:
        outcome = by_source.get(source)
        if not isinstance(outcome, Mapping):
            raise ProducerError("cleanup did not cover all eight process groups")
        returncode = outcome.get("returncode")
        if type(returncode) is not int:
            raise ProducerError(f"cleanup did not reap {source}")
        processes.append(
            {
                "source_id": source,
                "pid": int(outcome["pid"]),
                "pgid": int(outcome["pgid"]),
                "returncode": returncode,
                "termination": "clean-exit" if returncode == 0 else "terminated",
            }
        )
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "complete": len(outcomes) == 8 and not occupied and not remaining,
        "processes": processes,
    }
    _write_exclusive(
        _safe_child(run_directory, "runtime/cleanup-receipt.json"),
        _canonical(receipt),
    )
    return receipt


def execute(
    run_directory: Path,
    authorization_path: Path,
    *,
    spawn=base.spawn_process,
    execution_enabled: bool = False,
) -> dict[str, Any]:
    """Run exactly one finalized local attempt and seal accepted or aborted evidence."""
    if execution_enabled is not True:
        raise ProducerError(
            "EXECUTE is disabled pending independent lifecycle review"
        )
    run_directory = run_directory.resolve()
    plan, plan_bytes = _read_canonical_object(
        _safe_child(run_directory, EXECUTION_PLAN), "execution plan"
    )
    if plan.get("plan_sha256") != _execution_plan_digest(plan):
        raise ProducerError("execution plan semantic digest changed")
    if plan.get("run_id") != plan.get("bindings", {}).get("run_id"):
        raise ProducerError("execution plan run ID differs from its command binding")
    archived_authorization, archived_authorization_bytes = _read_canonical_object(
        _safe_child(run_directory, APPROVED_AUTHORIZATION), "archived authorization"
    )
    supplied_authorization, supplied_authorization_bytes = _read_canonical_object(
        authorization_path.resolve(), "supplied authorization"
    )
    if supplied_authorization != archived_authorization or supplied_authorization_bytes != archived_authorization_bytes:
        raise ProducerError("supplied authorization differs from finalized archived approval")
    if archived_authorization.get("execution_plan_sha256") != plan["plan_sha256"]:
        raise ProducerError("authorization does not bind the final execution plan")
    finalization, _ = _read_canonical_object(
        _safe_child(run_directory, "runtime/finalization-receipt.json"),
        "finalization receipt",
    )
    if (
        finalization.get("state") != "FINALIZED_EXECUTION_EXPLICITLY_REQUIRED"
        or finalization.get("authorization_sha256") != _sha256(archived_authorization_bytes)
        or finalization.get("execution_plan_sha256") != plan["plan_sha256"]
    ):
        raise ProducerError("finalization receipt is not bound to this execution")
    approved_preflight, approved_preflight_bytes = _read_canonical_object(
        _safe_child(run_directory, APPROVED_PREFLIGHT), "approved preflight"
    )
    if (
        approved_preflight.get("approved_plan_authorization_sha256")
        != _sha256(archived_authorization_bytes)
        or approved_preflight.get("approved_plan_request_sha256")
        != archived_authorization.get("request_sha256")
    ):
        raise ProducerError("approved preflight differs from finalized authorization")
    try:
        base_plan = json.loads(
            _safe_child(run_directory, "local-launch-plan.json").read_text(encoding="utf-8")
        )
        manager, replicas = adapter._verify_executable_local_plan(
            run_directory, base_plan
        )
        expected_manager, expected_replicas, expected_bindings = _final_commands(
            run_directory,
            base_plan,
            manager,
            replicas,
            hard_timeout_seconds=plan["hard_timeout_seconds"],
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError, adapter.AdapterError) as exc:
        raise ProducerError(f"cannot revalidate executable plan: {exc}") from exc
    if (
        list(expected_manager) != plan.get("manager_command")
        or [list(command) for command in expected_replicas] != plan.get("replica_commands")
        or expected_bindings != plan.get("bindings")
        or list(_declared_ports(run_directory, base_plan, expected_manager))
        != plan.get("declared_ports")
    ):
        raise ProducerError("final execution commands no longer recompute")
    final_launch = _safe_child(run_directory, plan["final_launch_arguments"])
    if base.sha256_file(final_launch) != plan["final_launch_arguments_sha256"]:
        raise ProducerError("final launch manifest hash changed")
    executables = plan.get("executables")
    if not isinstance(executables, Mapping) or set(executables) != {
        "hotstuff_app", "adaptation_manager"
    }:
        raise ProducerError("execution plan executable bindings have schema drift")
    for label, expected_path in (
        ("hotstuff_app", Path(expected_replicas[0][0]).resolve()),
        ("adaptation_manager", Path(expected_manager[0]).resolve()),
    ):
        binding = executables[label]
        if (
            not isinstance(binding, Mapping)
            or set(binding) != {"path", "sha256"}
            or Path(binding["path"]).resolve() != expected_path
            or base.sha256_file(expected_path) != binding["sha256"]
        ):
            raise ProducerError(f"{label} executable changed after authorization")
    for prospective in (FAULT_WINDOW_ARM, OMISSION_GATE):
        path = _safe_child(run_directory, prospective)
        if path.exists() or path.is_symlink():
            raise ProducerError(f"one-shot runtime artifact already exists: {path.name}")

    run_id = plan["bindings"]["run_id"]
    hard_timeout = plan["hard_timeout_seconds"]
    declared_ports = tuple(plan["declared_ports"])
    occupied_before_launch = base.ports_in_use(declared_ports)
    if occupied_before_launch:
        raise ProducerError(f"declared local ports are already occupied: {occupied_before_launch}")
    deadline = time.monotonic() + hard_timeout
    records: list[Any] = []
    cleanup_written = False
    accepted: dict[str, Any] | None = None
    arm_event: dict[str, Any] | None = None
    arm_line_sha256: str | None = None
    injection_event: dict[str, Any] | None = None
    injection_line_sha256: str | None = None
    previous_handlers: dict[int, Any] = {}
    interrupt_state = {"cleanup_in_progress": False, "signals_seen": 0}
    try:
        transition_request = json.loads(
            _one_option(expected_manager, "--transition-request")
        )
        transition_requests = base._profile_transition_requests(
            {"transition_requests": [transition_request]}
        )
    except (json.JSONDecodeError, base.RunnerError) as exc:
        raise ProducerError("manager transition request is not canonical") from exc

    def allow_clean_manager_exit(_record) -> bool:
        try:
            events = base._event_streams(run_directory)["adaptive-manager"]
            return base.manager_convergence_ready_event(
                events, transition_requests, required_completed=1
            ) is not None
        except (OSError, ValueError, base.RunnerError):
            return False

    def interrupt_handler(signum, _frame):
        interrupt_state["signals_seen"] += 1
        if interrupt_state["cleanup_in_progress"]:
            return
        raise ProducerError(f"execution interrupted by signal {signum}")

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupt_handler)
        except (ValueError, OSError) as exc:
            for installed, previous in previous_handlers.items():
                signal.signal(installed, previous)
            raise ProducerError("cannot install cleanup-preserving signal handlers") from exc

    def restore_handlers() -> None:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)

    try:
        records.append(
            spawn(
                "adaptive-manager",
                expected_manager,
                run_directory / "logs/adaptive-manager.log",
                run_directory,
                replica_id=None,
            )
        )
        for replica_id, command in enumerate(expected_replicas):
            records.append(
                spawn(
                    f"replica-{replica_id}",
                    command,
                    run_directory / f"logs/replica-{replica_id}.log",
                    run_directory,
                    replica_id=replica_id,
                )
            )

        def ready_probe():
            streams = base._event_streams(run_directory)
            return True if all(
                any(event.get("event_type") == "process.ready" for event in streams[source])
                for source in ("adaptive-manager", *(f"replica-{replica}" for replica in range(7)))
            ) else None

        _wait_for(
            records, deadline, ready_probe, "all eight process.ready events",
            allow_clean_manager_exit=allow_clean_manager_exit,
        )

        def baseline_probe():
            streams = base._event_streams(run_directory)
            try:
                return validator._common_e0_commit_before_arm(
                    {source: streams[source] for source in streams if source.startswith("replica-")},
                    epoch_digest=plan["bindings"]["epoch_digest"],
                    arm_start_ns=_raw_clock_ns() + 1,
                )
            except validator.ValidationError:
                return None

        _wait_for(
            records, deadline, baseline_probe, "all-seven pre-arm E0 common commit",
            allow_clean_manager_exit=allow_clean_manager_exit,
        )
        evidence_start_ns = _raw_clock_ns()
        arm = build_fault_window_arm(
            plan,
            authorization_sha256=_sha256(archived_authorization_bytes),
            evidence_start_monotonic_ns=evidence_start_ns,
        )
        arm_bytes = _canonical(arm)
        arm_path = _safe_child(run_directory, FAULT_WINDOW_ARM)
        _publish_atomic_once(arm_path, arm_bytes)
        arm_sha256 = _sha256(arm_bytes)

        manager_path = _safe_child(run_directory, MANAGER_EVENTS)

        def arm_probe():
            try:
                return _one_event_line(
                    manager_path,
                    "fault_window_armed",
                    payload_sha_field="fault_window_arm_sha256",
                    payload_sha256=arm_sha256,
                )
            except ProducerError:
                return None

        arm_event, arm_line_sha256 = _wait_for(
            records, deadline, arm_probe, "source-bound manager fault-window arm",
            allow_clean_manager_exit=allow_clean_manager_exit,
        )
        while True:
            activation_ns = _raw_clock_ns()
            if activation_ns > arm_event["source_monotonic_ns"]:
                break
        gate = build_omission_gate(
            plan,
            manager_source_sequence=arm_event["source_sequence"],
            manager_event_line_sha256=arm_line_sha256,
            activation_monotonic_ns=activation_ns,
        )
        gate_path = _safe_child(run_directory, OMISSION_GATE)
        _publish_atomic_once(gate_path, gate_bytes(gate))

        replica_one_path = _safe_child(run_directory, "raw/replica-1.jsonl")

        def injection_probe():
            try:
                return _one_event_line(
                    replica_one_path,
                    "fault.injection_armed",
                    payload_sha_field="gate_sha256",
                    payload_sha256=base.sha256_file(gate_path),
                )
            except ProducerError:
                return None

        injection_event, injection_line_sha256 = _wait_for(
            records, deadline, injection_probe, "source-bound replica-1 omission arm",
            allow_clean_manager_exit=allow_clean_manager_exit,
        )

        def acceptance_probe():
            streams = base._event_streams(run_directory)
            replicas_only = {
                f"replica-{replica}": streams[f"replica-{replica}"]
                for replica in range(7)
            }
            try:
                return validator.validate_known_raw_events(
                    approved_preflight,
                    streams["adaptive-manager"],
                    replicas_only,
                    arm_event,
                    run_id=run_id,
                )
            except validator.ValidationError:
                return None

        accepted = _wait_for(
            records, deadline, acceptance_probe,
            "six omissions, signed E1 activation, and post-E1 common commit",
            allow_clean_manager_exit=allow_clean_manager_exit,
        )
        cleanup_receipt = _cleanup_receipt(
            run_directory, run_id, records, declared_ports
        )
        cleanup_written = True
        if cleanup_receipt["complete"] is not True:
            raise ProducerError("cleanup left a live process group or declared listener")

        manager_bundle = Path(_one_option(expected_manager, "--bundle-output"))
        artifacts = {
            "preflight": _descriptor(run_directory, _safe_child(run_directory, APPROVED_PREFLIGHT)),
            "epoch0_tree": _descriptor(
                run_directory,
                _safe_child(run_directory, base_plan["e0_identity"]["tree_file"]),
            ),
            "execution_plan": _descriptor(run_directory, _safe_child(run_directory, EXECUTION_PLAN)),
            "authorization_request": _descriptor(
                run_directory, _safe_child(run_directory, AUTHORIZATION_REQUEST)
            ),
            "plan_authorization": _descriptor(run_directory, _safe_child(run_directory, APPROVED_AUTHORIZATION)),
            "fault_window_arm": _descriptor(run_directory, arm_path),
            "omission_gate": _descriptor(run_directory, gate_path),
            "manager_events": _descriptor(run_directory, manager_path),
            "replica_streams": {
                f"replica-{replica}": _descriptor(
                    run_directory, _safe_child(run_directory, f"raw/replica-{replica}.jsonl")
                )
                for replica in range(7)
            },
            "e1_bundle": _descriptor(run_directory, manager_bundle),
            "issuer_public_key": _descriptor(
                run_directory, _safe_child(run_directory, plan["issuer_public_key"])
            ),
            "cleanup": _descriptor(
                run_directory, _safe_child(run_directory, "runtime/cleanup-receipt.json")
            ),
        }
        raw_receipt = {
            "schema_version": 1,
            "scenario": PROFILE_ID,
            "run_id": run_id,
            "artifacts": artifacts,
            "fault_window_arm": {
                "source_sequence": arm_event["source_sequence"],
                "line_sha256": arm_line_sha256,
                "clock_domain": "host-raw",
            },
            "fault_injection_arm": {
                "source_sequence": injection_event["source_sequence"],
                "line_sha256": injection_line_sha256,
                "clock_domain": "host-raw",
            },
        }
        raw_receipt_path = _safe_child(run_directory, "raw-bundle-receipt.json")
        _write_exclusive(raw_receipt_path, _canonical(raw_receipt))
        verdict = validator.validate_raw_bundle(run_directory, raw_receipt)
        _write_exclusive(
            _safe_child(run_directory, "raw-bundle-verdict.json"), _canonical(verdict)
        )
        restore_handlers()
        return {
            "status": "RAW_BUNDLE_VALIDATED",
            "run_id": run_id,
            "receipt": str(raw_receipt_path.relative_to(run_directory)),
            "verdict": "raw-bundle-verdict.json",
            "e1_successor_epoch_digest": verdict["e1_successor_epoch_digest"],
            "claim_boundary": verdict["claim_boundary"],
        }
    except BaseException as exc:
        interrupt_state["cleanup_in_progress"] = True
        cleanup_error: BaseException | None = None
        try:
            if records and not cleanup_written:
                try:
                    cleanup_receipt = _cleanup_receipt(
                        run_directory, run_id, records, declared_ports
                    )
                    cleanup_written = cleanup_receipt["complete"] is True
                except BaseException as cleanup_exc:  # preserve both failures
                    cleanup_error = cleanup_exc
        finally:
            restore_handlers()
        abort = {
            "schema_version": 1,
            "scenario": PROFILE_ID,
            "run_id": run_id,
            "status": "ABORTED",
            "reason": str(exc)[:512],
            "cleanup_complete": cleanup_written,
            "cleanup_error": None if cleanup_error is None else str(cleanup_error)[:512],
            "accepted_partial": accepted,
            "claim_boundary": "no retry; aborted run is ineligible for figures or thesis claims",
        }
        abort_path = _safe_child(run_directory, "local-run-abort.json")
        if not abort_path.exists():
            _write_exclusive(abort_path, _canonical(abort))
        raise ProducerError(f"local no-retry execution aborted: {exc}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)
    inputs_parser = subparsers.add_parser("prepare-inputs")
    inputs_parser.add_argument("--run-root", type=Path, required=True)
    inputs_parser.add_argument(
        "--app-binary", type=Path,
        default=adapter.KAURI / "build-adaptive/examples/hotstuff-app",
    )
    inputs_parser.add_argument(
        "--manager-binary", type=Path,
        default=adapter.KAURI / "build-adaptive/examples/adaptation-manager",
    )
    inputs_parser.add_argument(
        "--keygen-binary", type=Path,
        default=adapter.KAURI / "build-adaptive/hotstuff-keygen",
    )
    inputs_parser.add_argument(
        "--tls-keygen-binary", type=Path,
        default=adapter.KAURI / "build-adaptive/hotstuff-tls-keygen",
    )
    inputs_parser.add_argument(
        "--e0-helper-binary", type=Path,
        default=adapter.KAURI / "build-adaptive/examples/n7-epoch0-treefile-digest",
    )
    inputs_parser.add_argument("--peer-port", type=int, default=25100)
    inputs_parser.add_argument("--client-port", type=int, default=26100)
    inputs_parser.add_argument("--manager-port", type=int, default=27100)
    inputs_parser.add_argument("--hard-timeout-seconds", type=int, default=300)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--run-root", type=Path, required=True)
    prepare_parser.add_argument("--hard-timeout-seconds", type=int, default=300)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--run-root", type=Path, required=True)
    finalize_parser.add_argument("--authorization", type=Path, required=True)
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--run-root", type=Path, required=True)
    execute_parser.add_argument("--authorization", type=Path, required=True)
    execute_parser.add_argument("--enable-reviewed-execution", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.phase == "prepare-inputs":
            result = prepare_inputs(
                args.run_root,
                app_binary=args.app_binary,
                manager_binary=args.manager_binary,
                keygen_binary=args.keygen_binary,
                tls_keygen_binary=args.tls_keygen_binary,
                e0_helper_binary=args.e0_helper_binary,
                peer_port=args.peer_port,
                client_port=args.client_port,
                manager_port=args.manager_port,
                hard_timeout_seconds=args.hard_timeout_seconds,
            )
        elif args.phase == "prepare":
            result = prepare(
                args.run_root, hard_timeout_seconds=args.hard_timeout_seconds
            )
        elif args.phase == "finalize":
            result = finalize(args.run_root, args.authorization)
        else:
            result = execute(
                args.run_root,
                args.authorization,
                execution_enabled=args.enable_reviewed_execution,
            )
    except (OSError, KeyError, IndexError, TypeError, ProducerError, adapter.AdapterError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
