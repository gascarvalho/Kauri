"""Read-only, prospective validation of the counterbalanced W16 CPU study.

This module reconstructs each cell through the v6 raw validator.  A PASS here
is technical completeness only: scheduler identity and durable archiving are
external evidence gates, so this module never makes a thesis/figure claim.
"""

from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from .w16_output_validator import validate_w16_output_v6


FORWARD = (
    "slow-roots:homogeneous", "fast-roots:homogeneous",
    "slow-roots:heterogeneous", "fast-roots:heterogeneous",
)
REVERSE = tuple(reversed(FORWARD))
_LABELS = frozenset(FORWARD)
_CELL_KIND = "kauri-w16-output-validation-v6"


class _InvalidCampaign(RuntimeError):
    def __init__(self, code: str, detail: str, *, verdict: str = "INCOMPLETE") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.verdict = verdict


def _fail(code: str, detail: str, *, verdict: str = "INCOMPLETE") -> None:
    raise _InvalidCampaign(code, detail, verdict=verdict)


def _read_unchanged(path: Path, before: bytes, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        _fail("sealed_cell", f"{label} is not a regular file")
    if path.read_bytes() != before:
        _fail("cell_changed_during_validation", f"{label} changed while validating")
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
    return (
        isinstance(value, str) and len(value) == size
        and all(character in "0123456789abcdef" for character in value)
    )


def _one_cell(root: Path, *, ordinal: int, expected_label: str) -> dict[str, object]:
    receipt_path = root / "feasibility-receipt.json"
    authorization_path = root / "authorization.json"
    if (
        receipt_path.is_symlink() or not receipt_path.is_file()
        or authorization_path.is_symlink() or not authorization_path.is_file()
    ):
        _fail("sealed_cell", f"cell {ordinal} lacks a regular receipt/authorization")
    receipt_before = receipt_path.read_bytes()
    authorization_before = authorization_path.read_bytes()
    validation = validate_w16_output_v6(root)
    receipt = _read_unchanged(receipt_path, receipt_before, f"cell {ordinal} receipt")
    authorization = _read_unchanged(
        authorization_path, authorization_before, f"cell {ordinal} authorization"
    )
    if not isinstance(validation, Mapping):
        _fail("cell_validation", f"cell {ordinal} produced no validation result")
    verdict = validation.get("verdict")
    if verdict != "PASS":
        _fail(
            "cell_validation", f"cell {ordinal} failed raw validation: "
            f"{validation.get('reason_code')}: {validation.get('detail')}",
            verdict="FAIL" if verdict == "FAIL" else "INCOMPLETE",
        )
    if (
        validation.get("schema_version") != 1
        or validation.get("kind") != _CELL_KIND
        or validation.get("evidence_class") != "CPU_QUOTA_SINGLE_ARM"
        or validation.get("claim_eligible") is not False
        or validation.get("figure_eligible") is not False
    ):
        _fail("cell_validation_contract", f"cell {ordinal} is not a bounded v6 CPU cell")
    preflight = _mapping(receipt.get("preflight"), "receipt preflight")
    binaries = _mapping(preflight.get("binary_sha256"), "preflight binaries")
    throughput = _mapping(validation.get("throughput"), "validated throughput")
    lifecycle = _mapping(validation.get("native_lifecycle_span"), "validated native lifecycle")
    raw_span = _mapping(validation.get("producer_raw_clock_span"), "producer RAW clock span")
    quota = _mapping(validation.get("quota"), "validated CPU quota")
    auth_summary = _mapping(validation.get("authorization"), "validated authorization")
    start = receipt.get("started_monotonic_ns")
    end = receipt.get("ended_monotonic_ns")
    transactions = throughput.get("transaction_count")
    duration = throughput.get("duration_ns")
    milli_tps = throughput.get("throughput_milli_tps")
    window_start = throughput.get("window_start_monotonic_ns")
    window_end = throughput.get("window_end_monotonic_ns")
    lifecycle_start = lifecycle.get("start_monotonic_ns")
    lifecycle_end = lifecycle.get("end_monotonic_ns")
    raw_start = receipt.get("started_raw_monotonic_ns")
    raw_end = receipt.get("ended_raw_monotonic_ns")
    label = f"{authorization.get('arm')}:{authorization.get('quota_mode')}"
    if (
        receipt.get("schema") != "kauri-n31-static-e0-local-executor-v2"
        or receipt.get("attempts") != 1
        or receipt.get("retries") != 0
        or receipt.get("required_complete_cycles") != 5
        or not isinstance(receipt.get("run_id"), str)
        or not receipt["run_id"]
        or not _positive_int(start) or not _positive_int(end)
        or int(end) <= int(start)
        or receipt.get("raw_clock_id") != "CLOCK_MONOTONIC_RAW"
        or not _positive_int(raw_start) or not _positive_int(raw_end)
        or int(raw_end) <= int(raw_start)
        or raw_span != {
            "clock_id": "CLOCK_MONOTONIC_RAW",
            "start_monotonic_ns": raw_start,
            "end_monotonic_ns": raw_end,
        }
        or validation.get("run_id") != receipt["run_id"]
        or validation.get("revision") != preflight.get("revision")
        or label != expected_label
        or validation.get("arm") != authorization.get("arm")
        or quota.get("mode") != authorization.get("quota_mode")
        or authorization.get("cell_ordinal") != ordinal
        or auth_summary.get("cell_ordinal") != ordinal
        or auth_summary.get("block_id") != authorization.get("block_id")
        or auth_summary.get("campaign_id") != authorization.get("campaign_id")
        or auth_summary.get("block_index") != authorization.get("block_index")
        or auth_summary.get("campaign_freeze_sha256")
        != authorization.get("campaign_freeze_sha256")
        or authorization.get("revision") != preflight.get("revision")
        or authorization.get("profile_sha256") != preflight.get("profile_sha256")
        or authorization.get("binary_sha256") != binaries
        or authorization.get("required_complete_cycles") != 5
        or authorization.get("hard_timeout_s") != 480
        or authorization.get("external_timeout_s") != 720
        or authorization.get("automatic_retries") != 0
        or authorization.get("claim_eligible") is not False
        or authorization.get("figure_eligible") is not False
        or not _positive_int(transactions) or not _positive_int(duration)
        or not _positive_int(milli_tps)
        or not _positive_int(window_start) or not _positive_int(window_end)
        or not int(raw_start) <= int(window_start) < int(window_end) <= int(raw_end)
        or set(lifecycle) != {"start_monotonic_ns", "end_monotonic_ns"}
        or not _positive_int(lifecycle_start) or not _positive_int(lifecycle_end)
        or not int(raw_start) <= int(lifecycle_start) < int(window_start)
        or not int(window_end) < int(lifecycle_end) <= int(raw_end)
        or int(duration) != int(window_end) - int(window_start)
        or int(milli_tps) != int(transactions) * 1_000_000_000_000 // int(duration)
    ):
        _fail("cell_contract", f"cell {ordinal} differs from its frozen five-cycle contract")
    revision = preflight.get("revision")
    profile = preflight.get("profile_sha256")
    if (
        not _hex(revision, 40) or not _hex(profile, 64)
        or set(binaries) != {"app", "keygen", "tls_keygen", "native_digest"}
        or any(not _hex(value, 64) for value in binaries.values())
        or not _hex(authorization.get("campaign_freeze_sha256"), 64)
    ):
        _fail("cell_identity", f"cell {ordinal} has malformed shared identity")
    return {
        "ordinal": ordinal, "label": label, "root": str(root),
        "run_id": receipt["run_id"],
        "started_monotonic_ns": int(start), "ended_monotonic_ns": int(end),
        "started_raw_monotonic_ns": int(raw_start),
        "ended_raw_monotonic_ns": int(raw_end),
        "window_start_monotonic_ns": int(window_start),
        "window_end_monotonic_ns": int(window_end),
        "native_lifecycle_start_monotonic_ns": int(lifecycle_start),
        "native_lifecycle_end_monotonic_ns": int(lifecycle_end),
        "revision": revision, "profile_sha256": profile,
        "binary_sha256": dict(binaries),
        "campaign_id": authorization["campaign_id"],
        "block_index": authorization["block_index"],
        "block_id": authorization["block_id"],
        "block_order": authorization["block_order"],
        "campaign_freeze_sha256": authorization["campaign_freeze_sha256"],
        "approval_ref": authorization["approval_ref"],
        "approved_at_utc": authorization["approved_at_utc"],
        "authorization_sha256": hashlib.sha256(authorization_before).hexdigest(),
        "transaction_count": int(transactions), "duration_ns": int(duration),
        "throughput_milli_tps": int(milli_tps),
        "throughput_tps_display": float(
            Fraction(int(transactions) * 1_000_000_000, int(duration))
        ),
        "required_branch_incomplete": validation.get("required_branch_incomplete"),
        "delta_success_triplets": validation.get("delta_success_triplets"),
    }


def validate_w16_campaign_block(roots: Sequence[Path]) -> dict[str, object]:
    """Validate one forward/reverse four-cell block; never grant a claim."""

    result: dict[str, object] = {
        "schema_version": 1, "kind": "kauri-w16-cpu-campaign-block-validation-v1",
        "verdict": "INCOMPLETE", "claim_eligible": False,
        "thesis_result_eligible": False, "figure_eligible": False,
    }
    try:
        if isinstance(roots, (str, bytes)) or len(roots) != 4:
            _fail("block_cardinality", "campaign block requires exactly four ordered roots")
        resolved: list[Path] = []
        for ordinal, candidate in enumerate(roots, 1):
            path = Path(candidate)
            if path.is_symlink() or not path.is_dir():
                _fail("output_root", f"cell {ordinal} is not a regular directory")
            resolved.append(path.resolve())
        if len(set(resolved)) != 4:
            _fail("block_cardinality", "campaign block roots must be distinct")
        first_auth = _read_unchanged(
            resolved[0] / "authorization.json",
            (resolved[0] / "authorization.json").read_bytes(),
            "first-cell authorization",
        )
        block_index = first_auth.get("block_index")
        if type(block_index) is not int or block_index not in range(1, 7):
            _fail("block_identity", "campaign block index must be 1 through 6")
        order = FORWARD if block_index % 2 else REVERSE
        cells = [
            _one_cell(root, ordinal=ordinal, expected_label=label)
            for ordinal, (root, label) in enumerate(zip(resolved, order, strict=True), 1)
        ]
        if len({cell["run_id"] for cell in cells}) != 4:
            _fail("duplicate_run_id", "campaign block run IDs must be distinct")
        for previous, current in zip(cells, cells[1:]):
            if (
                int(current["started_monotonic_ns"])
                <= int(previous["ended_monotonic_ns"])
                or int(current["started_raw_monotonic_ns"])
                <= int(previous["ended_raw_monotonic_ns"])
                or int(current["window_start_monotonic_ns"])
                <= int(previous["window_end_monotonic_ns"])
                or int(current["native_lifecycle_start_monotonic_ns"])
                <= int(previous["native_lifecycle_end_monotonic_ns"])
            ):
                _fail("cell_chronology", "campaign block cells overlap or violate order")
        shared = (
            "revision", "profile_sha256", "binary_sha256", "campaign_id",
            "block_index", "block_id", "campaign_freeze_sha256", "approval_ref",
        )
        for key in shared:
            if any(cell[key] != cells[0][key] for cell in cells[1:]):
                _fail("cross_cell_identity", f"campaign block differs in {key}")
        if any(cell["block_order"] != list(order) for cell in cells):
            _fail("cell_order", "campaign block authorization order drifted")
        rates = {
            cell["label"]: Fraction(
                int(cell["transaction_count"]), int(cell["duration_ns"])
            ) for cell in cells
        }
        if set(rates) != _LABELS:
            _fail("cell_order", "campaign block is missing a treatment label")
        homogeneous = (
            rates["fast-roots:homogeneous"] / rates["slow-roots:homogeneous"]
        )
        heterogeneous = (
            rates["fast-roots:heterogeneous"] / rates["slow-roots:heterogeneous"]
        )
        interaction = heterogeneous / homogeneous
        d = math.log1p(
            (interaction.numerator - interaction.denominator) / interaction.denominator
        )
        result.update({
            "verdict": "PASS", "reason_code": "campaign_block_complete",
            "execution_order": "forward" if block_index % 2 else "reverse",
            "block_index": block_index, "block_id": cells[0]["block_id"],
            "campaign_id": cells[0]["campaign_id"],
            "campaign_freeze_sha256": cells[0]["campaign_freeze_sha256"],
            "approval_ref": cells[0]["approval_ref"],
            "revision": cells[0]["revision"],
            "profile_sha256": cells[0]["profile_sha256"],
            "binary_sha256": cells[0]["binary_sha256"],
            "cells": cells,
            "effects": {
                "definition": "log(T_BX/T_AX)-log(T_BH/T_AH)",
                "rate_source": "exact transaction_count/duration_ns fractions",
                "ratio_unit": "dimensionless",
                "cell_display_unit": "transactions_per_second",
                "homogeneous_ratio": float(homogeneous),
                "heterogeneous_ratio": float(heterogeneous),
                "interaction_ratio": float(interaction),
                "log_interaction": d,
                "direct_and_adjusted_positive": heterogeneous > 1 and interaction > 1,
                "homogeneous_ratio_exact": {
                    "numerator": homogeneous.numerator,
                    "denominator": homogeneous.denominator,
                },
                "heterogeneous_ratio_exact": {
                    "numerator": heterogeneous.numerator,
                    "denominator": heterogeneous.denominator,
                },
                "interaction_ratio_exact": {
                    "numerator": interaction.numerator,
                    "denominator": interaction.denominator,
                },
            },
        })
    except _InvalidCampaign as error:
        result.update({"verdict": error.verdict, "reason_code": error.code, "detail": error.detail})
    except (OSError, ValueError, TypeError, OverflowError) as error:
        result.update({"reason_code": "validator_input_error", "detail": str(error)})
    return result


def validate_w16_cpu_campaign(blocks: Sequence[Sequence[Path]]) -> dict[str, object]:
    """Validate exactly six F/R blocks and calculate the frozen direction gate."""

    result: dict[str, object] = {
        "schema_version": 1, "kind": "kauri-w16-cpu-campaign-validation-v1",
        "verdict": "INCOMPLETE", "claim_eligible": False,
        "thesis_result_eligible": False, "figure_eligible": False,
        "technical_improvement_gate_passed": False,
        "limitations": [
            "Static Epoch-0 placement is not an adaptive reputation or Epoch-1 result.",
            "Same-host exclusive booking and durable archive need external audit.",
            "A raw-root validator alone cannot make a thesis or figure claim.",
        ],
    }
    try:
        if isinstance(blocks, (str, bytes)) or len(blocks) != 6:
            _fail("campaign_cardinality", "campaign requires exactly six four-cell blocks")
        validated = [validate_w16_campaign_block(roots) for roots in blocks]
        for index, block in enumerate(validated, 1):
            if block.get("verdict") != "PASS":
                _fail(
                    "block_validation", f"block {index} did not pass: "
                    f"{block.get('reason_code')}: {block.get('detail')}",
                    verdict="FAIL" if block.get("verdict") == "FAIL" else "INCOMPLETE",
                )
            if block.get("block_index") != index:
                _fail("block_order", f"block {index} has the wrong frozen index")
            if block.get("execution_order") != ("forward" if index % 2 else "reverse"):
                _fail("block_order", f"block {index} has the wrong execution direction")
        shared = (
            "campaign_id", "campaign_freeze_sha256", "approval_ref", "revision",
            "profile_sha256", "binary_sha256",
        )
        for key in shared:
            if any(block[key] != validated[0][key] for block in validated[1:]):
                _fail("cross_block_identity", f"campaign blocks differ in {key}")
        cells = [cell for block in validated for cell in block["cells"]]
        if len(cells) != 24 or len({cell["root"] for cell in cells}) != 24:
            _fail("campaign_cardinality", "campaign requires 24 distinct sealed roots")
        if len({cell["run_id"] for cell in cells}) != 24:
            _fail("duplicate_run_id", "campaign has a reused run ID")
        for previous, current in zip(cells, cells[1:]):
            if (
                int(current["started_monotonic_ns"])
                <= int(previous["ended_monotonic_ns"])
                or int(current["started_raw_monotonic_ns"])
                <= int(previous["ended_raw_monotonic_ns"])
                or int(current["window_start_monotonic_ns"])
                <= int(previous["window_end_monotonic_ns"])
                or int(current["native_lifecycle_start_monotonic_ns"])
                <= int(previous["native_lifecycle_end_monotonic_ns"])
            ):
                _fail("campaign_chronology", "campaign cells overlap or violate schedule")
        ds = [float(block["effects"]["log_interaction"]) for block in validated]
        positives = [bool(block["effects"]["direct_and_adjusted_positive"]) for block in validated]
        forward_positive = sum(positives[::2])
        reverse_positive = sum(positives[1::2])
        exact_interactions = [
            Fraction(
                int(block["effects"]["interaction_ratio_exact"]["numerator"]),
                int(block["effects"]["interaction_ratio_exact"]["denominator"]),
            ) for block in validated
        ]
        product = math.prod(exact_interactions)
        geometric_mean_adjusted = math.exp(math.fsum(ds) / 6)
        gate = (
            sum(positives) >= 5 and forward_positive >= 2
            and reverse_positive >= 2 and product >= Fraction(11, 10) ** 6
        )
        sorted_ds = sorted(ds)
        result.update({
            "verdict": "PASS", "reason_code": "campaign_technically_complete",
            "campaign_id": validated[0]["campaign_id"],
            "campaign_freeze_sha256": validated[0]["campaign_freeze_sha256"],
            "approval_ref": validated[0]["approval_ref"],
            "revision": validated[0]["revision"],
            "profile_sha256": validated[0]["profile_sha256"],
            "binary_sha256": validated[0]["binary_sha256"],
            "blocks": validated,
            "technical_improvement_gate_passed": gate,
            "direction_gate": {
                "status": "POSITIVE" if gate else "OBSERVED_NEGATIVE_OR_MIXED",
                "positive_block_count": sum(positives),
                "forward_positive_count": forward_positive,
                "reverse_positive_count": reverse_positive,
                "required_positive_blocks": 5,
                "required_positive_per_order": 2,
                "required_geometric_mean_adjusted": 1.10,
                "log_interactions": ds,
                "median_log_interaction": (sorted_ds[2] + sorted_ds[3]) / 2,
                "range_log_interaction": [sorted_ds[0], sorted_ds[-1]],
                "geometric_mean_adjusted_ratio": geometric_mean_adjusted,
                "product_adjusted_ratio_exact": {
                    "numerator": product.numerator,
                    "denominator": product.denominator,
                },
            },
        })
    except _InvalidCampaign as error:
        result.update({"verdict": error.verdict, "reason_code": error.code, "detail": error.detail})
    except (OSError, ValueError, TypeError, OverflowError) as error:
        result.update({"reason_code": "validator_input_error", "detail": str(error)})
    return result
