from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kauri_experiment import cpu_quota
from kauri_experiment import operator_capacity_stage_a_preflight as stage_a
from kauri_experiment import operator_capacity_v3_backend as backend
from kauri_experiment import operator_capacity_v3_local_runner as subject
from kauri_experiment.operator_capacity_preflight import _EXPECTED_QUOTA_PROFILE


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _write(path: Path, value: object) -> bytes:
    raw = _canonical(value)
    path.write_bytes(raw)
    return raw


def _fixture(tmp_path: Path) -> tuple[Path, list[str], list[list[str]], Path, Path, dict[str, Path]]:
    root = tmp_path / "materialized"
    for directory in (root / "config", root / "raw", root / "transitions/e0-to-e1-operator-capacity"):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    artifacts = {}
    for relative in ("config/epoch0.tree", "config/stage-a-envelope.wire", "config/hotstuff.gen.conf", *[f"config/replica-{i}.conf" for i in range(31)]):
        path = root / relative
        path.write_bytes(relative.encode())
        artifacts[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    transition = {"policy_intent": "performance_optimization", "evidence_window_rule": "fresh_exact_predecessor_after_common_commit", "transition_artifact_id": "e0-to-e1-operator-capacity", "bundle_path": "transitions/e0-to-e1-operator-capacity/successor.bundle", "evidence_snapshot_path": "transitions/e0-to-e1-operator-capacity/evidence-snapshot.json", "predecessor_epoch_number": 0, "successor_epoch_number": 1, "minimum_predecessor_residency_ms": 0, "minimum_post_baseline_observation_ms": 0, "apply_shape_selection": False, "policy_parameters": {}}
    binaries = {}
    for name in stage_a.REQUIRED_BINARIES:
        path = tmp_path / f"binary-{name}"
        path.write_bytes(name.encode("ascii"))
        binaries[name] = path
    binary_sha = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in binaries.items()}
    plan_binary_sha = {name: binary_sha[name] for name in ("adaptation_manager", "hotstuff_app", "identity_parity_verifier")}
    approval = tmp_path / "tool-approval.json"
    _write(approval, {"schema_version": 1, "kind": "kauri-n31-operator-capacity-tool-identity-approval-v1", "verdict": "EXTERNAL_TOOL_IDENTITY_APPROVED", "revision": "a" * 40, "approval_ref": "external-test", "approved_at_utc": "2026-09-29T00:00:00Z", "binary_sha256": binary_sha})
    manager = [str(binaries["adaptation_manager"]), "--protocol-mode", "adaptive_v3", "--transition-request", json.dumps(transition, sort_keys=True, separators=(",", ":")), "--structured-event-output", str(root / "raw/manager-events.jsonl"), "--operator-capacity-stage-b-authorization-output", str(root / "raw/stage-b-authorization.wire"), "--operator-capacity-consumption-output", str(root / "raw/consumption.json"), "--bundle-output", str(root / "transitions/e0-to-e1-operator-capacity/successor.bundle"), "--epoch-zero-tree-file", str(root / "config/epoch0.tree"), "--operator-capacity-stage-a-envelope", str(root / "config/stage-a-envelope.wire"), "--operator-capacity-stage-a-wire-sha256", hashlib.sha256((root / "config/stage-a-envelope.wire").read_bytes()).hexdigest(), "--operator-capacity-label-issuer-id", "7", "--operator-capacity-label-issuer-reference", "test", "--operator-capacity-label-issuer-public-key-hex", "a" * 66, "--operator-capacity-label-issuer-public-key-fingerprint", "b" * 64, "--operator-capacity-approved-capacity-digest", "c" * 64]
    replicas = [[str(binaries["hotstuff_app"]), "--structured-event-output", str(root / f"raw/replica-{i}.jsonl"), "--structured-event-commit-observer-id", "replica-0"] for i in range(31)]
    verifier_arguments = ["--epoch0-tree-file", str(root / "config/epoch0.tree"), "--stage-a-envelope-wire", str(root / "config/stage-a-envelope.wire"), "--issuer-id", "7", "--issuer-reference", "test", "--issuer-public-key-hex", "a" * 66, "--issuer-public-key-fingerprint", "b" * 64, "--approved-capacity-digest", "c" * 64, "--arm", "fast_priority_treatment", "--source-revision", "a" * 40]
    manifest = {"schema_version": 1, "kind": "kauri-n31-operator-capacity-v3-materialization-v1", "verdict": "MATERIALIZED_NO_EXECUTION", "claim_eligible": False, "figure_eligible": False, "arm": "treatment", "stage_a_native_arm": "fast_priority_treatment", "protocol": {"N": 31, "Q": 21, "tree_count": 21}, "slow_root_ids": list(range(6)), "revision": "a" * 40, "epoch0_tree": {"sha256": artifacts["config/epoch0.tree"], "topology_digest": "c" * 64}, "binary_sha256": plan_binary_sha, "artifact_sha256": artifacts, "manager_argv_sha256": backend._argv_digest(manager), "replica_argv_sha256": [backend._argv_digest(row) for row in replicas], "stage_a_envelope_sha256": artifacts["config/stage-a-envelope.wire"], "stage_a_verifier_receipt_sha256": "1" * 64, "stage_a_verifier_arguments": verifier_arguments, "identity_parity_receipt_sha256": "2" * 64, "tool_identity_approval_receipt_sha256": hashlib.sha256(approval.read_bytes()).hexdigest(), "stage_b_authorization_output": "raw/stage-b-authorization.wire", "consumption_output": "raw/consumption.json", "bundle_output": "transitions/e0-to-e1-operator-capacity/successor.bundle"}
    _write(root / "materialization-manifest.json", manifest)
    quota = tmp_path / "quota.json"
    quota.write_bytes(_canonical(_EXPECTED_QUOTA_PROFILE))
    return root, manager, replicas, quota, approval, binaries


class _FakeLifecycle:
    def __init__(self) -> None: self.calls: list[str] = []
    def start_replica(self, replica_id: int, argv: list[str], log: Path) -> None: self.calls.append(f"replica-{replica_id}")
    def start_manager(self, argv: list[str], log: Path) -> None: self.calls.append("manager")
    def wait_manager(self, deadline_monotonic: float) -> int: self.calls.append("wait"); return 0
    def stop_monitor(self) -> None: self.calls.append("stop-monitor")
    def terminate_manager_and_replicas(self) -> None: self.calls.append("terminate-processes")
    def terminate_owned_replica_scopes(self, deadline_monotonic: float) -> dict[str, object]: self.calls.append("terminate-scopes"); return {"units": []}
    def verify_scope_cleanup(self, deadline_monotonic: float) -> dict[str, object]: self.calls.append("verify-cleanup"); return {"verified": True}


def _contract() -> cpu_quota.CpuQuotaContract:
    assignments = tuple(cpu_quota.CpuQuotaAssignment(replica, "slow" if replica < 6 else "fast", 25 if replica < 6 else 100) for replica in range(31))
    return cpu_quota.CpuQuotaContract(schema_version=1, contract_id="n31-static-resource-cpu-sham-quota-v1", enabled=True, figure_eligible=False, launcher="systemd-user-scope-cpu-quota-v1", manager_visibility="none", sampling_interval_ms=1000, base_profile_id="n31-static-resource-cpu-sham-v1", base_profile_sha256="285aa55cb33637009ccd491d74830cd7485bcd83cb993c33c488dbff6fe4bf09", base_profile_canonical_sha256="2ed182ed95fe8514c80eb861ed2e86654afaaf6b881a2ede6fc1a03d0565b766", assignments=assignments, contract_sha256="0" * 64)


def _bind_materialization_to_authority(root: Path, authority: dict[str, object]) -> None:
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    envelope = Path(str(authority["envelope"])).read_bytes()
    native = Path(str(authority["native_receipt"])).read_bytes()
    tree = Path(str(authority["epoch0_tree"])).read_bytes()
    manifest["stage_a_envelope_sha256"] = hashlib.sha256(envelope).hexdigest()
    manifest["stage_a_verifier_receipt_sha256"] = hashlib.sha256(native).hexdigest()
    manifest["epoch0_tree"]["sha256"] = hashlib.sha256(tree).hexdigest()
    _write(manifest_path, manifest)


def _authorized(root: Path, manager: list[str], replicas: list[list[str]], quota: Path) -> tuple[dict[str, object], bytes, dict[str, object]]:
    plan = backend.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)
    request = subject.build_execution_request(plan, materialization_root=root, timeout_s=1200)
    receipt = {**json.loads(request), "kind": "kauri-n31-operator-capacity-v3-local-execution-authorization-v1", "request_sha256": hashlib.sha256(request).hexdigest(), "approval_reference": "external-test", "approved_utc": "2026-09-29T00:00:00Z"}
    return plan, request, receipt


def _authority(tmp_path: Path, root: Path, quota: Path, approval: Path, binaries: dict[str, Path]) -> dict[str, object]:
    snapshot = tmp_path / "snapshot.wire"; snapshot.write_bytes(b"snapshot")
    envelope = root / "config/stage-a-envelope.wire"
    tree = root / "config/epoch0.tree"
    native = tmp_path / "native-receipt.json"
    envelope_sha = hashlib.sha256(envelope.read_bytes()).hexdigest()
    native_raw = _write(native, {"schema_version": 1, "kind": stage_a.VERIFIER_KIND, "verdict": stage_a.VERIFIER_VERDICT, "envelope_wire_sha256": envelope_sha, "envelope_canonical_digest": "a" * 64, "approved_capacity_digest": "b" * 64, "issuer_id": 7, "issuer_reference": "test", "issuer_public_key_fingerprint": "c" * 64, "arm": "fast_priority_treatment", "source_revision": "a" * 40, "verification_monotonic_raw_ns": 1, "epoch0_tree_file_sha256": hashlib.sha256(tree.read_bytes()).hexdigest(), "epoch0_consensus_digest": "d" * 64, "epoch0_topology_digest": "c" * 64})
    input_sha = {"epoch0_tree_file": hashlib.sha256(tree.read_bytes()).hexdigest(), "capacity_snapshot_wire": hashlib.sha256(snapshot.read_bytes()).hexdigest(), "stage_a_envelope_wire": envelope_sha, "quota_profile": hashlib.sha256(quota.read_bytes()).hexdigest()}
    binary_sha = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in binaries.items()}
    preflight = {"schema_version": 1, "kind": stage_a.PREFLIGHT_KIND, "verdict": "PREFLIGHT_OK_NO_EXECUTION", "claim_eligible": False, "figure_eligible": False, "revision": "a" * 40, "protocol": {"N": 31, "Q": 21}, "arm": "treatment", "stage_a_native_arm": "fast_priority_treatment", "output_root": str(root.resolve()), "binary_sha256": binary_sha, "input_sha256": input_sha, "native_verifier_receipt_sha256": hashlib.sha256(native_raw).hexdigest(), "tool_identity_approval_receipt_sha256": hashlib.sha256(approval.read_bytes()).hexdigest()}
    preflight_path = tmp_path / "stage-a-preflight.json"; preflight_raw = _write(preflight_path, preflight)
    request = {"schema_version": 1, "kind": stage_a.REQUEST_KIND, "verdict": "EXECUTION_AUTHORIZATION_REQUEST_REQUIRED", "claim_eligible": False, "figure_eligible": False, "preflight_sha256": hashlib.sha256(preflight_raw).hexdigest(), "revision": "a" * 40, "arm": "treatment", "output_root": str(root.resolve()), "binary_sha256": binary_sha, "input_sha256": input_sha, "native_verifier_receipt_sha256": hashlib.sha256(native_raw).hexdigest(), "tool_identity_approval_receipt_sha256": hashlib.sha256(approval.read_bytes()).hexdigest()}
    request_path = tmp_path / "stage-a-request.json"; request_raw = _write(request_path, request)
    approval_path = tmp_path / "stage-a-approval.json"; approval_raw = _write(approval_path, {"schema_version": 1, "kind": "kauri-n31-operator-capacity-execution-approval-v1", "verdict": "EXTERNAL_EXECUTION_APPROVED", "request_sha256": hashlib.sha256(request_raw).hexdigest(), "approval_ref": "external-test", "approved_at_utc": "2026-09-29T00:00:00Z"})
    return {"preflight": preflight_path, "request": request_path, "approval": approval_path, "expected_approval_sha256": hashlib.sha256(approval_raw).hexdigest(), "native_receipt": native, "tool_approval": approval, "epoch0_tree": tree, "snapshot": snapshot, "envelope": envelope, "quota_profile": quota, "binaries": binaries}


def _native_verifier_copying_retained_receipt(authority: dict[str, object]):
    def invoke(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        Path(command[-1]).write_bytes(Path(str(authority["native_receipt"])).read_bytes())
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
    return invoke


def test_prepare_is_nonexecuting_and_requires_exact_external_authorization(tmp_path: Path) -> None:
    root, manager, replicas, quota, approval, _binaries = _fixture(tmp_path); _plan, request, receipt = _authorized(root, manager, replicas, quota)
    lifecycle = _FakeLifecycle()
    result = subject.execute_excluded_local_shakedown(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota, authorization_request=request, authorization_receipt=receipt, tool_identity_approval_path=approval, execute=False, timeout_s=1200, current_revision=lambda: "a" * 40, worktree_clean=lambda: True, lifecycle_factory=lambda: lifecycle)
    assert result["verdict"] == "PREPARED_NO_EXECUTION" and lifecycle.calls == []
    receipt["arm"] = "sham"
    with pytest.raises(subject.OperatorCapacityV3LocalRunnerError, match="not bound"):
        subject.verify_external_authorization(request, receipt)


def test_missing_or_fake_stage_a_authority_cannot_reach_mocked_lifecycle(tmp_path: Path) -> None:
    root, manager, replicas, quota, approval, _binaries = _fixture(tmp_path); _plan, request, receipt = _authorized(root, manager, replicas, quota)
    lifecycle = _FakeLifecycle()
    with pytest.raises(subject.OperatorCapacityV3LocalRunnerError, match="pre-spawn authority schema"):
        subject.execute_excluded_local_shakedown(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota, authorization_request=request, authorization_receipt=receipt, tool_identity_approval_path=approval, execute=True, timeout_s=1200, current_revision=lambda: "a" * 40, worktree_clean=lambda: True, lifecycle_factory=lambda: lifecycle, pre_spawn_authority={}, quota_contract=_contract())
    assert lifecycle.calls == [] and not (root / "logs").exists()


def test_forged_stage_a_chain_not_bound_to_materialization_never_constructs_lifecycle(tmp_path: Path) -> None:
    root, manager, replicas, quota, approval, binaries = _fixture(tmp_path)
    authority = _authority(tmp_path, root, quota, approval, binaries)
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest["epoch0_tree"]["sha256"] = hashlib.sha256((root / "config/epoch0.tree").read_bytes()).hexdigest()
    _write(manifest_path, manifest)
    _plan, request, receipt = _authorized(root, manager, replicas, quota)
    constructed: list[bool] = []
    with pytest.raises(subject.OperatorCapacityV3LocalRunnerError, match="not bound to the executable materialization plan"):
        subject.execute_excluded_local_shakedown(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota, authorization_request=request, authorization_receipt=receipt, tool_identity_approval_path=approval, execute=True, timeout_s=1200, current_revision=lambda: "a" * 40, worktree_clean=lambda: True, lifecycle_factory=lambda: (constructed.append(True) or _FakeLifecycle()), pre_spawn_authority=authority, quota_contract=_contract())
    assert constructed == [] and not (root / "runtime").exists()


def test_execution_rejects_dirty_or_wrong_revision_before_lifecycle(tmp_path: Path) -> None:
    root, manager, replicas, quota, approval, binaries = _fixture(tmp_path)
    lifecycle = _FakeLifecycle(); authority = _authority(tmp_path, root, quota, approval, binaries); _bind_materialization_to_authority(root, authority); _plan, request, receipt = _authorized(root, manager, replicas, quota)
    with pytest.raises(subject.OperatorCapacityV3LocalRunnerError, match="clean worktree"):
        subject.execute_excluded_local_shakedown(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota, authorization_request=request, authorization_receipt=receipt, tool_identity_approval_path=approval, execute=True, timeout_s=1200, current_revision=lambda: "b" * 40, worktree_clean=lambda: False, lifecycle_factory=lambda: lifecycle, pre_spawn_authority=authority, quota_contract=_contract())
    assert lifecycle.calls == [] and not (root / "logs").exists()


def test_valid_authority_reaches_fake_lifecycle_and_mutated_binary_cannot(tmp_path: Path) -> None:
    root, manager, replicas, quota, approval, binaries = _fixture(tmp_path)
    authority = _authority(tmp_path, root, quota, approval, binaries); _bind_materialization_to_authority(root, authority); _plan, request, receipt = _authorized(root, manager, replicas, quota)
    lifecycle = _FakeLifecycle()
    constructed: list[bool] = []
    def factory() -> _FakeLifecycle:
        assert (root / "logs").is_dir() and (root / "runtime").is_dir()
        (root / "runtime/factory-side-effect").write_bytes(b"owned")
        constructed.append(True)
        return lifecycle
    result = subject.execute_excluded_local_shakedown(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota, authorization_request=request, authorization_receipt=receipt, tool_identity_approval_path=approval, execute=True, timeout_s=1200, current_revision=lambda: "a" * 40, worktree_clean=lambda: True, lifecycle_factory=factory, pre_spawn_authority=authority, quota_contract=_contract(), native_verifier_run=_native_verifier_copying_retained_receipt(authority))
    assert result["verdict"] == "PROCESS_COMPLETED_PENDING_RAW_VALIDATION"
    assert constructed == [True] and (root / "runtime/factory-side-effect").read_bytes() == b"owned"
    assert lifecycle.calls[:32] == [*(f"replica-{replica}" for replica in range(31)), "manager"]
    assert lifecycle.calls[-5:] == ["wait", "stop-monitor", "terminate-processes", "terminate-scopes", "verify-cleanup"]

    root, manager, replicas, quota, approval, binaries = _fixture(tmp_path / "mutated")
    authority = _authority(tmp_path / "mutated", root, quota, approval, binaries); _bind_materialization_to_authority(root, authority); _plan, request, receipt = _authorized(root, manager, replicas, quota)
    binaries["hotstuff_app"].write_bytes(b"changed")
    lifecycle = _FakeLifecycle()
    with pytest.raises(subject.OperatorCapacityV3LocalRunnerError, match="binary hotstuff_app differs"):
        subject.execute_excluded_local_shakedown(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota, authorization_request=request, authorization_receipt=receipt, tool_identity_approval_path=approval, execute=True, timeout_s=1200, current_revision=lambda: "a" * 40, worktree_clean=lambda: True, lifecycle_factory=lambda: lifecycle, pre_spawn_authority=authority, quota_contract=_contract())
    assert lifecycle.calls == [] and not (root / "logs").exists()


def test_fresh_native_verifier_receipt_mismatch_never_constructs_lifecycle(tmp_path: Path) -> None:
    root, manager, replicas, quota, approval, binaries = _fixture(tmp_path)
    authority = _authority(tmp_path, root, quota, approval, binaries); _bind_materialization_to_authority(root, authority); _plan, request, receipt = _authorized(root, manager, replicas, quota)
    constructed: list[bool] = []
    def forged(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        value = json.loads(Path(str(authority["native_receipt"])).read_text(encoding="ascii"))
        value["approved_capacity_digest"] = "e" * 64
        _write(Path(command[-1]), value)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
    result = subject.execute_excluded_local_shakedown(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota, authorization_request=request, authorization_receipt=receipt, tool_identity_approval_path=approval, execute=True, timeout_s=1200, current_revision=lambda: "a" * 40, worktree_clean=lambda: True, lifecycle_factory=lambda: (constructed.append(True) or _FakeLifecycle()), pre_spawn_authority=authority, quota_contract=_contract(), native_verifier_run=forged)
    assert result["verdict"] == "ABORTED"
    assert "fresh native Stage-A verifier output differs" in str(result["failure"])
    assert constructed == []


def test_native_verifier_failure_seals_abort_before_any_process_spawn(tmp_path: Path) -> None:
    root, manager, replicas, quota, approval, binaries = _fixture(tmp_path)
    authority = _authority(tmp_path, root, quota, approval, binaries); _bind_materialization_to_authority(root, authority); _plan, request, receipt = _authorized(root, manager, replicas, quota)
    constructed: list[bool] = []
    def rejected(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"rejected")
    result = subject.execute_excluded_local_shakedown(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota, authorization_request=request, authorization_receipt=receipt, tool_identity_approval_path=approval, execute=True, timeout_s=1200, current_revision=lambda: "a" * 40, worktree_clean=lambda: True, lifecycle_factory=lambda: (constructed.append(True) or _FakeLifecycle()), pre_spawn_authority=authority, quota_contract=_contract(), native_verifier_run=rejected)
    assert result["verdict"] == "ABORTED"
    assert result["fresh_native_stage_a_receipt_sha256"] is None
    assert "pinned native Stage-A verifier rejected" in str(result["failure"])
    assert constructed == []
    sealed = json.loads((root / "runtime/local-shakedown-abort.json").read_text(encoding="ascii"))
    assert sealed == result
