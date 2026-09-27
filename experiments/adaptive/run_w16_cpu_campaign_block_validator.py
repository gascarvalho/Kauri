#!/usr/bin/env python3
"""Read-only validation of one prospective counterbalanced W16 CPU block."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kauri_experiment.w16_campaign_validator import validate_w16_campaign_block


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", type=Path, nargs=4, required=True)
    args = parser.parse_args()
    result = validate_w16_campaign_block(args.roots)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[str(result["verdict"])]


if __name__ == "__main__":
    raise SystemExit(main())
