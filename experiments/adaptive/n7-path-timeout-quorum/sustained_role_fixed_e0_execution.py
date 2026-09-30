#!/usr/bin/env python3
"""Sealed, no-launch handoff for the N=7 sustained-role fixed-E0 arm.

This module deliberately does *not* turn the sustained-role dry-run into an
experiment.  It binds the frozen fixed-E0 argv, external authorization, and
the exact future eight-log/eight-stream raw-bundle layout.  A separate native
verification receipt is required before an audited launcher may exist.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


HERE = Path(__file__).resolve().parent
PLAN = Path("runtime/sustained-role-execution-plan.json")
REQUEST = Path("runtime/sustained-role-authorization-request.json")
EXECUTION_PLAN = Path("runtime/sustained-role-fixed-e0-execution-plan.json")
EXECUTION_REQUEST = Path("runtime/sustained-role-fixed-e0-execution-request.json")
APPROVAL = Path("runtime/sustained-role-fixed-e0-approved-authorization.json")
NATIVE_VERIFICATION = Path("runtime/sustained-role-fixed-e0-native-verification.json")
FINALIZATION = Path("runtime/sustained-role-fixed-e0-finalization.json")
RAW_RECEIPT = Path("sustained-role-fixed-e0-raw-bundle-receipt.json")
ABORT = Path("sustained-role-fixed-e0-abort.json")

EXECUTION_KIND = "kauri-n7-sustained-role-fixed-e0-execution-plan-v1"
REQUEST_KIND = "kauri-n7-sustained-role-fixed-e0-execution-request-v1"
APPROVAL_KIND = "kauri-n7-sustained-role-fixed-e0-authorization-v1"
NATIVE_KIND = "kauri-n7-sustained-role-fixed-e0-native-verification-v1"
FINALIZATION_KIND = "kauri-n7-sustained-role-fixed-e0-finalization-v1"
RAW_KIND = "kauri-n7-sustained-role-fixed-e0-raw-bundle-v1"
_HEX = frozenset("0123456789abcdef")
_SOURCES = ("adaptive-manager", *(f"replica-{item}" for item in range(7)))


class FixedE0ExecutionError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, allow_nan=False, ensure_ascii=True,
                          sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    except (TypeError, ValueError) as exc:
        raise FixedE0ExecutionError("document is not canonical JSON") from exc


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hex64(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in _HEX for ch in value):
        raise FixedE0ExecutionError(f"{label} is not a lower-case SHA-256")
    return value


def _child(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise FixedE0ExecutionError("artifact path escapes the run root")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FixedE0ExecutionError("artifact path escapes the run root") from exc
    return candidate


def _write_once(path: Path, payload: bytes) -> None:
    if path.is_symlink() or path.exists() or not path.parent.is_dir():
        raise FixedE0ExecutionError(f"refusing to replace {path.name}")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _read_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
        raise FixedE0ExecutionError(f"{label} is not a bounded regular file")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixedE0ExecutionError(f"{label} is not JSON") from exc
    if not isinstance(value, dict) or raw != _canonical(value):
        raise FixedE0ExecutionError(f"{label} is not canonical JSON")
    return value, raw


def _digest(document: Mapping[str, Any], field: str) -> str:
    return _sha(_canonical({key: value for key, value in document.items() if key != field})[:-1])


def _descriptor(path: Path, *, root: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise FixedE0ExecutionError("raw artifact is not a regular file")
    try:
        relative = str(path.resolve().relative_to(root))
    except ValueError as exc:
        raise FixedE0ExecutionError("raw artifact escapes run root") from exc
    return {"path": relative, "sha256": _sha(path.read_bytes())}


def _require_fixed_e0_plan(plan: Mapping[str, Any]) -> None:
    if (plan.get("schema_version") != 1 or
            plan.get("kind") != "kauri-n7-sustained-role-execution-plan-v1" or
            plan.get("state") != "PREPARED_DRY_RUN_EXTERNAL_APPROVAL_REQUIRED" or
            plan.get("no_retry") is not True or plan.get("plan_sha256") != _digest(plan, "plan_sha256")):
        raise FixedE0ExecutionError("sustained-role plan identity drift")
    comparison = plan.get("comparison")
    commands = plan.get("commands")
    if (not isinstance(comparison, Mapping) or comparison.get("arm") != "fixed_e0" or
            not isinstance(commands, Mapping) or not isinstance(commands.get("manager"), Mapping) or
            not isinstance(commands.get("replicas"), list) or len(commands["replicas"]) != 7):
        raise FixedE0ExecutionError("execution requires exactly the fixed-E0 seven-replica plan")
    argv = commands["manager"].get("argv")
    if (not isinstance(argv, list) or argv.count("--fault-window-arm-control-only") != 1 or
            "--transition-request" in argv or "--bundle-output" in argv):
        raise FixedE0ExecutionError("fixed-E0 manager argv lacks the native no-successor mode")
    for expected, replica in enumerate(commands["replicas"]):
        if (not isinstance(replica, Mapping) or replica.get("replica_id") != expected or
                not isinstance(replica.get("argv"), list) or not replica["argv"]):
            raise FixedE0ExecutionError("fixed-E0 replica argv identity drift")
    contract = plan.get("evidence_contract")
    if (not isinstance(contract, Mapping) or
            contract.get("prearm_all_seven_e0_common_commit") != "strictly_before_scheduled_window_start" or
            contract.get("common_horizon_ns") != 60_000_000_000 or
            contract.get("raw_stream_coverage") != "all_seven_replica_streams_through_anchor_plus_common_horizon"):
        raise FixedE0ExecutionError("fixed-E0 evidence contract drift")


def _raw_contract(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": RAW_KIND,
        "state": "SEALED_RAW_BUNDLE_NO_CLAIM",
        "sources": list(_SOURCES),
        "required_logs": [f"logs/{source}.log" for source in _SOURCES],
        "required_jsonl": [f"raw/{source}.jsonl" for source in _SOURCES],
        "required_runtime": [
            "runtime/sustained-role-fixed-e0-prearm-attestation.json",
            "runtime/cleanup-receipt.json",
        ],
        "prearm_rule": plan["evidence_contract"]["prearm_all_seven_e0_common_commit"],
        "anchor": plan["evidence_contract"]["anchor"],
        "horizon_ns": plan["evidence_contract"]["common_horizon_ns"],
        "no_retry": True,
    }


def prepare_execution(run_directory: Path) -> dict[str, Any]:
    """Seal executable inputs, but keep the path explicitly PREPARE_ONLY."""
    root = run_directory.resolve()
    plan, plan_bytes = _read_object(_child(root, PLAN), "sustained-role plan")
    request, request_bytes = _read_object(_child(root, REQUEST), "sustained-role request")
    _require_fixed_e0_plan(plan)
    expected_request = {
        "schema_version": 1,
        "kind": "kauri-n7-sustained-role-execution-authorization-request-v1",
        "execution_plan_sha256": plan["plan_sha256"],
        "repository_revision": plan["repository_revision"], "arm": "fixed_e0",
        "scheduled_window": plan["scheduled_window"],
        "hard_timeout_seconds": plan["hard_timeout_seconds"],
        "no_retry": True, "claim_eligible": False, "figure_eligible": False,
    }
    if request != expected_request:
        raise FixedE0ExecutionError("sustained-role authorization request drift")
    raw_contract = _raw_contract(plan)
    execution = {
        "schema_version": 1, "kind": EXECUTION_KIND,
        "state": "PREPARE_ONLY_NATIVE_VERIFICATION_REQUIRED",
        "sustained_plan_sha256": _sha(plan_bytes),
        "sustained_request_sha256": _sha(request_bytes),
        "repository_revision": plan["repository_revision"], "arm": "fixed_e0",
        "hard_timeout_seconds": plan["hard_timeout_seconds"], "no_retry": True,
        "manager_argv_sha256": plan["commands"]["manager"]["sha256"],
        "manager_executable_sha256": plan["commands"]["manager"]["executable_sha256"],
        "replica_argv_sha256": [row["sha256"] for row in plan["commands"]["replicas"]],
        "replica_executable_sha256": plan["commands"]["replicas"][0]["executable_sha256"],
        "raw_bundle_contract": raw_contract,
        "claim_boundary": "No process launched; an independent native verification receipt is required before any launch implementation may be enabled.",
    }
    execution["execution_plan_sha256"] = _digest(execution, "execution_plan_sha256")
    execution_bytes = _canonical(execution)
    launch_request = {
        "schema_version": 1, "kind": REQUEST_KIND,
        "execution_plan_sha256": execution["execution_plan_sha256"],
        "sustained_authorization_request_sha256": _sha(request_bytes),
        "arm": "fixed_e0", "hard_timeout_seconds": plan["hard_timeout_seconds"],
        "no_retry": True, "claim_eligible": False, "figure_eligible": False,
    }
    _write_once(_child(root, EXECUTION_PLAN), execution_bytes)
    _write_once(_child(root, EXECUTION_REQUEST), _canonical(launch_request))
    return {"state": execution["state"], "execution_plan_sha256": execution["execution_plan_sha256"],
            "execution_request_sha256": _sha(_canonical(launch_request)),
            "claim_boundary": execution["claim_boundary"]}


def finalize_prepare_only(run_directory: Path, authorization_path: Path,
                          native_verification_path: Path) -> dict[str, Any]:
    """Seal external approvals while intentionally refusing to launch."""
    root = run_directory.resolve()
    execution, execution_bytes = _read_object(_child(root, EXECUTION_PLAN), "fixed-E0 execution plan")
    request, request_bytes = _read_object(_child(root, EXECUTION_REQUEST), "fixed-E0 execution request")
    approval, approval_bytes = _read_object(authorization_path.resolve(), "external authorization")
    verification, verification_bytes = _read_object(native_verification_path.resolve(), "native verification")
    if (execution.get("kind") != EXECUTION_KIND or
            execution.get("state") != "PREPARE_ONLY_NATIVE_VERIFICATION_REQUIRED" or
            execution.get("execution_plan_sha256") != _digest(execution, "execution_plan_sha256") or
            request.get("execution_plan_sha256") != execution["execution_plan_sha256"]):
        raise FixedE0ExecutionError("fixed-E0 execution inputs drift")
    expected_approval = {"schema_version", "kind", "request_sha256", "execution_plan_sha256",
                         "approval_reference", "approved_utc", "no_retry"}
    if (set(approval) != expected_approval or approval.get("schema_version") != 1 or
            approval.get("kind") != APPROVAL_KIND or approval.get("request_sha256") != _sha(request_bytes) or
            approval.get("execution_plan_sha256") != execution["execution_plan_sha256"] or
            approval.get("no_retry") is not True or not isinstance(approval.get("approval_reference"), str) or
            not approval["approval_reference"].strip() or not isinstance(approval.get("approved_utc"), str) or
            not approval["approved_utc"].endswith("Z")):
        raise FixedE0ExecutionError("external fixed-E0 authorization is not exact")
    expected_verification = {"schema_version", "kind", "execution_plan_sha256", "verified_utc",
                             "manager_executable_sha256", "replica_executable_sha256", "outcome"}
    if (set(verification) != expected_verification or verification.get("schema_version") != 1 or
            verification.get("kind") != NATIVE_KIND or
            verification.get("execution_plan_sha256") != execution["execution_plan_sha256"] or
            verification.get("outcome") != "INDEPENDENT_NATIVE_EXECUTION_READY" or
            not isinstance(verification.get("verified_utc"), str) or not verification["verified_utc"].endswith("Z") or
            verification.get("manager_executable_sha256") != execution["manager_executable_sha256"] or
            verification.get("replica_executable_sha256") != execution["replica_executable_sha256"]):
        raise FixedE0ExecutionError("native verification receipt is not exact")
    if authorization_path.resolve() == _child(root, APPROVAL) or native_verification_path.resolve() == _child(root, NATIVE_VERIFICATION):
        raise FixedE0ExecutionError("approval inputs must be external to the archive")
    _write_once(_child(root, APPROVAL), approval_bytes)
    _write_once(_child(root, NATIVE_VERIFICATION), verification_bytes)
    finalization = {"schema_version": 1, "kind": FINALIZATION_KIND,
                    "state": "PREPARE_ONLY_AUDITED_LAUNCHER_MISSING",
                    "execution_plan_sha256": execution["execution_plan_sha256"],
                    "authorization_sha256": _sha(approval_bytes),
                    "native_verification_sha256": _sha(verification_bytes), "no_retry": True,
                    "claim_boundary": "Authorization and native verification are sealed; no launcher or experiment has run."}
    finalization["finalization_sha256"] = _digest(finalization, "finalization_sha256")
    _write_once(_child(root, FINALIZATION), _canonical(finalization))
    return {"state": finalization["state"], "claim_boundary": finalization["claim_boundary"]}


def seal_raw_descriptor(run_directory: Path) -> dict[str, Any]:
    """Hash a complete future raw layout without interpreting it as evidence."""
    root = run_directory.resolve()
    execution, _ = _read_object(_child(root, EXECUTION_PLAN), "fixed-E0 execution plan")
    finalization, _ = _read_object(_child(root, FINALIZATION), "fixed-E0 finalization")
    if finalization.get("state") != "PREPARE_ONLY_AUDITED_LAUNCHER_MISSING":
        raise FixedE0ExecutionError("raw descriptor requires a sealed prepare-only finalization")
    contract = execution.get("raw_bundle_contract")
    if not isinstance(contract, Mapping):
        raise FixedE0ExecutionError("raw descriptor contract is missing")
    paths = [*contract.get("required_logs", ()), *contract.get("required_jsonl", ()),
             *contract.get("required_runtime", ())]
    if len(paths) != 18 or len(set(paths)) != 18 or not all(isinstance(path, str) for path in paths):
        raise FixedE0ExecutionError("raw descriptor layout is not exactly eight logs, eight streams, and two receipts")
    artifacts = {path: _descriptor(_child(root, Path(path)), root=root) for path in paths}
    receipt = {"schema_version": 1, "kind": RAW_KIND, "state": "SEALED_RAW_BUNDLE_NO_CLAIM",
               "execution_plan_sha256": execution["execution_plan_sha256"],
               "finalization_sha256": finalization["finalization_sha256"],
               "contract": contract, "artifacts": artifacts,
               "claim_boundary": "Raw files are hash-sealed only. Independent replay and experiment validation remain required."}
    _write_once(_child(root, RAW_RECEIPT), _canonical(receipt))
    return {"state": receipt["state"], "receipt_sha256": _sha(_canonical(receipt)),
            "claim_boundary": receipt["claim_boundary"]}
