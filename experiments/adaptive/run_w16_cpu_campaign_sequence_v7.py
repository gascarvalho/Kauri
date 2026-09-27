#!/usr/bin/env python3
"""Prepare or execute the fresh, no-retry W16 v7 CPU campaign."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kauri_experiment.w16_cpu_campaign_sequence_v7 import (
    execute_sequence_v7,
    prepare_sequence_v7,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="write v7 inputs; launch no cells")
    prepare.add_argument("--freeze-file", type=Path, required=True)
    prepare.add_argument("--approval-ref", required=True)
    prepare.add_argument("--approved-at-utc", required=True)
    run = commands.add_parser("run", help="execute one exact approved v7 manifest")
    run.add_argument("--freeze-file", type=Path, required=True)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--manifest-sha256", required=True)
    run.add_argument("--approval-ref", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_sequence_v7(args.freeze_file, approval_ref=args.approval_ref,
                                     approved_at_utc=args.approved_at_utc)
        success = result.get("verdict") == "PREPARED_NO_EXECUTION"
    else:
        result = execute_sequence_v7(args.manifest, manifest_sha256=args.manifest_sha256,
                                     freeze_file=args.freeze_file,
                                     approval_ref=args.approval_ref)
        success = result.get("verdict") == "PASS"
    print(json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
