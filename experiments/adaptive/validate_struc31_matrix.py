#!/usr/bin/env python3
"""Fail-closed CLI for the STRUC31 structural matrix."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from kauri_experiment.struc31_validation import Struc31ValidationError, validate_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--repository", type=Path,
                        default=Path(__file__).resolve().parents[2])
    parser.add_argument("--producer-binary", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        validate_file(str(arguments.artifact), repository=arguments.repository,
                      producer_binary=arguments.producer_binary)
    except (OSError, ValueError, Struc31ValidationError) as error:
        print(f"INVALID: {error}", file=sys.stderr)
        return 2
    print("PASS struc31-placement-matrix-v1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
