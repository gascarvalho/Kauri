"""Prospective W18 convergence amendment; launch requires author approval."""
import json


def expected_profile():
    return {"kind": "kauri-w18-cluster-aggregation-2s-leader-20s-convergence-90s-v4",
            "schema_version": 4, "aggregation_per_remaining_level_ms": 2000,
            "leader_progress_timeout_ms": 20000, "leader_activation_grace_ms": 1000,
            "convergence_deadline_seconds": 90}


def aggregation_seconds(profile=None):
    if profile is None:
        return 0.5
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if canonical(profile) != canonical(expected_profile()):
        raise ValueError("cluster aggregation timing differs from the approved exact profile")
    return 2.0


def leader_progress_seconds(profile=None):
    # Validate the same exact profile before selecting either native timer.
    aggregation_seconds(profile)
    return 5.0 if profile is None else 20.0


def convergence_seconds(profile=None):
    aggregation_seconds(profile)
    return 30 if profile is None else 90
