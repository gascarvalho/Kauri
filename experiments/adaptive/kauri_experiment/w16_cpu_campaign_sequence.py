"""Frozen, stop-on-first-failure W16 CPU campaign execution contract.

The sequence runner is not a scheduler/host attestor.  Its PASS means that
24 fixed commands exited zero and their independent raw validators passed;
the workspace evidence ledger must separately audit booking, identity, and
archive before any thesis/figure promotion.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Mapping

from . import n31_static_e0_feasibility as feasibility
from .w16_campaign_validator import (
    FORWARD, REVERSE, validate_w16_campaign_block, validate_w16_cpu_campaign,
)
from .w16_output_validator import validate_w16_output_v5


REPOSITORY = Path(__file__).resolve().parents[3]
_FREEZE_KIND = "kauri-w16-cpu-repeat-freeze-v1"
_MANIFEST_KIND = "kauri-w16-cpu-repeat-sequence-manifest-v1"
_FREEZE_KEYS = {
    "schema_version", "kind", "campaign_id", "revision", "host", "booking_id",
    "repository_root", "output_parent", "evidence_dir", "timeout_path",
    "timeout_sha256", "block_orders", "replica_count", "quorum",
    "fanout", "tree_count", "slow_replica_ids", "slow_quota_percent",
    "other_quota_percent", "complete_cycles", "hard_timeout_s",
    "external_timeout_s", "automatic_retries", "positive_blocks_required",
    "positive_per_order_required", "adjusted_gain_numerator",
    "adjusted_gain_denominator", "pilot_excluded",
}
_MANIFEST_KEYS = {
    "schema_version", "kind", "campaign_id", "revision",
    "campaign_freeze_sha256", "approval_ref", "output_parent",
    "evidence_dir", "cells",
}
_CELL_KEYS = {
    "ordinal", "block_index", "block_order", "cell_label",
    "block_cell_ordinal", "output_root", "preflight_path", "preflight_sha256",
    "authorization_path", "authorization_sha256", "hard_timeout_s",
    "external_timeout_s", "automatic_retries",
}
_AUTH_KEYS = {
    "schema_version", "kind", "campaign_id", "block_index",
    "campaign_freeze_sha256", "block_id", "block_order", "cell_ordinal",
    "revision", "profile_sha256", "arm", "quota_mode", "preflight_sha256",
    "binary_sha256", "output_root", "required_complete_cycles",
    "hard_timeout_s", "external_timeout_s", "automatic_retries",
    "claim_eligible", "figure_eligible", "approval_ref", "approved_at_utc",
}
_ORDERS = ("forward", "reverse", "forward", "reverse", "forward", "reverse")
_BINARY_NAMES = {"app", "keygen", "tls_keygen", "native_digest"}


class _InvalidSequence(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str) -> None:
    raise _InvalidSequence(code, detail)


def _digest(value: object, length: int = 64) -> bool:
    return (
        isinstance(value, str) and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        _fail("path_contract", "campaign paths must be absolute")
    path = Path(value)
    if ".." in path.parts or str(path) != str(path.resolve(strict=False)):
        _fail("path_contract", "campaign paths must be normalized")
    return path


def _read_regular(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        _fail("missing_input", f"{label} must be a regular file")
    return path.read_bytes()


def _json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as error:
        _fail("malformed_input", f"{label} is not JSON: {error}")
    if not isinstance(value, dict):
        _fail("malformed_input", f"{label} must be one JSON object")
    return value


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
    )


def _utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ) is None:
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ) == value
    except ValueError:
        return False


def _validate_freeze_or_raise(freeze: Mapping[str, object]) -> dict[str, object]:
    if set(freeze) != _FREEZE_KEYS:
        _fail("freeze_schema", "freeze file has missing or extra fields")
    campaign_id = freeze.get("campaign_id")
    if (
        type(freeze.get("schema_version")) is not int
        or freeze.get("schema_version") != 1
        or freeze.get("kind") != _FREEZE_KIND
        or not isinstance(campaign_id, str)
        or re.fullmatch(r"w16-cpu-repeat-[a-z0-9][a-z0-9-]*", campaign_id) is None
        or not _digest(freeze.get("revision"), 40)
        or freeze.get("host") != "proteina02"
        or freeze.get("booking_id") != "1hfblbqhgpne9en0k05jaq83t0"
        or freeze.get("block_orders") != list(_ORDERS)
        or freeze.get("replica_count") != 31
        or freeze.get("quorum") != 21
        or freeze.get("fanout") != 5
        or freeze.get("tree_count") != 21
        or freeze.get("slow_replica_ids") != list(range(6))
        or freeze.get("slow_quota_percent") != 25
        or freeze.get("other_quota_percent") != 100
        or freeze.get("complete_cycles") != 5
        or freeze.get("hard_timeout_s") != 480
        or freeze.get("external_timeout_s") != 720
        or freeze.get("automatic_retries") != 0
        or freeze.get("positive_blocks_required") != 5
        or freeze.get("positive_per_order_required") != 2
        or freeze.get("adjusted_gain_numerator") != 11
        or freeze.get("adjusted_gain_denominator") != 10
        or freeze.get("pilot_excluded") is not True
    ):
        _fail("freeze_contract", "freeze file differs from the prospective 24-cell design")
    for field in (
        "replica_count", "quorum", "fanout", "tree_count",
        "slow_quota_percent", "other_quota_percent", "complete_cycles",
        "hard_timeout_s", "external_timeout_s", "automatic_retries",
        "positive_blocks_required", "positive_per_order_required",
        "adjusted_gain_numerator", "adjusted_gain_denominator",
    ):
        if type(freeze[field]) is not int:
            _fail("freeze_contract", f"{field} must be an exact integer")
    output_parent = _absolute_path(freeze["output_parent"])
    evidence_dir = _absolute_path(freeze["evidence_dir"])
    repository_root = _absolute_path(freeze["repository_root"])
    timeout_path = _absolute_path(freeze["timeout_path"])
    if repository_root != REPOSITORY.resolve():
        _fail("repository_root", "freeze repository differs from this source checkout")
    if (REPOSITORY / "results").resolve() not in output_parent.parents:
        _fail("path_contract", "output parent must be below repository results")
    if (REPOSITORY / "build-adaptive").resolve() not in evidence_dir.parents:
        _fail("path_contract", "evidence directory must be below build-adaptive")
    if (
        not _digest(freeze["timeout_sha256"])
        or timeout_path.is_symlink()
        or not timeout_path.is_file()
        or hashlib.sha256(timeout_path.read_bytes()).hexdigest()
        != freeze["timeout_sha256"]
        or not os.access(timeout_path, os.X_OK)
    ):
        _fail("timeout_identity", "GNU timeout executable identity differs")
    if output_parent == evidence_dir or output_parent in evidence_dir.parents or evidence_dir in output_parent.parents:
        _fail("path_contract", "output roots and external evidence must be disjoint")
    return dict(freeze)


def validate_freeze(freeze: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {
        "verdict": "INCOMPLETE", "claim_eligible": False,
        "figure_eligible": False, "thesis_result_eligible": False,
    }
    try:
        accepted = _validate_freeze_or_raise(freeze)
        result.update({
            "verdict": "PASS", "campaign_id": accepted["campaign_id"],
            "revision": accepted["revision"],
        })
    except (_InvalidSequence, OSError, ValueError, TypeError) as error:
        result.update({"reason_code": getattr(error, "code", "freeze_input_error"),
                       "detail": str(error)})
    return result


def _validate_manifest_or_raise(
    manifest: Mapping[str, object], *, freeze_bytes: bytes,
    manifest_path: Path, approval_ref: str,
) -> dict[str, object]:
    freeze = _validate_freeze_or_raise(_json_object(freeze_bytes, "freeze"))
    if set(manifest) != _MANIFEST_KEYS:
        _fail("manifest_schema", "manifest has missing or extra fields")
    freeze_sha = hashlib.sha256(freeze_bytes).hexdigest()
    if (
        type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("kind") != _MANIFEST_KIND
        or manifest.get("campaign_id") != freeze["campaign_id"]
        or manifest.get("revision") != freeze["revision"]
        or manifest.get("campaign_freeze_sha256") != freeze_sha
        or not isinstance(approval_ref, str) or not approval_ref
        or manifest.get("approval_ref") != approval_ref
        or manifest.get("output_parent") != freeze["output_parent"]
        or manifest.get("evidence_dir") != freeze["evidence_dir"]
        or not isinstance(manifest.get("cells"), list)
        or len(manifest["cells"]) != 24
    ):
        _fail("manifest_contract", "manifest differs from exact freeze or approval")
    _absolute_path(str(manifest_path.resolve()))
    output_parent = _absolute_path(manifest["output_parent"])
    evidence_dir = _absolute_path(manifest["evidence_dir"])
    paths: set[Path] = set()
    preflight_identities: set[tuple[str, str]] = set()
    binary_identities: set[str] = set()
    for ordinal, raw_cell in enumerate(manifest["cells"], 1):
        if not isinstance(raw_cell, dict) or set(raw_cell) != _CELL_KEYS:
            _fail("cell_schema", f"cell {ordinal} manifest schema drifted")
        block_index = (ordinal - 1) // 4 + 1
        cell_ordinal = (ordinal - 1) % 4 + 1
        order_name = _ORDERS[block_index - 1]
        order = FORWARD if order_name == "forward" else REVERSE
        label = order[cell_ordinal - 1]
        slug = label.replace(":", "-")
        output = output_parent / f"block-{block_index:02d}" / slug
        input_dir = evidence_dir / "inputs" / f"block-{block_index:02d}" / f"cell-{cell_ordinal:02d}"
        preflight_path = input_dir / "preflight.json"
        authorization_path = input_dir / "authorization.json"
        if (
            type(raw_cell.get("ordinal")) is not int
            or raw_cell.get("ordinal") != ordinal
            or type(raw_cell.get("block_index")) is not int
            or raw_cell.get("block_index") != block_index
            or raw_cell.get("block_order") != order_name
            or raw_cell.get("cell_label") != label
            or type(raw_cell.get("block_cell_ordinal")) is not int
            or raw_cell.get("block_cell_ordinal") != cell_ordinal
            or raw_cell.get("output_root") != str(output)
            or raw_cell.get("preflight_path") != str(preflight_path)
            or raw_cell.get("authorization_path") != str(authorization_path)
            or type(raw_cell.get("hard_timeout_s")) is not int
            or raw_cell.get("hard_timeout_s") != 480
            or type(raw_cell.get("external_timeout_s")) is not int
            or raw_cell.get("external_timeout_s") != 720
            or type(raw_cell.get("automatic_retries")) is not int
            or raw_cell.get("automatic_retries") != 0
        ):
            _fail("cell_contract", f"cell {ordinal} differs from frozen slot")
        for path in (output, preflight_path, authorization_path):
            if path in paths or path.is_symlink():
                _fail("path_contract", f"cell {ordinal} has a reused or symlink path")
            paths.add(path)
        preflight_bytes = _read_regular(preflight_path, f"cell {ordinal} preflight")
        authorization_bytes = _read_regular(authorization_path, f"cell {ordinal} authorization")
        if (
            not _digest(raw_cell.get("preflight_sha256"))
            or raw_cell["preflight_sha256"] != hashlib.sha256(preflight_bytes).hexdigest()
            or not _digest(raw_cell.get("authorization_sha256"))
            or raw_cell["authorization_sha256"] != hashlib.sha256(authorization_bytes).hexdigest()
        ):
            _fail("input_hash", f"cell {ordinal} preflight/authorization bytes changed")
        preflight = _json_object(preflight_bytes, f"cell {ordinal} preflight")
        authorization = _json_object(authorization_bytes, f"cell {ordinal} authorization")
        arm, mode = label.split(":", 1)
        binaries = preflight.get("binary_sha256")
        profile = preflight.get("profile_sha256")
        if (
            type(preflight.get("schema_version")) is not int
            or preflight.get("schema_version") != 1
            or preflight.get("kind") != feasibility.SCHEMA
            or preflight.get("verdict") != "PREFLIGHT_OK_NO_EXECUTION"
            or preflight.get("revision") != freeze["revision"]
            or preflight.get("arm") != arm
            or not _digest(profile)
            or not isinstance(binaries, dict)
            or set(binaries) != _BINARY_NAMES
            or any(not _digest(value) for value in binaries.values())
            or set(authorization) != _AUTH_KEYS
            or type(authorization.get("schema_version")) is not int
            or authorization.get("schema_version") != 2
            or authorization.get("kind") != "kauri-w16-static-e0-campaign-authorization-v2"
            or authorization.get("campaign_id") != freeze["campaign_id"]
            or type(authorization.get("block_index")) is not int
            or authorization.get("block_index") != block_index
            or authorization.get("campaign_freeze_sha256") != freeze_sha
            or authorization.get("block_id") != f"{freeze['campaign_id']}-block-{block_index:02d}"
            or authorization.get("block_order") != list(order)
            or type(authorization.get("cell_ordinal")) is not int
            or authorization.get("cell_ordinal") != cell_ordinal
            or authorization.get("revision") != freeze["revision"]
            or authorization.get("profile_sha256") != profile
            or authorization.get("arm") != arm
            or authorization.get("quota_mode") != mode
            or authorization.get("preflight_sha256") != hashlib.sha256(preflight_bytes).hexdigest()
            or authorization.get("binary_sha256") != binaries
            or authorization.get("output_root") != str(output)
            or type(authorization.get("required_complete_cycles")) is not int
            or authorization.get("required_complete_cycles") != 5
            or type(authorization.get("hard_timeout_s")) is not int
            or authorization.get("hard_timeout_s") != 480
            or type(authorization.get("external_timeout_s")) is not int
            or authorization.get("external_timeout_s") != 720
            or type(authorization.get("automatic_retries")) is not int
            or authorization.get("automatic_retries") != 0
            or authorization.get("claim_eligible") is not False
            or authorization.get("figure_eligible") is not False
            or authorization.get("approval_ref") != approval_ref
            or not _utc_timestamp(authorization.get("approved_at_utc"))
        ):
            _fail("input_contract", f"cell {ordinal} inputs differ from frozen contract")
        preflight_identities.add((str(preflight["revision"]), str(profile)))
        binary_identities.add(json.dumps(binaries, sort_keys=True))
    if len(preflight_identities) != 1 or len(binary_identities) != 1:
        _fail("cross_cell_identity", "preflight identity differs across 24 cells")
    return dict(manifest)


def validate_manifest(
    manifest: Mapping[str, object], *, freeze_bytes: bytes,
    manifest_path: Path, approval_ref: str,
) -> dict[str, object]:
    result: dict[str, object] = {
        "verdict": "INCOMPLETE", "claim_eligible": False,
        "figure_eligible": False, "thesis_result_eligible": False,
    }
    try:
        accepted = _validate_manifest_or_raise(
            manifest, freeze_bytes=freeze_bytes,
            manifest_path=manifest_path, approval_ref=approval_ref,
        )
        result.update({"verdict": "PASS", "campaign_id": accepted["campaign_id"],
                       "cell_count": 24})
    except (_InvalidSequence, OSError, ValueError, TypeError) as error:
        result.update({"reason_code": getattr(error, "code", "manifest_input_error"),
                       "detail": str(error)})
    return result


def _write_new_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as destination:
        destination.write(_canonical(value))
        destination.flush()
        os.fsync(destination.fileno())


def _write_new_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as destination:
        destination.write(payload)
        destination.flush()
        os.fsync(destination.fileno())


def _append_journal(path: Path, entry: Mapping[str, object]) -> None:
    with path.open("ab") as destination:
        destination.write(_canonical(entry))
        destination.flush()
        os.fsync(destination.fileno())


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prepare_sequence(
    freeze_file: Path, *, approval_ref: str, approved_at_utc: str,
) -> dict[str, object]:
    """Create 24 no-execution preflights and exact v2 authorizations.

    The caller must supply a real user approval reference; this function can
    bind it to bytes, not authenticate the human decision.  Any partial
    preparation is retained and cannot be silently reused at the same path.
    """

    result: dict[str, object] = {
        "verdict": "INCOMPLETE", "claim_eligible": False,
        "figure_eligible": False, "thesis_result_eligible": False,
    }
    try:
        freeze_file = Path(freeze_file)
        freeze_bytes = _read_regular(freeze_file, "campaign freeze")
        freeze = _validate_freeze_or_raise(_json_object(freeze_bytes, "campaign freeze"))
        if not isinstance(approval_ref, str) or not approval_ref:
            _fail("approval_ref", "explicit approval reference is required")
        if not _utc_timestamp(approved_at_utc):
            _fail("approval_time", "approval time must be exact UTC seconds")
        output_parent = _absolute_path(freeze["output_parent"])
        evidence_dir = _absolute_path(freeze["evidence_dir"])
        if output_parent.exists() or output_parent.is_symlink():
            _fail("output_exists", "fresh campaign output parent already exists")
        if evidence_dir.exists() or evidence_dir.is_symlink():
            _fail("evidence_exists", "fresh campaign evidence directory already exists")

        # Complete every clean-checkout/binary/native-digest preflight before
        # writing the first campaign input.  No process is launched here.
        slots: list[tuple[int, int, str, dict[str, object], bytes]] = []
        for block_index, order_name in enumerate(_ORDERS, 1):
            order = FORWARD if order_name == "forward" else REVERSE
            for cell_ordinal, label in enumerate(order, 1):
                arm, _mode = label.split(":", 1)
                preflight = feasibility.preflight(
                    repository=REPOSITORY,
                    app_binary=REPOSITORY / "build-adaptive/examples/hotstuff-app",
                    keygen_binary=REPOSITORY / "build-adaptive/hotstuff-keygen",
                    tls_keygen_binary=REPOSITORY / "build-adaptive/hotstuff-tls-keygen",
                    native_digest_binary=(
                        REPOSITORY / "build-adaptive/examples/static-epoch0-digest"
                    ),
                    arm=arm,
                )
                if (
                    preflight.get("schema_version") != 1
                    or preflight.get("kind") != feasibility.SCHEMA
                    or preflight.get("verdict") != "PREFLIGHT_OK_NO_EXECUTION"
                    or preflight.get("revision") != freeze["revision"]
                    or preflight.get("arm") != arm
                    or not _digest(preflight.get("profile_sha256"))
                    or not isinstance(preflight.get("binary_sha256"), dict)
                    or set(preflight["binary_sha256"]) != _BINARY_NAMES
                    or any(not _digest(value) for value in preflight["binary_sha256"].values())
                ):
                    _fail("preflight_contract", "live preflight differs from frozen revision/profile")
                slots.append((block_index, cell_ordinal, label, preflight,
                              feasibility.canonical_json(preflight)))
        if len({str(slot[3]["profile_sha256"]) for slot in slots}) != 1:
            _fail("preflight_identity", "24 preflights differ in profile")
        if len({json.dumps(slot[3]["binary_sha256"], sort_keys=True) for slot in slots}) != 1:
            _fail("preflight_identity", "24 preflights differ in binary hashes")

        evidence_dir.mkdir(parents=True, exist_ok=False)
        freeze_sha = hashlib.sha256(freeze_bytes).hexdigest()
        manifest_cells: list[dict[str, object]] = []
        for ordinal, (block_index, cell_ordinal, label, preflight,
                      preflight_bytes) in enumerate(slots, 1):
            order_name = _ORDERS[block_index - 1]
            order = FORWARD if order_name == "forward" else REVERSE
            arm, mode = label.split(":", 1)
            output = output_parent / f"block-{block_index:02d}" / label.replace(":", "-")
            input_dir = (
                evidence_dir / "inputs" / f"block-{block_index:02d}"
                / f"cell-{cell_ordinal:02d}"
            )
            input_dir.mkdir(parents=True, exist_ok=False)
            preflight_path = input_dir / "preflight.json"
            authorization_path = input_dir / "authorization.json"
            _write_new_bytes(preflight_path, preflight_bytes)
            authorization = {
                "schema_version": 2,
                "kind": "kauri-w16-static-e0-campaign-authorization-v2",
                "campaign_id": freeze["campaign_id"],
                "block_index": block_index,
                "campaign_freeze_sha256": freeze_sha,
                "block_id": f"{freeze['campaign_id']}-block-{block_index:02d}",
                "block_order": list(order),
                "cell_ordinal": cell_ordinal,
                "revision": freeze["revision"],
                "profile_sha256": preflight["profile_sha256"],
                "arm": arm,
                "quota_mode": mode,
                "preflight_sha256": hashlib.sha256(preflight_bytes).hexdigest(),
                "binary_sha256": preflight["binary_sha256"],
                "output_root": str(output),
                "required_complete_cycles": 5,
                "hard_timeout_s": 480,
                "external_timeout_s": 720,
                "automatic_retries": 0,
                "claim_eligible": False,
                "figure_eligible": False,
                "approval_ref": approval_ref,
                "approved_at_utc": approved_at_utc,
            }
            authorization_bytes = _canonical(authorization)
            _write_new_bytes(authorization_path, authorization_bytes)
            manifest_cells.append({
                "ordinal": ordinal,
                "block_index": block_index,
                "block_order": order_name,
                "cell_label": label,
                "block_cell_ordinal": cell_ordinal,
                "output_root": str(output),
                "preflight_path": str(preflight_path),
                "preflight_sha256": hashlib.sha256(preflight_bytes).hexdigest(),
                "authorization_path": str(authorization_path),
                "authorization_sha256": hashlib.sha256(authorization_bytes).hexdigest(),
                "hard_timeout_s": 480,
                "external_timeout_s": 720,
                "automatic_retries": 0,
            })
        manifest = {
            "schema_version": 1,
            "kind": _MANIFEST_KIND,
            "campaign_id": freeze["campaign_id"],
            "revision": freeze["revision"],
            "campaign_freeze_sha256": freeze_sha,
            "approval_ref": approval_ref,
            "output_parent": str(output_parent),
            "evidence_dir": str(evidence_dir),
            "cells": manifest_cells,
        }
        manifest_path = evidence_dir / "manifest.json"
        _write_new_json(manifest_path, manifest)
        _validate_manifest_or_raise(
            manifest, freeze_bytes=freeze_bytes,
            manifest_path=manifest_path, approval_ref=approval_ref,
        )
        result.update({
            "verdict": "PREPARED_NO_EXECUTION",
            "campaign_id": freeze["campaign_id"],
            "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "campaign_freeze_sha256": freeze_sha,
            "cell_count": 24,
        })
    except (_InvalidSequence, OSError, ValueError, TypeError, feasibility.StaticE0FeasibilityError) as error:
        result.update({"reason_code": getattr(error, "code", "preparation_error"),
                       "detail": str(error)})
    return result


def _fixed_command(
    cell: Mapping[str, object], *, freeze_file: Path, approval_ref: str,
    timeout_path: Path,
) -> list[str]:
    arm, mode = str(cell["cell_label"]).split(":", 1)
    return [
        str(timeout_path), "-s", "INT", "-k", "30s", "720s",
        sys.executable,
        str(REPOSITORY / "experiments/adaptive/run_n31_static_e0_local_executor.py"),
        "run", "--arm", arm,
        "--preflight", str(cell["preflight_path"]),
        "--authorization", str(cell["authorization_path"]),
        "--output", str(cell["output_root"]),
        "--hard-timeout-s", "480",
        "--quota-mode", mode,
        "--campaign-freeze-file", str(freeze_file),
        "--campaign-approval-ref", approval_ref,
    ]


def _verify_live_preflight(cell: Mapping[str, object], revision: str) -> None:
    """Recheck clean pushed source and binary identity before each launch."""

    arm, _mode = str(cell["cell_label"]).split(":", 1)
    receipt = feasibility.preflight(
        repository=REPOSITORY,
        app_binary=REPOSITORY / "build-adaptive/examples/hotstuff-app",
        keygen_binary=REPOSITORY / "build-adaptive/hotstuff-keygen",
        tls_keygen_binary=REPOSITORY / "build-adaptive/hotstuff-tls-keygen",
        native_digest_binary=REPOSITORY / "build-adaptive/examples/static-epoch0-digest",
        arm=arm,
    )
    expected_bytes = _read_regular(Path(str(cell["preflight_path"])), "cell preflight")
    if receipt.get("revision") != revision or feasibility.canonical_json(receipt) != expected_bytes:
        _fail("live_preflight_drift", "clean source, native binary, or preflight differs")


def execute_sequence(
    manifest_path: Path, *, manifest_sha256: str,
    freeze_file: Path, approval_ref: str,
) -> dict[str, object]:
    """Run 24 distinct cells once each, stopping on the first failed gate.

    Output roots are never overwritten or retried.  Only GNU timeout may
    interrupt a child; this function itself never sends a signal or replaces
    an incomplete cell.  Native process lifecycle and cgroup cleanup are
    checked by the independent v5 validator before advancing.
    """

    result: dict[str, object] = {
        "schema_version": 1,
        "kind": "kauri-w16-cpu-repeat-sequence-result-v1",
        "verdict": "STOPPED",
        "claim_eligible": False,
        "figure_eligible": False,
        "thesis_result_eligible": False,
        "technical_improvement_gate_passed": False,
        "direction_status": "NOT_EVALUATED",
        "attempted_cells": 0,
        "first_failure_ordinal": None,
        "cells": [],
        "blocks": [],
    }
    execution_dir: Path | None = None
    journal_path: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    try:
        if not _digest(manifest_sha256):
            _fail("manifest_hash", "approved manifest digest is malformed")
        manifest_path = Path(manifest_path)
        freeze_file = Path(freeze_file)
        manifest_bytes = _read_regular(manifest_path, "execution manifest")
        freeze_bytes = _read_regular(freeze_file, "campaign freeze")
        if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
            _fail("manifest_hash", "execution manifest differs from approved bytes")
        manifest = _json_object(manifest_bytes, "execution manifest")
        _validate_manifest_or_raise(
            manifest, freeze_bytes=freeze_bytes,
            manifest_path=manifest_path, approval_ref=approval_ref,
        )
        freeze = _validate_freeze_or_raise(_json_object(freeze_bytes, "campaign freeze"))
        timeout_path = Path(str(freeze["timeout_path"]))
        evidence_dir = _absolute_path(manifest["evidence_dir"])
        execution_dir = evidence_dir / "execution"
        if execution_dir.exists() or execution_dir.is_symlink():
            _fail("execution_exists", "execution records already exist; no retry/resume")
        for cell in manifest["cells"]:
            if Path(str(cell["output_root"])).exists():
                _fail("output_exists", "one or more output roots already exist")
        execution_dir.mkdir(parents=True, exist_ok=False)
        snapshot_dir = execution_dir / "snapshot"
        _write_new_bytes(snapshot_dir / "freeze.json", freeze_bytes)
        _write_new_bytes(snapshot_dir / "manifest.json", manifest_bytes)
        journal_path = execution_dir / "journal.jsonl"
        with journal_path.open("xb") as journal:
            journal.flush()
            os.fsync(journal.fileno())
        _write_new_json(execution_dir / "sequence-start.json", {
            "schema_version": 1,
            "campaign_id": manifest["campaign_id"],
            "started_at_utc": _timestamp(),
            "manifest_sha256": manifest_sha256,
            "campaign_freeze_sha256": hashlib.sha256(freeze_bytes).hexdigest(),
            "approval_ref": approval_ref,
            "repository_root": str(REPOSITORY),
            "revision": freeze["revision"],
            "host_expected": freeze["host"],
            "booking_id_expected": freeze["booking_id"],
            "timeout_path": str(timeout_path),
            "timeout_sha256": freeze["timeout_sha256"],
            "claim_eligible": False,
        })
        _append_journal(journal_path, {
            "event": "sequence_start", "timestamp_utc": _timestamp(),
            "manifest_sha256": manifest_sha256,
            "campaign_freeze_sha256": hashlib.sha256(freeze_bytes).hexdigest(),
        })
        result["campaign_id"] = manifest["campaign_id"]
        result["manifest_sha256"] = manifest_sha256
        result["campaign_freeze_sha256"] = hashlib.sha256(freeze_bytes).hexdigest()
        block_roots: list[Path] = []
        all_blocks: list[list[Path]] = []
        for ordinal, cell in enumerate(manifest["cells"], 1):
            # Recheck all frozen bytes before each launch, not merely once.
            if (
                _read_regular(manifest_path, "execution manifest") != manifest_bytes
                or _read_regular(freeze_file, "campaign freeze") != freeze_bytes
            ):
                _fail("input_changed", "manifest or freeze bytes changed during execution")
            _validate_manifest_or_raise(
                manifest, freeze_bytes=freeze_bytes,
                manifest_path=manifest_path, approval_ref=approval_ref,
            )
            _verify_live_preflight(cell, str(freeze["revision"]))
            output = Path(str(cell["output_root"]))
            if output.exists() or output.is_symlink():
                _fail("output_exists", f"cell {ordinal} output already exists")
            block_index = int(cell["block_index"])
            block_ordinal = int(cell["block_cell_ordinal"])
            outside = (
                execution_dir / f"block-{block_index:02d}"
                / f"cell-{block_ordinal:02d}"
            )
            outside.mkdir(parents=True, exist_ok=False)
            stdout_path = outside / "wrapper.stdout"
            stderr_path = outside / "wrapper.stderr"
            validation_path = outside / "validation-v5.json"
            command = _fixed_command(
                cell, freeze_file=freeze_file, approval_ref=approval_ref,
                timeout_path=timeout_path,
            )
            started_at = _timestamp()
            intent = {
                "ordinal": ordinal,
                "block_index": block_index,
                "cell_label": cell["cell_label"],
                "output_root": str(output),
                "command": command,
                "cwd": str(REPOSITORY),
                "started_at_utc": started_at,
                "preflight_sha256": cell["preflight_sha256"],
                "authorization_sha256": cell["authorization_sha256"],
                "timeout_sha256": freeze["timeout_sha256"],
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "validation_path": str(validation_path),
            }
            _write_new_json(outside / "cell-intent.json", intent)
            assert journal_path is not None
            _append_journal(journal_path, {
                "event": "cell_intent", "timestamp_utc": started_at,
                "ordinal": ordinal, "command": command,
                "output_root": str(output),
            })
            print(json.dumps({
                "event": "cell_start", "ordinal": ordinal,
                "label": cell["cell_label"], "output_root": str(output),
            }, sort_keys=True), flush=True)
            wrapper_exit_code: int | None = None
            wrapper_error: str | None = None
            result["attempted_cells"] = ordinal
            with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
                try:
                    process = subprocess.Popen(
                        command, cwd=REPOSITORY, stdout=stdout, stderr=stderr,
                        start_new_session=True,
                    )
                    result["live_child_pid"] = process.pid
                    _append_journal(journal_path, {
                        "event": "cell_pid", "timestamp_utc": _timestamp(),
                        "ordinal": ordinal, "pid": process.pid,
                    })
                    while True:
                        try:
                            wrapper_exit_code = int(process.wait(timeout=30))
                            break
                        except subprocess.TimeoutExpired:
                            _append_journal(journal_path, {
                                "event": "cell_alive", "timestamp_utc": _timestamp(),
                                "ordinal": ordinal, "pid": process.pid,
                            })
                            print(json.dumps({
                                "event": "cell_alive", "ordinal": ordinal,
                                "pid": process.pid,
                            }, sort_keys=True), flush=True)
                    result.pop("live_child_pid", None)
                    _append_journal(journal_path, {
                        "event": "cell_wrapper_exit", "timestamp_utc": _timestamp(),
                        "ordinal": ordinal, "pid": process.pid,
                        "exit_code": wrapper_exit_code,
                    })
                    process = None
                except OSError as error:
                    wrapper_error = str(error)
            record: dict[str, object] = {
                "ordinal": ordinal, "block_index": block_index,
                "cell_label": cell["cell_label"],
                "output_root": str(output),
                "command": command,
                "started_at_utc": started_at,
                "finished_at_utc": _timestamp(),
                "wrapper_exit_code": wrapper_exit_code,
                "wrapper_error": wrapper_error,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "validation_path": str(validation_path),
                "validation_verdict": None,
            }
            if wrapper_exit_code != 0:
                _write_new_json(validation_path, {
                    "verdict": "NOT_RUN_NONZERO_WRAPPER",
                    "detail": "Nonzero or failed wrapper; sealed output is preserved for separate audit.",
                })
                record["validation_verdict"] = "NOT_RUN_NONZERO_WRAPPER"
                result["first_failure_ordinal"] = ordinal
                result["reason_code"] = "wrapper_nonzero"
                _write_new_json(outside / "cell-result.json", record)
                result["cells"].append(record)
                _append_journal(journal_path, {
                    "event": "cell_stop", "timestamp_utc": _timestamp(),
                    "ordinal": ordinal, "reason_code": "wrapper_nonzero",
                    "exit_code": wrapper_exit_code,
                })
                break
            validation = validate_w16_output_v5(output)
            _write_new_json(validation_path, validation)
            record["validation_verdict"] = validation.get("verdict")
            _write_new_json(outside / "cell-result.json", record)
            result["cells"].append(record)
            _append_journal(journal_path, {
                "event": "cell_validation", "timestamp_utc": _timestamp(),
                "ordinal": ordinal, "verdict": validation.get("verdict"),
            })
            if validation.get("verdict") != "PASS":
                result["first_failure_ordinal"] = ordinal
                result["reason_code"] = "cell_validation"
                break
            block_roots.append(output)
            if block_ordinal == 4:
                block_validation = validate_w16_campaign_block(block_roots)
                block_path = execution_dir / f"block-{block_index:02d}-validation.json"
                _write_new_json(block_path, block_validation)
                result["blocks"].append({
                    "block_index": block_index,
                    "verdict": block_validation.get("verdict"),
                    "validation_path": str(block_path),
                })
                if block_validation.get("verdict") != "PASS":
                    result["first_failure_ordinal"] = ordinal
                    result["reason_code"] = "block_validation"
                    break
                all_blocks.append(block_roots)
                block_roots = []
            print(json.dumps({
                "event": "cell_pass", "ordinal": ordinal,
                "label": cell["cell_label"],
            }, sort_keys=True), flush=True)
        if result["first_failure_ordinal"] is None and len(all_blocks) == 6:
            campaign_validation = validate_w16_cpu_campaign(all_blocks)
            campaign_path = execution_dir / "campaign-validation.json"
            _write_new_json(campaign_path, campaign_validation)
            result["campaign_validation_path"] = str(campaign_path)
            result["campaign_validation_verdict"] = campaign_validation.get("verdict")
            if campaign_validation.get("verdict") == "PASS":
                result["technical_improvement_gate_passed"] = bool(
                    campaign_validation.get("technical_improvement_gate_passed") is True
                )
                direction = campaign_validation.get("direction_gate")
                if isinstance(direction, dict):
                    result["direction_status"] = direction.get("status", "NOT_EVALUATED")
            if campaign_validation.get("verdict") == "PASS":
                _append_journal(journal_path, {
                    "event": "campaign_technical_pass", "timestamp_utc": _timestamp(),
                    "attempted_cells": 24,
                })
            else:
                result["first_failure_ordinal"] = 24
                result["reason_code"] = "campaign_validation"
        if result["first_failure_ordinal"] is not None:
            _append_journal(journal_path, {
                "event": "sequence_stop", "timestamp_utc": _timestamp(),
                "first_failure_ordinal": result["first_failure_ordinal"],
                "reason_code": result.get("reason_code"),
            })
        if result["first_failure_ordinal"] is None and len(all_blocks) == 6 and result.get("campaign_validation_verdict") == "PASS":
            terminal = dict(result)
            terminal["verdict"] = "PASS"
        else:
            terminal = dict(result)
        if terminal["verdict"] != "PASS" and "reason_code" not in terminal:
            terminal["reason_code"] = "sequence_incomplete"
        _write_new_json(execution_dir / "sequence-result.json", terminal)
        result = terminal
    except (_InvalidSequence, OSError, ValueError, TypeError, KeyboardInterrupt,
            feasibility.StaticE0FeasibilityError) as error:
        result["verdict"] = "STOPPED"
        result["reason_code"] = getattr(error, "code", "sequence_input_error")
        result["detail"] = str(error)
        if process is not None and process.poll() is None:
            result["live_child_pid"] = process.pid
        if journal_path is not None and journal_path.is_file():
            try:
                _append_journal(journal_path, {
                    "event": "sequence_abort", "timestamp_utc": _timestamp(),
                    "reason_code": result["reason_code"],
                    "attempted_cells": result["attempted_cells"],
                    "live_child_pid": result.get("live_child_pid"),
                })
            except OSError:
                pass
        if execution_dir is not None and execution_dir.is_dir():
            try:
                _write_new_json(execution_dir / "sequence-abort.json", result)
            except OSError:
                pass
        # Existing cells and logs remain in place, including an interrupted
        # cell.  Never auto-resume or run a replacement slot here.
    return result
