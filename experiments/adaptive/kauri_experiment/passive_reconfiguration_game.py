"""Finite-horizon passive diagnosis over safe Kauri tree choices.

The current robust-topology selector is deliberately myopic: after minimizing
worst-case structural exposure and predicted latency, it chooses the tree that
minimizes the hypotheses surviving the next passive observation.  This module
keeps the same primary objectives but plans over several future epochs.

At each belief state the defender may choose only a tree with minimum current
worst-case exposure and, among those, minimum predicted latency.  A Byzantine
adversary then chooses any passive outcome allowed by the actual compatible
hypothesis.  The exact dynamic program minimizes the worst-case number of
hypotheses left after a bounded horizon.

The solver emits no diagnostic probe or protocol message; it only returns a
choice.  Its outcome model must be supplied from existing tree traffic, and
its candidates must already be legal for Kauri's authorized epoch transition.
It has no authority over membership, quorum, votes, certificates, locks,
commits, or active topology.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations, islice

from .robust_topology import (
    JointFaultHypothesis,
    KauriTreeCandidate,
    NO_PASSIVE_OBSERVATION,
    RobustTopologyError,
    select_robust_topology,
)


MAXIMUM_LOOKAHEAD_HYPOTHESES = 16
MAXIMUM_LOOKAHEAD_CANDIDATES = 512
MAXIMUM_LOOKAHEAD_HORIZON = 8


@dataclass(frozen=True, slots=True)
class ReconfigurationOutcomeBranch:
    """One adversarial outcome and its optimal continuation bound."""

    outcome: str
    surviving_hypothesis_ids: tuple[str, ...]
    terminal_hypothesis_bound: int


@dataclass(frozen=True, slots=True)
class LookaheadTopologyScore:
    """Finite-horizon score for one possible first tree."""

    candidate_id: str
    primary_optimal: bool
    worst_case_exposure: int
    predicted_latency_us: int
    immediate_worst_surviving_hypotheses: int
    worst_case_terminal_hypotheses: int
    churn_cost: int
    branches: tuple[ReconfigurationOutcomeBranch, ...]


@dataclass(frozen=True, slots=True)
class LookaheadTopologySelection:
    """Canonical exact minimax first action and its proof scores."""

    selected: KauriTreeCandidate
    score: LookaheadTopologyScore
    candidate_scores: tuple[LookaheadTopologyScore, ...]
    horizon: int
    belief_states_evaluated: int


@dataclass(frozen=True, slots=True)
class GreedyPolicyEvaluation:
    """Worst-case terminal ambiguity of repeated one-step selection."""

    selected: KauriTreeCandidate
    immediate_worst_surviving_hypotheses: int
    worst_case_terminal_hypotheses: int
    horizon: int
    belief_states_evaluated: int


@dataclass(frozen=True, slots=True)
class _PreparedGame:
    hypotheses: tuple[JointFaultHypothesis, ...]
    candidates: tuple[KauriTreeCandidate, ...]
    exposure: tuple[tuple[int, ...], ...]
    outcome_masks: tuple[tuple[tuple[str, int], ...], ...]
    full_belief: int


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
        raise RobustTopologyError(
            f"{label} must be an iterable"
        ) from error
    if not bounded:
        singular = {
            "hypotheses": "hypothesis",
            "candidates": "candidate",
        }.get(label, label)
        raise RobustTopologyError(f"at least one {singular} is required")
    if len(bounded) > maximum:
        raise RobustTopologyError(f"{label} exceed the fixed count bound")
    return bounded


def _validate_horizon(horizon: object) -> int:
    if (
        isinstance(horizon, bool)
        or not isinstance(horizon, int)
        or horizon < 1
        or horizon > MAXIMUM_LOOKAHEAD_HORIZON
    ):
        raise RobustTopologyError(
            "lookahead horizon must be an integer between 1 and "
            f"{MAXIMUM_LOOKAHEAD_HORIZON}"
        )
    return horizon


def _prepare_game(
    hypotheses: Iterable[JointFaultHypothesis],
    candidates: Iterable[KauriTreeCandidate],
) -> _PreparedGame:
    bounded_hypotheses = _bounded_exact_tuple(
        hypotheses,
        maximum=MAXIMUM_LOOKAHEAD_HYPOTHESES,
        label="hypotheses",
    )
    if any(
        type(value) is not JointFaultHypothesis
        for value in bounded_hypotheses
    ):
        raise RobustTopologyError(
            "hypotheses must contain exact JointFaultHypothesis values"
        )
    canonical_hypotheses = tuple(sorted(bounded_hypotheses))

    bounded_candidates = _bounded_exact_tuple(
        candidates,
        maximum=MAXIMUM_LOOKAHEAD_CANDIDATES,
        label="candidates",
    )
    if any(
        type(value) is not KauriTreeCandidate
        for value in bounded_candidates
    ):
        raise RobustTopologyError(
            "candidates must contain exact KauriTreeCandidate values"
        )
    canonical_candidates = tuple(
        sorted(
            bounded_candidates,
            key=lambda value: value.candidate_id,
        )
    )

    validation = select_robust_topology(
        canonical_hypotheses,
        canonical_candidates,
    )
    scores = {
        score.candidate_id: score for score in validation.candidate_scores
    }
    hypothesis_ids = tuple(
        hypothesis.hypothesis_id for hypothesis in canonical_hypotheses
    )
    hypothesis_index = {
        hypothesis_id: index
        for index, hypothesis_id in enumerate(hypothesis_ids)
    }

    exposure: list[tuple[int, ...]] = []
    outcome_masks: list[tuple[tuple[str, int], ...]] = []
    full_belief = (1 << len(canonical_hypotheses)) - 1
    for candidate in canonical_candidates:
        exposure_by_id = dict(
            scores[candidate.candidate_id].exposure_by_hypothesis
        )
        exposure.append(
            tuple(
                exposure_by_id[hypothesis_id]
                for hypothesis_id in hypothesis_ids
            )
        )

        if not candidate.passive_outcomes:
            outcome_masks.append(
                ((NO_PASSIVE_OBSERVATION, full_belief),)
            )
            continue

        masks: dict[str, int] = {}
        for support in candidate.passive_outcomes:
            hypothesis_bit = 1 << hypothesis_index[support.hypothesis_id]
            for outcome in support.outcomes:
                masks[outcome] = masks.get(outcome, 0) | hypothesis_bit
        outcome_masks.append(tuple(sorted(masks.items())))

    return _PreparedGame(
        hypotheses=canonical_hypotheses,
        candidates=canonical_candidates,
        exposure=tuple(exposure),
        outcome_masks=tuple(outcome_masks),
        full_belief=full_belief,
    )


def _hypothesis_indices(belief: int) -> tuple[int, ...]:
    return tuple(
        index
        for index in range(belief.bit_length())
        if belief & (1 << index)
    )


def _primary_key(
    game: _PreparedGame,
    belief: int,
    candidate_index: int,
) -> tuple[int, int]:
    return (
        max(
            game.exposure[candidate_index][hypothesis_index]
            for hypothesis_index in _hypothesis_indices(belief)
        ),
        game.candidates[candidate_index].predicted_latency_us,
    )


def _primary_candidates(
    game: _PreparedGame,
    belief: int,
) -> tuple[int, ...]:
    keys = tuple(
        _primary_key(game, belief, candidate_index)
        for candidate_index in range(len(game.candidates))
    )
    best = min(keys)
    return tuple(
        candidate_index
        for candidate_index, key in enumerate(keys)
        if key == best
    )


def _branches(
    game: _PreparedGame,
    belief: int,
    candidate_index: int,
) -> tuple[tuple[str, int], ...]:
    return tuple(
        (outcome, belief & support_mask)
        for outcome, support_mask in game.outcome_masks[candidate_index]
        if belief & support_mask
    )


def _immediate_worst_survivors(
    game: _PreparedGame,
    belief: int,
    candidate_index: int,
) -> int:
    return max(
        branch.bit_count()
        for _, branch in _branches(game, belief, candidate_index)
    )


def select_lookahead_topology(
    hypotheses: Iterable[JointFaultHypothesis],
    candidates: Iterable[KauriTreeCandidate],
    *,
    horizon: int,
) -> LookaheadTopologySelection:
    """Choose the exact finite-horizon policy's first safe tree.

    "Safe" means lexicographically minimum current worst-case structural
    exposure and predicted latency.  Information gain is optimized only
    inside that primary-optimal set.
    """

    canonical_horizon = _validate_horizon(horizon)
    game = _prepare_game(hypotheses, candidates)

    @lru_cache(maxsize=None)
    def optimal_value(belief: int, remaining: int) -> int:
        if remaining == 0 or belief.bit_count() <= 1:
            return belief.bit_count()
        return min(
            max(
                optimal_value(branch, remaining - 1)
                for _, branch in _branches(
                    game,
                    belief,
                    candidate_index,
                )
            )
            for candidate_index in _primary_candidates(game, belief)
        )

    primary = frozenset(
        _primary_candidates(game, game.full_belief)
    )
    candidate_scores: list[LookaheadTopologyScore] = []
    for candidate_index, candidate in enumerate(game.candidates):
        primary_key = _primary_key(
            game,
            game.full_belief,
            candidate_index,
        )
        branch_values = tuple(
            ReconfigurationOutcomeBranch(
                outcome=outcome,
                surviving_hypothesis_ids=tuple(
                    game.hypotheses[index].hypothesis_id
                    for index in _hypothesis_indices(branch)
                ),
                terminal_hypothesis_bound=optimal_value(
                    branch,
                    canonical_horizon - 1,
                ),
            )
            for outcome, branch in _branches(
                game,
                game.full_belief,
                candidate_index,
            )
        )
        candidate_scores.append(
            LookaheadTopologyScore(
                candidate_id=candidate.candidate_id,
                primary_optimal=candidate_index in primary,
                worst_case_exposure=primary_key[0],
                predicted_latency_us=primary_key[1],
                immediate_worst_surviving_hypotheses=(
                    _immediate_worst_survivors(
                        game,
                        game.full_belief,
                        candidate_index,
                    )
                ),
                worst_case_terminal_hypotheses=max(
                    branch.terminal_hypothesis_bound
                    for branch in branch_values
                ),
                churn_cost=candidate.churn_cost,
                branches=branch_values,
            )
        )

    selected_index = min(
        primary,
        key=lambda candidate_index: (
            candidate_scores[
                candidate_index
            ].worst_case_terminal_hypotheses,
            candidate_scores[
                candidate_index
            ].immediate_worst_surviving_hypotheses,
            game.candidates[candidate_index].churn_cost,
            game.candidates[candidate_index].candidate_id,
        ),
    )
    return LookaheadTopologySelection(
        selected=game.candidates[selected_index],
        score=candidate_scores[selected_index],
        candidate_scores=tuple(candidate_scores),
        horizon=canonical_horizon,
        belief_states_evaluated=optimal_value.cache_info().currsize,
    )


def evaluate_greedy_topology_policy(
    hypotheses: Iterable[JointFaultHypothesis],
    candidates: Iterable[KauriTreeCandidate],
    *,
    horizon: int,
) -> GreedyPolicyEvaluation:
    """Evaluate repeated one-step robust-topology selection."""

    canonical_horizon = _validate_horizon(horizon)
    game = _prepare_game(hypotheses, candidates)

    def greedy_candidate(belief: int) -> int:
        return min(
            range(len(game.candidates)),
            key=lambda candidate_index: (
                *_primary_key(game, belief, candidate_index),
                _immediate_worst_survivors(
                    game,
                    belief,
                    candidate_index,
                ),
                game.candidates[candidate_index].churn_cost,
                game.candidates[candidate_index].candidate_id,
            ),
        )

    @lru_cache(maxsize=None)
    def greedy_value(belief: int, remaining: int) -> int:
        if remaining == 0 or belief.bit_count() <= 1:
            return belief.bit_count()
        candidate_index = greedy_candidate(belief)
        return max(
            greedy_value(branch, remaining - 1)
            for _, branch in _branches(
                game,
                belief,
                candidate_index,
            )
        )

    selected_index = greedy_candidate(game.full_belief)
    return GreedyPolicyEvaluation(
        selected=game.candidates[selected_index],
        immediate_worst_surviving_hypotheses=(
            _immediate_worst_survivors(
                game,
                game.full_belief,
                selected_index,
            )
        ),
        worst_case_terminal_hypotheses=greedy_value(
            game.full_belief,
            canonical_horizon,
        ),
        horizon=canonical_horizon,
        belief_states_evaluated=greedy_value.cache_info().currsize,
    )


def advance_compatible_hypotheses(
    hypotheses: Iterable[JointFaultHypothesis],
    candidate: KauriTreeCandidate,
    outcome: str,
) -> tuple[JointFaultHypothesis, ...]:
    """Apply one passive observation without eliminating its producer."""

    if not isinstance(outcome, str) or not outcome:
        raise RobustTopologyError("passive outcome must be nonempty")
    game = _prepare_game(hypotheses, (candidate,))
    matching = tuple(
        branch
        for branch_outcome, branch in _branches(
            game,
            game.full_belief,
            0,
        )
        if branch_outcome == outcome
    )
    if not matching:
        raise RobustTopologyError(
            "passive outcome is impossible for every compatible hypothesis"
        )
    belief = matching[0]
    return tuple(
        game.hypotheses[index]
        for index in _hypothesis_indices(belief)
    )


def indistinguishable_hypothesis_pairs(
    hypotheses: Iterable[JointFaultHypothesis],
    candidates: Iterable[KauriTreeCandidate],
) -> tuple[tuple[str, str], ...]:
    """Return pairs an adversary can keep ambiguous under every tree.

    For each candidate, the two hypotheses share at least one allowed
    outcome.  Whichever tree an adaptive policy chooses, the adversary can
    therefore emit a common outcome and retain both hypotheses forever.
    """

    game = _prepare_game(hypotheses, candidates)
    pairs: list[tuple[str, str]] = []
    for left, right in combinations(range(len(game.hypotheses)), 2):
        pair_mask = (1 << left) | (1 << right)
        if all(
            any(
                support_mask & pair_mask == pair_mask
                for _, support_mask in candidate_outcomes
            )
            for candidate_outcomes in game.outcome_masks
        ):
            pairs.append(
                (
                    game.hypotheses[left].hypothesis_id,
                    game.hypotheses[right].hypothesis_id,
                )
            )
    return tuple(pairs)


__all__ = (
    "GreedyPolicyEvaluation",
    "LookaheadTopologyScore",
    "LookaheadTopologySelection",
    "MAXIMUM_LOOKAHEAD_CANDIDATES",
    "MAXIMUM_LOOKAHEAD_HORIZON",
    "MAXIMUM_LOOKAHEAD_HYPOTHESES",
    "ReconfigurationOutcomeBranch",
    "advance_compatible_hypotheses",
    "evaluate_greedy_topology_policy",
    "indistinguishable_hypothesis_pairs",
    "select_lookahead_topology",
)
