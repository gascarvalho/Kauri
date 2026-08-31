#!/usr/bin/env python3
"""Replay frozen reputation policies over an accepted live fault campaign."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from typing import Sequence


KAURI_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(KAURI_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(KAURI_REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.reputation_policy_replay import (  # noqa: E402
    ReputationPolicyReplayError,
    build_campaign,
    canonical_json,
)


ARTIFACT_NAME = "reputation-policy-replay.json"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        type=Path,
        default=(
            KAURI_REPOSITORY_ROOT
            / "experiments/adaptive/profiles/n7-reputation-policy-replay-v1.json"
        ),
    )
    parser.add_argument("--kauri-revision", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow development output and mark the revision binding dirty",
    )
    return parser.parse_args(argv)


def verify_revision_binding(revision: str, *, allow_dirty: bool) -> str:
    current = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=KAURI_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if revision != current:
        raise ReputationPolicyReplayError(
            f"Kauri revision must equal current HEAD ({current})"
        )
    dirty = any(
        subprocess.run(
            command,
            cwd=KAURI_REPOSITORY_ROOT,
            check=False,
            capture_output=True,
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
    dirty = dirty or bool(adaptive_status)
    if dirty and not allow_dirty:
        raise ReputationPolicyReplayError(
            "commit adaptive experiment changes or use --allow-dirty"
        )
    return (
        "verified_current_head_dirty_override"
        if dirty
        else "verified_current_clean_head"
    )


def write_campaign(
    output_directory: Path,
    profile: Path,
    *,
    revision: str,
    revision_verification: str,
) -> Path:
    output = output_directory.resolve()
    if output.exists():
        raise ReputationPolicyReplayError("output directory already exists")
    campaign = build_campaign(
        KAURI_REPOSITORY_ROOT,
        profile.resolve(),
        analysis_revision=revision,
        revision_verification=revision_verification,
    )
    output.mkdir(parents=True, exist_ok=False)
    artifact = output / ARTIFACT_NAME
    try:
        artifact.write_text(canonical_json(campaign) + "\n", encoding="utf-8")
    except Exception:
        if artifact.exists():
            artifact.unlink()
        output.rmdir()
        raise
    return artifact


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        verification = verify_revision_binding(
            arguments.kauri_revision, allow_dirty=arguments.allow_dirty
        )
        artifact = write_campaign(
            arguments.output_dir,
            arguments.profile,
            revision=arguments.kauri_revision,
            revision_verification=verification,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        ReputationPolicyReplayError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
