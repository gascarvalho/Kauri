"""Fresh, fail-closed execution path for the W16 v7 cleanup-proof study.

This module intentionally does not accept the v1/v6 freeze or its inputs.
Its only successful path is a fresh 24-cell, v3-authorized sequence whose
cells are reconstructed by the v7 validator.
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
from .w16_campaign_validator_v7 import (
    FORWARD, REVERSE, validate_w16_campaign_block_v7,
    validate_w16_cpu_campaign_v7,
)
from .w16_output_validator import validate_w16_output_v7


REPOSITORY = Path(__file__).resolve().parents[3]
_FREEZE_KIND = "kauri-w16-cpu-repeat-freeze-v2"
_MANIFEST_KIND = "kauri-w16-cpu-repeat-sequence-manifest-v2"
_AUTH_KIND = "kauri-w16-static-e0-campaign-authorization-v3"
_RECEIPT_SCHEMA = "kauri-n31-static-e0-local-executor-v3"
_ORDERS = ("forward", "reverse", "forward", "reverse", "forward", "reverse")
_BINARIES = {"app", "keygen", "tls_keygen", "native_digest"}
_FREEZE_KEYS = {
    "schema_version", "kind", "campaign_id", "revision", "host", "booking_id",
    "repository_root", "output_parent", "evidence_dir", "timeout_path",
    "timeout_sha256", "block_orders", "replica_count", "quorum", "fanout",
    "tree_count", "slow_replica_ids", "slow_quota_percent", "other_quota_percent",
    "complete_cycles", "hard_timeout_s", "external_timeout_s", "automatic_retries",
    "positive_blocks_required", "positive_per_order_required",
    "adjusted_gain_numerator", "adjusted_gain_denominator", "pilot_excluded",
    "authorization_schema_version", "authorization_kind", "executor_receipt_schema",
    "cell_validator_version", "process_cleanup_required",
}
_MANIFEST_KEYS = {
    "schema_version", "kind", "campaign_id", "revision", "campaign_freeze_sha256",
    "approval_ref", "output_parent", "evidence_dir", "cells",
}
_CELL_KEYS = {
    "ordinal", "block_index", "block_order", "cell_label", "block_cell_ordinal",
    "output_root", "preflight_path", "preflight_sha256", "authorization_path",
    "authorization_sha256", "hard_timeout_s", "external_timeout_s", "automatic_retries",
}
_AUTH_KEYS = {
    "schema_version", "kind", "campaign_id", "block_index", "campaign_freeze_sha256",
    "block_id", "block_order", "cell_ordinal", "revision", "profile_sha256", "arm",
    "quota_mode", "preflight_sha256", "binary_sha256", "output_root",
    "required_complete_cycles", "hard_timeout_s", "external_timeout_s", "automatic_retries",
    "claim_eligible", "figure_eligible", "approval_ref", "approved_at_utc",
    "executor_receipt_schema", "cell_validator_version", "process_cleanup_required",
}


class _InvalidSequence(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


def _fail(code: str, detail: str) -> None:
    raise _InvalidSequence(code, detail)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii") + b"\n"


def _digest(value: object, length: int = 64) -> bool:
    return isinstance(value, str) and len(value) == length and all(c in "0123456789abcdef" for c in value)


def _absolute(value: object) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        _fail("path_contract", "all campaign paths must be absolute")
    path = Path(value)
    if ".." in path.parts or str(path) != str(path.resolve(strict=False)):
        _fail("path_contract", "all campaign paths must be normalized")
    return path


def _read(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        _fail("missing_input", f"{label} must be a regular file")
    return path.read_bytes()


def _object(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as error:
        _fail("malformed_input", f"{label} is not JSON: {error}")
    if not isinstance(value, dict):
        _fail("malformed_input", f"{label} must be one object")
    return value


def _timestamp(value: object) -> bool:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is None:
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%dT%H:%M:%SZ") == value
    except ValueError:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_new(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as target:
        target.write(_canonical(value)); target.flush(); os.fsync(target.fileno())


def _write_bytes_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as target:
        target.write(payload); target.flush(); os.fsync(target.fileno())


def _validate_freeze_or_raise(freeze: Mapping[str, object]) -> dict[str, object]:
    if set(freeze) != _FREEZE_KEYS:
        _fail("freeze_schema", "v7 freeze has missing or extra fields")
    fixed = (
        type(freeze.get("schema_version")) is int and freeze.get("schema_version") == 2
        and freeze.get("kind") == _FREEZE_KIND
        and isinstance(freeze.get("campaign_id"), str)
        and re.fullmatch(r"w16-cpu-repeat-[a-z0-9][a-z0-9-]*", str(freeze.get("campaign_id")))
        and _digest(freeze.get("revision"), 40) and freeze.get("host") == "proteina02"
        and freeze.get("booking_id") == "1hfblbqhgpne9en0k05jaq83t0"
        and freeze.get("block_orders") == list(_ORDERS) and freeze.get("replica_count") == 31
        and freeze.get("quorum") == 21 and freeze.get("fanout") == 5 and freeze.get("tree_count") == 21
        and freeze.get("slow_replica_ids") == list(range(6)) and freeze.get("slow_quota_percent") == 25
        and freeze.get("other_quota_percent") == 100 and freeze.get("complete_cycles") == 5
        and freeze.get("hard_timeout_s") == 480 and freeze.get("external_timeout_s") == 720
        and freeze.get("automatic_retries") == 0 and freeze.get("positive_blocks_required") == 5
        and freeze.get("positive_per_order_required") == 2 and freeze.get("adjusted_gain_numerator") == 11
        and freeze.get("adjusted_gain_denominator") == 10 and freeze.get("pilot_excluded") is True
        and freeze.get("authorization_schema_version") == 3 and freeze.get("authorization_kind") == _AUTH_KIND
        and freeze.get("executor_receipt_schema") == _RECEIPT_SCHEMA
        and freeze.get("cell_validator_version") == 7 and freeze.get("process_cleanup_required") is True
    )
    if not fixed:
        _fail("freeze_contract", "freeze differs from the prospective v7 design")
    for key in (
        "replica_count", "quorum", "fanout", "tree_count",
        "slow_quota_percent", "other_quota_percent", "complete_cycles",
        "hard_timeout_s", "external_timeout_s", "automatic_retries",
        "positive_blocks_required", "positive_per_order_required",
        "adjusted_gain_numerator", "adjusted_gain_denominator",
        "authorization_schema_version", "cell_validator_version",
    ):
        if type(freeze[key]) is not int:
            _fail("freeze_contract", f"{key} must be an exact integer")
    if any(type(replica) is not int for replica in freeze["slow_replica_ids"]):
        _fail("freeze_contract", "slow replica identifiers must be exact integers")
    for key in ("repository_root", "output_parent", "evidence_dir", "timeout_path"):
        _absolute(freeze[key])
    repository, output, evidence, timeout = (_absolute(freeze[key]) for key in
                                               ("repository_root", "output_parent", "evidence_dir", "timeout_path"))
    if repository != REPOSITORY.resolve() or (REPOSITORY / "results").resolve() not in output.parents or (REPOSITORY / "build-adaptive").resolve() not in evidence.parents:
        _fail("path_contract", "v7 paths do not belong to this checkout")
    if output == evidence or output in evidence.parents or evidence in output.parents:
        _fail("path_contract", "output and evidence roots must be disjoint")
    if not _digest(freeze.get("timeout_sha256")) or timeout.is_symlink() or not timeout.is_file() or not os.access(timeout, os.X_OK) or hashlib.sha256(timeout.read_bytes()).hexdigest() != freeze["timeout_sha256"]:
        _fail("timeout_identity", "timeout executable identity differs")
    return dict(freeze)


def validate_freeze_v7(freeze: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {"verdict": "INCOMPLETE", "claim_eligible": False, "figure_eligible": False, "thesis_result_eligible": False}
    try:
        accepted = _validate_freeze_or_raise(freeze)
        result.update({"verdict": "PASS", "campaign_id": accepted["campaign_id"], "revision": accepted["revision"]})
    except (_InvalidSequence, OSError, TypeError, ValueError) as error:
        result.update({"reason_code": getattr(error, "code", "freeze_input_error"), "detail": str(error)})
    return result


def _manifest_or_raise(manifest: Mapping[str, object], *, freeze_bytes: bytes, approval_ref: str) -> dict[str, object]:
    freeze = _validate_freeze_or_raise(_object(freeze_bytes, "freeze"))
    freeze_sha = hashlib.sha256(freeze_bytes).hexdigest()
    if set(manifest) != _MANIFEST_KEYS or type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 2 or manifest.get("kind") != _MANIFEST_KIND or manifest.get("campaign_id") != freeze["campaign_id"] or manifest.get("revision") != freeze["revision"] or manifest.get("campaign_freeze_sha256") != freeze_sha or manifest.get("approval_ref") != approval_ref or manifest.get("output_parent") != freeze["output_parent"] or manifest.get("evidence_dir") != freeze["evidence_dir"] or not isinstance(manifest.get("cells"), list) or len(manifest["cells"]) != 24:
        _fail("manifest_contract", "manifest differs from exact v7 freeze or approval")
    seen: set[Path] = set()
    identities: set[tuple[str, str, str]] = set()
    for ordinal, cell in enumerate(manifest["cells"], 1):
        if not isinstance(cell, dict) or set(cell) != _CELL_KEYS:
            _fail("cell_schema", f"cell {ordinal} schema drifted")
        for key in (
            "ordinal", "block_index", "block_cell_ordinal", "hard_timeout_s",
            "external_timeout_s", "automatic_retries",
        ):
            if type(cell[key]) is not int:
                _fail("cell_schema", f"cell {ordinal} {key} is not an exact integer")
        block, within = (ordinal - 1) // 4 + 1, (ordinal - 1) % 4 + 1
        order_name = _ORDERS[block - 1]; order = FORWARD if order_name == "forward" else REVERSE
        label = order[within - 1]; output = Path(str(freeze["output_parent"])) / f"block-{block:02d}" / label.replace(":", "-")
        inputs = Path(str(freeze["evidence_dir"])) / "inputs" / f"block-{block:02d}" / f"cell-{within:02d}"
        expected = {"ordinal": ordinal, "block_index": block, "block_order": order_name, "cell_label": label, "block_cell_ordinal": within, "output_root": str(output), "preflight_path": str(inputs / "preflight.json"), "authorization_path": str(inputs / "authorization.json"), "hard_timeout_s": 480, "external_timeout_s": 720, "automatic_retries": 0}
        if any(cell.get(key) != value for key, value in expected.items()):
            _fail("cell_contract", f"cell {ordinal} differs from frozen slot")
        pre_path, auth_path = Path(str(cell["preflight_path"])), Path(str(cell["authorization_path"]))
        for path in (output, pre_path, auth_path):
            if path in seen or path.is_symlink(): _fail("path_contract", "cell path is reused or symlinked")
            seen.add(path)
        pre_bytes, auth_bytes = _read(pre_path, "preflight"), _read(auth_path, "authorization")
        if cell.get("preflight_sha256") != hashlib.sha256(pre_bytes).hexdigest() or cell.get("authorization_sha256") != hashlib.sha256(auth_bytes).hexdigest():
            _fail("input_hash", f"cell {ordinal} input bytes changed")
        pre, auth = _object(pre_bytes, "preflight"), _object(auth_bytes, "authorization")
        if type(pre.get("schema_version")) is not int:
            _fail("input_contract", f"cell {ordinal} preflight schema is not an exact integer")
        for key in (
            "schema_version", "block_index", "cell_ordinal",
            "required_complete_cycles", "hard_timeout_s", "external_timeout_s",
            "automatic_retries", "cell_validator_version",
        ):
            if type(auth.get(key)) is not int:
                _fail("input_contract", f"cell {ordinal} authorization {key} is not an exact integer")
        arm, mode = label.split(":", 1)
        if (pre.get("schema_version") != 1 or pre.get("kind") != feasibility.SCHEMA or pre.get("verdict") != "PREFLIGHT_OK_NO_EXECUTION" or pre.get("revision") != freeze["revision"] or pre.get("arm") != arm or not _digest(pre.get("profile_sha256")) or not isinstance(pre.get("binary_sha256"), dict) or set(pre["binary_sha256"]) != _BINARIES or any(not _digest(v) for v in pre["binary_sha256"].values()) or set(auth) != _AUTH_KEYS or auth.get("schema_version") != 3 or auth.get("kind") != _AUTH_KIND or auth.get("campaign_id") != freeze["campaign_id"] or auth.get("block_index") != block or auth.get("campaign_freeze_sha256") != freeze_sha or auth.get("block_id") != f"{freeze['campaign_id']}-block-{block:02d}" or auth.get("block_order") != list(order) or auth.get("cell_ordinal") != within or auth.get("revision") != freeze["revision"] or auth.get("profile_sha256") != pre["profile_sha256"] or auth.get("arm") != arm or auth.get("quota_mode") != mode or auth.get("preflight_sha256") != hashlib.sha256(pre_bytes).hexdigest() or auth.get("binary_sha256") != pre["binary_sha256"] or auth.get("output_root") != str(output) or auth.get("required_complete_cycles") != 5 or auth.get("hard_timeout_s") != 480 or auth.get("external_timeout_s") != 720 or auth.get("automatic_retries") != 0 or auth.get("claim_eligible") is not False or auth.get("figure_eligible") is not False or auth.get("approval_ref") != approval_ref or not _timestamp(auth.get("approved_at_utc")) or auth.get("executor_receipt_schema") != _RECEIPT_SCHEMA or auth.get("cell_validator_version") != 7 or auth.get("process_cleanup_required") is not True):
            _fail("input_contract", f"cell {ordinal} inputs differ from v7 contract")
        identities.add((str(pre["revision"]), str(pre["profile_sha256"]), json.dumps(pre["binary_sha256"], sort_keys=True)))
    if len(identities) != 1: _fail("cross_cell_identity", "preflight identity differs across cells")
    return dict(manifest)


def validate_manifest_v7(manifest: Mapping[str, object], *, freeze_bytes: bytes, approval_ref: str) -> dict[str, object]:
    result: dict[str, object] = {"verdict": "INCOMPLETE", "claim_eligible": False, "figure_eligible": False, "thesis_result_eligible": False}
    try:
        accepted = _manifest_or_raise(manifest, freeze_bytes=freeze_bytes, approval_ref=approval_ref)
        result.update({"verdict": "PASS", "campaign_id": accepted["campaign_id"], "cell_count": 24})
    except (_InvalidSequence, OSError, TypeError, ValueError) as error:
        result.update({"reason_code": getattr(error, "code", "manifest_input_error"), "detail": str(error)})
    return result


def prepare_sequence_v7(freeze_file: Path, *, approval_ref: str, approved_at_utc: str) -> dict[str, object]:
    """Create fresh v3 authorizations and a v7 manifest without launching a cell."""
    result: dict[str, object] = {"verdict": "INCOMPLETE", "claim_eligible": False, "figure_eligible": False, "thesis_result_eligible": False}
    try:
        freeze_bytes = _read(Path(freeze_file), "campaign freeze"); freeze = _validate_freeze_or_raise(_object(freeze_bytes, "campaign freeze"))
        if not approval_ref or not _timestamp(approved_at_utc): _fail("approval", "exact explicit approval and UTC time required")
        output, evidence = Path(str(freeze["output_parent"])), Path(str(freeze["evidence_dir"]))
        if output.exists() or output.is_symlink() or evidence.exists() or evidence.is_symlink(): _fail("fresh_paths", "v7 output/evidence roots must be fresh")
        slots: list[tuple[int, int, str, dict[str, object], bytes]] = []
        for block, order_name in enumerate(_ORDERS, 1):
            for within, label in enumerate(FORWARD if order_name == "forward" else REVERSE, 1):
                arm = label.split(":", 1)[0]
                pre = feasibility.preflight(repository=REPOSITORY, app_binary=REPOSITORY / "build-adaptive/examples/hotstuff-app", keygen_binary=REPOSITORY / "build-adaptive/hotstuff-keygen", tls_keygen_binary=REPOSITORY / "build-adaptive/hotstuff-tls-keygen", native_digest_binary=REPOSITORY / "build-adaptive/examples/static-epoch0-digest", arm=arm)
                if pre.get("verdict") != "PREFLIGHT_OK_NO_EXECUTION" or pre.get("revision") != freeze["revision"] or pre.get("arm") != arm: _fail("preflight_contract", "live preflight differs from freeze")
                slots.append((block, within, label, pre, feasibility.canonical_json(pre)))
        if len({(str(p[3].get("profile_sha256")), json.dumps(p[3].get("binary_sha256"), sort_keys=True)) for p in slots}) != 1: _fail("preflight_identity", "preflight identities differ")
        evidence.mkdir(parents=True, exist_ok=False); freeze_sha = hashlib.sha256(freeze_bytes).hexdigest(); cells: list[dict[str, object]] = []
        for ordinal, (block, within, label, pre, pre_bytes) in enumerate(slots, 1):
            order = FORWARD if _ORDERS[block - 1] == "forward" else REVERSE; arm, mode = label.split(":", 1)
            output_root = output / f"block-{block:02d}" / label.replace(":", "-"); inputs = evidence / "inputs" / f"block-{block:02d}" / f"cell-{within:02d}"
            pre_path, auth_path = inputs / "preflight.json", inputs / "authorization.json"; _write_bytes_new(pre_path, pre_bytes)
            auth = {"schema_version": 3, "kind": _AUTH_KIND, "campaign_id": freeze["campaign_id"], "block_index": block, "campaign_freeze_sha256": freeze_sha, "block_id": f"{freeze['campaign_id']}-block-{block:02d}", "block_order": list(order), "cell_ordinal": within, "revision": freeze["revision"], "profile_sha256": pre["profile_sha256"], "arm": arm, "quota_mode": mode, "preflight_sha256": hashlib.sha256(pre_bytes).hexdigest(), "binary_sha256": pre["binary_sha256"], "output_root": str(output_root), "required_complete_cycles": 5, "hard_timeout_s": 480, "external_timeout_s": 720, "automatic_retries": 0, "claim_eligible": False, "figure_eligible": False, "approval_ref": approval_ref, "approved_at_utc": approved_at_utc, "executor_receipt_schema": _RECEIPT_SCHEMA, "cell_validator_version": 7, "process_cleanup_required": True}
            auth_bytes = _canonical(auth); _write_bytes_new(auth_path, auth_bytes)
            cells.append({"ordinal": ordinal, "block_index": block, "block_order": _ORDERS[block - 1], "cell_label": label, "block_cell_ordinal": within, "output_root": str(output_root), "preflight_path": str(pre_path), "preflight_sha256": hashlib.sha256(pre_bytes).hexdigest(), "authorization_path": str(auth_path), "authorization_sha256": hashlib.sha256(auth_bytes).hexdigest(), "hard_timeout_s": 480, "external_timeout_s": 720, "automatic_retries": 0})
        manifest = {"schema_version": 2, "kind": _MANIFEST_KIND, "campaign_id": freeze["campaign_id"], "revision": freeze["revision"], "campaign_freeze_sha256": freeze_sha, "approval_ref": approval_ref, "output_parent": str(output), "evidence_dir": str(evidence), "cells": cells}
        manifest_path = evidence / "manifest-v7.json"; _write_new(manifest_path, manifest); _manifest_or_raise(manifest, freeze_bytes=freeze_bytes, approval_ref=approval_ref)
        result.update({"verdict": "PREPARED_NO_EXECUTION", "campaign_id": freeze["campaign_id"], "manifest_path": str(manifest_path), "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(), "campaign_freeze_sha256": freeze_sha, "cell_count": 24})
    except (_InvalidSequence, OSError, TypeError, ValueError, feasibility.StaticE0FeasibilityError) as error:
        result.update({"reason_code": getattr(error, "code", "preparation_error"), "detail": str(error)})
    return result


def _command(cell: Mapping[str, object], *, freeze_file: Path, approval_ref: str, timeout: Path) -> list[str]:
    arm, mode = str(cell["cell_label"]).split(":", 1)
    return [str(timeout), "-s", "INT", "-k", "30s", "720s", sys.executable, str(REPOSITORY / "experiments/adaptive/run_n31_static_e0_local_executor.py"), "run", "--arm", arm, "--preflight", str(cell["preflight_path"]), "--authorization", str(cell["authorization_path"]), "--output", str(cell["output_root"]), "--hard-timeout-s", "480", "--quota-mode", mode, "--campaign-freeze-file", str(freeze_file), "--campaign-approval-ref", approval_ref]


def _verify_live_preflight_v7(cell: Mapping[str, object], revision: str) -> None:
    """Recheck pushed, clean source and binary identity before each launch."""

    arm, _mode = str(cell["cell_label"]).split(":", 1)
    receipt = feasibility.preflight(
        repository=REPOSITORY,
        app_binary=REPOSITORY / "build-adaptive/examples/hotstuff-app",
        keygen_binary=REPOSITORY / "build-adaptive/hotstuff-keygen",
        tls_keygen_binary=REPOSITORY / "build-adaptive/hotstuff-tls-keygen",
        native_digest_binary=REPOSITORY / "build-adaptive/examples/static-epoch0-digest",
        arm=arm,
    )
    expected = _read(Path(str(cell["preflight_path"])), "cell preflight")
    if receipt.get("revision") != revision or feasibility.canonical_json(receipt) != expected:
        _fail("live_preflight_drift", "clean source, binary, or preflight differs")


def execute_sequence_v7(manifest_path: Path, *, manifest_sha256: str, freeze_file: Path, approval_ref: str) -> dict[str, object]:
    """Execute each frozen v7 cell once; stop permanently at the first failed gate."""
    result: dict[str, object] = {"schema_version": 2, "kind": "kauri-w16-cpu-repeat-sequence-result-v7", "verdict": "STOPPED", "claim_eligible": False, "figure_eligible": False, "thesis_result_eligible": False, "technical_improvement_gate_passed": False, "direction_status": "NOT_EVALUATED", "attempted_cells": 0, "first_failure_ordinal": None, "cells": [], "blocks": []}
    execution: Path | None = None
    try:
        if not _digest(manifest_sha256): _fail("manifest_hash", "manifest hash is malformed")
        manifest_bytes, freeze_bytes = _read(Path(manifest_path), "manifest"), _read(Path(freeze_file), "freeze")
        if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256: _fail("manifest_hash", "manifest differs from approved bytes")
        manifest = _object(manifest_bytes, "manifest"); _manifest_or_raise(manifest, freeze_bytes=freeze_bytes, approval_ref=approval_ref); freeze = _validate_freeze_or_raise(_object(freeze_bytes, "freeze"))
        _verify_live_preflight_v7(manifest["cells"][0], str(freeze["revision"]))
        execution = Path(str(manifest["evidence_dir"])) / "execution-v7"
        if execution.exists() or execution.is_symlink() or any(Path(str(c["output_root"])).exists() for c in manifest["cells"]): _fail("execution_exists", "execution/output paths already exist")
        execution.mkdir(parents=True); _write_bytes_new(execution / "snapshot" / "freeze-v2.json", freeze_bytes); _write_bytes_new(execution / "snapshot" / "manifest-v7.json", manifest_bytes)
        _write_new(execution / "sequence-start-v7.json", {"schema_version": 2, "campaign_id": manifest["campaign_id"], "started_at_utc": _now(), "manifest_sha256": manifest_sha256, "campaign_freeze_sha256": hashlib.sha256(freeze_bytes).hexdigest(), "approval_ref": approval_ref, "revision": freeze["revision"], "host_expected": freeze["host"], "booking_id_expected": freeze["booking_id"], "timeout_path": freeze["timeout_path"], "timeout_sha256": freeze["timeout_sha256"], "claim_eligible": False})
        blocks: list[list[Path]] = []; roots: list[Path] = []
        for ordinal, cell in enumerate(manifest["cells"], 1):
            if _read(Path(manifest_path), "manifest") != manifest_bytes or _read(Path(freeze_file), "freeze") != freeze_bytes: _fail("input_changed", "manifest/freeze bytes changed")
            _manifest_or_raise(manifest, freeze_bytes=freeze_bytes, approval_ref=approval_ref)
            _verify_live_preflight_v7(cell, str(freeze["revision"]))
            outside = execution / f"block-{int(cell['block_index']):02d}" / f"cell-{int(cell['block_cell_ordinal']):02d}"; outside.mkdir(parents=True)
            stdout, stderr, validation = outside / "wrapper.stdout", outside / "wrapper.stderr", outside / "validation-v7.json"; command = _command(cell, freeze_file=Path(freeze_file), approval_ref=approval_ref, timeout=Path(str(freeze["timeout_path"])))
            _write_new(outside / "cell-intent.json", {"ordinal": ordinal, "block_index": cell["block_index"], "cell_label": cell["cell_label"], "output_root": cell["output_root"], "command": command, "cwd": str(REPOSITORY), "started_at_utc": _now(), "preflight_sha256": cell["preflight_sha256"], "authorization_sha256": cell["authorization_sha256"], "timeout_sha256": freeze["timeout_sha256"], "stdout_path": str(stdout), "stderr_path": str(stderr), "validation_path": str(validation)})
            result["attempted_cells"] = ordinal
            with stdout.open("xb") as out, stderr.open("xb") as err:
                process = subprocess.Popen(command, cwd=REPOSITORY, stdout=out, stderr=err, start_new_session=True)
                controller_interruptions = 0
                while True:
                    try:
                        code = process.wait()
                        break
                    except KeyboardInterrupt:
                        # GNU timeout still owns the cell.  Await that same
                        # child; do not seal STOPPED while it can write output.
                        controller_interruptions += 1
            record = {"ordinal": ordinal, "block_index": cell["block_index"], "cell_label": cell["cell_label"], "output_root": cell["output_root"], "command": command, "wrapper_exit_code": code, "stdout_path": str(stdout), "stderr_path": str(stderr), "validation_path": str(validation), "validation_verdict": None}
            if controller_interruptions:
                record["controller_interruptions"] = controller_interruptions
                _write_new(validation, {"verdict": "NOT_RUN_CONTROLLER_INTERRUPTED", "detail": "supervised cell completed after controller interruption; output preserved"})
                record["validation_verdict"] = "NOT_RUN_CONTROLLER_INTERRUPTED"
                result["first_failure_ordinal"] = ordinal
                result["reason_code"] = "controller_interrupted"
                _write_new(outside / "cell-result.json", record)
                result["cells"].append(record)
                break
            if code != 0:
                _write_new(validation, {"verdict": "NOT_RUN_NONZERO_WRAPPER", "detail": "nonzero wrapper; output preserved"}); record["validation_verdict"] = "NOT_RUN_NONZERO_WRAPPER"; result["first_failure_ordinal"] = ordinal; result["reason_code"] = "wrapper_nonzero"; _write_new(outside / "cell-result.json", record); result["cells"].append(record); break
            checked = validate_w16_output_v7(Path(str(cell["output_root"]))); _write_new(validation, checked); record["validation_verdict"] = checked.get("verdict"); _write_new(outside / "cell-result.json", record); result["cells"].append(record)
            if checked.get("verdict") != "PASS": result["first_failure_ordinal"] = ordinal; result["reason_code"] = "cell_validation"; break
            roots.append(Path(str(cell["output_root"])))
            if int(cell["block_cell_ordinal"]) == 4:
                block = validate_w16_campaign_block_v7(roots); path = execution / f"block-{int(cell['block_index']):02d}-validation-v7.json"; _write_new(path, block); result["blocks"].append({"block_index": cell["block_index"], "verdict": block.get("verdict"), "validation_path": str(path)})
                if block.get("verdict") != "PASS": result["first_failure_ordinal"] = ordinal; result["reason_code"] = "block_validation"; break
                blocks.append(roots); roots = []
        if result["first_failure_ordinal"] is None and len(blocks) == 6:
            campaign = validate_w16_cpu_campaign_v7(blocks); path = execution / "campaign-validation-v7.json"; _write_new(path, campaign); result["campaign_validation_path"] = str(path); result["campaign_validation_verdict"] = campaign.get("verdict")
            if campaign.get("verdict") == "PASS":
                result["technical_improvement_gate_passed"] = campaign.get("technical_improvement_gate_passed") is True; result["direction_status"] = campaign.get("direction_gate", {}).get("status", "NOT_EVALUATED"); result["verdict"] = "PASS"
            else: result["first_failure_ordinal"] = 24; result["reason_code"] = "campaign_validation"
        _write_new(execution / "sequence-result-v7.json", result)
    except (_InvalidSequence, OSError, ValueError, TypeError, KeyboardInterrupt, feasibility.StaticE0FeasibilityError) as error:
        result.update({"verdict": "STOPPED", "reason_code": getattr(error, "code", "sequence_input_error"), "detail": str(error)})
        if execution is not None and execution.is_dir():
            try: _write_new(execution / "sequence-abort-v7.json", result)
            except OSError: pass
    return result
