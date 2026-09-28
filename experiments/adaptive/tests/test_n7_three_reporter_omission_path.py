from __future__ import annotations

from copy import deepcopy
import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-three-reporter-omission"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PATH / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load("n7_three_runner_test", "runner.py")
validator = _load("n7_three_validator_test", "validator.py")


def _event(tree_id: int, reporter_id: int, sequence: int, *, outcome: str = "timeout"):
    return {
        "event_schema_version": 1, "run_id": "run-1", "source_kind": "adaptation_manager",
        "source_id": "adaptive-manager", "source_instance": "manager-1", "source_sequence": sequence,
        "source_monotonic_ns": 400 + sequence, "event_type": "evidence.observation_accepted",
        "payload": {"ingestion_sequence": sequence - 1, "observation": {
            "schema_version": 3, "observation_id": f"{sequence:064x}", "reporter_id": reporter_id,
            "observed_replica_id": 1, "configuration": {"epoch_number": 0, "tree_id": tree_id, "epoch_digest": "a" * 64},
            "block_hash": f"{sequence + 10:064x}", "expected_message_type": "aggregate_relay", "outcome": outcome,
            "response_duration_us": 0, "deadline_duration_us": 500, "reporter_monotonic_ns": 50 + sequence,
            "reporter_sequence": sequence, "signer_set": [], "attempt_start_monotonic_ns": 300 + sequence,
            "reporter_local_commit_monotonic_ns": 320 + sequence,
        }},
    }


def test_preflight_binds_file_topology_and_three_context_overlay():
    record = runner.preflight("a" * 64)
    assert record["status"] == "PREFLIGHT_ONLY"
    assert record["relay_omission"]["parent_reporters"] == [4, 5, 6]
    assert record["relay_omission"]["required_timeouts_per_reporter"] == 2
    assert record["relay_omission"]["total_omission_contexts"] == 6
    overlay = record["relay_omission"]["argv_overlay"]
    contexts = overlay[overlay.index("--experiment-omission-additional-configurations") + 1]
    assert contexts == f"0:5:{'a' * 64},0:6:{'a' * 64}"
    assert record["tree_file"]["main_config_overrides"][0] == "tree-generation = file"


def _six_events():
    return [
        _event(tree_id, reporter_id, sequence)
        for sequence, (tree_id, reporter_id) in enumerate(
            ((4, 4), (4, 4), (5, 5), (5, 5), (6, 6), (6, 6)), 2
        )
    ]


def test_validator_accepts_only_six_exact_v3_contexts():
    verdict = validator.validate(
        runner.preflight("a" * 64),
        _six_events(),
        run_id="run-1",
    )
    assert verdict["verdict"] == "EVIDENCE_PREFIX_PASS"
    assert verdict["reporters"] == [4, 5, 6]
    assert len(verdict["observation_ids"]) == 6


def test_validator_fails_closed_when_a_reporter_context_is_missing():
    with pytest.raises(validator.ValidationError, match="missing"):
        validator.validate(
            runner.preflight("a" * 64), _six_events()[:-2], run_id="run-1"
        )


def test_validator_rejects_a_reused_proposal_within_one_parent_context():
    events = _six_events()
    events[1]["payload"]["observation"]["block_hash"] = events[0]["payload"]["observation"]["block_hash"]
    with pytest.raises(validator.ValidationError, match="reuses"):
        validator.validate(
            runner.preflight("a" * 64),
            events,
            run_id="run-1",
        )


def test_validator_ignores_an_unrelated_late_observation():
    unrelated = _event(3, 3, 8, outcome="late")
    unrelated["payload"]["observation"]["observed_replica_id"] = 5
    verdict = validator.validate(
        runner.preflight("a" * 64),
        [*_six_events(), unrelated],
        run_id="run-1",
    )
    assert verdict["verdict"] == "EVIDENCE_PREFIX_PASS"


def _arm():
    return {
        "event_schema_version": 1, "run_id": "run-1", "source_kind": "adaptation_manager",
        "source_id": "adaptive-manager", "source_instance": "manager-1", "source_sequence": 1,
        "source_monotonic_ns": 101, "event_type": "fault_window_armed",
        "payload": {"schema_version": 4, "kind": "kauri-focused-fault-window-arm-v4", "run_id": "run-1", "profile_id": "n7", "profile_sha256": "b" * 64, "topology_proof_sha256": "c" * 64, "request_sha256": "d" * 64, "epoch_number": 0, "epoch_digest": "a" * 64, "fault_receipt_sha256": "e" * 64, "evidence_start_monotonic_ns": 100, "prefault_tree_id": 4, "required_tree_positions": 3, "required_tree_ids": [4, 5, 6], "clock_domain": "same_host_clock_monotonic_raw", "required_observation_schema": 3, "snapshot_evidence_basis": "exact_post_fault_attempt_start_v1", "selection_cardinality_policy": "all_guarded_up_to_fault_bound_v1", "timeout_evidence_basis": "exact_timeout_attempt_id_v1", "fault_window_arm_sha256": "f" * 64},
    }


def _snapshot(sequence: int):
    return {
        "event_schema_version": 1, "run_id": "run-1", "source_kind": "adaptation_manager",
        "source_id": "adaptive-manager", "source_instance": "manager-1", "source_sequence": sequence,
        "source_monotonic_ns": 500 + sequence, "event_type": "adaptive_v2_evidence_snapshot",
        "payload": {
            "schema_version": 2, "cycle_ordinal": 0, "policy_intent": "fault_containment",
            "transition_artifact_id": "n7-e1", "predecessor_epoch_number": 0,
            "predecessor_epoch_digest": "a" * 64, "activation_generation": 1,
            "baseline_cutoff": 1, "current_cutoff": 6, "full_prefix_snapshot_id": "b" * 64,
            "evidence_snapshot_id": "c" * 64, "accepted_prefix_count": 6,
            "eligible_ranking": [0, 2, 3, 4, 5, 6],
        },
    }


def _replica_streams():
    command = {
        "command_block_height": 10, "command_block_hash": "d" * 64, "payload_digest": "e" * 64,
        "predecessor_epoch_number": 0, "predecessor_epoch_digest": "a" * 64,
        "successor_epoch_number": 1, "successor_epoch_digest": "f" * 64,
        "activation_delay_blocks": 2, "activation_height": 12,
    }
    streams = {}
    for replica in range(7):
        def event(sequence, event_type, payload):
            return {
                "event_schema_version": 1, "run_id": "run-1", "source_kind": "replica",
                "source_id": f"replica-{replica}", "source_instance": f"replica-instance-{replica}",
                "source_sequence": sequence, "source_monotonic_ns": 600 + sequence,
                "event_type": event_type, "payload": payload,
            }
        activation = {"epoch_number": 1, "tree_id": replica % 5, "epoch_digest": "f" * 64, "activation_height": 12}
        if replica == 0:
            baseline = {
                "block_height": 9, "block_hash": "0" * 63 + "1", "parent_hash": None, "transaction_count": 1,
                "designated_observer": True,
                "decision_proof": {"epoch_number": 0, "tree_id": 0, "epoch_digest": "a" * 64, "block_hash": "0" * 63 + "1"},
                "view_generation": None, "commit_batch_index": 0,
            }
            commit = {
                "block_height": 13, "block_hash": "1" * 64, "parent_hash": None, "transaction_count": 1,
                "designated_observer": True,
                "decision_proof": {"epoch_number": 1, "tree_id": 0, "epoch_digest": "f" * 64, "block_hash": "1" * 64},
                "view_generation": None, "commit_batch_index": 0,
            }
            tail = event(4, "block.committed", commit)
            tail["source_monotonic_ns"] = 604
            first = event(1, "block.committed", baseline)
            first["source_monotonic_ns"] = 50
        else:
            baseline = {"block_height": 9, "block_hash": "0" * 63 + "1", "parent_hash": None, "transaction_count": 1, "commit_batch_index": 0}
            tail = event(3, "block.commit_observed", {"block_height": 13, "block_hash": "1" * 64, "parent_hash": None, "transaction_count": 1, "commit_batch_index": 0})
            tail["source_sequence"] = 4
            tail["source_monotonic_ns"] = 604
            first = event(1, "block.commit_observed", baseline)
            first["source_monotonic_ns"] = 50
        streams[f"replica-{replica}"] = [first, event(2, "epoch.command_committed", command), event(3, "epoch.activated", activation), tail]
    return streams


def test_partial_raw_validator_requires_arm_six_attempts_and_all_seven_commit_witnesses():
    manager = [_arm(), *_six_events(), _snapshot(8)]
    verdict = validator.validate_known_raw_events(
        runner.preflight("a" * 64), manager, _replica_streams(), _arm(), run_id="run-1"
    )
    assert verdict["verdict"] == "PARTIAL_ONLY"
    assert verdict["selected_replica_proof"] == "UNAVAILABLE_IN_KNOWN_RAW_EVENT_SCHEMA"


def test_prearm_common_commit_skips_non_designated_rich_events():
    streams = _replica_streams()
    for event in streams["replica-0"]:
        event["source_sequence"] += 1
    non_designated = deepcopy(streams["replica-0"][0])
    non_designated["source_sequence"] = 1
    non_designated["source_monotonic_ns"] = 49
    non_designated["payload"]["designated_observer"] = False
    streams["replica-0"].insert(0, non_designated)

    assert validator._common_e0_commit_before_arm(
        streams, epoch_digest="a" * 64, arm_start_ns=101
    ) == (9, "0" * 63 + "1")
    verdict = validator.validate_known_raw_events(
        runner.preflight("a" * 64),
        [_arm(), *_six_events(), _snapshot(8)],
        streams,
        _arm(),
        run_id="run-1",
    )
    assert verdict["verdict"] == "PARTIAL_ONLY"


def test_partial_raw_validator_rejects_prearm_timeout():
    manager = [_arm(), *_six_events(), _snapshot(8)]
    manager[1]["payload"]["observation"]["attempt_start_monotonic_ns"] = 100
    with pytest.raises(validator.ValidationError, match="after the fault-window arm"):
        validator.validate_known_raw_events(
            runner.preflight("a" * 64), manager, _replica_streams(), _arm(), run_id="run-1"
        )


def test_partial_raw_validator_rejects_attempt_at_manager_arm_envelope_time():
    manager = [_arm(), *_six_events(), _snapshot(8)]
    manager[1]["payload"]["observation"]["attempt_start_monotonic_ns"] = 101
    with pytest.raises(validator.ValidationError, match="after the fault-window arm"):
        validator.validate_known_raw_events(
            runner.preflight("a" * 64), manager, _replica_streams(), _arm(), run_id="run-1"
        )


def test_partial_raw_validator_rejects_missing_common_witness():
    streams = _replica_streams()
    streams["replica-6"] = streams["replica-6"][:-1]
    with pytest.raises(validator.ValidationError, match="common commit"):
        validator.validate_known_raw_events(
            runner.preflight("a" * 64), [_arm(), *_six_events(), _snapshot(8)], streams, _arm(), run_id="run-1"
        )


def test_partial_raw_validator_rejects_missing_prearm_e0_peer():
    streams = _replica_streams()
    streams["replica-6"] = streams["replica-6"][1:]
    with pytest.raises(validator.ValidationError, match="pre-arm common E0"):
        validator.validate_known_raw_events(
            runner.preflight("a" * 64), [_arm(), *_six_events(), _snapshot(8)], streams, _arm(), run_id="run-1"
        )


def test_partial_raw_validator_rejects_mismatched_prearm_e0_peer():
    streams = _replica_streams()
    streams["replica-5"][0]["payload"]["block_hash"] = "2" * 64
    with pytest.raises(validator.ValidationError, match="pre-arm common E0"):
        validator.validate_known_raw_events(
            runner.preflight("a" * 64), [_arm(), *_six_events(), _snapshot(8)], streams, _arm(), run_id="run-1"
        )


def test_partial_raw_validator_rejects_e0_commit_after_arm():
    streams = _replica_streams()
    for events in streams.values():
        events[0]["source_monotonic_ns"] = 100
    with pytest.raises(validator.ValidationError, match="pre-arm common E0"):
        validator.validate_known_raw_events(
            runner.preflight("a" * 64), [_arm(), *_six_events(), _snapshot(8)], streams, _arm(), run_id="run-1"
        )


def test_native_injection_uses_envelope_time_not_earlier_gate_file_time():
    arm = _arm()
    injection = {
        "event_schema_version": 1, "run_id": "run-1", "source_kind": "replica",
        "source_id": "replica-1", "source_instance": "replica-instance-1",
        "source_sequence": 7, "source_monotonic_ns": 250,
        "event_type": "fault.injection_armed",
        "payload": {
            "actor": 1, "gate_sha256": "b" * 64,
            "manager_fault_window_arm_event_sha256": "c" * 64,
            "profile_sha256": "d" * 64, "tree_file_sha256": "e" * 64,
            "launch_argv_sha256": "f" * 64,
            # Gate-file value is deliberately earlier than the sink envelope.
            "activation_monotonic_ns": 150,
        },
    }
    assert validator._validate_native_injection(
        injection, manager_arm_event=arm, manager_arm_line_sha256="c" * 64
    ) == "b" * 64


def _artifact(path: str, payload: bytes) -> dict[str, str]:
    return {"path": path, "sha256": hashlib.sha256(payload).hexdigest()}


def _u(value: int, size: int) -> bytes:
    return value.to_bytes(size, "big")


def _component(value: bytes) -> bytes:
    return _u(len(value), 4) + value


def _signed_n7_bundle(previous_digest: str) -> tuple[bytes, object, str]:
    """Test-local N=7 encoder for the native v2 wire, verified by production decoder."""
    fv = validator.factorial_validation
    membership = tuple(range(7))
    membership_digest = hashlib.sha256(
        b"kauri-membership-v1" + _u(7, 4) + b"".join(_u(i, 2) for i in membership)
    ).hexdigest()
    trees = [(tree_id, (0, 2, 3, 1, 4, 5, 6), (1,)) for tree_id in range(5)]
    canonical = bytearray(b"kauri-epoch-definition-v2")
    canonical += _u(2, 4) + _u(1, 4) + bytes.fromhex(previous_digest)
    canonical += bytes.fromhex(membership_digest) + _u(41719, 8)
    canonical += _component(b"operator-capacity-v1") + _component(b"c" * 64) + _u(6, 8) + _u(5, 4)
    for tree_id, members, exempt in trees:
        canonical += _u(tree_id, 4) + _u(2, 4) + _u(2, 4) + _u(7, 4)
        canonical += b"".join(_u(member, 2) for member in members)
        canonical += _u(1, 4) + _u(exempt[0], 2)
    successor = hashlib.sha256(canonical).hexdigest()
    signed = b"kauri-authorized-epoch-change-v1" + _u(1, 4) + _u(2, 1) + _u(1, 4) + _u(1, 4) + bytes.fromhex(previous_digest) + bytes.fromhex(successor) + _u(5, 8)
    point = fv._secp256k1_multiply(2, (fv._SECP256K1_GX, fv._SECP256K1_GY))
    assert point is not None
    order = fv._SECP256K1_ORDER
    r = point[0] % order
    s = (pow(2, -1, order) * (int.from_bytes(hashlib.sha256(signed).digest(), "big") + r)) % order
    if s > order // 2:
        s = order - s
    command = signed + r.to_bytes(32, "big") + s.to_bytes(32, "big")
    definition = _u(2, 4) + _u(2, 1) + _u(6, 1) + bytes.fromhex(successor) + bytes(canonical)[len(b"kauri-epoch-definition-v2"):]
    wire = b"kauri-adaptive-v2-epoch-change-bundle-v1" + _u(1, 4) + _u(2, 1) + _component(command) + _component(definition)
    issuer = "02" + f"{fv._SECP256K1_GX:064x}"
    decoded = fv.decode_epoch_change_bundle(wire, issuer_public_key=issuer)
    return wire, decoded, issuer


def test_raw_bundle_validator_requires_the_complete_source_bound_receipt(tmp_path: Path):
    with pytest.raises(validator.ValidationError, match="schema drift"):
        validator.validate_raw_bundle(tmp_path, {"schema_version": 1})


def test_raw_bundle_validator_rejects_an_incomplete_plan_before_source_parsing(tmp_path: Path):
    preflight = runner.preflight("a" * 64)
    plan = {"scenario": runner.SCENARIO, "run_id": "run-1"}; plan["plan_sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    plan_bytes = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    auth = {"schema_version": 1, "kind": "kauri-n7-local-execution-authorization-v1", "request_sha256": "8" * 64, "execution_plan_sha256": plan["plan_sha256"], "approval_reference": "test", "approved_utc": "2026-09-28T00:00:00Z", "no_retry": True}
    auth_bytes = json.dumps(auth, sort_keys=True, separators=(",", ":")).encode()
    preflight["approved_issuer_public_key_sha256"] = "1" * 64
    preflight["approved_plan_request_sha256"] = "8" * 64
    preflight["approved_plan_authorization_sha256"] = hashlib.sha256(auth_bytes).hexdigest()
    tree_bytes = Path(preflight["tree_file"]["path"]).read_bytes()
    preflight["tree_file"] = {**preflight["tree_file"], "path": "/missing/relocated-epoch0.tree"}
    payloads = {
        "preflight.json": json.dumps(preflight).encode(), "inputs/epoch0.tree": tree_bytes, "runtime/n7-local-execution-plan.json": plan_bytes, "authorization.json": auth_bytes,
        "runtime/execution-authorization-request.json": b"{}\n", "runtime/fault-window-arm.json": b"{}\n", "runtime/static-omission-gate.json": b"{}\n",
            "manager.jsonl": b"{}\n",
        "bundle.bin": b"x",
        "issuer.txt": b"02" + b"0" * 64 + b"\n",
        "cleanup.json": b"{}",
    }
    for replica in range(7):
        payloads[f"replica-{replica}.jsonl"] = b"{}\n"
    for name, value in payloads.items():
        target = tmp_path / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(value)
    receipt = {
        "schema_version": 1, "scenario": runner.SCENARIO, "run_id": "run-1",
        "artifacts": {
                "preflight": _artifact("preflight.json", payloads["preflight.json"]),
                "epoch0_tree": _artifact("inputs/epoch0.tree", tree_bytes),
                "execution_plan": _artifact("runtime/n7-local-execution-plan.json", plan_bytes),
                "authorization_request": _artifact("runtime/execution-authorization-request.json", payloads["runtime/execution-authorization-request.json"]),
                "plan_authorization": _artifact("authorization.json", auth_bytes),
                "fault_window_arm": _artifact("runtime/fault-window-arm.json", payloads["runtime/fault-window-arm.json"]),
                "omission_gate": _artifact("runtime/static-omission-gate.json", payloads["runtime/static-omission-gate.json"]),
                "manager_events": {"path": "manager.jsonl", "sha256": "0" * 64},
            "replica_streams": {f"replica-{i}": _artifact(f"replica-{i}.jsonl", payloads[f"replica-{i}.jsonl"]) for i in range(7)},
            "e1_bundle": _artifact("bundle.bin", payloads["bundle.bin"]),
            "issuer_public_key": _artifact("issuer.txt", payloads["issuer.txt"]),
            "cleanup": _artifact("cleanup.json", payloads["cleanup.json"]),
        },
        "fault_window_arm": {"source_sequence": 1, "line_sha256": "0" * 64, "clock_domain": "host-raw"},
        "fault_injection_arm": {"source_sequence": 2, "line_sha256": "0" * 64, "clock_domain": "host-raw"},
    }
    with pytest.raises(validator.ValidationError, match="execution plan bindings"):
        validator.validate_raw_bundle(tmp_path, receipt)


def test_raw_bundle_validator_accepts_native_signed_n7_bundle_and_rejects_bound_mutations(tmp_path: Path):
    wire, decoded, issuer = _signed_n7_bundle("a" * 64)
    preflight = runner.preflight("a" * 64)
    issuer_bytes = (issuer + "\n").encode()
    preflight["approved_issuer_public_key_sha256"] = hashlib.sha256(issuer_bytes).hexdigest()
    tree_bytes = Path(preflight["tree_file"]["path"]).read_bytes()
    tree_sha = hashlib.sha256(tree_bytes).hexdigest()
    plan = {
        "scenario": runner.SCENARIO, "run_id": "run-1", "state": "PREPARED_EXTERNAL_APPROVAL_REQUIRED",
        "profile_sha256": "d" * 64, "no_retry": True,
        "bindings": {
            "run_id": "run-1", "manager_source_instance": "manager-1", "epoch_digest": "a" * 64,
            "tree_file_sha256": tree_sha, "topology_proof_sha256": tree_sha,
            "transition_request_sha256": "e" * 64, "replica_1_launch_argv_sha256": "f" * 64,
            "replica_1_launch_argv_sha256_domain": "kauri-n7-replica-argv-without-self-hash-v1",
        },
    }
    plan["plan_sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    plan_bytes = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    request = {"schema_version": 1, "kind": "kauri-n7-local-execution-authorization-request-v1", "scenario": runner.SCENARIO, "execution_plan_sha256": plan["plan_sha256"], "no_retry": True}
    request_bytes = json.dumps(request, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    request_sha = hashlib.sha256(request_bytes).hexdigest()
    preflight["approved_plan_request_sha256"] = request_sha
    authorization = {"schema_version": 1, "kind": "kauri-n7-local-execution-authorization-v1", "request_sha256": request_sha, "execution_plan_sha256": plan["plan_sha256"], "approval_reference": "synthetic-test-only", "approved_utc": "2026-09-28T00:00:00Z", "no_retry": True}
    authorization_bytes = json.dumps(authorization, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    preflight["approved_plan_authorization_sha256"] = hashlib.sha256(authorization_bytes).hexdigest()
    preflight["tree_file"] = {**preflight["tree_file"], "path": "/missing/relocated-epoch0.tree"}
    arm_file = dict(_arm()["payload"])
    arm_file.pop("fault_window_arm_sha256")
    arm_file.update({
        "profile_sha256": plan["profile_sha256"], "topology_proof_sha256": tree_sha,
        "request_sha256": plan["bindings"]["transition_request_sha256"],
        "fault_receipt_sha256": hashlib.sha256(authorization_bytes).hexdigest(),
    })
    arm_file_bytes = json.dumps(arm_file, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    arm_event = _arm()
    arm_event["payload"] = {**arm_file, "fault_window_arm_sha256": hashlib.sha256(arm_file_bytes).hexdigest()}
    manager = [arm_event, *_six_events(), _snapshot(8)]
    streams = _replica_streams()
    command = streams["replica-0"][1]["payload"]
    for event_list in streams.values():
        event_list[1]["payload"] = {**command, "payload_digest": decoded.command.payload_digest,
            "successor_epoch_digest": decoded.epoch_digest}
        event_list[2]["payload"]["epoch_digest"] = decoded.epoch_digest
    streams["replica-0"][3]["payload"]["decision_proof"]["epoch_digest"] = decoded.epoch_digest
    manager_bytes = b"".join(json.dumps(x, sort_keys=True, separators=(",", ":")).encode() + b"\n" for x in manager)
    arm_sha = hashlib.sha256(manager_bytes.splitlines()[0]).hexdigest()
    gate_file = {
        "schema_version": 1, "kind": "kauri-n7-static-aggregate-omission-gate-v1",
        "profile_sha256": plan["profile_sha256"], "tree_file_sha256": tree_sha,
        "epoch_digest": "a" * 64, "replica_id": 1,
        "launch_argv_sha256": plan["bindings"]["replica_1_launch_argv_sha256"],
        "manager_run_id": "run-1", "manager_source_instance": "manager-1",
        "manager_source_sequence": 1, "fault_window_arm_event_sha256": arm_sha,
        "activation_monotonic_ns": 120,
    }
    gate_file_bytes = json.dumps(gate_file, separators=(",", ":")).encode() + b"\n"
    gate_sha = hashlib.sha256(gate_file_bytes).hexdigest()
    injection_payload = {"actor": 1, "gate_sha256": gate_sha, "manager_fault_window_arm_event_sha256": arm_sha,
                         "profile_sha256": plan["profile_sha256"], "tree_file_sha256": tree_sha,
                         "launch_argv_sha256": plan["bindings"]["replica_1_launch_argv_sha256"],
                         "activation_monotonic_ns": 120}
    replica_one_tail = streams["replica-1"][1:]
    for sequence, event in enumerate(replica_one_tail, 9):
        event["source_sequence"] = sequence
        event["source_monotonic_ns"] = 600 + sequence
    streams["replica-1"] = [streams["replica-1"][0], _replica_event(2, 250, "fault.injection_armed", injection_payload)]
    for sequence, event in enumerate(_six_events(), 3):
        observation = event["payload"]["observation"]
        streams["replica-1"].append(_replica_event(sequence, 350 + sequence, "fault.aggregate_omitted", {
            "actor": 1, "parent_replica": observation["configuration"]["tree_id"], "epoch_number": 0,
            "tree_id": observation["configuration"]["tree_id"], "epoch_digest": "a" * 64,
            "block_hash": observation["block_hash"], "gate_sha256": gate_sha, "first_for_context": True}))
    streams["replica-1"].extend(replica_one_tail)
    payloads = {"preflight.json": json.dumps(preflight, sort_keys=True).encode(), "inputs/epoch0.tree": tree_bytes, "runtime/n7-local-execution-plan.json": plan_bytes, "authorization.json": authorization_bytes, "runtime/execution-authorization-request.json": request_bytes, "runtime/fault-window-arm.json": arm_file_bytes, "runtime/static-omission-gate.json": gate_file_bytes, "manager.jsonl": manager_bytes,
                "bundle.bin": wire, "issuer.txt": issuer_bytes,
                "cleanup.json": json.dumps({"schema_version": 1, "run_id": "run-1", "complete": True, "processes": [
                    {"source_id": source, "pid": i + 1, "pgid": i + 1, "returncode": 0, "termination": "clean-exit"}
                    for i, source in enumerate(["adaptive-manager", *[f"replica-{i}" for i in range(7)]])]}).encode()}
    for source, events in streams.items():
        payloads[f"{source}.jsonl"] = b"".join(json.dumps(x, sort_keys=True, separators=(",", ":")).encode() + b"\n" for x in events)
    for name, value in payloads.items():
        target = tmp_path / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(value)
    receipt = {"schema_version": 1, "scenario": runner.SCENARIO, "run_id": "run-1", "artifacts": {
        "preflight": _artifact("preflight.json", payloads["preflight.json"]), "epoch0_tree": _artifact("inputs/epoch0.tree", tree_bytes), "execution_plan": _artifact("runtime/n7-local-execution-plan.json", plan_bytes), "authorization_request": _artifact("runtime/execution-authorization-request.json", request_bytes), "plan_authorization": _artifact("authorization.json", authorization_bytes), "fault_window_arm": _artifact("runtime/fault-window-arm.json", arm_file_bytes), "omission_gate": _artifact("runtime/static-omission-gate.json", gate_file_bytes), "manager_events": _artifact("manager.jsonl", manager_bytes),
        "replica_streams": {f"replica-{i}": _artifact(f"replica-{i}.jsonl", payloads[f"replica-{i}.jsonl"]) for i in range(7)},
        "e1_bundle": _artifact("bundle.bin", wire), "issuer_public_key": _artifact("issuer.txt", issuer_bytes), "cleanup": _artifact("cleanup.json", payloads["cleanup.json"])},
        "fault_window_arm": {"source_sequence": 1, "line_sha256": arm_sha, "clock_domain": "host-raw"},
        "fault_injection_arm": {"source_sequence": 2, "line_sha256": hashlib.sha256(payloads["replica-1.jsonl"].splitlines()[1]).hexdigest(), "clock_domain": "host-raw"}}
    assert validator.validate_raw_bundle(tmp_path, receipt)["verdict"] == "RAW_BUNDLE_VALIDATED"
    bad_arm = deepcopy(receipt); bad_arm["fault_window_arm"]["line_sha256"] = "0" * 64
    with pytest.raises(validator.ValidationError, match="fault-window arm"):
        validator.validate_raw_bundle(tmp_path, bad_arm)
    altered_arm = dict(arm_file); altered_arm["profile_sha256"] = "0" * 64
    altered_arm_bytes = json.dumps(altered_arm, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    (tmp_path / "runtime/fault-window-arm.json").write_bytes(altered_arm_bytes)
    bad_arm_file = deepcopy(receipt)
    bad_arm_file["artifacts"]["fault_window_arm"] = _artifact("runtime/fault-window-arm.json", altered_arm_bytes)
    with pytest.raises(validator.ValidationError, match="manager fault-window arm differs"):
        validator.validate_raw_bundle(tmp_path, bad_arm_file)
    (tmp_path / "runtime/fault-window-arm.json").write_bytes(arm_file_bytes)
    altered_gate = dict(gate_file); altered_gate["launch_argv_sha256"] = "0" * 64
    altered_gate_bytes = json.dumps(altered_gate, separators=(",", ":")).encode() + b"\n"
    (tmp_path / "runtime/static-omission-gate.json").write_bytes(altered_gate_bytes)
    bad_gate_file = deepcopy(receipt)
    bad_gate_file["artifacts"]["omission_gate"] = _artifact("runtime/static-omission-gate.json", altered_gate_bytes)
    with pytest.raises(validator.ValidationError, match="native injection differs"):
        validator.validate_raw_bundle(tmp_path, bad_gate_file)
    (tmp_path / "runtime/static-omission-gate.json").write_bytes(gate_file_bytes)
    bad_timing = deepcopy(receipt)
    timing_lines = payloads["replica-1.jsonl"].splitlines()
    late_injection = json.loads(timing_lines[1]); late_injection["source_monotonic_ns"] = 320
    timing_lines[1] = json.dumps(late_injection, sort_keys=True, separators=(",", ":")).encode()
    changed_timing = b"\n".join(timing_lines) + b"\n"
    (tmp_path / "replica-1.jsonl").write_bytes(changed_timing)
    bad_timing["artifacts"]["replica_streams"]["replica-1"] = _artifact("replica-1.jsonl", changed_timing)
    bad_timing["fault_injection_arm"]["line_sha256"] = hashlib.sha256(timing_lines[1]).hexdigest()
    with pytest.raises(validator.ValidationError, match="timeout attempts must begin after native injection"):
        validator.validate_raw_bundle(tmp_path, bad_timing)
    (tmp_path / "replica-1.jsonl").write_bytes(payloads["replica-1.jsonl"])
    bad_drop = deepcopy(receipt)
    drop_lines = payloads["replica-1.jsonl"].splitlines()
    altered = json.loads(drop_lines[2]); altered["payload"]["block_hash"] = "0" * 63 + "2"
    drop_lines[2] = json.dumps(altered, sort_keys=True, separators=(",", ":")).encode()
    changed = b"\n".join(drop_lines) + b"\n"; (tmp_path / "replica-1.jsonl").write_bytes(changed)
    bad_drop["artifacts"]["replica_streams"]["replica-1"] = _artifact("replica-1.jsonl", changed)
    with pytest.raises(validator.ValidationError, match="native omission"):
        validator.validate_raw_bundle(tmp_path, bad_drop)
    (tmp_path / "authorization.json").write_bytes(b"{}")
    with pytest.raises(validator.ValidationError, match="authorization receipt"):
        validator.validate_raw_bundle(tmp_path, receipt)
    (tmp_path / "authorization.json").write_bytes(authorization_bytes)
    (tmp_path / "runtime/n7-local-execution-plan.json").write_bytes(b"{}")
    with pytest.raises(validator.ValidationError, match="execution plan"):
        validator.validate_raw_bundle(tmp_path, receipt)
    (tmp_path / "runtime/n7-local-execution-plan.json").write_bytes(plan_bytes)
    (tmp_path / "cleanup.json").write_bytes(b"{}")
    with pytest.raises(validator.ValidationError, match="SHA-256"):
        validator.validate_raw_bundle(tmp_path, receipt)


def _replica_event(sequence: int, time_ns: int, event_type: str, payload: object) -> dict:
    return {"event_schema_version": 1, "run_id": "run-1", "source_kind": "replica", "source_id": "replica-1", "source_instance": "replica-instance-1", "source_sequence": sequence, "source_monotonic_ns": time_ns, "event_type": event_type, "payload": payload}
