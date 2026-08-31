#!/usr/bin/env python3
"""Render the PASS-gated N=7 reputation-policy replay figure."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


ARM_ORDER = (
    "sigkill_crash",
    "static_authenticated_false_report",
    "static_persistent_omission",
)
ARM_LABELS = ("Crash", "False report", "Omission")
MECHANISMS = ("responsiveness", "latency-priority")
MECHANISM_LABELS = ("Responsiveness", "Latency priority")
COLORS = ("#2B6CB0", "#D97706")


class PlotError(ValueError):
    """Raised when a figure input is not bound to a PASS verdict."""


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise PlotError(f"{path} is not a JSON object")
    return value


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PlotError(f"{label} is not an object")
    return value


def render(artifact_path: Path, validation_path: Path, output_dir: Path) -> tuple[Path, Path]:
    artifact = _load(artifact_path)
    validation = _load(validation_path)
    if validation.get("verdict") != "PASS":
        raise PlotError("validation verdict is not PASS")
    if validation.get("artifact_sha256") != _hash(artifact_path):
        raise PlotError("validation does not bind the replay artifact")
    if artifact.get("verdict") != "PASS" or artifact.get("run_count") != 15:
        raise PlotError("replay artifact is not the frozen 15-run PASS")
    output_dir.mkdir(parents=True, exist_ok=True)
    png = output_dir / "n7-reputation-policy-replay.png"
    pdf = output_dir / "n7-reputation-policy-replay.pdf"
    if png.exists() or pdf.exists():
        raise PlotError("figure output already exists")

    runs = artifact.get("runs")
    if not isinstance(runs, list):
        raise PlotError("artifact runs are missing")
    inversions = [
        [
            int(_object(run.get("comparison"), "comparison")["kendall_inversion_count"])
            for run in runs
            if run.get("arm") == arm
        ]
        for arm in ARM_ORDER
    ]
    if any(len(values) != 5 for values in inversions):
        raise PlotError("figure does not have five runs per fault arm")
    summaries = _object(artifact.get("summaries"), "summaries")
    exclusions = np.array(
        [
            [
                int(
                    _object(
                        _object(
                            _object(summaries.get(arm), "arm summary").get(
                                "mechanisms"
                            ),
                            "mechanism summaries",
                        ).get(mechanism),
                        "mechanism summary",
                    )["actor_excluded_from_influential_roles_count"]
                )
                for arm in ARM_ORDER
            ]
            for mechanism in MECHANISMS
        ]
    )

    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.titlesize": 11,
            "axes.labelsize": 10,
            "figure.dpi": 160,
        }
    )
    figure, axes = plt.subplots(1, 2, figsize=(9.2, 3.5), constrained_layout=True)
    positions = np.arange(len(ARM_ORDER))
    jitter = np.linspace(-0.12, 0.12, 5)
    for index, values in enumerate(inversions):
        axes[0].scatter(
            positions[index] + jitter,
            values,
            color="#4A5568",
            s=28,
            alpha=0.85,
            zorder=3,
        )
        axes[0].hlines(
            float(np.median(values)),
            positions[index] - 0.22,
            positions[index] + 0.22,
            color="#C53030",
            linewidth=2.2,
            label="Median" if index == 0 else None,
            zorder=4,
        )
    axes[0].set_title("(a) Ordering divergence")
    axes[0].set_ylabel("Kendall inversion count")
    axes[0].set_xticks(positions, ARM_LABELS)
    axes[0].set_ylim(bottom=-0.25)
    axes[0].grid(axis="y", color="#CBD5E0", linewidth=0.7, alpha=0.7)
    axes[0].legend(frameon=False, loc="upper left")

    width = 0.34
    for mechanism_index, (label, color) in enumerate(
        zip(MECHANISM_LABELS, COLORS, strict=True)
    ):
        offsets = positions + (mechanism_index - 0.5) * width
        bars = axes[1].bar(
            offsets,
            exclusions[mechanism_index],
            width,
            color=color,
            label=label,
        )
        axes[1].bar_label(bars, padding=2, fontsize=9)
    axes[1].set_title("(b) Fault actor outside top-three roles")
    axes[1].set_ylabel("Runs out of five")
    axes[1].set_xticks(positions, ARM_LABELS)
    axes[1].set_ylim(0, 5.7)
    axes[1].set_yticks(range(0, 6))
    axes[1].grid(axis="y", color="#CBD5E0", linewidth=0.7, alpha=0.7)
    axes[1].legend(
        frameon=False,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.16),
        ncol=2,
    )

    figure.suptitle(
        "Offline policy replay over 15 accepted live N=7 fault runs",
        fontsize=12,
        fontweight="bold",
    )
    figure.savefig(png, bbox_inches="tight")
    figure.savefig(
        pdf,
        bbox_inches="tight",
        metadata={"CreationDate": None, "ModDate": None},
    )
    plt.close(figure)
    return png, pdf


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--validation", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        png, pdf = render(
            arguments.artifact.resolve(),
            arguments.validation.resolve(),
            arguments.output_dir.resolve(),
        )
    except (OSError, PlotError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(png)
    print(pdf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
