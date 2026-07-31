"""Deterministic breadth audit for passive two-epoch Kauri planning.

This campaign samples a fixed population of 1,000 small information games.
Every game embeds four binary observation partitions in legal N=31,
fanout-five tree candidates.  The nine possible singleton-fault identities
remain leaves in every candidate, so the compared policies have identical
modeled containment, latency, reconfiguration, and message budgets.

The production greedy and lookahead solvers are checked against independent
bit-mask dynamic programs before a scenario is admitted to the artifact.
The population is synthetic and uniformly samples partition sets; it is not
an estimate of how often a benefit occurs in real deployments.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
import json
import math
import random
import re
from typing import Any

from .passive_reconfiguration_game import (
    evaluate_greedy_topology_policy,
    select_lookahead_topology,
)
from .robust_topology import (
    JointFaultHypothesis,
    KauriTreeCandidate,
    PassiveOutcomeSupport,
    score_topology,
)


PLANNING_BREADTH_SEED = 41_719
PLANNING_BREADTH_SCENARIOS = 1_000

_SUSPECT_COUNT = 9
_FULL_BELIEF = (1 << _SUSPECT_COUNT) - 1
_CANDIDATES_PER_SCENARIO = 4
_HORIZON = 2
_FANOUT = 5
_PREDICTED_LATENCY_US = 10
_SUSPECTS = tuple(range(_SUSPECT_COUNT))
_SAFE_INTERNALS = tuple(range(9, 15))
_SAFE_LEAF_FILLERS = tuple(range(15, 31))
_CANONICAL_PARTITIONS = tuple(
    mask for mask in range(1, _FULL_BELIEF) if mask & 1
)
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_REVISION_VERIFICATIONS = frozenset(
    {
        "verified_current_clean_head",
        "verified_current_head_dirty_override",
    }
)

_FIXED_CONTEXT = {
    "replica_count": 31,
    "fanout": _FANOUT,
    "tree_levels": 3,
    "singleton_fault_hypotheses": _SUSPECT_COUNT,
    "candidate_topologies_per_scenario": _CANDIDATES_PER_SCENARIO,
    "reconfiguration_budget_epochs": _HORIZON,
    "modeled_worst_case_exposure": 1,
    "modeled_latency_us": _PREDICTED_LATENCY_US,
    "extra_reconfigurations_vs_greedy": 0,
    "additional_diagnostic_messages": 0,
}

_SUPPORTED_CLAIM = (
    "under the frozen synthetic N=31 partition-set audit, exact two-epoch "
    "planning never leaves more terminal singleton-fault hypotheses than "
    "greedy placement at the same modeled cost, and is strictly better in "
    "a reproducible subset"
)

_CLAIMS_NOT_MADE = (
    "the synthetic partition-set population is not a deployment distribution",
    "no live planner activation",
    "no throughput or latency measurement",
    "no consensus-safety or liveness proof",
    "no arbitrary Byzantine identification",
    "only singleton hypotheses and coarsened binary outcomes are modeled",
)


class PlanningBreadthError(ValueError):
    """The breadth artifact cannot support its bounded claim."""


def _hypotheses() -> tuple[JointFaultHypothesis, ...]:
    return tuple(
        JointFaultHypothesis(
            hypothesis_id=f"fault-{replica_id}",
            faulty_replicas=frozenset({replica_id}),
        )
        for replica_id in _SUSPECTS
    )


def _members_for_partition(partition: int) -> tuple[int, ...]:
    left_suspects = tuple(
        replica_id
        for replica_id in _SUSPECTS
        if partition & (1 << replica_id)
    )
    right_suspects = tuple(
        replica_id
        for replica_id in _SUSPECTS
        if not partition & (1 << replica_id)
    )
    left_filler_count = 10 - len(left_suspects)
    return (
        _SAFE_INTERNALS
        + left_suspects
        + _SAFE_LEAF_FILLERS[:left_filler_count]
        + right_suspects
        + _SAFE_LEAF_FILLERS[left_filler_count:]
    )


def _candidate(candidate_index: int, partition: int) -> KauriTreeCandidate:
    return KauriTreeCandidate(
        candidate_id=f"tree-{candidate_index}",
        members_breadth_first=_members_for_partition(partition),
        fanout=_FANOUT,
        predicted_latency_us=_PREDICTED_LATENCY_US,
        churn_cost=0,
        passive_outcomes=tuple(
            PassiveOutcomeSupport(
                hypothesis_id=f"fault-{replica_id}",
                outcomes=frozenset(
                    {
                        "left-parent-group:timeout"
                        if partition & (1 << replica_id)
                        else "right-parent-group:timeout"
                    }
                ),
            )
            for replica_id in _SUSPECTS
        ),
    )


def _partition_scenarios() -> tuple[tuple[int, ...], ...]:
    generator = random.Random(PLANNING_BREADTH_SEED)
    seen: set[tuple[int, ...]] = set()
    scenarios: list[tuple[int, ...]] = []
    while len(scenarios) < PLANNING_BREADTH_SCENARIOS:
        partitions = tuple(
            sorted(
                generator.sample(
                    _CANONICAL_PARTITIONS,
                    _CANDIDATES_PER_SCENARIO,
                )
            )
        )
        if partitions in seen:
            continue
        seen.add(partitions)
        scenarios.append(partitions)
    return tuple(scenarios)


def _branches(belief: int, partition: int) -> tuple[int, ...]:
    return tuple(
        branch
        for branch in (
            belief & partition,
            belief & (~partition) & _FULL_BELIEF,
        )
        if branch
    )


def _immediate_worst_survivors(belief: int, partition: int) -> int:
    return max(branch.bit_count() for branch in _branches(belief, partition))


def _reference_values(
    partitions: tuple[int, ...],
) -> tuple[int, int, int]:
    """Return canonical greedy, best-tied greedy, and exact lookahead values."""

    @lru_cache(maxsize=None)
    def canonical_greedy(belief: int, remaining: int) -> int:
        if remaining == 0 or belief.bit_count() <= 1:
            return belief.bit_count()
        selected = min(
            partitions,
            key=lambda partition: (
                _immediate_worst_survivors(belief, partition),
                partition,
            ),
        )
        return max(
            canonical_greedy(branch, remaining - 1)
            for branch in _branches(belief, selected)
        )

    @lru_cache(maxsize=None)
    def best_tied_greedy(belief: int, remaining: int) -> int:
        if remaining == 0 or belief.bit_count() <= 1:
            return belief.bit_count()
        immediate = tuple(
            _immediate_worst_survivors(belief, partition)
            for partition in partitions
        )
        best_immediate = min(immediate)
        return min(
            max(
                best_tied_greedy(branch, remaining - 1)
                for branch in _branches(belief, partition)
            )
            for partition, value in zip(partitions, immediate, strict=True)
            if value == best_immediate
        )

    @lru_cache(maxsize=None)
    def lookahead(belief: int, remaining: int) -> int:
        if remaining == 0 or belief.bit_count() <= 1:
            return belief.bit_count()
        return min(
            max(
                lookahead(branch, remaining - 1)
                for branch in _branches(belief, partition)
            )
            for partition in partitions
        )

    return (
        canonical_greedy(_FULL_BELIEF, _HORIZON),
        best_tied_greedy(_FULL_BELIEF, _HORIZON),
        lookahead(_FULL_BELIEF, _HORIZON),
    )


def _relation(lookahead: int, baseline: int) -> str:
    if lookahead < baseline:
        return "win"
    if lookahead == baseline:
        return "tie"
    return "loss"


def _increment(counts: dict[str, int], key: int) -> None:
    text = str(key)
    counts[text] = counts.get(text, 0) + 1


def _scenario(
    index: int,
    partitions: tuple[int, ...],
    hypotheses: tuple[JointFaultHypothesis, ...],
) -> dict[str, Any]:
    candidates = tuple(
        _candidate(candidate_index, partition)
        for candidate_index, partition in enumerate(partitions)
    )
    greedy = evaluate_greedy_topology_policy(
        hypotheses,
        candidates,
        horizon=_HORIZON,
    )
    lookahead = select_lookahead_topology(
        hypotheses,
        candidates,
        horizon=_HORIZON,
    )
    canonical_reference, best_tied, lookahead_reference = _reference_values(
        partitions
    )
    if (
        greedy.worst_case_terminal_hypotheses != canonical_reference
        or lookahead.score.worst_case_terminal_hypotheses
        != lookahead_reference
    ):
        raise PlanningBreadthError(
            f"scenario-{index:04d} production/reference solver mismatch"
        )

    immediate = tuple(
        _immediate_worst_survivors(_FULL_BELIEF, partition)
        for partition in partitions
    )
    minimum_immediate = min(immediate)
    tie_count = immediate.count(minimum_immediate)
    scores = {
        score.candidate_id: score
        for score in lookahead.candidate_scores
    }
    candidate_records: list[dict[str, Any]] = []
    cost_regression = False
    for candidate_index, (candidate, partition) in enumerate(
        zip(candidates, partitions, strict=True)
    ):
        topology_score = score_topology(hypotheses, candidate)
        solver_score = scores[candidate.candidate_id]
        candidate_cost_regression = (
            topology_score.worst_case_exposure != 1
            or solver_score.predicted_latency_us != _PREDICTED_LATENCY_US
        )
        cost_regression = cost_regression or candidate_cost_regression
        candidate_records.append(
            {
                "candidate_id": f"tree-{candidate_index}",
                "partition_mask": partition,
                "members_breadth_first": list(
                    candidate.members_breadth_first
                ),
                "fanout": candidate.fanout,
                "worst_case_exposure": topology_score.worst_case_exposure,
                "predicted_latency_us": candidate.predicted_latency_us,
            }
        )

    canonical = greedy.worst_case_terminal_hypotheses
    planned = lookahead.score.worst_case_terminal_hypotheses
    return {
        "scenario_id": f"scenario-{index:04d}",
        "partition_masks": list(partitions),
        "candidates": candidate_records,
        "one_step_worst_survivors": list(immediate),
        "greedy_first_action_tie_count": tie_count,
        "unique_greedy_first_action": tie_count == 1,
        "canonical_greedy_terminal_ambiguity": canonical,
        "best_tie_resolved_greedy_terminal_ambiguity": best_tied,
        "lookahead_terminal_ambiguity": planned,
        "canonical_terminal_ambiguity_reduction": canonical - planned,
        "best_tied_terminal_ambiguity_reduction": best_tied - planned,
        "canonical_greedy_comparison": _relation(planned, canonical),
        "best_tied_greedy_comparison": _relation(planned, best_tied),
        "cost_regression": cost_regression,
    }


def build_planning_breadth_campaign(
    *,
    kauri_revision: str | None = None,
    revision_verification: str | None = None,
) -> dict[str, Any]:
    """Build the frozen campaign after checking every solver result."""

    if (kauri_revision is None) != (revision_verification is None):
        raise PlanningBreadthError(
            "revision and revision verification must be supplied together"
        )
    if kauri_revision is not None:
        if (
            not isinstance(kauri_revision, str)
            or _REVISION.fullmatch(kauri_revision) is None
        ):
            raise PlanningBreadthError(
                "Kauri revision must be 40 lowercase hexadecimal characters"
            )
        if revision_verification not in _REVISION_VERIFICATIONS:
            raise PlanningBreadthError("revision verification is unsupported")

    hypotheses = _hypotheses()
    scenarios = [
        _scenario(index, partitions, hypotheses)
        for index, partitions in enumerate(_partition_scenarios())
    ]

    canonical = {"wins": 0, "ties": 0, "losses": 0}
    best_tied = {"wins": 0, "ties": 0, "losses": 0}
    unique = {"wins": 0, "ties": 0, "losses": 0}
    canonical_reductions: dict[str, int] = {}
    best_tied_reductions: dict[str, int] = {}
    first_action_ties: dict[str, int] = {}
    unique_scenarios = 0
    cost_regressions = 0

    for scenario in scenarios:
        canonical_relation = scenario["canonical_greedy_comparison"] + "s"
        best_tied_relation = scenario["best_tied_greedy_comparison"] + "s"
        canonical[canonical_relation] += 1
        best_tied[best_tied_relation] += 1
        if scenario["unique_greedy_first_action"]:
            unique_scenarios += 1
            unique[canonical_relation] += 1
        _increment(
            canonical_reductions,
            scenario["canonical_terminal_ambiguity_reduction"],
        )
        _increment(
            best_tied_reductions,
            scenario["best_tied_terminal_ambiguity_reduction"],
        )
        _increment(
            first_action_ties,
            scenario["greedy_first_action_tie_count"],
        )
        cost_regressions += int(scenario["cost_regression"])

    summary = {
        "canonical_greedy": canonical,
        "best_tie_resolved_greedy": best_tied,
        "unique_greedy_first_action": {
            "scenarios": unique_scenarios,
            **unique,
        },
        "canonical_terminal_ambiguity_reduction_distribution": (
            canonical_reductions
        ),
        "best_tied_terminal_ambiguity_reduction_distribution": (
            best_tied_reductions
        ),
        "greedy_first_action_tie_count_distribution": first_action_ties,
        "canonical_only_tie_sensitive_wins": (
            canonical["wins"] - best_tied["wins"]
        ),
        "tie_robust_lookahead_wins": best_tied["wins"],
        "lookahead_losses": canonical["losses"],
        "cost_regressions": cost_regressions,
    }
    if summary["lookahead_losses"] or summary["cost_regressions"]:
        raise PlanningBreadthError(
            "frozen campaign contains a lookahead loss or cost regression"
        )

    campaign = {
        "schema_version": 1,
        "scenario": "n31-two-epoch-planning-breadth",
        "verdict": "PASS",
        "seed": PLANNING_BREADTH_SEED,
        "scenario_count": PLANNING_BREADTH_SCENARIOS,
        "sampling_frame": {
            "canonical_partition_classes": len(_CANONICAL_PARTITIONS),
            "unordered_partition_sets": math.comb(
                len(_CANONICAL_PARTITIONS),
                _CANDIDATES_PER_SCENARIO,
            ),
            "sampling_method": (
                "random.Random(seed).sample(partitions, 4), then sort"
            ),
            "duplicate_policy": (
                "reject duplicate sorted tuples until scenario_count is met"
            ),
        },
        "fixed_context": dict(_FIXED_CONTEXT),
        "supported_claim": _SUPPORTED_CLAIM,
        "claims_not_made": list(_CLAIMS_NOT_MADE),
        "validation": {
            "solver_reference_agreements": len(scenarios),
            "legal_candidate_trees": sum(
                len(scenario["candidates"]) for scenario in scenarios
            ),
            "cost_regressions": summary["cost_regressions"],
            "lookahead_losses": summary["lookahead_losses"],
        },
        "summary": summary,
        "scenarios": scenarios,
    }
    if kauri_revision is not None:
        campaign["kauri_revision"] = kauri_revision
        campaign["revision_verification"] = revision_verification
    return campaign


def validate_planning_breadth_campaign(
    campaign: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed unless *campaign* is the exact frozen canonical result."""

    if not isinstance(campaign, Mapping):
        raise PlanningBreadthError("planning breadth campaign must be an object")
    try:
        supplied = json.dumps(
            dict(campaign),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise PlanningBreadthError(
            "planning breadth campaign is not canonical JSON"
        ) from error
    revision = campaign.get("kauri_revision")
    revision_verification = campaign.get("revision_verification")
    expected_campaign = build_planning_breadth_campaign(
        kauri_revision=revision,
        revision_verification=revision_verification,
    )
    expected = json.dumps(
        expected_campaign,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if supplied != expected:
        raise PlanningBreadthError(
            "planning breadth campaign does not match the frozen audit"
        )
    return expected_campaign


def canonical_planning_breadth_json(campaign: Mapping[str, Any]) -> str:
    """Validate and serialize the exact frozen result without timestamps."""

    validated = validate_planning_breadth_campaign(campaign)
    try:
        return json.dumps(
            validated,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:  # pragma: no cover - validated.
        raise PlanningBreadthError(
            "planning breadth campaign is not canonical JSON"
        ) from error


__all__ = (
    "PLANNING_BREADTH_SCENARIOS",
    "PLANNING_BREADTH_SEED",
    "PlanningBreadthError",
    "build_planning_breadth_campaign",
    "canonical_planning_breadth_json",
    "validate_planning_breadth_campaign",
)
