"""Prospective W18: four excluded pilots, six counterbalanced four-cell blocks."""
from datetime import datetime, timezone
from pathlib import Path
import subprocess

from . import operator_capacity_v3_cluster as cluster
from . import operator_capacity_v3_cluster_prepare as prepare
from . import operator_capacity_v3_cluster_validation as validation
from . import operator_capacity_v3_cluster_timing as timing
from . import cpu_quota_calibration as calibration


HS = ("heterogeneous", "sham"); HT = ("heterogeneous", "treatment")
OS = ("homogeneous", "sham"); OT = ("homogeneous", "treatment")
SCHEDULE = ((HS, HT, OS, OT), (HT, HS, OT, OS), (OS, OT, HS, HT),
            (OT, OS, HT, HS), (OT, OS, HS, HT), (HT, HS, OS, OT))


def design():
    return {"kind": "kauri-w18-cluster-prospective-design-v1", "N": 31, "Q": 21,
        "tree_count": 21, "fanout": 5, "pipeline_stretch": 2,
        "heterogeneous_quota_percent": {"0..5": 25, "6..30": 100},
        "homogeneous_quota_percent": {"0..5": 100, "6..30": 100},
        "capacity_labels": "trusted predeclared IDs; unchanged across physical regimes",
        "excluded_pilots": [list(x) for x in (HS, HT, OS, OT)],
        "matched_schedule": [[list(x) for x in block] for block in SCHEDULE],
        "shared_inputs_per_block": ["31 BLS keys", "32 TLS identities", "epoch issuer", "label issuer", "capacity snapshot"],
        "snapshot_validity_seconds": 1800, "hard_scope_seconds": 300, "wrapper_timeout_seconds": 315,
        "replica_cfs_quota_period_usec": cluster.REPLICA_CFS_PERIOD_US,
        "cluster_timing_profile": timing.expected_profile(),
        "booking_policy": "each cell within one exact event; campaign across verified adjacent exclusive events",
        "baseline_decision_deadline_seconds": 240, "convergence_seconds": timing.convergence_seconds(timing.expected_profile()),
        "readiness_required": 31, "readiness_delivery_attempts": 1, "activation_delay_blocks": 5,
        "metric": "all31 same-window one-command common committed blocks",
        "metric_start": "maximum of all31 signed E1 activation times", "metric_seconds": 30,
        "calibration": {"slow_percent": 25, "fast_percent": 100, "run_seconds": 30,
            "measurement_seconds": 20, "minimum_cpu_service_ratio": 2.0},
        "heterogeneous_improvement_gate": {"minimum_pair_ratio": 1.10, "positive_blocks": 5,
            "minimum_positive_each_arm_order": 2, "minimum_aggregate_ratio": 1.10},
        "interaction_gate": {"ratio": "(heterogeneous treatment/sham)/(homogeneous treatment/sham)",
            "minimum_block_ratio": 1.10, "positive_blocks": 5, "minimum_aggregate_ratio": 1.10},
        "stop_at_first_failure": True, "automatic_retries": 0, "external_client_throughput": False,
        "claim_eligible": False, "figure_eligible": False}


def run_cell(session, *, ordinal, cell, shared, repo, build_receipt, booking_id, reference, pilot=False):
    name = ("p" if pilot else "c") + f"{ordinal:02d}"
    regime, arm = cell
    run_id = session.name + "-" + name
    root = session / ("pilots" if pilot else "cells") / name
    frozen, _ = cluster.read(session / "freeze.json")
    admission = cluster.admit_cell_booking(frozen["booking_ids"])
    booking_id = admission["booking_id"]
    arguments = prepare.prepare(root, shared_path=shared, private=session / "inputs" / name,
        run_id=run_id, arm=arm, regime=regime, repo=repo, build_receipt=build_receipt,
        booking_id=booking_id, approval_reference=reference)
    # Keep runtime absent until the bounded child verifies fresh outputs.
    # The root-level receipt is included in the sealed raw inventory.
    cluster.write(root / "session-booking-admission.json", admission)
    cluster.execute(**arguments)
    terminal_sha = cluster.sha((root / validation.TERMINAL).read_bytes())
    result = validation.replay(root, expected_terminal_sha256=terminal_sha)
    item = {"ordinal": ordinal, "root": str(root), "run_id": run_id,
        "physical_regime": regime, "arm": arm, "terminal_sha256": terminal_sha,
        "count": result["complete_common_commit_count"], "public_identity_fingerprint": result["public_identity_fingerprint"],
        "capacity_digest": result["consumption_chain"]["approved_capacity_digest"]
            if "approved_capacity_digest" in result["consumption_chain"] else
            cluster.read(root / "raw/consumption.json")[0]["capacity_digest"]}
    cluster.write(session / ("pilot-" if pilot else "cell-") / (name + ".json"), item)
    print("VALIDATED " + name + " " + regime + " " + arm + " " + str(item["count"]), flush=True)
    return item


def evaluate(session):
    frozen, _ = cluster.read(session / "freeze.json")
    build, build_raw = cluster.read(Path(frozen["build_receipt_path"]))
    if (frozen["design"] != design() or cluster.sha(build_raw) != frozen["build_receipt_sha256"] or
            frozen["repository_revision"] != build["repository_revision"]):
        raise cluster.ClusterError("W18 prospective design/build freeze changed")
    coverage = cluster.booking_coverage(frozen["booking_stdout"],
        datetime.fromisoformat(frozen["booking_observed_utc"]), reserve_s=9500)
    if (coverage != frozen["booking_coverage"] or
            frozen["booking_ids"] != [row[0] for row in coverage["booking_rows"]]):
        raise cluster.ClusterError("frozen reservation coverage changed")
    pilots, _ = cluster.read(session / "accepted-excluded-pilots.json")
    if len(pilots["pilots"]) != 4:
        raise cluster.ClusterError("W18 requires four accepted excluded pilots")
    for item, cell in zip(pilots["pilots"], (HS, HT, OS, OT)):
        proof = validation.replay(Path(item["root"]), expected_terminal_sha256=item["terminal_sha256"])
        verify_cell_booking(Path(item["root"]), frozen["booking_ids"])
        if (proof["physical_regime"], proof["arm"]) != cell:
            raise cluster.ClusterError("excluded pilot differs from frozen arm/regime")
    rows = []; totals = {HS: 0, HT: 0, OS: 0, OT: 0}; orders = {"sham,treatment": 0, "treatment,sham": 0}
    for block_number, block in enumerate(SCHEDULE, 1):
        items = []
        for j, cell in enumerate(block):
            ordinal = (block_number - 1) * 4 + j + 1
            item, _ = cluster.read(session / "cell-" / f"c{ordinal:02d}.json")
            result = validation.replay(Path(item["root"]), expected_terminal_sha256=item["terminal_sha256"])
            verify_cell_booking(Path(item["root"]), frozen["booking_ids"])
            if (result["repository_revision"] != frozen["repository_revision"] or
                    (result["physical_regime"], result["arm"]) != cell or
                    result["complete_common_commit_count"] != item["count"] or
                    type(item["count"]) is not int or item["count"] <= 0 or
                    item["ordinal"] != ordinal or item["run_id"] != session.name + f"-c{ordinal:02d}"):
                raise cluster.ClusterError("matched W18 cell differs from exact frozen schedule/source/count")
            items.append(item); totals[cell] += item["count"]
        if len({i["public_identity_fingerprint"] for i in items}) != 1 or len({i["capacity_digest"] for i in items}) != 1:
            raise cluster.ClusterError("W18 block did not share identical keys and capacity snapshot")
        counts = {(item["physical_regime"], item["arm"]): item["count"] for item in items}
        hratio = counts[HT] / counts[HS]; oratio = counts[OT] / counts[OS]
        hpositive = counts[HT] * 10 >= counts[HS] * 11
        ipositive = counts[HT] * counts[OS] * 10 >= counts[HS] * counts[OT] * 11
        order = ",".join(arm for regime, arm in block if regime == "heterogeneous")
        orders[order] += int(hpositive)
        rows.append({"block": block_number, "counts": {regime + "," + arm: count for (regime, arm), count in counts.items()},
            "heterogeneous_ratio": hratio, "homogeneous_ratio": oratio, "interaction_ratio": hratio / oratio,
            "heterogeneous_positive": hpositive, "interaction_positive": ipositive})
    hp = (sum(r["heterogeneous_positive"] for r in rows) >= 5 and min(orders.values()) >= 2 and
          totals[HT] * 10 >= totals[HS] * 11)
    ip = (sum(r["interaction_positive"] for r in rows) >= 5 and
          totals[HT] * totals[OS] * 10 >= totals[HS] * totals[OT] * 11)
    return {"kind": "kauri-w18-cluster-campaign-result-v1", "verdict": "COMPLETE_VALIDATED",
        "heterogeneous_improvement": "VALIDATED" if hp else "HYPOTHESIS_REJECTED",
        "capacity_interaction": "VALIDATED" if ip else "HYPOTHESIS_REJECTED", "blocks": rows,
        "aggregate_heterogeneous_ratio": totals[HT] / totals[HS],
        "aggregate_homogeneous_ratio": totals[OT] / totals[OS],
        "aggregate_interaction_ratio": (totals[HT] / totals[HS]) / (totals[OT] / totals[OS]),
        "positive_heterogeneous_by_order": orders, "claim_eligible": False, "figure_eligible": False,
        "external_client_throughput": False}


def verify_cell_booking(root, allowed_ids):
    admission, _ = cluster.read(root / "session-booking-admission.json")
    request, _ = cluster.read(root / "runtime/cluster-request.json")
    if (admission["booking_id"] not in allowed_ids or
            admission["booking_id"] != request["booking_id"] or
            admission["preparation_and_cell_reserve_seconds"] != 420 or
            admission["waited_before_admission_seconds"] < 0):
        raise cluster.ClusterError("cell reservation differs from its frozen admission")
    cluster.booking_row(admission["booking_stdout"], admission["booking_id"],
        datetime.fromisoformat(admission["observed_utc"]), reserve_s=420)


def run(session, *, repo, build_receipt, booking_id, reference):
    if session.exists() or session != session.resolve() or not reference:
        raise cluster.ClusterError("W18 session must be a fresh canonical authorized root")
    build, build_raw = cluster.read(build_receipt)
    cluster.require_no_owned_native()
    booking_stdout = cluster.booking_listing()
    booking_observed = datetime.now(timezone.utc)
    coverage = cluster.booking_coverage(booking_stdout, booking_observed, reserve_s=9500)
    session.mkdir(mode=0o700)
    for name in ("pilots", "cells", "inputs", "shared", "pilot-", "cell-"):
        (session / name).mkdir(mode=0o700)
    frozen = {"design": design(), "build_receipt_path": str(build_receipt),
        "build_receipt_sha256": cluster.sha(build_raw), "repository_revision": build["repository_revision"],
        "booking_id": booking_id, "approval_reference": reference,
        "booking_stdout": booking_stdout, "booking_observed_utc": booking_observed.isoformat(),
        "booking_coverage": coverage, "booking_ids": [row[0] for row in coverage["booking_rows"]],
        "frozen_before_first_pilot_utc": datetime.now(timezone.utc).isoformat()}
    cluster.write(session / "freeze.json", frozen)
    try:
        receipt = calibration.run_calibration(calibration.CalibrationPlan(), output_root=session / "calibration",
            run_id=session.name + "-calibration", command_builder=cluster.quota_calibration_command)
        if receipt.get("verdict") != "PASS":
            raise cluster.ClusterError("physical CPU quota service calibration rejected")
        pilots = [run_cell(session, ordinal=i + 1, cell=cell, shared=session / "shared/pilots.json",
            repo=repo, build_receipt=build_receipt, booking_id=booking_id, reference=reference, pilot=True)
            for i, cell in enumerate((HS, HT, OS, OT))]
        cluster.write(session / "accepted-excluded-pilots.json", {"pilots": pilots})
        for block_number, block in enumerate(SCHEDULE, 1):
            for j, cell in enumerate(block):
                run_cell(session, ordinal=(block_number - 1) * 4 + j + 1, cell=cell,
                    shared=session / "shared" / f"block{block_number:02d}.json", repo=repo,
                    build_receipt=build_receipt, booking_id=booking_id, reference=reference)
        result = evaluate(session)
        cluster.write(session / "result.json", result)
        return result
    except BaseException as exc:
        cluster.write(session / "session-abort.json", {"state": "ABORTED_NO_RETRY", "failure": str(exc),
            "claim_eligible": False, "figure_eligible": False})
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    for name in ("root", "repo", "build"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("booking", "reference"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    run(args.root, repo=args.repo, build_receipt=args.build, booking_id=args.booking, reference=args.reference)
