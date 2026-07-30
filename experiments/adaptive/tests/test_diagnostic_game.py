"""Contract tests for bounded active diagnostic probe selection."""

from __future__ import annotations

from itertools import product
import inspect

import pytest

from experiments.adaptive.kauri_experiment.diagnosis import (
    BoundedTwoModeDiagnosis,
    DiagnosisError,
    DiagnosticObservation,
)
from experiments.adaptive.kauri_experiment.diagnostic_game import (
    DiagnosticProbe,
    choose_minimax_probe,
    robust_safe_replicas,
    score_probe,
    target_omission_status,
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


def _initial_ambiguity():
    diagnosis = BoundedTwoModeDiagnosis(range(7), 1)
    snapshot = diagnosis.observe(
        _observation("initial-timeout", 6, 1, "timeout")
    )
    return diagnosis, snapshot


def test_minimax_prefers_a_fresh_witness_over_repeating_the_accuser() -> None:
    _, snapshot = _initial_ambiguity()
    repeated = DiagnosticProbe(reporter_id=6, target_id=1)
    fresh = DiagnosticProbe(reporter_id=0, target_id=1)

    repeated_score = score_probe(snapshot, repeated)
    fresh_score = score_probe(snapshot, fresh)

    assert snapshot.compatible_hypothesis_count == 2
    assert repeated_score.worst_case_surviving_hypotheses == 2
    assert repeated_score.guaranteed_hypothesis_elimination == 0
    assert fresh_score.worst_case_surviving_hypotheses == 1
    assert fresh_score.guaranteed_hypothesis_elimination == 1
    assert choose_minimax_probe(snapshot, (repeated, fresh)) == fresh


@pytest.mark.parametrize("outcome", ["response", "timeout"])
def test_fresh_witness_resolves_blame_and_recovers_one_safe_candidate(
    outcome: str,
) -> None:
    diagnosis, before = _initial_ambiguity()
    before_safe = robust_safe_replicas(before)

    after = diagnosis.observe(
        _observation("fresh-witness", 0, 1, outcome)
    )

    assert after.compatible_hypothesis_count == 1
    assert target_omission_status(after, 1) == (
        "persistent_omitter"
        if outcome == "timeout"
        else "not_persistent_omitter"
    )
    assert len(robust_safe_replicas(after)) == len(before_safe) + 1


@pytest.mark.parametrize("diagnostic_fault_bound", [1, 2, 3])
def test_two_d_distinct_reporters_always_determine_target_status(
    diagnostic_fault_bound: int,
) -> None:
    reporter_count = 2 * diagnostic_fault_bound
    target_id = reporter_count

    for pattern_number, outcomes in enumerate(
        product(("response", "timeout"), repeat=reporter_count)
    ):
        diagnosis = BoundedTwoModeDiagnosis(
            range(reporter_count + 1),
            diagnostic_fault_bound,
        )
        snapshot = diagnosis.observe_many(
            _observation(
                f"pattern-{pattern_number}-reporter-{reporter_id}",
                reporter_id,
                target_id,
                outcome,
            )
            for reporter_id, outcome in enumerate(outcomes)
        )

        timeout_count = outcomes.count("timeout")
        expected_status = (
            "persistent_omitter"
            if timeout_count > diagnostic_fault_bound
            else "not_persistent_omitter"
        )
        assert target_omission_status(snapshot, target_id) == (
            expected_status
        )


@pytest.mark.parametrize("diagnostic_fault_bound", [1, 2, 3])
def test_two_d_minus_one_boundary_pattern_remains_ambiguous(
    diagnostic_fault_bound: int,
) -> None:
    reporter_count = (2 * diagnostic_fault_bound) - 1
    target_id = reporter_count
    outcomes = (
        ("timeout",) * diagnostic_fault_bound
        + ("response",) * (diagnostic_fault_bound - 1)
    )
    diagnosis = BoundedTwoModeDiagnosis(
        range(reporter_count + 1),
        diagnostic_fault_bound,
    )

    snapshot = diagnosis.observe_many(
        _observation(
            f"reporter-{reporter_id}",
            reporter_id,
            target_id,
            outcome,
        )
        for reporter_id, outcome in enumerate(outcomes)
    )

    assert target_omission_status(snapshot, target_id) == "ambiguous"
    assert any(
        target_id in hypothesis.persistent_omitters
        for hypothesis in snapshot.hypotheses
    )
    assert any(
        target_id not in hypothesis.persistent_omitters
        for hypothesis in snapshot.hypotheses
    )


def test_minimax_tie_break_is_canonical_not_input_order_dependent() -> None:
    _, snapshot = _initial_ambiguity()
    probes = (
        DiagnosticProbe(reporter_id=5, target_id=1),
        DiagnosticProbe(reporter_id=2, target_id=1),
        DiagnosticProbe(reporter_id=0, target_id=1),
    )

    assert choose_minimax_probe(snapshot, probes) == DiagnosticProbe(0, 1)
    assert choose_minimax_probe(snapshot, reversed(probes)) == (
        DiagnosticProbe(0, 1)
    )


def test_minimax_avoids_a_fresh_reporter_already_implicated_elsewhere() -> (
    None
):
    diagnosis = BoundedTwoModeDiagnosis(range(5), 2)
    snapshot = diagnosis.observe_many(
        (
            _observation("first-edge", 0, 1, "timeout"),
            _observation("second-edge", 1, 2, "timeout"),
        )
    )
    lowest_fresh = DiagnosticProbe(reporter_id=0, target_id=2)
    independent_fresh = DiagnosticProbe(reporter_id=3, target_id=2)

    assert score_probe(
        snapshot,
        lowest_fresh,
    ).worst_case_surviving_hypotheses == 3
    assert score_probe(
        snapshot,
        independent_fresh,
    ).worst_case_surviving_hypotheses == 2
    assert choose_minimax_probe(
        snapshot,
        (lowest_fresh, independent_fresh),
    ) == independent_fresh


def test_public_selection_api_has_no_ground_truth_fault_inputs() -> None:
    assert tuple(inspect.signature(DiagnosticProbe).parameters) == (
        "reporter_id",
        "target_id",
    )
    assert tuple(inspect.signature(score_probe).parameters) == (
        "snapshot",
        "probe",
        "role_values",
    )
    assert tuple(inspect.signature(choose_minimax_probe).parameters) == (
        "snapshot",
        "probes",
        "role_values",
    )
    assert tuple(inspect.signature(robust_safe_replicas).parameters) == (
        "snapshot",
    )
    assert tuple(inspect.signature(target_omission_status).parameters) == (
        "snapshot",
        "target_id",
    )


def test_repeated_timeouts_do_not_create_independent_witnesses() -> None:
    diagnosis, initial = _initial_ambiguity()

    repeated = diagnosis.observe(
        _observation("repeat-timeout", 6, 1, "timeout")
    )

    assert repeated.compatible_hypothesis_count == (
        initial.compatible_hypothesis_count
    )
    assert target_omission_status(repeated, 1) == "ambiguous"
    repeat_score = score_probe(
        repeated,
        DiagnosticProbe(reporter_id=6, target_id=1),
    )
    assert repeat_score.worst_case_surviving_hypotheses == 2
    assert repeat_score.guaranteed_hypothesis_elimination == 0


def test_fabricated_snapshot_cannot_inject_a_fault_label() -> None:
    diagnosis, initial = _initial_ambiguity()
    resolved = diagnosis.observe(
        _observation("fresh-response", 0, 1, "response")
    )
    fabricated = type(initial)(
        hypotheses=resolved.hypotheses,
        observations=initial.observations,
        mode_counts=resolved.mode_counts,
    )

    with pytest.raises(DiagnosisError, match="reproducible"):
        score_probe(
            fabricated,
            DiagnosticProbe(reporter_id=0, target_id=1),
        )
