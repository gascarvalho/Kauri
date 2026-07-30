"""Tests for the thesis-minimum synthetic diagnosis comparison."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from experiments.adaptive.kauri_experiment.diagnosis_evaluation import (
    SCENARIO_NAMES,
    canonical_evaluation_json,
    evaluate_frozen_scenarios,
)


def _scenarios_by_name(evaluation: dict[str, object]) -> dict[str, dict]:
    return {
        scenario["name"]: scenario
        for scenario in evaluation["scenarios"]
    }


def test_evaluation_is_frozen_deterministic_and_claim_limited() -> None:
    first = evaluate_frozen_scenarios()
    second = evaluate_frozen_scenarios()

    assert first == second
    assert canonical_evaluation_json(first) == canonical_evaluation_json(
        second
    )
    assert tuple(
        scenario["name"] for scenario in first["scenarios"]
    ) == SCENARIO_NAMES
    assert first["fixed_context"] == {
        "replica_ids": list(range(7)),
        "diagnostic_fault_bound": 1,
        "observations_per_scenario": 3,
    }
    assert (
        first["scalar_baseline"]["kind"]
        == "target_only_scalar_reputation_proxy"
    )
    assert "proxy" in first["scalar_baseline"]["claim_boundary"]
    assert first["claims_not_made"]


def test_each_step_contains_exact_hypothesis_mode_and_proxy_metrics() -> None:
    evaluation = evaluate_frozen_scenarios()

    for scenario in evaluation["scenarios"]:
        assert len(scenario["steps"]) == 3
        for ordinal, step in enumerate(scenario["steps"], start=1):
            assert step["ordinal"] == ordinal
            assert set(step["observation"]) == {
                "attempt_id",
                "reporter_id",
                "target_id",
                "outcome",
            }
            assert not any(
                "truth" in key or "fault" in key
                for key in step["observation"]
            )
            assert step["compatible_hypothesis_count"] == len(
                step["compatible_hypotheses"]
            )
            assert len(step["replica_mode_counts"]) == 7
            for counts in step["replica_mode_counts"]:
                assert counts["compatible_hypotheses"] == (
                    step["compatible_hypothesis_count"]
                )
                assert counts["false_reporter_mass"]["denominator"] > 0
                assert counts["persistent_omitter_mass"]["denominator"] > 0
            assert (
                step["scalar_proxy"]["kind"]
                == "target_only_scalar_reputation_proxy"
            )


def test_false_reporter_diagnosis_succeeds_where_scalar_proxy_does_not() -> (
    None
):
    scenario = _scenarios_by_name(
        evaluate_frozen_scenarios()
    )["static_false_reporter"]

    assert scenario["steps"][0]["compatible_hypothesis_count"] == 2
    assert scenario["steps"][-1]["compatible_hypotheses"] == [
        {
            "false_reporters": [1],
            "persistent_omitters": [],
        }
    ]
    assert scenario["final_scalar_proxy_scores"][1] == {
        "replica_id": 1,
        "score": 0,
    }
    assert scenario["final_scalar_proxy_scores"][4] == {
        "replica_id": 4,
        "score": 1,
    }
    scoring = scenario["post_hoc_ground_truth_scoring"]
    assert scoring["truth_hypothesis_retained"] is True
    assert scoring["diagnosis_exact_identity_match"] is True
    assert scoring["scalar_negative_score_identity_match"] is False


def test_persistent_omitter_is_identified_from_three_distinct_reporters() -> (
    None
):
    scenario = _scenarios_by_name(
        evaluate_frozen_scenarios()
    )["static_persistent_omitter"]

    assert scenario["steps"][0]["compatible_hypothesis_count"] == 2
    assert scenario["steps"][-1]["compatible_hypotheses"] == [
        {
            "false_reporters": [],
            "persistent_omitters": [4],
        }
    ]
    assert scenario["final_scalar_proxy_scores"][4]["score"] == -3
    scoring = scenario["post_hoc_ground_truth_scoring"]
    assert scoring["truth_hypothesis_retained"] is True
    assert scoring["diagnosis_exact_identity_match"] is True
    assert scoring["scalar_negative_score_identity_match"] is True


def test_clean_trace_preserves_uncertainty_without_false_fault_diagnosis() -> (
    None
):
    scenario = _scenarios_by_name(
        evaluate_frozen_scenarios()
    )["clean"]

    assert scenario["steps"][-1]["compatible_hypothesis_count"] == 14
    scoring = scenario["post_hoc_ground_truth_scoring"]
    assert scoring["truth_hypothesis_retained"] is True
    assert scoring["diagnosed_false_reporters"] == []
    assert scoring["diagnosed_persistent_omitters"] == []
    assert scoring["diagnosis_exact_identity_match"] is False
    assert scoring["scalar_negative_score_identity_match"] is True


def test_cli_writes_one_canonical_json_artifact(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "diagnosis-evaluation"
    script = (
        repository_root
        / "experiments"
        / "adaptive"
        / "run_diagnosis_evaluation.py"
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--output-dir",
            str(output_dir),
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    artifact = output_dir / "diagnosis-evaluation.json"
    assert completed.stdout.strip() == str(artifact)
    assert artifact.read_text(encoding="utf-8") == (
        canonical_evaluation_json(evaluate_frozen_scenarios()) + "\n"
    )
    assert json.loads(artifact.read_text(encoding="utf-8")) == (
        evaluate_frozen_scenarios()
    )
