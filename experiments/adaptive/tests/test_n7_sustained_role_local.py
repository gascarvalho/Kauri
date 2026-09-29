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
    main = tmp_path / "main.conf"; main.write_text("main\n")
    app = tmp_path / "app"; manager = tmp_path / "manager"
    for binary in (app, manager):
        binary.write_text("binary\n"); binary.chmod(0o700)
    configs = [tmp_path / f"replica-{replica}.conf" for replica in range(7)]
    for config in configs:
        config.write_text("idx = 0\n")
    manager_argv = (
        str(manager), "--required-nonresponsive", "1", "--epoch-zero-tree-file", str(tree),
    )
    replicas = [(str(app), "--conf", str(main), "--conf", str(config)) for config in configs]
    snapshot = lambda _path: SimpleNamespace(revision="a" * 40, worktree_clean=True)
    return tree, main, configs, manager_argv, replicas, snapshot


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
    assert plan["epoch0"]["trees"][4].index(1) in (1, 2)
    assert plan["comparison"]["arm"] == "adaptive_e1"
    assert plan["comparison"]["paired_comparator"] == "fixed_e0"
    assert plan["comparison"]["effect_scope"] == "containment_epoch_package_vs_fixed_e0"
    assert plan["comparison"]["causal_boundary"] == "package_comparison_not_isolated_leaf_placement"
    evidence = plan["evidence_contract"]
    assert evidence["anchor"] == "first_admitted_native_e0_aggregate_omission"
    assert evidence["late_interval"] == {
        "start_after_anchor_ns": 20_000_000_000,
        "end_after_anchor_ns": 60_000_000_000,
        "both_arms_require_physical_omission": True,
    }
    assert evidence["adaptive_only"]["all_seven_e1_activation_deadline_after_anchor_ns"] == 20_000_000_000
    assert "native_process_logs_all_8" in evidence["future_receipt_descriptors"]
    assert plan["receipt_contract"]["kind"] == "kauri-n7-sustained-role-raw-bundle-receipt-v1"
    assert plan["scheduled_window"]["argv_pinned_before_launch"] is True
    assert plan["scheduled_window"]["attestation"]["is_not"] == "an_arm_or_gate"
    assert "--experiment-byzantine-mode" not in plan["commands"]["replicas"][0]["argv"]
    actor = plan["commands"]["replicas"][1]["argv"]
    assert actor[actor.index("--experiment-byzantine-mode") + 1] == subject.profile.NATIVE_MODE
    assert plan["no_retry"] is True and request["no_retry"] is True


def test_dry_run_rejects_v4_static_option_before_writing_output(tmp_path: Path) -> None:
    tree, main, configs, manager, replicas, snapshot = _fixture(tmp_path)
    replicas[1] += ("--experiment-omit-outbound-aggregate",)
    root = tmp_path / "dry-run"
    with pytest.raises(subject.SustainedRoleProducerError, match="v4/static"):
        subject.prepare_dry_run(
            root, arm="fixed_e0", epoch0_tree=tree, main_config=main, replica_configs=configs,
            manager_command=manager, replica_commands=replicas,
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
        replica_configs=configs, manager_command=manager, replica_commands=replicas,
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
