#!/usr/bin/env python3
"""Render the validator-gated CERT13 N=31 campaign result figure."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
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
    fields = ["pair_id"] + [
        f"{arm}_{phase}_tps"
        for arm in ("control", "adaptive")
        for phase in PHASES
    ] + ["adaptive_ratio", "paired_ratio", "effect_tps"]
    with path.open("x", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _render(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    adaptive = [
        [float(row[f"adaptive_{phase}_tps"]) for phase in PHASES] for row in rows
    ]
    medians = [sorted(values)[2] for values in zip(*adaptive, strict=True)]
    fig, axes = plt.subplots(1, 2, figsize=(7.15, 3.35), constrained_layout=True)
    left, right = axes
    x = list(range(4))
    for ordinal, values in enumerate(adaptive, start=1):
        left.plot(
            x,
            values,
            color="#7aa6c2",
            linewidth=0.9,
            marker="o",
            markersize=2.8,
            alpha=0.72,
            label="Individual pairs" if ordinal == 1 else None,
        )
    left.plot(
        x,
        medians,
        color="#0b3c5d",
        linewidth=2.2,
        marker="o",
        markersize=4.2,
        label="Median",
    )
    left.set_xticks(x, PHASE_LABELS)
    left.set_ylabel("Commit-derived throughput (tx/s)")
    left.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value/1000:.0f}k"))
    left.grid(axis="y", color="#d9d9d9", linewidth=0.55)
    left.legend(frameon=False, fontsize=7, loc="upper right")
    left.set_title("(a) Adaptive-arm phase medians", loc="left", fontsize=8.5)

    pair_x = list(range(1, 6))
    adaptive_ratio = [float(row["adaptive_ratio"]) for row in rows]
    paired_ratio = [float(row["paired_ratio"]) for row in rows]
    right.axhline(1.0, color="#6b7280", linewidth=0.8, linestyle=":")
    right.axhline(
        1.10,
        color="#b91c1c",
        linewidth=1.2,
        linestyle="--",
        label="Frozen 1.10 threshold",
    )
    right.plot(
        pair_x,
        adaptive_ratio,
        color="#2563eb",
        marker="o",
        linewidth=1.3,
        label="Optimized / adaptive baseline",
    )
    right.plot(
        pair_x,
        paired_ratio,
        color="#0f766e",
        marker="s",
        linewidth=1.3,
        label="Paired ratio of ratios",
    )
    right.set_xticks(pair_x, [f"P{value}" for value in pair_x])
    right.set_ylim(0.90, 1.13)
    right.set_ylabel("Throughput ratio")
    right.grid(axis="y", color="#d9d9d9", linewidth=0.55)
    right.legend(frameon=False, fontsize=6.7, loc="upper right")
    right.set_title("(b) Frozen performance tests", loc="left", fontsize=8.5)

    support = "NOT SUPPORTED"
    fig.suptitle(
        f"CERT13 N=31 campaign: integrity PASS; performance claim {support}",
        fontsize=9,
        fontweight="semibold",
    )
    fig.savefig(path.with_suffix(".pdf"), bbox_inches="tight")
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
    parser.add_argument("--campaign-root", required=True, type=Path)
    parser.add_argument("--trusted-provenance", required=True, type=Path)
    parser.add_argument("--readiness-verifier-path", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        outputs = generate(
            arguments.campaign_root,
            arguments.trusted_provenance,
            arguments.readiness_verifier_path,
            arguments.output_dir,
        )
    except (OSError, PlotError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
