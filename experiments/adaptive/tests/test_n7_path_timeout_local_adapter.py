from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "local_adapter.py"
spec = importlib.util.spec_from_file_location("n7_local_adapter_test", PATH)
assert spec and spec.loader
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def test_local_adapter_prepares_eight_commands_with_only_relay_one_impaired(monkeypatch, tmp_path: Path):
    (tmp_path / "runtime").mkdir(); (tmp_path / "config").mkdir(); (tmp_path / "raw").mkdir()
    main = tmp_path / "config" / "hotstuff.gen.conf"; main.write_text("tree-generation = file\ntree-switch-period = 2\n")
    configs = [tmp_path / "config" / f"r{i}" for i in range(7)]
    for config in configs:
        config.write_text("idx = 0\n")
    argv = tmp_path / "runtime" / "launch-arguments.json"
    adapter.base._write_json_exclusive(argv, {"processes": [
        *[{"source_id": f"replica-{i}", "argv": ["app", "--conf", str(main), "--conf", str(configs[i])], "effective_options": {}} for i in range(7)],
        {"source_id": "adaptive-manager", "argv": ["manager"], "effective_options": {}},
    ]})
    tree = tmp_path / "config" / "epoch0.tree"; tree.write_text(adapter.runner.TREE_FILE.read_text())
    monkeypatch.setattr(adapter.runner, "TREE_FILE", tree)
    stale = [{"path": "runtime/launch-arguments.json", "sha256": "0" * 64, "kind": "launch_arguments", "replica_id": None}]
    calls = []
    monkeypatch.setattr(adapter.base, "write_runtime_inputs", lambda *a, **k: (calls.append(k) or (main, configs, ("manager", "--required-nonresponsive", "1"), [("app", "--conf", str(main), "--conf", str(config)) for config in configs], stale)))
    helper = tmp_path / "n7-epoch0-treefile-digest"; helper.write_text("synthetic helper")
    plan = adapter.prepare_local_inputs(
        tmp_path, adapter._load_frozen_v4_profile(), [], [], {"pub": "issuer-public"}, peer_port=1, client_port=2, manager_port=3,
        run_id="run", repository_revision="d" * 40, source_instances={}, app_binary=tmp_path / "app",
        manager_binary=tmp_path / "manager", e0_helper_binary=helper,
        e0_helper_invoke=lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="a" * 64 + "\n", stderr=""),
    )
    assert len(plan["replica_commands"]) == 7
    assert "--experiment-omit-outbound-aggregate" not in plan["replica_commands"][0]
    assert "--experiment-omit-outbound-aggregate" in plan["replica_commands"][1]
    assert "--experiment-omit-outbound-aggregate" not in plan["replica_commands"][2]
    overlay = plan["preflight"]["relay_omission"]["argv_overlay"]
    assert overlay[overlay.index("--experiment-byzantine-context-limit") + 1] == "9"
    assert overlay[overlay.index("--experiment-omission-contexts-per-configuration") + 1] == "3"
    assert plan["preflight"]["relay_omission"]["total_omission_contexts"] == 9
    assert all(config.read_text().endswith("experiment-exact-timeout-attempt-evidence-v3 = true\n") for config in configs)
    assert next(artifact for artifact in plan["runtime_artifacts"] if artifact["path"] == "runtime/launch-arguments.json")["sha256"] == adapter.base.sha256_file(argv)
    launch = json.loads(argv.read_text())
    for replica, config in enumerate(configs):
        process = next(process for process in launch["processes"] if process["source_id"] == f"replica-{replica}")
        assert process["effective_options"]["replica_config_sha256"] == adapter.base.sha256_file(config)
        assert process["effective_options"]["experiment_exact_timeout_attempt_evidence_v3"] is True
    assert plan["preflight"]["relay_omission"]["epoch_digest"] == "a" * 64
    assert plan["e0_identity"]["tree_file"] == "config/epoch0.tree"
    assert plan["e0_identity"]["helper_binary_sha256"] == adapter.base.sha256_file(helper)
    assert (tmp_path / plan["issuer_public_key"]).read_text() == "issuer-public\n"
    assert plan["issuer_public_key_sha256"] == adapter.base.sha256_file(
        tmp_path / plan["issuer_public_key"]
    )
    assert plan["state"] == "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED"
    assert plan["repository_revision"] == "d" * 40
    assert calls[0]["manager_extra_args"][-2:] == ("--required-nonresponsive", "1")


def test_v3_timeout_config_requires_all_seven_clean_replica_files(tmp_path: Path):
    configs = [tmp_path / f"replica-{i}.conf" for i in range(7)]
    for config in configs:
        config.write_text("idx = 0\n")
    adapter._enable_exact_timeout_attempt_evidence_v3(configs)
    assert all(config.read_text().count("experiment-exact-timeout-attempt-evidence-v3 = true\n") == 1 for config in configs)
    with pytest.raises(adapter.AdapterError, match="configuration drifted"):
        adapter._enable_exact_timeout_attempt_evidence_v3(configs)


def test_v4_profile_rejects_v1_or_tampered_profile(tmp_path: Path):
    v1 = tmp_path / "profile-v1.json"
    v1.write_text('{"profile_id":"n7-f2-q5-crash-recovery-v2"}')
    with pytest.raises(adapter.AdapterError, match="SHA-256"):
        adapter._load_frozen_v4_profile(v1)
    frozen = adapter._load_frozen_v4_profile()
    altered = dict(frozen)
    altered["crash_targets"] = [0, 1]
    with pytest.raises(adapter.AdapterError, match="caller profile"):
        adapter._require_v4_profile(altered, adapter.PROFILE_V4_FILE)


def test_v4_profile_uses_manager_supported_live_predecessor_roots_and_freezes_causal_contract():
    frozen = adapter._load_frozen_v4_profile()
    request = frozen["transition_requests"][0]

    assert request["containment_baseline_root_source"] == "live_predecessor_roots"
    assert request["policy_parameters"] == {}
    assert adapter.base._profile_transition_requests(frozen) == (request,)
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
    assert '"containment_baseline_root_source":"live_predecessor_roots"' in encoded
    assert frozen["fault_window_arm"]["snapshot_evidence_basis"] == (
        "exact_post_fault_path_timeout_quorum_v1"
    )
    assert frozen["fault_window_arm"]["physical_omission_causality_basis"] == (
        "exact_matched_post_arm_physical_omission_v1"
    )


def test_v4_profile_passes_real_base_runtime_window_contract():
    frozen = adapter._load_frozen_v4_profile()
    runtime = adapter.base.runtime_parameters(frozen)

    assert runtime["throughput_windows"] == frozen["throughput_windows"]
    assert runtime["transition_requests"] == frozen["transition_requests"]


@pytest.mark.parametrize(
    ("source", "parameters"),
    (
        ("caller_supplied_roots", {}),
        ("live_predecessor_roots", {"containment_baseline_roots": []}),
    ),
)
def test_v3_profile_rejects_invalid_live_root_source_contract(
    source: str, parameters: dict[str, object]
):
    frozen = adapter._load_frozen_v4_profile()
    mutated = json.loads(json.dumps(frozen))
    request = mutated["transition_requests"][0]
    request["containment_baseline_root_source"] = source
    request["policy_parameters"] = parameters

    with pytest.raises(adapter.base.RunnerError, match="live containment roots"):
        adapter.base._profile_transition_requests(mutated)


def test_e0_identity_rejects_helper_failure_and_tree_tampering(tmp_path: Path):
    tree = tmp_path / "epoch0.tree"; tree.write_text(adapter.runner.TREE_FILE.read_text())
    helper = tmp_path / "n7-epoch0-treefile-digest"; helper.write_text("synthetic helper")
    with pytest.raises(adapter.AdapterError, match="helper failed"):
        adapter._derive_e0_identity(
            tmp_path, tree, helper,
            invoke=lambda *args, **kwargs: SimpleNamespace(
                returncode=2, stdout="", stderr="bad tree"),
        )

    def mutate_tree(*args, **kwargs):
        tree.write_text("tampered")
        return SimpleNamespace(returncode=0, stdout="a" * 64 + "\n", stderr="")

    with pytest.raises(adapter.AdapterError, match="changed during derivation"):
        adapter._derive_e0_identity(tmp_path, tree, helper, invoke=mutate_tree)


def test_plan_path_guard_rejects_escape(tmp_path: Path):
    with pytest.raises(adapter.AdapterError, match="escapes"):
        adapter._under_run_root(tmp_path, "../outside")


def test_local_execute_reaches_readiness_after_sealed_revalidation(monkeypatch, tmp_path: Path):
    (tmp_path / "logs").mkdir(); (tmp_path / "raw").mkdir(); (tmp_path / "runtime").mkdir(); (tmp_path / "config").mkdir()
    main = tmp_path / "main.conf"; main.write_text("x")
    tree = tmp_path / "config" / "epoch0.tree"; tree.write_text(adapter.runner.TREE_FILE.read_text())
    helper = tmp_path / "n7-epoch0-treefile-digest"; helper.write_text("synthetic helper")
    invoke = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="a" * 64 + "\n", stderr="")
    identity = adapter._derive_e0_identity(tmp_path, tree, helper, invoke=invoke)
    receipt = tmp_path / "runtime" / "e0-identity-receipt.json"
    adapter.base._write_json_exclusive(receipt, identity)
    preflight = adapter.runner.preflight(identity["epoch_digest"], tree)
    overlay = tuple(preflight["relay_omission"]["argv_overlay"])
    manager_command = [
        "manager",
        "--tls-privkey",
        "private-key",
        "--tls-cert",
        "manager-certificate",
        "--issuer-private-key",
        "issuer-private-key",
        *(
            item
            for replica in range(7)
            for item in (
                "--replica",
                f"{replica},127.0.0.1:{10000 + replica},replica-{replica}-certificate",
            )
        ),
        "--epoch-zero-tree-file",
        str(tree.resolve()),
        "--required-nonresponsive",
        "1",
    ]
    replica_commands = [
        ["app", "--conf", str(main), "--conf", str(tmp_path / "config" / f"replica-{replica}.conf"), *(overlay if replica == 1 else ())]
        for replica in range(7)
    ]
    configs = [tmp_path / "config" / f"replica-{replica}.conf" for replica in range(7)]
    for config in configs:
        config.write_bytes(b"idx = 0\n" + adapter.EXACT_TIMEOUT_EVIDENCE_OPTION)
    argv = tmp_path / "runtime" / "launch-arguments.json"
    adapter.base._write_json_exclusive(argv, {"processes": [
        *[{"source_id": f"replica-{replica}", "argv": command,
           "effective_options": {"replica_config_sha256": adapter.base.sha256_file(configs[replica]),
                                 "experiment_exact_timeout_attempt_evidence_v3": True}}
          for replica, command in enumerate(replica_commands)],
        {
            "source_id": "adaptive-manager",
            "argv": adapter.base.normalized_manager_argv(manager_command),
        },
    ]})
    artifacts = [
        adapter.base._runtime_artifact(tmp_path, path, kind="test", replica_id=None)
        for path in (main, tree, receipt, argv)
    ]
    plan = {
        "state": "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED", "scenario": adapter.runner.SCENARIO,
        "main_config": "main.conf", "main_config_sha256": adapter.base.sha256_file(main),
        "replica_configs": [str(config.relative_to(tmp_path)) for config in configs],
        "manager_command": manager_command, "replica_commands": replica_commands,
        "runtime_artifacts": artifacts, "preflight": preflight,
        "e0_identity_receipt": "runtime/e0-identity-receipt.json",
        "e0_identity_receipt_sha256": adapter.base.sha256_file(receipt),
        "e0_identity": identity,
    }
    plan["plan_sha256"] = adapter._plan_digest(plan)
    adapter.base._write_json_exclusive(tmp_path / "local-launch-plan.json", plan)
    adapter.base._write_json_exclusive(tmp_path / "authorization.json", {"plan_sha256": plan["plan_sha256"], "approval_reference": "test"})
    class Process:
        def poll(self): return None
    class Record:
        process = Process()
    calls = []
    monkeypatch.setattr(adapter.base, "_event_streams", lambda _: {
        **{f"replica-{replica}": [{"event_type": "process.ready"}] for replica in range(7)},
        "adaptive-manager": [{"event_type": "process.ready"}],
    })
    outcome = adapter.execute_local_plan(
        tmp_path, hard_timeout_s=1, authorization_path=tmp_path / "authorization.json",
        spawn=lambda *a, **k: (calls.append(a) or Record()), cleanup=lambda records: None,
        monotonic=lambda: 0, e0_helper_invoke=invoke,
    )
    assert outcome["status"] == "LOCAL_SMOKE_INCOMPLETE"
    assert len(calls) == 8
    configs[0].write_bytes(b"idx = 999\n" + adapter.EXACT_TIMEOUT_EVIDENCE_OPTION)
    with pytest.raises(adapter.AdapterError, match="replica config hash"):
        adapter._verify_executable_local_plan(
            tmp_path, plan, e0_helper_invoke=invoke
        )


def test_local_execute_aborts_and_cleans_up_on_pre_readiness_exit(monkeypatch, tmp_path: Path):
    (tmp_path / "logs").mkdir(); (tmp_path / "raw").mkdir()
    plan = {
        "state": "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED",
        "scenario": adapter.runner.SCENARIO,
    }
    plan["plan_sha256"] = adapter._plan_digest(plan)
    adapter.base._write_json_exclusive(tmp_path / "local-launch-plan.json", plan)
    adapter.base._write_json_exclusive(
        tmp_path / "authorization.json",
        {"plan_sha256": plan["plan_sha256"], "approval_reference": "test"},
    )
    monkeypatch.setattr(
        adapter, "_verify_executable_local_plan",
        lambda *args, **kwargs: (("manager",), [("app", str(replica)) for replica in range(7)]),
    )

    class Process:
        def poll(self): return 1

    class Record:
        process = Process()

    cleaned = []
    outcome = adapter.execute_local_plan(
        tmp_path, hard_timeout_s=1, authorization_path=tmp_path / "authorization.json",
        spawn=lambda *args, **kwargs: Record(),
        cleanup=lambda records: cleaned.extend(records), monotonic=lambda: 0,
    )
    assert outcome["status"] == "ABORTED"
    assert len(cleaned) == 8
