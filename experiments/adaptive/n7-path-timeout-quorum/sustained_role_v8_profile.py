"""Frozen, no-launch timing and evidence contract for the prospective W19 v8 pilot."""
from __future__ import annotations

import json
from typing import Any, Mapping


PROFILE_ID = "n7-sustained-role-proposal-boundary-v8"
CONVERGENCE_EVENT_TYPE = "adaptive_v2.convergence_started"
REPLICA_IDS = tuple(range(7))
QUORUM = 5
ANCHOR_TO_HORIZON_NS = 72_000_000_000
ANCHOR_TO_CONVERGENCE_NS = 20_000_000_000
ANCHOR_TO_ACTIVATION_NS = 32_000_000_000
SCHEDULED_START_TO_HORIZON_NS = 82_000_000_000
MEASUREMENT_WINDOW_NS = 40_000_000_000
NATIVE_MODE = "role_scoped_persistent_selected_omission_v1"
ACTOR_ID = 1
FIRST_OMISSION_TREE = 4
MAX_OMISSIONS_PER_PROPOSAL = 1
CONTEXT_LIMIT = 100_000

# This is intentionally complete enough for the inherited N=7 writer, but it
# remains no-launch input data.  The v8 identity is preserved: callers must
# not translate it to the legacy v4 profile merely to use common writer code.
CONSENSUS_PROFILE: dict[str, Any] = {
    "schema_version": 1, "profile_id": PROFILE_ID, "profile_version": 8,
    "frozen": True, "replica_ids": list(REPLICA_IDS), "fault_threshold": 2,
    "quorum": QUORUM, "authoritative_observer": 2, "crash_targets": [],
    "epoch0_roots": list(REPLICA_IDS), "fanout": 2, "pipeline_depth": 2,
    "block_size": 1, "tree_switch_period_blocks": 2,
    "aggregation_timeout_s": 0.5, "leader_progress_timeout_s": 5.0,
    "leader_activation_grace_s": 1.0, "activation_delay_blocks": 5,
    "snapshot_seed": 41719, "required_nonresponsive": 1,
    "transition": {
        "policy_intent": "fault_containment",
        "evidence_window_rule": "fresh_exact_predecessor_after_common_commit",
        "transition_artifact_id": "e0-to-e1-containment",
        "bundle_path": "transitions/e0-to-e1-containment/successor.bundle",
        "evidence_snapshot_path": "transitions/e0-to-e1-containment/evidence-snapshot.json",
        "predecessor_epoch_number": 0, "successor_epoch_number": 1,
        "minimum_predecessor_residency_ms": 0,
        "containment_baseline_root_source": "live_predecessor_roots",
        "policy_parameters": {},
    },
    "throughput_windows": [
        {"phase": "baseline", "epoch_number": 0, "bucket_count": 1},
        {"phase": "degraded", "epoch_number": 0, "bucket_count": 1},
        {"phase": "containment", "epoch_number": 1, "bucket_count": 1},
    ],
}

MATERIALIZATION_MAP: dict[str, Any] = {
    "writer": "experiments/adaptive/n7-crash-recovery/run.py:write_runtime_inputs",
    "profile_identity": PROFILE_ID,
    "transition_requests": "canonical_exact_CONSENSUS_PROFILE.transition",
    "throughput_windows": "baseline_e0,degraded_e0,containment_e1",
    "no_v4_translation": True,
}

CONVERGENCE_PAYLOAD_FIELDS = frozenset({
    "cycle_ordinal", "predecessor_epoch_number", "predecessor_epoch_digest",
    "successor_epoch_number", "successor_epoch_digest", "command_payload_digest",
    "evidence_snapshot_id", "baseline_evidence_cutoff", "evidence_cutoff",
})


class V8ProfileError(ValueError):
    """The frozen v8 timing profile is internally inconsistent."""


def _canonical_profile(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise V8ProfileError("profile is not canonical JSON data") from exc


def deadlines(anchor_monotonic_ns: int) -> dict[str, int]:
    if type(anchor_monotonic_ns) is not int or anchor_monotonic_ns < 0:
        raise V8ProfileError("anchor must be a nonnegative CLOCK_MONOTONIC_RAW timestamp")
    return {
        "convergence_deadline_ns": anchor_monotonic_ns + ANCHOR_TO_CONVERGENCE_NS,
        "activation_deadline_ns": anchor_monotonic_ns + ANCHOR_TO_ACTIVATION_NS,
        "horizon_ns": anchor_monotonic_ns + ANCHOR_TO_HORIZON_NS,
        "measurement_start_ns": anchor_monotonic_ns + ANCHOR_TO_ACTIVATION_NS,
        "measurement_end_ns": anchor_monotonic_ns + ANCHOR_TO_HORIZON_NS,
    }


def materialization_profile() -> dict[str, Any]:
    """Return the exact v8 profile accepted by the common N=7 writer.

    The copied mapping is an input descriptor only; it has no process or
    authorization capability.  ``transition_requests`` is the writer's
    established plural field for the one frozen containment transition.
    """
    value = json.loads(json.dumps(CONSENSUS_PROFILE, sort_keys=True))
    value["transition_requests"] = [value.pop("transition")]
    return value


def native_actor_overlay(*, scheduled_start_monotonic_ns: int,
                         scheduled_end_monotonic_ns: int) -> tuple[str, ...]:
    """Freeze v8's actor-1 physical omission window without inventing A.

    The native event's decision time, not this scheduled prediction, defines
    A.  The scheduled interval nevertheless reserves at least 82 seconds for
    the A+32..A+72 evidence horizon.
    """
    if (type(scheduled_start_monotonic_ns) is not int or type(scheduled_end_monotonic_ns) is not int or
            scheduled_start_monotonic_ns <= 0 or
            scheduled_end_monotonic_ns - scheduled_start_monotonic_ns < SCHEDULED_START_TO_HORIZON_NS):
        raise V8ProfileError("v8 scheduled fault window must cover at least 82 seconds")
    return (
        "--experiment-byzantine-mode", NATIVE_MODE,
        "--experiment-byzantine-window", PROFILE_ID,
        "--experiment-rotating-omission-actors", str(ACTOR_ID),
        "--experiment-byzantine-window-start-monotonic-ns", str(scheduled_start_monotonic_ns),
        "--experiment-byzantine-window-end-monotonic-ns", str(scheduled_end_monotonic_ns),
        "--experiment-byzantine-max-omissions-per-proposal", str(MAX_OMISSIONS_PER_PROPOSAL),
        "--experiment-rotating-omission-context-limit", str(CONTEXT_LIMIT),
        "--experiment-byzantine-first-omission-tree", str(FIRST_OMISSION_TREE),
    )


def validate_profile(profile: Mapping[str, object]) -> None:
    expected = {
        "profile_id": PROFILE_ID, "quorum": QUORUM,
        "anchor_to_convergence_ns": ANCHOR_TO_CONVERGENCE_NS,
        "anchor_to_activation_ns": ANCHOR_TO_ACTIVATION_NS,
        "anchor_to_horizon_ns": ANCHOR_TO_HORIZON_NS,
        "scheduled_start_to_horizon_ns": SCHEDULED_START_TO_HORIZON_NS,
        "measurement_window_ns": MEASUREMENT_WINDOW_NS,
    }
    if _canonical_profile(profile) != _canonical_profile(expected):
        raise V8ProfileError("profile differs from the frozen v8 timing contract")
    if ANCHOR_TO_HORIZON_NS - ANCHOR_TO_ACTIVATION_NS != MEASUREMENT_WINDOW_NS:
        raise V8ProfileError("v8 does not preserve its 40-second equal metric window")


def validate_materialization_profile(profile: Mapping[str, object]) -> None:
    if _canonical_profile(profile) != _canonical_profile(materialization_profile()):
        raise V8ProfileError("materialization profile differs from frozen v8 consensus inputs")


FROZEN_PROFILE = {
    "profile_id": PROFILE_ID, "quorum": QUORUM,
    "anchor_to_convergence_ns": ANCHOR_TO_CONVERGENCE_NS,
    "anchor_to_activation_ns": ANCHOR_TO_ACTIVATION_NS,
    "anchor_to_horizon_ns": ANCHOR_TO_HORIZON_NS,
    "scheduled_start_to_horizon_ns": SCHEDULED_START_TO_HORIZON_NS,
    "measurement_window_ns": MEASUREMENT_WINDOW_NS,
}
