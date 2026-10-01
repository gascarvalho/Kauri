#!/usr/bin/env python3
"""Authorized, no-retry v8 session: excluded pilots, then six frozen pairs.

This operator runs only in the exact existing exclusive booking.  It records
the human authorization reference separately from the exact per-cell approval
bytes.  Every child runs under the live module's bounded scope.  Results are
recomputed by reopening pinned terminals and raw archives, including when the
predeclared improvement hypothesis is rejected.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
from pathlib import Path
import subprocess
import sys

from sustained_role_v8_live import (
    ABORT, BOOKING, KAURI, RAW_RECEIPT, TERMINAL, LiveError,
    canonical, clock, execute, inputs, load, paths, read, replay, sha, write,
)

design = load("w19_v8_session_design", "sustained_role_v8_campaign_evaluator.py")


def binary_paths():
    build = KAURI / "build-adaptive"
    return {"app_binary": build / "examples/hotstuff-app",
            "manager_binary": build / "examples/adaptation-manager",
            "keygen_binary": build / "hotstuff-keygen",
            "tls_keygen_binary": build / "hotstuff-tls-keygen",
            "e0_helper_binary": build / "examples/n7-epoch0-treefile-digest"}


def run_cell(root, arm, build_receipt, *, run_id, approval_parent):
    start = clock() + 90_000_000_000
    sealed = inputs.prepare(root, run_id=run_id, ports=(18570, 19570, 20570),
        start_ns=start, end_ns=start + 82_000_000_000, binaries=binary_paths(),
        build_receipt=build_receipt, arm=arm)
    kind = "kauri-n7-sustained-role-v8-adaptive-child-launch-authorization-v1" if arm == "adaptive_e1" else "kauri-n7-sustained-role-v8-fixed-launch-authorization-v1"
    approval = {"schema_version": 1, "kind": kind, "request_sha256": sealed["request_sha256"],
                "plan_sha256": sealed["plan_sha256"], "run_id": run_id, "no_retry": True}
    external = approval_parent / (run_id + ".json")
    write(external, approval)
    execute(root, arm, external, sha(canonical(approval)))
    raw_sha, terminal_sha = sha((root / RAW_RECEIPT).read_bytes()), sha((root / TERMINAL).read_bytes())
    # Reopen after wrapper return; never use the producer's printed count.
    result = replay(root, expected_receipt_sha=raw_sha, expected_terminal_sha=terminal_sha)
    return {"run_id": run_id, "arm": arm, "root": str(root), "raw_receipt_sha256": raw_sha,
            "terminal_sha256": terminal_sha, "result": result}


def paired_result(cells):
    if len(cells) != 12 or len({cell["run_id"] for cell in cells}) != 12:
        raise LiveError("matched campaign needs twelve fresh unique cells")
    rows = []; positive_orders = {"fixed_e0,adaptive_e1": 0, "adaptive_e1,fixed_e0": 0}
    total_fixed = total_adaptive = 0
    for pair, order in enumerate(design.FROZEN_PAIR_SCHEDULE, 1):
        selected = cells[(pair - 1) * 2:pair * 2]
        if tuple(cell["arm"] for cell in selected) != order:
            raise LiveError("matched pair order differs from frozen AB/BA schedule")
        counts = {cell["arm"]: cell["result"]["common_committed_blocks"] for cell in selected}
        fixed, adaptive = counts["fixed_e0"], counts["adaptive_e1"]
        if type(fixed) is not int or type(adaptive) is not int or fixed <= 0 or adaptive <= 0:
            raise LiveError("common committed-block counts are invalid")
        positive = adaptive * 10 >= fixed * 11
        positive_orders[",".join(order)] += int(positive)
        total_fixed += fixed; total_adaptive += adaptive
        rows.append({"pair": pair, "order": list(order), "fixed_count": fixed,
                     "adaptive_count": adaptive, "ratio": adaptive / fixed, "positive": positive})
    passed = (sum(row["positive"] for row in rows) >= 5 and min(positive_orders.values()) >= 2 and
              total_adaptive * 10 >= total_fixed * 11)
    return {"verdict": "VALIDATED_IMPROVEMENT" if passed else "VALIDATED_HYPOTHESIS_REJECTED",
            "pairs": rows, "positive_pairs": sum(row["positive"] for row in rows),
            "positive_pairs_by_order": positive_orders,
            "total_fixed": total_fixed, "total_adaptive": total_adaptive,
            "aggregate_ratio": total_adaptive / total_fixed,
            "geometric_mean_pair_ratio": math.exp(sum(math.log(row["ratio"]) for row in rows) / 6),
            "metric_window": "[A+32s,A+72s)", "metric_duration_seconds": 40,
            "estimand": "common committed-block rate under replica-local one-command synthetic driver",
            "external_client_throughput": False}


def run_session(root, build_receipt, approval_reference):
    if root != root.resolve() or root.exists() or not root.is_absolute():
        raise LiveError("session root must be fresh and canonical")
    if not approval_reference.strip():
        raise LiveError("human authorization reference is required")
    revision = inputs.prep.local._require_clean_snapshot(inputs.prep.local.base.verify_repository_state(KAURI))
    build, build_raw = read(build_receipt)
    if build["repository_revision"] != revision:
        raise LiveError("session build revision differs from source")
    root.mkdir(mode=0o700)
    (root / "approvals").mkdir(mode=0o700)
    (root / "pilots").mkdir(mode=0o700)
    (root / "cells").mkdir(mode=0o700)
    session = {"schema_version": 1, "kind": "kauri-w19-v8-authorized-session-v1",
               "repository_revision": revision, "build_receipt_sha256": sha(build_raw),
               "booking_id": BOOKING, "approval_reference": approval_reference,
               "created_utc": datetime.now(timezone.utc).isoformat(), "no_retry": True,
               "pilot_policy": "excluded", "stop_at_first_validation_failure": True}
    write(root / "session.json", session)
    name = root.name
    try:
        pilots = []
        for arm in ("adaptive_e1", "fixed_e0"):
            pilot = run_cell(root / "pilots" / arm, arm, build_receipt,
                             run_id=name + "-pilot-" + arm, approval_parent=root / "approvals")
            pilots.append(pilot)
            write(root / ("pilot-" + arm + ".json"), pilot)
            print("EXCLUDED_PILOT_VALIDATED " + arm, flush=True)
        comparable = {"profile": inputs.prep.v8.materialization_profile(),
                      "build_receipt_sha256": sha(build_raw), "slot": BOOKING,
                      "native_mode": inputs.prep.v8.NATIVE_MODE,
                      "hard_scope_seconds": 210, "prearm_reserve_seconds": 90,
                      "fault_window_seconds": 82, "ports": [18570, 19570, 20570]}
        frozen = design.freeze(design.frozen_design(campaign_id=name, repository_revision=revision,
            comparability_sha256=sha(canonical(comparable)), approval_reference=approval_reference))
        write(root / "comparability.json", comparable); write(root / "freeze.json", frozen)
        manifest = {"freeze_sha256": frozen["freeze_sha256"], "repository_revision": revision,
                    "cells": [{"ordinal": index + 1, "arm": arm, "run_id": name + f"-cell{index + 1:02d}",
                               "root": "cells/" + f"cell{index + 1:02d}"}
                              for index, arm in enumerate(arm for pair in design.FROZEN_PAIR_SCHEDULE for arm in pair)]}
        write(root / "manifest.json", manifest)
        cells = []
        for item in manifest["cells"]:
            cell = run_cell(root / item["root"], item["arm"], build_receipt,
                            run_id=item["run_id"], approval_parent=root / "approvals")
            cells.append(cell)
            write(root / f"cell{item['ordinal']:02d}.json", cell)
            print("MATCHED_CELL_VALIDATED " + str(item["ordinal"]), flush=True)
        result = evaluate_session(root)
        write(root / "result.json", result)
        print(result["verdict"], flush=True)
        return result
    except BaseException as exc:
        write(root / "session-abort.json", {"state": "ABORTED_NO_RETRY", "error_type": type(exc).__name__,
            "reason": str(exc)[:512], "no_retry": True, "claim_eligible": False, "figure_eligible": False})
        raise


def evaluate_session(root):
    if (root / "session-abort.json").exists():
        raise LiveError("stopped session cannot yield a campaign result")
    session, _ = read(root / "session.json"); frozen, _ = read(root / "freeze.json")
    comparable, comparable_raw = read(root / "comparability.json")
    checked_freeze = design._check_freeze(frozen)
    manifest, _ = read(root / "manifest.json")
    if (checked_freeze["comparability_sha256"] != sha(comparable_raw) or
            comparable["profile"] != inputs.prep.v8.materialization_profile() or
            manifest["freeze_sha256"] != checked_freeze["freeze_sha256"] or
            session["repository_revision"] != checked_freeze["repository_revision"]):
        raise LiveError("campaign source/comparability freeze drift")
    cells = []
    for ordinal, order in enumerate(arm for pair in design.FROZEN_PAIR_SCHEDULE for arm in pair):
        cell, _ = read(root / f"cell{ordinal + 1:02d}.json")
        expected = manifest["cells"][ordinal]
        if (cell["arm"] != order or cell["run_id"] != expected["run_id"] or
                Path(cell["root"]) != root / expected["root"]):
            raise LiveError("cell does not bind its frozen manifest slot")
        result = replay(Path(cell["root"]), expected_receipt_sha=cell["raw_receipt_sha256"],
                        expected_terminal_sha=cell["terminal_sha256"])
        if result != cell["result"] or result["repository_revision"] != checked_freeze["repository_revision"]:
            raise LiveError("cell result differs from independent native-byte replay")
        plan, _ = read(Path(cell["root"]) / paths(cell["arm"])[0])
        if plan["artifacts"]["build_receipt"]["sha256"] != session["build_receipt_sha256"]:
            raise LiveError("cell differs from campaign's exact build")
        cells.append(cell)
    return {"schema_version": 1, "kind": "kauri-w19-v8-live-campaign-result-v1",
            "campaign_id": checked_freeze["campaign_id"], "freeze_sha256": checked_freeze["freeze_sha256"],
            "repository_revision": checked_freeze["repository_revision"], **paired_result(cells),
            "claim_eligible": False, "figure_eligible": False,
            "promotion_boundary": "author review and durable raw archive required before thesis inclusion"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "replay"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--build-receipt", type=Path)
    parser.add_argument("--approval-reference")
    args = parser.parse_args()
    result = run_session(args.root, args.build_receipt, args.approval_reference) if args.action == "run" else evaluate_session(args.root)
    print(canonical(result).decode(), end="")


if __name__ == "__main__":
    main()
