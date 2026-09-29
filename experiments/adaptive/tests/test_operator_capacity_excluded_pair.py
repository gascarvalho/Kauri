from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kauri_experiment import operator_capacity_excluded_pair as subject
from kauri_experiment.operator_capacity_stage_a_preflight import REQUIRED_BINARIES
from tests.test_operator_capacity_raw_validation import _raw_fixture


def _write(path: Path, value: object) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    path.write_bytes(raw)
    return raw


def _authorities(tmp_path: Path, output: Path) -> dict[str, dict[str, Path | str]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    result: dict[str, dict[str, Path | str]] = {}
    binary_sha = {
        name: hashlib.sha256(f"binary:{name}".encode()).hexdigest()
        for name in REQUIRED_BINARIES
    }
    for arm, native_arm in (("sham", "exact_copy_sham"), ("treatment", "fast_priority_treatment")):
        arm_root = output / arm
        input_sha = {
            "epoch0_tree_file": "a" * 64,
            "capacity_snapshot_wire": "b" * 64,
            "stage_a_envelope_wire": hashlib.sha256(f"stage-a:{arm}".encode()).hexdigest(),
            "quota_profile": "c" * 64,
        }
        preflight = {
            "schema_version": 1, "kind": "kauri-n31-operator-capacity-stage-a-preflight-v1",
            "verdict": "PREFLIGHT_OK_NO_EXECUTION", "claim_eligible": False, "figure_eligible": False,
            "revision": "c" * 40, "protocol": {"N": 31, "Q": 21}, "arm": arm,
            "stage_a_native_arm": native_arm, "output_root": str(arm_root.resolve()),
            "binary_sha256": binary_sha, "input_sha256": input_sha,
            "native_verifier_receipt_sha256": hashlib.sha256(f"receipt:{arm}".encode()).hexdigest(),
            "tool_identity_approval_receipt_sha256": "d" * 64,
        }
        preflight_path = tmp_path / f"{arm}-preflight.json"; preflight_raw = _write(preflight_path, preflight)
        request = {
            "schema_version": 1, "kind": "kauri-n31-operator-capacity-stage-a-execution-request-v1",
            "verdict": "EXECUTION_AUTHORIZATION_REQUEST_REQUIRED", "claim_eligible": False, "figure_eligible": False,
            "preflight_sha256": hashlib.sha256(preflight_raw).hexdigest(), "revision": "c" * 40,
            "arm": arm, "output_root": str(arm_root.resolve()), "binary_sha256": binary_sha,
            "input_sha256": input_sha, "native_verifier_receipt_sha256": preflight["native_verifier_receipt_sha256"],
            "tool_identity_approval_receipt_sha256": preflight["tool_identity_approval_receipt_sha256"],
        }
        request_path = tmp_path / f"{arm}-request.json"; request_raw = _write(request_path, request)
        approval = {
            "schema_version": 1, "kind": "kauri-n31-operator-capacity-execution-approval-v1",
            "verdict": "EXTERNAL_EXECUTION_APPROVED", "request_sha256": hashlib.sha256(request_raw).hexdigest(),
            "approval_ref": "user-confirmation-20260929", "approved_at_utc": "2026-09-29T12:00:00Z",
        }
        approval_path = tmp_path / f"{arm}-approval.json"; approval_raw = _write(approval_path, approval)
        result[arm] = {"preflight": preflight_path, "request": request_path, "approval": approval_path,
                       "expected_approval_sha256": hashlib.sha256(approval_raw).hexdigest()}
    return result


def _rebind_arm(inputs: dict[str, dict[str, Path | str]], arm: str) -> None:
    item = inputs[arm]
    preflight_path = Path(item["preflight"])
    request_path = Path(item["request"])
    approval_path = Path(item["approval"])
    preflight = json.loads(preflight_path.read_text())
    preflight_raw = _write(preflight_path, preflight)
    request = json.loads(request_path.read_text())
    request["preflight_sha256"] = hashlib.sha256(preflight_raw).hexdigest()
    for key in ("revision", "arm", "output_root", "binary_sha256", "input_sha256",
                "native_verifier_receipt_sha256", "tool_identity_approval_receipt_sha256"):
        request[key] = preflight[key]
    request_raw = _write(request_path, request)
    approval = json.loads(approval_path.read_text())
    approval["request_sha256"] = hashlib.sha256(request_raw).hexdigest()
    approval_raw = _write(approval_path, approval)
    item["expected_approval_sha256"] = hashlib.sha256(approval_raw).hexdigest()


def test_pair_plan_binds_two_distinct_approved_arms_without_launch(tmp_path: Path) -> None:
    output = tmp_path / "pair"
    result = subject.prepare_excluded_pair(output_root=output, arm_inputs=_authorities(tmp_path, output), hard_timeout_s=720)
    assert result["verdict"] == "PAIR_AUTHORIZED_BACKEND_REQUIRED_NO_EXECUTION"
    assert result["schedule"] == ["sham", "treatment"]
    assert result["automatic_retries"] == 0
    assert result["launch_permitted"] is False
    assert not output.exists()


def test_pair_rejects_reused_approval_for_other_arm(tmp_path: Path) -> None:
    output = tmp_path / "pair"; inputs = _authorities(tmp_path, output)
    inputs["treatment"]["approval"] = inputs["sham"]["approval"]
    inputs["treatment"]["expected_approval_sha256"] = inputs["sham"]["expected_approval_sha256"]
    with pytest.raises(subject.OperatorCapacityExcludedPairError, match="treatment execution approval"):
        subject.prepare_excluded_pair(output_root=output, arm_inputs=inputs, hard_timeout_s=720)


def test_pair_rejects_incomplete_binary_identity_map(tmp_path: Path) -> None:
    output = tmp_path / "pair"; inputs = _authorities(tmp_path, output)
    path = Path(inputs["sham"]["preflight"]); document = json.loads(path.read_text())
    document["binary_sha256"].pop("keygen")
    _write(path, document); _rebind_arm(inputs, "sham")
    with pytest.raises(subject.OperatorCapacityExcludedPairError, match="binary map keys"):
        subject.prepare_excluded_pair(output_root=output, arm_inputs=inputs, hard_timeout_s=720)


def test_pair_rejects_request_duplicate_drift(tmp_path: Path) -> None:
    output = tmp_path / "pair"; inputs = _authorities(tmp_path, output)
    request_path = Path(inputs["treatment"]["request"]); request = json.loads(request_path.read_text())
    request["input_sha256"]["quota_profile"] = "0" * 64
    request_raw = _write(request_path, request)
    approval_path = Path(inputs["treatment"]["approval"]); approval = json.loads(approval_path.read_text())
    approval["request_sha256"] = hashlib.sha256(request_raw).hexdigest()
    approval_raw = _write(approval_path, approval)
    inputs["treatment"]["expected_approval_sha256"] = hashlib.sha256(approval_raw).hexdigest()
    with pytest.raises(subject.OperatorCapacityExcludedPairError, match="duplicates differ"):
        subject.prepare_excluded_pair(output_root=output, arm_inputs=inputs, hard_timeout_s=720)


@pytest.mark.parametrize("input_key", ("epoch0_tree_file", "capacity_snapshot_wire", "quota_profile"))
def test_pair_rejects_cross_arm_shared_input_drift(tmp_path: Path, input_key: str) -> None:
    output = tmp_path / "pair"; inputs = _authorities(tmp_path, output)
    path = Path(inputs["treatment"]["preflight"]); document = json.loads(path.read_text())
    document["input_sha256"][input_key] = "0" * 64
    _write(path, document); _rebind_arm(inputs, "treatment")
    with pytest.raises(subject.OperatorCapacityExcludedPairError, match="share frozen Epoch-0"):
        subject.prepare_excluded_pair(output_root=output, arm_inputs=inputs, hard_timeout_s=720)


def test_pair_rejects_shared_stage_a_envelope_or_wrong_native_arm(tmp_path: Path) -> None:
    output = tmp_path / "pair"; inputs = _authorities(tmp_path, output)
    path = Path(inputs["treatment"]["preflight"]); document = json.loads(path.read_text())
    sham = json.loads(Path(inputs["sham"]["preflight"]).read_text())
    document["input_sha256"]["stage_a_envelope_wire"] = sham["input_sha256"]["stage_a_envelope_wire"]
    _write(path, document); _rebind_arm(inputs, "treatment")
    with pytest.raises(subject.OperatorCapacityExcludedPairError, match="distinct Stage-A"):
        subject.prepare_excluded_pair(output_root=output, arm_inputs=inputs, hard_timeout_s=720)

    inputs = _authorities(tmp_path / "second", output)
    path = Path(inputs["treatment"]["preflight"]); document = json.loads(path.read_text())
    document["stage_a_native_arm"] = "exact_copy_sham"
    _write(path, document); _rebind_arm(inputs, "treatment")
    with pytest.raises(subject.OperatorCapacityExcludedPairError, match="exact W18"):
        subject.prepare_excluded_pair(output_root=output, arm_inputs=inputs, hard_timeout_s=720)


def test_pair_rejects_self_consistent_non_git_revision(tmp_path: Path) -> None:
    output = tmp_path / "pair"; inputs = _authorities(tmp_path, output)
    for arm in ("sham", "treatment"):
        preflight_path = inputs[arm]["preflight"]
        request_path = inputs[arm]["request"]
        approval_path = inputs[arm]["approval"]
        preflight = json.loads(preflight_path.read_text())
        preflight["revision"] = "not-a-git-object"
        preflight_raw = _write(preflight_path, preflight)
        request = json.loads(request_path.read_text())
        request["revision"] = preflight["revision"]
        request["preflight_sha256"] = hashlib.sha256(preflight_raw).hexdigest()
        request_raw = _write(request_path, request)
        approval = json.loads(approval_path.read_text())
        approval["request_sha256"] = hashlib.sha256(request_raw).hexdigest()
        approval_raw = _write(approval_path, approval)
        inputs[arm]["expected_approval_sha256"] = hashlib.sha256(approval_raw).hexdigest()
    with pytest.raises(subject.OperatorCapacityExcludedPairError, match="Git revision"):
        subject.prepare_excluded_pair(output_root=output, arm_inputs=inputs, hard_timeout_s=720)


def test_run_is_hard_blocked_without_manager_quota_backend() -> None:
    with pytest.raises(subject.OperatorCapacityExcludedPairError, match="launch is blocked"):
        subject.execution_not_implemented()


def test_cli_preflight_reports_plan_but_run_never_launches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "pair"; inputs = _authorities(tmp_path, output)
    cli = importlib.import_module("experiments.adaptive.run_operator_capacity_excluded_pair")
    argv = ["preflight", "--output", str(output)]
    for arm in ("sham", "treatment"):
        argv += [
            f"--{arm}-preflight", str(inputs[arm]["preflight"]),
            f"--{arm}-request", str(inputs[arm]["request"]),
            f"--{arm}-approval", str(inputs[arm]["approval"]),
            f"--{arm}-expected-approval-sha256", str(inputs[arm]["expected_approval_sha256"]),
        ]
    assert cli.main(argv) == 0
    assert "PAIR_AUTHORIZED_BACKEND_REQUIRED_NO_EXECUTION" in capsys.readouterr().out
    assert not output.exists()
    argv[0] = "run"
    assert cli.main(argv) == 2
    assert "launch is blocked" in capsys.readouterr().err


def test_terminal_gate_requires_explicit_manager_success_before_raw_validation(tmp_path: Path) -> None:
    fixture = _raw_fixture(tmp_path)
    terminal = fixture["root"] / "raw" / "manager-terminal.json"
    retained_terminal = terminal.read_bytes()
    terminal.unlink()
    with pytest.raises(subject.OperatorCapacityExcludedPairError, match="manager terminal"):
        subject.validate_terminal_arm(fixture["root"], authority_paths=fixture["authority_paths"], pins=fixture["pins"])
    terminal.write_bytes(retained_terminal)
    result = subject.validate_terminal_arm(fixture["root"], authority_paths=fixture["authority_paths"], pins=fixture["pins"])
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_VALID_NO_RAW_REPLAY_NO_CLAIM"
    failed = json.loads(retained_terminal)
    failed["manager_exit_status"] = 1
    _write(terminal, failed)
    with pytest.raises(subject.OperatorCapacityExcludedPairError, match="predeadline success"):
        subject.validate_terminal_arm(fixture["root"], authority_paths=fixture["authority_paths"], pins=fixture["pins"])
