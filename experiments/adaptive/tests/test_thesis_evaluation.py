"""Contract tests for the fail-closed thesis evidence bundle and plots."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from experiments.adaptive.kauri_experiment.comparison import (
    build_n7_comparison,
)
from experiments.adaptive.kauri_experiment.robust_topology_evaluation import (
    evaluate_robust_topology,
)
from experiments.adaptive.kauri_experiment.thesis_evaluation import (
    ARM_NAMES,
    THESIS_COMPARISON_SEED,
    THESIS_DIAGNOSTIC_WINDOW,
    build_thesis_evaluation,
    canonical_thesis_evaluation_json,
    parse_thesis_json_object,
)
from experiments.adaptive.tests.test_fault_comparison import (
    _verdicts as comparison_verdicts,
)


REVISION = "a" * 40
SUPPORTED_CLAIM = (
    "finite-horizon planning reduces terminal ambiguity by 25% under the "
    "declared model with the same cost constraints"
)


def _model_evidence() -> dict[str, object]:
    return evaluate_robust_topology(
        REVISION,
        revision_verification="verified_current_clean_head",
    )


def _arm_verdicts() -> list[dict[str, object]]:
    comparison = build_n7_comparison(
        kauri_revision=REVISION,
        seed=THESIS_COMPARISON_SEED,
        crash_replica_id=1,
        false_reporter_id=6,
        false_report_target_id=1,
        persistent_omitter_id=1,
        diagnostic_window=THESIS_DIAGNOSTIC_WINDOW,
    )
    verdicts = comparison_verdicts(comparison)
    arms: list[dict[str, object]] = []
    for index, name in enumerate(ARM_NAMES):
        verdict = verdicts[name]
        verdict.update(
            {
                "schema_version": 2,
                "scenario": "n7-static-fault-comparison",
                "arm": name,
                "interrupted": False,
                "runtime_error": None,
                "fixed_context_observation": {
                    "active_configuration_records": 12,
                    "configured_fault_threshold": 2,
                    "configured_quorum": 5,
                    "configured_replica_count": 7,
                    "invalid_records": [],
                },
                "common_commit_before": {
                    "block_height": 1,
                    "block_hash": "1" * 64,
                    "participants": list(range(7)),
                    "common_monotonic_raw_ns": 100,
                },
                "common_commit_after": {
                    "block_height": 20 + index,
                    "block_hash": str(index + 2) * 64,
                    "participants": (
                        [0, 2, 3, 4, 5, 6]
                        if name == "sigkill_crash"
                        else list(range(7))
                    ),
                    "common_monotonic_raw_ns": 300,
                },
                "conflicting_commits": [],
            }
        )
        action = verdict["action_observation"]
        assert isinstance(action, dict)
        action["fault_evidence"] = {
            "journal_event_count": 2,
            "journal_path": "raw/fault-orchestrator.jsonl",
            "plan_path": "fault-plan.json",
            "plan_sha256": verdict["fault_plan_sha256"],
            "terminal_status": "succeeded",
        }
        if name == "sigkill_crash":
            action.update(
                {
                    "replica_id": 1,
                    "signal": "SIGKILL",
                    "returncode": -9,
                    "requested_monotonic_raw_ns": 180,
                    "confirmed_monotonic_raw_ns": 200,
                }
            )
        else:
            action.update(
                {
                    "observed_monotonic_raw_ns": 180,
                    "manager_acceptance_observed_monotonic_raw_ns": 200,
                }
            )
        arms.append(verdict)
    return arms


def test_builds_canonical_claim_bundle_from_model_and_live_evidence() -> None:
    model = _model_evidence()
    arms = _arm_verdicts()

    evaluation = build_thesis_evaluation(model, arms)

    assert evaluation["schema_version"] == 2
    assert evaluation["verdict"] == "PASS"
    assert evaluation["kauri_revision"] == REVISION
    assert evaluation["supported_claim"] == SUPPORTED_CLAIM
    assert {
        "no live planner activation",
        "no throughput speedup",
        "no general Byzantine identification",
    } <= set(evaluation["claims_not_made"])
    assert evaluation["model_evidence"] == model
    assert evaluation["live_evidence"]["arm_verdicts"] == arms
    assert evaluation["live_evidence"]["comparison_summary"]["seed"] == 41_719
    assert evaluation["metrics"]["model"]["ambiguity_paths"] == {
        "greedy": [9, 5, 4],
        "lookahead": [9, 6, 3],
    }
    assert evaluation["metrics"]["live"][
        "static_authenticated_false_report"
    ]["observation_outcomes"] == ["timeout", "response"]
    encoded = canonical_thesis_evaluation_json(evaluation)
    assert json.loads(encoded) == evaluation
    assert encoded == canonical_thesis_evaluation_json(
        build_thesis_evaluation(deepcopy(model), deepcopy(arms))
    )

    model["schema_version"] = 999
    arms[0]["runtime_error"] = "late mutation"
    assert evaluation["model_evidence"]["schema_version"] == 2
    assert evaluation["live_evidence"]["arm_verdicts"][0]["runtime_error"] is None


@pytest.mark.parametrize(
    "source, error",
    (
        ('{"verdict":"FAIL","verdict":"PASS"}', "duplicate JSON field"),
        ('{"value":NaN}', "non-finite JSON number"),
        ('{"value":Infinity}', "non-finite JSON number"),
    ),
)
def test_strict_json_contract_rejects_ambiguous_values(
    source: str,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        parse_thesis_json_object(source, "test evidence")

    with pytest.raises(ValueError, match="canonical JSON"):
        canonical_thesis_evaluation_json({"value": float("nan")})


@pytest.mark.parametrize(
    "case, error",
    (
        ("model_schema", "schema version 2"),
        ("model_numeric_type", "canonical exhaustive proof"),
        ("model_unverified", "verified clean revision"),
        ("model_theorem_tamper", "canonical exhaustive proof"),
        ("revision_mismatch", "comparison|revision"),
        ("missing_arm", "arm"),
        ("duplicate_arm", "arm"),
        ("failed_arm", "PASS"),
        ("arm_schema", "schema version 2"),
        ("arm_schema_float", "schema version"),
        ("interrupted", "interrupted"),
        ("runtime_error", "runtime error"),
        ("fixed_context", "fixed context|N=7"),
        ("missing_raw_fault_evidence", "fault evidence"),
        ("wrong_raw_plan_hash", "raw fault evidence"),
        ("malformed_settlement", "comparison contract|settled|hypothesis"),
        ("wrong_participants", "participants"),
        ("boolean_participant", "participants"),
        ("commit_before_fault", "bracket fault evidence"),
        ("cost_regression", "canonical exhaustive proof|cost"),
    ),
)
def test_rejects_unclaimable_evidence(case: str, error: str) -> None:
    model = _model_evidence()
    arms = _arm_verdicts()
    if case == "model_schema":
        model["schema_version"] = 1
    elif case == "model_numeric_type":
        model["passive_reconfiguration_game"]["two_epoch_greedy_separation"][
            "replica_count"
        ] = 31.0
    elif case == "model_unverified":
        model["revision_verification"] = "unverified"
    elif case == "model_theorem_tamper":
        model["passive_reconfiguration_game"]["two_epoch_greedy_separation"][
            "theorem_checks"
        ]["independent_optimum_matches"] = False
    elif case == "revision_mismatch":
        arms[0]["kauri_revision"] = "b" * 40
    elif case == "missing_arm":
        arms.pop()
    elif case == "duplicate_arm":
        arms.append(deepcopy(arms[0]))
    elif case == "failed_arm":
        arms[0]["verdict"] = "INCOMPLETE"
    elif case == "arm_schema":
        arms[0]["schema_version"] = 999
    elif case == "arm_schema_float":
        arms[0]["schema_version"] = 2.0
    elif case == "interrupted":
        arms[0]["interrupted"] = True
    elif case == "runtime_error":
        arms[0]["runtime_error"] = "process failed"
    elif case == "fixed_context":
        arms[0]["fixed_context_observation"] = {}
    elif case == "missing_raw_fault_evidence":
        arms[0]["action_observation"].pop("fault_evidence")
    elif case == "wrong_raw_plan_hash":
        arms[0]["action_observation"]["fault_evidence"]["plan_sha256"] = "f" * 64
    elif case == "malformed_settlement":
        arms[1]["diagnostic_certificate"]["compatible_hypothesis_count"] = 2
    elif case == "wrong_participants":
        arms[0]["common_commit_after"]["participants"] = list(range(7))
    elif case == "boolean_participant":
        arms[0]["common_commit_after"]["participants"][0] = False
    elif case == "commit_before_fault":
        arms[0]["common_commit_after"]["common_monotonic_raw_ns"] = 190
    elif case == "cost_regression":
        model["passive_reconfiguration_game"]["two_epoch_greedy_separation"][
            "extra_reconfigurations_vs_greedy"
        ] = 1

    with pytest.raises(ValueError, match=error):
        build_thesis_evaluation(model, arms)


def test_composer_cli_rejects_duplicate_and_nonfinite_json(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    script = repository_root / "experiments/adaptive/run_thesis_evaluation.py"
    model_source = json.dumps(_model_evidence(), indent=2, sort_keys=True)
    model_path = tmp_path / "model.json"
    arm_paths: list[Path] = []
    for index, arm in enumerate(_arm_verdicts()):
        path = tmp_path / f"arm-{index}.json"
        path.write_text(json.dumps(arm), encoding="utf-8")
        arm_paths.append(path)
    command = [
        sys.executable,
        str(script),
        "--model-evidence",
        str(model_path),
    ]
    for path in arm_paths:
        command.extend(("--arm-verdict", str(path)))

    model_path.write_text(
        model_source.replace(
            "{\n",
            '{\n  "schema_version": 999,\n',
            1,
        ),
        encoding="utf-8",
    )
    duplicate = subprocess.run(
        [*command, "--output-dir", str(tmp_path / "duplicate-output")],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert duplicate.returncode != 0
    assert "duplicate JSON field" in duplicate.stderr
    assert not (tmp_path / "duplicate-output").exists()

    model_path.write_text(model_source, encoding="utf-8")
    arm_source = arm_paths[0].read_text(encoding="utf-8")
    arm_paths[0].write_text(
        arm_source.replace("{", '{"not_finite": NaN,', 1),
        encoding="utf-8",
    )
    nonfinite = subprocess.run(
        [*command, "--output-dir", str(tmp_path / "nonfinite-output")],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert nonfinite.returncode != 0
    assert "non-finite JSON number" in nonfinite.stderr
    assert not (tmp_path / "nonfinite-output").exists()


def test_plot_cli_is_deterministic_and_refuses_tampered_or_minimal_pass(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    pytest.importorskip("matplotlib")
    script = repository_root / "experiments/adaptive/plot_thesis_evaluation.py"
    evaluation = build_thesis_evaluation(
        _model_evidence(),
        _arm_verdicts(),
    )
    evaluation_path = tmp_path / "evaluation.json"
    evaluation_path.write_text(
        canonical_thesis_evaluation_json(evaluation) + "\n",
        encoding="utf-8",
    )
    valid_source = evaluation_path.read_text(encoding="utf-8")
    output_a = tmp_path / "figures-a"
    output_b = tmp_path / "figures-b"
    command = [sys.executable, str(script), "--evaluation", str(evaluation_path)]
    for output in (output_a, output_b):
        completed = subprocess.run(
            [*command, "--output-dir", str(output)],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr

    names = {
        "ambiguity-path.png",
        "ambiguity-path.pdf",
        "live-fault-outcomes.png",
        "live-fault-outcomes.pdf",
    }
    assert {path.name for path in output_a.iterdir()} == names
    assert (output_a / "ambiguity-path.png").read_bytes().startswith(
        b"\x89PNG\r\n\x1a\n"
    )
    assert (output_a / "live-fault-outcomes.pdf").read_bytes().startswith(
        b"%PDF-"
    )
    for name in names:
        assert (output_a / name).read_bytes() == (output_b / name).read_bytes()

    evaluation["metrics"]["model"]["ambiguity_paths"]["lookahead"] = [9, 5, 2]
    evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
    refused = subprocess.run(
        [*command, "--output-dir", str(tmp_path / "refused")],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0
    assert "rebuilt evidence" in refused.stderr
    assert not (tmp_path / "refused").exists()

    numeric_type = build_thesis_evaluation(
        _model_evidence(),
        _arm_verdicts(),
    )
    numeric_type["schema_version"] = 2.0
    evaluation_path.write_text(json.dumps(numeric_type), encoding="utf-8")
    numeric_refused = subprocess.run(
        [*command, "--output-dir", str(tmp_path / "numeric-refused")],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert numeric_refused.returncode != 0
    assert "rebuilt evidence" in numeric_refused.stderr
    assert not (tmp_path / "numeric-refused").exists()

    duplicate_verdict = valid_source.replace(
        "{\n",
        '{\n  "verdict": "FAIL",\n',
        1,
    )
    evaluation_path.write_text(duplicate_verdict, encoding="utf-8")
    duplicate_refused = subprocess.run(
        [*command, "--output-dir", str(tmp_path / "duplicate-refused")],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert duplicate_refused.returncode != 0
    assert "duplicate JSON field" in duplicate_refused.stderr
    assert not (tmp_path / "duplicate-refused").exists()

    evaluation_path.write_text(
        json.dumps(
            {
                "verdict": "PASS",
                "scenario": "joint-hypothesis-thesis-evaluation",
                "metrics": {"model": {}, "live": {}},
            }
        ),
        encoding="utf-8",
    )
    minimal = subprocess.run(
        [*command, "--output-dir", str(tmp_path / "minimal")],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert minimal.returncode != 0
    assert "model evidence" in minimal.stderr
    assert not (tmp_path / "minimal").exists()
