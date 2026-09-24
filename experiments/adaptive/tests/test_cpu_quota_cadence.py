"""Standalone 31-scope CPU-quota monitor cadence gate."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from experiments.adaptive.kauri_experiment import cpu_quota
from experiments.adaptive.kauri_experiment import cpu_quota_cadence as cadence

PROFILE_ROOT = Path(__file__).parents[1] / "profiles"


def _contract() -> cpu_quota.CpuQuotaContract:
    return cpu_quota.load_cpu_quota_contract(
        PROFILE_ROOT / "n31-cpu-quota-heterogeneity-smoke-v1.json",
        base_profile_path=PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v13.json",
        expected_replica_ids=tuple(range(31)),
    )


def _evidence(
    *, gap_ns: int
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    contract = _contract()
    timestamps = (1_000_000_000, 1_000_000_000 + gap_ns, 1_000_000_000 + gap_ns * 2)
    samples = [
        {
            "replica_id": replica_id,
            "source_monotonic_ns": timestamp,
            "active_state": "active",
            "cpu_quota_percent": contract.quota_percent(replica_id),
            "cpu_quota_per_second_usec": contract.quota_percent(replica_id) * 10_000,
        }
        for timestamp in timestamps
        for replica_id in contract.replica_ids
    ]
    rounds = [
        {
            "round_ordinal": ordinal,
            "duration_ns": 20_000_000,
            "completion_overrun_ns": 0,
        }
        for ordinal in range(3)
    ]
    return samples, rounds


def test_evaluation_accepts_exact_31_scope_cadence_at_the_frozen_bound() -> None:
    contract = _contract()
    samples, rounds = _evidence(gap_ns=2_000_000_000)

    result = cadence.evaluate_cadence(
        contract, samples, rounds, minimum_samples_per_replica=3
    )

    assert result["verdict"] == "PASS"
    assert result["replica_count"] == 31
    assert result["samples_per_replica"] == 3
    assert result["maximum_gap_ms"] == 2_000.0


def test_evaluation_rejects_one_gap_above_the_frozen_bound() -> None:
    contract = _contract()
    samples, rounds = _evidence(gap_ns=2_000_000_001)

    result = cadence.evaluate_cadence(
        contract, samples, rounds, minimum_samples_per_replica=3
    )

    assert result["verdict"] == "FAIL"
    assert "frozen tolerance" in str(result["reason"])


def test_evaluation_rejects_missing_replica_coverage() -> None:
    contract = _contract()
    samples, rounds = _evidence(gap_ns=1_000_000_000)
    samples = [row for row in samples if row["replica_id"] != 30]

    result = cadence.evaluate_cadence(
        contract, samples, rounds, minimum_samples_per_replica=3
    )

    assert result["verdict"] == "FAIL"
    assert "coverage" in str(result["reason"])


def test_evaluation_accepts_exact_crash_transition_without_relaxing_cadence() -> None:
    contract = _contract()
    samples, rounds = _evidence(gap_ns=1_000_000_000)
    crashed = (21, 22, 23)
    for sample in samples:
        if (
            sample["replica_id"] in crashed
            and sample["source_monotonic_ns"] > 1_000_000_000
        ):
            sample["active_state"] = "inactive"
            sample["sub_state"] = "dead"
            sample["cpu_quota_per_second_usec"] = 0

    result = cadence.evaluate_cadence(
        contract,
        samples,
        rounds,
        minimum_samples_per_replica=3,
        crashed_replica_ids=crashed,
    )

    assert result["verdict"] == "PASS"
    assert result["maximum_gap_ms"] == 1_000.0


def test_evaluation_rejects_reactivated_scope() -> None:
    contract = _contract()
    samples, rounds = _evidence(gap_ns=1_000_000_000)
    samples[31 + 21]["active_state"] = "inactive"
    samples[31 + 21]["sub_state"] = "dead"
    samples[31 + 21]["cpu_quota_per_second_usec"] = 0

    result = cadence.evaluate_cadence(
        contract,
        samples,
        rounds,
        minimum_samples_per_replica=3,
        crashed_replica_ids=(21, 22, 23),
    )

    assert result["verdict"] == "FAIL"
    assert result["reason"] == "cadence active sample drifted"


def test_plan_rejects_short_or_relaxed_gate() -> None:
    plan = cadence.CadencePlan()

    for changes in (
        {"sample_seconds": 9},
        {"worker_seconds": 30},
        {"precrash_seconds": 30},
        {"maximum_gap_multiplier": 3},
    ):
        with pytest.raises(cadence.CadenceGateError):
            replace(plan, **changes)
