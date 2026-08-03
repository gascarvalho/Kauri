#!/usr/bin/env python3
"""Emit and independently validate the frozen LIVE22A gate artifact."""

from __future__ import annotations

from kauri_experiment.live_planner_witness import (
    build_live_planner_witness,
    canonical_live_planner_witness_json,
)
from kauri_experiment.live_planner_witness_oracle import (
    parse_and_validate_live_planner_witness,
)


def main() -> int:
    payload = canonical_live_planner_witness_json(build_live_planner_witness())
    report = parse_and_validate_live_planner_witness(payload)
    print(payload)
    print(f"oracle {report['status']}: verdict {report['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
