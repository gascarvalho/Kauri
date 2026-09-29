#!/usr/bin/env python3
"""Fail-closed, no-launch contract checks for matched N=7 omission pairs.

This module does not start processes or validate an execution trace.  It
checks the frozen comparison design and the shape of prospective terminal
outcome summaries.  Caller-authored PASS summaries remain planning-only and
ineligible until a future matcher independently reopens hash-bound raw receipt
and verdict files and reruns each arm's source-blind validator.  In particular,
an aborted or incomplete arm is retained rather than converted into a pass or
removed from the denominator.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "matched_manifest.json"
_HEX = frozenset("0123456789abcdef")
_ARM_IDS = frozenset({"A", "C"})
_TERMINAL = frozenset({"PASS", "ABORTED", "INCOMPLETE"})
_EXPECTED_SHARED_INPUTS = {
    "replica_ids": list(range(7)),
    "fault_threshold": 2,
    "quorum": 5,
    "epoch0_tree_sha256": "38a2baa37b7fcec43f5c58423be068d807cc253fedbdc379520a3e42428dffcc",
    "fault_actor": 1,
    "fault_mode": "selective_aggregate_relay_omission",
    "fault_window_basis": "exact_matched_post_arm_physical_omission_v1",
    "measurement_window": "[first_admitted_source_bound_physical_omission,+60000000000ns]",
    "measurement_anchor_source": "replica-1:fault.aggregate_omitted:first_for_context",
    "workload": "must_be_identical_within_pair",
    "hard_timeout_seconds": 600,
}
_EXPECTED_CONTROL = {
    "arm_id": "C",
    "policy": "inherited_fixed_epoch0_no_successor",
    "required_terminal_validator_verdict": "CONTROL_RAW_BUNDLE_VALIDATED_PROSPECTIVE",
    "requires_no_adaptive_successor": True,
}
_EXPECTED_ADAPTIVE = {
    "arm_id": "A",
    "policy": "native_path_timeout_containment",
    "required_terminal_validator_verdict": "RAW_BUNDLE_VALIDATED",
    "requires_selection": [1],
    "requires_successor_activation": True,
}
_EXPECTED_SEEDS = (41719, 41720, 41721)


class ValidationError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    semantic = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    return hashlib.sha256(_canonical(semantic)).hexdigest()


def _hex64(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in _HEX for ch in value):
        raise ValidationError(f"{label} must be a lower-case SHA-256")
    return value


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("manifest is not UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValidationError("manifest is not an object")
    return manifest


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version", "study_id", "state", "claim_boundary", "primary_outcome",
        "shared_inputs", "arms", "pairs", "failure_accounting", "campaign_gate",
        "manifest_sha256",
    }
    if set(manifest) != required or manifest.get("schema_version") != 1:
        raise ValidationError("manifest schema drift")
    if manifest.get("state") != "PLANNING_ONLY_NO_LAUNCH":
        raise ValidationError("manifest must remain planning-only and no-launch")
    if manifest.get("manifest_sha256") != manifest_digest(manifest):
        raise ValidationError("manifest semantic digest does not recompute")
    shared = manifest["shared_inputs"]
    if not isinstance(shared, Mapping) or dict(shared) != _EXPECTED_SHARED_INPUTS:
        raise ValidationError("manifest changes a fixed shared input")
    _hex64(shared.get("epoch0_tree_sha256"), "epoch0 tree hash")
    outcome = manifest["primary_outcome"]
    expected_outcome = {
        "status": "UNBOUND_PROSPECTIVE_DESIGN",
        "prerequisite": "One designated authoritative block.committed event after the frozen fault-window arm, with matching block.commit_observed witnesses from every other replica.",
        "candidate_advantage_outcome": "paired authoritative common-commit count and maximum inter-commit gap over a fixed source-bound post-physical-omission horizon",
        "required_control_boundary": "source-bound shadow boundary corresponding to the adaptive arm E1 activation",
        "throughput": "not_bound_in_planning_scaffold",
    }
    if not isinstance(outcome, Mapping) or dict(outcome) != expected_outcome:
        raise ValidationError("planning scaffold outcome contract drifted")
    arms = manifest["arms"]
    if not isinstance(arms, Mapping) or set(arms) != {"control", "adaptive"}:
        raise ValidationError("manifest must bind exactly control and adaptive arms")
    if dict(arms["control"]) != _EXPECTED_CONTROL:
        raise ValidationError("control policy contract drifted")
    if dict(arms["adaptive"]) != _EXPECTED_ADAPTIVE:
        raise ValidationError("adaptive policy contract drifted")
    pairs = manifest["pairs"]
    if not isinstance(pairs, list) or len(pairs) != 3:
        raise ValidationError("manifest must predeclare exactly three matched pairs")
    orders = []
    for ordinal, pair in enumerate(pairs, 1):
        if (not isinstance(pair, Mapping) or pair.get("pair_id") != f"P{ordinal}" or
                type(pair.get("seed")) is not int or pair["seed"] < 0 or
                pair["seed"] != _EXPECTED_SEEDS[ordinal - 1]):
            raise ValidationError("pair identity or seed drifted")
        order = pair.get("arm_order")
        if not isinstance(order, list) or set(order) != _ARM_IDS or len(order) != 2:
            raise ValidationError("pair arm order is not one control and one adaptive arm")
        orders.append(order[0])
    if len({pair["seed"] for pair in pairs}) != len(pairs):
        raise ValidationError("pair seeds must be unique")
    if orders.count("A") != 2 or orders.count("C") != 1:
        raise ValidationError("predeclared arm order is not counterbalanced")
    accounting = manifest["failure_accounting"]
    if not isinstance(accounting, Mapping) or accounting.get("no_retry") is not True or accounting.get("replacement_runs") is not False:
        raise ValidationError("manifest must retain no-retry failure accounting")
    if accounting.get("retain_terminal_statuses") != ["PASS", "ABORTED", "INCOMPLETE"]:
        raise ValidationError("manifest must retain every terminal status")
    return {"verdict": "MANIFEST_VALID", "study_id": manifest["study_id"], "pair_count": len(pairs)}


def _authoritative_common_commit(value: object) -> Mapping[str, Any]:
    required = {"authoritative_replica", "block_height", "block_hash", "witness_replicas", "after_fault_window"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValidationError("PASS outcome lacks exact authoritative common-commit proof")
    replica = value.get("authoritative_replica")
    witnesses = value.get("witness_replicas")
    if type(replica) is not int or replica not in range(7) or type(value.get("block_height")) is not int or value["block_height"] <= 0:
        raise ValidationError("authoritative common-commit identity is invalid")
    _hex64(value.get("block_hash"), "authoritative common-commit hash")
    if not isinstance(witnesses, list) or sorted(witnesses) != [item for item in range(7) if item != replica]:
        raise ValidationError("authoritative common commit lacks all six peer witnesses")
    if value.get("after_fault_window") is not True:
        raise ValidationError("authoritative common commit is not post-fault")
    return value


def _fixed_horizon_metric(value: object) -> Mapping[str, Any]:
    required = {
        "anchor_monotonic_ns", "horizon_end_monotonic_ns",
        "authoritative_common_commit_count", "maximum_inter_commit_gap_ns",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValidationError("PASS outcome lacks the fixed-horizon progress metric")
    start, end, count, gap = (
        value.get("anchor_monotonic_ns"), value.get("horizon_end_monotonic_ns"),
        value.get("authoritative_common_commit_count"), value.get("maximum_inter_commit_gap_ns"),
    )
    if (type(start) is not int or type(end) is not int or start <= 0 or
            end != start + 60_000_000_000 or
            type(count) is not int or count < 0 or
            (gap is not None and (type(gap) is not int or gap <= 0))):
        raise ValidationError("fixed-horizon progress metric is invalid")
    return value


def _validate_outcome(manifest: Mapping[str, Any], pair: Mapping[str, Any], arm_id: str, outcome: object) -> dict[str, Any]:
    required = {"pair_id", "arm_id", "status", "manifest_sha256", "shared_inputs", "terminal_validator"}
    if not isinstance(outcome, Mapping) or set(outcome) != required:
        raise ValidationError("outcome schema drift")
    if outcome.get("pair_id") != pair["pair_id"] or outcome.get("arm_id") != arm_id:
        raise ValidationError("outcome identity differs from its scheduled pair")
    if outcome.get("manifest_sha256") != manifest["manifest_sha256"]:
        raise ValidationError("outcome does not bind the frozen manifest")
    if outcome.get("shared_inputs") != manifest["shared_inputs"]:
        raise ValidationError("outcome changes E0, fault, workload, or measurement window")
    status = outcome.get("status")
    if status not in _TERMINAL:
        raise ValidationError("outcome status is not terminal")
    terminal = outcome["terminal_validator"]
    if not isinstance(terminal, Mapping):
        raise ValidationError("outcome lacks a terminal validator record")
    if status != "PASS":
        if set(terminal) != {"verdict", "reason"} or terminal.get("verdict") != status or not isinstance(terminal.get("reason"), str) or not terminal["reason"]:
            raise ValidationError("non-PASS outcome must retain its terminal verdict and reason")
        return {"arm_id": arm_id, "status": status, "eligible_for_primary_outcome": False}
    if arm_id == "A":
        required_terminal = {"verdict", "selection_replicas", "successor_activated", "authoritative_common_commit", "fixed_horizon"}
        arm = manifest["arms"]["adaptive"]
        if set(terminal) != required_terminal or terminal.get("verdict") != arm["required_terminal_validator_verdict"]:
            raise ValidationError("adaptive PASS outcome lacks its exact v4 terminal validator verdict")
        if terminal.get("selection_replicas") != arm["requires_selection"] or terminal.get("successor_activated") is not True:
            raise ValidationError("adaptive PASS outcome lacks selected containment activation")
    else:
        required_terminal = {"verdict", "no_adaptive_successor", "authoritative_common_commit", "fixed_horizon"}
        arm = manifest["arms"]["control"]
        if set(terminal) != required_terminal or terminal.get("verdict") != arm["required_terminal_validator_verdict"]:
            raise ValidationError("control PASS outcome lacks its exact terminal validator verdict")
        if terminal.get("no_adaptive_successor") is not True:
            raise ValidationError("control PASS outcome unexpectedly activated an adaptive successor")
    proof = _authoritative_common_commit(terminal["authoritative_common_commit"])
    metric = _fixed_horizon_metric(terminal["fixed_horizon"])
    return {
        "arm_id": arm_id,
        "status": "PASS",
        "eligible_for_primary_outcome": False,
        "evidence_state": "CALLER_SUMMARY_ONLY",
        "common_commit": dict(proof),
        "fixed_horizon": dict(metric),
        "claim_boundary": (
            "Planning-only summary; exact raw receipt and validator-verdict "
            "files were not independently reopened, hash-checked, and replayed."
        ),
    }


def validate_pair(manifest: Mapping[str, Any], pair_id: str, outcomes: Sequence[object]) -> dict[str, Any]:
    validate_manifest(manifest)
    pair = next((item for item in manifest["pairs"] if item["pair_id"] == pair_id), None)
    if pair is None:
        raise ValidationError("outcome names an unscheduled pair")
    if len(outcomes) != 2:
        raise ValidationError("pair must retain exactly two terminal outcomes")
    by_arm: dict[str, object] = {}
    for outcome in outcomes:
        if not isinstance(outcome, Mapping) or outcome.get("arm_id") not in _ARM_IDS:
            raise ValidationError("outcome arm identity is invalid")
        arm_id = outcome["arm_id"]
        if arm_id in by_arm:
            raise ValidationError("pair repeats an arm outcome")
        by_arm[arm_id] = outcome
    if set(by_arm) != _ARM_IDS:
        raise ValidationError("pair lacks a control or adaptive outcome")
    validated = [_validate_outcome(manifest, pair, arm_id, by_arm[arm_id]) for arm_id in pair["arm_order"]]
    has_nonpass = any(item["status"] != "PASS" for item in validated)
    verdict = (
        "PAIR_RETAINED_NONPASS"
        if has_nonpass else
        "PAIR_PLANNING_ONLY_UNBOUND"
    )
    return {
        "verdict": verdict,
        "pair_id": pair_id,
        "arm_order": list(pair["arm_order"]),
        "outcomes": validated,
        "claim_boundary": (
            "Planning-only schema validation; caller-authored PASS summaries "
            "are not evidence, and no throughput comparison, advantage "
            "estimate, or positive campaign claim follows."
        ),
    }
