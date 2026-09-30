from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kauri_experiment import operator_capacity_v3_backend as subject
from kauri_experiment.operator_capacity_preflight import _EXPECTED_QUOTA_PROFILE


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _fixture(tmp_path: Path) -> tuple[Path, list[str], list[list[str]], Path]:
    root = tmp_path / "materialized"
    (root / "config").mkdir(parents=True, mode=0o700)
    (root / "raw").mkdir(mode=0o700)
    (root / "transitions/e0-to-e1-operator-capacity").mkdir(parents=True, mode=0o700)
    artifacts: dict[str, str] = {}
    for relative in ("config/epoch0.tree", "config/stage-a-envelope.wire", "config/hotstuff.gen.conf", *[f"config/replica-{i}.conf" for i in range(31)]):
        path = root / relative; path.write_bytes(relative.encode("ascii")); artifacts[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    transition = {
        "policy_intent": "performance_optimization", "evidence_window_rule": "fresh_exact_predecessor_after_common_commit",
        "transition_artifact_id": "e0-to-e1-operator-capacity", "bundle_path": "transitions/e0-to-e1-operator-capacity/successor.bundle",
        "evidence_snapshot_path": "transitions/e0-to-e1-operator-capacity/evidence-snapshot.json",
        "predecessor_epoch_number": 0, "successor_epoch_number": 1, "minimum_predecessor_residency_ms": 0,
        "minimum_post_baseline_observation_ms": 0, "apply_shape_selection": False, "policy_parameters": {},
    }
    manager = ["/bin/manager", "--protocol-mode", "adaptive_v3", "--transition-request", json.dumps(transition, sort_keys=True, separators=(",", ":")),
               "--structured-event-output", str(root / "raw/manager-events.jsonl"),
               "--operator-capacity-stage-b-authorization-output", str(root / "raw/stage-b-authorization.wire"),
               "--operator-capacity-consumption-output", str(root / "raw/consumption.json"),
               "--bundle-output", str(root / "transitions/e0-to-e1-operator-capacity/successor.bundle"),
               "--epoch-zero-tree-file", str(root / "config/epoch0.tree"),
               "--operator-capacity-stage-a-envelope", str(root / "config/stage-a-envelope.wire"),
               "--operator-capacity-stage-a-wire-sha256", artifacts["config/stage-a-envelope.wire"],
               "--operator-capacity-label-issuer-id", "7", "--operator-capacity-label-issuer-reference", "test",
               "--operator-capacity-label-issuer-public-key-hex", "a" * 66,
               "--operator-capacity-label-issuer-public-key-fingerprint", "b" * 64,
               "--operator-capacity-approved-capacity-digest", "c" * 64]
    replicas = [["/bin/app", "--structured-event-output", str(root / f"raw/replica-{i}.jsonl"),
                 "--structured-event-commit-observer-id", "replica-0"] for i in range(31)]
    verifier_arguments = ["--epoch0-tree-file", str(root / "config/epoch0.tree"), "--stage-a-envelope-wire", str(root / "config/stage-a-envelope.wire"), "--issuer-id", "7", "--issuer-reference", "test", "--issuer-public-key-hex", "a" * 66, "--issuer-public-key-fingerprint", "b" * 64, "--approved-capacity-digest", "c" * 64, "--arm", "fast_priority_treatment", "--source-revision", "a" * 40]
    manifest = {
        "schema_version": 1, "kind": "kauri-n31-operator-capacity-v3-materialization-v1", "verdict": "MATERIALIZED_NO_EXECUTION",
        "claim_eligible": False, "figure_eligible": False, "arm": "treatment", "stage_a_native_arm": "fast_priority_treatment",
        "protocol": {"N": 31, "Q": 21, "tree_count": 21}, "slow_root_ids": list(range(6)), "revision": "a" * 40,
        "epoch0_tree": {"sha256": artifacts["config/epoch0.tree"], "topology_digest": "c" * 64}, "binary_sha256": {
        "adaptation_manager": "d" * 64, "hotstuff_app": "e" * 64,
            "identity_parity_verifier": "0" * 64,
        },
        "artifact_sha256": artifacts, "manager_argv_sha256": subject._argv_digest(manager),
        "replica_argv_sha256": [subject._argv_digest(argv) for argv in replicas], "stage_a_envelope_sha256": artifacts["config/stage-a-envelope.wire"],
        "stage_a_verifier_receipt_sha256": "1" * 64, "stage_a_verifier_arguments": verifier_arguments, "identity_parity_receipt_sha256": "2" * 64,
        "tool_identity_approval_receipt_sha256": "3" * 64,
        "stage_b_authorization_output": "raw/stage-b-authorization.wire", "consumption_output": "raw/consumption.json",
        "bundle_output": "transitions/e0-to-e1-operator-capacity/successor.bundle",
    }
    (root / "materialization-manifest.json").write_bytes(_canonical(manifest))
    quota = tmp_path / "quota.json"; quota.write_bytes(_canonical(_EXPECTED_QUOTA_PROFILE))
    return root, manager, replicas, quota


def test_backend_consumes_current_materializer_schema_but_keeps_execution_hard_stopped(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    plan = subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)
    assert plan["verdict"] == "BACKEND_PLAN_REVIEW_REQUIRED_NO_EXECUTION"
    assert plan["launch_permitted"] is False and plan["automatic_retries"] == 0
    assert plan["quota_ownership"]["replica_ids"] == list(range(31))
    assert plan["cleanup_contract"]["required_order"] == ["stop_quota_monitor", "terminate_manager_and_replicas", "terminate_owned_replica_scopes", "verify_scope_cleanup"]
    assert plan["native_policy_order_repaired"] is True
    assert plan["execution_blocker"] == "EXTERNAL_AUTHORIZATION_AND_PRESPAWN_AUTHORITY_REQUIRED"
    assert plan["stage_a"]["tool_identity_approval_receipt_sha256"] == "3" * 64
    assert list((root / "raw").iterdir()) == []


def test_backend_rejects_manager_argv_not_matching_frozen_manifest(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path); manager[-1] = str(root / "elsewhere.bundle")
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="manager argv differs"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_self_consistent_manager_output_path_escape(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    manager[manager.index("--bundle-output") + 1] = str(tmp_path / "escaped.bundle")
    manifest_path = root / "materialization-manifest.json"; manifest = json.loads(manifest_path.read_text())
    manifest["manager_argv_sha256"] = subject._argv_digest(manager); manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="escapes the exact materialization root"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_preexisting_raw_or_transition_output(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    (root / "raw/manager-events.jsonl").write_text("fabricated\n")
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="must be fresh"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_epoch0_manifest_binding_that_differs_from_materialized_artifact(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["epoch0_tree"]["sha256"] = "f" * 64
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="Epoch-0 tree artifact differs"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_non_n31_replica_argv_and_has_no_execution_entrypoint(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="exactly N31"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas[:-1], quota_profile=quota)
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="no process runner"):
        subject.execution_not_implemented()


def test_backend_rejects_manifest_missing_external_tool_identity_approval(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["tool_identity_approval_receipt_sha256"]
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="schema differs"):
        subject.prepare_no_launch_backend(
            materialization_root=root, manager_argv=manager,
                replica_argv=replicas, quota_profile=quota,
        )


def test_backend_rejects_materialized_config_that_differs_from_manifest(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path / "changed-config")
    (root / "config/hotstuff.gen.conf").write_text("changed\n")
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="materialization artifact"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)
