"""Fail-closed plotting contracts for the sealed N=31 PQAR campaign."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from experiments.adaptive.kauri_experiment.n31_post_qc_audit_campaign import (
    ARM_NAMES,
    CAMPAIGN_PROFILE_ID,
    CAMPAIGN_SCENARIO,
    CLASSIFICATION_NAMES,
    N31PostQcAuditCampaignError,
    TIMING_METRICS_NS,
    clopper_pearson_interval,
    nanosecond_metric_summary,
    semantic_document_sha256,
)
from experiments.adaptive import plot_n31_post_qc_audit_campaign as plotter

REVISION = "a" * 40
SHA256 = "b" * 64
EXPECTED_CLASSIFICATION = {
    "static_authenticated_false_report": "false_reporter",
    "static_persistent_direct_vote_omission": "omission_compatible",
    "static_authenticated_sham": "sham",
}
CLASSIFICATION_COUNTS = {
    ARM_NAMES[0]: {
        "false_reporter": 26,
        "omission_compatible": 2,
        "sham": 1,
        "unclassified": 1,
    },
    ARM_NAMES[1]: {
        "false_reporter": 1,
        "omission_compatible": 25,
        "sham": 2,
        "unclassified": 2,
    },
    ARM_NAMES[2]: {
        "false_reporter": 1,
        "omission_compatible": 1,
        "sham": 27,
        "unclassified": 1,
    },
}
MIXED_OUTCOMES = {
    ARM_NAMES[0]: {"PASS": 25, "FAIL": 3, "INCOMPLETE": 2},
    ARM_NAMES[1]: {"PASS": 20, "FAIL": 5, "INCOMPLETE": 5},
    ARM_NAMES[2]: {"PASS": 30, "FAIL": 0, "INCOMPLETE": 0},
}


def _rate(successes: int) -> dict[str, object]:
    lower, upper = clopper_pearson_interval(successes, 30)
    return {
        "successes": successes,
        "trials": 30,
        "estimate": f"{successes / 30:.10f}",
        "confidence_level": "0.95",
        "method": "two-sided-clopper-pearson-exact",
        "lower": f"{lower:.10f}",
        "upper": f"{upper:.10f}",
    }


def _expanded_labels(counts: dict[str, int]) -> list[str]:
    return [
        classification
        for classification in CLASSIFICATION_NAMES
        for _ in range(counts[classification])
    ]


def _expanded_outcomes(counts: dict[str, int]) -> list[str]:
    return [
        verdict
        for verdict in ("PASS", "FAIL", "INCOMPLETE")
        for _ in range(counts[verdict])
    ]


def _accepted_summary(*, mixed: bool = False) -> dict[str, Any]:
    execution_records: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    timing_values = {
        arm: {metric: [] for metric in TIMING_METRICS_NS} for arm in ARM_NAMES
    }
    outcomes = (
        MIXED_OUTCOMES
        if mixed
        else {arm: {"PASS": 30, "FAIL": 0, "INCOMPLETE": 0} for arm in ARM_NAMES}
    )
    ordinal = 0
    for arm_index, arm in enumerate(ARM_NAMES):
        labels = _expanded_labels(CLASSIFICATION_COUNTS[arm])
        verdicts = _expanded_outcomes(outcomes[arm])
        for local_index, (classification, verdict) in enumerate(
            zip(labels, verdicts, strict=True), start=1
        ):
            ordinal += 1
            execution_records.append(
                {
                    "ordinal": ordinal,
                    "arm": arm,
                    "launch_status": "returned",
                    "original_verdict": verdict,
                }
            )
            metrics: dict[str, int] = {}
            for metric_index, metric in enumerate(TIMING_METRICS_NS):
                # Preserve real missingness in one metric; every other metric has
                # the full n=30 per arm.  The plotter must expose, not impute, it.
                if metric_index == 1 and (local_index + arm_index) % 7 == 0:
                    continue
                value = (
                    ordinal * 1_000_000
                    + metric_index * 10_000
                    + (ordinal % 2 if metric_index == 0 else 0)
                )
                metrics[metric] = value
                timing_values[arm][metric].append(value)
            observations.append(
                {
                    "ordinal": ordinal,
                    "observation": {
                        "classification": classification,
                        "metrics_ns": metrics,
                    },
                }
            )

    aggregate_outcomes = {
        verdict: sum(outcomes[arm][verdict] for arm in ARM_NAMES)
        for verdict in ("PASS", "FAIL", "INCOMPLETE")
    }
    return {
        "schema_version": 1,
        "scenario": CAMPAIGN_SCENARIO,
        "campaign_profile": {
            "profile_id": CAMPAIGN_PROFILE_ID,
            "sha256": SHA256,
        },
        "kauri_revision": REVISION,
        "campaign_plan_sha256": SHA256,
        "trusted_provenance_sha256": SHA256,
        "pilot_gate_sha256": SHA256,
        "campaign_acceptance": "ACCEPTED",
        "outcome_status": "MIXED" if mixed else "ALL_QUALIFIED",
        "figure_eligible": True,
        "integrity_failures": [],
        "controller_failures": [],
        "scheduled_attempts": 90,
        "returned_invocations": 90,
        "fixed_denominator_per_arm": 30,
        "outcome_counts": aggregate_outcomes,
        "invocation_status_counts": {
            "returned": 90,
            "raised": 0,
            "not_started": 0,
        },
        "confusion_table": deepcopy(CLASSIFICATION_COUNTS),
        "rates": {
            arm: {
                "classification_correctness": _rate(
                    CLASSIFICATION_COUNTS[arm][EXPECTED_CLASSIFICATION[arm]]
                ),
                "strict_harness_qualification": _rate(outcomes[arm]["PASS"]),
            }
            for arm in ARM_NAMES
        },
        "timing_summaries_ns": {
            arm: {
                metric: nanosecond_metric_summary(timing_values[arm][metric])
                for metric in TIMING_METRICS_NS
            }
            for arm in ARM_NAMES
        },
        "execution_records": execution_records,
        "blind_observations": observations,
        "statistical_scope": (
            "same-host descriptive nanosecond timings; exact binomial intervals "
            "assume Bernoulli repetitions that only approximate independence "
            "and identical distribution"
        ),
        "claim_boundary": (
            "omission-compatible classification is not persistent-omitter "
            "identification, throughput evidence, or a consensus-safety proof"
        ),
    }


def _install_validation_stubs(
    monkeypatch: pytest.MonkeyPatch,
    summary: dict[str, Any],
) -> list[tuple[Path, object]]:
    calls: list[tuple[Path, object]] = []

    def validate(path: Path, *, trusted_provenance: object) -> dict[str, Any]:
        calls.append((path, trusted_provenance))
        return deepcopy(summary)

    monkeypatch.setattr(plotter, "validate_n31_pqar_campaign", validate)
    monkeypatch.setattr(
        plotter,
        "verify_evidence_seal",
        lambda _path: SimpleNamespace(tree_sha256="c" * 64, seal_sha256="d" * 64),
    )
    return calls


def test_accepted_mixed_campaign_renders_three_deterministic_figure_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("matplotlib")
    summary = _accepted_summary(mixed=True)
    assert (
        summary["timing_summaries_ns"][ARM_NAMES[0]][TIMING_METRICS_NS[0]]["median_ns"]
        == 15_500_000.5
    )
    calls = _install_validation_stubs(monkeypatch, summary)
    campaign = tmp_path / "sealed-campaign"
    campaign.mkdir()
    trusted = object()
    home_before = os.environ.get("HOME")
    mpl_before = os.environ.get("MPLCONFIGDIR")
    xdg_before = os.environ.get("XDG_CACHE_HOME")

    first = plotter.generate_figures(
        campaign, tmp_path / "figures-a", trusted_provenance=trusted
    )
    second = plotter.generate_figures(
        campaign, tmp_path / "figures-b", trusted_provenance=trusted
    )

    expected_names = (
        "figure-manifest.json",
        "n31-pqar-confusion-matrix.pdf",
        "n31-pqar-confusion-matrix.png",
        "n31-pqar-rates-outcomes.pdf",
        "n31-pqar-rates-outcomes.png",
        "n31-pqar-timings.pdf",
        "n31-pqar-timings.png",
    )
    assert tuple(path.name for path in first) == expected_names
    assert tuple(path.name for path in second) == expected_names
    for first_path, second_path in zip(first, second, strict=True):
        assert first_path.stat().st_size > 0
        assert first_path.read_bytes() == second_path.read_bytes()
    assert calls == [(campaign.resolve(), trusted), (campaign.resolve(), trusted)]
    assert os.environ.get("HOME") == home_before
    assert os.environ.get("MPLCONFIGDIR") == mpl_before
    assert os.environ.get("XDG_CACHE_HOME") == xdg_before

    manifest = json.loads(first[0].read_text(encoding="utf-8"))
    assert manifest["source"] == {
        "campaign_acceptance": "ACCEPTED",
        "campaign_plan_sha256": SHA256,
        "campaign_profile": summary["campaign_profile"],
        "evidence_seal_sha256": "d" * 64,
        "evidence_tree_sha256": "c" * 64,
        "figure_eligible": True,
        "fixed_denominator_per_arm": 30,
        "kauri_revision": REVISION,
        "outcome_status": "MIXED",
        "pilot_gate_sha256": SHA256,
        "scheduled_attempts": 90,
        "trusted_provenance_sha256": SHA256,
        "validated_summary_sha256": semantic_document_sha256(summary),
    }
    assert [item["path"] for item in manifest["files"]] == list(expected_names[1:])
    for item in manifest["files"]:
        rendered = first[0].parent / item["path"]
        assert item["sha256"] == hashlib.sha256(rendered.read_bytes()).hexdigest()
        assert item["size_bytes"] == rendered.stat().st_size
    assert manifest["output_membership"] == list(expected_names)
    caveat_text = " ".join(manifest["caveats"]).lower()
    for required in (
        "omission-compatible",
        "same-host",
        "culpability",
        "detection",
        "throughput",
        "safety",
    ):
        assert required in caveat_text


@pytest.mark.parametrize(
    ("acceptance", "eligible", "message"),
    (
        ("REJECTED", False, "ACCEPTED"),
        ("ACCEPTED", False, "figure-eligible"),
    ),
)
def test_rejects_nonaccepted_and_pilot_only_summaries_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    acceptance: str,
    eligible: bool,
    message: str,
) -> None:
    summary = _accepted_summary()
    summary["campaign_acceptance"] = acceptance
    summary["figure_eligible"] = eligible
    _install_validation_stubs(monkeypatch, summary)
    output = tmp_path / "figures"

    with pytest.raises(plotter.PlotError, match=message):
        plotter.generate_figures(
            tmp_path / "campaign", output, trusted_provenance=object()
        )
    assert not output.exists()


def test_rejects_an_interrupted_suffix_even_if_flags_claim_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _accepted_summary(mixed=True)
    summary["execution_records"][-1]["launch_status"] = "not_started"
    summary["execution_records"][-1]["original_verdict"] = None
    summary["returned_invocations"] = 89
    summary["invocation_status_counts"] = {
        "returned": 89,
        "raised": 0,
        "not_started": 1,
    }
    _install_validation_stubs(monkeypatch, summary)
    output = tmp_path / "figures"

    with pytest.raises(plotter.PlotError, match="invoked|returned|interrupted"):
        plotter.generate_figures(
            tmp_path / "campaign", output, trusted_provenance=object()
        )
    assert not output.exists()


def test_rejects_unsealed_or_tampered_campaign_before_creating_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path] = []

    def reject(path: Path, *, trusted_provenance: object) -> dict[str, Any]:
        del trusted_provenance
        calls.append(path)
        raise N31PostQcAuditCampaignError("outer campaign seal rejected: tampered")

    monkeypatch.setattr(plotter, "validate_n31_pqar_campaign", reject)
    output = tmp_path / "figures"
    with pytest.raises(plotter.PlotError, match="strict validation"):
        plotter.generate_figures(
            tmp_path / "campaign", output, trusted_provenance=object()
        )
    assert calls == [(tmp_path / "campaign").resolve()]
    assert not output.exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("denominator", "denominator"),
        ("missing", "rates"),
        ("nan", "finite"),
        ("hidden_timing_drop", "available|hidden|timing"),
        ("interval_drift", "Clopper|interval"),
        ("confusion_drift", "denominator|confusion"),
        ("controller_failure", "controller failures"),
    ),
)
def test_semantic_adapter_rejects_summary_drift_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    summary = _accepted_summary(mixed=True)
    if mutation == "denominator":
        summary["fixed_denominator_per_arm"] = 29
    elif mutation == "missing":
        del summary["rates"]
    elif mutation == "nan":
        summary["rates"][ARM_NAMES[0]]["classification_correctness"]["estimate"] = (
            float("nan")
        )
    elif mutation == "hidden_timing_drop":
        del summary["blind_observations"][0]["observation"]["metrics_ns"][
            TIMING_METRICS_NS[0]
        ]
    elif mutation == "interval_drift":
        summary["rates"][ARM_NAMES[0]]["classification_correctness"][
            "upper"
        ] = "0.1000000000"
    elif mutation == "confusion_drift":
        summary["confusion_table"][ARM_NAMES[0]]["unclassified"] = 0
    elif mutation == "controller_failure":
        summary["controller_failures"] = ["summary_write_failed"]
    else:  # pragma: no cover - keeps the fixture mutation exhaustive.
        raise AssertionError(mutation)
    _install_validation_stubs(monkeypatch, summary)
    output = tmp_path / "figures"

    with pytest.raises(plotter.PlotError, match=message):
        plotter.generate_figures(
            tmp_path / "campaign", output, trusted_provenance=object()
        )
    assert not output.exists()


def test_rejects_output_inside_the_sealed_campaign_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _accepted_summary()
    _install_validation_stubs(monkeypatch, summary)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    output = campaign / "figures"

    with pytest.raises(plotter.PlotError, match="outside.*campaign"):
        plotter.generate_figures(campaign, output, trusted_provenance=object())
    assert not output.exists()


def test_rejects_existing_output_directory_without_overwriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_validation_stubs(monkeypatch, _accepted_summary())
    output = tmp_path / "figures"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(plotter.PlotError, match="already exists"):
        plotter.generate_figures(
            tmp_path / "campaign", output, trusted_provenance=object()
        )
    assert sentinel.read_text(encoding="utf-8") == "keep"
