"""Independent source-blind validation for focused N31 crash-pair evidence."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
import hashlib
from itertools import combinations
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any

from experiments.adaptive import run_n31_crash_pair_campaign as campaign_contracts

from . import factorial_validation
from . import focused_crash_pair_runtime
from .profiled_fault_archive import EvidenceSealError, verify_evidence_seal

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
_V13_READINESS_MANIFEST_MAX_BYTES = 64 * 1024
_V13_READINESS_VERIFIER_TIMEOUT_SECONDS = 5
_V13_READY_IDENTITY_DOMAIN = b"kauri-adaptive-v3-activation-ready-identity-v1"
_V13_READY_OBSERVATION_DOMAIN = b"kauri-adaptive-v3-activation-ready-observation-v1"
_V13_READY_CERTIFICATE_DOMAIN = b"kauri-adaptive-v3-activation-readiness-certificate-v1"
_V13_READY_ACK_DOMAIN = b"kauri-adaptive-v3-activation-readiness-ack-v1"
_V13_READY_CERTIFICATE_DIGEST_DOMAIN = b"kauri-adaptive-v3-activation-readiness-certificate-digest-v1"
_V13_READY_ACK_PAYLOAD_DIGEST_DOMAIN = b"kauri-adaptive-v3-activation-readiness-ack-payload-digest-v1"
_V13_READY_OBSERVATION_OPCODE = 0x21
_V13_READY_CERTIFICATE_OPCODE = 0x22
_V13_MAX_REPLICA_MESSAGE_BYTES = 4 << 20
_V13_MAXIMUM_DELIVERY_ATTEMPTS = 5
_V13_READINESS_MEMBERSHIP_DOMAIN = (
    b"kauri-adaptive-v3-activation-readiness-membership-v1"
)
_PROFILE_KEYS_V2 = _PROFILE_KEYS | {"evidence_guard"}
_PROFILE_KEYS_V4 = _PROFILE_KEYS_V2 | {"fault_window_arm"}
_FCRASH_H_V3_PROFILE_IDS = frozenset(
    {
        "n7-f2-q5-two-crash-pair-smoke-v3",
        "n31-f5-q21-three-crash-pair-v3",
    }
)
_FCRASH_H_V4_PROFILE_IDS = frozenset(
    {
        "n7-f2-q5-two-crash-pair-smoke-v4",
        "n31-f5-q21-three-crash-pair-v4",
    }
)
_FCRASH_H_V5_PROFILE_IDS = frozenset(
    {
        "n7-f2-q5-two-crash-pair-smoke-v5",
        "n31-f5-q21-three-crash-pair-v5",
    }
)
_FCRASH_H_V5_IDENTITIES = {
    "n7-f2-q5-two-crash-pair-smoke-v5": (
        "a358c93d9cc418f06630108f30653ffb6bca3fa51fd05c7a62089b2a2814d6dc",
        "6127a7a6d11391c237eea8c9f8bc392666a61686083569b6cdd715127beda4c6",
    ),
    "n31-f5-q21-three-crash-pair-v5": (
        "5c080f6632b99f3be25b0253283e0da2f13fd5a892f77494342e1b6c248cafa2",
        "197650f1d4b4c2e0db950ad8dc191a830a36c3d412971036b03a283b245b381e",
    ),
}
_FCRASH_H_V6_PROFILE_IDS = frozenset(
    {
        "n7-f2-q5-two-crash-pair-smoke-v6",
        "n31-f5-q21-three-crash-pair-v6",
    }
)
_FCRASH_H_V6_IDENTITIES = {
    "n7-f2-q5-two-crash-pair-smoke-v6": (
        "13194cb75a0fe6cb40623be3d53fc1fe1db41f75f9c284e1d1267e409a447c00",
        "d59be85eac2b6828368f92f7c927bbf1eb4990c1ecb0eaee5b0f563b7de35a3c",
    ),
    "n31-f5-q21-three-crash-pair-v6": (
        "780d98fc0122c56fddf4ad2aa5eb3fc9c2f8a85386ada565257855b6dd6c527c",
        "a2d0435df187dd66c50daf59dedf24c4ffbcc4dcbb1a93191601c913bc959f0d",
    ),
}
_FCRASH_H_V7_PROFILE_IDS = frozenset(
    {
        "n7-f2-q5-two-crash-pair-smoke-v7",
        "n31-f5-q21-three-crash-pair-v7",
    }
)
_FCRASH_H_V7_IDENTITIES = {
    "n7-f2-q5-two-crash-pair-smoke-v7": (
        "ca948e998acfd1fc9511139321d11d5de9dd9856eb50293612f2ea31732f7d3d",
        "7e2d06acfaeebeb6c4b97fd83726cda86a64e7b9d419103a86502c189c04aa9b",
    ),
    "n31-f5-q21-three-crash-pair-v7": (
        "188890afb3dd2fff0b2e5f4cbf8614f6a21afdf67a1874844e9b466fe76c5abb",
        "60b53e89d24c76ff2016f43f49dbfdd8c88d80da9c4b96a15251d89a3bc870f3",
    ),
}
_FCRASH_H_V8_PROFILE_IDS = frozenset(
    {"n7-f2-q5-two-crash-pair-smoke-v8", "n31-f5-q21-three-crash-pair-v8"}
)
_FCRASH_H_V8_IDENTITIES = {
    "n7-f2-q5-two-crash-pair-smoke-v8": (
        "dfadb278014e21224b0326b1d4654f2dad0029e5dc1892aae7312e979170e64f",
        "1f88b12272e78546614a4467ccbbe790cadfa3e5cafec3336e457e8f1226a82f",
    ),
    "n31-f5-q21-three-crash-pair-v8": (
        "9d015b2c38c294d0b598cb8f48f579e33d116d276a612ba5edc36cd7edc883a9",
        "ba6e0670a9d2070b4344a404c7e4974f938beb57a12714865883a74a047f6c0f",
    ),
}
_FCRASH_H_V9_PROFILE_IDS = frozenset(
    {"n7-f2-q5-two-crash-pair-smoke-v9", "n31-f5-q21-three-crash-pair-v9"}
)
_FCRASH_H_V9_IDENTITIES = {
    "n7-f2-q5-two-crash-pair-smoke-v9": (
        "fb29c2a8c0a8f88177ecfade5b62255ca5966e359b523c1e3aa918500c10d74f",
        "652d310c0df2795ba28ccb56d49a8f276b9acfb68e0ec7064cdcb967dae83089",
    ),
    "n31-f5-q21-three-crash-pair-v9": (
        "bdb796bd1b8cf3fcf1c10c30df80b2e46d6b5fe1b2c4b9e1d037031ead1b04b0",
        "a50bd106887e5e3baf550875217db37c96fb9b07d40c39a98e80cef20e435ced",
    ),
}
_FCRASH_H_V10_PROFILE_IDS = frozenset(
    {"n7-f2-q5-two-crash-pair-smoke-v10", "n31-f5-q21-three-crash-pair-v10"}
)
_FCRASH_H_V11_PROFILE_IDS = frozenset(
    {"n7-f2-q5-two-crash-pair-smoke-v11", "n31-f5-q21-three-crash-pair-v11"}
)
_FCRASH_H_V12_PROFILE_IDS = frozenset(
    {"n7-f2-q5-two-crash-pair-smoke-v12", "n31-f5-q21-three-crash-pair-v12"}
)
_FCRASH_H_V13_PROFILE_IDS = frozenset(
    {"n7-f2-q5-two-crash-pair-smoke-v13", "n31-f5-q21-three-crash-pair-v13"}
)
_N31_FCRASH_H_V13_PROFILE_ID = "n31-f5-q21-three-crash-pair-v13"
_FCRASH_H_V13_IDENTITIES = {
    "n7-f2-q5-two-crash-pair-smoke-v13": (
        "3aa61c80d1c777db1469658532c978afc6fb52291cc6859cb5a889810541bd03",
        "8a43f845b41b8813df0c689adc1042baa4457af1c6061ad6db677434e5d4b99d"),
    "n31-f5-q21-three-crash-pair-v13": (
        "ea89fccc4910a7dfa5e8851d82f9033903d94f7c8e2df322e406b011a33bc1c9",
        "e32030efcaafbbd603aa61e59e94a89d9d3fd79ac9d8ea20359a10347f5cd619"),
}
_FCRASH_H_V12_IDENTITIES = {
    "n7-f2-q5-two-crash-pair-smoke-v12": (
        "54a879d783e071da2fce59773c79439a691ed549b50296c6f1bcea848694e697",
        "dc30f42f28d7228a44efe69f734cceebf965169202923b52c786f6e58e2e81d9",
    ),
    "n31-f5-q21-three-crash-pair-v12": (
        "2686e76451dea29018e33c87756b6c722badf82fd0d0c8f3500b860b424164ea",
        "7dbab26b7272d3f31e8bbcb38dc5778fc3666f11d5d5586dae67f499547f3ca2",
    ),
}
_FCRASH_H_V11_IDENTITIES = {
    "n7-f2-q5-two-crash-pair-smoke-v11": (
        "02b3ca67f3caf1e145f80daa700531188d6828bf25d4102d61174e99dd729bbb",
        "bbe9c4df3f6f5a05e16822abf3f27e91c6e121d0449fd69fc18396c6bb9d6814",
    ),
    "n31-f5-q21-three-crash-pair-v11": (
        "bab7175e31f961af7dcd1197deac332b739fa9c1c21e00da54e481c1f4dc184e",
        "f5a3d5c343580e71e90de4f4bf9b4e7198c61fe7b258d197e0ab0050c94341ae",
    ),
}
_FCRASH_H_V10_IDENTITIES = {
    "n7-f2-q5-two-crash-pair-smoke-v10": (
        "b57b6406be768305f917d7ad45d022e510733d01932bd75c91aa027e82e0b34c",
        "b08423625ab4eedb78a3bb18eaeceda860d006f81ab67b807b228f30cd7ad5e7",
    ),
    "n31-f5-q21-three-crash-pair-v10": (
        "066fbd2b1a14d6cec0d86eaafe28e19e4b52ed2a4cefb47d8a2b0079005bdf85",
        "8a4bc9a735cd73a31110ca4641e24a296247d5e17dd32af2e704ecbf76333a3d",
    ),
}
_FCRASH_H_GUARDED_PROFILE_IDS = (
    _FCRASH_H_V9_PROFILE_IDS
    | _FCRASH_H_V10_PROFILE_IDS
    | _FCRASH_H_V11_PROFILE_IDS
    | _FCRASH_H_V12_PROFILE_IDS
    | _FCRASH_H_V13_PROFILE_IDS
)
_REVIEWED_FOCUSED_PROFILE_IDS = frozenset(
    {
        "n7-f2-q5-two-crash-pair-smoke-v1",
        "n31-f5-q21-three-crash-pair-v1",
        "n7-f2-q5-two-crash-pair-smoke-v2",
        "n31-f5-q21-three-crash-pair-v2",
    }
) | (
    _FCRASH_H_V3_PROFILE_IDS
    | _FCRASH_H_V4_PROFILE_IDS
    | _FCRASH_H_V5_PROFILE_IDS
    | _FCRASH_H_V6_PROFILE_IDS
    | _FCRASH_H_V7_PROFILE_IDS
    | _FCRASH_H_V8_PROFILE_IDS
    | _FCRASH_H_GUARDED_PROFILE_IDS
)
_FAULT_WINDOW_PROFILE_IDS = (
    _FCRASH_H_V4_PROFILE_IDS
    | _FCRASH_H_V5_PROFILE_IDS
    | _FCRASH_H_V6_PROFILE_IDS
    | _FCRASH_H_V7_PROFILE_IDS
    | _FCRASH_H_V8_PROFILE_IDS
    | _FCRASH_H_GUARDED_PROFILE_IDS
)
_FAULT_WINDOW_ARM_DOMAIN_V1 = "kauri-focused-fault-window-arm-v1"
_FAULT_WINDOW_ARM_DOMAIN_V2 = "kauri-focused-fault-window-arm-v2"
_FAULT_WINDOW_ARM_DOMAIN_V3 = "kauri-focused-fault-window-arm-v3"
_FAULT_WINDOW_ARM_DOMAIN_V4 = "kauri-focused-fault-window-arm-v4"
_FAULT_WINDOW_ARM_FILENAME = "fault-window-arm.json"
_EVENT_KEYS = {
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
_TARGETS = (22, 23, 24)
_MEMBERS = tuple(range(31))
_SURVIVORS = tuple(replica for replica in _MEMBERS if replica not in _TARGETS)
_QUORUM = 21
_FANOUT = 5
_PIPELINE_STRETCH = 2
_EPOCH_ZERO_DIGEST = "145fac093343fa9cff20fcf49d85ad5443e93db14146f7854b17e28cf44f6d7a"
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
    """Return the exact sealed manager policy for one frozen profile."""

    policy = dict(_NATIVE_RESPONSIVENESS_POLICY)
    if profile_id == _N31_FCRASH_H_V13_PROFILE_ID:
        policy["trailing_timeout_streak"] = policy["attempt_window"]
    return policy


_MAIN_CONFIG_KEYS = {
    "aggregation-timeout",
    "async_blocks",
    "base-timeout",
    "block-size",
    "client-ip",
    "epoch-change-issuer-id",
    "epoch-change-issuer-public-key",
    "epoch-change-maximum-activation-delay",
    "epoch-change-maximum-ancestry-blocks",
    "epoch-change-maximum-block-extra-bytes",
    "epoch-change-minimum-activation-delay",
    "epoch-manager-address",
    "epoch-manager-tls-cert",
    "epoch-protocol-mode",
    "fan-out",
    "leader-activation-grace",
    "leader-progress-timeout",
    "max-rep-msg",
    "nworker",
    "pace-maker",
    "piped_latency",
    "prop-delay",
    "proposer",
    "replica",
    "repnworker",
    "stat-period",
    "tree-generation",
    "tree-switch-period",
}


class FocusedCrashPairValidationError(ValueError):
    """A sealed focused arm, pair, or campaign is not independently valid."""


def _error(message: str) -> None:
    raise FocusedCrashPairValidationError(message)


def _validate_controller_failure_terminal(
    payload: Mapping[str, Any], *, require_for_unhealthy: bool
) -> bool:
    """Validate the sealed diagnostic controller-failure projection."""

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


def _authoritative_lifecycle_instance(
    events: Sequence[Mapping[str, Any]], expected_source: str
) -> str:
    """Bind the authoritative commit source to one sealed lifecycle instance."""

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
        _error("sealed authoritative progress lacks an exact lifecycle binding")
    if any(event.get("source_kind") != "replica" for event in lifecycle):
        _error("sealed authoritative progress lifecycle kind drifted")
    instances = {event.get("source_instance") for event in lifecycle}
    if len(instances) != 1 or not isinstance(next(iter(instances)), str):
        _error("sealed authoritative progress lifecycle instance is ambiguous")
    return str(next(iter(instances)))


def _canonical(value: object) -> bytes:
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
        raise FocusedCrashPairValidationError("evidence is not canonical JSON") from exc


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha_bytes(value: bytes) -> str:
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
        payload = b"".join(
            (
                b"kauri-response-observation-v3",
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
        raise FocusedCrashPairValidationError(
            "v6 observation identity fields are out of range"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _is_v4_contract(contract: Mapping[str, object]) -> bool:
    return contract.get("profile_id") in _FAULT_WINDOW_PROFILE_IDS


def _is_v5_contract(contract: Mapping[str, object]) -> bool:
    return (
        contract.get("profile_id")
        in _FCRASH_H_V5_PROFILE_IDS
        | _FCRASH_H_V6_PROFILE_IDS
        | _FCRASH_H_V7_PROFILE_IDS
        | _FCRASH_H_V8_PROFILE_IDS
        | _FCRASH_H_GUARDED_PROFILE_IDS
    )


def _is_v6_contract(contract: Mapping[str, object]) -> bool:
    return contract.get("profile_id") in _FCRASH_H_V6_PROFILE_IDS


def _is_v7_contract(contract: Mapping[str, object]) -> bool:
    return contract.get("profile_id") in _FCRASH_H_V7_PROFILE_IDS


def _is_v8_contract(contract: Mapping[str, object]) -> bool:
    return contract.get("profile_id") in _FCRASH_H_V8_PROFILE_IDS


def _is_v9_contract(contract: Mapping[str, object]) -> bool:
    return contract.get("profile_id") in _FCRASH_H_GUARDED_PROFILE_IDS


def _is_v10_contract(contract: Mapping[str, object]) -> bool:
    return contract.get("profile_id") in (
        _FCRASH_H_V10_PROFILE_IDS
    | _FCRASH_H_V11_PROFILE_IDS
    | _FCRASH_H_V12_PROFILE_IDS
    | _FCRASH_H_V13_PROFILE_IDS
    )


def _is_v8_or_v9_contract(contract: Mapping[str, object]) -> bool:
    return _is_v8_contract(contract) or _is_v9_contract(contract)


def _v7_n31_target_selection_metric() -> dict[str, object]:
    """Frozen topology-only N31 choice, recomputed from its public domain."""
    replica_count = 31
    fanout = 5
    candidates = (21, 22, 23, 24, 25)
    prefix = (20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 0, 1, 2, 3, 4)
    order = tuple(range(20, 31)) + tuple(range(20))
    rows = []
    for targets in combinations(candidates, 3):
        per_tree = []
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
        "bfs_member_order": list(order),
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
    """Independently recompute the frozen v8 topology-only selection table."""

    replica_count, fanout = 31, 5
    candidates = (21, 22, 23, 24, 25)
    prefix = (20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 0, 1, 2, 3, 4, 5)
    order = tuple(range(20, 31)) + tuple(range(20))
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
    minimum_total = min(row[1] for row in rows)
    selected = min(
        targets for targets, total, _fixed, _maximum in rows if total == minimum_total
    )
    return {
        "schema_version": 1,
        "domain": "kauri-topology-survivor-path-shadow-v1",
        "candidate_internal_replica_ids": list(candidates),
        "prefix_tree_ids": list(prefix),
        "fanout": fanout,
        "bfs_member_order": list(order),
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
    """Independently recompute the frozen v12 full-cycle selection table."""

    replica_count, fanout = 31, 5
    candidates = (21, 22, 23, 24, 25)
    prefix = tuple(range(20, 31)) + tuple(range(20))
    order = tuple(range(20, 31)) + tuple(range(20))
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
        "bfs_member_order": list(order),
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


def _v8_reporter_capacity_document(
    *, replica_count: int, fanout: int, targets: Sequence[int], prefix: Sequence[int]
) -> dict[str, object]:
    """Independently derive the v8 topology-only reporter relation capacity."""

    leaf_start = (replica_count - 1 + fanout - 1) // fanout
    crashed = set(targets)
    rows: list[dict[str, object]] = []
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


def _v12_all_candidate_reporter_capacity_document(
    *,
    replica_count: int,
    quorum: int,
    fanout: int,
    unavailable: Sequence[int],
    prefix: Sequence[int],
) -> dict[str, object]:
    """Independently derive all v12 candidate relations under fixed crashes."""

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


def _is_v9_guard_relation(
    contract: Mapping[str, object],
    *,
    target: int,
    reporter: int,
    tree_id: int,
    message_type: str,
    prefix: Collection[int],
) -> bool:
    """Independently validate a topology relation for an inferred v9 cohort."""

    members = tuple(int(member) for member in contract["members"])
    count = len(members)
    fanout = _integer(contract.get("fanout"), "tree fanout", 1)
    position = (target - tree_id) % count
    leaf_start = (count - 1 + fanout - 1) // fanout
    return (
        tree_id in prefix
        and target in members
        and reporter in members
        and position != 0
        and _cyclic_parent(count, fanout, tree_id, target) == reporter
        and message_type
        == ("aggregate_relay" if position < leaf_start else "direct_vote")
    )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _error(f"{label} must be a sequence")
    return value


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
        _error(f"{label} is not a lowercase SHA-256 digest")
    return value


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        _error(f"{label} is absent or not a regular file")
    try:
        return _mapping(json.loads(path.read_bytes()), label)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise FocusedCrashPairValidationError(f"{label} is invalid JSON") from exc


def _profile_identity(profile: Mapping[str, Any]) -> dict[str, Any]:
    identity = json.loads(json.dumps(dict(profile)))
    topology = identity.get("topology")
    if not isinstance(topology, dict):
        _error("profile topology is malformed")
    topology.pop("proof_sha256", None)
    return identity


def _cyclic_parent(
    replica_count: int, fanout: int, root: int, target: int
) -> int | None:
    position = (target - root) % replica_count
    if position == 0:
        return None
    return (root + (position - 1) // fanout) % replica_count


def _derive_reporter_coverage_plan(
    profile: Mapping[str, Any],
    *,
    members: tuple[int, ...],
    targets: tuple[int, ...],
    fault_threshold: int,
    quorum: int,
    fanout: int,
    active_tree: int,
) -> dict[str, object]:
    guard = _mapping(profile.get("evidence_guard"), "profile evidence guard")
    timers = _mapping(profile.get("timers"), "profile timers")
    expected_guard_keys = {
        "schedule",
        "tree_switch_period_blocks",
        "horizon_tree_positions",
        "required_qualifying_reporters",
        "minimum_timeouts_per_reporter",
        "minimum_score_drop",
    }
    if profile["profile_id"] in _FCRASH_H_V3_PROFILE_IDS | _FAULT_WINDOW_PROFILE_IDS:
        expected_guard_keys.add("required_postfault_tree_positions")
    if profile["profile_id"] in _FCRASH_H_V8_PROFILE_IDS | _FCRASH_H_GUARDED_PROFILE_IDS:
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
    has_v12_coverage_deadline = profile["profile_id"] in (
        _FCRASH_H_V12_PROFILE_IDS | _FCRASH_H_V13_PROFILE_IDS
    )
    if has_v12_coverage_deadline:
        expected_timer_keys.add("postfault_configuration_coverage_deadline_seconds")
    if set(guard) != expected_guard_keys or set(timers) != expected_timer_keys:
        _error("FCRASH-H guard or timer schema drifted")
    required = fault_threshold + 1
    if profile["profile_id"] in _FCRASH_H_V8_PROFILE_IDS | _FCRASH_H_GUARDED_PROFILE_IDS:
        topology = _mapping(profile.get("topology"), "profile topology")
        arm = _mapping(profile.get("fault_window_arm"), "fault-window arm")
        capacity = _v8_reporter_capacity_document(
            replica_count=len(members),
            fanout=fanout,
            targets=targets,
            prefix=_sequence(arm.get("ordered_tree_prefix"), "fault-window prefix"),
        )
        horizon = len(capacity["prefix_tree_ids"])
        expected_guard = {
            "schedule": "native_cyclic_epoch_zero",
            "tree_switch_period_blocks": 2,
            "horizon_tree_positions": horizon,
            "required_postfault_tree_positions": horizon,
            "required_qualifying_reporters": required,
            "minimum_timeouts_per_reporter": 2,
            "minimum_score_drop": 2 * required,
            "reporter_selection_basis": "any_topology_valid_in_prefix_v1",
            "minimum_topology_eligible_reporter_capacity": capacity[
                "minimum_topology_eligible_reporter_capacity"
            ],
        }
        if (
            dict(guard) != expected_guard
            or topology.get("reporter_coverage_capacity") != capacity
        ):
            _error("v8 frozen evidence guard differs from topology derivation")
        all_candidate_capacity = None
        if profile["profile_id"] in {
            "n31-f5-q21-three-crash-pair-v12",
            "n31-f5-q21-three-crash-pair-v13",
        }:
            all_candidate_capacity = _v12_all_candidate_reporter_capacity_document(
                replica_count=len(members),
                quorum=quorum,
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
                _error("v12 all-candidate reporter capacity drifted")
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
                if has_v12_coverage_deadline
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
                has_v12_coverage_deadline
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
            "profile_id": profile["profile_id"],
            "active_tree_id": active_tree,
            "horizon_tree_positions": horizon,
            "required_postfault_tree_positions": horizon,
            "required_qualifying_reporters": required,
            "minimum_timeouts_per_reporter": 2,
            "minimum_score_drop": 2 * required,
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
    reporter_sets: list[set[int]] = []
    horizon = 0
    count = len(members)
    for target in targets:
        seen: set[int] = set()
        first: list[dict[str, int]] = []
        for offset in range(count):
            tree_id = (active_tree + offset) % count
            if tree_id in targets:
                continue
            reporter = _cyclic_parent(count, fanout, tree_id, target)
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
        reporter_sets.append({row["reporter_id"] for row in first})
        target_rows.append(
            {
                "target_replica_id": target,
                "authenticated_reporter_ids": [],
                "first_qualifying_reporters": first,
            }
        )
    common = set.intersection(*reporter_sets)
    if len(common) != required:
        _error("FCRASH-H targets lack one common honest reporter set")
    common_ids = sorted(common)
    for row in target_rows:
        row["authenticated_reporter_ids"] = common_ids
    expected_period = (
        2
        if profile["profile_id"] in _FCRASH_H_V3_PROFILE_IDS | _FAULT_WINDOW_PROFILE_IDS
        else count
    )
    expected_guard = {
        "schedule": "native_cyclic_epoch_zero",
        "tree_switch_period_blocks": expected_period,
        "horizon_tree_positions": horizon,
        "required_qualifying_reporters": required,
        "minimum_timeouts_per_reporter": 2,
        "minimum_score_drop": 2 * required,
    }
    if profile["profile_id"] in _FCRASH_H_V3_PROFILE_IDS | _FAULT_WINDOW_PROFILE_IDS:
        expected_guard["required_postfault_tree_positions"] = horizon
    if dict(guard) != expected_guard:
        _error("FCRASH-H frozen evidence guard differs from topology derivation")
    deadlines = {
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
        deadlines["evidence_seconds"] >= deadlines["epoch1_activation_seconds"]
        or deadlines["epoch1_activation_seconds"] >= deadlines["arm_hard_seconds"]
        or deadlines["optimization_activation_seconds"] >= deadlines["arm_hard_seconds"]
    ):
        _error("FCRASH-H phase deadlines are not strictly nested")
    return {
        "schema_version": 1,
        "profile_id": profile["profile_id"],
        "active_tree_id": active_tree,
        "horizon_tree_positions": horizon,
        "required_qualifying_reporters": required,
        "minimum_timeouts_per_reporter": 2,
        "minimum_score_drop": 2 * required,
        **(
            {
                "required_postfault_tree_positions": horizon,
                "nominal_commit_horizon": horizon * expected_period,
            }
            if profile["profile_id"]
            in _FCRASH_H_V3_PROFILE_IDS | _FAULT_WINDOW_PROFILE_IDS
            else {}
        ),
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
        "targets": target_rows,
    }


def _inside_deadline(origin_ns: int, candidate_ns: int, seconds: int) -> bool:
    return origin_ns <= candidate_ns < origin_ns + seconds * 1_000_000_000


def _v12_arm_before_configuration_coverage_deadline(
    contract: Mapping[str, object],
    coverage: Mapping[str, object],
    *,
    evidence_start_ns: int,
    armed_ns: int,
) -> bool:
    if contract.get("profile_id") not in _FCRASH_H_V12_PROFILE_IDS:
        return True
    return _inside_deadline(
        evidence_start_ns,
        armed_ns,
        _integer(
            _mapping(coverage["deadlines_seconds"], "coverage deadlines").get(
                "configuration_coverage_seconds"
            ),
            "configuration coverage deadline",
            1,
        ),
    )


def validate_fcrash_h_evidence(
    contract: Mapping[str, object], witness: Mapping[str, object]
) -> None:
    """Validate one independently reconstructed FCRASH-H causal witness."""

    coverage = _mapping(
        contract.get("reporter_coverage_plan"), "reporter coverage plan"
    )
    is_v6 = _is_v6_contract(contract)
    is_v7 = _is_v7_contract(contract) or _is_v8_or_v9_contract(contract)
    is_v8 = _is_v8_or_v9_contract(contract)
    is_v9 = _is_v9_contract(contract)
    expected_keys = {
        "fault_monotonic_ns",
        "nonresponse_monotonic_ns",
        "snapshot_audit_monotonic_ns",
        "epoch1_activation_monotonic_ns",
        "epoch2_activation_monotonic_ns",
        "timeout_observations",
        "guard_drawdowns",
    }
    required_progress = coverage.get("required_postfault_tree_positions")
    if required_progress is not None:
        expected_keys.add("postfault_progress")
    if is_v6 or is_v7 or _is_v8_or_v9_contract(contract):
        expected_keys.add("eligible_guard_drawdowns")
    if is_v9:
        expected_keys.add("guarded_nonresponsive_replica_ids")
    if set(witness) != expected_keys:
        _error("FCRASH-H witness schema drifted")
    fault_ns = _integer(witness.get("fault_monotonic_ns"), "fault timestamp")
    nonresponse_ns = _integer(
        witness.get("nonresponse_monotonic_ns"), "nonresponse timestamp"
    )
    snapshot_audit_ns = _integer(
        witness.get("snapshot_audit_monotonic_ns"), "snapshot audit timestamp"
    )
    epoch1_ns = _integer(
        witness.get("epoch1_activation_monotonic_ns"), "Epoch 1 activation"
    )
    epoch2_raw = witness.get("epoch2_activation_monotonic_ns")
    epoch2_ns = (
        None if epoch2_raw is None else _integer(epoch2_raw, "Epoch 2 activation")
    )
    deadlines = _mapping(coverage.get("deadlines_seconds"), "coverage deadlines")
    if (
        not _inside_deadline(
            fault_ns,
            snapshot_audit_ns,
            _integer(deadlines.get("evidence_seconds"), "evidence deadline", 1),
        )
        or not _inside_deadline(
            fault_ns,
            epoch1_ns,
            _integer(
                deadlines.get("epoch1_activation_seconds"),
                "Epoch 1 activation deadline",
                1,
            ),
        )
        or nonresponse_ns >= snapshot_audit_ns
        or snapshot_audit_ns >= epoch1_ns
        or (
            epoch2_ns is not None
            and (
                epoch2_ns <= epoch1_ns
                or not _inside_deadline(
                    epoch1_ns,
                    epoch2_ns,
                    _integer(
                        deadlines.get("optimization_activation_seconds"),
                        "Epoch 2 activation deadline",
                        1,
                    ),
                )
            )
        )
    ):
        _error("FCRASH-H causal timestamp or deadline drifted")
    guarded_targets: tuple[int, ...] | None = None
    if is_v9:
        guarded_targets = tuple(
            _integer(target, "guarded cohort member")
            for target in _sequence(
                witness.get("guarded_nonresponsive_replica_ids"), "guarded cohort"
            )
        )
        if (
            guarded_targets != tuple(sorted(set(guarded_targets)))
            or not set(contract["targets"]).issubset(guarded_targets)
            or len(guarded_targets) < len(tuple(contract["targets"]))
            or len(guarded_targets)
            > len(tuple(contract["members"])) - int(contract["quorum"])
        ):
            _error("v9 guarded cohort cardinality or crash binding drifted")
    relations: dict[int, dict[int, set[tuple[int, str]]]] = {}
    if is_v8 and not is_v9:
        for row in _sequence(coverage.get("targets"), "coverage targets"):
            target_row = _mapping(row, "coverage target")
            target = _integer(target_row.get("target_replica_id"), "coverage target")
            relations[target] = {
                _integer(reporter.get("reporter_id"), "coverage reporter"): {
                    (
                        _integer(relation.get("tree_id"), "coverage tree"),
                        str(relation.get("expected_message_type")),
                    )
                    for relation in _sequence(
                        _mapping(reporter, "eligible reporter").get("tree_relations"),
                        "coverage tree relations",
                    )
                }
                for reporter in _sequence(
                    target_row.get("eligible_reporters"), "eligible reporters"
                )
            }
        expected = {target: dict(reporters) for target, reporters in relations.items()}
        expected_trees: dict[tuple[int, int], int] = {}
    elif is_v9:
        expected = {target: {} for target in guarded_targets or ()}
        expected_trees = {}
    else:
        expected = {
            int(row["target_replica_id"]): {
                _integer(reporter["reporter_id"], "coverage reporter"): _integer(
                    reporter["tree_id"], "coverage first qualifying tree"
                )
                for reporter in _sequence(
                    row["first_qualifying_reporters"], "first qualifying reporters"
                )
            }
            for row in _sequence(coverage.get("targets"), "coverage targets")
        }
        expected_trees = {
            (int(row["target_replica_id"]), int(first["reporter_id"])): int(
                first["tree_id"]
            )
            for row in _sequence(coverage.get("targets"), "coverage targets")
            for first in _sequence(
                _mapping(row, "coverage target").get("first_qualifying_reporters"),
                "first qualifying reporters",
            )
        }
    counts = (
        {target: {} for target in expected}
        if is_v9
        else {
            target: {reporter: 0 for reporter in reporters}
            for target, reporters in expected.items()
        }
    )
    for raw in _sequence(witness.get("timeout_observations"), "timeout observations"):
        observation = _mapping(raw, "timeout observation")
        expected_observation_keys = {
            "epoch_number",
            "tree_id",
            "observed_replica_id",
            "reporter_id",
            "outcome",
            "compensated",
            "source_monotonic_ns",
        }
        if is_v8:
            expected_observation_keys.add("expected_message_type")
        if set(observation) != expected_observation_keys:
            _error("timeout observation schema drifted")
        target = _integer(observation.get("observed_replica_id"), "timeout target")
        reporter = _integer(observation.get("reporter_id"), "timeout reporter")
        timestamp = _integer(
            observation.get("source_monotonic_ns"), "timeout timestamp"
        )
        if (
            observation.get("epoch_number") != 0
            or (
                is_v9
                and not _is_v9_guard_relation(
                    contract,
                    target=target,
                    reporter=reporter,
                    tree_id=_integer(observation.get("tree_id"), "timeout tree"),
                    message_type=str(observation.get("expected_message_type")),
                    prefix=_sequence(
                        _mapping(
                            _mapping(contract.get("profile"), "focused profile").get(
                                "fault_window_arm"
                            ),
                            "fault-window arm",
                        ).get("ordered_tree_prefix"),
                        "fault-window prefix",
                    ),
                )
            )
            or (
                is_v8
                and not is_v9
                and (
                    _integer(observation.get("tree_id"), "timeout tree"),
                    str(observation.get("expected_message_type")),
                )
                not in relations.get(target, {}).get(reporter, set())
            )
            or (
                not is_v8
                and observation.get("tree_id") != expected_trees.get((target, reporter))
            )
            or observation.get("outcome") != "timeout"
            or observation.get("compensated") is not False
            or target not in counts
            or (not is_v9 and reporter not in counts[target])
            or not (fault_ns < timestamp <= nonresponse_ns)
        ):
            _error("timeout observation is not exact post-fault evidence")
        counts[target][reporter] = counts[target].get(reporter, 0) + 1
    minimum = _integer(
        coverage.get("minimum_timeouts_per_reporter"),
        "minimum timeouts per reporter",
        1,
    )
    required_reporters = _integer(
        coverage.get("required_qualifying_reporters"),
        "required qualifying reporters",
        1,
    )
    complete = (
        all(
            sum(count >= minimum for count in reporters.values()) >= required_reporters
            for reporters in counts.values()
        )
        if is_v8
        else all(
            count >= minimum
            for reporters in counts.values()
            for count in reporters.values()
        )
    )
    if not complete:
        _error("FCRASH-H reporter timeout coverage is incomplete")
    drawdowns = _mapping(witness.get("guard_drawdowns"), "guard drawdowns")
    minimum_drop = _integer(coverage.get("minimum_score_drop"), "minimum score drop", 1)
    if set(drawdowns) != {str(target) for target in expected} or any(
        type(drawdowns[str(target)]) is not int
        or int(drawdowns[str(target)]) > -minimum_drop
        for target in expected
    ):
        _error("FCRASH-H score drawdown is incomplete")
    if is_v6 or is_v7:
        eligible = _mapping(
            witness.get("eligible_guard_drawdowns"), "eligible guard drawdowns"
        )
        expected_eligible = {
            str(target): -sum(reporters.values())
            for target, reporters in counts.items()
        }
        if dict(eligible) != expected_eligible or any(
            value > -minimum_drop for value in expected_eligible.values()
        ):
            _error("v6 exact eligible timeout drawdown is incomplete")
    if required_progress is not None:
        progress = _mapping(witness.get("postfault_progress"), "post-fault progress")
        required_count = _integer(
            required_progress, "required post-fault commit horizon", 1
        )
        progress_keys = {
            "required_tree_positions",
            "actual_tree_positions",
            "starting_tree_id",
            "observed_tree_ids",
        }
        is_v12 = contract.get("profile_id") in _FCRASH_H_V12_PROFILE_IDS
        if is_v12:
            progress_keys.add("coverage_completion_monotonic_ns")
        if set(progress) != progress_keys or (
            _integer(
                progress.get("required_tree_positions"), "progress required count", 1
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
            _error("FCRASH-H post-fault authoritative progress is incomplete")
        start_tree = _integer(progress.get("starting_tree_id"), "progress start tree")
        observed_trees = tuple(
            _integer(tree, "progress observed tree")
            for tree in _sequence(progress.get("observed_tree_ids"), "progress trees")
        )
        expected_trees = tuple(
            (start_tree + offset) % len(tuple(contract["members"]))
            for offset in range(len(observed_trees))
        )
        if observed_trees != expected_trees:
            _error("FCRASH-H post-fault progress is not the exact cyclic prefix")
        if is_v12 and not _inside_deadline(
            fault_ns,
            _integer(
                progress.get("coverage_completion_monotonic_ns"),
                "configuration coverage completion",
            ),
            _integer(
                deadlines.get("configuration_coverage_seconds"),
                "configuration coverage deadline",
                1,
            ),
        ):
            _error("v12 configuration coverage exceeded its crash-anchored cap")


def _fcrash_h_postfault_progress(
    contract: Mapping[str, object],
    events: Sequence[Mapping[str, Any]],
    *,
    fault_ns: int,
    prefault_ns: int,
    audit_ns: int,
) -> dict[str, object]:
    """Independently reconstruct the v3 progress witness from sealed raw events."""

    coverage = _mapping(
        contract.get("reporter_coverage_plan"), "reporter coverage plan"
    )
    required_positions = _integer(
        coverage.get("required_postfault_tree_positions"),
        "required post-fault tree positions",
        1,
    )
    observer = _integer(
        contract.get("authoritative_replica_id"), "authoritative observer", 0
    )
    expected_source = f"replica-{observer}"
    expected_instance = _authoritative_lifecycle_instance(events, expected_source)
    expected_digest = str(contract["epoch_zero_digest"])
    transactions_per_block = _integer(
        contract.get("transactions_per_block"), "transactions per block", 1
    )
    members = tuple(int(member) for member in contract["members"])
    member_sources = {f"replica-{member}" for member in members}
    if any(
        event["source_kind"] == "replica"
        and event["source_id"] in member_sources
        and event["event_type"] == "adaptive.configuration_active"
        and prefault_ns
        <= _integer(event["source_monotonic_ns"], "configuration timestamp")
        <= fault_ns
        for event in events
    ):
        _error("configuration changed during the atomic fault batch")
    start_events = [
        event
        for event in events
        if event["source_kind"] == "replica"
        and event["source_id"] == expected_source
        and event["source_instance"] == expected_instance
        and event["event_type"] == "adaptive.configuration_active"
        and _integer(event["source_monotonic_ns"], "configuration timestamp")
        < prefault_ns
    ]
    if not start_events:
        _error("sealed authoritative progress lacks a pre-fault configuration")
    historical_configurations = sorted(
        start_events,
        key=lambda event: (
            _integer(event["source_sequence"], "configuration sequence", 1),
            _integer(event["source_monotonic_ns"], "configuration timestamp"),
        ),
    )
    for position, event in enumerate(historical_configurations):
        payload = _mapping(event["payload"], "pre-fault configuration")
        epoch = _integer(payload.get("epoch_number"), "configuration epoch")
        tree = _integer(payload.get("tree_id"), "configuration tree")
        if (
            epoch != 0
            or payload.get("epoch_digest") != expected_digest
            or tree != members[position % len(members)]
        ):
            _error("sealed authoritative progress historical configuration drifted")
    start = max(
        start_events,
        key=lambda event: (
            _integer(event["source_sequence"], "configuration sequence", 1),
            _integer(event["source_monotonic_ns"], "configuration timestamp"),
        ),
    )
    start_payload = _mapping(start["payload"], "pre-fault configuration")
    starting_tree = _integer(start_payload.get("tree_id"), "starting tree")
    if (
        _integer(start_payload.get("epoch_number"), "starting epoch") != 0
        or start_payload.get("epoch_digest") != expected_digest
        or starting_tree != int(coverage["active_tree_id"])
    ):
        _error("sealed authoritative progress pre-fault configuration drifted")
    activations = sorted(
        [
            event
            for event in events
            if event["source_kind"] == "replica"
            and event["source_id"] == expected_source
            and event["source_instance"] == expected_instance
            and event["event_type"] == "adaptive.configuration_active"
            and fault_ns
            < _integer(event["source_monotonic_ns"], "configuration timestamp")
            < audit_ns
        ],
        key=lambda event: (
            _integer(event["source_sequence"], "configuration sequence", 1),
            _integer(event["source_monotonic_ns"], "configuration timestamp"),
        ),
    )
    # The frozen horizon starts with the configuration already active at the
    # fault boundary; only H-1 later activations are required.
    observed_trees: list[int] = [starting_tree]
    for position, event in enumerate(activations, start=1):
        payload = _mapping(event["payload"], "post-fault configuration")
        tree = _integer(payload.get("tree_id"), "activated tree")
        if (
            _integer(payload.get("epoch_number"), "activated epoch") != 0
            or payload.get("epoch_digest") != expected_digest
            or tree != members[(members.index(starting_tree) + position) % len(members)]
        ):
            _error("sealed authoritative progress cyclic configuration drifted")
        observed_trees.append(tree)
    if len(observed_trees) < required_positions:
        _error("FCRASH-H post-fault tree positions are incomplete")
    configurations = [
        (
            _integer(event["source_sequence"], "configuration sequence", 1),
            _integer(event["source_monotonic_ns"], "configuration timestamp"),
            _integer(
                _mapping(event["payload"], "pre-fault configuration").get("tree_id"),
                "configuration tree",
            ),
        )
        for event in historical_configurations
    ] + [
        (
            _integer(event["source_sequence"], "configuration sequence", 1),
            _integer(event["source_monotonic_ns"], "configuration timestamp"),
            tree,
        )
        for event, tree in zip(activations, observed_trees[1:], strict=True)
    ]
    for event in events:
        if event["event_type"] != "block.committed":
            continue
        if (
            event["source_kind"] != "replica"
            or event["source_id"] != expected_source
            or event["source_instance"] != expected_instance
        ):
            continue
        timestamp = _integer(event["source_monotonic_ns"], "progress commit timestamp")
        if not fault_ns < timestamp < audit_ns:
            continue
        payload = _mapping(event["payload"], "authoritative progress commit")
        proof = _mapping(payload.get("decision_proof"), "progress decision proof")
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
            _error("sealed authoritative progress commit schema drifted")
        if set(proof) != {
            "epoch_number",
            "tree_id",
            "epoch_digest",
            "block_hash",
        }:
            _error("sealed authoritative progress proof schema drifted")
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
            _error("sealed authoritative progress commit invariants drifted")
        commit_key = (
            _integer(event["source_sequence"], "progress source sequence", 1),
            timestamp,
        )
        if view_generation > len(configurations):
            _error("sealed authoritative progress commit generation is not activated")
        generation_configuration = configurations[view_generation - 1]
        if (
            generation_configuration[:2] > commit_key
            or generation_configuration[2] != proof_tree
        ):
            _error("sealed authoritative progress commit is not causally activated")
    result = {
        "required_tree_positions": required_positions,
        "actual_tree_positions": len(observed_trees),
        "starting_tree_id": starting_tree,
        "observed_tree_ids": observed_trees,
    }
    if contract.get("profile_id") in _FCRASH_H_V12_PROFILE_IDS:
        result["coverage_completion_monotonic_ns"] = _integer(
            activations[required_positions - 2]["source_monotonic_ns"],
            "configuration coverage completion",
        )
    return result


def _v4_replay_fault_window_anchors(
    contract: Mapping[str, object],
    events: Sequence[Mapping[str, Any]],
    *,
    baseline_cutoff: int,
    current_cutoff: int,
    audit: Mapping[str, Any],
    guarded_targets: Sequence[int] | None = None,
) -> tuple[list[dict[str, object]], dict[str, int], int]:
    """Replay the v4 arm's source-blind post-fault proposal boundary.

    The arm establishes only a finite, intervention-boundary-aware proposal
    domain.  Timeout evidence remains useful only when its exact ProposalKey
    was first anchored by an on-time direct vote in that domain.  This replay
    intentionally does not use the manager's selected ranking.
    """

    is_v7 = _is_v7_contract(contract) or _is_v8_or_v9_contract(contract)
    is_v8 = _is_v8_or_v9_contract(contract)
    is_v9 = _is_v9_contract(contract)
    is_v6 = _is_v6_contract(contract) or is_v7
    armed = [
        event
        for event in events
        if event["source_kind"] == "adaptation_manager"
        and event["source_id"] == "adaptive-manager"
        and event["event_type"] == "fault_window_armed"
    ]
    if len(armed) != 1:
        _error("v4 proposal-anchor replay lacks one armed boundary")
    arm = _mapping(armed[0]["payload"], "fault-window armed payload")
    start_ns = _integer(
        arm.get("evidence_start_monotonic_ns"), "fault-window evidence start", 1
    )
    prefix = tuple(
        _integer(tree, "fault-window required tree")
        for tree in _sequence(arm.get("required_tree_ids"), "fault-window trees")
    )
    if not prefix or len(prefix) != len(set(prefix)):
        _error("v4 proposal-anchor replay arm prefix is malformed")
    expected_digest = _digest(contract.get("epoch_zero_digest"), "epoch-zero digest")
    audit_ns = _integer(audit.get("source_monotonic_ns"), "snapshot audit timestamp")
    audit_sequence = _integer(
        audit.get("source_sequence"), "snapshot audit sequence", 1
    )

    accepted: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]] = []
    anchors: dict[int, set[tuple[int, int, str, str]]] = {
        tree: set() for tree in prefix
    }
    for event in events:
        if (
            event["source_kind"] != "adaptation_manager"
            or event["source_id"] != "adaptive-manager"
            or event["event_type"] != "evidence.observation_accepted"
        ):
            continue
        payload = _mapping(event["payload"], "accepted evidence")
        sequence = _integer(
            payload.get("ingestion_sequence"), "evidence ingestion sequence", 1
        )
        if sequence > current_cutoff:
            continue
        observation = _mapping(payload.get("observation"), "accepted observation")
        configuration = _mapping(
            observation.get("configuration"), "observation configuration"
        )
        configuration_epoch = _integer(
            configuration.get("epoch_number"), "observation epoch"
        )
        if (
            configuration_epoch != 0
            or configuration.get("epoch_digest") != expected_digest
        ):
            continue
        if (
            _integer(event["source_monotonic_ns"], "evidence acceptance time")
            > audit_ns
            or _integer(event["source_sequence"], "evidence acceptance sequence", 1)
            >= audit_sequence
        ):
            _error("v4 accepted evidence does not precede predecessor-0 audit")
        tree_id = _integer(configuration.get("tree_id"), "observation tree")
        block_hash = _digest(observation.get("block_hash"), "observation block hash")
        key = (0, tree_id, expected_digest, block_hash)
        if (
            not is_v6
            and observation.get("outcome") == "on_time"
            and observation.get("expected_message_type") == "direct_vote"
            and tree_id in anchors
            and factorial_validation._conservative_attempt_started_at_or_after(
                reporter_monotonic_ns=_integer(
                    observation.get("reporter_monotonic_ns"), "evidence reporter time"
                ),
                duration_us=_integer(
                    observation.get("response_duration_us"),
                    "evidence response duration",
                ),
                lower_bound_ns=start_ns,
            )
        ):
            anchors[tree_id].add(key)
        if sequence > baseline_cutoff:
            accepted.append((sequence, observation, event))
    coverage = _mapping(
        contract.get("reporter_coverage_plan"), "reporter coverage plan"
    )
    frozen_anchor_trees = (
        {
            _integer(reporter.get("tree_id"), "coverage anchor tree")
            for row in _sequence(coverage.get("targets"), "coverage targets")
            for reporter in _sequence(
                _mapping(row, "coverage target").get("first_qualifying_reporters"),
                "first qualifying reporters",
            )
        }
        if not is_v6
        else set()
    )
    if not is_v6 and (
        not frozen_anchor_trees
        or not frozen_anchor_trees.issubset(anchors)
        or any(not anchors[tree] for tree in frozen_anchor_trees)
    ):
        _error("v4 proposal-anchor replay lacks a frozen eligible-tree anchor")
    anchored_keys = frozenset(key for keys in anchors.values() for key in keys)

    proof_bound_relations: dict[int, dict[int, set[tuple[int, str]]]] = {}
    if is_v8:
        capacity_rows = coverage.get("targets")
        if contract.get("profile_id") in {
            "n31-f5-q21-three-crash-pair-v12",
            "n31-f5-q21-three-crash-pair-v13",
        }:
            capacity_rows = _mapping(
                coverage.get("all_candidate_reporter_coverage_capacity"),
                "all-candidate reporter capacity",
            ).get("candidates")
        for row in _sequence(capacity_rows, "coverage targets"):
            target = _integer(
                _mapping(row, "coverage target").get("target_replica_id"),
                "coverage target",
            )
            proof_bound_relations[target] = {
                _integer(reporter.get("reporter_id"), "coverage reporter"): {
                    (
                        _integer(relation.get("tree_id"), "coverage tree"),
                        str(relation.get("expected_message_type")),
                    )
                    for relation in _sequence(
                        _mapping(reporter, "eligible reporter").get("tree_relations"),
                        "coverage tree relations",
                    )
                }
                for reporter in _sequence(
                    _mapping(row, "coverage target").get("eligible_reporters"),
                    "eligible reporters",
                )
            }
        expected_trees: dict[tuple[int, int], int] = {}
    else:
        expected_trees = {
            (
                int(row["target_replica_id"]),
                _integer(reporter["reporter_id"], "coverage reporter"),
            ): _integer(reporter["tree_id"], "coverage tree")
            for row in _sequence(coverage.get("targets"), "coverage targets")
            for reporter in _sequence(
                _mapping(row, "coverage target").get("first_qualifying_reporters"),
                "first qualifying reporters",
            )
        }
    filtered_outstanding: dict[
        str, tuple[int, int, tuple[int, int, str, str], int, str]
    ] = {}
    filtered_completed: set[str] = set()
    global_outstanding: dict[str, tuple[int, int, tuple[int, int, str, str]]] = {}
    if is_v9:
        if (
            guarded_targets is None
            or any(type(target) is not int for target in guarded_targets)
            or tuple(guarded_targets) != tuple(sorted(set(guarded_targets)))
        ):
            _error("v9 guarded cohort is malformed")
        global_drawdowns = set(guarded_targets)
    else:
        global_drawdowns = {
            int(row["target_replica_id"])
            for row in _sequence(coverage.get("targets"), "coverage targets")
        }
    drawdowns = {target: 0 for target in global_drawdowns}
    for _ingestion_sequence, observation, event in sorted(
        accepted, key=lambda row: row[0]
    ):
        outcome = observation.get("outcome")
        if outcome not in {"on_time", "timeout", "late"}:
            _error("v4 replay observation outcome is malformed")
        configuration = _mapping(
            observation.get("configuration"), "observation configuration"
        )
        key = (
            0,
            _integer(configuration.get("tree_id"), "observation tree"),
            expected_digest,
            _digest(observation.get("block_hash"), "observation block hash"),
        )
        observation_id = observation.get("observation_id")
        reporter = observation.get("reporter_id")
        target = observation.get("observed_replica_id")
        expected_message_type = observation.get("expected_message_type")
        if (
            not isinstance(observation_id, str)
            or type(reporter) is not int
            or type(target) is not int
        ):
            _error("v4 replay observation identity is malformed")
        identity = (reporter, target, key)
        causal_raw = (
            not is_v7
            or _uint64(
                observation.get("attempt_start_monotonic_ns"),
                "v7 raw observation attempt start",
                1,
            )
            >= start_ns
        )
        if causal_raw:
            if outcome == "timeout":
                if observation_id in global_outstanding:
                    _error("v4 replay timeout observation ID is reused")
                global_outstanding[observation_id] = identity
                if target in drawdowns:
                    drawdowns[target] -= 1
            elif outcome == "on_time":
                if target in drawdowns and drawdowns[target] < 0:
                    drawdowns[target] += 1
            else:
                previous = global_outstanding.pop(observation_id, None)
                if previous is not None and previous != identity:
                    _error("v4 late observation changed its attempt identity")
                if (
                    previous is not None
                    and target in drawdowns
                    and drawdowns[target] < 0
                ):
                    drawdowns[target] += 1
        if is_v6 and outcome == "on_time":
            attempt_start_ns = _uint64(
                observation.get("attempt_start_monotonic_ns"),
                "v6 on-time observation attempt start",
                1,
            )
            deadline_us = _uint64(
                observation.get("deadline_duration_us"),
                "v6 on-time observation deadline",
                1,
            )
            reporter_ns = _uint64(
                observation.get("reporter_monotonic_ns"),
                "v6 on-time observation reporter time",
                1,
            )
            message_type = observation.get("expected_message_type")
            if (
                _integer(observation.get("schema_version"), "v6 observation schema")
                != 3
                or message_type not in {"direct_vote", "aggregate_relay"}
                or observation_id
                != _v6_timeout_observation_id(
                    reporter_id=reporter,
                    observed_replica_id=target,
                    epoch_number=0,
                    tree_id=key[1],
                    epoch_digest=expected_digest,
                    block_hash=key[3],
                    expected_message_type=str(message_type),
                    attempt_start_monotonic_ns=attempt_start_ns,
                    deadline_duration_us=deadline_us,
                )
                or reporter_ns < attempt_start_ns
                or observation.get("response_duration_us")
                != (reporter_ns - attempt_start_ns) // 1_000
                or not _sequence(observation.get("signer_set"), "v6 on-time signers")
            ):
                _error("v6 on-time observation identity or timing drifted")
        if outcome == "on_time":
            continue
        if is_v6:
            message_type = observation.get("expected_message_type")
            if message_type not in {"direct_vote", "aggregate_relay"}:
                continue
            if (
                _integer(observation.get("schema_version"), "v6 observation schema")
                != 3
            ):
                continue
            attempt_start_ns = _uint64(
                observation.get("attempt_start_monotonic_ns"),
                "v6 observation attempt start",
                1,
            )
            deadline_us = _uint64(
                observation.get("deadline_duration_us"),
                "v6 observation deadline",
                1,
            )
            reporter_ns = _uint64(
                observation.get("reporter_monotonic_ns"),
                "v6 observation reporter time",
                1,
            )
            if attempt_start_ns < start_ns or key[1] not in prefix:
                continue
            if deadline_us > ((1 << 64) - 1) // 1_000:
                _error("v6 observation deadline overflows nanoseconds")
            deadline_ns = deadline_us * 1_000
            if (
                attempt_start_ns > (1 << 64) - 1 - deadline_ns
                or attempt_start_ns + deadline_ns > reporter_ns
                or observation_id
                != _v6_timeout_observation_id(
                    reporter_id=reporter,
                    observed_replica_id=target,
                    epoch_number=0,
                    tree_id=key[1],
                    epoch_digest=expected_digest,
                    block_hash=key[3],
                    expected_message_type=str(message_type),
                    attempt_start_monotonic_ns=attempt_start_ns,
                    deadline_duration_us=deadline_us,
                )
            ):
                _error("v6 exact timeout observation identity or timing drifted")
            signer_set = _sequence(
                observation.get("signer_set"), "v6 observation signers"
            )
            if outcome == "timeout" and (
                observation.get("response_duration_us") != 0 or signer_set
            ):
                _error("v6 timeout observation outcome timing drifted")
            if outcome == "late" and (
                observation.get("response_duration_us")
                != (reporter_ns - attempt_start_ns) // 1_000
                or not signer_set
            ):
                _error("v6 late observation outcome timing drifted")
        elif key not in anchored_keys:
            continue
        if outcome == "timeout":
            if (
                observation_id in filtered_outstanding
                or observation_id in filtered_completed
            ):
                _error("v4 filtered timeout observation ID is reused")
            filtered_outstanding[observation_id] = (
                reporter,
                target,
                key,
                max(
                    _integer(event["source_monotonic_ns"], "evidence acceptance time"),
                    _integer(
                        observation.get("reporter_monotonic_ns"),
                        "evidence reporter time",
                    ),
                ),
                str(expected_message_type),
            )
            continue
        previous = filtered_outstanding.pop(observation_id, None)
        if previous is None:
            # A timeout can legitimately originate before the suffix cutoff;
            # the late observation is then irrelevant to this filtered guard.
            continue
        if previous[:3] != identity:
            _error("v4 late observation does not exactly compensate its timeout")
        filtered_completed.add(observation_id)

    rows: list[dict[str, object]] = []
    for (
        reporter,
        target,
        key,
        timestamp,
        expected_message_type,
    ) in filtered_outstanding.values():
        if (
            (is_v9 and target not in global_drawdowns)
            or (
                is_v9
                and not _is_v9_guard_relation(
                    contract,
                    target=target,
                    reporter=reporter,
                    tree_id=key[1],
                    message_type=expected_message_type,
                    prefix=prefix,
                )
            )
            or (
                is_v8
                and (key[1], expected_message_type)
                not in proof_bound_relations.get(target, {}).get(reporter, set())
            )
            or (not is_v8 and expected_trees.get((target, reporter)) != key[1])
        ):
            continue
        row: dict[str, object] = {
            "epoch_number": 0,
            "tree_id": key[1],
            "observed_replica_id": target,
            "reporter_id": reporter,
            "outcome": "timeout",
            "compensated": False,
            "source_monotonic_ns": timestamp,
        }
        if is_v8:
            row["expected_message_type"] = expected_message_type
        rows.append(row)
    return (
        rows,
        {str(target): drawdowns[target] for target in sorted(drawdowns)},
        max((int(row["source_monotonic_ns"]) for row in rows), default=0),
    )


def _fcrash_h_witness_from_events(
    contract: Mapping[str, object],
    events: Sequence[Mapping[str, Any]],
    fault_receipt: Mapping[str, Any],
    activations1: Sequence[Mapping[str, Any]],
    activations2: Sequence[Mapping[str, Any]],
    *,
    guarded_targets: Sequence[int] | None = None,
) -> dict[str, object]:
    coverage = _mapping(
        contract.get("reporter_coverage_plan"), "reporter coverage plan"
    )
    confirmations = [
        _integer(
            _mapping(outcome, "SIGKILL outcome").get("confirmed_monotonic_ns"),
            "fault confirmation",
        )
        for outcome in _sequence(
            fault_receipt.get("sigkill_outcomes"), "SIGKILL outcomes"
        )
    ]
    fault_ns = max(confirmations)
    prefault_ns = min(
        _integer(
            _mapping(outcome, "SIGKILL outcome").get("requested_monotonic_ns"),
            "fault request",
        )
        for outcome in _sequence(
            fault_receipt.get("sigkill_outcomes"), "SIGKILL outcomes"
        )
    )
    audits = [
        event
        for event in events
        if event["source_kind"] == "adaptation_manager"
        and event["source_id"] == "adaptive-manager"
        and event["event_type"] == "adaptive_v2_evidence_snapshot"
        and _mapping(event["payload"], "snapshot audit").get("predecessor_epoch_number")
        == 0
    ]
    if len(audits) != 1:
        _error("FCRASH-H lacks one predecessor-0 native snapshot audit")
    cutoff = _integer(
        _mapping(audits[0]["payload"], "snapshot audit").get("current_cutoff"),
        "snapshot current cutoff",
        1,
    )
    baseline_cutoff = _integer(
        _mapping(audits[0]["payload"], "snapshot audit").get("baseline_cutoff"),
        "snapshot baseline cutoff",
    )
    if _is_v4_contract(contract):
        rows, guard_drawdowns, nonresponse_ns = _v4_replay_fault_window_anchors(
            contract,
            events,
            baseline_cutoff=baseline_cutoff,
            current_cutoff=cutoff,
            audit=audits[0],
            guarded_targets=guarded_targets,
        )
        if not rows:
            _error("FCRASH-H contains no qualifying timeout evidence")
        witness = {
            "fault_monotonic_ns": fault_ns,
            "nonresponse_monotonic_ns": nonresponse_ns,
            "snapshot_audit_monotonic_ns": _integer(
                audits[0]["source_monotonic_ns"], "snapshot audit timestamp"
            ),
            "epoch1_activation_monotonic_ns": max(
                _integer(event["source_monotonic_ns"], "Epoch 1 activation time")
                for event in activations1
            ),
            "epoch2_activation_monotonic_ns": (
                None
                if not activations2
                else max(
                    _integer(event["source_monotonic_ns"], "Epoch 2 activation time")
                    for event in activations2
                )
            ),
            "timeout_observations": rows,
            "guard_drawdowns": guard_drawdowns,
        }
        if _is_v9_contract(contract):
            if guarded_targets is None:
                _error("v9 witness lacks its guarded cohort")
            witness["guarded_nonresponsive_replica_ids"] = list(guarded_targets)
        if (
            _is_v6_contract(contract)
            or _is_v7_contract(contract)
            or _is_v8_or_v9_contract(contract)
        ):
            witness["eligible_guard_drawdowns"] = {
                str(target): -sum(row["observed_replica_id"] == target for row in rows)
                for target in (
                    tuple(guarded_targets)
                    if _is_v9_contract(contract) and guarded_targets is not None
                    else tuple(contract["targets"])
                )
            }
        if coverage.get("required_postfault_tree_positions") is not None:
            witness["postfault_progress"] = _fcrash_h_postfault_progress(
                contract,
                events,
                fault_ns=fault_ns,
                prefault_ns=prefault_ns,
                audit_ns=_integer(
                    audits[0]["source_monotonic_ns"], "snapshot audit timestamp"
                ),
            )
        return witness
    accepted: list[tuple[int, Mapping[str, Any]]] = []
    latest: dict[str, tuple[int, Mapping[str, Any], int, int]] = {}
    for event in events:
        if (
            event["source_kind"] != "adaptation_manager"
            or event["source_id"] != "adaptive-manager"
            or event["event_type"] != "evidence.observation_accepted"
        ):
            continue
        payload = _mapping(event["payload"], "accepted evidence")
        sequence = _integer(
            payload.get("ingestion_sequence"), "evidence ingestion sequence", 1
        )
        if sequence <= baseline_cutoff or sequence > cutoff:
            continue
        observation = _mapping(payload.get("observation"), "accepted observation")
        configuration = _mapping(
            observation.get("configuration"), "observation configuration"
        )
        if (
            configuration.get("epoch_number") != 0
            or configuration.get("epoch_digest") != contract["epoch_zero_digest"]
        ):
            continue
        observation_id = observation.get("observation_id")
        if not isinstance(observation_id, str):
            _error("accepted observation ID is malformed")
        previous = latest.get(observation_id)
        if previous is not None and sequence <= previous[0]:
            _error("accepted observation transition regressed")
        latest[observation_id] = (
            sequence,
            observation,
            _integer(event["source_monotonic_ns"], "evidence acceptance time"),
            _integer(
                observation.get("reporter_monotonic_ns"),
                "evidence reporter time",
            ),
        )
        accepted.append((sequence, observation))
    expected = {
        int(row["target_replica_id"]): {
            _integer(reporter["reporter_id"], "coverage reporter"): _integer(
                reporter["tree_id"], "coverage first qualifying tree"
            )
            for reporter in _sequence(
                row["first_qualifying_reporters"], "first qualifying reporters"
            )
        }
        for row in _sequence(coverage.get("targets"), "coverage targets")
    }
    rows: list[dict[str, object]] = []
    drawdowns = {target: 0 for target in expected}
    outstanding: dict[str, tuple[int, int]] = {}
    for _sequence_number, observation in sorted(accepted, key=lambda row: row[0]):
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
    for (
        _ingestion_sequence,
        observation,
        accepted_ns,
        reporter_ns,
    ) in latest.values():
        target = observation.get("observed_replica_id")
        reporter = observation.get("reporter_id")
        if (
            observation.get("outcome") != "timeout"
            or type(target) is not int
            or type(reporter) is not int
            or target not in expected
            or reporter not in expected[target]
            or _integer(
                _mapping(
                    observation.get("configuration"), "observation configuration"
                ).get("tree_id"),
                "observation tree",
            )
            != expected[target][reporter]
        ):
            continue
        rows.append(
            {
                "epoch_number": 0,
                "tree_id": _integer(
                    _mapping(
                        observation.get("configuration"),
                        "observation configuration",
                    ).get("tree_id"),
                    "observation tree",
                ),
                "observed_replica_id": target,
                "reporter_id": reporter,
                "outcome": "timeout",
                "compensated": False,
                "source_monotonic_ns": max(accepted_ns, reporter_ns),
            }
        )
    if not rows:
        _error("FCRASH-H contains no qualifying timeout evidence")
    witness = {
        "fault_monotonic_ns": fault_ns,
        "nonresponse_monotonic_ns": max(
            int(row["source_monotonic_ns"]) for row in rows
        ),
        "snapshot_audit_monotonic_ns": _integer(
            audits[0]["source_monotonic_ns"], "snapshot audit timestamp"
        ),
        "epoch1_activation_monotonic_ns": max(
            _integer(event["source_monotonic_ns"], "Epoch 1 activation time")
            for event in activations1
        ),
        "epoch2_activation_monotonic_ns": (
            None
            if not activations2
            else max(
                _integer(event["source_monotonic_ns"], "Epoch 2 activation time")
                for event in activations2
            )
        ),
        "timeout_observations": rows,
        "guard_drawdowns": {
            str(target): drawdown for target, drawdown in sorted(drawdowns.items())
        },
    }
    if coverage.get("required_postfault_tree_positions") is not None:
        witness["postfault_progress"] = _fcrash_h_postfault_progress(
            contract,
            events,
            fault_ns=fault_ns,
            prefault_ns=prefault_ns,
            audit_ns=_integer(
                audits[0]["source_monotonic_ns"], "snapshot audit timestamp"
            ),
        )
    return witness


def _validate_prefault_active_configuration(
    contract: Mapping[str, object],
    events: Sequence[Mapping[str, Any]],
    fault_ns: int,
) -> None:
    latest: dict[int, Mapping[str, Any]] = {}
    for event in events:
        source_id = str(event["source_id"])
        if (
            event["source_kind"] != "replica"
            or not source_id.startswith("replica-")
            or event["event_type"] != "adaptive.configuration_active"
            or int(event["source_monotonic_ns"]) >= fault_ns
        ):
            continue
        replica = int(source_id.removeprefix("replica-"))
        previous = latest.get(replica)
        if previous is None or (
            _integer(event["source_sequence"], "configuration sequence", 1),
            _integer(event["source_monotonic_ns"], "configuration timestamp"),
        ) > (
            _integer(previous["source_sequence"], "configuration sequence", 1),
            _integer(previous["source_monotonic_ns"], "configuration timestamp"),
        ):
            latest[replica] = event
    members = tuple(int(member) for member in contract["members"])
    if set(latest) != set(members):
        _error("FCRASH-H pre-fault active configuration lacks every member")
    for replica in members:
        payload = _mapping(latest[replica]["payload"], "active configuration")
        if {
            key: payload.get(key) for key in ("epoch_number", "tree_id", "epoch_digest")
        } != {
            "epoch_number": 0,
            "tree_id": _mapping(
                contract["reporter_coverage_plan"], "reporter coverage plan"
            )["active_tree_id"],
            "epoch_digest": contract["epoch_zero_digest"],
        }:
            _error("FCRASH-H pre-fault active configuration drifted")


def validation_contract_from_profile(root: Path) -> dict[str, object]:
    """Derive the independent N7/N31 contract from sealed frozen inputs."""

    profile = _read_json(root / "profile.json", "focused profile")
    schema_version = profile.get("schema_version")
    expected_keys = (
        _PROFILE_KEYS_V4
        if profile.get("profile_id") in _FAULT_WINDOW_PROFILE_IDS
        else _PROFILE_KEYS_V2 if schema_version == 2 else _PROFILE_KEYS
    )
    if (
        set(profile) != expected_keys
        or schema_version not in {1, 2}
        or profile.get("frozen") is not True
    ):
        _error("focused profile schema or identity drifted")
    profile_id = profile.get("profile_id")
    if profile_id not in _REVIEWED_FOCUSED_PROFILE_IDS:
        _error("focused profile identity is not reviewed")
    campaign = _mapping(profile.get("campaign"), "profile campaign")
    if profile_id in _FCRASH_H_GUARDED_PROFILE_IDS:
        if campaign.get("scientific_support_contract") != {
            "schema_version": 1,
            "domain": "kauri-focused-campaign-scientific-support-v1",
        }:
            _error("v9 scientific-support campaign contract drifted")
    elif "scientific_support_contract" in campaign:
        _error("archived campaign profile contains a prospective support contract")
    if profile_id in _FAULT_WINDOW_PROFILE_IDS:
        arm_metadata = _mapping(
            profile.get("fault_window_arm"), "fault-window arm metadata"
        )
        blinding_metadata = _mapping(profile.get("blinding"), "profile blinding")
        positions = _integer(
            arm_metadata.get("required_postfault_tree_positions"),
            "fault-window metadata tree positions",
            1,
        )
        topology_metadata = _mapping(profile.get("topology"), "profile topology")
        count_metadata = _integer(
            _mapping(profile.get("protocol"), "profile protocol").get("N"),
            "profile replica count",
            1,
        )
        prefix_metadata = arm_metadata.get("ordered_tree_prefix")
        expected_arm_keys = {
            "schema_version",
            "domain",
            "manager_visibility",
            "ordered_tree_prefix",
            "required_for_new_executions",
            "required_postfault_tree_positions",
        }
        if profile_id in (
            _FCRASH_H_V6_PROFILE_IDS
            | _FCRASH_H_V7_PROFILE_IDS
            | _FCRASH_H_V8_PROFILE_IDS
            | _FCRASH_H_GUARDED_PROFILE_IDS
        ):
            expected_arm_keys |= {
                "clock_domain",
                "required_observation_schema",
                "timeout_evidence_basis",
            }
        if profile_id in (
            _FCRASH_H_V7_PROFILE_IDS
            | _FCRASH_H_V8_PROFILE_IDS
            | _FCRASH_H_GUARDED_PROFILE_IDS
        ):
            expected_arm_keys.add("snapshot_evidence_basis")
        if profile_id in _FCRASH_H_GUARDED_PROFILE_IDS:
            expected_arm_keys.add("selection_cardinality_policy")
        if (
            set(arm_metadata) != expected_arm_keys
            or type(arm_metadata.get("schema_version")) is not int
            or arm_metadata.get("schema_version")
            != (
                4
                if profile_id in _FCRASH_H_GUARDED_PROFILE_IDS
                else (
                    3
                    if profile_id in _FCRASH_H_V7_PROFILE_IDS | _FCRASH_H_V8_PROFILE_IDS
                    else 2 if profile_id in _FCRASH_H_V6_PROFILE_IDS else 1
                )
            )
            or arm_metadata.get("domain") != "epoch_zero_native_cyclic_tree_positions"
            or arm_metadata.get("manager_visibility")
            != "target-identity/process-state blind; intervention-boundary aware"
            or arm_metadata.get("required_for_new_executions") is not True
            or blinding_metadata.get("manager_input_source")
            != "authenticated_runtime_evidence_plus_bound_fault_window_arm"
            or positions > count_metadata
            or not isinstance(prefix_metadata, list)
            or any(type(tree) is not int for tree in prefix_metadata)
            or len(prefix_metadata) != len(set(prefix_metadata))
            or prefix_metadata
            != [
                (int(topology_metadata["active_tree_id"]) + offset) % count_metadata
                for offset in range(positions)
            ]
        ):
            _error("fault-window arm metadata drifted")
        if profile_id in (
            _FCRASH_H_V6_PROFILE_IDS
            | _FCRASH_H_V7_PROFILE_IDS
            | _FCRASH_H_V8_PROFILE_IDS
            | _FCRASH_H_GUARDED_PROFILE_IDS
        ) and (
            arm_metadata.get("clock_domain") != "same_host_clock_monotonic_raw"
            or arm_metadata.get("required_observation_schema") != 3
            or arm_metadata.get("timeout_evidence_basis")
            != "exact_timeout_attempt_id_v1"
        ):
            _error("v6 fault-window timeout evidence metadata drifted")
        if (
            profile_id
            in _FCRASH_H_V7_PROFILE_IDS
            | _FCRASH_H_V8_PROFILE_IDS
            | _FCRASH_H_GUARDED_PROFILE_IDS
            and arm_metadata.get("snapshot_evidence_basis")
            != "exact_post_fault_attempt_start_v1"
        ):
            _error("v7 fault-window snapshot evidence metadata drifted")
        if (
            profile_id in _FCRASH_H_GUARDED_PROFILE_IDS
            and arm_metadata.get("selection_cardinality_policy")
            != "all_guarded_up_to_fault_bound_v1"
        ):
            _error("v9 fault-window selection cardinality policy drifted")
    protocol = _mapping(profile.get("protocol"), "profile protocol")
    count = _integer(protocol.get("N"), "profile replica count", 1)
    threshold = _integer(protocol.get("f"), "profile fault threshold")
    quorum = _integer(protocol.get("Q"), "profile quorum", 1)
    fanout = _integer(protocol.get("fanout"), "profile fanout", 1)
    pipeline = _integer(protocol.get("pipeline_stretch"), "profile pipeline stretch", 1)
    if count != 3 * threshold + 1 or quorum != 2 * threshold + 1:
        _error("focused profile protocol identity drifted")
    members = tuple(range(count))
    topology = _mapping(profile.get("topology"), "profile topology")
    targets = tuple(
        _integer(value, "profile target")
        for value in _sequence(
            topology.get("reviewed_target_replica_ids"), "profile targets"
        )
    )
    if (
        not targets
        or len(set(targets)) != len(targets)
        or not set(targets).issubset(members)
    ):
        _error("focused profile topology identity drifted")
    epoch_zero_digest = _digest(
        topology.get("epoch_zero_digest"), "profile epoch-zero digest"
    )
    active_tree = _integer(topology.get("active_tree_id"), "active tree")
    if active_tree not in members:
        _error("active tree is outside membership")
    measurement = _mapping(profile.get("measurement"), "profile measurement")
    expected_measurement_keys = {
        "authoritative_replica_id",
        "bucket_width_seconds",
        "commit_event_type",
        "phase_names",
    }
    if (
        profile_id
        in _FCRASH_H_V5_PROFILE_IDS
        | _FCRASH_H_V6_PROFILE_IDS
        | _FCRASH_H_V7_PROFILE_IDS
        | _FCRASH_H_V8_PROFILE_IDS
        | _FCRASH_H_GUARDED_PROFILE_IDS
    ):
        expected_measurement_keys.add("phase_window_contract")
    if set(measurement) != expected_measurement_keys:
        _error("profile measurement schema drifted")
    phase_window_contract: dict[str, object] | None = None
    if (
        profile_id
        in _FCRASH_H_V5_PROFILE_IDS
        | _FCRASH_H_V6_PROFILE_IDS
        | _FCRASH_H_V7_PROFILE_IDS
        | _FCRASH_H_V8_PROFILE_IDS
        | _FCRASH_H_GUARDED_PROFILE_IDS
    ):
        raw_phase_contract = _mapping(
            measurement.get("phase_window_contract"), "phase-window contract"
        )
        if (
            set(raw_phase_contract)
            != {
                "schema_version",
                "domain",
                "stabilization_offset_seconds",
                "control_optimization_hold_seconds",
            }
            or raw_phase_contract.get("schema_version") != 1
            or raw_phase_contract.get("domain")
            != "kauri-focused-causal-phase-windows-v1"
            or _integer(
                raw_phase_contract.get("stabilization_offset_seconds"),
                "phase stabilization offset",
                1,
            )
            != 30
            or _integer(
                raw_phase_contract.get("control_optimization_hold_seconds"),
                "control optimization hold",
                1,
            )
            != 30
        ):
            _error("phase-window contract drifted")
        phase_window_contract = dict(raw_phase_contract)
    observer = _integer(
        measurement.get("authoritative_replica_id"), "authoritative observer"
    )
    if observer not in members or observer in targets:
        _error("authoritative observer must be a survivor")
    fault = _mapping(profile.get("fault"), "profile fault")
    transitions = _mapping(profile.get("transitions"), "profile transitions")
    activation_readiness_contract: dict[str, object] | None = None
    if profile_id in _FCRASH_H_V13_PROFILE_IDS:
        activation_readiness_contract = _v13_certified_activation_contract(profile)
    elif "activation_readiness_contract" in transitions:
        _error("archived profile contains a prospective readiness contract")
    timing = transitions.get("adaptive_timing_contract")
    if profile_id in (
        _FCRASH_H_V10_PROFILE_IDS
        | _FCRASH_H_V11_PROFILE_IDS
        | _FCRASH_H_V12_PROFILE_IDS
        | _FCRASH_H_V13_PROFILE_IDS
    ):
        expected_timing = {
            "schema_version": 1,
            "domain": "kauri-focused-v10-transition-timing-v1",
            "epoch1_common_commit_anchor_deadline_seconds": 5,
            "optimization_minimum_predecessor_residency_ms": 65_000,
        }
        if timing != expected_timing:
            _error("v10 transition timing contract drifted")
        if (
            _integer(measurement.get("bucket_width_seconds"), "bucket width", 1)
            != expected_timing["epoch1_common_commit_anchor_deadline_seconds"]
            or expected_timing["optimization_minimum_predecessor_residency_ms"]
            != (
                _integer(
                    _mapping(
                        measurement.get("phase_window_contract"),
                        "phase-window contract",
                    ).get("stabilization_offset_seconds"),
                    "phase stabilization offset",
                    1,
                )
                + _integer(
                    _mapping(profile.get("timers"), "profile timers").get(
                        "stable_phase_seconds"
                    ),
                    "stable phase",
                    1,
                )
                + _integer(
                    measurement.get("bucket_width_seconds"), "bucket width", 1
                )
            )
            * 1_000
        ):
            _error("v10 transition timing contract is not phase-derived")
    elif timing is not None:
        _error("archived profile contains a prospective transition timing contract")
    if (
        fault.get("target_count") != len(targets)
        or transitions.get("common_commit_quorum") != quorum
        or transitions.get("survivor_barrier_count") != count - len(targets)
    ):
        _error("profile fault or transition cardinality drifted")
    relative = topology.get("proof_path")
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        _error("topology proof path is unsafe")
    proof_path = root / relative
    if proof_path.is_symlink() or not proof_path.is_file():
        _error("topology proof is absent")
    proof_bytes = proof_path.read_bytes()
    proof_sha = _digest(topology.get("proof_sha256"), "topology proof digest")
    if _sha_bytes(proof_bytes) != proof_sha:
        _error("topology proof bytes drifted")
    profile_sha = _hash(_profile_identity(profile))
    proof = _mapping(json.loads(proof_bytes), "topology proof")
    proof_keys = {
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
    is_v7_n31 = profile_id == "n31-f5-q21-three-crash-pair-v7"
    is_v8_n31 = profile_id in {
        "n31-f5-q21-three-crash-pair-v8",
        "n31-f5-q21-three-crash-pair-v9",
        "n31-f5-q21-three-crash-pair-v10",
        "n31-f5-q21-three-crash-pair-v11",
        "n31-f5-q21-three-crash-pair-v12",
        "n31-f5-q21-three-crash-pair-v13",
    }
    order = [members[(active_tree + offset) % count] for offset in range(count)]
    if (
        proof.get("source") != "native_epoch_profile_digest"
        or proof.get("profile_sha256") != profile_sha
        or proof.get("epoch_zero_digest") != epoch_zero_digest
        or proof.get("active_tree_id") != active_tree
        or proof.get("fanout") != fanout
        or set(proof) != proof_keys
        or proof.get("schema_version") != 1
        or proof.get("profile_id") != profile.get("profile_id")
        or proof.get("root_replica_id") != active_tree
        or proof.get("bfs_member_order") != order
        or _mapping(proof.get("target_derivation"), "target derivation").get(
            "selected_target_replica_ids"
        )
        != list(targets)
    ):
        _error("topology proof is not bound to the native focused tree")
    if is_v7_n31 or is_v8_n31:
        metric = (
            _v12_n31_target_selection_metric()
            if profile_id
            in {
                "n31-f5-q21-three-crash-pair-v12",
                "n31-f5-q21-three-crash-pair-v13",
            }
            else (
                _v8_n31_target_selection_metric()
                if is_v8_n31
                else _v7_n31_target_selection_metric()
            )
        )
        if (
            topology.get("target_selection_metric") != metric
            or _mapping(proof.get("target_derivation"), "target derivation").get(
                "target_selection_metric"
            )
            != metric
        ):
            _error("reviewed topology-only target selection metric drifted")
    elif "target_selection_metric" in topology or "target_selection_metric" in _mapping(
        proof.get("target_derivation"), "target derivation"
    ):
        _error("archived topology contains a prospective target selection metric")
    if _is_v8_or_v9_contract({"profile_id": profile_id}):
        arm = _mapping(profile.get("fault_window_arm"), "fault-window arm")
        capacity = _v8_reporter_capacity_document(
            replica_count=count,
            fanout=fanout,
            targets=targets,
            prefix=_sequence(arm.get("ordered_tree_prefix"), "fault-window prefix"),
        )
        derivation = _mapping(proof.get("target_derivation"), "target derivation")
        guard = _mapping(profile.get("evidence_guard"), "evidence guard")
        if (
            topology.get("reporter_coverage_capacity") != capacity
            or derivation.get("reporter_coverage_capacity") != capacity
            or guard.get("reporter_selection_basis")
            != capacity["reporter_selection_basis"]
            or guard.get("minimum_topology_eligible_reporter_capacity")
            != capacity["minimum_topology_eligible_reporter_capacity"]
        ):
            _error("v8 topology reporter capacity drifted")
        if profile_id in {
            "n31-f5-q21-three-crash-pair-v12",
            "n31-f5-q21-three-crash-pair-v13",
        }:
            all_capacity = _v12_all_candidate_reporter_capacity_document(
                replica_count=count,
                quorum=quorum,
                fanout=fanout,
                unavailable=targets,
                prefix=_sequence(arm.get("ordered_tree_prefix"), "fault-window prefix"),
            )
            if (
                topology.get("all_candidate_reporter_coverage_capacity")
                != all_capacity
                or derivation.get("all_candidate_reporter_coverage_capacity")
                != all_capacity
                or all_capacity["minimum_topology_eligible_reporter_capacity"] != 19
                or all_capacity["maximum_topology_eligible_reporter_capacity"] != 23
                or all_capacity["maximum_guarded_cohort_size"] != 10
                or all_capacity["minimum_remaining_reporter_capacity"] != 13
                or all_capacity[
                    "minimum_injected_target_remaining_reporter_capacity"
                ]
                != 16
            ):
                _error("v12 all-candidate reporter capacity drifted")
    if (
        profile_id
        in _FCRASH_H_V5_PROFILE_IDS
        | _FCRASH_H_V6_PROFILE_IDS
        | _FCRASH_H_V7_PROFILE_IDS
        | _FCRASH_H_V8_PROFILE_IDS
        | _FCRASH_H_GUARDED_PROFILE_IDS
        and (
            profile_sha,
            proof_sha,
        )
        != (
            _FCRASH_H_V13_IDENTITIES.get(str(profile_id))
            or _FCRASH_H_V11_IDENTITIES.get(str(profile_id))
            or _FCRASH_H_V12_IDENTITIES.get(str(profile_id))
            or _FCRASH_H_V10_IDENTITIES.get(str(profile_id))
            or _FCRASH_H_V9_IDENTITIES.get(str(profile_id))
            or _FCRASH_H_V8_IDENTITIES.get(str(profile_id))
            or _FCRASH_H_V7_IDENTITIES.get(str(profile_id))
            or _FCRASH_H_V6_IDENTITIES.get(str(profile_id))
            or _FCRASH_H_V5_IDENTITIES[str(profile_id)]
        )
    ):
        _error("reviewed profile or topology proof is not the frozen reviewed identity")
    children = {
        index: tuple(
            child
            for child in range(index * fanout + 1, index * fanout + fanout + 1)
            if child < count
        )
        for index in members
    }

    def subtree(index: int) -> tuple[int, ...]:
        return tuple(
            member
            for child in children[index]
            for member in (order[child], *subtree(child))
        )

    depths = [0] * count
    for index in range(1, count):
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
    expected_descendants = {
        str(order[index]): list(subtree(index))
        for index, child_ids in children.items()
        if child_ids
    }
    nonroot_internal = [
        index for index, child_ids in children.items() if index and child_ids
    ]
    deepest_depth = max(depths[index] for index in nonroot_internal)
    deepest = [
        order[index] for index in nonroot_internal if depths[index] == deepest_depth
    ]
    target_descendants = [set(expected_descendants[str(target)]) for target in targets]
    disjoint = all(
        left.isdisjoint(right)
        for position, left in enumerate(target_descendants)
        for right in target_descendants[position + 1 :]
    )
    if (
        proof.get("members") != expected_members
        or proof.get("internal_descendant_sets") != expected_descendants
        or proof.get("target_derivation")
        != {
            "deepest_member_ids": deepest,
            "selected_target_replica_ids": list(targets),
            "pairwise_disjoint": True,
            **(
                {"target_selection_metric": _v7_n31_target_selection_metric()}
                if is_v7_n31
                else (
                    {
                        "target_selection_metric": (
                            _v12_n31_target_selection_metric()
                            if profile_id
                            in {
                                "n31-f5-q21-three-crash-pair-v12",
                                "n31-f5-q21-three-crash-pair-v13",
                            }
                            else _v8_n31_target_selection_metric()
                        )
                    }
                    if is_v8_n31
                    else {}
                )
            ),
            **(
                {
                    "reporter_coverage_capacity": _v8_reporter_capacity_document(
                        replica_count=count,
                        fanout=fanout,
                        targets=targets,
                        prefix=_sequence(
                            _mapping(
                                profile.get("fault_window_arm"), "fault-window arm"
                            ).get("ordered_tree_prefix"),
                            "fault-window prefix",
                        ),
                    )
                }
                if is_v8_n31
                or profile_id
                in {
                    "n7-f2-q5-two-crash-pair-smoke-v8",
                    "n7-f2-q5-two-crash-pair-smoke-v9",
                    "n7-f2-q5-two-crash-pair-smoke-v10",
                    "n7-f2-q5-two-crash-pair-smoke-v11",
                    "n7-f2-q5-two-crash-pair-smoke-v12",
                    "n7-f2-q5-two-crash-pair-smoke-v13",
                }
                else {}
            ),
            **(
                {
                    "all_candidate_reporter_coverage_capacity": (
                        _v12_all_candidate_reporter_capacity_document(
                            replica_count=count,
                            quorum=quorum,
                            fanout=fanout,
                            unavailable=targets,
                            prefix=_sequence(
                                _mapping(
                                    profile.get("fault_window_arm"),
                                    "fault-window arm",
                                ).get("ordered_tree_prefix"),
                                "fault-window prefix",
                            ),
                        )
                    )
                }
                if profile_id
                in {
                    "n31-f5-q21-three-crash-pair-v12",
                    "n31-f5-q21-three-crash-pair-v13",
                }
                else {}
            ),
        }
        or not disjoint
    ):
        _error("topology proof roles, depths, or descendants drifted")
    survivors = tuple(member for member in members if member not in targets)
    result: dict[str, object] = {
        "profile": profile,
        "profile_sha256": profile_sha,
        "topology_proof_sha256": proof_sha,
        "profile_id": profile_id,
        "members": members,
        "quorum": quorum,
        "fault_threshold": threshold,
        "fanout": fanout,
        "pipeline_stretch": pipeline,
        "targets": targets,
        "fault_target_count": len(targets),
        "manager_blinding_target_count": len(targets),
        "survivor_barrier_count": len(survivors),
        "survivors": survivors,
        "control_transition_count": len(
            _sequence(transitions.get("control"), "control transitions")
        ),
        "adaptive_transition_count": len(
            _sequence(transitions.get("adaptive"), "adaptive transitions")
        ),
        "authoritative_replica_id": observer,
        "authoritative_source_id": f"replica-{observer}",
        "epoch_zero_digest": epoch_zero_digest,
        "phase_names": tuple(
            _sequence(measurement.get("phase_names"), "measurement phases")
        ),
        "bucket_width_seconds": _integer(
            measurement.get("bucket_width_seconds"), "bucket width", 1
        ),
        "transactions_per_block": _integer(
            protocol.get("transactions_per_block"), "transactions per block", 1
        ),
        "figure_eligible": profile.get("figure_eligible") is True,
    }
    if phase_window_contract is not None:
        result["phase_window_contract"] = phase_window_contract
    if activation_readiness_contract is not None:
        result["protocol_mode"] = "adaptive_v3"
        result["activation_readiness_contract"] = activation_readiness_contract
    if schema_version == 2:
        result["reporter_coverage_plan"] = _derive_reporter_coverage_plan(
            profile,
            members=members,
            targets=targets,
            fault_threshold=threshold,
            quorum=quorum,
            fanout=fanout,
            active_tree=active_tree,
        )
    return result


def _expected_treegen_payload(contract: Mapping[str, object]) -> bytes:
    members = tuple(int(member) for member in contract["members"])
    fanout = int(contract["fanout"])
    pipeline = int(contract["pipeline_stretch"])
    lines = [
        " ".join(
            (
                f"fan:{fanout}",
                f"pipe:{pipeline}",
                *(str(replica) for replica in members[offset:] + members[:offset]),
            )
        )
        for offset in range(len(members))
    ]
    return ("\n".join(lines) + "\n").encode("ascii")


def _expected_leader_progress_timeout(profile_id: object) -> str:
    # The profile argument is retained so legacy and v13 call sites share the
    # same explicit source-blind binding surface.
    del profile_id
    return "8.0"


def _validate_runtime_configuration(root: Path, contract: Mapping[str, object]) -> None:
    profile_id = _mapping(contract.get("profile"), "focused profile").get(
        "profile_id"
    )
    is_v13 = profile_id in _FCRASH_H_V13_PROFILE_IDS
    treegen_path = root / "treegen.conf"
    if treegen_path.is_symlink() or not treegen_path.is_file():
        _error("client tree configuration is absent")
    if treegen_path.read_bytes() != _expected_treegen_payload(contract):
        _error("client tree configuration differs from the frozen topology")

    main_path = root / "config" / "main.conf"
    if main_path.is_symlink() or not main_path.is_file():
        _error("main runtime configuration is absent")
    try:
        payload = main_path.read_bytes().decode("ascii")
    except UnicodeDecodeError as exc:
        raise FocusedCrashPairValidationError(
            "main runtime configuration is not canonical ASCII"
        ) from exc
    if not payload.endswith("\n"):
        _error("main runtime configuration is not newline terminated")
    options: dict[str, list[str]] = {}
    for line in payload.splitlines():
        if " = " not in line:
            _error("main runtime configuration contains a malformed line")
        key, value = line.split(" = ", 1)
        normalized_key = key.strip()
        normalized_value = value.strip()
        if (
            not normalized_key
            or not normalized_value
            or line != f"{normalized_key} = {normalized_value}"
        ):
            _error("main runtime configuration is not canonical")
        key, value = normalized_key, normalized_value
        options.setdefault(key, []).append(value)
    if set(options) != _MAIN_CONFIG_KEYS:
        _error("main runtime configuration key set drifted")
    if any(key != "replica" and len(values) != 1 for key, values in options.items()):
        _error("main runtime configuration duplicates a singleton option")
    required = {
        "block-size": str(contract["transactions_per_block"]),
        "fan-out": str(contract["fanout"]),
        "async_blocks": str(contract["pipeline_stretch"]),
        "aggregation-timeout": "1.0",
        "leader-progress-timeout": _expected_leader_progress_timeout(profile_id),
        "leader-activation-grace": "1.0",
        "tree-generation": "default",
        "tree-switch-period": str(
            2
            if _mapping(contract.get("profile"), "focused profile").get("profile_id")
            in _FCRASH_H_V3_PROFILE_IDS | _FAULT_WINDOW_PROFILE_IDS
            else len(tuple(contract["members"]))
        ),
        "epoch-protocol-mode": "adaptive_v3" if is_v13 else "adaptive_v2",
        "epoch-change-minimum-activation-delay": "5",
        "epoch-change-maximum-activation-delay": "5",
    }
    if is_v13:
        required["max-rep-msg"] = str(_V13_MAX_REPLICA_MESSAGE_BYTES)
    if any(options.get(key) != [value] for key, value in required.items()):
        _error("main runtime topology or timer configuration drifted")
    if {"conf", "default_epoch"}.intersection(options):
        _error("main runtime overrides the sealed client tree configuration")
    if len(options.get("replica", ())) != len(tuple(contract["members"])):
        _error("main runtime replica membership cardinality drifted")
    if (
        _is_v6_contract(contract)
        or _is_v7_contract(contract)
        or _is_v8_or_v9_contract(contract)
    ):
        for replica in tuple(contract["members"]):
            replica_path = root / "config" / f"replica-{replica}.conf"
            if replica_path.is_symlink() or not replica_path.is_file():
                _error("v6 replica timeout-attempt evidence configuration is absent")
            try:
                replica_payload = replica_path.read_bytes().decode("ascii")
            except UnicodeDecodeError as exc:
                raise FocusedCrashPairValidationError(
                    "v6 replica timeout-attempt evidence configuration is not ASCII"
                ) from exc
            if (
                not replica_payload.endswith("\n")
                or replica_payload.count(
                    "experiment-exact-timeout-attempt-evidence-v3 = true\n"
                )
                != 1
                or not replica_payload.endswith(
                    "experiment-exact-timeout-attempt-evidence-v3 = true\n"
                )
            ):
                _error("v6 replica timeout-attempt evidence configuration drifted")


def _validated_profile(root: Path) -> tuple[Mapping[str, Any], str, str]:
    contract = validation_contract_from_profile(root)
    return (
        _mapping(contract["profile"], "focused profile"),
        str(contract["profile_sha256"]),
        str(contract["topology_proof_sha256"]),
    )


def _read_jsonl(path: Path, source_kind: str) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        _error(f"{source_kind} event stream is absent")
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(), start=1):
        if not line:
            _error(f"{source_kind} event stream contains an empty record")
        try:
            event = _mapping(json.loads(line), f"event line {line_number}")
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise FocusedCrashPairValidationError(
                "event stream is invalid JSONL"
            ) from exc
        if (
            set(event) != _EVENT_KEYS
            or event.get("event_schema_version") != 1
            or event.get("source_kind") != source_kind
            or not isinstance(event.get("run_id"), str)
            or not isinstance(event.get("source_id"), str)
            or not isinstance(event.get("source_instance"), str)
            or not isinstance(event.get("event_type"), str)
            or not isinstance(event.get("payload"), Mapping)
        ):
            _error("runtime event envelope schema or source kind drifted")
        _integer(event.get("source_sequence"), "source sequence", 1)
        _integer(event.get("source_monotonic_ns"), "source timestamp")
        events.append(dict(event))
    return events


def _validate_sources(
    root: Path,
    *,
    require_controller_failure: bool = False,
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    events = [
        *_read_jsonl(root / "raw" / "replica-events.jsonl", "replica"),
        *_read_jsonl(
            root / "raw" / "adaptive-manager-events.jsonl",
            "adaptation_manager",
        ),
        *_read_jsonl(root / "raw" / "client-events.jsonl", "client"),
    ]
    by_source: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for event in events:
        key = (
            str(event["source_kind"]),
            str(event["source_id"]),
            str(event["source_instance"]),
        )
        by_source.setdefault(key, []).append(event)
    if not by_source or len({str(event["run_id"]) for event in events}) != 1:
        _error("raw sources do not belong to one run")
    source_ids: set[tuple[str, str]] = set()
    for (kind, source_id, _instance), source_events in by_source.items():
        if (kind, source_id) in source_ids:
            _error("one source ID spans multiple source instances")
        source_ids.add((kind, source_id))
        sequences = [int(event["source_sequence"]) for event in source_events]
        timestamps = [int(event["source_monotonic_ns"]) for event in source_events]
        if sequences != list(range(1, len(source_events) + 1)):
            _error("source sequence is not contiguous")
        if timestamps != sorted(timestamps):
            _error("source monotonic time regressed")
    inventory = [list(source) for source in sorted(by_source)]
    recorded_sources = [
        list(source)
        for source in sorted({(kind, source_id) for kind, source_id, _ in by_source})
    ]
    recorded_inventory = _read_json(
        root / "runtime" / "source-inventory.json", "source inventory"
    )
    if recorded_inventory.get("sources") != recorded_sources:
        _error("recorded source inventory differs from raw envelopes")
    for event in events:
        if event["event_type"] != "adaptive_v2_session_terminal":
            continue
        payload = _mapping(event["payload"], "manager terminal")
        if require_controller_failure and not _validate_v4_manager_terminal_payload(
            payload
        ):
            _error("manager terminal schema drifted")
        if not _validate_controller_failure_terminal(
            payload, require_for_unhealthy=require_controller_failure
        ):
            _error("manager terminal controller failure detail drifted")
    return events, inventory


_V13_READINESS_EVENT_KEYS = {
    "identity", "replica_id", "signer_source_sequence", "signer_monotonic_raw_ns",
    "observation_digest", "certificate_digest", "payload_digest", "observed_signers",
    "required_release_count", "delivery_attempt", "delivery_enqueued",
    "canonical_wire_payload_hex", "wire_opcode", "wire_payload_size", "disposition",
    "terminal_cycle_ordinal", "terminal_reason", "terminal_identity", "terminal_bundle_digest",
    "e2_cycle_ordinal", "e1_bundle_digest", "e2_final_ack_raw_ns", "e2_common_commit",
    "e2_common_commit_sources", "e2_common_commit_raw_ns", "e2_earliest_raw_ns",
    "e2_actual_begin_raw_ns", "e2_hard_deadline_raw_ns", "e2_reserve_raw_ns",
}
_V13_READINESS_SUCCESS_EVENTS = {
    "epoch.activation_prepared",
    "epoch.activation_ready_signed",
    "adaptive_v3.readiness_observation_retry_exhausted",
    "adaptive_v3.readiness_observation_accepted",
    "adaptive_v3.readiness_certificate_assembled",
    "adaptive_v3.readiness_certificate_delivery",
    "adaptive_v3.readiness_certificate_accepted",
    "adaptive_v3.readiness_certificate_acknowledged",
    "adaptive_v3.e2_eligibility",
    "adaptive_v3.readiness_terminal",
}


def _v13_readiness_wire(payload: Mapping[str, object], label: str) -> bytes:
    value = payload.get("canonical_wire_payload_hex")
    if (
        not isinstance(value, str)
        or not value
        or len(value) % 2 != 0
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _error(f"{label} wire is not canonical lowercase hex")
    return bytes.fromhex(value)


def _v13_readiness_replica_source(
    event: Mapping[str, object], payload: Mapping[str, object]
) -> int:
    source_id = event.get("source_id")
    if (
        event.get("source_kind") != "replica"
        or not isinstance(source_id, str)
        or not source_id.startswith("replica-")
    ):
        _error("v13 readiness event source kind drifted")
    try:
        source = int(source_id.removeprefix("replica-"))
    except ValueError as exc:
        raise FocusedCrashPairValidationError(
            "v13 readiness event source ID drifted"
        ) from exc
    if payload.get("replica_id") != source:
        _error("v13 readiness event replica/source binding drifted")
    return source


def _v13_readiness_no_audit_fields(
    payload: Mapping[str, object], *, allow_e2: bool = False,
    allow_terminal: bool = False,
) -> None:
    e2_fields = (
        "e2_cycle_ordinal", "e1_bundle_digest", "e2_final_ack_raw_ns",
        "e2_common_commit", "e2_common_commit_raw_ns", "e2_earliest_raw_ns",
        "e2_actual_begin_raw_ns", "e2_hard_deadline_raw_ns", "e2_reserve_raw_ns",
    )
    if not allow_e2 and (
        any(payload.get(field) is not None for field in e2_fields)
        or payload.get("e2_common_commit_sources") != []
    ):
        _error("v13 readiness event carries E2-only fields")
    terminal_fields = (
        "terminal_cycle_ordinal", "terminal_reason", "terminal_identity",
        "terminal_bundle_digest",
    )
    if not allow_terminal and any(
        payload.get(field) is not None for field in terminal_fields
    ):
        _error("v13 readiness event carries terminal-only fields")


def _validate_v13_readiness_event_payload(
    event: Mapping[str, object], contract: Mapping[str, object] | None = None,
) -> None:
    """Mirror each native successful v13 readiness event, including its wire."""
    payload = _mapping(event.get("payload"), "v13 readiness event payload")
    if set(payload) != _V13_READINESS_EVENT_KEYS:
        _error("v13 readiness event payload schema drifted")
    name = str(event.get("event_type"))
    if name == "adaptive_v3.readiness_wire_rejected":
        if payload.get("identity") is not None:
            _error("v13 wire rejection identity drifted")
        return

    if name not in _V13_READINESS_SUCCESS_EVENTS:
        _error("v13 readiness event name is not a successful native transition")
    members = tuple(range(31)) if contract is None else tuple(
        _integer(member, "v13 readiness member") for member in contract["members"]
    )
    maximum_members = len(members)
    maximum_payload_bytes = _V13_MAX_REPLICA_MESSAGE_BYTES
    identity, _identity_bytes = _v13_ready_identity_bytes(payload.get("identity"))
    if payload.get("wire_opcode") is not None or payload.get("wire_payload_size") is not None:
        _error("v13 successful readiness event carries wire-rejection fields")
    _v13_readiness_no_audit_fields(
        payload,
        allow_e2=name == "adaptive_v3.e2_eligibility",
        allow_terminal=name == "adaptive_v3.readiness_terminal",
    )

    no_signer = (
        payload.get("signer_source_sequence") is None
        and payload.get("signer_monotonic_raw_ns") is None
    )
    no_collection = (
        payload.get("observed_signers") == []
        and payload.get("required_release_count") == 0
    )

    if name == "epoch.activation_prepared":
        source = _v13_readiness_replica_source(event, payload)
        if source not in members or not no_signer or any(
            payload.get(field) is not None
            for field in ("observation_digest", "certificate_digest", "payload_digest",
                          "canonical_wire_payload_hex")
        ) or not no_collection or payload.get("delivery_attempt") != 0 or payload.get(
            "delivery_enqueued"
        ) is not False or payload.get("disposition") is not None:
            _error("v13 activation-prepared event drifted")
        return

    if name in {
        "epoch.activation_ready_signed",
        "adaptive_v3.readiness_observation_retry_exhausted",
        "adaptive_v3.readiness_observation_accepted",
    }:
        if name in {
            "epoch.activation_ready_signed",
            "adaptive_v3.readiness_observation_retry_exhausted",
        }:
            source = _v13_readiness_replica_source(event, payload)
            allowed_dispositions = (
                {None}
                if name == "epoch.activation_ready_signed"
                else {"retry_exhausted"}
            )
        else:
            if event.get("source_kind") != "adaptation_manager" or event.get(
                "source_id"
            ) != "adaptive-manager":
                _error("v13 observation acceptance source drifted")
            source = _integer(payload.get("replica_id"), "v13 observation replica")
            allowed_dispositions = {"accepted", "duplicate", "released"}
        wire = _v13_readiness_wire(payload, "v13 readiness observation")
        observation = _v13_decode_ready_observation(wire)
        if (
            source not in members
            or observation.get("identity") != identity
            or observation.get("signer_replica_id") != source
            or observation.get("signer_source_sequence")
            != payload.get("signer_source_sequence")
            or observation.get("signer_monotonic_raw_ns")
            != payload.get("signer_monotonic_raw_ns")
            or _v13_observation_signing_digest(observation)
            != _digest(payload.get("observation_digest"), "v13 observation digest")
            or payload.get("certificate_digest") is not None
            or payload.get("payload_digest") is not None
            or not no_collection
            or payload.get("delivery_attempt") != 0
            or payload.get("delivery_enqueued") is not False
            or payload.get("disposition") not in allowed_dispositions
        ):
            _error("v13 readiness observation event drifted")
        return

    if name in {
        "adaptive_v3.readiness_certificate_assembled",
        "adaptive_v3.readiness_certificate_delivery",
        "adaptive_v3.readiness_certificate_accepted",
    }:
        manager_event = name != "adaptive_v3.readiness_certificate_accepted"
        if manager_event:
            if event.get("source_kind") != "adaptation_manager" or event.get(
                "source_id"
            ) != "adaptive-manager":
                _error("v13 readiness certificate manager source drifted")
        else:
            source = _v13_readiness_replica_source(event, payload)
            if source not in members:
                _error("v13 readiness certificate replica source drifted")
        wire = _v13_readiness_wire(payload, "v13 readiness certificate")
        certificate = _v13_decode_readiness_certificate(
            wire,
            maximum_members=maximum_members,
            maximum_payload_bytes=maximum_payload_bytes,
        )
        certificate_signers = [
            _integer(item["signer_replica_id"], "v13 certificate signer")
            for item in certificate["observations"]
        ]
        if (
            not no_signer
            or payload.get("observation_digest") is not None
            or certificate.get("identity") != identity
            or certificate.get("certificate_digest")
            != _digest(payload.get("certificate_digest"), "v13 certificate digest")
            or _v13_ack_payload_digest(_V13_READY_CERTIFICATE_OPCODE, wire)
            != _digest(payload.get("payload_digest"), "v13 certificate payload digest")
        ):
            _error("v13 readiness certificate event wire binding drifted")
        if name == "adaptive_v3.readiness_certificate_assembled":
            if (
                payload.get("replica_id") is not None
                or payload.get("observed_signers") != certificate_signers
                or payload.get("required_release_count") != len(certificate_signers)
                or payload.get("delivery_attempt") != 0
                or payload.get("delivery_enqueued") is not False
                or payload.get("disposition") is not None
            ):
                _error("v13 readiness certificate assembly drifted")
        elif name == "adaptive_v3.readiness_certificate_delivery":
            replica = _integer(payload.get("replica_id"), "v13 delivery replica")
            delivery_enqueued = payload.get("delivery_enqueued")
            disposition = payload.get("disposition")
            if (
                replica not in members
                or not no_collection
                or _integer(payload.get("delivery_attempt"), "v13 delivery attempt", 1) < 1
                or not (
                    (disposition == "queued" and delivery_enqueued is True)
                    or (
                        disposition == "retry_scheduled"
                        and delivery_enqueued is False
                    )
                )
            ):
                _error("v13 bounded certificate delivery drifted")
        elif (
            not no_collection
            or payload.get("delivery_attempt") != 0
            or payload.get("delivery_enqueued") is not False
            or payload.get("disposition") is not None
        ):
            _error("v13 certificate acceptance drifted")
        return

    if name == "adaptive_v3.readiness_certificate_acknowledged":
        if event.get("source_kind") != "adaptation_manager" or event.get(
            "source_id"
        ) != "adaptive-manager":
            _error("v13 readiness ACK source drifted")
        wire = _v13_readiness_wire(payload, "v13 readiness ACK")
        acknowledgement = _v13_decode_readiness_ack(wire)
        replica = _integer(payload.get("replica_id"), "v13 ACK replica")
        if (
            replica not in members
            or not no_signer
            or payload.get("observation_digest") is not None
            or acknowledgement.get("identity") != identity
            or acknowledgement.get("recipient_replica_id") != replica
            or acknowledgement.get("acknowledged_opcode") != _V13_READY_CERTIFICATE_OPCODE
            or acknowledgement.get("disposition") != 1
            or acknowledgement.get("certificate_digest")
            != _digest(payload.get("certificate_digest"), "v13 ACK certificate digest")
            or acknowledgement.get("payload_digest")
            != _digest(payload.get("payload_digest"), "v13 ACK payload digest")
            or not no_collection
            or payload.get("delivery_attempt") != 0
            or payload.get("delivery_enqueued") is not False
            or payload.get("disposition") != "acknowledged"
        ):
            _error("v13 readiness ACK event drifted")
        return

    if name == "adaptive_v3.e2_eligibility":
        if event.get("source_kind") != "adaptation_manager" or event.get(
            "source_id"
        ) != "adaptive-manager":
            _error("v13 E2 eligibility source drifted")
        signers = payload.get("observed_signers")
        common_sources = payload.get("e2_common_commit_sources")
        common = _mapping(payload.get("e2_common_commit"), "v13 E2 common commit")
        if set(common) != {"epoch_number", "tree_id", "epoch_digest", "block_hash"}:
            _error("v13 E2 common commit schema drifted")
        common_configuration, _ = _v13_ready_configuration(
            {key: common[key] for key in ("epoch_number", "tree_id", "epoch_digest")},
            "v13 E2 common commit configuration",
        )
        final_ack = _uint64(payload.get("e2_final_ack_raw_ns"), "v13 E2 final ACK", 1)
        common_tick = _uint64(payload.get("e2_common_commit_raw_ns"), "v13 E2 common tick", 1)
        earliest = _uint64(payload.get("e2_earliest_raw_ns"), "v13 E2 earliest tick", 1)
        actual = _uint64(payload.get("e2_actual_begin_raw_ns"), "v13 E2 begin tick", 1)
        hard = _uint64(payload.get("e2_hard_deadline_raw_ns"), "v13 E2 hard deadline", 1)
        if (
            payload.get("replica_id") is not None
            or not no_signer
            or any(payload.get(field) is not None for field in
                   ("observation_digest", "certificate_digest", "payload_digest",
                    "canonical_wire_payload_hex"))
            or payload.get("delivery_attempt") != 0
            or payload.get("delivery_enqueued") is not False
            or payload.get("disposition") != "eligible"
            or payload.get("e2_cycle_ordinal") != 1
            or _digest(payload.get("e1_bundle_digest"), "v13 E1 bundle digest") == "0" * 64
            or not isinstance(signers, list)
            or any(type(source) is not int or source not in members for source in signers)
            or signers != sorted(signers)
            or len(signers) != len(set(signers))
            or not signers
            or (
                contract is not None
                and len(signers)
                != _integer(
                    contract.get("survivor_barrier_count"),
                    "v13 survivor barrier",
                    1,
                )
            )
            or signers != common_sources
            or payload.get("required_release_count") != len(signers)
            or common_configuration != identity["successor_configuration"]
            or _digest(common.get("block_hash"), "v13 E2 common block hash") == "0" * 64
            or payload.get("e2_reserve_raw_ns") != 90_000_000_000
            or common_tick <= final_ack
            or final_ack > (1 << 64) - 1 - 5_000_000_000
            or common_tick >= final_ack + 5_000_000_000
            or final_ack > (1 << 64) - 1 - 65_000_000_000
            or common_tick > (1 << 64) - 1 - 60_000_000_000
            or earliest != max(final_ack + 65_000_000_000,
                               common_tick + 60_000_000_000)
            or actual < earliest
            or actual > (1 << 64) - 1 - 90_000_000_000
            or actual + 90_000_000_000 >= hard
        ):
            _error("v13 E2 eligibility audit drifted")
        return

    if event.get("source_kind") != "adaptation_manager" or event.get(
        "source_id"
    ) != "adaptive-manager":
        _error("v13 readiness terminal source drifted")
    terminal_identity, _ = _v13_ready_identity_bytes(payload.get("terminal_identity"))
    signers = payload.get("observed_signers")
    if (
        payload.get("replica_id") is not None
        or not no_signer
        or any(payload.get(field) is not None for field in
               ("observation_digest", "certificate_digest", "payload_digest",
                "canonical_wire_payload_hex"))
        or not isinstance(signers, list)
        or any(type(source) is not int or source not in members for source in signers)
        or signers != sorted(signers)
        or len(signers) != len(set(signers))
        or payload.get("required_release_count") != len(signers)
        or payload.get("delivery_attempt") != 0
        or payload.get("delivery_enqueued") is not False
        or payload.get("disposition") != "session_terminal"
        or _integer(payload.get("terminal_cycle_ordinal"), "v13 terminal cycle") not in {0, 1}
        or payload.get("terminal_reason") != 1
        or terminal_identity != identity
        or _digest(payload.get("terminal_bundle_digest"), "v13 terminal bundle digest") == "0" * 64
    ):
        _error("v13 successful readiness terminal drifted")


def _validate_v13_sources(root: Path, contract: Mapping[str, object]) -> tuple[list[dict[str, Any]], list[list[str]]]:
    """V13-only source route; archived event/source rules remain unmodified."""
    events, inventory = _validate_sources(root)
    replicas = {str(event["source_id"]) for event in events if event["source_kind"] == "replica"}
    managers = {str(event["source_id"]) for event in events if event["source_kind"] == "adaptation_manager"}
    expected_replicas = {f"replica-{replica}" for replica in contract["members"]}
    if replicas != expected_replicas or len(managers) != 1:
        _error("v13 source inventory lacks exact replica or manager envelopes")
    readiness_names = {"epoch.activation_prepared", "epoch.activation_ready_signed",
        "adaptive_v3.readiness_observation_retry_exhausted",
        "adaptive_v3.readiness_observation_accepted", "adaptive_v3.readiness_observation_rejected",
        "adaptive_v3.readiness_source_quarantined", "adaptive_v3.readiness_certificate_assembled", "adaptive_v3.readiness_certificate_delivery",
        "adaptive_v3.readiness_certificate_accepted", "adaptive_v3.readiness_certificate_rejected", "adaptive_v3.readiness_certificate_acknowledged",
        "adaptive_v3.e2_eligibility", "adaptive_v3.readiness_terminal", "adaptive_v3.readiness_wire_rejected",
        "adaptive_v3.command_terminal"}
    forbidden = {"adaptive_v3.readiness_observation_rejected", "adaptive_v3.readiness_source_quarantined",
                 "adaptive_v3.readiness_certificate_rejected", "adaptive_v3.readiness_wire_rejected", "adaptive_v3.command_terminal"}
    for event in events:
        name = str(event["event_type"])
        if (name.startswith("adaptive_v3.readiness_") or
                name.startswith("adaptive_v3_readiness_")) and name not in readiness_names:
            _error("v13 readiness event name is unknown")
        if name in readiness_names:
            _validate_v13_readiness_event_payload(event, contract)
            if name in forbidden or (
                name == "adaptive_v3.readiness_certificate_delivery"
                and event["payload"].get("disposition")
                not in {"queued", "retry_scheduled"}
            ):
                _error("v13 PASS readiness event is unsuccessful")
    return events, inventory


def _v13_event_successor_epoch(event: Mapping[str, object]) -> int | None:
    payload = _mapping(event.get("payload"), "v13 readiness event payload")
    identity = payload.get("identity")
    if not isinstance(identity, Mapping):
        return None
    successor = identity.get("successor_configuration")
    if not isinstance(successor, Mapping):
        return None
    value = successor.get("epoch_number")
    return value if type(value) is int else None


def _validate_v13_observation_acceptance_causality(
    rows: Sequence[Mapping[str, object]],
    *,
    signer_raw_ns: int,
    assembled_sequence: int,
    terminal_sequence: int,
) -> None:
    """Bind the one collector admission while allowing in-flight retries."""
    primary_sequences: list[int] = []
    duplicate_sequences: list[int] = []
    for row in rows:
        payload = _mapping(row.get("payload"), "v13 accepted observation")
        sequence = _integer(
            row.get("source_sequence"), "v13 observation manager sequence", 1
        )
        if _uint64(
            row.get("source_monotonic_ns"),
            "v13 observation acceptance time",
        ) < signer_raw_ns or sequence >= terminal_sequence:
            _error("v13 manager observation acceptance is not causal")
        disposition = payload.get("disposition")
        if disposition in {"accepted", "released"}:
            if sequence >= assembled_sequence:
                _error("v13 manager observation acceptance is not causal")
            primary_sequences.append(sequence)
        elif disposition == "duplicate":
            duplicate_sequences.append(sequence)
        else:
            _error("v13 manager observation acceptance is not causal")
    if len(primary_sequences) != 1 or any(
        sequence <= primary_sequences[0] for sequence in duplicate_sequences
    ):
        _error("v13 manager observation acceptance is not causal")


def _reconstruct_v13_certified_readiness(
    events: Sequence[Mapping[str, object]],
    contract: Mapping[str, object],
    *,
    expected_cycle_count: int,
    bundle_digests: Sequence[str],
) -> list[dict[str, object]]:
    """Reconstruct successful certified activation without consulting fault truth."""
    if expected_cycle_count not in {1, 2} or len(bundle_digests) != expected_cycle_count:
        _error("v13 certified-readiness cycle cardinality drifted")
    members = tuple(_integer(member, "v13 readiness member") for member in contract["members"])
    release_count = _integer(
        contract.get("survivor_barrier_count"), "v13 survivor barrier", 1
    )
    if release_count > len(members):
        _error("v13 survivor barrier exceeds membership")
    results: list[dict[str, object]] = []
    terminal_events = [
        event for event in events
        if event.get("event_type") == "adaptive_v3.readiness_terminal"
    ]
    e2_events = [
        event for event in events
        if event.get("event_type") == "adaptive_v3.e2_eligibility"
    ]
    if len(terminal_events) != expected_cycle_count or len(e2_events) != expected_cycle_count - 1:
        _error("v13 readiness terminal or E2 audit cardinality drifted")

    for ordinal in range(expected_cycle_count):
        epoch_number = ordinal + 1
        cycle = [
            event for event in events
            if _v13_event_successor_epoch(event) == epoch_number
        ]
        by_type: dict[str, list[Mapping[str, object]]] = {}
        for event in cycle:
            name = str(event.get("event_type"))
            by_type.setdefault(name, []).append(event)
        required = {
            "epoch.activation_prepared",
            "epoch.activation_ready_signed",
            "adaptive_v3.readiness_observation_accepted",
            "adaptive_v3.readiness_certificate_assembled",
            "adaptive_v3.readiness_certificate_delivery",
            "adaptive_v3.readiness_certificate_accepted",
            "adaptive_v3.readiness_certificate_acknowledged",
            "adaptive_v3.readiness_terminal",
        }
        if not required.issubset(by_type):
            _error("v13 certified-readiness cycle is incomplete")
        for name in required:
            for event in by_type[name]:
                _validate_v13_readiness_event_payload(event, contract)

        assembled_events = by_type["adaptive_v3.readiness_certificate_assembled"]
        terminal = by_type["adaptive_v3.readiness_terminal"]
        if len(assembled_events) != 1 or len(terminal) != 1:
            _error("v13 certificate assembly or terminal is ambiguous")
        assembled_event = assembled_events[0]
        assembled = _mapping(assembled_event["payload"], "v13 assembled certificate")
        certificate_wire = _v13_readiness_wire(assembled, "v13 assembled certificate")
        certificate = _v13_decode_readiness_certificate(
            certificate_wire,
            maximum_members=len(members),
            maximum_payload_bytes=_V13_MAX_REPLICA_MESSAGE_BYTES,
        )
        identity = _mapping(certificate["identity"], "v13 certificate identity")
        signers = tuple(
            _integer(observation["signer_replica_id"], "v13 certificate signer")
            for observation in certificate["observations"]
        )
        if (
            len(signers) != release_count
            or len(set(signers)) != release_count
            or not set(signers).issubset(members)
            or tuple(assembled.get("observed_signers", ())) != signers
            or assembled.get("required_release_count") != release_count
        ):
            _error("v13 certificate does not contain exact R signers")

        def replica_map(name: str) -> dict[int, Mapping[str, object]]:
            rows = by_type[name]
            result: dict[int, Mapping[str, object]] = {}
            for row in rows:
                payload = _mapping(row["payload"], f"v13 {name} payload")
                replica = _integer(payload.get("replica_id"), f"v13 {name} replica")
                if replica in result:
                    _error(f"v13 {name} duplicates a replica")
                result[replica] = row
            return result

        prepared = replica_map("epoch.activation_prepared")
        signed = replica_map("epoch.activation_ready_signed")
        accepted = replica_map("adaptive_v3.readiness_certificate_accepted")
        acknowledgements = replica_map(
            "adaptive_v3.readiness_certificate_acknowledged"
        )
        expected_signers = set(signers)
        if any(set(rows) != expected_signers for rows in
               (prepared, signed, accepted, acknowledgements)):
            _error("v13 readiness per-replica barrier differs from certificate R")

        retry_exhausted = replica_map(
            "adaptive_v3.readiness_observation_retry_exhausted"
        ) if "adaptive_v3.readiness_observation_retry_exhausted" in by_type else {}
        if not set(retry_exhausted).issubset(expected_signers):
            _error("v13 retry exhaustion source differs from certificate R")
        for replica, row in retry_exhausted.items():
            retry_payload = _mapping(
                row.get("payload"), "v13 retry exhaustion payload"
            )
            signed_payload = _mapping(
                signed[replica].get("payload"),
                "v13 signed observation payload",
            )
            signed_sequence = _integer(
                signed[replica].get("source_sequence"),
                "v13 signed observation source sequence", 1,
            )
            retry_sequence = _integer(
                row.get("source_sequence"),
                "v13 retry exhaustion source sequence", 1,
            )
            accepted_sequence = _integer(
                accepted[replica].get("source_sequence"),
                "v13 certificate acceptance source sequence", 1,
            )
            if not signed_sequence < retry_sequence < accepted_sequence:
                _error("v13 retry exhaustion is outside its certificate interval")
            if (
                retry_payload.get("canonical_wire_payload_hex")
                != signed_payload.get("canonical_wire_payload_hex")
                or retry_payload.get("observation_digest")
                != signed_payload.get("observation_digest")
            ):
                _error("v13 retry exhaustion changed the signed observation")

        observations: dict[int, list[Mapping[str, object]]] = {}
        for row in by_type["adaptive_v3.readiness_observation_accepted"]:
            payload = _mapping(row["payload"], "v13 accepted observation")
            replica = _integer(payload.get("replica_id"), "v13 observation replica")
            observations.setdefault(replica, []).append(row)
        if set(observations) != expected_signers or sum(
            _mapping(row["payload"], "v13 released observation").get("disposition")
            == "released"
            for rows in observations.values() for row in rows
        ) != 1:
            _error("v13 accepted observation set does not release exact R")

        deliveries: dict[int, list[Mapping[str, object]]] = {}
        for row in by_type["adaptive_v3.readiness_certificate_delivery"]:
            payload = _mapping(row["payload"], "v13 certificate delivery")
            replica = _integer(payload.get("replica_id"), "v13 delivery replica")
            deliveries.setdefault(replica, []).append(row)
        if set(deliveries) != expected_signers:
            _error("v13 certificate delivery set differs from R")

        assembled_sequence = _integer(
            assembled_event.get("source_sequence"), "v13 assembly sequence", 1
        )
        terminal_event = terminal[0]
        terminal_payload = _mapping(terminal_event["payload"], "v13 terminal")
        terminal_sequence = _integer(
            terminal_event.get("source_sequence"), "v13 terminal sequence", 1
        )
        if (
            terminal_payload.get("terminal_cycle_ordinal") != ordinal
            or terminal_payload.get("terminal_reason") != 1
            or terminal_payload.get("terminal_identity") != identity
            or terminal_payload.get("terminal_bundle_digest")
            != _digest(bundle_digests[ordinal], "v13 bundle digest")
            or tuple(terminal_payload.get("observed_signers", ())) != signers
        ):
            _error("v13 successful terminal identity drifted")

        certificate_observations = {
            _integer(value["signer_replica_id"], "v13 certificate signer"): value
            for value in certificate["observations"]
        }
        for replica in signers:
            signed_event = signed[replica]
            signed_payload = _mapping(signed_event["payload"], "v13 signed observation")
            signed_wire = _v13_readiness_wire(signed_payload, "v13 signed observation")
            signed_observation = _v13_decode_ready_observation(signed_wire)
            if signed_observation != certificate_observations[replica]:
                _error("v13 replica-signed observation differs from certificate")
            for observed_event in observations[replica]:
                observed_payload = _mapping(observed_event["payload"], "v13 accepted observation")
                if (
                    _v13_readiness_wire(observed_payload, "v13 accepted observation")
                    != signed_wire
                ):
                    _error("v13 manager observation acceptance is not causal")
            _validate_v13_observation_acceptance_causality(
                observations[replica],
                signer_raw_ns=_uint64(
                    signed_payload.get("signer_monotonic_raw_ns"),
                    "v13 signer time",
                ),
                assembled_sequence=assembled_sequence,
                terminal_sequence=terminal_sequence,
            )
            accepted_payload = _mapping(accepted[replica]["payload"], "v13 certificate acceptance")
            if _v13_readiness_wire(accepted_payload, "v13 accepted certificate") != certificate_wire:
                _error("v13 replica accepted a different certificate")
            ack_payload = _mapping(acknowledgements[replica]["payload"], "v13 readiness ACK")
            ack_wire = _v13_readiness_wire(ack_payload, "v13 readiness ACK")
            _validate_v13_readiness_wire_chain(
                _v13_encode_ready_identity(identity),
                signed_wire,
                certificate_wire,
                ack_wire,
                maximum_members=len(members),
                maximum_payload_bytes=_V13_MAX_REPLICA_MESSAGE_BYTES,
            )
            delivery_sequences = [
                _integer(row.get("source_sequence"), "v13 delivery sequence", 1)
                for row in deliveries[replica]
            ]
            delivery_payloads = [
                _mapping(row["payload"], "v13 certificate delivery")
                for row in deliveries[replica]
            ]
            delivery_attempts = [
                _integer(
                    payload.get("delivery_attempt"),
                    "v13 delivery attempt",
                    1,
                )
                for payload in delivery_payloads
            ]
            queued_deliveries = [
                payload
                for payload in delivery_payloads
                if payload.get("disposition") == "queued"
                and payload.get("delivery_enqueued") is True
            ]
            ack_sequence = _integer(
                acknowledgements[replica].get("source_sequence"),
                "v13 ACK manager sequence", 1,
            )
            if (
                not queued_deliveries
                or delivery_attempts != list(range(1, len(delivery_attempts) + 1))
                or len(delivery_attempts) > _V13_MAXIMUM_DELIVERY_ATTEMPTS
                or delivery_sequences != sorted(delivery_sequences)
                or len(delivery_sequences) != len(set(delivery_sequences))
                or min(delivery_sequences) <= assembled_sequence
                or max(delivery_sequences) >= ack_sequence
                or ack_sequence >= terminal_sequence
            ):
                _error("v13 certificate delivery/ACK/terminal order drifted")

            source_events = sorted(
                (
                    row for row in events
                    if row.get("source_kind") == "replica"
                    and row.get("source_id") == f"replica-{replica}"
                ),
                key=lambda row: _integer(row.get("source_sequence"), "replica sequence", 1),
            )
            positions = {
                name: next(
                    (index for index, row in enumerate(source_events)
                     if row is candidate), None
                )
                for name, candidate in {
                    "prepared": prepared[replica],
                    "signed": signed[replica],
                    "accepted": accepted[replica],
                }.items()
            }
            activations = [
                (index, row) for index, row in enumerate(source_events)
                if row.get("event_type") == "epoch.activated"
                and _mapping(row.get("payload"), "v13 activation").get("epoch_number")
                == epoch_number
            ]
            if (
                any(position is None for position in positions.values())
                or len(activations) != 1
                or not positions["prepared"] < positions["signed"] < positions["accepted"] < activations[0][0]
            ):
                _error("v13 replica activation chain order drifted")
            activation = _mapping(activations[0][1]["payload"], "v13 activation")
            expected_activation = {
                **identity["successor_configuration"],
                "activation_height": identity["activation_height"],
                "activation_readiness_certificate_digest": certificate["certificate_digest"],
            }
            expected_activation_keys = set(expected_activation) | {
                "certificate_apply_committed_height"
            }
            if (
                set(activation) != expected_activation_keys
                or any(
                    activation.get(key) != value
                    for key, value in expected_activation.items()
                )
                or _uint64(
                    activation.get("certificate_apply_committed_height"),
                    "v13 certificate apply height",
                    1,
                )
                < _uint64(
                    identity["activation_height"],
                    "v13 scheduled activation height",
                    1,
                )
            ):
                _error("v13 replica activation certificate binding drifted")

        ordered_acknowledgements = sorted(
            acknowledgements.values(),
            key=lambda row: _integer(
                row.get("source_sequence"), "v13 ACK manager sequence", 1
            ),
        )
        if len(ordered_acknowledgements) < 2:
            _error("v13 certified cycle has fewer than two ACK observations")
        results.append({
            "cycle_ordinal": ordinal,
            "identity": identity,
            "certificate": certificate,
            "certificate_wire": certificate_wire,
            "signers": signers,
            "assembled_event": assembled_event,
            "terminal_event": terminal_event,
            "final_ack_manager_sequence": max(
                _integer(row.get("source_sequence"), "v13 final ACK sequence", 1)
                for row in acknowledgements.values()
            ),
            "pre_final_ack_manager_time_ns": _uint64(
                ordered_acknowledgements[-2].get("source_monotonic_ns"),
                "v13 pre-final ACK envelope time",
                1,
            ),
            "final_ack_manager_time_ns": _uint64(
                ordered_acknowledgements[-1].get("source_monotonic_ns"),
                "v13 final ACK envelope time",
                1,
            ),
        })

    if expected_cycle_count == 1:
        if e2_events:
            _error("v13 control arm contains E2 eligibility")
    else:
        e2 = e2_events[0]
        _validate_v13_readiness_event_payload(e2, contract)
        e2_payload = _mapping(e2["payload"], "v13 E2 eligibility")
        if (
            e2_payload.get("identity") != results[0]["identity"]
            or tuple(e2_payload.get("observed_signers", ())) != results[0]["signers"]
            or _integer(e2.get("source_sequence"), "v13 E2 sequence", 1)
            <= _integer(
                results[0]["terminal_event"].get("source_sequence"),
                "v13 E1 terminal sequence", 1,
            )
            or _integer(e2.get("source_sequence"), "v13 E2 sequence", 1)
            >= _integer(
                results[1]["assembled_event"].get("source_sequence"),
                "v13 E2 assembly sequence", 1,
            )
        ):
            _error("v13 E2 eligibility is not between certified cycles")
    if expected_cycle_count == 2 and results[0]["signers"] != results[1]["signers"]:
        _error("v13 certified cycles disagree on R")
    return results


def _validate_v13_transition(
    events: Sequence[Mapping[str, object]],
    decoded: Any,
    readiness_cycle: Mapping[str, object],
    contract: Mapping[str, object],
) -> dict[str, object]:
    """Bind one decoded v3 bundle to the exact certified replica transition."""

    members = tuple(
        _integer(member, "v13 transition member") for member in contract["members"]
    )
    sources = tuple(
        _integer(source, "v13 transition signer")
        for source in _sequence(
            readiness_cycle.get("signers"), "v13 transition signers"
        )
    )
    required_release_count = _integer(
        contract.get("survivor_barrier_count"), "v13 survivor barrier", 1
    )
    if (
        len(sources) != required_release_count
        or tuple(sorted(set(sources))) != sources
        or not set(sources).issubset(members)
    ):
        _error("v13 transition sources differ from certified R")

    identity = _mapping(readiness_cycle.get("identity"), "v13 transition identity")
    certificate = _mapping(
        readiness_cycle.get("certificate"), "v13 transition certificate"
    )
    certificate_digest = _digest(
        certificate.get("certificate_digest"), "v13 transition certificate digest"
    )
    def replica_source(event: Mapping[str, object], label: str) -> int:
        source_id = event.get("source_id")
        if event.get("source_kind") != "replica" or not isinstance(source_id, str):
            _error(f"{label} source is not a replica")
        prefix = "replica-"
        if not source_id.startswith(prefix):
            _error(f"{label} source ID drifted")
        try:
            source = int(source_id.removeprefix(prefix))
        except ValueError as exc:
            raise FocusedCrashPairValidationError(
                f"{label} source ID drifted"
            ) from exc
        if source_id != f"replica-{source}":
            _error(f"{label} source ID is not canonical")
        return source

    command_height = _uint64(
        identity.get("command_block_height"), "v13 certified command height", 1
    )
    command_hash = _digest(
        identity.get("command_block_hash"), "v13 certified command hash"
    )
    authoritative_candidates: list[Mapping[str, object]] = []
    for event in events:
        if event.get("event_type") != "block.committed":
            continue
        payload = _mapping(event.get("payload"), "v13 command decision")
        if (
            payload.get("block_height") == command_height
            or payload.get("block_hash") == command_hash
        ):
            authoritative_candidates.append(event)
    if len(authoritative_candidates) != 1:
        _error("v13 certified command lacks one authoritative decision")
    authoritative_event = authoritative_candidates[0]
    authoritative = _mapping(
        authoritative_event.get("payload"), "v13 command decision"
    )
    decision = _mapping(
        authoritative.get("decision_proof"), "v13 command decision proof"
    )
    if set(decision) != {
        "epoch_number", "tree_id", "epoch_digest", "block_hash"
    }:
        _error("v13 command decision proof schema drifted")
    command_configuration = {
        "epoch_number": _uint64(
            decision.get("epoch_number"), "v13 predecessor decision epoch"
        ),
        "tree_id": _integer(
            decision.get("tree_id"), "v13 predecessor decision tree"
        ),
        "epoch_digest": _digest(
            decision.get("epoch_digest"), "v13 predecessor decision digest"
        ),
    }
    command_generation = _uint64(
        authoritative.get("view_generation"),
        "v13 command decision generation",
        1,
    )
    expected_authoritative_source = contract.get("authoritative_source_id")
    if (
        not isinstance(expected_authoritative_source, str)
        or authoritative_event.get("source_id") != expected_authoritative_source
        or replica_source(authoritative_event, "v13 command decision") not in sources
        or authoritative.get("designated_observer") is not True
        or _uint64(
            authoritative.get("block_height"), "v13 authoritative command height", 1
        ) != command_height
        or _digest(
            authoritative.get("block_hash"), "v13 authoritative command hash"
        ) != command_hash
        or _digest(decision.get("block_hash"), "v13 command decision block hash")
        != command_hash
        or command_configuration["epoch_number"] != decoded.epoch_number - 1
        or command_configuration["epoch_digest"]
        != decoded.previous_epoch_digest
    ):
        _error("v13 certified command decision drifted")

    activation_height = _uint64(
        identity.get("activation_height"), "v13 certified activation height", 1
    )
    activation_boundary_hash = _digest(
        identity.get("activation_boundary_block_hash"),
        "v13 certified activation boundary hash",
    )
    boundary_candidates: list[Mapping[str, object]] = []
    for event in events:
        if event.get("event_type") != "block.committed":
            continue
        payload = _mapping(event.get("payload"), "v13 activation boundary decision")
        if (
            payload.get("block_height") == activation_height
            or payload.get("block_hash") == activation_boundary_hash
        ):
            boundary_candidates.append(event)
    if len(boundary_candidates) != 1:
        _error("v13 certified activation lacks one authoritative boundary")
    boundary_event = boundary_candidates[0]
    boundary = _mapping(
        boundary_event.get("payload"), "v13 activation boundary decision"
    )
    boundary_decision = _mapping(
        boundary.get("decision_proof"), "v13 activation boundary decision proof"
    )
    if set(boundary_decision) != {
        "epoch_number", "tree_id", "epoch_digest", "block_hash"
    }:
        _error("v13 activation boundary decision proof schema drifted")
    predecessor_configuration = {
        "epoch_number": _uint64(
            boundary_decision.get("epoch_number"),
            "v13 activation boundary epoch",
        ),
        "tree_id": _integer(
            boundary_decision.get("tree_id"), "v13 activation boundary tree"
        ),
        "epoch_digest": _digest(
            boundary_decision.get("epoch_digest"),
            "v13 activation boundary epoch digest",
        ),
    }
    predecessor_generation = _uint64(
        boundary.get("view_generation"),
        "v13 activation boundary generation",
        1,
    )
    if (
        boundary_event.get("source_id") != expected_authoritative_source
        or replica_source(boundary_event, "v13 activation boundary") not in sources
        or boundary.get("designated_observer") is not True
        or _uint64(
            boundary.get("block_height"), "v13 authoritative activation height", 1
        )
        != activation_height
        or _digest(
            boundary.get("block_hash"), "v13 authoritative activation hash"
        )
        != activation_boundary_hash
        or _digest(
            boundary_decision.get("block_hash"),
            "v13 activation boundary decision block hash",
        )
        != activation_boundary_hash
        or predecessor_configuration["epoch_number"] != decoded.epoch_number - 1
        or predecessor_configuration["epoch_digest"]
        != decoded.previous_epoch_digest
        or identity.get("predecessor_boundary_configuration")
        != predecessor_configuration
        or identity.get("predecessor_boundary_generation")
        != predecessor_generation
        or activation_height <= command_height
        or _integer(
            boundary_event.get("source_sequence"),
            "v13 activation boundary source sequence",
            1,
        )
        <= _integer(
            authoritative_event.get("source_sequence"),
            "v13 command decision source sequence",
            1,
        )
        or _uint64(
            boundary_event.get("source_monotonic_ns"),
            "v13 activation boundary time",
            1,
        )
        < _uint64(
            authoritative_event.get("source_monotonic_ns"),
            "v13 command decision time",
            1,
        )
    ):
        _error("v13 certified activation boundary drifted")

    successor_configuration = {
        "epoch_number": decoded.epoch_number,
        "tree_id": 0,
        "epoch_digest": decoded.epoch_digest,
    }
    if (
        identity.get("predecessor_boundary_configuration")
        != predecessor_configuration
        or identity.get("successor_configuration") != successor_configuration
        or identity.get("command_payload_digest") != decoded.command.payload_digest
    ):
        _error("v13 certified identity differs from the decoded bundle")

    commands = [
        event
        for event in events
        if event.get("event_type") == "epoch.command_committed"
        and _mapping(event.get("payload"), "v13 epoch command").get(
            "successor_epoch_number"
        )
        == decoded.epoch_number
    ]
    activations = [
        event
        for event in events
        if event.get("event_type") == "epoch.activated"
        and _mapping(event.get("payload"), "v13 epoch activation").get(
            "epoch_number"
        )
        == decoded.epoch_number
    ]

    def exact_replica_map(
        rows: Sequence[Mapping[str, object]], label: str
    ) -> dict[int, Mapping[str, object]]:
        result: dict[int, Mapping[str, object]] = {}
        for row in rows:
            source = replica_source(row, label)
            if source in result:
                _error(f"{label} duplicates a replica")
            result[source] = row
        if tuple(sorted(result)) != sources:
            _error(f"{label} sources differ from certified R")
        return result

    commands_by_source = exact_replica_map(commands, "v13 epoch command")
    activations_by_source = exact_replica_map(
        activations, "v13 epoch activation"
    )
    expected_command: dict[str, object] | None = None
    for command in commands_by_source.values():
        parsed = _command_payload(
            decoded, _mapping(command.get("payload"), "v13 epoch command")
        )
        if expected_command is None:
            expected_command = parsed
        elif parsed != expected_command:
            _error("v13 certified replicas disagree on the epoch command")
    assert expected_command is not None
    if (
        identity.get("command_block_height")
        != expected_command["command_block_height"]
        or identity.get("command_block_hash")
        != expected_command["command_block_hash"]
        or identity.get("activation_delay_blocks")
        != expected_command["activation_delay_blocks"]
        or identity.get("activation_height")
        != expected_command["activation_height"]
    ):
        _error("v13 certified identity differs from the committed epoch command")

    expected_activation = {
        **successor_configuration,
        "activation_height": identity["activation_height"],
        "activation_readiness_certificate_digest": certificate_digest,
    }
    expected_activation_keys = set(expected_activation) | {
        "certificate_apply_committed_height"
    }
    readiness_names = {
        "prepared": "epoch.activation_prepared",
        "signed": "epoch.activation_ready_signed",
        "accepted": "adaptive_v3.readiness_certificate_accepted",
    }
    for source in sources:
        activation = activations_by_source[source]
        activation_payload = _mapping(
            activation.get("payload"), "v13 epoch activation"
        )
        if (
            set(activation_payload) != expected_activation_keys
            or any(
                activation_payload.get(key) != value
                for key, value in expected_activation.items()
            )
            or _uint64(
                activation_payload.get("certificate_apply_committed_height"),
                "v13 certificate apply height",
                1,
            )
            < _uint64(
                identity["activation_height"],
                "v13 scheduled activation height",
                1,
            )
        ):
            _error("v13 activation differs from the certified successor")

        ordered_events: list[Mapping[str, object]] = [commands_by_source[source]]
        for label, name in readiness_names.items():
            matches = [
                event
                for event in events
                if event.get("source_kind") == "replica"
                and event.get("source_id") == f"replica-{source}"
                and event.get("event_type") == name
                and _mapping(event.get("payload"), f"v13 {label} event").get(
                    "identity"
                )
                == identity
            ]
            if len(matches) != 1:
                _error(f"v13 {label} event is absent or ambiguous")
            ordered_events.append(matches[0])
        ordered_events.append(activation)
        sequences = [
            _integer(event.get("source_sequence"), "v13 transition sequence", 1)
            for event in ordered_events
        ]
        times = [
            _uint64(event.get("source_monotonic_ns"), "v13 transition time")
            for event in ordered_events
        ]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            _error("v13 replica transition order drifted")
        if times != sorted(times):
            _error("v13 replica transition time regressed")

    return {
        "sources": sources,
        "authoritative_command": authoritative_event,
        "commands": tuple(commands_by_source[source] for source in sources),
        "activations": tuple(
            activations_by_source[source] for source in sources
        ),
        "command_payload": expected_command,
        "identity": identity,
        "certificate_digest": certificate_digest,
    }


def _validate_v13_e2_common_commit(
    events: Sequence[Mapping[str, object]],
    contract: Mapping[str, object],
    readiness_cycles: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Bind the E2 audit to one authoritative commit and all-R observations."""

    if len(readiness_cycles) != 2:
        _error("v13 E2 common-commit reconstruction requires two certified cycles")
    e2_events = [
        event
        for event in events
        if event.get("event_type") == "adaptive_v3.e2_eligibility"
    ]
    if len(e2_events) != 1:
        _error("v13 E2 eligibility audit is absent or ambiguous")
    e2_event = e2_events[0]
    _validate_v13_readiness_event_payload(e2_event, contract)
    e2_payload = _mapping(e2_event.get("payload"), "v13 E2 eligibility")

    cycle_signers = tuple(
        _integer(source, "v13 certified cycle signer")
        for source in _sequence(
            readiness_cycles[0].get("signers"), "v13 E1 certified signers"
        )
    )
    if (
        not cycle_signers
        or tuple(readiness_cycles[1].get("signers", ())) != cycle_signers
        or tuple(e2_payload.get("observed_signers", ())) != cycle_signers
        or tuple(e2_payload.get("e2_common_commit_sources", ())) != cycle_signers
    ):
        _error("v13 E2 common-commit sources differ from certified R")

    e1_identity = _mapping(readiness_cycles[0].get("identity"), "v13 E1 identity")
    if e2_payload.get("identity") != e1_identity:
        _error("v13 E2 common-commit identity differs from certified E1")
    e1_terminal = _mapping(
        _mapping(
            readiness_cycles[0].get("terminal_event"), "v13 E1 terminal event"
        ).get("payload"),
        "v13 E1 terminal payload",
    )
    if _digest(
        e2_payload.get("e1_bundle_digest"), "v13 E2 E1 bundle digest"
    ) != _digest(
        e1_terminal.get("terminal_bundle_digest"), "v13 E1 terminal bundle digest"
    ):
        _error("v13 E2 audit does not bind the certified E1 bundle")

    final_ack = _uint64(e2_payload.get("e2_final_ack_raw_ns"), "v13 E2 final ACK", 1)
    prior_ack_envelope = _uint64(
        readiness_cycles[0].get("pre_final_ack_manager_time_ns"),
        "v13 certified E1 pre-final ACK envelope time",
        1,
    )
    final_ack_envelope = _uint64(
        readiness_cycles[0].get("final_ack_manager_time_ns"),
        "v13 certified E1 final ACK envelope time",
        1,
    )
    if not prior_ack_envelope <= final_ack <= final_ack_envelope:
        _error("v13 E2 final ACK anchor differs from the certified E1 chain")
    common_tick = _uint64(
        e2_payload.get("e2_common_commit_raw_ns"), "v13 E2 common commit tick", 1
    )
    actual_begin = _uint64(
        e2_payload.get("e2_actual_begin_raw_ns"), "v13 E2 actual begin", 1
    )
    if (
        _uint64(e2_event.get("source_monotonic_ns"), "v13 E2 audit envelope time", 1)
        < actual_begin
    ):
        _error("v13 E2 audit envelope predates the atomic E2 begin")

    common_value = _mapping(e2_payload.get("e2_common_commit"), "v13 E2 common commit")
    if set(common_value) != {"epoch_number", "tree_id", "epoch_digest", "block_hash"}:
        _error("v13 E2 common commit schema drifted")
    common = {
        "epoch_number": _uint64(
            common_value.get("epoch_number"), "v13 E2 common epoch"
        ),
        "tree_id": _integer(common_value.get("tree_id"), "v13 E2 common tree"),
        "epoch_digest": _digest(
            common_value.get("epoch_digest"), "v13 E2 common epoch digest"
        ),
        "block_hash": _digest(
            common_value.get("block_hash"), "v13 E2 common block hash"
        ),
    }
    if {
        key: common[key] for key in ("epoch_number", "tree_id", "epoch_digest")
    } != dict(
        _mapping(
            e1_identity.get("successor_configuration"),
            "v13 E1 successor configuration",
        )
    ):
        _error("v13 E2 common commit is not in the certified E1 successor")

    expected_authoritative_source = contract.get("authoritative_source_id")
    if not isinstance(expected_authoritative_source, str):
        _error("v13 authoritative commit source is absent")

    def replica_source(event: Mapping[str, object], label: str) -> int:
        source_id = event.get("source_id")
        if (
            event.get("source_kind") != "replica"
            or not isinstance(source_id, str)
            or not source_id.startswith("replica-")
        ):
            _error(f"{label} source is not a replica")
        try:
            source = int(source_id.removeprefix("replica-"))
        except ValueError as exc:
            raise FocusedCrashPairValidationError(
                f"{label} replica source ID drifted"
            ) from exc
        if source_id != f"replica-{source}":
            _error(f"{label} replica source ID is not canonical")
        return source

    authoritative_candidates: list[Mapping[str, object]] = []
    for event in _v13_reconstruct_authoritative_commits(events, contract):
        payload = _mapping(event.get("payload"), "v13 authoritative commit")
        decision = payload.get("decision_proof")
        if payload.get("block_hash") == common["block_hash"] or decision == common:
            authoritative_candidates.append(event)
    if len(authoritative_candidates) != 1:
        _error("v13 E2 common commit lacks one authoritative decision")
    authoritative_event = authoritative_candidates[0]
    authoritative = _mapping(
        authoritative_event.get("payload"), "v13 authoritative commit"
    )
    if set(authoritative) != {
        "block_height",
        "block_hash",
        "parent_hash",
        "transaction_count",
        "designated_observer",
        "decision_proof",
        "view_generation",
        "commit_batch_index",
    }:
        _error("v13 authoritative commit payload schema drifted")
    authoritative_source = replica_source(
        authoritative_event, "v13 authoritative commit"
    )
    decision = _mapping(
        authoritative.get("decision_proof"), "v13 authoritative decision proof"
    )
    parent_hash = authoritative.get("parent_hash")
    if parent_hash is not None:
        _digest(parent_hash, "v13 authoritative parent hash")
    authoritative_time = _uint64(
        authoritative_event.get("source_monotonic_ns"),
        "v13 authoritative commit time",
        1,
    )
    _uint64(authoritative.get("block_height"), "v13 authoritative block height", 1)
    _uint64(
        authoritative.get("transaction_count"),
        "v13 authoritative transaction count",
    )
    _uint64(
        authoritative.get("view_generation"),
        "v13 authoritative view generation",
        1,
    )
    _uint64(
        authoritative.get("commit_batch_index"),
        "v13 authoritative commit batch",
    )
    if (
        authoritative_event.get("source_id") != expected_authoritative_source
        or authoritative_source not in cycle_signers
        or authoritative.get("designated_observer") is not True
        or set(decision) != set(common)
        or {
            "epoch_number": _uint64(decision.get("epoch_number"), "v13 decision epoch"),
            "tree_id": _integer(decision.get("tree_id"), "v13 decision tree"),
            "epoch_digest": _digest(
                decision.get("epoch_digest"), "v13 decision epoch digest"
            ),
            "block_hash": _digest(
                decision.get("block_hash"), "v13 decision block hash"
            ),
        }
        != common
        or _digest(authoritative.get("block_hash"), "v13 authoritative block hash")
        != common["block_hash"]
        or not final_ack < authoritative_time <= common_tick
    ):
        _error("v13 authoritative common commit drifted")
    identity_carriers = _sequence(
        authoritative_event.get("_identity_carriers"),
        "v13 authoritative identity carriers",
    )
    if not identity_carriers or any(
        _uint64(
            _mapping(carrier, "v13 authoritative identity carrier").get(
                "source_monotonic_ns"
            ),
            "v13 authoritative identity carrier time",
            1,
        )
        > common_tick
        for carrier in identity_carriers
    ):
        _error("v13 authoritative identity was not observed before the common tick")

    observed_by_source: dict[int, Mapping[str, object]] = {}
    for event in events:
        if event.get("event_type") != "block.commit_observed":
            continue
        payload = _mapping(event.get("payload"), "v13 common commit observation")
        if payload.get("block_hash") != common["block_hash"]:
            continue
        if set(payload) != {
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "commit_batch_index",
        }:
            _error("v13 common commit observation payload schema drifted")
        source = replica_source(event, "v13 common commit observation")
        if source in observed_by_source:
            _error("v13 common commit observation duplicates a source")
        observed_time = _uint64(
            event.get("source_monotonic_ns"), "v13 common commit observation time", 1
        )
        if (
            source not in cycle_signers
            or dict(payload)
            != {
                key: authoritative[key]
                for key in (
                    "block_height",
                    "block_hash",
                    "parent_hash",
                    "transaction_count",
                    "commit_batch_index",
                )
            }
            or not final_ack < observed_time <= common_tick
        ):
            _error("v13 common commit observation drifted")
        observed_by_source[source] = event
    if tuple(sorted(observed_by_source)) != cycle_signers:
        _error("v13 common commit observations do not cover exact R")
    designated_observation = observed_by_source[authoritative_source]
    if (
        authoritative_event.get("source_sequence")
        != designated_observation.get("source_sequence")
        or authoritative_event.get("source_monotonic_ns")
        != designated_observation.get("source_monotonic_ns")
    ):
        _error("v13 authoritative timing is not the designated observation")

    return {
        "sources": tuple(sorted(observed_by_source)),
        "authoritative_event": authoritative_event,
        "observations": tuple(observed_by_source[source] for source in cycle_signers),
        "common": common,
        "common_tick": common_tick,
        "final_ack_tick": final_ack,
    }


def _read_epoch_issuer_public_key(root: Path) -> tuple[str, bytes]:
    path = root / "raw" / "issuer-public-key.txt"
    if path.is_symlink() or not path.is_file():
        _error("issuer public key is absent")
    raw = path.read_bytes()
    if (
        not raw.endswith(b"\n")
        or raw.count(b"\n") != 1
        or any(byte > 0x7F for byte in raw)
    ):
        _error("issuer public key encoding is malformed")
    encoded = raw[:-1].decode("ascii")
    if (
        len(encoded) not in {66, 130}
        or any(character not in "0123456789abcdef" for character in encoded)
    ):
        _error("issuer public key encoding is malformed")
    try:
        decoded = bytes.fromhex(encoded)
    except ValueError as exc:  # pragma: no cover - alphabet check is exhaustive
        raise FocusedCrashPairValidationError(
            "issuer public key encoding is malformed"
        ) from exc
    return encoded, decoded


def _validate_v13_certified_transition_fragment(
    root: Path, contract: Mapping[str, object]
) -> dict[str, object]:
    """Validate the source-blind native v13 transition fragment for one arm."""

    profile = _mapping(contract.get("profile"), "focused profile")
    if profile.get("profile_id") not in _FCRASH_H_V13_PROFILE_IDS:
        _error("v13 certified transition fragment used by an archived profile")
    events, inventory = _validate_v13_sources(root, contract)
    issuer, issuer_bytes = _read_epoch_issuer_public_key(root)
    epoch2_path = root / "raw" / "epoch2.bundle"
    expected_cycle_count = 2 if epoch2_path.exists() else 1
    arm = "adaptive" if expected_cycle_count == 2 else "control"

    bundle_wires: list[bytes] = []
    bundles: list[Any] = []
    predecessor = _digest(
        contract.get("epoch_zero_digest"), "v13 Epoch 0 digest"
    )
    for epoch_number in range(1, expected_cycle_count + 1):
        wire, decoded = _decode_bundle(
            root / "raw" / f"epoch{epoch_number}.bundle",
            issuer,
            epoch_number,
            contract,
        )
        if decoded.previous_epoch_digest != predecessor:
            _error("v13 native bundle predecessor chain drifted")
        predecessor = decoded.epoch_digest
        bundle_wires.append(wire)
        bundles.append(decoded)

    bundle_digests = tuple(_sha_bytes(wire) for wire in bundle_wires)
    readiness = _reconstruct_v13_certified_readiness(
        events,
        contract,
        expected_cycle_count=expected_cycle_count,
        bundle_digests=bundle_digests,
    )
    transitions = tuple(
        _validate_v13_transition(events, decoded, cycle, contract)
        for decoded, cycle in zip(bundles, readiness, strict=True)
    )
    common_commit = (
        None
        if expected_cycle_count == 1
        else _validate_v13_e2_common_commit(events, contract, readiness)
    )
    return {
        "arm": arm,
        "events": events,
        "source_inventory": inventory,
        "issuer_public_key": issuer,
        "issuer_public_key_sha256": _sha_bytes(issuer_bytes),
        "bundle_wires": tuple(bundle_wires),
        "bundle_digests": bundle_digests,
        "bundles": tuple(bundles),
        "readiness_cycles": tuple(readiness),
        "transitions": transitions,
        "e2_common_commit": common_commit,
    }


def _validate_v13_fault_join(
    root: Path,
    contract: Mapping[str, object],
    transition_state: Mapping[str, object],
) -> dict[str, object]:
    """Join exact crash truth only after the certified source-blind state exists."""

    events = _sequence(transition_state.get("events"), "v13 source-blind events")
    cycles = _sequence(
        transition_state.get("readiness_cycles"), "v13 readiness cycles"
    )
    transitions = _sequence(
        transition_state.get("transitions"), "v13 certified transitions"
    )
    if not events or not cycles or len(cycles) != len(transitions):
        _error("v13 fault join lacks a complete source-blind reconstruction")

    receipt = _read_json(root / "raw" / "fault-receipt.json", "fault receipt")
    if (
        set(receipt)
        != {
            "schema_version",
            "fault_plan",
            "process_records",
            "sigkill_outcomes",
            "fault_journal",
        }
        or receipt.get("schema_version") != 1
    ):
        _error("v13 fault receipt schema drifted")
    _validate_atomic_fault_receipt(contract, receipt)

    outcomes = tuple(
        _mapping(value, "v13 SIGKILL outcome")
        for value in _sequence(receipt.get("sigkill_outcomes"), "SIGKILL outcomes")
    )
    confirmations = {
        _integer(outcome.get("replica_id"), "v13 crashed replica"): _uint64(
            outcome.get("confirmed_monotonic_ns"), "v13 fault confirmation", 1
        )
        for outcome in outcomes
    }
    targets = tuple(_integer(value, "v13 target") for value in contract["targets"])
    if tuple(confirmations) != targets:
        _error("v13 fault receipt targets differ from the frozen intervention")
    members = tuple(_integer(value, "v13 member") for value in contract["members"])
    survivors = tuple(member for member in members if member not in confirmations)
    if survivors != tuple(contract.get("survivors", ())):
        _error("v13 fault-derived survivors differ from the frozen membership")

    for event in events:
        if event.get("source_kind") != "replica":
            continue
        source_id = event.get("source_id")
        if not isinstance(source_id, str) or not source_id.startswith("replica-"):
            _error("v13 replica source ID drifted during fault join")
        try:
            replica = int(source_id.removeprefix("replica-"))
        except ValueError as exc:
            raise FocusedCrashPairValidationError(
                "v13 replica source ID drifted during fault join"
            ) from exc
        if (
            replica in confirmations
            and _uint64(
                event.get("source_monotonic_ns"), "v13 replica event time"
            )
            > confirmations[replica]
        ):
            _error("crashed v13 replica emitted an event after confirmed SIGKILL")

    for cycle, transition in zip(cycles, transitions, strict=True):
        if tuple(cycle.get("signers", ())) != survivors:
            _error("v13 certified readiness does not equal fault-derived survivors")
        if tuple(_mapping(transition, "v13 transition").get("sources", ())) != survivors:
            _error("v13 certified activation does not equal fault-derived survivors")

    fault_anchor = max(confirmations.values())
    profile = _mapping(contract.get("profile"), "focused profile")
    hard_seconds = _integer(
        _mapping(profile.get("timers"), "profile timers").get(
            "arm_hard_deadline_seconds"
        ),
        "v13 arm hard deadline",
        1,
    )
    hard_duration = hard_seconds * 1_000_000_000
    if hard_duration > (1 << 64) - 1 or fault_anchor > (1 << 64) - 1 - hard_duration:
        _error("v13 hard deadline overflows uint64")
    hard_deadline = fault_anchor + hard_duration
    if any(
        event.get("event_type") in _V13_READINESS_SUCCESS_EVENTS
        and _uint64(event.get("source_monotonic_ns"), "v13 readiness event time", 1)
        >= hard_deadline
        for event in events
    ):
        _error("v13 readiness evidence reached the half-open hard deadline")

    common = transition_state.get("e2_common_commit")
    if common is None:
        if len(cycles) != 1:
            _error("v13 control arm cycle cardinality drifted")
    else:
        if len(cycles) != 2:
            _error("v13 adaptive arm cycle cardinality drifted")
        e2_event = next(
            event
            for event in events
            if event.get("event_type") == "adaptive_v3.e2_eligibility"
        )
        e2_payload = _mapping(e2_event.get("payload"), "v13 E2 eligibility")
        if _uint64(
            e2_payload.get("e2_hard_deadline_raw_ns"), "v13 E2 hard deadline", 1
        ) != hard_deadline:
            _error("v13 E2 hard deadline differs from the fault-derived cap")
        e2_transition = _mapping(transitions[1], "v13 E2 transition")
        commands = tuple(
            _mapping(event, "v13 E2 command")
            for event in _sequence(e2_transition.get("commands"), "v13 E2 commands")
        )
        activations = tuple(
            _mapping(event, "v13 E2 activation")
            for event in _sequence(
                e2_transition.get("activations"), "v13 E2 activations"
            )
        )
        if not commands or not activations:
            _error("v13 E2 timing chain is incomplete")
        command_anchor = max(
            _uint64(event.get("source_monotonic_ns"), "v13 E2 command time", 1)
            for event in commands
        )
        if command_anchor > (1 << 64) - 1 - 90_000_000_000:
            _error("v13 E2 activation window overflows uint64")
        activation_limit = command_anchor + 90_000_000_000
        if any(
            not command_anchor
            < _uint64(event.get("source_monotonic_ns"), "v13 E2 activation time", 1)
            < min(activation_limit, hard_deadline)
            for event in activations
        ):
            _error("v13 E2 activation falls outside the half-open budget")

    return {
        "fault_receipt": receipt,
        "fault_receipt_sha256": _hash(receipt),
        "targets": targets,
        "survivors": survivors,
        "confirmations": confirmations,
        "fault_anchor_raw_ns": fault_anchor,
        "hard_deadline_raw_ns": hard_deadline,
    }


def _decode_bundle(
    path: Path,
    issuer: str,
    epoch_number: int,
    contract: Mapping[str, object],
) -> tuple[bytes, Any]:
    if path.is_symlink() or not path.is_file():
        _error(f"epoch {epoch_number} native bundle is absent")
    wire = path.read_bytes()
    try:
        profile = _mapping(contract["profile"], "focused profile")
        if profile.get("profile_id") in _FCRASH_H_V13_PROFILE_IDS:
            # V13 has a separately domain-separated schema/mode.  Never
            # sniff: archived profiles retain their exact v2 byte path.
            decoded = factorial_validation.decode_adaptive_v3_epoch_change_bundle(
                wire, issuer_public_key=issuer
            )
        else:
            decoded = factorial_validation.decode_epoch_change_bundle(
                wire, issuer_public_key=issuer
            )
    except factorial_validation.FactorialValidationError as exc:
        raise FocusedCrashPairValidationError(
            f"epoch {epoch_number} native bundle is invalid"
        ) from exc
    if decoded.epoch_number != epoch_number or len(decoded.trees) != contract["quorum"]:
        _error(f"epoch {epoch_number} bundle identity drifted")
    return wire, decoded


def _canonical_tree_members(
    root: int, contract: Mapping[str, object], selected: Sequence[int] | None = None
) -> tuple[int, ...]:
    targets = tuple(contract["targets"] if selected is None else selected)
    survivors = tuple(
        replica for replica in contract["members"] if replica not in targets
    )
    fanout = int(contract["fanout"])
    internal = tuple(replica for replica in survivors if replica != root)[:fanout]
    leaves = tuple(replica for replica in survivors if replica not in (root, *internal))
    return (root, *internal, *leaves, *targets)


def _validate_trees(
    decoded: Any,
    roots: Sequence[int],
    label: str,
    contract: Mapping[str, object],
    selected: Sequence[int] | None = None,
) -> None:
    quorum = int(contract["quorum"])
    if tuple(tree.tree_id for tree in decoded.trees) != tuple(range(quorum)):
        _error(f"{label} tree IDs drifted")
    if tuple(tree.members[0] for tree in decoded.trees) != tuple(roots):
        _error(f"{label} roots drifted")
    is_v3 = (
        _mapping(contract["profile"], "focused profile").get("profile_id")
        in _FCRASH_H_V3_PROFILE_IDS | _FAULT_WINDOW_PROFILE_IDS
    )
    members = tuple(_integer(member, "tree member") for member in contract["members"])
    selected_targets = tuple(contract["targets"] if selected is None else selected)
    first_leaf = (len(members) - 2) // int(contract["fanout"]) + 1
    for tree, root in zip(decoded.trees, roots, strict=True):
        if (
            tree.fanout != contract["fanout"]
            or tree.pipeline_stretch != contract["pipeline_stretch"]
            or tuple(tree.wait_exempt) != selected_targets
        ):
            _error(f"{label} native placement structure drifted")
        if is_v3:
            if (
                len(tree.members) != len(members)
                or set(tree.members) != set(members)
                or tuple(tree.members[:1]) != (root,)
                or any(
                    target not in tree.members[first_leaf:]
                    for target in selected_targets
                )
            ):
                _error(f"{label} native placement structure drifted")
        elif tuple(tree.members) != _canonical_tree_members(
            root, contract, selected_targets
        ):
            _error(f"{label} native placement structure drifted")


def _containment_roots(
    ranked_ids: Sequence[int], contract: Mapping[str, object]
) -> tuple[int, ...]:
    """Mirror native containment placement without reordering healthy roots."""

    baseline = tuple(range(int(contract["quorum"])))
    eligible = tuple(
        _integer(replica, "containment ranked replica") for replica in ranked_ids
    )
    if len(set(eligible)) != len(eligible):
        _error("containment ranking duplicates an eligible replica")
    preserved = {root for root in baseline if root in eligible}
    replacement_ids = tuple(replica for replica in eligible if replica not in preserved)
    if (
        _is_v6_contract(contract)
        or _is_v7_contract(contract)
        or _is_v8_or_v9_contract(contract)
    ):
        # v6 freezes the native containment fallback independently of scorer
        # order.  Healthy baseline roots still retain their tree slots.
        replacement_ids = tuple(sorted(replacement_ids))
    replacements = iter(replacement_ids)
    roots: list[int] = []
    for root in baseline:
        if root in preserved:
            roots.append(root)
            continue
        try:
            roots.append(next(replacements))
        except StopIteration as exc:
            raise FocusedCrashPairValidationError(
                "containment ranking cannot fill every baseline root slot"
            ) from exc
    if len(set(roots)) != len(roots):
        _error("containment placement repeats a root")
    return tuple(roots)


def _v9_optimization_roots(
    ranked_ids: Sequence[int],
    inherited_wait_exempt: Sequence[int],
    contract: Mapping[str, object],
    *,
    enforce_expected_nonresponses: bool = True,
) -> tuple[int, ...]:
    """Reconstruct native recurring roots while preserving inherited leaves."""

    inherited = tuple(
        _integer(replica, "inherited wait-exempt member")
        for replica in inherited_wait_exempt
    )
    members = tuple(int(replica) for replica in contract["members"])
    quorum = int(contract["quorum"])
    if (
        inherited != tuple(sorted(set(inherited)))
        or (
            enforce_expected_nonresponses
            and not set(contract["targets"]).issubset(inherited)
        )
        or not set(inherited).issubset(members)
        or len(inherited) > len(members) - quorum
    ):
        _error("v9 optimization inherited cohort drifted")
    roots = tuple(replica for replica in ranked_ids if replica not in set(inherited))[
        :quorum
    ]
    if len(roots) != quorum:
        _error("v9 optimization lacks Q unconstrained responsive roots")
    return roots


def _command_payload(decoded: Any, payload: Mapping[str, Any]) -> dict[str, object]:
    height = _integer(payload.get("command_block_height"), "command block height")
    delay = decoded.command.activation_delay_blocks
    expected = {
        "command_block_height": height,
        "command_block_hash": _digest(
            payload.get("command_block_hash"), "command hash"
        ),
        "payload_digest": decoded.command.payload_digest,
        "predecessor_epoch_number": decoded.epoch_number - 1,
        "predecessor_epoch_digest": decoded.previous_epoch_digest,
        "successor_epoch_number": decoded.epoch_number,
        "successor_epoch_digest": decoded.epoch_digest,
        "activation_delay_blocks": delay,
        "activation_height": height + delay,
    }
    if dict(payload) != expected:
        _error("committed epoch command differs from the native bundle")
    return expected


def _validate_transition(
    events: Sequence[Mapping[str, Any]],
    decoded: Any,
    contract: Mapping[str, object],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    commands = [
        event
        for event in events
        if event["event_type"] == "epoch.command_committed"
        and event["payload"].get("successor_epoch_number") == decoded.epoch_number
    ]
    activations = [
        event
        for event in events
        if event["event_type"] == "epoch.activated"
        and event["payload"].get("epoch_number") == decoded.epoch_number
    ]
    command_sources = {str(event["source_id"]) for event in commands}
    activation_sources = {str(event["source_id"]) for event in activations}
    expected_sources = {f"replica-{replica}" for replica in contract["survivors"]}
    if (
        len(commands) != len(activations) != 0
        or len(commands) != len(contract["survivors"])
        or command_sources != expected_sources
        or activation_sources != expected_sources
    ):
        _error("transition does not contain one command and activation per survivor")
    expected_command: dict[str, object] | None = None
    for event in commands:
        parsed = _command_payload(decoded, _mapping(event["payload"], "command"))
        if expected_command is None:
            expected_command = parsed
        elif parsed != expected_command:
            _error("survivors disagree on the epoch command")
    assert expected_command is not None
    expected_activation = {
        "epoch_number": decoded.epoch_number,
        "tree_id": 0,
        "epoch_digest": decoded.epoch_digest,
        "activation_height": expected_command["activation_height"],
    }
    commands_by_source = {str(event["source_id"]): event for event in commands}
    for activation in activations:
        if dict(activation["payload"]) != expected_activation:
            _error("survivor activation identity drifted")
        command = commands_by_source[str(activation["source_id"])]
        if int(activation["source_monotonic_ns"]) <= int(
            command["source_monotonic_ns"]
        ):
            _error("survivor activation precedes its command")
    return commands, activations


def _observation_id(observation: Mapping[str, Any]) -> str:
    configuration = _mapping(observation.get("configuration"), "observation config")
    outcome = observation.get("outcome")
    outcome_code = 1 if outcome in {"on_time", "timeout"} else -1
    if outcome_code < 0:
        _error("evidence observation outcome is unsupported")
    payload = b"".join(
        (
            b"kauri-response-observation-v1",
            _integer(observation.get("reporter_id"), "reporter").to_bytes(2, "big"),
            _integer(
                observation.get("observed_replica_id"), "observed replica"
            ).to_bytes(2, "big"),
            _integer(configuration.get("epoch_number"), "observation epoch").to_bytes(
                4, "big"
            ),
            _integer(configuration.get("tree_id"), "observation tree").to_bytes(
                4, "big"
            ),
            bytes.fromhex(
                _digest(configuration.get("epoch_digest"), "observation epoch digest")
            ),
            bytes.fromhex(
                _digest(observation.get("block_hash"), "observation block hash")
            ),
            outcome_code.to_bytes(1, "big"),
        )
    )
    return _sha_bytes(payload)


def reconstruct_focused_ranking(
    events: Sequence[Mapping[str, Any]],
    *,
    membership_replica_ids: Sequence[int],
    predecessor_epoch_number: int,
    predecessor_epoch_digest: str,
    baseline_evidence_cutoff: int,
    current_evidence_cutoff: int,
    policy: Mapping[str, Any],
    seed: int,
    suffix_only: bool,
    allowed_schema_versions: Collection[int] = frozenset({1}),
    minimum_attempt_start_monotonic_ns: int | None = None,
) -> dict[str, object]:
    """Replay the native scorer and expose its exact eligible ordering."""

    try:
        replay = factorial_validation.replay_native_adaptation_snapshot(
            events,
            membership_replica_ids=membership_replica_ids,
            predecessor_epoch_number=predecessor_epoch_number,
            predecessor_epoch_digest=predecessor_epoch_digest,
            baseline_evidence_cutoff=baseline_evidence_cutoff,
            current_evidence_cutoff=current_evidence_cutoff,
            policy=policy,
            seed=seed,
            suffix_only=suffix_only,
            allowed_schema_versions=allowed_schema_versions,
            minimum_attempt_start_monotonic_ns=minimum_attempt_start_monotonic_ns,
        )
    except factorial_validation.FactorialValidationError as exc:
        raise FocusedCrashPairValidationError(
            "native adaptation snapshot replay rejected"
        ) from exc
    ranking = tuple(
        _mapping(row, "native ranking row")
        for row in _sequence(replay.get("ranking"), "native ranking")
    )
    minimum_attempts = _integer(
        policy.get("minimum_attempts"), "ranking minimum attempts", 1
    )
    if any(
        row.get("eligible") is True
        and _integer(row.get("attempt_count"), "ranking attempt count")
        < minimum_attempts
        for row in ranking
    ):
        _error("ranking contains a replica below the minimum attempt count")
    return {
        **dict(replay),
        "ranked_ids": [
            _integer(row.get("replica_id"), "ranked replica")
            for row in ranking
            if row.get("eligible") is True
        ],
    }


def epoch_structural_projection(decoded: Any) -> dict[str, object]:
    """Project only matched, arm-independent native Epoch 1 structure."""

    return {
        "issuer_id": decoded.command.issuer_id,
        "successor_epoch_number": decoded.command.successor_epoch_number,
        "command_predecessor_epoch_digest": decoded.command.predecessor_epoch_digest,
        "activation_delay_blocks": decoded.command.activation_delay_blocks,
        "epoch_number": decoded.epoch_number,
        "previous_epoch_digest": decoded.previous_epoch_digest,
        "membership_digest": decoded.membership_digest,
        "generation_seed": decoded.generation_seed,
        "policy_version": decoded.policy_version,
        "trees": [asdict(tree) for tree in decoded.trees],
    }


def _ranking(
    events: Sequence[Mapping[str, Any]],
    epoch1: Any,
    contract: Mapping[str, object],
    *,
    predecessor_epoch: int,
    inherited_wait_exempt: Sequence[int] | None = None,
    enforce_expected_nonresponses: bool = True,
) -> tuple[
    list[int],
    list[str],
    tuple[int, ...],
    str | None,
    int | None,
    int | None,
    tuple[int, ...],
]:
    manager_events = [
        event for event in events if event["source_kind"] == "adaptation_manager"
    ]
    accepted = [
        event
        for event in manager_events
        if event["event_type"] == "evidence.observation_accepted"
        and _mapping(
            _mapping(
                _mapping(event["payload"], "accepted evidence payload").get(
                    "observation"
                ),
                "accepted observation",
            ).get("configuration"),
            "observation configuration",
        ).get("epoch_number")
        == predecessor_epoch
    ]
    members = tuple(contract["members"])
    profile_id = _mapping(contract["profile"], "focused profile").get("profile_id")
    if (
        not enforce_expected_nonresponses
        and profile_id not in _FCRASH_H_V13_PROFILE_IDS
    ):
        _error("source-blind ranking mode is reserved for v13")
    survivors = (
        tuple(contract["survivors"])
        if enforce_expected_nonresponses
        else ()
    )
    expected_targets = (
        tuple(contract["targets"])
        if enforce_expected_nonresponses
        else ()
    )
    if not accepted:
        _error("ranking evidence is absent")
    has_snapshot_audit = any(
        event["event_type"] == "adaptive_v2_evidence_snapshot"
        for event in manager_events
    )
    replay_snapshot_id: str | None = None
    replay_cutoff: int | None = None
    replay_audit_ns: int | None = None
    audited_eligible_ranking: tuple[int, ...] | None = None
    if has_snapshot_audit:
        all_audit_events = [
            event
            for event in manager_events
            if event["event_type"] == "adaptive_v2_evidence_snapshot"
        ]
        v7_arm_start_ns: int | None = None
        if _is_v7_contract(contract) or _is_v8_or_v9_contract(contract):
            armed = [
                event
                for event in manager_events
                if event["event_type"] == "fault_window_armed"
            ]
            if len(armed) != 1:
                _error("v7 ranking lacks one armed causal boundary")
            v7_arm_start_ns = _integer(
                _mapping(armed[0]["payload"], "fault-window arm").get(
                    "evidence_start_monotonic_ns"
                ),
                "v7 ranking causal boundary",
                1,
            )
        audit_events = [
            event
            for event in all_audit_events
            if _mapping(event["payload"], "native ranking audit").get(
                "predecessor_epoch_number"
            )
            == predecessor_epoch
        ]
        if len(audit_events) != 1:
            _error("ranking evidence lacks one audit for the selected predecessor")
        audit = _mapping(audit_events[0]["payload"], "native ranking audit")
        audited_eligible_ranking = tuple(
            _integer(replica, "native audit eligible replica")
            for replica in _sequence(
                audit.get("eligible_ranking"), "native audit eligible ranking"
            )
        )
        replay_audit_ns = _integer(
            audit_events[0]["source_monotonic_ns"], "native ranking audit timestamp"
        )
        audited_epoch = _integer(
            audit.get("predecessor_epoch_number"), "ranking predecessor epoch"
        )
        if audited_epoch == 0:
            replay_digest = str(contract["epoch_zero_digest"])
            baseline_cutoff = _integer(
                audit.get("baseline_cutoff"), "ranking baseline cutoff"
            )
            suffix_only = False
        elif audited_epoch == 1:
            replay_digest = epoch1.epoch_digest
            baseline_cutoff = _integer(
                audit.get("baseline_cutoff"), "ranking baseline cutoff"
            )
            suffix_only = True
        else:
            _error("ranking audit is not bound to Epoch 0 or Epoch 1")
        current_cutoff = _integer(
            audit.get("current_cutoff"), "ranking current cutoff", 1
        )
        causal_start_ns = v7_arm_start_ns if audited_epoch == 0 else None
        responsiveness_policy = _native_responsiveness_policy(
            _mapping(contract.get("profile"), "focused profile").get("profile_id")
        )
        replay = reconstruct_focused_ranking(
            manager_events,
            membership_replica_ids=members,
            predecessor_epoch_number=audited_epoch,
            predecessor_epoch_digest=replay_digest,
            baseline_evidence_cutoff=baseline_cutoff,
            current_evidence_cutoff=current_cutoff,
            policy=responsiveness_policy,
            seed=_integer(epoch1.generation_seed, "Epoch 1 generation seed"),
            suffix_only=suffix_only,
            allowed_schema_versions=(
                frozenset({3})
                if (
                    _is_v6_contract(contract)
                    or _is_v7_contract(contract)
                    or _is_v8_or_v9_contract(contract)
                )
                else frozenset({1})
            ),
            minimum_attempt_start_monotonic_ns=causal_start_ns,
        )
        replay_snapshot_id = _digest(
            replay.get("snapshot_id"), "ranking replay snapshot ID"
        )
        replay_cutoff = current_cutoff
        for other_epoch in {0, 1} - {audited_epoch}:
            other_audits = [
                _mapping(candidate["payload"], "native ranking audit")
                for candidate in all_audit_events
                if _mapping(candidate["payload"], "native ranking audit").get(
                    "predecessor_epoch_number"
                )
                == other_epoch
            ]
            if len(other_audits) > 1:
                _error("ranking evidence duplicates a predecessor audit")
            if not other_audits:
                continue
            other_audit = other_audits[0]
            reconstruct_focused_ranking(
                manager_events,
                membership_replica_ids=members,
                predecessor_epoch_number=other_epoch,
                predecessor_epoch_digest=(
                    str(contract["epoch_zero_digest"])
                    if other_epoch == 0
                    else epoch1.epoch_digest
                ),
                baseline_evidence_cutoff=_integer(
                    other_audit.get("baseline_cutoff"),
                    "ranking baseline cutoff",
                ),
                current_evidence_cutoff=_integer(
                    other_audit.get("current_cutoff"),
                    "ranking current cutoff",
                    1,
                ),
                policy=responsiveness_policy,
                seed=_integer(epoch1.generation_seed, "Epoch 1 generation seed"),
                suffix_only=other_epoch == 1,
                allowed_schema_versions=(
                    frozenset({3})
                    if (
                        _is_v6_contract(contract)
                        or _is_v7_contract(contract)
                        or _is_v8_or_v9_contract(contract)
                    )
                    else frozenset({1})
                ),
                minimum_attempt_start_monotonic_ns=(
                    v7_arm_start_ns if other_epoch == 0 else None
                ),
            )
        ranked = list(replay["ranked_ids"])
    else:
        if len(accepted) != len(members):
            _error("ranking evidence lacks native replay audit and full membership")
        observed: set[int] = set()
        responsive: list[tuple[int, int]] = []
        for ingestion_sequence, event in enumerate(accepted, start=1):
            payload = _mapping(event["payload"], "accepted evidence payload")
            observation = _mapping(payload.get("observation"), "accepted observation")
            replica = _integer(
                observation.get("observed_replica_id"), "observed replica"
            )
            configuration = _mapping(
                observation.get("configuration"), "observation configuration"
            )
            if (
                payload.get("ingestion_sequence") != ingestion_sequence
                or replica in observed
                or replica not in members
                or configuration.get("epoch_number") != 1
                or configuration.get("tree_id") != 0
                or configuration.get("epoch_digest") != epoch1.epoch_digest
                or observation.get("observation_id") != _observation_id(observation)
            ):
                _error("legacy ranking evidence identity drifted")
            observed.add(replica)
            if observation.get("outcome") == "on_time":
                responsive.append(
                    (
                        _integer(
                            observation.get("response_duration_us"),
                            "response duration",
                        ),
                        replica,
                    )
                )
            elif observation.get("response_duration_us") != 0:
                _error("timeout observation contains a response duration")
        if observed != set(members):
            _error("legacy ranking evidence membership drifted")
        ranked = [replica for _latency, replica in sorted(responsive)]
    timeout_targets = tuple(sorted(set(members) - set(ranked)))
    if _is_v9_contract(contract):
        if (
            len(timeout_targets)
            < (
                len(expected_targets)
                if enforce_expected_nonresponses
                else _integer(
                    contract.get("fault_target_count"),
                    "source-blind fault cardinality",
                    1,
                )
            )
            or len(timeout_targets) > len(members) - int(contract["quorum"])
            or (
                enforce_expected_nonresponses
                and not set(expected_targets).issubset(timeout_targets)
            )
            or len(ranked) < int(contract["quorum"])
        ):
            _error("v9 ranking does not reconstruct a guarded nonresponsive cohort")
    elif len(ranked) != len(survivors) or timeout_targets != expected_targets:
        _error("ranking eligibility does not identify exactly the focused nonresponses")
    expected_audit_roots = (
        (
            _containment_roots(ranked, contract)
            if profile_id in _FCRASH_H_V3_PROFILE_IDS | _FAULT_WINDOW_PROFILE_IDS
            else tuple(range(int(contract["quorum"])))
        )
        if predecessor_epoch == 0
        else (
            _v9_optimization_roots(
                ranked,
                inherited_wait_exempt or (),
                contract,
                enforce_expected_nonresponses=enforce_expected_nonresponses,
            )
            if _is_v9_contract(contract)
            else tuple(ranked[: int(contract["quorum"])])
        )
    )
    if (
        audited_eligible_ranking is not None
        and profile_id in _FCRASH_H_V3_PROFILE_IDS | _FAULT_WINDOW_PROFILE_IDS
    ):
        if audited_eligible_ranking != expected_audit_roots:
            _error("native audit eligible ranking drifted")
    observation_ids = sorted(
        str(
            _mapping(
                _mapping(event["payload"], "accepted evidence payload").get(
                    "observation"
                ),
                "accepted observation",
            ).get("observation_id")
        )
        for event in accepted
    )
    return (
        ranked,
        observation_ids,
        timeout_targets,
        replay_snapshot_id,
        replay_cutoff,
        replay_audit_ns,
        expected_audit_roots,
    )


def _common_commit_key(
    payload: Mapping[str, Any], contract: Mapping[str, object]
) -> tuple[object, ...]:
    fields = (
        "block_height",
        "block_hash",
        "parent_hash",
        "transaction_count",
    )
    profile = _mapping(contract.get("profile"), "focused profile")
    if profile.get("profile_id") not in _FCRASH_H_V13_PROFILE_IDS:
        fields = (*fields, "commit_batch_index")
    return tuple(payload.get(field) for field in fields)


def _select_latest_common_commit(
    commits: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    contract: Mapping[str, object],
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    survivor_sources = {f"replica-{replica}" for replica in contract["survivors"]}
    observations_by_commit: dict[
        tuple[object, ...], list[Mapping[str, Any]]
    ] = {}
    for event in observations:
        if (
            event.get("source_kind") != "replica"
            or event.get("source_id") not in survivor_sources
        ):
            continue
        payload = _mapping(event.get("payload"), "common commit observation")
        observations_by_commit.setdefault(
            _common_commit_key(payload, contract), []
        ).append(event)
    eligible: list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = []
    for commit in commits:
        payload = _mapping(commit["payload"], "authoritative commit")
        matching = observations_by_commit.get(
            _common_commit_key(payload, contract), []
        )
        if len({str(event["source_id"]) for event in matching}) >= int(
            contract["quorum"]
        ):
            eligible.append((commit, matching))
    if not eligible:
        _error("common commit does not contain matching survivor witnesses")
    latest_height = max(
        _integer(
            _mapping(commit["payload"], "authoritative commit").get("block_height"),
            "commit height",
            1,
        )
        for commit, _matching in eligible
    )
    latest = [
        item
        for item in eligible
        if _mapping(item[0]["payload"], "authoritative commit").get("block_height")
        == latest_height
    ]
    if len(latest) != 1:
        _error("latest common commit identity is ambiguous")
    return latest[0]


def _event_epoch(event: Mapping[str, Any], label: str) -> int:
    payload = _mapping(event.get("payload"), label)
    proof = _mapping(payload.get("decision_proof"), f"{label} proof")
    return _integer(proof.get("epoch_number"), f"{label} epoch")


def _first_common_commit_anchor(
    commits: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    *,
    epoch_number: int,
    after_ns: int,
    contract: Mapping[str, object],
) -> int:
    survivor_sources = {f"replica-{replica}" for replica in contract["survivors"]}
    earliest_by_commit: dict[tuple[object, ...], dict[str, int]] = {}
    for observation in observations:
        source = str(observation.get("source_id"))
        if observation.get("source_kind") != "replica" or source not in survivor_sources:
            continue
        timestamp = _integer(
            observation.get("source_monotonic_ns"),
            "phase commit observation timestamp",
        )
        if timestamp <= after_ns:
            continue
        payload = _mapping(
            observation.get("payload"), "phase commit observation"
        )
        by_source = earliest_by_commit.setdefault(
            _common_commit_key(payload, contract), {}
        )
        previous = by_source.get(source)
        if previous is None or timestamp < previous:
            by_source[source] = timestamp
    candidates = sorted(
        (
            event
            for event in commits
            if _event_epoch(event, "phase commit") == epoch_number
            and _integer(event.get("source_monotonic_ns"), "phase commit timestamp")
            > after_ns
        ),
        key=lambda event: (
            _integer(event.get("source_monotonic_ns"), "phase commit timestamp"),
            _integer(
                _mapping(event.get("payload"), "phase commit").get("block_height"),
                "phase commit height",
                1,
            ),
        ),
    )
    for commit in candidates:
        payload = _mapping(commit.get("payload"), "phase commit")
        commit_key = _common_commit_key(payload, contract)
        earliest_by_source = earliest_by_commit.get(commit_key, {})
        if len(earliest_by_source) < int(contract["quorum"]):
            continue
        quorum_times = sorted(earliest_by_source.values())[: int(contract["quorum"])]
        return max(
            _integer(commit.get("source_monotonic_ns"), "phase commit timestamp"),
            max(quorum_times),
        )
    _error("causal phase lacks a post-activation common commit")


def _v5_causal_phase_windows(
    root: Path,
    events: Sequence[Mapping[str, Any]],
    authoritative_commits: Sequence[Mapping[str, Any]],
    epoch2: Any | None,
    contract: Mapping[str, object],
) -> list[tuple[str, int, int, int]]:
    raw_contract = _mapping(
        contract.get("phase_window_contract"), "phase-window contract"
    )
    if (
        set(raw_contract)
        != {
            "schema_version",
            "domain",
            "stabilization_offset_seconds",
            "control_optimization_hold_seconds",
        }
        or raw_contract.get("schema_version") != 1
        or raw_contract.get("domain") != "kauri-focused-causal-phase-windows-v1"
    ):
        _error("causal phase-window contract drifted")
    width_ns = _integer(contract.get("bucket_width_seconds"), "bucket width", 1)
    width_ns *= 1_000_000_000
    stable_seconds = _integer(
        _mapping(
            _mapping(contract.get("profile"), "focused profile").get("timers"),
            "profile timers",
        ).get("stable_phase_seconds"),
        "stable phase",
        1,
    )
    phase_duration_ns = (
        stable_seconds * 1_000_000_000 if _is_v9_contract(contract) else width_ns
    )
    if phase_duration_ns % width_ns != 0:
        _error("causal stable phase is not an exact bucket multiple")
    stabilization_ns = (
        _integer(
            raw_contract.get("stabilization_offset_seconds"),
            "phase stabilization offset",
            1,
        )
        * 1_000_000_000
    )
    control_hold_ns = (
        _integer(
            raw_contract.get("control_optimization_hold_seconds"),
            "control optimization hold",
            1,
        )
        * 1_000_000_000
    )

    fault_receipt = _read_json(root / "raw" / "fault-receipt.json", "fault receipt")
    outcomes = _sequence(fault_receipt.get("sigkill_outcomes"), "SIGKILL outcomes")
    if not outcomes:
        _error("causal phase windows lack fault outcomes")
    prefault_ns = min(
        _integer(
            _mapping(outcome, "SIGKILL outcome").get("requested_monotonic_ns"),
            "fault request timestamp",
            1,
        )
        for outcome in outcomes
    )
    fault_ns = max(
        _integer(
            _mapping(outcome, "SIGKILL outcome").get("confirmed_monotonic_ns"),
            "fault confirmation timestamp",
            1,
        )
        for outcome in outcomes
    )
    baseline_start = prefault_ns - phase_duration_ns
    if baseline_start < 0:
        _error("causal baseline window precedes the event clock")
    epoch0_commits = [
        event
        for event in authoritative_commits
        if _event_epoch(event, "phase commit") == 0
    ]
    if not epoch0_commits:
        _error("causal phase windows lack Epoch-0 commits")
    if (
        min(
            _integer(event.get("source_monotonic_ns"), "Epoch-0 commit timestamp")
            for event in epoch0_commits
        )
        > prefault_ns - stable_seconds * 1_000_000_000
    ):
        _error("causal baseline is not inside the proven stable interval")

    command1_times = [
        _integer(event.get("source_monotonic_ns"), "Epoch-1 command timestamp")
        for event in events
        if event.get("event_type") == "epoch.command_committed"
        and _mapping(event.get("payload"), "Epoch-1 command").get(
            "successor_epoch_number"
        )
        == 1
    ]
    if not command1_times or fault_ns + phase_duration_ns >= min(command1_times):
        _error("causal fault window overlaps the Epoch-1 transition")
    activations1 = [
        event
        for event in events
        if event.get("event_type") == "epoch.activated"
        and _mapping(event.get("payload"), "Epoch-1 activation").get("epoch_number")
        == 1
    ]
    expected_sources = {f"replica-{replica}" for replica in contract["survivors"]}
    if {str(event.get("source_id")) for event in activations1} != expected_sources:
        _error("causal Epoch-1 phase lacks every survivor activation")
    activation1_ns = max(
        _integer(event.get("source_monotonic_ns"), "Epoch-1 activation timestamp")
        for event in activations1
    )
    observations = [
        event for event in events if event.get("event_type") == "block.commit_observed"
    ]
    common1_ns = _first_common_commit_anchor(
        authoritative_commits,
        observations,
        epoch_number=1,
        after_ns=activation1_ns,
        contract=contract,
    )
    if _is_v10_contract(contract):
        timing = _mapping(
            _mapping(contract.get("profile"), "focused profile")
            .get("transitions"),
            "profile transitions",
        )
        timing = _mapping(timing.get("adaptive_timing_contract"), "v10 timing")
        anchor_deadline_ns = _integer(
            timing.get("epoch1_common_commit_anchor_deadline_seconds"),
            "v10 common commit anchor deadline",
            1,
        ) * 1_000_000_000
        if common1_ns > activation1_ns + anchor_deadline_ns:
            _error("v10 Epoch-1 common commit exceeded its activation anchor bound")
    epoch1_start = max(activation1_ns, common1_ns) + stabilization_ns
    epoch1_end = epoch1_start + phase_duration_ns

    windows: list[tuple[str, int, int, int]] = [
        ("baseline", baseline_start, prefault_ns, 0),
        ("fault", fault_ns, fault_ns + phase_duration_ns, 0),
        ("epoch1", epoch1_start, epoch1_end, 1),
    ]
    if epoch2 is not None:
        command2_times = [
            _integer(event.get("source_monotonic_ns"), "Epoch-2 command timestamp")
            for event in events
            if event.get("event_type") == "epoch.command_committed"
            and _mapping(event.get("payload"), "Epoch-2 command").get(
                "successor_epoch_number"
            )
            == 2
        ]
        if not command2_times or epoch1_end >= min(command2_times):
            _error("causal Epoch-1 window overlaps the Epoch-2 transition")
        activations2 = [
            event
            for event in events
            if event.get("event_type") == "epoch.activated"
            and _mapping(event.get("payload"), "Epoch-2 activation").get("epoch_number")
            == 2
        ]
        if {str(event.get("source_id")) for event in activations2} != expected_sources:
            _error("causal late phase lacks every survivor Epoch-2 activation")
        activation2_ns = max(
            _integer(event.get("source_monotonic_ns"), "Epoch-2 activation timestamp")
            for event in activations2
        )
        common2_ns = _first_common_commit_anchor(
            authoritative_commits,
            observations,
            epoch_number=2,
            after_ns=activation2_ns,
            contract=contract,
        )
        late_start = max(activation2_ns, common2_ns) + stabilization_ns
        late_epoch = 2
    else:
        late_start = epoch1_end + control_hold_ns
        late_epoch = 1
    windows.append(("late", late_start, late_start + phase_duration_ns, late_epoch))

    if tuple(name for name, _start, _end, _epoch in windows) != tuple(
        contract["phase_names"]
    ):
        _error("causal phase identities drifted")
    if any(right[1] < left[2] for left, right in zip(windows, windows[1:])):
        _error("causal phase windows overlap")
    for phase, start, end, expected_epoch in windows:
        phase_commits = [
            event
            for event in authoritative_commits
            if start
            <= _integer(event.get("source_monotonic_ns"), "phase commit timestamp")
            < end
        ]
        if (
            (phase != "fault" and not phase_commits)
            or any(
                _event_epoch(event, f"{phase} commit") != expected_epoch
                for event in phase_commits
            )
            or (
                phase != "fault"
                and sum(
                    _integer(
                        _mapping(event.get("payload"), f"{phase} commit").get(
                            "transaction_count"
                        ),
                        f"{phase} transactions",
                    )
                    for event in phase_commits
                )
                <= 0
            )
        ):
            _error("causal phase transactions or exact epoch drifted")
    return windows


def _complete_phase_buckets(
    commits: Sequence[Mapping[str, Any]],
    *,
    start_ns: int,
    end_ns: int,
    bucket_width_ns: int,
) -> tuple[list[dict[str, int]], int]:
    """Reconstruct every fixed-width bucket and its exact conventional median."""

    if (
        bucket_width_ns <= 0
        or end_ns <= start_ns
        or (end_ns - start_ns) % bucket_width_ns != 0
    ):
        _error("scientific phase is not an exact complete-bucket interval")
    buckets: list[dict[str, int]] = []
    for index in range((end_ns - start_ns) // bucket_width_ns):
        bucket_start = start_ns + index * bucket_width_ns
        bucket_end = bucket_start + bucket_width_ns
        transactions = sum(
            _integer(
                _mapping(event.get("payload"), "bucket commit").get(
                    "transaction_count"
                ),
                "bucket transactions",
            )
            for event in commits
            if bucket_start
            <= _integer(event.get("source_monotonic_ns"), "bucket commit timestamp")
            < bucket_end
        )
        buckets.append(
            {
                "bucket_index": index,
                "start_ns": bucket_start,
                "end_ns": bucket_end,
                "transactions": transactions,
                "mean_milli_tps": transactions * 1_000_000_000_000 // bucket_width_ns,
            }
        )
    throughputs = sorted(bucket["mean_milli_tps"] for bucket in buckets)
    middle = len(throughputs) // 2
    if len(throughputs) % 2:
        median = throughputs[middle]
    else:
        median_sum = throughputs[middle - 1] + throughputs[middle]
        if median_sum % 2:
            _error("scientific phase median is not an integral milli-TPS value")
        median = median_sum // 2
    return buckets, median


_V13_COMMIT_PHYSICAL_KEYS = {
    "block_height",
    "block_hash",
    "parent_hash",
    "transaction_count",
    "commit_batch_index",
}
_V13_COMMIT_PROOF_KEYS = {
    "epoch_number",
    "tree_id",
    "epoch_digest",
    "block_hash",
}


def _v13_commit_physical_projection(
    payload: Mapping[str, object], label: str
) -> dict[str, object]:
    if not _V13_COMMIT_PHYSICAL_KEYS.issubset(payload):
        _error(f"{label} physical schema drifted")
    parent = payload.get("parent_hash")
    return {
        "block_height": _uint64(payload.get("block_height"), f"{label} height", 1),
        "block_hash": _digest(payload.get("block_hash"), f"{label} hash"),
        "parent_hash": _digest(parent, f"{label} parent hash"),
        "transaction_count": _uint64(
            payload.get("transaction_count"), f"{label} transactions"
        ),
        "commit_batch_index": _uint64(
            payload.get("commit_batch_index"), f"{label} batch"
        ),
    }


def _v13_commit_identity_projection(
    payload: Mapping[str, object], label: str
) -> tuple[dict[str, object], int]:
    proof = _mapping(payload.get("decision_proof"), f"{label} decision proof")
    if set(proof) != _V13_COMMIT_PROOF_KEYS:
        _error(f"{label} decision proof schema drifted")
    block_hash = _digest(proof.get("block_hash"), f"{label} proof block hash")
    identity = {
        "epoch_number": _uint64(proof.get("epoch_number"), f"{label} epoch"),
        "tree_id": _integer(proof.get("tree_id"), f"{label} tree"),
        "epoch_digest": _digest(
            proof.get("epoch_digest"), f"{label} epoch digest"
        ),
        "block_hash": block_hash,
    }
    generation = _uint64(
        payload.get("view_generation"), f"{label} view generation", 1
    )
    if block_hash != payload.get("block_hash"):
        _error(f"{label} proof does not bind its committed block")
    return identity, generation


def _v13_commit_cross_source_projection(
    physical: Mapping[str, object],
) -> tuple[object, object, object, object]:
    """Project only fields that are invariant across replica-local commits.

    ``commit_batch_index`` is the position within one replica's local decide
    batch.  It remains exact for the adjacent observation/carrier check, but
    it is not a physical block identity and can legitimately differ between
    replicas committing the same block.
    """

    return (
        physical["block_height"],
        physical["block_hash"],
        physical["parent_hash"],
        physical["transaction_count"],
    )


def _v13_reconstruct_authoritative_commits(
    events: Sequence[Mapping[str, Any]], contract: Mapping[str, object]
) -> list[dict[str, Any]]:
    """Join designated physical commits to exact, non-authoritative identities."""

    authoritative_source = str(contract["authoritative_source_id"])
    member_sources = {f"replica-{member}" for member in contract["members"]}
    instances = {
        source: _authoritative_lifecycle_instance(events, source)
        for source in member_sources
    }
    observed = [
        event
        for event in events
        if event.get("event_type") == "block.commit_observed"
    ]
    exact = [
        event for event in events if event.get("event_type") == "block.committed"
    ]
    witnesses = [
        event
        for event in events
        if event.get("event_type") == "block.commit_identity_witness"
    ]
    carriers = [*exact, *witnesses]

    for event in [*observed, *carriers]:
        source = str(event.get("source_id"))
        if (
            event.get("source_kind") != "replica"
            or source not in member_sources
            or event.get("source_instance") != instances[source]
        ):
            _error("v13 commit evidence is not member and lifecycle bound")

    observed_by_source: dict[str, list[Mapping[str, Any]]] = {}
    observed_by_source_physical: dict[
        tuple[str, tuple[object, object, object, object, object]],
        list[Mapping[str, Any]],
    ] = {}
    observed_physical: dict[int, dict[str, object]] = {}
    for event in observed:
        payload = _mapping(event.get("payload"), "v13 commit observation")
        if set(payload) != _V13_COMMIT_PHYSICAL_KEYS:
            _error("v13 commit observation schema drifted")
        physical = _v13_commit_physical_projection(
            payload, "v13 commit observation"
        )
        observed_physical[id(event)] = physical
        source = str(event["source_id"])
        observed_by_source.setdefault(source, []).append(event)
        local_key = (
            *_v13_commit_cross_source_projection(physical),
            physical["commit_batch_index"],
        )
        observed_by_source_physical.setdefault((source, local_key), []).append(event)

    carrier_physical: dict[int, dict[str, object]] = {}
    carrier_identity: dict[int, tuple[dict[str, object], int]] = {}
    carriers_by_physical: dict[
        tuple[object, object, object, object], list[Mapping[str, Any]]
    ] = {}
    for event in carriers:
        name = str(event.get("event_type"))
        payload = _mapping(event.get("payload"), "v13 commit identity carrier")
        expected_keys = _V13_COMMIT_PHYSICAL_KEYS | {
            "decision_proof",
            "view_generation",
        }
        if name == "block.committed":
            expected_keys = expected_keys | {"designated_observer"}
            if (
                event.get("source_id") != authoritative_source
                or payload.get("designated_observer") is not True
            ):
                _error("v13 authoritative commit source or designation drifted")
        elif name == "block.commit_identity_witness":
            if event.get("source_id") == authoritative_source:
                _error("v13 designated source emitted a non-authoritative witness")
        else:  # pragma: no cover - carriers are selected by an exact allowlist
            _error("v13 commit identity carrier type drifted")
        if set(payload) != expected_keys:
            _error("v13 commit identity carrier schema drifted")

        physical = _v13_commit_physical_projection(
            payload, "v13 commit identity carrier"
        )
        identity = _v13_commit_identity_projection(
            payload, "v13 commit identity carrier"
        )
        carrier_physical[id(event)] = physical
        carrier_identity[id(event)] = identity
        source = str(event["source_id"])
        local_key = (
            *_v13_commit_cross_source_projection(physical),
            physical["commit_batch_index"],
        )
        matches = observed_by_source_physical.get((source, local_key), ())
        if len(matches) != 1:
            _error("v13 commit identity carrier lacks one local observation")
        observation = matches[0]
        observation_sequence = _integer(
            observation.get("source_sequence"), "v13 commit observation sequence", 1
        )
        carrier_sequence = _integer(
            event.get("source_sequence"), "v13 commit identity sequence", 1
        )
        observation_time = _uint64(
            observation.get("source_monotonic_ns"),
            "v13 commit observation time",
            1,
        )
        carrier_time = _uint64(
            event.get("source_monotonic_ns"), "v13 commit identity time", 1
        )
        if (
            carrier_sequence != observation_sequence + 1
            or carrier_time < observation_time
        ):
            _error("v13 commit identity does not immediately follow its observation")
        carriers_by_physical.setdefault(
            _v13_commit_cross_source_projection(physical), []
        ).append(event)

    authoritative_observations = observed_by_source.get(authoritative_source, [])
    if len(authoritative_observations) < 4:
        _error("raw evidence lacks the minimum authoritative commit chain")
    reconstructed: list[dict[str, Any]] = []
    consumed_carriers: set[int] = set()
    for observation in authoritative_observations:
        physical = observed_physical[id(observation)]
        matching = list(
            carriers_by_physical.get(
                _v13_commit_cross_source_projection(physical), ()
            )
        )
        if not matching:
            _error("v13 designated commit observation lacks an exact identity")
        if len({str(carrier["source_id"]) for carrier in matching}) != len(matching):
            _error("v13 commit identity carrier is duplicated by one source")
        designated = [
            carrier
            for carrier in matching
            if carrier.get("event_type") == "block.committed"
        ]
        identity_carriers = designated if designated else matching
        identities = {
            (
                tuple(sorted(carrier_identity[id(carrier)][0].items())),
                carrier_identity[id(carrier)][1],
            )
            for carrier in identity_carriers
        }
        if len(identities) != 1:
            _error("v13 cross-source commit identities conflict")
        proof_items, generation = next(iter(identities))
        proof = dict(proof_items)
        if proof["block_hash"] != physical["block_hash"]:
            _error("v13 reconstructed identity changed the physical block")
        consumed_carriers.update(id(carrier) for carrier in matching)
        reconstructed.append(
            {
                **dict(observation),
                "event_type": "block.committed",
                "payload": {
                    **physical,
                    "designated_observer": True,
                    "decision_proof": proof,
                    "view_generation": generation,
                },
                "_identity_carriers": tuple(
                    {
                        "event_type": carrier["event_type"],
                        "source_id": carrier["source_id"],
                        "source_instance": carrier["source_instance"],
                        "source_sequence": carrier["source_sequence"],
                        "source_monotonic_ns": carrier["source_monotonic_ns"],
                    }
                    for carrier in sorted(
                        matching,
                        key=lambda value: (
                            str(value["source_id"]),
                            _integer(
                                value["source_sequence"],
                                "v13 carrier source sequence",
                                1,
                            ),
                        ),
                    )
                ),
            }
        )
    orphaned = [
        carrier for carrier in carriers if id(carrier) not in consumed_carriers
    ]
    if orphaned:
        # Coordinated shutdown is source-local: a non-designated replica may
        # commit a strict canonical suffix after the designated observer's
        # last durable row.  Such rows cannot contribute to throughput or
        # transition claims, but rejecting them would make a valid sealed run
        # depend on shutdown scheduling.  Admit only one conflict-free,
        # contiguous extension of the designated tip; any orphan at or below
        # the authoritative height, gap, fork, or parent mismatch fails.
        tip = max(
            authoritative_observations,
            key=lambda event: observed_physical[id(event)]["block_height"],
        )
        tip_physical = observed_physical[id(tip)]
        tip_height = int(tip_physical["block_height"])
        suffix_by_height: dict[int, tuple[object, object, object, object]] = {}
        for carrier in orphaned:
            physical = carrier_physical[id(carrier)]
            height = int(physical["block_height"])
            projection = _v13_commit_cross_source_projection(physical)
            if height <= tip_height:
                _error("v13 commit identity carrier is orphaned")
            prior = suffix_by_height.setdefault(height, projection)
            if prior != projection:
                _error("v13 commit identity carrier suffix conflicts")
        heights = sorted(suffix_by_height)
        if heights != list(range(tip_height + 1, heights[-1] + 1)):
            _error("v13 commit identity carrier suffix is not contiguous")
        parent_hash = tip_physical["block_hash"]
        for height in heights:
            projection = suffix_by_height[height]
            if projection[2] != parent_hash:
                _error("v13 commit identity carrier suffix parent drifted")
            parent_hash = projection[1]
    return reconstructed


def _commit_reconstruction(
    root: Path,
    events: Sequence[Mapping[str, Any]],
    epoch1: Any,
    epoch2: Any | None,
    contract: Mapping[str, object],
) -> tuple[list[Mapping[str, Any]], dict[str, object]]:
    profile = _mapping(contract.get("profile"), "focused profile")
    is_v13 = profile.get("profile_id") in _FCRASH_H_V13_PROFILE_IDS
    commits: list[Mapping[str, Any]] = (
        _v13_reconstruct_authoritative_commits(events, contract)
        if is_v13
        else [event for event in events if event["event_type"] == "block.committed"]
    )
    authoritative_source = str(contract["authoritative_source_id"])
    member_sources = {f"replica-{member}" for member in contract["members"]}
    if any(
        event["source_kind"] != "replica" or event["source_id"] not in member_sources
        for event in commits
    ):
        _error("raw evidence contains a non-member committed block")
    authoritative_commits = [
        event for event in commits if event["source_id"] == authoritative_source
    ]
    if len(authoritative_commits) < 4:
        _error("raw evidence lacks the minimum authoritative commit chain")
    is_v3 = (
        profile.get("profile_id")
        in _FCRASH_H_V3_PROFILE_IDS | _FAULT_WINDOW_PROFILE_IDS
    )
    configurations_by_source_epoch: dict[
        tuple[str, int], list[tuple[tuple[int, int], int]]
    ] = {}
    if is_v3:
        instances = {
            source: _authoritative_lifecycle_instance(events, source)
            for source in member_sources
        }
        if any(
            event.get("source_instance") != instances[str(event["source_id"])]
            for event in commits
        ):
            _error("committed block is not lifecycle-bound")
        epoch_trees = {
            0: tuple(range(len(tuple(contract["members"])))),
            1: tuple(tree.tree_id for tree in epoch1.trees),
        }
        if epoch2 is not None:
            epoch_trees[2] = tuple(tree.tree_id for tree in epoch2.trees)
        allowed_digests = {
            0: str(contract["epoch_zero_digest"]),
            1: epoch1.epoch_digest,
        }
        if epoch2 is not None:
            allowed_digests[2] = epoch2.epoch_digest
        expected_indexes = {
            (source, epoch): 0 for source in member_sources for epoch in allowed_digests
        }
        config_events = sorted(
            [
                event
                for event in events
                if event["event_type"] == "adaptive.configuration_active"
                and event["source_kind"] == "replica"
                and event["source_id"] in member_sources
                and event.get("source_instance") == instances[str(event["source_id"])]
            ],
            key=lambda event: (
                str(event["source_id"]),
                _integer(event["source_sequence"], "configuration sequence", 1),
                _integer(event["source_monotonic_ns"], "configuration timestamp"),
            ),
        )
        for event in config_events:
            source = str(event["source_id"])
            payload = _mapping(event["payload"], "committed-block configuration")
            epoch = _integer(payload.get("epoch_number"), "configuration epoch")
            tree = _integer(payload.get("tree_id"), "configuration tree")
            if (
                epoch not in allowed_digests
                or payload.get("epoch_digest") != allowed_digests[epoch]
                or not epoch_trees[epoch]
                or tree != epoch_trees[epoch][expected_indexes[(source, epoch)]]
            ):
                _error("committed-block cyclic configuration drifted")
            configurations_by_source_epoch.setdefault((source, epoch), []).append(
                (
                    (
                        _integer(event["source_sequence"], "configuration sequence", 1),
                        _integer(
                            event["source_monotonic_ns"], "configuration timestamp"
                        ),
                    ),
                    tree,
                )
            )
            expected_indexes[(source, epoch)] = (
                expected_indexes[(source, epoch)] + 1
            ) % len(epoch_trees[epoch])
        if {
            epoch
            for source, epoch in configurations_by_source_epoch
            if source == authoritative_source
        } != set(allowed_digests):
            _error("v3 authoritative commit chain lacks active epoch configurations")
    commits.sort(
        key=(
            (
                lambda event: _integer(
                    event["source_sequence"], "commit source sequence", 1
                )
            )
            if is_v3
            else (
                lambda event: _integer(
                    event["payload"].get("block_height"), "commit height", 1
                )
            )
        )
    )
    prior_hash: str | None = None
    prior_height: int | None = None
    prior_epoch = 0
    prior_sequence: int | None = None
    for event in commits:
        is_authoritative = event["source_id"] == authoritative_source
        payload = _mapping(event["payload"], "authoritative commit")
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
            _error("authoritative commit schema drifted")
        height = _integer(payload.get("block_height"), "commit height", 1)
        block_hash = _digest(payload.get("block_hash"), "commit hash")
        if is_v3:
            _digest(payload.get("parent_hash"), "commit parent hash")
        transactions = _uint64(payload.get("transaction_count"), "transactions")
        commit_batch_index = _uint64(
            payload.get("commit_batch_index"), "commit batch index"
        )
        view_generation = _uint64(
            payload.get("view_generation"), "commit view generation", 1
        )
        proof = _mapping(payload.get("decision_proof"), "decision proof")
        if set(proof) != {
            "epoch_number",
            "tree_id",
            "epoch_digest",
            "block_hash",
        }:
            _error("authoritative commit decision proof schema drifted")
        sequence = _integer(event["source_sequence"], "commit source sequence", 1)
        if (
            (
                is_authoritative
                and is_v3
                and prior_sequence is not None
                and sequence <= prior_sequence
            )
            or (
                is_authoritative
                and prior_height is not None
                and (height != prior_height + 1 if is_v3 else height <= prior_height)
            )
            or (
                is_authoritative
                and prior_hash is not None
                and payload.get("parent_hash") != prior_hash
            )
            or payload.get("designated_observer")
            is not (event["source_id"] == authoritative_source)
            or (not is_v3 and commit_batch_index != 0)
            or (not is_v3 and view_generation != 1)
            or (
                transactions not in {0, int(contract["transactions_per_block"])}
                if is_v3
                else transactions % 5 != 0
            )
            or proof.get("block_hash") != block_hash
        ):
            _error("authoritative commit chain or workload identity drifted")
        expected_epoch = _integer(proof.get("epoch_number"), "commit epoch")
        allowed_digests = {
            0: str(contract["epoch_zero_digest"]),
            1: epoch1.epoch_digest,
        }
        if epoch2 is not None:
            allowed_digests[2] = epoch2.epoch_digest
        if (
            expected_epoch not in allowed_digests
            or (is_authoritative and expected_epoch < prior_epoch)
            or proof.get("epoch_digest") != allowed_digests[expected_epoch]
        ):
            _error("authoritative commit decision proof drifted")
        tree = _integer(proof.get("tree_id"), "commit tree")
        if not is_v3 and tree != 0:
            _error("authoritative commit decision proof drifted")
        if is_v3:
            commit_key = (
                sequence,
                _integer(event["source_monotonic_ns"], "commit timestamp"),
            )
            timeline = configurations_by_source_epoch.get(
                (str(event["source_id"]), expected_epoch)
            )
            if timeline is None:
                _error("committed block lacks an active epoch configuration")
            generation_base = (expected_epoch << 32) + 1
            if view_generation < generation_base:
                _error("authoritative commit generation is not activated")
            rotation_ordinal = view_generation - generation_base
            if rotation_ordinal >= len(timeline):
                _error("authoritative commit generation is not activated")
            generation_configuration = timeline[rotation_ordinal]
            if (
                generation_configuration[0] > commit_key
                or generation_configuration[1] != tree
            ):
                _error("authoritative commit is not bound to its active generation")
        if is_authoritative:
            prior_epoch = expected_epoch
            prior_hash = block_hash
            prior_height = height
            prior_sequence = sequence
    observations = [
        event
        for event in events
        if event["event_type"] == "block.commit_observed"
        and event["source_kind"] == "replica"
    ]
    _select_latest_common_commit(authoritative_commits, observations, contract)
    phase_document = _read_json(
        root / "derived" / "phase-windows.json", "phase windows"
    )
    recorded = _sequence(phase_document.get("phases"), "phase windows")
    phase_names = tuple(str(name) for name in contract["phase_names"])
    width_ns = int(contract["bucket_width_seconds"]) * 1_000_000_000
    if contract.get("profile_id") in _FCRASH_H_V4_PROFILE_IDS:
        _error("v4 evidence lacks the frozen causal phase-window contract")
    if _is_v5_contract(contract):
        if (
            set(phase_document) != {"schema_version", "domain", "phases"}
            or phase_document.get("schema_version") != 1
            or phase_document.get("domain") != "kauri-focused-causal-phase-windows-v1"
            or not recorded
        ):
            _error("v5 causal phase-window document drifted")
        derived = _v5_causal_phase_windows(
            root,
            events,
            authoritative_commits,
            epoch2,
            contract,
        )
        expected_rows = [
            {
                "phase": phase,
                "start_ns": start,
                "end_ns": end,
                "epoch_number": epoch,
            }
            for phase, start, end, epoch in derived
        ]
        if list(recorded) != expected_rows:
            _error("recorded causal phase windows differ from independent replay")
        windows = [(phase, start, end) for phase, start, end, _epoch in derived]
    elif recorded:
        if len(recorded) != len(phase_names):
            _error("phase window count drifted")
        windows: list[tuple[str, int, int]] = []
        for name, value in zip(phase_names, recorded, strict=True):
            row = _mapping(value, "phase window")
            start = _integer(row.get("start_ns"), "phase start")
            end = _integer(row.get("end_ns"), "phase end", 1)
            if row.get("phase") != name or end - start != width_ns:
                _error("phase window identity or duration drifted")
            windows.append((name, start, end))
        if any(right[1] != left[2] for left, right in zip(windows, windows[1:])):
            _error("phase windows are not contiguous")
    else:
        first_ns = min(
            int(event["source_monotonic_ns"]) for event in authoritative_commits
        )
        origin = first_ns - (first_ns % width_ns)
        windows = [
            (name, origin + index * width_ns, origin + (index + 1) * width_ns)
            for index, name in enumerate(phase_names)
        ]
    phase_rows: list[dict[str, object]] = []
    for phase, start, end in windows:
        transaction_count = sum(
            int(event["payload"]["transaction_count"])
            for event in authoritative_commits
            if start <= int(event["source_monotonic_ns"]) < end
        )
        allow_zero_fault = _is_v5_contract(contract) and phase == "fault"
        if not allow_zero_fault and transaction_count <= 0:
            _error("throughput phase has no authoritative committed transactions")
        row: dict[str, object] = {
            "phase": phase,
            "start_ns": start,
            "end_ns": end,
            "transactions": transaction_count,
            "mean_milli_tps": transaction_count * 1_000_000_000_000 // (end - start),
        }
        if _is_v9_contract(contract):
            buckets, median = _complete_phase_buckets(
                authoritative_commits,
                start_ns=start,
                end_ns=end,
                bucket_width_ns=width_ns,
            )
            row["buckets"] = buckets
            row["median_milli_tps"] = median
        phase_rows.append(row)
    late_throughput = (
        phase_rows[-1]["median_milli_tps"]
        if _is_v9_contract(contract)
        else phase_rows[-1]["mean_milli_tps"]
    )
    return authoritative_commits, {
        "phases": phase_rows,
        "late_window_throughput_milli_tps": late_throughput,
    }


_MANAGER_SINGLETON_OPTIONS = {
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
}
_MANAGER_REPEATABLE_OPTIONS = {"--transition-request", "--bundle-output", "--replica"}


def _validate_manager_boundary(
    contract: Mapping[str, object],
    argv: Sequence[Any],
    manager_input: Mapping[str, Any],
    manager_events: Sequence[Mapping[str, Any]],
    *,
    transition_count: int,
    readiness_manifest: Mapping[str, object] | None = None,
) -> None:
    arguments = tuple(argv)
    is_v13 = _mapping(contract["profile"], "focused profile").get("profile_id") in _FCRASH_H_V13_PROFILE_IDS
    v13_singletons = {
        "--protocol-mode", "--activation-readiness-release-count",
        "--activation-readiness-maximum-delivery-attempts",
        "--activation-readiness-retry-interval-ticks",
    }
    v13_repeatable = {"--activation-readiness-member"}
    if (
        not arguments
        or any(not isinstance(value, str) for value in arguments)
        or Path(arguments[0]).name != "adaptation-manager"
        or set(manager_input)
        != {"input_source", "requested_argv", "observed_argv", "stdin"}
        or manager_input.get("input_source") != "normalized_manager_launch_boundary_v1"
        or manager_input.get("requested_argv") != list(arguments)
        or manager_input.get("observed_argv") != list(arguments)
        or manager_input.get("stdin") != "closed"
    ):
        _error("manager launch boundary schema or identity drifted")
    counts: dict[str, int] = {}
    position = 1
    while position < len(arguments):
        option = arguments[position]
        if option not in _MANAGER_SINGLETON_OPTIONS | _MANAGER_REPEATABLE_OPTIONS | (v13_singletons | v13_repeatable if is_v13 else set()):
            _error("manager launch boundary contains an unknown option")
        if position + 1 >= len(arguments) or arguments[position + 1].startswith("--"):
            _error("manager launch boundary contains an unpaired option")
        counts[option] = counts.get(option, 0) + 1
        position += 2
    if any(counts.get(option, 0) > 1 for option in _MANAGER_SINGLETON_OPTIONS | (v13_singletons if is_v13 else set())):
        _error("manager launch boundary repeats a singleton option")
    if is_v13:
        transitions = _mapping(_mapping(contract["profile"], "focused profile").get("transitions"), "v13 transitions")
        responsiveness = _native_responsiveness_policy(
            _mapping(contract["profile"], "focused profile").get("profile_id")
        )
        expected = {
            "--protocol-mode": "adaptive_v3",
            "--activation-readiness-release-count": str(transitions.get("survivor_barrier_count")),
            "--activation-readiness-maximum-delivery-attempts": "5",
            "--activation-readiness-retry-interval-ticks": "1000000000",
            "--responsiveness-policy-version": str(
                responsiveness["policy_version"]
            ),
            "--responsiveness-attempt-window": str(
                responsiveness["attempt_window"]
            ),
            "--responsiveness-minimum-attempts": str(
                responsiveness["minimum_attempts"]
            ),
            "--responsiveness-minimum-response-rate-ppm": str(
                responsiveness["minimum_response_rate_ppm"]
            ),
            "--responsiveness-maximum-timeout-rate-ppm": str(
                responsiveness["maximum_timeout_rate_ppm"]
            ),
            "--responsiveness-trailing-timeout-streak": str(
                responsiveness["trailing_timeout_streak"]
            ),
            "--responsiveness-latency-percentile-basis-points": str(
                responsiveness["latency_percentile_basis_points"]
            ),
        }
        values = {arguments[index]: arguments[index + 1] for index in range(1, len(arguments), 2)}
        if any(counts.get(key) != 1 or values.get(key) != value for key, value in expected.items()):
            _error("v13 manager readiness singleton binding drifted")
        members = [arguments[index + 1] for index in range(1, len(arguments), 2) if arguments[index] == "--activation-readiness-member"]
        if readiness_manifest is None:
            _error("v13 manager boundary lacks the authorized readiness manifest")
        manifest_members = tuple(
            _mapping(value, "v13 manager readiness manifest member")
            for value in _sequence(
                readiness_manifest.get("members"), "v13 manager readiness members"
            )
        )
        expected_members = [
            f"{replica},{member.get('public_key_hex')}"
            for replica, member in zip(contract["members"], manifest_members, strict=True)
        ]
        if members != expected_members or "--activation-readiness-identity" in arguments:
            _error("v13 manager public readiness membership or identity drifted")
    if counts.get("--replica") != len(tuple(contract["members"])):
        _error("manager launch boundary does not contain the exact membership")
    expected_transition_count = (
        transition_count
        if _mapping(contract["profile"], "focused profile").get("profile_id")
        in _FCRASH_H_V3_PROFILE_IDS | _FAULT_WINDOW_PROFILE_IDS
        else int(contract["adaptive_transition_count"])
    )
    if counts.get("--transition-request") != expected_transition_count:
        _error("manager launch boundary transition cardinality drifted")
    if counts.get("--bundle-output") != counts.get("--transition-request"):
        _error("manager launch boundary bundle output cardinality drifted")
    transition_requests: list[Mapping[str, Any]] = []
    for index, argument in enumerate(arguments[:-1]):
        if argument != "--transition-request":
            continue
        try:
            request = json.loads(arguments[index + 1])
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise FocusedCrashPairValidationError(
                "manager transition request is invalid JSON"
            ) from exc
        transition_requests.append(_mapping(request, "manager transition request"))


    if _is_v10_contract(contract):
        residence_ms = _mapping(
            _mapping(contract.get("profile"), "focused profile").get("transitions"),
            "profile transitions",
        ).get("adaptive_timing_contract", {})
        residence_ms = _mapping(residence_ms, "v10 transition timing").get(
            "optimization_minimum_predecessor_residency_ms"
        )
        expected_residencies = (0, residence_ms) if transition_count == 2 else (0,)
        actual_residencies = tuple(
            request.get("minimum_predecessor_residency_ms")
            for request in transition_requests
        )
        if actual_residencies != expected_residencies:
            _error("v10 manager transition residence differs from the frozen profile")
    arm_options = {
        option
        for option in _MANAGER_SINGLETON_OPTIONS
        if option.startswith("--fault-window-arm-")
    }
    v6_arm_options = {
        "--fault-window-arm-clock-domain",
        "--fault-window-arm-required-observation-schema",
        "--fault-window-arm-timeout-evidence-basis",
    }
    v7_arm_options = {"--fault-window-arm-snapshot-evidence-basis"}
    v9_arm_options = {"--fault-window-arm-selection-cardinality-policy"}
    common_arm_options = arm_options - v6_arm_options - v7_arm_options - v9_arm_options
    if _is_v4_contract(contract):
        if (
            any(counts.get(option) != 1 for option in common_arm_options)
            or (
                (
                    _is_v6_contract(contract)
                    or _is_v7_contract(contract)
                    or _is_v8_or_v9_contract(contract)
                )
                and any(counts.get(option) != 1 for option in v6_arm_options)
            )
            or (
                not (
                    _is_v6_contract(contract)
                    or _is_v7_contract(contract)
                    or _is_v8_or_v9_contract(contract)
                )
                and any(counts.get(option, 0) for option in v6_arm_options)
            )
            or (
                (_is_v7_contract(contract) or _is_v8_or_v9_contract(contract))
                and any(counts.get(option) != 1 for option in v7_arm_options)
            )
            or (
                not (_is_v7_contract(contract) or _is_v8_or_v9_contract(contract))
                and any(counts.get(option, 0) for option in v7_arm_options)
            )
            or (
                _is_v9_contract(contract)
                and any(counts.get(option) != 1 for option in v9_arm_options)
            )
            or (
                not _is_v9_contract(contract)
                and any(counts.get(option, 0) for option in v9_arm_options)
            )
        ):
            _error("v4 manager launch lacks exact fault-window arm bindings")
    elif any(counts.get(option, 0) for option in arm_options):
        _error("legacy manager launch contains a prospective fault-window arm")
    try:
        factorial_validation.validate_manager_blinding(arguments, manager_events)
    except factorial_validation.FactorialValidationError as exc:
        raise FocusedCrashPairValidationError("manager boundary is not blind") from exc


def _validate_v13_redacted_launch_arguments(
    root: Path,
    contract: Mapping[str, object],
    launch: Mapping[str, object],
    events: Sequence[Mapping[str, object]],
    readiness_manifest: Mapping[str, object],
    issuer_public_key: object,
) -> None:
    """Bind the sealed public replica/client argv without recovering secrets."""

    sealed_root = root.resolve(strict=True)
    if set(launch) != {
        "manager_argv",
        "manager_checkpoint",
        "replica_argv",
        "client_argv",
    } or launch.get("manager_checkpoint") != "adaptive_v3_unified_manager_checkpoint4":
        _error("v13 launch-argument schema or checkpoint drifted")
    members = tuple(
        _mapping(member, "v13 launch readiness member")
        for member in _sequence(
            readiness_manifest.get("members"), "v13 launch readiness members"
        )
    )
    replica_ids = tuple(
        _integer(replica, "v13 launch replica") for replica in contract["members"]
    )
    if tuple(member.get("replica_id") for member in members) != replica_ids:
        _error("v13 launch readiness membership order drifted")
    membership_argv = tuple(
        argument
        for member in members
        for argument in (
            "--activation-readiness-member",
            f"{member['replica_id']},{member['public_key_hex']}",
        )
    )
    source_instances: dict[str, str] = {}
    run_ids: set[str] = set()
    for event in events:
        source_id = event.get("source_id")
        source_instance = event.get("source_instance")
        run_id = event.get("run_id")
        if isinstance(source_id, str) and isinstance(source_instance, str):
            prior = source_instances.setdefault(source_id, source_instance)
            if prior != source_instance:
                _error("v13 launch source spans multiple instances")
        if isinstance(run_id, str):
            run_ids.add(run_id)
    if len(run_ids) != 1:
        _error("v13 launch events do not bind one run")
    run_id = next(iter(run_ids))
    observer_id = (
        f"replica-{_integer(contract['authoritative_replica_id'], 'v13 observer')}"
    )
    observer_instance = source_instances.get(observer_id)
    if not observer_instance:
        _error("v13 launch authoritative observer instance is absent")

    replicas = tuple(
        _sequence(command, "v13 redacted replica argv")
        for command in _sequence(launch.get("replica_argv"), "v13 replica argv")
    )
    if len(replicas) != len(replica_ids):
        _error("v13 replica argv cardinality drifted")
    manager_cert: str | None = None
    profile = _mapping(contract["profile"], "v13 launch profile")
    ports = _mapping(profile.get("ports"), "v13 launch ports")
    manager_port = _integer(ports.get("base"), "v13 launch port base", 1) + 2 * len(
        replica_ids
    )
    issuer = issuer_public_key
    if (
        not isinstance(issuer, str)
        or len(issuer) != 66
        or issuer[:2] not in {"02", "03"}
        or any(character not in "0123456789abcdef" for character in issuer)
    ):
        _error("v13 launch issuer public key width drifted")
    for replica, command in zip(replica_ids, replicas, strict=True):
        arguments = tuple(command)
        if not arguments or any(not isinstance(value, str) for value in arguments):
            _error("v13 replica argv is not canonical text")
        try:
            certificate = arguments[
                arguments.index("--epoch-manager-tls-cert") + 1
            ]
        except (ValueError, IndexError) as exc:
            raise FocusedCrashPairValidationError(
                "v13 replica argv lacks the manager certificate"
            ) from exc
        if not certificate or certificate == "<redacted>":
            _error("v13 replica argv manager certificate drifted")
        if manager_cert is None:
            manager_cert = certificate
        elif certificate != manager_cert:
            _error("v13 replica argv manager certificate is inconsistent")
        source_id = f"replica-{replica}"
        source_instance = source_instances.get(source_id)
        if not source_instance:
            _error("v13 replica argv source instance is absent")
        expected_tail = (
            "--conf", str(sealed_root / "config" / "main.conf"),
            "--conf", str(sealed_root / "config" / f"replica-{replica}.conf"),
            "--privkey", "<redacted>",
            "--epoch-protocol-mode", "adaptive_v3",
            "--epoch-change-issuer-id", "1",
            "--epoch-change-issuer-public-key", issuer,
            "--epoch-change-minimum-activation-delay", "5",
            "--epoch-change-maximum-activation-delay", "5",
            "--epoch-change-maximum-block-extra-bytes", "4096",
            "--epoch-change-maximum-ancestry-blocks", "128",
            "--epoch-manager-address", f"127.0.0.1:{manager_port}",
            "--epoch-manager-tls-cert", certificate,
            *membership_argv,
            "--activation-readiness-maximum-observation-attempts", "5",
            "--activation-readiness-observation-retry-interval-ms", "1000",
            "--structured-event-run-id", run_id,
            "--structured-event-source-instance", source_instance,
            "--structured-event-output", str(sealed_root / "raw" / f"replica-{replica}.jsonl"),
            "--structured-event-commit-observer-id", observer_id,
            "--structured-event-commit-observer-instance", observer_instance,
        )
        if Path(arguments[0]).name != "hotstuff-app":
            _error(f"v13 redacted replica {replica} executable drifted")
        if len(arguments[1:]) != len(expected_tail):
            _error(f"v13 redacted replica {replica} argv cardinality drifted")
        for index, (actual, expected) in enumerate(
            zip(arguments[1:], expected_tail, strict=True), start=1
        ):
            if actual != expected:
                _error(
                    f"v13 redacted replica {replica} argv drifted at position {index}"
                )

    client = tuple(_sequence(launch.get("client_argv"), "v13 client argv"))
    expected_client_tail = (
        "--conf", str(sealed_root / "config" / "main.conf"),
        "--idx", "0",
        "--iter", "-1",
        "--max-async",
        str(_integer(contract["transactions_per_block"], "v13 client block size", 1)),
        "--epoch-protocol-mode", "adaptive_v3",
    )
    if (
        not client
        or any(not isinstance(value, str) for value in client)
        or Path(client[0]).name != "hotstuff-client"
        or client[1:] != expected_client_tail
    ):
        _error("v13 client argv drifted")


def _validate_v13_parent_readiness_projection(
    value: object, *, pair_count: int, member_count: int
) -> dict[str, dict[str, object]]:
    """Independently validate the approval-bound public v13 projection."""
    projection = _mapping(value, "v13 parent readiness projection")
    expected = {f"pair-{ordinal:02d}" for ordinal in range(1, pair_count + 1)}
    if set(projection) != expected:
        _error("v13 parent readiness projection pair keys drifted")
    result: dict[str, dict[str, object]] = {}
    manifests: set[str] = set()
    memberships: set[str] = set()
    for pair_id in sorted(expected):
        row = _mapping(projection[pair_id], f"{pair_id} parent readiness projection")
        if set(row) != {"manifest_sha256", "membership_digest", "member_count"}:
            _error("v13 parent readiness projection schema drifted")
        manifest = _digest(row.get("manifest_sha256"), f"{pair_id} manifest digest")
        membership = _digest(row.get("membership_digest"), f"{pair_id} membership digest")
        count = _integer(row.get("member_count"), f"{pair_id} member count", 1)
        if count != member_count or manifest in manifests or membership in memberships:
            _error("v13 parent readiness projection commitment drifted")
        manifests.add(manifest)
        memberships.add(membership)
        result[pair_id] = {"manifest_sha256": manifest,
                           "membership_digest": membership,
                           "member_count": count}
    return result


def _parse_utc_approval_time(value: str, label: str) -> datetime:
    # Python 3.10's fromisoformat predates RFC 3339's common ``Z`` spelling.
    # Normalize only that exact UTC suffix; offset validation remains strict.
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise FocusedCrashPairValidationError(
            f"{label} approval time is invalid"
        ) from exc
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        _error(f"{label} approval time is not UTC")
    return parsed


def _validate_v13_parent_authorization_projection(
    root: Path, contract: Mapping[str, object]
) -> Mapping[str, object]:
    """Validate and select the child-bound v13 public approval projection."""
    profile = _mapping(contract["profile"], "focused profile")
    if profile.get("profile_id") not in _FCRASH_H_V13_PROFILE_IDS:
        _error("v13 parent authorization used by an archived profile")
    request_path = root / "runtime" / "parent-authorization-request.json"
    receipt_path = root / "runtime" / "parent-authorization-receipt.json"
    request = _read_json(request_path, "parent authorization request")
    receipt = _read_json(receipt_path, "parent authorization receipt")
    request_bytes, receipt_bytes = request_path.read_bytes(), receipt_path.read_bytes()
    if request_bytes != _canonical(request) or receipt_bytes != _canonical(receipt):
        _error("v13 parent authorization provenance is not canonical")
    base = {"schema_version", "mode", "pair_count", "profile_sha256",
            "topology_proof_sha256", "output_root", "automatic_retries",
            "replacement_policy", "authorization_nonce"}
    keys = base | {"execution_context_sha256", "pair_readiness_manifests"}
    if set(request) != keys or request.get("schema_version") != 2:
        _error("v13 parent authorization request schema drifted")
    if (
        request.get("mode") not in {"pair", "smoke", "campaign"}
        or request.get("profile_sha256") != contract.get("profile_sha256")
        or request.get("topology_proof_sha256") != contract.get("topology_proof_sha256")
        or request.get("automatic_retries") != 0
        or request.get("replacement_policy") != "none"
        or not isinstance(request.get("output_root"), str)
        or not Path(request["output_root"]).is_absolute()
        or not isinstance(request.get("authorization_nonce"), str)
        or not request["authorization_nonce"]
    ):
        _error("v13 parent authorization request binding drifted")
    _digest(request.get("execution_context_sha256"), "parent execution-context digest")
    count = _integer(request.get("pair_count"), "authorized pair count", 1)
    projection = _validate_v13_parent_readiness_projection(
        request.get("pair_readiness_manifests"), pair_count=count,
        member_count=_integer(_mapping(profile.get("protocol"), "profile protocol").get("N"), "profile N", 1))
    receipt_keys = keys | {"request_sha256", "approval_reference", "approved_utc"}
    if (set(receipt) != receipt_keys or
        any(receipt.get(key) != request.get(key) for key in keys) or
        receipt.get("request_sha256") != _sha_bytes(request_bytes)):
        _error("v13 parent authorization receipt binding drifted")
    approval_reference = receipt.get("approval_reference")
    approved_utc = receipt.get("approved_utc")
    if (
        not isinstance(approval_reference, str)
        or not 1 <= len(approval_reference) <= 200
        or any(not 32 <= ord(character) <= 126 for character in approval_reference)
        or not isinstance(approved_utc, str)
        or not 1 <= len(approved_utc) <= 64
        or any(not 32 <= ord(character) <= 126 for character in approved_utc)
    ):
        _error("v13 parent authorization approval metadata drifted")
    _parse_utc_approval_time(approved_utc, "v13 parent authorization")
    pair_receipt = _mapping(
        _read_json(root / "pair-receipt.json", "pair receipt"), "pair receipt"
    )
    pair = pair_receipt.get("pair_id")
    slot = pair_receipt.get("slot_id")
    output_root = Path(str(request["output_root"]))
    if request.get("mode") == "campaign":
        child_path_valid = (
            isinstance(slot, str)
            and slot.startswith("slot-")
            and slot[5:].isdigit()
            and root.resolve() == (output_root / "children" / slot).resolve()
        )
    else:
        child_path_valid = (
            root.name in {"control", "adaptive"}
            and root.resolve() == (output_root / str(pair) / root.name).resolve()
        )
    if not isinstance(pair, str) or pair not in projection or not child_path_valid:
        _error("v13 parent authorization child pair binding drifted")
    return projection[pair]


def _validate_parent_authorization_projection(
    root: Path, contract: Mapping[str, object]
) -> Mapping[str, object] | None:
    """Route parent authorization by frozen profile schema without sniffing."""
    profile = _mapping(contract["profile"], "focused profile")
    if profile.get("profile_id") in _FCRASH_H_V13_PROFILE_IDS:
        return _validate_v13_parent_authorization_projection(root, contract)
    request_path = root / "runtime" / "parent-authorization-request.json"
    receipt_path = root / "runtime" / "parent-authorization-receipt.json"
    request = _read_json(request_path, "parent authorization request")
    receipt = _read_json(receipt_path, "parent authorization receipt")
    request_bytes, receipt_bytes = request_path.read_bytes(), receipt_path.read_bytes()
    if request_bytes != _canonical(request) or receipt_bytes != _canonical(receipt):
        _error("parent authorization provenance is not canonical")
    base = {
        "schema_version", "mode", "pair_count", "profile_sha256",
        "topology_proof_sha256", "output_root", "automatic_retries",
        "replacement_policy", "authorization_nonce",
    }
    request_keys = set(request)
    if request_keys == base | {"execution_context_sha256"}:
        _digest(request.get("execution_context_sha256"), "parent execution-context digest")
    elif request_keys != base or request.get("schema_version") != 1:
        _error("archived parent authorization request schema drifted")
    if request.get("schema_version") != 1:
        _error("archived parent authorization request schema drifted")
    receipt_keys = request_keys | {"request_sha256", "approval_reference", "approved_utc"}
    if (
        set(receipt) != receipt_keys
        or any(receipt.get(key) != request.get(key) for key in request_keys)
        or receipt.get("request_sha256") != _sha_bytes(request_bytes)
    ):
        _error("archived parent authorization receipt binding drifted")
    return None


def _read_v13_readiness_public_manifest(path: Path) -> tuple[bytes, Mapping[str, object]]:
    """Read one regular, bounded, final-component-nofollow public manifest."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or not 1 <= status.st_size <= _V13_READINESS_MANIFEST_MAX_BYTES:
            _error("v13 readiness public manifest is not one bounded regular file")
        chunks: list[bytes] = []
        remaining = status.st_size
        while remaining:
            try:
                chunk = os.read(descriptor, remaining)
            except InterruptedError:
                continue
            if not chunk:
                _error("v13 readiness public manifest was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _error("v13 readiness public manifest changed while being read")
        raw = b"".join(chunks)
    except FocusedCrashPairValidationError:
        raise
    except OSError as exc:
        raise FocusedCrashPairValidationError(
            "v13 readiness public manifest cannot be opened safely"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not raw.endswith(b"\n") or any(byte > 0x7F for byte in raw):
        _error("v13 readiness public manifest bytes are not compact ASCII JSON")
    try:
        document = _mapping(json.loads(raw.decode("ascii")), "v13 readiness public manifest")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FocusedCrashPairValidationError(
            "v13 readiness public manifest is malformed"
        ) from exc
    if raw != _canonical(document):
        _error("v13 readiness public manifest bytes are not canonical")
    return raw, document


def _v13_readiness_membership_digest(members: Sequence[Mapping[str, object]]) -> str:
    encoded = bytearray(_V13_READINESS_MEMBERSHIP_DOMAIN)
    encoded.extend(len(members).to_bytes(4, "big"))
    for expected_replica, member in enumerate(members):
        replica = _integer(member.get("replica_id"), "v13 manifest replica id", 0)
        public = member.get("public_key_hex")
        if replica != expected_replica or not isinstance(public, str):
            _error("v13 readiness manifest membership is not canonical")
        encoded.extend(replica.to_bytes(2, "big"))
        encoded.extend(bytes.fromhex(public))
    return _sha_bytes(bytes(encoded))


def _validate_v13_readiness_public_manifest(
    root: Path, contract: Mapping[str, object], selected_projection: Mapping[str, object]
) -> Mapping[str, object]:
    """Validate the approval-bound, secret-free v13 BLS member manifest."""
    profile = _mapping(contract["profile"], "focused profile")
    if profile.get("profile_id") not in _FCRASH_H_V13_PROFILE_IDS:
        _error("v13 readiness public manifest used by an archived profile")
    runtime = root / "runtime"
    path = runtime / "activation-readiness-public-manifest.json"
    if runtime.is_symlink() or not runtime.is_dir() or path.is_symlink():
        _error("v13 readiness public manifest path is unsafe")
    raw, manifest = _read_v13_readiness_public_manifest(path)
    expected_keys = {
        "algorithm", "domain", "membership_digest", "members", "profile_id",
        "profile_sha256", "protocol_mode", "schema_version",
    }
    protocol = _mapping(profile.get("protocol"), "profile protocol")
    count = _integer(protocol.get("N"), "profile N", 1)
    members_value = manifest.get("members")
    if not isinstance(members_value, list) or len(members_value) != count:
        _error("v13 readiness public manifest member cardinality drifted")
    members = [_mapping(member, "v13 readiness public manifest member") for member in members_value]
    public_keys: set[str] = set()
    for expected_replica, member in enumerate(members):
        if set(member) != {"replica_id", "public_key_hex"}:
            _error("v13 readiness public manifest member schema drifted")
        replica = _integer(member.get("replica_id"), "v13 manifest replica id", 0)
        public = member.get("public_key_hex")
        if (
            replica != expected_replica
            or not isinstance(public, str)
            or len(public) != 96
            or any(character not in "0123456789abcdef" for character in public)
            or public in public_keys
        ):
            _error("v13 readiness public manifest member identity drifted")
        public_keys.add(public)
    membership = _v13_readiness_membership_digest(members)
    if (
        set(manifest) != expected_keys
        or manifest.get("schema_version") != 1
        or manifest.get("algorithm") != "bls-pop"
        or manifest.get("domain") != "kauri-adaptive-v3-readiness-public-key-manifest-v1"
        or manifest.get("protocol_mode") != "adaptive_v3"
        or manifest.get("profile_id") != contract.get("profile_id")
        or manifest.get("profile_id") != profile.get("profile_id")
        or manifest.get("profile_sha256") != contract.get("profile_sha256")
        or manifest.get("membership_digest") != membership
    ):
        _error("v13 readiness public manifest binding drifted")
    if set(selected_projection) != {"manifest_sha256", "membership_digest", "member_count"}:
        _error("v13 selected readiness projection schema drifted")
    if (
        _digest(selected_projection.get("manifest_sha256"), "selected manifest digest") != _sha_bytes(raw)
        or _digest(selected_projection.get("membership_digest"), "selected membership digest") != membership
        or _integer(selected_projection.get("member_count"), "selected member count", 1) != count
    ):
        _error("v13 readiness public manifest authorization binding drifted")
    return manifest


def _open_hashed_nofollow_executable(path: Path, *, label: str) -> tuple[int, str]:
    """Return an open, descriptor-hashed executable; the caller owns its fd."""
    if not path.is_absolute():
        _error(f"{label} path is not absolute")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or not status.st_mode & 0o111:
            _error(f"{label} is not one executable regular file")
        digest = hashlib.sha256()
        while True:
            try:
                chunk = os.read(descriptor, 64 * 1024)
            except InterruptedError:
                continue
            if not chunk:
                break
            digest.update(chunk)
        return descriptor, digest.hexdigest()
    except FocusedCrashPairValidationError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise FocusedCrashPairValidationError(
            f"{label} cannot be opened safely"
        ) from exc


def _sha256_regular_copy(path: Path) -> str:
    status = path.stat()
    if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o777 != 0o500:
        _error("v13 verified executable copy mode drifted")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_v13_verifier_payload(path: Path, *, label: str) -> bytes:
    if not path.is_absolute():
        _error(f"{label} path is not absolute")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or not 3 <= status.st_size <= 2 * _V13_READINESS_MANIFEST_MAX_BYTES + 1:
            _error(f"{label} is not one bounded regular file")
        raw = bytearray()
        while len(raw) < status.st_size:
            try:
                chunk = os.read(descriptor, status.st_size - len(raw))
            except InterruptedError:
                continue
            if not chunk:
                _error(f"{label} was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            _error(f"{label} changed while being read")
    except FocusedCrashPairValidationError:
        raise
    except OSError as exc:
        raise FocusedCrashPairValidationError(f"{label} cannot be opened safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        _error(f"{label} is not one canonical hex line")
    encoded = bytes(raw[:-1])
    if not encoded or len(encoded) % 2 or any(byte not in b"0123456789abcdef" for byte in encoded):
        _error(f"{label} is not canonical lowercase hex")
    return bytes.fromhex(encoded.decode("ascii"))


def _validate_v13_readiness_verifier(
    root: Path,
    contract: Mapping[str, object],
    selected_projection: Mapping[str, object],
    *,
    trusted_provenance: Mapping[str, object],
    readiness_verifier_path: Path,
    certificate_path: Path,
    identity_path: Path,
    expected_certificate_digest: object,
    expected_observation_count: int,
    run_command: Any = subprocess.run,
) -> Mapping[str, object]:
    """Invoke the explicitly provenance-bound native v13 verifier once."""
    if not isinstance(trusted_provenance, Mapping):
        _error("v13 trusted provenance must be an exact object")
    expected_binary_sha = _digest(
        trusted_provenance.get("epoch_profile_digest_sha256"),
        "v13 epoch-profile-digest provenance",
    )
    certificate_digest = _digest(expected_certificate_digest, "v13 certificate digest")
    if type(expected_observation_count) is not int or expected_observation_count < 1:
        _error("v13 expected observation count is invalid")
    manifest = _validate_v13_readiness_public_manifest(root, contract, selected_projection)
    manifest_bytes, _ = _read_v13_readiness_public_manifest(
        root / "runtime" / "activation-readiness-public-manifest.json"
    )
    certificate = _read_v13_verifier_payload(certificate_path, label="v13 certificate payload")
    identity = _read_v13_verifier_payload(identity_path, label="v13 identity payload")
    try:
        descriptor, actual_binary_sha = _open_hashed_nofollow_executable(
            readiness_verifier_path, label="v13 readiness verifier"
        )
        if actual_binary_sha != expected_binary_sha:
            _error("v13 readiness verifier provenance digest drifted")
        with tempfile.TemporaryDirectory(prefix="kauri-v13-verifier-") as directory:
            copy_path = Path(directory) / "verified"
            copy_fd = os.open(copy_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o500)
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk: break
                    digest.update(chunk)
                    offset = 0
                    while offset < len(chunk):
                        written = os.write(copy_fd, chunk[offset:])
                        if written <= 0:
                            _error("v13 verified executable copy write failed")
                        offset += written
                os.fsync(copy_fd)
            finally:
                os.close(copy_fd)
            if digest.hexdigest() != expected_binary_sha or _sha256_regular_copy(copy_path) != expected_binary_sha:
                _error("v13 verified executable copy digest drifted")
            argv = (str(copy_path), "--verify-adaptive-v3-readiness-v1", str(root / "runtime" / "activation-readiness-public-manifest.json"), str(certificate_path), str(identity_path))
            completed = run_command(argv, check=False, capture_output=True, text=False, shell=False, timeout=_V13_READINESS_VERIFIER_TIMEOUT_SECONDS)
            if _sha256_regular_copy(copy_path) != expected_binary_sha:
                _error("v13 verified executable copy changed after execution")
    except subprocess.TimeoutExpired as exc:
        raise FocusedCrashPairValidationError("v13 readiness verifier timed out") from exc
    except OSError as exc:
        raise FocusedCrashPairValidationError("v13 readiness verifier execution failed") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    stdout, stderr = getattr(completed, "stdout", None), getattr(completed, "stderr", None)
    if (
        getattr(completed, "returncode", None) != 0
        or not isinstance(stdout, bytes)
        or not isinstance(stderr, bytes)
        or stderr
        or not 1 <= len(stdout) <= _V13_READINESS_MANIFEST_MAX_BYTES
        or not stdout.endswith(b"\n")
        or any(byte > 0x7F for byte in stdout)
    ):
        _error("v13 readiness verifier execution failed")
    try:
        result = _mapping(json.loads(stdout.decode("ascii")), "v13 readiness verifier result")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FocusedCrashPairValidationError("v13 readiness verifier returned invalid JSON") from exc
    if stdout != _canonical(result):
        _error("v13 readiness verifier result is not canonical")
    profile = _mapping(contract["profile"], "focused profile")
    protocol = _mapping(profile.get("protocol"), "profile protocol")
    expected = {
        "certificate_digest": certificate_digest,
        "certificate_payload_digest": _sha_bytes(certificate),
        "expected_identity_payload_digest": _sha_bytes(identity),
        "manifest_payload_digest": _sha_bytes(manifest_bytes),
        "member_count": _integer(protocol.get("N"), "profile N", 1),
        "membership_digest": manifest["membership_digest"],
        "observation_count": expected_observation_count,
        "schema": "kauri-adaptive-v3-readiness-verification-v1",
        "valid": True,
    }
    if dict(result) != expected:
        _error("v13 readiness verifier result binding drifted")
    return result


def _validate_v13_certified_readiness_crypto(
    root: Path,
    contract: Mapping[str, object],
    selected_projection: Mapping[str, object],
    transition_state: Mapping[str, object],
    *,
    trusted_provenance: Mapping[str, object],
    readiness_verifier_path: Path,
) -> tuple[Mapping[str, object], ...]:
    """Verify every event-reconstructed readiness certificate natively."""

    cycles = _sequence(
        transition_state.get("readiness_cycles"), "v13 readiness cycles"
    )
    if not cycles:
        _error("v13 readiness certificate set is empty")
    results: list[Mapping[str, object]] = []
    with tempfile.TemporaryDirectory(
        prefix="kauri-v13-readiness-inputs-"
    ) as directory:
        temporary = Path(directory)
        for ordinal, raw_cycle in enumerate(cycles, start=1):
            cycle = _mapping(raw_cycle, "v13 certified readiness cycle")
            identity = _mapping(cycle.get("identity"), "v13 certified identity")
            certificate = cycle.get("certificate_wire")
            signers = _sequence(cycle.get("signers"), "v13 certified signers")
            if not isinstance(certificate, bytes) or not certificate or not signers:
                _error("v13 certified readiness artifacts are incomplete")
            identity_path = temporary / f"epoch{ordinal}.identity.hex"
            certificate_path = temporary / f"epoch{ordinal}.certificate.hex"
            identity_path.write_text(
                _v13_encode_ready_identity(identity).hex() + "\n",
                encoding="ascii",
            )
            certificate_path.write_text(
                certificate.hex() + "\n", encoding="ascii"
            )
            results.append(
                _validate_v13_readiness_verifier(
                    root,
                    contract,
                    selected_projection,
                    trusted_provenance=trusted_provenance,
                    readiness_verifier_path=readiness_verifier_path,
                    certificate_path=certificate_path,
                    identity_path=identity_path,
                    expected_certificate_digest=_mapping(
                        cycle.get("certificate"), "v13 readiness certificate"
                    ).get("certificate_digest"),
                    expected_observation_count=len(signers),
                )
            )
    return tuple(results)


class _V13ReadinessReader:
    def __init__(self, payload: bytes) -> None:
        self.payload, self.offset = payload, 0

    def take(self, size: int) -> bytes:
        if size < 0 or self.offset + size > len(self.payload):
            _error("v13 readiness wire is truncated")
        value = self.payload[self.offset:self.offset + size]
        self.offset += size
        return value

    def integer(self, size: int) -> int:
        return int.from_bytes(self.take(size), "big")

    def domain(self, expected: bytes) -> None:
        if self.take(len(expected)) != expected:
            _error("v13 readiness wire domain drifted")

    def eof(self) -> None:
        if self.offset != len(self.payload):
            _error("v13 readiness wire has trailing bytes")

    def remaining(self) -> int:
        return len(self.payload) - self.offset


def _v13_ready_digest(value: object, label: str) -> bytes:
    return bytes.fromhex(_digest(value, label))


def _v13_ready_u(value: object, bits: int, label: str) -> bytes:
    if type(value) is not int or not 0 <= value < (1 << bits):
        _error(f"{label} is not an unsigned {bits}-bit integer")
    return value.to_bytes(bits // 8, "big")


def _v13_ready_configuration(value: object, label: str) -> tuple[dict[str, object], bytes]:
    item = _mapping(value, label)
    if set(item) != {"epoch_number", "tree_id", "epoch_digest"}:
        _error(f"{label} schema drifted")
    epoch = _v13_ready_u(item.get("epoch_number"), 32, f"{label} epoch")
    tree = _v13_ready_u(item.get("tree_id"), 32, f"{label} tree")
    digest = _v13_ready_digest(item.get("epoch_digest"), f"{label} digest")
    if digest == bytes(32):
        _error(f"{label} digest is zero")
    return dict(item), epoch + tree + digest


def _v13_ready_identity_bytes(value: object) -> tuple[dict[str, object], bytes]:
    item = _mapping(value, "v13 readiness identity")
    expected = {"schema_version", "membership_digest", "predecessor_boundary_configuration",
                "predecessor_boundary_generation", "successor_configuration",
                "successor_activation_generation", "command_payload_digest", "command_block_height",
                "command_block_hash", "activation_delay_blocks", "activation_height",
                "activation_boundary_block_hash"}
    if set(item) != expected or item.get("schema_version") != 1:
        _error("v13 readiness identity schema drifted")
    membership = _v13_ready_digest(item.get("membership_digest"), "v13 readiness membership digest")
    predecessor, predecessor_bytes = _v13_ready_configuration(item.get("predecessor_boundary_configuration"), "v13 predecessor configuration")
    successor, successor_bytes = _v13_ready_configuration(item.get("successor_configuration"), "v13 successor configuration")
    predecessor_generation = item.get("predecessor_boundary_generation")
    successor_generation = item.get("successor_activation_generation")
    predecessor_epoch, successor_epoch = predecessor["epoch_number"], successor["epoch_number"]
    if (membership == bytes(32) or predecessor_epoch == 0xFFFFFFFF or successor_epoch != predecessor_epoch + 1
        or successor["tree_id"] != 0 or type(predecessor_generation) is not int or predecessor_generation == 0
        or predecessor_generation > (1 << 64) - 1 or (predecessor_generation - 1) >> 32 != predecessor_epoch
        or successor_generation != (successor_epoch << 32) + 1):
        _error("v13 readiness identity generation or epoch drifted")
    command_digest = _v13_ready_digest(item.get("command_payload_digest"), "v13 command payload digest")
    command_height = item.get("command_block_height")
    command_hash = _v13_ready_digest(item.get("command_block_hash"), "v13 command block hash")
    delay, activation = item.get("activation_delay_blocks"), item.get("activation_height")
    boundary = _v13_ready_digest(item.get("activation_boundary_block_hash"), "v13 activation boundary hash")
    if (command_digest == bytes(32) or command_hash == bytes(32) or boundary == bytes(32)
        or type(command_height) is not int or command_height <= 0 or type(delay) is not int or delay <= 0
        or command_height > (1 << 64) - 1 - delay or activation != command_height + delay
        or predecessor["epoch_digest"] == successor["epoch_digest"]):
        _error("v13 readiness identity transition drifted")
    encoded = (b"\x00\x00\x00\x01" + membership + predecessor_bytes + _v13_ready_u(predecessor_generation, 64, "predecessor generation")
               + successor_bytes + _v13_ready_u(successor_generation, 64, "successor generation") + command_digest
               + _v13_ready_u(command_height, 64, "command height") + command_hash + _v13_ready_u(delay, 64, "activation delay")
               + _v13_ready_u(activation, 64, "activation height") + boundary)
    return dict(item), encoded


def _v13_decode_ready_identity(payload: bytes) -> dict[str, object]:
    reader = _V13ReadinessReader(payload); reader.domain(_V13_READY_IDENTITY_DOMAIN)
    if reader.integer(1) != 0: _error("v13 readiness identity flags drifted")
    value = _v13_decode_identity_body(reader); reader.eof()
    if _v13_encode_ready_identity(value) != payload: _error("v13 readiness identity is noncanonical")
    return value


def _v13_decode_identity_body(reader: _V13ReadinessReader) -> dict[str, object]:
    def configuration() -> dict[str, object]:
        return {"epoch_number": reader.integer(4), "tree_id": reader.integer(4), "epoch_digest": reader.take(32).hex()}
    value = {"schema_version": reader.integer(4), "membership_digest": reader.take(32).hex(),
             "predecessor_boundary_configuration": configuration(), "predecessor_boundary_generation": reader.integer(8),
             "successor_configuration": configuration(), "successor_activation_generation": reader.integer(8),
             "command_payload_digest": reader.take(32).hex(), "command_block_height": reader.integer(8),
             "command_block_hash": reader.take(32).hex(), "activation_delay_blocks": reader.integer(8),
             "activation_height": reader.integer(8), "activation_boundary_block_hash": reader.take(32).hex()}
    _v13_ready_identity_bytes(value)
    return value


def _v13_encode_ready_identity(value: object) -> bytes:
    _identity, body = _v13_ready_identity_bytes(value)
    return _V13_READY_IDENTITY_DOMAIN + b"\0" + body


def _v13_observation_body(value: object) -> tuple[dict[str, object], bytes]:
    item = _mapping(value, "v13 readiness observation")
    if set(item) != {"identity", "signer_replica_id", "signer_source_sequence", "signer_monotonic_raw_ns", "vote_fence_engaged", "signature_hex"}:
        _error("v13 readiness observation schema drifted")
    identity, identity_bytes = _v13_ready_identity_bytes(item.get("identity"))
    signature = item.get("signature_hex")
    if item.get("vote_fence_engaged") is not True or not isinstance(signature, str) or len(signature) != 192 or any(c not in "0123456789abcdef" for c in signature):
        _error("v13 readiness observation fence or signature drifted")
    sequence = item.get("signer_source_sequence")
    if type(sequence) is not int or sequence == 0: _error("v13 readiness observation sequence drifted")
    encoded = identity_bytes + _v13_ready_u(item.get("signer_replica_id"), 16, "observation signer") + _v13_ready_u(sequence, 64, "observation sequence") + _v13_ready_u(item.get("signer_monotonic_raw_ns"), 64, "observation tick") + b"\x01" + bytes.fromhex(signature)
    return {**dict(item), "identity": identity}, encoded


def _v13_observation_signing_digest(value: object) -> str:
    item, body = _v13_observation_body(value)
    return _sha_bytes(_V13_READY_OBSERVATION_DOMAIN + body[:-96])


def _v13_encode_ready_observation(value: object) -> bytes:
    _item, body = _v13_observation_body(value)
    return _V13_READY_OBSERVATION_DOMAIN + b"\0" + body


def _v13_decode_ready_observation(payload: bytes) -> dict[str, object]:
    reader = _V13ReadinessReader(payload); reader.domain(_V13_READY_OBSERVATION_DOMAIN)
    if reader.integer(1) != 0: _error("v13 readiness observation flags drifted")
    value = _v13_decode_observation_body(reader); reader.eof()
    if _v13_encode_ready_observation(value) != payload: _error("v13 readiness observation is noncanonical")
    return value


def _v13_decode_observation_body(reader: _V13ReadinessReader) -> dict[str, object]:
    value = {"identity": _v13_decode_identity_body(reader), "signer_replica_id": reader.integer(2), "signer_source_sequence": reader.integer(8), "signer_monotonic_raw_ns": reader.integer(8), "vote_fence_engaged": reader.integer(1) == 1, "signature_hex": reader.take(96).hex()}
    _v13_observation_body(value)
    return value


def _v13_certificate_digest(identity: object, observations: Sequence[object]) -> str:
    _identity, identity_bytes = _v13_ready_identity_bytes(identity)
    bodies = [_v13_observation_body(observation)[1] for observation in observations]
    return _sha_bytes(_V13_READY_CERTIFICATE_DIGEST_DOMAIN + b"\0\0\0\1" + identity_bytes + _v13_ready_u(len(bodies), 32, "certificate observation count") + b"".join(bodies))


def _v13_certificate_body(value: object) -> tuple[dict[str, object], bytes]:
    item = _mapping(value, "v13 readiness certificate")
    if set(item) != {"schema_version", "identity", "observations", "certificate_digest"} or item.get("schema_version") != 1:
        _error("v13 readiness certificate schema drifted")
    observations_value = item.get("observations")
    if not isinstance(observations_value, list) or not observations_value:
        _error("v13 readiness certificate observations drifted")
    identity, identity_bytes = _v13_ready_identity_bytes(item.get("identity"))
    observations = [_v13_observation_body(value)[0] for value in observations_value]
    if any(observation["identity"] != identity for observation in observations) or any(
        observations[index - 1]["signer_replica_id"] >= observations[index]["signer_replica_id"]
        for index in range(1, len(observations))
    ):
        _error("v13 readiness certificate signer ordering or identity drifted")
    digest = _v13_certificate_digest(identity, observations)
    if _digest(item.get("certificate_digest"), "v13 certificate digest") != digest:
        _error("v13 readiness certificate digest drifted")
    encoded = b"\0\0\0\1" + identity_bytes + _v13_ready_u(len(observations), 32, "certificate observation count") + b"".join(_v13_observation_body(value)[1] for value in observations) + bytes.fromhex(digest)
    return {"schema_version": 1, "identity": identity, "observations": observations, "certificate_digest": digest}, encoded


def _v13_encode_readiness_certificate(value: object) -> bytes:
    _item, body = _v13_certificate_body(value)
    return _V13_READY_CERTIFICATE_DOMAIN + b"\0" + body


def _v13_decode_readiness_certificate(payload: bytes, *, maximum_members: int,
                                      maximum_payload_bytes: int) -> dict[str, object]:
    if type(maximum_members) is not int or maximum_members < 1 or type(maximum_payload_bytes) is not int or maximum_payload_bytes < 1:
        _error("v13 readiness certificate limits are invalid")
    if len(payload) > maximum_payload_bytes:
        _error("v13 readiness certificate exceeds byte limit")
    reader = _V13ReadinessReader(payload); reader.domain(_V13_READY_CERTIFICATE_DOMAIN)
    if reader.integer(1) != 0: _error("v13 readiness certificate flags drifted")
    schema, identity, count = reader.integer(4), _v13_decode_identity_body(reader), reader.integer(4)
    # Each encoded observation is fixed-width once its identity is present.
    # Reject hostile counts before allocating/ranging, as native does.
    minimum_observation_bytes = 4 + 32 + 40 + 8 + 40 + 8 + 32 + 8 + 32 + 8 + 8 + 32 + 2 + 8 + 8 + 1 + 96
    if schema != 1 or count == 0 or count > maximum_members or count > reader.remaining() // minimum_observation_bytes:
        _error("v13 readiness certificate schema or count drifted")
    observations = [_v13_decode_observation_body(reader) for _ in range(count)]
    value = {"schema_version": schema, "identity": identity, "observations": observations, "certificate_digest": reader.take(32).hex()}
    reader.eof(); _v13_certificate_body(value)
    if _v13_encode_readiness_certificate(value) != payload: _error("v13 readiness certificate is noncanonical")
    return value


def _v13_ack_payload_digest(opcode: object, payload: bytes) -> str:
    if opcode not in {_V13_READY_OBSERVATION_OPCODE, _V13_READY_CERTIFICATE_OPCODE}:
        _error("v13 readiness acknowledgement opcode drifted")
    return _sha_bytes(_V13_READY_ACK_PAYLOAD_DIGEST_DOMAIN + _v13_ready_u(opcode, 8, "acknowledged opcode") + _v13_ready_u(len(payload), 64, "acknowledged payload size") + payload)


def _v13_ack_body(value: object) -> tuple[dict[str, object], bytes]:
    item = _mapping(value, "v13 readiness acknowledgement")
    expected = {"schema_version", "acknowledged_opcode", "recipient_replica_id", "identity", "certificate_digest", "payload_digest", "disposition"}
    if set(item) != expected or item.get("schema_version") != 1 or item.get("acknowledged_opcode") not in {_V13_READY_OBSERVATION_OPCODE, _V13_READY_CERTIFICATE_OPCODE} or item.get("disposition") not in {1, 2}:
        _error("v13 readiness acknowledgement schema, opcode, or disposition drifted")
    identity, identity_bytes = _v13_ready_identity_bytes(item.get("identity"))
    certificate = _v13_ready_digest(item.get("certificate_digest"), "ack certificate digest")
    payload = _v13_ready_digest(item.get("payload_digest"), "ack payload digest")
    if certificate == bytes(32) or payload == bytes(32): _error("v13 readiness acknowledgement digest is zero")
    body = b"\0\0\0\1" + _v13_ready_u(item.get("acknowledged_opcode"), 8, "ack opcode") + _v13_ready_u(item.get("recipient_replica_id"), 16, "ack recipient") + identity_bytes + certificate + payload + _v13_ready_u(item.get("disposition"), 8, "ack disposition")
    return {**dict(item), "identity": identity}, body


def _v13_encode_readiness_ack(value: object) -> bytes:
    _item, body = _v13_ack_body(value)
    return _V13_READY_ACK_DOMAIN + b"\0" + body


def _v13_decode_readiness_ack(payload: bytes) -> dict[str, object]:
    reader = _V13ReadinessReader(payload); reader.domain(_V13_READY_ACK_DOMAIN)
    if reader.integer(1) != 0: _error("v13 readiness acknowledgement flags drifted")
    value = {"schema_version": reader.integer(4), "acknowledged_opcode": reader.integer(1), "recipient_replica_id": reader.integer(2), "identity": _v13_decode_identity_body(reader), "certificate_digest": reader.take(32).hex(), "payload_digest": reader.take(32).hex(), "disposition": reader.integer(1)}
    reader.eof(); _v13_ack_body(value)
    if _v13_encode_readiness_ack(value) != payload: _error("v13 readiness acknowledgement is noncanonical")
    return value


def _validate_v13_readiness_wire_chain(
    identity_payload: bytes, observation_payload: bytes, certificate_payload: bytes, ack_payload: bytes,
    *, maximum_members: int, maximum_payload_bytes: int
) -> Mapping[str, object]:
    identity = _v13_decode_ready_identity(identity_payload)
    observation = _v13_decode_ready_observation(observation_payload)
    certificate = _v13_decode_readiness_certificate(certificate_payload, maximum_members=maximum_members,
                                                     maximum_payload_bytes=maximum_payload_bytes)
    acknowledgement = _v13_decode_readiness_ack(ack_payload)
    if (observation["identity"] != identity or certificate["identity"] != identity or acknowledgement["identity"] != identity
        or acknowledgement["certificate_digest"] != certificate["certificate_digest"]
        or acknowledgement["acknowledged_opcode"] != _V13_READY_CERTIFICATE_OPCODE
        or acknowledgement["disposition"] != 1
        or not any(candidate == observation for candidate in certificate["observations"])
        or acknowledgement["payload_digest"] != _v13_ack_payload_digest(_V13_READY_CERTIFICATE_OPCODE, certificate_payload)):
        _error("v13 readiness wire chain binding drifted")
    return {"identity": identity, "observation": observation, "certificate": certificate, "acknowledgement": acknowledgement,
            "observation_signing_digest": _v13_observation_signing_digest(observation),
            "certificate_payload_digest": _sha_bytes(certificate_payload),
            "ack_payload_digest": acknowledgement["payload_digest"]}


def _validate_fault_window_arm(
    root: Path,
    contract: Mapping[str, object],
    argv: Sequence[Any],
    fault_receipt: Mapping[str, Any],
    confirmations: Mapping[int, int],
    events: Sequence[Mapping[str, Any]],
    *,
    snapshot_audit_ns: int | None,
) -> Mapping[str, object] | None:
    """Independently bind the persisted v4 arm; it is never evidence itself."""

    if not _is_v4_contract(contract):
        return None
    is_v6 = (
        _is_v6_contract(contract)
        or _is_v7_contract(contract)
        or _is_v8_or_v9_contract(contract)
    )
    is_v7 = _is_v7_contract(contract) or _is_v8_or_v9_contract(contract)
    is_v9 = _is_v9_contract(contract)
    path = (root / "runtime" / _FAULT_WINDOW_ARM_FILENAME).resolve()
    if (
        path.parent != (root / "runtime").resolve()
        or path.is_symlink()
        or not path.is_file()
    ):
        _error("v4 fault-window arm is absent or escapes the child root")
    arm = _read_json(path, "fault-window arm")
    arm_bytes = path.read_bytes()
    if arm_bytes != _canonical(arm):
        _error("v4 fault-window arm bytes are not canonical")
    expected_keys = {
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
    if is_v6:
        expected_keys |= {
            "clock_domain",
            "required_observation_schema",
            "timeout_evidence_basis",
        }
    if is_v7:
        expected_keys.add("snapshot_evidence_basis")
    if is_v9:
        expected_keys.add("selection_cardinality_policy")
    if (
        set(arm) != expected_keys
        or arm.get("schema_version")
        != (4 if is_v9 else 3 if is_v7 else 2 if is_v6 else 1)
        or arm.get("kind")
        != (
            _FAULT_WINDOW_ARM_DOMAIN_V4
            if is_v9
            else (
                _FAULT_WINDOW_ARM_DOMAIN_V3
                if is_v7
                else (
                    _FAULT_WINDOW_ARM_DOMAIN_V2
                    if is_v6
                    else _FAULT_WINDOW_ARM_DOMAIN_V1
                )
            )
        )
    ):
        _error("v4 fault-window arm schema drifted")
    _integer(arm.get("schema_version"), "fault-window schema version", 1)
    if (is_v6 or is_v7) and (
        arm.get("clock_domain") != "same_host_clock_monotonic_raw"
        or arm.get("required_observation_schema") != 3
        or arm.get("timeout_evidence_basis") != "exact_timeout_attempt_id_v1"
    ):
        _error("v6 fault-window arm timeout evidence binding drifted")
    if (
        is_v7
        and arm.get("snapshot_evidence_basis") != "exact_post_fault_attempt_start_v1"
    ):
        _error("v7 fault-window arm snapshot evidence binding drifted")
    if (
        is_v9
        and arm.get("selection_cardinality_policy")
        != "all_guarded_up_to_fault_bound_v1"
    ):
        _error("v9 fault-window arm cardinality policy drifted")
    _integer(arm.get("epoch_number"), "fault-window epoch number")
    _integer(arm.get("evidence_start_monotonic_ns"), "fault-window evidence start", 1)
    for key in (
        "profile_sha256",
        "topology_proof_sha256",
        "request_sha256",
        "epoch_digest",
        "fault_receipt_sha256",
    ):
        _digest(arm.get(key), f"fault-window {key}")
    profile = _mapping(contract["profile"], "focused profile")
    v13_selected_projection = _validate_parent_authorization_projection(root, contract)
    coverage = _mapping(contract["reporter_coverage_plan"], "reporter coverage plan")
    parent_request_path = root / "runtime" / "parent-authorization-request.json"
    parent_receipt_path = root / "runtime" / "parent-authorization-receipt.json"
    parent_request = _read_json(parent_request_path, "parent authorization request")
    parent_receipt = _read_json(parent_receipt_path, "parent authorization receipt")
    parent_request_bytes = parent_request_path.read_bytes()
    parent_receipt_bytes = parent_receipt_path.read_bytes()
    if parent_request_bytes != _canonical(
        parent_request
    ) or parent_receipt_bytes != _canonical(parent_receipt):
        _error("parent authorization provenance is not canonical")
    request_keys = set(parent_request)
    expected_request_keys = {
        "schema_version",
        "mode",
        "pair_count",
        "profile_sha256",
        "topology_proof_sha256",
        "output_root",
        "automatic_retries",
        "replacement_policy",
        "authorization_nonce",
    }
    is_v13 = profile.get("profile_id") in _FCRASH_H_V13_PROFILE_IDS
    v13_request_keys = expected_request_keys | {
        "execution_context_sha256", "pair_readiness_manifests"
    }
    if is_v13:
        if request_keys != v13_request_keys or parent_request.get("schema_version") != 2:
            _error("v13 parent authorization request schema drifted")
        _digest(parent_request.get("execution_context_sha256"),
                "parent execution-context digest")
        projection = _validate_v13_parent_readiness_projection(
            parent_request.get("pair_readiness_manifests"),
            pair_count=_integer(parent_request.get("pair_count"), "authorized pair count", 1),
            member_count=_integer(_mapping(profile.get("protocol"), "profile protocol").get("N"), "profile N", 1),
        )
    elif request_keys == expected_request_keys | {"execution_context_sha256"}:
        _digest(
            parent_request.get("execution_context_sha256"),
            "parent execution-context digest",
        )
    elif request_keys != expected_request_keys:
        _error("parent authorization request schema drifted")
    parent_request_sha = _sha_bytes(parent_request_bytes)
    expected_receipt_keys = request_keys | {
        "request_sha256",
        "approval_reference",
        "approved_utc",
    }
    if (
        set(parent_receipt) != expected_receipt_keys
        or any(
            parent_receipt.get(key) != parent_request.get(key) for key in request_keys
        )
        or parent_receipt.get("request_sha256") != parent_request_sha
        or parent_request.get("schema_version") != (2 if is_v13 else 1)
        or parent_request.get("mode") not in {"pair", "smoke", "campaign"}
        or type(parent_request.get("pair_count")) is not int
        or parent_request["pair_count"] < 1
        or parent_request.get("profile_sha256") != contract["profile_sha256"]
        or parent_request.get("topology_proof_sha256")
        != contract["topology_proof_sha256"]
        or parent_request.get("automatic_retries") != 0
        or parent_request.get("replacement_policy") != "none"
        or not isinstance(parent_request.get("output_root"), str)
        or not Path(parent_request["output_root"]).is_absolute()
        or not isinstance(parent_request.get("authorization_nonce"), str)
        or not parent_request["authorization_nonce"]
    ):
        _error("parent authorization provenance binding drifted")
    expected_pairs = (
        {"smoke": 1}
        if len(tuple(contract["members"])) == 7
        else {"pair": 1, "campaign": 5}
    )
    mode = str(parent_request["mode"])
    if expected_pairs.get(mode) != parent_request["pair_count"] or parent_request[
        "authorization_nonce"
    ] != _sha_bytes(
        f"{mode}:{parent_request['pair_count']}:{parent_request['output_root']}".encode(
            "utf-8"
        )
    ):
        _error("parent authorization mode, pair count, or nonce drifted")
    approval_reference = parent_receipt.get("approval_reference")
    approved_utc = parent_receipt.get("approved_utc")
    if (
        not isinstance(approval_reference, str)
        or not 1 <= len(approval_reference) <= 200
        or any(not 32 <= ord(character) <= 126 for character in approval_reference)
        or not isinstance(approved_utc, str)
        or not 1 <= len(approved_utc) <= 64
        or any(not 32 <= ord(character) <= 126 for character in approved_utc)
    ):
        _error("parent authorization approval metadata drifted")
    _parse_utc_approval_time(approved_utc, "parent authorization")
    pair_receipt = _read_json(root / "pair-receipt.json", "pair receipt")
    pair_id = pair_receipt.get("pair_id")
    if (
        not isinstance(pair_id, str)
        or not pair_id.startswith("pair-")
        or not pair_id[5:].isdigit()
        or not 1 <= int(pair_id[5:]) <= int(parent_request["pair_count"])
        or (
            not is_v13
            and (
                root.parent.name != pair_id
                or root.name not in {"control", "adaptive"}
            )
        )
    ):
        _error("parent authorization output or pair binding drifted")
    if is_v13:
        # Expose only the selected public projection; later v13 validation
        # binds it to the public manifest without reopening authorization.
        selected_projection = v13_selected_projection
    historical_arm_path = (
        path
        if is_v13
        else Path(parent_request["output_root"])
        / pair_id
        / root.name
        / "runtime"
        / _FAULT_WINDOW_ARM_FILENAME
    )
    receipt_path = root / "raw" / "fault-receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    if receipt_bytes != _canonical(fault_receipt):
        _error("v4 fault receipt bytes are not canonical")
    run_ids = {event.get("run_id") for event in events}
    if len(run_ids) != 1 or arm.get("run_id") != next(iter(run_ids)):
        _error("v4 fault-window arm run identity drifted")
    prefault_tree = _integer(arm.get("prefault_tree_id"), "fault-window pre-fault tree")
    positions = _integer(
        arm.get("required_tree_positions"), "fault-window required positions", 1
    )
    required_ids = tuple(
        _integer(value, "fault-window required tree")
        for value in _sequence(
            arm.get("required_tree_ids"), "fault-window required trees"
        )
    )
    if (
        arm.get("profile_id") != contract["profile_id"]
        or arm.get("profile_sha256") != contract["profile_sha256"]
        or arm.get("topology_proof_sha256") != contract["topology_proof_sha256"]
        or arm.get("request_sha256") != parent_request_sha
        or arm.get("epoch_number") != 0
        or arm.get("epoch_digest") != contract["epoch_zero_digest"]
        or arm.get("fault_receipt_sha256") != _sha_bytes(receipt_bytes)
        or arm.get("evidence_start_monotonic_ns") != max(confirmations.values())
        or prefault_tree
        != _mapping(coverage, "reporter coverage plan").get("active_tree_id")
        or positions != coverage.get("required_postfault_tree_positions")
        or required_ids
        != tuple(
            (prefault_tree + offset) % len(tuple(contract["members"]))
            for offset in range(positions)
        )
    ):
        _error("v4 fault-window arm binding drifted")
    pairs = dict(zip(argv[1::2], argv[2::2], strict=True))
    expected_argv = {
        "--fault-window-arm-path": str(historical_arm_path),
        "--fault-window-arm-schema-version": (
            "4" if is_v9 else "3" if is_v7 else "2" if is_v6 else "1"
        ),
        "--fault-window-arm-domain": (
            _FAULT_WINDOW_ARM_DOMAIN_V4
            if is_v9
            else (
                _FAULT_WINDOW_ARM_DOMAIN_V3
                if is_v7
                else (
                    _FAULT_WINDOW_ARM_DOMAIN_V2
                    if is_v6
                    else _FAULT_WINDOW_ARM_DOMAIN_V1
                )
            )
        ),
        "--fault-window-arm-run-id": str(arm["run_id"]),
        "--fault-window-arm-profile-id": str(arm["profile_id"]),
        "--fault-window-arm-profile-sha256": str(arm["profile_sha256"]),
        "--fault-window-arm-topology-proof-sha256": str(arm["topology_proof_sha256"]),
        "--fault-window-arm-request-sha256": str(arm["request_sha256"]),
        "--fault-window-arm-epoch-number": "0",
        "--fault-window-arm-epoch-digest": str(arm["epoch_digest"]),
        "--fault-window-arm-prefault-tree-id": str(arm["prefault_tree_id"]),
        "--fault-window-arm-required-tree-positions": str(
            arm["required_tree_positions"]
        ),
        "--fault-window-arm-deadline-seconds": str(
            _mapping(coverage["deadlines_seconds"], "coverage deadlines")[
                "arm_hard_seconds"
            ]
        ),
    }
    if is_v6:
        expected_argv.update(
            {
                "--fault-window-arm-clock-domain": "same_host_clock_monotonic_raw",
                "--fault-window-arm-required-observation-schema": "3",
                "--fault-window-arm-timeout-evidence-basis": "exact_timeout_attempt_id_v1",
            }
        )
    if is_v7:
        expected_argv["--fault-window-arm-snapshot-evidence-basis"] = (
            "exact_post_fault_attempt_start_v1"
        )
    if is_v9:
        expected_argv["--fault-window-arm-selection-cardinality-policy"] = (
            "all_guarded_up_to_fault_bound_v1"
        )
    if any(pairs.get(key) != value for key, value in expected_argv.items()):
        _error("v4 manager arm bindings differ from the persisted arm")
    arm_sha = _sha_bytes(arm_bytes)
    all_armed = [
        event for event in events if event["event_type"] == "fault_window_armed"
    ]
    armed = [
        event
        for event in events
        if event["event_type"] == "fault_window_armed"
        and event["source_kind"] == "adaptation_manager"
        and event["source_id"] == "adaptive-manager"
    ]
    if len(all_armed) != 1 or len(armed) != 1 or snapshot_audit_ns is None:
        _error("v4 fault-window armed event is absent or ambiguous")
    event = armed[0]
    manager_instances = {
        candidate["source_instance"]
        for candidate in events
        if candidate["source_kind"] == "adaptation_manager"
        and candidate["source_id"] == "adaptive-manager"
    }
    if len(manager_instances) != 1 or event["source_instance"] not in manager_instances:
        _error("v4 fault-window armed event manager instance drifted")
    payload = _mapping(event["payload"], "fault-window armed payload")
    _integer(payload.get("schema_version"), "armed event schema version", 1)
    _integer(payload.get("epoch_number"), "armed event epoch number")
    _integer(
        payload.get("evidence_start_monotonic_ns"), "armed event evidence start", 1
    )
    _integer(payload.get("prefault_tree_id"), "armed event pre-fault tree")
    _integer(payload.get("required_tree_positions"), "armed event tree positions", 1)
    for key in (
        "profile_sha256",
        "topology_proof_sha256",
        "request_sha256",
        "epoch_digest",
        "fault_receipt_sha256",
        "fault_window_arm_sha256",
    ):
        _digest(payload.get(key), f"armed event {key}")
    tuple(
        _integer(value, "armed event required tree")
        for value in _sequence(payload.get("required_tree_ids"), "armed event trees")
    )
    if dict(payload) != {**dict(arm), "fault_window_arm_sha256": arm_sha}:
        _error("v4 fault-window armed event payload drifted")
    armed_ns = _integer(event["source_monotonic_ns"], "fault-window armed timestamp")
    if not int(arm["evidence_start_monotonic_ns"]) <= armed_ns < snapshot_audit_ns:
        _error("v4 fault-window arm was not accepted before snapshot audit")
    if not _v12_arm_before_configuration_coverage_deadline(
        contract,
        coverage,
        evidence_start_ns=int(arm["evidence_start_monotonic_ns"]),
        armed_ns=armed_ns,
    ):
        _error("v12 fault-window arm exceeded the configuration coverage cap")
    requests = [
        _integer(
            _mapping(outcome, "SIGKILL outcome").get("requested_monotonic_ns"),
            "fault request",
            1,
        )
        for outcome in _sequence(
            fault_receipt.get("sigkill_outcomes"), "SIGKILL outcomes"
        )
    ]
    if not requests:
        _error("v4 fault-window arm lacks fault requests")
    prearm_progress = _fcrash_h_postfault_progress(
        contract,
        events,
        fault_ns=max(confirmations.values()),
        prefault_ns=min(requests),
        audit_ns=armed_ns,
    )
    observed_before_arm = tuple(
        _integer(tree, "pre-arm observed tree")
        for tree in _sequence(
            prearm_progress.get("observed_tree_ids"), "pre-arm observed trees"
        )
    )
    if observed_before_arm[:positions] != required_ids:
        _error("v4 fault-window arm preceded its authoritative tree horizon")
    return selected_projection if is_v13 else None


def _aggregate_child_provenance(
    trusted_provenance: object, directory: Path, *, expected_arm: str | None = None
) -> Mapping[str, Any]:
    aggregate = _mapping(trusted_provenance, "aggregate trusted provenance")
    if (
        set(aggregate) != {"schema_version", "children"}
        or aggregate.get("schema_version") != 1
    ):
        _error("aggregate trusted provenance schema drifted")
    children = _mapping(aggregate.get("children"), "trusted child provenance")
    if expected_arm is not None:
        if expected_arm not in {"control", "adaptive"} or set(children) != {
            "control",
            "adaptive",
        }:
            _error("aggregate trusted provenance arm binding drifted")
        raw_entry = children[expected_arm]
        entry = _mapping(raw_entry, "trusted child entry")
        if set(entry) != {"tree_sha256", "seal_sha256", "provenance"}:
            _error("trusted child entry schema drifted")
        seal = verify_evidence_seal(directory)
        if (
            entry.get("tree_sha256") != seal.tree_sha256
            or entry.get("seal_sha256") != seal.seal_sha256
        ):
            _error("trusted provenance arm entry does not bind its child")
        provenance = _mapping(entry.get("provenance"), "child provenance")
        if (
            provenance.get("evidence_tree_sha256") != seal.tree_sha256
            or provenance.get("evidence_seal_sha256") != seal.seal_sha256
        ):
            _error("trusted child provenance seal binding drifted")
        return provenance
    seal = verify_evidence_seal(directory)
    matches: list[Mapping[str, Any]] = []
    for raw_entry in children.values():
        entry = _mapping(raw_entry, "trusted child entry")
        if set(entry) != {"tree_sha256", "seal_sha256", "provenance"}:
            _error("trusted child entry schema drifted")
        if (
            entry.get("tree_sha256") == seal.tree_sha256
            and entry.get("seal_sha256") == seal.seal_sha256
        ):
            provenance = _mapping(entry.get("provenance"), "child provenance")
            if (
                provenance.get("evidence_tree_sha256") != seal.tree_sha256
                or provenance.get("evidence_seal_sha256") != seal.seal_sha256
            ):
                _error("trusted child provenance seal binding drifted")
            matches.append(provenance)
    if len(matches) != 1:
        _error("trusted provenance does not bind exactly one child")
    return matches[0]


def _validate_receipts(
    root: Path, profile_sha: str, proof_sha: str
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    manifest = _read_json(root / "manifest.json", "arm manifest")
    build = _read_json(root / "runtime" / "build-provenance.json", "build provenance")
    effective = _read_json(
        root / "runtime" / "effective-runtime.json", "runtime identity"
    )
    pair = _read_json(root / "pair-receipt.json", "pair receipt")
    if (
        set(manifest)
        != {
            "schema_version",
            "profile_sha256",
            "build_sha256",
            "pair_id",
            "pair_seed",
            "slot_id",
        }
        or set(build) != {"revision", "build_sha256"}
        or set(effective) != {"profile_sha256", "pair_seed"}
        or set(pair)
        != {
            "schema_version",
            "pair_id",
            "slot_id",
            "automatic_retries",
            "replacement_policy",
        }
        or manifest.get("schema_version") != 1
        or manifest.get("profile_sha256") != profile_sha
        or manifest.get("build_sha256") != build.get("build_sha256")
        or manifest.get("pair_seed") != effective.get("pair_seed")
        or effective.get("profile_sha256") != profile_sha
        or manifest.get("pair_id") != pair.get("pair_id")
        or manifest.get("slot_id") != pair.get("slot_id")
        or pair.get("automatic_retries") != 0
        or pair.get("replacement_policy") != "none"
    ):
        _error("arm manifest, build, runtime, or pair identity drifted")
    preflight = _read_json(root / "preflight.json", "preflight receipt")
    authorization = _read_json(root / "authorization.json", "authorization receipt")
    request_keys = {
        "schema_version",
        "profile_sha256",
        "topology_proof_sha256",
        "pair_id",
        "slot_id",
        "automatic_retries",
        "replacement_policy",
    }
    request = {key: preflight.get(key) for key in request_keys}
    request_sha = _sha_bytes(_canonical(request))
    if (
        set(preflight)
        != request_keys | {"request_sha256", "execution_authorized", "launch_permitted"}
        or set(authorization)
        != request_keys | {"request_sha256", "approval_reference", "approved_utc"}
        or preflight.get("request_sha256") != request_sha
        or preflight.get("execution_authorized") is not False
        or preflight.get("launch_permitted") is not False
        or authorization.get("request_sha256") != request_sha
        or any(authorization.get(key) != value for key, value in request.items())
        or request.get("profile_sha256") != profile_sha
        or request.get("topology_proof_sha256") != proof_sha
        or request.get("automatic_retries") != 0
        or request.get("replacement_policy") != "none"
        or not isinstance(authorization.get("approval_reference"), str)
        or not isinstance(authorization.get("approved_utc"), str)
    ):
        _error("preflight or authorization receipt is not exact and bound")
    return manifest, pair, build


def _validate_atomic_fault_receipt(
    contract: Mapping[str, object], receipt: Mapping[str, Any]
) -> None:
    plan = _mapping(receipt.get("fault_plan"), "fault plan")
    records = tuple(
        _mapping(value, "process record")
        for value in _sequence(receipt.get("process_records"), "process records")
    )
    outcomes = tuple(
        _mapping(value, "SIGKILL outcome")
        for value in _sequence(receipt.get("sigkill_outcomes"), "SIGKILL outcomes")
    )
    journal = tuple(
        _mapping(value, "fault journal event")
        for value in _sequence(receipt.get("fault_journal"), "fault journal")
    )
    targets = tuple(contract["targets"])
    expected_actions = [
        {
            "fault_id": f"crash-replica-{replica}",
            "kind": "replica_group_sigkill",
            "replica_id": replica,
        }
        for replica in targets
    ]
    if (
        set(plan) != {"schema_version", "seed", "scenario", "actions"}
        or plan.get("schema_version") != 1
        or plan.get("actions") != expected_actions
        or _mapping(plan.get("scenario"), "fault scenario")
        != {
            "replica_ids": list(contract["members"]),
            "quorum": contract["quorum"],
            "crash_budget": contract["fault_threshold"],
            "successor_bundle_retry_limit": 1,
        }
        or len(records) != len(targets)
        or len(outcomes) != len(targets)
        or len(journal) != 2 * len(targets)
    ):
        _error("atomic fault plan or evidence cardinality drifted")
    plan_sha = _sha_bytes(_canonical(plan).rstrip(b"\n"))
    requested: list[int] = []
    confirmed: list[int] = []
    for replica, record, outcome in zip(targets, records, outcomes, strict=True):
        identity = {
            "name": f"replica-{replica}",
            "replica_id": replica,
            "pid": record.get("pid"),
            "pgid": record.get("pgid"),
        }
        if (
            dict(record) != identity
            or type(record.get("pid")) is not int
            or record.get("pid") != record.get("pgid")
            or any(outcome.get(key) != value for key, value in identity.items())
            or outcome.get("fault_id") != f"crash-replica-{replica}"
            or outcome.get("signal_number") != 9
            or outcome.get("returncode") != -9
        ):
            _error("SIGKILL outcome does not match its owned process group")
        requested.append(
            _integer(outcome.get("requested_monotonic_ns"), "fault request")
        )
        confirmed.append(
            _integer(outcome.get("confirmed_monotonic_ns"), "fault confirmation")
        )
    if max(requested) >= min(confirmed):
        _error("SIGKILL batch was not requested atomically before confirmation")
    expected_lifecycles = ["started"] * len(targets) + ["terminal"] * len(targets)
    if [event.get("lifecycle") for event in journal] != expected_lifecycles:
        _error("fault journal does not contain one ordered terminal per target")
    for sequence, event in enumerate(journal):
        replica = targets[sequence % len(targets)]
        if (
            event.get("schema_version") != 1
            or event.get("source_id") != "fault-orchestrator"
            or event.get("source_sequence") != sequence
            or event.get("plan_sha256") != plan_sha
            or event.get("fault_id") != f"crash-replica-{replica}"
            or (
                sequence >= len(targets)
                and _mapping(event.get("outcome"), "fault terminal outcome").get(
                    "status"
                )
                != "succeeded"
            )
        ):
            _error("fault journal identity or terminal outcome drifted")


def _validate_v4_pass_terminals(
    events: Sequence[Mapping[str, Any]],
    *,
    contract: Mapping[str, object],
    epoch1: Any,
    epoch2: Any | None,
    commands1: Sequence[Mapping[str, Any]],
    commands2: Sequence[Mapping[str, Any]],
    activations1: Sequence[Mapping[str, Any]],
    activations2: Sequence[Mapping[str, Any]],
) -> None:
    """Bind a v4 PASS to exactly the successful manager-terminal chain."""

    terminals = sorted(
        (
            event
            for event in events
            if event["source_kind"] == "adaptation_manager"
            and event["event_type"] == "adaptive_v2_session_terminal"
        ),
        key=lambda event: _integer(event["source_sequence"], "terminal sequence", 1),
    )
    expected_epochs = ((1, epoch1, commands1, activations1),)
    if epoch2 is not None:
        expected_epochs += ((2, epoch2, commands2, activations2),)
    if len(terminals) != len(expected_epochs):
        _error("v4 PASS manager terminal cardinality drifted")

    for ordinal, (epoch_number, epoch, commands, activations) in enumerate(
        expected_epochs
    ):
        if not commands or not activations:
            _error("v4 PASS terminal lacks a committed transition")
        payload = _mapping(terminals[ordinal]["payload"], "v4 PASS manager terminal")
        if not _validate_v4_manager_terminal_payload(payload):
            _error("v4 PASS manager terminal schema drifted")
        command = _mapping(commands[0]["payload"], "terminal command")
        activation = _mapping(activations[0]["payload"], "terminal activation")
        predecessor_digest = (
            str(contract["epoch_zero_digest"])
            if epoch_number == 1
            else str(epoch1.epoch_digest)
        )
        artifact = (
            "e0-to-e1-containment" if epoch_number == 1 else "e1-to-e2-optimization"
        )
        snapshot = next(
            (
                _mapping(event["payload"], "terminal evidence snapshot")
                for event in events
                if event["source_kind"] == "adaptation_manager"
                and event["event_type"] == "adaptive_v2_evidence_snapshot"
                and event["payload"].get("predecessor_epoch_number") == epoch_number - 1
            ),
            None,
        )
        if snapshot is None:
            _error("v4 PASS terminal lacks its evidence snapshot")
        winning = {
            "predecessor_epoch_number": command.get("predecessor_epoch_number"),
            "predecessor_epoch_digest": command.get("predecessor_epoch_digest"),
            "successor_epoch_number": command.get("successor_epoch_number"),
            "successor_epoch_digest": command.get("successor_epoch_digest"),
            "command_payload_digest": command.get("payload_digest"),
            "command_block_height": command.get("command_block_height"),
            "command_block_hash": command.get("command_block_hash"),
            "activation_delay_blocks": command.get("activation_delay_blocks"),
            "activation_height": activation.get("activation_height"),
        }
        expected = {
            "cycle_ordinal": ordinal,
            "policy_intent": (
                "fault_containment" if epoch_number == 1 else "performance_optimization"
            ),
            "outcome": "advanced",
            "reason": "successor_converged",
            "transition_artifact_id": artifact,
            "predecessor_epoch_number": epoch_number - 1,
            "predecessor_epoch_digest": predecessor_digest,
            "successor_epoch_number": epoch_number,
            "successor_epoch_digest": epoch.epoch_digest,
            "command_payload_digest": epoch.command.payload_digest,
            "winning_activation": winning,
            "controller_failure": None,
            "evidence_window_activation_generation": snapshot.get(
                "activation_generation"
            ),
            "baseline_evidence_cutoff": _integer(
                snapshot.get("baseline_cutoff"),
                "terminal snapshot baseline cutoff",
                0,
            ),
            "current_evidence_cutoff": _integer(
                snapshot.get("current_cutoff"),
                "terminal snapshot current cutoff",
                0,
            ),
        }
        if dict(payload) != expected:
            _error("v4 PASS manager terminal identity drifted")


def _validate_sealed_v13_arm(
    root: Path,
    *,
    seal: Any,
    contract: Mapping[str, object],
    trusted_provenance: Mapping[str, object],
    readiness_verifier_path: Path | None,
) -> dict[str, object]:
    """Validate one exact v13 arm without entering archived validation paths."""

    if readiness_verifier_path is None:
        _error("v13 readiness verifier path is required")
    profile_sha = str(contract["profile_sha256"])
    proof_sha = str(contract["topology_proof_sha256"])
    manifest, _pair_receipt, build = _validate_receipts(
        root, profile_sha, proof_sha
    )
    verifier_sha = _digest(
        trusted_provenance.get("epoch_profile_digest_sha256"),
        "v13 epoch-profile-digest provenance",
    )
    expected_provenance = {
        "schema_version": 1,
        "revision": build.get("revision"),
        "build_sha256": build.get("build_sha256"),
        "profile_sha256": profile_sha,
        "topology_proof_sha256": proof_sha,
        "epoch_profile_digest_sha256": verifier_sha,
        "evidence_tree_sha256": seal.tree_sha256,
        "evidence_seal_sha256": seal.seal_sha256,
    }
    if dict(trusted_provenance) != expected_provenance:
        _error("v13 trusted provenance is not exact or child-seal-bound")

    _validate_runtime_configuration(root, contract)
    transition_state = _validate_v13_certified_transition_fragment(root, contract)
    events = tuple(
        _mapping(event, "v13 sealed event")
        for event in _sequence(
            transition_state.get("events"), "v13 sealed events"
        )
    )
    bundles = tuple(
        _sequence(transition_state.get("bundles"), "v13 decoded bundles")
    )
    transitions = tuple(
        _mapping(value, "v13 certified transition")
        for value in _sequence(
            transition_state.get("transitions"), "v13 certified transitions"
        )
    )
    if len(bundles) not in {1, 2} or len(transitions) != len(bundles):
        _error("v13 sealed transition cardinality drifted")

    (
        containment_ranked_ids,
        containment_observation_ids,
        containment_timeout_targets,
        epoch1_snapshot_id,
        epoch1_cutoff,
        epoch1_audit_ns,
        _epoch1_audited_roots,
    ) = _ranking(
        events,
        bundles[0],
        contract,
        predecessor_epoch=0,
        enforce_expected_nonresponses=False,
    )
    if (
        epoch1_snapshot_id is None
        or epoch1_cutoff is None
        or epoch1_audit_ns is None
        or bundles[0].evidence_snapshot_id != epoch1_snapshot_id
        or bundles[0].evidence_cutoff != epoch1_cutoff
    ):
        _error("v13 Epoch 1 bundle is not bound to its source-blind snapshot")
    epoch1_roots = _containment_roots(containment_ranked_ids, contract)
    _validate_trees(
        bundles[0],
        epoch1_roots,
        "v13 Epoch 1",
        contract,
        containment_timeout_targets,
    )
    if min(
        _uint64(event.get("source_monotonic_ns"), "v13 Epoch 1 command time", 1)
        for event in _sequence(
            transitions[0].get("commands"), "v13 Epoch 1 commands"
        )
    ) <= epoch1_audit_ns:
        _error("v13 Epoch 1 command does not follow its source-blind snapshot")

    ranked_ids = containment_ranked_ids
    observation_ids = containment_observation_ids
    timeout_targets = containment_timeout_targets
    if len(bundles) == 2:
        (
            ranked_ids,
            observation_ids,
            timeout_targets,
            epoch2_snapshot_id,
            epoch2_cutoff,
            epoch2_audit_ns,
            epoch2_roots,
        ) = _ranking(
            events,
            bundles[0],
            contract,
            predecessor_epoch=1,
            inherited_wait_exempt=containment_timeout_targets,
            enforce_expected_nonresponses=False,
        )
        if (
            epoch2_snapshot_id is None
            or epoch2_cutoff is None
            or epoch2_audit_ns is None
            or bundles[1].evidence_snapshot_id != epoch2_snapshot_id
            or bundles[1].evidence_cutoff != epoch2_cutoff
            or not set(timeout_targets).issubset(containment_timeout_targets)
        ):
            _error("v13 Epoch 2 bundle is not bound to its source-blind snapshot")
        _validate_trees(
            bundles[1],
            epoch2_roots,
            "v13 Epoch 2",
            contract,
            containment_timeout_targets,
        )
        e2_eligibility = [
            event
            for event in events
            if event.get("event_type") == "adaptive_v3.e2_eligibility"
        ]
        commands2 = tuple(
            _sequence(transitions[1].get("commands"), "v13 Epoch 2 commands")
        )
        if (
            len(e2_eligibility) != 1
            or _uint64(
                e2_eligibility[0].get("source_monotonic_ns"),
                "v13 E2 eligibility time",
                1,
            )
            >= epoch2_audit_ns
            or min(
                _uint64(
                    event.get("source_monotonic_ns"),
                    "v13 Epoch 2 command time",
                    1,
                )
                for event in commands2
            )
            <= epoch2_audit_ns
        ):
            _error("v13 Epoch 2 selection is outside its atomic audit order")

    fault_join = _validate_v13_fault_join(root, contract, transition_state)
    if tuple(containment_timeout_targets) != tuple(fault_join["targets"]):
        _error("v13 source-blind nonresponse differs from confirmed crash truth")

    launch = _read_json(root / "runtime" / "launch-arguments.json", "launch arguments")
    observed = _read_json(
        root / "runtime" / "manager-observed-argv.json", "observed manager argv"
    )
    manager_input = _read_json(root / "runtime" / "manager-input.json", "manager input")
    manager_argv = _sequence(observed.get("argv"), "observed manager argv")
    if launch.get("manager_argv") != observed.get("argv"):
        _error("requested and observed v13 manager argv differ")
    selected_projection = _validate_v13_parent_authorization_projection(
        root, contract
    )
    public_manifest = _validate_v13_readiness_public_manifest(
        root, contract, selected_projection
    )
    _validate_v13_redacted_launch_arguments(
        root,
        contract,
        launch,
        events,
        public_manifest,
        transition_state["issuer_public_key"],
    )
    _validate_manager_boundary(
        contract,
        manager_argv,
        manager_input,
        [event for event in events if event.get("source_kind") == "adaptation_manager"],
        transition_count=len(bundles),
        readiness_manifest=public_manifest,
    )
    armed_projection = _validate_fault_window_arm(
        root,
        contract,
        manager_argv,
        _mapping(fault_join.get("fault_receipt"), "v13 fault receipt"),
        _mapping(fault_join.get("confirmations"), "v13 fault confirmations"),
        events,
        snapshot_audit_ns=epoch1_audit_ns,
    )
    if dict(_mapping(armed_projection, "v13 armed readiness projection")) != dict(
        selected_projection
    ):
        _error("v13 fault arm changed the readiness authorization projection")
    verifier_results = _validate_v13_certified_readiness_crypto(
        root,
        contract,
        selected_projection,
        transition_state,
        trusted_provenance=trusted_provenance,
        readiness_verifier_path=readiness_verifier_path,
    )

    commits, measurements = _commit_reconstruction(
        root,
        events,
        bundles[0],
        bundles[1] if len(bundles) == 2 else None,
        contract,
    )
    cleanup = _read_json(root / "cleanup.json", "cleanup result")
    if cleanup.get("complete") is not True:
        _error("v13 arm cleanup is incomplete")

    commit_identity = [
        {
            "source_id": event["source_id"],
            "source_instance": event["source_instance"],
            "source_sequence": event["source_sequence"],
            "payload": event["payload"],
            "identity_carriers": list(
                _sequence(
                    event.get("_identity_carriers"),
                    "v13 reconstructed identity carriers",
                )
            ),
        }
        for event in commits
    ]
    epoch_identity = {
        "bundle_sha256": list(transition_state["bundle_digests"]),
        "issuer_public_key_sha256": transition_state["issuer_public_key_sha256"],
        "commands": [
            event["payload"]
            for transition in transitions
            for event in _sequence(transition.get("commands"), "v13 transition commands")
        ],
        "activations": [
            event["payload"]
            for transition in transitions
            for event in _sequence(
                transition.get("activations"), "v13 transition activations"
            )
        ],
        "readiness_verifier_results": list(verifier_results),
    }
    ranking_identity = {
        "source_epoch_digest": bundles[0].epoch_digest,
        "observation_ids": observation_ids,
        "ranked_eligible_replica_ids": ranked_ids,
    }
    source_inventory = _sequence(
        transition_state.get("source_inventory"), "v13 source inventory"
    )
    return {
        "schema_version": 1,
        "verdict": "PASS",
        "outcome": "PASS",
        "integrity_valid": True,
        "claim_slot": True,
        "source_blind_reconstruction": True,
        "fault_receipt_joined_after_reconstruction": True,
        "reconstructed_from_raw_evidence": True,
        "fault_receipt_joined": True,
        "native_bundles_decoded": True,
        "runtime_graph_validated": True,
        "ranking_reconstructed_from_raw": True,
        "certified_readiness_verified": True,
        "guarded_nonresponsive_replica_ids": list(containment_timeout_targets),
        "dependent_nonresponsive_replica_ids": [],
        "exact_crash_equality_claimed": True,
        "epoch2_present": len(bundles) == 2,
        "profile_sha256": profile_sha,
        "topology_proof_sha256": proof_sha,
        "build_sha256": build.get("build_sha256"),
        "issuer_public_key_sha256": transition_state["issuer_public_key_sha256"],
        "readiness_membership_digest": public_manifest["membership_digest"],
        "pair_id": manifest.get("pair_id"),
        "slot_id": manifest.get("slot_id"),
        "pair_seed": manifest.get("pair_seed"),
        "arm": transition_state["arm"],
        "epoch1_structure_sha256": _hash(epoch_structural_projection(bundles[0])),
        "source_inventory_sha256": _hash(source_inventory),
        "authoritative_commit_identity_sha256": _hash(commit_identity),
        "epoch_identity_sha256": _hash(epoch_identity),
        "ranking_identity_sha256": _hash(ranking_identity),
        "fault_receipt_sha256": fault_join["fault_receipt_sha256"],
        "authoritative_commit_count": len(commits),
        "scientific_measurements": measurements,
        "child": {
            "path": str(root),
            "run_id": root.name,
            "evidence_tree_sha256": seal.tree_sha256,
            "evidence_seal_sha256": seal.seal_sha256,
        },
    }


def validate_sealed_arm(
    run_directory: Path,
    *,
    trusted_provenance: object,
    readiness_verifier_path: Path | None = None,
) -> dict[str, object]:
    """Reconstruct one sealed arm from raw sources before joining fault truth."""

    if not isinstance(trusted_provenance, Mapping):
        _error("trusted provenance must be an exact object")
    root = Path(run_directory)
    if root.is_symlink() or not root.is_dir():
        _error("sealed arm directory is absent")
    try:
        seal = verify_evidence_seal(root)
    except (EvidenceSealError, OSError) as exc:
        raise FocusedCrashPairValidationError("arm evidence seal rejected") from exc
    contract = validation_contract_from_profile(root)
    if contract.get("profile_id") in _FCRASH_H_V13_PROFILE_IDS:
        return _validate_sealed_v13_arm(
            root,
            seal=seal,
            contract=contract,
            trusted_provenance=trusted_provenance,
            readiness_verifier_path=readiness_verifier_path,
        )
    profile_sha = str(contract["profile_sha256"])
    proof_sha = str(contract["topology_proof_sha256"])
    manifest, pair_receipt, build = _validate_receipts(root, profile_sha, proof_sha)
    expected_provenance = {
        "schema_version": 1,
        "revision": build.get("revision"),
        "build_sha256": build.get("build_sha256"),
        "profile_sha256": profile_sha,
        "topology_proof_sha256": proof_sha,
        "evidence_tree_sha256": seal.tree_sha256,
        "evidence_seal_sha256": seal.seal_sha256,
    }
    if dict(trusted_provenance) != expected_provenance:
        _error("trusted provenance is not exact or child-seal-bound")
    _validate_runtime_configuration(root, contract)
    events, source_inventory = _validate_sources(
        root,
        require_controller_failure=_is_v4_contract(contract),
    )

    issuer_path = root / "raw" / "issuer-public-key.txt"
    if issuer_path.is_symlink() or not issuer_path.is_file():
        _error("issuer public key is absent")
    issuer = issuer_path.read_text(encoding="utf-8").strip()
    if len(issuer) not in {66, 130}:
        _error("issuer public key encoding is malformed")
    try:
        issuer_bytes = bytes.fromhex(issuer)
    except ValueError as exc:
        raise FocusedCrashPairValidationError(
            "issuer public key encoding is malformed"
        ) from exc
    epoch1_wire, epoch1 = _decode_bundle(
        root / "raw" / "epoch1.bundle", issuer, 1, contract
    )
    if epoch1.previous_epoch_digest != contract["epoch_zero_digest"]:
        _error("Epoch 1 predecessor identity drifted")
    epoch2_path = root / "raw" / "epoch2.bundle"
    epoch2_wire: bytes | None = None
    epoch2: Any | None = None
    if epoch2_path.exists():
        epoch2_wire, epoch2 = _decode_bundle(epoch2_path, issuer, 2, contract)
        if epoch2.previous_epoch_digest != epoch1.epoch_digest:
            _error("Epoch 2 is not chained to Epoch 1")

    commands1, activations1 = _validate_transition(events, epoch1, contract)
    (
        containment_ranked_ids,
        containment_observation_ids,
        containment_timeout_targets,
        epoch1_snapshot_id,
        epoch1_cutoff,
        epoch1_audit_ns,
        _epoch1_roots,
    ) = _ranking(events, epoch1, contract, predecessor_epoch=0)
    if epoch1_snapshot_id is not None and (
        epoch1.evidence_snapshot_id != epoch1_snapshot_id
        or epoch1.evidence_cutoff != epoch1_cutoff
    ):
        _error("Epoch 1 bundle is not bound to its native snapshot audit")
    epoch1_roots = (
        _containment_roots(containment_ranked_ids, contract)
        if _mapping(contract["profile"], "focused profile").get("profile_id")
        in _FCRASH_H_V3_PROFILE_IDS | _FAULT_WINDOW_PROFILE_IDS
        else tuple(range(int(contract["quorum"])))
    )
    _validate_trees(
        epoch1,
        epoch1_roots,
        "Epoch 1",
        contract,
        containment_timeout_targets if _is_v9_contract(contract) else None,
    )
    if epoch2 is None:
        if any(
            event["event_type"] in {"epoch.command_committed", "epoch.activated"}
            and event["payload"].get(
                "successor_epoch_number", event["payload"].get("epoch_number")
            )
            == 2
            for event in events
        ):
            _error("control arm contains an Epoch 2 transition")
        commands2: list[Mapping[str, Any]] = []
        activations2: list[Mapping[str, Any]] = []
    else:
        (
            ranked_ids,
            observation_ids,
            timeout_targets,
            epoch2_snapshot_id,
            epoch2_cutoff,
            epoch2_audit_ns,
            epoch2_roots,
        ) = _ranking(
            events,
            epoch1,
            contract,
            predecessor_epoch=1,
            inherited_wait_exempt=(
                containment_timeout_targets if _is_v9_contract(contract) else None
            ),
        )
        if epoch2_snapshot_id is None or (
            epoch2.evidence_snapshot_id != epoch2_snapshot_id
            or epoch2.evidence_cutoff != epoch2_cutoff
        ):
            _error("Epoch 2 bundle is not bound to its native snapshot audit")
        if _is_v9_contract(contract):
            if not set(timeout_targets).issubset(containment_timeout_targets):
                _error("optimization introduces a new nonresponsive cohort member")
        elif timeout_targets != containment_timeout_targets:
            _error("containment and optimization evidence disagree on fault targets")
        _validate_trees(
            epoch2,
            epoch2_roots,
            "Epoch 2",
            contract,
            containment_timeout_targets if _is_v9_contract(contract) else None,
        )
        commands2, activations2 = _validate_transition(events, epoch2, contract)
        if (
            epoch2_audit_ns is None
            or min(
                _integer(event["source_monotonic_ns"], "Epoch 2 command timestamp")
                for event in commands2
            )
            <= epoch2_audit_ns
        ):
            _error("Epoch 2 command does not follow its native snapshot audit")
        if epoch2_audit_ns is None:
            _error("Epoch 2 native snapshot audit is absent")
        authoritative_source = str(contract["authoritative_source_id"])
        activation1_ns = max(
            _integer(event["source_monotonic_ns"], "Epoch 1 activation timestamp")
            for event in activations1
        )
        predecessor_commits = [
            event
            for event in events
            if event["event_type"] == "block.committed"
            and event["source_kind"] == "replica"
            and event["source_id"] == authoritative_source
            and _integer(event["source_monotonic_ns"], "predecessor commit timestamp")
            > activation1_ns
            and _integer(event["source_monotonic_ns"], "predecessor commit timestamp")
            < epoch2_audit_ns
            and _mapping(
                _mapping(event["payload"], "predecessor commit").get("decision_proof"),
                "predecessor decision proof",
            ).get("epoch_number")
            == 1
        ]
        _predecessor_commit, predecessor_observations = _select_latest_common_commit(
            predecessor_commits,
            [
                event
                for event in events
                if event["event_type"] == "block.commit_observed"
                and _integer(
                    event["source_monotonic_ns"], "common commit observation timestamp"
                )
                < epoch2_audit_ns
            ],
            contract,
        )
        observation_times = [
            _integer(
                event["source_monotonic_ns"], "common commit observation timestamp"
            )
            for event in predecessor_observations
        ]
        if (
            min(observation_times) <= activation1_ns
            or max(observation_times) >= epoch2_audit_ns
            or min(
                _integer(event["source_monotonic_ns"], "Epoch 2 command timestamp")
                for event in commands2
            )
            <= epoch2_audit_ns
        ):
            _error("Epoch 2 command precedes the fresh common-commit window")
    if _is_v4_contract(contract):
        _validate_v4_pass_terminals(
            events,
            contract=contract,
            epoch1=epoch1,
            epoch2=epoch2,
            commands1=commands1,
            commands2=commands2,
            activations1=activations1,
            activations2=activations2,
        )
    if epoch2 is None:
        ranked_ids = containment_ranked_ids
        observation_ids = containment_observation_ids
        timeout_targets = containment_timeout_targets

    commits, measurements = _commit_reconstruction(
        root, events, epoch1, epoch2, contract
    )
    if not _is_v9_contract(contract) and timeout_targets != tuple(contract["targets"]):
        _error("source-blind nonresponse reconstruction drifted")
    replica_sources = {
        int(str(event["source_id"]).removeprefix("replica-"))
        for event in events
        if event["source_kind"] == "replica"
    }
    members = set(contract["members"])
    survivors = set(contract["survivors"])
    if not survivors.issubset(replica_sources) or not replica_sources.issubset(members):
        _error("raw replica sources differ from the reconstructed membership")

    fault_receipt = _read_json(root / "raw" / "fault-receipt.json", "fault receipt")
    if (
        set(fault_receipt)
        != {
            "schema_version",
            "fault_plan",
            "process_records",
            "sigkill_outcomes",
            "fault_journal",
        }
        or fault_receipt.get("schema_version") != 1
    ):
        _error("fault receipt schema drifted")
    confirmations = {
        _integer(outcome.get("replica_id"), "fault outcome replica"): _integer(
            outcome.get("confirmed_monotonic_ns"), "fault confirmation"
        )
        for outcome in _sequence(fault_receipt["sigkill_outcomes"], "SIGKILL outcomes")
    }
    for event in events:
        if event["source_kind"] != "replica":
            continue
        replica = int(str(event["source_id"]).removeprefix("replica-"))
        if (
            replica in confirmations
            and int(event["source_monotonic_ns"]) > confirmations[replica]
        ):
            _error("crashed replica emitted an event after confirmed SIGKILL")
    _validate_atomic_fault_receipt(contract, fault_receipt)
    receipt_targets = {
        _integer(outcome.get("replica_id"), "fault outcome replica")
        for outcome in _sequence(fault_receipt["sigkill_outcomes"], "SIGKILL outcomes")
    }
    if _is_v9_contract(contract) and not receipt_targets.issubset(
        containment_timeout_targets
    ):
        _error("v9 crash receipt targets are outside the guarded cohort")
    if "reporter_coverage_plan" in contract:
        fault_ns = max(confirmations.values())
        prefault_ns = min(
            _integer(
                _mapping(outcome, "SIGKILL outcome").get("requested_monotonic_ns"),
                "fault request",
            )
            for outcome in _sequence(
                fault_receipt["sigkill_outcomes"], "SIGKILL outcomes"
            )
        )
        _validate_prefault_active_configuration(contract, events, prefault_ns)
        coverage_witness = _fcrash_h_witness_from_events(
            contract,
            events,
            fault_receipt,
            activations1,
            activations2,
            guarded_targets=(
                containment_timeout_targets if _is_v9_contract(contract) else None
            ),
        )
        validate_fcrash_h_evidence(contract, coverage_witness)
        audit_ns = _integer(
            coverage_witness.get("snapshot_audit_monotonic_ns"),
            "snapshot audit timestamp",
        )
        if epoch1_audit_ns != audit_ns:
            _error("Epoch 1 replay audit timestamp drifted")
        if (
            min(
                _integer(event["source_monotonic_ns"], "Epoch 1 command timestamp")
                for event in commands1
            )
            <= audit_ns
        ):
            _error("Epoch 1 command does not follow its native snapshot audit")

    launch = _read_json(root / "runtime" / "launch-arguments.json", "launch arguments")
    observed = _read_json(
        root / "runtime" / "manager-observed-argv.json", "observed manager argv"
    )
    manager_input = _read_json(root / "runtime" / "manager-input.json", "manager input")
    if launch.get("manager_argv") != observed.get("argv"):
        _error("requested and observed manager argv differ")
    _validate_manager_boundary(
        contract,
        _sequence(observed.get("argv"), "observed manager argv"),
        manager_input,
        [event for event in events if event["source_kind"] == "adaptation_manager"],
        transition_count=1 if epoch2 is None else 2,
    )
    _validate_fault_window_arm(
        root,
        contract,
        _sequence(observed.get("argv"), "observed manager argv"),
        fault_receipt,
        confirmations,
        events,
        snapshot_audit_ns=epoch1_audit_ns,
    )

    cleanup = _read_json(root / "cleanup.json", "cleanup result")
    if cleanup.get("complete") is not True:
        _error("arm cleanup is incomplete")
    commit_identity = [
        {
            "source_id": event["source_id"],
            "source_instance": event["source_instance"],
            "source_sequence": event["source_sequence"],
            "payload": event["payload"],
        }
        for event in commits
    ]
    all_commands = [*commands1, *commands2]
    all_activations = [*activations1, *activations2]
    epoch_identity = {
        "epoch1_bundle_sha256": _sha_bytes(epoch1_wire),
        "epoch2_bundle_sha256": (
            None if epoch2_wire is None else _sha_bytes(epoch2_wire)
        ),
        "issuer_public_key_sha256": _sha_bytes(issuer_bytes),
        "commands": [event["payload"] for event in all_commands],
        "activations": [event["payload"] for event in all_activations],
    }
    ranking_identity = {
        "source_epoch_digest": epoch1.epoch_digest,
        "observation_ids": observation_ids,
        "ranked_eligible_replica_ids": ranked_ids,
    }
    return {
        "schema_version": 1,
        "verdict": "PASS",
        "outcome": "PASS",
        "integrity_valid": True,
        "claim_slot": True,
        "source_blind_reconstruction": True,
        "fault_receipt_joined_after_reconstruction": True,
        "reconstructed_from_raw_evidence": True,
        "fault_receipt_joined": True,
        "native_bundles_decoded": True,
        "runtime_graph_validated": True,
        "ranking_reconstructed_from_raw": True,
        "guarded_nonresponsive_replica_ids": list(containment_timeout_targets),
        "dependent_nonresponsive_replica_ids": (
            sorted(set(containment_timeout_targets) - set(receipt_targets))
            if _is_v9_contract(contract)
            else []
        ),
        "exact_crash_equality_claimed": False if _is_v9_contract(contract) else True,
        "epoch2_present": epoch2 is not None,
        "profile_sha256": profile_sha,
        "topology_proof_sha256": proof_sha,
        "build_sha256": build.get("build_sha256"),
        "issuer_public_key_sha256": _sha_bytes(issuer_bytes),
        "pair_id": manifest.get("pair_id"),
        "slot_id": manifest.get("slot_id"),
        "pair_seed": manifest.get("pair_seed"),
        "arm": "adaptive" if epoch2 is not None else "control",
        "epoch1_structure_sha256": _hash(epoch_structural_projection(epoch1)),
        "source_inventory_sha256": _hash(source_inventory),
        "authoritative_commit_identity_sha256": _hash(commit_identity),
        "epoch_identity_sha256": _hash(epoch_identity),
        "ranking_identity_sha256": _hash(ranking_identity),
        "fault_receipt_sha256": _hash(fault_receipt),
        "authoritative_commit_count": len(commits),
        "scientific_measurements": measurements,
        "child": {
            "path": str(root),
            "run_id": root.name,
            "evidence_tree_sha256": seal.tree_sha256,
            "evidence_seal_sha256": seal.seal_sha256,
        },
    }


def validate_sealed_pair(
    pair_directory: Path,
    *,
    trusted_provenance: object,
    readiness_verifier_path: Path | None = None,
) -> dict[str, object]:
    root = Path(pair_directory)
    if not isinstance(trusted_provenance, Mapping):
        _error("aggregate trusted provenance must be an exact object")
    try:
        verify_evidence_seal(root)
    except (EvidenceSealError, OSError) as exc:
        raise FocusedCrashPairValidationError("pair evidence seal rejected") from exc
    receipt = _read_json(root / "pair-receipt.json", "sealed pair receipt")
    children = _sequence(receipt.get("children"), "sealed pair children")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("automatic_retries") != 0
        or receipt.get("replacement_policy") != "none"
        or len(children) != 2
    ):
        _error("sealed pair receipt schema or no-retry contract drifted")
    results: dict[str, Mapping[str, Any]] = {}
    for child in children:
        entry = _mapping(child, "pair child")
        relative = entry.get("path")
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or entry.get("arm") not in {"control", "adaptive"}
        ):
            _error("pair child path or arm is invalid")
        arm = str(entry["arm"])
        result = validate_sealed_arm(
            root / relative,
            trusted_provenance=_aggregate_child_provenance(
                trusted_provenance, root / relative, expected_arm=arm
            ),
            readiness_verifier_path=readiness_verifier_path,
        )
        if (
            result["arm"] != arm
            or result["pair_id"] != receipt.get("pair_id")
            or entry.get("tree_sha256") != result["child"]["evidence_tree_sha256"]
            or entry.get("seal_sha256") != result["child"]["evidence_seal_sha256"]
            or arm in results
        ):
            _error("pair child identity or seal drifted")
        results[arm] = result
    if set(results) != {"control", "adaptive"}:
        _error("sealed pair does not contain one child per arm")
    control = results["control"]
    adaptive = results["adaptive"]
    if (
        control["build_sha256"] != adaptive["build_sha256"]
        or control["pair_seed"] != adaptive["pair_seed"]
        or control["profile_sha256"] != adaptive["profile_sha256"]
        or control["issuer_public_key_sha256"] != adaptive["issuer_public_key_sha256"]
        or control["epoch1_structure_sha256"] != adaptive["epoch1_structure_sha256"]
    ):
        _error("sealed pair matched inputs or Epoch 1 structure drifted")
    control_tps = int(
        control["scientific_measurements"]["late_window_throughput_milli_tps"]
    )
    adaptive_tps = int(
        adaptive["scientific_measurements"]["late_window_throughput_milli_tps"]
    )
    outcome = (
        "FAVORABLE"
        if adaptive_tps > control_tps
        else "UNFAVORABLE" if adaptive_tps < control_tps else "NEUTRAL"
    )
    return {
        "schema_version": 1,
        "verdict": "PASS",
        "pair_id": receipt.get("pair_id"),
        "scientific_outcome": outcome,
        "retained": True,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "children": [dict(results["control"]), dict(results["adaptive"])],
    }


def validate_sealed_campaign(
    campaign_directory: Path,
    *,
    trusted_provenance: object,
    readiness_verifier_path: Path | None = None,
) -> dict[str, object]:
    root = Path(campaign_directory)
    if not isinstance(trusted_provenance, Mapping):
        _error("aggregate trusted provenance must be an exact object")
    try:
        verify_evidence_seal(root)
    except (EvidenceSealError, OSError) as exc:
        raise FocusedCrashPairValidationError(
            "campaign evidence seal rejected"
        ) from exc
    plan = _read_json(root / "plan.json", "campaign plan")
    ledger_path = root / "campaign-ledger.jsonl"
    if ledger_path.is_symlink() or not ledger_path.is_file():
        _error("campaign ledger is absent")
    try:
        ledger = [json.loads(line) for line in ledger_path.read_text().splitlines()]
        campaign_contracts.validate_campaign_ledger(plan, ledger)
    except (json.JSONDecodeError, campaign_contracts.N31CrashPairCampaignError) as exc:
        raise FocusedCrashPairValidationError("campaign ledger rejected") from exc
    children: list[dict[str, object]] = []
    for slot, record in zip(plan["slots"], ledger, strict=True):
        directory = root / "children" / str(slot["slot_id"])
        result = validate_sealed_arm(
            directory,
            trusted_provenance=_aggregate_child_provenance(
                trusted_provenance, directory
            ),
            readiness_verifier_path=readiness_verifier_path,
        )
        children.append(
            {
                "slot_id": slot["slot_id"],
                "pair_id": slot["pair_id"],
                "arm": slot["arm"],
                "child_tree_sha256": record["child_tree_sha256"],
                "child_seal_sha256": record["child_seal_sha256"],
                "sealed_child_directory": directory,
                "source_inventory_sha256": result["source_inventory_sha256"],
                "authoritative_commit_identity_sha256": result[
                    "authoritative_commit_identity_sha256"
                ],
                "epoch_identity_sha256": result["epoch_identity_sha256"],
                "ranking_identity_sha256": result["ranking_identity_sha256"],
            }
        )
    try:

        def validate_isolated_child(
            directory: Path, *, trusted_provenance: object
        ) -> Mapping[str, Any]:
            return validate_sealed_arm(
                directory,
                trusted_provenance=_aggregate_child_provenance(
                    trusted_provenance, directory
                ),
                readiness_verifier_path=readiness_verifier_path,
            )

        source_blind = campaign_contracts.validate_campaign_source_blind(
            plan,
            children,
            ledger_records=ledger,
            validate_child=validate_isolated_child,
            trusted_provenance=trusted_provenance,
        )
    except campaign_contracts.N31CrashPairCampaignError as exc:
        raise FocusedCrashPairValidationError("source-blind campaign rejected") from exc
    pairs = [
        {
            **dict(pair),
            "retained": True,
        }
        for pair in source_blind["pair_verdicts"]
    ]
    result: dict[str, object] = {
        "schema_version": 1,
        "verdict": (
            "PASS" if source_blind["campaign_acceptance"] == "ACCEPTED" else "FAIL"
        ),
        "terminal_slot_count": len(ledger),
        "pair_count": len(pairs),
        "automatic_retries": 0,
        "replacement_policy": "none",
        "pairs": pairs,
        "figure_eligible": source_blind["figure_eligible"],
        "ledger_head_sha256": source_blind["ledger_head_sha256"],
    }
    if "scientific_support" in source_blind:
        result["scientific_support"] = source_blind["scientific_support"]
        result["claim_eligible"] = source_blind["claim_eligible"]
    return result


__all__ = [
    "FocusedCrashPairValidationError",
    "epoch_structural_projection",
    "reconstruct_focused_ranking",
    "validate_sealed_arm",
    "validate_sealed_campaign",
    "validate_sealed_pair",
]
def _reconstruct_v13_activation_readiness(*, profile, events, forbidden_context=None):
    """Source-blind v13 readiness reconstruction; receipt data is forbidden."""
    if forbidden_context:
        # Deliberately do not inspect values: callers may supply poison objects.
        tuple(forbidden_context.keys())
    contract = profile["transitions"]["activation_readiness_contract"]
    required = int(contract["required_reporter_count"])
    quorum = int(contract["certificate_quorum"])
    rows = []
    for epoch in (1, 2):
        certificates = [e for e in events
            if e.get("event_type") == "manager.activation_readiness_certificate_assembled"
            and e.get("payload", {}).get("identity", {}).get("successor_configuration", {}).get("epoch_number") == epoch]
        if len(certificates) != 1:
            raise FocusedCrashPairValidationError("v13 requires one certificate per successor")
        certificate = certificates[0]["payload"]
        identity = certificate.get("identity")
        signers = sorted(certificate.get("signer_replica_ids", []))
        if len(signers) != required or len(set(signers)) != required or certificate.get("certificate_quorum") != quorum or certificate.get("required_reporter_count") != required:
            raise FocusedCrashPairValidationError("v13 certificate cardinality drifted")
        accepted = [e.get("payload", {}) for e in events
            if e.get("event_type") == "manager.activation_readiness_accepted"
            and e.get("payload", {}).get("identity") == identity]
        reporters = sorted({p.get("signer_replica_id") for p in accepted
            if p.get("authenticated_replica_id") == p.get("signer_replica_id")})
        activated = sorted({e.get("source_replica_id") for e in events
            if e.get("event_type") == "epoch.activated" and e.get("payload", {}).get("identity") == identity
            and e.get("payload", {}).get("certificate_digest") == certificate.get("certificate_digest")})
        if reporters != signers or activated != signers:
            raise FocusedCrashPairValidationError("v13 readiness signer/apply set drifted")
        rows.append({"successor_epoch_number": epoch, "identity": identity,
                     "certificate_digest": certificate.get("certificate_digest"),
                     "observed_reporter_ids": reporters, "activated_replica_ids": activated})
    canonical = {"profile_id": profile["profile_id"], "transitions": rows}
    if rows[0]["observed_reporter_ids"] != rows[1]["observed_reporter_ids"]:
        raise FocusedCrashPairValidationError("v13 E1/E2 reporter sets differ")
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"observed_reporter_ids": rows[0]["observed_reporter_ids"],
            "transitions": rows, "reconstruction_digest": digest}


def _join_v13_fault_receipt(*, reconstruction, fault_receipt):
    expected = sorted(fault_receipt["survivor_replica_ids"])
    observed = reconstruction["observed_reporter_ids"]
    transitions = reconstruction.get("transitions", ())
    if len(transitions) != 2 or any(
        row.get("observed_reporter_ids") != expected for row in transitions
    ):
        raise FocusedCrashPairValidationError("v13 fault receipt does not match both readiness cycles")
    return {"expected_survivor_ids": expected, "observed_survivor_ids": observed,
            "exact_survivor_set_match": expected == observed,
            "source_blind_reconstruction_digest": reconstruction["reconstruction_digest"]}


def _v13_certified_activation_contract(profile):
    """Validate the public frozen v13 policy without sealed-evidence claims."""
    protocol = profile["protocol"]
    contract = profile["transitions"]["activation_readiness_contract"]
    expected = {
        "schema_version": 1,
        "domain": "kauri-focused-v13-certified-activation-v1",
        "certificate_quorum": protocol["Q"],
        "required_reporter_count": profile["transitions"]["survivor_barrier_count"],
        "epoch1_common_commit_anchor_deadline_seconds": 5,
        "containment_stabilization_seconds": 30,
        "containment_measurement_seconds": 30,
        "minimum_predecessor_residency_ms": 65000,
        "optimization_activation_budget_seconds": 90,
        "deadline_semantics": "half_open_monotonic_v1",
        "readiness_ledger_schema": "kauri-focused-readiness-ledger-v1",
    }
    if (profile.get("profile_id") not in _FCRASH_H_V13_PROFILE_IDS or
        protocol.get("epoch_protocol_mode") != "adaptive_v3" or
        profile["transitions"].get("adaptive") != ["epoch1", "epoch2"] or
        contract != expected or expected["certificate_quorum"] > expected["required_reporter_count"]):
        raise FocusedCrashPairValidationError("v13 certified activation contract drifted")
    return dict(contract)
