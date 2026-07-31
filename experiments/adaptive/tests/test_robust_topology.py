"""Proof-oriented tests for hypothesis-aware Kauri tree selection."""

from __future__ import annotations

from itertools import combinations, permutations

import pytest

from experiments.adaptive.kauri_experiment.robust_topology import (
    JointFaultHypothesis,
    KauriTreeCandidate,
    PassiveOutcomeSupport,
    RobustTopologyError,
    score_topology,
    select_robust_topology,
    structural_exposure,
)
from experiments.adaptive.kauri_experiment.robust_topology_evaluation import (
    run_correlation_gap_evaluation,
    run_exhaustive_minimax_audit,
    run_passive_diagnosis_evaluation,
    run_reachable_diagnosis_collision_evaluation,
)


def _hypothesis(
    hypothesis_id: str,
    *faulty_replicas: int,
) -> JointFaultHypothesis:
    return JointFaultHypothesis(
        hypothesis_id=hypothesis_id,
        faulty_replicas=frozenset(faulty_replicas),
    )


def _candidate(
    candidate_id: str,
    members: tuple[int, ...],
    *,
    fanout: int = 2,
    latency_us: int = 0,
    supports: tuple[PassiveOutcomeSupport, ...] = (),
) -> KauriTreeCandidate:
    return KauriTreeCandidate(
        candidate_id=candidate_id,
        members_breadth_first=members,
        fanout=fanout,
        predicted_latency_us=latency_us,
        churn_cost=0,
        passive_outcomes=supports,
    )


def test_structural_exposure_unions_overlapping_faulty_subtrees() -> None:
    tree = _candidate("tree", (6, 0, 4, 1, 2, 3, 5))

    nested = _hypothesis("nested", 0, 1)
    split = _hypothesis("split", 0, 4)

    assert structural_exposure(tree, nested) == 3
    assert structural_exposure(tree, split) == 6


def test_selector_contains_then_preserves_latency_then_learns_for_free() -> (
    None
):
    hypotheses = (
        _hypothesis("false-reporter-0", 0),
        _hypothesis("persistent-omitter-2", 2),
    )
    baseline_members = (6, 0, 1, 2, 3, 4, 5)
    rematched_members = (6, 0, 1, 3, 4, 2, 5)
    uninformative = _candidate(
        "repeated-parent",
        baseline_members,
        fanout=2,
        latency_us=10,
        supports=(
            PassiveOutcomeSupport(
                "false-reporter-0",
                frozenset({"0->2:response", "0->2:timeout"}),
            ),
            PassiveOutcomeSupport(
                "persistent-omitter-2",
                frozenset({"0->2:timeout"}),
            ),
        ),
    )
    fast_informative = _candidate(
        "fresh-parent-rematch",
        rematched_members,
        fanout=2,
        latency_us=10,
        supports=(
            PassiveOutcomeSupport(
                "false-reporter-0",
                frozenset({"1->2:response"}),
            ),
            PassiveOutcomeSupport(
                "persistent-omitter-2",
                frozenset({"1->2:timeout"}),
            ),
        ),
    )
    slow_perfect_test = _candidate(
        "slow-fresh-parent-rematch",
        rematched_members,
        fanout=2,
        latency_us=11,
        supports=(
            PassiveOutcomeSupport(
                "false-reporter-0",
                frozenset({"1->2:response"}),
            ),
            PassiveOutcomeSupport(
                "persistent-omitter-2",
                frozenset({"1->2:timeout"}),
            ),
        ),
    )

    selection = select_robust_topology(
        hypotheses,
        (uninformative, slow_perfect_test, fast_informative),
    )

    assert selection.selected.candidate_id == "fresh-parent-rematch"
    assert selection.score.worst_case_exposure == 3
    assert selection.score.predicted_latency_us == 10
    assert selection.score.worst_case_surviving_hypotheses == 1
    assert selection.score.guaranteed_hypothesis_elimination == 1


def test_set_valued_outcomes_are_scored_against_byzantine_choice() -> None:
    hypotheses = (
        _hypothesis("adaptive-liar", 0),
        _hypothesis("fault-1", 1),
        _hypothesis("fault-2", 2),
    )
    candidate = _candidate(
        "nondeterministic",
        (3, 0, 1, 2),
        fanout=3,
        supports=(
            PassiveOutcomeSupport(
                "adaptive-liar",
                frozenset({"left", "right"}),
            ),
            PassiveOutcomeSupport("fault-1", frozenset({"left"})),
            PassiveOutcomeSupport("fault-2", frozenset({"right"})),
        ),
    )

    score = score_topology(hypotheses, candidate)

    assert score.worst_case_surviving_hypotheses == 2
    assert score.guaranteed_hypothesis_elimination == 1
    assert {
        branch.outcome: branch.surviving_hypothesis_ids
        for branch in score.passive_branches
    } == {
        "left": ("adaptive-liar", "fault-1"),
        "right": ("adaptive-liar", "fault-2"),
    }


def test_n7_correlation_gap_is_an_exact_scalar_reputation_counterexample() -> (
    None
):
    result = run_correlation_gap_evaluation()

    assert result["legal_topologies_checked"] == 720
    assert result["structural_exposure_reference_mismatches"] == 0
    assert result["marginal_fault_counts_identical"] is True
    assert result["marginal_fault_counts"] == [3, 3, 3, 3, 3, 3, 0]
    assert result["hypotheses_per_evidence_state"] == 9
    assert result["state_a_optimal_worst_case_exposure"] == 4
    assert result["state_b_optimal_worst_case_exposure"] == 4
    assert result["common_optimal_topologies"] == 0
    assert result["best_marginal_blind_worst_case_exposure"] == 6
    assert result["marginal_blind_excess_ppm"] == 500_000
    assert result["hypothesis_aware_reduction_ppm"] == 333_333
    assert result["theorem_checks"] == {
        "same_scalar_input": True,
        "different_joint_evidence": True,
        "no_shared_optimum": True,
        "scalar_policy_lower_bound_holds": True,
        "hypothesis_aware_strictly_improves": True,
    }


def test_passive_diagnosis_adds_no_modeled_risk_latency_or_messages() -> None:
    result = run_passive_diagnosis_evaluation()

    assert result == {
        "candidate_count": 3,
        "selected_candidate_id": "fresh-parent-rematch",
        "worst_case_exposure": 3,
        "predicted_latency_us": 10,
        "guaranteed_hypothesis_elimination": 1,
        "baseline_guaranteed_hypothesis_elimination": 0,
        "additional_protocol_messages": 0,
        "outcome_source": "existing parent-child response evidence",
        "risk_regression": 0,
        "latency_regression_us": 0,
    }


def test_scalar_collision_is_reachable_in_the_implemented_diagnosis() -> None:
    result = run_reachable_diagnosis_collision_evaluation()

    assert result["replica_count"] == 5
    assert result["diagnostic_fault_bound"] == 2
    assert result["legal_topologies_checked"] == 120
    assert result["compatible_hypotheses_per_state"] == 4
    assert result["mode_marginal_counts_identical"] is True
    assert result["mode_marginal_counts"] == [
        [0, 0],
        [0, 2],
        [2, 0],
        [2, 0],
        [0, 2],
    ]
    assert result["joint_hypothesis_sets_differ"] is True
    assert result["state_a_optimal_worst_case_exposure"] == 3
    assert result["state_b_optimal_worst_case_exposure"] == 3
    assert result["common_optimal_topologies"] == 0
    assert result["best_marginal_blind_worst_case_exposure"] == 4
    assert result["marginal_blind_excess_ppm"] == 333_333
    assert result["hypothesis_aware_reduction_ppm"] == 250_000


def test_exhaustive_small_instance_minimax_audit_has_no_mismatches() -> None:
    result = run_exhaustive_minimax_audit()

    assert result == {
        "replica_count": 4,
        "fanout": 2,
        "fault_sets": 10,
        "nonempty_hypothesis_families": 1023,
        "legal_topologies_per_family": 24,
        "topology_scores_checked": 24_552,
        "minimax_mismatches": 0,
        "actual_hypothesis_bound_violations": 0,
    }


def test_selector_matches_an_independent_minimax_reference() -> None:
    members = tuple(range(4))
    hypotheses = tuple(
        _hypothesis(
            "fault-" + "-".join(str(replica) for replica in faulty),
            *faulty,
        )
        for fault_count in (1, 2)
        for faulty in combinations(members, fault_count)
    )
    candidates = tuple(
        _candidate(
            "tree-" + "".join(str(replica) for replica in order),
            order,
        )
        for order in permutations(members)
    )

    selection = select_robust_topology(hypotheses, candidates)
    independent_minimum = min(
        max(
            structural_exposure(candidate, hypothesis)
            for hypothesis in hypotheses
        )
        for candidate in candidates
    )

    assert selection.score.worst_case_exposure == independent_minimum
    assert all(
        structural_exposure(selection.selected, hypothesis)
        <= selection.score.worst_case_exposure
        for hypothesis in hypotheses
    )


@pytest.mark.parametrize(
    ("hypotheses", "candidates", "message"),
    [
        ((), (_candidate("tree", (0,)),), "hypothesis"),
        (
            (_hypothesis("h", 9),),
            (_candidate("tree", (0,)),),
            "outside",
        ),
        (
            (_hypothesis("h", 0),),
            (),
            "candidate",
        ),
    ],
)
def test_invalid_games_fail_closed(
    hypotheses: tuple[JointFaultHypothesis, ...],
    candidates: tuple[KauriTreeCandidate, ...],
    message: str,
) -> None:
    with pytest.raises(RobustTopologyError, match=message):
        select_robust_topology(hypotheses, candidates)
