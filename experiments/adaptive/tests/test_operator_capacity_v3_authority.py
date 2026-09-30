from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kauri_experiment import operator_capacity_v3_authority as subject


def _write(path: Path, value: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    path.write_bytes(raw)
    return raw


def _receipt() -> dict[str, object]:
    return {"schema_version": 1, "kind": "kauri-n31-operator-capacity-v3-local-shakedown-receipt-v1",
            "verdict": "PROCESS_COMPLETED_PENDING_RAW_VALIDATION", "claim_eligible": False,
            "figure_eligible": False, "automatic_retries": 0, "manager_exit_code": 0,
            "manager_exit_code_after_cleanup": 0, "manager_success_terminal_verified": True,
            "failure": None, "raw_validation_required": True,
            "execution_request_sha256": "a" * 64, "cleanup": {},
            "fresh_native_stage_a_receipt_sha256": "b" * 64,
            "e1_measurement_window": None}


def test_authority_requires_a_sealed_runner_receipt(tmp_path: Path) -> None:
    with pytest.raises(subject.OperatorCapacityV3AuthorityError, match="runner receipt"):
        subject.produce_operator_capacity_v3_authority(
            tmp_path, authority_path=tmp_path.parent / "authority.json", pins={"run_id": "test"},
            stage_a_command=(), stage_b_command=())


def test_authority_output_cannot_be_runner_owned(tmp_path: Path) -> None:
    _write(tmp_path / "runtime/local-shakedown-receipt.json", _receipt())
    with pytest.raises(subject.OperatorCapacityV3AuthorityError, match="outside runner-owned"):
        subject.produce_operator_capacity_v3_authority(
            tmp_path, authority_path=tmp_path / "runtime/authority.json", pins={},
            stage_a_command=(), stage_b_command=())


def test_native_stage_b_command_is_mandatory_after_seal(tmp_path: Path) -> None:
    _write(tmp_path / "runtime/local-shakedown-receipt.json", _receipt())
    _write(tmp_path / "materialization-manifest.json", {
        "verdict": "MATERIALIZED_NO_EXECUTION", "protocol": {"N": 31, "Q": 21, "tree_count": 21},
    })
    with pytest.raises(subject.OperatorCapacityV3AuthorityError, match="Stage-A verifier receipt"):
        subject.produce_operator_capacity_v3_authority(
            tmp_path, authority_path=tmp_path.parent / "authority.json", pins={"run_id": "test"},
            stage_a_command=("stage-a",), stage_b_command=None)


def test_native_verifier_rejects_retained_receipt_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    retained = _write(tmp_path / "runtime/stage-a-verifier-receipt.json", {"x": 1, "verification_monotonic_raw_ns": 1})
    def fake_run(command: tuple[str, ...], **_kwargs: object):
        output = Path(command[-1])
        _write(output, {"x": 2, "verification_monotonic_raw_ns": 2})
        return type("Done", (), {"returncode": 0})()
    with pytest.raises(subject.OperatorCapacityV3AuthorityError, match="differs from retained"):
        subject._rerun(("native-stage-a",), "Stage-A", retained, runner=fake_run)


def test_missing_native_stage_b_command_has_no_acceptance_path() -> None:
    with pytest.raises(subject.OperatorCapacityV3AuthorityError, match="explicit native Stage-B"):
        subject._rerun((), "Stage-B", b'{"receipt":"b"}\n', runner=lambda *_args, **_kwargs: None)


def test_missing_stage_b_receipt_requires_a_complete_bound_native_command(tmp_path: Path) -> None:
    with pytest.raises(subject.OperatorCapacityV3AuthorityError, match="complete native Stage-B"):
        subject._stage_b_command_is_bound(
            (), root=tmp_path, manifest={"revision": "a" * 40},
            pins={"epoch_change_issuer_id": 1, "epoch_change_issuer_reference": "issuer",
                  "epoch_change_issuer_public_key_fingerprint": "a" * 64,
                  "label_issuer_reference": "label", "approved_capacity_digest": "b" * 64,
                  "arm": "fast_priority_treatment"},
        )


def test_authority_reopens_all_raw_sources_and_pins_exact_validator_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "runtime/local-shakedown-receipt.json", _receipt())
    stage_a = _write(tmp_path / "runtime/stage-a-verifier-receipt.json", {
        "receipt": "a", "verification_monotonic_raw_ns": 1,
    })
    stage_b = _write(tmp_path / "runtime/stage-b-verifier-receipt.json", {"receipt": "b"})
    stage_a_binary = tmp_path / "stage-a-native"; stage_a_binary.write_bytes(b"stage-a")
    stage_b_binary = tmp_path / "stage-b-native"; stage_b_binary.write_bytes(b"stage-b")
    approval = _write(tmp_path / "runtime/tool-identity-approval.json", {
        "revision": "a" * 40, "binary_sha256": {
            "stage_a_envelope_verifier": hashlib.sha256(stage_a_binary.read_bytes()).hexdigest(),
            "stage_b_authorization_verifier": hashlib.sha256(stage_b_binary.read_bytes()).hexdigest(),
        },
    })
    _write(tmp_path / "materialization-manifest.json", {
        "verdict": "MATERIALIZED_NO_EXECUTION", "protocol": {"N": 31, "Q": 21, "tree_count": 21},
        "stage_a_verifier_receipt_sha256": hashlib.sha256(stage_a).hexdigest(),
        "stage_a_verifier_arguments": ["--fixture"], "tool_identity_approval_receipt_sha256": hashlib.sha256(approval).hexdigest(),
        "revision": "a" * 40,
    })
    for name in ("cpu-quota-samples.jsonl", "cpu-quota-monitor-rounds.jsonl"):
        (tmp_path / "raw" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "raw" / name).write_bytes(b"{}\n")
    _write(tmp_path / "raw/manager-events.jsonl", {
        "event_schema_version": 1, "run_id": "independent-test",
        "source_kind": "adaptation_manager", "source_id": "adaptive-manager",
    })
    for replica in range(31):
        _write(tmp_path / "raw" / f"replica-{replica}.jsonl", {
            "event_schema_version": 1, "run_id": "independent-test",
            "source_kind": "replica", "source_id": f"replica-{replica}",
        })
    _write(tmp_path / "runtime/cpu-quota-contract.json", {"contract": 1})
    _write(tmp_path / "runtime/frozen-cpu-quota-contract.json", {"contract": 1})
    _write(tmp_path / "runtime/cpu-quota-launch.json", {"launch": 1})
    monkeypatch.setattr(subject.consumption_audit, "audit_consumption_chain", lambda **_kwargs: {})
    def fake_run(command: tuple[str, ...], **_kwargs: object):
        target = Path(command[-1])
        target.write_bytes(stage_a if command[0] == str(stage_a_binary) else stage_b)
        return type("Done", (), {"returncode": 0})()
    external = tmp_path.parent / f"{tmp_path.name}-external-authority.json"
    pins = {"run_id": "independent-test", "epoch_change_issuer_id": 1,
            "epoch_change_issuer_reference": "epoch-issuer",
            "epoch_change_issuer_public_key_fingerprint": "c" * 64,
            "label_issuer_reference": "label-issuer", "approved_capacity_digest": "d" * 64,
            "arm": "fast_priority_treatment"}
    stage_b_command = (str(stage_b_binary), "--epoch0-tree-file", str(tmp_path / "config/epoch0.tree"),
                       "--stage-b-authorization-wire", str(tmp_path / "raw/stage-b-authorization.wire"),
                       "--issuer-id", "1", "--issuer-reference", "epoch-issuer",
                       "--issuer-public-key-hex", "a" * 66, "--issuer-public-key-fingerprint", "c" * 64,
                       "--label-issuer-reference", "label-issuer", "--approved-capacity-digest", "d" * 64,
                       "--arm", "fast_priority_treatment", "--source-revision", "a" * 40)
    authority = subject.produce_operator_capacity_v3_authority(
        tmp_path, authority_path=external, pins=pins,
        stage_a_command=(str(stage_a_binary), "--fixture"), stage_b_command=stage_b_command, native_runner=fake_run)
    assert set(authority) == {
        "schema_version", "kind", "runner_receipt_sha256", "materialization_manifest_sha256",
        "event_stream_sha256", "cpu_quota_samples_sha256", "cpu_quota_rounds_sha256",
        "cpu_quota_frozen_contract_sha256", "cpu_quota_contract_sha256", "cpu_quota_launch_sha256", "stage_a_verifier_receipt",
        "stage_b_verifier_receipt", "pins",
    }
    assert authority["kind"] == "kauri-n31-operator-capacity-v3-raw-validation-authority-v1"
    assert set(authority["event_stream_sha256"]) == {"manager", *{f"replica-{item}" for item in range(31)}}
    assert json.loads(external.read_text(encoding="ascii")) == authority
