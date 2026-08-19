"""Red-first contracts for the focused N7/N31 crash-pair runnable layer."""

from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
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
N31_PROFILE_V4 = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v4.json"
N7_PROFILE_V5 = PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v5.json"
N31_PROFILE_V5 = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v5.json"
N7_PROFILE_V6 = PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v6.json"
N31_PROFILE_V6 = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v6.json"
N7_PROFILE_V7 = PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v7.json"
N31_PROFILE_V7 = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v7.json"
N7_PROFILE_V8 = PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v8.json"
N31_PROFILE_V8 = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v8.json"
N7_PROFILE_V9 = PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v9.json"
N31_PROFILE_V9 = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v9.json"


def test_v7_profiles_bind_arm_v3_and_independently_recomputed_n31_metric() -> None:
    runtime = _runtime()
    validator = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_validation"
    )
    n7 = runtime.load_focused_profile(N7_PROFILE_V7)
    n31 = runtime.load_focused_profile(N31_PROFILE_V7)
    assert n7.raw["fault_window_arm"] == {
        **n7.raw["fault_window_arm"],
        "schema_version": 3,
        "required_observation_schema": 3,
        "timeout_evidence_basis": "exact_timeout_attempt_id_v1",
        "clock_domain": "same_host_clock_monotonic_raw",
        "snapshot_evidence_basis": "exact_post_fault_attempt_start_v1",
    }
    metric = runtime._v7_n31_target_selection_metric()
    assert metric == validator._v7_n31_target_selection_metric()
    assert n31.raw["topology"]["target_selection_metric"] == metric
    assert metric["selected_target_replica_ids"] == [21, 22, 23]
    for key in ("bfs_member_order", "fanout", "prefix_tree_ids"):
        mutated = deepcopy(metric)
        mutated[key] = [] if key != "fanout" else 4
        assert mutated != runtime._v7_n31_target_selection_metric()
    for row in metric["triple_scores"]:
        mutated = deepcopy(metric)
        mutated["triple_scores"][0]["total_survivor_path_shadow"] += 1
        assert mutated != runtime._v7_n31_target_selection_metric()
    mutated = deepcopy(metric)
    mutated["selected_target_replica_ids"] = [21, 22, 24]
    assert mutated != runtime._v7_n31_target_selection_metric()


def test_v8_profiles_bind_independent_capacity_and_metric() -> None:
    runtime = _runtime()
    validator = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_validation"
    )
    n7 = runtime.load_focused_profile(N7_PROFILE_V8)
    n31 = runtime.load_focused_profile(N31_PROFILE_V8)
    assert (
        runtime._v8_n31_target_selection_metric()
        == validator._v8_n31_target_selection_metric()
    )
    for profile in (n7, n31):
        capacity = runtime._reporter_capacity_document(
            replica_count=len(profile.replica_ids),
            fanout=int(profile.raw["protocol"]["fanout"]),
            targets=profile.target_replica_ids,
            prefix=profile.raw["fault_window_arm"]["ordered_tree_prefix"],
        )
        assert capacity == validator._v8_reporter_capacity_document(
            replica_count=len(profile.replica_ids),
            fanout=int(profile.raw["protocol"]["fanout"]),
            targets=profile.target_replica_ids,
            prefix=profile.raw["fault_window_arm"]["ordered_tree_prefix"],
        )
        assert profile.raw["topology"]["reporter_coverage_capacity"] == capacity
    assert (
        n31.raw["topology"]["target_selection_metric"]
        == runtime._v8_n31_target_selection_metric()
    )


def test_v7_runtime_loader_rejects_mutated_metric_recomputation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    mutated = deepcopy(runtime._v7_n31_target_selection_metric())
    mutated["triple_scores"][0]["total_survivor_path_shadow"] += 1
    monkeypatch.setattr(runtime, "_v7_n31_target_selection_metric", lambda: mutated)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        runtime.load_focused_profile(N31_PROFILE_V7)


def test_v6_replica_configs_enable_exact_timeout_attempt_evidence(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    config = tmp_path / "config"
    config.mkdir()
    for replica in (0, 1):
        (config / f"replica-{replica}.conf").write_text("idx = 0\n", encoding="ascii")

    runtime._enable_v6_timeout_attempt_evidence(tmp_path, (0, 1))

    for replica in (0, 1):
        assert (config / f"replica-{replica}.conf").read_text(encoding="ascii") == (
            "idx = 0\nexperiment-exact-timeout-attempt-evidence-v3 = true\n"
        )


@pytest.mark.parametrize("profile_path", (N31_PROFILE_V4, N31_PROFILE_V5))
def test_archived_focused_ranking_rejects_schema2_replay(
    profile_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Focused archive replay is schema-1 only even though generic replay has v2."""

    runtime = _runtime()
    profile = runtime.load_focused_profile(profile_path)
    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._profile = profile
    epoch1 = SimpleNamespace(epoch_digest="11" * 32, generation_seed=1)
    source._bundle = lambda _epoch: (b"", epoch1)
    audit = {
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": profile.raw["topology"]["epoch_zero_digest"],
        "baseline_cutoff": 0,
        "current_cutoff": 1,
    }
    poisoned_observation = {"schema_version": 2}
    events = [
        {
            "source_kind": "adaptation_manager",
            "source_id": "adaptive-manager",
            "event_type": "adaptive_v2_evidence_snapshot",
            "payload": audit,
        },
        {
            "source_kind": "adaptation_manager",
            "source_id": "adaptive-manager",
            "event_type": "evidence.observation_accepted",
            "payload": {"observation": poisoned_observation},
        },
    ]

    def reject_schema2(_events: object, **kwargs: object) -> object:
        assert kwargs["allowed_schema_versions"] == frozenset({1})
        assert poisoned_observation["schema_version"] == 2
        raise runtime.factorial_validation.FactorialValidationError("schema2 poison")

    monkeypatch.setattr(
        runtime.factorial_validation,
        "replay_native_adaptation_snapshot",
        reject_schema2,
    )
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="ranking replay"):
        source._ranking(events, predecessor_epoch=0)


def _v6_runtime_timeout_events(profile: Any) -> list[dict[str, object]]:
    runtime = _runtime()
    coverage = runtime.derive_reporter_coverage_plan(profile)
    digest = str(profile.raw["topology"]["epoch_zero_digest"])
    events: list[dict[str, object]] = []

    def append(
        *,
        target: int,
        reporter: int,
        tree: int,
        message_type: str,
        outcome: str,
        start: int,
        ordinal: int,
    ) -> None:
        deadline = 1
        block_hash = hashlib.sha256(
            f"v6-runtime-{target}-{reporter}-{tree}-{ordinal}".encode()
        ).hexdigest()
        reporter_ns = start + 1_000 if outcome != "on_time" else start
        response_us = 0
        observation = {
            "schema_version": 3,
            "reporter_id": reporter,
            "observed_replica_id": target,
            "configuration": {
                "epoch_number": 0,
                "tree_id": tree,
                "epoch_digest": digest,
            },
            "block_hash": block_hash,
            "expected_message_type": message_type,
            "outcome": outcome,
            "response_duration_us": response_us,
            "deadline_duration_us": deadline,
            "reporter_monotonic_ns": reporter_ns,
            "reporter_sequence": ordinal + 1,
            "attempt_start_monotonic_ns": start,
            "reporter_local_commit_monotonic_ns": 0,
            "signer_set": [] if outcome == "timeout" else [reporter],
        }
        observation["observation_id"] = runtime._v6_timeout_observation_id(
            reporter_id=reporter,
            observed_replica_id=target,
            epoch_number=0,
            tree_id=tree,
            epoch_digest=digest,
            block_hash=block_hash,
            expected_message_type=message_type,
            attempt_start_monotonic_ns=start,
            deadline_duration_us=deadline,
        )
        sequence = len(events) + 1
        events.append(
            {
                "source_kind": "adaptation_manager",
                "source_id": "adaptive-manager",
                "source_sequence": sequence,
                "source_monotonic_ns": 50_000 + sequence,
                "event_type": "evidence.observation_accepted",
                "payload": {"ingestion_sequence": sequence, "observation": observation},
            }
        )

    # A valid pre-R timeout and on-time fact compensate in the raw suffix but
    # cannot enter the exact post-arm reporter guard.
    first = (
        coverage["targets"][0]["first_qualifying_reporters"][0]
        if "first_qualifying_reporters" in coverage["targets"][0]
        else {
            "reporter_id": coverage["targets"][0]["eligible_reporters"][0][
                "reporter_id"
            ],
            "tree_id": coverage["targets"][0]["eligible_reporters"][0][
                "tree_relations"
            ][0]["tree_id"],
            "expected_message_type": coverage["targets"][0]["eligible_reporters"][0][
                "tree_relations"
            ][0]["expected_message_type"],
        }
    )
    append(
        target=int(coverage["targets"][0]["target_replica_id"]),
        reporter=int(first["reporter_id"]),
        tree=int(first["tree_id"]),
        message_type=(
            str(
                first.get(
                    "expected_message_type",
                    (
                        "aggregate_relay"
                        if int(first["reporter_id"]) == 6
                        else "direct_vote"
                    ),
                )
            )
        ),
        outcome="timeout",
        start=9_000,
        ordinal=99,
    )
    append(
        target=int(coverage["targets"][0]["target_replica_id"]),
        reporter=int(first["reporter_id"]),
        tree=int(first["tree_id"]),
        message_type=(
            str(
                first.get(
                    "expected_message_type",
                    (
                        "aggregate_relay"
                        if int(first["reporter_id"]) == 6
                        else "direct_vote"
                    ),
                )
            )
        ),
        outcome="on_time",
        start=9_100,
        ordinal=100,
    )
    ordinal = 0
    for row in coverage["targets"]:
        reporter_rows = (
            row["first_qualifying_reporters"]
            if "first_qualifying_reporters" in row
            else [
                {
                    "reporter_id": reporter["reporter_id"],
                    "tree_id": reporter["tree_relations"][0]["tree_id"],
                    "expected_message_type": reporter["tree_relations"][0][
                        "expected_message_type"
                    ],
                }
                for reporter in row["eligible_reporters"]
            ]
        )
        for reporter_row in reporter_rows:
            for _ in range(2):
                ordinal += 1
                reporter = int(reporter_row["reporter_id"])
                append(
                    target=int(row["target_replica_id"]),
                    reporter=reporter,
                    tree=int(reporter_row["tree_id"]),
                    message_type=(
                        str(
                            reporter_row.get(
                                "expected_message_type",
                                "aggregate_relay" if reporter == 6 else "direct_vote",
                            )
                        )
                    ),
                    outcome="timeout",
                    start=10_000 + ordinal,
                    ordinal=ordinal,
                )
    return events


def test_v6_runtime_exact_timeout_guard_replays_mixed_native_attempts() -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(N7_PROFILE_V6)
    backend = object.__new__(runtime.FocusedRawEvidenceSource)
    backend._profile = profile
    events = _v6_runtime_timeout_events(profile)
    result = backend._qualifying_timeout_counts(
        events, fault_ns=10_000, baseline_cutoff=0, current_cutoff=len(events)
    )
    assert result is not None
    counts, raw_drawdowns, _timestamp = result
    assert counts == {
        "0": {"4": 2, "5": 2, "6": 2},
        "1": {"4": 2, "5": 2, "6": 2},
    }
    assert raw_drawdowns == {"0": -6, "1": -6}


def test_v7_runtime_raw_drawdown_excludes_pre_arm_timeout() -> None:
    runtime = _runtime()
    v6 = runtime.load_focused_profile(N7_PROFILE_V6)
    v7 = runtime.load_focused_profile(N7_PROFILE_V7)
    events = _v6_runtime_timeout_events(v6)
    # Keep the pre-R timeout but remove its separate on-time compensation.
    events = [
        event
        for event in events
        if event["payload"]["observation"]["attempt_start_monotonic_ns"] != 9_100
    ]
    legacy = object.__new__(runtime.FocusedRawEvidenceSource)
    legacy._profile = v6
    causal = object.__new__(runtime.FocusedRawEvidenceSource)
    causal._profile = v7
    cutoff = max(event["payload"]["ingestion_sequence"] for event in events)
    legacy_result = legacy._qualifying_timeout_counts(
        events, fault_ns=10_000, baseline_cutoff=0, current_cutoff=cutoff
    )
    causal_result = causal._qualifying_timeout_counts(
        events, fault_ns=10_000, baseline_cutoff=0, current_cutoff=cutoff
    )
    assert legacy_result is not None and causal_result is not None
    assert legacy_result[1] == {"0": -7, "1": -6}
    assert causal_result[1] == {"0": -6, "1": -6}


def test_v8_runtime_requires_any_eleven_topology_valid_reporters() -> None:
    """The v8 guard admits any 11 proof-bound reporters, not a first tree."""

    runtime = _runtime()
    profile = runtime.load_focused_profile(N31_PROFILE_V8)
    backend = object.__new__(runtime.FocusedRawEvidenceSource)
    backend._profile = profile
    events = _v6_runtime_timeout_events(profile)
    # Reporter 0 has two valid target-21 relations.  The alternate tree 27
    # must count in v8 even though the fixture starts on tree 26.
    for event in events:
        observation = event["payload"]["observation"]
        if observation["reporter_id"] != 0 or observation["observed_replica_id"] != 21:
            continue
        observation["configuration"]["tree_id"] = 27
        observation["observation_id"] = runtime._v6_timeout_observation_id(
            reporter_id=0,
            observed_replica_id=21,
            epoch_number=0,
            tree_id=27,
            epoch_digest=str(profile.raw["topology"]["epoch_zero_digest"]),
            block_hash=str(observation["block_hash"]),
            expected_message_type="direct_vote",
            attempt_start_monotonic_ns=int(observation["attempt_start_monotonic_ns"]),
            deadline_duration_us=1,
        )
    eleven = [
        event
        for event in events
        if event["payload"]["observation"]["reporter_id"] != 30
    ]
    cutoff = max(event["payload"]["ingestion_sequence"] for event in eleven)
    assert (
        backend._qualifying_timeout_counts(
            eleven, fault_ns=10_000, baseline_cutoff=0, current_cutoff=cutoff
        )
        is not None
    )
    ten = [
        event
        for event in eleven
        if event["payload"]["observation"]["reporter_id"] != 29
    ]
    cutoff = max(event["payload"]["ingestion_sequence"] for event in ten)
    assert (
        backend._qualifying_timeout_counts(
            ten, fault_ns=10_000, baseline_cutoff=0, current_cutoff=cutoff
        )
        is None
    )


def test_v7_runtime_preserves_first_tree_requirement() -> None:
    """A relation only v8 admits cannot satisfy the archived v7 guard."""

    runtime = _runtime()
    profile = runtime.load_focused_profile(N7_PROFILE_V7)
    backend = object.__new__(runtime.FocusedRawEvidenceSource)
    backend._profile = profile
    events = _v6_runtime_timeout_events(profile)
    for event in events:
        observation = event["payload"]["observation"]
        if observation["reporter_id"] != 4 or observation["observed_replica_id"] != 0:
            continue
        observation["configuration"]["tree_id"] = 3
        observation["observation_id"] = runtime._v6_timeout_observation_id(
            reporter_id=4,
            observed_replica_id=0,
            epoch_number=0,
            tree_id=3,
            epoch_digest=str(profile.raw["topology"]["epoch_zero_digest"]),
            block_hash=str(observation["block_hash"]),
            expected_message_type="direct_vote",
            attempt_start_monotonic_ns=int(observation["attempt_start_monotonic_ns"]),
            deadline_duration_us=1,
        )
    assert (
        backend._qualifying_timeout_counts(
            events, fault_ns=10_000, baseline_cutoff=0, current_cutoff=len(events)
        )
        is None
    )


def test_v9_uses_six_complete_buckets_while_v8_keeps_archived_window_width() -> None:
    runtime = _runtime()
    bucket_width_ns = 5_000_000_000
    v8 = runtime.load_focused_profile(N7_PROFILE_V8)
    v9 = runtime.load_focused_profile(N7_PROFILE_V9)

    assert (
        runtime._phase_measurement_duration_ns(v8, bucket_width_ns) == bucket_width_ns
    )
    assert runtime._phase_measurement_duration_ns(v9, bucket_width_ns) == 30_000_000_000


def test_v6_runtime_exact_timeout_guard_rejects_cross_attempt_late_bleed() -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(N7_PROFILE_V6)
    backend = object.__new__(runtime.FocusedRawEvidenceSource)
    backend._profile = profile
    events = _v6_runtime_timeout_events(profile)
    observation = events[-1]["payload"]["observation"]
    assert isinstance(observation, dict)
    observation["outcome"] = "late"
    observation["reporter_id"] = 4 if observation["reporter_id"] != 4 else 5
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="identity"):
        backend._qualifying_timeout_counts(
            events, fault_ns=10_000, baseline_cutoff=0, current_cutoff=len(events)
        )


_CONTROLLER_FAILURE_MUTATIONS = (
    ("v1-v3 absent", {"reason": "controller_unhealthy"}, False, True),
    ("v4 missing", {"reason": "controller_unhealthy"}, True, False),
    (
        "unhealthy null",
        {"reason": "controller_unhealthy", "controller_failure": None},
        False,
        False,
    ),
    (
        "healthy detail",
        {
            "reason": "success",
            "controller_failure": {
                "stage": "operational_precondition",
                "selection_status": None,
                "epoch_factory_status": None,
            },
        },
        False,
        False,
    ),
    (
        "operational",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "operational_precondition",
                "selection_status": None,
                "epoch_factory_status": None,
            },
        },
        True,
        True,
    ),
    (
        "guarded recoverable",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "guarded_selection",
                "selection_status": "insufficient_guarded_candidates",
                "epoch_factory_status": None,
            },
        },
        True,
        False,
    ),
    (
        "guarded fatal",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "guarded_selection",
                "selection_status": "internal_failure",
                "epoch_factory_status": None,
            },
        },
        True,
        True,
    ),
    (
        "guarded cohort bound exceeded",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "guarded_selection",
                "selection_status": "guarded_candidate_bound_exceeded",
                "epoch_factory_status": None,
            },
        },
        True,
        True,
    ),
    (
        "guarded factory",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "guarded_selection",
                "selection_status": "insufficient_guarded_candidates",
                "epoch_factory_status": "bundle_failed",
            },
        },
        True,
        False,
    ),
    (
        "successor",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "successor_factory",
                "selection_status": "selected",
                "epoch_factory_status": "bundle_failed",
            },
        },
        True,
        True,
    ),
    (
        "unknown field",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "operational_precondition",
                "selection_status": None,
                "epoch_factory_status": None,
                "extra": 1,
            },
        },
        True,
        False,
    ),
    (
        "unknown stage",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "unknown",
                "selection_status": None,
                "epoch_factory_status": None,
            },
        },
        True,
        False,
    ),
    (
        "unknown selection",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "guarded_selection",
                "selection_status": "unknown",
                "epoch_factory_status": None,
            },
        },
        True,
        False,
    ),
    (
        "bool selection",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "guarded_selection",
                "selection_status": True,
                "epoch_factory_status": None,
            },
        },
        True,
        False,
    ),
    (
        "bool factory",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "successor_factory",
                "selection_status": "selected",
                "epoch_factory_status": True,
            },
        },
        True,
        False,
    ),
    (
        "missing stage",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "selection_status": None,
                "epoch_factory_status": None,
            },
        },
        True,
        False,
    ),
    (
        "missing selection",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "operational_precondition",
                "epoch_factory_status": None,
            },
        },
        True,
        False,
    ),
    (
        "missing factory",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "operational_precondition",
                "selection_status": None,
            },
        },
        True,
        False,
    ),
    (
        "factory success",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "successor_factory",
                "selection_status": "selected",
                "epoch_factory_status": "success",
            },
        },
        True,
        False,
    ),
    (
        "successor wrong selection",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "successor_factory",
                "selection_status": "internal_failure",
                "epoch_factory_status": "bundle_failed",
            },
        },
        True,
        False,
    ),
    (
        "successor null",
        {
            "reason": "controller_unhealthy",
            "controller_failure": {
                "stage": "successor_factory",
                "selection_status": "selected",
                "epoch_factory_status": None,
            },
        },
        True,
        False,
    ),
)
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


@pytest.mark.parametrize(
    "_name,payload,required,expected", _CONTROLLER_FAILURE_MUTATIONS
)
@pytest.mark.parametrize(
    "module_name",
    (
        RUNTIME,
        "experiments.adaptive.kauri_experiment.focused_crash_pair_validation",
    ),
)
def test_controller_failure_validator_mutation_matrix(
    module_name: str,
    _name: str,
    payload: Mapping[str, object],
    required: bool,
    expected: bool,
) -> None:
    module = importlib.import_module(module_name)
    assert (
        module._validate_controller_failure_terminal(
            deepcopy(payload), require_for_unhealthy=required
        )
        is expected
    )


def test_v4_manager_terminal_requires_exact_arm_failure_projection() -> None:
    terminal = {
        "cycle_ordinal": 0,
        "policy_intent": "fault_containment",
        "outcome": "failed",
        "reason": "fault_window_arm_missing",
        "transition_artifact_id": "e0-to-e1-containment",
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": "a" * 64,
        "successor_epoch_number": None,
        "successor_epoch_digest": None,
        "command_payload_digest": None,
        "winning_activation": None,
        "evidence_window_activation_generation": 1,
        "baseline_evidence_cutoff": 1,
        "current_evidence_cutoff": 1,
        "controller_failure": None,
    }
    modules = (
        _runtime(),
        importlib.import_module(
            "experiments.adaptive.kauri_experiment.focused_crash_pair_validation"
        ),
    )
    assert all(
        module._validate_v4_manager_terminal_payload(terminal) for module in modules
    )

    post_arm_invalid = deepcopy(terminal)
    post_arm_invalid["reason"] = "fault_window_arm_invalid"
    post_arm_invalid["current_evidence_cutoff"] = 2
    assert all(
        module._validate_v4_manager_terminal_payload(post_arm_invalid)
        for module in modules
    )

    late_missing = deepcopy(terminal)
    late_missing["current_evidence_cutoff"] = 2
    assert not any(
        module._validate_v4_manager_terminal_payload(late_missing) for module in modules
    )

    mutations = (
        ("unknown_reason", "fault_window_arm_unknown"),
        ("wrong_outcome", "advanced"),
        ("wrong_cycle", 1),
        ("wrong_policy", "performance_optimization"),
        ("wrong_predecessor", 1),
        ("successor", 1),
        ("winning", {}),
        ("controller_failure", {}),
        ("cycle_bool", True),
        ("digest_bool", True),
    )
    for field, value in mutations:
        candidate = deepcopy(terminal)
        if field == "unknown_reason":
            candidate["reason"] = value
        elif field == "wrong_outcome":
            candidate["outcome"] = value
        elif field == "wrong_cycle":
            candidate["cycle_ordinal"] = value
        elif field == "wrong_policy":
            candidate["policy_intent"] = value
        elif field == "wrong_predecessor":
            candidate["predecessor_epoch_number"] = value
        elif field == "successor":
            candidate["successor_epoch_number"] = value
        elif field == "winning":
            candidate["winning_activation"] = value
        elif field == "controller_failure":
            candidate["controller_failure"] = value
        elif field == "cycle_bool":
            candidate["cycle_ordinal"] = value
        else:
            candidate["predecessor_epoch_digest"] = value
        assert not any(
            module._validate_v4_manager_terminal_payload(candidate)
            for module in modules
        ), field

    missing = deepcopy(terminal)
    del missing["controller_failure"]
    extra = deepcopy(terminal)
    extra["unexpected"] = None
    assert not any(
        module._validate_v4_manager_terminal_payload(candidate)
        for candidate in (missing, extra)
        for module in modules
    )


def test_v4_pass_terminal_chain_rejects_injected_arm_failure() -> None:
    validation = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_validation"
    )
    command = {
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": "a" * 64,
        "successor_epoch_number": 1,
        "successor_epoch_digest": "b" * 64,
        "payload_digest": "c" * 64,
        "command_block_height": 10,
        "command_block_hash": "d" * 64,
        "activation_delay_blocks": 5,
    }
    activation = {"activation_height": 15}
    winning = {
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": "a" * 64,
        "successor_epoch_number": 1,
        "successor_epoch_digest": "b" * 64,
        "command_payload_digest": "c" * 64,
        "command_block_height": 10,
        "command_block_hash": "d" * 64,
        "activation_delay_blocks": 5,
        "activation_height": 15,
    }
    terminal = {
        "cycle_ordinal": 0,
        "policy_intent": "fault_containment",
        "outcome": "advanced",
        "reason": "successor_converged",
        "transition_artifact_id": "e0-to-e1-containment",
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": "a" * 64,
        "successor_epoch_number": 1,
        "successor_epoch_digest": "b" * 64,
        "command_payload_digest": "c" * 64,
        "winning_activation": winning,
        "evidence_window_activation_generation": 1,
        "baseline_evidence_cutoff": 7,
        "current_evidence_cutoff": 9,
        "controller_failure": None,
    }
    events = [
        {
            "source_kind": "adaptation_manager",
            "event_type": "adaptive_v2_evidence_snapshot",
            "source_sequence": 1,
            "payload": {
                "predecessor_epoch_number": 0,
                "activation_generation": 1,
                "baseline_cutoff": 7,
                "current_cutoff": 9,
            },
        },
        {
            "source_kind": "adaptation_manager",
            "event_type": "adaptive_v2_session_terminal",
            "source_sequence": 2,
            "payload": terminal,
        },
    ]
    epoch = SimpleNamespace(
        epoch_digest="b" * 64,
        command=SimpleNamespace(payload_digest="c" * 64),
    )
    kwargs = {
        "contract": {"epoch_zero_digest": "a" * 64},
        "epoch1": epoch,
        "epoch2": None,
        "commands1": [{"payload": command}],
        "commands2": [],
        "activations1": [{"payload": activation}],
        "activations2": [],
    }
    validation._validate_v4_pass_terminals(events, **kwargs)

    for snapshot_field, terminal_field in (
        ("baseline_cutoff", "baseline_evidence_cutoff"),
        ("current_cutoff", "current_evidence_cutoff"),
    ):
        for delta in (-1, 1):
            snapshot_drift = deepcopy(events)
            snapshot_drift[0]["payload"][snapshot_field] += delta
            with pytest.raises(
                validation.FocusedCrashPairValidationError,
                match="identity drifted",
            ):
                validation._validate_v4_pass_terminals(snapshot_drift, **kwargs)
            terminal_drift = deepcopy(events)
            terminal_drift[1]["payload"][terminal_field] += delta
            with pytest.raises(
                validation.FocusedCrashPairValidationError,
                match="identity drifted",
            ):
                validation._validate_v4_pass_terminals(terminal_drift, **kwargs)

    injected = deepcopy(terminal)
    injected.update(
        outcome="failed",
        reason="fault_window_arm_missing",
        successor_epoch_number=None,
        successor_epoch_digest=None,
        command_payload_digest=None,
        winning_activation=None,
        baseline_evidence_cutoff=7,
        current_evidence_cutoff=7,
    )
    events.append(
        {
            "source_kind": "adaptation_manager",
            "event_type": "adaptive_v2_session_terminal",
            "source_sequence": 3,
            "payload": injected,
        }
    )
    with pytest.raises(validation.FocusedCrashPairValidationError, match="cardinality"):
        validation._validate_v4_pass_terminals(events, **kwargs)


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
        "pre_signal_monotonic_ns": fault_ns - 1,
        "prefault_active_configuration_barrier": [
            {"replica_id": replica, "configuration": dict(configuration)}
            for replica in profile.replica_ids
        ],
    }
    timeout_observations: list[dict[str, object]] = []
    qualifying_counts: dict[str, dict[str, int]] = {}
    for target in plan["targets"]:
        reporters = (
            target["authenticated_reporter_ids"]
            if "authenticated_reporter_ids" in target
            else [
                reporter["reporter_id"]
                for reporter in target["eligible_reporters"][
                    : plan["required_qualifying_reporters"]
                ]
            ]
        )
        qualifying_counts[str(target["target_replica_id"])] = {
            str(reporter): int(plan["minimum_timeouts_per_reporter"])
            for reporter in reporters
        }
        for reporter in reporters:
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
        "qualifying_timeout_counts": qualifying_counts,
        **(
            {
                "postfault_progress": {
                    "required_tree_positions": plan[
                        "required_postfault_tree_positions"
                    ],
                    "actual_tree_positions": plan["required_postfault_tree_positions"],
                    "starting_tree_id": plan["active_tree_id"],
                    "observed_tree_ids": list(
                        range(plan["required_postfault_tree_positions"])
                    ),
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
            "source_monotonic_ns": 88 + position,
            "event_type": "adaptive.configuration_active",
            "payload": {
                "epoch_number": 0,
                "tree_id": members[position % len(members)],
                "epoch_digest": digest,
            },
        }
        for position in range(len(members))
    ]
    configurations.extend(
        {
            **configuration,
            "source_sequence": 3 + len(members) + position,
            "source_monotonic_ns": 101 + position,
            "payload": {
                **configuration["payload"],
                "tree_id": members[
                    (members.index(starting_tree) + position + 1) % len(members)
                ],
            },
        }
        for position, configuration in enumerate(configurations[:5])
    )
    commits = [
        {
            "run_id": run_id,
            "source_kind": "replica",
            "source_id": source_id,
            "source_instance": instance,
            "event_type": "block.committed",
            "source_sequence": index + 16,
            "source_monotonic_ns": 120 + index,
            "payload": {
                "block_height": index + 1,
                "block_hash": f"{index + 1:064x}",
                "parent_hash": "00" * 32 if index == 0 else f"{index:064x}",
                "transaction_count": 0 if index % 2 == 0 else 1000,
                "commit_batch_index": index % 3,
                "designated_observer": True,
                "view_generation": 6 if index == 0 else 12 if index == 1 else 5,
                "decision_proof": {
                    "epoch_number": 0,
                    "epoch_digest": digest,
                    "block_hash": f"{index + 1:064x}",
                    "tree_id": 5 if index == 0 else 4,
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
        _v3_raw_progress_events(profile), fault_ns=100, prefault_ns=100, audit_ns=200
    ) == {
        "required_tree_positions": 6,
        "actual_tree_positions": 6,
        "starting_tree_id": 6,
        "observed_tree_ids": [6, 0, 1, 2, 3, 4],
    }


def test_v3_raw_progress_rejects_configuration_after_signal_request() -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._profile = profile
    events = _v3_raw_progress_events(profile)
    between_request_and_confirmation = deepcopy(events[3])
    between_request_and_confirmation["source_sequence"] = 4
    between_request_and_confirmation["source_monotonic_ns"] = 99
    events.append(between_request_and_confirmation)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="fault batch"):
        source._postfault_authoritative_progress(
            events, fault_ns=100, prefault_ns=95, audit_ns=200
        )


@pytest.mark.parametrize(
    ("boundary_ns", "foreign_source"),
    ((100, False), (110, False), (105, True)),
)
def test_v3_raw_progress_rejects_any_member_configuration_in_fault_batch(
    boundary_ns: int, foreign_source: bool
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._profile = profile
    events = _v3_raw_progress_events(profile)
    postfault = [
        event
        for event in events
        if event["event_type"] == "adaptive.configuration_active"
        and event["source_monotonic_ns"] > 100
    ]
    for position, event in enumerate(postfault, start=1):
        event["source_monotonic_ns"] = 110 + position
    injected = deepcopy(postfault[0])
    if foreign_source:
        injected["source_id"] = "replica-0"
        injected["source_instance"] = "v3-progress-run-replica-0-foreign-uuid"
    injected.update(
        {
            "source_sequence": 1,
            "source_monotonic_ns": boundary_ns,
        }
    )
    events.append(injected)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="fault batch"):
        source._postfault_authoritative_progress(
            events, fault_ns=110, prefault_ns=100, audit_ns=200
        )


def _v3_common_commit_fixture(tmp_path: Path) -> tuple[object, list[dict[str, object]]]:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    _control_wire, _control, epoch1_wire, epoch1 = (
        native_fixture._independent_epoch1_bundles()
    )
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    (root / "raw" / "epoch1.bundle").write_bytes(epoch1_wire)
    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._root = root
    source._profile = replace(
        profile, issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY
    )
    observer = int(profile.raw["measurement"]["authoritative_replica_id"])
    payload = {
        "block_height": 1,
        "block_hash": "1" * 64,
        "parent_hash": "0" * 64,
        "transaction_count": 1000,
        "commit_batch_index": 0,
        "designated_observer": True,
        "view_generation": (1 << 32) + 1,
        "decision_proof": {
            "epoch_number": 1,
            "epoch_digest": epoch1.epoch_digest,
            "block_hash": "1" * 64,
            "tree_id": 0,
        },
    }
    events: list[dict[str, object]] = [
        {
            "source_kind": "replica",
            "source_id": f"replica-{observer}",
            "source_instance": "v3-authoritative-uuid",
            "source_sequence": sequence,
            "source_monotonic_ns": sequence,
            "event_type": event_type,
            "payload": {},
        }
        for sequence, event_type in ((1, "process.started"), (2, "process.ready"))
    ]
    events.append(
        {
            "source_kind": "replica",
            "source_id": f"replica-{observer}",
            "source_instance": "v3-authoritative-uuid",
            "source_sequence": 3,
            "source_monotonic_ns": 3,
            "event_type": "adaptive.configuration_active",
            "payload": {
                "epoch_number": 1,
                "tree_id": 0,
                "epoch_digest": epoch1.epoch_digest,
            },
        }
    )
    events.append(
        {
            "source_kind": "replica",
            "source_id": f"replica-{observer}",
            "source_instance": "v3-authoritative-uuid",
            "source_sequence": 4,
            "source_monotonic_ns": 4,
            "event_type": "block.committed",
            "payload": payload,
        }
    )
    identity = {
        key: payload[key]
        for key in (
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "commit_batch_index",
        )
    }
    survivors = [
        replica
        for replica in profile.replica_ids
        if replica not in profile.target_replica_ids
    ]
    for replica in survivors[: profile.quorum]:
        instance = f"v3-replica-{replica}-uuid"
        if replica != observer:
            events.extend(
                {
                    "source_kind": "replica",
                    "source_id": f"replica-{replica}",
                    "source_instance": instance,
                    "source_sequence": sequence,
                    "source_monotonic_ns": sequence,
                    "event_type": event_type,
                    "payload": {},
                }
                for sequence, event_type in (
                    (1, "process.started"),
                    (2, "process.ready"),
                )
            )
        events.append(
            {
                "source_kind": "replica",
                "source_id": f"replica-{replica}",
                "source_instance": (
                    "v3-authoritative-uuid" if replica == observer else instance
                ),
                "source_sequence": 5 if replica == observer else 3,
                "source_monotonic_ns": 5 if replica == observer else 3,
                "event_type": "block.commit_observed",
                "payload": dict(identity),
            }
        )
    return source, events


def test_v3_raw_common_commit_accepts_full_and_pends_for_insufficient_witnesses(
    tmp_path: Path,
) -> None:
    source, events = _v3_common_commit_fixture(tmp_path)
    assert source._common_commit(events, 1) is not None
    assert source._common_commit(events[:-1], 1) is None


def test_v3_raw_common_commit_accepts_zero_transaction_workload(
    tmp_path: Path,
) -> None:
    source, events = _v3_common_commit_fixture(tmp_path)
    for event in events:
        if event["event_type"] in {"block.committed", "block.commit_observed"}:
            event["payload"]["transaction_count"] = 0
    snapshot = source._common_commit(events, 1)
    assert snapshot is not None
    assert snapshot["transaction_count"] == 0


def _v5_runtime_phase_fixture(
    *, arm: str = "adaptive"
) -> tuple[object, list[dict[str, object]], dict[str, object], object]:
    runtime = _runtime()
    profile = runtime.load_focused_profile(N7_PROFILE_V5)
    second = 1_000_000_000
    digests = ("0" * 64, "1" * 64, "2" * 64)
    observer = int(profile.raw["measurement"]["authoritative_replica_id"])

    def commit(epoch: int, height: int, timestamp: int) -> dict[str, object]:
        block_hash = f"{height:064x}"
        return {
            "source_kind": "replica",
            "source_id": f"replica-{observer}",
            "source_monotonic_ns": timestamp * second,
            "event_type": "block.committed",
            "payload": {
                "block_height": height,
                "block_hash": block_hash,
                "parent_hash": "0" * 64 if height == 1 else f"{height - 1:064x}",
                "transaction_count": 1000,
                "commit_batch_index": 0,
                "decision_proof": {
                    "epoch_number": epoch,
                    "epoch_digest": digests[epoch],
                    "tree_id": 0,
                    "block_hash": block_hash,
                },
            },
        }

    commits = [
        commit(0, 1, 5),
        commit(0, 2, 36),
        commit(0, 3, 49),
        commit(1, 4, 60),
        commit(1, 5, 91),
        *(
            [commit(2, 6, 110), commit(2, 7, 141)]
            if arm == "adaptive"
            else [commit(1, 6, 126)]
        ),
    ]
    events = list(commits)
    survivors = [
        replica
        for replica in profile.replica_ids
        if replica not in profile.target_replica_ids
    ]
    for item in commits:
        payload = item["payload"]
        assert isinstance(payload, dict)
        identity = {
            key: payload[key]
            for key in (
                "block_height",
                "block_hash",
                "parent_hash",
                "transaction_count",
                "commit_batch_index",
            )
        }
        events.extend(
            {
                "source_kind": "replica",
                "source_id": f"replica-{replica}",
                "source_monotonic_ns": item["source_monotonic_ns"],
                "event_type": "block.commit_observed",
                "payload": dict(identity),
            }
            for replica in survivors
        )
    events.extend(
        {
            "source_kind": "replica",
            "source_id": f"replica-{replica}",
            "source_monotonic_ns": timestamp * second,
            "event_type": "epoch.activated",
            "payload": {"epoch_number": epoch},
        }
        for epoch, timestamp in (
            ((1, 55), (2, 105)) if arm == "adaptive" else ((1, 55),)
        )
        for replica in survivors
    )
    events.extend(
        {
            "source_kind": "replica",
            "source_id": f"replica-{replica}",
            "source_monotonic_ns": timestamp * second,
            "event_type": "epoch.command_committed",
            "payload": {"successor_epoch_number": epoch},
        }
        for epoch, timestamp in (
            ((1, 50), (2, 100)) if arm == "adaptive" else ((1, 50),)
        )
        for replica in survivors
    )
    receipt = {
        "sigkill_outcomes": [
            {
                "requested_monotonic_ns": 40 * second,
                "confirmed_monotonic_ns": 40 * second,
            }
        ]
    }

    class PhaseSource:
        def _common_commit(self, _events: object, _epoch: int) -> dict[str, object]:
            return {"source_monotonic_ns": 1}

        def _transition(
            self, _events: object, epoch: int, *, activation: bool
        ) -> dict[str, object]:
            assert activation
            return {"source_monotonic_ns": (55 if epoch == 1 else 105) * second}

    return profile, events, receipt, PhaseSource()


@pytest.mark.parametrize(
    ("arm", "late_epoch", "late_start"),
    (("adaptive", 2, 140), ("control", 1, 125)),
)
def test_v5_runtime_materializes_exact_causal_phase_windows(
    arm: str, late_epoch: int, late_start: int
) -> None:
    runtime = _runtime()
    profile, events, receipt, source = _v5_runtime_phase_fixture(arm=arm)
    document = runtime._v5_phase_window_document(profile, arm, events, receipt, source)
    assert document["domain"] == "kauri-focused-causal-phase-windows-v1"
    assert document["phases"] == [
        {
            "phase": "baseline",
            "start_ns": 35_000_000_000,
            "end_ns": 40_000_000_000,
            "epoch_number": 0,
        },
        {
            "phase": "fault",
            "start_ns": 40_000_000_000,
            "end_ns": 45_000_000_000,
            "epoch_number": 0,
        },
        {
            "phase": "epoch1",
            "start_ns": 90_000_000_000,
            "end_ns": 95_000_000_000,
            "epoch_number": 1,
        },
        {
            "phase": "late",
            "start_ns": late_start * 1_000_000_000,
            "end_ns": (late_start + 5) * 1_000_000_000,
            "epoch_number": late_epoch,
        },
    ]


def test_v5_runtime_accepts_a_truly_empty_fault_interval() -> None:
    runtime = _runtime()
    profile, events, receipt, source = _v5_runtime_phase_fixture()
    document = runtime._v5_phase_window_document(
        profile, "adaptive", events, receipt, source
    )
    fault = next(row for row in document["phases"] if row["phase"] == "fault")
    assert not [
        event
        for event in events
        if event["event_type"] == "block.committed"
        and fault["start_ns"] <= event["source_monotonic_ns"] < fault["end_ns"]
    ]


@pytest.mark.parametrize("mutation", ("wrong-epoch-fault", "command-at-fault-end"))
def test_v5_runtime_rejects_fault_bucket_epoch_or_transition_drift(
    mutation: str,
) -> None:
    runtime = _runtime()
    profile, events, receipt, source = _v5_runtime_phase_fixture()
    if mutation == "wrong-epoch-fault":
        wrong = deepcopy(
            next(
                event
                for event in events
                if event["event_type"] == "block.committed"
                and event["payload"]["decision_proof"]["epoch_number"] == 1
            )
        )
        wrong["source_monotonic_ns"] = 42_000_000_000
        events.append(wrong)
    else:
        for event in events:
            if (
                event["event_type"] == "epoch.command_committed"
                and event["payload"]["successor_epoch_number"] == 1
            ):
                event["source_monotonic_ns"] = 45_000_000_000
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        runtime._v5_phase_window_document(profile, "adaptive", events, receipt, source)


def test_v5_runtime_rejects_phase_without_quorum_common_commit() -> None:
    runtime = _runtime()
    profile, events, receipt, source = _v5_runtime_phase_fixture()
    last_survivor = max(
        replica
        for replica in profile.replica_ids
        if replica not in profile.target_replica_ids
    )
    events = [
        event
        for event in events
        if not (
            event["event_type"] == "block.commit_observed"
            and event["source_id"] == f"replica-{last_survivor}"
            and event["payload"]["block_height"] in {6, 7}
        )
    ]
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="common commit"):
        runtime._v5_phase_window_document(profile, "adaptive", events, receipt, source)


@pytest.mark.parametrize(("after_fault", "raises"), ((479.0, False), (480.0, True)))
def test_v5_run_arm_hard_cap_is_anchored_at_fault(
    after_fault: float,
    raises: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(N31_PROFILE_V5)
    clock = [100.0]
    deadlines: list[float | None] = []

    def wait(
        _name: str, _poll: object, *, deadline_monotonic: float | None
    ) -> dict[str, object]:
        deadlines.append(deadline_monotonic)
        if _name == "fault":
            return {"receipt": True}
        if _name == "commit1":
            clock[0] = 200.0 + after_fault
        if deadline_monotonic is not None and clock[0] >= deadline_monotonic:
            raise runtime.FocusedCrashPairRuntimeError("deadline")
        return {}

    root = tmp_path / "run"
    root.mkdir()
    backend = runtime.FocusedLaunchBackend(
        execute_fault=lambda _c, _p: {"receipt": True}
    )
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(backend, "_wait_for_snapshot", wait)
    monkeypatch.setattr(
        backend, "_wait_for_prefault_configuration", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        backend, "_wait_for_v4_fault_window_coverage", lambda *_a, **_k: None
    )
    monkeypatch.setattr(runtime, "_fault_window_arm_document", lambda *_a: {})
    monkeypatch.setattr(runtime, "_publish_fault_window_arm", lambda *_a: "a" * 64)

    class Source:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def poll(self, _name: str) -> dict[str, object]:
            return {"receipt": True} if _name == "fault" else {}

        def unexpected_exit_ids(self) -> tuple[int, ...]:
            return ()

    monkeypatch.setattr(runtime, "FocusedRawEvidenceSource", Source)
    monkeypatch.setattr(
        runtime,
        "_drive_arm_state_machine",
        lambda _profile, _arm, _pair, hooks: (
            hooks.wait_for_stable_phase("baseline"),
            hooks.inject_atomic_fault_batch(),
            hooks.wait_for_common_commit(1),
            {},
        )[-1],
    )

    def fault(
        configuration: Mapping[str, object], processes: object
    ) -> dict[str, object]:
        clock[0] = 200.0
        backend._fault_receipts[str(root.resolve())] = {"receipt": True}
        return {"receipt": True}

    backend._execute_fault = fault
    configuration = {
        "profile": profile,
        "pair_id": "pair-01",
        "arm": "control",
        "run_directory": root,
        "run_id": "hard-cap",
        "source_instances": {},
        "fault_window_arm_path": root / "fault-window-arm.json",
    }
    expected = (
        pytest.raises(runtime.FocusedCrashPairRuntimeError, match="deadline")
        if raises
        else nullcontext()
    )
    with expected:
        backend.run_arm(configuration, SimpleNamespace(records=()))
    assert deadlines == [160.0, 580.0, 680.0, 680.0]


def test_v5_runtime_phase_document_matches_validator_reconstruction(
    tmp_path: Path,
) -> None:
    """The producer and source-blind replay derive the same causal windows."""

    runtime = _runtime()
    validation = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_validation"
    )
    profile, events, receipt, source = _v5_runtime_phase_fixture(arm="adaptive")
    document = runtime._v5_phase_window_document(
        profile, "adaptive", events, receipt, source
    )
    root = tmp_path / "same-synthetic-fixture"
    receipt_path = root / "raw" / "fault-receipt.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    measurement = profile.raw["measurement"]
    assert isinstance(measurement, Mapping)
    contract = {
        "profile": profile.raw,
        "phase_window_contract": measurement["phase_window_contract"],
        "bucket_width_seconds": measurement["bucket_width_seconds"],
        "phase_names": measurement["phase_names"],
        "members": profile.replica_ids,
        "survivors": tuple(
            replica
            for replica in profile.replica_ids
            if replica not in profile.target_replica_ids
        ),
        "quorum": profile.quorum,
        "authoritative_source_id": "replica-0",
    }
    commits = [event for event in events if event["event_type"] == "block.committed"]
    derived = validation._v5_causal_phase_windows(
        root, events, commits, object(), contract
    )
    assert document["phases"] == [
        {
            "phase": phase,
            "start_ns": start,
            "end_ns": end,
            "epoch_number": epoch,
        }
        for phase, start, end, epoch in derived
    ]


def test_v5_default_materializer_writes_phase_document_from_finalized_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(N7_PROFILE_V5)
    root = tmp_path / "arm"
    for relative in (
        "raw",
        "runtime",
        "derived",
        "transitions/e0-to-e1-containment",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "raw" / "replica-0.jsonl").write_bytes(
        _canonical_json({"source_kind": "replica", "source_id": "replica-0"})
    )
    (root / "raw" / "adaptive-manager.jsonl").write_bytes(
        _canonical_json(
            {"source_kind": "adaptation_manager", "source_id": "adaptive-manager"}
        )
    )
    (root / "raw" / "client-events.jsonl").write_bytes(b"")
    (root / "transitions" / "e0-to-e1-containment" / "successor.bundle").write_bytes(
        b"signed-bundle"
    )
    receipt = {"schema_version": 1, "sigkill_outcomes": []}
    (root / "raw" / "fault-receipt.json").write_bytes(_canonical_json(receipt))
    expected_document = {
        "schema_version": 1,
        "domain": "kauri-focused-causal-phase-windows-v1",
        "phases": [{"phase": "sentinel"}],
    }
    captured: list[Mapping[str, object]] = []

    class Source:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def _events(self) -> list[dict[str, object]]:
            return [{"validated": True}]

    def phase_document(
        _profile: object,
        _arm: str,
        events: Sequence[Mapping[str, object]],
        finalized_receipt: Mapping[str, object],
        _source: object,
    ) -> dict[str, object]:
        assert events == [{"validated": True}]
        captured.append(finalized_receipt)
        return expected_document

    monkeypatch.setattr(runtime, "FocusedRawEvidenceSource", Source)
    monkeypatch.setattr(runtime, "_v5_phase_window_document", phase_document)
    backend = runtime.FocusedLaunchBackend()
    backend._fault_receipts[str(root.resolve())] = receipt
    backend.materialize_artifacts(
        {
            "run_directory": root,
            "run_id": "v5-materializer-run",
            "source_instances": {},
            "profile": profile,
            "arm": "control",
        },
        {"runtime_graph": "complete"},
        {"complete": True, "outcomes": []},
    )
    assert captured == [receipt]
    assert json.loads((root / "derived" / "phase-windows.json").read_bytes()) == (
        expected_document
    )


def test_v3_raw_common_commit_caches_lifecycle_identity_per_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime()
    source, events = _v3_common_commit_fixture(tmp_path)
    repeated = [deepcopy(events[-1]) for _ in range(1_000)]
    for event in repeated:
        event["payload"]["block_hash"] = "f" * 64
    events.extend(repeated)
    original = runtime._authoritative_lifecycle_instance
    calls: list[str] = []

    def counted(raw_events: Sequence[Mapping[str, object]], source_id: str) -> str:
        calls.append(source_id)
        return original(raw_events, source_id)

    monkeypatch.setattr(runtime, "_authoritative_lifecycle_instance", counted)
    assert source._common_commit(events, 1) is not None
    configured_sources = {
        str(event["source_id"])
        for event in events
        if event["event_type"] in {"block.committed", "block.commit_observed"}
    }
    assert len(calls) <= len(configured_sources)


@pytest.mark.parametrize(
    "mutation",
    (
        "transactions",
        "view-overflow",
        "batch-overflow",
        "unactivated-tree-generation",
        "client-spoof",
        "nonmember-spoof",
    ),
)
def test_v3_raw_common_commit_rejects_invalid_raw_commit_or_observation(
    mutation: str, tmp_path: Path
) -> None:
    runtime = _runtime()
    source, events = _v3_common_commit_fixture(tmp_path)
    commit = next(event for event in events if event["event_type"] == "block.committed")
    if mutation == "transactions":
        commit["payload"]["transaction_count"] = 13
        for event in events[1:]:
            event["payload"]["transaction_count"] = 13
    elif mutation == "view-overflow":
        commit["payload"]["view_generation"] = 1 << 64
    elif mutation == "batch-overflow":
        commit["payload"]["commit_batch_index"] = 1 << 64
        for event in events[1:]:
            event["payload"]["commit_batch_index"] = 1 << 64
    elif mutation == "unactivated-tree-generation":
        commit["payload"]["decision_proof"]["tree_id"] = 99
        commit["payload"]["view_generation"] = (1 << 32) + 2
    else:
        spoof = deepcopy(events[-1])
        spoof["payload"]["block_hash"] = "f" * 64
        if mutation == "client-spoof":
            spoof["source_kind"] = "client"
            spoof["source_id"] = "client-0"
        else:
            spoof["source_id"] = "replica-999"
        events.append(spoof)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        source._common_commit(events, 1)


def test_prefault_tail_latch_rejects_rewrite_after_initial_drain(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    events = []
    for replica in profile.replica_ids:
        events.append(
            {
                "event_schema_version": 1,
                "run_id": "tail-run",
                "source_kind": "replica",
                "source_id": f"replica-{replica}",
                "source_instance": f"tail-run-replica-{replica}-uuid",
                "source_sequence": 1,
                "source_monotonic_ns": 1,
                "event_type": "adaptive.configuration_active",
                "payload": {
                    "epoch_number": 0,
                    "tree_id": 6,
                    "epoch_digest": profile.raw["topology"]["epoch_zero_digest"],
                },
            }
        )
        events.append(
            {
                "event_schema_version": 1,
                "run_id": "tail-run",
                "source_kind": "replica",
                "source_id": f"replica-{replica}",
                "source_instance": f"tail-run-replica-{replica}-uuid",
                "source_sequence": 2,
                "source_monotonic_ns": 2,
                "event_type": "adaptive.configuration_active",
                "payload": {
                    "epoch_number": 0,
                    "tree_id": 0,
                    "epoch_digest": profile.raw["topology"]["epoch_zero_digest"],
                },
            }
        )
    for replica in profile.replica_ids:
        (root / "raw" / f"replica-{replica}.jsonl").write_text(
            "\n".join(
                json.dumps(event)
                for event in events
                if event["source_id"] == f"replica-{replica}"
            )
            + "\n",
            encoding="utf-8",
        )
    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._root = root
    source._profile = profile
    assert source.latch_prefault_active_configuration_barrier() is None
    for replica in profile.replica_ids:
        exact = next(
            event
            for event in events
            if event["source_id"] == f"replica-{replica}"
            and event["source_sequence"] == 1
        )
        ordinary = {
            **exact,
            "source_sequence": 2,
            "source_monotonic_ns": 2,
            "event_type": "block.committed",
            "payload": {},
        }
        (root / "raw" / f"replica-{replica}.jsonl").write_text(
            json.dumps(exact) + "\n" + json.dumps(ordinary) + "\n",
            encoding="utf-8",
        )
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="truncated"):
        source.latch_prefault_active_configuration_barrier()


def test_n31_baseline_cache_refreshes_only_the_incremental_prefault_barrier() -> None:
    """Stable baseline replay is immutable; the barrier must remain live/raw."""

    runtime = _runtime()
    profile = _fcrash_h_profile(N31_PROFILE_V3)
    active = {
        "epoch_number": 0,
        "tree_id": profile.raw["topology"]["active_tree_id"],
        "epoch_digest": profile.raw["topology"]["epoch_zero_digest"],
    }
    barrier = [
        {"replica_id": replica, "configuration": dict(active)}
        for replica in profile.replica_ids
    ]
    events: list[dict[str, object]] = []
    for replica in profile.replica_ids:
        for event_type in ("process.started", "process.ready"):
            events.append(
                {
                    "source_kind": "replica",
                    "source_id": f"replica-{replica}",
                    "event_type": event_type,
                    "payload": {},
                }
            )
        events.append(
            {
                "source_kind": "replica",
                "source_id": f"replica-{replica}",
                "event_type": "adaptive.configuration_active",
                "payload": dict(active),
            }
        )
    for event_type in ("process.started", "process.ready"):
        events.append(
            {
                "source_kind": "adaptation_manager",
                "source_id": "adaptive-manager",
                "event_type": event_type,
                "payload": {},
            }
        )
    stable_seconds = int(profile.raw["timers"]["stable_phase_seconds"])
    for timestamp in (1, stable_seconds * 1_000_000_000 + 1):
        events.append(
            {
                "source_kind": "replica",
                "source_id": "replica-0",
                "event_type": "block.committed",
                "source_monotonic_ns": timestamp,
                "payload": {"decision_proof": {"epoch_number": 0}},
            }
        )

    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._profile = profile
    source._process_records = ()
    calls: list[str] = []
    source._events = lambda: calls.append("events") or events
    source._reject_post_fault_target_events = lambda _events: None
    source.unexpected_exit_ids = lambda _events: ()
    source.latch_prefault_active_configuration_barrier = (
        lambda: calls.append("latch") or barrier
    )

    first = source.poll("baseline")
    second = source.poll("baseline")

    assert first is not None and second is not None
    assert first["active_configuration_barrier"] == barrier
    assert second["active_configuration_barrier"] == barrier
    assert calls == ["events", "latch", "latch"]
    assert not hasattr(source, "_baseline_stable_events")


def test_n31_cached_baseline_waits_for_an_exact_live_barrier() -> None:
    """A cached stable baseline must not accept tree zero or malformed latches."""

    runtime = _runtime()
    profile = _fcrash_h_profile(N31_PROFILE_V3)
    active = {
        "epoch_number": 0,
        "tree_id": profile.raw["topology"]["active_tree_id"],
        "epoch_digest": profile.raw["topology"]["epoch_zero_digest"],
    }
    exact = [
        {"replica_id": replica, "configuration": dict(active)}
        for replica in profile.replica_ids
    ]
    tree_zero = deepcopy(exact)
    tree_zero[0]["configuration"]["tree_id"] = 0
    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._profile = profile
    source._process_records = ()
    source._baseline_stable_result = {
        "stable": True,
        "authoritative_commit_count": 2,
        "source_monotonic_ns": 30_000_000_001,
    }
    source._events = lambda: pytest.fail("cached baseline reparsed aggregate events")
    latches = iter((tree_zero, None, exact))
    source.latch_prefault_active_configuration_barrier = lambda: next(latches)

    assert source.poll("baseline") is None
    assert source.poll("baseline") is None
    snapshot = source.poll("baseline")
    assert snapshot is not None
    assert snapshot["active_configuration_barrier"] == exact


@pytest.mark.parametrize("replica_id", (-2, -1, 0))
def test_n31_cached_baseline_keeps_live_process_exit_checks(replica_id: int) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N31_PROFILE_V3)
    polls = iter((None, 1))
    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._profile = profile
    source._process_records = (
        SimpleNamespace(
            replica_id=replica_id,
            process=SimpleNamespace(poll=lambda: next(polls)),
        ),
    )
    source._baseline_stable_result = {
        "stable": True,
        "authoritative_commit_count": 2,
        "source_monotonic_ns": 30_000_000_001,
    }
    source._events = lambda: pytest.fail("cached baseline reparsed aggregate events")
    source.latch_prefault_active_configuration_barrier = lambda: None

    assert source.poll("baseline") is None
    with pytest.raises(
        runtime.FocusedCrashPairRuntimeError, match="unexpected process exit"
    ):
        source.poll("baseline")


def test_n31_cached_baseline_rejects_spoofed_live_barrier_identity(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N31_PROFILE_V3)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    source._process_records = ()
    source._baseline_stable_result = {
        "stable": True,
        "authoritative_commit_count": 2,
        "source_monotonic_ns": 30_000_000_001,
    }
    source._events = lambda: pytest.fail("cached baseline reparsed aggregate events")
    path = root / "raw" / "replica-0.jsonl"
    event = json.loads(path.read_text(encoding="utf-8"))
    event["run_id"] = "spoofed-run"
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="identity"):
        source.poll("baseline")


def _write_exact_live_tail_set(
    root: Path, profile: object, *, run_id: str = "run-a"
) -> object:
    runtime = _runtime()
    instances = {}
    for replica in profile.replica_ids:
        source_instance = (
            f"{run_id}-replica-{replica}-550e8400-e29b-41d4-a716-446655440000"
        )
        instances[f"replica-{replica}"] = source_instance
        event = {
            "event_schema_version": 1,
            "run_id": run_id,
            "source_kind": "replica",
            "source_id": f"replica-{replica}",
            "source_instance": source_instance,
            "source_sequence": 1,
            "source_monotonic_ns": 1,
            "event_type": "adaptive.configuration_active",
            "payload": {
                "epoch_number": 0,
                "tree_id": profile.raw["topology"]["active_tree_id"],
                "epoch_digest": profile.raw["topology"]["epoch_zero_digest"],
                "block_hash": None,
                "context_generation": None,
                "observer_replica": replica,
                "wait_exempt_signers": [],
                "accepted_signers": [],
                "absent_direct_children": [],
                "missing_optional_signers": [],
                "required_branch_gaps": [],
                "root_signer_count": 0,
                "global_quorum": profile.quorum,
                "rejection_reason": None,
            },
        }
        (root / "raw" / f"replica-{replica}.jsonl").write_text(
            json.dumps(event) + "\n", encoding="utf-8"
        )
    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._root = root
    source._profile = profile
    source._expected_run_id = run_id
    source._expected_source_instances = instances
    return source


def _append_v4_authoritative_configurations(
    root: Path, source: object, profile: object, tree_ids: Sequence[int]
) -> None:
    authoritative = profile.raw["measurement"]["authoritative_replica_id"]
    path = root / "raw" / f"replica-{authoritative}.jsonl"
    existing = path.read_text(encoding="utf-8").splitlines()
    initial = json.loads(existing[-1])
    previous_sequence = initial["source_sequence"]
    previous_timestamp = initial["source_monotonic_ns"]
    with path.open("a", encoding="utf-8") as stream:
        for offset, tree_id in enumerate(tree_ids, start=1):
            event = deepcopy(initial)
            event["source_sequence"] = previous_sequence + offset
            event["source_monotonic_ns"] = max(100, previous_timestamp) + offset
            event["payload"]["tree_id"] = tree_id
            stream.write(json.dumps(event) + "\n")


def test_v4_authoritative_postfault_prefix_waits_for_latched_cyclic_coverage(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N31_PROFILE_V4)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    prefix = profile.raw["fault_window_arm"]["ordered_tree_prefix"]
    assert len(prefix) == 16
    _append_v4_authoritative_configurations(root, source, profile, prefix[1:-1])
    assert not source.postfault_authoritative_configuration_prefix_complete(
        evidence_start_monotonic_ns=100,
        prefault_tree_id=prefix[0],
        required_tree_ids=prefix,
    )
    _append_v4_authoritative_configurations(root, source, profile, [prefix[-1]])
    assert source.postfault_authoritative_configuration_prefix_complete(
        evidence_start_monotonic_ns=100,
        prefault_tree_id=prefix[0],
        required_tree_ids=prefix,
    )


@pytest.mark.parametrize("mutation", ("wrong", "skipped", "malformed"))
def test_v4_authoritative_postfault_prefix_rejects_invalid_configuration(
    mutation: str, tmp_path: Path
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N31_PROFILE_V4)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    prefix = profile.raw["fault_window_arm"]["ordered_tree_prefix"]
    values = list(prefix[1:])
    if mutation == "wrong":
        values[0] = prefix[2]
    elif mutation == "skipped":
        values.pop(0)
    _append_v4_authoritative_configurations(root, source, profile, values)
    path = (
        root
        / "raw"
        / f"replica-{profile.raw['measurement']['authoritative_replica_id']}.jsonl"
    )
    if mutation == "malformed":
        rows = path.read_text(encoding="utf-8").splitlines()
        event = json.loads(rows[-1])
        del event["payload"]["epoch_digest"]
        rows[-1] = json.dumps(event)
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="configuration"):
        source.postfault_authoritative_configuration_prefix_complete(
            evidence_start_monotonic_ns=100,
            prefault_tree_id=prefix[0],
            required_tree_ids=prefix,
        )


@pytest.mark.parametrize("unexpected", ((20,), (-1,), (-2,)))
def test_v4_postfault_configuration_wait_rejects_nonexempt_exit_before_arm(
    unexpected: tuple[int, ...],
) -> None:
    runtime = _runtime()
    driver = object.__new__(runtime.FocusedLaunchBackend)
    driver._poll_interval_s = 0
    source = SimpleNamespace(
        unexpected_exit_ids=lambda _events: unexpected,
        postfault_authoritative_configuration_prefix_complete=lambda **_kwargs: pytest.fail(
            "a dead process must prevent arm coverage polling"
        ),
    )
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="process exited"):
        driver._wait_for_v4_fault_window_coverage(
            source,
            SimpleNamespace(records=()),
            {
                "evidence_start_monotonic_ns": 100,
                "prefault_tree_id": 20,
                "required_tree_ids": list(range(20, 31)) + list(range(5)),
            },
            deadline_monotonic=float("inf"),
        )


def test_v4_postfault_configuration_wait_allows_receipt_exempt_sigkill() -> None:
    runtime = _runtime()
    driver = object.__new__(runtime.FocusedLaunchBackend)
    driver._poll_interval_s = 0
    source = SimpleNamespace(
        unexpected_exit_ids=lambda _events: (),
        postfault_authoritative_configuration_prefix_complete=lambda **_kwargs: True,
    )
    driver._wait_for_v4_fault_window_coverage(
        source,
        SimpleNamespace(records=()),
        {
            "evidence_start_monotonic_ns": 100,
            "prefault_tree_id": 20,
            "required_tree_ids": list(range(20, 31)) + list(range(5)),
        },
        deadline_monotonic=float("inf"),
    )


def test_v4_postfault_configuration_rejects_malformed_record_after_coverage(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N31_PROFILE_V4)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    prefix = profile.raw["fault_window_arm"]["ordered_tree_prefix"]
    _append_v4_authoritative_configurations(root, source, profile, prefix[1:])
    path = (
        root
        / "raw"
        / f"replica-{profile.raw['measurement']['authoritative_replica_id']}.jsonl"
    )
    rows = path.read_text(encoding="utf-8").splitlines()
    malformed = json.loads(rows[-1])
    malformed["source_sequence"] += 1
    malformed["source_monotonic_ns"] += 1
    malformed["payload"] = {"epoch_number": 0, "tree_id": prefix[0]}
    rows.append(json.dumps(malformed))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="configuration"):
        source.postfault_authoritative_configuration_prefix_complete(
            evidence_start_monotonic_ns=100,
            prefault_tree_id=prefix[0],
            required_tree_ids=prefix,
        )


def test_v4_postfault_configuration_accepts_valid_rotation_after_coverage(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N31_PROFILE_V4)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    prefix = profile.raw["fault_window_arm"]["ordered_tree_prefix"]
    _append_v4_authoritative_configurations(
        root, source, profile, [*prefix[1:], prefix[0]]
    )
    assert source.postfault_authoritative_configuration_prefix_complete(
        evidence_start_monotonic_ns=100,
        prefault_tree_id=prefix[0],
        required_tree_ids=prefix,
    )


def test_n31_prefault_tail_latch_handles_large_high_rate_prefix(
    tmp_path: Path,
) -> None:
    """The bounded cursor still finds the complete current configuration line."""

    runtime = _runtime()
    profile = _fcrash_h_profile(N31_PROFILE_V3)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    for replica in profile.replica_ids:
        path = root / "raw" / f"replica-{replica}.jsonl"
        configuration = json.loads(path.read_text(encoding="utf-8"))
        leading = [
            {
                **configuration,
                "source_sequence": sequence,
                "source_monotonic_ns": sequence,
                "event_type": "block.committed",
                "payload": {"high_rate_padding": "x" * 16_384},
            }
            for sequence in range(1, 81)
        ]
        configuration["source_sequence"] = 81
        configuration["source_monotonic_ns"] = 81
        path.write_text(
            "\n".join(json.dumps(event) for event in (*leading, configuration)) + "\n",
            encoding="utf-8",
        )

    assert runtime.has_exact_active_configuration_barrier(
        profile, source.latch_prefault_active_configuration_barrier()
    )


def test_prefault_tail_latch_accepts_exact_launched_uuid_streams(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    assert runtime.has_exact_active_configuration_barrier(
        profile, source.latch_prefault_active_configuration_barrier()
    )


def test_prefault_tail_latch_uses_source_sequence_when_timestamps_tie(
    tmp_path: Path,
) -> None:
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    for replica in profile.replica_ids:
        path = root / "raw" / f"replica-{replica}.jsonl"
        first = json.loads(path.read_text())
        later = {
            **first,
            "source_sequence": 2,
            "source_monotonic_ns": first["source_monotonic_ns"],
            "payload": {
                **first["payload"],
                "tree_id": 0,
            },
        }
        path.write_text(
            json.dumps(first) + "\n" + json.dumps(later) + "\n",
            encoding="utf-8",
        )
    assert source.latch_prefault_active_configuration_barrier() is None


@pytest.mark.parametrize("mode", ("foreign", "mixed"))
def test_prefault_tail_latch_rejects_foreign_or_cross_replica_run_ids(
    mode: str, tmp_path: Path
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    path = root / "raw" / "replica-0.jsonl"
    event = json.loads(path.read_text())
    event["run_id"] = "foreign" if mode == "foreign" else "run-b"
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        source.latch_prefault_active_configuration_barrier()


@pytest.mark.parametrize("mutation", ("sequence-gap", "time-regression", "uuid-drift"))
def test_prefault_tail_latch_rejects_source_history_drift(
    mutation: str, tmp_path: Path
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    path = root / "raw" / "replica-0.jsonl"
    first = json.loads(path.read_text())
    assert runtime.has_exact_active_configuration_barrier(
        profile, source.latch_prefault_active_configuration_barrier()
    )
    second = {
        **first,
        "source_sequence": 2,
        "source_monotonic_ns": 2,
        "event_type": "block.committed",
        "payload": {},
    }
    if mutation == "sequence-gap":
        second["source_sequence"] = 3
    elif mutation == "time-regression":
        second["source_monotonic_ns"] = 0
    else:
        second["source_instance"] = "foreign-uuid"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(second) + "\n")
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        source.latch_prefault_active_configuration_barrier()


def test_prefault_tail_latch_accepts_complete_prefix_before_partial_suffix(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    path = root / "raw" / "replica-0.jsonl"
    exact = path.read_bytes()
    path.write_bytes(exact + b'{"event_schema_version":1')
    assert runtime.has_exact_active_configuration_barrier(
        profile, source.latch_prefault_active_configuration_barrier()
    )


def test_prefault_tail_latch_rejects_truncation_after_initial_drain(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    path = root / "raw" / "replica-0.jsonl"
    exact = path.read_bytes()
    assert runtime.has_exact_active_configuration_barrier(
        profile, source.latch_prefault_active_configuration_barrier()
    )
    path.write_bytes(exact[:-1])
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="truncated"):
        source.latch_prefault_active_configuration_barrier()


def test_prefault_tail_cursor_does_not_consume_beyond_captured_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)
    target = root / "raw" / "replica-0.jsonl"
    first = json.loads(target.read_text(encoding="utf-8"))
    appended = {
        **first,
        "source_sequence": 2,
        "source_monotonic_ns": 2,
        "event_type": "block.committed",
        "payload": {},
    }
    original_open = Path.open
    grew = False

    class GrowingReader:
        def __init__(self, stream: object) -> None:
            self._stream = stream

        def __enter__(self) -> "GrowingReader":
            self._stream.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._stream.__exit__(*args)

        def seek(self, *args: object) -> object:
            return self._stream.seek(*args)

        def tell(self) -> int:
            return self._stream.tell()

        def read(self, size: int = -1) -> bytes:
            nonlocal grew
            if not grew:
                grew = True
                with original_open(target, "ab") as writer:
                    writer.write((json.dumps(appended) + "\n").encode("utf-8"))
            return self._stream.read(size)

    def open_with_growth(path: Path, *args: object, **kwargs: object) -> object:
        stream = original_open(path, *args, **kwargs)
        if path == target and args and args[0] == "rb":
            return GrowingReader(stream)
        return stream

    monkeypatch.setattr(Path, "open", open_with_growth)
    assert runtime.has_exact_active_configuration_barrier(
        profile, source.latch_prefault_active_configuration_barrier()
    )
    assert runtime.has_exact_active_configuration_barrier(
        profile, source.latch_prefault_active_configuration_barrier()
    )
    assert source._prefault_tail_states[0]["previous_sequence"] == 2


def test_prefault_wait_reaches_exact_barrier_before_returning() -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    configuration = {
        "epoch_number": 0,
        "tree_id": profile.raw["topology"]["active_tree_id"],
        "epoch_digest": profile.raw["topology"]["epoch_zero_digest"],
    }
    barrier = [
        {"replica_id": replica, "configuration": dict(configuration)}
        for replica in profile.replica_ids
    ]
    trace = []

    class Source:
        def latch_prefault_active_configuration_barrier(self) -> object:
            trace.append("poll")
            return None if len(trace) == 1 else barrier

    processes = SimpleNamespace(
        records=[SimpleNamespace(process=SimpleNamespace(poll=lambda: None))]
    )
    backend = runtime.FocusedLaunchBackend(poll_interval_s=0)
    assert (
        backend._wait_for_prefault_configuration(
            Source(), processes, deadline_monotonic=10**30
        )
        == barrier
    )
    assert trace == ["poll", "poll", "poll"]


def test_prefault_wait_rechecks_process_health_after_confirmation() -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    configuration = {
        "epoch_number": 0,
        "tree_id": profile.raw["topology"]["active_tree_id"],
        "epoch_digest": profile.raw["topology"]["epoch_zero_digest"],
    }
    barrier = [
        {"replica_id": replica, "configuration": dict(configuration)}
        for replica in profile.replica_ids
    ]

    class Source:
        def latch_prefault_active_configuration_barrier(self) -> object:
            return barrier

    polls = iter((None, 1))
    processes = SimpleNamespace(
        records=[SimpleNamespace(process=SimpleNamespace(poll=lambda: next(polls)))]
    )
    backend = runtime.FocusedLaunchBackend(poll_interval_s=0)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="process exited"):
        backend._wait_for_prefault_configuration(
            Source(), processes, deadline_monotonic=10**30
        )


def test_prefault_wait_rejects_tree_drift_during_confirmation(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    source = _write_exact_live_tail_set(root, profile)

    class CandidateThenDrift:
        calls = 0

        def latch_prefault_active_configuration_barrier(self) -> object:
            self.calls += 1
            if self.calls == 2:
                for replica in profile.replica_ids:
                    path = root / "raw" / f"replica-{replica}.jsonl"
                    event = json.loads(path.read_text())
                    event["source_sequence"] = 2
                    event["payload"] = {**event["payload"], "tree_id": 0}
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(event) + "\n")
            return source.latch_prefault_active_configuration_barrier()

    polls = iter((None, 1))
    processes = SimpleNamespace(
        records=[SimpleNamespace(process=SimpleNamespace(poll=lambda: next(polls)))]
    )
    backend = runtime.FocusedLaunchBackend(poll_interval_s=0)
    candidate = CandidateThenDrift()
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="process exited"):
        backend._wait_for_prefault_configuration(
            candidate, processes, deadline_monotonic=10**30
        )
    assert candidate.calls == 2


@pytest.mark.parametrize("mutation", ("timeout", "process-exit"))
def test_prefault_wait_fails_before_fault_when_no_exact_barrier(
    mutation: str,
) -> None:
    runtime = _runtime()
    calls = []

    class Source:
        def latch_prefault_active_configuration_barrier(self) -> None:
            calls.append("poll")
            return None

    returncode = 1 if mutation == "process-exit" else None
    processes = SimpleNamespace(
        records=[SimpleNamespace(process=SimpleNamespace(poll=lambda: returncode))]
    )
    backend = runtime.FocusedLaunchBackend(poll_interval_s=0)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        backend._wait_for_prefault_configuration(
            Source(),
            processes,
            deadline_monotonic=-1 if mutation == "timeout" else 10**30,
        )
    assert calls == ([] if mutation == "process-exit" else ["poll"])


@pytest.mark.parametrize("mutation", ("malformed", "spoofed"))
def test_prefault_tail_latch_rejects_malformed_or_spoofed_configuration(
    mutation: str, tmp_path: Path
) -> None:
    runtime = _runtime()
    profile = _fcrash_h_profile(N7_PROFILE_V3)
    root = tmp_path / "run"
    (root / "raw").mkdir(parents=True)
    if mutation == "malformed":
        payload = "not-json\n"
    else:
        payload = (
            json.dumps(
                {
                    "source_kind": "replica",
                    "source_id": "spoof",
                    "event_type": "adaptive.configuration_active",
                    "payload": {},
                }
            )
            + "\n"
        )
    (root / "raw" / "replica-0.jsonl").write_text(payload, encoding="utf-8")
    source = object.__new__(runtime.FocusedRawEvidenceSource)
    source._root = root
    source._profile = profile
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        source.latch_prefault_active_configuration_barrier()


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
        assert (
            source._postfault_authoritative_progress(
                events, fault_ns=100, prefault_ns=100, audit_ns=200
            )
            is not None
        )
    else:
        with pytest.raises(runtime.FocusedCrashPairRuntimeError):
            source._postfault_authoritative_progress(
                events, fault_ns=100, prefault_ns=100, audit_ns=200
            )


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong-proof-hash",
        "wrong-proof-tree",
        "extra-proof-key",
        "proof-epoch-bool",
        "historical-config-epoch-bool",
        "historical-config-tree-bool",
        "out-of-order",
        "view-generation-zero",
        "view-generation-noninteger",
        "view-generation-overflow",
        "view-generation-future",
        "view-generation-wrong-epoch-packed",
        "view-generation-wrong-existing",
        "activation-after-commit",
        "batch-negative",
        "batch-noninteger",
        "batch-overflow",
        "transactions-negative",
        "transactions-noninteger",
        "transactions-overflow",
        "transactions-unexpected-workload",
    ),
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
        target["payload"]["decision_proof"]["tree_id"] = 99
    elif mutation == "extra-proof-key":
        target["payload"]["decision_proof"]["extra"] = True
    elif mutation == "proof-epoch-bool":
        target["payload"]["decision_proof"]["epoch_number"] = False
    elif mutation == "historical-config-epoch-bool":
        next(
            event
            for event in events
            if event["event_type"] == "adaptive.configuration_active"
            and event["source_monotonic_ns"] < 100
        )["payload"]["epoch_number"] = False
    elif mutation == "historical-config-tree-bool":
        next(
            event
            for event in events
            if event["event_type"] == "adaptive.configuration_active"
            and event["source_monotonic_ns"] < 100
        )["payload"]["tree_id"] = False
    else:
        payload = target["payload"]
        if mutation == "out-of-order":
            target["source_sequence"] = 1
        elif mutation == "view-generation-zero":
            payload["view_generation"] = 0
        elif mutation == "view-generation-noninteger":
            payload["view_generation"] = "132"
        elif mutation == "view-generation-overflow":
            payload["view_generation"] = 1 << 64
        elif mutation == "view-generation-future":
            payload["view_generation"] = 13
        elif mutation == "view-generation-wrong-epoch-packed":
            payload["view_generation"] = (1 << 32) + 1
        elif mutation == "view-generation-wrong-existing":
            payload["view_generation"] = 6
        elif mutation == "activation-after-commit":
            target = next(
                event
                for event in events
                if event["event_type"] == "block.committed"
                and event["payload"]["view_generation"] == 12
            )
            activation = next(
                event
                for event in events
                if event["event_type"] == "adaptive.configuration_active"
                and event["payload"]["tree_id"] == 4
                and event["source_sequence"] == 14
            )
            activation["source_sequence"] = int(target["source_sequence"]) + 1
            activation["source_monotonic_ns"] = int(target["source_monotonic_ns"]) + 1
        elif mutation == "batch-negative":
            payload["commit_batch_index"] = -1
        elif mutation == "batch-noninteger":
            payload["commit_batch_index"] = "2"
        elif mutation == "batch-overflow":
            payload["commit_batch_index"] = 1 << 64
        elif mutation == "transactions-negative":
            payload["transaction_count"] = -1
        elif mutation == "transactions-noninteger":
            payload["transaction_count"] = "1000"
        elif mutation == "transactions-overflow":
            payload["transaction_count"] = 1 << 64
        else:
            payload["transaction_count"] = 5
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        source._postfault_authoritative_progress(
            events, fault_ns=100, prefault_ns=100, audit_ns=200
        )


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
        "missing-progress": lambda: snapshots["nonresponse"].pop("postfault_progress"),
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


def test_v8_state_machine_accepts_exactly_any_eleven_eligible_reporters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production gate accepts the v8 subset, but not ten or an outsider."""

    runtime = _runtime()
    original_profile = runtime.load_focused_profile(N31_PROFILE_V8)
    coverage = deepcopy(runtime.derive_reporter_coverage_plan(original_profile))
    profile = replace(
        original_profile,
        # The existing native transition fixture is fixed to 22/23/24.  Keep
        # its valid transitions while supplying a v8-shaped coverage plan.
        target_replica_ids=(22, 23, 24),
        issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
    )
    for row, target in zip(coverage["targets"], profile.target_replica_ids):
        row["target_replica_id"] = target
    monkeypatch.setattr(
        runtime, "derive_reporter_coverage_plan", lambda _profile: coverage
    )
    accepted = _fcrash_h_snapshots(profile, "A")
    hooks, _ = _arm_hooks(runtime, accepted)
    runtime._drive_arm_state_machine(profile, "A", "pair-01", hooks)

    target = str(profile.target_replica_ids[0])
    ten = _fcrash_h_snapshots(profile, "A")
    ten["nonresponse"]["qualifying_timeout_counts"][target].popitem()
    hooks, _ = _arm_hooks(runtime, ten)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="reporter coverage"):
        runtime._drive_arm_state_machine(profile, "A", "pair-01", hooks)


def test_v9_state_machine_accepts_full_guarded_cohort_and_recovered_dependents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V9 contains every guarded member while crash truth remains a strict subset."""

    runtime = _runtime()
    original_profile = runtime.load_focused_profile(N31_PROFILE_V9)
    coverage = deepcopy(runtime.derive_reporter_coverage_plan(original_profile))
    profile = replace(
        original_profile,
        target_replica_ids=(22, 23, 24),
        issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
    )
    for row, target in zip(coverage["targets"], profile.target_replica_ids):
        row["target_replica_id"] = target
    monkeypatch.setattr(
        runtime, "derive_reporter_coverage_plan", lambda _profile: coverage
    )
    snapshots = _fcrash_h_snapshots(profile, "A")
    cohort = (11, 12, 13, 14, 22, 23, 24)
    snapshots["nonresponse"]["detected_target_ids"] = list(cohort)
    for target in cohort:
        reporters = sorted(runtime._v9_eligible_guard_reporters(profile, target))
        assert len(reporters) >= 11
        snapshots["nonresponse"]["qualifying_timeout_counts"][str(target)] = {
            str(reporter): 2 for reporter in reporters[:11]
        }
        snapshots["nonresponse"]["guard_drawdowns"][str(target)] = -22
    snapshots["ranking"]["detected_target_ids"] = list(profile.target_replica_ids)
    ranked_ids = list(snapshots["ranking"]["ranked_ids"])
    containment_eligible = [
        replica for replica in profile.replica_ids if replica not in cohort
    ]
    baseline_roots = list(range(profile.quorum))
    preserved = {root for root in baseline_roots if root in containment_eligible}
    replacements = iter(
        sorted(replica for replica in containment_eligible if replica not in preserved)
    )
    epoch1_roots = [
        root if root in preserved else next(replacements) for root in baseline_roots
    ]
    epoch1_trees = _native_trees(
        replica_count=len(profile.replica_ids),
        quorum=profile.quorum,
        fanout=int(profile.raw["protocol"]["fanout"]),
        targets=cohort,
        roots=epoch1_roots,
    )
    prior_epoch1 = snapshots["epoch1"]["decoded"]
    epoch1_wire, epoch1 = _encode_signed_bundle(
        replica_count=len(profile.replica_ids),
        epoch_number=1,
        previous_digest=str(prior_epoch1["previous_epoch_digest"]),
        trees=epoch1_trees,
        evidence_snapshot_id=str(prior_epoch1["evidence_snapshot_id"]),
        evidence_cutoff=int(prior_epoch1["evidence_cutoff"]),
        nonce=16,
    )
    commands1, activations1 = _transition_snapshot(
        epoch1,
        epoch1_wire,
        tuple(
            replica
            for replica in profile.replica_ids
            if replica not in profile.target_replica_ids
        ),
        timestamp_ns=2_000_000_004,
        command_height=4,
    )
    snapshots["epoch1"] = {
        "native_bundle": epoch1_wire,
        "decoded": asdict(epoch1),
        "source_monotonic_ns": 2_000_000_003,
    }
    snapshots["commands1"] = {
        **commands1,
        "source_monotonic_ns": 2_000_000_004,
    }
    snapshots["activations1"] = {
        **activations1,
        "source_monotonic_ns": 2_000_000_005,
    }
    snapshots["commit1"] = {
        **snapshots["commit1"],
        "epoch_digest": epoch1.epoch_digest,
    }
    snapshots["ranking"]["predecessor_epoch_digest"] = epoch1.epoch_digest
    epoch2_roots = [
        replica for replica in ranked_ids if replica not in cohort
    ][: profile.quorum]
    epoch2_trees = _native_trees(
        replica_count=len(profile.replica_ids),
        quorum=profile.quorum,
        fanout=int(profile.raw["protocol"]["fanout"]),
        targets=cohort,
        roots=epoch2_roots,
    )
    epoch2_wire, epoch2 = _encode_signed_bundle(
        replica_count=len(profile.replica_ids),
        epoch_number=2,
        previous_digest=epoch1.epoch_digest,
        trees=epoch2_trees,
        evidence_snapshot_id="47" * 32,
        evidence_cutoff=29,
        nonce=17,
    )
    commands2, activations2 = _transition_snapshot(
        epoch2,
        epoch2_wire,
        tuple(
            replica
            for replica in profile.replica_ids
            if replica not in profile.target_replica_ids
        ),
        timestamp_ns=2_000_000_010,
        command_height=11,
    )
    snapshots["ranking"]["selected_root_ids"] = epoch2_roots
    snapshots["epoch2"] = {
        "native_bundle": epoch2_wire,
        "decoded": asdict(epoch2),
        "source_monotonic_ns": 2_000_000_009,
    }
    snapshots["commands2"] = {
        **commands2,
        "source_monotonic_ns": 2_000_000_010,
    }
    snapshots["activations2"] = {
        **activations2,
        "source_monotonic_ns": 2_000_000_011,
    }
    snapshots["commit2"] = {
        **snapshots["commit2"],
        "epoch_digest": epoch2.epoch_digest,
    }
    hooks, _ = _arm_hooks(runtime, snapshots)
    runtime._drive_arm_state_machine(profile, "A", "pair-01", hooks)

    promoted_dependent = deepcopy(snapshots)
    promoted_dependent["ranking"]["selected_root_ids"] = ranked_ids[
        : profile.quorum
    ]
    hooks, _ = _arm_hooks(runtime, promoted_dependent)
    with pytest.raises(
        runtime.FocusedCrashPairRuntimeError, match="membership-incomplete"
    ):
        runtime._drive_arm_state_machine(profile, "A", "pair-01", hooks)

    missing = deepcopy(snapshots)
    del missing["nonresponse"]["qualifying_timeout_counts"]["14"]
    hooks, _ = _arm_hooks(runtime, missing)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="guarded-cohort"):
        runtime._drive_arm_state_machine(profile, "A", "pair-01", hooks)

    new_epoch1_nonresponse = deepcopy(snapshots)
    new_epoch1_nonresponse["ranking"]["detected_target_ids"] = [
        11,
        12,
        13,
        14,
        16,
        22,
        23,
        24,
    ]
    new_epoch1_nonresponse["ranking"]["ranked_ids"].remove(16)
    hooks, _ = _arm_hooks(runtime, new_epoch1_nonresponse)
    with pytest.raises(
        runtime.FocusedCrashPairRuntimeError, match="membership-incomplete"
    ):
        runtime._drive_arm_state_machine(profile, "A", "pair-01", hooks)

    outsider = _fcrash_h_snapshots(profile, "A")
    counts = outsider["nonresponse"]["qualifying_timeout_counts"][str(target)]
    reporter, value = counts.popitem()
    assert reporter != "999"
    counts["999"] = value
    hooks, _ = _arm_hooks(runtime, outsider)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="guarded-cohort"):
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


def test_backend_atomic_fault_projection_preserves_pre_signal_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    plan = _fault_plan((0, 1))
    outcomes = (_outcome(0, 100), _outcome(1, 101))
    monkeypatch.setattr(
        runtime,
        "_execute_atomic_fault_batch",
        lambda *_args, **_kwargs: outcomes,
    )
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "fault-orchestrator.jsonl").write_text("{}\n", encoding="utf-8")
    configuration = {
        "profile": SimpleNamespace(
            replica_ids=tuple(range(7)),
            target_replica_ids=(0, 1),
        ),
        "fault_plan": plan,
        "run_directory": tmp_path,
    }
    processes_state = SimpleNamespace(
        registry=object(),
        lifecycle=object(),
        records=(),
    )

    projected = runtime.FocusedLaunchBackend().execute_atomic_fault_batch(
        configuration,
        processes_state,
    )

    assert projected["pre_signal_monotonic_ns"] == min(
        outcome.requested_monotonic_ns for outcome in outcomes
    )
    assert projected["source_monotonic_ns"] == max(
        outcome.confirmed_monotonic_ns for outcome in outcomes
    )
    receipt = json.loads((raw / "fault-receipt.json").read_text(encoding="utf-8"))
    assert projected["sigkill_outcomes"] == receipt["sigkill_outcomes"]


def test_fault_window_arm_publication_is_one_shot_and_hides_its_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime()
    target = tmp_path / "runtime" / "fault-window-arm.json"
    target.parent.mkdir()
    arm = {
        "schema_version": 1,
        "kind": "kauri-focused-fault-window-arm-v1",
        "run_id": "run-1",
        "profile_id": "n7-f2-q5-two-crash-pair-smoke-v4",
        "profile_sha256": "a" * 64,
        "topology_proof_sha256": "b" * 64,
        "request_sha256": "c" * 64,
        "epoch_number": 0,
        "epoch_digest": "d" * 64,
        "fault_receipt_sha256": "e" * 64,
        "evidence_start_monotonic_ns": 9,
        "prefault_tree_id": 6,
        "required_tree_positions": 2,
        "required_tree_ids": [6, 0],
    }
    linked: list[Path] = []
    original_link = runtime.os.link

    def inspect_then_link(source: Path, destination: Path) -> None:
        assert source.name.endswith(".tmp") and source.is_file()
        assert not destination.exists()
        linked.append(source)
        original_link(source, destination)

    monkeypatch.setattr(runtime.os, "link", inspect_then_link)
    digest = runtime._publish_fault_window_arm(target, arm)
    assert linked and target.is_file()
    assert not list(target.parent.glob("*.tmp"))
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="destination"):
        runtime._publish_fault_window_arm(target, arm)


def test_fault_window_arm_document_binds_finalized_sigkill_outcomes(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(
        N7_PROFILE_V3.with_name("n7-f2-q5-two-crash-pair-smoke-v4.json")
    )
    topology = profile.raw["topology"]
    barrier = [
        {
            "replica_id": replica,
            "configuration": {
                "epoch_number": 0,
                "tree_id": topology["active_tree_id"],
                "epoch_digest": topology["epoch_zero_digest"],
            },
        }
        for replica in profile.replica_ids
    ]
    receipt = {
        "schema_version": 1,
        "sigkill_outcomes": [
            {"confirmed_monotonic_ns": 100},
            {"confirmed_monotonic_ns": 101},
        ],
    }
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "fault-receipt.json").write_bytes(runtime._canonical_json(receipt))

    arm = runtime._fault_window_arm_document(
        {
            "profile": profile,
            "run_directory": tmp_path,
            "run_id": "run-v4",
            "parent_request_sha256": "a" * 64,
        },
        receipt,
        barrier,
    )

    assert arm["evidence_start_monotonic_ns"] == 101
    assert arm["required_tree_ids"] == [6, 0, 1, 2, 3, 4]


def test_v6_fault_window_arm_document_publishes_canonical_v2_bytes(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(N7_PROFILE_V6)
    topology = profile.raw["topology"]
    barrier = [
        {
            "replica_id": replica,
            "configuration": {
                "epoch_number": 0,
                "tree_id": topology["active_tree_id"],
                "epoch_digest": topology["epoch_zero_digest"],
            },
        }
        for replica in profile.replica_ids
    ]
    receipt = {
        "schema_version": 1,
        "sigkill_outcomes": [
            {"confirmed_monotonic_ns": 100},
            {"confirmed_monotonic_ns": 101},
        ],
    }
    raw = tmp_path / "raw"
    runtime_directory = tmp_path / "runtime"
    raw.mkdir()
    runtime_directory.mkdir()
    (raw / "fault-receipt.json").write_bytes(runtime._canonical_json(receipt))
    arm = runtime._fault_window_arm_document(
        {
            "profile": profile,
            "run_directory": tmp_path,
            "run_id": "run-v6",
            "parent_request_sha256": "a" * 64,
        },
        receipt,
        barrier,
    )
    target = runtime_directory / "fault-window-arm.json"
    digest = runtime._publish_fault_window_arm(target, arm)
    assert arm["schema_version"] == 2
    assert arm["kind"] == "kauri-focused-fault-window-arm-v2"
    assert arm["clock_domain"] == "same_host_clock_monotonic_raw"
    assert arm["required_observation_schema"] == 3
    assert arm["timeout_evidence_basis"] == "exact_timeout_attempt_id_v1"
    assert target.read_bytes() == runtime._canonical_json(arm)
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()


@pytest.mark.parametrize("mutation", ("unknown", "schema-bool", "observation-bool"))
def test_fault_window_arm_publication_rejects_v2_schema_and_type_drift(
    mutation: str, tmp_path: Path
) -> None:
    runtime = _runtime()
    target = (tmp_path / "runtime" / "fault-window-arm.json").resolve()
    target.parent.mkdir()
    arm: dict[str, object] = {
        "schema_version": 2,
        "kind": "kauri-focused-fault-window-arm-v2",
        "run_id": "run-v6",
        "profile_id": "n7-f2-q5-two-crash-pair-smoke-v6",
        "profile_sha256": "a" * 64,
        "topology_proof_sha256": "b" * 64,
        "request_sha256": "c" * 64,
        "epoch_number": 0,
        "epoch_digest": "d" * 64,
        "fault_receipt_sha256": "e" * 64,
        "evidence_start_monotonic_ns": 9,
        "prefault_tree_id": 6,
        "required_tree_positions": 2,
        "required_tree_ids": [6, 0],
        "clock_domain": "same_host_clock_monotonic_raw",
        "required_observation_schema": 3,
        "timeout_evidence_basis": "exact_timeout_attempt_id_v1",
    }
    if mutation == "unknown":
        arm["unexpected"] = None
    elif mutation == "schema-bool":
        arm["schema_version"] = True
    else:
        arm["required_observation_schema"] = True
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        runtime._publish_fault_window_arm(target, arm)
    assert not target.exists()


def test_v4_manager_binds_the_profile_tree_horizon(tmp_path: Path) -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(
        N7_PROFILE_V3.with_name("n7-f2-q5-two-crash-pair-smoke-v4.json")
    )
    adapter = runtime._profiled_adapter(profile, 41_719)
    tls = [{"sec": f"key-{index}", "crt": f"cert-{index}"} for index in range(8)]
    arm_path = tmp_path / "runtime" / "fault-window-arm.json"
    arm_path.parent.mkdir()
    argv = runtime._focused_manager_command(
        profile,
        adapter,
        arm="control",
        manager_binary=Path("/build/adaptation-manager"),
        tls=tls,
        issuer={"sec": "issuer-key", "pub": native_fixture.ISSUER_PUBLIC_KEY},
        run_directory=tmp_path,
        run_id="run-v4",
        source_instance="manager-v4",
        fault_window_arm_path=arm_path.resolve(),
        request_sha256="a" * 64,
    )
    pairs = dict(zip(argv[1::2], argv[2::2], strict=True))
    assert pairs["--fault-window-arm-required-tree-positions"] == "6"
    assert pairs["--fault-window-arm-path"] == str(arm_path.resolve())
    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="requires"):
        runtime._focused_manager_command(
            profile,
            adapter,
            arm="control",
            manager_binary=Path("/build/adaptation-manager"),
            tls=tls,
            issuer={"sec": "issuer-key", "pub": native_fixture.ISSUER_PUBLIC_KEY},
            run_directory=tmp_path,
            run_id="run-v4",
            source_instance="manager-v4",
            fault_window_arm_path=None,
            request_sha256="a" * 64,
        )


def test_v7_manager_launch_boundary_accepts_snapshot_basis(tmp_path: Path) -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(
        N7_PROFILE_V3.with_name("n7-f2-q5-two-crash-pair-smoke-v7.json")
    )
    adapter = runtime._profiled_adapter(profile, 41_719)
    tls = [{"sec": f"key-{index}", "crt": f"cert-{index}"} for index in range(8)]
    arm_path = tmp_path / "runtime" / "fault-window-arm.json"
    arm_path.parent.mkdir()
    argv = runtime._focused_manager_command(
        profile,
        adapter,
        arm="control",
        manager_binary=Path("/build/adaptation-manager"),
        tls=tls,
        issuer={"sec": "issuer-key", "pub": native_fixture.ISSUER_PUBLIC_KEY},
        run_directory=tmp_path,
        run_id="run-v7",
        source_instance="manager-v7",
        fault_window_arm_path=arm_path.resolve(),
        request_sha256="a" * 64,
    )
    manager_input = {
        "input_source": "normalized_manager_launch_boundary_v1",
        "requested_argv": list(argv),
        "observed_argv": list(argv),
        "stdin": "closed",
    }
    proof = runtime._validate_manager_launch_boundary(
        argv,
        argv,
        manager_input=manager_input,
        forbidden_values=(),
    )
    pairs = dict(zip(argv[1::2], argv[2::2], strict=True))
    assert pairs["--fault-window-arm-snapshot-evidence-basis"] == (
        "exact_post_fault_attempt_start_v1"
    )
    assert proof["blinded"] is True


def test_v9_arm_publication_and_manager_launch_bind_schema_four(tmp_path: Path) -> None:
    runtime = _runtime()
    profile = runtime.load_focused_profile(N7_PROFILE_V9)
    topology = profile.raw["topology"]
    barrier = [
        {
            "replica_id": replica,
            "configuration": {
                "epoch_number": 0,
                "tree_id": topology["active_tree_id"],
                "epoch_digest": topology["epoch_zero_digest"],
            },
        }
        for replica in profile.replica_ids
    ]
    receipt = {
        "schema_version": 1,
        "sigkill_outcomes": [
            {"confirmed_monotonic_ns": 100},
            {"confirmed_monotonic_ns": 101},
        ],
    }
    (tmp_path / "raw").mkdir()
    (tmp_path / "runtime").mkdir()
    (tmp_path / "raw" / "fault-receipt.json").write_bytes(
        runtime._canonical_json(receipt)
    )
    arm = runtime._fault_window_arm_document(
        {
            "profile": profile,
            "run_directory": tmp_path,
            "run_id": "run-v9",
            "parent_request_sha256": "a" * 64,
        },
        receipt,
        barrier,
    )
    published_path = (tmp_path / "runtime" / "fault-window-arm.json").resolve()
    runtime._publish_fault_window_arm(published_path, arm)
    assert arm["schema_version"] == 4
    assert arm["kind"] == "kauri-focused-fault-window-arm-v4"
    assert arm["selection_cardinality_policy"] == ("all_guarded_up_to_fault_bound_v1")

    launch_root = tmp_path / "launch"
    launch_arm = (launch_root / "runtime" / "fault-window-arm.json").resolve()
    launch_arm.parent.mkdir(parents=True)
    adapter = runtime._profiled_adapter(profile, 41_719)
    tls = [{"sec": f"key-{index}", "crt": f"cert-{index}"} for index in range(8)]
    argv = runtime._focused_manager_command(
        profile,
        adapter,
        arm="control",
        manager_binary=Path("/build/adaptation-manager"),
        tls=tls,
        issuer={"sec": "issuer-key", "pub": native_fixture.ISSUER_PUBLIC_KEY},
        run_directory=launch_root,
        run_id="run-v9",
        source_instance="manager-v9",
        fault_window_arm_path=launch_arm,
        request_sha256="a" * 64,
    )
    pairs = dict(zip(argv[1::2], argv[2::2], strict=True))
    assert pairs["--fault-window-arm-schema-version"] == "4"
    assert pairs["--fault-window-arm-domain"] == "kauri-focused-fault-window-arm-v4"
    assert pairs["--fault-window-arm-selection-cardinality-policy"] == (
        "all_guarded_up_to_fault_bound_v1"
    )
    runtime._validate_manager_launch_boundary(
        argv,
        argv,
        manager_input={
            "input_source": "normalized_manager_launch_boundary_v1",
            "requested_argv": list(argv),
            "observed_argv": list(argv),
            "stdin": "closed",
        },
        forbidden_values=(),
    )


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
            "pre_signal_monotonic_ns": 1_999,
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


def test_default_backend_cleanup_closes_every_writer_after_registry_failure(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    trace: list[str] = []
    failure = RuntimeError("registry cleanup failed")

    class Log:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            trace.append(f"close-{self.name}")

    class Evidence:
        def __exit__(self, *_args: object) -> None:
            trace.append("close-evidence")

    def cleanup_registry(_processes: object) -> tuple[object, ...]:
        trace.append("registry-cleanup")
        raise failure

    backend = runtime.FocusedLaunchBackend(cleanup_registry=cleanup_registry)
    processes = SimpleNamespace(
        registry=object(),
        records=(),
        logs=(Log("manager"), Log("client")),
        evidence=Evidence(),
    )

    with pytest.raises(RuntimeError) as raised:
        backend.cleanup({"run_directory": tmp_path}, processes)

    assert raised.value is failure
    assert trace == [
        "registry-cleanup",
        "close-manager",
        "close-client",
        "close-evidence",
    ]


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
