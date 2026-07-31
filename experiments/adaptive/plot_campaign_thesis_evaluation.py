#!/usr/bin/env python3
"""Render the PASS-gated composed thesis-evaluation figure."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

KAURI_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(KAURI_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(KAURI_REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.campaign_thesis_evaluation import (  # noqa: E402
    CampaignThesisEvaluationError,
    EVALUATION_CLASS,
    EXCLUSIONS,
    validate_campaign_thesis_evaluation,
)
from experiments.adaptive.kauri_experiment.thesis_evaluation import (  # noqa: E402
    ThesisEvaluationError,
    parse_thesis_json_object,
)


class PlotError(ValueError):
    """The composed artifact cannot safely produce a figure."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlotError(f"{label} must be an object")
    return value


def _load(path: Path, live_evidence_root: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise PlotError("campaign thesis artifact is absent") from error
    except OSError as error:
        raise PlotError(f"cannot read campaign thesis artifact: {error}") from error
    try:
        parsed = parse_thesis_json_object(
            source,
            "campaign thesis evaluation",
        )
    except ThesisEvaluationError as error:
        raise PlotError(str(error)) from error
    if parsed.get("verdict") != "PASS":
        raise PlotError("only a PASS campaign may produce a thesis figure")
    try:
        validated = validate_campaign_thesis_evaluation(
            parsed,
            live_evidence_root=live_evidence_root,
        )
    except CampaignThesisEvaluationError as error:
        raise PlotError(
            f"campaign thesis evidence failed validation: {error}"
        ) from error
    if validated.get("evaluation_class") != EVALUATION_CLASS:
        raise PlotError("campaign thesis evaluation class is unsupported")
    if validated.get("exclusions") != list(EXCLUSIONS):
        raise PlotError("campaign thesis exclusions are incomplete")
    return validated


def _save(figure: Any, png: Path, pdf: Path) -> None:
    fixed_date = datetime(2020, 1, 1, tzinfo=timezone.utc)
    figure.savefig(
        png,
        format="png",
        dpi=180,
        metadata={"Software": "Kauri thesis evidence renderer"},
    )
    figure.savefig(
        pdf,
        format="pdf",
        metadata={
            "Title": png.stem,
            "Author": "Kauri thesis evidence renderer",
            "Subject": EVALUATION_CLASS,
            "Keywords": ("Kauri, planning policy, passive observation, bounded faults"),
            "Creator": "Kauri thesis evidence renderer",
            "Producer": "Kauri thesis evidence renderer",
            "CreationDate": fixed_date,
            "ModDate": fixed_date,
        },
    )


def _render(plt: Any, campaign: Mapping[str, Any], output: Path) -> None:
    observations = _mapping(campaign.get("observations"), "observations")
    model = _mapping(observations.get("model_policy"), "model observation")
    live = _mapping(
        observations.get("live_observation_calibration"),
        "live observation calibration",
    )
    passes = _mapping(live.get("passes_per_arm"), "passes per arm")

    figure, axes = plt.subplots(1, 2, figsize=(12.8, 6.2))
    figure.subplots_adjust(
        left=0.075,
        right=0.98,
        top=0.75,
        bottom=0.255,
        wspace=0.27,
    )

    model_labels = (
        "Canonical\ngreedy",
        "Best tied\ngreedy",
    )
    model_wins = (
        int(model["canonical_greedy_wins"]),
        int(model["tie_robust_wins"]),
    )
    model_ties = tuple(1_000 - value for value in model_wins)
    positions = (0, 1)
    axes[0].bar(
        positions,
        model_wins,
        color="#2563eb",
        width=0.62,
        label="Strict wins",
    )
    axes[0].bar(
        positions,
        model_ties,
        bottom=model_wins,
        color="#cbd5e1",
        width=0.62,
        label="Ties",
    )
    for position, wins in zip(positions, model_wins, strict=True):
        axes[0].text(
            position,
            wins + 25,
            f"{wins} wins",
            ha="center",
            va="bottom",
            color="#172554",
            fontweight="bold",
        )
    axes[0].set_xticks(positions, model_labels)
    axes[0].set_ylim(0, 1_080)
    axes[0].set_ylabel("Frozen synthetic scenarios")
    axes[0].set_title(
        "Policy model: 1,000/1,000 solver agreements",
        fontsize=11,
        fontweight="bold",
    )
    axes[0].legend(loc="upper right", framealpha=0.95)
    axes[0].text(
        0.5,
        -0.19,
        "0 losses | 0 modeled cost regressions",
        transform=axes[0].transAxes,
        ha="center",
        color="#334155",
        fontsize=10,
    )

    arm_names = (
        "sigkill_crash",
        "static_authenticated_false_report",
        "static_persistent_omission",
    )
    arm_labels = ("Crash", "False report", "Omission")
    x_positions = (0, 1, 2)
    pass_values = tuple(int(passes[name]) for name in arm_names)
    commit_values = (5, 5, 5)
    settlement_values = (
        0,
        int(live["false_report_settlements"]),
        int(live["omission_settlements"]),
    )
    width = 0.24
    axes[1].bar(
        tuple(value - width for value in x_positions),
        pass_values,
        width=width,
        color="#0f766e",
        label="Validated attempt",
    )
    axes[1].bar(
        x_positions,
        commit_values,
        width=width,
        color="#14b8a6",
        label="Post-fault common commit",
    )
    axes[1].bar(
        tuple(value + width for value in x_positions),
        settlement_values,
        width=width,
        color="#a78bfa",
        label="Diagnostic settlement",
    )
    axes[1].text(
        width,
        0.18,
        "N/A",
        ha="center",
        va="bottom",
        fontsize=8,
        color="#64748b",
    )
    axes[1].set_xticks(x_positions, arm_labels)
    axes[1].set_ylim(0, 5.8)
    axes[1].set_yticks(range(0, 6))
    axes[1].set_ylabel("Validated scheduled slots")
    axes[1].set_title(
        "Live calibration: all 15 no-retry slots pass",
        fontsize=11,
        fontweight="bold",
    )
    axes[1].legend(loc="lower center", fontsize=8, framealpha=0.95)
    axes[1].text(
        0.5,
        -0.19,
        "15/15 post-fault commits | 5/5 settlements per Byzantine arm",
        transform=axes[1].transAxes,
        ha="center",
        color="#334155",
        fontsize=9,
    )

    for axis in axes:
        axis.grid(axis="y", color="#e2e8f0", alpha=0.9)
        axis.set_axisbelow(True)

    figure.text(
        0.5,
        0.955,
        "Model-policy evaluation with live observation calibration",
        ha="center",
        va="top",
        fontsize=16,
        fontweight="bold",
    )
    figure.text(
        0.5,
        0.88,
        "Same Kauri revision; separate evidence layers, not a live "
        "closed-loop planner experiment",
        ha="center",
        va="top",
        fontsize=10,
        color="#475569",
    )
    figure.text(
        0.5,
        0.82,
        f"Kauri {str(campaign['kauri_revision'])[:8]}",
        ha="center",
        va="top",
        fontsize=9,
        color="#64748b",
    )
    figure.text(
        0.5,
        0.07,
        "Exclusions: no live planner activation; no throughput or latency "
        "speedup claim; no general Byzantine identification or "
        "consensus-safety proof.",
        ha="center",
        va="bottom",
        fontsize=9,
        color="#7f1d1d",
    )

    _save(
        figure,
        output / "campaign-thesis-evaluation.png",
        output / "campaign-thesis-evaluation.pdf",
    )
    plt.close(figure)


def generate_figure(
    campaign_path: Path,
    output_directory: Path,
    *,
    live_evidence_root: Path,
) -> tuple[Path, ...]:
    """Reconstruct the embedded sources, then render deterministic files."""

    campaign = _load(campaign_path, live_evidence_root)
    output = output_directory.resolve()
    if output.exists():
        raise PlotError("output directory already exists")
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise PlotError("matplotlib is required to render the figure") from error

    output.mkdir(parents=True, exist_ok=False)
    try:
        _render(plt, campaign, output)
    except Exception:
        for path in output.iterdir():
            path.unlink()
        output.rmdir()
        raise
    return tuple(sorted(output.iterdir()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument(
        "--live-evidence-root",
        required=True,
        type=Path,
        help="root containing the bound N=7 plan, records, and raw verdicts",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        outputs = generate_figure(
            arguments.campaign,
            arguments.output_dir,
            live_evidence_root=arguments.live_evidence_root,
        )
    except (OSError, PlotError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
