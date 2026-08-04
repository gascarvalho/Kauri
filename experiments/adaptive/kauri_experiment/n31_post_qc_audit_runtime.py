"""One-attempt runtime and preserved-evidence validator for the v3 PQAR pilot.

Audit classification consumes only raw replica logs.  Manager events are used
for process ownership only and never as diagnostic observations.  The fixed
pilot order is sham, false-report, omission.  Returned outcomes cannot select,
replace, or retry an arm; an in-process exception spends its arm, seals the
partial parent as INCOMPLETE, and stops without attempting later arms.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import IO, Any
import uuid

from .n31_post_qc_audit import (
    ARM_FALSE_REPORT,
    ARM_OMISSION,
    ARM_SHAM,
    PILOT_EXECUTION_ORDER,
    SCENARIO,
    SHIPPED_PROFILE_ID,
    SHIPPED_PROFILE_SHA256,
    ConsensusEvidence,
    FrozenPqarProfile,
    N31PostQcAuditError,
    PqarValidation,
    SourceBlindClassification,
    build_launch_contract,
    classify_source_blind,
    load_frozen_profile,
    parse_replica_audit_logs,
    validate_ground_truth,
    validate_pilot,
)
from .n31_static_diagnosis_runtime import (
    TrustedBinary,
    TrustedProvenance,
    derive_trusted_provenance,
    load_trusted_provenance,
    write_trusted_provenance,
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


class N31PostQcAuditRuntimeError(RuntimeError):
    """The live or preserved PQAR pilot cannot qualify as evidence."""


_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[1-9][0-9]*-[0-9a-f]{8}$")
_MANIFEST_FIELDS = {
    "schema_version",
    "scenario",
    "run_id",
    "kauri_revision",
    "profile",
    "runtime_profile",
    "arm",
    "attempt",
    "retry_policy",
    "outcome_scanning",
    "complete",
    "started_utc",
    "finished_utc",
    "preflight",
    "source_instances",
    "ready_barrier_ns",
    "clean_boundary_ns",
    "log_offsets",
    "runtime_artifacts",
    "cleanup_ledger",
    "runtime_error",
}
_LOG_OFFSET_FIELDS = {"path", "sha256", "start_offset", "terminal_offset"}
_ONE_SHOT_LEDGER_FILENAME = "pqar-v3-one-shot-ledger.json"
_ONE_SHOT_LEDGER_FIELDS = {
    "schema_version",
    "scenario",
    "kind",
    "profile_id",
    "profile_sha256",
    "results_root",
    "sequence_directory",
    "allocated_utc",
    "state",
}


def _error(message: str) -> None:
    raise N31PostQcAuditRuntimeError(message)


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise N31PostQcAuditRuntimeError(f"{label} is unreadable") from error
    if not isinstance(value, dict):
        _error(f"{label} must be a JSON object")
    return value


def _canonical_document_sha256(value: object) -> str:
    payload = (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    return hashlib.sha256(payload).hexdigest()


def _load_bound_profiles(
    profile_path: Path, repository: Path
) -> tuple[FrozenPqarProfile, FrozenProfile, Path]:
    profile = load_frozen_profile(profile_path.resolve())
    runtime_path = (repository.resolve() / profile.runtime_profile_path).resolve()
    try:
        runtime_path.relative_to(repository.resolve())
    except ValueError as error:
        raise N31PostQcAuditRuntimeError(
            "bound runtime profile escapes the repository"
        ) from error
    runtime_profile = load_runtime_profile(runtime_path)
    if (
        runtime_profile.profile_id != profile.runtime_profile_id
        or runtime_profile.profile_sha256 != profile.runtime_profile_sha256
        or runtime_profile.replica_ids != profile.replica_ids
        or runtime_profile.fault_threshold != profile.fault_threshold
        or runtime_profile.quorum != profile.quorum
        or runtime_profile.fanout != profile.fanout
        or runtime_profile.pipeline_depth != profile.pipeline_stretch
        or runtime_profile.tree_switch_period_blocks
        != profile.tree_switch_period_blocks
    ):
        _error("runtime profile differs from the frozen PQAR binding")
    runtime.require_shipped_profile(runtime_profile)
    return profile, runtime_profile, runtime_path


def preflight(
    *,
    audit_profile_path: Path,
    repository: Path,
    app_binary: Path,
    manager_binary: Path,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    epoch_profile_digest_binary: Path,
    build_directory: Path,
    build_provenance_path: Path,
) -> dict[str, object]:
    """Verify exact build/profile provenance and freeze all three launches."""

    profile, runtime_profile, runtime_path = _load_bound_profiles(
        audit_profile_path, repository
    )
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
    return {
        "schema_version": 1,
        "scenario": SCENARIO,
        "verdict": "PASS",
        "audit_profile": {
            "profile_id": profile.profile_id,
            "sha256": profile.profile_sha256,
        },
        "runtime_profile": {
            "profile_id": runtime_profile.profile_id,
            "sha256": runtime_profile.profile_sha256,
            "path": str(runtime_path),
        },
        "revision": result["revision"],
        "commit_witnesses": list(profile.commit_witnesses),
        "pilot_execution_order": list(PILOT_EXECUTION_ORDER),
        "launch_contracts": {
            arm: build_launch_contract(profile, arm=arm)
            for arm in PILOT_EXECUTION_ORDER
        },
        "profiled_runtime": result,
    }


def _checked_preflight(
    profile: FrozenPqarProfile,
    runtime_profile: FrozenProfile,
    preflight_result: Mapping[str, object],
    trusted: TrustedProvenance,
) -> str:
    revision = preflight_result.get("revision")
    contracts = preflight_result.get("launch_contracts")
    runtime_record = preflight_result.get("runtime_profile")
    if (
        preflight_result.get("scenario") != SCENARIO
        or preflight_result.get("verdict") != "PASS"
        or revision != trusted.revision
        or preflight_result.get("audit_profile")
        != {"profile_id": profile.profile_id, "sha256": profile.profile_sha256}
        or not isinstance(runtime_record, Mapping)
        or runtime_record.get("profile_id") != runtime_profile.profile_id
        or runtime_record.get("sha256") != runtime_profile.profile_sha256
        or preflight_result.get("pilot_execution_order") != list(PILOT_EXECUTION_ORDER)
        or not isinstance(contracts, Mapping)
        or set(contracts) != set(PILOT_EXECUTION_ORDER)
    ):
        _error("launch input differs from the one frozen preflight")
    for arm in PILOT_EXECUTION_ORDER:
        if contracts[arm] != build_launch_contract(profile, arm=arm):
            _error("preflight launch contract drifted")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        _error("preflight revision is malformed")
    return revision


def _complete_log_offset(path: Path) -> int:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            payload = b""
        except OSError as error:
            raise N31PostQcAuditRuntimeError(f"cannot snapshot log: {path}") from error
        if not payload or payload.endswith(b"\n"):
            return len(payload)
        if time.monotonic() >= deadline:
            _error(f"log did not reach a complete-line boundary: {path.name}")
        time.sleep(0.005)


def _live_log_slices(
    run_directory: Path,
    profile: FrozenPqarProfile,
    start_offsets: Mapping[int, int],
) -> dict[int, str]:
    result: dict[int, str] = {}
    for replica in profile.replica_ids:
        path = run_directory / "logs" / f"replica-{replica}.log"
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise N31PostQcAuditRuntimeError(
                f"cannot read live replica-{replica} log"
            ) from error
        start = start_offsets[replica]
        if start > len(payload):
            _error("live log regressed behind its clean offset")
        if (
            b"KAURI_AUDIT " in payload[:start]
            or b"KAURI_FAULT direct_vote_omitted " in payload[:start]
            or b"KAURI_FAULT aggregate_omitted " in payload[:start]
        ):
            _error("audit/fault marker appeared before the ready log boundary")
        complete = payload.rfind(b"\n")
        terminal = start if complete < start else complete + 1
        result[replica] = payload[start:terminal].decode("utf-8", errors="replace")
    return result


def _audit_only_logs(replica_logs: Mapping[int, str]) -> dict[int, str]:
    """Remove all native fault ground truth before the blind pass."""

    return {
        replica: "\n".join(
            line for line in content.splitlines() if "KAURI_AUDIT " in line
        )
        for replica, content in replica_logs.items()
    }


def _poll_source_blind_classification(
    run_directory: Path,
    profile: FrozenPqarProfile,
    start_offsets: Mapping[int, int],
    *,
    clean_boundary_ns: int,
) -> SourceBlindClassification | None:
    logs = _audit_only_logs(_live_log_slices(run_directory, profile, start_offsets))
    markers = parse_replica_audit_logs(logs)
    snapshots = [marker for marker in markers if marker.kind == "root_snapshot"]
    prepared = [marker for marker in markers if marker.kind == "root_prepared"]
    if len(snapshots) == 0 or len(prepared) == 0:
        return None
    if len(snapshots) != 1 or len(prepared) != 1:
        _error("live audit produced multiple root candidates")
    try:
        expiry_ns = int(snapshots[0].fields["retention_deadline_ns"])
    except (KeyError, ValueError) as error:
        raise N31PostQcAuditRuntimeError(
            "live root snapshot retention is malformed"
        ) from error
    # Wait to the frozen expiry for every class.  This gives the same logs a
    # source-blind terminal boundary and avoids class-dependent early success.
    if runtime.monotonic_raw_ns() <= expiry_ns:
        return None
    return classify_source_blind(
        profile.source_blind_contract(),
        logs,
        clean_boundary_ns=clean_boundary_ns,
    )


def _validate_manifest(
    manifest: Mapping[str, object],
    *,
    run_directory: Path,
    profile: FrozenPqarProfile,
    runtime_profile: FrozenProfile,
) -> str:
    if set(manifest) != _MANIFEST_FIELDS:
        _error("manifest schema drifted")
    arm = manifest.get("arm")
    if not isinstance(arm, str):
        _error("manifest arm is absent")
    profile.arm(arm)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("scenario") != SCENARIO
        or manifest.get("run_id") != run_directory.name
        or manifest.get("profile")
        != {"profile_id": profile.profile_id, "sha256": profile.profile_sha256}
        or manifest.get("runtime_profile")
        != {
            "profile_id": runtime_profile.profile_id,
            "sha256": runtime_profile.profile_sha256,
        }
        or manifest.get("attempt") != 1
        or manifest.get("retry_policy") != "none"
        or manifest.get("outcome_scanning") is not False
    ):
        _error("manifest identity, attempt, or no-scan policy drifted")
    return arm


def _validate_source_blind_manifest(
    manifest: Mapping[str, object],
    *,
    run_directory: Path,
    profile: FrozenPqarProfile,
    runtime_profile: FrozenProfile,
) -> None:
    """Validate only arm-neutral child identity and execution policy fields."""

    if set(manifest) != _MANIFEST_FIELDS:
        _error("manifest schema drifted")
    run_id = manifest.get("run_id")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("scenario") != SCENARIO
        or not isinstance(run_id, str)
        or _RUN_ID.fullmatch(run_id) is None
        or manifest.get("profile")
        != {"profile_id": profile.profile_id, "sha256": profile.profile_sha256}
        or manifest.get("runtime_profile")
        != {
            "profile_id": runtime_profile.profile_id,
            "sha256": runtime_profile.profile_sha256,
        }
        or manifest.get("attempt") != 1
        or manifest.get("retry_policy") != "none"
        or manifest.get("outcome_scanning") is not False
    ):
        _error("manifest identity, attempt, or no-scan policy drifted")


def _verify_preserved_build_provenance(
    run_directory: Path,
    manifest: Mapping[str, object],
    trusted: TrustedProvenance,
) -> None:
    preflight_record = manifest.get("preflight")
    if not isinstance(preflight_record, Mapping):
        _error("manifest preflight is absent")
    profiled = preflight_record.get("profiled_runtime")
    if not isinstance(profiled, Mapping):
        _error("profiled runtime preflight is absent")
    provenance = profiled.get("build_provenance")
    executables = profiled.get("executables")
    expected_executables = {
        binary.name: {"path": binary.path, "sha256": binary.sha256}
        for binary in trusted.binaries
    }
    if (
        preflight_record.get("revision") != trusted.revision
        or profiled.get("revision") != trusted.revision
        or not isinstance(provenance, Mapping)
        or provenance.get("revision") != trusted.revision
        or provenance.get("repository") != trusted.repository
        or provenance.get("build_directory") != trusted.build_directory
        or _canonical_document_sha256(provenance)
        != trusted.build_provenance_document_sha256
        or executables != expected_executables
    ):
        _error("preserved preflight differs from trusted build provenance")
    copied_preflight = _read_json_object(
        run_directory / "runtime" / "preflight.json", "copied preflight"
    )
    if copied_preflight != dict(preflight_record):
        _error("copied preflight differs from manifest preflight")
    build_copy = run_directory / "runtime" / "build-provenance.json"
    if runtime.sha256_file(
        build_copy
    ) != trusted.build_provenance_file_sha256 or _read_json_object(
        build_copy, "copied build provenance"
    ) != dict(
        provenance
    ):
        _error("copied build provenance differs from the external receipt")


def _verify_preserved_launch_membership(
    run_directory: Path,
    profile: FrozenPqarProfile,
    trusted: TrustedProvenance,
) -> list[list[object]]:
    """Bind every source log to a trusted executable without reading its overlay."""

    launch = _read_json_object(
        run_directory / "runtime" / "launch-arguments.json", "launch arguments"
    )
    if set(launch) != {"schema_version", "manager", "replicas"}:
        _error("launch argument schema drifted")
    manager = launch.get("manager")
    replicas = launch.get("replicas")
    if (
        launch.get("schema_version") != 1
        or not isinstance(manager, list)
        or not manager
        or manager[0] != trusted.binary("manager").path
        or not isinstance(replicas, list)
        or len(replicas) != len(profile.replica_ids)
    ):
        _error("manager or replica launch membership drifted")
    checked: list[list[object]] = []
    for replica, command in zip(profile.replica_ids, replicas, strict=True):
        if (
            not isinstance(command, list)
            or not command
            or command[0] != trusted.binary("app").path
        ):
            _error(f"replica-{replica} actual launch argv drifted")
        checked.append(command)
    return checked


def _verify_runtime_artifact_inventory(
    run_directory: Path,
    manifest: Mapping[str, object],
) -> None:
    """Verify every sealed runtime artifact named by the child manifest."""

    artifacts = manifest.get("runtime_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        _error("runtime artifact inventory is absent")
    seen: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            _error("runtime artifact record is malformed")
        relative = artifact.get("path")
        digest = artifact.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or relative in seen
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(digest, str)
            or _HEX_256.fullmatch(digest) is None
        ):
            _error("runtime artifact identity is malformed")
        seen.add(relative)
        path = run_directory / relative
        if path.is_symlink() or runtime.sha256_file(path) != digest:
            _error("runtime artifact bytes drifted")
    required = {
        "profile.json",
        "runtime-profile.json",
        "runtime/preflight.json",
        "runtime/launch-contract.json",
        "runtime/build-provenance.json",
        "runtime/launch-arguments.json",
    }
    if not required <= seen:
        _error("runtime artifact inventory omits a provenance-critical file")


def _verify_preserved_provenance(
    run_directory: Path,
    manifest: Mapping[str, object],
    profile: FrozenPqarProfile,
    trusted: TrustedProvenance,
) -> None:
    _verify_preserved_build_provenance(run_directory, manifest, trusted)
    replica_commands = _verify_preserved_launch_membership(
        run_directory, profile, trusted
    )
    contract = build_launch_contract(profile, arm=str(manifest["arm"]))
    if (
        _read_json_object(
            run_directory / "runtime" / "launch-contract.json", "launch contract"
        )
        != contract
    ):
        _error("preserved launch contract drifted")
    overlays = contract["replica_arguments"]
    assert isinstance(overlays, Mapping)
    for replica, command in zip(profile.replica_ids, replica_commands, strict=True):
        overlay = overlays[str(replica)]
        if not isinstance(overlay, list) or command[-len(overlay) :] != overlay:
            _error(f"replica-{replica} actual launch argv drifted")
    _verify_runtime_artifact_inventory(run_directory, manifest)


def _validated_log_slices(
    run_directory: Path,
    manifest: Mapping[str, object],
    profile: FrozenPqarProfile,
) -> dict[int, str]:
    records = manifest.get("log_offsets")
    if not isinstance(records, Mapping) or set(records) != {
        str(replica) for replica in profile.replica_ids
    }:
        _error("manifest must bind exact N31 replica log offsets")
    result: dict[int, str] = {}
    for replica in profile.replica_ids:
        value = records[str(replica)]
        if not isinstance(value, Mapping) or set(value) != _LOG_OFFSET_FIELDS:
            _error(f"replica {replica} log-offset record drifted")
        relative = value.get("path")
        digest = value.get("sha256")
        start = value.get("start_offset")
        terminal = value.get("terminal_offset")
        if (
            relative != f"logs/replica-{replica}.log"
            or not isinstance(digest, str)
            or _HEX_256.fullmatch(digest) is None
            or type(start) is not int
            or type(terminal) is not int
            or start < 0
            or terminal <= start
        ):
            _error(f"replica {replica} log-offset identity is malformed")
        path = run_directory / str(relative)
        if path.is_symlink():
            _error("replica log must not be a symlink")
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise N31PostQcAuditRuntimeError(
                f"replica {replica} log is unreadable"
            ) from error
        if runtime.sha256_file(path) != digest or terminal > len(payload):
            _error(f"replica {replica} log bytes differ from the manifest")
        if (start and payload[start - 1 : start] != b"\n") or payload[
            terminal - 1 : terminal
        ] != b"\n":
            _error("replica log offset bisects an incomplete line")
        before = payload[:start]
        after = payload[terminal:]
        if (
            b"KAURI_AUDIT " in before
            or b"KAURI_FAULT direct_vote_omitted " in before
            or b"KAURI_FAULT aggregate_omitted " in before
        ):
            _error("audit/fault marker appeared before the clean baseline offset")
        if (
            b"KAURI_AUDIT " in after
            or b"KAURI_FAULT direct_vote_omitted " in after
            or b"KAURI_FAULT aggregate_omitted " in after
        ):
            _error("audit/fault marker appeared after the terminal snapshot")
        result[replica] = payload[start:terminal].decode("utf-8", errors="replace")
    return result


def _marker_tokens(line: str) -> dict[str, str]:
    pairs = re.findall(r"(?:^| )([a-z_]+)=([^ ]+)", line)
    values = dict(pairs)
    if len(values) != len(pairs):
        _error("ground-truth marker contains duplicate fields")
    return values


def _validate_native_ground_truth_marker(
    profile: FrozenPqarProfile,
    classification: SourceBlindClassification,
    *,
    arm: str,
    replica_logs: Mapping[int, str],
) -> None:
    aggregate_lines = [
        line
        for content in replica_logs.values()
        for line in content.splitlines()
        if "KAURI_FAULT aggregate_omitted " in line
    ]
    reporter_lines = [
        line
        for line in replica_logs[profile.reporter_id].splitlines()
        if "KAURI_FAULT aggregate_omitted " in line
    ]
    expected_aggregate_count = 0 if arm == ARM_OMISSION else 1
    if (
        aggregate_lines != reporter_lines
        or len(aggregate_lines) != expected_aggregate_count
    ):
        _error("aggregate omission marker count or source drifted")
    if aggregate_lines:
        aggregate = _marker_tokens(aggregate_lines[0])
        try:
            aggregate_ns = int(aggregate.get("monotonic_ns", ""))
        except ValueError as error:
            raise N31PostQcAuditRuntimeError(
                "aggregate omission marker timestamp is malformed"
            ) from error
        if (
            set(aggregate)
            != {
                "replica",
                "parent",
                "epoch",
                "tree",
                "block",
                "window",
                "monotonic_ns",
            }
            or aggregate.get("replica") != str(profile.reporter_id)
            or aggregate.get("parent") != str(profile.root_id)
            or aggregate.get("epoch") != str(profile.epoch_number)
            or aggregate.get("tree") != str(profile.tree_id)
            or aggregate.get("block") != classification.identity.block
            or aggregate.get("window") != profile.diagnostic_window
            or not classification.armed_ns
            <= aggregate_ns
            <= classification.qc_published_ns
        ):
            _error("aggregate omission marker identity or timing drifted")

    omission_lines = [
        line
        for content in replica_logs.values()
        for line in content.splitlines()
        if "KAURI_FAULT direct_vote_omitted " in line
    ]
    if arm != ARM_OMISSION:
        if omission_lines:
            _error("non-omission arm contains direct-vote omission ground truth")
        return
    target_lines = [
        line
        for line in replica_logs[profile.target_id].splitlines()
        if "KAURI_FAULT direct_vote_omitted " in line
    ]
    if omission_lines != target_lines or len(target_lines) != 1:
        _error("omission arm lacks exactly one target-owned omission marker")
    tokens = _marker_tokens(target_lines[0])
    try:
        marker_ns = int(tokens.get("monotonic_ns", ""))
    except ValueError as error:
        raise N31PostQcAuditRuntimeError(
            "omission marker timestamp is malformed"
        ) from error
    if (
        set(tokens)
        != {"replica", "parent", "epoch", "tree", "block", "window", "monotonic_ns"}
        or tokens.get("replica") != str(profile.target_id)
        or tokens.get("parent") != str(profile.reporter_id)
        or tokens.get("epoch") != str(profile.epoch_number)
        or tokens.get("tree") != str(profile.tree_id)
        or tokens.get("block") != classification.identity.block
        or tokens.get("window") != profile.diagnostic_window
        or marker_ns <= classification.armed_ns
        or marker_ns > int(classification.deadline_ns or 0)
    ):
        _error("omission ground-truth marker identity or timing drifted")


def _event_timestamp(event: Mapping[str, Any]) -> int:
    value = event.get("source_monotonic_ns")
    if type(value) is not int or value <= 0:
        _error("structured event timestamp is malformed")
    return value


def _validate_structured_sources(
    profile: FrozenPqarProfile,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    manifest: Mapping[str, object],
) -> int:
    expected = {
        *(f"replica-{replica}" for replica in profile.replica_ids),
        "adaptive-manager",
    }
    instances = manifest.get("source_instances")
    if set(streams) != expected or not isinstance(instances, Mapping):
        _error("structured evidence lacks the exact 31 replicas and manager")
    if set(instances) != expected:
        _error("manifest source-instance membership is not exact")
    instance_values = tuple(instances.values())
    if any(not isinstance(value, str) or not value for value in instance_values) or len(
        set(instance_values)
    ) != len(instance_values):
        _error("manifest source-instance identities are malformed or reused")
    ready: list[int] = []
    for source in sorted(expected):
        events = streams[source]
        previous_ns = 0
        matches: list[int] = []
        for expected_sequence, event in enumerate(events, start=1):
            if (
                event.get("run_id") != manifest.get("run_id")
                or event.get("source_id") != source
                or event.get("source_instance") != instances[source]
                or event.get("source_sequence") != expected_sequence
            ):
                _error("structured source identity or sequence drifted")
            timestamp = _event_timestamp(event)
            if timestamp < previous_ns:
                _error("structured source timestamp regressed")
            previous_ns = timestamp
            if event.get("event_type") == "process.ready":
                matches.append(timestamp)
            if event.get("event_type") in {"process.restarted", "runtime.restarted"}:
                _error("structured evidence contains a process restart")
        if len(matches) != 1:
            _error("each source requires exactly one process.ready event")
        ready.append(matches[0])
    return max(ready)


def _commit_identity(event: Mapping[str, Any]) -> tuple[int, str, str, int, int]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        _error("commit payload is malformed")
    height = payload.get("block_height")
    block = payload.get("block_hash")
    parent = payload.get("parent_hash")
    transactions = payload.get("transaction_count")
    batch = payload.get("commit_batch_index")
    if (
        type(height) is not int
        or height <= 0
        or not isinstance(block, str)
        or _HEX_256.fullmatch(block) is None
        or not isinstance(parent, str)
        or _HEX_256.fullmatch(parent) is None
        or type(transactions) is not int
        or transactions < 0
        or type(batch) is not int
        or batch < 0
    ):
        _error("commit identity is malformed")
    return height, block, parent, transactions, batch


def _common_commits(
    profile: FrozenPqarProfile,
    runtime_profile: FrozenProfile,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, object], ...]:
    observed: dict[int, dict[tuple[int, str, str, int], int]] = {}
    for replica in profile.commit_witnesses:
        values: dict[tuple[int, str, str, int], int] = {}
        for event in streams[f"replica-{replica}"]:
            if event.get("event_type") != "block.commit_observed":
                continue
            height, block, parent, transactions, _batch = _commit_identity(event)
            key = (height, block, parent, transactions)
            timestamp = _event_timestamp(event)
            values[key] = min(timestamp, values.get(key, timestamp))
        observed[replica] = values
    results: list[dict[str, object]] = []
    observer = f"replica-{runtime_profile.authoritative_observer}"
    for event in streams[observer]:
        if event.get("event_type") != "block.committed":
            continue
        height, block, parent, transactions, batch = _commit_identity(event)
        key = (height, block, parent, transactions)
        witness_times = [
            observed[replica].get(key) for replica in profile.commit_witnesses
        ]
        if any(timestamp is None for timestamp in witness_times):
            continue
        timestamp = _event_timestamp(event)
        results.append(
            {
                "block_height": height,
                "block_hash": block,
                "parent_hash": parent,
                "transaction_count": transactions,
                "commit_batch_index": batch,
                "observer_monotonic_ns": timestamp,
                "common_monotonic_ns": max(
                    timestamp,
                    *(int(value) for value in witness_times if value is not None),
                ),
            }
        )
    return tuple(results)


def _validate_source_blind_clean_boundary(
    profile: FrozenPqarProfile,
    runtime_profile: FrozenProfile,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    ready_barrier_ns: int,
    clean_boundary_ns: int,
) -> None:
    matches = [
        commit
        for commit in _common_commits(profile, runtime_profile, streams)
        if int(commit["common_monotonic_ns"]) == clean_boundary_ns
    ]
    if clean_boundary_ns <= ready_barrier_ns or len(matches) != 1:
        _error("recorded clean boundary is not one post-ready fixed-Q21 commit")


def _observer_ancestry(
    runtime_profile: FrozenProfile,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    descendant: str,
) -> tuple[str, ...]:
    observer = f"replica-{runtime_profile.authoritative_observer}"
    blocks: dict[str, tuple[int, str, int, int]] = {}
    for event in streams[observer]:
        if event.get("event_type") != "block.commit_observed":
            continue
        height, block, parent, _transactions, _batch = _commit_identity(event)
        sequence = event.get("source_sequence")
        if type(sequence) is not int or sequence <= 0:
            _error("observer ancestry sequence is malformed")
        blocks[block] = (height, parent, sequence, _event_timestamp(event))
    chain: list[str] = []
    seen: set[str] = set()
    current = descendant
    while current in blocks:
        if current in seen:
            _error("observer ancestry contains a cycle")
        seen.add(current)
        chain.append(current)
        height, parent, sequence, timestamp = blocks[current]
        parent_value = blocks.get(parent)
        if parent_value is None:
            break
        if (
            parent_value[0] != height - 1
            or parent_value[2] >= sequence
            or parent_value[3] > timestamp
        ):
            _error("observer ancestry order is non-monotonic")
        current = parent
    return tuple(chain)


def _source_blind_later_commit_latency_ns(
    profile: FrozenPqarProfile,
    runtime_profile: FrozenProfile,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    classification: SourceBlindClassification,
) -> int | None:
    candidates: list[int] = []
    for commit in _common_commits(profile, runtime_profile, streams):
        timestamp = int(commit["common_monotonic_ns"])
        if timestamp <= classification.audit_expiry_ns:
            continue
        ancestry = _observer_ancestry(
            runtime_profile,
            streams,
            descendant=str(commit["block_hash"]),
        )
        if classification.identity.block in ancestry:
            candidates.append(timestamp)
    if not candidates:
        return None
    return min(candidates) - classification.audit_expiry_ns


_ROOT_QC_PAYLOAD_FIELDS = {
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


def _independent_root_qc_ns(
    profile: FrozenPqarProfile,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    classification: SourceBlindClassification,
) -> int:
    source = f"replica-{profile.root_id}"
    matches: list[Mapping[str, Any]] = []
    for event in streams[source]:
        if event.get("event_type") != "aggregation.root_qc_published":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            _error("root QC payload is malformed")
        if (
            payload.get("epoch_number") != classification.identity.epoch
            or payload.get("tree_id") != classification.identity.tree
            or payload.get("epoch_digest") != classification.identity.epoch_digest
            or payload.get("block_hash") != classification.identity.block
        ):
            continue
        if set(payload) != _ROOT_QC_PAYLOAD_FIELDS:
            _error("root QC payload schema drifted")
        if event.get("source_kind") != "replica" or event.get("source_id") != source:
            _error("root QC source envelope drifted")
        accepted = payload.get("accepted_signers")
        if (
            not isinstance(accepted, list)
            or any(type(signer) is not int for signer in accepted)
            or accepted != sorted(set(accepted))
            or tuple(accepted) != classification.qc_signers
            or len(accepted) < profile.quorum
            or payload.get("context_generation")
            != classification.root_context_generation
            or payload.get("observer_replica") != profile.root_id
            or payload.get("wait_exempt_signers") != []
            or payload.get("root_signer_count") != len(accepted)
            or payload.get("global_quorum") != profile.quorum
            or payload.get("rejection_reason") is not None
        ):
            _error("root QC signer, generation, or quorum identity drifted")
        for field in (
            "absent_direct_children",
            "missing_optional_signers",
        ):
            values = payload.get(field)
            if (
                not isinstance(values, list)
                or any(type(value) is not int for value in values)
                or values != sorted(set(values))
            ):
                _error(f"root QC {field} is non-canonical")
        gaps = payload.get("required_branch_gaps")
        if not isinstance(gaps, list):
            _error("root QC required_branch_gaps is malformed")
        previous_child = -1
        for gap in gaps:
            if not isinstance(gap, Mapping) or set(gap) != {
                "direct_child",
                "missing_required_signers",
            }:
                _error("root QC required branch gap schema drifted")
            child = gap.get("direct_child")
            missing = gap.get("missing_required_signers")
            if (
                type(child) is not int
                or child <= previous_child
                or not isinstance(missing, list)
                or not missing
                or any(type(value) is not int for value in missing)
                or missing != sorted(set(missing))
            ):
                _error("root QC required branch gaps are non-canonical")
            previous_child = child
        matches.append(event)
    if len(matches) != 1:
        _error("selected proposal requires exactly one independent root QC event")
    timestamp = _event_timestamp(matches[0])
    skew = classification.qc_published_ns - timestamp
    if not 0 <= skew <= profile.qc_snapshot_max_skew_ns:
        _error("independent root QC exceeds the frozen root_snapshot skew bound")
    return timestamp


def _derive_consensus_evidence(
    profile: FrozenPqarProfile,
    runtime_profile: FrozenProfile,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    classification: SourceBlindClassification,
    *,
    ready_barrier_ns: int,
    clean_boundary_ns: int,
) -> ConsensusEvidence:
    commits = _common_commits(profile, runtime_profile, streams)
    baseline_candidates = [
        commit
        for commit in commits
        if ready_barrier_ns
        < int(commit["common_monotonic_ns"])
        < classification.armed_ns
    ]
    if not baseline_candidates:
        _error("fixed Q21 clean baseline is absent before audit arming")
    matching_baselines = [
        commit
        for commit in baseline_candidates
        if int(commit["common_monotonic_ns"]) == clean_boundary_ns
    ]
    if len(matching_baselines) != 1:
        _error("recorded clean boundary is not one fixed-Q21 baseline commit")
    baseline = matching_baselines[0]
    later_candidates: list[tuple[dict[str, object], tuple[str, ...]]] = []
    for commit in commits:
        if int(commit["common_monotonic_ns"]) <= classification.audit_expiry_ns:
            continue
        ancestry = _observer_ancestry(
            runtime_profile,
            streams,
            descendant=str(commit["block_hash"]),
        )
        if classification.identity.block in ancestry:
            later_candidates.append((commit, ancestry))
    if not later_candidates:
        _error("later fixed-Q21 commit preserving selected ancestry is absent")
    later, ancestry = min(
        later_candidates, key=lambda item: int(item[0]["common_monotonic_ns"])
    )

    observer_events = streams[f"replica-{runtime_profile.authoritative_observer}"]
    unique_buckets: list[tuple[int, str, int]] = []
    seen_buckets: set[tuple[int, str, int]] = set()
    height_hashes: dict[int, set[str]] = {}
    for event in observer_events:
        if event.get("event_type") != "block.committed":
            continue
        height, block, _parent, _transactions, batch = _commit_identity(event)
        bucket = (height, block, batch)
        if bucket not in seen_buckets:
            seen_buckets.add(bucket)
            unique_buckets.append(bucket)
        height_hashes.setdefault(height, set()).add(block)
    conflict_count = sum(len(values) - 1 for values in height_hashes.values())
    restart_count = sum(
        event.get("event_type") in {"process.restarted", "runtime.restarted"}
        for source_events in streams.values()
        for event in source_events
    )
    return ConsensusEvidence(
        baseline_block=str(baseline["block_hash"]),
        baseline_commit_ns=int(baseline["common_monotonic_ns"]),
        baseline_witnesses=profile.commit_witnesses,
        selected_block=classification.identity.block,
        root_qc_ns=_independent_root_qc_ns(profile, streams, classification),
        root_qc_signers=classification.qc_signers,
        later_block=str(later["block_hash"]),
        later_commit_ns=int(later["common_monotonic_ns"]),
        later_witnesses=profile.commit_witnesses,
        later_ancestry=ancestry,
        conflict_count=conflict_count,
        restart_count=restart_count,
        retry_count=0,
        unique_commit_buckets=tuple(unique_buckets),
    )


def _validation_document(run_id: str, result: PqarValidation) -> dict[str, object]:
    identity = asdict(result.identity)
    return {
        "schema_version": 1,
        "scenario": SCENARIO,
        "verdict": result.verdict,
        "run_id": run_id,
        "arm": result.arm,
        "source_blind_classification": result.source_blind_classification,
        "identity": identity,
        "quantitative_audit": {
            "armed_ns": result.armed_ns,
            "deadline_ns": result.deadline_ns,
            "target_arrival_ns": result.target_arrival_ns,
            "claim_deadline_ns": result.claim_deadline_ns,
            "claim_ns": result.claim_ns,
            "relay_sent_ns": result.relay_sent_ns,
            "qc_published_ns": result.qc_published_ns,
            "root_received_ns": result.root_received_ns,
            "root_verified_ns": result.root_verified_ns,
            "audit_expiry_ns": result.audit_expiry_ns,
            "qc_to_deadline_slack_ns": result.qc_to_deadline_slack_ns,
            "target_to_deadline_slack_ns": result.target_to_deadline_slack_ns,
            "relay_to_root_latency_ns": result.relay_to_root_latency_ns,
            "root_verification_latency_ns": result.root_verification_latency_ns,
            "qc_to_audit_latency_ns": result.qc_to_audit_latency_ns,
            "relay_wire_bytes": result.relay_wire_bytes,
            "root_wire_bytes": result.root_wire_bytes,
            "frozen_qc_signers_before": list(result.frozen_qc_signers_before),
            "frozen_qc_signers_after": list(result.frozen_qc_signers_after),
            "frozen_qc_hash_before": result.frozen_qc_hash_before,
            "frozen_qc_hash_after": result.frozen_qc_hash_after,
            "root_context_generation": result.root_context_generation,
            "later_commit_ns": result.later_commit_ns,
            "expiry_to_later_commit_latency_ns": (
                result.expiry_to_later_commit_latency_ns
            ),
            "unique_commit_buckets": [
                {"height": height, "block_hash": block, "batch_index": batch}
                for height, block, batch in result.unique_commit_buckets
            ],
        },
        "evidence_ceiling": result.evidence_ceiling,
        "figure_eligible": result.figure_eligible,
    }


def _validate_cleanup(
    profile: FrozenPqarProfile,
    manifest: Mapping[str, object],
    *,
    later_commit_ns: int,
) -> None:
    ledger = manifest.get("cleanup_ledger")
    if not isinstance(ledger, list) or len(ledger) != len(profile.replica_ids) + 1:
        _error("cleanup ledger lacks the exact 31 replicas and manager")
    expected = {
        "adaptive-manager",
        *(f"replica-{value}" for value in profile.replica_ids),
    }
    seen: set[str] = set()
    timestamps: set[int] = set()
    for entry in ledger:
        if not isinstance(entry, Mapping):
            _error("cleanup ledger entry is malformed")
        name = entry.get("name")
        timestamp = entry.get("cleanup_started_ns")
        if (
            not isinstance(name, str)
            or name in seen
            or type(timestamp) is not int
            or timestamp <= later_commit_ns
            or entry.get("classification")
            not in {"expected_cleanup", "expected_forced_cleanup"}
            or entry.get("cleanup_errors") != []
            or entry.get("cleanup_started_after_post_window") is not True
        ):
            _error("cleanup occurred early or has an unexpected process outcome")
        seen.add(name)
        timestamps.add(timestamp)
    if seen != expected or len(timestamps) != 1:
        _error("cleanup ledger membership or common timestamp drifted")


def _raw_validation(
    run_directory: Path,
    profile: FrozenPqarProfile,
    runtime_profile: FrozenProfile,
    manifest: Mapping[str, object],
) -> dict[str, object]:
    arm = str(manifest["arm"])
    replica_logs = _validated_log_slices(run_directory, manifest, profile)
    ready_barrier_ns = manifest.get("ready_barrier_ns")
    clean_boundary_ns = manifest.get("clean_boundary_ns")
    if (
        type(ready_barrier_ns) is not int
        or ready_barrier_ns <= 0
        or type(clean_boundary_ns) is not int
        or clean_boundary_ns <= ready_barrier_ns
    ):
        _error("manifest ready/clean boundary is malformed")
    # First pass has no arm input by construction.
    blind_logs = _audit_only_logs(replica_logs)
    classification = classify_source_blind(
        profile.source_blind_contract(),
        blind_logs,
        clean_boundary_ns=clean_boundary_ns,
    )
    validate_ground_truth(
        profile,
        arm=arm,
        classification=classification,
    )
    _validate_native_ground_truth_marker(
        profile, classification, arm=arm, replica_logs=replica_logs
    )
    streams = runtime.event_streams(
        runtime_profile, run_directory, include_manager=True, allow_partial=False
    )
    if _validate_structured_sources(profile, streams, manifest) != ready_barrier_ns:
        _error("manifest ready barrier differs from raw structured evidence")
    consensus = _derive_consensus_evidence(
        profile,
        runtime_profile,
        streams,
        classification,
        ready_barrier_ns=ready_barrier_ns,
        clean_boundary_ns=clean_boundary_ns,
    )
    _validate_cleanup(profile, manifest, later_commit_ns=consensus.later_commit_ns)
    result = validate_pilot(
        profile,
        blind_logs,
        clean_boundary_ns=clean_boundary_ns,
        arm=arm,
        consensus=consensus,
    )
    return _validation_document(run_directory.name, result)


def _load_source_blind_preserved_profiles(
    run_directory: Path,
) -> tuple[FrozenPqarProfile, FrozenProfile]:
    try:
        profile = load_frozen_profile(run_directory / "profile.json")
        runtime_profile = load_runtime_profile(run_directory / "runtime-profile.json")
        runtime.require_shipped_profile(runtime_profile)
    except (
        OSError,
        N31PostQcAuditError,
        ProfiledFaultEvaluationError,
        runtime.ProfiledFaultRuntimeError,
    ) as error:
        raise N31PostQcAuditRuntimeError(
            f"preserved profile binding rejected: {error}"
        ) from error
    if (
        profile.profile_id != SHIPPED_PROFILE_ID
        or profile.profile_sha256 != SHIPPED_PROFILE_SHA256
        or runtime_profile.profile_id != profile.runtime_profile_id
        or runtime_profile.profile_sha256 != profile.runtime_profile_sha256
    ):
        _error("preserved runtime profile differs from the canonical v3 binding")
    return profile, runtime_profile


def _source_blind_observation(
    *,
    run_directory: Path,
    profile: FrozenPqarProfile,
    runtime_profile: FrozenProfile,
    manifest: Mapping[str, object],
    trusted_provenance: TrustedProvenance,
    seal: object,
    classification: SourceBlindClassification | None,
    unclassified_reason: str | None,
    clean_boundary_ns: int,
    ready_barrier_ns: int,
    expiry_to_later_commit_latency_ns: int | None,
) -> dict[str, object]:
    def delta(end: int | None, start: int | None) -> int | None:
        return None if end is None or start is None else end - start

    if classification is None:
        predicted = "unclassified"
        identity: dict[str, object] | None = None
        witness_signers: list[int] = []
        qc_signers: list[int] = []
        qc_fingerprint: str | None = None
        root_context_generation: int | None = None
        armed_ns = None
        deadline_ns = None
        target_arrival_ns = None
        claim_ns = None
        relay_sent_ns = None
        qc_published_ns = None
        root_received_ns = None
        root_verified_ns = None
        audit_expiry_ns = None
        qc_to_audit_latency_ns = None
        relay_wire_bytes = None
        root_wire_bytes = None
    else:
        predicted = classification.classification
        identity = asdict(classification.identity)
        witness_signers = list(classification.witness_signers)
        qc_signers = list(classification.qc_signers)
        qc_fingerprint = classification.frozen_qc_hash_after
        root_context_generation = classification.root_context_generation
        armed_ns = classification.armed_ns
        deadline_ns = classification.deadline_ns
        target_arrival_ns = classification.target_arrival_ns
        claim_ns = classification.claim_ns
        relay_sent_ns = classification.relay_sent_ns
        qc_published_ns = classification.qc_published_ns
        root_received_ns = classification.root_received_ns
        root_verified_ns = classification.root_verified_ns
        audit_expiry_ns = classification.audit_expiry_ns
        qc_to_audit_latency_ns = classification.qc_to_audit_latency_ns
        relay_wire_bytes = classification.relay_wire_bytes
        root_wire_bytes = classification.root_wire_bytes

    metrics_ns = {
        "qc_to_deadline_slack_ns": delta(deadline_ns, qc_published_ns),
        "target_to_deadline_slack_ns": delta(deadline_ns, target_arrival_ns),
        "relay_to_root_latency_ns": delta(root_received_ns, relay_sent_ns),
        "root_verification_latency_ns": delta(root_verified_ns, root_received_ns),
        "qc_to_audit_latency_ns": qc_to_audit_latency_ns,
        "expiry_to_later_commit_latency_ns": (expiry_to_later_commit_latency_ns),
    }
    timing = {
        "ready_barrier_ns": ready_barrier_ns,
        "clean_boundary_ns": clean_boundary_ns,
        "armed_ns": armed_ns,
        "deadline_ns": deadline_ns,
        "target_arrival_ns": target_arrival_ns,
        "claim_ns": claim_ns,
        "relay_sent_ns": relay_sent_ns,
        "qc_published_ns": qc_published_ns,
        "root_received_ns": root_received_ns,
        "root_verified_ns": root_verified_ns,
        "audit_expiry_ns": audit_expiry_ns,
        **metrics_ns,
    }
    child = {
        "path": str(run_directory),
        "run_id": manifest["run_id"],
        "kauri_revision": trusted_provenance.revision,
        "trusted_provenance_sha256": trusted_provenance.sha256,
        "evidence_tree_sha256": getattr(seal, "tree_sha256"),
        "evidence_seal_sha256": getattr(seal, "seal_sha256"),
        "profile_binding": {
            "profile_id": profile.profile_id,
            "sha256": profile.profile_sha256,
        },
        "runtime_profile_binding": {
            "profile_id": runtime_profile.profile_id,
            "sha256": runtime_profile.profile_sha256,
        },
        "log_offset_binding_sha256": _canonical_document_sha256(
            manifest["log_offsets"]
        ),
        "source_instance_binding_sha256": _canonical_document_sha256(
            manifest["source_instances"]
        ),
        "replica_log_count": len(profile.replica_ids),
        "structured_source_count": len(profile.replica_ids) + 1,
    }
    return {
        "schema_version": 1,
        "scenario": SCENARIO,
        "kind": "preserved-source-blind-classification",
        "classification": predicted,
        "predicted_classification": predicted,
        "unclassified_reason": unclassified_reason,
        "identity": identity,
        "root_context_generation": root_context_generation,
        "witness_signers": witness_signers,
        "qc_signers": qc_signers,
        "qc_fingerprint": qc_fingerprint,
        "metrics_ns": metrics_ns,
        "timing": timing,
        "wire_bytes": {
            "relay": relay_wire_bytes,
            "root": root_wire_bytes,
        },
        "child": child,
        "evidence_ceiling": "campaign_observation_only",
        "figure_eligible": False,
    }


def classify_preserved_run_source_blind(
    run_directory: Path,
    *,
    trusted_provenance: TrustedProvenance,
) -> dict[str, object]:
    """Extract one sealed child observation without consulting ground truth."""

    if type(trusted_provenance) is not TrustedProvenance:
        _error("source-blind extraction requires external trusted provenance")
    if run_directory.is_symlink():
        _error("source-blind child path must not be a symlink")
    run_directory = run_directory.resolve()
    try:
        seal = verify_evidence_seal(run_directory)
    except EvidenceSealError as error:
        raise N31PostQcAuditRuntimeError(f"evidence seal rejected: {error}") from error
    profile, runtime_profile = _load_source_blind_preserved_profiles(run_directory)
    manifest = _read_json_object(run_directory / "manifest.json", "manifest")
    _validate_source_blind_manifest(
        manifest,
        run_directory=run_directory,
        profile=profile,
        runtime_profile=runtime_profile,
    )
    if manifest.get("kauri_revision") != trusted_provenance.revision:
        _error("manifest revision differs from trusted provenance")
    try:
        _verify_preserved_build_provenance(run_directory, manifest, trusted_provenance)
        _verify_preserved_launch_membership(run_directory, profile, trusted_provenance)
        _verify_runtime_artifact_inventory(run_directory, manifest)
    except OSError as error:
        raise N31PostQcAuditRuntimeError(
            f"preserved runtime provenance is unreadable: {error}"
        ) from error
    try:
        replica_logs = _validated_log_slices(run_directory, manifest, profile)
        streams = runtime.event_streams(
            runtime_profile,
            run_directory,
            include_manager=True,
            allow_partial=False,
        )
    except (OSError, UnicodeDecodeError, runtime.ProfiledFaultRuntimeError) as error:
        raise N31PostQcAuditRuntimeError(
            f"preserved raw source integrity rejected: {error}"
        ) from error
    ready_barrier_ns = manifest.get("ready_barrier_ns")
    clean_boundary_ns = manifest.get("clean_boundary_ns")
    if (
        type(ready_barrier_ns) is not int
        or ready_barrier_ns <= 0
        or type(clean_boundary_ns) is not int
        or clean_boundary_ns <= ready_barrier_ns
    ):
        _error("manifest ready/clean boundary is malformed")
    if _validate_structured_sources(profile, streams, manifest) != ready_barrier_ns:
        _error("manifest ready barrier differs from raw structured evidence")
    _validate_source_blind_clean_boundary(
        profile,
        runtime_profile,
        streams,
        ready_barrier_ns=ready_barrier_ns,
        clean_boundary_ns=clean_boundary_ns,
    )

    blind_logs = _audit_only_logs(replica_logs)
    try:
        classification = classify_source_blind(
            profile.source_blind_contract(),
            blind_logs,
            clean_boundary_ns=clean_boundary_ns,
        )
    except N31PostQcAuditError as error:
        return _source_blind_observation(
            run_directory=run_directory,
            profile=profile,
            runtime_profile=runtime_profile,
            manifest=manifest,
            trusted_provenance=trusted_provenance,
            seal=seal,
            classification=None,
            unclassified_reason=str(error),
            clean_boundary_ns=clean_boundary_ns,
            ready_barrier_ns=ready_barrier_ns,
            expiry_to_later_commit_latency_ns=None,
        )
    if type(classification) is not SourceBlindClassification:
        _error("source-blind classifier returned an invalid result")
    later_latency = _source_blind_later_commit_latency_ns(
        profile,
        runtime_profile,
        streams,
        classification,
    )
    return _source_blind_observation(
        run_directory=run_directory,
        profile=profile,
        runtime_profile=runtime_profile,
        manifest=manifest,
        trusted_provenance=trusted_provenance,
        seal=seal,
        classification=classification,
        unclassified_reason=None,
        clean_boundary_ns=clean_boundary_ns,
        ready_barrier_ns=ready_barrier_ns,
        expiry_to_later_commit_latency_ns=later_latency,
    )


def validate_preserved_run(
    run_directory: Path,
    *,
    trusted_provenance: TrustedProvenance,
) -> dict[str, object]:
    """Revalidate PASS, while never upgrading recorded FAIL/INCOMPLETE."""

    if type(trusted_provenance) is not TrustedProvenance:
        _error("validation requires an external trusted provenance receipt")
    run_directory = run_directory.resolve()
    try:
        seal = verify_evidence_seal(run_directory)
    except EvidenceSealError as error:
        raise N31PostQcAuditRuntimeError(f"evidence seal rejected: {error}") from error
    profile = load_frozen_profile(run_directory / "profile.json")
    runtime_profile = load_runtime_profile(run_directory / "runtime-profile.json")
    if (
        runtime_profile.profile_id != profile.runtime_profile_id
        or runtime_profile.profile_sha256 != profile.runtime_profile_sha256
    ):
        _error("preserved runtime profile differs from its frozen binding")
    manifest = _read_json_object(run_directory / "manifest.json", "manifest")
    arm = _validate_manifest(
        manifest,
        run_directory=run_directory,
        profile=profile,
        runtime_profile=runtime_profile,
    )
    if (
        manifest.get("kauri_revision") != trusted_provenance.revision
        or trusted_provenance.repository == ""
    ):
        _error("manifest revision differs from trusted provenance")
    _verify_preserved_provenance(run_directory, manifest, profile, trusted_provenance)
    recorded = _read_json_object(
        run_directory / "validation.json", "recorded validation"
    )
    original_verdict = recorded.get("verdict")
    if original_verdict not in {"PASS", "FAIL", "INCOMPLETE"}:
        _error("recorded validation verdict is invalid")
    if (
        recorded.get("scenario") != SCENARIO
        or recorded.get("run_id") != run_directory.name
        or recorded.get("arm") != arm
        or recorded.get("evidence_ceiling") != "harness_validation_only"
        or recorded.get("figure_eligible") is not False
    ):
        _error("recorded validation identity or evidence ceiling drifted")
    if original_verdict == "PASS" and (
        manifest.get("complete") is not True
        or manifest.get("runtime_error") is not None
    ):
        _error("recorded PASS came from an incomplete runtime attempt")
    if original_verdict != "PASS":
        return {
            **recorded,
            "original_verdict_preserved": True,
            "run_directory": str(run_directory),
            "trusted_provenance_sha256": trusted_provenance.sha256,
            "evidence_tree_sha256": seal.tree_sha256,
            "evidence_seal_sha256": seal.seal_sha256,
        }
    independent = _raw_validation(run_directory, profile, runtime_profile, manifest)
    if independent != recorded:
        _error("recorded PASS differs from independent raw validation")
    return {
        **independent,
        "original_verdict_preserved": True,
        "run_directory": str(run_directory),
        "trusted_provenance_sha256": trusted_provenance.sha256,
        "evidence_tree_sha256": seal.tree_sha256,
        "evidence_seal_sha256": seal.seal_sha256,
    }


def run_once(
    *,
    audit_profile_path: Path,
    arm: str,
    trusted_provenance: TrustedProvenance,
    frozen_preflight: Mapping[str, object],
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
    """Run and seal one arm without preflighting, rebuilding, or retrying."""

    if type(trusted_provenance) is not TrustedProvenance:
        _error("run requires an exact external trusted provenance receipt")
    profile, runtime_profile, runtime_profile_path = _load_bound_profiles(
        audit_profile_path.resolve(), repository.resolve()
    )
    profile.arm(arm)
    revision = _checked_preflight(
        profile, runtime_profile, frozen_preflight, trusted_provenance
    )
    current_provenance = derive_trusted_provenance(
        repository=repository.resolve(),
        app_binary=app_binary.resolve(),
        manager_binary=manager_binary.resolve(),
        keygen_binary=keygen_binary.resolve(),
        tls_keygen_binary=tls_keygen_binary.resolve(),
        epoch_profile_digest_binary=epoch_profile_digest_binary.resolve(),
        build_directory=build_directory.resolve(),
        build_provenance_path=build_provenance_path.resolve(),
    )
    if current_provenance != trusted_provenance:
        _error("per-arm binaries or build provenance changed after preflight")
    contracts = frozen_preflight["launch_contracts"]
    assert isinstance(contracts, Mapping)
    launch_contract = contracts[arm]
    assert isinstance(launch_contract, Mapping)
    raw_arguments = launch_contract.get("replica_arguments")
    if not isinstance(raw_arguments, Mapping) or set(raw_arguments) != {
        str(replica) for replica in profile.replica_ids
    }:
        _error("selected launch contract lacks exact N31 arguments")
    overlays: dict[int, tuple[str, ...]] = {}
    for replica in profile.replica_ids:
        arguments = raw_arguments[str(replica)]
        if not isinstance(arguments, list) or any(
            not isinstance(argument, str) for argument in arguments
        ):
            _error("selected launch arguments are malformed")
        overlays[replica] = tuple(arguments)

    run_directory = runtime.create_run_directory(results_root.resolve())
    run_id = run_directory.name
    started_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    state_path = run_directory / "runner-state.json"
    state: dict[str, object] = {
        "schema_version": 1,
        "scenario": SCENARIO,
        "run_id": run_id,
        "arm": arm,
        "attempt": 1,
        "retry_policy": "none",
        "outcome_scanning": False,
        "phase": "identity_generation",
        "started_utc": started_utc,
        "verdict": None,
    }
    runtime.write_json_exclusive(state_path, state)
    runtime.write_exclusive(
        run_directory / "profile.json", audit_profile_path.resolve().read_bytes()
    )
    runtime.write_exclusive(
        run_directory / "runtime-profile.json", runtime_profile_path.read_bytes()
    )
    runtime.write_json_exclusive(
        run_directory / "runtime" / "preflight.json", dict(frozen_preflight)
    )
    runtime.write_json_exclusive(
        run_directory / "runtime" / "launch-contract.json", dict(launch_contract)
    )
    runtime.write_exclusive(
        run_directory / "runtime" / "build-provenance.json",
        build_provenance_path.resolve().read_bytes(),
    )

    source_instances = {
        f"replica-{replica}": f"{run_id}-replica-{replica}-{uuid.uuid4().hex}"
        for replica in profile.replica_ids
    }
    source_instances["adaptive-manager"] = f"{run_id}-manager-{uuid.uuid4().hex}"
    registry = ProcessRegistry(monotonic_ns=runtime.monotonic_raw_ns)
    resources = ExitStack()
    records: list[ProcessRecord] = []
    log_handles: list[IO[bytes]] = []
    cleanup_ledger: list[dict[str, object]] = []
    start_offsets: dict[int, int] = {}
    ready_barrier_ns: int | None = None
    clean_boundary_ns: int | None = None
    runtime_error: str | None = None
    validation_error: str | None = None
    live_result: PqarValidation | None = None
    consensus: ConsensusEvidence | None = None
    generated_artifacts: list[dict[str, object]] = []
    hard_deadline_ns = runtime.monotonic_raw_ns() + int(
        runtime_profile.hard_timeout_s * 1_000_000_000
    )

    def update_state(phase: str, **extra: object) -> None:
        state["phase"] = phase
        state.update(extra)
        runtime.replace_json(state_path, state)

    try:
        bls, tls, issuer = runtime.generate_identities(
            runtime_profile,
            keygen_binary=keygen_binary.resolve(),
            tls_keygen_binary=tls_keygen_binary.resolve(),
            config_directory=run_directory / "config",
        )
        manager_command, replica_commands, generated_artifacts = (
            runtime.write_runtime_inputs(
                runtime_profile,
                run_directory=run_directory,
                app_binary=app_binary.resolve(),
                manager_binary=manager_binary.resolve(),
                bls=bls,
                tls=tls,
                issuer=issuer,
                run_id=run_id,
                source_instances=source_instances,
                replica_overlays=overlays,
            )
        )
        update_state("launch")
        manager_record, manager_log = runtime.spawn_owned_process(
            registry,
            name="adaptive-manager",
            replica_id=-1,
            command=manager_command,
            log_path=run_directory / "logs" / "adaptive-manager.log",
            working_directory=run_directory,
        )
        records.append(manager_record)
        log_handles.append(manager_log)
        for replica in profile.replica_ids:
            record, log = runtime.spawn_owned_process(
                registry,
                name=f"replica-{replica}",
                replica_id=replica,
                command=replica_commands[replica],
                log_path=run_directory / "logs" / f"replica-{replica}.log",
                working_directory=run_directory,
            )
            records.append(record)
            log_handles.append(log)

        def all_ready() -> int | None:
            streams = runtime.event_streams(
                runtime_profile,
                run_directory,
                include_manager=True,
                allow_partial=True,
            )
            if len(streams) != 32:
                return None
            timestamps: list[int] = []
            for events in streams.values():
                ready = [
                    event
                    for event in events
                    if event.get("event_type") == "process.ready"
                ]
                if len(ready) != 1:
                    return None
                timestamps.append(_event_timestamp(ready[0]))
            return max(timestamps)

        ready_barrier_ns = int(
            runtime.wait_until(
                "all 32 exact process.ready events",
                all_ready,
                phase_timeout_s=runtime_profile.startup_timeout_s,
                hard_deadline_ns=hard_deadline_ns,
                records=records,
                crashed_replica=None,
            )
        )
        # Snap once, before the clean commit, so the polling race cannot hide
        # an audit that armed too early.
        start_offsets = {
            replica: _complete_log_offset(
                run_directory / "logs" / f"replica-{replica}.log"
            )
            for replica in profile.replica_ids
        }
        _live_log_slices(run_directory, profile, start_offsets)
        update_state("clean_baseline", ready_barrier_ns=ready_barrier_ns)

        def baseline() -> dict[str, object] | None:
            streams = runtime.event_streams(
                runtime_profile, run_directory, allow_partial=True
            )
            return runtime.find_common_commit(
                runtime_profile,
                streams,
                witnesses=profile.commit_witnesses,
                after_ns=ready_barrier_ns + 1,
            )

        baseline_commit = dict(
            runtime.wait_until(
                "clean fixed-Q21 common commit",
                baseline,
                phase_timeout_s=runtime_profile.startup_timeout_s,
                hard_deadline_ns=hard_deadline_ns,
                records=records,
                crashed_replica=None,
            )
        )
        clean_boundary_ns = int(baseline_commit["common_monotonic_ns"])
        update_state(
            "source_blind_audit",
            clean_boundary_ns=clean_boundary_ns,
            clean_baseline=baseline_commit,
        )
        classification = runtime.wait_until(
            "one terminal source-blind post-QC audit context",
            lambda: _poll_source_blind_classification(
                run_directory,
                profile,
                start_offsets,
                clean_boundary_ns=clean_boundary_ns,
            ),
            phase_timeout_s=runtime_profile.startup_timeout_s,
            hard_deadline_ns=hard_deadline_ns,
            records=records,
            crashed_replica=None,
            poll_interval_s=0.01,
        )
        if not isinstance(classification, SourceBlindClassification):
            _error("source-blind classifier returned an invalid result")
        # Ground truth is intentionally unavailable until the classifier has
        # returned a terminal class.
        live_logs = _live_log_slices(run_directory, profile, start_offsets)
        validate_ground_truth(
            profile,
            arm=arm,
            classification=classification,
        )
        _validate_native_ground_truth_marker(
            profile,
            classification,
            arm=arm,
            replica_logs=live_logs,
        )
        update_state(
            "later_q21_ancestry",
            source_blind_classification=classification.classification,
        )

        def later_evidence() -> ConsensusEvidence | None:
            streams = runtime.event_streams(
                runtime_profile,
                run_directory,
                include_manager=True,
                allow_partial=True,
            )
            try:
                return _derive_consensus_evidence(
                    profile,
                    runtime_profile,
                    streams,
                    classification,
                    ready_barrier_ns=ready_barrier_ns,
                    clean_boundary_ns=clean_boundary_ns,
                )
            except N31PostQcAuditRuntimeError as error:
                if "later fixed-Q21 commit" in str(error):
                    return None
                raise

        consensus = runtime.wait_until(
            "later fixed-Q21 commit preserving selected ancestry",
            later_evidence,
            phase_timeout_s=runtime_profile.startup_timeout_s,
            hard_deadline_ns=hard_deadline_ns,
            records=records,
            crashed_replica=None,
            poll_interval_s=0.01,
        )
        if not isinstance(consensus, ConsensusEvidence):
            _error("later consensus evidence is malformed")
        live_result = validate_pilot(
            profile,
            _audit_only_logs(_live_log_slices(run_directory, profile, start_offsets)),
            clean_boundary_ns=clean_boundary_ns,
            arm=arm,
            consensus=consensus,
        )
        update_state("qualified_pending_cleanup")
    except KeyboardInterrupt as error:
        runtime_error = f"interrupted: {error}"
    except (N31PostQcAuditError, N31PostQcAuditRuntimeError) as error:
        validation_error = str(error)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
    ) as error:
        runtime_error = str(error)
    finally:
        cleanup_errors: list[str] = []
        try:
            if records:
                cleanup_ledger, _cleanup_ns = runtime.concurrent_cleanup(
                    records,
                    faulted_replica_id=None,
                    post_end_ns=(
                        consensus.later_commit_ns if consensus is not None else None
                    ),
                )
                for entry in cleanup_ledger:
                    if entry.get("classification") == "unexpected_exit":
                        cleanup_errors.append(
                            f"unexpected process exit: {entry.get('name')}"
                        )
                    errors = entry.get("cleanup_errors")
                    if isinstance(errors, list):
                        cleanup_errors.extend(str(value) for value in errors)
        except (OSError, RuntimeError) as error:
            cleanup_errors.append(str(error))
        try:
            resources.close()
        except (OSError, RuntimeError, ValueError) as error:
            cleanup_errors.append(str(error))
        for handle in log_handles:
            try:
                handle.close()
            except OSError as error:
                cleanup_errors.append(str(error))
        try:
            runtime.wait_ports_clear(
                tuple(
                    [runtime_profile.manager_port]
                    + [
                        runtime_profile.peer_base + replica
                        for replica in runtime_profile.replica_ids
                    ]
                    + [
                        runtime_profile.client_base + replica
                        for replica in runtime_profile.replica_ids
                    ]
                )
            )
        except runtime.ProfiledFaultRuntimeError as error:
            cleanup_errors.append(str(error))
        if cleanup_errors:
            runtime_error = runtime_error or "; ".join(dict.fromkeys(cleanup_errors))

    terminal_offsets: dict[int, int] = {}
    log_records: dict[str, dict[str, object]] = {}
    for replica in profile.replica_ids:
        path = run_directory / "logs" / f"replica-{replica}.log"
        if not path.exists():
            runtime.write_exclusive(path, b"")
        terminal = _complete_log_offset(path)
        terminal_offsets[replica] = terminal
        log_records[str(replica)] = {
            "path": f"logs/replica-{replica}.log",
            "sha256": runtime.sha256_file(path),
            "start_offset": start_offsets.get(replica, 0),
            "terminal_offset": terminal,
        }

    finished_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    runtime_artifacts = list(generated_artifacts)
    for kind, relative in (
        ("audit_profile", "profile.json"),
        ("runtime_profile", "runtime-profile.json"),
        ("preflight", "runtime/preflight.json"),
        ("launch_contract", "runtime/launch-contract.json"),
        ("build_provenance", "runtime/build-provenance.json"),
    ):
        path = run_directory / relative
        runtime_artifacts.append(
            {"kind": kind, "path": relative, "sha256": runtime.sha256_file(path)}
        )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "scenario": SCENARIO,
        "run_id": run_id,
        "kauri_revision": revision,
        "profile": {
            "profile_id": profile.profile_id,
            "sha256": profile.profile_sha256,
        },
        "runtime_profile": {
            "profile_id": runtime_profile.profile_id,
            "sha256": runtime_profile.profile_sha256,
        },
        "arm": arm,
        "attempt": 1,
        "retry_policy": "none",
        "outcome_scanning": False,
        "complete": runtime_error is None,
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "preflight": dict(frozen_preflight),
        "source_instances": source_instances,
        "ready_barrier_ns": ready_barrier_ns,
        "clean_boundary_ns": clean_boundary_ns,
        "log_offsets": log_records,
        "runtime_artifacts": runtime_artifacts,
        "cleanup_ledger": cleanup_ledger,
        "runtime_error": runtime_error,
    }
    runtime.write_json_exclusive(run_directory / "manifest.json", manifest)

    if runtime_error is not None:
        verdict = "INCOMPLETE"
        validation = {
            "schema_version": 1,
            "scenario": SCENARIO,
            "verdict": verdict,
            "run_id": run_id,
            "arm": arm,
            "error": runtime_error,
            "evidence_ceiling": "harness_validation_only",
            "figure_eligible": False,
        }
    elif validation_error is not None:
        verdict = "FAIL"
        validation = {
            "schema_version": 1,
            "scenario": SCENARIO,
            "verdict": verdict,
            "run_id": run_id,
            "arm": arm,
            "error": validation_error,
            "evidence_ceiling": "harness_validation_only",
            "figure_eligible": False,
        }
    else:
        try:
            validation = _raw_validation(
                run_directory, profile, runtime_profile, manifest
            )
            verdict = "PASS"
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            N31PostQcAuditError,
            N31PostQcAuditRuntimeError,
        ) as error:
            verdict = "FAIL"
            validation = {
                "schema_version": 1,
                "scenario": SCENARIO,
                "verdict": verdict,
                "run_id": run_id,
                "arm": arm,
                "error": str(error),
                "evidence_ceiling": "harness_validation_only",
                "figure_eligible": False,
            }
    state.update(
        {
            "phase": "finished",
            "finished_utc": finished_utc,
            "runtime_error": runtime_error,
            "validation_error": validation_error,
            "verdict": verdict,
        }
    )
    runtime.replace_json(state_path, state)
    runtime.write_json_exclusive(run_directory / "validation.json", validation)
    try:
        create_evidence_seal(run_directory)
        verify_evidence_seal(run_directory)
    except (OSError, EvidenceSealError) as error:
        raise N31PostQcAuditRuntimeError(
            f"preserved run evidence could not be sealed: {error}"
        ) from error
    if verdict == "PASS":
        independently_validated = validate_preserved_run(
            run_directory, trusted_provenance=trusted_provenance
        )
        if independently_validated.get("verdict") != "PASS":
            _error("sealed independent validation did not preserve PASS")
    return run_directory, verdict


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _one_shot_ledger_path(sequence_directory: Path) -> Path:
    return sequence_directory.resolve().parent / _ONE_SHOT_LEDGER_FILENAME


def _create_sequence_directory(
    results_root: Path, *, profile: FrozenPqarProfile
) -> Path:
    """Consume the frozen v3 results root and allocate one empty parent."""

    if (
        profile.profile_id != SHIPPED_PROFILE_ID
        or profile.profile_sha256 != SHIPPED_PROFILE_SHA256
    ):
        _error("one-shot allocation requires the canonical shipped v3 profile")
    root = results_root.resolve()
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.mkdir(mode=0o700)
        _fsync_directory(root.parent)
    except FileExistsError as error:
        raise N31PostQcAuditRuntimeError(
            "frozen v3 results root is already spent by a prior allocation"
        ) from error
    except OSError as error:
        raise N31PostQcAuditRuntimeError(
            "could not exclusively allocate the frozen v3 results root"
        ) from error
    os.chmod(root, 0o700)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = root / f"sequence-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    ledger = {
        "schema_version": 1,
        "scenario": SCENARIO,
        "kind": "filesystem-exclusive-one-shot-allocation",
        "profile_id": profile.profile_id,
        "profile_sha256": profile.profile_sha256,
        "results_root": str(root),
        "sequence_directory": candidate.name,
        "allocated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "state": "allocated",
    }
    ledger_path = root / _ONE_SHOT_LEDGER_FILENAME
    try:
        runtime.write_json_exclusive(ledger_path, ledger, mode=0o400)
        _fsync_directory(root)
    except FileExistsError as error:
        raise N31PostQcAuditRuntimeError(
            "frozen v3 results root is already spent by a concurrent allocation"
        ) from error
    except OSError as error:
        raise N31PostQcAuditRuntimeError(
            "could not persist the frozen v3 one-shot allocation ledger"
        ) from error

    try:
        if {path.resolve() for path in root.iterdir()} != {ledger_path.resolve()}:
            _error("frozen v3 results root gained an unexpected allocation sibling")
        candidate.mkdir(mode=0o700)
        _fsync_directory(root)
    except (OSError, N31PostQcAuditRuntimeError) as error:
        raise N31PostQcAuditRuntimeError(
            "one-shot allocation was consumed but its sequence parent was not created"
        ) from error
    if any(candidate.iterdir()):  # pragma: no cover - mkdir is atomic and empty
        _error("new sequence directory unexpectedly contains scaffolding")
    return candidate


def run_pilot_sequence(
    *,
    audit_profile_path: Path,
    trusted_provenance: TrustedProvenance,
    frozen_preflight: Mapping[str, object],
    repository: Path,
    results_root: Path,
    app_binary: Path,
    manager_binary: Path,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    epoch_profile_digest_binary: Path,
    build_directory: Path,
    build_provenance_path: Path,
) -> tuple[Path, tuple[dict[str, str], ...]]:
    """Attempt the fixed order once, sealing and stopping on an exception."""

    profile, runtime_profile, _runtime_path = _load_bound_profiles(
        audit_profile_path.resolve(), repository.resolve()
    )
    _checked_preflight(profile, runtime_profile, frozen_preflight, trusted_provenance)
    sequence_directory = _create_sequence_directory(results_root, profile=profile)
    results: list[dict[str, str]] = []
    spent_arms: list[str] = []
    interrupted_arm: str | None = None
    sequence_error: Exception | None = None
    for arm in PILOT_EXECUTION_ORDER:
        spent_arms.append(arm)
        try:
            run_directory, verdict = run_once(
                audit_profile_path=audit_profile_path,
                arm=arm,
                trusted_provenance=trusted_provenance,
                frozen_preflight=frozen_preflight,
                repository=repository,
                results_root=sequence_directory,
                app_binary=app_binary,
                manager_binary=manager_binary,
                keygen_binary=keygen_binary,
                tls_keygen_binary=tls_keygen_binary,
                epoch_profile_digest_binary=epoch_profile_digest_binary,
                build_directory=build_directory,
                build_provenance_path=build_provenance_path,
            )
            run_directory = run_directory.resolve()
            if run_directory.parent != sequence_directory:
                _error("arm attempt escaped its one-shot sequence parent")
        except Exception as error:  # noqa: BLE001 - preserve any ordinary rejection
            interrupted_arm = arm
            sequence_error = error
            break
        results.append(
            {
                "arm": arm,
                "run_directory": str(run_directory),
                "verdict": verdict,
            }
        )
    try:
        observed_directories = sorted(
            (path.resolve() for path in sequence_directory.iterdir() if path.is_dir()),
            key=str,
        )
    except OSError as error:
        raise N31PostQcAuditRuntimeError(
            "cannot preserve observed one-shot attempt directories"
        ) from error
    execution_complete = sequence_error is None
    runtime_error = (
        None
        if sequence_error is None
        else f"{type(sequence_error).__name__}: {sequence_error}"
    )
    ledger_path = _one_shot_ledger_path(sequence_directory)
    receipt = {
        "schema_version": 1,
        "scenario": SCENARIO,
        "kind": "fresh-three-arm-pilot",
        "pilot_execution_order": list(PILOT_EXECUTION_ORDER),
        "attempts_per_arm": 1,
        "automatic_retries": 0,
        "outcome_scanning": False,
        "preflight_revision": trusted_provenance.revision,
        "preflight_profile_sha256": profile.profile_sha256,
        "one_shot_ledger_sha256": runtime.sha256_file(ledger_path),
        "execution_complete": execution_complete,
        "spent_arms": spent_arms,
        "interrupted_arm": interrupted_arm,
        "runtime_error": runtime_error,
        "observed_run_directories": [str(path) for path in observed_directories],
        "results": results,
        "evidence_ceiling": "harness_validation_only",
        "figure_eligible": False,
    }
    runtime.write_json_exclusive(sequence_directory / "pilot-sequence.json", receipt)
    try:
        create_evidence_seal(sequence_directory)
        verify_evidence_seal(sequence_directory)
    except (OSError, EvidenceSealError) as error:
        raise N31PostQcAuditRuntimeError(
            f"global pilot sequence could not be sealed: {error}"
        ) from error
    if sequence_error is not None:
        raise N31PostQcAuditRuntimeError(
            "one-shot pilot stopped at "
            f"{interrupted_arm}; preserved sealed parent {sequence_directory} "
            f"as INCOMPLETE: {runtime_error}"
        ) from sequence_error
    validate_pilot_sequence(sequence_directory, trusted_provenance=trusted_provenance)
    return sequence_directory, tuple(results)


def _validate_one_shot_ledger(
    sequence_directory: Path, receipt: Mapping[str, object]
) -> None:
    root = sequence_directory.parent
    ledger_path = _one_shot_ledger_path(sequence_directory)
    if ledger_path.is_symlink() or not ledger_path.is_file():
        _error("one-shot allocation ledger is absent or not a regular file")
    expected_digest = receipt.get("one_shot_ledger_sha256")
    if (
        not isinstance(expected_digest, str)
        or _HEX_256.fullmatch(expected_digest) is None
        or runtime.sha256_file(ledger_path) != expected_digest
    ):
        _error("one-shot allocation ledger differs from the sealed sequence receipt")
    ledger = _read_json_object(ledger_path, "one-shot allocation ledger")
    allocated_utc = ledger.get("allocated_utc")
    try:
        allocated = dt.datetime.fromisoformat(str(allocated_utc))
    except ValueError as error:
        raise N31PostQcAuditRuntimeError(
            "one-shot allocation timestamp is malformed"
        ) from error
    if (
        set(ledger) != _ONE_SHOT_LEDGER_FIELDS
        or ledger.get("schema_version") != 1
        or ledger.get("scenario") != SCENARIO
        or ledger.get("kind") != "filesystem-exclusive-one-shot-allocation"
        or ledger.get("profile_id") != SHIPPED_PROFILE_ID
        or ledger.get("profile_sha256") != SHIPPED_PROFILE_SHA256
        or ledger.get("results_root") != str(root)
        or ledger.get("sequence_directory") != sequence_directory.name
        or not isinstance(allocated_utc, str)
        or allocated.tzinfo is None
        or ledger.get("state") != "allocated"
    ):
        _error("one-shot allocation ledger identity or state drifted")
    try:
        siblings = {path.resolve() for path in root.iterdir()}
    except OSError as error:
        raise N31PostQcAuditRuntimeError(
            "cannot inspect one-shot results-root siblings"
        ) from error
    if siblings != {ledger_path.resolve(), sequence_directory.resolve()}:
        _error("one-shot results root contains an unexpected sibling")


def validate_pilot_sequence(
    sequence_directory: Path,
    *,
    trusted_provenance: TrustedProvenance,
) -> dict[str, object]:
    """Verify a sealed complete or structurally preserved incomplete sequence."""

    if type(trusted_provenance) is not TrustedProvenance:
        _error("sequence validation requires exact trusted provenance")
    sequence_directory = sequence_directory.resolve()
    try:
        seal = verify_evidence_seal(sequence_directory)
    except EvidenceSealError as error:
        raise N31PostQcAuditRuntimeError(
            f"global sequence seal rejected: {error}"
        ) from error
    receipt = _read_json_object(
        sequence_directory / "pilot-sequence.json", "pilot sequence receipt"
    )
    expected_fields = {
        "schema_version",
        "scenario",
        "kind",
        "pilot_execution_order",
        "attempts_per_arm",
        "automatic_retries",
        "outcome_scanning",
        "preflight_revision",
        "preflight_profile_sha256",
        "one_shot_ledger_sha256",
        "execution_complete",
        "spent_arms",
        "interrupted_arm",
        "runtime_error",
        "observed_run_directories",
        "results",
        "evidence_ceiling",
        "figure_eligible",
    }
    results = receipt.get("results")
    spent_arms = receipt.get("spent_arms")
    observed_values = receipt.get("observed_run_directories")
    execution_complete = receipt.get("execution_complete")
    if (
        set(receipt) != expected_fields
        or receipt.get("schema_version") != 1
        or receipt.get("scenario") != SCENARIO
        or receipt.get("kind") != "fresh-three-arm-pilot"
        or receipt.get("pilot_execution_order") != list(PILOT_EXECUTION_ORDER)
        or receipt.get("attempts_per_arm") != 1
        or receipt.get("automatic_retries") != 0
        or receipt.get("outcome_scanning") is not False
        or receipt.get("preflight_revision") != trusted_provenance.revision
        or receipt.get("preflight_profile_sha256") != SHIPPED_PROFILE_SHA256
        or receipt.get("evidence_ceiling") != "harness_validation_only"
        or receipt.get("figure_eligible") is not False
        or not isinstance(results, list)
        or not isinstance(spent_arms, list)
        or not isinstance(observed_values, list)
        or not isinstance(execution_complete, bool)
    ):
        _error("pilot sequence receipt drifted from the fixed fresh-run contract")
    if (
        any(not isinstance(arm, str) for arm in spent_arms)
        or spent_arms != list(PILOT_EXECUTION_ORDER[: len(spent_arms)])
        or not spent_arms
    ):
        _error("pilot sequence spent-arm prefix is malformed")
    interrupted_arm = receipt.get("interrupted_arm")
    runtime_error = receipt.get("runtime_error")
    if execution_complete:
        if (
            spent_arms != list(PILOT_EXECUTION_ORDER)
            or interrupted_arm is not None
            or runtime_error is not None
            or len(results) != len(PILOT_EXECUTION_ORDER)
        ):
            _error("completed pilot sequence has an incomplete execution record")
        expected_result_arms = tuple(PILOT_EXECUTION_ORDER)
    else:
        if (
            interrupted_arm != spent_arms[-1]
            or not isinstance(runtime_error, str)
            or not runtime_error
            or len(results) != len(spent_arms) - 1
        ):
            _error("incomplete pilot sequence lacks one exact interrupted arm")
        expected_result_arms = tuple(spent_arms[:-1])
    _validate_one_shot_ledger(sequence_directory, receipt)

    observed_directories: list[Path] = []
    for value in observed_values:
        if not isinstance(value, str):
            _error("observed run directory is malformed")
        path = Path(value).resolve()
        if path.parent != sequence_directory or path in observed_directories:
            _error("observed run directory escaped or duplicated its sequence")
        observed_directories.append(path)
    if observed_directories != sorted(observed_directories, key=str):
        _error("observed run directories are not canonical")
    if execution_complete and len(observed_directories) != len(PILOT_EXECUTION_ORDER):
        _error("completed sequence lacks exactly three observed directories")
    if not execution_complete and len(observed_directories) not in {
        len(results),
        len(results) + 1,
    }:
        _error("interrupted sequence has an impossible attempt-directory count")

    expected_directories: set[Path] = set()
    validated: list[dict[str, object]] = []
    for expected_arm, record in zip(expected_result_arms, results, strict=True):
        if not isinstance(record, Mapping) or set(record) != {
            "arm",
            "run_directory",
            "verdict",
        }:
            _error("pilot sequence attempt record is malformed")
        path_value = record.get("run_directory")
        if not isinstance(path_value, str):
            _error("pilot sequence run directory is malformed")
        run_directory = Path(path_value).resolve()
        if (
            record.get("arm") != expected_arm
            or run_directory.parent != sequence_directory
            or run_directory in expected_directories
        ):
            _error("pilot sequence arm order or run-directory scope drifted")
        expected_directories.add(run_directory)
        try:
            child_profile = load_frozen_profile(run_directory / "profile.json")
        except N31PostQcAuditError as error:
            raise N31PostQcAuditRuntimeError(
                "result-recorded child profile is absent or not the canonical "
                "shipped v3 profile"
            ) from error
        if (
            child_profile.profile_id != SHIPPED_PROFILE_ID
            or child_profile.profile_sha256 != SHIPPED_PROFILE_SHA256
            or child_profile.profile_sha256 != receipt.get("preflight_profile_sha256")
        ):
            _error(
                "result-recorded child profile identity/hash differs from parent "
                "preflight"
            )
        result = validate_preserved_run(
            run_directory, trusted_provenance=trusted_provenance
        )
        if result.get("verdict") != record.get("verdict"):
            _error("pilot sequence verdict differs from its sealed arm run")
        validated.append(result)
    actual_directories = {
        path.resolve() for path in sequence_directory.iterdir() if path.is_dir()
    }
    if actual_directories != set(observed_directories):
        _error("global sequence contains an extra or missing attempt directory")
    if not expected_directories.issubset(actual_directories):
        _error("pilot results reference an unobserved attempt directory")
    unvalidated_interrupted: list[dict[str, str]] = []
    for run_directory in sorted(
        actual_directories - expected_directories,
        key=str,
    ):
        profile_binding = "unavailable_or_invalid"
        try:
            child_profile = load_frozen_profile(run_directory / "profile.json")
        except N31PostQcAuditError:
            pass
        else:
            if (
                child_profile.profile_id == SHIPPED_PROFILE_ID
                and child_profile.profile_sha256 == SHIPPED_PROFILE_SHA256
                and child_profile.profile_sha256
                == receipt.get("preflight_profile_sha256")
            ):
                profile_binding = "canonical"
        unvalidated_interrupted.append(
            {
                "run_directory": str(run_directory),
                "reason": "interrupted_before_result_record",
                "profile_binding": profile_binding,
            }
        )
    verdicts = [result.get("verdict") for result in validated]
    aggregate_verdict = (
        "INCOMPLETE"
        if not execution_complete
        else (
            "PASS"
            if all(verdict == "PASS" for verdict in verdicts)
            else "INCOMPLETE" if "INCOMPLETE" in verdicts else "FAIL"
        )
    )
    return {
        **receipt,
        "verdict": aggregate_verdict,
        "validated_results": validated,
        "unvalidated_interrupted_directories": unvalidated_interrupted,
        "sequence_directory": str(sequence_directory),
        "evidence_tree_sha256": seal.tree_sha256,
        "evidence_seal_sha256": seal.seal_sha256,
    }


def prepare_and_run_pilot_sequence(
    *,
    audit_profile_path: Path,
    trusted_provenance_path: Path,
    repository: Path,
    results_root: Path,
    app_binary: Path,
    manager_binary: Path,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    epoch_profile_digest_binary: Path,
    build_directory: Path,
    build_provenance_path: Path,
) -> tuple[Path, tuple[dict[str, str], ...], TrustedProvenance]:
    """Build, derive provenance, and preflight exactly once before all arms."""

    runtime.prepare_exact_revision_build(
        repository=repository.resolve(), build_directory=build_directory.resolve()
    )
    trusted = derive_trusted_provenance(
        repository=repository.resolve(),
        app_binary=app_binary.resolve(),
        manager_binary=manager_binary.resolve(),
        keygen_binary=keygen_binary.resolve(),
        tls_keygen_binary=tls_keygen_binary.resolve(),
        epoch_profile_digest_binary=epoch_profile_digest_binary.resolve(),
        build_directory=build_directory.resolve(),
        build_provenance_path=build_provenance_path.resolve(),
    )
    frozen_preflight = preflight(
        audit_profile_path=audit_profile_path.resolve(),
        repository=repository.resolve(),
        app_binary=app_binary.resolve(),
        manager_binary=manager_binary.resolve(),
        keygen_binary=keygen_binary.resolve(),
        tls_keygen_binary=tls_keygen_binary.resolve(),
        epoch_profile_digest_binary=epoch_profile_digest_binary.resolve(),
        build_directory=build_directory.resolve(),
        build_provenance_path=build_provenance_path.resolve(),
    )
    write_trusted_provenance(trusted_provenance_path.resolve(), trusted)
    sequence, results = run_pilot_sequence(
        audit_profile_path=audit_profile_path.resolve(),
        trusted_provenance=trusted,
        frozen_preflight=frozen_preflight,
        repository=repository.resolve(),
        results_root=results_root.resolve(),
        app_binary=app_binary.resolve(),
        manager_binary=manager_binary.resolve(),
        keygen_binary=keygen_binary.resolve(),
        tls_keygen_binary=tls_keygen_binary.resolve(),
        epoch_profile_digest_binary=epoch_profile_digest_binary.resolve(),
        build_directory=build_directory.resolve(),
        build_provenance_path=build_provenance_path.resolve(),
    )
    return sequence, results, trusted


__all__ = (
    "N31PostQcAuditRuntimeError",
    "TrustedBinary",
    "TrustedProvenance",
    "classify_preserved_run_source_blind",
    "derive_trusted_provenance",
    "load_trusted_provenance",
    "preflight",
    "prepare_and_run_pilot_sequence",
    "run_once",
    "run_pilot_sequence",
    "validate_pilot_sequence",
    "validate_preserved_run",
    "write_trusted_provenance",
)
