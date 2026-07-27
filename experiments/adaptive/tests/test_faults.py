"""Contract tests for the reusable adaptive-experiment fault plan.

The tests deliberately import FI-Core inside each test.  That keeps the suite
collectable while the implementation is still absent and makes the initial
TDD failure point at the missing public package rather than at test syntax.
"""

from __future__ import annotations

import hashlib
import importlib
import itertools
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _faults() -> ModuleType:
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.faults"
    )


def _scenario(faults: ModuleType, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "replica_ids": tuple(range(7)),
        "quorum": 5,
        "crash_budget": 2,
        "successor_bundle_retry_limit": 3,
    }
    values.update(overrides)
    return faults.ScenarioContext(**values)


def _representative_plan(faults: ModuleType) -> Any:
    return faults.FaultPlan(
        context=_scenario(faults),
        seed=1729,
        actions=(
            faults.ReplicaGroupSigkill(
                fault_id="crash-replica-0",
                replica_id=0,
            ),
            faults.SuccessorBundleAttemptDrop(
                fault_id="drop-bundle-2-attempt-1",
                replica_id=2,
                attempt=1,
            ),
            faults.ActivationAckDrop(
                fault_id="drop-quorum-ack",
                accepted_activation_ordinal=5,
            ),
        ),
    )


def test_fault_plan_has_canonical_schema_v1_json_and_stable_sha256() -> None:
    faults = _faults()
    plan = _representative_plan(faults)
    expected_value = {
        "actions": [
            {
                "fault_id": "crash-replica-0",
                "kind": "replica_group_sigkill",
                "replica_id": 0,
            },
            {
                "attempt": 1,
                "fault_id": "drop-bundle-2-attempt-1",
                "kind": "successor_bundle_attempt_drop",
                "replica_id": 2,
            },
            {
                "accepted_activation_ordinal": 5,
                "fault_id": "drop-quorum-ack",
                "kind": "activation_ack_drop",
            },
        ],
        "scenario": {
            "crash_budget": 2,
            "quorum": 5,
            "replica_ids": list(range(7)),
            "successor_bundle_retry_limit": 3,
        },
        "schema_version": 1,
        "seed": 1729,
    }
    expected_json = json.dumps(
        expected_value,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert plan.canonical_json() == expected_json
    assert plan.sha256 == hashlib.sha256(expected_json.encode()).hexdigest()
    assert plan.canonical_json() == plan.canonical_json()
    assert plan.sha256 == plan.sha256


@pytest.mark.parametrize("fault_id", ["", " ", "\t"])
def test_fault_plan_rejects_empty_fault_ids(fault_id: str) -> None:
    faults = _faults()

    with pytest.raises(ValueError, match="fault.*id"):
        faults.FaultPlan(
            context=_scenario(faults),
            seed=1729,
            actions=(
                faults.ReplicaGroupSigkill(
                    fault_id=fault_id,
                    replica_id=0,
                ),
            ),
        )


def test_fault_plan_rejects_duplicate_fault_ids() -> None:
    faults = _faults()

    with pytest.raises(ValueError, match="duplicate.*fault.*id"):
        faults.FaultPlan(
            context=_scenario(faults),
            seed=1729,
            actions=(
                faults.ReplicaGroupSigkill(
                    fault_id="duplicate",
                    replica_id=0,
                ),
                faults.SuccessorBundleAttemptDrop(
                    fault_id="duplicate",
                    replica_id=2,
                    attempt=1,
                ),
            ),
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"replica_ids": ()}, "replica"),
        ({"replica_ids": (0, 1, 1)}, "replica"),
        ({"quorum": 0}, "quorum"),
        ({"quorum": 8}, "quorum"),
        ({"crash_budget": -1}, "crash"),
        ({"crash_budget": 3}, "crash"),
        ({"successor_bundle_retry_limit": 0}, "retry"),
    ],
)
def test_scenario_context_rejects_invalid_membership_and_limits(
    overrides: dict[str, Any],
    message: str,
) -> None:
    faults = _faults()

    with pytest.raises(ValueError, match=message):
        _scenario(faults, **overrides)


@pytest.mark.parametrize(
    "action_factory",
    [
        lambda faults: faults.ReplicaGroupSigkill(
            fault_id="unknown-crash",
            replica_id=7,
        ),
        lambda faults: faults.SuccessorBundleAttemptDrop(
            fault_id="unknown-bundle-recipient",
            replica_id=7,
            attempt=1,
        ),
    ],
)
def test_fault_plan_rejects_targets_outside_scenario_membership(
    action_factory: Any,
) -> None:
    faults = _faults()

    with pytest.raises(ValueError, match="replica"):
        faults.FaultPlan(
            context=_scenario(faults),
            seed=1729,
            actions=(action_factory(faults),),
        )


def test_fault_plan_enforces_scenario_crash_budget() -> None:
    faults = _faults()

    with pytest.raises(ValueError, match="crash.*budget"):
        faults.FaultPlan(
            context=_scenario(faults, crash_budget=1),
            seed=1729,
            actions=(
                faults.ReplicaGroupSigkill(
                    fault_id="crash-0",
                    replica_id=0,
                ),
                faults.ReplicaGroupSigkill(
                    fault_id="crash-1",
                    replica_id=1,
                ),
            ),
        )


@pytest.mark.parametrize("attempt", [0, 4])
def test_fault_plan_enforces_successor_bundle_retry_limit(
    attempt: int,
) -> None:
    faults = _faults()

    with pytest.raises(ValueError, match="attempt"):
        faults.FaultPlan(
            context=_scenario(faults),
            seed=1729,
            actions=(
                faults.SuccessorBundleAttemptDrop(
                    fault_id="invalid-attempt",
                    replica_id=2,
                    attempt=attempt,
                ),
            ),
        )


@pytest.mark.parametrize("ordinal", [1, 4, 6])
def test_activation_ack_drop_is_only_the_quorum_completing_ack(
    ordinal: int,
) -> None:
    faults = _faults()

    with pytest.raises(ValueError, match="quorum"):
        faults.FaultPlan(
            context=_scenario(faults),
            seed=1729,
            actions=(
                faults.ActivationAckDrop(
                    fault_id="wrong-ack",
                    accepted_activation_ordinal=ordinal,
                ),
            ),
        )


def test_manager_cli_arguments_use_existing_order_and_hide_crash_truth() -> None:
    faults = _faults()
    plan = faults.FaultPlan(
        context=_scenario(faults),
        seed=1729,
        actions=(
            faults.ActivationAckDrop(
                fault_id="ack-first-in-plan",
                accepted_activation_ordinal=5,
            ),
            faults.ReplicaGroupSigkill(
                fault_id="crash-middle-in-plan",
                replica_id=0,
            ),
            faults.SuccessorBundleAttemptDrop(
                fault_id="bundle-last-in-plan",
                replica_id=2,
                attempt=1,
            ),
        ),
    )

    assert plan.manager_cli_args() == (
        "--experiment-drop-bundle-attempt",
        "2:1",
        "--experiment-drop-activation-ack",
        "5",
    )
    assert all(
        "crash" not in argument
        and "sigkill" not in argument
        and "crash-middle-in-plan" not in argument
        for argument in plan.manager_cli_args()
    )


def test_empty_fault_plan_adds_no_manager_arguments() -> None:
    faults = _faults()
    plan = faults.FaultPlan(
        context=_scenario(faults),
        seed=1729,
        actions=(),
    )

    assert plan.manager_cli_args() == ()


def test_fault_journal_flushes_a_contiguous_readable_lifecycle(
    tmp_path: Path,
) -> None:
    faults = _faults()
    plan = _representative_plan(faults)
    timestamps = iter((100, 200))
    path = tmp_path / "faults.jsonl"

    with faults.FaultJournal(
        path=path,
        plan_sha256=plan.sha256,
        monotonic_ns=lambda: next(timestamps),
    ) as journal:
        journal.append(
            fault_id="crash-replica-0",
            lifecycle="started",
        )

        partial = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(partial) == 1
        assert partial[0]["source_sequence"] == 0

        journal.append(
            fault_id="crash-replica-0",
            lifecycle="terminal",
            outcome={
                "status": "succeeded",
                "returncode": -9,
            },
        )

    events = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["schema_version"] for event in events] == [1, 1]
    assert [event["source_sequence"] for event in events] == [0, 1]
    assert [event["source_monotonic_ns"] for event in events] == [100, 200]
    assert [event["plan_sha256"] for event in events] == [
        plan.sha256,
        plan.sha256,
    ]
    assert [event["fault_id"] for event in events] == [
        "crash-replica-0",
        "crash-replica-0",
    ]
    assert [event["lifecycle"] for event in events] == [
        "started",
        "terminal",
    ]
    assert "outcome" not in events[0]
    assert events[1]["outcome"] == {
        "returncode": -9,
        "status": "succeeded",
    }


def test_fault_journal_rejects_a_non_monotonic_timestamp_without_corruption(
    tmp_path: Path,
) -> None:
    faults = _faults()
    plan = _representative_plan(faults)
    timestamps = iter((200, 100))
    path = tmp_path / "faults.jsonl"

    with faults.FaultJournal(
        path=path,
        plan_sha256=plan.sha256,
        monotonic_ns=lambda: next(timestamps),
    ) as journal:
        journal.append(
            fault_id="crash-replica-0",
            lifecycle="started",
        )
        with pytest.raises(ValueError, match="monotonic"):
            journal.append(
                fault_id="crash-replica-0",
                lifecycle="terminal",
                outcome={"status": "failed"},
            )

    remaining = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(remaining) == 1
    assert remaining[0]["source_sequence"] == 0


def test_fault_lifecycle_finalizes_every_unstarted_action_once(
    tmp_path: Path,
) -> None:
    faults = _faults()
    plan = _representative_plan(faults)
    path = tmp_path / "faults.jsonl"

    with faults.FaultJournal(
        path=path,
        plan_sha256=plan.sha256,
        monotonic_ns=itertools.count(100).__next__,
    ) as journal:
        lifecycle = faults.FaultLifecycle(plan, journal)
        lifecycle.finalize()
        lifecycle.finalize()

    events = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["fault_id"] for event in events] == [
        action.fault_id for action in plan.actions
    ]
    assert all(event["lifecycle"] == "terminal" for event in events)
    assert all(
        event["outcome"] == {"status": "not_reached"}
        for event in events
    )
    assert len(events) == len(plan.actions)


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    (
        (OSError("injection I/O failed"), "injection I/O failed"),
        (KeyboardInterrupt(), "KeyboardInterrupt"),
    ),
)
def test_fault_lifecycle_closes_started_failure_and_untouched_actions(
    tmp_path: Path,
    failure: BaseException,
    expected_error: str,
) -> None:
    faults = _faults()
    plan = _representative_plan(faults)
    path = tmp_path / "faults.jsonl"
    started = plan.actions[0].fault_id

    with faults.FaultJournal(
        path=path,
        plan_sha256=plan.sha256,
        monotonic_ns=itertools.count(100).__next__,
    ) as journal:
        lifecycle = faults.FaultLifecycle(plan, journal)
        lifecycle.start(started)
        try:
            raise failure
        except BaseException as exc:
            message = str(exc) or type(exc).__name__
            lifecycle.terminal(
                started,
                "failed",
                {"error": message},
            )
        finally:
            lifecycle.finalize()
            lifecycle.finalize()

    events = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    terminals = [
        event for event in events if event["lifecycle"] == "terminal"
    ]
    assert [event["fault_id"] for event in terminals] == [
        action.fault_id for action in plan.actions
    ]
    assert terminals[0]["outcome"] == {
        "error": expected_error,
        "status": "failed",
    }
    assert [
        event["outcome"]["status"] for event in terminals[1:]
    ] == ["not_reached", "not_reached"]
    assert len(terminals) == len(plan.actions)


def test_fault_evidence_binds_plan_journal_and_lifecycle_on_failure(
    tmp_path: Path,
) -> None:
    faults = _faults()
    plan = _representative_plan(faults)

    with pytest.raises(OSError, match="runtime inputs failed"):
        with faults.FaultEvidence(
            run_directory=tmp_path,
            plan=plan,
            monotonic_ns=itertools.count(100).__next__,
        ) as lifecycle:
            assert isinstance(lifecycle, faults.FaultLifecycle)
            raise OSError("runtime inputs failed")

    plan_path = tmp_path / "fault-plan.json"
    journal_path = tmp_path / "raw" / "fault-orchestrator.jsonl"
    assert plan_path.read_bytes() == plan.canonical_json().encode("utf-8")
    events = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["fault_id"] for event in events] == [
        action.fault_id for action in plan.actions
    ]
    assert all(event["lifecycle"] == "terminal" for event in events)
    assert all(
        event["outcome"]["status"] == "not_reached" for event in events
    )
    assert {event["plan_sha256"] for event in events} == {plan.sha256}


def test_fault_evidence_rolls_back_plan_if_matching_journal_cannot_open(
    tmp_path: Path,
) -> None:
    faults = _faults()
    plan = _representative_plan(faults)
    journal_path = tmp_path / "raw" / "fault-orchestrator.jsonl"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text("occupied\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        with faults.FaultEvidence(
            run_directory=tmp_path,
            plan=plan,
        ):
            raise AssertionError("unreachable")

    assert not (tmp_path / "fault-plan.json").exists()
    assert journal_path.read_text(encoding="utf-8") == "occupied\n"
