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


def _native_success_terminal() -> dict[str, object]:
    identity = {"predecessor_boundary_configuration": {"epoch_number": 0},
                "successor_configuration": {"epoch_number": 1, "tree_id": 0, "epoch_digest": "b" * 64},
                "activation_delay_blocks": 5, "command_block_height": 21,
                "activation_height": 26}
    return {"run_id": "native-run", "source_kind": "adaptation_manager",
            "event_type": "adaptive_v3.readiness_terminal",
            "payload": {"terminal_reason": 1, "terminal_cycle_ordinal": 0,
                        "disposition": "session_terminal", "required_release_count": 31,
                        "observed_signers": list(range(31)), "terminal_bundle_digest": "a" * 64,
                        "identity": identity, "terminal_identity": identity}}


def test_success_gate_accepts_native_v3_all_live_terminal(tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/manager-events.jsonl").write_text(json.dumps(_native_success_terminal()) + "\n")
    assert subject._manager_success_terminal(tmp_path, "native-run")


@pytest.mark.parametrize("mode", ["duplicate", "retry_exhausted", "partial_release", "wrong_epoch", "legacy"])
def test_success_gate_rejects_non_native_or_incomplete_terminal(tmp_path: Path, mode: str) -> None:
    event = _native_success_terminal()
    if mode == "retry_exhausted":
        event["payload"]["terminal_reason"] = 4
    elif mode == "partial_release":
        event["payload"]["required_release_count"] = 21
        event["payload"]["observed_signers"] = list(range(21))
    elif mode == "wrong_epoch":
        event["payload"]["identity"]["successor_configuration"]["epoch_number"] = 2
    elif mode == "legacy":
        event["event_type"] = "adaptive_v2_session_terminal"
        event["payload"] = {"outcome": "advanced", "reason": "successor_converged", "successor_epoch_number": 1}
    (tmp_path / "raw").mkdir()
    raw = json.dumps(event) + "\n"
    (tmp_path / "raw/manager-events.jsonl").write_text(raw * (2 if mode == "duplicate" else 1))
    assert not subject._manager_success_terminal(tmp_path, "native-run")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


_MAIN_CONFIG = b"block-size = 1\nfan-out = 5\nasync_blocks = 2\npiped_latency = 1\nbase-timeout = 2.0\nprop-delay = 0.1\naggregation-timeout = 0.5\nleader-progress-timeout = 5.0\nleader-activation-grace = 1.0\ntree-switch-period = 2\n"


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
        replica = int(relative.split("-")[-1].split(".")[0]) if relative.startswith("config/replica-") else None
        path.write_bytes(_MAIN_CONFIG if relative == "config/hotstuff.gen.conf" else (f"privkey = key\ntls-privkey = tls-key\ntls-cert = {replica:064x}\nidx = {replica}\n".encode("ascii") if replica is not None else relative.encode("ascii")))
        artifacts[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    transition = {"policy_intent": "performance_optimization", "evidence_window_rule": "fresh_exact_predecessor_after_common_commit", "transition_artifact_id": "e0-to-e1-operator-capacity", "bundle_path": "transitions/e0-to-e1-operator-capacity/successor.bundle", "evidence_snapshot_path": "transitions/e0-to-e1-operator-capacity/evidence-snapshot.json", "predecessor_epoch_number": 0, "successor_epoch_number": 1, "minimum_predecessor_residency_ms": 0, "minimum_post_baseline_observation_ms": 0, "apply_shape_selection": False, "policy_parameters": {}}
    binaries = {}
    for name in stage_a.REQUIRED_BINARIES:
        path = tmp_path / f"binary-{name}"
        if name == "identity_parity_verifier":
            path.write_text("""#!/usr/bin/env python3
import hashlib,json,sys
args=sys.argv
bundle=open(args[args.index('--identity-bundle')+1],'rb').read()
source=args[args.index('--source-revision')+1]
fingerprint=args[args.index('--expected-public-fingerprint')+1]
output=args[args.index('--output')+1]
receipt={'schema_version':1,'kind':'kauri-operator-capacity-native-identity-parity-receipt-v1','verdict':'NATIVE_IDENTITY_PARITY_VERIFIED_NO_EXECUTION','source_revision':source,'identity_bundle_sha256':hashlib.sha256(bundle).hexdigest(),'public_identity_fingerprint':fingerprint,'bls_replicas':31,'tls_identities':32}
open(output,'w').write(json.dumps(receipt,sort_keys=True,separators=(',',':'))+'\\n')
""", encoding="ascii")
            path.chmod(0o700)
        else:
            path.write_bytes(name.encode("ascii"))
        binaries[name] = path
    binary_sha = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in binaries.items()}
    plan_binary_sha = {name: binary_sha[name] for name in ("adaptation_manager", "hotstuff_app", "identity_parity_verifier")}
    approval = tmp_path / "tool-approval.json"
    _write(approval, {"schema_version": 1, "kind": "kauri-n31-operator-capacity-tool-identity-approval-v1", "verdict": "EXTERNAL_TOOL_IDENTITY_APPROVED", "revision": "a" * 40, "approval_ref": "external-test", "approved_at_utc": "2026-09-29T00:00:00Z", "binary_sha256": binary_sha})
    manager = [str(binaries["adaptation_manager"]), "--protocol-mode", "adaptive_v3", "--listen", "127.0.0.1:20000",
               "--tls-privkey", "d" * 64, "--tls-cert", "e" * 64, "--issuer-id", "1",
               "--issuer-private-key", "f" * 64, "--activation-delay-blocks", "5",
               "--convergence-deadline-seconds", "30", "--tree-fanout", "5", "--pipeline-stretch", "2",
               "--shape-candidate-fanouts", "5", "--shape-deterministic-seed", "1",
               "--transition-request", json.dumps(transition, sort_keys=True, separators=(",", ":")),
               "--bundle-output", str(root / "transitions/e0-to-e1-operator-capacity/successor.bundle"),
               "--epoch-zero-tree-file", str(root / "config/epoch0.tree"), "--structured-event-run-id", "test-run",
               "--structured-event-source-instance", "test-source", "--structured-event-output", str(root / "raw/manager-events.jsonl"),
               "--activation-readiness-release-count", "31", "--activation-readiness-maximum-delivery-attempts", "1",
               "--activation-readiness-retry-interval-ticks", "30000", "--operator-capacity-stage-a-envelope", str(root / "config/stage-a-envelope.wire"),
               "--operator-capacity-stage-a-wire-sha256", hashlib.sha256((root / "config/stage-a-envelope.wire").read_bytes()).hexdigest(),
               "--operator-capacity-label-issuer-id", "7", "--operator-capacity-label-issuer-reference", "test",
               "--operator-capacity-label-issuer-public-key-hex", "a" * 66,
               "--operator-capacity-label-issuer-public-key-fingerprint", "b" * 64,
               "--operator-capacity-approved-capacity-digest", "c" * 64,
               "--operator-capacity-hard-deadline-ns", "1000000000",
               "--operator-capacity-stage-b-authorization-output", str(root / "raw/stage-b-authorization.wire"),
               "--operator-capacity-consumption-output", str(root / "raw/consumption.json"),
               "--operator-capacity-stage-b-issuer-reference", "test-stage-b"]
    for replica in range(31):
        manager.extend(("--replica", f"{replica},127.0.0.1:{21000 + replica},{replica:064x}",
                        "--activation-readiness-member", f"{replica},{replica + 31:064x}"))
    replicas = [[str(binaries["hotstuff_app"]), "--conf", str(root / "config/hotstuff.gen.conf"), "--conf", str(root / f"config/replica-{i}.conf"),
                 "--structured-event-run-id", "test-run", "--structured-event-source-instance", f"test-source-replica-{i}",
                 "--structured-event-output", str(root / f"raw/replica-{i}.jsonl"), "--structured-event-commit-observer-id", "replica-0",
                 "--structured-event-commit-observer-instance", "test-source-replica-0"] for i in range(31)]
    main = _MAIN_CONFIG + f"nworker = 2\nrepnworker = 2\npace-maker = dummy\nproposer = 0\nclient-ip = 127.0.0.1\ntree-generation = file\ntree-generation-fpath = {root / 'config/epoch0.tree'}\nepoch-protocol-mode = adaptive_v3\nepoch-change-issuer-id = 1\nepoch-change-issuer-public-key = 02abcd\nepoch-change-minimum-activation-delay = 5\nepoch-change-maximum-activation-delay = 5\nepoch-change-maximum-block-extra-bytes = 4096\nepoch-change-maximum-ancestry-blocks = 128\nepoch-manager-address = 127.0.0.1:20000\nepoch-manager-tls-cert = {'e' * 64}\nmax-rep-msg = 4194304\n".encode("ascii") + b"".join(f"replica = 127.0.0.1:{21000 + replica};{22000 + replica}, {replica + 31:064x}, {replica + 62:064x}\n".encode("ascii") for replica in range(31))
    main += b"".join(f"activation-readiness-member = {replica},{replica + 31:064x}\n".encode("ascii")
                     for replica in range(31))
    main += b"experiment-exact-timeout-attempt-evidence-v3 = true\n"
    (root / "config/hotstuff.gen.conf").write_bytes(main); artifacts["config/hotstuff.gen.conf"] = hashlib.sha256(main).hexdigest()
    bls = [f"{replica + 31:064x}" for replica in range(31)]
    tls = [(f"{replica:064x}", f"{replica + 62:064x}") for replica in range(31)] + [("e" * 64, "d" * 64)]
    public_fingerprint = backend._public_identity_fingerprint(bls, tls, "02abcd")
    bundle_sha = hashlib.sha256(backend._identity_bundle_bytes(bls=bls, bls_secrets=["key"] * 31, tls=tls, tls_secrets=["tls-key"] * 31 + ["d" * 64], issuer_public_key="02abcd", issuer_secret="f" * 64)).hexdigest()
    receipt = {"schema_version": 1, "kind": "kauri-operator-capacity-native-identity-parity-receipt-v1", "verdict": "NATIVE_IDENTITY_PARITY_VERIFIED_NO_EXECUTION", "source_revision": "a" * 40, "identity_bundle_sha256": bundle_sha, "public_identity_fingerprint": public_fingerprint, "bls_replicas": 31, "tls_identities": 32}
    projection = {"schema_version": 1, "kind": "kauri-operator-capacity-materialized-public-identity-v1", "bls_public_keys": bls, "replica_tls": [{"certificate": certificate, "common_name": common_name} for certificate, common_name in tls[:-1]], "manager_tls": {"certificate": tls[-1][0], "common_name": tls[-1][1]}, "issuer_public_key": "02abcd"}
    for relative, value in (("config/identity-parity-receipt.json", receipt), ("config/identity-public-projection.json", projection)):
        path = root / relative; path.write_bytes(_canonical(value)); artifacts[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    verifier_arguments = ["--epoch0-tree-file", str(root / "config/epoch0.tree"), "--stage-a-envelope-wire", str(root / "config/stage-a-envelope.wire"), "--issuer-id", "7", "--issuer-reference", "test", "--issuer-public-key-hex", "a" * 66, "--issuer-public-key-fingerprint", "b" * 64, "--approved-capacity-digest", "c" * 64, "--arm", "fast_priority_treatment", "--source-revision", "a" * 40]
    manifest = {"schema_version": 1, "kind": "kauri-n31-operator-capacity-v3-materialization-v1", "verdict": "MATERIALIZED_NO_EXECUTION", "claim_eligible": False, "figure_eligible": False, "arm": "treatment", "stage_a_native_arm": "fast_priority_treatment", "protocol": {"N": 31, "Q": 21, "tree_count": 21}, "slow_root_ids": list(range(6)), "revision": "a" * 40, "epoch0_tree": {"sha256": artifacts["config/epoch0.tree"], "topology_digest": "c" * 64}, "binary_sha256": plan_binary_sha, "artifact_sha256": artifacts, "manager_argv_sha256": backend._argv_digest(manager), "replica_argv_sha256": [backend._argv_digest(row) for row in replicas], "synthetic_workload": {"kind": "replica-local-synthetic-v1", "source_revision": "a" * 40, "hotstuff_app_sha256": plan_binary_sha["hotstuff_app"], "main_config_sha256": artifacts["config/hotstuff.gen.conf"], "block_size": 1, "initial_beat_delay_ms": 10000, "beat_interval_ms": 50, "transaction_count_per_block": 1, "post_e1_window_ns": 30 * 1_000_000_000}, "stage_a_envelope_sha256": artifacts["config/stage-a-envelope.wire"], "stage_a_verifier_receipt_sha256": "1" * 64, "stage_a_verifier_arguments": verifier_arguments, "identity_parity_receipt_sha256": artifacts["config/identity-parity-receipt.json"], "public_identity_fingerprint": public_fingerprint, "tool_identity_approval_receipt_sha256": hashlib.sha256(approval.read_bytes()).hexdigest(), "stage_b_authorization_output": "raw/stage-b-authorization.wire", "consumption_output": "raw/consumption.json", "bundle_output": "transitions/e0-to-e1-operator-capacity/successor.bundle"}
    _write(root / "materialization-manifest.json", manifest)
    quota = tmp_path / "quota.json"
    quota.write_bytes(_canonical(_EXPECTED_QUOTA_PROFILE))
    return root, manager, replicas, quota, approval, binaries


class _FakeLifecycle:
    def __init__(self, *, terminal: bool = True) -> None:
        self.calls: list[str] = []
        self.terminal = terminal
        self.terminated = False
    def start_replica(self, replica_id: int, argv: list[str], log: Path) -> None: self.calls.append(f"replica-{replica_id}")
    def start_manager(self, argv: list[str], log: Path) -> None: self.calls.append("manager")
    def await_e1_measurement_window(self, deadline_monotonic: float) -> dict[str, object]: self.calls.append("e1-window"); return {"activated_replica_ids": list(range(31)), "post_e1_commit": {"block_height": 1}}
    def manager_exit_status(self) -> int | None:
        self.calls.append("manager-status")
        return -15 if self.terminated else None
    def manager_success_terminal_verified(self) -> bool:
        self.calls.append("terminal")
        return self.terminal
    def stop_monitor(self) -> None: self.calls.append("stop-monitor")
    def terminate_manager_and_replicas(self) -> None:
        self.calls.append("terminate-processes")
        self.terminated = True
    def terminate_owned_replica_scopes(self, deadline_monotonic: float) -> dict[str, object]: self.calls.append("terminate-scopes"); return {"units": []}
    def verify_scope_cleanup(self, deadline_monotonic: float) -> dict[str, object]: self.calls.append("verify-cleanup"); return {"verified": True}


def _contract() -> cpu_quota.CpuQuotaContract:
    assignments = tuple(cpu_quota.CpuQuotaAssignment(replica, "slow" if replica < 6 else "fast", 25 if replica < 6 else 100) for replica in range(31))
    return cpu_quota.CpuQuotaContract(schema_version=1, contract_id="n31-static-resource-cpu-sham-quota-v1", enabled=True, figure_eligible=False, launcher="systemd-user-scope-cpu-quota-v1", manager_visibility="none", sampling_interval_ms=1000, base_profile_id="n31-static-resource-cpu-sham-v1", base_profile_sha256="285aa55cb33637009ccd491d74830cd7485bcd83cb993c33c488dbff6fe4bf09", base_profile_canonical_sha256="2ed182ed95fe8514c80eb861ed2e86654afaaf6b881a2ede6fc1a03d0565b766", assignments=assignments, contract_sha256=hashlib.sha256(_canonical(_EXPECTED_QUOTA_PROFILE)).hexdigest())


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


def test_pre_spawn_rejects_changed_synthetic_main_config(tmp_path: Path) -> None:
    root, _manager, _replicas, _quota, _approval, _binaries = _fixture(tmp_path)
    config = root / "config/hotstuff.gen.conf"
    config.write_bytes(config.read_bytes() + b" async_blocks=200\n")
    with pytest.raises(subject.OperatorCapacityV3LocalRunnerError, match="synthetic workload configuration changed"):
        subject._verify_synthetic_config_before_spawn(root, _manager)


def test_factory_config_mutation_reaches_zero_replica_starts(tmp_path: Path) -> None:
    root, manager, replicas, quota, approval, _binaries = _fixture(tmp_path)
    authority = _authority(tmp_path, root, quota, approval, _binaries)
    _bind_materialization_to_authority(root, authority)
    _plan, request, receipt = _authorized(root, manager, replicas, quota)
    lifecycle = _FakeLifecycle()
    def factory() -> _FakeLifecycle:
        config = root / "config/hotstuff.gen.conf"
        config.write_bytes(config.read_bytes() + b" async_blocks=200\n")
        return lifecycle
    result = subject.execute_excluded_local_shakedown(
        materialization_root=root, manager_argv=manager, replica_argv=replicas,
        quota_profile=quota, authorization_request=request, authorization_receipt=receipt,
        tool_identity_approval_path=approval, execute=True, timeout_s=1200,
        current_revision=lambda: "a" * 40, worktree_clean=lambda: True,
        lifecycle_factory=factory, pre_spawn_authority=authority, quota_contract=_contract(),
        native_verifier_run=_native_verifier_copying_retained_receipt(authority))
    assert result["verdict"] == "ABORTED"
    assert not any(call.startswith("replica-") or call == "manager" for call in lifecycle.calls)


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
    assert (root / "runtime/stage-a-verifier-receipt.json").read_bytes() == Path(str(authority["native_receipt"])).read_bytes()
    assert lifecycle.calls[:33] == [*(f"replica-{replica}" for replica in range(31)), "manager", "e1-window"]
    assert lifecycle.calls[-7:] == ["terminal", "manager-status", "stop-monitor", "terminate-processes", "manager-status", "terminate-scopes", "verify-cleanup"]
    assert result["e1_measurement_window"] == {"activated_replica_ids": list(range(31)), "post_e1_commit": {"block_height": 1}}
    assert result["manager_exit_code"] is None
    assert result["manager_exit_code_after_cleanup"] == -15
    assert result["manager_success_terminal_verified"] is True
    assert (root / "runtime/frozen-cpu-quota-contract.json").read_bytes() == quota.read_bytes()
    assert json.loads((root / "runtime/manager-argv.json").read_text(encoding="ascii")) == manager


def test_live_manager_is_cleaned_after_window_without_waiting_for_spontaneous_exit(tmp_path: Path) -> None:
    root, manager, replicas, quota, approval, binaries = _fixture(tmp_path)
    authority = _authority(tmp_path, root, quota, approval, binaries)
    _bind_materialization_to_authority(root, authority)
    _plan, request, receipt = _authorized(root, manager, replicas, quota)
    lifecycle = _FakeLifecycle()
    result = subject.execute_excluded_local_shakedown(
        materialization_root=root, manager_argv=manager, replica_argv=replicas,
        quota_profile=quota, authorization_request=request, authorization_receipt=receipt,
        tool_identity_approval_path=approval, execute=True, timeout_s=1200,
        current_revision=lambda: "a" * 40, worktree_clean=lambda: True,
        lifecycle_factory=lambda: lifecycle, pre_spawn_authority=authority,
        quota_contract=_contract(), native_verifier_run=_native_verifier_copying_retained_receipt(authority),
    )
    assert result["verdict"] == "PROCESS_COMPLETED_PENDING_RAW_VALIDATION"
    assert "wait" not in lifecycle.calls
    assert result["manager_exit_code"] is None
    assert result["manager_exit_code_after_cleanup"] == -15


def test_missing_successor_terminal_seals_abort_even_if_window_completes(tmp_path: Path) -> None:
    root, manager, replicas, quota, approval, binaries = _fixture(tmp_path)
    authority = _authority(tmp_path, root, quota, approval, binaries)
    _bind_materialization_to_authority(root, authority)
    _plan, request, receipt = _authorized(root, manager, replicas, quota)
    lifecycle = _FakeLifecycle(terminal=False)
    result = subject.execute_excluded_local_shakedown(
        materialization_root=root, manager_argv=manager, replica_argv=replicas,
        quota_profile=quota, authorization_request=request, authorization_receipt=receipt,
        tool_identity_approval_path=approval, execute=True, timeout_s=1200,
        current_revision=lambda: "a" * 40, worktree_clean=lambda: True,
        lifecycle_factory=lambda: lifecycle, pre_spawn_authority=authority,
        quota_contract=_contract(), native_verifier_run=_native_verifier_copying_retained_receipt(authority),
    )
    assert result["verdict"] == "ABORTED"
    assert "successor-converged terminal" in str(result["failure"])
    assert result["manager_success_terminal_verified"] is False


def test_frozen_profile_archive_requires_exact_contract_and_authority_bytes(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"; runtime.mkdir()
    profile = tmp_path / "quota.json"; profile.write_bytes(_canonical(_EXPECTED_QUOTA_PROFILE))
    authority_profile = tmp_path / "authority-quota.json"; authority_profile.write_bytes(profile.read_bytes())
    assert subject._archive_frozen_quota_contract(
        quota_profile=profile, contract=_contract(), authority={"quota_profile": authority_profile}, runtime=runtime,
    ) == hashlib.sha256(profile.read_bytes()).hexdigest()
    assert (runtime / "frozen-cpu-quota-contract.json").read_bytes() == profile.read_bytes()
    authority_profile.write_bytes(b"{}\n")
    with pytest.raises(subject.OperatorCapacityV3LocalRunnerError, match="pre-spawn authority"):
        subject._archive_frozen_quota_contract(
            quota_profile=profile, contract=_contract(), authority={"quota_profile": authority_profile}, runtime=tmp_path,
        )


def test_quota_monitor_requires_a_complete_round_after_raw_window_phase_offset(tmp_path: Path) -> None:
    raw = tmp_path / "raw"; raw.mkdir()
    lifecycle = object.__new__(subject.CpuQuotaLocalLifecycle)
    lifecycle._root = tmp_path
    def append_round(timestamp: int) -> None:
        with (raw / "cpu-quota-samples.jsonl").open("a", encoding="ascii") as handle:
            for replica in range(31):
                handle.write(json.dumps({"source_monotonic_ns": timestamp, "replica_id": replica}) + "\n")
        with (raw / "cpu-quota-monitor-rounds.jsonl").open("a", encoding="ascii") as handle:
            handle.write(json.dumps({"sample_monotonic_ns": timestamp,
                                     "finished_monotonic_ns": timestamp + 1}) + "\n")
    append_round(99)
    assert lifecycle._complete_quota_round_at_or_after(100) is False
    append_round(100)
    assert lifecycle._complete_quota_round_at_or_after(100) is True

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


def test_e1_measurement_window_requires_every_replica_activation_and_post_e1_observer_commit(tmp_path: Path) -> None:
    root = tmp_path
    raw = root / "raw"; raw.mkdir()
    for replica in range(31):
        event = {"source_kind": "replica", "source_id": f"replica-{replica}",
                 "source_sequence": 1, "source_monotonic_ns": 100 + replica,
                 "event_type": "epoch.activated", "payload": {"epoch_number": 1,
                 "tree_id": 0, "epoch_digest": "a" * 64, "activation_height": 5,
                 "certificate_apply_committed_height": 4,
                 "activation_readiness_certificate_digest": "c" * 64}}
        (raw / f"replica-{replica}.jsonl").write_text(json.dumps(event) + "\n", encoding="ascii")
    assert subject._e1_measurement_window(root) is None
    with (raw / "replica-0.jsonl").open("a", encoding="ascii") as handle:
        handle.write(json.dumps({"source_kind": "replica", "source_id": "replica-0", "source_monotonic_ns": 200,
                                 "event_type": "block.committed",
                                 "payload": {"decision_proof": {"epoch_number": 1,
                                             "tree_id": 0, "epoch_digest": "a" * 64,
                                             "block_hash": "b" * 64}, "designated_observer": True,
                                             "block_height": 7}}) + "\n")
    result = subject._e1_measurement_window(root)
    assert result is not None
    assert result["activated_replica_ids"] == list(range(31))
    assert result["all_replica_e1_activation_monotonic_ns"] == 130
    assert result["post_e1_window_start_monotonic_ns"] == 130
    assert result["post_e1_window_end_monotonic_ns"] == 130 + subject._POST_E1_WINDOW_NS
    assert result["post_e1_commit"] == {
        "block_height": 7, "source_monotonic_ns": 200, "epoch_digest": "a" * 64,
    }


def test_e1_measurement_window_rejects_legacy_nested_activation_or_nonreplica_source(tmp_path: Path) -> None:
    root = tmp_path
    raw = root / "raw"; raw.mkdir()
    for replica in range(31):
        payload = {"epoch_number": 1, "tree_id": 0, "epoch_digest": "a" * 64,
                   "activation_height": 5}
        if replica == 7:
            payload = {"configuration": payload}
        event = {"source_kind": "replica", "source_id": f"replica-{replica}",
                 "event_type": "epoch.activated", "payload": payload}
        (raw / f"replica-{replica}.jsonl").write_text(json.dumps(event) + "\n", encoding="ascii")
    with (raw / "replica-0.jsonl").open("a", encoding="ascii") as handle:
        handle.write(json.dumps({"source_kind": "manager", "source_id": "replica-0",
                                 "event_type": "block.committed",
                                 "payload": {"decision_proof": {"epoch_number": 1},
                                             "designated_observer": True, "block_height": 7}}) + "\n")
    assert subject._e1_measurement_window(root) is None


def test_manager_success_terminal_requires_run_bound_e1_success(tmp_path: Path) -> None:
    raw = tmp_path / "raw"; raw.mkdir()
    terminal = _native_success_terminal()
    terminal["run_id"] = "run-1"
    (raw / "manager-events.jsonl").write_text(json.dumps(terminal) + "\n", encoding="ascii")
    assert subject._manager_success_terminal(tmp_path, "run-1") is True
    assert subject._manager_success_terminal(tmp_path, "other-run") is False
    terminal["payload"]["identity"]["successor_configuration"]["epoch_number"] = 2
    (raw / "manager-events.jsonl").write_text(json.dumps(terminal) + "\n", encoding="ascii")
    assert subject._manager_success_terminal(tmp_path, "run-1") is False


def test_e1_measurement_window_rejects_commit_before_raw_anchor_or_different_e1_digest(tmp_path: Path) -> None:
    raw = tmp_path / "raw"; raw.mkdir()
    for replica in range(31):
        digest = "b" * 64 if replica == 30 else "a" * 64
        event = {"source_kind": "replica", "source_id": f"replica-{replica}",
                 "source_sequence": 1, "source_monotonic_ns": 100 + replica,
                 "event_type": "epoch.activated", "payload": {"epoch_number": 1,
                 "tree_id": 0, "epoch_digest": digest, "activation_height": 5}}
        (raw / f"replica-{replica}.jsonl").write_text(json.dumps(event) + "\n", encoding="ascii")
    with (raw / "replica-0.jsonl").open("a", encoding="ascii") as handle:
        handle.write(json.dumps({"source_kind": "replica", "source_id": "replica-0",
                                 "source_monotonic_ns": 129, "event_type": "block.committed",
                                 "payload": {"decision_proof": {"epoch_number": 1, "epoch_digest": "a" * 64},
                                             "designated_observer": True, "block_height": 1}}) + "\n")
    assert subject._e1_measurement_window(tmp_path) is None
