"""Focused fail-closed contracts for the C-032 source-bound renderer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive import plot_w16_cpu_placement as plot


def test_figure_labels_state_the_recomputation_boundary() -> None:
    assert plot.Y_AXIS_LABEL == "Relative placement effect (dimensionless ratio)"
    assert "archived validation cells" in plot.FIGURE_BOUNDARY
    assert "raw commit events are not replayed" in plot.FIGURE_BOUNDARY


def test_pdf_metadata_has_no_clock_field() -> None:
    assert plot.PDF_METADATA["CreationDate"] is None
    assert plot.PDF_METADATA["ModDate"] is None


def _campaign() -> dict[str, object]:
    labels = (
        "slow-roots:homogeneous",
        "fast-roots:homogeneous",
        "slow-roots:heterogeneous",
        "fast-roots:heterogeneous",
    )
    blocks = []
    for index in range(1, 7):
        counts = (100 + index, 120 + index, 80 + index, 144 + index)
        cells = [
            {"label": label, "transaction_count": count, "duration_ns": 10}
            for label, count in zip(labels, counts, strict=True)
        ]
        effects = plot._recompute_block_effects(cells)
        blocks.append(
            {
                "block_index": index,
                "cells": cells,
                "effects": {
                    "homogeneous_ratio": float(effects["homogeneous_ratio"]),
                    "heterogeneous_ratio": float(effects["heterogeneous_ratio"]),
                    "interaction_ratio": float(effects["interaction_ratio"]),
                    "interaction_ratio_exact": {
                        "numerator": effects["interaction_ratio"].numerator,
                        "denominator": effects["interaction_ratio"].denominator,
                    },
                    "direct_and_adjusted_positive": True,
                },
            }
        )
    return {
        "campaign_id": plot.CAMPAIGN_ID,
        "campaign_freeze_sha256": plot.FREEZE_SHA256,
        "revision": plot.REVISION,
        "blocks": blocks,
    }


def test_effects_recompute_all_three_series_and_geometric_mean() -> None:
    effects = plot._effects(_campaign())

    assert len(effects) == 6
    assert all(set(block) == {"homogeneous_ratio", "heterogeneous_ratio", "interaction_ratio"} for block in effects)
    assert plot._geometric_mean([block["interaction_ratio"] for block in effects]) > 1


def test_effects_reject_stored_float_that_disagrees_with_exact_cell_ratio() -> None:
    campaign = _campaign()
    campaign["blocks"][0]["effects"]["homogeneous_ratio"] = 9.0  # type: ignore[index]

    with pytest.raises(plot.PlotError, match="stored homogeneous_ratio"):
        plot._effects(campaign)


def test_figure_uses_points_and_lines_not_truncated_bars() -> None:
    effects = plot._effects(_campaign())
    figure = plot._build_figure(effects)
    axis = figure.axes[0]
    try:
        assert not axis.patches
        assert len(axis.lines) >= 4  # three series plus the neutral reference
        assert axis.get_ylabel() == plot.Y_AXIS_LABEL
        assert axis.get_legend() is not None
    finally:
        import matplotlib.pyplot as plt

        plt.close(figure)


def test_effects_accepts_only_six_positive_ordered_blocks() -> None:
    campaign = {
        "blocks": [
            {"block_index": index, "effects": {"interaction_ratio": 1.4 + index / 100}}
            for index in range(1, 7)
        ]
    }

    with pytest.raises(plot.PlotError, match="validation cells"):
        plot._effects(campaign)


@pytest.mark.parametrize(
    "campaign",
    [
        {"blocks": []},
        {"blocks": [{"block_index": 2, "effects": {"interaction_ratio": 1.2}}] * 6},
        {"blocks": [{"block_index": index, "effects": {"interaction_ratio": 1.0}} for index in range(1, 7)]},
    ],
)
def test_effects_fail_closed_on_missing_order_or_nonpositive_ratio(campaign: object) -> None:
    with pytest.raises(plot.PlotError):
        plot._effects(campaign)  # type: ignore[arg-type]


def test_archive_rejects_any_hash_other_than_accepted_source(tmp_path: Path) -> None:
    archive = tmp_path / "not-the-accepted-archive.tar.zst"
    archive.write_bytes(b"untrusted")

    with pytest.raises(plot.PlotError, match="accepted W16 v8 source"):
        plot._extract_verified_archive(archive, tmp_path / "extract")


def test_rebuild_requires_passed_v8_raw_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    archive = tmp_path / "accepted.tar.zst"
    archive.write_bytes(b"accepted")
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(plot, "_extract_verified_archive", lambda _archive, _destination: root)
    monkeypatch.setattr(plot, "_verify_inventory", lambda _root: None)
    monkeypatch.setattr(plot, "_require_raw_roots", lambda _payload: None)
    monkeypatch.setattr(plot, "_read_json", lambda _path, _label: {"verdict": "INCOMPLETE"})

    with pytest.raises(plot.PlotError, match="archived validation"):
        plot.rebuild_campaign_from_archive(archive)


def test_render_writes_deterministic_source_bound_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "accepted.tar.zst"
    archive.write_bytes(b"accepted")
    monkeypatch.setattr(plot, "rebuild_campaign_from_archive", lambda _archive: _campaign())
    png = tmp_path / "figure.png"
    pdf = tmp_path / "figure.pdf"

    plot.render(archive, png, pdf)

    manifest = json.loads((tmp_path / "figure-manifest.json").read_text())
    assert manifest["archive_sha256"] == plot.ARCHIVE_SHA256
    assert manifest["campaign_id"] == plot.CAMPAIGN_ID
    assert manifest["campaign_freeze_sha256"] == plot.FREEZE_SHA256
    assert manifest["evidence_revision"] == plot.REVISION
    assert manifest["plotter_source_sha256"] == hashlib.sha256(Path(plot.__file__).read_bytes()).hexdigest()
    assert manifest["figure_boundary"] == "Ratios are recomputed from archived validation cells; raw commit events are not replayed."
    assert manifest["outputs"]["figure.png"]["sha256"] == hashlib.sha256(png.read_bytes()).hexdigest()
    assert manifest["outputs"]["figure.pdf"]["sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
