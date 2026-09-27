"""Focused contracts for the isolated W16 v7 campaign aggregation path."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest

from experiments.adaptive.tests.test_w16_cpu_campaign import (
    CAMPAIGN_ID, FORWARD, REVERSE, _campaign_root, _campaign_root_v3,
    _reseal, _write_json,
)


def _module():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.w16_campaign_validator_v7"
    )


def _v3_root(
    tmp_path: Path, *, order: tuple[str, ...], ordinal: int, block_index: int,
) -> Path:
    root = _campaign_root(tmp_path, order=order, ordinal=ordinal, block_index=block_index)
    receipt_path, authorization_path = root / "feasibility-receipt.json", root / "authorization.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    authorization["schema_version"] = 3
    authorization["kind"] = "kauri-w16-static-e0-campaign-authorization-v3"
    authorization["executor_receipt_schema"] = "kauri-n31-static-e0-local-executor-v3"
    authorization["cell_validator_version"] = 7
    authorization["process_cleanup_required"] = True
    _write_json(authorization_path, authorization)
    receipt["schema"] = "kauri-n31-static-e0-local-executor-v3"
    receipt["authorization_sha256"] = hashlib.sha256(authorization_path.read_bytes()).hexdigest()
    _write_json(receipt_path, receipt)
    _reseal(root)
    return root


def _mock_v7(root: Path) -> dict[str, object]:
    receipt = json.loads((root / "feasibility-receipt.json").read_text())
    authorization = json.loads((root / "authorization.json").read_text())
    raw_start, raw_end = int(receipt["started_raw_monotonic_ns"]), int(receipt["ended_raw_monotonic_ns"])
    window_start, window_end, transactions = raw_start + 2, raw_end - 2, 1_000_000
    return {
        "schema_version": 1, "kind": "kauri-w16-output-validation-v7", "verdict": "PASS",
        "claim_eligible": False, "figure_eligible": False, "evidence_class": "CPU_QUOTA_SINGLE_ARM",
        "run_id": receipt["run_id"], "revision": receipt["preflight"]["revision"], "arm": authorization["arm"],
        "quota": {"mode": authorization["quota_mode"]},
        "authorization": {key: authorization[key] for key in (
            "campaign_id", "block_index", "campaign_freeze_sha256", "block_id", "cell_ordinal")},
        "throughput": {"window_start_monotonic_ns": window_start, "window_end_monotonic_ns": window_end,
                       "transaction_count": transactions, "duration_ns": window_end - window_start,
                       "throughput_milli_tps": transactions * 1_000_000_000_000 // (window_end - window_start)},
        "native_lifecycle_span": {"start_monotonic_ns": raw_start + 1, "end_monotonic_ns": raw_end - 1},
        "producer_raw_clock_span": {"clock_id": "CLOCK_MONOTONIC_RAW", "start_monotonic_ns": raw_start, "end_monotonic_ns": raw_end},
        "process_cleanup": {"replica_count": 31, "sigint_count": 31, "sigterm_count": 0, "sigkill_count": 0, "all_returncodes_zero": True},
    }


def _blocks(tmp_path: Path) -> list[list[Path]]:
    return [[_v3_root(tmp_path / f"block-{block_index}", order=FORWARD if block_index % 2 else REVERSE,
                       ordinal=ordinal, block_index=block_index) for ordinal in range(1, 5)]
            for block_index in range(1, 7)]


def test_v7_aggregation_reads_one_real_validated_cell(tmp_path: Path) -> None:
    root = _campaign_root_v3(tmp_path)
    cell = _module()._one_cell(
        root, ordinal=3, expected_label="slow-roots:heterogeneous",
    )
    assert cell["label"] == "slow-roots:heterogeneous"
    assert cell["authorization_sha256"] == hashlib.sha256(
        (root / "authorization.json").read_bytes()
    ).hexdigest()


def test_v7_campaign_requires_six_v3_v7_counterbalanced_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, blocks = _module(), _blocks(tmp_path)
    results = {root.resolve(): _mock_v7(root) for block in blocks for root in block}
    monkeypatch.setattr(module, "validate_w16_output_v7", lambda root: results[Path(root).resolve()])

    accepted = module.validate_w16_cpu_campaign_v7(blocks)
    assert accepted["verdict"] == "PASS", accepted
    assert accepted["kind"] == "kauri-w16-cpu-campaign-validation-v7"
    assert accepted["campaign_id"] == CAMPAIGN_ID
    assert accepted["technical_improvement_gate_passed"] is False
    assert accepted["claim_eligible"] is False
    assert accepted["figure_eligible"] is False


def test_v7_campaign_uses_only_v7_and_rejects_v2_or_v6_mixing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, blocks = _module(), _blocks(tmp_path)
    results = {root.resolve(): _mock_v7(root) for block in blocks for root in block}
    calls: list[Path] = []

    def v7(root: Path) -> dict[str, object]:
        calls.append(Path(root).resolve())
        return results[Path(root).resolve()]

    monkeypatch.setattr(module, "validate_w16_output_v7", v7)
    assert module.validate_w16_campaign_block_v7(blocks[0])["verdict"] == "PASS"
    assert len(calls) == 4

    root = blocks[0][0]
    receipt_path, authorization_path = root / "feasibility-receipt.json", root / "authorization.json"
    receipt, authorization = json.loads(receipt_path.read_text()), json.loads(authorization_path.read_text())
    receipt["schema"] = "kauri-n31-static-e0-local-executor-v2"
    authorization["schema_version"], authorization["kind"] = 2, "kauri-w16-static-e0-campaign-authorization-v2"
    _write_json(authorization_path, authorization)
    receipt["authorization_sha256"] = hashlib.sha256(authorization_path.read_bytes()).hexdigest()
    _write_json(receipt_path, receipt)
    _reseal(root)

    rejected = module.validate_w16_campaign_block_v7(blocks[0])
    assert rejected["verdict"] != "PASS", rejected
    assert rejected["reason_code"] == "cell_contract"


def test_v7_campaign_keeps_frozen_positive_direction_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, blocks = _module(), _blocks(tmp_path)
    results = {root.resolve(): _mock_v7(root) for block in blocks for root in block}
    for block_index, block in enumerate(blocks, 1):
        for root in block:
            result = results[root.resolve()]
            label = f"{result['arm']}:{result['quota']['mode']}"
            count = 1_200_000 if label == "fast-roots:heterogeneous" and block_index != 5 else 1_000_000
            result["throughput"]["transaction_count"] = count
            result["throughput"]["throughput_milli_tps"] = count * 1_000_000_000_000 // result["throughput"]["duration_ns"]
    monkeypatch.setattr(module, "validate_w16_output_v7", lambda root: results[Path(root).resolve()])

    result = module.validate_w16_cpu_campaign_v7(blocks)
    assert result["verdict"] == "PASS", result
    assert result["technical_improvement_gate_passed"] is True
    assert result["direction_gate"]["positive_block_count"] == 5
