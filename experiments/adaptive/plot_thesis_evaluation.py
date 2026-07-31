#!/usr/bin/env python3
"""Render deterministic, PASS-gated thesis evaluation figures."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.thesis_evaluation import (  # noqa: E402
    ThesisEvaluationError,
    build_thesis_evaluation,
    canonical_thesis_evaluation_json,
    parse_thesis_json_object,
)


class PlotError(ValueError):
    """The evaluation artifact cannot safely produce thesis figures."""


ARM_LABELS = {
    "sigkill_crash": "Crash (SIGKILL)",
    "static_authenticated_false_report": "False report",
    "static_persistent_omission": "Persistent omission",
}


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlotError(f"{label} must be an object")
    return value


def _load(path: Path) -> Mapping[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise PlotError("evaluation artifact is absent") from error
    except OSError as error:
        raise PlotError(f"cannot read evaluation artifact: {error}") from error
    try:
        evaluation = parse_thesis_json_object(source, "evaluation artifact")
    except ThesisEvaluationError as error:
        raise PlotError(f"cannot parse evaluation artifact: {error}") from error
    model = _mapping(evaluation.get("model_evidence"), "model evidence")
    live = _mapping(evaluation.get("live_evidence"), "live evidence")
    arm_verdicts = live.get("arm_verdicts")
    if not isinstance(arm_verdicts, list):
        raise PlotError("live evidence must contain arm verdicts")
    try:
        rebuilt = build_thesis_evaluation(model, arm_verdicts)
    except ThesisEvaluationError as error:
        raise PlotError(f"evaluation evidence failed validation: {error}") from error
    if canonical_thesis_evaluation_json(evaluation) != (
        canonical_thesis_evaluation_json(rebuilt)
    ):
        raise PlotError(
            "evaluation artifact does not exactly match its rebuilt evidence"
        )
    return evaluation


def _metrics(evaluation: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    metrics = _mapping(evaluation.get("metrics"), "evaluation metrics")
    model = _mapping(metrics.get("model"), "model metrics")
    live = _mapping(metrics.get("live"), "live metrics")
    paths = _mapping(model.get("ambiguity_paths"), "ambiguity paths")
    if paths.get("greedy") != [9, 5, 4] or paths.get("lookahead") != [9, 6, 3]:
        raise PlotError("PASS artifact has an unexpected ambiguity witness")
    if model.get("terminal_ambiguity_reduction_ppm") != 250_000:
        raise PlotError("PASS artifact does not bind the 25% reduction")
    if set(live) != set(ARM_LABELS):
        raise PlotError("PASS artifact does not contain all live fault arms")
    return model, live


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
            "Subject": "Revision-bound experiment evidence",
            "Keywords": "Kauri, Byzantine faults, joint hypotheses",
            "Creator": "Kauri thesis evidence renderer",
            "Producer": "Kauri thesis evidence renderer",
            "CreationDate": fixed_date,
            "ModDate": fixed_date,
        },
    )


def _plot_ambiguity(plt: Any, model: Mapping[str, Any], output: Path) -> None:
    paths = _mapping(model["ambiguity_paths"], "ambiguity paths")
    figure, axis = plt.subplots(figsize=(8.2, 5.2), layout="constrained")
    epochs = (0, 1, 2)
    axis.plot(
        epochs,
        paths["greedy"],
        color="#d97706",
        marker="o",
        linewidth=2.6,
        markersize=7,
        label="Greedy one-epoch choice",
    )
    axis.plot(
        epochs,
        paths["lookahead"],
        color="#2563eb",
        marker="o",
        linewidth=2.6,
        markersize=7,
        label="Two-epoch joint planning",
    )
    for values, color in ((paths["greedy"], "#92400e"), (paths["lookahead"], "#1e40af")):
        for epoch, value in zip(epochs, values, strict=True):
            axis.annotate(
                str(value),
                (epoch, value),
                xytext=(0, 9),
                textcoords="offset points",
                ha="center",
                color=color,
                fontweight="bold",
            )
    axis.annotate(
        "25% fewer hypotheses\nafter the same 2 epochs",
        xy=(2, 3),
        xytext=(1.35, 6.6),
        arrowprops={"arrowstyle": "->", "color": "#1e40af", "linewidth": 1.4},
        color="#1e3a8a",
        fontsize=10,
        fontweight="bold",
    )
    axis.set_xticks(epochs, ("Initial belief", "After epoch 1", "After epoch 2"))
    axis.set_ylim(0, 10)
    axis.set_ylabel("Worst-case compatible fault hypotheses")
    figure.suptitle(
        "Two-epoch planning reduces final diagnostic ambiguity",
        fontsize=15,
        fontweight="bold",
    )
    axis.set_title(
        "Synthetic N=31 witness with coarsened existing-traffic outcomes; "
        "not live planner activation.",
        fontsize=8.5,
        color="#475569",
        pad=9,
    )
    axis.grid(axis="y", color="#cbd5e1", alpha=0.75)
    axis.legend(loc="lower left", framealpha=0.95)
    constraint = (
        f"Same budget: {model['reconfiguration_budget_epochs']} epochs | "
        f"extra messages: {model['additional_diagnostic_messages']} | "
        f"worst exposure: {model['selected_worst_case_exposure']} | "
        f"modeled latency: {model['selected_predicted_latency_us']} us"
    )
    axis.text(
        0.5,
        -0.16,
        constraint,
        transform=axis.transAxes,
        ha="center",
        fontsize=9,
        color="#334155",
    )
    _save(figure, output / "ambiguity-path.png", output / "ambiguity-path.pdf")
    plt.close(figure)


def _plot_live(
    plt: Any,
    live: Mapping[str, Any],
    revision: str,
    output: Path,
) -> None:
    figure, (diagnosis_axis, commit_axis) = plt.subplots(
        1,
        2,
        figsize=(11.5, 5.4),
        layout="constrained",
        gridspec_kw={"width_ratios": [1, 1.3]},
    )
    diagnosis_arms = (
        "static_authenticated_false_report",
        "static_persistent_omission",
    )
    endpoints: dict[str, tuple[str, str, str, str]] = {}
    for arm in diagnosis_arms:
        summary = _mapping(live[arm], arm)
        values = summary.get("compatible_hypotheses_by_observation")
        if values != [2, 1]:
            raise PlotError(f"{arm} does not contain the 2-to-1 settlement")
        outcomes = summary.get("observation_outcomes")
        hypothesis = _mapping(
            summary.get("settled_hypothesis"),
            f"{arm} settled hypothesis",
        )
        if arm == "static_authenticated_false_report":
            if outcomes != ["timeout", "response"]:
                raise PlotError("false-report outcome path is invalid")
            endpoints[arm] = (
                "#7c3aed",
                f"response  -> false reporter L={{{hypothesis['false_reporters'][0]}}}",
                "solid",
                "o",
            )
        else:
            if outcomes != ["timeout", "timeout"]:
                raise PlotError("persistent-omission outcome path is invalid")
            endpoints[arm] = (
                "#dc2626",
                f"timeout   -> persistent omitter C={{{hypothesis['persistent_omitters'][0]}}}",
                "dashed",
                "X",
            )
    diagnosis_axis.scatter(1, 2, color="#64748b", s=85, zorder=4)
    for color, _, line_style, marker in endpoints.values():
        diagnosis_axis.plot(
            (1, 2),
            (2, 1),
            linewidth=2.5,
            color=color,
            linestyle=line_style,
            alpha=0.9,
        )
        diagnosis_axis.scatter(
            2,
            1,
            color=color,
            marker=marker,
            s=85,
            zorder=4,
        )
    diagnosis_axis.set_xticks((1, 2), ("Initial timeout", "Passive cross-check"))
    diagnosis_axis.set_yticks((1, 2))
    diagnosis_axis.set_ylim(0.75, 2.25)
    diagnosis_axis.set_ylabel("Compatible hypotheses")
    diagnosis_axis.set_title("Existing traffic resolves the same initial syndrome")
    diagnosis_axis.grid(axis="y", color="#cbd5e1", alpha=0.75)
    diagnosis_axis.text(
        1.45,
        1.36,
        "cross-check " + endpoints[diagnosis_arms[0]][1] + "\n"
        "cross-check " + endpoints[diagnosis_arms[1]][1],
        fontsize=8.5,
        va="center",
        color="#334155",
        bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.82},
    )

    y_positions = list(range(len(ARM_LABELS)))
    for y, (arm, label) in zip(y_positions, ARM_LABELS.items(), strict=True):
        summary = _mapping(live[arm], arm)
        heights = summary.get("common_commit_heights")
        if (
            not isinstance(heights, list)
            or len(heights) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in heights)
            or heights[1] <= heights[0]
        ):
            raise PlotError(f"{arm} has invalid common-commit heights")
        commit_axis.plot((0, 1), (y, y), color="#94a3b8", linewidth=2.2)
        commit_axis.scatter(0, y, color="#64748b", s=55, zorder=3)
        commit_axis.scatter(1, y, color="#16a34a", s=70, zorder=3)
        commit_axis.annotate(
            f"h={heights[0]}",
            (0, y),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            fontsize=8,
        )
        commit_axis.annotate(
            f"h={heights[1]}",
            (1, y),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            fontsize=8,
            color="#166534",
        )
    commit_axis.set_yticks(y_positions, tuple(ARM_LABELS.values()))
    commit_axis.set_xticks((0, 1), ("Common commit before", "Common commit after"))
    commit_axis.set_xlim(-0.12, 1.12)
    commit_axis.set_title("Every bounded live arm commits after fault evidence")
    commit_axis.grid(axis="x", color="#cbd5e1", alpha=0.75)
    commit_axis.invert_yaxis()
    figure.suptitle(
        f"Revision-bound N=7 fault evidence ({revision[:8]}; fixed f=2, Q=5)",
        fontweight="bold",
    )
    figure.supxlabel(
        "Single-run commit-height spans are continuity witnesses, not throughput comparisons or safety proofs.",
        fontsize=8.5,
        color="#475569",
    )
    _save(
        figure,
        output / "live-fault-outcomes.png",
        output / "live-fault-outcomes.pdf",
    )
    plt.close(figure)


def generate_figures(evaluation_path: Path, output_directory: Path) -> tuple[Path, ...]:
    evaluation = _load(evaluation_path)
    model, live = _metrics(evaluation)
    output = output_directory.resolve()
    if output.exists():
        raise PlotError("output directory already exists")

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise PlotError("matplotlib is required to render thesis figures") from error

    output.mkdir(parents=True, exist_ok=False)
    try:
        _plot_ambiguity(plt, model, output)
        _plot_live(plt, live, str(evaluation["kauri_revision"]), output)
    except Exception:
        for path in output.iterdir():
            path.unlink()
        output.rmdir()
        raise
    return tuple(sorted(output.iterdir()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        outputs = generate_figures(arguments.evaluation, arguments.output_dir)
    except (OSError, PlotError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
