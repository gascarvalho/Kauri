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
    assert loaded.minimum_positive_postfault_buckets == 0
    assert loaded.minimum_mean_throughput_retention == 0.0


def test_loads_optional_strict_postfault_recovery_requirements(
    tmp_path: Path,
) -> None:
    api = _api()
    raw = _profile()
    raw["minimum_positive_postfault_buckets"] = 5
    raw["minimum_mean_throughput_retention"] = 0.8

    loaded = api.load_frozen_profile(_write_profile(tmp_path, raw))

    assert loaded.minimum_positive_postfault_buckets == 5
    assert loaded.minimum_mean_throughput_retention == 0.8


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("minimum_positive_postfault_buckets", -1, "positive postfault buckets"),
        ("minimum_positive_postfault_buckets", 7, "positive postfault buckets"),
        ("minimum_positive_postfault_buckets", 1.5, "integer"),
        ("minimum_mean_throughput_retention", -0.01, "throughput retention"),
        ("minimum_mean_throughput_retention", float("nan"), "throughput retention"),
    ),
)
def test_rejects_invalid_strict_postfault_recovery_requirements(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    api = _api()
    raw = _profile()
    raw[field] = value

    with pytest.raises(api.ProfiledFaultEvaluationError, match=message):
        api.load_frozen_profile(_write_profile(tmp_path, raw))


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
    diagnostic_path = profiles / "n31-f5-internal1-handoff-diagnostic-v1.json"
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
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
    assert diagnostic.profile_id == ("n31-f5-q21-internal1-handoff-diagnostic-v1")
    assert diagnostic.fault.fault_id == ("single-internal-replica1-handoff-diagnostic")
    assert diagnostic.fault.replica_id == control.fault.replica_id == 1
    assert diagnostic.profile_sha256 == (
        "daea7057ef840706dfc8d060c1fdcac538a54a23850084b5eb84ca431eab61c6"
    )


def test_internal1_handoff_tail_diagnostic_changes_only_identity_metadata() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = profiles / "n31-f5-internal1-crash-shakedown-v1.json"
    diagnostic_path = profiles / "n31-f5-internal1-handoff-tail-diagnostic-v2.json"
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
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
    assert diagnostic.profile_id == ("n31-f5-q21-internal1-handoff-tail-diagnostic-v2")
    assert diagnostic.fault.fault_id == (
        "single-internal-replica1-handoff-tail-diagnostic"
    )
    assert diagnostic.fault.replica_id == control.fault.replica_id == 1
    assert diagnostic.profile_sha256 == (
        "369ba72c9a0ba92d74e128330905e2fcac2254a6e4b1e7e5d1377c418e033482"
    )


def test_internal1_forwarding_tail_diagnostic_changes_only_identity_metadata() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = profiles / "n31-f5-internal1-crash-shakedown-v1.json"
    diagnostic_path = profiles / "n31-f5-internal1-forwarding-tail-diagnostic-v3.json"
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
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
        "n31-f5-q21-internal1-forwarding-tail-diagnostic-v3"
    )
    assert diagnostic.fault.fault_id == (
        "single-internal-replica1-forwarding-tail-diagnostic"
    )
    assert diagnostic.fault.replica_id == control.fault.replica_id == 1
    assert diagnostic.profile_sha256 == (
        "fca3d8e0b99b6cd7c576e5db2d69c406ba13b3f19af30a2445de4f604bcb9e00"
    )


def test_internal1_repnet2_diagnostic_changes_only_identity_metadata() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = profiles / "n31-f5-internal1-crash-shakedown-v1.json"
    diagnostic_path = profiles / "n31-f5-internal1-repnet2-diagnostic-v4.json"
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
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
    assert diagnostic.profile_id == ("n31-f5-q21-internal1-repnet2-diagnostic-v4")
    assert diagnostic.fault.fault_id == ("single-internal-replica1-repnet2-diagnostic")
    assert diagnostic.fault.replica_id == control.fault.replica_id == 1
    assert diagnostic.profile_sha256 == (
        "c61aad2d835f4ffdac79e18a7373196d82d6b03a109009abad15b17966fb4561"
    )


def test_missing_signer_repair_diagnostic_changes_only_identity_metadata() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = profiles / "n31-f5-internal1-repnet2-diagnostic-v4.json"
    diagnostic_path = (
        profiles / "n31-f5-internal1-missing-signer-repair-diagnostic-v5.json"
    )
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault

    diagnostic = api.load_frozen_profile(diagnostic_path)
    runtime.require_shipped_profile(diagnostic)
    assert diagnostic.profile_id == (
        "n31-f5-q21-internal1-missing-signer-repair-diagnostic-v5"
    )
    assert diagnostic.fault.fault_id == (
        "single-internal-replica1-missing-signer-repair-diagnostic"
    )
    assert diagnostic.profile_sha256 == (
        "512d69ffe4df0acb50035cb65017cc2abfdce0238f3d2fe5504957f762e3cabc"
    )


def test_staged_repair_diagnostic_changes_only_identity_metadata() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = (
        profiles / "n31-f5-internal1-missing-signer-repair-diagnostic-v5.json"
    )
    diagnostic_path = profiles / "n31-f5-internal1-staged-repair-diagnostic-v6.json"
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault

    diagnostic = api.load_frozen_profile(diagnostic_path)
    runtime.require_shipped_profile(diagnostic)
    assert diagnostic.profile_id == ("n31-f5-q21-internal1-staged-repair-diagnostic-v6")
    assert diagnostic.fault.fault_id == (
        "single-internal-replica1-staged-repair-diagnostic"
    )
    assert diagnostic.profile_sha256 == (
        "19717485f292628fdc13a99c79ab9069b728b6377cde8630ad38f0c783f7c1a0"
    )


def test_commit_dwell_diagnostic_changes_only_cadence_and_identity() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = profiles / "n31-f5-internal1-staged-repair-diagnostic-v6.json"
    diagnostic_path = profiles / "n31-f5-internal1-commit-dwell-diagnostic-v7.json"
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")
    control_period = control_document.pop("tree_switch_period_blocks")
    diagnostic_period = diagnostic_document.pop("tree_switch_period_blocks")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault
    assert control_period == 1
    assert diagnostic_period == 5

    diagnostic = api.load_frozen_profile(diagnostic_path)
    runtime.require_shipped_profile(diagnostic)
    assert diagnostic.profile_id == ("n31-f5-q21-internal1-commit-dwell-diagnostic-v7")
    assert diagnostic.fault.fault_id == (
        "single-internal-replica1-commit-dwell-diagnostic"
    )
    assert diagnostic.tree_switch_period_blocks == 5
    assert diagnostic.profile_sha256 == (
        "c9e8d6385ced8c7d70b78d22d865cea893097f6e75bf71475edbbee45a0ae84f"
    )


def test_ack_tail_diagnostic_changes_only_identity_metadata() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = profiles / "n31-f5-internal1-commit-dwell-diagnostic-v7.json"
    diagnostic_path = profiles / "n31-f5-internal1-ack-tail-diagnostic-v8.json"
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault

    diagnostic = api.load_frozen_profile(diagnostic_path)
    runtime.require_shipped_profile(diagnostic)
    assert diagnostic.profile_id == ("n31-f5-q21-internal1-ack-tail-diagnostic-v8")
    assert diagnostic.fault.fault_id == ("single-internal-replica1-ack-tail-diagnostic")
    assert diagnostic.profile_sha256 == (
        "71f942e69735380216f4906e40d22c4456ee9a3cb3d5afa89c59af4690cc6750"
    )


def test_pre_qc_credit_diagnostic_changes_only_identity_metadata() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = profiles / "n31-f5-internal1-ack-tail-diagnostic-v8.json"
    diagnostic_path = profiles / "n31-f5-internal1-pre-qc-credit-diagnostic-v9.json"
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault

    diagnostic = api.load_frozen_profile(diagnostic_path)
    runtime.require_shipped_profile(diagnostic)
    assert diagnostic.profile_id == ("n31-f5-q21-internal1-pre-qc-credit-diagnostic-v9")
    assert diagnostic.fault.fault_id == (
        "single-internal-replica1-pre-qc-credit-diagnostic"
    )
    assert diagnostic.profile_sha256 == (
        "20be0d3e9a58c354e610a09441c9ad486d9db3f7f9fa5bd2b8d0ce58b382d539"
    )


def test_connection_refresh_diagnostic_changes_only_identity_metadata() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = profiles / "n31-f5-internal1-pre-qc-credit-diagnostic-v9.json"
    diagnostic_path = (
        profiles / "n31-f5-internal1-connection-refresh-diagnostic-v10.json"
    )
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault

    diagnostic = api.load_frozen_profile(diagnostic_path)
    runtime.require_shipped_profile(diagnostic)
    assert diagnostic.profile_id == (
        "n31-f5-q21-internal1-connection-refresh-diagnostic-v10"
    )
    assert diagnostic.fault.fault_id == (
        "single-internal-replica1-connection-refresh-diagnostic"
    )
    assert diagnostic.profile_sha256 == (
        "d85c9c97cf766730477dbbec601b70e4e3af637618425c682c8cc591a716f7bf"
    )


def test_connection_refresh_capacity_diagnostic_changes_only_identity_metadata() -> (
    None
):
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = profiles / "n31-f5-internal1-connection-refresh-diagnostic-v10.json"
    diagnostic_path = (
        profiles / "n31-f5-internal1-connection-refresh-capacity-diagnostic-v11.json"
    )
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault

    diagnostic = api.load_frozen_profile(diagnostic_path)
    runtime.require_shipped_profile(diagnostic)
    assert diagnostic.profile_id == (
        "n31-f5-q21-internal1-connection-refresh-capacity-diagnostic-v11"
    )
    assert diagnostic.fault.fault_id == (
        "single-internal-replica1-connection-refresh-capacity-diagnostic"
    )
    assert diagnostic.attempt_count == 1
    assert diagnostic.retry_failed_attempts is False
    assert diagnostic.profile_sha256 == (
        "36c4bdc9740a0bf92cb059f3e3068335102320d0a7db7f994ebbea6efb745b6e"
    )


def test_fresh_connection_coalescing_diagnostic_changes_only_identity_metadata() -> (
    None
):
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = (
        profiles / "n31-f5-internal1-connection-refresh-capacity-diagnostic-v11.json"
    )
    diagnostic_path = (
        profiles / "n31-f5-internal1-fresh-connection-coalescing-diagnostic-v12.json"
    )
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault

    diagnostic = api.load_frozen_profile(diagnostic_path)
    runtime.require_shipped_profile(diagnostic)
    assert diagnostic.profile_id == (
        "n31-f5-q21-internal1-fresh-connection-coalescing-diagnostic-v12"
    )
    assert diagnostic.fault.fault_id == (
        "single-internal-replica1-fresh-connection-coalescing-diagnostic"
    )
    assert diagnostic.attempt_count == 1
    assert diagnostic.retry_failed_attempts is False
    assert diagnostic.profile_sha256 == (
        "be6fa779f0f6920f50172ff751a6b2523b95e4cc4e116d00fe31e3e3ebd1b42d"
    )


def test_fresh_first_draining_repair_diagnostic_changes_only_identity_metadata() -> (
    None
):
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = (
        profiles / "n31-f5-internal1-fresh-connection-coalescing-diagnostic-v12.json"
    )
    diagnostic_path = (
        profiles / "n31-f5-internal1-fresh-first-draining-repair-diagnostic-v13.json"
    )
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault

    diagnostic = api.load_frozen_profile(diagnostic_path)
    runtime.require_shipped_profile(diagnostic)
    assert diagnostic.profile_id == (
        "n31-f5-q21-internal1-fresh-first-draining-repair-diagnostic-v13"
    )
    assert diagnostic.fault.fault_id == (
        "single-internal-replica1-fresh-first-draining-repair-diagnostic"
    )
    assert diagnostic.attempt_count == 1
    assert diagnostic.retry_failed_attempts is False
    assert diagnostic.profile_sha256 == (
        "6058cf54c21d842c4b28b5ef2ff2ff4e6adb5a4e100c80b7a7c6ef28de6862a7"
    )


def test_peer_identity_dispatch_diagnostic_changes_only_identity_metadata() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = (
        profiles / "n31-f5-internal1-fresh-first-draining-repair-diagnostic-v13.json"
    )
    diagnostic_path = (
        profiles / "n31-f5-internal1-peer-identity-dispatch-diagnostic-v14.json"
    )
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault

    diagnostic = api.load_frozen_profile(diagnostic_path)
    runtime.require_shipped_profile(diagnostic)
    assert diagnostic.profile_id == (
        "n31-f5-q21-internal1-peer-identity-dispatch-diagnostic-v14"
    )
    assert diagnostic.fault.fault_id == (
        "single-internal-replica1-peer-identity-dispatch-diagnostic"
    )
    assert diagnostic.attempt_count == 1
    assert diagnostic.retry_failed_attempts is False
    assert diagnostic.profile_sha256 == (
        "0b73b9c3485cf9b3e74d09c68260cad61219d43022c8aa0972f933b54125f6b8"
    )


def test_stable_tree_recovery_diagnostic_freezes_strict_gate() -> None:
    api = _api()
    runtime = _runtime()
    profile_path = (
        Path(__file__).resolve().parents[1]
        / "profiles"
        / "n31-f5-internal1-stable-tree-recovery-diagnostic-v15.json"
    )

    profile = api.load_frozen_profile(profile_path)

    runtime.require_shipped_profile(profile)
    assert profile.profile_id == (
        "n31-f5-q21-internal1-stable-tree-recovery-diagnostic-v15"
    )
    assert profile.profile_sha256 == (
        "5a640ae1c12e6b4fe6a2420dc9efbc95d617388a0a2d0304a334e703392ac471"
    )
    assert profile.fault.fault_id == (
        "single-internal-replica1-stable-tree-recovery-diagnostic"
    )
    assert profile.fault.tree_id == 0
    assert profile.fault.replica_id == 1
    assert profile.crash_subtree == (1, 6, 7, 8, 9, 10)
    assert profile.tree_switch_period_blocks == 100000
    assert profile.leader_progress_timeout_s == 20
    assert profile.minimum_positive_postfault_buckets == 6
    assert profile.minimum_mean_throughput_retention == 0.8
    assert profile.quorum == 21
    assert profile.attempt_count == 1
    assert profile.retry_failed_attempts is False


def test_fresh_tail_coverage_diagnostic_freezes_strict_gate() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = (
        profiles / "n31-f5-internal1-stable-tree-recovery-diagnostic-v15.json"
    )
    profile_path = (
        profiles / "n31-f5-internal1-fresh-tail-coverage-diagnostic-v16.json"
    )
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(profile_path.read_text(encoding="utf-8"))
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_document.pop("ports")
    diagnostic_ports = diagnostic_document.pop("ports")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault
    assert diagnostic_ports == {
        "peer_base": 25200,
        "client_base": 26200,
        "manager": 27200,
    }

    profile = api.load_frozen_profile(profile_path)

    runtime.require_shipped_profile(profile)
    assert profile.profile_id == (
        "n31-f5-q21-internal1-fresh-tail-coverage-diagnostic-v16"
    )
    assert profile.profile_sha256 == (
        "96daff4a11185cd397105c1cfa5183ab7ce4fb378b997d94db81ab9d6d4677d2"
    )
    assert profile.fault.fault_id == (
        "single-internal-replica1-fresh-tail-coverage-diagnostic"
    )
    assert profile.fault.tree_id == 0
    assert profile.fault.replica_id == 1
    assert profile.crash_subtree == (1, 6, 7, 8, 9, 10)
    assert profile.tree_switch_period_blocks == 100000
    assert profile.leader_progress_timeout_s == 20
    assert profile.minimum_positive_postfault_buckets == 6
    assert profile.minimum_mean_throughput_retention == 0.8
    assert profile.quorum == 21
    assert profile.attempt_count == 1
    assert profile.retry_failed_attempts is False


def test_parked_tail_late_qc_diagnostic_freezes_strict_gate() -> None:
    api = _api()
    runtime = _runtime()
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    control_path = (
        profiles / "n31-f5-internal1-fresh-tail-coverage-diagnostic-v16.json"
    )
    profile_path = (
        profiles / "n31-f5-internal1-parked-tail-late-qc-diagnostic-v17.json"
    )
    control_document = json.loads(control_path.read_text(encoding="utf-8"))
    diagnostic_document = json.loads(profile_path.read_text(encoding="utf-8"))
    control_fault = control_document.pop("fault")
    diagnostic_fault = diagnostic_document.pop("fault")
    control_document.pop("profile_id")
    diagnostic_document.pop("profile_id")
    control_document.pop("ports")
    diagnostic_ports = diagnostic_document.pop("ports")
    control_fault.pop("fault_id")
    diagnostic_fault.pop("fault_id")

    assert diagnostic_document == control_document
    assert diagnostic_fault == control_fault
    assert diagnostic_ports == {
        "peer_base": 25300,
        "client_base": 26300,
        "manager": 27300,
    }

    profile = api.load_frozen_profile(profile_path)

    runtime.require_shipped_profile(profile)
    assert profile.profile_id == (
        "n31-f5-q21-internal1-parked-tail-late-qc-diagnostic-v17"
    )
    assert profile.profile_sha256 == (
        "e584f3949e384c9fa1099d62f7d28066e14a9f0a76043c8db2db9a630a54250d"
    )
    assert profile.fault.fault_id == (
        "single-internal-replica1-parked-tail-late-qc-diagnostic"
    )
    assert profile.fault.tree_id == 0
    assert profile.fault.replica_id == 1
    assert profile.crash_subtree == (1, 6, 7, 8, 9, 10)
    assert profile.tree_switch_period_blocks == 100000
    assert profile.leader_progress_timeout_s == 20
    assert profile.minimum_positive_postfault_buckets == 6
    assert profile.minimum_mean_throughput_retention == 0.8
    assert profile.quorum == 21
    assert profile.attempt_count == 1
    assert profile.retry_failed_attempts is False


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
