"""Regression contracts for the signer-aware LIVE22A gate."""

from __future__ import annotations

import ast
from copy import deepcopy
import inspect
import json
from pathlib import Path
import subprocess
import sys

import pytest

from experiments.adaptive.kauri_experiment.live_planner_witness import (
    build_live_planner_witness,
    canonical_live_planner_witness_json,
)
from experiments.adaptive.kauri_experiment.live_planner_witness_oracle import (
    LivePlannerWitnessOracleError,
    parse_and_validate_live_planner_witness,
    validate_live_planner_witness,
)

EXPECTED_HASHES = [
    "10473b5f1a8a366277cc4fb89e8dfe1d0da592bd11edc802bec62982b204b307",
    "d41c5b8d925cfc6234d1bfdee063abee06cfc5182664814ef7e4f0f4ae21c8b2",
    "86a4bfb806f0e03ebfe4ad1f6a46f4780c99ec4fdffdb43d0166a400ff118e55",
    "a641cbbf16e62e744c652bd1cfdb1371d786cd3929ceb4793d6219748d944a2e",
    "1e13793e58b1021e0972e39703b0ad1d486237631e34323771f0f5c4ce0c2ddc",
]


@pytest.fixture(scope="module")
def witness() -> dict[str, object]:
    return build_live_planner_witness()


def _swap_candidate_epochs_and_hashes(value: dict[str, object]) -> None:
    candidates = value["candidates"]
    candidates[0]["epoch"], candidates[1]["epoch"] = (
        candidates[1]["epoch"],
        candidates[0]["epoch"],
    )
    candidates[0]["canonical_sha256"], candidates[1]["canonical_sha256"] = (
        candidates[1]["canonical_sha256"],
        candidates[0]["canonical_sha256"],
    )


def test_rejects_the_response_only_witness_with_exact_signer_evidence(
    witness: dict[str, object],
) -> None:
    assert witness["schema"] == "live22a-signer-aware-gate-v1"
    assert witness["verdict"] == "REJECTED"
    assert witness["precursor"]["response_only_belief_count"] == 68
    assert witness["precursor"]["signer_aware_belief_count"] == 33
    assert witness["gate"] == {
        "live_planner_wiring_authorized": False,
        "matched_greedy_joint_campaign_authorized": False,
        "claim_state": "rejected",
        "preserved_claims": ["C-019", "C-020", "D-017"],
        "claim_exclusions": [
            "live planner advantage",
            "faster recovery from joint planning",
            "fewer plausible attackers",
        ],
    }


def test_preserves_five_legal_complete_epoch_lifts(
    witness: dict[str, object],
) -> None:
    candidates = witness["candidates"]
    assert [candidate["canonical_sha256"] for candidate in candidates] == (
        EXPECTED_HASHES
    )
    for candidate in candidates:
        trees = candidate["epoch"]["trees"]
        assert len(trees) == 21
        assert len({tree["members_breadth_first"][0] for tree in trees}) == 21
        assert all(len(set(tree["members_breadth_first"])) == 31 for tree in trees)
        assert all(
            {4, 5, 6, 8, 10, 12, 14, 16, 21, 22}
            <= set(tree["members_breadth_first"][6:])
            for tree in trees
        )


def test_exact_policy_replay_reaches_the_floor_with_greedy(
    witness: dict[str, object],
) -> None:
    policy = witness["policy_audit"]
    assert policy == {
        "reachable_branch_support_counts": [8, 8, 9, 9, 9],
        "immediate_worst_survivors": [32, 32, 31, 31, 31],
        "horizon_two_worst_survivors": [31, 31, 31, 31, 31],
        "true_c16_survivors_after_first": [32, 32, 31, 31, 31],
        "greedy_first_is_unique": False,
        "best_tied_greedy_terminal": 31,
        "joint_two_epoch_terminal": 31,
        "strict_joint_advantage": False,
    }
    core = witness["irreducible_core"]
    assert core["count"] == 31
    assert len(core["hypothesis_ids"]) == 31
    assert core["c004_true_branch_equals_core"] is True


def test_false_timeout_and_real_omission_have_distinct_ancestor_signers(
    witness: dict[str, object],
) -> None:
    assert witness["signer_crosscheck"] == {
        "candidate_id": "live22a-n31-c004",
        "false_timeout_edge": [24, 16],
        "ancestor_aggregate_edge": [30, 24],
        "healthy_false_timeout_aggregate_contains_signer_16": True,
        "true_c16_aggregate_contains_signer_16": False,
    }


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update(verdict="PASS"),
        lambda value: value["precursor"]["records"][3].update(signer_set=[]),
        lambda value: value["policy_audit"].update(strict_joint_advantage=True),
        lambda value: value["candidates"][4].update(canonical_sha256="0" * 64),
        _swap_candidate_epochs_and_hashes,
        lambda value: value["candidates"][4].update(immediate_worst_survivors=30),
        lambda value: value["irreducible_core"].update(count=30),
        lambda value: value["irreducible_core"].update(lower_bound_reason="guess"),
        lambda value: value["signer_semantics"].update(
            wait_exempt_signer_absence_is_non_identifying=False
        ),
    ),
)
def test_independent_oracle_fails_closed_on_material_tampering(
    witness: dict[str, object], mutate
) -> None:
    tampered = deepcopy(witness)
    mutate(tampered)
    with pytest.raises(LivePlannerWitnessOracleError):
        validate_live_planner_witness(tampered)


def test_independent_oracle_recomputes_without_importing_builder(
    witness: dict[str, object],
) -> None:
    report = parse_and_validate_live_planner_witness(
        canonical_live_planner_witness_json(witness)
    )
    assert report == {
        "status": "PASS",
        "verdict": "REJECTED",
        "initial_belief_count": 33,
        "irreducible_core_count": 31,
        "immediate_worst_survivors": [32, 32, 31, 31, 31],
        "horizon_two_worst_survivors": [31, 31, 31, 31, 31],
        "candidate_hashes": EXPECTED_HASHES,
    }
    oracle_path = Path(inspect.getsourcefile(validate_live_planner_witness))
    syntax = ast.parse(oracle_path.read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(syntax) if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        module and module.endswith("live_planner_witness") for module in imported
    )


def test_builder_and_cli_are_deterministic(
    witness: dict[str, object], repository_root: Path
) -> None:
    payload = canonical_live_planner_witness_json(witness)
    assert payload == canonical_live_planner_witness_json(build_live_planner_witness())
    script = repository_root / "experiments/adaptive/run_live_planner_witness.py"
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    assert lines[-1] == "oracle PASS: verdict REJECTED"
    assert json.loads(lines[0]) == witness


def test_canonical_json_rejects_non_finite_numbers(
    witness: dict[str, object],
) -> None:
    tampered = deepcopy(witness)
    tampered["parameters"]["quorum"] = float("nan")
    with pytest.raises(ValueError):
        canonical_live_planner_witness_json(tampered)
