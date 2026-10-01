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


def test_manager_grammar_accepts_native_variable_width_tls_and_bls(tmp_path: Path) -> None:
    root, manager, _replicas, _quota = _fixture(tmp_path)
    manager[manager.index("--tls-privkey") + 1] = "ab" * 1218
    manager[manager.index("--tls-cert") + 1] = "cd" * 742
    for index in range(1, len(manager), 2):
        if manager[index] == "--replica":
            replica, endpoint, _certificate = manager[index + 1].split(",", 2)
            manager[index + 1] = f"{replica},{endpoint},{'ef' * 742}"
        elif manager[index] == "--activation-readiness-member":
            replica, _public_key = manager[index + 1].split(",", 1)
            manager[index + 1] = f"{replica},{'ab' * 48}"
    subject.validate_materialized_manager_argv(
        root=root, manager_argv=manager,
        expected_sha256=subject._argv_digest(manager),
    )


@pytest.mark.parametrize("encoded", ["a", "abc", "zz", "ab" * 16385],
                         ids=["odd-short", "odd-long", "nonhex", "oversized"])
def test_native_key_encoding_rejects_odd_nonhex_or_oversized(encoded: str) -> None:
    with pytest.raises(subject.OperatorCapacityV3BackendError):
        subject._hex_public(encoded, "native encoding")


@pytest.mark.parametrize("mode", ["missing", "disabled"])
def test_backend_requires_native_v3_response_evidence(tmp_path: Path, mode: str) -> None:
    root, manager, _replicas, _quota = _fixture(tmp_path)
    raw = (root / "config/hotstuff.gen.conf").read_bytes()
    raw = raw.replace(b"experiment-exact-timeout-attempt-evidence-v3 = true\n", b"")
    if mode == "disabled":
        raw += b"experiment-exact-timeout-attempt-evidence-v3 = false\n"
    with pytest.raises(subject.OperatorCapacityV3BackendError):
        subject.validate_synthetic_main_config(raw, root=root, manager_argv=manager)


@pytest.mark.parametrize("mode", ["missing", "duplicate", "mismatch"])
def test_backend_rejects_readiness_membership_drift(tmp_path: Path, mode: str) -> None:
    root, manager, _replicas, _quota = _fixture(tmp_path)
    raw = (root / "config/hotstuff.gen.conf").read_bytes()
    row = f"activation-readiness-member = 0,{31:064x}\n".encode("ascii")
    if mode == "missing":
        raw = raw.replace(row, b"")
    elif mode == "duplicate":
        raw += row
    else:
        raw = raw.replace(row, b"activation-readiness-member = 0," + b"f" * 64 + b"\n")
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="readiness membership"):
        subject.validate_synthetic_main_config(raw, root=root, manager_argv=manager)


@pytest.mark.parametrize("flag,value", [
    ("--activation-readiness-release-count", "21"),
    ("--activation-readiness-retry-interval-ticks", "1"),
    ("--activation-readiness-maximum-delivery-attempts", "2"),
])
def test_backend_rejects_incompatible_all_live_delivery_settings(tmp_path: Path, flag: str, value: str) -> None:
    root, manager, _replicas, _quota = _fixture(tmp_path)
    manager[manager.index(flag) + 1] = value
    with pytest.raises(subject.OperatorCapacityV3BackendError):
        subject.validate_materialized_manager_argv(
            root=root, manager_argv=manager, expected_sha256=subject._argv_digest(manager))


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


_MAIN_CONFIG = b"block-size = 1\nfan-out = 5\nasync_blocks = 2\npiped_latency = 1\nbase-timeout = 2.0\nprop-delay = 0.1\naggregation-timeout = 0.5\nleader-progress-timeout = 5.0\nleader-activation-grace = 1.0\ntree-switch-period = 2\n"


def _fixture(tmp_path: Path) -> tuple[Path, list[str], list[list[str]], Path]:
    root = tmp_path / "materialized"
    (root / "config").mkdir(parents=True, mode=0o700)
    (root / "raw").mkdir(mode=0o700)
    (root / "transitions/e0-to-e1-operator-capacity").mkdir(parents=True, mode=0o700)
    artifacts: dict[str, str] = {}
    for relative in ("config/epoch0.tree", "config/stage-a-envelope.wire", "config/hotstuff.gen.conf", *[f"config/replica-{i}.conf" for i in range(31)]):
        path = root / relative
        replica = int(relative.split("-")[-1].split(".")[0]) if relative.startswith("config/replica-") else None
        path.write_bytes(_MAIN_CONFIG if relative == "config/hotstuff.gen.conf" else (f"privkey = key\ntls-privkey = tls-key\ntls-cert = {replica:064x}\nidx = {replica}\n".encode("ascii") if replica is not None else relative.encode("ascii")))
        artifacts[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    transition = {
        "policy_intent": "performance_optimization", "evidence_window_rule": "fresh_exact_predecessor_after_common_commit",
        "transition_artifact_id": "e0-to-e1-operator-capacity", "bundle_path": "transitions/e0-to-e1-operator-capacity/successor.bundle",
        "evidence_snapshot_path": "transitions/e0-to-e1-operator-capacity/evidence-snapshot.json",
        "predecessor_epoch_number": 0, "successor_epoch_number": 1, "minimum_predecessor_residency_ms": 0,
        "minimum_post_baseline_observation_ms": 0, "apply_shape_selection": False, "policy_parameters": {},
    }
    manager = ["/bin/manager", "--protocol-mode", "adaptive_v3", "--listen", "127.0.0.1:20000",
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
               "--operator-capacity-stage-a-wire-sha256", artifacts["config/stage-a-envelope.wire"],
               "--operator-capacity-label-issuer-id", "1", "--operator-capacity-label-issuer-reference", "test",
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
    main = _MAIN_CONFIG + f"nworker = 2\nrepnworker = 2\npace-maker = dummy\nproposer = 0\nclient-ip = 127.0.0.1\ntree-generation = file\ntree-generation-fpath = {root / 'config/epoch0.tree'}\nepoch-protocol-mode = adaptive_v3\nepoch-change-issuer-id = 1\nepoch-change-issuer-public-key = 02abcd\nepoch-change-minimum-activation-delay = 5\nepoch-change-maximum-activation-delay = 5\nepoch-change-maximum-block-extra-bytes = 4096\nepoch-change-maximum-ancestry-blocks = 128\nepoch-manager-address = 127.0.0.1:20000\nepoch-manager-tls-cert = {'e' * 64}\nmax-rep-msg = 4194304\n".encode("ascii") + b"".join(f"replica = 127.0.0.1:{21000 + replica};{22000 + replica}, {replica + 31:064x}, {replica + 62:064x}\n".encode("ascii") for replica in range(31))
    main += b"".join(f"activation-readiness-member = {replica},{replica + 31:064x}\n".encode("ascii")
                     for replica in range(31))
    main += b"experiment-exact-timeout-attempt-evidence-v3 = true\n"
    (root / "config/hotstuff.gen.conf").write_bytes(main); artifacts["config/hotstuff.gen.conf"] = hashlib.sha256(main).hexdigest()
    bls = [f"{replica + 31:064x}" for replica in range(31)]
    tls = [(f"{replica:064x}", f"{replica + 62:064x}") for replica in range(31)] + [("e" * 64, "d" * 64)]
    public_fingerprint = subject._public_identity_fingerprint(bls, tls, "02abcd")
    bundle_sha = hashlib.sha256(subject._identity_bundle_bytes(bls=bls, bls_secrets=["key"] * 31, tls=tls, tls_secrets=["tls-key"] * 31 + ["d" * 64], issuer_public_key="02abcd", issuer_secret="f" * 64)).hexdigest()
    receipt = {"schema_version": 1, "kind": "kauri-operator-capacity-native-identity-parity-receipt-v1", "verdict": "NATIVE_IDENTITY_PARITY_VERIFIED_NO_EXECUTION", "source_revision": "a" * 40, "identity_bundle_sha256": bundle_sha, "public_identity_fingerprint": public_fingerprint, "bls_replicas": 31, "tls_identities": 32}
    projection = {"schema_version": 1, "kind": "kauri-operator-capacity-materialized-public-identity-v1", "bls_public_keys": bls, "replica_tls": [{"certificate": certificate, "common_name": common_name} for certificate, common_name in tls[:-1]], "manager_tls": {"certificate": tls[-1][0], "common_name": tls[-1][1]}, "issuer_public_key": "02abcd"}
    for relative, value in (("config/identity-parity-receipt.json", receipt), ("config/identity-public-projection.json", projection)):
        path = root / relative; path.write_bytes(_canonical(value)); artifacts[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    replicas = [["/bin/app", "--conf", str(root / "config/hotstuff.gen.conf"), "--conf", str(root / f"config/replica-{i}.conf"),
                 "--structured-event-run-id", "test-run", "--structured-event-source-instance", f"test-source-replica-{i}",
                 "--structured-event-output", str(root / f"raw/replica-{i}.jsonl"), "--structured-event-commit-observer-id", "replica-0",
                 "--structured-event-commit-observer-instance", "test-source-replica-0"] for i in range(31)]
    verifier_arguments = ["--epoch0-tree-file", str(root / "config/epoch0.tree"), "--stage-a-envelope-wire", str(root / "config/stage-a-envelope.wire"), "--issuer-id", "1", "--issuer-reference", "test", "--issuer-public-key-hex", "a" * 66, "--issuer-public-key-fingerprint", "b" * 64, "--approved-capacity-digest", "c" * 64, "--arm", "fast_priority_treatment", "--source-revision", "a" * 40]
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
        "synthetic_workload": {"kind": "replica-local-synthetic-v1", "source_revision": "a" * 40,
                               "hotstuff_app_sha256": "e" * 64, "main_config_sha256": artifacts["config/hotstuff.gen.conf"],
                               "block_size": 1, "initial_beat_delay_ms": 10000, "beat_interval_ms": 50,
                               "transaction_count_per_block": 1, "post_e1_window_ns": 30 * 1_000_000_000},
        "stage_a_verifier_receipt_sha256": "1" * 64, "stage_a_verifier_arguments": verifier_arguments, "identity_parity_receipt_sha256": artifacts["config/identity-parity-receipt.json"],
        "public_identity_fingerprint": public_fingerprint,
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
    root, manager, replicas, quota = _fixture(tmp_path)
    manager[manager.index("--bundle-output") + 1] = str(root / "elsewhere.bundle")
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


def test_backend_rejects_synthetic_block_size_drift_even_when_self_described(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    (root / "config/hotstuff.gen.conf").write_text("block-size = 2\n", encoding="ascii")
    manifest["artifact_sha256"]["config/hotstuff.gen.conf"] = hashlib.sha256((root / "config/hotstuff.gen.conf").read_bytes()).hexdigest()
    manifest["synthetic_workload"]["main_config_sha256"] = manifest["artifact_sha256"]["config/hotstuff.gen.conf"]
    manifest["synthetic_workload"]["block_size"] = 2
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="synthetic workload identity"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_extra_block_size_directive(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    config = root / "config/hotstuff.gen.conf"
    config.write_bytes(config.read_bytes() + b" block-size=2\n")
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    manifest["artifact_sha256"]["config/hotstuff.gen.conf"] = digest
    manifest["synthetic_workload"]["main_config_sha256"] = digest
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="repeats a singleton key"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_whitespace_normalized_async_cadence_override(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    config = root / "config/hotstuff.gen.conf"
    config.write_bytes(config.read_bytes() + b" async_blocks = 200\n")
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    manifest["artifact_sha256"]["config/hotstuff.gen.conf"] = digest
    manifest["synthetic_workload"]["main_config_sha256"] = digest
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="repeats a singleton key"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


@pytest.mark.parametrize("line", ["nworker = 9", "repnworker = 9", "pace-maker = rotating", "epoch-protocol-mode = legacy_static"])
def test_backend_rejects_self_consistent_frozen_main_option_drift(tmp_path: Path, line: str) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    config = root / "config/hotstuff.gen.conf"
    config.write_bytes(config.read_bytes().replace(line.split(" = ")[0].encode("ascii") + b" = " + ({"nworker": b"2", "repnworker": b"2", "pace-maker": b"dummy", "epoch-protocol-mode": b"adaptive_v3"}[line.split(" = ")[0]]), line.encode("ascii"), 1))
    manifest_path = root / "materialization-manifest.json"; manifest = json.loads(manifest_path.read_text())
    digest = hashlib.sha256(config.read_bytes()).hexdigest(); manifest["artifact_sha256"]["config/hotstuff.gen.conf"] = digest; manifest["synthetic_workload"]["main_config_sha256"] = digest
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="frozen synthetic cadence"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_self_consistent_extra_native_option(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    config = root / "config/hotstuff.gen.conf"
    config.write_bytes(config.read_bytes() + b"parent-limit = 1\n")
    manifest_path = root / "materialization-manifest.json"; manifest = json.loads(manifest_path.read_text())
    digest = hashlib.sha256(config.read_bytes()).hexdigest(); manifest["artifact_sha256"]["config/hotstuff.gen.conf"] = digest; manifest["synthetic_workload"]["main_config_sha256"] = digest
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="unpinned native options"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_accepts_materializer_shaped_issuer_public_key_option(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    assert subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)["launch_permitted"] is False


def _refresh_identity_artifact_and_argv(root: Path, manager: list[str]) -> None:
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    main = root / "config/hotstuff.gen.conf"
    manifest["artifact_sha256"]["config/hotstuff.gen.conf"] = hashlib.sha256(main.read_bytes()).hexdigest()
    manifest["synthetic_workload"]["main_config_sha256"] = manifest["artifact_sha256"]["config/hotstuff.gen.conf"]
    manifest["manager_argv_sha256"] = subject._argv_digest(manager)
    manifest_path.write_bytes(_canonical(manifest))


def test_backend_rejects_alternate_valid_bls_keyset_with_refreshed_hashes(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    main = root / "config/hotstuff.gen.conf"
    changed = main.read_text(encoding="ascii").replace(f", {31:064x},", f", {'f' * 64},", 1)
    changed = changed.replace(f"activation-readiness-member = 0,{31:064x}",
                              f"activation-readiness-member = 0,{'f' * 64}", 1)
    main.write_text(changed, encoding="ascii")
    readiness = manager.index("--activation-readiness-member")
    manager[readiness + 1] = f"0,{'f' * 64}"
    _refresh_identity_artifact_and_argv(root, manager)
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    projection_path = root / "config/identity-public-projection.json"
    projection = json.loads(projection_path.read_text(encoding="ascii"))
    projection["bls_public_keys"][0] = "f" * 64
    projection_path.write_bytes(_canonical(projection))
    manifest["artifact_sha256"]["config/identity-public-projection.json"] = hashlib.sha256(projection_path.read_bytes()).hexdigest()
    manifest["public_identity_fingerprint"] = subject._public_identity_fingerprint(
        projection["bls_public_keys"],
        [(row["certificate"], row["common_name"]) for row in projection["replica_tls"]] + [
            (projection["manager_tls"]["certificate"], projection["manager_tls"]["common_name"])],
        projection["issuer_public_key"])
    receipt_path = root / "config/identity-parity-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="ascii"))
    receipt["public_identity_fingerprint"] = manifest["public_identity_fingerprint"]
    receipt_path.write_bytes(_canonical(receipt))
    manifest["artifact_sha256"]["config/identity-parity-receipt.json"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    manifest["identity_parity_receipt_sha256"] = manifest["artifact_sha256"]["config/identity-parity-receipt.json"]
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="full identity bundle"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_valid_tls_certificate_index_swap_with_refreshed_hashes(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    first = manager.index("--replica")
    manager[first + 1] = f"0,127.0.0.1:21000,{1:064x}"
    config = root / "config/replica-0.conf"
    config.write_text(f"privkey = key\ntls-privkey = tls-key\ntls-cert = {1:064x}\nidx = 0\n", encoding="ascii")
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest["artifact_sha256"]["config/replica-0.conf"] = hashlib.sha256(config.read_bytes()).hexdigest()
    manifest["manager_argv_sha256"] = subject._argv_digest(manager)
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="public identity projection"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_alternate_valid_issuer_public_key_with_refreshed_hashes(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    main = root / "config/hotstuff.gen.conf"
    main.write_text(main.read_text(encoding="ascii").replace("epoch-change-issuer-public-key = 02abcd", "epoch-change-issuer-public-key = 03abcd"), encoding="ascii")
    _refresh_identity_artifact_and_argv(root, manager)
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="public identity projection"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_replica_config_index_drift(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    replica = root / "config/replica-7.conf"
    replica.write_text("privkey = key\ntls-privkey = tls-key\ntls-cert = cert\n idx=8\n", encoding="ascii")
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifact_sha256"]["config/replica-7.conf"] = hashlib.sha256(replica.read_bytes()).hexdigest()
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="exact replica index"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_duplicate_whitespace_normalized_replica_index(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    replica = root / "config/replica-7.conf"
    replica.write_bytes(replica.read_bytes() + b" idx=7\n")
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifact_sha256"]["config/replica-7.conf"] = hashlib.sha256(replica.read_bytes()).hexdigest()
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="repeats a singleton key"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_extra_replica_cli_override_with_refreshed_digest(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    replicas[7].extend(("--block-size", "2"))
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["replica_argv_sha256"][7] = subject._argv_digest(replicas[7])
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="materializer command shape"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda argv: argv.extend(("--tree-fanout", "20")),
        lambda argv: argv.extend(("--activation-delay-blocks", "5")),
    ],
    ids=["appended-fanout", "duplicate-activation-delay"],
)
def test_backend_rejects_self_consistent_appended_manager_option(
    tmp_path: Path, mutation: object,
) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    mutation(manager)  # type: ignore[operator]
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["manager_argv_sha256"] = subject._argv_digest(manager)
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="materializer command grammar"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


def test_backend_rejects_self_consistent_changed_manager_cadence(tmp_path: Path) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    manager[manager.index("--tree-fanout") + 1] = "20"
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["manager_argv_sha256"] = subject._argv_digest(manager)
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="frozen cadence or protocol parameter"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("policy_intent", "fault_containment"),
        ("evidence_window_rule", "stale_predecessor_ok"),
        ("transition_artifact_id", "other-transition"),
        ("bundle_path", "transitions/other.bundle"),
        ("evidence_snapshot_path", "transitions/other.json"),
        ("predecessor_epoch_number", 1),
        ("successor_epoch_number", 2),
        ("minimum_predecessor_residency_ms", 999_999),
        ("minimum_post_baseline_observation_ms", 1),
        ("apply_shape_selection", True),
        ("policy_parameters", {"fanout": 20}),
    ],
)
def test_backend_rejects_self_consistent_transition_contract_mutation(
    tmp_path: Path, field: str, replacement: object,
) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    option = manager.index("--transition-request") + 1
    transition = json.loads(manager[option])
    transition[field] = replacement
    manager[option] = json.dumps(transition, sort_keys=True, separators=(",", ":"))
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["manager_argv_sha256"] = subject._argv_digest(manager)
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="frozen W18 E0-to-E1 contract"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)


@pytest.mark.parametrize("option", ["--tls-privkey", "--tls-cert", "--issuer-private-key"])
def test_backend_rejects_nonhex_inline_manager_identity_with_refreshed_digest(
    tmp_path: Path, option: str,
) -> None:
    root, manager, replicas, quota = _fixture(tmp_path)
    manager[manager.index(option) + 1] = "not-a-materialized-hex-value"
    manifest_path = root / "materialization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["manager_argv_sha256"] = subject._argv_digest(manager)
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(subject.OperatorCapacityV3BackendError, match="not lower-case hexadecimal"):
        subject.prepare_no_launch_backend(materialization_root=root, manager_argv=manager, replica_argv=replicas, quota_profile=quota)
