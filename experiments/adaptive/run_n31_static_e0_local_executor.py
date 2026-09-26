#!/usr/bin/env python3
"""Run one bounded CPU-free local W16 feasibility attempt, or inspect inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from kauri_experiment import n31_static_e0_feasibility as feasibility
from kauri_experiment import n31_static_e0_local_executor as executor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--arm", choices=("slow-roots", "fast-roots"), required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--treegen", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hard-timeout-s", type=float, default=180.0)
    args = parser.parse_args(argv)
    try:
        preflight = json.loads(args.preflight.read_text(encoding="utf-8"))
        plan = feasibility.frozen_plan(arm=args.arm)
        if args.command == "run":
            if args.output is None or args.treegen is not None or args.run_id is not None:
                parser.error("run requires --output and forbids --treegen/--run-id")
            result = executor.execute_once(
                plan=plan, preflight=preflight, directory=args.output,
                hard_timeout_s=args.hard_timeout_s,
            )
            print(json.dumps(result, sort_keys=True))
            return 0 if result["verdict"] == "PASS" else 1
        if args.treegen is None or args.run_id is None or args.output is not None:
            parser.error("prepare requires --treegen/--run-id and forbids --output")
        prepared = executor.prepare_launch(
            plan=plan, preflight=preflight, treegen_path=args.treegen,
            run_id=args.run_id, hard_timeout_s=args.hard_timeout_s,
        )
    except (OSError, ValueError, executor.LocalExecutorError) as error:
        print(json.dumps({"verdict": "REJECT_NO_EXECUTION", "error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({"verdict": "PREPARED_NO_EXECUTION", "run_id": prepared.run_id,
                      "epoch_zero_digest": prepared.epoch_zero_digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
