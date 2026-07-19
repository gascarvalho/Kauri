"""Synthetic PASS-gate tests; generated figures are never experiment evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
from pathlib import Path
import threading

import pytest

import plot
import synthetic_run
import validator


def test_refuses_terminal_non_pass_before_loading_matplotlib(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    (tmp_path / "run/raw/adaptive-manager.jsonl").write_text("", encoding="utf-8")
    output = tmp_path / "validated"
    verdict = validator.validate_run(manifest, epochs, output)
    assert verdict["verdict"] == "INCOMPLETE"

    with pytest.raises(plot.PlotError, match="terminal INCOMPLETE"):
        plot.generate_figure(output)

    assert not (output / "figure.png").exists()
    assert not (output / "figure.pdf").exists()


def test_renders_png_and_pdf_from_pass_only(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    output = tmp_path / "validated"
    verdict = validator.validate_run(manifest, epochs, output)
    assert verdict["verdict"] == "PASS"

    png, pdf = plot.generate_figure(output)

    assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert pdf.read_bytes().startswith(b"%PDF-")
    assert png.stat().st_size > 10_000
    assert pdf.stat().st_size > 1_000

    with pytest.raises(plot.PlotError, match="already exists"):
        plot.generate_figure(output)


def test_rechecks_leader_conservation_before_plotting(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    output = tmp_path / "validated"
    verdict = validator.validate_run(manifest, epochs, output)
    assert verdict["verdict"] == "PASS"
    throughput_path = output / "throughput.csv"
    with throughput_path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
        fieldnames = reader.fieldnames
    assert fieldnames is not None
    rows[0]["leader_0_tps"] = str(float(rows[0]["leader_0_tps"]) + 1.0)
    with throughput_path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    verdict_path = output / "validation.json"
    verdict_value = json.loads(verdict_path.read_text(encoding="utf-8"))
    verdict_value["artifacts"]["throughput"]["sha256"] = hashlib.sha256(
        throughput_path.read_bytes()
    ).hexdigest()
    verdict_path.write_text(json.dumps(verdict_value), encoding="utf-8")

    with pytest.raises(plot.PlotError, match="conserve aggregate"):
        plot.generate_figure(output)

    assert not (output / "figure.png").exists()
    assert not (output / "figure.pdf").exists()


@pytest.mark.parametrize(
    "artifact",
    (
        "manifest.json",
        "profile.json",
        "epochs.json",
        "throughput.csv",
        "reputation.csv",
    ),
)
def test_pass_verdict_detects_artifact_tampering(
    tmp_path: Path, artifact: str
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    output = tmp_path / "validated"
    verdict = validator.validate_run(manifest, epochs, output)
    assert verdict["verdict"] == "PASS"
    artifact_path = output / artifact
    artifact_path.write_bytes(artifact_path.read_bytes() + b"\n")

    with pytest.raises(plot.PlotError, match="artifact hash mismatch"):
        plot.generate_figure(output)

    assert not (output / "figure.png").exists()
    assert not (output / "figure.pdf").exists()


def test_concurrent_plotters_cannot_replace_one_figure_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    output = tmp_path / "validated"
    verdict = validator.validate_run(manifest, epochs, output)
    assert verdict["verdict"] == "PASS"
    entered = threading.Event()
    release = threading.Event()

    def paused_plot(directory: Path) -> tuple[Path, Path]:
        entered.set()
        assert release.wait(timeout=10)
        return (directory / "figure.png", directory / "figure.pdf")

    monkeypatch.setattr(plot, "_generate_claimed_figure", paused_plot)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(plot.generate_figure, output)
        assert entered.wait(timeout=10)
        with pytest.raises(plot.PlotError, match="already claimed"):
            plot.generate_figure(output)
        release.set()
        assert first.result(timeout=20) == (
            output.resolve() / "figure.png",
            output.resolve() / "figure.pdf",
        )

    assert not (output / plot.PLOT_CLAIM).exists()
