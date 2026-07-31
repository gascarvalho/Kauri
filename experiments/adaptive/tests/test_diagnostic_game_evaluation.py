"""Tests for the reproducible minimax-rematching evidence artifact."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from experiments.adaptive.kauri_experiment import (
    diagnostic_game_evaluation as evaluation_module,
)
from experiments.adaptive.kauri_experiment.diagnostic_game_evaluation import (
    canonical_evaluation_json,
    evaluate_minimax_rematching,
)


REVISION = "a" * 40


def _expected_revision_verification(repository_root: Path) -> str:
    tracked_dirty = any(
        subprocess.run(
            command,
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        ).returncode
        for command in (
            ["git", "diff", "--quiet"],
            ["git", "diff", "--cached", "--quiet"],
        )
    )
    adaptive_status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "experiments/adaptive",
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return (
        "verified_current_head_dirty_override"
        if tracked_dirty or adaptive_status
        else "verified_current_clean_head"
    )


def test_evaluation_records_tight_bound_and_ambiguity_tax() -> None:
    evaluation = evaluate_minimax_rematching(REVISION)

    assert evaluation["kauri_revision"] == REVISION
    assert evaluation["fixed_context"]["diagnostic_fault_bound"] == 1
    assert [
        item["outcome_patterns_checked"]
        for item in evaluation["tight_bound_audit"]
    ] == [4, 16, 64]
    assert all(
        item["unresolved_patterns_after_2d_reports"] == 0
        for item in evaluation["tight_bound_audit"]
    )
    assert all(
        item["two_d_minus_one_witness"]["status"] == "ambiguous"
        for item in evaluation["tight_bound_audit"]
    )

    game = evaluation["canonical_minimax_game"]
    assert game["initial_compatible_hypotheses"] == 2
    assert game["selected_probe"] == {
        "reporter_id": 0,
        "target_id": 1,
    }
    assert game["repeat_probe_score"][
        "guaranteed_hypothesis_elimination"
    ] == 0
    assert game["fresh_probe_score"][
        "guaranteed_hypothesis_elimination"
    ] == 1
    assert game["ambiguity_tax"]["pre_challenge_excluded_endpoints"] == [
        1,
        6,
    ]
    assert game["ambiguity_tax"][
        "worst_case_post_challenge_excluded_endpoints"
    ] == 1
    assert game["ambiguity_tax"][
        "guaranteed_safe_role_candidate_recovery"
    ] == 1
    assert game["symmetric_policy_result"][
        "fresh_round_robin_equals_minimax"
    ] is True
    assert game["exhaustive_abstract_probe_audit"][
        "abstract_directed_pairs_scored"
    ] == 42
    assert len(
        game["exhaustive_abstract_probe_audit"][
            "optimal_equivalence_class"
        ]
    ) == 5
    assert all(
        probe["target_id"] == 1
        for probe in game["exhaustive_abstract_probe_audit"][
            "optimal_equivalence_class"
        ]
    )
    assert game["exhaustive_abstract_probe_audit"][
        "topology_legality_claimed"
    ] is False
    multi_conflict = evaluation["multi_conflict_probe_audit"]
    assert multi_conflict["initial_compatible_hypotheses"] == 3
    assert multi_conflict["lowest_fresh_reporter_score"][
        "worst_case_surviving_hypotheses"
    ] == 3
    assert multi_conflict["minimax_probe_score"][
        "worst_case_surviving_hypotheses"
    ] == 2
    assert multi_conflict["selected_probe"] == {
        "reporter_id": 3,
        "target_id": 2,
    }
    assert multi_conflict["fresh_safe_round_robin_probe"] == {
        "reporter_id": 3,
        "target_id": 2,
    }
    assert multi_conflict[
        "fresh_safe_round_robin_equals_minimax"
    ] is True
    value_aware = evaluation["value_aware_target_audit"]
    assert value_aware["robust_safe_replicas"] == [4, 5, 6]
    assert value_aware["ambiguous_targets"] == [1, 2]
    assert value_aware["uniform_value_minimax_baseline"][
        "probe"
    ] == {
        "reporter_id": 4,
        "target_id": 1,
    }
    assert value_aware["minimax_selection"]["probe"] == {
        "reporter_id": 4,
        "target_id": 2,
    }
    assert value_aware["uniform_value_minimax_baseline"][
        "score_under_frozen_role_values"
    ]["guaranteed_safe_role_value_recovery"] == 1
    assert value_aware["minimax_selection"]["score"][
        "guaranteed_safe_role_value_recovery"
    ] == 2
    assert value_aware["strict_value_advantage"] == {
        "guaranteed_safe_role_value_recovery_delta": 1,
        "worst_case_robust_safe_role_value_delta": 1,
        "worst_case_surviving_hypotheses_delta": 0,
    }


def test_frozen_n7_aggregate_only_schedule_audit_is_tight() -> None:
    audit = evaluation_module.aggregate_only_schedule_audit()

    assert audit["replica_count"] == 7
    assert audit["exposures_per_target"] == [2] * 7
    assert audit["sequential_schedule"] == list(range(7))
    assert audit["sequential_worst_gap"] == 6
    assert audit["stride2_schedule"] == [0, 2, 4, 6, 1, 3, 5]
    assert audit["stride2_worst_gap"] == 4
    assert audit["lower_bound"] == 4
    assert audit["exhaustive_minimum"] == 4
    assert audit["normalized_optimal_schedules"] == [
        [0, 2, 4, 6, 1, 3, 5],
        [0, 5, 3, 1, 6, 4, 2],
    ]


def test_evaluation_is_canonical_and_rejects_invalid_revision() -> None:
    first = evaluate_minimax_rematching(REVISION)
    second = evaluate_minimax_rematching(REVISION)

    assert first == second
    assert canonical_evaluation_json(first) == canonical_evaluation_json(
        second
    )

    for invalid in ("", "A" * 40, "a" * 39, "not-a-revision"):
        try:
            evaluate_minimax_rematching(invalid)
        except ValueError as error:
            assert "revision" in str(error)
        else:
            raise AssertionError("invalid revision was accepted")


def test_cli_writes_one_canonical_evidence_artifact(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "diagnostic-game"
    script = (
        repository_root
        / "experiments"
        / "adaptive"
        / "run_diagnostic_game_evaluation.py"
    )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    expected_revision_verification = _expected_revision_verification(
        repository_root
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--kauri-revision",
            revision,
            "--output-dir",
            str(output_dir),
            "--allow-dirty",
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    artifact = output_dir / "diagnostic-game-evaluation.json"
    assert completed.stdout.strip() == str(artifact)
    observed = json.loads(artifact.read_text(encoding="utf-8"))
    assert observed["kauri_revision_verification"] in {
        "verified_current_clean_head",
        "verified_current_head_dirty_override",
    }
    assert artifact.read_text(encoding="utf-8") == (
        canonical_evaluation_json(
            evaluate_minimax_rematching(
                revision,
                revision_verification=expected_revision_verification,
            )
        )
        + "\n"
    )
    assert observed == (
        evaluate_minimax_rematching(
            revision,
            revision_verification=expected_revision_verification,
        )
    )


def test_cli_rejects_a_revision_other_than_current_head(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    script = (
        repository_root
        / "experiments"
        / "adaptive"
        / "run_diagnostic_game_evaluation.py"
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--kauri-revision",
            REVISION,
            "--output-dir",
            str(tmp_path / "wrong-revision"),
            "--allow-dirty",
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "current HEAD" in completed.stderr
