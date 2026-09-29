"""Strict, offline audit of the prospective W18 consumption artifact chain.

This module launches nothing and does not replay manager or replica events.  It
only checks that actual retained Stage-A, Stage-B and successor-bundle bytes are
bound by the two native verifier receipts and by the manager's exact canonical
consumption record.  Native verifier tool identity and raw execution evidence
remain separate gates, so success here is deliberately component-only.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping, Sequence


class ConsumptionAuditError(ValueError):
    """One retained input or one cross-artifact binding is invalid."""


_HEX = frozenset("0123456789abcdef")
_ARMS = frozenset({"fast_priority_treatment", "exact_copy_sham"})

_STAGE_A_RECEIPT_KEYS = (
    "schema_version",
    "kind",
    "verdict",
    "envelope_wire_sha256",
    "envelope_canonical_digest",
    "approved_capacity_digest",
    "issuer_id",
    "issuer_reference",
    "issuer_public_key_fingerprint",
    "arm",
    "source_revision",
    "verification_monotonic_raw_ns",
    "epoch0_tree_file_sha256",
    "epoch0_consensus_digest",
    "epoch0_topology_digest",
)

_STAGE_B_RECEIPT_KEYS = (
    "schema_version",
    "kind",
    "verdict",
    "source_revision",
    "authorization_wire_sha256",
    "authorization_canonical_digest",
    "epoch0_tree_file_sha256",
    "epoch0_consensus_digest",
    "epoch0_topology_digest",
    "issuer_id",
    "issuer_reference",
    "issuer_public_key_fingerprint",
    "label_issuer_reference",
    "approved_capacity_digest",
    "arm",
    "baseline_snapshot_id",
    "baseline_evidence_cutoff",
    "decision_monotonic_raw_ns",
)

# This order is the byte contract emitted by
# serialize_operator_capacity_consumption_record().
_CONSUMPTION_KEYS = (
    "arm",
    "baseline_evidence_cutoff",
    "baseline_snapshot_id",
    "capacity_digest",
    "decision_monotonic_raw_ns",
    "epoch0_digest",
    "epoch0_topology_digest",
    "epoch_change_issuer_id",
    "hard_deadline_monotonic_raw_ns",
    "kind",
    "label_issuer_id",
    "label_issuer_public_key_fingerprint",
    "label_issuer_reference",
    "run_id",
    "schema_version",
    "source_instance",
    "stage_a_semantic_digest",
    "stage_a_wire_sha256",
    "stage_b_authorization_wire_sha256",
    "successor_bundle_sha256",
    "successor_policy_snapshot_id",
)

_PIN_KEYS = frozenset({
    "source_revision",
    "arm",
    "epoch0_consensus_digest",
    "epoch0_topology_digest",
    "approved_capacity_digest",
    "label_issuer_id",
    "label_issuer_reference",
    "label_issuer_public_key_fingerprint",
    "epoch_change_issuer_id",
    "epoch_change_issuer_reference",
    "epoch_change_issuer_public_key_fingerprint",
    "run_id",
    "source_instance",
    "baseline_snapshot_id",
    "baseline_evidence_cutoff",
    "decision_monotonic_raw_ns",
    "hard_deadline_monotonic_raw_ns",
    "successor_policy_snapshot_id",
})


def _read_regular(path: Path, maximum_bytes: int, label: str) -> bytes:
    """Read one bounded final-component non-symlink through one descriptor."""
    if maximum_bytes <= 0:
        raise ConsumptionAuditError(f"{label} byte limit is invalid")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    except OSError as exc:
        raise ConsumptionAuditError(f"{label} is not a readable regular file") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ConsumptionAuditError(f"{label} is not a regular file")
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            raise ConsumptionAuditError(f"{label} size is invalid")
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise ConsumptionAuditError(f"{label} changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ConsumptionAuditError(f"{label} changed during read")
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after or not stat.S_ISREG(after.st_mode):
            raise ConsumptionAuditError(f"{label} changed during read")
        return b"".join(chunks)
    except OSError as exc:
        raise ConsumptionAuditError(f"cannot read {label}") from exc
    finally:
        os.close(descriptor)


def _reject_constant(value: str) -> None:
    raise ConsumptionAuditError(f"non-finite JSON constant is forbidden: {value}")


def _pairs(label: str):
    def decode(items: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ConsumptionAuditError(f"{label} repeats JSON field {key}")
            result[key] = value
        return result
    return decode


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise ConsumptionAuditError(f"{label} does not have exact one-line framing")
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_pairs(label),
            parse_constant=_reject_constant,
        )
    except ConsumptionAuditError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConsumptionAuditError(f"{label} is not strict ASCII JSON") from exc
    if not isinstance(value, dict):
        raise ConsumptionAuditError(f"{label} is not a JSON object")
    return value


def _canonical_bytes(value: Mapping[str, Any], order: Sequence[str]) -> bytes:
    ordered = {key: value[key] for key in order}
    return json.dumps(
        ordered, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
    ).encode("ascii") + b"\n"


def _require_exact_document(
    raw: bytes, value: Mapping[str, Any], order: Sequence[str], label: str,
) -> None:
    if set(value) != set(order):
        raise ConsumptionAuditError(f"{label} fields differ from its strict schema")
    if raw != _canonical_bytes(value, order):
        raise ConsumptionAuditError(f"{label} bytes are not the native canonical form")


def _hex(value: object, label: str, size: int = 64) -> str:
    if (not isinstance(value, str) or len(value) != size or
            any(character not in _HEX for character in value)):
        raise ConsumptionAuditError(f"{label} is not lower-case hexadecimal")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0 or value > (1 << 64) - 1:
        raise ConsumptionAuditError(f"{label} is not a positive uint64")
    return value


def _positive_u32(value: object, label: str) -> int:
    result = _positive(value, label)
    if result > (1 << 32) - 1:
        raise ConsumptionAuditError(f"{label} exceeds uint32")
    return result


def _ascii(value: object, label: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ConsumptionAuditError(f"{label} length is invalid")
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        raise ConsumptionAuditError(f"{label} is not printable ASCII")
    return value


def _arm(value: object, label: str) -> str:
    if not isinstance(value, str) or value not in _ARMS:
        raise ConsumptionAuditError(f"{label} is invalid")
    return value


def _schema_one(value: object, label: str) -> None:
    if type(value) is not int or value != 1:
        raise ConsumptionAuditError(f"{label} schema version is invalid")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _validate_pins(pins: Mapping[str, object]) -> None:
    if not isinstance(pins, Mapping) or set(pins) != _PIN_KEYS:
        raise ConsumptionAuditError("audit pins differ from the strict schema")
    _hex(pins["source_revision"], "pinned source revision", 40)
    _arm(pins["arm"], "pinned arm")
    for key in (
        "epoch0_consensus_digest", "epoch0_topology_digest",
        "approved_capacity_digest", "label_issuer_public_key_fingerprint",
        "epoch_change_issuer_public_key_fingerprint", "baseline_snapshot_id",
        "successor_policy_snapshot_id",
    ):
        _hex(pins[key], f"pinned {key}")
    _positive_u32(pins["label_issuer_id"], "pinned label issuer ID")
    _positive_u32(pins["epoch_change_issuer_id"], "pinned epoch-change issuer ID")
    for key in (
        "label_issuer_reference", "epoch_change_issuer_reference",
        "run_id", "source_instance",
    ):
        _ascii(pins[key], f"pinned {key}")
    _positive(pins["baseline_evidence_cutoff"], "pinned baseline cutoff")
    decision = _positive(pins["decision_monotonic_raw_ns"], "pinned decision time")
    deadline = _positive(pins["hard_deadline_monotonic_raw_ns"], "pinned hard deadline")
    if deadline <= decision:
        raise ConsumptionAuditError("pinned hard deadline does not follow decision time")


def audit_consumption_chain(
    *,
    stage_a_wire: Path,
    stage_b_wire: Path,
    successor_bundle: Path,
    consumption_record: Path,
    stage_a_verifier_receipt: Path,
    stage_b_verifier_receipt: Path,
    pins: Mapping[str, object],
) -> dict[str, object]:
    """Audit retained component bytes without granting execution evidence."""
    _validate_pins(pins)
    a_wire = _read_regular(stage_a_wire, 32 * 1024, "Stage-A wire")
    b_wire = _read_regular(stage_b_wire, 128 * 1024, "Stage-B wire")
    bundle = _read_regular(successor_bundle, 1024 * 1024, "successor bundle")
    record_raw = _read_regular(consumption_record, 64 * 1024, "consumption record")
    a_receipt_raw = _read_regular(
        stage_a_verifier_receipt, 64 * 1024, "Stage-A verifier receipt",
    )
    b_receipt_raw = _read_regular(
        stage_b_verifier_receipt, 64 * 1024, "Stage-B verifier receipt",
    )

    record = _json_object(record_raw, "consumption record")
    a_receipt = _json_object(a_receipt_raw, "Stage-A verifier receipt")
    b_receipt = _json_object(b_receipt_raw, "Stage-B verifier receipt")
    _require_exact_document(record_raw, record, _CONSUMPTION_KEYS, "consumption record")
    _require_exact_document(a_receipt_raw, a_receipt, _STAGE_A_RECEIPT_KEYS,
                            "Stage-A verifier receipt")
    _require_exact_document(b_receipt_raw, b_receipt, _STAGE_B_RECEIPT_KEYS,
                            "Stage-B verifier receipt")

    _schema_one(a_receipt["schema_version"], "Stage-A receipt")
    _schema_one(b_receipt["schema_version"], "Stage-B receipt")
    _schema_one(record["schema_version"], "consumption record")
    if (a_receipt["kind"] != "kauri-operator-capacity-native-envelope-verification-receipt-v1" or
            a_receipt["verdict"] != "NATIVE_ENVELOPE_VERIFIED_NO_EXECUTION"):
        raise ConsumptionAuditError("Stage-A native receipt identity is invalid")
    if (b_receipt["kind"] != "kauri-operator-capacity-native-stage-b-verification-receipt-v1" or
            b_receipt["verdict"] != "NATIVE_STAGE_B_VERIFIED_NO_EXECUTION"):
        raise ConsumptionAuditError("Stage-B native receipt identity is invalid")
    if record["kind"] != "kauri-operator-capacity-consumption-v1":
        raise ConsumptionAuditError("consumption record identity is invalid")

    for value, label in (
        (a_receipt["envelope_wire_sha256"], "Stage-A receipt wire hash"),
        (a_receipt["envelope_canonical_digest"], "Stage-A semantic digest"),
        (a_receipt["approved_capacity_digest"], "Stage-A capacity digest"),
        (a_receipt["issuer_public_key_fingerprint"], "Stage-A issuer fingerprint"),
        (a_receipt["epoch0_tree_file_sha256"], "Stage-A tree-file hash"),
        (a_receipt["epoch0_consensus_digest"], "Stage-A E0 digest"),
        (a_receipt["epoch0_topology_digest"], "Stage-A topology digest"),
        (b_receipt["authorization_wire_sha256"], "Stage-B receipt wire hash"),
        (b_receipt["authorization_canonical_digest"], "Stage-B semantic digest"),
        (b_receipt["epoch0_tree_file_sha256"], "Stage-B tree-file hash"),
        (b_receipt["epoch0_consensus_digest"], "Stage-B E0 digest"),
        (b_receipt["epoch0_topology_digest"], "Stage-B topology digest"),
        (b_receipt["issuer_public_key_fingerprint"], "Stage-B issuer fingerprint"),
        (b_receipt["approved_capacity_digest"], "Stage-B capacity digest"),
        (b_receipt["baseline_snapshot_id"], "Stage-B baseline snapshot"),
    ):
        _hex(value, label)
    _hex(a_receipt["source_revision"], "Stage-A source revision", 40)
    _hex(b_receipt["source_revision"], "Stage-B source revision", 40)
    _positive_u32(a_receipt["issuer_id"], "Stage-A issuer ID")
    _positive_u32(b_receipt["issuer_id"], "Stage-B issuer ID")
    stage_a_verification = _positive(
        a_receipt["verification_monotonic_raw_ns"], "Stage-A verification time",
    )
    _positive(b_receipt["baseline_evidence_cutoff"], "Stage-B baseline cutoff")
    _positive(b_receipt["decision_monotonic_raw_ns"], "Stage-B decision time")
    for value, label in (
        (a_receipt["issuer_reference"], "Stage-A issuer reference"),
        (b_receipt["issuer_reference"], "Stage-B issuer reference"),
        (b_receipt["label_issuer_reference"], "Stage-B label issuer reference"),
    ):
        _ascii(value, label)
    _arm(a_receipt["arm"], "Stage-A receipt arm")
    _arm(b_receipt["arm"], "Stage-B receipt arm")

    for key in (
        "stage_a_wire_sha256", "stage_a_semantic_digest",
        "stage_b_authorization_wire_sha256", "capacity_digest",
        "epoch0_digest", "epoch0_topology_digest",
        "baseline_snapshot_id", "label_issuer_public_key_fingerprint",
        "successor_policy_snapshot_id", "successor_bundle_sha256",
    ):
        _hex(record[key], f"consumption {key}")
    _positive_u32(record["label_issuer_id"], "consumption label issuer ID")
    _positive_u32(record["epoch_change_issuer_id"], "consumption epoch-change issuer ID")
    _positive(record["baseline_evidence_cutoff"], "consumption baseline cutoff")
    decision = _positive(record["decision_monotonic_raw_ns"], "consumption decision time")
    deadline = _positive(record["hard_deadline_monotonic_raw_ns"], "consumption hard deadline")
    if deadline <= decision:
        raise ConsumptionAuditError("consumption hard deadline does not follow decision")
    for key in ("label_issuer_reference", "run_id", "source_instance"):
        _ascii(record[key], f"consumption {key}")
    _arm(record["arm"], "consumption arm")
    if stage_a_verification > decision:
        raise ConsumptionAuditError("Stage-A verification follows the live decision")

    if a_receipt["envelope_wire_sha256"] != _sha256(a_wire):
        raise ConsumptionAuditError("actual Stage-A wire hash differs from native receipt")
    if b_receipt["authorization_wire_sha256"] != _sha256(b_wire):
        raise ConsumptionAuditError("actual Stage-B wire hash differs from native receipt")
    if record["stage_a_wire_sha256"] != _sha256(a_wire):
        raise ConsumptionAuditError("consumption record does not bind actual Stage-A wire")
    if record["stage_b_authorization_wire_sha256"] != _sha256(b_wire):
        raise ConsumptionAuditError("consumption record does not bind actual Stage-B wire")
    if record["successor_bundle_sha256"] != _sha256(bundle):
        raise ConsumptionAuditError("consumption record does not bind actual successor bundle")

    exact = (
        (a_receipt["source_revision"], pins["source_revision"], "Stage-A revision"),
        (b_receipt["source_revision"], pins["source_revision"], "Stage-B revision"),
        (a_receipt["arm"], pins["arm"], "Stage-A arm"),
        (b_receipt["arm"], pins["arm"], "Stage-B arm"),
        (record["arm"], pins["arm"], "consumption arm"),
        (a_receipt["epoch0_consensus_digest"], pins["epoch0_consensus_digest"], "Stage-A E0"),
        (b_receipt["epoch0_consensus_digest"], pins["epoch0_consensus_digest"], "Stage-B E0"),
        (record["epoch0_digest"], pins["epoch0_consensus_digest"], "consumption E0"),
        (a_receipt["epoch0_topology_digest"], pins["epoch0_topology_digest"], "Stage-A topology"),
        (b_receipt["epoch0_topology_digest"], pins["epoch0_topology_digest"], "Stage-B topology"),
        (record["epoch0_topology_digest"], pins["epoch0_topology_digest"], "consumption topology"),
        (a_receipt["approved_capacity_digest"], pins["approved_capacity_digest"], "Stage-A capacity"),
        (b_receipt["approved_capacity_digest"], pins["approved_capacity_digest"], "Stage-B capacity"),
        (record["capacity_digest"], pins["approved_capacity_digest"], "consumption capacity"),
        (a_receipt["issuer_id"], pins["label_issuer_id"], "Stage-A issuer ID"),
        (record["label_issuer_id"], pins["label_issuer_id"], "consumption label issuer ID"),
        (a_receipt["issuer_reference"], pins["label_issuer_reference"], "Stage-A issuer reference"),
        (b_receipt["label_issuer_reference"], pins["label_issuer_reference"], "Stage-B label reference"),
        (record["label_issuer_reference"], pins["label_issuer_reference"], "consumption label reference"),
        (a_receipt["issuer_public_key_fingerprint"], pins["label_issuer_public_key_fingerprint"], "Stage-A key"),
        (record["label_issuer_public_key_fingerprint"], pins["label_issuer_public_key_fingerprint"], "consumption label key"),
        (b_receipt["issuer_id"], pins["epoch_change_issuer_id"], "Stage-B issuer ID"),
        (record["epoch_change_issuer_id"], pins["epoch_change_issuer_id"], "consumption epoch-change issuer ID"),
        (b_receipt["issuer_reference"], pins["epoch_change_issuer_reference"], "Stage-B issuer reference"),
        (b_receipt["issuer_public_key_fingerprint"], pins["epoch_change_issuer_public_key_fingerprint"], "Stage-B key"),
        (record["run_id"], pins["run_id"], "run ID"),
        (record["source_instance"], pins["source_instance"], "source instance"),
        (b_receipt["baseline_snapshot_id"], pins["baseline_snapshot_id"], "Stage-B baseline snapshot"),
        (record["baseline_snapshot_id"], pins["baseline_snapshot_id"], "consumption baseline snapshot"),
        (b_receipt["baseline_evidence_cutoff"], pins["baseline_evidence_cutoff"], "Stage-B cutoff"),
        (record["baseline_evidence_cutoff"], pins["baseline_evidence_cutoff"], "consumption cutoff"),
        (b_receipt["decision_monotonic_raw_ns"], pins["decision_monotonic_raw_ns"], "Stage-B decision time"),
        (record["decision_monotonic_raw_ns"], pins["decision_monotonic_raw_ns"], "consumption decision time"),
        (record["hard_deadline_monotonic_raw_ns"], pins["hard_deadline_monotonic_raw_ns"], "consumption deadline"),
        (record["successor_policy_snapshot_id"], pins["successor_policy_snapshot_id"], "successor policy snapshot"),
        (record["stage_a_semantic_digest"], a_receipt["envelope_canonical_digest"], "Stage-A semantic digest"),
        (a_receipt["epoch0_tree_file_sha256"], b_receipt["epoch0_tree_file_sha256"], "E0 tree-file hash"),
    )
    for observed, expected, label in exact:
        if observed != expected:
            raise ConsumptionAuditError(f"{label} binding differs")

    return {
        "verdict": "COMPONENT_CHAIN_VALID_NO_RAW_REPLAY",
        "claim_eligible": False,
        "figure_eligible": False,
        "raw_replay_validated": False,
        "source_revision": pins["source_revision"],
        "arm": pins["arm"],
        "stage_a_wire_sha256": _sha256(a_wire),
        "stage_b_authorization_wire_sha256": _sha256(b_wire),
        "successor_bundle_sha256": _sha256(bundle),
        "baseline_snapshot_id": pins["baseline_snapshot_id"],
        "baseline_evidence_cutoff": pins["baseline_evidence_cutoff"],
        "claim_boundary": (
            "Component byte-chain only; native verifier tool identity, raw manager/replica "
            "replay, activation, commits, quotas, cleanup, campaign acceptance, figures, "
            "and thesis claims remain unverified."
        ),
    }
