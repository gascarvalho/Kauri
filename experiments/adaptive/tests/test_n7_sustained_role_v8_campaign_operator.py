from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "n7-path-timeout-quorum" / "sustained_role_v8_campaign_operator.py"
SPEC = importlib.util.spec_from_file_location("n7_sustained_role_v8_campaign_operator", MODULE)
assert SPEC and SPEC.loader
subject = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = subject
SPEC.loader.exec_module(subject)

RUN = "run-1"
E0 = "a" * 64
BLOCK = "d" * 64
PARENT = "f" * 64
E1_BLOCK = "e" * 64
ANCHOR = 1_000_000_000
START = 1
END = 83_000_000_001


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii") + b"\n"


def _write(root: Path, relative: str, body: bytes) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return {"path": relative, "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest()}


def _event(source: str, sequence: int, monotonic_ns: int,
           event_type: str, payload: object) -> dict[str, object]:
    return {
        "event_schema_version": 1, "run_id": RUN,
        "source_kind": "adaptation_manager" if source == "adaptive-manager" else "replica",
        "source_id": source, "source_instance": f"{RUN}-{source}",
        "source_sequence": sequence, "source_monotonic_ns": monotonic_ns,
        "event_type": event_type, "payload": payload,
    }


def _opportunity(epoch: int, tree: int, digest: str, role: str,
                 action: str, decision_ns: int) -> dict[str, object]:
    return {
        "actor": 1,
        "proposal": {"epoch_number": epoch, "tree_id": tree,
                     "epoch_digest": digest, "block_hash": "9" * 64},
        "physical_role": role, "parent_replica": tree,
        "authenticated_proposal_source_replica": tree,
        "expected_message_type": "aggregate_relay" if role == "internal" else "direct_vote",
        "cohort": "hard", "diagnostic_window": "w19-v8",
        "window_start_monotonic_ns": decision_ns - 1,
        "window_end_monotonic_ns": decision_ns + 1,
        "decision_monotonic_ns": decision_ns, "contribution_ordinal": 0,
        "role_contribution_ordinal": 0, "scheduled_action": action,
        "responsive_omission_period": 0, "fault_threshold": 2,
        "hard_actor_count": 1, "responsive_degraded_actor_count": 0,
        "fault_mode": "role_scoped_persistent_selected_omission_v1", "view_generation": 0,
    }


def _marker(payload: dict[str, object]) -> bytes:
    proposal = payload["proposal"]
    assert isinstance(proposal, dict)
    fields = {
        "fault": payload["fault_mode"], "proposal_epoch": proposal["epoch_number"],
        "proposal_tree": proposal["tree_id"], "proposal_epoch_digest": proposal["epoch_digest"],
        "proposal_block_hash": proposal["block_hash"], "window": payload["diagnostic_window"],
        "window_start_monotonic_ns": payload["window_start_monotonic_ns"],
        "window_end_monotonic_ns": payload["window_end_monotonic_ns"],
        "actor": payload["actor"], "action": payload["scheduled_action"],
        "monotonic_ns": payload["decision_monotonic_ns"], "cohort": payload["cohort"],
        "hard_actor_count": payload["hard_actor_count"],
        "responsive_degraded_actor_count": payload["responsive_degraded_actor_count"],
        "fault_threshold": payload["fault_threshold"], "max_omissions_per_proposal": 1,
        "responsive_omission_period": payload["responsive_omission_period"],
        "contribution_ordinal": payload["contribution_ordinal"],
        "contribution_role": payload["physical_role"],
        "role_contribution_ordinal": payload["role_contribution_ordinal"],
        "authenticated_proposal_source_replica": payload["authenticated_proposal_source_replica"],
    }
    return ("KAURI_FAULT " + " ".join(f"{key}={value}" for key, value in fields.items()) + "\n").encode()


def _u(value: int, size: int) -> bytes:
    return value.to_bytes(size, "big")


def _component(value: bytes) -> bytes:
    return _u(len(value), 4) + value


def _signed_bundle() -> tuple[bytes, object, str]:
    """Encode the real native v2 wire and verify it with the production decoder."""
    inserted = str(ROOT) not in sys.path
    if inserted:
        sys.path.insert(0, str(ROOT))
    try:
        fv = __import__("kauri_experiment.factorial_validation", fromlist=["decode_epoch_change_bundle"])
    finally:
        if inserted:
            sys.path.remove(str(ROOT))
    membership_digest = hashlib.sha256(
        b"kauri-membership-v1" + _u(7, 4) + b"".join(_u(item, 2) for item in range(7))
    ).hexdigest()
    canonical = bytearray(b"kauri-epoch-definition-v2")
    canonical += _u(2, 4) + _u(1, 4) + bytes.fromhex(E0)
    canonical += bytes.fromhex(membership_digest) + _u(41719, 8)
    canonical += _component(b"operator-capacity-v1") + _component(b"c" * 64) + _u(6, 8) + _u(5, 4)
    members = (0, 2, 3, 1, 4, 5, 6)
    for tree_id in range(5):
        canonical += _u(tree_id, 4) + _u(2, 4) + _u(2, 4) + _u(7, 4)
        canonical += b"".join(_u(member, 2) for member in members)
        canonical += _u(1, 4) + _u(1, 2)
    successor = hashlib.sha256(canonical).hexdigest()
    signed = (b"kauri-authorized-epoch-change-v1" + _u(1, 4) + _u(2, 1) +
              _u(1, 4) + _u(1, 4) + bytes.fromhex(E0) + bytes.fromhex(successor) + _u(5, 8))
    point = fv._secp256k1_multiply(2, (fv._SECP256K1_GX, fv._SECP256K1_GY))
    assert point is not None
    order = fv._SECP256K1_ORDER
    r = point[0] % order
    s = (pow(2, -1, order) * (int.from_bytes(hashlib.sha256(signed).digest(), "big") + r)) % order
    if s > order // 2:
        s = order - s
    command = signed + r.to_bytes(32, "big") + s.to_bytes(32, "big")
    definition = (_u(2, 4) + _u(2, 1) + _u(6, 1) + bytes.fromhex(successor) +
                  bytes(canonical)[len(b"kauri-epoch-definition-v2"):])
    wire = (b"kauri-adaptive-v2-epoch-change-bundle-v1" + _u(1, 4) + _u(2, 1) +
            _component(command) + _component(definition))
    issuer = "02" + f"{fv._SECP256K1_GX:064x}"
    return wire, fv.decode_epoch_change_bundle(wire, issuer_public_key=issuer), issuer


def _identity(bundle: object) -> dict[str, object]:
    return {"predecessor_epoch_number": 0, "predecessor_epoch_digest": E0,
            "successor_epoch_number": 1, "successor_epoch_digest": bundle.epoch_digest,
            "command_payload_digest": bundle.command.payload_digest,
            "evidence_snapshot_id": bundle.evidence_snapshot_id,
            "baseline_evidence_cutoff": 1, "evidence_cutoff": bundle.evidence_cutoff}


def _command(identity: dict[str, object]) -> dict[str, object]:
    return {"command_block_height": 10, "command_block_hash": BLOCK,
            "payload_digest": identity["command_payload_digest"],
            "predecessor_epoch_number": 0, "predecessor_epoch_digest": E0,
            "successor_epoch_number": 1, "successor_epoch_digest": identity["successor_epoch_digest"],
            "activation_delay_blocks": 5, "activation_height": 15}


def _command_identity(identity: dict[str, object]) -> dict[str, object]:
    value = _command(identity)
    value["command_payload_digest"] = value.pop("payload_digest")
    return value


def _manager_events(identity: dict[str, object], bundle_wire: bytes) -> list[dict[str, object]]:
    command_identity = _command_identity(identity)
    return [
        _event("adaptive-manager", 1, ANCHOR + 10, "adaptive_v2.selection_decided", {
            "schema_version": 1, "cycle_ordinal": 0, "predecessor_epoch_number": 0,
            "predecessor_epoch_digest": E0, "baseline_cutoff": 1,
            "evidence_cutoff": identity["evidence_cutoff"],
            "evidence_snapshot_id": identity["evidence_snapshot_id"],
            "snapshot_evidence_basis": "exact_post_fault_path_timeout_quorum_v1",
            "selection_cardinality_policy": "all_guarded_up_to_fault_bound_v1",
            "selected_replicas": [1]}),
        _event("adaptive-manager", 2, ANCHOR + 20, "adaptive_v2.convergence_started",
               {"cycle_ordinal": 0, **identity}),
        _event("adaptive-manager", 3, ANCHOR + 30, "adaptive_v2_delivery_attempt", {
            "replica_id": 0, "delivery_attempt": 1, "disposition": "enqueued",
            "canonical_payload_digest": hashlib.sha256(bundle_wire).hexdigest(), "identity": None}),
        _event("adaptive-manager", 4, ANCHOR + 40, "adaptive_v2_ready", {
            "replica_id": None, "delivery_attempt": None, "disposition": None,
            "identity": command_identity, "accepted_commit_count": 5,
            "accepted_activation_count": 5, "required_activation_count": 5,
            "canonical_payload_digest": None, "failure_reason": None}),
        _event("adaptive-manager", 5, ANCHOR + 50, "adaptive_v2_session_terminal", {
            "cycle_ordinal": 0, "policy_intent": "fault_containment", "outcome": "advanced",
            "reason": "successor_converged", "transition_artifact_id": "e0-to-e1-containment",
            "predecessor_epoch_number": 0, "predecessor_epoch_digest": E0,
            "successor_epoch_number": 1, "successor_epoch_digest": identity["successor_epoch_digest"],
            "command_payload_digest": identity["command_payload_digest"],
            "winning_activation": command_identity, "controller_failure": None,
            "evidence_window_activation_generation": 1,
            "baseline_evidence_cutoff": 1, "current_evidence_cutoff": identity["evidence_cutoff"]}),
        _event("adaptive-manager", 6, ANCHOR + 72_000_000_000,
               "process.lifecycle", {"exit_status": 0}),
    ]


def _replica_events(replica: int, identity: dict[str, object],
                    anchor_payload: dict[str, object], late_payload: dict[str, object]) -> list[dict[str, object]]:
    rows: list[tuple[int, str, object]] = []
    if replica == 1:
        rows.append((ANCHOR, "fault.contribution_opportunity", anchor_payload))
    authority = {"block_height": 10, "block_hash": BLOCK, "parent_hash": PARENT,
                 "transaction_count": 1, "designated_observer": True,
                 "decision_proof": {"epoch_number": 0, "tree_id": 0, "epoch_digest": E0,
                                    "block_hash": BLOCK},
                 "view_generation": 0, "commit_batch_index": 0}
    if replica == 2:
        rows.append((ANCHOR + 40, "block.committed", authority))
    rows.append((ANCHOR + 40, "block.commit_observed", {
        "block_height": 10, "block_hash": BLOCK, "parent_hash": PARENT,
        "transaction_count": 1, "commit_batch_index": replica}))
    rows.append((ANCHOR + 50, "epoch.command_committed", _command(identity)))
    rows.append((ANCHOR + 31_000_000_000, "epoch.activated", {
        "epoch_number": 1, "tree_id": 0, "epoch_digest": identity["successor_epoch_digest"],
        "activation_height": 15}))
    if replica == 2:
        rows.append((ANCHOR + 32_000_000_001, "block.committed", {
            "block_height": 16, "block_hash": E1_BLOCK, "parent_hash": PARENT,
            "transaction_count": 1, "designated_observer": True,
            "decision_proof": {"epoch_number": 1, "tree_id": 0,
                               "epoch_digest": identity["successor_epoch_digest"],
                               "block_hash": E1_BLOCK},
            "view_generation": 1, "commit_batch_index": 0}))
    rows.append((ANCHOR + 32_000_000_002, "block.commit_observed", {
        "block_height": 16, "block_hash": E1_BLOCK, "parent_hash": PARENT,
        "transaction_count": 1, "commit_batch_index": replica}))
    if replica == 1:
        rows.append((ANCHOR + 40_000_000_000, "fault.contribution_opportunity", late_payload))
    rows.append((ANCHOR + 72_000_000_000, "process.lifecycle", {"exit_status": 0}))
    return [_event(f"replica-{replica}", sequence, timestamp, event_type, payload)
            for sequence, (timestamp, event_type, payload) in enumerate(rows, 1)]


def _write_receipt(root: Path, receipt: dict[str, object]) -> str:
    raw = _canonical(receipt)
    (root / "runtime").mkdir(exist_ok=True)
    (root / "runtime/receipt.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _root(tmp_path: Path) -> tuple[Path, dict[str, object], str]:
    root = tmp_path / "v8-cell"
    root.mkdir()
    wire, bundle, issuer = _signed_bundle()
    identity = _identity(bundle)
    scalar = {name: _write(root, f"inputs/{name}", (name + "\n").encode())
              for name in ("build_receipt", "launch_arguments", "preparation", "e0_identity",
                           "v8_profile", "selection_profile", "main_config")}
    scalar["epoch0_tree"] = _write(
        root, "inputs/epoch0.tree", (ROOT / "n7-path-timeout-quorum/epoch0.tree").read_bytes())
    scalar["issuer_public_key"] = _write(root, "inputs/issuer-public-key.txt",
                                          (issuer + "\n").encode("ascii"))
    executables = {name: _write(root, f"inputs/{name}", (name + "\n").encode())
                   for name in ("hotstuff_app", "adaptation_manager", "hotstuff_keygen",
                                "hotstuff_tls_keygen", "e0_helper")}
    replica_configs = [_write(root, f"inputs/replica-{replica}.conf", f"replica={replica}\n".encode())
                       for replica in range(7)]
    plan_artifacts = {**scalar, **executables, "replica_configs": replica_configs}
    processes = [{"source_kind": "adaptation_manager" if source == "adaptive-manager" else "replica",
                  "source_id": source, "source_instance": f"{RUN}-{source}", "argv": [f"/{source}"],
                  "argv_sha256": hashlib.sha256(_canonical([f"/{source}"])).hexdigest()}
                 for source in subject.SOURCES]
    plan = {"schema_version": 1, "kind": "kauri-n7-sustained-role-v8-full-input-plan-v1",
            "state": "PREPARED_INPUTS_NO_LAUNCH", "run_id": RUN,
            "repository_revision": "b" * 40, "profile_id": subject.PROFILE_ID,
            "profile_sha256": scalar["v8_profile"]["sha256"], "hard_timeout_seconds": 210,
            "scheduled_window": {"clock": "CLOCK_MONOTONIC_RAW", "start_ns": START,
                                 "end_ns": END, "minimum_duration_ns": 82_000_000_000},
            "processes": processes, "artifacts": plan_artifacts, "no_retry": True,
            "claim_eligible": False, "figure_eligible": False,
            "build_provenance": "ARCHIVED_NOT_LIVE_ATTESTED"}
    plan["plan_sha256"] = hashlib.sha256(_canonical(plan)).hexdigest()
    plan_descriptor = _write(root, subject.PLAN_PATH, _canonical(plan))
    request = {"schema_version": 1, "kind": "kauri-n7-sustained-role-v8-full-input-request-v1",
               "plan_sha256": plan["plan_sha256"], "run_id": RUN, "no_launch": True, "no_retry": True}
    request_raw = _canonical(request)
    request_descriptor = _write(root, subject.REQUEST_PATH, request_raw)
    approval = {"schema_version": 1,
                "kind": "kauri-n7-sustained-role-v8-adaptive-child-launch-authorization-v1",
                "request_sha256": hashlib.sha256(request_raw).hexdigest(),
                "plan_sha256": plan["plan_sha256"], "run_id": RUN, "no_retry": True}
    approval_raw = _canonical(approval)
    approval_descriptor = _write(root, subject.APPROVAL_PATH, approval_raw)
    binary_map = {"hotstuff_app": "hotstuff_app", "adaptation_manager": "adaptation_manager",
                  "hotstuff_keygen": "hotstuff_keygen", "hotstuff_tls_keygen": "hotstuff_tls_keygen",
                  "epoch0_treefile_digest": "e0_helper"}
    build = {"kind": "kauri-n7-sustained-role-v8-build-binding-v1",
             "state": "BUILD_COMPONENT_VERIFIED_NOT_LIVE_ATTESTED",
             "repository_revision": "b" * 40, "target_host": "proteina02",
             "linux_boot_id": "12345678-1234-1234-1234-123456789abc",
             "receipt_sha256": scalar["build_receipt"]["sha256"],
             "binaries": {receipt: executables[artifact] for receipt, artifact in binary_map.items()},
             "live_attested": False, "launch_eligible": False}
    intent = {"schema_version": 1,
              "kind": "kauri-n7-sustained-role-v8-adaptive-child-launch-intent-v1",
              "state": "PRESPAWN_INTENT_SEALED_NO_LAUNCH", "plan_sha256": plan["plan_sha256"],
              "request_sha256": hashlib.sha256(request_raw).hexdigest(),
              "approval_sha256": hashlib.sha256(approval_raw).hexdigest(), "build_binding": build,
              "run_id": RUN, "no_retry": True, "hard_scope_seconds": 210,
              "claim_eligible": False, "figure_eligible": False}
    intent["intent_sha256"] = hashlib.sha256(_canonical(intent)).hexdigest()
    intent_descriptor = _write(root, subject.INTENT_PATH, _canonical(intent))
    anchor_payload = _opportunity(0, 4, E0, "internal", "omit_aggregate", ANCHOR)
    late_payload = _opportunity(1, 0, bundle.epoch_digest, "leaf", "omit_direct_vote",
                                ANCHOR + 40_000_000_000)
    events = {"adaptive-manager": _manager_events(identity, wire)}
    events.update({f"replica-{replica}": _replica_events(replica, identity, anchor_payload, late_payload)
                   for replica in range(7)})
    raw = {source: _write(root, f"raw/{source}.jsonl",
                          b"".join(_canonical(event) for event in events[source]))
           for source in subject.SOURCES}
    logs = {source: _write(root, f"logs/{source}.log", b"no-fault\n") for source in subject.SOURCES}
    logs["replica-1"] = _write(root, "logs/replica-1.log",
                                _marker(anchor_payload) + _marker(late_payload))
    replay = subject.validator_v8.validate_v8_raw_contract(
        anchor_monotonic_ns=ANCHOR, expected_identity=identity,
        manager_events=events["adaptive-manager"],
        replica_events={replica: events[f"replica-{replica}"] for replica in range(7)},
        bundle_bytes=wire, issuer_public_key=issuer, predecessor_tree_ids=frozenset(range(7)))
    validator = _write(root, "runtime/validator.json", _canonical(replay))
    cleanup = {"schema_version": 1, "run_id": RUN, "complete": True,
               "processes": [{"source_id": source, "pid": index + 10, "pgid": index + 20,
                               "returncode": 0, "termination": "clean-exit"}
                              for index, source in enumerate(subject.SOURCES)]}
    cleanup_descriptor = _write(root, "runtime/cleanup-receipt.json", _canonical(cleanup))
    anchor_line = _canonical(events["replica-1"][0]).rstrip(b"\n")
    provenance = {name: scalar[name] for name in (
        "build_receipt", "launch_arguments", "preparation", "e0_identity", "v8_profile",
        "selection_profile", "epoch0_tree", "main_config")}
    provenance.update({"replica_configs": replica_configs, "executables": executables})
    receipt = {"schema_version": 1, "kind": subject.ARM_RECEIPT_KIND, "run_id": RUN,
               "profile": {"id": subject.PROFILE_ID, "sha256": scalar["v8_profile"]["sha256"]},
               "repository_revision": "b" * 40, "arm": "adaptive_e1", "no_retry": True,
               "claim_eligible": False, "figure_eligible": False,
               "plan": plan_descriptor, "request": request_descriptor,
               "external_authorization": approval_descriptor, "launch_intent": intent_descriptor,
               "artifacts": {"bundle": _write(root, "runtime/successor.bundle", wire)},
               "anchor": {"source_id": "replica-1", "source_sequence": 1,
                          "line_sha256": hashlib.sha256(anchor_line).hexdigest(), "monotonic_ns": ANCHOR},
               "fault_window": {"start_monotonic_ns": START, "end_monotonic_ns": END,
                                "coverage_through_horizon": True},
               "provenance": provenance, "raw": raw, "logs": logs, "cleanup": cleanup_descriptor,
               "validator": {**validator, "profile_id": subject.PROFILE_ID}}
    _write(root, subject.COMMON_TERMINAL, _canonical({
        "schema_version": 1, "kind": "kauri-n7-sustained-role-v8-pilot-terminal-v1",
        "state": "SEALED_EXPLORATORY_NO_CLAIM", "run_id": RUN, "no_retry": True,
        "claim_eligible": False, "figure_eligible": False,
    }))
    return root, receipt, _write_receipt(root, receipt)


@pytest.fixture(autouse=True)
def _accept_upstream_full_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject.full_input, "verify", lambda *_args, **_kwargs: {"plan_sha256": "ok"})


def test_component_replay_joins_native_signed_e1_and_equal_window(tmp_path: Path) -> None:
    root, receipt, _digest = _root(tmp_path)
    replay = subject._component_replay(root, receipt)
    assert replay["verdict"] == "PASS_COMPONENT_ONLY_NO_CLAIM"
    assert replay["common_e1_committed_blocks"] == 1
    assert replay["measurement_window"] == [ANCHOR + 32_000_000_000, ANCHOR + 72_000_000_000]
    assert replay["lifecycle_bound"] is False and replay["post_scope_bound"] is False


def test_public_operator_runs_component_replay_but_never_issues_a_verdict(tmp_path: Path) -> None:
    root, _receipt, digest = _root(tmp_path)
    with pytest.raises(subject.V8SlotError, match="native lifecycle, and post-scope proof"):
        subject.verify_sealed_slot(root, receipt_relative="runtime/receipt.json",
                                   expected_receipt_sha256=digest)
    with pytest.raises(TypeError):
        subject.verify_sealed_slot(root, receipt_relative="runtime/receipt.json",
                                   expected_receipt_sha256=digest, replay=lambda *_: {"forged": True})


@pytest.mark.parametrize(("mutation", "error"), [
    ("source_instance", "source instance"), ("late_digest", "active signed E1"),
    ("cleanup", "process closure"), ("validator", "immediate fixed replay"),
    ("fifth_binary", "five-binary"), ("zero_command", "no all-seven common"),
    ("multi_command", "one-command maximum"),
])
def test_component_replay_rejects_cross_layer_mutations(
    tmp_path: Path, mutation: str, error: str,
) -> None:
    root, receipt, _digest = _root(tmp_path)
    if mutation == "source_instance":
        path = root / receipt["raw"]["replica-6"]["path"]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            row["source_instance"] = "forged-instance"
        receipt["raw"]["replica-6"] = _write(root, "raw/replica-6.jsonl",
                                                b"".join(_canonical(row) for row in rows))
    elif mutation == "late_digest":
        path = root / receipt["raw"]["replica-1"]["path"]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        faults = [row for row in rows if row["event_type"] == "fault.contribution_opportunity"]
        faults[1]["payload"]["proposal"]["epoch_digest"] = "8" * 64
        receipt["raw"]["replica-1"] = _write(root, "raw/replica-1.jsonl",
                                                b"".join(_canonical(row) for row in rows))
        receipt["logs"]["replica-1"] = _write(root, "logs/replica-1.log",
                                                 _marker(faults[0]["payload"]) + _marker(faults[1]["payload"]))
    elif mutation == "cleanup":
        value = json.loads((root / receipt["cleanup"]["path"]).read_text())
        value["processes"][-1]["source_id"] = "replica-5"
        receipt["cleanup"] = _write(root, "runtime/cleanup-receipt.json", _canonical(value))
    elif mutation == "validator":
        receipt["validator"] = {**_write(root, "runtime/validator.json", _canonical({"forged": True})),
                                "profile_id": subject.PROFILE_ID}
    elif mutation in {"zero_command", "multi_command"}:
        count = 0 if mutation == "zero_command" else 2
        for source in subject.SOURCES[1:]:
            path = root / receipt["raw"][source]["path"]
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            for row in rows:
                if (row["event_type"] in {"block.committed", "block.commit_observed"} and
                        row["payload"].get("block_height") == 16):
                    row["payload"]["transaction_count"] = count
            receipt["raw"][source] = _write(
                root, f"raw/{source}.jsonl", b"".join(_canonical(row) for row in rows))
    else:
        intent = json.loads((root / receipt["launch_intent"]["path"]).read_text())
        intent["build_binding"]["binaries"].pop("hotstuff_tls_keygen")
        without_hash = {key: value for key, value in intent.items() if key != "intent_sha256"}
        intent["intent_sha256"] = hashlib.sha256(_canonical(without_hash)).hexdigest()
        receipt["launch_intent"] = _write(root, subject.INTENT_PATH, _canonical(intent))
    digest = _write_receipt(root, receipt)
    with pytest.raises(subject.V8SlotError, match=error):
        subject.verify_sealed_slot(root, receipt_relative="runtime/receipt.json",
                                   expected_receipt_sha256=digest)


def test_operator_rechecks_abort_after_signed_replay(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    root, _receipt, digest = _root(tmp_path)
    original = subject.validator_v8.validate_v8_raw_contract

    def replay_then_abort(**kwargs):
        result = original(**kwargs)
        abort = root / subject.ABORTS[-1]
        abort.parent.mkdir(parents=True, exist_ok=True)
        abort.write_bytes(b"{}\n")
        return result

    monkeypatch.setattr(subject.validator_v8, "validate_v8_raw_contract", replay_then_abort)
    with pytest.raises(subject.V8SlotError, match="sealed abort"):
        subject.verify_sealed_slot(root, receipt_relative="runtime/receipt.json",
                                   expected_receipt_sha256=digest)


def test_operator_cannot_skip_the_genuine_full_input_replay(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    root, _receipt, digest = _root(tmp_path)

    def reject(*_args, **_kwargs):
        raise subject.full_input.FullInputError("fixture rejection")

    monkeypatch.setattr(subject.full_input, "verify", reject)
    with pytest.raises(subject.V8SlotError, match="full-input closure rejected"):
        subject.verify_sealed_slot(root, receipt_relative="runtime/receipt.json",
                                   expected_receipt_sha256=digest)


def test_public_operator_requires_the_exact_common_terminal(tmp_path: Path) -> None:
    root, _receipt, digest = _root(tmp_path)
    (root / subject.COMMON_TERMINAL).unlink()
    with pytest.raises(subject.V8SlotError, match="common terminal path is unavailable"):
        subject.verify_sealed_slot(root, receipt_relative="runtime/receipt.json",
                                   expected_receipt_sha256=digest)
