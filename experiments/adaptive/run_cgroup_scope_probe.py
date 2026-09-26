#!/usr/bin/env python3
"""Run one disposable W16 cgroup-ownership probe under an outer watchdog."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from kauri_experiment.cgroup_scope_probe import CgroupScopeProbe, CgroupScopeProbeError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.exists() or not output.parent.is_dir():
        parser.error("output must be a fresh file beneath an existing directory")
    try:
        receipt = CgroupScopeProbe().run()
        verdict = "PASS" if receipt.completed and receipt.ownership_verified else "ABORT"
        exit_code = 0 if verdict == "PASS" else 1
    except CgroupScopeProbeError as exc:
        receipt = exc.receipt
        verdict = "ABORT"
        exit_code = 1
    document = {"verdict": verdict, **asdict(receipt)}
    with output.open("x", encoding="utf-8") as destination:
        json.dump(document, destination, sort_keys=True, separators=(",", ":"))
        destination.write("\n")
    print(json.dumps({"verdict": verdict, "receipt": str(output)}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
