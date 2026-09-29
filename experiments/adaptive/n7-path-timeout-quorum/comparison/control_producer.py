#!/usr/bin/env python3
"""No-launch command contract for the N=7 fixed-E0 matched control.

This is intentionally not an execution entrypoint.  It derives a control
command from the same prepared N=7 inputs as the adaptive arm, removes every
successor request/output, and requires the native manager's explicit future
``--fault-window-arm-control-only`` mode.  Until that native mode exists, the
only valid outcome is a prepared contract; this module cannot start a process.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
STUDY = HERE.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adaptive = _load("n7_adaptive_arm_for_control_contract", STUDY / "run_local.py")
control_validator = _load("n7_fixed_e0_control_validator", HERE / "control_validator.py")
comparison_validator = _load("n7_matched_control_manifest", HERE / "validator.py")

CONTROL_PLAN = Path("runtime/fixed-e0-control-plan.json")
CONTROL_AUTHORIZATION_REQUEST = Path("runtime/fixed-e0-control-authorization-request.json")
CONTROL_APPROVED_AUTHORIZATION = Path("runtime/fixed-e0-control-approved-authorization.json")
CONTROL_FINALIZATION_RECEIPT = Path("runtime/fixed-e0-control-finalization-receipt.json")
CONTROL_MANIFEST = Path("runtime/fixed-e0-control-matched-manifest.json")
CONTROL_MODE_OPTION = "--fault-window-arm-control-only"
PLAN_KIND = "kauri-n7-fixed-e0-control-plan-v2"
REQUEST_KIND = "kauri-n7-fixed-e0-control-authorization-request-v2"
AUTHORIZATION_KIND = "kauri-n7-fixed-e0-control-authorization-v2"
FINALIZATION_KIND = "kauri-n7-fixed-e0-control-finalization-receipt-v2"
ALLOWED_OMISSION_CONTEXTS = frozenset({(4, 4), (5, 5), (6, 6)})


class ProducerError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
    except (TypeError, ValueError) as exc:
        raise ProducerError("control document is not canonical JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_child(root: Path, relative: Path | str) -> Path:
    candidate = (root.resolve() / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ProducerError("control artifact escapes run root") from exc
    return candidate


def _write_exclusive(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise ProducerError("refusing to replace control artifact")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as output:
            fd = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _require_clean_control_cleanup(receipt: object) -> None:
    """Reject a completion marker that hides any failed control process."""
    if not isinstance(receipt, Mapping) or receipt.get("complete") is not True:
        raise ProducerError("control cleanup incomplete")
    processes = receipt.get("processes")
    if not isinstance(processes, list):
        raise ProducerError("control cleanup lacks process outcomes")
    expected_sources = {"adaptive-manager", *(f"replica-{item}" for item in range(7))}
    by_source = {
        item.get("source_id"): item for item in processes
        if isinstance(item, Mapping) and isinstance(item.get("source_id"), str)
    }
    if len(processes) != 8 or set(by_source) != expected_sources:
        raise ProducerError("control cleanup lacks exact process outcomes")
    if any(item.get("returncode") != 0 or item.get("termination") != "clean-exit"
           for item in by_source.values()):
        raise ProducerError("control manager did not exit cleanly")


def _strip_exact_option_pairs(command: Sequence[str]) -> tuple[str, ...]:
    """Remove the only two successor-authorizing option families."""
    result: list[str] = []
    cursor = 0
    removed = {"--transition-request": 0, "--bundle-output": 0}
    while cursor < len(command):
        option = command[cursor]
        if option in removed:
            if cursor + 1 >= len(command) or not command[cursor + 1]:
                raise ProducerError("adaptive manager command has malformed successor option")
            removed[option] += 1
            cursor += 2
            continue
        result.append(option)
        cursor += 1
    if any(count != 1 for count in removed.values()):
        raise ProducerError("adaptive manager command lacks one exact successor pair")
    if CONTROL_MODE_OPTION in result:
        raise ProducerError("prepared manager command already contains control-only mode")
    return tuple((*result, CONTROL_MODE_OPTION))


def _semantic_digest(plan: Mapping[str, Any], digest_field: str = "prepared_plan_sha256") -> str:
    return _sha256(_canonical({key: value for key, value in plan.items() if key != digest_field})[:-1])


def _descriptor(path: Path, *, root: Path | None = None) -> dict[str, str]:
    """Return an exact artifact identity, retaining a root-relative path when possible."""
    resolved = path.resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise ProducerError("control artifact is not a regular file")
    rendered = str(resolved)
    if root is not None:
        try:
            rendered = str(resolved.relative_to(root.resolve()))
        except ValueError:
            pass
    return {"path": rendered, "sha256": _sha256(resolved.read_bytes())}


def _launch_contract(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The immutable execution-relevant subset which approval binds."""
    return {
        "run_id": plan["run_id"],
        "epoch0": plan["epoch0"],
        "hard_timeout_seconds": plan["hard_timeout_seconds"],
        "declared_ports": plan["declared_ports"],
        "bindings": plan["bindings"],
        "manager_command": plan["manager_command"],
        "replica_commands": plan["replica_commands"],
        "executables": plan["executables"],
        "required_native_mode": plan["required_native_mode"],
        "no_successor_guards": plan["no_successor_guards"],
    }


def prepare_no_successor_control(run_directory: Path, *, hard_timeout_seconds: int = 600) -> dict[str, Any]:
    """Write exactly one no-launch control command contract from frozen inputs."""
    if type(hard_timeout_seconds) is not int or hard_timeout_seconds != 600:
        raise ProducerError("control hard timeout must match the frozen 600-second manifest")
    root = run_directory.resolve()
    base_path = _safe_child(root, "local-launch-plan.json")
    if base_path.is_symlink() or not base_path.is_file():
        raise ProducerError("control requires the adaptive arm's prepared base inputs")
    try:
        base_plan = json.loads(base_path.read_text(encoding="utf-8"))
        manager, replicas = adaptive.adapter._verify_executable_local_plan(root, base_plan)
        final_manager, final_replicas, bindings = adaptive._final_commands(
            root, base_plan, manager, replicas, hard_timeout_seconds=hard_timeout_seconds
        )
    except (OSError, json.JSONDecodeError, adaptive.ProducerError, adaptive.adapter.AdapterError) as exc:
        raise ProducerError(f"cannot derive fixed-E0 command contract: {exc}") from exc
    control_manager = _strip_exact_option_pairs(final_manager)
    if "--transition-request" in control_manager or "--bundle-output" in control_manager:
        raise ProducerError("fixed-E0 control command still contains a successor authorization")
    declared_ports = adaptive._declared_ports(root, base_plan, control_manager)
    app_binary = Path(final_replicas[0][0]).resolve()
    manager_binary = Path(final_manager[0]).resolve()
    for binary, label in ((app_binary, "hotstuff-app"), (manager_binary, "adaptation-manager")):
        try:
            adaptive.base._assert_executable(binary, label)
        except adaptive.base.RunnerError as exc:
            raise ProducerError(str(exc)) from exc
    plan = {
        "schema_version": 2,
        "kind": PLAN_KIND,
        "state": "PREPARED_EXTERNAL_APPROVAL_REQUIRED",
        "claim_boundary": "No process launched. The native control-only manager mode is required before execution or a result.",
        "base_plan": _descriptor(base_path, root=root),
        "base_plan_sha256": base_plan.get("plan_sha256"),
        "run_id": bindings["run_id"],
        "epoch0": {
            "epoch_number": 0,
            "epoch_digest": bindings["epoch_digest"],
            "tree_file_sha256": bindings["tree_file_sha256"],
        },
        "hard_timeout_seconds": hard_timeout_seconds,
        "declared_ports": list(declared_ports),
        "bindings": bindings,
        "fault_window_arm": str(adaptive.FAULT_WINDOW_ARM),
        "omission_gate": str(adaptive.OMISSION_GATE),
        "manager_events": str(adaptive.MANAGER_EVENTS),
        "manager_command": list(control_manager),
        "replica_commands": [list(command) for command in final_replicas],
        "executables": {
            "hotstuff_app": _descriptor(app_binary),
            "adaptation_manager": _descriptor(manager_binary),
        },
        "required_native_mode": CONTROL_MODE_OPTION,
        "no_successor_guards": {
            "transition_request_absent": True,
            "bundle_output_absent": True,
            "control_only_mode_required": True,
        },
    }
    if not isinstance(plan["base_plan_sha256"], str):
        raise ProducerError("prepared base plan lacks its identity digest")
    plan["launch_contract_sha256"] = _sha256(_canonical(_launch_contract(plan)))
    plan["prepared_plan_sha256"] = _semantic_digest(plan)
    target = _safe_child(root, CONTROL_PLAN)
    _write_exclusive(target, _canonical(plan))
    request = {
        "schema_version": 2,
        "kind": REQUEST_KIND,
        "prepared_plan_sha256": plan["prepared_plan_sha256"],
        "base_plan_sha256": plan["base_plan_sha256"],
        "run_id": plan["run_id"],
        "epoch0": plan["epoch0"],
        "launch_contract_sha256": plan["launch_contract_sha256"],
        "manager_command_sha256": _sha256(_canonical(plan["manager_command"])),
        "replica_1_launch_argv_sha256": bindings["replica_1_launch_argv_sha256"],
        "hard_timeout_seconds": hard_timeout_seconds,
        "declared_ports": list(declared_ports),
        "no_retry": True,
        "claim_boundary": "Approval inputs only; no process launched.",
    }
    _write_exclusive(
        _safe_child(root, CONTROL_AUTHORIZATION_REQUEST), _canonical(request)
    )
    return {
        "state": plan["state"],
        "prepared_plan_sha256": plan["prepared_plan_sha256"],
        "artifact": str(CONTROL_PLAN),
        "authorization_request": str(CONTROL_AUTHORIZATION_REQUEST),
        "authorization_request_sha256": _sha256(_canonical(request)),
        "claim_boundary": plan["claim_boundary"],
    }


def _read_canonical_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise ProducerError(f"{label} is not a regular file")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProducerError(f"{label} is not JSON") from exc
    if not isinstance(value, dict) or raw != _canonical(value):
        raise ProducerError(f"{label} is not canonical JSON")
    return value, raw


def _rederive_prepared_control(root: Path, plan: Mapping[str, Any]) -> None:
    """Recompute every launch-relevant field from the immutable base plan."""
    base_path = _safe_child(root, "local-launch-plan.json")
    base_plan, _ = _read_canonical_object(base_path, "prepared base plan")
    if (plan.get("base_plan") != _descriptor(base_path, root=root) or
            plan.get("base_plan_sha256") != base_plan.get("plan_sha256") or
            base_plan.get("plan_sha256") != adaptive.adapter._plan_digest(base_plan)):
        raise ProducerError("base plan differs from approved control plan")
    try:
        base_manager, base_replicas = adaptive.adapter._verify_executable_local_plan(root, base_plan)
        manager, replicas, bindings = adaptive._final_commands(
            root, base_plan, base_manager, base_replicas,
            hard_timeout_seconds=plan["hard_timeout_seconds"],
        )
    except (KeyError, TypeError, adaptive.ProducerError, adaptive.adapter.AdapterError) as exc:
        raise ProducerError(f"cannot rederive approved control argv: {exc}") from exc
    manager = _strip_exact_option_pairs(manager)
    app_binary, manager_binary = Path(replicas[0][0]).resolve(), Path(manager[0]).resolve()
    expected_executables = {
        "hotstuff_app": _descriptor(app_binary),
        "adaptation_manager": _descriptor(manager_binary),
    }
    if (plan.get("manager_command") != list(manager) or
            plan.get("replica_commands") != [list(command) for command in replicas] or
            plan.get("bindings") != bindings or
            plan.get("executables") != expected_executables or
            plan.get("declared_ports") != list(adaptive._declared_ports(root, base_plan, manager)) or
            plan.get("run_id") != bindings.get("run_id") or
            plan.get("epoch0") != {
                "epoch_number": 0,
                "epoch_digest": bindings.get("epoch_digest"),
                "tree_file_sha256": bindings.get("tree_file_sha256"),
            }):
        raise ProducerError("prepared control fields are not rederived from immutable base inputs")


def finalize_no_successor_control(run_directory: Path, authorization_path: Path) -> dict[str, Any]:
    """Bind an external no-retry authorization to the exact control plan.

    This is deliberately separate from execution.  It writes only immutable
    inputs and cannot spawn a manager or replica.
    """
    root = run_directory.resolve()
    plan, plan_bytes = _read_canonical_object(_safe_child(root, CONTROL_PLAN), "control plan")
    request, request_bytes = _read_canonical_object(
        _safe_child(root, CONTROL_AUTHORIZATION_REQUEST), "control authorization request"
    )
    authorization, authorization_bytes = _read_canonical_object(
        authorization_path.resolve(), "external control authorization"
    )
    if (plan.get("schema_version") != 2 or plan.get("kind") != PLAN_KIND or
            plan.get("state") != "PREPARED_EXTERNAL_APPROVAL_REQUIRED" or
            plan.get("prepared_plan_sha256") != _semantic_digest(plan) or
            plan.get("launch_contract_sha256") != _sha256(_canonical(_launch_contract(plan)))):
        raise ProducerError("control plan digest drifted")
    _rederive_prepared_control(root, plan)
    expected_request = {
        "schema_version": 2,
        "kind": REQUEST_KIND,
        "prepared_plan_sha256": plan["prepared_plan_sha256"],
        "base_plan_sha256": plan["base_plan_sha256"], "run_id": plan["run_id"],
        "epoch0": plan["epoch0"],
        "launch_contract_sha256": plan["launch_contract_sha256"],
        "manager_command_sha256": _sha256(_canonical(plan["manager_command"])),
        "replica_1_launch_argv_sha256": plan["bindings"]["replica_1_launch_argv_sha256"],
        "hard_timeout_seconds": plan["hard_timeout_seconds"], "no_retry": True,
        "declared_ports": plan["declared_ports"],
        "claim_boundary": "Approval inputs only; no process launched.",
    }
    expected_authorization_fields = {
        "schema_version", "kind", "request_sha256", "prepared_plan_sha256",
        "approval_reference", "approved_utc", "no_retry",
    }
    if (request != expected_request or set(authorization) != expected_authorization_fields or
            authorization.get("schema_version") != 2 or
            authorization.get("kind") != AUTHORIZATION_KIND or
            authorization.get("request_sha256") != _sha256(request_bytes) or
            authorization.get("prepared_plan_sha256") != plan["prepared_plan_sha256"] or
            authorization.get("no_retry") is not True or
            not isinstance(authorization.get("approval_reference"), str) or
            not authorization["approval_reference"].strip() or
            not isinstance(authorization.get("approved_utc"), str) or not authorization["approved_utc"].endswith("Z")):
        raise ProducerError("control authorization is not bound to the exact no-retry request")
    if authorization_path.resolve() == _safe_child(root, CONTROL_APPROVED_AUTHORIZATION):
        raise ProducerError("authorization input must be external to its archive path")
    _write_exclusive(_safe_child(root, CONTROL_APPROVED_AUTHORIZATION), authorization_bytes)
    finalization = {
        "schema_version": 2,
        "kind": FINALIZATION_KIND,
        "state": "FINALIZED_EXECUTION_EXPLICITLY_REQUIRED",
        "prepared_plan_sha256": plan["prepared_plan_sha256"],
        "authorization_request_sha256": _sha256(request_bytes),
        "authorization_sha256": _sha256(authorization_bytes),
        "claim_boundary": "external approval verified; no process launched",
    }
    finalization["finalization_receipt_sha256"] = _semantic_digest(
        finalization, "finalization_receipt_sha256"
    )
    _write_exclusive(_safe_child(root, CONTROL_FINALIZATION_RECEIPT), _canonical(finalization))
    return {
        "state": finalization["state"],
        "finalization_receipt_sha256": finalization["finalization_receipt_sha256"],
        "claim_boundary": "Authorization finalized; execution remains explicit and no claim is promoted.",
    }


def execute_no_successor_control(
    run_directory: Path, authorization_path: Path, *,
    spawn=adaptive.base.spawn_process, execution_enabled: bool = False,
    wait_for=adaptive._wait_for, cleanup=adaptive._cleanup_receipt,
    event_streams=adaptive.base._event_streams,
    port_probe=adaptive.base.ports_in_use, raw_clock=adaptive._raw_clock_ns,
    monotonic=__import__("time").monotonic, sleep=__import__("time").sleep,
) -> dict[str, Any]:
    """Execute one finalized fixed-E0 control, preserving terminal evidence.

    Disabled by default: callers must deliberately opt into process launch.
    The path shares only process/arm/gate/cleanup primitives with the adaptive
    runner; it never calls its E1 validator or successor lifecycle.
    """
    if execution_enabled is not True:
        raise ProducerError("fixed-E0 EXECUTE is disabled pending explicit launch")
    root = run_directory.resolve()
    plan, plan_bytes = _read_canonical_object(_safe_child(root, CONTROL_PLAN), "prepared control plan")
    finalization, finalization_bytes = _read_canonical_object(
        _safe_child(root, CONTROL_FINALIZATION_RECEIPT), "control finalization receipt"
    )
    approval, approval_bytes = _read_canonical_object(_safe_child(root, CONTROL_APPROVED_AUTHORIZATION), "approved control authorization")
    supplied, supplied_bytes = _read_canonical_object(authorization_path.resolve(), "supplied control authorization")
    request, request_bytes = _read_canonical_object(
        _safe_child(root, CONTROL_AUTHORIZATION_REQUEST), "control authorization request"
    )
    if (plan.get("schema_version") != 2 or plan.get("kind") != PLAN_KIND or
            plan.get("state") != "PREPARED_EXTERNAL_APPROVAL_REQUIRED" or
            plan.get("prepared_plan_sha256") != _semantic_digest(plan) or
            plan.get("launch_contract_sha256") != _sha256(_canonical(_launch_contract(plan))) or
            finalization.get("schema_version") != 2 or
            finalization.get("kind") != FINALIZATION_KIND or
            finalization.get("state") != "FINALIZED_EXECUTION_EXPLICITLY_REQUIRED" or
            finalization.get("finalization_receipt_sha256") != _semantic_digest(finalization, "finalization_receipt_sha256") or
            finalization.get("prepared_plan_sha256") != plan.get("prepared_plan_sha256") or
            finalization.get("authorization_request_sha256") != _sha256(request_bytes) or
            finalization.get("authorization_sha256") != _sha256(approval_bytes) or
            approval.get("request_sha256") != _sha256(request_bytes) or
            approval.get("prepared_plan_sha256") != plan.get("prepared_plan_sha256")):
        raise ProducerError("finalized control plan or authorization binding drifted")
    if supplied != approval or supplied_bytes != approval_bytes:
        raise ProducerError("control execution approval/final plan mismatch")
    _rederive_prepared_control(root, plan)
    manager = tuple(plan.get("manager_command", ()))
    replicas = tuple(tuple(item) for item in plan.get("replica_commands", ()))
    if (len(manager) < 2 or manager[-1] != CONTROL_MODE_OPTION or len(replicas) != 7 or
            "--transition-request" in manager or "--bundle-output" in manager):
        raise ProducerError("finalized control argv is not fixed-E0")
    ports = tuple(plan.get("declared_ports", ()))
    if (len(ports) != 15 or len(set(ports)) != 15 or
            any(type(port) is not int or port <= 1024 or port > 65535 for port in ports)):
        raise ProducerError("finalized control listener set is invalid")
    occupied = port_probe(ports)
    if occupied:
        raise ProducerError(f"control listeners are already occupied: {occupied}")
    for relative in (adaptive.FAULT_WINDOW_ARM, adaptive.OMISSION_GATE):
        if _safe_child(root, relative).exists():
            raise ProducerError("one-shot control arm/gate already exists")
    records: list[Any] = []
    cleanup_written = False
    try:
        records.append(spawn("adaptive-manager", manager, root / "logs/adaptive-manager.log", root, replica_id=None))
        for replica_id, command in enumerate(replicas):
            records.append(spawn(f"replica-{replica_id}", command, root / f"logs/replica-{replica_id}.log", root, replica_id=replica_id))
        deadline = monotonic() + plan["hard_timeout_seconds"]
        def ready():
            streams = event_streams(root)
            return True if all(any(item.get("event_type") == "process.ready" for item in streams[source]) for source in ("adaptive-manager", *(f"replica-{i}" for i in range(7)))) else None
        wait_for(records, deadline, ready, "all eight control process.ready events", allow_clean_manager_exit=lambda _: False)
        def baseline():
            streams = event_streams(root)
            try:
                return adaptive.validator._common_e0_commit_before_arm({key: value for key, value in streams.items() if key.startswith("replica-")}, epoch_digest=plan["epoch0"]["epoch_digest"], arm_start_ns=raw_clock() + 1)
            except adaptive.validator.ValidationError: return None
        wait_for(records, deadline, baseline, "all-seven pre-arm E0 common commit", allow_clean_manager_exit=lambda _: False)
        arm = adaptive.build_fault_window_arm({"bindings": {**plan["bindings"], "transition_request_sha256": plan["bindings"]["transition_request_sha256"]}}, authorization_sha256=_sha256(approval_bytes), evidence_start_monotonic_ns=raw_clock())
        arm_path = _safe_child(root, adaptive.FAULT_WINDOW_ARM)
        adaptive._publish_atomic_once(arm_path, adaptive._canonical(arm))
        arm_sha = _sha256(adaptive._canonical(arm))
        manager_path = _safe_child(root, adaptive.MANAGER_EVENTS)
        arm_event, arm_line = wait_for(records, deadline, lambda: _control_event(manager_path, "fault_window_armed", "fault_window_arm_sha256", arm_sha), "source-bound control fault-window arm", allow_clean_manager_exit=lambda _: False)
        gate = adaptive.build_omission_gate({"bindings": plan["bindings"]}, manager_source_sequence=arm_event["source_sequence"], manager_event_line_sha256=arm_line, activation_monotonic_ns=raw_clock())
        gate_path = _safe_child(root, adaptive.OMISSION_GATE)
        adaptive._publish_atomic_once(gate_path, adaptive.gate_bytes(gate))
        wait_for(records, deadline, lambda: _control_event(_safe_child(root, "raw/replica-1.jsonl"), "fault.injection_armed", "gate_sha256", adaptive.base.sha256_file(gate_path)), "source-bound control omission gate", allow_clean_manager_exit=lambda _: False)
        gate_sha = adaptive.base.sha256_file(gate_path)
        def first_physical_omission():
            try:
                return first_source_bound_physical_omission(event_streams(root), gate_sha)
            except (KeyError, TypeError):
                return None
        omission = wait_for(records, deadline, first_physical_omission,
                            "source-bound physical aggregate omission",
                            allow_clean_manager_exit=lambda _: False)
        omission_payload = omission.get("payload")
        if (not isinstance(omission_payload, Mapping) or
                (omission_payload.get("tree_id"), omission_payload.get("parent_replica"))
                not in ALLOWED_OMISSION_CONTEXTS):
            raise ProducerError("control first physical omission has an unadmitted tree context")
        horizon_deadline = omission["source_monotonic_ns"] + 60_000_000_000
        while raw_clock() < horizon_deadline:
            if monotonic() >= deadline: raise ProducerError("control fixed horizon exceeded hard timeout")
            sleep(0.05)
        coverage = event_streams(root)
        if not streams_cover_horizon(coverage, horizon_deadline):
            raise ProducerError("control raw streams do not cover the fixed omission horizon")
        cleanup_receipt = cleanup(root, plan["run_id"], records, ports)
        _require_clean_control_cleanup(cleanup_receipt)
        cleanup_written = True
        manifest = comparison_validator.load_manifest(); comparison_validator.validate_manifest(manifest)
        manifest_path = _safe_child(root, CONTROL_MANIFEST)
        _write_exclusive(manifest_path, comparison_validator.MANIFEST_PATH.read_bytes())
        contract = {"schema_version": 2, "state": "FROZEN_EXECUTION_NO_SUCCESSOR", "run_id": plan["run_id"], "manifest_sha256": manifest["manifest_sha256"], "epoch0": plan["epoch0"], "fault_window_arm": {"source_sequence": arm_event["source_sequence"], "event_sha256": arm_line}, "omission_gate_sha256": gate_sha, "omission_context": {"tree_id": omission_payload["tree_id"], "parent_replica": omission_payload["parent_replica"], "expected_message_type": "aggregate_relay"}, "designated_observer": 0, "horizon_ns": 60_000_000_000}
        artifacts = {"manager_events": adaptive._descriptor(root, manager_path), "replica_streams": {f"replica-{i}": adaptive._descriptor(root, _safe_child(root, f"raw/replica-{i}.jsonl")) for i in range(7)}, "fault_window_arm": adaptive._descriptor(root, arm_path), "omission_gate": adaptive._descriptor(root, gate_path), "cleanup": adaptive._descriptor(root, _safe_child(root, "runtime/cleanup-receipt.json")), "manifest": _descriptor(manifest_path, root=root), "prepared_plan": _descriptor(_safe_child(root, CONTROL_PLAN), root=root), "authorization_request": _descriptor(_safe_child(root, CONTROL_AUTHORIZATION_REQUEST), root=root), "approved_authorization": _descriptor(_safe_child(root, CONTROL_APPROVED_AUTHORIZATION), root=root), "finalization_receipt": _descriptor(_safe_child(root, CONTROL_FINALIZATION_RECEIPT), root=root), "base_plan": _descriptor(_safe_child(root, "local-launch-plan.json"), root=root), "executables": plan["executables"]}
        receipt = {"schema_version": 2, "kind": "kauri-n7-fixed-e0-control-raw-bundle-v2", "contract": contract, "artifacts": artifacts}
        adaptive._write_exclusive(_safe_child(root, "fixed-e0-control-raw-bundle-receipt.json"), _canonical(receipt))
        verdict = control_validator.validate_raw_bundle(root, receipt)
        adaptive._write_exclusive(_safe_child(root, "fixed-e0-control-raw-bundle-verdict.json"), _canonical(verdict))
        return {"status": verdict["verdict"], "claim_boundary": verdict["claim_boundary"]}
    except BaseException as exc:
        if records and not cleanup_written:
            try: cleanup(root, plan["run_id"], records, ports)
            except BaseException: pass
        abort = {"schema_version": 1, "run_id": plan.get("run_id"), "status": "ABORTED", "reason": str(exc)[:512], "claim_boundary": "No retry; aborted control is ineligible for figures and claims."}
        _write_exclusive(_safe_child(root, "fixed-e0-control-abort.json"), _canonical(abort))
        raise ProducerError(abort["reason"]) from exc


def _control_event(path: Path, event_type: str, payload_key: str, payload_value: str):
    try:
        return adaptive._one_event_line(path, event_type, payload_sha_field=payload_key, payload_sha256=payload_value)
    except adaptive.ProducerError:
        return None


def first_source_bound_physical_omission(streams: Mapping[str, Sequence[Mapping[str, Any]]], gate_sha256: str) -> Mapping[str, Any] | None:
    """Return the first gate-bound physical omission in an admitted T4/T5/T6 path."""
    events = streams.get("replica-1", ())
    candidates = [
        event for event in events
        if event.get("event_type") == "fault.aggregate_omitted"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("gate_sha256") == gate_sha256
        and event["payload"].get("first_for_context") is True
        and (event["payload"].get("tree_id"), event["payload"].get("parent_replica"))
        in ALLOWED_OMISSION_CONTEXTS
    ]
    return min(candidates, key=lambda event: (event["source_monotonic_ns"], event["source_sequence"])) if candidates else None


def streams_cover_horizon(streams: Mapping[str, Sequence[Mapping[str, Any]]], horizon_ns: int) -> bool:
    expected = {"adaptive-manager", *(f"replica-{i}" for i in range(7))}
    return (set(streams) == expected and all(
        events and events[-1].get("source_monotonic_ns", 0) >= horizon_ns
        for events in streams.values()))


def main(argv: Sequence[str] | None = None) -> int:
    """Explicit excluded-shakedown entrypoint; no flag means no launch."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--enable-reviewed-execution", action="store_true")
    args = parser.parse_args(argv)
    try:
        outcome = execute_no_successor_control(
            args.run_root, args.authorization,
            execution_enabled=args.enable_reviewed_execution,
        )
    except ProducerError as exc:
        print(f"fixed-e0-control: {exc}")
        return 2
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
