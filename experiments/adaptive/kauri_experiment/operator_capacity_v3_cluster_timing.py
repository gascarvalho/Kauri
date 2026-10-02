"""Author-approved W18 cluster timing; the legacy local default stays separate."""
import json


def expected_profile():
    return {"kind": "kauri-w18-cluster-aggregation-1s-leader-5s-v1",
            "schema_version": 1, "aggregation_per_remaining_level_ms": 1000,
            "leader_progress_timeout_ms": 5000, "leader_activation_grace_ms": 1000}


def aggregation_seconds(profile=None):
    if profile is None:
        return 0.5
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if canonical(profile) != canonical(expected_profile()):
        raise ValueError("cluster aggregation timing differs from the approved exact profile")
    return 1.0
