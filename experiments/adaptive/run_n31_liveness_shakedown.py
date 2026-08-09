#!/usr/bin/env python3
"""Preflight, run, or validate one bounded non-claim N31 liveness pair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__:
    from .kauri_experiment import n31_liveness_shakedown as shakedown
else:
    from kauri_experiment import n31_liveness_shakedown as shakedown


REPOSITORY = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS_ROOT = REPOSITORY / shakedown.CANONICAL_RESULTS_RELATIVE_PATH


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""commands:
  preflight --approval-receipt <external.json> --approval-reference <ref> \\
            --approved-utc <ISO-8601 UTC>
  run       --approval-receipt <external.json>
  validate  --approval-receipt <external.json> --run-directory <pair>

The first preflight creates the external receipt exclusively. Later preflight,
run, and validate calls reuse the same receipt. One run executes both fixed
attempts before revealing either outcome and never retries or replaces one.
""",
    )
    parser.add_argument("command", choices=("preflight", "run", "validate"))
    parser.add_argument("--profile", type=Path, default=shakedown.DEFAULT_PROFILE_PATH)
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--run-directory", type=Path)
    parser.add_argument("--approval-receipt", type=Path)
    parser.add_argument("--approval-reference")
    parser.add_argument("--approved-utc")
    parser.add_argument(
        "--build-directory",
        type=Path,
        default=REPOSITORY / "build-adaptive",
    )
    parser.add_argument(
        "--build-provenance",
        type=Path,
        default=REPOSITORY
        / "build-adaptive/n31-exact-build-provenance.json",
    )
    parser.add_argument("--minimum-free-bytes", type=int, default=0)
    return parser.parse_args(argv)


def _emit(value: object, *, error: bool = False) -> None:
    print(
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True),
        file=sys.stderr if error else sys.stdout,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        if args.approval_receipt is None:
            raise shakedown.N31LivenessShakedownError(
                "all commands require --approval-receipt"
            )
        approval_path = args.approval_receipt.resolve()
        if args.command == "validate":
            if args.run_directory is None:
                raise shakedown.N31LivenessShakedownError(
                    "validate requires --run-directory"
                )
            result = shakedown.validate_pair(
                args.run_directory.resolve(),
                approval_receipt_path=approval_path,
            )
            _emit(result)
            return 0 if result.get("pair_complete") is True else 1

        profile_path = args.profile.resolve()
        repository = args.repository.resolve()
        build_directory = args.build_directory.resolve()
        build_provenance_path = args.build_provenance.resolve()
        results_root = args.results_root.resolve()
        common = {
            "profile_path": profile_path,
            "repository": repository,
            "build_directory": build_directory,
            "build_provenance_path": build_provenance_path,
            "results_root": results_root,
            "approval_receipt_path": approval_path,
            "minimum_free_bytes": args.minimum_free_bytes,
        }
        if args.command == "preflight" and not approval_path.exists():
            if not args.approval_reference or not args.approved_utc:
                raise shakedown.N31LivenessShakedownError(
                    "new preflight receipt requires --approval-reference and --approved-utc"
                )
            preflight_receipt = shakedown.prepare_approval_receipt(
                **common,
                approval_reference=args.approval_reference,
                approved_utc=args.approved_utc,
            )
        else:
            preflight_receipt = shakedown.preflight(**common)
        if args.command == "preflight":
            _emit(preflight_receipt)
            return 0

        profile = shakedown.load_frozen_profile(profile_path)
        attempts = shakedown.run_pair(
            profile=profile,
            preflight_receipt=preflight_receipt,
            approval_receipt=approval_path,
            results_root=results_root,
            repository=repository,
            build_directory=build_directory,
            build_provenance_path=build_provenance_path,
            minimum_free_bytes=args.minimum_free_bytes,
        )
        _emit(
            {
                "run_directory": str(results_root),
                "pair_complete": all(
                    verdict != "INCOMPLETE" for _attempt, verdict in attempts
                ),
                "attempts": [
                    {
                        "attempt_directory": str(attempt),
                        "verdict": verdict,
                    }
                    for attempt, verdict in attempts
                ],
                "campaign_member": False,
                "denominator_contribution": 0,
                "figure_eligible": False,
            }
        )
        return 0 if all(
            verdict != "INCOMPLETE" for _attempt, verdict in attempts
        ) else 1
    except (
        OSError,
        ValueError,
        shakedown.N31LivenessShakedownError,
    ) as error:
        _emit({"verdict": "INCOMPLETE", "reason": str(error)}, error=True)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
