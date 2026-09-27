"""Fail-closed aggregation for the prospective W16 v7 CPU campaign.

This is deliberately separate from :mod:`w16_campaign_validator`: a v7
campaign is meaningful only when every cell has the v7 clean-shutdown proof.
The module reports technical completeness, never a thesis or figure claim.
"""

from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from .w16_output_validator import validate_w16_output_v7


FORWARD = (
    "slow-roots:homogeneous", "fast-roots:homogeneous",
    "slow-roots:heterogeneous", "fast-roots:heterogeneous",
)
REVERSE = tuple(reversed(FORWARD))
_LABELS = frozenset(FORWARD)
_CELL_KIND = "kauri-w16-output-validation-v7"
_RECEIPT_SCHEMA = "kauri-n31-static-e0-local-executor-v3"
_AUTHORIZATION_KIND = "kauri-w16-static-e0-campaign-authorization-v3"


class _InvalidCampaign(RuntimeError):
    def __init__(self, code: str, detail: str, *, verdict: str = "INCOMPLETE") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.verdict = verdict


def _fail(code: str, detail: str, *, verdict: str = "INCOMPLETE") -> None:
    raise _InvalidCampaign(code, detail, verdict=verdict)


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


def _hex(value: object, size: int) -> bool:
    return isinstance(value, str) and len(value) == size and all(
        character in "0123456789abcdef" for character in value
    )


def _one_cell(root: Path, *, ordinal: int, expected_label: str) -> dict[str, object]:
    receipt_path = root / "feasibility-receipt.json"
    authorization_path = root / "authorization.json"
    if any(path.is_symlink() or not path.is_file() for path in (receipt_path, authorization_path)):
        _fail("sealed_cell", f"cell {ordinal} lacks a regular receipt/authorization")
    receipt_before = receipt_path.read_bytes()
    authorization_before = authorization_path.read_bytes()
    validation = validate_w16_output_v7(root)
    receipt = _read_unchanged(receipt_path, receipt_before, f"cell {ordinal} receipt")
    authorization = _read_unchanged(authorization_path, authorization_before, f"cell {ordinal} authorization")
    if not isinstance(validation, Mapping) or validation.get("verdict") != "PASS":
        verdict = validation.get("verdict") if isinstance(validation, Mapping) else None
        _fail("cell_validation", f"cell {ordinal} failed v7 validation", verdict="FAIL" if verdict == "FAIL" else "INCOMPLETE")

    preflight = _mapping(receipt.get("preflight"), "receipt preflight")
    binaries = _mapping(preflight.get("binary_sha256"), "preflight binaries")
    throughput = _mapping(validation.get("throughput"), "validated throughput")
    lifecycle = _mapping(validation.get("native_lifecycle_span"), "validated native lifecycle")
    raw_span = _mapping(validation.get("producer_raw_clock_span"), "producer RAW clock span")
    quota = _mapping(validation.get("quota"), "validated CPU quota")
    auth_summary = _mapping(validation.get("authorization"), "validated authorization")
    cleanup = _mapping(validation.get("process_cleanup"), "validated process cleanup")
    start, end = receipt.get("started_monotonic_ns"), receipt.get("ended_monotonic_ns")
    raw_start, raw_end = receipt.get("started_raw_monotonic_ns"), receipt.get("ended_raw_monotonic_ns")
    window_start, window_end = throughput.get("window_start_monotonic_ns"), throughput.get("window_end_monotonic_ns")
    lifecycle_start, lifecycle_end = lifecycle.get("start_monotonic_ns"), lifecycle.get("end_monotonic_ns")
    transactions, duration, milli_tps = (throughput.get(key) for key in (
        "transaction_count", "duration_ns", "throughput_milli_tps"))
    label = f"{authorization.get('arm')}:{authorization.get('quota_mode')}"
    if (
        validation.get("schema_version") != 1 or validation.get("kind") != _CELL_KIND
        or validation.get("evidence_class") != "CPU_QUOTA_SINGLE_ARM"
        or validation.get("claim_eligible") is not False or validation.get("figure_eligible") is not False
        or receipt.get("schema") != _RECEIPT_SCHEMA
        or type(receipt.get("attempts")) is not int or receipt.get("attempts") != 1
        or type(receipt.get("retries")) is not int or receipt.get("retries") != 0
        or type(receipt.get("required_complete_cycles")) is not int
        or receipt.get("required_complete_cycles") != 5
        or not isinstance(receipt.get("run_id"), str) or not receipt["run_id"]
        or not _positive_int(start) or not _positive_int(end) or int(end) <= int(start)
        or receipt.get("raw_clock_id") != "CLOCK_MONOTONIC_RAW"
        or not _positive_int(raw_start) or not _positive_int(raw_end) or int(raw_end) <= int(raw_start)
        or raw_span != {"clock_id": "CLOCK_MONOTONIC_RAW", "start_monotonic_ns": raw_start, "end_monotonic_ns": raw_end}
        or validation.get("run_id") != receipt["run_id"] or validation.get("revision") != preflight.get("revision")
        or label != expected_label or validation.get("arm") != authorization.get("arm")
        or quota.get("mode") != authorization.get("quota_mode")
        or authorization.get("schema_version") != 3 or authorization.get("kind") != _AUTHORIZATION_KIND
        or authorization.get("cell_ordinal") != ordinal or auth_summary.get("cell_ordinal") != ordinal
        or any(auth_summary.get(key) != authorization.get(key) for key in (
            "block_id", "campaign_id", "block_index", "campaign_freeze_sha256"))
        or authorization.get("revision") != preflight.get("revision")
        or authorization.get("profile_sha256") != preflight.get("profile_sha256")
        or authorization.get("binary_sha256") != binaries or authorization.get("required_complete_cycles") != 5
        or authorization.get("hard_timeout_s") != 480 or authorization.get("external_timeout_s") != 720
        or authorization.get("automatic_retries") != 0 or authorization.get("claim_eligible") is not False
        or authorization.get("figure_eligible") is not False
        or cleanup.get("replica_count") != 31 or cleanup.get("sigint_count") != 31
        or cleanup.get("sigkill_count") != 0 or cleanup.get("all_returncodes_zero") is not True
        or not _positive_int(transactions) or not _positive_int(duration) or not _positive_int(milli_tps)
        or not _positive_int(window_start) or not _positive_int(window_end)
        or not int(raw_start) <= int(window_start) < int(window_end) <= int(raw_end)
        or set(lifecycle) != {"start_monotonic_ns", "end_monotonic_ns"}
        or not _positive_int(lifecycle_start) or not _positive_int(lifecycle_end)
        or not int(raw_start) <= int(lifecycle_start) < int(window_start) < int(window_end) < int(lifecycle_end) <= int(raw_end)
        or int(duration) != int(window_end) - int(window_start)
        or int(milli_tps) != int(transactions) * 1_000_000_000_000 // int(duration)
    ):
        _fail("cell_contract", f"cell {ordinal} differs from the frozen v7 contract")
    revision, profile = preflight.get("revision"), preflight.get("profile_sha256")
    if (
        not _hex(revision, 40) or not _hex(profile, 64)
        or set(binaries) != {"app", "keygen", "tls_keygen", "native_digest"}
        or any(not _hex(value, 64) for value in binaries.values())
        or not _hex(authorization.get("campaign_freeze_sha256"), 64)
    ):
        _fail("cell_identity", f"cell {ordinal} has malformed shared identity")
    return {
        "ordinal": ordinal, "label": label, "root": str(root), "run_id": receipt["run_id"],
        "started_monotonic_ns": int(start), "ended_monotonic_ns": int(end),
        "started_raw_monotonic_ns": int(raw_start), "ended_raw_monotonic_ns": int(raw_end),
        "window_start_monotonic_ns": int(window_start), "window_end_monotonic_ns": int(window_end),
        "native_lifecycle_start_monotonic_ns": int(lifecycle_start),
        "native_lifecycle_end_monotonic_ns": int(lifecycle_end),
        "revision": revision, "profile_sha256": profile, "binary_sha256": dict(binaries),
        "campaign_id": authorization["campaign_id"], "block_index": authorization["block_index"],
        "block_id": authorization["block_id"], "block_order": authorization["block_order"],
        "campaign_freeze_sha256": authorization["campaign_freeze_sha256"],
        "approval_ref": authorization["approval_ref"], "approved_at_utc": authorization["approved_at_utc"],
        "authorization_sha256": hashlib.sha256(authorization_before).hexdigest(),
        "transaction_count": int(transactions), "duration_ns": int(duration),
    }


def validate_w16_campaign_block_v7(roots: Sequence[Path]) -> dict[str, object]:
    """Validate one v7 forward/reverse block; never grant a thesis claim."""

    result: dict[str, object] = {
        "schema_version": 1, "kind": "kauri-w16-cpu-campaign-block-validation-v7",
        "verdict": "INCOMPLETE", "claim_eligible": False,
        "thesis_result_eligible": False, "figure_eligible": False,
    }
    try:
        if isinstance(roots, (str, bytes)) or len(roots) != 4:
            _fail("block_cardinality", "campaign block requires exactly four ordered roots")
        resolved = [Path(candidate) for candidate in roots]
        if any(path.is_symlink() or not path.is_dir() for path in resolved) or len(set(path.resolve() for path in resolved)) != 4:
            _fail("block_cardinality", "campaign block roots must be four distinct regular directories")
        first_authorization = _read_unchanged(resolved[0] / "authorization.json", (resolved[0] / "authorization.json").read_bytes(), "first-cell authorization")
        block_index = first_authorization.get("block_index")
        if type(block_index) is not int or block_index not in range(1, 7):
            _fail("block_identity", "campaign block index must be 1 through 6")
        order = FORWARD if block_index % 2 else REVERSE
        cells = [_one_cell(root.resolve(), ordinal=ordinal, expected_label=label) for ordinal, (root, label) in enumerate(zip(resolved, order, strict=True), 1)]
        if len({cell["run_id"] for cell in cells}) != 4:
            _fail("duplicate_run_id", "campaign block run IDs must be distinct")
        for previous, current in zip(cells, cells[1:]):
            if any(int(current[key]) <= int(previous[other]) for key, other in (
                ("started_monotonic_ns", "ended_monotonic_ns"),
                ("started_raw_monotonic_ns", "ended_raw_monotonic_ns"),
                ("window_start_monotonic_ns", "window_end_monotonic_ns"),
                ("native_lifecycle_start_monotonic_ns", "native_lifecycle_end_monotonic_ns"),
            )):
                _fail("cell_chronology", "campaign block cells overlap or violate order")
        shared = ("revision", "profile_sha256", "binary_sha256", "campaign_id", "block_index", "block_id", "campaign_freeze_sha256", "approval_ref")
        if any(any(cell[key] != cells[0][key] for cell in cells[1:]) for key in shared):
            _fail("cross_cell_identity", "campaign block identity differs between cells")
        if any(cell["block_order"] != list(order) for cell in cells):
            _fail("cell_order", "campaign block authorization order drifted")
        rates = {cell["label"]: Fraction(int(cell["transaction_count"]), int(cell["duration_ns"])) for cell in cells}
        if set(rates) != _LABELS:
            _fail("cell_order", "campaign block is missing a treatment label")
        homogeneous = rates["fast-roots:homogeneous"] / rates["slow-roots:homogeneous"]
        heterogeneous = rates["fast-roots:heterogeneous"] / rates["slow-roots:heterogeneous"]
        interaction = heterogeneous / homogeneous
        result.update({
            "verdict": "PASS", "reason_code": "campaign_block_complete",
            "execution_order": "forward" if block_index % 2 else "reverse", "block_index": block_index,
            "block_id": cells[0]["block_id"], "campaign_id": cells[0]["campaign_id"],
            "campaign_freeze_sha256": cells[0]["campaign_freeze_sha256"], "approval_ref": cells[0]["approval_ref"],
            "revision": cells[0]["revision"], "profile_sha256": cells[0]["profile_sha256"],
            "binary_sha256": cells[0]["binary_sha256"], "cells": cells,
            "effects": {
                "definition": "log(T_BX/T_AX)-log(T_BH/T_AH)",
                "rate_source": "exact transaction_count/duration_ns fractions", "ratio_unit": "dimensionless",
                "homogeneous_ratio": float(homogeneous), "heterogeneous_ratio": float(heterogeneous),
                "interaction_ratio": float(interaction),
                "log_interaction": math.log(float(interaction)),
                "direct_and_adjusted_positive": heterogeneous > 1 and interaction > 1,
                "interaction_ratio_exact": {"numerator": interaction.numerator, "denominator": interaction.denominator},
            },
        })
    except _InvalidCampaign as error:
        result.update({"verdict": error.verdict, "reason_code": error.code, "detail": error.detail})
    except (OSError, ValueError, TypeError, OverflowError) as error:
        result.update({"reason_code": "validator_input_error", "detail": str(error)})
    return result


def validate_w16_cpu_campaign_v7(blocks: Sequence[Sequence[Path]]) -> dict[str, object]:
    """Validate exactly six v7 F/R blocks and the frozen direction gate."""

    result: dict[str, object] = {
        "schema_version": 1, "kind": "kauri-w16-cpu-campaign-validation-v7",
        "verdict": "INCOMPLETE", "claim_eligible": False, "thesis_result_eligible": False,
        "figure_eligible": False, "technical_improvement_gate_passed": False,
    }
    try:
        if isinstance(blocks, (str, bytes)) or len(blocks) != 6:
            _fail("campaign_cardinality", "campaign requires exactly six four-cell blocks")
        validated = [validate_w16_campaign_block_v7(roots) for roots in blocks]
        for index, block in enumerate(validated, 1):
            if block.get("verdict") != "PASS":
                _fail("block_validation", f"block {index} did not pass: {block.get('reason_code')}: {block.get('detail')}", verdict="FAIL" if block.get("verdict") == "FAIL" else "INCOMPLETE")
            if block.get("block_index") != index or block.get("execution_order") != ("forward" if index % 2 else "reverse"):
                _fail("block_order", f"block {index} contradicts the frozen F/R design")
        shared = ("campaign_id", "campaign_freeze_sha256", "approval_ref", "revision", "profile_sha256", "binary_sha256")
        if any(any(block[key] != validated[0][key] for block in validated[1:]) for key in shared):
            _fail("cross_block_identity", "campaign blocks differ in identity")
        cells = [cell for block in validated for cell in block["cells"]]
        if len(cells) != 24 or len({cell["root"] for cell in cells}) != 24 or len({cell["run_id"] for cell in cells}) != 24:
            _fail("campaign_cardinality", "campaign requires 24 distinct sealed roots and run IDs")
        for previous, current in zip(cells, cells[1:]):
            if int(current["started_raw_monotonic_ns"]) <= int(previous["ended_raw_monotonic_ns"]):
                _fail("campaign_chronology", "campaign cells overlap or violate schedule")
        interactions = [Fraction(int(block["effects"]["interaction_ratio_exact"]["numerator"]), int(block["effects"]["interaction_ratio_exact"]["denominator"])) for block in validated]
        positives = [bool(block["effects"]["direct_and_adjusted_positive"]) for block in validated]
        forward_positive, reverse_positive = sum(positives[::2]), sum(positives[1::2])
        product = math.prod(interactions)
        gate = sum(positives) >= 5 and forward_positive >= 2 and reverse_positive >= 2 and product >= Fraction(11, 10) ** 6
        logs = [math.log(float(value)) for value in interactions]
        ordered_logs = sorted(logs)
        result.update({
            "verdict": "PASS", "reason_code": "campaign_technically_complete", "campaign_id": validated[0]["campaign_id"],
            "campaign_freeze_sha256": validated[0]["campaign_freeze_sha256"], "approval_ref": validated[0]["approval_ref"],
            "revision": validated[0]["revision"], "profile_sha256": validated[0]["profile_sha256"],
            "binary_sha256": validated[0]["binary_sha256"], "blocks": validated,
            "technical_improvement_gate_passed": gate,
            "direction_gate": {
                "status": "POSITIVE" if gate else "OBSERVED_NEGATIVE_OR_MIXED", "positive_block_count": sum(positives),
                "forward_positive_count": forward_positive, "reverse_positive_count": reverse_positive,
                "required_positive_blocks": 5, "required_positive_per_order": 2,
                "required_geometric_mean_adjusted": 1.10, "log_interactions": logs,
                "median_log_interaction": (ordered_logs[2] + ordered_logs[3]) / 2,
                "range_log_interaction": [ordered_logs[0], ordered_logs[-1]],
                "geometric_mean_adjusted_ratio": math.exp(math.fsum(logs) / 6),
                "product_adjusted_ratio_exact": {"numerator": product.numerator, "denominator": product.denominator},
            },
        })
    except _InvalidCampaign as error:
        result.update({"verdict": error.verdict, "reason_code": error.code, "detail": error.detail})
    except (OSError, ValueError, TypeError, OverflowError) as error:
        result.update({"reason_code": "validator_input_error", "detail": str(error)})
    return result
