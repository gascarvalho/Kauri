#!/usr/bin/env python3
"""Write the frozen synthetic diagnosis comparison as canonical JSON.

Run from the Kauri repository root:

    .venv-adaptive/bin/python experiments/adaptive/run_diagnosis_evaluation.py \
        --output-dir results/diagnosis-synthetic
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence


KAURI_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(KAURI_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(KAURI_REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.diagnosis_evaluation import (  # noqa: E402
    canonical_evaluation_json,
    evaluate_frozen_scenarios,
)


ARTIFACT_NAME = "diagnosis-evaluation.json"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen N=7 synthetic two-mode diagnosis comparison"
        )
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="directory for diagnosis-evaluation.json",
    )
    return parser.parse_args(argv)


def write_evaluation(output_dir: Path) -> Path:
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("output path exists and is not a directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / ARTIFACT_NAME
    artifact.write_text(
        canonical_evaluation_json(evaluate_frozen_scenarios()) + "\n",
        encoding="utf-8",
    )
    return artifact


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    artifact = write_evaluation(args.output_dir)
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
