#!/usr/bin/env python3
"""Render PASS-gated figures for the N=31 planning breadth audit."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence


KAURI_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(KAURI_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(KAURI_REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.planning_breadth_campaign import (  # noqa: E402
    PlanningBreadthError,
    validate_planning_breadth_campaign,
)


_REVISION = re.compile(r"^[0-9a-f]{40}$")


class PlotError(ValueError):
    """The breadth artifact cannot safely produce figures."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PlotError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise PlotError(f"non-finite JSON number is not canonical: {value}")


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlotError(f"{label} must be an object")
    return value


def _load(path: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise PlotError("planning breadth artifact is absent") from error
    except OSError as error:
        raise PlotError(f"cannot read planning breadth artifact: {error}") from error
    try:
        parsed = json.loads(
            source,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as error:
        raise PlotError(f"cannot parse planning breadth artifact: {error}") from error
    if not isinstance(parsed, dict):
        raise PlotError("planning breadth artifact must contain an object")
    try:
        campaign = validate_planning_breadth_campaign(parsed)
    except PlanningBreadthError as error:
        raise PlotError(f"planning breadth evidence failed validation: {error}") from error
    revision = campaign.get("kauri_revision")
    verification = campaign.get("revision_verification")
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise PlotError("planning breadth artifact is not revision-bound")
    if verification != "verified_current_clean_head":
        raise PlotError(
            "planning breadth artifact is not bound to a verified clean head"
        )
    if campaign.get("verdict") != "PASS":
        raise PlotError("planning breadth artifact is not PASS")
    return campaign


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
            "Subject": "Synthetic N=31 passive-planning breadth audit",
            "Keywords": "Kauri, planning, ambiguity, deterministic audit",
            "Creator": "Kauri thesis evidence renderer",
            "Producer": "Kauri thesis evidence renderer",
            "CreationDate": fixed_date,
            "ModDate": fixed_date,
        },
    )


def _plot_policy_outcomes(plt: Any, campaign: Mapping[str, Any], output: Path) -> None:
    summary = _mapping(campaign["summary"], "campaign summary")
    comparisons = (
        (
            "Canonical greedy\n(all scenarios)",
            _mapping(summary["canonical_greedy"], "canonical greedy"),
        ),
        (
            "Best tie-resolved greedy\n(all scenarios)",
            _mapping(
                summary["best_tie_resolved_greedy"],
                "best tie-resolved greedy",
            ),
        ),
        (
            "Canonical greedy\n(unique first action)",
            _mapping(
                summary["unique_greedy_first_action"],
                "unique greedy first action",
            ),
        ),
    )
    figure, axis = plt.subplots(figsize=(10.2, 5.6), layout="constrained")
    y_positions = tuple(range(len(comparisons)))
    colors = {"wins": "#2563eb", "ties": "#cbd5e1", "losses": "#dc2626"}
    left = [0.0] * len(comparisons)
    for relation in ("wins", "ties", "losses"):
        widths: list[float] = []
        for _, values in comparisons:
            total = values.get("scenarios", 0) or sum(
                int(values[name]) for name in ("wins", "ties", "losses")
            )
            widths.append(int(values[relation]) * 100.0 / total)
        axis.barh(
            y_positions,
            widths,
            left=left,
            color=colors[relation],
            height=0.58,
            label=relation.capitalize(),
        )
        left = [start + width for start, width in zip(left, widths, strict=True)]

    for y, (_, values) in zip(y_positions, comparisons, strict=True):
        total = values.get("scenarios", 0) or sum(
            int(values[name]) for name in ("wins", "ties", "losses")
        )
        wins = int(values["wins"])
        ties = int(values["ties"])
        axis.text(
            min(99.0, wins * 100.0 / total + 0.8),
            y,
            f"{wins} strict wins; {ties} ties; 0 losses (n={total})",
            va="center",
            ha="left",
            fontsize=9,
            color="#0f172a",
            fontweight="bold" if wins else "normal",
        )
    axis.set_yticks(y_positions, tuple(label for label, _ in comparisons))
    axis.invert_yaxis()
    axis.set_xlim(0, 100)
    axis.set_xlabel("Share of frozen scenarios (%)")
    axis.grid(axis="x", color="#e2e8f0", alpha=0.8)
    axis.legend(loc="lower right", ncol=3, framealpha=0.95)
    figure.suptitle(
        "Two-epoch planning is never worse and sometimes strictly better",
        fontsize=15,
        fontweight="bold",
    )
    axis.set_title(
        "1,000 deterministic synthetic N=31 games; identical modeled "
        "exposure, latency, epochs, and messages | Kauri "
        f"{str(campaign['kauri_revision'])[:8]}",
        fontsize=9,
        color="#475569",
        pad=10,
    )
    axis.text(
        0.5,
        -0.17,
        "Uniform partition-set audit, not an estimate of real deployment frequency or throughput.",
        transform=axis.transAxes,
        ha="center",
        fontsize=9,
        color="#475569",
    )
    _save(
        figure,
        output / "planning-breadth-outcomes.png",
        output / "planning-breadth-outcomes.pdf",
    )
    plt.close(figure)


def _plot_win_robustness(plt: Any, campaign: Mapping[str, Any], output: Path) -> None:
    summary = _mapping(campaign["summary"], "campaign summary")
    tie_robust = int(summary["tie_robust_lookahead_wins"])
    tie_sensitive = int(summary["canonical_only_tie_sensitive_wins"])
    total_wins = tie_robust + tie_sensitive
    figure, axis = plt.subplots(figsize=(9.5, 5.4))
    figure.subplots_adjust(left=0.26, right=0.97, top=0.72, bottom=0.29)
    axis.barh(
        (0,),
        (tie_robust,),
        color="#1d4ed8",
        height=0.5,
        label="Survives best adaptive greedy tie-breaking",
    )
    axis.barh(
        (0,),
        (tie_sensitive,),
        left=(tie_robust,),
        color="#93c5fd",
        height=0.5,
        label="Depends on canonical greedy tie-breaking",
    )
    axis.text(
        tie_robust / 2,
        0,
        str(tie_robust),
        ha="center",
        va="center",
        color="white",
        fontsize=13,
        fontweight="bold",
    )
    axis.text(
        tie_robust + tie_sensitive / 2,
        0,
        str(tie_sensitive),
        ha="center",
        va="center",
        color="#172554",
        fontsize=13,
        fontweight="bold",
    )
    axis.set_yticks((0,), (f"{total_wins} total strict wins",))
    axis.set_xlim(0, max(25, total_wins + 1))
    axis.set_xlabel("")
    axis.grid(axis="x", color="#e2e8f0", alpha=0.8)
    handles, labels = axis.get_legend_handles_labels()
    figure.legend(
        handles,
        labels,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.035),
        framealpha=0.95,
    )
    figure.text(
        0.5,
        0.95,
        f"{tie_robust} strict wins are independent of greedy tie-breaking",
        ha="center",
        va="top",
        fontsize=15,
        fontweight="bold",
    )
    figure.text(
        0.5,
        0.84,
        f"The other {tie_sensitive} wins expose a deterministic "
        "tie-breaking weakness.\n"
        "All 1,000 scenarios: zero losses and zero cost regressions | Kauri "
        f"{str(campaign['kauri_revision'])[:8]}",
        ha="center",
        va="top",
        fontsize=9,
        color="#475569",
    )
    figure.text(
        0.615,
        0.225,
        "Frozen scenarios with one fewer terminal hypothesis",
        ha="center",
        fontsize=10,
    )
    _save(
        figure,
        output / "planning-win-robustness.png",
        output / "planning-win-robustness.pdf",
    )
    plt.close(figure)


def generate_figures(
    campaign_path: Path,
    output_directory: Path,
) -> tuple[Path, ...]:
    campaign = _load(campaign_path)
    output = output_directory.resolve()
    if output.exists():
        raise PlotError("output directory already exists")
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise PlotError("matplotlib is required to render campaign figures") from error

    output.mkdir(parents=True, exist_ok=False)
    try:
        _plot_policy_outcomes(plt, campaign, output)
        _plot_win_robustness(plt, campaign, output)
    except Exception:
        for path in output.iterdir():
            path.unlink()
        output.rmdir()
        raise
    return tuple(sorted(output.iterdir()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        outputs = generate_figures(arguments.campaign, arguments.output_dir)
    except (OSError, PlotError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
