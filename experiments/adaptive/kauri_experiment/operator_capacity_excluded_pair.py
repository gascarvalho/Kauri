"""First executable gate for the excluded local W18 treatment/sham shakedown.

The module only validates authority and post-backend terminal evidence.  It
does not contain a launcher because no existing N31 materializer can pass the
capacity Stage-A/Stage-B lifecycle through the production manager while also
proving identity-bound per-replica quota scopes.  ``run`` must therefore fail
closed until that adapter is supplied; it never fabricates raw evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping, Sequence

from . import operator_capacity_raw_validation as raw_validation
from .operator_capacity_stage_a_preflight import (
    PREFLIGHT_KIND,
    REQUIRED_BINARIES,
    REQUEST_KIND,
    canonical_json,
)


class OperatorCapacityExcludedPairError(RuntimeError):
    """The excluded pair is not exactly authorized or is not terminal."""


_ARMS = ("sham", "treatment")
_HEX = frozenset("0123456789abcdef")
_APPROVAL_KEYS = frozenset({
    "schema_version", "kind", "verdict", "request_sha256", "approval_ref", "approved_at_utc",
})
_PREFLIGHT_KEYS = frozenset({
    "schema_version", "kind", "verdict", "claim_eligible", "figure_eligible", "revision",
    "protocol", "arm", "stage_a_native_arm", "output_root", "binary_sha256", "input_sha256",
    "native_verifier_receipt_sha256", "tool_identity_approval_receipt_sha256",
})
_REQUEST_KEYS = frozenset({
    "schema_version", "kind", "verdict", "claim_eligible", "figure_eligible", "preflight_sha256",
    "revision", "arm", "output_root", "binary_sha256", "input_sha256",
    "native_verifier_receipt_sha256", "tool_identity_approval_receipt_sha256",
})
_INPUT_KEYS = frozenset({
    "epoch0_tree_file", "capacity_snapshot_wire", "stage_a_envelope_wire", "quota_profile",
})
_NATIVE_ARM = {"sham": "exact_copy_sham", "treatment": "fast_priority_treatment"}


def _fail(message: str) -> None:
    raise OperatorCapacityExcludedPairError(message)


def _read_regular(path: Path, maximum: int, label: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        _fail(f"{label} is not a readable regular file")
        raise AssertionError from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > maximum:
            _fail(f"{label} size or type is invalid")
        result = b""
        while len(result) < before.st_size:
            chunk = os.read(fd, before.st_size - len(result))
            if not chunk:
                _fail(f"{label} changed during read")
            result += chunk
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            _fail(f"{label} changed during read")
        return result
    finally:
        os.close(fd)


def _object_pairs(label: str):
    def decode(items: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(f"{label} repeats JSON field {key}")
            result[key] = value
        return result
    return decode


def _document(path: Path, keys: frozenset[str], label: str) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular(path, 128 * 1024, label)
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_object_pairs(label))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"{label} is not strict ASCII JSON")
        raise AssertionError from exc
    if not isinstance(value, dict) or set(value) != keys:
        _fail(f"{label} schema differs")
    if raw != canonical_json(value):
        _fail(f"{label} is not canonical")
    return value, raw


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in _HEX for c in value):
        _fail(f"{label} is not lower-case SHA-256")
    return value


def _revision(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(c not in _HEX for c in value):
        _fail(f"{label} is not a lower-case Git revision")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        _fail(f"{label} is not positive")
    return value


def _digest_map(value: object, keys: frozenset[str], label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail(f"{label} keys differ from the frozen contract")
    return {key: _sha256(value[key], f"{label} {key}") for key in sorted(keys)}


def _arm_authority(
    *, arm: str, preflight_path: Path, request_path: Path, approval_path: Path,
    expected_approval_sha256: str, expected_output_root: Path,
) -> dict[str, object]:
    preflight, preflight_raw = _document(preflight_path, _PREFLIGHT_KEYS, f"{arm} preflight")
    request, request_raw = _document(request_path, _REQUEST_KEYS, f"{arm} request")
    approval, approval_raw = _document(approval_path, _APPROVAL_KEYS, f"{arm} execution approval")
    _sha256(expected_approval_sha256, f"{arm} expected execution approval SHA-256")
    if hashlib.sha256(approval_raw).hexdigest() != expected_approval_sha256:
        _fail(f"{arm} execution approval differs from caller pin")
    if (preflight["schema_version"] != 1 or preflight["kind"] != PREFLIGHT_KIND or
            preflight["verdict"] != "PREFLIGHT_OK_NO_EXECUTION" or preflight["arm"] != arm or
            preflight["claim_eligible"] is not False or preflight["figure_eligible"] is not False or
            preflight["protocol"] != {"N": 31, "Q": 21} or
            preflight["stage_a_native_arm"] != _NATIVE_ARM[arm] or
            preflight["output_root"] != str(expected_output_root.resolve())):
        _fail(f"{arm} preflight is not an exact W18 no-launch input")
    revision = _revision(preflight["revision"], f"{arm} preflight revision")
    binary_sha256 = _digest_map(preflight["binary_sha256"], REQUIRED_BINARIES, f"{arm} binary map")
    input_sha256 = _digest_map(preflight["input_sha256"], _INPUT_KEYS, f"{arm} input map")
    native_receipt_sha256 = _sha256(preflight["native_verifier_receipt_sha256"], f"{arm} native receipt")
    tool_approval_sha256 = _sha256(
        preflight["tool_identity_approval_receipt_sha256"], f"{arm} tool approval",
    )
    if (request["schema_version"] != 1 or request["kind"] != REQUEST_KIND or
            request["verdict"] != "EXECUTION_AUTHORIZATION_REQUEST_REQUIRED" or request["arm"] != arm or
            request["claim_eligible"] is not False or request["figure_eligible"] is not False or
            request["output_root"] != str(expected_output_root.resolve()) or
            request["preflight_sha256"] != hashlib.sha256(preflight_raw).hexdigest()):
        _fail(f"{arm} request does not bind its exact no-launch preflight")
    if (request["revision"] != preflight["revision"] or
            _digest_map(request["binary_sha256"], REQUIRED_BINARIES, f"{arm} request binary map") != binary_sha256 or
            _digest_map(request["input_sha256"], _INPUT_KEYS, f"{arm} request input map") != input_sha256 or
            _sha256(request["native_verifier_receipt_sha256"], f"{arm} request native receipt") != native_receipt_sha256 or
            _sha256(request["tool_identity_approval_receipt_sha256"], f"{arm} request tool approval") != tool_approval_sha256):
        _fail(f"{arm} request duplicates differ from its preflight")
    if (approval["schema_version"] != 1 or approval["kind"] != "kauri-n31-operator-capacity-execution-approval-v1" or
            approval["verdict"] != "EXTERNAL_EXECUTION_APPROVED" or
            approval["request_sha256"] != hashlib.sha256(request_raw).hexdigest() or
            not isinstance(approval["approval_ref"], str) or not approval["approval_ref"] or
            not isinstance(approval["approved_at_utc"], str) or not approval["approved_at_utc"]):
        _fail(f"{arm} execution approval does not bind its exact request")
    return {
        "preflight_sha256": hashlib.sha256(preflight_raw).hexdigest(),
        "request_sha256": hashlib.sha256(request_raw).hexdigest(),
        "execution_approval_sha256": hashlib.sha256(approval_raw).hexdigest(),
        "revision": revision, "binary_sha256": binary_sha256,
        "input_sha256": input_sha256,
        "stage_a_envelope_sha256": input_sha256["stage_a_envelope_wire"],
        "native_verifier_receipt_sha256": native_receipt_sha256,
    }


def prepare_excluded_pair(
    *, output_root: Path, arm_inputs: Mapping[str, Mapping[str, Path | str]],
    hard_timeout_s: int,
) -> dict[str, object]:
    """Validate both independently approved arms without creating output or launching."""
    root = Path(output_root)
    if root.exists() or root.is_symlink():
        _fail("excluded-pair output root must be fresh")
    if set(arm_inputs) != set(_ARMS):
        _fail("excluded pair requires exactly sham and treatment authority inputs")
    timeout = _positive(hard_timeout_s, "hard timeout")
    authorities: dict[str, dict[str, object]] = {}
    for arm in _ARMS:
        item = arm_inputs[arm]
        if set(item) != {"preflight", "request", "approval", "expected_approval_sha256"}:
            _fail(f"{arm} authority input schema differs")
        authorities[arm] = _arm_authority(
            arm=arm, preflight_path=Path(item["preflight"]), request_path=Path(item["request"]),
            approval_path=Path(item["approval"]), expected_approval_sha256=str(item["expected_approval_sha256"]),
            expected_output_root=root / arm,
        )
    shared = ("revision", "binary_sha256")
    if any(authorities["sham"][key] != authorities["treatment"][key] for key in shared):
        _fail("arms do not share exact revision and binary map")
    for key in ("epoch0_tree_file", "capacity_snapshot_wire", "quota_profile"):
        if authorities["sham"]["input_sha256"][key] != authorities["treatment"]["input_sha256"][key]:
            _fail("arms do not share frozen Epoch-0 tree, snapshot, and quota inputs")
    if (authorities["sham"]["stage_a_envelope_sha256"] ==
            authorities["treatment"]["stage_a_envelope_sha256"] or
            authorities["sham"]["native_verifier_receipt_sha256"] ==
            authorities["treatment"]["native_verifier_receipt_sha256"]):
        _fail("arms must retain distinct Stage-A envelope and native receipt bindings")
    return {
        "schema_version": 1,
        "kind": "kauri-n31-operator-capacity-excluded-local-pair-plan-v1",
        "verdict": "PAIR_AUTHORIZED_BACKEND_REQUIRED_NO_EXECUTION",
        "claim_eligible": False,
        "figure_eligible": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "output_root": str(root.resolve()),
        "schedule": list(_ARMS),
        "automatic_retries": 0,
        "replacement_policy": "none",
        "hard_timeout_s": timeout,
        "arms": authorities,
        "launch_permitted": False,
        "backend_requirement": (
            "A dedicated manager/quota-scope adapter must capture manager exit=0 and success terminal, "
            "all 31 raw activation/commit/quota/cleanup sources, then invoke the independent raw validator."
        ),
    }


def execution_not_implemented() -> None:
    """Prevent accidental use of the plan as a launcher."""
    _fail("W18 excluded-local launch is blocked: no manager/quota-scope backend is implemented")


def validate_terminal_arm(
    root: Path, *, authority_paths: Mapping[str, Path], pins: Mapping[str, object],
) -> dict[str, object]:
    """Require a successful terminal before the prospective schema check."""
    terminal, _ = _document(
        Path(root) / "raw/manager-terminal.json",
        frozenset({
            "schema_version", "run_id", "source_instance", "manager_exit_status",
            "session_terminal_reason", "terminal_monotonic_raw_ns",
            "successor_bundle_sha256", "hard_deadline_exhausted", "fatal_reason",
        }),
        "manager terminal",
    )
    bundle_sha = hashlib.sha256(_read_regular(
        Path(authority_paths["successor_bundle"]), 512 * 1024,
        "successor bundle",
    )).hexdigest()
    if (terminal["schema_version"] != 1 or terminal["run_id"] != pins.get("run_id") or
            terminal["source_instance"] != pins.get("source_instance") or
            type(terminal["manager_exit_status"]) is not int or
            terminal["manager_exit_status"] != 0 or
            terminal["session_terminal_reason"] != "acknowledgements_complete" or
            type(terminal["terminal_monotonic_raw_ns"]) is not int or
            terminal["terminal_monotonic_raw_ns"] <= 0 or
            type(pins.get("hard_deadline_monotonic_raw_ns")) is not int or
            terminal["terminal_monotonic_raw_ns"] >= pins["hard_deadline_monotonic_raw_ns"] or
            terminal["successor_bundle_sha256"] != bundle_sha or
            terminal["hard_deadline_exhausted"] is not False or
            terminal["fatal_reason"] is not None):
        _fail("manager terminal does not prove predeadline success")
    result = raw_validation.validate_operator_capacity_raw(
        Path(root), authority_paths=authority_paths, pins=pins,
    )
    if result.get("verdict") != "PROSPECTIVE_SCHEMA_VALID_NO_RAW_REPLAY_NO_CLAIM":
        _fail("prospective raw schema rejected: " + str(result.get("detail", "unknown")))
    return result
