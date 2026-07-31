"""Fault-generic minimax ranking for candidate Kauri trees.

This module deliberately keeps joint fault hypotheses instead of flattening
them into one score per replica.  A hypothesis may represent any bounded
behavior model--crash, omission, equivocation, false reporting, selective
behavior, or a composition--provided that its potentially faulty identities
and its possible passive observation outcomes are supplied explicitly.

For each caller-supplied legal tree, structural exposure is the union of the
subtrees rooted at identities that may be faulty in a hypothesis.  This is a
conservative bound on contributions that those identities can suppress by
withholding aggregates; it is not a consensus-safety or liveness proof.

Selection is lexicographic:

1. minimize worst-case structural exposure across compatible hypotheses;
2. preserve the best predicted latency among equally robust trees;
3. minimize the worst-case hypotheses left by existing-traffic outcomes;
4. minimize churn and then use a canonical identifier tie-break.

The third step is passive: this module emits no probe or protocol message.
Set-valued outcome support lets a Byzantine identity choose any declared
outcome, so diagnostic value is scored adversarially rather than optimistically.

The caller remains responsible for evidence soundness, fault bounds, legal
topology generation, performance estimates, fixed consensus quorum rules,
and activation through Kauri's authorized epoch-transition path.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from itertools import islice


MAXIMUM_HYPOTHESES = 4_096
MAXIMUM_CANDIDATES = 100_000
MAXIMUM_MEMBERS = 4_096
MAXIMUM_IDENTIFIER_BYTES = 256
NO_PASSIVE_OBSERVATION = "kauri:no-passive-observation"


class RobustTopologyError(ValueError):
    """Raised when a robust-topology game is malformed."""


def _valid_nonnegative_integer(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and value >= 0
    )


def _validate_identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise RobustTopologyError(f"{label} must be a nonempty string")
    if len(value.encode("utf-8")) > MAXIMUM_IDENTIFIER_BYTES:
        raise RobustTopologyError(f"{label} exceeds the fixed byte bound")


def _validate_replica_set(replicas: frozenset[int], label: str) -> None:
    if type(replicas) is not frozenset:
        raise RobustTopologyError(f"{label} must be an exact frozenset")
    if len(replicas) > MAXIMUM_MEMBERS:
        raise RobustTopologyError(f"{label} exceeds the fixed member bound")
    if any(not _valid_nonnegative_integer(replica) for replica in replicas):
        raise RobustTopologyError(
            f"{label} must contain non-negative integer replica identifiers"
        )


@dataclass(frozen=True, order=True, slots=True)
class JointFaultHypothesis:
    """One bounded fault explanation retained as a joint identity set."""

    hypothesis_id: str
    faulty_replicas: frozenset[int]

    def __post_init__(self) -> None:
        _validate_identifier(self.hypothesis_id, "hypothesis identifier")
        _validate_replica_set(self.faulty_replicas, "faulty replicas")


@dataclass(frozen=True, order=True, slots=True)
class PassiveOutcomeSupport:
    """Outcomes one hypothesis may produce in normal tree traffic."""

    hypothesis_id: str
    outcomes: frozenset[str]

    def __post_init__(self) -> None:
        _validate_identifier(self.hypothesis_id, "hypothesis identifier")
        if type(self.outcomes) is not frozenset or not self.outcomes:
            raise RobustTopologyError(
                "passive outcomes must be a nonempty exact frozenset"
            )
        for outcome in self.outcomes:
            _validate_identifier(outcome, "passive outcome")


@dataclass(frozen=True, slots=True)
class KauriTreeCandidate:
    """One legal-tree candidate and its passive observation model."""

    candidate_id: str
    members_breadth_first: tuple[int, ...]
    fanout: int
    predicted_latency_us: int = 0
    churn_cost: int = 0
    passive_outcomes: tuple[PassiveOutcomeSupport, ...] = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.candidate_id, "candidate identifier")
        if type(self.members_breadth_first) is not tuple:
            raise RobustTopologyError(
                "tree membership must be an exact breadth-first tuple"
            )
        if not self.members_breadth_first:
            raise RobustTopologyError("tree membership must not be empty")
        if len(self.members_breadth_first) > MAXIMUM_MEMBERS:
            raise RobustTopologyError(
                "tree membership exceeds the fixed member bound"
            )
        if any(
            not _valid_nonnegative_integer(replica)
            for replica in self.members_breadth_first
        ):
            raise RobustTopologyError(
                "tree members must be non-negative integer identifiers"
            )
        if len(set(self.members_breadth_first)) != len(
            self.members_breadth_first
        ):
            raise RobustTopologyError("tree membership must be unique")
        if (
            not _valid_nonnegative_integer(self.fanout)
            or self.fanout == 0
        ):
            raise RobustTopologyError("tree fanout must be a positive integer")
        if not _valid_nonnegative_integer(self.predicted_latency_us):
            raise RobustTopologyError(
                "predicted latency must be a non-negative integer"
            )
        if not _valid_nonnegative_integer(self.churn_cost):
            raise RobustTopologyError(
                "churn cost must be a non-negative integer"
            )
        if type(self.passive_outcomes) is not tuple:
            raise RobustTopologyError(
                "passive outcomes must be an exact tuple"
            )
        if any(
            type(support) is not PassiveOutcomeSupport
            for support in self.passive_outcomes
        ):
            raise RobustTopologyError(
                "passive outcomes must contain exact support values"
            )
        support_ids = tuple(
            support.hypothesis_id for support in self.passive_outcomes
        )
        if len(set(support_ids)) != len(support_ids):
            raise RobustTopologyError(
                "passive outcome hypothesis identifiers must be unique"
            )


@dataclass(frozen=True, slots=True)
class PassiveObservationBranch:
    """Hypotheses surviving one adversarially possible passive outcome."""

    outcome: str
    surviving_hypothesis_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RobustTopologyScore:
    """Worst-case containment, performance, and passive-information score."""

    candidate_id: str
    exposure_by_hypothesis: tuple[tuple[str, int], ...]
    worst_case_exposure: int
    predicted_latency_us: int
    passive_branches: tuple[PassiveObservationBranch, ...]
    worst_case_surviving_hypotheses: int
    guaranteed_hypothesis_elimination: int
    churn_cost: int


@dataclass(frozen=True, slots=True)
class RobustTopologySelection:
    """Canonical selected tree plus auditable scores for every candidate."""

    selected: KauriTreeCandidate
    score: RobustTopologyScore
    candidate_scores: tuple[RobustTopologyScore, ...]


def _bounded_exact_tuple(
    values: Iterable[object],
    *,
    maximum: int,
    label: str,
) -> tuple[object, ...]:
    if isinstance(values, (str, bytes)):
        raise RobustTopologyError(f"{label} must be an iterable")
    try:
        bounded = tuple(islice(iter(values), maximum + 1))
    except TypeError as error:
        raise RobustTopologyError(f"{label} must be an iterable") from error
    if not bounded:
        singular = {
            "hypotheses": "hypothesis",
            "candidates": "candidate",
        }.get(label, label)
        raise RobustTopologyError(f"at least one {singular} is required")
    if len(bounded) > maximum:
        raise RobustTopologyError(f"{label} exceed the fixed count bound")
    return bounded


def _canonical_hypotheses(
    hypotheses: Iterable[JointFaultHypothesis],
) -> tuple[JointFaultHypothesis, ...]:
    bounded = _bounded_exact_tuple(
        hypotheses,
        maximum=MAXIMUM_HYPOTHESES,
        label="hypotheses",
    )
    if any(type(value) is not JointFaultHypothesis for value in bounded):
        raise RobustTopologyError(
            "hypotheses must contain exact JointFaultHypothesis values"
        )
    canonical = tuple(sorted(bounded))
    identifiers = tuple(value.hypothesis_id for value in canonical)
    if len(set(identifiers)) != len(identifiers):
        raise RobustTopologyError("hypothesis identifiers must be unique")
    return canonical


def _canonical_candidates(
    candidates: Iterable[KauriTreeCandidate],
) -> tuple[KauriTreeCandidate, ...]:
    bounded = _bounded_exact_tuple(
        candidates,
        maximum=MAXIMUM_CANDIDATES,
        label="candidates",
    )
    if any(type(value) is not KauriTreeCandidate for value in bounded):
        raise RobustTopologyError(
            "candidates must contain exact KauriTreeCandidate values"
        )
    canonical = tuple(sorted(bounded, key=lambda value: value.candidate_id))
    identifiers = tuple(value.candidate_id for value in canonical)
    if len(set(identifiers)) != len(identifiers):
        raise RobustTopologyError("candidate identifiers must be unique")

    membership = frozenset(canonical[0].members_breadth_first)
    fanout = canonical[0].fanout
    for candidate in canonical[1:]:
        if frozenset(candidate.members_breadth_first) != membership:
            raise RobustTopologyError(
                "candidate trees must have exactly the same membership"
            )
        if candidate.fanout != fanout:
            raise RobustTopologyError(
                "candidate trees must have exactly the same fanout"
            )
    return canonical


@lru_cache(maxsize=131_072)
def _subtree_members(
    members_breadth_first: tuple[int, ...],
    fanout: int,
    root_position: int,
) -> frozenset[int]:
    subtree: set[int] = set()
    pending = [root_position]
    while pending:
        position = pending.pop()
        if position >= len(members_breadth_first):
            continue
        subtree.add(members_breadth_first[position])
        first_child = (fanout * position) + 1
        pending.extend(
            range(
                first_child,
                min(
                    first_child + fanout,
                    len(members_breadth_first),
                ),
            )
        )
    return frozenset(subtree)


def structural_exposure(
    candidate: KauriTreeCandidate,
    hypothesis: JointFaultHypothesis,
) -> int:
    """Count the union of subtrees a compatible faulty set can suppress."""

    if type(candidate) is not KauriTreeCandidate:
        raise RobustTopologyError(
            "candidate must be an exact KauriTreeCandidate"
        )
    if type(hypothesis) is not JointFaultHypothesis:
        raise RobustTopologyError(
            "hypothesis must be an exact JointFaultHypothesis"
        )
    positions = {
        replica: position
        for position, replica in enumerate(candidate.members_breadth_first)
    }
    foreign = hypothesis.faulty_replicas.difference(positions)
    if foreign:
        raise RobustTopologyError(
            "hypothesis contains a faulty replica outside tree membership"
        )

    exposed: set[int] = set()
    for replica in hypothesis.faulty_replicas:
        exposed.update(
            _subtree_members(
                candidate.members_breadth_first,
                candidate.fanout,
                positions[replica],
            )
        )
    return len(exposed)


def _outcomes_by_hypothesis(
    hypotheses: tuple[JointFaultHypothesis, ...],
    candidate: KauriTreeCandidate,
) -> dict[str, frozenset[str]]:
    hypothesis_ids = {value.hypothesis_id for value in hypotheses}
    if not candidate.passive_outcomes:
        return {
            hypothesis_id: frozenset({NO_PASSIVE_OBSERVATION})
            for hypothesis_id in hypothesis_ids
        }
    support = {
        value.hypothesis_id: value.outcomes
        for value in candidate.passive_outcomes
    }
    if set(support) != hypothesis_ids:
        raise RobustTopologyError(
            "passive outcomes must cover exactly the compatible hypotheses"
        )
    return support


def _score_canonical(
    hypotheses: tuple[JointFaultHypothesis, ...],
    candidate: KauriTreeCandidate,
) -> RobustTopologyScore:
    exposure = tuple(
        (
            hypothesis.hypothesis_id,
            structural_exposure(candidate, hypothesis),
        )
        for hypothesis in hypotheses
    )
    supports = _outcomes_by_hypothesis(hypotheses, candidate)
    outcomes = sorted(
        outcome
        for hypothesis_outcomes in supports.values()
        for outcome in hypothesis_outcomes
    )
    branches = tuple(
        PassiveObservationBranch(
            outcome=outcome,
            surviving_hypothesis_ids=tuple(
                sorted(
                    hypothesis_id
                    for hypothesis_id, possible in supports.items()
                    if outcome in possible
                )
            ),
        )
        for outcome in dict.fromkeys(outcomes)
    )
    worst_survivors = max(
        len(branch.surviving_hypothesis_ids) for branch in branches
    )
    return RobustTopologyScore(
        candidate_id=candidate.candidate_id,
        exposure_by_hypothesis=exposure,
        worst_case_exposure=max(value for _, value in exposure),
        predicted_latency_us=candidate.predicted_latency_us,
        passive_branches=branches,
        worst_case_surviving_hypotheses=worst_survivors,
        guaranteed_hypothesis_elimination=(
            len(hypotheses) - worst_survivors
        ),
        churn_cost=candidate.churn_cost,
    )


def score_topology(
    hypotheses: Iterable[JointFaultHypothesis],
    candidate: KauriTreeCandidate,
) -> RobustTopologyScore:
    """Score one candidate against every compatible fault explanation."""

    canonical = _canonical_hypotheses(hypotheses)
    if type(candidate) is not KauriTreeCandidate:
        raise RobustTopologyError(
            "candidate must be an exact KauriTreeCandidate"
        )
    return _score_canonical(canonical, candidate)


def _selection_key(score: RobustTopologyScore) -> tuple[int, int, int, int, str]:
    return (
        score.worst_case_exposure,
        score.predicted_latency_us,
        score.worst_case_surviving_hypotheses,
        score.churn_cost,
        score.candidate_id,
    )


def select_robust_topology(
    hypotheses: Iterable[JointFaultHypothesis],
    candidates: Iterable[KauriTreeCandidate],
) -> RobustTopologySelection:
    """Choose a canonical no-regret passive-diagnosis tree."""

    canonical_hypotheses = _canonical_hypotheses(hypotheses)
    canonical_candidates = _canonical_candidates(candidates)
    membership = frozenset(canonical_candidates[0].members_breadth_first)
    for hypothesis in canonical_hypotheses:
        if not hypothesis.faulty_replicas.issubset(membership):
            raise RobustTopologyError(
                "hypothesis contains a faulty replica outside tree membership"
            )

    scored = tuple(
        _score_canonical(canonical_hypotheses, candidate)
        for candidate in canonical_candidates
    )
    selected_score = min(scored, key=_selection_key)
    selected = canonical_candidates[
        next(
            index
            for index, score in enumerate(scored)
            if score.candidate_id == selected_score.candidate_id
        )
    ]
    return RobustTopologySelection(
        selected=selected,
        score=selected_score,
        candidate_scores=scored,
    )


__all__ = (
    "JointFaultHypothesis",
    "KauriTreeCandidate",
    "PassiveObservationBranch",
    "PassiveOutcomeSupport",
    "RobustTopologyError",
    "RobustTopologyScore",
    "RobustTopologySelection",
    "score_topology",
    "select_robust_topology",
    "structural_exposure",
)
