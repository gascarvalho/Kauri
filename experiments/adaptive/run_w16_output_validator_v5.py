#!/usr/bin/env python3
"""Read-only validation of one prospective W16 CPU campaign cell."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kauri_experiment.w16_output_validator import validate_w16_output_v5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    result = validate_w16_output_v5(args.root)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[str(result["verdict"])]


if __name__ == "__main__":
    raise SystemExit(main())
