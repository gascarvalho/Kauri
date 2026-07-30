"""Frozen synthetic comparison for the bounded diagnosis oracle.

This evaluator is intentionally small.  It replays three matched N=7 traces
through the exact two-mode diagnosis and, separately, through a target-only
``+1/-1`` score proxy matching the shape of the existing scalar reputation.
The proxy is a comparison aid, not a Byzantine diagnosis.

Fault identities are consulted only by the post-hoc scorer after a trace has
finished.  They are never passed to :class:`BoundedTwoModeDiagnosis`.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
from typing import Any

from .diagnosis import (
    BoundedTwoModeDiagnosis,
    DiagnosisSnapshot,
    DiagnosticObservation,
    FaultHypothesis,
    ReplicaModeCounts,
)


SCHEMA_VERSION = 1
REPLICA_IDS = tuple(range(7))
DIAGNOSTIC_FAULT_BOUND = 1
OBSERVATIONS_PER_SCENARIO = 3
SCENARIO_NAMES = (
    "clean",
    "static_false_reporter",
    "static_persistent_omitter",
)
SCALAR_PROXY_KIND = "target_only_scalar_reputation_proxy"

CLAIMS_NOT_MADE = (
    "synthetic traces are not live protocol evidence",
    "one frozen trace per scenario is not statistical evidence",
    "the target-only scalar baseline is a comparison proxy, not diagnosis",
    "no active rematching or topology policy is evaluated",
    "no throughput, liveness, or consensus-safety conclusion is evaluated",
)


@dataclass(frozen=True, slots=True)
class _PostHocTruth:
    false_reporters: tuple[int, ...] = ()
    persistent_omitters: tuple[int, ...] = ()

    @property
    def hypothesis(self) -> FaultHypothesis:
        return FaultHypothesis(
            false_reporters=self.false_reporters,
            persistent_omitters=self.persistent_omitters,
        )

    @property
    def faulty_identities(self) -> frozenset[int]:
        return frozenset(
            self.false_reporters + self.persistent_omitters
        )


def _observation(
    scenario_name: str,
    ordinal: int,
    reporter_id: int,
    outcome: str,
) -> DiagnosticObservation:
    return DiagnosticObservation(
        attempt_id=f"{scenario_name}-attempt-{ordinal}",
        reporter_id=reporter_id,
        target_id=4,
        outcome=outcome,
    )


# The traces and validator-owned labels are deliberately separate constants.
# Trace evaluation below receives only the observation tuple.
_FROZEN_TRACES = (
    (
        "clean",
        (
            _observation("clean", 1, 1, "response"),
            _observation("clean", 2, 2, "response"),
            _observation("clean", 3, 3, "response"),
        ),
    ),
    (
        "static_false_reporter",
        (
            _observation("static_false_reporter", 1, 1, "timeout"),
            _observation("static_false_reporter", 2, 2, "response"),
            _observation("static_false_reporter", 3, 3, "response"),
        ),
    ),
    (
        "static_persistent_omitter",
        (
            _observation(
                "static_persistent_omitter",
                1,
                1,
                "timeout",
            ),
            _observation(
                "static_persistent_omitter",
                2,
                2,
                "timeout",
            ),
            _observation(
                "static_persistent_omitter",
                3,
                3,
                "timeout",
            ),
        ),
    ),
)

_POST_HOC_TRUTH = {
    "clean": _PostHocTruth(),
    "static_false_reporter": _PostHocTruth(false_reporters=(1,)),
    "static_persistent_omitter": _PostHocTruth(
        persistent_omitters=(4,)
    ),
}


def _fraction_value(value: Fraction) -> dict[str, int]:
    return {
        "numerator": value.numerator,
        "denominator": value.denominator,
    }


def _hypothesis_value(hypothesis: FaultHypothesis) -> dict[str, list[int]]:
    return {
        "false_reporters": list(hypothesis.false_reporters),
        "persistent_omitters": list(hypothesis.persistent_omitters),
    }


def _mode_count_value(counts: ReplicaModeCounts) -> dict[str, Any]:
    return {
        "replica_id": counts.replica_id,
        "compatible_hypotheses": counts.compatible_hypotheses,
        "false_reporter_hypotheses": (
            counts.false_reporter_hypotheses
        ),
        "persistent_omitter_hypotheses": (
            counts.persistent_omitter_hypotheses
        ),
        "false_reporter_mass": _fraction_value(
            counts.false_reporter_mass
        ),
        "persistent_omitter_mass": _fraction_value(
            counts.persistent_omitter_mass
        ),
        "definitive_mode": counts.definitive_mode,
    }


def _observation_value(
    observation: DiagnosticObservation,
) -> dict[str, int | str]:
    return {
        "attempt_id": observation.attempt_id,
        "reporter_id": observation.reporter_id,
        "target_id": observation.target_id,
        "outcome": observation.outcome,
    }


def _score_values(scores: dict[int, int]) -> list[dict[str, int]]:
    return [
        {
            "replica_id": replica_id,
            "score": scores[replica_id],
        }
        for replica_id in REPLICA_IDS
    ]


def _step_value(
    ordinal: int,
    observation: DiagnosticObservation,
    snapshot: DiagnosisSnapshot,
    scalar_scores: dict[int, int],
    scalar_delta: int,
) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "observation": _observation_value(observation),
        "compatible_hypothesis_count": (
            snapshot.compatible_hypothesis_count
        ),
        "compatible_hypotheses": [
            _hypothesis_value(hypothesis)
            for hypothesis in snapshot.hypotheses
        ],
        "replica_mode_counts": [
            _mode_count_value(counts)
            for counts in snapshot.mode_counts
        ],
        "scalar_proxy": {
            "kind": SCALAR_PROXY_KIND,
            "applied_to_target_id": observation.target_id,
            "delta": scalar_delta,
            "resulting_target_score": scalar_scores[
                observation.target_id
            ],
            "scores": _score_values(scalar_scores),
        },
    }


def _evaluate_trace(
    scenario_name: str,
    observations: tuple[DiagnosticObservation, ...],
) -> tuple[dict[str, Any], DiagnosisSnapshot, dict[int, int]]:
    """Evaluate one trace without accepting any validator label."""

    diagnosis = BoundedTwoModeDiagnosis(
        REPLICA_IDS,
        DIAGNOSTIC_FAULT_BOUND,
    )
    scalar_scores = {replica_id: 0 for replica_id in REPLICA_IDS}
    steps: list[dict[str, Any]] = []
    snapshot = diagnosis.snapshot()

    for ordinal, observation in enumerate(observations, start=1):
        snapshot = diagnosis.observe(observation)
        scalar_delta = 1 if observation.outcome == "response" else -1
        scalar_scores[observation.target_id] += scalar_delta
        steps.append(
            _step_value(
                ordinal,
                observation,
                snapshot,
                scalar_scores,
                scalar_delta,
            )
        )

    return (
        {
            "name": scenario_name,
            "steps": steps,
            "final_compatible_hypothesis_count": (
                snapshot.compatible_hypothesis_count
            ),
            "final_scalar_proxy_scores": _score_values(scalar_scores),
        },
        snapshot,
        scalar_scores,
    )


def _post_hoc_score(
    snapshot: DiagnosisSnapshot,
    scalar_scores: dict[int, int],
    truth: _PostHocTruth,
) -> dict[str, Any]:
    """Score final outputs after diagnosis, using validator-owned labels."""

    diagnosed_false_reporters = tuple(
        counts.replica_id
        for counts in snapshot.mode_counts
        if counts.definitive_mode == "false_reporter"
    )
    diagnosed_persistent_omitters = tuple(
        counts.replica_id
        for counts in snapshot.mode_counts
        if counts.definitive_mode == "persistent_omitter"
    )
    negative_score_replicas = tuple(
        replica_id
        for replica_id in REPLICA_IDS
        if scalar_scores[replica_id] < 0
    )
    truth_hypothesis_retained = truth.hypothesis in snapshot.hypotheses

    return {
        "scope": (
            "validator-only post-hoc scoring; these identities were not "
            "diagnosis inputs"
        ),
        "validator_ground_truth": {
            "false_reporters": list(truth.false_reporters),
            "persistent_omitters": list(truth.persistent_omitters),
        },
        "truth_hypothesis_retained": truth_hypothesis_retained,
        "diagnosed_false_reporters": list(diagnosed_false_reporters),
        "diagnosed_persistent_omitters": list(
            diagnosed_persistent_omitters
        ),
        "diagnosis_exact_identity_match": (
            snapshot.hypotheses == (truth.hypothesis,)
        ),
        "scalar_negative_score_replicas": list(
            negative_score_replicas
        ),
        "scalar_negative_score_identity_match": (
            frozenset(negative_score_replicas)
            == truth.faulty_identities
        ),
    }


def evaluate_frozen_scenarios() -> dict[str, Any]:
    """Return the deterministic three-scenario synthetic evaluation."""

    evaluated: list[
        tuple[dict[str, Any], DiagnosisSnapshot, dict[int, int]]
    ] = [
        _evaluate_trace(scenario_name, observations)
        for scenario_name, observations in _FROZEN_TRACES
    ]

    scenarios: list[dict[str, Any]] = []
    for (
        scenario_result,
        final_snapshot,
        final_scalar_scores,
    ) in evaluated:
        scenario_name = scenario_result["name"]
        truth = _POST_HOC_TRUTH[scenario_name]
        scenario_result["post_hoc_ground_truth_scoring"] = (
            _post_hoc_score(
                final_snapshot,
                final_scalar_scores,
                truth,
            )
        )
        scenarios.append(scenario_result)

    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation_kind": (
            "synthetic_bounded_two_mode_diagnosis_comparison"
        ),
        "fixed_context": {
            "replica_ids": list(REPLICA_IDS),
            "diagnostic_fault_bound": DIAGNOSTIC_FAULT_BOUND,
            "observations_per_scenario": OBSERVATIONS_PER_SCENARIO,
        },
        "scalar_baseline": {
            "kind": SCALAR_PROXY_KIND,
            "update_rule": (
                "target score += 1 for response; target score -= 1 "
                "for timeout"
            ),
            "claim_boundary": (
                "comparison proxy only; target scores are not Byzantine "
                "mode diagnoses"
            ),
        },
        "scenarios": scenarios,
        "claims_not_made": list(CLAIMS_NOT_MADE),
    }


def canonical_evaluation_json(evaluation: dict[str, Any]) -> str:
    """Serialize an evaluation with stable ordering and no timestamps."""

    return json.dumps(
        evaluation,
        sort_keys=True,
        separators=(",", ":"),
    )
