"""Contracts for the campaign-level thesis evidence composition."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from experiments.adaptive.kauri_experiment.campaign_thesis_evaluation import (
    CampaignThesisEvaluationError,
    EVALUATION_CLASS,
    EXCLUSIONS,
    build_campaign_thesis_evaluation,
    canonical_campaign_thesis_evaluation_json,
    validate_campaign_thesis_evaluation,
)
from experiments.adaptive.kauri_experiment.n7_fault_repetition_campaign import (
    canonical_n7_fault_repetition_json,
)
from experiments.adaptive.kauri_experiment.planning_breadth_campaign import (
    build_planning_breadth_campaign,
    canonical_planning_breadth_json,
)
from experiments.adaptive.plot_campaign_thesis_evaluation import (
    PlotError,
    generate_figure,
)
from experiments.adaptive.tests.test_n7_fault_repetition_campaign import (
    _comparison,
    _install_fake_arm_runner,
)

REVISION = "a" * 40


def _materialize_live_campaign(
    repository_root: Path,
    output: Path,
    *,
    missing_ordinal: int | None = None,
) -> dict[str, Any]:
    runner = importlib.import_module(
        "experiments.adaptive.run_n7_fault_repetition_campaign"
    )
    patcher = pytest.MonkeyPatch()
    try:
        _install_fake_arm_runner(
            patcher,
            runner,
            _comparison(),
            missing_ordinal=missing_ordinal,
        )
        artifact = runner.run_campaign(
            repository=repository_root,
            output_directory=output,
            kauri_revision=REVISION,
        )
    finally:
        patcher.undo()
    parsed = json.loads(artifact.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture(scope="module")
def source_campaigns(
    tmp_path_factory: pytest.TempPathFactory,
    repository_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    planning = build_planning_breadth_campaign(
        kauri_revision=REVISION,
        revision_verification="verified_current_clean_head",
    )
    evidence_root = tmp_path_factory.mktemp("campaign-live-evidence") / "n7-campaign"
    live = _materialize_live_campaign(
        repository_root,
        evidence_root,
    )
    assert live["verdict"] == "PASS"
    return planning, live, evidence_root


@pytest.fixture(scope="module")
def composed_campaign(
    source_campaigns: tuple[dict[str, Any], dict[str, Any], Path],
) -> dict[str, Any]:
    planning, live, evidence_root = source_campaigns
    return build_campaign_thesis_evaluation(
        planning,
        live,
        live_evidence_root=evidence_root,
    )


def test_reconstructs_both_sources_into_one_canonical_pass_artifact(
    source_campaigns: tuple[dict[str, Any], dict[str, Any], Path],
    composed_campaign: dict[str, Any],
) -> None:
    planning, live, evidence_root = source_campaigns
    assert composed_campaign["schema_version"] == 1
    assert composed_campaign["scenario"] == "campaign-level-thesis-evaluation"
    assert composed_campaign["verdict"] == "PASS"
    assert composed_campaign["kauri_revision"] == REVISION
    assert composed_campaign["evaluation_class"] == EVALUATION_CLASS
    assert composed_campaign["exclusions"] == list(EXCLUSIONS)
    assert composed_campaign["observations"] == {
        "model_policy": {
            "synthetic_scenarios": 1_000,
            "solver_reference_agreements": 1_000,
            "canonical_greedy_wins": 23,
            "tie_robust_wins": 4,
            "lookahead_losses": 0,
            "cost_regressions": 0,
        },
        "live_observation_calibration": {
            "scheduled_no_retry_slots": 15,
            "validated_slots": 15,
            "replacement_slots": 0,
            "passes_per_arm": {
                "sigkill_crash": 5,
                "static_authenticated_false_report": 5,
                "static_persistent_omission": 5,
            },
            "post_fault_common_commits": 15,
            "false_report_settlements": 5,
            "omission_settlements": 5,
            "missing_slots": 0,
            "failed_slots": 0,
        },
    }
    assert composed_campaign["source_artifacts"] == {
        "planning_breadth_campaign": planning,
        "n7_fault_repetition_campaign": live,
    }
    assert composed_campaign["source_canonical_sha256"] == {
        "planning_breadth_campaign": hashlib.sha256(
            canonical_planning_breadth_json(planning).encode("utf-8")
        ).hexdigest(),
        "n7_fault_repetition_campaign": hashlib.sha256(
            canonical_n7_fault_repetition_json(live).encode("utf-8")
        ).hexdigest(),
    }

    assert validate_campaign_thesis_evaluation(
        composed_campaign,
        live_evidence_root=evidence_root,
    ) == (composed_campaign)
    encoded = canonical_campaign_thesis_evaluation_json(
        composed_campaign,
        live_evidence_root=evidence_root,
    )
    assert encoded == json.dumps(
        composed_campaign,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert json.loads(encoded) == composed_campaign


def test_rejects_individually_valid_sources_from_different_revisions(
    source_campaigns: tuple[dict[str, Any], dict[str, Any], Path],
) -> None:
    _, live, evidence_root = source_campaigns
    other_revision_planning = build_planning_breadth_campaign(
        kauri_revision="b" * 40,
        revision_verification="verified_current_clean_head",
    )

    with pytest.raises(
        CampaignThesisEvaluationError,
        match="different Kauri revisions",
    ):
        build_campaign_thesis_evaluation(
            other_revision_planning,
            live,
            live_evidence_root=evidence_root,
        )


def test_rejects_planning_evidence_from_a_dirty_head(
    source_campaigns: tuple[dict[str, Any], dict[str, Any], Path],
) -> None:
    planning, live, evidence_root = source_campaigns
    dirty_planning = deepcopy(planning)
    dirty_planning["revision_verification"] = "verified_current_head_dirty_override"

    with pytest.raises(
        CampaignThesisEvaluationError,
        match="verified current clean head",
    ):
        build_campaign_thesis_evaluation(
            dirty_planning,
            live,
            live_evidence_root=evidence_root,
        )


def test_rejects_tampered_embedded_source(
    source_campaigns: tuple[dict[str, Any], dict[str, Any], Path],
    composed_campaign: dict[str, Any],
) -> None:
    _, _, evidence_root = source_campaigns
    tampered = deepcopy(composed_campaign)
    tampered["source_artifacts"]["planning_breadth_campaign"]["summary"][
        "canonical_greedy"
    ]["wins"] = 24

    with pytest.raises(
        CampaignThesisEvaluationError,
        match="planning source failed strict validation",
    ):
        validate_campaign_thesis_evaluation(
            tampered,
            live_evidence_root=evidence_root,
        )


def test_rejects_live_campaign_with_a_missing_scheduled_slot(
    source_campaigns: tuple[dict[str, Any], dict[str, Any], Path],
    repository_root: Path,
    tmp_path: Path,
) -> None:
    planning, _, _ = source_campaigns
    evidence_root = tmp_path / "incomplete-live-evidence"
    incomplete = _materialize_live_campaign(
        repository_root,
        evidence_root,
        missing_ordinal=15,
    )
    assert incomplete["verdict"] == "INCOMPLETE"
    assert len(incomplete["missing_attempts"]) == 1

    with pytest.raises(
        CampaignThesisEvaluationError,
        match="live source does not contain five valid",
    ):
        build_campaign_thesis_evaluation(
            planning,
            incomplete,
            live_evidence_root=evidence_root,
        )


@pytest.mark.parametrize("root_kind", ("missing", "wrong"))
def test_composition_and_plot_refuse_an_unbound_live_evidence_root(
    root_kind: str,
    tmp_path: Path,
    source_campaigns: tuple[dict[str, Any], dict[str, Any], Path],
    composed_campaign: dict[str, Any],
) -> None:
    planning, live, _ = source_campaigns
    invalid_root = tmp_path / root_kind
    if root_kind == "wrong":
        invalid_root.mkdir()

    with pytest.raises(
        CampaignThesisEvaluationError,
        match="live source failed strict validation",
    ):
        build_campaign_thesis_evaluation(
            planning,
            live,
            live_evidence_root=invalid_root,
        )

    artifact = tmp_path / f"{root_kind}-root.json"
    artifact.write_text(json.dumps(composed_campaign), encoding="utf-8")
    with pytest.raises(
        PlotError,
        match="campaign thesis evidence failed validation",
    ):
        generate_figure(
            artifact,
            tmp_path / f"{root_kind}-figures",
            live_evidence_root=invalid_root,
        )


def test_composition_and_plot_refuse_no_live_evidence_root(
    tmp_path: Path,
    source_campaigns: tuple[dict[str, Any], dict[str, Any], Path],
    composed_campaign: dict[str, Any],
) -> None:
    planning, live, _ = source_campaigns
    with pytest.raises(
        CampaignThesisEvaluationError,
        match="a bound evidence root is required",
    ):
        build_campaign_thesis_evaluation(
            planning,
            live,
            live_evidence_root=None,  # type: ignore[arg-type]
        )

    artifact = tmp_path / "no-root.json"
    artifact.write_text(json.dumps(composed_campaign), encoding="utf-8")
    with pytest.raises(
        PlotError,
        match="campaign thesis evidence failed validation",
    ):
        generate_figure(
            artifact,
            tmp_path / "no-root-figures",
            live_evidence_root=None,  # type: ignore[arg-type]
        )


def test_plotter_refuses_a_nonpass_artifact(
    tmp_path: Path,
    source_campaigns: tuple[dict[str, Any], dict[str, Any], Path],
    composed_campaign: dict[str, Any],
) -> None:
    _, _, evidence_root = source_campaigns
    nonpass = deepcopy(composed_campaign)
    nonpass["verdict"] = "INCOMPLETE"
    artifact = tmp_path / "nonpass.json"
    artifact.write_text(json.dumps(nonpass), encoding="utf-8")

    with pytest.raises(PlotError, match="only a PASS campaign"):
        generate_figure(
            artifact,
            tmp_path / "figures",
            live_evidence_root=evidence_root,
        )
    assert not (tmp_path / "figures").exists()


def test_plotter_reconstructs_sources_and_writes_deterministic_png_pdf(
    tmp_path: Path,
    source_campaigns: tuple[dict[str, Any], dict[str, Any], Path],
    composed_campaign: dict[str, Any],
) -> None:
    _, _, evidence_root = source_campaigns
    pytest.importorskip("matplotlib")
    artifact = tmp_path / "campaign-thesis-evaluation.json"
    artifact.write_text(
        json.dumps(
            composed_campaign,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    first = generate_figure(
        artifact,
        tmp_path / "first",
        live_evidence_root=evidence_root,
    )
    second = generate_figure(
        artifact,
        tmp_path / "second",
        live_evidence_root=evidence_root,
    )
    assert tuple(path.name for path in first) == (
        "campaign-thesis-evaluation.pdf",
        "campaign-thesis-evaluation.png",
    )
    assert tuple(path.name for path in second) == tuple(path.name for path in first)
    for first_path, second_path in zip(first, second, strict=True):
        assert first_path.stat().st_size > 0
        assert first_path.read_bytes() == second_path.read_bytes()
