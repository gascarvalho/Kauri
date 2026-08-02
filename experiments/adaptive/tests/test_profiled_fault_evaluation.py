"""Contract tests for the profile-driven N=31 crash shakedown."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _api():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.profiled_fault_evaluation"
    )


def _runtime():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.profiled_fault_runtime"
    )


def _profile(
    *,
    replica_count: int = 31,
    fanout: int = 5,
    crash_replica_id: int = 0,
) -> dict[str, object]:
    fault_threshold = (replica_count - 1) // 3
    quorum = 2 * fault_threshold + 1
    return {
        "schema_version": 1,
        "profile_id": (f"n{replica_count}-f{fanout}-q{quorum}-" "sigkill-shakedown-v1"),
        "frozen": True,
        "replica_ids": list(range(replica_count)),
        "fault_threshold": fault_threshold,
        "quorum": quorum,
        "fanout": fanout,
        "pipeline_depth": 2,
        "epoch0_roots": list(range(replica_count)),
        "epoch0_members_breadth_first": (
            [replica_count - 1] + list(range(replica_count - 1))
        ),
        "authoritative_observer": 2,
        "snapshot_seed": 41719,
        "ports": {
            "peer_base": 25100,
            "client_base": 26100,
            "manager": 27100,
        },
        "fault": {
            "fault_id": "single-internal-sigkill",
            "kind": "replica_group_sigkill",
            "replica_id": crash_replica_id,
            "tree_id": replica_count - 1,
        },
        "attempt_count": 1,
        "retry_failed_attempts": False,
        "require_successor_activation": False,
    }


def _write_profile(tmp_path: Path, value: dict[str, object]) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


def test_loads_n31_fanout_five_profile_and_derives_fixed_quorum(
    tmp_path: Path,
) -> None:
    api = _api()
    loaded = api.load_frozen_profile(_write_profile(tmp_path, _profile()))

    assert loaded.replica_ids == tuple(range(31))
    assert loaded.fault_threshold == 10
    assert loaded.quorum == 21
    assert loaded.fanout == 5
    assert loaded.pipeline_depth == 2
    assert loaded.fault.kind == "replica_group_sigkill"
    assert loaded.fault.replica_id == 0
    assert loaded.fault.tree_id == 30
    assert loaded.crash_subtree == (0, 5, 6, 7, 8, 9)
    assert len(loaded.profile_sha256) == 64
    assert loaded.attempt_count == 1
    assert loaded.retry_failed_attempts is False
    assert loaded.require_successor_activation is False


@pytest.mark.parametrize("replica_count", (0, 8, 30, 32))
def test_rejects_membership_sizes_that_are_not_n_equals_three_f_plus_one(
    tmp_path: Path,
    replica_count: int,
) -> None:
    api = _api()
    value = _profile()
    value["replica_ids"] = list(range(replica_count))

    with pytest.raises(
        api.ProfiledFaultEvaluationError,
        match="3f|membership|replica",
    ):
        api.load_frozen_profile(_write_profile(tmp_path, value))


def test_rejects_declared_quorum_and_fault_threshold_drift(
    tmp_path: Path,
) -> None:
    api = _api()
    for field, value in (("fault_threshold", 9), ("quorum", 20)):
        raw = _profile()
        raw[field] = value
        with pytest.raises(
            api.ProfiledFaultEvaluationError,
            match=field.replace("_", " ") + "|quorum|fault",
        ):
            api.load_frozen_profile(_write_profile(tmp_path, raw))


def test_rejects_topology_membership_mismatch(tmp_path: Path) -> None:
    api = _api()
    raw = _profile()
    members = list(raw["epoch0_members_breadth_first"])
    members[-1] = members[0]
    raw["epoch0_members_breadth_first"] = members

    with pytest.raises(
        api.ProfiledFaultEvaluationError,
        match="topology|membership|breadth",
    ):
        api.load_frozen_profile(_write_profile(tmp_path, raw))


def test_rejects_a_crash_branch_that_can_hide_the_fixed_quorum(
    tmp_path: Path,
) -> None:
    api = _api()
    raw = _profile(fanout=2, crash_replica_id=0)

    with pytest.raises(
        api.ProfiledFaultEvaluationError,
        match="branch|quorum|subtree",
    ):
        api.load_frozen_profile(_write_profile(tmp_path, raw))


def test_accepts_a_safe_fanout_two_internal_branch(tmp_path: Path) -> None:
    api = _api()
    profile = _profile(fanout=2, crash_replica_id=2)
    profile["authoritative_observer"] = 1
    loaded = api.load_frozen_profile(_write_profile(tmp_path, profile))

    assert loaded.crash_subtree == (2, 6, 7, 14, 15, 16, 17)
    assert len(loaded.crash_subtree) <= 31 - loaded.quorum


@pytest.mark.parametrize("crash_replica_id", (30, 5))
def test_rejects_root_and_leaf_crash_targets(
    tmp_path: Path,
    crash_replica_id: int,
) -> None:
    api = _api()

    with pytest.raises(
        api.ProfiledFaultEvaluationError,
        match="internal|leader|leaf|root",
    ):
        api.load_frozen_profile(
            _write_profile(
                tmp_path,
                _profile(crash_replica_id=crash_replica_id),
            )
        )


def test_breadth_first_geometry_is_position_based_and_canonical() -> None:
    api = _api()
    members = tuple(range(31))

    assert api.breadth_first_subtree_at(members, 5, 1) == (
        1,
        6,
        7,
        8,
        9,
        10,
    )
    assert api.breadth_first_subtree_at(members, 5, 6) == (6,)
    with pytest.raises(
        api.ProfiledFaultEvaluationError,
        match="position|fanout",
    ):
        api.breadth_first_subtree_at(members, 0, 1)


def test_n31_profile_reserves_exactly_sixty_three_unique_ports(
    tmp_path: Path,
) -> None:
    api = _api()
    loaded = api.load_frozen_profile(_write_profile(tmp_path, _profile()))

    ports = api.required_ports(loaded)
    assert len(ports) == 63
    assert len(set(ports)) == 63
    assert ports[:31] == tuple(range(25100, 25131))
    assert ports[31:62] == tuple(range(26100, 26131))
    assert ports[-1] == 27100


def test_identity_commands_derive_counts_from_membership(
    tmp_path: Path,
) -> None:
    api = _api()
    loaded = api.load_frozen_profile(_write_profile(tmp_path, _profile()))

    commands = api.identity_generation_commands(
        loaded,
        keygen_binary=Path("/build/hotstuff-keygen"),
        tls_keygen_binary=Path("/build/hotstuff-tls-keygen"),
    )
    assert commands == {
        "bls": (
            "/build/hotstuff-keygen",
            "--num",
            "31",
            "--algo",
            "bls",
        ),
        "tls": (
            "/build/hotstuff-tls-keygen",
            "--num",
            "32",
        ),
        "issuer": (
            "/build/hotstuff-keygen",
            "--num",
            "1",
            "--algo",
            "secp256k1",
        ),
    }


def test_initial_epoch_and_replica_argv_cover_exact_n31_membership(
    tmp_path: Path,
) -> None:
    api = _api()
    loaded = api.load_frozen_profile(_write_profile(tmp_path, _profile()))

    epoch = api.initial_epoch_input(loaded)
    assert epoch["replica_count"] == 31
    assert epoch["fault_threshold"] == 10
    assert epoch["quorum"] == 21
    assert len(epoch["epoch0_trees"]) == 31
    assert epoch["epoch0_trees"][0]["members_breadth_first"] == list(range(31))
    assert epoch["epoch0_trees"][1]["members_breadth_first"] == (
        list(range(1, 31)) + [0]
    )
    assert epoch["epoch0_trees"][30]["members_breadth_first"] == (
        [30] + list(range(30))
    )
    assert all(tree["fanout"] == 5 for tree in epoch["epoch0_trees"])

    commands = api.replica_argvs(
        loaded,
        app_binary=Path("/build/hotstuff-app"),
        config_directory=Path("/run/config"),
    )
    assert len(commands) == 31
    assert commands[0] == (
        "/build/hotstuff-app",
        "--conf",
        "/run/config/main.conf",
        "--conf",
        "/run/config/replica-0.conf",
    )
    assert commands[-1][-1] == "/run/config/replica-30.conf"


def test_shipped_profile_pins_runtime_and_external_q21_witnesses() -> None:
    api = _api()
    runtime = _runtime()
    profile_path = (
        Path(__file__).resolve().parents[1]
        / "profiles"
        / "n31-f5-crash-shakedown-v1.json"
    )

    profile = api.load_frozen_profile(profile_path)

    runtime.require_shipped_profile(profile)
    assert profile.profile_sha256 == runtime.SHIPPED_PROFILE_SHA256
    assert profile.block_size == 1000
    assert profile.tree_switch_period_blocks == 1
    assert profile.bucket_width_s == 5
    assert profile.baseline_bucket_count == 6
    assert profile.post_bucket_count == 6
    assert profile.leader_progress_timeout_s == 5
    assert profile.leader_activation_grace_s == 1
    assert api.postfault_witnesses(profile) == (
        1,
        2,
        3,
        4,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
    )


def test_internal1_profile_changes_only_the_controlled_crash_position() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    original_path = profiles / "n31-f5-crash-shakedown-v1.json"
    internal1_path = profiles / "n31-f5-internal1-crash-shakedown-v1.json"
    original_document = json.loads(original_path.read_text(encoding="utf-8"))
    internal1_document = json.loads(internal1_path.read_text(encoding="utf-8"))
    original_fault = original_document.pop("fault")
    internal1_fault = internal1_document.pop("fault")
    original_document.pop("profile_id")
    internal1_document.pop("profile_id")
    original_fault.pop("fault_id")
    internal1_fault.pop("fault_id")
    original_fault.pop("replica_id")
    internal1_fault.pop("replica_id")

    assert internal1_document == original_document
    assert internal1_fault == original_fault

    original = api.load_frozen_profile(original_path)
    internal1 = api.load_frozen_profile(internal1_path)

    runtime.require_shipped_profile(internal1)
    assert internal1.profile_id == "n31-f5-q21-internal1-sigkill-shakedown-v1"
    assert internal1.fault.fault_id == "single-internal-replica1-sigkill"
    assert internal1.fault.replica_id == 1
    assert internal1.fault.tree_id == 30
    assert internal1.crash_subtree == (1, 10, 11, 12, 13, 14)
    assert api.postfault_witnesses(internal1) == (
        0,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
    )
    assert internal1.aggregation_timeout_s == original.aggregation_timeout_s == 0.5
    assert internal1.replica_ids == original.replica_ids
    assert internal1.quorum == original.quorum == 21
    assert internal1.fanout == original.fanout == 5
    assert internal1.pipeline_depth == original.pipeline_depth == 2
    assert internal1.attempt_count == 1
    assert internal1.retry_failed_attempts is False


def test_internal1_handoff_diagnostic_changes_only_identity_metadata() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = profiles / "n31-f5-internal1-crash-shakedown-v1.json"
    diagnostic_path = (
        profiles / "n31-f5-internal1-handoff-diagnostic-v1.json"
    )
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(
        diagnostic_path.read_text(encoding="utf-8")
    )
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault

    control = api.load_frozen_profile(control_path)
    diagnostic = api.load_frozen_profile(diagnostic_path)

    runtime.require_shipped_profile(diagnostic)
    assert diagnostic.profile_id == (
        "n31-f5-q21-internal1-handoff-diagnostic-v1"
    )
    assert diagnostic.fault.fault_id == (
        "single-internal-replica1-handoff-diagnostic"
    )
    assert diagnostic.fault.replica_id == control.fault.replica_id == 1
    assert diagnostic.profile_sha256 == (
        "daea7057ef840706dfc8d060c1fdcac538a54a23850084b5eb84ca431eab61c6"
    )


def test_manager_argv_freezes_containment_without_exposing_secrets(
    tmp_path: Path,
) -> None:
    api = _api()
    runtime = _runtime()
    profile_path = (
        Path(__file__).resolve().parents[1]
        / "profiles"
        / "n31-f5-crash-shakedown-v1.json"
    )
    profile = api.load_frozen_profile(profile_path)
    tls = [
        {"crt": f"crt-{index}", "sec": f"sec-{index}", "cid": f"cid-{index}"}
        for index in range(32)
    ]
    command = runtime.manager_argv(
        profile,
        manager_binary=Path("/build/adaptation-manager"),
        tls=tls,
        issuer={"pub": "issuer-pub", "sec": "issuer-sec"},
        run_directory=tmp_path,
        run_id="run-n31",
        source_instance="run-n31-manager",
    )

    assert command[command.index("--tree-fanout") + 1] == "5"
    assert command[command.index("--pipeline-stretch") + 1] == "2"
    assert command.count("--replica") == 31
    request = json.loads(command[command.index("--transition-request") + 1])
    assert request["policy_intent"] == "fault_containment"
    assert request["minimum_predecessor_residency_ms"] == 0
    assert request["transition_artifact_id"] == ("e0-to-e1-shakedown-containment")
    assert request["policy_parameters"]["containment_baseline_roots"] == [
        {"tree_id": replica, "replica_id": replica} for replica in range(21)
    ]
    normalized = runtime.normalized_manager_argv(command)
    assert "issuer-sec" not in normalized
    assert "sec-31" not in normalized
    assert all("crt-" not in argument for argument in normalized)
