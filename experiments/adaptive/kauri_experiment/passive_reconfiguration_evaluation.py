"""Executable evidence for containment-constrained passive lookahead.

The main witness uses four legal three-level N=31, fanout-five trees.  Nine
potentially faulty replicas remain leaves in every tree, so every candidate
has the same modeled worst-case structural exposure and latency.  Reassigning
those leaves changes which existing parent-child outcome is observed.

The witness separates one-step and two-step policies: the uniquely best
one-step split leaves four hypotheses in the worst case after two epochs,
while the exact finite-horizon policy leaves at most three.  A second fixture
shows the opposite boundary: if two hypotheses share an adversarially
selectable outcome under every tree, no passive policy can separate them.

This is synthetic model evidence, not a live Kauri execution, latency
measurement, consensus-safety proof, or global novelty proof.
"""

from __future__ import annotations

from functools import lru_cache
from itertools import product
from typing import Any

from .passive_reconfiguration_game import (
    advance_compatible_hypotheses,
    evaluate_greedy_topology_policy,
    indistinguishable_hypothesis_pairs,
    select_lookahead_topology,
)
from .robust_topology import (
    JointFaultHypothesis,
    KauriTreeCandidate,
    PassiveOutcomeSupport,
    score_topology,
)


N31_MEMBERSHIP = tuple(range(31))
N31_SUSPECTS = tuple(range(9))
N31_SAFE_INTERNALS = tuple(range(9, 15))
N31_SAFE_LEAF_FILLERS = tuple(range(15, 31))
N31_FANOUT = 5

# Canonical binary partitions of the nine singleton-fault hypotheses.
# The third is the unique best one-step split; the second and fourth are the
# better first actions when two epochs are planned jointly.
PARTITIONS = (
    ("tree-0-singleton", frozenset({0})),
    ("tree-1-planned-a", frozenset({0, 1, 2})),
    ("tree-2-greedy", frozenset({0, 1, 3, 4})),
    ("tree-3-planned-b", frozenset({0, 1, 2, 5, 6, 7})),
)


def _hypotheses() -> tuple[JointFaultHypothesis, ...]:
    return tuple(
        JointFaultHypothesis(
            hypothesis_id=f"fault-{replica_id}",
            faulty_replicas=frozenset({replica_id}),
        )
        for replica_id in N31_SUSPECTS
    )


def _candidate(
    candidate_id: str,
    left_partition: frozenset[int],
) -> KauriTreeCandidate:
    # A complete N=31, fanout=5 Kauri tree has one root, five
    # intermediate nodes, and 25 leaves.  The first two intermediate
    # subtrees provide ten leaf slots for the left observation group.
    left_filler_count = 10 - len(left_partition)
    left_fillers = N31_SAFE_LEAF_FILLERS[:left_filler_count]
    right_fillers = N31_SAFE_LEAF_FILLERS[left_filler_count:]
    left_leaves = tuple(sorted(left_partition)) + left_fillers
    right_leaves = tuple(
        replica_id
        for replica_id in N31_SUSPECTS
        if replica_id not in left_partition
    ) + right_fillers
    members = N31_SAFE_INTERNALS + left_leaves + right_leaves
    return KauriTreeCandidate(
        candidate_id=candidate_id,
        members_breadth_first=members,
        fanout=N31_FANOUT,
        predicted_latency_us=10,
        churn_cost=0,
        passive_outcomes=tuple(
            PassiveOutcomeSupport(
                hypothesis_id=f"fault-{replica_id}",
                outcomes=frozenset(
                    {
                        "left-parent-group:timeout"
                        if replica_id in left_partition
                        else "right-parent-group:timeout"
                    }
                ),
            )
            for replica_id in N31_SUSPECTS
        ),
    )


def _fixture() -> tuple[
    tuple[JointFaultHypothesis, ...],
    tuple[KauriTreeCandidate, ...],
]:
    return (
        _hypotheses(),
        tuple(
            _candidate(candidate_id, partition)
            for candidate_id, partition in PARTITIONS
        ),
    )


def _partition_branches(belief: int, partition: int) -> tuple[int, ...]:
    full = (1 << len(N31_SUSPECTS)) - 1
    return tuple(
        branch
        for branch in (belief & partition, belief & (~partition) & full)
        if branch
    )


def _partition_masks() -> tuple[int, ...]:
    return tuple(
        sum(1 << replica_id for replica_id in partition)
        for _, partition in PARTITIONS
    )


def _independent_optimum(
    belief: int,
    horizon: int,
    partitions: tuple[int, ...],
) -> int:
    @lru_cache(maxsize=None)
    def value(current: int, remaining: int) -> int:
        if remaining == 0 or current.bit_count() <= 1:
            return current.bit_count()
        return min(
            max(
                value(branch, remaining - 1)
                for branch in _partition_branches(current, partition)
            )
            for partition in partitions
        )

    return value(belief, horizon)


def _independent_two_epoch_policy_audit() -> dict[str, int]:
    partitions = _partition_masks()
    full = (1 << len(N31_SUSPECTS)) - 1
    policies_checked = 0
    optimum = len(N31_SUSPECTS)
    for first in partitions:
        first_branches = _partition_branches(full, first)
        for continuations in product(
            range(len(partitions)),
            repeat=len(first_branches),
        ):
            policies_checked += 1
            terminal = max(
                max(
                    branch.bit_count()
                    for branch in _partition_branches(
                        first_branch,
                        partitions[continuation],
                    )
                )
                for first_branch, continuation in zip(
                    first_branches,
                    continuations,
                    strict=True,
                )
            )
            optimum = min(optimum, terminal)
    return {
        "deterministic_adaptive_policies_checked": policies_checked,
        "independent_two_epoch_optimum": optimum,
    }


def _project_candidate(
    candidate: KauriTreeCandidate,
    hypothesis_ids: frozenset[str],
) -> KauriTreeCandidate:
    return KauriTreeCandidate(
        candidate_id=candidate.candidate_id,
        members_breadth_first=candidate.members_breadth_first,
        fanout=candidate.fanout,
        predicted_latency_us=candidate.predicted_latency_us,
        churn_cost=candidate.churn_cost,
        passive_outcomes=tuple(
            support
            for support in candidate.passive_outcomes
            if support.hypothesis_id in hypothesis_ids
        ),
    )


def _exhaustive_solver_audit() -> dict[str, int]:
    hypotheses, candidates = _fixture()
    partitions = _partition_masks()
    belief_horizon_pairs = 0
    value_mismatches = 0
    primary_regressions = 0
    full = (1 << len(hypotheses)) - 1
    for belief in range(1, full + 1):
        projected_hypotheses = tuple(
            hypothesis
            for index, hypothesis in enumerate(hypotheses)
            if belief & (1 << index)
        )
        hypothesis_ids = frozenset(
            hypothesis.hypothesis_id
            for hypothesis in projected_hypotheses
        )
        projected_candidates = tuple(
            _project_candidate(candidate, hypothesis_ids)
            for candidate in candidates
        )
        for horizon in (1, 2):
            belief_horizon_pairs += 1
            selection = select_lookahead_topology(
                projected_hypotheses,
                projected_candidates,
                horizon=horizon,
            )
            expected = _independent_optimum(
                belief,
                horizon,
                partitions,
            )
            if selection.score.worst_case_terminal_hypotheses != expected:
                value_mismatches += 1
            if (
                selection.score.worst_case_exposure != 1
                or selection.score.predicted_latency_us != 10
            ):
                primary_regressions += 1
    return {
        "nonempty_beliefs_checked": full,
        "belief_horizon_pairs_checked": belief_horizon_pairs,
        "independent_value_mismatches": value_mismatches,
        "primary_objective_regressions": primary_regressions,
    }


def _sound_update_audit() -> dict[str, int]:
    hypotheses, candidates = _fixture()
    observations_checked = 0
    actual_hypothesis_eliminations = 0
    for candidate in candidates:
        supports = {
            support.hypothesis_id: next(iter(support.outcomes))
            for support in candidate.passive_outcomes
        }
        for hypothesis in hypotheses:
            observations_checked += 1
            survivors = advance_compatible_hypotheses(
                hypotheses,
                candidate,
                supports[hypothesis.hypothesis_id],
            )
            if hypothesis not in survivors:
                actual_hypothesis_eliminations += 1
    return {
        "supported_actual_outcomes_checked": observations_checked,
        "actual_hypothesis_eliminations": actual_hypothesis_eliminations,
    }


def run_two_epoch_separation_evaluation() -> dict[str, Any]:
    """Show strict lookahead value with no primary-objective regression."""

    hypotheses, candidates = _fixture()
    greedy = evaluate_greedy_topology_policy(
        hypotheses,
        candidates,
        horizon=2,
    )
    lookahead = select_lookahead_topology(
        hypotheses,
        candidates,
        horizon=2,
    )
    one_step_scores = {
        score.candidate_id: score.immediate_worst_surviving_hypotheses
        for score in lookahead.candidate_scores
    }
    exposure_scores = {
        candidate.candidate_id: score_topology(
            hypotheses,
            candidate,
        ).worst_case_exposure
        for candidate in candidates
    }
    policy_audit = _independent_two_epoch_policy_audit()

    return {
        "replica_count": len(N31_MEMBERSHIP),
        "fanout": N31_FANOUT,
        "tree_levels": 3,
        "internal_replicas_per_candidate": len(N31_SAFE_INTERNALS),
        "leaf_replicas_per_candidate": (
            len(N31_MEMBERSHIP) - len(N31_SAFE_INTERNALS)
        ),
        "compatible_singleton_fault_hypotheses": len(hypotheses),
        "candidate_topologies": len(candidates),
        "all_suspects_are_leaves_in_every_candidate": all(
            all(
                candidate.members_breadth_first.index(replica_id) >= 6
                for replica_id in N31_SUSPECTS
            )
            for candidate in candidates
        ),
        "worst_case_exposure_by_candidate": exposure_scores,
        "predicted_latency_us_by_candidate": {
            candidate.candidate_id: candidate.predicted_latency_us
            for candidate in candidates
        },
        "one_step_worst_survivors_by_candidate": one_step_scores,
        "greedy_policy": {
            "first_candidate_id": greedy.selected.candidate_id,
            "immediate_worst_surviving_hypotheses": (
                greedy.immediate_worst_surviving_hypotheses
            ),
            "two_epoch_worst_surviving_hypotheses": (
                greedy.worst_case_terminal_hypotheses
            ),
        },
        "lookahead_policy": {
            "first_candidate_id": lookahead.selected.candidate_id,
            "immediate_worst_surviving_hypotheses": (
                lookahead.score.immediate_worst_surviving_hypotheses
            ),
            "two_epoch_worst_surviving_hypotheses": (
                lookahead.score.worst_case_terminal_hypotheses
            ),
        },
        "lookahead_reduction_ppm": (
            (
                greedy.worst_case_terminal_hypotheses
                - lookahead.score.worst_case_terminal_hypotheses
            )
            * 1_000_000
            // greedy.worst_case_terminal_hypotheses
        ),
        "additional_safe_role_candidates_guaranteed": (
            greedy.worst_case_terminal_hypotheses
            - lookahead.score.worst_case_terminal_hypotheses
        ),
        "reconfiguration_budget_epochs": 2,
        "extra_reconfigurations_vs_greedy": 0,
        "additional_diagnostic_messages": 0,
        "outcome_source": (
            "coarsened existing parent-child timeout evidence"
        ),
        "observation_model_caveat": (
            "the strict gap is proved for this declared two-group "
            "abstraction; exact runtime reporter-target evidence is richer "
            "and must be evaluated separately"
        ),
        "independent_policy_audit": policy_audit,
        "theorem_checks": {
            "unique_one_step_greedy_action": (
                list(one_step_scores.values()).count(
                    min(one_step_scores.values())
                )
                == 1
            ),
            "lookahead_strictly_improves_terminal_ambiguity": (
                lookahead.score.worst_case_terminal_hypotheses
                < greedy.worst_case_terminal_hypotheses
            ),
            "containment_does_not_regress": (
                lookahead.score.worst_case_exposure
                == min(exposure_scores.values())
            ),
            "latency_does_not_regress": (
                lookahead.score.predicted_latency_us
                == min(
                    candidate.predicted_latency_us
                    for candidate in candidates
                    if exposure_scores[candidate.candidate_id]
                    == min(exposure_scores.values())
                )
            ),
            "independent_optimum_matches": (
                lookahead.score.worst_case_terminal_hypotheses
                == policy_audit["independent_two_epoch_optimum"]
            ),
        },
    }


def run_passive_identifiability_ceiling_evaluation() -> dict[str, Any]:
    """Show that arbitrary shared outcomes can defeat every passive policy."""

    hypotheses = (
        JointFaultHypothesis("strategy-left", frozenset({0})),
        JointFaultHypothesis("strategy-right", frozenset({1})),
    )
    candidates = tuple(
        KauriTreeCandidate(
            candidate_id=f"ambiguous-tree-{index}",
            members_breadth_first=order,
            fanout=6,
            predicted_latency_us=10,
            passive_outcomes=(
                PassiveOutcomeSupport(
                    "strategy-left",
                    frozenset({"normal", f"left-only-{index}"}),
                ),
                PassiveOutcomeSupport(
                    "strategy-right",
                    frozenset({"normal", f"right-only-{index}"}),
                ),
            ),
        )
        for index, order in enumerate(
            (
                (6, 0, 1, 2, 3, 4, 5),
                (6, 1, 0, 2, 3, 4, 5),
            )
        )
    )
    horizons = tuple(range(1, 9))
    terminal_values = tuple(
        select_lookahead_topology(
            hypotheses,
            candidates,
            horizon=horizon,
        ).score.worst_case_terminal_hypotheses
        for horizon in horizons
    )
    return {
        "hypotheses": len(hypotheses),
        "candidate_topologies": len(candidates),
        "shared_adversarial_outcome": "normal",
        "indistinguishable_pairs": [
            list(pair)
            for pair in indistinguishable_hypothesis_pairs(
                hypotheses,
                candidates,
            )
        ],
        "horizons_checked": list(horizons),
        "worst_terminal_hypotheses": list(terminal_values),
        "analytic_reason": (
            "after every chosen tree the adversary can emit 'normal', "
            "which is allowed by both hypotheses and reproduces the same "
            "belief state"
        ),
        "passive_identification_possible": False,
    }


def run_passive_reconfiguration_game_evaluation() -> dict[str, Any]:
    """Return the complete bounded proof bundle."""

    return {
        "claim": (
            "Among Kauri trees tied for minimum worst-case exposure and "
            "predicted latency, finite-horizon joint-hypothesis planning "
            "can strictly reduce worst-case fault ambiguity relative to "
            "greedy one-epoch selection under the same reconfiguration "
            "budget and using only existing-traffic outcomes; a shared "
            "Byzantine outcome under every tree certifies a passive "
            "identification impossibility."
        ),
        "two_epoch_greedy_separation": (
            run_two_epoch_separation_evaluation()
        ),
        "exhaustive_solver_audit": _exhaustive_solver_audit(),
        "sound_belief_update_audit": _sound_update_audit(),
        "passive_identifiability_ceiling": (
            run_passive_identifiability_ceiling_evaluation()
        ),
    }


__all__ = (
    "run_passive_identifiability_ceiling_evaluation",
    "run_passive_reconfiguration_game_evaluation",
    "run_two_epoch_separation_evaluation",
)
