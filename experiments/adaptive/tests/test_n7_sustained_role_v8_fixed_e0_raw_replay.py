from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "n7-path-timeout-quorum" / "sustained_role_v8_fixed_e0_raw_replay.py"
SPEC = importlib.util.spec_from_file_location("w19_v8_fixed_e0_raw_replay_test", PATH)
assert SPEC and SPEC.loader
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)

RUN = "fixed-e0-replay"
E0 = "a" * 64
BLOCK = "b" * 64
PARENT = "c" * 64
ANCHOR = 20_000_000_000
START = ANCHOR - 10_000_000_000
END = ANCHOR + 72_000_000_000
PROFILE = "e" * 64


def _event(source: str, sequence: int, timestamp: int, event_type: str, payload: object) -> dict[str, object]:
    return {"event_schema_version": 1, "run_id": RUN,
            "source_kind": "adaptation_manager" if source == "adaptive-manager" else "replica",
            "source_id": source, "source_instance": f"instance-{source}",
            "source_sequence": sequence, "source_monotonic_ns": timestamp,
            "event_type": event_type, "payload": payload}


def _jsonl(events: list[dict[str, object]]) -> bytes:
    return b"".join(json.dumps(item, sort_keys=True, separators=(",", ":")).encode() + b"\n" for item in events)


def _opportunity() -> dict[str, object]:
    return {"actor": 1, "proposal": {"epoch_number": 0, "tree_id": 4, "epoch_digest": E0, "block_hash": BLOCK},
            "physical_role": "internal", "parent_replica": 4, "authenticated_proposal_source_replica": 4,
            "expected_message_type": "aggregate_relay", "cohort": "hard", "diagnostic_window": "fixed-v8",
            "window_start_monotonic_ns": ANCHOR - 1, "window_end_monotonic_ns": ANCHOR + 1,
            "decision_monotonic_ns": ANCHOR, "contribution_ordinal": 0, "role_contribution_ordinal": 0,
            "scheduled_action": "omit_aggregate", "responsive_omission_period": 0, "fault_threshold": 2,
            "hard_actor_count": 1, "responsive_degraded_actor_count": 0,
            "fault_mode": "role_scoped_persistent_selected_omission_v1", "view_generation": 0}


def _late_opportunity() -> dict[str, object]:
    payload = copy.deepcopy(_opportunity())
    payload["proposal"]["block_hash"] = "d" * 64
    payload["decision_monotonic_ns"] = ANCHOR + 40_000_000_000
    payload["window_end_monotonic_ns"] = ANCHOR + 72_000_000_000
    return payload


def _marker(payload: dict[str, object]) -> bytes:
    proposal = payload["proposal"]
    fields = {"fault": payload["fault_mode"], "proposal_epoch": proposal["epoch_number"], "proposal_tree": proposal["tree_id"],
              "proposal_epoch_digest": proposal["epoch_digest"], "proposal_block_hash": proposal["block_hash"],
              "window": payload["diagnostic_window"], "window_start_monotonic_ns": payload["window_start_monotonic_ns"],
              "window_end_monotonic_ns": payload["window_end_monotonic_ns"], "actor": payload["actor"],
              "action": payload["scheduled_action"], "monotonic_ns": payload["decision_monotonic_ns"], "cohort": payload["cohort"],
              "hard_actor_count": 1, "responsive_degraded_actor_count": 0, "fault_threshold": 2,
              "max_omissions_per_proposal": 1, "responsive_omission_period": 0,
              "contribution_ordinal": 0, "contribution_role": payload["physical_role"],
              "role_contribution_ordinal": 0, "authenticated_proposal_source_replica": payload["authenticated_proposal_source_replica"]}
    return ("KAURI_FAULT " + " ".join(f"{key}={value}" for key, value in fields.items()) + "\n").encode()


def _observed() -> dict[str, object]:
    return {"block_height": 7, "block_hash": BLOCK, "parent_hash": PARENT, "transaction_count": 1, "commit_batch_index": 0}


def _committed() -> dict[str, object]:
    return {**_observed(), "designated_observer": True,
            "decision_proof": {"epoch_number": 0, "tree_id": 4, "epoch_digest": E0, "block_hash": BLOCK},
            "view_generation": 0}


def _accepted() -> dict[str, object]:
    metric = ANCHOR + 32_000_000_000
    horizon = ANCHOR + 72_000_000_000
    replica_raw: list[bytes] = []
    for replica in range(7):
        source = f"replica-{replica}"
        events = [_event(source, 1, 1, "replica.heartbeat", {})]
        if replica == 1:
            events.append(_event(source, 2, ANCHOR, "fault.contribution_opportunity", _opportunity()))
            sequence = 3
        else:
            sequence = 2
        events.append(_event(source, sequence, metric, "block.commit_observed", _observed()))
        if replica == 2:
            sequence += 1
            events.append(_event(source, sequence, metric + 1, "block.committed", _committed()))
        if replica == 1:
            sequence += 1
            events.append(_event(source, sequence, ANCHOR + 40_000_000_000,
                                 "fault.contribution_opportunity", _late_opportunity()))
        events.append(_event(source, sequence + 1, horizon, "replica.heartbeat", {}))
        replica_raw.append(_jsonl(events))
    cleanup = {"schema_version": 1, "run_id": RUN, "complete": True,
               "processes": [{"source_id": source, "pid": 1, "pgid": 1, "returncode": 0, "termination": "clean-exit"}
                             for source in ("adaptive-manager", *(f"replica-{i}" for i in range(7)))]}
    control = {"run_id": RUN, "profile_sha256": PROFILE, "epoch_zero_digest": E0,
               "window_start_monotonic_ns": START, "window_end_monotonic_ns": END}
    return {"run_id": RUN, "e0_digest": E0, "profile_sha256": PROFILE,
            "scheduled_start_monotonic_ns": START, "scheduled_end_monotonic_ns": END,
            "manager_raw": _jsonl([_event("adaptive-manager", 1, 1, "manager.heartbeat", {}),
                                    _event("adaptive-manager", 2, START, "scheduled_fixed_e0_control.observation", control),
                                    _event("adaptive-manager", 3, ANCHOR + 40_000_000_000,
                                           "scheduled_fixed_e0_control.observation", control),
                                    _event("adaptive-manager", 4, END, "scheduled_fixed_e0_control.terminal", control),
                                    _event("adaptive-manager", 5, END + 1, "adaptive_v2_session_terminal", {
                                        "cycle_ordinal": 0, "policy_intent": "fault_containment", "outcome": "no_op",
                                        "reason": "explicit_no_op", "transition_artifact_id": "scheduled-fixed-e0-control/" + RUN,
                                        "predecessor_epoch_number": 0, "predecessor_epoch_digest": E0,
                                        "successor_epoch_number": None, "successor_epoch_digest": None,
                                        "command_payload_digest": None, "winning_activation": None,
                                        "evidence_window_activation_generation": 1, "baseline_evidence_cutoff": 1,
                                        "current_evidence_cutoff": 1, "controller_failure": None})]),
            "replica_raw": replica_raw,
            "replica_logs": [b"", _marker(_opportunity()) + _marker(_late_opportunity()), b"", b"", b"", b"", b""],
            "cleanup_raw": json.dumps(cleanup, sort_keys=True, separators=(",", ":")).encode()}


def _events(raw_bytes: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in raw_bytes.splitlines()]


def test_fixed_e0_raw_replay_accepts_only_component_evidence() -> None:
    result = subject.replay_fixed_e0_raw(**_accepted())
    assert result["verdict"] == "COMPONENT_VALIDATED_NO_LAUNCH"
    assert result["anchor_monotonic_ns"] == ANCHOR
    assert result["measurement_window"] == {"start_monotonic_ns": ANCHOR + 32_000_000_000, "end_monotonic_ns": ANCHOR + 72_000_000_000}
    assert result["common_authoritative_e0_commit_count"] == 1
    assert result["late_e0_physical_omission_count"] == 1
    assert result["profile_sha256"] == PROFILE
    assert result["claim_eligible"] is False and result["figure_eligible"] is False
    assert result["missing_fixed_input_closure"]


@pytest.mark.parametrize("mutation, error", [
    ("e1_command", "forbidden successor"),
    ("e1_activation", "forbidden successor"),
    ("wrong_transaction_count", "one-command maximum"),
    ("missing_witness", "no all-seven common"),
    ("duplicate_instance", "eight unique source instances"),
    ("foreign_fault", "outside physical actor 1"),
    ("abort", "abort or terminal"),
    ("unclean_cleanup", "eight unique clean exits"),
])
def test_fixed_e0_replay_rejects_authority_and_cleanup_mutations(mutation: str, error: str) -> None:
    case = copy.deepcopy(_accepted())
    if mutation in {"e1_command", "e1_activation"}:
        events = _events(case["replica_raw"][0])
        events.insert(1, _event("replica-0", 2, 2,
                                "epoch.command_committed" if mutation == "e1_command" else "epoch.activated", {}))
        for index, event in enumerate(events, 1): event["source_sequence"] = index
        case["replica_raw"][0] = _jsonl(events)
    elif mutation == "wrong_transaction_count":
        events = _events(case["replica_raw"][2])
        events[2]["payload"]["transaction_count"] = 2
        case["replica_raw"][2] = _jsonl(events)
    elif mutation == "missing_witness":
        events = _events(case["replica_raw"][6])
        del events[1]
        for index, event in enumerate(events, 1): event["source_sequence"] = index
        case["replica_raw"][6] = _jsonl(events)
    elif mutation == "duplicate_instance":
        events = _events(case["replica_raw"][0])
        for event in events: event["source_instance"] = "instance-replica-1"
        case["replica_raw"][0] = _jsonl(events)
    elif mutation == "foreign_fault":
        events = _events(case["replica_raw"][0])
        events.insert(1, _event("replica-0", 2, ANCHOR, "fault.contribution_opportunity", _opportunity()))
        for index, event in enumerate(events, 1): event["source_sequence"] = index
        case["replica_raw"][0] = _jsonl(events)
    elif mutation == "abort":
        case["abort_raw"] = b"{}"
    elif mutation == "unclean_cleanup":
        receipt = json.loads(case["cleanup_raw"]); receipt["processes"][0]["returncode"] = 1
        case["cleanup_raw"] = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(subject.V8FixedE0RawReplayError, match=error):
        subject.replay_fixed_e0_raw(**case)


def test_fixed_e0_replay_rejects_boundary_e1_authority_and_terminal() -> None:
    case = _accepted()
    case["terminal_raw"] = b"{}"
    with pytest.raises(subject.V8FixedE0RawReplayError, match="abort or terminal"):
        subject.replay_fixed_e0_raw(**case)


@pytest.mark.parametrize("mutation, error", [
    ("missing_late", "no physical omission"),
    ("missing_late_marker", "biject"),
    ("late_e1", "frozen actor-1 E0"),
    ("late_wrong_role", "frozen actor-1 E0"),
    ("duplicate_late", "unique native markers"),
])
def test_fixed_e0_replay_rejects_invalid_persistent_exposure(mutation: str, error: str) -> None:
    case = copy.deepcopy(_accepted())
    events = _events(case["replica_raw"][1])
    if mutation == "missing_late":
        del events[3]
        case["replica_logs"][1] = _marker(_opportunity())
    elif mutation == "missing_late_marker":
        case["replica_logs"][1] = _marker(_opportunity())
    elif mutation in {"late_e1", "late_wrong_role"}:
        payload = events[3]["payload"]
        if mutation == "late_e1":
            payload["proposal"]["epoch_number"] = 1
        else:
            payload["physical_role"] = "leaf"
            payload["expected_message_type"] = "direct_vote"
            payload["scheduled_action"] = "omit_direct_vote"
        case["replica_logs"][1] = _marker(_opportunity()) + _marker(payload)
    else:
        events.insert(4, copy.deepcopy(events[3]))
        case["replica_logs"][1] += _marker(_late_opportunity())
    for index, event in enumerate(events, 1):
        event["source_sequence"] = index
    case["replica_raw"][1] = _jsonl(events)
    with pytest.raises(subject.V8FixedE0RawReplayError, match=error):
        subject.replay_fixed_e0_raw(**case)


@pytest.mark.parametrize("mutation, error", [
    ("missing_terminal", "lacks observation"),
    ("wrong_binding", "differs from sealed"),
    ("early_terminal", "precedes scheduled end"),
    ("no_late_observation", "lacks observation"),
    ("adaptive_transition", "forbidden adaptive"),
])
def test_fixed_e0_replay_rejects_unbound_native_control(mutation: str, error: str) -> None:
    case = copy.deepcopy(_accepted())
    events = _events(case["manager_raw"])
    if mutation == "missing_terminal":
        events.pop(-2)
    elif mutation == "wrong_binding":
        events[1]["payload"]["epoch_zero_digest"] = "f" * 64
    elif mutation == "early_terminal":
        events[-2]["source_monotonic_ns"] = END - 1
    elif mutation == "no_late_observation":
        events.pop(2)
    else:
        events.insert(2, _event("adaptive-manager", 3, ANCHOR + 1,
                                "adaptive_v2.convergence_started", {}))
    for index, event in enumerate(events, 1):
        event["source_sequence"] = index
    case["manager_raw"] = _jsonl(events)
    with pytest.raises(subject.V8FixedE0RawReplayError, match=error):
        subject.replay_fixed_e0_raw(**case)
