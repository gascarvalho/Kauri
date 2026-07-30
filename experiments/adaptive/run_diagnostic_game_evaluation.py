#!/usr/bin/env python3
"""Write deterministic certificate-based rematching model evidence."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from typing import Sequence


KAURI_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(KAURI_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(KAURI_REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.diagnostic_game_evaluation import (  # noqa: E402
    canonical_evaluation_json,
    evaluate_minimax_rematching,
)


ARTIFACT_NAME = "diagnostic-game-evaluation.json"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kauri-revision",
        required=True,
        help="exact 40-character lowercase Kauri Git revision",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help=f"directory for {ARTIFACT_NAME}",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "allow development output from a dirty checkout and mark the "
            "revision binding as an explicit dirty override"
        ),
    )
    return parser.parse_args(argv)


def verify_revision_binding(
    kauri_revision: str,
    *,
    allow_dirty: bool,
) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=KAURI_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    current_head = completed.stdout.strip()
    if kauri_revision != current_head:
        raise ValueError(
            "Kauri revision must equal the current HEAD "
            f"({current_head})"
        )

    tracked_dirty = any(
        subprocess.run(
            command,
            cwd=KAURI_REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        ).returncode
        for command in (
            ["git", "diff", "--quiet"],
            ["git", "diff", "--cached", "--quiet"],
        )
    )
    adaptive_status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "experiments/adaptive",
        ],
        cwd=KAURI_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = tracked_dirty or bool(adaptive_status)
    if dirty and not allow_dirty:
        raise ValueError(
            "Kauri checkout has tracked changes or untracked adaptive "
            "experiment files; commit them or use --allow-dirty"
        )
    return (
        "verified_current_head_dirty_override"
        if dirty
        else "verified_current_clean_head"
    )


def write_evaluation(
    output_dir: Path,
    *,
    kauri_revision: str,
    revision_verification: str,
) -> Path:
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("output path exists and is not a directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / ARTIFACT_NAME
    artifact.write_text(
        canonical_evaluation_json(
            evaluate_minimax_rematching(
                kauri_revision,
                revision_verification=revision_verification,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return artifact


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        revision_verification = verify_revision_binding(
            args.kauri_revision,
            allow_dirty=args.allow_dirty,
        )
        artifact = write_evaluation(
            args.output_dir,
            kauri_revision=args.kauri_revision,
            revision_verification=revision_verification,
        )
    except (subprocess.CalledProcessError, ValueError) as error:
        raise SystemExit(f"error: {error}") from error
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
