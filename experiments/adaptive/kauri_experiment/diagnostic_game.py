"""Minimax probe ranking for bounded Kauri diagnosis experiments.

The defender chooses one authenticated reporter--target probe.  The
adversary then chooses any outcome that remains compatible with the current
two-mode hypothesis set.  Probe selection maximizes the worst-case recovery
of replicas that are safe under every surviving hypothesis and then minimizes
the worst-case number of surviving hypotheses.

The caller supplies the admissible probe set and remains responsible for
Kauri topology, epoch, role, and activation constraints.  This module is
experiment-only decision support.  It accepts no injected fault labels and
has no authority over membership, quorum, votes, certificates, locks,
commits, or active topology.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from itertools import islice
from typing import Literal, TypeAlias

from .diagnosis import (
    BoundedTwoModeDiagnosis,
    DiagnosisError,
    DiagnosisSnapshot,
    DiagnosticObservation,
    FaultHypothesis,
    MAXIMUM_DIAGNOSTIC_FAULT_BOUND,
)


MAXIMUM_PROBES = 31 * 30

TargetOmissionStatus: TypeAlias = Literal[
    "ambiguous",
    "persistent_omitter",
    "not_persistent_omitter",
]


@dataclass(frozen=True, order=True, slots=True)
class DiagnosticProbe:
    """One abstract authenticated reporter--target rematch."""

    reporter_id: int
    target_id: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.reporter_id, bool)
            or not isinstance(self.reporter_id, int)
            or isinstance(self.target_id, bool)
            or not isinstance(self.target_id, int)
        ):
            raise DiagnosisError("probe endpoints must be integers")
        if self.reporter_id < 0 or self.target_id < 0:
            raise DiagnosisError("probe endpoints must be non-negative")
        if self.reporter_id == self.target_id:
            raise DiagnosisError(
                "probe reporter and target must be distinct"
            )


@dataclass(frozen=True, slots=True)
class DiagnosticProbeBranch:
    """One feasible adversarial outcome for a candidate probe."""

    outcome: Literal["response", "timeout"]
    surviving_hypotheses: int
    robust_safe_replicas: tuple[int, ...]
    robust_safe_role_value: int


@dataclass(frozen=True, slots=True)
class DiagnosticProbeScore:
    """Deterministic worst-case score for one candidate probe."""

    probe: DiagnosticProbe
    branches: tuple[DiagnosticProbeBranch, ...]
    current_robust_safe_replicas: tuple[int, ...]
    current_robust_safe_role_value: int
    worst_case_surviving_hypotheses: int
    guaranteed_hypothesis_elimination: int
    worst_case_robust_safe_role_value: int
    guaranteed_safe_role_value_recovery: int
    fresh_reporter_for_target: bool


@lru_cache(maxsize=128)
def _validated_membership(
    snapshot: DiagnosisSnapshot,
) -> tuple[int, ...]:
    if type(snapshot) is not DiagnosisSnapshot:
        raise DiagnosisError("snapshot must be an exact DiagnosisSnapshot")
    if not snapshot.hypotheses:
        raise DiagnosisError("snapshot must contain compatible hypotheses")
    membership = tuple(count.replica_id for count in snapshot.mode_counts)
    if not membership or len(set(membership)) != len(membership):
        raise DiagnosisError(
            "snapshot mode counts must define a unique membership"
        )

    maximum_fault_bound = min(
        MAXIMUM_DIAGNOSTIC_FAULT_BOUND,
        len(membership),
    )
    for diagnostic_fault_bound in range(maximum_fault_bound + 1):
        diagnosis = BoundedTwoModeDiagnosis(
            membership,
            diagnostic_fault_bound,
        )
        try:
            reproduced = diagnosis.observe_many(snapshot.observations)
        except DiagnosisError:
            continue
        if reproduced == snapshot:
            return membership
    raise DiagnosisError(
        "snapshot is not reproducible from its accepted observations"
    )


def _safe_replicas(
    hypotheses: tuple[FaultHypothesis, ...],
    membership: tuple[int, ...],
) -> tuple[int, ...]:
    return tuple(
        replica_id
        for replica_id in membership
        if all(
            replica_id not in hypothesis.false_reporters
            and replica_id not in hypothesis.persistent_omitters
            for hypothesis in hypotheses
        )
    )


def robust_safe_replicas(
    snapshot: DiagnosisSnapshot,
) -> tuple[int, ...]:
    """Return replicas fault-free in every compatible hypothesis."""

    membership = _validated_membership(snapshot)
    return _safe_replicas(snapshot.hypotheses, membership)


def target_omission_status(
    snapshot: DiagnosisSnapshot,
    target_id: int,
) -> TargetOmissionStatus:
    """Return only what all compatible hypotheses establish for a target."""

    _validated_membership(snapshot)
    counts = snapshot.mode_count_for(target_id)
    if (
        counts.persistent_omitter_hypotheses
        == counts.compatible_hypotheses
    ):
        return "persistent_omitter"
    if counts.persistent_omitter_hypotheses == 0:
        return "not_persistent_omitter"
    return "ambiguous"


def _canonical_role_values(
    membership: tuple[int, ...],
    role_values: Mapping[int, int] | None,
) -> dict[int, int]:
    if role_values is None:
        return {replica_id: 1 for replica_id in membership}
    if not isinstance(role_values, Mapping):
        raise DiagnosisError("role values must be a mapping")
    if set(role_values) != set(membership):
        raise DiagnosisError(
            "role values must contain exactly the diagnosis membership"
        )

    canonical: dict[int, int] = {}
    for replica_id in membership:
        value = role_values[replica_id]
        if isinstance(value, bool) or not isinstance(value, int):
            raise DiagnosisError("role values must be integers")
        if value < 0:
            raise DiagnosisError("role values must be non-negative")
        canonical[replica_id] = value
    return canonical


def _role_value(
    replicas: tuple[int, ...],
    role_values: Mapping[int, int],
) -> int:
    return sum(role_values[replica_id] for replica_id in replicas)


def score_probe(
    snapshot: DiagnosisSnapshot,
    probe: DiagnosticProbe,
    role_values: Mapping[int, int] | None = None,
) -> DiagnosticProbeScore:
    """Score a probe against the worst compatible response."""

    membership = _validated_membership(snapshot)
    membership_set = frozenset(membership)
    if type(probe) is not DiagnosticProbe:
        raise DiagnosisError("probe must be an exact DiagnosticProbe")
    if (
        probe.reporter_id not in membership_set
        or probe.target_id not in membership_set
    ):
        raise DiagnosisError("probe endpoint is outside diagnosis membership")

    canonical_role_values = _canonical_role_values(
        membership,
        role_values,
    )
    current_safe = _safe_replicas(snapshot.hypotheses, membership)
    current_safe_value = _role_value(
        current_safe,
        canonical_role_values,
    )

    branches: list[DiagnosticProbeBranch] = []
    for outcome in ("response", "timeout"):
        counterfactual = DiagnosticObservation(
            attempt_id="diagnostic-game-counterfactual",
            reporter_id=probe.reporter_id,
            target_id=probe.target_id,
            outcome=outcome,
        )
        survivors = tuple(
            hypothesis
            for hypothesis in snapshot.hypotheses
            if hypothesis.compatible_with(counterfactual)
        )
        if not survivors:
            continue
        safe = _safe_replicas(survivors, membership)
        branches.append(
            DiagnosticProbeBranch(
                outcome=outcome,
                surviving_hypotheses=len(survivors),
                robust_safe_replicas=safe,
                robust_safe_role_value=_role_value(
                    safe,
                    canonical_role_values,
                ),
            )
        )

    if not branches:
        raise DiagnosisError("probe has no compatible outcome")

    canonical_branches = tuple(branches)
    worst_hypotheses = max(
        branch.surviving_hypotheses for branch in canonical_branches
    )
    worst_safe_value = min(
        branch.robust_safe_role_value for branch in canonical_branches
    )
    prior_reporters = {
        observation.reporter_id
        for observation in snapshot.observations
        if observation.target_id == probe.target_id
    }

    return DiagnosticProbeScore(
        probe=probe,
        branches=canonical_branches,
        current_robust_safe_replicas=current_safe,
        current_robust_safe_role_value=current_safe_value,
        worst_case_surviving_hypotheses=worst_hypotheses,
        guaranteed_hypothesis_elimination=(
            snapshot.compatible_hypothesis_count - worst_hypotheses
        ),
        worst_case_robust_safe_role_value=worst_safe_value,
        guaranteed_safe_role_value_recovery=(
            worst_safe_value - current_safe_value
        ),
        fresh_reporter_for_target=(
            probe.reporter_id not in prior_reporters
        ),
    )


def _selection_key(score: DiagnosticProbeScore) -> tuple[int, ...]:
    return (
        -score.guaranteed_safe_role_value_recovery,
        score.worst_case_surviving_hypotheses,
        0 if score.fresh_reporter_for_target else 1,
        score.probe.target_id,
        score.probe.reporter_id,
    )


def choose_minimax_probe(
    snapshot: DiagnosisSnapshot,
    probes: Iterable[DiagnosticProbe],
    role_values: Mapping[int, int] | None = None,
) -> DiagnosticProbe:
    """Rank a caller-supplied admissible set by worst-case outcome."""

    if isinstance(probes, (str, bytes)):
        raise DiagnosisError("probes must be an iterable of DiagnosticProbe")
    try:
        candidates = tuple(islice(iter(probes), MAXIMUM_PROBES + 1))
    except TypeError as error:
        raise DiagnosisError(
            "probes must be an iterable of DiagnosticProbe"
        ) from error
    if not candidates:
        raise DiagnosisError("at least one diagnostic probe is required")
    if len(candidates) > MAXIMUM_PROBES:
        raise DiagnosisError(
            f"diagnostic probes cannot exceed {MAXIMUM_PROBES}"
        )
    if any(type(probe) is not DiagnosticProbe for probe in candidates):
        raise DiagnosisError("all probes must be exact DiagnosticProbe values")

    unique_candidates = tuple(sorted(set(candidates)))
    scores = tuple(
        score_probe(snapshot, probe, role_values)
        for probe in unique_candidates
    )
    return min(scores, key=_selection_key).probe


__all__ = (
    "DiagnosticProbe",
    "DiagnosticProbeBranch",
    "DiagnosticProbeScore",
    "TargetOmissionStatus",
    "choose_minimax_probe",
    "robust_safe_replicas",
    "score_probe",
    "target_omission_status",
)
