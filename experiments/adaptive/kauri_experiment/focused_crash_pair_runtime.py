"""Fail-closed runtime for the focused N7/N31 crash-pair experiment.

Authorization, process control, phase ordering, and artifact sealing remain
separate boundaries.  The default backend composes the reviewed low-level
runtime primitives, while every external effect remains injectable for tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import ctypes
from dataclasses import asdict, dataclass, field, is_dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import time
from types import SimpleNamespace
from typing import Any
import uuid

from . import factorial_validation
from .faults import (
    FaultEvidence,
    FaultLifecycle,
    FaultPlan,
    ReplicaGroupSigkill,
    ScenarioContext,
)
from .processes import (
    ProcessRecord,
    ProcessRegistry,
    SigkillBatchError,
    SigkillBatchResult,
    SigkillOutcome,
)
from .profiled_fault_archive import create_evidence_seal
from . import profiled_fault_runtime
from .profiled_fault_evaluation import FrozenProfile, ProfileFault
from .profiled_fault_runtime import spawn_owned_process


_PROFILE_KEYS = {
    "schema_version",
    "profile_id",
    "frozen",
    "execution_class",
    "campaign_member",
    "figure_eligible",
    "protocol",
    "topology",
    "fault",
    "matched_inputs",
    "transitions",
    "timers",
    "measurement",
    "performance",
    "thresholds",
    "ports",
    "campaign",
    "blinding",
}
_AUTHORIZATION_KEYS = (
    "schema_version",
    "mode",
    "pair_count",
    "profile_sha256",
    "topology_proof_sha256",
    "output_root",
    "automatic_retries",
    "replacement_policy",
    "authorization_nonce",
)
_MAX_ARGV_BYTES = 1 << 20
_CTL_KERN = 1
_KERN_PROCARGS2 = 49
_NATIVE_RESPONSIVENESS_POLICY = {
    "schema_version": 1,
    "policy_version": "adaptive-v2-controller-responsiveness-v1",
    "attempt_window": 32,
    "minimum_attempts": 2,
    "minimum_response_rate_ppm": 750_000,
    "maximum_timeout_rate_ppm": 250_000,
    "trailing_timeout_streak": 2,
    "latency_percentile_basis_points": 5_000,
}

_RUNTIME_EVENT_KEYS = {
    "event_schema_version",
    "run_id",
    "source_kind",
    "source_id",
    "source_instance",
    "source_sequence",
    "source_monotonic_ns",
    "event_type",
    "payload",
}


class FocusedCrashPairRuntimeError(RuntimeError):
    """The focused runtime input or observed execution state is invalid."""


def _error(message: str) -> None:
    raise FocusedCrashPairRuntimeError(message)


def _canonical_json(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise FocusedCrashPairRuntimeError("document is not canonical JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _document(value: object, label: str) -> Mapping[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if not isinstance(value, Mapping):
        _error(f"{label} must be an object")
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _error(f"{label} must be an integer >= {minimum}")
    return value


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _error(f"{label} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class FocusedProfile:
    path: Path
    profile_id: str
    profile_sha256: str
    topology_proof_sha256: str
    topology_proof_path: Path
    replica_ids: tuple[int, ...]
    quorum: int
    target_replica_ids: tuple[int, ...]
    issuer_public_key: str | None
    raw: Mapping[str, Any]


@dataclass(slots=True)
class FocusedPairIssuerAllocator:
    """Generate and persist one private issuer identity per matched pair."""

    allocation_root: Path
    pair_count: int
    keygen_binary: Path
    run_command: Callable[..., Any] = subprocess.run

    def allocate_pair_issuers(self) -> dict[str, object]:
        pair_count = _integer(self.pair_count, "issuer pair count", 1)
        root = Path(self.allocation_root).resolve()
        root.mkdir(parents=True, exist_ok=False)
        rows: dict[str, object] = {}
        public_keys: set[str] = set()
        for ordinal in range(1, pair_count + 1):
            pair_id = f"pair-{ordinal:02d}"
            pair_root = root / pair_id
            pair_root.mkdir(mode=0o700)
            result = self.run_command(
                (
                    str(Path(self.keygen_binary).resolve()),
                    "--num",
                    "1",
                    "--algo",
                    "secp256k1",
                ),
                cwd=pair_root,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                _error(f"issuer keygen failed for {pair_id}")
            identities = profiled_fault_runtime._parse_identity_output(
                result.stdout,
                expected_count=1,
                expected_fields=frozenset({"pub", "sec"}),
                label=f"{pair_id} issuer keygen",
            )
            issuer = identities[0]
            public = str(issuer["pub"])
            private = f"{issuer['sec']}\n".encode("ascii")
            if public in public_keys:
                _error("pair issuer keygen produced a duplicate public key")
            public_keys.add(public)
            private_path = pair_root / "issuer.sec"
            public_path = pair_root / "issuer.pub"
            profiled_fault_runtime.write_exclusive(
                private_path, private, mode=0o600
            )
            profiled_fault_runtime.write_exclusive(
                public_path, f"{public}\n".encode("ascii"), mode=0o600
            )
            rows[pair_id] = {
                "private_key_path": str(private_path),
                "private_key_sha256": _sha256(private),
                "public_key_path": str(public_path),
                "public_key": public,
            }
        return {
            "issuer_public_key": rows["pair-01"]["public_key"],
            "pair_issuers": rows,
        }


def _profile_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    identity = json.loads(json.dumps(dict(raw)))
    topology = identity.get("topology")
    if not isinstance(topology, dict):
        _error("profile topology must be an object")
    topology.pop("proof_sha256", None)
    return identity


def _validate_topology_proof(
    proof: Mapping[str, Any],
    *,
    profile: Mapping[str, Any],
    profile_sha256: str,
) -> None:
    protocol = _document(profile.get("protocol"), "profile protocol")
    topology = _document(profile.get("topology"), "profile topology")
    replica_count = _integer(protocol.get("N"), "replica count", 1)
    fanout = _integer(protocol.get("fanout"), "tree fanout", 1)
    targets = tuple(topology.get("reviewed_target_replica_ids", ()))
    if any(type(target) is not int for target in targets):
        _error("reviewed targets must contain integers")
    required = {
        "schema_version",
        "source",
        "profile_id",
        "profile_sha256",
        "epoch_zero_digest",
        "active_tree_id",
        "fanout",
        "root_replica_id",
        "bfs_member_order",
        "members",
        "internal_descendant_sets",
        "target_derivation",
    }
    if set(proof) != required or proof.get("schema_version") != 1:
        _error("topology proof schema drifted")
    if (
        proof.get("source") != "native_epoch_profile_digest"
        or proof.get("profile_id") != profile.get("profile_id")
        or proof.get("profile_sha256") != profile_sha256
        or proof.get("epoch_zero_digest") != topology.get("epoch_zero_digest")
        or proof.get("active_tree_id") != topology.get("active_tree_id")
        or proof.get("fanout") != fanout
    ):
        _error("topology proof identity differs from its profile")
    order = proof.get("bfs_member_order")
    members = proof.get("members")
    descendants = proof.get("internal_descendant_sets")
    derivation = proof.get("target_derivation")
    if (
        not isinstance(order, list)
        or len(order) != replica_count
        or set(order) != set(range(replica_count))
        or not isinstance(members, list)
        or len(members) != replica_count
        or not isinstance(descendants, dict)
        or not isinstance(derivation, dict)
        or proof.get("root_replica_id") != order[0]
    ):
        _error("topology proof membership is incomplete")
    children = {
        index: tuple(
            child
            for child in range(index * fanout + 1, index * fanout + fanout + 1)
            if child < replica_count
        )
        for index in range(replica_count)
    }

    def subtree(index: int) -> tuple[int, ...]:
        return tuple(
            member
            for child in children[index]
            for member in (order[child], *subtree(child))
        )

    depths = [0] * replica_count
    for index in range(1, replica_count):
        depths[index] = depths[(index - 1) // fanout] + 1
    expected_members = [
        {
            "replica_id": replica,
            "bfs_index": index,
            "depth": depths[index],
            "role": (
                "root" if index == 0 else "internal" if children[index] else "leaf"
            ),
        }
        for index, replica in enumerate(order)
    ]
    internal = [index for index, child_ids in children.items() if child_ids]
    expected_descendants = {
        str(order[index]): list(subtree(index)) for index in internal
    }
    nonroot_internal = [index for index in internal if index]
    deepest_depth = max(depths[index] for index in nonroot_internal)
    deepest = [
        order[index]
        for index in nonroot_internal
        if depths[index] == deepest_depth
    ]
    target_descendants = [set(expected_descendants[str(target)]) for target in targets]
    pairwise_disjoint = all(
        left.isdisjoint(right)
        for index, left in enumerate(target_descendants)
        for right in target_descendants[index + 1 :]
    )
    if (
        members != expected_members
        or descendants != expected_descendants
        or derivation
        != {
            "deepest_member_ids": deepest,
            "selected_target_replica_ids": list(targets),
            "pairwise_disjoint": True,
        }
        or not set(targets).issubset(deepest)
        or order[0] in targets
        or not pairwise_disjoint
    ):
        _error("topology proof roles, depths, descendants, or targets drifted")


def load_focused_profile(path: Path) -> FocusedProfile:
    """Load one frozen profile and verify its one-way proof binding."""

    profile_path = Path(path)
    if profile_path.is_symlink() or not profile_path.is_file():
        _error("focused profile must be a regular non-symlink file")
    try:
        raw = json.loads(profile_path.read_bytes())
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise FocusedCrashPairRuntimeError("focused profile is invalid JSON") from exc
    profile = _document(raw, "focused profile")
    if set(profile) != _PROFILE_KEYS or profile.get("schema_version") != 1:
        _error("focused profile schema drifted")
    if profile.get("frozen") is not True:
        _error("focused profile is not frozen")
    profile_id = profile.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id:
        _error("focused profile ID is invalid")
    protocol = _document(profile.get("protocol"), "profile protocol")
    topology = _document(profile.get("topology"), "profile topology")
    replica_count = _integer(protocol.get("N"), "replica count", 1)
    fault_threshold = _integer(protocol.get("f"), "fault threshold")
    quorum = _integer(protocol.get("Q"), "quorum", 1)
    if replica_count != 3 * fault_threshold + 1 or quorum != 2 * fault_threshold + 1:
        _error("profile does not preserve N=3f+1 and Q=2f+1")
    targets = tuple(topology.get("reviewed_target_replica_ids", ()))
    if (
        not targets
        or len(set(targets)) != len(targets)
        or any(type(target) is not int or target not in range(replica_count) for target in targets)
    ):
        _error("profile reviewed targets are invalid")
    fault = _document(profile.get("fault"), "profile fault")
    if fault.get("target_count") != len(targets):
        _error("profile fault cardinality differs from the proven targets")
    measurement = _document(profile.get("measurement"), "profile measurement")
    observer = _integer(
        measurement.get("authoritative_replica_id"),
        "authoritative replica",
    )
    if observer not in range(replica_count) or observer in targets:
        _error("authoritative replica must be a surviving member")
    _digest(topology.get("epoch_zero_digest"), "epoch-zero digest")
    profile_sha256 = _sha256(_canonical_json(_profile_identity(profile)))
    proof_relative = topology.get("proof_path")
    if (
        not isinstance(proof_relative, str)
        or not proof_relative
        or Path(proof_relative).is_absolute()
        or ".." in Path(proof_relative).parts
    ):
        _error("topology proof path is unsafe")
    proof_path = profile_path.parent / proof_relative
    if proof_path.is_symlink() or not proof_path.is_file():
        _error("topology proof is absent or not a regular file")
    proof_bytes = proof_path.read_bytes()
    proof_sha256 = _digest(topology.get("proof_sha256"), "topology proof digest")
    if _sha256(proof_bytes) != proof_sha256:
        _error("topology proof bytes differ from the profile pointer")
    try:
        proof = _document(json.loads(proof_bytes), "topology proof")
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise FocusedCrashPairRuntimeError("topology proof is invalid JSON") from exc
    _validate_topology_proof(proof, profile=profile, profile_sha256=profile_sha256)
    issuer_key = _document(profile.get("matched_inputs"), "matched inputs").get(
        "issuer_public_key"
    )
    if issuer_key is not None and not isinstance(issuer_key, str):
        _error("issuer public key is malformed")
    return FocusedProfile(
        path=profile_path.resolve(),
        profile_id=profile_id,
        profile_sha256=profile_sha256,
        topology_proof_sha256=proof_sha256,
        topology_proof_path=proof_path.resolve(),
        replica_ids=tuple(range(replica_count)),
        quorum=quorum,
        target_replica_ids=targets,
        issuer_public_key=issuer_key,
        raw=dict(profile),
    )


def _authorization_request(
    profile: FocusedProfile,
    *,
    mode: str,
    pair_count: int,
    output_root: Path,
) -> dict[str, object]:
    if mode not in {"smoke", "pair", "campaign"}:
        _error("focused execution mode is unsupported")
    expected_pairs = 5 if mode == "campaign" else 1
    if pair_count != expected_pairs:
        _error("focused execution pair cardinality drifted")
    resolved_output = str(Path(output_root).resolve())
    nonce = _sha256(f"{mode}:{pair_count}:{resolved_output}".encode("utf-8"))
    return {
        "schema_version": 1,
        "mode": mode,
        "pair_count": pair_count,
        "profile_sha256": profile.profile_sha256,
        "topology_proof_sha256": profile.topology_proof_sha256,
        "output_root": resolved_output,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "authorization_nonce": nonce,
    }


@dataclass(slots=True)
class FocusedLivePreflightChecks:
    """Read-only live checks used by the CLI before authorization.

    The class intentionally owns no launch method.  Tests may supply a small
    object with the same seven methods to exercise every failure boundary.
    """

    repository_path: Path
    build_directory: Path
    build_provenance_path: Path
    run_command: Callable[..., Any] = subprocess.run
    issuer_allocator: object | None = None
    _build_record: Mapping[str, object] | None = field(default=None, init=False)
    _binaries: Mapping[str, Path] | None = field(default=None, init=False)

    @classmethod
    def for_profile(
        cls,
        profile: FocusedProfile,
        *,
        allocation_root: Path | None = None,
        pair_count: int = 1,
    ) -> "FocusedLivePreflightChecks":
        repository = Path(__file__).resolve().parents[3]
        build_directory = repository / "build-adaptive"
        return cls(
            repository_path=repository,
            build_directory=build_directory,
            build_provenance_path=(
                build_directory / profiled_fault_runtime.BUILD_PROVENANCE_FILENAME
            ),
            issuer_allocator=(
                None
                if allocation_root is None
                else FocusedPairIssuerAllocator(
                    allocation_root=allocation_root,
                    pair_count=pair_count,
                    keygen_binary=build_directory / "hotstuff-keygen",
                )
            ),
        )

    def repository(self, _profile: object) -> Mapping[str, object]:
        revision = profiled_fault_runtime.verify_repository_state(
            self.repository_path
        )
        return {"revision": revision}

    def build(self, _profile: object) -> Mapping[str, object]:
        binaries = profiled_fault_runtime.exact_binary_paths(
            self.repository_path,
            self.build_directory,
        )
        record = profiled_fault_runtime.verify_exact_build_provenance(
            repository=self.repository_path,
            build_directory=self.build_directory,
            provenance_path=self.build_provenance_path,
            binaries=binaries,
        )
        self._build_record = record
        self._binaries = binaries
        return {
            "revision": record["revision"],
            "build_sha256": _sha256(_canonical_json(record)),
        }

    def binaries(self, _profile: object) -> Mapping[str, object]:
        binaries = self._binaries
        if binaries is None:
            _error("build check must precede binaries check")
        return {
            "verified": True,
            "executables": {
                name: {
                    "path": str(path.resolve()),
                    "sha256": profiled_fault_runtime.sha256_file(path.resolve()),
                }
                for name, path in binaries.items()
            },
        }

    def ports(self, profile: object) -> Mapping[str, object]:
        focused = profile if isinstance(profile, FocusedProfile) else None
        if focused is None:
            _error("ports check requires a focused profile")
        ports = _focused_ports(focused)
        occupied = profiled_fault_runtime.occupied_ports(ports)
        if occupied:
            _error(f"required ports are already in use: {list(occupied)}")
        return {"available": True, "ports": list(ports)}

    def clock(self, _profile: object) -> Mapping[str, object]:
        first = profiled_fault_runtime.monotonic_raw_ns()
        second = profiled_fault_runtime.monotonic_raw_ns()
        if second < first:
            _error("CLOCK_MONOTONIC_RAW regressed")
        return {"monotonic": True, "clock": "CLOCK_MONOTONIC_RAW"}

    def native_topology(self, profile: object) -> Mapping[str, object]:
        focused = profile if isinstance(profile, FocusedProfile) else None
        binaries = self._binaries
        if focused is None or binaries is None:
            _error("native topology check requires verified binaries")
        protocol = _document(focused.raw.get("protocol"), "profile protocol")
        result = self.run_command(
            (
                str(binaries["epoch_profile_digest"]),
                str(len(focused.replica_ids)),
                str(protocol["fanout"]),
                str(protocol["pipeline_stretch"]),
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            _error("native_topology check failed")
        try:
            witness = _document(json.loads(result.stdout), "native topology")
            epoch = _document(witness.get("epoch_zero"), "native epoch zero")
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise FocusedCrashPairRuntimeError(
                "native_topology check returned invalid JSON"
            ) from exc
        expected = _document(focused.raw.get("topology"), "profile topology")
        if epoch.get("epoch_digest") != expected.get("epoch_zero_digest"):
            _error("native_topology epoch-zero digest drifted")
        return {
            "epoch_zero_digest": epoch["epoch_digest"],
            "topology_proof_sha256": focused.topology_proof_sha256,
            "native_witness_sha256": _sha256(_canonical_json(witness)),
        }

    def issuer_public_key(self, profile: object) -> Mapping[str, object]:
        focused = profile if isinstance(profile, FocusedProfile) else None
        if focused is None:
            _error("issuer_public_key check requires a focused profile")
        if self.issuer_allocator is not None:
            allocate = getattr(self.issuer_allocator, "allocate_pair_issuers", None)
            if not callable(allocate):
                _error("pair issuer allocator is unavailable")
            return dict(
                _document(allocate(), "pair issuer allocation")
            )
        configured = focused.issuer_public_key
        if not isinstance(configured, str) or len(configured) != 66:
            _error(
                "issuer_public_key check requires a pair-bound issuer context"
            )
        return {"issuer_public_key": configured}


def _focused_ports(profile: FocusedProfile) -> tuple[int, ...]:
    ports = _document(profile.raw.get("ports"), "profile ports")
    base = _integer(ports.get("base"), "port base", 1)
    stride = _integer(ports.get("stride"), "port stride", 1)
    count = len(profile.replica_ids)
    values = tuple(base + stride * index for index in range(2 * count + 1))
    if len(set(values)) != len(values) or values[-1] > 65_535:
        _error("focused port allocation is invalid")
    return values


def prepare_focused_preflight(
    profile: FocusedProfile,
    *,
    mode: str,
    pair_count: int,
    output_root: Path,
    checks: object | None = None,
) -> dict[str, object]:
    """Materialize a no-launch request that cannot authorize execution."""

    if type(profile) is not FocusedProfile:
        _error("preflight requires a loaded focused profile")
    request = _authorization_request(
        profile, mode=mode, pair_count=pair_count, output_root=output_root
    )
    preflight_root = Path(output_root).resolve().parent / (
        f".{Path(output_root).name}-{request['authorization_nonce'][:16]}-preflight"
    )
    preflight_root.mkdir(parents=True, exist_ok=False)
    if isinstance(checks, type) and issubclass(checks, FocusedLivePreflightChecks):
        checks = checks.for_profile(
            profile,
            allocation_root=preflight_root / "issuer-allocation",
            pair_count=pair_count,
        )
    check_results: dict[str, Mapping[str, object]] = {}
    if checks is not None:
        for name in (
            "repository",
            "build",
            "binaries",
            "ports",
            "clock",
            "native_topology",
            "issuer_public_key",
        ):
            method = getattr(checks, name, None)
            if not callable(method):
                _error(f"{name} check is unavailable")
            try:
                result = method(profile)
            except FocusedCrashPairRuntimeError:
                raise
            except Exception as exc:
                raise FocusedCrashPairRuntimeError(f"{name} check failed") from exc
            check_results[name] = dict(_document(result, f"{name} check result"))
        native = check_results["native_topology"]
        issuer = check_results["issuer_public_key"]
        if (
            native.get("epoch_zero_digest")
            != _document(profile.raw.get("topology"), "profile topology").get(
                "epoch_zero_digest"
            )
            or native.get("topology_proof_sha256")
            != profile.topology_proof_sha256
            or not isinstance(issuer.get("issuer_public_key"), str)
        ):
            _error("preflight execution context is not profile-bound")
    execution_context: dict[str, object] | None = None
    if check_results:
        issuer_result = _document(
            check_results["issuer_public_key"], "issuer allocation"
        )
        execution_context = {
            **{
                name: dict(result)
                for name, result in check_results.items()
                if name != "issuer_public_key"
            },
            "issuer_public_key": check_results["issuer_public_key"][
                "issuer_public_key"
            ],
            "profile_sha256": profile.profile_sha256,
            "topology_proof_sha256": profile.topology_proof_sha256,
        }
        if "pair_issuers" in issuer_result:
            raw_allocations = _document(
                issuer_result.get("pair_issuers"), "pair issuer allocations"
            )
            expected_pairs = {
                f"pair-{ordinal:02d}" for ordinal in range(1, pair_count + 1)
            }
            if set(raw_allocations) != expected_pairs:
                _error("pair issuer allocation cardinality drifted")
            normalized: dict[str, object] = {}
            for pair_id, raw in raw_allocations.items():
                allocation = _document(raw, f"{pair_id} issuer allocation")
                normalized[pair_id] = {
                    key: allocation.get(key)
                    for key in (
                        "private_key_path",
                        "private_key_sha256",
                        "public_key_path",
                        "public_key",
                    )
                }
                if any(value is None for value in normalized[pair_id].values()):
                    _error("pair issuer allocation schema drifted")
            execution_context["pair_issuers"] = normalized
        request = {
            **request,
            "execution_context_sha256": _sha256(
                _canonical_json(execution_context)
            ),
        }
    request_bytes = _canonical_json(request)
    request_path = preflight_root / "authorization-request.json"
    preflight_path = preflight_root / "preflight.json"
    request_path.write_bytes(request_bytes)
    preflight_document = {
        **request,
        "request_sha256": _sha256(request_bytes),
        "execution_authorized": False,
        "launch_permitted": False,
    }
    if execution_context is not None:
        preflight_document["checks"] = check_results
        preflight_document["execution_context"] = execution_context
    preflight_path.write_bytes(_canonical_json(preflight_document))
    return {
        **preflight_document,
        "preflight_path": str(preflight_path),
        "authorization_request_path": str(request_path),
        "topology_proof_path": str(profile.topology_proof_path),
    }


def build_focused_authorization_request(preflight: object) -> bytes:
    document = _document(preflight, "focused preflight")
    request_keys = list(_AUTHORIZATION_KEYS)
    execution_context = document.get("execution_context")
    if document.get("execution_context_sha256") is not None:
        context_digest = _sha256(
            _canonical_json(_document(execution_context, "execution context"))
        )
        if document.get("execution_context_sha256") != context_digest:
            _error("preflight execution context digest drifted")
        request_keys.append("execution_context_sha256")
    request = {key: document.get(key) for key in request_keys}
    if any(
        request[key] is None for key in request_keys
    ):
        _error("preflight does not contain the complete authorization request")
    payload = _canonical_json(request)
    expected_sha = document.get("request_sha256")
    if expected_sha is not None and expected_sha != _sha256(payload):
        base_request = {key: document.get(key) for key in _AUTHORIZATION_KEYS}
        if (
            "execution_context_sha256" not in request
            or expected_sha != _sha256(_canonical_json(base_request))
        ):
            _error("preflight request digest drifted")
    if (
        document.get("execution_authorized") is not False
        or document.get("launch_permitted") is not False
    ):
        _error("preflight must remain non-authorizing")
    return payload


def verify_focused_authorization_receipt(
    request: bytes,
    receipt: Mapping[str, Any],
) -> dict[str, object]:
    """Verify exact approval of one canonical execution request."""

    try:
        request_document = json.loads(request)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise FocusedCrashPairRuntimeError("authorization request is invalid") from exc
    if _canonical_json(request_document) != request:
        _error("authorization request is not exact canonical JSON")
    request_keys = set(request_document)
    if frozenset(request_keys) not in {
        frozenset(_AUTHORIZATION_KEYS),
        frozenset((*_AUTHORIZATION_KEYS, "execution_context_sha256")),
    }:
        _error("authorization request schema drifted")
    expected_keys = {
        *request_keys,
        "request_sha256",
        "approval_reference",
        "approved_utc",
    }
    document = _document(receipt, "authorization receipt")
    if set(document) != expected_keys:
        _error("authorization receipt schema drifted")
    if any(document.get(key) != request_document.get(key) for key in request_keys):
        _error("authorization receipt is not bound to the request")
    if document.get("request_sha256") != _sha256(request):
        _error("authorization receipt request digest drifted")
    if not isinstance(document.get("approval_reference"), str) or not document[
        "approval_reference"
    ]:
        _error("authorization approval reference is absent")
    if not isinstance(document.get("approved_utc"), str) or not document["approved_utc"]:
        _error("authorization approval time is absent")
    return {**dict(document), "execution_authorized": True, "launch_permitted": True}


def reload_pair_issuer_allocations(
    *,
    preflight: Mapping[str, Any],
    authorization: Mapping[str, Any],
) -> dict[str, dict[str, object]]:
    """Reload exact authorized private issuer material for separate execution."""

    request = build_focused_authorization_request(preflight)
    verified = verify_focused_authorization_receipt(request, authorization)
    context = _document(preflight.get("execution_context"), "execution context")
    if verified.get("execution_context_sha256") != _sha256(
        _canonical_json(context)
    ):
        _error("authorization does not bind the issuer execution context")
    allocations = _document(
        context.get("pair_issuers"), "pair issuer allocations"
    )
    pair_count = _integer(verified.get("pair_count"), "authorized pair count", 1)
    expected = {f"pair-{ordinal:02d}" for ordinal in range(1, pair_count + 1)}
    if set(allocations) != expected:
        _error("pair issuer allocation cardinality drifted")
    loaded: dict[str, dict[str, object]] = {}
    public_keys: set[str] = set()
    for pair_id in sorted(expected):
        allocation = _document(
            allocations[pair_id], f"{pair_id} issuer allocation"
        )
        if set(allocation) != {
            "private_key_path",
            "private_key_sha256",
            "public_key_path",
            "public_key",
        }:
            _error("pair issuer allocation schema drifted")
        raw_path = allocation.get("private_key_path")
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            _error("pair issuer private path is not absolute")
        private_path = Path(raw_path)
        if private_path.is_symlink() or not private_path.is_file():
            _error("pair issuer private file is absent or not regular")
        raw_public_path = allocation.get("public_key_path")
        if (
            not isinstance(raw_public_path, str)
            or not Path(raw_public_path).is_absolute()
        ):
            _error("pair issuer public path is not absolute")
        public_path = Path(raw_public_path)
        if (
            public_path.is_symlink()
            or not public_path.is_file()
            or private_path.parent.name != pair_id
            or public_path.parent != private_path.parent
            or public_path.name != "issuer.pub"
            or private_path.name != "issuer.sec"
        ):
            _error("pair issuer allocation escaped its exact pair directory")
        if private_path.stat().st_mode & 0o777 != 0o600:
            _error("pair issuer private file mode is not 0600")
        private_bytes = private_path.read_bytes()
        if _sha256(private_bytes) != _digest(
            allocation.get("private_key_sha256"), "pair issuer private digest"
        ):
            _error("pair issuer private bytes drifted")
        try:
            private_text = private_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise FocusedCrashPairRuntimeError(
                "pair issuer private scalar is not ASCII"
            ) from exc
        if (
            len(private_text) != 65
            or private_text[-1] != "\n"
            or any(character not in "0123456789abcdef" for character in private_text[:-1])
        ):
            _error("pair issuer private scalar encoding drifted")
        scalar = int(private_text[:-1], 16)
        if not 1 <= scalar < factorial_validation._SECP256K1_ORDER:
            _error("pair issuer private scalar is outside secp256k1 range")
        point = factorial_validation._secp256k1_multiply(
            scalar,
            (
                factorial_validation._SECP256K1_GX,
                factorial_validation._SECP256K1_GY,
            ),
        )
        if point is None:
            _error("pair issuer public key derivation failed")
        public_key = f"{2 + point[1] % 2:02x}{point[0]:064x}"
        try:
            public_bytes = public_path.read_bytes()
        except OSError as exc:
            raise FocusedCrashPairRuntimeError(
                "pair issuer public file cannot be read"
            ) from exc
        if (
            public_bytes != f"{public_key}\n".encode("ascii")
            or allocation.get("public_key") != public_key
            or public_key in public_keys
        ):
            _error("pair issuer public key binding or uniqueness drifted")
        public_keys.add(public_key)
        arm_identity = {
            "public_key": public_key,
            "private_key": private_text[:-1],
        }
        loaded[pair_id] = {
            "private_key_path": str(private_path),
            "private_key_sha256": allocation["private_key_sha256"],
            "public_key_path": str(public_path),
            "public_key": public_key,
            "control": dict(arm_identity),
            "adaptive": dict(arm_identity),
        }
    if context.get("issuer_public_key") != loaded["pair-01"]["public_key"]:
        _error("execution context primary issuer key drifted")
    return loaded


def derive_focused_child_authorization(
    *,
    parent_authorization: Mapping[str, Any],
    pair_id: str,
    slot_id: str,
    arm: str,
    pair_seed: int,
) -> dict[str, object]:
    """Derive one immutable child without rewriting the approved parent."""

    parent = dict(_document(parent_authorization, "parent authorization"))
    parent_sha = _digest(parent.get("request_sha256"), "parent request digest")
    if (
        not isinstance(pair_id, str)
        or not pair_id
        or not isinstance(slot_id, str)
        or not slot_id
        or arm not in {"control", "adaptive"}
    ):
        _error("child authorization identity is invalid")
    seed = _integer(pair_seed, "child pair seed")
    return {
        "schema_version": 1,
        "parent_authorization": parent,
        "parent_request_sha256": parent_sha,
        "derivation": {
            "pair_id": pair_id,
            "slot_id": slot_id,
            "arm": arm,
            "pair_seed": seed,
        },
    }


def verify_focused_child_authorization(
    *,
    parent_authorization: Mapping[str, Any],
    child_authorization: Mapping[str, Any],
    pair_id: str,
    slot_id: str,
    arm: str,
    pair_seed: int,
) -> dict[str, object]:
    """Re-derive one child and reject relabelling or parent replacement."""

    expected = derive_focused_child_authorization(
        parent_authorization=parent_authorization,
        pair_id=pair_id,
        slot_id=slot_id,
        arm=arm,
        pair_seed=pair_seed,
    )
    supplied = dict(_document(child_authorization, "child authorization"))
    if supplied != expected:
        _error("child authorization differs from its exact parent derivation")
    return expected


def _outcome_document(outcome: SigkillOutcome) -> dict[str, object]:
    return asdict(outcome)


def _batch_result_document(result: SigkillBatchResult) -> dict[str, object]:
    if result.outcome is not None:
        return {**_outcome_document(result.outcome), "status": result.status}
    return {
        "status": result.status,
        "error": result.error,
        "name": result.name,
        "replica_id": result.replica_id,
        "pid": result.pid,
        "pgid": result.pgid,
        "signal_number": result.signal_number,
        "requested_monotonic_ns": result.requested_monotonic_ns,
    }


def _execute_atomic_fault_batch(
    registry: ProcessRegistry,
    plan: FaultPlan,
    lifecycle: FaultLifecycle,
    timeout_s: float,
) -> tuple[SigkillOutcome, ...]:
    """Execute one precommitted batch and terminalize every reserved action."""

    actions = tuple(plan.actions)
    if not actions or any(type(action) is not ReplicaGroupSigkill for action in actions):
        _error("focused fault plan must contain only replica SIGKILL actions")
    for action in actions:
        lifecycle.start(action.fault_id)
    requests = tuple((action.fault_id, action.replica_id) for action in actions)
    try:
        outcomes = registry.sigkill_replica_groups(requests, timeout_s=timeout_s)
    except SigkillBatchError as exc:
        by_fault = {result.fault_id: result for result in exc.results}
        for action in actions:
            result = by_fault.get(action.fault_id)
            if result is None:
                lifecycle.terminal(action.fault_id, "failed")
            else:
                lifecycle.terminal(
                    action.fault_id,
                    result.status,
                    _batch_result_document(result),
                )
        raise
    except BaseException:
        lifecycle.finalize(started_status="failed")
        raise
    for action, outcome in zip(actions, outcomes, strict=True):
        if outcome.fault_id != action.fault_id or outcome.replica_id != action.replica_id:
            lifecycle.finalize(started_status="failed")
            _error("atomic SIGKILL result order or identity drifted")
        lifecycle.terminal(action.fault_id, "succeeded", _outcome_document(outcome))
    return tuple(outcomes)


@dataclass(frozen=True, slots=True)
class ArmRuntimeHooks:
    wait_for_stable_phase: Callable[[str], Mapping[str, object]]
    inject_atomic_fault_batch: Callable[[], Mapping[str, object]]
    wait_for_nonresponse: Callable[[], Mapping[str, object]]
    issue_epoch_request: Callable[[int], Mapping[str, object]]
    wait_for_epoch_commands: Callable[[int], Mapping[str, object]]
    wait_for_epoch_activations: Callable[[int], Mapping[str, object]]
    wait_for_common_commit: Callable[[int], Mapping[str, object]]
    rebuild_ranking: Callable[[], Mapping[str, object]]
    unexpected_exit_ids: Callable[[], Sequence[int]]


def _timestamp(value: Mapping[str, object], label: str) -> int:
    return _integer(value.get("source_monotonic_ns"), f"{label} timestamp")


def _decoded_bundle(
    snapshot: Mapping[str, object], issuer_public_key: str, label: str
) -> tuple[bytes, Any]:
    wire = snapshot.get("native_bundle")
    if not isinstance(wire, bytes) or not wire:
        _error(f"{label} native bundle is absent")
    try:
        decoded = factorial_validation.decode_epoch_change_bundle(
            wire, issuer_public_key=issuer_public_key
        )
    except factorial_validation.FactorialValidationError as exc:
        raise FocusedCrashPairRuntimeError(f"{label} native bundle is invalid") from exc
    if snapshot.get("decoded") != asdict(decoded):
        _error(f"{label} decoded identity drifted")
    return wire, decoded


def _transition_barrier(
    snapshot: Mapping[str, object],
    *,
    survivors: tuple[int, ...],
    decoded: Any,
    wire: bytes,
    label: str,
    after_ns: int,
    activation: bool,
) -> int:
    if tuple(snapshot.get("survivor_replica_ids", ())) != survivors:
        _error(f"{label} does not cover exactly all survivors")
    if snapshot.get("witness_count") != len(survivors):
        _error(f"{label} witness count drifted")
    identity = {
        "successor_epoch_number": decoded.epoch_number,
        "successor_epoch_digest": decoded.epoch_digest,
        "bundle_sha256": _sha256(wire),
    }
    if any(snapshot.get(key) != expected for key, expected in identity.items()):
        _error(f"{label} epoch identity drifted")
    timestamp = _timestamp(snapshot, label)
    if timestamp <= after_ns:
        _error(f"{label} is causally early")
    if activation:
        _integer(snapshot.get("activation_height"), f"{label} activation height")
    else:
        height = _integer(snapshot.get("command_block_height"), f"{label} command height")
        activation_height = _integer(
            snapshot.get("activation_height"), f"{label} activation height"
        )
        if activation_height != height + decoded.command.activation_delay_blocks:
            _error(f"{label} activation height is not command-derived")
    return timestamp


def _common_commit(
    snapshot: Mapping[str, object],
    *,
    epoch_number: int,
    epoch_digest: str,
    survivors: tuple[int, ...],
    quorum: int,
    after_ns: int,
) -> int:
    observed = tuple(snapshot.get("observed_replica_ids", ()))
    if (
        snapshot.get("epoch_number") != epoch_number
        or snapshot.get("epoch_digest") != epoch_digest
        or snapshot.get("authoritative_commit_count") != 1
        or len(observed) < quorum
        or len(set(observed)) != len(observed)
        or not set(observed).issubset(survivors)
    ):
        _error("common commit authority or quorum drifted")
    timestamp = _timestamp(snapshot, "common commit")
    if timestamp <= after_ns:
        _error("common commit is causally early")
    return timestamp


def epoch1_structurally_identical(left: Any, right: Any) -> bool:
    """Compare the native placement projection while excluding arm provenance."""

    fields = (
        "epoch_number",
        "previous_epoch_digest",
        "membership_digest",
        "generation_seed",
        "policy_version",
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields) and tuple(
        asdict(tree) for tree in left.trees
    ) == tuple(asdict(tree) for tree in right.trees)


def _drive_arm_state_machine(
    profile: object,
    arm: str,
    pair_id: str,
    hooks: ArmRuntimeHooks,
) -> dict[str, object]:
    """Drive one control/adaptive arm through the frozen causal graph."""

    if arm not in {"C", "A"} or not isinstance(pair_id, str) or not pair_id:
        _error("arm or pair identity is invalid")
    replicas = tuple(getattr(profile, "replica_ids", ()))
    targets = tuple(getattr(profile, "target_replica_ids", ()))
    quorum = _integer(getattr(profile, "quorum", None), "profile quorum", 1)
    issuer = getattr(profile, "issuer_public_key", None)
    if not replicas or not isinstance(issuer, str):
        _error("arm profile membership or issuer identity is absent")
    survivors = tuple(replica for replica in replicas if replica not in targets)
    if len(survivors) < quorum or hooks.unexpected_exit_ids():
        _error("arm has an unexplained exit or insufficient survivors")

    baseline = hooks.wait_for_stable_phase("baseline")
    if baseline.get("stable") is not True:
        _error("baseline did not stabilize")
    baseline_ns = _timestamp(baseline, "baseline")
    fault = hooks.inject_atomic_fault_batch()
    fault_ns = _timestamp(fault, "fault")
    if (
        fault_ns <= baseline_ns
        or tuple(fault.get("confirmed_target_ids", ())) != targets
        or tuple(fault.get("survivor_replica_ids", ())) != survivors
    ):
        _error("fault receipt or survivor projection drifted")
    nonresponse = hooks.wait_for_nonresponse()
    nonresponse_ns = _timestamp(nonresponse, "nonresponse")
    if nonresponse_ns <= fault_ns or tuple(nonresponse.get("detected_target_ids", ())) != targets:
        _error("runtime nonresponse does not match the confirmed fault set")

    epoch1_snapshot = hooks.issue_epoch_request(1)
    epoch1_ns = _timestamp(epoch1_snapshot, "epoch 1 request")
    if epoch1_ns <= nonresponse_ns:
        _error("epoch 1 request is causally early")
    epoch1_wire, epoch1 = _decoded_bundle(epoch1_snapshot, issuer, "epoch 1")
    if epoch1.epoch_number != 1:
        _error("epoch 1 bundle has the wrong epoch number")
    command1 = hooks.wait_for_epoch_commands(1)
    command1_ns = _transition_barrier(
        command1,
        survivors=survivors,
        decoded=epoch1,
        wire=epoch1_wire,
        label="epoch 1 command",
        after_ns=epoch1_ns,
        activation=False,
    )
    activation1 = hooks.wait_for_epoch_activations(1)
    activation1_ns = _transition_barrier(
        activation1,
        survivors=survivors,
        decoded=epoch1,
        wire=epoch1_wire,
        label="epoch 1 activation",
        after_ns=command1_ns,
        activation=True,
    )
    if activation1.get("activation_height") != command1.get("activation_height"):
        _error("epoch 1 activation does not match its command")
    commit1 = hooks.wait_for_common_commit(1)
    commit1_ns = _common_commit(
        commit1,
        epoch_number=1,
        epoch_digest=epoch1.epoch_digest,
        survivors=survivors,
        quorum=quorum,
        after_ns=activation1_ns,
    )
    containment = hooks.wait_for_stable_phase("containment")
    containment_ns = _timestamp(containment, "containment")
    if (
        containment.get("stable") is not True
        or containment.get("epoch_number") != 1
        or containment_ns <= commit1_ns
    ):
        _error("containment phase did not stabilize in epoch 1")

    epoch2 = None
    epoch2_wire: bytes | None = None
    if arm == "A":
        ranking = hooks.rebuild_ranking()
        ranking_ns = _timestamp(ranking, "ranking")
        ranked_ids = tuple(ranking.get("ranked_ids", ()))
        selected_root_ids = tuple(ranking.get("selected_root_ids", ()))
        if (
            ranking_ns <= max(commit1_ns, containment_ns)
            or ranking.get("predecessor_epoch_digest") != epoch1.epoch_digest
            or ranking.get("fresh_after_common_commit") is not True
            or set(ranked_ids) != set(survivors)
            or len(ranked_ids) != len(survivors)
            or not selected_root_ids
            or ranked_ids[: len(selected_root_ids)] != selected_root_ids
        ):
            _error("adaptive ranking is stale or membership-incomplete")
        epoch2_snapshot = hooks.issue_epoch_request(2)
        epoch2_ns = _timestamp(epoch2_snapshot, "epoch 2 request")
        if epoch2_ns <= ranking_ns:
            _error("epoch 2 request precedes its ranking")
        epoch2_wire, epoch2 = _decoded_bundle(epoch2_snapshot, issuer, "epoch 2")
        if epoch2.epoch_number != 2 or epoch2.previous_epoch_digest != epoch1.epoch_digest:
            _error("epoch 2 is not the exact successor of epoch 1")
        if selected_root_ids != tuple(tree.members[0] for tree in epoch2.trees):
            _error("epoch 2 roots differ from the frozen ranking")
        command2 = hooks.wait_for_epoch_commands(2)
        command2_ns = _transition_barrier(
            command2,
            survivors=survivors,
            decoded=epoch2,
            wire=epoch2_wire,
            label="epoch 2 command",
            after_ns=ranking_ns,
            activation=False,
        )
        activation2 = hooks.wait_for_epoch_activations(2)
        activation2_ns = _transition_barrier(
            activation2,
            survivors=survivors,
            decoded=epoch2,
            wire=epoch2_wire,
            label="epoch 2 activation",
            after_ns=command2_ns,
            activation=True,
        )
        if activation2.get("activation_height") != command2.get("activation_height"):
            _error("epoch 2 activation does not match its command")
        commit2 = hooks.wait_for_common_commit(2)
        final_commit_ns = _common_commit(
            commit2,
            epoch_number=2,
            epoch_digest=epoch2.epoch_digest,
            survivors=survivors,
            quorum=quorum,
            after_ns=activation2_ns,
        )
    else:
        final_commit_ns = containment_ns

    late = hooks.wait_for_stable_phase("late")
    if (
        late.get("stable") is not True
        or _timestamp(late, "late") <= final_commit_ns
        or late.get("held_epoch_number") != (2 if arm == "A" else 1)
        or late.get("epoch2_present") is not (arm == "A")
        or hooks.unexpected_exit_ids()
    ):
        _error("late phase violates the arm contract")
    return {
        "schema_version": 1,
        "pair_id": pair_id,
        "arm": arm,
        "quorum": quorum,
        "survivor_replica_ids": list(survivors),
        "epoch1_native_validated": True,
        "epoch1_epoch_digest": epoch1.epoch_digest,
        "epoch1_bundle_sha256": _sha256(epoch1_wire),
        "epoch2_present": epoch2 is not None,
        "epoch2_predecessor_digest": (
            None if epoch2 is None else epoch2.previous_epoch_digest
        ),
        "epoch2_epoch_digest": None if epoch2 is None else epoch2.epoch_digest,
        "epoch2_bundle_sha256": None if epoch2_wire is None else _sha256(epoch2_wire),
    }


class FocusedRawEvidenceSource:
    """Incrementally reconstruct state-machine snapshots from raw artifacts.

    The source never consumes runner outcomes or derived phase documents.  Each
    poll reparses the append-only native streams, validates their provenance,
    and builds the requested barrier from bundles, events, and fault evidence.
    """

    def __init__(
        self,
        run_directory: Path,
        *,
        poll_interval_s: float = 0.05,
        timeout_s: float = 60.0,
        process_records: Sequence[object] = (),
    ) -> None:
        self._root = Path(run_directory)
        self._poll_interval_s = max(0.0, float(poll_interval_s))
        self._timeout_s = float(timeout_s)
        self._process_records = tuple(process_records)
        if self._timeout_s <= 0:
            _error("raw evidence polling timeout must be positive")
        profile_path = self._root / "profile.json"
        loaded = load_focused_profile(profile_path)
        issuer_path = self._root / "raw" / "issuer-public-key.txt"
        if issuer_path.is_symlink() or not issuer_path.is_file():
            _error("raw issuer public key is absent")
        issuer = issuer_path.read_text(encoding="ascii").strip()
        if len(issuer) not in {66, 130}:
            _error("raw issuer public key encoding is malformed")
        try:
            bytes.fromhex(issuer)
        except ValueError as exc:
            raise FocusedCrashPairRuntimeError(
                "raw issuer public key encoding is malformed"
            ) from exc
        self._profile = FocusedProfile(
            path=loaded.path,
            profile_id=loaded.profile_id,
            profile_sha256=loaded.profile_sha256,
            topology_proof_sha256=loaded.topology_proof_sha256,
            topology_proof_path=loaded.topology_proof_path,
            replica_ids=loaded.replica_ids,
            quorum=loaded.quorum,
            target_replica_ids=loaded.target_replica_ids,
            issuer_public_key=issuer,
            raw=loaded.raw,
        )
        manifest = self._read_json(self._root / "manifest.json", "manifest")
        pair_id = manifest.get("pair_id")
        if not isinstance(pair_id, str) or not pair_id:
            _error("raw manifest pair identity is absent")
        self._pair_id = pair_id
        adaptive_output = (
            self._root
            / "transitions"
            / "e1-to-e2-optimization"
            / "successor.bundle"
        )
        self._arm = (
            "A"
            if adaptive_output.parent.is_dir()
            or (self._root / "raw" / "epoch2.bundle").is_file()
            else "C"
        )

    @staticmethod
    def _read_json(path: Path, label: str) -> Mapping[str, Any]:
        if path.is_symlink() or not path.is_file():
            _error(f"raw {label} is absent")
        try:
            value = json.loads(path.read_bytes())
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise FocusedCrashPairRuntimeError(f"raw {label} is invalid JSON") from exc
        return _document(value, f"raw {label}")

    def state_machine_profile(self) -> FocusedProfile:
        return self._profile

    def arm(self) -> str:
        return self._arm

    def pair_id(self) -> str:
        return self._pair_id

    def _events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        raw_root = self._root / "raw"
        aggregate_names = (
            "replica-events.jsonl",
            "adaptive-manager-events.jsonl",
            "client-events.jsonl",
        )
        aggregate_paths = tuple(raw_root / name for name in aggregate_names)
        paths = (
            list(aggregate_paths)
            if all(path.exists() for path in aggregate_paths[:2])
            else [
                path
                for path in sorted(raw_root.glob("*.jsonl"))
                if path.name != "fault-orchestrator.jsonl"
            ]
        )
        for path in paths:
            try:
                stream = profiled_fault_runtime.read_jsonl(path, allow_partial=True)
            except profiled_fault_runtime.ProfiledFaultRuntimeError as exc:
                raise FocusedCrashPairRuntimeError(
                    "raw structured event stream is malformed"
                ) from exc
            for value in stream:
                event = _document(value, "raw structured event")
                if (
                    set(event) != _RUNTIME_EVENT_KEYS
                    or event.get("event_schema_version") != 1
                    or event.get("source_kind")
                    not in {"replica", "adaptation_manager", "client"}
                    or not isinstance(event.get("run_id"), str)
                    or not isinstance(event.get("source_id"), str)
                    or not isinstance(event.get("source_instance"), str)
                    or not isinstance(event.get("event_type"), str)
                    or not isinstance(event.get("payload"), Mapping)
                ):
                    _error("raw structured event envelope schema drifted")
                _integer(event.get("source_sequence"), "raw source sequence", 1)
                _integer(event.get("source_monotonic_ns"), "raw source timestamp")
                events.append(dict(event))
        if not events:
            return []
        if len({event["run_id"] for event in events}) != 1:
            _error("raw event streams span multiple runs")
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        source_ids: set[tuple[str, str]] = set()
        for event in events:
            key = (
                str(event["source_kind"]),
                str(event["source_id"]),
                str(event["source_instance"]),
            )
            grouped.setdefault(key, []).append(event)
        for (kind, source_id, _instance), source_events in grouped.items():
            if (kind, source_id) in source_ids:
                _error("one raw source ID spans multiple instances")
            source_ids.add((kind, source_id))
            sequences = [int(event["source_sequence"]) for event in source_events]
            timestamps = [int(event["source_monotonic_ns"]) for event in source_events]
            if sequences != list(range(1, len(source_events) + 1)):
                _error("raw source sequence is not contiguous")
            if timestamps != sorted(timestamps):
                _error("raw source monotonic time regressed")
        return events

    def _bundle(self, epoch: int) -> tuple[bytes, Any]:
        path = self._root / "raw" / f"epoch{epoch}.bundle"
        if not path.exists():
            artifact = (
                "e0-to-e1-containment"
                if epoch == 1
                else "e1-to-e2-optimization"
            )
            path = self._root / "transitions" / artifact / "successor.bundle"
        if path.is_symlink() or not path.is_file():
            _error(f"raw Epoch {epoch} bundle is absent")
        wire = path.read_bytes()
        try:
            decoded = factorial_validation.decode_epoch_change_bundle(
                wire, issuer_public_key=str(self._profile.issuer_public_key)
            )
        except factorial_validation.FactorialValidationError as exc:
            raise FocusedCrashPairRuntimeError(
                f"raw Epoch {epoch} bundle is invalid"
            ) from exc
        if decoded.epoch_number != epoch:
            _error(f"raw Epoch {epoch} bundle identity drifted")
        return wire, decoded

    def _fault_receipt(self) -> Mapping[str, Any]:
        receipt = self._read_json(
            self._root / "raw" / "fault-receipt.json", "fault receipt"
        )
        outcomes = receipt.get("sigkill_outcomes")
        if not isinstance(outcomes, Sequence) or isinstance(outcomes, (str, bytes)):
            _error("raw fault receipt outcomes are malformed")
        return receipt

    def atomic_fault_outcome(self) -> dict[str, object]:
        receipt = self._fault_receipt()
        outcomes = [
            _document(value, "raw SIGKILL outcome")
            for value in receipt["sigkill_outcomes"]
        ]
        targets = self._profile.target_replica_ids
        confirmed = tuple(
            _integer(outcome.get("replica_id"), "raw fault target")
            for outcome in outcomes
        )
        if (
            confirmed != targets
            or len(outcomes) != len(targets)
            or any(
                outcome.get("signal_number") != 9
                or outcome.get("returncode") != -9
                for outcome in outcomes
            )
        ):
            _error("raw atomic fault outcome drifted")
        confirmations = [
            _integer(outcome.get("confirmed_monotonic_ns"), "fault confirmation")
            for outcome in outcomes
        ]
        survivors = tuple(
            replica
            for replica in self._profile.replica_ids
            if replica not in targets
        )
        return {
            "schema_version": 1,
            "atomic": True,
            "confirmed_target_ids": list(targets),
            "survivor_replica_ids": list(survivors),
            "source_monotonic_ns": max(confirmations),
            "sigkill_outcomes": [dict(outcome) for outcome in outcomes],
        }

    def _ranking(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        predecessor_epoch: int | None = None,
    ) -> dict[str, object]:
        manager = [
            event
            for event in events
            if event["source_kind"] == "adaptation_manager"
            and event["source_id"] == "adaptive-manager"
        ]
        all_audits = [
            event
            for event in manager
            if event["event_type"] == "adaptive_v2_evidence_snapshot"
        ]
        if predecessor_epoch is None:
            predecessor_epoch = (
                0
                if any(
                    _document(event["payload"], "raw ranking audit").get(
                        "predecessor_epoch_number"
                    )
                    == 0
                    for event in all_audits
                )
                else 1
            )
        audits = [
            event
            for event in all_audits
            if _document(event["payload"], "raw ranking audit").get(
                "predecessor_epoch_number"
            )
            == predecessor_epoch
        ]
        if len(audits) != 1:
            _error("raw ranking lacks one native snapshot audit")
        audit = _document(audits[0]["payload"], "raw ranking audit")
        predecessor = _integer(
            audit.get("predecessor_epoch_number"), "ranking predecessor epoch"
        )
        epoch1_wire, epoch1 = self._bundle(1)
        del epoch1_wire
        expected_digest = (
            _document(self._profile.raw["topology"], "profile topology")[
                "epoch_zero_digest"
            ]
            if predecessor == 0
            else epoch1.epoch_digest
        )
        if predecessor not in {0, 1} or audit.get(
            "predecessor_epoch_digest"
        ) != expected_digest:
            _error("raw ranking audit predecessor drifted")
        try:
            replay = factorial_validation.replay_native_adaptation_snapshot(
                manager,
                membership_replica_ids=self._profile.replica_ids,
                predecessor_epoch_number=predecessor,
                predecessor_epoch_digest=str(expected_digest),
                baseline_evidence_cutoff=_integer(
                    audit.get("baseline_cutoff"), "ranking baseline cutoff"
                ),
                current_evidence_cutoff=_integer(
                    audit.get("current_cutoff"), "ranking current cutoff", 1
                ),
                policy=_NATIVE_RESPONSIVENESS_POLICY,
                seed=_integer(epoch1.generation_seed, "ranking seed"),
                suffix_only=predecessor == 1,
            )
        except factorial_validation.FactorialValidationError as exc:
            raise FocusedCrashPairRuntimeError(
                "raw native ranking replay rejected"
            ) from exc
        ranking = [
            _document(row, "raw native ranking row")
            for row in replay.get("ranking", ())
        ]
        ranked = [
            _integer(row.get("replica_id"), "ranked replica")
            for row in ranking
            if row.get("eligible") is True
        ]
        targets = tuple(sorted(set(self._profile.replica_ids) - set(ranked)))
        if targets != self._profile.target_replica_ids:
            _error("raw ranking does not identify the exact nonresponses")
        accepted = [
            event
            for event in manager
            if event["event_type"] == "evidence.observation_accepted"
            and _document(
                _document(
                    _document(event["payload"], "accepted evidence").get(
                        "observation"
                    ),
                    "accepted observation",
                ).get("configuration"),
                "observation configuration",
            ).get("epoch_number")
            == predecessor
            and _integer(
                _document(event["payload"], "accepted evidence").get(
                    "ingestion_sequence"
                ),
                "evidence ingestion sequence",
                1,
            )
            <= _integer(audit.get("current_cutoff"), "ranking current cutoff", 1)
        ]
        if not accepted:
            _error("raw ranking has no accepted evidence in its exact window")
        return {
            "detected_target_ids": list(targets),
            "ranked_ids": ranked,
            "selected_root_ids": ranked[: self._profile.quorum],
            "predecessor_epoch_digest": epoch1.epoch_digest,
            "fresh_after_common_commit": predecessor == 1,
            "source_monotonic_ns": max(
                _integer(event["source_monotonic_ns"], "accepted evidence time")
                for event in accepted
            ),
            "audit_source_monotonic_ns": _integer(
                audits[0].get("source_monotonic_ns"), "ranking audit timestamp"
            ),
        }

    def _transition(
        self,
        events: Sequence[Mapping[str, Any]],
        epoch: int,
        *,
        activation: bool,
    ) -> dict[str, object]:
        wire, decoded = self._bundle(epoch)
        event_type = "epoch.activated" if activation else "epoch.command_committed"
        number_key = "epoch_number" if activation else "successor_epoch_number"
        selected = [
            event
            for event in events
            if event["event_type"] == event_type
            and _document(event["payload"], "transition payload").get(number_key)
            == epoch
        ]
        survivors = tuple(
            replica
            for replica in self._profile.replica_ids
            if replica not in self._profile.target_replica_ids
        )
        expected_sources = {f"replica-{replica}" for replica in survivors}
        payloads = {_canonical_json(event["payload"]) for event in selected}
        if (
            len(selected) != len(survivors)
            or {event["source_id"] for event in selected} != expected_sources
            or len(payloads) != 1
        ):
            _error(f"raw Epoch {epoch} transition lacks every survivor")
        payload = _document(selected[0]["payload"], "transition payload")
        snapshot = {
            "survivor_replica_ids": list(survivors),
            "witness_count": len(survivors),
            "successor_epoch_number": epoch,
            "successor_epoch_digest": decoded.epoch_digest,
            "bundle_sha256": _sha256(wire),
            "activation_height": _integer(
                payload.get("activation_height"), "transition activation height", 1
            ),
            "source_monotonic_ns": max(
                _integer(event["source_monotonic_ns"], "transition timestamp")
                for event in selected
            ),
        }
        if not activation:
            snapshot["command_block_height"] = _integer(
                payload.get("command_block_height"), "transition command height", 1
            )
        return snapshot

    def _common_commit(
        self, events: Sequence[Mapping[str, Any]], epoch: int
    ) -> dict[str, object]:
        _wire, decoded = self._bundle(epoch)
        commits = [
            event
            for event in events
            if event["event_type"] == "block.committed"
            and _document(event["payload"], "commit payload")
            .get("decision_proof", {})
            .get("epoch_number")
            == epoch
        ]
        observers = [
            event
            for event in events
            if event["event_type"] == "block.commit_observed"
        ]
        survivors = set(self._profile.replica_ids) - set(
            self._profile.target_replica_ids
        )
        matches: list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = []
        for commit in commits:
            payload = _document(commit["payload"], "authoritative commit")
            identity = {
                key: payload.get(key)
                for key in (
                    "block_height",
                    "block_hash",
                    "parent_hash",
                    "transaction_count",
                    "commit_batch_index",
                )
            }
            witnessed = [
                event
                for event in observers
                if dict(_document(event["payload"], "common commit observation"))
                == identity
                and event["source_kind"] == "replica"
                and str(event["source_id"]).startswith("replica-")
                and int(str(event["source_id"]).removeprefix("replica-"))
                in survivors
            ]
            witness_ids = {
                int(str(event["source_id"]).removeprefix("replica-"))
                for event in witnessed
            }
            if len(witness_ids) >= self._profile.quorum:
                matches.append((commit, witnessed))
        if not matches:
            _error(f"raw Epoch {epoch} common commit is absent or ambiguous")
        latest_height = max(
            _integer(
                _document(commit["payload"], "authoritative commit").get(
                    "block_height"
                ),
                "commit height",
                1,
            )
            for commit, _witnessed in matches
        )
        latest = [
            match
            for match in matches
            if _document(match[0]["payload"], "authoritative commit").get(
                "block_height"
            )
            == latest_height
        ]
        if len(latest) != 1:
            _error(f"raw Epoch {epoch} common commit is absent or ambiguous")
        commit, witnessed = latest[0]
        payload = _document(commit["payload"], "authoritative commit")
        witness_ids = sorted(
            {
                int(str(event["source_id"]).removeprefix("replica-"))
                for event in witnessed
            }
        )
        return {
            "epoch_number": epoch,
            "epoch_digest": decoded.epoch_digest,
            "authoritative_commit_count": 1,
            "observed_replica_ids": witness_ids,
            "block_height": _integer(payload.get("block_height"), "commit height", 1),
            "block_hash": _digest(payload.get("block_hash"), "commit hash"),
            "parent_hash": payload.get("parent_hash"),
            "transaction_count": _integer(
                payload.get("transaction_count"), "commit transactions", 1
            ),
            "commit_batch_index": _integer(
                payload.get("commit_batch_index"), "commit batch index"
            ),
            "source_monotonic_ns": max(
                _integer(event["source_monotonic_ns"], "commit witness timestamp")
                for event in witnessed
            ),
        }

    def unexpected_exit_ids(self) -> tuple[int, ...]:
        events = self._events()
        unexpected: set[int] = set()
        for record in self._process_records:
            replica = getattr(record, "replica_id", None)
            process = getattr(record, "process", None)
            if type(replica) is not int or process is None:
                continue
            returncode = process.poll()
            if returncode is None:
                continue
            if replica < 0 or replica not in self._profile.target_replica_ids:
                unexpected.add(replica)
                continue
            receipt_path = self._root / "raw" / "fault-receipt.json"
            exempt = False
            if receipt_path.is_file():
                receipt = self._fault_receipt()
                outcomes = [
                    _document(item, "fault outcome")
                    for item in receipt["sigkill_outcomes"]
                    if _document(item, "fault outcome").get("replica_id")
                    == replica
                ]
                receipt_records = [
                    _document(item, "fault process record")
                    for item in receipt.get("process_records", ())
                    if _document(item, "fault process record").get("replica_id")
                    == replica
                ]
                if len(outcomes) == 1 and len(receipt_records) == 1:
                    outcome = outcomes[0]
                    receipt_record = receipt_records[0]
                    process_pid = getattr(process, "pid", None)
                    record_pid = getattr(record, "pid", process_pid)
                    record_pgid = getattr(record, "pgid", receipt_record.get("pgid"))
                    requested = _integer(
                        outcome.get("requested_monotonic_ns"), "fault request"
                    )
                    confirmed = _integer(
                        outcome.get("confirmed_monotonic_ns"),
                        "fault confirmation",
                    )
                    exempt = (
                        returncode == -9
                        and outcome.get("signal_number") == 9
                        and outcome.get("returncode") == returncode
                        and outcome.get("pid") == record_pid == process_pid
                        and outcome.get("pgid") == record_pgid
                        and receipt_record.get("pid") == record_pid
                        and receipt_record.get("pgid") == record_pgid
                        and receipt_record.get("name") == outcome.get("name")
                        and requested < confirmed
                    )
            if not exempt:
                unexpected.add(replica)
        for event in events:
            if event["event_type"] != "process.exited":
                continue
            source_id = str(event["source_id"])
            if source_id.startswith("replica-"):
                replica = int(source_id.removeprefix("replica-"))
                receipt_path = self._root / "raw" / "fault-receipt.json"
                exempt = False
                if replica in self._profile.target_replica_ids and receipt_path.is_file():
                    receipt = self._fault_receipt()
                    matching = [
                        _document(item, "fault outcome")
                        for item in receipt["sigkill_outcomes"]
                        if _document(item, "fault outcome").get("replica_id")
                        == replica
                    ]
                    exempt = (
                        len(matching) == 1
                        and matching[0].get("signal_number") == 9
                        and matching[0].get("returncode") == -9
                        and int(event["source_monotonic_ns"])
                        >= _integer(
                            matching[0].get("confirmed_monotonic_ns"),
                            "fault confirmation",
                        )
                    )
                if not exempt:
                    unexpected.add(replica)
        return tuple(sorted(unexpected))

    def _reject_post_fault_target_events(
        self, events: Sequence[Mapping[str, Any]]
    ) -> None:
        if not (self._root / "raw" / "fault-receipt.json").is_file():
            return
        receipt = self._fault_receipt()
        confirmations = {
            _integer(row.get("replica_id"), "fault replica"):
            _integer(row.get("confirmed_monotonic_ns"), "fault confirmation")
            for row in (
                _document(item, "fault outcome")
                for item in receipt["sigkill_outcomes"]
            )
        }
        for event in events:
            source_id = str(event["source_id"])
            if event["source_kind"] != "replica" or not source_id.startswith(
                "replica-"
            ):
                continue
            replica = int(source_id.removeprefix("replica-"))
            if replica in confirmations and int(event["source_monotonic_ns"]) > confirmations[replica]:
                _error("crashed replica emitted raw evidence after confirmed SIGKILL")

    def poll(self, name: str) -> Mapping[str, object] | None:
        events = self._events()
        self._reject_post_fault_target_events(events)
        unexpected = self.unexpected_exit_ids()
        if unexpected:
            _error("raw process health contains an unexpected survivor exit")
        expected_lifecycle = {
            **{
                f"replica-{replica}": "replica"
                for replica in self._profile.replica_ids
            },
            "adaptive-manager": "adaptation_manager",
        }
        lifecycle = [
            event
            for event in events
            if event["event_type"] in {"process.started", "process.ready"}
        ]
        if lifecycle:
            allowed_lifecycle = {**expected_lifecycle, "client-0": "client"}
            for event in lifecycle:
                if allowed_lifecycle.get(str(event["source_id"])) != event[
                    "source_kind"
                ]:
                    _error("raw readiness source identity or kind drifted")
        ready_sources = {
            str(event["source_id"])
            for event in lifecycle
            if event["event_type"] == "process.ready"
        }
        started_sources = {
            str(event["source_id"])
            for event in lifecycle
            if event["event_type"] == "process.started"
        }
        readiness_complete = (
            set(expected_lifecycle).issubset(ready_sources)
            and set(expected_lifecycle).issubset(started_sources)
            and ready_sources == started_sources
        )
        if ready_sources and not readiness_complete:
            _error("raw readiness barrier does not cover every native emitter")
        if name == "readiness":
            return {"ready": True} if readiness_complete else None
        if not readiness_complete and name in {
            "baseline",
            "nonresponse",
            "containment",
            "late",
        }:
            return None
        if name == "baseline":
            commits = [
                event
                for event in events
                if event["event_type"] == "block.committed"
                and _document(event["payload"], "commit payload")
                .get("decision_proof", {})
                .get("epoch_number")
                == 0
            ]
            stable_duration_ns = (
                _integer(
                    _document(self._profile.raw["timers"], "profile timers").get(
                        "stable_phase_seconds"
                    ),
                    "stable phase seconds",
                    1,
                )
                * 1_000_000_000
            )
            if not commits:
                return None
            first_commit_ns = min(
                int(event["source_monotonic_ns"]) for event in commits
            )
            last_commit_ns = max(
                int(event["source_monotonic_ns"]) for event in commits
            )
            if (
                last_commit_ns - first_commit_ns < stable_duration_ns
            ):
                return None
            return {
                "stable": True,
                "authoritative_commit_count": len(commits),
                "source_monotonic_ns": last_commit_ns,
            }
        if name == "fault":
            return self.atomic_fault_outcome()
        if name == "nonresponse":
            fault = self.atomic_fault_outcome()
            ranking = self._ranking(events, predecessor_epoch=0)
            timeout_events = [
                event
                for event in events
                if event["event_type"] == "evidence.observation_accepted"
                and _document(
                    _document(event["payload"], "accepted evidence").get(
                        "observation"
                    ),
                    "accepted observation",
                ).get("outcome")
                == "timeout"
                and _document(
                    _document(
                        _document(event["payload"], "accepted evidence").get(
                            "observation"
                        ),
                        "accepted observation",
                    ).get("configuration"),
                    "observation configuration",
                ).get("epoch_number")
                == 0
            ]
            if not timeout_events:
                return None
            targets = set(ranking["detected_target_ids"])
            target_timeouts = [
                event
                for event in timeout_events
                if _document(
                    _document(event["payload"], "accepted evidence").get(
                        "observation"
                    ),
                    "accepted observation",
                ).get("observed_replica_id")
                in targets
            ]
            observed_timeout_targets = {
                _document(
                    _document(event["payload"], "accepted evidence").get(
                        "observation"
                    ),
                    "accepted observation",
                ).get("observed_replica_id")
                for event in target_timeouts
            }
            if observed_timeout_targets != targets:
                return None
            timestamp = max(
                int(event["source_monotonic_ns"]) for event in target_timeouts
            )
            return {
                "detected_target_ids": ranking["detected_target_ids"],
                "source_monotonic_ns": timestamp,
            }
        if name == "epoch1":
            epoch1_wire, epoch1 = self._bundle(1)
            request = self._ranking(events, predecessor_epoch=0)
            return {
                "native_bundle": epoch1_wire,
                "decoded": asdict(epoch1),
                "source_monotonic_ns": request["audit_source_monotonic_ns"],
            }
        if name == "commands1":
            return self._transition(events, 1, activation=False)
        if name == "activations1":
            return self._transition(events, 1, activation=True)
        if name == "commit1":
            return self._common_commit(events, 1)
        if name == "containment":
            commit = self._common_commit(events, 1)
            stable_duration_ns = (
                _integer(
                    _document(self._profile.raw["timers"], "profile timers").get(
                        "stable_phase_seconds"
                    ),
                    "stable phase seconds",
                    1,
                )
                * 1_000_000_000
            )
            epoch1_commits = [
                event
                for event in events
                if event["event_type"] == "block.committed"
                and _document(event["payload"], "commit payload")
                .get("decision_proof", {})
                .get("epoch_number")
                == 1
            ]
            if (
                not epoch1_commits
                or max(
                    int(event["source_monotonic_ns"])
                    for event in epoch1_commits
                )
                - min(
                    int(event["source_monotonic_ns"])
                    for event in epoch1_commits
                )
                < stable_duration_ns
            ):
                return None
            boundary = max(
                int(event["source_monotonic_ns"]) for event in epoch1_commits
            )
            return {
                "stable": True,
                "epoch_number": 1,
                "source_monotonic_ns": boundary,
            }
        if name == "ranking":
            if self._arm != "A":
                return None
            return self._ranking(events, predecessor_epoch=1)
        if name == "epoch2":
            if self._arm != "A":
                return None
            wire, decoded = self._bundle(2)
            request = self._ranking(events, predecessor_epoch=1)
            return {
                "native_bundle": wire,
                "decoded": asdict(decoded),
                "source_monotonic_ns": request["audit_source_monotonic_ns"],
            }
        if name == "commands2":
            return self._transition(events, 2, activation=False)
        if name == "activations2":
            return self._transition(events, 2, activation=True)
        if name == "commit2":
            try:
                return self._common_commit(events, 2)
            except FocusedCrashPairRuntimeError:
                return None
        if name == "late":
            try:
                final = self._common_commit(events, 2 if self._arm == "A" else 1)
            except FocusedCrashPairRuntimeError:
                return None
            stable_duration_ns = (
                _integer(
                    _document(self._profile.raw["timers"], "profile timers").get(
                        "stable_phase_seconds"
                    ),
                    "stable phase seconds",
                    1,
                )
                * 1_000_000_000
            )
            final_epoch = 2 if self._arm == "A" else 1
            final_commits = [
                event
                for event in events
                if event["event_type"] == "block.committed"
                and _document(event["payload"], "commit payload")
                .get("decision_proof", {})
                .get("epoch_number")
                == final_epoch
            ]
            if (
                not final_commits
                or max(
                    int(event["source_monotonic_ns"])
                    for event in final_commits
                )
                - min(
                    int(event["source_monotonic_ns"])
                    for event in final_commits
                )
                < stable_duration_ns
            ):
                return None
            return {
                "stable": True,
                "held_epoch_number": 2 if self._arm == "A" else 1,
                "epoch2_present": self._arm == "A",
                "source_monotonic_ns": max(
                    int(event["source_monotonic_ns"]) for event in final_commits
                ),
            }
        _error(f"unsupported raw evidence snapshot {name}")

    def _wait(self, name: str) -> Mapping[str, object]:
        deadline = time.monotonic() + self._timeout_s
        while True:
            snapshot = self.poll(name)
            if snapshot is not None:
                return snapshot
            if time.monotonic() >= deadline:
                _error(f"timed out waiting for raw {name} evidence")
            if self._poll_interval_s:
                time.sleep(self._poll_interval_s)

    def arm_runtime_hooks(
        self,
        *,
        inject_atomic_fault_batch: Callable[[], Mapping[str, object]],
    ) -> ArmRuntimeHooks:
        return ArmRuntimeHooks(
            wait_for_stable_phase=lambda phase: self._wait(phase),
            inject_atomic_fault_batch=inject_atomic_fault_batch,
            wait_for_nonresponse=lambda: self._wait("nonresponse"),
            issue_epoch_request=lambda epoch: self._wait(f"epoch{epoch}"),
            wait_for_epoch_commands=lambda epoch: self._wait(f"commands{epoch}"),
            wait_for_epoch_activations=lambda epoch: self._wait(f"activations{epoch}"),
            wait_for_common_commit=lambda epoch: self._wait(f"commit{epoch}"),
            rebuild_ranking=lambda: self._wait("ranking"),
            unexpected_exit_ids=self.unexpected_exit_ids,
        )


def _valid_pid(pid: object) -> int:
    if type(pid) is not int or pid <= 1:
        _error("process PID must be an integer greater than one")
    return pid


def _parse_nul_cmdline(payload: bytes, label: str) -> tuple[str, ...]:
    if not payload or len(payload) > _MAX_ARGV_BYTES or not payload.endswith(b"\0"):
        _error(f"{label} is empty, oversized, or not NUL terminated")
    raw = payload[:-1].split(b"\0")
    if not raw or not raw[0]:
        _error(f"{label} has an empty argv[0]")
    return tuple(os.fsdecode(item) for item in raw)


def _linux_argv(pid: int, proc_root: Path) -> tuple[str, ...]:
    path = proc_root / str(pid) / "cmdline"
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, _MAX_ARGV_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_ARGV_BYTES:
                _error("process argv exceeds the fixed byte bound")
        return _parse_nul_cmdline(b"".join(chunks), f"process {pid} cmdline")
    except FocusedCrashPairRuntimeError:
        raise
    except OSError as exc:
        raise FocusedCrashPairRuntimeError(f"cannot read process {pid} argv") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _darwin_procargs2(pid: int) -> bytes:
    libc = ctypes.CDLL(None, use_errno=True)
    sysctl = libc.sysctl
    sysctl.argtypes = (
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    )
    sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    for _ in range(3):
        required = ctypes.c_size_t()
        if sysctl(mib, 3, None, ctypes.byref(required), None, 0) != 0:
            error = ctypes.get_errno() or errno.EIO
            raise FocusedCrashPairRuntimeError(
                f"cannot size process {pid} argv: {os.strerror(error)}"
            )
        if required.value <= ctypes.sizeof(ctypes.c_int) or required.value > _MAX_ARGV_BYTES:
            _error("Darwin process argv payload size is invalid")
        buffer = ctypes.create_string_buffer(required.value)
        actual = ctypes.c_size_t(required.value)
        if sysctl(mib, 3, buffer, ctypes.byref(actual), None, 0) == 0:
            return bytes(buffer.raw[: actual.value])
        if (ctypes.get_errno() or errno.EIO) != errno.ENOMEM:
            _error(f"cannot read process {pid} argv")
    _error("process argv changed during repeated reads")


def _parse_darwin_procargs2(payload: bytes) -> tuple[str, ...]:
    int_size = ctypes.sizeof(ctypes.c_int)
    if len(payload) <= int_size or len(payload) > _MAX_ARGV_BYTES:
        _error("Darwin process argv payload size is invalid")
    argc = struct.unpack_from("=i", payload, 0)[0]
    if argc < 1 or argc > 65_536:
        _error("Darwin process argc is outside fixed bounds")
    cursor = payload.find(b"\0", int_size)
    if cursor <= int_size:
        _error("Darwin process executable path is malformed")
    cursor += 1
    while cursor < len(payload) and payload[cursor] == 0:
        cursor += 1
    arguments: list[str] = []
    for index in range(argc):
        end = payload.find(b"\0", cursor)
        if end < 0:
            _error(f"Darwin process argv[{index}] is not NUL terminated")
        raw = payload[cursor:end]
        if index == 0 and not raw:
            _error("Darwin process argv[0] is empty")
        arguments.append(os.fsdecode(raw))
        cursor = end + 1
    return tuple(arguments)


def _capture_process_argv(
    record: ProcessRecord | object,
    proc_root: Path | None = None,
    *,
    platform_system: str | None = None,
    darwin_reader: Callable[[int], Sequence[str] | bytes] | None = None,
) -> tuple[str, ...]:
    """Capture exact NUL-delimited argv for one owned manager process."""

    pid = _valid_pid(getattr(record, "pid", None))
    if getattr(record, "pgid", None) != pid:
        _error("manager does not own its exact process group")
    if getattr(record, "name", None) not in {"manager", "adaptive-manager"}:
        _error("argv capture accepts only a manager process")
    if getattr(record, "replica_id", None) != -1:
        _error("manager process has an invalid replica identity")
    system = platform.system() if platform_system is None else platform_system
    if system == "Linux":
        return _linux_argv(pid, Path("/proc") if proc_root is None else Path(proc_root))
    if system == "Darwin":
        observed = (_darwin_procargs2 if darwin_reader is None else darwin_reader)(pid)
        if isinstance(observed, bytes):
            return _parse_darwin_procargs2(observed)
        if isinstance(observed, Sequence) and not isinstance(observed, (str, bytearray)):
            if not observed or any(not isinstance(item, str) for item in observed):
                _error("Darwin argv reader returned malformed arguments")
            return tuple(observed)
        _error("Darwin argv reader returned an unsupported payload")
    _error(f"exact argv capture is unsupported on {system}")


def _validate_manager_launch_boundary(
    requested_argv: Sequence[str],
    observed_argv: Sequence[str],
    *,
    manager_input: Mapping[str, Any],
    forbidden_values: Sequence[str],
) -> dict[str, object]:
    """Bind the parser-valid requested boundary to the exact observed argv."""

    requested = tuple(requested_argv)
    observed = tuple(observed_argv)
    if not requested or requested != observed:
        _error("requested and observed manager argv differ")
    serialized = _canonical_json(
        {"argv": list(observed), "input": dict(manager_input)}
    ).decode("ascii")
    if any(value and value in serialized for value in forbidden_values):
        _error("manager boundary contains orchestrator-private truth")
    if dict(manager_input) != {
        "input_source": "normalized_manager_launch_boundary_v1",
        "requested_argv": list(requested),
        "observed_argv": list(observed),
        "stdin": "closed",
    }:
        _error("manager input is not the exact observed launch boundary")
    allowed = {
        "--listen",
        "--tls-privkey",
        "--tls-cert",
        "--issuer-id",
        "--issuer-private-key",
        "--activation-delay-blocks",
        "--convergence-deadline-seconds",
        "--tree-fanout",
        "--pipeline-stretch",
        "--shape-candidate-fanouts",
        "--shape-deterministic-seed",
        "--responsiveness-policy-version",
        "--required-nonresponsive",
        "--responsiveness-attempt-window",
        "--responsiveness-minimum-attempts",
        "--responsiveness-minimum-response-rate-ppm",
        "--responsiveness-maximum-timeout-rate-ppm",
        "--responsiveness-trailing-timeout-streak",
        "--responsiveness-latency-percentile-basis-points",
        "--transition-request",
        "--bundle-output",
        "--structured-event-run-id",
        "--structured-event-source-instance",
        "--structured-event-output",
        "--replica",
    }
    if len(requested) % 2 == 0:
        _error("manager argv must contain an executable and option/value pairs")
    pairs = tuple(zip(requested[1::2], requested[2::2], strict=True))
    if any(option not in allowed for option, _value in pairs):
        _error("manager argv contains an unreviewed option")
    transitions = [value for option, value in pairs if option == "--transition-request"]
    outputs = [value for option, value in pairs if option == "--bundle-output"]
    replicas = [value for option, value in pairs if option == "--replica"]
    if (
        len(transitions) not in {1, 2}
        or len(outputs) != len(transitions)
        or not replicas
        or len(set(replicas)) != len(replicas)
        or any(not Path(value).is_absolute() for value in outputs)
    ):
        _error("manager transition or replica cardinality is invalid")
    for ordinal, raw in enumerate(transitions, start=1):
        try:
            transition = _document(json.loads(raw), "manager transition")
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise FocusedCrashPairRuntimeError(
                "manager transition request is invalid JSON"
            ) from exc
        if (
            transition.get("predecessor_epoch_number") != ordinal - 1
            or transition.get("successor_epoch_number") != ordinal
            or transition.get("bundle_path") is None
            or _canonical_json(transition).decode("ascii").rstrip("\n") != raw
        ):
            _error("manager transition request is not canonical or contiguous")
    return {
        "blinded": True,
        "manager_cli_args_fault_truth_free": True,
        "manager_input_fault_truth_free": True,
        "requested_observed_argv_identical": True,
        "input_source": "normalized_manager_launch_boundary_v1",
    }


def _seal_arm_artifacts(
    run_directory: Path,
    required_files: Sequence[str],
    runner_outcome: Mapping[str, object],
    cleanup: Mapping[str, object],
    *,
    create_seal: Callable[[Path], object] = create_evidence_seal,
) -> dict[str, object]:
    """Finalize mutable outputs, verify the layout, then write only the seal."""

    root = Path(run_directory)
    required = tuple(required_files)
    if len(required) != len(set(required)) or "evidence-seal.json" in required:
        _error("required arm artifact list is invalid")
    (root / "runner-outcome.json").write_bytes(_canonical_json(runner_outcome))
    (root / "cleanup.json").write_bytes(_canonical_json(cleanup))
    actual = {
        str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
    }
    if actual != set(required):
        _error("arm artifact layout is incomplete or contains extra files")
    metadata = create_seal(root)
    result = asdict(metadata) if is_dataclass(metadata) else metadata
    if not isinstance(result, Mapping):
        _error("evidence sealer returned an invalid result")
    return dict(result)


def _profiled_adapter(profile: FocusedProfile, pair_seed: int) -> FrozenProfile:
    """Project the frozen focused contract onto the reviewed launch utilities."""

    raw = profile.raw
    protocol = _document(raw.get("protocol"), "profile protocol")
    topology = _document(raw.get("topology"), "profile topology")
    timers = _document(raw.get("timers"), "profile timers")
    measurement = _document(raw.get("measurement"), "profile measurement")
    ports = _document(raw.get("ports"), "profile ports")
    base = _integer(ports.get("base"), "profile port base", 1)
    count = len(profile.replica_ids)
    target = profile.target_replica_ids[0]
    return FrozenProfile(
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        replica_ids=profile.replica_ids,
        fault_threshold=(count - 1) // 3,
        quorum=profile.quorum,
        fanout=_integer(protocol.get("fanout"), "profile fanout", 1),
        pipeline_depth=_integer(
            protocol.get("pipeline_stretch"), "profile pipeline stretch", 1
        ),
        epoch0_roots=profile.replica_ids[: profile.quorum],
        epoch0_members_breadth_first=profile.replica_ids,
        authoritative_observer=_integer(
            measurement.get("authoritative_replica_id"),
            "authoritative observer",
        ),
        snapshot_seed=pair_seed,
        peer_base=base,
        client_base=base + count,
        manager_port=base + 2 * count,
        fault=ProfileFault(
            fault_id=f"crash-replica-{target}",
            kind="replica_group_sigkill",
            replica_id=target,
            tree_id=_integer(topology.get("active_tree_id"), "active tree"),
        ),
        crash_subtree=profile.target_replica_ids,
        attempt_count=1,
        retry_failed_attempts=False,
        require_successor_activation=True,
        block_size=_integer(
            protocol.get("transactions_per_block"), "transactions per block", 1
        ),
        tree_switch_period_blocks=count,
        bucket_width_s=_integer(
            measurement.get("bucket_width_seconds"), "bucket width", 1
        ),
        baseline_bucket_count=1,
        post_bucket_count=3,
        minimum_positive_postfault_buckets=3,
        minimum_mean_throughput_retention=0.0,
        aggregation_timeout_s=1.0,
        # Both frozen topologies have a two-edge deepest tree.  The native
        # fallback horizon is twice the level-aware maximum: 2 * (2 + 1) * 1s
        # = 6s.  Suspicion therefore needs strictly more than 6s once the
        # 1s activation grace is included.
        leader_progress_timeout_s=6.0,
        leader_activation_grace_s=1.0,
        activation_delay_blocks=5,
        maximum_stall_s=float(timers.get("containment_deadline_seconds", 60)),
        startup_timeout_s=float(timers.get("containment_deadline_seconds", 60)),
        hard_timeout_s=float(timers.get("optimization_deadline_seconds", 90)) + 60.0,
        crash_confirm_timeout_s=5.0,
    )


def _generate_arm_identities(
    profile: FrozenProfile,
    *,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    config_directory: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Generate arm-local BLS/TLS identities without generating an issuer."""

    commands = profiled_fault_runtime.identity_generation_commands(
        profile,
        keygen_binary=keygen_binary,
        tls_keygen_binary=tls_keygen_binary,
    )
    outputs: dict[str, str] = {}
    for label in ("bls", "tls"):
        result = subprocess.run(
            commands[label],
            cwd=config_directory,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            _error(f"{label} arm identity generation failed")
        outputs[label] = result.stdout
        profiled_fault_runtime.write_exclusive(
            config_directory / f"{label}-identities.txt",
            result.stdout.encode(),
        )
    count = len(profile.replica_ids)
    return (
        profiled_fault_runtime._parse_identity_output(
            outputs["bls"],
            expected_count=count,
            expected_fields=frozenset({"pub", "sec"}),
            label="BLS keygen",
        ),
        profiled_fault_runtime._parse_identity_output(
            outputs["tls"],
            expected_count=count + 1,
            expected_fields=frozenset({"crt", "sec", "cid"}),
            label="TLS keygen",
        ),
    )


def _focused_transition_requests(
    run_directory: Path, arm: str
) -> tuple[tuple[dict[str, object], Path], ...]:
    requests: list[tuple[dict[str, object], Path]] = []
    specifications = [
        (
            "e0-to-e1-containment",
            "fault_containment",
            False,
            0,
            1,
            0,
        )
    ]
    if arm == "adaptive":
        specifications.append(
            (
                "e1-to-e2-optimization",
                "performance_optimization",
                True,
                1,
                2,
                40_000,
            )
        )
    for artifact, intent, shape, predecessor, successor, residence_ms in specifications:
        relative_bundle = f"transitions/{artifact}/successor.bundle"
        request: dict[str, object] = {
            "apply_shape_selection": shape,
            "bundle_path": relative_bundle,
            "evidence_snapshot_path": f"transitions/{artifact}/evidence-snapshot.json",
            "evidence_window_rule": "fresh_exact_predecessor_after_common_commit",
            "minimum_post_baseline_observation_ms": 0,
            "minimum_predecessor_residency_ms": residence_ms,
            "policy_intent": intent,
            "policy_parameters": {},
            "predecessor_epoch_number": predecessor,
            "successor_epoch_number": successor,
            "transition_artifact_id": artifact,
        }
        if successor == 1:
            request["containment_baseline_root_source"] = "live_predecessor_roots"
        output = (run_directory / relative_bundle).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        requests.append((request, output))
    return tuple(requests)


def _focused_client_default_epoch(profile: FocusedProfile, adapter: FrozenProfile) -> bytes:
    """Mirror the replica default-tree schedule for the standalone client.

    The client does not read ``main.conf`` and defaults to ``treegen.conf`` in
    its working directory.  Materialize the same cyclic initial epoch used by
    ``tree-generation = default`` so client routing cannot fall back to a
    repository-relative file.
    """

    members = tuple(profile.replica_ids)
    lines = [
        " ".join(
            (
                f"fan:{adapter.fanout}",
                f"pipe:{adapter.pipeline_depth}",
                *(str(replica) for replica in members[offset:] + members[:offset]),
            )
        )
        for offset in range(len(members))
    ]
    return ("\n".join(lines) + "\n").encode("ascii")


def _focused_manager_command(
    profile: FocusedProfile,
    adapter: FrozenProfile,
    *,
    arm: str,
    manager_binary: Path,
    tls: Sequence[Mapping[str, str]],
    issuer: Mapping[str, str],
    run_directory: Path,
    run_id: str,
    source_instance: str,
) -> tuple[str, ...]:
    count = len(profile.replica_ids)
    policy = _NATIVE_RESPONSIVENESS_POLICY
    command = [
        str(manager_binary),
        "--listen",
        f"127.0.0.1:{adapter.manager_port}",
        "--tls-privkey",
        tls[count]["sec"],
        "--tls-cert",
        tls[count]["crt"],
        "--issuer-id",
        str(profiled_fault_runtime.ISSUER_ID),
        "--issuer-private-key",
        issuer["sec"],
        "--activation-delay-blocks",
        str(adapter.activation_delay_blocks),
        "--convergence-deadline-seconds",
        str(max(1, int(adapter.startup_timeout_s))),
        "--tree-fanout",
        str(adapter.fanout),
        "--pipeline-stretch",
        str(adapter.pipeline_depth),
        "--shape-candidate-fanouts",
        str(adapter.fanout),
        "--shape-deterministic-seed",
        str(adapter.snapshot_seed),
        "--responsiveness-policy-version",
        str(policy["policy_version"]),
        "--required-nonresponsive",
        str(len(profile.target_replica_ids)),
        "--responsiveness-attempt-window",
        str(policy["attempt_window"]),
        "--responsiveness-minimum-attempts",
        str(policy["minimum_attempts"]),
        "--responsiveness-minimum-response-rate-ppm",
        str(policy["minimum_response_rate_ppm"]),
        "--responsiveness-maximum-timeout-rate-ppm",
        str(policy["maximum_timeout_rate_ppm"]),
        "--responsiveness-trailing-timeout-streak",
        str(policy["trailing_timeout_streak"]),
        "--responsiveness-latency-percentile-basis-points",
        str(policy["latency_percentile_basis_points"]),
    ]
    for request, output in _focused_transition_requests(run_directory, arm):
        command.extend(
            (
                "--transition-request",
                _canonical_json(request).decode("ascii").rstrip("\n"),
                "--bundle-output",
                str(output),
            )
        )
    command.extend(
        (
            "--structured-event-run-id",
            run_id,
            "--structured-event-source-instance",
            source_instance,
            "--structured-event-output",
            str(run_directory / "raw" / "adaptive-manager.jsonl"),
        )
    )
    for replica in profile.replica_ids:
        command.extend(
            (
                "--replica",
                f"{replica},127.0.0.1:{adapter.peer_base + replica},{tls[replica]['crt']}",
            )
        )
    return tuple(command)


@dataclass(slots=True)
class _FocusedProcesses:
    registry: ProcessRegistry
    records: list[ProcessRecord]
    logs: list[Any]
    evidence: FaultEvidence
    lifecycle: FaultLifecycle


class FocusedLaunchBackend:
    """Concrete local backend with injectable process and evidence seams.

    Construction has no side effects.  All subprocess creation occurs only
    after the CLI has verified the exact parent authorization receipt.
    """

    def __init__(
        self,
        *,
        spawn: Callable[..., tuple[ProcessRecord, Any]] = spawn_owned_process,
        seal_artifacts: Callable[..., dict[str, object]] = _seal_arm_artifacts,
        poll_snapshot: Callable[[str], Mapping[str, object] | None] | None = None,
        poll_interval_s: float = 0.05,
        readiness_timeout_s: float = 60.0,
        execute_fault: Callable[
            [Mapping[str, object], object], Mapping[str, object]
        ]
        | None = None,
        cleanup_registry: Callable[[object], Sequence[object]] | None = None,
        materialize_artifacts: Callable[
            [Mapping[str, object], Mapping[str, object], Mapping[str, object]], None
        ]
        | None = None,
    ) -> None:
        self._spawn = spawn
        self._seal_artifacts = seal_artifacts
        self._poll_snapshot = poll_snapshot
        self._poll_interval_s = max(0.0, float(poll_interval_s))
        self._readiness_timeout_s = max(0.0, float(readiness_timeout_s))
        self._execute_fault = execute_fault
        self._cleanup_registry = cleanup_registry
        self._materialize_artifacts = materialize_artifacts
        self._ledger_tail: str | None = None
        self._fault_receipts: dict[str, dict[str, object]] = {}

    def bind_execution_context(
        self, invocation: Mapping[str, object]
    ) -> Mapping[str, object]:
        profile = invocation.get("profile")
        preflight = _document(
            invocation.get("preflight_receipt"), "preflight receipt"
        )
        if not isinstance(profile, FocusedProfile):
            _error("live execution requires a loaded focused profile")
        execution = _document(
            preflight.get("execution_context"), "preflight execution context"
        )
        if (
            execution.get("profile_sha256") != profile.profile_sha256
            or execution.get("topology_proof_sha256")
            != profile.topology_proof_sha256
            or not isinstance(execution.get("issuer_public_key"), str)
        ):
            _error("live execution context is not preflight-bound")
        repository = Path(__file__).resolve().parents[3]
        build_directory = repository / "build-adaptive"
        authorized_repository = _document(
            execution.get("repository"), "authorized repository"
        )
        current_revision = profiled_fault_runtime.verify_repository_state(repository)
        if authorized_repository.get("revision") != current_revision:
            _error("authorized repository revision drifted")
        binaries = profiled_fault_runtime.exact_binary_paths(
            repository, build_directory
        )
        client = build_directory / "examples" / "hotstuff-client"
        binaries = {**binaries, "client": client}
        for name, path in binaries.items():
            if not path.is_file() or not os.access(path, os.X_OK):
                _error(f"preflighted {name} binary is unavailable")
        build_record = profiled_fault_runtime.verify_exact_build_provenance(
            repository=repository,
            build_directory=build_directory,
            provenance_path=(
                build_directory / profiled_fault_runtime.BUILD_PROVENANCE_FILENAME
            ),
            binaries=binaries,
        )
        authorized_build = _document(execution.get("build"), "authorized build")
        if (
            authorized_build.get("revision") != current_revision
            or authorized_build.get("build_sha256")
            != _sha256(_canonical_json(build_record))
        ):
            _error("authorized build provenance drifted")
        authorized_binaries = _document(
            execution.get("binaries"), "authorized binaries"
        )
        executable_records = _document(
            authorized_binaries.get("executables"), "authorized executables"
        )
        if authorized_binaries.get("verified") is not True or set(
            executable_records
        ) != set(binaries):
            _error("authorized binary set drifted")
        for name, path in binaries.items():
            authorized = _document(
                executable_records.get(name), f"authorized {name} binary"
            )
            resolved = path.resolve()
            if (
                authorized.get("path") != str(resolved)
                or authorized.get("sha256")
                != profiled_fault_runtime.sha256_file(resolved)
            ):
                _error(f"authorized {name} binary identity drifted")
        loaded_allocations = invocation.get("pair_issuer_allocations")
        authorized_allocations = execution.get("pair_issuers")
        if loaded_allocations is not None or authorized_allocations is not None:
            loaded = _document(loaded_allocations, "loaded pair issuers")
            authorized = _document(
                authorized_allocations, "authorized pair issuers"
            )
            if set(loaded) != set(authorized):
                _error("authorized pair issuer cardinality drifted")
            for pair_id in authorized:
                loaded_pair = _document(loaded[pair_id], f"loaded {pair_id} issuer")
                authorized_pair = _document(
                    authorized[pair_id], f"authorized {pair_id} issuer"
                )
                if any(
                    loaded_pair.get(key) != authorized_pair.get(key)
                    for key in (
                        "private_key_path",
                        "private_key_sha256",
                        "public_key_path",
                        "public_key",
                    )
                ):
                    _error(f"authorized {pair_id} issuer identity drifted")
        return {
            **dict(invocation),
            "execution_context": dict(execution),
            "repository": repository,
            "build_directory": build_directory,
            "binaries": binaries,
        }

    def materialize_arm_configuration(
        self,
        context: Mapping[str, object],
        *,
        pair_ordinal: int,
        arm: str,
    ) -> Mapping[str, object]:
        if arm not in {"control", "adaptive"}:
            _error("focused arm must be control or adaptive")
        source_profile = context.get("profile")
        if not isinstance(source_profile, FocusedProfile):
            _error("focused context lost its profile")
        pair_id = f"pair-{_integer(pair_ordinal, 'pair ordinal', 1):02d}"
        pair_seed = _integer(
            context.get("pair_seed", 41_719 + pair_ordinal), "pair seed"
        )
        output_root = Path(context["output_root"])
        configured_run_directory = context.get("slot_directory")
        run_directory = (
            Path(configured_run_directory)
            if isinstance(configured_run_directory, (str, Path))
            else output_root / pair_id / arm
        )
        for path in (
            run_directory,
            run_directory / "config",
            run_directory / "logs",
            run_directory / "raw",
            run_directory / "runtime",
            run_directory / "derived",
            run_directory / "transitions",
        ):
            path.mkdir(parents=True, exist_ok=False)
        (run_directory / "profile.json").write_bytes(source_profile.path.read_bytes())
        proof_destination = run_directory / source_profile.raw["topology"]["proof_path"]
        proof_destination.parent.mkdir(parents=True, exist_ok=True)
        proof_destination.write_bytes(source_profile.topology_proof_path.read_bytes())
        adapter = _profiled_adapter(source_profile, pair_seed)
        binaries = _document(context.get("binaries"), "focused binaries")
        allocations = _document(
            context.get("pair_issuer_allocations"), "pair issuer allocations"
        )
        pair_allocation = _document(
            allocations.get(pair_id), f"{pair_id} issuer allocation"
        )
        arm_allocation = _document(
            pair_allocation.get(arm), f"{pair_id} {arm} issuer"
        )
        if set(arm_allocation) != {"public_key", "private_key"}:
            _error("pair issuer arm allocation schema drifted")
        issuer = {
            "pub": str(arm_allocation["public_key"]),
            "sec": str(arm_allocation["private_key"]),
        }
        if pair_allocation.get("public_key") != issuer["pub"]:
            _error("pair issuer public identity drifted between arms")
        profile = FocusedProfile(
            path=source_profile.path,
            profile_id=source_profile.profile_id,
            profile_sha256=source_profile.profile_sha256,
            topology_proof_sha256=source_profile.topology_proof_sha256,
            topology_proof_path=source_profile.topology_proof_path,
            replica_ids=source_profile.replica_ids,
            quorum=source_profile.quorum,
            target_replica_ids=source_profile.target_replica_ids,
            issuer_public_key=issuer["pub"],
            raw=source_profile.raw,
        )
        bls, tls = _generate_arm_identities(
            adapter,
            keygen_binary=Path(binaries["keygen"]),
            tls_keygen_binary=Path(binaries["tls_keygen"]),
            config_directory=run_directory / "config",
        )
        run_id = f"{pair_id}-{arm}-{uuid.uuid4().hex}"
        instances = {
            f"replica-{replica}": f"{run_id}-replica-{replica}-{uuid.uuid4().hex}"
            for replica in profile.replica_ids
        }
        instances[profiled_fault_runtime.MANAGER_SOURCE_ID] = (
            f"{run_id}-manager-{uuid.uuid4().hex}"
        )
        _legacy_manager, replicas, artifacts = (
            profiled_fault_runtime.write_runtime_inputs(
                adapter,
                run_directory=run_directory,
                app_binary=Path(binaries["app"]),
                manager_binary=Path(binaries["manager"]),
                bls=bls,
                tls=tls,
                issuer=issuer,
                run_id=run_id,
                source_instances=instances,
                include_issuer_identity_artifact=False,
            )
        )
        profiled_fault_runtime.write_exclusive(
            run_directory / "treegen.conf",
            _focused_client_default_epoch(profile, adapter),
        )
        manager = _focused_manager_command(
            profile,
            adapter,
            arm=arm,
            manager_binary=Path(binaries["manager"]),
            tls=tls,
            issuer=issuer,
            run_directory=run_directory,
            run_id=run_id,
            source_instance=instances[profiled_fault_runtime.MANAGER_SOURCE_ID],
        )
        preflight = _document(
            context.get("preflight_receipt"), "parent preflight receipt"
        )
        authorization = _document(
            context.get("authorization_receipt"), "parent authorization receipt"
        )
        request = {
            "schema_version": 1,
            "profile_sha256": source_profile.profile_sha256,
            "topology_proof_sha256": source_profile.topology_proof_sha256,
            "pair_id": pair_id,
            "slot_id": str(
                context.get(
                    "slot_id",
                    f"slot-{2 * pair_ordinal - (1 if arm == 'control' else 0):02d}",
                )
            ),
            "automatic_retries": 0,
            "replacement_policy": "none",
        }
        request_sha = _sha256(_canonical_json(request))
        build_record_path = (
            Path(context["build_directory"])
            / profiled_fault_runtime.BUILD_PROVENANCE_FILENAME
        )
        build_record = _document(
            json.loads(build_record_path.read_bytes()), "exact build provenance"
        )
        build_sha = _sha256(_canonical_json(build_record))
        documents = {
            "preflight.json": {
                **request,
                "request_sha256": request_sha,
                "execution_authorized": False,
                "launch_permitted": False,
            },
            "authorization.json": {
                **request,
                "request_sha256": request_sha,
                "approval_reference": authorization.get("approval_reference"),
                "approved_utc": authorization.get("approved_utc"),
            },
            "pair-receipt.json": {
                "schema_version": 1,
                "pair_id": pair_id,
                "slot_id": request["slot_id"],
                "automatic_retries": 0,
                "replacement_policy": "none",
            },
            "manifest.json": {
                "schema_version": 1,
                "profile_sha256": source_profile.profile_sha256,
                "build_sha256": build_sha,
                "pair_id": pair_id,
                "pair_seed": pair_seed,
                "slot_id": request["slot_id"],
            },
            "runtime/build-provenance.json": {
                "revision": build_record.get("revision"),
                "build_sha256": build_sha,
            },
            "runtime/effective-runtime.json": {
                "profile_sha256": source_profile.profile_sha256,
                "pair_seed": pair_seed,
            },
            "runtime/launch-arguments.json": {
                "manager_argv": profiled_fault_runtime.normalized_manager_argv(manager)
            },
            "derived/phase-windows.json": {"phases": []},
            "derived/throughput.json": {"rows": []},
        }
        for relative, value in documents.items():
            (run_directory / relative).write_bytes(_canonical_json(value))
        (run_directory / "raw" / "issuer-public-key.txt").write_text(
            f"{issuer['pub']}\n", encoding="utf-8"
        )
        (run_directory / "raw" / "client-events.jsonl").write_bytes(b"")
        client = (
            str(binaries["client"]),
            "--conf",
            str(run_directory / "config" / "main.conf"),
            "--idx",
            "0",
            "--iter",
            "-1",
            "--max-async",
            str(max(1, adapter.pipeline_depth * adapter.block_size)),
        )
        plan = FaultPlan(
            ScenarioContext(
                profile.replica_ids,
                profile.quorum,
                len(profile.replica_ids) - profile.quorum,
                1,
            ),
            pair_seed,
            tuple(
                ReplicaGroupSigkill(f"crash-replica-{replica}", replica)
                for replica in profile.target_replica_ids
            ),
        )
        return {
            "context": context,
            "profile": profile,
            "runtime_profile": adapter,
            "pair_id": pair_id,
            "slot_id": request["slot_id"],
            "pair_seed": pair_seed,
            "arm": arm,
            "run_id": run_id,
            "run_directory": run_directory,
            "manager_command": manager,
            "replica_commands": replicas,
            "client_command": client,
            "issuer": issuer,
            "fault_plan": plan,
            "runtime_artifacts": artifacts,
        }

    def spawn_processes(
        self, configuration: Mapping[str, object]
    ) -> _FocusedProcesses:
        root = Path(configuration["run_directory"])
        registry = ProcessRegistry(monotonic_ns=profiled_fault_runtime.monotonic_raw_ns)
        evidence = FaultEvidence(
            root,
            configuration["fault_plan"],
            monotonic_ns=profiled_fault_runtime.monotonic_raw_ns,
        )
        lifecycle = evidence.__enter__()
        records: list[ProcessRecord] = []
        logs: list[Any] = []
        commands = [
            ("adaptive-manager", -1, configuration["manager_command"]),
            *(
                (f"replica-{replica}", replica, command)
                for replica, command in zip(
                    configuration["profile"].replica_ids,
                    configuration["replica_commands"],
                    strict=True,
                )
            ),
            ("workload-client", -2, configuration["client_command"]),
        ]
        try:
            for name, replica, command in commands:
                record, log = self._spawn(
                    registry,
                    name=name,
                    replica_id=replica,
                    command=command,
                    log_path=root / "logs" / f"{name}.log",
                    working_directory=root,
                )
                records.append(record)
                logs.append(log)
            manager = records[0]
            observed = _capture_process_argv(manager)
            requested = tuple(configuration["manager_command"])
            manager_input = {
                "input_source": "normalized_manager_launch_boundary_v1",
                "requested_argv": list(requested),
                "observed_argv": list(observed),
                "stdin": "closed",
            }
            _validate_manager_launch_boundary(
                requested,
                observed,
                manager_input=manager_input,
                forbidden_values=tuple(
                    f"crash-replica-{replica}"
                    for replica in configuration["profile"].target_replica_ids
                ),
            )
            root = Path(configuration["run_directory"])
            normalized_requested = profiled_fault_runtime.normalized_manager_argv(requested)
            normalized_observed = profiled_fault_runtime.normalized_manager_argv(observed)
            (root / "runtime" / "manager-observed-argv.json").write_bytes(
                _canonical_json({"argv": normalized_observed})
            )
            (root / "runtime" / "manager-input.json").write_bytes(
                _canonical_json(
                    {
                        **manager_input,
                        "requested_argv": normalized_requested,
                        "observed_argv": normalized_observed,
                    }
                )
            )
            return _FocusedProcesses(registry, records, logs, evidence, lifecycle)
        except BaseException as exc:
            registry.cleanup(timeout_s=2.0)
            for log in logs:
                log.close()
            evidence.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def execute_atomic_fault_batch(
        self,
        configuration: Mapping[str, object],
        processes: _FocusedProcesses,
    ) -> Mapping[str, object]:
        outcomes = _execute_atomic_fault_batch(
            processes.registry,
            configuration["fault_plan"],
            processes.lifecycle,
            5.0,
        )
        survivor_ids = [
            replica
            for replica in configuration["profile"].replica_ids
            if replica not in configuration["profile"].target_replica_ids
        ]
        outcome_documents = [asdict(outcome) for outcome in outcomes]
        result = {
            "schema_version": 1,
            "atomic": True,
            "confirmed_target_ids": [outcome.replica_id for outcome in outcomes],
            "survivor_replica_ids": survivor_ids,
            "source_monotonic_ns": max(
                outcome.confirmed_monotonic_ns for outcome in outcomes
            ),
            "sigkill_outcomes": outcome_documents,
        }
        target_ids = set(configuration["profile"].target_replica_ids)
        process_records = [
            {
                "name": record.name,
                "replica_id": record.replica_id,
                "pid": record.pid,
                "pgid": record.pgid,
            }
            for record in processes.records
            if record.replica_id in target_ids
        ]
        root = Path(configuration["run_directory"])
        journal = profiled_fault_runtime.read_jsonl(
            root / "raw" / "fault-orchestrator.jsonl", allow_partial=False
        )
        receipt = {
            "schema_version": 1,
            "fault_plan": json.loads(configuration["fault_plan"].canonical_json()),
            "process_records": process_records,
            "sigkill_outcomes": outcome_documents,
            "fault_journal": journal,
        }
        profiled_fault_runtime.write_exclusive(
            root / "raw" / "fault-receipt.json",
            _canonical_json(receipt),
        )
        self._fault_receipts[str(root.resolve())] = receipt
        return result

    def _wait_for_snapshot(
        self,
        name: str,
        poll: Callable[[str], Mapping[str, object] | None],
    ) -> Mapping[str, object]:
        deadline = time.monotonic() + self._readiness_timeout_s
        while True:
            snapshot = poll(name)
            if snapshot is not None:
                return dict(_document(snapshot, f"{name} runtime snapshot"))
            if time.monotonic() >= deadline:
                _error(f"timed out waiting for {name} runtime evidence")
            if self._poll_interval_s:
                time.sleep(self._poll_interval_s)

    def run_arm(
        self,
        configuration: Mapping[str, object],
        processes: object,
    ) -> Mapping[str, object]:
        """Drive one arm; the atomic fault exists only at its causal hook."""

        if self._poll_snapshot is None:
            source = FocusedRawEvidenceSource(
                run_directory=Path(configuration["run_directory"]),
                poll_interval_s=self._poll_interval_s,
                timeout_s=self._readiness_timeout_s,
                process_records=tuple(getattr(processes, "records", ())),
            )
            poll = source.poll
            unexpected_exits = source.unexpected_exit_ids
        else:
            poll = self._poll_snapshot
            unexpected_exits = lambda: ()
        try:
            self._wait_for_snapshot("readiness", poll)
        except KeyError:
            if self._poll_snapshot is None:
                raise

        def wait(name: str) -> Mapping[str, object]:
            return self._wait_for_snapshot(name, poll)

        def inject() -> Mapping[str, object]:
            execute = self._execute_fault
            if execute is not None:
                outcome = dict(execute(configuration, processes))
            else:
                outcome = dict(
                    self.execute_atomic_fault_batch(  # type: ignore[arg-type]
                        configuration, processes
                    )
                )
            observed = wait("fault")
            if dict(observed) != outcome:
                _error("observed fault receipt differs from the atomic outcome")
            return outcome

        hooks = ArmRuntimeHooks(
            wait_for_stable_phase=lambda phase: wait(phase),
            inject_atomic_fault_batch=inject,
            wait_for_nonresponse=lambda: wait("nonresponse"),
            issue_epoch_request=lambda epoch: wait(f"epoch{epoch}"),
            wait_for_epoch_commands=lambda epoch: wait(f"commands{epoch}"),
            wait_for_epoch_activations=lambda epoch: wait(f"activations{epoch}"),
            wait_for_common_commit=lambda epoch: wait(f"commit{epoch}"),
            rebuild_ranking=lambda: wait("ranking"),
            unexpected_exit_ids=unexpected_exits,
        )
        arm = "C" if configuration.get("arm") == "control" else "A"
        outcome = _drive_arm_state_machine(
            configuration["profile"],
            arm,
            str(configuration["pair_id"]),
            hooks,
        )
        return {**outcome, "runtime_graph": "complete"}

    def drive_event_hooks(
        self,
        configuration: Mapping[str, object],
        processes: _FocusedProcesses,
        fault_receipt: Mapping[str, object],
    ) -> Mapping[str, object]:
        root = Path(configuration["run_directory"])
        streams = {
            path.name: profiled_fault_runtime.read_jsonl(path, allow_partial=False)
            for path in sorted((root / "raw").glob("*.jsonl"))
        }
        events = [event for stream in streams.values() for event in stream]
        required = {"block.committed", "epoch.command_committed", "epoch.activated"}
        observed = {str(event.get("event_type")) for event in events}
        if not required.issubset(observed):
            _error("live event streams do not contain the required causal graph")
        if configuration["arm"] == "adaptive" and not any(
            event.get("event_type") == "epoch.activated"
            and _document(event.get("payload"), "activation").get("epoch_number") == 2
            for event in events
        ):
            _error("adaptive arm did not produce an Epoch 2 activation")
        return {
            "schema_version": 1,
            "runtime_graph": "complete",
            "event_stream_sha256": _sha256(_canonical_json(streams)),
            "fault_receipt_sha256": _sha256(_canonical_json(fault_receipt)),
            "event_count": len(events),
        }

    def cleanup(
        self,
        configuration: Mapping[str, object],
        processes: _FocusedProcesses,
    ) -> Mapping[str, object]:
        outcomes = (
            self._cleanup_registry(processes)
            if self._cleanup_registry is not None
            else processes.registry.cleanup(timeout_s=2.0)
        )
        for log in getattr(processes, "logs", ()):
            log.close()
        evidence = getattr(processes, "evidence", None)
        if evidence is not None:
            evidence.__exit__(None, None, None)
        return {
            "complete": all(
                record.process.poll() is not None
                for record in getattr(processes, "records", ())
            ),
            "outcomes": [
                asdict(outcome) if is_dataclass(outcome) else outcome
                for outcome in outcomes
            ],
        }

    def materialize_artifacts(
        self,
        configuration: Mapping[str, object],
        outcome: Mapping[str, object],
        cleanup: Mapping[str, object],
    ) -> None:
        if cleanup.get("complete") is not True:
            _error("artifacts cannot materialize before complete cleanup")
        if self._materialize_artifacts is not None:
            self._materialize_artifacts(configuration, outcome, cleanup)
            return
        if self._poll_snapshot is not None or self._execute_fault is not None:
            return
        root = Path(configuration["run_directory"])
        receipt_path = root / "raw" / "fault-receipt.json"
        receipt = self._fault_receipts.get(str(root.resolve()))
        if receipt is None or not receipt_path.is_file():
            _error("atomic fault receipt was not finalized before artifacts")
        for epoch, artifact in (
            (1, "e0-to-e1-containment"),
            (2, "e1-to-e2-optimization"),
        ):
            source = root / "transitions" / artifact / "successor.bundle"
            destination = root / "raw" / f"epoch{epoch}.bundle"
            if source.is_file():
                if destination.exists():
                    _error(f"raw Epoch {epoch} bundle already exists")
                shutil.copyfile(source, destination)
            elif epoch == 1 or configuration.get("arm") == "adaptive":
                _error(f"native Epoch {epoch} bundle output is absent")

        raw_root = root / "raw"
        replica_events: list[Mapping[str, Any]] = []
        for path in sorted(raw_root.glob("replica-*.jsonl")):
            replica_events.extend(
                profiled_fault_runtime.read_jsonl(path, allow_partial=False)
            )
        manager_events = profiled_fault_runtime.read_jsonl(
            raw_root / "adaptive-manager.jsonl", allow_partial=False
        )
        client_events = profiled_fault_runtime.read_jsonl(
            raw_root / "client-events.jsonl", allow_partial=False
        )
        if not replica_events or not manager_events:
            _error("native raw event sources are incomplete")
        for path, events in (
            (raw_root / "replica-events.jsonl", replica_events),
            (raw_root / "adaptive-manager-events.jsonl", manager_events),
        ):
            profiled_fault_runtime.write_exclusive(
                path,
                b"".join(_canonical_json(event) for event in events),
            )
        all_events = [*replica_events, *manager_events, *client_events]
        inventory = sorted(
            {
                (str(event.get("source_kind")), str(event.get("source_id")))
                for event in all_events
            }
        )
        (root / "runtime" / "source-inventory.json").write_bytes(
            _canonical_json({"sources": [list(source) for source in inventory]})
        )

    def seal(
        self,
        configuration: Mapping[str, object],
        outcome: Mapping[str, object],
        cleanup: Mapping[str, object],
    ) -> Mapping[str, object]:
        root = Path(configuration["run_directory"])
        required = {
            str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
        } | {"runner-outcome.json", "cleanup.json"}
        return self._seal_artifacts(
            root, sorted(required), outcome, cleanup
        )

    def validate(
        self,
        configuration: Mapping[str, object],
        seal: Mapping[str, object],
    ) -> Mapping[str, object]:
        return {
            "schema_version": 1,
            "verdict": "PROVISIONAL",
            "trusted_provenance_supplied": False,
            "trusted_provenance_required": True,
            "pending_external_provenance": {
                "run_directory": str(configuration["run_directory"]),
                "evidence_tree_sha256": seal.get("tree_sha256"),
                "evidence_seal_sha256": seal.get("seal_sha256"),
            },
        }

    def append_ledger(
        self,
        context: Mapping[str, object],
        configuration: Mapping[str, object],
        validation: Mapping[str, object],
    ) -> Mapping[str, object]:
        if validation.get("verdict") not in {"PASS", "PROVISIONAL"}:
            _error("terminal ledger requires a completed or provisional child")
        record = {
            "schema_version": 1,
            "pair_id": configuration["pair_id"],
            "slot_id": configuration["slot_id"],
            "arm": configuration["arm"],
            "previous_record_sha256": self._ledger_tail,
            "validation_sha256": _sha256(_canonical_json(validation)),
            "automatic_retries": 0,
            "replacement_policy": "none",
            "terminal": True,
        }
        digest = _sha256(_canonical_json(record))
        self._ledger_tail = digest
        return {**record, "record_sha256": digest}
