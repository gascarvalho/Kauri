"""Read-only validation of one complete exploratory W16 four-cell block.

Each cell is first reconstructed by the independent W16 v2 cell validator.
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

from .w16_output_validator import validate_w16_output


KIND = "kauri-w16-four-cell-block-validation-v1"
CELL_KIND = "kauri-w16-output-validation-v2"
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
            f"cell {expected_ordinal} is not a bounded W16 v2 CPU validation",
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
            validation = validate_w16_output(root)
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
