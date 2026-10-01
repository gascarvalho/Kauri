from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "n7-path-timeout-quorum" / "sustained_role_v8_raw_replay.py"
spec = importlib.util.spec_from_file_location("w19_v8_raw_replay_test", PATH)
assert spec and spec.loader
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)

RUN = "raw-replay-run"
E0 = "a" * 64
E1 = "b" * 64
BLOCK = "c" * 64


def _event(source: str, sequence: int, monotonic_ns: int, event_type: str, payload: object) -> dict[str, object]:
    return {"event_schema_version": 1, "run_id": RUN,
            "source_kind": "adaptation_manager" if source == "adaptive-manager" else "replica",
            "source_id": source, "source_instance": f"{source}-instance",
            "source_sequence": sequence, "source_monotonic_ns": monotonic_ns,
            "event_type": event_type, "payload": payload}


def _jsonl(events: list[dict[str, object]]) -> bytes:
    return b"".join(json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n" for event in events)


def _opportunity(*, epoch: int, tree: int, digest: str, role: str, action: str, timestamp: int) -> dict[str, object]:
    return {"actor": 1, "proposal": {"epoch_number": epoch, "tree_id": tree, "epoch_digest": digest, "block_hash": BLOCK},
            "physical_role": role, "parent_replica": tree, "authenticated_proposal_source_replica": tree,
            "expected_message_type": "aggregate_relay" if role == "internal" else "direct_vote", "cohort": "hard",
            "diagnostic_window": "w19", "window_start_monotonic_ns": timestamp - 1,
            "window_end_monotonic_ns": timestamp + 1, "decision_monotonic_ns": timestamp,
            "contribution_ordinal": 0, "role_contribution_ordinal": 0, "scheduled_action": action,
            "responsive_omission_period": 0, "fault_threshold": 2, "hard_actor_count": 1,
            "responsive_degraded_actor_count": 0, "fault_mode": "role_scoped_persistent_selected_omission_v1",
            "view_generation": 0}


def _marker(payload: dict[str, object]) -> bytes:
    proposal = payload["proposal"]
    fields = {"fault": payload["fault_mode"], "proposal_epoch": proposal["epoch_number"], "proposal_tree": proposal["tree_id"],
              "proposal_epoch_digest": proposal["epoch_digest"], "proposal_block_hash": proposal["block_hash"],
              "window": payload["diagnostic_window"], "window_start_monotonic_ns": payload["window_start_monotonic_ns"],
              "window_end_monotonic_ns": payload["window_end_monotonic_ns"], "actor": payload["actor"], "action": payload["scheduled_action"],
              "monotonic_ns": payload["decision_monotonic_ns"], "cohort": payload["cohort"],
              "hard_actor_count": payload["hard_actor_count"], "responsive_degraded_actor_count": payload["responsive_degraded_actor_count"],
              "fault_threshold": payload["fault_threshold"], "max_omissions_per_proposal": 1,
              "responsive_omission_period": payload["responsive_omission_period"], "contribution_ordinal": payload["contribution_ordinal"],
              "contribution_role": payload["physical_role"], "role_contribution_ordinal": payload["role_contribution_ordinal"],
              "authenticated_proposal_source_replica": payload["authenticated_proposal_source_replica"]}
    return ("KAURI_FAULT " + " ".join(f"{key}={value}" for key, value in fields.items()) + "\n").encode()


def _accepted() -> dict[str, object]:
    anchor = _opportunity(epoch=0, tree=4, digest=E0, role="internal", action="omit_aggregate", timestamp=100)
    # A repeated, later E0 omission is allowed; it must still independently biject its marker.
    repeated = _opportunity(epoch=0, tree=4, digest=E0, role="internal", action="omit_aggregate", timestamp=200)
    late = _opportunity(epoch=1, tree=0, digest=E1, role="leaf", action="omit_direct_vote", timestamp=32_000_000_100)
    replica_raw = [_jsonl([_event(f"replica-{replica}", 1, 0, "replica.heartbeat", {})]) for replica in range(7)]
    replica_raw[1] = _jsonl([_event("replica-1", 1, 100, "fault.contribution_opportunity", anchor),
                             _event("replica-1", 2, 200, "fault.contribution_opportunity", repeated),
                             _event("replica-1", 3, 32_000_000_100, "fault.contribution_opportunity", late)])
    logs = [b"" for _ in range(7)]
    logs[1] = _marker(anchor) + _marker(repeated) + _marker(late)
    return {"run_id": RUN, "manager_raw": _jsonl([_event("adaptive-manager", 1, 0, "manager.heartbeat", {})]),
            "replica_raw": replica_raw, "replica_logs": logs,
            }


def test_replay_accepts_exact_first_anchor_repeated_e0_and_signed_e1_late_leaf() -> None:
    result = subject.replay_fault_evidence(**_accepted())
    assert result["verdict"] == "FAULT_EVIDENCE_COMPONENT_ONLY_NO_CLAIM"
    assert result["anchor_decision_monotonic_ns"] == 100
    assert result["anchor_source_monotonic_ns"] == 100
    assert result["anchor_epoch_digest"] == E0
    assert result["anchor_tree_id"] == 4
    assert len(result["late_e1_leaf_candidates_untrusted_pending_root_join"]) == 1


@pytest.mark.parametrize("mutation, error", [
    ("first_is_leaf", "first actor-1 opportunity"),
    ("marker_identity", "do not biject"),
    ("foreign_marker", "outside replica 1"),
    ("noncontiguous", "sequence is not contiguous"),
    ("late_before_window", "no late E1 leaf"),
])
def test_replay_rejects_raw_marker_and_signed_e1_mutations(mutation: str, error: str) -> None:
    case = _accepted()
    if mutation == "first_is_leaf":
        bad = _opportunity(epoch=0, tree=0, digest=E0, role="leaf", action="omit_direct_vote", timestamp=99)
        old = _decode_events(case["replica_raw"][1])
        for event in old: event["source_sequence"] += 1
        case["replica_raw"][1] = _jsonl([_event("replica-1", 1, 99, "fault.contribution_opportunity", bad), *old])
        case["replica_logs"][1] = _marker(bad) + case["replica_logs"][1]
    elif mutation == "marker_identity":
        case["replica_logs"][1] = case["replica_logs"][1].replace(b"actor=1", b"actor=6", 1)
    elif mutation == "foreign_marker": case["replica_logs"][0] = case["replica_logs"][1].splitlines()[0] + b"\n"
    elif mutation == "noncontiguous":
        events = _decode_events(case["replica_raw"][1]); events[1]["source_sequence"] = 3; case["replica_raw"][1] = _jsonl(events)
    elif mutation == "late_before_window":
        events = _decode_events(case["replica_raw"][1]); late = events[-1]["payload"]
        late["window_start_monotonic_ns"] = 101; late["window_end_monotonic_ns"] = 32_000_000_100; late["decision_monotonic_ns"] = 32_000_000_099
        events[-1]["source_monotonic_ns"] = 32_000_000_099; case["replica_raw"][1] = _jsonl(events)
        original = _decode_events(_accepted()["replica_raw"][1]); case["replica_logs"][1] = _marker(original[0]["payload"]) + _marker(original[1]["payload"]) + _marker(late)
    with pytest.raises(subject.V8RawReplayError, match=error): subject.replay_fault_evidence(**case)


def test_replay_rejects_the_combined_old_false_pass_mutation() -> None:
    case = _accepted()
    bad = _opportunity(epoch=0, tree=0, digest=E0, role="leaf", action="omit_direct_vote", timestamp=99)
    old = _decode_events(case["replica_raw"][1])
    for event in old: event["source_sequence"] += 1
    case["replica_raw"][1] = _jsonl([_event("replica-1", 1, 99, "fault.contribution_opportunity", bad), *old])
    case["replica_logs"][1] = (_marker(bad) + case["replica_logs"][1]).replace(b"actor=1", b"actor=6").replace(
        b"window_start_monotonic_ns=98", b"window_start_monotonic_ns=999").replace(b"fault_threshold=2", b"fault_threshold=0")
    with pytest.raises(subject.V8RawReplayError): subject.replay_fault_evidence(**case)


@pytest.mark.parametrize("offset_ns, accepted", [
    (32_000_000_000, True),
    (71_999_999_999, True),
    (72_000_000_000, False),
])
def test_replay_uses_native_decision_clock_for_late_boundaries(offset_ns: int, accepted: bool) -> None:
    case = _accepted()
    events = _decode_events(case["replica_raw"][1])
    late = events[-1]["payload"]
    decision = 100 + offset_ns
    late["window_start_monotonic_ns"] = decision - 1
    late["window_end_monotonic_ns"] = decision + 1
    late["decision_monotonic_ns"] = decision
    events[-1]["source_monotonic_ns"] = decision
    case["replica_raw"][1] = _jsonl(events)
    case["replica_logs"][1] = b"".join(_marker(event["payload"]) for event in events)
    if accepted:
        assert subject.replay_fault_evidence(**case)["late_e1_leaf_candidates_untrusted_pending_root_join"][0]["decision_monotonic_ns"] == decision
    else:
        with pytest.raises(subject.V8RawReplayError, match="no late E1 leaf"):
            subject.replay_fault_evidence(**case)


def test_replay_rejects_w18_style_31_second_decision_despite_32_second_source_event() -> None:
    case = _accepted()
    events = _decode_events(case["replica_raw"][1])
    late = events[-1]["payload"]
    late["window_start_monotonic_ns"] = 31_000_000_099
    late["window_end_monotonic_ns"] = 31_000_000_101
    late["decision_monotonic_ns"] = 31_000_000_100
    events[-1]["source_monotonic_ns"] = 32_000_000_100
    case["replica_raw"][1] = _jsonl(events)
    case["replica_logs"][1] = b"".join(_marker(event["payload"]) for event in events)
    with pytest.raises(subject.V8RawReplayError, match="no late E1 leaf"):
        subject.replay_fault_evidence(**case)


def test_replay_rejects_one_nanosecond_pre_boundary_decision_with_delayed_source_event() -> None:
    case = _accepted()
    events = _decode_events(case["replica_raw"][1])
    late = events[-1]["payload"]
    late["window_start_monotonic_ns"] = 32_000_000_098
    late["window_end_monotonic_ns"] = 32_000_000_100
    late["decision_monotonic_ns"] = 32_000_000_099  # A + 32s - 1ns
    events[-1]["source_monotonic_ns"] = 32_000_000_100  # delayed emission at A + 32s
    case["replica_raw"][1] = _jsonl(events)
    case["replica_logs"][1] = b"".join(_marker(event["payload"]) for event in events)
    with pytest.raises(subject.V8RawReplayError, match="no late E1 leaf"):
        subject.replay_fault_evidence(**case)


def _decode_events(raw: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in raw.splitlines()]
