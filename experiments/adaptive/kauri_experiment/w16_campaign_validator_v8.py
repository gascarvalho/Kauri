"""Fail-closed aggregation for the prospective, distinct W16 v8 campaign.

This module intentionally does not accept v7 cells.  Version 8 may report a
bounded non-authoritative identity-gap diagnostic, but its throughput remains
the designated observer's exact validated chain and never becomes a claim.
"""

from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from .w16_output_validator import validate_w16_output_v8


FORWARD = ("slow-roots:homogeneous", "fast-roots:homogeneous",
           "slow-roots:heterogeneous", "fast-roots:heterogeneous")
REVERSE = tuple(reversed(FORWARD))
_LABELS = frozenset(FORWARD)
_CELL_KIND = "kauri-w16-output-validation-v8"
_RECEIPT_SCHEMA = "kauri-n31-static-e0-local-executor-v3"
_AUTH_KIND = "kauri-w16-static-e0-campaign-authorization-v3"


class _InvalidCampaign(RuntimeError):
    def __init__(self, code: str, detail: str, verdict: str = "INCOMPLETE") -> None:
        super().__init__(detail)
        self.code, self.detail, self.verdict = code, detail, verdict


def _fail(code: str, detail: str, verdict: str = "INCOMPLETE") -> None:
    raise _InvalidCampaign(code, detail, verdict)


def _read_unchanged(path: Path, before: bytes, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or path.read_bytes() != before:
        _fail("sealed_cell", f"{label} is not an unchanged regular file")
    try:
        value = json.loads(before)
    except (UnicodeError, json.JSONDecodeError) as error:
        _fail("sealed_cell", f"{label} is malformed: {error}")
    if not isinstance(value, dict):
        _fail("sealed_cell", f"{label} must be one JSON object")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail("cell_contract", f"{label} must be an object")
    return value


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _hex(value: object, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(c in "0123456789abcdef" for c in value)


def _post_measurement_peer_tails(
    value: object, *, ordinal: int, authoritative_observer: int,
) -> dict[str, object]:
    """Retain the v8 observer-excluded tail disclosure without using it as rate data."""

    summary = _mapping(value, "post-measurement peer-tail diagnostic")
    if set(summary) != {"count", "reporters", "details"}:
        _fail("peer_tails", f"cell {ordinal} peer-tail diagnostic schema drifted")
    count, reporters, details = summary.get("count"), summary.get("reporters"), summary.get("details")
    if type(count) is not int or count < 0 or not isinstance(reporters, list) or not isinstance(details, list) or len(details) != count:
        _fail("peer_tails", f"cell {ordinal} peer-tail diagnostic count drifted")
    if (
        any(type(replica) is not int or not 0 <= replica < 31 or replica == authoritative_observer for replica in reporters)
        or reporters != sorted(set(reporters))
    ):
        _fail("peer_tails", f"cell {ordinal} peer-tail reporters drifted")
    normalized: list[dict[str, object]] = []
    previous_height = 0
    seen_reporters: set[int] = set()
    for detail in details:
        if (
            not isinstance(detail, dict)
            or set(detail) != {"block_height", "block_hash", "parent_hash", "transaction_count", "reporters", "tree_id", "view_generation"}
            or type(detail.get("block_height")) is not int or detail["block_height"] <= previous_height
            or not _hex(detail.get("block_hash"), 64) or not _hex(detail.get("parent_hash"), 64)
            or type(detail.get("transaction_count")) is not int or not 0 <= detail["transaction_count"] <= 1000
            or type(detail.get("tree_id")) is not int or not 0 <= detail["tree_id"] < 21
            or type(detail.get("view_generation")) is not int or detail["view_generation"] <= 0
            or not isinstance(detail.get("reporters"), list) or not detail["reporters"]
            or any(type(replica) is not int or replica not in reporters for replica in detail["reporters"])
            or detail["reporters"] != sorted(set(detail["reporters"]))
        ):
            _fail("peer_tails", f"cell {ordinal} peer-tail detail drifted")
        previous_height = detail["block_height"]
        seen_reporters.update(detail["reporters"])
        normalized.append(dict(detail))
    if seen_reporters != set(reporters):
        _fail("peer_tails", f"cell {ordinal} peer-tail reporter disclosure is incomplete")
    return {"count": count, "reporters": list(reporters), "details": normalized}


def _identity_unavailable(
    value: object, *, ordinal: int, authoritative_observer: int,
) -> dict[str, object]:
    """Validate and retain the v8 non-authoritative-gap disclosure verbatim."""

    summary = _mapping(value, "identity-unavailable diagnostic")
    if set(summary) != {"schema_version", "event_type", "total_count", "in_measurement_count", "details", "undisputed_height_gaps", "unanimous_observed_bridges", "post_measurement_peer_tails"} or summary.get("schema_version") != 1 or summary.get("event_type") != "block.commit_identity_unavailable":
        _fail("identity_unavailable", f"cell {ordinal} diagnostic schema drifted")
    total, in_window, details, gaps, bridges = summary.get("total_count"), summary.get("in_measurement_count"), summary.get("details"), summary.get("undisputed_height_gaps"), summary.get("unanimous_observed_bridges")
    if type(total) is not int or total < 0 or type(in_window) is not int or in_window < 0 or not isinstance(details, list) or len(details) != total or not isinstance(gaps, list) or not isinstance(bridges, list):
        _fail("identity_unavailable", f"cell {ordinal} diagnostic count drifted")
    normalized: list[dict[str, object]] = []
    previous: tuple[int, str, int] | None = None
    for detail in details:
        if not isinstance(detail, dict) or set(detail) != {"replica_id", "block_height", "block_hash", "transaction_count", "phase", "rich_peer_count", "observed_replica_count", "tree_id", "view_generation"}:
            _fail("identity_unavailable", f"cell {ordinal} diagnostic detail schema drifted")
        key = (detail.get("block_height"), detail.get("block_hash"), detail.get("replica_id"))
        if (type(detail.get("replica_id")) is not int or not 0 <= detail["replica_id"] < 31 or detail["replica_id"] == authoritative_observer or type(detail.get("block_height")) is not int or detail["block_height"] <= 0 or not _hex(detail.get("block_hash"), 64) or type(detail.get("transaction_count")) is not int or not 0 <= detail["transaction_count"] <= 1000 or detail.get("phase") not in {"pre_measurement", "in_measurement", "post_measurement"} or detail.get("rich_peer_count") != 30 or detail.get("observed_replica_count") != 31 or type(detail.get("tree_id")) is not int or not 0 <= detail["tree_id"] < 21 or type(detail.get("view_generation")) is not int or detail["view_generation"] <= 0 or previous is not None and key <= previous):
            _fail("identity_unavailable", f"cell {ordinal} diagnostic detail differs from v8 contract")
        previous = key
        normalized.append(dict(detail))
    if in_window != sum(row["phase"] == "in_measurement" for row in normalized):
        _fail("identity_unavailable", f"cell {ordinal} diagnostic phase count drifted")
    normalized_gaps: list[dict[str, int]] = []
    previous_after = 0
    for gap in gaps:
        if (
            not isinstance(gap, dict)
            or set(gap) != {"before_height", "after_height", "missing_height_count"}
            or any(type(gap.get(key)) is not int for key in gap)
            or gap["before_height"] <= 0 or gap["after_height"] <= gap["before_height"] + 1
            or gap["missing_height_count"] != gap["after_height"] - gap["before_height"] - 1
            or gap["before_height"] < previous_after
        ):
            _fail("identity_unavailable", f"cell {ordinal} height-gap disclosure drifted")
        previous_after = gap["after_height"]
        normalized_gaps.append(dict(gap))
    normalized_bridges: list[dict[str, object]] = []
    previous_height = 0
    for bridge in bridges:
        if (
            not isinstance(bridge, dict)
            or set(bridge) != {"block_height", "block_hash", "parent_hash", "transaction_count", "observed_replica_count", "reason"}
            or type(bridge.get("block_height")) is not int or bridge["block_height"] <= previous_height
            or not _hex(bridge.get("block_hash"), 64) or not _hex(bridge.get("parent_hash"), 64)
            or type(bridge.get("transaction_count")) is not int or not 0 <= bridge["transaction_count"] <= 1000
            or bridge.get("observed_replica_count") != 31 or bridge.get("reason") != "unknown_no_rich_disposition"
        ):
            _fail("identity_unavailable", f"cell {ordinal} observed-only bridge disclosure drifted")
        previous_height = bridge["block_height"]
        normalized_bridges.append(dict(bridge))
    tails = _post_measurement_peer_tails(
        summary.get("post_measurement_peer_tails"), ordinal=ordinal,
        authoritative_observer=authoritative_observer,
    )
    return {"schema_version": 1, "event_type": "block.commit_identity_unavailable", "total_count": total, "in_measurement_count": in_window, "details": normalized, "undisputed_height_gaps": normalized_gaps, "unanimous_observed_bridges": normalized_bridges, "post_measurement_peer_tails": tails}


def _one_cell(root: Path, *, ordinal: int, expected_label: str) -> dict[str, object]:
    receipt_path, authorization_path = root / "feasibility-receipt.json", root / "authorization.json"
    if any(path.is_symlink() or not path.is_file() for path in (receipt_path, authorization_path)):
        _fail("sealed_cell", f"cell {ordinal} lacks a regular receipt/authorization")
    receipt_bytes, authorization_bytes = receipt_path.read_bytes(), authorization_path.read_bytes()
    validation = validate_w16_output_v8(root)
    receipt = _read_unchanged(receipt_path, receipt_bytes, f"cell {ordinal} receipt")
    authorization = _read_unchanged(authorization_path, authorization_bytes, f"cell {ordinal} authorization")
    if not isinstance(validation, Mapping) or validation.get("verdict") != "PASS":
        verdict = validation.get("verdict") if isinstance(validation, Mapping) else None
        _fail("cell_validation", f"cell {ordinal} failed v8 validation", "FAIL" if verdict == "FAIL" else "INCOMPLETE")
    identity_unavailable = _identity_unavailable(
        validation.get("identity_unavailable"), ordinal=ordinal,
        authoritative_observer=27,
    )
    if validation.get("post_measurement_peer_tails") != identity_unavailable["post_measurement_peer_tails"]:
        _fail("peer_tails", f"cell {ordinal} peer-tail diagnostic is not bound to identity validation")
    preflight = _mapping(receipt.get("preflight"), "receipt preflight")
    binaries = _mapping(preflight.get("binary_sha256"), "preflight binaries")
    throughput, lifecycle = _mapping(validation.get("throughput"), "validated throughput"), _mapping(validation.get("native_lifecycle_span"), "validated native lifecycle")
    raw_span, quota = _mapping(validation.get("producer_raw_clock_span"), "producer RAW clock span"), _mapping(validation.get("quota"), "validated quota")
    auth_summary, cleanup = _mapping(validation.get("authorization"), "validated authorization"), _mapping(validation.get("process_cleanup"), "validated cleanup")
    start, end = receipt.get("started_monotonic_ns"), receipt.get("ended_monotonic_ns")
    raw_start, raw_end = receipt.get("started_raw_monotonic_ns"), receipt.get("ended_raw_monotonic_ns")
    window_start, window_end = throughput.get("window_start_monotonic_ns"), throughput.get("window_end_monotonic_ns")
    lifecycle_start, lifecycle_end = lifecycle.get("start_monotonic_ns"), lifecycle.get("end_monotonic_ns")
    transactions, duration = throughput.get("transaction_count"), throughput.get("duration_ns")
    milli_tps = throughput.get("throughput_milli_tps")
    label = f"{authorization.get('arm')}:{authorization.get('quota_mode')}"
    if (
        validation.get("schema_version") != 1 or validation.get("kind") != _CELL_KIND
        or validation.get("evidence_class") != "CPU_QUOTA_SINGLE_ARM"
        or validation.get("claim_eligible") is not False or validation.get("figure_eligible") is not False
        or receipt.get("schema") != _RECEIPT_SCHEMA or receipt.get("attempts") != 1 or receipt.get("retries") != 0
        or receipt.get("required_complete_cycles") != 5 or not isinstance(receipt.get("run_id"), str) or not receipt["run_id"]
        or receipt.get("raw_clock_id") != "CLOCK_MONOTONIC_RAW" or not all(_positive_int(v) for v in (start, end, raw_start, raw_end, window_start, window_end, lifecycle_start, lifecycle_end, transactions, duration, milli_tps))
        or int(end) <= int(start) or int(raw_end) <= int(raw_start)
        or raw_span != {"clock_id": "CLOCK_MONOTONIC_RAW", "start_monotonic_ns": raw_start, "end_monotonic_ns": raw_end}
        or validation.get("run_id") != receipt["run_id"] or validation.get("revision") != preflight.get("revision")
        or label != expected_label or validation.get("arm") != authorization.get("arm") or quota.get("mode") != authorization.get("quota_mode")
        or validation.get("authoritative_observer") != 27
        or authorization.get("schema_version") != 3 or authorization.get("kind") != _AUTH_KIND or authorization.get("cell_validator_version") != 8 or authorization.get("authoritative_observer") != 27
        or authorization.get("cell_ordinal") != ordinal or auth_summary.get("cell_ordinal") != ordinal
        or any(auth_summary.get(k) != authorization.get(k) for k in ("block_id", "campaign_id", "block_index", "campaign_freeze_sha256"))
        or preflight.get("schema_version") != 8 or preflight.get("kind") != "kauri-n31-static-e0-feasibility-preflight-v8" or preflight.get("profile_id") != "n31-static-e0-local-feasibility-v8"
        or authorization.get("revision") != preflight.get("revision") or authorization.get("profile_sha256") != preflight.get("profile_sha256") or authorization.get("binary_sha256") != binaries
        or authorization.get("required_complete_cycles") != 5 or authorization.get("hard_timeout_s") != 480 or authorization.get("external_timeout_s") != 720 or authorization.get("automatic_retries") != 0
        or authorization.get("claim_eligible") is not False or authorization.get("figure_eligible") is not False or authorization.get("process_cleanup_required") is not True
        or cleanup.get("replica_count") != 31 or cleanup.get("sigint_count") != 31 or cleanup.get("sigkill_count") != 0 or cleanup.get("all_returncodes_zero") is not True
        or not int(raw_start) <= int(lifecycle_start) < int(window_start) < int(window_end) < int(lifecycle_end) <= int(raw_end)
        or int(duration) != int(window_end) - int(window_start) or int(milli_tps) != int(transactions) * 1_000_000_000_000 // int(duration)
    ):
        _fail("cell_contract", f"cell {ordinal} differs from the frozen v8 contract")
    revision, profile = preflight.get("revision"), preflight.get("profile_sha256")
    if not _hex(revision, 40) or not _hex(profile, 64) or set(binaries) != {"app", "keygen", "tls_keygen", "native_digest"} or any(not _hex(v, 64) for v in binaries.values()) or not _hex(authorization.get("campaign_freeze_sha256"), 64):
        _fail("cell_identity", f"cell {ordinal} has malformed shared identity")
    return {"ordinal": ordinal, "label": label, "root": str(root), "run_id": receipt["run_id"], "started_raw_monotonic_ns": int(raw_start), "ended_raw_monotonic_ns": int(raw_end), "revision": revision, "profile_sha256": profile, "binary_sha256": dict(binaries), "campaign_id": authorization["campaign_id"], "block_index": authorization["block_index"], "block_id": authorization["block_id"], "block_order": authorization["block_order"], "campaign_freeze_sha256": authorization["campaign_freeze_sha256"], "approval_ref": authorization["approval_ref"], "authorization_sha256": hashlib.sha256(authorization_bytes).hexdigest(), "authoritative_observer": 27, "transaction_count": int(transactions), "duration_ns": int(duration), "identity_unavailable": identity_unavailable, "post_measurement_peer_tails": identity_unavailable["post_measurement_peer_tails"]}


def validate_w16_campaign_block_v8(roots: Sequence[Path]) -> dict[str, object]:
    result: dict[str, object] = {"schema_version": 1, "kind": "kauri-w16-cpu-campaign-block-validation-v8", "verdict": "INCOMPLETE", "claim_eligible": False, "thesis_result_eligible": False, "figure_eligible": False}
    try:
        if isinstance(roots, (str, bytes)) or len(roots) != 4:
            _fail("block_cardinality", "campaign block requires exactly four ordered roots")
        resolved = [Path(root) for root in roots]
        if any(path.is_symlink() or not path.is_dir() for path in resolved) or len({path.resolve() for path in resolved}) != 4:
            _fail("block_cardinality", "campaign block roots must be four distinct regular directories")
        auth = _read_unchanged(resolved[0] / "authorization.json", (resolved[0] / "authorization.json").read_bytes(), "first-cell authorization")
        block_index = auth.get("block_index")
        if type(block_index) is not int or block_index not in range(1, 7):
            _fail("block_identity", "campaign block index must be 1 through 6")
        order = FORWARD if block_index % 2 else REVERSE
        cells = [_one_cell(root.resolve(), ordinal=ordinal, expected_label=label) for ordinal, (root, label) in enumerate(zip(resolved, order, strict=True), 1)]
        if len({cell["run_id"] for cell in cells}) != 4:
            _fail("duplicate_run_id", "campaign block run IDs must be distinct")
        if any(int(current["started_raw_monotonic_ns"]) <= int(previous["ended_raw_monotonic_ns"]) for previous, current in zip(cells, cells[1:])):
            _fail("cell_chronology", "campaign block cells overlap or violate order")
        shared = ("revision", "profile_sha256", "binary_sha256", "campaign_id", "block_index", "block_id", "campaign_freeze_sha256", "approval_ref")
        if any(any(cell[key] != cells[0][key] for cell in cells[1:]) for key in shared) or any(cell["block_order"] != list(order) for cell in cells):
            _fail("cross_cell_identity", "campaign block identity or order differs")
        rates = {cell["label"]: Fraction(cell["transaction_count"], cell["duration_ns"]) for cell in cells}
        if set(rates) != _LABELS:
            _fail("cell_order", "campaign block is missing a treatment label")
        homogeneous = rates["fast-roots:homogeneous"] / rates["slow-roots:homogeneous"]
        heterogeneous = rates["fast-roots:heterogeneous"] / rates["slow-roots:heterogeneous"]
        interaction = heterogeneous / homogeneous
        gap_by_cell = [{"ordinal": cell["ordinal"], "label": cell["label"], **cell["identity_unavailable"]} for cell in cells]
        tails_by_cell = [{"ordinal": cell["ordinal"], "label": cell["label"], **cell["post_measurement_peer_tails"]} for cell in cells]
        result.update({"verdict": "PASS", "reason_code": "campaign_block_complete", "execution_order": "forward" if block_index % 2 else "reverse", "block_index": block_index, "block_id": cells[0]["block_id"], "campaign_id": cells[0]["campaign_id"], "campaign_freeze_sha256": cells[0]["campaign_freeze_sha256"], "approval_ref": cells[0]["approval_ref"], "revision": cells[0]["revision"], "profile_sha256": cells[0]["profile_sha256"], "binary_sha256": cells[0]["binary_sha256"], "cells": cells, "identity_unavailable": {"total_count": sum(row["total_count"] for row in gap_by_cell), "in_measurement_count": sum(row["in_measurement_count"] for row in gap_by_cell), "by_cell": gap_by_cell}, "post_measurement_peer_tails": {"count": sum(row["count"] for row in tails_by_cell), "reporters": sorted({reporter for row in tails_by_cell for reporter in row["reporters"]}), "by_cell": tails_by_cell}, "effects": {"definition": "log(T_BX/T_AX)-log(T_BH/T_AH)", "rate_source": "exact transaction_count/duration_ns fractions", "ratio_unit": "dimensionless", "homogeneous_ratio": float(homogeneous), "heterogeneous_ratio": float(heterogeneous), "interaction_ratio": float(interaction), "log_interaction": math.log(float(interaction)), "direct_and_adjusted_positive": heterogeneous > 1 and interaction > 1, "interaction_ratio_exact": {"numerator": interaction.numerator, "denominator": interaction.denominator}}})
    except _InvalidCampaign as error:
        result.update({"verdict": error.verdict, "reason_code": error.code, "detail": error.detail})
    except (OSError, ValueError, TypeError, OverflowError) as error:
        result.update({"reason_code": "validator_input_error", "detail": str(error)})
    return result


def validate_w16_cpu_campaign_v8(blocks: Sequence[Sequence[Path]]) -> dict[str, object]:
    result: dict[str, object] = {"schema_version": 1, "kind": "kauri-w16-cpu-campaign-validation-v8", "verdict": "INCOMPLETE", "claim_eligible": False, "thesis_result_eligible": False, "figure_eligible": False, "technical_improvement_gate_passed": False}
    try:
        if isinstance(blocks, (str, bytes)) or len(blocks) != 6:
            _fail("campaign_cardinality", "campaign requires exactly six four-cell blocks")
        validated = [validate_w16_campaign_block_v8(block) for block in blocks]
        for index, block in enumerate(validated, 1):
            if block.get("verdict") != "PASS":
                _fail("block_validation", f"block {index} did not pass: {block.get('reason_code')}", "FAIL" if block.get("verdict") == "FAIL" else "INCOMPLETE")
            if block.get("block_index") != index or block.get("execution_order") != ("forward" if index % 2 else "reverse"):
                _fail("block_order", "block contradicts frozen F/R design")
        shared = ("campaign_id", "campaign_freeze_sha256", "approval_ref", "revision", "profile_sha256", "binary_sha256")
        if any(any(block[key] != validated[0][key] for block in validated[1:]) for key in shared):
            _fail("cross_block_identity", "campaign blocks differ in identity")
        cells = [cell for block in validated for cell in block["cells"]]
        if len(cells) != 24 or len({cell["root"] for cell in cells}) != 24 or len({cell["run_id"] for cell in cells}) != 24:
            _fail("campaign_cardinality", "campaign requires 24 distinct sealed roots and run IDs")
        if any(int(current["started_raw_monotonic_ns"]) <= int(previous["ended_raw_monotonic_ns"]) for previous, current in zip(cells, cells[1:])):
            _fail("campaign_chronology", "campaign cells overlap or violate schedule")
        interactions = [Fraction(block["effects"]["interaction_ratio_exact"]["numerator"], block["effects"]["interaction_ratio_exact"]["denominator"]) for block in validated]
        positives = [bool(block["effects"]["direct_and_adjusted_positive"]) for block in validated]
        forward_positive, reverse_positive, product = sum(positives[::2]), sum(positives[1::2]), math.prod(interactions)
        gate = sum(positives) >= 5 and forward_positive >= 2 and reverse_positive >= 2 and product >= Fraction(11, 10) ** 6
        logs, ordered = [math.log(float(item)) for item in interactions], sorted(math.log(float(item)) for item in interactions)
        gap_by_block = [{"block_index": block["block_index"], **block["identity_unavailable"]} for block in validated]
        tails_by_block = [{"block_index": block["block_index"], **block["post_measurement_peer_tails"]} for block in validated]
        result.update({"verdict": "PASS", "reason_code": "campaign_technically_complete", "campaign_id": validated[0]["campaign_id"], "campaign_freeze_sha256": validated[0]["campaign_freeze_sha256"], "approval_ref": validated[0]["approval_ref"], "revision": validated[0]["revision"], "profile_sha256": validated[0]["profile_sha256"], "binary_sha256": validated[0]["binary_sha256"], "blocks": validated, "identity_unavailable": {"total_count": sum(row["total_count"] for row in gap_by_block), "in_measurement_count": sum(row["in_measurement_count"] for row in gap_by_block), "by_block": gap_by_block}, "post_measurement_peer_tails": {"count": sum(row["count"] for row in tails_by_block), "reporters": sorted({reporter for row in tails_by_block for reporter in row["reporters"]}), "by_block": tails_by_block}, "technical_improvement_gate_passed": gate, "direction_gate": {"status": "POSITIVE" if gate else "OBSERVED_NEGATIVE_OR_MIXED", "positive_block_count": sum(positives), "forward_positive_count": forward_positive, "reverse_positive_count": reverse_positive, "required_positive_blocks": 5, "required_positive_per_order": 2, "required_geometric_mean_adjusted": 1.10, "log_interactions": logs, "median_log_interaction": (ordered[2] + ordered[3]) / 2, "range_log_interaction": [ordered[0], ordered[-1]], "geometric_mean_adjusted_ratio": math.exp(math.fsum(logs) / 6), "product_adjusted_ratio_exact": {"numerator": product.numerator, "denominator": product.denominator}}})
    except _InvalidCampaign as error:
        result.update({"verdict": error.verdict, "reason_code": error.code, "detail": error.detail})
    except (OSError, ValueError, TypeError, OverflowError) as error:
        result.update({"reason_code": "validator_input_error", "detail": str(error)})
    return result
