from __future__ import annotations

import copy
import importlib.util
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "n7-path-timeout-quorum" / "sustained_role_v8_validator.py"
spec = importlib.util.spec_from_file_location("w19_v8_validator_test", PATH); assert spec and spec.loader
subject = importlib.util.module_from_spec(spec); spec.loader.exec_module(subject)


def _identity() -> dict[str, object]:
    return {"predecessor_epoch_number": 0, "predecessor_epoch_digest": "a" * 64,
            "successor_epoch_number": 1, "successor_epoch_digest": "b" * 64,
            "command_payload_digest": "c" * 64, "evidence_snapshot_id": "snapshot",
            "baseline_evidence_cutoff": 1, "evidence_cutoff": 2}


def _bundle() -> object:
    identity = _identity()
    tree = lambda tree_id: SimpleNamespace(tree_id=tree_id, members=(0, 2, 3, 4, 5, 6, 1), fanout=2, pipeline_stretch=2, wait_exempt=(1,))
    return SimpleNamespace(command=SimpleNamespace(predecessor_epoch_digest=identity["predecessor_epoch_digest"], successor_epoch_number=1, successor_epoch_digest=identity["successor_epoch_digest"], payload_digest=identity["command_payload_digest"], activation_delay_blocks=5), evidence_snapshot_id="snapshot", evidence_cutoff=2, trees=tuple(tree(tree_id) for tree_id in range(5)))


def _command(sequence: int = 3) -> dict[str, object]:
    return {"event_type": "epoch.command_committed", "source_sequence": sequence, "source_monotonic_ns": 24,
            "payload": {"command_block_height": 10, "command_block_hash": "d" * 64, "payload_digest": "c" * 64,
                        "predecessor_epoch_number": 0, "predecessor_epoch_digest": "a" * 64,
                        "successor_epoch_number": 1, "successor_epoch_digest": "b" * 64,
                        "activation_delay_blocks": 5, "activation_height": 15}}


def _activation(sequence: int = 4) -> dict[str, object]:
    return {"event_type": "epoch.activated", "source_sequence": sequence, "source_monotonic_ns": 32,
            "payload": {"epoch_number": 1, "tree_id": 0, "epoch_digest": "b" * 64, "activation_height": 15}}


def _command_identity() -> dict[str, object]:
    return {
        "predecessor_epoch_number": 0, "predecessor_epoch_digest": "a" * 64,
        "successor_epoch_number": 1, "successor_epoch_digest": "b" * 64,
        "command_payload_digest": "c" * 64, "command_block_height": 10,
        "command_block_hash": "d" * 64, "activation_delay_blocks": 5,
        "activation_height": 15,
    }


def _accepted() -> dict[str, object]:
    identity = _identity()
    selection = {"event_type": "adaptive_v2.selection_decided", "source_kind": "adaptation_manager", "source_sequence": 0, "source_monotonic_ns": 19,
                 "payload": {"schema_version": 1, "cycle_ordinal": 0, "predecessor_epoch_number": 0,
                             "predecessor_epoch_digest": "a" * 64, "baseline_cutoff": 1, "evidence_cutoff": 2,
                             "evidence_snapshot_id": "snapshot", "snapshot_evidence_basis": "exact_post_fault_path_timeout_quorum_v1", "selection_cardinality_policy": "all_guarded_up_to_fault_bound_v1", "selected_replicas": [1]}}
    start = {"event_type": "adaptive_v2.convergence_started", "source_kind": "adaptation_manager", "source_sequence": 1, "source_monotonic_ns": 20,
             "payload": {"cycle_ordinal": 0, **identity}}
    delivery = {"event_type": "adaptive_v2_delivery_attempt", "source_sequence": 2, "source_monotonic_ns": 21,
                "payload": {"replica_id": 0, "delivery_attempt": 1, "disposition": "enqueued", "canonical_payload_digest": hashlib.sha256(b"sealed").hexdigest(), "identity": None}}
    commit = {"event_type": "block.committed", "source_sequence": 5, "source_monotonic_ns": 32_000_000_001,
              "payload": {"block_height": 16, "block_hash": "e" * 64, "parent_hash": "f" * 64, "transaction_count": 1, "designated_observer": True,
                          "decision_proof": {"epoch_number": 1, "tree_id": 0, "epoch_digest": "b" * 64, "block_hash": "e" * 64}, "view_generation": 1, "commit_batch_index": 0}}
    command_authority = {"event_type": "block.committed", "source_sequence": 2, "source_monotonic_ns": 23,
                         "payload": {"block_height": 10, "block_hash": "d" * 64, "parent_hash": "f" * 64, "transaction_count": 1, "designated_observer": True,
                                     "decision_proof": {"epoch_number": 0, "tree_id": 0, "epoch_digest": "a" * 64, "block_hash": "d" * 64}, "view_generation": 1, "commit_batch_index": 0}}
    observed = lambda replica: {"event_type": "block.commit_observed", "source_sequence": 2, "source_monotonic_ns": 23,
        "payload": {"block_height": 10, "block_hash": "d" * 64, "parent_hash": "f" * 64, "transaction_count": 1, "commit_batch_index": replica}}
    observed_e1 = lambda replica: {"event_type": "block.commit_observed", "source_sequence": 7, "source_monotonic_ns": 32_000_000_002,
        "payload": {"block_height": 16, "block_hash": "e" * 64, "parent_hash": "f" * 64, "transaction_count": 1, "commit_batch_index": replica}}
    replicas = {replica: [_command(), _activation(), {
        "event_type": "process.lifecycle", "source_sequence": 8,
        "source_monotonic_ns": 72_000_000_000, "payload": {"state": "running"},
    }] for replica in range(7)}
    replicas[2].insert(2, commit)
    for replica in range(7):
        replicas[replica].insert(0, observed(replica))
        replicas[replica].insert(-1, observed_e1(replica))
    replicas[2].insert(0, command_authority)
    ready = {"event_type": "adaptive_v2_ready", "source_sequence": 3, "source_monotonic_ns": 33,
        "payload": {"replica_id": None, "delivery_attempt": None, "disposition": None,
                    "identity": _command_identity(), "accepted_commit_count": 5,
                    "accepted_activation_count": 5, "required_activation_count": 5,
                    "canonical_payload_digest": None, "failure_reason": None}}
    terminal = {"event_type": "adaptive_v2_session_terminal", "source_sequence": 4, "source_monotonic_ns": 34,
        "payload": {"cycle_ordinal": 0, "policy_intent": "fault_containment",
                    "outcome": "advanced", "reason": "successor_converged",
                    "transition_artifact_id": "e0-to-e1-containment",
                    "predecessor_epoch_number": 0, "predecessor_epoch_digest": "a" * 64,
                    "successor_epoch_number": 1, "successor_epoch_digest": "b" * 64,
                    "command_payload_digest": "c" * 64,
                    "winning_activation": _command_identity(), "controller_failure": None,
                    "evidence_window_activation_generation": 1,
                    "baseline_evidence_cutoff": 1, "current_evidence_cutoff": 2}}
    return {"anchor_monotonic_ns": 0, "expected_identity": identity,
            "manager_events": [selection, start, delivery, ready, terminal],
            "replica_events": replicas, "bundle_bytes": b"sealed",
            "issuer_public_key": "issuer", "predecessor_tree_ids": frozenset(range(7))}


@pytest.fixture(autouse=True)
def _decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "_decode_bundle", lambda _raw, _issuer: _bundle())


def test_v8_validator_accepts_first_cycle_only_with_signed_command_delivery_and_commit() -> None:
    assert subject.validate_v8_raw_contract(**_accepted())["verdict"] == "PASS_COMPONENT_ONLY_NO_CLAIM"


@pytest.mark.parametrize("mutation, error", [
    ("pre_r_activations", "matching epoch.command_committed"),
    ("missing_delivery", "first enqueued delivery"),
    ("missing_selection", "selection_decided"),
    ("missing_commit", "designated E0"),
    ("missing_command_witness", "commit_observed witness"),
    ("missing_e1_witness", "all-seven observations"),
    ("post_command_witness", "does not precede"),
    ("foreign_authority", "misbound epoch activation"),
    ("misbound_command", "exactly one matching epoch.command_committed"),
    ("duplicate_activation", "exactly one matching epoch.activated"),
    ("pre_delivery_witness", "does not precede"),
    ("delivery_sequence", "delivery attempt does not follow"),
    ("foreign_same_time_delivery", "after manager evidence"),
    ("selection_source", "selection_decided"),
    ("selection_policy", "same-cycle manager selection contradicts"),
    ("selection_conflict", "same-cycle manager selection contradicts"),
    ("predecessor_metric", "predecessor authority"),
    ("failed_terminal", "contradicts successor success"),
    ("late_activation", "misses A\\+32"),
])
def test_v8_validator_rejects_the_old_timestamp_only_false_pass(mutation: str, error: str) -> None:
    case = _accepted()
    if mutation == "pre_r_activations":
        for events in case["replica_events"].values(): events[:] = [event for event in events if event.get("event_type") != "epoch.command_committed"]
    elif mutation == "missing_delivery": case["manager_events"].pop(2)
    elif mutation == "missing_selection": case["manager_events"].pop(0)
    elif mutation == "missing_commit": case["replica_events"][2] = [event for event in case["replica_events"][2] if event.get("event_type") != "block.committed"]
    elif mutation == "missing_command_witness": case["replica_events"][6] = [event for event in case["replica_events"][6] if event.get("event_type") != "block.commit_observed"]
    elif mutation == "missing_e1_witness": case["replica_events"][6] = [event for event in case["replica_events"][6] if not (event.get("event_type") == "block.commit_observed" and event["payload"].get("block_height") == 16)]
    elif mutation == "post_command_witness":
        witness = next(event for event in case["replica_events"][0] if event.get("event_type") == "block.commit_observed" and event["payload"]["block_height"] == 10)
        witness["source_sequence"], witness["source_monotonic_ns"] = 4, 25
    elif mutation == "foreign_authority": case["replica_events"][6].insert(-1, {"event_type": "epoch.activated", "source_sequence": 7, "source_monotonic_ns": 40, "payload": {"epoch_number": 2, "tree_id": 0, "epoch_digest": "9" * 64, "activation_height": 20}})
    elif mutation == "misbound_command":
        extra = _command(sequence=7); extra["source_monotonic_ns"] = 26; extra["payload"]["command_block_hash"] = "8" * 64
        case["replica_events"][6].insert(-1, extra)
    elif mutation == "duplicate_activation":
        extra = _activation(sequence=7); extra["source_monotonic_ns"] = 33
        case["replica_events"][6].insert(-1, extra)
    elif mutation == "pre_delivery_witness":
        witness = next(event for event in case["replica_events"][0] if event.get("event_type") == "block.commit_observed" and event["payload"]["block_height"] == 10)
        witness["source_monotonic_ns"] = 21
    elif mutation == "delivery_sequence": case["manager_events"][2]["source_sequence"] = 1
    elif mutation == "foreign_same_time_delivery":
        actual = case["manager_events"][2]; actual["source_sequence"] = 5
        foreign = copy.deepcopy(actual); foreign["source_sequence"] = 2; foreign["payload"]["canonical_payload_digest"] = "0" * 64
        case["manager_events"].insert(2, foreign)
    elif mutation == "selection_source": case["manager_events"][0]["source_kind"] = "replica"
    elif mutation == "selection_policy": case["manager_events"][0]["payload"]["selected_replicas"] = [0]
    elif mutation == "selection_conflict":
        conflicting = copy.deepcopy(case["manager_events"][0]); conflicting["source_monotonic_ns"] = 18; conflicting["payload"]["selected_replicas"] = [0]
        case["manager_events"].insert(0, conflicting)
    elif mutation == "predecessor_metric":
        authority = next(event for event in case["replica_events"][2] if event.get("event_type") == "block.committed" and event["payload"]["block_height"] == 10)
        extra = copy.deepcopy(authority); extra["source_sequence"] = 8; extra["source_monotonic_ns"] = 32_000_000_003
        extra["payload"]["block_height"] = 17; extra["payload"]["block_hash"] = "6" * 64; extra["payload"]["decision_proof"]["block_hash"] = "6" * 64
        case["replica_events"][2].insert(-1, extra)
    elif mutation == "failed_terminal":
        terminal = copy.deepcopy(case["manager_events"][4]); terminal["source_sequence"] = 5; terminal["source_monotonic_ns"] = 35; terminal["payload"]["outcome"] = "failed"
        case["manager_events"].append(terminal)
    else: case["replica_events"][6][1]["source_monotonic_ns"] = 32_000_000_001
    with pytest.raises(subject.V8ValidationError, match=error): subject.validate_v8_raw_contract(**case)


def test_v8_validator_rejects_bundle_identity_mismatch() -> None:
    case = _accepted(); case["expected_identity"] = {**case["expected_identity"], "evidence_cutoff": 3}
    with pytest.raises(subject.V8ValidationError, match="independently decoded"): subject.validate_v8_raw_contract(**case)


@pytest.mark.parametrize("count", [0, 2])
def test_v8_validator_rejects_nonunit_synthetic_command_count_in_every_scored_observation(count: int) -> None:
    case = _accepted()
    for events in case["replica_events"].values():
        for event in events:
            if event.get("event_type") in {"block.committed", "block.commit_observed"} and event["payload"].get("block_height") == 16:
                event["payload"]["transaction_count"] = count
    with pytest.raises(subject.V8ValidationError, match="exactly one synthetic command"):
        subject.validate_v8_raw_contract(**case)


def test_v8_validator_rejects_non_native_delivery_identity_or_pre_anchor_selection() -> None:
    case = _accepted(); case["manager_events"][2]["payload"]["identity"] = {"forged": True}
    with pytest.raises(subject.V8ValidationError, match="first enqueued delivery"):
        subject.validate_v8_raw_contract(**case)
    case = _accepted(); case["manager_events"][0]["source_monotonic_ns"] = 0
    with pytest.raises(subject.V8ValidationError, match="selection_decided"):
        subject.validate_v8_raw_contract(**case)


def test_v8_validator_rejects_two_witnessed_hashes_at_one_e1_height() -> None:
    case = _accepted()
    authority = next(event for event in case["replica_events"][2] if event.get("event_type") == "block.committed" and event["payload"]["block_height"] == 16)
    extra_authority = copy.deepcopy(authority); extra_authority["source_sequence"] = 8; extra_authority["source_monotonic_ns"] = 32_000_000_003
    extra_authority["payload"]["block_hash"] = "7" * 64; extra_authority["payload"]["decision_proof"]["block_hash"] = "7" * 64
    case["replica_events"][2].insert(-1, extra_authority)
    for replica in range(7):
        witness = next(event for event in case["replica_events"][replica] if event.get("event_type") == "block.commit_observed" and event["payload"]["block_height"] == 16)
        extra_witness = copy.deepcopy(witness); extra_witness["source_sequence"] = 8; extra_witness["source_monotonic_ns"] = 32_000_000_003; extra_witness["payload"]["block_hash"] = "7" * 64
        case["replica_events"][replica].insert(-1, extra_witness)
    with pytest.raises(subject.V8ValidationError, match="conflicting commit hashes"):
        subject.validate_v8_raw_contract(**case)


def test_v8_validator_binds_signed_delay_first_cycle_predecessor_tree_and_authority_flag() -> None:
    case = _accepted()
    for events in case["replica_events"].values():
        command = next(event for event in events if event.get("event_type") == "epoch.command_committed")
        command["payload"]["activation_delay_blocks"], command["payload"]["activation_height"] = 6, 16
        activation = next(event for event in events if event.get("event_type") == "epoch.activated")
        activation["payload"]["activation_height"] = 16
    with pytest.raises(subject.V8ValidationError, match="matching epoch.command_committed"):
        subject.validate_v8_raw_contract(**case)

    case = _accepted()
    case["manager_events"][0]["payload"]["cycle_ordinal"] = 7
    case["manager_events"][1]["payload"]["cycle_ordinal"] = 7
    with pytest.raises(subject.V8ValidationError, match="frozen first-cycle"):
        subject.validate_v8_raw_contract(**case)

    case = _accepted()
    authority = next(event for event in case["replica_events"][2] if event.get("event_type") == "block.committed" and event["payload"]["block_height"] == 10)
    authority["payload"]["decision_proof"]["tree_id"] = 999
    with pytest.raises(subject.V8ValidationError, match="exact designated E0"):
        subject.validate_v8_raw_contract(**case)

    case = _accepted()
    e1 = next(event for event in case["replica_events"][2] if event.get("event_type") == "block.committed" and event["payload"]["block_height"] == 16)
    foreign = copy.deepcopy(e1); foreign["source_sequence"] = 8; foreign["payload"]["designated_observer"] = True
    case["replica_events"][0].insert(-1, foreign)
    with pytest.raises(subject.V8ValidationError, match="designated-observer flag"):
        subject.validate_v8_raw_contract(**case)


def test_v8_manager_success_accepts_inclusive_a_plus_32_boundary_only() -> None:
    identity = _identity(); start = {"source_sequence": 1, "source_monotonic_ns": 20_000_000_000}
    ready = {"event_type": "adaptive_v2_ready", "source_sequence": 3,
        "source_monotonic_ns": 20_000_000_002,
        "payload": {"replica_id": None, "delivery_attempt": None, "disposition": None,
                    "identity": _command_identity(), "accepted_commit_count": 5,
                    "accepted_activation_count": 5, "required_activation_count": 5,
                    "canonical_payload_digest": None, "failure_reason": None}}
    terminal = copy.deepcopy(_accepted()["manager_events"][-1])
    terminal["source_sequence"] = 4; terminal["source_monotonic_ns"] = 32_000_000_000
    subject._manager_success([ready, terminal], start=start, delivery_sequence=2,
        delivery_ns=20_000_000_001, activation_deadline_ns=32_000_000_000,
        identity=identity, command=_command()["payload"])
    terminal["source_monotonic_ns"] += 1
    with pytest.raises(subject.V8ValidationError, match="before A\\+32"):
        subject._manager_success([ready, terminal], start=start, delivery_sequence=2,
            delivery_ns=20_000_000_001, activation_deadline_ns=32_000_000_000,
            identity=identity, command=_command()["payload"])


def test_v8_manager_success_is_manager_local_and_may_precede_last_replica_activation() -> None:
    case = _accepted()
    case["manager_events"][3]["source_monotonic_ns"] = 24
    case["manager_events"][4]["source_monotonic_ns"] = 25
    assert subject.validate_v8_raw_contract(**case)["verdict"] == "PASS_COMPONENT_ONLY_NO_CLAIM"
    case = _accepted()
    case["manager_events"][3]["source_monotonic_ns"] = 21
    case["manager_events"][4]["source_monotonic_ns"] = 22
    with pytest.raises(subject.V8ValidationError, match="after manager evidence"):
        subject.validate_v8_raw_contract(**case)


def test_v8_validator_rejects_reduced_manager_terminal_without_native_q5_identity() -> None:
    case = _accepted()
    case["manager_events"] = [event for event in case["manager_events"]
                              if event["event_type"] != "adaptive_v2_ready"]
    case["manager_events"][-1]["payload"] = {
        "outcome": "advanced", "reason": "successor_converged",
        "predecessor_epoch_number": 0, "predecessor_epoch_digest": "a" * 64,
        "successor_epoch_number": 1, "successor_epoch_digest": "b" * 64,
        "command_payload_digest": "c" * 64, "current_evidence_cutoff": 2,
    }
    with pytest.raises(subject.V8ValidationError, match="native Q5"):
        subject.validate_v8_raw_contract(**case)

    case = _accepted()
    del case["manager_events"][-1]["payload"]["winning_activation"]
    with pytest.raises(subject.V8ValidationError, match="complete native successor identity"):
        subject.validate_v8_raw_contract(**case)


def test_v8_validator_rejects_payloadless_timestamp_only_horizon_trailer() -> None:
    case = _accepted()
    for events in case["replica_events"].values():
        trailer = events[-1]
        trailer.pop("event_type"); trailer.pop("payload")
    with pytest.raises(subject.V8ValidationError, match="payload-bearing event through A\\+72"):
        subject.validate_v8_raw_contract(**case)


def test_v8_validator_rejects_same_time_metric_before_local_e1_activation() -> None:
    case = _accepted()
    boundary = 32_000_000_000
    for events in case["replica_events"].values():
        activation = next(event for event in events if event.get("event_type") == "epoch.activated")
        activation["source_monotonic_ns"] = boundary; activation["source_sequence"] = 9
        for event in events:
            if event.get("event_type") in {"block.committed", "block.commit_observed"} and event["payload"]["block_height"] == 16:
                event["source_monotonic_ns"] = boundary
        events[-1]["source_sequence"] = 10
    with pytest.raises(subject.V8ValidationError, match="does not follow local E1 activation"):
        subject.validate_v8_raw_contract(**case)


def test_v8_validator_rejects_negative_foreign_epoch_tree_authority() -> None:
    case = _accepted()
    authority = next(event for event in case["replica_events"][2]
                     if event.get("event_type") == "block.committed" and
                     event["payload"]["block_height"] == 16)
    foreign = copy.deepcopy(authority)
    foreign["source_sequence"] = 8; foreign["source_monotonic_ns"] = 32_000_000_003
    foreign["payload"]["block_height"] = 17; foreign["payload"]["block_hash"] = "6" * 64
    foreign["payload"]["decision_proof"]["epoch_number"] = -1
    foreign["payload"]["decision_proof"]["tree_id"] = -1
    foreign["payload"]["decision_proof"]["block_hash"] = "6" * 64
    case["replica_events"][2].insert(-1, foreign)
    with pytest.raises(subject.V8ValidationError, match="decision proof drifted"):
        subject.validate_v8_raw_contract(**case)
