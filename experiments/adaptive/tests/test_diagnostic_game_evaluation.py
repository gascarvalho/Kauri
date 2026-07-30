"""Tests for the reproducible minimax-rematching evidence artifact."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from experiments.adaptive.kauri_experiment.diagnostic_game_evaluation import (
    canonical_evaluation_json,
    evaluate_minimax_rematching,
)


REVISION = "a" * 40


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
    assert game["exhaustive_legal_probe_audit"][
        "legal_probes_scored"
    ] == 42
    assert len(
        game["exhaustive_legal_probe_audit"][
            "optimal_equivalence_class"
        ]
    ) == 5
    assert all(
        probe["target_id"] == 1
        for probe in game["exhaustive_legal_probe_audit"][
            "optimal_equivalence_class"
        ]
    )
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
    assert artifact.read_text(encoding="utf-8") == (
        canonical_evaluation_json(
            evaluate_minimax_rematching(
                revision,
                revision_verification="verified_current_head_dirty_override",
            )
        )
        + "\n"
    )
    assert json.loads(artifact.read_text(encoding="utf-8")) == (
        evaluate_minimax_rematching(
            revision,
            revision_verification="verified_current_head_dirty_override",
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
