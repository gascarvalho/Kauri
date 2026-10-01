"""Reconstruct W18's all-responsive E0 snapshot from retained ledger events.

The native capacity path retains the baseline in its consumption record rather
than emitting the ordinary guarded-transition snapshot file. This audit binds
that record to the accepted raw prefix using the independent canonical snapshot
encoder and scorer. It imports no native policy decision or runner function.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from . import factorial_validation as independent
from .operator_capacity_native_replay import NativeReplayError, _hex64, _positive


POLICY = {
    "schema_version": 1,
    "policy_version": "adaptive-v2-controller-responsiveness-v1",
    "attempt_window": 32,
    "minimum_attempts": 2,
    "minimum_response_rate_ppm": 750000,
    "maximum_timeout_rate_ppm": 250000,
    "trailing_timeout_streak": 2,
    "latency_percentile_basis_points": 5000,
}
SEED = 0xA2F7
_ENVELOPE = {
    "event_schema_version", "run_id", "source_kind", "source_id",
    "source_instance", "source_sequence", "source_monotonic_ns", "event_type", "payload",
}


class BaselineReplayError(ValueError):
    """The preserved ledger does not prove the authorized all-live baseline."""


def _validate_policy_argv(argv: Sequence[str]) -> None:
    if (not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)) or
            not argv or not all(isinstance(value, str) for value in argv)):
        raise BaselineReplayError("retained manager argv is invalid")
    expected = {
        "--responsiveness-policy-version": str(POLICY["policy_version"]),
        "--reputation-mechanism": "responsiveness",
        **{f"--responsiveness-{key.replace('_', '-')}": str(value)
           for key, value in POLICY.items() if key not in {"schema_version", "policy_version"}},
    }
    for index, item in enumerate(argv):
        key = item.partition("=")[0]
        if key.startswith("--responsiveness-") or key == "--reputation-mechanism":
            if key not in expected or item != key or argv.count(key) != 1:
                raise BaselineReplayError("manager responsiveness policy differs from frozen W18")
            if index + 1 >= len(argv) or argv[index + 1] != expected[key]:
                raise BaselineReplayError("manager responsiveness policy differs from frozen W18")


def replay_operator_capacity_baseline(
    manager_events: Sequence[Mapping[str, Any]], *,
    consumption_record: Mapping[str, Any], manager_argv: Sequence[str],
) -> dict[str, Any]:
    """Verify the exact E0 accepted prefix, snapshot identity and eligibility.

    Rejected observations consume native ingestion sequence numbers as well.
    Accepted sequence gaps are therefore legal; the exact snapshot hash binds
    the complete retained accepted prefix without inventing contiguity.
    """
    try:
        _validate_policy_argv(manager_argv)
        if (not manager_events or len(manager_events) > 1_048_576 or
                not isinstance(consumption_record, Mapping)):
            raise BaselineReplayError("baseline manager stream is empty or exceeds bounds")
        run_id = consumption_record.get("run_id")
        instance = consumption_record.get("source_instance")
        if not isinstance(run_id, str) or not run_id or not isinstance(instance, str) or not instance:
            raise BaselineReplayError("baseline run identity is absent")
        cutoff = _positive(consumption_record.get("baseline_evidence_cutoff"), "baseline cutoff")
        decision = _positive(consumption_record.get("decision_monotonic_raw_ns"), "baseline decision time")
        epoch0 = _hex64(consumption_record.get("epoch0_digest"), "baseline E0 digest")
        expected_id = _hex64(consumption_record.get("baseline_snapshot_id"), "baseline snapshot ID")
        accepted = []
        previous_time = 0
        previous_ingestion = 0
        for index, event in enumerate(manager_events, 1):
            if (not isinstance(event, Mapping) or set(event) != _ENVELOPE or
                    type(event.get("event_schema_version")) is not int or event["event_schema_version"] != 1 or
                    event.get("run_id") != run_id or event.get("source_instance") != instance or
                    event.get("source_kind") != "adaptation_manager" or event.get("source_id") != "adaptive-manager" or
                    type(event.get("source_sequence")) is not int or event["source_sequence"] != index):
                raise BaselineReplayError("baseline manager source identity or sequence drifted")
            when = _positive(event.get("source_monotonic_ns"), "baseline event time")
            if when < previous_time:
                raise BaselineReplayError("baseline manager time regressed")
            previous_time = when
            if event.get("event_type") != "evidence.observation_accepted":
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise BaselineReplayError("accepted baseline payload is invalid")
            ingestion = _positive(payload.get("ingestion_sequence"), "accepted ingestion sequence")
            if ingestion <= previous_ingestion:
                raise BaselineReplayError("accepted ingestion sequence regressed")
            previous_ingestion = ingestion
            if ingestion > cutoff:
                continue
            if when > decision:
                raise BaselineReplayError("accepted baseline evidence follows the signed decision")
            accepted.append(independent._NativeEvent(
                relative_path="raw/manager-events.jsonl", line_number=index,
                source_kind="adaptation_manager", source_id="adaptive-manager", source_instance=instance,
                source_sequence=index, monotonic_ns=when, event_type=event["event_type"], payload=payload,
                line_sha256=hashlib.sha256(json.dumps(dict(event), sort_keys=True,
                    separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()))
        grouped = independent._accepted_evidence(accepted, 31,
            allowed_schema_versions={3}, allow_ingestion_sequence_gaps=True)
        if set(grouped) != {(0, epoch0)}:
            raise BaselineReplayError("baseline accepted prefix is not exact E0")
        prefix = grouped[(0, epoch0)]
        actual_id = independent._snapshot_id(prefix, replica_count=31,
            epoch_number=0, epoch_digest=epoch0, cutoff=cutoff, policy=POLICY, seed=SEED)
        if actual_id != expected_id:
            raise BaselineReplayError("independently reconstructed baseline snapshot ID differs")
        scores = independent._score_snapshot(prefix, 31, POLICY)
        if len(scores) != 31 or not all(score.eligible for score in scores):
            raise BaselineReplayError("baseline does not classify all 31 replicas as responsive")
        return {"schema_version": 1, "kind": "kauri-w18-v3-independent-baseline-replay-v1",
                "baseline_snapshot_id": actual_id, "baseline_evidence_cutoff": cutoff,
                "accepted_prefix_count": len(prefix), "responsive_replica_ids": list(range(31)),
                "policy": dict(POLICY), "seed": SEED}
    except (NativeReplayError, independent._Reject, KeyError, TypeError, UnicodeError) as exc:
        raise BaselineReplayError(str(exc)) from exc
