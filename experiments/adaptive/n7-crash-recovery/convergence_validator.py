#!/usr/bin/env python3
"""Validate the frozen N=7 epoch-1 convergence-only experiment."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


SCENARIO = "n7-epoch1-convergence"
PROFILE_ID = "n7-f2-q5-epoch1-convergence-v2"
PROFILE_SHA256 = (
    "4146e736501e5b6f07409ccf83cd3b2b39fbefee3a89590f47b03bdcc1db16ae"
)
BASE_PROFILE_ID = "n7-f2-q5-crash-recovery-v2"
BASE_PROFILE_SHA256 = (
    "768c33418937f9b738c607b523ad847a7cb38220c95a499e82823ac41aa1e038"
)
MEMBERSHIP = tuple(range(7))
CRASH_TARGETS = (0, 1)
SURVIVORS = (2, 3, 4, 5, 6)
FAULT_THRESHOLD = 2
QUORUM = 5
SUCCESSOR_EPOCH = 1

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
        "crash_targets",
        "survivors",
        "successor_epoch",
        "crash_markers",
        "crash_configuration_boundary",
        "sources",
        "artifacts",
        "manager_argv",
    }
)
_PROFILE_DESCRIPTOR_FIELDS = frozenset({"identity", "path", "sha256"})
_RUN_COMPLETION_FIELDS = frozenset(
    {
        "complete",
        "interrupted",
        "runtime_error",
        "manager_exit_code",
        "unexpected_survivor_exits",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "source_kind",
        "source_id",
        "source_instance",
        "pid",
        "pgid",
        "path",
        "sha256",
    }
)
_ARTIFACT_FIELDS = frozenset({"kind", "path", "sha256"})
_CONVERGENCE_PAYLOAD_FIELDS = frozenset(
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
_IDENTITY_FIELDS = frozenset(
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
_PROCESS_FIELDS = frozenset({"exit_status"})
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
_CRASH_BOUNDARY_FIELDS = frozenset(
    {
        "epoch_number",
        "tree_id",
        "root_replica",
        "epoch_digest",
        "context_generation",
        "replica_evidence",
    }
)
_CRASH_BOUNDARY_EVIDENCE_FIELDS = frozenset(
    {"source_id", "source_sequence", "source_monotonic_ns"}
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
_ACTIVATION_FIELDS = frozenset(
    {"epoch_number", "tree_id", "epoch_digest", "activation_height"}
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
_DECISION_PROOF_FIELDS = frozenset(
    {"epoch_number", "tree_id", "epoch_digest", "block_hash"}
)
_COMMON_SUCCESSOR_FIELDS = frozenset(
    {
        "epoch_number",
        "block_height",
        "block_hash",
        "common_monotonic_ns",
        "participants",
        "authoritative_observer",
    }
)
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
_ARTIFACT_KINDS = (
    "profile",
    "successor_bundle",
    "epochs",
    "runner_state",
)
_DIGEST_DISPOSITIONS = frozenset(
    {
        "enqueued",
        "enqueue_failed",
        "injected_drop",
        "accepted",
        "duplicate",
        "rejected_nonmember",
        "rejected_spoofed_source",
        "rejected_stale",
        "rejected_wrong_identity",
        "conflicting_observation",
        "terminal",
        "ack_sent",
        "ack_injected_drop",
    }
)
class ValidationError(ValueError):
    """A complete artifact contradicts the frozen convergence contract."""


class IncompleteRun(ValidationError):
    """Required evidence is absent without contradictory success evidence."""


@dataclass(frozen=True, slots=True)
class StructuredEvent:
    run_id: str
    source_kind: str
    source_id: str
    source_instance: str
    source_sequence: int
    timestamp_ns: int
    event_type: str
    payload: dict[str, Any]


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValidationError(f"non-finite JSON number is forbidden: {value}")


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError as exc:
        raise IncompleteRun(f"missing {label}: {path}") from exc
    except OSError as exc:
        raise IncompleteRun(f"cannot read {label}: {exc}") from exc
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object")
    return value, payload


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} must be a non-empty string")
    return value


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = (1 << 64) - 1,
) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise ValidationError(f"{label} must be an integer in range")
    return value


def _exact_fields(value: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    if set(value) != fields:
        raise ValidationError(f"{label} has a noncanonical field set")


def _digest(value: Any, label: str) -> str:
    text = _string(value, label)
    if (
        len(text) != 64
        or text == "0" * 64
        or any(character not in "0123456789abcdef" for character in text)
    ):
        raise ValidationError(f"{label} must be a nonzero lowercase SHA-256")
    return text


def _profile_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profile_id": PROFILE_ID,
        "frozen": True,
        "base_profile": {
            "identity": BASE_PROFILE_ID,
            "sha256": BASE_PROFILE_SHA256,
        },
        "claim_scope": "epoch1_convergence_only",
        "replica_ids": list(MEMBERSHIP),
        "fault_threshold": FAULT_THRESHOLD,
        "quorum": QUORUM,
        "crash_targets": list(CRASH_TARGETS),
        "survivors": list(SURVIVORS),
        "successor_epoch": SUCCESSOR_EPOCH,
        "activation_delay_blocks": 5,
        "fault_injection": {
            "bundle_delivery": {"recipient": 2, "attempt": 1},
            "activation_ack": {"positive_ack_ordinal": 1},
        },
        "requirements": {
            "matching_activation_sources": QUORUM,
            "exactly_one_converged": True,
            "exactly_one_ready": True,
            "require_ack_retransmission": True,
            "require_common_successor_commit": True,
            "forbid_epoch_above": SUCCESSOR_EPOCH,
            "throughput_claim": False,
        },
        "timeouts": {
            "startup_s": 90,
            "phase_s": 240,
            "manager_convergence_deadline_s": 120,
            "crash_confirm_s": 5,
            "ack_drain_s": 2,
        },
    }


def _safe_relative(base: Path, value: Any, label: str) -> tuple[str, Path]:
    relative = _string(value, label)
    candidate = Path(relative)
    if candidate.is_absolute() or relative != candidate.as_posix():
        raise ValidationError(f"{label} must be a canonical relative path")
    resolved = (base / candidate).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise ValidationError(f"{label} escapes the run directory") from exc
    return relative, resolved


def _load_artifacts(
    manifest: Mapping[str, Any], manifest_path: Path, epochs_path: Path
) -> tuple[dict[str, tuple[Path, bytes]], dict[str, Any]]:
    run_directory = manifest_path.parent
    entries = _list(manifest["artifacts"], "manifest.artifacts")
    if len(entries) != len(_ARTIFACT_KINDS):
        raise ValidationError("manifest must contain exactly one artifact of each kind")
    artifacts: dict[str, tuple[Path, bytes]] = {}
    for index, raw in enumerate(entries):
        entry = _object(raw, f"artifacts[{index}]")
        _exact_fields(entry, _ARTIFACT_FIELDS, f"artifacts[{index}]")
        kind = _string(entry["kind"], f"artifacts[{index}].kind")
        if kind not in _ARTIFACT_KINDS or kind in artifacts:
            raise ValidationError("artifact kinds are not exact and unique")
        _, path = _safe_relative(
            run_directory, entry["path"], f"artifacts[{index}].path"
        )
        try:
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise IncompleteRun(f"missing artifact: {entry['path']}") from exc
        expected = _digest(entry["sha256"], f"artifacts[{index}].sha256")
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValidationError(f"artifact SHA-256 mismatch: {entry['path']}")
        artifacts[kind] = (path, payload)
    if tuple(artifacts) != _ARTIFACT_KINDS:
        raise ValidationError("artifact ordering is not canonical")
    if artifacts["epochs"][0] != epochs_path.resolve():
        raise ValidationError("--epochs does not select the manifest epoch artifact")

    profile_path, profile_payload = artifacts["profile"]
    if hashlib.sha256(profile_payload).hexdigest() != PROFILE_SHA256:
        raise ValidationError(
            "convergence profile bytes differ from the frozen contract"
        )
    profile, _ = _load_json(profile_path, "convergence profile")
    if profile != _profile_document():
        raise ValidationError("convergence profile differs from the frozen contract")
    runner_state, _ = _load_json(artifacts["runner_state"][0], "runner state")
    return artifacts, runner_state


def _parse_structured_events(
    text: str,
    *,
    expected_run_id: str,
    expected_source_kind: str,
    expected_source_id: str,
    expected_source_instance: str,
) -> tuple[StructuredEvent, ...]:
    """Parse one canonical, source-bound JSONL stream without shared helpers."""
    events: list[StructuredEvent] = []
    previous_timestamp_ns: int | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            envelope = json.loads(
                line,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"line {line_number}: malformed structured-event JSON: {exc.msg}"
            ) from exc
        envelope = _object(envelope, f"line {line_number} event envelope")
        _exact_fields(
            envelope,
            _EVENT_ENVELOPE_FIELDS,
            f"line {line_number} event envelope",
        )
        if _integer(
            envelope["event_schema_version"],
            f"line {line_number} event_schema_version",
            maximum=(1 << 32) - 1,
        ) != 1:
            raise ValidationError("unsupported event_schema_version")
        for field, expected in (
            ("run_id", expected_run_id),
            ("source_kind", expected_source_kind),
            ("source_id", expected_source_id),
            ("source_instance", expected_source_instance),
        ):
            if _string(envelope[field], f"line {line_number} {field}") != expected:
                raise ValidationError(
                    f"line {line_number} structured event {field} mismatch"
                )
        sequence = _integer(
            envelope["source_sequence"],
            f"line {line_number} source_sequence",
            minimum=1,
        )
        expected_sequence = len(events) + 1
        if sequence != expected_sequence:
            raise IncompleteRun(
                f"source sequence gap: expected {expected_sequence}, got {sequence}"
            )
        timestamp_ns = _integer(
            envelope["source_monotonic_ns"],
            f"line {line_number} source_monotonic_ns",
            minimum=1,
        )
        if previous_timestamp_ns is not None and timestamp_ns < previous_timestamp_ns:
            raise ValidationError(
                f"line {line_number} source_monotonic_ns regressed"
            )
        previous_timestamp_ns = timestamp_ns
        event_type = _string(envelope["event_type"], f"line {line_number} event_type")
        payload = _object(envelope["payload"], f"line {line_number} payload")
        events.append(
            StructuredEvent(
                run_id=expected_run_id,
                source_kind=expected_source_kind,
                source_id=expected_source_id,
                source_instance=expected_source_instance,
                source_sequence=sequence,
                timestamp_ns=timestamp_ns,
                event_type=event_type,
                payload=payload,
            )
        )
    return tuple(events)


def _load_streams(
    manifest: Mapping[str, Any], manifest_path: Path, run_id: str
) -> dict[str, tuple[StructuredEvent, ...]]:
    entries = _list(manifest["sources"], "manifest.sources")
    expected_ids = tuple(f"replica-{replica}" for replica in MEMBERSHIP) + (
        "adaptive-manager",
    )
    if len(entries) != len(expected_ids):
        raise ValidationError("manifest must contain seven replicas and one manager")
    streams: dict[str, tuple[StructuredEvent, ...]] = {}
    for index, (raw, expected_id) in enumerate(zip(entries, expected_ids)):
        source = _object(raw, f"sources[{index}]")
        _exact_fields(source, _SOURCE_FIELDS, f"sources[{index}]")
        source_id = _string(source["source_id"], f"sources[{index}].source_id")
        if source_id != expected_id:
            raise ValidationError("source ordering or identity is not canonical")
        expected_kind = "replica" if index < len(MEMBERSHIP) else "adaptation_manager"
        if source["source_kind"] != expected_kind:
            raise ValidationError(f"{source_id} has the wrong source kind")
        source_instance = _string(
            source["source_instance"], f"sources[{index}].source_instance"
        )
        _integer(source["pid"], f"sources[{index}].pid", minimum=1)
        _integer(source["pgid"], f"sources[{index}].pgid", minimum=1)
        _, path = _safe_relative(
            manifest_path.parent, source["path"], f"sources[{index}].path"
        )
        try:
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise IncompleteRun(f"missing raw source: {source['path']}") from exc
        digest = _digest(source["sha256"], f"sources[{index}].sha256")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValidationError(f"raw source SHA-256 mismatch: {source['path']}")
        try:
            text = payload.decode("utf-8")
            events = _parse_structured_events(
                text,
                expected_run_id=run_id,
                expected_source_kind=expected_kind,
                expected_source_id=source_id,
                expected_source_instance=source_instance,
            )
        except UnicodeDecodeError as exc:
            raise ValidationError(f"invalid raw source {source['path']}: {exc}") from exc
        if not events or events[0].source_sequence != 1:
            raise IncompleteRun(f"raw source is absent or truncated: {source['path']}")
        if any(event.source_sequence != offset for offset, event in enumerate(events, 1)):
            raise IncompleteRun(f"raw source has a sequence gap: {source['path']}")
        streams[source_id] = events
    return streams


def _replica_list(value: Any, label: str) -> tuple[int, ...]:
    values = _list(value, label)
    replicas = tuple(
        _integer(item, f"{label}[{index}]", maximum=len(MEMBERSHIP) - 1)
        for index, item in enumerate(values)
    )
    if len(replicas) != len(set(replicas)):
        raise ValidationError(f"{label} contains duplicate replicas")
    return replicas


def _load_epochs(path: Path) -> dict[str, Any]:
    document, _ = _load_json(path, "epoch definition")
    _exact_fields(document, _EPOCH_DOCUMENT_FIELDS, "epoch definition")
    for field, expected in (
        ("schema_version", 1),
        ("replica_count", len(MEMBERSHIP)),
        ("fault_threshold", FAULT_THRESHOLD),
        ("quorum", QUORUM),
        ("membership", list(MEMBERSHIP)),
    ):
        if document[field] != expected:
            raise ValidationError(f"epochs {field} differs from the frozen contract")
    epoch_values = _list(document["epochs"], "epochs")
    if len(epoch_values) != 2:
        raise ValidationError("epoch definition must contain exactly epochs 0 and 1")

    parsed: list[tuple[str, list[dict[str, Any]], dict[str, Any] | None]] = []
    for index, value in enumerate(epoch_values):
        epoch = _object(value, f"epochs[{index}]")
        _exact_fields(epoch, _EPOCH_FIELDS, f"epochs[{index}]")
        if _integer(
            epoch["epoch_number"],
            f"epochs[{index}].epoch_number",
            maximum=(1 << 32) - 1,
        ) != index:
            raise ValidationError("epoch numbers must be exactly 0 then 1")
        epoch_digest = _digest(epoch["epoch_digest"], f"epochs[{index}].epoch_digest")
        trees: list[dict[str, Any]] = []
        for tree_index, tree_value in enumerate(
            _list(epoch["trees"], f"epochs[{index}].trees")
        ):
            tree = _object(tree_value, f"epochs[{index}].trees[{tree_index}]")
            _exact_fields(tree, _TREE_FIELDS, f"epochs[{index}].trees[{tree_index}]")
            _integer(tree["tree_id"], "tree_id", maximum=(1 << 32) - 1)
            _integer(tree["fanout"], "fanout", minimum=1, maximum=(1 << 32) - 1)
            _replica_list(tree["members_breadth_first"], "members_breadth_first")
            _replica_list(tree["wait_exempt"], "wait_exempt")
            trees.append(tree)
        command_value = epoch["command"]
        command: dict[str, Any] | None = None
        if command_value is not None:
            command = _object(command_value, f"epochs[{index}].command")
            _exact_fields(command, _COMMAND_FIELDS, f"epochs[{index}].command")
        parsed.append((epoch_digest, trees, command))

    initial_digest, initial_trees, initial_command = parsed[0]
    successor_digest, successor_trees, successor_command = parsed[1]
    if initial_digest == successor_digest:
        raise ValidationError("successor epoch digest must differ from epoch 0")
    if initial_command is not None:
        raise ValidationError("epoch 0 command must be null")
    if successor_command is None:
        raise ValidationError("successor epoch has no committed command")
    if len(initial_trees) != len(MEMBERSHIP):
        raise ValidationError("epoch 0 must contain seven cyclic trees")
    for tree_id, tree in enumerate(initial_trees):
        members = _replica_list(tree["members_breadth_first"], "epoch 0 members")
        if tree["tree_id"] != tree_id or tree["fanout"] != 2:
            raise ValidationError("epoch 0 trees require ids 0..6 and fanout 2")
        if members != tuple((tree_id + offset) % 7 for offset in MEMBERSHIP):
            raise ValidationError(f"epoch 0 tree {tree_id} is not cyclic")
        if tree["wait_exempt"] != []:
            raise ValidationError("epoch 0 must not have wait-exempt replicas")
    if len(successor_trees) != QUORUM:
        raise ValidationError("successor epoch must contain exactly five trees")
    roots: list[int] = []
    for tree_id, tree in enumerate(successor_trees):
        members = _replica_list(tree["members_breadth_first"], "successor members")
        wait_exempt = _replica_list(tree["wait_exempt"], "successor wait_exempt")
        if tree["tree_id"] != tree_id or tree["fanout"] != 2:
            raise ValidationError("successor trees require ids 0..4 and fanout 2")
        if len(members) != len(MEMBERSHIP) or set(members) != set(MEMBERSHIP):
            raise ValidationError("successor tree membership must remain unchanged")
        if wait_exempt != CRASH_TARGETS:
            raise ValidationError("only replicas 0 and 1 may be wait-exempt")
        leaf_start = (len(members) - 2) // tree["fanout"] + 1
        if any(members.index(replica) < leaf_start for replica in CRASH_TARGETS):
            raise ValidationError("crash targets must be successor-tree leaves")
        roots.append(members[0])
    if len(set(roots)) != QUORUM or set(roots) != set(SURVIVORS):
        raise ValidationError("successor roots must be exactly the five survivors")

    command = successor_command
    for field in ("command_block_height", "activation_delay_blocks", "activation_height"):
        _integer(command[field], f"successor.command.{field}", minimum=1)
    for field in ("predecessor_epoch_number", "successor_epoch_number"):
        _integer(command[field], f"successor.command.{field}", maximum=(1 << 32) - 1)
    for field in (
        "command_block_hash",
        "payload_digest",
        "predecessor_epoch_digest",
        "successor_epoch_digest",
    ):
        _digest(command[field], f"successor.command.{field}")
    if command["predecessor_epoch_number"] != 0 or command["successor_epoch_number"] != 1:
        raise ValidationError("successor command epoch numbers are not exactly 0 then 1")
    if command["predecessor_epoch_digest"] != initial_digest:
        raise ValidationError("command predecessor digest does not match epoch 0")
    if command["successor_epoch_digest"] != successor_digest:
        raise ValidationError("command successor digest does not match epoch 1")
    if command["activation_delay_blocks"] != 5:
        raise ValidationError("successor command changed the frozen activation delay")
    if command["activation_height"] != command["command_block_height"] + 5:
        raise ValidationError("command activation height must equal h_c + delta")
    return document


def _expected_identity(epoch_document: Mapping[str, Any]) -> dict[str, Any]:
    successor = _object(epoch_document["epochs"][1], "epochs[1]")
    command = _object(successor["command"], "epochs[1].command")
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


def _convergence_payload(event: StructuredEvent) -> dict[str, Any]:
    payload = _object(event.payload, f"{event.event_type}.payload")
    _exact_fields(payload, _CONVERGENCE_PAYLOAD_FIELDS, f"{event.event_type}.payload")
    if _integer(payload["required_activation_count"], "required_activation_count", minimum=1) != QUORUM:
        raise ValidationError("convergence event changed the fixed quorum")
    for field in ("accepted_commit_count", "accepted_activation_count"):
        _integer(payload[field], field, maximum=len(MEMBERSHIP))
    identity = payload["identity"]
    if identity is not None:
        identity = _object(identity, "convergence identity")
        _exact_fields(identity, _IDENTITY_FIELDS, "convergence identity")
        for field in (
            "predecessor_epoch_digest",
            "successor_epoch_digest",
            "command_payload_digest",
            "command_block_hash",
        ):
            _digest(identity[field], f"identity.{field}")
    canonical_digest = payload["canonical_payload_digest"]
    disposition = payload["disposition"]
    if disposition in _DIGEST_DISPOSITIONS:
        _digest(canonical_digest, "canonical_payload_digest")
    elif disposition == "ack_send_failed":
        if canonical_digest is not None:
            _digest(canonical_digest, "canonical_payload_digest")
    elif canonical_digest is not None:
        raise ValidationError("canonical payload digest is present for an uncorrelated event")
    return payload


def _events(
    stream: Sequence[StructuredEvent], event_type: str
) -> list[StructuredEvent]:
    return [event for event in stream if event.event_type == event_type]


def _validate_crash_ground_truth(
    manifest: Mapping[str, Any],
    streams: Mapping[str, Sequence[StructuredEvent]],
    identity: Mapping[str, Any],
) -> dict[int, int]:
    boundary = _object(
        manifest["crash_configuration_boundary"],
        "manifest.crash_configuration_boundary",
    )
    _exact_fields(boundary, _CRASH_BOUNDARY_FIELDS, "crash boundary")
    if (
        boundary["epoch_number"] != 0
        or boundary["tree_id"] != 6
        or boundary["root_replica"] != 6
        or boundary["epoch_digest"] != identity["predecessor_epoch_digest"]
    ):
        raise ValidationError("crash boundary is not the exact pre-crash tree-6")
    context_generation = boundary["context_generation"]
    if context_generation is not None:
        _integer(context_generation, "crash boundary context_generation", minimum=1)
    evidence_values = _list(boundary["replica_evidence"], "crash boundary evidence")
    if len(evidence_values) != len(MEMBERSHIP):
        raise IncompleteRun("pre-crash tree-6 boundary lacks all replica evidence")
    boundary_timestamps: list[int] = []
    for replica, raw in zip(MEMBERSHIP, evidence_values):
        evidence = _object(raw, f"crash boundary evidence[{replica}]")
        _exact_fields(
            evidence,
            _CRASH_BOUNDARY_EVIDENCE_FIELDS,
            f"crash boundary evidence[{replica}]",
        )
        source_id = f"replica-{replica}"
        if evidence["source_id"] != source_id:
            raise ValidationError("crash boundary evidence source ordering changed")
        sequence = _integer(
            evidence["source_sequence"], "crash boundary source_sequence", minimum=1
        )
        timestamp_ns = _integer(
            evidence["source_monotonic_ns"],
            "crash boundary source_monotonic_ns",
            minimum=1,
        )
        matching = [
            event
            for event in streams[source_id]
            if event.source_sequence == sequence
            and event.timestamp_ns == timestamp_ns
            and event.event_type == "adaptive.configuration_active"
        ]
        if len(matching) != 1:
            raise ValidationError("crash boundary does not bind one raw tree-6 event")
        payload = _object(matching[0].payload, "adaptive.configuration_active payload")
        _exact_fields(
            payload,
            _CONFIGURATION_ACTIVE_FIELDS,
            "adaptive.configuration_active payload",
        )
        if (
            payload["epoch_number"] != 0
            or payload["tree_id"] != 6
            or payload["epoch_digest"] != identity["predecessor_epoch_digest"]
            or payload["block_hash"] is not None
            or payload["observer_replica"] != replica
            or payload["wait_exempt_signers"] != []
            or payload["accepted_signers"] != []
            or payload["absent_direct_children"] != []
            or payload["missing_optional_signers"] != []
            or payload["required_branch_gaps"] != []
            or payload["root_signer_count"] != 0
            or payload["global_quorum"] != QUORUM
            or payload["rejection_reason"] is not None
            or payload["context_generation"] != context_generation
        ):
            raise ValidationError("raw crash boundary event is not the exact tree-6 identity")
        boundary_timestamps.append(timestamp_ns)

    marker_values = _list(manifest["crash_markers"], "manifest.crash_markers")
    if len(marker_values) < len(CRASH_TARGETS):
        raise IncompleteRun("both exact SIGKILL crash markers are required")
    if len(marker_values) != len(CRASH_TARGETS):
        raise ValidationError("crash markers must contain exactly replicas 0 and 1")
    requested_by_replica: dict[int, int] = {}
    sources_by_id = {
        _string(source["source_id"], "manifest source_id"): source
        for source in _list(manifest["sources"], "manifest.sources")
    }
    for expected_replica, raw in zip(CRASH_TARGETS, marker_values):
        marker = _object(raw, f"crash marker {expected_replica}")
        _exact_fields(marker, _CRASH_MARKER_FIELDS, f"crash marker {expected_replica}")
        if marker["replica_id"] != expected_replica:
            raise ValidationError("SIGKILL crash marker ordering or target changed")
        pid = _integer(marker["pid"], "crash marker pid", minimum=1)
        pgid = _integer(marker["pgid"], "crash marker pgid", minimum=1)
        source = sources_by_id[f"replica-{expected_replica}"]
        source_pid = _integer(source["pid"], "replica source pid", minimum=1)
        source_pgid = _integer(source["pgid"], "replica source pgid", minimum=1)
        if pid != source_pid:
            raise ValidationError("crash marker pid differs from its replica source")
        if pgid != source_pgid:
            raise ValidationError("crash marker pgid differs from its replica source")
        if marker["signal"] != "SIGKILL" or marker["signal_number"] != 9:
            raise ValidationError("crash marker is not exact SIGKILL ground truth")
        requested_ns = _integer(
            marker["requested_monotonic_raw_ns"],
            "crash marker requested_monotonic_raw_ns",
            minimum=1,
        )
        confirmed_raw = marker["confirmed_exit"]
        if confirmed_raw is None:
            raise IncompleteRun("SIGKILL crash marker lacks confirmed exit")
        confirmed = _object(confirmed_raw, "crash marker confirmed_exit")
        _exact_fields(confirmed, _CONFIRMED_EXIT_FIELDS, "confirmed SIGKILL exit")
        if (
            confirmed["pid"] != pid
            or confirmed["pgid"] != pgid
            or confirmed["signal"] != "SIGKILL"
            or confirmed["signal_number"] != 9
        ):
            raise ValidationError("confirmed SIGKILL exit differs from its request")
        observed_ns = _integer(
            confirmed["observed_monotonic_raw_ns"],
            "confirmed SIGKILL observed_monotonic_raw_ns",
            minimum=1,
        )
        if observed_ns < requested_ns or max(boundary_timestamps) >= requested_ns:
            raise ValidationError("SIGKILL ordering does not follow the tree-6 boundary")
        requested_by_replica[expected_replica] = requested_ns

    for replica, requested_ns in requested_by_replica.items():
        if any(event.timestamp_ns >= requested_ns for event in streams[f"replica-{replica}"]):
            raise ValidationError(f"replica-{replica} emitted a post-SIGKILL event")
    return requested_by_replica


def _validate_process_streams(
    streams: Mapping[str, Sequence[StructuredEvent]],
    crash_requests: Mapping[int, int],
) -> None:
    for source_id, stream in streams.items():
        for event_type in ("process.started", "process.ready"):
            values = _events(stream, event_type)
            if len(values) != 1:
                raise IncompleteRun(f"{source_id} lacks exactly one {event_type}")
            _exact_fields(values[0].payload, _PROCESS_FIELDS, f"{event_type}.payload")
        stopped = _events(stream, "process.stopped")
        replica = (
            int(source_id.removeprefix("replica-"))
            if source_id.startswith("replica-")
            else None
        )
        if replica in crash_requests:
            if stopped:
                raise ValidationError(f"{source_id} emitted process.stopped after SIGKILL")
            continue
        if len(stopped) != 1:
            raise IncompleteRun(f"{source_id} lacks exactly one process.stopped")
        _exact_fields(stopped[0].payload, _PROCESS_FIELDS, "process.stopped.payload")
        if stopped[0].payload["exit_status"] != 0:
            raise ValidationError(f"{source_id} did not stop with exit status zero")


def _validate_replica_activations(
    streams: Mapping[str, Sequence[StructuredEvent]],
    identity: Mapping[str, Any],
) -> None:
    for replica in SURVIVORS:
        source_id = f"replica-{replica}"
        activations = _events(streams[source_id], "epoch.activated")
        if len(activations) != 1:
            raise IncompleteRun(f"{source_id} lacks exactly one epoch.activated")
        payload = _object(activations[0].payload, "epoch.activated.payload")
        _exact_fields(payload, _ACTIVATION_FIELDS, "epoch.activated.payload")
        if (
            payload["epoch_number"] != SUCCESSOR_EPOCH
            or payload["epoch_digest"] != identity["successor_epoch_digest"]
            or payload["activation_height"] != identity["activation_height"]
        ):
            raise ValidationError(f"{source_id} epoch.activated differs from the winner")
    for replica in CRASH_TARGETS:
        if any(
            event.event_type == "epoch.activated"
            and event.payload.get("epoch_number") not in (0, SUCCESSOR_EPOCH)
            for event in streams[f"replica-{replica}"]
        ):
            raise ValidationError("an event activates an epoch above the frozen successor")


def _reject_survivor_epoch_above_successor(
    streams: Mapping[str, Sequence[StructuredEvent]],
) -> None:
    for replica in SURVIVORS:
        source_id = f"replica-{replica}"
        for event in streams[source_id]:
            if event.event_type == "epoch.command_committed":
                payload = _object(event.payload, "epoch.command_committed.payload")
                _exact_fields(
                    payload,
                    _COMMAND_FIELDS,
                    "epoch.command_committed.payload",
                )
                predecessor = _integer(
                    payload["predecessor_epoch_number"],
                    "command predecessor_epoch_number",
                    maximum=(1 << 32) - 1,
                )
                successor = _integer(
                    payload["successor_epoch_number"],
                    "command successor_epoch_number",
                    maximum=(1 << 32) - 1,
                )
                if predecessor > SUCCESSOR_EPOCH or successor > SUCCESSOR_EPOCH:
                    raise ValidationError(
                        f"{source_id} emitted an epoch command above epoch 1"
                    )
            elif event.event_type == "block.committed":
                payload = _object(event.payload, "block.committed.payload")
                _exact_fields(payload, _COMMIT_FIELDS, "block.committed.payload")
                proof = _object(
                    payload["decision_proof"], "block.committed decision_proof"
                )
                _exact_fields(
                    proof,
                    _DECISION_PROOF_FIELDS,
                    "block.committed decision_proof",
                )
                epoch_number = _integer(
                    proof["epoch_number"],
                    "decision epoch_number",
                    maximum=(1 << 32) - 1,
                )
                if epoch_number > SUCCESSOR_EPOCH:
                    raise ValidationError(
                        f"{source_id} emitted a block commit above epoch 1"
                    )
            elif event.event_type == "epoch.activated":
                payload = _object(event.payload, "epoch.activated.payload")
                _exact_fields(payload, _ACTIVATION_FIELDS, "epoch.activated.payload")
                epoch_number = _integer(
                    payload["epoch_number"],
                    "activated epoch_number",
                    maximum=(1 << 32) - 1,
                )
                if epoch_number > SUCCESSOR_EPOCH:
                    raise ValidationError(
                        f"{source_id} activated an epoch above epoch 1"
                    )


def _commit_identity(
    event: StructuredEvent, *, authoritative: bool
) -> tuple[int, str]:
    payload = _object(event.payload, f"{event.event_type}.payload")
    expected_fields = _COMMIT_FIELDS if authoritative else _COMMIT_OBSERVED_FIELDS
    _exact_fields(payload, expected_fields, f"{event.event_type}.payload")
    height = _integer(payload["block_height"], "commit block_height", minimum=1)
    block_hash = _digest(payload["block_hash"], "commit block_hash")
    parent_hash = payload["parent_hash"]
    if parent_hash is not None:
        _digest(parent_hash, "commit parent_hash")
    _integer(payload["transaction_count"], "commit transaction_count")
    _integer(payload["commit_batch_index"], "commit_batch_index")
    if authoritative:
        if type(payload["designated_observer"]) is not bool:
            raise ValidationError("block.committed designated_observer must be boolean")
        proof = _object(payload["decision_proof"], "block.committed decision_proof")
        _exact_fields(proof, _DECISION_PROOF_FIELDS, "block.committed decision_proof")
        _integer(proof["epoch_number"], "decision epoch_number", maximum=(1 << 32) - 1)
        _integer(proof["tree_id"], "decision tree_id", maximum=(1 << 32) - 1)
        _digest(proof["epoch_digest"], "decision epoch_digest")
        if _digest(proof["block_hash"], "decision block_hash") != block_hash:
            raise ValidationError("successor commit proof block hash disagrees")
        view_generation = payload["view_generation"]
        if view_generation is not None:
            _integer(view_generation, "commit view_generation")
    return height, block_hash


def _validate_common_successor_commit(
    streams: Mapping[str, Sequence[StructuredEvent]],
    identity: Mapping[str, Any],
    runner_state: Mapping[str, Any],
    *,
    ack_complete_ns: int,
) -> dict[str, Any]:
    committed_by_replica: dict[
        int, list[tuple[StructuredEvent, tuple[int, str], int]]
    ] = {}
    hashes_by_epoch_height: dict[tuple[int, int], set[str]] = {}
    for replica in SURVIVORS:
        committed_by_replica[replica] = []
        for event in _events(streams[f"replica-{replica}"], "block.committed"):
            key = _commit_identity(event, authoritative=True)
            proof = _object(
                event.payload["decision_proof"], "block.committed decision_proof"
            )
            epoch_number = _integer(
                proof["epoch_number"],
                "decision epoch_number",
                maximum=(1 << 32) - 1,
            )
            committed_by_replica[replica].append((event, key, epoch_number))
            hashes_by_epoch_height.setdefault(
                (epoch_number, key[0]), set()
            ).add(key[1])

    candidates: list[tuple[StructuredEvent, tuple[int, str]]] = []
    for event, key, epoch_number in committed_by_replica[2]:
        payload = event.payload
        proof = _object(payload["decision_proof"], "block.committed decision_proof")
        if epoch_number > SUCCESSOR_EPOCH:
            raise ValidationError("a block commit claims an epoch above epoch 1")
        if epoch_number != SUCCESSOR_EPOCH:
            continue
        if (
            payload["designated_observer"] is not True
            or proof["epoch_digest"] != identity["successor_epoch_digest"]
            or proof["tree_id"] >= QUORUM
        ):
            raise ValidationError("authoritative successor commit has the wrong epoch-1 identity")
        candidates.append((event, key))
    if not candidates:
        raise IncompleteRun("common epoch-1 commit lacks an authoritative survivor commit")

    observations: dict[int, list[tuple[StructuredEvent, tuple[int, str]]]] = {}
    for replica in SURVIVORS:
        observations[replica] = [
            (event, _commit_identity(event, authoritative=False))
            for event in _events(streams[f"replica-{replica}"], "block.commit_observed")
        ]

    for values in observations.values():
        for _, witness_key in values:
            hashes_by_epoch_height.setdefault(
                (SUCCESSOR_EPOCH, witness_key[0]), set()
            ).add(witness_key[1])
    for epoch_height, hashes in hashes_by_epoch_height.items():
        _, height = epoch_height
        for values in observations.values():
            hashes.update(
                witness_key[1]
                for _, witness_key in values
                if witness_key[0] == height
            )
        if len(hashes) > 1:
            raise ValidationError("successor commit disagreement among survivors")

    disagreement = False
    complete_before_ack = False
    selected: tuple[StructuredEvent, tuple[int, str], list[StructuredEvent]] | None = None
    for observer_event, key in candidates:
        witnesses: list[StructuredEvent] = []
        missing = False
        for replica in SURVIVORS:
            values = observations[replica]
            matching = [event for event, witness_key in values if witness_key == key]
            if matching:
                witnesses.append(matching[0])
                continue
            if any(witness_key[0] == key[0] for _, witness_key in values):
                disagreement = True
            missing = True
        if not missing and observer_event.timestamp_ns <= ack_complete_ns:
            complete_before_ack = True
            continue
        if not missing:
            selected = (observer_event, key, witnesses)
            break
    if selected is None:
        if disagreement:
            raise ValidationError("successor commit disagreement among survivors")
        if complete_before_ack:
            raise ValidationError(
                "authoritative common epoch-1 commit does not follow ACK-loss recovery"
            )
        raise IncompleteRun("common epoch-1 commit lacks all survivor witnesses")

    observer_event, (height, block_hash), witnesses = selected
    common_ns = max(
        observer_event.timestamp_ns,
        *(event.timestamp_ns for event in witnesses),
    )
    expected_record = {
        "epoch_number": SUCCESSOR_EPOCH,
        "block_height": height,
        "block_hash": block_hash,
        "common_monotonic_ns": common_ns,
        "participants": list(SURVIVORS),
        "authoritative_observer": "replica-2",
    }
    state_record = _object(
        runner_state.get("first_common_successor_commit"),
        "runner_state.first_common_successor_commit",
    )
    _exact_fields(
        state_record,
        _COMMON_SUCCESSOR_FIELDS,
        "runner_state.first_common_successor_commit",
    )
    if state_record != expected_record:
        raise ValidationError("runner state common epoch-1 commit differs from raw evidence")
    return expected_record


def _single_event(values: Sequence[Any], label: str) -> Any:
    if len(values) != 1:
        raise ValidationError(f"expected exactly one {label}; found {len(values)}")
    return values[0]


def _validate_convergence(
    manager_events: Sequence[StructuredEvent],
    identity: Mapping[str, Any],
    bundle_digest: str,
) -> dict[str, Any]:
    if _events(manager_events, "adaptive_v2_convergence_failure"):
        raise ValidationError("manager emitted an adaptive-v2 convergence failure")
    delivery = _events(manager_events, "adaptive_v2_delivery_attempt")
    delivery_payloads = [(event, _convergence_payload(event)) for event in delivery]
    for _, payload in delivery_payloads:
        if payload["disposition"] not in {"enqueued", "enqueue_failed", "injected_drop"}:
            raise ValidationError("delivery attempt has an invalid disposition")
        if payload["identity"] is not None:
            raise ValidationError("delivery attempt must precede the winning identity")
        if payload["canonical_payload_digest"] != bundle_digest:
            raise ValidationError("bundle retry bytes differ from the canonical bundle")
    all_injected = [
        item for item in delivery_payloads
        if item[1]["disposition"] == "injected_drop"
    ]
    if len(all_injected) != 1:
        raise ValidationError("expected exactly one configured bundle injected drop")
    injected = [
        item for item in delivery_payloads
        if item[1]["replica_id"] == 2
        and item[1]["delivery_attempt"] == 1
        and item[1]["disposition"] == "injected_drop"
    ]
    retry = [
        item for item in delivery_payloads
        if item[1]["replica_id"] == 2
        and item[1]["delivery_attempt"] == 2
        and item[1]["disposition"] == "enqueued"
    ]
    injection_event, injection_payload = _single_event(
        injected, "bundle injected drop"
    )
    retry_event, retry_payload = _single_event(retry, "bundle retry")
    if injection_event.timestamp_ns >= retry_event.timestamp_ns:
        raise ValidationError("bundle retry does not follow its injected drop")

    commit_events = _events(manager_events, "adaptive_v2_commit_observed")
    activation_events = _events(manager_events, "adaptive_v2_activation_observed")
    commits = [(event, _convergence_payload(event)) for event in commit_events]
    activations = [(event, _convergence_payload(event)) for event in activation_events]
    accepted_commits = [item for item in commits if item[1]["disposition"] == "accepted"]
    accepted_activations = [
        item for item in activations if item[1]["disposition"] == "accepted"
    ]

    terminal_converged = _events(manager_events, "adaptive_v2_converged")
    terminal_ready = _events(manager_events, "adaptive_v2_ready")
    if len(terminal_converged) > 1 or len(terminal_ready) > 1:
        raise ValidationError("duplicate terminal convergence event")

    commit_sources = [item[1]["replica_id"] for item in accepted_commits]
    activation_sources = [item[1]["replica_id"] for item in accepted_activations]
    if len(commit_sources) != len(set(commit_sources)):
        raise ValidationError("accepted commits do not have distinct sources")
    if len(activation_sources) != len(set(activation_sources)):
        raise ValidationError("accepted activations do not have distinct sources")
    if any(source not in SURVIVORS for source in commit_sources + activation_sources):
        raise ValidationError("accepted convergence source is not a survivor")

    for _, payload in accepted_commits + accepted_activations:
        if payload["identity"] != identity:
            raise ValidationError("accepted observation has a split full identity")

    if len(activation_sources) < QUORUM:
        if terminal_converged or terminal_ready:
            raise ValidationError("terminal success lacks Q distinct activation sources")
        raise IncompleteRun("fewer than Q distinct matching activation observations")
    if set(activation_sources) != set(SURVIVORS):
        raise ValidationError("Q activations do not come from the exact survivor set")

    converged = _single_event(terminal_converged, "adaptive_v2_converged")
    ready = _single_event(terminal_ready, "adaptive_v2_ready")
    converged_payload = _convergence_payload(converged)
    ready_payload = _convergence_payload(ready)
    for label, payload in (("converged", converged_payload), ("ready", ready_payload)):
        if payload["identity"] != identity:
            raise ValidationError(f"{label} event has the wrong winning identity")
        if (
            payload["replica_id"] is not None
            or payload["delivery_attempt"] is not None
            or payload["disposition"] is not None
            or payload["accepted_commit_count"] != len(commit_sources)
            or payload["accepted_activation_count"] != QUORUM
            or payload["canonical_payload_digest"] is not None
            or payload["failure_reason"] is not None
        ):
            raise ValidationError(f"{label} event is not the exact terminal record")
    if converged.timestamp_ns >= ready.timestamp_ns:
        raise ValidationError("ready does not follow converged")
    fifth_activation_ns = max(event.timestamp_ns for event, _ in accepted_activations)
    if fifth_activation_ns >= converged.timestamp_ns:
        raise ValidationError("terminal success precedes the fifth accepted activation")

    dropped = [item for item in activations if item[1]["disposition"] == "ack_injected_drop"]
    drop_event, drop_payload = _single_event(
        dropped, "activation ACK injected drop"
    )
    target = drop_payload["replica_id"]
    if target not in SURVIVORS or drop_payload["identity"] != identity:
        raise ValidationError("activation ACK injected drop has the wrong identity")
    if any(
        payload["disposition"] == "ack_sent"
        and event.timestamp_ns <= drop_event.timestamp_ns
        for event, payload in activations
    ):
        raise ValidationError("an activation ACK was sent before or at the injected drop")
    accepted_target = [
        item for item in accepted_activations if item[1]["replica_id"] == target
    ]
    duplicate_target = [
        item for item in activations
        if item[1]["replica_id"] == target and item[1]["disposition"] == "duplicate"
    ]
    acked_target = [
        item for item in activations
        if item[1]["replica_id"] == target and item[1]["disposition"] == "ack_sent"
        and item[0].timestamp_ns > ready.timestamp_ns
    ]
    accepted_event, accepted_payload = _single_event(
        accepted_target, "activation accepted before ACK loss"
    )
    duplicate_event, duplicate_payload = _single_event(
        duplicate_target, "activation retransmission"
    )
    acked_event, acked_payload = _single_event(
        acked_target, "activation ACK during drain"
    )
    digests = {
        accepted_payload["canonical_payload_digest"],
        drop_payload["canonical_payload_digest"],
        duplicate_payload["canonical_payload_digest"],
        acked_payload["canonical_payload_digest"],
    }
    if len(digests) != 1:
        raise ValidationError("activation retransmission bytes differ")
    if not (
        accepted_event.timestamp_ns < drop_event.timestamp_ns < ready.timestamp_ns
        < duplicate_event.timestamp_ns < acked_event.timestamp_ns
    ):
        raise ValidationError("activation ACK-loss recovery ordering is invalid")

    return {
        "advisory_commit_sources": sorted(commit_sources),
        "advisory_commit_count": len(commit_sources),
        "activation_sources": sorted(activation_sources),
        "accepted_activation_count": len(activation_sources),
        "converged_event_count": len(terminal_converged),
        "ready_event_count": len(terminal_ready),
        "bundle_retry": {
            "recipient": 2,
            "first_attempt": injection_payload["delivery_attempt"],
            "retry_attempt": retry_payload["delivery_attempt"],
            "canonical_payload_digest": retry_payload["canonical_payload_digest"],
            "byte_identical": True,
        },
        "activation_ack_retry": {
            "replica_id": target,
            "canonical_payload_digest": accepted_payload["canonical_payload_digest"],
            "byte_identical": True,
            "acked_during_drain": True,
            "ack_source_monotonic_ns": acked_event.timestamp_ns,
        },
    }


def _validate_runner_state(
    runner_state: Mapping[str, Any],
    *,
    run_id: str,
    manager_exit_code: int,
    convergence: Mapping[str, Any],
    crash_markers: Any,
    crash_configuration_boundary: Any,
) -> None:
    if runner_state.get("schema_version") != 1 or runner_state.get("run_id") != run_id:
        raise ValidationError("runner state identity differs from the manifest")
    if runner_state.get("manager_exit_code") != manager_exit_code:
        raise ValidationError("runner state manager exit code differs from the manifest")
    if runner_state.get("ready_event_count") != 1:
        raise ValidationError("runner state must record exactly one ready event")
    if runner_state.get("crash_markers") != crash_markers:
        raise ValidationError("runner state crash markers differ from the manifest")
    if (
        runner_state.get("crash_configuration_boundary")
        != crash_configuration_boundary
    ):
        raise ValidationError("runner state crash boundary differs from the manifest")
    controls = _object(runner_state.get("loss_controls"), "runner_state.loss_controls")
    bundle = _object(controls.get("bundle_delivery"), "bundle loss control")
    ack = _object(controls.get("activation_ack"), "activation ACK loss control")
    if (
        bundle.get("recipient") != 2
        or bundle.get("attempt") != 1
        or bundle.get("observed") is not True
        or bundle.get("canonical_payload_digest")
        != convergence["bundle_retry"]["canonical_payload_digest"]
    ):
        raise ValidationError("runner state does not prove the exact bundle loss")
    if (
        ack.get("positive_ack_ordinal") != 1
        or ack.get("observed") is not True
        or ack.get("replica_id") != convergence["activation_ack_retry"]["replica_id"]
        or ack.get("canonical_payload_digest")
        != convergence["activation_ack_retry"]["canonical_payload_digest"]
        or ack.get("ack_source_monotonic_ns")
        != convergence["activation_ack_retry"]["ack_source_monotonic_ns"]
    ):
        raise ValidationError("runner state does not prove the exact activation ACK loss")


def _validate(
    manifest_path: Path, epochs_path: Path
) -> tuple[dict[str, Any], bytes, bytes]:
    manifest, manifest_bytes = _load_json(manifest_path, "manifest")
    _exact_fields(manifest, _MANIFEST_FIELDS, "manifest")
    if manifest.get("schema_version") != 1 or manifest.get("scenario") != SCENARIO:
        raise ValidationError(
            "manifest schema or scenario is not convergence-only"
        )
    run_id = _string(manifest.get("run_id"), "manifest.run_id")
    revision = _string(manifest.get("kauri_revision"), "manifest.kauri_revision")
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValidationError("manifest revision is not a full lowercase Git revision")
    if manifest.get("kauri_worktree_clean") is not True:
        raise ValidationError("run was not launched from a clean Kauri worktree")
    for field, expected in (
        ("replica_count", len(MEMBERSHIP)),
        ("fault_threshold", FAULT_THRESHOLD),
        ("quorum", QUORUM),
        ("membership", list(MEMBERSHIP)),
        ("crash_targets", list(CRASH_TARGETS)),
        ("survivors", list(SURVIVORS)),
        ("successor_epoch", SUCCESSOR_EPOCH),
    ):
        if manifest.get(field) != expected:
            raise ValidationError(f"manifest {field} differs from the frozen N/f/Q contract")

    completion = _object(manifest["run_completion"], "manifest.run_completion")
    _exact_fields(completion, _RUN_COMPLETION_FIELDS, "manifest.run_completion")
    if completion["unexpected_survivor_exits"] != []:
        raise IncompleteRun("run records unexpected survivor exits")
    if (
        completion["complete"] is not True
        or completion["interrupted"] is not False
        or completion["runtime_error"] is not None
    ):
        raise IncompleteRun("run completion record is incomplete")
    manager_exit_code = _integer(
        completion["manager_exit_code"], "manager_exit_code", maximum=255
    )

    profile_descriptor = _object(manifest["profile"], "manifest.profile")
    _exact_fields(profile_descriptor, _PROFILE_DESCRIPTOR_FIELDS, "manifest.profile")
    if profile_descriptor["identity"] != PROFILE_ID:
        raise ValidationError("manifest profile identity differs from the frozen profile")

    artifacts, runner_state = _load_artifacts(manifest, manifest_path, epochs_path)
    profile_path, profile_bytes = artifacts["profile"]
    if (
        profile_descriptor["path"] != profile_path.relative_to(manifest_path.parent).as_posix()
        or profile_descriptor["sha256"] != hashlib.sha256(profile_bytes).hexdigest()
    ):
        raise ValidationError("manifest profile descriptor differs from its artifact")
    if len([entry for entry in manifest["artifacts"] if entry["kind"] == "successor_bundle"]) != 1:
        raise ValidationError("exactly one successor bundle is required")

    epoch_document = _load_epochs(epochs_path.resolve())
    identity = _expected_identity(epoch_document)

    streams = _load_streams(manifest, manifest_path, run_id)
    crash_requests = _validate_crash_ground_truth(manifest, streams, identity)
    _validate_process_streams(streams, crash_requests)
    _reject_survivor_epoch_above_successor(streams)
    _validate_replica_activations(streams, identity)
    bundle_digest = hashlib.sha256(artifacts["successor_bundle"][1]).hexdigest()
    convergence = _validate_convergence(
        streams["adaptive-manager"], identity, bundle_digest
    )
    common_successor_commit = _validate_common_successor_commit(
        streams,
        identity,
        runner_state,
        ack_complete_ns=convergence["activation_ack_retry"][
            "ack_source_monotonic_ns"
        ],
    )

    manager_argv = _list(manifest["manager_argv"], "manifest.manager_argv")
    expected_suffix = [
        "--convergence-deadline-seconds",
        "120",
        "--experiment-drop-bundle-attempt",
        "2:1",
        "--experiment-drop-activation-ack",
        "1",
    ]
    for option in expected_suffix[::2]:
        if manager_argv.count(option) != 1:
            raise ValidationError(
                "manager argv does not contain each convergence control exactly once"
            )
    if manager_argv[-len(expected_suffix) :] != expected_suffix:
        raise ValidationError(
            "manager argv does not contain the exact convergence controls"
        )
    _validate_runner_state(
        runner_state,
        run_id=run_id,
        manager_exit_code=manager_exit_code,
        convergence=convergence,
        crash_markers=manifest["crash_markers"],
        crash_configuration_boundary=manifest[
            "crash_configuration_boundary"
        ],
    )
    if manager_exit_code != 0 or convergence["ready_event_count"] != 1:
        raise ValidationError("manager exit success requires exactly one ready event")

    verdict = {
        "schema_version": 1,
        "verdict": "PASS",
        "reason": "exact N=7/f=2/Q=5 epoch-1 convergence contract satisfied",
        "profile_identity": PROFILE_ID,
        "replica_count": len(MEMBERSHIP),
        "fault_threshold": FAULT_THRESHOLD,
        "quorum": QUORUM,
        "winning_identity": identity,
        "first_common_successor_commit": common_successor_commit,
        **convergence,
    }
    return verdict, manifest_bytes, profile_bytes


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_result(
    output_directory: Path,
    verdict: Mapping[str, Any],
    manifest_bytes: bytes | None,
    profile_bytes: bytes | None,
) -> None:
    output_directory.mkdir(parents=True, exist_ok=False)
    _write_json(output_directory / "convergence.json", verdict)
    if manifest_bytes is not None and profile_bytes is not None:
        (output_directory / "manifest.json").write_bytes(manifest_bytes)
        (output_directory / "convergence-profile.json").write_bytes(profile_bytes)


def validate_run(
    manifest_path: Path, epochs_path: Path, output_directory: Path
) -> dict[str, Any]:
    """Validate one preserved run and write a non-graphical immutable verdict."""
    manifest_bytes: bytes | None = None
    profile_bytes: bytes | None = None
    try:
        verdict, manifest_bytes, profile_bytes = _validate(
            manifest_path.resolve(), epochs_path.resolve()
        )
    except IncompleteRun as exc:
        verdict = {
            "schema_version": 1,
            "verdict": "INCOMPLETE",
            "reason": str(exc),
        }
    except (ValidationError, OSError, ValueError) as exc:
        verdict = {
            "schema_version": 1,
            "verdict": "FAIL",
            "reason": str(exc),
        }
    _write_result(output_directory.resolve(), verdict, manifest_bytes, profile_bytes)
    return verdict


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--epochs", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _arguments(argv)
    verdict = validate_run(args.manifest, args.epochs, args.output_dir)
    print(f"{verdict['verdict']}: {verdict['reason']}")
    raise SystemExit(0 if verdict["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
