#!/usr/bin/env python3
"""Write a read-only placement contrast report for accepted CERT13 evidence."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.adaptive.kauri_experiment.cert13_placement_contrast import (  # noqa: E402
    PlacementContrastError,
    PlacementContrastAbort,
    write_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = write_report(args.campaign_root, args.output_root, command="\x00".join(sys.argv))
    except PlacementContrastAbort as exc:
        print(exc.path, file=sys.stderr)
        return 2
    except PlacementContrastError as exc:
        print(f"audit rejected: {exc}", file=sys.stderr)
        return 2
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
