from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "run_local.py"
spec = importlib.util.spec_from_file_location("n7_run_local_test", PATH)
assert spec and spec.loader
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


def _fixture(monkeypatch: pytest.MonkeyPatch, root: Path):
    (root / "runtime").mkdir()
    (root / "raw").mkdir()
    (root / "config").mkdir()
    main_config = root / "config/hotstuff.gen.conf"
    main_config.write_text(
        "".join(
            f"replica = 127.0.0.1:{11000 + replica};{12000 + replica}, key, cert\n"
            for replica in range(7)
        ),
        encoding="utf-8",
    )
    (root / "config/epoch0.tree").write_bytes(producer.runner.TREE_FILE.read_bytes())
    app_binary = root / "hotstuff-app"
    manager_binary = root / "adaptation-manager"
    for binary in (app_binary, manager_binary):
        binary.write_text("synthetic executable", encoding="utf-8")
        binary.chmod(0o700)
    transition = root / "runtime" / "transition-requests.json"
    transition.write_bytes(producer._canonical({"schema_version": 1, "requests": [{}]}))
    issuer = root / "runtime" / "issuer-public-key.txt"
    issuer.write_text("issuer-public-key\n", encoding="ascii")
    launch = {
        "schema_version": 1,
        "processes": [
            *[
                {
                    "source_id": f"replica-{replica}",
                    "argv": ["app", str(replica)],
                    "effective_options": {},
                }
                for replica in range(7)
            ],
            {
                "source_id": "adaptive-manager",
                "argv": ["manager"],
                "effective_options": {},
            },
        ],
    }
    (root / "runtime" / "launch-arguments.json").write_bytes(
        producer._canonical(launch)
    )
    overlay = producer.runner.omission_overlay("a" * 64)
    manager = (
        str(manager_binary),
        "--listen", "127.0.0.1:13000",
        "--tls-privkey", "manager-private-key",
        "--tls-cert", "manager-certificate",
        "--issuer-private-key", "issuer-private-key",
        "--structured-event-run-id", "run-n7",
        "--structured-event-source-instance", "manager-instance",
        "--transition-request",
        json.dumps(
            producer.adapter._load_frozen_v4_profile()["transition_requests"][0],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "--bundle-output", str(root / "transitions/e0-to-e1-containment/successor.bundle"),
        *(
            item
            for replica in range(7)
            for item in (
                "--replica",
                f"{replica},127.0.0.1:{10000 + replica},replica-{replica}-certificate",
            )
        ),
        "--epoch-zero-tree-file", str(root / "config/epoch0.tree"),
        "--required-nonresponsive", "1",
    )
    replicas = tuple(
        (str(app_binary), str(replica), *(overlay if replica == 1 else ()))
        for replica in range(7)
    )
    preflight = {
        "schema_version": 1,
        "scenario": producer.PROFILE_ID,
        "relay_omission": {"replica_id": 1},
        "fault_window_arm": {
            "timeout_evidence_basis": "exact_timeout_attempt_id_v1",
            "snapshot_evidence_basis": "exact_post_fault_path_timeout_quorum_v1",
            "physical_omission_causality_basis": (
                "exact_matched_post_arm_physical_omission_v1"
            ),
        },
    }
    plan = {
        "schema_version": 1,
        "scenario": producer.PROFILE_ID,
        "repository_revision": "d" * 40,
        "state": "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED",
        "e0_identity": {
            "epoch_digest": "a" * 64,
            "tree_file_sha256": "b" * 64,
            "tree_file": "config/epoch0.tree",
        },
        "preflight": preflight,
        "main_config": "config/hotstuff.gen.conf",
        "issuer_public_key": "runtime/issuer-public-key.txt",
        "issuer_public_key_sha256": producer.base.sha256_file(issuer),
        "runtime_artifacts": [
            {
                "kind": "transition_requests",
                "path": "runtime/transition-requests.json",
                "sha256": producer.base.sha256_file(transition),
                "replica_id": None,
            }
        ],
    }
    plan["plan_sha256"] = producer.adapter._plan_digest(plan)
    producer.base._write_json_exclusive(root / "local-launch-plan.json", plan)
    monkeypatch.setattr(
        producer.adapter,
        "_verify_executable_local_plan",
        lambda *_args, **_kwargs: (manager, [tuple(command) for command in replicas]),
    )
    return plan, manager, replicas


def test_prepare_binds_exact_arm_gate_commands_without_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    base_plan, manager, replicas = _fixture(monkeypatch, tmp_path)
    spawn_attempted = False

    def forbidden_spawn(*_args, **_kwargs):
        nonlocal spawn_attempted
        spawn_attempted = True
        raise AssertionError("PREPARE must not spawn")

    monkeypatch.setattr(producer.base, "spawn_process", forbidden_spawn)
    result = producer.prepare(tmp_path, hard_timeout_seconds=300)

    assert not spawn_attempted
    assert result["state"] == "PREPARED_EXTERNAL_APPROVAL_REQUIRED"
    assert not (tmp_path / producer.APPROVED_AUTHORIZATION).exists()
    plan = json.loads((tmp_path / producer.EXECUTION_PLAN).read_bytes())
    assert plan["plan_sha256"] == producer._execution_plan_digest(plan)
    assert plan["run_id"] == plan["bindings"]["run_id"] == "run-n7"
    assert plan["physical_omission_causality_basis"] == (
        "exact_matched_post_arm_physical_omission_v1"
    )
    assert plan["base_plan_sha256"] == base_plan["plan_sha256"]
    final_manager = plan["manager_command"]
    assert final_manager[: len(manager)] == list(manager)
    assert final_manager[final_manager.index("--fault-window-arm-prefault-tree-id") + 1] == "4"
    assert final_manager[final_manager.index("--fault-window-arm-required-tree-positions") + 1] == "3"
    assert final_manager[final_manager.index("--fault-window-arm-profile-id") + 1] == producer.PROFILE_ID
    assert final_manager[final_manager.index("--fault-window-arm-profile-sha256") + 1] == producer.PROFILE_SHA256
    assert final_manager[final_manager.index("--fault-window-arm-snapshot-evidence-basis") + 1] == "exact_post_fault_path_timeout_quorum_v1"
    assert final_manager[final_manager.index("--fault-window-arm-request-sha256") + 1] == producer.base.sha256_file(
        tmp_path / "runtime/transition-requests.json"
    )
    actor = plan["replica_commands"][1]
    assert actor[: len(replicas[1])] == list(replicas[1])
    assert actor[actor.index("--experiment-omission-activation-gate-run-id") + 1] == "run-n7"
    assert actor[actor.index("--experiment-omission-activation-gate-manager-source-instance") + 1] == "manager-instance"
    launch_hash = actor[
        actor.index("--experiment-omission-activation-gate-launch-argv-sha256") + 1
    ]
    assert launch_hash == producer.replica_launch_argv_sha256(actor)
    assert all(
        "--experiment-omission-activation-gate-path" not in command
        for replica, command in enumerate(plan["replica_commands"])
        if replica != 1
    )
    request = json.loads((tmp_path / producer.AUTHORIZATION_REQUEST).read_bytes())
    assert request["execution_plan_sha256"] == plan["plan_sha256"]
    assert request["physical_omission_causality_basis"] == (
        "exact_matched_post_arm_physical_omission_v1"
    )
    assert request["no_retry"] is True
    assert result["authorization_request_sha256"] == producer.base.sha256_file(
        tmp_path / producer.AUTHORIZATION_REQUEST
    )


def test_prepare_rejects_a_rehashed_base_plan_with_a_different_causal_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    plan, _manager, _replicas = _fixture(monkeypatch, tmp_path)
    altered = json.loads(json.dumps(plan))
    altered["preflight"]["fault_window_arm"][
        "physical_omission_causality_basis"
    ] = "unbound"
    altered["plan_sha256"] = producer.adapter._plan_digest(altered)
    producer.base._replace_json(tmp_path / "local-launch-plan.json", altered)

    with pytest.raises(producer.ProducerError, match="causal contract drifted"):
        producer.prepare(tmp_path)
    assert not (tmp_path / producer.EXECUTION_PLAN).exists()
    assert not (tmp_path / producer.AUTHORIZATION_REQUEST).exists()


def test_prospective_prepare_hash_binds_the_exact_omission_window_without_changing_legacy_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _fixture(monkeypatch, tmp_path)
    producer.prepare(tmp_path, prospective_fixed_horizon=True)
    plan = json.loads((tmp_path / producer.EXECUTION_PLAN).read_bytes())
    request = json.loads((tmp_path / producer.AUTHORIZATION_REQUEST).read_bytes())
    assert plan["prospective_fixed_horizon"] == producer.PROSPECTIVE_FIXED_HORIZON
    assert request["prospective_fixed_horizon"] == producer.PROSPECTIVE_FIXED_HORIZON
    assert request["execution_plan_sha256"] == plan["plan_sha256"]
    authorization_path, _authorization = _external_authorization(tmp_path)
    finalized = producer.finalize(tmp_path, authorization_path)
    assert finalized["authorization_request_sha256"] == producer.base.sha256_file(
        tmp_path / producer.AUTHORIZATION_REQUEST
    )


def test_prospective_execute_rejects_legacy_or_mutated_horizon_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    authorization_path = _finalized_execution_fixture(monkeypatch, tmp_path)
    launched = []
    with pytest.raises(producer.ProducerError, match="not bound"):
        producer.execute(tmp_path, authorization_path, execution_enabled=True,
                         prospective_fixed_horizon=True,
                         spawn=lambda name, *_args, **_kwargs: launched.append(name))
    assert launched == []


def test_prospective_prepare_rejects_a_timeout_without_startup_cleanup_reserve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    launched = []
    _fixture(monkeypatch, tmp_path)
    with pytest.raises(producer.ProducerError, match="startup/cleanup reserve"):
        producer.prepare(tmp_path, hard_timeout_seconds=90, prospective_fixed_horizon=True)
    assert not (tmp_path / producer.EXECUTION_PLAN).exists()

    other_root = tmp_path / "prospective"
    other_root.mkdir()
    _fixture(monkeypatch, other_root)
    producer.prepare(other_root, prospective_fixed_horizon=True)
    plan_path = other_root / producer.EXECUTION_PLAN
    plan = json.loads(plan_path.read_bytes())
    plan["prospective_fixed_horizon"]["duration_ns"] = 59_000_000_000
    plan["plan_sha256"] = producer._execution_plan_digest(plan)
    plan_path.write_bytes(producer._canonical(plan))
    with pytest.raises(producer.ProducerError, match="not bound"):
        producer.execute(other_root, other_root / "missing.json", execution_enabled=True,
                         prospective_fixed_horizon=True,
                         spawn=lambda name, *_args, **_kwargs: launched.append(name))
    assert launched == []


def test_execute_rejects_a_mutated_runtime_authorization_request_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    authorization_path = _finalized_execution_fixture(monkeypatch, tmp_path)
    request_path = tmp_path / producer.AUTHORIZATION_REQUEST
    request = json.loads(request_path.read_bytes())
    request["hard_timeout_seconds"] = 301
    request_path.write_bytes(producer._canonical(request))
    launched = []
    with pytest.raises(producer.ProducerError, match="authorization request drifted"):
        producer.execute(tmp_path, authorization_path, execution_enabled=True,
                         spawn=lambda name, *_args, **_kwargs: launched.append(name))
    assert launched == []


def test_prepare_inputs_is_one_reproducible_no_launch_entrypoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    run_root = tmp_path / "n7-local-001"
    binaries = {}
    for name in ("app", "manager", "keygen", "tls-keygen", "e0-helper"):
        path = tmp_path / name
        path.write_text("synthetic executable", encoding="utf-8")
        path.chmod(0o700)
        binaries[name] = path
    monkeypatch.setattr(producer.base, "ports_in_use", lambda _ports: [])
    captured = {}

    def prepare_adapter(root, _profile, _bls, _tls, _issuer, **kwargs):
        captured["adapter_root"] = root
        captured["adapter_kwargs"] = kwargs
        return {"plan_sha256": "a" * 64}

    def prepare_execution(root, *, hard_timeout_seconds):
        captured["execution_root"] = root
        captured["hard_timeout_seconds"] = hard_timeout_seconds
        return {
            "state": "PREPARED_EXTERNAL_APPROVAL_REQUIRED",
            "authorization_request": str(producer.AUTHORIZATION_REQUEST),
        }

    result = producer.prepare_inputs(
        run_root,
        app_binary=binaries["app"],
        manager_binary=binaries["manager"],
        keygen_binary=binaries["keygen"],
        tls_keygen_binary=binaries["tls-keygen"],
        e0_helper_binary=binaries["e0-helper"],
        peer_port=31100,
        client_port=32100,
        manager_port=33100,
        hard_timeout_seconds=240,
        verify_repository=lambda _repo: SimpleNamespace(revision="d" * 40),
        generate_identities=lambda *_args: ([{"pub": "b", "sec": "s"}] * 7, [{"crt": "c", "sec": "s", "cid": "i"}] * 8, {"pub": "issuer", "sec": "secret"}),
        prepare_adapter=prepare_adapter,
        prepare_execution=prepare_execution,
    )

    assert result["run_root"] == str(run_root)
    assert result["repository_revision"] == "d" * 40
    assert captured["adapter_kwargs"]["repository_revision"] == "d" * 40
    assert captured["hard_timeout_seconds"] == 240
    assert (run_root / "profile.json").read_bytes() == producer.adapter.PROFILE_V4_FILE.read_bytes()
    assert all((run_root / child).is_dir() for child in ("raw", "logs", "config"))
    assert not (run_root / producer.APPROVED_AUTHORIZATION).exists()


def _external_authorization(root: Path) -> tuple[Path, dict[str, object]]:
    plan = json.loads((root / producer.EXECUTION_PLAN).read_bytes())
    request_bytes = (root / producer.AUTHORIZATION_REQUEST).read_bytes()
    receipt = {
        "schema_version": 1,
        "kind": producer.AUTHORIZATION_KIND,
        "request_sha256": producer._sha256(request_bytes),
        "execution_plan_sha256": plan["plan_sha256"],
        "approval_reference": "explicit test operator approval",
        "approved_utc": "2026-09-28T10:00:00Z",
        "no_retry": True,
    }
    path = root.parent / f"{root.name}-external-authorization.json"
    path.write_bytes(producer._canonical(receipt))
    return path, receipt


def test_finalize_archives_exact_external_receipt_and_approved_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _fixture(monkeypatch, tmp_path)
    producer.prepare(tmp_path)
    authorization_path, authorization = _external_authorization(tmp_path)

    result = producer.finalize(tmp_path, authorization_path)

    archived = tmp_path / producer.APPROVED_AUTHORIZATION
    assert archived.read_bytes() == authorization_path.read_bytes()
    assert result["authorization_sha256"] == producer.base.sha256_file(archived)
    approved = json.loads((tmp_path / producer.APPROVED_PREFLIGHT).read_bytes())
    assert approved["approved_issuer_public_key_sha256"] == producer.base.sha256_file(
        tmp_path / "runtime/issuer-public-key.txt"
    )
    assert approved["approved_plan_authorization_sha256"] == producer._sha256(
        producer._canonical(authorization)
    )
    assert approved["approved_plan_request_sha256"] == producer.base.sha256_file(
        tmp_path / producer.AUTHORIZATION_REQUEST
    )
    assert result["claim_boundary"] == "external approval verified; no process launched"


@pytest.mark.parametrize("mutation", ["request", "plan", "retry", "timestamp"])
def test_finalize_rejects_unbound_or_malformed_external_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str
):
    _fixture(monkeypatch, tmp_path)
    producer.prepare(tmp_path)
    authorization_path, receipt = _external_authorization(tmp_path)
    if mutation == "request":
        receipt["request_sha256"] = "0" * 64
    elif mutation == "plan":
        receipt["execution_plan_sha256"] = "0" * 64
    elif mutation == "retry":
        receipt["no_retry"] = False
    else:
        receipt["approved_utc"] = "2026-09-28T10:00:00+00:00"
    authorization_path.write_bytes(producer._canonical(receipt))

    with pytest.raises(producer.ProducerError, match="not bound"):
        producer.finalize(tmp_path, authorization_path)
    assert not (tmp_path / producer.APPROVED_AUTHORIZATION).exists()
    assert not (tmp_path / producer.APPROVED_PREFLIGHT).exists()


def test_finalize_rejects_a_self_consistent_replacement_base_plan_before_archiving_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _fixture(monkeypatch, tmp_path)
    producer.prepare(tmp_path)
    authorization_path, _ = _external_authorization(tmp_path)
    base_path = tmp_path / "local-launch-plan.json"
    altered = json.loads(base_path.read_bytes())
    altered["claim_boundary"] = "replacement after approval request"
    altered["plan_sha256"] = producer.adapter._plan_digest(altered)
    base_path.write_bytes(producer._canonical(altered))

    with pytest.raises(producer.ProducerError, match="base plan differs"):
        producer.finalize(tmp_path, authorization_path)
    assert not (tmp_path / producer.APPROVED_AUTHORIZATION).exists()
    assert not (tmp_path / producer.APPROVED_PREFLIGHT).exists()


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("base_plan_sha256", "0" * 64),
        ("repository_revision", "0" * 40),
        ("final_launch_arguments_sha256", "0" * 64),
        ("issuer_public_key_sha256", "0" * 64),
        ("replica_1_launch_argv_sha256", "0" * 64),
        ("hard_timeout_seconds", 601),
        ("unexpected", "extra"),
    ],
)
def test_finalize_rejects_rehashed_misleading_authorization_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, replacement: object
):
    _fixture(monkeypatch, tmp_path)
    producer.prepare(tmp_path)
    request_path = tmp_path / producer.AUTHORIZATION_REQUEST
    request = json.loads(request_path.read_bytes())
    request[field] = replacement
    request_path.write_bytes(producer._canonical(request))
    authorization_path, _ = _external_authorization(tmp_path)

    with pytest.raises(producer.ProducerError, match="request and execution plan"):
        producer.finalize(tmp_path, authorization_path)
    assert not (tmp_path / producer.APPROVED_AUTHORIZATION).exists()
    assert not (tmp_path / producer.APPROVED_PREFLIGHT).exists()


def test_gate_and_arm_documents_bind_the_final_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _fixture(monkeypatch, tmp_path)
    producer.prepare(tmp_path)
    plan = json.loads((tmp_path / producer.EXECUTION_PLAN).read_bytes())
    arm = producer.build_fault_window_arm(
        plan, authorization_sha256="c" * 64, evidence_start_monotonic_ns=101
    )
    assert arm["required_tree_ids"] == [4, 5, 6]
    assert arm["profile_id"] == "n7-path-local-timeout-quorum-v4"
    assert arm["snapshot_evidence_basis"] == "exact_post_fault_path_timeout_quorum_v1"
    assert arm["schema_version"] == 4
    assert arm["kind"] == "kauri-focused-fault-window-arm-v4"
    assert arm["request_sha256"] == plan["bindings"]["transition_request_sha256"]
    assert arm["fault_receipt_sha256"] == "c" * 64
    gate = producer.build_omission_gate(
        plan,
        manager_source_sequence=9,
        manager_event_line_sha256="d" * 64,
        activation_monotonic_ns=102,
    )
    assert gate["launch_argv_sha256"] == plan["bindings"]["replica_1_launch_argv_sha256"]
    assert gate["manager_source_sequence"] == 9
    wire = producer.gate_bytes(gate)
    assert wire.startswith(b'{"schema_version":1,"kind":"kauri-n7-static-aggregate-omission-gate-v1"')
    assert wire.endswith(b"}\n")


def test_execute_is_explicitly_fail_closed_before_any_launch(tmp_path: Path):
    with pytest.raises(producer.ProducerError, match="EXECUTE is disabled"):
        producer.execute(tmp_path, tmp_path / "authorization.json")


def test_execute_cli_requires_explicit_reviewed_enablement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    command = ["execute", "--run-root", str(tmp_path), "--authorization", str(tmp_path / "approval.json")]
    with pytest.raises(SystemExit) as disabled:
        producer.main(command)
    assert disabled.value.code == 2
    assert "EXECUTE is disabled" in capsys.readouterr().err
    seen = []
    monkeypatch.setattr(producer, "execute", lambda *args, **kwargs: seen.append((args, kwargs)) or {"status": "synthetic-only"})
    assert producer.main([*command, "--enable-reviewed-execution"]) == 0
    assert seen == [((tmp_path, tmp_path / "approval.json"), {"execution_enabled": True})]


def _finalized_execution_fixture(
    monkeypatch: pytest.MonkeyPatch, root: Path, *, prospective_fixed_horizon: bool = False,
):
    _fixture(monkeypatch, root)
    producer.prepare(root, prospective_fixed_horizon=prospective_fixed_horizon)
    authorization_path, _ = _external_authorization(root)
    producer.finalize(root, authorization_path)
    (root / "logs").mkdir(exist_ok=True)
    transition = root / "transitions/e0-to-e1-containment"
    transition.mkdir(parents=True, exist_ok=True)
    (transition / "successor.bundle").write_bytes(b"signed-bundle")
    for source in ["adaptive-manager", *(f"replica-{replica}" for replica in range(7))]:
        (root / "raw" / f"{source}.jsonl").write_text("{}\n", encoding="utf-8")
    return authorization_path


@pytest.mark.parametrize("artifact", ["base_plan", "approved_preflight"])
def test_execute_rejects_post_approval_input_replacement_without_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, artifact: str
):
    authorization_path = _finalized_execution_fixture(monkeypatch, tmp_path)
    if artifact == "base_plan":
        path = tmp_path / "local-launch-plan.json"
        altered = json.loads(path.read_bytes())
        altered["claim_boundary"] = "replacement after finalization"
        altered["plan_sha256"] = producer.adapter._plan_digest(altered)
        expected_error = "base plan differs"
    else:
        path = tmp_path / producer.APPROVED_PREFLIGHT
        altered = json.loads(path.read_bytes())
        altered["claim_boundary"] = "replacement after finalization"
        expected_error = "approved preflight differs"
    path.write_bytes(producer._canonical(altered))
    if artifact == "approved_preflight":
        # Even a matching, rewritten local finalization hash cannot redefine
        # the preflight derived from the externally approved base plan.
        finalization_path = tmp_path / "runtime/finalization-receipt.json"
        finalization = json.loads(finalization_path.read_bytes())
        finalization["approved_preflight_sha256"] = producer._sha256(path.read_bytes())
        finalization_path.write_bytes(producer._canonical(finalization))
    launched: list[str] = []

    def forbidden_spawn(name, *_args, **_kwargs):
        launched.append(name)
        raise AssertionError("approval drift must fail before launch")

    with pytest.raises(producer.ProducerError, match=expected_error):
        producer.execute(
            tmp_path, authorization_path,
            spawn=forbidden_spawn, execution_enabled=True,
        )
    assert launched == []


def test_enabled_lifecycle_orders_arm_gate_cleanup_and_raw_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    authorization_path = _finalized_execution_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(producer.base, "ports_in_use", lambda _ports: [])
    monkeypatch.setattr(
        producer.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )
    ready = {"event_type": "process.ready"}
    streams = {
        "adaptive-manager": [ready],
        **{f"replica-{replica}": [ready] for replica in range(7)},
    }
    monkeypatch.setattr(producer.base, "_event_streams", lambda _root: streams)
    monkeypatch.setattr(
        producer.validator,
        "_common_e0_commit_before_arm",
        lambda *_args, **_kwargs: (7, "e" * 64),
    )
    monkeypatch.setattr(
        producer.validator,
        "validate_known_raw_events",
        lambda *_args, **_kwargs: {"verdict": "PARTIAL_ONLY"},
    )
    monkeypatch.setattr(
        producer.validator,
        "validate_raw_bundle",
        lambda _root, receipt: {
            "verdict": "RAW_BUNDLE_VALIDATED",
            "e1_successor_epoch_digest": "f" * 64,
            "claim_boundary": "validated test boundary",
            "artifact_keys": sorted(receipt["artifacts"]),
        },
    )
    arm_event = {
        "source_sequence": 9,
        "source_monotonic_ns": 100,
        "event_type": "fault_window_armed",
        "payload": {},
    }
    injection_event = {
        "source_sequence": 10,
        "source_monotonic_ns": 102,
        "event_type": "fault.injection_armed",
        "payload": {},
    }
    observed_event_order = []

    def event_line(_path, event_type, **_kwargs):
        observed_event_order.append(event_type)
        return (
            (arm_event, "1" * 64)
            if event_type == "fault_window_armed"
            else (injection_event, "2" * 64)
        )

    monkeypatch.setattr(producer, "_one_event_line", event_line)
    monkeypatch.setattr(producer, "_raw_clock_ns", lambda: 101)

    class Process:
        def poll(self):
            return None

    class Record:
        def __init__(self, name: str, ordinal: int):
            self.name = name
            self.pid = 1000 + ordinal
            self.pgid = 1000 + ordinal
            self.process = Process()

    launched = []

    def spawn(name, *_args, **_kwargs):
        launched.append(name)
        return Record(name, len(launched))

    monkeypatch.setattr(
        producer.adapter.comparison,
        "_shutdown_records",
        lambda records: [
            {
                "source_id": record.name,
                "pid": record.pid,
                "pgid": record.pgid,
                "returncode": -2,
            }
            for record in records
        ],
    )

    outcome = producer.execute(
        tmp_path,
        authorization_path,
        spawn=spawn,
        execution_enabled=True,
    )

    assert launched == ["adaptive-manager", *(f"replica-{replica}" for replica in range(7))]
    assert observed_event_order == ["fault_window_armed", "fault.injection_armed"]
    assert outcome["status"] == "RAW_BUNDLE_VALIDATED"
    cleanup = json.loads((tmp_path / "runtime/cleanup-receipt.json").read_bytes())
    assert cleanup["complete"] is True
    assert [item["source_id"] for item in cleanup["processes"]] == launched
    receipt = json.loads((tmp_path / "raw-bundle-receipt.json").read_bytes())
    assert set(receipt["artifacts"]) == {
        "preflight", "epoch0_tree", "execution_plan", "authorization_request",
        "plan_authorization", "fault_window_arm", "omission_gate", "manager_events",
        "replica_streams", "e1_bundle", "issuer_public_key", "cleanup",
    }
    for key, relative in (
        ("authorization_request", producer.AUTHORIZATION_REQUEST),
        ("fault_window_arm", producer.FAULT_WINDOW_ARM),
        ("omission_gate", producer.OMISSION_GATE),
    ):
        descriptor = receipt["artifacts"][key]
        assert descriptor["path"] == str(relative)
        assert descriptor["sha256"] == producer.base.sha256_file(
            tmp_path / relative
        )
    assert receipt["fault_window_arm"]["source_sequence"] == 9
    assert receipt["fault_injection_arm"]["source_sequence"] == 10


def test_prospective_horizon_anchors_only_a_gate_bound_physical_omission_and_rejects_censoring():
    streams = {
        "adaptive-manager": [{"source_monotonic_ns": 200}],
        **{f"replica-{replica}": [{"source_monotonic_ns": 200}] for replica in range(7)},
    }
    streams["replica-1"] = [
        {"event_type": "fault.aggregate_omitted", "source_monotonic_ns": 150,
         "payload": {"actor": 1, "parent_replica": 4, "epoch_number": 0,
                     "tree_id": 4, "epoch_digest": "a" * 64,
                     "block_hash": "b" * 64, "gate_sha256": "wrong",
                     "first_for_context": True}},
        {"event_type": "fault.aggregate_omitted", "source_monotonic_ns": 160,
         "payload": {"actor": 1, "parent_replica": 4, "epoch_number": 0,
                     "tree_id": 4, "epoch_digest": "a" * 64,
                     "block_hash": "c" * 64, "gate_sha256": "right",
                     "first_for_context": True}},
        {"event_type": "stream.coverage", "source_monotonic_ns": 200, "payload": {}},
    ]
    assert producer._first_source_bound_physical_omission(streams, "right")["source_monotonic_ns"] == 160
    assert producer._first_source_bound_physical_omission(streams, "absent") is None
    assert producer._streams_cover_fixed_horizon(streams, 200) is True
    streams["replica-6"] = [{"source_monotonic_ns": 199}]
    assert producer._streams_cover_fixed_horizon(streams, 200) is False


def test_prospective_no_omission_aborts_once_and_cleans_all_eight_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    authorization_path = _finalized_execution_fixture(
        monkeypatch, tmp_path, prospective_fixed_horizon=True,
    )
    monkeypatch.setattr(producer.base, "ports_in_use", lambda _ports: [])
    monkeypatch.setattr(producer.os, "killpg", lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(producer.validator, "_common_e0_commit_before_arm", lambda *_args, **_kwargs: (7, "e" * 64))
    monkeypatch.setattr(producer.validator, "validate_known_raw_events", lambda *_args, **_kwargs: {"verdict": "PARTIAL_ONLY"})
    streams = {"adaptive-manager": [{"event_type": "process.ready"}], **{f"replica-{replica}": [{"event_type": "process.ready"}] for replica in range(7)}}
    monkeypatch.setattr(producer.base, "_event_streams", lambda _root: streams)
    arm = {"source_sequence": 9, "source_monotonic_ns": 100, "event_type": "fault_window_armed", "payload": {}}
    injection = {"source_sequence": 10, "source_monotonic_ns": 102, "event_type": "fault.injection_armed", "payload": {}}
    monkeypatch.setattr(producer, "_one_event_line", lambda _path, event_type, **_kwargs: (arm, "1" * 64) if event_type == "fault_window_armed" else (injection, "2" * 64))
    monkeypatch.setattr(producer, "_raw_clock_ns", lambda: 101)

    class Process:
        def poll(self): return None
    class Record:
        def __init__(self, name, ordinal):
            self.name, self.pid, self.pgid, self.process = name, 3000 + ordinal, 3000 + ordinal, Process()
    launched, cleaned = [], []
    monkeypatch.setattr(producer.adapter.comparison, "_shutdown_records", lambda records: (cleaned.extend(record.name for record in records) or [{"source_id": record.name, "pid": record.pid, "pgid": record.pgid, "returncode": -15} for record in records]))
    def wait(_records, _deadline, probe, label, **_kwargs):
        if label == "source-bound physical aggregate omission":
            raise producer.ProducerError("no gate-bound physical omission")
        return probe()
    monkeypatch.setattr(producer, "_wait_for", wait)
    with pytest.raises(producer.ProducerError, match="no-retry execution aborted"):
        producer.execute(tmp_path, authorization_path, prospective_fixed_horizon=True,
                         execution_enabled=True,
                         spawn=lambda name, *_args, **_kwargs: (launched.append(name) or Record(name, len(launched))))
    assert launched == cleaned == ["adaptive-manager", *(f"replica-{replica}" for replica in range(7))]
    assert json.loads((tmp_path / "local-run-abort.json").read_bytes())["status"] == "ABORTED"


@pytest.mark.parametrize(
    "failure",
    [producer.ProducerError("synthetic lifecycle failure"), KeyboardInterrupt()],
)
def test_enabled_lifecycle_seals_abort_and_cleans_every_spawned_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: BaseException
):
    authorization_path = _finalized_execution_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(producer.base, "ports_in_use", lambda _ports: [])
    monkeypatch.setattr(
        producer.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )

    class Process:
        def poll(self):
            return None

    class Record:
        def __init__(self, name: str, ordinal: int):
            self.name = name
            self.pid = 2000 + ordinal
            self.pgid = 2000 + ordinal
            self.process = Process()

    launched = []
    cleaned = []

    def spawn(name, *_args, **_kwargs):
        launched.append(name)
        return Record(name, len(launched))

    def shutdown(records):
        if isinstance(failure, KeyboardInterrupt):
            handler = producer.signal.getsignal(producer.signal.SIGINT)
            assert callable(handler)
            handler(producer.signal.SIGINT, None)
        cleaned.extend(record.name for record in records)
        return [
            {
                "source_id": record.name,
                "pid": record.pid,
                "pgid": record.pgid,
                "returncode": -15,
            }
            for record in records
        ]

    monkeypatch.setattr(producer.adapter.comparison, "_shutdown_records", shutdown)
    def fail_wait(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(producer, "_wait_for", fail_wait)

    with pytest.raises(producer.ProducerError, match="no-retry execution aborted"):
        producer.execute(
            tmp_path,
            authorization_path,
            spawn=spawn,
            execution_enabled=True,
        )

    assert len(launched) == len(cleaned) == 8
    abort = json.loads((tmp_path / "local-run-abort.json").read_bytes())
    assert abort["status"] == "ABORTED"
    assert abort["cleanup_complete"] is True
    assert abort["claim_boundary"].startswith("no retry")


def test_atomic_publication_never_exposes_partial_final_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    destination = tmp_path / "arm.json"
    payload = b'{"complete":true}\n'
    real_link = producer.os.link
    observed = []

    def inspect_link(source, target):
        assert Path(target) == destination
        assert not destination.exists()
        assert Path(source).read_bytes() == payload
        observed.append(True)
        return real_link(source, target)

    monkeypatch.setattr(producer.os, "link", inspect_link)
    producer._publish_atomic_once(destination, payload)

    assert observed == [True]
    assert destination.read_bytes() == payload
    with pytest.raises(producer.ProducerError, match="replace"):
        producer._publish_atomic_once(destination, payload)


def test_cleanup_refuses_complete_receipt_while_a_declared_listener_survives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    (tmp_path / "runtime").mkdir()
    class Process:
        def poll(self):
            return -15

    class Record:
        def __init__(self, name: str, ordinal: int):
            self.name = name
            self.pid = 3000 + ordinal
            self.pgid = 3000 + ordinal
            self.process = Process()

    records = [Record("adaptive-manager", 0), *[Record(f"replica-{i}", i + 1) for i in range(7)]]
    monkeypatch.setattr(
        producer.adapter.comparison,
        "_shutdown_records",
        lambda values: [
            {
                "source_id": record.name,
                "pid": record.pid,
                "pgid": record.pgid,
                "returncode": -15,
            }
            for record in values
        ],
    )
    monkeypatch.setattr(
        producer.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(producer.base, "ports_in_use", lambda _ports: [11000])
    ticks = iter(range(100))
    monkeypatch.setattr(producer.time, "monotonic", lambda: next(ticks) * 0.25)
    monkeypatch.setattr(producer.time, "sleep", lambda _seconds: None)

    receipt = producer._cleanup_receipt(tmp_path, "run", records, (11000,))

    assert receipt["complete"] is False
    archived = json.loads((tmp_path / "runtime/cleanup-receipt.json").read_bytes())
    assert archived["complete"] is False


def test_clean_manager_exit_requires_exact_ready_evidence():
    class Process:
        def __init__(self, code):
            self.code = code

        def poll(self):
            return self.code

    manager = type("Record", (), {"name": "adaptive-manager", "process": Process(0)})()
    replica = type("Record", (), {"name": "replica-0", "process": Process(None)})()

    producer._check_live([manager, replica], lambda _record: True)
    with pytest.raises(producer.ProducerError, match="adaptive-manager exited"):
        producer._check_live([manager, replica], lambda _record: False)
