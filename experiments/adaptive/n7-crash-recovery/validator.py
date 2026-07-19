#!/usr/bin/env python3
"""Canonical validator for the frozen N=7/f=2 crash-recovery scenario.

The validator consumes raw StructuredEventSink JSONL plus explicit manifest and
epoch-definition inputs.  It writes an immutable verdict.  Only a PASS verdict
contains canonical plotting inputs; FAIL and INCOMPLETE runs remain preserved
and can never be plotted by ``plot.py``.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence

import analysis


SCHEMA_VERSION = 1
SCENARIO = "n7-crash-recovery"
MEMBERSHIP = tuple(range(7))
FAULT_THRESHOLD = 2
QUORUM = 5
CRASHED_REPLICAS = (0, 1)
SURVIVING_REPLICAS = (2, 3, 4, 5, 6)
SUCCESSOR_ROOTS = SURVIVING_REPLICAS
FROZEN_PROFILE_ID = "n7-f2-q5-crash-recovery-v1"
FROZEN_PROFILE_SHA256 = (
    "529d93344ecb69e73133d832a89d4098f39700f428589d2985bd7e52ed99d80b"
)
CANONICAL_OUTPUT_NAMES = (
    "validation.json",
    "manifest.json",
    "profile.json",
    "epochs.json",
    "throughput.csv",
    "reputation.csv",
    "figure.png",
    "figure.pdf",
)
VALIDATION_CLAIM = ".validation.claim"
SOURCE_KINDS = frozenset(
    {"replica", "adaptation_manager", "orchestrator", "workload_client"}
)

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "scenario",
        "run_id",
        "kauri_revision",
        "kauri_worktree_clean",
        "profile",
        "run_completion",
        "replica_count",
        "fault_threshold",
        "quorum",
        "membership",
        "authoritative_observer",
        "manager",
        "bucket_width_ns",
        "activation_grace_ns",
        "baseline_start_ns",
        "end_ns",
        "sources",
        "crash_markers",
        "runtime",
        "runtime_artifacts",
    }
)
_PROFILE_FIELDS = frozenset({"identity", "path", "sha256"})
_FROZEN_PROFILE_FIELDS = frozenset(
    {
        "schema_version",
        "profile_id",
        "frozen",
        "replica_ids",
        "fault_threshold",
        "quorum",
        "authoritative_observer",
        "epoch0_roots",
        "crash_targets",
        "crash_epoch",
        "crash_root",
        "successor_epoch",
        "successor_roots",
        "successor_wait_exempt",
        "fanout",
        "pipeline_depth",
        "tree_switch_period_blocks",
        "bucket_width_s",
        "baseline_bucket_count",
        "activation_grace_s",
        "post_bucket_count",
        "minimum_qualifying_reporters",
        "minimum_timeout_observations_per_reporter",
        "minimum_net_reputation_drop",
        "response_reputation_delta",
        "timeout_reputation_delta",
        "aggregation_timeout_s",
        "leader_progress_timeout_s",
        "leader_activation_grace_s",
        "activation_delay_blocks",
        "maximum_stall_s",
        "degraded_maximum_stall_s",
        "block_size",
        "snapshot_seed",
    }
)
_RUN_COMPLETION_FIELDS = frozenset(
    {"complete", "interrupted", "runtime_error", "unexpected_survivor_exits"}
)
_SOURCE_FIELDS = frozenset(
    {"source_kind", "source_id", "source_instance", "pid", "pgid", "path"}
)
_MANAGER_FIELDS = frozenset(
    {"source_id", "receives_crash_ground_truth"}
)
_CRASH_MARKER_FIELDS = frozenset(
    {
        "replica_id",
        "pid",
        "pgid",
        "signal",
        "signal_number",
        "requested_monotonic_raw_ns",
        "confirmed_exit",
    }
)
_CONFIRMED_EXIT_FIELDS = frozenset(
    {
        "pid",
        "pgid",
        "signal",
        "signal_number",
        "observed_monotonic_raw_ns",
    }
)
_EPOCH_DOCUMENT_FIELDS = frozenset(
    {
        "schema_version",
        "replica_count",
        "fault_threshold",
        "quorum",
        "membership",
        "epochs",
    }
)
_EPOCH_FIELDS = frozenset(
    {"epoch_number", "epoch_digest", "trees", "command"}
)
_TREE_FIELDS = frozenset(
    {"tree_id", "fanout", "members_breadth_first", "wait_exempt"}
)
_COMMAND_FIELDS = frozenset(
    {
        "command_block_height",
        "command_block_hash",
        "payload_digest",
        "predecessor_epoch_number",
        "predecessor_epoch_digest",
        "successor_epoch_number",
        "successor_epoch_digest",
        "activation_delay_blocks",
        "activation_height",
    }
)
_EPOCH_EVENT_FIELDS = frozenset(
    {"epoch_number", "tree_id", "epoch_digest", "activation_height"}
)
_PROCESS_FIELDS = frozenset({"exit_status"})
_REPUTATION_FIELDS = frozenset(
    {
        "evidence_cutoff",
        "ingestion_sequence",
        "observation_id",
        "reporter_id",
        "target_id",
        "evidence_outcome",
        "reputation_outcome",
        "delta",
        "resulting_score",
    }
)
_COMMIT_FIELDS = frozenset(
    {
        "block_height",
        "block_hash",
        "parent_hash",
        "transaction_count",
        "designated_observer",
        "decision_proof",
        "view_generation",
        "commit_batch_index",
    }
)
_DECISION_FIELDS = frozenset(
    {"epoch_number", "tree_id", "epoch_digest", "block_hash"}
)
_RUNTIME_FIELDS = frozenset(
    {
        "block_size",
        "pipeline_depth",
        "aggregation_timeout_ms",
        "leader_progress_timeout_ms",
        "leader_activation_grace_ms",
        "activation_delay_blocks",
        "fanout",
        "epoch0_roots",
        "successor_roots",
        "successor_wait_exempt",
        "tree_switch_period_blocks",
        "snapshot_seed",
        "executables",
    }
)
_RUNTIME_EXECUTABLE_FIELDS = frozenset(
    {"hotstuff_app", "adaptation_manager"}
)
_RUNTIME_EXECUTABLE_DESCRIPTOR_FIELDS = frozenset({"path", "sha256"})
_RUNTIME_ARTIFACT_FIELDS = frozenset(
    {"kind", "replica_id", "path", "sha256"}
)
_REPLICA_EFFECTIVE_FIELDS = frozenset(
    {
        "schema_version",
        "replica_id",
        "protocol_mode",
        "replica_count",
        "fault_threshold",
        "quorum",
        "membership",
        "authoritative_observer",
        "block_size",
        "pipeline_depth",
        "aggregation_timeout_ms",
        "leader_progress_timeout_ms",
        "leader_activation_grace_ms",
        "fanout",
        "tree_switch_period_blocks",
    }
)
_INITIAL_EPOCH_INPUT_FIELDS = frozenset(
    {
        "schema_version",
        "replica_count",
        "membership",
        "fault_threshold",
        "quorum",
        "epoch0_trees",
    }
)
_INITIAL_EPOCH_TREE_FIELDS = frozenset(
    {
        "tree_id",
        "fanout",
        "pipeline_depth",
        "members_breadth_first",
        "wait_exempt",
    }
)
_LAUNCH_ARGUMENT_FIELDS = frozenset({"schema_version", "processes"})
_LAUNCH_PROCESS_FIELDS = frozenset(
    {"source_kind", "source_id", "argv", "effective_options"}
)
_REPLICA_RUNTIME_OPTION_FIELDS = frozenset(
    {
        "block_size",
        "pipeline_depth",
        "aggregation_timeout_ms",
        "leader_progress_timeout_ms",
        "leader_activation_grace_ms",
        "fanout",
        "tree_switch_period_blocks",
    }
)
_REPLICA_EFFECTIVE_OPTION_FIELDS = frozenset(
    {
        *_REPLICA_RUNTIME_OPTION_FIELDS,
        "binary_sha256",
        "main_config_sha256",
        "replica_config_sha256",
        "bls_public_key_sha256",
        "tls_certificate_sha256",
        "issuer_public_key_sha256",
        "manager_tls_certificate_sha256",
    }
)
_MANAGER_EFFECTIVE_OPTION_FIELDS = frozenset(
    {
        "activation_delay_blocks",
        "snapshot_seed",
        "binary_sha256",
        "tls_certificate_sha256",
        "issuer_public_key_sha256",
        "replica_tls_certificate_sha256",
    }
)


class ValidationError(ValueError):
    """Invalid canonical input or a contradicted acceptance invariant."""


class IncompleteRun(ValidationError):
    """Required evidence is absent, so the run is not judgeable."""


@dataclass(frozen=True, slots=True)
class SourceSpec:
    source_kind: str
    source_id: str
    source_instance: str
    pid: int
    pgid: int
    relative_path: str
    path: Path

    @property
    def key(self) -> tuple[str, str]:
        return (self.source_kind, self.source_id)


@dataclass(frozen=True, slots=True)
class CrashMarkerSpec:
    replica_id: int
    pid: int
    pgid: int
    requested_ns: int
    confirmed_ns: int


@dataclass(frozen=True, slots=True)
class RuntimeArtifactSpec:
    kind: str
    replica_id: int | None
    relative_path: str
    path: Path
    sha256: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class Manifest:
    raw: Mapping[str, Any]
    path: Path
    run_id: str
    kauri_revision: str
    profile_identity: str
    profile_path: Path
    profile_bytes: bytes
    authoritative_observer: str
    manager_source_id: str
    baseline_start_ns: int
    end_ns: int
    activation_grace_ns: int
    baseline_bucket_count: int
    post_bucket_count: int
    maximum_stall_ns: int
    degraded_maximum_stall_ns: int
    runtime: Mapping[str, Any]
    runtime_artifacts: tuple[RuntimeArtifactSpec, ...]
    sources: tuple[SourceSpec, ...]
    crash_markers: tuple[CrashMarkerSpec, ...]


@dataclass(frozen=True, slots=True)
class TreeDefinition:
    epoch_number: int
    epoch_digest: str
    tree_id: int
    fanout: int
    members: tuple[int, ...]
    wait_exempt: tuple[int, ...]

    @property
    def leader(self) -> int:
        return self.members[0]

    @property
    def configuration_key(self) -> analysis.ConfigurationKey:
        return (self.epoch_number, self.epoch_digest, self.tree_id)


@dataclass(frozen=True, slots=True)
class EpochDefinition:
    epoch_number: int
    epoch_digest: str
    trees: tuple[TreeDefinition, ...]
    command: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class EpochDocument:
    raw: Mapping[str, Any]
    path: Path
    initial: EpochDefinition
    successor: EpochDefinition
    leader_by_configuration: Mapping[analysis.ConfigurationKey, int]


@dataclass(frozen=True, slots=True)
class ReputationPoint:
    timestamp_ns: int
    replica_id: int
    score: int
    source_sequence: int | None
    evidence_outcome: str
    delta: int


@dataclass(frozen=True, slots=True)
class Evaluation:
    manifest: Manifest
    epochs: EpochDocument
    crash_markers_ns: tuple[int, int]
    crash_ns: int
    command_ns: int
    activation_ns: int
    post_start_ns: int
    throughput: analysis.ThroughputAnalysis
    reputation: tuple[ReputationPoint, ...]
    final_scores: tuple[int, int, int, int, int, int, int]
    common_commit_heights: int
    complete_bucket_counts: Mapping[str, int]
    maximum_stall_ns_by_phase: Mapping[str, int]


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValidationError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValidationError(f"non-finite JSON number is not canonical: {value}")


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise IncompleteRun(f"missing {label}: {path}") from exc
    except OSError as exc:
        raise IncompleteRun(f"cannot read {label} {path}: {exc}") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except ValidationError:
        raise
    except json.JSONDecodeError as exc:
        raise ValidationError(f"malformed {label}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object")
    return value


def _load_json_bytes(payload: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except ValidationError:
        raise
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"malformed {label}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object")
    return value


def _json_values_equal(actual: Any, expected: Any) -> bool:
    """Compare JSON values without Python's bool/int equality aliasing."""
    return json.dumps(actual, sort_keys=True, separators=(",", ":")) == json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    )


def _exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        fragments: list[str] = []
        if missing:
            fragments.append("missing " + ", ".join(missing))
        if unknown:
            fragments.append("unknown " + ", ".join(unknown))
        raise ValidationError(f"{label} fields are invalid: {'; '.join(fragments)}")


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = analysis.UINT64_MAX,
) -> int:
    if type(value) is not int or value < minimum:
        raise ValidationError(f"{label} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{label} exceeds {maximum}")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} must be a non-empty string")
    return value


def _hash(value: Any, label: str) -> str:
    try:
        return analysis._require_hash(value, label)
    except analysis.AnalysisError as exc:
        raise ValidationError(str(exc)) from exc


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    return value


def _replica_list(value: Any, label: str) -> tuple[int, ...]:
    items = tuple(
        _integer(item, f"{label} item", maximum=analysis.UINT32_MAX)
        for item in _list(value, label)
    )
    if len(set(items)) != len(items):
        raise ValidationError(f"{label} contains duplicate replicas")
    if any(item not in MEMBERSHIP for item in items):
        raise ValidationError(f"{label} contains a replica outside membership")
    return items


def _require_frozen_header(value: Mapping[str, Any], label: str) -> None:
    if _integer(value["replica_count"], f"{label}.replica_count") != 7:
        raise ValidationError("frozen scenario requires replica_count=7")
    if _integer(value["fault_threshold"], f"{label}.fault_threshold") != 2:
        raise ValidationError("frozen scenario requires fault_threshold=2")
    if _integer(value["quorum"], f"{label}.quorum") != 5:
        raise ValidationError("frozen scenario requires quorum=5")
    if _replica_list(value["membership"], f"{label}.membership") != MEMBERSHIP:
        raise ValidationError("frozen scenario requires membership [0,1,2,3,4,5,6]")
    if 3 * FAULT_THRESHOLD + 1 != len(MEMBERSHIP):
        raise ValidationError("internal frozen 3f+1 invariant failed")
    if QUORUM != 2 * FAULT_THRESHOLD + 1:
        raise ValidationError("internal frozen quorum invariant failed")


def _decode_frozen_profile(payload: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ValidationError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object")
    _exact_fields(value, _FROZEN_PROFILE_FIELDS, label)
    expected: Mapping[str, Any] = {
        "schema_version": 1,
        "profile_id": FROZEN_PROFILE_ID,
        "frozen": True,
        "replica_ids": list(MEMBERSHIP),
        "fault_threshold": 2,
        "quorum": 5,
        "authoritative_observer": 2,
        "epoch0_roots": list(MEMBERSHIP),
        "crash_targets": list(CRASHED_REPLICAS),
        "crash_epoch": 0,
        "crash_root": 6,
        "successor_epoch": 1,
        "successor_roots": list(SUCCESSOR_ROOTS),
        "successor_wait_exempt": list(CRASHED_REPLICAS),
        "fanout": 2,
        "pipeline_depth": 2,
        "tree_switch_period_blocks": 1,
        "bucket_width_s": 5,
        "baseline_bucket_count": 7,
        "activation_grace_s": 1,
        "post_bucket_count": 7,
        "minimum_qualifying_reporters": 3,
        "minimum_timeout_observations_per_reporter": 2,
        "minimum_net_reputation_drop": 6,
        "response_reputation_delta": 1,
        "timeout_reputation_delta": -1,
        "aggregation_timeout_s": 0.5,
        "leader_progress_timeout_s": 5.0,
        "leader_activation_grace_s": 1.0,
        "activation_delay_blocks": 5,
        "maximum_stall_s": 10,
        "degraded_maximum_stall_s": 25,
        "block_size": 1,
        "snapshot_seed": 41719,
    }
    if value != expected or value.get("frozen") is not True:
        raise ValidationError(f"{label} differs from the exact frozen v1 settings")
    return value


def _repository_frozen_profile() -> bytes:
    path = Path(__file__).resolve().with_name("profile.json")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read repository frozen profile: {exc}") from exc
    if hashlib.sha256(payload).hexdigest() != FROZEN_PROFILE_SHA256:
        raise ValidationError("repository frozen profile SHA-256 drifted")
    _decode_frozen_profile(payload, "repository frozen profile")
    return payload


def _expected_runtime(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "block_size": profile["block_size"],
        "pipeline_depth": profile["pipeline_depth"],
        "aggregation_timeout_ms": int(profile["aggregation_timeout_s"] * 1000),
        "leader_progress_timeout_ms": int(
            profile["leader_progress_timeout_s"] * 1000
        ),
        "leader_activation_grace_ms": int(
            profile["leader_activation_grace_s"] * 1000
        ),
        "activation_delay_blocks": profile["activation_delay_blocks"],
        "fanout": profile["fanout"],
        "epoch0_roots": list(profile["epoch0_roots"]),
        "successor_roots": list(profile["successor_roots"]),
        "successor_wait_exempt": list(profile["successor_wait_exempt"]),
        "tree_switch_period_blocks": profile["tree_switch_period_blocks"],
        "snapshot_seed": profile["snapshot_seed"],
    }


def _validate_runtime(
    value: Any, profile: Mapping[str, Any]
) -> Mapping[str, Any]:
    runtime = _object(value, "manifest.runtime")
    _exact_fields(runtime, _RUNTIME_FIELDS, "manifest.runtime")
    expected = _expected_runtime(profile)
    static_runtime = {
        field: runtime[field] for field in _RUNTIME_FIELDS if field != "executables"
    }
    if not _json_values_equal(static_runtime, expected):
        raise ValidationError(
            "manifest.runtime differs from the exact frozen effective runtime"
        )
    executables = _object(runtime["executables"], "manifest.runtime.executables")
    _exact_fields(
        executables, _RUNTIME_EXECUTABLE_FIELDS, "manifest.runtime.executables"
    )
    for name, expected_basename in (
        ("hotstuff_app", "hotstuff-app"),
        ("adaptation_manager", "adaptation-manager"),
    ):
        descriptor = _object(executables[name], f"runtime.executables.{name}")
        _exact_fields(
            descriptor,
            _RUNTIME_EXECUTABLE_DESCRIPTOR_FIELDS,
            f"runtime.executables.{name}",
        )
        executable_path = Path(
            _string(descriptor["path"], f"runtime.executables.{name}.path")
        )
        if not executable_path.is_absolute() or executable_path.name != expected_basename:
            raise ValidationError(
                f"runtime executable {name} must use an absolute {expected_basename} path"
            )
        try:
            payload = executable_path.read_bytes()
        except FileNotFoundError as exc:
            raise IncompleteRun(
                f"runtime executable is missing: {executable_path}"
            ) from exc
        except OSError as exc:
            raise IncompleteRun(
                f"cannot read runtime executable {executable_path}: {exc}"
            ) from exc
        digest = _hash(descriptor["sha256"], f"runtime.executables.{name}.sha256")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValidationError(f"runtime executable SHA-256 mismatch: {name}")
    return runtime


def _expected_initial_epoch_input(runtime: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "replica_count": 7,
        "membership": list(MEMBERSHIP),
        "fault_threshold": FAULT_THRESHOLD,
        "quorum": QUORUM,
        "epoch0_trees": [
            {
                "tree_id": root,
                "fanout": runtime["fanout"],
                "pipeline_depth": runtime["pipeline_depth"],
                "members_breadth_first": [
                    (root + offset) % len(MEMBERSHIP)
                    for offset in range(len(MEMBERSHIP))
                ],
                "wait_exempt": [],
            }
            for root in runtime["epoch0_roots"]
        ],
    }


def _expected_replica_effective(
    replica_id: int, runtime: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "replica_id": replica_id,
        "protocol_mode": "adaptive-v2",
        "replica_count": 7,
        "fault_threshold": FAULT_THRESHOLD,
        "quorum": QUORUM,
        "membership": list(MEMBERSHIP),
        "authoritative_observer": analysis.AUTHORITATIVE_SOURCE_ID,
        "block_size": runtime["block_size"],
        "pipeline_depth": runtime["pipeline_depth"],
        "aggregation_timeout_ms": runtime["aggregation_timeout_ms"],
        "leader_progress_timeout_ms": runtime["leader_progress_timeout_ms"],
        "leader_activation_grace_ms": runtime["leader_activation_grace_ms"],
        "fanout": runtime["fanout"],
        "tree_switch_period_blocks": runtime["tree_switch_period_blocks"],
    }


def _read_file_for_runtime(path: Path, label: str) -> tuple[bytes, str]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError as exc:
        raise IncompleteRun(f"missing {label}: {path}") from exc
    except OSError as exc:
        raise IncompleteRun(f"cannot read {label} {path}: {exc}") from exc
    return payload, hashlib.sha256(payload).hexdigest()


def _config_assignments(payload: bytes, label: str) -> Mapping[str, tuple[str, ...]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} is not UTF-8") from exc
    values: dict[str, list[str]] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValidationError(f"{label} line {line_number} is not key=value")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key or not value:
            raise ValidationError(f"{label} line {line_number} has an empty key/value")
        values.setdefault(key, []).append(value)
    return {key: tuple(items) for key, items in values.items()}


def _single_config_value(
    values: Mapping[str, tuple[str, ...]], key: str, label: str
) -> str:
    matches = values.get(key, ())
    if len(matches) != 1:
        raise ValidationError(f"{label} requires exactly one {key} setting")
    return matches[0]


def _validate_main_config_bytes(
    payload: bytes, runtime: Mapping[str, Any]
) -> None:
    values = _config_assignments(payload, "effective main config")
    exact_strings = {
        "block-size": str(runtime["block_size"]),
        "fan-out": str(runtime["fanout"]),
        "async_blocks": str(runtime["pipeline_depth"]),
        "tree-switch-period": str(runtime["tree_switch_period_blocks"]),
        "epoch-protocol-mode": "adaptive_v2",
        "epoch-change-minimum-activation-delay": str(
            runtime["activation_delay_blocks"]
        ),
        "epoch-change-maximum-activation-delay": str(
            runtime["activation_delay_blocks"]
        ),
    }
    for key, expected in exact_strings.items():
        if _single_config_value(values, key, "effective main config") != expected:
            raise ValidationError(f"effective main config {key} differs from runtime")
    timeout_settings = {
        "aggregation-timeout": runtime["aggregation_timeout_ms"],
        "leader-progress-timeout": runtime["leader_progress_timeout_ms"],
        "leader-activation-grace": runtime["leader_activation_grace_ms"],
    }
    for key, expected_ms in timeout_settings.items():
        encoded = _single_config_value(values, key, "effective main config")
        try:
            actual_ms = Decimal(encoded) * 1000
        except InvalidOperation as exc:
            raise ValidationError(f"effective main config {key} is not numeric") from exc
        if actual_ms != Decimal(expected_ms):
            raise ValidationError(f"effective main config {key} differs from runtime")


def _single_argv_value(argv: Sequence[str], flag: str) -> str:
    if argv.count(flag) != 1:
        raise ValidationError(f"launch argv must contain exactly one {flag}")
    index = argv.index(flag)
    if index + 1 >= len(argv):
        raise ValidationError(f"launch argv {flag} has no value")
    return argv[index + 1]


def _validate_launch_arguments(
    value: Mapping[str, Any], runtime: Mapping[str, Any]
) -> None:
    _exact_fields(value, _LAUNCH_ARGUMENT_FIELDS, "launch arguments")
    if _integer(value["schema_version"], "launch schema_version") != SCHEMA_VERSION:
        raise ValidationError("unsupported launch-arguments schema_version")
    process_values = _list(value["processes"], "launch processes")
    expected_sources = [
        ("replica", f"replica-{replica}") for replica in MEMBERSHIP
    ] + [("adaptation_manager", "adaptive-manager")]
    if len(process_values) != len(expected_sources):
        raise ValidationError("launch arguments must bind seven replicas and one manager")
    replica_runtime_options = {
        field: runtime[field] for field in _REPLICA_RUNTIME_OPTION_FIELDS
    }
    executables = _object(runtime["executables"], "runtime.executables")
    app_executable = _object(executables["hotstuff_app"], "hotstuff executable")
    manager_executable = _object(
        executables["adaptation_manager"], "adaptation-manager executable"
    )
    replica_tls_hashes: list[str] = []
    issuer_hashes: set[str] = set()
    manager_certificate_hashes: set[str] = set()
    main_config_hashes: set[str] = set()
    manager_options: Mapping[str, Any] | None = None
    for index, (item, expected_source) in enumerate(
        zip(process_values, expected_sources)
    ):
        process = _object(item, f"launch.processes[{index}]")
        _exact_fields(
            process, _LAUNCH_PROCESS_FIELDS, f"launch.processes[{index}]"
        )
        source = (
            _string(process["source_kind"], "launch source_kind"),
            _string(process["source_id"], "launch source_id"),
        )
        if source != expected_source:
            raise ValidationError("launch process identities are not canonical")
        argv_values = _list(process["argv"], "launch argv")
        if not argv_values or any(
            not isinstance(argument, str) or not argument for argument in argv_values
        ):
            raise ValidationError("launch argv must contain non-empty strings")
        argv = tuple(argv_values)
        options = _object(process["effective_options"], "launch effective_options")
        if source[0] == "replica":
            _exact_fields(
                options,
                _REPLICA_EFFECTIVE_OPTION_FIELDS,
                "replica launch effective_options",
            )
            actual_runtime_options = {
                field: options[field] for field in _REPLICA_RUNTIME_OPTION_FIELDS
            }
            if not _json_values_equal(
                actual_runtime_options, replica_runtime_options
            ):
                raise ValidationError(
                    "replica launch effective_options differ from manifest.runtime"
                )
            digests = {
                field: _hash(options[field], f"replica launch {field}")
                for field in (
                    "binary_sha256",
                    "main_config_sha256",
                    "replica_config_sha256",
                    "bls_public_key_sha256",
                    "tls_certificate_sha256",
                    "issuer_public_key_sha256",
                    "manager_tls_certificate_sha256",
                )
            }
            if digests["binary_sha256"] != app_executable["sha256"]:
                raise ValidationError(
                    "replica launch binary hash differs from manifest.runtime"
                )
            replica_id = int(source[1].removeprefix("replica-"))
            if (
                len(argv) != 5
                or argv[0] != app_executable["path"]
                or argv[1] != "--conf"
                or Path(argv[2]).name != "hotstuff.gen.conf"
                or argv[3] != "--conf"
                or Path(argv[4]).name != f"replica-{replica_id}.conf"
            ):
                raise ValidationError(
                    f"replica-{replica_id} launch argv does not bind its effective configs"
                )
            main_config_path = Path(argv[2])
            replica_config_path = Path(argv[4])
            if not main_config_path.is_absolute() or not replica_config_path.is_absolute():
                raise ValidationError("replica launch config paths must be absolute")
            main_payload, main_digest = _read_file_for_runtime(
                main_config_path, "effective main config"
            )
            replica_payload, replica_digest = _read_file_for_runtime(
                replica_config_path, f"replica-{replica_id} effective private config"
            )
            if main_digest != digests["main_config_sha256"]:
                raise ValidationError("effective main config SHA-256 mismatch")
            if replica_digest != digests["replica_config_sha256"]:
                raise ValidationError(
                    f"replica-{replica_id} private config SHA-256 mismatch"
                )
            _validate_main_config_bytes(main_payload, runtime)
            replica_values = _config_assignments(
                replica_payload, f"replica-{replica_id} private config"
            )
            if _single_config_value(
                replica_values, "idx", f"replica-{replica_id} private config"
            ) != str(replica_id):
                raise ValidationError(
                    f"replica-{replica_id} private config binds the wrong idx"
                )
            main_config_hashes.add(main_digest)
            replica_tls_hashes.append(digests["tls_certificate_sha256"])
            issuer_hashes.add(digests["issuer_public_key_sha256"])
            manager_certificate_hashes.add(
                digests["manager_tls_certificate_sha256"]
            )
        else:
            _exact_fields(
                options,
                _MANAGER_EFFECTIVE_OPTION_FIELDS,
                "manager launch effective_options",
            )
            if options["activation_delay_blocks"] != runtime[
                "activation_delay_blocks"
            ] or options["snapshot_seed"] != runtime["snapshot_seed"]:
                raise ValidationError(
                    "manager launch effective_options differ from manifest.runtime"
                )
            if _hash(options["binary_sha256"], "manager binary_sha256") != (
                manager_executable["sha256"]
            ):
                raise ValidationError(
                    "manager launch binary hash differs from manifest.runtime"
                )
            _hash(options["tls_certificate_sha256"], "manager TLS certificate hash")
            _hash(options["issuer_public_key_sha256"], "manager issuer key hash")
            replica_hash_values = _list(
                options["replica_tls_certificate_sha256"],
                "manager replica TLS certificate hashes",
            )
            if len(replica_hash_values) != len(MEMBERSHIP):
                raise ValidationError(
                    "manager must bind seven replica TLS certificate hashes"
                )
            for replica_index, digest in enumerate(replica_hash_values):
                _hash(digest, f"manager replica-{replica_index} TLS certificate hash")
            if argv[0] != manager_executable["path"]:
                raise ValidationError("manager launch argv names the wrong binary")
            if _single_argv_value(argv, "--activation-delay-blocks") != str(
                runtime["activation_delay_blocks"]
            ):
                raise ValidationError(
                    "manager launch activation delay differs from manifest.runtime"
                )
            for private_flag in ("--tls-privkey", "--issuer-private-key"):
                if _single_argv_value(argv, private_flag) != "<redacted>":
                    raise ValidationError(
                        f"manager launch {private_flag} must be redacted"
                    )
            if _single_argv_value(argv, "--tls-cert") != "<fingerprinted>":
                raise ValidationError(
                    "manager public TLS certificate must be fingerprinted in launch argv"
                )
            if argv.count("--replica") != len(MEMBERSHIP):
                raise ValidationError("manager launch argv must bind seven replicas")
            replica_arguments = [
                argv[position + 1]
                for position, argument in enumerate(argv[:-1])
                if argument == "--replica"
            ]
            for replica_id, replica_argument in enumerate(replica_arguments):
                components = replica_argument.split(",", 2)
                if (
                    len(components) != 3
                    or components[0] != str(replica_id)
                    or not components[1]
                    or components[2] != "<fingerprinted>"
                ):
                    raise ValidationError(
                        "manager replica launch arguments are not canonically fingerprinted"
                    )
            manager_options = options

    if len(main_config_hashes) != 1:
        raise ValidationError("replicas did not launch from one common main config")
    if len(issuer_hashes) != 1 or len(manager_certificate_hashes) != 1:
        raise ValidationError(
            "replicas disagree on issuer or manager certificate fingerprints"
        )
    assert manager_options is not None
    if manager_options["issuer_public_key_sha256"] not in issuer_hashes:
        raise ValidationError("manager and replicas bind different issuer public keys")
    if manager_options["tls_certificate_sha256"] not in manager_certificate_hashes:
        raise ValidationError("manager and replicas bind different manager TLS certificates")
    if not _json_values_equal(
        manager_options["replica_tls_certificate_sha256"], replica_tls_hashes
    ):
        raise ValidationError("manager and replicas bind different replica TLS certificates")


def _load_runtime_artifacts(
    value: Any, *, manifest_path: Path, runtime: Mapping[str, Any]
) -> tuple[RuntimeArtifactSpec, ...]:
    entries = _list(value, "manifest.runtime_artifacts")
    expected = [
        ("replica_config", replica, f"runtime/replica-{replica}.effective.json")
        for replica in MEMBERSHIP
    ] + [
        ("epoch_input", None, "runtime/epoch-input.json"),
        ("launch_arguments", None, "runtime/launch-arguments.json"),
    ]
    if len(entries) != len(expected):
        raise IncompleteRun(
            "runtime_artifacts must contain seven replica configs, epoch input, "
            "and launch arguments"
        )
    base = manifest_path.resolve().parent
    artifacts: list[RuntimeArtifactSpec] = []
    for index, (item, expected_identity) in enumerate(zip(entries, expected)):
        entry = _object(item, f"runtime_artifacts[{index}]")
        _exact_fields(
            entry, _RUNTIME_ARTIFACT_FIELDS, f"runtime_artifacts[{index}]"
        )
        kind = _string(entry["kind"], f"runtime_artifacts[{index}].kind")
        replica_id = entry["replica_id"]
        if replica_id is not None:
            replica_id = _integer(
                replica_id,
                f"runtime_artifacts[{index}].replica_id",
                maximum=analysis.UINT32_MAX,
            )
        relative = _string(entry["path"], f"runtime_artifacts[{index}].path")
        if (kind, replica_id, relative) != expected_identity:
            raise ValidationError("runtime artifact identities or ordering are not canonical")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValidationError("runtime artifact path must be manifest-relative")
        path = (base / relative_path).resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise ValidationError("runtime artifact path escapes manifest directory") from exc
        try:
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise IncompleteRun(f"missing runtime artifact: {relative}") from exc
        except OSError as exc:
            raise IncompleteRun(f"cannot read runtime artifact {relative}: {exc}") from exc
        digest = _hash(entry["sha256"], f"runtime_artifacts[{index}].sha256")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValidationError(f"runtime artifact SHA-256 mismatch: {relative}")
        artifacts.append(
            RuntimeArtifactSpec(kind, replica_id, relative, path, digest, payload)
        )

    for artifact in artifacts[: len(MEMBERSHIP)]:
        assert artifact.replica_id is not None
        decoded = _load_json_bytes(artifact.payload, artifact.relative_path)
        _exact_fields(
            decoded,
            _REPLICA_EFFECTIVE_FIELDS,
            f"replica-{artifact.replica_id} effective config",
        )
        expected_replica = _expected_replica_effective(
            artifact.replica_id, runtime
        )
        if not _json_values_equal(decoded, expected_replica):
            raise ValidationError(
                f"replica-{artifact.replica_id} effective config differs from "
                "the frozen runtime"
            )

    epoch_input = _load_json_bytes(artifacts[-2].payload, "initial epoch input")
    _exact_fields(epoch_input, _INITIAL_EPOCH_INPUT_FIELDS, "initial epoch input")
    for index, tree_value in enumerate(
        _list(epoch_input["epoch0_trees"], "initial epoch trees")
    ):
        _exact_fields(
            _object(tree_value, f"initial epoch tree {index}"),
            _INITIAL_EPOCH_TREE_FIELDS,
            f"initial epoch tree {index}",
        )
    if not _json_values_equal(epoch_input, _expected_initial_epoch_input(runtime)):
        raise ValidationError("initial epoch input differs from the frozen runtime")

    launch = _load_json_bytes(artifacts[-1].payload, "launch arguments")
    _validate_launch_arguments(launch, runtime)
    return tuple(artifacts)


def load_manifest(path: Path) -> Manifest:
    raw = _load_json(path, "manifest")
    _exact_fields(raw, _MANIFEST_FIELDS, "manifest")
    if _integer(raw["schema_version"], "manifest.schema_version") != SCHEMA_VERSION:
        raise ValidationError("unsupported manifest schema_version")
    if raw["scenario"] != SCENARIO:
        raise ValidationError(f"manifest.scenario must be {SCENARIO}")
    _require_frozen_header(raw, "manifest")
    run_id = _string(raw["run_id"], "manifest.run_id")
    revision = _string(raw["kauri_revision"], "manifest.kauri_revision")
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValidationError("kauri_revision must be a full lowercase 40-digit Git SHA")
    if raw["kauri_worktree_clean"] is not True:
        raise ValidationError("accepted evidence requires kauri_worktree_clean=true")
    completion = _object(raw["run_completion"], "manifest.run_completion")
    _exact_fields(completion, _RUN_COMPLETION_FIELDS, "manifest.run_completion")
    if completion["complete"] is not True:
        raise IncompleteRun("run_completion.complete is not true")
    if completion["interrupted"] is not False:
        raise IncompleteRun("interrupted run cannot be accepted")
    if completion["runtime_error"] is not None:
        raise ValidationError("completed run records a runtime_error")
    if completion["unexpected_survivor_exits"] != []:
        raise ValidationError("completed run records unexpected survivor exits")

    profile = _object(raw["profile"], "manifest.profile")
    _exact_fields(profile, _PROFILE_FIELDS, "manifest.profile")
    profile_identity = _string(profile["identity"], "manifest.profile.identity")
    if profile_identity != FROZEN_PROFILE_ID:
        raise ValidationError("profile identity is not the frozen N7 campaign profile")
    profile_relative = Path(_string(profile["path"], "manifest.profile.path"))
    if profile_relative.is_absolute() or ".." in profile_relative.parts:
        raise ValidationError("profile path must be manifest-relative")
    if profile_relative != Path("profile.json"):
        raise ValidationError("frozen profile path must be exactly profile.json")
    profile_path = (path.resolve().parent / profile_relative).resolve()
    try:
        profile_path.relative_to(path.resolve().parent)
    except ValueError as exc:
        raise ValidationError("profile path escapes manifest directory") from exc
    try:
        profile_bytes = profile_path.read_bytes()
    except FileNotFoundError as exc:
        raise IncompleteRun(f"missing frozen profile: {profile_relative}") from exc
    except OSError as exc:
        raise IncompleteRun(f"cannot read frozen profile: {exc}") from exc
    profile_sha = _hash(profile["sha256"], "manifest.profile.sha256")
    if profile_sha != FROZEN_PROFILE_SHA256:
        raise ValidationError("manifest does not pin the canonical frozen profile SHA-256")
    if hashlib.sha256(profile_bytes).hexdigest() != profile_sha:
        raise ValidationError("profile sha256 does not match the preserved profile")
    canonical_profile = _repository_frozen_profile()
    if profile_bytes != canonical_profile:
        raise ValidationError("run profile is not byte-exact canonical frozen v1")
    profile_json = _decode_frozen_profile(profile_bytes, "run frozen profile")
    runtime = _validate_runtime(raw["runtime"], profile_json)
    runtime_artifacts = _load_runtime_artifacts(
        raw["runtime_artifacts"], manifest_path=path, runtime=runtime
    )
    observer = _string(
        raw["authoritative_observer"], "manifest.authoritative_observer"
    )
    if observer != analysis.AUTHORITATIVE_SOURCE_ID:
        raise ValidationError("authoritative observer must be replica-2")

    manager = _object(raw["manager"], "manifest.manager")
    _exact_fields(manager, _MANAGER_FIELDS, "manifest.manager")
    manager_id = _string(manager["source_id"], "manifest.manager.source_id")
    if manager["receives_crash_ground_truth"] is not False:
        raise ValidationError(
            "manager.receives_crash_ground_truth must be false"
        )
    if manager_id != "adaptive-manager":
        raise ValidationError("manager source_id must be adaptive-manager")

    width = _integer(raw["bucket_width_ns"], "manifest.bucket_width_ns", minimum=1)
    frozen_bucket_width_ns = int(profile_json["bucket_width_s"]) * 1_000_000_000
    if width != analysis.BUCKET_WIDTH_NS or width != frozen_bucket_width_ns:
        raise ValidationError(
            f"frozen bucket width must be {analysis.BUCKET_WIDTH_NS} ns"
        )
    baseline_start_ns = _integer(
        raw["baseline_start_ns"], "manifest.baseline_start_ns", minimum=1
    )
    end_ns = _integer(raw["end_ns"], "manifest.end_ns", minimum=1)
    if end_ns <= baseline_start_ns:
        raise ValidationError("manifest end must follow baseline start")
    activation_grace_ns = _integer(
        raw["activation_grace_ns"],
        "manifest.activation_grace_ns",
        minimum=1,
    )
    if activation_grace_ns != int(profile_json["activation_grace_s"]) * 1_000_000_000:
        raise ValidationError("activation grace differs from the frozen profile")
    baseline_bucket_count = _integer(
        profile_json["baseline_bucket_count"],
        "profile.baseline_bucket_count",
        minimum=1,
    )
    post_bucket_count = _integer(
        profile_json["post_bucket_count"],
        "profile.post_bucket_count",
        minimum=1,
    )
    maximum_stall_ns = _integer(
        profile_json["maximum_stall_s"],
        "profile.maximum_stall_s",
        minimum=1,
    ) * 1_000_000_000
    degraded_maximum_stall_ns = _integer(
        profile_json["degraded_maximum_stall_s"],
        "profile.degraded_maximum_stall_s",
        minimum=1,
    ) * 1_000_000_000

    source_values = _list(raw["sources"], "manifest.sources")
    sources: list[SourceSpec] = []
    keys: set[tuple[str, str]] = set()
    paths: set[str] = set()
    base = path.resolve().parent
    for index, item in enumerate(source_values):
        source = _object(item, f"manifest.sources[{index}]")
        _exact_fields(source, _SOURCE_FIELDS, f"manifest.sources[{index}]")
        kind = _string(source["source_kind"], f"sources[{index}].source_kind")
        if kind not in SOURCE_KINDS:
            raise ValidationError(f"unsupported source_kind: {kind}")
        source_id = _string(source["source_id"], f"sources[{index}].source_id")
        instance = _string(
            source["source_instance"], f"sources[{index}].source_instance"
        )
        pid = _integer(source["pid"], f"sources[{index}].pid", minimum=2)
        pgid = _integer(source["pgid"], f"sources[{index}].pgid", minimum=2)
        relative = _string(source["path"], f"sources[{index}].path")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValidationError("source paths must be safe manifest-relative paths")
        if relative_path.suffix != ".jsonl":
            raise ValidationError("source paths must end in .jsonl")
        key = (kind, source_id)
        if key in keys:
            raise ValidationError(f"duplicate source identity: {key}")
        if relative in paths:
            raise ValidationError(f"duplicate source path: {relative}")
        keys.add(key)
        paths.add(relative)
        resolved = (base / relative_path).resolve()
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ValidationError("source path escapes manifest directory") from exc
        sources.append(
            SourceSpec(kind, source_id, instance, pid, pgid, relative, resolved)
        )

    expected_replicas = {("replica", f"replica-{replica}") for replica in MEMBERSHIP}
    missing_replicas = expected_replicas - keys
    if missing_replicas:
        raise IncompleteRun(
            "manifest is missing replica sources: "
            + ", ".join(source_id for _, source_id in sorted(missing_replicas))
        )
    actual_replicas = {key for key in keys if key[0] == "replica"}
    if actual_replicas != expected_replicas:
        raise ValidationError("manifest contains a replica source outside membership")
    replica_source_values = [
        source for source in sources if source.source_kind == "replica"
    ]
    if len({source.pid for source in replica_source_values}) != 7:
        raise ValidationError("replica sources must bind seven distinct PIDs")
    if len({source.pgid for source in replica_source_values}) != 7:
        raise ValidationError("replica sources must bind seven distinct PGIDs")
    manager_sources = {key for key in keys if key[0] == "adaptation_manager"}
    if manager_sources != {("adaptation_manager", manager_id)}:
        raise ValidationError("manifest must contain exactly one adaptation manager")
    if ("adaptation_manager", manager_id) not in keys:
        raise IncompleteRun("manifest is missing the bound adaptation-manager source")
    markers_value = _list(raw["crash_markers"], "manifest.crash_markers")
    markers: list[CrashMarkerSpec] = []
    for index, item in enumerate(markers_value):
        marker = _object(item, f"manifest.crash_markers[{index}]")
        _exact_fields(marker, _CRASH_MARKER_FIELDS, f"crash_markers[{index}]")
        confirmed = _object(
            marker["confirmed_exit"], f"crash_markers[{index}].confirmed_exit"
        )
        _exact_fields(
            confirmed,
            _CONFIRMED_EXIT_FIELDS,
            f"crash_markers[{index}].confirmed_exit",
        )
        replica_id = _integer(
            marker["replica_id"],
            f"crash_markers[{index}].replica_id",
            maximum=analysis.UINT32_MAX,
        )
        pid = _integer(marker["pid"], f"crash_markers[{index}].pid", minimum=2)
        pgid = _integer(marker["pgid"], f"crash_markers[{index}].pgid", minimum=2)
        requested_ns = _integer(
            marker["requested_monotonic_raw_ns"],
            f"crash_markers[{index}].requested_monotonic_raw_ns",
            minimum=1,
        )
        if marker["signal"] != "SIGKILL" or marker["signal_number"] != 9:
            raise ValidationError("crash requests must use SIGKILL (9)")
        if confirmed["pid"] != pid or confirmed["pgid"] != pgid:
            raise ValidationError("confirmed exit must bind the requested PID and PGID")
        if confirmed["signal"] != "SIGKILL" or confirmed["signal_number"] != 9:
            raise ValidationError("confirmed exit must report SIGKILL (9)")
        confirmed_ns = _integer(
            confirmed["observed_monotonic_raw_ns"],
            f"crash_markers[{index}].confirmed_exit.observed_monotonic_raw_ns",
            minimum=1,
        )
        if confirmed_ns < requested_ns:
            raise ValidationError("confirmed exit precedes its crash request")
        markers.append(
            CrashMarkerSpec(replica_id, pid, pgid, requested_ns, confirmed_ns)
        )
    if tuple(marker.replica_id for marker in markers) != CRASHED_REPLICAS:
        raise ValidationError("crash markers must identify replicas 0 then 1 exactly")
    if len({marker.pid for marker in markers}) != 2:
        raise ValidationError("crash markers must bind distinct PIDs")
    if len({marker.pgid for marker in markers}) != 2:
        raise ValidationError("crash markers must bind distinct PGIDs")
    replica_sources = {
        int(source.source_id.removeprefix("replica-")): source
        for source in sources
        if source.source_kind == "replica"
    }
    for marker in markers:
        source = replica_sources[marker.replica_id]
        if marker.pid != source.pid or marker.pgid != source.pgid:
            raise ValidationError(
                f"replica {marker.replica_id} crash PID/PGID does not match its source process"
            )
    if not all(
        baseline_start_ns < marker.requested_ns <= marker.confirmed_ns < end_ns
        for marker in markers
    ):
        raise ValidationError("crash request/exit evidence must lie in the run window")

    return Manifest(
        raw=raw,
        path=path.resolve(),
        run_id=run_id,
        kauri_revision=revision,
        profile_identity=profile_identity,
        profile_path=profile_path,
        profile_bytes=profile_bytes,
        authoritative_observer=observer,
        manager_source_id=manager_id,
        baseline_start_ns=baseline_start_ns,
        end_ns=end_ns,
        activation_grace_ns=activation_grace_ns,
        baseline_bucket_count=baseline_bucket_count,
        post_bucket_count=post_bucket_count,
        maximum_stall_ns=maximum_stall_ns,
        degraded_maximum_stall_ns=degraded_maximum_stall_ns,
        runtime=runtime,
        runtime_artifacts=runtime_artifacts,
        sources=tuple(sources),
        crash_markers=tuple(markers),
    )


def _load_tree(
    value: Any, *, epoch_number: int, epoch_digest: str, index: int
) -> TreeDefinition:
    tree = _object(value, f"epochs[{epoch_number}].trees[{index}]")
    _exact_fields(tree, _TREE_FIELDS, f"epochs[{epoch_number}].trees[{index}]")
    return TreeDefinition(
        epoch_number=epoch_number,
        epoch_digest=epoch_digest,
        tree_id=_integer(
            tree["tree_id"], f"epochs[{epoch_number}].trees[{index}].tree_id"
        ),
        fanout=_integer(
            tree["fanout"],
            f"epochs[{epoch_number}].trees[{index}].fanout",
            minimum=1,
        ),
        members=_replica_list(
            tree["members_breadth_first"],
            f"epochs[{epoch_number}].trees[{index}].members_breadth_first",
        ),
        wait_exempt=_replica_list(
            tree["wait_exempt"],
            f"epochs[{epoch_number}].trees[{index}].wait_exempt",
        ),
    )


def load_epochs(path: Path) -> EpochDocument:
    raw = _load_json(path, "epoch definition")
    _exact_fields(raw, _EPOCH_DOCUMENT_FIELDS, "epoch definition")
    if _integer(raw["schema_version"], "epochs.schema_version") != SCHEMA_VERSION:
        raise ValidationError("unsupported epoch schema_version")
    _require_frozen_header(raw, "epochs")
    epoch_values = _list(raw["epochs"], "epochs.epochs")
    if len(epoch_values) != 2:
        raise ValidationError("epoch definition must contain exactly epochs 0 and 1")

    epochs: list[EpochDefinition] = []
    for index, item in enumerate(epoch_values):
        epoch = _object(item, f"epochs[{index}]")
        _exact_fields(epoch, _EPOCH_FIELDS, f"epochs[{index}]")
        number = _integer(
            epoch["epoch_number"],
            f"epochs[{index}].epoch_number",
            maximum=analysis.UINT32_MAX,
        )
        if number != index:
            raise ValidationError("epoch numbers must be exactly 0 then 1")
        digest = _hash(epoch["epoch_digest"], f"epochs[{index}].epoch_digest")
        trees = tuple(
            _load_tree(
                tree,
                epoch_number=number,
                epoch_digest=digest,
                index=tree_index,
            )
            for tree_index, tree in enumerate(_list(epoch["trees"], "epoch trees"))
        )
        command = epoch["command"]
        if command is not None:
            command = _object(command, f"epochs[{index}].command")
            _exact_fields(command, _COMMAND_FIELDS, f"epochs[{index}].command")
        epochs.append(EpochDefinition(number, digest, trees, command))

    initial, successor = epochs
    if initial.epoch_digest == successor.epoch_digest:
        raise ValidationError("successor epoch digest must differ from epoch 0")
    if initial.command is not None:
        raise ValidationError("epoch 0 command must be null")
    if successor.command is None:
        raise ValidationError("successor epoch must contain its committed command")

    if len(initial.trees) != 7:
        raise ValidationError("epoch 0 must contain seven cyclic trees")
    for tree_id, tree in enumerate(initial.trees):
        if tree.tree_id != tree_id or tree.fanout != 2:
            raise ValidationError("epoch 0 trees require ids 0..6 and fanout 2")
        expected = tuple((tree_id + offset) % 7 for offset in range(7))
        if tree.members != expected:
            raise ValidationError(
                f"epoch 0 tree {tree_id} is not the frozen cyclic ordering"
            )
        if tree.wait_exempt:
            raise ValidationError("epoch 0 must not have wait-exempt replicas")

    if len(successor.trees) != 5:
        raise ValidationError("successor epoch must contain exactly five trees")
    for tree_id, tree in enumerate(successor.trees):
        if tree.tree_id != tree_id or tree.fanout != 2:
            raise ValidationError("successor trees require ids 0..4 and fanout 2")
        if len(tree.members) != len(MEMBERSHIP):
            raise ValidationError("successor tree must contain all seven replicas")
        if set(tree.members) != set(MEMBERSHIP):
            raise ValidationError("successor tree membership must remain unchanged")
        if tree.wait_exempt != CRASHED_REPLICAS:
            raise ValidationError("only replicas 0 and 1 may be wait-exempt")
        leaf_start = (len(tree.members) - 2) // tree.fanout + 1
        for failed in CRASHED_REPLICAS:
            if tree.members.index(failed) < leaf_start:
                raise ValidationError(
                    f"replica {failed} is not a physical leaf in successor tree {tree_id}"
                )
    if tuple(tree.leader for tree in successor.trees) != SUCCESSOR_ROOTS:
        raise ValidationError("successor roots must be exactly 2,3,4,5,6")

    command = successor.command
    assert command is not None
    for field in (
        "command_block_height",
        "activation_delay_blocks",
        "activation_height",
    ):
        _integer(command[field], f"successor.command.{field}", minimum=1)
    for field in ("predecessor_epoch_number", "successor_epoch_number"):
        _integer(
            command[field],
            f"successor.command.{field}",
            maximum=analysis.UINT32_MAX,
        )
    for field in (
        "command_block_hash",
        "payload_digest",
        "predecessor_epoch_digest",
        "successor_epoch_digest",
    ):
        _hash(command[field], f"successor.command.{field}")
    if command["predecessor_epoch_number"] != 0:
        raise ValidationError("command predecessor epoch must be 0")
    if command["successor_epoch_number"] != 1:
        raise ValidationError("command successor epoch must be 1")
    if command["predecessor_epoch_digest"] != initial.epoch_digest:
        raise ValidationError("command predecessor digest does not match epoch 0")
    if command["successor_epoch_digest"] != successor.epoch_digest:
        raise ValidationError("command successor digest does not match epoch 1")
    if command["activation_height"] != (
        command["command_block_height"] + command["activation_delay_blocks"]
    ):
        raise ValidationError("command activation height must equal h_c + delta")

    leader_map: dict[analysis.ConfigurationKey, int] = {}
    for epoch in epochs:
        for tree in epoch.trees:
            if tree.configuration_key in leader_map:
                raise ValidationError("duplicate configuration identity")
            leader_map[tree.configuration_key] = tree.leader
    return EpochDocument(raw, path.resolve(), initial, successor, leader_map)


def _read_source_events(
    manifest: Manifest,
) -> tuple[dict[tuple[str, str], tuple[analysis.StructuredEvent, ...]], dict[tuple[str, str], str]]:
    streams: dict[tuple[str, str], tuple[analysis.StructuredEvent, ...]] = {}
    texts: dict[tuple[str, str], str] = {}
    for source in manifest.sources:
        try:
            text = source.path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise IncompleteRun(f"missing raw source file: {source.relative_path}") from exc
        except OSError as exc:
            raise IncompleteRun(
                f"cannot read raw source file {source.relative_path}: {exc}"
            ) from exc
        try:
            events = analysis.parse_structured_events(
                text,
                expected_run_id=manifest.run_id,
                expected_source_kind=source.source_kind,
                expected_source_id=source.source_id,
                expected_source_instance=source.source_instance,
                allow_legacy_prefix=False,
            )
        except analysis.AnalysisError as exc:
            raise ValidationError(
                f"invalid raw source {source.relative_path}: {exc}"
            ) from exc
        if not events:
            raise IncompleteRun(f"raw source is empty: {source.relative_path}")
        if events[0].source_sequence != 1:
            raise IncompleteRun(
                f"raw source does not begin at sequence 1: {source.relative_path}"
            )
        expected_sequence = 1
        for event in events:
            if event.source_sequence != expected_sequence:
                raise IncompleteRun(
                    f"raw source has a sequence gap at {source.relative_path}: "
                    f"expected {expected_sequence}, got {event.source_sequence}"
                )
            if event.timestamp_ns == 0:
                raise ValidationError("source_monotonic_ns must be nonzero")
            expected_sequence += 1
        streams[source.key] = events
        texts[source.key] = text
    return streams, texts


def _event_at_sequence(
    events: Sequence[analysis.StructuredEvent], sequence: int
) -> analysis.StructuredEvent:
    for event in events:
        if event.source_sequence == sequence:
            return event
    raise IncompleteRun(f"referenced source_sequence {sequence} is absent")


def _validate_process_lifecycle(
    manifest: Manifest,
    streams: Mapping[tuple[str, str], Sequence[analysis.StructuredEvent]],
) -> None:
    required_keys = [
        ("replica", f"replica-{replica}") for replica in MEMBERSHIP
    ] + [("adaptation_manager", manifest.manager_source_id)]
    for key in required_keys:
        events = streams[key]
        lifecycle = [event for event in events if event.event_type.startswith("process.")]
        for event in lifecycle:
            _exact_fields(event.payload, _PROCESS_FIELDS, "process lifecycle payload")
            exit_status = event.payload["exit_status"]
            if exit_status is not None:
                _integer(
                    exit_status,
                    "process exit_status",
                    minimum=-(1 << 31),
                    maximum=(1 << 31) - 1,
                )
        started = [event for event in lifecycle if event.event_type == "process.started"]
        ready = [event for event in lifecycle if event.event_type == "process.ready"]
        if len(started) != 1 or len(ready) != 1:
            raise IncompleteRun(
                f"{key[1]} requires exactly one process.started and process.ready event"
            )
        if not (
            started[0].timestamp_ns
            <= ready[0].timestamp_ns
            < manifest.baseline_start_ns
        ):
            raise ValidationError(
                f"{key[1]} lifecycle is not ready before baseline"
            )
        if started[0].payload["exit_status"] is not None or ready[0].payload[
            "exit_status"
        ] is not None:
            raise ValidationError("started/ready lifecycle exit_status must be null")

        if key[0] == "replica" and int(key[1].removeprefix("replica-")) in CRASHED_REPLICAS:
            if any(
                event.event_type
                in ("process.stopping", "process.stopped", "process.exited")
                for event in lifecycle
            ):
                raise ValidationError(
                    f"crashed {key[1]} emitted an orderly or self-reported exit"
                )
            continue

        stopping = [
            event for event in lifecycle if event.event_type == "process.stopping"
        ]
        stopped = [event for event in lifecycle if event.event_type == "process.stopped"]
        if len(stopping) != 1 or len(stopped) != 1:
            raise IncompleteRun(
                f"completed run lacks one stopping/stopped pair for {key[1]}"
            )
        if not (
            manifest.end_ns
            <= stopping[0].timestamp_ns
            <= stopped[0].timestamp_ns
        ):
            raise ValidationError(f"{key[1]} stopped before measurement end")
        if stopping[0].payload["exit_status"] is not None or stopped[0].payload[
            "exit_status"
        ] is not None:
            raise ValidationError("stopping/stopped lifecycle exit_status must be null")
        if any(event.event_type == "process.exited" for event in lifecycle):
            raise ValidationError(f"surviving {key[1]} emitted process.exited")


def _validate_crash_markers(
    manifest: Manifest,
    streams: Mapping[tuple[str, str], Sequence[analysis.StructuredEvent]],
) -> tuple[int, int]:
    # The process-lifecycle event payload has no subject identity and a SIGKILLed
    # process cannot emit its own exit.  The canonical runner manifest therefore
    # supplies the authoritative PID/PGID/signal/raw-clock binding.
    all_forced = [
        event
        for events in streams.values()
        for event in events
        if event.event_type == "process.forced_crash_requested"
    ]
    if all_forced:
        raise ValidationError(
            "unbound process.forced_crash_requested event cannot replace manifest crash evidence"
        )
    for marker in manifest.crash_markers:
        events = streams[("replica", f"replica-{marker.replica_id}")]
        if any(event.timestamp_ns > marker.confirmed_ns for event in events):
            raise ValidationError(
                f"replica-{marker.replica_id} emitted after its confirmed SIGKILL exit"
            )
    return tuple(marker.requested_ns for marker in manifest.crash_markers)  # type: ignore[return-value]


def _parse_command_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    _exact_fields(payload, _COMMAND_FIELDS, "epoch.command_committed payload")
    result = dict(payload)
    for field in (
        "command_block_height",
        "activation_delay_blocks",
        "activation_height",
    ):
        result[field] = _integer(payload[field], field, minimum=1)
    for field in ("predecessor_epoch_number", "successor_epoch_number"):
        result[field] = _integer(
            payload[field], field, maximum=analysis.UINT32_MAX
        )
    for field in (
        "command_block_hash",
        "payload_digest",
        "predecessor_epoch_digest",
        "successor_epoch_digest",
    ):
        result[field] = _hash(payload[field], field)
    if result["successor_epoch_number"] != result["predecessor_epoch_number"] + 1:
        raise ValidationError("command successor must be predecessor + 1")
    if result["activation_height"] != (
        result["command_block_height"] + result["activation_delay_blocks"]
    ):
        raise ValidationError("command activation height must equal h_c + delta")
    return result


def _validate_command_and_activation(
    manifest: Manifest,
    epochs: EpochDocument,
    streams: Mapping[tuple[str, str], Sequence[analysis.StructuredEvent]],
    crash_complete_ns: int,
) -> tuple[int, int]:
    expected_command = dict(epochs.successor.command or {})
    command_times: dict[int, int] = {}
    activation_times: dict[int, int] = {}
    for replica in CRASHED_REPLICAS:
        events = streams[("replica", f"replica-{replica}")]
        if any(event.event_type == "epoch.command_committed" for event in events):
            raise ValidationError(
                f"crashed replica-{replica} emitted an epoch command after its confirmed exit"
            )
        if any(
            event.event_type == "epoch.activated"
            and event.payload.get("epoch_number") == epochs.successor.epoch_number
            for event in events
        ):
            raise ValidationError(
                f"crashed replica-{replica} emitted a successor activation"
            )
    for replica in SURVIVING_REPLICAS:
        events = streams[("replica", f"replica-{replica}")]
        commands = [event for event in events if event.event_type == "epoch.command_committed"]
        if not commands:
            raise IncompleteRun(f"replica-{replica} has no committed epoch command")
        if len(commands) != 1:
            raise ValidationError(f"replica-{replica} emitted more than one epoch command")
        command = _parse_command_payload(commands[0].payload)
        if command != expected_command:
            raise ValidationError(
                f"replica-{replica} command differs from canonical epoch input"
            )
        if commands[0].timestamp_ns <= crash_complete_ns:
            raise ValidationError(
                "epoch command must commit after both confirmed crash exits"
            )
        command_times[replica] = commands[0].timestamp_ns

        activations = [
            event
            for event in events
            if event.event_type == "epoch.activated"
            and event.payload.get("epoch_number") == epochs.successor.epoch_number
        ]
        if not activations:
            raise IncompleteRun(f"replica-{replica} has no successor activation")
        if len(activations) != 1:
            raise ValidationError(
                f"replica-{replica} emitted more than one successor activation"
            )
        activation = activations[0]
        _exact_fields(activation.payload, _EPOCH_EVENT_FIELDS, "epoch.activated payload")
        epoch_number = _integer(
            activation.payload["epoch_number"], "activation.epoch_number"
        )
        tree_id = _integer(activation.payload["tree_id"], "activation.tree_id")
        digest = _hash(activation.payload["epoch_digest"], "activation.epoch_digest")
        height = _integer(
            activation.payload["activation_height"],
            "activation.activation_height",
            minimum=1,
        )
        if epoch_number != 1 or digest != epochs.successor.epoch_digest:
            raise ValidationError("activation does not identify canonical successor epoch")
        if tree_id not in {tree.tree_id for tree in epochs.successor.trees}:
            raise ValidationError("activation tree_id is absent from successor epoch")
        if height != expected_command["activation_height"]:
            raise ValidationError("activation height differs from committed command")
        if activation.timestamp_ns < commands[0].timestamp_ns:
            raise ValidationError("activation precedes the committed command")
        activation_times[replica] = activation.timestamp_ns

    observer_command_ns = command_times[analysis.AUTHORITATIVE_OBSERVER]
    observer_activation_ns = activation_times[analysis.AUTHORITATIVE_OBSERVER]
    if max(activation_times.values()) - min(activation_times.values()) > (
        manifest.activation_grace_ns
    ):
        raise ValidationError("survivor activation spread exceeds activation grace")
    return observer_command_ns, observer_activation_ns


def _compressed(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        if not result or result[-1] != value:
            result.append(value)
    return result


def _contains_contiguous(values: Sequence[int], expected: Sequence[int]) -> bool:
    width = len(expected)
    return any(list(values[index : index + width]) == list(expected) for index in range(len(values) - width + 1))


def _commit_observations(
    events: Sequence[analysis.StructuredEvent], replica: int
) -> dict[int, str]:
    observations: dict[int, str] = {}
    hashes: dict[str, int] = {}
    for event in events:
        if event.event_type != "block.committed":
            continue
        payload = event.payload
        _exact_fields(payload, _COMMIT_FIELDS, "block.committed payload")
        designated = payload["designated_observer"]
        if type(designated) is not bool:
            raise ValidationError("designated_observer must be boolean")
        if designated != (replica == analysis.AUTHORITATIVE_OBSERVER):
            raise ValidationError(
                f"replica-{replica} designated-observer flag is inconsistent"
            )
        height = _integer(payload["block_height"], "commit.block_height", minimum=1)
        block_hash = _hash(payload["block_hash"], "commit.block_hash")
        parent = payload["parent_hash"]
        if parent is not None:
            _hash(parent, "commit.parent_hash")
        _integer(payload["transaction_count"], "commit.transaction_count")
        proof = _object(payload["decision_proof"], "commit.decision_proof")
        _exact_fields(proof, _DECISION_FIELDS, "commit.decision_proof")
        _integer(
            proof["epoch_number"],
            "commit.decision_proof.epoch_number",
            maximum=analysis.UINT32_MAX,
        )
        _integer(
            proof["tree_id"],
            "commit.decision_proof.tree_id",
            maximum=analysis.UINT32_MAX,
        )
        _hash(proof["epoch_digest"], "commit.decision_proof.epoch_digest")
        if _hash(proof["block_hash"], "commit.decision_proof.block_hash") != block_hash:
            raise ValidationError("commit decision proof hash mismatch")
        view = payload["view_generation"]
        if view is not None:
            _integer(view, "commit.view_generation")
        _integer(payload["commit_batch_index"], "commit.commit_batch_index")
        previous_hash = observations.get(height)
        if previous_hash is not None and previous_hash != block_hash:
            raise ValidationError(
                f"replica-{replica} has conflicting commits at height {height}"
            )
        previous_height = hashes.get(block_hash)
        if previous_height is not None and previous_height != height:
            raise ValidationError(
                f"replica-{replica} reuses one commit hash at two heights"
            )
        observations[height] = block_hash
        hashes[block_hash] = height
    return observations


def _complete_bucket_counts(
    throughput: analysis.ThroughputAnalysis,
) -> dict[str, int]:
    counts = {"baseline": 0, "degraded": 0, "post": 0}
    for bucket in throughput.buckets:
        if bucket.phase not in counts:
            raise ValidationError(f"unknown throughput phase: {bucket.phase}")
        width_ns = bucket.end_ns - bucket.start_ns
        if width_ns > analysis.BUCKET_WIDTH_NS:
            raise ValidationError("throughput bucket exceeds five seconds")
        if width_ns == analysis.BUCKET_WIDTH_NS:
            counts[bucket.phase] += 1
    return counts


def _maximum_commit_stalls(
    commits: Sequence[analysis.CommitEvent],
    phase_intervals: Sequence[tuple[str, int, int]],
) -> dict[str, int]:
    """Return the maximum no-commit interval in each measured phase.

    Phase boundaries reset the gap calculation: a crash or post-grace boundary
    is treated as a fresh observation start, so one transition interval is
    never charged to two phases.  The phase tail through its exclusive end is
    included, which makes zero-filled terminal buckets auditable.
    """
    result: dict[str, int] = {}
    for phase, start_ns, end_ns in phase_intervals:
        timestamps = [
            commit.timestamp_ns
            for commit in commits
            if start_ns <= commit.timestamp_ns < end_ns
        ]
        if not timestamps:
            raise IncompleteRun(
                f"authoritative observer has no commits in {phase} phase"
            )
        points = [start_ns, *timestamps, end_ns]
        result[phase] = max(
            right_ns - left_ns
            for left_ns, right_ns in zip(points, points[1:])
        )
    return result


def _validate_commits(
    manifest: Manifest,
    epochs: EpochDocument,
    streams: Mapping[tuple[str, str], Sequence[analysis.StructuredEvent]],
    texts: Mapping[tuple[str, str], str],
    crash_ns: int,
    command_ns: int,
    activation_ns: int,
    post_start_ns: int,
) -> tuple[
    analysis.ThroughputAnalysis,
    int,
    Mapping[str, int],
    Mapping[str, int],
]:
    observer_key = ("replica", manifest.authoritative_observer)
    try:
        commits = analysis.parse_commit_events(
            texts[observer_key],
            expected_run_id=manifest.run_id,
            leader_by_configuration=epochs.leader_by_configuration,
            expected_source_instance=next(
                source.source_instance
                for source in manifest.sources
                if source.key == observer_key
            ),
            allow_legacy_prefix=False,
        )
    except analysis.AnalysisError as exc:
        raise ValidationError(f"authoritative commit stream is invalid: {exc}") from exc
    boundaries = analysis.PhaseBoundaries(
        baseline_start_ns=manifest.baseline_start_ns,
        crash_ns=crash_ns,
        activation_ns=post_start_ns,
        end_ns=manifest.end_ns,
    )
    try:
        throughput = analysis.analyze_throughput(commits, boundaries)
    except analysis.AnalysisError as exc:
        raise ValidationError(f"throughput analysis failed: {exc}") from exc
    complete_bucket_counts = _complete_bucket_counts(throughput)
    if complete_bucket_counts["baseline"] < manifest.baseline_bucket_count:
        raise IncompleteRun(
            "baseline phase has "
            f"{complete_bucket_counts['baseline']} complete raw buckets; "
            f"the frozen profile requires at least {manifest.baseline_bucket_count}"
        )
    if complete_bucket_counts["post"] < manifest.post_bucket_count:
        raise IncompleteRun(
            "post phase has "
            f"{complete_bucket_counts['post']} complete raw buckets; "
            f"the frozen profile requires at least {manifest.post_bucket_count}"
        )
    maximum_stalls = _maximum_commit_stalls(
        commits,
        (
            ("baseline", manifest.baseline_start_ns, crash_ns),
            ("degraded", crash_ns, post_start_ns),
            ("post", post_start_ns, manifest.end_ns),
        ),
    )
    for phase, stall_ns in maximum_stalls.items():
        limit_ns = (
            manifest.degraded_maximum_stall_ns
            if phase == "degraded"
            else manifest.maximum_stall_ns
        )
        if stall_ns > limit_ns:
            raise ValidationError(
                f"{phase} authoritative commit stall is "
                f"{stall_ns / 1_000_000_000:g}s; frozen maximum is "
                f"{limit_ns / 1_000_000_000:g}s"
            )
    commits_by_height = {commit.height: commit for commit in commits}
    command = epochs.successor.command
    assert command is not None
    command_height = int(command["command_block_height"])
    command_commit = commits_by_height.get(command_height)
    if command_commit is None:
        raise IncompleteRun(
            f"authoritative observer lacks command block height {command_height}"
        )
    if command_commit.block_hash != command["command_block_hash"]:
        raise ValidationError("committed command hash does not match its consensus block")
    if command_commit.timestamp_ns > command_ns:
        raise ValidationError("epoch command event precedes its committed consensus block")
    activation_height = int(command["activation_height"])
    activation_commit = commits_by_height.get(activation_height)
    if activation_commit is None:
        raise IncompleteRun(
            f"authoritative observer lacks activation height {activation_height}"
        )
    if activation_commit.timestamp_ns > activation_ns:
        raise ValidationError("epoch activation event precedes its activating commit")
    for commit in commits:
        if not manifest.baseline_start_ns <= commit.timestamp_ns < manifest.end_ns:
            continue
        expected_epoch = 0 if commit.height <= activation_height else 1
        if commit.epoch_number != expected_epoch:
            raise ValidationError(
                f"commit height {commit.height} uses epoch {commit.epoch_number}; "
                f"expected epoch {expected_epoch} at the exact activation height"
            )
    pre_crash = [
        commit
        for commit in commits
        if manifest.baseline_start_ns <= commit.timestamp_ns < crash_ns
    ]
    if any(commit.epoch_number != 0 for commit in pre_crash):
        raise ValidationError("pre-crash commits must use epoch 0")
    initial_leaders = _compressed(commit.leader_replica for commit in pre_crash)
    if len(initial_leaders) < 7 or initial_leaders[-7:] != list(MEMBERSHIP):
        raise ValidationError(
            "crash must follow a complete final epoch-0 root cycle 0..6 ending at root 6"
        )

    post = [
        commit
        for commit in commits
        if post_start_ns <= commit.timestamp_ns < manifest.end_ns
    ]
    if any(commit.epoch_number != 1 for commit in post):
        raise ValidationError("post-grace commits must use only successor epoch 1")
    post_leaders = _compressed(commit.leader_replica for commit in post)
    if not _contains_contiguous(post_leaders, SUCCESSOR_ROOTS):
        raise IncompleteRun(
            "post-grace commits do not contain one complete successor root cycle 2..6"
        )

    by_replica = {
        replica: _commit_observations(
            streams[("replica", f"replica-{replica}")], replica
        )
        for replica in SURVIVING_REPLICAS
    }
    authoritative_in_window = {
        commit.height: commit.block_hash
        for commit in commits
        if manifest.baseline_start_ns <= commit.timestamp_ns < manifest.end_ns
    }
    if not authoritative_in_window:
        raise IncompleteRun("authoritative observer has no measurement commits")
    for height, block_hash in authoritative_in_window.items():
        for replica, observations in by_replica.items():
            if height not in observations:
                raise IncompleteRun(
                    f"replica-{replica} is missing authoritative height {height}"
                )
            if observations[height] != block_hash:
                raise ValidationError(
                    f"survivor commit disagreement at height {height}"
                )
    common = set.intersection(*(set(values) for values in by_replica.values()))
    observed_heights = set.union(*(set(values) for values in by_replica.values()))
    for height in observed_heights:
        observed_hashes = {
            observations[height]
            for observations in by_replica.values()
            if height in observations
        }
        observer_count = sum(
            height in observations for observations in by_replica.values()
        )
        if observer_count >= 2 and len(observed_hashes) != 1:
            raise ValidationError(f"survivor commit disagreement at height {height}")
    for height in common:
        if len({values[height] for values in by_replica.values()}) != 1:
            raise ValidationError(f"survivor commit disagreement at height {height}")

    for bucket in throughput.buckets:
        if sum(bucket.leader_transactions) != bucket.transaction_count:
            raise ValidationError("leader transaction columns do not conserve aggregate")
        if sum(bucket.leader_tps) != bucket.aggregate_tps:
            raise ValidationError("leader TPS columns do not conserve aggregate")
    if throughput.medians.post_tps <= throughput.medians.degraded_tps:
        raise ValidationError("post median throughput must exceed degraded median")
    if throughput.medians.baseline_tps <= 0:
        raise ValidationError("baseline median must be positive to report recovery ratio")
    return throughput, len(common), complete_bucket_counts, maximum_stalls


def _phase_for_timestamp(
    timestamp_ns: int, *, baseline_ns: int, crash_ns: int, post_ns: int
) -> str:
    if timestamp_ns < crash_ns:
        return "baseline"
    if timestamp_ns < post_ns:
        return "degraded"
    return "post"


def _validate_reputation(
    manifest: Manifest,
    streams: Mapping[tuple[str, str], Sequence[analysis.StructuredEvent]],
    crash_ns: int,
    command_ns: int,
    post_start_ns: int,
) -> tuple[tuple[ReputationPoint, ...], tuple[int, int, int, int, int, int, int]]:
    manager_events = streams[("adaptation_manager", manifest.manager_source_id)]
    if any(event.event_type == "process.forced_crash_requested" for event in manager_events):
        raise ValidationError("manager stream contains crash ground truth")
    reputation_events = [
        event
        for event in manager_events
        if event.event_type == "reputation.evidence_applied"
    ]
    if not reputation_events:
        raise IncompleteRun("manager emitted no reputation.evidence_applied events")

    scores = [0] * 7
    crash_boundaries = {
        marker.replica_id: marker.confirmed_ns for marker in manifest.crash_markers
    }
    score_at_crash: dict[int, int | None] = {
        replica: None for replica in CRASHED_REPLICAS
    }
    points: list[ReputationPoint] = []
    seen_targets: set[int] = set()
    observations: dict[str, tuple[str, int, int]] = {}
    previous_ingestion = 0
    previous_cutoff = 0
    baseline_snapshot_taken = False
    command_snapshot_taken = False
    score_at_command = [0] * 7
    baseline_on_time_targets: set[int] = set()
    precommand_timeout_ids: dict[int, dict[int, set[str]]] = {
        replica: {} for replica in CRASHED_REPLICAS
    }
    for event in reputation_events:
        if not 0 < event.timestamp_ns < manifest.end_ns:
            raise ValidationError("reputation update lies outside the completed run")
        if not baseline_snapshot_taken and event.timestamp_ns >= manifest.baseline_start_ns:
            points.extend(
                ReputationPoint(
                    timestamp_ns=manifest.baseline_start_ns,
                    replica_id=replica,
                    score=scores[replica],
                    source_sequence=None,
                    evidence_outcome="initial",
                    delta=0,
                )
                for replica in MEMBERSHIP
            )
            baseline_snapshot_taken = True
        for failed in CRASHED_REPLICAS:
            if (
                score_at_crash[failed] is None
                and event.timestamp_ns >= crash_boundaries[failed]
            ):
                score_at_crash[failed] = scores[failed]
        if not command_snapshot_taken and event.timestamp_ns >= command_ns:
            score_at_command = scores.copy()
            command_snapshot_taken = True
        payload = event.payload
        _exact_fields(payload, _REPUTATION_FIELDS, "reputation payload")
        cutoff = _integer(payload["evidence_cutoff"], "evidence_cutoff", minimum=1)
        ingestion = _integer(
            payload["ingestion_sequence"], "ingestion_sequence", minimum=1
        )
        if ingestion <= previous_ingestion:
            raise ValidationError("reputation ingestion_sequence must increase")
        if cutoff < previous_cutoff or ingestion > cutoff:
            raise ValidationError("reputation evidence cutoff is invalid")
        previous_ingestion = ingestion
        previous_cutoff = cutoff
        observation = _hash(payload["observation_id"], "observation_id")
        reporter = _integer(
            payload["reporter_id"], "reporter_id", maximum=analysis.UINT32_MAX
        )
        target = _integer(
            payload["target_id"], "target_id", maximum=analysis.UINT32_MAX
        )
        if reporter not in MEMBERSHIP or target not in MEMBERSHIP or reporter == target:
            raise ValidationError("reputation reporter/target identity is invalid")
        evidence_outcome = payload["evidence_outcome"]
        reputation_outcome = payload["reputation_outcome"]
        if evidence_outcome == "timeout":
            expected_reputation, expected_delta = "timeout", -1
        elif evidence_outcome in ("on_time", "late"):
            expected_reputation, expected_delta = "response", 1
        else:
            raise ValidationError("unsupported reputation evidence_outcome")
        previous_observation = observations.get(observation)
        if previous_observation is None:
            if evidence_outcome == "late":
                raise ValidationError(
                    "standalone late evidence has no preceding matching timeout"
                )
            observations[observation] = (evidence_outcome, reporter, target)
        elif (
            previous_observation == ("timeout", reporter, target)
            and evidence_outcome == "late"
        ):
            observations[observation] = ("late", reporter, target)
        else:
            raise ValidationError(
                "observation_id duplicate is not one ordered timeout-to-late transition"
            )
        if reputation_outcome != expected_reputation:
            raise ValidationError("evidence-to-reputation outcome mapping is invalid")
        delta = _integer(
            payload["delta"], "delta", minimum=-1, maximum=1
        )
        if delta != expected_delta:
            raise ValidationError("evidence-to-reputation delta mapping is invalid")
        resulting = _integer(
            payload["resulting_score"],
            "resulting_score",
            minimum=-(1 << 31),
            maximum=(1 << 31) - 1,
        )
        if resulting != scores[target] + delta:
            raise ValidationError("resulting_score does not continue full run trajectory")
        scores[target] = resulting
        seen_targets.add(target)
        if (
            manifest.baseline_start_ns <= event.timestamp_ns < crash_ns
            and evidence_outcome == "on_time"
        ):
            baseline_on_time_targets.add(target)
        if (
            target in CRASHED_REPLICAS
            and evidence_outcome == "timeout"
            and crash_boundaries[target] <= event.timestamp_ns < command_ns
        ):
            reporter_ids = precommand_timeout_ids[target].setdefault(
                reporter, set()
            )
            reporter_ids.add(observation)
        elif target in CRASHED_REPLICAS and evidence_outcome == "late":
            reporter_ids = precommand_timeout_ids[target].get(reporter)
            if reporter_ids is not None:
                reporter_ids.discard(observation)
        if event.timestamp_ns >= manifest.baseline_start_ns:
            points.append(
                ReputationPoint(
                    timestamp_ns=event.timestamp_ns,
                    replica_id=target,
                    score=resulting,
                    source_sequence=event.source_sequence,
                    evidence_outcome=evidence_outcome,
                    delta=delta,
                )
            )
    if not baseline_snapshot_taken:
        points.extend(
            ReputationPoint(
                timestamp_ns=manifest.baseline_start_ns,
                replica_id=replica,
                score=scores[replica],
                source_sequence=None,
                evidence_outcome="initial",
                delta=0,
            )
            for replica in MEMBERSHIP
        )
    for failed in CRASHED_REPLICAS:
        if score_at_crash[failed] is None:
            score_at_crash[failed] = scores[failed]
    if not command_snapshot_taken:
        score_at_command = scores.copy()
    if seen_targets != set(MEMBERSHIP):
        missing = sorted(set(MEMBERSHIP) - seen_targets)
        raise IncompleteRun(
            "manager reputation trajectory is missing replicas: "
            + ", ".join(map(str, missing))
        )
    if baseline_on_time_targets != set(MEMBERSHIP):
        missing = sorted(set(MEMBERSHIP) - baseline_on_time_targets)
        raise IncompleteRun(
            "responsive baseline lacks on_time evidence for replicas: "
            + ", ".join(map(str, missing))
        )
    if not all(
        scores[failed] < int(score_at_crash[failed])
        for failed in CRASHED_REPLICAS
    ):
        raise ValidationError("crashed replicas did not lose reputation after crash")
    for failed in CRASHED_REPLICAS:
        timeout_ids = precommand_timeout_ids[failed]
        qualifying_reporters = [
            reporter
            for reporter, observation_ids in timeout_ids.items()
            if len(observation_ids) >= 2
        ]
        if len(qualifying_reporters) < FAULT_THRESHOLD + 1:
            raise IncompleteRun(
                f"replica {failed} requires timeout evidence from at least "
                f"{FAULT_THRESHOLD + 1} distinct reporters with at least 2 "
                "observations each before the epoch command"
            )
        net_drop = int(score_at_crash[failed]) - score_at_command[failed]
        if net_drop < 6:
            raise ValidationError(
                f"replica {failed} net reputation drop before command is "
                f"{net_drop}, expected at least 6 after late compensation"
            )
    if max(score_at_command[replica] for replica in CRASHED_REPLICAS) >= min(
        score_at_command[replica] for replica in SURVIVING_REPLICAS
    ):
        raise ValidationError(
            "replicas 0 and 1 were not the lowest-ranked pair before the epoch command"
        )
    if max(scores[replica] for replica in CRASHED_REPLICAS) >= min(
        scores[replica] for replica in SURVIVING_REPLICAS
    ):
        raise ValidationError("replicas 0 and 1 are not the deterministic lowest pair")
    return tuple(points), tuple(scores)  # type: ignore[return-value]


def evaluate(manifest_path: Path, epochs_path: Path) -> Evaluation:
    manifest = load_manifest(manifest_path)
    epochs = load_epochs(epochs_path)
    command = epochs.successor.command
    assert command is not None
    if command["activation_delay_blocks"] != manifest.runtime[
        "activation_delay_blocks"
    ]:
        raise ValidationError(
            "committed activation delay differs from manifest.runtime"
        )
    if [tree.leader for tree in epochs.initial.trees] != manifest.runtime[
        "epoch0_roots"
    ]:
        raise ValidationError("epoch-0 roots differ from manifest.runtime")
    if [tree.leader for tree in epochs.successor.trees] != manifest.runtime[
        "successor_roots"
    ]:
        raise ValidationError("successor roots differ from manifest.runtime")
    streams, texts = _read_source_events(manifest)
    _validate_process_lifecycle(manifest, streams)
    crash_markers = _validate_crash_markers(manifest, streams)
    crash_ns = min(crash_markers)
    crash_complete_ns = max(marker.confirmed_ns for marker in manifest.crash_markers)
    command_ns, activation_ns = _validate_command_and_activation(
        manifest, epochs, streams, crash_complete_ns
    )
    if activation_ns > analysis.UINT64_MAX - manifest.activation_grace_ns:
        raise ValidationError("activation grace overflows the monotonic clock")
    post_start_ns = activation_ns + manifest.activation_grace_ns
    if not crash_ns < command_ns <= activation_ns < post_start_ns < manifest.end_ns:
        raise ValidationError(
            "boundaries must satisfy crash < command <= activation < post < end"
        )
    (
        throughput,
        common_heights,
        complete_bucket_counts,
        maximum_stalls,
    ) = _validate_commits(
        manifest,
        epochs,
        streams,
        texts,
        crash_ns,
        command_ns,
        activation_ns,
        post_start_ns,
    )
    reputation, final_scores = _validate_reputation(
        manifest, streams, crash_ns, command_ns, post_start_ns
    )
    return Evaluation(
        manifest=manifest,
        epochs=epochs,
        crash_markers_ns=crash_markers,
        crash_ns=crash_ns,
        command_ns=command_ns,
        activation_ns=activation_ns,
        post_start_ns=post_start_ns,
        throughput=throughput,
        reputation=reputation,
        final_scores=final_scores,
        common_commit_heights=common_heights,
        complete_bucket_counts=complete_bucket_counts,
        maximum_stall_ns_by_phase=maximum_stalls,
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


def _exclusive_write(path: Path, text: str) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError as exc:
        raise ValidationError(
            f"refusing to overwrite canonical output: {path.name}"
        ) from exc


def _csv_text(rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _pass_record(
    evaluation: Evaluation, artifacts: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    medians = evaluation.throughput.medians
    ratio = medians.post_tps / medians.baseline_tps
    return {
        "schema_version": SCHEMA_VERSION,
        "scenario": SCENARIO,
        "run_id": evaluation.manifest.run_id,
        "kauri_revision": evaluation.manifest.kauri_revision,
        "profile_identity": evaluation.manifest.profile_identity,
        "run_complete": True,
        "verdict": "PASS",
        "reason": None,
        "authoritative_observer": evaluation.manifest.authoritative_observer,
        "boundaries": {
            "baseline_start_ns": evaluation.manifest.baseline_start_ns,
            "crash_ns": evaluation.crash_ns,
            "command_ns": evaluation.command_ns,
            "activation_ns": evaluation.activation_ns,
            "post_start_ns": evaluation.post_start_ns,
            "end_ns": evaluation.manifest.end_ns,
        },
        "markers": {
            "crash_replicas": list(CRASHED_REPLICAS),
            "crash_marker_ns": list(evaluation.crash_markers_ns),
            "crash_confirmed_ns": [
                marker.confirmed_ns for marker in evaluation.manifest.crash_markers
            ],
        },
        "metrics": {
            "baseline_median_tps": medians.baseline_tps,
            "degraded_median_tps": medians.degraded_tps,
            "post_median_tps": medians.post_tps,
            "recovery_ratio": ratio,
            "common_survivor_commit_heights": evaluation.common_commit_heights,
            "complete_bucket_counts": dict(evaluation.complete_bucket_counts),
            "maximum_commit_stall_seconds": {
                phase: stall_ns / 1_000_000_000
                for phase, stall_ns in evaluation.maximum_stall_ns_by_phase.items()
            },
            "final_reputation_scores": list(evaluation.final_scores),
        },
        "artifacts": dict(artifacts),
    }


def _failure_record(
    *, verdict: str, reason: str, manifest_path: Path, epochs_path: Path
) -> dict[str, Any]:
    revision: str | None = None
    profile_identity: str | None = None
    run_complete: bool | None = None
    run_id: str | None = None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            if isinstance(raw.get("run_id"), str):
                run_id = raw["run_id"]
            if isinstance(raw.get("kauri_revision"), str):
                revision = raw["kauri_revision"]
            profile = raw.get("profile")
            if isinstance(profile, dict) and isinstance(profile.get("identity"), str):
                profile_identity = profile["identity"]
            completion = raw.get("run_completion")
            if isinstance(completion, dict) and type(completion.get("complete")) is bool:
                run_complete = completion["complete"]
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "schema_version": SCHEMA_VERSION,
        "scenario": SCENARIO,
        "run_id": run_id,
        "kauri_revision": revision,
        "profile_identity": profile_identity,
        "run_complete": run_complete,
        "verdict": verdict,
        "reason": reason,
        "inputs": {
            "manifest": str(manifest_path.resolve()),
            "epochs": str(epochs_path.resolve()),
        },
        "artifacts": {},
    }


def _validate_claimed_run(
    manifest_path: Path, epochs_path: Path, output_directory: Path
) -> Mapping[str, Any]:
    verdict_path = output_directory / "validation.json"
    try:
        evaluation = evaluate(manifest_path, epochs_path)
    except IncompleteRun as exc:
        record = _failure_record(
            verdict="INCOMPLETE",
            reason=str(exc),
            manifest_path=manifest_path,
            epochs_path=epochs_path,
        )
        _exclusive_write(verdict_path, _canonical_json(record))
        return record
    except (ValidationError, analysis.AnalysisError) as exc:
        record = _failure_record(
            verdict="FAIL",
            reason=str(exc),
            manifest_path=manifest_path,
            epochs_path=epochs_path,
        )
        _exclusive_write(verdict_path, _canonical_json(record))
        return record

    throughput_rows = [bucket.as_row() for bucket in evaluation.throughput.buckets]
    throughput_fields = list(throughput_rows[0])
    reputation_rows = [
        {
            "timestamp_ns": point.timestamp_ns,
            "elapsed_seconds": (
                point.timestamp_ns - evaluation.manifest.baseline_start_ns
            )
            / 1_000_000_000,
            "phase": _phase_for_timestamp(
                point.timestamp_ns,
                baseline_ns=evaluation.manifest.baseline_start_ns,
                crash_ns=evaluation.crash_ns,
                post_ns=evaluation.post_start_ns,
            ),
            "replica_id": point.replica_id,
            "score": point.score,
            "source_sequence": ""
            if point.source_sequence is None
            else point.source_sequence,
            "evidence_outcome": point.evidence_outcome,
            "delta": point.delta,
        }
        for point in evaluation.reputation
    ]
    manifest_output = output_directory / "manifest.json"
    profile_output = output_directory / "profile.json"
    epochs_output = output_directory / "epochs.json"
    throughput_output = output_directory / "throughput.csv"
    reputation_output = output_directory / "reputation.csv"
    _exclusive_write(manifest_output, _canonical_json(evaluation.manifest.raw))
    profile_text = evaluation.manifest.profile_bytes.decode("utf-8")
    _exclusive_write(profile_output, profile_text)
    _exclusive_write(epochs_output, _canonical_json(evaluation.epochs.raw))
    _exclusive_write(
        throughput_output, _csv_text(throughput_rows, throughput_fields)
    )
    _exclusive_write(
        reputation_output,
        _csv_text(
            reputation_rows,
            (
                "timestamp_ns",
                "elapsed_seconds",
                "phase",
                "replica_id",
                "score",
                "source_sequence",
                "evidence_outcome",
                "delta",
            ),
        ),
    )
    artifact_paths = {
        "manifest": manifest_output,
        "profile": profile_output,
        "epochs": epochs_output,
        "throughput": throughput_output,
        "reputation": reputation_output,
    }
    artifacts = {
        name: {
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for name, path in artifact_paths.items()
    }
    record = _pass_record(evaluation, artifacts)
    _exclusive_write(verdict_path, _canonical_json(record))
    return record


def _existing_canonical_outputs(output_directory: Path) -> list[str]:
    return [
        name
        for name in CANONICAL_OUTPUT_NAMES
        if (output_directory / name).exists()
    ]


def validate_run(
    manifest_path: Path, epochs_path: Path, output_directory: Path
) -> Mapping[str, Any]:
    """Atomically claim and validate one immutable output directory."""
    output_directory.mkdir(parents=True, exist_ok=True)
    existing = _existing_canonical_outputs(output_directory)
    if existing:
        raise ValidationError(
            "canonical output already exists; preserve the run and use a new "
            "output directory: " + ", ".join(existing)
        )
    claim_path = output_directory / VALIDATION_CLAIM
    try:
        with claim_path.open("x", encoding="utf-8") as claim:
            claim.write(f"pid={os.getpid()}\n")
            claim.flush()
            os.fsync(claim.fileno())
    except FileExistsError as exc:
        raise ValidationError(
            "validation output is already claimed; preserve it or use a new directory"
        ) from exc
    try:
        existing = _existing_canonical_outputs(output_directory)
        if existing:
            raise ValidationError(
                "canonical output appeared while claiming the directory: "
                + ", ".join(existing)
            )
        return _validate_claimed_run(
            manifest_path, epochs_path, output_directory
        )
    finally:
        try:
            claim_path.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--epochs", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        record = validate_run(
            arguments.manifest, arguments.epochs, arguments.output_dir
        )
    except ValidationError as exc:
        print(f"validator error: {exc}", file=sys.stderr)
        return 2
    print(f"{record['verdict']}: {record.get('reason') or 'all frozen checks passed'}")
    return 0 if record["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
