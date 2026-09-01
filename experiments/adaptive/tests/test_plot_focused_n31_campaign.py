from __future__ import annotations

import importlib
import json

import pytest


def _plotter():
    return importlib.import_module("experiments.adaptive.plot_focused_n31_campaign")


def _accepted_result(*, supported: bool) -> dict[str, object]:
    return {
        "verdict": "PASS",
        "figure_eligible": True,
        "claim_eligible": supported,
        "terminal_slot_count": 10,
        "pair_count": 5,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "scientific_support": {"supported": supported},
    }


def test_figure_gate_allows_an_accepted_negative_result() -> None:
    _plotter()._require_campaign_gate(_accepted_result(supported=False))


@pytest.mark.parametrize(
    ("field", "value"),
    (("verdict", "FAIL"), ("figure_eligible", False), ("terminal_slot_count", 9)),
)
def test_figure_gate_rejects_ineligible_campaigns(field: str, value: object) -> None:
    result = _accepted_result(supported=False)
    result[field] = value
    with pytest.raises(_plotter().PlotError):
        _plotter()._require_campaign_gate(result)


def test_figure_gate_rejects_claim_support_mismatch() -> None:
    result = _accepted_result(supported=False)
    result["claim_eligible"] = True
    with pytest.raises(_plotter().PlotError):
        _plotter()._require_campaign_gate(result)


def test_phase_medians_require_frozen_order_and_convert_milli_tps() -> None:
    phases = [
        {"phase": phase, "median_milli_tps": value}
        for phase, value in zip(
            _plotter().PHASES,
            (10_500_000, 100_000, 11_500_000, 11_300_000),
            strict=True,
        )
    ]
    assert _plotter()._phase_medians(
        {"scientific_measurements": {"phases": phases}}
    ) == {
        "baseline": 10_500.0,
        "fault": 100.0,
        "epoch1": 11_500.0,
        "late": 11_300.0,
    }


def test_figure_artifacts_exclude_platform_and_wall_clock_metadata(tmp_path) -> None:
    rows = [
        {
            "pair_id": f"pair-{ordinal:02d}",
            **{
                f"{arm}_{phase}_tps": 10_000.0 + ordinal
                for arm in ("control", "adaptive")
                for phase in _plotter().PHASES
            },
            "adaptive_ratio": 1.0,
            "paired_ratio": 1.0,
            "effect_tps": 0.0,
        }
        for ordinal in range(1, 6)
    ]

    csv_path = tmp_path / "campaign.csv"
    _plotter()._write_csv(csv_path, rows)
    csv_bytes = csv_path.read_bytes()
    assert b"\r\n" not in csv_bytes
    assert csv_bytes.endswith(b"\n")

    _plotter()._render(tmp_path / "campaign", rows)
    pdf_bytes = (tmp_path / "campaign.pdf").read_bytes()
    assert b"/CreationDate" not in pdf_bytes
    assert b"/ModDate" not in pdf_bytes

    _plotter()._render(tmp_path / "campaign-copy", rows)
    assert (tmp_path / "campaign-copy.pdf").read_bytes() == pdf_bytes
    assert (tmp_path / "campaign-copy.png").read_bytes() == (
        tmp_path / "campaign.png"
    ).read_bytes()


def _accepted_rows() -> list[dict[str, object]]:
    plotter = _plotter()
    return [
        {
            "pair_id": f"pair-{ordinal:02d}",
            **{
                f"{arm}_{phase}_tps": 10_000.0 + ordinal
                for arm in ("control", "adaptive")
                for phase in plotter.PHASES
            },
            "adaptive_ratio": 0.98 + ordinal / 1000,
            "paired_ratio": 0.97 + ordinal / 1000,
            "effect_tps": float(-ordinal),
        }
        for ordinal in range(1, 6)
    ]


def _accepted_source(tmp_path):
    plotter = _plotter()
    csv_path = tmp_path / f"{plotter.STEM}.csv"
    plotter._write_csv(csv_path, _accepted_rows())
    manifest = {
        "schema_version": 1,
        "kind": "kauri-cert13-n31-campaign-figure-manifest-v1",
        "validator_verdict": "PASS",
        "figure_eligible": True,
        "claim_eligible": False,
        "scientific_support": {"supported": False},
        "campaign_directory": "/accepted/campaign",
        "campaign_tree_sha256": "1" * 64,
        "campaign_seal_sha256": "2" * 64,
        "campaign_validation_sha256": "3" * 64,
        "trusted_provenance_sha256": "4" * 64,
        "evidence_revision": "5" * 40,
        "artifacts": {
            csv_path.name: {
                "sha256": plotter._sha256_file(csv_path),
                "size_bytes": csv_path.stat().st_size,
            }
        },
        "caveats": ["accepted source caveat"],
    }
    manifest_path = tmp_path / "source-manifest.json"
    manifest_path.write_bytes(plotter._canonical(manifest))
    return csv_path, manifest_path


def test_restyle_is_bound_to_the_accepted_csv_and_source_manifest(tmp_path) -> None:
    plotter = _plotter()
    csv_path, manifest_path = _accepted_source(tmp_path)
    outputs = plotter.restyle_accepted_package(
        csv_path, manifest_path, tmp_path / "restyled"
    )
    second = plotter.restyle_accepted_package(
        csv_path, manifest_path, tmp_path / "restyled-second"
    )

    output_names = {path.name for path in outputs}
    assert output_names == {
        "accepted-source-manifest.json",
        f"{plotter.STEM}.csv",
        f"{plotter.STEM}.pdf",
        f"{plotter.STEM}.png",
        "figure-manifest.json",
    }
    assert (tmp_path / "restyled" / f"{plotter.STEM}.csv").read_bytes() == (
        csv_path.read_bytes()
    )
    assert (
        tmp_path / "restyled" / "accepted-source-manifest.json"
    ).read_bytes() == manifest_path.read_bytes()
    restyled_manifest = json.loads(
        (tmp_path / "restyled" / "figure-manifest.json").read_bytes()
    )
    assert restyled_manifest["rendering_mode"].startswith("presentation-only")
    assert restyled_manifest["accepted_csv_sha256"] == plotter._sha256_file(
        csv_path
    )
    assert (tmp_path / "restyled" / f"{plotter.STEM}.pdf").read_bytes() == (
        tmp_path / "restyled-second" / f"{plotter.STEM}.pdf"
    ).read_bytes()
    assert (tmp_path / "restyled" / f"{plotter.STEM}.png").read_bytes() == (
        tmp_path / "restyled-second" / f"{plotter.STEM}.png"
    ).read_bytes()
    assert {path.name for path in second} == output_names


def test_restyle_rejects_a_csv_that_drifted_after_acceptance(tmp_path) -> None:
    plotter = _plotter()
    csv_path, manifest_path = _accepted_source(tmp_path)
    csv_path.write_bytes(csv_path.read_bytes() + b"\n")

    with pytest.raises(plotter.PlotError, match="not bound"):
        plotter.restyle_accepted_package(
            csv_path, manifest_path, tmp_path / "restyled"
        )
