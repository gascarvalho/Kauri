"""Contracts for the read-only W16 four-cell block validator."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from experiments.adaptive.kauri_experiment import w16_block_validator as validator
from experiments.adaptive.tests.test_w16_output_validator import (
    _build_output as _build_cell_output,
    _reseal as _reseal_cell_output,
)


ORDER = (
    ("slow-roots", "homogeneous"),
    ("fast-roots", "homogeneous"),
    ("slow-roots", "heterogeneous"),
    ("fast-roots", "heterogeneous"),
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _roots(tmp_path: Path) -> list[Path]:
    roots: list[Path] = []
    binary_sha256 = {
        name: hashlib.sha256(name.encode()).hexdigest()
        for name in ("app", "keygen", "tls_keygen", "native_digest")
    }
    block_order = [f"{arm}:{mode}" for arm, mode in ORDER]
    for ordinal, (arm, mode) in enumerate(ORDER, 1):
        root = tmp_path / f"cell-{ordinal}"
        root.mkdir()
        run_id = f"w16-run-{ordinal}"
        preflight = {
            "revision": "a" * 40,
            "profile_sha256": "b" * 64,
            "binary_sha256": binary_sha256,
        }
        _write_json(root / "feasibility-receipt.json", {
            "schema": "kauri-n31-static-e0-local-executor-v1",
            "run_id": run_id,
            "attempts": 1,
            "retries": 0,
            "required_complete_cycles": 5,
            "started_monotonic_ns": ordinal * 1_000_000_000,
            "ended_monotonic_ns": ordinal * 1_000_000_000 + 900_000_000,
            "preflight": preflight,
        })
        _write_json(root / "authorization.json", {
            "block_id": "w16-static-e0-test-block",
            "block_order": block_order,
            "cell_ordinal": ordinal,
            "revision": preflight["revision"],
            "profile_sha256": preflight["profile_sha256"],
            "binary_sha256": binary_sha256,
            "arm": arm,
            "quota_mode": mode,
            "required_complete_cycles": 5,
            "hard_timeout_s": 480,
            "external_timeout_s": 720,
            "automatic_retries": 0,
            "claim_eligible": False,
            "figure_eligible": False,
        })
        roots.append(root)
    return roots


def _cell_results(roots: list[Path], throughputs: tuple[int, int, int, int]) -> dict[Path, dict[str, object]]:
    return {
        root.resolve(): {
            "schema_version": 1,
            "kind": "kauri-w16-output-validation-v2",
            "verdict": "PASS",
            "evidence_class": "CPU_QUOTA_SINGLE_ARM",
            "claim_eligible": False,
            "figure_eligible": False,
            "run_id": f"w16-run-{ordinal}",
            "revision": "a" * 40,
            "arm": arm,
            "quota": {"mode": mode},
            "authorization": {
                "block_id": "w16-static-e0-test-block",
                "cell_ordinal": ordinal,
            },
            "throughput": {
                "transaction_count": throughput,
                "duration_ns": 1_000_000_000_000,
                "throughput_milli_tps": throughput,
            },
        }
        for ordinal, (root, (arm, mode), throughput) in enumerate(
            zip(roots, ORDER, throughputs, strict=True), 1
        )
    }


def _install_cell_validator(
    monkeypatch: pytest.MonkeyPatch,
    results: dict[Path, dict[str, object]],
) -> list[Path]:
    calls: list[Path] = []

    def fake(root: Path) -> dict[str, object]:
        resolved = Path(root).resolve()
        calls.append(resolved)
        return results[resolved]

    monkeypatch.setattr(validator, "validate_w16_output", fake)
    return calls


def _replace_run_id(root: Path, run_id: str, ordinal: int) -> None:
    old = "w16-local-test"
    for path in sorted((root / "config").glob("replica-*.conf")):
        path.write_text(path.read_text().replace(old, run_id), encoding="utf-8")
    for path in sorted((root / "raw").glob("replica-*.jsonl")):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            row["run_id"] = run_id
            row["source_instance"] = str(row["source_instance"]).replace(old, run_id)
        path.write_text(
            "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in rows
            ),
            encoding="utf-8",
        )
    receipt_path = root / "feasibility-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["run_id"] = run_id
    receipt["started_monotonic_ns"] = ordinal * 10_000_000_000
    receipt["ended_monotonic_ns"] = ordinal * 10_000_000_000 + 9_000_000_000
    _write_json(receipt_path, receipt)
    _reseal_cell_output(root)


def test_real_v2_validator_accepts_complete_four_cell_fixture(tmp_path: Path) -> None:
    roots: list[Path] = []
    for ordinal, (arm, mode) in enumerate(ORDER, 1):
        root = _build_cell_output(
            tmp_path / f"sealed-{ordinal}", cpu_mode=mode, arm=arm,
        )
        _replace_run_id(root, f"w16-integrated-{ordinal}", ordinal)
        roots.append(root)

    result = validator.validate_w16_block(roots)

    assert result["verdict"] == "PASS", result
    assert len(result["cells"]) == 4
    assert result["positive_mechanism"] is False


def test_complete_block_computes_only_predeclared_direction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    results = _cell_results(roots, (1_000_000, 1_010_000, 800_000, 1_200_000))
    calls = _install_cell_validator(monkeypatch, results)

    result = validator.validate_w16_block(roots)

    assert result["verdict"] == "PASS", result
    assert result["kind"] == "kauri-w16-four-cell-block-validation-v1"
    assert result["claim_eligible"] is False
    assert result["figure_eligible"] is False
    assert result["campaign_claim"] is False
    assert result["positive_mechanism"] is True
    assert result["effects"]["homogeneous_ratio"] == pytest.approx(1.01)
    assert result["effects"]["heterogeneous_ratio"] == pytest.approx(1.5)
    assert result["effects"]["log_interaction"] == pytest.approx(
        math.log(1.5) - math.log(1.01)
    )
    assert calls == [root.resolve() for root in roots]


def test_exact_rates_detect_positive_direction_when_displayed_milli_tps_ties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    results = _cell_results(roots, (1, 1, 1, 1))
    results[roots[3].resolve()]["throughput"].update({
        "transaction_count": 1,
        "duration_ns": 999_999_999_999,
        "throughput_milli_tps": 1,
    })
    _install_cell_validator(monkeypatch, results)

    result = validator.validate_w16_block(roots)

    assert result["verdict"] == "PASS", result
    assert [cell["throughput_milli_tps"] for cell in result["cells"]] == [1, 1, 1, 1]
    assert result["positive_mechanism"] is True
    assert result["effects"]["heterogeneous_ratio"] > 1
    assert result["effects"]["log_interaction"] > 0
    assert result["effects"]["interaction_exact"] == {
        "numerator": 1_000_000_000_000,
        "denominator": 999_999_999_999,
    }


def test_rejects_displayed_milli_tps_not_derived_from_transactions_and_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    results = _cell_results(roots, (1_000_000, 1_010_000, 800_000, 1_200_000))
    results[roots[1].resolve()]["throughput"]["throughput_milli_tps"] += 1
    _install_cell_validator(monkeypatch, results)

    result = validator.validate_w16_block(roots)

    assert result["verdict"] == "INCOMPLETE"
    assert result["reason_code"] == "cell_contract"
    assert "effects" not in result


def test_complete_negative_direction_is_still_a_valid_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    results = _cell_results(roots, (1_000_000, 1_100_000, 1_000_000, 1_050_000))
    _install_cell_validator(monkeypatch, results)

    result = validator.validate_w16_block(roots)

    assert result["verdict"] == "PASS", result
    assert result["positive_mechanism"] is False
    assert result["effects"]["heterogeneous_ratio"] > 1
    assert result["effects"]["log_interaction"] < 0


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    (
        ("duplicate-run", "duplicate_run_id"),
        ("revision-drift", "cross_cell_identity"),
        ("binary-drift", "cross_cell_identity"),
        ("profile-drift", "cross_cell_identity"),
        ("block-drift", "cross_cell_identity"),
        ("cycle-drift", "cell_contract"),
        ("timeout-drift", "cell_contract"),
        ("retry-drift", "cell_contract"),
    ),
)
def test_rejects_cross_cell_or_contract_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    reason_code: str,
) -> None:
    roots = _roots(tmp_path)
    results = _cell_results(roots, (1_000_000, 1_010_000, 800_000, 1_200_000))
    _install_cell_validator(monkeypatch, results)
    receipt_path = roots[3] / "feasibility-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    authorization_path = roots[3] / "authorization.json"
    authorization = json.loads(authorization_path.read_text())
    if mutation == "duplicate-run":
        receipt["run_id"] = "w16-run-1"
        results[roots[3].resolve()]["run_id"] = "w16-run-1"
    elif mutation == "revision-drift":
        receipt["preflight"]["revision"] = "c" * 40
        authorization["revision"] = "c" * 40
        results[roots[3].resolve()]["revision"] = "c" * 40
    elif mutation == "binary-drift":
        receipt["preflight"]["binary_sha256"]["app"] = "c" * 64
        authorization["binary_sha256"]["app"] = "c" * 64
    elif mutation == "profile-drift":
        receipt["preflight"]["profile_sha256"] = "c" * 64
        authorization["profile_sha256"] = "c" * 64
    elif mutation == "block-drift":
        authorization["block_id"] = "w16-static-e0-other-block"
        results[roots[3].resolve()]["authorization"]["block_id"] = (
            "w16-static-e0-other-block"
        )
    elif mutation == "cycle-drift":
        receipt["required_complete_cycles"] = 4
        authorization["required_complete_cycles"] = 4
    elif mutation == "timeout-drift":
        authorization["hard_timeout_s"] = 300
        authorization["external_timeout_s"] = 540
    elif mutation == "retry-drift":
        receipt["retries"] = 1
        authorization["automatic_retries"] = 1
    _write_json(receipt_path, receipt)
    _write_json(authorization_path, authorization)

    result = validator.validate_w16_block(roots)

    assert result["verdict"] == "INCOMPLETE"
    assert result["reason_code"] == reason_code
    assert "effects" not in result


def test_rejects_wrong_root_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    results = _cell_results(roots, (1_000_000, 1_010_000, 800_000, 1_200_000))
    _install_cell_validator(monkeypatch, results)

    result = validator.validate_w16_block([roots[1], roots[0], roots[2], roots[3]])

    assert result["verdict"] == "INCOMPLETE"
    assert result["reason_code"] == "cell_order"


def test_rejects_overlapping_or_out_of_order_cell_chronology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    results = _cell_results(roots, (1_000_000, 1_010_000, 800_000, 1_200_000))
    _install_cell_validator(monkeypatch, results)
    receipt_path = roots[2] / "feasibility-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    previous = json.loads((roots[1] / "feasibility-receipt.json").read_text())
    receipt["started_monotonic_ns"] = previous["ended_monotonic_ns"]
    _write_json(receipt_path, receipt)

    result = validator.validate_w16_block(roots)

    assert result["verdict"] == "INCOMPLETE"
    assert result["reason_code"] == "cell_chronology"
    assert "effects" not in result


@pytest.mark.parametrize("cell_verdict", ("FAIL", "INCOMPLETE"))
def test_propagates_nonpass_cell_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cell_verdict: str,
) -> None:
    roots = _roots(tmp_path)
    results = _cell_results(roots, (1_000_000, 1_010_000, 800_000, 1_200_000))
    results[roots[2].resolve()].update({
        "verdict": cell_verdict,
        "reason_code": "producer_abort",
        "detail": "fixture nonpass",
    })
    calls = _install_cell_validator(monkeypatch, results)

    result = validator.validate_w16_block(roots)

    assert result["verdict"] == cell_verdict
    assert result["reason_code"] == "cell_validation"
    assert len(calls) == 3
    assert "effects" not in result


def test_rejects_cell_validator_version_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    results = _cell_results(roots, (1_000_000, 1_010_000, 800_000, 1_200_000))
    results[roots[0].resolve()]["kind"] = "kauri-w16-output-validation-v3"
    _install_cell_validator(monkeypatch, results)

    result = validator.validate_w16_block(roots)

    assert result["verdict"] == "INCOMPLETE"
    assert result["reason_code"] == "cell_validation_contract"
