"""Executable proof bundle for joint-hypothesis Kauri tree selection.

The bundle contains three deterministic model audits:

* an exhaustive N=7 binary-tree counterexample showing that identical
  per-replica fault marginals can require different robust topologies;
* two exact reachable snapshots of the implemented two-mode diagnosis model
  with identical per-mode marginals but different optimal trees;
* a passive-diagnosis no-regret witness; and
* an exhaustive N=4 minimax cross-check over every nonempty family of one-
  and two-replica fault sets.

This is synthetic model evidence.  It does not prove evidence soundness,
live integration, consensus safety, throughput recovery, or global novelty.
"""

from __future__ import annotations

from itertools import combinations, permutations
import json
import re
from typing import Any

from .diagnosis import (
    BoundedTwoModeDiagnosis,
    DiagnosisSnapshot,
    DiagnosticObservation,
    DiagnosticOutcome,
    FaultHypothesis,
)
from .robust_topology import (
    JointFaultHypothesis,
    KauriTreeCandidate,
    PassiveOutcomeSupport,
    score_topology,
    select_robust_topology,
)


SCHEMA_VERSION = 1
N7_MEMBERSHIP = tuple(range(7))
N7_SAFE_ROOT = 6
N7_FANOUT = 2
_GIT_REVISION = re.compile(r"^[0-9a-f]{40}$")

# Every replica 0..5 occurs in three of nine hypotheses in both states.
# Replica 6 is robust-safe.  The relational structure differs.
STATE_A_FAULT_SETS = (
    (0, 1),
    (0, 2),
    (0, 3),
    (1, 2),
    (1, 4),
    (2, 5),
    (3, 4),
    (3, 5),
    (4, 5),
)
STATE_B_FAULT_SETS = (
    (0, 1),
    (0, 4),
    (0, 5),
    (1, 3),
    (1, 5),
    (2, 3),
    (2, 4),
    (2, 5),
    (3, 4),
)

CLAIMS_NOT_MADE = (
    "the model audit is not a live Kauri protocol run",
    "compatible hypotheses are inputs, not proven sound by this module",
    "structural exposure is not a consensus-safety or liveness proof",
    "passive outcomes require existing-traffic instrumentation at runtime",
    "predicted latency equality is modeled rather than measured here",
    "the audit does not establish arbitrary Byzantine identification",
    "the scalar lower bound applies to deterministic marginal-only placement",
    "the N=5 reachable audit is a diagnostic submodel, not a BFT deployment",
    "the bounded literature search does not establish global novelty",
)


def _hypotheses(
    prefix: str,
    fault_sets: tuple[tuple[int, ...], ...],
) -> tuple[JointFaultHypothesis, ...]:
    return tuple(
        JointFaultHypothesis(
            hypothesis_id=(
                prefix + "-" + "-".join(str(value) for value in faulty)
            ),
            faulty_replicas=frozenset(faulty),
        )
        for faulty in fault_sets
    )


def _tree_id(order: tuple[int, ...]) -> str:
    return "tree-" + "-".join(str(replica) for replica in order)


def _candidate(
    order: tuple[int, ...],
    *,
    candidate_id: str | None = None,
    fanout: int = N7_FANOUT,
    predicted_latency_us: int = 0,
    passive_outcomes: tuple[PassiveOutcomeSupport, ...] = (),
) -> KauriTreeCandidate:
    return KauriTreeCandidate(
        candidate_id=candidate_id or _tree_id(order),
        members_breadth_first=order,
        fanout=fanout,
        predicted_latency_us=predicted_latency_us,
        churn_cost=0,
        passive_outcomes=passive_outcomes,
    )


def _marginal_fault_counts(
    hypotheses: tuple[JointFaultHypothesis, ...],
    membership: tuple[int, ...],
) -> list[int]:
    return [
        sum(
            replica in hypothesis.faulty_replicas
            for hypothesis in hypotheses
        )
        for replica in membership
    ]


def _independent_structural_exposure(
    candidate: KauriTreeCandidate,
    faulty_replicas: frozenset[int],
) -> int:
    """Reference exposure via parent chains, independent of subtree union."""

    exposed = 0
    for position in range(len(candidate.members_breadth_first)):
        ancestor = position
        while True:
            if candidate.members_breadth_first[ancestor] in faulty_replicas:
                exposed += 1
                break
            if ancestor == 0:
                break
            ancestor = (ancestor - 1) // candidate.fanout
    return exposed


def run_correlation_gap_evaluation() -> dict[str, Any]:
    """Exhaust normalized N=7 trees for the abstract scalar-state witness."""

    state_a = _hypotheses("a", STATE_A_FAULT_SETS)
    state_b = _hypotheses("b", STATE_B_FAULT_SETS)
    candidates = tuple(
        _candidate((N7_SAFE_ROOT, *suffix))
        for suffix in permutations(range(N7_SAFE_ROOT))
    )
    scores_a = tuple(
        score_topology(state_a, candidate).worst_case_exposure
        for candidate in candidates
    )
    scores_b = tuple(
        score_topology(state_b, candidate).worst_case_exposure
        for candidate in candidates
    )
    reference_scores_a = tuple(
        max(
            _independent_structural_exposure(
                candidate,
                hypothesis.faulty_replicas,
            )
            for hypothesis in state_a
        )
        for candidate in candidates
    )
    reference_scores_b = tuple(
        max(
            _independent_structural_exposure(
                candidate,
                hypothesis.faulty_replicas,
            )
            for hypothesis in state_b
        )
        for candidate in candidates
    )
    optimum_a = min(scores_a)
    optimum_b = min(scores_b)
    optimal_a = {
        index for index, value in enumerate(scores_a) if value == optimum_a
    }
    optimal_b = {
        index for index, value in enumerate(scores_b) if value == optimum_b
    }
    best_shared = min(
        max(left, right) for left, right in zip(scores_a, scores_b)
    )
    shared_index = min(
        index
        for index, (left, right) in enumerate(zip(scores_a, scores_b))
        if max(left, right) == best_shared
    )
    marginals_a = _marginal_fault_counts(state_a, N7_MEMBERSHIP)
    marginals_b = _marginal_fault_counts(state_b, N7_MEMBERSHIP)
    selected_a = select_robust_topology(state_a, candidates)
    selected_b = select_robust_topology(state_b, candidates)

    return {
        "replica_count": len(N7_MEMBERSHIP),
        "fanout": N7_FANOUT,
        "fixed_robust_safe_root": N7_SAFE_ROOT,
        "evidence_state_scope": (
            "abstract bounded joint hypotheses; reachability is audited "
            "separately in the N=5 implemented diagnosis submodel"
        ),
        "legal_topologies_checked": len(candidates),
        "hypotheses_per_evidence_state": len(state_a),
        "structural_exposure_reference_mismatches": sum(
            left != right
            for left, right in zip(scores_a, reference_scores_a)
        )
        + sum(
            left != right
            for left, right in zip(scores_b, reference_scores_b)
        ),
        "marginal_fault_counts_identical": marginals_a == marginals_b,
        "marginal_fault_counts": marginals_a,
        "state_a_fault_sets": [list(value) for value in STATE_A_FAULT_SETS],
        "state_b_fault_sets": [list(value) for value in STATE_B_FAULT_SETS],
        "state_a_optimal_worst_case_exposure": optimum_a,
        "state_b_optimal_worst_case_exposure": optimum_b,
        "state_a_optimal_topologies": len(optimal_a),
        "state_b_optimal_topologies": len(optimal_b),
        "common_optimal_topologies": len(optimal_a.intersection(optimal_b)),
        "best_marginal_blind_worst_case_exposure": best_shared,
        "best_marginal_blind_tree": list(
            candidates[shared_index].members_breadth_first
        ),
        "state_a_selected_tree": list(
            selected_a.selected.members_breadth_first
        ),
        "state_b_selected_tree": list(
            selected_b.selected.members_breadth_first
        ),
        "marginal_blind_excess_ppm": (
            ((best_shared - optimum_a) * 1_000_000) // optimum_a
        ),
        "hypothesis_aware_reduction_ppm": (
            ((best_shared - optimum_a) * 1_000_000) // best_shared
        ),
        "theorem_checks": {
            "same_scalar_input": marginals_a == marginals_b,
            "different_joint_evidence": (
                frozenset(STATE_A_FAULT_SETS)
                != frozenset(STATE_B_FAULT_SETS)
            ),
            "no_shared_optimum": not optimal_a.intersection(optimal_b),
            "scalar_policy_lower_bound_holds": best_shared == 6,
            "hypothesis_aware_strictly_improves": (
                optimum_a == optimum_b == 4 and best_shared > optimum_a
            ),
        },
    }


def _diagnostic_observation(
    attempt_id: str,
    reporter_id: int,
    target_id: int,
    outcome: DiagnosticOutcome,
) -> DiagnosticObservation:
    return DiagnosticObservation(
        attempt_id=attempt_id,
        reporter_id=reporter_id,
        target_id=target_id,
        outcome=outcome,
    )


def _joint_projection(
    prefix: str,
    hypotheses: tuple[FaultHypothesis, ...],
) -> tuple[JointFaultHypothesis, ...]:
    return tuple(
        JointFaultHypothesis(
            hypothesis_id=(
                f"{prefix}-L"
                + "-".join(str(value) for value in hypothesis.false_reporters)
                + "-C"
                + "-".join(
                    str(value)
                    for value in hypothesis.persistent_omitters
                )
            ),
            faulty_replicas=frozenset(
                hypothesis.false_reporters
                + hypothesis.persistent_omitters
            ),
        )
        for hypothesis in hypotheses
    )


def _mode_marginal_counts(
    snapshot: DiagnosisSnapshot,
) -> list[list[int]]:
    return [
        [
            counts.false_reporter_hypotheses,
            counts.persistent_omitter_hypotheses,
        ]
        for counts in snapshot.mode_counts
    ]


def run_reachable_diagnosis_collision_evaluation() -> dict[str, Any]:
    """Prove the correlation collision is reachable in the current oracle."""

    membership = tuple(range(5))
    state_a_observations = (
        _diagnostic_observation("a-2-to-1", 2, 1, "timeout"),
        _diagnostic_observation("a-3-to-4", 3, 4, "timeout"),
    )
    state_b_observations = (
        _diagnostic_observation("b-0-to-2", 0, 2, "response"),
        _diagnostic_observation("b-2-to-4", 2, 4, "timeout"),
        _diagnostic_observation("b-3-to-1", 3, 1, "timeout"),
    )
    state_a = BoundedTwoModeDiagnosis(membership, 2).observe_many(
        state_a_observations
    )
    state_b = BoundedTwoModeDiagnosis(membership, 2).observe_many(
        state_b_observations
    )
    joint_a = _joint_projection("a", state_a.hypotheses)
    joint_b = _joint_projection("b", state_b.hypotheses)
    candidates = tuple(
        _candidate(order, fanout=2) for order in permutations(membership)
    )
    scores_a = tuple(
        score_topology(joint_a, candidate).worst_case_exposure
        for candidate in candidates
    )
    scores_b = tuple(
        score_topology(joint_b, candidate).worst_case_exposure
        for candidate in candidates
    )
    optimum_a = min(scores_a)
    optimum_b = min(scores_b)
    optimal_a = {
        index for index, value in enumerate(scores_a) if value == optimum_a
    }
    optimal_b = {
        index for index, value in enumerate(scores_b) if value == optimum_b
    }
    best_shared = min(
        max(left, right) for left, right in zip(scores_a, scores_b)
    )
    marginals_a = _mode_marginal_counts(state_a)
    marginals_b = _mode_marginal_counts(state_b)

    def observations_value(
        values: tuple[DiagnosticObservation, ...],
    ) -> list[dict[str, int | str]]:
        return [
            {
                "reporter_id": value.reporter_id,
                "target_id": value.target_id,
                "outcome": value.outcome,
            }
            for value in values
        ]

    def hypotheses_value(
        values: tuple[FaultHypothesis, ...],
    ) -> list[dict[str, list[int]]]:
        return [
            {
                "false_reporters": list(value.false_reporters),
                "persistent_omitters": list(
                    value.persistent_omitters
                ),
            }
            for value in values
        ]

    return {
        "scope": (
            "exact implemented two-mode diagnosis submodel; N=5,d=2 is "
            "not claimed as a valid BFT deployment"
        ),
        "replica_count": len(membership),
        "diagnostic_fault_bound": 2,
        "legal_topologies_checked": len(candidates),
        "state_a_observations": observations_value(state_a_observations),
        "state_b_observations": observations_value(state_b_observations),
        "state_a_hypotheses": hypotheses_value(state_a.hypotheses),
        "state_b_hypotheses": hypotheses_value(state_b.hypotheses),
        "compatible_hypotheses_per_state": len(state_a.hypotheses),
        "mode_marginal_counts_identical": marginals_a == marginals_b,
        "mode_marginal_counts": marginals_a,
        "joint_hypothesis_sets_differ": (
            state_a.hypotheses != state_b.hypotheses
        ),
        "state_a_optimal_worst_case_exposure": optimum_a,
        "state_b_optimal_worst_case_exposure": optimum_b,
        "common_optimal_topologies": len(optimal_a.intersection(optimal_b)),
        "best_marginal_blind_worst_case_exposure": best_shared,
        "marginal_blind_excess_ppm": (
            ((best_shared - optimum_a) * 1_000_000) // optimum_a
        ),
        "hypothesis_aware_reduction_ppm": (
            ((best_shared - optimum_a) * 1_000_000) // best_shared
        ),
    }


def _passive_fixture() -> tuple[
    tuple[JointFaultHypothesis, ...],
    tuple[KauriTreeCandidate, ...],
]:
    hypotheses = (
        JointFaultHypothesis("false-reporter-0", frozenset({0})),
        JointFaultHypothesis("persistent-omitter-2", frozenset({2})),
    )
    baseline_members = (6, 0, 1, 2, 3, 4, 5)
    rematched_members = (6, 0, 1, 3, 4, 2, 5)
    uninformative = _candidate(
        baseline_members,
        candidate_id="repeated-parent",
        fanout=2,
        predicted_latency_us=10,
        passive_outcomes=(
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
    informative = _candidate(
        rematched_members,
        candidate_id="fresh-parent-rematch",
        fanout=2,
        predicted_latency_us=10,
        passive_outcomes=(
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
    slow_perfect = _candidate(
        rematched_members,
        candidate_id="slow-fresh-parent-rematch",
        fanout=2,
        predicted_latency_us=11,
        passive_outcomes=(
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
    return hypotheses, (uninformative, slow_perfect, informative)


def run_passive_diagnosis_evaluation() -> dict[str, int | str]:
    """Show information gain only after risk and latency remain tied."""

    hypotheses, candidates = _passive_fixture()
    selection = select_robust_topology(hypotheses, candidates)
    baseline = score_topology(hypotheses, candidates[0])
    return {
        "candidate_count": len(candidates),
        "selected_candidate_id": selection.selected.candidate_id,
        "worst_case_exposure": selection.score.worst_case_exposure,
        "predicted_latency_us": selection.score.predicted_latency_us,
        "guaranteed_hypothesis_elimination": (
            selection.score.guaranteed_hypothesis_elimination
        ),
        "baseline_guaranteed_hypothesis_elimination": (
            baseline.guaranteed_hypothesis_elimination
        ),
        "additional_protocol_messages": 0,
        "outcome_source": "existing parent-child response evidence",
        "risk_regression": (
            selection.score.worst_case_exposure
            - baseline.worst_case_exposure
        ),
        "latency_regression_us": (
            selection.score.predicted_latency_us
            - baseline.predicted_latency_us
        ),
    }


def run_exhaustive_minimax_audit() -> dict[str, int]:
    """Cross-check all small joint uncertainty sets against brute force."""

    membership = tuple(range(4))
    fault_sets = tuple(
        faulty
        for fault_count in (1, 2)
        for faulty in combinations(membership, fault_count)
    )
    hypotheses = tuple(
        JointFaultHypothesis(
            hypothesis_id=(
                "fault-" + "-".join(str(replica) for replica in faulty)
            ),
            faulty_replicas=frozenset(faulty),
        )
        for faulty in fault_sets
    )
    candidates = tuple(
        _candidate(order, fanout=2) for order in permutations(membership)
    )

    minimax_mismatches = 0
    bound_violations = 0
    family_count = (2 ** len(hypotheses)) - 1
    for family_mask in range(1, family_count + 1):
        family = tuple(
            hypothesis
            for index, hypothesis in enumerate(hypotheses)
            if family_mask & (1 << index)
        )
        selection = select_robust_topology(family, candidates)
        independent_minimum = min(
            max(
                _independent_structural_exposure(
                    candidate,
                    hypothesis.faulty_replicas,
                )
                for hypothesis in family
            )
            for candidate in candidates
        )
        if selection.score.worst_case_exposure != independent_minimum:
            minimax_mismatches += 1
        bound_violations += sum(
            _independent_structural_exposure(
                selection.selected,
                hypothesis.faulty_replicas,
            )
            > selection.score.worst_case_exposure
            for hypothesis in family
        )

    return {
        "replica_count": len(membership),
        "fanout": 2,
        "fault_sets": len(fault_sets),
        "nonempty_hypothesis_families": family_count,
        "legal_topologies_per_family": len(candidates),
        "topology_scores_checked": family_count * len(candidates),
        "minimax_mismatches": minimax_mismatches,
        "actual_hypothesis_bound_violations": bound_violations,
    }


def evaluate_robust_topology(
    kauri_revision: str,
    *,
    revision_verification: str,
) -> dict[str, Any]:
    """Build the canonical revision-bound proof bundle."""

    if not _GIT_REVISION.fullmatch(kauri_revision):
        raise ValueError("Kauri revision must be 40 lowercase hex characters")
    if not revision_verification:
        raise ValueError("revision verification must not be empty")
    return {
        "schema_version": SCHEMA_VERSION,
        "kauri_revision": kauri_revision,
        "revision_verification": revision_verification,
        "claim": (
            "For deterministic Kauri placement, retaining joint compatible "
            "fault hypotheses can strictly reduce worst-case tree exposure "
            "compared with retaining only per-replica marginals; among "
            "equally robust and equally fast trees, existing-traffic "
            "outcomes can then improve diagnosis without extra messages."
        ),
        "correlation_gap": run_correlation_gap_evaluation(),
        "reachable_diagnosis_collision": (
            run_reachable_diagnosis_collision_evaluation()
        ),
        "passive_diagnosis": run_passive_diagnosis_evaluation(),
        "exhaustive_minimax_audit": run_exhaustive_minimax_audit(),
        "claims_not_made": list(CLAIMS_NOT_MADE),
    }


def canonical_evaluation_json(value: dict[str, Any]) -> str:
    """Return deterministic human-readable JSON."""

    return json.dumps(value, indent=2, sort_keys=True)


__all__ = (
    "canonical_evaluation_json",
    "evaluate_robust_topology",
    "run_correlation_gap_evaluation",
    "run_exhaustive_minimax_audit",
    "run_passive_diagnosis_evaluation",
    "run_reachable_diagnosis_collision_evaluation",
)
