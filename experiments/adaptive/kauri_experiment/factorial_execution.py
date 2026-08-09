"""Fail-closed local execution for one SHAPE25 factorial slot.

The frozen factorial modules describe *what* may be run.  This module owns the
small, deliberately local execution boundary: it proves an exact pushed
revision, materializes one slot, launches each process once in its own POSIX
session, observes the two queued transitions from native events, and preserves
the attempt even when it is incomplete.

Nothing in this module chooses an adaptive result.  In particular, actor truth
is passed only to the replica-side Byzantine adapter and never to the manager.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import IO, Any, Protocol

from .factorial_manifest import (
    FactorialArm,
    FactorialManifestError,
    FactorialSlot,
    PRECONTAINMENT_FAULT_COVERAGE_GATE_V1,
    PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1,
    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1,
    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2,
    PortAllocation,
    build_factorial_plan,
    derive_actor_ids,
    derive_consensus_shape,
    derive_tiered_cohorts,
    load_frozen_manifest_bytes,
)
from .factorial_runtime import (
    FactorialRuntimePlan,
    ManagerSecretMaterial,
    ReplicaProcessSpec,
    SlotRuntimeSpec,
    build_factorial_runtime,
    build_slot_runtime,
    canonical_runtime_bytes,
    materialize_manager_argv,
    materialize_replica_argv,
)
from .processes import (
    CleanupEscalationOutcome,
    CleanupEscalationResult,
    CleanupOutcome,
    ProcessRecord,
    ProcessRegistry,
)
from . import profiled_fault_runtime as _legacy_runtime


MANAGER_REPLICA_ID = -1
ISSUER_ID = 1
REPLICA_NETWORK_WORKERS = 2
MAX_REPLICA_MESSAGE_BYTES = 4 << 20
MAX_COMMAND_BYTES = 4096
MAX_ANCESTRY_BLOCKS = 128
MAX_TRANSACTION_COUNT = (1 << 64) - 1
NANOSECONDS_PER_SECOND = 1_000_000_000
DEFAULT_POLL_INTERVAL_S = 0.05
DEFAULT_CLEANUP_TIMEOUT_S = 5.0
CLEANUP_SAMPLE_DURATION_S = 5
CLEANUP_SAMPLE_INTERVAL_MS = 1
CLEANUP_SAMPLE_TIMEOUT_S = 15.0
CLEANUP_DIAGNOSTICS_RELATIVE_PATH = Path(
    "raw/diagnostics/cleanup-escalations.json"
)
BUILD_EVIDENCE_DIRECTORY = "build-evidence"
CAMPAIGN_AUTHORIZATION_FILENAME = "campaign-authorization.json"
CAMPAIGN_CONTRACT_FILENAME = "campaign-execution-contract.json"
CAMPAIGN_LEDGER_FILENAME = "campaign-attempt-ledger.jsonl"
CAMPAIGN_SUMMARY_FILENAME = "campaign-execution-summary.json"
MAX_CAMPAIGN_ROOT_ARTIFACT_BYTES = 4 << 20
_BUILD_EVIDENCE_GROUPS = {
    "binaries": "binaries",
    "build_metadata": "build-metadata",
}
_REDACTION_KEY_DOMAIN = b"kauri.shape25.launch-redaction-key.v1"
_EVENT_ENVELOPE_FIELDS = frozenset(
    {
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
)
_HEX_DIGITS = frozenset("0123456789abcdef")


class FactorialExecutionError(RuntimeError):
    """A slot cannot be launched safely or cannot qualify as evidence."""


class IncompleteFactorialSlot(FactorialExecutionError):
    """The one preserved attempt stopped before all frozen gates completed."""


class _ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecutionBinaries:
    app: Path
    manager: Path
    keygen: Path
    tls_keygen: Path

    def as_mapping(self) -> dict[str, Path]:
        return {
            "app": self.app,
            "manager": self.manager,
            "keygen": self.keygen,
            "tls_keygen": self.tls_keygen,
        }


@dataclass(frozen=True, slots=True)
class IdentityMaterial:
    bls: tuple[Mapping[str, str], ...]
    tls: tuple[Mapping[str, str], ...]
    issuer: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ExecutionPreflight:
    revision: str
    repository: Path
    build_directory: Path
    result_root: Path
    slot_directory: Path
    free_bytes: int
    binaries: ExecutionBinaries
    build_provenance: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class MaterializedLaunch:
    manager_argv: tuple[str, ...]
    replica_argv: tuple[ReplicaProcessSpec, ...]
    redacted_manager_argv: tuple[str, ...]
    redacted_replica_argv: tuple[ReplicaProcessSpec, ...]
    input_artifacts: tuple[Mapping[str, object], ...]
    redaction_key_id: str


@dataclass(frozen=True, slots=True)
class SpawnedProcess:
    record: ProcessRecord
    stdout: IO[bytes]
    stderr: IO[bytes]


@dataclass(frozen=True, slots=True)
class N7SmokeSlot:
    """Explicitly excluded, non-figure N=7/fanout-two PS smoke contract."""

    slot: FactorialSlot
    runtime: SlotRuntimeSpec
    campaign_member: bool = False
    figure_eligible: bool = False
    denominator_contribution: int = 0
    actor_count_rule: str = "fixed_1_hard_actor_smoke_only"


@dataclass(frozen=True, slots=True)
class N31CoverageSmokeSlot:
    """Excluded N=31 precontainment-coverage proof using campaign slot 066."""

    slot: FactorialSlot
    runtime: SlotRuntimeSpec
    campaign_member: bool = False
    figure_eligible: bool = False
    denominator_contribution: int = 0
    source_campaign_slot_id: str = "slot-066-n31-f5-b05-P"


@dataclass(frozen=True, slots=True)
class SlotExecutionResult:
    slot_directory: Path
    outcome: str
    reason: str | None
    launch_count: int
    phase_cutoffs: Mapping[str, object] | None
    cleanup_ledger: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class _Event:
    source: str
    relative_path: str
    line_number: int
    value: Mapping[str, Any]
    line_sha256: str

    @property
    def timestamp_ns(self) -> int:
        value = self.value.get("source_monotonic_ns")
        if type(value) is not int or value <= 0:
            raise FactorialExecutionError(
                f"invalid structured-event timestamp in {self.relative_path}:"
                f"{self.line_number}"
            )
        return value

    def reference(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "line_number": self.line_number,
            "source_id": self.source,
            "source_sequence": self.value["source_sequence"],
            "source_monotonic_ns": self.timestamp_ns,
            "event_type": self.value["event_type"],
            "line_sha256": self.line_sha256,
        }


def _canonical_json_bytes(value: object) -> bytes:
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
        raise FactorialExecutionError("execution artifact is not canonical JSON") from error


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_stable_regular_file(path: Path, label: str) -> bytes:
    """Read one bounded root artifact without following links or racing a writer."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FactorialExecutionError(
            f"{label} is absent or is not a safe regular file"
        ) from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > MAX_CAMPAIGN_ROOT_ARTIFACT_BYTES
        ):
            raise FactorialExecutionError(
                f"{label} is not a bounded regular file"
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise FactorialExecutionError(f"{label} changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise FactorialExecutionError(f"{label} changed while being read")
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise FactorialExecutionError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _acquire_campaign_root_lock(root: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(root, flags)
    except OSError as error:
        raise FactorialExecutionError(
            "campaign result root is absent or unsafe"
        ) from error
    try:
        opened = os.fstat(descriptor)
        current = os.stat(root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise FactorialExecutionError("campaign result root identity drifted")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FactorialExecutionError(
                "another campaign ledger/launch operation is active"
            ) from error
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _assert_campaign_root_identity(root: Path, descriptor: int) -> None:
    opened = os.fstat(descriptor)
    try:
        current = os.stat(root, follow_symlinks=False)
    except OSError as error:
        raise FactorialExecutionError("campaign result root disappeared") from error
    if (
        not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise FactorialExecutionError("campaign result root identity drifted")


def _release_campaign_root_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def append_campaign_ledger_record(path: Path, value: object) -> None:
    """Append one canonical row while excluding concurrent launch guards."""

    path = Path(path)
    if path.name != CAMPAIGN_LEDGER_FILENAME:
        raise FactorialExecutionError("campaign ledger path is not exact")
    root = path.parent
    root_descriptor = _acquire_campaign_root_lock(root)
    try:
        _assert_campaign_root_identity(root, root_descriptor)
        summary_path = root / CAMPAIGN_SUMMARY_FILENAME
        if summary_path.exists() or summary_path.is_symlink():
            raise FactorialExecutionError(
                "finalized campaign cannot append another ledger record"
            )
        if path.is_symlink():
            raise FactorialExecutionError(
                "campaign attempt ledger must not be a symlink"
            )
        if path.exists() and not stat.S_ISREG(path.stat().st_mode):
            raise FactorialExecutionError(
                "campaign attempt ledger must be a regular file"
            )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "ab") as output:
                descriptor = -1
                output.write(_canonical_json_bytes(value))
                output.flush()
                os.fsync(output.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        _assert_campaign_root_identity(root, root_descriptor)
    finally:
        _release_campaign_root_lock(root_descriptor)


def _campaign_summary_payload_for_locked_ledger(
    value: object,
    *,
    authorization_payload: bytes,
    contract_payload: bytes,
    ledger_payload: bytes,
) -> bytes:
    summary_payload = _canonical_json_bytes(value)
    summary = _parse_canonical_object(
        summary_payload,
        "campaign execution summary",
    )
    authorization = _parse_canonical_object(
        authorization_payload,
        "campaign authorization",
    )
    contract = _parse_canonical_object(
        contract_payload,
        "campaign execution contract",
    )
    if not ledger_payload or not ledger_payload.endswith(b"\n"):
        raise FactorialExecutionError(
            "campaign attempt ledger has no complete terminal record"
        )
    raw_rows = ledger_payload.splitlines(keepends=True)
    if len(raw_rows) % 2:
        raise FactorialExecutionError(
            "campaign attempt ledger has an active unpaired STARTED record"
        )
    rows = tuple(
        _parse_canonical_object(raw, f"campaign attempt ledger row {index}")
        for index, raw in enumerate(raw_rows, 1)
    )
    attempted_count = len(rows) // 2
    expected_slot_count = contract.get("expected_slot_count")
    schedule = contract.get("execution_schedule")
    if (
        type(expected_slot_count) is not int
        or expected_slot_count <= 0
        or not isinstance(schedule, list)
        or len(schedule) != expected_slot_count
        or attempted_count > expected_slot_count
    ):
        raise FactorialExecutionError(
            "campaign execution contract has an invalid expected count"
        )

    campaign_id = contract.get("campaign_id")
    authorization_id = authorization.get("authorization_id")
    authorization_sha256 = _sha256_bytes(authorization_payload)
    contract_sha256 = _sha256_bytes(contract_payload)
    raw_approved_utc = authorization.get("approved_utc")
    if not isinstance(raw_approved_utc, str) or not raw_approved_utc:
        raise FactorialExecutionError(
            "campaign authorization approval timestamp is invalid"
        )
    try:
        approved_utc = dt.datetime.fromisoformat(
            raw_approved_utc.replace("Z", "+00:00")
        )
    except ValueError as error:
        raise FactorialExecutionError(
            "campaign authorization approval timestamp is invalid"
        ) from error
    if approved_utc.tzinfo is None or approved_utc.utcoffset() is None:
        raise FactorialExecutionError(
            "campaign authorization approval timestamp must be timezone-aware"
        )
    first_recorded_utc: dt.datetime | None = None
    previous_recorded_utc: dt.datetime | None = None
    previous_monotonic_ns = 0
    final_terminal_utc: dt.datetime | None = None
    for attempt_index in range(attempted_count):
        started = rows[attempt_index * 2]
        terminal = rows[attempt_index * 2 + 1]
        expected = schedule[attempt_index]
        if not isinstance(expected, Mapping):
            raise FactorialExecutionError(
                "campaign execution contract schedule is malformed"
            )
        if started.get("state") != "STARTED" or terminal.get("state") != "TERMINAL":
            raise FactorialExecutionError(
                "campaign attempt ledger does not pair STARTED then TERMINAL"
            )
        expected_ordinal = attempt_index + 1
        if (
            type(expected.get("execution_ordinal")) is not int
            or expected.get("execution_ordinal") != expected_ordinal
            or any(
                row.get("execution_ordinal") != expected_ordinal
                or type(row.get("execution_ordinal")) is not int
                or row.get("campaign_id") != campaign_id
                or row.get("authorization_id") != authorization_id
                or row.get("authorization_sha256") != authorization_sha256
                or row.get("contract_sha256") != contract_sha256
                or any(
                    row.get(field) != expected.get(field)
                    for field in ("slot_id", "block_id", "arm_code")
                )
                for row in (started, terminal)
            )
        ):
            raise FactorialExecutionError(
                "campaign attempt ledger pair identity/order binding drifted"
            )
        for row in (started, terminal):
            raw_recorded_utc = row.get("recorded_utc")
            if not isinstance(raw_recorded_utc, str) or not raw_recorded_utc:
                raise FactorialExecutionError(
                    "campaign ledger recorded UTC timestamp is invalid"
                )
            try:
                recorded_utc = dt.datetime.fromisoformat(
                    raw_recorded_utc.replace("Z", "+00:00")
                )
            except ValueError as error:
                raise FactorialExecutionError(
                    "campaign ledger recorded UTC timestamp is invalid"
                ) from error
            if recorded_utc.tzinfo is None or recorded_utc.utcoffset() is None:
                raise FactorialExecutionError(
                    "campaign ledger recorded UTC timestamp must be timezone-aware"
                )
            monotonic_ns = row.get("recorded_monotonic_ns")
            if type(monotonic_ns) is not int or monotonic_ns <= 0:
                raise FactorialExecutionError(
                    "campaign ledger monotonic timestamp is invalid"
                )
            if monotonic_ns <= previous_monotonic_ns:
                raise FactorialExecutionError(
                    "campaign ledger monotonic timestamps are not strictly increasing"
                )
            if (
                previous_recorded_utc is not None
                and recorded_utc <= previous_recorded_utc
            ):
                raise FactorialExecutionError(
                    "campaign ledger UTC timestamps are not strictly increasing"
                )
            if first_recorded_utc is None:
                first_recorded_utc = recorded_utc
            previous_monotonic_ns = monotonic_ns
            previous_recorded_utc = recorded_utc
        final_terminal_utc = previous_recorded_utc

    expected_summary_fields = {
        "schema_version",
        "campaign_id",
        "authorization_id",
        "authorization_sha256",
        "contract_sha256",
        "ledger_sha256",
        "expected_slot_count",
        "attempted_slot_count",
        "next_execution_ordinal",
        "execution_complete",
        "stopped_reason",
        "completed_utc",
    }
    if set(summary) != expected_summary_fields:
        raise FactorialExecutionError("campaign execution summary schema drifted")
    if summary.get("ledger_sha256") != _sha256_bytes(ledger_payload):
        raise FactorialExecutionError(
            "campaign execution summary ledger digest drifted"
        )
    if (
        type(summary.get("attempted_slot_count")) is not int
        or summary.get("attempted_slot_count") != attempted_count
    ):
        raise FactorialExecutionError(
            "campaign execution summary attempted count drifted"
        )
    if (
        type(summary.get("expected_slot_count")) is not int
        or summary.get("expected_slot_count") != expected_slot_count
    ):
        raise FactorialExecutionError(
            "campaign execution summary expected count drifted"
        )
    stopped_reason = summary.get("stopped_reason")
    complete = attempted_count == expected_slot_count and stopped_reason is None
    expected_next = (
        None if attempted_count == expected_slot_count else attempted_count + 1
    )
    raw_completed_utc = summary.get("completed_utc")
    if not isinstance(raw_completed_utc, str) or not raw_completed_utc:
        raise FactorialExecutionError(
            "campaign execution summary completion timestamp is invalid"
        )
    try:
        completed = dt.datetime.fromisoformat(
            raw_completed_utc.replace("Z", "+00:00")
        )
    except ValueError as error:
        raise FactorialExecutionError(
            "campaign execution summary completion timestamp is invalid"
        ) from error
    if completed.tzinfo is None or completed.utcoffset() is None:
        raise FactorialExecutionError(
            "campaign execution summary completion timestamp must be timezone-aware"
        )
    if completed < approved_utc:
        raise FactorialExecutionError(
            "campaign execution summary completion timestamp precedes authorization "
            "approval"
        )
    if first_recorded_utc is None or first_recorded_utc < approved_utc:
        raise FactorialExecutionError(
            "campaign first ledger record precedes authorization approval"
        )
    if final_terminal_utc is None or completed < final_terminal_utc:
        raise FactorialExecutionError(
            "campaign execution summary completion timestamp precedes final TERMINAL"
        )
    if (
        type(summary.get("schema_version")) is not int
        or summary.get("schema_version") != 1
        or summary.get("campaign_id") != campaign_id
        or summary.get("authorization_id") != authorization_id
        or summary.get("authorization_sha256") != authorization_sha256
        or summary.get("contract_sha256") != contract_sha256
        or summary.get("next_execution_ordinal") != expected_next
        or type(summary.get("execution_complete")) is not bool
        or summary.get("execution_complete") is not complete
        or (
            not complete
            and (not isinstance(stopped_reason, str) or not stopped_reason)
        )
    ):
        raise FactorialExecutionError(
            "campaign execution summary does not seal the locked lifecycle"
        )
    return summary_payload


def publish_campaign_summary(path: Path, value: object) -> None:
    """Publish the sole canonical final summary under the campaign-root lock."""

    path = Path(path)
    if path.name != CAMPAIGN_SUMMARY_FILENAME:
        raise FactorialExecutionError("campaign summary path is not exact")
    root = path.parent
    root_descriptor = _acquire_campaign_root_lock(root)
    try:
        _assert_campaign_root_identity(root, root_descriptor)
        authorization_payload = _read_stable_regular_file(
            root / CAMPAIGN_AUTHORIZATION_FILENAME,
            "campaign authorization",
        )
        contract_payload = _read_stable_regular_file(
            root / CAMPAIGN_CONTRACT_FILENAME,
            "campaign execution contract",
        )
        ledger_payload = _read_stable_regular_file(
            root / CAMPAIGN_LEDGER_FILENAME,
            "campaign attempt ledger",
        )
        if path.exists() or path.is_symlink():
            raise FactorialExecutionError(
                "campaign summary already exists; refusing replacement"
            )
        payload = _campaign_summary_payload_for_locked_ledger(
            value,
            authorization_payload=authorization_payload,
            contract_payload=contract_payload,
            ledger_payload=ledger_payload,
        )
        try:
            _write_exclusive(path, payload)
        except FileExistsError as error:
            raise FactorialExecutionError(
                "campaign summary already exists; refusing replacement"
            ) from error
        if _read_stable_regular_file(path, "campaign execution summary") != payload:
            raise FactorialExecutionError(
                "published campaign summary differs from exact canonical bytes"
            )
        _assert_campaign_root_identity(root, root_descriptor)
    finally:
        _release_campaign_root_lock(root_descriptor)


def _build_evidence_rows(
    build_provenance: Mapping[str, object],
) -> dict[str, dict[str, Mapping[str, object]]]:
    groups: dict[str, dict[str, Mapping[str, object]]] = {}
    for provenance_name, directory_name in _BUILD_EVIDENCE_GROUPS.items():
        raw_rows = build_provenance.get(provenance_name)
        if not isinstance(raw_rows, Mapping) or not raw_rows:
            raise FactorialExecutionError(
                f"exact-build provenance lacks {provenance_name} rows"
            )
        rows: dict[str, Mapping[str, object]] = {}
        for name, raw_row in raw_rows.items():
            if (
                not isinstance(name, str)
                or not name
                or name in {".", ".."}
                or "/" in name
                or "\\" in name
                or not isinstance(raw_row, Mapping)
                or set(raw_row) != {"path", "size_bytes", "sha256"}
            ):
                raise FactorialExecutionError(
                    f"exact-build provenance row is malformed: {provenance_name}"
                )
            source = raw_row.get("path")
            size = raw_row.get("size_bytes")
            digest = raw_row.get("sha256")
            if (
                not isinstance(source, str)
                or not Path(source).is_absolute()
                or type(size) is not int
                or size <= 0
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in _HEX_DIGITS for character in digest)
            ):
                raise FactorialExecutionError(
                    f"exact-build provenance identity is malformed: {name}"
                )
            rows[name] = raw_row
        groups[directory_name] = rows
    return groups


def _copy_build_evidence_file(
    source_path: Path,
    destination_path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    source_descriptor = -1
    destination_descriptor = -1
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    digest = hashlib.sha256()
    try:
        source_descriptor = os.open(source_path, source_flags)
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise FactorialExecutionError(
                f"build evidence source size/type drifted: {source_path}"
            )
        destination_descriptor = os.open(destination_path, destination_flags, 0o500)
        copied = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise OSError("short build-evidence write")
                view = view[written:]
        os.fsync(destination_descriptor)
        after = os.fstat(source_descriptor)
        if (
            copied != expected_size
            or digest.hexdigest() != expected_sha256
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise FactorialExecutionError(
                f"build evidence source bytes drifted during preservation: {source_path}"
            )
    except FileExistsError as error:
        raise FactorialExecutionError(
            f"build evidence destination already exists: {destination_path}"
        ) from error
    except OSError as error:
        raise FactorialExecutionError(
            f"cannot preserve build evidence {source_path}: {error}"
        ) from error
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
    if (
        destination_path.is_symlink()
        or not destination_path.is_file()
        or destination_path.stat().st_size != expected_size
        or _sha256_file(destination_path) != expected_sha256
    ):
        raise FactorialExecutionError(
            f"preserved build evidence does not re-hash exactly: {destination_path}"
        )


def preserve_build_evidence(
    result_root: Path,
    build_provenance: Mapping[str, object],
    *,
    initial_files: Mapping[str, bytes] | None = None,
) -> Path:
    """Verify a staged archive, then claim and publish a fresh result root."""

    result_root = Path(result_root)
    groups = _build_evidence_rows(build_provenance)
    initial_files = {} if initial_files is None else dict(initial_files)
    for name, payload in initial_files.items():
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name == BUILD_EVIDENCE_DIRECTORY
            or not isinstance(payload, bytes)
        ):
            raise FactorialExecutionError("initial result-root artifact is unsafe")
    if result_root.is_symlink() or result_root.exists():
        raise FactorialExecutionError(
            f"build evidence result root already exists: {result_root}"
        )
    parent = result_root.parent
    if parent.is_symlink():
        raise FactorialExecutionError(
            f"build evidence result parent is unsafe: {parent}"
        )
    if not parent.exists():
        parent_parent = parent.parent
        if parent_parent.is_symlink() or not parent_parent.is_dir():
            raise FactorialExecutionError(
                f"build evidence result parent cannot be created safely: {parent}"
            )
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise FactorialExecutionError(
                f"cannot create build evidence result parent: {parent}"
            ) from error
    if parent.is_symlink() or not parent.is_dir():
        raise FactorialExecutionError(
            f"build evidence result parent is unsafe: {parent}"
        )
    staging_root: Path | None = Path(
        tempfile.mkdtemp(
            prefix=f".{result_root.name}.build-evidence-staging-",
            dir=parent,
        )
    )
    result_root_claimed = False
    try:
        evidence_root = staging_root / BUILD_EVIDENCE_DIRECTORY
        evidence_root.mkdir(mode=0o700)
        for directory_name in sorted(groups):
            directory = evidence_root / directory_name
            directory.mkdir(mode=0o700)
            for name, row in sorted(groups[directory_name].items()):
                _copy_build_evidence_file(
                    Path(str(row["path"])),
                    directory / name,
                    expected_size=int(row["size_bytes"]),
                    expected_sha256=str(row["sha256"]),
                )
        for name, payload in sorted(initial_files.items()):
            _write_exclusive(staging_root / name, payload)
        verify_preserved_build_evidence(staging_root, build_provenance)
        try:
            result_root.mkdir(mode=0o700)
        except FileExistsError as error:
            raise FactorialExecutionError(
                f"build evidence result root appeared during staging: {result_root}"
            ) from error
        result_root_claimed = True
        for staged_path in sorted(staging_root.iterdir(), key=lambda path: path.name):
            os.rename(staged_path, result_root / staged_path.name)
        staging_root.rmdir()
        staging_root = None
        return result_root / BUILD_EVIDENCE_DIRECTORY
    except OSError as error:
        preserved_remainder = (
            f"; unmoved staged evidence preserved at {staging_root}"
            if result_root_claimed and staging_root is not None
            else ""
        )
        raise FactorialExecutionError(
            f"cannot publish preserved build evidence: {error}{preserved_remainder}"
        ) from error
    finally:
        if (
            not result_root_claimed
            and staging_root is not None
            and staging_root.exists()
        ):
            shutil.rmtree(staging_root)


def verify_preserved_build_evidence(
    result_root: Path,
    build_provenance: Mapping[str, object],
) -> None:
    """Re-hash the exact root archive without consulting original build paths."""

    groups = _build_evidence_rows(build_provenance)
    evidence_root = Path(result_root) / BUILD_EVIDENCE_DIRECTORY
    if evidence_root.is_symlink() or not evidence_root.is_dir():
        raise FactorialExecutionError("build evidence root is absent or is a symlink")
    actual_groups = {path.name: path for path in evidence_root.iterdir()}
    if set(actual_groups) != set(groups):
        raise FactorialExecutionError("build evidence contains unexpected root entries")
    for directory_name, rows in groups.items():
        directory = actual_groups[directory_name]
        if directory.is_symlink() or not directory.is_dir():
            raise FactorialExecutionError(
                f"build evidence group is not a regular directory: {directory_name}"
            )
        actual = {path.name: path for path in directory.iterdir()}
        if set(actual) != set(rows):
            raise FactorialExecutionError(
                f"build evidence {directory_name} membership drifted"
            )
        for name, row in rows.items():
            path = actual[name]
            if path.is_symlink():
                raise FactorialExecutionError(
                    f"build evidence contains a symlink: {directory_name}/{name}"
                )
            try:
                file_stat = path.stat()
            except OSError as error:
                raise FactorialExecutionError(
                    f"cannot inspect build evidence: {directory_name}/{name}"
                ) from error
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_size != row["size_bytes"]
                or _sha256_file(path) != row["sha256"]
            ):
                raise FactorialExecutionError(
                    f"build evidence bytes drifted: {directory_name}/{name}"
                )


def _preserved_execution_binaries(result_root: Path) -> ExecutionBinaries:
    binary_root = Path(result_root) / BUILD_EVIDENCE_DIRECTORY / "binaries"
    return ExecutionBinaries(
        app=binary_root / "app",
        manager=binary_root / "manager",
        keygen=binary_root / "keygen",
        tls_keygen=binary_root / "tls_keygen",
    )


def _derive_redaction_key(authorization_receipt: bytes, slot_id: str) -> bytes:
    """Derive the reproducible evidence-redaction key for one authorized slot."""

    if not isinstance(authorization_receipt, bytes) or not authorization_receipt:
        raise FactorialExecutionError(
            "redaction key derivation requires exact authorization receipt bytes"
        )
    if not isinstance(slot_id, str) or not slot_id:
        raise FactorialExecutionError(
            "redaction key derivation requires a nonempty slot ID"
        )
    slot_bytes = slot_id.encode("utf-8")
    try:
        preimage = b"".join(
            (
                _REDACTION_KEY_DOMAIN,
                b"\x00",
                len(slot_bytes).to_bytes(4, "big"),
                slot_bytes,
                len(authorization_receipt).to_bytes(8, "big"),
                authorization_receipt,
            )
        )
    except OverflowError as error:
        raise FactorialExecutionError(
            "redaction key derivation input is too large"
        ) from error
    return hashlib.sha256(preimage).digest()


def build_execution_authorization_receipt(
    *,
    scope: str,
    approval_reference: str,
    approved_utc: str,
    kauri_revision: str,
    slot_ids: Sequence[str],
    result_root: str,
    static_artifacts: Mapping[str, bytes],
    build_provenance_sha256: str,
) -> bytes:
    """Create the explicit thesis-author receipt required before launch."""

    if scope not in {
        "excluded_n7_smoke",
        "excluded_n31_coverage_smoke",
        "shape25_campaign",
    }:
        raise FactorialExecutionError("execution authorization scope is invalid")
    if not isinstance(approval_reference, str) or not approval_reference.strip():
        raise FactorialExecutionError("execution approval reference is required")
    if len(approval_reference) > 512:
        raise FactorialExecutionError("execution approval reference is too long")
    try:
        approved = dt.datetime.fromisoformat(approved_utc.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise FactorialExecutionError("execution approval timestamp is invalid") from error
    if approved.tzinfo is None or approved.utcoffset() is None:
        raise FactorialExecutionError("execution approval timestamp must include UTC offset")
    if (
        not isinstance(kauri_revision, str)
        or len(kauri_revision) != 40
        or any(character not in "0123456789abcdef" for character in kauri_revision)
    ):
        raise FactorialExecutionError("execution authorization revision is invalid")
    slots = tuple(slot_ids)
    if not slots or len(set(slots)) != len(slots) or any(
        not isinstance(slot_id, str) or not slot_id for slot_id in slots
    ):
        raise FactorialExecutionError("execution authorization slot set is invalid")
    if not isinstance(result_root, str) or not result_root:
        raise FactorialExecutionError("execution authorization result root is invalid")
    result_path = Path(result_root)
    if (
        result_path.is_absolute()
        or result_path.as_posix() != result_root
        or not result_path.parts
        or result_path.parts[0] != "results"
        or any(part in {"", ".", ".."} for part in result_path.parts)
    ):
        raise FactorialExecutionError(
            "execution authorization result root must be a canonical repository-relative "
            "results path"
        )
    if set(static_artifacts) != {"manifest.json", "plan.json", "runtime.json"}:
        raise FactorialExecutionError("execution authorization static artifacts drifted")
    static_hashes = {
        name: _sha256_bytes(bytes(static_artifacts[name]))
        for name in sorted(static_artifacts)
    }
    if (
        not isinstance(build_provenance_sha256, str)
        or len(build_provenance_sha256) != 64
        or any(character not in _HEX_DIGITS for character in build_provenance_sha256)
    ):
        raise FactorialExecutionError(
            "execution authorization build provenance digest is invalid"
        )
    core: dict[str, object] = {
        "schema_version": 2,
        "scope": scope,
        "authorized_by": "thesis_author",
        "approval_reference": approval_reference.strip(),
        "approved_utc": approved_utc,
        "kauri_revision": kauri_revision,
        "slot_ids": list(slots),
        "result_root": result_root,
        "static_artifacts_sha256": static_hashes,
        "build_provenance_sha256": build_provenance_sha256,
        "automatic_retries": 0,
        "replacement_policy": "none",
    }
    document = {
        **core,
        "authorization_id": "execution-authorization-"
        + _sha256_bytes(_canonical_json_bytes(core))[:24],
    }
    return _canonical_json_bytes(document)


def build_campaign_execution_contract(
    *,
    runtime: FactorialRuntimePlan,
    static_artifacts: Mapping[str, bytes],
    authorization: Mapping[str, object],
    authorization_payload: bytes,
    build_provenance: Mapping[str, object],
) -> dict[str, object]:
    """Bind the exact authorization, build, and immutable sequential schedule."""

    if set(static_artifacts) != {"manifest.json", "plan.json", "runtime.json"}:
        raise FactorialExecutionError("campaign contract static artifacts drifted")
    try:
        return {
            "schema_version": 1,
            "campaign_id": runtime.runtime_id,
            "manifest_id": runtime.manifest_id,
            "manifest_sha256": _sha256_bytes(static_artifacts["manifest.json"]),
            "plan_sha256": _sha256_bytes(static_artifacts["plan.json"]),
            "runtime_sha256": _sha256_bytes(static_artifacts["runtime.json"]),
            "authorization_id": authorization["authorization_id"],
            "authorization_sha256": _sha256_bytes(authorization_payload),
            "kauri_revision": authorization["kauri_revision"],
            "build_provenance_sha256": _sha256_bytes(
                _canonical_json_bytes(build_provenance)
            ),
            "execution_mode": "fixed_sequential",
            "automatic_retries": 0,
            "replacement_policy": "none",
            "outcome_dependent_order": False,
            "expected_slot_count": len(runtime.slots),
            "execution_schedule": [
                {
                    "execution_ordinal": item.execution_ordinal,
                    "slot_id": item.slot_id,
                    "block_id": item.block_id,
                    "arm_code": item.arm_code,
                }
                for item in runtime.slots
            ],
        }
    except KeyError as error:
        raise FactorialExecutionError(
            "campaign contract authorization is malformed"
        ) from error


def _bind_execution_authorization(
    payload: bytes,
    *,
    slot: FactorialSlot,
    preflight: ExecutionPreflight,
    static_artifacts: Mapping[str, bytes],
    campaign_member: bool,
) -> dict[str, object]:
    if not isinstance(payload, bytes) or not payload:
        raise FactorialExecutionError("explicit execution authorization receipt is required")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FactorialExecutionError("execution authorization receipt is invalid JSON") from error
    if not isinstance(document, dict) or _canonical_json_bytes(document) != payload:
        raise FactorialExecutionError("execution authorization receipt is not canonical")
    expected_fields = {
        "schema_version",
        "scope",
        "authorized_by",
        "approval_reference",
        "approved_utc",
        "kauri_revision",
        "slot_ids",
        "result_root",
        "static_artifacts_sha256",
        "build_provenance_sha256",
        "automatic_retries",
        "replacement_policy",
        "authorization_id",
    }
    if set(document) != expected_fields:
        raise FactorialExecutionError("execution authorization receipt schema drifted")
    manifest = load_frozen_manifest_bytes(static_artifacts["manifest.json"])
    plan = build_factorial_plan(manifest)
    expected_scope = (
        "shape25_campaign"
        if campaign_member
        else (
            "excluded_n7_smoke"
            if slot.replica_count == 7
            else "excluded_n31_coverage_smoke"
        )
    )
    expected_slots = (
        [planned.slot_id for planned in plan.slots]
        if campaign_member
        else [slot.slot_id]
    )
    expected_hashes = {
        name: _sha256_bytes(static_artifacts[name])
        for name in sorted(static_artifacts)
    }
    core = {key: value for key, value in document.items() if key != "authorization_id"}
    expected_id = "execution-authorization-" + _sha256_bytes(
        _canonical_json_bytes(core)
    )[:24]
    try:
        actual_result_root = preflight.result_root.relative_to(
            preflight.repository
        ).as_posix()
    except ValueError as error:
        raise FactorialExecutionError(
            "execution preflight result root is outside the exact repository"
        ) from error
    expected_result_root = Path(slot.result_path).parent.as_posix()
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != 2
        or document["scope"] != expected_scope
        or document["authorized_by"] != "thesis_author"
        or not isinstance(document["approval_reference"], str)
        or not document["approval_reference"].strip()
        or len(document["approval_reference"]) > 512
        or document["kauri_revision"] != preflight.revision
        or document["slot_ids"] != expected_slots
        or document["result_root"] != expected_result_root
        or document["result_root"] != actual_result_root
        or document["static_artifacts_sha256"] != expected_hashes
        or document["build_provenance_sha256"]
        != _sha256_bytes(_canonical_json_bytes(preflight.build_provenance))
        or type(document["automatic_retries"]) is not int
        or document["automatic_retries"] != 0
        or document["replacement_policy"] != "none"
        or document["authorization_id"] != expected_id
    ):
        raise FactorialExecutionError("execution authorization receipt is not exact")
    try:
        approved = dt.datetime.fromisoformat(
            str(document["approved_utc"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise FactorialExecutionError("execution approval timestamp is invalid") from error
    if approved.tzinfo is None or approved.utcoffset() is None:
        raise FactorialExecutionError("execution approval timestamp must include UTC offset")
    return document


def _parse_canonical_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FactorialExecutionError(f"{label} is invalid JSON") from error
    if not isinstance(document, dict) or _canonical_json_bytes(document) != payload:
        raise FactorialExecutionError(f"{label} is not exact canonical JSON")
    return document


def _exact_json_value(actual: object, expected: object) -> bool:
    return _canonical_json_bytes(actual) == _canonical_json_bytes(expected)


def _validate_campaign_launch_order(
    *,
    spec: SlotRuntimeSpec,
    preflight: ExecutionPreflight,
    static_artifacts: Mapping[str, bytes],
    authorization_receipt: bytes,
    authorization: Mapping[str, object],
) -> None:
    """Require the exact accepted prefix plus this slot's sole STARTED row."""

    root = preflight.result_root
    if root.is_symlink() or not root.is_dir():
        raise FactorialExecutionError("campaign result root is not a safe directory")
    preserved_authorization = _read_stable_regular_file(
        root / CAMPAIGN_AUTHORIZATION_FILENAME,
        "campaign authorization",
    )
    if preserved_authorization != authorization_receipt:
        raise FactorialExecutionError(
            "campaign root authorization differs from the launch receipt"
        )

    try:
        manifest = load_frozen_manifest_bytes(static_artifacts["manifest.json"])
        plan = build_factorial_plan(manifest)
        runtime = build_factorial_runtime(plan)
    except (KeyError, FactorialManifestError) as error:
        raise FactorialExecutionError(
            "campaign launch order cannot derive the frozen runtime"
        ) from error
    expected_contract = build_campaign_execution_contract(
        runtime=runtime,
        static_artifacts=static_artifacts,
        authorization=authorization,
        authorization_payload=authorization_receipt,
        build_provenance=preflight.build_provenance,
    )
    contract_payload = _read_stable_regular_file(
        root / CAMPAIGN_CONTRACT_FILENAME,
        "campaign execution contract",
    )
    contract = _parse_canonical_object(
        contract_payload,
        "campaign execution contract",
    )
    if contract_payload != _canonical_json_bytes(expected_contract):
        raise FactorialExecutionError(
            "campaign execution contract differs from the exact frozen schedule"
        )

    ledger_payload = _read_stable_regular_file(
        root / CAMPAIGN_LEDGER_FILENAME,
        "campaign attempt ledger",
    )
    if not ledger_payload or not ledger_payload.endswith(b"\n"):
        raise FactorialExecutionError(
            "campaign attempt ledger has no complete STARTED record"
        )
    raw_rows = ledger_payload.splitlines(keepends=True)
    expected_row_count = spec.execution_ordinal * 2 - 1
    if len(raw_rows) != expected_row_count:
        raise FactorialExecutionError(
            "campaign attempt ledger is not the exact next-slot prefix"
        )
    rows = [
        _parse_canonical_object(raw, f"campaign attempt ledger row {index}")
        for index, raw in enumerate(raw_rows, 1)
    ]

    ordered = tuple(sorted(runtime.slots, key=lambda item: item.execution_ordinal))
    if (
        len(ordered) != len(runtime.slots)
        or tuple(item.execution_ordinal for item in ordered)
        != tuple(range(1, len(ordered) + 1))
        or spec.execution_ordinal > len(ordered)
        or ordered[spec.execution_ordinal - 1] != spec
    ):
        raise FactorialExecutionError("campaign runtime order is not contiguous and exact")

    if (root / CAMPAIGN_SUMMARY_FILENAME).exists() or (
        root / CAMPAIGN_SUMMARY_FILENAME
    ).is_symlink():
        raise FactorialExecutionError("a finalized campaign cannot be resumed")
    expected_root_entries = {
        BUILD_EVIDENCE_DIRECTORY,
        CAMPAIGN_AUTHORIZATION_FILENAME,
        CAMPAIGN_CONTRACT_FILENAME,
        CAMPAIGN_LEDGER_FILENAME,
        *(item.slot_id for item in ordered[: spec.execution_ordinal - 1]),
    }
    actual_root_entries = {path.name: path for path in root.iterdir()}
    if set(actual_root_entries) != expected_root_entries:
        raise FactorialExecutionError(
            "campaign result root is not the exact completed-slot prefix"
        )
    for name, path in actual_root_entries.items():
        should_be_directory = name == BUILD_EVIDENCE_DIRECTORY or name.startswith(
            "slot-"
        )
        if path.is_symlink() or (
            should_be_directory and not path.is_dir()
        ) or (not should_be_directory and not path.is_file()):
            raise FactorialExecutionError(
                "campaign result root contains an unsafe prefix artifact"
            )
    contract_sha256 = _sha256_bytes(contract_payload)
    authorization_sha256 = _sha256_bytes(authorization_receipt)
    build_provenance_sha256 = _sha256_bytes(
        _canonical_json_bytes(preflight.build_provenance)
    )
    started_fields = {
        "schema_version",
        "campaign_id",
        "manifest_sha256",
        "source_plan_sha256",
        "runtime_sha256",
        "contract_sha256",
        "authorization_id",
        "authorization_sha256",
        "kauri_revision",
        "execution_ordinal",
        "slot_id",
        "block_id",
        "arm_code",
        "attempt_ordinal",
        "automatic_retries",
        "replacement_policy",
        "state",
        "recorded_utc",
        "recorded_monotonic_ns",
        "slot_directory",
        "preflight_revision",
        "preflight_free_bytes",
        "build_provenance_sha256",
    }
    terminal_fields = (
        started_fields
        - {
            "preflight_revision",
            "preflight_free_bytes",
            "build_provenance_sha256",
        }
        | {
            "execution_outcome",
            "execution_reason",
            "launch_count",
            "validation",
        }
    )
    previous_monotonic_ns = 0
    for ordinal in range(1, spec.execution_ordinal + 1):
        expected = ordered[ordinal - 1]
        expected_common = {
            "schema_version": 1,
            "campaign_id": runtime.runtime_id,
            "manifest_sha256": _sha256_bytes(static_artifacts["manifest.json"]),
            "source_plan_sha256": _sha256_bytes(static_artifacts["plan.json"]),
            "runtime_sha256": _sha256_bytes(static_artifacts["runtime.json"]),
            "contract_sha256": contract_sha256,
            "authorization_id": authorization["authorization_id"],
            "authorization_sha256": authorization_sha256,
            "kauri_revision": preflight.revision,
            "execution_ordinal": ordinal,
            "slot_id": expected.slot_id,
            "block_id": expected.block_id,
            "arm_code": expected.arm_code,
            "attempt_ordinal": 1,
            "automatic_retries": 0,
            "replacement_policy": "none",
        }
        started = rows[(ordinal - 1) * 2]
        if set(started) != started_fields or any(
            not _exact_json_value(started.get(key), value)
            for key, value in expected_common.items()
        ):
            raise FactorialExecutionError(
                "campaign STARTED ledger identity/order binding drifted"
            )
        if (
            started["state"] != "STARTED"
            or started["slot_directory"] != str(root / expected.slot_id)
            or started["preflight_revision"] != preflight.revision
            or started["build_provenance_sha256"] != build_provenance_sha256
            or type(started["preflight_free_bytes"]) is not int
            or started["preflight_free_bytes"] < runtime.minimum_free_bytes
        ):
            raise FactorialExecutionError(
                "campaign STARTED ledger does not bind the exact preflight"
            )
        if ordinal == spec.execution_ordinal and (
            started["preflight_free_bytes"] != preflight.free_bytes
            or started["slot_directory"] != str(preflight.slot_directory)
        ):
            raise FactorialExecutionError(
                "current campaign STARTED row differs from this preflight"
            )

        records = [started]
        if ordinal < spec.execution_ordinal:
            terminal = rows[(ordinal - 1) * 2 + 1]
            if set(terminal) != terminal_fields or any(
                not _exact_json_value(terminal.get(key), value)
                for key, value in expected_common.items()
            ):
                raise FactorialExecutionError(
                    "campaign TERMINAL ledger identity/order binding drifted"
                )
            expected_validation = {
                "outcome": "PASS",
                "reason": None,
                "integrity_valid": True,
                "campaign_member": True,
                "figure_eligible": True,
            }
            if (
                terminal["state"] != "TERMINAL"
                or terminal["slot_directory"] != str(root / expected.slot_id)
                or terminal["execution_outcome"] != "PASS"
                or terminal["execution_reason"] is not None
                or type(terminal["launch_count"]) is not int
                or terminal["launch_count"] != expected.replica_count + 1
                or not _exact_json_value(
                    terminal["validation"], expected_validation
                )
            ):
                raise FactorialExecutionError(
                    "campaign cannot continue after a non-PASS terminal attempt"
                )
            prior_directory = root / expected.slot_id
            if prior_directory.is_symlink() or not prior_directory.is_dir():
                raise FactorialExecutionError(
                    "campaign ledger prefix lacks its preserved prior slot"
                )
            prior_authorization = _read_stable_regular_file(
                prior_directory / "execution-authorization.json",
                "prior slot execution authorization",
            )
            prior_build_provenance = _read_stable_regular_file(
                prior_directory / "runtime/exact-build-provenance.json",
                "prior slot exact-build provenance",
            )
            if (
                prior_authorization != authorization_receipt
                or prior_build_provenance
                != _canonical_json_bytes(preflight.build_provenance)
            ):
                raise FactorialExecutionError(
                    "campaign ledger prefix differs from its preserved authorization/build"
                )
            if ordinal == spec.execution_ordinal - 1:
                # The immediately preceding slot independently validates the
                # sequence inductively; the final validator replays every slot.
                from .factorial_validation import validate_slot

                replayed = validate_slot(prior_directory)
                replayed_validation = {
                    "outcome": replayed.outcome,
                    "reason": replayed.reason,
                    "integrity_valid": replayed.integrity_valid,
                    "campaign_member": replayed.campaign_member,
                    "figure_eligible": replayed.figure_eligible,
                }
                if (
                    replayed.slot_id != expected.slot_id
                    or not _exact_json_value(
                        replayed_validation, expected_validation
                    )
                    or not _exact_json_value(
                        terminal["validation"], replayed_validation
                    )
                ):
                    raise FactorialExecutionError(
                        "campaign cannot continue without an independently "
                        "validated predecessor"
                    )
            records.append(terminal)

        for record in records:
            try:
                recorded = dt.datetime.fromisoformat(
                    str(record["recorded_utc"]).replace("Z", "+00:00")
                )
            except ValueError as error:
                raise FactorialExecutionError(
                    "campaign ledger UTC timestamp is invalid"
                ) from error
            monotonic_ns = record["recorded_monotonic_ns"]
            if (
                recorded.tzinfo is None
                or recorded.utcoffset() is None
                or type(monotonic_ns) is not int
                or monotonic_ns <= 0
                or monotonic_ns < previous_monotonic_ns
            ):
                raise FactorialExecutionError(
                    "campaign ledger timestamps are invalid or regress"
                )
            previous_monotonic_ns = monotonic_ns


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def monotonic_raw_ns() -> int:
    """Read the exact native event clock and reject fallback clocks."""

    clock_id = getattr(time, "CLOCK_MONOTONIC_RAW", None)
    reader = getattr(time, "clock_gettime_ns", None)
    if clock_id is None or not callable(reader):
        raise FactorialExecutionError(
            "CLOCK_MONOTONIC_RAW is required for SHAPE25 execution"
        )
    value = int(reader(clock_id))
    if value <= 0:
        raise FactorialExecutionError(
            "CLOCK_MONOTONIC_RAW returned a non-positive timestamp"
        )
    return value


def _write_exclusive(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _replace_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    _write_exclusive(temporary, _canonical_json_bytes(value))
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _append_jsonl(path: Path, value: object) -> None:
    payload = _canonical_json_bytes(value)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab", buffering=0) as output:
        output.write(payload)
        os.fsync(output.fileno())


def slot_ports(slot: FactorialSlot) -> tuple[int, ...]:
    return (
        *range(slot.ports.peer_base, slot.ports.peer_base + slot.replica_count),
        *range(slot.ports.client_base, slot.ports.client_base + slot.replica_count),
        slot.ports.manager,
    )


def _bind_static_artifacts(
    slot: FactorialSlot,
    spec: SlotRuntimeSpec,
    static_artifacts: Mapping[str, bytes],
    *,
    campaign_member: bool,
) -> dict[str, bytes]:
    """Bind exact frozen planning bytes to this supplied slot/runtime pair."""

    expected_names = {"manifest.json", "plan.json", "runtime.json"}
    if set(static_artifacts) != expected_names:
        raise FactorialExecutionError(
            "static artifacts must be exact manifest.json, plan.json, runtime.json"
        )
    normalized: dict[str, bytes] = {}
    for name in sorted(expected_names):
        payload = static_artifacts[name]
        if not isinstance(payload, bytes):
            raise FactorialExecutionError(f"{name} must be supplied as exact bytes")
        normalized[name] = payload

    try:
        derived_spec = build_slot_runtime(slot)
    except FactorialManifestError as error:
        raise FactorialExecutionError(f"slot/runtime derivation failed: {error}") from error
    if derived_spec != spec:
        raise FactorialExecutionError("supplied slot/runtime contract does not match")

    try:
        manifest = load_frozen_manifest_bytes(normalized["manifest.json"])
    except FactorialManifestError as error:
        raise FactorialExecutionError(
            f"manifest.json does not match the exact frozen bytes: {error}"
        ) from error
    try:
        plan = build_factorial_plan(manifest)
    except FactorialManifestError as error:
        raise FactorialExecutionError(
            f"plan.json cannot be derived from manifest.json: {error}"
        ) from error
    if normalized["plan.json"] != plan.canonical_bytes:
        raise FactorialExecutionError(
            "plan.json does not match the exact manifest-derived campaign plan bytes"
        )

    if campaign_member:
        planned_slots = tuple(item for item in plan.slots if item.slot_id == slot.slot_id)
        if len(planned_slots) != 1 or planned_slots[0] != slot:
            raise FactorialExecutionError(
                "supplied campaign slot is not the exact plan.json slot"
            )
        try:
            runtime = build_factorial_runtime(plan)
        except FactorialManifestError as error:
            raise FactorialExecutionError(
                f"runtime.json cannot be derived from plan.json: {error}"
            ) from error
        if normalized["runtime.json"] != canonical_runtime_bytes(runtime):
            raise FactorialExecutionError(
                "runtime.json does not match the exact plan-derived runtime bytes"
            )
        runtime_slots = tuple(
            item for item in runtime.slots if item.slot_id == spec.slot_id
        )
        if len(runtime_slots) != 1 or runtime_slots[0] != spec:
            raise FactorialExecutionError(
                "supplied campaign runtime is not the exact runtime.json slot"
            )
        return normalized

    expected_runtime_bytes = _canonical_json_bytes(spec.as_document())
    if normalized["runtime.json"] != expected_runtime_bytes:
        raise FactorialExecutionError(
            "runtime.json does not match the exact direct excluded-smoke runtime document"
        )
    smoke_matches: list[N7SmokeSlot | N31CoverageSmokeSlot] = []
    if slot.replica_count == 7:
        for template in plan.slots:
            try:
                candidate = build_n7_ps_smoke_slot(
                    template,
                    scientific_seed=slot.scientific_seed,
                    peer_base=slot.ports.peer_base,
                    client_base=slot.ports.client_base,
                    manager_port=slot.ports.manager,
                    result_path=slot.result_path,
                )
            except FactorialExecutionError:
                continue
            if candidate.slot == slot and candidate.runtime == spec:
                smoke_matches.append(candidate)
    elif slot.replica_count == 31:
        for template in plan.slots:
            try:
                candidate = build_n31_coverage_smoke_slot(
                    template,
                    result_path=slot.result_path,
                )
            except FactorialExecutionError:
                continue
            if candidate.slot == slot and candidate.runtime == spec:
                smoke_matches.append(candidate)
    if not smoke_matches or (
        slot.replica_count == 31 and len(smoke_matches) != 1
    ):
        raise FactorialExecutionError(
            "supplied excluded smoke is not derivable from the exact frozen plan"
        )
    return normalized


def verify_evidence_preflight(
    slot: FactorialSlot,
    *,
    repository: Path,
    build_directory: Path,
    build_provenance_path: Path,
    result_root: Path,
    minimum_free_bytes: int,
    verify_repository: Callable[[Path], str] = _legacy_runtime.verify_repository_state,
    verify_build: Callable[..., Mapping[str, object]] = (
        _legacy_runtime.verify_exact_build_provenance
    ),
    occupied_ports: Callable[[Sequence[int]], Sequence[int]] = (
        _legacy_runtime.occupied_ports
    ),
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> ExecutionPreflight:
    """Prove clean/pushed source, exact binaries, free space, and free ports.

    This function is intentionally read-only.  A colliding slot directory is
    rejected rather than reused, and no result directory is created until the
    complete preflight has passed.
    """

    if not isinstance(slot, FactorialSlot):
        raise FactorialExecutionError("evidence preflight requires a FactorialSlot")
    if type(minimum_free_bytes) is not int or minimum_free_bytes < 0:
        raise FactorialExecutionError("minimum free bytes must be nonnegative")
    repository = repository.resolve()
    build_directory = build_directory.resolve()
    result_root = result_root.resolve()
    try:
        result_root.relative_to(repository)
    except ValueError as error:
        raise FactorialExecutionError(
            "evidence result root must be inside the exact Kauri repository"
        ) from error
    if repository.name != "Kauri":
        raise FactorialExecutionError("repository is not the Kauri top level")
    try:
        revision = verify_repository(repository)
    except Exception as error:
        raise FactorialExecutionError(str(error)) from error
    binary_paths = _legacy_runtime.exact_binary_paths(repository, build_directory)
    try:
        provenance = verify_build(
            repository=repository,
            build_directory=build_directory,
            provenance_path=build_provenance_path,
            binaries=binary_paths,
        )
    except Exception as error:
        raise FactorialExecutionError(str(error)) from error
    binaries = ExecutionBinaries(
        app=binary_paths["app"].resolve(),
        manager=binary_paths["manager"].resolve(),
        keygen=binary_paths["keygen"].resolve(),
        tls_keygen=binary_paths["tls_keygen"].resolve(),
    )
    slot_directory = result_root / slot.slot_id
    if slot_directory.exists():
        raise FactorialExecutionError(
            f"slot result collision; refusing reuse: {slot_directory}"
        )
    probe = result_root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free_bytes = int(disk_usage(probe).free)
    if free_bytes < minimum_free_bytes:
        raise FactorialExecutionError(
            "result volume does not meet the fixed minimum-free-bytes threshold"
        )
    ports = slot_ports(slot)
    if len(set(ports)) != len(ports) or any(not 1 <= port <= 65_535 for port in ports):
        raise FactorialExecutionError("slot port allocation is invalid or overlapping")
    occupied = tuple(occupied_ports(ports))
    if occupied:
        raise FactorialExecutionError(f"required ports are already in use: {list(occupied)}")
    return ExecutionPreflight(
        revision=revision,
        repository=repository,
        build_directory=build_directory,
        result_root=result_root,
        slot_directory=slot_directory,
        free_bytes=free_bytes,
        binaries=binaries,
        build_provenance=provenance,
    )


def _parse_identity_output(
    text: str,
    *,
    expected_count: int,
    expected_fields: frozenset[str],
    label: str,
) -> tuple[Mapping[str, str], ...]:
    rows: list[dict[str, str]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields: dict[str, str] = {}
        for token in line.split():
            if ":" not in token:
                raise FactorialExecutionError(
                    f"{label} line {line_number} is malformed"
                )
            key, value = token.split(":", 1)
            if not key or not value or key in fields:
                raise FactorialExecutionError(
                    f"{label} line {line_number} is malformed"
                )
            fields[key] = value
        if set(fields) != expected_fields:
            raise FactorialExecutionError(f"{label} line {line_number} schema drifted")
        rows.append(fields)
    if len(rows) != expected_count:
        raise FactorialExecutionError(
            f"{label} produced {len(rows)} identities; expected {expected_count}"
        )
    for field in expected_fields:
        if len({row[field] for row in rows}) != len(rows):
            raise FactorialExecutionError(f"{label} produced duplicate {field} values")
    return tuple(rows)


def generate_identities(
    spec: SlotRuntimeSpec,
    *,
    binaries: ExecutionBinaries,
    runtime_directory: Path,
    run_command: Callable[..., Any] = subprocess.run,
) -> IdentityMaterial:
    """Generate exactly N BLS, N+1 TLS, and one secp256k1 identity."""

    commands = {
        "bls": (
            str(binaries.keygen),
            "--num",
            str(spec.replica_count),
            "--algo",
            "bls",
        ),
        "tls": (
            str(binaries.tls_keygen),
            "--num",
            str(spec.replica_count + 1),
        ),
        "issuer": (str(binaries.keygen), "--num", "1", "--algo", "secp256k1"),
    }
    outputs: dict[str, str] = {}
    for label, command in commands.items():
        result = run_command(
            command,
            cwd=runtime_directory,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise FactorialExecutionError(
                f"{label} identity generation failed with exit {result.returncode}"
            )
        outputs[label] = result.stdout
        _write_exclusive(
            runtime_directory / f"{label}-identities.txt",
            result.stdout.encode("utf-8"),
        )
    return IdentityMaterial(
        bls=_parse_identity_output(
            outputs["bls"],
            expected_count=spec.replica_count,
            expected_fields=frozenset({"pub", "sec"}),
            label="BLS keygen",
        ),
        tls=_parse_identity_output(
            outputs["tls"],
            expected_count=spec.replica_count + 1,
            expected_fields=frozenset({"crt", "sec", "cid"}),
            label="TLS keygen",
        ),
        issuer=_parse_identity_output(
            outputs["issuer"],
            expected_count=1,
            expected_fields=frozenset({"pub", "sec"}),
            label="issuer keygen",
        )[0],
    )


def _main_config_payload(
    slot: FactorialSlot,
    spec: SlotRuntimeSpec,
    identities: IdentityMaterial,
) -> bytes:
    if (
        len(identities.bls) != spec.replica_count
        or len(identities.tls) != spec.replica_count + 1
    ):
        raise FactorialExecutionError("identity cardinality drifted")
    lines = [
        *spec.main_config.lines,
        "nworker = 2",
        f"repnworker = {REPLICA_NETWORK_WORKERS}",
        f"stat-period = {spec.fault_window.hard_timeout_s + 60}",
        "pace-maker = dummy",
        "proposer = 0",
        "base-timeout = 2.0",
        "prop-delay = 0.1",
        "client-ip = 127.0.0.1",
        "tree-generation = default",
        f"epoch-change-issuer-id = {ISSUER_ID}",
        f"epoch-change-issuer-public-key = {identities.issuer['pub']}",
        f"epoch-change-maximum-block-extra-bytes = {MAX_COMMAND_BYTES}",
        f"epoch-change-maximum-ancestry-blocks = {MAX_ANCESTRY_BLOCKS}",
        f"epoch-manager-tls-cert = {identities.tls[spec.replica_count]['crt']}",
        f"max-rep-msg = {MAX_REPLICA_MESSAGE_BYTES}",
    ]
    keys = [line.split("=", 1)[0].strip() for line in lines]
    if len(set(keys)) != len(keys):
        duplicates = sorted(key for key in set(keys) if keys.count(key) > 1)
        raise FactorialExecutionError(f"main config duplicates keys: {duplicates}")
    for replica_id in range(spec.replica_count):
        lines.append(
            "replica = "
            f"127.0.0.1:{slot.ports.peer_base + replica_id};"
            f"{slot.ports.client_base + replica_id}, "
            f"{identities.bls[replica_id]['pub']}, "
            f"{identities.tls[replica_id]['cid']}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _replica_config_payload(
    replica_id: int,
    identities: IdentityMaterial,
) -> bytes:
    return (
        f"privkey = {identities.bls[replica_id]['sec']}\n"
        f"tls-privkey = {identities.tls[replica_id]['sec']}\n"
        f"tls-cert = {identities.tls[replica_id]['crt']}\n"
        f"idx = {replica_id}\n"
    ).encode("utf-8")


def _redaction(
    value: str,
    *,
    key: bytes,
    key_id: str,
) -> str:
    return f"hmac-sha256:{key_id}:{hmac.new(key, value.encode(), hashlib.sha256).hexdigest()}"


def _redact_manager_argv(
    argv: Sequence[str],
    *,
    key: bytes,
    key_id: str,
) -> tuple[str, ...]:
    result = list(argv)
    for option in ("--tls-privkey", "--tls-cert", "--issuer-private-key"):
        if result.count(option) != 1:
            raise FactorialExecutionError(f"manager argv has invalid {option} count")
        index = result.index(option) + 1
        result[index] = _redaction(result[index], key=key, key_id=key_id)
    for index, argument in enumerate(tuple(result)):
        if argument != "--replica":
            continue
        fields = result[index + 1].split(",")
        if len(fields) != 3:
            raise FactorialExecutionError("manager replica endpoint is malformed")
        fields[2] = _redaction(fields[2], key=key, key_id=key_id)
        result[index + 1] = ",".join(fields)
    return tuple(result)


def write_slot_configs(
    slot: FactorialSlot,
    spec: SlotRuntimeSpec,
    *,
    slot_directory: Path,
    identities: IdentityMaterial,
) -> tuple[Mapping[str, object], ...]:
    """Write all private configs before the shared prelaunch clock sample."""

    if slot.slot_id != spec.slot_id or slot.replica_count != spec.replica_count:
        raise FactorialExecutionError("slot/runtime identity mismatch")
    slot_directory = slot_directory.resolve()
    runtime_directory = slot_directory / "runtime"
    main_config = slot_directory / spec.main_config.path
    _write_exclusive(main_config, _main_config_payload(slot, spec, identities))
    paths: list[tuple[Path, str, int | None]] = [
        (main_config, "main_config", None)
    ]
    for replica_id in range(spec.replica_count):
        path = runtime_directory / f"replica-{replica_id}.conf"
        _write_exclusive(path, _replica_config_payload(replica_id, identities))
        paths.append((path, "replica_config", replica_id))
    paths.extend(
        (
            runtime_directory / f"{label}-identities.txt",
            f"{label}_identity_input",
            None,
        )
        for label in ("bls", "tls", "issuer")
    )
    rows: list[Mapping[str, object]] = []
    for path, kind, replica_id in paths:
        if not path.is_file() or path.is_symlink():
            raise FactorialExecutionError(f"required runtime input is absent: {path}")
        rows.append(
            {
                "kind": kind,
                "replica_id": replica_id,
                "relative_path": str(path.relative_to(slot_directory)),
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return tuple(rows)


def materialize_launch(
    slot: FactorialSlot,
    spec: SlotRuntimeSpec,
    *,
    slot_directory: Path,
    binaries: ExecutionBinaries,
    identities: IdentityMaterial,
    input_artifacts: Sequence[Mapping[str, object]],
    shared_raw_clock_anchor_ns: int,
    redaction_key: bytes,
) -> MaterializedLaunch:
    """Bind already-written configs to one shared prelaunch clock anchor."""

    if slot.slot_id != spec.slot_id or slot.replica_count != spec.replica_count:
        raise FactorialExecutionError("slot/runtime identity mismatch")
    if not redaction_key:
        raise FactorialExecutionError("a nonempty redaction key is required")
    slot_directory = slot_directory.resolve()
    expected_count = spec.replica_count + 4
    if len(input_artifacts) != expected_count:
        raise FactorialExecutionError("runtime input artifact cardinality drifted")
    for item in input_artifacts:
        relative = item.get("relative_path")
        digest = item.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise FactorialExecutionError("runtime input artifact row is malformed")
        path = slot_directory / relative
        if not path.is_file() or _sha256_file(path) != digest:
            raise FactorialExecutionError("runtime input changed before argv binding")
    secrets = ManagerSecretMaterial(
        manager_tls_private_key_der_hex=identities.tls[spec.replica_count]["sec"],
        manager_tls_certificate_der_hex=identities.tls[spec.replica_count]["crt"],
        issuer_private_key_hex=identities.issuer["sec"],
        replica_tls_certificate_der_hex=tuple(
            row["crt"] for row in identities.tls[: spec.replica_count]
        ),
    )
    manager_template = materialize_manager_argv(
        spec,
        slot_directory,
        secrets,
        shared_raw_clock_anchor_ns=shared_raw_clock_anchor_ns,
    )
    manager_argv = (str(binaries.manager), *manager_template[1:])
    replica_templates = materialize_replica_argv(
        spec,
        slot_directory,
        shared_raw_clock_anchor_ns,
    )
    replica_argv = tuple(
        ReplicaProcessSpec(
            replica_id=process.replica_id,
            argv=(str(binaries.app), *process.argv[1:]),
        )
        for process in replica_templates
    )
    key_id = hashlib.sha256(redaction_key).hexdigest()[:16]
    redacted_manager = _redact_manager_argv(
        manager_template,
        key=redaction_key,
        key_id=key_id,
    )
    # Replica argv has no secret values; it is still HMAC-bound as a complete
    # launch vector in slot.json.
    redacted_replicas = tuple(replica_templates)
    return MaterializedLaunch(
        manager_argv=manager_argv,
        replica_argv=replica_argv,
        redacted_manager_argv=redacted_manager,
        redacted_replica_argv=redacted_replicas,
        input_artifacts=tuple(input_artifacts),
        redaction_key_id=key_id,
    )


def _safe_terminate_unregistered(process: _ProcessLike) -> None:
    if process.poll() is not None:
        return
    try:
        _legacy_runtime._terminate_unregistered_process(process)  # type: ignore[attr-defined]
    except Exception as error:
        raise FactorialExecutionError(
            f"unregistered child {process.pid} could not be cleaned up"
        ) from error


def spawn_exclusive_owned_process(
    registry: ProcessRegistry,
    *,
    name: str,
    replica_id: int,
    command: Sequence[str],
    stdout_path: Path,
    stderr_path: Path,
    working_directory: Path,
    popen_factory: Callable[..., _ProcessLike] = subprocess.Popen,
) -> SpawnedProcess:
    """Spawn once with separate exclusive logs and register its exact PGID."""

    stdout = stdout_path.open("xb", buffering=0)
    try:
        stderr = stderr_path.open("xb", buffering=0)
    except BaseException:
        stdout.close()
        raise
    process: _ProcessLike | None = None
    try:
        process = popen_factory(
            tuple(command),
            cwd=working_directory,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            close_fds=True,
        )
        record = registry.register(name=name, replica_id=replica_id, process=process)
    except BaseException as registration_error:
        cleanup_error: BaseException | None = None
        if process is not None:
            try:
                _safe_terminate_unregistered(process)
            except BaseException as error:
                cleanup_error = error
        stdout.close()
        stderr.close()
        if cleanup_error is not None:
            raise FactorialExecutionError(
                f"process registration and cleanup both failed: {cleanup_error}"
            ) from registration_error
        raise
    return SpawnedProcess(record=record, stdout=stdout, stderr=stderr)


def _validate_event(
    value: Mapping[str, Any],
    *,
    spec: SlotRuntimeSpec,
    source: str,
    expected_instance: str,
    expected_sequence: int,
) -> None:
    expected_kind = "adaptation_manager" if source == "adaptive-manager" else "replica"
    if (
        set(value) != _EVENT_ENVELOPE_FIELDS
        or value.get("event_schema_version") != 1
        or value.get("run_id") != spec.slot_id
        or value.get("source_kind") != expected_kind
        or value.get("source_id") != source
        or value.get("source_instance") != expected_instance
        or value.get("source_sequence") != expected_sequence
        or not isinstance(value.get("payload"), Mapping)
        or not isinstance(value.get("event_type"), str)
    ):
        raise FactorialExecutionError(f"structured-event envelope drifted in {source}")


def _read_event_file(
    spec: SlotRuntimeSpec,
    slot_directory: Path,
    *,
    source: str,
    instance: str,
    relative_path: str,
    allow_partial: bool,
) -> tuple[_Event, ...]:
    path = slot_directory / relative_path
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return ()
    lines = payload.splitlines(keepends=True)
    if lines and not lines[-1].endswith(b"\n"):
        if not allow_partial:
            raise FactorialExecutionError(f"incomplete final JSONL line: {relative_path}")
        lines.pop()
    events: list[_Event] = []
    previous_timestamp = 0
    for line_number, raw in enumerate(lines, 1):
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FactorialExecutionError(
                f"malformed JSONL at {relative_path}:{line_number}"
            ) from error
        if not isinstance(value, dict):
            raise FactorialExecutionError(
                f"non-object JSONL at {relative_path}:{line_number}"
            )
        _validate_event(
            value,
            spec=spec,
            source=source,
            expected_instance=instance,
            expected_sequence=line_number,
        )
        event = _Event(
            source=source,
            relative_path=relative_path,
            line_number=line_number,
            value=value,
            line_sha256=_sha256_bytes(raw),
        )
        if event.timestamp_ns < previous_timestamp:
            raise FactorialExecutionError(f"structured-event clock regressed in {source}")
        previous_timestamp = event.timestamp_ns
        events.append(event)
    return tuple(events)


def read_event_streams(
    spec: SlotRuntimeSpec,
    slot_directory: Path,
    *,
    allow_partial: bool = True,
) -> dict[str, tuple[_Event, ...]]:
    contract = spec.structured_events
    streams = {
        "adaptive-manager": _read_manager_event_stream(
            spec, slot_directory, allow_partial=allow_partial
        )
    }
    for replica_id in range(spec.replica_count):
        source = f"replica-{replica_id}"
        streams[source] = _read_event_file(
            spec,
            slot_directory,
            source=source,
            instance=contract.replica_source_instances[replica_id],
            relative_path=contract.replica_output_relative_paths[replica_id],
            allow_partial=allow_partial,
        )
    return streams


def _read_manager_event_stream(
    spec: SlotRuntimeSpec,
    slot_directory: Path,
    *,
    allow_partial: bool,
) -> tuple[_Event, ...]:
    contract = spec.structured_events
    return _read_event_file(
        spec,
        slot_directory,
        source="adaptive-manager",
        instance=contract.manager_source_instance,
        relative_path=contract.manager_output_relative_path,
        allow_partial=allow_partial,
    )


def _assert_process_health(
    records: Sequence[ProcessRecord],
    *,
    expected_clean_exits: Collection[str] = (),
    clean_exit_authorizer: Callable[[ProcessRecord], bool] | None = None,
) -> None:
    allowed = frozenset(expected_clean_exits)
    exited: list[str] = []
    for record in records:
        returncode = record.process.poll()
        if returncode is None:
            continue
        if record.name in allowed and returncode == 0:
            continue
        if (
            returncode == 0
            and clean_exit_authorizer is not None
            and clean_exit_authorizer(record)
        ):
            continue
        exited.append(f"{record.name}={returncode}")
    if exited:
        raise IncompleteFactorialSlot("unexpected pre-cleanup exit: " + ", ".join(exited))


def _wait_until(
    description: str,
    predicate: Callable[[], object | None],
    *,
    phase_timeout_s: float,
    hard_deadline_ns: int,
    records: Sequence[ProcessRecord],
    raw_now_ns: Callable[[], int],
    sleep: Callable[[float], None],
    poll_interval_s: float,
    expected_clean_exits: Collection[str] = (),
    clean_exit_authorizer: Callable[[ProcessRecord], bool] | None = None,
) -> object:
    phase_deadline_ns = raw_now_ns() + int(phase_timeout_s * NANOSECONDS_PER_SECOND)
    while True:
        result = predicate()
        _assert_process_health(
            records,
            expected_clean_exits=expected_clean_exits,
            clean_exit_authorizer=clean_exit_authorizer,
        )
        now = raw_now_ns()
        if now >= hard_deadline_ns:
            raise IncompleteFactorialSlot(
                f"hard deadline expired while waiting for {description}"
            )
        if now >= phase_deadline_ns:
            raise IncompleteFactorialSlot(f"timed out waiting for {description}")
        if result is not None:
            return result
        sleep(poll_interval_s)


def _remaining_hard_deadline_s(
    hard_deadline_ns: int,
    raw_now_ns: Callable[[], int],
) -> float:
    """Return the only sound bound while manager selection is still pending.

    The native convergence deadline starts after a successor has been selected;
    it is not an evidence-accumulation deadline.  Until the shape decision marks
    that boundary, the observer therefore uses the already-frozen per-slot hard
    deadline instead of inventing an earlier clock.
    """

    return max(
        0.0,
        (hard_deadline_ns - raw_now_ns()) / NANOSECONDS_PER_SECOND,
    )


def _event_payload(event: _Event) -> Mapping[str, Any]:
    payload = event.value.get("payload")
    if not isinstance(payload, Mapping):
        raise FactorialExecutionError("structured event has no payload object")
    return payload


def _epoch_activation_identity(
    event: _Event,
) -> tuple[int, int, str, int]:
    """Parse the exact flat payload emitted for a native epoch activation."""

    payload = _event_payload(event)
    if set(payload) != {
        "epoch_number",
        "tree_id",
        "epoch_digest",
        "activation_height",
    }:
        raise FactorialExecutionError("epoch activation payload is malformed")
    epoch_number = payload.get("epoch_number")
    tree_id = payload.get("tree_id")
    epoch_digest = payload.get("epoch_digest")
    activation_height = payload.get("activation_height")
    if (
        type(epoch_number) is not int
        or epoch_number < 0
        or type(tree_id) is not int
        or tree_id < 0
        or not isinstance(epoch_digest, str)
        or len(epoch_digest) != 64
        or any(character not in _HEX_DIGITS for character in epoch_digest)
        or type(activation_height) is not int
        or activation_height < 1
    ):
        raise FactorialExecutionError("epoch activation identity is malformed")
    return epoch_number, tree_id, epoch_digest, activation_height


def _replica_transition_barrier(
    current: Mapping[str, Sequence[_Event]],
    *,
    replica_count: int,
    event_type: str,
    epoch: int,
) -> _Event | None:
    """Require one identical native transition witness from every replica."""

    selected: list[_Event] = []
    identities: set[tuple[object, ...]] = set()
    for replica_id in range(replica_count):
        candidates: list[_Event] = []
        for event in current[f"replica-{replica_id}"]:
            if event.value.get("event_type") != event_type:
                continue
            payload = _event_payload(event)
            if event_type == "epoch.command_committed":
                if payload.get("successor_epoch_number") != epoch:
                    continue
                identity = tuple(
                    payload.get(key)
                    for key in (
                        "payload_digest",
                        "predecessor_epoch_number",
                        "predecessor_epoch_digest",
                        "successor_epoch_number",
                        "successor_epoch_digest",
                        "activation_delay_blocks",
                        "activation_height",
                    )
                )
            elif event_type == "epoch.activated":
                identity = _epoch_activation_identity(event)
                if identity[0] != epoch:
                    continue
            else:
                raise FactorialExecutionError(
                    f"unsupported replica transition barrier: {event_type}"
                )
            candidates.append(event)
            identities.add(identity)
        if len(candidates) > 1:
            raise FactorialExecutionError(
                f"replica-{replica_id} duplicated {event_type} for epoch {epoch}"
            )
        if not candidates:
            return None
        selected.append(candidates[0])
    if len(identities) != 1:
        raise FactorialExecutionError(
            f"replicas disagree on {event_type} for epoch {epoch}"
        )
    return max(selected, key=lambda event: (event.timestamp_ns, event.source))


def _commit_key(event: _Event) -> tuple[object, object, object, object]:
    payload = _event_payload(event)
    key = (
        payload.get("block_height"),
        payload.get("block_hash"),
        payload.get("parent_hash"),
        payload.get("transaction_count"),
    )
    height, block_hash, parent_hash, transaction_count = key
    if (
        type(height) is not int
        or height <= 0
        or not isinstance(block_hash, str)
        or len(block_hash) != 64
        or any(character not in _HEX_DIGITS for character in block_hash)
        or not isinstance(parent_hash, str)
        or len(parent_hash) != 64
        or any(character not in _HEX_DIGITS for character in parent_hash)
        or type(transaction_count) is not int
        or not 0 <= transaction_count <= MAX_TRANSACTION_COUNT
    ):
        raise FactorialExecutionError("commit identity is malformed")
    return key


def _commit_decision_proof(event: _Event) -> dict[str, object]:
    payload = _event_payload(event)
    proof = payload.get("decision_proof")
    if not isinstance(proof, Mapping) or set(proof) != {
        "epoch_number",
        "tree_id",
        "epoch_digest",
        "block_hash",
    }:
        raise FactorialExecutionError("authoritative commit decision proof is malformed")
    epoch_number = proof.get("epoch_number")
    tree_id = proof.get("tree_id")
    epoch_digest = proof.get("epoch_digest")
    block_hash = proof.get("block_hash")
    if (
        type(epoch_number) is not int
        or epoch_number < 0
        or type(tree_id) is not int
        or tree_id < 0
        or not isinstance(epoch_digest, str)
        or len(epoch_digest) != 64
        or any(character not in _HEX_DIGITS for character in epoch_digest)
        or block_hash != payload.get("block_hash")
    ):
        raise FactorialExecutionError("authoritative commit configuration is malformed")
    return {
        "epoch_number": epoch_number,
        "tree_id": tree_id,
        "epoch_digest": epoch_digest,
        "block_hash": block_hash,
    }


def _commit_has_configuration(
    event: _Event,
    configuration: tuple[int, str],
) -> bool:
    proof = _commit_decision_proof(event)
    return (
        proof["epoch_number"],
        proof["epoch_digest"],
    ) == configuration


def _find_common_commit(
    spec: SlotRuntimeSpec,
    streams: Mapping[str, Sequence[_Event]],
    *,
    start_ns: int,
    end_ns: int,
    expected_configuration: tuple[int, str] | None = None,
) -> dict[str, object] | None:
    witnesses = tuple(range(spec.q))
    observations: dict[int, dict[tuple[object, ...], _Event]] = {}
    for replica_id in witnesses:
        observations[replica_id] = {
            _commit_key(event): event
            for event in streams[f"replica-{replica_id}"]
            if event.value.get("event_type") == "block.commit_observed"
            and start_ns <= event.timestamp_ns < end_ns
        }
    observer = streams[spec.structured_events.commit_observer_id]
    for event in observer:
        if (
            event.value.get("event_type") != "block.committed"
            or not start_ns <= event.timestamp_ns < end_ns
        ):
            continue
        proof = _commit_decision_proof(event)
        if expected_configuration is not None and (
            proof["epoch_number"], proof["epoch_digest"]
        ) != expected_configuration:
            continue
        key = _commit_key(event)
        matches = [observations[replica_id].get(key) for replica_id in witnesses]
        if not all(match is not None for match in matches):
            continue
        exact = [match for match in matches if match is not None]
        return {
            "identity": {
                "block_height": key[0],
                "block_hash": key[1],
                "parent_hash": key[2],
                "transaction_count": key[3],
                "decision_proof": proof,
            },
            "observer": event.reference(),
            "witnesses": [match.reference() for match in exact],
            "common_monotonic_ns": max(
                event.timestamp_ns, *(match.timestamp_ns for match in exact)
            ),
        }
    return None


def _single_manager_event(
    streams: Mapping[str, Sequence[_Event]],
    *,
    event_type: str,
    cycle_ordinal: int,
) -> _Event | None:
    matches = [
        event
        for event in streams["adaptive-manager"]
        if event.value.get("event_type") == event_type
        and _event_payload(event).get("cycle_ordinal") == cycle_ordinal
    ]
    if len(matches) > 1:
        raise FactorialExecutionError(
            f"manager duplicated {event_type} for cycle {cycle_ordinal}"
        )
    return matches[0] if matches else None


def _manager_selection_event(
    streams: Mapping[str, Sequence[_Event]],
    *,
    cycle_ordinal: int,
    precontainment_shape_evaluation_contract: str | None,
) -> _Event | None:
    """Bind each cycle to its exact frozen manager-selection anchor."""

    if precontainment_shape_evaluation_contract not in {
        None,
        PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1,
    }:
        raise FactorialExecutionError(
            "manager selection has an unknown precontainment shape contract"
        )
    preserves_initial_shape = (
        cycle_ordinal == 0
        and precontainment_shape_evaluation_contract
        == PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1
    )
    if preserves_initial_shape:
        if (
            _single_manager_event(
                streams,
                event_type="adaptive_v2_shape_decision",
                cycle_ordinal=cycle_ordinal,
            )
            is not None
        ):
            raise FactorialExecutionError(
                "shape-preserving cycle 0 emitted a shape-v1 decision"
            )
        return _single_manager_event(
            streams,
            event_type="adaptive_v2_evidence_snapshot",
            cycle_ordinal=cycle_ordinal,
        )
    return _single_manager_event(
        streams,
        event_type="adaptive_v2_shape_decision",
        cycle_ordinal=cycle_ordinal,
    )


def _successful_manager_terminal(
    streams: Mapping[str, Sequence[_Event]],
    *,
    cycle_ordinal: int,
) -> _Event | None:
    terminal = _single_manager_event(
        streams,
        event_type="adaptive_v2_session_terminal",
        cycle_ordinal=cycle_ordinal,
    )
    if terminal is None:
        return None
    payload = _event_payload(terminal)
    if (
        payload.get("outcome") != "advanced"
        or payload.get("reason") != "successor_converged"
    ):
        raise IncompleteFactorialSlot(
            f"manager cycle {cycle_ordinal} terminated without convergence"
        )
    return terminal


def _authorize_manager_clean_exit(
    spec: SlotRuntimeSpec,
    slot_directory: Path,
    record: ProcessRecord,
) -> bool:
    """Authorize only the manager's final clean exit from a fresh full read."""

    if record.name != "adaptive-manager":
        return False
    manager_events = _read_manager_event_stream(
        spec,
        slot_directory,
        allow_partial=False,
    )
    return _successful_manager_terminal(
        {"adaptive-manager": manager_events},
        cycle_ordinal=1,
    ) is not None


def observe_slot_phases(
    spec: SlotRuntimeSpec,
    slot_directory: Path,
    records: Sequence[ProcessRecord],
    *,
    shared_raw_clock_anchor_ns: int,
    hard_deadline_ns: int,
    raw_now_ns: Callable[[], int] = monotonic_raw_ns,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> dict[str, object]:
    """Observe only native cutoffs accepted by the independent validator."""

    observation_bound_rule = spec.fault_window.transition_observation_bound_rule
    if observation_bound_rule not in {
        "phase_deadline_v1",
        "shared_slot_hard_deadline_until_manager_selection_v1",
    }:
        raise FactorialExecutionError(
            "transition observer has an unknown manager-selection clock bound"
        )
    expected_clean_exits: set[str] = set()
    precontainment_shape_contract = (
        spec.causal_acceptance.precontainment_shape_evaluation_contract
    )

    def authorize_clean_exit(record: ProcessRecord) -> bool:
        if record.name in expected_clean_exits:
            return True
        authorized = _authorize_manager_clean_exit(spec, slot_directory, record)
        if authorized:
            expected_clean_exits.add(record.name)
        return authorized

    def streams() -> dict[str, tuple[_Event, ...]]:
        return read_event_streams(spec, slot_directory, allow_partial=True)

    def ready() -> int | None:
        current = streams()
        timestamps: list[int] = []
        for source, events in current.items():
            matches = [event for event in events if event.value.get("event_type") == "process.ready"]
            if len(matches) > 1:
                raise FactorialExecutionError(f"{source} duplicated process.ready")
            if not matches:
                return None
            timestamps.append(matches[0].timestamp_ns)
        return max(timestamps)

    ready_ns = int(
        _wait_until(
            f"all {spec.replica_count + 1} process.ready events",
            ready,
            phase_timeout_s=spec.fault_window.start_after_prelaunch_anchor_s,
            hard_deadline_ns=hard_deadline_ns,
            records=records,
            raw_now_ns=raw_now_ns,
            sleep=sleep,
            poll_interval_s=poll_interval_s,
            expected_clean_exits=expected_clean_exits,
            clean_exit_authorizer=authorize_clean_exit,
        )
    )
    width_s = spec.cutoff_contract.bucket_width_s
    width_ns = width_s * NANOSECONDS_PER_SECOND
    fault_start = shared_raw_clock_anchor_ns + (
        spec.fault_window.start_after_prelaunch_anchor_s * NANOSECONDS_PER_SECOND
    )
    fault_evidence_end = fault_start + (
        spec.cutoff_contract.fault_evidence_bucket_count * width_ns
    )

    def baseline() -> tuple[_Event, Mapping[str, object]] | None:
        if raw_now_ns() < fault_start:
            return None
        current = streams()
        duration = spec.cutoff_contract.baseline_bucket_count * width_ns
        candidates = sorted(
            (
                event
                for event in current[spec.structured_events.commit_observer_id]
                if event.value.get("event_type") == "block.committed"
                and ready_ns + duration <= event.timestamp_ns < fault_start
            ),
            key=lambda event: event.timestamp_ns,
            reverse=True,
        )
        for cutoff_event in candidates:
            common = _find_common_commit(
                spec,
                current,
                start_ns=cutoff_event.timestamp_ns - duration,
                end_ns=cutoff_event.timestamp_ns,
            )
            if common is not None:
                return cutoff_event, common
        return None

    baseline_wait_s = max(
        0.0,
        (fault_start - raw_now_ns()) / NANOSECONDS_PER_SECOND,
    ) + spec.fault_window.schedule_slack_s
    baseline_event, baseline_common = _wait_until(
        "fixed pre-fault baseline ending at a native commit with exact Q witness",
        baseline,
        phase_timeout_s=baseline_wait_s,
        hard_deadline_ns=hard_deadline_ns,
        records=records,
        raw_now_ns=raw_now_ns,
        sleep=sleep,
        poll_interval_s=poll_interval_s,
        expected_clean_exits=expected_clean_exits,
        clean_exit_authorizer=authorize_clean_exit,
    )
    if not isinstance(baseline_common, Mapping):
        raise FactorialExecutionError("baseline common-commit proof is malformed")
    baseline_identity = baseline_common.get("identity")
    baseline_proof = (
        baseline_identity.get("decision_proof")
        if isinstance(baseline_identity, Mapping)
        else None
    )
    if not isinstance(baseline_proof, Mapping):
        raise FactorialExecutionError(
            "baseline common-commit proof lacks its exact configuration"
        )
    baseline_configuration = (
        baseline_proof.get("epoch_number"),
        baseline_proof.get("epoch_digest"),
    )
    if (
        type(baseline_configuration[0]) is not int
        or not isinstance(baseline_configuration[1], str)
    ):
        raise FactorialExecutionError("baseline commit configuration is malformed")

    def fault_window_complete() -> bool | None:
        if raw_now_ns() < fault_evidence_end:
            return None
        observer_events = streams()[spec.structured_events.commit_observer_id]
        return True if any(
            event.value.get("event_type") == "block.committed"
            and fault_start <= event.timestamp_ns < fault_evidence_end
            and _commit_has_configuration(event, baseline_configuration)
            for event in observer_events
        ) else None

    fault_evidence_wait_s = max(
        0.0,
        (fault_evidence_end - raw_now_ns()) / NANOSECONDS_PER_SECOND,
    ) + spec.fault_window.schedule_slack_s
    _wait_until(
        "fixed fault-evidence window with authoritative progress",
        fault_window_complete,
        phase_timeout_s=fault_evidence_wait_s,
        hard_deadline_ns=hard_deadline_ns,
        records=records,
        raw_now_ns=raw_now_ns,
        sleep=sleep,
        poll_interval_s=poll_interval_s,
        expected_clean_exits=expected_clean_exits,
        clean_exit_authorizer=authorize_clean_exit,
    )

    transition_events: dict[int, tuple[_Event, _Event, _Event]] = {}
    stable_events: dict[int, tuple[_Event, Mapping[str, object], int]] = {}
    phase_configurations: dict[int, tuple[int, str]] = {}
    for cycle, epoch in ((0, 1), (1, 2)):

        def manager_selection() -> _Event | None:
            current = streams()
            return _manager_selection_event(
                current,
                cycle_ordinal=cycle,
                precontainment_shape_evaluation_contract=(
                    precontainment_shape_contract
                ),
            )

        def transition_ready(
            selected_anchor: _Event | None = None,
        ) -> tuple[_Event, _Event, _Event] | None:
            current = streams()
            command = _replica_transition_barrier(
                current,
                replica_count=spec.replica_count,
                event_type="epoch.command_committed",
                epoch=epoch,
            )
            activation = _replica_transition_barrier(
                current,
                replica_count=spec.replica_count,
                event_type="epoch.activated",
                epoch=epoch,
            )
            selection = _manager_selection_event(
                current,
                cycle_ordinal=cycle,
                precontainment_shape_evaluation_contract=(
                    precontainment_shape_contract
                ),
            )
            terminal = _successful_manager_terminal(
                current,
                cycle_ordinal=cycle,
            )
            if selected_anchor is not None and selection != selected_anchor:
                raise FactorialExecutionError(
                    f"epoch-{epoch} manager selection identity changed"
                )
            if (
                command is None
                or activation is None
                or selection is None
                or terminal is None
            ):
                return None
            if cycle == 0 and command.timestamp_ns < fault_evidence_end:
                raise FactorialExecutionError(
                    "epoch-1 command occurred before the frozen fault-evidence window ended"
                )
            return command, activation, selection

        if observation_bound_rule == (
            "shared_slot_hard_deadline_until_manager_selection_v1"
        ):
            selection = _wait_until(
                f"manager selection anchor for epoch-{epoch}",
                manager_selection,
                phase_timeout_s=_remaining_hard_deadline_s(
                    hard_deadline_ns, raw_now_ns
                ),
                hard_deadline_ns=hard_deadline_ns,
                records=records,
                raw_now_ns=raw_now_ns,
                sleep=sleep,
                poll_interval_s=poll_interval_s,
                expected_clean_exits=expected_clean_exits,
                clean_exit_authorizer=authorize_clean_exit,
            )
            if not isinstance(selection, _Event):
                raise FactorialExecutionError(
                    f"epoch-{epoch} manager selection is malformed"
                )
            command, activation, _ = _wait_until(
                f"exact epoch-{epoch} command, terminal, and activation "
                "after manager selection",
                lambda: transition_ready(selection),
                phase_timeout_s=(
                    spec.fault_window.transition_convergence_deadline_s
                    + spec.fault_window.schedule_slack_s
                ),
                hard_deadline_ns=hard_deadline_ns,
                records=records,
                raw_now_ns=raw_now_ns,
                sleep=sleep,
                poll_interval_s=poll_interval_s,
                expected_clean_exits=expected_clean_exits,
                clean_exit_authorizer=authorize_clean_exit,
            )
        else:
            command, activation, selection = _wait_until(
                f"exact epoch-{epoch} command, manager selection, terminal, "
                "and activation",
                transition_ready,
                phase_timeout_s=(
                    spec.fault_window.transition_convergence_deadline_s
                    + spec.fault_window.schedule_slack_s
                ),
                hard_deadline_ns=hard_deadline_ns,
                records=records,
                raw_now_ns=raw_now_ns,
                sleep=sleep,
                poll_interval_s=poll_interval_s,
                expected_clean_exits=expected_clean_exits,
                clean_exit_authorizer=authorize_clean_exit,
            )
        transition_events[epoch] = (command, activation, selection)
        activated_epoch, _, activated_digest, _ = _epoch_activation_identity(
            activation
        )
        if activated_epoch != epoch:
            raise FactorialExecutionError(
                f"epoch-{epoch} activation identity is malformed"
            )
        phase_configurations[epoch] = (activated_epoch, activated_digest)
        stable_count = (
            spec.cutoff_contract.epoch1_stable_bucket_count
            if epoch == 1
            else spec.cutoff_contract.epoch2_stable_bucket_count
        )

        def first_stable_commit() -> _Event | None:
            observer_events = streams()[spec.structured_events.commit_observer_id]
            candidates = [
                event
                for event in observer_events
                if event.value.get("event_type") == "block.committed"
                and event.timestamp_ns >= activation.timestamp_ns
                and _commit_has_configuration(
                    event,
                    phase_configurations[epoch],
                )
            ]
            return min(candidates, key=lambda event: event.timestamp_ns) if candidates else None

        stable_start_event = _wait_until(
            f"first authoritative epoch-{epoch} commit after activation",
            first_stable_commit,
            phase_timeout_s=spec.fault_window.schedule_slack_s,
            hard_deadline_ns=hard_deadline_ns,
            records=records,
            raw_now_ns=raw_now_ns,
            sleep=sleep,
            poll_interval_s=poll_interval_s,
            expected_clean_exits=expected_clean_exits,
            clean_exit_authorizer=authorize_clean_exit,
        )
        assert isinstance(stable_start_event, _Event)
        stable_end = stable_start_event.timestamp_ns + stable_count * width_ns

        def stable_complete() -> Mapping[str, object] | None:
            if raw_now_ns() < stable_end:
                return None
            return _find_common_commit(
                spec,
                streams(),
                start_ns=stable_start_event.timestamp_ns,
                end_ns=stable_end,
                expected_configuration=phase_configurations[epoch],
            )

        stable_common = _wait_until(
            f"fixed epoch-{epoch} stable window with exact Q common commit",
            stable_complete,
            phase_timeout_s=stable_count * width_s
            + spec.fault_window.schedule_slack_s,
            hard_deadline_ns=hard_deadline_ns,
            records=records,
            raw_now_ns=raw_now_ns,
            sleep=sleep,
            poll_interval_s=poll_interval_s,
            expected_clean_exits=expected_clean_exits,
            clean_exit_authorizer=authorize_clean_exit,
        )
        if not isinstance(stable_common, Mapping):
            raise FactorialExecutionError(
                f"epoch-{epoch} common-commit proof is malformed"
            )
        stable_events[epoch] = (stable_start_event, stable_common, stable_end)

    epoch1_shape = transition_events[2][2]
    if stable_events[1][2] > epoch1_shape.timestamp_ns:
        raise FactorialExecutionError(
            "epoch-2 selector ran before the fixed epoch-1 stable window completed"
        )
    drain_not_before = stable_events[2][2] + (
        spec.fault_window.drain_margin_s * NANOSECONDS_PER_SECOND
    )

    def drain_commit() -> _Event | None:
        candidates = [
            event
            for event in streams()[spec.structured_events.commit_observer_id]
            if event.value.get("event_type") == "block.committed"
            and event.timestamp_ns >= drain_not_before
            and _commit_has_configuration(event, phase_configurations[2])
        ]
        return min(candidates, key=lambda event: event.timestamp_ns) if candidates else None

    drain_event = _wait_until(
        "post-epoch-2 drain and authoritative commit",
        drain_commit,
        phase_timeout_s=spec.fault_window.drain_margin_s
        + spec.fault_window.schedule_slack_s,
        hard_deadline_ns=hard_deadline_ns,
        records=records,
        raw_now_ns=raw_now_ns,
        sleep=sleep,
        poll_interval_s=poll_interval_s,
        expected_clean_exits=expected_clean_exits,
        clean_exit_authorizer=authorize_clean_exit,
    )
    assert isinstance(drain_event, _Event)

    def row(name: str, event: _Event) -> dict[str, object]:
        return {
            "name": name,
            "source_path": event.relative_path,
            "source_sequence": event.value["source_sequence"],
            "event_type": event.value["event_type"],
            "source_monotonic_ns": event.timestamp_ns,
            "event_sha256": event.line_sha256,
        }

    slot_receipt = slot_directory / "slot.json"
    cutoffs = [
        row("baseline_stable", baseline_event),
        {
            "name": "fault_window_open",
            "source_path": "slot.json",
            "source_sequence": 0,
            "event_type": "fault_window.open",
            "source_monotonic_ns": fault_start,
            "event_sha256": _sha256_file(slot_receipt),
        },
        row("epoch1_command", transition_events[1][0]),
        row("epoch1_activation", transition_events[1][1]),
        row("epoch1_stable", stable_events[1][0]),
        row("shape_v1_computed", transition_events[2][2]),
        row("epoch2_command", transition_events[2][0]),
        row("epoch2_activation", transition_events[2][1]),
        row("epoch2_stable", stable_events[2][0]),
        row("epoch2_drain_complete", drain_event),
    ]
    timestamps = [int(item["source_monotonic_ns"]) for item in cutoffs]
    if timestamps != sorted(timestamps):
        raise FactorialExecutionError("native phase cutoffs are not monotonic")
    phases = [
        {
            "phase": "baseline",
            "start_monotonic_ns": baseline_event.timestamp_ns
            - spec.cutoff_contract.baseline_bucket_count * width_ns,
            "end_monotonic_ns": baseline_event.timestamp_ns,
            "bucket_count": spec.cutoff_contract.baseline_bucket_count,
            "configuration": {
                "epoch_number": baseline_configuration[0],
                "epoch_digest": baseline_configuration[1],
            },
        },
        {
            "phase": "fault_evidence",
            "start_monotonic_ns": fault_start,
            "end_monotonic_ns": fault_evidence_end,
            "bucket_count": spec.cutoff_contract.fault_evidence_bucket_count,
            "configuration": {
                "epoch_number": baseline_configuration[0],
                "epoch_digest": baseline_configuration[1],
            },
        },
        {
            "phase": "epoch1_stable",
            "start_monotonic_ns": stable_events[1][0].timestamp_ns,
            "end_monotonic_ns": stable_events[1][2],
            "bucket_count": spec.cutoff_contract.epoch1_stable_bucket_count,
            "configuration": {
                "epoch_number": phase_configurations[1][0],
                "epoch_digest": phase_configurations[1][1],
            },
        },
        {
            "phase": "epoch2_stable",
            "start_monotonic_ns": stable_events[2][0].timestamp_ns,
            "end_monotonic_ns": stable_events[2][2],
            "bucket_count": spec.cutoff_contract.epoch2_stable_bucket_count,
            "configuration": {
                "epoch_number": phase_configurations[2][0],
                "epoch_digest": phase_configurations[2][1],
            },
        },
    ]
    return {
        "schema_version": 1,
        "slot_id": spec.slot_id,
        "cutoff_rule": spec.cutoff_contract.actual_cutoff_validation_rule,
        "cutoffs": cutoffs,
        "phases": phases,
        "phase_qualifications": [
            {"phase": "baseline", "common_commit": dict(baseline_common)},
            {"phase": "epoch1_stable", "common_commit": dict(stable_events[1][1])},
            {"phase": "epoch2_stable", "common_commit": dict(stable_events[2][1])},
        ],
    }


def _assert_epoch2_completion_within_fault_window(
    spec: SlotRuntimeSpec,
    phase_cutoffs: Mapping[str, object],
    *,
    shared_raw_clock_anchor_ns: int,
) -> None:
    phases_raw = phase_cutoffs.get("phases")
    cutoffs_raw = phase_cutoffs.get("cutoffs")
    if (
        isinstance(phases_raw, (str, bytes))
        or not isinstance(phases_raw, Sequence)
        or isinstance(cutoffs_raw, (str, bytes))
        or not isinstance(cutoffs_raw, Sequence)
    ):
        raise FactorialExecutionError(
            "observed phases do not contain epoch-2 completion evidence"
        )
    epoch2_phases = [
        item
        for item in phases_raw
        if isinstance(item, Mapping) and item.get("phase") == "epoch2_stable"
    ]
    drain_cutoffs = [
        item
        for item in cutoffs_raw
        if isinstance(item, Mapping)
        and item.get("name") == "epoch2_drain_complete"
    ]
    if len(epoch2_phases) != 1 or len(drain_cutoffs) != 1:
        raise FactorialExecutionError(
            "observed phases do not contain one exact epoch-2 stable/drain boundary"
        )
    stable_end_ns = epoch2_phases[0].get("end_monotonic_ns")
    drain_ns = drain_cutoffs[0].get("source_monotonic_ns")
    if (
        type(stable_end_ns) is not int
        or stable_end_ns <= 0
        or type(drain_ns) is not int
        or drain_ns <= 0
    ):
        raise FactorialExecutionError(
            "epoch-2 stable/drain completion timestamps are malformed"
        )
    fault_window_end_ns = shared_raw_clock_anchor_ns + (
        spec.fault_window.start_after_prelaunch_anchor_s
        + spec.fault_window.duration_s
    ) * NANOSECONDS_PER_SECOND
    if not (
        stable_end_ns < fault_window_end_ns
        and drain_ns < fault_window_end_ns
    ):
        raise IncompleteFactorialSlot(
            "epoch-2 stable window and drain must finish strictly before "
            "the fault window ends"
        )


def _throughput_document(
    spec: SlotRuntimeSpec,
    slot_directory: Path,
    phase_cutoffs: Mapping[str, object],
) -> dict[str, object]:
    streams = read_event_streams(spec, slot_directory, allow_partial=False)
    observer_events = streams[spec.structured_events.commit_observer_id]
    phases_raw = phase_cutoffs.get("phases")
    if isinstance(phases_raw, (str, bytes)) or not isinstance(phases_raw, Sequence):
        raise FactorialExecutionError("phase cutoffs lack phase windows")
    phase_by_name: dict[str, Mapping[str, object]] = {}
    for value in phases_raw:
        if not isinstance(value, Mapping) or not isinstance(value.get("phase"), str):
            raise FactorialExecutionError("phase cutoff window is malformed")
        phase_by_name[str(value["phase"])] = value
    width_s = spec.cutoff_contract.bucket_width_s
    width_ns = width_s * NANOSECONDS_PER_SECOND
    phases: list[dict[str, object]] = []
    for name in ("baseline", "fault_evidence", "epoch1_stable", "epoch2_stable"):
        window = phase_by_name.get(name)
        if not isinstance(window, Mapping):
            raise FactorialExecutionError(f"phase cutoffs omit {name}")
        start = window.get("start_monotonic_ns")
        end = window.get("end_monotonic_ns")
        count = window.get("bucket_count")
        raw_configuration = window.get("configuration")
        if type(start) is not int or type(end) is not int or type(count) is not int:
            raise FactorialExecutionError(f"phase {name} boundaries are malformed")
        if not isinstance(raw_configuration, Mapping) or set(raw_configuration) != {
            "epoch_number",
            "epoch_digest",
        }:
            raise FactorialExecutionError(f"phase {name} configuration is malformed")
        expected_configuration = (
            raw_configuration.get("epoch_number"),
            raw_configuration.get("epoch_digest"),
        )
        if (
            type(expected_configuration[0]) is not int
            or expected_configuration[0] < 0
            or not isinstance(expected_configuration[1], str)
            or len(expected_configuration[1]) != 64
            or any(
                character not in _HEX_DIGITS
                for character in expected_configuration[1]
            )
        ):
            raise FactorialExecutionError(f"phase {name} configuration is malformed")
        transactions = [0] * count
        seen: set[tuple[object, object]] = set()
        for event in observer_events:
            if (
                event.value.get("event_type") != "block.committed"
                or not start <= event.timestamp_ns < end
            ):
                continue
            if not _commit_has_configuration(event, expected_configuration):
                raise FactorialExecutionError(
                    f"phase {name} contains a commit outside its exact configuration"
                )
            key = _commit_key(event)
            identity = key[:2]
            if identity in seen:
                raise FactorialExecutionError(
                    f"duplicate authoritative commit in phase {name}"
                )
            seen.add(identity)
            transactions[(event.timestamp_ns - start) // width_ns] += int(key[3])
        buckets = [
            {
                "bucket_index": index,
                "start_monotonic_ns": start + index * width_ns,
                "end_monotonic_ns": start + (index + 1) * width_ns,
                "transactions": value,
                "tps": value / width_s,
            }
            for index, value in enumerate(transactions)
        ]
        total = sum(transactions)
        phases.append(
            {
                "phase": name,
                "start_monotonic_ns": start,
                "end_monotonic_ns": end,
                "configuration": dict(raw_configuration),
                "buckets": buckets,
                "transactions": total,
                "mean_tps": total / (count * width_s),
            }
        )
    return {
        "schema_version": 1,
        "slot_id": spec.slot_id,
        "bucket_width_s": width_s,
        "authority": {
            "event_type": "block.committed",
            "source_id": spec.structured_events.commit_observer_id,
            "source_instance": spec.structured_events.commit_observer_instance,
            "unique_commit_rule": "block_height_and_hash_exactly_once_v1",
        },
        "phases": phases,
    }


def _create_slot_directories(slot_directory: Path, spec: SlotRuntimeSpec) -> None:
    slot_directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    slot_directory.mkdir(mode=0o700)
    for relative in ("runtime", "raw", "raw/process", "transitions"):
        (slot_directory / relative).mkdir(mode=0o700)
    for transition in spec.transitions:
        (slot_directory / transition.bundle_relative_path).parent.mkdir(
            parents=True,
            exist_ok=False,
            mode=0o700,
        )


def _launch_receipt(
    spec: SlotRuntimeSpec,
    materialized: MaterializedLaunch,
    *,
    anchor_ns: int,
    manifest_sha256: str,
    plan_sha256: str,
    runtime_sha256: str,
) -> dict[str, object]:
    start_ns = anchor_ns + (
        spec.fault_window.start_after_prelaunch_anchor_s * NANOSECONDS_PER_SECOND
    )
    end_ns = start_ns + spec.fault_window.duration_s * NANOSECONDS_PER_SECOND
    manager_argv = materialized.redacted_manager_argv
    evidence_start_option = "--fault-containment-evidence-start-monotonic-ns"
    coverage_option = "--fault-containment-required-tree-coverage"
    coverage_enabled = (
        spec.causal_acceptance.precontainment_fault_coverage_gate
        == PRECONTAINMENT_FAULT_COVERAGE_GATE_V1
    )
    if coverage_enabled:
        if (
            manager_argv.count(evidence_start_option) != 1
            or manager_argv[manager_argv.index(evidence_start_option) + 1]
            != str(start_ns)
            or manager_argv.count(coverage_option) != 1
            or manager_argv[manager_argv.index(coverage_option) + 1]
            != str(spec.replica_count)
        ):
            raise FactorialExecutionError(
                "launch receipt cannot seal drifted precontainment coverage argv"
            )
    elif evidence_start_option in manager_argv or coverage_option in manager_argv:
        raise FactorialExecutionError(
            "legacy launch receipt must not seal precontainment coverage argv"
        )
    return {
        "schema_version": 1,
        "slot_id": spec.slot_id,
        "runtime_artifact_id": spec.artifact_id,
        "manifest_sha256": manifest_sha256,
        "plan_sha256": plan_sha256,
        "runtime_sha256": runtime_sha256,
        "execution_ordinal": spec.execution_ordinal,
        "attempt_ordinal": 1,
        "retry_of": None,
        "replacement_for": None,
        "shared_raw_clock_anchor_ns": anchor_ns,
        "fault_window_start_ns": start_ns,
        "fault_window_end_ns": end_ns,
        "redaction_key_id": materialized.redaction_key_id,
        "manager_argv": list(materialized.redacted_manager_argv),
        "replica_argv": [
            {
                "replica_id": process.replica_id,
                "argv": list(process.argv),
            }
            for process in materialized.redacted_replica_argv
        ],
    }


def _launch_binding(
    receipt: Mapping[str, object],
    input_artifacts: Sequence[Mapping[str, object]],
    binaries: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    input_hashes: list[dict[str, str]] = []
    for row in input_artifacts:
        relative_path = row.get("relative_path")
        digest = row.get("sha256")
        if not isinstance(relative_path, str) or not isinstance(digest, str):
            raise FactorialExecutionError(
                "launch binding input artifact hash row is malformed"
            )
        input_hashes.append(
            {
                "relative_path": relative_path,
                "sha256": digest,
            }
        )
    expected_binary_names = {"app", "manager", "keygen", "tls_keygen"}
    if set(binaries) != expected_binary_names:
        raise FactorialExecutionError("launch binding binary membership drifted")
    executable_hashes: dict[str, dict[str, str]] = {}
    for name in sorted(expected_binary_names):
        row = binaries[name]
        path = row.get("path")
        digest = row.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in _HEX_DIGITS for character in digest)
        ):
            raise FactorialExecutionError(
                f"launch binding binary identity is malformed: {name}"
            )
        executable_hashes[name] = {"path": path, "sha256": digest}
    payload = {
        "redacted_receipt_sha256": _sha256_bytes(
            _canonical_json_bytes(receipt)
        ),
        "input_hashes": input_hashes,
        "executables": executable_hashes,
    }
    return {
        "algorithm": "sha256",
        "canonicalization": "sorted_key_compact_json_utf8_lf_v1",
        "payload": payload,
        "sha256": _sha256_bytes(_canonical_json_bytes(payload)),
    }


def _final_expected_clean_exits(
    spec: SlotRuntimeSpec,
    slot_directory: Path,
) -> dict[str, Mapping[str, object]]:
    streams = read_event_streams(spec, slot_directory, allow_partial=False)
    terminal = _successful_manager_terminal(streams, cycle_ordinal=1)
    if terminal is None:
        return {}
    return {"adaptive-manager": terminal.reference()}


def _capture_cleanup_sample(
    record: ProcessRecord,
    *,
    slot_directory: Path,
    platform_name: str,
    run_command: Callable[..., Any],
) -> CleanupEscalationResult:
    """Capture one bounded macOS sample before cleanup escalates past SIGINT."""

    if platform_name != "darwin":
        return CleanupEscalationResult(
            status="unsupported_platform",
            artifact_relative_path=None,
            error=f"/usr/bin/sample is unavailable on {platform_name}",
        )
    if Path(record.name).name != record.name or record.name in {".", ".."}:
        return CleanupEscalationResult(
            status="failed",
            artifact_relative_path=None,
            error="process name is unsafe for a diagnostic artifact",
        )

    relative_path = Path("raw/diagnostics") / f"{record.name}.sample.txt"
    output_path = slot_directory / relative_path
    diagnostic_directory = output_path.parent
    try:
        diagnostic_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        return CleanupEscalationResult(
            status="failed",
            artifact_relative_path=None,
            error=f"could not create sample directory: {error}",
        )
    if (
        diagnostic_directory.is_symlink()
        or not diagnostic_directory.is_dir()
        or output_path.exists()
        or output_path.is_symlink()
    ):
        return CleanupEscalationResult(
            status="failed",
            artifact_relative_path=None,
            error="sample output path is unsafe or already exists",
        )

    command = [
        "/usr/bin/sample",
        str(record.pid),
        str(CLEANUP_SAMPLE_DURATION_S),
        str(CLEANUP_SAMPLE_INTERVAL_MS),
        "-mayDie",
        "-fullPaths",
        "-file",
        str(output_path),
    ]
    try:
        completed = run_command(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=CLEANUP_SAMPLE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError) as error:
        return CleanupEscalationResult(
            status="failed",
            artifact_relative_path=(
                relative_path.as_posix()
                if output_path.is_file() and not output_path.is_symlink()
                else None
            ),
            error=f"could not invoke /usr/bin/sample: {error}",
        )

    returncode = getattr(completed, "returncode", None)
    artifact_relative_path = (
        relative_path.as_posix()
        if output_path.is_file() and not output_path.is_symlink()
        else None
    )
    if returncode != 0:
        stderr = getattr(completed, "stderr", "")
        stdout = getattr(completed, "stdout", "")
        detail = stderr.strip() if isinstance(stderr, str) else ""
        if not detail and isinstance(stdout, str):
            detail = stdout.strip()
        suffix = f": {detail[:2048]}" if detail else ""
        return CleanupEscalationResult(
            status="failed",
            artifact_relative_path=artifact_relative_path,
            error=f"sample exited with status {returncode}{suffix}",
        )
    if artifact_relative_path is None:
        return CleanupEscalationResult(
            status="failed",
            artifact_relative_path=None,
            error="sample exited successfully without a regular output file",
        )
    return CleanupEscalationResult(
        status="captured",
        artifact_relative_path=artifact_relative_path,
        error=None,
    )


def _write_cleanup_escalation_diagnostics(
    slot_directory: Path,
    slot_id: str,
    attempts: Sequence[CleanupEscalationOutcome],
) -> None:
    """Persist every pre-escalation probe so the terminal seal covers it."""

    if not attempts:
        return
    rows: list[dict[str, object]] = []
    for attempt in attempts:
        if not isinstance(attempt, CleanupEscalationOutcome):
            raise FactorialExecutionError(
                "cleanup escalation diagnostics contain an invalid row"
            )
        rows.append(
            {
                "name": attempt.name,
                "replica_id": (
                    attempt.replica_id if attempt.replica_id >= 0 else None
                ),
                "pid": attempt.pid,
                "pgid": attempt.pgid,
                "after_signal_number": attempt.after_signal_number,
                "before_signal_number": attempt.before_signal_number,
                "status": attempt.status,
                "artifact_relative_path": attempt.artifact_relative_path,
                "error": attempt.error,
            }
        )
    _write_exclusive(
        slot_directory / CLEANUP_DIAGNOSTICS_RELATIVE_PATH,
        _canonical_json_bytes(
            {
                "schema_version": 1,
                "slot_id": slot_id,
                "attempts": rows,
            }
        ),
    )


def _cleanup_ledger(
    records: Sequence[ProcessRecord],
    outcomes: Sequence[CleanupOutcome],
    *,
    cleanup_started_ns: int,
    expected_clean_exits: Mapping[str, Mapping[str, object]] | None = None,
    cleanup_completed: bool = True,
    injected_replica_ids: Collection[int] = (),
) -> tuple[Mapping[str, object], ...]:
    by_name: dict[str, CleanupOutcome] = {}
    duplicate_outcome_names: set[str] = set()
    for outcome in outcomes:
        if outcome.name in by_name:
            duplicate_outcome_names.add(outcome.name)
        by_name[outcome.name] = outcome
    registered_names = {record.name for record in records}
    outcome_set_has_unknown_names = not set(by_name).issubset(registered_names)
    authorizations = expected_clean_exits or {}
    injected = frozenset(injected_replica_ids)
    rows: list[Mapping[str, object]] = []

    def credible_cleanup(
        record: ProcessRecord,
        outcome: CleanupOutcome,
        returncode: int,
    ) -> bool:
        signal_number = outcome.signal_number
        if (
            type(signal_number) is not int
            or type(returncode) is not int
            or type(outcome.returncode) is not int
            or outcome.name != record.name
            or outcome.replica_id != record.replica_id
            or outcome.pid != record.pid
            or outcome.pgid != record.pgid
            or outcome.returncode != returncode
            or signal_number
            not in (int(signal.SIGINT), int(signal.SIGTERM), int(signal.SIGKILL))
        ):
            return False
        if signal_number == int(signal.SIGKILL):
            return returncode == -signal_number
        return returncode in (0, -signal_number)

    for record in records:
        outcome = by_name.get(record.name)
        returncode = record.process.poll()
        authorization = authorizations.get(record.name)
        if (
            outcome is None
            and record.replica_id in injected
            and returncode == -int(signal.SIGKILL)
        ):
            classification = "expected_injected_fault"
        elif (
            outcome is not None
            and returncode is not None
            and record.name not in duplicate_outcome_names
            and not outcome_set_has_unknown_names
            and credible_cleanup(record, outcome, returncode)
        ):
            if (
                record.replica_id >= 0
                and outcome.signal_number
                in (int(signal.SIGTERM), int(signal.SIGKILL))
            ):
                classification = "unexpected_cleanup_escalation"
            else:
                classification = "expected_cleanup"
        elif outcome is not None and returncode is not None:
            classification = "unexpected_cleanup_exit"
        elif authorization is not None and returncode == 0:
            classification = "expected_clean_exit"
        elif not cleanup_completed and returncode is not None:
            classification = "cleanup_outcome_unavailable"
        elif returncode is not None:
            classification = "unexpected_precleanup_exit"
        else:
            classification = "cleanup_incomplete"
        rows.append(
            {
                "name": record.name,
                "replica_id": record.replica_id if record.replica_id >= 0 else None,
                "pid": record.pid,
                "pgid": record.pgid,
                "cleanup_started_monotonic_ns": cleanup_started_ns,
                "signal_number": outcome.signal_number if outcome else None,
                "returncode": returncode,
                "classification": classification,
                "exit_authorization": (
                    dict(authorization)
                    if classification == "expected_clean_exit"
                    else None
                ),
            }
        )
    return tuple(rows)


def _seal_files(slot_directory: Path, *, exclude: frozenset[str]) -> dict[str, str]:
    rows: dict[str, str] = {}
    for path in sorted(slot_directory.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = str(path.relative_to(slot_directory))
        if relative in exclude:
            continue
        rows[relative] = _sha256_file(path)
    return rows


def execute_slot_once(
    slot: FactorialSlot,
    spec: SlotRuntimeSpec,
    *,
    preflight: ExecutionPreflight,
    static_artifacts: Mapping[str, bytes],
    authorization_receipt: bytes,
    campaign_member: bool,
    raw_now_ns: Callable[[], int] = monotonic_raw_ns,
    wall_now: Callable[[], str] = _utc_now,
    run_command: Callable[..., Any] = subprocess.run,
    popen_factory: Callable[..., _ProcessLike] = subprocess.Popen,
    registry_factory: Callable[..., ProcessRegistry] = ProcessRegistry,
    observer: Callable[..., Mapping[str, object]] = observe_slot_phases,
    sleep: Callable[[float], None] = time.sleep,
    wait_ports_clear: Callable[[Sequence[int], float], None] = (
        _legacy_runtime.wait_ports_clear
    ),
    cleanup_timeout_s: float = DEFAULT_CLEANUP_TIMEOUT_S,
    sample_run_command: Callable[..., Any] = subprocess.run,
    cleanup_sample_platform: str = sys.platform,
) -> SlotExecutionResult:
    """Execute and preserve exactly one slot attempt with no retry path."""

    if preflight.slot_directory.exists():
        raise FactorialExecutionError(
            f"slot result collision; refusing reuse: {preflight.slot_directory}"
        )
    if preflight.slot_directory.name != spec.slot_id or slot.slot_id != spec.slot_id:
        raise FactorialExecutionError("slot/preflight/runtime identity mismatch")
    if preflight.slot_directory.parent != preflight.result_root:
        raise FactorialExecutionError("slot/preflight result-root identity mismatch")
    if campaign_member and slot.replica_count not in (13, 22, 31):
        raise FactorialExecutionError("campaign slot has an excluded replica count")
    if not campaign_member and slot.replica_count not in {7, 31}:
        raise FactorialExecutionError(
            "only the exact N=7 or N=31 coverage smoke may be non-campaign"
        )
    static_artifacts = _bind_static_artifacts(
        slot,
        spec,
        static_artifacts,
        campaign_member=campaign_member,
    )
    authorization = _bind_execution_authorization(
        authorization_receipt,
        slot=slot,
        preflight=preflight,
        static_artifacts=static_artifacts,
        campaign_member=campaign_member,
    )
    root_lock: int | None = None
    if campaign_member:
        root_lock = _acquire_campaign_root_lock(preflight.result_root)
    try:
        if campaign_member:
            _validate_campaign_launch_order(
                spec=spec,
                preflight=preflight,
                static_artifacts=static_artifacts,
                authorization_receipt=authorization_receipt,
                authorization=authorization,
            )
        verify_preserved_build_evidence(
            preflight.result_root,
            preflight.build_provenance,
        )
        execution_binaries = _preserved_execution_binaries(preflight.result_root)
        slot_directory = preflight.slot_directory
        if slot_directory.exists() or slot_directory.is_symlink():
            raise FactorialExecutionError(
                f"slot result collision; refusing reuse: {slot_directory}"
            )
        _create_slot_directories(slot_directory, spec)
        if root_lock is not None:
            _assert_campaign_root_identity(preflight.result_root, root_lock)
    finally:
        if root_lock is not None:
            _release_campaign_root_lock(root_lock)
    for relative, payload in static_artifacts.items():
        _write_exclusive(slot_directory / relative, bytes(payload))
    _write_exclusive(
        slot_directory / "execution-authorization.json",
        authorization_receipt,
    )
    _write_exclusive(
        slot_directory / "runtime/exact-build-provenance.json",
        _canonical_json_bytes(preflight.build_provenance),
    )
    state_path = slot_directory / "runner-state.jsonl"
    outcome_path = slot_directory / "outcome.json"
    history: list[dict[str, object]] = [
        {
            "sequence": 0,
            "state": "NOT_STARTED",
            "reason": None,
        }
    ]
    _replace_json(
        outcome_path,
        {
            "schema_version": 1,
            "slot_id": spec.slot_id,
            "history": history,
            "sealed_files": {},
        },
    )

    phase_cutoffs: Mapping[str, object] | None = None
    runtime_error: str | None = None
    cleanup_error: str | None = None
    cleanup_rows: tuple[Mapping[str, object], ...] = ()
    records: list[ProcessRecord] = []
    spawned: list[SpawnedProcess] = []
    launch_count = 0
    registry = registry_factory(
        monotonic_ns=raw_now_ns,
        cleanup_escalation_hook=lambda record: _capture_cleanup_sample(
            record,
            slot_directory=preflight.slot_directory,
            platform_name=cleanup_sample_platform,
            run_command=sample_run_command,
        ),
    )
    anchor_ns: int | None = None
    hard_deadline_ns: int | None = None

    def state(phase: str, **extra: object) -> None:
        _append_jsonl(
            state_path,
            {
                "schema_version": 1,
                "slot_id": spec.slot_id,
                "phase": phase,
                "recorded_utc": wall_now(),
                "recorded_monotonic_ns": raw_now_ns(),
                **extra,
            },
        )

    try:
        state("identity_generation")
        identities = generate_identities(
            spec,
            binaries=execution_binaries,
            runtime_directory=slot_directory / "runtime",
            run_command=run_command,
        )
        input_artifacts = write_slot_configs(
            slot,
            spec,
            slot_directory=slot_directory,
            identities=identities,
        )
        # The only raw-clock sample used to materialize the Byzantine window is
        # deliberately after all identity/config preparation and before argv
        # binding, the launch receipt, and process creation.
        anchor_ns = raw_now_ns()
        hard_deadline_ns = anchor_ns + (
            spec.fault_window.hard_timeout_s * NANOSECONDS_PER_SECOND
        )
        materialized = materialize_launch(
            slot,
            spec,
            slot_directory=slot_directory,
            binaries=execution_binaries,
            identities=identities,
            input_artifacts=input_artifacts,
            shared_raw_clock_anchor_ns=anchor_ns,
            redaction_key=_derive_redaction_key(
                authorization_receipt,
                spec.slot_id,
            ),
        )
        receipt = _launch_receipt(
            spec,
            materialized,
            anchor_ns=anchor_ns,
            manifest_sha256=_sha256_bytes(static_artifacts["manifest.json"]),
            plan_sha256=_sha256_bytes(static_artifacts["plan.json"]),
            runtime_sha256=_sha256_bytes(static_artifacts["runtime.json"]),
        )
        slot_receipt_bytes = _canonical_json_bytes(receipt)
        _write_exclusive(slot_directory / "slot.json", slot_receipt_bytes)
        binary_identities = {
            name: {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            for name, path in execution_binaries.as_mapping().items()
        }
        _write_exclusive(
            slot_directory / "runtime/execution-provenance.json",
            _canonical_json_bytes(
                {
                    "schema_version": 1,
                    "slot_id": spec.slot_id,
                    "campaign_member": campaign_member,
                    "figure_eligible": campaign_member,
                    "denominator_contribution": 1 if campaign_member else 0,
                    "kauri_revision": preflight.revision,
                    "build_provenance_sha256": _sha256_bytes(
                        _canonical_json_bytes(preflight.build_provenance)
                    ),
                    "execution_authorization_id": authorization["authorization_id"],
                    "execution_authorization_sha256": _sha256_bytes(
                        authorization_receipt
                    ),
                    "binaries": binary_identities,
                    "input_artifacts": list(materialized.input_artifacts),
                    "launch_binding": _launch_binding(
                        receipt,
                        materialized.input_artifacts,
                        binary_identities,
                    ),
                    "slot_receipt_sha256": _sha256_bytes(slot_receipt_bytes),
                    "attempt_ordinal": 1,
                    "automatic_retries": 0,
                    "replacement_policy": "none",
                }
            ),
        )
        state("launch", shared_raw_clock_anchor_ns=anchor_ns)
        manager = spawn_exclusive_owned_process(
            registry,
            name="adaptive-manager",
            replica_id=MANAGER_REPLICA_ID,
            command=materialized.manager_argv,
            stdout_path=slot_directory / spec.process_logs.manager_stdout_relative_path,
            stderr_path=slot_directory / spec.process_logs.manager_stderr_relative_path,
            working_directory=slot_directory,
            popen_factory=popen_factory,
        )
        spawned.append(manager)
        records.append(manager.record)
        launch_count += 1
        for process in materialized.replica_argv:
            replica_id = process.replica_id
            launched = spawn_exclusive_owned_process(
                registry,
                name=f"replica-{replica_id}",
                replica_id=replica_id,
                command=process.argv,
                stdout_path=slot_directory
                / spec.process_logs.replica_stdout_relative_paths[replica_id],
                stderr_path=slot_directory
                / spec.process_logs.replica_stderr_relative_paths[replica_id],
                working_directory=slot_directory,
                popen_factory=popen_factory,
            )
            spawned.append(launched)
            records.append(launched.record)
            launch_count += 1
        if launch_count != spec.replica_count + 1:
            raise FactorialExecutionError("exact one-shot launch cardinality drifted")
        state("observing", launch_count=launch_count)
        observed_phase_cutoffs = observer(
            spec,
            slot_directory,
            records,
            shared_raw_clock_anchor_ns=anchor_ns,
            hard_deadline_ns=hard_deadline_ns,
            raw_now_ns=raw_now_ns,
            sleep=sleep,
        )
        _assert_epoch2_completion_within_fault_window(
            spec,
            observed_phase_cutoffs,
            shared_raw_clock_anchor_ns=anchor_ns,
        )
        _write_exclusive(
            slot_directory / "phase-cutoffs.json",
            _canonical_json_bytes(observed_phase_cutoffs),
        )
        phase_cutoffs = observed_phase_cutoffs
        state("qualified_pending_cleanup")
    except (KeyboardInterrupt, OSError, ValueError, subprocess.SubprocessError) as error:
        runtime_error = f"{type(error).__name__}: {error}"
    except FactorialExecutionError as error:
        runtime_error = str(error)
    except Exception as error:  # preserve unexpected one-attempt failures too
        runtime_error = f"{type(error).__name__}: {error}"
    finally:
        cleanup_started_ns = raw_now_ns()
        outcomes: tuple[CleanupOutcome, ...] = ()
        cleanup_completed = False
        try:
            outcomes = registry.cleanup(timeout_s=cleanup_timeout_s)
            cleanup_completed = True
        except (Exception, KeyboardInterrupt) as error:
            cleanup_error = str(error) or type(error).__name__
        try:
            _write_cleanup_escalation_diagnostics(
                slot_directory,
                spec.slot_id,
                tuple(getattr(registry, "cleanup_escalations", ())),
            )
        except (FactorialExecutionError, OSError, ValueError) as error:
            cleanup_error = cleanup_error or (
                f"cleanup diagnostic persistence failed: {error}"
            )
        streams_closed = True
        for launched in spawned:
            for stream in (launched.stdout, launched.stderr):
                try:
                    stream.close()
                except OSError as error:
                    streams_closed = False
                    cleanup_error = cleanup_error or str(error)
        ports_clear = True
        try:
            wait_ports_clear(slot_ports(slot), cleanup_timeout_s)
        except Exception as error:
            ports_clear = False
            cleanup_error = cleanup_error or str(error)

        expected_clean_exits: dict[str, Mapping[str, object]] = {}
        final_streams_complete = False
        if cleanup_completed and streams_closed:
            try:
                expected_clean_exits = _final_expected_clean_exits(
                    spec,
                    slot_directory,
                )
                final_streams_complete = True
            except (FactorialExecutionError, OSError, ValueError) as error:
                cleanup_error = cleanup_error or (
                    f"final structured-event read failed: {error}"
                )
        cleanup_rows = _cleanup_ledger(
            records,
            outcomes,
            cleanup_started_ns=cleanup_started_ns,
            expected_clean_exits=expected_clean_exits,
            cleanup_completed=cleanup_completed,
            injected_replica_ids=getattr(
                registry,
                "injected_sigkill_replica_ids",
                (),
            ),
        )
        accepted_cleanup_classes = {
            "expected_cleanup",
            "expected_clean_exit",
            "expected_injected_fault",
        }
        if cleanup_completed and any(
            row["classification"] not in accepted_cleanup_classes
            for row in cleanup_rows
        ):
            cleanup_error = cleanup_error or (
                "cleanup ledger contains a non-expected process exit"
            )

        if (
            phase_cutoffs is not None
            and cleanup_completed
            and streams_closed
            and ports_clear
            and final_streams_complete
        ):
            try:
                throughput = _throughput_document(
                    spec,
                    slot_directory,
                    phase_cutoffs,
                )
                _write_exclusive(
                    slot_directory / "throughput.json",
                    _canonical_json_bytes(throughput),
                )
            except (FactorialExecutionError, OSError, ValueError) as error:
                runtime_error = runtime_error or (
                    f"final throughput extraction failed: {error}"
                )
        _write_exclusive(
            slot_directory / "cleanup-ledger.json",
            _canonical_json_bytes(
                {
                    "schema_version": 1,
                    "slot_id": spec.slot_id,
                    "cleanup_started_monotonic_ns": cleanup_started_ns,
                    "cleanup_completed": cleanup_completed,
                    "streams_closed": streams_closed,
                    "ports_clear": ports_clear,
                    "final_streams_complete": final_streams_complete,
                    "processes": list(cleanup_rows),
                    "error": cleanup_error,
                }
            ),
        )

    if phase_cutoffs is None:
        _write_exclusive(
            slot_directory / "phase-cutoffs.json",
            _canonical_json_bytes(
                {
                    "schema_version": 1,
                    "slot_id": spec.slot_id,
                    "cutoff_rule": spec.cutoff_contract.actual_cutoff_validation_rule,
                    "cutoffs": [],
                    "phases": [],
                    "phase_qualifications": [],
                }
            ),
        )
    terminal = "PASS" if runtime_error is None and cleanup_error is None else "INCOMPLETE"
    reason = runtime_error or cleanup_error
    history.append({"sequence": 1, "state": terminal, "reason": reason})
    state("terminal", outcome=terminal, reason=reason)
    seal = _seal_files(slot_directory, exclude=frozenset({"outcome.json"}))
    _replace_json(
        outcome_path,
        {
            "schema_version": 1,
            "slot_id": spec.slot_id,
            "history": history,
            "sealed_files": seal,
        },
    )
    return SlotExecutionResult(
        slot_directory=slot_directory,
        outcome=terminal,
        reason=reason,
        launch_count=launch_count,
        phase_cutoffs=phase_cutoffs,
        cleanup_ledger=cleanup_rows,
    )


def build_n7_ps_smoke_slot(
    template: FactorialSlot,
    *,
    scientific_seed: int = 41_700,
    peer_base: int = 45_100,
    client_base: int = 46_100,
    manager_port: int = 47_100,
    result_path: str | None = None,
) -> N7SmokeSlot:
    """Derive the excluded N=7, f=2, k=2, fanout-two PS smoke."""

    if not isinstance(template, FactorialSlot):
        raise FactorialExecutionError("N=7 smoke requires one frozen slot template")
    if result_path is None:
        template_path = Path(template.result_path)
        if (
            template_path.is_absolute()
            or ".." in template_path.parts
            or len(template_path.parts) < 3
            or template_path.parent.name == ""
        ):
            raise FactorialExecutionError(
                "N=7 smoke template result path is not an exact relative slot path"
            )
        campaign_root = template_path.parent
        result_path = str(
            campaign_root.parent
            / f"{campaign_root.name}-smoke"
            / "smoke-n7-f2-PS"
        )
    consensus = derive_consensus_shape(
        7,
        initial_fanout=2,
        candidate_fanouts=template.candidate_fanouts,
    )
    tiered = (
        template.byzantine.mode
        in {
            "tiered_persistent_responsive_omission_v1",
            "tiered_persistent_responsive_omission_v2",
        }
        and template.byzantine.responsive_degradation is not None
    )
    if tiered:
        cohorts = derive_tiered_cohorts(
            7,
            consensus.q,
            1,
            scientific_seed,
        )
        actors = cohorts.hard_actor_ids
        degraded = cohorts.responsive_degraded_actor_ids
        fast = cohorts.fast_replica_ids
        if (
            len(actors) != 1
            or len(degraded) != 1
            or len((*actors, *degraded)) != consensus.f
            or len(fast) != consensus.q
            or 0 not in fast
            or any(actor < consensus.q for actor in actors)
            or any(actor < 1 or actor >= consensus.q for actor in degraded)
        ):
            raise FactorialExecutionError("N=7 smoke tiered cohort derivation drifted")
        responsive_degradation = replace(
            template.byzantine.responsive_degradation,
            actor_selection_vectors=(),
        )
        actor_count_rule = "fixed_1_hard_actor_smoke_only"
        maximum = None
        maximum_rule = "derived_f_per_slot_v1"
    else:
        actors = derive_actor_ids(
            7,
            consensus.q,
            min(3, consensus.f),
            scientific_seed,
        )
        degraded = ()
        fast = ()
        responsive_degradation = None
        actor_count_rule = "min_3_derived_f_smoke_only"
        maximum = (
            len(actors)
            if template.byzantine.mode == "persistent_selected_omission_v1"
            else 1
        )
        maximum_rule = None
        if len(actors) != min(3, consensus.f) or any(
            actor < consensus.q for actor in actors
        ):
            raise FactorialExecutionError("N=7 legacy smoke actor derivation drifted")
    smoke = replace(
        template,
        ordinal=1,
        slot_nonce=0,
        slot_id="smoke-n7-f2-PS",
        block_id="n7-f2-smoke-b01",
        block_index=1,
        blocks_in_cell=1,
        block_execution_ordinal=1,
        arm_execution_position=1,
        execution_ordinal=1,
        scientific_seed=scientific_seed,
        consensus=consensus,
        arm=FactorialArm(
            code="PS",
            placement_adaptation=True,
            shape_adaptation=True,
        ),
        byzantine=replace(
            template.byzantine,
            actor_count=len(actors),
            actor_count_rule=actor_count_rule,
            actor_selection_vectors=(),
            responsive_degradation=responsive_degradation,
            max_omissions_per_proposal=maximum,
            max_omissions_per_proposal_rule=maximum_rule,
        ),
        byzantine_actor_ids=actors,
        responsive_degraded_actor_ids=degraded,
        fast_replica_ids=fast,
        max_omissions_per_proposal=(consensus.f if tiered else None),
        ports=PortAllocation(
            peer_base=peer_base,
            client_base=client_base,
            manager=manager_port,
        ),
        result_path=result_path,
    )
    return N7SmokeSlot(
        slot=smoke,
        runtime=build_slot_runtime(smoke),
        actor_count_rule=actor_count_rule,
    )


def build_n31_coverage_smoke_slot(
    template: FactorialSlot,
    *,
    result_path: str | None = None,
) -> N31CoverageSmokeSlot:
    """Derive the excluded N=31 coverage smoke from exact campaign slot 066."""

    if not isinstance(template, FactorialSlot):
        raise FactorialExecutionError(
            "N=31 coverage smoke requires one frozen slot template"
        )
    responsive = template.byzantine.responsive_degradation
    frozen_campaign_paths = {
        "results/shape-placement-factorial-v15/slot-066-n31-f5-b05-P": "v15",
        "results/shape-placement-factorial-v16/slot-066-n31-f5-b05-P": "v16",
        "results/shape-placement-factorial-v17/slot-066-n31-f5-b05-P": "v17",
    }
    manifest_version = frozen_campaign_paths.get(template.result_path)
    expected_timeout_eligibility = {
        "v15": RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1,
        "v16": RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2,
        "v17": RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2,
    }.get(manifest_version)
    expected_shape_evaluation_contract = (
        PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1
        if manifest_version == "v17"
        else None
    )
    if (
        manifest_version is None
        or template.slot_id != "slot-066-n31-f5-b05-P"
        or template.result_path
        != (
            f"results/shape-placement-factorial-{manifest_version}/"
            "slot-066-n31-f5-b05-P"
        )
        or template.ordinal != 66
        or template.execution_ordinal != 1
        or template.block_id != "n31-f5-b05"
        or template.block_index != 5
        or template.blocks_in_cell != 5
        or template.block_execution_ordinal != 1
        or template.arm_execution_position != 1
        or template.scientific_seed != 41_735
        or template.replica_count != 31
        or template.f != 10
        or template.q != 21
        or template.tree_count != 21
        or template.initial_fanout != 5
        or template.candidate_fanouts != (2, 3, 5)
        or template.arm_code != "P"
        or not template.placement_adaptation
        or template.shape_adaptation
        or template.byzantine_actor_ids != (22, 25, 29)
        or template.responsive_degraded_actor_ids != (1, 2, 3, 5, 7, 8, 16)
        or template.fast_replica_ids
        != (
            0,
            4,
            6,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
            17,
            18,
            19,
            20,
            21,
            23,
            24,
            26,
            27,
            28,
            30,
        )
        or template.maximum_omissions_per_proposal != 10
        or template.ports
        != PortAllocation(peer_base=31_600, client_base=32_600, manager=33_600)
        or responsive is None
        or responsive.precontainment_fault_coverage_gate
        != PRECONTAINMENT_FAULT_COVERAGE_GATE_V1
        or responsive.causal_timeout_eligibility
        != expected_timeout_eligibility
        or responsive.precontainment_shape_evaluation_contract
        != expected_shape_evaluation_contract
    ):
        raise FactorialExecutionError(
            "N=31 coverage smoke must derive from an exact frozen campaign "
            "slot 066"
        )
    expected_result_path = (
        f"results/shape-placement-factorial-{manifest_version}-coverage-smoke/"
        "slot-066-n31-f5-b05-P"
    )
    if result_path is None:
        result_path = expected_result_path
    if result_path != expected_result_path:
        raise FactorialExecutionError(
            "N=31 coverage smoke result path must be the exact canonical root"
        )
    smoke = replace(template, result_path=result_path)
    return N31CoverageSmokeSlot(
        slot=smoke,
        runtime=build_slot_runtime(smoke),
    )


__all__ = (
    "ExecutionBinaries",
    "ExecutionPreflight",
    "FactorialExecutionError",
    "IdentityMaterial",
    "IncompleteFactorialSlot",
    "MaterializedLaunch",
    "N7SmokeSlot",
    "N31CoverageSmokeSlot",
    "SlotExecutionResult",
    "build_execution_authorization_receipt",
    "build_n7_ps_smoke_slot",
    "build_n31_coverage_smoke_slot",
    "execute_slot_once",
    "generate_identities",
    "materialize_launch",
    "monotonic_raw_ns",
    "observe_slot_phases",
    "preserve_build_evidence",
    "read_event_streams",
    "slot_ports",
    "spawn_exclusive_owned_process",
    "verify_evidence_preflight",
    "verify_preserved_build_evidence",
    "write_slot_configs",
)
