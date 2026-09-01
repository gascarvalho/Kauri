#!/usr/bin/env python3
"""Render the validator-gated CERT13 N=31 campaign result figure."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.focused_crash_pair_validation import (  # noqa: E402
    validate_sealed_arm,
    validate_sealed_campaign,
)
from experiments.adaptive.kauri_experiment.profiled_fault_archive import (  # noqa: E402
    verify_evidence_seal,
)

PHASES = ("baseline", "fault", "epoch1", "late")
PHASE_LABELS = ("Baseline", "Fault", "Containment\n(E1)", "Optimized\n(E2)")
STEM = "cert13-n31-campaign-result"
CSV_FIELDS = ["pair_id"] + [
    f"{arm}_{phase}_tps"
    for arm in ("control", "adaptive")
    for phase in PHASES
] + ["adaptive_ratio", "paired_ratio", "effect_tps"]


class PlotError(RuntimeError):
    """The sealed campaign cannot safely produce the thesis figure."""


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PlotError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeError) as error:
        raise PlotError(f"{label} is invalid JSON") from error
    if not isinstance(value, Mapping):
        raise PlotError(f"{label} must be an object")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_campaign_gate(result: Mapping[str, Any]) -> None:
    if (
        result.get("verdict") != "PASS"
        or result.get("figure_eligible") is not True
        or result.get("terminal_slot_count") != 10
        or result.get("pair_count") != 5
        or result.get("automatic_retries") != 0
        or result.get("replacement_policy") != "none"
    ):
        raise PlotError("campaign is not validator-accepted and figure-eligible")
    support = result.get("scientific_support")
    if not isinstance(support, Mapping) or type(support.get("supported")) is not bool:
        raise PlotError("campaign lacks the frozen scientific-support decision")
    if result.get("claim_eligible") is not bool(support["supported"]):
        raise PlotError("campaign claim eligibility contradicts scientific support")


def _phase_medians(validation: Mapping[str, Any]) -> dict[str, float]:
    measurements = validation.get("scientific_measurements")
    if not isinstance(measurements, Mapping):
        raise PlotError("arm validation lacks scientific measurements")
    phases = measurements.get("phases")
    if not isinstance(phases, list) or len(phases) != len(PHASES):
        raise PlotError("arm validation lacks the four frozen phases")
    result: dict[str, float] = {}
    for row in phases:
        if not isinstance(row, Mapping):
            raise PlotError("phase measurement is not an object")
        phase = row.get("phase")
        value = row.get("median_milli_tps")
        if phase not in PHASES or type(value) is not int or value < 0:
            raise PlotError("phase median is missing or malformed")
        if phase in result:
            raise PlotError("phase measurement is duplicated")
        result[str(phase)] = value / 1000.0
    if tuple(result) != PHASES:
        raise PlotError("phase order differs from the frozen campaign contract")
    return result


def _collect_rows(
    campaign_root: Path,
    plan: Mapping[str, Any],
    provenance: Mapping[str, Any],
    verifier: Path,
) -> list[dict[str, Any]]:
    slots = plan.get("slots")
    children = provenance.get("children")
    if not isinstance(slots, list) or len(slots) != 10 or not isinstance(children, Mapping):
        raise PlotError("campaign plan or trusted child provenance drifted")
    rows: list[dict[str, Any]] = []
    for slot in slots:
        if not isinstance(slot, Mapping):
            raise PlotError("campaign slot is not an object")
        slot_id = slot.get("slot_id")
        entry = children.get(slot_id)
        if not isinstance(slot_id, str) or not isinstance(entry, Mapping):
            raise PlotError("campaign slot lacks trusted provenance")
        child_provenance = entry.get("provenance")
        if not isinstance(child_provenance, Mapping):
            raise PlotError("trusted child provenance is malformed")
        validation = validate_sealed_arm(
            campaign_root / "children" / slot_id,
            trusted_provenance=child_provenance,
            readiness_verifier_path=verifier,
        )
        if (
            validation.get("verdict") != "PASS"
            or validation.get("pair_id") != slot.get("pair_id")
            or validation.get("arm") != slot.get("arm")
        ):
            raise PlotError("arm validation does not match its frozen campaign slot")
        rows.append(
            {
                "slot_id": slot_id,
                "pair_id": str(slot["pair_id"]),
                "arm": str(slot["arm"]),
                "phases": _phase_medians(validation),
            }
        )
    return rows


def _pair_rows(
    arms: Sequence[Mapping[str, Any]], campaign_result: Mapping[str, Any]
) -> list[dict[str, Any]]:
    by_pair: dict[str, dict[str, Mapping[str, Any]]] = {}
    for arm in arms:
        by_pair.setdefault(str(arm["pair_id"]), {})[str(arm["arm"])] = arm
    validator_pairs = campaign_result.get("pairs")
    if not isinstance(validator_pairs, list) or len(validator_pairs) != 5:
        raise PlotError("campaign validator lacks five paired outcomes")
    outcome_by_pair = {
        str(row["pair_id"]): row
        for row in validator_pairs
        if isinstance(row, Mapping) and isinstance(row.get("pair_id"), str)
    }
    rows: list[dict[str, Any]] = []
    for ordinal in range(1, 6):
        pair_id = f"pair-{ordinal:02d}"
        arms_for_pair = by_pair.get(pair_id, {})
        outcome = outcome_by_pair.get(pair_id)
        if set(arms_for_pair) != {"control", "adaptive"} or not isinstance(
            outcome, Mapping
        ):
            raise PlotError("campaign pair join is incomplete")
        control = arms_for_pair["control"]["phases"]
        adaptive = arms_for_pair["adaptive"]["phases"]
        rows.append(
            {
                "pair_id": pair_id,
                **{
                    f"control_{phase}_tps": control[phase]
                    for phase in PHASES
                },
                **{
                    f"adaptive_{phase}_tps": adaptive[phase]
                    for phase in PHASES
                },
                "adaptive_ratio": int(outcome["adaptive_ratio_ppm"]) / 1_000_000,
                "paired_ratio": int(outcome["paired_ratio_ppm"]) / 1_000_000,
                "effect_tps": int(outcome["effect_milli_tps"]) / 1000.0,
            }
        )
    return rows


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=CSV_FIELDS, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def _read_accepted_csv(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise PlotError("accepted campaign CSV must be a regular non-symlink file")
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != CSV_FIELDS:
            raise PlotError("accepted campaign CSV columns drifted")
        rows = list(reader)
    if len(rows) != 5:
        raise PlotError("accepted campaign CSV must contain five matched pairs")
    throughput_fields = [
        f"{arm}_{phase}_tps"
        for arm in ("control", "adaptive")
        for phase in PHASES
    ]
    for ordinal, row in enumerate(rows, start=1):
        if row.get("pair_id") != f"pair-{ordinal:02d}":
            raise PlotError("accepted campaign CSV pair order drifted")
        try:
            numeric = {
                field: float(row[field])
                for field in CSV_FIELDS
                if field != "pair_id"
            }
        except (KeyError, TypeError, ValueError) as error:
            raise PlotError("accepted campaign CSV contains malformed values") from error
        if not all(math.isfinite(value) for value in numeric.values()):
            raise PlotError("accepted campaign CSV contains non-finite values")
        if any(numeric[field] < 0 for field in throughput_fields):
            raise PlotError("accepted campaign CSV contains negative throughput")
        if numeric["adaptive_ratio"] <= 0 or numeric["paired_ratio"] <= 0:
            raise PlotError("accepted campaign CSV contains a non-positive ratio")
    return rows


def _require_accepted_figure_source(
    csv_path: Path, source_manifest: Mapping[str, Any]
) -> None:
    artifacts = source_manifest.get("artifacts")
    csv_record = (
        artifacts.get(f"{STEM}.csv") if isinstance(artifacts, Mapping) else None
    )
    support = source_manifest.get("scientific_support")
    if (
        source_manifest.get("kind")
        != "kauri-cert13-n31-campaign-figure-manifest-v1"
        or source_manifest.get("validator_verdict") != "PASS"
        or source_manifest.get("figure_eligible") is not True
        or type(source_manifest.get("claim_eligible")) is not bool
        or not isinstance(support, Mapping)
        or type(support.get("supported")) is not bool
        or source_manifest.get("claim_eligible") is not bool(support["supported"])
        or not isinstance(csv_record, Mapping)
        or csv_record.get("sha256") != _sha256_file(csv_path)
    ):
        raise PlotError("CSV is not bound to an accepted campaign figure manifest")


def restyle_accepted_package(
    accepted_csv_path: Path,
    accepted_figure_manifest_path: Path,
    output_directory: Path,
) -> tuple[Path, ...]:
    """Restyle accepted values without reopening or weakening raw validation."""
    accepted_csv = accepted_csv_path.resolve(strict=True)
    source_manifest_path = accepted_figure_manifest_path.resolve(strict=True)
    source_manifest = _read_json(source_manifest_path, "accepted figure manifest")
    _require_accepted_figure_source(accepted_csv, source_manifest)
    rows = _read_accepted_csv(accepted_csv)

    output = output_directory.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(prefix="matplotlib-", dir=output) as cache:
            os.environ["MPLCONFIGDIR"] = cache
            _render(output / STEM, rows)
        shutil.copyfile(accepted_csv, output / f"{STEM}.csv")
        source_copy = output / "accepted-source-manifest.json"
        shutil.copyfile(source_manifest_path, source_copy)
        generator_revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        artifacts = {
            path.name: {
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for path in sorted(output.iterdir())
            if path.is_file()
        }
        manifest = {
            "schema_version": 2,
            "kind": "kauri-cert13-n31-campaign-figure-manifest-v2",
            "rendering_mode": "presentation-only restyle of validator-accepted CSV",
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "generator_revision": generator_revision,
            "source_figure_manifest_sha256": _sha256_file(source_manifest_path),
            "accepted_csv_sha256": _sha256_file(accepted_csv),
            "campaign_directory": source_manifest.get("campaign_directory"),
            "campaign_tree_sha256": source_manifest.get("campaign_tree_sha256"),
            "campaign_seal_sha256": source_manifest.get("campaign_seal_sha256"),
            "campaign_validation_sha256": source_manifest.get(
                "campaign_validation_sha256"
            ),
            "trusted_provenance_sha256": source_manifest.get(
                "trusted_provenance_sha256"
            ),
            "evidence_revision": source_manifest.get("evidence_revision"),
            "validator_verdict": source_manifest["validator_verdict"],
            "figure_eligible": source_manifest["figure_eligible"],
            "claim_eligible": source_manifest["claim_eligible"],
            "scientific_support": source_manifest["scientific_support"],
            "artifacts": artifacts,
            "caveats": list(source_manifest.get("caveats", ()))
            + [
                "This revision changes only visual encoding; accepted CSV values and the frozen scientific verdict are unchanged."
            ],
        }
        (output / "figure-manifest.json").write_bytes(_canonical(manifest))
        return tuple(sorted(output.iterdir()))
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


def _render(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    adaptive = [
        [float(row[f"adaptive_{phase}_tps"]) for phase in PHASES] for row in rows
    ]
    medians = [sorted(values)[2] for values in zip(*adaptive, strict=True)]
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 11,
            "axes.titleweight": "bold",
            "axes.labelsize": 9.5,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "legend.fontsize": 8.4,
            "axes.edgecolor": "#9AA7B2",
            "axes.linewidth": 0.8,
            "figure.dpi": 180,
        }
    )
    fig, axes = plt.subplots(
        1,
        2,
        figsize=(7.2, 3.65),
        constrained_layout=True,
        gridspec_kw={"width_ratios": (1.0, 1.16)},
    )
    left, right = axes
    x = list(range(4))
    left.axvspan(0.68, 1.32, color="#FCE8E1", alpha=0.9, linewidth=0)
    for ordinal, values in enumerate(adaptive, start=1):
        left.plot(
            x,
            values,
            color="#A8BBC7",
            linewidth=1.0,
            marker="o",
            markersize=3.0,
            markeredgewidth=0,
            alpha=0.68,
            zorder=2,
        )
    left.plot(
        x,
        medians,
        color="#173F5F",
        linewidth=2.6,
        marker="o",
        markersize=5.3,
        markerfacecolor="white",
        markeredgewidth=1.8,
        zorder=4,
    )
    phase_colors = ("#173F5F", "#D55E00", "#009E73", "#7B61A8")
    left.scatter(
        x,
        medians,
        s=34,
        c=phase_colors,
        edgecolors="white",
        linewidths=0.8,
        zorder=5,
    )
    for phase_x, value in zip(x, medians, strict=True):
        left.annotate(
            f"{value / 1000:.1f}k",
            (phase_x, value),
            xytext=(0, 9),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=8.4,
            fontweight="bold",
            color="#173F5F",
            bbox={
                "boxstyle": "round,pad=0.2",
                "facecolor": "white",
                "edgecolor": "none",
                "alpha": 0.9,
            },
            zorder=6,
        )
    left.set_xticks(x, PHASE_LABELS)
    left.set_ylabel("Commit-derived throughput (tx/s)")
    left.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value/1000:.0f}k"))
    left.set_xlim(-0.25, 3.25)
    left.set_ylim(0, max(max(values) for values in adaptive) * 1.16)
    left.grid(axis="y", color="#DCE3E8", linewidth=0.7)
    left.set_axisbelow(True)
    left.spines[["top", "right"]].set_visible(False)
    left.set_title("(a) Containment restores throughput", loc="left", pad=10)
    pair_x = list(range(1, 6))
    adaptive_ratio = [float(row["adaptive_ratio"]) for row in rows]
    paired_ratio = [float(row["paired_ratio"]) for row in rows]
    ratio_rows = list(range(5))
    median_adaptive = sorted(adaptive_ratio)[2]
    median_paired = sorted(paired_ratio)[2]
    right.axvspan(0.94, 1.0, color="#F4F6F8", linewidth=0)
    right.axvline(1.0, color="#7B8790", linewidth=0.9, linestyle=":")
    right.axvline(
        1.10,
        color="#b91c1c",
        linewidth=1.4,
        linestyle="--",
        zorder=1,
    )
    for row_y, adaptive_value, paired_value in zip(
        ratio_rows, adaptive_ratio, paired_ratio, strict=True
    ):
        right.plot(
            (adaptive_value, paired_value),
            (row_y, row_y),
            color="#C8D1D8",
            linewidth=1.5,
            zorder=2,
        )
    right.scatter(
        adaptive_ratio,
        ratio_rows,
        color="#0072B2",
        edgecolors="white",
        linewidths=0.7,
        s=42,
        marker="o",
        zorder=4,
        label="Optimized / containment",
    )
    right.scatter(
        paired_ratio,
        ratio_rows,
        color="#D55E00",
        edgecolors="white",
        linewidths=0.7,
        s=42,
        marker="D",
        zorder=4,
        label="Paired ratio of ratios",
    )
    median_y = 5.25
    right.axhspan(4.72, 5.78, color="#EEF2F5", linewidth=0, zorder=0)
    right.plot(
        (median_adaptive, median_paired),
        (median_y, median_y),
        color="#9AA7B2",
        linewidth=1.8,
        zorder=2,
    )
    right.scatter(
        [median_adaptive],
        [median_y],
        color="#0072B2",
        edgecolors="white",
        linewidths=0.8,
        s=58,
        marker="o",
        zorder=5,
    )
    right.scatter(
        [median_paired],
        [median_y],
        color="#D55E00",
        edgecolors="white",
        linewidths=0.8,
        s=58,
        marker="D",
        zorder=5,
    )
    right.annotate(
        f"{median_adaptive:.3f}",
        (median_adaptive, median_y),
        xytext=(0, 11),
        textcoords="offset points",
        ha="center",
        va="bottom",
        fontsize=7.8,
        color="#005C91",
        fontweight="bold",
    )
    right.annotate(
        f"{median_paired:.3f}",
        (median_paired, median_y),
        xytext=(0, 11),
        textcoords="offset points",
        ha="center",
        va="bottom",
        fontsize=7.8,
        color="#A84500",
        fontweight="bold",
    )
    right.text(
        1.098,
        -0.78,
        "frozen gate 1.10",
        ha="right",
        va="bottom",
        fontsize=8.0,
        color="#9F1D1D",
        fontweight="bold",
    )
    right.set_xlim(0.94, 1.112)
    right.set_ylim(-1.05, 5.85)
    right.set_xticks((0.95, 1.00, 1.05, 1.10))
    right.set_yticks(
        ratio_rows + [median_y],
        [f"P{value}" for value in pair_x] + ["Median"],
    )
    right.invert_yaxis()
    right.set_xlabel("Throughput ratio")
    right.grid(axis="x", color="#DCE3E8", linewidth=0.7)
    right.set_axisbelow(True)
    right.spines[["top", "right", "left"]].set_visible(False)
    right.tick_params(axis="y", length=0)
    right.legend(
        frameon=False,
        loc="upper left",
        bbox_to_anchor=(0.0, 0.995),
        borderaxespad=0.4,
        handletextpad=0.45,
        labelspacing=0.35,
        fontsize=7.7,
    )
    right.set_title("(b) All tested effects miss the 1.10 gate", loc="left", pad=10)

    fig.patch.set_facecolor("white")
    fig.savefig(
        path.with_suffix(".pdf"),
        bbox_inches="tight",
        metadata={
            "Creator": "Kauri CERT13 figure generator",
            "Producer": "Matplotlib",
            "CreationDate": None,
            "ModDate": None,
        },
    )
    fig.savefig(path.with_suffix(".png"), dpi=240, bbox_inches="tight")
    plt.close(fig)


def generate(
    campaign_root: Path,
    provenance_path: Path,
    verifier_path: Path,
    output_directory: Path,
) -> tuple[Path, ...]:
    campaign = campaign_root.resolve(strict=True)
    verifier = verifier_path.resolve(strict=True)
    provenance = _read_json(provenance_path.resolve(strict=True), "trusted provenance")
    campaign_result = validate_sealed_campaign(
        campaign,
        trusted_provenance=provenance,
        readiness_verifier_path=verifier,
    )
    _require_campaign_gate(campaign_result)
    plan = _read_json(campaign / "plan.json", "campaign plan")
    arms = _collect_rows(campaign, plan, provenance, verifier)
    pairs = _pair_rows(arms, campaign_result)
    seal = verify_evidence_seal(campaign)

    output = output_directory.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(prefix="matplotlib-", dir=output) as cache:
            os.environ["MPLCONFIGDIR"] = cache
            _render(output / STEM, pairs)
        _write_csv(output / f"{STEM}.csv", pairs)
        revisions = {
            str(entry["provenance"]["revision"])
            for entry in provenance["children"].values()
        }
        if len(revisions) != 1:
            raise PlotError("trusted children do not share one evidence revision")
        generator_revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        artifacts = {
            path.name: {"sha256": _sha256_file(path), "size_bytes": path.stat().st_size}
            for path in sorted(output.glob(f"{STEM}.*"))
        }
        manifest = {
            "schema_version": 1,
            "kind": "kauri-cert13-n31-campaign-figure-manifest-v1",
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "campaign_directory": str(campaign),
            "campaign_tree_sha256": seal.tree_sha256,
            "campaign_seal_sha256": seal.seal_sha256,
            "trusted_provenance_sha256": _sha256_file(provenance_path),
            "campaign_validation_sha256": _sha256_bytes(_canonical(campaign_result)),
            "evidence_revision": revisions.pop(),
            "generator_revision": generator_revision,
            "validator_verdict": campaign_result["verdict"],
            "figure_eligible": campaign_result["figure_eligible"],
            "claim_eligible": campaign_result["claim_eligible"],
            "scientific_support": campaign_result["scientific_support"],
            "artifacts": artifacts,
            "caveats": [
                "Five matched same-host pairs are descriptive evidence for the frozen profile, not a general consensus-safety proof.",
                "Figure eligibility establishes evidence integrity; it does not imply that the performance hypothesis is supported.",
            ],
        }
        (output / "figure-manifest.json").write_bytes(_canonical(manifest))
        return tuple(sorted(output.iterdir()))
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path)
    parser.add_argument("--trusted-provenance", type=Path)
    parser.add_argument("--readiness-verifier-path", type=Path)
    parser.add_argument("--accepted-csv", type=Path)
    parser.add_argument("--accepted-figure-manifest", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        raw_inputs = (
            arguments.campaign_root,
            arguments.trusted_provenance,
            arguments.readiness_verifier_path,
        )
        accepted_inputs = (
            arguments.accepted_csv,
            arguments.accepted_figure_manifest,
        )
        if all(value is not None for value in raw_inputs) and not any(
            value is not None for value in accepted_inputs
        ):
            outputs = generate(
                arguments.campaign_root,
                arguments.trusted_provenance,
                arguments.readiness_verifier_path,
                arguments.output_dir,
            )
        elif all(value is not None for value in accepted_inputs) and not any(
            value is not None for value in raw_inputs
        ):
            outputs = restyle_accepted_package(
                arguments.accepted_csv,
                arguments.accepted_figure_manifest,
                arguments.output_dir,
            )
        else:
            raise PlotError(
                "select exactly one complete source: raw campaign validation or accepted figure package"
            )
    except (OSError, PlotError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
