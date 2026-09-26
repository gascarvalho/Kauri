#!/usr/bin/env python3
"""Run only the no-launch local preflight for static N31 Epoch-0 feasibility."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from kauri_experiment import n31_static_e0_feasibility as feasibility


REPOSITORY = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight",))
    parser.add_argument("--arm", choices=("slow-roots", "fast-roots"), required=True)
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument("--app-binary", type=Path, default=REPOSITORY / "build-adaptive/examples/hotstuff-app")
    parser.add_argument("--keygen-binary", type=Path, default=REPOSITORY / "build-adaptive/hotstuff-keygen")
    parser.add_argument("--tls-keygen-binary", type=Path, default=REPOSITORY / "build-adaptive/hotstuff-tls-keygen")
    parser.add_argument("--native-digest-binary", type=Path, default=REPOSITORY / "build-adaptive/examples/static-epoch0-digest")
    args = parser.parse_args(argv)
    try:
        receipt = feasibility.preflight(
            repository=args.repository,
            app_binary=args.app_binary,
            keygen_binary=args.keygen_binary,
            tls_keygen_binary=args.tls_keygen_binary,
            native_digest_binary=args.native_digest_binary,
            arm=args.arm,
        )
    except (OSError, ValueError, feasibility.StaticE0FeasibilityError) as error:
        print(json.dumps({"verdict": "REJECT", "error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    sys.stdout.buffer.write(feasibility.canonical_json(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
