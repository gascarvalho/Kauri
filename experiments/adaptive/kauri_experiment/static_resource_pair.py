"""Fail-closed identity and preflight gate for the all-live N=31 CPU study.

This module deliberately does not reuse the crash-pair runner: that runner's
fault window and SIGKILL contract would invalidate this all-responsive study.
It prepares the two-arm sham/adaptive invocation, binds the external CPU
contract, and refuses execution until a dedicated all-live backend exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import stat
from typing import Any

from . import cpu_quota
from .profiled_fault_archive import EvidenceSealError, create_evidence_seal


class StaticResourcePairError(RuntimeError):
    """The all-live CPU study cannot be prepared or authorized exactly."""


_PROFILE_KEYS = frozenset(
    {
        "baseline_root_ids",
        "execution",
        "manager_visibility",
        "profile_id",
        "protocol",
        "resource",
        "schema_version",
        "topology",
    }
)
_EXECUTION_KEYS = frozenset(
    {
        "automatic_retries",
        "claim_eligible",
        "figure_eligible",
        "pair_count",
        "replacement_policy",
        "schedule",
    }
)
_RESOURCE_KEYS = frozenset(
    {"factor", "fast_ids", "fast_quota_percent", "slow_ids", "slow_quota_percent"}
)
_PROTOCOL_KEYS = frozenset({"N", "Q", "f", "tree_root_positions"})
_AUTHORIZATION_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "profile_id",
        "profile_sha256",
        "profile_canonical_sha256",
        "cpu_contract_id",
        "cpu_contract_sha256",
        "cpu_contract_semantic_sha256",
        "authorization_stage",
        "output_root",
        "arms",
        "automatic_retries",
        "replacement_policy",
        "claim_eligible",
        "figure_eligible",
        "authorization_nonce",
    }
)
_EXPLICIT_USER_AUTHORITY = "user-confirmation:2026-09-25:static-resource-cpu-study"
_ARM_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "study_id",
        "pair_id",
        "arm",
        "attempt",
        "retry_count",
        "issuer_key_path",
        "epoch0_path",
        "epoch1_path",
        "manager_path",
        "manager_argv_path",
        "activation_path",
        "resource_receipt_path",
        "commit_path",
        "cleanup_path",
    }
)
_BINDING_KEYS = frozenset(
    {
        "revision",
        "build_sha256",
        "workload_sha256",
        "successor_schedule_sha256",
        "host_identity_sha256",
    }
)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
        + b"\n"
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _regular_bytes(path: Path, label: str) -> bytes:
    candidate = Path(path)
    try:
        record = candidate.lstat()
    except OSError as exc:
        raise StaticResourcePairError(f"cannot inspect {label}") from exc
    if stat.S_ISLNK(record.st_mode) or not stat.S_ISREG(record.st_mode):
        raise StaticResourcePairError(f"{label} must be a regular non-symlink file")
    try:
        return candidate.read_bytes()
    except OSError as exc:
        raise StaticResourcePairError(f"cannot read {label}") from exc


def _write_exclusive_regular(path: Path, payload: bytes, label: str) -> None:
    """Create one evidence file without following or replacing a path."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o644)
    except OSError as exc:
        raise StaticResourcePairError(f"cannot create exclusive {label}") from exc
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    except OSError as exc:
        raise StaticResourcePairError(f"cannot write {label}") from exc
    finally:
        os.close(descriptor)


def _sealed_relative_regular(root: Path, raw: object, label: str) -> Path:
    """Reject traversal and all symlink routes before accepting evidence input."""

    if not isinstance(raw, str) or not raw:
        raise StaticResourcePairError(f"{label} path is invalid")
    parts = PurePosixPath(raw).parts
    if PurePosixPath(raw).is_absolute() or not parts or any(part in {".", ".."} for part in parts):
        raise StaticResourcePairError(f"{label} path escapes completed pair root")
    candidate = root.joinpath(*parts)
    try:
        info = candidate.lstat()
        root_real = root.resolve(strict=True)
        candidate_real = candidate.resolve(strict=True)
        candidate_real.relative_to(root_real)
    except (OSError, ValueError) as exc:
        raise StaticResourcePairError(f"{label} path escapes completed pair root") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise StaticResourcePairError(f"{label} must be a regular non-symlink file")
    return candidate


@dataclass(frozen=True, slots=True)
class StaticResourceProfile:
    profile_id: str
    profile_sha256: str
    canonical_sha256: str
    slow_ids: tuple[int, ...]
    fast_ids: tuple[int, ...]
    baseline_root_ids: tuple[int, ...]


def load_profile(path: Path) -> StaticResourceProfile:
    """Load exactly the frozen six-slow, all-live, sham/adaptive profile."""

    payload = _regular_bytes(path, "static-resource profile")
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StaticResourcePairError("static-resource profile is invalid JSON") from exc
    if not isinstance(document, dict) or set(document) != _PROFILE_KEYS:
        raise StaticResourcePairError("static-resource profile schema drifted")
    protocol = document.get("protocol")
    execution = document.get("execution")
    resource = document.get("resource")
    if (
        not isinstance(protocol, dict)
        or set(protocol) != _PROTOCOL_KEYS
        or protocol != {"N": 31, "Q": 21, "f": 10, "tree_root_positions": 21}
        or not isinstance(execution, dict)
        or set(execution) != _EXECUTION_KEYS
        or execution
        != {
            "automatic_retries": 0,
            "claim_eligible": False,
            "figure_eligible": False,
            "pair_count": 1,
            "replacement_policy": "none",
            "schedule": ["sham", "adaptive"],
        }
        or not isinstance(resource, dict)
        or set(resource) != _RESOURCE_KEYS
        or document.get("schema_version") != 1
        or document.get("profile_id") != "n31-static-resource-cpu-sham-v1"
        or document.get("manager_visibility") != "none"
        or not isinstance(document.get("topology"), dict)
    ):
        raise StaticResourcePairError("static-resource profile identity or safety flags drifted")
    slow = resource.get("slow_ids")
    fast = resource.get("fast_ids")
    roots = document.get("baseline_root_ids")
    if (
        resource.get("factor") != "cpu_quota"
        or resource.get("slow_quota_percent") != 25
        or resource.get("fast_quota_percent") != 100
        or slow != list(range(6))
        or fast != list(range(6, 31))
        or roots != list(range(6))
    ):
        raise StaticResourcePairError("static-resource cohort is not the frozen 0..5 / 6..30 split")
    return StaticResourceProfile(
        profile_id=document["profile_id"],
        profile_sha256=_sha256(payload),
        canonical_sha256=_sha256(_canonical(document)),
        slow_ids=tuple(slow),
        fast_ids=tuple(fast),
        baseline_root_ids=tuple(roots),
    )


def load_cpu_contract(path: Path, *, profile_path: Path) -> cpu_quota.CpuQuotaContract:
    """Load a manager-blind external quota contract for every exact member."""

    profile = load_profile(profile_path)
    contract = cpu_quota.load_cpu_quota_contract(
        path, base_profile_path=profile_path, expected_replica_ids=tuple(range(31))
    )
    if (
        contract.base_profile_id != profile.profile_id
        or contract.base_profile_sha256 != profile.profile_sha256
        or contract.base_profile_canonical_sha256 != profile.canonical_sha256
        or contract.manager_visibility != "none"
        or contract.figure_eligible
        or tuple(contract.quota_percent(replica) for replica in range(31))
        != (25,) * 6 + (100,) * 25
    ):
        raise StaticResourcePairError("CPU contract is not the frozen all-live cohort")
    return contract


def build_authorization_request(
    *, profile: StaticResourceProfile, contract: cpu_quota.CpuQuotaContract,
    output_root: Path,
) -> bytes:
    """Build a read-only-preflight approval request, with no host probe.

    A separately approved live-probe request must precede any systemd scope
    probe.  This first-stage request authorizes neither that probe nor launch.
    """

    root = Path(output_root).absolute()
    nonce = _sha256(
        f"static-resource-cpu-preflight:{profile.profile_sha256}:{contract.contract_sha256}:{root}".encode("utf-8")
    )
    return _canonical(
        {
            "schema_version": 1,
            "kind": "kauri-static-resource-cpu-preflight-authorization-v1",
            "profile_id": profile.profile_id,
            "profile_sha256": profile.profile_sha256,
            "profile_canonical_sha256": profile.canonical_sha256,
            "cpu_contract_id": contract.contract_id,
            "cpu_contract_sha256": contract.contract_sha256,
            "cpu_contract_semantic_sha256": cpu_quota.contract_digest(contract),
            "authorization_stage": "read-only-preflight",
            "output_root": str(root),
            "arms": ["sham", "adaptive"],
            "automatic_retries": 0,
            "replacement_policy": "none",
            "claim_eligible": False,
            "figure_eligible": False,
            "authorization_nonce": nonce,
        }
    )


def prepare_preflight(
    *, profile_path: Path, contract_path: Path, output_root: Path,
) -> dict[str, object]:
    """Write an exclusive read-only preflight without a host capability probe."""

    root = Path(output_root).resolve()
    if root.exists():
        raise StaticResourcePairError("allocated result root already exists")
    profile = load_profile(profile_path)
    contract = load_cpu_contract(contract_path, profile_path=profile_path)
    request = build_authorization_request(
        profile=profile, contract=contract, output_root=root
    )
    request_document = json.loads(request)
    preflight_root = root.parent / f".{root.name}-{request_document['authorization_nonce'][:16]}-preflight"
    try:
        preflight_root.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise StaticResourcePairError("cannot allocate exclusive preflight root") from exc
    request_path = preflight_root / "authorization-request.json"
    preflight_path = preflight_root / "preflight.json"
    request_path.write_bytes(request)
    preflight = {
        **request_document,
        "request_sha256": _sha256(request),
        "live_probe_authorization_required": True,
        "execution_authorized": False,
        "launch_permitted": False,
    }
    preflight_path.write_bytes(_canonical(preflight))
    return {**preflight, "preflight_path": str(preflight_path), "authorization_request_path": str(request_path)}


def build_live_probe_request(
    preflight_request: bytes, preflight_receipt: Mapping[str, Any]
) -> bytes:
    """Bind a separate authorization step before any mutating CPU probe.

    This returns a request only.  It does not call ``systemd-run`` or assert
    that an approval-reference string is cryptographic proof of user intent.
    """

    verified = verify_authorization_receipt(preflight_request, preflight_receipt)
    return _canonical(
        {
            "schema_version": 1,
            "kind": "kauri-static-resource-cpu-live-probe-authorization-v1",
            "preflight_request_sha256": _sha256(preflight_request),
            "preflight_approval_reference": verified["approval_reference"],
            "scope": "one-cgroup-v2-systemd-user-scope-probe-no-workload",
            "launch_permitted": False,
        }
    )


def verify_authorization_receipt(request: bytes, receipt: Mapping[str, Any]) -> dict[str, object]:
    """Check exact request binding and a descriptive explicit-user trace.

    ``approval_reference`` is an auditable task reference, not a signature or
    independent proof that a human approved it.
    """

    try:
        document = json.loads(request)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StaticResourcePairError("authorization request is invalid JSON") from exc
    if not isinstance(document, dict) or set(document) != _AUTHORIZATION_KEYS or _canonical(document) != request:
        raise StaticResourcePairError("authorization request schema drifted")
    expected = {**document, "request_sha256": _sha256(request), "approval_reference": None, "approved_utc": None}
    if set(receipt) != set(expected) or any(receipt.get(key) != value for key, value in document.items()):
        raise StaticResourcePairError("authorization receipt is not bound to the request")
    approved_utc = receipt.get("approved_utc")
    try:
        approved_at = datetime.fromisoformat(approved_utc[:-1] + "+00:00") if isinstance(approved_utc, str) and approved_utc.endswith("Z") else None
    except ValueError:
        approved_at = None
    if (
        receipt.get("request_sha256") != expected["request_sha256"]
        or receipt.get("approval_reference") != _EXPLICIT_USER_AUTHORITY
        or approved_at is None
        or approved_at.tzinfo is None
        or approved_at.utcoffset() != timedelta(0)
    ):
        raise StaticResourcePairError("authorization receipt approval evidence is invalid")
    return dict(receipt)


def seal_completed_pair(
    *,
    output_root: Path,
    profile_path: Path,
    contract_path: Path,
    pair_id: str,
    provenance: Mapping[str, object],
    shared_epoch0_projection_sha256: str,
    shared_epoch0_digest: str,
    issuer_public_key_sha256: str,
    backend_raw_evidence_path: str,
) -> dict[str, object]:
    """Seal outputs from a future dedicated all-live backend without interpreting them.

    The backend must have already produced both arm directories and every raw
    source named by their manifests.  This function never starts processes,
    invents evidence, or converts a malformed arm into a retry.  The separate
    validator remains responsible for deciding PASS, FAIL, or INCOMPLETE.
    """

    requested_root = Path(output_root)
    try:
        root_info = requested_root.lstat()
    except OSError as exc:
        raise StaticResourcePairError("completed pair root is absent") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise StaticResourcePairError("completed pair root must be a directory")
    # Only resolve after preserving the caller-visible path identity above.
    root = requested_root.resolve(strict=True)
    if not isinstance(pair_id, str) or not pair_id:
        raise StaticResourcePairError("pair identity is invalid")
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in (
            shared_epoch0_projection_sha256,
            shared_epoch0_digest,
            issuer_public_key_sha256,
        )
    ):
        raise StaticResourcePairError("shared Epoch-0 or issuer digest is invalid")
    _sealed_relative_regular(root, backend_raw_evidence_path, "dedicated backend raw-evidence contract")
    if set(provenance) != _BINDING_KEYS or any(
        not isinstance(provenance.get(key), str) or not provenance[key]
        for key in _BINDING_KEYS
    ):
        raise StaticResourcePairError("pair provenance binding schema drifted")
    profile = load_profile(profile_path)
    contract = load_cpu_contract(contract_path, profile_path=profile_path)
    profile_destination = root / "profile.json"
    contract_destination = root / "cpu-contract.json"
    _write_exclusive_regular(
        profile_destination, _regular_bytes(profile_path, "static-resource profile"), "profile archive"
    )
    _write_exclusive_regular(
        contract_destination, _regular_bytes(contract_path, "CPU-quota contract"), "CPU-contract archive"
    )

    descriptors: dict[str, dict[str, object]] = {}
    referenced_sources = {"profile.json", "cpu-contract.json", backend_raw_evidence_path}
    for arm in ("sham", "adaptive"):
        arm_root = root / arm
        manifest_path = arm_root / "arm-manifest.json"
        try:
            manifest = json.loads(_regular_bytes(manifest_path, f"{arm} arm manifest"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StaticResourcePairError(f"{arm} arm manifest is invalid JSON") from exc
        if (
            not isinstance(manifest, dict)
            or set(manifest) != _ARM_MANIFEST_KEYS
            or manifest.get("schema_version") != 1
            or manifest.get("study_id") != "static-resource-n31-pair-v1"
            or manifest.get("pair_id") != pair_id
            or manifest.get("arm") != arm
            or manifest.get("attempt") != 1
            or manifest.get("retry_count") != 0
        ):
            raise StaticResourcePairError(f"{arm} arm manifest identity drifted")
        for key in (
            "issuer_key_path",
            "epoch0_path",
            "epoch1_path",
            "manager_path",
            "manager_argv_path",
            "activation_path",
            "resource_receipt_path",
            "commit_path",
            "cleanup_path",
        ):
            raw_path = manifest[key]
            _sealed_relative_regular(root, raw_path, f"{arm} {key}")
            referenced_sources.add(str(raw_path))
        try:
            seal = create_evidence_seal(arm_root)
        except EvidenceSealError as exc:
            raise StaticResourcePairError(f"cannot seal {arm} arm evidence") from exc
        descriptors[arm] = {
            "manifest_path": f"{arm}/arm-manifest.json",
            "tree_sha256": seal.tree_sha256,
            "seal_sha256": seal.seal_sha256,
        }

    pair_manifest = root / "pair-manifest.json"
    # These are all regular evidence files at this point, except the final
    # pair seal which is deliberately excluded by the archive primitive.
    paths = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path != root / "evidence-seal.json"
    )
    paths.append("pair-manifest.json")
    bindings = {
        **dict(provenance),
        "profile_path": "profile.json",
        "profile_sha256": profile.profile_sha256,
        "cpu_contract_path": "cpu-contract.json",
        "cpu_contract_sha256": contract.contract_sha256,
    }
    _write_exclusive_regular(
        pair_manifest,
        _canonical(
            {
                "schema_version": 1,
                "study_id": "static-resource-n31-pair-v1",
                "mode": "excluded_shakedown",
                "pair_id": pair_id,
                "execution": {
                    "schedule": ["sham", "adaptive"],
                    "automatic_retries": 0,
                    "replacement_policy": "none",
                    "claim_eligible": False,
                    "figure_eligible": False,
                },
                "protocol": {"N": 31, "f": 10, "Q": 21, "tree_count": 21},
                "bindings": bindings,
                "shared_epoch0_projection_sha256": shared_epoch0_projection_sha256,
                "shared_epoch0_digest": shared_epoch0_digest,
                "issuer_public_key_sha256": issuer_public_key_sha256,
                "backend_raw_evidence_path": backend_raw_evidence_path,
                "arms": descriptors,
                "paths": sorted(paths),
            }
        ),
        "pair manifest",
    )
    try:
        seal = create_evidence_seal(root)
    except EvidenceSealError as exc:
        raise StaticResourcePairError("cannot seal pair evidence") from exc
    sealed_paths = {entry.path for entry in seal.entries}
    if not referenced_sources.issubset(sealed_paths):
        raise StaticResourcePairError("pair seal does not inventory referenced evidence")
    return {
        "pair_id": pair_id,
        "pair_manifest_path": str(pair_manifest),
        "pair_tree_sha256": seal.tree_sha256,
        "pair_seal_sha256": seal.seal_sha256,
        "claim_eligible": False,
        "figure_eligible": False,
    }


def execution_not_implemented() -> None:
    """Make accidental CLI/backend reuse fail before any process can start."""

    raise StaticResourcePairError(
        "all-live static-resource execution backend is not implemented; launch is prohibited"
    )
