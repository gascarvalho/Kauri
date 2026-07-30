"""Contract tests for the fixed N=7 fault-mode comparison.

This comparison is an evidence-wiring primitive, not a diagnosis or
statistical-analysis framework.  Each arm keeps its own canonical FI-Core
plan and journal, and the comparison summary accepts only three individually
validated runs bound to the same revision and frozen fault identity.
"""

from __future__ import annotations

import importlib
import itertools
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ARM_NAMES = (
    "sigkill_crash",
    "static_authenticated_false_report",
    "static_persistent_omission",
)
KAURI_REVISION = "a" * 40
SEED = 1729
FAULTY_REPLICA_ID = 1
FALSE_REPORT_TARGET_ID = 4
DIAGNOSTIC_WINDOW = "diagnostic-window-1"


def _comparison_module() -> ModuleType:
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.comparison"
    )


def _comparison() -> Any:
    return _comparison_module().build_n7_comparison(
        kauri_revision=KAURI_REVISION,
        seed=SEED,
        faulty_replica_id=FAULTY_REPLICA_ID,
        false_report_target_id=FALSE_REPORT_TARGET_ID,
        diagnostic_window=DIAGNOSTIC_WINDOW,
    )


def _verdicts(comparison: Any) -> dict[str, dict[str, object]]:
    return {
        arm.name: {
            "verdict": "PASS",
            "run_id": f"run-{arm.name}",
            "kauri_revision": arm.kauri_revision,
            "fault_plan_sha256": arm.plan.sha256,
        }
        for arm in comparison.arms
    }


def test_fixed_n7_comparison_has_exactly_three_equivalent_arms() -> None:
    comparison = _comparison()

    assert tuple(arm.name for arm in comparison.arms) == ARM_NAMES
    assert comparison.kauri_revision == KAURI_REVISION
    assert comparison.seed == SEED
    assert comparison.faulty_replica_id == FAULTY_REPLICA_ID
    assert comparison.diagnostic_fault_bound == 1

    for arm in comparison.arms:
        assert arm.kauri_revision == KAURI_REVISION
        assert arm.faulty_replica_id == FAULTY_REPLICA_ID
        assert arm.plan.seed == SEED
        assert arm.plan.context.replica_ids == tuple(range(7))
        assert arm.plan.context.quorum == 5
        assert arm.plan.context.diagnostic_fault_bound == 1
        assert len(arm.plan.actions) == 1

    crash, false_report, omission = (
        arm.plan.actions[0] for arm in comparison.arms
    )
    assert type(crash).__name__ == "ReplicaGroupSigkill"
    assert crash.replica_id == FAULTY_REPLICA_ID
    assert type(false_report).__name__ == "StaticAuthenticatedFalseReport"
    assert false_report.reporter_id == FAULTY_REPLICA_ID
    assert false_report.target_id == FALSE_REPORT_TARGET_ID
    assert false_report.reported_outcome == "timeout"
    assert false_report.diagnostic_window == DIAGNOSTIC_WINDOW
    assert type(omission).__name__ == "StaticPersistentOmission"
    assert omission.replica_id == FAULTY_REPLICA_ID
    assert omission.diagnostic_window == DIAGNOSTIC_WINDOW


def test_diagnostic_cli_args_reach_only_the_faulty_replica() -> None:
    comparison = _comparison()
    crash, false_report, omission = comparison.arms

    assert crash.plan.manager_cli_args() == ()
    assert false_report.plan.manager_cli_args() == ()
    assert omission.plan.manager_cli_args() == ()

    assert all(
        crash.plan.replica_cli_args(replica_id) == ()
        for replica_id in range(7)
    )
    for arm in (false_report, omission):
        assert arm.plan.replica_cli_args(FAULTY_REPLICA_ID)
        assert all(
            arm.plan.replica_cli_args(replica_id) == ()
            for replica_id in range(7)
            if replica_id != FAULTY_REPLICA_ID
        )


def test_each_arm_persists_a_canonical_plan_and_matching_journal(
    tmp_path: Path,
) -> None:
    faults = importlib.import_module(
        "experiments.adaptive.kauri_experiment.faults"
    )
    comparison = _comparison()
    timestamps = itertools.count(100)

    for arm in comparison.arms:
        run_directory = tmp_path / arm.name
        run_directory.mkdir()
        with faults.FaultEvidence(
            run_directory=run_directory,
            plan=arm.plan,
            monotonic_ns=timestamps.__next__,
        ):
            pass

        plan_path = run_directory / "fault-plan.json"
        journal_path = (
            run_directory / "raw" / "fault-orchestrator.jsonl"
        )
        assert plan_path.read_text(encoding="utf-8") == (
            arm.plan.canonical_json()
        )
        assert arm.plan.canonical_json() == json.dumps(
            json.loads(arm.plan.canonical_json()),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

        events = [
            json.loads(line)
            for line in journal_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert len(events) == 1
        assert events[0]["fault_id"] == arm.plan.actions[0].fault_id
        assert events[0]["plan_sha256"] == arm.plan.sha256
        assert events[0]["lifecycle"] == "terminal"
        assert events[0]["outcome"] == {"status": "not_reached"}


def test_summary_accepts_only_complete_pass_bound_comparison() -> None:
    module = _comparison_module()
    comparison = _comparison()

    summary = module.summarize_comparison(
        comparison,
        _verdicts(comparison),
    )

    assert summary == {
        "schema_version": 1,
        "scenario": "n7-static-fault-comparison",
        "kauri_revision": KAURI_REVISION,
        "seed": SEED,
        "faulty_replica_id": FAULTY_REPLICA_ID,
        "diagnostic_fault_bound": 1,
        "arms": [
            {
                "name": arm.name,
                "run_id": f"run-{arm.name}",
                "verdict": "PASS",
                "fault_plan_sha256": arm.plan.sha256,
            }
            for arm in comparison.arms
        ],
    }


@pytest.mark.parametrize("missing_arm", ARM_NAMES)
def test_summary_refuses_a_missing_arm(missing_arm: str) -> None:
    module = _comparison_module()
    comparison = _comparison()
    verdicts = _verdicts(comparison)
    verdicts.pop(missing_arm)

    with pytest.raises(
        module.ComparisonError,
        match="missing|exactly three|arm",
    ):
        module.summarize_comparison(comparison, verdicts)


@pytest.mark.parametrize("verdict", ("FAIL", "INCOMPLETE"))
def test_summary_refuses_any_non_pass_arm(verdict: str) -> None:
    module = _comparison_module()
    comparison = _comparison()
    verdicts = _verdicts(comparison)
    verdicts["static_persistent_omission"]["verdict"] = verdict

    with pytest.raises(
        module.ComparisonError,
        match="PASS|failed|incomplete|verdict",
    ):
        module.summarize_comparison(comparison, verdicts)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("kauri_revision", "b" * 40),
        ("fault_plan_sha256", "0" * 64),
    ),
)
def test_summary_refuses_an_arm_not_bound_to_the_frozen_inputs(
    field: str,
    value: str,
) -> None:
    module = _comparison_module()
    comparison = _comparison()
    verdicts = _verdicts(comparison)
    verdicts["static_authenticated_false_report"][field] = value

    with pytest.raises(
        module.ComparisonError,
        match="revision|plan|bind|sha256",
    ):
        module.summarize_comparison(comparison, verdicts)
