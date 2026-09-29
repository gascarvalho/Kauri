from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "comparison" / "control_validator.py"
spec = importlib.util.spec_from_file_location("n7_control_validator_test", PATH)
assert spec and spec.loader
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)

PRODUCER_PATH = ROOT / "n7-path-timeout-quorum" / "comparison" / "control_producer.py"
producer_spec = importlib.util.spec_from_file_location("n7_control_producer_v2_raw_test", PRODUCER_PATH)
assert producer_spec and producer_spec.loader
producer = importlib.util.module_from_spec(producer_spec)
producer_spec.loader.exec_module(producer)


def _event(source_kind, source_id, sequence, timestamp, event_type, payload):
    return {"event_schema_version": 1, "run_id": "control-1", "source_kind": source_kind, "source_id": source_id, "source_instance": source_id + "-i", "source_sequence": sequence, "source_monotonic_ns": timestamp, "event_type": event_type, "payload": payload}


def _fixture():
    arm = _event("adaptation_manager", "adaptive-manager", 1, 100, "fault_window_armed", {"epoch_number": 0, "epoch_digest": "a" * 64})
    contract = {"schema_version": 2, "state": "PLANNING_ONLY_NO_LAUNCH", "run_id": "control-1", "manifest_sha256": "b" * 64, "epoch0": {"epoch_number": 0, "epoch_digest": "a" * 64, "tree_file_sha256": "c" * 64}, "fault_window_arm": {"source_sequence": 1, "event_sha256": control._event_digest(arm)}, "omission_gate_sha256": "f" * 64, "omission_context": {"tree_id": 4, "parent_replica": 4, "expected_message_type": "aggregate_relay"}, "designated_observer": 0, "horizon_ns": 60_000_000_000}
    streams = {}
    for replica in range(7):
        source = f"replica-{replica}"
        events = []
        if replica == 1:
            events.append(_event("replica", source, 1, 200, "fault.aggregate_omitted", {"actor": 1, "parent_replica": 4, "epoch_number": 0, "tree_id": 4, "epoch_digest": "a" * 64, "block_hash": "e" * 64, "gate_sha256": "f" * 64, "first_for_context": True}))
        if replica == 0:
            events.append(_event("replica", source, 1, 300, "block.committed", {"block_height": 9, "block_hash": "d" * 64, "parent_hash": None, "transaction_count": 1, "designated_observer": True, "decision_proof": {"epoch_number": 0, "tree_id": 0, "epoch_digest": "a" * 64, "block_hash": "d" * 64}, "view_generation": None, "commit_batch_index": 0}))
        seq = len(events) + 1
        events.append(_event("replica", source, seq, 301, "block.commit_observed", {"block_height": 9, "block_hash": "d" * 64, "parent_hash": None, "transaction_count": 1, "commit_batch_index": 0}))
        streams[source] = events
    return contract, [arm], streams


def test_control_validator_accepts_zero_or_more_e0_common_commits_after_first_drop():
    contract, manager, streams = _fixture()
    result = control.validate_control(contract, manager, streams)
    assert result["verdict"] == "CONTROL_PLANNING_ONLY_VALID"
    assert result["common_commit_count"] == 1
    assert result["maximum_inter_commit_gap_ns"] is None


def test_control_accepts_peer_witness_logged_before_designated_event_within_horizon():
    contract, manager, streams = _fixture()
    streams["replica-2"][0]["source_monotonic_ns"] = 250
    result = control.validate_control(contract, manager, streams)
    assert result["common_commit_count"] == 1


@pytest.mark.parametrize("mutation", ["conflicting_hash", "malformed_proof"])
def test_control_rejects_non_designated_native_commit_drift(mutation):
    contract, manager, streams = _fixture()
    payload = copy.deepcopy(streams["replica-0"][0]["payload"])
    payload["designated_observer"] = False
    if mutation == "conflicting_hash":
        payload["block_hash"] = "9" * 64
        payload["decision_proof"]["block_hash"] = "9" * 64
    else:
        payload["decision_proof"]["epoch_number"] = 1
    streams["replica-3"].append(
        _event("replica", "replica-3", 2, 302, "block.committed", payload)
    )
    with pytest.raises(control.ValidationError):
        control.validate_control(contract, manager, streams)


@pytest.mark.parametrize("field", (
    "omission_hash", "designated_hash", "proof_hash", "witness_hash",
    "designated_parent", "witness_parent", "proof_tree", "view_generation",
    "omission_actor", "omission_epoch", "omission_tree", "omission_parent",
    "proof_epoch", "arm_epoch", "context_parent",
))
def test_control_rejects_non_native_counted_identity_fields(field):
    contract, manager, streams = _fixture()
    omission = streams["replica-1"][0]["payload"]
    designated = streams["replica-0"][0]["payload"]
    witness = streams["replica-2"][0]["payload"]
    if field == "omission_hash":
        omission["block_hash"] = "not-a-native-hash"
    elif field == "designated_hash":
        designated["block_hash"] = "x"
        designated["decision_proof"]["block_hash"] = "x"
        for replica in range(7):
            streams[f"replica-{replica}"][-1]["payload"]["block_hash"] = "x"
    elif field == "proof_hash":
        designated["decision_proof"]["block_hash"] = "x"
    elif field == "witness_hash":
        witness["block_hash"] = "x"
    elif field == "designated_parent":
        designated["parent_hash"] = "x"
    elif field == "witness_parent":
        witness["parent_hash"] = "x"
    elif field == "proof_tree":
        designated["decision_proof"]["tree_id"] = True
    elif field == "view_generation":
        designated["view_generation"] = "1"
    elif field == "omission_actor":
        omission["actor"] = True
    elif field == "omission_epoch":
        omission["epoch_number"] = False
    elif field == "omission_tree":
        omission["tree_id"] = True
    elif field == "omission_parent":
        omission["parent_replica"] = True
    elif field == "proof_epoch":
        designated["decision_proof"]["epoch_number"] = False
    elif field == "arm_epoch":
        manager[0]["payload"]["epoch_number"] = False
    elif field == "context_parent":
        contract["omission_context"]["parent_replica"] = True
    with pytest.raises(control.ValidationError):
        control.validate_control(contract, manager, streams)


def test_control_accepts_later_same_context_omissions_but_never_anchors_on_them():
    contract, manager, streams = _fixture()
    first = streams["replica-1"][0]
    later = copy.deepcopy(first)
    later["source_sequence"] = 2
    later["source_monotonic_ns"] = 250
    later["payload"]["first_for_context"] = False
    streams["replica-1"].insert(1, later)
    streams["replica-1"][2]["source_sequence"] = 3
    result = control.validate_control(contract, manager, streams)
    assert result["anchor_monotonic_ns"] == 200
    assert producer.first_source_bound_physical_omission(
        {"replica-1": streams["replica-1"]}, "f" * 64
    )["source_monotonic_ns"] == 200

    first["payload"]["first_for_context"] = False
    with pytest.raises(control.ValidationError, match="later omission"):
        control.validate_control(contract, manager, streams)


def test_control_rejects_a_later_false_omission_without_an_identical_prior_true_context():
    contract, manager, streams = _fixture()
    later = copy.deepcopy(streams["replica-1"][0])
    later["source_sequence"] = 2
    later["source_monotonic_ns"] = 250
    later["payload"]["first_for_context"] = False
    later["payload"]["block_hash"] = "0" * 64
    streams["replica-1"].insert(1, later)
    streams["replica-1"][2]["source_sequence"] = 3
    with pytest.raises(control.ValidationError, match="later omission"):
        control.validate_control(contract, manager, streams)


def test_control_validator_measures_multiple_common_commits_in_fixed_horizon():
    contract, manager, streams = _fixture()
    second_hash = "1" * 64
    streams["replica-0"].append(_event("replica", "replica-0", len(streams["replica-0"]) + 1, 500, "block.committed", {"block_height": 10, "block_hash": second_hash, "parent_hash": "d" * 64, "transaction_count": 1, "designated_observer": True, "decision_proof": {"epoch_number": 0, "tree_id": 0, "epoch_digest": "a" * 64, "block_hash": second_hash}, "view_generation": None, "commit_batch_index": 1}))
    for replica in range(7):
        source = f"replica-{replica}"
        streams[source].append(_event("replica", source, len(streams[source]) + 1, 501, "block.commit_observed", {"block_height": 10, "block_hash": second_hash, "parent_hash": "d" * 64, "transaction_count": 1, "commit_batch_index": 1}))
    result = control.validate_control(contract, manager, streams)
    assert result["common_commit_count"] == 2
    assert [item["block_height"] for item in result["authoritative_common_commits"]] == [9, 10]
    assert result["maximum_inter_commit_gap_ns"] == 200


def _descriptor(root: Path, relative: str) -> dict[str, str]:
    payload = (root / relative).read_bytes()
    return {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}


def _v2_authority_fixture(monkeypatch: pytest.MonkeyPatch, root: Path, *, hard_timeout_seconds: int = 600):
    """Build no-launch, native-shaped base bytes plus a complete v2 authority chain."""
    for directory in ("runtime", "raw", "config"):
        (root / directory).mkdir()
    for replica in range(7):
        (root / f"runtime/replica-{replica}.effective.json").write_text(
            json.dumps({"authoritative_observer": "replica-2"}), encoding="utf-8"
        )
    (root / "config/hotstuff.gen.conf").write_text(
        "".join(f"replica = 127.0.0.1:{11000 + item};{12000 + item}, key, cert\n" for item in range(7)),
        encoding="utf-8",
    )
    tree = root / "config/epoch0.tree"
    tree.write_bytes(producer.adaptive.runner.TREE_FILE.read_bytes())
    app, manager_binary = root / "hotstuff-app", root / "adaptation-manager"
    for binary in (app, manager_binary):
        binary.write_text("synthetic native-shaped executable", encoding="utf-8")
        binary.chmod(0o700)
    transition = root / "runtime/transition-requests.json"
    transition.write_bytes(producer._canonical({"schema_version": 1, "requests": [{}]}))
    manager = (
        str(manager_binary), "--listen", "127.0.0.1:13000", "--structured-event-run-id", "control-1",
        "--structured-event-source-instance", "manager-instance", "--transition-request", "request-json",
        "--bundle-output", str(root / "successor.bundle"), "--epoch-zero-tree-file", str(tree),
        "--required-nonresponsive", "1",
    )
    replicas = [(str(app), str(item)) for item in range(7)]
    plan = {
        "schema_version": 1, "scenario": producer.adaptive.PROFILE_ID, "repository_revision": "d" * 40,
        "state": "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED",
        "e0_identity": {"epoch_digest": "a" * 64, "tree_file_sha256": hashlib.sha256(tree.read_bytes()).hexdigest(), "tree_file": "config/epoch0.tree"},
        "preflight": {"schema_version": 1, "scenario": producer.adaptive.PROFILE_ID, "relay_omission": {"replica_id": 1}, "fault_window_arm": {"timeout_evidence_basis": "exact_timeout_attempt_id_v1", "snapshot_evidence_basis": "exact_post_fault_path_timeout_quorum_v1", "physical_omission_causality_basis": "exact_matched_post_arm_physical_omission_v1"}},
        "main_config": "config/hotstuff.gen.conf", "runtime_artifacts": [{"kind": "transition_requests", "path": "runtime/transition-requests.json", "sha256": hashlib.sha256(transition.read_bytes()).hexdigest(), "replica_id": None}],
    }
    plan["plan_sha256"] = producer.adaptive.adapter._plan_digest(plan)
    (root / "local-launch-plan.json").write_bytes(
        (json.dumps(plan, sort_keys=True, indent=2) + "\n").encode("utf-8")
    )
    monkeypatch.setattr(producer.adaptive.adapter, "_verify_executable_local_plan", lambda *_args, **_kwargs: (manager, replicas))
    prepared = producer.prepare_no_successor_control(root, hard_timeout_seconds=hard_timeout_seconds)
    request_path = root / producer.CONTROL_AUTHORIZATION_REQUEST
    request = json.loads(request_path.read_bytes())
    approval = {"schema_version": 2, "kind": producer.AUTHORIZATION_KIND, "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(), "prepared_plan_sha256": request["prepared_plan_sha256"], "approval_reference": "test", "approved_utc": "2026-09-29T12:00:00Z", "no_retry": True}
    external = root.parent / "external-approval.json"
    external.write_bytes(producer._canonical(approval))
    producer.finalize_no_successor_control(root, external)
    monkeypatch.setattr(control, "_load_adaptive_runner", lambda: producer.adaptive)
    return json.loads((root / producer.CONTROL_PLAN).read_bytes())


def _v2_receipt(monkeypatch: pytest.MonkeyPatch, root: Path, *, tree_id: int = 4, hard_timeout_seconds: int = 600):
    prepared = _v2_authority_fixture(monkeypatch, root, hard_timeout_seconds=hard_timeout_seconds)
    contract, manager, streams = _fixture()
    contract["designated_observer"] = 2
    designated = streams["replica-0"].pop(0)
    designated["source_id"] = "replica-2"
    designated["source_instance"] = "replica-2-i"
    streams["replica-2"].insert(0, designated)
    streams["replica-0"][0]["source_sequence"] = 1
    streams["replica-2"][1]["source_sequence"] = 2
    manifest_bytes = (Path(control.__file__).resolve().parent / "matched_manifest.json").read_bytes()
    contract["manifest_sha256"] = json.loads(manifest_bytes)["manifest_sha256"]
    contract.update({"state": "FROZEN_EXECUTION_NO_SUCCESSOR", "run_id": prepared["run_id"], "epoch0": prepared["epoch0"], "omission_context": {"tree_id": tree_id, "parent_replica": tree_id, "expected_message_type": "aggregate_relay"}})
    manager[0]["run_id"] = prepared["run_id"]
    manager[0]["payload"]["epoch_digest"] = prepared["epoch0"]["epoch_digest"]
    (root / "runtime/fault-window-arm.json").write_bytes(producer._canonical({"run_id": prepared["run_id"], "epoch_number": 0, "epoch_digest": prepared["epoch0"]["epoch_digest"]}))
    arm_hash = _descriptor(root, "runtime/fault-window-arm.json")["sha256"]
    manager[0]["payload"]["fault_window_arm_sha256"] = arm_hash
    manager_line = json.dumps(manager[0], separators=(",", ":")).encode("utf-8")
    contract["fault_window_arm"]["event_sha256"] = hashlib.sha256(manager_line).hexdigest()
    (root / "runtime/static-omission-gate.json").write_bytes(producer._canonical({
        "schema_version": 1, "kind": "kauri-n7-static-aggregate-omission-gate-v1",
        "profile_sha256": "1" * 64, "tree_file_sha256": "2" * 64,
        "epoch_digest": prepared["epoch0"]["epoch_digest"], "replica_id": 1,
        "launch_argv_sha256": "3" * 64, "manager_run_id": prepared["run_id"],
        "manager_source_instance": "adaptive-manager-i", "manager_source_sequence": 1,
        "fault_window_arm_event_sha256": contract["fault_window_arm"]["event_sha256"],
        "activation_monotonic_ns": 140,
    }))
    gate_hash = _descriptor(root, "runtime/static-omission-gate.json")["sha256"]
    contract["omission_gate_sha256"] = gate_hash
    streams["replica-1"].insert(0, _event("replica", "replica-1", 1, 150, "fault.injection_armed", {
        "actor": 1, "gate_sha256": gate_hash,
        "manager_fault_window_arm_event_sha256": contract["fault_window_arm"]["event_sha256"],
        "profile_sha256": "1" * 64, "tree_file_sha256": "2" * 64,
        "launch_argv_sha256": "3" * 64, "activation_monotonic_ns": 140,
    }))
    for sequence, event in enumerate(streams["replica-1"], 1):
        event["source_sequence"] = sequence
    for events in streams.values():
        for event in events:
            event["run_id"] = prepared["run_id"]
            if isinstance(event["payload"], dict) and event["payload"].get("epoch_digest") == "a" * 64:
                event["payload"]["epoch_digest"] = prepared["epoch0"]["epoch_digest"]
    for event in streams["replica-1"]:
        if event["event_type"] == "fault.aggregate_omitted":
            event["payload"]["tree_id"] = tree_id
            event["payload"]["parent_replica"] = tree_id
            event["payload"]["gate_sha256"] = gate_hash
    first = next(event for event in streams["replica-1"] if event["event_type"] == "fault.aggregate_omitted")
    for other_tree in (4, 5, 6):
        if other_tree != tree_id:
            duplicate = copy.deepcopy(first)
            duplicate["source_monotonic_ns"] += other_tree
            duplicate["payload"]["tree_id"] = other_tree
            duplicate["payload"]["parent_replica"] = other_tree
            streams["replica-1"].append(duplicate)
    streams["replica-1"].sort(key=lambda event: event["source_monotonic_ns"])
    for sequence, event in enumerate(streams["replica-1"], 1):
        event["source_sequence"] = sequence
    for source, events in streams.items():
        events.append(_event("replica", source, len(events) + 1, 60_000_000_200, "process.horizon_coverage", {}))
    manager.append(_event("adaptation_manager", "adaptive-manager", 2, 60_000_000_200, "fixed_e0_control.observation", {"fault_window_arm_sha256": arm_hash}))
    manager.append(_event("adaptation_manager", "adaptive-manager", 3, 60_000_000_201, "adaptive_v2_session_terminal", {
        "cycle_ordinal": 0, "policy_intent": "fault_containment", "outcome": "no_op",
        "reason": "explicit_no_op", "transition_artifact_id": f"fixed-e0-control/{prepared['run_id']}",
        "predecessor_epoch_number": 0, "predecessor_epoch_digest": prepared["epoch0"]["epoch_digest"],
        "successor_epoch_number": None, "successor_epoch_digest": None,
        "command_payload_digest": None, "winning_activation": None, "controller_failure": None,
        "evidence_window_activation_generation": 1, "baseline_evidence_cutoff": 1,
        "current_evidence_cutoff": 1,
    }))
    (root / "runtime/cleanup-receipt.json").write_bytes(producer._canonical({
        "schema_version": 1, "run_id": prepared["run_id"], "complete": True,
        "processes": [
            {"source_id": source, "pid": 100 + index, "pgid": 200 + index,
             "returncode": 0, "termination": "clean-exit"}
            for index, source in enumerate(["adaptive-manager", *(f"replica-{i}" for i in range(7))])
        ],
    }))
    (root / "raw/adaptive-manager.jsonl").write_bytes(b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in manager))
    for source, events in streams.items():
        (root / f"raw/{source}.jsonl").write_bytes(b"".join(json.dumps(item, sort_keys=True, separators=(",", ":")).encode() + b"\n" for item in events))
    (root / "runtime/fixed-e0-control-matched-manifest.json").write_bytes(manifest_bytes)
    artifacts = {"manager_events": _descriptor(root, "raw/adaptive-manager.jsonl"), "replica_streams": {source: _descriptor(root, f"raw/{source}.jsonl") for source in streams}, "fault_window_arm": _descriptor(root, "runtime/fault-window-arm.json"), "omission_gate": _descriptor(root, "runtime/static-omission-gate.json"), "cleanup": _descriptor(root, "runtime/cleanup-receipt.json"), "manifest": _descriptor(root, "runtime/fixed-e0-control-matched-manifest.json"), "prepared_plan": _descriptor(root, str(producer.CONTROL_PLAN)), "authorization_request": _descriptor(root, str(producer.CONTROL_AUTHORIZATION_REQUEST)), "approved_authorization": _descriptor(root, str(producer.CONTROL_APPROVED_AUTHORIZATION)), "finalization_receipt": _descriptor(root, str(producer.CONTROL_FINALIZATION_RECEIPT)), "base_plan": _descriptor(root, "local-launch-plan.json"), "executables": prepared["executables"]}
    return {"schema_version": 2, "kind": "kauri-n7-fixed-e0-control-raw-bundle-v2", "contract": contract, "artifacts": artifacts}


def test_raw_bundle_v2_replays_the_full_prepared_authority_chain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    receipt = _v2_receipt(monkeypatch, tmp_path)
    result = control.validate_raw_bundle(tmp_path, receipt)
    assert result["verdict"] == "CONTROL_RAW_BUNDLE_VALIDATED_PROSPECTIVE"
    replica_one = (tmp_path / receipt["artifacts"]["replica_streams"]["replica-1"]["path"]).read_text(encoding="utf-8")
    assert replica_one.count('"event_type":"fault.aggregate_omitted"') == 3


def test_raw_bundle_v2_rejects_observer_config_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    receipt = _v2_receipt(monkeypatch, tmp_path)
    (tmp_path / "runtime/replica-3.effective.json").write_text(
        json.dumps({"authoritative_observer": "replica-0"}), encoding="utf-8"
    )
    with pytest.raises(control.ValidationError, match="observer differs"):
        control.validate_raw_bundle(tmp_path, receipt)


def test_raw_bundle_v2_requires_the_manifest_frozen_600_second_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    receipt = _v2_receipt(monkeypatch, tmp_path)
    replay = control._verify_v2_authority_chain
    def changed_timeout(root, artifacts):
        return {**replay(root, artifacts), "hard_timeout_seconds": 601}
    monkeypatch.setattr(control, "_verify_v2_authority_chain", changed_timeout)
    with pytest.raises(control.ValidationError, match="600-second"):
        control.validate_raw_bundle(tmp_path, receipt)


def test_control_producer_rejects_nonfrozen_timeout_before_preparation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    with pytest.raises(producer.ProducerError, match="600-second"):
        _v2_authority_fixture(monkeypatch, tmp_path, hard_timeout_seconds=601)


@pytest.mark.parametrize("mutation", ["manager_returncode", "manager_termination", "duplicate_process"])
def test_raw_bundle_v2_rejects_nonclean_or_ambiguous_cleanup_outcomes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str):
    receipt = _v2_receipt(monkeypatch, tmp_path)
    path = tmp_path / receipt["artifacts"]["cleanup"]["path"]
    cleanup = json.loads(path.read_bytes())
    if mutation == "manager_returncode":
        cleanup["processes"][0]["returncode"] = 1
        cleanup["processes"][0]["termination"] = "terminated"
    elif mutation == "manager_termination":
        cleanup["processes"][0]["termination"] = "terminated"
    else:
        cleanup["processes"].append(copy.deepcopy(cleanup["processes"][0]))
    path.write_bytes(producer._canonical(cleanup))
    receipt["artifacts"]["cleanup"] = _descriptor(tmp_path, "runtime/cleanup-receipt.json")
    with pytest.raises(control.ValidationError, match="cleanup"):
        control.validate_raw_bundle(tmp_path, receipt)


@pytest.mark.parametrize("mutation", ["missing", "wrong_outcome", "wrong_artifact"])
def test_raw_bundle_v2_requires_the_clean_fixed_e0_noop_terminal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str):
    receipt = _v2_receipt(monkeypatch, tmp_path)
    path = tmp_path / receipt["artifacts"]["manager_events"]["path"]
    events = [json.loads(line) for line in path.read_bytes().splitlines()]
    if mutation == "missing":
        events = events[:-1]
    elif mutation == "wrong_outcome":
        events[-1]["payload"]["outcome"] = "failed"
    elif mutation == "wrong_artifact":
        events[-1]["payload"]["transition_artifact_id"] = "e0-to-e1-containment"
    path.write_bytes(b"".join(json.dumps(event, separators=(",", ":")).encode() + b"\n" for event in events))
    receipt["artifacts"]["manager_events"] = _descriptor(tmp_path, "raw/adaptive-manager.jsonl")
    with pytest.raises(control.ValidationError, match="clean fixed-E0 no-op terminal"):
        control.validate_raw_bundle(tmp_path, receipt)


@pytest.mark.parametrize("mutation", ["before_gate", "at_anchor", "payload_activation_drift"])
def test_raw_bundle_v2_requires_gate_injection_anchor_chronology(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str):
    receipt = _v2_receipt(monkeypatch, tmp_path)
    path = tmp_path / receipt["artifacts"]["replica_streams"]["replica-1"]["path"]
    events = [json.loads(line) for line in path.read_bytes().splitlines()]
    injection = next(event for event in events if event["event_type"] == "fault.injection_armed")
    if mutation == "before_gate":
        injection["source_monotonic_ns"] = 139
    elif mutation == "at_anchor":
        injection["source_monotonic_ns"] = 200
    else:
        injection["payload"]["activation_monotonic_ns"] = 141
    path.write_bytes(b"".join(json.dumps(event, separators=(",", ":")).encode() + b"\n" for event in events))
    receipt["artifacts"]["replica_streams"]["replica-1"] = _descriptor(tmp_path, "raw/replica-1.jsonl")
    with pytest.raises(control.ValidationError, match="injection|monotonic"):
        control.validate_raw_bundle(tmp_path, receipt)


def test_raw_bundle_v2_rejects_native_jsonl_field_order_mutation_even_with_an_updated_file_hash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    receipt = _v2_receipt(monkeypatch, tmp_path)
    descriptor = receipt["artifacts"]["manager_events"]
    path = tmp_path / descriptor["path"]
    lines = path.read_bytes().splitlines()
    event = json.loads(lines[0])
    path.write_bytes(json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n" + b"\n".join(lines[1:]) + b"\n")
    descriptor["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(control.ValidationError, match="arm event"):
        control.validate_raw_bundle(tmp_path, receipt)


@pytest.mark.parametrize("artifact", ["omission_gate", "manifest"])
def test_raw_bundle_v2_rejects_gate_or_manifest_byte_mutation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, artifact: str):
    receipt = _v2_receipt(monkeypatch, tmp_path)
    descriptor = receipt["artifacts"][artifact]
    path = tmp_path / descriptor["path"]
    path.write_bytes(path.read_bytes() + b" ")
    descriptor["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(control.ValidationError):
        control.validate_raw_bundle(tmp_path, receipt)


@pytest.mark.parametrize("mutation", ["missing", "wrong_arm", "early"])
def test_raw_bundle_v2_requires_a_post_horizon_pinned_arm_observation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str):
    receipt = _v2_receipt(monkeypatch, tmp_path)
    descriptor = receipt["artifacts"]["manager_events"]
    path = tmp_path / descriptor["path"]
    events = [json.loads(line) for line in path.read_bytes().splitlines()]
    if mutation == "missing":
        events = events[:1]
    elif mutation == "wrong_arm":
        events[1]["payload"]["fault_window_arm_sha256"] = "0" * 64
    else:
        events[1]["source_monotonic_ns"] = 60_000_000_199
    path.write_bytes(b"".join(json.dumps(event, separators=(",", ":")).encode() + b"\n" for event in events))
    descriptor["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(control.ValidationError, match="post-horizon pinned-arm observation"):
        control.validate_raw_bundle(tmp_path, receipt)


@pytest.mark.parametrize("tree_id", [4, 5, 6])
def test_raw_bundle_v2_accepts_each_admitted_t4_t5_t6_omission_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tree_id: int):
    receipt = _v2_receipt(monkeypatch, tmp_path, tree_id=tree_id)
    assert control.validate_raw_bundle(tmp_path, receipt)["verdict"] == "CONTROL_RAW_BUNDLE_VALIDATED_PROSPECTIVE"


@pytest.mark.parametrize("tree_id", [3, 7])
def test_raw_bundle_v2_rejects_non_admitted_omission_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tree_id: int):
    receipt = _v2_receipt(monkeypatch, tmp_path, tree_id=tree_id)
    with pytest.raises(control.ValidationError, match="omission context"):
        control.validate_raw_bundle(tmp_path, receipt)


@pytest.mark.parametrize("artifact", ["base_plan", "authorization_request", "approved_authorization", "finalization_receipt"])
def test_raw_bundle_v2_rejects_authority_artifact_mutation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, artifact: str):
    receipt = _v2_receipt(monkeypatch, tmp_path)
    path = tmp_path / receipt["artifacts"][artifact]["path"]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(control.ValidationError):
        control.validate_raw_bundle(tmp_path, receipt)


def test_raw_bundle_v2_rejects_executable_mutation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    receipt = _v2_receipt(monkeypatch, tmp_path)
    app = Path(receipt["artifacts"]["executables"]["hotstuff_app"]["path"])
    app.write_text("mutated executable", encoding="utf-8")
    with pytest.raises(control.ValidationError):
        control.validate_raw_bundle(tmp_path, receipt)


@pytest.mark.parametrize("source", [f"replica-{item}" for item in range(7)])
def test_raw_bundle_v2_rejects_each_replica_stream_truncated_before_the_60s_horizon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str):
    receipt = _v2_receipt(monkeypatch, tmp_path)
    descriptor = receipt["artifacts"]["replica_streams"][source]
    path = tmp_path / descriptor["path"]
    path.write_bytes(b"\n".join(path.read_bytes().splitlines()[:-1]) + b"\n")
    descriptor["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(control.ValidationError, match="cover first omission plus 60s"):
        control.validate_raw_bundle(tmp_path, receipt)


def test_raw_bundle_rejects_a_hand_authored_v1_plan_even_when_stream_bytes_match(tmp_path: Path):
    contract, manager, streams = _fixture()
    contract["state"] = "FROZEN_EXECUTION_NO_SUCCESSOR"
    raw = tmp_path / "raw"
    runtime = tmp_path / "runtime"
    raw.mkdir()
    runtime.mkdir()
    (runtime / "fault-window-arm.json").write_text(
        json.dumps({"run_id": "control-1", "epoch_number": 0, "epoch_digest": "a" * 64}) + "\n", encoding="utf-8"
    )
    arm_hash = _descriptor(tmp_path, "runtime/fault-window-arm.json")["sha256"]
    manager[0]["payload"]["fault_window_arm_sha256"] = arm_hash
    contract["fault_window_arm"]["event_sha256"] = control._event_digest(manager[0])
    (runtime / "static-omission-gate.json").write_text(
        json.dumps({"manager_run_id": "control-1", "epoch_digest": "a" * 64,
                    "replica_id": 1, "fault_window_arm_event_sha256": contract["fault_window_arm"]["event_sha256"]}) + "\n",
        encoding="utf-8",
    )
    gate_hash = _descriptor(tmp_path, "runtime/static-omission-gate.json")["sha256"]
    (runtime / "cleanup-receipt.json").write_text(
        json.dumps({"run_id": "control-1", "complete": True,
                    "processes": [{"source_id": source} for source in ["adaptive-manager", *(f"replica-{i}" for i in range(7))]]}) + "\n",
        encoding="utf-8",
    )
    (runtime / "fixed-e0-control-plan.json").write_text(
        json.dumps({"kind": "kauri-n7-fixed-e0-control-plan-v1", "run_id": "control-1",
                    "state": "FINALIZED_NATIVE_CONTROL_EXECUTION",
                    "epoch0": contract["epoch0"],
                    "manager_command": ["manager", "--fault-window-arm-control-only"]}) + "\n",
        encoding="utf-8",
    )
    streams["replica-1"].insert(0, _event(
        "replica", "replica-1", 1, 150, "fault.injection_armed",
        {"gate_sha256": gate_hash},
    ))
    for sequence, event in enumerate(streams["replica-1"], 1):
        event["source_sequence"] = sequence
    (raw / "adaptive-manager.jsonl").write_bytes(
        b"".join(json.dumps(item, sort_keys=True, separators=(",", ":")).encode() + b"\n" for item in manager)
    )
    for source, events in streams.items():
        (raw / f"{source}.jsonl").write_bytes(
            b"".join(json.dumps(item, sort_keys=True, separators=(",", ":")).encode() + b"\n" for item in events)
        )
    receipt = {
        "schema_version": 1,
        "kind": "kauri-n7-fixed-e0-control-raw-bundle-v1",
        "contract": contract,
        "artifacts": {
            "manager_events": _descriptor(tmp_path, "raw/adaptive-manager.jsonl"),
            "replica_streams": {
                source: _descriptor(tmp_path, f"raw/{source}.jsonl") for source in streams
            },
            "fault_window_arm": _descriptor(tmp_path, "runtime/fault-window-arm.json"),
            "omission_gate": _descriptor(tmp_path, "runtime/static-omission-gate.json"),
            "cleanup": _descriptor(tmp_path, "runtime/cleanup-receipt.json"),
            "control_plan": _descriptor(tmp_path, "runtime/fixed-e0-control-plan.json"),
        },
    }
    with pytest.raises(control.ValidationError, match="schema drift"):
        control.validate_raw_bundle(tmp_path, receipt)
    (raw / "replica-0.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(control.ValidationError):
        control.validate_raw_bundle(tmp_path, receipt)


def test_control_validator_retains_a_zero_common_commit_horizon():
    contract, manager, streams = _fixture()
    for replica in range(7):
        if replica != 1:
            streams[f"replica-{replica}"] = []
    result = control.validate_control(contract, manager, streams)
    assert result["common_commit_count"] == 0
    assert result["authoritative_common_commits"] == []
    assert result["maximum_inter_commit_gap_ns"] is None


@pytest.mark.parametrize("mutation", ["selection", "e1", "no_drop", "missing_witness", "wrong_arm", "early_witness", "conflicting_height", "duplicate_designated", "duplicate_witness", "wrong_context", "time_regression"])
def test_control_validator_fails_closed_on_adaptive_or_anchor_drift(mutation):
    contract, manager, streams = _fixture()
    if mutation == "selection":
        manager.append(_event("adaptation_manager", "adaptive-manager", 2, 101, "adaptive_v2.selection_decided", {}))
    elif mutation == "e1":
        streams["replica-0"].append(_event("replica", "replica-0", 2, 302, "epoch.activated", {}))
    elif mutation == "no_drop":
        streams["replica-1"] = streams["replica-1"][1:]
    elif mutation == "missing_witness":
        streams["replica-6"] = []
    elif mutation == "wrong_arm":
        contract["omission_gate_sha256"] = "e" * 64
    elif mutation == "early_witness":
        streams["replica-2"][0]["source_monotonic_ns"] = 150
    elif mutation == "conflicting_height":
        streams["replica-2"][0]["payload"]["block_hash"] = "e" * 64
    elif mutation == "duplicate_designated":
        duplicate = copy.deepcopy(streams["replica-0"][0])
        duplicate["source_sequence"] = 2
        duplicate["source_monotonic_ns"] = 302
        streams["replica-0"].append(duplicate)
    elif mutation == "duplicate_witness":
        duplicate = copy.deepcopy(streams["replica-2"][0])
        duplicate["source_sequence"] = 2
        duplicate["source_monotonic_ns"] = 302
        streams["replica-2"].append(duplicate)
    elif mutation == "wrong_context":
        streams["replica-1"][0]["payload"]["tree_id"] = 5
    else:
        repeated = copy.deepcopy(streams["replica-2"][0])
        repeated["source_sequence"] = 2
        repeated["source_monotonic_ns"] = 300
        streams["replica-2"].append(repeated)
    with pytest.raises(control.ValidationError):
        control.validate_control(contract, manager, streams)
