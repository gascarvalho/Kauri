"""No-launch provenance gate for the prospective N31 operator-capacity study."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Mapping


KIND = "kauri-n31-operator-capacity-preflight-request-v1"
TOOL_IDENTITY_DOCUMENT_KIND = "kauri-n31-operator-capacity-tool-identity-document-v1"
TOOL_IDENTITY_DOCUMENT_VERDICT = "UNVERIFIED_TOOL_IDENTITY_DOCUMENT"
_NATIVE_ARM_BY_PRELAUNCH_ARM = {
    "treatment": "fast_priority_treatment",
    "sham": "exact_copy_sham",
}
SLOW_REPLICA_IDS = tuple(range(6))
FAST_REPLICA_IDS = tuple(range(6, 31))
QUOTA_CONTRACT_ID = "n31-static-resource-cpu-sham-quota-v1"
_EXPECTED_QUOTA_PROFILE = {
    "assignments": [
        {
            "capacity_class": "slow" if replica < 6 else "fast",
            "cpu_quota_percent": 25 if replica < 6 else 100,
            "replica_id": replica,
        }
        for replica in range(31)
    ],
    "base_profile_id": "n31-static-resource-cpu-sham-v1",
    "base_profile_canonical_sha256":
        "2ed182ed95fe8514c80eb861ed2e86654afaaf6b881a2ede6fc1a03d0565b766",
    "base_profile_sha256":
        "285aa55cb33637009ccd491d74830cd7485bcd83cb993c33c488dbff6fe4bf09",
    "contract_id": QUOTA_CONTRACT_ID,
    "enabled": True,
    "figure_eligible": False,
    "launcher": "systemd-user-scope-cpu-quota-v1",
    "manager_visibility": "none",
    "sampling_interval_ms": 1000,
    "schema_version": 1,
}


class PreflightError(RuntimeError):
    pass


def _canonical_payload(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")


def _reject_duplicate_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PreflightError(f"quota profile has duplicate JSON field: {key}")
        result[key] = value
    return result


def _strict_json_file(path: Path, maximum_bytes: int, *, label: str) -> tuple[dict[str, object], str]:
    """Read a bounded, non-symlink JSON object and retain its exact-byte hash."""
    return _strict_json_bytes(_read_regular(path, maximum_bytes), label=label)


def _strict_json_bytes(payload: bytes, *, label: str) -> tuple[dict[str, object], str]:
    """Parse an already descriptor-bound JSON byte string exactly once."""
    try:
        parsed = json.loads(
            payload.decode("ascii"), object_pairs_hook=_reject_duplicate_fields,
        )
    except PreflightError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightError(f"{label} is not strict ASCII JSON") from error
    if not isinstance(parsed, dict):
        raise PreflightError(f"{label} must be a JSON object")
    return parsed, hashlib.sha256(payload).hexdigest()


def _hex_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise PreflightError(f"{label} is malformed")
    return value


def _validate_quota_profile_bytes(payload: bytes) -> str:
    try:
        parsed = json.loads(payload.decode("ascii"), object_pairs_hook=_reject_duplicate_fields)
    except PreflightError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightError("quota profile is not strict ASCII JSON") from error
    if _canonical_payload(parsed) != _canonical_payload(_EXPECTED_QUOTA_PROFILE):
        raise PreflightError("quota profile differs from the frozen N31 contract")
    return hashlib.sha256(
        _canonical_payload(_EXPECTED_QUOTA_PROFILE["assignments"])
    ).hexdigest()


def _unverified_binary_identity_document(
    path: Path, *, binary_names: set[str],
) -> tuple[dict[str, str], str, str]:
    """Read caller-supplied tool provenance; it is never an authorization."""
    document, document_sha256 = _strict_json_file(
        path, 64 * 1024, label="tool identity document",
    )
    if (document.get("schema_version") != 1 or
            document.get("kind") != TOOL_IDENTITY_DOCUMENT_KIND or
            document.get("verdict") != TOOL_IDENTITY_DOCUMENT_VERDICT):
        raise PreflightError("tool identity document is malformed")
    approval_ref = document.get("approval_ref")
    if not isinstance(approval_ref, str) or not approval_ref or len(approval_ref) > 256:
        raise PreflightError("tool identity document reference is malformed")
    identities = document.get("binary_sha256")
    if not isinstance(identities, dict) or set(identities) != binary_names:
        raise PreflightError("tool identity document set is incomplete")
    return {
        name: _hex_digest(value, label=f"tool identity document digest {name}")
        for name, value in identities.items()
    }, document_sha256, approval_ref


def _sha256(path: Path, maximum_bytes: int) -> str:
    return hashlib.sha256(_read_regular(path, maximum_bytes)).hexdigest()


def _read_regular(path: Path, maximum_bytes: int) -> bytes:
    """Read one bounded regular file through a single no-follow descriptor."""
    if maximum_bytes <= 0:
        raise PreflightError(f"input exceeds byte limit: {path}")
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    except OSError as error:
        raise PreflightError(f"not a regular file: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PreflightError(f"not a regular file: {path}")
        if metadata.st_size < 0 or metadata.st_size > maximum_bytes:
            raise PreflightError(f"input exceeds byte limit: {path}")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise PreflightError(f"input changed during read: {path}")
            chunks.append(chunk); remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise PreflightError(f"input changed during read: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _repository_state(repository: Path) -> tuple[str, str]:
    revision_result = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        text=True,
        capture_output=True,
        check=False,
    )
    revision = revision_result.stdout.strip()
    if (
        revision_result.returncode != 0
        or len(revision) != 40
        or any(c not in "0123456789abcdef" for c in revision)
    ):
        raise PreflightError("repository revision is unavailable")
    status_result = subprocess.run(
        ("git", "-C", str(repository), "status", "--porcelain"),
        text=True,
        capture_output=True,
        check=False,
    )
    if status_result.returncode != 0:
        raise PreflightError("repository status is unavailable")
    return revision, status_result.stdout


def canonical_request(
    *, repository: Path, capacity_snapshot_wire: Path,
    stage_a_envelope_wire: Path, capacity_digest_binary: Path,
    epoch0_digest_binary: Path, epoch0_arm: str, epoch0_tree_file: Path,
    arm: str, quota_profile: Path, output_root: Path,
    issuer_id: int, issuer_reference: str,
    issuer_public_key_fingerprint: str, approved_capacity_digest: str,
    issuer_public_key_hex: str, epoch0_topology_digest: str,
    native_envelope_verifier_binary: Path, native_envelope_receipt_output: Path,
    tool_identity_document: Path | None,
    binaries: Mapping[str, Path],
) -> dict[str, object]:
    """Derive a request only; it never creates an approval or execution root."""
    if arm not in {"treatment", "sham"}:
        raise PreflightError("arm is not predeclared")
    native_arm = _NATIVE_ARM_BY_PRELAUNCH_ARM[arm]
    if epoch0_arm != "slow-roots":
        raise PreflightError("operator-capacity study requires the frozen slow-roots baseline")
    if issuer_id <= 0:
        raise PreflightError("issuer id is malformed")
    if not issuer_reference or len(issuer_reference.encode("ascii", "ignore")) != len(issuer_reference) or len(issuer_reference) > 128:
        raise PreflightError("issuer reference is malformed")
    _hex_digest(issuer_public_key_fingerprint, label="issuer fingerprint")
    if len(issuer_public_key_hex) != 66 or any(c not in "0123456789abcdef" for c in issuer_public_key_hex):
        raise PreflightError("issuer public key is malformed")
    _hex_digest(approved_capacity_digest, label="approved capacity digest")
    _hex_digest(epoch0_topology_digest, label="epoch0 topology digest")
    if output_root.exists() or output_root.is_symlink():
        raise PreflightError("output root must be fresh")
    binary_names = {"adaptation_manager", "keygen", "tls_keygen", "capacity_digest", "epoch0_digest", "stage_a_envelope_verifier"}
    if set(binaries) != binary_names:
        raise PreflightError("binary identity set is incomplete")
    if binaries["capacity_digest"].resolve() != capacity_digest_binary.resolve() or \
       binaries["epoch0_digest"].resolve() != epoch0_digest_binary.resolve():
        raise PreflightError("invoked helper identity is not bound")
    if binaries["stage_a_envelope_verifier"].resolve() != native_envelope_verifier_binary.resolve():
        raise PreflightError("invoked verifier identity is not bound")
    if native_envelope_receipt_output.exists() or native_envelope_receipt_output.is_symlink():
        raise PreflightError("native envelope receipt output must be fresh")
    revision, repository_status = _repository_state(repository)
    if repository_status:
        raise PreflightError("repository is not clean")
    if tool_identity_document is None:
        return {"schema_version": 1, "kind": KIND,
                "verdict": "PENDING_TOOL_IDENTITY_APPROVAL",
                "claim_eligible": False, "figure_eligible": False,
                "revision": revision, "protocol": {"N": 31, "Q": 21},
                "arm": arm, "stage_a_native_arm": native_arm,
                "required_binary_names": sorted(binary_names),
                "tool_identity_authorization_required": True,
                "output_root": str(output_root.resolve())}
    documented_binary_sha256, tool_identity_document_sha256, tool_identity_document_ref = (
        _unverified_binary_identity_document(
            tool_identity_document, binary_names=binary_names,
        )
    )
    observed_binary_sha256 = {
        name: _sha256(path, 512 * 1024 * 1024)
        for name, path in sorted(binaries.items())
    }
    input_sha256 = {
        "capacity_snapshot_wire": _sha256(capacity_snapshot_wire, 16 * 1024),
        "stage_a_envelope_wire": _sha256(stage_a_envelope_wire, 32 * 1024),
        "epoch0_tree_file": _sha256(epoch0_tree_file, 8 * 1024),
        "quota_profile": _sha256(quota_profile, 64 * 1024),
    }
    _validate_quota_profile_bytes(_read_regular(quota_profile, 64 * 1024))
    if _sha256(tool_identity_document, 64 * 1024) != tool_identity_document_sha256:
        raise PreflightError("tool identity document changed during inspection")
    return {"schema_version": 1, "kind": KIND,
            "verdict": TOOL_IDENTITY_DOCUMENT_VERDICT,
            "claim_eligible": False, "figure_eligible": False,
            "revision": revision, "protocol": {"N": 31, "Q": 21},
            "arm": arm, "stage_a_native_arm": native_arm,
            "tool_identity_authorization_required": True,
            "tool_identity_document_sha256": tool_identity_document_sha256,
            "tool_identity_document_ref": tool_identity_document_ref,
            "binary_sha256": observed_binary_sha256,
            "input_sha256": input_sha256,
            "tool_identity_document_matches_observed_binaries": (
                observed_binary_sha256 == documented_binary_sha256),
            "output_root": str(output_root.resolve())}


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
