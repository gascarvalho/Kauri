#!/usr/bin/env python3
"""No-crash N=31 CPU sham/adaptive study gate (execution intentionally blocked).

``preflight`` is read-only.  The later CPU live probe requires a separate,
exact authorization request before it may invoke systemd-run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.adaptive.kauri_experiment import cpu_quota, static_resource_pair


_PROFILES = Path(__file__).resolve().parent / "profiles"
_PROFILE = _PROFILES / "n31-static-resource-cpu-sham-v1.json"
_CONTRACT = _PROFILES / "n31-static-resource-cpu-sham-quota-v1.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--profile", type=Path, default=_PROFILE)
    preflight.add_argument("--cpu-quota-contract", type=Path, default=_CONTRACT)
    preflight.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("execute")
    execute.add_argument("--profile", type=Path, default=_PROFILE)
    execute.add_argument("--cpu-quota-contract", type=Path, default=_CONTRACT)
    execute.add_argument("--preflight-receipt", type=Path, required=True)
    execute.add_argument("--authorization-receipt", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    return parser


def _read_json(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise static_resource_pair.StaticResourcePairError("receipt is invalid JSON") from exc
    if not isinstance(document, dict):
        raise static_resource_pair.StaticResourcePairError("receipt must contain an object")
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "preflight":
            result = static_resource_pair.prepare_preflight(
                profile_path=arguments.profile,
                contract_path=arguments.cpu_quota_contract,
                output_root=arguments.output,
            )
        else:
            preflight = _read_json(arguments.preflight_receipt)
            request = static_resource_pair.build_authorization_request(
                profile=static_resource_pair.load_profile(arguments.profile),
                contract=static_resource_pair.load_cpu_contract(
                    arguments.cpu_quota_contract, profile_path=arguments.profile
                ),
                output_root=arguments.output,
            )
            static_resource_pair.verify_authorization_receipt(
                request, _read_json(arguments.authorization_receipt)
            )
            static_resource_pair.execution_not_implemented()
    except (static_resource_pair.StaticResourcePairError, cpu_quota.CpuQuotaContractError) as exc:
        parser.error(str(exc))
    sys.stdout.write(json.dumps(result, allow_nan=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
