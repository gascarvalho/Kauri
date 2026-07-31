"""Breadth-campaign contracts for two-epoch passive Kauri planning.

The campaign is deliberately model-only.  It freezes one deterministic
population of legal N=31 tree games and checks the exact minimax values with
an independent bit-mask solver.  No test in this file launches a replica.
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import random
import subprocess
import sys
from typing import Any

import pytest

from experiments.adaptive.kauri_experiment.planning_breadth_campaign import (
    PLANNING_BREADTH_SCENARIOS,
    PLANNING_BREADTH_SEED,
    build_planning_breadth_campaign,
    canonical_planning_breadth_json,
    validate_planning_breadth_campaign,
)


FULL_BELIEF = (1 << 9) - 1
CANONICAL_PARTITIONS = tuple(
    mask
    for mask in range(1, FULL_BELIEF)
    if mask & 1
)


def _branches(belief: int, partition: int) -> tuple[int, ...]:
    return tuple(
        branch
        for branch in (
            belief & partition,
            belief & (~partition) & FULL_BELIEF,
        )
        if branch
    )


def _immediate_worst_survivors(
    belief: int,
    partition: int,
) -> int:
    return max(
        branch.bit_count()
        for branch in _branches(belief, partition)
    )


def _canonical_greedy_terminal(
    partitions: tuple[int, ...],
) -> int:
    """Evaluate the frozen lowest-mask tie break independently."""

    @lru_cache(maxsize=None)
    def value(belief: int, remaining: int) -> int:
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
            value(branch, remaining - 1)
            for branch in _branches(belief, selected)
        )

    return value(FULL_BELIEF, 2)


def _best_tie_resolved_greedy_terminal(
    partitions: tuple[int, ...],
) -> int:
    """Give greedy the best adaptive tie break at every belief."""

    @lru_cache(maxsize=None)
    def value(belief: int, remaining: int) -> int:
        if remaining == 0 or belief.bit_count() <= 1:
            return belief.bit_count()
        best_immediate = min(
            _immediate_worst_survivors(belief, partition)
            for partition in partitions
        )
        tied = tuple(
            partition
            for partition in partitions
            if _immediate_worst_survivors(belief, partition)
            == best_immediate
        )
        return min(
            max(
                value(branch, remaining - 1)
                for branch in _branches(belief, partition)
            )
            for partition in tied
        )

    return value(FULL_BELIEF, 2)


def _lookahead_terminal(partitions: tuple[int, ...]) -> int:
    @lru_cache(maxsize=None)
    def value(belief: int, remaining: int) -> int:
        if remaining == 0 or belief.bit_count() <= 1:
            return belief.bit_count()
        return min(
            max(
                value(branch, remaining - 1)
                for branch in _branches(belief, partition)
            )
            for partition in partitions
        )

    return value(FULL_BELIEF, 2)


def _expected_partition_scenarios() -> tuple[tuple[int, ...], ...]:
    generator = random.Random(41_719)
    seen: set[tuple[int, ...]] = set()
    scenarios: list[tuple[int, ...]] = []
    while len(scenarios) < 1_000:
        partitions = tuple(
            sorted(generator.sample(CANONICAL_PARTITIONS, 4))
        )
        if partitions in seen:
            continue
        seen.add(partitions)
        scenarios.append(partitions)
    return tuple(scenarios)


def _members_for_partition(partition: int) -> tuple[int, ...]:
    """Independent canonical N=31, fanout-five tree construction."""

    left_suspects = tuple(
        replica_id
        for replica_id in range(9)
        if partition & (1 << replica_id)
    )
    right_suspects = tuple(
        replica_id
        for replica_id in range(9)
        if not partition & (1 << replica_id)
    )
    safe_fillers = tuple(range(15, 31))
    left_filler_count = 10 - len(left_suspects)
    return (
        tuple(range(9, 15))
        + left_suspects
        + safe_fillers[:left_filler_count]
        + right_suspects
        + safe_fillers[left_filler_count:]
    )


@pytest.fixture(scope="module")
def campaign() -> dict[str, Any]:
    return build_planning_breadth_campaign()


def test_campaign_freezes_exactly_1000_unique_canonical_n31_games(
    campaign: dict[str, Any],
) -> None:
    assert PLANNING_BREADTH_SEED == 41_719
    assert PLANNING_BREADTH_SCENARIOS == 1_000
    assert campaign["schema_version"] == 1
    assert campaign["scenario"] == "n31-two-epoch-planning-breadth"
    assert campaign["seed"] == PLANNING_BREADTH_SEED
    assert campaign["scenario_count"] == PLANNING_BREADTH_SCENARIOS
    assert campaign["fixed_context"] == {
        "replica_count": 31,
        "fanout": 5,
        "tree_levels": 3,
        "singleton_fault_hypotheses": 9,
        "candidate_topologies_per_scenario": 4,
        "reconfiguration_budget_epochs": 2,
        "modeled_worst_case_exposure": 1,
        "modeled_latency_us": 10,
        "extra_reconfigurations_vs_greedy": 0,
        "additional_diagnostic_messages": 0,
    }

    scenarios = campaign["scenarios"]
    assert len(scenarios) == 1_000
    observed_partitions = tuple(
        tuple(item["partition_masks"]) for item in scenarios
    )
    assert observed_partitions == _expected_partition_scenarios()
    assert len(set(observed_partitions)) == 1_000

    for index, (item, partitions) in enumerate(
        zip(scenarios, observed_partitions, strict=True)
    ):
        assert item["scenario_id"] == f"scenario-{index:04d}"
        assert len(partitions) == len(set(partitions)) == 4
        assert tuple(sorted(partitions)) == partitions
        assert all(
            type(mask) is int
            and mask in CANONICAL_PARTITIONS
            and mask & 1
            and mask != FULL_BELIEF
            for mask in partitions
        )

        candidates = item["candidates"]
        assert len(candidates) == 4
        for candidate_index, (candidate, partition) in enumerate(
            zip(candidates, partitions, strict=True)
        ):
            assert candidate == {
                "candidate_id": f"tree-{candidate_index}",
                "partition_mask": partition,
                "members_breadth_first": list(
                    _members_for_partition(partition)
                ),
                "fanout": 5,
                "worst_case_exposure": 1,
                "predicted_latency_us": 10,
            }
            members = tuple(candidate["members_breadth_first"])
            assert len(members) == len(set(members)) == 31
            assert set(members) == set(range(31))
            assert all(members.index(suspect) >= 6 for suspect in range(9))


def test_campaign_matches_independent_greedy_tie_and_lookahead_solvers(
    campaign: dict[str, Any],
) -> None:
    canonical_results = {"wins": 0, "ties": 0, "losses": 0}
    best_tied_results = {"wins": 0, "ties": 0, "losses": 0}
    unique_results = {"wins": 0, "ties": 0, "losses": 0}
    canonical_reductions: dict[str, int] = {}
    best_tied_reductions: dict[str, int] = {}
    first_action_ties: dict[str, int] = {}

    for item in campaign["scenarios"]:
        partitions = tuple(item["partition_masks"])
        immediate = tuple(
            _immediate_worst_survivors(FULL_BELIEF, partition)
            for partition in partitions
        )
        minimum_immediate = min(immediate)
        tie_count = immediate.count(minimum_immediate)
        canonical = _canonical_greedy_terminal(partitions)
        best_tied = _best_tie_resolved_greedy_terminal(partitions)
        lookahead = _lookahead_terminal(partitions)

        assert item["one_step_worst_survivors"] == list(immediate)
        assert item["greedy_first_action_tie_count"] == tie_count
        assert item["unique_greedy_first_action"] is (tie_count == 1)
        assert item["canonical_greedy_terminal_ambiguity"] == canonical
        assert item["best_tie_resolved_greedy_terminal_ambiguity"] == best_tied
        assert item["lookahead_terminal_ambiguity"] == lookahead
        assert item["canonical_terminal_ambiguity_reduction"] == (
            canonical - lookahead
        )
        assert item["best_tied_terminal_ambiguity_reduction"] == (
            best_tied - lookahead
        )
        assert item["cost_regression"] is False
        assert lookahead <= best_tied <= canonical

        canonical_relation = (
            "wins" if lookahead < canonical else "ties"
            if lookahead == canonical else "losses"
        )
        best_tied_relation = (
            "wins" if lookahead < best_tied else "ties"
            if lookahead == best_tied else "losses"
        )
        assert item["canonical_greedy_comparison"] == canonical_relation[:-1]
        assert item["best_tied_greedy_comparison"] == best_tied_relation[:-1]
        canonical_results[canonical_relation] += 1
        best_tied_results[best_tied_relation] += 1
        if tie_count == 1:
            unique_results[canonical_relation] += 1
        canonical_key = str(canonical - lookahead)
        best_tied_key = str(best_tied - lookahead)
        tie_key = str(tie_count)
        canonical_reductions[canonical_key] = (
            canonical_reductions.get(canonical_key, 0) + 1
        )
        best_tied_reductions[best_tied_key] = (
            best_tied_reductions.get(best_tied_key, 0) + 1
        )
        first_action_ties[tie_key] = first_action_ties.get(tie_key, 0) + 1

    assert canonical_results == {"wins": 23, "ties": 977, "losses": 0}
    assert best_tied_results == {"wins": 4, "ties": 996, "losses": 0}
    assert unique_results == {"wins": 4, "ties": 288, "losses": 0}
    assert canonical_reductions == {"0": 977, "1": 23}
    assert best_tied_reductions == {"0": 996, "1": 4}
    assert first_action_ties == {"1": 292, "2": 388, "3": 247, "4": 73}

    assert campaign["summary"] == {
        "canonical_greedy": canonical_results,
        "best_tie_resolved_greedy": best_tied_results,
        "unique_greedy_first_action": {
            "scenarios": 292,
            **unique_results,
        },
        "canonical_terminal_ambiguity_reduction_distribution": (
            canonical_reductions
        ),
        "best_tied_terminal_ambiguity_reduction_distribution": (
            best_tied_reductions
        ),
        "greedy_first_action_tie_count_distribution": first_action_ties,
        "canonical_only_tie_sensitive_wins": 19,
        "tie_robust_lookahead_wins": 4,
        "lookahead_losses": 0,
        "cost_regressions": 0,
    }


def test_campaign_is_canonical_deterministic_and_type_strict(
    campaign: dict[str, Any],
) -> None:
    rebuilt = build_planning_breadth_campaign()
    assert rebuilt == campaign
    assert validate_planning_breadth_campaign(campaign) == campaign

    encoded = canonical_planning_breadth_json(campaign)
    assert encoded == json.dumps(
        campaign,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert json.loads(encoded) == campaign
    assert encoded == canonical_planning_breadth_json(rebuilt)


@pytest.mark.parametrize(
    "case",
    (
        "numeric_type",
        "duplicate_scenario",
        "noncanonical_partition",
        "solver_result",
        "cost_regression",
        "summary",
    ),
)
def test_campaign_validation_fails_closed_on_tampering(
    campaign: dict[str, Any],
    case: str,
) -> None:
    tampered = deepcopy(campaign)
    if case == "numeric_type":
        tampered["scenario_count"] = 1_000.0
    elif case == "duplicate_scenario":
        tampered["scenarios"][1]["scenario_id"] = tampered["scenarios"][0][
            "scenario_id"
        ]
    elif case == "noncanonical_partition":
        tampered["scenarios"][0]["partition_masks"][0] = 2
    elif case == "solver_result":
        tampered["scenarios"][0]["lookahead_terminal_ambiguity"] -= 1
    elif case == "cost_regression":
        tampered["scenarios"][0]["candidates"][0][
            "predicted_latency_us"
        ] = 11
        tampered["scenarios"][0]["cost_regression"] = False
    elif case == "summary":
        tampered["summary"]["tie_robust_lookahead_wins"] = 5
    else:  # pragma: no cover - parametrization exhausts this branch.
        raise AssertionError(case)

    with pytest.raises(ValueError):
        validate_planning_breadth_campaign(tampered)
    with pytest.raises(ValueError):
        canonical_planning_breadth_json(tampered)


def test_canonical_json_rejects_non_finite_values(
    campaign: dict[str, Any],
) -> None:
    tampered = deepcopy(campaign)
    tampered["summary"]["lookahead_losses"] = float("nan")

    with pytest.raises(ValueError):
        canonical_planning_breadth_json(tampered)


def test_plotter_refuses_tampered_campaign_before_creating_output(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    campaign = build_planning_breadth_campaign(
        kauri_revision="a" * 40,
        revision_verification="verified_current_clean_head",
    )
    campaign["summary"]["tie_robust_lookahead_wins"] = 5
    source = tmp_path / "tampered-campaign.json"
    source.write_text(json.dumps(campaign), encoding="utf-8")
    output = tmp_path / "figures"

    completed = subprocess.run(
        [
            sys.executable,
            str(
                repository_root
                / "experiments/adaptive/plot_planning_breadth_campaign.py"
            ),
            "--campaign",
            str(source),
            "--output-dir",
            str(output),
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "failed validation" in completed.stderr
    assert not output.exists()
