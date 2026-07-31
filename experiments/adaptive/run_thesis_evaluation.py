#!/usr/bin/env python3
"""Compose one revision-consistent model/live thesis evidence artifact."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.thesis_evaluation import (  # noqa: E402
    ThesisEvaluationError,
    build_thesis_evaluation,
    canonical_thesis_evaluation_json,
    parse_thesis_json_object,
)


ARTIFACT_NAME = "thesis-evaluation.json"


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ThesisEvaluationError(f"{label} is absent: {path}") from error
    except OSError as error:
        raise ThesisEvaluationError(f"cannot read {label}: {error}") from error
    return parse_thesis_json_object(source, label)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-evidence", required=True, type=Path)
    parser.add_argument(
        "--arm-verdict",
        required=True,
        action="append",
        type=Path,
        help="repeat exactly once for each of the three frozen fault arms",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        model = _load_json(arguments.model_evidence, "model evidence")
        arms = [
            _load_json(path, f"arm verdict {index}")
            for index, path in enumerate(arguments.arm_verdict, start=1)
        ]
        evaluation = build_thesis_evaluation(model, arms)
        output_directory = arguments.output_dir.resolve()
        output_directory.mkdir(parents=True, exist_ok=False)
        artifact = output_directory / ARTIFACT_NAME
        with artifact.open("x", encoding="utf-8") as destination:
            destination.write(canonical_thesis_evaluation_json(evaluation))
            destination.write("\n")
    except (OSError, ThesisEvaluationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
