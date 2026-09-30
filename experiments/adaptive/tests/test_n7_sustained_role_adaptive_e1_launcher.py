from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "sustained_role_adaptive_e1_launcher.py"
spec = importlib.util.spec_from_file_location("w19_adaptive_launcher", PATH)
assert spec and spec.loader
subject = importlib.util.module_from_spec(spec); spec.loader.exec_module(subject)


def test_adaptive_approval_uses_outer_verified_raw_hash(tmp_path: Path):
    root = tmp_path / "run"; (root / "runtime").mkdir(parents=True)
    request_bytes = b'{"request":"one"}\n'
    approval = {"schema_version": 1, "kind": subject.AUTH_KIND,
                "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
                "plan_sha256": "a" * 64, "approval_reference": "approved-campaign",
                "approved_utc": "2026-09-30T17:00:00Z", "no_retry": True}
    external = tmp_path / "approval.json"
    original = json.dumps(approval, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    external.write_bytes(original)
    verified_sha = hashlib.sha256(original).hexdigest()
    accepted = subject.fixed._exact_approval(
        root, external, {"plan_sha256": "a" * 64}, request_bytes,
        kind=subject.AUTH_KIND, archive_path=subject.APPROVAL,
        expected_authorization_sha256=verified_sha)
    assert accepted["approval_reference"] == "approved-campaign"
    assert (root / subject.APPROVAL).read_bytes() == original


def test_adaptive_execution_threads_required_approval_hash_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "run"; (root / "runtime").mkdir(parents=True)
    plan = {"state": "PREPARED_DRY_RUN_EXTERNAL_APPROVAL_REQUIRED",
            "comparison": {"arm": "adaptive_e1"}, "no_retry": True,
            "plan_sha256": "a" * 64}
    request = {"execution_plan_sha256": "a" * 64, "no_retry": True}
    monkeypatch.setattr(subject.fixed, "_read", lambda path, _label: (
        (plan, b"plan") if path.name == subject.PLAN.name else (request, b"request")))
    monkeypatch.setattr(subject.fixed, "_plan_digest", lambda _plan: "a" * 64)
    observed: list[str] = []
    def approval(*_args: object, **kwargs: object) -> object:
        observed.append(kwargs["expected_authorization_sha256"])
        raise subject.LaunchError("pin checked before spawn")
    monkeypatch.setattr(subject.fixed, "_exact_approval", approval)
    with pytest.raises(subject.LaunchError, match="pin checked before spawn"):
        subject.execute_adaptive_e1_pilot(
            root, tmp_path / "approval.json", expected_authorization_sha256="b" * 64,
            spawn=lambda *_args, **_kwargs: pytest.fail("must not spawn"),
            event_streams=lambda _root: {}, cleanup=lambda *_args: {}, raw_clock=lambda: 0,
        )
    assert observed == ["b" * 64]


def test_adaptive_manager_requires_native_signed_successor_bindings():
    plan = {"commands": {"manager": {"argv": [
        "manager", "--issuer-id", "1", "--issuer-private-key", "key",
        "--transition-request", "{}", "--bundle-output", "bundle",
    ]}}}
    assert subject._adaptive_manager(plan)[0] == "manager"
    plan["commands"]["manager"]["argv"].remove("--bundle-output")
    with pytest.raises(subject.LaunchError, match="signed successor"):
        subject._adaptive_manager(plan)


def test_anchor_is_only_native_actor_one_internal_aggregate_omission():
    streams = {"replica-1": [
        {"event_type": "fault.contribution_opportunity", "source_monotonic_ns": 10,
         "payload": {"actor": 1, "fault_mode": "role_scoped_persistent_selected_omission_v1",
                     "physical_role": "leaf", "scheduled_action": "omit_direct_vote"}},
        {"event_type": "fault.contribution_opportunity", "source_monotonic_ns": 11,
         "source_sequence": 4, "payload": {"actor": 1, "fault_mode": "role_scoped_persistent_selected_omission_v1",
                     "physical_role": "internal", "scheduled_action": "omit_aggregate"}},
    ]}
    assert subject._first_anchor(streams)["source_sequence"] == 4


def test_phase_gated_adaptive_anchor_rejects_earlier_wrong_tree():
    streams = {"replica-1": [
        {"event_type": "fault.contribution_opportunity", "source_sequence": 1,
         "source_monotonic_ns": 10,
         "payload": {"actor": 1,
                     "fault_mode": "role_scoped_persistent_selected_omission_v1",
                     "physical_role": "internal", "scheduled_action": "omit_aggregate",
                     "proposal": {"epoch_number": 0, "tree_id": 3}}},
        {"event_type": "fault.contribution_opportunity", "source_sequence": 2,
         "source_monotonic_ns": 11,
         "payload": {"actor": 1,
                     "fault_mode": "role_scoped_persistent_selected_omission_v1",
                     "physical_role": "internal", "scheduled_action": "omit_aggregate",
                     "proposal": {"epoch_number": 0, "tree_id": 4}}},
    ]}
    with pytest.raises(subject.LaunchError, match="first actor-1 physical omission does not bind frozen tree 4 internal aggregate"):
        subject._first_anchor(streams, first_omission_tree=4)


def test_phase_gated_adaptive_anchor_rejects_earlier_direct_vote():
    streams = {"replica-1": [
        {"event_type": "fault.contribution_opportunity", "source_sequence": 1,
         "source_monotonic_ns": 10,
         "payload": {"actor": 1,
                     "fault_mode": "role_scoped_persistent_selected_omission_v1",
                     "physical_role": "leaf", "scheduled_action": "omit_direct_vote",
                     "proposal": {"epoch_number": 0, "tree_id": 4}}},
    ]}
    with pytest.raises(subject.LaunchError, match="first actor-1 physical omission does not bind frozen tree 4 internal aggregate"):
        subject._first_anchor(streams, first_omission_tree=4)


def test_phase_gated_adaptive_sealed_anchor_rejects_only_pre_tree_four_omission(
        tmp_path: Path):
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    opportunity = {
        "event_type": "fault.contribution_opportunity", "source_sequence": 1,
        "source_monotonic_ns": 10,
        "payload": {"actor": 1,
                    "fault_mode": "role_scoped_persistent_selected_omission_v1",
                    "physical_role": "internal", "scheduled_action": "omit_aggregate",
                    "proposal": {"tree_id": 3}},
    }
    for source in ("adaptive-manager", *[f"replica-{replica}" for replica in range(7)]):
        rows = [opportunity, {"source_monotonic_ns": 60_000_000_010}] if source == "replica-1" else [
            {"source_monotonic_ns": 60_000_000_010}]
        (root / "raw" / f"{source}.jsonl").write_text(
            "".join(__import__("json").dumps(row) + "\n" for row in rows))
    with pytest.raises(subject.LaunchError, match="first actor-1 physical omission does not bind frozen tree 4 internal aggregate"):
        subject._sealed_anchor_and_coverage(root, first_omission_tree=4)


def test_all_seven_native_v2_e1_activations_are_deadline_bound():
    streams = {f"replica-{i}": [{"event_type": "epoch.activated", "source_monotonic_ns": 20,
                                   "payload": {"epoch_number": 1, "tree_id": 0,
                                               "epoch_digest": "a" * 64, "activation_height": 9}}] for i in range(7)}
    assert subject._all_seven_e1_activated(streams, deadline_ns=20)
    streams["replica-6"][0]["source_monotonic_ns"] = 21
    assert not subject._all_seven_e1_activated(streams, deadline_ns=20)
    streams["replica-6"][0]["source_monotonic_ns"] = 20
    streams["replica-6"][0]["payload"]["unexpected"] = True
    assert not subject._all_seven_e1_activated(streams, deadline_ns=20)


def test_issuer_requires_archived_canonical_public_key(tmp_path: Path):
    root = tmp_path / "run"; (root / "runtime").mkdir(parents=True)
    identity = root / "runtime/e0-identity-receipt.json"
    identity.write_text('{"epoch_digest":"a","epoch_number":0,"schema_version":1,"state":"DERIVED_READ_ONLY"}\n')
    issuer = root / "runtime/issuer-public-key.txt"
    issuer.write_text("02" + "a" * 64 + "\n")
    adapter_plan = {"state": "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED",
                    "e0_identity_receipt": "runtime/e0-identity-receipt.json",
                    "e0_identity_receipt_sha256": __import__("hashlib").sha256(identity.read_bytes()).hexdigest(),
                    "issuer_public_key": "runtime/issuer-public-key.txt",
                    "issuer_public_key_sha256": __import__("hashlib").sha256(issuer.read_bytes()).hexdigest()}
    (root / "local-launch-plan.json").write_text(__import__("json").dumps(adapter_plan, sort_keys=True, separators=(",", ":")) + "\n")
    assert subject._issuer_path(root, {}) == issuer
    issuer.write_text("not-a-key\n")
    with pytest.raises(subject.LaunchError, match="hash-drifted"):
        subject._issuer_path(root, {})


def test_sealed_anchor_rejects_earlier_leaf_opportunity(tmp_path: Path):
    root = tmp_path / "run"; (root / "raw").mkdir(parents=True)
    def event(sequence, ns, role, action):
        return {"event_type": "fault.contribution_opportunity", "source_sequence": sequence, "source_monotonic_ns": ns,
                "payload": {"actor": 1, "fault_mode": "role_scoped_persistent_selected_omission_v1", "physical_role": role, "scheduled_action": action}}
    rows = [event(1, 10, "leaf", "omit_direct_vote"), event(2, 11, "internal", "omit_aggregate")]
    for source in ("adaptive-manager", *[f"replica-{i}" for i in range(7)]):
        values = rows if source == "replica-1" else [{"source_monotonic_ns": 60_000_000_011}]
        if source == "replica-1": values = [*rows, {"source_monotonic_ns": 60_000_000_011}]
        (root / "raw" / f"{source}.jsonl").write_text("".join(__import__("json").dumps(item) + "\n" for item in values))
    sealed = subject._sealed_anchor_and_coverage(root)
    assert sealed["source_sequence"] == 2 and sealed["monotonic_ns"] == 11


def test_adaptive_coverage_is_replica_only_and_requires_success_terminal(tmp_path: Path):
    root = tmp_path / "run"; (root / "raw").mkdir(parents=True)
    anchor = {"event_type": "fault.contribution_opportunity", "source_sequence": 2,
              "source_monotonic_ns": 11, "payload": {"actor": 1,
              "fault_mode": "role_scoped_persistent_selected_omission_v1",
              "physical_role": "internal", "scheduled_action": "omit_aggregate"}}
    for replica in range(7):
        values = [anchor, {"source_monotonic_ns": 60_000_000_011}] if replica == 1 else [{"source_monotonic_ns": 60_000_000_011}]
        (root / "raw" / f"replica-{replica}.jsonl").write_text("".join(__import__("json").dumps(row) + "\n" for row in values))
    # A successful manager can terminate before the peer horizon.
    manager = {"event_type": "adaptive_v2_session_terminal", "source_monotonic_ns": 12,
               "payload": {"outcome": "advanced", "reason": "successor_converged",
                           "successor_epoch_number": 1}}
    (root / "raw/adaptive-manager.jsonl").write_text(__import__("json").dumps(manager) + "\n")
    assert subject._sealed_anchor_and_coverage(root)["source_sequence"] == 2
    subject._adaptive_manager_success_terminal(root)
    manager["payload"]["reason"] = "caller_failed"
    (root / "raw/adaptive-manager.jsonl").write_text(__import__("json").dumps(manager) + "\n")
    with pytest.raises(subject.LaunchError, match="success terminal"):
        subject._adaptive_manager_success_terminal(root)


def test_fault_window_arm_is_exactly_bound_and_requires_native_manager_ack(tmp_path: Path):
    root = tmp_path / "run"
    tree = root / "config/epoch0.tree"; tree.parent.mkdir(parents=True)
    tree.write_bytes(b"frozen epoch zero tree\n")
    topology = __import__("hashlib").sha256(tree.read_bytes()).hexdigest()
    e0, profile, request = "a" * 64, "b" * 64, __import__("hashlib").sha256(b"{}").hexdigest()
    manager = (
        "manager", "--structured-event-run-id", "w19-ack", "--epoch-zero-tree-file", str(tree),
        "--transition-request", "{}",
        "--fault-window-arm-path", str(root / "runtime/fault-window-arm.json"),
        "--fault-window-arm-run-id", "w19-ack",
        "--fault-window-arm-schema-version", "4",
        "--fault-window-arm-domain", "kauri-focused-fault-window-arm-v4",
        "--fault-window-arm-profile-id", "n7-path-local-timeout-quorum-v4",
        "--fault-window-arm-profile-sha256", profile,
        "--fault-window-arm-topology-proof-sha256", topology,
        "--fault-window-arm-request-sha256", request,
        "--fault-window-arm-epoch-number", "0",
        "--fault-window-arm-epoch-digest", e0,
        "--fault-window-arm-prefault-tree-id", "4",
        "--fault-window-arm-required-tree-positions", "3",
        "--fault-window-arm-timeout-evidence-basis", "exact_timeout_attempt_id_v1",
        "--fault-window-arm-required-observation-schema", "3",
        "--fault-window-arm-clock-domain", "same_host_clock_monotonic_raw",
        "--fault-window-arm-snapshot-evidence-basis", "exact_post_fault_path_timeout_quorum_v1",
        "--fault-window-arm-selection-cardinality-policy", "all_guarded_up_to_fault_bound_v1",
    )
    approval = {"schema_version": 1, "kind": "test", "no_retry": True}
    # The native parser requires this path to be absent before manager spawn.
    assert subject._fault_window_arm_startup_path(root, manager) == root / "runtime/fault-window-arm.json"
    startup_arm = root / "runtime/fault-window-arm.json"; startup_arm.parent.mkdir()
    startup_arm.write_text("stale\n")
    with pytest.raises(subject.LaunchError, match="absent at startup"):
        subject._fault_window_arm_startup_path(root, manager)
    startup_arm.unlink()
    arm_path, document = subject._fault_window_arm_document(
        root, manager, approval, e0_digest=e0, evidence_start_monotonic_ns=123,
    )
    assert arm_path == root / "runtime/fault-window-arm.json"
    assert arm_path.read_bytes() == subject._canonical(document)
    assert document["required_tree_ids"] == [4, 5, 6]
    assert document["fault_receipt_sha256"] == subject._sha(subject._canonical(approval))
    event = {"event_type": "fault_window_armed", "payload": {
        **document, "fault_window_arm_sha256": subject._sha(subject._canonical(document)),
    }}
    assert subject._fault_window_arm_ack({"adaptive-manager": [event]}, document)
    event["payload"]["fault_window_arm_sha256"] = "0" * 64
    assert not subject._fault_window_arm_ack({"adaptive-manager": [event]}, document)
    # The publication primitive is O_EXCL: a stale or competing arm cannot be
    # replaced after the native manager has started polling it.
    with pytest.raises(subject.LaunchError, match="already exists"):
        subject._fault_window_arm_document(
            root, manager, approval, e0_digest=e0, evidence_start_monotonic_ns=124,
        )


def test_fault_window_arm_refuses_mismatched_topology_or_reuse(tmp_path: Path):
    root = tmp_path / "run"; tree = root / "config/epoch0.tree"; tree.parent.mkdir(parents=True)
    tree.write_text("tree\n")
    manager = (
        "manager", "--structured-event-run-id", "w19", "--epoch-zero-tree-file", str(tree),
        "--fault-window-arm-path", str(root / "runtime/fault-window-arm.json"),
        "--fault-window-arm-run-id", "w19",
        "--fault-window-arm-schema-version", "4", "--fault-window-arm-domain", "kauri-focused-fault-window-arm-v4",
        "--fault-window-arm-profile-id", "n7-path-local-timeout-quorum-v4", "--fault-window-arm-profile-sha256", "a" * 64,
        "--fault-window-arm-topology-proof-sha256", "b" * 64, "--fault-window-arm-request-sha256", "c" * 64,
        "--fault-window-arm-epoch-number", "0", "--fault-window-arm-epoch-digest", "d" * 64,
        "--fault-window-arm-prefault-tree-id", "4", "--fault-window-arm-required-tree-positions", "3",
        "--fault-window-arm-timeout-evidence-basis", "exact_timeout_attempt_id_v1",
        "--fault-window-arm-required-observation-schema", "3", "--fault-window-arm-clock-domain", "same_host_clock_monotonic_raw",
        "--fault-window-arm-snapshot-evidence-basis", "exact_post_fault_path_timeout_quorum_v1",
        "--fault-window-arm-selection-cardinality-policy", "all_guarded_up_to_fault_bound_v1",
    )
    with pytest.raises(subject.LaunchError, match="topology proof"):
        subject._fault_window_arm_document(root, manager, {"x": 1}, e0_digest="d" * 64,
                                           evidence_start_monotonic_ns=1)
