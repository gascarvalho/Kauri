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
import run as campaign_runner


SCHEMA_VERSION = 1
SCENARIO = "n7-crash-recovery"
MEMBERSHIP = tuple(range(7))
FAULT_THRESHOLD = 2
QUORUM = 5
MANAGER_CONVERGENCE_DEADLINE_S = 120
CRASHED_REPLICAS = (0, 1)
SURVIVING_REPLICAS = (2, 3, 4, 5, 6)
SUCCESSOR_ROOTS = SURVIVING_REPLICAS
FROZEN_PROFILE_ID = "n7-f2-q5-crash-recovery-recurring-v3"
FROZEN_PROFILE_SHA256 = (
    "ddfb037c707ebc699138e446f365aa51f19624ee997635784249cc464c5eccef"
)
PAIRED_ADAPTIVE_PROFILE_ID = (
    "n7-f2-q5-crash-recovery-matched-adaptive-v1"
)
PAIRED_ADAPTIVE_PROFILE_SHA256 = (
    "faece247365cf0cac5a771abfe67cc7b3e29f04c1ae53befc03e86ebf95c2777"
)
PAIRED_CONTROL_PROFILE_ID = (
    "n7-f2-q5-crash-recovery-containment-control-v1"
)
PAIRED_CONTROL_PROFILE_SHA256 = (
    "70ae0386338f8d66ff9d5489baa5cde403e576a9e32455090810fe5bb5906b03"
)
LEGACY_FROZEN_PROFILE_ID = "n7-f2-q5-crash-recovery-v2"
LEGACY_FROZEN_PROFILE_SHA256 = (
    "768c33418937f9b738c607b523ad847a7cb38220c95a499e82823ac41aa1e038"
)
RECURRING_PROFILE_IDS = frozenset(
    {
        FROZEN_PROFILE_ID,
        PAIRED_ADAPTIVE_PROFILE_ID,
        PAIRED_CONTROL_PROFILE_ID,
    }
)
PAIRED_PROFILE_IDS = frozenset(
    {PAIRED_ADAPTIVE_PROFILE_ID, PAIRED_CONTROL_PROFILE_ID}
)
MAXIMUM_PREDECESSOR_RESIDENCY_MS = 3_600_000
EPOCH_CHANGE_PAYLOAD_DOMAIN = b"kauri-epoch-change-payload-v1"
MEMBERSHIP_DOMAIN = b"kauri-membership-v1"
PLACEMENT_POLICY_VERSION = "adaptive-v2-performance-optimization-v1"
RESPONSIVENESS_ATTEMPT_WINDOW = 32
RESPONSIVENESS_MINIMUM_ATTEMPTS = 2
RESPONSIVENESS_RATE_PPM_SCALE = 1_000_000
MINIMUM_RESPONSE_RATE_PPM = 750_000
MAXIMUM_TIMEOUT_RATE_PPM = 250_000
TRAILING_TIMEOUT_STREAK = 2
LATENCY_PERCENTILE_BASIS_POINTS = 5_000
MANAGER_LIMITS = {
    "maximum_members": 7,
    "readiness_wire_maximum_payload_bytes": 256,
    "lifecycle_wire_maximum_payload_bytes": 512,
    "evidence_wire_maximum_payload_bytes": 4096,
    "evidence_wire_maximum_observations": 8,
    "evidence_wire_maximum_signers_per_observation": 7,
    "proposal_maximum_exact_records": 8192,
    "proposal_maximum_retired_configurations": 16,
    "evidence_maximum_accepted_records": 131072,
    "evidence_maximum_rejected_records": 131072,
    "reputation_maximum_audit_updates": 131072,
    "quarantine_maximum_records": 1024,
    "quarantine_maximum_canonical_bytes": 256 * 1024,
    "quarantine_maximum_reporter_queues": 7,
    "quarantine_maximum_signer_entries": 8192,
    "quarantine_maximum_deduplication_entries": 1024,
    "quarantine_maximum_lifecycle_sources": 7,
    "quarantine_maximum_records_per_reporter": 128,
    "accounting_maximum_records": 1024,
    "accounting_maximum_canonical_bytes": 256 * 1024,
    "accounting_maximum_signer_entries": 8192,
    "maximum_pending_lifecycle_facts_per_source": 64,
}
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
        "minimum_post_activation_grace_ns",
        "baseline_start_ns",
        "end_ns",
        "sources",
        "crash_markers",
        "crash_configuration_boundary",
        "runtime",
        "runtime_artifacts",
    }
)
_RECURRING_MANIFEST_FIELDS = frozenset(
    {*_MANIFEST_FIELDS, "transition_requests", "throughput_windows"}
)
_PAIRED_MANIFEST_FIELDS = frozenset(
    {*_RECURRING_MANIFEST_FIELDS, "pair_id", "pair_arm"}
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
        "minimum_post_activation_grace_s",
        "maximum_activation_to_successor_s",
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
_RECURRING_FROZEN_PROFILE_FIELDS = frozenset(
    {
        *(_FROZEN_PROFILE_FIELDS - {
            "successor_epoch",
            "successor_roots",
            "successor_wait_exempt",
            "baseline_bucket_count",
            "post_bucket_count",
        }),
        "transition_requests",
        "throughput_windows",
        "minimum_containment_to_degraded_ratio",
        "minimum_optimized_to_containment_ratio",
    }
)
_PAIRED_ADAPTIVE_FROZEN_PROFILE_FIELDS = frozenset(
    {*_RECURRING_FROZEN_PROFILE_FIELDS, "final_measurement_delay_ms"}
)
_PAIRED_CONTROL_FROZEN_PROFILE_FIELDS = frozenset(
    {
        *(
            _RECURRING_FROZEN_PROFILE_FIELDS
            - {"minimum_optimized_to_containment_ratio"}
        ),
        "final_measurement_delay_ms",
        "minimum_control_late_to_containment_ratio",
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
_RECURRING_MANAGER_FIELDS = frozenset(
    {*_MANAGER_FIELDS, "transition_artifact_ids"}
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
_CRASH_CONFIGURATION_BOUNDARY_FIELDS = frozenset(
    {
        "epoch_number",
        "tree_id",
        "root_replica",
        "epoch_digest",
        "context_generation",
        "replica_evidence",
    }
)
_CRASH_CONFIGURATION_EVIDENCE_FIELDS = frozenset(
    {
        "source_id",
        "source_sequence",
        "source_monotonic_ns",
    }
)
_CONFIGURATION_ACTIVE_FIELDS = frozenset(
    {
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
_MANAGER_CONVERGENCE_PAYLOAD_FIELDS = frozenset(
    {
        "replica_id",
        "delivery_attempt",
        "disposition",
        "identity",
        "accepted_commit_count",
        "accepted_activation_count",
        "required_activation_count",
        "canonical_payload_digest",
        "failure_reason",
    }
)
_MANAGER_CONVERGENCE_IDENTITY_FIELDS = frozenset(
    {
        "predecessor_epoch_number",
        "predecessor_epoch_digest",
        "successor_epoch_number",
        "successor_epoch_digest",
        "command_payload_digest",
        "command_block_height",
        "command_block_hash",
        "activation_delay_blocks",
        "activation_height",
    }
)
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
_COMMIT_OBSERVED_FIELDS = frozenset(
    {
        "block_height",
        "block_hash",
        "parent_hash",
        "transaction_count",
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
        "manager_limits",
        "executables",
    }
)
_RECURRING_RUNTIME_FIELDS = frozenset(
    {
        *(_RUNTIME_FIELDS - {"successor_roots", "successor_wait_exempt"}),
        "transition_requests",
        "throughput_windows",
    }
)
_PAIRED_RUNTIME_FIELDS = frozenset(
    {*_RECURRING_RUNTIME_FIELDS, "final_measurement_delay_ms"}
)
_RUNTIME_EXECUTABLE_FIELDS = frozenset(
    {"hotstuff_app", "adaptation_manager"}
)
_RUNTIME_EXECUTABLE_DESCRIPTOR_FIELDS = frozenset({"path", "sha256"})
_MANAGER_LIMIT_FIELDS = frozenset(MANAGER_LIMITS)
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
_MANAGER_LAUNCH_FLAGS = frozenset(
    {
        "--listen",
        "--tls-privkey",
        "--tls-cert",
        "--issuer-id",
        "--issuer-private-key",
        "--activation-delay-blocks",
        "--structured-event-run-id",
        "--structured-event-source-instance",
        "--structured-event-output",
        "--transition-request",
        "--bundle-output",
        "--replica",
        "--convergence-deadline-seconds",
    }
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
        "manager_limits",
        "binary_sha256",
        "tls_certificate_sha256",
        "issuer_public_key_sha256",
        "replica_tls_certificate_sha256",
    }
)
_RECURRING_MANAGER_EFFECTIVE_OPTION_FIELDS = frozenset(
    {*_MANAGER_EFFECTIVE_OPTION_FIELDS, "transition_requests"}
)
_MANAGER_SESSION_TERMINAL_FIELDS = frozenset(
    {
        "cycle_ordinal",
        "policy_intent",
        "outcome",
        "reason",
        "transition_artifact_id",
        "predecessor_epoch_number",
        "predecessor_epoch_digest",
        "successor_epoch_number",
        "successor_epoch_digest",
        "command_payload_digest",
        "winning_activation",
        "evidence_window_activation_generation",
        "baseline_evidence_cutoff",
        "current_evidence_cutoff",
    }
)
_EVIDENCE_SNAPSHOT_FIELDS = frozenset(
    {
        "cycle_ordinal",
        "policy_intent",
        "transition_artifact_id",
        "predecessor_epoch_number",
        "predecessor_epoch_digest",
        "activation_generation",
        "baseline_cutoff",
        "current_cutoff",
        "observations",
        "eligible_ranking",
    }
)
_EVIDENCE_SNAPSHOT_OBSERVATION_FIELDS = frozenset(
    {
        "observation_id",
        "ingestion_sequence",
        "epoch_number",
        "epoch_digest",
        "reporter_id",
        "target_id",
        "outcome",
    }
)
_EVIDENCE_SNAPSHOT_LATENCY_OBSERVATION_FIELDS = frozenset(
    {*_EVIDENCE_SNAPSHOT_OBSERVATION_FIELDS, "latency_ns"}
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
class CrashConfigurationEvidenceSpec:
    source_id: str
    source_sequence: int
    timestamp_ns: int


@dataclass(frozen=True, slots=True)
class CrashConfigurationBoundarySpec:
    epoch_number: int
    tree_id: int
    root_replica: int
    epoch_digest: str
    context_generation: None
    evidence: tuple[CrashConfigurationEvidenceSpec, ...]


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
    pair_id: str | None
    pair_arm: str | None
    profile_path: Path
    profile_bytes: bytes
    authoritative_observer: str
    manager_source_id: str
    baseline_start_ns: int
    end_ns: int
    minimum_post_activation_grace_ns: int
    maximum_activation_to_successor_ns: int
    baseline_bucket_count: int
    post_bucket_count: int
    final_measurement_delay_ns: int
    transition_requests: tuple[Mapping[str, Any], ...]
    throughput_windows: tuple[analysis.PhaseWindow, ...]
    required_bucket_counts: Mapping[str, int]
    minimum_containment_to_degraded_ratio: float | None
    minimum_optimized_to_containment_ratio: float | None
    minimum_control_late_to_containment_ratio: float | None
    maximum_stall_ns: int
    degraded_maximum_stall_ns: int
    runtime: Mapping[str, Any]
    runtime_artifacts: tuple[RuntimeArtifactSpec, ...]
    sources: tuple[SourceSpec, ...]
    crash_markers: tuple[CrashMarkerSpec, ...]
    crash_configuration_boundary: CrashConfigurationBoundarySpec


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
    epochs: tuple[EpochDefinition, ...]
    initial: EpochDefinition
    successor: EpochDefinition
    leader_by_configuration: Mapping[analysis.ConfigurationKey, int]

    @property
    def final(self) -> EpochDefinition:
        return self.epochs[-1]


@dataclass(frozen=True, slots=True)
class ReputationPoint:
    timestamp_ns: int
    replica_id: int
    score: int
    source_sequence: int | None
    evidence_outcome: str
    delta: int


@dataclass(frozen=True, slots=True)
class CommitObservation:
    block_hash: str
    parent_hash: str | None
    transaction_count: int
    commit_batch_index: int
    source_sequence: int
    timestamp_ns: int

    @property
    def shared_identity(self) -> tuple[str, str | None, int, int]:
        return (
            self.block_hash,
            self.parent_hash,
            self.transaction_count,
            self.commit_batch_index,
        )


@dataclass(frozen=True, slots=True)
class Evaluation:
    manifest: Manifest
    epochs: EpochDocument
    crash_markers_ns: tuple[int, int]
    crash_ns: int
    command_ns: int
    activation_ns: int
    minimum_post_start_ns: int
    first_common_successor_ns: int
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
    identity = value.get("profile_id")
    if identity == FROZEN_PROFILE_ID:
        _exact_fields(value, _RECURRING_FROZEN_PROFILE_FIELDS, label)
        if hashlib.sha256(payload).hexdigest() != FROZEN_PROFILE_SHA256:
            raise ValidationError(
                f"{label} differs from the exact frozen recurring v3 settings"
            )
        return value
    if identity == PAIRED_ADAPTIVE_PROFILE_ID:
        _exact_fields(value, _PAIRED_ADAPTIVE_FROZEN_PROFILE_FIELDS, label)
        if (
            hashlib.sha256(payload).hexdigest()
            != PAIRED_ADAPTIVE_PROFILE_SHA256
        ):
            raise ValidationError(
                f"{label} differs from the exact frozen paired adaptive settings"
            )
        return value
    if identity == PAIRED_CONTROL_PROFILE_ID:
        _exact_fields(value, _PAIRED_CONTROL_FROZEN_PROFILE_FIELDS, label)
        if (
            hashlib.sha256(payload).hexdigest()
            != PAIRED_CONTROL_PROFILE_SHA256
        ):
            raise ValidationError(
                f"{label} differs from the exact frozen paired control settings"
            )
        return value
    if identity != LEGACY_FROZEN_PROFILE_ID:
        raise ValidationError(f"{label} has an unknown frozen profile identity")
    _exact_fields(value, _FROZEN_PROFILE_FIELDS, label)
    expected: Mapping[str, Any] = {
        "schema_version": 1,
        "profile_id": LEGACY_FROZEN_PROFILE_ID,
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
        "minimum_post_activation_grace_s": 1,
        "maximum_activation_to_successor_s": 10,
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
    if not _json_values_equal(value, expected) or value.get("frozen") is not True:
        raise ValidationError(f"{label} differs from the exact frozen v2 settings")
    if hashlib.sha256(payload).hexdigest() != LEGACY_FROZEN_PROFILE_SHA256:
        raise ValidationError(f"{label} is not byte-exact frozen v2")
    return value


def _repository_frozen_profile(
    profile_identity: str = FROZEN_PROFILE_ID,
) -> bytes:
    profile_specs = {
        FROZEN_PROFILE_ID: ("profile.json", FROZEN_PROFILE_SHA256),
        PAIRED_ADAPTIVE_PROFILE_ID: (
            "profile-paired-adaptive.json",
            PAIRED_ADAPTIVE_PROFILE_SHA256,
        ),
        PAIRED_CONTROL_PROFILE_ID: (
            "profile-paired-control.json",
            PAIRED_CONTROL_PROFILE_SHA256,
        ),
    }
    try:
        filename, expected_sha256 = profile_specs[profile_identity]
    except KeyError as exc:
        raise ValidationError(
            "repository profile identity is not recurring"
        ) from exc
    path = Path(__file__).resolve().with_name(filename)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read repository frozen profile: {exc}") from exc
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValidationError("repository frozen profile SHA-256 drifted")
    _decode_frozen_profile(payload, "repository frozen profile")
    return payload


def _transition_requests(
    value: Any, label: str
) -> tuple[Mapping[str, Any], ...]:
    requests = _list(value, label)
    if not requests:
        raise ValidationError(f"{label} must not be empty")
    fields = frozenset(
        {
            "policy_intent",
            "evidence_window_rule",
            "transition_artifact_id",
            "bundle_path",
            "evidence_snapshot_path",
            "predecessor_epoch_number",
            "successor_epoch_number",
            "minimum_predecessor_residency_ms",
            "policy_parameters",
        }
    )
    result: list[Mapping[str, Any]] = []
    paths: set[str] = set()
    artifact_ids: set[str] = set()
    expected_predecessor = 0
    for index, item in enumerate(requests):
        request = _object(item, f"{label}[{index}]")
        _exact_fields(request, fields, f"{label}[{index}]")
        predecessor = _integer(
            request["predecessor_epoch_number"],
            f"{label}[{index}].predecessor_epoch_number",
            maximum=analysis.UINT32_MAX,
        )
        successor = _integer(
            request["successor_epoch_number"],
            f"{label}[{index}].successor_epoch_number",
            maximum=analysis.UINT32_MAX,
        )
        if predecessor != expected_predecessor or successor != predecessor + 1:
            raise ValidationError("transition requests must form one contiguous chain")
        expected_predecessor = successor
        residency_ms = _integer(
            request["minimum_predecessor_residency_ms"],
            f"{label}[{index}].minimum_predecessor_residency_ms",
            maximum=MAXIMUM_PREDECESSOR_RESIDENCY_MS,
        )
        if index == 0 and residency_ms != 0:
            raise ValidationError(
                "the initial containment transition residency must be zero"
            )
        intent = _string(
            request["policy_intent"], f"{label}[{index}].policy_intent"
        )
        if intent not in ("fault_containment", "performance_optimization"):
            raise ValidationError("transition request policy intent is unsupported")
        if request["evidence_window_rule"] != (
            "fresh_exact_predecessor_after_common_commit"
        ):
            raise ValidationError("transition request evidence rule is not frozen")
        artifact_id = _string(
            request["transition_artifact_id"],
            f"{label}[{index}].transition_artifact_id",
        )
        if artifact_id in artifact_ids:
            raise ValidationError("transition artifact IDs must be distinct")
        artifact_ids.add(artifact_id)
        for field in ("bundle_path", "evidence_snapshot_path"):
            relative = _string(request[field], f"{label}[{index}].{field}")
            relative_path = Path(relative)
            if (
                relative_path.is_absolute()
                or ".." in relative_path.parts
                or artifact_id not in relative_path.parts
                or relative in paths
            ):
                raise ValidationError("transition artifact paths must be distinct")
            paths.add(relative)
        parameters = _object(
            request["policy_parameters"],
            f"{label}[{index}].policy_parameters",
        )
        if intent == "performance_optimization" and parameters:
            raise ValidationError("optimization policy parameters must be empty")
        if intent == "fault_containment":
            _exact_fields(
                parameters,
                frozenset({"containment_baseline_roots"}),
                f"{label}[{index}].policy_parameters",
            )
            roots = _list(
                parameters["containment_baseline_roots"],
                f"{label}[{index}].containment_baseline_roots",
            )
            if not roots:
                raise ValidationError("containment baseline roots must not be empty")
            pairs: list[tuple[int, int]] = []
            for root_index, root_value in enumerate(roots):
                root = _object(root_value, "containment baseline root")
                _exact_fields(
                    root,
                    frozenset({"tree_id", "replica_id"}),
                    "containment baseline root",
                )
                pairs.append(
                    (
                        _integer(root["tree_id"], "containment tree_id"),
                        _integer(root["replica_id"], "containment replica_id"),
                    )
                )
            if len({tree_id for tree_id, _ in pairs}) != len(pairs) or len(
                {replica_id for _, replica_id in pairs}
            ) != len(pairs):
                raise ValidationError("containment baseline roots must be unique")
        result.append(request)
    return tuple(result)


def _throughput_window_specs(
    value: Any, label: str
) -> tuple[Mapping[str, Any], ...]:
    windows = _list(value, label)
    if len(windows) != 4:
        raise ValidationError(f"{label} must contain four ordered phases")
    final_phase = (
        windows[-1].get("phase")
        if isinstance(windows[-1], dict)
        else None
    )
    if final_phase not in ("optimized", "control_late"):
        raise ValidationError(
            f"{label} final phase must be optimized or control_late"
        )
    phases = ("baseline", "degraded", "containment", final_phase)
    result: list[Mapping[str, Any]] = []
    for index, (item, phase) in enumerate(zip(windows, phases)):
        window = _object(item, f"{label}[{index}]")
        _exact_fields(
            window,
            frozenset({"phase", "epoch_number", "bucket_count"}),
            f"{label}[{index}]",
        )
        if window["phase"] != phase:
            raise ValidationError(f"{label} phases are not canonical")
        _integer(window["epoch_number"], f"{label}[{index}].epoch_number")
        _integer(
            window["bucket_count"],
            f"{label}[{index}].bucket_count",
            minimum=1,
        )
        result.append(window)
    return tuple(result)


def _validate_transition_residencies(
    requests: Sequence[Mapping[str, Any]],
    windows: Sequence[Mapping[str, Any]],
    *,
    bucket_width_ns: int,
    post_activation_grace_ns: int,
) -> None:
    for previous, request in zip(requests, requests[1:]):
        predecessor_epoch = int(request["predecessor_epoch_number"])
        if predecessor_epoch != int(previous["successor_epoch_number"]):
            raise ValidationError("transition residency does not bind its predecessor")
        matching = [
            window
            for window in windows
            if int(window["epoch_number"]) == predecessor_epoch
        ]
        if len(matching) != 1:
            raise ValidationError(
                f"successor epoch {predecessor_epoch} requires one throughput phase"
            )
        required_ns = (
            int(matching[0]["bucket_count"]) * bucket_width_ns
            + post_activation_grace_ns
        )
        declared_ns = int(request["minimum_predecessor_residency_ms"]) * 1_000_000
        if declared_ns < required_ns:
            raise ValidationError(
                f"transition into epoch {request['successor_epoch_number']} does not "
                "preserve its predecessor's complete throughput window and grace"
            )


def _measurement_windows(
    value: Any, specs: Sequence[Mapping[str, Any]]
) -> tuple[analysis.PhaseWindow, ...]:
    windows = _list(value, "manifest.throughput_windows")
    if len(windows) != len(specs):
        raise ValidationError("manifest throughput windows do not match the profile")
    result: list[analysis.PhaseWindow] = []
    previous_end = 0
    for index, (item, spec) in enumerate(zip(windows, specs)):
        window = _object(item, f"manifest.throughput_windows[{index}]")
        _exact_fields(
            window,
            frozenset({"phase", "epoch_number", "start_ns", "end_ns"}),
            f"manifest.throughput_windows[{index}]",
        )
        phase = _string(window["phase"], f"throughput_windows[{index}].phase")
        epoch = _integer(
            window["epoch_number"], f"throughput_windows[{index}].epoch_number"
        )
        start = _integer(window["start_ns"], f"throughput_windows[{index}].start_ns")
        end = _integer(window["end_ns"], f"throughput_windows[{index}].end_ns")
        if phase != spec["phase"] or epoch != spec["epoch_number"]:
            raise ValidationError("manifest throughput window identity differs from profile")
        if start < previous_end or end <= start:
            raise ValidationError("manifest throughput windows overlap or are empty")
        previous_end = end
        result.append(analysis.PhaseWindow(phase, epoch, start, end))
    return tuple(result)


def _expected_runtime(profile: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
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
        "tree_switch_period_blocks": profile["tree_switch_period_blocks"],
        "snapshot_seed": profile["snapshot_seed"],
        "manager_limits": dict(MANAGER_LIMITS),
    }
    if profile["profile_id"] in RECURRING_PROFILE_IDS:
        expected["transition_requests"] = profile["transition_requests"]
        expected["throughput_windows"] = profile["throughput_windows"]
        if profile["profile_id"] in PAIRED_PROFILE_IDS:
            expected["final_measurement_delay_ms"] = profile[
                "final_measurement_delay_ms"
            ]
    else:
        expected["successor_roots"] = list(profile["successor_roots"])
        expected["successor_wait_exempt"] = list(
            profile["successor_wait_exempt"]
        )
    return expected


def _validate_runtime(
    value: Any, profile: Mapping[str, Any]
) -> Mapping[str, Any]:
    runtime = _object(value, "manifest.runtime")
    if profile["profile_id"] in PAIRED_PROFILE_IDS:
        expected_fields = _PAIRED_RUNTIME_FIELDS
    elif profile["profile_id"] in RECURRING_PROFILE_IDS:
        expected_fields = _RECURRING_RUNTIME_FIELDS
    else:
        expected_fields = _RUNTIME_FIELDS
    _exact_fields(runtime, expected_fields, "manifest.runtime")
    manager_limits = _object(
        runtime["manager_limits"], "manifest.runtime.manager_limits"
    )
    _exact_fields(
        manager_limits,
        _MANAGER_LIMIT_FIELDS,
        "manifest.runtime.manager_limits",
    )
    expected = _expected_runtime(profile)
    static_runtime = {
        field: runtime[field] for field in expected_fields if field != "executables"
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


def _validate_manager_argv_flags(
    argv: Sequence[str], *, recurring: bool
) -> None:
    if len(argv) < 3 or (len(argv) - 1) % 2 != 0:
        raise ValidationError("manager launch argv is not an exact flag/value sequence")
    for position in range(1, len(argv), 2):
        flag = argv[position]
        if flag not in _MANAGER_LAUNCH_FLAGS:
            raise ValidationError(
                f"manager launch argv contains unsupported flag: {flag}"
            )
    repeated_flags = {"--transition-request", "--bundle-output", "--replica"}
    singleton_flags = _MANAGER_LAUNCH_FLAGS - repeated_flags
    if any(argv.count(flag) > 1 for flag in singleton_flags):
        raise ValidationError("manager launch argv repeats a singleton flag")
    if recurring and any(argv.count(flag) != 1 for flag in singleton_flags):
        raise ValidationError(
            "recurring manager launch argv omits a canonical singleton flag"
        )


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
            manager_option_fields = (
                _RECURRING_MANAGER_EFFECTIVE_OPTION_FIELDS
                if "transition_requests" in runtime
                else _MANAGER_EFFECTIVE_OPTION_FIELDS
            )
            _exact_fields(
                options,
                manager_option_fields,
                "manager launch effective_options",
            )
            if options["activation_delay_blocks"] != runtime[
                "activation_delay_blocks"
            ] or options["snapshot_seed"] != runtime["snapshot_seed"] or not (
                _json_values_equal(
                    options["manager_limits"], runtime["manager_limits"]
                )
            ):
                raise ValidationError(
                    "manager launch effective_options differ from manifest.runtime"
                )
            if "transition_requests" in runtime and not _json_values_equal(
                options["transition_requests"], runtime["transition_requests"]
            ):
                raise ValidationError(
                    "manager launch transition requests differ from manifest.runtime"
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
            recurring_launch = "transition_requests" in runtime
            _validate_manager_argv_flags(argv, recurring=recurring_launch)
            if recurring_launch:
                if _single_argv_value(argv, "--issuer-id") != str(
                    campaign_runner.ISSUER_ID
                ):
                    raise ValidationError("manager launch binds the wrong issuer ID")
                for structured_flag in (
                    "--listen",
                    "--structured-event-run-id",
                    "--structured-event-source-instance",
                    "--structured-event-output",
                ):
                    _string(
                        _single_argv_value(argv, structured_flag),
                        f"manager launch {structured_flag}",
                    )
            if _single_argv_value(argv, "--activation-delay-blocks") != str(
                runtime["activation_delay_blocks"]
            ):
                raise ValidationError(
                    "manager launch activation delay differs from manifest.runtime"
                )
            if _single_argv_value(
                argv, "--convergence-deadline-seconds"
            ) != str(MANAGER_CONVERGENCE_DEADLINE_S):
                raise ValidationError(
                    "manager launch --convergence-deadline-seconds differs from 120"
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
            if "transition_requests" in runtime:
                request_positions = [
                    position
                    for position, argument in enumerate(argv[:-1])
                    if argument == "--transition-request"
                ]
                bundle_positions = [
                    position
                    for position, argument in enumerate(argv[:-1])
                    if argument == "--bundle-output"
                ]
                expected_requests = runtime["transition_requests"]
                if (
                    len(request_positions) != len(expected_requests)
                    or len(bundle_positions) != len(expected_requests)
                ):
                    raise ValidationError(
                        "manager launch requires one repeated request and bundle output per transition"
                    )
                decoded_requests: list[Any] = []
                for position in request_positions:
                    try:
                        decoded_requests.append(json.loads(argv[position + 1]))
                    except json.JSONDecodeError as exc:
                        raise ValidationError(
                            "manager launch transition request is not JSON"
                        ) from exc
                if not _json_values_equal(decoded_requests, expected_requests):
                    raise ValidationError(
                        "manager launch repeated requests differ from frozen order"
                    )
                bundle_paths = [Path(argv[position + 1]) for position in bundle_positions]
                if len({path.resolve() for path in bundle_paths}) != len(bundle_paths):
                    raise ValidationError("transition artifact paths must be distinct")
                for path, request in zip(bundle_paths, expected_requests):
                    if not path.is_absolute() or not str(path).endswith(
                        request["bundle_path"]
                    ):
                        raise ValidationError(
                            "manager bundle output does not bind its transition request"
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
    base_expected = [
        ("replica_config", replica, f"runtime/replica-{replica}.effective.json")
        for replica in MEMBERSHIP
    ] + [
        ("epoch_input", None, "runtime/epoch-input.json"),
        ("launch_arguments", None, "runtime/launch-arguments.json"),
    ]
    recurring = "transition_requests" in runtime
    if recurring:
        transition_expected = [
            ("transition_requests", None, "runtime/transition-requests.json")
        ]
        for request in runtime["transition_requests"]:
            transition_expected.extend(
                (
                    ("transition_bundle", None, request["bundle_path"]),
                    (
                        "evidence_snapshot",
                        None,
                        request["evidence_snapshot_path"],
                    ),
                )
            )
        expected = [*base_expected, *transition_expected]
    else:
        expected = base_expected
    if len(entries) != len(expected):
        raise IncompleteRun(
            "runtime_artifacts do not contain the complete frozen artifact set"
        )
    base = manifest_path.resolve().parent
    artifacts: list[RuntimeArtifactSpec] = []
    identities: set[tuple[str, int | None, str]] = set()
    relative_paths: set[str] = set()
    for index, item in enumerate(entries):
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
        identity = (kind, replica_id, relative)
        if identity not in set(expected):
            raise ValidationError("runtime artifact identity is not canonical")
        if identity in identities or relative in relative_paths:
            if recurring:
                raise ValidationError("transition artifact paths must be distinct")
            raise ValidationError("runtime artifact identities or ordering are not canonical")
        identities.add(identity)
        relative_paths.add(relative)
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

    if identities != set(expected):
        raise IncompleteRun("runtime_artifacts omit a frozen artifact identity")
    if not recurring and [
        (artifact.kind, artifact.replica_id, artifact.relative_path)
        for artifact in artifacts
    ] != expected:
        raise ValidationError("runtime artifact identities or ordering are not canonical")

    artifacts_by_path = {
        artifact.relative_path: artifact for artifact in artifacts
    }

    for replica in MEMBERSHIP:
        artifact = artifacts_by_path[f"runtime/replica-{replica}.effective.json"]
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

    epoch_input_artifact = artifacts_by_path["runtime/epoch-input.json"]
    epoch_input = _load_json_bytes(epoch_input_artifact.payload, "initial epoch input")
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

    launch_artifact = artifacts_by_path["runtime/launch-arguments.json"]
    launch = _load_json_bytes(launch_artifact.payload, "launch arguments")
    _validate_launch_arguments(launch, runtime)
    if recurring:
        requests_artifact = artifacts_by_path["runtime/transition-requests.json"]
        requests_document = _load_json_bytes(
            requests_artifact.payload, "transition requests"
        )
        _exact_fields(
            requests_document,
            frozenset({"schema_version", "requests"}),
            "transition requests",
        )
        if (
            _integer(
                requests_document["schema_version"],
                "transition requests schema_version",
            )
            != SCHEMA_VERSION
            or not _json_values_equal(
                requests_document["requests"], runtime["transition_requests"]
            )
        ):
            raise ValidationError(
                "transition request artifact differs from manifest.runtime"
            )
    return tuple(artifacts)


def load_manifest(path: Path) -> Manifest:
    raw = _load_json(path, "manifest")
    profile_header = _object(raw.get("profile"), "manifest.profile")
    profile_identity = _string(
        profile_header.get("identity"), "manifest.profile.identity"
    )
    recurring = profile_identity in RECURRING_PROFILE_IDS
    paired = profile_identity in PAIRED_PROFILE_IDS
    if not recurring and profile_identity != LEGACY_FROZEN_PROFILE_ID:
        raise ValidationError("profile identity is not a frozen N7 campaign profile")
    _exact_fields(
        raw,
        (
            _PAIRED_MANIFEST_FIELDS
            if paired
            else (
                _RECURRING_MANIFEST_FIELDS
                if recurring
                else _MANIFEST_FIELDS
            )
        ),
        "manifest",
    )
    if _integer(raw["schema_version"], "manifest.schema_version") != SCHEMA_VERSION:
        raise ValidationError("unsupported manifest schema_version")
    if raw["scenario"] != SCENARIO:
        raise ValidationError(f"manifest.scenario must be {SCENARIO}")
    _require_frozen_header(raw, "manifest")
    run_id = _string(raw["run_id"], "manifest.run_id")
    pair_id: str | None = None
    pair_arm: str | None = None
    if paired:
        pair_id = _string(raw["pair_id"], "manifest.pair_id")
        pair_arm = _string(raw["pair_arm"], "manifest.pair_arm")
        expected_pair_arm = (
            "adaptive"
            if profile_identity == PAIRED_ADAPTIVE_PROFILE_ID
            else "control"
        )
        if pair_arm != expected_pair_arm:
            raise ValidationError(
                "manifest pair_arm differs from its paired profile identity"
            )
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
    expected_profile_sha = {
        FROZEN_PROFILE_ID: FROZEN_PROFILE_SHA256,
        PAIRED_ADAPTIVE_PROFILE_ID: PAIRED_ADAPTIVE_PROFILE_SHA256,
        PAIRED_CONTROL_PROFILE_ID: PAIRED_CONTROL_PROFILE_SHA256,
        LEGACY_FROZEN_PROFILE_ID: LEGACY_FROZEN_PROFILE_SHA256,
    }[profile_identity]
    if profile_sha != expected_profile_sha:
        raise ValidationError("manifest does not pin the canonical frozen profile SHA-256")
    if hashlib.sha256(profile_bytes).hexdigest() != profile_sha:
        raise ValidationError("profile sha256 does not match the preserved profile")
    if recurring:
        canonical_profile = _repository_frozen_profile(profile_identity)
        if profile_bytes != canonical_profile:
            raise ValidationError(
                "run profile is not byte-exact canonical frozen recurring profile"
            )
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
    _exact_fields(
        manager,
        _RECURRING_MANAGER_FIELDS if recurring else _MANAGER_FIELDS,
        "manifest.manager",
    )
    manager_id = _string(manager["source_id"], "manifest.manager.source_id")
    if manager["receives_crash_ground_truth"] is not False:
        raise ValidationError(
            "manager.receives_crash_ground_truth must be false"
        )
    if manager_id != "adaptive-manager":
        raise ValidationError("manager source_id must be adaptive-manager")
    transition_requests: tuple[Mapping[str, Any], ...] = ()
    throughput_windows: tuple[analysis.PhaseWindow, ...] = ()
    required_bucket_counts: dict[str, int]
    containment_ratio: float | None = None
    optimized_ratio: float | None = None
    control_late_ratio: float | None = None
    if recurring:
        transition_requests = _transition_requests(
            raw["transition_requests"], "manifest.transition_requests"
        )
        profile_requests = _transition_requests(
            profile_json["transition_requests"], "profile.transition_requests"
        )
        if not _json_values_equal(transition_requests, profile_requests):
            raise ValidationError(
                "manifest transition requests differ from the frozen profile"
            )
        if not _json_values_equal(
            runtime["transition_requests"], transition_requests
        ):
            raise ValidationError(
                "runtime transition requests differ from the manifest"
            )
        artifact_ids = _list(
            manager["transition_artifact_ids"],
            "manifest.manager.transition_artifact_ids",
        )
        if artifact_ids != [
            request["transition_artifact_id"] for request in transition_requests
        ]:
            raise ValidationError(
                "manager transition artifact IDs differ from the requests"
            )
        window_specs = _throughput_window_specs(
            profile_json["throughput_windows"], "profile.throughput_windows"
        )
        if not _json_values_equal(runtime["throughput_windows"], window_specs):
            raise ValidationError(
                "runtime throughput windows differ from the frozen profile"
            )
        throughput_windows = _measurement_windows(
            raw["throughput_windows"], window_specs
        )
        required_bucket_counts = {
            str(spec["phase"]): int(spec["bucket_count"])
            for spec in window_specs
        }
        containment_ratio = float(
            profile_json["minimum_containment_to_degraded_ratio"]
        )
        if profile_identity == PAIRED_CONTROL_PROFILE_ID:
            control_late_ratio = float(
                profile_json[
                    "minimum_control_late_to_containment_ratio"
                ]
            )
        else:
            optimized_ratio = float(
                profile_json["minimum_optimized_to_containment_ratio"]
            )
    else:
        required_bucket_counts = {
            "baseline": int(profile_json["baseline_bucket_count"]),
            "post": int(profile_json["post_bucket_count"]),
        }

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
    minimum_post_activation_grace_ns = _integer(
        raw["minimum_post_activation_grace_ns"],
        "manifest.minimum_post_activation_grace_ns",
        minimum=1,
    )
    if minimum_post_activation_grace_ns != (
        int(profile_json["minimum_post_activation_grace_s"]) * 1_000_000_000
    ):
        raise ValidationError(
            "minimum post-activation grace differs from the frozen profile"
        )
    if recurring:
        _validate_transition_residencies(
            transition_requests,
            window_specs,
            bucket_width_ns=width,
            post_activation_grace_ns=minimum_post_activation_grace_ns,
        )
    maximum_activation_to_successor_ns = _integer(
        profile_json["maximum_activation_to_successor_s"],
        "profile.maximum_activation_to_successor_s",
        minimum=1,
    ) * 1_000_000_000
    baseline_bucket_count = required_bucket_counts["baseline"]
    post_bucket_count = required_bucket_counts.get("post", 0)
    final_measurement_delay_ns = (
        _integer(
            profile_json["final_measurement_delay_ms"],
            "profile.final_measurement_delay_ms",
            maximum=MAXIMUM_PREDECESSOR_RESIDENCY_MS,
        )
        * 1_000_000
        if paired
        else 0
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

    boundary = _object(
        raw["crash_configuration_boundary"],
        "manifest.crash_configuration_boundary",
    )
    _exact_fields(
        boundary,
        _CRASH_CONFIGURATION_BOUNDARY_FIELDS,
        "manifest.crash_configuration_boundary",
    )
    epoch_number = _integer(
        boundary["epoch_number"],
        "crash_configuration_boundary.epoch_number",
        maximum=analysis.UINT32_MAX,
    )
    tree_id = _integer(
        boundary["tree_id"],
        "crash_configuration_boundary.tree_id",
        maximum=analysis.UINT32_MAX,
    )
    root_replica = _integer(
        boundary["root_replica"],
        "crash_configuration_boundary.root_replica",
        maximum=analysis.UINT32_MAX,
    )
    epoch_digest = _hash(
        boundary["epoch_digest"],
        "crash_configuration_boundary.epoch_digest",
    )
    if boundary["context_generation"] is not None:
        raise ValidationError(
            "configuration-active evidence must record unavailable generation as null"
        )
    evidence_values = _list(
        boundary["replica_evidence"],
        "crash_configuration_boundary.replica_evidence",
    )
    evidence: list[CrashConfigurationEvidenceSpec] = []
    for index, item in enumerate(evidence_values):
        reference = _object(
            item, f"crash_configuration_boundary.replica_evidence[{index}]"
        )
        _exact_fields(
            reference,
            _CRASH_CONFIGURATION_EVIDENCE_FIELDS,
            f"crash_configuration_boundary.replica_evidence[{index}]",
        )
        source_id = _string(
            reference["source_id"],
            f"crash_configuration_boundary.replica_evidence[{index}].source_id",
        )
        source_sequence = _integer(
            reference["source_sequence"],
            f"crash_configuration_boundary.replica_evidence[{index}].source_sequence",
            minimum=1,
        )
        timestamp_ns = _integer(
            reference["source_monotonic_ns"],
            f"crash_configuration_boundary.replica_evidence[{index}].source_monotonic_ns",
            minimum=1,
        )
        evidence.append(
            CrashConfigurationEvidenceSpec(
                source_id,
                source_sequence,
                timestamp_ns,
            )
        )
    expected_source_ids = tuple(f"replica-{replica}" for replica in MEMBERSHIP)
    if tuple(item.source_id for item in evidence) != expected_source_ids:
        raise ValidationError(
            "crash boundary must bind replicas 0..6 in canonical source order"
        )
    if epoch_number != 0 or tree_id != 6 or root_replica != 6:
        raise ValidationError("crash boundary must bind exact epoch-0 tree/root 6")
    if max(item.timestamp_ns for item in evidence) >= min(
        marker.requested_ns for marker in markers
    ):
        raise ValidationError("crash boundary evidence does not precede crash request")

    return Manifest(
        raw=raw,
        path=path.resolve(),
        run_id=run_id,
        kauri_revision=revision,
        profile_identity=profile_identity,
        pair_id=pair_id,
        pair_arm=pair_arm,
        profile_path=profile_path,
        profile_bytes=profile_bytes,
        authoritative_observer=observer,
        manager_source_id=manager_id,
        baseline_start_ns=baseline_start_ns,
        end_ns=end_ns,
        minimum_post_activation_grace_ns=minimum_post_activation_grace_ns,
        maximum_activation_to_successor_ns=maximum_activation_to_successor_ns,
        baseline_bucket_count=baseline_bucket_count,
        post_bucket_count=post_bucket_count,
        final_measurement_delay_ns=final_measurement_delay_ns,
        transition_requests=transition_requests,
        throughput_windows=throughput_windows,
        required_bucket_counts=required_bucket_counts,
        minimum_containment_to_degraded_ratio=containment_ratio,
        minimum_optimized_to_containment_ratio=optimized_ratio,
        minimum_control_late_to_containment_ratio=control_late_ratio,
        maximum_stall_ns=maximum_stall_ns,
        degraded_maximum_stall_ns=degraded_maximum_stall_ns,
        runtime=runtime,
        runtime_artifacts=runtime_artifacts,
        sources=tuple(sources),
        crash_markers=tuple(markers),
        crash_configuration_boundary=CrashConfigurationBoundarySpec(
            epoch_number,
            tree_id,
            root_replica,
            epoch_digest,
            None,
            tuple(evidence),
        ),
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
    if len(epoch_values) < 2:
        raise IncompleteRun("epoch definition requires at least one transition")

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
            raise ValidationError("epoch numbers must be contiguous from 0")
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

    initial = epochs[0]
    successor = epochs[1]
    if len({epoch.epoch_digest for epoch in epochs}) != len(epochs):
        raise ValidationError("epoch digests must be distinct across transitions")
    if initial.command is not None:
        raise ValidationError("epoch 0 command must be null")

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

    payload_digests: set[str] = set()
    previous_command_height = 0
    previous_activation_height = 0
    for epoch_index, current in enumerate(epochs[1:], start=1):
        if len(current.trees) != 5:
            raise ValidationError("successor epoch must contain exactly five trees")
        predecessor = epochs[epoch_index - 1]
        if epoch_index == 1:
            inherited_wait_exempt = CRASHED_REPLICAS
        else:
            predecessor_wait_exempt = {
                tree.wait_exempt for tree in predecessor.trees
            }
            if len(predecessor_wait_exempt) != 1:
                raise ValidationError(
                    "predecessor epoch has no canonical wait-exempt set to inherit"
                )
            inherited_wait_exempt = next(iter(predecessor_wait_exempt))
            if inherited_wait_exempt != CRASHED_REPLICAS:
                raise ValidationError(
                    "optimization predecessor must preserve wait-exempt replicas 0 and 1"
                )
        for tree_id, tree in enumerate(current.trees):
            if tree.tree_id != tree_id or tree.fanout != 2:
                raise ValidationError("successor trees require ids 0..4 and fanout 2")
            if len(tree.members) != len(MEMBERSHIP):
                raise ValidationError("successor tree must contain all seven replicas")
            if set(tree.members) != set(MEMBERSHIP):
                raise ValidationError("successor tree membership must remain unchanged")
            if tree.wait_exempt != inherited_wait_exempt:
                if epoch_index == 1:
                    raise ValidationError("only replicas 0 and 1 may be wait-exempt")
                raise ValidationError(
                    f"epoch {current.epoch_number} wait-exempt set does not inherit "
                    f"epoch {predecessor.epoch_number}'s canonical set"
                )
            leaf_start = (len(tree.members) - 2) // tree.fanout + 1
            for failed in CRASHED_REPLICAS:
                if tree.members.index(failed) < leaf_start:
                    raise ValidationError(
                        f"replica {failed} is not a physical leaf in successor tree {tree_id}"
                    )
        roots = tuple(tree.leader for tree in current.trees)
        if len(set(roots)) != QUORUM or set(roots) != set(SUCCESSOR_ROOTS):
            raise ValidationError(
                "successor roots must be exactly the five surviving replicas"
            )

        command = current.command
        if command is None:
            raise IncompleteRun(
                f"successor epoch {current.epoch_number} lacks its committed command"
            )
        for field in (
            "command_block_height",
            "activation_delay_blocks",
            "activation_height",
        ):
            _integer(command[field], f"epochs[{epoch_index}].command.{field}", minimum=1)
        for field in ("predecessor_epoch_number", "successor_epoch_number"):
            _integer(
                command[field],
                f"epochs[{epoch_index}].command.{field}",
                maximum=analysis.UINT32_MAX,
            )
        for field in (
            "command_block_hash",
            "payload_digest",
            "predecessor_epoch_digest",
            "successor_epoch_digest",
        ):
            _hash(command[field], f"epochs[{epoch_index}].command.{field}")
        if (
            command["predecessor_epoch_number"] != predecessor.epoch_number
            or command["predecessor_epoch_digest"] != predecessor.epoch_digest
        ):
            raise ValidationError(
                f"epoch {current.epoch_number} command does not continue the exact predecessor"
            )
        if (
            command["successor_epoch_number"] != current.epoch_number
            or command["successor_epoch_digest"] != current.epoch_digest
        ):
            raise ValidationError(
                f"epoch {current.epoch_number} command does not bind its successor"
            )
        if command["activation_height"] != (
            command["command_block_height"] + command["activation_delay_blocks"]
        ):
            raise ValidationError("command activation height must equal h_c + delta")
        payload_digest = str(command["payload_digest"])
        if payload_digest in payload_digests:
            raise ValidationError("command payload digest is reused across transitions")
        payload_digests.add(payload_digest)
        if (
            int(command["command_block_height"]) <= previous_command_height
            or int(command["activation_height"]) <= previous_activation_height
        ):
            raise ValidationError("transition command heights must increase")
        previous_command_height = int(command["command_block_height"])
        previous_activation_height = int(command["activation_height"])

    leader_map: dict[analysis.ConfigurationKey, int] = {}
    for epoch in epochs:
        for tree in epoch.trees:
            if tree.configuration_key in leader_map:
                raise ValidationError("duplicate configuration identity")
            leader_map[tree.configuration_key] = tree.leader
    return EpochDocument(
        raw,
        path.resolve(),
        tuple(epochs),
        initial,
        successor,
        leader_map,
    )


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


def _canonical_membership_digest(membership: Sequence[int]) -> str:
    payload = b"".join(
        (
            MEMBERSHIP_DOMAIN,
            len(membership).to_bytes(4, "big"),
            b"".join(replica.to_bytes(2, "big") for replica in sorted(membership)),
        )
    )
    return hashlib.sha256(payload).hexdigest()


def _validate_recurring_transition_bundles(
    manifest: Manifest,
    epochs: EpochDocument,
    manager_events: Sequence[analysis.StructuredEvent],
) -> None:
    artifacts = {
        artifact.relative_path: artifact for artifact in manifest.runtime_artifacts
    }
    snapshots = {
        int(event.payload["cycle_ordinal"]): event
        for event in manager_events
        if event.event_type == "adaptive_v2_evidence_snapshot"
    }
    terminals = {
        int(event.payload["cycle_ordinal"]): event
        for event in manager_events
        if event.event_type == "adaptive_v2_session_terminal"
    }
    expected_membership_digest = _canonical_membership_digest(MEMBERSHIP)
    snapshot_ids: set[str] = set()

    for ordinal, request in enumerate(manifest.transition_requests):
        relative_path = str(request["bundle_path"])
        try:
            decoded = campaign_runner.decode_epoch_change_bundle(
                artifacts[relative_path].payload
            )
        except campaign_runner.RunnerError as exc:
            raise ValidationError(
                f"transition bundle {relative_path} is not canonical: {exc}"
            ) from exc

        predecessor = epochs.epochs[ordinal]
        successor = epochs.epochs[ordinal + 1]
        command = _object(successor.command, "recurring successor command")
        decoded_command = decoded.command
        if (
            decoded_command.issuer_id != campaign_runner.ISSUER_ID
            or decoded_command.successor_epoch_number
            != command["successor_epoch_number"]
            or decoded_command.predecessor_epoch_digest
            != command["predecessor_epoch_digest"]
            or decoded_command.successor_epoch_digest
            != command["successor_epoch_digest"]
            or decoded_command.activation_delay_blocks
            != command["activation_delay_blocks"]
            or campaign_runner.epoch_change_payload_digest(decoded_command)
            != command["payload_digest"]
        ):
            raise ValidationError(
                f"transition bundle {relative_path} command identity differs from epochs.json"
            )

        expected_trees = tuple(
            (
                tree.tree_id,
                tree.fanout,
                int(manifest.runtime["pipeline_depth"]),
                tree.members,
                tree.wait_exempt,
            )
            for tree in successor.trees
        )
        decoded_trees = tuple(
            (
                tree.tree_id,
                tree.fanout,
                tree.pipeline_stretch,
                tree.members,
                tree.wait_exempt,
            )
            for tree in decoded.trees
        )
        if (
            decoded.epoch_number != successor.epoch_number
            or decoded.epoch_digest != successor.epoch_digest
            or decoded.previous_epoch_digest != predecessor.epoch_digest
            or decoded.membership_digest != expected_membership_digest
            or decoded.generation_seed != manifest.runtime["snapshot_seed"]
            or decoded.policy_version != PLACEMENT_POLICY_VERSION
            or decoded_trees != expected_trees
        ):
            raise ValidationError(
                f"transition bundle {relative_path} definition differs from epochs.json"
            )

        snapshot_id = _string(
            decoded.evidence_snapshot_id,
            f"transition bundle {relative_path} evidence snapshot ID",
        )
        if snapshot_id in snapshot_ids:
            raise ValidationError(
                "transition bundles reuse one evidence snapshot identity"
            )
        snapshot_ids.add(snapshot_id)
        snapshot_cutoff = snapshots[ordinal].payload["current_cutoff"]
        terminal_cutoff = terminals[ordinal].payload["current_evidence_cutoff"]
        if (
            decoded.evidence_cutoff != snapshot_cutoff
            or decoded.evidence_cutoff != terminal_cutoff
        ):
            raise ValidationError(
                f"transition bundle {relative_path} evidence cutoff differs from manager events"
            )


def _command_identity(command: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "predecessor_epoch_number": command["predecessor_epoch_number"],
        "predecessor_epoch_digest": command["predecessor_epoch_digest"],
        "successor_epoch_number": command["successor_epoch_number"],
        "successor_epoch_digest": command["successor_epoch_digest"],
        "command_payload_digest": command["payload_digest"],
        "command_block_height": command["command_block_height"],
        "command_block_hash": command["command_block_hash"],
        "activation_delay_blocks": command["activation_delay_blocks"],
        "activation_height": command["activation_height"],
    }


def _missing_transition_label(index: int) -> str:
    if index == 0:
        return "first transition"
    if index == 1:
        return "second transition"
    return f"transition {index + 1}"


def _checked_activation_generation(epoch_number: int) -> int:
    epoch = _integer(
        epoch_number,
        "activation-generation epoch",
        maximum=analysis.UINT32_MAX,
    )
    generation = (epoch << 32) | 1
    if generation <= 0 or generation > analysis.UINT64_MAX:
        raise ValidationError("activation generation is outside uint64 range")
    return generation


@dataclass
class _SnapshotAttempt:
    observation_id: str
    first_ingestion_sequence: int
    reporter_id: int
    target_id: int
    state: str
    latency_ns: int | None


@dataclass(frozen=True)
class _SnapshotReplicaScore:
    replica_id: int
    attempt_count: int
    response_rate_ppm: int
    timeout_rate_ppm: int
    trailing_timeout_count: int
    latency_percentile_ns: int | None
    eligible: bool
    reasons: tuple[str, ...]


def _replay_snapshot_attempts(
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, _SnapshotAttempt]:
    attempts: dict[str, _SnapshotAttempt] = {}
    previous_sequence = 0
    for observation in observations:
        sequence = _integer(
            observation.get("ingestion_sequence"),
            "snapshot observation ingestion_sequence",
            minimum=1,
        )
        if sequence <= previous_sequence:
            raise ValidationError(
                "snapshot observations are not ordered by ingestion sequence"
            )
        previous_sequence = sequence
        observation_id = _hash(
            observation.get("observation_id"), "snapshot observation_id"
        )
        reporter = _integer(
            observation.get("reporter_id"),
            "snapshot observation reporter_id",
            maximum=max(MEMBERSHIP),
        )
        target = _integer(
            observation.get("target_id"),
            "snapshot observation target_id",
            maximum=max(MEMBERSHIP),
        )
        if reporter not in MEMBERSHIP or target not in MEMBERSHIP:
            raise ValidationError("snapshot attempt contains a non-member")
        if reporter == target:
            raise ValidationError(
                "snapshot observation reporter and target must differ"
            )
        outcome = _string(
            observation.get("outcome"), "snapshot observation outcome"
        )
        if outcome not in ("on_time", "late", "timeout"):
            raise ValidationError("snapshot observation outcome is invalid")
        latency_value = observation.get("latency_ns")
        if outcome == "timeout":
            if latency_value is not None:
                raise ValidationError("snapshot timeout attempt contains latency")
            latency: int | None = None
        elif outcome == "late":
            latency = _integer(
                latency_value,
                "snapshot late attempt latency_ns",
                minimum=1,
            )
        else:
            # The manager omits latency_ns when the underlying C++ response
            # duration is zero; AdaptationSnapshot still ranks that value as 0.
            latency = (
                0
                if latency_value is None
                else _integer(
                    latency_value,
                    "snapshot on-time attempt latency_ns",
                    minimum=1,
                )
            )

        prior = attempts.get(observation_id)
        if prior is None:
            if outcome == "late":
                raise ValidationError(
                    "snapshot adaptation attempt starts with a late response"
                )
            attempts[observation_id] = _SnapshotAttempt(
                observation_id=observation_id,
                first_ingestion_sequence=sequence,
                reporter_id=reporter,
                target_id=target,
                state="timeout_only" if outcome == "timeout" else "on_time",
                latency_ns=latency,
            )
            continue

        if prior.reporter_id != reporter or prior.target_id != target:
            raise ValidationError(
                "snapshot timeout-to-late correlation differs in reporter or target"
            )
        if prior.state != "timeout_only" or outcome != "late":
            raise ValidationError("snapshot adaptation attempt transition is invalid")
        prior.state = "late"
        prior.latency_ns = latency
    return attempts


def _score_snapshot_replicas(
    attempts: Iterable[_SnapshotAttempt],
) -> dict[int, _SnapshotReplicaScore]:
    by_target: dict[int, list[_SnapshotAttempt]] = {
        target: [] for target in MEMBERSHIP
    }
    for attempt in attempts:
        by_target[attempt.target_id].append(attempt)

    scores: dict[int, _SnapshotReplicaScore] = {}
    for target, target_attempts in by_target.items():
        target_attempts.sort(
            key=lambda attempt: attempt.first_ingestion_sequence
        )
        target_attempts = target_attempts[-RESPONSIVENESS_ATTEMPT_WINDOW:]
        response_count = 0
        timeout_count = 0
        latencies: list[int] = []
        for attempt in target_attempts:
            if attempt.state == "on_time":
                response_count += 1
                assert attempt.latency_ns is not None
                latencies.append(attempt.latency_ns)
            elif attempt.state == "timeout_only":
                timeout_count += 1
            elif attempt.state == "late":
                response_count += 1
                timeout_count += 1
                assert attempt.latency_ns is not None
                latencies.append(attempt.latency_ns)
            else:  # pragma: no cover - replay constructs a closed state set.
                raise AssertionError("unexpected snapshot attempt state")

        attempt_count = len(target_attempts)
        response_rate = (
            response_count * RESPONSIVENESS_RATE_PPM_SCALE // attempt_count
            if attempt_count
            else 0
        )
        timeout_rate = (
            timeout_count * RESPONSIVENESS_RATE_PPM_SCALE // attempt_count
            if attempt_count
            else 0
        )
        trailing_timeouts = 0
        for attempt in reversed(target_attempts):
            if attempt.state != "timeout_only":
                break
            trailing_timeouts += 1
        latency_percentile: int | None = None
        if latencies:
            latencies.sort()
            numerator = LATENCY_PERCENTILE_BASIS_POINTS * len(latencies)
            rank = (numerator + 10_000 - 1) // 10_000
            latency_percentile = latencies[rank - 1]

        reasons: list[str] = []
        if attempt_count < RESPONSIVENESS_MINIMUM_ATTEMPTS:
            reasons.append("insufficient attempts")
        else:
            if response_rate < MINIMUM_RESPONSE_RATE_PPM:
                reasons.append("response rate below minimum")
            if timeout_rate > MAXIMUM_TIMEOUT_RATE_PPM:
                reasons.append("timeout rate above maximum")
            if trailing_timeouts >= TRAILING_TIMEOUT_STREAK:
                reasons.append("trailing timeout streak")
        scores[target] = _SnapshotReplicaScore(
            replica_id=target,
            attempt_count=attempt_count,
            response_rate_ppm=response_rate,
            timeout_rate_ppm=timeout_rate,
            trailing_timeout_count=trailing_timeouts,
            latency_percentile_ns=latency_percentile,
            eligible=not reasons,
            reasons=tuple(reasons),
        )
    return scores


def _cpp_snapshot_ranking(
    scores: Mapping[int, _SnapshotReplicaScore],
) -> list[int]:
    return sorted(
        MEMBERSHIP,
        key=lambda target: (
            0 if scores[target].eligible else 1,
            -scores[target].response_rate_ppm,
            scores[target].timeout_rate_ppm,
            0 if scores[target].latency_percentile_ns is not None else 1,
            (
                scores[target].latency_percentile_ns
                if scores[target].latency_percentile_ns is not None
                else 0
            ),
            -scores[target].attempt_count,
            target,
        ),
    )


def _validate_recurring_evidence_window(
    request: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    *,
    baseline_cutoff: int,
    minimum_attempts: int,
    minimum_reporters: int,
) -> list[int] | None:
    baseline_cutoff = _integer(
        baseline_cutoff, "snapshot baseline cutoff", minimum=1
    )
    # Replay the full prefix first so malformed sequence types/order cannot be
    # hidden by boundary partitioning.
    full_attempts = _replay_snapshot_attempts(observations)
    baseline_observations = [
        observation
        for observation in observations
        if _integer(
            observation.get("ingestion_sequence"),
            "snapshot observation ingestion_sequence",
            minimum=1,
        )
        <= baseline_cutoff
    ]
    intent = request["policy_intent"]
    if intent not in ("fault_containment", "performance_optimization"):
        raise ValidationError("snapshot has an unsupported transition policy intent")

    # Replay the baseline independently because a late response after the
    # boundary must not retroactively change baseline state.
    baseline_attempts = _replay_snapshot_attempts(baseline_observations)
    baseline_scores = _score_snapshot_replicas(baseline_attempts.values())
    responsive_baseline = sum(
        score.eligible for score in baseline_scores.values()
    )
    required_baseline = (
        len(MEMBERSHIP) if intent == "fault_containment" else QUORUM
    )
    if responsive_baseline < required_baseline:
        raise ValidationError(
            "snapshot baseline lacks the configured responsive replica count"
        )

    # Attempts belong to the window containing their first ingestion.  A late
    # response to a pre-baseline timeout is therefore legal but excluded from
    # fresh classification, ranking, and guarded timeout counts.
    fresh_attempts = {
        observation_id: attempt
        for observation_id, attempt in full_attempts.items()
        if attempt.first_ingestion_sequence > baseline_cutoff
    }

    if intent == "fault_containment":
        timeout_reporters: dict[int, dict[int, int]] = {
            target: {} for target in CRASHED_REPLICAS
        }
        for attempt in fresh_attempts.values():
            if (
                attempt.state != "timeout_only"
                or attempt.target_id not in CRASHED_REPLICAS
            ):
                continue
            counts = timeout_reporters[attempt.target_id]
            counts[attempt.reporter_id] = (
                counts.get(attempt.reporter_id, 0) + 1
            )
        for target, reporters in timeout_reporters.items():
            qualifying = sum(
                count >= minimum_attempts for count in reporters.values()
            )
            if qualifying < minimum_reporters:
                raise ValidationError(
                    f"snapshot target {target} lacks configured guarded timeout attempts"
                )

        full_scores = _score_snapshot_replicas(full_attempts.values())
        if any(full_scores[target].eligible for target in CRASHED_REPLICAS):
            raise ValidationError(
                "containment crash target is not snapshot-nonresponsive"
            )
        if any(not full_scores[target].eligible for target in SURVIVING_REPLICAS):
            raise ValidationError(
                "containment does not retain exactly Q responsive survivors"
            )
        return None

    fresh_scores = _score_snapshot_replicas(fresh_attempts.values())
    for target in SURVIVING_REPLICAS:
        score = fresh_scores[target]
        if score.eligible:
            continue
        reasons = ", ".join(score.reasons)
        if "insufficient attempts" in score.reasons:
            raise ValidationError(
                f"optimization suffix lacks configured fresh on-time attempts for target {target}"
            )
        raise ValidationError(
            f"optimization target {target} is nonresponsive: {reasons}"
        )
    ranking = [
        target
        for target in _cpp_snapshot_ranking(fresh_scores)
        if target not in CRASHED_REPLICAS and fresh_scores[target].eligible
    ]
    if len(ranking) != QUORUM or set(ranking) != set(SURVIVING_REPLICAS):
        raise ValidationError(
            "optimization does not have exactly Q unconstrained eligible survivors"
        )
    return ranking


def _validate_recurring_manager_sessions(
    manifest: Manifest,
    epochs: EpochDocument,
    events: Sequence[analysis.StructuredEvent],
) -> analysis.StructuredEvent:
    if any(
        event.event_type == "adaptive_v2_convergence_failure"
        for event in events
    ):
        raise ValidationError("manager emitted adaptive_v2_convergence_failure")
    ready_events = [event for event in events if event.event_type == "adaptive_v2_ready"]
    terminal_events = [
        event
        for event in events
        if event.event_type == "adaptive_v2_session_terminal"
    ]
    snapshot_events = [
        event
        for event in events
        if event.event_type == "adaptive_v2_evidence_snapshot"
    ]
    artifacts = {
        artifact.relative_path: artifact for artifact in manifest.runtime_artifacts
    }
    ready_by_key: dict[tuple[int, int], analysis.StructuredEvent] = {}
    for ready in ready_events:
        payload = _object(ready.payload, "adaptive_v2_ready payload")
        _exact_fields(
            payload,
            _MANAGER_CONVERGENCE_PAYLOAD_FIELDS,
            "adaptive_v2_ready payload",
        )
        identity = _object(payload["identity"], "adaptive_v2_ready identity")
        _exact_fields(
            identity,
            _MANAGER_CONVERGENCE_IDENTITY_FIELDS,
            "adaptive_v2_ready identity",
        )
        key = (
            _integer(identity["predecessor_epoch_number"], "ready predecessor"),
            _integer(identity["successor_epoch_number"], "ready successor"),
        )
        if key in ready_by_key:
            raise ValidationError("duplicate adaptive_v2_ready for one transition")
        if any(
            payload[field] is not None
            for field in (
                "replica_id",
                "delivery_attempt",
                "disposition",
                "canonical_payload_digest",
                "failure_reason",
            )
        ):
            raise ValidationError("adaptive_v2_ready is not the exact terminal record")
        if (
            _integer(payload["accepted_activation_count"], "accepted activations")
            != QUORUM
            or _integer(payload["required_activation_count"], "required activations")
            != QUORUM
        ):
            raise ValidationError("adaptive_v2_ready does not prove the fixed quorum")
        _integer(payload["accepted_commit_count"], "accepted commits")
        ready_by_key[key] = ready

    terminal_by_ordinal: dict[int, analysis.StructuredEvent] = {}
    for terminal in terminal_events:
        payload = _object(
            terminal.payload, "adaptive_v2_session_terminal payload"
        )
        _exact_fields(
            payload,
            _MANAGER_SESSION_TERMINAL_FIELDS,
            "adaptive_v2_session_terminal payload",
        )
        ordinal = _integer(payload["cycle_ordinal"], "terminal cycle_ordinal")
        if ordinal in terminal_by_ordinal:
            raise ValidationError("duplicate adaptive_v2_session_terminal record")
        terminal_by_ordinal[ordinal] = terminal

    snapshot_by_ordinal: dict[int, analysis.StructuredEvent] = {}
    for snapshot in snapshot_events:
        payload = _object(snapshot.payload, "adaptive_v2_evidence_snapshot payload")
        _exact_fields(
            payload,
            _EVIDENCE_SNAPSHOT_FIELDS,
            "adaptive_v2_evidence_snapshot payload",
        )
        ordinal = _integer(payload["cycle_ordinal"], "snapshot cycle_ordinal")
        if ordinal in snapshot_by_ordinal:
            raise ValidationError("duplicate adaptive_v2_evidence_snapshot record")
        snapshot_by_ordinal[ordinal] = snapshot

    reputation_cycles: list[list[analysis.StructuredEvent]] = []
    current_cycle: list[analysis.StructuredEvent] = []
    previous_ingestion = 0
    for event in events:
        if event.event_type != "reputation.evidence_applied":
            continue
        payload = _object(event.payload, "reputation evidence payload")
        _exact_fields(payload, _REPUTATION_FIELDS, "reputation evidence payload")
        ingestion = _integer(
            payload["ingestion_sequence"], "reputation ingestion_sequence", minimum=1
        )
        if ingestion <= previous_ingestion:
            if ingestion != 1 or not current_cycle:
                raise ValidationError(
                    "reputation ingestion sequence reset is not canonical"
                )
            reputation_cycles.append(current_cycle)
            current_cycle = []
        current_cycle.append(event)
        previous_ingestion = ingestion
    if current_cycle:
        reputation_cycles.append(current_cycle)

    previous_terminal_sequence = 0
    final_ready: analysis.StructuredEvent | None = None
    # One cycle may contain the two records of a timeout-to-late attempt; the
    # replay below validates that exact transition.  Reuse across ledgers is
    # still forbidden even when ingestion sequences restart.
    observation_cycles: dict[str, int] = {}
    profile = _decode_frozen_profile(
        manifest.profile_bytes, "recurring manager profile"
    )
    minimum_attempts = _integer(
        profile["minimum_timeout_observations_per_reporter"],
        "profile minimum attempts",
        minimum=1,
    )
    minimum_reporters = _integer(
        profile["minimum_qualifying_reporters"],
        "profile minimum qualifying reporters",
        minimum=1,
    )
    for index, request in enumerate(manifest.transition_requests):
        if index + 1 >= len(epochs.epochs):
            raise IncompleteRun(
                f"{_missing_transition_label(index)} has no successor epoch"
            )
        successor = epochs.epochs[index + 1]
        command = _object(successor.command, "recurring successor command")
        expected_identity = _command_identity(command)
        key = (
            int(request["predecessor_epoch_number"]),
            int(request["successor_epoch_number"]),
        )
        ready = ready_by_key.get(key)
        terminal = terminal_by_ordinal.get(index)
        snapshot = snapshot_by_ordinal.get(index)
        if ready is None or terminal is None or snapshot is None:
            raise IncompleteRun(
                f"{_missing_transition_label(index)} lacks its ready, terminal, or evidence snapshot record"
            )
        ready_identity = _object(ready.payload["identity"], "ready identity")
        if not _json_values_equal(ready_identity, expected_identity):
            raise ValidationError(
                "adaptive_v2_ready identity differs from the canonical epoch command"
            )
        terminal_payload = _object(terminal.payload, "session terminal payload")
        if (
            terminal_payload["cycle_ordinal"] != index
            or terminal_payload["policy_intent"] != request["policy_intent"]
            or terminal_payload["outcome"] != "advanced"
            or terminal_payload["reason"] != "successor_converged"
            or terminal_payload["transition_artifact_id"]
            != request["transition_artifact_id"]
            or terminal_payload["predecessor_epoch_number"] != key[0]
            or terminal_payload["predecessor_epoch_digest"]
            != command["predecessor_epoch_digest"]
            or terminal_payload["successor_epoch_number"] != key[1]
            or terminal_payload["successor_epoch_digest"]
            != command["successor_epoch_digest"]
            or terminal_payload["command_payload_digest"]
            != command["payload_digest"]
            or not _json_values_equal(
                terminal_payload["winning_activation"], expected_identity
            )
        ):
            raise ValidationError(
                "adaptive_v2_session_terminal does not bind its requested transition"
            )
        snapshot_payload = _object(snapshot.payload, "evidence snapshot payload")
        artifact = artifacts[request["evidence_snapshot_path"]]
        artifact_payload = _load_json_bytes(
            artifact.payload, request["evidence_snapshot_path"]
        )
        if not _json_values_equal(snapshot_payload, artifact_payload):
            raise ValidationError(
                "manager evidence snapshot event differs from its immutable artifact"
            )
        predecessor = epochs.epochs[index]
        observations = _list(
            snapshot_payload.get("observations"), "evidence snapshot observations"
        )
        if not observations or any(
            not isinstance(observation, dict)
            or observation.get("epoch_number") != predecessor.epoch_number
            or observation.get("epoch_digest") != predecessor.epoch_digest
            for observation in observations
        ):
            raise ValidationError(
                f"{_missing_transition_label(index)} requires fresh Epoch {predecessor.epoch_number} evidence only"
            )
        if index >= len(reputation_cycles):
            raise IncompleteRun(
                f"{_missing_transition_label(index)} lacks its emitted reputation evidence prefix"
            )
        cycle_events = reputation_cycles[index]
        if index > 0:
            previous_terminal = terminal_by_ordinal[index - 1]
            if not (
                cycle_events[0].source_sequence > previous_terminal.source_sequence
                and cycle_events[0].timestamp_ns >= previous_terminal.timestamp_ns
            ):
                raise ValidationError(
                    "reputation ingestion sequence reset precedes the authenticated successor-window boundary"
                )
        baseline_cutoff = _integer(
            snapshot_payload.get("baseline_cutoff"),
            "snapshot baseline_cutoff",
        )
        current_cutoff = _integer(
            snapshot_payload.get("current_cutoff"),
            "snapshot current_cutoff",
            minimum=1,
        )
        if baseline_cutoff >= current_cutoff:
            raise ValidationError("snapshot evidence cutoffs are not ordered")
        expected_events = [
            event
            for event in cycle_events
            if event.source_sequence < snapshot.source_sequence
        ]
        expected_ingestion = list(range(1, current_cutoff + 1))
        if [
            int(event.payload["ingestion_sequence"])
            for event in expected_events
        ] != expected_ingestion:
            raise ValidationError(
                "snapshot does not cover the full emitted evidence prefix"
            )
        if len(observations) != len(expected_events):
            raise ValidationError(
                "snapshot omits or adds emitted reputation observations"
            )
        for observation, evidence_event in zip(observations, expected_events):
            assert isinstance(observation, dict)
            _exact_fields(
                observation,
                (
                    _EVIDENCE_SNAPSHOT_LATENCY_OBSERVATION_FIELDS
                    if "latency_ns" in observation
                    else _EVIDENCE_SNAPSHOT_OBSERVATION_FIELDS
                ),
                "evidence snapshot observation",
            )
            _integer(
                observation["ingestion_sequence"],
                "snapshot observation ingestion_sequence",
                minimum=1,
            )
            _integer(
                observation["epoch_number"],
                "snapshot observation epoch_number",
                maximum=analysis.UINT32_MAX,
            )
            _hash(
                observation["epoch_digest"],
                "snapshot observation epoch_digest",
            )
            reporter = _integer(
                observation["reporter_id"],
                "snapshot observation reporter_id",
                maximum=max(MEMBERSHIP),
            )
            target = _integer(
                observation["target_id"],
                "snapshot observation target_id",
                maximum=max(MEMBERSHIP),
            )
            if reporter == target:
                raise ValidationError(
                    "snapshot observation reporter and target must differ"
                )
            outcome = _string(
                observation["outcome"], "snapshot observation outcome"
            )
            if outcome not in ("on_time", "late", "timeout"):
                raise ValidationError("snapshot observation outcome is invalid")
            has_latency = "latency_ns" in observation
            if (outcome == "timeout" and has_latency) or (
                outcome == "late" and not has_latency
            ):
                raise ValidationError(
                    "snapshot observation latency does not match its outcome"
                )
            if has_latency:
                _integer(
                    observation["latency_ns"],
                    "snapshot observation latency_ns",
                    minimum=1,
                )
            evidence = evidence_event.payload
            observation_id = _hash(
                observation.get("observation_id"),
                "snapshot observation_id",
            )
            previous_cycle = observation_cycles.setdefault(
                observation_id, index
            )
            if previous_cycle != index:
                raise ValidationError(
                    "snapshot observation is reused across transition cycles"
                )
            expected_observation = {
                "observation_id": evidence["observation_id"],
                "ingestion_sequence": evidence["ingestion_sequence"],
                "reporter_id": evidence["reporter_id"],
                "target_id": evidence["target_id"],
                "outcome": evidence["evidence_outcome"],
            }
            actual_observation = {
                field: observation.get(field) for field in expected_observation
            }
            if not _json_values_equal(actual_observation, expected_observation):
                raise ValidationError(
                    "snapshot observation prefix differs from emitted reputation evidence"
                )
        calculated_ranking = _validate_recurring_evidence_window(
            request,
            observations,
            baseline_cutoff=baseline_cutoff,
            minimum_attempts=minimum_attempts,
            minimum_reporters=minimum_reporters,
        )
        if calculated_ranking is not None:
            if snapshot_payload.get("eligible_ranking") != calculated_ranking:
                raise ValidationError(
                    "optimization ranking does not follow fresh successor-window latency evidence"
                )
        roots = [tree.leader for tree in successor.trees]
        if snapshot_payload.get("eligible_ranking") != roots:
            raise ValidationError(
                "successor roots do not match the fresh eligible ranking"
            )
        if (
            snapshot_payload.get("cycle_ordinal") != index
            or snapshot_payload.get("policy_intent") != request["policy_intent"]
            or snapshot_payload.get("transition_artifact_id")
            != request["transition_artifact_id"]
            or snapshot_payload.get("predecessor_epoch_number") != key[0]
            or snapshot_payload.get("predecessor_epoch_digest")
            != predecessor.epoch_digest
            or snapshot_payload.get("activation_generation")
            != _checked_activation_generation(predecessor.epoch_number)
            or terminal_payload["evidence_window_activation_generation"]
            != snapshot_payload.get("activation_generation")
            or terminal_payload["baseline_evidence_cutoff"]
            != snapshot_payload.get("baseline_cutoff")
            or terminal_payload["current_evidence_cutoff"]
            != snapshot_payload.get("current_cutoff")
        ):
            raise ValidationError("terminal evidence window differs from its snapshot")
        if not (
            snapshot.source_sequence < ready.source_sequence < terminal.source_sequence
            and snapshot.timestamp_ns <= ready.timestamp_ns <= terminal.timestamp_ns
            and ready.source_sequence > previous_terminal_sequence
        ):
            raise ValidationError("manager transition records are not ordered")
        previous_terminal_sequence = terminal.source_sequence
        final_ready = ready

    if (
        len(ready_by_key) != len(manifest.transition_requests)
        or set(terminal_by_ordinal) != set(range(len(manifest.transition_requests)))
        or set(snapshot_by_ordinal) != set(range(len(manifest.transition_requests)))
    ):
        raise ValidationError("manager emitted an unrequested transition session")
    assert final_ready is not None
    return final_ready


def _validate_manager_convergence_ready(
    manifest: Manifest,
    epochs: EpochDocument,
    streams: Mapping[tuple[str, str], Sequence[analysis.StructuredEvent]],
) -> analysis.StructuredEvent:
    events = streams[("adaptation_manager", manifest.manager_source_id)]
    if manifest.transition_requests:
        return _validate_recurring_manager_sessions(manifest, epochs, events)
    if any(
        event.event_type == "adaptive_v2_convergence_failure"
        for event in events
    ):
        raise ValidationError("manager emitted adaptive_v2_convergence_failure")
    ready = [event for event in events if event.event_type == "adaptive_v2_ready"]
    if not ready:
        raise IncompleteRun("manager requires exactly one adaptive_v2_ready event")
    if len(ready) > 1:
        raise ValidationError("manager emitted duplicate adaptive_v2_ready events")
    event = ready[0]
    payload = _object(event.payload, "adaptive_v2_ready payload")
    _exact_fields(
        payload,
        _MANAGER_CONVERGENCE_PAYLOAD_FIELDS,
        "adaptive_v2_ready payload",
    )
    if any(
        payload[field] is not None
        for field in (
            "replica_id",
            "delivery_attempt",
            "disposition",
            "canonical_payload_digest",
            "failure_reason",
        )
    ):
        raise ValidationError("adaptive_v2_ready is not the exact terminal record")
    _integer(
        payload["accepted_commit_count"],
        "adaptive_v2_ready.accepted_commit_count",
        maximum=len(MEMBERSHIP),
    )
    accepted_activations = _integer(
        payload["accepted_activation_count"],
        "adaptive_v2_ready.accepted_activation_count",
        maximum=len(MEMBERSHIP),
    )
    required_activations = _integer(
        payload["required_activation_count"],
        "adaptive_v2_ready.required_activation_count",
        minimum=1,
        maximum=len(MEMBERSHIP),
    )
    if accepted_activations != QUORUM or required_activations != QUORUM:
        raise ValidationError("adaptive_v2_ready does not prove the fixed quorum")

    identity = _object(payload["identity"], "adaptive_v2_ready identity")
    _exact_fields(
        identity,
        _MANAGER_CONVERGENCE_IDENTITY_FIELDS,
        "adaptive_v2_ready identity",
    )
    for field in (
        "predecessor_epoch_number",
        "successor_epoch_number",
        "command_block_height",
        "activation_delay_blocks",
        "activation_height",
    ):
        _integer(identity[field], f"adaptive_v2_ready.identity.{field}")
    for field in (
        "predecessor_epoch_digest",
        "successor_epoch_digest",
        "command_payload_digest",
        "command_block_hash",
    ):
        _hash(identity[field], f"adaptive_v2_ready.identity.{field}")
    command = _object(
        epochs.successor.command,
        "successor command for adaptive_v2_ready",
    )
    expected_identity = {
        "predecessor_epoch_number": command["predecessor_epoch_number"],
        "predecessor_epoch_digest": command["predecessor_epoch_digest"],
        "successor_epoch_number": command["successor_epoch_number"],
        "successor_epoch_digest": command["successor_epoch_digest"],
        "command_payload_digest": command["payload_digest"],
        "command_block_height": command["command_block_height"],
        "command_block_hash": command["command_block_hash"],
        "activation_delay_blocks": command["activation_delay_blocks"],
        "activation_height": command["activation_height"],
    }
    if not _json_values_equal(identity, expected_identity):
        raise ValidationError(
            "adaptive_v2_ready identity differs from the canonical epoch command"
        )
    return event


def _validate_process_lifecycle(
    manifest: Manifest,
    streams: Mapping[tuple[str, str], Sequence[analysis.StructuredEvent]],
    *,
    manager_ready: analysis.StructuredEvent,
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
        if key[0] == "adaptation_manager":
            if not (
                manager_ready.source_sequence
                < stopping[0].source_sequence
                < stopped[0].source_sequence
                and manager_ready.timestamp_ns
                <= stopping[0].timestamp_ns
                <= stopped[0].timestamp_ns
            ):
                raise ValidationError(
                    "manager lifecycle is not ordered after convergence readiness"
                )
        elif not (
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


def _validate_crash_configuration_boundary(
    manifest: Manifest,
    epochs: EpochDocument,
    streams: Mapping[tuple[str, str], Sequence[analysis.StructuredEvent]],
    final_crash_request_ns: int,
) -> int:
    boundary = manifest.crash_configuration_boundary
    initial_tree = epochs.initial.trees[6]
    if (
        boundary.epoch_number != initial_tree.epoch_number
        or boundary.tree_id != initial_tree.tree_id
        or boundary.root_replica != initial_tree.leader
        or boundary.epoch_digest != initial_tree.epoch_digest
    ):
        raise ValidationError(
            "crash boundary does not match the exact epoch-0 root 6 definition"
        )
    if initial_tree.fanout != 2 or initial_tree.members != (6, 0, 1, 2, 3, 4, 5):
        raise ValidationError("crash boundary does not use the frozen tree-6 BFS")
    positions = tuple(
        initial_tree.members.index(replica) for replica in CRASHED_REPLICAS
    )
    if len(set(positions)) != 2 or any(
        position == 0
        or initial_tree.fanout * position + 1 >= len(initial_tree.members)
        for position in positions
    ):
        raise ValidationError(
            "crashed replicas are not distinct non-root internal tree-6 replicas"
        )
    timestamps: list[int] = []
    observer_boundary_sequence: int | None = None
    for replica, reference in zip(MEMBERSHIP, boundary.evidence):
        events = streams[("replica", reference.source_id)]
        event = _event_at_sequence(events, reference.source_sequence)
        if event.timestamp_ns != reference.timestamp_ns:
            raise ValidationError(
                f"{reference.source_id} crash-boundary timestamp mismatch"
            )
        if event.event_type != "adaptive.configuration_active":
            raise ValidationError(
                f"{reference.source_id} crash boundary does not reference configuration activation"
            )
        payload = event.payload
        _exact_fields(
            payload,
            _CONFIGURATION_ACTIVE_FIELDS,
            "adaptive.configuration_active payload",
        )
        payload_epoch = _integer(
            payload["epoch_number"],
            "adaptive.configuration_active.epoch_number",
            maximum=analysis.UINT32_MAX,
        )
        payload_tree = _integer(
            payload["tree_id"],
            "adaptive.configuration_active.tree_id",
            maximum=analysis.UINT32_MAX,
        )
        payload_observer = _integer(
            payload["observer_replica"],
            "adaptive.configuration_active.observer_replica",
            maximum=analysis.UINT32_MAX,
        )
        payload_root_signers = _integer(
            payload["root_signer_count"],
            "adaptive.configuration_active.root_signer_count",
        )
        payload_quorum = _integer(
            payload["global_quorum"],
            "adaptive.configuration_active.global_quorum",
            minimum=1,
        )
        payload_digest = _hash(
            payload["epoch_digest"],
            "adaptive.configuration_active.epoch_digest",
        )
        if (
            payload_epoch != boundary.epoch_number
            or payload_tree != boundary.tree_id
            or payload_digest != boundary.epoch_digest
            or payload_observer != replica
            or payload_quorum != QUORUM
            or payload["block_hash"] is not None
            or payload["context_generation"] is not None
            or payload["wait_exempt_signers"] != []
            or payload["accepted_signers"] != []
            or payload["absent_direct_children"] != []
            or payload["missing_optional_signers"] != []
            or payload["required_branch_gaps"] != []
            or payload_root_signers != 0
            or payload["rejection_reason"] is not None
        ):
            raise ValidationError(
                f"{reference.source_id} does not prove canonical epoch-0 root 6 active"
            )
        timestamps.append(event.timestamp_ns)
        if any(
            later.event_type == "adaptive.configuration_active"
            and later.source_sequence > reference.source_sequence
            and later.timestamp_ns <= final_crash_request_ns
            for later in events
        ):
            raise ValidationError(
                "intervening configuration activation before crash request on "
                f"{reference.source_id}"
            )
        if reference.source_id == manifest.authoritative_observer:
            observer_boundary_sequence = reference.source_sequence
    if max(timestamps) - min(timestamps) > (
        int(manifest.runtime["aggregation_timeout_ms"]) * 1_000_000
    ):
        raise ValidationError(
            "common root-6 activation spread exceeds the aggregation timeout"
        )
    if max(timestamps) >= final_crash_request_ns:
        raise ValidationError("tree-6 boundary is not ordered before crash request")
    if observer_boundary_sequence is None:
        raise ValidationError("crash boundary lacks authoritative observer evidence")

    observer_events = streams[("replica", manifest.authoritative_observer)]
    for event in observer_events:
        if (
            event.event_type != "block.committed"
            or event.source_sequence <= observer_boundary_sequence
            or event.timestamp_ns > final_crash_request_ns
        ):
            continue
        proof = _object(
            event.payload.get("decision_proof"),
            "pre-crash authoritative decision proof",
        )
        if (proof.get("epoch_number"), proof.get("tree_id")) != (0, 6):
            raise ValidationError(
                "authoritative root changed after root-6 boundary before crash request"
            )
    return max(timestamps)


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
) -> tuple[int, int, int]:
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
    leader_activation_grace_ns = _integer(
        manifest.runtime["leader_activation_grace_ms"],
        "runtime.leader_activation_grace_ms",
        minimum=1,
    ) * 1_000_000
    if max(activation_times.values()) - min(activation_times.values()) > (
        leader_activation_grace_ns
    ):
        raise ValidationError(
            "survivor activation spread exceeds frozen leader activation grace"
        )
    return (
        observer_command_ns,
        observer_activation_ns,
        max(activation_times.values()),
    )


def _compressed(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        if not result or result[-1] != value:
            result.append(value)
    return result


def _contains_contiguous(values: Sequence[int], expected: Sequence[int]) -> bool:
    width = len(expected)
    return any(list(values[index : index + width]) == list(expected) for index in range(len(values) - width + 1))


def _contains_cyclic_cycle(
    values: Sequence[int], expected: Sequence[int]
) -> bool:
    return any(
        _contains_contiguous(values, (*expected[offset:], *expected[:offset]))
        for offset in range(len(expected))
    )


def _rich_commit_observations(
    events: Sequence[analysis.StructuredEvent], replica: int
) -> dict[int, CommitObservation]:
    observations: dict[int, CommitObservation] = {}
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
            parent = _hash(parent, "commit.parent_hash")
        transaction_count = _integer(
            payload["transaction_count"], "commit.transaction_count"
        )
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
        commit_batch_index = _integer(
            payload["commit_batch_index"], "commit.commit_batch_index"
        )
        observation = CommitObservation(
            block_hash=block_hash,
            parent_hash=parent,
            transaction_count=transaction_count,
            commit_batch_index=commit_batch_index,
            source_sequence=event.source_sequence,
            timestamp_ns=event.timestamp_ns,
        )
        previous = observations.get(height)
        if (
            previous is not None
            and previous.shared_identity != observation.shared_identity
        ):
            raise ValidationError(
                f"replica-{replica} has conflicting rich commits at height {height}"
            )
        previous_height = hashes.get(block_hash)
        if previous_height is not None and previous_height != height:
            raise ValidationError(
                f"replica-{replica} reuses one rich commit hash at two heights"
            )
        if previous is None:
            observations[height] = observation
        hashes[block_hash] = height
    return observations


def _commit_witness_observations(
    events: Sequence[analysis.StructuredEvent], replica: int
) -> dict[int, CommitObservation]:
    observations: dict[int, CommitObservation] = {}
    hashes: dict[str, int] = {}
    for event in events:
        if event.event_type != "block.commit_observed":
            continue
        payload = event.payload
        _exact_fields(
            payload,
            _COMMIT_OBSERVED_FIELDS,
            "block.commit_observed payload",
        )
        height = _integer(
            payload["block_height"],
            "commit witness.block_height",
            minimum=1,
        )
        block_hash = _hash(
            payload["block_hash"],
            "commit witness.block_hash",
        )
        parent = payload["parent_hash"]
        if parent is not None:
            parent = _hash(parent, "commit witness.parent_hash")
        transaction_count = _integer(
            payload["transaction_count"],
            "commit witness.transaction_count",
        )
        commit_batch_index = _integer(
            payload["commit_batch_index"],
            "commit witness.commit_batch_index",
        )
        observation = CommitObservation(
            block_hash=block_hash,
            parent_hash=parent,
            transaction_count=transaction_count,
            commit_batch_index=commit_batch_index,
            source_sequence=event.source_sequence,
            timestamp_ns=event.timestamp_ns,
        )
        previous = observations.get(height)
        if (
            previous is not None
            and previous.shared_identity != observation.shared_identity
        ):
            raise ValidationError(
                f"replica-{replica} has conflicting commit witnesses at height "
                f"{height}"
            )
        previous_height = hashes.get(block_hash)
        if previous_height is not None and previous_height != height:
            raise ValidationError(
                f"replica-{replica} reuses one commit witness hash at two heights"
            )
        if previous is None:
            observations[height] = observation
        hashes[block_hash] = height
    return observations


def _commit_observations(
    events: Sequence[analysis.StructuredEvent], replica: int
) -> dict[int, str]:
    witnesses = _commit_witness_observations(events, replica)
    rich_commits = _rich_commit_observations(events, replica)
    for height, rich_commit in rich_commits.items():
        witness = witnesses.get(height)
        if witness is None:
            raise IncompleteRun(
                f"replica-{replica} rich commit at height {height} has no "
                "commit witness"
            )
        if witness.shared_identity != rich_commit.shared_identity:
            raise ValidationError(
                f"replica-{replica} rich commit disagrees with commit witness "
                f"at height {height}"
            )
        if witness.source_sequence >= rich_commit.source_sequence:
            raise ValidationError(
                f"replica-{replica} commit witness does not precede rich commit "
                f"at height {height}"
            )
    if replica == analysis.AUTHORITATIVE_OBSERVER:
        for height in sorted(witnesses):
            if height not in rich_commits:
                raise IncompleteRun(
                    f"replica-{replica} commit witness at height {height} has "
                    "no rich commit"
                )
    return {
        height: observation.block_hash
        for height, observation in witnesses.items()
    }


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


def _validate_epoch_transition(
    commits: Sequence[analysis.CommitEvent],
    *,
    activation_height: int,
    activation_ns: int,
) -> analysis.CommitEvent:
    """Accept one predecessor prefix and return the first successor commit."""
    first_successor: analysis.CommitEvent | None = None
    for commit in commits:
        if commit.height <= activation_height:
            if commit.epoch_number != 0:
                raise ValidationError(
                    f"commit height {commit.height} activates epoch 1 before "
                    "the predecessor activation-height commit"
                )
            continue
        if commit.epoch_number == 0:
            if first_successor is not None:
                raise ValidationError(
                    f"predecessor commit height {commit.height} follows a "
                    "successor commit"
                )
            continue
        if commit.epoch_number == 1:
            if commit.timestamp_ns < activation_ns:
                raise ValidationError(
                    f"successor commit height {commit.height} precedes the "
                    "common activation event"
                )
            if first_successor is None:
                first_successor = commit
            continue
        raise ValidationError(
            f"commit height {commit.height} uses unexpected epoch "
            f"{commit.epoch_number}"
        )
    if first_successor is None:
        raise IncompleteRun("no successor commit was observed after activation")
    return first_successor


def _validate_commits(
    manifest: Manifest,
    epochs: EpochDocument,
    streams: Mapping[tuple[str, str], Sequence[analysis.StructuredEvent]],
    texts: Mapping[tuple[str, str], str],
    crash_ns: int,
    command_ns: int,
    activation_ns: int,
) -> tuple[
    analysis.ThroughputAnalysis,
    int,
    Mapping[str, int],
    Mapping[str, int],
    int,
    int,
    int,
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
    command = epochs.successor.command
    assert command is not None
    activation_height = int(command["activation_height"])
    first_successor = _validate_epoch_transition(
        commits,
        activation_height=activation_height,
        activation_ns=activation_ns,
    )
    first_successor_observations = {
        replica: _commit_witness_observations(
            streams[("replica", f"replica-{replica}")], replica
        )
        for replica in SURVIVING_REPLICAS
    }
    common_successor_timestamps = [first_successor.timestamp_ns]
    for replica, observations in first_successor_observations.items():
        witness = observations.get(first_successor.height)
        if witness is None:
            raise IncompleteRun(
                f"replica-{replica} is missing first successor height "
                f"{first_successor.height}"
            )
        if witness.block_hash != first_successor.block_hash:
            raise ValidationError(
                "survivor commit disagreement at first successor height "
                f"{first_successor.height}"
            )
        common_successor_timestamps.append(witness.timestamp_ns)
    first_common_successor_ns = max(common_successor_timestamps)
    activation_to_successor_ns = first_common_successor_ns - activation_ns
    if activation_to_successor_ns > manifest.maximum_activation_to_successor_ns:
        raise ValidationError(
            "first common successor commit exceeds the frozen "
            f"{manifest.maximum_activation_to_successor_ns / 1_000_000_000:g}s "
            "activation-to-successor maximum"
        )
    if activation_ns > (
        analysis.UINT64_MAX - manifest.minimum_post_activation_grace_ns
    ):
        raise ValidationError(
            "minimum post-activation grace overflows the monotonic clock"
        )
    minimum_post_start_ns = (
        activation_ns + manifest.minimum_post_activation_grace_ns
    )
    post_start_ns = max(
        minimum_post_start_ns,
        first_common_successor_ns,
    )
    if not crash_ns < command_ns <= activation_ns < post_start_ns < manifest.end_ns:
        raise ValidationError(
            "boundaries must satisfy crash < command <= activation < post < end"
        )
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
    activation_commit = commits_by_height.get(activation_height)
    if activation_commit is None:
        raise IncompleteRun(
            f"authoritative observer lacks activation height {activation_height}"
        )
    if activation_commit.timestamp_ns > activation_ns:
        raise ValidationError("epoch activation event precedes its activating commit")
    pre_crash = [
        commit
        for commit in commits
        if manifest.baseline_start_ns <= commit.timestamp_ns < crash_ns
    ]
    if any(commit.epoch_number != 0 for commit in pre_crash):
        raise ValidationError("pre-crash commits must use epoch 0")
    pre_crash_by_replica = {
        replica: _commit_observations(
            streams[("replica", f"replica-{replica}")], replica
        )
        for replica in MEMBERSHIP
    }
    by_replica = {
        replica: pre_crash_by_replica[replica]
        for replica in SURVIVING_REPLICAS
    }
    for commit in pre_crash:
        for replica in SURVIVING_REPLICAS:
            observed_hash = pre_crash_by_replica[replica].get(commit.height)
            if observed_hash is None:
                raise IncompleteRun(
                    f"replica-{replica} is missing authoritative height "
                    f"{commit.height}"
                )
            if observed_hash != commit.block_hash:
                raise ValidationError(
                    f"survivor commit disagreement at height {commit.height}"
                )
    common_pre_crash = [
        commit
        for commit in pre_crash
        if all(
            pre_crash_by_replica[replica].get(commit.height)
            == commit.block_hash
            for replica in MEMBERSHIP
        )
    ]
    initial_leaders = _compressed(
        commit.leader_replica for commit in common_pre_crash
    )
    if not _contains_contiguous(initial_leaders, MEMBERSHIP):
        raise ValidationError(
            "crash must follow a complete common epoch-0 root cycle 0..6"
        )

    post = [
        commit
        for commit in commits
        if post_start_ns <= commit.timestamp_ns < manifest.end_ns
    ]
    if any(commit.epoch_number != 1 for commit in post):
        raise ValidationError(
            "post-measurement commits must use only successor epoch 1"
        )
    post_leaders = _compressed(commit.leader_replica for commit in post)
    successor_root_cycle = tuple(
        tree.leader for tree in epochs.successor.trees
    )
    if not _contains_contiguous(post_leaders, successor_root_cycle):
        raise IncompleteRun(
            "post-measurement commits do not contain one complete ranked "
            "successor root cycle"
        )

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
    return (
        throughput,
        len(common),
        complete_bucket_counts,
        maximum_stalls,
        minimum_post_start_ns,
        first_common_successor_ns,
        post_start_ns,
    )


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
    evidence_window_resets = 0
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
            if (
                not manifest.transition_requests
                or ingestion != 1
                or event.timestamp_ns < post_start_ns
                or evidence_window_resets
                >= len(manifest.transition_requests) - 1
            ):
                raise ValidationError("reputation ingestion_sequence must increase")
            evidence_window_resets += 1
            previous_ingestion = 0
            previous_cutoff = 0
            scores = [0] * 7
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
    if manifest.transition_requests and evidence_window_resets != (
        len(manifest.transition_requests) - 1
    ):
        raise IncompleteRun(
            "reputation evidence did not open every authenticated successor window"
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


def _validate_recurring_command_and_activation(
    manifest: Manifest,
    epochs: EpochDocument,
    streams: Mapping[tuple[str, str], Sequence[analysis.StructuredEvent]],
    crash_complete_ns: int,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    transitions = epochs.epochs[1:]
    expected_commands = [dict(epoch.command or {}) for epoch in transitions]
    command_times_by_transition: list[dict[int, int]] = [
        {} for _ in transitions
    ]
    activation_times_by_transition: list[dict[int, int]] = [
        {} for _ in transitions
    ]
    for replica in CRASHED_REPLICAS:
        events = streams[("replica", f"replica-{replica}")]
        if any(
            event.event_type in ("epoch.command_committed", "epoch.activated")
            for event in events
        ):
            raise ValidationError(
                f"crashed replica-{replica} emitted transition evidence after exit"
            )
    for replica in SURVIVING_REPLICAS:
        events = streams[("replica", f"replica-{replica}")]
        command_events = [
            event for event in events if event.event_type == "epoch.command_committed"
        ]
        if len(command_events) < len(transitions):
            raise IncompleteRun(
                f"replica-{replica} is missing a recurring epoch command"
            )
        if len(command_events) > len(transitions):
            raise ValidationError(
                f"replica-{replica} emitted an unrequested epoch command"
            )
        activation_events = [
            event for event in events if event.event_type == "epoch.activated"
        ]
        if len(activation_events) > len(transitions):
            raise ValidationError(
                f"replica-{replica} emitted an unrequested epoch activation"
            )
        for index, (epoch, expected_command, command_event) in enumerate(
            zip(transitions, expected_commands, command_events)
        ):
            command = _parse_command_payload(command_event.payload)
            if command != expected_command:
                raise ValidationError(
                    f"replica-{replica} transition command differs from the epoch chain"
                )
            if index == 0 and command_event.timestamp_ns <= crash_complete_ns:
                raise ValidationError("first epoch command must follow both crash exits")
            command_times_by_transition[index][replica] = command_event.timestamp_ns
            candidates = [
                event
                for event in activation_events
                if event.payload.get("epoch_number") == epoch.epoch_number
            ]
            if not candidates:
                raise IncompleteRun(
                    f"replica-{replica} has no activation for epoch {epoch.epoch_number}"
                )
            if len(candidates) != 1:
                raise ValidationError(
                    f"replica-{replica} emitted duplicate epoch {epoch.epoch_number} activation"
                )
            activation = candidates[0]
            _exact_fields(
                activation.payload, _EPOCH_EVENT_FIELDS, "epoch.activated payload"
            )
            if (
                activation.payload["epoch_number"] != epoch.epoch_number
                or activation.payload["epoch_digest"] != epoch.epoch_digest
                or activation.payload["activation_height"]
                != expected_command["activation_height"]
                or activation.payload["tree_id"]
                not in {tree.tree_id for tree in epoch.trees}
            ):
                raise ValidationError(
                    f"replica-{replica} activation does not identify epoch {epoch.epoch_number}"
                )
            if activation.timestamp_ns < command_event.timestamp_ns:
                raise ValidationError("epoch activation precedes its committed command")
            activation_times_by_transition[index][replica] = activation.timestamp_ns

    grace_ns = _integer(
        manifest.runtime["leader_activation_grace_ms"],
        "runtime.leader_activation_grace_ms",
        minimum=1,
    ) * 1_000_000
    observer_commands: list[int] = []
    observer_activations: list[int] = []
    latest_activations: list[int] = []
    for command_times, activation_times in zip(
        command_times_by_transition, activation_times_by_transition
    ):
        if max(activation_times.values()) - min(activation_times.values()) > grace_ns:
            raise ValidationError(
                "survivor activation spread exceeds frozen leader activation grace"
            )
        observer_commands.append(command_times[analysis.AUTHORITATIVE_OBSERVER])
        observer_activations.append(
            activation_times[analysis.AUTHORITATIVE_OBSERVER]
        )
        latest_activations.append(max(activation_times.values()))
    if any(
        right <= left
        for left, right in zip(observer_commands, observer_commands[1:])
    ):
        raise ValidationError("recurring epoch commands are not ordered")
    return (
        tuple(observer_commands),
        tuple(observer_activations),
        tuple(latest_activations),
    )


def _validate_recurring_commits(
    manifest: Manifest,
    epochs: EpochDocument,
    streams: Mapping[tuple[str, str], Sequence[analysis.StructuredEvent]],
    texts: Mapping[tuple[str, str], str],
    crash_ns: int,
    command_times: Sequence[int],
    activation_times: Sequence[int],
) -> tuple[
    analysis.ThroughputAnalysis,
    int,
    Mapping[str, int],
    Mapping[str, int],
    tuple[int, ...],
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
    if not commits:
        raise IncompleteRun("authoritative observer has no commit events")
    epoch_positions = {epoch.epoch_number: index for index, epoch in enumerate(epochs.epochs)}
    previous_position = -1
    first_timestamp_by_epoch: dict[int, int] = {}
    for commit in commits:
        position = epoch_positions.get(commit.epoch_number)
        if position is None:
            raise ValidationError(
                f"commit height {commit.height} uses an unknown epoch"
            )
        if position < previous_position:
            raise ValidationError("authoritative commits regress to a retired epoch")
        if position > previous_position + 1:
            raise ValidationError("authoritative commits skip an epoch")
        if position > 0 and commit.timestamp_ns < activation_times[position - 1]:
            raise ValidationError(
                f"epoch {commit.epoch_number} commit precedes common activation"
            )
        previous_position = position
        first_timestamp_by_epoch.setdefault(commit.epoch_number, commit.timestamp_ns)

    survivor_observations = {
        replica: _commit_observations(
            streams[("replica", f"replica-{replica}")], replica
        )
        for replica in SURVIVING_REPLICAS
    }
    common_first_ns: list[int] = []
    for transition_index, epoch in enumerate(epochs.epochs[1:]):
        successor_commits = [
            commit for commit in commits if commit.epoch_number == epoch.epoch_number
        ]
        if len(successor_commits) < 2:
            raise IncompleteRun(
                f"epoch {epoch.epoch_number} lacks two post-activation authoritative commits"
            )
        first_two = successor_commits[:2]
        first_common_times = [first_two[0].timestamp_ns]
        for commit in first_two:
            for replica, observations in survivor_observations.items():
                observed_hash = observations.get(commit.height)
                if observed_hash is None:
                    raise IncompleteRun(
                        f"replica-{replica} is missing post-activation height {commit.height}"
                    )
                if observed_hash != commit.block_hash:
                    raise ValidationError(
                        f"survivor commit disagreement at height {commit.height}"
                    )
                if commit is first_two[0]:
                    witness = _commit_witness_observations(
                        streams[("replica", f"replica-{replica}")], replica
                    )[commit.height]
                    first_common_times.append(witness.timestamp_ns)
        first_common = max(first_common_times)
        common_first_ns.append(first_common)
        if first_common - activation_times[transition_index] > (
            manifest.maximum_activation_to_successor_ns
        ):
            raise ValidationError(
                f"epoch {epoch.epoch_number} first common commit exceeds the activation deadline"
            )
        if transition_index + 1 < len(command_times) and not (
            first_common < command_times[transition_index + 1]
        ):
            raise ValidationError(
                "next transition command precedes a common predecessor commit"
            )

    for activation_ns in activation_times:
        if (
            activation_ns
            > analysis.UINT64_MAX
            - manifest.minimum_post_activation_grace_ns
        ):
            raise ValidationError(
                "post-activation throughput boundary overflows monotonic time"
            )
    windows = manifest.throughput_windows
    baseline_window = analysis.PhaseWindow(
        "baseline",
        epochs.epochs[0].epoch_number,
        manifest.baseline_start_ns,
        crash_ns,
    )
    degraded_window = analysis.PhaseWindow(
        "degraded",
        epochs.epochs[0].epoch_number,
        crash_ns,
        command_times[0],
    )
    containment_start_ns = max(
        activation_times[0] + manifest.minimum_post_activation_grace_ns,
        common_first_ns[0],
    )
    if manifest.profile_identity in PAIRED_PROFILE_IDS:
        containment_duration_ns = (
            manifest.required_bucket_counts["containment"]
            * analysis.BUCKET_WIDTH_NS
        )
        containment_end_ns = containment_start_ns + containment_duration_ns
        containment_window = analysis.PhaseWindow(
            "containment",
            epochs.epochs[1].epoch_number,
            containment_start_ns,
            containment_end_ns,
        )
        if manifest.profile_identity == PAIRED_ADAPTIVE_PROFILE_ID:
            if containment_end_ns > command_times[1]:
                raise ValidationError(
                    "paired containment window does not finish before the "
                    "epoch-2 command"
                )
            optimized_start_ns = max(
                activation_times[1]
                + manifest.minimum_post_activation_grace_ns,
                common_first_ns[1],
            )
            optimized_end_ns = optimized_start_ns + (
                manifest.required_bucket_counts["optimized"]
                * analysis.BUCKET_WIDTH_NS
            )
            expected_windows = (
                baseline_window,
                degraded_window,
                containment_window,
                analysis.PhaseWindow(
                    "optimized",
                    epochs.epochs[2].epoch_number,
                    optimized_start_ns,
                    optimized_end_ns,
                ),
            )
            expected_end_ns = optimized_end_ns
        else:
            control_late_start_ns = (
                containment_start_ns
                + manifest.final_measurement_delay_ns
            )
            control_late_end_ns = control_late_start_ns + (
                manifest.required_bucket_counts["control_late"]
                * analysis.BUCKET_WIDTH_NS
            )
            expected_windows = (
                baseline_window,
                degraded_window,
                containment_window,
                analysis.PhaseWindow(
                    "control_late",
                    epochs.epochs[1].epoch_number,
                    control_late_start_ns,
                    control_late_end_ns,
                ),
            )
            expected_end_ns = control_late_end_ns
        if manifest.end_ns != expected_end_ns:
            raise ValidationError(
                "paired run end does not match its fixed final measurement window"
            )
    else:
        expected_windows = (
            baseline_window,
            degraded_window,
            analysis.PhaseWindow(
                "containment",
                epochs.epochs[1].epoch_number,
                containment_start_ns,
                command_times[1],
            ),
            analysis.PhaseWindow(
                "optimized",
                epochs.epochs[2].epoch_number,
                max(
                    activation_times[1]
                    + manifest.minimum_post_activation_grace_ns,
                    common_first_ns[1],
                ),
                manifest.end_ns,
            ),
        )
    if windows != expected_windows:
        raise ValidationError(
            "four throughput windows do not match their exact causal boundaries"
        )
    try:
        throughput = analysis.analyze_throughput(commits, windows)
    except analysis.AnalysisError as exc:
        raise ValidationError(f"throughput analysis failed: {exc}") from exc
    counts = {phase: 0 for phase in manifest.required_bucket_counts}
    for bucket in throughput.buckets:
        if bucket.phase not in counts:
            raise ValidationError(f"unknown throughput phase: {bucket.phase}")
        if bucket.end_ns - bucket.start_ns == analysis.BUCKET_WIDTH_NS:
            counts[bucket.phase] += 1
    for phase, required in manifest.required_bucket_counts.items():
        if (
            counts[phase] != required
            if manifest.profile_identity in PAIRED_PROFILE_IDS
            else counts[phase] < required
        ):
            requirement = (
                f"requires exactly {required}"
                if manifest.profile_identity in PAIRED_PROFILE_IDS
                else f"requires {required}"
            )
            raise IncompleteRun(
                f"{phase} phase has {counts[phase]} complete raw buckets; "
                f"{requirement}"
            )
    maximum_stalls = _maximum_commit_stalls(
        commits,
        tuple((window.phase, window.start_ns, window.end_ns) for window in windows),
    )
    for phase, stall_ns in maximum_stalls.items():
        limit = (
            manifest.degraded_maximum_stall_ns
            if phase == "degraded"
            else manifest.maximum_stall_ns
        )
        if stall_ns > limit:
            raise ValidationError(
                f"{phase} authoritative commit stall exceeds the frozen maximum"
            )
    for transition_index, epoch in enumerate(epochs.epochs[1:]):
        command = _object(epoch.command, "successor command")
        by_height = {commit.height: commit for commit in commits}
        command_commit = by_height.get(int(command["command_block_height"]))
        if command_commit is None:
            raise IncompleteRun(
                f"authoritative observer lacks command height {command['command_block_height']}"
            )
        if command_commit.block_hash != command["command_block_hash"]:
            raise ValidationError("committed command hash differs from its consensus block")
        if command_commit.timestamp_ns > command_times[transition_index]:
            raise ValidationError("epoch command event precedes its committed block")

    pre_crash = [
        commit
        for commit in commits
        if manifest.baseline_start_ns <= commit.timestamp_ns < crash_ns
    ]
    all_replica_observations = {
        replica: _commit_observations(
            streams[("replica", f"replica-{replica}")], replica
        )
        for replica in MEMBERSHIP
    }
    if not _contains_contiguous(
        _compressed(commit.leader_replica for commit in pre_crash), MEMBERSHIP
    ):
        raise ValidationError("crash must follow a complete common epoch-0 root cycle 0..6")
    for commit in pre_crash:
        for replica in MEMBERSHIP:
            if all_replica_observations[replica].get(commit.height) != commit.block_hash:
                raise IncompleteRun(
                    f"replica-{replica} is missing common baseline height {commit.height}"
                )
    for window in windows[2:]:
        phase_commits = [
            commit
            for commit in commits
            if window.start_ns <= commit.timestamp_ns < window.end_ns
        ]
        epoch = epochs.epochs[window.epoch_number]
        expected_roots = tuple(tree.leader for tree in epoch.trees)
        if not _contains_cyclic_cycle(
            _compressed(commit.leader_replica for commit in phase_commits),
            expected_roots,
        ):
            raise IncompleteRun(
                f"{window.phase} phase lacks one complete ranked root cycle"
            )
    authoritative_window = {
        commit.height: commit.block_hash
        for commit in commits
        if any(
            window.start_ns <= commit.timestamp_ns < window.end_ns
            for window in windows
        )
    }
    for height, block_hash in authoritative_window.items():
        for replica, observations in survivor_observations.items():
            if observations.get(height) != block_hash:
                raise IncompleteRun(
                    f"replica-{replica} is missing authoritative height {height}"
                )
    common = set.intersection(
        *(set(observations) for observations in survivor_observations.values())
    )
    medians = throughput.medians
    assert medians.containment_tps is not None
    if manifest.profile_identity == PAIRED_CONTROL_PROFILE_ID:
        assert medians.control_late_tps is not None
        paired_medians = (
            medians.baseline_tps,
            medians.degraded_tps,
            medians.containment_tps,
            medians.control_late_tps,
        )
    else:
        assert medians.optimized_tps is not None
        paired_medians = (
            medians.baseline_tps,
            medians.degraded_tps,
            medians.containment_tps,
            medians.optimized_tps,
        )
    if manifest.profile_identity in PAIRED_PROFILE_IDS:
        if any(value <= 0 for value in paired_medians):
            raise ValidationError(
                "paired throughput metrics require four positive phase medians"
            )
    else:
        if medians.degraded_tps <= 0 or medians.containment_tps <= 0:
            raise ValidationError(
                "recurring throughput ratios require positive medians"
            )
        assert medians.optimized_tps is not None
        containment_ratio = medians.containment_tps / medians.degraded_tps
        optimized_ratio = medians.optimized_tps / medians.containment_tps
        if containment_ratio < float(
            manifest.minimum_containment_to_degraded_ratio
        ):
            raise ValidationError(
                "containment throughput ratio is below the frozen minimum"
            )
        if optimized_ratio < float(
            manifest.minimum_optimized_to_containment_ratio
        ):
            raise ValidationError(
                "optimized throughput ratio is below the frozen minimum"
            )
    for bucket in throughput.buckets:
        if sum(bucket.leader_transactions) != bucket.transaction_count:
            raise ValidationError("leader transaction columns do not conserve aggregate")
        if sum(bucket.leader_tps) != bucket.aggregate_tps:
            raise ValidationError("leader TPS columns do not conserve aggregate")
    return throughput, len(common), counts, maximum_stalls, tuple(common_first_ns)


def evaluate(manifest_path: Path, epochs_path: Path) -> Evaluation:
    manifest = load_manifest(manifest_path)
    epochs = load_epochs(epochs_path)
    if manifest.transition_requests:
        if len(epochs.epochs) != len(manifest.transition_requests) + 1:
            raise IncompleteRun(
                "epoch chain does not contain one successor per transition request"
            )
        if [tree.leader for tree in epochs.initial.trees] != manifest.runtime[
            "epoch0_roots"
        ]:
            raise ValidationError("epoch-0 roots differ from manifest.runtime")
        for request, epoch in zip(
            manifest.transition_requests, epochs.epochs[1:]
        ):
            command = _object(epoch.command, "recurring successor command")
            if command["activation_delay_blocks"] != manifest.runtime[
                "activation_delay_blocks"
            ]:
                raise ValidationError(
                    "committed activation delay differs from manifest.runtime"
                )
            if request["policy_intent"] == "fault_containment":
                parameters = _object(
                    request["policy_parameters"], "containment policy parameters"
                )
                predecessor = epochs.epochs[
                    int(request["predecessor_epoch_number"])
                ]
                expected_baseline_roots = [
                    {
                        "tree_id": tree.tree_id,
                        "replica_id": tree.leader,
                    }
                    for tree in predecessor.trees[:QUORUM]
                ]
                actual_baseline_roots = [
                    dict(item)
                    for item in _list(
                        parameters.get("containment_baseline_roots"),
                        "containment baseline roots",
                    )
                ]
                if not _json_values_equal(
                    actual_baseline_roots, expected_baseline_roots
                ):
                    raise ValidationError(
                        "containment baseline roots do not describe the predecessor layout"
                    )
        streams, texts = _read_source_events(manifest)
        manager_ready = _validate_manager_convergence_ready(
            manifest, epochs, streams
        )
        _validate_recurring_transition_bundles(
            manifest,
            epochs,
            streams[("adaptation_manager", manifest.manager_source_id)],
        )
        _validate_process_lifecycle(
            manifest,
            streams,
            manager_ready=manager_ready,
        )
        crash_markers = _validate_crash_markers(manifest, streams)
        crash_ns = min(crash_markers)
        crash_complete_ns = max(
            marker.confirmed_ns for marker in manifest.crash_markers
        )
        _validate_crash_configuration_boundary(
            manifest,
            epochs,
            streams,
            max(crash_markers),
        )
        command_times, activation_times, latest_activations = (
            _validate_recurring_command_and_activation(
                manifest, epochs, streams, crash_complete_ns
            )
        )
        ready_events = [
            event
            for event in streams[
                ("adaptation_manager", manifest.manager_source_id)
            ]
            if event.event_type == "adaptive_v2_ready"
        ]
        ready_by_successor = {
            int(event.payload["identity"]["successor_epoch_number"]): event
            for event in ready_events
        }
        manager_events = streams[
            ("adaptation_manager", manifest.manager_source_id)
        ]
        snapshots_by_ordinal = {
            int(event.payload["cycle_ordinal"]): event
            for event in manager_events
            if event.event_type == "adaptive_v2_evidence_snapshot"
        }
        terminals_by_ordinal = {
            int(event.payload["cycle_ordinal"]): event
            for event in manager_events
            if event.event_type == "adaptive_v2_session_terminal"
        }
        for index, (command_ns, activation_ns, latest_ns) in enumerate(
            zip(command_times, activation_times, latest_activations)
        ):
            successor_number = epochs.epochs[index + 1].epoch_number
            ready = ready_by_successor[successor_number]
            if not (
                (crash_ns if index == 0 else latest_activations[index - 1])
                < command_ns
                <= activation_ns
                <= latest_ns
                <= ready.timestamp_ns
                < manifest.end_ns
            ):
                raise ValidationError(
                    f"transition to epoch {successor_number} has invalid causal boundaries"
                )
            if index > 0:
                residency_ns = int(
                    manifest.transition_requests[index][
                        "minimum_predecessor_residency_ms"
                    ]
                ) * 1_000_000
                predecessor_terminal_ns = terminals_by_ordinal[
                    index - 1
                ].timestamp_ns
                if predecessor_terminal_ns > analysis.UINT64_MAX - residency_ns:
                    raise ValidationError(
                        "minimum predecessor residency overflows monotonic time"
                    )
                earliest_transition_ns = predecessor_terminal_ns + residency_ns
                snapshot = snapshots_by_ordinal[index]
                if (
                    snapshot.timestamp_ns < earliest_transition_ns
                    or command_ns < earliest_transition_ns
                ):
                    raise ValidationError(
                        f"transition to epoch {successor_number} violates its "
                        "minimum predecessor residency"
                    )
        (
            throughput,
            common_heights,
            complete_bucket_counts,
            maximum_stalls,
            first_common_successors,
        ) = _validate_recurring_commits(
            manifest,
            epochs,
            streams,
            texts,
            crash_ns,
            command_times,
            activation_times,
        )
        containment_start_ns = manifest.throughput_windows[2].start_ns
        reputation, final_scores = _validate_reputation(
            manifest,
            streams,
            crash_ns,
            command_times[0],
            containment_start_ns,
        )
        return Evaluation(
            manifest=manifest,
            epochs=epochs,
            crash_markers_ns=crash_markers,
            crash_ns=crash_ns,
            command_ns=command_times[0],
            activation_ns=activation_times[0],
            minimum_post_start_ns=(
                activation_times[0]
                + manifest.minimum_post_activation_grace_ns
            ),
            first_common_successor_ns=first_common_successors[0],
            post_start_ns=containment_start_ns,
            throughput=throughput,
            reputation=reputation,
            final_scores=final_scores,
            common_commit_heights=common_heights,
            complete_bucket_counts=complete_bucket_counts,
            maximum_stall_ns_by_phase=maximum_stalls,
        )
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
    if set(tree.leader for tree in epochs.successor.trees) != set(
        manifest.runtime["successor_roots"]
    ):
        raise ValidationError("successor root set differs from manifest.runtime")
    streams, texts = _read_source_events(manifest)
    manager_ready = _validate_manager_convergence_ready(
        manifest, epochs, streams
    )
    _validate_process_lifecycle(
        manifest,
        streams,
        manager_ready=manager_ready,
    )
    manager_ready_ns = manager_ready.timestamp_ns
    crash_markers = _validate_crash_markers(manifest, streams)
    crash_ns = min(crash_markers)
    crash_complete_ns = max(marker.confirmed_ns for marker in manifest.crash_markers)
    _validate_crash_configuration_boundary(
        manifest,
        epochs,
        streams,
        max(crash_markers),
    )
    command_ns, activation_ns, latest_activation_ns = _validate_command_and_activation(
        manifest, epochs, streams, crash_complete_ns
    )
    if not (
        crash_ns
        < command_ns
        <= activation_ns
        <= latest_activation_ns
        <= manager_ready_ns
        < manifest.end_ns
    ):
        raise ValidationError(
            "boundaries must satisfy crash < command <= observer activation "
            "<= latest survivor activation <= manager readiness < end"
        )
    (
        throughput,
        common_heights,
        complete_bucket_counts,
        maximum_stalls,
        minimum_post_start_ns,
        first_common_successor_ns,
        post_start_ns,
    ) = _validate_commits(
        manifest,
        epochs,
        streams,
        texts,
        crash_ns,
        command_ns,
        activation_ns,
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
        minimum_post_start_ns=minimum_post_start_ns,
        first_common_successor_ns=first_common_successor_ns,
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
    recurring = bool(evaluation.manifest.transition_requests)
    if recurring:
        assert medians.containment_tps is not None
        phase_median_tps = {
            "baseline": medians.baseline_tps,
            "degraded": medians.degraded_tps,
            "containment": medians.containment_tps,
        }
        metrics: dict[str, Any] = {
            "containment_to_degraded_ratio": (
                medians.containment_tps / medians.degraded_tps
            ),
        }
        if evaluation.manifest.profile_identity == PAIRED_CONTROL_PROFILE_ID:
            assert medians.control_late_tps is not None
            phase_median_tps["control_late"] = medians.control_late_tps
            metrics["control_late_to_containment_ratio"] = (
                medians.control_late_tps / medians.containment_tps
            )
        else:
            assert medians.optimized_tps is not None
            phase_median_tps["optimized"] = medians.optimized_tps
            metrics["optimized_to_containment_ratio"] = (
                medians.optimized_tps / medians.containment_tps
            )
        metrics["phase_median_tps"] = phase_median_tps
    else:
        assert medians.post_tps is not None
        metrics = {
            "baseline_median_tps": medians.baseline_tps,
            "degraded_median_tps": medians.degraded_tps,
            "post_median_tps": medians.post_tps,
            "recovery_ratio": medians.post_tps / medians.baseline_tps,
        }
    metrics.update(
        {
            "common_survivor_commit_heights": evaluation.common_commit_heights,
            "complete_bucket_counts": dict(evaluation.complete_bucket_counts),
            "maximum_commit_stall_seconds": {
                phase: stall_ns / 1_000_000_000
                for phase, stall_ns in evaluation.maximum_stall_ns_by_phase.items()
            },
            "final_reputation_scores": list(evaluation.final_scores),
        }
    )
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
            "minimum_post_start_ns": evaluation.minimum_post_start_ns,
            "first_common_successor_ns": (
                evaluation.first_common_successor_ns
            ),
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
        "metrics": metrics,
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
