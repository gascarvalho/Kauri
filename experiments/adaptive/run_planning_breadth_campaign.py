#!/usr/bin/env python3
"""Write the revision-bound N=31 planning breadth audit."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from typing import Sequence


KAURI_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(KAURI_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(KAURI_REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.planning_breadth_campaign import (  # noqa: E402
    PlanningBreadthError,
    build_planning_breadth_campaign,
    canonical_planning_breadth_json,
)


ARTIFACT_NAME = "planning-breadth-campaign.json"


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
        help=f"new directory for {ARTIFACT_NAME}",
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
        raise PlanningBreadthError(
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
        raise PlanningBreadthError(
            "Kauri checkout has tracked changes or untracked adaptive "
            "experiment files; commit them or use --allow-dirty"
        )
    return (
        "verified_current_head_dirty_override"
        if dirty
        else "verified_current_clean_head"
    )


def write_campaign(
    output_directory: Path,
    *,
    kauri_revision: str,
    revision_verification: str,
) -> Path:
    output = output_directory.resolve()
    if output.exists():
        raise PlanningBreadthError("output directory already exists")
    campaign = build_planning_breadth_campaign(
        kauri_revision=kauri_revision,
        revision_verification=revision_verification,
    )
    output.mkdir(parents=True, exist_ok=False)
    artifact = output / ARTIFACT_NAME
    try:
        with artifact.open("x", encoding="utf-8") as destination:
            destination.write(canonical_planning_breadth_json(campaign))
            destination.write("\n")
    except Exception:
        if artifact.exists():
            artifact.unlink()
        output.rmdir()
        raise
    return artifact


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        revision_verification = verify_revision_binding(
            arguments.kauri_revision,
            allow_dirty=arguments.allow_dirty,
        )
        artifact = write_campaign(
            arguments.output_dir,
            kauri_revision=arguments.kauri_revision,
            revision_verification=revision_verification,
        )
    except (OSError, subprocess.CalledProcessError, PlanningBreadthError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
