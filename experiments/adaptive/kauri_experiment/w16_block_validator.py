"""Read-only validation of one complete exploratory W16 four-cell block.

Each cell is first reconstructed by the independent W16 v4 cell validator.
This module then checks the frozen block identity and computes only the
predeclared topology-by-quota directional interaction.  A complete block is
still exploratory: neither a positive direction nor a ``PASS`` verdict creates
a campaign, thesis, or figure claim.
"""

from __future__ import annotations

import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Mapping, Sequence

from .w16_output_validator import validate_w16_output_v4


KIND = "kauri-w16-four-cell-block-validation-v3"
CELL_KIND = "kauri-w16-output-validation-v4"
ORDER = (
    ("slow-roots", "homogeneous"),
    ("fast-roots", "homogeneous"),
    ("slow-roots", "heterogeneous"),
    ("fast-roots", "heterogeneous"),
)
ORDER_LABELS = tuple(f"{arm}:{mode}" for arm, mode in ORDER)


class _InvalidBlock(RuntimeError):
    def __init__(self, code: str, detail: str, *, verdict: str = "INCOMPLETE") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.verdict = verdict


def _fail(code: str, detail: str, *, verdict: str = "INCOMPLETE") -> None:
    raise _InvalidBlock(code, detail, verdict=verdict)


def _read_stable_json(path: Path, before: bytes, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        _fail("sealed_cell", f"{label} is not a regular file")
    after = path.read_bytes()
    if after != before:
        _fail("cell_changed_during_validation", f"{label} changed during validation")
    try:
        value = json.loads(after)
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("sealed_cell", f"{label} is malformed JSON: {exc}")
    if not isinstance(value, dict):
        _fail("sealed_cell", f"{label} must contain one object")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail("cell_contract", f"{label} must be an object")
    return value


def _positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


def _nonnegative_integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _required_branch_diagnostics(
    value: object, *, expected_ordinal: int,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _fail(
            "cell_diagnostics_contract",
            f"cell {expected_ordinal} lacks required-branch diagnostics",
        )
    diagnostics = _mapping(
        value, f"cell {expected_ordinal} required-branch diagnostics"
    )
    expected_keys = {
        "schema_version", "event_type", "total_count",
        "pre_measurement_count", "in_measurement_count",
        "post_measurement_count", "gap_count", "missing_signer_count",
        "by_replica", "by_tree", "by_direct_child",
    }
    if set(diagnostics) != expected_keys:
        _fail(
            "cell_diagnostics_contract",
            f"cell {expected_ordinal} required-branch diagnostics schema drifted",
        )
    counts = (
        diagnostics.get("total_count"),
        diagnostics.get("pre_measurement_count"),
        diagnostics.get("in_measurement_count"),
        diagnostics.get("post_measurement_count"),
    )
    by_replica = diagnostics.get("by_replica")
    gap_count = diagnostics.get("gap_count")
    missing_signer_count = diagnostics.get("missing_signer_count")
    by_tree = diagnostics.get("by_tree")
    by_direct_child = diagnostics.get("by_direct_child")
    if (
        diagnostics.get("schema_version") != 1
        or diagnostics.get("event_type")
        != "aggregation.required_branch_incomplete"
        or any(not _nonnegative_integer(count) for count in counts)
        or not _nonnegative_integer(gap_count)
        or not _nonnegative_integer(missing_signer_count)
        or not isinstance(by_replica, list)
        or not isinstance(by_tree, list)
        or not isinstance(by_direct_child, list)
        or int(counts[0]) != sum(int(count) for count in counts[1:])
        or int(gap_count) < int(counts[0])
        or int(missing_signer_count) < int(gap_count)
    ):
        _fail(
            "cell_diagnostics_contract",
            f"cell {expected_ordinal} required-branch diagnostics are malformed",
        )

    normalized_replicas: list[dict[str, int]] = []
    previous_replica = -1
    for raw_entry in by_replica:
        entry = _mapping(
            raw_entry, f"cell {expected_ordinal} per-replica diagnostics"
        )
        if set(entry) != {
            "replica_id", "count", "pre_measurement_count",
            "in_measurement_count", "post_measurement_count",
        }:
            _fail(
                "cell_diagnostics_contract",
                f"cell {expected_ordinal} per-replica diagnostics schema drifted",
            )
        replica_id = entry.get("replica_id")
        replica_counts = (
            entry.get("count"),
            entry.get("pre_measurement_count"),
            entry.get("in_measurement_count"),
            entry.get("post_measurement_count"),
        )
        if (
            type(replica_id) is not int
            or replica_id < 0 or replica_id >= 31
            or replica_id <= previous_replica
            or not _positive_integer(replica_counts[0])
            or any(not _nonnegative_integer(count) for count in replica_counts[1:])
            or int(replica_counts[0])
            != sum(int(count) for count in replica_counts[1:])
        ):
            _fail(
                "cell_diagnostics_contract",
                f"cell {expected_ordinal} per-replica diagnostics are malformed",
            )
        normalized_replicas.append({
            "replica_id": replica_id,
            "count": int(replica_counts[0]),
            "pre_measurement_count": int(replica_counts[1]),
            "in_measurement_count": int(replica_counts[2]),
            "post_measurement_count": int(replica_counts[3]),
        })
        previous_replica = replica_id

    normalized_trees: list[dict[str, int]] = []
    previous_tree = -1
    for raw_entry in by_tree:
        entry = _mapping(
            raw_entry, f"cell {expected_ordinal} per-tree diagnostics"
        )
        if set(entry) != {
            "tree_id", "event_count", "pre_measurement_count",
            "in_measurement_count", "post_measurement_count",
        }:
            _fail(
                "cell_diagnostics_contract",
                f"cell {expected_ordinal} per-tree diagnostics schema drifted",
            )
        tree_id = entry.get("tree_id")
        tree_counts = (
            entry.get("event_count"),
            entry.get("pre_measurement_count"),
            entry.get("in_measurement_count"),
            entry.get("post_measurement_count"),
        )
        if (
            type(tree_id) is not int
            or tree_id < 0 or tree_id >= 21
            or tree_id <= previous_tree
            or not _positive_integer(tree_counts[0])
            or any(not _nonnegative_integer(count) for count in tree_counts[1:])
            or int(tree_counts[0])
            != sum(int(count) for count in tree_counts[1:])
        ):
            _fail(
                "cell_diagnostics_contract",
                f"cell {expected_ordinal} per-tree diagnostics are malformed",
            )
        normalized_trees.append({
            "tree_id": tree_id,
            "event_count": int(tree_counts[0]),
            "pre_measurement_count": int(tree_counts[1]),
            "in_measurement_count": int(tree_counts[2]),
            "post_measurement_count": int(tree_counts[3]),
        })
        previous_tree = tree_id

    normalized_children: list[dict[str, int]] = []
    previous_child = -1
    for raw_entry in by_direct_child:
        entry = _mapping(
            raw_entry, f"cell {expected_ordinal} per-child diagnostics"
        )
        if set(entry) != {
            "replica_id", "gap_count", "missing_signer_count",
            "pre_measurement_gap_count", "in_measurement_gap_count",
            "post_measurement_gap_count",
        }:
            _fail(
                "cell_diagnostics_contract",
                f"cell {expected_ordinal} per-child diagnostics schema drifted",
            )
        child_id = entry.get("replica_id")
        child_counts = (
            entry.get("gap_count"),
            entry.get("pre_measurement_gap_count"),
            entry.get("in_measurement_gap_count"),
            entry.get("post_measurement_gap_count"),
        )
        child_missing = entry.get("missing_signer_count")
        if (
            type(child_id) is not int
            or child_id < 0 or child_id >= 31
            or child_id <= previous_child
            or not _positive_integer(child_counts[0])
            or not _positive_integer(child_missing)
            or int(child_missing) < int(child_counts[0])
            or any(not _nonnegative_integer(count) for count in child_counts[1:])
            or int(child_counts[0])
            != sum(int(count) for count in child_counts[1:])
        ):
            _fail(
                "cell_diagnostics_contract",
                f"cell {expected_ordinal} per-child diagnostics are malformed",
            )
        normalized_children.append({
            "replica_id": child_id,
            "gap_count": int(child_counts[0]),
            "missing_signer_count": int(child_missing),
            "pre_measurement_gap_count": int(child_counts[1]),
            "in_measurement_gap_count": int(child_counts[2]),
            "post_measurement_gap_count": int(child_counts[3]),
        })
        previous_child = child_id

    if (
        sum(entry["count"] for entry in normalized_replicas) != int(counts[0])
        or sum(entry["pre_measurement_count"] for entry in normalized_replicas)
        != int(counts[1])
        or sum(entry["in_measurement_count"] for entry in normalized_replicas)
        != int(counts[2])
        or sum(entry["post_measurement_count"] for entry in normalized_replicas)
        != int(counts[3])
        or sum(entry["event_count"] for entry in normalized_trees)
        != int(counts[0])
        or sum(entry["pre_measurement_count"] for entry in normalized_trees)
        != int(counts[1])
        or sum(entry["in_measurement_count"] for entry in normalized_trees)
        != int(counts[2])
        or sum(entry["post_measurement_count"] for entry in normalized_trees)
        != int(counts[3])
        or sum(entry["gap_count"] for entry in normalized_children)
        != int(gap_count)
        or sum(entry["missing_signer_count"] for entry in normalized_children)
        != int(missing_signer_count)
    ):
        _fail(
            "cell_diagnostics_contract",
            f"cell {expected_ordinal} per-replica diagnostics do not sum to totals",
        )
    return {
        "schema_version": 1,
        "event_type": "aggregation.required_branch_incomplete",
        "total_count": int(counts[0]),
        "pre_measurement_count": int(counts[1]),
        "in_measurement_count": int(counts[2]),
        "post_measurement_count": int(counts[3]),
        "gap_count": int(gap_count),
        "missing_signer_count": int(missing_signer_count),
        "by_replica": normalized_replicas,
        "by_tree": normalized_trees,
        "by_direct_child": normalized_children,
    }


def _delta_success_diagnostics(
    value: object, *, expected_ordinal: int,
) -> dict[str, object]:
    """Check the complete, phase-classified delta-triplet summary."""

    if not isinstance(value, Mapping):
        _fail("cell_diagnostics_contract", f"cell {expected_ordinal} lacks delta diagnostics")
    expected = {
        "schema_version", "event_type", "total_count", "signer_count",
        "pre_measurement_count", "in_measurement_count",
        "post_measurement_count", "by_replica", "by_tree",
    }
    if set(value) != expected:
        _fail("cell_diagnostics_contract", f"cell {expected_ordinal} delta schema drifted")
    total = value.get("total_count")
    signers = value.get("signer_count")
    phases = tuple(value.get(key) for key in (
        "pre_measurement_count", "in_measurement_count", "post_measurement_count",
    ))
    if (
        value.get("schema_version") != 1
        or value.get("event_type") != "aggregation.delta_success_triplet"
        or not _nonnegative_integer(total)
        or not _nonnegative_integer(signers)
        or any(not _nonnegative_integer(count) for count in phases)
        or int(total) != sum(int(count) for count in phases)
        or int(signers) < int(total)
    ):
        _fail("cell_diagnostics_contract", f"cell {expected_ordinal} delta totals are malformed")

    def rows_for(label: str, id_key: str, limit: int) -> list[dict[str, int]]:
        raw_rows = value.get(label)
        if not isinstance(raw_rows, list):
            _fail("cell_diagnostics_contract", f"cell {expected_ordinal} {label} is not an array")
        normalized: list[dict[str, int]] = []
        previous = -1
        for raw in raw_rows:
            if not isinstance(raw, Mapping) or set(raw) != {
                id_key, "triplet_count", "signer_count",
                "pre_measurement_count", "in_measurement_count",
                "post_measurement_count",
            }:
                _fail("cell_diagnostics_contract", f"cell {expected_ordinal} {label} row schema drifted")
            identifier = raw.get(id_key)
            triplets = raw.get("triplet_count")
            row_signers = raw.get("signer_count")
            row_phases = tuple(raw.get(key) for key in (
                "pre_measurement_count", "in_measurement_count", "post_measurement_count",
            ))
            if (
                type(identifier) is not int or identifier <= previous
                or identifier < 0 or identifier >= limit
                or not _positive_integer(triplets)
                or not _positive_integer(row_signers)
                or int(row_signers) < int(triplets)
                or any(not _nonnegative_integer(count) for count in row_phases)
                or int(triplets) != sum(int(count) for count in row_phases)
            ):
                _fail("cell_diagnostics_contract", f"cell {expected_ordinal} {label} row is malformed")
            normalized.append({
                id_key: identifier, "triplet_count": int(triplets),
                "signer_count": int(row_signers),
                "pre_measurement_count": int(row_phases[0]),
                "in_measurement_count": int(row_phases[1]),
                "post_measurement_count": int(row_phases[2]),
            })
            previous = identifier
        if (
            sum(row["triplet_count"] for row in normalized) != int(total)
            or sum(row["signer_count"] for row in normalized) != int(signers)
            or any(sum(row[key] for row in normalized) != int(expected_count)
                   for key, expected_count in zip((
                       "pre_measurement_count", "in_measurement_count",
                       "post_measurement_count",
                   ), phases, strict=True))
        ):
            _fail("cell_diagnostics_contract", f"cell {expected_ordinal} {label} does not sum to totals")
        return normalized

    return {
        "schema_version": 1,
        "event_type": "aggregation.delta_success_triplet",
        "total_count": int(total),
        "signer_count": int(signers),
        "pre_measurement_count": int(phases[0]),
        "in_measurement_count": int(phases[1]),
        "post_measurement_count": int(phases[2]),
        "by_replica": rows_for("by_replica", "replica_id", 31),
        "by_tree": rows_for("by_tree", "tree_id", 21),
    }


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, allow_nan=False, ensure_ascii=True,
        sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _read_cell_metadata(
    *, root: Path, expected_ordinal: int, expected_arm: str,
    expected_mode: str, validation: Mapping[str, object],
    receipt_before: bytes, authorization_before: bytes,
) -> dict[str, object]:
    receipt = _read_stable_json(
        root / "feasibility-receipt.json", receipt_before,
        f"cell {expected_ordinal} receipt",
    )
    authorization = _read_stable_json(
        root / "authorization.json", authorization_before,
        f"cell {expected_ordinal} authorization",
    )
    if (
        validation.get("schema_version") != 1
        or validation.get("kind") != CELL_KIND
        or validation.get("evidence_class") != "CPU_QUOTA_SINGLE_ARM"
        or validation.get("claim_eligible") is not False
        or validation.get("figure_eligible") is not False
    ):
        _fail(
            "cell_validation_contract",
            f"cell {expected_ordinal} is not a bounded W16 v4 CPU validation",
        )
    verdict = validation.get("verdict")
    if verdict != "PASS":
        propagated = "FAIL" if verdict == "FAIL" else "INCOMPLETE"
        _fail(
            "cell_validation",
            f"cell {expected_ordinal} did not pass independently: "
            f"{validation.get('reason_code')}: {validation.get('detail')}",
            verdict=propagated,
        )

    preflight = _mapping(receipt.get("preflight"), "receipt preflight")
    throughput = _mapping(validation.get("throughput"), "cell throughput")
    quota = _mapping(validation.get("quota"), "cell quota")
    validation_authorization = _mapping(
        validation.get("authorization"), "cell authorization summary"
    )
    required_branch_diagnostics = _required_branch_diagnostics(
        validation.get("required_branch_incomplete"),
        expected_ordinal=expected_ordinal,
    )
    delta_success_diagnostics = _delta_success_diagnostics(
        validation.get("delta_success_triplets"),
        expected_ordinal=expected_ordinal,
    )
    binary_sha256 = _mapping(preflight.get("binary_sha256"), "binary hashes")
    run_id = receipt.get("run_id")
    started_ns = receipt.get("started_monotonic_ns")
    ended_ns = receipt.get("ended_monotonic_ns")
    transaction_count = throughput.get("transaction_count")
    duration_ns = throughput.get("duration_ns")
    milli_tps = throughput.get("throughput_milli_tps")
    if (
        validation.get("arm") != expected_arm
        or quota.get("mode") != expected_mode
        or authorization.get("arm") != expected_arm
        or authorization.get("quota_mode") != expected_mode
        or authorization.get("cell_ordinal") != expected_ordinal
    ):
        _fail(
            "cell_order",
            f"cell {expected_ordinal} is not {ORDER_LABELS[expected_ordinal - 1]}",
        )
    if (
        receipt.get("schema") != "kauri-n31-static-e0-local-executor-v1"
        or receipt.get("attempts") != 1
        or receipt.get("retries") != 0
        or receipt.get("required_complete_cycles") != 5
        or not isinstance(run_id, str) or not run_id
        or not _positive_integer(started_ns)
        or not _positive_integer(ended_ns)
        or int(ended_ns) <= int(started_ns)
        or validation.get("run_id") != run_id
        or validation.get("revision") != preflight.get("revision")
        or authorization.get("block_order") != list(ORDER_LABELS)
        or authorization.get("required_complete_cycles") != 5
        or authorization.get("hard_timeout_s") != 480
        or authorization.get("external_timeout_s") != 720
        or authorization.get("automatic_retries") != 0
        or authorization.get("claim_eligible") is not False
        or authorization.get("figure_eligible") is not False
        or authorization.get("revision") != preflight.get("revision")
        or authorization.get("profile_sha256") != preflight.get("profile_sha256")
        or authorization.get("binary_sha256") != binary_sha256
        or validation_authorization.get("block_id") != authorization.get("block_id")
        or validation_authorization.get("cell_ordinal") != expected_ordinal
        or not _positive_integer(transaction_count)
        or not _positive_integer(duration_ns)
        or not _positive_integer(milli_tps)
        or int(milli_tps)
        != int(transaction_count) * 1_000_000_000_000 // int(duration_ns)
    ):
        _fail(
            "cell_contract",
            f"cell {expected_ordinal} differs from its frozen zero-retry five-cycle contract",
        )
    revision = preflight.get("revision")
    profile_sha256 = preflight.get("profile_sha256")
    block_id = authorization.get("block_id")
    if (
        not isinstance(revision, str) or len(revision) != 40
        or not isinstance(profile_sha256, str) or len(profile_sha256) != 64
        or not isinstance(block_id, str) or not block_id
        or set(binary_sha256) != {"app", "keygen", "tls_keygen", "native_digest"}
        or any(not isinstance(value, str) or len(value) != 64
               for value in binary_sha256.values())
    ):
        _fail("cell_contract", f"cell {expected_ordinal} has malformed shared identity")
    return {
        "ordinal": expected_ordinal,
        "label": ORDER_LABELS[expected_ordinal - 1],
        "root": str(root),
        "run_id": run_id,
        "revision": revision,
        "profile_sha256": profile_sha256,
        "binary_sha256": dict(binary_sha256),
        "block_id": block_id,
        "started_monotonic_ns": int(started_ns),
        "ended_monotonic_ns": int(ended_ns),
        "transaction_count": int(transaction_count),
        "duration_ns": int(duration_ns),
        "throughput_milli_tps": int(milli_tps),
        "required_branch_incomplete": required_branch_diagnostics,
        "delta_success_triplets": delta_success_diagnostics,
        "cell_validation_sha256": _canonical_sha256(validation),
    }


def validate_w16_block(roots: Sequence[Path]) -> dict[str, object]:
    """Validate four frozen cells and calculate the predeclared interaction.

    Input order is evidence: A-homogeneous, B-homogeneous,
    A-heterogeneous, then B-heterogeneous.  The function performs no writes.
    ``PASS`` means the exploratory block is complete, irrespective of effect
    direction.
    """

    result: dict[str, object] = {
        "schema_version": 1,
        "kind": KIND,
        "verdict": "INCOMPLETE",
        "evidence_class": None,
        "claim_eligible": False,
        "figure_eligible": False,
        "campaign_claim": False,
        "limitations": [
            "One exploratory four-cell block is not a repeated campaign estimate.",
            "Static Epoch-0 placement does not demonstrate adaptive reputation or Epoch-1 selection.",
            "Same-host and active exclusive reservation require external attestation; monotonic chronology alone does not prove them.",
            "A positive directional interaction is descriptive and cannot create a thesis figure or campaign claim.",
        ],
    }
    try:
        if isinstance(roots, (str, bytes)) or len(roots) != 4:
            _fail("block_cardinality", "W16 block requires exactly four ordered roots")
        resolved: list[Path] = []
        for ordinal, raw_root in enumerate(roots, 1):
            root = Path(raw_root)
            if root.is_symlink() or not root.is_dir():
                _fail("output_root", f"cell {ordinal} root is not a regular directory")
            resolved.append(root.resolve())
        if len(set(resolved)) != 4:
            _fail("block_cardinality", "W16 block roots must be distinct")

        cells: list[dict[str, object]] = []
        for ordinal, (root, (expected_arm, expected_mode)) in enumerate(
            zip(resolved, ORDER, strict=True), 1
        ):
            receipt_path = root / "feasibility-receipt.json"
            authorization_path = root / "authorization.json"
            if (
                receipt_path.is_symlink() or not receipt_path.is_file()
                or authorization_path.is_symlink() or not authorization_path.is_file()
            ):
                _fail("sealed_cell", f"cell {ordinal} lacks receipt or authorization")
            receipt_before = receipt_path.read_bytes()
            authorization_before = authorization_path.read_bytes()
            validation = validate_w16_output_v4(root)
            if not isinstance(validation, Mapping):
                _fail("cell_validation_contract", f"cell {ordinal} validator returned no object")
            cells.append(_read_cell_metadata(
                root=root, expected_ordinal=ordinal,
                expected_arm=expected_arm, expected_mode=expected_mode,
                validation=validation, receipt_before=receipt_before,
                authorization_before=authorization_before,
            ))

        ordinals = [cell["ordinal"] for cell in cells]
        labels = [cell["label"] for cell in cells]
        run_ids = [cell["run_id"] for cell in cells]
        if ordinals != [1, 2, 3, 4] or labels != list(ORDER_LABELS):
            _fail("cell_order", "cell ordinals or A-H/B-H/A-X/B-X order drifted")
        if len(set(run_ids)) != 4:
            _fail("duplicate_run_id", "four-cell block must contain four unique run IDs")
        for previous, current in zip(cells, cells[1:]):
            if int(current["started_monotonic_ns"]) <= int(previous["ended_monotonic_ns"]):
                _fail(
                    "cell_chronology",
                    "cell execution intervals overlap or do not follow frozen block order",
                )

        shared_fields = ("revision", "profile_sha256", "binary_sha256", "block_id")
        for field in shared_fields:
            if any(cell[field] != cells[0][field] for cell in cells[1:]):
                _fail("cross_cell_identity", f"four-cell block differs in shared {field}")

        a_h, b_h, a_x, b_x = (
            Fraction(int(cell["transaction_count"]), int(cell["duration_ns"]))
            for cell in cells
        )
        homogeneous_ratio_exact = b_h / a_h
        heterogeneous_ratio_exact = b_x / a_x
        interaction_exact = heterogeneous_ratio_exact / homogeneous_ratio_exact
        homogeneous_ratio = float(homogeneous_ratio_exact)
        heterogeneous_ratio = float(heterogeneous_ratio_exact)
        log_interaction = math.log1p(
            (interaction_exact.numerator - interaction_exact.denominator)
            / interaction_exact.denominator
        )
        positive_mechanism = b_x > a_x and interaction_exact > 1
        diagnostic_cells = [
            {
                "ordinal": cell["ordinal"],
                "label": cell["label"],
                **dict(cell["required_branch_incomplete"]),
            }
            for cell in cells
        ]
        delta_cells = [
            {
                "ordinal": cell["ordinal"],
                "label": cell["label"],
                **dict(cell["delta_success_triplets"]),
            }
            for cell in cells
        ]
        result.update({
            "verdict": "PASS",
            "reason_code": "exploratory_block_complete",
            "detail": (
                "Four independently validated cells complete the frozen exploratory block; "
                "the directional result is not a campaign claim."
            ),
            "evidence_class": "CPU_QUOTA_FOUR_CELL_EXPLORATORY_BLOCK",
            "block_id": cells[0]["block_id"],
            "revision": cells[0]["revision"],
            "profile_sha256": cells[0]["profile_sha256"],
            "binary_sha256": cells[0]["binary_sha256"],
            "cells": cells,
            "effects": {
                "definition": "log(T_BX/T_AX)-log(T_BH/T_AH)",
                "throughput_unit": "milli_tps",
                "rate_source": "exact transaction_count/duration_ns fractions",
                "homogeneous_ratio": homogeneous_ratio,
                "homogeneous_ratio_exact": {
                    "numerator": homogeneous_ratio_exact.numerator,
                    "denominator": homogeneous_ratio_exact.denominator,
                },
                "heterogeneous_ratio": heterogeneous_ratio,
                "heterogeneous_ratio_exact": {
                    "numerator": heterogeneous_ratio_exact.numerator,
                    "denominator": heterogeneous_ratio_exact.denominator,
                },
                "log_interaction": log_interaction,
                "interaction_exact": {
                    "numerator": interaction_exact.numerator,
                    "denominator": interaction_exact.denominator,
                },
            },
            "diagnostics": {
                "required_branch_incomplete": {
                    "schema_version": 1,
                    "event_type": "aggregation.required_branch_incomplete",
                    "total_count": sum(
                        int(cell["total_count"]) for cell in diagnostic_cells
                    ),
                    "pre_measurement_count": sum(
                        int(cell["pre_measurement_count"])
                        for cell in diagnostic_cells
                    ),
                    "in_measurement_count": sum(
                        int(cell["in_measurement_count"])
                        for cell in diagnostic_cells
                    ),
                    "post_measurement_count": sum(
                        int(cell["post_measurement_count"])
                        for cell in diagnostic_cells
                    ),
                    "gap_count": sum(
                        int(cell["gap_count"]) for cell in diagnostic_cells
                    ),
                    "missing_signer_count": sum(
                        int(cell["missing_signer_count"])
                        for cell in diagnostic_cells
                    ),
                    "by_cell": diagnostic_cells,
                },
                "delta_success_triplets": {
                    "schema_version": 1,
                    "event_type": "aggregation.delta_success_triplet",
                    "total_count": sum(int(cell["total_count"]) for cell in delta_cells),
                    "signer_count": sum(int(cell["signer_count"]) for cell in delta_cells),
                    "pre_measurement_count": sum(
                        int(cell["pre_measurement_count"]) for cell in delta_cells
                    ),
                    "in_measurement_count": sum(
                        int(cell["in_measurement_count"]) for cell in delta_cells
                    ),
                    "post_measurement_count": sum(
                        int(cell["post_measurement_count"]) for cell in delta_cells
                    ),
                    "by_cell": delta_cells,
                },
            },
            "positive_mechanism": positive_mechanism,
        })
    except _InvalidBlock as exc:
        result["verdict"] = exc.verdict
        result["reason_code"] = exc.code
        result["detail"] = exc.detail
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError) as exc:
        result["verdict"] = "INCOMPLETE"
        result["reason_code"] = "validator_input_error"
        result["detail"] = str(exc) or type(exc).__name__
    return result
