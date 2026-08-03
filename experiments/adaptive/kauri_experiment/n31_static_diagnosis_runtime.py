"""One-attempt runtime for the frozen N=31 signer-aware diagnosis.

The implementation deliberately delegates process ownership, exact-build
provenance, identity generation, structured-event tailing, configuration
boundaries, and cleanup to :mod:`profiled_fault_runtime`.  This module binds
one post-baseline tree-30 marker to an order-independent same-proposal
timeout/aggregate pair.  A passing pilot validates only the harness.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import IO, Any
import uuid

from .faults import FaultEvidence, FaultLifecycle
from .n31_static_diagnosis import (
    ARM_NAMES,
    DiagnosticArm,
    FrozenN31StaticDiagnosisProfile,
    N31StaticDiagnosisError,
    SCENARIO,
    build_fault_plan,
    build_launch_contract,
    build_signer_aware_certificates,
    load_frozen_profile,
    validate_n31_static_diagnosis_run,
)
from .processes import ProcessRecord, ProcessRegistry
from .profiled_fault_archive import (
    EvidenceSealError,
    create_evidence_seal,
    verify_evidence_seal,
)
from .profiled_fault_evaluation import (
    FrozenProfile,
    ProfiledFaultEvaluationError,
    load_frozen_profile as load_runtime_profile,
)
from . import profiled_fault_runtime as runtime

MANAGER_SOURCE_ID = "adaptive-manager"
ATTEMPT_SCOPE = "one_invocation_without_automatic_retry"
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_TRUSTED_BINARY_NAMES = frozenset(
    {"app", "manager", "keygen", "tls_keygen", "epoch_profile_digest"}
)
_ROOT_PAYLOAD_FIELDS = {
    "epoch_number",
    "tree_id",
    "epoch_digest",
    "block_hash",
    "context_generation",
    "observer_replica",
    "wait_exempt_signers",
    "accepted_signers",
    "absent_direct_children",
    "missing_optional_signers",
    "required_branch_gaps",
    "root_signer_count",
    "global_quorum",
    "rejection_reason",
}


class N31StaticDiagnosisRuntimeError(RuntimeError):
    """The live attempt cannot safely proceed or qualify as evidence."""


@dataclass(frozen=True, slots=True)
class TrustedBinary:
    """One externally anchored executable, never reconstructed from an archive."""

    name: str
    path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            self.name not in _TRUSTED_BINARY_NAMES
            or not isinstance(self.path, str)
            or not Path(self.path).is_absolute()
            or type(self.size_bytes) is not int
            or self.size_bytes <= 0
            or not isinstance(self.sha256, str)
            or _HEX_256.fullmatch(self.sha256) is None
        ):
            raise N31StaticDiagnosisRuntimeError("trusted binary receipt is malformed")


@dataclass(frozen=True, slots=True)
class TrustedProvenance:
    """Portable external receipt anchoring archive provenance and launch argv."""

    revision: str
    required_branch: str
    remote_tracking_ref: str
    repository_clean: bool
    head_equals_remote: bool
    repository: str
    build_directory: str
    build_provenance_file_sha256: str
    build_provenance_document_sha256: str
    binaries: tuple[TrustedBinary, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", self.revision) is None
            or self.required_branch != runtime.REQUIRED_BRANCH
            or self.remote_tracking_ref != f"origin/{runtime.REQUIRED_BRANCH}"
            or self.repository_clean is not True
            or self.head_equals_remote is not True
            or not isinstance(self.repository, str)
            or not Path(self.repository).is_absolute()
            or not isinstance(self.build_directory, str)
            or not Path(self.build_directory).is_absolute()
            or not isinstance(self.build_provenance_file_sha256, str)
            or _HEX_256.fullmatch(self.build_provenance_file_sha256) is None
            or not isinstance(self.build_provenance_document_sha256, str)
            or _HEX_256.fullmatch(self.build_provenance_document_sha256) is None
            or not isinstance(self.binaries, tuple)
            or any(type(binary) is not TrustedBinary for binary in self.binaries)
        ):
            raise N31StaticDiagnosisRuntimeError(
                "trusted provenance receipt is malformed"
            )
        names = tuple(binary.name for binary in self.binaries)
        if set(names) != _TRUSTED_BINARY_NAMES or names != tuple(sorted(names)):
            raise N31StaticDiagnosisRuntimeError(
                "trusted provenance binary membership is not canonical"
            )

    def binary(self, name: str) -> TrustedBinary:
        for binary in self.binaries:
            if binary.name == name:
                return binary
        raise N31StaticDiagnosisRuntimeError(f"trusted provenance lacks binary: {name}")

    def as_document(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "kauri-n31-trusted-provenance-v1",
            "repository_state": {
                "required_branch": self.required_branch,
                "remote_tracking_ref": self.remote_tracking_ref,
                "revision": self.revision,
                "clean": self.repository_clean,
                "head_equals_remote": self.head_equals_remote,
            },
            "repository": self.repository,
            "build_directory": self.build_directory,
            "build_provenance_file_sha256": (self.build_provenance_file_sha256),
            "build_provenance_document_sha256": (self.build_provenance_document_sha256),
            "binaries": {
                binary.name: {
                    "path": binary.path,
                    "size_bytes": binary.size_bytes,
                    "sha256": binary.sha256,
                }
                for binary in self.binaries
            },
        }

    @property
    def sha256(self) -> str:
        return _canonical_document_sha256(self.as_document())


def _canonical_document_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as error:
        raise N31StaticDiagnosisRuntimeError(
            "trusted provenance document is not canonical JSON"
        ) from error


def _canonical_document_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_document_bytes(value)).hexdigest()


def write_trusted_provenance(
    path: Path,
    trusted_provenance: TrustedProvenance,
) -> str:
    """Create one canonical receipt outside any run directory."""

    if type(trusted_provenance) is not TrustedProvenance:
        raise N31StaticDiagnosisRuntimeError(
            "trusted provenance output requires an exact receipt"
        )
    path = path.resolve()
    if path.exists():
        if load_trusted_provenance(path) != trusted_provenance:
            raise N31StaticDiagnosisRuntimeError(
                "existing trusted provenance receipt differs from current build"
            )
        return trusted_provenance.sha256
    runtime.write_exclusive(
        path,
        _canonical_document_bytes(trusted_provenance.as_document()),
    )
    return trusted_provenance.sha256


def load_trusted_provenance(path: Path) -> TrustedProvenance:
    """Load an out-of-band canonical receipt without consulting a repository."""

    path = path.resolve()
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise N31StaticDiagnosisRuntimeError(
            "trusted provenance receipt is unreadable"
        ) from error
    if not isinstance(document, Mapping) or set(document) != {
        "schema_version",
        "kind",
        "repository_state",
        "repository",
        "build_directory",
        "build_provenance_file_sha256",
        "build_provenance_document_sha256",
        "binaries",
    }:
        raise N31StaticDiagnosisRuntimeError(
            "trusted provenance receipt schema drifted"
        )
    binaries = document.get("binaries")
    repository_state = document.get("repository_state")
    if (
        document.get("schema_version") != 1
        or document.get("kind") != "kauri-n31-trusted-provenance-v1"
        or not isinstance(repository_state, Mapping)
        or set(repository_state)
        != {
            "required_branch",
            "remote_tracking_ref",
            "revision",
            "clean",
            "head_equals_remote",
        }
        or not isinstance(binaries, Mapping)
        or set(binaries) != _TRUSTED_BINARY_NAMES
    ):
        raise N31StaticDiagnosisRuntimeError(
            "trusted provenance receipt identity drifted"
        )
    values: list[TrustedBinary] = []
    for name in sorted(_TRUSTED_BINARY_NAMES):
        binary = binaries.get(name)
        if not isinstance(binary, Mapping) or set(binary) != {
            "path",
            "size_bytes",
            "sha256",
        }:
            raise N31StaticDiagnosisRuntimeError(
                f"trusted provenance binary schema drifted: {name}"
            )
        values.append(
            TrustedBinary(
                name=name,
                path=binary.get("path"),
                size_bytes=binary.get("size_bytes"),
                sha256=binary.get("sha256"),
            )
        )
    receipt = TrustedProvenance(
        revision=repository_state.get("revision"),
        required_branch=repository_state.get("required_branch"),
        remote_tracking_ref=repository_state.get("remote_tracking_ref"),
        repository_clean=repository_state.get("clean"),
        head_equals_remote=repository_state.get("head_equals_remote"),
        repository=document.get("repository"),
        build_directory=document.get("build_directory"),
        build_provenance_file_sha256=document.get("build_provenance_file_sha256"),
        build_provenance_document_sha256=document.get(
            "build_provenance_document_sha256"
        ),
        binaries=tuple(values),
    )
    if raw != _canonical_document_bytes(receipt.as_document()):
        raise N31StaticDiagnosisRuntimeError(
            "trusted provenance receipt is not canonical"
        )
    return receipt


def derive_trusted_provenance(
    *,
    repository: Path,
    app_binary: Path,
    manager_binary: Path,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    epoch_profile_digest_binary: Path,
    build_directory: Path,
    build_provenance_path: Path,
) -> TrustedProvenance:
    """Derive a receipt from the clean pushed fixed branch and trusted build."""

    repository = repository.resolve()
    build_directory = build_directory.resolve()
    provenance_path = build_provenance_path.resolve()
    supplied = {
        "app": app_binary.resolve(),
        "manager": manager_binary.resolve(),
        "keygen": keygen_binary.resolve(),
        "tls_keygen": tls_keygen_binary.resolve(),
        "epoch_profile_digest": epoch_profile_digest_binary.resolve(),
    }
    revision_before = runtime.verify_repository_state(repository)
    record = runtime.verify_exact_build_provenance(
        repository=repository,
        build_directory=build_directory,
        provenance_path=provenance_path,
        binaries=supplied,
    )
    revision_after = runtime.verify_repository_state(repository)
    recorded_binaries = record.get("binaries")
    if not isinstance(recorded_binaries, Mapping):
        raise N31StaticDiagnosisRuntimeError(
            "verified build provenance lacks binary records"
        )
    receipts: list[TrustedBinary] = []
    for name in sorted(_TRUSTED_BINARY_NAMES):
        value = recorded_binaries.get(name)
        if not isinstance(value, Mapping):
            raise N31StaticDiagnosisRuntimeError(
                f"verified build provenance lacks binary: {name}"
            )
        receipts.append(
            TrustedBinary(
                name=name,
                path=str(value.get("path")),
                size_bytes=value.get("size_bytes"),
                sha256=str(value.get("sha256")),
            )
        )
    revision = record.get("revision")
    if (
        not isinstance(revision, str)
        or revision != revision_before
        or revision != revision_after
    ):
        raise N31StaticDiagnosisRuntimeError(
            "trusted repository revision changed while deriving provenance"
        )
    return TrustedProvenance(
        revision=revision,
        required_branch=runtime.REQUIRED_BRANCH,
        remote_tracking_ref=f"origin/{runtime.REQUIRED_BRANCH}",
        repository_clean=True,
        head_equals_remote=True,
        repository=str(repository),
        build_directory=str(build_directory),
        build_provenance_file_sha256=runtime.sha256_file(provenance_path),
        build_provenance_document_sha256=_canonical_document_sha256(record),
        binaries=tuple(receipts),
    )


def _selected_arm(profile: FrozenN31StaticDiagnosisProfile, name: str) -> DiagnosticArm:
    if name not in ARM_NAMES:
        raise N31StaticDiagnosisRuntimeError(f"unknown diagnosis arm: {name}")
    return next(candidate for candidate in profile.arms if candidate.name == name)


def _load_bound_profiles(
    diagnosis_profile_path: Path, repository: Path
) -> tuple[FrozenN31StaticDiagnosisProfile, FrozenProfile, Path]:
    repository = repository.resolve()
    diagnosis = load_frozen_profile(diagnosis_profile_path.resolve())
    runtime_path = (repository / diagnosis.runtime_profile_path).resolve()
    try:
        runtime_path.relative_to(repository)
    except ValueError as error:
        raise N31StaticDiagnosisRuntimeError(
            "runtime profile escapes the Kauri repository"
        ) from error
    profiled = load_runtime_profile(runtime_path)
    runtime.require_shipped_profile(profiled)
    if (
        profiled.profile_id != diagnosis.runtime_profile_id
        or profiled.profile_sha256 != diagnosis.runtime_profile_sha256
        or profiled.replica_ids != diagnosis.replica_ids
        or profiled.fault_threshold != diagnosis.fault_threshold
        or profiled.quorum != diagnosis.quorum
        or profiled.fanout != diagnosis.fanout
        or profiled.pipeline_depth != diagnosis.pipeline_stretch
        or profiled.tree_switch_period_blocks != diagnosis.tree_switch_period_blocks
    ):
        raise N31StaticDiagnosisRuntimeError(
            "bound runtime profile differs from the diagnosis geometry"
        )
    return diagnosis, profiled, runtime_path


def _commit_witnesses(
    diagnosis: FrozenN31StaticDiagnosisProfile,
    profiled: FrozenProfile,
) -> tuple[int, ...]:
    witnesses = tuple(diagnosis.commit_witnesses)
    if (
        len(witnesses) != diagnosis.quorum
        or len(set(witnesses)) != len(witnesses)
        or any(replica not in diagnosis.replica_ids for replica in witnesses)
        or profiled.authoritative_observer not in witnesses
    ):
        raise N31StaticDiagnosisRuntimeError(
            "diagnosis commit witnesses are not one fixed Q21 set"
        )
    return witnesses


def preflight(
    *,
    diagnosis_profile_path: Path,
    repository: Path,
    app_binary: Path,
    manager_binary: Path,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    epoch_profile_digest_binary: Path,
    build_directory: Path,
    build_provenance_path: Path,
) -> dict[str, object]:
    """Verify the clean pushed revision and both frozen profile bindings."""

    diagnosis, profiled, runtime_path = _load_bound_profiles(
        diagnosis_profile_path, repository
    )
    witnesses = _commit_witnesses(diagnosis, profiled)
    result = runtime.preflight(
        profile_path=runtime_path,
        repository=repository.resolve(),
        app_binary=app_binary.resolve(),
        manager_binary=manager_binary.resolve(),
        keygen_binary=keygen_binary.resolve(),
        tls_keygen_binary=tls_keygen_binary.resolve(),
        epoch_profile_digest_binary=epoch_profile_digest_binary.resolve(),
        build_directory=build_directory.resolve(),
        build_provenance_path=build_provenance_path.resolve(),
    )
    revision = str(result["revision"])
    return {
        "schema_version": 1,
        "scenario": SCENARIO,
        "verdict": "PASS",
        "diagnosis_profile": {
            "profile_id": diagnosis.profile_id,
            "sha256": diagnosis.profile_sha256,
        },
        "runtime_profile": {
            "profile_id": profiled.profile_id,
            "sha256": profiled.profile_sha256,
            "path": str(runtime_path),
        },
        "revision": revision,
        "commit_witnesses": list(witnesses),
        "launch_contracts": {
            arm: build_launch_contract(diagnosis, arm=arm, kauri_revision=revision)
            for arm in ARM_NAMES
        },
        "profiled_runtime": result,
    }


def _marker_path(run_directory: Path, arm: DiagnosticArm) -> Path:
    return run_directory / "logs" / f"replica-{arm.actor_replica_id}.log"


def _log_cursor(run_directory: Path, arm: DiagnosticArm) -> int:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            payload = _marker_path(run_directory, arm).read_bytes()
        except OSError as error:
            raise N31StaticDiagnosisRuntimeError(
                "cannot snapshot the actor log"
            ) from error
        if not payload or payload.endswith(b"\n"):
            return len(payload)
        if time.monotonic() >= deadline:
            raise N31StaticDiagnosisRuntimeError(
                "actor log did not reach a complete-line snapshot boundary"
            )
        time.sleep(0.005)


def _marker_prefix(arm: DiagnosticArm) -> str:
    return f"KAURI_FAULT {arm.runtime_marker}"


def _marker_tokens(line: str) -> dict[str, str]:
    pairs = re.findall(r"(?:^| )([a-z_]+)=([^ ]+)", line)
    tokens = dict(pairs)
    if len(tokens) != len(pairs):
        raise N31StaticDiagnosisRuntimeError("actor marker contains duplicate fields")
    return tokens


def _reject_earlier_marker(
    run_directory: Path,
    arm: DiagnosticArm,
    end_offset: int,
) -> None:
    try:
        with _marker_path(run_directory, arm).open("rb") as source:
            payload = source.read(end_offset)
    except OSError as error:
        raise N31StaticDiagnosisRuntimeError("cannot read the actor log") from error
    forbidden = [_marker_prefix(arm).encode()]
    if arm.positive_suppression_marker is not None:
        forbidden.append(f"KAURI_FAULT {arm.positive_suppression_marker}".encode())
    if any(marker in payload for marker in forbidden):
        raise N31StaticDiagnosisRuntimeError(
            "matching tree-30 fault marker appeared before the clean baseline"
        )


def _find_markers(
    diagnosis: FrozenN31StaticDiagnosisProfile,
    arm: DiagnosticArm,
    run_directory: Path,
    *,
    start_offset: int,
    end_offset: int | None = None,
) -> list[dict[str, object]]:
    path = _marker_path(run_directory, arm)
    if end_offset is not None and end_offset < start_offset:
        raise N31StaticDiagnosisRuntimeError(
            "actor log terminal offset precedes its baseline offset"
        )
    try:
        with path.open("rb") as source:
            source.seek(start_offset)
            payload = source.read(
                -1 if end_offset is None else end_offset - start_offset
            )
    except FileNotFoundError:
        return []
    except OSError as error:
        raise N31StaticDiagnosisRuntimeError("cannot read the actor log") from error
    newline = payload.rfind(b"\n")
    if newline < 0:
        return []
    prefix = _marker_prefix(arm)
    candidates: list[tuple[str, str, int]] = []
    for line in payload[: newline + 1].decode("utf-8", errors="replace").splitlines():
        if f"{prefix} " not in line:
            continue
        tokens = _marker_tokens(line)
        required = {
            "epoch": str(diagnosis.epoch_number),
            "tree": str(diagnosis.phase.tree_id),
            "window": diagnosis.diagnostic_window,
        }
        if any(tokens.get(key) != value for key, value in required.items()):
            continue
        if arm.name == ARM_NAMES[0]:
            if tokens.get("reporter") != str(diagnosis.reporter_id) or tokens.get(
                "target"
            ) != str(diagnosis.target_id):
                continue
        elif tokens.get("replica") != str(diagnosis.target_id) or tokens.get(
            "parent"
        ) != str(diagnosis.reporter_id):
            continue
        block_hash = tokens.get("block")
        marker_raw = tokens.get("monotonic_ns")
        try:
            marker_ns = int(marker_raw or "")
        except ValueError as error:
            raise N31StaticDiagnosisRuntimeError(
                "actor marker has an invalid monotonic timestamp"
            ) from error
        if (
            block_hash is None
            or _HEX_256.fullmatch(block_hash) is None
            or marker_ns <= 0
        ):
            raise N31StaticDiagnosisRuntimeError(
                "actor marker has no exact proposal identity and clock"
            )
        candidates.append((line[-1024:], block_hash, marker_ns))
    if len({block for _line, block, _clock in candidates}) != len(candidates):
        raise N31StaticDiagnosisRuntimeError(
            "duplicate actor marker exists for one proposal context"
        )
    configuration = (
        f"{diagnosis.epoch_number}:{diagnosis.phase.tree_id}:"
        f"{diagnosis.epoch_digest}"
    )
    return [
        {
            "kind": arm.runtime_marker,
            "source_id": f"replica-{arm.actor_replica_id}",
            "actor_replica_id": arm.actor_replica_id,
            "reporter_id": diagnosis.reporter_id,
            "target_id": diagnosis.target_id,
            "configuration": configuration,
            "block_hash": block_hash,
            "marker_monotonic_ns": marker_ns,
            "line": line,
            "log_path": str(path.relative_to(run_directory)),
            "log_start_offset": start_offset,
            "matching_line_count": len(candidates),
        }
        for line, block_hash, marker_ns in candidates
    ]


def _observation(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if event.get("event_type") != "evidence.observation_accepted":
        return None
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    observation = payload.get("observation")
    return observation if isinstance(observation, Mapping) else None


def _find_false_positive_suppressions(
    diagnosis: FrozenN31StaticDiagnosisProfile,
    arm: DiagnosticArm,
    run_directory: Path,
    *,
    start_offset: int,
    end_offset: int | None = None,
) -> list[dict[str, object]]:
    if arm.positive_suppression_marker is None:
        return []
    if end_offset is not None and end_offset < start_offset:
        raise N31StaticDiagnosisRuntimeError(
            "positive-suppression terminal offset precedes its baseline offset"
        )
    path = _marker_path(run_directory, arm)
    try:
        with path.open("rb") as source:
            source.seek(start_offset)
            payload = source.read(
                -1 if end_offset is None else end_offset - start_offset
            )
    except FileNotFoundError:
        return []
    except OSError as error:
        raise N31StaticDiagnosisRuntimeError("cannot read the actor log") from error
    newline = payload.rfind(b"\n")
    if newline < 0:
        return []
    prefix = f"KAURI_FAULT {arm.positive_suppression_marker}"
    configuration = (
        f"{diagnosis.epoch_number}:{diagnosis.phase.tree_id}:"
        f"{diagnosis.epoch_digest}"
    )
    values: list[dict[str, object]] = []
    for line in payload[: newline + 1].decode("utf-8", errors="replace").splitlines():
        if f"{prefix} " not in line:
            continue
        tokens = _marker_tokens(line)
        if (
            tokens.get("reporter") != str(diagnosis.reporter_id)
            or tokens.get("target") != str(diagnosis.target_id)
            or tokens.get("epoch") != str(diagnosis.epoch_number)
            or tokens.get("tree") != str(diagnosis.phase.tree_id)
            or tokens.get("window") != diagnosis.diagnostic_window
        ):
            continue
        block_hash = tokens.get("block")
        try:
            marker_ns = int(tokens.get("monotonic_ns", ""))
        except ValueError as error:
            raise N31StaticDiagnosisRuntimeError(
                "positive-suppression marker clock is invalid"
            ) from error
        if (
            block_hash is None
            or _HEX_256.fullmatch(block_hash) is None
            or marker_ns <= 0
        ):
            raise N31StaticDiagnosisRuntimeError(
                "positive-suppression marker identity is invalid"
            )
        values.append(
            {
                "kind": arm.positive_suppression_marker,
                "source_id": f"replica-{arm.actor_replica_id}",
                "reporter_id": diagnosis.reporter_id,
                "target_id": diagnosis.target_id,
                "configuration": configuration,
                "block_hash": block_hash,
                "line": line[-1024:],
                "marker_monotonic_ns": marker_ns,
            }
        )
    return values


def _canonical_replica_list(
    diagnosis: FrozenN31StaticDiagnosisProfile,
    value: object,
    label: str,
    *,
    nonempty: bool = False,
) -> list[int]:
    if not isinstance(value, list) or any(type(item) is not int for item in value):
        raise N31StaticDiagnosisRuntimeError(f"{label} must be an integer array")
    if (
        (nonempty and not value)
        or value != sorted(set(value))
        or any(item not in diagnosis.replica_ids for item in value)
    ):
        raise N31StaticDiagnosisRuntimeError(f"{label} is non-canonical")
    return value


def _validate_root_auxiliary_payload(
    diagnosis: FrozenN31StaticDiagnosisProfile,
    payload: Mapping[str, Any],
) -> list[int]:
    accepted = _canonical_replica_list(
        diagnosis, payload.get("accepted_signers"), "root accepted signers"
    )
    for field in (
        "wait_exempt_signers",
        "absent_direct_children",
        "missing_optional_signers",
    ):
        _canonical_replica_list(
            diagnosis,
            payload.get(field),
            f"root {field}",
        )
    gaps = payload.get("required_branch_gaps")
    if not isinstance(gaps, list):
        raise N31StaticDiagnosisRuntimeError(
            "root required_branch_gaps must be an array"
        )
    previous_child = -1
    for gap in gaps:
        if not isinstance(gap, Mapping) or set(gap) != {
            "direct_child",
            "missing_required_signers",
        }:
            raise N31StaticDiagnosisRuntimeError(
                "root required branch gap schema drifted"
            )
        direct_child = gap.get("direct_child")
        if (
            type(direct_child) is not int
            or direct_child not in diagnosis.replica_ids
            or direct_child <= previous_child
        ):
            raise N31StaticDiagnosisRuntimeError(
                "root required branch children are non-canonical"
            )
        _canonical_replica_list(
            diagnosis,
            gap.get("missing_required_signers"),
            "root missing required signers",
            nonempty=True,
        )
        previous_child = direct_child
    return accepted


def _live_root_qc(
    diagnosis: FrozenN31StaticDiagnosisProfile,
    arm: DiagnosticArm,
    events: Sequence[Mapping[str, Any]],
    *,
    block_hash: str,
    boundary_max_ns: int,
) -> Mapping[str, Any] | None:
    qcs: list[Mapping[str, Any]] = []
    for event in events:
        if event.get("event_type") not in {
            "aggregation.root_quorum_progress",
            "aggregation.root_qc_published",
        }:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise N31StaticDiagnosisRuntimeError("root progress payload is malformed")
        if (
            payload.get("epoch_number") != diagnosis.epoch_number
            or payload.get("tree_id") != diagnosis.phase.tree_id
            or payload.get("epoch_digest") != diagnosis.epoch_digest
            or payload.get("block_hash") != block_hash
            or payload.get("observer_replica") != diagnosis.witness_reporter_id
        ):
            continue
        if set(payload) != _ROOT_PAYLOAD_FIELDS:
            raise N31StaticDiagnosisRuntimeError("root progress payload schema drifted")
        if (
            event.get("source_kind") != "replica"
            or event.get("source_id") != f"replica-{diagnosis.witness_reporter_id}"
        ):
            raise N31StaticDiagnosisRuntimeError(
                "root progress source envelope drifted"
            )
        accepted = _validate_root_auxiliary_payload(diagnosis, payload)
        timestamp = event.get("source_monotonic_ns")
        if (
            type(payload.get("context_generation")) is not int
            or payload["context_generation"] <= 0
            or payload.get("wait_exempt_signers") != []
            or type(payload.get("root_signer_count")) is not int
            or payload["root_signer_count"] != len(accepted)
            or type(payload.get("global_quorum")) is not int
            or payload["global_quorum"] != diagnosis.quorum
            or payload.get("rejection_reason") is not None
            or type(timestamp) is not int
            or timestamp <= boundary_max_ns
        ):
            raise N31StaticDiagnosisRuntimeError(
                "same-context root progress payload drifted"
            )
        if arm.name == ARM_NAMES[1] and diagnosis.target_id in accepted:
            raise N31StaticDiagnosisRuntimeError(
                "leaf 5 reappeared in same-context root progress or QC"
            )
        if event.get("event_type") == "aggregation.root_qc_published":
            if len(accepted) < diagnosis.quorum:
                raise N31StaticDiagnosisRuntimeError("root QC does not preserve Q21")
            qcs.append(event)
    if not qcs:
        return None
    if len(qcs) != 1:
        raise N31StaticDiagnosisRuntimeError(
            "same-context root QC publication is duplicate"
        )
    accepted = qcs[0]["payload"]["accepted_signers"]
    if not set(arm.witness_signer_set) <= set(accepted):
        raise N31StaticDiagnosisRuntimeError(
            "root QC does not include the witnessed branch aggregate"
        )
    if (diagnosis.target_id in accepted) != (arm.name == ARM_NAMES[0]):
        raise N31StaticDiagnosisRuntimeError(
            "root QC leaf-5 membership disagrees with the selected arm"
        )
    return qcs[0]


def _live_signer_pair(
    diagnosis: FrozenN31StaticDiagnosisProfile,
    arm: DiagnosticArm,
    run_directory: Path,
    *,
    start_offset: int,
    manager_events: Sequence[Mapping[str, Any]],
    root_events: Sequence[Mapping[str, Any]],
    boundary_max_ns: int,
) -> tuple[dict[str, object], dict[str, Any], dict[str, Any]] | None:
    markers = _find_markers(
        diagnosis,
        arm,
        run_directory,
        start_offset=start_offset,
    )
    if len(markers) > diagnosis.byzantine_context_limit:
        raise N31StaticDiagnosisRuntimeError(
            "actor markers exceed the frozen proposal-context bound"
        )
    suppressions = _find_false_positive_suppressions(
        diagnosis,
        arm,
        run_directory,
        start_offset=start_offset,
    )
    exact_configuration = {
        "epoch_number": diagnosis.epoch_number,
        "tree_id": diagnosis.phase.tree_id,
        "epoch_digest": diagnosis.epoch_digest,
    }
    for marker in markers:
        marker_ns = int(marker["marker_monotonic_ns"])
        if marker_ns <= boundary_max_ns:
            raise N31StaticDiagnosisRuntimeError(
                "actor marker did not follow the fresh common tree-30 boundary"
            )
        block_hash = marker["block_hash"]
        if arm.name == ARM_NAMES[0]:
            exact_suppressions = [
                value for value in suppressions if value.get("block_hash") == block_hash
            ]
            if len(exact_suppressions) != 1:
                raise N31StaticDiagnosisRuntimeError(
                    "false-report proposal lacks one positive-suppression marker"
                )
            suppression = exact_suppressions[0]
            suppression_ns = int(suppression["marker_monotonic_ns"])
            if not boundary_max_ns < suppression_ns <= marker_ns:
                raise N31StaticDiagnosisRuntimeError(
                    "positive suppression is outside boundary-to-timeout causality"
                )
            marker["false_positive_suppression"] = suppression
        else:
            marker["false_positive_suppression"] = None
        context = [
            event
            for event in manager_events
            if (
                (observation := _observation(event)) is not None
                and observation.get("configuration") == exact_configuration
                and observation.get("block_hash") == block_hash
            )
        ]
        claims = [
            event
            for event in context
            if (
                (observation := _observation(event)) is not None
                and observation.get("reporter_id") == diagnosis.reporter_id
                and observation.get("observed_replica_id") == diagnosis.target_id
                and observation.get("expected_message_type")
                == diagnosis.claim_expected_message_type
            )
        ]
        witnesses = [
            event
            for event in context
            if (
                (observation := _observation(event)) is not None
                and observation.get("reporter_id") == diagnosis.witness_reporter_id
                and observation.get("observed_replica_id") == diagnosis.reporter_id
                and observation.get("expected_message_type")
                == diagnosis.witness_expected_message_type
            )
        ]
        if not claims or not witnesses:
            continue
        if len(claims) != 1 or len(witnesses) != 1:
            raise N31StaticDiagnosisRuntimeError(
                "same-context claim or root witness is duplicate"
            )
        claim_event, witness_event = claims[0], witnesses[0]
        claim = _observation(claim_event)
        witness = _observation(witness_event)
        assert claim is not None and witness is not None
        if claim.get("outcome") != "timeout" or claim.get("signer_set") != []:
            raise N31StaticDiagnosisRuntimeError(
                "same-context reporter claim is not the exact leaf timeout"
            )
        if witness.get("outcome") != "on_time" or witness.get("signer_set") != list(
            arm.witness_signer_set
        ):
            raise N31StaticDiagnosisRuntimeError(
                "same-context root aggregate signer set drifted"
            )
        if arm.name == ARM_NAMES[1] and any(
            (
                (observation := _observation(event)) is not None
                and observation.get("reporter_id") == diagnosis.witness_reporter_id
                and diagnosis.target_id in observation.get("signer_set", [])
            )
            for event in context
        ):
            raise N31StaticDiagnosisRuntimeError(
                "leaf 5 reappeared in root evidence for the omitted proposal"
            )
        root_qc = _live_root_qc(
            diagnosis,
            arm,
            root_events,
            block_hash=str(block_hash),
            boundary_max_ns=boundary_max_ns,
        )
        if root_qc is None:
            continue
        claim_reporter_ns = claim.get("reporter_monotonic_ns")
        witness_reporter_ns = witness.get("reporter_monotonic_ns")
        claim_manager_ns = claim_event.get("source_monotonic_ns")
        witness_manager_ns = witness_event.get("source_monotonic_ns")
        if any(
            type(value) is not int
            for value in (
                claim_reporter_ns,
                witness_reporter_ns,
                claim_manager_ns,
                witness_manager_ns,
            )
        ):
            raise N31StaticDiagnosisRuntimeError(
                "same-context observation clocks are malformed"
            )
        if not (
            boundary_max_ns < int(claim_reporter_ns) <= int(claim_manager_ns)
            and boundary_max_ns < int(witness_reporter_ns) <= int(witness_manager_ns)
        ):
            raise N31StaticDiagnosisRuntimeError(
                "same-context observations did not follow the common boundary"
            )
        root_qc_ns = root_qc.get("source_monotonic_ns")
        if type(root_qc_ns) is not int or int(witness_reporter_ns) > root_qc_ns:
            raise N31StaticDiagnosisRuntimeError(
                "root aggregate witness was timestamped after root QC publication"
            )
        if arm.name == ARM_NAMES[0]:
            if int(claim_reporter_ns) > marker_ns:
                raise N31StaticDiagnosisRuntimeError(
                    "false timeout marker preceded reporter timeout evidence"
                )
            if int(marker["false_positive_suppression"]["marker_monotonic_ns"]) > int(
                witness_reporter_ns
            ):
                raise N31StaticDiagnosisRuntimeError(
                    "positive suppression followed the honest root aggregate witness"
                )
        elif marker_ns > min(int(claim_reporter_ns), int(witness_reporter_ns)):
            raise N31StaticDiagnosisRuntimeError(
                "direct-vote omission marker followed timeout or aggregate evidence"
            )
        return marker, dict(claim_event), dict(witness_event)
    return None


def _commit_identity(event: Mapping[str, Any]) -> tuple[int, str, str, int] | None:
    if event.get("event_type") not in {"block.commit_observed", "block.committed"}:
        return None
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise N31StaticDiagnosisRuntimeError("commit payload is malformed")
    height = payload.get("block_height")
    block_hash = payload.get("block_hash")
    parent_hash = payload.get("parent_hash")
    transactions = payload.get("transaction_count")
    if (
        type(height) is not int
        or height <= 0
        or not isinstance(block_hash, str)
        or _HEX_256.fullmatch(block_hash) is None
        or not isinstance(parent_hash, str)
        or _HEX_256.fullmatch(parent_hash) is None
        or type(transactions) is not int
        or transactions < 0
    ):
        raise N31StaticDiagnosisRuntimeError("commit identity is malformed")
    return height, block_hash, parent_hash, transactions


def _find_later_ancestry_commit(
    diagnosis: FrozenN31StaticDiagnosisProfile,
    profiled: FrozenProfile,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    witnesses: Sequence[int],
    baseline: Mapping[str, object],
    after_ns: int,
) -> dict[str, object] | None:
    witness_maps: dict[int, dict[tuple[int, str, str, int], int]] = {}
    for replica_id in witnesses:
        values: dict[tuple[int, str, str, int], int] = {}
        for event in streams[f"replica-{replica_id}"]:
            identity = _commit_identity(event)
            if event.get("event_type") != "block.commit_observed" or identity is None:
                continue
            timestamp = event.get("source_monotonic_ns")
            if type(timestamp) is not int or timestamp <= 0:
                raise N31StaticDiagnosisRuntimeError(
                    "commit witness timestamp is malformed"
                )
            values[identity] = min(timestamp, values.get(identity, timestamp))
        witness_maps[replica_id] = values
    observer_chain: dict[str, tuple[tuple[int, str, str, int], int, int]] = {}
    for event in streams[f"replica-{profiled.authoritative_observer}"]:
        if event.get("event_type") != "block.commit_observed":
            continue
        identity = _commit_identity(event)
        assert identity is not None
        sequence = event.get("source_sequence")
        timestamp = event.get("source_monotonic_ns")
        if (
            type(sequence) is not int
            or sequence <= 0
            or type(timestamp) is not int
            or timestamp <= 0
        ):
            raise N31StaticDiagnosisRuntimeError(
                "replica-2 ancestry witness order is malformed"
            )
        observer_chain[identity[1]] = (identity, sequence, timestamp)
    common: dict[str, dict[str, object]] = {}
    for event in streams[f"replica-{profiled.authoritative_observer}"]:
        if event.get("event_type") != "block.committed":
            continue
        identity = _commit_identity(event)
        assert identity is not None
        witness_times = [witness_maps[replica].get(identity) for replica in witnesses]
        if any(value is None for value in witness_times):
            continue
        observer_ns = event.get("source_monotonic_ns")
        if type(observer_ns) is not int or observer_ns <= 0:
            raise N31StaticDiagnosisRuntimeError(
                "authoritative commit timestamp is malformed"
            )
        exact_times = [int(value) for value in witness_times if value is not None]
        common[identity[1]] = {
            "block_height": identity[0],
            "block_hash": identity[1],
            "parent_hash": identity[2],
            "transaction_count": identity[3],
            "observer_monotonic_ns": observer_ns,
            "common_monotonic_ns": max(observer_ns, *exact_times),
            "witnesses": list(witnesses),
        }
    baseline_hash = baseline.get("block_hash")
    baseline_height = baseline.get("block_height")
    if not isinstance(baseline_hash, str) or type(baseline_height) is not int:
        raise N31StaticDiagnosisRuntimeError("baseline commit identity is malformed")
    candidates: list[dict[str, object]] = []
    for candidate in common.values():
        if (
            int(candidate["common_monotonic_ns"]) <= after_ns
            or int(candidate["block_height"]) <= baseline_height
        ):
            continue
        current_hash = str(candidate["block_hash"])
        seen = {current_hash}
        chain: list[tuple[tuple[int, str, str, int], int, int]] = []
        while True:
            current = observer_chain.get(current_hash)
            if current is None:
                break
            identity, _sequence, _timestamp = current
            chain.append(current)
            current_height = identity[0]
            parent_hash = identity[2]
            if parent_hash == baseline_hash:
                if current_height == baseline_height + 1:
                    baseline_event = observer_chain.get(baseline_hash)
                    if baseline_event is None:
                        break
                    chain.append(baseline_event)
                    chain.reverse()
                    if any(
                        right[1] <= left[1] or right[2] < left[2]
                        for left, right in zip(chain, chain[1:])
                    ):
                        raise N31StaticDiagnosisRuntimeError(
                            "replica-2 ancestry order is non-monotonic"
                        )
                    candidates.append(candidate)
                break
            parent = observer_chain.get(parent_hash)
            if (
                parent is None
                or parent_hash in seen
                or parent[0][0] != current_height - 1
            ):
                break
            seen.add(parent_hash)
            current_hash = parent_hash
    if not candidates:
        return None
    return min(candidates, key=lambda value: int(value["common_monotonic_ns"]))


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise N31StaticDiagnosisRuntimeError(f"{label} is unreadable") from error
    if not isinstance(value, dict):
        raise N31StaticDiagnosisRuntimeError(f"{label} is not a JSON object")
    return value


def _validate_profile_binding(
    diagnosis: FrozenN31StaticDiagnosisProfile,
    profiled: FrozenProfile,
) -> None:
    if (
        profiled.profile_id != diagnosis.runtime_profile_id
        or profiled.profile_sha256 != diagnosis.runtime_profile_sha256
        or profiled.replica_ids != diagnosis.replica_ids
        or profiled.fault_threshold != diagnosis.fault_threshold
        or profiled.quorum != diagnosis.quorum
        or profiled.fanout != diagnosis.fanout
        or profiled.pipeline_depth != diagnosis.pipeline_stretch
        or profiled.tree_switch_period_blocks != diagnosis.tree_switch_period_blocks
    ):
        raise N31StaticDiagnosisRuntimeError(
            "preserved runtime profile differs from the diagnosis binding"
        )
    runtime.require_shipped_profile(profiled)
    _commit_witnesses(diagnosis, profiled)


def _raw_validation(
    run_directory: Path,
    diagnosis: FrozenN31StaticDiagnosisProfile,
    profiled: FrozenProfile,
) -> dict[str, object]:
    manifest = _read_json_object(run_directory / "manifest.json", "manifest")
    arm_name = manifest.get("arm")
    if not isinstance(arm_name, str):
        raise N31StaticDiagnosisRuntimeError("manifest arm is absent")
    arm = _selected_arm(diagnosis, arm_name)
    streams = runtime.event_streams(
        profiled,
        run_directory,
        include_manager=True,
        allow_partial=False,
    )
    try:
        fault_plan = (run_directory / "fault-plan.json").read_bytes()
        actor_log = _marker_path(run_directory, arm).read_bytes()
    except OSError as error:
        raise N31StaticDiagnosisRuntimeError(
            "preserved fault plan or actor log is unreadable"
        ) from error
    fault_journal = runtime.read_jsonl(
        run_directory / "raw" / "fault-orchestrator.jsonl",
        allow_partial=False,
    )
    try:
        return validate_n31_static_diagnosis_run(
            diagnosis,
            manifest=manifest,
            streams=streams,
            fault_plan=fault_plan,
            fault_journal=fault_journal,
            actor_log=actor_log,
        )
    except N31StaticDiagnosisError as error:
        raise N31StaticDiagnosisRuntimeError(
            f"preserved semantic evidence rejected: {error}"
        ) from error


def _verify_hashed_file(
    run_directory: Path,
    value: object,
    *,
    expected_keys: set[str],
    label: str,
) -> Path:
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise N31StaticDiagnosisRuntimeError(f"{label} schema drifted")
    relative = value.get("path")
    digest = value.get("sha256")
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or not isinstance(digest, str)
        or _HEX_256.fullmatch(digest) is None
    ):
        raise N31StaticDiagnosisRuntimeError(f"{label} identity drifted")
    path = run_directory / relative
    current = run_directory
    try:
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise N31StaticDiagnosisRuntimeError(f"{label} path contains a symlink")
    except OSError as error:
        raise N31StaticDiagnosisRuntimeError(
            f"{label} path cannot be inspected"
        ) from error
    if not path.is_file() or runtime.sha256_file(path) != digest:
        raise N31StaticDiagnosisRuntimeError(f"{label} bytes drifted")
    return path


def _expected_manager_launch(
    run_directory: Path,
    diagnosis: FrozenN31StaticDiagnosisProfile,
    profiled: FrozenProfile,
    manifest: Mapping[str, object],
    *,
    executable: str,
) -> list[str]:
    run_id = manifest.get("run_id")
    instances = manifest.get("source_instances")
    if not isinstance(run_id, str) or not isinstance(instances, Mapping):
        raise N31StaticDiagnosisRuntimeError(
            "manager launch provenance lacks run identity"
        )
    manager_instance = instances.get(MANAGER_SOURCE_ID)
    if not isinstance(manager_instance, str):
        raise N31StaticDiagnosisRuntimeError(
            "manager launch provenance lacks source instance"
        )
    request = runtime.transition_request()
    expected = [
        executable,
        "--listen",
        f"127.0.0.1:{profiled.manager_port}",
        "--tls-privkey",
        "<redacted>",
        "--tls-cert",
        "<fingerprinted>",
        "--issuer-id",
        str(runtime.ISSUER_ID),
        "--issuer-private-key",
        "<redacted>",
        "--activation-delay-blocks",
        str(profiled.activation_delay_blocks),
        "--convergence-deadline-seconds",
        "120",
        "--tree-fanout",
        str(diagnosis.fanout),
        "--pipeline-stretch",
        str(diagnosis.pipeline_stretch),
        "--transition-request",
        json.dumps(request, sort_keys=True, separators=(",", ":")),
        "--bundle-output",
        str(run_directory / str(request["bundle_path"])),
        "--structured-event-run-id",
        run_id,
        "--structured-event-source-instance",
        manager_instance,
        "--structured-event-output",
        str(run_directory / "raw" / "adaptive-manager.jsonl"),
    ]
    for replica in diagnosis.replica_ids:
        expected.extend(
            (
                "--replica",
                f"{replica},127.0.0.1:{profiled.peer_base + replica},"
                "<fingerprinted>",
            )
        )
    return expected


def _verify_provenance(
    run_directory: Path,
    diagnosis: FrozenN31StaticDiagnosisProfile,
    profiled: FrozenProfile,
    manifest: Mapping[str, object],
    trusted: TrustedProvenance,
) -> None:
    if type(trusted) is not TrustedProvenance:
        raise N31StaticDiagnosisRuntimeError(
            "validation requires an external TrustedProvenance receipt"
        )
    preflight_record = manifest.get("preflight")
    if not isinstance(preflight_record, Mapping):
        raise N31StaticDiagnosisRuntimeError("manifest preflight is absent")
    if (
        preflight_record.get("schema_version") != 1
        or preflight_record.get("scenario") != SCENARIO
        or preflight_record.get("verdict") != "PASS"
        or manifest.get("kauri_revision") != trusted.revision
        or preflight_record.get("revision") != trusted.revision
        or preflight_record.get("diagnosis_profile")
        != {
            "profile_id": diagnosis.profile_id,
            "sha256": diagnosis.profile_sha256,
        }
    ):
        raise N31StaticDiagnosisRuntimeError(
            "preserved preflight revision or diagnosis profile drifted"
        )
    runtime_record = preflight_record.get("runtime_profile")
    if not isinstance(runtime_record, Mapping) or {
        "profile_id": runtime_record.get("profile_id"),
        "sha256": runtime_record.get("sha256"),
    } != {
        "profile_id": profiled.profile_id,
        "sha256": profiled.profile_sha256,
    }:
        raise N31StaticDiagnosisRuntimeError(
            "preserved preflight runtime profile drifted"
        )
    profiled_record = preflight_record.get("profiled_runtime")
    if not isinstance(profiled_record, Mapping):
        raise N31StaticDiagnosisRuntimeError(
            "preserved profiled-runtime preflight is absent"
        )
    provenance = profiled_record.get("build_provenance")
    witness = profiled_record.get("epoch_zero_witness")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("revision") != trusted.revision
        or provenance.get("repository") != trusted.repository
        or provenance.get("build_directory") != trusted.build_directory
        or profiled_record.get("revision") != trusted.revision
        or not isinstance(witness, Mapping)
        or _canonical_document_sha256(provenance)
        != trusted.build_provenance_document_sha256
    ):
        raise N31StaticDiagnosisRuntimeError(
            "preserved build provenance or epoch witness is malformed"
        )
    copied_provenance_path = run_directory / "runtime" / "build-provenance.json"
    if runtime.sha256_file(
        copied_provenance_path
    ) != trusted.build_provenance_file_sha256 or _read_json_object(
        copied_provenance_path,
        "copied build provenance",
    ) != dict(
        provenance
    ):
        raise N31StaticDiagnosisRuntimeError("copied build provenance drifted")
    if _read_json_object(
        run_directory / "runtime" / "epoch-zero-witness.json",
        "copied epoch witness",
    ) != dict(witness):
        raise N31StaticDiagnosisRuntimeError("copied epoch witness drifted")

    arm_name = manifest.get("arm")
    launch_contracts = preflight_record.get("launch_contracts")
    if not isinstance(arm_name, str) or not isinstance(launch_contracts, Mapping):
        raise N31StaticDiagnosisRuntimeError("preflight launch contracts are absent")
    expected_contracts = {
        name: build_launch_contract(
            diagnosis,
            arm=name,
            kauri_revision=trusted.revision,
        )
        for name in ARM_NAMES
    }
    if dict(launch_contracts) != expected_contracts:
        raise N31StaticDiagnosisRuntimeError(
            "preflight launch contracts differ from the frozen profile"
        )
    selected_contract = launch_contracts.get(arm_name)
    if not isinstance(selected_contract, Mapping) or _read_json_object(
        run_directory / "runtime" / "launch-contract.json",
        "selected launch contract",
    ) != dict(selected_contract):
        raise N31StaticDiagnosisRuntimeError("selected launch contract drifted")
    launch = _read_json_object(
        run_directory / "runtime" / "launch-arguments.json",
        "launch arguments",
    )
    replicas = launch.get("replicas")
    manager = launch.get("manager")
    overlays = selected_contract.get("replica_overlays")
    manager_overlay = selected_contract.get("manager_overlay")
    if (
        set(launch) != {"schema_version", "manager", "replicas"}
        or launch.get("schema_version") != 1
        or not isinstance(manager, list)
        or not isinstance(replicas, list)
        or len(replicas) != len(diagnosis.replica_ids)
        or not isinstance(overlays, list)
        or len(overlays) != len(diagnosis.replica_ids)
        or manager_overlay != []
    ):
        raise N31StaticDiagnosisRuntimeError("launch argument schema drifted")
    executables = profiled_record.get("executables")
    expected_executables = {
        binary.name: {"path": binary.path, "sha256": binary.sha256}
        for binary in trusted.binaries
    }
    if (
        not isinstance(executables, Mapping)
        or dict(executables) != expected_executables
    ):
        raise N31StaticDiagnosisRuntimeError(
            "archived executable paths or hashes differ from trusted provenance"
        )
    manager_path = trusted.binary("manager").path
    app_path = trusted.binary("app").path
    if manager != _expected_manager_launch(
        run_directory,
        diagnosis,
        profiled,
        manifest,
        executable=manager_path,
    ):
        raise N31StaticDiagnosisRuntimeError(
            "actual manager command differs from frozen provenance"
        )
    base_replica_commands = runtime.replica_argvs(
        profiled,
        app_binary=Path(app_path),
        config_directory=run_directory / "config",
    )
    active: list[int] = []
    fault_prefixes = (
        "--experiment-byzantine",
        "--experiment-omission-additional",
        "--experiment-false-report",
        "--experiment-omit-outbound",
    )
    for replica_id, command, overlay in zip(
        diagnosis.replica_ids, replicas, overlays, strict=True
    ):
        if (
            not isinstance(command, list)
            or any(not isinstance(argument, str) for argument in command)
            or not isinstance(overlay, Mapping)
            or overlay.get("replica_id") != replica_id
            or not isinstance(overlay.get("argv"), list)
        ):
            raise N31StaticDiagnosisRuntimeError("replica launch overlay drifted")
        arguments = overlay["argv"]
        expected_command = [*base_replica_commands[replica_id], *arguments]
        if command != expected_command:
            raise N31StaticDiagnosisRuntimeError(
                "actual replica command differs from frozen provenance"
            )
        base_command = command[: -len(arguments)] if arguments else command
        if any(argument.startswith(fault_prefixes) for argument in base_command):
            raise N31StaticDiagnosisRuntimeError(
                "non-actor or unbound fault control appeared in launch arguments"
            )
        if arguments:
            active.append(replica_id)
            if len(command) < len(arguments) or command[-len(arguments) :] != arguments:
                raise N31StaticDiagnosisRuntimeError(
                    "actor overlay is absent from the actual launch command"
                )
    arm = _selected_arm(diagnosis, arm_name)
    if active != [arm.actor_replica_id]:
        raise N31StaticDiagnosisRuntimeError(
            "actual replica commands do not contain one actor-only overlay"
        )


def _verify_inventory(
    run_directory: Path,
    diagnosis: FrozenN31StaticDiagnosisProfile,
    manifest: Mapping[str, object],
) -> None:
    artifacts = manifest.get("runtime_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise N31StaticDiagnosisRuntimeError("runtime artifact inventory is absent")
    expected_artifacts: dict[str, tuple[str, int | None]] = {
        "profile.json": ("diagnosis_profile", None),
        "runtime-profile.json": ("runtime_profile", None),
        "config/bls-identities.txt": ("bls_identity_input", None),
        "config/tls-identities.txt": ("tls_identity_input", None),
        "config/issuer-identities.txt": ("issuer_identity_input", None),
        "config/main.conf": ("main_config", None),
        **{
            f"config/replica-{replica}.conf": ("replica_config", replica)
            for replica in diagnosis.replica_ids
        },
        "runtime/initial-epoch.json": ("initial_epoch", None),
        "runtime/effective-runtime.json": ("effective_runtime", None),
        "runtime/transition-request.json": ("transition_request", None),
        "runtime/launch-arguments.json": ("launch_arguments", None),
        "runtime/build-provenance.json": ("build_provenance", None),
        "runtime/epoch-zero-witness.json": ("epoch_zero_witness", None),
        "runtime/launch-contract.json": ("launch_contract", None),
    }
    artifact_paths: set[str] = set()
    for index, item in enumerate(artifacts):
        path = _verify_hashed_file(
            run_directory,
            item,
            expected_keys={"kind", "replica_id", "path", "sha256"},
            label=f"runtime artifact {index}",
        )
        relative = path.relative_to(run_directory).as_posix()
        if relative in artifact_paths:
            raise N31StaticDiagnosisRuntimeError("runtime artifact paths are duplicate")
        artifact_paths.add(relative)
        expected = expected_artifacts.get(relative)
        if expected is None or (item.get("kind"), item.get("replica_id")) != expected:
            raise N31StaticDiagnosisRuntimeError(
                f"runtime artifact kind or replica drifted: {relative}"
            )
    if artifact_paths != set(expected_artifacts):
        raise N31StaticDiagnosisRuntimeError(
            "runtime artifact inventory is not the exact immutable input set"
        )

    sources = manifest.get("sources")
    expected_sources = [
        *(f"replica-{replica}" for replica in diagnosis.replica_ids),
        MANAGER_SOURCE_ID,
    ]
    if not isinstance(sources, list) or len(sources) != len(expected_sources):
        raise N31StaticDiagnosisRuntimeError("source descriptor inventory drifted")
    instances = manifest.get("source_instances")
    cleanup = manifest.get("cleanup_ledger")
    if not isinstance(instances, Mapping) or not isinstance(cleanup, list):
        raise N31StaticDiagnosisRuntimeError(
            "source instances or cleanup ledger are absent"
        )
    if set(instances) != set(expected_sources):
        raise N31StaticDiagnosisRuntimeError(
            "source instance inventory is not exact N31 plus manager"
        )
    if len(cleanup) != len(expected_sources) or any(
        not isinstance(item, Mapping) for item in cleanup
    ):
        raise N31StaticDiagnosisRuntimeError(
            "cleanup inventory is not the exact owned process set"
        )
    cleanup_names = [item.get("name") for item in cleanup]
    if any(not isinstance(name, str) for name in cleanup_names):
        raise N31StaticDiagnosisRuntimeError(
            "cleanup inventory contains a malformed process name"
        )
    cleanup_name_set = set(cleanup_names)
    if len(cleanup_name_set) != len(cleanup_names) or cleanup_name_set != set(
        expected_sources
    ):
        raise N31StaticDiagnosisRuntimeError(
            "cleanup inventory contains duplicate, missing, or extra processes"
        )
    cleanup_by_name = {item.get("name"): item for item in cleanup}
    for source, item in zip(expected_sources, sources, strict=True):
        path = _verify_hashed_file(
            run_directory,
            item,
            expected_keys={
                "source_kind",
                "source_id",
                "source_instance",
                "pid",
                "pgid",
                "path",
                "sha256",
            },
            label=f"source descriptor {source}",
        )
        expected_kind = (
            "adaptation_manager" if source == MANAGER_SOURCE_ID else "replica"
        )
        cleanup_entry = cleanup_by_name.get(source)
        if (
            item.get("source_id") != source
            or item.get("source_kind") != expected_kind
            or item.get("source_instance") != instances.get(source)
            or path.relative_to(run_directory).as_posix() != f"raw/{source}.jsonl"
            or not isinstance(cleanup_entry, Mapping)
            or item.get("pid") != cleanup_entry.get("pid")
            or item.get("pgid") != cleanup_entry.get("pgid")
        ):
            raise N31StaticDiagnosisRuntimeError(
                "source descriptor PID/PGID differs from cleanup ledger or "
                "source identity drifted"
            )

    actor = _selected_arm(diagnosis, str(manifest.get("arm")))
    actor_path = _verify_hashed_file(
        run_directory,
        manifest.get("actor_log"),
        expected_keys={"path", "sha256"},
        label="actor log",
    )
    if actor_path != _marker_path(run_directory, actor):
        raise N31StaticDiagnosisRuntimeError("actor log path drifted")


def validate_preserved_run(
    run_directory: Path,
    *,
    trusted_provenance: TrustedProvenance,
) -> dict[str, object]:
    """Reconstruct PASS source-blind, anchored by an external build receipt.

    ``source-blind`` means that the recorded verdict is not trusted. It does
    not mean provenance-anchor-free: revision and executable authenticity
    come only from ``trusted_provenance``, never from the mutable archive.
    """

    run_directory = run_directory.resolve()
    try:
        seal = verify_evidence_seal(run_directory)
    except EvidenceSealError as error:
        raise N31StaticDiagnosisRuntimeError(
            f"evidence seal rejected: {error}"
        ) from error
    diagnosis = load_frozen_profile(run_directory / "profile.json")
    profiled = load_runtime_profile(run_directory / "runtime-profile.json")
    _validate_profile_binding(diagnosis, profiled)
    manifest = _read_json_object(run_directory / "manifest.json", "manifest")
    if manifest.get("run_id") != run_directory.name:
        raise N31StaticDiagnosisRuntimeError(
            "manifest run identity differs from its directory"
        )
    _verify_inventory(run_directory, diagnosis, manifest)
    _verify_provenance(
        run_directory,
        diagnosis,
        profiled,
        manifest,
        trusted_provenance,
    )
    result = _raw_validation(run_directory, diagnosis, profiled)
    recorded = _read_json_object(
        run_directory / "validation.json", "recorded validation"
    )
    if recorded != result or result.get("verdict") != "PASS":
        raise N31StaticDiagnosisRuntimeError(
            "recorded validation differs from independently reconstructed evidence"
        )
    return {
        **result,
        "run_directory": str(run_directory),
        "trusted_provenance_sha256": trusted_provenance.sha256,
        "evidence_tree_sha256": seal.tree_sha256,
        "evidence_seal_sha256": seal.seal_sha256,
    }


def run_once(
    *,
    diagnosis_profile_path: Path,
    arm: str,
    trusted_provenance: TrustedProvenance,
    repository: Path,
    results_root: Path,
    app_binary: Path,
    manager_binary: Path,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    epoch_profile_digest_binary: Path,
    build_directory: Path,
    build_provenance_path: Path,
) -> tuple[Path, str]:
    """Execute and seal one no-retry signer-aware harness pilot."""

    if type(trusted_provenance) is not TrustedProvenance:
        raise N31StaticDiagnosisRuntimeError(
            "run requires an external TrustedProvenance receipt"
        )
    diagnosis_profile_path = diagnosis_profile_path.resolve()
    repository = repository.resolve()
    diagnosis, profiled, runtime_profile_path = _load_bound_profiles(
        diagnosis_profile_path, repository
    )
    selected = _selected_arm(diagnosis, arm)
    witnesses = _commit_witnesses(diagnosis, profiled)
    preflight_result = preflight(
        diagnosis_profile_path=diagnosis_profile_path,
        repository=repository,
        app_binary=app_binary,
        manager_binary=manager_binary,
        keygen_binary=keygen_binary,
        tls_keygen_binary=tls_keygen_binary,
        epoch_profile_digest_binary=epoch_profile_digest_binary,
        build_directory=build_directory,
        build_provenance_path=build_provenance_path,
    )
    revision = str(preflight_result["revision"])
    trusted_paths = {
        "app": str(app_binary.resolve()),
        "manager": str(manager_binary.resolve()),
        "keygen": str(keygen_binary.resolve()),
        "tls_keygen": str(tls_keygen_binary.resolve()),
        "epoch_profile_digest": str(epoch_profile_digest_binary.resolve()),
    }
    profiled_preflight = preflight_result.get("profiled_runtime")
    trusted_executables = {
        binary.name: {"path": binary.path, "sha256": binary.sha256}
        for binary in trusted_provenance.binaries
    }
    live_provenance = (
        profiled_preflight.get("build_provenance")
        if isinstance(profiled_preflight, Mapping)
        else None
    )
    if (
        trusted_provenance.revision != revision
        or trusted_provenance.repository != str(repository)
        or trusted_provenance.build_directory != str(build_directory.resolve())
        or {name: trusted_provenance.binary(name).path for name in trusted_paths}
        != trusted_paths
        or not isinstance(profiled_preflight, Mapping)
        or profiled_preflight.get("executables") != trusted_executables
        or not isinstance(live_provenance, Mapping)
        or _canonical_document_sha256(live_provenance)
        != trusted_provenance.build_provenance_document_sha256
        or runtime.sha256_file(build_provenance_path.resolve())
        != trusted_provenance.build_provenance_file_sha256
    ):
        raise N31StaticDiagnosisRuntimeError(
            "live preflight or launch inputs differ from trusted provenance"
        )
    contracts = preflight_result.get("launch_contracts")
    if not isinstance(contracts, Mapping) or not isinstance(
        contracts.get(arm), Mapping
    ):
        raise N31StaticDiagnosisRuntimeError(
            "preflight omitted the selected launch contract"
        )
    launch_contract = dict(contracts[arm])
    overlay_values = launch_contract.get("replica_overlays")
    if not isinstance(overlay_values, list):
        raise N31StaticDiagnosisRuntimeError("replica overlays are absent")
    replica_overlays: dict[int, tuple[str, ...]] = {}
    for value in overlay_values:
        if not isinstance(value, Mapping):
            raise N31StaticDiagnosisRuntimeError("replica overlay is malformed")
        replica_id = value.get("replica_id")
        arguments = value.get("argv")
        if (
            type(replica_id) is not int
            or not isinstance(arguments, list)
            or any(not isinstance(argument, str) for argument in arguments)
        ):
            raise N31StaticDiagnosisRuntimeError("replica overlay is malformed")
        replica_overlays[replica_id] = tuple(arguments)
    if set(replica_overlays) != set(diagnosis.replica_ids):
        raise N31StaticDiagnosisRuntimeError(
            "replica overlay membership is not exact N31"
        )

    run_directory = runtime.create_run_directory(results_root.resolve())
    run_id = run_directory.name
    started_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    hard_deadline_ns = runtime.monotonic_raw_ns() + int(
        profiled.hard_timeout_s * 1_000_000_000
    )
    state_path = run_directory / "runner-state.json"
    state: dict[str, object] = {
        "schema_version": 1,
        "scenario": SCENARIO,
        "run_id": run_id,
        "arm": arm,
        "revision": revision,
        "phase": "fault_evidence",
        "runtime_error": None,
        "started_utc": started_utc,
        "evidence_ceiling": diagnosis.pilot_ceiling,
    }
    runtime.write_json_exclusive(state_path, state)

    source_instances = {
        f"replica-{replica}": f"{run_id}-replica-{replica}-{uuid.uuid4().hex}"
        for replica in diagnosis.replica_ids
    }
    source_instances[MANAGER_SOURCE_ID] = f"{run_id}-manager-{uuid.uuid4().hex}"
    plan = build_fault_plan(diagnosis, selected)
    resources = ExitStack()
    registry = ProcessRegistry(monotonic_ns=runtime.monotonic_raw_ns)
    lifecycle: FaultLifecycle | None = None
    records: list[ProcessRecord] = []
    log_handles: list[IO[bytes]] = []
    runtime_artifacts: list[dict[str, object]] = []
    cleanup_ledger: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    diagnostic_boundary: dict[str, object] | None = None
    baseline_commit: dict[str, object] | None = None
    later_commit: dict[str, object] | None = None
    selected_marker: dict[str, object] | None = None
    claim_manager: dict[str, Any] | None = None
    witness_manager: dict[str, Any] | None = None
    log_start_offset: int | None = None
    log_terminal_offset: int | None = None
    runtime_error: str | None = None
    cleanup_error: str | None = None
    validation_error: str | None = None
    interrupted = False
    previous_handlers: dict[int, Any] = {}

    def update_state(phase: str, **extra: object) -> None:
        state["phase"] = phase
        state.update(extra)
        runtime.replace_json(state_path, state)

    def request_shutdown(signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        raise KeyboardInterrupt(signal.Signals(signum).name)

    try:
        previous_handlers = {
            signum: signal.signal(signum, request_shutdown)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        lifecycle = resources.enter_context(
            FaultEvidence(
                run_directory,
                plan,
                monotonic_ns=runtime.monotonic_raw_ns,
            )
        )
        update_state("identity_generation")
        bls, tls, issuer = runtime.generate_identities(
            profiled,
            keygen_binary=keygen_binary.resolve(),
            tls_keygen_binary=tls_keygen_binary.resolve(),
            config_directory=run_directory / "config",
        )
        manager_command, replica_commands, generated_artifacts = (
            runtime.write_runtime_inputs(
                profiled,
                run_directory=run_directory,
                app_binary=app_binary.resolve(),
                manager_binary=manager_binary.resolve(),
                bls=bls,
                tls=tls,
                issuer=issuer,
                run_id=run_id,
                source_instances=source_instances,
                replica_overlays=replica_overlays,
            )
        )
        runtime_artifacts.extend(generated_artifacts)
        profile_copy = run_directory / "profile.json"
        runtime_profile_copy = run_directory / "runtime-profile.json"
        runtime.write_exclusive(profile_copy, diagnosis_profile_path.read_bytes())
        runtime.write_exclusive(runtime_profile_copy, runtime_profile_path.read_bytes())
        profiled_preflight = preflight_result.get("profiled_runtime")
        if not isinstance(profiled_preflight, Mapping):
            raise N31StaticDiagnosisRuntimeError("profiled-runtime preflight is absent")
        provenance = profiled_preflight.get("build_provenance")
        epoch_witness = profiled_preflight.get("epoch_zero_witness")
        if not isinstance(provenance, Mapping) or not isinstance(
            epoch_witness, Mapping
        ):
            raise N31StaticDiagnosisRuntimeError(
                "preflight provenance or epoch witness is absent"
            )
        provenance_copy = run_directory / "runtime" / "build-provenance.json"
        witness_copy = run_directory / "runtime" / "epoch-zero-witness.json"
        contract_copy = run_directory / "runtime" / "launch-contract.json"
        runtime.write_json_exclusive(provenance_copy, dict(provenance))
        runtime.write_json_exclusive(witness_copy, dict(epoch_witness))
        runtime.write_json_exclusive(contract_copy, launch_contract)
        for kind, replica_id, path in (
            ("diagnosis_profile", None, profile_copy),
            ("runtime_profile", None, runtime_profile_copy),
            ("build_provenance", None, provenance_copy),
            ("epoch_zero_witness", None, witness_copy),
            ("launch_contract", None, contract_copy),
        ):
            runtime_artifacts.append(
                {
                    "kind": kind,
                    "replica_id": replica_id,
                    "path": path.relative_to(run_directory).as_posix(),
                    "sha256": runtime.sha256_file(path),
                }
            )
        runtime_artifacts.sort(key=lambda item: str(item["path"]))

        update_state("launch")
        manager_record, manager_log = runtime.spawn_owned_process(
            registry,
            name=MANAGER_SOURCE_ID,
            replica_id=-1,
            command=manager_command,
            log_path=run_directory / "logs" / "adaptive-manager.log",
            working_directory=run_directory,
        )
        records.append(manager_record)
        log_handles.append(manager_log)
        for replica_id in diagnosis.replica_ids:
            record, log = runtime.spawn_owned_process(
                registry,
                name=f"replica-{replica_id}",
                replica_id=replica_id,
                command=replica_commands[replica_id],
                log_path=run_directory / "logs" / f"replica-{replica_id}.log",
                working_directory=run_directory,
            )
            records.append(record)
            log_handles.append(log)

        def all_ready() -> int | None:
            streams = runtime.event_streams(
                profiled,
                run_directory,
                include_manager=True,
                allow_partial=True,
            )
            ready: list[int] = []
            if len(streams) != 32:
                return None
            for events in streams.values():
                matches = [
                    event
                    for event in events
                    if event.get("event_type") == "process.ready"
                ]
                if len(matches) != 1:
                    return None
                timestamp = matches[0].get("source_monotonic_ns")
                if type(timestamp) is not int or timestamp <= 0:
                    raise N31StaticDiagnosisRuntimeError(
                        "process.ready timestamp is malformed"
                    )
                ready.append(timestamp)
            return max(ready)

        ready_barrier_ns = int(
            runtime.wait_until(
                "all 32 exact process.ready events",
                all_ready,
                phase_timeout_s=profiled.startup_timeout_s,
                hard_deadline_ns=hard_deadline_ns,
                records=records,
                crashed_replica=None,
            )
        )
        update_state("baseline", ready_barrier_ns=ready_barrier_ns)

        def baseline() -> dict[str, object] | None:
            streams = runtime.event_streams(profiled, run_directory, allow_partial=True)
            return runtime.find_common_commit(
                profiled,
                streams,
                witnesses=witnesses,
                after_ns=ready_barrier_ns + 1,
            )

        baseline_commit = dict(
            runtime.wait_until(
                "clean fixed-Q21 baseline common commit",
                baseline,
                phase_timeout_s=profiled.startup_timeout_s,
                hard_deadline_ns=hard_deadline_ns,
                records=records,
                crashed_replica=None,
            )
        )
        if lifecycle is None:
            raise N31StaticDiagnosisRuntimeError("fault lifecycle is absent")
        lifecycle.start(selected.fault_id)
        log_start_offset = _log_cursor(run_directory, selected)
        _reject_earlier_marker(run_directory, selected, log_start_offset)
        watermarks, offsets = runtime.event_tail_snapshot(profiled, run_directory)
        update_state(
            "awaiting_tree30",
            clean_baseline_common_commit=baseline_commit,
            log_start_offset=log_start_offset,
        )
        boundary_poller = runtime.ConfigurationBoundaryPoller(
            profiled,
            run_directory,
            watermarks=watermarks,
            offsets=offsets,
            target_tree_id=diagnosis.phase.tree_id,
        )
        diagnostic_boundary = dict(
            runtime.wait_until(
                "fresh common epoch-zero tree-30 boundary",
                boundary_poller.poll,
                phase_timeout_s=profiled.startup_timeout_s,
                hard_deadline_ns=hard_deadline_ns,
                records=records,
                crashed_replica=None,
                poll_interval_s=0.01,
            )
        )
        references = diagnostic_boundary.get("replica_evidence")
        if not isinstance(references, list) or len(references) != 31:
            raise N31StaticDiagnosisRuntimeError(
                "tree-30 boundary lacks exact N31 replica references"
            )
        boundary_timestamps = [
            int(reference["source_monotonic_ns"])
            for reference in references
            if isinstance(reference, Mapping)
        ]
        if len(boundary_timestamps) != 31:
            raise N31StaticDiagnosisRuntimeError(
                "tree-30 boundary references are malformed"
            )
        boundary_min_ns = min(boundary_timestamps)
        boundary_max_ns = max(boundary_timestamps)
        if boundary_max_ns - boundary_min_ns > diagnosis.boundary_max_skew_ns:
            raise N31StaticDiagnosisRuntimeError(
                "common tree-30 boundary exceeds the frozen 0.5-second skew"
            )
        update_state(
            "awaiting_signer_pair",
            diagnostic_boundary=diagnostic_boundary,
        )

        def signer_pair() -> (
            tuple[dict[str, object], dict[str, Any], dict[str, Any]] | None
        ):
            live_streams = runtime.event_streams(
                profiled,
                run_directory,
                include_manager=True,
                allow_partial=True,
            )
            return _live_signer_pair(
                diagnosis,
                selected,
                run_directory,
                start_offset=log_start_offset,
                manager_events=live_streams[MANAGER_SOURCE_ID],
                root_events=live_streams[f"replica-{diagnosis.witness_reporter_id}"],
                boundary_max_ns=boundary_max_ns,
            )

        selected_marker, claim_manager, witness_manager = runtime.wait_until(
            "same-proposal timeout and signer-aware root aggregate",
            signer_pair,
            phase_timeout_s=profiled.startup_timeout_s,
            hard_deadline_ns=hard_deadline_ns,
            records=records,
            crashed_replica=None,
            poll_interval_s=0.01,
        )
        pair_observed_ns = runtime.monotonic_raw_ns()
        claim_observation = _observation(claim_manager)
        witness_observation = _observation(witness_manager)
        if claim_observation is None or witness_observation is None:
            raise N31StaticDiagnosisRuntimeError("same-proposal pair is malformed")
        response_only, signer_aware = build_signer_aware_certificates(
            diagnosis,
            selected,
            {"observation": claim_observation},
            {"observation": witness_observation},
        )
        if (
            response_only.get("compatible_hypothesis_count") != 2
            or signer_aware.get("compatible_hypothesis_count") != 1
        ):
            raise N31StaticDiagnosisRuntimeError(
                "live signer-aware classifier did not narrow B2 to B1"
            )
        selected_suppression = selected_marker["false_positive_suppression"]
        log_terminal_offset = _log_cursor(run_directory, selected)
        terminal_markers = _find_markers(
            diagnosis,
            selected,
            run_directory,
            start_offset=log_start_offset,
            end_offset=log_terminal_offset,
        )
        exact_markers = [
            marker
            for marker in terminal_markers
            if marker.get("block_hash") == selected_marker.get("block_hash")
        ]
        if len(exact_markers) != 1:
            raise N31StaticDiagnosisRuntimeError(
                "selected tree-30 marker changed before lifecycle terminal"
            )
        selected_marker = exact_markers[0]
        terminal_suppressions = _find_false_positive_suppressions(
            diagnosis,
            selected,
            run_directory,
            start_offset=log_start_offset,
            end_offset=log_terminal_offset,
        )
        if selected.name == ARM_NAMES[0]:
            exact_suppressions = [
                value
                for value in terminal_suppressions
                if value.get("block_hash") == selected_marker.get("block_hash")
            ]
            if len(exact_suppressions) != 1 or exact_suppressions[0] != (
                selected_suppression
            ):
                raise N31StaticDiagnosisRuntimeError(
                    "selected positive suppression changed before terminal"
                )
        selected_marker["false_positive_suppression"] = selected_suppression
        lifecycle.terminal(
            selected.fault_id,
            "succeeded",
            {
                "fault_id": selected.fault_id,
                **selected_marker,
                "context_limit": diagnosis.byzantine_context_limit,
                "log_terminal_offset": log_terminal_offset,
                "pair_observed_monotonic_ns": pair_observed_ns,
                "diagnostic_certificate_sha256": signer_aware["certificate_sha256"],
            },
        )
        claim_receipt = claim_manager.get("source_monotonic_ns")
        witness_receipt = witness_manager.get("source_monotonic_ns")
        if type(claim_receipt) is not int or type(witness_receipt) is not int:
            raise N31StaticDiagnosisRuntimeError(
                "same-proposal manager receipt clocks are malformed"
            )
        settled_ns = max(claim_receipt, witness_receipt)
        update_state(
            "awaiting_later_ancestry_commit",
            selected_block_hash=selected_marker["block_hash"],
            claim_manager_receipt_ns=claim_receipt,
            witness_manager_receipt_ns=witness_receipt,
            settled_monotonic_ns=settled_ns,
            settlement_latency_ns=settled_ns - boundary_max_ns,
            diagnostic_certificate_sha256=signer_aware["certificate_sha256"],
            log_terminal_offset=log_terminal_offset,
        )

        def later() -> dict[str, object] | None:
            streams = runtime.event_streams(profiled, run_directory, allow_partial=True)
            return _find_later_ancestry_commit(
                diagnosis,
                profiled,
                streams,
                witnesses=witnesses,
                baseline=baseline_commit,
                after_ns=settled_ns,
            )

        later_commit = dict(
            runtime.wait_until(
                "later fixed-Q21 commit with preserved baseline ancestry",
                later,
                phase_timeout_s=profiled.startup_timeout_s,
                hard_deadline_ns=hard_deadline_ns,
                records=records,
                crashed_replica=None,
            )
        )
        update_state("qualified_pending_cleanup", later_common_commit=later_commit)
    except KeyboardInterrupt as error:
        runtime_error = f"interrupted: {error}"
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
        ProfiledFaultEvaluationError,
        N31StaticDiagnosisError,
        N31StaticDiagnosisRuntimeError,
        runtime.ProfiledFaultRuntimeError,
    ) as error:
        runtime_error = str(error)
    finally:
        cleanup_messages: list[str] = []
        try:
            if records:
                post_end_ns = (
                    int(later_commit["common_monotonic_ns"])
                    if later_commit is not None
                    else None
                )
                cleanup_ledger, _cleanup_started_ns = runtime.concurrent_cleanup(
                    records,
                    faulted_replica_id=None,
                    post_end_ns=post_end_ns,
                )
                for entry in cleanup_ledger:
                    errors = entry.get("cleanup_errors")
                    if isinstance(errors, list):
                        cleanup_messages.extend(str(error) for error in errors)
                    if entry.get("classification") == "unexpected_exit":
                        cleanup_messages.append(
                            f"unexpected cleanup exit: {entry.get('name')}"
                        )
        except (OSError, runtime.ProfiledFaultRuntimeError) as error:
            cleanup_messages.append(str(error))
        try:
            resources.close()
        except (OSError, RuntimeError, ValueError) as error:
            cleanup_messages.append(f"fault evidence close failed: {error}")
        for log in log_handles:
            try:
                log.close()
            except OSError as error:
                cleanup_messages.append(f"process log close failed: {error}")
        try:
            runtime.wait_ports_clear(
                tuple(
                    [profiled.manager_port]
                    + [profiled.peer_base + replica for replica in profiled.replica_ids]
                    + [
                        profiled.client_base + replica
                        for replica in profiled.replica_ids
                    ]
                )
            )
        except runtime.ProfiledFaultRuntimeError as error:
            cleanup_messages.append(str(error))
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, RuntimeError, ValueError) as error:
                cleanup_messages.append(
                    f"could not restore signal handler {signum}: {error}"
                )
        if cleanup_messages:
            cleanup_error = "; ".join(dict.fromkeys(cleanup_messages))
            runtime_error = runtime_error or cleanup_error

    finished_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    if records:
        try:
            sources = runtime._source_descriptors(
                profiled,
                run_directory,
                records,
                source_instances,
            )
        except (OSError, runtime.ProfiledFaultRuntimeError) as error:
            runtime_error = runtime_error or str(error)
    actor_path = _marker_path(run_directory, selected)
    actor_log_record: dict[str, object] | None = None
    if actor_path.is_file():
        actor_log_record = {
            "path": actor_path.relative_to(run_directory).as_posix(),
            "sha256": runtime.sha256_file(actor_path),
        }
    manifest: dict[str, object] = {
        "schema_version": 1,
        "scenario": SCENARIO,
        "run_id": run_id,
        "kauri_revision": revision,
        "profile": {
            "profile_id": diagnosis.profile_id,
            "sha256": diagnosis.profile_sha256,
        },
        "runtime_profile": {
            "profile_id": profiled.profile_id,
            "sha256": profiled.profile_sha256,
        },
        "arm": arm,
        "fault_plan_sha256": plan.sha256,
        "attempt": 1,
        "attempt_scope": ATTEMPT_SCOPE,
        "retry_policy": "none",
        "complete": runtime_error is None,
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "source_instances": source_instances,
        "authoritative_observer": profiled.authoritative_observer,
        "configuration_boundaries": {"diagnostic": diagnostic_boundary},
        "cleanup_ledger": cleanup_ledger,
        "runtime_error": runtime_error,
        "preflight": preflight_result,
        "runtime_artifacts": runtime_artifacts,
        "sources": sources,
        "actor_log": actor_log_record,
    }
    runtime.write_json_exclusive(run_directory / "manifest.json", manifest)

    verdict = "INCOMPLETE"
    validation: dict[str, object]
    if runtime_error is not None:
        validation = {
            "schema_version": 1,
            "scenario": SCENARIO,
            "verdict": "INCOMPLETE",
            "run_id": run_id,
            "arm": arm,
            "error": runtime_error,
            "evidence_ceiling": diagnosis.pilot_ceiling,
            "figure_eligible": False,
        }
    else:
        try:
            _verify_inventory(run_directory, diagnosis, manifest)
            _verify_provenance(
                run_directory,
                diagnosis,
                profiled,
                manifest,
                trusted_provenance,
            )
            validation = _raw_validation(run_directory, diagnosis, profiled)
            if validation.get("verdict") != "PASS":
                raise N31StaticDiagnosisRuntimeError(
                    "independent raw validation did not PASS"
                )
            verdict = "PASS"
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            N31StaticDiagnosisError,
            N31StaticDiagnosisRuntimeError,
            ProfiledFaultEvaluationError,
            runtime.ProfiledFaultRuntimeError,
        ) as error:
            verdict = "FAIL"
            validation_error = str(error)
            validation = {
                "schema_version": 1,
                "scenario": SCENARIO,
                "verdict": "FAIL",
                "run_id": run_id,
                "arm": arm,
                "error": validation_error,
                "evidence_ceiling": diagnosis.pilot_ceiling,
                "figure_eligible": False,
            }
    state.update(
        {
            "phase": "finished",
            "runtime_error": runtime_error,
            "cleanup_error": cleanup_error,
            "validation_error": validation_error,
            "verdict": verdict,
            "interrupted": interrupted,
            "finished_utc": finished_utc,
        }
    )
    runtime.replace_json(state_path, state)
    runtime.write_json_exclusive(run_directory / "validation.json", validation)
    try:
        create_evidence_seal(run_directory)
        verify_evidence_seal(run_directory)
    except (OSError, EvidenceSealError) as error:
        raise N31StaticDiagnosisRuntimeError(
            f"preserved run evidence could not be sealed: {error}"
        ) from error
    if verdict == "PASS":
        independent = validate_preserved_run(
            run_directory,
            trusted_provenance=trusted_provenance,
        )
        if independent.get("verdict") != "PASS":
            raise N31StaticDiagnosisRuntimeError(
                "sealed independent validation did not PASS"
            )
    return run_directory, verdict


__all__ = (
    "N31StaticDiagnosisRuntimeError",
    "TrustedBinary",
    "TrustedProvenance",
    "derive_trusted_provenance",
    "load_trusted_provenance",
    "preflight",
    "run_once",
    "validate_preserved_run",
    "write_trusted_provenance",
)
