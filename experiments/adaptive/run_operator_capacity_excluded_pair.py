#!/usr/bin/env python3
"""Plan or reject the excluded local W18 treatment/sham pair; never launch it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.adaptive.kauri_experiment import operator_capacity_excluded_pair as pair


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hard-timeout-s", type=int, default=720)
    for arm in ("sham", "treatment"):
        prefix = arm.replace("-", "_")
        parser.add_argument(f"--{arm}-preflight", type=Path, required=True, dest=f"{prefix}_preflight")
        parser.add_argument(f"--{arm}-request", type=Path, required=True, dest=f"{prefix}_request")
        parser.add_argument(f"--{arm}-approval", type=Path, required=True, dest=f"{prefix}_approval")
        parser.add_argument(f"--{arm}-expected-approval-sha256", required=True, dest=f"{prefix}_approval_sha")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    inputs = {
        arm: {
            "preflight": getattr(args, f"{arm}_preflight"),
            "request": getattr(args, f"{arm}_request"),
            "approval": getattr(args, f"{arm}_approval"),
            "expected_approval_sha256": getattr(args, f"{arm}_approval_sha"),
        }
        for arm in ("sham", "treatment")
    }
    try:
        plan = pair.prepare_excluded_pair(
            output_root=args.output, arm_inputs=inputs, hard_timeout_s=args.hard_timeout_s,
        )
        if args.command == "run":
            pair.execution_not_implemented()
        print(json.dumps(plan, sort_keys=True, separators=(",", ":")))
        return 0
    except pair.OperatorCapacityExcludedPairError as exc:
        print(json.dumps({"verdict": "INCOMPLETE_NO_EXECUTION", "detail": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
