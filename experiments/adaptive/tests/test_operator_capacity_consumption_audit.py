from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT / "kauri_experiment" / "operator_capacity_consumption_audit.py"
)
SPEC = importlib.util.spec_from_file_location(
    "operator_capacity_consumption_audit_test", MODULE_PATH,
)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: dict[str, object], order: tuple[str, ...]) -> bytes:
    ordered = {key: value[key] for key in order}
    return json.dumps(
        ordered, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
    ).encode("ascii") + b"\n"


def _fixture(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    stage_a = b"signed-stage-a-wire-v1"
    stage_b = b"signed-stage-b-wire-v1"
    bundle = b"signed-adaptive-v3-successor-bundle-v2"
    pins: dict[str, object] = {
        "source_revision": "1" * 40,
        "arm": "fast_priority_treatment",
        "epoch0_consensus_digest": "2" * 64,
        "epoch0_topology_digest": "3" * 64,
        "approved_capacity_digest": "4" * 64,
        "label_issuer_id": 74,
        "label_issuer_reference": "w18-label-authority",
        "label_issuer_public_key_fingerprint": "5" * 64,
        "epoch_change_issuer_id": 73,
        "epoch_change_issuer_reference": "w18-epoch-authority",
        "epoch_change_issuer_public_key_fingerprint": "6" * 64,
        "run_id": "w18-run-001",
        "source_instance": "manager-instance-001",
        "baseline_snapshot_id": "7" * 64,
        "baseline_evidence_cutoff": 124,
        "decision_monotonic_raw_ns": 500,
        "hard_deadline_monotonic_raw_ns": 1000,
        "successor_policy_snapshot_id": "8" * 64,
    }
    stage_a_receipt: dict[str, object] = {
        "schema_version": 1,
        "kind": "kauri-operator-capacity-native-envelope-verification-receipt-v1",
        "verdict": "NATIVE_ENVELOPE_VERIFIED_NO_EXECUTION",
        "envelope_wire_sha256": _sha(stage_a),
        "envelope_canonical_digest": "9" * 64,
        "approved_capacity_digest": pins["approved_capacity_digest"],
        "issuer_id": pins["label_issuer_id"],
        "issuer_reference": pins["label_issuer_reference"],
        "issuer_public_key_fingerprint": pins["label_issuer_public_key_fingerprint"],
        "arm": pins["arm"],
        "source_revision": pins["source_revision"],
        "verification_monotonic_raw_ns": 450,
        "epoch0_tree_file_sha256": "b" * 64,
        "epoch0_consensus_digest": pins["epoch0_consensus_digest"],
        "epoch0_topology_digest": pins["epoch0_topology_digest"],
    }
    stage_b_receipt: dict[str, object] = {
        "schema_version": 1,
        "kind": "kauri-operator-capacity-native-stage-b-verification-receipt-v1",
        "verdict": "NATIVE_STAGE_B_VERIFIED_NO_EXECUTION",
        "source_revision": pins["source_revision"],
        "authorization_wire_sha256": _sha(stage_b),
        "authorization_canonical_digest": "a" * 64,
        "epoch0_tree_file_sha256": "b" * 64,
        "epoch0_consensus_digest": pins["epoch0_consensus_digest"],
        "epoch0_topology_digest": pins["epoch0_topology_digest"],
        "issuer_id": pins["epoch_change_issuer_id"],
        "issuer_reference": pins["epoch_change_issuer_reference"],
        "issuer_public_key_fingerprint": pins[
            "epoch_change_issuer_public_key_fingerprint"
        ],
        "label_issuer_reference": pins["label_issuer_reference"],
        "approved_capacity_digest": pins["approved_capacity_digest"],
        "arm": pins["arm"],
        "baseline_snapshot_id": pins["baseline_snapshot_id"],
        "baseline_evidence_cutoff": pins["baseline_evidence_cutoff"],
        "decision_monotonic_raw_ns": pins["decision_monotonic_raw_ns"],
    }
    record: dict[str, object] = {
        "arm": pins["arm"],
        "baseline_evidence_cutoff": pins["baseline_evidence_cutoff"],
        "baseline_snapshot_id": pins["baseline_snapshot_id"],
        "capacity_digest": pins["approved_capacity_digest"],
        "decision_monotonic_raw_ns": pins["decision_monotonic_raw_ns"],
        "epoch0_digest": pins["epoch0_consensus_digest"],
        "epoch0_topology_digest": pins["epoch0_topology_digest"],
        "epoch_change_issuer_id": pins["epoch_change_issuer_id"],
        "hard_deadline_monotonic_raw_ns": pins[
            "hard_deadline_monotonic_raw_ns"
        ],
        "kind": "kauri-operator-capacity-consumption-v1",
        "label_issuer_id": pins["label_issuer_id"],
        "label_issuer_public_key_fingerprint": pins[
            "label_issuer_public_key_fingerprint"
        ],
        "label_issuer_reference": pins["label_issuer_reference"],
        "run_id": pins["run_id"],
        "schema_version": 1,
        "source_instance": pins["source_instance"],
        "stage_a_semantic_digest": stage_a_receipt[
            "envelope_canonical_digest"
        ],
        "stage_a_wire_sha256": _sha(stage_a),
        "stage_b_authorization_wire_sha256": _sha(stage_b),
        "successor_bundle_sha256": _sha(bundle),
        "successor_policy_snapshot_id": pins[
            "successor_policy_snapshot_id"
        ],
    }
    paths = {
        "stage_a_wire": tmp_path / "stage-a.wire",
        "stage_b_wire": tmp_path / "stage-b.wire",
        "successor_bundle": tmp_path / "successor.bundle",
        "consumption_record": tmp_path / "consumption.json",
        "stage_a_verifier_receipt": tmp_path / "stage-a-receipt.json",
        "stage_b_verifier_receipt": tmp_path / "stage-b-receipt.json",
    }
    paths["stage_a_wire"].write_bytes(stage_a)
    paths["stage_b_wire"].write_bytes(stage_b)
    paths["successor_bundle"].write_bytes(bundle)
    paths["consumption_record"].write_bytes(
        _canonical(record, audit._CONSUMPTION_KEYS)
    )
    paths["stage_a_verifier_receipt"].write_bytes(
        _canonical(stage_a_receipt, audit._STAGE_A_RECEIPT_KEYS)
    )
    paths["stage_b_verifier_receipt"].write_bytes(
        _canonical(stage_b_receipt, audit._STAGE_B_RECEIPT_KEYS)
    )
    return {
        "paths": paths,
        "pins": pins,
        "record": record,
        "stage_a_receipt": stage_a_receipt,
        "stage_b_receipt": stage_b_receipt,
    }


def _run(fixture: dict[str, object]):
    paths = fixture["paths"]
    assert isinstance(paths, dict)
    return audit.audit_consumption_chain(**paths, pins=fixture["pins"])


def _rewrite(
    fixture: dict[str, object], name: str, document: dict[str, object],
    order: tuple[str, ...],
) -> None:
    paths = fixture["paths"]
    assert isinstance(paths, dict)
    paths[name].write_bytes(_canonical(document, order))


def test_component_chain_binds_actual_wires_bundle_receipts_and_cpp_record(
    tmp_path: Path,
):
    fixture = _fixture(tmp_path)
    result = _run(fixture)
    assert result["verdict"] == "COMPONENT_CHAIN_VALID_NO_RAW_REPLAY"
    assert result["claim_eligible"] is False
    assert result["figure_eligible"] is False
    assert result["raw_replay_validated"] is False
    assert "native verifier tool identity" in result["claim_boundary"]
    assert "thesis claims remain unverified" in result["claim_boundary"]


@pytest.mark.parametrize(
    ("artifact", "replacement"),
    (
        ("stage_a_wire", b"changed-stage-a"),
        ("stage_b_wire", b"changed-stage-b"),
        ("successor_bundle", b"changed-bundle"),
    ),
)
def test_actual_artifact_hash_drift_is_rejected(
    tmp_path: Path, artifact: str, replacement: bytes,
):
    fixture = _fixture(tmp_path)
    fixture["paths"][artifact].write_bytes(replacement)
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)


@pytest.mark.parametrize(
    ("document_name", "field", "value"),
    (
        ("record", "baseline_snapshot_id", "c" * 64),
        ("stage_b_receipt", "baseline_snapshot_id", "c" * 64),
        ("record", "baseline_evidence_cutoff", 125),
        ("stage_b_receipt", "baseline_evidence_cutoff", 125),
        ("record", "decision_monotonic_raw_ns", 501),
        ("stage_b_receipt", "decision_monotonic_raw_ns", 501),
        ("record", "hard_deadline_monotonic_raw_ns", 499),
    ),
)
def test_snapshot_cutoff_decision_and_deadline_drift_is_rejected(
    tmp_path: Path, document_name: str, field: str, value: object,
):
    fixture = _fixture(tmp_path)
    document = fixture[document_name]
    assert isinstance(document, dict)
    document[field] = value
    if document_name == "record":
        _rewrite(
            fixture, "consumption_record", document, audit._CONSUMPTION_KEYS,
        )
    else:
        _rewrite(
            fixture, "stage_b_verifier_receipt", document,
            audit._STAGE_B_RECEIPT_KEYS,
        )
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)


@pytest.mark.parametrize(
    "pin",
    (
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
        "successor_policy_snapshot_id",
    ),
)
def test_independent_identity_pin_drift_is_rejected(tmp_path: Path, pin: str):
    fixture = _fixture(tmp_path)
    pins = copy.deepcopy(fixture["pins"])
    current = pins[pin]
    if pin == "arm":
        pins[pin] = "exact_copy_sham"
    elif type(current) is int:
        pins[pin] = current + 1
    elif isinstance(current, str) and len(current) in (40, 64):
        pins[pin] = ("0" if current[0] != "0" else "f") + current[1:]
    else:
        pins[pin] = str(current) + "-different"
    fixture["pins"] = pins
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)


def test_duplicate_and_extra_json_fields_are_rejected(tmp_path: Path):
    fixture = _fixture(tmp_path)
    record_path = fixture["paths"]["consumption_record"]
    raw = record_path.read_bytes()
    record_path.write_bytes(raw.replace(b'{"arm":', b'{"arm":"fast_priority_treatment","arm":', 1))
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)

    fixture = _fixture(tmp_path / "extra")
    receipt = fixture["stage_a_receipt"]
    assert isinstance(receipt, dict)
    receipt["unexpected"] = "forbidden"
    path = fixture["paths"]["stage_a_verifier_receipt"]
    path.write_bytes(json.dumps(receipt, separators=(",", ":")).encode() + b"\n")
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)


@pytest.mark.parametrize(
    ("document_name", "path_name", "order", "field", "value"),
    (
        (
            "stage_a_receipt", "stage_a_verifier_receipt",
            audit._STAGE_A_RECEIPT_KEYS, "kind", "counterfeit-stage-a",
        ),
        (
            "stage_a_receipt", "stage_a_verifier_receipt",
            audit._STAGE_A_RECEIPT_KEYS, "verdict", "VERIFIED",
        ),
        (
            "stage_a_receipt", "stage_a_verifier_receipt",
            audit._STAGE_A_RECEIPT_KEYS, "envelope_wire_sha256", "c" * 64,
        ),
        (
            "stage_b_receipt", "stage_b_verifier_receipt",
            audit._STAGE_B_RECEIPT_KEYS, "kind", "counterfeit-stage-b",
        ),
        (
            "stage_b_receipt", "stage_b_verifier_receipt",
            audit._STAGE_B_RECEIPT_KEYS, "verdict", "VERIFIED",
        ),
        (
            "stage_b_receipt", "stage_b_verifier_receipt",
            audit._STAGE_B_RECEIPT_KEYS, "authorization_wire_sha256", "c" * 64,
        ),
    ),
)
def test_counterfeit_receipt_identity_or_wire_binding_is_rejected(
    tmp_path: Path,
    document_name: str,
    path_name: str,
    order: tuple[str, ...],
    field: str,
    value: object,
):
    fixture = _fixture(tmp_path)
    receipt = fixture[document_name]
    assert isinstance(receipt, dict)
    receipt[field] = value
    _rewrite(fixture, path_name, receipt, order)
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)


def test_noncanonical_record_and_receipt_bytes_are_rejected(tmp_path: Path):
    fixture = _fixture(tmp_path)
    path = fixture["paths"]["consumption_record"]
    value = fixture["record"]
    path.write_bytes(json.dumps(value, sort_keys=True).encode() + b"\n")
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)


@pytest.mark.parametrize("schema_version", (True, 1.0))
def test_non_integer_schema_versions_are_rejected(
    tmp_path: Path, schema_version: object,
):
    fixture = _fixture(tmp_path)
    receipt = fixture["stage_a_receipt"]
    assert isinstance(receipt, dict)
    receipt["schema_version"] = schema_version
    _rewrite(
        fixture, "stage_a_verifier_receipt", receipt,
        audit._STAGE_A_RECEIPT_KEYS,
    )
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)


def test_symlink_and_nonregular_inputs_are_rejected(tmp_path: Path):
    fixture = _fixture(tmp_path)
    actual = fixture["paths"]["stage_a_wire"]
    link = tmp_path / "stage-a-link.wire"
    link.symlink_to(actual)
    fixture["paths"]["stage_a_wire"] = link
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)

    fixture = _fixture(tmp_path / "nonregular")
    directory = tmp_path / "wire-directory"
    directory.mkdir()
    fixture["paths"]["stage_b_wire"] = directory
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)


def test_semantic_and_tree_file_receipt_cross_bindings_are_rejected(
    tmp_path: Path,
):
    fixture = _fixture(tmp_path)
    record = fixture["record"]
    assert isinstance(record, dict)
    record["stage_a_semantic_digest"] = "c" * 64
    _rewrite(fixture, "consumption_record", record, audit._CONSUMPTION_KEYS)
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)

    fixture = _fixture(tmp_path / "tree-drift")
    receipt = fixture["stage_b_receipt"]
    assert isinstance(receipt, dict)
    receipt["epoch0_tree_file_sha256"] = "c" * 64
    _rewrite(
        fixture, "stage_b_verifier_receipt", receipt,
        audit._STAGE_B_RECEIPT_KEYS,
    )
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)


def test_stage_a_verification_must_precede_the_live_decision(tmp_path: Path):
    fixture = _fixture(tmp_path)
    receipt = fixture["stage_a_receipt"]
    assert isinstance(receipt, dict)
    receipt["verification_monotonic_raw_ns"] = 501
    _rewrite(
        fixture, "stage_a_verifier_receipt", receipt,
        audit._STAGE_A_RECEIPT_KEYS,
    )
    with pytest.raises(audit.ConsumptionAuditError):
        _run(fixture)
