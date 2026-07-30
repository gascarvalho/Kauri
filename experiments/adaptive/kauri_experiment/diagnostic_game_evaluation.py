"""Deterministic evidence for certificate-based minimax rematching.

The evaluator exhaustively checks the tight distinct-reporter bound for
``d=1..3`` and records the canonical N=7 one-timeout game.  It is synthetic
model evidence, not a live protocol run or a consensus-safety proof.
"""

from __future__ import annotations

from itertools import product
import json
import re
from typing import Any

from .diagnosis import (
    BoundedTwoModeDiagnosis,
    DiagnosticObservation,
    FaultHypothesis,
)
from .diagnostic_game import (
    DiagnosticProbe,
    DiagnosticProbeScore,
    choose_minimax_probe,
    robust_safe_replicas,
    score_probe,
    target_omission_status,
)


SCHEMA_VERSION = 2
REPLICA_IDS = tuple(range(7))
DIAGNOSTIC_FAULT_BOUND = 1
INITIAL_REPORTER_ID = 6
TARGET_ID = 1
FRESH_REPORTER_ID = 0
_GIT_REVISION = re.compile(r"^[0-9a-f]{40}$")

CLAIMS_NOT_MADE = (
    "synthetic model enumeration is not live protocol evidence",
    "the counting bound is a scoped specialization of classical diagnosis",
    "persistent omission does not distinguish crash, attack, or link failure",
    "the model excludes selective omission and mode changes inside a window",
    "abstract directed pairs are not proof of Kauri topology legality",
    "no throughput improvement or arbitrary-Byzantine diagnosis is claimed",
    "probe scores cannot change consensus membership or safety rules",
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


def _probe_value(probe: DiagnosticProbe) -> dict[str, int]:
    return {
        "reporter_id": probe.reporter_id,
        "target_id": probe.target_id,
    }


def _hypothesis_value(
    hypothesis: FaultHypothesis,
) -> dict[str, list[int]]:
    return {
        "false_reporters": list(hypothesis.false_reporters),
        "persistent_omitters": list(
            hypothesis.persistent_omitters
        ),
    }


def _score_value(score: DiagnosticProbeScore) -> dict[str, Any]:
    return {
        "probe": _probe_value(score.probe),
        "fresh_reporter_for_target": score.fresh_reporter_for_target,
        "worst_case_surviving_hypotheses": (
            score.worst_case_surviving_hypotheses
        ),
        "guaranteed_hypothesis_elimination": (
            score.guaranteed_hypothesis_elimination
        ),
        "current_robust_safe_role_value": (
            score.current_robust_safe_role_value
        ),
        "worst_case_robust_safe_role_value": (
            score.worst_case_robust_safe_role_value
        ),
        "guaranteed_safe_role_value_recovery": (
            score.guaranteed_safe_role_value_recovery
        ),
        "branches": [
            {
                "outcome": branch.outcome,
                "surviving_hypotheses": (
                    branch.surviving_hypotheses
                ),
                "robust_safe_replicas": list(
                    branch.robust_safe_replicas
                ),
                "robust_safe_role_value": (
                    branch.robust_safe_role_value
                ),
            }
            for branch in score.branches
        ],
    }


def _initial_snapshot():
    diagnosis = BoundedTwoModeDiagnosis(
        REPLICA_IDS,
        DIAGNOSTIC_FAULT_BOUND,
    )
    snapshot = diagnosis.observe(
        _observation(
            "initial-timeout",
            INITIAL_REPORTER_ID,
            TARGET_ID,
            "timeout",
        )
    )
    return snapshot


def _tight_bound_audit(diagnostic_fault_bound: int) -> dict[str, Any]:
    reporter_count = 2 * diagnostic_fault_bound
    target_id = reporter_count
    unresolved_patterns = 0
    status_formula_mismatches = 0

    for pattern_number, outcomes in enumerate(
        product(("response", "timeout"), repeat=reporter_count)
    ):
        diagnosis = BoundedTwoModeDiagnosis(
            range(reporter_count + 1),
            diagnostic_fault_bound,
        )
        snapshot = diagnosis.observe_many(
            _observation(
                f"d{diagnostic_fault_bound}-"
                f"pattern{pattern_number}-reporter{reporter_id}",
                reporter_id,
                target_id,
                outcome,
            )
            for reporter_id, outcome in enumerate(outcomes)
        )
        status = target_omission_status(snapshot, target_id)
        if status == "ambiguous":
            unresolved_patterns += 1
        expected = (
            "persistent_omitter"
            if outcomes.count("timeout") > diagnostic_fault_bound
            else "not_persistent_omitter"
        )
        if status != expected:
            status_formula_mismatches += 1

    lower_reporter_count = reporter_count - 1
    lower_target_id = lower_reporter_count
    lower_outcomes = (
        ("timeout",) * diagnostic_fault_bound
        + ("response",) * (diagnostic_fault_bound - 1)
    )
    lower_diagnosis = BoundedTwoModeDiagnosis(
        range(lower_reporter_count + 1),
        diagnostic_fault_bound,
    )
    lower_snapshot = lower_diagnosis.observe_many(
        _observation(
            f"d{diagnostic_fault_bound}-lower-reporter{reporter_id}",
            reporter_id,
            lower_target_id,
            outcome,
        )
        for reporter_id, outcome in enumerate(lower_outcomes)
    )

    return {
        "diagnostic_fault_bound": diagnostic_fault_bound,
        "total_distinct_reporters": reporter_count,
        "additional_reporters_after_triggering_timeout_worst_case": (
            reporter_count - 1
        ),
        "outcome_patterns_checked": 2**reporter_count,
        "unresolved_patterns_after_2d_reports": unresolved_patterns,
        "status_formula_mismatches": status_formula_mismatches,
        "certification_thresholds": {
            "timeouts_for_persistent_omitter": (
                diagnostic_fault_bound + 1
            ),
            "responses_for_not_persistent_omitter": (
                diagnostic_fault_bound
            ),
        },
        "two_d_minus_one_witness": {
            "distinct_reporters": lower_reporter_count,
            "timeouts": diagnostic_fault_bound,
            "responses": diagnostic_fault_bound - 1,
            "status": target_omission_status(
                lower_snapshot,
                lower_target_id,
            ),
            "compatible_hypotheses": (
                lower_snapshot.compatible_hypothesis_count
            ),
        },
    }


def _challenge_branch(outcome: str) -> dict[str, Any]:
    diagnosis = BoundedTwoModeDiagnosis(
        REPLICA_IDS,
        DIAGNOSTIC_FAULT_BOUND,
    )
    diagnosis.observe(
        _observation(
            "initial-timeout",
            INITIAL_REPORTER_ID,
            TARGET_ID,
            "timeout",
        )
    )
    snapshot = diagnosis.observe(
        _observation(
            f"fresh-{outcome}",
            FRESH_REPORTER_ID,
            TARGET_ID,
            outcome,
        )
    )
    return {
        "outcome": outcome,
        "target_status": target_omission_status(snapshot, TARGET_ID),
        "compatible_hypotheses": (
            snapshot.compatible_hypothesis_count
        ),
        "robust_safe_replicas": list(robust_safe_replicas(snapshot)),
    }


def _canonical_game() -> dict[str, Any]:
    snapshot = _initial_snapshot()
    repeat_probe = DiagnosticProbe(INITIAL_REPORTER_ID, TARGET_ID)
    fresh_probe = DiagnosticProbe(FRESH_REPORTER_ID, TARGET_ID)
    fresh_alternative = DiagnosticProbe(2, TARGET_ID)
    abstract_probes = tuple(
        DiagnosticProbe(reporter_id, target_id)
        for reporter_id in REPLICA_IDS
        for target_id in REPLICA_IDS
        if reporter_id != target_id
    )
    abstract_scores = tuple(
        score_probe(snapshot, probe) for probe in abstract_probes
    )
    primary_values = tuple(
        (
            -score.guaranteed_safe_role_value_recovery,
            score.worst_case_surviving_hypotheses,
            0 if score.fresh_reporter_for_target else 1,
        )
        for score in abstract_scores
    )
    best_primary_value = min(primary_values)
    optimal_equivalence_class = tuple(
        score.probe
        for score, primary_value in zip(
            abstract_scores,
            primary_values,
            strict=True,
        )
        if primary_value == best_primary_value
    )
    selected = choose_minimax_probe(snapshot, abstract_probes)
    before_safe = robust_safe_replicas(snapshot)
    branches = (
        _challenge_branch("response"),
        _challenge_branch("timeout"),
    )
    membership = frozenset(REPLICA_IDS)
    pre_excluded = sorted(membership - frozenset(before_safe))
    post_excluded_counts = tuple(
        len(
            membership
            - frozenset(branch["robust_safe_replicas"])
        )
        for branch in branches
    )
    lowest_fresh_round_robin = min(
        replica_id
        for replica_id in REPLICA_IDS
        if replica_id not in (INITIAL_REPORTER_ID, TARGET_ID)
    )

    return {
        "initial_observation": {
            "reporter_id": INITIAL_REPORTER_ID,
            "target_id": TARGET_ID,
            "outcome": "timeout",
        },
        "initial_compatible_hypotheses": (
            snapshot.compatible_hypothesis_count
        ),
        "initial_robust_safe_replicas": list(before_safe),
        "repeat_probe_score": _score_value(
            score_probe(snapshot, repeat_probe)
        ),
        "fresh_probe_score": _score_value(
            score_probe(snapshot, fresh_probe)
        ),
        "selected_probe": _probe_value(selected),
        "exhaustive_abstract_probe_audit": {
            "abstract_directed_pairs_scored": len(abstract_probes),
            "optimal_equivalence_class": [
                _probe_value(probe)
                for probe in optimal_equivalence_class
            ],
            "selection_from_complete_abstract_pair_set": True,
            "topology_legality_claimed": False,
        },
        "challenge_branches": list(branches),
        "ambiguity_tax": {
            "definition": (
                "replicas withheld from critical roles because at least "
                "one compatible hypothesis marks them faulty"
            ),
            "pre_challenge_excluded_endpoints": pre_excluded,
            "worst_case_post_challenge_excluded_endpoints": max(
                post_excluded_counts
            ),
            "guaranteed_safe_role_candidate_recovery": (
                min(
                    len(branch["robust_safe_replicas"])
                    for branch in branches
                )
                - len(before_safe)
            ),
        },
        "symmetric_policy_result": {
            "fresh_round_robin_reporter_id": (
                lowest_fresh_round_robin
            ),
            "minimax_reporter_id": selected.reporter_id,
            "fresh_round_robin_equals_minimax": (
                lowest_fresh_round_robin == selected.reporter_id
            ),
            "interpretation": (
                "for this one-conflict state, every unused reporter to "
                "the implicated target is in the exhaustively checked "
                "optimal equivalence class"
            ),
        },
    }


def _multi_conflict_probe_audit() -> dict[str, Any]:
    diagnosis = BoundedTwoModeDiagnosis(range(5), 2)
    snapshot = diagnosis.observe_many(
        (
            _observation("first-edge", 0, 1, "timeout"),
            _observation("second-edge", 1, 2, "timeout"),
        )
    )
    lowest_fresh = DiagnosticProbe(0, 2)
    independent_fresh = DiagnosticProbe(3, 2)
    alternative_fresh = DiagnosticProbe(4, 2)
    candidates = (
        lowest_fresh,
        independent_fresh,
        alternative_fresh,
    )
    selected = choose_minimax_probe(snapshot, candidates)
    robust_safe = frozenset(robust_safe_replicas(snapshot))
    fresh_safe_round_robin = min(
        (
            probe
            for probe in candidates
            if probe.reporter_id in robust_safe
        ),
        key=lambda probe: (probe.reporter_id, probe.target_id),
    )

    return {
        "scope": (
            "synthetic N=5,d=2 chain of two timeout conflicts; all "
            "candidate reporters are fresh for target 2"
        ),
        "accepted_observations": [
            {
                "reporter_id": 0,
                "target_id": 1,
                "outcome": "timeout",
            },
            {
                "reporter_id": 1,
                "target_id": 2,
                "outcome": "timeout",
            },
        ],
        "initial_compatible_hypotheses": (
            snapshot.compatible_hypothesis_count
        ),
        "lowest_fresh_reporter_score": _score_value(
            score_probe(snapshot, lowest_fresh)
        ),
        "minimax_probe_score": _score_value(
            score_probe(snapshot, independent_fresh)
        ),
        "selected_probe": _probe_value(selected),
        "fresh_safe_round_robin_probe": _probe_value(
            fresh_safe_round_robin
        ),
        "fresh_safe_round_robin_equals_minimax": (
            fresh_safe_round_robin == selected
        ),
        "interpretation": (
            "target-only freshness is insufficient when a reporter is "
            "already implicated by another conflict; excluding reporters "
            "that are not robust-safe ties the minimax choice in this "
            "fixture"
        ),
    }


def _value_aware_target_audit() -> dict[str, Any]:
    membership = tuple(range(7))
    diagnosis = BoundedTwoModeDiagnosis(membership, 2)
    snapshot = diagnosis.observe_many(
        (
            _observation("target1-response", 0, 1, "response"),
            _observation("target2-timeout", 0, 2, "timeout"),
            _observation("target1-timeout", 3, 1, "timeout"),
        )
    )
    role_values = {
        replica_id: 2 if replica_id == 2 else 1
        for replica_id in membership
    }
    candidates = tuple(
        DiagnosticProbe(reporter_id, target_id)
        for target_id in (1, 2)
        for reporter_id in (4, 5, 6)
    )
    uniform_value_minimax = choose_minimax_probe(
        snapshot,
        candidates,
    )
    selected = choose_minimax_probe(
        snapshot,
        candidates,
        role_values,
    )
    baseline_score = score_probe(
        snapshot,
        uniform_value_minimax,
        role_values,
    )
    selected_score = score_probe(
        snapshot,
        selected,
        role_values,
    )

    return {
        "scope": (
            "synthetic N=7,d=2 interacting-conflict fixture with one "
            "frozen higher-value future-role candidate"
        ),
        "accepted_observations": [
            {
                "reporter_id": observation.reporter_id,
                "target_id": observation.target_id,
                "outcome": observation.outcome,
            }
            for observation in snapshot.observations
        ],
        "compatible_hypotheses": [
            _hypothesis_value(hypothesis)
            for hypothesis in snapshot.hypotheses
        ],
        "robust_safe_replicas": list(
            robust_safe_replicas(snapshot)
        ),
        "ambiguous_targets": [
            target_id
            for target_id in membership
            if target_omission_status(snapshot, target_id)
            == "ambiguous"
        ],
        "role_values": [
            {
                "replica_id": replica_id,
                "future_role_value": role_values[replica_id],
            }
            for replica_id in membership
        ],
        "caller_admissible_probes": [
            _probe_value(probe) for probe in candidates
        ],
        "uniform_value_minimax_baseline": {
            "probe": _probe_value(uniform_value_minimax),
            "score_under_frozen_role_values": _score_value(
                baseline_score
            ),
        },
        "minimax_selection": {
            "probe": _probe_value(selected),
            "score": _score_value(selected_score),
        },
        "strict_value_advantage": {
            "guaranteed_safe_role_value_recovery_delta": (
                selected_score.guaranteed_safe_role_value_recovery
                - baseline_score.guaranteed_safe_role_value_recovery
            ),
            "worst_case_robust_safe_role_value_delta": (
                selected_score.worst_case_robust_safe_role_value
                - baseline_score.worst_case_robust_safe_role_value
            ),
            "worst_case_surviving_hypotheses_delta": (
                selected_score.worst_case_surviving_hypotheses
                - baseline_score.worst_case_surviving_hypotheses
            ),
        },
        "interpretation": (
            "robust-safe reporters tie within each target; adding frozen "
            "future-role values changes the minimax target and improves "
            "worst-case future-role recovery without changing the "
            "worst-case hypothesis count"
        ),
    }


def evaluate_minimax_rematching(
    kauri_revision: str,
    *,
    revision_verification: str = "caller_asserted",
) -> dict[str, Any]:
    """Return deterministic model evidence with explicit revision provenance."""

    if (
        not isinstance(kauri_revision, str)
        or _GIT_REVISION.fullmatch(kauri_revision) is None
    ):
        raise ValueError(
            "Kauri revision must be 40 lowercase hexadecimal characters"
        )
    if revision_verification not in (
        "caller_asserted",
        "verified_current_clean_head",
        "verified_current_head_dirty_override",
    ):
        raise ValueError("unknown Kauri revision verification state")

    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation_kind": (
            "certificate_based_minimax_rematching_model_evidence"
        ),
        "kauri_revision": kauri_revision,
        "kauri_revision_verification": revision_verification,
        "fixed_context": {
            "replica_ids": list(REPLICA_IDS),
            "diagnostic_fault_bound": DIAGNOSTIC_FAULT_BOUND,
            "static_disjoint_fault_modes": True,
            "distinct_authenticated_reporters": True,
            "post_gst_or_calibrated_timeout_semantics": True,
        },
        "theorem": {
            "statement": (
                "2d distinct target reports are sufficient in the worst "
                "case and 2d-1 can remain ambiguous"
            ),
            "scope": (
                "target persistent-omission status in the frozen "
                "two-mode Kauri diagnostic model"
            ),
            "policy_class": (
                "distinct authenticated reporters testing the same target"
            ),
        },
        "tight_bound_audit": [
            _tight_bound_audit(diagnostic_fault_bound)
            for diagnostic_fault_bound in (1, 2, 3)
        ],
        "canonical_minimax_game": _canonical_game(),
        "multi_conflict_probe_audit": _multi_conflict_probe_audit(),
        "value_aware_target_audit": _value_aware_target_audit(),
        "comparison_baseline": {
            "name": (
                "OptiTree provisional or persistent mutual-suspicion "
                "edge exclusion"
            ),
            "scope": (
                "single-edge candidate-exclusion behavior only; full "
                "OptiLog temporal conversion of unreciprocated suspicion "
                "is not reimplemented"
            ),
            "doi": "10.1145/3767295.3769342",
        },
        "claims_not_made": list(CLAIMS_NOT_MADE),
    }


def canonical_evaluation_json(evaluation: dict[str, Any]) -> str:
    """Serialize evidence with stable ordering and no timestamps."""

    return json.dumps(
        evaluation,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = (
    "canonical_evaluation_json",
    "evaluate_minimax_rematching",
)
