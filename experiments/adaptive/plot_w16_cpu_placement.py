#!/usr/bin/env python3
"""Render a source-bound figure for the accepted W16 v8 C-032 result.

This renderer deliberately accepts only the one sealed evidence archive named
in the C-032 ledger. It verifies every archived payload member, requires the
24 raw roots, and recomputes ratios from accepted validation cell records.
It does not replay raw commit events.
"""

from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

ARCHIVE_SHA256 = "19e49dfdc63ac2fe5aa8b7e8fe88c3bc4fe368bf21d129a088c0d88db1f0e9f7"
ARCHIVE_ROOT = "w16-v8-evidence"
CAMPAIGN_ID = "w16-cpu-repeat-v8-20260927-8dd8ba38"
REVISION = "8dd8ba388c3209bc952010c5e346259c331cc1b3"
FREEZE_SHA256 = "347c854817051fe6c72422f4fa49ae6a78e3ab54e23ccbae3ac4a2ae250961f1"
Y_AXIS_LABEL = "Relative placement effect (dimensionless ratio)"
FIGURE_BOUNDARY = (
    "Ratios are recomputed from archived validation cells; raw commit events are not replayed."
)
PDF_METADATA = {
    "Title": "C-032 static CPU placement",
    "Creator": "Kauri W16 C-032 evidence renderer",
    "CreationDate": None,
    "ModDate": None,
}
FORWARD = ("slow-roots:homogeneous", "fast-roots:homogeneous", "slow-roots:heterogeneous", "fast-roots:heterogeneous")
REVERSE = tuple(reversed(FORWARD))


class PlotError(RuntimeError):
    """The supplied bytes are not the accepted C-032 evidence source."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise PlotError(f"{label} must be a regular non-symlink file")


def _archive_members(archive: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["tar", "--use-compress-program=unzstd", "-tf", str(archive)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise PlotError("cannot list the sealed zstd archive") from error
    members = completed.stdout.splitlines()
    if not members or len(members) != len(set(members)):
        raise PlotError("archive members are absent or duplicated")
    for member in members:
        pure = PurePosixPath(member)
        if (
            not member.startswith(f"{ARCHIVE_ROOT}/")
            or pure.is_absolute()
            or ".." in pure.parts
        ):
            raise PlotError("archive contains an unsafe or unexpected member path")
    return members


def _extract_verified_archive(archive: Path, destination: Path) -> Path:
    _regular_file(archive, "evidence archive")
    if _sha256(archive) != ARCHIVE_SHA256:
        raise PlotError("archive SHA-256 is not the accepted W16 v8 source")
    _archive_members(archive)
    try:
        subprocess.run(
            ["tar", "--use-compress-program=unzstd", "-xf", str(archive), "-C", str(destination)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise PlotError("cannot extract the sealed zstd archive") from error
    root = destination / ARCHIVE_ROOT
    if root.is_symlink() or not root.is_dir():
        raise PlotError("archive root is missing or unsafe")
    return root


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    _regular_file(path, label)
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlotError(f"{label} is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise PlotError(f"{label} must be a JSON object")
    return value


def _verify_inventory(root: Path) -> None:
    inventory = _read_json(root / "inventory-v1.json", "archive inventory")
    if inventory.get("schema_version") != 1 or not isinstance(inventory.get("payload_files"), list):
        raise PlotError("archive inventory schema drifted")
    expected: dict[str, tuple[int, str]] = {}
    for entry in inventory["payload_files"]:
        if not isinstance(entry, Mapping):
            raise PlotError("archive inventory member is malformed")
        relative, size, digest = entry.get("path"), entry.get("bytes"), entry.get("sha256")
        pure = PurePosixPath(relative) if isinstance(relative, str) else None
        if (
            pure is None or pure.is_absolute() or ".." in pure.parts
            or type(size) is not int or size < 0
            or not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or relative in expected
        ):
            raise PlotError("archive inventory member contract drifted")
        expected[relative] = (size, digest)
    payload = root / "payload"
    if payload.is_symlink() or not payload.is_dir():
        raise PlotError("archive payload is missing or unsafe")
    payload_paths = list(payload.rglob("*"))
    if any(path.is_symlink() for path in payload_paths):
        raise PlotError("archive payload contains a symlink")
    actual = {
        path.relative_to(payload).as_posix()
        for path in payload_paths
        if path.is_file()
    }
    if actual != set(expected):
        raise PlotError("archive payload does not exactly match its inventory")
    for relative, (size, digest) in expected.items():
        path = payload / relative
        _regular_file(path, f"payload member {relative}")
        if path.stat().st_size != size or _sha256(path) != digest:
            raise PlotError(f"payload member hash mismatch: {relative}")


def _require_raw_roots(payload: Path) -> None:
    results = payload / "results"
    if results.is_symlink() or not results.is_dir():
        raise PlotError("archived raw results are absent")
    for index in range(1, 7):
        labels = FORWARD if index % 2 else REVERSE
        roots = [results / f"block-{index:02d}" / label.replace(":", "-") for label in labels]
        if any(root.is_symlink() or not root.is_dir() for root in roots):
            raise PlotError(f"archived raw block {index} is incomplete")
        for root in roots:
            raw = root / "raw"
            replicas = [raw / f"replica-{replica}.jsonl" for replica in range(31)]
            if raw.is_symlink() or not raw.is_dir() or any(path.is_symlink() or not path.is_file() for path in replicas):
                raise PlotError(f"archived raw root is incomplete: {root.name}")


def _recompute_block_effects(cells: Sequence[Mapping[str, Any]]) -> Mapping[str, Fraction]:
    """Recompute every displayed ratio from accepted cell counts and durations."""
    if len(cells) != 4:
        raise PlotError("archived validation block schema drifted")
    rates: dict[str, Fraction] = {}
    for cell in cells:
        label, transactions, duration = cell.get("label"), cell.get("transaction_count"), cell.get("duration_ns")
        if (
            label not in FORWARD
            or type(transactions) is not int
            or type(duration) is not int
            or transactions <= 0
            or duration <= 0
            or label in rates
        ):
            raise PlotError("archived validation cell rate is malformed")
        rates[label] = Fraction(transactions, duration)
    if set(rates) != set(FORWARD):
        raise PlotError("archived validation block misses a treatment cell")
    homogeneous = rates["fast-roots:homogeneous"] / rates["slow-roots:homogeneous"]
    heterogeneous = rates["fast-roots:heterogeneous"] / rates["slow-roots:heterogeneous"]
    return {
        "homogeneous_ratio": homogeneous,
        "heterogeneous_ratio": heterogeneous,
        "interaction_ratio": heterogeneous / homogeneous,
    }


def _require_stored_float(effects: Mapping[str, Any], name: str, exact: Fraction) -> None:
    stored = effects.get(name)
    if type(stored) not in (int, float) or not math.isfinite(float(stored)) or float(stored) != float(exact):
        raise PlotError(f"archived validation stored {name} does not match exact cell ratio")


def _rebuild_effects(campaign: Mapping[str, Any]) -> None:
    blocks = campaign.get("blocks")
    if not isinstance(blocks, list) or len(blocks) != 6:
        raise PlotError("archived validation lacks six campaign blocks")
    for index, block in enumerate(blocks, 1):
        if not isinstance(block, Mapping) or block.get("block_index") != index:
            raise PlotError("archived validation block order drifted")
        cells = block.get("cells")
        effects = block.get("effects")
        if not isinstance(cells, list) or not isinstance(effects, Mapping):
            raise PlotError("archived validation block schema drifted")
        if any(not isinstance(cell, Mapping) for cell in cells):
            raise PlotError("archived validation cell is malformed")
        ratios = _recompute_block_effects(cells)
        interaction = ratios["interaction_ratio"]
        exact = effects.get("interaction_ratio_exact")
        if (
            not isinstance(exact, Mapping)
            or exact.get("numerator") != interaction.numerator
            or exact.get("denominator") != interaction.denominator
            or effects.get("direct_and_adjusted_positive") is not True
        ):
            raise PlotError("archived validation effect does not match its cell rates")
        for name, ratio in ratios.items():
            _require_stored_float(effects, name, ratio)


def rebuild_campaign_from_archive(archive: Path) -> Mapping[str, Any]:
    """Verify the archive and recompute ratios from its accepted cell records."""
    with tempfile.TemporaryDirectory(prefix="kauri-w16-v8-") as temporary:
        root = _extract_verified_archive(archive, Path(temporary))
        _verify_inventory(root)
        payload = root / "payload"
        _require_raw_roots(payload)
        campaign = _read_json(payload / "execution" / "campaign-validation-v8.json", "archived campaign validation")
        _rebuild_effects(campaign)
    if (
        campaign.get("verdict") != "PASS"
        or campaign.get("technical_improvement_gate_passed") is not True
        or campaign.get("campaign_id") != CAMPAIGN_ID
        or campaign.get("revision") != REVISION
        or campaign.get("campaign_freeze_sha256") != FREEZE_SHA256
    ):
        raise PlotError("archived validation does not match the accepted C-032 campaign")
    return campaign


def _effects(campaign: Mapping[str, Any]) -> list[Mapping[str, Fraction]]:
    blocks = campaign.get("blocks")
    if not isinstance(blocks, list) or len(blocks) != 6:
        raise PlotError("reconstructed campaign lacks six blocks")
    values: list[Mapping[str, Fraction]] = []
    for index, block in enumerate(blocks, 1):
        if not isinstance(block, Mapping) or block.get("block_index") != index:
            raise PlotError("reconstructed block order drifted")
        cells = block.get("cells")
        effects = block.get("effects")
        if not isinstance(cells, list) or not isinstance(effects, Mapping) or any(not isinstance(cell, Mapping) for cell in cells):
            raise PlotError("reconstructed block lacks validation cells")
        ratios = _recompute_block_effects(cells)
        exact = effects.get("interaction_ratio_exact")
        interaction = ratios["interaction_ratio"]
        if (
            not isinstance(exact, Mapping)
            or exact.get("numerator") != interaction.numerator
            or exact.get("denominator") != interaction.denominator
            or effects.get("direct_and_adjusted_positive") is not True
            or interaction <= 1
        ):
            raise PlotError("reconstructed block lacks a positive adjusted ratio")
        for name, ratio in ratios.items():
            _require_stored_float(effects, name, ratio)
        values.append(ratios)
    return values


def _geometric_mean(values: Sequence[Fraction]) -> float:
    if not values or any(value <= 0 for value in values):
        raise PlotError("geometric mean requires positive exact ratios")
    return math.exp(math.fsum(math.log(float(value)) for value in values) / len(values))


def _build_figure(values: Sequence[Mapping[str, Fraction]]):
    """Build an untruncated three-series dot/line comparison."""
    try:
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise PlotError("matplotlib is required to render the C-032 figure") from error
    positions = list(range(1, 7))
    series = (
        ("homogeneous_ratio", "Homogeneous B/A", "#2563eb"),
        ("heterogeneous_ratio", "Heterogeneous B/A", "#0f766e"),
        ("interaction_ratio", "Adjusted interaction", "#7c3aed"),
    )
    figure, axis = plt.subplots(figsize=(7.2, 4.2))
    figure.subplots_adjust(bottom=0.22, top=0.72, left=0.11, right=0.98)
    for key, label, color in series:
        axis.plot(positions, [float(block[key]) for block in values], marker="o", markersize=6, linewidth=1.8, color=color, label=label)
    axis.axhline(1.0, color="#475569", linewidth=1.1, label="No B/A difference")
    axis.axhline(1.10, color="#b45309", linestyle="--", linewidth=1.4, label="Predeclared adjusted 1.10 threshold")
    axis.set_xticks(positions, [f"B{index}\n{'F' if index % 2 else 'R'} order" for index in positions], fontsize=10)
    axis.set_ylabel(Y_AXIS_LABEL, fontsize=10)
    axis.tick_params(axis="y", labelsize=10)
    axis.grid(axis="y", color="#cbd5e1", alpha=0.75)
    axis.legend(loc="lower center", bbox_to_anchor=(0.5, 1.03), ncols=3, frameon=False, fontsize=9)
    axis.set_ylim(bottom=0.9)
    figure.suptitle("Static CPU placement contrast in one frozen N=31 profile", y=0.98, fontsize=13, fontweight="bold")
    figure.text(0.5, 0.90, "Six static Epoch-0 blocks; commit-derived observer throughput.", ha="center", fontsize=10, color="#475569")
    geometric_mean = _geometric_mean([block["interaction_ratio"] for block in values])
    figure.text(0.5, 0.035, f"C-032 | adjusted geometric mean {geometric_mean:.3f} | 6/6 positive blocks", ha="center", fontsize=9, color="#475569")
    return figure


def _generator_revision() -> str | None:
    relative = Path(__file__).resolve().relative_to(REPOSITORY_ROOT).as_posix()
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", relative], cwd=REPOSITORY_ROOT, capture_output=True, text=True)
    clean = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", relative], cwd=REPOSITORY_ROOT, capture_output=True, text=True)
    if tracked.returncode or clean.returncode:
        return None
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True).stdout.strip()
    return revision or None


def _write_manifest(campaign: Mapping[str, Any], output_png: Path, output_pdf: Path | None) -> Path:
    outputs = [output_png] + ([output_pdf] if output_pdf is not None else [])
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "kauri-w16-cpu-placement-figure-manifest-v1",
        "archive_sha256": ARCHIVE_SHA256,
        "campaign_id": campaign["campaign_id"],
        "campaign_freeze_sha256": campaign["campaign_freeze_sha256"],
        "evidence_revision": campaign["revision"],
        "plotter_source_sha256": _sha256(Path(__file__).resolve()),
        "figure_boundary": FIGURE_BOUNDARY,
        "outputs": {path.name: {"sha256": _sha256(path), "size_bytes": path.stat().st_size} for path in outputs},
    }
    revision = _generator_revision()
    if revision is not None:
        manifest["generator_revision"] = revision
    manifest_path = output_png.parent / "figure-manifest.json"
    manifest_path.write_bytes(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n")
    return manifest_path


def render(archive: Path, output_png: Path, output_pdf: Path | None = None) -> Mapping[str, Any]:
    """Rebuild C-032 from the sealed raw source and write the requested figure."""
    campaign = rebuild_campaign_from_archive(archive)
    values = _effects(campaign)
    output_png.parent.mkdir(parents=True, exist_ok=True)
    if output_pdf is not None:
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
    figure = _build_figure(values)
    figure.savefig(output_png, dpi=180, metadata={"Software": "Kauri W16 C-032 evidence renderer"})
    if output_pdf is not None:
        figure.savefig(output_pdf, metadata=PDF_METADATA)
    import matplotlib.pyplot as plt
    plt.close(figure)
    _write_manifest(campaign, output_png, output_pdf)
    return campaign


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output-png", type=Path, required=True)
    parser.add_argument("--output-pdf", type=Path)
    arguments = parser.parse_args(argv)
    try:
        render(arguments.archive, arguments.output_png, arguments.output_pdf)
    except PlotError as error:
        print(f"C-032 figure refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
