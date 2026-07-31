"""Proof tests for finite-horizon passive Kauri reconfiguration."""

from __future__ import annotations

import pytest

from experiments.adaptive.kauri_experiment.passive_reconfiguration_evaluation import (
    run_passive_reconfiguration_game_evaluation,
    run_two_epoch_separation_evaluation,
)
from experiments.adaptive.kauri_experiment.passive_reconfiguration_game import (
    MAXIMUM_LOOKAHEAD_HORIZON,
    advance_compatible_hypotheses,
    indistinguishable_hypothesis_pairs,
    select_lookahead_topology,
)
from experiments.adaptive.kauri_experiment.robust_topology import (
    JointFaultHypothesis,
    KauriTreeCandidate,
    PassiveOutcomeSupport,
    RobustTopologyError,
)


def _hypothesis(
    hypothesis_id: str,
    replica_id: int,
) -> JointFaultHypothesis:
    return JointFaultHypothesis(
        hypothesis_id=hypothesis_id,
        faulty_replicas=frozenset({replica_id}),
    )


def _support(
    hypothesis_id: str,
    *outcomes: str,
) -> PassiveOutcomeSupport:
    return PassiveOutcomeSupport(
        hypothesis_id=hypothesis_id,
        outcomes=frozenset(outcomes),
    )


def test_two_epoch_policy_beats_the_unique_one_step_greedy_action() -> None:
    result = run_two_epoch_separation_evaluation()

    assert result["replica_count"] == 31
    assert result["fanout"] == 5
    assert result["tree_levels"] == 3
    assert result["internal_replicas_per_candidate"] == 6
    assert result["leaf_replicas_per_candidate"] == 25
    assert result["compatible_singleton_fault_hypotheses"] == 9
    assert result["candidate_topologies"] == 4
    assert result["all_suspects_are_leaves_in_every_candidate"] is True
    assert set(result["worst_case_exposure_by_candidate"].values()) == {1}
    assert set(result["predicted_latency_us_by_candidate"].values()) == {10}
    assert result["one_step_worst_survivors_by_candidate"] == {
        "tree-0-singleton": 8,
        "tree-1-planned-a": 6,
        "tree-2-greedy": 5,
        "tree-3-planned-b": 6,
    }
    assert result["greedy_policy"] == {
        "first_candidate_id": "tree-2-greedy",
        "immediate_worst_surviving_hypotheses": 5,
        "two_epoch_worst_surviving_hypotheses": 4,
    }
    assert result["lookahead_policy"] == {
        "first_candidate_id": "tree-1-planned-a",
        "immediate_worst_surviving_hypotheses": 6,
        "two_epoch_worst_surviving_hypotheses": 3,
    }
    assert result["lookahead_reduction_ppm"] == 250_000
    assert result["additional_safe_role_candidates_guaranteed"] == 1
    assert result["reconfiguration_budget_epochs"] == 2
    assert result["extra_reconfigurations_vs_greedy"] == 0
    assert result["additional_diagnostic_messages"] == 0
    assert result["independent_policy_audit"] == {
        "deterministic_adaptive_policies_checked": 64,
        "independent_two_epoch_optimum": 3,
    }
    assert all(result["theorem_checks"].values())


def test_exhaustive_solver_soundness_and_identifiability_ceiling() -> None:
    result = run_passive_reconfiguration_game_evaluation()

    assert result["exhaustive_solver_audit"] == {
        "nonempty_beliefs_checked": 511,
        "belief_horizon_pairs_checked": 1_022,
        "independent_value_mismatches": 0,
        "primary_objective_regressions": 0,
    }
    assert result["sound_belief_update_audit"] == {
        "supported_actual_outcomes_checked": 36,
        "actual_hypothesis_eliminations": 0,
    }
    ceiling = result["passive_identifiability_ceiling"]
    assert ceiling["indistinguishable_pairs"] == [
        ["strategy-left", "strategy-right"]
    ]
    assert ceiling["worst_terminal_hypotheses"] == [2] * 8
    assert ceiling["passive_identification_possible"] is False


def test_information_cannot_buy_a_worse_current_containment_bound() -> None:
    hypotheses = (
        _hypothesis("fault-0", 0),
        _hypothesis("fault-1", 1),
    )
    safe_uninformative = KauriTreeCandidate(
        candidate_id="safe-uninformative",
        members_breadth_first=(3, 2, 0, 1),
        fanout=2,
        predicted_latency_us=10,
        passive_outcomes=(
            _support("fault-0", "common"),
            _support("fault-1", "common"),
        ),
    )
    risky_informative = KauriTreeCandidate(
        candidate_id="risky-informative",
        members_breadth_first=(3, 0, 2, 1),
        fanout=2,
        predicted_latency_us=10,
        passive_outcomes=(
            _support("fault-0", "left"),
            _support("fault-1", "right"),
        ),
    )

    selection = select_lookahead_topology(
        hypotheses,
        (risky_informative, safe_uninformative),
        horizon=2,
    )
    scores = {
        score.candidate_id: score for score in selection.candidate_scores
    }

    assert selection.selected.candidate_id == "safe-uninformative"
    assert selection.score.worst_case_exposure == 1
    assert selection.score.worst_case_terminal_hypotheses == 2
    assert scores["risky-informative"].worst_case_exposure == 2
    assert scores["risky-informative"].worst_case_terminal_hypotheses == 1
    assert scores["risky-informative"].primary_optimal is False


def test_set_valued_outcomes_update_soundly_and_expose_impossibility() -> None:
    hypotheses = (
        _hypothesis("left-strategy", 0),
        _hypothesis("right-strategy", 1),
    )
    candidate = KauriTreeCandidate(
        candidate_id="tree",
        members_breadth_first=(2, 0, 1),
        fanout=2,
        passive_outcomes=(
            _support("left-strategy", "common", "left-only"),
            _support("right-strategy", "common", "right-only"),
        ),
    )

    assert advance_compatible_hypotheses(
        hypotheses,
        candidate,
        "common",
    ) == hypotheses
    assert advance_compatible_hypotheses(
        hypotheses,
        candidate,
        "left-only",
    ) == (hypotheses[0],)
    assert indistinguishable_hypothesis_pairs(
        hypotheses,
        (candidate,),
    ) == (("left-strategy", "right-strategy"),)
    with pytest.raises(RobustTopologyError, match="impossible"):
        advance_compatible_hypotheses(
            hypotheses,
            candidate,
            "not-supported",
        )


@pytest.mark.parametrize(
    "horizon",
    (0, MAXIMUM_LOOKAHEAD_HORIZON + 1, True),
)
def test_invalid_lookahead_horizons_fail_closed(horizon: object) -> None:
    hypothesis = _hypothesis("fault", 0)
    candidate = KauriTreeCandidate(
        candidate_id="tree",
        members_breadth_first=(1, 0),
        fanout=1,
    )

    with pytest.raises(RobustTopologyError, match="horizon"):
        select_lookahead_topology(
            (hypothesis,),
            (candidate,),
            horizon=horizon,  # type: ignore[arg-type]
        )


def test_exact_solver_rejects_an_unbounded_hypothesis_space() -> None:
    hypotheses = tuple(
        _hypothesis(f"fault-mode-{index}", 0) for index in range(17)
    )
    candidate = KauriTreeCandidate(
        candidate_id="tree",
        members_breadth_first=(1, 0),
        fanout=1,
    )

    with pytest.raises(RobustTopologyError, match="fixed count bound"):
        select_lookahead_topology(
            hypotheses,
            (candidate,),
            horizon=1,
        )
