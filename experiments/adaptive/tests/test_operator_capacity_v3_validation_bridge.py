from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kauri_experiment import operator_capacity_v3_validation_bridge as subject


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n")


def _root(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    epoch_public_key = "02" + "a" * 64
    epoch_fingerprint = hashlib.sha256(bytes.fromhex(epoch_public_key)).hexdigest()
    stage_a = {
        "source_revision": "a" * 40, "arm": "fast_priority_treatment",
        "epoch0_consensus_digest": "b" * 64, "epoch0_topology_digest": "c" * 64,
        "approved_capacity_digest": "d" * 64, "issuer_id": 73,
        "issuer_reference": "label-authority", "issuer_public_key_fingerprint": "e" * 64,
    }
    stage_b = {
        "issuer_id": 1, "issuer_reference": "epoch-authority",
        "issuer_public_key_fingerprint": epoch_fingerprint,
        "baseline_snapshot_id": "f" * 64, "baseline_evidence_cutoff": 100,
        "decision_monotonic_raw_ns": 200,
    }
    consumption = {
        "run_id": "w18-unit", "source_instance": "unit-manager",
        "hard_deadline_monotonic_raw_ns": 300,
        "successor_policy_snapshot_id": "1" * 64,
        "baseline_snapshot_id": "f" * 64, "baseline_evidence_cutoff": 100,
        "decision_monotonic_raw_ns": 200,
    }
    config = (
        "epoch-change-issuer-id = 1\n"
        f"epoch-change-issuer-public-key = {epoch_public_key}\n"
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config/hotstuff.gen.conf").write_text(config, encoding="ascii")
    manager_argv = ["manager", "--operator-capacity-stage-b-issuer-reference", "epoch-authority"]
    _write(tmp_path / "runtime/manager-argv.json", manager_argv)
    _write(tmp_path / "materialization-manifest.json", {
        "revision": stage_a["source_revision"], "arm": "treatment",
        "stage_a_native_arm": stage_a["arm"], "stage_a_verifier_arguments": ["--fixture", "value"],
        "manager_argv_sha256": hashlib.sha256((json.dumps(manager_argv, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")).hexdigest(),
        "artifact_sha256": {"config/hotstuff.gen.conf": hashlib.sha256(config.encode("ascii")).hexdigest()},
    })
    _write(tmp_path / "runtime/stage-a-verifier-receipt.json", stage_a)
    _write(tmp_path / "runtime/stage-b-verifier-receipt.json", stage_b)
    _write(tmp_path / "raw/consumption.json", consumption)
    return tmp_path, {**stage_a, **stage_b, **consumption}


def test_derives_all_strict_pins_and_native_commands(tmp_path: Path) -> None:
    root, values = _root(tmp_path)
    inputs = subject.derive_validation_inputs(
        root, stage_a_verifier_binary=tmp_path / "stage-a",
        stage_b_verifier_binary=tmp_path / "stage-b")
    assert inputs.pins["source_revision"] == values["source_revision"]
    assert inputs.pins["epoch_change_issuer_reference"] == "epoch-authority"
    assert inputs.pins["run_id"] == "w18-unit"
    assert inputs.stage_a_command == (str((tmp_path / "stage-a").resolve()), "--fixture", "value")
    assert inputs.stage_b_command[0] == str((tmp_path / "stage-b").resolve())
    assert "--issuer-public-key-hex" in inputs.stage_b_command


def test_rejects_stage_b_key_not_matching_materialized_config(tmp_path: Path) -> None:
    root, _values = _root(tmp_path)
    stage_b = json.loads((root / "runtime/stage-b-verifier-receipt.json").read_text(encoding="ascii"))
    stage_b["issuer_public_key_fingerprint"] = "0" * 64
    _write(root / "runtime/stage-b-verifier-receipt.json", stage_b)
    with pytest.raises(subject.OperatorCapacityV3ValidationBridgeError, match="epoch issuer identity"):
        subject.derive_validation_inputs(
            root, stage_a_verifier_binary=tmp_path / "stage-a",
            stage_b_verifier_binary=tmp_path / "stage-b")


def test_derives_provisional_inputs_before_authority_creates_stage_b_receipt(tmp_path: Path) -> None:
    root, _values = _root(tmp_path)
    (root / "runtime/stage-b-verifier-receipt.json").unlink()
    inputs = subject.derive_validation_inputs(
        root, stage_a_verifier_binary=tmp_path / "stage-a",
        stage_b_verifier_binary=tmp_path / "stage-b")
    assert inputs.pins["baseline_snapshot_id"] == "f" * 64
    assert inputs.pins["epoch_change_issuer_reference"] == "epoch-authority"


def test_revalidator_rejects_authority_with_different_pins_before_native_execution(tmp_path: Path) -> None:
    root, _values = _root(tmp_path)
    with pytest.raises(subject.OperatorCapacityV3ValidationBridgeError, match="differ from the external authority pins"):
        subject.independently_recompute_verifiers(
            root, {"pins": {}}, stage_a_verifier_binary=tmp_path / "stage-a",
            stage_b_verifier_binary=tmp_path / "stage-b")


def test_pair_callback_preserves_missing_authority_as_an_incomplete_raw_result(tmp_path: Path) -> None:
    root, _values = _root(tmp_path)
    callback = subject.make_pair_revalidator(
        stage_a_verifier_binary=tmp_path / "stage-a",
        stage_b_verifier_binary=tmp_path / "stage-b")
    result = callback(root, tmp_path.parent / "missing-external-authority.json")
    assert result["verdict"] == "INCOMPLETE"
    assert result["claim_eligible"] is False


def test_pair_callback_converts_internal_callback_failure_to_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = subject.make_pair_revalidator(
        stage_a_verifier_binary=tmp_path / "stage-a",
        stage_b_verifier_binary=tmp_path / "stage-b")
    monkeypatch.setattr(subject.raw_validator, "validate_operator_capacity_v3_raw",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("recheck failed")))
    result = callback(tmp_path, tmp_path.parent / "authority.json")
    assert result["verdict"] == "INCOMPLETE"
    assert result["failure_code"] == "RAW_CONTRACT_INCOMPLETE"


def test_rejects_config_that_no_longer_matches_materialization_hash(tmp_path: Path) -> None:
    root, _values = _root(tmp_path)
    (root / "config/hotstuff.gen.conf").write_text("epoch-change-issuer-id = 1\n", encoding="ascii")
    with pytest.raises(subject.OperatorCapacityV3ValidationBridgeError, match="configuration differs"):
        subject.derive_validation_inputs(
            root, stage_a_verifier_binary=tmp_path / "stage-a",
            stage_b_verifier_binary=tmp_path / "stage-b")


def test_seals_external_incomplete_record_without_overwrite(tmp_path: Path) -> None:
    root, _values = _root(tmp_path)
    output = tmp_path.parent / "w18-validation-abort.json"
    result = subject.validate_completed_arm(
        root, authority_output=tmp_path.parent / "unused-authority.json",
        raw_validation_output=output, stage_a_verifier_binary=tmp_path / "stage-a",
        stage_b_verifier_binary=tmp_path / "stage-b")
    assert result["verdict"] == "INCOMPLETE"
    assert json.loads(output.read_text(encoding="ascii")) == result


def test_validation_cli_exits_nonzero_for_preserved_incomplete_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_operator_capacity_v3_validate as cli

    monkeypatch.setattr(cli.bridge, "validate_completed_arm",
                        lambda *_args, **_kwargs: {"verdict": "INCOMPLETE"})
    assert cli.main([
        "--root", str(tmp_path), "--authority-output", str(tmp_path / "authority.json"),
        "--raw-validation-output", str(tmp_path / "validation.json"),
        "--stage-a-verifier-binary", str(tmp_path / "stage-a"),
        "--stage-b-verifier-binary", str(tmp_path / "stage-b"),
    ]) == 1
