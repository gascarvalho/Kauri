#!/usr/bin/env python3
"""Bind the frozen planning and live campaigns into one thesis artifact."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Sequence

KAURI_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(KAURI_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(KAURI_REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.campaign_thesis_evaluation import (  # noqa: E402
    CampaignThesisEvaluationError,
    build_campaign_thesis_evaluation,
    canonical_campaign_thesis_evaluation_json,
)
from experiments.adaptive.kauri_experiment.thesis_evaluation import (  # noqa: E402
    ThesisEvaluationError,
    parse_thesis_json_object,
)

ARTIFACT_NAME = "campaign-thesis-evaluation.json"


def _read_source(path: Path, label: str) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CampaignThesisEvaluationError(f"cannot read {label}: {error}") from error
    try:
        return parse_thesis_json_object(source, label)
    except ThesisEvaluationError as error:
        raise CampaignThesisEvaluationError(str(error)) from error


def write_campaign_thesis_evaluation(
    planning_campaign_path: Path,
    live_campaign_path: Path,
    live_evidence_root: Path,
    output_directory: Path,
) -> Path:
    """Strictly validate both source files and write a new artifact."""

    planning_path = planning_campaign_path.resolve(strict=True)
    live_path = live_campaign_path.resolve(strict=True)
    evidence_root = live_evidence_root.resolve(strict=True)
    planning = _read_source(planning_path, "planning breadth campaign")
    live = _read_source(live_path, "N=7 fault repetition campaign")
    campaign = build_campaign_thesis_evaluation(
        planning,
        live,
        live_evidence_root=evidence_root,
    )

    output = output_directory.resolve()
    if output.exists():
        raise CampaignThesisEvaluationError("output directory already exists")
    output.mkdir(parents=True, exist_ok=False)
    artifact = output / ARTIFACT_NAME
    try:
        with artifact.open("x", encoding="utf-8") as destination:
            destination.write(
                canonical_campaign_thesis_evaluation_json(
                    campaign,
                    live_evidence_root=evidence_root,
                )
            )
            destination.write("\n")
    except Exception:
        if artifact.exists():
            artifact.unlink()
        output.rmdir()
        raise
    return artifact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--planning-campaign",
        required=True,
        type=Path,
        help="path to planning-breadth-campaign.json",
    )
    parser.add_argument(
        "--live-campaign",
        required=True,
        type=Path,
        help="path to n7-fault-repetition-campaign.json",
    )
    parser.add_argument(
        "--live-evidence-root",
        required=True,
        type=Path,
        help="root containing the bound N=7 plan, records, and raw verdicts",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help=f"new directory for {ARTIFACT_NAME}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        artifact = write_campaign_thesis_evaluation(
            arguments.planning_campaign,
            arguments.live_campaign,
            arguments.live_evidence_root,
            arguments.output_dir,
        )
    except (CampaignThesisEvaluationError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
