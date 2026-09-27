#!/usr/bin/env python3
"""Read-only validation of a prospective 24-cell W16 CPU campaign."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kauri_experiment.w16_campaign_validator import validate_w16_cpu_campaign


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--block", type=Path, nargs=4, action="append", required=True,
        metavar=("CELL_1", "CELL_2", "CELL_3", "CELL_4"),
        help="repeat exactly six times in frozen F/R/F/R/F/R order",
    )
    args = parser.parse_args()
    result = validate_w16_cpu_campaign(args.block)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[str(result["verdict"])]


if __name__ == "__main__":
    raise SystemExit(main())
