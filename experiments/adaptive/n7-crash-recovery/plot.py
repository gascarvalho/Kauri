#!/usr/bin/env python3
"""Render the PASS-gated N=7 crash-recovery throughput/reputation figure."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


class PlotError(ValueError):
    """The validated artifacts cannot safely produce a figure."""


PLOT_CLAIM = ".plot.claim"


def _load_verdict(directory: Path) -> Mapping[str, Any]:
    path = directory / "validation.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlotError("validation.json is absent; validate the run first") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PlotError(f"cannot read validation.json: {exc}") from exc
    if not isinstance(value, dict):
        raise PlotError("validation.json must contain an object")
    verdict = value.get("verdict")
    if verdict != "PASS":
        if verdict in ("FAIL", "INCOMPLETE"):
            raise PlotError(f"refusing to plot terminal {verdict} run")
        raise PlotError("validation.json has no canonical PASS verdict")
    if value.get("scenario") != "n7-crash-recovery":
        raise PlotError("PASS verdict is for a different scenario")
    if value.get("run_complete") is not True:
        raise PlotError("PASS verdict does not bind a complete run")
    if not isinstance(value.get("kauri_revision"), str):
        raise PlotError("PASS verdict does not bind a Kauri revision")
    return value


def _artifact_path(
    directory: Path, verdict: Mapping[str, Any], key: str, expected: str
) -> Path:
    artifacts = verdict.get("artifacts")
    if not isinstance(artifacts, dict):
        raise PlotError(f"PASS verdict does not bind canonical {expected}")
    descriptor = artifacts.get(key)
    if not isinstance(descriptor, dict) or set(descriptor) != {"path", "sha256"}:
        raise PlotError(f"PASS verdict does not hash-bind canonical {expected}")
    if descriptor.get("path") != expected:
        raise PlotError(f"PASS verdict does not bind canonical {expected}")
    digest = descriptor.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise PlotError(f"PASS verdict has invalid hash for {expected}")
    path = (directory / expected).resolve()
    try:
        path.relative_to(directory.resolve())
    except ValueError as exc:
        raise PlotError("artifact path escapes validated directory") from exc
    if not path.is_file():
        raise PlotError(f"PASS artifact is absent: {expected}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise PlotError(f"PASS artifact hash mismatch: {expected}")
    return path


def _float(row: Mapping[str, str], field: str) -> float:
    try:
        value = float(row[field])
    except (KeyError, ValueError) as exc:
        raise PlotError(f"invalid numeric throughput field: {field}") from exc
    if value < 0 or value != value or value in (float("inf"), float("-inf")):
        raise PlotError(f"non-canonical numeric throughput field: {field}")
    return value


def _integer(row: Mapping[str, str], field: str) -> int:
    try:
        value = int(row[field])
    except (KeyError, ValueError) as exc:
        raise PlotError(f"invalid integer field: {field}") from exc
    return value


def _load_throughput(path: Path) -> list[dict[str, Any]]:
    required = {
        "bucket_index",
        "phase",
        "bucket_start_ns",
        "bucket_end_ns",
        "elapsed_seconds",
        "commit_count",
        "transaction_count",
        "throughput_tps",
    }
    for replica in range(7):
        required.add(f"leader_{replica}_transactions")
        required.add(f"leader_{replica}_tps")
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or set(reader.fieldnames) != required:
            raise PlotError("throughput.csv has a non-canonical header")
        rows: list[dict[str, Any]] = []
        for index, raw in enumerate(reader):
            if _integer(raw, "bucket_index") != index:
                raise PlotError("throughput bucket indices are not contiguous")
            phase = raw["phase"]
            if phase not in ("baseline", "degraded", "post"):
                raise PlotError("throughput row has an unknown phase")
            start = _integer(raw, "bucket_start_ns")
            end = _integer(raw, "bucket_end_ns")
            elapsed = _float(raw, "elapsed_seconds")
            if not 0 < end - start <= 5_000_000_000:
                raise PlotError("throughput bucket is not in (0, 5s]")
            aggregate = _float(raw, "throughput_tps")
            leaders = [_float(raw, f"leader_{replica}_tps") for replica in range(7)]
            transactions = _integer(raw, "transaction_count")
            leader_transactions = [
                _integer(raw, f"leader_{replica}_transactions")
                for replica in range(7)
            ]
            if abs(sum(leaders) - aggregate) > 1e-9:
                raise PlotError("leader TPS does not conserve aggregate throughput")
            if sum(leader_transactions) != transactions:
                raise PlotError("leader transactions do not conserve aggregate")
            rows.append(
                {
                    "phase": phase,
                    "start_ns": start,
                    "end_ns": end,
                    "elapsed_seconds": elapsed,
                    "aggregate": aggregate,
                    "leaders": leaders,
                }
            )
    if not rows:
        raise PlotError("throughput.csv contains no buckets")
    if any(
        rows[index]["start_ns"] != rows[index - 1]["end_ns"]
        for index in range(1, len(rows))
    ):
        raise PlotError("throughput buckets are not contiguous")
    return rows


def _load_reputation(path: Path) -> dict[int, list[tuple[int, int]]]:
    expected = {
        "timestamp_ns",
        "elapsed_seconds",
        "phase",
        "replica_id",
        "score",
        "source_sequence",
        "evidence_outcome",
        "delta",
    }
    trajectories = {replica: [] for replica in range(7)}
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or set(reader.fieldnames) != expected:
            raise PlotError("reputation.csv has a non-canonical header")
        for row in reader:
            timestamp = _integer(row, "timestamp_ns")
            replica = _integer(row, "replica_id")
            score = _integer(row, "score")
            if replica not in trajectories:
                raise PlotError("reputation trajectory contains a non-member")
            if trajectories[replica] and timestamp < trajectories[replica][-1][0]:
                raise PlotError("reputation trajectory time regressed")
            trajectories[replica].append((timestamp, score))
    if any(not points for points in trajectories.values()):
        raise PlotError("reputation.csv must contain all seven trajectories")
    return trajectories


def _boundaries(verdict: Mapping[str, Any]) -> dict[str, int]:
    value = verdict.get("boundaries")
    if not isinstance(value, dict):
        raise PlotError("PASS verdict has no boundaries")
    names = (
        "baseline_start_ns",
        "crash_ns",
        "command_ns",
        "activation_ns",
        "post_start_ns",
        "end_ns",
    )
    result: dict[str, int] = {}
    for name in names:
        item = value.get(name)
        if type(item) is not int or item < 0:
            raise PlotError(f"invalid PASS boundary: {name}")
        result[name] = item
    if not (
        result["baseline_start_ns"]
        < result["crash_ns"]
        < result["command_ns"]
        <= result["activation_ns"]
        < result["post_start_ns"]
        < result["end_ns"]
    ):
        raise PlotError("PASS boundaries are not ordered")
    return result


def _crash_markers(verdict: Mapping[str, Any], crash_ns: int) -> tuple[int, int]:
    markers = verdict.get("markers")
    if not isinstance(markers, dict):
        raise PlotError("PASS verdict has no crash markers")
    replicas = markers.get("crash_replicas")
    values = markers.get("crash_marker_ns")
    if replicas != [0, 1] or not isinstance(values, list) or len(values) != 2:
        raise PlotError("PASS verdict does not bind crashes for replicas 0 and 1")
    if any(type(value) is not int or value <= 0 for value in values):
        raise PlotError("PASS verdict has invalid crash marker time")
    if min(values) != crash_ns:
        raise PlotError("crash phase boundary does not match crash markers")
    return (values[0], values[1])


def _seconds(timestamp_ns: int, baseline_ns: int) -> float:
    return (timestamp_ns - baseline_ns) / 1_000_000_000


def _publish_exclusive(temporary: Path, destination: Path) -> None:
    try:
        with destination.open("xb") as output:
            output.write(temporary.read_bytes())
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError as exc:
        raise PlotError(
            f"refusing to overwrite figure output: {destination.name}"
        ) from exc


def _generate_claimed_figure(directory: Path) -> tuple[Path, Path]:
    """Write PNG and PDF only when the directory contains a canonical PASS."""
    directory = directory.resolve()
    verdict = _load_verdict(directory)
    for key, expected in (
        ("manifest", "manifest.json"),
        ("profile", "profile.json"),
        ("epochs", "epochs.json"),
    ):
        _artifact_path(directory, verdict, key, expected)
    throughput_path = _artifact_path(
        directory, verdict, "throughput", "throughput.csv"
    )
    reputation_path = _artifact_path(
        directory, verdict, "reputation", "reputation.csv"
    )
    rows = _load_throughput(throughput_path)
    trajectories = _load_reputation(reputation_path)
    boundaries = _boundaries(verdict)
    crash_markers = _crash_markers(verdict, boundaries["crash_ns"])
    metrics = verdict.get("metrics")
    if not isinstance(metrics, dict):
        raise PlotError("PASS verdict has no metrics")
    medians = {
        "baseline": metrics.get("baseline_median_tps"),
        "degraded": metrics.get("degraded_median_tps"),
        "post": metrics.get("post_median_tps"),
    }
    if any(type(value) not in (int, float) for value in medians.values()):
        raise PlotError("PASS verdict has invalid throughput medians")

    png_path = directory / "figure.png"
    pdf_path = directory / "figure.pdf"
    if png_path.exists() or pdf_path.exists():
        raise PlotError("figure output already exists; preserve it or use a new run")

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise PlotError(
            "matplotlib is required only for plotting; install it before rendering"
        ) from exc

    baseline = boundaries["baseline_start_ns"]
    phase_ranges = {
        "baseline": (
            _seconds(boundaries["baseline_start_ns"], baseline),
            _seconds(boundaries["crash_ns"], baseline),
        ),
        "degraded": (
            _seconds(boundaries["crash_ns"], baseline),
            _seconds(boundaries["post_start_ns"], baseline),
        ),
        "post": (
            _seconds(boundaries["post_start_ns"], baseline),
            _seconds(boundaries["end_ns"], baseline),
        ),
    }
    phase_colors = {
        "baseline": "#dbeafe",
        "degraded": "#fee2e2",
        "post": "#dcfce7",
    }
    phase_labels = {
        "baseline": "Normal / Epoch 0",
        "degraded": "Replicas 0 & 1 crashed / Epoch 0",
        "post": "Epoch 1 / crashed replicas are leaves",
    }
    figure, (throughput_axis, reputation_axis) = plt.subplots(
        2,
        1,
        figsize=(13, 8.5),
        sharex=True,
        gridspec_kw={"height_ratios": [2.25, 1]},
        constrained_layout=True,
    )
    for axis in (throughput_axis, reputation_axis):
        for phase, (start, end) in phase_ranges.items():
            axis.axvspan(start, end, color=phase_colors[phase], alpha=0.55, zorder=0)
        axis.axvspan(
            _seconds(boundaries["activation_ns"], baseline),
            _seconds(boundaries["post_start_ns"], baseline),
            color="#e5e7eb",
            alpha=0.8,
            zorder=0,
        )
        axis.grid(axis="y", color="#cbd5e1", alpha=0.6, linewidth=0.7)
    for phase, (start, end) in phase_ranges.items():
        throughput_axis.text(
            (start + end) / 2,
            0.98,
            phase_labels[phase],
            transform=throughput_axis.get_xaxis_transform(),
            ha="center",
            va="top",
            fontsize=9,
            fontweight="bold",
            color="#334155",
        )

    x_values = [
        _seconds((row["start_ns"] + row["end_ns"]) // 2, baseline)
        for row in rows
    ]
    throughput_axis.plot(
        x_values,
        [row["aggregate"] for row in rows],
        color="#111827",
        linewidth=2.5,
        marker="o",
        markersize=3.2,
        label="Aggregate",
        zorder=5,
    )
    leader_colors = (
        "#dc2626",
        "#ea580c",
        "#2563eb",
        "#7c3aed",
        "#0891b2",
        "#16a34a",
        "#ca8a04",
    )
    for replica, color in enumerate(leader_colors):
        throughput_axis.plot(
            x_values,
            [row["leaders"][replica] for row in rows],
            color=color,
            linewidth=1.15,
            alpha=0.9,
            label=f"Leader {replica}",
            zorder=3,
        )

    for phase, (start, end) in phase_ranges.items():
        median = float(medians[phase])
        throughput_axis.hlines(
            median,
            start,
            end,
            colors="#475569",
            linestyles="--",
            linewidth=1.5,
            zorder=4,
        )
        throughput_axis.text(
            (start + end) / 2,
            median,
            f" {phase} median {median:.2f}",
            fontsize=8,
            color="#334155",
            ha="center",
            va="bottom",
        )

    for replica, timestamp_ns in enumerate(crash_markers):
        x_value = _seconds(timestamp_ns, baseline)
        for axis in (throughput_axis, reputation_axis):
            axis.axvline(
                x_value,
                color="#b91c1c",
                linestyle="--",
                linewidth=1.6,
                zorder=6,
            )
        throughput_axis.text(
            x_value,
            1.01 if replica == 0 else 0.93,
            f"Crash replica {replica}",
            transform=throughput_axis.get_xaxis_transform(),
            rotation=90,
            ha="left",
            va="bottom",
            fontsize=8,
            color="#b91c1c",
        )

    marker_specs = (
        ("command_ns", "Epoch command committed", "#c2410c", "--"),
        ("activation_ns", "Epoch 1 activated", "#6d28d9", ":"),
    )
    for name, label, color, style in marker_specs:
        x_value = _seconds(boundaries[name], baseline)
        for axis in (throughput_axis, reputation_axis):
            axis.axvline(
                x_value,
                color=color,
                linestyle=style,
                linewidth=1.6,
                zorder=6,
            )
        throughput_axis.text(
            x_value,
            1.01,
            label,
            transform=throughput_axis.get_xaxis_transform(),
            rotation=90,
            ha="left",
            va="bottom",
            fontsize=8,
            color=color,
        )

    end_ns = boundaries["end_ns"]
    for replica, color in enumerate(leader_colors):
        points = trajectories[replica]
        times = [_seconds(timestamp, baseline) for timestamp, _ in points]
        scores = [score for _, score in points]
        if points[-1][0] < end_ns:
            times.append(_seconds(end_ns, baseline))
            scores.append(scores[-1])
        emphasized = replica in (0, 1)
        reputation_axis.step(
            times,
            scores,
            where="post",
            color=color,
            linewidth=2.5 if emphasized else 1.35,
            alpha=1.0 if emphasized else 0.8,
            label=f"Replica {replica}" + (" (crashed)" if emphasized else ""),
            zorder=5 if emphasized else 3,
        )

    run_id = str(verdict.get("run_id", "unknown"))
    revision = str(verdict["kauri_revision"])
    recovery_ratio = float(metrics.get("recovery_ratio", 0.0))
    throughput_axis.set_title(
        "N=7, f=2 crash recovery: raw commit throughput by scheduled leader\n"
        f"run={run_id}  revision={revision[:12]}  post/baseline={recovery_ratio:.2f}x"
    )
    throughput_axis.set_ylabel("Committed transactions / second")
    reputation_axis.set_ylabel("Reputation score")
    reputation_axis.set_xlabel("Seconds from baseline start")
    throughput_axis.legend(
        loc="upper left", ncol=4, fontsize=8, framealpha=0.92
    )
    reputation_axis.legend(
        loc="upper left", ncol=4, fontsize=8, framealpha=0.92
    )
    reputation_axis.set_xlim(
        phase_ranges["baseline"][0], phase_ranges["post"][1]
    )

    png_temporary = png_path.with_name(f".{png_path.name}.{os.getpid()}.tmp")
    pdf_temporary = pdf_path.with_name(f".{pdf_path.name}.{os.getpid()}.tmp")
    published: list[Path] = []
    try:
        figure.savefig(png_temporary, format="png", dpi=180)
        figure.savefig(pdf_temporary, format="pdf")
        _publish_exclusive(png_temporary, png_path)
        published.append(png_path)
        _publish_exclusive(pdf_temporary, pdf_path)
        published.append(pdf_path)
    except (OSError, PlotError) as exc:
        for output in published:
            try:
                output.unlink()
            except FileNotFoundError:
                pass
        if isinstance(exc, PlotError):
            raise
        raise PlotError(f"could not write figure outputs: {exc}") from exc
    finally:
        for temporary in (png_temporary, pdf_temporary):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        plt.close(figure)
    return png_path, pdf_path


def generate_figure(directory: Path) -> tuple[Path, Path]:
    """Atomically claim a PASS directory, then write non-overwriting figures."""
    directory = directory.resolve()
    claim_path = directory / PLOT_CLAIM
    try:
        with claim_path.open("x", encoding="utf-8") as claim:
            claim.write(f"pid={os.getpid()}\n")
            claim.flush()
            os.fsync(claim.fileno())
    except FileNotFoundError as exc:
        raise PlotError("validated run directory does not exist") from exc
    except FileExistsError as exc:
        raise PlotError(
            "figure output is already claimed; preserve it or use a new run"
        ) from exc
    try:
        return _generate_claimed_figure(directory)
    finally:
        try:
            claim_path.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "validated_run",
        type=Path,
        help="directory containing an immutable PASS validation.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        png, pdf = generate_figure(arguments.validated_run)
    except PlotError as exc:
        print(f"plot error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {png} and {pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
