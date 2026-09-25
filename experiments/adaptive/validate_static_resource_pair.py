#!/usr/bin/env python3
"""Validate sealed static-resource S4 evidence without launching anything."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from experiments.adaptive.kauri_experiment.static_resource_validation import StaticResourceValidationError, validate_pair

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--pair-root", required=True, type=Path)
args = parser.parse_args()
try:
    print(json.dumps(validate_pair(args.pair_root), sort_keys=True))
except StaticResourceValidationError as exc:
    print(f"validation incomplete: {exc}", file=sys.stderr)
    raise SystemExit(2)
