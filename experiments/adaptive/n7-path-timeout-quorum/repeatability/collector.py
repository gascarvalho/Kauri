#!/usr/bin/env python3
"""Fail-closed collector for a prospective, active-only N=7 v4 repetition.

This module launches nothing.  A future authorized slot can enter the fixed
denominator only through a replay of its source-bound v4 raw bundle and a
hash-bound archive receipt.  Aborts and incompletes are retained; they cannot
be retried or replaced by this collector.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "active_repeatability_manifest.json"
_HEX = frozenset("0123456789abcdef")
_TERMINAL = frozenset({"RAW_BUNDLE_VALIDATED", "ABORTED", "INCOMPLETE"})
_EXPECTED_PROFILE = {
    "profile_id": "n7-path-local-timeout-quorum-v4",
    "profile_sha256": "3e2b2af834279168199db31bd5ee47e0abdef480d1d5327a17cbcc1b57efc244",
    "snapshot_seed": 41719,
    "epoch0_tree_sha256": "38a2baa37b7fcec43f5c58423be068d807cc253fedbdc379520a3e42428dffcc",
    "replica_ids": list(range(7)),
    "fault_threshold": 2,
    "quorum": 5,
    "omitting_replica": 1,
    "tree_ids": [4, 5, 6],
    "parent_reporters": [4, 5, 6],
    "physical_omission_causality_basis": "exact_matched_post_arm_physical_omission_v1",
}
_EXPECTED_EXECUTION = {
    "hard_timeout_seconds": 600,
    "external_timeout_seconds": 720,
    "no_retry": True,
    "replacement_runs": False,
    "requires_distinct_execution_authorization_per_slot": True,
}
_EXPECTED_SLOTS = (
    ("R1", "n7-path-quorum-v4-repeatability-r1", "n7-path-quorum-v4-repeatability-r1-archive"),
    ("R2", "n7-path-quorum-v4-repeatability-r2", "n7-path-quorum-v4-repeatability-r2-archive"),
    ("R3", "n7-path-quorum-v4-repeatability-r3", "n7-path-quorum-v4-repeatability-r3-archive"),
)


class ValidationError(ValueError):
    """The prospective freeze or retained terminal records are invalid."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex64(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in _HEX for char in value):
        raise ValidationError(f"{label} must be a lower-case SHA-256")
    return value


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    return _sha256(_canonical({key: value for key, value in manifest.items() if key != "manifest_sha256"}))


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("cannot read prospective repetition manifest") from exc
    if not isinstance(value, dict):
        raise ValidationError("prospective repetition manifest is not an object")
    return value


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version", "study_id", "state", "claim_boundary", "frozen_profile",
        "execution_policy", "slots", "denominator", "replay_and_archive_gate", "manifest_sha256",
    }
    if set(manifest) != required or manifest.get("schema_version") != 1:
        raise ValidationError("repeatability manifest schema drift")
    if manifest.get("study_id") != "n7-path-local-timeout-quorum-v4-active-repeatability-v1":
        raise ValidationError("repeatability study identity drift")
    if manifest.get("state") != "NON_EVIDENTIARY_PLANNING_NO_LAUNCH":
        raise ValidationError("repeatability manifest must remain non-evidentiary and no-launch")
    if not isinstance(manifest.get("claim_boundary"), str) or not manifest["claim_boundary"]:
        raise ValidationError("repeatability claim boundary is missing")
    if manifest.get("manifest_sha256") != manifest_digest(manifest):
        raise ValidationError("repeatability manifest semantic digest does not recompute")
    if not isinstance(manifest.get("frozen_profile"), Mapping) or dict(manifest["frozen_profile"]) != _EXPECTED_PROFILE:
        raise ValidationError("existing v4 profile, seed, tree, or omission identity drifted")
    if not isinstance(manifest.get("execution_policy"), Mapping) or dict(manifest["execution_policy"]) != _EXPECTED_EXECUTION:
        raise ValidationError("no-retry execution policy drifted")
    slots = manifest.get("slots")
    if not isinstance(slots, list) or [(slot.get("slot_id"), slot.get("root_id"), slot.get("archive_root_id")) if isinstance(slot, Mapping) else None for slot in slots] != list(_EXPECTED_SLOTS):
        raise ValidationError("three predeclared slot identities or roots drifted")
    if len({root_id for _, root_id, _ in _EXPECTED_SLOTS}) != len(_EXPECTED_SLOTS) or len({archive_root_id for _, _, archive_root_id in _EXPECTED_SLOTS}) != len(_EXPECTED_SLOTS):
        raise ValidationError("repeatability roots must be unique")
    denominator = manifest.get("denominator")
    expected_denominator = {
        "scheduled_slots": 3,
        "retain_terminal_statuses": ["RAW_BUNDLE_VALIDATED", "ABORTED", "INCOMPLETE"],
        "missing_slot_is_rejected": True,
        "failed_slot_is_retained_not_replaced": True,
    }
    if not isinstance(denominator, Mapping) or dict(denominator) != expected_denominator:
        raise ValidationError("fixed denominator or retained-failure policy drifted")
    gate = manifest.get("replay_and_archive_gate")
    expected_gate = {
        "required_raw_receipt": "raw-bundle-receipt.json",
        "required_raw_verdict": "raw-bundle-verdict.json",
        "required_validator_verdict": "RAW_BUNDLE_VALIDATED",
        "required_terminal_receipt": "repeatability-terminal-receipt.json",
        "required_archive_receipt": "repeatability-archive-receipt.json",
        "required_archive_file_manifest": "archive-file-manifest.json",
        "archive_contract": "terminal and archive receipts bind the actual authorization, source artifact, source root, and archive file manifest",
    }
    if not isinstance(gate, Mapping) or dict(gate) != expected_gate:
        raise ValidationError("raw replay or archive gate drifted")
    return {"verdict": "NON_EVIDENTIARY_REPEATABILITY_PLAN_VALID", "study_id": manifest["study_id"], "scheduled_slots": 3}


def _load_v4_validator():
    path = HERE.parent / "validator.py"
    spec = importlib.util.spec_from_file_location("n7_path_timeout_v4_replay", path)
    if spec is None or spec.loader is None:
        raise ValidationError("cannot load existing v4 raw-bundle validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_canonical_object(path: Path, label: str) -> tuple[Mapping[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read {label}") from exc
    if not isinstance(value, Mapping) or raw != _canonical(value):
        raise ValidationError(f"{label} is not canonical JSON")
    return value, raw


def _validate_archive_receipt(
    archive_root: Path, *, manifest: Mapping[str, Any], slot_id: str, root_id: str, archive_root_id: str,
    run_id: str, receipt_sha256: str, verdict_sha256: str,
) -> dict[str, Any]:
    gate = manifest["replay_and_archive_gate"]
    if archive_root.is_symlink() or not archive_root.is_dir():
        raise ValidationError("archive root is not a directory")
    archive, raw = _read_canonical_object(archive_root / gate["required_archive_receipt"], "hash-bound archive receipt")
    required = {
        "schema_version", "kind", "study_id", "manifest_sha256", "slot_id", "source_root_id",
        "source_run_id", "source_receipt_sha256", "source_verdict_sha256", "archive_root_id",
        "archive_file_manifest_sha256",
    }
    if set(archive) != required or archive.get("schema_version") != 1 or archive.get("kind") != "n7-active-repeatability-archive-v1":
        raise ValidationError("archive receipt schema drift")
    if (archive.get("study_id") != manifest["study_id"] or archive.get("manifest_sha256") != manifest["manifest_sha256"] or
            archive.get("slot_id") != slot_id or archive.get("source_root_id") != root_id or archive.get("source_run_id") != run_id or
            archive.get("source_receipt_sha256") != receipt_sha256 or archive.get("source_verdict_sha256") != verdict_sha256):
        raise ValidationError("archive receipt is not bound to this source raw bundle")
    if archive.get("archive_root_id") != archive_root_id:
        raise ValidationError("archive receipt lacks the scheduled archive root identity")
    _, archive_manifest_raw = _read_canonical_object(archive_root / gate["required_archive_file_manifest"], "actual archive file manifest")
    if archive.get("archive_file_manifest_sha256") != _sha256(archive_manifest_raw):
        raise ValidationError("archive receipt does not match the actual archive file manifest")
    return {"archive_receipt_sha256": _sha256(raw), "archive_root_id": archive["archive_root_id"]}


def _scheduled_slot(slot: Mapping[str, Any]) -> tuple[str, str, str]:
    required = {"slot_id", "root_id", "manifest_sha256"}
    if set(slot) != required or not isinstance(slot.get("slot_id"), str):
        raise ValidationError("scheduled slot schema drift")
    for expected in _EXPECTED_SLOTS:
        if slot["slot_id"] == expected[0] and slot.get("root_id") == expected[1]:
            return expected
    raise ValidationError("scheduled slot does not match its frozen root")


def _authorization_sha256(run_root: Path) -> str:
    try:
        raw = (run_root / "runtime/approved-execution-authorization.json").read_bytes()
    except OSError as exc:
        raise ValidationError("source terminal receipt lacks archived execution authorization") from exc
    return _sha256(raw)


def _validate_source_terminal_receipt(
    manifest: Mapping[str, Any], slot: Mapping[str, Any], run_root: Path,
) -> tuple[Mapping[str, Any], Mapping[str, Any], bytes]:
    slot_id, root_id, _ = _scheduled_slot(slot)
    if slot.get("manifest_sha256") != manifest["manifest_sha256"]:
        raise ValidationError("scheduled slot does not bind the frozen manifest")
    if run_root.is_symlink() or not run_root.is_dir():
        raise ValidationError("terminal slot run root is not a directory")
    terminal, terminal_raw = _read_canonical_object(
        run_root / manifest["replay_and_archive_gate"]["required_terminal_receipt"], "source-bound terminal receipt"
    )
    required = {
        "schema_version", "kind", "study_id", "manifest_sha256", "slot_id", "source_root_id",
        "source_run_id", "status", "execution_authorization_sha256", "terminal_artifact_path",
        "terminal_artifact_sha256",
    }
    if set(terminal) != required or terminal.get("schema_version") != 1 or terminal.get("kind") != "n7-active-repeatability-terminal-v1":
        raise ValidationError("source terminal receipt schema drift")
    if (terminal.get("study_id") != manifest["study_id"] or terminal.get("manifest_sha256") != manifest["manifest_sha256"] or
            terminal.get("slot_id") != slot_id or terminal.get("source_root_id") != root_id or
            not isinstance(terminal.get("source_run_id"), str) or not terminal["source_run_id"] or
            terminal.get("status") not in _TERMINAL or
            terminal.get("execution_authorization_sha256") != _authorization_sha256(run_root)):
        raise ValidationError("source terminal receipt is not bound to the scheduled root or authorization")
    expected_artifact = {
        "RAW_BUNDLE_VALIDATED": "raw-bundle-verdict.json",
        "ABORTED": "local-run-abort.json",
        "INCOMPLETE": "local-run-incomplete.json",
    }[terminal["status"]]
    if terminal.get("terminal_artifact_path") != expected_artifact:
        raise ValidationError("source terminal receipt has the wrong terminal artifact path")
    artifact, artifact_raw = _read_canonical_object(run_root / expected_artifact, "source terminal artifact")
    if terminal.get("terminal_artifact_sha256") != _sha256(artifact_raw):
        raise ValidationError("source terminal receipt does not bind the actual terminal artifact")
    if artifact.get("run_id") != terminal["source_run_id"] or artifact.get("status") != terminal["status"]:
        raise ValidationError("source terminal artifact does not match its receipt status or run identity")
    return terminal, artifact, terminal_raw


def replay_validated_slot(
    manifest: Mapping[str, Any], slot: Mapping[str, Any], run_root: Path, archive_root: Path,
) -> dict[str, Any]:
    """Re-run the existing v4 validator after source terminal receipt validation."""
    validate_manifest(manifest)
    slot_id, root_id, archive_root_id = _scheduled_slot(slot)
    terminal, verdict, _ = _validate_source_terminal_receipt(manifest, slot, run_root)
    if terminal["status"] != "RAW_BUNDLE_VALIDATED":
        raise ValidationError("only a source-bound validated terminal receipt may enter raw replay")
    gate = manifest["replay_and_archive_gate"]
    receipt, receipt_raw = _read_canonical_object(run_root / gate["required_raw_receipt"], "raw bundle receipt")
    verdict_raw = _canonical(verdict)
    replay = _load_v4_validator().validate_raw_bundle(run_root, receipt)
    if replay.get("verdict") != gate["required_validator_verdict"] or verdict != replay:
        raise ValidationError("existing v4 raw-bundle replay does not reproduce the sealed verdict")
    if archive_root.resolve() == run_root.resolve():
        raise ValidationError("archive root must be distinct from its source run root")
    archive = _validate_archive_receipt(
        archive_root, manifest=manifest, slot_id=slot_id, root_id=root_id, archive_root_id=archive_root_id,
        run_id=terminal["source_run_id"], receipt_sha256=_sha256(receipt_raw), verdict_sha256=_sha256(verdict_raw),
    )
    return {"slot_id": slot_id, "root_id": root_id, "status": terminal["status"], "run_id": terminal["source_run_id"], **archive}


def collect_terminal_slots(
    manifest: Mapping[str, Any], slots: Sequence[object], *, run_roots: Mapping[str, Path], archive_roots: Mapping[str, Path],
) -> dict[str, Any]:
    """Validate the complete fixed denominator; missing slots fail closed."""
    validate_manifest(manifest)
    if len(slots) != 3:
        raise ValidationError("all three scheduled terminal slots are required")
    by_id: dict[str, Mapping[str, Any]] = {}
    for value in slots:
        if not isinstance(value, Mapping):
            raise ValidationError("scheduled slot is not an object")
        slot_id, _, _ = _scheduled_slot(value)
        if slot_id in by_id:
            raise ValidationError("terminal slots must be unique and scheduled")
        if value.get("manifest_sha256") != manifest["manifest_sha256"]:
            raise ValidationError("scheduled slot manifest binding is invalid")
        by_id[slot_id] = value
    if set(by_id) != {slot_id for slot_id, _, _ in _EXPECTED_SLOTS}:
        raise ValidationError("one or more scheduled slots are missing")
    results: list[dict[str, Any]] = []
    source_run_ids: set[str] = set()
    for slot_id, root_id, _ in _EXPECTED_SLOTS:
        slot = by_id[slot_id]
        root = run_roots.get(slot_id)
        if not isinstance(root, Path):
            raise ValidationError("scheduled terminal slot lacks its source root")
        terminal, _, _ = _validate_source_terminal_receipt(manifest, slot, root)
        if terminal["source_run_id"] in source_run_ids:
            raise ValidationError("source run IDs must be unique across the three scheduled slots")
        source_run_ids.add(terminal["source_run_id"])
        if terminal["status"] == "RAW_BUNDLE_VALIDATED":
            archive_root = archive_roots.get(slot_id)
            if not isinstance(archive_root, Path):
                raise ValidationError("validated terminal slot lacks its archive root")
            results.append(replay_validated_slot(manifest, slot, root, archive_root))
        else:
            results.append({"slot_id": slot_id, "root_id": root_id, "status": terminal["status"], "source_run_id": terminal["source_run_id"]})
    return {
        "verdict": "REPEATABILITY_DENOMINATOR_RETAINED",
        "scheduled_slots": 3,
        "terminal_slots": results,
        "claim_boundary": "Planning-only collector output. It records source-receipt-derived same-profile terminal outcomes and does not establish a comparative advantage, scenario breadth, throughput, or general Byzantine-resilience result.",
    }
