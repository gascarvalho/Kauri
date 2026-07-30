"""Tests for the bounded, experiment-only two-mode diagnosis oracle."""

from __future__ import annotations

from fractions import Fraction
import inspect
import json

import pytest

from experiments.adaptive.kauri_experiment.diagnosis import (
    BoundedTwoModeDiagnosis,
    DiagnosisCapacityError,
    DiagnosisContradictionError,
    DiagnosisError,
    DiagnosticObservation,
    MANAGER_EVIDENCE_PROJECTION_SCOPE,
    ManagerEvidenceProjectionError,
    project_manager_evidence_jsonl,
)


def _observation(
    attempt_id: str,
    reporter_id: int,
    target_id: int,
    outcome: str,
) -> DiagnosticObservation:
    return DiagnosticObservation(
        attempt_id=attempt_id,
        reporter_id=reporter_id,
        target_id=target_id,
        outcome=outcome,
    )


def _manager_record(
    *,
    observation_id: str,
    reporter_id: int,
    target_id: int,
    evidence_outcome: str,
) -> str:
    return json.dumps(
        {
            "event_schema_version": 1,
            "source_kind": "adaptation_manager",
            "event_type": "reputation.evidence_applied",
            "payload": {
                "observation_id": observation_id,
                "reporter_id": reporter_id,
                "target_id": target_id,
                "evidence_outcome": evidence_outcome,
                "reputation_outcome": evidence_outcome,
                "delta": -1 if evidence_outcome == "timeout" else 1,
                "resulting_score": 0,
            },
        },
        sort_keys=True,
    )


def test_n31_td3_has_the_exact_initial_bounded_hypothesis_count() -> None:
    diagnosis = BoundedTwoModeDiagnosis(range(31), 3)

    assert diagnosis.hypothesis_count == 37_883
    assert diagnosis.snapshot().compatible_hypothesis_count == 37_883


def test_hypothesis_and_mode_count_order_is_canonical() -> None:
    forwards = BoundedTwoModeDiagnosis((4, 1, 3, 0, 2), 2).snapshot()
    backwards = BoundedTwoModeDiagnosis((2, 0, 3, 1, 4), 2).snapshot()

    assert forwards.hypotheses == backwards.hypotheses
    assert forwards.mode_counts == backwards.mode_counts
    assert tuple(count.replica_id for count in forwards.mode_counts) == (
        0,
        1,
        2,
        3,
        4,
    )
    assert forwards.hypotheses == tuple(sorted(forwards.hypotheses))


def test_one_negative_preserves_false_reporter_or_omitter_ambiguity() -> None:
    diagnosis = BoundedTwoModeDiagnosis(range(4), 1)

    snapshot = diagnosis.observe(_observation("a", 1, 3, "timeout"))

    assert snapshot.compatible_hypothesis_count == 2
    assert {
        (
            hypothesis.false_reporters,
            hypothesis.persistent_omitters,
        )
        for hypothesis in snapshot.hypotheses
    } == {
        ((1,), ()),
        ((), (3,)),
    }
    assert snapshot.mode_count_for(1).false_reporter_mass == Fraction(1, 2)
    assert (
        snapshot.mode_count_for(3).persistent_omitter_mass
        == Fraction(1, 2)
    )
    assert snapshot.mode_count_for(1).definitive_mode is None
    assert snapshot.mode_count_for(3).definitive_mode is None


def test_positive_corroboration_identifies_the_false_reporter() -> None:
    diagnosis = BoundedTwoModeDiagnosis(range(4), 1)
    diagnosis.observe(_observation("negative", 1, 3, "timeout"))

    snapshot = diagnosis.observe(_observation("positive", 2, 3, "response"))

    assert snapshot.compatible_hypothesis_count == 1
    assert snapshot.hypotheses[0].false_reporters == (1,)
    assert snapshot.hypotheses[0].persistent_omitters == ()
    assert snapshot.mode_count_for(1).definitive_mode == "false_reporter"
    assert snapshot.mode_count_for(3).definitive_mode == "non_faulty"


@pytest.mark.parametrize("diagnostic_fault_bound", [1, 2])
def test_two_td_plus_one_distinct_reporters_identify_omission(
    diagnostic_fault_bound: int,
) -> None:
    target_id = 6
    reporter_ids = tuple(range((2 * diagnostic_fault_bound) + 1))
    diagnosis = BoundedTwoModeDiagnosis(range(7), diagnostic_fault_bound)

    snapshot = diagnosis.observe_many(
        _observation(
            f"timeout-{reporter_id}",
            reporter_id,
            target_id,
            "timeout",
        )
        for reporter_id in reporter_ids
    )

    target_counts = snapshot.mode_count_for(target_id)
    assert target_counts.persistent_omitter_hypotheses == (
        snapshot.compatible_hypothesis_count
    )
    assert target_counts.persistent_omitter_mass == Fraction(1, 1)
    assert target_counts.definitive_mode == "persistent_omitter"


def test_observation_order_does_not_change_the_result() -> None:
    observations = (
        _observation("a", 1, 4, "timeout"),
        _observation("b", 2, 4, "response"),
        _observation("c", 3, 0, "response"),
    )

    forwards = BoundedTwoModeDiagnosis(range(5), 2)
    backwards = BoundedTwoModeDiagnosis(range(5), 2)

    assert (
        forwards.observe_many(observations).hypotheses
        == backwards.observe_many(reversed(observations)).hypotheses
    )


@pytest.mark.parametrize(
    ("membership", "fault_bound", "message"),
    [
        ((), 0, "membership"),
        ((0, 1, 1), 1, "unique"),
        ((0, -1), 1, "non-negative"),
        ((0, True), 1, "integer"),
        ((0, 1), -1, "fault bound"),
        ((0, 1), 3, "membership"),
    ],
)
def test_invalid_membership_and_bound_fail_closed(
    membership: tuple[object, ...],
    fault_bound: int,
    message: str,
) -> None:
    with pytest.raises(DiagnosisError, match=message):
        BoundedTwoModeDiagnosis(membership, fault_bound)


@pytest.mark.parametrize(
    "observation",
    [
        _observation("unknown-reporter", 9, 1, "timeout"),
        _observation("unknown-target", 1, 9, "timeout"),
    ],
)
def test_unknown_observation_endpoints_fail_closed(
    observation: DiagnosticObservation,
) -> None:
    diagnosis = BoundedTwoModeDiagnosis(range(4), 1)

    with pytest.raises(DiagnosisError, match="membership"):
        diagnosis.observe(observation)


def test_self_observation_is_rejected() -> None:
    with pytest.raises(DiagnosisError, match="distinct"):
        _observation("self", 1, 1, "timeout")


def test_conflicting_duplicate_attempt_fails_closed_without_mutation() -> None:
    diagnosis = BoundedTwoModeDiagnosis(range(4), 1)
    accepted = diagnosis.observe(_observation("same", 1, 3, "timeout"))

    with pytest.raises(DiagnosisError, match="conflicting duplicate"):
        diagnosis.observe(_observation("same", 1, 3, "response"))

    assert diagnosis.snapshot() == accepted


def test_identical_duplicate_attempt_is_idempotent() -> None:
    diagnosis = BoundedTwoModeDiagnosis(range(4), 1)
    observation = _observation("same", 1, 3, "timeout")

    first = diagnosis.observe(observation)
    second = diagnosis.observe(observation)

    assert first == second
    assert second.observation_count == 1


def test_hypothesis_and_observation_capacities_fail_closed() -> None:
    with pytest.raises(DiagnosisCapacityError, match="31 replicas"):
        BoundedTwoModeDiagnosis(range(32), 1)

    with pytest.raises(DiagnosisCapacityError, match="cannot exceed 3"):
        BoundedTwoModeDiagnosis(range(7), 4)

    with pytest.raises(DiagnosisCapacityError, match="hypothesis capacity"):
        BoundedTwoModeDiagnosis(
            range(31),
            3,
            maximum_hypotheses=37_882,
        )

    diagnosis = BoundedTwoModeDiagnosis(
        range(4),
        1,
        maximum_observations=1,
    )
    accepted = diagnosis.observe(_observation("first", 1, 3, "timeout"))

    with pytest.raises(DiagnosisCapacityError, match="observation capacity"):
        diagnosis.observe(_observation("second", 2, 3, "response"))

    assert diagnosis.snapshot() == accepted


def test_contradictory_history_fails_closed_without_mutation() -> None:
    diagnosis = BoundedTwoModeDiagnosis(range(4), 1)
    diagnosis.observe(_observation("a", 1, 3, "timeout"))
    accepted = diagnosis.observe(_observation("b", 2, 3, "response"))

    with pytest.raises(DiagnosisContradictionError, match="no compatible"):
        diagnosis.observe(_observation("c", 2, 1, "timeout"))

    assert diagnosis.snapshot().observation_count == 2
    assert diagnosis.snapshot() == accepted


def test_mode_masses_are_exact_rational_counts_not_probability_labels() -> None:
    snapshot = BoundedTwoModeDiagnosis(range(4), 1).observe(
        _observation("a", 1, 3, "timeout")
    )
    counts = snapshot.mode_count_for(1)

    assert counts.compatible_hypotheses == 2
    assert counts.false_reporter_hypotheses == 1
    assert isinstance(counts.false_reporter_mass, Fraction)
    assert "probab" not in repr(counts).lower()


def test_manager_jsonl_projection_maps_accepted_outcomes_and_ignores_others() -> (
    None
):
    records = (
        json.dumps(
            {
                "event_schema_version": 1,
                "event_type": "block_committed",
                "payload": {},
            }
        ),
        _manager_record(
            observation_id="obs-timeout",
            reporter_id=1,
            target_id=3,
            evidence_outcome="timeout",
        ),
        _manager_record(
            observation_id="obs-on-time",
            reporter_id=3,
            target_id=2,
            evidence_outcome="on_time",
        ),
    )

    projected = project_manager_evidence_jsonl(
        records,
        membership=range(4),
    )

    assert projected == (
        _observation("obs-timeout", 1, 3, "timeout"),
        _observation("obs-on-time", 3, 2, "response"),
    )
    assert "development-only" in MANAGER_EVIDENCE_PROJECTION_SCOPE
    assert "tree/block context" in MANAGER_EVIDENCE_PROJECTION_SCOPE


def test_manager_projection_rejects_conflicts_invalid_records_and_capacity() -> (
    None
):
    timeout = _manager_record(
        observation_id="same",
        reporter_id=1,
        target_id=3,
        evidence_outcome="timeout",
    )
    response = _manager_record(
        observation_id="same",
        reporter_id=1,
        target_id=3,
        evidence_outcome="on_time",
    )

    with pytest.raises(
        ManagerEvidenceProjectionError,
        match="conflicting duplicate",
    ):
        project_manager_evidence_jsonl(
            (timeout, response),
            membership=range(4),
        )

    with pytest.raises(ManagerEvidenceProjectionError, match="JSON"):
        project_manager_evidence_jsonl(("{bad-json",), membership=range(4))

    with pytest.raises(ManagerEvidenceProjectionError, match="membership"):
        project_manager_evidence_jsonl(
            (
                _manager_record(
                    observation_id="unknown",
                    reporter_id=9,
                    target_id=3,
                    evidence_outcome="timeout",
                ),
            ),
            membership=range(4),
        )

    with pytest.raises(ManagerEvidenceProjectionError, match="record capacity"):
        project_manager_evidence_jsonl(
            (timeout, timeout),
            membership=range(4),
            maximum_records=1,
        )


def test_manager_projection_fails_closed_on_late_transition() -> None:
    timeout = _manager_record(
        observation_id="same",
        reporter_id=1,
        target_id=3,
        evidence_outcome="timeout",
    )
    late = _manager_record(
        observation_id="same",
        reporter_id=1,
        target_id=3,
        evidence_outcome="late",
    )

    with pytest.raises(
        ManagerEvidenceProjectionError,
        match="projection incomplete",
    ):
        project_manager_evidence_jsonl(
            (timeout, late),
            membership=range(4),
        )


def test_diagnosis_api_has_no_ground_truth_or_fault_label_input() -> None:
    public_callables = (
        BoundedTwoModeDiagnosis,
        BoundedTwoModeDiagnosis.observe,
        BoundedTwoModeDiagnosis.observe_many,
        project_manager_evidence_jsonl,
    )

    for callable_object in public_callables:
        parameter_names = inspect.signature(callable_object).parameters
        assert not any(
            "ground" in name or "truth" in name or "fault_label" in name
            for name in parameter_names
        )
