"""No-launch Stage-A authority gate for the prospective N31 CPU study.

This module deliberately produces a preflight and an execution *request* only.
It cannot generate, amend, or infer the external tool-identity approval.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Mapping

from .operator_capacity_preflight import (
    PreflightError as FrozenQuotaProfileError,
    _validate_quota_profile_bytes,
)


PREFLIGHT_KIND = "kauri-n31-operator-capacity-stage-a-preflight-v1"
REQUEST_KIND = "kauri-n31-operator-capacity-stage-a-execution-request-v1"
APPROVAL_KIND = "kauri-n31-operator-capacity-tool-identity-approval-v1"
APPROVAL_VERDICT = "EXTERNAL_TOOL_IDENTITY_APPROVED"
VERIFIER_KIND = "kauri-operator-capacity-native-envelope-verification-receipt-v1"
VERIFIER_VERDICT = "NATIVE_ENVELOPE_VERIFIED_NO_EXECUTION"
REQUIRED_BINARIES = frozenset({
    "adaptation_manager", "hotstuff_app", "keygen", "tls_keygen", "capacity_digest",
    "epoch0_digest", "stage_a_envelope_signer", "stage_a_envelope_verifier",
    "stage_b_authorization_verifier", "identity_parity_verifier",
})


class StageAPreflightError(RuntimeError):
    pass


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StageAPreflightError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _hex(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise StageAPreflightError(f"{label} is not lowercase SHA-256")
    return value


def _read_regular(path: Path, maximum: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise StageAPreflightError(f"not a regular file: {path}") from error
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 0 or metadata.st_size > maximum:
            raise StageAPreflightError(f"not a bounded regular file: {path}")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                raise StageAPreflightError(f"input changed during read: {path}")
            chunks.append(chunk); remaining -= len(chunk)
        if os.read(fd, 1):
            raise StageAPreflightError(f"input changed during read: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _json(path: Path, maximum: int, label: str) -> tuple[dict[str, object], str]:
    payload = _read_regular(path, maximum)
    try:
        value = json.loads(payload.decode("ascii"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StageAPreflightError(f"{label} is not strict ASCII JSON") from error
    if not isinstance(value, dict):
        raise StageAPreflightError(f"{label} is not a JSON object")
    return value, hashlib.sha256(payload).hexdigest()


def _sha(path: Path, maximum: int) -> str:
    return hashlib.sha256(_read_regular(path, maximum)).hexdigest()


def _validated_quota_sha(path: Path) -> str:
    payload = _read_regular(path, 64 * 1024)
    try:
        _validate_quota_profile_bytes(payload)
    except FrozenQuotaProfileError as error:
        raise StageAPreflightError(
            "quota profile differs from the frozen N31 contract"
        ) from error
    return hashlib.sha256(payload).hexdigest()


def _revision(repository: Path) -> str:
    result = subprocess.run(("git", "-C", str(repository), "rev-parse", "HEAD"), text=True, capture_output=True, check=False)
    revision = result.stdout.strip()
    if result.returncode or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise StageAPreflightError("repository revision is unavailable")
    status = subprocess.run(("git", "-C", str(repository), "status", "--porcelain"), text=True, capture_output=True, check=False)
    if status.returncode or status.stdout:
        raise StageAPreflightError("repository is not clean")
    return revision


def _approval(path: Path, expected_sha256: str, revision: str, observed: Mapping[str, str]) -> str:
    _hex(expected_sha256, "expected tool-identity approval receipt SHA-256")
    document, actual_sha256 = _json(path, 64 * 1024, "tool-identity approval receipt")
    if actual_sha256 != expected_sha256:
        raise StageAPreflightError("external tool-identity approval receipt SHA-256 differs from caller pin")
    if (document.get("schema_version") != 1 or document.get("kind") != APPROVAL_KIND or
            document.get("verdict") != APPROVAL_VERDICT or document.get("revision") != revision):
        raise StageAPreflightError("external tool-identity approval receipt is not bound to this revision")
    if not isinstance(document.get("approval_ref"), str) or not document["approval_ref"]:
        raise StageAPreflightError("external tool-identity approval reference is missing")
    if not isinstance(document.get("approved_at_utc"), str) or not document["approved_at_utc"]:
        raise StageAPreflightError("external tool-identity approval timestamp is missing")
    binaries = document.get("binary_sha256")
    if not isinstance(binaries, dict) or set(binaries) != set(REQUIRED_BINARIES):
        raise StageAPreflightError("external tool-identity approval binary map is incomplete")
    approved = {name: _hex(value, f"approved binary {name}") for name, value in binaries.items()}
    if approved != dict(observed):
        raise StageAPreflightError("external tool-identity approval does not match observed binaries")
    return actual_sha256


def prepare_stage_a_preflight(*, repository: Path, arm: str, output_root: Path,
                              epoch0_tree_file: Path, capacity_snapshot_wire: Path,
                              stage_a_envelope_wire: Path, quota_profile: Path,
                              binaries: Mapping[str, Path], verifier_binary: Path,
                              verifier_receipt_output: Path,
                              issuer_id: int, issuer_reference: str,
                              issuer_public_key_hex: str,
                              issuer_public_key_fingerprint: str,
                              approved_capacity_digest: str,
                              epoch0_topology_digest: str,
                              tool_identity_approval_receipt: Path,
                              expected_tool_identity_approval_sha256: str) -> tuple[dict[str, object], dict[str, object]]:
    """Verify Stage A natively and return a no-execution preflight/request pair."""
    if arm not in {"treatment", "sham"}:
        raise StageAPreflightError("arm is not predeclared")
    if output_root.exists() or output_root.is_symlink() or verifier_receipt_output.exists() or verifier_receipt_output.is_symlink():
        raise StageAPreflightError("output paths must be fresh")
    if set(binaries) != REQUIRED_BINARIES or binaries["stage_a_envelope_verifier"].resolve() != verifier_binary.resolve():
        raise StageAPreflightError("binary identity set or verifier binding is incomplete")
    revision = _revision(repository)
    observed = {name: _sha(path, 512 * 1024 * 1024) for name, path in sorted(binaries.items())}
    approval_sha = _approval(tool_identity_approval_receipt, expected_tool_identity_approval_sha256, revision, observed)
    native_arm = "fast_priority_treatment" if arm == "treatment" else "exact_copy_sham"
    command = (str(verifier_binary), "--epoch0-tree-file", str(epoch0_tree_file),
               "--stage-a-envelope-wire", str(stage_a_envelope_wire), "--issuer-id", str(issuer_id),
               "--issuer-reference", issuer_reference, "--issuer-public-key-hex", issuer_public_key_hex,
               "--issuer-public-key-fingerprint", issuer_public_key_fingerprint,
               "--approved-capacity-digest", approved_capacity_digest, "--arm", native_arm,
               "--source-revision", revision, "--output", str(verifier_receipt_output))
    invoked = subprocess.run(command, text=True, capture_output=True, check=False)
    if invoked.returncode != 0:
        raise StageAPreflightError("native Stage-A verifier rejected the supplied envelope")
    verifier_receipt, verifier_receipt_sha = _json(verifier_receipt_output, 16 * 1024, "native verifier receipt")
    expected_inputs = {
        "envelope_wire_sha256": _sha(stage_a_envelope_wire, 32 * 1024),
        "approved_capacity_digest": approved_capacity_digest,
        "issuer_id": issuer_id, "issuer_reference": issuer_reference,
        "issuer_public_key_fingerprint": issuer_public_key_fingerprint,
        "arm": native_arm, "source_revision": revision,
        "epoch0_tree_file_sha256": _sha(epoch0_tree_file, 8 * 1024),
        "epoch0_topology_digest": epoch0_topology_digest,
    }
    if verifier_receipt.get("kind") != VERIFIER_KIND or verifier_receipt.get("verdict") != VERIFIER_VERDICT or any(verifier_receipt.get(k) != v for k, v in expected_inputs.items()):
        raise StageAPreflightError("native verifier receipt does not bind the supplied Stage-A inputs")
    inputs = {"epoch0_tree_file": expected_inputs["epoch0_tree_file_sha256"],
              "capacity_snapshot_wire": _sha(capacity_snapshot_wire, 16 * 1024),
              "stage_a_envelope_wire": expected_inputs["envelope_wire_sha256"],
              "quota_profile": _validated_quota_sha(quota_profile)}
    preflight = {"schema_version": 1, "kind": PREFLIGHT_KIND, "verdict": "PREFLIGHT_OK_NO_EXECUTION",
                 "claim_eligible": False, "figure_eligible": False, "revision": revision,
                 "protocol": {"N": 31, "Q": 21}, "arm": arm, "stage_a_native_arm": native_arm,
                 "output_root": str(output_root.resolve()), "binary_sha256": observed,
                 "input_sha256": inputs, "native_verifier_receipt_sha256": verifier_receipt_sha,
                 "tool_identity_approval_receipt_sha256": approval_sha}
    request = {"schema_version": 1, "kind": REQUEST_KIND,
               "verdict": "EXECUTION_AUTHORIZATION_REQUEST_REQUIRED",
               "claim_eligible": False, "figure_eligible": False,
               "preflight_sha256": hashlib.sha256(canonical_json(preflight)).hexdigest(),
               "revision": revision, "arm": arm, "output_root": str(output_root.resolve()),
               "binary_sha256": observed, "input_sha256": inputs,
               "native_verifier_receipt_sha256": verifier_receipt_sha,
               "tool_identity_approval_receipt_sha256": approval_sha}
    return preflight, request
