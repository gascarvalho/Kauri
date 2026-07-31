#!/usr/bin/env python3
"""Render a PASS-gated descriptive figure for the N=7 repetitions."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.comparison import (  # noqa: E402
    build_n7_comparison,
)
from experiments.adaptive.kauri_experiment.n7_fault_repetition_campaign import (  # noqa: E402
    N7FaultRepetitionCampaignError,
    validate_n7_fault_repetition_campaign,
)
from experiments.adaptive.kauri_experiment.thesis_evaluation import (  # noqa: E402
    ARM_NAMES,
    parse_thesis_json_object,
)


ARM_LABELS = {
    "sigkill_crash": "Crash\n(SIGKILL)",
    "static_authenticated_false_report": "False\nreport",
    "static_persistent_omission": "Persistent\nomission",
}


class PlotError(ValueError):
    """The supplied campaign cannot safely produce a figure."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlotError(f"{label} must be an object")
    return value


def _load(path: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise PlotError("campaign artifact is absent") from error
    except OSError as error:
        raise PlotError(f"cannot read campaign artifact: {error}") from error
    try:
        campaign = parse_thesis_json_object(source, "N=7 repetition campaign")
        revision = campaign.get("kauri_revision")
        seed = campaign.get("seed")
        comparison = build_n7_comparison(
            kauri_revision=revision,
            seed=seed,
            crash_replica_id=1,
            false_reporter_id=6,
            false_report_target_id=1,
            persistent_omitter_id=1,
            diagnostic_window="n7-epoch0-tree6-tree0-static-v1",
        )
        validated = validate_n7_fault_repetition_campaign(
            comparison,
            campaign,
            evidence_root=path.resolve().parent,
        )
    except (N7FaultRepetitionCampaignError, ValueError) as error:
        raise PlotError(f"campaign failed strict validation: {error}") from error
    if validated.get("verdict") != "PASS":
        raise PlotError("only a PASS campaign may produce a thesis figure")
    return validated


def _timing_points(
    campaign: Mapping[str, Any],
) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    post_fault = {name: [] for name in ARM_NAMES}
    settlement = {
        "static_authenticated_false_report": [],
        "static_persistent_omission": [],
    }
    repetitions = campaign.get("repetitions")
    if not isinstance(repetitions, list):
        raise PlotError("campaign repetitions are absent")
    for repetition_value in repetitions:
        repetition = _mapping(repetition_value, "campaign repetition")
        attempts = repetition.get("attempts")
        if not isinstance(attempts, list):
            raise PlotError("campaign attempts are absent")
        for attempt_value in attempts:
            attempt = _mapping(attempt_value, "campaign attempt")
            arm = attempt.get("arm")
            if arm not in ARM_NAMES:
                raise PlotError("campaign attempt has an unknown arm")
            action = _mapping(attempt.get("action_observation"), "fault action")
            after = _mapping(attempt.get("common_commit_after"), "common commit")
            after_ns = int(after["common_monotonic_raw_ns"])
            if arm == "sigkill_crash":
                action_ns = int(action["confirmed_monotonic_raw_ns"])
            else:
                action_ns = int(
                    action["manager_acceptance_observed_monotonic_raw_ns"]
                )
                initial = _mapping(
                    attempt.get("manager_accepted_timeout_observation"),
                    "initial manager observation",
                )
                followup = _mapping(
                    attempt.get("followup_manager_observation"),
                    "followup manager observation",
                )
                settlement[str(arm)].append(
                    (
                        int(followup["source_monotonic_ns"])
                        - int(initial["source_monotonic_ns"])
                    )
                    // 1_000_000
                )
            post_fault[str(arm)].append(
                (after_ns - action_ns) // 1_000_000
            )
    if any(len(post_fault[name]) != 5 for name in ARM_NAMES) or any(
        len(values) != 5 for values in settlement.values()
    ):
        raise PlotError("PASS campaign does not contain five timing points per arm")
    return post_fault, settlement


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
            "Subject": "Bounded repeated N=7 fault evidence",
            "Keywords": "Kauri, crash fault, Byzantine fault, repetition",
            "Creator": "Kauri thesis evidence renderer",
            "Producer": "Kauri thesis evidence renderer",
            "CreationDate": fixed_date,
            "ModDate": fixed_date,
        },
    )


def generate_figure(campaign_path: Path, output_directory: Path) -> tuple[Path, ...]:
    campaign = _load(campaign_path)
    post_fault, settlement = _timing_points(campaign)
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
        figure, axes = plt.subplots(
            1,
            3,
            figsize=(13.2, 4.9),
            layout="constrained",
            gridspec_kw={"width_ratios": [0.8, 1.45, 1.05]},
        )
        colors = ("#2563eb", "#7c3aed", "#dc2626")
        pass_counts = _mapping(campaign.get("pass_counts"), "pass counts")
        counts = [int(pass_counts[name]) for name in ARM_NAMES]
        axes[0].bar(range(3), counts, color=colors, width=0.68)
        axes[0].set_xticks(range(3), [ARM_LABELS[name] for name in ARM_NAMES])
        axes[0].set_ylim(0, 5.8)
        axes[0].set_yticks(range(0, 6))
        axes[0].set_ylabel("Validated attempts")
        axes[0].set_title("All scheduled slots pass")
        for index, value in enumerate(counts):
            axes[0].text(index, value + 0.12, f"{value}/5", ha="center")

        repetitions = range(1, 6)
        for index, arm in enumerate(ARM_NAMES):
            values = post_fault[arm]
            axes[1].plot(
                repetitions,
                values,
                marker="o",
                linewidth=1.8,
                color=colors[index],
                label=ARM_LABELS[arm].replace("\n", " "),
            )
        axes[1].set_xticks(tuple(repetitions))
        axes[1].set_xlabel("Scheduled repetition")
        axes[1].set_ylabel("Delay to next common commit (ms)")
        axes[1].set_title("Post-fault commit continuity")
        axes[1].legend(fontsize=8)

        byzantine_arms = tuple(settlement)
        for index, arm in enumerate(byzantine_arms, start=1):
            values = settlement[arm]
            axes[2].scatter(
                [index] * len(values),
                values,
                s=45,
                color=colors[index],
                alpha=0.85,
            )
            median = sorted(values)[len(values) // 2]
            axes[2].hlines(
                median,
                index - 0.22,
                index + 0.22,
                color="#111827",
                linewidth=2,
            )
        axes[2].set_xticks(
            (1, 2),
            ("False report", "Persistent\nomission"),
        )
        axes[2].set_ylabel("Diagnostic settlement delay (ms)")
        axes[2].set_title("Passive cross-check settles 5/5")

        for axis in axes:
            axis.grid(axis="y", color="#cbd5e1", alpha=0.7)
        figure.suptitle(
            "Repeated bounded N=7 crash and Byzantine fault evidence "
            f"({str(campaign['kauri_revision'])[:8]})",
            fontsize=14,
            fontweight="bold",
        )
        figure.supxlabel(
            "Same-host CLOCK_MONOTONIC_RAW timing is descriptive continuity "
            "evidence—not throughput, statistical inference, or a safety proof.",
            fontsize=8.5,
            color="#475569",
        )
        png = output / "n7-fault-repetitions.png"
        pdf = output / "n7-fault-repetitions.pdf"
        _save(figure, png, pdf)
        plt.close(figure)
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
        outputs = generate_figure(arguments.campaign, arguments.output_dir)
    except (OSError, PlotError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
