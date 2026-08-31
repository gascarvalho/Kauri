"""Rendering guardrails for the reputation-policy replay figure."""

from __future__ import annotations

import hashlib
import json
import matplotlib
from pathlib import Path

from experiments.adaptive import plot_reputation_policy_replay as plotter


def test_plotter_uses_a_headless_backend() -> None:
    assert matplotlib.get_backend().lower() == "agg"


def _figure_input(tmp_path: Path) -> tuple[Path, Path]:
    arms = (
        "sigkill_crash",
        "static_authenticated_false_report",
        "static_persistent_omission",
    )
    artifact = {
        "verdict": "PASS",
        "run_count": 15,
        "runs": [
            {
                "arm": arm,
                "comparison": {"kendall_inversion_count": index % 4},
            }
            for arm in arms
            for index in range(5)
        ],
        "summaries": {
            arm: {
                "mechanisms": {
                    mechanism: {
                        "actor_excluded_from_influential_roles_count": 5
                    }
                    for mechanism in ("responsiveness", "latency-priority")
                }
            }
            for arm in arms
        },
    }
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    validation_path = tmp_path / "validation.json"
    validation_path.write_text(
        json.dumps(
            {
                "verdict": "PASS",
                "artifact_sha256": hashlib.sha256(
                    artifact_path.read_bytes()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return artifact_path, validation_path


def test_pdf_omits_wall_clock_metadata(tmp_path: Path) -> None:
    artifact, validation = _figure_input(tmp_path)
    png, pdf = plotter.render(artifact, validation, tmp_path / "figures")
    second_png, second_pdf = plotter.render(
        artifact, validation, tmp_path / "figures-second"
    )

    assert b"/CreationDate" not in pdf.read_bytes()
    assert b"/ModDate" not in pdf.read_bytes()
    assert second_png.read_bytes() == png.read_bytes()
    assert second_pdf.read_bytes() == pdf.read_bytes()
