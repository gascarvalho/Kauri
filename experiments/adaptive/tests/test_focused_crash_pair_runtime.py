"""Red-first contracts for the focused N7/N31 crash-pair runnable layer."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields, is_dataclass, replace
import hashlib
import importlib
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest

from experiments.adaptive.kauri_experiment import faults, processes
from experiments.adaptive.kauri_experiment import factorial_validation
from experiments.adaptive.kauri_experiment import profiled_fault_archive
from experiments.adaptive.tests import test_n31_crash_pair_contract as native_fixture

RUNTIME = "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
PROFILE_ROOT = Path(__file__).parents[1] / "profiles"
N7_PROFILE = PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v1.json"
N31_PROFILE = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v1.json"
# FCRASH-H supersedes these immutable v1 inputs prospectively.  Keep the v1
# constants above: archived fixtures and validation continue to bind them.
N7_PROFILE_V2 = PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v2.json"
N31_PROFILE_V2 = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v2.json"
N7_PROFILE_V3 = PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v3.json"
N31_PROFILE_V3 = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v3.json"
PROFILE_KEYS = {
    "schema_version",
    "profile_id",
    "frozen",
    "execution_class",
    "campaign_member",
    "figure_eligible",
    "protocol",
    "topology",
    "fault",
    "matched_inputs",
    "transitions",
    "timers",
    "measurement",
    "performance",
    "thresholds",
    "ports",
    "campaign",
    "blinding",
}


def _runtime() -> Any:
    return importlib.import_module(RUNTIME)


def _document(value: object) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    assert isinstance(value, dict)
    return value


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _canonical_profile_sha256(raw: Mapping[str, object]) -> str:
    identity = deepcopy(dict(raw))
    topology = identity.get("topology")
    assert isinstance(topology, dict)
    topology.pop("proof_sha256", None)
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _topology_proof_path(profile_path: Path, raw: Mapping[str, object]) -> Path:
    topology = raw["topology"]
    assert isinstance(topology, dict)
    relative = topology["proof_path"]
    assert isinstance(relative, str)
    return profile_path.parent / relative


def _fcrash_h_profile(path: Path) -> object:
    """Load a prospective immutable profile without falling back to v1."""

    assert path.is_file(), f"FCRASH-H profile is absent: {path.name}"
    return _runtime().load_focused_profile(path)


def _coverage_plan_document(value: object) -> dict[str, Any]:
    """Keep the public plan serializable for preflight sealing."""

    return _document(value)


def _assert_complete_topology_proof(
    proof: Mapping[str, object],
    *,
    replica_count: int,
    fanout: int,
    targets: Sequence[int],
) -> None:
    order = proof["bfs_member_order"]
    members = proof["members"]
    descendants = proof["internal_descendant_sets"]
    derivation = proof["target_derivation"]
    assert isinstance(order, list) and len(order) == replica_count
    assert set(order) == set(range(replica_count))
    assert isinstance(members, list) and len(members) == replica_count
    assert isinstance(descendants, dict)
    assert isinstance(derivation, dict)
    by_replica = {row["replica_id"]: row for row in members}
    assert set(by_replica) == set(order)

    children_by_index = {
        index: tuple(
            child
            for child in range(index * fanout + 1, index * fanout + fanout + 1)
            if child < replica_count
        )
        for index in range(replica_count)
    }

    def subtree(index: int) -> tuple[int, ...]:
        children = children_by_index[index]
        return tuple(
            member for child in children for member in (order[child], *subtree(child))
        )

    depths = [0] * replica_count
    for index in range(1, replica_count):
        depths[index] = depths[(index - 1) // fanout] + 1
    for index, replica in enumerate(order):
        role = (
            "root" if index == 0 else "internal" if children_by_index[index] else "leaf"
        )
        assert by_replica[replica] == {
            "replica_id": replica,
            "bfs_index": index,
            "depth": depths[index],
            "role": role,
        }
    internal_indices = [
        index for index, children in children_by_index.items() if children
    ]
    assert set(descendants) == {str(order[index]) for index in internal_indices}
    assert descendants == {
        str(order[index]): list(subtree(index)) for index in internal_indices
    }
    nonroot_internal_indices = [index for index in internal_indices if index != 0]
    deepest_internal_depth = max(depths[index] for index in nonroot_internal_indices)
    deepest = [
        order[index]
        for index in nonroot_internal_indices
        if depths[index] == deepest_internal_depth
    ]
    assert derivation["deepest_member_ids"] == deepest
    assert derivation["selected_target_replica_ids"] == list(targets)
    assert all(target != order[0] for target in targets)
    assert set(targets).issubset(deepest)
    target_descendants = [set(descendants[str(target)]) for target in targets]
    pairwise_disjoint = all(
        left.isdisjoint(right)
        for left, right in itertools.combinations(target_descendants, 2)
    )
    assert pairwise_disjoint
    assert derivation["pairwise_disjoint"] is True


@pytest.mark.parametrize("profile_path", (N7_PROFILE, N31_PROFILE))
def test_focused_adapter_leaves_the_full_fallback_horizon_before_suspicion(
    profile_path: Path,
) -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(profile_path)
    adapter = runtime._profiled_adapter(profile, 41_720)
    maximum_tree_level_count = 3
    fallback_horizon = (
        2 * (maximum_tree_level_count + 1) * adapter.aggregation_timeout_s
    )

    assert fallback_horizon < (
        adapter.leader_activation_grace_s + adapter.leader_progress_timeout_s
    )


@pytest.mark.parametrize(
    ("profile_path", "expected"),
    (
        (
            N7_PROFILE_V3,
            {
                "profile_id": "n7-f2-q5-two-crash-pair-smoke-v3",
                "active_tree_id": 6,
                "horizon_tree_positions": 6,
                "required_qualifying_reporters": 3,
                "minimum_timeouts_per_reporter": 2,
                "minimum_score_drop": 6,
                # The intersection is intentional: all targets must have the
                # same honest reporters, not merely individually possible ones.
                "reporter_ids": (4, 5, 6),
                "targets": (0, 1),
                "coverage": {
                    0: ((1, 6, 6), (4, 2, 4), (6, 4, 5)),
                    1: ((1, 6, 6), (4, 2, 4), (5, 3, 5)),
                },
                "deadlines": {
                    "evidence_seconds": 120,
                    "epoch1_activation_seconds": 180,
                    "optimization_activation_seconds": 90,
                    "arm_hard_seconds": 330,
                },
            },
        ),
        (
            N31_PROFILE_V3,
            {
                "profile_id": "n31-f5-q21-three-crash-pair-v3",
                "active_tree_id": 20,
                "horizon_tree_positions": 16,
                "required_qualifying_reporters": 11,
                "minimum_timeouts_per_reporter": 2,
                "minimum_score_drop": 22,
                # The 11 reporters are the common honest set accumulated by
                # trees 20,21,...,4; crashed roots/parents 22,23,24 are absent.
                "reporter_ids": (*range(8), 20, 21, 30),
                "targets": (22, 23, 24),
                "coverage": {
                    22: (
                        (1, 20, 20),
                        (2, 21, 21),
                        (6, 25, 30),
                        (7, 26, 0),
                        (8, 27, 1),
                        (10, 29, 2),
                        (11, 30, 3),
                        (12, 0, 4),
                        (13, 1, 5),
                        (15, 3, 6),
                        (16, 4, 7),
                    ),
                    23: (
                        (1, 20, 20),
                        (2, 21, 21),
                        (6, 25, 30),
                        (7, 26, 0),
                        (8, 27, 1),
                        (9, 28, 2),
                        (11, 30, 3),
                        (12, 0, 4),
                        (13, 1, 5),
                        (14, 2, 6),
                        (16, 4, 7),
                    ),
                    24: (
                        (1, 20, 20),
                        (2, 21, 21),
                        (6, 25, 30),
                        (7, 26, 0),
                        (8, 27, 1),
                        (9, 28, 2),
                        (10, 29, 3),
                        (12, 0, 4),
                        (13, 1, 5),
                        (14, 2, 6),
                        (15, 3, 7),
                    ),
                },
                "deadlines": {
                    "evidence_seconds": 180,
                    "epoch1_activation_seconds": 270,
                    "optimization_activation_seconds": 90,
                    "arm_hard_seconds": 420,
                },
            },
        ),
    ),
)
def test_fcrash_h_coverage_plan_is_common_honest_and_bounded(
    profile_path: Path,
    expected: Mapping[str, object],
) -> None:
    """The profile is rejected unless every target has f+1 honest reporters."""

    runtime = _runtime()
    profile = _fcrash_h_profile(profile_path)
    assert profile.profile_id == expected["profile_id"]
    plan = _coverage_plan_document(runtime.derive_reporter_coverage_plan(profile))

    assert plan["profile_id"] == expected["profile_id"]
    assert plan["active_tree_id"] == expected["active_tree_id"]
    assert plan["horizon_tree_positions"] == expected["horizon_tree_positions"]
    assert (
        plan["required_qualifying_reporters"]
        == expected["required_qualifying_reporters"]
    )
    assert plan["minimum_timeouts_per_reporter"] == 2
    assert plan["minimum_score_drop"] == expected["minimum_score_drop"]
    assert plan["deadlines_seconds"] == expected["deadlines"]
    assert plan["stable_phase_seconds"] == 30
    assert plan["readiness_timeout_seconds"] == 60
    assert plan["manager_convergence_timeout_seconds"] == 60

    targets = plan["targets"]
    assert isinstance(targets, list)
    assert targets == [
        {
            "target_replica_id": target,
            "authenticated_reporter_ids": list(expected["reporter_ids"]),
            "first_qualifying_reporters": [
                {
                    "tree_position": position,
                    "tree_id": tree_id,
                    "reporter_id": reporter_id,
                }
                for position, tree_id, reporter_id in expected["coverage"][target]
            ],
        }
        for target in expected["targets"]
    ]
    crashed = set(expected["targets"])
    assert crashed.isdisjoint(expected["reporter_ids"])
    assert len(expected["reporter_ids"]) == expected["required_qualifying_reporters"]


@pytest.mark.parametrize("profile_path", (N7_PROFILE_V3, N31_PROFILE_V3))
def test_fcrash_h_deadlines_are_absolute_half_open_intervals(
    profile_path: Path,
) -> None:
    """A phase deadline accepts t < end and rejects the exact endpoint."""

    runtime = _runtime()
    profile = _fcrash_h_profile(profile_path)
    plan = _coverage_plan_document(runtime.derive_reporter_coverage_plan(profile))
    deadlines = plan["deadlines_seconds"]
    assert isinstance(deadlines, Mapping)
    fault_ns = 9_000_000_000
    epoch1_ns = (
        fault_ns + int(deadlines["epoch1_activation_seconds"]) * 1_000_000_000 - 1
    )
    for phase, deadline_seconds in deadlines.items():
        origin_ns = (
            epoch1_ns if phase == "optimization_activation_seconds" else fault_ns
        )
        end_ns = origin_ns + int(deadline_seconds) * 1_000_000_000
        assert runtime.is_before_fcrash_h_deadline(
            origin_ns, end_ns - 1, int(deadline_seconds)
        )
        assert not runtime.is_before_fcrash_h_deadline(
            origin_ns, end_ns, int(deadline_seconds)
        )
        assert not runtime.is_before_fcrash_h_deadline(
            origin_ns, end_ns + 1, int(deadline_seconds)
        )


@pytest.mark.parametrize("profile_path", (N7_PROFILE_V3, N31_PROFILE_V3))
def test_fcrash_h_requires_all_member_exact_active_configuration_before_fault(
    profile_path: Path,
) -> None:
    """Process readiness alone cannot authorize the SIGKILL causal hook."""

    runtime = _runtime()
    profile = _fcrash_h_profile(profile_path)
    plan = _coverage_plan_document(runtime.derive_reporter_coverage_plan(profile))
    members = tuple(profile.replica_ids)
    active_tree = int(plan["active_tree_id"])
    configuration = {
        "epoch_number": 0,
        "tree_id": active_tree,
        "epoch_digest": profile.raw["topology"]["epoch_zero_digest"],
    }
    barrier = [
        {"replica_id": replica, "configuration": dict(configuration)}
        for replica in members
    ]
    assert runtime.has_exact_active_configuration_barrier(profile, barrier)

    assert not runtime.has_exact_active_configuration_barrier(profile, barrier[:-1])
    wrong_tree = deepcopy(barrier)
    wrong_tree[-1]["configuration"]["tree_id"] = (active_tree + 1) % len(members)
    assert not runtime.has_exact_active_configuration_barrier(profile, wrong_tree)


def _fcrash_h_snapshots(profile: object, arm: str) -> dict[str, Mapping[str, object]]:
    """A v3 state-machine witness with the exact pre-fault evidence boundary."""

    replicas = len(profile.replica_ids)
    snapshots = _arm_snapshots(replicas, arm)
    plan = _coverage_plan_document(_runtime().derive_reporter_coverage_plan(profile))
    fault_ns = 2_000_000_000
    configuration = {
        "epoch_number": 0,
        "tree_id": plan["active_tree_id"],
        "epoch_digest": profile.raw["topology"]["epoch_zero_digest"],
    }
    snapshots["baseline"] = {
        "stable": True,
        "source_monotonic_ns": fault_ns - 1,
        "active_configuration_barrier": [
            {"replica_id": replica, "configuration": dict(configuration)}
            for replica in profile.replica_ids
        ],
    }
    snapshots["fault"] = {
        **snapshots["fault"],
        "source_monotonic_ns": fault_ns,
    }
    timeout_observations: list[dict[str, object]] = []
    for target in plan["targets"]:
        for reporter in target["authenticated_reporter_ids"]:
            for ordinal in range(2):
                timeout_observations.append(
                    {
                        "epoch_number": 0,
                        "observed_replica_id": target["target_replica_id"],
                        "reporter_id": reporter,
                        "outcome": "timeout",
                        "compensated": False,
                        "source_monotonic_ns": fault_ns + 1 + ordinal,
                    }
                )
    snapshots["nonresponse"] = {
        **snapshots["nonresponse"],
        "source_monotonic_ns": fault_ns + 2,
        "snapshot_audit_monotonic_ns": fault_ns + 3,
        "timeout_observations": timeout_observations,
        "guard_drawdowns": {
            str(target["target_replica_id"]): -int(plan["minimum_score_drop"])
            for target in plan["targets"]
        },
        "qualifying_timeout_counts": {
            str(target["target_replica_id"]): {
                str(reporter): plan["minimum_timeouts_per_reporter"]
                for reporter in target["authenticated_reporter_ids"]
            }
            for target in plan["targets"]
        },
        **(
            {
                "postfault_progress": {
                    "required_tree_positions": plan["required_postfault_tree_positions"],
                    "actual_tree_positions": plan["required_postfault_tree_positions"],
                    "starting_tree_id": plan["active_tree_id"],
                    "observed_tree_ids": list(range(plan["required_postfault_tree_positions"])),
                }
            }
            if "required_postfault_tree_positions" in plan
            else {}
        ),
    }
    snapshots["epoch1"] = {
        **snapshots["epoch1"],
        "source_monotonic_ns": fault_ns + 3,
    }
    snapshots["commands1"] = {
        **snapshots["commands1"],
        "source_monotonic_ns": fault_ns + 4,
    }
    snapshots["activations1"] = {
        **snapshots["activations1"],
        "source_monotonic_ns": fault_ns + 5,
    }
    snapshots["commit1"] = {
        **snapshots["commit1"],
        "source_monotonic_ns": fault_ns + 6,
    }
    snapshots["containment"] = {
        **snapshots["containment"],
        "source_monotonic_ns": fault_ns + 7,
    }
    snapshots["ranking"] = {
        **snapshots["ranking"],
        "source_monotonic_ns": fault_ns + 8,
    }
    snapshots["epoch2"] = {
        **snapshots["epoch2"],
        "source_monotonic_ns": fault_ns + 9,
    }
    snapshots["commands2"] = {
        **snapshots["commands2"],
        "source_monotonic_ns": fault_ns + 10,
    }
    snapshots["activations2"] = {
        **snapshots["activations2"],
        "source_monotonic_ns": fault_ns + 11,
    }
    snapshots["commit2"] = {
        **snapshots["commit2"],
        "source_monotonic_ns": fault_ns + 12,
    }
    snapshots["late"] = {
        **snapshots["late"],
        "source_monotonic_ns": fault_ns + 13,
    }
    return snapshots


def _v3_raw_progress_events(profile: object) -> list[dict[str, object]]:
    run_id = "v3-progress-run"
    source_id = f"replica-{profile.raw['measurement']['authoritative_replica_id']}"
    instance = f"{run_id}-{source_id}-550e8400-e29b-41d4-a716-446655440000"
    digest = profile.raw["topology"]["epoch_zero_digest"]
    lifecycle = [
        {
            "run_id": run_id,
            "source_kind": "replica",
            "source_id": source_id,
            "source_instance": instance,
            "event_type": event_type,
            "source_monotonic_ns": 10 + index,
            "payload": {},
        }
        for index, event_type in enumerate(("process.started", "process.ready"))
    ]
    starting_tree = int(profile.raw["topology"]["active_tree_id"])
    members = list(profile.replica_ids)
    configurations = [
        {
            "run_id": run_id,
            "source_kind": "replica",
            "source_id": source_id,
            "source_instance": instance,
            "source_sequence": 3 + position,
            "source_monotonic_ns": 90 if position == 0 else 100 + position,
            "event_type": "adaptive.configuration_active",
            "payload": {
                "epoch_number": 0,
                "tree_id": members[(members.index(starting_tree) + position) % len(members)],
                "epoch_digest": digest,
            },
        }
        for position in range(6)
    ]
    commits = [
        {
            "run_id": run_id,
            "source_kind": "replica",
            "source_id": source_id,
            "source_instance": instance,
            "event_type": "block.committed",
            "source_sequence": index + 10,
            "source_monotonic_ns": 120 + index,
            "payload": {
                "block_height": index + 1,
                "block_hash": f"{index + 1:064x}",
                "parent_hash": "00" * 32 if index == 0 else f"{index:064x}",
                "transaction_count": 1000,
                "commit_batch_index": 0,
                "designated_observer": True,
                "view_generation": 1,
                "decision_proof": {
                    "epoch_number": 0,
                    "epoch_digest": digest,
                    "block_hash": f"{index + 1:064x}",
                    "tree_id": 4,
                },
            },
        }
        for index in range(12)
    ]
    return [*lifecycle, *configurations, *commits]


def test_v3_raw_progress_binds_native_uuid_lifecycle_instance() -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._profile = profile
    assert source._postfault_authoritative_progress(
        _v3_raw_progress_events(profile), fault_ns=100, audit_ns=200
    ) == {
        "required_tree_positions": 6,
        "actual_tree_positions": 6,
        "starting_tree_id": 6,
        "observed_tree_ids": [6, 0, 1, 2, 3, 4],
    }


@pytest.mark.parametrize("mutation", ("unbound", "mixed", "multiple"))
def test_v3_raw_progress_rejects_unbound_or_mixed_lifecycle_instances(
    mutation: str,
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._profile = profile
    events = _v3_raw_progress_events(profile)
    if mutation == "unbound":
        events.pop(1)
    elif mutation == "mixed":
        events[-1]["source_instance"] = "foreign-uuid"
    else:
        events[1]["source_instance"] = "other-uuid"
    if mutation == "mixed":
        assert source._postfault_authoritative_progress(
            events, fault_ns=100, audit_ns=200
        ) is not None
    else:
        with pytest.raises(runtime.FocusedCrashPairRuntimeError):
            source._postfault_authoritative_progress(
                events, fault_ns=100, audit_ns=200
            )


@pytest.mark.parametrize(
    "mutation",
    ("wrong-proof-hash", "wrong-proof-tree", "out-of-order"),
)
def test_v3_raw_progress_requires_one_consecutive_authoritative_commit_chain(
    mutation: str,
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._profile = profile
    events = _v3_raw_progress_events(profile)
    target = events[-1]
    if mutation == "wrong-proof-hash":
        target["payload"]["decision_proof"]["block_hash"] = "ff" * 32
    elif mutation == "wrong-proof-tree":
        target["payload"]["decision_proof"]["tree_id"] = 1
    else:
        target["source_sequence"] = 1
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        source._postfault_authoritative_progress(events, fault_ns=100, audit_ns=200)


@pytest.mark.parametrize("profile_path", (N7_PROFILE_V3, N31_PROFILE_V3))
def test_fcrash_h_state_machine_requires_exact_barrier_and_guarded_timeouts(
    profile_path: Path,
) -> None:
    """v3 cannot fault until its exact barrier and native timeout guard exist."""

    runtime = _runtime()
    profile = replace(
        _fcrash_h_profile(profile_path),
        issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
    )
    snapshots = _fcrash_h_snapshots(profile, "A")
    hooks, _ = _arm_hooks(runtime, snapshots)
    runtime._drive_arm_state_machine(profile, "A", "pair-01", hooks)

    mutations = {
        "missing-member": lambda: snapshots["baseline"][
            "active_configuration_barrier"
        ].pop(),
        "wrong-configuration": lambda: snapshots["baseline"][
            "active_configuration_barrier"
        ][-1]["configuration"].update(tree_id=0),
        "missing-reporter": lambda: snapshots["nonresponse"][
            "qualifying_timeout_counts"
        ][str(profile.target_replica_ids[0])].popitem(),
        "missing-progress": lambda: snapshots["nonresponse"].pop(
            "postfault_progress"
        ),
        "healed-score": lambda: snapshots["nonresponse"]["guard_drawdowns"].update(
            {str(profile.target_replica_ids[0]): 0}
        ),
        "epoch2-command-before-audit": lambda: snapshots["epoch2"].update(
            source_monotonic_ns=snapshots["commands2"]["source_monotonic_ns"]
        ),
    }
    for _name, mutate in mutations.items():
        rejected = _fcrash_h_snapshots(profile, "A")
        snapshots = rejected
        mutate()
        hooks, _ = _arm_hooks(runtime, rejected)
        with pytest.raises(runtime.FocusedCrashPairRuntimeError):
            runtime._drive_arm_state_machine(profile, "A", "pair-01", hooks)


@pytest.mark.parametrize("profile_path", (N7_PROFILE_V3, N31_PROFILE_V3))
@pytest.mark.parametrize("phase", ("nonresponse", "activations1", "activations2"))
def test_fcrash_h_state_machine_rejects_exact_deadline_endpoint(
    profile_path: Path,
    phase: str,
) -> None:
    """Evidence/E1 are fault-anchored; E2 is anchored to E1 activation."""

    runtime = _runtime()
    profile = replace(
        _fcrash_h_profile(profile_path),
        issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
    )
    snapshots = _fcrash_h_snapshots(profile, "A")
    plan = _coverage_plan_document(runtime.derive_reporter_coverage_plan(profile))
    deadlines = plan["deadlines_seconds"]
    fault_ns = snapshots["fault"]["source_monotonic_ns"]
    assert isinstance(fault_ns, int)
    if phase == "nonresponse":
        snapshots[phase]["snapshot_audit_monotonic_ns"] = (
            fault_ns + int(deadlines["evidence_seconds"]) * 1_000_000_000
        )
    elif phase == "activations1":
        snapshots[phase]["source_monotonic_ns"] = (
            fault_ns + int(deadlines["epoch1_activation_seconds"]) * 1_000_000_000
        )
    else:
        epoch1_ns = snapshots["activations1"]["source_monotonic_ns"]
        assert isinstance(epoch1_ns, int)
        snapshots[phase]["source_monotonic_ns"] = (
            epoch1_ns
            + int(deadlines["optimization_activation_seconds"]) * 1_000_000_000
        )
    hooks, _ = _arm_hooks(runtime, snapshots)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        runtime._drive_arm_state_machine(profile, "A", "pair-01", hooks)


@pytest.mark.parametrize(
    ("path", "expected"),
    (
        (
            N7_PROFILE,
            {
                "profile_id": "n7-f2-q5-two-crash-pair-smoke-v1",
                "execution_class": "excluded_n7_smoke",
                "campaign_member": False,
                "figure_eligible": False,
                "N": 7,
                "f": 2,
                "Q": 5,
                "fanout": 2,
                "pipeline_stretch": 2,
                "active_tree_id": 6,
                "targets": [0, 1],
            },
        ),
        (
            N31_PROFILE,
            {
                "profile_id": "n31-f5-q21-three-crash-pair-v1",
                "execution_class": "n31_focused",
                "campaign_member": True,
                "figure_eligible": True,
                "N": 31,
                "f": 10,
                "Q": 21,
                "fanout": 5,
                "pipeline_stretch": 2,
                "active_tree_id": 20,
                "targets": [22, 23, 24],
            },
        ),
    ),
)
def test_frozen_profiles_have_strict_schema_and_native_topology_binding(
    path: Path,
    expected: Mapping[str, object],
    tmp_path: Path,
) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == PROFILE_KEYS
    assert raw["schema_version"] == 1
    assert raw["frozen"] is True
    assert raw["profile_id"] == expected["profile_id"]
    assert raw["execution_class"] == expected["execution_class"]
    assert raw["campaign_member"] is expected["campaign_member"]
    assert raw["figure_eligible"] is expected["figure_eligible"]
    assert raw["protocol"] == {
        **raw["protocol"],
        "N": expected["N"],
        "f": expected["f"],
        "Q": expected["Q"],
        "fanout": expected["fanout"],
        "pipeline_stretch": expected["pipeline_stretch"],
    }
    assert raw["topology"]["active_tree_id"] == expected["active_tree_id"]
    assert raw["topology"]["reviewed_target_replica_ids"] == expected["targets"]
    assert len(raw["topology"]["epoch_zero_digest"]) == 64
    proof_path = _topology_proof_path(path, raw)
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    _assert_complete_topology_proof(
        proof,
        replica_count=int(expected["N"]),
        fanout=int(expected["fanout"]),
        targets=expected["targets"],  # type: ignore[arg-type]
    )
    topology_proof_sha256 = raw["topology"].pop("proof_sha256")
    assert isinstance(topology_proof_sha256, str)
    assert len(topology_proof_sha256) == 64
    assert topology_proof_sha256 == hashlib.sha256(proof_path.read_bytes()).hexdigest()
    raw["topology"]["proof_sha256"] = topology_proof_sha256

    profile = _runtime().load_focused_profile(path)
    assert profile.profile_id == expected["profile_id"]
    assert profile.profile_sha256 == _canonical_profile_sha256(raw)
    assert profile.topology_proof_sha256 == topology_proof_sha256

    proof_pointer_mutation = deepcopy(raw)
    proof_pointer_mutation["topology"]["proof_sha256"] = "0" * 64
    assert _canonical_profile_sha256(proof_pointer_mutation) == profile.profile_sha256

    mutation = deepcopy(raw)
    mutation["topology"]["reviewed_target_replica_ids"] = list(
        reversed(expected["targets"])
    )
    changed = tmp_path / path.name
    changed.write_text(json.dumps(mutation), encoding="utf-8")
    assert _canonical_profile_sha256(mutation) != profile.profile_sha256
    try:
        with pytest.raises(_runtime().FocusedCrashPairRuntimeError):
            _runtime().load_focused_profile(changed)
    finally:
        changed.unlink()


@pytest.mark.parametrize("mutation", ("order", "role", "depth", "descendants"))
def test_topology_proof_rejects_isolated_semantic_drift(
    mutation: str,
    tmp_path: Path,
) -> None:
    raw = json.loads(N31_PROFILE.read_text(encoding="utf-8"))
    proof = json.loads(
        _topology_proof_path(N31_PROFILE, raw).read_text(encoding="utf-8")
    )
    if mutation == "order":
        proof["bfs_member_order"][-2:] = reversed(proof["bfs_member_order"][-2:])
    elif mutation == "role":
        proof["members"][-1]["role"] = "internal"
    elif mutation == "depth":
        proof["members"][-1]["depth"] += 1
    else:
        first = next(iter(proof["internal_descendant_sets"]))
        proof["internal_descendant_sets"][first].pop()
    proof_path = tmp_path / raw["topology"]["proof_path"]
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    proof_path.write_bytes(_canonical_json(proof))
    raw["topology"]["proof_sha256"] = hashlib.sha256(
        proof_path.read_bytes()
    ).hexdigest()
    profile_path = tmp_path / N31_PROFILE.name
    profile_path.write_bytes(_canonical_json(raw))
    with pytest.raises(_runtime().FocusedCrashPairRuntimeError):
        _runtime().load_focused_profile(profile_path)


def test_n7_authoritative_observer_is_replica_two_and_cannot_be_crashed(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(N7_PROFILE)
    measurement = profile.raw["measurement"]
    observer = measurement["authoritative_replica_id"]
    assert observer == 2
    assert observer not in profile.target_replica_ids

    raw = deepcopy(profile.raw)
    raw["measurement"]["authoritative_replica_id"] = profile.target_replica_ids[0]
    proof = json.loads(profile.topology_proof_path.read_text(encoding="utf-8"))
    proof["profile_sha256"] = _canonical_profile_sha256(raw)
    proof_path = tmp_path / raw["topology"]["proof_path"]
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    proof_path.write_bytes(_canonical_json(proof))
    raw["topology"]["proof_sha256"] = hashlib.sha256(
        proof_path.read_bytes()
    ).hexdigest()
    changed = tmp_path / N7_PROFILE.name
    changed.write_bytes(_canonical_json(raw))
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        runtime.load_focused_profile(changed)


def test_preflight_is_no_launch_and_receipt_binds_every_execution_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    launched: list[object] = []
    monkeypatch.setattr(
        runtime,
        "spawn_owned_process",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )
    profile = runtime.load_focused_profile(N31_PROFILE)
    preflight = _document(
        runtime.prepare_focused_preflight(
            profile,
            mode="campaign",
            pair_count=5,
            output_root=tmp_path / "results",
        )
    )
    assert launched == []
    assert preflight["execution_authorized"] is False
    assert preflight["launch_permitted"] is False
    assert Path(preflight["preflight_path"]).is_file()
    assert Path(preflight["authorization_request_path"]).is_file()
    topology_path = Path(preflight["topology_proof_path"])
    topology = json.loads(topology_path.read_text(encoding="utf-8"))
    assert topology["source"] == "native_epoch_profile_digest"
    assert topology["profile_sha256"] == profile.profile_sha256
    assert (
        preflight["topology_proof_sha256"]
        == hashlib.sha256(topology_path.read_bytes()).hexdigest()
    )
    assert preflight["topology_proof_sha256"] == profile.topology_proof_sha256
    assert preflight["profile_sha256"] == profile.profile_sha256

    request = runtime.build_focused_authorization_request(preflight)
    request_document = json.loads(request)
    assert request_document["profile_sha256"] == profile.profile_sha256
    assert (
        request_document["topology_proof_sha256"] == preflight["topology_proof_sha256"]
    )
    receipt = {
        **request_document,
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "approval_reference": "thesis-author-approved-run-22",
        "approved_utc": "2026-08-11T12:00:00+00:00",
    }
    verified = _document(runtime.verify_focused_authorization_receipt(request, receipt))
    assert verified["execution_authorized"] is True
    assert verified["profile_sha256"] == profile.profile_sha256
    assert verified["automatic_retries"] == 0
    assert verified["replacement_policy"] == "none"

    for key in request_document:
        changed = deepcopy(receipt)
        changed[key] = "0" * 64
        with pytest.raises(runtime.FocusedCrashPairRuntimeError):
            runtime.verify_focused_authorization_receipt(request, changed)


def test_preflight_runs_real_checks_and_binds_issuer_before_execution(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(N7_PROFILE)
    calls: list[str] = []

    class Checks:
        def __init__(self, failing: str | None = None) -> None:
            self.failing = failing

        def _result(
            self, name: str, value: Mapping[str, object]
        ) -> Mapping[str, object]:
            calls.append(name)
            if self.failing == name:
                raise runtime.FocusedCrashPairRuntimeError(f"{name} check failed")
            return value

        def repository(self, _profile: object) -> Mapping[str, object]:
            return self._result("repository", {"revision": "a" * 40})

        def build(self, _profile: object) -> Mapping[str, object]:
            return self._result("build", {"build_sha256": "b" * 64})

        def binaries(self, _profile: object) -> Mapping[str, object]:
            return self._result("binaries", {"verified": True})

        def ports(self, _profile: object) -> Mapping[str, object]:
            return self._result("ports", {"available": True})

        def clock(self, _profile: object) -> Mapping[str, object]:
            return self._result("clock", {"monotonic": True})

        def native_topology(self, _profile: object) -> Mapping[str, object]:
            return self._result(
                "native_topology",
                {
                    "epoch_zero_digest": profile.raw["topology"]["epoch_zero_digest"],
                    "topology_proof_sha256": profile.topology_proof_sha256,
                },
            )

        def issuer_public_key(self, _profile: object) -> Mapping[str, object]:
            return self._result(
                "issuer_public_key",
                {"issuer_public_key": native_fixture.ISSUER_PUBLIC_KEY},
            )

    checks = Checks()
    preflight = _document(
        runtime.prepare_focused_preflight(
            profile,
            mode="smoke",
            pair_count=1,
            output_root=tmp_path / "results",
            checks=checks,
        )
    )
    expected_calls = [
        "repository",
        "build",
        "binaries",
        "ports",
        "clock",
        "native_topology",
        "issuer_public_key",
    ]
    assert calls == expected_calls
    context = preflight["execution_context"]
    assert context["issuer_public_key"] == native_fixture.ISSUER_PUBLIC_KEY
    assert context["profile_sha256"] == profile.profile_sha256
    assert context["topology_proof_sha256"] == profile.topology_proof_sha256

    for index, failing in enumerate(expected_calls, start=1):
        calls.clear()
        with pytest.raises(runtime.FocusedCrashPairRuntimeError, match=failing):
            runtime.prepare_focused_preflight(
                profile,
                mode="smoke",
                pair_count=1,
                output_root=tmp_path / f"rejected-{index}",
                checks=Checks(failing),
            )


@pytest.mark.parametrize(
    "mutation",
    ("revision", "build-digest", "binary-path", "binary-hash"),
)
def test_live_execution_context_binds_authorized_revision_build_and_binaries(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(N7_PROFILE)
    revision = "a" * 40
    binary_sha256 = "c" * 64
    build_record = {
        "schema_version": 1,
        "revision": revision,
        "targets": list(runtime.profiled_fault_runtime.EXACT_BUILD_TARGETS),
    }
    build_sha256 = hashlib.sha256(_canonical_json(build_record)).hexdigest()
    current_binary = Path("/usr/bin/true").resolve()
    binary_names = (
        "app",
        "client",
        "manager",
        "keygen",
        "tls_keygen",
        "epoch_profile_digest",
    )
    monkeypatch.setattr(
        runtime.profiled_fault_runtime,
        "exact_binary_paths",
        lambda *_args, **_kwargs: {name: current_binary for name in binary_names},
    )
    monkeypatch.setattr(
        runtime.profiled_fault_runtime,
        "verify_repository_state",
        lambda *_args, **_kwargs: revision,
    )
    monkeypatch.setattr(
        runtime.profiled_fault_runtime,
        "verify_exact_build_provenance",
        lambda **_kwargs: build_record,
    )
    monkeypatch.setattr(
        runtime.profiled_fault_runtime,
        "sha256_file",
        lambda _path: binary_sha256,
    )
    current_binary_paths = {name: current_binary for name in binary_names}
    current_binary_paths["client"] = (
        Path(runtime.__file__).resolve().parents[3]
        / "build-adaptive"
        / "examples"
        / "hotstuff-client"
    )
    execution_context = {
        "repository": {"revision": revision},
        "build": {"revision": revision, "build_sha256": build_sha256},
        "binaries": {
            "verified": True,
            "executables": {
                name: {
                    "path": str(current_binary_paths[name]),
                    "sha256": binary_sha256,
                }
                for name in binary_names
            },
        },
        "ports": {"available": True},
        "clock": {"monotonic": True},
        "native_topology": {
            "epoch_zero_digest": profile.raw["topology"]["epoch_zero_digest"],
            "topology_proof_sha256": profile.topology_proof_sha256,
        },
        "issuer_public_key": native_fixture.ISSUER_PUBLIC_KEY,
        "profile_sha256": profile.profile_sha256,
        "topology_proof_sha256": profile.topology_proof_sha256,
    }
    invocation = {
        "profile": profile,
        "preflight_receipt": {"execution_context": execution_context},
    }
    backend = runtime.FocusedLaunchBackend()
    bound = backend.bind_execution_context(invocation)
    assert bound["binaries"] == current_binary_paths

    changed = deepcopy(invocation)
    changed_context = changed["preflight_receipt"]["execution_context"]
    if mutation == "revision":
        changed_context["repository"]["revision"] = "0" * 40
    elif mutation == "build-digest":
        changed_context["build"]["build_sha256"] = "0" * 64
    elif mutation == "binary-path":
        changed_context["binaries"]["executables"]["app"]["path"] = "/usr/bin/false"
    else:
        changed_context["binaries"]["executables"]["app"]["sha256"] = "0" * 64
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        backend.bind_execution_context(changed)


def _fault_plan(targets: tuple[int, ...] = (22, 23, 24)) -> faults.FaultPlan:
    return faults.FaultPlan(
        context=faults.ScenarioContext(
            replica_ids=tuple(range(31)),
            quorum=21,
            crash_budget=10,
            successor_bundle_retry_limit=1,
        ),
        seed=41_719,
        actions=tuple(
            faults.ReplicaGroupSigkill(f"crash-replica-{replica}", replica)
            for replica in targets
        ),
    )


def _outcome(replica: int, timestamp: int) -> processes.SigkillOutcome:
    return processes.SigkillOutcome(
        fault_id=f"crash-replica-{replica}",
        name=f"replica-{replica}",
        replica_id=replica,
        pid=20_000 + replica,
        pgid=20_000 + replica,
        signal_number=9,
        returncode=-9,
        requested_monotonic_ns=timestamp,
        confirmed_monotonic_ns=timestamp + 1_000,
    )


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        assert timeout == 0.25
        if self.returncode is None:
            raise TimeoutError
        return self.returncode


def test_atomic_fault_batch_is_one_call_and_terminalizes_partial_failure(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    plan = _fault_plan()
    processes_by_pid = {
        20_000 + replica: _FakeProcess(20_000 + replica) for replica in (22, 23, 24)
    }
    calls: list[tuple[int, int]] = []

    def killpg(pgid: int, signal_number: int) -> None:
        calls.append((pgid, signal_number))
        processes_by_pid[pgid].returncode = -signal_number

    registry = processes.ProcessRegistry(
        getpgid=lambda pid: pid,
        killpg=killpg,
        get_launcher_pgid=lambda: 99_999,
        monotonic_ns=iter((100, 101, 102, 1_100, 1_101, 1_102)).__next__,
    )
    for replica in (22, 23, 24):
        registry.register(
            name=f"replica-{replica}",
            replica_id=replica,
            process=processes_by_pid[20_000 + replica],
        )
    journal_path = tmp_path / "success.jsonl"
    with faults.FaultJournal(
        journal_path,
        plan.sha256,
        monotonic_ns=itertools.count(2_000).__next__,
    ) as journal:
        lifecycle = faults.FaultLifecycle(plan, journal)
        result = runtime._execute_atomic_fault_batch(registry, plan, lifecycle, 0.25)
    assert calls == [(20_022, 9), (20_023, 9), (20_024, 9)]
    assert max(item.requested_monotonic_ns for item in result) < min(
        item.confirmed_monotonic_ns for item in result
    )
    journal_events = [
        json.loads(line) for line in journal_path.read_text().splitlines()
    ]
    assert [event["lifecycle"] for event in journal_events] == [
        "started",
        "started",
        "started",
        "terminal",
        "terminal",
        "terminal",
    ]
    for replica in (22, 23, 24):
        with pytest.raises(RuntimeError, match="already targeted"):
            registry.sigkill_replica_group(
                fault_id=f"retry-{replica}",
                replica_id=replica,
                timeout_s=0.25,
            )
    assert registry.cleanup(timeout_s=0.25) == ()

    partial = (
        processes.SigkillBatchResult(
            fault_id="crash-replica-22",
            replica_id=22,
            status="succeeded",
            outcome=_outcome(22, 100),
            error=None,
            name="replica-22",
            pid=20_022,
            pgid=20_022,
            signal_number=9,
            requested_monotonic_ns=100,
        ),
        *(
            processes.SigkillBatchResult(
                fault_id=f"crash-replica-{replica}",
                replica_id=replica,
                status="failed",
                outcome=None,
                error="confirmation failed",
                name=f"replica-{replica}",
                pid=20_000 + replica,
                pgid=20_000 + replica,
                signal_number=9,
                requested_monotonic_ns=101,
            )
            for replica in (23, 24)
        ),
    )

    class PartialRegistry:
        calls = 0

        def sigkill_replica_groups(self, *_args: object, **_kwargs: object) -> None:
            self.calls += 1
            raise processes.SigkillBatchError(partial)

    partial_registry = PartialRegistry()
    partial_path = tmp_path / "partial.jsonl"
    with faults.FaultJournal(
        partial_path,
        plan.sha256,
        monotonic_ns=itertools.count(3_000).__next__,
    ) as journal:
        partial_lifecycle = faults.FaultLifecycle(plan, journal)
        with pytest.raises(processes.SigkillBatchError):
            runtime._execute_atomic_fault_batch(
                partial_registry, plan, partial_lifecycle, 0.25
            )
    assert partial_registry.calls == 1
    partial_events = [
        json.loads(line) for line in partial_path.read_text().splitlines()
    ]
    terminals = [event for event in partial_events if event["lifecycle"] == "terminal"]
    assert [event["fault_id"] for event in terminals] == [
        action.fault_id for action in plan.actions
    ]
    assert [event["outcome"]["status"] for event in terminals] == [
        "succeeded",
        "failed",
        "failed",
    ]


def _membership_digest(replica_count: int) -> str:
    payload = b"kauri-membership-v1" + native_fixture._u(replica_count, 4)
    payload += b"".join(
        native_fixture._u(replica, 2) for replica in range(replica_count)
    )
    return hashlib.sha256(payload).hexdigest()


def _native_trees(
    *,
    replica_count: int,
    quorum: int,
    fanout: int,
    targets: Sequence[int],
    roots: Sequence[int],
) -> list[dict[str, object]]:
    assert len(roots) == quorum
    survivors = tuple(
        replica for replica in range(replica_count) if replica not in targets
    )
    trees: list[dict[str, object]] = []
    for tree_id, root in enumerate(roots):
        internal = tuple(replica for replica in survivors if replica != root)[:fanout]
        leaves = tuple(
            replica for replica in survivors if replica not in (root, *internal)
        )
        trees.append(
            {
                "tree_id": tree_id,
                "fanout": fanout,
                "pipeline_stretch": 2,
                "members": [root, *internal, *leaves, *targets],
                "wait_exempt": list(targets),
            }
        )
    return trees


def _encode_signed_bundle(
    *,
    replica_count: int,
    epoch_number: int,
    previous_digest: str,
    trees: Sequence[Mapping[str, object]],
    evidence_snapshot_id: str,
    evidence_cutoff: int,
    nonce: int,
) -> tuple[bytes, Any]:
    canonical = bytearray(b"kauri-epoch-definition-v2")
    canonical += native_fixture._u(2, 4)
    canonical += native_fixture._u(epoch_number, 4)
    canonical += bytes.fromhex(previous_digest)
    canonical += bytes.fromhex(_membership_digest(replica_count))
    canonical += native_fixture._u(native_fixture.NATIVE_SNAPSHOT_SEED, 8)
    canonical += native_fixture._string(native_fixture.NATIVE_PLACEMENT_POLICY)
    canonical += native_fixture._string(evidence_snapshot_id)
    canonical += native_fixture._u(evidence_cutoff, 8)
    canonical += native_fixture._u(len(trees), 4)
    for tree in trees:
        members = tuple(int(value) for value in tree["members"])  # type: ignore[arg-type]
        wait_exempt = tuple(
            int(value) for value in tree["wait_exempt"]  # type: ignore[arg-type]
        )
        canonical += native_fixture._u(int(tree["tree_id"]), 4)
        canonical += native_fixture._u(int(tree["fanout"]), 4)
        canonical += native_fixture._u(int(tree["pipeline_stretch"]), 4)
        canonical += native_fixture._u(len(members), 4)
        canonical += b"".join(native_fixture._u(member, 2) for member in members)
        canonical += native_fixture._u(len(wait_exempt), 4)
        canonical += b"".join(native_fixture._u(member, 2) for member in wait_exempt)
    successor_digest = hashlib.sha256(canonical).hexdigest()
    signing_bytes = b"".join(
        (
            b"kauri-authorized-epoch-change-v1",
            native_fixture._u(1, 4),
            native_fixture._u(2, 1),
            native_fixture._u(1, 4),
            native_fixture._u(epoch_number, 4),
            bytes.fromhex(previous_digest),
            bytes.fromhex(successor_digest),
            native_fixture._u(5, 8),
        )
    )
    command = signing_bytes + native_fixture._low_s_signature(
        signing_bytes,
        nonce=nonce,
    )
    definition = b"".join(
        (
            native_fixture._u(2, 4),
            native_fixture._u(2, 1),
            native_fixture._u(6, 1),
            bytes.fromhex(successor_digest),
            bytes(canonical)[len(b"kauri-epoch-definition-v2") :],
        )
    )
    wire = b"".join(
        (
            b"kauri-adaptive-v2-epoch-change-bundle-v1",
            native_fixture._u(1, 4),
            native_fixture._u(2, 1),
            native_fixture._component(command),
            native_fixture._component(definition),
        )
    )
    decoded = factorial_validation.decode_epoch_change_bundle(
        wire,
        issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
    )
    assert decoded.epoch_digest == successor_digest
    assert len(decoded.trees) == len(trees)
    return wire, decoded


def _native_arm_bundles(
    replicas: int,
) -> tuple[tuple[bytes, Any], tuple[bytes, Any], tuple[bytes, Any]]:
    if replicas == 31:
        control_wire, control_e1, adaptive_wire, adaptive_e1 = (
            native_fixture._independent_epoch1_bundles(verify_replay=True)
        )
        chain_e1_wire, chain_e1, epoch2_wire, epoch2 = (
            native_fixture._native_epoch_chain()
        )
        assert chain_e1_wire == adaptive_wire
        assert chain_e1 == adaptive_e1
        return (
            (control_wire, control_e1),
            (adaptive_wire, adaptive_e1),
            (epoch2_wire, epoch2),
        )

    assert replicas == 7
    targets = (0, 1)
    roots = (2, 3, 4, 5, 6)
    trees = _native_trees(
        replica_count=7,
        quorum=5,
        fanout=2,
        targets=targets,
        roots=roots,
    )
    predecessor = "a7" * 32
    control = _encode_signed_bundle(
        replica_count=7,
        epoch_number=1,
        previous_digest=predecessor,
        trees=trees,
        evidence_snapshot_id="17" * 32,
        evidence_cutoff=14,
        nonce=11,
    )
    adaptive = _encode_signed_bundle(
        replica_count=7,
        epoch_number=1,
        previous_digest=predecessor,
        trees=trees,
        evidence_snapshot_id="27" * 32,
        evidence_cutoff=21,
        nonce=12,
    )
    epoch2 = _encode_signed_bundle(
        replica_count=7,
        epoch_number=2,
        previous_digest=adaptive[1].epoch_digest,
        trees=trees,
        evidence_snapshot_id="37" * 32,
        evidence_cutoff=28,
        nonce=13,
    )
    return control, adaptive, epoch2


def _transition_snapshot(
    decoded: Any,
    wire: bytes,
    survivor_ids: Sequence[int],
    *,
    timestamp_ns: int,
    command_height: int,
) -> tuple[dict[str, object], dict[str, object]]:
    identity = {
        "successor_epoch_number": decoded.epoch_number,
        "successor_epoch_digest": decoded.epoch_digest,
        "bundle_sha256": hashlib.sha256(wire).hexdigest(),
        "survivor_replica_ids": list(survivor_ids),
        "witness_count": len(survivor_ids),
    }
    command = {
        **identity,
        "command_block_height": command_height,
        "activation_height": command_height + decoded.command.activation_delay_blocks,
        "source_monotonic_ns": timestamp_ns,
    }
    activation = {
        **identity,
        "activation_height": command["activation_height"],
        "source_monotonic_ns": timestamp_ns + 1_000,
    }
    return command, activation


def _arm_snapshots(replicas: int, arm: str) -> dict[str, Mapping[str, object]]:
    quorum = 5 if replicas == 7 else 21
    targets = (0, 1) if replicas == 7 else (22, 23, 24)
    survivors = tuple(replica for replica in range(replicas) if replica not in targets)
    control_e1, adaptive_e1, epoch2 = _native_arm_bundles(replicas)
    epoch1_wire, epoch1 = control_e1 if arm == "C" else adaptive_e1
    epoch2_wire, epoch2_decoded = epoch2
    commands1, activations1 = _transition_snapshot(
        epoch1,
        epoch1_wire,
        survivors,
        timestamp_ns=6_000,
        command_height=4,
    )
    commands2, activations2 = _transition_snapshot(
        epoch2_decoded,
        epoch2_wire,
        survivors,
        timestamp_ns=13_000,
        command_height=11,
    )
    epoch2_roots = [tree.members[0] for tree in epoch2_decoded.trees]
    ranked_ids = [
        *epoch2_roots,
        *(replica for replica in survivors if replica not in epoch2_roots),
    ]
    return {
        "baseline": {"stable": True, "source_monotonic_ns": 1_000},
        "fault": {
            "confirmed_target_ids": list(targets),
            "survivor_replica_ids": list(survivors),
            "source_monotonic_ns": 2_000,
        },
        "nonresponse": {
            "detected_target_ids": list(targets),
            "source_monotonic_ns": 3_000,
        },
        "epoch1": {
            "native_bundle": epoch1_wire,
            "decoded": asdict(epoch1),
            "source_monotonic_ns": 4_000,
        },
        "commands1": commands1,
        "activations1": activations1,
        "commit1": {
            "epoch_number": 1,
            "epoch_digest": epoch1.epoch_digest,
            "authoritative_commit_count": 1,
            "observed_replica_ids": list(survivors[:quorum]),
            "source_monotonic_ns": 10_000,
        },
        "containment": {
            "stable": True,
            "epoch_number": 1,
            "source_monotonic_ns": 11_000,
        },
        "ranking": {
            "predecessor_epoch_digest": epoch1.epoch_digest,
            "fresh_after_common_commit": True,
            "ranked_ids": ranked_ids,
            "selected_root_ids": epoch2_roots,
            "source_monotonic_ns": 12_000,
        },
        "epoch2": {
            "native_bundle": epoch2_wire,
            "decoded": asdict(epoch2_decoded),
            "source_monotonic_ns": 12_500,
        },
        "commands2": commands2,
        "activations2": activations2,
        "commit2": {
            "epoch_number": 2,
            "epoch_digest": epoch2_decoded.epoch_digest,
            "authoritative_commit_count": 1,
            "observed_replica_ids": list(survivors[:quorum]),
            "source_monotonic_ns": 16_000,
        },
        "late": {
            "stable": True,
            "held_epoch_number": 1 if arm == "C" else 2,
            "epoch2_present": arm == "A",
            "source_monotonic_ns": 17_000,
        },
        "unexpected": {"ids": []},
    }


def _arm_hooks(runtime: Any, snapshots: Mapping[str, Mapping[str, object]]) -> Any:
    required = {
        "wait_for_stable_phase",
        "inject_atomic_fault_batch",
        "wait_for_nonresponse",
        "issue_epoch_request",
        "wait_for_epoch_commands",
        "wait_for_epoch_activations",
        "wait_for_common_commit",
        "rebuild_ranking",
        "unexpected_exit_ids",
    }
    assert {field.name for field in fields(runtime.ArmRuntimeHooks)} == required
    calls: list[tuple[str, object]] = []

    def lookup(kind: str, name: str) -> Mapping[str, object]:
        calls.append((kind, name))
        return snapshots[name]

    hooks = runtime.ArmRuntimeHooks(
        wait_for_stable_phase=lambda name: lookup("stable", name),
        inject_atomic_fault_batch=lambda: lookup("fault", "fault"),
        wait_for_nonresponse=lambda: lookup("evidence", "nonresponse"),
        issue_epoch_request=lambda epoch: lookup("request", f"epoch{epoch}"),
        wait_for_epoch_commands=lambda epoch: lookup("commands", f"commands{epoch}"),
        wait_for_epoch_activations=lambda epoch: lookup(
            "activations", f"activations{epoch}"
        ),
        wait_for_common_commit=lambda epoch: lookup("commit", f"commit{epoch}"),
        rebuild_ranking=lambda: lookup("ranking", "ranking"),
        unexpected_exit_ids=lambda: snapshots.get("unexpected", {}).get("ids", []),
    )
    return hooks, calls


@pytest.mark.parametrize(("replicas", "quorum"), ((7, 5), (31, 21)))
def test_fake_process_control_and_adaptive_state_machines(
    replicas: int,
    quorum: int,
) -> None:
    runtime = _runtime()
    targets = (0, 1) if replicas == 7 else (22, 23, 24)
    survivors = tuple(replica for replica in range(replicas) if replica not in targets)
    profile = SimpleNamespace(
        replica_ids=tuple(range(replicas)),
        quorum=quorum,
        target_replica_ids=targets,
        issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
    )
    control_snapshots = _arm_snapshots(replicas, "C")
    adaptive_snapshots = _arm_snapshots(replicas, "A")
    control_hooks, control_calls = _arm_hooks(runtime, control_snapshots)
    control = _document(
        runtime._drive_arm_state_machine(profile, "C", "pair-01", control_hooks)
    )
    adaptive_hooks, adaptive_calls = _arm_hooks(runtime, adaptive_snapshots)
    adaptive = _document(
        runtime._drive_arm_state_machine(profile, "A", "pair-01", adaptive_hooks)
    )
    assert (
        control["survivor_replica_ids"]
        == adaptive["survivor_replica_ids"]
        == list(survivors)
    )
    assert control["quorum"] == adaptive["quorum"] == quorum
    assert control["epoch1_native_validated"] is True
    assert adaptive["epoch1_native_validated"] is True
    assert control["epoch2_present"] is False
    assert adaptive["epoch2_present"] is True
    assert adaptive["epoch2_predecessor_digest"] == adaptive["epoch1_epoch_digest"]
    assert not any(call == ("request", "epoch2") for call in control_calls)
    assert ("ranking", "ranking") in adaptive_calls
    assert control_calls == [
        ("stable", "baseline"),
        ("fault", "fault"),
        ("evidence", "nonresponse"),
        ("request", "epoch1"),
        ("commands", "commands1"),
        ("activations", "activations1"),
        ("commit", "commit1"),
        ("stable", "containment"),
        ("stable", "late"),
    ]
    assert adaptive_calls == [
        *control_calls[:-1],
        ("ranking", "ranking"),
        ("request", "epoch2"),
        ("commands", "commands2"),
        ("activations", "activations2"),
        ("commit", "commit2"),
        ("stable", "late"),
    ]

    if replicas == 31:
        control_decoded = factorial_validation.decode_epoch_change_bundle(
            control_snapshots["epoch1"]["native_bundle"],  # type: ignore[arg-type]
            issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
        )
        adaptive_decoded = factorial_validation.decode_epoch_change_bundle(
            adaptive_snapshots["epoch1"]["native_bundle"],  # type: ignore[arg-type]
            issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
        )
        assert control_decoded.epoch_digest != adaptive_decoded.epoch_digest
        assert control_decoded.command.signature != adaptive_decoded.command.signature
        assert (
            runtime.epoch1_structurally_identical(control_decoded, adaptive_decoded)
            is True
        )
        changed_trees = [asdict(tree) for tree in adaptive_decoded.trees]
        changed_members = list(changed_trees[0]["members"])  # type: ignore[arg-type]
        changed_members[1:3] = reversed(changed_members[1:3])
        changed_trees[0]["members"] = tuple(changed_members)
        _, changed = _encode_signed_bundle(
            replica_count=31,
            epoch_number=1,
            previous_digest=adaptive_decoded.previous_epoch_digest,
            trees=changed_trees,
            evidence_snapshot_id=adaptive_decoded.evidence_snapshot_id,
            evidence_cutoff=adaptive_decoded.evidence_cutoff,
            nonce=19,
        )
        assert runtime.epoch1_structurally_identical(control_decoded, changed) is False


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-survivor",
        "activation-identity",
        "early-ranking",
        "early-epoch2",
        "ranking-prefix",
        "control-epoch2",
        "unexpected-exit",
    ),
)
def test_state_machine_rejects_incomplete_or_misordered_runtime_graph(
    mutation: str,
) -> None:
    runtime = _runtime()
    profile = SimpleNamespace(
        replica_ids=tuple(range(31)),
        quorum=21,
        target_replica_ids=(22, 23, 24),
        issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
    )
    arm = "C" if mutation == "control-epoch2" else "A"
    snapshots = _arm_snapshots(31, arm)
    if mutation == "missing-survivor":
        snapshots["commands1"]["survivor_replica_ids"].pop()  # type: ignore[union-attr]
    elif mutation == "activation-identity":
        snapshots["activations1"]["successor_epoch_digest"] = "0" * 64  # type: ignore[index]
    elif mutation == "early-ranking":
        snapshots["ranking"]["source_monotonic_ns"] = 9_999  # type: ignore[index]
    elif mutation == "early-epoch2":
        snapshots["commands2"]["source_monotonic_ns"] = 11_999  # type: ignore[index]
    elif mutation == "ranking-prefix":
        ranking = snapshots["ranking"]
        ranked_ids = list(ranking["ranked_ids"])  # type: ignore[arg-type]
        ranked_ids[0], ranked_ids[-1] = ranked_ids[-1], ranked_ids[0]
        ranking["ranked_ids"] = ranked_ids  # type: ignore[index]
    elif mutation == "control-epoch2":
        snapshots["late"]["epoch2_present"] = True  # type: ignore[index]
    else:
        snapshots["unexpected"] = {"ids": [30]}
    hooks, _ = _arm_hooks(runtime, snapshots)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        runtime._drive_arm_state_machine(profile, arm, "pair-01", hooks)


class _DelayedPollingSource:
    def __init__(
        self,
        snapshots: Mapping[str, Mapping[str, object]],
        trace: list[str],
    ) -> None:
        self.snapshots = snapshots
        self.trace = trace
        self.polls: dict[str, int] = {}

    def poll(self, name: str) -> Mapping[str, object] | None:
        count = self.polls.get(name, 0) + 1
        self.polls[name] = count
        self.trace.append(f"poll:{name}:{count}")
        return None if count == 1 else self.snapshots[name]


def test_default_backend_materializes_pair_issuer_without_secret_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(N7_PROFILE)
    bls = [
        {"pub": f"bls-pub-{replica}", "sec": f"bls-sec-{replica}"}
        for replica in profile.replica_ids
    ]
    tls = [
        {
            "crt": f"tls-crt-{identity}",
            "sec": f"tls-sec-{identity}",
            "cid": f"tls-cid-{identity}",
        }
        for identity in range(len(profile.replica_ids) + 1)
    ]

    def generate_identities(
        _profile: object,
        *,
        keygen_binary: Path,
        tls_keygen_binary: Path,
        config_directory: Path,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        assert keygen_binary.name == "hotstuff-keygen"
        assert tls_keygen_binary.name == "hotstuff-tls-keygen"
        (config_directory / "bls-identities.txt").write_text(
            "synthetic BLS identities\n", encoding="utf-8"
        )
        (config_directory / "tls-identities.txt").write_text(
            "synthetic TLS identities\n", encoding="utf-8"
        )
        return bls, tls

    monkeypatch.setattr(runtime, "_generate_arm_identities", generate_identities)
    build_directory = tmp_path / "build"
    build_directory.mkdir()
    (
        build_directory / runtime.profiled_fault_runtime.BUILD_PROVENANCE_FILENAME
    ).write_text(
        json.dumps({"schema_version": 1, "revision": "a" * 40}),
        encoding="utf-8",
    )
    public_key = native_fixture.ISSUER_PUBLIC_KEY
    private_key = f"{1:064x}"
    context = {
        "profile": profile,
        "pair_seed": 41_720,
        "output_root": tmp_path / "results",
        "build_directory": build_directory,
        "binaries": {
            "app": Path("/build/hotstuff-app"),
            "manager": Path("/build/adaptation-manager"),
            "client": Path("/build/hotstuff-client"),
            "keygen": Path("/build/hotstuff-keygen"),
            "tls_keygen": Path("/build/hotstuff-tls-keygen"),
        },
        "pair_issuer_allocations": {
            "pair-01": {
                "public_key": public_key,
                "control": {"public_key": public_key, "private_key": private_key},
                "adaptive": {"public_key": public_key, "private_key": private_key},
            }
        },
        "preflight_receipt": {},
        "authorization_receipt": {
            "approval_reference": "test-only",
            "approved_utc": "2026-08-17T00:00:00Z",
        },
    }

    configuration = runtime.FocusedLaunchBackend().materialize_arm_configuration(
        context,
        pair_ordinal=1,
        arm="control",
    )

    run_directory = Path(configuration["run_directory"])
    assert not (run_directory / "config/issuer-identities.txt").exists()
    assert all(
        artifact["kind"] != "issuer_identity_input"
        for artifact in configuration["runtime_artifacts"]
    )
    assert private_key not in (
        run_directory / "runtime/launch-arguments.json"
    ).read_text(encoding="utf-8")
    assert (run_directory / "treegen.conf").read_text(
        encoding="ascii"
    ).splitlines() == [
        "fan:2 pipe:2 "
        + " ".join(str(replica) for replica in (*range(offset, 7), *range(offset)))
        for offset in range(7)
    ]


@pytest.mark.parametrize("arm", ("C", "A"))
def test_default_backend_run_arm_polls_and_faults_only_at_state_machine_boundary(
    arm: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    snapshots = _arm_snapshots(31, arm)
    trace: list[str] = []
    source = _DelayedPollingSource(snapshots, trace)
    profile = SimpleNamespace(
        replica_ids=tuple(range(31)),
        quorum=21,
        target_replica_ids=(22, 23, 24),
        issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
    )
    run_directory = tmp_path / arm
    run_directory.mkdir()
    configuration = {
        "profile": profile,
        "pair_id": "pair-01",
        "arm": "control" if arm == "C" else "adaptive",
        "run_directory": run_directory,
    }
    fake_processes = SimpleNamespace(records=())
    fault_calls: list[str] = []

    def atomic_fault(
        received_configuration: Mapping[str, object],
        received_processes: object,
    ) -> Mapping[str, object]:
        assert received_configuration is configuration
        assert received_processes is fake_processes
        fault_calls.append("fault")
        trace.append("fault")
        outcome = snapshots["fault"]
        assert "source_monotonic_ns" in outcome
        assert tuple(outcome["survivor_replica_ids"]) == tuple(
            replica for replica in range(31) if replica not in (22, 23, 24)
        )
        return outcome

    backend = runtime.FocusedLaunchBackend(
        poll_snapshot=source.poll,
        poll_interval_s=0,
        readiness_timeout_s=1,
        execute_fault=atomic_fault,
    )
    original_driver = runtime._drive_arm_state_machine
    monkeypatch.setattr(
        runtime,
        "_drive_arm_state_machine",
        lambda state_profile, state_arm, pair_id, hooks: (
            trace.append("state-machine"),
            original_driver(
                state_profile,
                state_arm,
                pair_id,
                hooks,
            ),
        )[1],
    )
    outcome = _document(backend.run_arm(configuration, fake_processes))
    assert fault_calls == ["fault"]
    assert trace.index("state-machine") < trace.index("fault")
    expected = [
        "readiness",
        "baseline",
        "fault",
        "nonresponse",
        "epoch1",
        "commands1",
        "activations1",
        "commit1",
        "containment",
    ]
    if arm == "A":
        expected.extend(("ranking", "epoch2", "commands2", "activations2", "commit2"))
    expected.append("late")
    for name in expected:
        assert source.polls[name] >= 2
    assert outcome["runtime_graph"] == "complete"
    trace.append("cleanup")
    backend.materialize_artifacts(configuration, outcome, {"complete": True})
    seal = backend.seal(configuration, outcome, {"complete": True})
    assert trace[-1] == "cleanup"
    assert seal["tree_sha256"]


def test_default_backend_registry_cleanup_precedes_materialization_and_seal(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    trace: list[str] = []

    class Registry:
        def cleanup(self, *, timeout_s: float) -> tuple[object, ...]:
            assert timeout_s > 0
            trace.append("registry-cleanup")
            return ()

    def materialize(
        _configuration: Mapping[str, object],
        _outcome: Mapping[str, object],
        cleanup: Mapping[str, object],
    ) -> None:
        assert cleanup["complete"] is True
        assert trace == ["registry-cleanup"]
        trace.append("materialize")

    def seal_artifacts(
        root: Path,
        _required: Sequence[str],
        _outcome: Mapping[str, object],
        cleanup: Mapping[str, object],
    ) -> Mapping[str, object]:
        assert cleanup["complete"] is True
        assert trace == ["registry-cleanup", "materialize"]
        trace.append("seal")
        return {"tree_sha256": "a" * 64, "seal_sha256": "b" * 64}

    backend = runtime.FocusedLaunchBackend(
        cleanup_registry=lambda processes: processes.registry.cleanup(timeout_s=2.0),
        materialize_artifacts=materialize,
        seal_artifacts=seal_artifacts,
    )
    processes = SimpleNamespace(registry=Registry(), records=(), logs=())
    configuration = {"run_directory": tmp_path}
    outcome = {"runtime_graph": "complete"}
    cleanup = backend.cleanup(configuration, processes)
    backend.materialize_artifacts(configuration, outcome, cleanup)
    backend.seal(configuration, outcome, cleanup)
    assert trace == ["registry-cleanup", "materialize", "seal"]


def test_default_backend_rejects_one_shot_artifact_existence_without_polling(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    run_directory = tmp_path / "arm"
    run_directory.mkdir()
    (run_directory / "raw").mkdir()
    (run_directory / "raw" / "epoch1.bundle").write_bytes(b"exists-once")
    backend = runtime.FocusedLaunchBackend(
        poll_snapshot=lambda _name: None,
        poll_interval_s=0,
        readiness_timeout_s=0,
    )
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        backend.run_arm(
            {
                "profile": SimpleNamespace(
                    replica_ids=tuple(range(31)),
                    quorum=21,
                    target_replica_ids=(22, 23, 24),
                    issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
                ),
                "pair_id": "pair-01",
                "arm": "control",
                "run_directory": run_directory,
            },
            SimpleNamespace(records=()),
        )


def test_manager_actual_argv_and_input_are_blind(tmp_path: Path) -> None:
    runtime = _runtime()
    proc_root = tmp_path / "proc"
    cmdline = proc_root / "123" / "cmdline"
    cmdline.parent.mkdir(parents=True)
    requested, manager_input = native_fixture._safe_manager_boundary()
    assert (
        _document(
            native_fixture._subject().validate_manager_blinding(
                fault_plan=native_fixture._fault_evidence()[0],
                manager_cli_args=requested,
                manager_input=manager_input,
            )
        )["blinded"]
        is True
    )
    cmdline.write_bytes(b"\0".join(item.encode() for item in requested) + b"\0")
    record = SimpleNamespace(name="manager", replica_id=-1, pid=123, pgid=123)
    linux_observed = runtime._capture_process_argv(
        record,
        proc_root,
        platform_system="Linux",
    )
    assert tuple(linux_observed) == requested
    darwin_calls: list[int] = []

    def darwin_reader(pid: int) -> Sequence[str]:
        darwin_calls.append(pid)
        return requested

    darwin_observed = runtime._capture_process_argv(
        record,
        proc_root,
        platform_system="Darwin",
        darwin_reader=darwin_reader,
    )
    assert tuple(darwin_observed) == requested
    assert darwin_calls == [123]
    proof = _document(
        runtime._validate_manager_launch_boundary(
            requested,
            linux_observed,
            manager_input=manager_input,
            forbidden_values=("crash-replica-22", "pgid-20022", "fast-tier"),
        )
    )
    assert proof["blinded"] is True

    mutations = (
        (requested, (*linux_observed, "--crash-replica-22")),
        (requested, linux_observed[:-1]),
    )
    for expected, actual in mutations:
        with pytest.raises(runtime.FocusedCrashPairRuntimeError):
            runtime._validate_manager_launch_boundary(
                expected,
                actual,
                manager_input=manager_input,
                forbidden_values=("crash-replica-22", "pgid-20022", "fast-tier"),
            )
    cmdline.write_bytes(b"manager\0--broken")
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        runtime._capture_process_argv(record, proc_root, platform_system="Linux")
    cmdline.unlink()
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        runtime._capture_process_argv(record, proc_root, platform_system="Linux")


def test_spawn_boundary_allows_run_identity_but_not_fault_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pair/arm labels are routing identity, not hidden fault-plan truth."""

    runtime = _runtime()
    requested, _manager_input = native_fixture._safe_manager_boundary()
    (tmp_path / "runtime").mkdir()
    requested = tuple(
        "pair-01-control" if value == native_fixture.RUN_ID else value
        for value in requested
    )
    cleanup_calls: list[float] = []

    class Registry:
        def cleanup(self, *, timeout_s: float) -> tuple[object, ...]:
            cleanup_calls.append(timeout_s)
            return ()

    class Evidence:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    class Log:
        def close(self) -> None:
            return None

    registry = Registry()
    monkeypatch.setattr(runtime, "ProcessRegistry", lambda **_kwargs: registry)
    monkeypatch.setattr(runtime, "FaultEvidence", lambda *_args, **_kwargs: Evidence())
    monkeypatch.setattr(runtime, "_capture_process_argv", lambda _record: requested)

    def spawn(
        _registry: object,
        *,
        name: str,
        replica_id: int,
        command: Sequence[str],
        **_kwargs: object,
    ) -> tuple[object, object]:
        assert tuple(command) == requested if name == "adaptive-manager" else True
        return SimpleNamespace(name=name, replica_id=replica_id, pid=10, pgid=10), Log()

    backend = runtime.FocusedLaunchBackend(spawn=spawn)
    processes = backend.spawn_processes(
        {
            "run_directory": tmp_path,
            "profile": SimpleNamespace(replica_ids=(), target_replica_ids=(22,)),
            "fault_plan": object(),
            "manager_command": requested,
            "replica_commands": (),
            "client_command": ("client",),
            "pair_id": "pair-01",
            "arm": "control",
        }
    )

    assert len(processes.records) == 2
    assert cleanup_calls == []


def test_spawn_boundary_failure_cleans_up_every_owned_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    requested, _manager_input = native_fixture._safe_manager_boundary()
    cleanup_calls: list[float] = []
    closed_logs: list[str] = []
    exited: list[tuple[object, object, object]] = []

    class Registry:
        def cleanup(self, *, timeout_s: float) -> tuple[object, ...]:
            cleanup_calls.append(timeout_s)
            return ()

    class Evidence:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            exited.append(args)

    class Log:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed_logs.append(self.name)

    registry = Registry()
    monkeypatch.setattr(runtime, "ProcessRegistry", lambda **_kwargs: registry)
    monkeypatch.setattr(runtime, "FaultEvidence", lambda *_args, **_kwargs: Evidence())
    monkeypatch.setattr(runtime, "_capture_process_argv", lambda _record: ("wrong",))

    def spawn(
        _registry: object,
        *,
        name: str,
        replica_id: int,
        **_kwargs: object,
    ) -> tuple[object, object]:
        return SimpleNamespace(name=name, replica_id=replica_id, pid=10, pgid=10), Log(
            name
        )

    backend = runtime.FocusedLaunchBackend(spawn=spawn)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="argv differ"):
        backend.spawn_processes(
            {
                "run_directory": tmp_path,
                "profile": SimpleNamespace(replica_ids=(), target_replica_ids=(22,)),
                "fault_plan": object(),
                "manager_command": requested,
                "replica_commands": (),
                "client_command": ("client",),
                "pair_id": "pair-01",
                "arm": "control",
            }
        )

    assert cleanup_calls == [2.0]
    assert closed_logs == ["adaptive-manager", "workload-client"]
    assert len(exited) == 1
    assert exited[0][0] is runtime.FocusedCrashPairRuntimeError


@pytest.mark.parametrize("arm", ("C", "A"))
def test_arm_artifact_layout_is_complete_and_seal_is_final_write(
    arm: str,
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    run_directory = tmp_path / arm
    required = [
        "profile.json",
        "topology-proof.json",
        "preflight.json",
        "authorization.json",
        "pair-receipt.json",
        "fault-plan.json",
        "manifest.json",
        "runner-outcome.json",
        "runtime/build-provenance.json",
        "runtime/effective-runtime.json",
        "runtime/launch-arguments.json",
        "runtime/manager-observed-argv.json",
        "runtime/manager-input.json",
        "runtime/source-inventory.json",
        "raw/fault-receipt.json",
        "raw/replica-events.jsonl",
        "raw/adaptive-manager-events.jsonl",
        "raw/client-events.jsonl",
        "raw/epoch1.bundle",
        "raw/issuer-public-key.txt",
        "derived/phase-windows.json",
        "derived/throughput.json",
        "cleanup.json",
    ]
    if arm == "A":
        required.append("raw/epoch2.bundle")
    control_e1, adaptive_e1, epoch2 = _native_arm_bundles(31)
    epoch1_wire = control_e1[0] if arm == "C" else adaptive_e1[0]
    for relative in required:
        path = run_directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "raw/epoch1.bundle":
            path.write_bytes(epoch1_wire)
        elif relative == "raw/epoch2.bundle":
            path.write_bytes(epoch2[0])
        elif relative == "raw/issuer-public-key.txt":
            path.write_text(native_fixture.ISSUER_PUBLIC_KEY + "\n", encoding="utf-8")
        else:
            path.write_bytes(b"{}\n")
    snapshots: list[set[str]] = []

    def create_seal(path: Path) -> object:
        snapshots.append(
            {str(item.relative_to(path)) for item in path.rglob("*") if item.is_file()}
        )
        return profiled_fault_archive.create_evidence_seal(path)

    seal = _document(
        runtime._seal_arm_artifacts(
            run_directory,
            tuple(required),
            {"verdict": "PASS"},
            {"complete": True},
            create_seal=create_seal,
        )
    )
    assert snapshots == [set(required)]
    verified = profiled_fault_archive.verify_evidence_seal(run_directory)
    assert seal["tree_sha256"] == verified.tree_sha256
    assert seal["seal_sha256"] == verified.seal_sha256
    assert (run_directory / "evidence-seal.json").is_file()
    assert sorted(
        str(path.relative_to(run_directory))
        for path in run_directory.rglob("*")
        if path.is_file()
    ) == sorted([*required, "evidence-seal.json"])
    assert (run_directory / "raw/epoch2.bundle").exists() is (arm == "A")

    (run_directory / "raw/replica-events.jsonl").write_bytes(b"mutated\n")
    with pytest.raises(profiled_fault_archive.EvidenceSealError):
        profiled_fault_archive.verify_evidence_seal(run_directory)
