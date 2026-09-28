"""No-launch provenance gate for the prospective N31 operator-capacity study."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Mapping


KIND = "kauri-n31-operator-capacity-preflight-request-v1"
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


def _validate_quota_profile(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise PreflightError(f"not a regular file: {path}")
        with path.open("rb") as source:
            payload = source.read(64 * 1024 + 1)
        if len(payload) > 64 * 1024:
            raise PreflightError(f"input exceeds byte limit: {path}")
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


def _sha256(path: Path, maximum_bytes: int) -> str:
    if maximum_bytes <= 0 or path.is_symlink() or not path.is_file():
        raise PreflightError(f"not a regular file: {path}")
    if path.stat().st_size > maximum_bytes:
        raise PreflightError(f"input exceeds byte limit: {path}")
    with path.open("rb") as source:
        payload = source.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise PreflightError(f"input exceeds byte limit: {path}")
    return hashlib.sha256(payload).hexdigest()


def _native_digest(binary: Path, *arguments: str) -> str:
    if binary.is_symlink() or not binary.is_file():
        raise PreflightError(f"native helper unavailable: {binary}")
    try:
        completed = subprocess.run((str(binary), *arguments), text=True,
                                   capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PreflightError(f"native helper unavailable: {binary}") from error
    value = completed.stdout.strip()
    if completed.returncode != 0 or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise PreflightError(f"native helper rejected input: {binary.name}")
    return value


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
    *, repository: Path, snapshot_wire: Path, capacity_digest_binary: Path,
    epoch0_digest_binary: Path, epoch0_arm: str, epoch0_tree_file: Path,
    arm: str, quota_profile: Path, output_root: Path,
    issuer_public_key_fingerprint: str, binaries: Mapping[str, Path],
) -> dict[str, object]:
    """Derive a request only; it never creates an approval or execution root."""
    if arm not in {"treatment", "sham"}:
        raise PreflightError("arm is not predeclared")
    if epoch0_arm != "slow-roots":
        raise PreflightError("operator-capacity study requires the frozen slow-roots baseline")
    if len(issuer_public_key_fingerprint) != 64 or any(c not in "0123456789abcdef" for c in issuer_public_key_fingerprint):
        raise PreflightError("issuer fingerprint is malformed")
    if output_root.exists() or output_root.is_symlink():
        raise PreflightError("output root must be fresh")
    if set(binaries) != {"app", "keygen", "tls_keygen", "capacity_digest", "epoch0_digest"}:
        raise PreflightError("binary identity set is incomplete")
    if binaries["capacity_digest"].resolve() != capacity_digest_binary.resolve() or \
       binaries["epoch0_digest"].resolve() != epoch0_digest_binary.resolve():
        raise PreflightError("invoked helper identity is not bound")
    revision, repository_status = _repository_state(repository)
    if repository_status:
        raise PreflightError("repository is not clean")
    snapshot_sha256 = _sha256(snapshot_wire, 16 * 1024)
    quota_sha256 = _sha256(quota_profile, 64 * 1024)
    quota_assignment_semantic_sha256 = _validate_quota_profile(quota_profile)
    epoch0_tree_file_sha256 = _sha256(epoch0_tree_file, 8 * 1024)
    for helper in (capacity_digest_binary, epoch0_digest_binary):
        if helper.is_symlink() or not helper.is_file():
            raise PreflightError(f"native helper unavailable: {helper}")
    binary_sha256 = {
        name: _sha256(path, 512 * 1024 * 1024)
        for name, path in sorted(binaries.items())
    }
    epoch0_digest = _native_digest(epoch0_digest_binary, epoch0_arm, str(epoch0_tree_file))
    semantic_digest = _native_digest(
        capacity_digest_binary, "--validate-n31", epoch0_digest, str(snapshot_wire)
    )
    if (_sha256(snapshot_wire, 16 * 1024) != snapshot_sha256 or
            _sha256(epoch0_tree_file, 8 * 1024) != epoch0_tree_file_sha256 or
            _sha256(quota_profile, 64 * 1024) != quota_sha256):
        raise PreflightError("input bytes changed during native derivation")
    binary_sha256_after = {
        name: _sha256(path, 512 * 1024 * 1024)
        for name, path in sorted(binaries.items())
    }
    if binary_sha256_after != binary_sha256:
        raise PreflightError("binary bytes changed during native derivation")
    revision_after, repository_status_after = _repository_state(repository)
    if revision_after != revision or repository_status_after:
        raise PreflightError("repository state changed during native derivation")
    return {"schema_version": 1, "kind": KIND, "verdict": "PREFLIGHT_OK_NO_EXECUTION",
            "claim_eligible": False, "figure_eligible": False, "revision": revision,
            "protocol": {"N": 31, "Q": 21}, "arm": arm,
            "snapshot_wire_sha256": snapshot_sha256,
            "snapshot_semantic_digest": semantic_digest,
            "epoch0_tree_digest": epoch0_digest, "epoch0_tree_file_sha256": epoch0_tree_file_sha256, "epoch0_arm": epoch0_arm,
            "quota_profile_sha256": quota_sha256,
            "quota_contract_id": QUOTA_CONTRACT_ID,
            "quota_assignment_semantic_sha256": quota_assignment_semantic_sha256,
            "slow_replica_ids": list(SLOW_REPLICA_IDS),
            "fast_replica_ids": list(FAST_REPLICA_IDS),
            "slow_cpu_quota_percent": 25, "fast_cpu_quota_percent": 100,
            "epoch0_exposed_slow_root_ids": list(SLOW_REPLICA_IDS),
            "issuer_public_key_fingerprint": issuer_public_key_fingerprint,
            "binary_sha256": binary_sha256, "output_root": str(output_root.resolve())}


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
