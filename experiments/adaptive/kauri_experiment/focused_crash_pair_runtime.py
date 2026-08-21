"""Fail-closed runtime for the focused N7/N31 crash-pair experiment.

Authorization, process control, phase ordering, and artifact sealing remain
separate boundaries.  The default backend composes the reviewed low-level
runtime primitives, while every external effect remains injectable for tests.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
import ctypes
from dataclasses import asdict, dataclass, field, is_dataclass
import errno
import hashlib
from itertools import combinations
import json
import os
from pathlib import Path
import platform
import shutil
import stat
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
_PROFILE_KEYS_V2 = _PROFILE_KEYS | {"evidence_guard"}
_PROFILE_KEYS_V4 = _PROFILE_KEYS_V2 | {"fault_window_arm"}
_FCRASH_H_V3_PROFILE_IDS = frozenset(
    {
        "n7-f2-q5-two-crash-pair-smoke-v3",
        "n31-f5-q21-three-crash-pair-v3",
    }
)
_FCRASH_H_V13_PROFILE_IDS = frozenset(
    {
        "n7-f2-q5-two-crash-pair-smoke-v13",
        "n31-f5-q21-three-crash-pair-v13",
    }
)
_N31_FCRASH_H_V13_PROFILE_ID = "n31-f5-q21-three-crash-pair-v13"
_FOCUSED_LEADER_PROGRESS_TIMEOUT_S = 8.0
_V13_MAXIMUM_OBSERVATION_ATTEMPTS = 5
_V13_OBSERVATION_RETRY_INTERVAL_MS = 1_000
_V13_MANAGER_CHECKPOINT = "adaptive_v3_unified_manager_checkpoint4"
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
_V13_AUTHORIZATION_READINESS_KEY = "pair_readiness_manifests"
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


def _native_responsiveness_policy(profile_id: object) -> dict[str, object]:
    """Return the exact manager responsiveness policy for one frozen profile."""

    policy = dict(_NATIVE_RESPONSIVENESS_POLICY)
    if profile_id == _N31_FCRASH_H_V13_PROFILE_ID:
        # The 31-replica same-host runtime can produce a short burst of
        # collateral timeouts while retaining a healthy rate over the exact
        # bounded window.  Let the unchanged rate gates classify that burst;
        # a full-window timeout streak remains independently nonresponsive.
        policy["trailing_timeout_streak"] = policy["attempt_window"]
    return policy


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
_FAULT_WINDOW_ARM_DOMAIN_V1 = "kauri-focused-fault-window-arm-v1"
_FAULT_WINDOW_ARM_DOMAIN_V2 = "kauri-focused-fault-window-arm-v2"
_FAULT_WINDOW_ARM_DOMAIN_V3 = "kauri-focused-fault-window-arm-v3"
_FAULT_WINDOW_ARM_DOMAIN_V4 = "kauri-focused-fault-window-arm-v4"
_FAULT_WINDOW_ARM_FILENAME = "fault-window-arm.json"
_V6_TIMEOUT_OBSERVATION_DOMAIN = b"kauri-response-observation-v3"


class FocusedCrashPairRuntimeError(RuntimeError):
    """The focused runtime input or observed execution state is invalid."""


class FocusedIncompleteTransitionError(FocusedCrashPairRuntimeError):
    """A successful manager stopped before every survivor witnessed a transition."""

    def __init__(
        self,
        phase: str,
        epoch: int,
        activation: bool,
        missing_survivor_ids: Sequence[int],
        *,
        live_raw_event_stream_sha256: str,
        live_source_cutoffs: Sequence[Mapping[str, object]],
    ) -> None:
        self.phase = phase
        self.epoch = epoch
        self.activation = activation
        self.live_missing_survivor_ids = tuple(sorted(missing_survivor_ids))
        self.live_raw_event_stream_sha256 = live_raw_event_stream_sha256
        self.live_source_cutoffs = tuple(dict(item) for item in live_source_cutoffs)
        kind = "activation" if activation else "command"
        super().__init__(
            f"Epoch {epoch} {kind} barrier is incomplete; "
            f"missing survivor IDs {list(self.live_missing_survivor_ids)}"
        )

    def bind_final_replay(self, missing_survivor_ids: Sequence[int]) -> None:
        """Update the human diagnosis without overwriting the live observation."""

        self.final_missing_survivor_ids = tuple(sorted(missing_survivor_ids))
        kind = "activation" if self.activation else "command"
        if self.final_missing_survivor_ids:
            message = (
                f"Epoch {self.epoch} {kind} barrier is incomplete; final post-cleanup "
                f"missing survivor IDs {list(self.final_missing_survivor_ids)}"
            )
        else:
            message = (
                f"Epoch {self.epoch} {kind} barrier was incomplete at manager exit "
                "but completed before cleanup; final missing survivor IDs []"
            )
        self.args = (message,)


def _error(message: str) -> None:
    raise FocusedCrashPairRuntimeError(message)


def _authoritative_lifecycle_instance(
    events: Sequence[Mapping[str, Any]], expected_source: str
) -> str:
    """Bind an authoritative replica to its one native lifecycle instance."""

    lifecycle = [
        event
        for event in events
        if event.get("event_type") in {"process.started", "process.ready"}
        and event.get("source_id") == expected_source
    ]
    if len(lifecycle) != 2 or {event.get("event_type") for event in lifecycle} != {
        "process.started",
        "process.ready",
    }:
        _error("authoritative progress lacks an exact lifecycle binding")
    if any(event.get("source_kind") != "replica" for event in lifecycle):
        _error("authoritative progress lifecycle kind drifted")
    instances = {event.get("source_instance") for event in lifecycle}
    if len(instances) != 1 or not isinstance(next(iter(instances)), str):
        _error("authoritative progress lifecycle instance is ambiguous")
    return str(next(iter(instances)))


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


def _raw_event_source_cutoffs(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, object]]:
    """Bind a live aggregate replay to each authenticated source tail."""

    tails: dict[tuple[str, str, str], tuple[int, int]] = {}
    for event in events:
        key = (
            str(event["source_kind"]),
            str(event["source_id"]),
            str(event["source_instance"]),
        )
        value = (
            _integer(event["source_sequence"], "source cutoff sequence", 1),
            _integer(event["source_monotonic_ns"], "source cutoff timestamp", 1),
        )
        if key not in tails or value[0] > tails[key][0]:
            tails[key] = value
    return [
        {
            "source_kind": key[0],
            "source_id": key[1],
            "source_instance": key[2],
            "source_sequence": value[0],
            "source_monotonic_ns": value[1],
        }
        for key, value in sorted(tails.items())
    ]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _v6_timeout_observation_id(
    *,
    reporter_id: int,
    observed_replica_id: int,
    epoch_number: int,
    tree_id: int,
    epoch_digest: str,
    block_hash: str,
    expected_message_type: str,
    attempt_start_monotonic_ns: int,
    deadline_duration_us: int,
) -> str:
    """Recompute the native schema-v3 exact-attempt observation identity."""

    message_types = {"direct_vote": 1, "aggregate_relay": 2}
    if expected_message_type not in message_types:
        _error("v6 observation expected message type is invalid")
    try:
        encoded = b"".join(
            (
                _V6_TIMEOUT_OBSERVATION_DOMAIN,
                reporter_id.to_bytes(2, "big"),
                observed_replica_id.to_bytes(2, "big"),
                epoch_number.to_bytes(4, "big"),
                tree_id.to_bytes(4, "big"),
                bytes.fromhex(epoch_digest),
                bytes.fromhex(block_hash),
                message_types[expected_message_type].to_bytes(1, "big"),
                attempt_start_monotonic_ns.to_bytes(8, "big"),
                deadline_duration_us.to_bytes(8, "big"),
            )
        )
    except (OverflowError, ValueError) as exc:
        raise FocusedCrashPairRuntimeError(
            "v6 observation identity fields are out of range"
        ) from exc
    return _sha256(encoded)


def _is_v4_profile(profile: FocusedProfile | object) -> bool:
    return str(getattr(profile, "profile_id", "")).endswith(
        ("-v4", "-v5", "-v6", "-v7", "-v8", "-v9", "-v10", "-v11", "-v12", "-v13")
    )


def _is_v5_profile(profile: FocusedProfile | object) -> bool:
    return str(getattr(profile, "profile_id", "")).endswith(
        ("-v5", "-v6", "-v7", "-v8", "-v9", "-v10", "-v11", "-v12", "-v13")
    )


def _is_v6_profile(profile: FocusedProfile | object) -> bool:
    return str(getattr(profile, "profile_id", "")).endswith("-v6")


def _is_v7_profile(profile: FocusedProfile | object) -> bool:
    return str(getattr(profile, "profile_id", "")).endswith("-v7")


def _is_v8_profile(profile: FocusedProfile | object) -> bool:
    return str(getattr(profile, "profile_id", "")).endswith("-v8")


def _is_v9_profile(profile: FocusedProfile | object) -> bool:
    """Return whether this is the prospective guarded-cohort contract."""

    return str(getattr(profile, "profile_id", "")).endswith(
        ("-v9", "-v10", "-v11", "-v12", "-v13")
    )


def _is_v10_profile(profile: FocusedProfile | object) -> bool:
    return str(getattr(profile, "profile_id", "")).endswith(
        ("-v10", "-v11", "-v12", "-v13")
    )


def _is_v12_profile(profile: FocusedProfile | object) -> bool:
    return str(getattr(profile, "profile_id", "")).endswith(("-v12", "-v13"))


def _profile_document_value(profile: FocusedProfile | Mapping[str, Any] | object) -> Mapping[str, Any]:
    if isinstance(profile, Mapping):
        return profile
    raw = getattr(profile, "raw", None)
    if not isinstance(raw, Mapping):
        _error("profile document is unavailable")
    return raw


def _profile_id_value(profile: FocusedProfile | Mapping[str, Any] | object) -> str:
    raw = _profile_document_value(profile)
    value = raw.get("profile_id", getattr(profile, "profile_id", None))
    if not isinstance(value, str):
        _error("profile id is unavailable")
    return value


def _is_v13_profile(profile: FocusedProfile | Mapping[str, Any] | object) -> bool:
    """Recognize only the two frozen CERT13 execution identities."""

    try:
        return _profile_id_value(profile) in _FCRASH_H_V13_PROFILE_IDS
    except FocusedCrashPairRuntimeError:
        return False


def _decode_focused_epoch_change_bundle(
    wire: bytes,
    *,
    issuer_public_key: str,
    profile: FocusedProfile | Mapping[str, Any] | object,
) -> Any:
    """Decode by frozen profile identity without sniffing or fallback."""

    if _is_v13_profile(profile):
        return factorial_validation.decode_adaptive_v3_epoch_change_bundle(
            wire, issuer_public_key=issuer_public_key
        )
    return factorial_validation.decode_epoch_change_bundle(
        wire, issuer_public_key=issuer_public_key
    )


def _v13_certified_activation_contract(
    profile: FocusedProfile | Mapping[str, Any] | object,
) -> dict[str, object]:
    """Validate and return the exact CERT13 certified-activation contract."""

    raw = _profile_document_value(profile)
    profile_id = _profile_id_value(profile)
    if profile_id not in _FCRASH_H_V13_PROFILE_IDS or raw.get("schema_version") != 2:
        _error("v13 certified activation requires an exact v13 profile identity")
    protocol = _document(raw.get("protocol"), "v13 protocol")
    transitions = _document(raw.get("transitions"), "v13 transitions")
    timers = _document(raw.get("timers"), "v13 timers")
    expected_shape = {
        "n7-f2-q5-two-crash-pair-smoke-v13": (7, 2, 5, 5, 480),
        "n31-f5-q21-three-crash-pair-v13": (31, 10, 21, 28, 780),
    }[profile_id]
    replica_count, fault_bound, quorum, reporters, hard_seconds = expected_shape
    if (
        protocol.get("N"),
        protocol.get("f"),
        protocol.get("Q"),
        protocol.get("epoch_protocol_mode"),
    ) != (replica_count, fault_bound, quorum, "adaptive_v3"):
        _error("v13 protocol identity or quorum drifted")
    if transitions.get("survivor_barrier_count") != reporters:
        _error("v13 survivor barrier count drifted")
    if transitions.get("common_commit_quorum") != quorum:
        _error("v13 common commit quorum drifted")
    expected = {
        "schema_version": 1,
        "domain": "kauri-focused-v13-certified-activation-v1",
        "certificate_quorum": quorum,
        "required_reporter_count": reporters,
        "epoch1_common_commit_anchor_deadline_seconds": 5,
        "containment_stabilization_seconds": 30,
        "containment_measurement_seconds": 30,
        "minimum_predecessor_residency_ms": 65_000,
        "optimization_activation_budget_seconds": 90,
        "deadline_semantics": "half_open_monotonic_v1",
        "readiness_ledger_schema": "kauri-focused-readiness-ledger-v1",
    }
    contract = _document(
        transitions.get("activation_readiness_contract"),
        "v13 activation readiness contract",
    )
    if contract != expected:
        _error("v13 activation readiness contract drifted")
    if (
        timers.get("optimization_activation_deadline_seconds") != 90
        or timers.get("arm_hard_deadline_seconds") != hard_seconds
        or timers.get("stable_phase_seconds") != 30
    ):
        _error("v13 activation timers drifted")
    return dict(contract)


def _v13_e2_activation_is_timely(
    *,
    final_common_command_ns: int,
    activation_ns: int,
    hard_deadline_ns: int,
    budget_seconds: int,
) -> bool:
    """Apply the frozen, half-open E2 activation interval."""

    anchor = _uint64(final_common_command_ns, "v13 final common E2 command")
    activation = _uint64(activation_ns, "v13 E2 activation")
    hard = _uint64(hard_deadline_ns, "v13 hard deadline")
    budget = _integer(budget_seconds, "v13 E2 activation budget", 1)
    if budget != 90:
        _error("v13 E2 activation budget drifted")
    budget_ns = budget * 1_000_000_000
    return anchor < activation < anchor + budget_ns and activation < hard


def _v13_readiness_ledger_document(
    *,
    profile: FocusedProfile | Mapping[str, Any] | object,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    """Reconstruct the two CERT13 readiness barriers from raw events only."""

    contract = _v13_certified_activation_contract(profile)
    raw = _profile_document_value(profile)
    protocol = _document(raw.get("protocol"), "v13 protocol")
    expected_reporters = _integer(
        contract["required_reporter_count"], "v13 required reporter count", 1
    )
    expected_quorum = _integer(
        contract["certificate_quorum"], "v13 certificate quorum", 1
    )
    relevant_types = {
        "epoch.activation_prepared",
        "epoch.activation_ready_signed",
        "manager.activation_readiness_accepted",
        "manager.activation_readiness_certificate_assembled",
        "epoch.activated",
    }
    relevant: list[Mapping[str, Any]] = []
    last_sequences: dict[tuple[str, str], int] = {}
    for event in events:
        if not isinstance(event, Mapping) or event.get("event_type") not in relevant_types:
            continue
        source_kind = event.get("source_kind")
        source_instance = event.get("source_instance")
        if not isinstance(source_kind, str) or not isinstance(source_instance, str):
            _error("v13 readiness event source identity is malformed")
        sequence = _integer(event.get("source_sequence"), "v13 source sequence", 1)
        source_key = (source_kind, source_instance)
        previous = last_sequences.get(source_key, 0)
        if sequence <= previous:
            _error("v13 readiness source sequence is not strictly increasing")
        last_sequences[source_key] = sequence
        _uint64(event.get("source_monotonic_ns"), "v13 source monotonic time")
        _document(event.get("payload"), "v13 readiness payload")
        relevant.append(event)

    rows: list[dict[str, object]] = []
    for successor in (1, 2):
        epoch_events = []
        identities: list[Mapping[str, Any]] = []
        for event in relevant:
            payload = _document(event.get("payload"), "v13 readiness payload")
            identity = _document(payload.get("identity"), "v13 readiness identity")
            successor_configuration = _document(
                identity.get("successor_configuration"),
                "v13 readiness successor configuration",
            )
            if successor_configuration.get("epoch_number") == successor:
                epoch_events.append(event)
                identities.append(identity)
        if not epoch_events:
            _error(f"v13 readiness evidence is missing Epoch {successor}")
        canonical_identity = _canonical_json(identities[0])
        if any(_canonical_json(identity) != canonical_identity for identity in identities):
            _error("v13 readiness identity drifted within one transition")

        prepared: set[int] = set()
        signed: set[int] = set()
        accepted: set[int] = set()
        activated: set[int] = set()
        certificates: list[Mapping[str, Any]] = []
        certificate_digest: str | None = None
        for event in epoch_events:
            event_type = str(event["event_type"])
            payload = _document(event["payload"], "v13 readiness payload")
            if event_type.startswith("epoch."):
                replica = _integer(
                    event.get("source_replica_id"), "v13 source replica id"
                )
                if replica >= protocol["N"]:
                    _error("v13 source replica id is out of range")
                if event_type == "epoch.activation_prepared":
                    if payload.get("vote_fence_engaged") is not True:
                        _error("v13 activation preparation did not engage vote fence")
                    prepared.add(replica)
                elif event_type == "epoch.activation_ready_signed":
                    if (
                        payload.get("vote_fence_engaged") is not True
                        or payload.get("signer_replica_id") != replica
                        or payload.get("signer_source_sequence")
                        != event.get("source_sequence")
                        or payload.get("signer_monotonic_raw_ns")
                        != event.get("source_monotonic_ns")
                    ):
                        _error("v13 signed readiness observation is not source-bound")
                    signature = payload.get("signature")
                    if not isinstance(signature, str) or not signature:
                        _error("v13 readiness signature is absent")
                    signed.add(replica)
                elif event_type == "epoch.activated":
                    digest = _digest(
                        payload.get("certificate_digest"),
                        "v13 applied readiness certificate digest",
                    )
                    if certificate_digest is not None and digest != certificate_digest:
                        _error("v13 applied readiness certificate digest drifted")
                    activated.add(replica)
            elif event_type == "manager.activation_readiness_accepted":
                authenticated = _integer(
                    payload.get("authenticated_replica_id"),
                    "v13 authenticated replica id",
                )
                if payload.get("signer_replica_id") != authenticated:
                    _error("v13 manager accepted mismatched signer identity")
                accepted.add(authenticated)
            else:
                certificates.append(payload)

        if len(certificates) != 1:
            _error("v13 transition must assemble exactly one readiness certificate")
        certificate = certificates[0]
        certificate_digest = _digest(
            certificate.get("certificate_digest"),
            "v13 readiness certificate digest",
        )
        signer_ids_value = certificate.get("signer_replica_ids")
        if not isinstance(signer_ids_value, list) or any(
            type(value) is not int for value in signer_ids_value
        ):
            _error("v13 readiness certificate signer ids are malformed")
        signer_ids = set(signer_ids_value)
        if (
            len(signer_ids_value) != len(signer_ids)
            or certificate.get("certificate_quorum") != expected_quorum
            or certificate.get("required_reporter_count") != expected_reporters
        ):
            _error("v13 readiness certificate cardinality drifted")
        if not (
            prepared
            == signed
            == accepted
            == signer_ids
            == activated
            and len(signed) == expected_reporters
        ):
            _error("v13 readiness barrier is not the exact complete survivor set")
        for event in epoch_events:
            if event["event_type"] == "epoch.activated":
                payload = _document(event["payload"], "v13 activation payload")
                if payload.get("certificate_digest") != certificate_digest:
                    _error("v13 activation used a different readiness certificate")
        rows.append(
            {
                "successor_epoch_number": successor,
                "identity": json.loads(canonical_identity),
                "certificate_digest": certificate_digest,
                "observed_reporter_ids": sorted(signed),
                "activated_replica_ids": sorted(activated),
            }
        )

    ledger: dict[str, object] = {
        "schema_version": 1,
        "domain": "kauri-focused-readiness-ledger-v1",
        "profile_id": _profile_id_value(profile),
        "protocol_mode": "adaptive_v3",
        "replica_count": protocol["N"],
        "fault_bound": protocol["f"],
        "certificate_quorum": expected_quorum,
        "required_reporter_count": expected_reporters,
        "transitions": rows,
    }
    ledger["reconstruction_digest"] = _sha256(_canonical_json(ledger))
    return ledger


def _is_v8_or_v9_profile(profile: FocusedProfile | object) -> bool:
    return _is_v8_profile(profile) or _is_v9_profile(profile)


def _v10_transition_timing(profile: FocusedProfile) -> tuple[int, int]:
    """Return the frozen E1 anchor margin and E2 residence for v10."""

    transitions = _document(profile.raw.get("transitions"), "profile transitions")
    timing = transitions.get("adaptive_timing_contract")
    if not _is_v10_profile(profile):
        if timing is not None:
            _error("archived profile contains a prospective transition timing contract")
        return (0, 40_000)
    contract = _document(timing, "v10 transition timing contract")
    expected = {
        "schema_version": 1,
        "domain": "kauri-focused-v10-transition-timing-v1",
        "epoch1_common_commit_anchor_deadline_seconds": 5,
        "optimization_minimum_predecessor_residency_ms": 65_000,
    }
    if contract != expected:
        _error("v10 transition timing contract drifted")
    measurement = _document(profile.raw.get("measurement"), "profile measurement")
    phase = _document(
        measurement.get("phase_window_contract"), "phase-window contract"
    )
    bucket_seconds = _integer(
        measurement.get("bucket_width_seconds"), "bucket width", 1
    )
    stabilization_seconds = _integer(
        phase.get("stabilization_offset_seconds"), "phase stabilization offset", 1
    )
    stable_seconds = _integer(
        _document(profile.raw.get("timers"), "profile timers").get(
            "stable_phase_seconds"
        ),
        "stable phase",
        1,
    )
    if (
        contract["epoch1_common_commit_anchor_deadline_seconds"] != bucket_seconds
        or contract["optimization_minimum_predecessor_residency_ms"]
        != (stabilization_seconds + stable_seconds + bucket_seconds) * 1_000
    ):
        _error("v10 transition timing contract is not phase-derived")
    return (bucket_seconds * 1_000_000_000, 65_000)


def _reporter_capacity_document(
    *, replica_count: int, fanout: int, targets: Sequence[int], prefix: Sequence[int]
) -> dict[str, object]:
    """Derive the public topology-only reporter capacity for one fault arm."""

    leaf_start = (replica_count - 1 + fanout - 1) // fanout
    rows: list[dict[str, object]] = []
    crashed = set(targets)
    for target in sorted(targets):
        reporters: list[dict[str, object]] = []
        for reporter in range(replica_count):
            if reporter in crashed:
                continue
            relations: list[dict[str, object]] = []
            for root in prefix:
                if root in crashed:
                    continue
                position = (target - root) % replica_count
                if (
                    position == 0
                    or _cyclic_parent(replica_count, fanout, root, target) != reporter
                ):
                    continue
                relations.append(
                    {
                        "tree_id": root,
                        "expected_message_type": (
                            "aggregate_relay"
                            if position < leaf_start
                            else "direct_vote"
                        ),
                    }
                )
            if relations:
                reporters.append({"reporter_id": reporter, "tree_relations": relations})
        rows.append({"target_replica_id": target, "eligible_reporters": reporters})
    return {
        "schema_version": 1,
        "domain": "kauri-topology-fault-window-reporter-capacity-v1",
        "reporter_selection_basis": "any_topology_valid_in_prefix_v1",
        "prefix_tree_ids": list(prefix),
        "minimum_topology_eligible_reporter_capacity": min(
            len(row["eligible_reporters"]) for row in rows
        ),
        "targets": rows,
    }


def _v12_configuration_coverage_raw_deadline_ns(
    arm_document: Mapping[str, object],
    coverage: Mapping[str, object],
) -> int:
    """Derive the exact half-open raw-clock cap from the fault receipt."""

    return _integer(
        arm_document.get("evidence_start_monotonic_ns"),
        "fault-window evidence start",
        1,
    ) + _integer(
        _document(coverage["deadlines_seconds"], "coverage deadlines").get(
            "configuration_coverage_seconds"
        ),
        "post-fault configuration coverage deadline",
        1,
    ) * 1_000_000_000


def _v12_configuration_coverage_deadline(
    arm_document: Mapping[str, object],
    coverage: Mapping[str, object],
    *,
    postfault_hard_deadline: float,
) -> float:
    """Translate the receipt's raw-clock cap to the live monotonic waiter."""

    raw_deadline_ns = _v12_configuration_coverage_raw_deadline_ns(
        arm_document, coverage
    )
    remaining_seconds = max(
        0.0,
        (raw_deadline_ns - profiled_fault_runtime.monotonic_raw_ns())
        / 1_000_000_000,
    )
    return min(postfault_hard_deadline, time.monotonic() + remaining_seconds)


def _all_candidate_reporter_capacity_document(
    *,
    replica_count: int,
    quorum: int,
    fanout: int,
    unavailable: Sequence[int],
    prefix: Sequence[int],
) -> dict[str, object]:
    """Derive v12 relations for every candidate under the injected crash set."""

    leaf_start = (replica_count - 1 + fanout - 1) // fanout
    excluded = set(unavailable)
    rows: list[dict[str, object]] = []
    for target in range(replica_count):
        reporters: list[dict[str, object]] = []
        for reporter in range(replica_count):
            if reporter in excluded:
                continue
            relations: list[dict[str, object]] = []
            for root in prefix:
                if root in excluded:
                    continue
                position = (target - root) % replica_count
                if (
                    position == 0
                    or _cyclic_parent(replica_count, fanout, root, target) != reporter
                ):
                    continue
                relations.append(
                    {
                        "tree_id": root,
                        "expected_message_type": (
                            "aggregate_relay"
                            if position < leaf_start
                            else "direct_vote"
                        ),
                    }
                )
            if relations:
                reporters.append({"reporter_id": reporter, "tree_relations": relations})
        rows.append({"target_replica_id": target, "eligible_reporters": reporters})
    capacities = [len(row["eligible_reporters"]) for row in rows]
    maximum_guarded_cohort_size = replica_count - quorum
    remaining_capacities = [
        len(row["eligible_reporters"])
        - min(
            len(row["eligible_reporters"]),
            maximum_guarded_cohort_size
            - len(excluded | {int(row["target_replica_id"])}),
        )
        for row in rows
    ]
    injected_target_remaining_capacities = [
        remaining
        for row, remaining in zip(rows, remaining_capacities, strict=True)
        if int(row["target_replica_id"]) in excluded
    ]
    return {
        "schema_version": 1,
        "domain": "kauri-topology-all-candidate-reporter-capacity-v1",
        "reporter_selection_basis": "any_topology_valid_in_prefix_v1",
        "unavailable_replica_ids": sorted(excluded),
        "prefix_tree_ids": list(prefix),
        "maximum_guarded_cohort_size": maximum_guarded_cohort_size,
        "minimum_topology_eligible_reporter_capacity": min(capacities),
        "maximum_topology_eligible_reporter_capacity": max(capacities),
        "minimum_remaining_reporter_capacity": min(remaining_capacities),
        "minimum_injected_target_remaining_reporter_capacity": min(
            injected_target_remaining_capacities
        ),
        "candidates": rows,
    }


def _fault_window_arm_path(run_directory: Path) -> Path:
    """Return the one prospective arm path, confined to this child root."""

    root = run_directory.resolve()
    path = (root / "runtime" / _FAULT_WINDOW_ARM_FILENAME).resolve()
    if path.parent != (root / "runtime").resolve():
        _error("fault-window arm path escapes the child runtime root")
    return path


def _publish_fault_window_arm(path: Path, arm: Mapping[str, object]) -> str:
    """Publish one canonical arm without exposing a partial or replacement file."""

    common = {
        "schema_version",
        "kind",
        "run_id",
        "profile_id",
        "profile_sha256",
        "topology_proof_sha256",
        "request_sha256",
        "epoch_number",
        "epoch_digest",
        "fault_receipt_sha256",
        "evidence_start_monotonic_ns",
        "prefault_tree_id",
        "required_tree_positions",
        "required_tree_ids",
    }
    schema_version = arm.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2, 3, 4}:
        _error("fault-window arm schema version drifted")
    v2 = {
        "clock_domain",
        "required_observation_schema",
        "timeout_evidence_basis",
    }
    v3 = v2 | {"snapshot_evidence_basis"}
    v4 = v3 | {"selection_cardinality_policy"}
    if set(arm) != common | (
        v4
        if schema_version == 4
        else v3 if schema_version == 3 else v2 if schema_version == 2 else set()
    ):
        _error("fault-window arm schema drifted")
    if arm.get("kind") != (
        _FAULT_WINDOW_ARM_DOMAIN_V4
        if schema_version == 4
        else (
            _FAULT_WINDOW_ARM_DOMAIN_V3
            if schema_version == 3
            else (
                _FAULT_WINDOW_ARM_DOMAIN_V2
                if schema_version == 2
                else _FAULT_WINDOW_ARM_DOMAIN_V1
            )
        )
    ):
        _error("fault-window arm kind drifted")
    for key in (
        "run_id",
        "profile_id",
    ):
        if not isinstance(arm.get(key), str) or not arm[key]:
            _error(f"fault-window arm {key} drifted")
    for key in (
        "profile_sha256",
        "topology_proof_sha256",
        "request_sha256",
        "epoch_digest",
        "fault_receipt_sha256",
    ):
        _digest(arm.get(key), f"fault-window arm {key}")
    for key, minimum in (
        ("epoch_number", 0),
        ("evidence_start_monotonic_ns", 1),
        ("prefault_tree_id", 0),
        ("required_tree_positions", 1),
    ):
        _integer(arm.get(key), f"fault-window arm {key}", minimum)
    required_ids = arm.get("required_tree_ids")
    if (
        not isinstance(required_ids, list)
        or len(required_ids) != arm["required_tree_positions"]
        or any(type(tree_id) is not int or tree_id < 0 for tree_id in required_ids)
        or len(set(required_ids)) != len(required_ids)
    ):
        _error("fault-window arm required tree IDs drifted")
    if schema_version in {2, 3, 4} and (
        arm.get("clock_domain") != "same_host_clock_monotonic_raw"
        or arm.get("timeout_evidence_basis") != "exact_timeout_attempt_id_v1"
        or type(arm.get("required_observation_schema")) is not int
        or arm.get("required_observation_schema") != 3
    ):
        _error("fault-window arm v2 timeout evidence contract drifted")
    if (
        schema_version in {3, 4}
        and arm.get("snapshot_evidence_basis") != "exact_post_fault_attempt_start_v1"
    ):
        _error("fault-window arm v3 snapshot evidence contract drifted")
    if (
        schema_version == 4
        and arm.get("selection_cardinality_policy")
        != "all_guarded_up_to_fault_bound_v1"
    ):
        _error("fault-window arm v4 cardinality policy drifted")
    payload = _canonical_json(arm)
    parent = path.parent
    if (
        not path.is_absolute()
        or path.exists()
        or path.is_symlink()
        or not parent.is_dir()
    ):
        _error("fault-window arm destination is not an absent regular child path")
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # link(2) is atomic and fails with EEXIST, unlike replace(2).
        os.link(temporary, path)
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError as exc:
        raise FocusedCrashPairRuntimeError(
            "fault-window arm cannot replace an existing destination"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return _sha256(payload)


def _document(value: object, label: str) -> Mapping[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if not isinstance(value, Mapping):
        _error(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _error(f"{label} must be a sequence")
    return value


def _validate_controller_failure_terminal(
    payload: Mapping[str, Any], *, require_for_unhealthy: bool
) -> bool:
    """Validate the diagnostic-only controller failure projection."""

    reason = payload.get("reason")
    arm_diagnostics = {
        "fault_window_arm_missing",
        "fault_window_arm_invalid",
        "fault_window_arm_io_failure",
    }
    if isinstance(reason, str) and reason.startswith("fault_window_arm_"):
        return reason in arm_diagnostics and payload.get("controller_failure") is None
    unhealthy = reason == "controller_unhealthy"
    present = "controller_failure" in payload
    detail = payload.get("controller_failure")
    if (
        (require_for_unhealthy and unhealthy and not present)
        or (unhealthy and present and detail is None)
        or (not unhealthy and detail is not None)
    ):
        return False
    if detail is None:
        return True
    if not isinstance(detail, Mapping) or set(detail) != {
        "stage",
        "selection_status",
        "epoch_factory_status",
    }:
        return False
    fatal_selection_statuses = {
        "invalid_state",
        "invalid_cutoff",
        "ledger_unhealthy",
        "mixed_epoch",
        "nonmember_evidence",
        "projection_failed",
        "capacity_exceeded",
        "guarded_candidate_bound_exceeded",
        "snapshot_failed",
        "internal_failure",
    }
    factory_statuses = {
        "invalid_current_epoch",
        "epoch_number_exhausted",
        "invalid_selection",
        "epoch_mismatch",
        "membership_mismatch",
        "root_mismatch",
        "tree_count_mismatch",
        "insufficient_leaf_capacity",
        "invalid_activation_delay",
        "capacity_exceeded",
        "placement_failed",
        "authorization_failed",
        "bundle_failed",
        "internal_failure",
    }
    stage = detail.get("stage")
    selection = detail.get("selection_status")
    factory = detail.get("epoch_factory_status")
    if stage == "operational_precondition":
        return selection is None and factory is None
    if stage == "baseline_selection":
        return (
            selection in fatal_selection_statuses | {"baseline_frozen"}
            and factory is None
        )
    if stage == "guarded_selection":
        return selection in fatal_selection_statuses and factory is None
    return (
        stage == "successor_factory"
        and selection == "selected"
        and factory in factory_statuses
    )


def _validate_v4_manager_terminal_payload(payload: Mapping[str, Any]) -> bool:
    """Validate the complete v4 terminal projection, including arm failures."""

    keys = {
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
        "controller_failure",
    }
    if set(payload) != keys:
        return False

    def uint(value: object, maximum: int) -> bool:
        return type(value) is int and 0 <= value <= maximum

    def digest(value: object, *, nonzero: bool = False) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
            and (not nonzero or value != "0" * 64)
        )

    if (
        not uint(payload["cycle_ordinal"], (1 << 64) - 1)
        or payload["policy_intent"]
        not in {"fault_containment", "performance_optimization"}
        or payload["outcome"] not in {"advanced", "no_op", "failed"}
        or payload["reason"]
        not in {
            "successor_converged",
            "explicit_no_op",
            "controller_unhealthy",
            "convergence_start_failed",
            "convergence_retry_exhausted",
            "convergence_conflicting_observation",
            "invalid_terminal_identity",
            "successor_rotation_failed",
            "evidence_window_reset_failed",
            "caller_failed",
            "fault_window_arm_missing",
            "fault_window_arm_invalid",
            "fault_window_arm_io_failure",
        }
        or payload["transition_artifact_id"]
        not in {"e0-to-e1-containment", "e1-to-e2-optimization"}
        or not uint(payload["predecessor_epoch_number"], (1 << 32) - 1)
        or not digest(payload["predecessor_epoch_digest"], nonzero=True)
        or not uint(payload["evidence_window_activation_generation"], (1 << 64) - 1)
        or payload["evidence_window_activation_generation"] == 0
        or not uint(payload["baseline_evidence_cutoff"], (1 << 64) - 1)
        or not uint(payload["current_evidence_cutoff"], (1 << 64) - 1)
        or payload["baseline_evidence_cutoff"] > payload["current_evidence_cutoff"]
    ):
        return False

    successor = (
        payload["successor_epoch_number"],
        payload["successor_epoch_digest"],
        payload["command_payload_digest"],
    )
    if successor != (None, None, None) and not (
        uint(successor[0], (1 << 32) - 1)
        and digest(successor[1], nonzero=True)
        and digest(successor[2], nonzero=True)
    ):
        return False
    if payload["winning_activation"] is not None and not isinstance(
        payload["winning_activation"], Mapping
    ):
        return False

    if payload["reason"] in {
        "fault_window_arm_missing",
        "fault_window_arm_invalid",
        "fault_window_arm_io_failure",
    }:
        cutoff_shape_is_valid = (
            payload["current_evidence_cutoff"] == payload["baseline_evidence_cutoff"]
            if payload["reason"] == "fault_window_arm_missing"
            else payload["current_evidence_cutoff"]
            >= payload["baseline_evidence_cutoff"]
        )
        return (
            payload["outcome"] == "failed"
            and payload["cycle_ordinal"] == 0
            and payload["policy_intent"] == "fault_containment"
            and payload["transition_artifact_id"] == "e0-to-e1-containment"
            and payload["predecessor_epoch_number"] == 0
            and payload["evidence_window_activation_generation"] == 1
            and payload["baseline_evidence_cutoff"] > 0
            and cutoff_shape_is_valid
            and successor == (None, None, None)
            and payload["winning_activation"] is None
            and payload["controller_failure"] is None
        )
    return True


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _error(f"{label} must be an integer >= {minimum}")
    return value


def _uint64(value: object, label: str, minimum: int = 0) -> int:
    result = _integer(value, label, minimum)
    if result > (1 << 64) - 1:
        _error(f"{label} exceeds uint64")
    return result


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
            profiled_fault_runtime.write_exclusive(private_path, private, mode=0o600)
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


def _v13_membership_digest(members: Sequence[Mapping[str, object]]) -> str:
    encoded = bytearray(b"kauri-adaptive-v3-activation-readiness-membership-v1")
    encoded.extend(len(members).to_bytes(4, "big"))
    seen: set[int] = set()
    public_keys: set[str] = set()
    for expected_replica, member in enumerate(members):
        replica = _integer(member.get("replica_id"), "v13 manifest replica id")
        public = member.get("public_key_hex")
        if replica != expected_replica or replica in seen:
            _error("v13 public manifest membership is not canonical")
        if (
            not isinstance(public, str)
            or len(public) != 96
            or any(character not in "0123456789abcdef" for character in public)
            or public in public_keys
        ):
            _error("v13 readiness public key is not canonical BLS hex")
        seen.add(replica)
        public_keys.add(public)
        encoded.extend(replica.to_bytes(2, "big"))
        encoded.extend(bytes.fromhex(public))
    return _sha256(bytes(encoded))


def _v13_public_manifest_bytes(
    profile: FocusedProfile | Mapping[str, Any] | object,
    members: Sequence[Mapping[str, object]],
) -> bytes:
    _v13_certified_activation_contract(profile)
    raw = _profile_document_value(profile)
    replica_count = _integer(
        _document(raw.get("protocol"), "v13 protocol").get("N"),
        "v13 replica count",
        1,
    )
    if len(members) != replica_count:
        _error("v13 public manifest cardinality drifted")
    profile_sha = getattr(profile, "profile_sha256", raw.get("profile_sha256"))
    profile_sha = _digest(profile_sha, "v13 profile digest")
    document = {
        "algorithm": "bls-pop",
        "domain": "kauri-adaptive-v3-readiness-public-key-manifest-v1",
        "membership_digest": _v13_membership_digest(members),
        "members": [dict(member) for member in members],
        "profile_id": _profile_id_value(profile),
        "profile_sha256": profile_sha,
        "protocol_mode": "adaptive_v3",
        "schema_version": 1,
    }
    return _canonical_json(document)


def _read_exact_private_scalar(path: Path, *, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FocusedCrashPairRuntimeError(f"{label} cannot be opened safely") from exc
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o777 != 0o600:
            _error(f"{label} must be one regular 0600 file")
        if status.st_size != 65:
            _error(f"{label} has invalid size")
        chunks: list[bytes] = []
        remaining = 65
        while remaining:
            try:
                chunk = os.read(descriptor, remaining)
            except InterruptedError:
                continue
            if not chunk:
                _error(f"{label} ended early")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _error(f"{label} contains trailing bytes")
        value = b"".join(chunks)
    finally:
        os.close(descriptor)
    if value[-1:] != b"\n" or any(
        byte not in b"0123456789abcdef" for byte in value[:-1]
    ):
        _error(f"{label} is not canonical lowercase scalar hex")
    return value


def _sha256_regular_file_no_follow(path: Path, *, label: str) -> str:
    """Hash one authorized executable without following a replacement link."""

    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            _error(f"{label} is not a regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    except FocusedCrashPairRuntimeError:
        raise
    except OSError as exc:
        raise FocusedCrashPairRuntimeError(f"cannot read {label}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(slots=True)
class FocusedV13ReadinessAllocator:
    """Preallocate pair-bound BLS readiness keys before authorization."""

    allocation_root: Path
    pair_count: int
    keygen_binary: Path
    profile: FocusedProfile | Mapping[str, Any] | object
    run_command: Callable[..., Any] = subprocess.run

    def allocate(self) -> dict[str, object]:
        _v13_certified_activation_contract(self.profile)
        raw = _profile_document_value(self.profile)
        count = _integer(
            _document(raw.get("protocol"), "v13 protocol").get("N"),
            "v13 replica count",
            1,
        )
        pair_count = _integer(self.pair_count, "v13 readiness pair count", 1)
        root = Path(self.allocation_root).absolute()
        if root.exists() or root.is_symlink():
            _error("v13 readiness allocation root already exists")
        root.mkdir(mode=0o700)
        pairs: dict[str, object] = {}
        manifest_digests: set[str] = set()
        # Each pair becomes an independently authorized live run, but its
        # readiness identities share this allocation authority.  A key that
        # appears in just one row of another pair is therefore as unsafe as a
        # duplicate manifest: both would create ambiguous BLS ownership.
        allocation_public_keys: set[str] = set()
        allocation_private_scalars: set[str] = set()
        for ordinal in range(1, pair_count + 1):
            pair_id = f"pair-{ordinal:02d}"
            pair_root = root / pair_id
            pair_root.mkdir(mode=0o700)
            result = self.run_command(
                (
                    str(Path(self.keygen_binary).resolve()),
                    "--secure-bls-preallocation",
                    "--algo",
                    "bls",
                    "--num",
                    str(count),
                ),
                cwd=pair_root,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0 or result.stderr:
                _error(f"v13 readiness key preallocation failed for {pair_id}")
            identities = profiled_fault_runtime._parse_identity_output(
                result.stdout,
                expected_count=count,
                expected_fields=frozenset({"pub", "sec"}),
                label=f"{pair_id} readiness keygen",
            )
            members: list[dict[str, object]] = []
            private_records: list[dict[str, object]] = []
            for replica, identity in enumerate(identities):
                public = str(identity["pub"])
                secret = str(identity["sec"])
                if (
                    len(public) != 96
                    or len(secret) != 64
                    or any(c not in "0123456789abcdef" for c in public + secret)
                    or public in allocation_public_keys
                    or secret in allocation_private_scalars
                ):
                    _error("v13 readiness keygen output is malformed or duplicate")
                allocation_public_keys.add(public)
                allocation_private_scalars.add(secret)
                private_path = pair_root / f"replica-{replica}.sec"
                private_bytes = f"{secret}\n".encode("ascii")
                profiled_fault_runtime.write_exclusive(
                    private_path, private_bytes, mode=0o600
                )
                members.append({"public_key_hex": public, "replica_id": replica})
                private_records.append(
                    {
                        "private_key_path": str(private_path),
                        "private_key_sha256": _sha256(private_bytes),
                        "public_key_hex": public,
                        "replica_id": replica,
                    }
                )
            manifest = _v13_public_manifest_bytes(self.profile, members)
            manifest_path = pair_root / "readiness-public-manifest.json"
            profiled_fault_runtime.write_exclusive(manifest_path, manifest, mode=0o600)
            manifest_sha = _sha256(manifest)
            if manifest_sha in manifest_digests:
                _error("v13 readiness preallocation duplicated a pair manifest")
            manifest_digests.add(manifest_sha)
            pairs[pair_id] = {
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_sha,
                "membership_digest": json.loads(manifest)["membership_digest"],
                "members": members,
                "private_allocations": private_records,
            }
        return {"pair_readiness_allocations": pairs}


def _profile_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    identity = json.loads(json.dumps(dict(raw)))
    topology = identity.get("topology")
    if not isinstance(topology, dict):
        _error("profile topology must be an object")
    topology.pop("proof_sha256", None)
    return identity


def _v7_n31_target_selection_metric() -> dict[str, object]:
    """Independently derive the frozen N31 survivor-path score table."""

    replica_count = 31
    fanout = 5
    candidates = (21, 22, 23, 24, 25)
    prefix = (20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 0, 1, 2, 3, 4)
    rows: list[tuple[tuple[int, int, int], int, int, int]] = []
    for targets in combinations(candidates, 3):
        per_tree: list[int] = []
        for root in prefix:
            shadow = 0
            for member in range(replica_count):
                if member in targets:
                    continue
                position = (member - root) % replica_count
                while True:
                    if (root + position) % replica_count in targets:
                        shadow += 1
                        break
                    if position == 0:
                        break
                    position = (position - 1) // fanout
            per_tree.append(shadow)
        fixed = sum(per_tree[prefix.index(target)] for target in targets)
        rows.append((targets, sum(per_tree), fixed, max(per_tree)))
    minimum = min(total for _targets, total, _fixed, _maximum in rows)
    selected = min(
        targets for targets, total, _fixed, _maximum in rows if total == minimum
    )
    return {
        "schema_version": 1,
        "domain": "kauri-topology-survivor-path-shadow-v1",
        "candidate_internal_replica_ids": list(candidates),
        "prefix_tree_ids": list(prefix),
        "fanout": fanout,
        "bfs_member_order": list(range(20, 31)) + list(range(20)),
        "triple_scores": [
            {
                "target_replica_ids": list(targets),
                "total_survivor_path_shadow": total,
                "fixed_root_shadow": fixed,
                "collateral_survivor_path_shadow": total - fixed,
                "maximum_per_tree_survivor_path_shadow": maximum,
            }
            for targets, total, fixed, maximum in rows
        ],
        "selected_target_replica_ids": list(selected),
        "tie_break": "lexicographic_replica_id",
    }


def _v8_n31_target_selection_metric() -> dict[str, object]:
    """Independently derive the H17 topology-only survivor-path table."""

    replica_count, fanout = 31, 5
    candidates = (21, 22, 23, 24, 25)
    prefix = (20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 0, 1, 2, 3, 4, 5)
    rows: list[tuple[tuple[int, int, int], int, int, int]] = []
    for targets in combinations(candidates, 3):
        per_tree: list[int] = []
        for root in prefix:
            shadow = 0
            for member in range(replica_count):
                if member in targets:
                    continue
                position = (member - root) % replica_count
                while True:
                    if (root + position) % replica_count in targets:
                        shadow += 1
                        break
                    if position == 0:
                        break
                    position = (position - 1) // fanout
            per_tree.append(shadow)
        fixed = sum(per_tree[prefix.index(target)] for target in targets)
        rows.append((targets, sum(per_tree), fixed, max(per_tree)))
    selected = min(
        targets
        for targets, total, _fixed, _maximum in rows
        if total == min(row[1] for row in rows)
    )
    return {
        "schema_version": 1,
        "domain": "kauri-topology-survivor-path-shadow-v1",
        "candidate_internal_replica_ids": list(candidates),
        "prefix_tree_ids": list(prefix),
        "fanout": fanout,
        "bfs_member_order": list(range(20, 31)) + list(range(20)),
        "triple_scores": [
            {
                "target_replica_ids": list(targets),
                "total_survivor_path_shadow": total,
                "fixed_root_shadow": fixed,
                "collateral_survivor_path_shadow": total - fixed,
                "maximum_per_tree_survivor_path_shadow": maximum,
            }
            for targets, total, fixed, maximum in rows
        ],
        "selected_target_replica_ids": list(selected),
        "tie_break": "lexicographic_replica_id",
    }


def _v12_n31_target_selection_metric() -> dict[str, object]:
    """Independently derive the v12 full-cycle survivor-path score table."""

    replica_count, fanout = 31, 5
    candidates = (21, 22, 23, 24, 25)
    prefix = tuple(range(20, 31)) + tuple(range(20))
    rows: list[tuple[tuple[int, int, int], int, int, int]] = []
    for targets in combinations(candidates, 3):
        per_tree: list[int] = []
        for root in prefix:
            shadow = 0
            for member in range(replica_count):
                if member in targets:
                    continue
                position = (member - root) % replica_count
                while True:
                    if (root + position) % replica_count in targets:
                        shadow += 1
                        break
                    if position == 0:
                        break
                    position = (position - 1) // fanout
            per_tree.append(shadow)
        fixed = sum(per_tree[prefix.index(target)] for target in targets)
        rows.append((targets, sum(per_tree), fixed, max(per_tree)))
    selected = min(
        targets
        for targets, total, _fixed, _maximum in rows
        if total == min(row[1] for row in rows)
    )
    return {
        "schema_version": 1,
        "domain": "kauri-topology-survivor-path-shadow-v1",
        "candidate_internal_replica_ids": list(candidates),
        "prefix_tree_ids": list(prefix),
        "fanout": fanout,
        "bfs_member_order": list(range(20, 31)) + list(range(20)),
        "triple_scores": [
            {
                "target_replica_ids": list(targets),
                "total_survivor_path_shadow": total,
                "fixed_root_shadow": fixed,
                "collateral_survivor_path_shadow": total - fixed,
                "maximum_per_tree_survivor_path_shadow": maximum,
            }
            for targets, total, fixed, maximum in rows
        ],
        "selected_target_replica_ids": list(selected),
        "tie_break": "lexicographic_replica_id",
    }


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
        order[index] for index in nonroot_internal if depths[index] == deepest_depth
    ]
    target_descendants = [set(expected_descendants[str(target)]) for target in targets]
    pairwise_disjoint = all(
        left.isdisjoint(right)
        for index, left in enumerate(target_descendants)
        for right in target_descendants[index + 1 :]
    )
    expected_derivation: dict[str, object] = {
        "deepest_member_ids": deepest,
        "selected_target_replica_ids": list(targets),
        "pairwise_disjoint": True,
    }
    if profile.get("profile_id") in {
        "n31-f5-q21-three-crash-pair-v7",
        "n31-f5-q21-three-crash-pair-v8",
        "n31-f5-q21-three-crash-pair-v9",
        "n31-f5-q21-three-crash-pair-v10",
        "n31-f5-q21-three-crash-pair-v11",
        "n31-f5-q21-three-crash-pair-v12",
        "n31-f5-q21-three-crash-pair-v13",
    }:
        metric = topology.get("target_selection_metric")
        expected_metric = (
            _v12_n31_target_selection_metric()
            if str(profile.get("profile_id", "")).endswith(("-v12", "-v13"))
            else (
                _v8_n31_target_selection_metric()
                if str(profile.get("profile_id", "")).endswith(
                    ("-v8", "-v9", "-v10", "-v11")
                )
                else _v7_n31_target_selection_metric()
            )
        )
        if metric != expected_metric:
            _error("v7 topology target selection metric is absent")
        expected_derivation["target_selection_metric"] = expected_metric
    elif (
        "target_selection_metric" in topology or "target_selection_metric" in derivation
    ):
        _error("archived topology contains a prospective target selection metric")
    if str(profile.get("profile_id", "")).endswith(
        ("-v8", "-v9", "-v10", "-v11", "-v12", "-v13")
    ):
        arm = _document(profile.get("fault_window_arm"), "fault-window arm metadata")
        expected_capacity = _reporter_capacity_document(
            replica_count=replica_count,
            fanout=fanout,
            targets=targets,
            prefix=_sequence(arm.get("ordered_tree_prefix"), "fault-window prefix"),
        )
        if (
            topology.get("reporter_coverage_capacity") != expected_capacity
            or derivation.get("reporter_coverage_capacity") != expected_capacity
        ):
            _error("v8 topology reporter capacity differs from topology derivation")
        expected_derivation["reporter_coverage_capacity"] = expected_capacity
    if str(profile.get("profile_id", "")) in {
        "n31-f5-q21-three-crash-pair-v12",
        "n31-f5-q21-three-crash-pair-v13",
    }:
        arm = _document(profile.get("fault_window_arm"), "fault-window arm metadata")
        expected_all = _all_candidate_reporter_capacity_document(
            replica_count=replica_count,
            quorum=_integer(protocol.get("Q"), "quorum", 1),
            fanout=fanout,
            unavailable=targets,
            prefix=_sequence(arm.get("ordered_tree_prefix"), "fault-window prefix"),
        )
        if (
            topology.get("all_candidate_reporter_coverage_capacity") != expected_all
            or derivation.get("all_candidate_reporter_coverage_capacity")
            != expected_all
            or expected_all["minimum_topology_eligible_reporter_capacity"] != 19
            or expected_all["maximum_topology_eligible_reporter_capacity"] != 23
            or expected_all["maximum_guarded_cohort_size"] != 10
            or expected_all["minimum_remaining_reporter_capacity"] != 13
            or expected_all[
                "minimum_injected_target_remaining_reporter_capacity"
            ]
            != 16
        ):
            _error("v12 all-candidate reporter capacity drifted")
        expected_derivation["all_candidate_reporter_coverage_capacity"] = expected_all
    if (
        members != expected_members
        or descendants != expected_descendants
        or derivation != expected_derivation
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
    schema_version = profile.get("schema_version")
    expected_keys = (
        _PROFILE_KEYS_V4
        if _is_v4_profile(
            SimpleNamespace(profile_id=str(profile.get("profile_id", "")))
        )
        else _PROFILE_KEYS_V2 if schema_version == 2 else _PROFILE_KEYS
    )
    if set(profile) != expected_keys or schema_version not in {1, 2}:
        _error("focused profile schema drifted")
    if profile.get("frozen") is not True:
        _error("focused profile is not frozen")
    profile_id = profile.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id:
        _error("focused profile ID is invalid")
    if _is_v9_profile(SimpleNamespace(profile_id=profile_id)):
        campaign = _document(profile.get("campaign"), "v9 campaign contract")
        support = _document(
            campaign.get("scientific_support_contract"),
            "v9 scientific-support contract",
        )
        if support != {
            "schema_version": 1,
            "domain": "kauri-focused-campaign-scientific-support-v1",
        }:
            _error("v9 scientific-support campaign contract drifted")
    elif "scientific_support_contract" in _document(
        profile.get("campaign"), "campaign contract"
    ):
        _error("archived campaign profile contains a prospective support contract")
    if _is_v4_profile(SimpleNamespace(profile_id=profile_id)):
        arm = _document(profile.get("fault_window_arm"), "fault-window arm metadata")
        blinding = _document(profile.get("blinding"), "profile blinding")
        topology = _document(profile.get("topology"), "profile topology")
        positions = _integer(
            arm.get("required_postfault_tree_positions"),
            "fault-window metadata tree positions",
            1,
        )
        replica_count = _integer(
            _document(profile.get("protocol"), "profile protocol").get("N"),
            "replica count",
            1,
        )
        prefix = arm.get("ordered_tree_prefix")
        expected_arm_keys = {
            "schema_version",
            "domain",
            "manager_visibility",
            "ordered_tree_prefix",
            "required_for_new_executions",
            "required_postfault_tree_positions",
        }
        if (
            _is_v6_profile(SimpleNamespace(profile_id=profile_id))
            or _is_v7_profile(SimpleNamespace(profile_id=profile_id))
            or _is_v8_or_v9_profile(SimpleNamespace(profile_id=profile_id))
        ):
            expected_arm_keys |= {
                "clock_domain",
                "required_observation_schema",
                "timeout_evidence_basis",
            }
        if _is_v7_profile(
            SimpleNamespace(profile_id=profile_id)
        ) or _is_v8_or_v9_profile(SimpleNamespace(profile_id=profile_id)):
            expected_arm_keys.add("snapshot_evidence_basis")
        if _is_v9_profile(SimpleNamespace(profile_id=profile_id)):
            expected_arm_keys.add("selection_cardinality_policy")
        if (
            set(arm) != expected_arm_keys
            or type(arm.get("schema_version")) is not int
            or arm.get("schema_version")
            != (
                4
                if _is_v9_profile(SimpleNamespace(profile_id=profile_id))
                else (
                    3
                    if _is_v7_profile(SimpleNamespace(profile_id=profile_id))
                    or _is_v8_profile(SimpleNamespace(profile_id=profile_id))
                    else (
                        2
                        if _is_v6_profile(SimpleNamespace(profile_id=profile_id))
                        else 1
                    )
                )
            )
            or arm.get("domain") != "epoch_zero_native_cyclic_tree_positions"
            or arm.get("manager_visibility")
            != "target-identity/process-state blind; intervention-boundary aware"
            or arm.get("required_for_new_executions") is not True
            or blinding.get("manager_input_source")
            != "authenticated_runtime_evidence_plus_bound_fault_window_arm"
            or positions > replica_count
            or not isinstance(prefix, list)
            or any(type(tree) is not int for tree in prefix)
            or len(prefix) != len(set(prefix))
            or prefix
            != [
                (topology.get("active_tree_id") + offset) % replica_count
                for offset in range(positions)
            ]
        ):
            _error("fault-window arm metadata drifted")
        if (
            _is_v6_profile(SimpleNamespace(profile_id=profile_id))
            or _is_v7_profile(SimpleNamespace(profile_id=profile_id))
            or _is_v8_or_v9_profile(SimpleNamespace(profile_id=profile_id))
        ) and (
            arm.get("clock_domain") != "same_host_clock_monotonic_raw"
            or arm.get("required_observation_schema") != 3
            or arm.get("timeout_evidence_basis") != "exact_timeout_attempt_id_v1"
        ):
            _error("v6 fault-window timeout evidence metadata drifted")
        if (
            _is_v7_profile(SimpleNamespace(profile_id=profile_id))
            or _is_v8_or_v9_profile(SimpleNamespace(profile_id=profile_id))
        ) and arm.get("snapshot_evidence_basis") != "exact_post_fault_attempt_start_v1":
            _error("v7 fault-window snapshot evidence metadata drifted")
        if (
            _is_v9_profile(SimpleNamespace(profile_id=profile_id))
            and arm.get("selection_cardinality_policy")
            != "all_guarded_up_to_fault_bound_v1"
        ):
            _error("v9 fault-window selection cardinality policy drifted")
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
        or any(
            type(target) is not int or target not in range(replica_count)
            for target in targets
        )
    ):
        _error("profile reviewed targets are invalid")
    fault = _document(profile.get("fault"), "profile fault")
    if fault.get("target_count") != len(targets):
        _error("profile fault cardinality differs from the proven targets")
    measurement = _document(profile.get("measurement"), "profile measurement")
    expected_measurement_keys = {
        "authoritative_replica_id",
        "bucket_width_seconds",
        "commit_event_type",
        "phase_names",
    }
    if _is_v5_profile(SimpleNamespace(profile_id=profile_id)):
        expected_measurement_keys.add("phase_window_contract")
    if set(measurement) != expected_measurement_keys:
        _error("profile measurement schema drifted")
    if _is_v5_profile(SimpleNamespace(profile_id=profile_id)):
        phase_contract = _document(
            measurement.get("phase_window_contract"),
            "phase-window contract",
        )
        if (
            set(phase_contract)
            != {
                "schema_version",
                "domain",
                "stabilization_offset_seconds",
                "control_optimization_hold_seconds",
            }
            or phase_contract.get("schema_version") != 1
            or phase_contract.get("domain") != "kauri-focused-causal-phase-windows-v1"
            or _integer(
                phase_contract.get("stabilization_offset_seconds"),
                "phase stabilization offset",
                1,
            )
            != 30
            or _integer(
                phase_contract.get("control_optimization_hold_seconds"),
                "control optimization hold",
                1,
            )
            != 30
        ):
            _error("phase-window contract drifted")
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
    loaded = FocusedProfile(
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
    _v10_transition_timing(loaded)
    if schema_version == 2:
        derive_reporter_coverage_plan(loaded)
    return loaded


def is_before_fcrash_h_deadline(
    origin_ns: int,
    candidate_ns: int,
    deadline_seconds: int,
) -> bool:
    """Return whether a native timestamp is inside one absolute half-open cap."""

    if (
        type(origin_ns) is not int
        or type(candidate_ns) is not int
        or type(deadline_seconds) is not int
        or origin_ns < 0
        or candidate_ns < origin_ns
        or deadline_seconds <= 0
    ):
        return False
    return candidate_ns < origin_ns + deadline_seconds * 1_000_000_000


def _cyclic_parent(
    replica_count: int,
    fanout: int,
    tree_root: int,
    target: int,
) -> int | None:
    position = (target - tree_root) % replica_count
    if position == 0:
        return None
    return (tree_root + (position - 1) // fanout) % replica_count


def _v9_eligible_guard_reporters(
    profile: FocusedProfile, target: int
) -> frozenset[int]:
    """Derive reporters with at least one proof-bound relation for a v9 cohort member."""

    protocol = _document(profile.raw.get("protocol"), "profile protocol")
    arm = _document(profile.raw.get("fault_window_arm"), "fault-window arm")
    fanout = _integer(protocol.get("fanout"), "profile fanout", 1)
    prefix = _sequence(arm.get("ordered_tree_prefix"), "fault-window prefix")
    crashed = set(profile.target_replica_ids)
    replica_count = len(profile.replica_ids)
    return frozenset(
        reporter
        for reporter in profile.replica_ids
        if reporter not in crashed
        and any(
            tree_id not in crashed
            and tree_id != target
            and _cyclic_parent(replica_count, fanout, tree_id, target) == reporter
            for tree_id in prefix
        )
    )


def _v12_guard_relations(
    profile: FocusedProfile,
) -> dict[int, dict[int, frozenset[tuple[int, str]]]]:
    coverage = derive_reporter_coverage_plan(profile)
    all_candidates = coverage.get("all_candidate_reporter_coverage_capacity")
    if all_candidates is None:
        rows = _document(
            coverage.get("reporter_coverage_capacity"),
            "v12 injected-target reporter capacity",
        ).get("targets")
    else:
        rows = _document(
            all_candidates, "v12 all-candidate reporter capacity"
        ).get("candidates")
    return {
        _integer(row["target_replica_id"], "v12 candidate"): {
            _integer(reporter["reporter_id"], "v12 reporter"): frozenset(
                (
                    _integer(relation["tree_id"], "v12 relation tree"),
                    str(relation["expected_message_type"]),
                )
                for relation in reporter["tree_relations"]
            )
            for reporter in row["eligible_reporters"]
        }
        for row in _sequence(rows, "v12 guard relation rows")
    }


def _v9_optimization_roots(
    ranked_ids: Sequence[int],
    inherited_wait_exempt: Sequence[int],
    quorum: int,
) -> tuple[int, ...]:
    """Mirror native recurring selection without promoting inherited leaves."""

    inherited = set(inherited_wait_exempt)
    roots = tuple(replica for replica in ranked_ids if replica not in inherited)[:quorum]
    if len(roots) != quorum:
        _error("v9 optimization lacks Q unconstrained responsive roots")
    return roots


def _v9_inherited_cohort(profile: FocusedProfile, decoded: Any) -> tuple[int, ...]:
    """Bind every native tree to one exact inherited leaf/wait-exempt cohort."""

    if not decoded.trees:
        _error("v9 epoch lacks inherited trees")
    cohort = tuple(decoded.trees[0].wait_exempt)
    members = set(profile.replica_ids)
    fanout = int(profile.raw["protocol"]["fanout"])
    pipeline_stretch = int(profile.raw["protocol"]["pipeline_stretch"])
    first_leaf = (len(profile.replica_ids) - 2) // fanout + 1
    if (
        cohort != tuple(sorted(set(cohort)))
        or not set(profile.target_replica_ids).issubset(cohort)
        or not set(cohort).issubset(members)
        or len(cohort) > len(profile.replica_ids) - profile.quorum
        or any(
            tree.fanout != fanout
            or tree.pipeline_stretch != pipeline_stretch
            or tuple(tree.wait_exempt) != cohort
            or len(tree.members) != len(profile.replica_ids)
            or set(tree.members) != members
            or any(target not in tree.members[first_leaf:] for target in cohort)
            for tree in decoded.trees
        )
    ):
        _error("v9 epoch inherited cohort or leaf placement drifted")
    return cohort


def derive_reporter_coverage_plan(profile: FocusedProfile) -> dict[str, object]:
    """Derive and verify the frozen FCRASH-H reporter-coverage contract."""

    if type(profile) is not FocusedProfile:
        _error("reporter coverage requires a focused profile")
    raw = profile.raw
    if raw.get("schema_version") != 2:
        _error("reporter coverage requires an immutable FCRASH-H profile")
    protocol = _document(raw.get("protocol"), "profile protocol")
    topology = _document(raw.get("topology"), "profile topology")
    guard = _document(raw.get("evidence_guard"), "profile evidence guard")
    timers = _document(raw.get("timers"), "profile timers")
    expected_guard_keys = {
        "schedule",
        "tree_switch_period_blocks",
        "horizon_tree_positions",
        "required_qualifying_reporters",
        "minimum_timeouts_per_reporter",
        "minimum_score_drop",
    }
    if getattr(
        profile, "profile_id", None
    ) in _FCRASH_H_V3_PROFILE_IDS or _is_v4_profile(profile):
        expected_guard_keys.add("required_postfault_tree_positions")
    if _is_v8_or_v9_profile(profile):
        expected_guard_keys |= {
            "reporter_selection_basis",
            "minimum_topology_eligible_reporter_capacity",
        }
    expected_timer_keys = {
        "adaptation_interval_seconds",
        "stable_phase_seconds",
        "readiness_timeout_seconds",
        "manager_convergence_timeout_seconds",
        "nonresponse_evidence_deadline_seconds",
        "containment_activation_deadline_seconds",
        "optimization_activation_deadline_seconds",
        "arm_hard_deadline_seconds",
    }
    if _is_v12_profile(profile):
        expected_timer_keys.add("postfault_configuration_coverage_deadline_seconds")
    if set(guard) != expected_guard_keys or set(timers) != expected_timer_keys:
        _error("FCRASH-H guard or timer schema drifted")
    replica_count = len(profile.replica_ids)
    fanout = _integer(protocol.get("fanout"), "profile fanout", 1)
    active_tree = _integer(topology.get("active_tree_id"), "active tree")
    fault_threshold = _integer(protocol.get("f"), "fault threshold")
    required = fault_threshold + 1
    minimum_timeouts = 2
    targets = profile.target_replica_ids
    if _is_v8_or_v9_profile(profile):
        arm = _document(raw.get("fault_window_arm"), "fault-window arm metadata")
        capacity = _reporter_capacity_document(
            replica_count=replica_count,
            fanout=fanout,
            targets=targets,
            prefix=_sequence(arm.get("ordered_tree_prefix"), "fault-window prefix"),
        )
        if topology.get("reporter_coverage_capacity") != capacity:
            _error("v8 reporter capacity differs from topology derivation")
        all_candidate_capacity = None
        if profile.profile_id == "n31-f5-q21-three-crash-pair-v12":
            all_candidate_capacity = _all_candidate_reporter_capacity_document(
                replica_count=replica_count,
                quorum=profile.quorum,
                fanout=fanout,
                unavailable=targets,
                prefix=_sequence(arm.get("ordered_tree_prefix"), "fault-window prefix"),
            )
            if (
                topology.get("all_candidate_reporter_coverage_capacity")
                != all_candidate_capacity
                or all_candidate_capacity[
                    "minimum_topology_eligible_reporter_capacity"
                ]
                != 19
                or all_candidate_capacity[
                    "maximum_topology_eligible_reporter_capacity"
                ]
                != 23
                or all_candidate_capacity["maximum_guarded_cohort_size"] != 10
                or all_candidate_capacity["minimum_remaining_reporter_capacity"]
                != 13
                or all_candidate_capacity[
                    "minimum_injected_target_remaining_reporter_capacity"
                ]
                != 16
            ):
                _error("v12 all-candidate reporter capacity differs from topology")
        expected_guard = {
            "schedule": "native_cyclic_epoch_zero",
            "tree_switch_period_blocks": 2,
            "horizon_tree_positions": len(capacity["prefix_tree_ids"]),
            "required_postfault_tree_positions": len(capacity["prefix_tree_ids"]),
            "required_qualifying_reporters": required,
            "minimum_timeouts_per_reporter": minimum_timeouts,
            "minimum_score_drop": minimum_timeouts * required,
            "reporter_selection_basis": "any_topology_valid_in_prefix_v1",
            "minimum_topology_eligible_reporter_capacity": capacity[
                "minimum_topology_eligible_reporter_capacity"
            ],
        }
        if dict(guard) != expected_guard:
            _error("v8 frozen evidence guard differs from topology derivation")
        deadlines = {
            **(
                {
                    "configuration_coverage_seconds": _integer(
                        timers.get(
                            "postfault_configuration_coverage_deadline_seconds"
                        ),
                        "post-fault configuration coverage deadline",
                        1,
                    )
                }
                if _is_v12_profile(profile)
                else {}
            ),
            "evidence_seconds": _integer(
                timers.get("nonresponse_evidence_deadline_seconds"),
                "nonresponse evidence deadline",
                1,
            ),
            "epoch1_activation_seconds": _integer(
                timers.get("containment_activation_deadline_seconds"),
                "containment activation deadline",
                1,
            ),
            "optimization_activation_seconds": _integer(
                timers.get("optimization_activation_deadline_seconds"),
                "optimization activation deadline",
                1,
            ),
            "arm_hard_seconds": _integer(
                timers.get("arm_hard_deadline_seconds"), "arm hard deadline", 1
            ),
        }
        if (
            (
                _is_v12_profile(profile)
                and deadlines["configuration_coverage_seconds"]
                >= deadlines["evidence_seconds"]
            )
            or deadlines["evidence_seconds"] >= deadlines["epoch1_activation_seconds"]
            or deadlines["epoch1_activation_seconds"] >= deadlines["arm_hard_seconds"]
            or deadlines["optimization_activation_seconds"]
            >= deadlines["arm_hard_seconds"]
        ):
            _error("FCRASH-H phase deadlines are not strictly nested")
        result = {
            "schema_version": 1,
            "profile_id": profile.profile_id,
            "active_tree_id": active_tree,
            "horizon_tree_positions": len(capacity["prefix_tree_ids"]),
            "required_postfault_tree_positions": len(capacity["prefix_tree_ids"]),
            "required_qualifying_reporters": required,
            "minimum_timeouts_per_reporter": minimum_timeouts,
            "minimum_score_drop": minimum_timeouts * required,
            "reporter_selection_basis": "any_topology_valid_in_prefix_v1",
            "minimum_topology_eligible_reporter_capacity": capacity[
                "minimum_topology_eligible_reporter_capacity"
            ],
            "reporter_coverage_capacity": capacity,
            "deadlines_seconds": deadlines,
            "stable_phase_seconds": _integer(
                timers.get("stable_phase_seconds"), "stable phase", 1
            ),
            "readiness_timeout_seconds": _integer(
                timers.get("readiness_timeout_seconds"), "readiness timeout", 1
            ),
            "manager_convergence_timeout_seconds": _integer(
                timers.get("manager_convergence_timeout_seconds"),
                "manager convergence timeout",
                1,
            ),
            "targets": capacity["targets"],
        }
        if all_candidate_capacity is not None:
            result["all_candidate_reporter_coverage_capacity"] = (
                all_candidate_capacity
            )
        return result
    target_rows: list[dict[str, object]] = []
    first_sets: list[set[int]] = []
    horizon = 0
    for target in targets:
        seen: set[int] = set()
        first: list[dict[str, int]] = []
        for offset in range(replica_count):
            tree_id = (active_tree + offset) % replica_count
            if tree_id in targets:
                continue
            reporter = _cyclic_parent(replica_count, fanout, tree_id, target)
            if reporter is None or reporter in targets or reporter in seen:
                continue
            seen.add(reporter)
            first.append(
                {
                    "tree_position": offset + 1,
                    "tree_id": tree_id,
                    "reporter_id": reporter,
                }
            )
            if len(first) == required:
                break
        if len(first) != required:
            _error("FCRASH-H has insufficient honest reporter coverage")
        horizon = max(horizon, first[-1]["tree_position"])
        reporter_set = {row["reporter_id"] for row in first}
        first_sets.append(reporter_set)
        target_rows.append(
            {
                "target_replica_id": target,
                "authenticated_reporter_ids": [],
                "first_qualifying_reporters": first,
            }
        )
    common_reporters = set.intersection(*first_sets)
    if len(common_reporters) != required:
        _error("FCRASH-H targets lack one common honest reporter set")
    ordered_common = sorted(common_reporters)
    for row in target_rows:
        row["authenticated_reporter_ids"] = ordered_common
    expected_period = (
        2
        if profile.profile_id in _FCRASH_H_V3_PROFILE_IDS or _is_v4_profile(profile)
        else replica_count
    )
    expected_guard = {
        "schedule": "native_cyclic_epoch_zero",
        "tree_switch_period_blocks": expected_period,
        "horizon_tree_positions": horizon,
        "required_qualifying_reporters": required,
        "minimum_timeouts_per_reporter": minimum_timeouts,
        "minimum_score_drop": minimum_timeouts * required,
    }
    if profile.profile_id in _FCRASH_H_V3_PROFILE_IDS or _is_v4_profile(profile):
        expected_guard["required_postfault_tree_positions"] = horizon
    if dict(guard) != expected_guard:
        _error("FCRASH-H frozen evidence guard differs from topology derivation")
    deadline_fields = {
        "evidence_seconds": "nonresponse_evidence_deadline_seconds",
        "epoch1_activation_seconds": "containment_activation_deadline_seconds",
        "optimization_activation_seconds": "optimization_activation_deadline_seconds",
        "arm_hard_seconds": "arm_hard_deadline_seconds",
    }
    deadlines = {
        output: _integer(timers.get(source), source, 1)
        for output, source in deadline_fields.items()
    }
    readiness = _integer(
        timers.get("readiness_timeout_seconds"), "readiness timeout", 1
    )
    convergence = _integer(
        timers.get("manager_convergence_timeout_seconds"),
        "manager convergence timeout",
        1,
    )
    stable = _integer(timers.get("stable_phase_seconds"), "stable phase", 1)
    if (
        deadlines["evidence_seconds"] >= deadlines["epoch1_activation_seconds"]
        or deadlines["epoch1_activation_seconds"] >= deadlines["arm_hard_seconds"]
        or deadlines["optimization_activation_seconds"] >= deadlines["arm_hard_seconds"]
    ):
        _error("FCRASH-H phase deadlines are not strictly nested")
    return {
        "schema_version": 1,
        "profile_id": profile.profile_id,
        "active_tree_id": active_tree,
        "horizon_tree_positions": horizon,
        "required_qualifying_reporters": required,
        "minimum_timeouts_per_reporter": minimum_timeouts,
        "minimum_score_drop": minimum_timeouts * required,
        **(
            {
                "required_postfault_tree_positions": horizon,
                "nominal_commit_horizon": horizon * expected_period,
            }
            if profile.profile_id in _FCRASH_H_V3_PROFILE_IDS or _is_v4_profile(profile)
            else {}
        ),
        "deadlines_seconds": deadlines,
        "stable_phase_seconds": stable,
        "readiness_timeout_seconds": readiness,
        "manager_convergence_timeout_seconds": convergence,
        "targets": target_rows,
    }


def _fault_window_arm_document(
    configuration: Mapping[str, object],
    receipt: Mapping[str, object],
    barrier: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Bind the one-shot manager arm to finalized fault and pre-fault state."""

    profile = configuration.get("profile")
    if not isinstance(profile, FocusedProfile) or not _is_v4_profile(profile):
        _error("fault-window arm requires a v4 focused profile")
    if not has_exact_active_configuration_barrier(profile, barrier):
        _error("fault-window arm lacks the exact pre-fault configuration")
    coverage = derive_reporter_coverage_plan(profile)
    first = _document(barrier[0], "pre-fault active configuration")
    configuration_row = _document(first.get("configuration"), "pre-fault configuration")
    prefault_tree_id = _integer(configuration_row.get("tree_id"), "pre-fault tree")
    epoch_number = _integer(configuration_row.get("epoch_number"), "pre-fault epoch")
    epoch_digest = _digest(
        configuration_row.get("epoch_digest"), "pre-fault epoch digest"
    )
    confirmations = [
        _integer(
            _document(outcome, "fault outcome").get("confirmed_monotonic_ns"),
            "fault confirmation",
        )
        for outcome in _sequence(receipt.get("sigkill_outcomes"), "SIGKILL outcomes")
    ]
    if not confirmations:
        _error("fault-window arm has no finalized confirmations")
    receipt_path = Path(configuration["run_directory"]) / "raw" / "fault-receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    if receipt_bytes != _canonical_json(receipt):
        _error("fault-window arm receipt bytes are not canonical")
    positions = _integer(
        coverage.get("required_postfault_tree_positions"),
        "fault-window required tree positions",
        1,
    )
    required_ids = [
        (prefault_tree_id + offset) % len(profile.replica_ids)
        for offset in range(positions)
    ]
    if len(set(required_ids)) != len(required_ids):
        _error("fault-window required tree prefix is not unique")
    arm = {
        "schema_version": (
            4
            if _is_v9_profile(profile)
            else (
                3
                if _is_v7_profile(profile) or _is_v8_profile(profile)
                else 2 if _is_v6_profile(profile) else 1
            )
        ),
        "kind": (
            "kauri-focused-fault-window-arm-v4"
            if _is_v9_profile(profile)
            else (
                _FAULT_WINDOW_ARM_DOMAIN_V3
                if _is_v7_profile(profile) or _is_v8_profile(profile)
                else (
                    _FAULT_WINDOW_ARM_DOMAIN_V2
                    if _is_v6_profile(profile)
                    else _FAULT_WINDOW_ARM_DOMAIN_V1
                )
            )
        ),
        "run_id": str(configuration["run_id"]),
        "profile_id": profile.profile_id,
        "profile_sha256": profile.profile_sha256,
        "topology_proof_sha256": profile.topology_proof_sha256,
        "request_sha256": _digest(
            configuration.get("parent_request_sha256"), "parent request digest"
        ),
        "epoch_number": epoch_number,
        "epoch_digest": epoch_digest,
        "fault_receipt_sha256": _sha256(receipt_bytes),
        "evidence_start_monotonic_ns": max(confirmations),
        "prefault_tree_id": prefault_tree_id,
        "required_tree_positions": positions,
        "required_tree_ids": required_ids,
    }
    if (
        _is_v6_profile(profile)
        or _is_v7_profile(profile)
        or _is_v8_or_v9_profile(profile)
    ):
        arm.update(
            {
                "clock_domain": "same_host_clock_monotonic_raw",
                "required_observation_schema": 3,
                "timeout_evidence_basis": "exact_timeout_attempt_id_v1",
            }
        )
    if _is_v7_profile(profile) or _is_v8_or_v9_profile(profile):
        arm["snapshot_evidence_basis"] = "exact_post_fault_attempt_start_v1"
    if _is_v9_profile(profile):
        arm["selection_cardinality_policy"] = "all_guarded_up_to_fault_bound_v1"
    return arm


def _enable_v6_timeout_attempt_evidence(
    run_directory: Path, replica_ids: Sequence[int]
) -> None:
    """Append the one native v3 evidence switch to every replica config."""

    suffix = b"experiment-exact-timeout-attempt-evidence-v3 = true\n"
    for replica in replica_ids:
        path = run_directory / "config" / f"replica-{replica}.conf"
        payload = path.read_bytes()
        if not payload.endswith(b"\n") or suffix in payload:
            _error("v6 replica timeout-attempt evidence configuration drifted")
        path.write_bytes(payload + suffix)


def has_exact_active_configuration_barrier(
    profile: FocusedProfile,
    rows: Sequence[Mapping[str, object]],
) -> bool:
    """Check the exact all-member Epoch-0 configuration immediately pre-fault."""

    if type(profile) is not FocusedProfile or len(rows) != len(profile.replica_ids):
        return False
    topology = _document(profile.raw.get("topology"), "profile topology")
    expected = {
        "epoch_number": 0,
        "tree_id": topology.get("active_tree_id"),
        "epoch_digest": topology.get("epoch_zero_digest"),
    }
    seen: set[int] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            return False
        replica = raw.get("replica_id")
        configuration = raw.get("configuration")
        if (
            type(replica) is not int
            or replica not in profile.replica_ids
            or replica in seen
            or not isinstance(configuration, Mapping)
            or dict(configuration) != expected
        ):
            return False
        seen.add(replica)
    return seen == set(profile.replica_ids)


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


def _v13_authorization_readiness_projection(
    profile: FocusedProfile,
    allocations: Mapping[str, object],
    *,
    pair_count: int,
) -> dict[str, dict[str, object]]:
    """Public, canonical pre-authorization commitment to v13 BLS members."""
    expected_pairs = {f"pair-{ordinal:02d}" for ordinal in range(1, pair_count + 1)}
    if set(allocations) != expected_pairs:
        _error("v13 pair readiness allocation cardinality drifted")
    expected_members = _integer(
        _document(profile.raw.get("protocol"), "v13 protocol").get("N"),
        "v13 profile member count", 1,
    )
    manifests: dict[str, dict[str, object]] = {}
    manifest_digests: set[str] = set()
    membership_digests: set[str] = set()
    for pair_id in sorted(expected_pairs):
        allocation = _document(allocations[pair_id], f"{pair_id} readiness allocation")
        manifest_sha = _digest(allocation.get("manifest_sha256"), f"{pair_id} manifest digest")
        membership = _digest(allocation.get("membership_digest"), f"{pair_id} membership digest")
        members = allocation.get("members")
        if not isinstance(members, list) or len(members) != expected_members:
            _error("v13 readiness allocation member count drifted")
        if manifest_sha in manifest_digests or membership in membership_digests:
            _error("v13 readiness allocation has duplicate public commitments")
        manifest_digests.add(manifest_sha)
        membership_digests.add(membership)
        manifests[pair_id] = {
            "manifest_sha256": manifest_sha,
            "membership_digest": membership,
            "member_count": expected_members,
        }
    return manifests


def _validate_v13_authorization_readiness_projection(
    value: object, *, pair_count: int
) -> dict[str, dict[str, object]]:
    projection = _document(value, "v13 authorization readiness projection")
    expected = {f"pair-{ordinal:02d}" for ordinal in range(1, pair_count + 1)}
    if set(projection) != expected:
        _error("v13 authorization readiness projection pair keys drifted")
    result: dict[str, dict[str, object]] = {}
    manifests: set[str] = set()
    memberships: set[str] = set()
    for pair_id in sorted(expected):
        row = _document(projection[pair_id], f"{pair_id} readiness projection")
        if set(row) != {"manifest_sha256", "membership_digest", "member_count"}:
            _error("v13 authorization readiness projection schema drifted")
        manifest = _digest(row.get("manifest_sha256"), f"{pair_id} manifest digest")
        membership = _digest(row.get("membership_digest"), f"{pair_id} membership digest")
        members = _integer(row.get("member_count"), f"{pair_id} member count", 1)
        if manifest in manifests or membership in memberships:
            _error("v13 authorization readiness projection duplicates a public digest")
        manifests.add(manifest)
        memberships.add(membership)
        result[pair_id] = {
            "manifest_sha256": manifest,
            "membership_digest": membership,
            "member_count": members,
        }
    return result


def _authorization_request_keys(
    document: Mapping[str, object], *, strict: bool = True
) -> tuple[str, ...]:
    schema = document.get("schema_version")
    if schema == 1:
        keys = _AUTHORIZATION_KEYS
        if "execution_context_sha256" in document:
            keys = (*keys, "execution_context_sha256")
    elif schema == 2:
        keys = (*_AUTHORIZATION_KEYS, "execution_context_sha256", _V13_AUTHORIZATION_READINESS_KEY)
    else:
        _error("authorization request schema drifted")
    if strict and set(document) != set(keys):
        _error("authorization request schema drifted")
    return keys


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
    readiness_allocator: object | None = None
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
            readiness_allocator=(
                None
                if allocation_root is None or not _is_v13_profile(profile)
                else FocusedV13ReadinessAllocator(
                    allocation_root=allocation_root.parent / "readiness-allocation",
                    pair_count=pair_count,
                    keygen_binary=build_directory / "hotstuff-keygen",
                    profile=profile,
                )
            ),
        )

    def repository(self, _profile: object) -> Mapping[str, object]:
        revision = profiled_fault_runtime.verify_repository_state(self.repository_path)
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
            return dict(_document(allocate(), "pair issuer allocation"))
        configured = focused.issuer_public_key
        if not isinstance(configured, str) or len(configured) != 66:
            _error("issuer_public_key check requires a pair-bound issuer context")
        return {"issuer_public_key": configured}

    def activation_readiness(self, profile: object) -> Mapping[str, object]:
        if not _is_v13_profile(profile):
            _error("activation readiness preallocation is v13-only")
        allocate = getattr(self.readiness_allocator, "allocate", None)
        if not callable(allocate):
            _error("v13 readiness allocator is unavailable")
        return dict(_document(allocate(), "v13 readiness allocation"))


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
    if _is_v13_profile(profile) and checks is None:
        _error("v13 preflight requires readiness allocation before authorization")
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
        check_names = [
            "repository",
            "build",
            "binaries",
            "ports",
            "clock",
            "native_topology",
            "issuer_public_key",
        ]
        if _is_v13_profile(profile):
            check_names.append("activation_readiness")
        for name in check_names:
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
            or native.get("topology_proof_sha256") != profile.topology_proof_sha256
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
                if name not in {"issuer_public_key", "activation_readiness"}
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
        if _is_v13_profile(profile):
            readiness = _document(
                check_results["activation_readiness"],
                "v13 readiness allocation",
            )
            allocations = _document(
                readiness.get("pair_readiness_allocations"),
                "v13 pair readiness allocations",
            )
            expected_pairs = {
                f"pair-{ordinal:02d}" for ordinal in range(1, pair_count + 1)
            }
            if set(allocations) != expected_pairs:
                _error("v13 pair readiness allocation cardinality drifted")
            execution_context["pair_readiness_allocations"] = {
                pair_id: dict(
                    _document(allocations[pair_id], f"{pair_id} readiness allocation")
                )
                for pair_id in sorted(expected_pairs)
            }
            request = {
                **request,
                "schema_version": 2,
                _V13_AUTHORIZATION_READINESS_KEY:
                    _v13_authorization_readiness_projection(
                        profile,
                        execution_context["pair_readiness_allocations"],
                        pair_count=pair_count,
                    ),
            }
        if profile.raw.get("schema_version") == 2:
            execution_context["reporter_coverage"] = derive_reporter_coverage_plan(
                profile
            )
        request = {
            **request,
            "execution_context_sha256": _sha256(_canonical_json(execution_context)),
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
    request_keys = _authorization_request_keys(document, strict=False)
    execution_context = document.get("execution_context")
    if "execution_context_sha256" in request_keys:
        context_digest = _sha256(
            _canonical_json(_document(execution_context, "execution context"))
        )
        if document.get("execution_context_sha256") != context_digest:
            _error("preflight execution context digest drifted")
    request = {key: document.get(key) for key in request_keys}
    if any(request[key] is None for key in request_keys):
        _error("preflight does not contain the complete authorization request")
    if request["schema_version"] == 2:
        projection = _validate_v13_authorization_readiness_projection(
            request[_V13_AUTHORIZATION_READINESS_KEY],
            pair_count=_integer(request["pair_count"], "authorized pair count", 1),
        )
        allocations = _document(
            _document(execution_context, "execution context").get(
                "pair_readiness_allocations"
            ),
            "v13 readiness allocations",
        )
        actual: dict[str, dict[str, object]] = {}
        for pair_id in sorted(projection):
            allocation = _document(allocations.get(pair_id), f"{pair_id} readiness allocation")
            members = allocation.get("members")
            if not isinstance(members, list):
                _error("v13 readiness allocation members are malformed")
            actual[pair_id] = {
                "manifest_sha256": _digest(allocation.get("manifest_sha256"), f"{pair_id} manifest digest"),
                "membership_digest": _digest(allocation.get("membership_digest"), f"{pair_id} membership digest"),
                "member_count": len(members),
            }
        if projection != actual:
            _error("preflight readiness projection drifted")
    payload = _canonical_json(request)
    expected_sha = document.get("request_sha256")
    if expected_sha is not None and expected_sha != _sha256(payload):
        base_request = {key: document.get(key) for key in _AUTHORIZATION_KEYS}
        if request["schema_version"] != 1 or "execution_context_sha256" not in request or expected_sha != _sha256(
            _canonical_json(base_request)
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
    request_keys = set(_authorization_request_keys(request_document))
    if request_document["schema_version"] == 2:
        _validate_v13_authorization_readiness_projection(
            request_document[_V13_AUTHORIZATION_READINESS_KEY],
            pair_count=_integer(request_document.get("pair_count"), "authorized pair count", 1),
        )
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
    if (
        not isinstance(document.get("approval_reference"), str)
        or not document["approval_reference"]
    ):
        _error("authorization approval reference is absent")
    if (
        not isinstance(document.get("approved_utc"), str)
        or not document["approved_utc"]
    ):
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
    if verified.get("execution_context_sha256") != _sha256(_canonical_json(context)):
        _error("authorization does not bind the issuer execution context")
    allocations = _document(context.get("pair_issuers"), "pair issuer allocations")
    pair_count = _integer(verified.get("pair_count"), "authorized pair count", 1)
    expected = {f"pair-{ordinal:02d}" for ordinal in range(1, pair_count + 1)}
    if set(allocations) != expected:
        _error("pair issuer allocation cardinality drifted")
    loaded: dict[str, dict[str, object]] = {}
    public_keys: set[str] = set()
    for pair_id in sorted(expected):
        allocation = _document(allocations[pair_id], f"{pair_id} issuer allocation")
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
            or any(
                character not in "0123456789abcdef" for character in private_text[:-1]
            )
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


def reload_v13_readiness_allocations(
    *,
    preflight: Mapping[str, Any],
    authorization: Mapping[str, Any],
    keygen_record: Mapping[str, object],
    run_command: Callable[..., Any] = subprocess.run,
) -> dict[str, dict[str, object]]:
    """Reload authorized readiness scalars and rederive every public key natively."""

    request = build_focused_authorization_request(preflight)
    verified = verify_focused_authorization_receipt(request, authorization)
    context = _document(preflight.get("execution_context"), "execution context")
    if verified.get("execution_context_sha256") != _sha256(_canonical_json(context)):
        _error("authorization does not bind v13 readiness allocation")
    authorized_keygen = _document(keygen_record, "authorized keygen binary")
    keygen_path_value = authorized_keygen.get("path")
    if not isinstance(keygen_path_value, str) or not Path(keygen_path_value).is_absolute():
        _error("authorized keygen path is not absolute")
    keygen_binary = Path(keygen_path_value)
    if keygen_binary.is_symlink() or not keygen_binary.is_file():
        _error("authorized keygen binary is absent or unsafe")
    resolved_keygen = keygen_binary.resolve()
    if (
        str(resolved_keygen) != keygen_path_value
        or _sha256_regular_file_no_follow(resolved_keygen, label="authorized keygen binary")
        != _digest(authorized_keygen.get("sha256"), "authorized keygen digest")
    ):
        _error("authorized keygen binary identity drifted")
    allocations = _document(
        context.get("pair_readiness_allocations"), "v13 readiness allocations"
    )
    expected_pairs = {
        f"pair-{ordinal:02d}"
        for ordinal in range(
            1, _integer(verified.get("pair_count"), "authorized pair count", 1) + 1
        )
    }
    if set(allocations) != expected_pairs:
        _error("v13 readiness allocation cardinality drifted")
    projection = _validate_v13_authorization_readiness_projection(
        verified.get(_V13_AUTHORIZATION_READINESS_KEY),
        pair_count=len(expected_pairs),
    )
    expected_projection: dict[str, dict[str, object]] = {}
    for pair_id in sorted(expected_pairs):
        allocation = _document(allocations[pair_id], f"{pair_id} readiness allocation")
        members = allocation.get("members")
        if not isinstance(members, list):
            _error("v13 readiness allocation members are malformed")
        expected_projection[pair_id] = {
            "manifest_sha256": _digest(allocation.get("manifest_sha256"), f"{pair_id} manifest digest"),
            "membership_digest": _digest(allocation.get("membership_digest"), f"{pair_id} membership digest"),
            "member_count": len(members),
        }
    if projection != expected_projection:
        _error("authorization readiness projection does not match allocation")
    loaded: dict[str, dict[str, object]] = {}
    allocation_public_keys: set[str] = set()
    allocation_private_scalars: set[bytes] = set()
    for pair_id in sorted(expected_pairs):
        allocation = _document(allocations[pair_id], f"{pair_id} readiness allocation")
        manifest_path_value = allocation.get("manifest_path")
        if not isinstance(manifest_path_value, str) or not Path(manifest_path_value).is_absolute():
            _error("v13 readiness manifest path is not absolute")
        manifest_path = Path(manifest_path_value)
        if manifest_path.is_symlink() or not manifest_path.is_file():
            _error("v13 readiness manifest is absent or unsafe")
        manifest = manifest_path.read_bytes()
        if _sha256(manifest) != _digest(
            allocation.get("manifest_sha256"), "v13 readiness manifest digest"
        ):
            _error("v13 readiness public manifest drifted")
        try:
            manifest_document = _document(json.loads(manifest), "v13 readiness manifest")
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise FocusedCrashPairRuntimeError(
                "v13 readiness public manifest is malformed"
            ) from exc
        if _canonical_json(manifest_document) != manifest:
            _error("v13 readiness public manifest is not canonical JSON")
        members_value = manifest_document.get("members")
        private_value = allocation.get("private_allocations")
        if not isinstance(members_value, list) or not isinstance(private_value, list):
            _error("v13 readiness allocation members are malformed")
        if allocation.get("members") != members_value or len(private_value) != len(members_value):
            _error("v13 readiness public/private cardinality drifted")
        if manifest_document.get("membership_digest") != _v13_membership_digest(
            [_document(member, "v13 readiness member") for member in members_value]
        ) or allocation.get("membership_digest") != manifest_document.get(
            "membership_digest"
        ):
            _error("v13 readiness membership digest drifted")
        private_rows: list[dict[str, object]] = []
        for expected_replica, raw_private in enumerate(private_value):
            private = _document(raw_private, "v13 readiness private allocation")
            member = _document(members_value[expected_replica], "v13 readiness member")
            if (
                private.get("replica_id") != expected_replica
                or member.get("replica_id") != expected_replica
                or private.get("public_key_hex") != member.get("public_key_hex")
            ):
                _error("v13 readiness private allocation identity drifted")
            raw_path = private.get("private_key_path")
            if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                _error("v13 readiness private path is not absolute")
            private_path = Path(raw_path)
            if private_path.parent != manifest_path.parent or private_path.name != f"replica-{expected_replica}.sec":
                _error("v13 readiness private allocation escaped its pair directory")
            scalar = _read_exact_private_scalar(
                private_path, label=f"{pair_id} replica {expected_replica} readiness scalar"
            )
            if _sha256(scalar) != _digest(
                private.get("private_key_sha256"), "v13 readiness private digest"
            ):
                _error("v13 readiness private scalar drifted")
            public = member.get("public_key_hex")
            if (
                not isinstance(public, str)
                or public in allocation_public_keys
                or scalar in allocation_private_scalars
            ):
                _error("v13 readiness allocation has cross-pair key overlap")
            allocation_public_keys.add(public)
            allocation_private_scalars.add(scalar)
            result = run_command(
                (
                    str(resolved_keygen),
                    "--derive-bls-public",
                ),
                input=scalar.decode("ascii"),
                check=False,
                capture_output=True,
                text=True,
            )
            expected_output = f"pub:{member['public_key_hex']}\n"
            if result.returncode != 0 or result.stderr or result.stdout != expected_output:
                _error("v13 readiness private/public derivation drifted")
            private_rows.append(
                {
                    **dict(private),
                    "private_key": scalar[:-1].decode("ascii"),
                }
            )
        loaded[pair_id] = {
            **dict(allocation),
            "private_allocations": private_rows,
            "manifest_bytes": manifest,
        }
    return loaded


def _v13_launch_readiness_allocation(
    profile: FocusedProfile,
    *,
    pair_id: str,
    allocations: Mapping[str, object],
) -> dict[str, object]:
    """Validate the authorized public/private split before constructing argv."""

    _v13_certified_activation_contract(profile)
    if set(allocations) == {pair_id}:
        raw_allocation = allocations[pair_id]
    elif pair_id in allocations:
        raw_allocation = allocations[pair_id]
    else:
        _error("v13 launch readiness allocation is missing its pair")
    allocation = _document(raw_allocation, f"{pair_id} readiness allocation")
    if set(allocation) != {
        "manifest_path",
        "manifest_sha256",
        "membership_digest",
        "members",
        "private_allocations",
        "manifest_bytes",
    }:
        _error("v13 launch readiness allocation schema drifted")
    manifest_bytes = allocation.get("manifest_bytes")
    if not isinstance(manifest_bytes, bytes):
        _error("v13 launch public manifest bytes are absent")
    if _sha256(manifest_bytes) != _digest(
        allocation.get("manifest_sha256"), "v13 launch manifest digest"
    ):
        _error("v13 launch public manifest digest drifted")
    try:
        manifest = _document(
            json.loads(manifest_bytes), "v13 launch public manifest"
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FocusedCrashPairRuntimeError(
            "v13 launch public manifest is malformed"
        ) from exc
    if _canonical_json(manifest) != manifest_bytes:
        _error("v13 launch public manifest is not canonical JSON")
    members_value = manifest.get("members")
    if not isinstance(members_value, list):
        _error("v13 launch public manifest members are malformed")
    members = [
        dict(_document(member, "v13 launch readiness member"))
        for member in members_value
    ]
    if (
        set(manifest)
        != {
            "algorithm",
            "domain",
            "membership_digest",
            "members",
            "profile_id",
            "profile_sha256",
            "protocol_mode",
            "schema_version",
        }
        or len(members) != len(profile.replica_ids)
        or manifest.get("schema_version") != 1
        or manifest.get("domain")
        != "kauri-adaptive-v3-readiness-public-key-manifest-v1"
        or manifest.get("algorithm") != "bls-pop"
        or manifest.get("protocol_mode") != "adaptive_v3"
        or manifest.get("profile_id") != profile.profile_id
        or manifest.get("profile_sha256") != profile.profile_sha256
        or allocation.get("members") != members
        or manifest.get("membership_digest") != _v13_membership_digest(members)
        or allocation.get("membership_digest")
        != manifest.get("membership_digest")
    ):
        _error("v13 launch public manifest identity drifted")
    private_value = allocation.get("private_allocations")
    if not isinstance(private_value, list) or len(private_value) != len(members):
        _error("v13 launch private allocation cardinality drifted")
    private_rows: list[dict[str, object]] = []
    private_values: set[str] = set()
    for expected_replica, raw_private in enumerate(private_value):
        private = _document(raw_private, "v13 launch private allocation")
        if set(private) != {
            "private_key_path",
            "private_key_sha256",
            "public_key_hex",
            "replica_id",
            "private_key",
        }:
            _error("v13 launch private allocation schema drifted")
        member = members[expected_replica]
        scalar = private.get("private_key")
        private_path = private.get("private_key_path")
        if (
            private.get("replica_id") != expected_replica
            or member.get("replica_id") != expected_replica
            or private.get("public_key_hex") != member.get("public_key_hex")
            or not isinstance(private_path, str)
            or not Path(private_path).is_absolute()
            or not isinstance(scalar, str)
            or len(scalar) != 64
            or any(character not in "0123456789abcdef" for character in scalar)
            or scalar in private_values
            or _sha256(f"{scalar}\n".encode("ascii"))
            != _digest(
                private.get("private_key_sha256"),
                "v13 launch private allocation digest",
            )
        ):
            _error("v13 launch private allocation identity drifted")
        private_values.add(scalar)
        private_rows.append(dict(private))
    if any(value.encode("ascii") in manifest_bytes for value in private_values):
        _error("v13 readiness private material contaminated the public manifest")
    return {
        "manifest_bytes": manifest_bytes,
        "manifest_sha256": allocation["manifest_sha256"],
        "membership_digest": allocation["membership_digest"],
        "members": members,
        "private_allocations": private_rows,
        "private_values": private_values,
    }


def _v13_redacted_launch_projection(
    argv: Sequence[str], *, private_values: Collection[str]
) -> tuple[str, ...]:
    """Return a launch projection that cannot contain readiness private material."""

    secrets = {value for value in private_values if isinstance(value, str) and value}
    projected: list[str] = []
    for value in argv:
        if not isinstance(value, str):
            _error("v13 launch argument is not text")
        redacted = value
        for secret in secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "<redacted>")
        projected.append(redacted)
    encoded = _canonical_json(projected)
    if any(secret.encode("utf-8") in encoded for secret in secrets):
        _error("v13 private material survived launch redaction")
    return tuple(projected)


def _v13_assert_private_material_excluded(
    root: Path, *, private_values: Collection[str]
) -> None:
    secrets = tuple(
        value.encode("ascii")
        for value in private_values
        if isinstance(value, str) and value
    )
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        payload = path.read_bytes()
        if any(secret in payload for secret in secrets):
            _error(
                "v13 readiness private material entered a materialized launch artifact"
            )


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
    if not actions or any(
        type(action) is not ReplicaGroupSigkill for action in actions
    ):
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
        if (
            outcome.fault_id != action.fault_id
            or outcome.replica_id != action.replica_id
        ):
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
    snapshot: Mapping[str, object],
    issuer_public_key: str,
    label: str,
    profile: FocusedProfile | Mapping[str, Any] | object,
) -> tuple[bytes, Any]:
    wire = snapshot.get("native_bundle")
    if not isinstance(wire, bytes) or not wire:
        _error(f"{label} native bundle is absent")
    try:
        decoded = _decode_focused_epoch_change_bundle(
            wire,
            issuer_public_key=issuer_public_key,
            profile=profile,
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
        height = _integer(
            snapshot.get("command_block_height"), f"{label} command height"
        )
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
    return all(
        getattr(left, field) == getattr(right, field) for field in fields
    ) and tuple(asdict(tree) for tree in left.trees) == tuple(
        asdict(tree) for tree in right.trees
    )


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
    coverage = (
        derive_reporter_coverage_plan(profile)
        if isinstance(profile, FocusedProfile)
        and profile.raw.get("schema_version") == 2
        else None
    )
    if len(survivors) < quorum or hooks.unexpected_exit_ids():
        _error("arm has an unexplained exit or insufficient survivors")

    baseline = hooks.wait_for_stable_phase("baseline")
    if baseline.get("stable") is not True:
        _error("baseline did not stabilize")
    if coverage is not None:
        raw_barrier = baseline.get("active_configuration_barrier")
        if (
            not isinstance(raw_barrier, Sequence)
            or isinstance(raw_barrier, (str, bytes))
            or not has_exact_active_configuration_barrier(profile, raw_barrier)
        ):
            _error("baseline lacks the exact all-member active configuration")
    baseline_ns = _timestamp(baseline, "baseline")
    fault = hooks.inject_atomic_fault_batch()
    fault_ns = _timestamp(fault, "fault")
    if (
        fault_ns <= baseline_ns
        or tuple(fault.get("confirmed_target_ids", ())) != targets
        or tuple(fault.get("survivor_replica_ids", ())) != survivors
    ):
        _error("fault receipt or survivor projection drifted")
    if getattr(
        profile, "profile_id", None
    ) in _FCRASH_H_V3_PROFILE_IDS or _is_v4_profile(profile):
        latched = fault.get("prefault_active_configuration_barrier")
        if (
            not isinstance(latched, Sequence)
            or isinstance(latched, (str, bytes))
            or not has_exact_active_configuration_barrier(profile, latched)
        ):
            _error("fault boundary lacks the exact latched active configuration")
    nonresponse = hooks.wait_for_nonresponse()
    nonresponse_ns = _timestamp(nonresponse, "nonresponse")
    guarded_cohort = tuple(nonresponse.get("detected_target_ids", ()))
    if _is_v9_profile(profile):
        guarded_valid = (
            all(type(replica) is int for replica in guarded_cohort)
            and guarded_cohort == tuple(sorted(set(guarded_cohort)))
            and set(targets).issubset(guarded_cohort)
            and set(guarded_cohort).issubset(replicas)
            and len(targets) <= len(guarded_cohort) <= len(replicas) - quorum
        )
    else:
        guarded_valid = guarded_cohort == targets
    if nonresponse_ns <= fault_ns or not guarded_valid:
        _error("runtime nonresponse does not match the confirmed fault set")
    if coverage is not None:
        deadline = _document(coverage["deadlines_seconds"], "coverage deadlines")
        snapshot_audit_ns = _integer(
            nonresponse.get("snapshot_audit_monotonic_ns"),
            "nonresponse snapshot audit timestamp",
        )
        if snapshot_audit_ns <= nonresponse_ns:
            _error("nonresponse snapshot audit is not causally after its evidence")
        if not is_before_fcrash_h_deadline(
            fault_ns,
            snapshot_audit_ns,
            _integer(deadline.get("evidence_seconds"), "evidence deadline", 1),
        ):
            _error("runtime nonresponse exceeded the crash-anchored evidence cap")
        observed_counts = nonresponse.get("qualifying_timeout_counts")
        if _is_v9_profile(profile):
            required = _integer(
                coverage.get("required_qualifying_reporters"),
                "required qualifying reporters",
                1,
            )
            minimum = _integer(
                coverage.get("minimum_timeouts_per_reporter"),
                "minimum timeouts per reporter",
                1,
            )
            expected_target_keys = {str(target) for target in guarded_cohort}
            if _is_v12_profile(profile):
                v12_relations = _v12_guard_relations(profile)
                eligible_reporters = {
                    str(target): frozenset(v12_relations[target])
                    for target in guarded_cohort
                }
            else:
                eligible_reporters = {
                    str(target): _v9_eligible_guard_reporters(profile, target)
                    for target in guarded_cohort
                }
            if (
                not isinstance(observed_counts, Mapping)
                or set(observed_counts) != expected_target_keys
                or any(
                    not isinstance(observed_counts[target], Mapping)
                    or len(observed_counts[target]) < required
                    or any(
                        not str(reporter).isdigit()
                        or int(reporter) not in eligible_reporters[target]
                        or type(count) is not int
                        or count != minimum
                        for reporter, count in observed_counts[target].items()
                    )
                    for target in expected_target_keys
                )
            ):
                _error("runtime nonresponse lacks the full guarded-cohort coverage")
        elif _is_v8_profile(profile):
            required = _integer(
                coverage.get("required_qualifying_reporters"),
                "required qualifying reporters",
                1,
            )
            minimum = _integer(
                coverage.get("minimum_timeouts_per_reporter"),
                "minimum timeouts per reporter",
                1,
            )
            expected_targets = {
                str(row["target_replica_id"]): {
                    str(reporter["reporter_id"])
                    for reporter in row["eligible_reporters"]
                }
                for row in coverage["targets"]
            }
            if (
                not isinstance(observed_counts, Mapping)
                or set(observed_counts) != set(expected_targets)
                or any(
                    not isinstance(observed_counts[target], Mapping)
                    or not set(observed_counts[target]).issubset(allowed_reporters)
                    or len(observed_counts[target]) < required
                    or any(
                        type(count) is not int or count != minimum
                        for count in observed_counts[target].values()
                    )
                    for target, allowed_reporters in expected_targets.items()
                )
            ):
                _error("runtime nonresponse lacks the frozen reporter coverage")
        else:
            expected_counts = {
                str(row["target_replica_id"]): {
                    str(reporter): coverage["minimum_timeouts_per_reporter"]
                    for reporter in row["authenticated_reporter_ids"]
                }
                for row in coverage["targets"]
            }
            if observed_counts != expected_counts:
                _error("runtime nonresponse lacks the frozen reporter coverage")
        minimum_drop = _integer(
            coverage.get("minimum_score_drop"), "minimum score drop", 1
        )
        drawdowns = nonresponse.get("guard_drawdowns")
        if (
            not isinstance(drawdowns, Mapping)
            or (
                _is_v9_profile(profile)
                and set(drawdowns) != {str(target) for target in guarded_cohort}
            )
            or any(
                type(drawdowns.get(str(target))) is not int
                or int(drawdowns[str(target)]) > -minimum_drop
                for target in guarded_cohort
            )
        ):
            _error("runtime nonresponse lacks the frozen score drawdown")
        required_progress = coverage.get("required_postfault_tree_positions")
        if required_progress is not None:
            progress = _document(
                nonresponse.get("postfault_progress"), "runtime post-fault progress"
            )
            expected_progress_keys = {
                "required_tree_positions",
                "actual_tree_positions",
                "starting_tree_id",
                "observed_tree_ids",
            }
            required_count = _integer(
                required_progress, "required post-fault commit horizon", 1
            )
            if set(progress) != expected_progress_keys or (
                _integer(
                    progress.get("required_tree_positions"),
                    "progress required count",
                    1,
                )
                != required_count
                or _integer(
                    progress.get("actual_tree_positions"), "progress actual count", 1
                )
                < required_count
                or not isinstance(progress.get("observed_tree_ids"), list)
                or len(progress["observed_tree_ids"])
                != _integer(
                    progress.get("actual_tree_positions"), "progress actual count", 1
                )
            ):
                _error("runtime nonresponse lacks the frozen post-fault progress")

    epoch1_snapshot = hooks.issue_epoch_request(1)
    epoch1_ns = _timestamp(epoch1_snapshot, "epoch 1 request")
    if epoch1_ns <= nonresponse_ns:
        _error("epoch 1 request is causally early")
    if coverage is not None and epoch1_ns != snapshot_audit_ns:
        _error("Epoch 1 request is not bound to its native snapshot audit")
    epoch1_wire, epoch1 = _decoded_bundle(
        epoch1_snapshot, issuer, "epoch 1", profile
    )
    if epoch1.epoch_number != 1:
        _error("epoch 1 bundle has the wrong epoch number")
    if (
        _is_v9_profile(profile)
        and _v9_inherited_cohort(profile, epoch1) != guarded_cohort
    ):
        _error("Epoch 1 does not preserve the full guarded cohort")
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
    if coverage is not None and not is_before_fcrash_h_deadline(
        fault_ns,
        activation1_ns,
        _integer(
            _document(coverage["deadlines_seconds"], "coverage deadlines").get(
                "epoch1_activation_seconds"
            ),
            "Epoch 1 activation deadline",
            1,
        ),
    ):
        _error("Epoch 1 activation exceeded the crash-anchored containment cap")
    commit1 = hooks.wait_for_common_commit(1)
    commit1_ns = _common_commit(
        commit1,
        epoch_number=1,
        epoch_digest=epoch1.epoch_digest,
        survivors=survivors,
        quorum=quorum,
        after_ns=activation1_ns,
    )
    v13_contract = (
        _v13_certified_activation_contract(profile)
        if _is_v13_profile(profile)
        else None
    )
    if v13_contract is not None:
        first_common1_ns = _integer(
            commit1.get("first_common_commit_anchor_monotonic_ns"),
            "first Epoch-1 common commit anchor",
        )
        margin_ns = _integer(
            v13_contract["epoch1_common_commit_anchor_deadline_seconds"],
            "v13 Epoch-1 anchor deadline",
            1,
        ) * 1_000_000_000
        if (
            first_common1_ns <= activation1_ns
            or first_common1_ns > commit1_ns
            or first_common1_ns >= activation1_ns + margin_ns
        ):
            _error("v13 Epoch-1 common commit missed its half-open anchor bound")
    elif isinstance(profile, FocusedProfile) and _is_v10_profile(profile):
        anchor_margin_ns, _residency_ms = _v10_transition_timing(profile)
        first_common1_ns = _integer(
            commit1.get("first_common_commit_anchor_monotonic_ns"),
            "first Epoch-1 common commit anchor",
        )
        if (
            first_common1_ns <= activation1_ns
            or first_common1_ns > commit1_ns
            or first_common1_ns > activation1_ns + anchor_margin_ns
        ):
            _error("v10 Epoch-1 common commit exceeded its activation anchor bound")
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
        current_nonresponses = tuple(ranking.get("detected_target_ids", ()))
        if _is_v9_profile(profile):
            ranking_membership_valid = (
                all(
                    type(replica) is int
                    for replica in (*ranked_ids, *current_nonresponses)
                )
                and ranked_ids == tuple(dict.fromkeys(ranked_ids))
                and current_nonresponses == tuple(sorted(set(current_nonresponses)))
                and set(ranked_ids).isdisjoint(current_nonresponses)
                and set(ranked_ids) | set(current_nonresponses) == set(replicas)
                and set(targets).issubset(current_nonresponses)
                and set(current_nonresponses).issubset(guarded_cohort)
                and len(ranked_ids) >= quorum
            )
        else:
            ranking_membership_valid = set(ranked_ids) == set(survivors) and len(
                ranked_ids
            ) == len(survivors)
        expected_root_ids = (
            _v9_optimization_roots(ranked_ids, guarded_cohort, quorum)
            if _is_v9_profile(profile) and ranking_membership_valid
            else ranked_ids[: len(selected_root_ids)]
        )
        if (
            ranking_ns <= max(commit1_ns, containment_ns)
            or ranking.get("predecessor_epoch_digest") != epoch1.epoch_digest
            or ranking.get("fresh_after_common_commit") is not True
            or not ranking_membership_valid
            or not selected_root_ids
            or selected_root_ids != expected_root_ids
        ):
            _error("adaptive ranking is stale or membership-incomplete")
        epoch2_snapshot = hooks.issue_epoch_request(2)
        epoch2_ns = _timestamp(epoch2_snapshot, "epoch 2 request")
        if epoch2_ns <= ranking_ns:
            _error("epoch 2 request precedes its ranking")
        if v13_contract is not None:
            phase_end_ns = commit1_ns + (
                _integer(
                    v13_contract["containment_stabilization_seconds"],
                    "v13 containment stabilization",
                    1,
                )
                + _integer(
                    v13_contract["containment_measurement_seconds"],
                    "v13 containment measurement",
                    1,
                )
            ) * 1_000_000_000
            residence_end_ns = activation1_ns + _integer(
                v13_contract["minimum_predecessor_residency_ms"],
                "v13 predecessor residency",
                1,
            ) * 1_000_000
            if epoch2_ns <= max(phase_end_ns, residence_end_ns):
                _error("v13 Epoch-2 request is ineligible before the Epoch-1 window")
        epoch2_wire, epoch2 = _decoded_bundle(
            epoch2_snapshot, issuer, "epoch 2", profile
        )
        if (
            epoch2.epoch_number != 2
            or epoch2.previous_epoch_digest != epoch1.epoch_digest
        ):
            _error("epoch 2 is not the exact successor of epoch 1")
        if (
            _is_v9_profile(profile)
            and _v9_inherited_cohort(profile, epoch2) != guarded_cohort
        ):
            _error("Epoch 2 does not preserve the inherited guarded cohort")
        if selected_root_ids != tuple(tree.members[0] for tree in epoch2.trees):
            _error("epoch 2 roots differ from the frozen ranking")
        command2 = hooks.wait_for_epoch_commands(2)
        command2_ns = _transition_barrier(
            command2,
            survivors=survivors,
            decoded=epoch2,
            wire=epoch2_wire,
            label="epoch 2 command",
            after_ns=epoch2_ns,
            activation=False,
        )
        if isinstance(profile, FocusedProfile) and _is_v10_profile(profile):
            first_command2_ns = _integer(
                command2.get("first_source_monotonic_ns"),
                "first Epoch-2 command timestamp",
            )
            if first_command2_ns <= containment_ns:
                _error("v10 first Epoch-2 command overlaps the Epoch-1 phase")
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
        if v13_contract is not None:
            hard_deadline_ns = fault_ns + _integer(
                _document(_profile_document_value(profile).get("timers"), "v13 timers").get(
                    "arm_hard_deadline_seconds"
                ),
                "v13 hard deadline",
                1,
            ) * 1_000_000_000
            if not _v13_e2_activation_is_timely(
                final_common_command_ns=command2_ns,
                activation_ns=activation2_ns,
                hard_deadline_ns=hard_deadline_ns,
                budget_seconds=_integer(
                    v13_contract["optimization_activation_budget_seconds"],
                    "v13 optimization activation budget",
                    1,
                ),
            ):
                _error("v13 Epoch-2 activation missed its command-anchored window")
        elif coverage is not None and not is_before_fcrash_h_deadline(
            activation1_ns,
            activation2_ns,
            _integer(
                _document(coverage["deadlines_seconds"], "coverage deadlines").get(
                    "optimization_activation_seconds"
                ),
                "Epoch 2 activation deadline",
                1,
            ),
        ):
            _error("Epoch 2 activation exceeded the Epoch-1-anchored cap")
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
    if coverage is not None and not is_before_fcrash_h_deadline(
        fault_ns,
        _timestamp(late, "late"),
        _integer(
            _document(coverage["deadlines_seconds"], "coverage deadlines").get(
                "arm_hard_seconds"
            ),
            "arm hard deadline",
            1,
        ),
    ):
        _error("arm exceeded the crash-anchored hard deadline")
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
        expected_run_id: str | None = None,
        expected_source_instances: Mapping[str, str] | None = None,
    ) -> None:
        self._root = Path(run_directory)
        self._poll_interval_s = max(0.0, float(poll_interval_s))
        self._timeout_s = float(timeout_s)
        self._process_records = tuple(process_records)
        self._expected_run_id = expected_run_id
        self._expected_source_instances = dict(expected_source_instances or {})
        self._persist_exit_diagnostics = True
        self._persisted_natural_exit_identities: set[tuple[int, int, int]] = set()
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
            self._root / "transitions" / "e1-to-e2-optimization" / "successor.bundle"
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

    def latch_prefault_active_configuration_barrier(
        self,
    ) -> list[dict[str, object]] | None:
        """Incrementally drain native replica tails for the fault-boundary latch."""

        states = getattr(self, "_prefault_tail_states", None)
        if states is None:
            states = {}
            self._prefault_tail_states = states
        for replica in self._profile.replica_ids:
            path = self._root / "raw" / f"replica-{replica}.jsonl"
            if path.is_symlink() or not path.is_file():
                _error("live replica tail is absent")
            state = states.setdefault(
                replica,
                {
                    "offset": 0,
                    "partial": b"",
                    "previous_sequence": None,
                    "previous_timestamp": None,
                    "run_id": None,
                    "source_instance": None,
                    "latest_configuration": None,
                },
            )
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                offset = int(state["offset"])
                if size < offset:
                    _error("live replica tail was truncated")
                initial = offset == 0
                start = max(0, size - 1_048_576) if initial else offset
                stream.seek(start)
                appended = stream.read(size - start)
            if len(appended) != size - start:
                _error("live replica tail changed below the captured cutoff")
            state["offset"] = size
            payload = bytes(state["partial"]) + appended
            if initial and start > 0:
                if b"\n" not in payload:
                    _error("live replica tail has no complete record")
                payload = payload.split(b"\n", 1)[1]
            final_newline = payload.rfind(b"\n")
            if final_newline < 0:
                if len(payload) > 1_048_576:
                    _error("live replica tail record exceeds the bounded cursor")
                state["partial"] = payload
                complete = b""
            else:
                complete = payload[: final_newline + 1]
                state["partial"] = payload[final_newline + 1 :]
            for line in complete.splitlines():
                try:
                    event = _document(json.loads(line), "raw replica tail event")
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise FocusedCrashPairRuntimeError(
                        "raw replica tail is malformed"
                    ) from exc
                if (
                    set(event) != _RUNTIME_EVENT_KEYS
                    or event.get("event_schema_version") != 1
                    or event.get("source_kind") != "replica"
                    or event.get("source_id") != f"replica-{replica}"
                    or not isinstance(event.get("run_id"), str)
                    or not event.get("run_id")
                    or not isinstance(event.get("source_instance"), str)
                    or not event.get("source_instance")
                    or not isinstance(event.get("event_type"), str)
                    or not isinstance(event.get("payload"), Mapping)
                ):
                    _error("live replica tail envelope or identity drifted")
                sequence = _integer(
                    event.get("source_sequence"), "tail source sequence", 1
                )
                timestamp = _integer(
                    event.get("source_monotonic_ns"), "tail source timestamp"
                )
                expected_run_id = state["run_id"]
                expected_instance = state["source_instance"]
                if expected_run_id is None:
                    state["run_id"] = str(event["run_id"])
                    state["source_instance"] = str(event["source_instance"])
                elif (
                    event["run_id"] != expected_run_id
                    or event["source_instance"] != expected_instance
                ):
                    _error("live replica tail spans multiple source identities")
                previous_sequence = state["previous_sequence"]
                previous_timestamp = state["previous_timestamp"]
                if previous_sequence is not None and (
                    sequence != int(previous_sequence) + 1
                    or timestamp < int(previous_timestamp)
                ):
                    _error("live replica tail sequence or timestamp drifted")
                state["previous_sequence"] = sequence
                state["previous_timestamp"] = timestamp
                launched_run_id = getattr(self, "_expected_run_id", None)
                launched_instances = getattr(self, "_expected_source_instances", {})
                if (
                    launched_run_id is not None and event["run_id"] != launched_run_id
                ) or (
                    launched_instances
                    and event["source_instance"]
                    != launched_instances.get(f"replica-{replica}")
                ):
                    _error("live replica tail differs from the launched identity")
                if event["event_type"] != "adaptive.configuration_active":
                    continue
                selected = state["latest_configuration"]
                if selected is None or (
                    int(event["source_sequence"]),
                    int(event["source_monotonic_ns"]),
                ) > (
                    int(selected["source_sequence"]),
                    int(selected["source_monotonic_ns"]),
                ):
                    state["latest_configuration"] = event
        rows = [
            {
                "replica_id": replica,
                "configuration": {
                    key: _document(
                        states[replica]["latest_configuration"]["payload"],
                        "tail configuration",
                    ).get(key)
                    for key in ("epoch_number", "tree_id", "epoch_digest")
                },
            }
            for replica in self._profile.replica_ids
            if states[replica]["latest_configuration"] is not None
        ]
        if not has_exact_active_configuration_barrier(self._profile, rows):
            return None
        return rows

    def postfault_authoritative_configuration_prefix_complete(
        self,
        *,
        evidence_start_monotonic_ns: int,
        prefault_tree_id: int,
        required_tree_ids: Sequence[object],
    ) -> bool:
        """Incrementally verify the bounded authoritative post-fault tree prefix.

        The pre-fault latch supplies position one.  This cursor retains only the
        authoritative stream position and parser continuity state; it never
        reconstructs the aggregate evidence graph.
        """

        profile = self._profile
        if not _is_v4_profile(profile):
            _error("post-fault configuration prefix requires a v4 profile")
        expected = tuple(
            _integer(tree_id, "post-fault required tree ID")
            for tree_id in required_tree_ids
        )
        if (
            not expected
            or len(set(expected)) != len(expected)
            or expected[0] != prefault_tree_id
            or expected
            != tuple(
                (prefault_tree_id + offset) % len(profile.replica_ids)
                for offset in range(len(expected))
            )
        ):
            _error("post-fault configuration prefix is not exact cyclic coverage")
        evidence_start_monotonic_ns = _integer(
            evidence_start_monotonic_ns, "fault-window evidence start", 1
        )
        authoritative = _integer(
            profile.raw["measurement"].get("authoritative_replica_id"),
            "authoritative replica ID",
        )
        state = getattr(self, "_postfault_authoritative_tail_state", None)
        if state is None:
            state = {
                "offset": 0,
                "partial": b"",
                "previous_sequence": None,
                "previous_timestamp": None,
                "position": 1,
            }
            self._postfault_authoritative_tail_state = state
        path = self._root / "raw" / f"replica-{authoritative}.jsonl"
        if path.is_symlink() or not path.is_file():
            _error("live authoritative replica tail is absent")
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            offset = _integer(state["offset"], "authoritative tail offset")
            if size < offset:
                _error("live authoritative replica tail was truncated")
            initial = offset == 0
            start = max(0, size - 1_048_576) if initial else offset
            stream.seek(start)
            appended = stream.read(size - start)
        if len(appended) != size - start:
            _error("live authoritative replica tail changed below the captured cutoff")
        state["offset"] = size
        payload = bytes(state["partial"]) + appended
        if initial and start > 0:
            if b"\n" not in payload:
                _error("live authoritative replica tail has no complete record")
            payload = payload.split(b"\n", 1)[1]
        final_newline = payload.rfind(b"\n")
        if final_newline < 0:
            if len(payload) > 1_048_576:
                _error(
                    "live authoritative replica tail record exceeds the bounded cursor"
                )
            state["partial"] = payload
            complete = b""
        else:
            complete = payload[: final_newline + 1]
            state["partial"] = payload[final_newline + 1 :]
        expected_run_id = getattr(self, "_expected_run_id", None)
        expected_instances = getattr(self, "_expected_source_instances", {})
        for line in complete.splitlines():
            try:
                event = _document(json.loads(line), "authoritative tail event")
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise FocusedCrashPairRuntimeError(
                    "authoritative post-fault configuration is malformed"
                ) from exc
            source_id = f"replica-{authoritative}"
            if (
                set(event) != _RUNTIME_EVENT_KEYS
                or event.get("event_schema_version") != 1
                or event.get("source_kind") != "replica"
                or event.get("source_id") != source_id
                or not isinstance(event.get("run_id"), str)
                or not isinstance(event.get("source_instance"), str)
                or not isinstance(event.get("event_type"), str)
                or not isinstance(event.get("payload"), Mapping)
                or (expected_run_id is not None and event["run_id"] != expected_run_id)
                or (
                    expected_instances
                    and event["source_instance"] != expected_instances.get(source_id)
                )
            ):
                _error("authoritative post-fault tail identity or schema drifted")
            sequence = _integer(
                event.get("source_sequence"), "authoritative tail sequence", 1
            )
            timestamp = _integer(
                event.get("source_monotonic_ns"), "authoritative tail timestamp"
            )
            previous_sequence = state["previous_sequence"]
            previous_timestamp = state["previous_timestamp"]
            if previous_sequence is not None and (
                sequence != int(previous_sequence) + 1
                or timestamp < int(previous_timestamp)
            ):
                _error("authoritative post-fault tail sequence or timestamp drifted")
            state["previous_sequence"] = sequence
            state["previous_timestamp"] = timestamp
            if event["event_type"] != "adaptive.configuration_active":
                continue
            if timestamp <= evidence_start_monotonic_ns:
                continue
            configuration = _document(event["payload"], "post-fault configuration")
            if not {"epoch_number", "tree_id", "epoch_digest"}.issubset(configuration):
                _error("authoritative post-fault configuration is malformed")
            position = _integer(state["position"], "post-fault tree position", 1)
            if (
                _integer(configuration.get("epoch_number"), "post-fault epoch") != 0
                or _integer(configuration.get("tree_id"), "post-fault tree")
                != expected[position % len(expected)]
                or _digest(configuration.get("epoch_digest"), "post-fault epoch digest")
                != profile.raw["topology"]["epoch_zero_digest"]
            ):
                _error("authoritative post-fault configuration prefix drifted")
            state["position"] = position + 1
        return _integer(state["position"], "post-fault tree position", 1) >= len(
            expected
        )

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
        expected_run_id = getattr(self, "_expected_run_id", None)
        expected_instances = getattr(self, "_expected_source_instances", {})
        if expected_run_id is not None and events[0]["run_id"] != expected_run_id:
            _error("raw event streams differ from the launched run")
        if expected_instances:
            for event in events:
                source_id = str(event["source_id"])
                if (
                    source_id not in expected_instances
                    or event["source_instance"] != expected_instances[source_id]
                ):
                    _error("raw event streams differ from launched source identities")
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
        terminal_keys = {
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
        requires_controller_failure = _is_v4_profile(self._profile)
        for event in events:
            if event["event_type"] != "adaptive_v2_session_terminal":
                continue
            payload = _document(event["payload"], "manager terminal")
            if (
                requires_controller_failure
                and not _validate_v4_manager_terminal_payload(payload)
            ):
                _error("manager terminal schema drifted")
            if not requires_controller_failure and set(payload) not in (
                terminal_keys,
                terminal_keys | {"controller_failure"},
            ):
                _error("manager terminal schema drifted")
            if not _validate_controller_failure_terminal(
                payload,
                require_for_unhealthy=requires_controller_failure,
            ):
                _error("manager terminal controller failure drifted")
        return events

    def _bundle(self, epoch: int) -> tuple[bytes, Any]:
        path = self._root / "raw" / f"epoch{epoch}.bundle"
        if not path.exists():
            artifact = "e0-to-e1-containment" if epoch == 1 else "e1-to-e2-optimization"
            path = self._root / "transitions" / artifact / "successor.bundle"
        if path.is_symlink() or not path.is_file():
            _error(f"raw Epoch {epoch} bundle is absent")
        wire = path.read_bytes()
        try:
            decoded = _decode_focused_epoch_change_bundle(
                wire,
                issuer_public_key=str(self._profile.issuer_public_key),
                profile=self._profile,
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
                outcome.get("signal_number") != 9 or outcome.get("returncode") != -9
                for outcome in outcomes
            )
        ):
            _error("raw atomic fault outcome drifted")
        confirmations = [
            _integer(outcome.get("confirmed_monotonic_ns"), "fault confirmation")
            for outcome in outcomes
        ]
        requested = [
            _integer(outcome.get("requested_monotonic_ns"), "fault request")
            for outcome in outcomes
        ]
        survivors = tuple(
            replica for replica in self._profile.replica_ids if replica not in targets
        )
        return {
            "schema_version": 1,
            "atomic": True,
            "confirmed_target_ids": list(targets),
            "survivor_replica_ids": list(survivors),
            "source_monotonic_ns": max(confirmations),
            "pre_signal_monotonic_ns": min(requested),
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
        if (
            predecessor not in {0, 1}
            or audit.get("predecessor_epoch_digest") != expected_digest
        ):
            _error("raw ranking audit predecessor drifted")
        causal_start_ns: int | None = None
        if (
            _is_v7_profile(self._profile) or _is_v8_or_v9_profile(self._profile)
        ) and predecessor == 0:
            armed = [
                event
                for event in manager
                if event["event_type"] == "fault_window_armed"
            ]
            if len(armed) != 1:
                _error("raw v7 ranking lacks one armed causal boundary")
            causal_start_ns = _integer(
                _document(armed[0]["payload"], "raw fault-window arm").get(
                    "evidence_start_monotonic_ns"
                ),
                "raw v7 ranking causal boundary",
                1,
            )
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
                allowed_schema_versions=(
                    frozenset({3})
                    if (
                        _is_v6_profile(self._profile)
                        or _is_v7_profile(self._profile)
                        or _is_v8_or_v9_profile(self._profile)
                    )
                    else frozenset({1})
                ),
                minimum_attempt_start_monotonic_ns=causal_start_ns,
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
        if _is_v9_profile(self._profile):
            ranking_valid = (
                set(self._profile.target_replica_ids).issubset(targets)
                and len(targets)
                <= len(self._profile.replica_ids) - self._profile.quorum
                and len(ranked) >= self._profile.quorum
            )
        else:
            ranking_valid = targets == self._profile.target_replica_ids
        if not ranking_valid:
            _error("raw ranking does not identify the exact nonresponses")
        accepted = [
            event
            for event in manager
            if event["event_type"] == "evidence.observation_accepted"
            and _document(
                _document(
                    _document(event["payload"], "accepted evidence").get("observation"),
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
        selected_root_ids: Sequence[int] = ranked[: self._profile.quorum]
        if _is_v9_profile(self._profile) and predecessor == 1:
            inherited_wait_exempt = _v9_inherited_cohort(self._profile, epoch1)
            selected_root_ids = _v9_optimization_roots(
                ranked,
                inherited_wait_exempt,
                self._profile.quorum,
            )
            audited_roots = tuple(
                _integer(replica, "v9 native audit root")
                for replica in _sequence(
                    audit.get("eligible_ranking"), "v9 native audit roots"
                )
            )
            if audited_roots != selected_root_ids:
                _error("v9 native audit roots differ from inherited constraints")
        return {
            "detected_target_ids": list(targets),
            "ranked_ids": ranked,
            "selected_root_ids": list(selected_root_ids),
            "predecessor_epoch_digest": epoch1.epoch_digest,
            "fresh_after_common_commit": predecessor == 1,
            "source_monotonic_ns": max(
                _integer(event["source_monotonic_ns"], "accepted evidence time")
                for event in accepted
            ),
            "audit_source_monotonic_ns": _integer(
                audits[0].get("source_monotonic_ns"), "ranking audit timestamp"
            ),
            "baseline_evidence_cutoff": _integer(
                audit.get("baseline_cutoff"), "ranking baseline cutoff"
            ),
            "current_evidence_cutoff": _integer(
                audit.get("current_cutoff"), "ranking current cutoff", 1
            ),
            "replayed_snapshot_id": _digest(
                replay.get("snapshot_id"), "ranking replay snapshot ID"
            ),
        }

    def _is_v9_guard_relation(
        self,
        *,
        target: int,
        reporter: int,
        tree_id: int,
        message_type: str,
        prefix: Collection[int],
    ) -> bool:
        """Validate one native topology relation for an inferred v9 cohort member."""

        count = len(self._profile.replica_ids)
        fanout = _integer(
            _document(self._profile.raw.get("protocol"), "profile protocol").get(
                "fanout"
            ),
            "tree fanout",
            1,
        )
        position = (target - tree_id) % count
        leaf_start = (count - 1 + fanout - 1) // fanout
        return (
            tree_id in prefix
            and position != 0
            and _cyclic_parent(count, fanout, tree_id, target) == reporter
            and message_type
            == ("aggregate_relay" if position < leaf_start else "direct_vote")
        )

    def _qualifying_timeout_counts(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        fault_ns: int,
        baseline_cutoff: int,
        current_cutoff: int,
        candidate_targets: Sequence[int] | None = None,
    ) -> tuple[dict[str, dict[str, int]], dict[str, int], int] | None:
        if (
            _is_v6_profile(self._profile)
            or _is_v7_profile(self._profile)
            or _is_v8_or_v9_profile(self._profile)
        ):
            return self._qualifying_v6_timeout_counts(
                events,
                fault_ns=fault_ns,
                baseline_cutoff=baseline_cutoff,
                current_cutoff=current_cutoff,
                candidate_targets=candidate_targets,
            )
        coverage = derive_reporter_coverage_plan(self._profile)
        topology = _document(self._profile.raw.get("topology"), "profile topology")
        expected_digest = topology.get("epoch_zero_digest")
        expected = {
            int(row["target_replica_id"]): {
                int(reporter) for reporter in row["authenticated_reporter_ids"]
            }
            for row in coverage["targets"]
        }
        expected_trees = {
            (
                int(row["target_replica_id"]),
                int(first["reporter_id"]),
            ): int(first["tree_id"])
            for row in coverage["targets"]
            for first in row["first_qualifying_reporters"]
        }
        accepted: list[tuple[int, Mapping[str, Any], int, int]] = []
        latest: dict[str, tuple[int, Mapping[str, Any], int, int]] = {}
        for event in events:
            if (
                event["source_kind"] != "adaptation_manager"
                or event["source_id"] != "adaptive-manager"
                or event["event_type"] != "evidence.observation_accepted"
            ):
                continue
            payload = _document(event["payload"], "accepted evidence")
            sequence = _integer(
                payload.get("ingestion_sequence"), "evidence ingestion sequence", 1
            )
            if sequence <= baseline_cutoff or sequence > current_cutoff:
                continue
            observation = _document(payload.get("observation"), "accepted observation")
            configuration = _document(
                observation.get("configuration"), "observation configuration"
            )
            if (
                configuration.get("epoch_number") != 0
                or configuration.get("epoch_digest") != expected_digest
            ):
                continue
            observation_id = observation.get("observation_id")
            reporter = observation.get("reporter_id")
            target = observation.get("observed_replica_id")
            outcome = observation.get("outcome")
            accepted_ns = _integer(
                event.get("source_monotonic_ns"), "evidence acceptance timestamp"
            )
            reporter_ns = _integer(
                observation.get("reporter_monotonic_ns"),
                "evidence reporter timestamp",
            )
            if (
                not isinstance(observation_id, str)
                or type(reporter) is not int
                or type(target) is not int
                or outcome not in {"on_time", "timeout", "late"}
            ):
                _error("accepted evidence identity or outcome drifted")
            previous = latest.get(observation_id)
            if previous is not None and sequence <= previous[0]:
                _error("accepted evidence attempt sequence regressed")
            latest[observation_id] = (
                sequence,
                observation,
                accepted_ns,
                reporter_ns,
            )
            accepted.append((sequence, observation, accepted_ns, reporter_ns))
        accepted.sort(key=lambda row: row[0])
        drawdowns = {target: 0 for target in expected}
        outstanding: dict[str, tuple[int, int]] = {}
        for _sequence, observation, _accepted_ns, _reporter_ns in accepted:
            observation_id = str(observation["observation_id"])
            reporter = int(observation["reporter_id"])
            target = int(observation["observed_replica_id"])
            outcome = str(observation["outcome"])
            if outcome == "timeout":
                if observation_id in outstanding:
                    _error("accepted timeout attempt is duplicated")
                outstanding[observation_id] = (reporter, target)
                if target in drawdowns:
                    drawdowns[target] -= 1
            elif outcome == "on_time":
                if target in drawdowns and drawdowns[target] < 0:
                    drawdowns[target] += 1
            elif outcome == "late":
                previous = outstanding.pop(observation_id, None)
                if previous is not None:
                    if previous != (reporter, target):
                        _error("accepted late evidence changed its attempt identity")
                    if target in drawdowns and drawdowns[target] < 0:
                        drawdowns[target] += 1
        counts = {
            target: {reporter: 0 for reporter in reporters}
            for target, reporters in expected.items()
        }
        timestamps: list[int] = []
        for _sequence, observation, accepted_ns, reporter_ns in latest.values():
            target = observation.get("observed_replica_id")
            reporter = observation.get("reporter_id")
            configuration = _document(
                observation.get("configuration"), "observation configuration"
            )
            if (
                observation.get("outcome") == "timeout"
                and target in counts
                and reporter in counts[target]
                and configuration.get("tree_id")
                == expected_trees.get((int(target), int(reporter)))
                and accepted_ns >= fault_ns
                and reporter_ns >= fault_ns
            ):
                counts[int(target)][int(reporter)] += 1
                timestamps.extend((accepted_ns, reporter_ns))
        minimum = _integer(
            coverage.get("minimum_timeouts_per_reporter"),
            "minimum timeouts per reporter",
            1,
        )
        minimum_drop = _integer(
            coverage.get("minimum_score_drop"), "minimum score drop", 1
        )
        if any(
            count < minimum
            for reporters in counts.values()
            for count in reporters.values()
        ) or any(drawdown > -minimum_drop for drawdown in drawdowns.values()):
            return None
        clipped = {
            str(target): {str(reporter): minimum for reporter in sorted(reporters)}
            for target, reporters in counts.items()
        }
        if not timestamps:
            return None
        return (
            clipped,
            {str(target): drawdown for target, drawdown in sorted(drawdowns.items())},
            max(timestamps),
        )

    def _qualifying_v6_timeout_counts(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        fault_ns: int,
        baseline_cutoff: int,
        current_cutoff: int,
        candidate_targets: Sequence[int] | None = None,
    ) -> tuple[dict[str, dict[str, int]], dict[str, int], int] | None:
        """Replay only post-arm native v3 timeout attempts for the live guard."""

        coverage = derive_reporter_coverage_plan(self._profile)
        topology = _document(self._profile.raw.get("topology"), "profile topology")
        expected_digest = _digest(
            topology.get("epoch_zero_digest"), "profile epoch-zero digest"
        )
        arm = _document(
            self._profile.raw.get("fault_window_arm"), "v6 fault-window metadata"
        )
        prefix = {
            _integer(tree_id, "v6 fault-window tree")
            for tree_id in _sequence(arm.get("ordered_tree_prefix"), "v6 tree prefix")
        }
        v8_relations: dict[int, dict[int, set[tuple[int, str]]]] = {}
        v12_relations: dict[int, dict[int, frozenset[tuple[int, str]]]] | None = None
        if _is_v9_profile(self._profile):
            if (
                candidate_targets is None
                or any(type(target) is not int for target in candidate_targets)
                or len(set(candidate_targets)) != len(candidate_targets)
                or not set(candidate_targets).issubset(self._profile.replica_ids)
            ):
                _error("v9 guarded-cohort timeout targets are malformed")
            expected = {int(target): set() for target in candidate_targets}
            expected_trees: dict[tuple[int, int], int] = {}
            if _is_v12_profile(self._profile):
                v12_relations = _v12_guard_relations(self._profile)
        elif _is_v8_profile(self._profile):
            for row in coverage["targets"]:
                target = int(row["target_replica_id"])
                v8_relations[target] = {
                    int(reporter_row["reporter_id"]): {
                        (
                            int(relation["tree_id"]),
                            str(relation["expected_message_type"]),
                        )
                        for relation in reporter_row["tree_relations"]
                    }
                    for reporter_row in row["eligible_reporters"]
                }
            expected = {
                target: set(reporters) for target, reporters in v8_relations.items()
            }
            expected_trees: dict[tuple[int, int], int] = {}
        else:
            expected = {
                int(row["target_replica_id"]): {
                    int(reporter) for reporter in row["authenticated_reporter_ids"]
                }
                for row in coverage["targets"]
            }
            expected_trees = {
                (int(row["target_replica_id"]), int(first["reporter_id"])): int(
                    first["tree_id"]
                )
                for row in coverage["targets"]
                for first in row["first_qualifying_reporters"]
            }
        outstanding: dict[str, tuple[int, int]] = {}
        latest: dict[str, tuple[Mapping[str, Any], int, int]] = {}
        eligible_drawdowns = {target: 0 for target in expected}
        raw_drawdowns = {target: 0 for target in expected}
        raw_outstanding: dict[str, tuple[int, int]] = {}
        for event in events:
            if (
                event.get("source_kind") != "adaptation_manager"
                or event.get("source_id") != "adaptive-manager"
                or event.get("event_type") != "evidence.observation_accepted"
            ):
                continue
            payload = _document(event.get("payload"), "accepted evidence")
            sequence = _integer(
                payload.get("ingestion_sequence"), "evidence ingestion sequence", 1
            )
            if sequence <= baseline_cutoff or sequence > current_cutoff:
                continue
            observation = _document(payload.get("observation"), "accepted observation")
            configuration = _document(
                observation.get("configuration"), "observation configuration"
            )
            if (
                configuration.get("epoch_number") != 0
                or configuration.get("epoch_digest") != expected_digest
            ):
                continue
            reporter = _integer(observation.get("reporter_id"), "v6 reporter", 0)
            target = _integer(observation.get("observed_replica_id"), "v6 target", 0)
            tree_id = _integer(configuration.get("tree_id"), "v6 tree", 0)
            schema = _integer(
                observation.get("schema_version"), "v6 observation schema"
            )
            message_type = observation.get("expected_message_type")
            outcome = observation.get("outcome")
            attempt_start = _integer(
                observation.get("attempt_start_monotonic_ns"), "v6 attempt start", 1
            )
            deadline_us = _integer(
                observation.get("deadline_duration_us"), "v6 deadline", 1
            )
            reporter_ns = _integer(
                observation.get("reporter_monotonic_ns"), "v6 reporter timestamp", 1
            )
            response_us = _integer(
                observation.get("response_duration_us"), "v6 response duration"
            )
            local_commit_ns = _integer(
                observation.get("reporter_local_commit_monotonic_ns"),
                "v6 reporter local commit",
                0,
            )
            observation_id = observation.get("observation_id")
            signers = _sequence(observation.get("signer_set"), "v6 signer set")
            if (
                schema != 3
                or message_type not in {"direct_vote", "aggregate_relay"}
                or outcome not in {"on_time", "timeout", "late"}
                or not isinstance(observation_id, str)
                or deadline_us > ((1 << 64) - 1) // 1_000
                or attempt_start > (1 << 64) - 1 - deadline_us * 1_000
                or attempt_start > reporter_ns
                or observation_id
                != _v6_timeout_observation_id(
                    reporter_id=reporter,
                    observed_replica_id=target,
                    epoch_number=0,
                    tree_id=tree_id,
                    epoch_digest=expected_digest,
                    block_hash=str(observation.get("block_hash")),
                    expected_message_type=str(message_type),
                    attempt_start_monotonic_ns=attempt_start,
                    deadline_duration_us=deadline_us,
                )
            ):
                _error("v6 accepted observation identity or schema drifted")
            absolute_deadline = attempt_start + deadline_us * 1_000
            if (
                (
                    outcome == "timeout"
                    and (response_us != 0 or signers or absolute_deadline > reporter_ns)
                )
                or (
                    outcome != "timeout"
                    and (
                        response_us != (reporter_ns - attempt_start) // 1_000
                        or not signers
                        or (outcome == "late" and reporter_ns < absolute_deadline)
                    )
                )
                or (
                    local_commit_ns != 0
                    and (
                        outcome != "timeout"
                        or not (attempt_start <= local_commit_ns < absolute_deadline)
                    )
                )
            ):
                _error("v6 accepted observation timing or signer drifted")
            if (
                not (
                    _is_v7_profile(self._profile) or _is_v8_or_v9_profile(self._profile)
                )
                or attempt_start >= fault_ns
            ):
                if outcome == "timeout":
                    if observation_id in raw_outstanding:
                        _error("v6 raw timeout attempt is duplicated")
                    raw_outstanding[observation_id] = (reporter, target)
                    if target in raw_drawdowns:
                        raw_drawdowns[target] -= 1
                elif outcome == "on_time":
                    if target in raw_drawdowns and raw_drawdowns[target] < 0:
                        raw_drawdowns[target] += 1
                else:
                    raw_previous = raw_outstanding.pop(observation_id, None)
                    if raw_previous is not None:
                        if raw_previous != (reporter, target):
                            _error(
                                "v6 raw late evidence changed its exact attempt identity"
                            )
                        if target in raw_drawdowns and raw_drawdowns[target] < 0:
                            raw_drawdowns[target] += 1
            if (
                attempt_start < fault_ns
                or tree_id not in prefix
                or target not in expected
                or (
                    not _is_v9_profile(self._profile)
                    and reporter not in expected[target]
                )
                or (
                    _is_v8_profile(self._profile)
                    and (tree_id, str(message_type))
                    not in v8_relations[target][reporter]
                )
                or (
                    not _is_v8_or_v9_profile(self._profile)
                    and expected_trees.get((target, reporter)) != tree_id
                )
                or (
                    _is_v9_profile(self._profile)
                    and not self._is_v9_guard_relation(
                        target=target,
                        reporter=reporter,
                        tree_id=tree_id,
                        message_type=str(message_type),
                        prefix=prefix,
                    )
                )
                or (
                    v12_relations is not None
                    and (tree_id, str(message_type))
                    not in v12_relations.get(target, {}).get(reporter, frozenset())
                )
            ):
                continue
            if outcome == "timeout":
                if observation_id in outstanding:
                    _error("v6 accepted timeout attempt is duplicated")
                outstanding[observation_id] = (reporter, target)
                eligible_drawdowns[target] -= 1
            elif outcome == "late":
                previous = outstanding.pop(observation_id, None)
                if previous is not None:
                    if previous != (reporter, target):
                        _error("v6 late evidence changed its exact attempt identity")
                    eligible_drawdowns[target] += 1
            latest[observation_id] = (
                observation,
                int(event["source_monotonic_ns"]),
                reporter_ns,
            )
        counts = (
            {target: {} for target in expected}
            if _is_v9_profile(self._profile)
            else {
                target: {reporter: 0 for reporter in reporters}
                for target, reporters in expected.items()
            }
        )
        timestamps: list[int] = []
        for observation, accepted_ns, reporter_ns in latest.values():
            if observation.get("outcome") != "timeout":
                continue
            target = int(observation["observed_replica_id"])
            reporter = int(observation["reporter_id"])
            counts[target][reporter] = counts[target].get(reporter, 0) + 1
            timestamps.extend((accepted_ns, reporter_ns))
        minimum = _integer(
            coverage.get("minimum_timeouts_per_reporter"),
            "minimum timeouts per reporter",
            1,
        )
        minimum_drop = _integer(
            coverage.get("minimum_score_drop"), "minimum score drop", 1
        )
        if _is_v8_or_v9_profile(self._profile):
            complete = all(
                sum(count >= minimum for count in reporters.values())
                >= _integer(
                    coverage.get("required_qualifying_reporters"),
                    "required qualifying reporters",
                    1,
                )
                for reporters in counts.values()
            )
        else:
            complete = all(
                count >= minimum
                for reporters in counts.values()
                for count in reporters.values()
            )
        if (
            not timestamps
            or not complete
            or any(drawdown > -minimum_drop for drawdown in raw_drawdowns.values())
            or any(drawdown > -minimum_drop for drawdown in eligible_drawdowns.values())
        ):
            return None
        return (
            {
                str(target): {
                    str(reporter): minimum
                    for reporter, count in sorted(reporters.items())
                    if count >= minimum
                }
                for target, reporters in counts.items()
            },
            {
                str(target): drawdown
                for target, drawdown in sorted(raw_drawdowns.items())
            },
            max(timestamps),
        )

    def _postfault_authoritative_progress(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        fault_ns: int,
        prefault_ns: int,
        audit_ns: int,
    ) -> dict[str, int] | None:
        """Derive the v3 progress gate solely from authoritative raw commits."""

        coverage = derive_reporter_coverage_plan(self._profile)
        required = coverage.get("required_postfault_tree_positions")
        if required is None:
            return None
        required_count = _integer(required, "required post-fault commit horizon", 1)
        measurement = _document(
            self._profile.raw.get("measurement"), "profile measurement"
        )
        observer = _integer(
            measurement.get("authoritative_replica_id"), "authoritative observer", 0
        )
        expected_source = f"replica-{observer}"
        expected_instance = _authoritative_lifecycle_instance(events, expected_source)
        expected_digest = _document(
            self._profile.raw.get("topology"), "profile topology"
        ).get("epoch_zero_digest")
        transactions_per_block = _integer(
            _document(self._profile.raw.get("protocol"), "profile protocol").get(
                "transactions_per_block"
            ),
            "profile transactions per block",
            1,
        )
        member_sources = {f"replica-{member}" for member in self._profile.replica_ids}
        if any(
            event.get("source_kind") == "replica"
            and event.get("source_id") in member_sources
            and event.get("event_type") == "adaptive.configuration_active"
            and prefault_ns
            <= _integer(event.get("source_monotonic_ns"), "configuration timestamp")
            <= fault_ns
            for event in events
        ):
            _error("configuration changed during the atomic fault batch")
        start_events = [
            event
            for event in events
            if event.get("source_kind") == "replica"
            and event.get("source_id") == expected_source
            and event.get("source_instance") == expected_instance
            and event.get("event_type") == "adaptive.configuration_active"
            and _integer(event.get("source_monotonic_ns"), "configuration timestamp")
            < prefault_ns
        ]
        if not start_events:
            _error("raw authoritative progress lacks a pre-fault configuration")
        historical_configurations = sorted(
            start_events,
            key=lambda event: (
                _integer(event.get("source_sequence"), "configuration sequence", 1),
                _integer(event.get("source_monotonic_ns"), "configuration timestamp"),
            ),
        )
        for position, event in enumerate(historical_configurations):
            payload = _document(event.get("payload"), "pre-fault configuration")
            epoch = _integer(payload.get("epoch_number"), "configuration epoch")
            tree = _integer(payload.get("tree_id"), "configuration tree")
            if (
                epoch != 0
                or payload.get("epoch_digest") != expected_digest
                or tree
                != self._profile.replica_ids[position % len(self._profile.replica_ids)]
            ):
                _error("raw authoritative progress historical configuration drifted")
        start = max(
            start_events,
            key=lambda event: (
                _integer(event.get("source_sequence"), "configuration sequence", 1),
                _integer(event.get("source_monotonic_ns"), "configuration timestamp"),
            ),
        )
        start_payload = _document(start.get("payload"), "pre-fault configuration")
        starting_tree = _integer(start_payload.get("tree_id"), "starting tree")
        if (
            _integer(start_payload.get("epoch_number"), "starting epoch") != 0
            or start_payload.get("epoch_digest") != expected_digest
            or starting_tree
            != _integer(
                derive_reporter_coverage_plan(self._profile).get("active_tree_id"),
                "active tree",
            )
        ):
            _error("raw authoritative progress pre-fault configuration drifted")
        members = tuple(self._profile.replica_ids)
        activations = sorted(
            [
                event
                for event in events
                if event.get("source_kind") == "replica"
                and event.get("source_id") == expected_source
                and event.get("source_instance") == expected_instance
                and event.get("event_type") == "adaptive.configuration_active"
                and fault_ns
                < _integer(event.get("source_monotonic_ns"), "configuration timestamp")
                < audit_ns
            ],
            key=lambda event: (
                _integer(event.get("source_sequence"), "configuration sequence", 1),
                _integer(event.get("source_monotonic_ns"), "configuration timestamp"),
            ),
        )
        # The active pre-fault tree is position one of the frozen horizon.
        observed_trees: list[int] = [starting_tree]
        for position, event in enumerate(activations, start=1):
            payload = _document(event.get("payload"), "post-fault configuration")
            tree = _integer(payload.get("tree_id"), "activated tree")
            if (
                _integer(payload.get("epoch_number"), "activated epoch") != 0
                or payload.get("epoch_digest") != expected_digest
                or tree
                != members[(members.index(starting_tree) + position) % len(members)]
            ):
                _error("raw authoritative progress cyclic configuration drifted")
            observed_trees.append(tree)
        if len(observed_trees) < required_count:
            return None
        configurations = [
            (
                _integer(event.get("source_sequence"), "configuration sequence", 1),
                _integer(event.get("source_monotonic_ns"), "configuration timestamp"),
                _integer(
                    _document(event.get("payload"), "pre-fault configuration").get(
                        "tree_id"
                    ),
                    "configuration tree",
                ),
            )
            for event in historical_configurations
        ] + [
            (
                _integer(event.get("source_sequence"), "configuration sequence", 1),
                _integer(event.get("source_monotonic_ns"), "configuration timestamp"),
                tree,
            )
            for event, tree in zip(activations, observed_trees[1:], strict=True)
        ]
        for event in events:
            if event.get("event_type") != "block.committed":
                continue
            if (
                event.get("source_kind") != "replica"
                or event.get("source_id") != expected_source
                or event.get("source_instance") != expected_instance
            ):
                continue
            timestamp = _integer(
                event.get("source_monotonic_ns"), "authoritative progress timestamp"
            )
            if not fault_ns < timestamp < audit_ns:
                continue
            payload = _document(event.get("payload"), "authoritative progress commit")
            proof = _document(payload.get("decision_proof"), "progress decision proof")
            if set(payload) != {
                "block_height",
                "block_hash",
                "parent_hash",
                "transaction_count",
                "commit_batch_index",
                "designated_observer",
                "decision_proof",
                "view_generation",
            }:
                _error("raw authoritative progress commit schema drifted")
            if set(proof) != {
                "epoch_number",
                "tree_id",
                "epoch_digest",
                "block_hash",
            }:
                _error("raw authoritative progress proof schema drifted")
            block_hash = _digest(payload.get("block_hash"), "progress commit hash")
            proof_tree = _integer(proof.get("tree_id"), "progress proof tree")
            proof_epoch = _integer(proof.get("epoch_number"), "progress proof epoch")
            _uint64(payload.get("commit_batch_index"), "progress commit batch index")
            view_generation = _uint64(
                payload.get("view_generation"), "progress view generation", 1
            )
            if (
                payload.get("designated_observer") is not True
                or _uint64(payload.get("transaction_count"), "progress transactions")
                not in {0, transactions_per_block}
                or proof.get("block_hash") != block_hash
                or proof_epoch != 0
                or proof.get("epoch_digest") != expected_digest
            ):
                _error("raw authoritative progress commit invariants drifted")
            commit_key = (
                _integer(event.get("source_sequence"), "progress source sequence", 1),
                timestamp,
            )
            if view_generation > len(configurations):
                _error("raw authoritative progress commit generation is not activated")
            generation_configuration = configurations[view_generation - 1]
            if (
                generation_configuration[:2] > commit_key
                or generation_configuration[2] != proof_tree
            ):
                _error("raw authoritative progress commit is not causally activated")
        return {
            "required_tree_positions": required_count,
            "actual_tree_positions": len(observed_trees),
            "starting_tree_id": starting_tree,
            "observed_tree_ids": observed_trees,
        }

    def _transition(
        self,
        events: Sequence[Mapping[str, Any]],
        epoch: int,
        *,
        activation: bool,
        diagnostic_allow_incomplete: bool = False,
    ) -> dict[str, object] | None:
        wire, decoded = self._bundle(epoch)
        event_type = "epoch.activated" if activation else "epoch.command_committed"
        number_key = "epoch_number" if activation else "successor_epoch_number"
        expected_payload_keys = (
            {
                "epoch_number",
                "tree_id",
                "epoch_digest",
                "activation_height",
            }
            if activation
            else {
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
        if activation and _is_v13_profile(self._profile):
            expected_payload_keys |= {
                "certificate_apply_committed_height",
                "activation_readiness_certificate_digest",
            }
        selected: list[Mapping[str, Any]] = []
        for event in events:
            if event["event_type"] != event_type:
                continue
            payload = _document(event["payload"], "transition payload")
            if set(payload) != expected_payload_keys:
                _error(f"raw Epoch {epoch} transition payload schema drifted")
            reported_epoch = _integer(
                payload.get(number_key), "transition epoch number", 0
            )
            if reported_epoch == epoch:
                selected.append(event)
        survivors = tuple(
            replica
            for replica in self._profile.replica_ids
            if replica not in self._profile.target_replica_ids
        )
        expected_sources = {f"replica-{replica}" for replica in survivors}
        if not selected:
            return None
        if len(selected) > len(survivors):
            _error(f"raw Epoch {epoch} transition lacks every survivor")
        source_ids: set[str] = set()
        payloads: set[str] = set()
        launched_instances = getattr(self, "_expected_source_instances", {})
        for event in selected:
            source_id = str(event["source_id"])
            if event["source_kind"] != "replica" or source_id not in expected_sources:
                _error(f"raw Epoch {epoch} transition source drifted")
            if launched_instances and event.get(
                "source_instance"
            ) != launched_instances.get(source_id):
                _error(f"raw Epoch {epoch} transition launch identity drifted")
            if source_id in source_ids:
                _error(f"raw Epoch {epoch} transition duplicates a survivor")
            source_ids.add(source_id)
            payload = _document(event["payload"], "transition payload")
            if set(payload) != expected_payload_keys:
                _error(f"raw Epoch {epoch} transition payload schema drifted")
            activation_height = _integer(
                payload.get("activation_height"), "transition activation height", 1
            )
            if activation:
                if (
                    _integer(payload.get("epoch_number"), "transition epoch number", 0)
                    != epoch
                    or _integer(payload.get("tree_id"), "transition tree") != 0
                    or _digest(payload.get("epoch_digest"), "transition epoch digest")
                    != decoded.epoch_digest
                ):
                    _error(f"raw Epoch {epoch} transition payload drifted")
                if _is_v13_profile(self._profile) and (
                    _integer(
                        payload.get("certificate_apply_committed_height"),
                        "transition certificate apply height",
                        1,
                    )
                    < activation_height
                    or _digest(
                        payload.get("activation_readiness_certificate_digest"),
                        "transition readiness certificate digest",
                    )
                    == "0" * 64
                ):
                    _error(
                        f"raw Epoch {epoch} transition readiness binding drifted"
                    )
            else:
                command_height = _integer(
                    payload.get("command_block_height"),
                    "transition command height",
                    1,
                )
                activation_delay = _integer(
                    payload.get("activation_delay_blocks"),
                    "transition activation delay",
                    1,
                )
                if (
                    _digest(
                        payload.get("command_block_hash"), "transition command hash"
                    )
                    == "0" * 64
                    or _digest(
                        payload.get("payload_digest"), "transition payload digest"
                    )
                    != decoded.command.payload_digest
                    or _integer(
                        payload.get("predecessor_epoch_number"),
                        "transition predecessor epoch",
                        0,
                    )
                    != epoch - 1
                    or _digest(
                        payload.get("predecessor_epoch_digest"),
                        "transition predecessor digest",
                    )
                    != decoded.previous_epoch_digest
                    or _integer(
                        payload.get("successor_epoch_number"),
                        "transition successor epoch",
                        1,
                    )
                    != epoch
                    or _digest(
                        payload.get("successor_epoch_digest"),
                        "transition successor digest",
                    )
                    != decoded.epoch_digest
                    or activation_delay != decoded.command.activation_delay_blocks
                    or activation_height != command_height + activation_delay
                ):
                    _error(f"raw Epoch {epoch} transition payload drifted")
            comparable_payload = dict(payload)
            if activation and _is_v13_profile(self._profile):
                # Certified activation is bound to the scheduled boundary,
                # but a valid certificate may arrive at a later local
                # committed height.  Replicas therefore need not agree on
                # the diagnostic application height.
                comparable_payload.pop("certificate_apply_committed_height")
            payloads.add(_canonical_json(comparable_payload))
        if len(payloads) != 1:
            _error(f"raw Epoch {epoch} transition payloads conflict")
        if source_ids != expected_sources and not diagnostic_allow_incomplete:
            return None
        payload = _document(selected[0]["payload"], "transition payload")
        snapshot = {
            "survivor_replica_ids": [
                replica
                for replica in survivors
                if f"replica-{replica}" in source_ids
            ],
            "witness_count": len(source_ids),
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
        if _is_v10_profile(self._profile):
            snapshot["first_source_monotonic_ns"] = min(
                _integer(event["source_monotonic_ns"], "transition timestamp")
                for event in selected
            )
        if not activation:
            snapshot["command_block_height"] = _integer(
                payload.get("command_block_height"), "transition command height", 1
            )
        return snapshot

    def _common_commit(
        self, events: Sequence[Mapping[str, Any]], epoch: int
    ) -> dict[str, object] | None:
        _wire, decoded = self._bundle(epoch)
        authoritative_keys = {
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "commit_batch_index",
            "designated_observer",
            "view_generation",
            "decision_proof",
        }
        proof_keys = {"epoch_number", "tree_id", "epoch_digest", "block_hash"}
        observation_keys = {
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "commit_batch_index",
        }

        def commit_identity(event: Mapping[str, Any]) -> tuple[int, dict[str, object]]:
            payload = _document(event["payload"], "authoritative commit")
            proof = _document(payload.get("decision_proof"), "commit decision proof")
            proof_epoch = _integer(proof.get("epoch_number"), "commit proof epoch", 0)
            if proof_epoch != epoch:
                return proof_epoch, {}
            if set(payload) != authoritative_keys or set(proof) != proof_keys:
                _error("raw authoritative commit schema drifted")
            identity = {
                "block_height": _integer(
                    payload.get("block_height"), "commit height", 1
                ),
                "block_hash": _digest(payload.get("block_hash"), "commit hash"),
                "parent_hash": _digest(
                    payload.get("parent_hash"), "commit parent hash"
                ),
                "transaction_count": _integer(
                    payload.get("transaction_count"), "commit transactions", 0
                ),
                "commit_batch_index": _integer(
                    payload.get("commit_batch_index"), "commit batch index", 0
                ),
            }
            _uint64(payload.get("transaction_count"), "commit transactions")
            _uint64(payload.get("commit_batch_index"), "commit batch index")
            if (
                self._profile.profile_id in _FCRASH_H_V3_PROFILE_IDS
                or _is_v4_profile(self._profile)
            ) and _uint64(
                payload.get("transaction_count"), "commit transactions"
            ) not in {
                0,
                _integer(
                    _document(
                        self._profile.raw.get("protocol"), "profile protocol"
                    ).get("transactions_per_block"),
                    "profile transactions per block",
                    1,
                ),
            }:
                _error("raw authoritative commit workload drifted")
            if (
                _uint64(payload.get("view_generation"), "commit view generation", 1) < 1
                or _integer(proof.get("tree_id"), "commit proof tree", 0) < 0
                or _digest(proof.get("epoch_digest"), "commit proof epoch digest")
                != decoded.epoch_digest
                or _digest(proof.get("block_hash"), "commit proof hash")
                != identity["block_hash"]
            ):
                _error("raw authoritative commit invariants drifted")
            return proof_epoch, identity

        replica_instances: dict[str, str] = {}

        def expected_replica_instance(source_id: str) -> str:
            cached = replica_instances.get(source_id)
            if cached is not None:
                return cached
            launched = getattr(self, "_expected_source_instances", {})
            if launched:
                instance = launched.get(source_id)
                if not isinstance(instance, str) or not instance:
                    _error("raw common commit witness launch identity is absent")
                lifecycle = [
                    event
                    for event in events
                    if event.get("event_type") in {"process.started", "process.ready"}
                    and event.get("source_id") == source_id
                ]
                if (
                    lifecycle
                    and _authoritative_lifecycle_instance(events, source_id) != instance
                ):
                    _error("raw common commit witness lifecycle drifted")
                replica_instances[source_id] = instance
                return instance
            lifecycle = [
                event
                for event in events
                if event.get("event_type") in {"process.started", "process.ready"}
                and event.get("source_id") == source_id
            ]
            if lifecycle:
                instance = _authoritative_lifecycle_instance(events, source_id)
                replica_instances[source_id] = instance
                return instance
            instances = {
                event.get("source_instance")
                for event in events
                if event.get("source_kind") == "replica"
                and event.get("source_id") == source_id
            }
            if len(instances) != 1 or not isinstance(next(iter(instances)), str):
                _error("raw common commit witness lifecycle is ambiguous")
            instance = str(next(iter(instances)))
            replica_instances[source_id] = instance
            return instance

        measurement = _document(
            self._profile.raw.get("measurement"), "profile measurement"
        )
        authoritative_source = f"replica-{_integer(measurement.get('authoritative_replica_id'), 'authoritative observer', 0)}"
        is_v3 = self._profile.profile_id in _FCRASH_H_V3_PROFILE_IDS or _is_v4_profile(
            self._profile
        )
        configurations: list[tuple[tuple[int, int], int]] = []
        if is_v3:
            tree_ids = tuple(tree.tree_id for tree in decoded.trees)
            for event in events:
                if (
                    event.get("event_type") != "adaptive.configuration_active"
                    or event.get("source_kind") != "replica"
                    or event.get("source_id") != authoritative_source
                    or event.get("source_instance")
                    != expected_replica_instance(authoritative_source)
                ):
                    continue
                payload = _document(event.get("payload"), "authoritative configuration")
                if (
                    _integer(payload.get("epoch_number"), "configuration epoch", 0)
                    != epoch
                ):
                    continue
                tree = _integer(payload.get("tree_id"), "configuration tree", 0)
                if (
                    _digest(payload.get("epoch_digest"), "configuration epoch digest")
                    != decoded.epoch_digest
                    or tree != tree_ids[len(configurations) % len(tree_ids)]
                ):
                    _error("raw authoritative cyclic configuration drifted")
                configurations.append(
                    (
                        (
                            _integer(
                                event.get("source_sequence"),
                                "configuration sequence",
                                1,
                            ),
                            _integer(
                                event.get("source_monotonic_ns"),
                                "configuration timestamp",
                            ),
                        ),
                        tree,
                    )
                )
            configurations.sort()
        commits: list[Mapping[str, Any]] = []
        for event in events:
            if event["event_type"] != "block.committed":
                continue
            proof_epoch, _identity = commit_identity(event)
            if proof_epoch == epoch:
                source_id = str(event["source_id"])
                if (
                    event["source_kind"] != "replica"
                    or not source_id.startswith("replica-")
                    or not source_id.removeprefix("replica-").isdigit()
                    or int(source_id.removeprefix("replica-"))
                    not in self._profile.replica_ids
                    or event.get("source_instance")
                    != expected_replica_instance(source_id)
                ):
                    _error("raw authoritative commit source drifted")
                designated = _document(event["payload"], "authoritative commit").get(
                    "designated_observer"
                )
                if type(designated) is not bool or designated != (
                    source_id == authoritative_source
                ):
                    _error("raw authoritative commit observer drifted")
                if source_id == authoritative_source:
                    if is_v3:
                        payload = _document(event["payload"], "authoritative commit")
                        generation = _uint64(
                            payload.get("view_generation"), "commit view generation", 1
                        )
                        ordinal = generation - ((epoch << 32) + 1)
                        proof_tree = _integer(
                            _document(
                                payload.get("decision_proof"), "commit decision proof"
                            ).get("tree_id"),
                            "commit proof tree",
                            0,
                        )
                        key = (
                            _integer(
                                event["source_sequence"], "commit source sequence", 1
                            ),
                            _integer(event["source_monotonic_ns"], "commit timestamp"),
                        )
                        if (
                            ordinal < 0
                            or ordinal >= len(configurations)
                            or configurations[ordinal][0] > key
                            or configurations[ordinal][1] != proof_tree
                        ):
                            _error(
                                "raw authoritative commit is not bound to its active generation"
                            )
                    commits.append(event)
        if not commits:
            return None
        observers: list[tuple[Mapping[str, Any], dict[str, object]]] = []
        for event in events:
            if event["event_type"] != "block.commit_observed":
                continue
            payload = _document(event["payload"], "common commit observation")
            if set(payload) != observation_keys:
                _error("raw common commit observation schema drifted")
            source_id = str(event["source_id"])
            if (
                event["source_kind"] != "replica"
                or not source_id.startswith("replica-")
                or not source_id.removeprefix("replica-").isdigit()
                or int(source_id.removeprefix("replica-"))
                not in self._profile.replica_ids
                or event.get("source_instance") != expected_replica_instance(source_id)
            ):
                _error("raw common commit witness source drifted")
            observers.append(
                (
                    event,
                    {
                        "block_height": _integer(
                            payload.get("block_height"), "observation height", 1
                        ),
                        "block_hash": _digest(
                            payload.get("block_hash"), "observation hash"
                        ),
                        "parent_hash": _digest(
                            payload.get("parent_hash"), "observation parent hash"
                        ),
                        "transaction_count": _integer(
                            payload.get("transaction_count"),
                            "observation transactions",
                            0,
                        ),
                        "commit_batch_index": _integer(
                            payload.get("commit_batch_index"),
                            "observation commit batch index",
                            0,
                        ),
                    },
                )
            )
        survivors = set(self._profile.replica_ids) - set(
            self._profile.target_replica_ids
        )

        matches: list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = []
        for commit in commits:
            _proof_epoch, identity = commit_identity(commit)
            identity_matching = [
                event
                for event, observed_identity in observers
                if observed_identity == identity
            ]
            witnessed: list[Mapping[str, Any]] = []
            witnessed_sources: set[int] = set()
            for event in identity_matching:
                source_id = str(event["source_id"])
                if (
                    event["source_kind"] != "replica"
                    or not source_id.startswith("replica-")
                    or not source_id.removeprefix("replica-").isdigit()
                ):
                    _error("raw common commit witness source drifted")
                replica = int(source_id.removeprefix("replica-"))
                if replica not in survivors:
                    _error("raw common commit witness is outside survivors")
                if event.get("source_instance") != expected_replica_instance(source_id):
                    _error("raw common commit witness lifecycle drifted")
                if replica in witnessed_sources:
                    _error("raw common commit witness duplicates a survivor")
                witnessed_sources.add(replica)
                witnessed.append(event)
            witness_ids = {
                int(str(event["source_id"]).removeprefix("replica-"))
                for event in witnessed
            }
            if len(witness_ids) >= self._profile.quorum:
                matches.append((commit, witnessed))
        if not matches:
            return None
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
        snapshot = {
            "epoch_number": epoch,
            "epoch_digest": decoded.epoch_digest,
            "authoritative_commit_count": 1,
            "observed_replica_ids": witness_ids,
            "block_height": _integer(payload.get("block_height"), "commit height", 1),
            "block_hash": _digest(payload.get("block_hash"), "commit hash"),
            "parent_hash": payload.get("parent_hash"),
            "transaction_count": _integer(
                payload.get("transaction_count"), "commit transactions", 0
            ),
            "commit_batch_index": _integer(
                payload.get("commit_batch_index"), "commit batch index"
            ),
            "source_monotonic_ns": max(
                _integer(event["source_monotonic_ns"], "commit witness timestamp")
                for event in witnessed
            ),
        }
        if _is_v10_profile(self._profile):
            snapshot["first_common_commit_anchor_monotonic_ns"] = (
                _runtime_first_common_commit_anchor(
                    events,
                    self._profile,
                    epoch_number=epoch,
                    after_ns=max(
                        _integer(
                            event["source_monotonic_ns"],
                            "transition activation timestamp",
                        )
                        for event in events
                        if event.get("event_type") == "epoch.activated"
                        and _document(
                            event.get("payload"), "transition activation"
                        ).get("epoch_number")
                        == epoch
                    ),
                )
            )
        return snapshot

    def _manager_clean_exit_is_expected(
        self,
        events: Sequence[Mapping[str, Any]],
        record: object,
        *,
        diagnostic_allow_incomplete_transitions: bool = False,
    ) -> bool:
        """Accept only a fully witnessed, successful manager completion."""

        if (
            getattr(record, "name", None) != "adaptive-manager"
            or getattr(record, "replica_id", None) != -1
        ):
            return False
        process = getattr(record, "process", None)
        if process is None or process.poll() != 0:
            return False
        try:
            expected_epochs = (1, 2) if self._arm == "A" else (1,)
            cache_key = _sha256(
                _canonical_json(
                    {
                        "arm": self._arm,
                        "returncode": 0,
                        "diagnostic_allow_incomplete_transitions": (
                            diagnostic_allow_incomplete_transitions
                        ),
                        "bundles": {
                            str(epoch): _sha256(self._bundle(epoch)[0])
                            for epoch in expected_epochs
                        },
                        "events": [
                            event
                            for event in events
                            if (
                                event["source_kind"] == "adaptation_manager"
                                and event["source_id"] == "adaptive-manager"
                            )
                            or event["event_type"]
                            in {
                                "adaptive_v2_evidence_snapshot",
                                "adaptive_v2_session_terminal",
                                "epoch.command_committed",
                                "epoch.activated",
                                "process.stopping",
                                "process.stopped",
                            }
                        ],
                    }
                )
            )
            if getattr(self, "_manager_clean_exit_cache_key", None) == cache_key:
                return True
            terminal_keys = {
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
            winning_keys = {
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
            manager_events = [
                event
                for event in events
                if event["source_kind"] == "adaptation_manager"
                and event["source_id"] == "adaptive-manager"
            ]
            terminals = [
                event
                for event in manager_events
                if event["event_type"] == "adaptive_v2_session_terminal"
            ]
            if len(terminals) != len(expected_epochs):
                return False
            if [event["event_type"] for event in manager_events[-2:]] != [
                "process.stopping",
                "process.stopped",
            ]:
                return False
            if any(
                _document(event["payload"], "manager clean-exit tail")
                != {"exit_status": None}
                for event in manager_events[-2:]
            ):
                return False
            final_terminal = terminals[-1]
            final_terminal_index = next(
                index
                for index, event in enumerate(manager_events)
                if event is final_terminal
            )
            terminal_tail = manager_events[final_terminal_index:]
            if [int(event["source_sequence"]) for event in terminal_tail] != list(
                range(
                    int(final_terminal["source_sequence"]),
                    int(final_terminal["source_sequence"]) + len(terminal_tail),
                )
            ):
                return False
            acknowledgement_tail = terminal_tail[1:-2]
            for epoch, terminal in zip(expected_epochs, terminals, strict=True):
                if epoch == expected_epochs[-1] and terminal is not final_terminal:
                    return False
                payload = _document(terminal["payload"], "manager terminal")
                if (
                    set(payload)
                    not in (
                        terminal_keys,
                        terminal_keys | {"controller_failure"},
                    )
                    or payload.get("controller_failure") is not None
                ):
                    return False
                command = self._transition(
                    events,
                    epoch,
                    activation=False,
                    diagnostic_allow_incomplete=(
                        diagnostic_allow_incomplete_transitions
                    ),
                )
                activation = self._transition(
                    events,
                    epoch,
                    activation=True,
                    diagnostic_allow_incomplete=(
                        diagnostic_allow_incomplete_transitions
                    ),
                )
                if command is None or activation is None:
                    return False
                ranking = self._ranking(events, predecessor_epoch=epoch - 1)
                audits = [
                    event
                    for event in manager_events
                    if event["event_type"] == "adaptive_v2_evidence_snapshot"
                    and _integer(
                        _document(event["payload"], "manager snapshot audit").get(
                            "predecessor_epoch_number"
                        ),
                        "manager snapshot predecessor epoch",
                        0,
                    )
                    == epoch - 1
                ]
                if len(audits) != 1:
                    return False
                audit = _document(audits[0]["payload"], "manager snapshot audit")
                _wire, decoded = self._bundle(epoch)
                predecessor_digest = (
                    _document(self._profile.raw["topology"], "profile topology").get(
                        "epoch_zero_digest"
                    )
                    if epoch == 1
                    else self._bundle(epoch - 1)[1].epoch_digest
                )
                expected_artifact = (
                    "e0-to-e1-containment" if epoch == 1 else "e1-to-e2-optimization"
                )
                winning = _document(
                    payload.get("winning_activation"), "manager winning activation"
                )
                terminal_cycle = _integer(
                    payload.get("cycle_ordinal"), "manager terminal cycle", 0
                )
                terminal_predecessor = _integer(
                    payload.get("predecessor_epoch_number"),
                    "manager terminal predecessor epoch",
                    0,
                )
                terminal_successor = _integer(
                    payload.get("successor_epoch_number"),
                    "manager terminal successor epoch",
                    1,
                )
                terminal_generation = _uint64(
                    payload.get("evidence_window_activation_generation"),
                    "manager terminal activation generation",
                    1,
                )
                terminal_baseline = _integer(
                    payload.get("baseline_evidence_cutoff"),
                    "manager terminal baseline cutoff",
                    0,
                )
                terminal_current = _integer(
                    payload.get("current_evidence_cutoff"),
                    "manager terminal current cutoff",
                    0,
                )
                winning_numeric = {
                    "predecessor_epoch_number": _integer(
                        winning.get("predecessor_epoch_number"),
                        "manager winning predecessor epoch",
                        0,
                    ),
                    "successor_epoch_number": _integer(
                        winning.get("successor_epoch_number"),
                        "manager winning successor epoch",
                        1,
                    ),
                    "command_block_height": _integer(
                        winning.get("command_block_height"),
                        "manager winning command height",
                        1,
                    ),
                    "activation_delay_blocks": _integer(
                        winning.get("activation_delay_blocks"),
                        "manager winning activation delay",
                        1,
                    ),
                    "activation_height": _integer(
                        winning.get("activation_height"),
                        "manager winning activation height",
                        1,
                    ),
                }
                if (
                    set(winning) != winning_keys
                    or terminal_cycle != epoch - 1
                    or payload.get("policy_intent")
                    != (
                        "fault_containment"
                        if epoch == 1
                        else "performance_optimization"
                    )
                    or payload.get("outcome") != "advanced"
                    or payload.get("reason") != "successor_converged"
                    or payload.get("transition_artifact_id") != expected_artifact
                    or terminal_predecessor != epoch - 1
                    or payload.get("predecessor_epoch_digest") != predecessor_digest
                    or terminal_successor != epoch
                    or payload.get("successor_epoch_digest") != decoded.epoch_digest
                    or payload.get("command_payload_digest")
                    != decoded.command.payload_digest
                    or terminal_generation
                    != _uint64(
                        audit.get("activation_generation"),
                        "manager snapshot activation generation",
                        1,
                    )
                    or terminal_baseline
                    != _integer(
                        ranking.get("baseline_evidence_cutoff"),
                        "manager ranking baseline cutoff",
                        0,
                    )
                    or terminal_current
                    != _integer(
                        ranking.get("current_evidence_cutoff"),
                        "manager ranking current cutoff",
                        0,
                    )
                ):
                    return False
                if epoch == expected_epochs[-1]:
                    if len(acknowledgement_tail) % 2 != 0:
                        return False
                    expected_winning = {
                        **{
                            key: (
                                _digest(
                                    winning.get(key),
                                    f"manager winning {key}",
                                )
                                if key
                                in {
                                    "predecessor_epoch_digest",
                                    "successor_epoch_digest",
                                    "command_payload_digest",
                                    "command_block_hash",
                                }
                                else winning.get(key)
                            )
                            for key in winning_keys
                            if key not in winning_numeric
                        },
                        **winning_numeric,
                    }
                    for index in range(0, len(acknowledgement_tail), 2):
                        duplicate = acknowledgement_tail[index]
                        acknowledged = acknowledgement_tail[index + 1]
                        observed_event_type = duplicate["event_type"]
                        if (
                            observed_event_type
                            not in {
                                "adaptive_v2_activation_observed",
                                "adaptive_v2_commit_observed",
                            }
                            or acknowledged["event_type"] != observed_event_type
                        ):
                            return False
                        normalized_payloads: list[dict[str, object]] = []
                        for observed in (duplicate, acknowledged):
                            observed_payload = _document(
                                observed["payload"],
                                "manager post-terminal convergence acknowledgement",
                            )
                            if set(observed_payload) != {
                                "replica_id",
                                "delivery_attempt",
                                "disposition",
                                "identity",
                                "accepted_commit_count",
                                "accepted_activation_count",
                                "required_activation_count",
                                "canonical_payload_digest",
                                "failure_reason",
                            }:
                                return False
                            replica_id = _integer(
                                observed_payload.get("replica_id"),
                                "manager post-terminal convergence replica",
                                0,
                            )
                            identity = _document(
                                observed_payload.get("identity"),
                                "manager post-terminal convergence identity",
                            )
                            if set(identity) != winning_keys:
                                return False
                            normalized_identity = {
                                key: (
                                    _integer(
                                        identity.get(key),
                                        f"manager post-terminal convergence {key}",
                                        0,
                                    )
                                    if key
                                    in {
                                        "predecessor_epoch_number",
                                        "successor_epoch_number",
                                        "command_block_height",
                                        "activation_delay_blocks",
                                        "activation_height",
                                    }
                                    else _digest(
                                        identity.get(key),
                                        f"manager post-terminal convergence {key}",
                                    )
                                )
                                for key in winning_keys
                            }
                            normalized_payloads.append(
                                {
                                    "replica_id": replica_id,
                                    "delivery_attempt": observed_payload.get(
                                        "delivery_attempt"
                                    ),
                                    "disposition": observed_payload.get("disposition"),
                                    "identity": normalized_identity,
                                    "accepted_commit_count": _integer(
                                        observed_payload.get("accepted_commit_count"),
                                        "manager post-terminal accepted commits",
                                        0,
                                    ),
                                    "accepted_activation_count": _integer(
                                        observed_payload.get(
                                            "accepted_activation_count"
                                        ),
                                        "manager post-terminal accepted activations",
                                        0,
                                    ),
                                    "required_activation_count": _integer(
                                        observed_payload.get(
                                            "required_activation_count"
                                        ),
                                        "manager post-terminal required activations",
                                        1,
                                    ),
                                    "canonical_payload_digest": _digest(
                                        observed_payload.get(
                                            "canonical_payload_digest"
                                        ),
                                        "manager post-terminal payload digest",
                                    ),
                                    "failure_reason": observed_payload.get(
                                        "failure_reason"
                                    ),
                                }
                            )
                        first, second = normalized_payloads
                        replica_id = int(first["replica_id"])
                        if (
                            first["disposition"] != "duplicate"
                            or second["disposition"]
                            not in {"ack_sent", "ack_send_failed"}
                            or {**first, "disposition": None}
                            != {**second, "disposition": None}
                            or replica_id not in self._profile.replica_ids
                            or replica_id in self._profile.target_replica_ids
                            or first["delivery_attempt"] is not None
                            or first["identity"] != expected_winning
                            or first["accepted_commit_count"]
                            > len(self._profile.replica_ids)
                            - len(self._profile.target_replica_ids)
                            or first["accepted_activation_count"]
                            != self._profile.quorum
                            or first["required_activation_count"]
                            != self._profile.quorum
                            or first["failure_reason"] is not None
                        ):
                            return False
                command_payload = {
                    key: command[key]
                    for key in (
                        "command_block_height",
                        "activation_height",
                    )
                }
                command_event = next(
                    event
                    for event in events
                    if event["event_type"] == "epoch.command_committed"
                    and _document(event["payload"], "transition payload").get(
                        "successor_epoch_number"
                    )
                    == epoch
                )
                full_command = _document(command_event["payload"], "transition payload")
                if (
                    {
                        **{
                            key: winning.get(key)
                            for key in winning_keys
                            if key
                            not in {
                                "predecessor_epoch_number",
                                "successor_epoch_number",
                                "command_block_height",
                                "activation_delay_blocks",
                                "activation_height",
                            }
                        },
                        **winning_numeric,
                    }
                    != {
                        **{
                            key: (
                                _integer(
                                    full_command.get(key), "transition numeric field", 0
                                )
                                if key
                                in {
                                    "predecessor_epoch_number",
                                    "successor_epoch_number",
                                    "command_block_height",
                                    "activation_delay_blocks",
                                    "activation_height",
                                }
                                else full_command.get(key)
                            )
                            for key in winning_keys
                            if key != "command_payload_digest"
                        },
                        "command_payload_digest": full_command.get("payload_digest"),
                    }
                    or winning_numeric["activation_height"]
                    != command_payload["activation_height"]
                    or winning_numeric["activation_height"]
                    != activation["activation_height"]
                ):
                    return False
            self._manager_clean_exit_cache_key = cache_key
            return True
        except (FocusedCrashPairRuntimeError, KeyError, StopIteration, TypeError):
            return False

    def _manager_successfully_stopped(
        self,
        events: Sequence[Mapping[str, Any]],
    ) -> bool:
        """Reuse the full clean-exit proof with diagnostic-only partial barriers."""

        records = [
            record
            for record in self._process_records
            if getattr(record, "name", None) == "adaptive-manager"
            and getattr(record, "replica_id", None) == -1
        ]
        return len(records) == 1 and self._manager_clean_exit_is_expected(
            events,
            records[0],
            diagnostic_allow_incomplete_transitions=True,
        )

    def _transition_missing_survivor_ids(
        self,
        events: Sequence[Mapping[str, Any]],
        epoch: int,
        *,
        activation: bool,
    ) -> tuple[int, ...]:
        """Validate a transition prefix and return its exact absent survivors."""

        if self._transition(events, epoch, activation=activation) is not None:
            return ()
        event_type = "epoch.activated" if activation else "epoch.command_committed"
        number_key = "epoch_number" if activation else "successor_epoch_number"
        observed = {
            int(str(event["source_id"]).removeprefix("replica-"))
            for event in events
            if event.get("event_type") == event_type
            and _document(event.get("payload"), "transition payload").get(number_key)
            == epoch
        }
        survivors = {
            replica
            for replica in self._profile.replica_ids
            if replica not in self._profile.target_replica_ids
        }
        return tuple(sorted(survivors - observed))

    def _transition_barrier_states(
        self, events: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, object]]:
        """Reconstruct every expected transition barrier from one raw snapshot."""

        epochs = (1, 2) if self._arm == "A" else (1,)
        states: list[dict[str, object]] = []
        for epoch in epochs:
            for activation in (False, True):
                missing = self._transition_missing_survivor_ids(
                    events, epoch, activation=activation
                )
                states.append(
                    {
                        "phase": (
                            f"activations{epoch}" if activation else f"commands{epoch}"
                        ),
                        "epoch_number": epoch,
                        "transition_kind": (
                            "activation" if activation else "command"
                        ),
                        "missing_survivor_ids": list(missing),
                    }
                )
        return states

    def _persist_natural_process_exits(
        self, exits: Sequence[Mapping[str, object]]
    ) -> None:
        if not exits or not getattr(self, "_persist_exit_diagnostics", False):
            return
        directory = self._root / "runtime" / "natural-process-exits"
        if directory.exists() and (not directory.is_dir() or directory.is_symlink()):
            _error("natural process exit diagnostic directory is absent")
        directory.mkdir(mode=0o700, exist_ok=True)
        persisted = getattr(self, "_persisted_natural_exit_identities", set())
        for exit_document in exits:
            identity = (
                int(exit_document["replica_id"]),
                int(exit_document["pid"]),
                int(exit_document["pgid"]),
            )
            if identity in persisted:
                continue
            document = {
                "schema_version": 1,
                "kind": "kauri-focused-natural-process-exit-v1",
                "clock_domain": "same_host_clock_monotonic_raw",
                **dict(exit_document),
            }
            profiled_fault_runtime.write_exclusive(
                directory
                / f"replica-{identity[0]}-pid-{identity[1]}-pgid-{identity[2]}.json",
                _canonical_json(document),
            )
            persisted.add(identity)
        self._persisted_natural_exit_identities = persisted

    def unexpected_exit_ids(
        self, events: Sequence[Mapping[str, Any]] | None = None
    ) -> tuple[int, ...]:
        if events is None:
            events = self._events()
        unexpected: set[int] = set()
        natural_exits: list[dict[str, object]] = []

        def record_natural_exit(
            record: object, process: object, replica: int, returncode: int
        ) -> None:
            if not getattr(self, "_persist_exit_diagnostics", False):
                return
            name = getattr(record, "name", None)
            pid = getattr(record, "pid", getattr(process, "pid", None))
            pgid = getattr(record, "pgid", None)
            if (
                not isinstance(name, str)
                or not name
                or type(pid) is not int
                or pid <= 0
                or type(pgid) is not int
                or pgid <= 0
                or type(returncode) is not int
            ):
                # Lightweight source-blind fixtures intentionally omit OS
                # identity fields. Production ProcessRecord values are
                # complete; an incomplete record remains fatal to health but
                # cannot truthfully produce an OS-identity diagnostic.
                return
            natural_exits.append(
                {
                    "process_identity": f"{name}:{replica}:{pid}:{pgid}",
                    "name": name,
                    "replica_id": replica,
                    "pid": pid,
                    "pgid": pgid,
                    "returncode": returncode,
                    "detected_monotonic_raw_ns": (
                        profiled_fault_runtime.monotonic_raw_ns()
                    ),
                }
            )

        for record in self._process_records:
            replica = getattr(record, "replica_id", None)
            process = getattr(record, "process", None)
            if type(replica) is not int or process is None:
                continue
            returncode = process.poll()
            if returncode is None:
                continue
            if replica == -1:
                if self._manager_clean_exit_is_expected(events, record):
                    continue
                # The aggregate replay reads the manager stream before the
                # replica streams.  The manager can finish its acknowledged
                # drain while that replay is still parsing the remaining
                # sources, leaving this otherwise authenticated snapshot short
                # of the terminal drain or final process lifecycle.  Refresh
                # once at the exact rc=0 manager boundary and validate the
                # entire raw graph again.  Nonzero exits and malformed
                # refreshed evidence remain fatal.
                if returncode == 0:
                    refreshed = self._events()
                    self._reject_post_fault_target_events(refreshed)
                    if self._manager_clean_exit_is_expected(refreshed, record):
                        unexpected.update(self.unexpected_exit_ids(refreshed))
                        continue
                unexpected.add(replica)
                record_natural_exit(record, process, replica, returncode)
                continue
            if replica < 0 or replica not in self._profile.target_replica_ids:
                unexpected.add(replica)
                record_natural_exit(record, process, replica, returncode)
                continue
            receipt_path = self._root / "raw" / "fault-receipt.json"
            exempt = False
            if receipt_path.is_file():
                receipt = self._fault_receipt()
                outcomes = [
                    _document(item, "fault outcome")
                    for item in receipt["sigkill_outcomes"]
                    if _document(item, "fault outcome").get("replica_id") == replica
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
                record_natural_exit(record, process, replica, returncode)
        self._persist_natural_process_exits(natural_exits)
        for event in events:
            if event["event_type"] != "process.exited":
                continue
            source_id = str(event["source_id"])
            if event["source_kind"] == "adaptation_manager":
                unexpected.add(-1)
            elif event["source_kind"] == "client":
                unexpected.add(-2)
            elif source_id.startswith("replica-"):
                replica = int(source_id.removeprefix("replica-"))
                receipt_path = self._root / "raw" / "fault-receipt.json"
                exempt = False
                if (
                    replica in self._profile.target_replica_ids
                    and receipt_path.is_file()
                ):
                    receipt = self._fault_receipt()
                    matching = [
                        _document(item, "fault outcome")
                        for item in receipt["sigkill_outcomes"]
                        if _document(item, "fault outcome").get("replica_id") == replica
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

    def _prefault_unexpected_exit_ids(self) -> tuple[int, ...]:
        """Fail closed on any live process exit before the injected fault."""

        return tuple(
            sorted(
                replica
                for record in self._process_records
                if type(replica := getattr(record, "replica_id", None)) is int
                and (process := getattr(record, "process", None)) is not None
                and process.poll() is not None
            )
        )

    def _reject_post_fault_target_events(
        self, events: Sequence[Mapping[str, Any]]
    ) -> None:
        if not (self._root / "raw" / "fault-receipt.json").is_file():
            return
        receipt = self._fault_receipt()
        confirmations = {
            _integer(row.get("replica_id"), "fault replica"): _integer(
                row.get("confirmed_monotonic_ns"), "fault confirmation"
            )
            for row in (
                _document(item, "fault outcome") for item in receipt["sigkill_outcomes"]
            )
        }
        for event in events:
            source_id = str(event["source_id"])
            if event["source_kind"] != "replica" or not source_id.startswith(
                "replica-"
            ):
                continue
            replica = int(source_id.removeprefix("replica-"))
            if (
                replica in confirmations
                and int(event["source_monotonic_ns"]) > confirmations[replica]
            ):
                _error("crashed replica emitted raw evidence after confirmed SIGKILL")

    @staticmethod
    def _snapshot_predecessors(
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[int, ...]:
        predecessors: list[int] = []
        for event in events:
            if event["event_type"] != "adaptive_v2_evidence_snapshot":
                continue
            if (
                event["source_kind"] != "adaptation_manager"
                or event["source_id"] != "adaptive-manager"
            ):
                _error("raw ranking audit source identity or kind drifted")
            predecessor = _integer(
                _document(event["payload"], "raw ranking audit").get(
                    "predecessor_epoch_number"
                ),
                "ranking predecessor epoch",
                0,
            )
            if predecessor not in {0, 1}:
                _error("raw ranking audit predecessor drifted")
            predecessors.append(predecessor)
        return tuple(predecessors)

    def poll(self, name: str) -> Mapping[str, object] | None:
        # Once a full native replay has proved the immutable baseline predicate,
        # keep sampling only the live per-replica configuration tails.  Replaying
        # the aggregate stream here can otherwise miss a short all-member
        # configuration span while the manager's ingress continues to fill.
        cached_baseline = getattr(self, "_baseline_stable_result", None)
        if name == "baseline" and cached_baseline is not None:
            unexpected = self._prefault_unexpected_exit_ids()
            if unexpected:
                _error("raw process health contains an unexpected process exit")
            barrier = self.latch_prefault_active_configuration_barrier()
            if barrier is None or not has_exact_active_configuration_barrier(
                self._profile, barrier
            ):
                return None
            return {
                **cached_baseline,
                "active_configuration_barrier": barrier,
            }
        events = self._events()
        self._reject_post_fault_target_events(events)
        unexpected = self.unexpected_exit_ids(events)
        if unexpected:
            transition_phase = {
                "commands1": (1, False),
                "activations1": (1, True),
                "commands2": (2, False),
                "activations2": (2, True),
            }.get(name)
            if transition_phase is not None and unexpected == (-1,):
                epoch, activation = transition_phase
                if self._manager_successfully_stopped(events):
                    missing = self._transition_missing_survivor_ids(
                        events, epoch, activation=activation
                    )
                    if missing:
                        raise FocusedIncompleteTransitionError(
                            name,
                            epoch,
                            activation,
                            missing,
                            live_raw_event_stream_sha256=_sha256(
                                _canonical_json(events)
                            ),
                            live_source_cutoffs=_raw_event_source_cutoffs(events),
                        )
            _error("raw process health contains an unexpected process exit")
        expected_lifecycle = {
            **{
                f"replica-{replica}": "replica" for replica in self._profile.replica_ids
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
                if (
                    allowed_lifecycle.get(str(event["source_id"]))
                    != event["source_kind"]
                ):
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
            last_commit_ns = max(int(event["source_monotonic_ns"]) for event in commits)
            if last_commit_ns - first_commit_ns < stable_duration_ns:
                return None
            result: dict[str, object] = {
                "stable": True,
                "authoritative_commit_count": len(commits),
                "source_monotonic_ns": last_commit_ns,
            }
            if self._profile.raw.get("schema_version") == 2:
                self._baseline_stable_result = dict(result)
                barrier = self.latch_prefault_active_configuration_barrier()
                if barrier is None or not has_exact_active_configuration_barrier(
                    self._profile, barrier
                ):
                    return None
                result["active_configuration_barrier"] = barrier
            return result
        if name == "fault":
            return self.atomic_fault_outcome()
        if name == "nonresponse":
            fault = self.atomic_fault_outcome()
            snapshot_predecessors = self._snapshot_predecessors(events)
            if 0 not in snapshot_predecessors:
                return None
            ranking = self._ranking(events, predecessor_epoch=0)
            if self._profile.raw.get("schema_version") == 2:
                qualified = self._qualifying_timeout_counts(
                    events,
                    fault_ns=_integer(
                        fault.get("source_monotonic_ns"), "fault timestamp"
                    ),
                    baseline_cutoff=_integer(
                        ranking.get("baseline_evidence_cutoff"),
                        "ranking baseline cutoff",
                    ),
                    current_cutoff=_integer(
                        ranking.get("current_evidence_cutoff"),
                        "ranking current cutoff",
                        1,
                    ),
                    candidate_targets=(
                        tuple(ranking["detected_target_ids"])
                        if _is_v9_profile(self._profile)
                        else None
                    ),
                )
                if qualified is None:
                    return None
                counts, drawdowns, timestamp = qualified
                result: dict[str, object] = {
                    "detected_target_ids": ranking["detected_target_ids"],
                    "qualifying_timeout_counts": counts,
                    "guard_drawdowns": drawdowns,
                    "source_monotonic_ns": timestamp,
                    "snapshot_audit_monotonic_ns": _integer(
                        ranking.get("audit_source_monotonic_ns"),
                        "ranking audit timestamp",
                    ),
                }
                progress = self._postfault_authoritative_progress(
                    events,
                    fault_ns=_integer(
                        fault.get("source_monotonic_ns"), "fault timestamp"
                    ),
                    prefault_ns=_integer(
                        fault.get("pre_signal_monotonic_ns"),
                        "fault pre-signal timestamp",
                    ),
                    audit_ns=_integer(
                        ranking.get("audit_source_monotonic_ns"),
                        "ranking audit timestamp",
                    ),
                )
                if (
                    derive_reporter_coverage_plan(self._profile).get(
                        "required_postfault_tree_positions"
                    )
                    is not None
                ):
                    if progress is None:
                        return None
                    result["postfault_progress"] = progress
                return result
            timeout_events = [
                event
                for event in events
                if event["event_type"] == "evidence.observation_accepted"
                and _document(
                    _document(event["payload"], "accepted evidence").get("observation"),
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
            targets = set(ranking["detected_target_ids"])
            target_timeouts = [
                event
                for event in timeout_events
                if _document(
                    _document(event["payload"], "accepted evidence").get("observation"),
                    "accepted observation",
                ).get("observed_replica_id")
                in targets
            ]
            if {
                _document(
                    _document(event["payload"], "accepted evidence").get("observation"),
                    "accepted observation",
                ).get("observed_replica_id")
                for event in target_timeouts
            } != targets:
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
            if (
                epoch1.evidence_snapshot_id != request["replayed_snapshot_id"]
                or epoch1.evidence_cutoff != request["current_evidence_cutoff"]
            ):
                _error("Epoch 1 bundle is not bound to its native snapshot audit")
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
            if commit is None:
                return None
            if _is_v5_profile(self._profile):
                activation = self._transition(events, 1, activation=True)
                if activation is None:
                    return None
                width_ns, stabilization_ns, _control_hold_ns = _v5_phase_parameters(
                    self._profile
                )
                phase_duration_ns = _phase_measurement_duration_ns(
                    self._profile, width_ns
                )
                activation_ns = _integer(
                    activation.get("source_monotonic_ns"),
                    "Epoch-1 activation timestamp",
                )
                common_ns = _runtime_first_common_commit_anchor(
                    events,
                    self._profile,
                    epoch_number=1,
                    after_ns=activation_ns,
                )
                start_ns = max(activation_ns, common_ns) + stabilization_ns
                end_ns = start_ns + phase_duration_ns
                authoritative = _runtime_authoritative_commits(events, self._profile)
                if (
                    not authoritative
                    or max(
                        _integer(
                            event.get("source_monotonic_ns"),
                            "authoritative commit timestamp",
                        )
                        for event in authoritative
                    )
                    < end_ns
                    or not _runtime_phase_has_transactions(
                        authoritative,
                        start_ns=start_ns,
                        end_ns=end_ns,
                        epoch_number=1,
                    )
                ):
                    return None
                return {
                    "stable": True,
                    "epoch_number": 1,
                    "source_monotonic_ns": end_ns,
                }
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
                or max(int(event["source_monotonic_ns"]) for event in epoch1_commits)
                - min(int(event["source_monotonic_ns"]) for event in epoch1_commits)
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
            if 1 not in self._snapshot_predecessors(events):
                return None
            return self._ranking(events, predecessor_epoch=1)
        if name == "epoch2":
            if self._arm != "A":
                return None
            wire, decoded = self._bundle(2)
            request = self._ranking(events, predecessor_epoch=1)
            if (
                decoded.evidence_snapshot_id != request["replayed_snapshot_id"]
                or decoded.evidence_cutoff != request["current_evidence_cutoff"]
            ):
                _error("Epoch 2 bundle is not bound to its native snapshot audit")
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
            return self._common_commit(events, 2)
        if name == "late":
            final = self._common_commit(events, 2 if self._arm == "A" else 1)
            if final is None:
                return None
            if _is_v5_profile(self._profile):
                final_epoch = 2 if self._arm == "A" else 1
                activation = self._transition(events, final_epoch, activation=True)
                if activation is None:
                    return None
                width_ns, stabilization_ns, control_hold_ns = _v5_phase_parameters(
                    self._profile
                )
                phase_duration_ns = _phase_measurement_duration_ns(
                    self._profile, width_ns
                )
                activation_ns = _integer(
                    activation.get("source_monotonic_ns"),
                    "final activation timestamp",
                )
                common_ns = _runtime_first_common_commit_anchor(
                    events,
                    self._profile,
                    epoch_number=final_epoch,
                    after_ns=activation_ns,
                )
                start_ns = max(activation_ns, common_ns) + stabilization_ns
                if self._arm == "C":
                    start_ns += phase_duration_ns + control_hold_ns
                end_ns = start_ns + phase_duration_ns
                authoritative = _runtime_authoritative_commits(events, self._profile)
                if (
                    not authoritative
                    or max(
                        _integer(
                            event.get("source_monotonic_ns"),
                            "authoritative commit timestamp",
                        )
                        for event in authoritative
                    )
                    < end_ns
                    or not _runtime_phase_has_transactions(
                        authoritative,
                        start_ns=start_ns,
                        end_ns=end_ns,
                        epoch_number=final_epoch,
                    )
                ):
                    return None
                return {
                    "stable": True,
                    "held_epoch_number": final_epoch,
                    "epoch2_present": self._arm == "A",
                    "source_monotonic_ns": end_ns,
                }
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
                or max(int(event["source_monotonic_ns"]) for event in final_commits)
                - min(int(event["source_monotonic_ns"]) for event in final_commits)
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


def _v5_phase_parameters(profile: FocusedProfile) -> tuple[int, int, int]:
    if not _is_v5_profile(profile):
        _error("causal phase parameters require a v5 profile")
    measurement = _document(profile.raw.get("measurement"), "profile measurement")
    phase_contract = _document(
        measurement.get("phase_window_contract"), "phase-window contract"
    )
    return (
        _integer(measurement.get("bucket_width_seconds"), "bucket width", 1)
        * 1_000_000_000,
        _integer(
            phase_contract.get("stabilization_offset_seconds"),
            "phase stabilization offset",
            1,
        )
        * 1_000_000_000,
        _integer(
            phase_contract.get("control_optimization_hold_seconds"),
            "control optimization hold",
            1,
        )
        * 1_000_000_000,
    )


def _phase_measurement_duration_ns(
    profile: FocusedProfile, bucket_width_ns: int
) -> int:
    """Use the frozen 30-second stable phase only for v9 scientific support."""

    if not _is_v9_profile(profile):
        return bucket_width_ns
    stable_ns = (
        _integer(
            _document(profile.raw.get("timers"), "profile timers").get(
                "stable_phase_seconds"
            ),
            "stable phase",
            1,
        )
        * 1_000_000_000
    )
    if stable_ns % bucket_width_ns != 0:
        _error("v9 stable phase is not an exact bucket multiple")
    return stable_ns


def _runtime_commit_epoch(event: Mapping[str, Any], label: str) -> int:
    payload = _document(event.get("payload"), label)
    proof = _document(payload.get("decision_proof"), f"{label} proof")
    return _integer(proof.get("epoch_number"), f"{label} epoch")


def _runtime_authoritative_commits(
    events: Sequence[Mapping[str, Any]], profile: FocusedProfile
) -> list[Mapping[str, Any]]:
    observer = _integer(
        _document(profile.raw.get("measurement"), "profile measurement").get(
            "authoritative_replica_id"
        ),
        "authoritative replica",
    )
    exact = [
        event
        for event in events
        if event.get("event_type") == "block.committed"
        and event.get("source_kind") == "replica"
        and event.get("source_id") == f"replica-{observer}"
    ]
    if not _is_v13_profile(profile):
        return exact

    source_id = f"replica-{observer}"
    physical_keys = {
        "block_height",
        "block_hash",
        "parent_hash",
        "transaction_count",
        "commit_batch_index",
    }
    exact_keys = physical_keys | {
        "designated_observer",
        "decision_proof",
        "view_generation",
    }
    witness_keys = physical_keys | {"decision_proof", "view_generation"}
    observations = [
        event
        for event in events
        if event.get("event_type") == "block.commit_observed"
        and event.get("source_kind") == "replica"
    ]
    observed_payloads: dict[int, Mapping[str, Any]] = {}
    observed_by_source: dict[str, list[Mapping[str, Any]]] = {}
    for observation in observations:
        payload = _document(
            observation.get("payload"), "authoritative commit observation"
        )
        if set(payload) != physical_keys:
            _error("authoritative commit observation schema drifted")
        observed_payloads[id(observation)] = payload
        observed_by_source.setdefault(str(observation.get("source_id")), []).append(
            observation
        )

    carriers = [
        event
        for event in events
        if event.get("event_type")
        in {"block.committed", "block.commit_identity_witness"}
        and event.get("source_kind") == "replica"
    ]
    carrier_rows: list[
        tuple[Mapping[str, Any], Mapping[str, Any], int]
    ] = []
    for carrier in carriers:
        carrier_payload = _document(
            carrier.get("payload"), "authoritative commit identity carrier"
        )
        if carrier.get("event_type") == "block.committed":
            if (
                set(carrier_payload) != exact_keys
                or carrier.get("source_id") != source_id
                or carrier_payload.get("designated_observer") is not True
            ):
                _error("authoritative commit identity carrier schema drifted")
        elif (
            set(carrier_payload) != witness_keys
            or carrier.get("source_id") == source_id
        ):
            _error("authoritative commit identity witness schema drifted")
        local_matches = [
            observation
            for observation in observed_by_source.get(
                str(carrier.get("source_id")), ()
            )
            if all(
                observed_payloads[id(observation)].get(key)
                == carrier_payload.get(key)
                for key in physical_keys
            )
        ]
        if len(local_matches) != 1:
            _error("authoritative commit identity carrier lacks one local observation")
        local_observation = local_matches[0]
        if (
            _integer(
                carrier.get("source_sequence"), "commit identity sequence", 1
            )
            != _integer(
                local_observation.get("source_sequence"),
                "commit observation sequence",
                1,
            )
            + 1
            or _integer(
                carrier.get("source_monotonic_ns"), "commit identity timestamp", 1
            )
            < _integer(
                local_observation.get("source_monotonic_ns"),
                "commit observation timestamp",
                1,
            )
        ):
            _error("authoritative commit identity does not follow its observation")
        proof = _document(
            carrier_payload.get("decision_proof"), "commit identity proof"
        )
        if set(proof) != {"epoch_number", "tree_id", "epoch_digest", "block_hash"}:
            _error("authoritative commit identity proof schema drifted")
        generation = _uint64(
            carrier_payload.get("view_generation"), "commit identity generation", 1
        )
        carrier_rows.append((carrier_payload, proof, generation))

    reconstructed: list[Mapping[str, Any]] = []
    for observation in observed_by_source.get(source_id, ()):
        observed_payload = observed_payloads[id(observation)]
        matches = [
            row
            for row in carrier_rows
            if all(
                row[0].get(key) == observed_payload.get(key)
                for key in (
                    "block_height",
                    "block_hash",
                    "parent_hash",
                    "transaction_count",
                )
            )
        ]
        identities = {(_canonical_json(row[1]), row[2]) for row in matches}
        if not matches or len(identities) != 1:
            _error("authoritative commit observation lacks one exact identity")
        proof = matches[0][1]
        generation = matches[0][2]
        reconstructed.append(
            {
                **dict(observation),
                "event_type": "block.committed",
                "payload": {
                    **dict(observed_payload),
                    "designated_observer": True,
                    "decision_proof": dict(proof),
                    "view_generation": generation,
                },
            }
        )
    return reconstructed


def _runtime_first_common_commit_anchor(
    events: Sequence[Mapping[str, Any]],
    profile: FocusedProfile,
    *,
    epoch_number: int,
    after_ns: int,
) -> int:
    commits = _runtime_authoritative_commits(events, profile)
    survivors = {
        f"replica-{replica}"
        for replica in profile.replica_ids
        if replica not in profile.target_replica_ids
    }
    observations = [
        event
        for event in events
        if event.get("event_type") == "block.commit_observed"
        and event.get("source_kind") == "replica"
    ]
    candidates = sorted(
        (
            event
            for event in commits
            if _runtime_commit_epoch(event, "phase commit") == epoch_number
            and _integer(event.get("source_monotonic_ns"), "phase commit timestamp")
            > after_ns
        ),
        key=lambda event: (
            _integer(event.get("source_monotonic_ns"), "phase commit timestamp"),
            _integer(
                _document(event.get("payload"), "phase commit").get("block_height"),
                "phase commit height",
                1,
            ),
        ),
    )
    for commit in candidates:
        payload = _document(commit.get("payload"), "phase commit")
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
        earliest_by_source: dict[str, int] = {}
        for observation in observations:
            source = str(observation.get("source_id"))
            if (
                source not in survivors
                or dict(
                    _document(observation.get("payload"), "phase commit observation")
                )
                != identity
            ):
                continue
            timestamp = _integer(
                observation.get("source_monotonic_ns"),
                "phase commit observation timestamp",
            )
            if timestamp <= after_ns:
                continue
            previous = earliest_by_source.get(source)
            if previous is None or timestamp < previous:
                earliest_by_source[source] = timestamp
        if len(earliest_by_source) < profile.quorum:
            continue
        quorum_times = sorted(earliest_by_source.values())[: profile.quorum]
        return max(
            _integer(commit.get("source_monotonic_ns"), "phase commit timestamp"),
            max(quorum_times),
        )
    _error("causal phase lacks a post-activation common commit")


def _runtime_phase_has_transactions(
    commits: Sequence[Mapping[str, Any]],
    *,
    start_ns: int,
    end_ns: int,
    epoch_number: int,
    require_positive: bool = True,
) -> bool:
    selected = [
        event
        for event in commits
        if start_ns
        <= _integer(event.get("source_monotonic_ns"), "phase commit timestamp")
        < end_ns
    ]
    if any(
        _runtime_commit_epoch(event, "phase commit") != epoch_number
        for event in selected
    ):
        return False
    if not require_positive:
        return True
    return (
        bool(selected)
        and sum(
            _integer(
                _document(event.get("payload"), "phase commit").get(
                    "transaction_count"
                ),
                "phase transactions",
            )
            for event in selected
        )
        > 0
    )


def _v5_phase_window_document(
    profile: FocusedProfile,
    arm: str,
    events: Sequence[Mapping[str, Any]],
    fault_receipt: Mapping[str, object],
    source: FocusedRawEvidenceSource,
) -> dict[str, object]:
    width_ns, stabilization_ns, control_hold_ns = _v5_phase_parameters(profile)
    phase_duration_ns = _phase_measurement_duration_ns(profile, width_ns)
    source._common_commit(events, 1)
    if arm == "adaptive":
        source._common_commit(events, 2)
    outcomes = _sequence(fault_receipt.get("sigkill_outcomes"), "SIGKILL outcomes")
    if not outcomes:
        _error("causal phase windows lack fault outcomes")
    prefault_ns = min(
        _integer(
            _document(outcome, "SIGKILL outcome").get("requested_monotonic_ns"),
            "fault request timestamp",
            1,
        )
        for outcome in outcomes
    )
    fault_ns = max(
        _integer(
            _document(outcome, "SIGKILL outcome").get("confirmed_monotonic_ns"),
            "fault confirmation timestamp",
            1,
        )
        for outcome in outcomes
    )
    commits = _runtime_authoritative_commits(events, profile)
    epoch0 = [
        event for event in commits if _runtime_commit_epoch(event, "phase commit") == 0
    ]
    stable_ns = (
        _integer(
            _document(profile.raw.get("timers"), "profile timers").get(
                "stable_phase_seconds"
            ),
            "stable phase",
            1,
        )
        * 1_000_000_000
    )
    if (
        not epoch0
        or min(
            _integer(event.get("source_monotonic_ns"), "Epoch-0 commit timestamp")
            for event in epoch0
        )
        > prefault_ns - stable_ns
    ):
        _error("causal baseline is not inside the proven stable interval")
    command1_times = [
        _integer(event.get("source_monotonic_ns"), "Epoch-1 command timestamp")
        for event in events
        if event.get("event_type") == "epoch.command_committed"
        and _document(event.get("payload"), "Epoch-1 command").get(
            "successor_epoch_number"
        )
        == 1
    ]
    if not command1_times or fault_ns + phase_duration_ns >= min(command1_times):
        _error("causal fault window overlaps the Epoch-1 transition")
    activation1 = source._transition(events, 1, activation=True)
    if activation1 is None:
        _error("causal Epoch-1 phase lacks every survivor activation")
    activation1_ns = _integer(
        activation1.get("source_monotonic_ns"), "Epoch-1 activation timestamp"
    )
    common1_ns = _runtime_first_common_commit_anchor(
        events, profile, epoch_number=1, after_ns=activation1_ns
    )
    epoch1_start = max(activation1_ns, common1_ns) + stabilization_ns
    epoch1_end = epoch1_start + phase_duration_ns
    windows: list[tuple[str, int, int, int]] = [
        ("baseline", prefault_ns - phase_duration_ns, prefault_ns, 0),
        ("fault", fault_ns, fault_ns + phase_duration_ns, 0),
        ("epoch1", epoch1_start, epoch1_end, 1),
    ]
    if arm == "adaptive":
        command2_times = [
            _integer(event.get("source_monotonic_ns"), "Epoch-2 command timestamp")
            for event in events
            if event.get("event_type") == "epoch.command_committed"
            and _document(event.get("payload"), "Epoch-2 command").get(
                "successor_epoch_number"
            )
            == 2
        ]
        if not command2_times or epoch1_end >= min(command2_times):
            _error("causal Epoch-1 window overlaps the Epoch-2 transition")
        activation2 = source._transition(events, 2, activation=True)
        if activation2 is None:
            _error("causal late phase lacks every survivor Epoch-2 activation")
        activation2_ns = _integer(
            activation2.get("source_monotonic_ns"), "Epoch-2 activation timestamp"
        )
        common2_ns = _runtime_first_common_commit_anchor(
            events, profile, epoch_number=2, after_ns=activation2_ns
        )
        late_start = max(activation2_ns, common2_ns) + stabilization_ns
        late_epoch = 2
    elif arm == "control":
        late_start = epoch1_end + control_hold_ns
        late_epoch = 1
    else:
        _error("causal phase window arm is invalid")
    windows.append(("late", late_start, late_start + phase_duration_ns, late_epoch))
    if any(right[1] < left[2] for left, right in zip(windows, windows[1:])):
        _error("causal phase windows overlap")
    if any(
        not _runtime_phase_has_transactions(
            commits,
            start_ns=start,
            end_ns=end,
            epoch_number=epoch,
            require_positive=phase != "fault",
        )
        for phase, start, end, epoch in windows
    ):
        _error("causal phase transactions or exact epoch drifted")
    return {
        "schema_version": 1,
        "domain": "kauri-focused-causal-phase-windows-v1",
        "phases": [
            {
                "phase": phase,
                "start_ns": start,
                "end_ns": end,
                "epoch_number": epoch,
            }
            for phase, start, end, epoch in windows
        ],
    }


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
        if (
            required.value <= ctypes.sizeof(ctypes.c_int)
            or required.value > _MAX_ARGV_BYTES
        ):
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
        if isinstance(observed, Sequence) and not isinstance(
            observed, (str, bytearray)
        ):
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
        "--protocol-mode",
        "--activation-readiness-release-count",
        "--activation-readiness-maximum-delivery-attempts",
        "--activation-readiness-retry-interval-ticks",
        "--activation-readiness-member",
        "--transition-request",
        "--bundle-output",
        "--structured-event-run-id",
        "--structured-event-source-instance",
        "--structured-event-output",
        "--fault-window-arm-path",
        "--fault-window-arm-schema-version",
        "--fault-window-arm-domain",
        "--fault-window-arm-run-id",
        "--fault-window-arm-profile-id",
        "--fault-window-arm-profile-sha256",
        "--fault-window-arm-topology-proof-sha256",
        "--fault-window-arm-request-sha256",
        "--fault-window-arm-epoch-number",
        "--fault-window-arm-epoch-digest",
        "--fault-window-arm-prefault-tree-id",
        "--fault-window-arm-required-tree-positions",
        "--fault-window-arm-deadline-seconds",
        "--fault-window-arm-clock-domain",
        "--fault-window-arm-required-observation-schema",
        "--fault-window-arm-timeout-evidence-basis",
        "--fault-window-arm-snapshot-evidence-basis",
        "--fault-window-arm-selection-cardinality-policy",
        "--replica",
    }
    if len(requested) % 2 == 0:
        _error("manager argv must contain an executable and option/value pairs")
    pairs = tuple(zip(requested[1::2], requested[2::2], strict=True))
    if any(option not in allowed for option, _value in pairs):
        _error("manager argv contains an unreviewed option")
    v3_options = {
        "--protocol-mode",
        "--activation-readiness-release-count",
        "--activation-readiness-maximum-delivery-attempts",
        "--activation-readiness-retry-interval-ticks",
        "--activation-readiness-member",
    }
    if any(option in v3_options for option, _value in pairs):
        values = {
            option: [value for current, value in pairs if current == option]
            for option in v3_options
        }
        if (
            values["--protocol-mode"] != ["adaptive_v3"]
            or len(values["--activation-readiness-release-count"]) != 1
            or values["--activation-readiness-maximum-delivery-attempts"]
            != [str(_V13_MAXIMUM_OBSERVATION_ATTEMPTS)]
            or values["--activation-readiness-retry-interval-ticks"]
            != [str(_V13_OBSERVATION_RETRY_INTERVAL_MS * 1_000_000)]
            or not values["--activation-readiness-member"]
        ):
            _error("manager adaptive-v3 launch boundary is incomplete")
        readiness_members: list[tuple[int, str]] = []
        for value in values["--activation-readiness-member"]:
            raw_replica, separator, public_key = value.partition(",")
            if (
                separator != ","
                or not raw_replica.isascii()
                or not raw_replica.isdecimal()
                or str(int(raw_replica)) != raw_replica
                or len(public_key) != 96
                or any(character not in "0123456789abcdef" for character in public_key)
            ):
                _error("manager adaptive-v3 readiness member is not canonical")
            readiness_members.append((int(raw_replica), public_key))
        release_raw = values["--activation-readiness-release-count"][0]
        if (
            not release_raw.isascii()
            or not release_raw.isdecimal()
            or str(int(release_raw)) != release_raw
            or [replica for replica, _public in readiness_members]
            != list(range(len(readiness_members)))
            or len({public for _replica, public in readiness_members})
            != len(readiness_members)
            or not 0 < int(release_raw) <= len(readiness_members)
        ):
            _error("manager adaptive-v3 readiness boundary is invalid")
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
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
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
    if raw.get("schema_version") == 2:
        coverage = derive_reporter_coverage_plan(profile)
        deadline = _document(coverage.get("deadlines_seconds"), "coverage deadlines")
        tree_switch_period = _integer(
            _document(raw.get("evidence_guard"), "profile evidence guard").get(
                "tree_switch_period_blocks"
            ),
            "tree switch period",
            1,
        )
        startup_timeout = _integer(
            timers.get("readiness_timeout_seconds"), "readiness timeout", 1
        )
        maximum_stall = _integer(
            timers.get("manager_convergence_timeout_seconds"),
            "manager convergence timeout",
            1,
        )
        hard_timeout = _integer(
            deadline.get("arm_hard_seconds"), "arm hard deadline", 1
        )
    else:
        tree_switch_period = count
        maximum_stall = float(timers.get("containment_deadline_seconds", 60))
        startup_timeout = float(timers.get("containment_deadline_seconds", 60))
        hard_timeout = float(timers.get("optimization_deadline_seconds", 90)) + 60.0
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
        tree_switch_period_blocks=tree_switch_period,
        bucket_width_s=_integer(
            measurement.get("bucket_width_seconds"), "bucket width", 1
        ),
        baseline_bucket_count=1,
        post_bucket_count=3,
        minimum_positive_postfault_buckets=3,
        minimum_mean_throughput_retention=0.0,
        aggregation_timeout_s=1.0,
        # Both frozen topologies have a two-edge deepest tree, whose native
        # get_max_level() value is the three-level count. The fallback horizon
        # is therefore 2 * (3 + 1) * 1s = 8s. Suspicion must remain strictly
        # after it once the 1s activation grace is included. Keeping this
        # bounded value also makes the frozen N31 31-tree/420s coverage
        # contract executable; a 20s per-tree timeout cannot satisfy it.
        leader_progress_timeout_s=_FOCUSED_LEADER_PROGRESS_TIMEOUT_S,
        leader_activation_grace_s=1.0,
        activation_delay_blocks=5,
        maximum_stall_s=float(maximum_stall),
        startup_timeout_s=float(startup_timeout),
        hard_timeout_s=float(hard_timeout),
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


def _generate_v13_arm_tls_identities(
    profile: FrozenProfile,
    *,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    config_directory: Path,
) -> list[dict[str, str]]:
    """Generate only TLS material; v13 BLS identities are preauthorized."""

    command = profiled_fault_runtime.identity_generation_commands(
        profile,
        keygen_binary=keygen_binary,
        tls_keygen_binary=tls_keygen_binary,
    )["tls"]
    result = subprocess.run(
        command,
        cwd=config_directory,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stderr:
        _error("v13 TLS arm identity generation failed")
    profiled_fault_runtime.write_exclusive(
        config_directory / "tls-identities.txt", result.stdout.encode("ascii")
    )
    return profiled_fault_runtime._parse_identity_output(
        result.stdout,
        expected_count=len(profile.replica_ids) + 1,
        expected_fields=frozenset({"crt", "sec", "cid"}),
        label="v13 TLS keygen",
    )


def _focused_transition_requests(
    run_directory: Path, arm: str, profile: FocusedProfile
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
        _anchor_margin_ns, residence_ms = _v10_transition_timing(profile)
        specifications.append(
            (
                "e1-to-e2-optimization",
                "performance_optimization",
                True,
                1,
                2,
                residence_ms,
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


def _focused_client_default_epoch(
    profile: FocusedProfile, adapter: FrozenProfile
) -> bytes:
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


def _v13_replica_argvs(
    profile: FocusedProfile,
    adapter: FrozenProfile,
    *,
    base_commands: Sequence[Sequence[str]],
    readiness: Mapping[str, object],
    tls: Sequence[Mapping[str, str]],
    issuer: Mapping[str, str],
    run_directory: Path,
    run_id: str,
    source_instances: Mapping[str, str],
) -> tuple[tuple[str, ...], ...]:
    """Construct the exact P3 replica argv without persisting readiness secrets."""

    _v13_certified_activation_contract(profile)
    members = readiness.get("members")
    private_rows = readiness.get("private_allocations")
    if (
        not isinstance(members, list)
        or not isinstance(private_rows, list)
        or len(base_commands) != len(profile.replica_ids)
        or len(members) != len(profile.replica_ids)
        or len(private_rows) != len(profile.replica_ids)
        or len(tls) != len(profile.replica_ids) + 1
    ):
        _error("v13 replica launch cardinality drifted")
    membership_argv = tuple(
        argument
        for member in members
        for argument in (
            "--activation-readiness-member",
            f"{member['replica_id']},{member['public_key_hex']}",
        )
    )
    commands: list[tuple[str, ...]] = []
    observer_id = f"replica-{_integer(profile.raw['measurement']['authoritative_replica_id'], 'v13 observer id')}"
    for position, replica in enumerate(profile.replica_ids):
        private = _document(private_rows[position], "v13 replica private allocation")
        source_id = f"replica-{replica}"
        commands.append(
            (
                *tuple(base_commands[position]),
                "--privkey",
                str(private["private_key"]),
                "--epoch-protocol-mode",
                "adaptive_v3",
                "--epoch-change-issuer-id",
                str(profiled_fault_runtime.ISSUER_ID),
                "--epoch-change-issuer-public-key",
                str(issuer["pub"]),
                "--epoch-change-minimum-activation-delay",
                str(adapter.activation_delay_blocks),
                "--epoch-change-maximum-activation-delay",
                str(adapter.activation_delay_blocks),
                "--epoch-change-maximum-block-extra-bytes",
                str(profiled_fault_runtime.MAX_COMMAND_BYTES),
                "--epoch-change-maximum-ancestry-blocks",
                str(profiled_fault_runtime.MAX_ANCESTRY_BLOCKS),
                "--epoch-manager-address",
                f"127.0.0.1:{adapter.manager_port}",
                "--epoch-manager-tls-cert",
                str(tls[len(profile.replica_ids)]["crt"]),
                *membership_argv,
                "--activation-readiness-maximum-observation-attempts",
                str(_V13_MAXIMUM_OBSERVATION_ATTEMPTS),
                "--activation-readiness-observation-retry-interval-ms",
                str(_V13_OBSERVATION_RETRY_INTERVAL_MS),
                "--structured-event-run-id",
                run_id,
                "--structured-event-source-instance",
                str(source_instances[source_id]),
                "--structured-event-output",
                str(run_directory / "raw" / f"{source_id}.jsonl"),
                "--structured-event-commit-observer-id",
                observer_id,
                "--structured-event-commit-observer-instance",
                str(source_instances[observer_id]),
            )
        )
    return tuple(commands)


def _v13_client_argv(
    *,
    client_binary: Path,
    run_directory: Path,
    maximum_async: int,
) -> tuple[str, ...]:
    return (
        str(client_binary),
        "--conf",
        str(run_directory / "config" / "main.conf"),
        "--idx",
        "0",
        "--iter",
        "-1",
        "--max-async",
        str(maximum_async),
        "--epoch-protocol-mode",
        "adaptive_v3",
    )


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
    fault_window_arm_path: Path | None = None,
    request_sha256: str | None = None,
    readiness_members: Sequence[Mapping[str, object]] | None = None,
) -> tuple[str, ...]:
    v13 = _is_v13_profile(profile)
    if v13:
        _v13_certified_activation_contract(profile)
    count = len(profile.replica_ids)
    policy = _native_responsiveness_policy(profile.profile_id)
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
    if v13:
        # The unified manager still owns the common v2 selection/arm parser;
        # only its public readiness policy is v3-specific.  Identity is
        # deliberately absent: it is derived from the committed bundle.
        retry_ns = _V13_OBSERVATION_RETRY_INTERVAL_MS * 1_000_000
        command.extend(
            (
                "--protocol-mode",
                "adaptive_v3",
                "--activation-readiness-release-count",
                str(profile.raw["transitions"]["survivor_barrier_count"]),
                "--activation-readiness-maximum-delivery-attempts",
                str(_V13_MAXIMUM_OBSERVATION_ATTEMPTS),
                "--activation-readiness-retry-interval-ticks",
                str(retry_ns),
            )
        )
        if (
            readiness_members is None
            or len(readiness_members) != len(profile.replica_ids)
        ):
            _error("v13 manager readiness members are absent or malformed")
        for replica, member in zip(profile.replica_ids, readiness_members, strict=True):
            member_id = member.get("replica_id")
            public_key = member.get("public_key_hex")
            if member_id != replica or not isinstance(public_key, str):
                _error("v13 manager readiness member identity drifted")
            command.extend(
                (
                    "--activation-readiness-member",
                    f"{replica},{public_key}",
                )
            )
    if _is_v4_profile(profile):
        if (
            fault_window_arm_path is None
            or request_sha256 is None
            or not fault_window_arm_path.is_absolute()
            or fault_window_arm_path.exists()
        ):
            _error("v4 manager requires one absent absolute fault-window arm path")
        coverage = derive_reporter_coverage_plan(profile)
        command.extend(
            (
                "--fault-window-arm-path",
                str(fault_window_arm_path),
                "--fault-window-arm-schema-version",
                (
                    "4"
                    if _is_v9_profile(profile)
                    else (
                        "3"
                        if _is_v7_profile(profile) or _is_v8_profile(profile)
                        else "2" if _is_v6_profile(profile) else "1"
                    )
                ),
                "--fault-window-arm-domain",
                (
                    _FAULT_WINDOW_ARM_DOMAIN_V4
                    if _is_v9_profile(profile)
                    else (
                        _FAULT_WINDOW_ARM_DOMAIN_V3
                        if _is_v7_profile(profile) or _is_v8_profile(profile)
                        else (
                            _FAULT_WINDOW_ARM_DOMAIN_V2
                            if _is_v6_profile(profile)
                            else _FAULT_WINDOW_ARM_DOMAIN_V1
                        )
                    )
                ),
                "--fault-window-arm-run-id",
                run_id,
                "--fault-window-arm-profile-id",
                profile.profile_id,
                "--fault-window-arm-profile-sha256",
                profile.profile_sha256,
                "--fault-window-arm-topology-proof-sha256",
                profile.topology_proof_sha256,
                "--fault-window-arm-request-sha256",
                request_sha256,
                "--fault-window-arm-epoch-number",
                "0",
                "--fault-window-arm-epoch-digest",
                str(profile.raw["topology"]["epoch_zero_digest"]),
                "--fault-window-arm-prefault-tree-id",
                str(profile.raw["topology"]["active_tree_id"]),
                "--fault-window-arm-required-tree-positions",
                str(coverage["required_postfault_tree_positions"]),
                "--fault-window-arm-deadline-seconds",
                str(
                    _document(coverage["deadlines_seconds"], "coverage deadlines")[
                        "arm_hard_seconds"
                    ]
                ),
            )
        )
        if (
            _is_v6_profile(profile)
            or _is_v7_profile(profile)
            or _is_v8_or_v9_profile(profile)
        ):
            command.extend(
                (
                    "--fault-window-arm-clock-domain",
                    "same_host_clock_monotonic_raw",
                    "--fault-window-arm-required-observation-schema",
                    "3",
                    "--fault-window-arm-timeout-evidence-basis",
                    "exact_timeout_attempt_id_v1",
                )
            )
        if _is_v7_profile(profile) or _is_v8_or_v9_profile(profile):
            command.extend(
                (
                    "--fault-window-arm-snapshot-evidence-basis",
                    "exact_post_fault_attempt_start_v1",
                )
            )
        if _is_v9_profile(profile):
            command.extend(
                (
                    "--fault-window-arm-selection-cardinality-policy",
                    "all_guarded_up_to_fault_bound_v1",
                )
            )
    for request, output in _focused_transition_requests(run_directory, arm, profile):
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
        execute_fault: (
            Callable[[Mapping[str, object], object], Mapping[str, object]] | None
        ) = None,
        cleanup_registry: Callable[[object], Sequence[object]] | None = None,
        materialize_artifacts: (
            Callable[
                [Mapping[str, object], Mapping[str, object], Mapping[str, object]], None
            ]
            | None
        ) = None,
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
        preflight = _document(invocation.get("preflight_receipt"), "preflight receipt")
        if not isinstance(profile, FocusedProfile):
            _error("live execution requires a loaded focused profile")
        execution = _document(
            preflight.get("execution_context"), "preflight execution context"
        )
        if (
            execution.get("profile_sha256") != profile.profile_sha256
            or execution.get("topology_proof_sha256") != profile.topology_proof_sha256
            or not isinstance(execution.get("issuer_public_key"), str)
        ):
            _error("live execution context is not preflight-bound")
        if profile.raw.get("schema_version") == 2 and execution.get(
            "reporter_coverage"
        ) != derive_reporter_coverage_plan(profile):
            _error("live reporter coverage differs from preflight")
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
        if authorized_build.get("revision") != current_revision or authorized_build.get(
            "build_sha256"
        ) != _sha256(_canonical_json(build_record)):
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
            if authorized.get("path") != str(resolved) or authorized.get(
                "sha256"
            ) != profiled_fault_runtime.sha256_file(resolved):
                _error(f"authorized {name} binary identity drifted")
        loaded_allocations = invocation.get("pair_issuer_allocations")
        authorized_allocations = execution.get("pair_issuers")
        if loaded_allocations is not None or authorized_allocations is not None:
            loaded = _document(loaded_allocations, "loaded pair issuers")
            authorized = _document(authorized_allocations, "authorized pair issuers")
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
        loaded_readiness = invocation.get("pair_readiness_allocations")
        authorized_readiness = execution.get("pair_readiness_allocations")
        if _is_v13_profile(profile):
            loaded = _document(loaded_readiness, "loaded v13 readiness allocations")
            authorized = _document(
                authorized_readiness, "authorized v13 readiness allocations"
            )
            if set(loaded) != set(authorized):
                _error("authorized v13 readiness allocation cardinality drifted")
            for pair_id in authorized:
                loaded_pair = _document(
                    loaded[pair_id], f"loaded {pair_id} readiness allocation"
                )
                authorized_pair = _document(
                    authorized[pair_id], f"authorized {pair_id} readiness allocation"
                )
                if any(
                    loaded_pair.get(key) != authorized_pair.get(key)
                    for key in (
                        "manifest_path",
                        "manifest_sha256",
                        "membership_digest",
                        "members",
                    )
                ):
                    _error(f"authorized {pair_id} readiness identity drifted")
                loaded_private = loaded_pair.get("private_allocations")
                authorized_private = authorized_pair.get("private_allocations")
                if (
                    not isinstance(loaded_private, list)
                    or not isinstance(authorized_private, list)
                    or len(loaded_private) != len(authorized_private)
                    or any(
                        any(loaded_row.get(key) != authorized_row.get(key) for key in authorized_row)
                        for loaded_row, authorized_row in zip(
                            loaded_private, authorized_private, strict=True
                        )
                        if isinstance(loaded_row, Mapping)
                        and isinstance(authorized_row, Mapping)
                    )
                    or any(
                        not isinstance(row, Mapping)
                        for row in (*loaded_private, *authorized_private)
                    )
                ):
                    _error(f"authorized {pair_id} readiness private identity drifted")
        elif loaded_readiness is not None or authorized_readiness is not None:
            _error("v13 readiness allocations cannot enter an archived launch")
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
        readiness = (
            _v13_launch_readiness_allocation(
                source_profile,
                pair_id=pair_id,
                allocations=_document(
                    context.get("pair_readiness_allocations"),
                    "v13 pair readiness allocations",
                ),
            )
            if _is_v13_profile(source_profile)
            else None
        )
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
        arm_allocation = _document(pair_allocation.get(arm), f"{pair_id} {arm} issuer")
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
        if readiness is None:
            bls, tls = _generate_arm_identities(
                adapter,
                keygen_binary=Path(binaries["keygen"]),
                tls_keygen_binary=Path(binaries["tls_keygen"]),
                config_directory=run_directory / "config",
            )
        else:
            tls = _generate_v13_arm_tls_identities(
                adapter,
                keygen_binary=Path(binaries["keygen"]),
                tls_keygen_binary=Path(binaries["tls_keygen"]),
                config_directory=run_directory / "config",
            )
            members = readiness["members"]
            assert isinstance(members, list)
            placeholder = "v13-private-key-is-supplied-only-in-process-argv"
            bls = [
                {"pub": str(member["public_key_hex"]), "sec": placeholder}
                for member in members
            ]
            profiled_fault_runtime.write_exclusive(
                run_directory / "config" / "bls-identities.txt",
                b"".join(
                    f"pub:{member['public_key_hex']}\n".encode("ascii")
                    for member in members
                ),
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
                epoch_protocol_mode=("adaptive_v3" if _is_v13_profile(profile) else "adaptive_v2"),
            )
        )
        if readiness is not None:
            placeholder_line = (
                "privkey = v13-private-key-is-supplied-only-in-process-argv\n"
            ).encode("ascii")
            for replica in profile.replica_ids:
                config_path = run_directory / "config" / f"replica-{replica}.conf"
                payload = config_path.read_bytes()
                if payload.count(placeholder_line) != 1:
                    _error("v13 replica private-key placeholder drifted")
                config_path.write_bytes(payload.replace(placeholder_line, b""))
            replicas = _v13_replica_argvs(
                profile,
                adapter,
                base_commands=replicas,
                readiness=readiness,
                tls=tls,
                issuer=issuer,
                run_directory=run_directory,
                run_id=run_id,
                source_instances=instances,
            )
            public_manifest_path = (
                run_directory / "runtime" / "activation-readiness-public-manifest.json"
            )
            profiled_fault_runtime.write_exclusive(
                public_manifest_path, readiness["manifest_bytes"]
            )
            artifacts = [
                {
                    **artifact,
                    "sha256": profiled_fault_runtime.sha256_file(
                        run_directory / str(artifact["path"])
                    ),
                }
                for artifact in artifacts
            ]
            artifacts.append(
                {
                    "kind": "activation_readiness_public_manifest",
                    "replica_id": None,
                    "path": str(public_manifest_path.relative_to(run_directory)),
                    "sha256": str(readiness["manifest_sha256"]),
                }
            )
        if (
            _is_v6_profile(profile)
            or _is_v7_profile(profile)
            or _is_v8_or_v9_profile(profile)
        ):
            _enable_v6_timeout_attempt_evidence(run_directory, profile.replica_ids)
            artifacts = [
                (
                    {
                        **artifact,
                        "sha256": profiled_fault_runtime.sha256_file(
                            run_directory / str(artifact["path"])
                        ),
                    }
                    if artifact["kind"] == "replica_config"
                    else artifact
                )
                for artifact in artifacts
            ]
        profiled_fault_runtime.write_exclusive(
            run_directory / "treegen.conf",
            _focused_client_default_epoch(profile, adapter),
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
        preflight = _document(
            context.get("preflight_receipt"), "parent preflight receipt"
        )
        authorization = _document(
            context.get("authorization_receipt"), "parent authorization receipt"
        )
        child_request_sha = _sha256(_canonical_json(request))
        parent_request_sha: str | None = None
        if _is_v4_profile(profile):
            parent_request = build_focused_authorization_request(preflight)
            verified_parent = verify_focused_authorization_receipt(
                parent_request, authorization
            )
            parent_request_sha = _sha256(parent_request)
            if verified_parent.get("request_sha256") != parent_request_sha:
                _error("v4 parent authorization request digest drifted")
        arm_path = (
            _fault_window_arm_path(run_directory) if _is_v4_profile(profile) else None
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
            fault_window_arm_path=arm_path,
            request_sha256=parent_request_sha,
            readiness_members=(
                readiness["members"] if readiness is not None else None
            ),
        )
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
                "request_sha256": child_request_sha,
                "execution_authorized": False,
                "launch_permitted": False,
            },
            "authorization.json": {
                **request,
                "request_sha256": child_request_sha,
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
                "manager_argv": profiled_fault_runtime.normalized_manager_argv(
                    manager
                ),
                **(
                    {
                        "manager_checkpoint": _V13_MANAGER_CHECKPOINT,
                        "replica_argv": [
                            list(
                                _v13_redacted_launch_projection(
                                    command,
                                    private_values=readiness["private_values"],
                                )
                            )
                            for command in replicas
                        ],
                    }
                    if readiness is not None
                    else {}
                ),
            },
            "derived/phase-windows.json": {"phases": []},
            "derived/throughput.json": {"rows": []},
        }
        if _is_v4_profile(profile):
            assert parent_request_sha is not None
            documents["runtime/parent-authorization-request.json"] = json.loads(
                parent_request
            )
            documents["runtime/parent-authorization-receipt.json"] = dict(authorization)
        for relative, value in documents.items():
            destination = run_directory / relative
            payload = _canonical_json(value)
            if relative.startswith("runtime/parent-authorization-"):
                profiled_fault_runtime.write_exclusive(destination, payload)
            else:
                destination.write_bytes(payload)
        (run_directory / "raw" / "issuer-public-key.txt").write_text(
            f"{issuer['pub']}\n", encoding="utf-8"
        )
        (run_directory / "raw" / "client-events.jsonl").write_bytes(b"")
        client = (
            _v13_client_argv(
                client_binary=Path(binaries["client"]),
                run_directory=run_directory,
                maximum_async=max(1, adapter.pipeline_depth * adapter.block_size),
            )
            if readiness is not None
            else (
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
        )
        if readiness is not None:
            launch_path = run_directory / "runtime" / "launch-arguments.json"
            launch = _document(json.loads(launch_path.read_bytes()), "v13 launch argv")
            launch["client_argv"] = list(client)
            launch_path.write_bytes(_canonical_json(launch))
            artifacts = [
                {
                    **artifact,
                    "sha256": profiled_fault_runtime.sha256_file(
                        run_directory / str(artifact["path"])
                    ),
                }
                if artifact["kind"] == "launch_arguments"
                else artifact
                for artifact in artifacts
            ]
            _v13_assert_private_material_excluded(
                run_directory,
                private_values=readiness["private_values"],
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
            "source_instances": instances,
            "run_directory": run_directory,
            "manager_command": manager,
            "replica_commands": replicas,
            "client_command": client,
            "issuer": issuer,
            "fault_plan": plan,
            "runtime_artifacts": artifacts,
            "fault_window_arm_path": arm_path,
            "child_request_sha256": child_request_sha,
            "parent_request_sha256": parent_request_sha,
            "manager_launch_checkpoint": (
                _V13_MANAGER_CHECKPOINT if readiness is not None else None
            ),
        }

    def spawn_processes(self, configuration: Mapping[str, object]) -> _FocusedProcesses:
        manager_command = configuration.get("manager_command")
        if isinstance(manager_command, (str, bytes)) or not isinstance(
            manager_command, Sequence
        ):
            _error("focused manager command is unavailable")
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
            ("adaptive-manager", -1, manager_command),
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
            normalized_requested = profiled_fault_runtime.normalized_manager_argv(
                requested
            )
            normalized_observed = profiled_fault_runtime.normalized_manager_argv(
                observed
            )
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
            "pre_signal_monotonic_ns": min(
                outcome.requested_monotonic_ns for outcome in outcomes
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
        *,
        deadline_monotonic: float | None = None,
    ) -> Mapping[str, object]:
        deadline = (
            time.monotonic() + self._readiness_timeout_s
            if deadline_monotonic is None
            else deadline_monotonic
        )
        while True:
            snapshot = poll(name)
            if snapshot is not None:
                return dict(_document(snapshot, f"{name} runtime snapshot"))
            if time.monotonic() >= deadline:
                _error(f"timed out waiting for {name} runtime evidence")
            if self._poll_interval_s:
                time.sleep(self._poll_interval_s)

    def _wait_for_prefault_configuration(
        self,
        source: FocusedRawEvidenceSource,
        processes: object,
        *,
        deadline_monotonic: float,
    ) -> list[dict[str, object]]:
        def require_live_processes() -> None:
            for record in getattr(processes, "records", ()):
                process = getattr(record, "process", None)
                if process is not None and process.poll() is not None:
                    _error("process exited while awaiting the pre-fault configuration")

        while True:
            require_live_processes()
            latched = source.latch_prefault_active_configuration_barrier()
            if latched is not None:
                if self._poll_interval_s:
                    time.sleep(self._poll_interval_s)
                confirmed = source.latch_prefault_active_configuration_barrier()
                if confirmed == latched:
                    require_live_processes()
                    if time.monotonic() >= deadline_monotonic:
                        _error(
                            "timed out waiting for the exact pre-fault configuration"
                        )
                    return confirmed
            if time.monotonic() >= deadline_monotonic:
                _error("timed out waiting for the exact pre-fault configuration")
            if self._poll_interval_s:
                time.sleep(self._poll_interval_s)

    def _wait_for_v4_fault_window_coverage(
        self,
        source: FocusedRawEvidenceSource,
        processes: object,
        arm_document: Mapping[str, object],
        *,
        deadline_monotonic: float,
        deadline_monotonic_raw_ns: int | None = None,
    ) -> None:
        """Wait for the live authoritative cyclic prefix before publishing v4 arm."""

        def require_live_processes() -> None:
            unexpected = source.unexpected_exit_ids(())
            if unexpected:
                _error("process exited while awaiting post-fault configuration")

        def deadline_reached() -> bool:
            return time.monotonic() >= deadline_monotonic or (
                deadline_monotonic_raw_ns is not None
                and profiled_fault_runtime.monotonic_raw_ns()
                >= deadline_monotonic_raw_ns
            )

        while True:
            require_live_processes()
            if source.postfault_authoritative_configuration_prefix_complete(
                evidence_start_monotonic_ns=_integer(
                    arm_document.get("evidence_start_monotonic_ns"),
                    "fault-window evidence start",
                    1,
                ),
                prefault_tree_id=_integer(
                    arm_document.get("prefault_tree_id"), "pre-fault tree"
                ),
                required_tree_ids=_sequence(
                    arm_document.get("required_tree_ids"), "fault-window tree IDs"
                ),
            ):
                require_live_processes()
                if deadline_reached():
                    _error("timed out waiting for post-fault configuration coverage")
                return
            if deadline_reached():
                _error("timed out waiting for post-fault configuration coverage")
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
                expected_run_id=str(configuration["run_id"]),
                expected_source_instances=_document(
                    configuration.get("source_instances"),
                    "focused source instances",
                ),
            )
            poll = source.poll
            unexpected_exits = source.unexpected_exit_ids
        else:
            poll = self._poll_snapshot
            unexpected_exits = lambda: ()
        profile = configuration.get("profile")
        coverage = (
            derive_reporter_coverage_plan(profile)
            if isinstance(profile, FocusedProfile)
            and profile.raw.get("schema_version") == 2
            else None
        )
        started_wall = time.monotonic()
        arm_hard_seconds = (
            None
            if coverage is None
            else _integer(
                _document(coverage["deadlines_seconds"], "coverage deadlines").get(
                    "arm_hard_seconds"
                ),
                "arm hard deadline",
                1,
            )
        )
        prefault_hard_deadline = (
            None if arm_hard_seconds is None else started_wall + arm_hard_seconds
        )
        readiness_deadline = (
            None
            if coverage is None
            else started_wall
            + _integer(
                coverage.get("readiness_timeout_seconds"),
                "readiness timeout",
                1,
            )
        )
        fault_wall: float | None = None
        postfault_hard_deadline: float | None = None
        epoch1_activation_wall: float | None = None
        try:
            self._wait_for_snapshot(
                "readiness", poll, deadline_monotonic=readiness_deadline
            )
        except KeyError:
            if self._poll_snapshot is None:
                raise

        def wait(name: str) -> Mapping[str, object]:
            nonlocal epoch1_activation_wall
            deadline = (
                postfault_hard_deadline
                if fault_wall is not None
                else prefault_hard_deadline
            )
            if coverage is not None and fault_wall is not None:
                if postfault_hard_deadline is None:
                    _error("post-fault hard deadline is not anchored")
                limits = _document(coverage["deadlines_seconds"], "coverage deadlines")
                if name in {"nonresponse", "epoch1"}:
                    deadline = min(
                        postfault_hard_deadline,
                        fault_wall
                        + _integer(
                            limits.get("evidence_seconds"),
                            "evidence deadline",
                            1,
                        ),
                    )
                elif name in {"commands1", "activations1"}:
                    deadline = min(
                        postfault_hard_deadline,
                        fault_wall
                        + _integer(
                            limits.get("epoch1_activation_seconds"),
                            "Epoch 1 activation deadline",
                            1,
                        ),
                    )
                elif name in {"ranking", "epoch2", "commands2", "activations2"}:
                    if epoch1_activation_wall is None:
                        _error("optimization wait lacks its Epoch 1 activation anchor")
                    deadline = min(
                        postfault_hard_deadline,
                        epoch1_activation_wall
                        + _integer(
                            limits.get("optimization_activation_seconds"),
                            "optimization activation deadline",
                            1,
                        ),
                    )
            snapshot = self._wait_for_snapshot(name, poll, deadline_monotonic=deadline)
            if name == "activations1" and coverage is not None:
                epoch1_activation_wall = time.monotonic()
            return snapshot

        def inject() -> Mapping[str, object]:
            nonlocal fault_wall, postfault_hard_deadline
            latched_barrier: list[dict[str, object]] | None = None
            if (
                self._poll_snapshot is None
                and isinstance(profile, FocusedProfile)
                and (
                    profile.profile_id in _FCRASH_H_V3_PROFILE_IDS
                    or _is_v4_profile(profile)
                )
            ):
                latch_deadline = min(
                    float(prefault_hard_deadline),
                    time.monotonic() + self._readiness_timeout_s,
                )
                latched_barrier = self._wait_for_prefault_configuration(
                    source,
                    processes,
                    deadline_monotonic=latch_deadline,
                )
            execute = self._execute_fault
            if execute is not None:
                outcome = dict(execute(configuration, processes))
            else:
                outcome = dict(
                    self.execute_atomic_fault_batch(  # type: ignore[arg-type]
                        configuration, processes
                    )
                )
            fault_wall = time.monotonic()
            if arm_hard_seconds is not None:
                postfault_hard_deadline = fault_wall + arm_hard_seconds
            observed = wait("fault")
            if dict(observed) != outcome:
                _error("observed fault receipt differs from the atomic outcome")
            if latched_barrier is not None:
                outcome["prefault_active_configuration_barrier"] = latched_barrier
            if _is_v4_profile(profile):
                root = Path(configuration["run_directory"])
                receipt = self._fault_receipts.get(str(root.resolve()))
                arm_path = configuration.get("fault_window_arm_path")
                if receipt is None or not isinstance(arm_path, Path):
                    _error("v4 fault-window arm lacks its finalized receipt or path")
                arm_document = _fault_window_arm_document(
                    configuration, receipt, latched_barrier or ()
                )
                if self._poll_snapshot is not None:
                    _error("v4 fault-window coverage requires live raw evidence")
                self._wait_for_v4_fault_window_coverage(
                    source,
                    processes,
                    arm_document,
                    deadline_monotonic=(
                        _v12_configuration_coverage_deadline(
                            arm_document,
                            coverage,
                            postfault_hard_deadline=float(postfault_hard_deadline),
                        )
                        if coverage is not None
                        and _is_v12_profile(profile)
                        and postfault_hard_deadline is not None
                        and fault_wall is not None
                        else (
                            postfault_hard_deadline
                            if postfault_hard_deadline is not None
                            else time.monotonic() + self._readiness_timeout_s
                        )
                    ),
                    deadline_monotonic_raw_ns=(
                        _v12_configuration_coverage_raw_deadline_ns(
                            arm_document, coverage
                        )
                        if coverage is not None and _is_v12_profile(profile)
                        else None
                    ),
                )
                outcome["fault_window_arm_sha256"] = _publish_fault_window_arm(
                    arm_path, arm_document
                )
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

    def materialize_abort_artifacts(
        self,
        configuration: Mapping[str, object],
        runtime_error: FocusedCrashPairRuntimeError,
    ) -> None:
        """Materialize a final barrier diagnosis only after cleanup closed writers."""

        if not isinstance(runtime_error, FocusedIncompleteTransitionError):
            return
        root = Path(configuration["run_directory"])
        source = FocusedRawEvidenceSource(
            root,
            process_records=(),
            expected_run_id=str(configuration["run_id"]),
            expected_source_instances=_document(
                configuration.get("source_instances"), "source instances"
            ),
        )
        events = source._events()
        source._reject_post_fault_target_events(events)
        barrier_states = source._transition_barrier_states(events)
        current_state = next(
            state for state in barrier_states if state["phase"] == runtime_error.phase
        )
        missing = tuple(int(item) for item in current_state["missing_survivor_ids"])
        any_incomplete = any(state["missing_survivor_ids"] for state in barrier_states)
        survivors = tuple(
            replica
            for replica in source._profile.replica_ids
            if replica not in source._profile.target_replica_ids
        )
        document = {
            "schema_version": 1,
            "kind": "kauri-focused-transition-barrier-final-state-v1",
            "disposition": (
                "incomplete_after_cleanup"
                if any_incomplete
                else "completed_during_cleanup"
            ),
            "phase": runtime_error.phase,
            "epoch_number": runtime_error.epoch,
            "transition_kind": (
                "activation" if runtime_error.activation else "command"
            ),
            "expected_survivor_ids": list(survivors),
            "observed_survivor_ids": [
                replica for replica in survivors if replica not in set(missing)
            ],
            "missing_survivor_ids": list(missing),
            "barriers": barrier_states,
            "live_observation": {
                "missing_survivor_ids": list(
                    runtime_error.live_missing_survivor_ids
                ),
                "raw_event_stream_sha256": (
                    runtime_error.live_raw_event_stream_sha256
                ),
                "source_cutoffs": [
                    dict(item) for item in runtime_error.live_source_cutoffs
                ],
            },
            "final_raw_event_stream_sha256": _sha256(_canonical_json(events)),
        }
        profiled_fault_runtime.write_exclusive(
            root / "runtime" / "incomplete-transition-barrier.json",
            _canonical_json(document),
        )
        runtime_error.bind_final_replay(missing)

    def cleanup(
        self,
        configuration: Mapping[str, object],
        processes: _FocusedProcesses,
    ) -> Mapping[str, object]:
        outcomes: Sequence[object] = ()
        cleanup_error: BaseException | None = None
        cleanup_traceback = None
        try:
            outcomes = (
                self._cleanup_registry(processes)
                if self._cleanup_registry is not None
                else processes.registry.cleanup(timeout_s=2.0)
            )
        except BaseException as exc:
            cleanup_error = exc
            cleanup_traceback = exc.__traceback__
        for log in getattr(processes, "logs", ()):
            try:
                log.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                    cleanup_traceback = exc.__traceback__
        evidence = getattr(processes, "evidence", None)
        if evidence is not None:
            try:
                evidence.__exit__(None, None, None)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                    cleanup_traceback = exc.__traceback__
        if cleanup_error is not None:
            raise cleanup_error.with_traceback(cleanup_traceback)
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
        profile = configuration.get("profile")
        if isinstance(profile, FocusedProfile) and _is_v5_profile(profile):
            source = FocusedRawEvidenceSource(
                root,
                poll_interval_s=0.0,
                timeout_s=1.0,
                expected_run_id=str(configuration["run_id"]),
                expected_source_instances=_document(
                    configuration.get("source_instances"),
                    "focused source instances",
                ),
            )
            validated_events = source._events()
            phase_document = _v5_phase_window_document(
                profile,
                str(configuration["arm"]),
                validated_events,
                _document(receipt, "fault receipt"),
                source,
            )
            (root / "derived" / "phase-windows.json").write_bytes(
                _canonical_json(phase_document)
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
        return self._seal_artifacts(root, sorted(required), outcome, cleanup)

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
