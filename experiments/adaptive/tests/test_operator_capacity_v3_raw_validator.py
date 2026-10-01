from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kauri_experiment import operator_capacity_v3_raw_validator as subject
from tests.test_operator_capacity_v3_backend import _fixture as _materialized_fixture


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")) .encode("ascii") + b"\n")


def _receipt() -> dict[str, object]:
    return {"schema_version": 1, "kind": "kauri-n31-operator-capacity-v3-local-shakedown-receipt-v1",
            "verdict": "PROCESS_COMPLETED_PENDING_RAW_VALIDATION", "claim_eligible": False,
            "figure_eligible": False, "automatic_retries": 0, "execution_request_sha256": "a" * 64,
            "manager_exit_code": 0, "failure": None, "cleanup": {},
            "manager_exit_code_after_cleanup": 0, "manager_success_terminal_verified": True,
            "fresh_native_stage_a_receipt_sha256": "b" * 64, "raw_validation_required": True,
            "e1_measurement_window": None}


def test_missing_independent_authority_is_honestly_incomplete(tmp_path: Path) -> None:
    _write(tmp_path / "runtime/local-shakedown-receipt.json", _receipt())
    result = subject.validate_operator_capacity_v3_raw(tmp_path)
    assert result["verdict"] == "INCOMPLETE"
    assert result["claim_eligible"] is False and result["figure_eligible"] is False
    assert result["complete_common_commit_count"] == 0
    assert "independent raw-validation authority" in str(result["detail"])


def test_receipt_cannot_bypass_no_retry_or_success_terminal(tmp_path: Path) -> None:
    receipt = _receipt(); receipt["automatic_retries"] = 1
    _write(tmp_path / "runtime/local-shakedown-receipt.json", receipt)
    result = subject.validate_operator_capacity_v3_raw(tmp_path)
    assert result["verdict"] == "INCOMPLETE"
    assert "no-retry" in str(result["detail"])


def test_authority_requires_hashes_for_manager_and_every_replica(tmp_path: Path) -> None:
    receipt = _receipt(); _write(tmp_path / "runtime/local-shakedown-receipt.json", receipt)
    raw = (tmp_path / "runtime/local-shakedown-receipt.json").read_bytes()
    authority = tmp_path.parent / "external-authority.json"
    _write(authority, {
        "schema_version": 1, "kind": "kauri-n31-operator-capacity-v3-raw-validation-authority-v1",
        "runner_receipt_sha256": subject._sha(raw), "materialization_manifest_sha256": "c" * 64,
        "event_stream_sha256": {"manager": "d" * 64},
        "cpu_quota_contract_sha256": "1" * 64, "cpu_quota_launch_sha256": "2" * 64,
        "cpu_quota_frozen_contract_sha256": "3" * 64,
        "cpu_quota_samples_sha256": "e" * 64,
        "cpu_quota_rounds_sha256": "f" * 64, "stage_a_verifier_receipt": "runtime/a.json",
        "stage_b_verifier_receipt": "runtime/b.json", "pins": {}})
    result = subject.validate_operator_capacity_v3_raw(tmp_path, authority_path=authority)
    assert result["verdict"] == "INCOMPLETE"
    assert "manager plus 31 streams" in str(result["detail"])


def test_runner_owned_authority_path_cannot_support_a_result(tmp_path: Path) -> None:
    _write(tmp_path / "runtime/local-shakedown-receipt.json", _receipt())
    _write(tmp_path / "runtime/raw-validation-authority.json", {})
    result = subject.validate_operator_capacity_v3_raw(
        tmp_path, authority_path=tmp_path / "runtime/raw-validation-authority.json",
    )
    assert result["verdict"] == "INCOMPLETE"
    assert "outside runner-owned" in str(result["detail"])


def test_raw_identity_gate_passes_source_revision_to_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    observed: dict[str, object] = {}
    def accepted(**kwargs: object) -> str:
        observed.update(kwargs)
        return "a" * 64
    monkeypatch.setattr(subject.backend, "validate_materialized_public_identity", accepted)
    manifest = {"identity_parity_receipt_sha256": "b" * 64,
                "public_identity_fingerprint": "c" * 64,
                "revision": "d" * 40}
    assert subject._validate_materialized_identity_for_raw(
        root=tmp_path, manager_argv=["manager"], manifest=manifest) == "a" * 64
    assert observed["source_revision"] == "d" * 40


def test_raw_rejects_retained_manager_transition_mutation(tmp_path: Path) -> None:
    root, manager, _replicas, _quota = _materialized_fixture(tmp_path)
    manifest = json.loads((root / "materialization-manifest.json").read_text(encoding="ascii"))
    transition_index = manager.index("--transition-request") + 1
    transition = json.loads(manager[transition_index])
    transition["apply_shape_selection"] = True
    manager[transition_index] = json.dumps(transition, sort_keys=True, separators=(",", ":"))
    manifest["manager_argv_sha256"] = subject.backend._argv_digest(manager)
    with pytest.raises(subject.RawValidationError, match="frozen W18 E0-to-E1 contract"):
        subject._validate_retained_manager_argv_for_raw(
            root=root, manager_argv=manager, manifest=manifest)


@pytest.mark.parametrize("field,value", [
    ("source_revision", "f" * 40), ("hotstuff_app_sha256", "f" * 64),
    ("main_config_sha256", "f" * 64), ("initial_beat_delay_ms", 1),
    ("beat_interval_ms", 51),
])
def test_raw_rejects_altered_complete_synthetic_workload_contract(
    tmp_path: Path, field: str, value: object,
) -> None:
    root, _manager, _replicas, _quota = _materialized_fixture(tmp_path)
    manifest = json.loads((root / "materialization-manifest.json").read_text(encoding="ascii"))
    manifest["synthetic_workload"][field] = value
    with pytest.raises(subject.RawValidationError, match="synthetic workload identity"):
        subject._validate_materialized_workload_for_raw(manifest)
