#!/usr/bin/env python3
"""No-launch post-run W18 authority and raw-validation command."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

try:
    from .kauri_experiment import operator_capacity_v3_validation_bridge as bridge
except ImportError:
    from kauri_experiment import operator_capacity_v3_validation_bridge as bridge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--authority-output", required=True, type=Path)
    parser.add_argument("--raw-validation-output", required=True, type=Path)
    parser.add_argument("--stage-a-verifier-binary", required=True, type=Path)
    parser.add_argument("--stage-b-verifier-binary", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = bridge.validate_completed_arm(
            args.root, authority_output=args.authority_output,
            raw_validation_output=args.raw_validation_output,
            stage_a_verifier_binary=args.stage_a_verifier_binary,
            stage_b_verifier_binary=args.stage_b_verifier_binary)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("verdict") == "COMPLETE_NO_CLAIM" else 1


if __name__ == "__main__":
    raise SystemExit(main())
