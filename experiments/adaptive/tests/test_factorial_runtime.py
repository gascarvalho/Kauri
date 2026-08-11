"""Pure runtime-contract tests for the frozen SHAPE25 factorial."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from experiments.adaptive import run_shape_factorial_campaign
from experiments.adaptive.kauri_experiment import factorial_execution
from experiments.adaptive.kauri_experiment import factorial_runtime
from experiments.adaptive.kauri_experiment import factorial_validation
from experiments.adaptive.kauri_experiment.factorial_manifest import (
    EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1,
    EXECUTION_CLEANUP_CONTRACT_V1,
    FactorialManifestError,
    FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2,
    PRECONTAINMENT_FAULT_COVERAGE_GATE_V1,
    PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1,
    PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1,
    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2,
    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V3,
    RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1,
    RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2,
    SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1,
    V13_PLAN_SHA256,
    V14_PLAN_SHA256,
    V10_PLAN_SHA256,
    V11_PLAN_SHA256,
    V12_PLAN_SHA256,
    V9_PLAN_SHA256,
    build_factorial_plan,
    load_frozen_manifest,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    ManagerSecretMaterial,
    V11_RUNTIME_SHA256,
    V11_SMOKE_RUNTIME_SHA256,
    V12_RUNTIME_SHA256,
    V12_SMOKE_RUNTIME_SHA256,
    V13_RUNTIME_SHA256,
    V13_SMOKE_RUNTIME_SHA256,
    V14_RUNTIME_SHA256,
    V14_SMOKE_RUNTIME_SHA256,
    V24_RUNTIME_SHA256,
    V24_SMOKE_RUNTIME_SHA256,
    build_factorial_runtime,
    build_slot_runtime,
    build_smoke_metadata,
    canonical_runtime_bytes,
    materialize_manager_argv,
    materialize_replica_argv,
    runtime_preflight,
)
from experiments.adaptive.kauri_experiment.factorial_validation import (
    V10_RUNTIME_SHA256,
    V10_SMOKE_RUNTIME_SHA256,
    V9_RUNTIME_SHA256,
    V9_SMOKE_RUNTIME_SHA256,
)

REPOSITORY = Path(__file__).resolve().parents[3]
MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v24.json"
)
V25_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v25.json"
)
V23_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v23.json"
)
V22_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v22.json"
)
V21_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v21.json"
)
V20_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v20.json"
)
V19_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v19.json"
)
V18_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v18.json"
)
V17_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v17.json"
)
V16_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v16.json"
)
V15_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v15.json"
)
V14_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v14.json"
)
V13_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v13.json"
)
V12_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v12.json"
)
V11_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v11.json"
)
V10_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v10.json"
)
V9_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v9.json"
)
V8_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v8.json"
)
V7_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v7.json"
)
V6_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v6.json"
)
V5_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v5.json"
)
V4_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v4.json"
)
V3_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v3.json"
)
V2_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v2.json"
)
LEGACY_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v1.json"
)


@pytest.fixture(scope="module")
def frozen_plan():
    return build_factorial_plan(load_frozen_manifest(MANIFEST_PATH))


@pytest.fixture(scope="module")
def runtime_plan(frozen_plan):
    return build_factorial_runtime(frozen_plan)


def _matched_slots(frozen_plan):
    block_id = frozen_plan.slots[0].block_id
    return tuple(slot for slot in frozen_plan.slots if slot.block_id == block_id)


def _option(argv: tuple[str, ...], name: str) -> str:
    assert argv.count(name) == 1
    return argv[argv.index(name) + 1]


def test_matched_arms_share_the_frozen_cutoff_and_transition_contracts(
    frozen_plan,
) -> None:
    slots = _matched_slots(frozen_plan)
    specs = tuple(build_slot_runtime(slot) for slot in slots)

    assert {spec.arm_code for spec in specs} == {"00", "P", "S", "PS"}
    assert len({spec.scientific_seed for spec in specs}) == 1
    assert len({spec.actor_ids for spec in specs}) == 1
    assert len({spec.tiered_cohorts for spec in specs}) == 1
    assert len({spec.fault_window for spec in specs}) == 1
    assert len({spec.cutoff_contract for spec in specs}) == 1
    assert len({spec.transition_sequence for spec in specs}) == 1
    assert specs[0].cutoff_contract.as_document() == {
        "actual_cutoffs_recorded_live": True,
        "actual_cutoff_validation_rule": "slot_local_monotonic_phase_order_v1",
        "baseline_bucket_count": slots[0].workload.baseline_bucket_count,
        "bucket_width_s": slots[0].workload.bucket_width_s,
        "epoch1_stable_bucket_count": slots[0].workload.epoch1_stable_bucket_count,
        "epoch2_stable_bucket_count": slots[0].workload.epoch2_stable_bucket_count,
        "evidence_window_rule": "fresh_exact_predecessor_after_common_commit",
        "fault_evidence_bucket_count": slots[0].workload.fault_evidence_bucket_count,
        "same_cutoff_rule_required": True,
    }
    assert specs[0].transition_sequence.transition_count == 2
    assert specs[0].transition_sequence.total_activation_overhead_blocks == (
        2 * slots[0].common_timers.activation_delay_blocks
    )
    tiered = specs[0].tiered_cohorts
    assert tiered is not None
    assert tiered.hard_actor_ids == slots[0].byzantine_actor_ids
    assert tiered.responsive_degraded_actor_ids == (
        slots[0].responsive_degraded_actor_ids
    )
    assert tiered.fast_replica_ids == slots[0].fast_replica_ids
    assert len((*tiered.hard_actor_ids, *tiered.responsive_degraded_actor_ids)) == (
        specs[0].f
    )
    assert len(tiered.fast_replica_ids) == specs[0].q
    assert tiered.max_omissions_per_proposal == specs[0].f
    assert tiered.responsive_omission_period == 41
    assert tiered.mode == "tiered_persistent_responsive_omission_v2"
    assert tiered.responsive_actor_schedule == (
        "omit_every_41st_unique_non_root_contribution_per_exact_"
        "epoch_identity_physical_role_stream_v1"
    )
    assert tiered.observer_isolation == (
        "replica_0_reserved_authoritative_commit_observer_v1"
    )
    assert tiered.pending_attempt_retention == (
        "retain_unanswered_exact_parent_child_attempt_across_consensus_commit_"
        "until_original_aggregation_derived_deadline_observational_only_no_"
        "consensus_authority_v1"
    )
    assert tiered.causal_timeout_linkage == (
        "fault_marker_to_exact_parent_attempt_to_scored_timeout_required_v1"
    )
    assert tiered.causal_timeout_provenance_window == (
        "epoch1_manager_ingestion_sequence_full_prefix_zero_exclusive_current_"
        "inclusive_v1"
    )
    assert tiered.causal_internal_witness_candidates == (
        "actor_level_strict_reporter_local_epoch1_internal_omit_aggregate_cross_"
        "commit_v1"
    )
    assert tiered.causal_selection_linkage_window == (
        "epoch1_manager_ingestion_sequence_baseline_exclusive_current_inclusive_v1"
    )
    assert tiered.marker_completeness_witness == (
        RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2
    )
    assert specs[0].cleanup_contract == EXECUTION_CLEANUP_CONTRACT_V1
    assert tiered.causal_timeout_eligibility == (
        RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V3
    )


def test_shape_invocation_is_common_and_only_application_boolean_varies(
    frozen_plan,
) -> None:
    specs = {
        spec.arm_code: spec
        for spec in map(build_slot_runtime, _matched_slots(frozen_plan))
    }
    without_apply = {
        spec.shape_invocation.selector_input_document() for spec in specs.values()
    }

    assert len(without_apply) == 1
    assert all(spec.shape_invocation.compute_live for spec in specs.values())
    assert specs["00"].shape_invocation.apply_selected_by_transition == (
        False,
        False,
    )
    assert specs["P"].shape_invocation.apply_selected_by_transition == (
        False,
        False,
    )
    assert specs["S"].shape_invocation.apply_selected_by_transition == (
        False,
        True,
    )
    assert specs["PS"].shape_invocation.apply_selected_by_transition == (
        False,
        True,
    )
    for spec in specs.values():
        document = spec.as_document()
        assert "selected_fanout" not in document
        assert "applied_fanout" not in document
        assert "ranking" not in document
        assert "trees" not in document
        assert "evidence_cutoffs" not in document


def test_factorial_placement_is_expressed_as_live_acceptance_predicates(
    frozen_plan,
) -> None:
    specs = {
        spec.arm_code: spec
        for spec in map(build_slot_runtime, _matched_slots(frozen_plan))
    }

    for spec in specs.values():
        assert spec.epoch1_placement.policy_intent == "fault_containment"
        assert spec.epoch1_placement.actors_are_wait_exempt_leaves is True
        assert spec.epoch1_placement.actor_truth_is_policy_input is False
        assert spec.epoch1_placement.only_hard_cohort_is_wait_exempt is True
        assert (
            spec.epoch1_placement.all_worse_replicas_are_physical_leaves is False
        )
        assert spec.epoch1_placement.root_and_internal_roles_are_fast_only is False
        assert spec.epoch1_placement.roots_equal_live_top_q_fast_replicas is False
        assert spec.epoch2_placement.actors_are_wait_exempt_leaves is True
        assert spec.epoch2_placement.actor_truth_is_policy_input is False
        assert spec.epoch2_placement.only_hard_cohort_is_wait_exempt is True
        assert spec.tiered_cohorts is not None
        assert spec.tiered_cohorts.hard_cohort_wait_exempt is True
        assert spec.tiered_cohorts.responsive_degraded_cohort_wait_exempt is False
        assert spec.tiered_cohorts.tiered_marker_schedule_required is True
        assert (
            spec.tiered_cohorts.responsive_degraded_rank_below_every_fast_replica
            is True
        )
        assert spec.tiered_cohorts.epoch1_responsive_degraded_are_roots is True
        assert (
            spec.tiered_cohorts.epoch1_responsive_degraded_internal_role_exposure_required
            is True
        )

    for code in ("00", "S"):
        assert specs[code].epoch2_placement.policy_intent == "fault_containment"
        assert (
            specs[code].epoch2_placement.roots_equal_live_highest_ranked_eligible
            is False
        )
        assert (
            specs[code].epoch2_placement.all_worse_replicas_are_physical_leaves
            is False
        )
        assert (
            specs[code].epoch2_placement.root_and_internal_roles_are_fast_only
            is False
        )
        assert (
            specs[code].epoch2_placement.roots_equal_live_top_q_fast_replicas
            is False
        )
    for code in ("P", "PS"):
        assert specs[code].epoch2_placement.policy_intent == (
            "performance_optimization"
        )
        assert (
            specs[code].epoch2_placement.roots_equal_live_highest_ranked_eligible
            is True
        )
        assert specs[code].epoch2_placement.influential_order_source == (
            "live_accepted_evidence_ranking"
        )
        assert (
            specs[code].epoch2_placement.all_worse_replicas_are_physical_leaves
            is True
        )
        assert (
            specs[code].epoch2_placement.root_and_internal_roles_are_fast_only
            is True
        )
        assert (
            specs[code].epoch2_placement.roots_equal_live_top_q_fast_replicas
            is True
        )


def test_causal_sequence_requires_independent_raw_actor_role_proof(
    frozen_plan,
) -> None:
    for spec in map(build_slot_runtime, _matched_slots(frozen_plan)):
        assert spec.causal_acceptance.as_document() == {
            "declared_or_synthetic_outcomes_accepted": False,
            "post_containment_actor_coverage_rule": "every_declared_actor",
            "post_containment_required_role": "leaf",
            "post_containment_wait_exempt": True,
            "pre_epoch1_actor_coverage_rule": (
                "each_declared_actor_has_source_bound_marker"
            ),
            "pre_epoch1_required_action": "omit_aggregate",
            "pre_epoch1_required_role": "internal",
            "precontainment_coverage_ready_event_type": (
                "adaptive_v2.fault_containment_coverage_ready"
            ),
            "precontainment_fault_coverage_gate": (
                PRECONTAINMENT_FAULT_COVERAGE_GATE_V1
            ),
            "precontainment_required_tree_coverage_rule": (
                "all_exact_predecessor_tree_ids_v1"
            ),
            "precontainment_shape_evaluation_contract": (
                PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1
            ),
            "precontainment_guarded_selection_contract": (
                PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1
            ),
            "future_tree_proposal_delivery_contract": (
                FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2
            ),
            "source_bound_proposal_witness_contract": (
                SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1
            ),
            "evidence_snapshot_selection_contract": (
                EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1
            ),
            "epoch1_preselection_residency_ms": 60_000,
            "minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection": 82,
            "proof_source": "independent_raw_artifact_validation",
        }


def test_manager_is_blinded_and_carries_two_live_transition_requests(
    frozen_plan,
) -> None:
    for slot in _matched_slots(frozen_plan):
        spec = build_slot_runtime(slot)
        manager = spec.manager_argv_template.argv

        assert _option(manager, "--shape-candidate-fanouts") == ",".join(
            map(str, slot.candidate_fanouts)
        )
        assert _option(manager, "--tree-fanout") == str(slot.initial_fanout)
        assert _option(manager, "--pipeline-stretch") == str(slot.pipeline_stretch)
        assert _option(manager, "--shape-deterministic-seed") == str(
            slot.scientific_seed
        )
        assert manager.count("--required-nonresponsive") == 1
        assert _option(manager, "--required-nonresponsive") == str(
            len(slot.byzantine_actor_ids)
        )
        assert "--shape-adaptation-enabled" not in manager
        assert manager.count("--transition-request") == 2
        assert manager.count("--bundle-output") == 2
        assert manager.count("--replica") == slot.replica_count
        assert not any("experiment-byzantine" in argument for argument in manager)
        assert not any("rotating-omission" in argument for argument in manager)
        requests = [
            json.loads(manager[index + 1])
            for index, argument in enumerate(manager)
            if argument == "--transition-request"
        ]
        assert [request["successor_epoch_number"] for request in requests] == [1, 2]
        assert [
            request["minimum_predecessor_residency_ms"] for request in requests
        ] == [
            0,
            60_000,
        ]
        assert [
            request["minimum_post_baseline_observation_ms"] for request in requests
        ] == [
            (
                slot.byzantine.start_after_prelaunch_anchor_s
                + slot.workload.fault_evidence_bucket_count
                * slot.workload.bucket_width_s
            )
            * 1_000,
            0,
        ]
        assert [request["apply_shape_selection"] for request in requests] == [
            False,
            slot.shape_adaptation,
        ]
        assert requests[0]["policy_intent"] == "fault_containment"
        assert requests[1]["policy_intent"] == (
            "performance_optimization"
            if slot.placement_adaptation
            else "fault_containment"
        )
        assert all(
            request["evidence_window_rule"]
            == "fresh_exact_predecessor_after_common_commit"
            for request in requests
        )
        for request in requests:
            if request["policy_intent"] == "fault_containment":
                assert request["containment_baseline_root_source"] == (
                    "live_predecessor_roots"
                )
                assert request["policy_parameters"] == {}
            else:
                assert "containment_baseline_root_source" not in request
                assert request["policy_parameters"] == {}
        policy = slot.responsiveness_policy
        assert _option(manager, "--responsiveness-policy-version") == (
            policy.policy_version
        )
        assert _option(manager, "--responsiveness-attempt-window") == str(
            policy.attempt_window
        )
        assert _option(manager, "--responsiveness-minimum-attempts") == str(
            policy.minimum_attempts
        )
        assert _option(manager, "--responsiveness-minimum-response-rate-ppm") == str(
            policy.minimum_response_rate_ppm
        )
        assert _option(manager, "--responsiveness-maximum-timeout-rate-ppm") == str(
            policy.maximum_timeout_rate_ppm
        )
        assert _option(manager, "--responsiveness-trailing-timeout-streak") == str(
            policy.trailing_timeout_streak
        )
        assert _option(
            manager, "--responsiveness-latency-percentile-basis-points"
        ) == str(policy.latency_percentile_basis_points)
        assert (
            1_000_000 // len(slot.byzantine_actor_ids) > policy.maximum_timeout_rate_ppm
        )


def test_every_slot_requires_the_frozen_three_nonresponsive_actors(
    frozen_plan,
) -> None:
    for slot in frozen_plan.slots:
        spec = build_slot_runtime(slot)
        assert len(spec.actor_ids) == 3
        assert (
            _option(
                spec.manager_argv_template.argv,
                "--required-nonresponsive",
            )
            == "3"
        )
    assert {
        replica_count: {
            build_slot_runtime(slot).tiered_cohorts.max_omissions_per_proposal  # type: ignore[union-attr]
            for slot in frozen_plan.slots
            if slot.replica_count == replica_count
        }
        for replica_count in (13, 22, 31)
    } == {13: {4}, 22: {7}, 31: {10}}


def test_manager_materialization_supplies_hex_and_absolute_slot_paths(
    frozen_plan, tmp_path: Path
) -> None:
    spec = build_slot_runtime(_matched_slots(frozen_plan)[0])
    slot_directory = tmp_path / spec.slot_id
    secrets = ManagerSecretMaterial(
        manager_tls_private_key_der_hex="a1b2",
        manager_tls_certificate_der_hex="c3d4",
        issuer_private_key_hex="11" * 32,
        replica_tls_certificate_der_hex=tuple(
            f"{replica_id + 1:02x}" for replica_id in range(spec.replica_count)
        ),
    )
    template_document = json.dumps(spec.as_document(), sort_keys=True)
    argv = materialize_manager_argv(
        spec,
        slot_directory,
        secrets,
        shared_raw_clock_anchor_ns=7_000_000_000,
    )

    assert str(tmp_path) not in template_document
    assert secrets.issuer_private_key_hex not in template_document
    assert not any("{{" in argument or "}}" in argument for argument in argv)
    assert _option(argv, "--tls-privkey") == secrets.manager_tls_private_key_der_hex
    assert _option(argv, "--tls-cert") == secrets.manager_tls_certificate_der_hex
    assert _option(argv, "--issuer-private-key") == secrets.issuer_private_key_hex
    bundle_outputs = tuple(
        argv[index + 1]
        for index, argument in enumerate(argv)
        if argument == "--bundle-output"
    )
    assert bundle_outputs == tuple(
        str(slot_directory / transition.bundle_relative_path)
        for transition in spec.transitions
    )
    assert all(Path(output).is_absolute() for output in bundle_outputs)
    requests = tuple(
        json.loads(argv[index + 1])
        for index, argument in enumerate(argv)
        if argument == "--transition-request"
    )
    for request, transition, bundle_output in zip(
        requests,
        spec.transitions,
        bundle_outputs,
    ):
        # The native parser requires canonical relative declarations, then
        # derives the exact absolute snapshot sibling from bundle-output.
        assert request["bundle_path"] == transition.bundle_relative_path
        assert request["evidence_snapshot_path"] == (
            transition.request.evidence_snapshot_path
        )
        assert Path(bundle_output).parent / "evidence-snapshot.json" == (
            slot_directory / transition.request.evidence_snapshot_path
        )


def test_containment_roots_are_late_bound_from_each_live_predecessor(
    frozen_plan,
) -> None:
    cells: set[tuple[int, int]] = set()
    for slot in frozen_plan.slots:
        cell = (slot.replica_count, slot.initial_fanout)
        if cell in cells or slot.arm_code not in ("00", "S"):
            continue
        cells.add(cell)
        spec = build_slot_runtime(slot)
        epoch1_roots = spec.transitions[0].request.containment_baseline_roots
        epoch2_roots = spec.transitions[1].request.containment_baseline_roots
        assert epoch1_roots == ()
        assert epoch2_roots == ()
        assert spec.transitions[0].request.containment_baseline_root_source == (
            "live_predecessor_roots"
        )
        assert spec.transitions[1].request.containment_baseline_root_source == (
            "live_predecessor_roots"
        )
    assert cells == {
        (replica_count, fanout)
        for replica_count in (13, 22, 31)
        for fanout in (2, 3, 5)
    }


def test_prelaunch_fault_window_is_mechanically_feasible(frozen_plan) -> None:
    for slot in frozen_plan.slots:
        spec = build_slot_runtime(slot)
        timers = slot.common_timers
        workload = slot.workload
        minimum_duration = (
            workload.fault_evidence_bucket_count * workload.bucket_width_s
            + 2 * timers.transition_convergence_deadline_s
            + workload.epoch1_stable_bucket_count * workload.bucket_width_s
            + workload.epoch2_stable_bucket_count * workload.bucket_width_s
            + timers.drain_margin_s
            + timers.schedule_slack_s
        )

        assert spec.fault_window.start_after_prelaunch_anchor_s == (
            timers.startup_timeout_s
            + workload.baseline_bucket_count * workload.bucket_width_s
        )
        assert spec.fault_window.duration_s >= minimum_duration
        assert spec.fault_window.schedule_slack_s >= 30
        assert spec.fault_window.transition_observation_bound_rule == (
            "shared_slot_hard_deadline_until_manager_selection_v1"
        )
        assert timers.hard_timeout_s >= (
            spec.fault_window.start_after_prelaunch_anchor_s
            + spec.fault_window.duration_s
            + timers.drain_margin_s
        )


def test_replica_argv_materialization_uses_one_shared_raw_clock_anchor(
    frozen_plan,
    tmp_path: Path,
) -> None:
    slot = _matched_slots(frozen_plan)[0]
    spec = build_slot_runtime(slot)
    slot_directory = tmp_path / spec.slot_id
    anchor = 987_654_321_000
    processes = materialize_replica_argv(spec, slot_directory, anchor)
    expected_start = anchor + slot.byzantine.start_after_prelaunch_anchor_s * 10**9
    expected_end = expected_start + slot.byzantine.duration_s * 10**9

    assert len(processes) == slot.replica_count
    assert len(spec.process_logs.kauri_fault_marker_relative_paths) == (
        2 * slot.replica_count
    )
    assert len(set(spec.process_logs.kauri_fault_marker_relative_paths)) == (
        2 * slot.replica_count
    )
    assert "window-start-monotonic-ns" not in json.dumps(spec.as_document())
    assert "window-end-monotonic-ns" not in json.dumps(spec.as_document())
    for process in processes:
        argv = process.argv
        assert not any("{{" in argument or "}}" in argument for argument in argv)
        config_paths = tuple(
            argv[index + 1]
            for index, argument in enumerate(argv)
            if argument == "--conf"
        )
        assert config_paths == (
            str(slot_directory / spec.main_config.path),
            str(slot_directory / f"runtime/replica-{process.replica_id}.conf"),
        )
        assert all(Path(path).is_absolute() for path in config_paths)
        assert _option(argv, "--structured-event-run-id") == spec.slot_id
        assert _option(argv, "--structured-event-output") == str(
            slot_directory / f"raw/replica-{process.replica_id}.jsonl"
        )
        assert _option(argv, "--experiment-byzantine-mode") == slot.byzantine.mode
        assert _option(argv, "--experiment-byzantine-window") == (
            f"{slot.block_id}-tiered-responsive-omission-v2"
        )
        assert _option(argv, "--experiment-rotating-omission-actors") == ",".join(
            map(str, slot.byzantine_actor_ids)
        )
        assert _option(
            argv, "--experiment-responsive-degraded-omission-actors"
        ) == ",".join(map(str, slot.responsive_degraded_actor_ids))
        assert _option(argv, "--experiment-responsive-omission-period") == "41"
        assert _option(argv, "--experiment-byzantine-window-start-monotonic-ns") == str(
            expected_start
        )
        assert _option(argv, "--experiment-byzantine-window-end-monotonic-ns") == str(
            expected_end
        )
        assert _option(
            argv, "--experiment-byzantine-max-omissions-per-proposal"
        ) == str(slot.f)
        assert _option(argv, "--experiment-rotating-omission-context-limit") == str(
            slot.byzantine.maximum_rotating_contexts
        )


def test_two_epoch_sequence_and_artifact_identity_are_deterministic(
    frozen_plan,
) -> None:
    first = build_slot_runtime(_matched_slots(frozen_plan)[0])
    second = build_slot_runtime(_matched_slots(frozen_plan)[0])

    assert first == second
    assert first.artifact_id == second.artifact_id
    assert first.transition_sequence.required_events == (
        "baseline_stable",
        "fault_window_open",
        "epoch1_command",
        "epoch1_activation",
        "epoch1_stable",
        "shape_v1_computed",
        "epoch2_command",
        "epoch2_activation",
        "epoch2_stable",
        "epoch2_drain_complete",
    )
    assert first.transition_sequence.required_events.count("epoch2_command") == 1
    assert first.transition_sequence.required_events.count("epoch2_activation") == 1
    assert first.transition_sequence.required_events.count("epoch2_stable") == 1
    assert first.transition_sequence.required_events.count("epoch2_drain_complete") == 1
    assert tuple(transition.successor_epoch for transition in first.transitions) == (
        1,
        2,
    )
    assert tuple(
        transition.request.minimum_predecessor_residency_ms
        for transition in first.transitions
    ) == (
        0,
        60_000,
    )
    assert tuple(
        transition.request.minimum_post_baseline_observation_ms
        for transition in first.transitions
    ) == (
        (
            frozen_plan.slots[0].byzantine.start_after_prelaunch_anchor_s
            + frozen_plan.slots[0].workload.fault_evidence_bucket_count
            * frozen_plan.slots[0].workload.bucket_width_s
        )
        * 1_000,
        0,
    )
    assert len({transition.artifact_id for transition in first.transitions}) == 2


def test_slot_artifact_identity_seals_tiered_cohorts_and_v13_evidence_rules(
    frozen_plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot = next(
        slot
        for slot in frozen_plan.slots
        if slot.replica_count == 31 and slot.arm_code == "00"
    )
    original = build_slot_runtime(slot)
    promoted = next(
        actor
        for actor in range(1, slot.q)
        if actor not in slot.responsive_degraded_actor_ids
    )
    changed_degraded = tuple(
        sorted((promoted, *slot.responsive_degraded_actor_ids[1:]))
    )
    changed_worse = set((*slot.byzantine_actor_ids, *changed_degraded))
    changed_slot = replace(
        slot,
        responsive_degraded_actor_ids=changed_degraded,
        fast_replica_ids=tuple(
            member
            for member in range(slot.replica_count)
            if member not in changed_worse
        ),
    )

    changed = build_slot_runtime(changed_slot)
    assert changed.artifact_id != original.artifact_id
    assert changed.tiered_cohorts != original.tiered_cohorts

    responsive = slot.byzantine.responsive_degradation
    assert responsive is not None
    changed_contract_slot = replace(
        slot,
        byzantine=replace(
            slot.byzantine,
            responsive_degradation=replace(
                responsive,
                marker_completeness_witness=None,
                causal_timeout_eligibility=None,
            ),
        ),
    )
    changed_contract = build_slot_runtime(changed_contract_slot)
    assert changed_contract.artifact_id != original.artifact_id
    assert changed_contract.tiered_cohorts != original.tiered_cohorts

    tiered = original.tiered_cohorts
    assert tiered is not None
    changed_schedule = replace(
        tiered,
        responsive_actor_schedule=f"{tiered.responsive_actor_schedule}-drift",
    )
    monkeypatch.setattr(
        factorial_runtime,
        "_tiered_cohort_contract",
        lambda _slot: changed_schedule,
    )
    schedule_changed = build_slot_runtime(slot)
    assert schedule_changed.artifact_id != original.artifact_id


def test_runtime_contract_has_no_duplicate_transition_fields_or_bare_config_lines(
    frozen_plan,
) -> None:
    source = inspect.getsource(factorial_runtime.TransitionContract)
    spec = build_slot_runtime(_matched_slots(frozen_plan)[0])

    assert source.count("artifact_id: str") == 1
    assert all(" = " in line for line in spec.main_config.lines)
    assert (
        sum(
            line.startswith("leader-progress-timeout = ")
            for line in spec.main_config.lines
        )
        == 1
    )


def test_campaign_runtime_is_execution_ordered_and_cannot_launch(
    frozen_plan,
    runtime_plan,
) -> None:
    assert tuple(slot.execution_ordinal for slot in runtime_plan.slots) == tuple(
        range(1, len(frozen_plan.slots) + 1)
    )
    assert tuple(slot.slot_id for slot in runtime_plan.slots) != tuple(
        slot.slot_id for slot in frozen_plan.slots
    )
    assert runtime_plan.execution_authorized is False
    assert runtime_plan.execution_receipt_required is True
    assert runtime_plan.launch_permitted is False
    assert runtime_plan.automatic_retries == 0
    assert runtime_plan.replacement_policy == "none"
    assert runtime_plan.outcome_dependent_order is False
    with pytest.raises(FactorialManifestError, match="not authorized|receipt"):
        runtime_plan.require_execution_authorized()

    encoded = canonical_runtime_bytes(runtime_plan)
    assert encoded == canonical_runtime_bytes(build_factorial_runtime(frozen_plan))
    assert hashlib.sha256(encoded).hexdigest() == V24_RUNTIME_SHA256
    smoke = factorial_execution.build_n7_ps_smoke_slot(frozen_plan.slots[0])
    smoke_payload = factorial_execution._canonical_json_bytes(
        smoke.runtime.as_document()
    )
    assert hashlib.sha256(smoke_payload).hexdigest() == V24_SMOKE_RUNTIME_SHA256
    document = json.loads(encoded)
    assert document["slot_count"] == len(frozen_plan.slots)
    assert "selected_fanout" not in document
    assert "ranking" not in document
    assert "window_start_monotonic_ns" not in document


def test_preflight_and_smoke_metadata_never_authorize_a_process(runtime_plan) -> None:
    preflight = runtime_preflight(
        runtime_plan,
        available_free_bytes=runtime_plan.minimum_free_bytes,
    )
    smoke = build_smoke_metadata()

    assert preflight["status"] == "PASS"
    assert preflight["launch_permitted"] is False
    assert preflight["automatic_retries"] == 0
    assert preflight["replacement_policy"] == "none"
    assert preflight["available_free_bytes"] == runtime_plan.minimum_free_bytes
    assert smoke.campaign_member is False
    assert smoke.figure_eligible is False
    assert smoke.denominator_contribution == 0
    assert smoke.launch_permitted is False
    assert "import subprocess" not in inspect.getsource(factorial_runtime)
    assert "import subprocess" not in inspect.getsource(run_shape_factorial_campaign)


def test_preflight_accepts_only_the_exact_n7_hard_one_smoke(
    frozen_plan,
    runtime_plan,
) -> None:
    smoke = factorial_execution.build_n7_ps_smoke_slot(frozen_plan.slots[0])
    smoke_plan = replace(runtime_plan, slots=(smoke.runtime,))

    result = runtime_preflight(
        smoke_plan,
        available_free_bytes=smoke_plan.minimum_free_bytes,
    )
    assert result["status"] == "PASS"
    assert smoke.runtime.tiered_cohorts is not None
    assert len(smoke.runtime.actor_ids) == 1
    assert len(smoke.runtime.tiered_cohorts.responsive_degraded_actor_ids) == 1
    assert smoke.runtime.tiered_cohorts.max_omissions_per_proposal == 2
    assert _option(
        smoke.runtime.manager_argv_template.argv,
        "--required-nonresponsive",
    ) == "1"

    wrong_hard = 5 if smoke.runtime.actor_ids != (5,) else 6
    wrong_worse = {
        wrong_hard,
        *smoke.runtime.tiered_cohorts.responsive_degraded_actor_ids,
    }
    wrong_tiered = replace(
        smoke.runtime.tiered_cohorts,
        hard_actor_ids=(wrong_hard,),
        fast_replica_ids=tuple(
            member
            for member in range(smoke.runtime.replica_count)
            if member not in wrong_worse
        ),
    )
    wrong_smoke = replace(
        smoke.runtime,
        actor_ids=(wrong_hard,),
        tiered_cohorts=wrong_tiered,
    )
    with pytest.raises(FactorialManifestError, match="tiered cohort"):
        runtime_preflight(
            replace(runtime_plan, slots=(wrong_smoke,)),
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )


def test_preflight_rejects_tiered_period_and_native_argv_drift(runtime_plan) -> None:
    slot = runtime_plan.slots[0]
    assert slot.tiered_cohorts is not None
    missing_cleanup_slot = replace(slot, cleanup_contract=None)
    with pytest.raises(FactorialManifestError, match="cleanup contract"):
        runtime_preflight(
            replace(
                runtime_plan,
                slots=(missing_cleanup_slot, *runtime_plan.slots[1:]),
            ),
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )

    bad_period_slot = replace(
        slot,
        tiered_cohorts=replace(
            slot.tiered_cohorts,
            responsive_omission_period=40,
        ),
    )
    with pytest.raises(FactorialManifestError, match="tiered cohort"):
        runtime_preflight(
            replace(runtime_plan, slots=(bad_period_slot, *runtime_plan.slots[1:])),
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )

    missing_measurement_slot = replace(
        slot,
        tiered_cohorts=replace(
            slot.tiered_cohorts,
            pending_attempt_retention=None,
            causal_timeout_linkage=None,
        ),
    )
    with pytest.raises(FactorialManifestError, match="tiered cohort"):
        runtime_preflight(
            replace(
                runtime_plan,
                slots=(missing_measurement_slot, *runtime_plan.slots[1:]),
            ),
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )

    process = slot.replica_argv_templates[0]
    renamed = tuple(
        "--experiment-responsive-degradation-actors"
        if argument == "--experiment-responsive-degraded-omission-actors"
        else argument
        for argument in process.argv
    )
    bad_argv_slot = replace(
        slot,
        replica_argv_templates=(
            replace(process, argv=renamed),
            *slot.replica_argv_templates[1:],
        ),
    )
    with pytest.raises(FactorialManifestError, match="tiered replica argv"):
        runtime_preflight(
            replace(runtime_plan, slots=(bad_argv_slot, *runtime_plan.slots[1:])),
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )


def test_preflight_rejects_an_early_epoch1_evaluation_hold(runtime_plan) -> None:
    slot = runtime_plan.slots[0]
    transition = slot.transitions[0]
    early_request = replace(
        transition.request,
        minimum_post_baseline_observation_ms=(
            transition.request.minimum_post_baseline_observation_ms - 1
        ),
    )
    early_slot = replace(
        slot,
        transitions=(replace(transition, request=early_request), slot.transitions[1]),
    )
    early_runtime = replace(
        runtime_plan,
        slots=(early_slot, *runtime_plan.slots[1:]),
    )

    with pytest.raises(FactorialManifestError, match="invalid transition contract"):
        runtime_preflight(
            early_runtime,
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )


def test_preflight_rejects_low_disk_and_output_namespace_collisions(
    runtime_plan,
) -> None:
    with pytest.raises(FactorialManifestError, match="minimum-free-bytes"):
        runtime_preflight(
            runtime_plan,
            available_free_bytes=runtime_plan.minimum_free_bytes - 1,
        )
    assert (
        runtime_preflight(
            runtime_plan,
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )["status"]
        == "PASS"
    )
    assert (
        runtime_preflight(
            runtime_plan,
            available_free_bytes=runtime_plan.minimum_free_bytes + 1,
        )["status"]
        == "PASS"
    )

    slot = runtime_plan.slots[0]
    duplicate_events = replace(
        slot.structured_events,
        exclusive_output_per_process=False,
        replica_source_instances=(slot.structured_events.replica_source_instances[0],)
        * slot.replica_count,
        replica_output_relative_paths=(
            slot.structured_events.replica_output_relative_paths[0],
        )
        * slot.replica_count,
    )
    bad_events = replace(
        runtime_plan,
        slots=(replace(slot, structured_events=duplicate_events),)
        + runtime_plan.slots[1:],
    )
    with pytest.raises(FactorialManifestError, match="structured-event"):
        runtime_preflight(
            bad_events,
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )

    colliding_logs = replace(
        slot.process_logs,
        exclusive_output_per_process=False,
        manager_stdout_relative_path=(
            slot.process_logs.replica_stdout_relative_paths[0]
        ),
    )
    bad_logs = replace(
        runtime_plan,
        slots=(replace(slot, process_logs=colliding_logs),) + runtime_plan.slots[1:],
    )
    with pytest.raises(FactorialManifestError, match="KAURI_FAULT"):
        runtime_preflight(
            bad_logs,
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )


@pytest.mark.parametrize("command", ("preflight", "run"))
def test_cli_v24_production_commands_are_validation_only(
    command: str,
    capsys,
) -> None:
    assert (
        run_shape_factorial_campaign.main(
            ["--manifest", str(MANIFEST_PATH), command]
        )
        == 2
    )
    refusal = json.loads(capsys.readouterr().err)
    assert refusal["status"] == "REJECT"
    assert "v1 through v36 are validation-only" in refusal["reason"]


@pytest.mark.parametrize(
    "prior_manifest",
    (
        V2_MANIFEST_PATH,
        V3_MANIFEST_PATH,
        V4_MANIFEST_PATH,
        V5_MANIFEST_PATH,
        V6_MANIFEST_PATH,
        V7_MANIFEST_PATH,
        V8_MANIFEST_PATH,
        V9_MANIFEST_PATH,
        V10_MANIFEST_PATH,
        V11_MANIFEST_PATH,
        V12_MANIFEST_PATH,
        V13_MANIFEST_PATH,
        V14_MANIFEST_PATH,
        V15_MANIFEST_PATH,
        V16_MANIFEST_PATH,
        V17_MANIFEST_PATH,
        V18_MANIFEST_PATH,
        V19_MANIFEST_PATH,
        V20_MANIFEST_PATH,
        V21_MANIFEST_PATH,
        V22_MANIFEST_PATH,
        V23_MANIFEST_PATH,
        MANIFEST_PATH,
        V25_MANIFEST_PATH,
    ),
)
def test_cli_defaults_to_v37_and_refuses_historical_production(
    prior_manifest: Path,
    capsys,
) -> None:
    assert run_shape_factorial_campaign.DEFAULT_MANIFEST.name == (
        "shape-placement-factorial-v37.json"
    )
    assert (
        run_shape_factorial_campaign.main(
            ["--manifest", str(prior_manifest), "plan"]
        )
        == 2
    )
    refusal = json.loads(capsys.readouterr().err)
    assert refusal["status"] == "REJECT"
    assert "v1 through v36 are validation-only" in refusal["reason"]


@pytest.mark.parametrize(
    "historical_manifest",
    (
        LEGACY_MANIFEST_PATH,
        V2_MANIFEST_PATH,
        V3_MANIFEST_PATH,
        V4_MANIFEST_PATH,
        V5_MANIFEST_PATH,
        V6_MANIFEST_PATH,
        V7_MANIFEST_PATH,
    ),
)
def test_historical_v1_through_v7_runtime_identities_remain_exact(
    historical_manifest: Path,
) -> None:
    manifest = load_frozen_manifest(historical_manifest)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    identity = factorial_validation._frozen_artifact_identity(manifest.manifest_id)
    encoded = canonical_runtime_bytes(runtime)

    assert plan.plan_sha256 == identity.plan_sha256
    assert hashlib.sha256(encoded).hexdigest() == identity.runtime_sha256
    document = json.loads(encoded)
    assert all("tiered_cohorts" not in slot for slot in document["slots"])
    assert all(
        "only_hard_cohort_is_wait_exempt" not in placement
        for slot in document["slots"]
        for placement in (slot["epoch1_placement"], slot["epoch2_placement"])
    )


def test_v8_tiered_runtime_identity_remains_exact_without_v9_fields() -> None:
    manifest = load_frozen_manifest(V8_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    encoded = canonical_runtime_bytes(runtime)

    assert plan.plan_sha256 == (
        "0f1d1c321109f795c03d658208d340fff5b38da7d74986996ed52bd7828a8598"
    )
    assert hashlib.sha256(encoded).hexdigest() == (
        "05b846c2fd9dc1005348993e147bb3e33def0011a7b52b3a68473ec79507e1a0"
    )
    smoke = factorial_execution.build_n7_ps_smoke_slot(plan.slots[0])
    smoke_payload = factorial_execution._canonical_json_bytes(
        smoke.runtime.as_document()
    )
    assert hashlib.sha256(smoke_payload).hexdigest() == (
        "bd0bf9291e4b34a229be6ce5a5e09ffe7a34964199a0ea21696ab750ddab8e0b"
    )
    document = json.loads(encoded)
    assert all(
        "pending_attempt_retention" not in slot["tiered_cohorts"]
        and "causal_timeout_linkage" not in slot["tiered_cohorts"]
        for slot in document["slots"]
    )


def test_v9_runtime_identities_remain_exact_without_v10_linkage_fields() -> None:
    manifest = load_frozen_manifest(V9_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    encoded = canonical_runtime_bytes(runtime)

    assert plan.plan_sha256 == V9_PLAN_SHA256
    assert hashlib.sha256(encoded).hexdigest() == V9_RUNTIME_SHA256
    smoke = factorial_execution.build_n7_ps_smoke_slot(plan.slots[0])
    smoke_payload = factorial_execution._canonical_json_bytes(
        smoke.runtime.as_document()
    )
    assert hashlib.sha256(smoke_payload).hexdigest() == V9_SMOKE_RUNTIME_SHA256
    document = json.loads(encoded)
    assert all(
        "causal_timeout_provenance_window" not in slot["tiered_cohorts"]
        and "causal_internal_witness_candidates" not in slot["tiered_cohorts"]
        and "causal_selection_linkage_window" not in slot["tiered_cohorts"]
        for slot in document["slots"]
    )


def test_v10_runtime_identity_remains_exact_without_v11_edge_fields() -> None:
    manifest = load_frozen_manifest(V10_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    encoded = canonical_runtime_bytes(runtime)

    assert plan.plan_sha256 == V10_PLAN_SHA256
    assert hashlib.sha256(encoded).hexdigest() == V10_RUNTIME_SHA256
    smoke = factorial_execution.build_n7_ps_smoke_slot(plan.slots[0])
    smoke_payload = factorial_execution._canonical_json_bytes(
        smoke.runtime.as_document()
    )
    assert hashlib.sha256(smoke_payload).hexdigest() == V10_SMOKE_RUNTIME_SHA256
    document = json.loads(encoded)
    assert all(
        "marker_completeness_witness" not in slot["tiered_cohorts"]
        and "causal_timeout_eligibility" not in slot["tiered_cohorts"]
        for slot in document["slots"]
    )


def test_v11_runtime_identities_remain_exact_without_v12_role_scoping() -> None:
    manifest = load_frozen_manifest(V11_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    encoded = canonical_runtime_bytes(runtime)

    assert plan.plan_sha256 == V11_PLAN_SHA256
    assert hashlib.sha256(encoded).hexdigest() == V11_RUNTIME_SHA256
    smoke = factorial_execution.build_n7_ps_smoke_slot(plan.slots[0])
    smoke_payload = factorial_execution._canonical_json_bytes(
        smoke.runtime.as_document()
    )
    assert hashlib.sha256(smoke_payload).hexdigest() == V11_SMOKE_RUNTIME_SHA256
    document = json.loads(encoded)
    assert all(
        slot["tiered_cohorts"]["mode"]
        == "tiered_persistent_responsive_omission_v1"
        and slot["tiered_cohorts"]["responsive_actor_schedule"]
        == (
            "omit_every_41st_unique_non_root_contribution_per_responsive_"
            "degraded_actor_v2"
        )
        for slot in document["slots"]
    )


def test_v12_runtime_identities_remain_exact_with_legacy_scoring_policy() -> None:
    manifest = load_frozen_manifest(V12_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    encoded = canonical_runtime_bytes(runtime)

    assert plan.plan_sha256 == V12_PLAN_SHA256
    assert hashlib.sha256(encoded).hexdigest() == V12_RUNTIME_SHA256
    smoke = factorial_execution.build_n7_ps_smoke_slot(plan.slots[0])
    smoke_payload = factorial_execution._canonical_json_bytes(
        smoke.runtime.as_document()
    )
    assert hashlib.sha256(smoke_payload).hexdigest() == V12_SMOKE_RUNTIME_SHA256
    document = json.loads(encoded)
    assert all(
        slot["responsiveness_policy"]["policy_version"]
        == "shape25-sensitive-responsiveness-v1"
        and slot["tiered_cohorts"]["mode"]
        == "tiered_persistent_responsive_omission_v2"
        for slot in document["slots"]
    )


def test_v13_runtime_identities_remain_exact_without_v14_contracts() -> None:
    manifest = load_frozen_manifest(V13_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    encoded = canonical_runtime_bytes(runtime)

    assert plan.plan_sha256 == V13_PLAN_SHA256
    assert hashlib.sha256(encoded).hexdigest() == V13_RUNTIME_SHA256
    smoke = factorial_execution.build_n7_ps_smoke_slot(plan.slots[0])
    smoke_payload = factorial_execution._canonical_json_bytes(
        smoke.runtime.as_document()
    )
    assert hashlib.sha256(smoke_payload).hexdigest() == V13_SMOKE_RUNTIME_SHA256
    document = json.loads(encoded)
    assert all(
        slot["tiered_cohorts"]["marker_completeness_witness"]
        == RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1
        and "cleanup_contract" not in slot
        for slot in document["slots"]
    )


def test_v14_runtime_identities_remain_exact_without_v15_coverage_gate() -> None:
    manifest = load_frozen_manifest(V14_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)

    assert plan.plan_sha256 == V14_PLAN_SHA256
    assert runtime.runtime_sha256 == V14_RUNTIME_SHA256
    smoke = factorial_execution.build_n7_ps_smoke_slot(plan.slots[0])
    assert hashlib.sha256(
        factorial_execution._canonical_json_bytes(smoke.runtime.as_document())
    ).hexdigest() == V14_SMOKE_RUNTIME_SHA256
    for slot in runtime.slots:
        assert "--fault-containment-evidence-start-monotonic-ns" not in (
            slot.manager_argv_template.argv
        )
        assert "--fault-containment-required-tree-coverage" not in (
            slot.manager_argv_template.argv
        )
        assert (
            slot.causal_acceptance.precontainment_fault_coverage_gate is None
        )
