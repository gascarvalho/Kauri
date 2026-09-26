#!/usr/bin/env python3
"""Run one bounded W16 static-E0 feasibility cell or inspect inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from kauri_experiment import n31_static_e0_feasibility as feasibility
from kauri_experiment import n31_static_e0_local_executor as executor
from kauri_experiment import static_e0_cpu_contract


_W16_BLOCK_ORDER = (
    "slow-roots:homogeneous",
    "fast-roots:homogeneous",
    "slow-roots:heterogeneous",
    "fast-roots:heterogeneous",
)


def _read_cpu_authorization(
    path: Path, *, preflight_bytes: bytes, preflight: dict[str, object],
    arm: str, quota_mode: str, output: Path, hard_timeout_s: float,
) -> bytes:
    payload = path.read_bytes()
    document = json.loads(payload)
    expected_keys = {
        "schema_version", "kind", "block_id", "block_order", "cell_ordinal",
        "revision", "profile_sha256", "arm", "quota_mode", "preflight_sha256",
        "binary_sha256", "output_root", "required_complete_cycles",
        "hard_timeout_s", "external_timeout_s", "automatic_retries",
        "claim_eligible", "figure_eligible", "approval_ref", "approved_at_utc",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise executor.LocalExecutorError("W16 CPU authorization schema drifted")
    cell = f"{arm}:{quota_mode}"
    if (
        document["schema_version"] != 1
        or document["kind"] != "kauri-w16-static-e0-exploratory-authorization-v1"
        or not isinstance(document["block_id"], str)
        or not document["block_id"].startswith("w16-static-e0-")
        or document["block_order"] != list(_W16_BLOCK_ORDER)
        or document["cell_ordinal"] != _W16_BLOCK_ORDER.index(cell) + 1
        or document["revision"] != preflight.get("revision")
        or document["profile_sha256"] != preflight.get("profile_sha256")
        or document["arm"] != arm
        or document["quota_mode"] != quota_mode
        or document["preflight_sha256"] != hashlib.sha256(preflight_bytes).hexdigest()
        or document["binary_sha256"] != preflight.get("binary_sha256")
        or document["output_root"] != str(output.resolve())
        or document["required_complete_cycles"] != 5
        or document["hard_timeout_s"] != hard_timeout_s
        or document["external_timeout_s"] != 540
        or document["automatic_retries"] != 0
        or document["claim_eligible"] is not False
        or document["figure_eligible"] is not False
        or document["approval_ref"] != "user-confirmation:2026-09-26:inesc-cpu-throughput"
        or not isinstance(document["approved_at_utc"], str)
        or not document["approved_at_utc"].endswith("Z")
    ):
        raise executor.LocalExecutorError("W16 CPU authorization does not bind this exact cell")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--arm", choices=("slow-roots", "fast-roots"), required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--treegen", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hard-timeout-s", type=float, default=180.0)
    parser.add_argument("--quota-mode", choices=("none", "heterogeneous", "homogeneous"), default="none")
    args = parser.parse_args(argv)
    try:
        preflight_bytes = args.preflight.read_bytes()
        preflight = json.loads(preflight_bytes)
        plan = feasibility.frozen_plan(arm=args.arm)
        if args.command == "run":
            if args.output is None or args.treegen is not None or args.run_id is not None:
                parser.error("run requires --output and forbids --treegen/--run-id")
            if args.quota_mode != "none" and args.authorization is None:
                parser.error("CPU run requires --authorization")
            if args.quota_mode == "none" and args.authorization is not None:
                parser.error("CPU-free run forbids --authorization")
            authorization_bytes = (
                _read_cpu_authorization(
                    args.authorization, preflight_bytes=preflight_bytes,
                    preflight=preflight, arm=args.arm, quota_mode=args.quota_mode,
                    output=args.output, hard_timeout_s=args.hard_timeout_s,
                ) if args.authorization is not None else None
            )
            result = executor.execute_once(
                plan=plan, preflight=preflight, directory=args.output,
                hard_timeout_s=args.hard_timeout_s,
                authorization_bytes=authorization_bytes,
                quota_contract=(
                    static_e0_cpu_contract.frozen_contract(plan, args.quota_mode)
                    if args.quota_mode != "none" else None
                ),
                required_complete_cycles=(5 if args.quota_mode != "none" else 1),
            )
            outcome_file = (
                "feasibility-receipt.json" if result["verdict"] == "PASS"
                else "feasibility-abort.json"
            )
            print(json.dumps({
                "verdict": result["verdict"], "run_id": result["run_id"],
                "failure": result["failure"],
                "outcome": str(args.output.resolve() / outcome_file),
            }, sort_keys=True))
            return 0 if result["verdict"] == "PASS" else 1
        if args.treegen is None or args.run_id is None or args.output is not None or args.authorization is not None:
            parser.error("prepare requires --treegen/--run-id and forbids --output")
        if args.quota_mode != "none":
            parser.error("prepare is CPU-free; quota mode is only for run")
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
