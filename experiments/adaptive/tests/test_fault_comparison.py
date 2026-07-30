"""Contract tests for the fixed N=7 fault-mode comparison.

This comparison is an evidence-wiring primitive, not a diagnosis or
statistical-analysis framework.  Each arm keeps its own canonical FI-Core
plan and journal, and the comparison summary accepts only three individually
validated runs bound to the same revision and matched Byzantine syndrome.
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
CRASH_REPLICA_ID = 1
FALSE_REPORTER_ID = 6
FALSE_REPORT_TARGET_ID = 1
PERSISTENT_OMITTER_ID = 1
DIAGNOSTIC_WINDOW = "diagnostic-window-1"


def _comparison_module() -> ModuleType:
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.comparison"
    )


def _comparison() -> Any:
    return _comparison_module().build_n7_comparison(
        kauri_revision=KAURI_REVISION,
        seed=SEED,
        crash_replica_id=CRASH_REPLICA_ID,
        false_reporter_id=FALSE_REPORTER_ID,
        false_report_target_id=FALSE_REPORT_TARGET_ID,
        persistent_omitter_id=PERSISTENT_OMITTER_ID,
        diagnostic_window=DIAGNOSTIC_WINDOW,
    )


def _verdicts(comparison: Any) -> dict[str, dict[str, object]]:
    verdicts: dict[str, dict[str, object]] = {}
    for arm in comparison.arms:
        run_id = f"run-{arm.name}"
        verdict: dict[str, object] = {
            "verdict": "PASS",
            "run_id": run_id,
            "kauri_revision": arm.kauri_revision,
            "fault_plan_sha256": arm.plan.sha256,
        }
        if arm.name == "sigkill_crash":
            verdict["action_observation"] = {
                "kind": "replica_group_sigkill"
            }
            verdict["manager_accepted_timeout_observation"] = None
        else:
            epoch_digest = "d" * 64
            block_hash = "b" * 64
            action_identity = {
                "static_authenticated_false_report": (
                    "false_timeout_emitted",
                    f"replica-{FALSE_REPORTER_ID}",
                ),
                "static_persistent_omission": (
                    "aggregate_omitted",
                    f"replica-{PERSISTENT_OMITTER_ID}",
                ),
            }[arm.name]
            verdict["action_observation"] = {
                "kind": action_identity[0],
                "source_id": action_identity[1],
                "block_hash": block_hash,
                "configuration": f"0:6:{epoch_digest}",
            }
            verdict["manager_accepted_timeout_observation"] = {
                "event_schema_version": 1,
                "run_id": run_id,
                "source_kind": "adaptation_manager",
                "source_id": "adaptive-manager",
                "source_instance": "manager-instance-1",
                "source_sequence": 9,
                "source_monotonic_ns": 1_000,
                "event_type": "evidence.observation_accepted",
                "payload": {
                    "ingestion_sequence": 7,
                    "observation": {
                        "schema_version": 1,
                        "observation_id": "c" * 64,
                        "reporter_id": FALSE_REPORTER_ID,
                        "observed_replica_id": FALSE_REPORT_TARGET_ID,
                        "configuration": {
                            "epoch_number": 0,
                            "tree_id": 6,
                            "epoch_digest": epoch_digest,
                        },
                        "block_hash": block_hash,
                        "expected_message_type": "aggregate_relay",
                        "outcome": "timeout",
                        "response_duration_us": 0,
                        "deadline_duration_us": 500_000,
                        "reporter_monotonic_ns": 900,
                        "reporter_sequence": 3,
                        "signer_set": [],
                    },
                },
            }
        verdicts[arm.name] = verdict
    return verdicts


def test_fixed_n7_comparison_has_exactly_three_equivalent_arms() -> None:
    comparison = _comparison()

    assert tuple(arm.name for arm in comparison.arms) == ARM_NAMES
    assert comparison.kauri_revision == KAURI_REVISION
    assert comparison.seed == SEED
    assert comparison.crash_replica_id == CRASH_REPLICA_ID
    assert comparison.false_reporter_id == FALSE_REPORTER_ID
    assert comparison.false_report_target_id == FALSE_REPORT_TARGET_ID
    assert comparison.persistent_omitter_id == PERSISTENT_OMITTER_ID
    assert comparison.diagnostic_fault_bound == 1

    assert tuple(
        arm.faulty_replica_id for arm in comparison.arms
    ) == (
        CRASH_REPLICA_ID,
        FALSE_REPORTER_ID,
        PERSISTENT_OMITTER_ID,
    )
    for arm in comparison.arms:
        assert arm.kauri_revision == KAURI_REVISION
        assert arm.plan.seed == SEED
        assert arm.plan.context.replica_ids == tuple(range(7))
        assert arm.plan.context.quorum == 5
        assert arm.plan.context.diagnostic_fault_bound == 1
        assert len(arm.plan.actions) == 1

    crash, false_report, omission = (
        arm.plan.actions[0] for arm in comparison.arms
    )
    assert type(crash).__name__ == "ReplicaGroupSigkill"
    assert crash.replica_id == CRASH_REPLICA_ID
    assert type(false_report).__name__ == "StaticAuthenticatedFalseReport"
    assert false_report.reporter_id == FALSE_REPORTER_ID
    assert false_report.target_id == FALSE_REPORT_TARGET_ID
    assert false_report.reported_outcome == "timeout"
    assert false_report.diagnostic_window == DIAGNOSTIC_WINDOW
    assert type(omission).__name__ == "StaticPersistentOmission"
    assert omission.replica_id == PERSISTENT_OMITTER_ID
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
    for arm, faulty_replica_id in (
        (false_report, FALSE_REPORTER_ID),
        (omission, PERSISTENT_OMITTER_ID),
    ):
        assert arm.plan.replica_cli_args(faulty_replica_id)
        assert all(
            arm.plan.replica_cli_args(replica_id) == ()
            for replica_id in range(7)
            if replica_id != faulty_replica_id
        )


def test_byzantine_arms_require_one_shared_initial_syndrome() -> None:
    module = _comparison_module()

    with pytest.raises(
        module.ComparisonError,
        match="same reporter-target syndrome",
    ):
        module.build_n7_comparison(
            kauri_revision=KAURI_REVISION,
            seed=SEED,
            crash_replica_id=CRASH_REPLICA_ID,
            false_reporter_id=FALSE_REPORTER_ID,
            false_report_target_id=FALSE_REPORT_TARGET_ID,
            persistent_omitter_id=5,
            diagnostic_window=DIAGNOSTIC_WINDOW,
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
        "fault_identities": {
            "crash_replica_id": CRASH_REPLICA_ID,
            "false_reporter_id": FALSE_REPORTER_ID,
            "false_report_target_id": FALSE_REPORT_TARGET_ID,
            "persistent_omitter_id": PERSISTENT_OMITTER_ID,
        },
        "initial_byzantine_syndrome": {
            "reporter_id": FALSE_REPORTER_ID,
            "target_id": FALSE_REPORT_TARGET_ID,
            "outcome": "timeout",
        },
        "diagnostic_fault_bound": 1,
        "arms": [
            {
                "name": arm.name,
                "run_id": f"run-{arm.name}",
                "verdict": "PASS",
                "fault_plan_sha256": arm.plan.sha256,
                "manager_accepted_observation_id": (
                    None if arm.name == "sigkill_crash" else "c" * 64
                ),
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
        ("reporter_id", 5),
        ("observed_replica_id", 2),
        ("outcome", "on_time"),
        ("block_hash", "e" * 64),
    ),
)
def test_summary_revalidates_matched_manager_timeout(
    field: str,
    value: object,
) -> None:
    module = _comparison_module()
    comparison = _comparison()
    verdicts = _verdicts(comparison)
    verdict = verdicts["static_authenticated_false_report"]
    accepted = verdict["manager_accepted_timeout_observation"]
    observation = accepted["payload"]["observation"]
    observation[field] = value

    with pytest.raises(
        module.ComparisonError,
        match="timeout|syndrome|evidence",
    ):
        module.summarize_comparison(comparison, verdicts)


def test_summary_revalidates_arm_specific_action_identity() -> None:
    module = _comparison_module()
    comparison = _comparison()
    verdicts = _verdicts(comparison)
    verdicts["static_persistent_omission"]["action_observation"][
        "kind"
    ] = "false_timeout_emitted"

    with pytest.raises(
        module.ComparisonError,
        match="timeout|syndrome|evidence",
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
