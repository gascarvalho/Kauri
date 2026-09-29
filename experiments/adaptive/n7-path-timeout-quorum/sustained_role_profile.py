"""No-launch profile for the prospective N=7 sustained role-scoped study.

This module is intentionally separate from the frozen v4 aggregate-relay
omission path.  It only prepares the native scheduled-fault argv that a
future, separately authorized runner may bind to its measured raw-clock
window.  It never starts a process or asserts that an Epoch-1 successor was
activated.
"""
from __future__ import annotations

from typing import Any


PROFILE_ID = "n7-role-scoped-persistent-selected-omission-v1"
NATIVE_MODE = "role_scoped_persistent_selected_omission_v1"
ACTOR_ID = 1
FAULT_THRESHOLD = 2
QUORUM = 5
REPLICA_IDS = tuple(range(7))
COMMON_HORIZON_NS = 60_000_000_000
MINIMUM_POST_START_ANCHOR_SLACK_NS = 10_000_000_000
CONTEXT_LIMIT = 100_000
MAX_OMISSIONS_PER_PROPOSAL = 1


class SustainedRoleProfileError(ValueError):
    """The scheduled one-actor profile is not bounded exactly."""


def _positive_uint64(value: object, label: str) -> int:
    if type(value) is not int or value <= 0 or value > (1 << 64) - 1:
        raise SustainedRoleProfileError(f"{label} is not a positive uint64")
    return value


def argv_overlay(*, window_start_monotonic_ns: int,
                 window_end_monotonic_ns: int) -> tuple[str, ...]:
    """Return the exact native scheduled-fault options for actor 1.

    The mode is deliberately configuration-independent: Epoch-1 leaf
    placement must not turn the fault off merely by changing epoch identity.
    A later runner must obtain both timestamps from the same raw-clock domain
    that it preserves in the evidence bundle.
    """
    start = _positive_uint64(window_start_monotonic_ns, "fault window start")
    end = _positive_uint64(window_end_monotonic_ns, "fault window end")
    minimum_duration = COMMON_HORIZON_NS + MINIMUM_POST_START_ANCHOR_SLACK_NS
    if end <= start or end - start < minimum_duration:
        raise SustainedRoleProfileError(
            "fault window must cover a positive post-start anchor allowance plus the 60-second common horizon"
        )
    return (
        "--experiment-byzantine-mode", NATIVE_MODE,
        "--experiment-byzantine-window", PROFILE_ID,
        "--experiment-rotating-omission-actors", str(ACTOR_ID),
        "--experiment-byzantine-window-start-monotonic-ns", str(start),
        "--experiment-byzantine-window-end-monotonic-ns", str(end),
        "--experiment-byzantine-max-omissions-per-proposal",
        str(MAX_OMISSIONS_PER_PROPOSAL),
        "--experiment-rotating-omission-context-limit", str(CONTEXT_LIMIT),
    )


def preflight(*, window_start_monotonic_ns: int,
              window_end_monotonic_ns: int) -> dict[str, Any]:
    """Describe one bounded prospective input without authorizing execution."""
    overlay = argv_overlay(
        window_start_monotonic_ns=window_start_monotonic_ns,
        window_end_monotonic_ns=window_end_monotonic_ns,
    )
    return {
        "schema_version": 1,
        "profile_id": PROFILE_ID,
        "status": "PREFLIGHT_ONLY_NO_EXECUTION",
        "claim_boundary": (
            "No process was launched; Epoch-1 all-leaf placement, continued "
            "post-E1 omission, commit metrics, raw replay, and matched-arm "
            "comparison remain unverified."
        ),
        "protocol": {
            "replica_ids": list(REPLICA_IDS),
            "fault_threshold": FAULT_THRESHOLD,
            "quorum": QUORUM,
        },
        "fault": {
            "native_mode": NATIVE_MODE,
            "actor_id": ACTOR_ID,
            "hard_actor_count": 1,
            "responsive_degraded_actor_count": 0,
            "responsive_omission_period": 0,
            "max_omissions_per_proposal": MAX_OMISSIONS_PER_PROPOSAL,
            "context_limit": CONTEXT_LIMIT,
            "window_start_monotonic_ns": window_start_monotonic_ns,
            "window_end_monotonic_ns": window_end_monotonic_ns,
            "common_horizon_ns": COMMON_HORIZON_NS,
            "minimum_post_start_anchor_slack_ns": MINIMUM_POST_START_ANCHOR_SLACK_NS,
            "argv_overlay": list(overlay),
        },
    }
