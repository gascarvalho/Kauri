from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "sustained_role_local.py"
spec = importlib.util.spec_from_file_location("n7_sustained_role_local_test", PATH)
assert spec and spec.loader
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


def _fixture(tmp_path: Path):
    tree = tmp_path / "epoch0.tree"
    tree.write_bytes(subject.tree_runner.TREE_FILE.read_bytes())
    raw = tmp_path / "raw"; raw.mkdir()
    main = tmp_path / "main.conf"
    main.write_text(
        "block-size = 1\nfan-out = 2\nasync_blocks = 2\n"
        "tree-switch-period = 2\naggregation-timeout = 0.5\n"
        "leader-progress-timeout = 5.0\nleader-activation-grace = 1.0\n"
        "epoch-change-minimum-activation-delay = 5\n"
        "epoch-change-maximum-activation-delay = 5\ntree-generation = file\n"
        "tree-generation-fpath = " + str(tree) + "\n"
        "epoch-change-issuer-id = 1\n"
        "epoch-change-issuer-public-key = test-public-key\n"
    )
    app = tmp_path / "app"; manager = tmp_path / "manager"
    for binary in (app, manager):
        binary.write_text("binary\n"); binary.chmod(0o700)
    configs = [tmp_path / f"replica-{replica}.conf" for replica in range(7)]
    run_id = "sustained-role-test-run"
    for replica, config in enumerate(configs):
        config.write_text(
            f"idx = {replica}\nstructured-event-run-id = {run_id}\n"
            f"structured-event-source-instance = instance-{replica}\n"
            f"structured-event-output = {raw / f'replica-{replica}.jsonl'}\n"
        )
    manager_argv = (
        str(manager), "--issuer-id", "1", "--issuer-private-key", "test-secret",
        "--activation-delay-blocks", "5", "--structured-event-run-id", run_id,
        "--structured-event-source-instance", "manager-instance",
        "--structured-event-output", str(raw / "adaptive-manager.jsonl"),
        "--transition-request", subject.profile.canonical_transition_request(),
        "--bundle-output", str(tmp_path / "successor.bundle"),
        "--epoch-zero-tree-file", str(tree),
        "--required-nonresponsive", "1",
        "--replica", "0,127.0.0.1:12000,test0.crt",
        "--replica", "1,127.0.0.1:12001,test1.crt",
        "--replica", "2,127.0.0.1:12002,test2.crt",
        "--replica", "3,127.0.0.1:12003,test3.crt",
        "--replica", "4,127.0.0.1:12004,test4.crt",
        "--replica", "5,127.0.0.1:12005,test5.crt",
        "--replica", "6,127.0.0.1:12006,test6.crt",
        "--fault-window-arm-path", str(tmp_path / "fault-window-arm.json"),
        "--fault-window-arm-schema-version", "4",
        "--fault-window-arm-domain", subject.profile.PATH_TIMEOUT_ARM_DOMAIN,
        "--fault-window-arm-run-id", run_id,
        "--fault-window-arm-profile-id", subject.profile.PATH_TIMEOUT_SELECTION_PROFILE_ID,
        "--fault-window-arm-profile-sha256", subject.profile.PATH_TIMEOUT_SELECTION_PROFILE_SHA256,
        "--fault-window-arm-topology-proof-sha256", subject.tree_runner.sha256_file(tree),
        "--fault-window-arm-request-sha256", "b" * 64,
        "--fault-window-arm-epoch-number", "0",
        "--fault-window-arm-epoch-digest", "c" * 64,
        "--fault-window-arm-prefault-tree-id", "4",
        "--fault-window-arm-required-tree-positions", "3",
        "--fault-window-arm-deadline-seconds", "180",
        "--fault-window-arm-timeout-evidence-basis", "exact_timeout_attempt_id_v1",
        "--fault-window-arm-required-observation-schema", "3",
        "--fault-window-arm-clock-domain", "same_host_clock_monotonic_raw",
        "--fault-window-arm-snapshot-evidence-basis", "exact_post_fault_path_timeout_quorum_v1",
        "--fault-window-arm-selection-cardinality-policy", "all_guarded_up_to_fault_bound_v1",
    )
    replicas = [(str(app), "--conf", str(main), "--conf", str(config)) for config in configs]
    snapshot = lambda _path: SimpleNamespace(revision="a" * 40, worktree_clean=True)
    return tree, main, configs, manager_argv, replicas, snapshot


def _fixed_manager(manager: tuple[str, ...]) -> tuple[str, ...]:
    values = list(manager)
    for option in ("--transition-request", "--bundle-output"):
        position = values.index(option)
        del values[position:position + 2]
    position = 0
    while position < len(values):
        if values[position].startswith("--fault-window-arm-"):
            del values[position:position + 2]
        else:
            position += 1
    return tuple(values)


def test_dry_run_binds_clean_identity_exact_tree_configs_and_actor_only_schedule(tmp_path: Path) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    root = tmp_path / "dry-run"
    result = subject.prepare_dry_run(
        root, arm="adaptive_e1", epoch0_tree=tree, main_config=main, replica_configs=configs,
        manager_command=manager, replica_commands=replicas,
        window_start_monotonic_ns=100,
        window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                 + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
        repository_snapshot=snapshot,
        native_mode_revision_check=lambda _revision: True,
    )
    plan = json.loads((root / subject.PLAN).read_text())
    request = json.loads((root / subject.REQUEST).read_text())
    assert result["execution_plan_sha256"] == plan["plan_sha256"]
    assert request["execution_plan_sha256"] == plan["plan_sha256"]
    assert request["arm"] == "adaptive_e1"
    assert plan["repository_revision"] == "a" * 40
    assert plan["native_mode_introduction_revision"] == subject.NATIVE_MODE_INTRODUCTION_REVISION
    assert plan["native_fault_schedule"]["values"]["profile_id"] == subject.profile.PROFILE_ID
    assert plan["manager_selection_policy"]["values"]["profile_id"] == "n7-path-local-timeout-quorum-v4"
    assert plan["manager_selection_policy"]["descriptor"]["sha256"] == subject.profile.PATH_TIMEOUT_SELECTION_PROFILE_SHA256
    assert plan["epoch0"]["trees"][4].index(1) in (1, 2)
    assert plan["comparison"]["arm"] == "adaptive_e1"
    assert plan["comparison"]["paired_comparator"] == "fixed_e0"
    assert plan["comparison"]["effect_scope"] == "containment_epoch_package_vs_mixed_role_fixed_e0"
    assert plan["comparison"]["causal_boundary"] == "package_comparison_not_isolated_leaf_placement_or_aggregate_only_control"
    evidence = plan["evidence_contract"]
    assert evidence["anchor"] == "first_bijected_e0_internal_aggregate_omission"
    assert evidence["fixed_e0_physical_actions"] == "mixed_root_forward_leaf_direct_vote_internal_aggregate"
    assert evidence["late_interval"] == {
        "start_after_anchor_ns": 20_000_000_000,
        "end_after_anchor_ns": 60_000_000_000,
        "both_arms_require_physical_omission": True,
    }
    assert evidence["adaptive_only"]["all_seven_e1_activation_deadline_after_anchor_ns"] == 20_000_000_000
    assert "native_process_logs_all_8" in evidence["future_receipt_descriptors"]
    assert "manager_evidence_snapshot" in evidence["future_receipt_descriptors"]
    assert plan["receipt_contract"]["kind"] == "kauri-n7-sustained-role-raw-bundle-receipt-v1"
    assert plan["scheduled_window"]["argv_pinned_before_launch"] is True
    assert plan["scheduled_window"]["attestation"]["is_not"] == "an_arm_or_gate"
    assert "--experiment-byzantine-mode" not in plan["commands"]["replicas"][0]["argv"]
    actor = plan["commands"]["replicas"][1]["argv"]
    assert actor[actor.index("--experiment-byzantine-mode") + 1] == subject.profile.NATIVE_MODE
    assert plan["no_retry"] is True and request["no_retry"] is True


def test_integrated_materialization_owns_one_new_root_and_seals_abort_on_escape(tmp_path: Path) -> None:
    root = tmp_path / "integrated"
    external = tmp_path / "outside.tree"; external.write_text("not a tree\n")

    def escaping_materializer(created: Path):
        # The callback did run in an exclusive fresh directory, but returning
        # an external artifact must never turn that directory into a plan.
        (created / "local-launch-plan.json").write_text("{}\n")
        return {"epoch0_tree": external, "main_config": external,
                "replica_configs": (), "manager_command": (), "replica_commands": ()}

    with pytest.raises(subject.SustainedRoleProducerError, match="escapes exclusive root"):
        subject.prepare_materialized_dry_run(
            root, arm="fixed_e0", materialize=escaping_materializer,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=100 + subject.profile.COMMON_HORIZON_NS + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS,
            repository_snapshot=lambda _path: SimpleNamespace(revision="a" * 40, worktree_clean=True),
            native_mode_revision_check=lambda _revision: True,
        )
    abort = json.loads((root / "runtime/sustained-role-materialization-abort.json").read_text())
    assert abort["state"] == "MATERIALIZATION_ABORTED_NO_EXECUTION"
    with pytest.raises(subject.SustainedRoleProducerError, match="must be absent"):
        subject.prepare_materialized_dry_run(
            root, arm="fixed_e0", materialize=escaping_materializer,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=100 + subject.profile.COMMON_HORIZON_NS + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS,
            repository_snapshot=lambda _path: SimpleNamespace(revision="a" * 40, worktree_clean=True),
            native_mode_revision_check=lambda _revision: True,
        )


def test_real_no_launch_cli_materializes_fixed_native_argv_and_timeout_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the parser and production materializer; never spawn a node."""
    original = subject.prepare_production_dry_run

    def prepared_for_test(*args, **kwargs):
        return original(
            *args, **kwargs,
            repository_snapshot=lambda _path: SimpleNamespace(revision="a" * 40, worktree_clean=True),
            native_mode_revision_check=lambda _revision: True,
        )

    monkeypatch.setattr(subject, "prepare_production_dry_run", prepared_for_test)
    root = tmp_path / "cli-fixed"
    assert subject.main([
        "--run-root", str(root), "--arm", "fixed_e0", "--run-id", "cli-no-launch",
        "--window-start-monotonic-ns", "100", "--window-end-monotonic-ns", "70000000100",
    ]) == 0
    plan = json.loads((root / subject.PLAN).read_text())
    manager = plan["commands"]["manager"]["argv"]
    assert "--transition-request" not in manager
    assert "--bundle-output" not in manager
    assert not any(option.startswith("--fault-window-arm-") for option in manager)
    assert "--scheduled-fixed-e0-control" not in manager  # launcher alone adds it.
    for config in plan["configuration"]["replicas"]:
        payload = Path(config["path"]).read_text()
        assert "experiment-exact-timeout-attempt-evidence-v3 = true\n" in payload
    local = json.loads((root / "local-launch-plan.json").read_text())
    helper = json.loads((root / local["e0_identity_receipt"]).read_text())["helper_binary"]
    assert Path(helper).resolve().is_relative_to(root.resolve())


def test_dry_run_rejects_v4_static_option_before_writing_output(tmp_path: Path) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    replicas[1] += ("--experiment-omit-outbound-aggregate",)
    root = tmp_path / "dry-run"
    with pytest.raises(subject.SustainedRoleProducerError, match="v4/static"):
        subject.prepare_dry_run(
            root, arm="fixed_e0", epoch0_tree=tree, main_config=main, replica_configs=configs,
            manager_command=_fixed_manager(manager), replica_commands=replicas,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                     + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
            repository_snapshot=snapshot,
            native_mode_revision_check=lambda _revision: True,
        )
    assert not root.exists()


def test_fixed_e0_dry_run_retains_its_exact_arm_identity(tmp_path: Path) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    root = tmp_path / "fixed"
    subject.prepare_dry_run(
        root, arm="fixed_e0", epoch0_tree=tree, main_config=main,
        replica_configs=configs, manager_command=_fixed_manager(manager), replica_commands=replicas,
        window_start_monotonic_ns=100,
        window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                 + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
        repository_snapshot=snapshot,
        native_mode_revision_check=lambda _revision: True,
    )
    plan = json.loads((root / subject.PLAN).read_text())
    request = json.loads((root / subject.REQUEST).read_text())
    assert plan["comparison"]["arm"] == "fixed_e0"
    assert plan["comparison"]["paired_comparator"] == "adaptive_e1"
    assert plan["receipt_contract"]["arm"] == "fixed_e0"
    assert request["arm"] == "fixed_e0"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda argv: tuple(value for value in argv if value != "--issuer-id" and value != "1"), "issuer"),
        (lambda argv: tuple("6" if value == "5" else value for value in argv), "activation delay"),
        (lambda argv: tuple("{}" if value == subject.profile.canonical_transition_request() else value for value in argv), "transition request"),
    ],
)
def test_adaptive_dry_run_rejects_altered_manager_profile_bindings(
    tmp_path: Path, mutate, match: str,
) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    with pytest.raises(subject.SustainedRoleProducerError, match=match):
        subject.prepare_dry_run(
            tmp_path / "bad", arm="adaptive_e1", epoch0_tree=tree, main_config=main,
            replica_configs=configs, manager_command=mutate(manager), replica_commands=replicas,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                     + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
            repository_snapshot=snapshot, native_mode_revision_check=lambda _revision: True,
        )


def test_dry_run_rejects_config_run_id_issuer_or_output_binding(tmp_path: Path) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    configs[3].write_text(configs[3].read_text().replace("sustained-role-test-run", "other-run"))
    with pytest.raises(subject.SustainedRoleProducerError, match="run id"):
        subject.prepare_dry_run(
            tmp_path / "bad-run", arm="adaptive_e1", epoch0_tree=tree, main_config=main,
            replica_configs=configs, manager_command=manager, replica_commands=replicas,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                     + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
            repository_snapshot=snapshot, native_mode_revision_check=lambda _revision: True,
        )
    configs[3].write_text(configs[3].read_text().replace("other-run", "sustained-role-test-run"))
    configs[3].write_text(configs[3].read_text().replace("replica-3.jsonl", "wrong-source.jsonl"))
    with pytest.raises(subject.SustainedRoleProducerError, match="output"):
        subject.prepare_dry_run(
            tmp_path / "bad-output", arm="adaptive_e1", epoch0_tree=tree, main_config=main,
            replica_configs=configs, manager_command=manager, replica_commands=replicas,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                     + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
            repository_snapshot=snapshot, native_mode_revision_check=lambda _revision: True,
        )


def test_dry_run_rejects_missing_path_timeout_manager_binding(tmp_path: Path) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    values = list(manager)
    position = values.index("--fault-window-arm-profile-sha256")
    del values[position:position + 2]
    with pytest.raises(subject.SustainedRoleProducerError, match="profile-sha256"):
        subject.prepare_dry_run(
            tmp_path / "bad-manager", arm="adaptive_e1", epoch0_tree=tree, main_config=main,
            replica_configs=configs, manager_command=tuple(values), replica_commands=replicas,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                     + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
            repository_snapshot=snapshot, native_mode_revision_check=lambda _revision: True,
        )


def test_dry_run_rejects_tree_binding_drift(tmp_path: Path) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    main.write_text(main.read_text().replace(str(tree), str(tmp_path / "other.tree")))
    with pytest.raises(subject.SustainedRoleProducerError, match="tree"):
        subject.prepare_dry_run(
            tmp_path / "bad-tree", arm="adaptive_e1", epoch0_tree=tree, main_config=main,
            replica_configs=configs, manager_command=manager, replica_commands=replicas,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                     + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
            repository_snapshot=snapshot, native_mode_revision_check=lambda _revision: True,
        )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda configs, manager: configs[1].write_text(
            configs[1].read_text().replace("instance-1", "instance-0")
        ),
        lambda configs, manager: configs[1].write_text(
            configs[1].read_text().replace("instance-1", "manager-instance")
        ),
    ),
)
def test_dry_run_rejects_duplicate_or_manager_colliding_source_instance(
    tmp_path: Path, mutate,
) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    mutate(configs, manager)
    with pytest.raises(subject.SustainedRoleProducerError, match="globally distinct"):
        subject.prepare_dry_run(
            tmp_path / "bad-source", arm="adaptive_e1", epoch0_tree=tree, main_config=main,
            replica_configs=configs, manager_command=manager, replica_commands=replicas,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                     + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
            repository_snapshot=snapshot, native_mode_revision_check=lambda _revision: True,
        )


def test_dry_run_rejects_arm_deadline_that_differs_from_plan_timeout(tmp_path: Path) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    values = list(manager)
    values[values.index("--fault-window-arm-deadline-seconds") + 1] = "9999"
    with pytest.raises(subject.SustainedRoleProducerError, match="deadline"):
        subject.prepare_dry_run(
            tmp_path / "bad-deadline", arm="adaptive_e1", epoch0_tree=tree, main_config=main,
            replica_configs=configs, manager_command=tuple(values), replica_commands=replicas,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                     + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
            repository_snapshot=snapshot, native_mode_revision_check=lambda _revision: True,
        )


@pytest.mark.parametrize(
    "option",
    (
        "--fault-window-arm-topology-proof-sha256",
        "--fault-window-arm-request-sha256",
        "--fault-window-arm-epoch-digest",
    ),
)
def test_dry_run_rejects_noncanonical_dynamic_arm_digest(tmp_path: Path, option: str) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    values = list(manager)
    values[values.index(option) + 1] = "x"
    with pytest.raises(subject.SustainedRoleProducerError, match="SHA-256"):
        subject.prepare_dry_run(
            tmp_path / "bad-digest", arm="adaptive_e1", epoch0_tree=tree, main_config=main,
            replica_configs=configs, manager_command=tuple(values), replica_commands=replicas,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                     + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
            repository_snapshot=snapshot, native_mode_revision_check=lambda _revision: True,
        )


def test_dry_run_rejects_topology_digest_for_another_tree(tmp_path: Path) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    values = list(manager)
    values[values.index("--fault-window-arm-topology-proof-sha256") + 1] = "d" * 64
    with pytest.raises(subject.SustainedRoleProducerError, match="topology proof"):
        subject.prepare_dry_run(
            tmp_path / "bad-topology", arm="adaptive_e1", epoch0_tree=tree, main_config=main,
            replica_configs=configs, manager_command=tuple(values), replica_commands=replicas,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                     + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
            repository_snapshot=snapshot, native_mode_revision_check=lambda _revision: True,
        )


def test_dry_run_rejects_missing_issuer_public_key_binding(tmp_path: Path) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    main.write_text(main.read_text().replace("test-public-key", ""))
    with pytest.raises(subject.SustainedRoleProducerError, match="issuer-public-key"):
        subject.prepare_dry_run(
            tmp_path / "bad-issuer", arm="adaptive_e1", epoch0_tree=tree, main_config=main,
            replica_configs=configs, manager_command=manager, replica_commands=replicas,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                     + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
            repository_snapshot=snapshot, native_mode_revision_check=lambda _revision: True,
        )


def test_dry_run_rejects_dirty_or_nonpushed_revision_before_writing_output(tmp_path: Path) -> None:
    tree, main, configs, manager, replicas, _snapshot = _fixture(tmp_path)
    with pytest.raises(subject.SustainedRoleProducerError, match="clean pushed"):
        subject.prepare_dry_run(
            tmp_path / "dry-run", arm="fixed_e0", epoch0_tree=tree, main_config=main,
            replica_configs=configs, manager_command=manager, replica_commands=replicas,
            window_start_monotonic_ns=100,
            window_end_monotonic_ns=(100 + subject.profile.COMMON_HORIZON_NS
                                     + subject.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
            repository_snapshot=lambda _path: SimpleNamespace(revision="a" * 40, worktree_clean=False),
            native_mode_revision_check=lambda _revision: True,
        )
