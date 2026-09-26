#!/usr/bin/env python3
"""Validate one ordered W16 four-cell exploratory block without modifying it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kauri_experiment.w16_block_validator import validate_w16_block


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--roots", type=Path, nargs=4, required=True,
        metavar=("A_HOMOGENEOUS", "B_HOMOGENEOUS", "A_HETEROGENEOUS", "B_HETEROGENEOUS"),
        help="exactly four sealed roots in frozen A-H, B-H, A-X, B-X order",
    )
    args = parser.parse_args()
    result = validate_w16_block(args.roots)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[str(result["verdict"])]


if __name__ == "__main__":
    raise SystemExit(main())
