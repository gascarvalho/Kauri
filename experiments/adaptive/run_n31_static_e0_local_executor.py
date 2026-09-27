#!/usr/bin/env python3
"""Run one bounded W16 static-E0 feasibility cell or inspect inputs."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
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
_W16_REVERSE_ORDER = tuple(reversed(_W16_BLOCK_ORDER))
_CAMPAIGN_ID = re.compile(r"w16-cpu-repeat-[a-z0-9][a-z0-9-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _read_campaign_cpu_authorization(
    payload: bytes, document: dict[str, object], *, preflight_bytes: bytes,
    preflight: dict[str, object], arm: str, quota_mode: str, output: Path,
    hard_timeout_s: float, campaign_freeze_file: Path | None,
    campaign_approval_ref: str | None,
) -> bytes:
    expected_keys = {
        "schema_version", "kind", "campaign_id", "block_index",
        "campaign_freeze_sha256", "block_id", "block_order", "cell_ordinal",
        "revision", "profile_sha256", "arm", "quota_mode", "preflight_sha256",
        "binary_sha256", "output_root", "required_complete_cycles",
        "hard_timeout_s", "external_timeout_s", "automatic_retries",
        "claim_eligible", "figure_eligible", "approval_ref", "approved_at_utc",
    }
    block_index = document.get("block_index")
    campaign_id = document.get("campaign_id")
    order = (
        _W16_BLOCK_ORDER if type(block_index) is int and block_index % 2 == 1
        else _W16_REVERSE_ORDER
    )
    cell = f"{arm}:{quota_mode}"
    approval_time = document.get("approved_at_utc")
    try:
        valid_approval_time = (
            isinstance(approval_time, str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", approval_time)
            is not None
            and datetime.strptime(approval_time, "%Y-%m-%dT%H:%M:%SZ")
            .strftime("%Y-%m-%dT%H:%M:%SZ") == approval_time
        )
    except ValueError:
        valid_approval_time = False
    freeze_bytes = None
    if campaign_freeze_file is not None and not campaign_freeze_file.is_symlink():
        if campaign_freeze_file.is_file():
            freeze_bytes = campaign_freeze_file.read_bytes()
    if (
        set(document) != expected_keys
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != 2
        or document.get("kind") != "kauri-w16-static-e0-campaign-authorization-v2"
        or not isinstance(campaign_id, str)
        or _CAMPAIGN_ID.fullmatch(campaign_id) is None
        or type(block_index) is not int or block_index not in range(1, 7)
        or document.get("block_id") != f"{campaign_id}-block-{block_index:02d}"
        or not isinstance(document.get("campaign_freeze_sha256"), str)
        or _SHA256.fullmatch(str(document["campaign_freeze_sha256"])) is None
        or quota_mode not in ("homogeneous", "heterogeneous")
        or document.get("block_order") != list(order)
        or type(document.get("cell_ordinal")) is not int
        or document.get("cell_ordinal") != order.index(cell) + 1
        or document.get("revision") != preflight.get("revision")
        or document.get("profile_sha256") != preflight.get("profile_sha256")
        or document.get("arm") != arm
        or document.get("quota_mode") != quota_mode
        or document.get("preflight_sha256") != hashlib.sha256(preflight_bytes).hexdigest()
        or document.get("binary_sha256") != preflight.get("binary_sha256")
        or document.get("output_root") != str(output.resolve())
        or type(document.get("required_complete_cycles")) is not int
        or document.get("required_complete_cycles") != 5
        or hard_timeout_s != 480
        or type(document.get("hard_timeout_s")) is not int
        or document.get("hard_timeout_s") != 480
        or type(document.get("external_timeout_s")) is not int
        or document.get("external_timeout_s") != 720
        or type(document.get("automatic_retries")) is not int
        or document.get("automatic_retries") != 0
        or document.get("claim_eligible") is not False
        or document.get("figure_eligible") is not False
        or not isinstance(document.get("approval_ref"), str)
        or not document["approval_ref"]
        or campaign_approval_ref is None
        or document["approval_ref"] != campaign_approval_ref
        or freeze_bytes is None
        or hashlib.sha256(freeze_bytes).hexdigest()
        != document.get("campaign_freeze_sha256")
        or not valid_approval_time
    ):
        raise executor.LocalExecutorError("W16 campaign CPU authorization does not bind this exact cell")
    return payload


def _read_cpu_authorization(
    path: Path, *, preflight_bytes: bytes, preflight: dict[str, object],
    arm: str, quota_mode: str, output: Path, hard_timeout_s: float,
    campaign_freeze_file: Path | None = None,
    campaign_approval_ref: str | None = None,
) -> bytes:
    payload = path.read_bytes()
    document = json.loads(payload)
    if isinstance(document, dict) and document.get("schema_version") == 2:
        if path.is_symlink():
            raise executor.LocalExecutorError("W16 campaign CPU authorization is a symlink")
        return _read_campaign_cpu_authorization(
            payload, document, preflight_bytes=preflight_bytes,
            preflight=preflight, arm=arm, quota_mode=quota_mode,
            output=output, hard_timeout_s=hard_timeout_s,
            campaign_freeze_file=campaign_freeze_file,
            campaign_approval_ref=campaign_approval_ref,
        )
    if campaign_freeze_file is not None or campaign_approval_ref is not None:
        raise executor.LocalExecutorError("W16 v1 CPU authorization forbids campaign inputs")
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
        or hard_timeout_s != 480
        or document["hard_timeout_s"] != 480
        or document["external_timeout_s"] != 720
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
    parser.add_argument("--campaign-freeze-file", type=Path)
    parser.add_argument("--campaign-approval-ref")
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
                    campaign_freeze_file=args.campaign_freeze_file,
                    campaign_approval_ref=args.campaign_approval_ref,
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
                campaign_authorization_validated=(
                    args.campaign_freeze_file is not None
                ),
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
        if args.campaign_freeze_file is not None or args.campaign_approval_ref is not None:
            parser.error("prepare forbids campaign run authorization")
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
