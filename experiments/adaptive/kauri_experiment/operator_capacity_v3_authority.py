"""Produce an external, non-accepting W18 raw-validation authority.

This is deliberately not part of the runner.  It can only be invoked after a
sealed successful runner receipt and reopens every raw input before pinning its
bytes.  It also reruns the two native verifiers; retained verifier receipts are
evidence, never a substitute for an independent verification invocation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any, Callable, Mapping, Sequence

from . import operator_capacity_consumption_audit as consumption_audit


N = 31
_AUTHORITY_KIND = "kauri-n31-operator-capacity-v3-raw-validation-authority-v1"
_RECEIPT_KIND = "kauri-n31-operator-capacity-v3-local-shakedown-receipt-v1"
_HEX = frozenset("0123456789abcdef")


class OperatorCapacityV3AuthorityError(ValueError):
    """The independent authority cannot honestly be issued."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path, label: str, maximum: int = 8 * 1024 * 1024) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise OperatorCapacityV3AuthorityError(f"{label} is not a readable regular file") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > maximum:
            raise OperatorCapacityV3AuthorityError(f"{label} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                raise OperatorCapacityV3AuthorityError(f"{label} changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1) or os.fstat(fd) != before:
            raise OperatorCapacityV3AuthorityError(f"{label} changed during read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _pairs(label: str):
    def decode(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise OperatorCapacityV3AuthorityError(f"{label} repeats JSON field {key}")
            result[key] = value
        return result
    return decode


def _json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs(label),
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OperatorCapacityV3AuthorityError(f"{label} is not strict ASCII JSON") from exc
    if not isinstance(value, dict):
        raise OperatorCapacityV3AuthorityError(f"{label} is not an object")
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii") + b"\n"


def _relative_regular(root: Path, relative: object, label: str) -> tuple[str, bytes]:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise OperatorCapacityV3AuthorityError(f"{label} path is invalid")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise OperatorCapacityV3AuthorityError(f"{label} escapes the sealed run root") from exc
    return relative, _read(path, label, 128 * 1024)


def _sealed_receipt(root: Path) -> tuple[bytes, dict[str, Any]]:
    raw = _read(root / "runtime/local-shakedown-receipt.json", "runner receipt", 64 * 1024)
    receipt = _json(raw, "runner receipt")
    expected = {"schema_version", "kind", "verdict", "claim_eligible", "figure_eligible",
                "automatic_retries", "execution_request_sha256", "manager_exit_code", "failure",
                "cleanup", "fresh_native_stage_a_receipt_sha256", "raw_validation_required",
                "e1_measurement_window", "manager_exit_code_after_cleanup",
                "manager_success_terminal_verified"}
    if (set(receipt) != expected or receipt.get("schema_version") != 1 or receipt.get("kind") != _RECEIPT_KIND or
            receipt.get("verdict") != "PROCESS_COMPLETED_PENDING_RAW_VALIDATION" or
            receipt.get("claim_eligible") is not False or receipt.get("figure_eligible") is not False or
            receipt.get("automatic_retries") != 0 or receipt.get("manager_exit_code") not in (None, 0) or
            not isinstance(receipt.get("manager_exit_code_after_cleanup"), int) or
            receipt.get("manager_success_terminal_verified") is not True or
            receipt.get("failure") is not None or receipt.get("raw_validation_required") is not True):
        raise OperatorCapacityV3AuthorityError("runner receipt is not sealed pending raw validation")
    return raw, receipt


def _stream(root: Path, relative: str, *, source_kind: str, source_id: str, run_id: str) -> str:
    raw = _read(root / relative, f"{source_id} event stream")
    if not raw.endswith(b"\n"):
        raise OperatorCapacityV3AuthorityError(f"{source_id} event stream has incomplete framing")
    lines = raw.splitlines()
    if not lines:
        raise OperatorCapacityV3AuthorityError(f"{source_id} event stream is empty")
    for line in lines:
        event = _json(line, f"{source_id} event")
        if (event.get("event_schema_version") != 1 or event.get("run_id") != run_id or
                event.get("source_kind") != source_kind or event.get("source_id") != source_id):
            raise OperatorCapacityV3AuthorityError(f"{source_id} event envelope differs")
    return _sha(raw)


def _rerun(command: Sequence[str], label: str, retained: bytes,
           *, runner: Callable[..., Any]) -> None:
    if (not isinstance(command, Sequence) or isinstance(command, (str, bytes)) or not command or
            not all(isinstance(item, str) and item for item in command)):
        raise OperatorCapacityV3AuthorityError(f"explicit native {label} verifier command is required")
    if "--output" in command:
        raise OperatorCapacityV3AuthorityError(f"native {label} verifier command must not preselect output")
    with tempfile.TemporaryDirectory(prefix=f"kauri-w18-{label.lower()}-verify-") as directory:
        output = Path(directory) / "receipt.json"
        try:
            invoked = runner((*command, "--output", str(output)), capture_output=True,
                             check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperatorCapacityV3AuthorityError(f"native {label} verifier could not complete") from exc
        if getattr(invoked, "returncode", None) != 0:
            raise OperatorCapacityV3AuthorityError(f"native {label} verifier rejected sealed raw inputs")
        fresh = _read(output, f"fresh native {label} verifier receipt", 128 * 1024)
    old = _json(retained, f"retained {label} verifier receipt")
    new = _json(fresh, f"fresh native {label} verifier receipt")
    ignored = {"verification_monotonic_raw_ns"} if label == "Stage-A" else set()
    if set(old) != set(new) or any(new[key] != value for key, value in old.items() if key not in ignored):
        raise OperatorCapacityV3AuthorityError(f"fresh native {label} verifier receipt differs from retained receipt")
    if ignored and (type(new.get("verification_monotonic_raw_ns")) is not int or
                    new["verification_monotonic_raw_ns"] <= 0):
        raise OperatorCapacityV3AuthorityError("fresh native Stage-A verifier timestamp is invalid")


def _hex(value: object, label: str, length: int = 64) -> str:
    if not isinstance(value, str) or len(value) != length or any(char not in _HEX for char in value):
        raise OperatorCapacityV3AuthorityError(f"{label} is not lower-case hexadecimal")
    return value


def _stage_b_command_is_bound(command: Sequence[str], *, root: Path,
                              manifest: Mapping[str, Any], pins: Mapping[str, object]) -> tuple[str, ...]:
    if (not isinstance(command, Sequence) or isinstance(command, (str, bytes)) or len(command) != 21 or
            not all(isinstance(item, str) and item for item in command) or "--output" in command):
        raise OperatorCapacityV3AuthorityError("explicit complete native Stage-B verifier command is required")
    values = dict(zip(command[1::2], command[2::2]))
    expected_flags = {"--epoch0-tree-file", "--stage-b-authorization-wire", "--issuer-id",
                      "--issuer-reference", "--issuer-public-key-hex", "--issuer-public-key-fingerprint",
                      "--label-issuer-reference", "--approved-capacity-digest", "--arm", "--source-revision"}
    if set(values) != expected_flags:
        raise OperatorCapacityV3AuthorityError("native Stage-B verifier flags differ from the sealed contract")
    expected = {
        "--epoch0-tree-file": str(root / "config/epoch0.tree"),
        "--stage-b-authorization-wire": str(root / "raw/stage-b-authorization.wire"),
        "--issuer-id": str(pins.get("epoch_change_issuer_id")),
        "--issuer-reference": str(pins.get("epoch_change_issuer_reference")),
        "--issuer-public-key-fingerprint": str(pins.get("epoch_change_issuer_public_key_fingerprint")),
        "--label-issuer-reference": str(pins.get("label_issuer_reference")),
        "--approved-capacity-digest": str(pins.get("approved_capacity_digest")),
        "--arm": str(pins.get("arm")), "--source-revision": str(manifest.get("revision")),
    }
    if any(values.get(flag) != value for flag, value in expected.items()):
        raise OperatorCapacityV3AuthorityError("native Stage-B verifier arguments differ from sealed inputs")
    _hex(values["--issuer-public-key-hex"], "native Stage-B issuer public key", 66)
    approval_raw = _read(root / "runtime/tool-identity-approval.json", "tool-identity approval", 256 * 1024)
    approval = _json(approval_raw, "tool-identity approval")
    if (approval_raw != _canonical(approval) or _sha(approval_raw) != manifest.get("tool_identity_approval_receipt_sha256") or
            approval.get("revision") != manifest.get("revision") or not isinstance(approval.get("binary_sha256"), dict)):
        raise OperatorCapacityV3AuthorityError("tool-identity approval is not bound to materialization manifest")
    approved_binary = _hex(approval["binary_sha256"].get("stage_b_authorization_verifier"),
                           "approved Stage-B verifier binary")
    if _sha(_read(Path(command[0]), "native Stage-B verifier binary", 512 * 1024 * 1024)) != approved_binary:
        raise OperatorCapacityV3AuthorityError("native Stage-B verifier binary differs from approved identity")
    return tuple(command)


def _stage_a_command_is_bound(command: Sequence[str] | None, *, root: Path,
                              manifest: Mapping[str, Any]) -> tuple[str, ...]:
    arguments = manifest.get("stage_a_verifier_arguments")
    if (not isinstance(command, Sequence) or isinstance(command, (str, bytes)) or
            not isinstance(arguments, list) or not all(isinstance(item, str) and item for item in arguments) or
            tuple(command[1:]) != tuple(arguments) or "--output" in command):
        raise OperatorCapacityV3AuthorityError("native Stage-A verifier command differs from materialization manifest")
    approval_raw = _read(root / "runtime/tool-identity-approval.json", "tool-identity approval", 256 * 1024)
    approval = _json(approval_raw, "tool-identity approval")
    if (approval_raw != _canonical(approval) or _sha(approval_raw) != manifest.get("tool_identity_approval_receipt_sha256") or
            not isinstance(approval.get("binary_sha256"), dict)):
        raise OperatorCapacityV3AuthorityError("tool-identity approval is not bound to materialization manifest")
    approved_binary = _hex(approval["binary_sha256"].get("stage_a_envelope_verifier"),
                           "approved Stage-A verifier binary")
    if _sha(_read(Path(command[0]), "native Stage-A verifier binary", 512 * 1024 * 1024)) != approved_binary:
        raise OperatorCapacityV3AuthorityError("native Stage-A verifier binary differs from approved identity")
    return tuple(command)


def _materialize_stage_b_receipt(command: Sequence[str] | None, *, root: Path,
                                 manifest: Mapping[str, Any], pins: Mapping[str, object],
                                 target: Path, runner: Callable[..., Any]) -> bytes:
    bound = _stage_b_command_is_bound(command or (), root=root, manifest=manifest, pins=pins)
    if target.exists() or target.is_symlink():
        raise OperatorCapacityV3AuthorityError("Stage-B verifier receipt output must be fresh")
    try:
        invoked = runner((*bound, "--output", str(target)), capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperatorCapacityV3AuthorityError("native Stage-B verifier could not complete") from exc
    if getattr(invoked, "returncode", None) != 0:
        raise OperatorCapacityV3AuthorityError("native Stage-B verifier rejected sealed raw inputs")
    return _read(target, "native Stage-B verifier receipt", 128 * 1024)


def produce_operator_capacity_v3_authority(
    root: Path, *, authority_path: Path, pins: Mapping[str, object],
    stage_a_command: Sequence[str] | None, stage_b_command: Sequence[str] | None,
    stage_a_receipt: str = "runtime/stage-a-verifier-receipt.json",
    stage_b_receipt: str = "runtime/stage-b-verifier-receipt.json",
    native_runner: Callable[..., Any] = subprocess.run,
) -> dict[str, object]:
    """Create an external authority JSON, or fail closed without an authority.

    ``authority_path`` must be outside the runner-owned root and must not
    exist.  The output grants no claim or figure eligibility; the raw validator
    remains the only consumer of this pin set.
    """
    root = Path(root).resolve()
    authority_path = Path(authority_path).resolve()
    try:
        authority_path.relative_to(root)
    except ValueError:
        pass
    else:
        raise OperatorCapacityV3AuthorityError("authority output must be outside runner-owned output")
    if authority_path.exists() or authority_path.is_symlink():
        raise OperatorCapacityV3AuthorityError("authority output must be fresh")
    if not authority_path.parent.is_dir():
        raise OperatorCapacityV3AuthorityError("external authority parent does not exist")
    receipt_raw, _receipt = _sealed_receipt(root)
    run_id = pins.get("run_id") if isinstance(pins, Mapping) else None
    if not isinstance(run_id, str) or not run_id:
        raise OperatorCapacityV3AuthorityError("authority pins lack a run ID")
    manifest_raw = _read(root / "materialization-manifest.json", "materialization manifest", 256 * 1024)
    manifest = _json(manifest_raw, "materialization manifest")
    if (manifest.get("verdict") != "MATERIALIZED_NO_EXECUTION" or
            manifest.get("protocol") != {"N": 31, "Q": 21, "tree_count": 21}):
        raise OperatorCapacityV3AuthorityError("materialization manifest is not frozen W18 N31")
    stage_a_relative, stage_a_raw = _relative_regular(root, stage_a_receipt, "Stage-A verifier receipt")
    if not isinstance(stage_b_receipt, str) or Path(stage_b_receipt).is_absolute() or stage_b_receipt != "runtime/stage-b-verifier-receipt.json":
        raise OperatorCapacityV3AuthorityError("Stage-B verifier receipt must use the sealed runtime target")
    stage_b_relative = stage_b_receipt
    stage_b_path = root / stage_b_relative
    if stage_b_path.exists() or stage_b_path.is_symlink():
        stage_b_raw = _read(stage_b_path, "Stage-B verifier receipt", 128 * 1024)
    else:
        stage_b_raw = _materialize_stage_b_receipt(
            stage_b_command, root=root, manifest=manifest, pins=pins, target=stage_b_path,
            runner=native_runner)
    if manifest.get("stage_a_verifier_receipt_sha256") != _sha(stage_a_raw):
        raise OperatorCapacityV3AuthorityError("retained Stage-A receipt differs from materialization manifest")
    streams: dict[str, str] = {
        "manager": _stream(root, "raw/manager-events.jsonl", source_kind="adaptation_manager",
                           source_id="adaptive-manager", run_id=run_id),
    }
    for replica in range(N):
        streams[f"replica-{replica}"] = _stream(
            root, f"raw/replica-{replica}.jsonl", source_kind="replica",
            source_id=f"replica-{replica}", run_id=run_id)
    samples = _read(root / "raw/cpu-quota-samples.jsonl", "CPU quota samples")
    rounds = _read(root / "raw/cpu-quota-monitor-rounds.jsonl", "CPU quota monitor rounds")
    frozen_contract = _read(root / "runtime/frozen-cpu-quota-contract.json",
                            "frozen CPU quota contract", 256 * 1024)
    contract = _read(root / "runtime/cpu-quota-contract.json", "CPU quota contract", 256 * 1024)
    launch = _read(root / "runtime/cpu-quota-launch.json", "CPU quota launch record", 256 * 1024)
    # Audit all retained Stage-A/B, successor and consumption bytes before
    # issuing their hashes; this independently checks their cross-bindings.
    try:
        consumption_audit.audit_consumption_chain(
            stage_a_wire=root / "config/stage-a-envelope.wire",
            stage_b_wire=root / "raw/stage-b-authorization.wire",
            successor_bundle=root / "transitions/e0-to-e1-operator-capacity/successor.bundle",
            consumption_record=root / "raw/consumption.json",
            stage_a_verifier_receipt=root / stage_a_relative,
            stage_b_verifier_receipt=root / stage_b_relative, pins=pins)
    except consumption_audit.ConsumptionAuditError as exc:
        raise OperatorCapacityV3AuthorityError(f"Stage-A/B consumption chain is incomplete: {exc}") from exc
    _rerun(_stage_a_command_is_bound(stage_a_command, root=root, manifest=manifest),
           "Stage-A", stage_a_raw, runner=native_runner)
    _rerun(_stage_b_command_is_bound(stage_b_command or (), root=root, manifest=manifest, pins=pins),
           "Stage-B", stage_b_raw, runner=native_runner)
    authority = {
        "schema_version": 1, "kind": _AUTHORITY_KIND,
        "runner_receipt_sha256": _sha(receipt_raw),
        "materialization_manifest_sha256": _sha(manifest_raw),
        "event_stream_sha256": streams,
        "cpu_quota_samples_sha256": _sha(samples),
        "cpu_quota_rounds_sha256": _sha(rounds),
        "cpu_quota_frozen_contract_sha256": _sha(frozen_contract),
        "cpu_quota_contract_sha256": _sha(contract),
        "cpu_quota_launch_sha256": _sha(launch),
        "stage_a_verifier_receipt": stage_a_relative,
        "stage_b_verifier_receipt": stage_b_relative,
        "pins": dict(pins),
    }
    # The output is an authority *input* for validation, never a result.
    fd = os.open(authority_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        payload = _canonical(authority)
        offset = 0
        while offset < len(payload):
            count = os.write(fd, payload[offset:])
            if count <= 0:
                raise OperatorCapacityV3AuthorityError("cannot write external authority")
            offset += count
        os.fsync(fd)
    finally:
        os.close(fd)
    return authority
