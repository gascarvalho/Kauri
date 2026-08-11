"""Independent fail-closed validator tests for the frozen SHAPE25 campaign."""

from __future__ import annotations

from dataclasses import replace
import copy
import hashlib
import inspect
import json
import math
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment import factorial_manifest as manifest_module
from experiments.adaptive.kauri_experiment import factorial_runtime as runtime_module
from experiments.adaptive.kauri_experiment import factorial_validation as validation
from experiments.adaptive.kauri_experiment.factorial_manifest import (
    build_factorial_plan,
    canonical_plan_bytes,
    load_frozen_manifest,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    ManagerSecretMaterial,
    build_factorial_runtime,
    canonical_runtime_bytes,
    materialize_manager_argv,
    materialize_replica_argv,
)
from experiments.adaptive.kauri_experiment.factorial_validation import (
    FactorialValidationError,
    FaultMarker,
    ReplicaScore,
    Tree,
    derive_actor_ids,
    fnv1a_rotating_actor,
    validate_fault_causality,
    validate_manager_blinding,
    validate_schedule_document,
    validate_shape_decision,
    validate_slot,
    validate_throughput_document,
)

REPOSITORY = Path(__file__).resolve().parents[3]
MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v24.json"
)
V25_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v25.json"
)
V26_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v26.json"
)
V27_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v27.json"
)
V28_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v28.json"
)
V29_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v29.json"
)
V30_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v30.json"
)
V31_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v31.json"
)
V32_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v32.json"
)
V33_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v33.json"
)
V34_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v34.json"
)
V35_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v35.json"
)
V36_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v36.json"
)
V37_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v37.json"
)
V38_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v38.json"
)
FROZEN_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v39.json"
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
FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT = (
    "exact_immediate_successor_tree_proposals_relayed_and_buffered_without_pre_"
    "activation_protocol_effects_then_revalidated_and_replayed_once_after_exact_"
    "activation_v1"
)
FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2 = (
    "exact_nonwrapping_same_epoch_tree_count_minus_one_future_proposal_horizon_"
    "is_capacity_bounded_relayed_and_buffered_without_pre_activation_protocol_"
    "effects_then_revalidated_and_replayed_once_after_each_exact_activation_v2"
)
INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT = (
    "exact_selected_wait_exempt_replicas_are_excluded_from_root_and_internal_"
    "assignment_and_placed_as_leaves_in_every_successor_tree_v1"
)
VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT = (
    "exact_duplicate_verified_child_response_is_idempotent_and_cannot_fail_"
    "the_response_deadline_or_suppress_later_convergence_observations_v1"
)
VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2 = (
    "exact_duplicate_verified_child_response_is_idempotent_guard_marker_follows_"
    "completed_accepted_ingress_and_cannot_fail_the_response_deadline_or_suppress_"
    "later_convergence_observations_v2"
)
EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT = (
    "exact_excluded_repair_smoke_epoch1_selection_terminal_all_replica_command_"
    "activation_and_stable_end_before_fault_end_then_fault_end_before_cycle1_"
    "selection_then_epoch2_terminal_all_replica_command_activation_stable_end_"
    "and_drain_before_shared_hard_deadline_v1"
)
EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2 = (
    "exact_excluded_repair_smoke_fault_evidence_and_epoch1_stable_are_the_only_"
    "fault_active_causal_phases_with_epoch1_selection_terminal_all_replica_"
    "command_activation_and_stable_end_before_fault_end_then_epoch2_is_post_"
    "fault_recovery_and_stability_with_fault_end_before_cycle1_selection_then_"
    "epoch2_terminal_all_replica_command_activation_stable_end_and_drain_before_"
    "shared_hard_deadline_v2"
)
EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3 = (
    "exact_excluded_repair_smoke_source_fault_window_duration_450s_effective_"
    "fault_window_duration_330s_hard_deadline_650s_with_fault_evidence_and_"
    "epoch1_stable_as_the_only_fault_active_causal_phases_with_epoch1_selection_"
    "terminal_all_replica_command_activation_and_stable_end_before_fault_end_"
    "then_epoch2_as_post_fault_recovery_and_stability_with_fault_end_before_"
    "cycle1_selection_then_epoch2_terminal_all_replica_command_activation_"
    "stable_end_and_drain_before_shared_hard_deadline_v3"
)
EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4 = (
    "exact_excluded_repair_smoke_source_fault_window_duration_450s_effective_"
    "fault_window_duration_330s_hard_deadline_650s_post_fault_cycle1_selection_"
    "observation_grace_5s_with_exact_cycle1_manager_selection_gate_bound_to_the_"
    "materialized_shared_clock_fault_end_plus_the_frozen_observation_grace_as_"
    "an_exclusive_lower_bound_with_fault_evidence_and_epoch1_stable_as_the_only_"
    "fault_active_causal_phases_with_epoch1_selection_terminal_all_replica_"
    "command_activation_and_stable_end_before_fault_end_then_epoch2_as_post_"
    "fault_recovery_and_stability_with_fault_end_before_cycle1_selection_and_"
    "all_replica_commands_then_epoch2_terminal_all_replica_activation_stable_"
    "end_and_drain_before_shared_hard_deadline_v4"
)
EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2 = (
    "byzantine.window.duration_s:450->330"
)
EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V3 = (
    "byzantine.window.duration_s:450->330;"
    "cycle1.selection.exclusive_lower_bound:unset->"
    "effective_fault_window_end_plus_5s"
)
CYCLE1_SELECTION_NOT_BEFORE_OPTION = (
    "--cycle-1-selection-not-before-monotonic-ns"
)
CYCLE1_SELECTION_NOT_BEFORE_TOKEN = (
    "{{cycle_1_selection_not_before_monotonic_ns}}"
)
EXCLUDED_REPAIR_SMOKE_VERIFIED_RESPONSE_DUPLICATE_PROBE_CONTRACT = (
    "exact_excluded_repair_smoke_at_least_one_post_fault_epoch1_internal_"
    "responsive_child_response_is_replayed_only_into_response_evidence_bridge_"
    "after_consensus_acceptance_and_first_evidence_record_with_at_most_one_probe_"
    "per_reporter_v1"
)
RESPONSE_DUPLICATE_PROBE_MODE = (
    "exact_once_post_fault_epoch1_responsive_internal_child_v1"
)


def test_v28_repair_contract_literals_are_exact() -> None:
    assert validation.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V1 == (
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT
    )
    assert (
        validation.EXCLUDED_REPAIR_SMOKE_VERIFIED_RESPONSE_DUPLICATE_PROBE_CONTRACT_V1
        == EXCLUDED_REPAIR_SMOKE_VERIFIED_RESPONSE_DUPLICATE_PROBE_CONTRACT
    )
    assert validation.RESPONSE_DUPLICATE_PROBE_MODE_V1 == RESPONSE_DUPLICATE_PROBE_MODE
    assert validation.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2 == (
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
    )
    assert validation.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3 == (
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
    )
    assert validation.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4 == (
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
    )
    assert runtime_module.EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V3 == (
        EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V3
    )
    assert runtime_module.EXCLUDED_REPAIR_SMOKE_POST_FAULT_OBSERVATION_GRACE_S == 5
    assert runtime_module.CYCLE1_SELECTION_NOT_BEFORE_OPTION == (
        CYCLE1_SELECTION_NOT_BEFORE_OPTION
    )
    assert runtime_module.CYCLE1_SELECTION_NOT_BEFORE_TOKEN == (
        CYCLE1_SELECTION_NOT_BEFORE_TOKEN
    )
    assert validation._EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V3 == (
        EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V3
    )
    assert validation._V39_POST_FAULT_SELECTION_OBSERVATION_GRACE_S == 5


def test_v39_preserves_strict_v9_and_v2_identity_validator_source() -> None:
    expected_hashes = {
        "_validate_v9_cross_commit_retention_witnesses": (
            "80be9290ca1dd7347c8964c807c6397b2fac4658b1f3de0777e2e9439b73cc4e"
        ),
        "_validate_manifest_commit_identity_unavailable": (
            "021f9c3c782f6d099dc515669729af3498b20acf451cafe161f0fa7e196ef0cb"
        ),
        "_validate_manifest_commit_identity_evidence": (
            "efc31b5f2b2b665f90d6aa7187c6753f9908ab974746ed1c42480d6f582b4050"
        ),
    }

    for name, expected_hash in expected_hashes.items():
        source = inspect.getsource(getattr(validation, name)).encode("utf-8")
        assert hashlib.sha256(source).hexdigest() == expected_hash


def test_v38_history_and_v39_cycle1_gate_dispatch_are_exact() -> None:
    repair_slot = "slot-037-n31-f2-b04-00"
    primary_slot = "slot-066-n31-f5-b05-P"

    assert validation._expected_excluded_repair_observation_contract(
        validation.V38_MANIFEST_ID
    ) == EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
    assert validation._expected_excluded_repair_observation_contract(
        validation.FROZEN_MANIFEST_ID
    ) == EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
    assert not validation._uses_excluded_repair_cycle1_selection_gate(
        manifest_id=validation.V38_MANIFEST_ID,
        coverage_smoke=True,
        slot_id=repair_slot,
    )
    assert validation._uses_excluded_repair_cycle1_selection_gate(
        manifest_id=validation.FROZEN_MANIFEST_ID,
        coverage_smoke=True,
        slot_id=repair_slot,
    )
    assert not validation._uses_excluded_repair_cycle1_selection_gate(
        manifest_id=validation.FROZEN_MANIFEST_ID,
        coverage_smoke=False,
        slot_id=repair_slot,
    )
    assert not validation._uses_excluded_repair_cycle1_selection_gate(
        manifest_id=validation.FROZEN_MANIFEST_ID,
        coverage_smoke=True,
        slot_id=primary_slot,
    )


def test_v26_and_v27_duplicate_delivery_dispatch_is_version_and_field_exact() -> None:
    def manifest(manifest_id: str, value: str | None) -> SimpleNamespace:
        responsive = SimpleNamespace()
        if value is not None:
            responsive.verified_response_duplicate_delivery_contract = value
        return SimpleNamespace(
            manifest_id=manifest_id,
            byzantine=SimpleNamespace(responsive_degradation=responsive),
        )

    assert validation.VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V1 == (
        VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT
    )
    assert validation.VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2 == (
        VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2
    )
    assert validation._uses_verified_response_duplicate_delivery_contract(
        manifest(
            validation.V26_MANIFEST_ID,
            VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT,
        )
    )
    assert validation._uses_verified_response_duplicate_delivery_contract(
        manifest(
            validation.V29_MANIFEST_ID,
            VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2,
        )
    )
    assert validation._uses_verified_response_duplicate_delivery_contract(
        manifest(
            validation.V30_MANIFEST_ID,
            VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2,
        )
    )
    assert validation._uses_verified_response_duplicate_delivery_contract(
        manifest(
            validation.V32_MANIFEST_ID,
            VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2,
        )
    )
    assert validation._uses_verified_response_duplicate_delivery_contract(
        manifest(
            validation.V37_MANIFEST_ID,
            VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2,
        )
    )
    assert validation._uses_verified_response_duplicate_delivery_contract(
        manifest(
            validation.FROZEN_MANIFEST_ID,
            VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2,
        )
    )
    assert not validation._uses_verified_response_duplicate_delivery_contract(
        manifest(
            validation.V25_MANIFEST_ID,
            VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT,
        )
    )
    assert not validation._uses_verified_response_duplicate_delivery_contract(
        manifest(validation.V26_MANIFEST_ID, None)
    )
    assert not validation._uses_verified_response_duplicate_delivery_contract(
        manifest(
            validation.V26_MANIFEST_ID,
            f"{VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT}-drift",
        )
    )
    assert not validation._uses_verified_response_duplicate_delivery_contract(
        manifest(
            validation.FROZEN_MANIFEST_ID,
            VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT,
        )
    )
    assert not validation._uses_verified_response_duplicate_delivery_contract(
        manifest(
            validation.V26_MANIFEST_ID,
            VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2,
        )
    )


def test_v23_future_tree_delivery_dispatch_is_version_and_field_exact() -> None:
    def manifest(manifest_id: str, value: str | None) -> SimpleNamespace:
        responsive = SimpleNamespace()
        if value is not None:
            responsive.future_tree_proposal_delivery_contract = value
        return SimpleNamespace(
            manifest_id=manifest_id,
            byzantine=SimpleNamespace(responsive_degradation=responsive),
        )

    assert validation.FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2 == (
        FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2
    )
    assert validation._uses_future_tree_proposal_delivery_contract(
        manifest(validation.V22_MANIFEST_ID, FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT)
    )
    assert validation._uses_future_tree_proposal_delivery_contract(
        manifest(
            validation.V23_MANIFEST_ID,
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2,
        )
    )
    assert validation._uses_future_tree_proposal_delivery_contract(
        manifest(
            validation.V30_MANIFEST_ID,
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2,
        )
    )
    assert validation._uses_future_tree_proposal_delivery_contract(
        manifest(
            validation.V32_MANIFEST_ID,
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2,
        )
    )
    assert validation._uses_future_tree_proposal_delivery_contract(
        manifest(
            validation.V37_MANIFEST_ID,
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2,
        )
    )
    assert validation._uses_future_tree_proposal_delivery_contract(
        manifest(
            validation.FROZEN_MANIFEST_ID,
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2,
        )
    )
    assert not validation._uses_future_tree_proposal_delivery_contract(
        manifest(validation.V22_MANIFEST_ID, FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2)
    )
    assert not validation._uses_future_tree_proposal_delivery_contract(
        manifest(validation.V23_MANIFEST_ID, FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT)
    )
    assert not validation._uses_future_tree_proposal_delivery_contract(
        manifest(validation.V23_MANIFEST_ID, None)
    )
    assert not validation._uses_future_tree_proposal_delivery_contract(
        manifest(validation.V18_MANIFEST_ID, None)
    )


def test_v23_responsive_omission_period_remains_41_after_v24_dispatch() -> None:
    assert validation._expected_responsive_omission_period(
        "shape-placement-factorial-v23"
    ) == 41
    assert validation._expected_responsive_omission_period(
        "shape-placement-factorial-v24"
    ) == 41
    assert validation._expected_responsive_omission_period(
        "shape-placement-factorial-v25"
    ) == 41


def test_v25_inherited_wait_exempt_placement_dispatch_is_version_and_field_exact() -> None:
    def manifest(manifest_id: str, value: str | None) -> SimpleNamespace:
        responsive = SimpleNamespace()
        if value is not None:
            responsive.inherited_consensus_wait_exempt_placement_contract = value
        return SimpleNamespace(
            manifest_id=manifest_id,
            byzantine=SimpleNamespace(responsive_degradation=responsive),
        )

    assert validation.INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT_V1 == (
        INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT
    )
    assert validation._uses_inherited_consensus_wait_exempt_placement_contract(
        manifest(
            validation.V25_MANIFEST_ID,
            INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT,
        )
    )
    assert validation._uses_inherited_consensus_wait_exempt_placement_contract(
        manifest(
            validation.V30_MANIFEST_ID,
            INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT,
        )
    )
    assert validation._uses_inherited_consensus_wait_exempt_placement_contract(
        manifest(
            validation.V32_MANIFEST_ID,
            INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT,
        )
    )
    assert validation._uses_inherited_consensus_wait_exempt_placement_contract(
        manifest(
            validation.FROZEN_MANIFEST_ID,
            INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT,
        )
    )
    assert not validation._uses_inherited_consensus_wait_exempt_placement_contract(
        manifest(validation.V24_MANIFEST_ID, None)
    )
    assert not validation._uses_inherited_consensus_wait_exempt_placement_contract(
        manifest(
            validation.V24_MANIFEST_ID,
            INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT,
        )
    )
    assert not validation._uses_inherited_consensus_wait_exempt_placement_contract(
        manifest(validation.FROZEN_MANIFEST_ID, None)
    )
    assert not validation._uses_inherited_consensus_wait_exempt_placement_contract(
        manifest(
            validation.FROZEN_MANIFEST_ID,
            f"{INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT}-drift",
        )
    )


def test_v36_post_final_unmatched_commit_contract_dispatch_is_exact() -> None:
    contract = (
        validation.POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
    )
    assert contract == (
        "exact_v36_non_designated_legal_qc_skipped_ancestor_without_authenticated_"
        "exact_identity_source_authoritative_commit_identity_absence_strictly_"
        "after_final_cycle_successor_converged_terminal_is_scoped_only_when_each_"
        "gap_has_one_same_source_native_marker_every_replica_has_exactly_one_"
        "matching_commit_observed_the_designated_observer_stream_is_complete_and_"
        "at_least_derived_q_distinct_source_bound_rich_block_committed_proofs_"
        "match_height_hash_parent_transaction_count_commit_batch_index_epoch_tree_"
        "digest_and_view_generation_without_synthesizing_commit_evidence_or_"
        "changing_consensus_or_throughput_authority_v1"
    )

    def manifest(manifest_id: str, value: str | None) -> SimpleNamespace:
        responsive = SimpleNamespace(
            post_final_convergence_unmatched_commit_evidence_contract=value,
        )
        return SimpleNamespace(
            manifest_id=manifest_id,
            byzantine=SimpleNamespace(responsive_degradation=responsive),
        )

    assert validation._uses_post_final_convergence_unmatched_commit_evidence_contract(
        manifest(validation.V36_MANIFEST_ID, contract)
    )
    assert validation._uses_post_final_convergence_unmatched_commit_evidence_contract(
        manifest(validation.V37_MANIFEST_ID, contract)
    )
    assert not validation._uses_post_final_convergence_unmatched_commit_evidence_contract(
        manifest(validation.V35_MANIFEST_ID, contract)
    )
    assert not validation._uses_post_final_convergence_unmatched_commit_evidence_contract(
        manifest(validation.V36_MANIFEST_ID, None)
    )
    assert not validation._uses_post_final_convergence_unmatched_commit_evidence_contract(
        manifest(validation.V37_MANIFEST_ID, contract + "-forged")
    )


def test_v38_post_final_unmatched_commit_contract_dispatch_is_exact() -> None:
    v1 = validation.POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
    v2 = validation.POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V2
    assert v2 == (
        "exact_v38_legal_qc_skipped_ancestor_without_authenticated_exact_identity_"
        "source_authoritative_commit_identity_absence_strictly_after_final_cycle_"
        "successor_converged_terminal_is_scoped_only_when_each_gap_has_one_same_"
        "source_native_marker_every_replica_has_exactly_one_matching_commit_"
        "observed_and_at_least_derived_q_distinct_source_bound_rich_block_"
        "committed_proofs_match_height_hash_parent_transaction_count_commit_batch_"
        "index_epoch_tree_digest_and_view_generation_non_designated_gaps_require_"
        "the_designated_observer_rich_proof_while_at_most_one_designated_observer_"
        "gap_is_permitted_only_for_zero_transactions_with_exact_height_adjacent_"
        "designated_observer_rich_predecessor_and_successor_parent_chain_and_"
        "configuration_generation_closure_every_positive_transaction_designated_"
        "observer_observation_remains_complete_without_synthesizing_commit_"
        "evidence_or_changing_consensus_or_transaction_throughput_authority_v2"
    )

    def manifest(manifest_id: str, value: str | None) -> SimpleNamespace:
        responsive = SimpleNamespace(
            post_final_convergence_unmatched_commit_evidence_contract=value,
        )
        return SimpleNamespace(
            manifest_id=manifest_id,
            byzantine=SimpleNamespace(responsive_degradation=responsive),
        )

    assert validation._uses_post_final_convergence_unmatched_commit_evidence_contract(
        manifest(validation.V37_MANIFEST_ID, v1)
    )
    assert validation._uses_post_final_convergence_unmatched_commit_evidence_contract(
        manifest(validation.V38_MANIFEST_ID, v2)
    )
    assert validation._uses_post_final_convergence_unmatched_commit_evidence_contract(
        manifest(validation.FROZEN_MANIFEST_ID, v2)
    )
    assert not validation._uses_post_final_convergence_unmatched_commit_evidence_contract(
        manifest(validation.V37_MANIFEST_ID, v2)
    )
    assert not validation._uses_post_final_convergence_unmatched_commit_evidence_contract(
        manifest(validation.V38_MANIFEST_ID, v1)
    )
    assert not validation._uses_post_final_convergence_unmatched_commit_evidence_contract(
        manifest(validation.FROZEN_MANIFEST_ID, v1)
    )
    assert not validation._uses_post_final_convergence_unmatched_commit_evidence_contract(
        manifest(validation.FROZEN_MANIFEST_ID, v2 + "-forged")
    )


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _v25_candidate_manifest(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = V25_MANIFEST_PATH.read_bytes()
    semantic = _canonical(json.loads(payload))
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_SEMANTIC_SHA256",
        hashlib.sha256(semantic).hexdigest(),
    )
    return manifest_module.parse_manifest_bytes(payload)


def _v26_candidate_manifest(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = V26_MANIFEST_PATH.read_bytes()
    return manifest_module.parse_manifest_bytes(payload)


def _v27_candidate_manifest(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = V27_MANIFEST_PATH.read_bytes()
    return manifest_module.parse_manifest_bytes(payload)


def _v28_candidate_manifest(
    _monkeypatch: pytest.MonkeyPatch,
):
    payload = V28_MANIFEST_PATH.read_bytes()
    return manifest_module.parse_manifest_bytes(payload)


def _v29_candidate_manifest(
    _monkeypatch: pytest.MonkeyPatch,
):
    payload = V29_MANIFEST_PATH.read_bytes()
    return manifest_module.parse_manifest_bytes(payload)


def _v30_candidate_manifest(
    _monkeypatch: pytest.MonkeyPatch,
):
    payload = V30_MANIFEST_PATH.read_bytes()
    return manifest_module.parse_manifest_bytes(payload)


def _v31_candidate_manifest(
    _monkeypatch: pytest.MonkeyPatch,
):
    payload = V31_MANIFEST_PATH.read_bytes()
    return manifest_module.parse_manifest_bytes(payload)


def _v32_candidate_manifest(
    _monkeypatch: pytest.MonkeyPatch,
):
    payload = V32_MANIFEST_PATH.read_bytes()
    return manifest_module.parse_manifest_bytes(payload)


def _v33_candidate_manifest(
    _monkeypatch: pytest.MonkeyPatch,
):
    payload = V33_MANIFEST_PATH.read_bytes()
    return manifest_module.parse_manifest_bytes(payload)


def _v34_candidate_manifest(
    _monkeypatch: pytest.MonkeyPatch,
):
    payload = V34_MANIFEST_PATH.read_bytes()
    return manifest_module.parse_manifest_bytes(payload)


def _v35_candidate_manifest(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = V35_MANIFEST_PATH.read_bytes()
    semantic = _canonical(json.loads(payload))
    monkeypatch.setattr(
        manifest_module,
        "V35_SEMANTIC_SHA256",
        hashlib.sha256(semantic).hexdigest(),
    )
    return manifest_module.parse_manifest_bytes(payload)


def _v36_candidate_manifest(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = V36_MANIFEST_PATH.read_bytes()
    semantic = _canonical(json.loads(payload))
    monkeypatch.setattr(
        manifest_module,
        "V36_SEMANTIC_SHA256",
        hashlib.sha256(semantic).hexdigest(),
    )
    return manifest_module.parse_manifest_bytes(payload)


def _v37_candidate_manifest(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = V37_MANIFEST_PATH.read_bytes()
    semantic = _canonical(json.loads(payload))
    monkeypatch.setattr(
        manifest_module,
        "V37_SEMANTIC_SHA256",
        hashlib.sha256(semantic).hexdigest(),
    )
    return manifest_module.parse_manifest_bytes(payload)


def _v38_candidate_manifest(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = V38_MANIFEST_PATH.read_bytes()
    semantic = _canonical(json.loads(payload))
    monkeypatch.setattr(
        manifest_module,
        "V38_SEMANTIC_SHA256",
        hashlib.sha256(semantic).hexdigest(),
    )
    return manifest_module.parse_manifest_bytes(payload)


def _v39_candidate_manifest(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = FROZEN_MANIFEST_PATH.read_bytes()
    semantic = _canonical(json.loads(payload))
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_SEMANTIC_SHA256",
        hashlib.sha256(semantic).hexdigest(),
    )
    return manifest_module.parse_manifest_bytes(payload)


def _native_event(
    *,
    source_id: str,
    sequence: int,
    monotonic_ns: int,
    event_type: str,
    payload: dict[str, object],
) -> validation._NativeEvent:
    return validation._NativeEvent(
        relative_path=f"raw/{source_id}.jsonl",
        line_number=sequence,
        source_kind="replica",
        source_id=source_id,
        source_instance=f"slot-{source_id}",
        source_sequence=sequence,
        monotonic_ns=monotonic_ns,
        event_type=event_type,
        payload=payload,
        line_sha256=hashlib.sha256(str(payload).encode()).hexdigest(),
    )


def _commit_event(
    sequence: int,
    monotonic_ns: int,
    height: int,
    *,
    epoch_number: int = 0,
    epoch_digest: str = "11" * 32,
    tree_id: int = 0,
    replica_id: int = 0,
) -> validation._NativeEvent:
    block_hash = f"{height:064x}"
    parent_hash = None if height == 1 else f"{height - 1:064x}"
    return _native_event(
        source_id=f"replica-{replica_id}",
        sequence=sequence,
        monotonic_ns=monotonic_ns,
        event_type="block.committed",
        payload={
            "block_height": height,
            "block_hash": block_hash,
            "parent_hash": parent_hash,
            "transaction_count": 1000,
            "designated_observer": replica_id == 0,
            "decision_proof": {
                "epoch_number": epoch_number,
                "tree_id": tree_id,
                "epoch_digest": epoch_digest,
                "block_hash": block_hash,
            },
            "view_generation": 1,
            "commit_batch_index": 0,
        },
    )


def _commit_observed_event(
    committed: validation._NativeEvent,
    *,
    sequence: int,
    monotonic_ns: int,
    payload_updates: dict[str, object] | None = None,
) -> validation._NativeEvent:
    payload = {
        key: committed.payload[key]
        for key in (
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "commit_batch_index",
        )
    }
    if payload_updates is not None:
        payload.update(payload_updates)
    return _native_event(
        source_id=committed.source_id,
        sequence=sequence,
        monotonic_ns=monotonic_ns,
        event_type="block.commit_observed",
        payload=payload,
    )


def _commit_identity_unavailable_event(
    observed: validation._NativeEvent,
    *,
    sequence: int,
    monotonic_ns: int,
    payload_updates: dict[str, object] | None = None,
) -> validation._NativeEvent:
    payload = {
        **observed.payload,
        "reason": "no_authenticated_exact_identity_source",
        "convergence_identity_pending": False,
    }
    if payload_updates is not None:
        payload.update(payload_updates)
    return _native_event(
        source_id=observed.source_id,
        sequence=sequence,
        monotonic_ns=monotonic_ns,
        event_type="block.commit_identity_unavailable",
        payload=payload,
    )


def _throughput_document(
    phase_windows: dict[str, tuple[int, int, int]],
    *,
    omit_zero_bucket: bool = False,
) -> dict[str, object]:
    width_s = 5
    phases: list[dict[str, object]] = []
    phase_configurations = {
        "baseline": (0, "11" * 32),
        "fault_evidence": (0, "11" * 32),
        "epoch1_stable": (1, "22" * 32),
        "epoch2_stable": (2, "33" * 32),
    }
    for name in validation.PHASES:
        start, end, count = phase_windows[name]
        buckets = []
        for index in range(count):
            transactions = 1000 if name == "baseline" and index == 0 else 0
            buckets.append(
                {
                    "bucket_index": index,
                    "start_monotonic_ns": start + index * width_s * 1_000_000_000,
                    "end_monotonic_ns": start + (index + 1) * width_s * 1_000_000_000,
                    "transactions": transactions,
                    "tps": transactions / width_s,
                }
            )
        if omit_zero_bucket and name == "fault_evidence":
            buckets.pop()
        transactions = 1000 if name == "baseline" else 0
        phases.append(
            {
                "phase": name,
                "start_monotonic_ns": start,
                "end_monotonic_ns": end,
                "configuration": {
                    "epoch_number": phase_configurations[name][0],
                    "epoch_digest": phase_configurations[name][1],
                },
                "buckets": buckets,
                "transactions": transactions,
                "mean_tps": transactions / (count * width_s),
            }
        )
    return {
        "schema_version": 1,
        "slot_id": "slot-test",
        "bucket_width_s": width_s,
        "authority": {
            "event_type": "block.committed",
            "source_id": "replica-0",
            "source_instance": "slot-replica-0",
            "unique_commit_rule": "block_height_and_hash_exactly_once_v1",
        },
        "phases": phases,
    }


def _throughput_phase_configurations(
) -> dict[str, tuple[int, str, frozenset[int]]]:
    return {
        "baseline": (0, "11" * 32, frozenset({0})),
        "fault_evidence": (0, "11" * 32, frozenset({0})),
        "epoch1_stable": (1, "22" * 32, frozenset({0, 1})),
        "epoch2_stable": (2, "33" * 32, frozenset({0, 1})),
    }


def test_actor_and_fnv_vectors_recompute_without_runtime_decision_code() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)

    for vector in manifest.byzantine.actor_selection_vectors:
        assert derive_actor_ids(
            vector.replica_count,
            manifest.byzantine.actor_count,
            vector.scientific_seed,
        ) == vector.selected_actor_ids
    legacy = load_frozen_manifest(LEGACY_MANIFEST_PATH)
    for vector in legacy.byzantine.actor_rotation_vectors:
        assert fnv1a_rotating_actor(
            vector.sorted_actor_ids,
            epoch_number=vector.epoch_number,
            tree_id=vector.tree_id,
            epoch_digest=vector.epoch_digest,
            block_hash=vector.block_hash,
        ) == (vector.fnv1a64, vector.selected_actor)


def test_responsive_degraded_vectors_recompute_with_observer_zero_isolated() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    for vector in responsive.actor_selection_vectors:
        hard = derive_actor_ids(
            vector.replica_count,
            manifest.byzantine.actor_count,
            vector.scientific_seed,
        )
        degraded = validation.derive_responsive_degraded_actor_ids(
            vector.replica_count,
            hard,
            vector.scientific_seed,
        )
        assert degraded == vector.selected_actor_ids
        assert 0 not in degraded
        assert not set(degraded).intersection(hard)
        assert len((*hard, *degraded)) == (vector.replica_count - 1) // 3


def test_validator_retains_exact_v1_through_v37_artifact_identities() -> None:
    identities = {
        version: validation._frozen_artifact_identity(
            load_frozen_manifest(path).manifest_id
        )
        for version, path in (
            (1, LEGACY_MANIFEST_PATH),
            (2, V2_MANIFEST_PATH),
            (3, V3_MANIFEST_PATH),
            (4, V4_MANIFEST_PATH),
            (5, V5_MANIFEST_PATH),
            (6, V6_MANIFEST_PATH),
            (7, V7_MANIFEST_PATH),
            (8, V8_MANIFEST_PATH),
            (9, V9_MANIFEST_PATH),
            (10, V10_MANIFEST_PATH),
            (11, V11_MANIFEST_PATH),
            (12, V12_MANIFEST_PATH),
            (13, V13_MANIFEST_PATH),
            (14, V14_MANIFEST_PATH),
            (15, V15_MANIFEST_PATH),
            (16, V16_MANIFEST_PATH),
            (17, V17_MANIFEST_PATH),
            (18, V18_MANIFEST_PATH),
            (19, V19_MANIFEST_PATH),
            (20, V20_MANIFEST_PATH),
            (21, V21_MANIFEST_PATH),
            (22, V22_MANIFEST_PATH),
            (23, V23_MANIFEST_PATH),
            (24, MANIFEST_PATH),
            (25, V25_MANIFEST_PATH),
            (26, V26_MANIFEST_PATH),
            (27, V27_MANIFEST_PATH),
        )
    }
    identities[28] = validation._frozen_artifact_identity(
        validation.V28_MANIFEST_ID
    )
    identities[29] = validation._frozen_artifact_identity(
        validation.V29_MANIFEST_ID
    )
    identities[30] = validation._frozen_artifact_identity(
        validation.V30_MANIFEST_ID
    )
    identities[31] = validation._frozen_artifact_identity(
        validation.V31_MANIFEST_ID
    )
    identities[32] = validation._frozen_artifact_identity(
        validation.V32_MANIFEST_ID
    )
    identities[33] = validation._frozen_artifact_identity(
        validation.V33_MANIFEST_ID
    )
    identities[34] = validation._frozen_artifact_identity(
        validation.V34_MANIFEST_ID
    )
    identities[35] = validation._frozen_artifact_identity(
        validation.V35_MANIFEST_ID
    )
    identities[36] = validation._frozen_artifact_identity(
        validation.V36_MANIFEST_ID
    )
    identities[37] = validation._frozen_artifact_identity(
        validation.V37_MANIFEST_ID
    )

    assert identities[1].manifest_sha256 == validation.LEGACY_MANIFEST_SHA256
    assert identities[2].manifest_sha256 == validation.V2_MANIFEST_SHA256
    assert identities[2].runtime_sha256 == validation.V2_RUNTIME_SHA256
    assert identities[3].manifest_sha256 == validation.V3_MANIFEST_SHA256
    assert identities[3].runtime_sha256 == validation.V3_RUNTIME_SHA256
    assert identities[3].smoke_runtime_sha256 == validation.V3_SMOKE_RUNTIME_SHA256
    assert identities[4].manifest_sha256 == validation.V4_MANIFEST_SHA256
    assert identities[4].runtime_sha256 == validation.V4_RUNTIME_SHA256
    assert identities[4].smoke_runtime_sha256 == validation.V4_SMOKE_RUNTIME_SHA256
    assert identities[5].manifest_sha256 == validation.V5_MANIFEST_SHA256
    assert identities[5].runtime_sha256 == validation.V5_RUNTIME_SHA256
    assert identities[5].smoke_runtime_sha256 == validation.V5_SMOKE_RUNTIME_SHA256
    assert identities[6].manifest_sha256 == validation.V6_MANIFEST_SHA256
    assert identities[6].runtime_sha256 == validation.V6_RUNTIME_SHA256
    assert identities[6].smoke_runtime_sha256 == validation.V6_SMOKE_RUNTIME_SHA256
    assert identities[7].manifest_sha256 == validation.V7_MANIFEST_SHA256
    assert identities[7].runtime_sha256 == validation.V7_RUNTIME_SHA256
    assert identities[7].smoke_runtime_sha256 == validation.V7_SMOKE_RUNTIME_SHA256
    assert identities[8].manifest_sha256 == validation.V8_MANIFEST_SHA256
    assert identities[8].runtime_sha256 == validation.V8_RUNTIME_SHA256
    assert identities[8].smoke_runtime_sha256 == validation.V8_SMOKE_RUNTIME_SHA256
    assert identities[9].manifest_sha256 == validation.V9_MANIFEST_SHA256
    assert identities[9].runtime_sha256 == validation.V9_RUNTIME_SHA256
    assert identities[9].smoke_runtime_sha256 == validation.V9_SMOKE_RUNTIME_SHA256
    assert identities[10].manifest_sha256 == validation.V10_MANIFEST_SHA256
    assert identities[10].runtime_sha256 == validation.V10_RUNTIME_SHA256
    assert (
        identities[10].smoke_runtime_sha256
        == validation.V10_SMOKE_RUNTIME_SHA256
    )
    assert identities[11].manifest_sha256 == validation.V11_MANIFEST_SHA256
    assert identities[11].runtime_sha256 == validation.V11_RUNTIME_SHA256
    assert (
        identities[11].smoke_runtime_sha256
        == validation.V11_SMOKE_RUNTIME_SHA256
    )
    assert identities[12].manifest_sha256 == validation.V12_MANIFEST_SHA256
    assert identities[12].runtime_sha256 == validation.V12_RUNTIME_SHA256
    assert (
        identities[12].smoke_runtime_sha256
        == validation.V12_SMOKE_RUNTIME_SHA256
    )
    assert identities[13].manifest_sha256 == validation.V13_MANIFEST_SHA256
    assert identities[13].runtime_sha256 == validation.V13_RUNTIME_SHA256
    assert (
        identities[13].smoke_runtime_sha256
        == validation.V13_SMOKE_RUNTIME_SHA256
    )
    assert identities[14].manifest_sha256 == validation.V14_MANIFEST_SHA256
    assert identities[14].runtime_sha256 == validation.V14_RUNTIME_SHA256
    assert (
        identities[14].smoke_runtime_sha256
        == validation.V14_SMOKE_RUNTIME_SHA256
    )
    assert identities[15].manifest_sha256 == validation.V15_MANIFEST_SHA256
    assert identities[15].plan_sha256 == validation.V15_PLAN_SHA256
    assert identities[15].runtime_sha256 == validation.V15_RUNTIME_SHA256
    assert (
        identities[15].smoke_runtime_sha256
        == validation.V15_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[15].coverage_smoke_runtime_sha256
        == validation.V15_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[16].manifest_sha256 == validation.V16_MANIFEST_SHA256
    assert identities[16].plan_sha256 == validation.V16_PLAN_SHA256
    assert identities[16].runtime_sha256 == validation.V16_RUNTIME_SHA256
    assert (
        identities[16].smoke_runtime_sha256
        == validation.V16_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[16].coverage_smoke_runtime_sha256
        == validation.V16_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[17].manifest_sha256 == validation.V17_MANIFEST_SHA256
    assert identities[17].plan_sha256 == validation.V17_PLAN_SHA256
    assert identities[17].runtime_sha256 == validation.V17_RUNTIME_SHA256
    assert (
        identities[17].smoke_runtime_sha256
        == validation.V17_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[17].coverage_smoke_runtime_sha256
        == validation.V17_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[18].manifest_sha256 == validation.V18_MANIFEST_SHA256
    assert identities[18].plan_sha256 == validation.V18_PLAN_SHA256
    assert identities[18].runtime_sha256 == validation.V18_RUNTIME_SHA256
    assert (
        identities[18].smoke_runtime_sha256
        == validation.V18_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[18].coverage_smoke_runtime_sha256
        == validation.V18_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[19].manifest_sha256 == validation.V19_MANIFEST_SHA256
    assert identities[19].plan_sha256 == validation.V19_PLAN_SHA256
    assert identities[19].runtime_sha256 == validation.V19_RUNTIME_SHA256
    assert (
        identities[19].smoke_runtime_sha256
        == validation.V19_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[19].coverage_smoke_runtime_sha256
        == validation.V19_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[20].manifest_sha256 == validation.V20_MANIFEST_SHA256
    assert identities[20].plan_sha256 == validation.V20_PLAN_SHA256
    assert identities[20].runtime_sha256 == validation.V20_RUNTIME_SHA256
    assert (
        identities[20].smoke_runtime_sha256
        == validation.V20_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[20].coverage_smoke_runtime_sha256
        == validation.V20_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[21].manifest_sha256 == validation.V21_MANIFEST_SHA256
    assert identities[21].plan_sha256 == validation.V21_PLAN_SHA256
    assert identities[21].runtime_sha256 == validation.V21_RUNTIME_SHA256
    assert (
        identities[21].smoke_runtime_sha256
        == validation.V21_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[21].coverage_smoke_runtime_sha256
        == validation.V21_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[22].manifest_sha256 == validation.V22_MANIFEST_SHA256
    assert identities[22].plan_sha256 == validation.V22_PLAN_SHA256
    assert identities[22].runtime_sha256 == validation.V22_RUNTIME_SHA256
    assert (
        identities[22].smoke_runtime_sha256
        == validation.V22_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[22].coverage_smoke_runtime_sha256
        == validation.V22_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[23].manifest_sha256 == validation.V23_MANIFEST_SHA256
    assert identities[23].plan_sha256 == validation.V23_PLAN_SHA256
    assert identities[23].runtime_sha256 == validation.V23_RUNTIME_SHA256
    assert (
        identities[23].smoke_runtime_sha256
        == validation.V23_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[23].coverage_smoke_runtime_sha256
        == validation.V23_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[24].manifest_sha256 == validation.V24_MANIFEST_SHA256
    assert identities[24].plan_sha256 == validation.V24_PLAN_SHA256
    assert identities[24].runtime_sha256 == validation.V24_RUNTIME_SHA256
    assert (
        identities[24].smoke_runtime_sha256
        == validation.V24_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[24].coverage_smoke_runtime_sha256
        == validation.V24_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[25].manifest_sha256 == validation.V25_MANIFEST_SHA256
    assert identities[25].plan_sha256 == validation.V25_PLAN_SHA256
    assert identities[25].runtime_sha256 == validation.V25_RUNTIME_SHA256
    assert (
        identities[25].smoke_runtime_sha256
        == validation.V25_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[25].coverage_smoke_runtime_sha256
        == validation.V25_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[26].manifest_sha256 == validation.V26_MANIFEST_SHA256
    assert identities[26].plan_sha256 == validation.V26_PLAN_SHA256
    assert identities[26].runtime_sha256 == validation.V26_RUNTIME_SHA256
    assert (
        identities[26].smoke_runtime_sha256
        == validation.V26_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[26].coverage_smoke_runtime_sha256
        == validation.V26_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[27].manifest_sha256 == validation.V27_MANIFEST_SHA256
    assert identities[27].plan_sha256 == validation.V27_PLAN_SHA256
    assert identities[27].runtime_sha256 == validation.V27_RUNTIME_SHA256
    assert identities[27].smoke_runtime_sha256 == validation.V27_SMOKE_RUNTIME_SHA256
    assert (
        identities[27].coverage_smoke_runtime_sha256
        == validation.V27_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[28].manifest_sha256 == validation.V28_MANIFEST_SHA256
    assert identities[28].plan_sha256 == validation.V28_PLAN_SHA256
    assert identities[28].runtime_sha256 == validation.V28_RUNTIME_SHA256
    assert (
        identities[28].smoke_runtime_sha256
        == validation.V28_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[28].coverage_smoke_runtime_sha256
        == validation.V28_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[29].manifest_sha256 == validation.V29_MANIFEST_SHA256
    assert identities[29].plan_sha256 == validation.V29_PLAN_SHA256
    assert identities[29].runtime_sha256 == validation.V29_RUNTIME_SHA256
    assert (
        identities[29].smoke_runtime_sha256
        == validation.V29_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[29].coverage_smoke_runtime_sha256
        == validation.V29_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[30].manifest_sha256 == validation.V30_MANIFEST_SHA256
    assert identities[30].plan_sha256 == validation.V30_PLAN_SHA256
    assert identities[30].runtime_sha256 == validation.V30_RUNTIME_SHA256
    assert (
        identities[30].smoke_runtime_sha256
        == validation.V30_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[30].coverage_smoke_runtime_sha256
        == validation.V30_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[31].manifest_sha256 == validation.V31_MANIFEST_SHA256
    assert identities[31].plan_sha256 == validation.V31_PLAN_SHA256
    assert identities[31].runtime_sha256 == validation.V31_RUNTIME_SHA256
    assert (
        identities[31].smoke_runtime_sha256
        == validation.V31_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[31].coverage_smoke_runtime_sha256
        == validation.V31_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[32].manifest_sha256 == validation.V32_MANIFEST_SHA256
    assert identities[32].plan_sha256 == validation.V32_PLAN_SHA256
    assert identities[32].runtime_sha256 == validation.V32_RUNTIME_SHA256
    assert (
        identities[32].smoke_runtime_sha256
        == validation.V32_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[32].coverage_smoke_runtime_sha256
        == validation.V32_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[33].manifest_sha256 == validation.V33_MANIFEST_SHA256
    assert identities[33].plan_sha256 == validation.V33_PLAN_SHA256
    assert identities[33].runtime_sha256 == validation.V33_RUNTIME_SHA256
    assert (
        identities[33].smoke_runtime_sha256
        == validation.V33_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[33].coverage_smoke_runtime_sha256
        == validation.V33_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[34].manifest_sha256 == validation.V34_MANIFEST_SHA256
    assert identities[34].plan_sha256 == validation.V34_PLAN_SHA256
    assert identities[34].runtime_sha256 == validation.V34_RUNTIME_SHA256
    assert (
        identities[34].smoke_runtime_sha256
        == validation.V34_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[34].coverage_smoke_runtime_sha256
        == validation.V34_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[35].manifest_sha256 == validation.V35_MANIFEST_SHA256
    assert identities[35].plan_sha256 == validation.V35_PLAN_SHA256
    assert identities[35].runtime_sha256 == validation.V35_RUNTIME_SHA256
    assert (
        identities[35].smoke_runtime_sha256
        == validation.V35_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[35].coverage_smoke_runtime_sha256
        == validation.V35_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[36].manifest_sha256 == validation.V36_MANIFEST_SHA256
    assert identities[36].plan_sha256 == validation.V36_PLAN_SHA256
    assert identities[36].runtime_sha256 == validation.V36_RUNTIME_SHA256
    assert (
        identities[36].smoke_runtime_sha256
        == validation.V36_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[36].coverage_smoke_runtime_sha256
        == validation.V36_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert identities[37].manifest_sha256 == validation.V37_MANIFEST_SHA256
    assert identities[37].plan_sha256 == validation.V37_PLAN_SHA256
    assert identities[37].runtime_sha256 == validation.V37_RUNTIME_SHA256
    assert (
        identities[37].smoke_runtime_sha256
        == validation.V37_SMOKE_RUNTIME_SHA256
    )
    assert (
        identities[37].coverage_smoke_runtime_sha256
        == validation.V37_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert validation._coverage_smoke_result_root(
        validation.V23_MANIFEST_ID
    ) == "results/shape-placement-factorial-v23-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V24_MANIFEST_ID
    ) == "results/shape-placement-factorial-v24-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V25_MANIFEST_ID
    ) == "results/shape-placement-factorial-v25-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V26_MANIFEST_ID
    ) == "results/shape-placement-factorial-v26-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V27_MANIFEST_ID
    ) == "results/shape-placement-factorial-v27-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V28_MANIFEST_ID
    ) == "results/shape-placement-factorial-v28-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V29_MANIFEST_ID
    ) == "results/shape-placement-factorial-v29-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V30_MANIFEST_ID
    ) == "results/shape-placement-factorial-v30-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V31_MANIFEST_ID
    ) == "results/shape-placement-factorial-v31-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V32_MANIFEST_ID
    ) == "results/shape-placement-factorial-v32-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V33_MANIFEST_ID
    ) == "results/shape-placement-factorial-v33-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V34_MANIFEST_ID
    ) == "results/shape-placement-factorial-v34-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V35_MANIFEST_ID
    ) == "results/shape-placement-factorial-v35-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V36_MANIFEST_ID
    ) == "results/shape-placement-factorial-v36-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V37_MANIFEST_ID
    ) == "results/shape-placement-factorial-v37-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.V38_MANIFEST_ID
    ) == "results/shape-placement-factorial-v38-coverage-smoke"
    assert validation._coverage_smoke_result_root(
        validation.FROZEN_MANIFEST_ID
    ) == "results/shape-placement-factorial-v39-coverage-smoke"


def test_validator_v27_identities_match_independent_artifact_recomputation() -> None:
    manifest_payload = V27_MANIFEST_PATH.read_bytes()
    manifest = load_frozen_manifest(V27_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    primary = next(
        slot for slot in plan.slots if slot.execution_ordinal == 1
    )
    repair = next(
        slot for slot in plan.slots if slot.execution_ordinal == 5
    )
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )

    recomputed = (
        hashlib.sha256(manifest_payload).hexdigest(),
        hashlib.sha256(_canonical(json.loads(manifest_payload))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(smoke.runtime.as_document())
        ).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest(),
    )
    assert recomputed == (
        manifest_module.V27_MANIFEST_SHA256,
        manifest_module.V27_SEMANTIC_SHA256,
        manifest_module.V27_PLAN_SHA256,
        validation.V27_RUNTIME_SHA256,
        validation.V27_SMOKE_RUNTIME_SHA256,
        validation.V27_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_validator_v28_identities_match_independent_artifact_recomputation() -> None:
    manifest_payload = V28_MANIFEST_PATH.read_bytes()
    manifest = load_frozen_manifest(V28_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    primary = next(
        slot for slot in plan.slots if slot.execution_ordinal == 1
    )
    repair = next(
        slot for slot in plan.slots if slot.execution_ordinal == 5
    )
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )

    recomputed = (
        hashlib.sha256(manifest_payload).hexdigest(),
        hashlib.sha256(_canonical(json.loads(manifest_payload))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(smoke.runtime.as_document())
        ).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest(),
    )
    assert recomputed == (
        manifest_module.V28_MANIFEST_SHA256,
        manifest_module.V28_SEMANTIC_SHA256,
        manifest_module.V28_PLAN_SHA256,
        validation.V28_RUNTIME_SHA256,
        validation.V28_SMOKE_RUNTIME_SHA256,
        validation.V28_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_validator_v29_identities_match_independent_artifact_recomputation() -> None:
    manifest_payload = V29_MANIFEST_PATH.read_bytes()
    manifest = load_frozen_manifest(V29_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    primary = next(
        slot for slot in plan.slots if slot.execution_ordinal == 1
    )
    repair = next(
        slot for slot in plan.slots if slot.execution_ordinal == 5
    )
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )

    recomputed = (
        hashlib.sha256(manifest_payload).hexdigest(),
        hashlib.sha256(_canonical(json.loads(manifest_payload))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(smoke.runtime.as_document())
        ).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest(),
    )
    assert recomputed == (
        manifest_module.V29_MANIFEST_SHA256,
        manifest_module.V29_SEMANTIC_SHA256,
        manifest_module.V29_PLAN_SHA256,
        validation.V29_RUNTIME_SHA256,
        validation.V29_SMOKE_RUNTIME_SHA256,
        validation.V29_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_validator_v30_identities_match_independent_artifact_recomputation() -> None:
    manifest_payload = V30_MANIFEST_PATH.read_bytes()
    manifest = load_frozen_manifest(V30_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )

    recomputed = (
        hashlib.sha256(manifest_payload).hexdigest(),
        hashlib.sha256(_canonical(json.loads(manifest_payload))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(smoke.runtime.as_document())
        ).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest(),
    )
    assert recomputed == (
        manifest_module.V30_MANIFEST_SHA256,
        manifest_module.V30_SEMANTIC_SHA256,
        manifest_module.V30_PLAN_SHA256,
        validation.V30_RUNTIME_SHA256,
        validation.V30_SMOKE_RUNTIME_SHA256,
        validation.V30_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_validator_v31_identities_match_independent_artifact_recomputation() -> None:
    manifest_payload = V31_MANIFEST_PATH.read_bytes()
    manifest = load_frozen_manifest(V31_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )

    recomputed = (
        hashlib.sha256(manifest_payload).hexdigest(),
        hashlib.sha256(_canonical(json.loads(manifest_payload))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(smoke.runtime.as_document())
        ).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest(),
    )
    assert recomputed == (
        manifest_module.V31_MANIFEST_SHA256,
        manifest_module.V31_SEMANTIC_SHA256,
        manifest_module.V31_PLAN_SHA256,
        validation.V31_RUNTIME_SHA256,
        validation.V31_SMOKE_RUNTIME_SHA256,
        validation.V31_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_validator_v32_identities_match_independent_artifact_recomputation() -> None:
    manifest_payload = V32_MANIFEST_PATH.read_bytes()
    manifest = load_frozen_manifest(V32_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    identity = validation._frozen_artifact_identity(
        validation.V32_MANIFEST_ID
    )
    recomputed = (
        hashlib.sha256(manifest_payload).hexdigest(),
        hashlib.sha256(_canonical(json.loads(manifest_payload))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(smoke.runtime.as_document())
        ).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest(),
    )
    expected = (
        "cbc03d8c58b8192b70b5c0f8c0a504dc07076f8e78895a3a2c802b98658d691d",
        "eb44b9c5c229db10fc967b7d3834029f8780c48f6ae4740b213a692c9af8cb42",
        "3325d3d1b0b1bf2569686db9d28e3cc8d6cd6e9b6d6fc4ddf97e48b56c1bc2fa",
        "5771dcfa48a4d6550231221b4a7dd409af190b6389797511c1e84419e1b4b395",
        "5a27efa53d8324068c67ead555a304c77d7727d82b69bb1d84f4cc80b6d46e74",
        "45da6535ee0bf9035b9f61375e03afd7737b6f0d8920431cb5f0248a58a06f86",
    )

    assert validation.V32_MANIFEST_ID == "shape-placement-factorial-v32"
    assert recomputed == expected
    assert expected[3:] == (
        runtime_module.V32_RUNTIME_SHA256,
        runtime_module.V32_SMOKE_RUNTIME_SHA256,
        runtime_module.V32_COVERAGE_SMOKE_RUNTIME_SHA256,
    )
    assert expected == (
        identity.manifest_sha256,
        manifest_module.V32_SEMANTIC_SHA256,
        identity.plan_sha256,
        identity.runtime_sha256,
        identity.smoke_runtime_sha256,
        identity.coverage_smoke_runtime_sha256,
    )


def test_validator_v33_identities_match_independent_artifact_recomputation() -> None:
    manifest_payload = V33_MANIFEST_PATH.read_bytes()
    manifest = load_frozen_manifest(V33_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    identity = validation._frozen_artifact_identity(
        validation.V33_MANIFEST_ID
    )
    recomputed = (
        hashlib.sha256(manifest_payload).hexdigest(),
        hashlib.sha256(_canonical(json.loads(manifest_payload))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(smoke.runtime.as_document())
        ).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest(),
    )
    expected = (
        "aa109a401ef88f7d135ff7f590ae1f372329ed5165c223ce0e3bf44536cf61fc",
        "8faee7b8cf1148a7acec48b75f02ca3f5b864ee6981555ae78624664e246509f",
        "0897fc233ae6fc25898ee576066403f122ad65416322dec2d4e99e03503fd81c",
        "597bddecd5ada141dfaf1830cabb645fa5f50aaf20abd198c134a48e2d1e4e2b",
        "c0b96dfaaf73a374113a0c6ba98f87c06a934eb6b40202d96a51b6182c714c05",
        "18d9dd6841b3dc69a3797d9473aa0f14df2bc793cf3354abd67be91000e7007e",
    )

    assert validation.V33_MANIFEST_ID == "shape-placement-factorial-v33"
    assert recomputed == expected
    assert expected == (
        identity.manifest_sha256,
        manifest_module.V33_SEMANTIC_SHA256,
        identity.plan_sha256,
        identity.runtime_sha256,
        identity.smoke_runtime_sha256,
        identity.coverage_smoke_runtime_sha256,
    )
    assert expected[3:] == (
        runtime_module.V33_RUNTIME_SHA256,
        runtime_module.V33_SMOKE_RUNTIME_SHA256,
        runtime_module.V33_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_validator_v34_identities_match_independent_artifact_recomputation() -> None:
    manifest_payload = V34_MANIFEST_PATH.read_bytes()
    manifest = load_frozen_manifest(V34_MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    identity = validation._frozen_artifact_identity(
        validation.V34_MANIFEST_ID
    )
    recomputed = (
        hashlib.sha256(manifest_payload).hexdigest(),
        hashlib.sha256(_canonical(json.loads(manifest_payload))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(smoke.runtime.as_document())
        ).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest(),
    )
    expected = (
        "1b0c22f5f517487c507c7699198f597093dbc879994925ddbb8220c67decedb0",
        "e963372c05c51367c6a2d54ff2f1065b1bfb136b4e0857f2dbb69793078fc0a4",
        "6e48cd511a0788ea1e2e5b42fb9b09e47a4c269d40fe18779cb559990b536e92",
        "73dc1bd18235fe2ef9a565b2486cd48ba49fe10b3e89adc7666d5baee6ba985b",
        "b004afbe823f75bc96521dcc7202cfdbb91930eea6286730f17e316308c0107f",
        "f79fa73e1af6c2be6b2284d9d9a0b56224dcca94c153f7e91e2220ae49266d5e",
    )

    assert validation.V34_MANIFEST_ID == "shape-placement-factorial-v34"
    assert recomputed == expected
    assert expected == (
        identity.manifest_sha256,
        manifest_module.V34_SEMANTIC_SHA256,
        identity.plan_sha256,
        identity.runtime_sha256,
        identity.smoke_runtime_sha256,
        identity.coverage_smoke_runtime_sha256,
    )
    assert expected[:3] == (
        manifest_module.V34_MANIFEST_SHA256,
        manifest_module.V34_SEMANTIC_SHA256,
        manifest_module.V34_PLAN_SHA256,
    )
    assert expected[3:] == (
        runtime_module.V34_RUNTIME_SHA256,
        runtime_module.V34_SMOKE_RUNTIME_SHA256,
        runtime_module.V34_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_validator_v35_identities_are_exactly_frozen() -> None:
    identity = validation._frozen_artifact_identity(
        validation.V35_MANIFEST_ID
    )

    assert validation.V35_MANIFEST_ID == "shape-placement-factorial-v35"
    assert (
        identity.manifest_sha256,
        identity.plan_sha256,
        identity.runtime_sha256,
        identity.smoke_runtime_sha256,
        identity.coverage_smoke_runtime_sha256,
    ) == (
        "28303487578594d7eac64aa1eda11ea891a85b8a6d6ff386d42e920dc8b82b95",
        "6c558653d0db5e5f646de58c1a853d656fedbd7e61257e13755ef01ea9d42025",
        "05ffcdc81d0cd0ec8a264cd0d5545e88fbf14dba1569629e6ec0f02c3a16615c",
        "785abe70a6500a66c331e00dd81eeada1065457ff4dace4e6f9035b9006aaee6",
        "4fa44f57128bc096c7fd40bbf9c054c184a273d07ca8f2bdc43fdda7c5e93ad2",
    )
    assert (
        validation.V35_MANIFEST_SHA256,
        manifest_module.V35_SEMANTIC_SHA256,
        validation.V35_PLAN_SHA256,
        validation.V35_RUNTIME_SHA256,
        validation.V35_SMOKE_RUNTIME_SHA256,
        validation.V35_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "28303487578594d7eac64aa1eda11ea891a85b8a6d6ff386d42e920dc8b82b95",
        "40422f2b8ac74e695fdfa18b687fce2b31b516f36bd2b6bfe51f53602bd4d0c8",
        "6c558653d0db5e5f646de58c1a853d656fedbd7e61257e13755ef01ea9d42025",
        "05ffcdc81d0cd0ec8a264cd0d5545e88fbf14dba1569629e6ec0f02c3a16615c",
        "785abe70a6500a66c331e00dd81eeada1065457ff4dace4e6f9035b9006aaee6",
        "4fa44f57128bc096c7fd40bbf9c054c184a273d07ca8f2bdc43fdda7c5e93ad2",
    )


def test_validator_v36_identities_are_exactly_frozen() -> None:
    identity = validation._frozen_artifact_identity(
        validation.V36_MANIFEST_ID
    )

    assert validation.V36_MANIFEST_ID == "shape-placement-factorial-v36"
    assert (
        identity.manifest_sha256,
        manifest_module.V36_SEMANTIC_SHA256,
        identity.plan_sha256,
        identity.runtime_sha256,
        identity.smoke_runtime_sha256,
        identity.coverage_smoke_runtime_sha256,
    ) == (
        "50761ebcd8693c33ca30257b3abee6f44f992481b6f10e57732d098b029073d1",
        "87cd28e7df12aeb9f54386623b207526ea71d096d3699d687dc07784219d63ed",
        "d5075db22099788a1c687ddc72cc4953a2d665fc1fa09104ba69ab128f91a65f",
        "5b088b4d3e2a0a484f2829664b94db6d51e32fb0e8a0bc904fdf342098a85c89",
        "3ad3f26401c190827591351b21578e2b3ec088f0222e8262f2cfa6f29a402c7a",
        "34df26b4aaff8c3f417d1634aa1e52e7f40551b8265c2830720456dc068acc81",
    )


def test_validator_v37_identities_are_exactly_frozen() -> None:
    identity = validation._frozen_artifact_identity(
        validation.V37_MANIFEST_ID
    )

    assert validation.V37_MANIFEST_ID == "shape-placement-factorial-v37"
    assert (
        identity.manifest_sha256,
        manifest_module.V37_SEMANTIC_SHA256,
        identity.plan_sha256,
        identity.runtime_sha256,
        identity.smoke_runtime_sha256,
        identity.coverage_smoke_runtime_sha256,
    ) == (
        "a926192d3c6a5129ea8304504317f921a8a1a681b489e1503e715dcbd3e8be11",
        "63efea700bd3a0fa3d59b7876602e3a5f99a3d71edb4e57ed218a8e6ea4bec8c",
        "e413ff98733b5e462b058018ce25ca4c77013a760fffe730dcbfd50150379e36",
        "a05f26983a34f626f95d326847db7a049a05c2258fdadf3bbaddd4ec3850c5d7",
        "1eda50b8e2887ab4d0f1763816f82344136dabf480d752f5a4d82f48272e8f63",
        "7aa68f246e06bee0a666734113e5a5bda7a747c912b3cc51db6f81d03b2a6d81",
    )


def test_validator_v38_identities_are_exactly_frozen() -> None:
    identity = validation._frozen_artifact_identity(
        validation.V38_MANIFEST_ID
    )

    assert validation.V38_MANIFEST_ID == "shape-placement-factorial-v38"
    assert (
        identity.manifest_sha256,
        manifest_module.V38_SEMANTIC_SHA256,
        identity.plan_sha256,
        identity.runtime_sha256,
        identity.smoke_runtime_sha256,
        identity.coverage_smoke_runtime_sha256,
    ) == (
        "aec2c4f2a9cb53e7b3d8d212bc0b56c008679aa97ebba140db7bac9404415698",
        "9af592b436d934d13b1243439b84f78e1a78c19b035b37dbfe73bc168926b277",
        "7e731a7f36a49a5e49aa62601165bd1fe8c846e3eff20001d0fccfe37b6050e0",
        "2f080f10550c6eaac914435b436724a43f38615097063d3a70437a4459f69c3c",
        "f1ae1fcffdaf4b209a595f3dd034ec935ad88ada5821930c105694b525e2817a",
        "3cf676755478f686af64f12fd3fcb5993f4887a8f5165fbf540d289463544950",
    )


def test_validator_v39_identities_are_exactly_frozen() -> None:
    identity = validation._frozen_artifact_identity(
        validation.FROZEN_MANIFEST_ID
    )

    assert validation.FROZEN_MANIFEST_ID == "shape-placement-factorial-v39"
    assert (
        identity.manifest_sha256,
        manifest_module.FROZEN_SEMANTIC_SHA256,
        identity.plan_sha256,
        identity.runtime_sha256,
        identity.smoke_runtime_sha256,
        identity.coverage_smoke_runtime_sha256,
    ) == (
        "ce6fb4c999275b575f1cf522524a5f3b41d109a6dcb3a1b77e789946b67b042f",
        "14a3c3b910c89368481780d176e031e3bffeb15731b3448359fd73b10aee0a5a",
        "481b9df491a68355eade98bf241e9b2805430276681c148cf2178ad5e3c794a0",
        "8c46107ccea00424961501d31e9c85534d129353d8ed7fabba65e52c1049b17d",
        "ad75abbf4592661aee236625a18c61a476f730d2825378e6598b42a63882fe65",
        "aba47a1d783aa21769f153236e9f37c755cc7ef2ce6248c17cd21e84b505e856",
    )


@pytest.mark.parametrize(
    "manifest_path",
    (
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
    ),
)
def test_exact_prior_runtime_remains_validator_compatible(
    manifest_path: Path,
) -> None:
    manifest = load_frozen_manifest(manifest_path)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected for expected in validation._expected_slots(manifest)
    }
    runtime_slot = runtime.slots[0]

    validation._validate_runtime_slot(
        json.loads(json.dumps(runtime_slot.as_document())),
        expected_by_id[runtime_slot.slot_id],
        manifest,
    )


def test_validator_requires_v9_through_v22_causal_contracts_but_accepts_v8() -> None:
    explicit_v10_fields = {
        "causal_timeout_provenance_window": (
            validation.RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1
        ),
        "causal_internal_witness_candidates": (
            validation.RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1
        ),
        "causal_selection_linkage_window": (
            validation.RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1
        ),
    }
    explicit_v11_fields = {
        "marker_completeness_witness": (
            validation.RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1
        ),
        "causal_timeout_eligibility": (
            validation.RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1
        ),
    }
    for manifest_path in (
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
    ):
        manifest = load_frozen_manifest(manifest_path)
        runtime = build_factorial_runtime(build_factorial_plan(manifest))
        expected_by_id = {
            expected.slot_id: expected
            for expected in validation._expected_slots(manifest)
        }
        slot = runtime.slots[0]
        document = json.loads(json.dumps(slot.as_document()))

        validation._validate_runtime_slot(
            document,
            expected_by_id[slot.slot_id],
            manifest,
        )
        if manifest_path == V8_MANIFEST_PATH:
            assert "pending_attempt_retention" not in document["tiered_cohorts"]
            assert "causal_timeout_linkage" not in document["tiered_cohorts"]
            assert not set(explicit_v10_fields).intersection(
                document["tiered_cohorts"]
            )
            assert not set(explicit_v11_fields).intersection(
                document["tiered_cohorts"]
            )
            continue

        assert document["tiered_cohorts"]["pending_attempt_retention"] == (
            validation.RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1
        )
        assert document["tiered_cohorts"]["causal_timeout_linkage"] == (
            validation.RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1
        )
        if manifest_path == V9_MANIFEST_PATH:
            assert not set(explicit_v10_fields).intersection(
                document["tiered_cohorts"]
            )
        else:
            assert {
                field: document["tiered_cohorts"][field]
                for field in explicit_v10_fields
            } == explicit_v10_fields
        if manifest_path in (
            V11_MANIFEST_PATH,
            V12_MANIFEST_PATH,
            V13_MANIFEST_PATH,
        ):
            assert {
                field: document["tiered_cohorts"][field]
                for field in explicit_v11_fields
            } == explicit_v11_fields
        else:
            if manifest_path in (
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
            ):
                assert document["tiered_cohorts"][
                    "marker_completeness_witness"
                ] == validation.RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2
                assert document["tiered_cohorts"][
                    "causal_timeout_eligibility"
                ] == (
                    validation.RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2
                    if manifest_path
                    in (
                        V16_MANIFEST_PATH,
                        V17_MANIFEST_PATH,
                        V18_MANIFEST_PATH,
                        V19_MANIFEST_PATH,
                        V20_MANIFEST_PATH,
                    )
                    else (
                        validation.RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V3
                        if manifest_path
                        in (
                            V21_MANIFEST_PATH,
                            V22_MANIFEST_PATH,
                            V23_MANIFEST_PATH,
                            MANIFEST_PATH,
                        )
                        else validation.RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1
                    )
                )
            else:
                assert not set(explicit_v11_fields).intersection(
                    document["tiered_cohorts"]
                )

        drifted = copy.deepcopy(document)
        del drifted["tiered_cohorts"]["pending_attempt_retention"]
        with pytest.raises(
            FactorialValidationError,
            match="tiered cohort contract",
        ):
            validation._validate_runtime_slot(
                drifted,
                expected_by_id[slot.slot_id],
                manifest,
            )


def test_v17_runtime_mirrors_precontainment_shape_contract_only_in_causal_acceptance(
) -> None:
    manifest = load_frozen_manifest(V17_MANIFEST_PATH)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))
    contract = validation.PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1

    assert document["causal_acceptance"][
        "precontainment_shape_evaluation_contract"
    ] == contract
    validation._validate_runtime_slot(
        document,
        expected_by_id[slot.slot_id],
        manifest,
    )

    drifted = copy.deepcopy(document)
    del drifted["causal_acceptance"][
        "precontainment_shape_evaluation_contract"
    ]
    with pytest.raises(FactorialValidationError, match="causal acceptance contract"):
        validation._validate_runtime_slot(
            drifted,
            expected_by_id[slot.slot_id],
            manifest,
        )

    v16 = load_frozen_manifest(V16_MANIFEST_PATH)
    v16_runtime = build_factorial_runtime(build_factorial_plan(v16))
    assert "precontainment_shape_evaluation_contract" not in (
        v16_runtime.slots[0].causal_acceptance.as_document()
    )


def test_v18_through_v24_guarded_selection_contract_dispatch_is_exact() -> None:
    contract = validation.PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1

    def manifest(manifest_id: str, value: str | None) -> SimpleNamespace:
        responsive = SimpleNamespace()
        if value is not None:
            responsive.precontainment_guarded_selection_contract = value
        return SimpleNamespace(
            manifest_id=manifest_id,
            byzantine=SimpleNamespace(responsive_degradation=responsive),
        )

    assert validation._uses_precontainment_guarded_selection_contract(
        manifest(validation.FROZEN_MANIFEST_ID, contract)
    )
    assert validation._uses_precontainment_guarded_selection_contract(
        manifest(validation.V18_MANIFEST_ID, contract)
    )
    assert validation._uses_precontainment_guarded_selection_contract(
        manifest(validation.V19_MANIFEST_ID, contract)
    )
    assert validation._uses_precontainment_guarded_selection_contract(
        manifest(validation.V20_MANIFEST_ID, contract)
    )
    assert validation._uses_precontainment_guarded_selection_contract(
        manifest(validation.V21_MANIFEST_ID, contract)
    )
    assert validation._uses_precontainment_guarded_selection_contract(
        manifest(validation.V22_MANIFEST_ID, contract)
    )
    assert validation._uses_precontainment_guarded_selection_contract(
        manifest(validation.V23_MANIFEST_ID, contract)
    )
    assert not validation._uses_precontainment_guarded_selection_contract(
        manifest(validation.V17_MANIFEST_ID, contract)
    )
    assert not validation._uses_precontainment_guarded_selection_contract(
        manifest(validation.FROZEN_MANIFEST_ID, None)
    )
    assert not validation._uses_precontainment_guarded_selection_contract(
        manifest(validation.FROZEN_MANIFEST_ID, contract + "-forged")
    )


def test_v19_through_v22_future_tree_delivery_dispatch_is_exact() -> None:
    def manifest(manifest_id: str, value: str | None) -> SimpleNamespace:
        responsive = SimpleNamespace()
        if value is not None:
            responsive.future_tree_proposal_delivery_contract = value
        return SimpleNamespace(
            manifest_id=manifest_id,
            byzantine=SimpleNamespace(responsive_degradation=responsive),
        )

    assert validation._uses_future_tree_proposal_delivery_contract(
        manifest(
            validation.V19_MANIFEST_ID,
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT,
        )
    )
    assert validation._uses_future_tree_proposal_delivery_contract(
        manifest(
            validation.V20_MANIFEST_ID,
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT,
        )
    )
    assert validation._uses_future_tree_proposal_delivery_contract(
        manifest(
            validation.V21_MANIFEST_ID,
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT,
        )
    )
    assert validation._uses_future_tree_proposal_delivery_contract(
        manifest(
            validation.V22_MANIFEST_ID,
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT,
        )
    )
    assert not validation._uses_future_tree_proposal_delivery_contract(
        manifest(
            validation.V18_MANIFEST_ID,
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT,
        )
    )
    assert not validation._uses_future_tree_proposal_delivery_contract(
        manifest(validation.V19_MANIFEST_ID, None)
    )
    assert not validation._uses_future_tree_proposal_delivery_contract(
        manifest(
            validation.V19_MANIFEST_ID,
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT + "-forged",
        )
    )


def test_v20_through_v24_source_bound_proposal_witness_dispatch_is_exact() -> None:
    contract = validation.SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1
    assert contract == (
        "strictly_bijected_source_bound_fault_contribution_opportunity_proposal_"
        "keys_are_native_proposal_configuration_witnesses_after_exact_topology_"
        "validation_v1"
    )

    def manifest(manifest_id: str, value: str | None) -> SimpleNamespace:
        responsive = SimpleNamespace()
        if value is not None:
            responsive.source_bound_proposal_witness_contract = value
        return SimpleNamespace(
            manifest_id=manifest_id,
            byzantine=SimpleNamespace(responsive_degradation=responsive),
        )

    assert validation._uses_source_bound_proposal_witness_contract(
        manifest(validation.V20_MANIFEST_ID, contract)
    )
    assert validation._uses_source_bound_proposal_witness_contract(
        manifest(validation.V21_MANIFEST_ID, contract)
    )
    assert validation._uses_source_bound_proposal_witness_contract(
        manifest(validation.V22_MANIFEST_ID, contract)
    )
    assert validation._uses_source_bound_proposal_witness_contract(
        manifest(validation.V23_MANIFEST_ID, contract)
    )
    assert validation._uses_source_bound_proposal_witness_contract(
        manifest(validation.FROZEN_MANIFEST_ID, contract)
    )
    assert not validation._uses_source_bound_proposal_witness_contract(
        manifest(validation.V19_MANIFEST_ID, contract)
    )
    assert not validation._uses_source_bound_proposal_witness_contract(
        manifest(validation.V20_MANIFEST_ID, None)
    )
    assert not validation._uses_source_bound_proposal_witness_contract(
        manifest(validation.V20_MANIFEST_ID, contract + "-forged")
    )


def test_v21_through_v24_timeout_nonwitness_dispatch_is_field_exact() -> None:
    contract = validation.RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V3
    assert contract == (
        "selection_visible_exact_outstanding_timeout_witnesses_with_internal_"
        "and_f_plus_one_actor_gates_hard_and_responsive_degraded_absent_prefix_"
        "timeout_nonwitness_present_prefix_mismatch_fatal_v3"
    )

    def manifest(manifest_id: str, value: str | None) -> SimpleNamespace:
        responsive = SimpleNamespace(causal_timeout_eligibility=value)
        return SimpleNamespace(
            manifest_id=manifest_id,
            byzantine=SimpleNamespace(responsive_degradation=responsive),
        )

    assert validation._uses_selection_visible_responsive_timeout_nonwitnesses(
        manifest(validation.V21_MANIFEST_ID, contract)
    )
    assert validation._uses_selection_visible_responsive_timeout_nonwitnesses(
        manifest(validation.FROZEN_MANIFEST_ID, contract)
    )
    assert validation._uses_selection_visible_responsive_timeout_nonwitnesses(
        manifest(validation.V22_MANIFEST_ID, contract)
    )
    assert validation._uses_selection_visible_responsive_timeout_nonwitnesses(
        manifest(validation.V23_MANIFEST_ID, contract)
    )
    assert not validation._uses_selection_visible_responsive_timeout_nonwitnesses(
        manifest(validation.V20_MANIFEST_ID, contract)
    )
    assert not validation._uses_selection_visible_responsive_timeout_nonwitnesses(
        manifest(
            validation.FROZEN_MANIFEST_ID,
            validation.RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2,
        )
    )
    assert not validation._uses_selection_visible_responsive_timeout_nonwitnesses(
        manifest(validation.FROZEN_MANIFEST_ID, None)
    )
    assert not validation._uses_selection_visible_responsive_timeout_nonwitnesses(
        manifest(validation.FROZEN_MANIFEST_ID, contract + "-forged")
    )


def test_v22_through_v24_snapshot_selection_contract_dispatch_is_field_exact() -> None:
    contract = validation.EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1
    assert contract == (
        "full_prefix_without_inherited_consensus_wait_exempt_and_baseline_"
        "exclusive_suffix_with_exact_inherited_consensus_wait_exempt_v1"
    )

    def manifest(manifest_id: str, value: str | None) -> SimpleNamespace:
        responsive = SimpleNamespace()
        if value is not None:
            responsive.evidence_snapshot_selection_contract = value
        return SimpleNamespace(
            manifest_id=manifest_id,
            byzantine=SimpleNamespace(responsive_degradation=responsive),
        )

    assert validation._uses_evidence_snapshot_selection_contract(
        manifest(validation.FROZEN_MANIFEST_ID, contract)
    )
    assert validation._uses_evidence_snapshot_selection_contract(
        manifest(validation.V22_MANIFEST_ID, contract)
    )
    assert validation._uses_evidence_snapshot_selection_contract(
        manifest(validation.V23_MANIFEST_ID, contract)
    )
    assert not validation._uses_evidence_snapshot_selection_contract(
        manifest(validation.V21_MANIFEST_ID, contract)
    )
    assert not validation._uses_evidence_snapshot_selection_contract(
        manifest(validation.FROZEN_MANIFEST_ID, None)
    )
    assert not validation._uses_evidence_snapshot_selection_contract(
        manifest(validation.FROZEN_MANIFEST_ID, contract + "-forged")
    )


def test_v18_binds_guarded_selection_contract_to_runtime_identity_and_acceptance(
) -> None:
    manifest = load_frozen_manifest(V18_MANIFEST_PATH)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))
    contract = validation.PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1

    assert document["causal_acceptance"][
        "precontainment_guarded_selection_contract"
    ] == contract
    validation._validate_runtime_slot(
        document,
        expected_by_id[slot.slot_id],
        manifest,
    )

    v17 = load_frozen_manifest(V17_MANIFEST_PATH)
    v17_runtime = build_factorial_runtime(build_factorial_plan(v17))
    assert slot.artifact_id != v17_runtime.slots[0].artifact_id
    assert "precontainment_guarded_selection_contract" not in (
        v17_runtime.slots[0].causal_acceptance.as_document()
    )

    missing = copy.deepcopy(document)
    del missing["causal_acceptance"][
        "precontainment_guarded_selection_contract"
    ]
    with pytest.raises(FactorialValidationError, match="causal acceptance contract"):
        validation._validate_runtime_slot(
            missing,
            expected_by_id[slot.slot_id],
            manifest,
        )

    wrong_identity = copy.deepcopy(document)
    wrong_identity["artifact_id"] = v17_runtime.slots[0].artifact_id
    with pytest.raises(FactorialValidationError, match="runtime slot identity"):
        validation._validate_runtime_slot(
            wrong_identity,
            expected_by_id[slot.slot_id],
            manifest,
        )


def test_v19_binds_future_tree_contract_to_runtime_identity_and_acceptance() -> None:
    manifest = load_frozen_manifest(V19_MANIFEST_PATH)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))

    assert validation.FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1 == (
        FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT
    )
    assert document["causal_acceptance"][
        "future_tree_proposal_delivery_contract"
    ] == FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT
    validation._validate_runtime_slot(
        document,
        expected_by_id[slot.slot_id],
        manifest,
    )

    v18 = load_frozen_manifest(V18_MANIFEST_PATH)
    v18_runtime = build_factorial_runtime(build_factorial_plan(v18))
    assert slot.artifact_id != v18_runtime.slots[0].artifact_id
    assert "future_tree_proposal_delivery_contract" not in (
        v18_runtime.slots[0].causal_acceptance.as_document()
    )

    missing = copy.deepcopy(document)
    del missing["causal_acceptance"]["future_tree_proposal_delivery_contract"]
    with pytest.raises(FactorialValidationError, match="causal acceptance contract"):
        validation._validate_runtime_slot(
            missing,
            expected_by_id[slot.slot_id],
            manifest,
        )

    wrong_identity = copy.deepcopy(document)
    wrong_identity["artifact_id"] = v18_runtime.slots[0].artifact_id
    with pytest.raises(FactorialValidationError, match="runtime slot identity"):
        validation._validate_runtime_slot(
            wrong_identity,
            expected_by_id[slot.slot_id],
            manifest,
        )

    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    missing_manifest_contract = replace(
        manifest,
        byzantine=replace(
            manifest.byzantine,
            responsive_degradation=replace(
                responsive,
                future_tree_proposal_delivery_contract=None,
            ),
        ),
    )
    with pytest.raises(
        FactorialValidationError,
        match="future-tree proposal delivery contract",
    ):
        validation._validate_runtime_slot(
            document,
            expected_by_id[slot.slot_id],
            missing_manifest_contract,
        )


@pytest.mark.parametrize(
    "manifest_path",
    (
        V20_MANIFEST_PATH,
        V21_MANIFEST_PATH,
        V22_MANIFEST_PATH,
        V23_MANIFEST_PATH,
        MANIFEST_PATH,
    ),
)
def test_v20_through_v24_bind_source_bound_witness_to_runtime_identity_and_acceptance(
    manifest_path: Path,
) -> None:
    manifest = load_frozen_manifest(manifest_path)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))
    contract = validation.SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1

    assert document["causal_acceptance"][
        "source_bound_proposal_witness_contract"
    ] == contract
    validation._validate_runtime_slot(
        document,
        expected_by_id[slot.slot_id],
        manifest,
    )

    v19 = load_frozen_manifest(V19_MANIFEST_PATH)
    v19_runtime = build_factorial_runtime(build_factorial_plan(v19))
    assert slot.artifact_id != v19_runtime.slots[0].artifact_id
    assert "source_bound_proposal_witness_contract" not in (
        v19_runtime.slots[0].causal_acceptance.as_document()
    )

    missing = copy.deepcopy(document)
    del missing["causal_acceptance"][
        "source_bound_proposal_witness_contract"
    ]
    with pytest.raises(FactorialValidationError, match="causal acceptance contract"):
        validation._validate_runtime_slot(
            missing,
            expected_by_id[slot.slot_id],
            manifest,
        )

    wrong_identity = copy.deepcopy(document)
    wrong_identity["artifact_id"] = v19_runtime.slots[0].artifact_id
    with pytest.raises(FactorialValidationError, match="runtime slot identity"):
        validation._validate_runtime_slot(
            wrong_identity,
            expected_by_id[slot.slot_id],
            manifest,
        )

    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    missing_manifest_contract = replace(
        manifest,
        byzantine=replace(
            manifest.byzantine,
            responsive_degradation=replace(
                responsive,
                source_bound_proposal_witness_contract=None,
            ),
        ),
    )
    with pytest.raises(
        FactorialValidationError,
        match="source-bound proposal witness contract",
    ):
        validation._validate_runtime_slot(
            document,
            expected_by_id[slot.slot_id],
            missing_manifest_contract,
        )


@pytest.mark.parametrize(
    "manifest_path",
    (V22_MANIFEST_PATH, V23_MANIFEST_PATH, MANIFEST_PATH),
)
def test_v22_through_v24_bind_snapshot_selection_to_runtime_identity_and_acceptance(
    manifest_path: Path,
) -> None:
    manifest = load_frozen_manifest(manifest_path)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))
    contract = validation.EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1

    assert document["causal_acceptance"][
        "evidence_snapshot_selection_contract"
    ] == contract
    assert "evidence_snapshot_selection_contract" not in document[
        "tiered_cohorts"
    ]
    validation._validate_runtime_slot(
        document,
        expected_by_id[slot.slot_id],
        manifest,
    )

    v21 = load_frozen_manifest(V21_MANIFEST_PATH)
    v21_runtime = build_factorial_runtime(build_factorial_plan(v21))
    v21_document = json.loads(json.dumps(v21_runtime.slots[0].as_document()))
    assert "evidence_snapshot_selection_contract" not in v21_document[
        "causal_acceptance"
    ]
    assert slot.artifact_id != v21_runtime.slots[0].artifact_id

    missing = copy.deepcopy(document)
    del missing["causal_acceptance"][
        "evidence_snapshot_selection_contract"
    ]
    with pytest.raises(
        FactorialValidationError,
        match="causal acceptance contract",
    ):
        validation._validate_runtime_slot(
            missing,
            expected_by_id[slot.slot_id],
            manifest,
        )

    wrong_identity = copy.deepcopy(document)
    wrong_identity["artifact_id"] = v21_runtime.slots[0].artifact_id
    with pytest.raises(FactorialValidationError, match="runtime slot identity"):
        validation._validate_runtime_slot(
            wrong_identity,
            expected_by_id[slot.slot_id],
            manifest,
        )

    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    missing_manifest_contract = replace(
        manifest,
        byzantine=replace(
            manifest.byzantine,
            responsive_degradation=replace(
                responsive,
                evidence_snapshot_selection_contract=None,
            ),
        ),
    )
    with pytest.raises(
        FactorialValidationError,
        match="evidence snapshot selection contract",
    ):
        validation._validate_runtime_slot(
            document,
            expected_by_id[slot.slot_id],
            missing_manifest_contract,
        )

    forged_v21 = copy.deepcopy(v21_document)
    forged_v21["causal_acceptance"][
        "evidence_snapshot_selection_contract"
    ] = contract
    v21_expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(v21)
    }
    with pytest.raises(
        FactorialValidationError,
        match="causal acceptance contract",
    ):
        validation._validate_runtime_slot(
            forged_v21,
            v21_expected_by_id[v21_runtime.slots[0].slot_id],
            v21,
        )


def test_v24_binds_preselection_fields_to_runtime_identity_acceptance_and_transition() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))
    residency_field = "epoch1_preselection_residency_ms"
    opportunity_field = (
        "minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_"
        "actor_before_selection"
    )

    assert document["causal_acceptance"][residency_field] == 60_000
    assert document["causal_acceptance"][opportunity_field] == 82
    assert residency_field not in document["tiered_cohorts"]
    assert opportunity_field not in document["tiered_cohorts"]
    assert document["cutoff_contract"]["epoch1_stable_bucket_count"] == 6
    assert document["transitions"][1]["request"][
        "minimum_predecessor_residency_ms"
    ] == 60_000
    validation._validate_runtime_slot(
        document,
        expected_by_id[slot.slot_id],
        manifest,
    )

    for field in (residency_field, opportunity_field):
        missing = copy.deepcopy(document)
        del missing["causal_acceptance"][field]
        with pytest.raises(
            FactorialValidationError,
            match="causal acceptance contract",
        ):
            validation._validate_runtime_slot(
                missing,
                expected_by_id[slot.slot_id],
                manifest,
            )

    short = copy.deepcopy(document)
    short["transitions"][1]["request"][
        "minimum_predecessor_residency_ms"
    ] = 30_000
    with pytest.raises(FactorialValidationError, match="transition sequence"):
        validation._validate_runtime_slot(
            short,
            expected_by_id[slot.slot_id],
            manifest,
        )

    v23 = load_frozen_manifest(V23_MANIFEST_PATH)
    v23_runtime = build_factorial_runtime(build_factorial_plan(v23))
    v23_document = json.loads(json.dumps(v23_runtime.slots[0].as_document()))
    assert residency_field not in v23_document["causal_acceptance"]
    assert opportunity_field not in v23_document["causal_acceptance"]
    assert v23_document["transitions"][1]["request"][
        "minimum_predecessor_residency_ms"
    ] == 30_000
    assert slot.artifact_id != v23_runtime.slots[0].artifact_id


def test_v26_binds_duplicate_delivery_to_runtime_identity_and_rejects_v25_forgery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _v26_candidate_manifest(monkeypatch)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))
    field = "verified_response_duplicate_delivery_contract"

    assert document["causal_acceptance"][field] == (
        VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT
    )
    assert field not in document["tiered_cohorts"]
    validation._validate_runtime_slot(
        document,
        expected_by_id[slot.slot_id],
        manifest,
    )

    for value in (None, f"{VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT}-drift"):
        drifted = copy.deepcopy(document)
        if value is None:
            del drifted["causal_acceptance"][field]
        else:
            drifted["causal_acceptance"][field] = value
        with pytest.raises(
            FactorialValidationError,
            match="causal acceptance contract",
        ):
            validation._validate_runtime_slot(
                drifted,
                expected_by_id[slot.slot_id],
                manifest,
            )

    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    for value in (None, f"{VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT}-drift"):
        drifted_manifest = replace(
            manifest,
            byzantine=replace(
                manifest.byzantine,
                responsive_degradation=replace(
                    responsive,
                    verified_response_duplicate_delivery_contract=value,
                ),
            ),
        )
        with pytest.raises(
            FactorialValidationError,
            match="verified response duplicate delivery contract",
        ):
            validation._validate_runtime_slot(
                document,
                expected_by_id[slot.slot_id],
                drifted_manifest,
            )

    v25 = _v25_candidate_manifest(monkeypatch)
    v25_runtime = build_factorial_runtime(build_factorial_plan(v25))
    v25_slot = v25_runtime.slots[0]
    v25_document = json.loads(json.dumps(v25_slot.as_document()))
    assert field not in v25_document["causal_acceptance"]
    assert slot.artifact_id != v25_slot.artifact_id
    v25_document["causal_acceptance"][field] = (
        VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT
    )
    v25_expected = {
        expected.slot_id: expected
        for expected in validation._expected_slots(v25)
    }
    with pytest.raises(FactorialValidationError, match="causal acceptance contract"):
        validation._validate_runtime_slot(
            v25_document,
            v25_expected[v25_slot.slot_id],
            v25,
        )

    forged_v25_manifest = replace(
        v25,
        byzantine=replace(
            v25.byzantine,
            responsive_degradation=replace(
                v25.byzantine.responsive_degradation,
                verified_response_duplicate_delivery_contract=(
                    VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT
                ),
            ),
        ),
    )
    with pytest.raises(FactorialValidationError, match="v26-only"):
        validation._validate_runtime_slot(
            json.loads(json.dumps(v25_slot.as_document())),
            v25_expected[v25_slot.slot_id],
            forged_v25_manifest,
        )


def test_v27_binds_v2_contract_and_exact_timing_to_runtime_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _v27_candidate_manifest(monkeypatch)
    plan = build_factorial_plan(manifest)
    slot = plan.slots[0]
    runtime_slot = runtime_module.build_slot_runtime(slot)
    document = json.loads(json.dumps(runtime_slot.as_document()))
    expected = {
        item.slot_id: item for item in validation._expected_slots(manifest)
    }[slot.slot_id]
    field = "verified_response_duplicate_delivery_contract"

    assert manifest.byzantine.duration_s == 450
    assert manifest.common_timers.hard_timeout_s == 650
    assert document["fault_window"]["duration_s"] == 450
    assert document["fault_window"]["hard_timeout_s"] == 650
    assert document["causal_acceptance"][field] == (
        VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2
    )
    validation._validate_runtime_slot(document, expected, manifest)

    for value in (None, VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT):
        drifted_document = copy.deepcopy(document)
        if value is None:
            del drifted_document["causal_acceptance"][field]
        else:
            drifted_document["causal_acceptance"][field] = value
        with pytest.raises(
            FactorialValidationError,
            match="causal acceptance contract",
        ):
            validation._validate_runtime_slot(
                drifted_document,
                expected,
                manifest,
            )

    for timing_drift in (
        replace(slot, byzantine=replace(slot.byzantine, duration_s=300)),
        replace(
            slot,
            common_timers=replace(slot.common_timers, hard_timeout_s=500),
        ),
    ):
        drifted_artifact_id = runtime_module.build_slot_runtime(
            timing_drift
        ).artifact_id
        assert drifted_artifact_id != runtime_slot.artifact_id
        drifted_document = copy.deepcopy(document)
        drifted_document["artifact_id"] = drifted_artifact_id
        with pytest.raises(FactorialValidationError, match="runtime slot identity"):
            validation._validate_runtime_slot(
                drifted_document,
                expected,
                manifest,
            )

    v26 = _v26_candidate_manifest(monkeypatch)
    v26_slot = build_factorial_plan(v26).slots[0]
    v26_runtime_slot = runtime_module.build_slot_runtime(v26_slot)
    assert runtime_module.build_slot_runtime(
        replace(
            v26_slot,
            byzantine=replace(v26_slot.byzantine, duration_s=450),
        )
    ).artifact_id == v26_runtime_slot.artifact_id
    assert runtime_module.build_slot_runtime(
        replace(
            v26_slot,
            common_timers=replace(v26_slot.common_timers, hard_timeout_s=650),
        )
    ).artifact_id == v26_runtime_slot.artifact_id


def test_v19_rejects_forged_v20_source_bound_witness_runtime_field() -> None:
    manifest = load_frozen_manifest(V19_MANIFEST_PATH)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))
    document["causal_acceptance"][
        "source_bound_proposal_witness_contract"
    ] = validation.SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1

    with pytest.raises(FactorialValidationError, match="causal acceptance contract"):
        validation._validate_runtime_slot(
            document,
            expected_by_id[slot.slot_id],
            manifest,
        )


def test_v18_rejects_forged_v19_future_tree_runtime_field() -> None:
    manifest = load_frozen_manifest(V18_MANIFEST_PATH)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))
    document["causal_acceptance"][
        "future_tree_proposal_delivery_contract"
    ] = FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT

    with pytest.raises(FactorialValidationError, match="causal acceptance contract"):
        validation._validate_runtime_slot(
            document,
            expected_by_id[slot.slot_id],
            manifest,
        )


def test_v19_contract_does_not_bypass_epoch2_actor_opportunity_nonvacuity() -> None:
    responsive = SimpleNamespace(
        future_tree_proposal_delivery_contract=(
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT
        )
    )
    manifest = SimpleNamespace(
        manifest_id=validation.V19_MANIFEST_ID,
        byzantine=SimpleNamespace(responsive_degradation=responsive),
    )
    assert validation._uses_future_tree_proposal_delivery_contract(manifest)

    actor = 25
    tree = Tree(0, 2, 2, (0, actor, 1, 2, 3), ())
    phase_rows = (
        ("fault_evidence", 0, "11" * 32, 300),
        ("epoch1_stable", 1, "22" * 32, 900),
        ("epoch2_stable", 2, "33" * 32, 1_500),
    )
    phase_windows = {
        "fault_evidence": (100, 700, 6),
        "epoch1_stable": (700, 1_300, 6),
        "epoch2_stable": (1_300, 1_900, 6),
    }
    markers: list[FaultMarker] = []
    events: list[validation._NativeEvent] = []
    for phase_index, (_phase, epoch, digest, timestamp) in enumerate(
        phase_rows[:2]
    ):
        marker = _v14_marker(
            actor=actor,
            tree=tree,
            epoch_number=epoch,
            epoch_digest=digest,
            block_ordinal=phase_index + 1,
            monotonic_ns=timestamp,
            cohort="hard",
            contribution_ordinal=phase_index + 1,
            role_contribution_ordinal=phase_index + 1,
        )
        markers.append(marker)
        events.append(
            _v14_opportunity_event(
                marker,
                tree,
                sequence=phase_index + 1,
            )
        )

    with pytest.raises(
        FactorialValidationError,
        match=(
            "epoch2_stable scheduled actor 25 lacks a source-bound contribution "
            "opportunity in the frozen interior"
        ),
    ):
        validation._validate_fault_contribution_opportunity_bijection(
            markers=tuple(markers),
            opportunities=validation._fault_contribution_opportunities(
                {actor: tuple(events)}
            ),
            fault_actor_ids=(actor,),
            phase_windows=phase_windows,
            phase_configurations=tuple(
                (phase, epoch, digest, {tree.tree_id: tree})
                for phase, epoch, digest, _timestamp in phase_rows
            ),
        )


def test_v17_rejects_forged_v18_guarded_selection_runtime_field() -> None:
    manifest = load_frozen_manifest(V17_MANIFEST_PATH)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))
    document["causal_acceptance"][
        "precontainment_guarded_selection_contract"
    ] = validation.PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1

    with pytest.raises(FactorialValidationError, match="causal acceptance contract"):
        validation._validate_runtime_slot(
            document,
            expected_by_id[slot.slot_id],
            manifest,
        )


def test_v10_through_v24_route_through_explicit_causal_linkage_windows() -> None:
    assert not validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V9_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V10_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V11_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V12_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V13_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V14_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V15_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V16_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V17_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V18_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V19_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V20_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V21_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V22_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V23_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(MANIFEST_PATH)
    )


def test_v11_through_v24_route_through_explicit_phase_edge_eligibility() -> None:
    assert not validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V10_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V11_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V12_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V13_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V14_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V15_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V16_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V17_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V18_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V19_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V20_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V21_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V22_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V23_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(MANIFEST_PATH)
    )


def test_v16_through_v24_use_selection_visible_hard_timeout_witnesses() -> None:
    assert not validation._uses_selection_visible_hard_timeout_witnesses(
        load_frozen_manifest(V15_MANIFEST_PATH)
    )
    assert validation._uses_selection_visible_hard_timeout_witnesses(
        load_frozen_manifest(V16_MANIFEST_PATH)
    )
    assert validation._uses_selection_visible_hard_timeout_witnesses(
        load_frozen_manifest(V17_MANIFEST_PATH)
    )
    assert validation._uses_selection_visible_hard_timeout_witnesses(
        load_frozen_manifest(V18_MANIFEST_PATH)
    )
    assert validation._uses_selection_visible_hard_timeout_witnesses(
        load_frozen_manifest(V19_MANIFEST_PATH)
    )
    assert validation._uses_selection_visible_hard_timeout_witnesses(
        load_frozen_manifest(V20_MANIFEST_PATH)
    )
    assert validation._uses_selection_visible_hard_timeout_witnesses(
        load_frozen_manifest(V21_MANIFEST_PATH)
    )
    assert validation._uses_selection_visible_hard_timeout_witnesses(
        load_frozen_manifest(V22_MANIFEST_PATH)
    )
    assert validation._uses_selection_visible_hard_timeout_witnesses(
        load_frozen_manifest(V23_MANIFEST_PATH)
    )
    assert validation._uses_selection_visible_hard_timeout_witnesses(
        load_frozen_manifest(MANIFEST_PATH)
    )


def test_v14_through_v24_dispatch_source_bound_witnesses_and_sigint_cleanup() -> None:
    v13 = load_frozen_manifest(V13_MANIFEST_PATH)

    assert not validation._uses_source_bound_contribution_opportunities(v13)
    assert not validation._uses_strict_sigint_cleanup(v13)
    for manifest_path in (
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
    ):
        manifest = load_frozen_manifest(manifest_path)
        assert validation._uses_source_bound_contribution_opportunities(manifest)
        assert validation._uses_strict_sigint_cleanup(manifest)


def test_validator_binds_v14_through_v24_cleanup_without_changing_v13() -> None:
    for manifest_path in (
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
    ):
        manifest = load_frozen_manifest(manifest_path)
        runtime = build_factorial_runtime(build_factorial_plan(manifest))
        expected_by_id = {
            expected.slot_id: expected
            for expected in validation._expected_slots(manifest)
        }
        slot = runtime.slots[0]
        document = json.loads(json.dumps(slot.as_document()))
        if manifest_path != V13_MANIFEST_PATH:
            assert document.pop("cleanup_contract") == (
                validation.EXECUTION_CLEANUP_CONTRACT_V1
            )
            message = "cleanup contract"
        else:
            document["cleanup_contract"] = validation.EXECUTION_CLEANUP_CONTRACT_V1
            message = "legacy runtime"
        with pytest.raises(FactorialValidationError, match=message):
            validation._validate_runtime_slot(
                document,
                expected_by_id[slot.slot_id],
                manifest,
            )


@pytest.mark.parametrize(
    "field",
    (
        "causal_timeout_provenance_window",
        "causal_internal_witness_candidates",
        "causal_selection_linkage_window",
    ),
)
@pytest.mark.parametrize(
    "manifest_path",
    (
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
    ),
)
def test_validator_requires_each_explicit_causal_linkage_field(
    field: str,
    manifest_path: Path,
) -> None:
    manifest = load_frozen_manifest(manifest_path)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))
    del document["tiered_cohorts"][field]

    with pytest.raises(FactorialValidationError, match="tiered cohort contract"):
        validation._validate_runtime_slot(
            document,
            expected_by_id[slot.slot_id],
            manifest,
        )


@pytest.mark.parametrize(
    "field",
    ("marker_completeness_witness", "causal_timeout_eligibility"),
)
def test_validator_requires_each_explicit_v11_through_v24_phase_edge_field(
    field: str,
) -> None:
    for manifest_path in (
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
    ):
        manifest = load_frozen_manifest(manifest_path)
        runtime = build_factorial_runtime(build_factorial_plan(manifest))
        expected_by_id = {
            expected.slot_id: expected
            for expected in validation._expected_slots(manifest)
        }
        slot = runtime.slots[0]
        document = json.loads(json.dumps(slot.as_document()))
        del document["tiered_cohorts"][field]

        with pytest.raises(
            FactorialValidationError,
            match="tiered cohort contract",
        ):
            validation._validate_runtime_slot(
                document,
                expected_by_id[slot.slot_id],
                manifest,
            )


def _compact_snapshot_audit() -> dict[str, object]:
    return {
        "schema_version": 2,
        "cycle_ordinal": 1,
        "policy_intent": "performance_optimization",
        "transition_artifact_id": "slot-test-epoch2",
        "predecessor_epoch_number": 1,
        "predecessor_epoch_digest": "33" * 32,
        "activation_generation": 4_294_967_297,
        "baseline_cutoff": 1,
        "current_cutoff": 3,
        "full_prefix_snapshot_id": "44" * 32,
        "evidence_snapshot_id": "55" * 32,
        "accepted_prefix_count": 3,
        "eligible_ranking": [0, 1, 2],
    }


def test_compact_snapshot_schema_and_legacy_snapshot_schema_are_exact() -> None:
    compact = _compact_snapshot_audit()
    assert validation._validate_snapshot_audit_schema(
        compact,
        evidence_snapshot_format="digest_commitment_v2",
        label="test compact snapshot",
    ) is True

    legacy = {
        key: value
        for key, value in compact.items()
        if key
        not in {
            "schema_version",
            "full_prefix_snapshot_id",
            "evidence_snapshot_id",
            "accepted_prefix_count",
        }
    }
    legacy["observations"] = []
    assert validation._validate_snapshot_audit_schema(
        legacy,
        evidence_snapshot_format="full_prefix_v1",
        label="test legacy snapshot",
    ) is False

    with pytest.raises(FactorialValidationError, match="invalid field set"):
        validation._validate_snapshot_audit_schema(
            {**compact, "observations": []},
            evidence_snapshot_format="digest_commitment_v2",
            label="test compact snapshot",
        )
    with pytest.raises(FactorialValidationError, match="schema version"):
        validation._validate_snapshot_audit_schema(
            {**compact, "schema_version": 1},
            evidence_snapshot_format="digest_commitment_v2",
            label="test compact snapshot",
        )


def test_activation_generation_matches_native_epoch_packing() -> None:
    assert validation._expected_activation_generation(0) == 1
    assert validation._expected_activation_generation(1) == 4_294_967_297
    assert validation._expected_activation_generation(1, 7) == 4_294_967_304
    assert validation._expected_activation_generation(
        0xFFFF_FFFF, 0xFFFF_FFFE
    ) == 0xFFFF_FFFF_FFFF_FFFF
    assert validation._validated_activation_generation(
        4_294_967_297,
        predecessor_epoch_number=1,
        label="test generation",
    ) == 4_294_967_297

    with pytest.raises(FactorialValidationError, match="exceed uint32"):
        validation._expected_activation_generation(0x1_0000_0000)
    with pytest.raises(FactorialValidationError, match="overflows uint64"):
        validation._expected_activation_generation(0xFFFF_FFFF, 0xFFFF_FFFF)
    with pytest.raises(FactorialValidationError, match="integer"):
        validation._validated_activation_generation(
            True,
            predecessor_epoch_number=0,
            label="test generation",
        )
    with pytest.raises(FactorialValidationError, match="canonical predecessor"):
        validation._validated_activation_generation(
            2,
            predecessor_epoch_number=0,
            label="test generation",
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("accepted_prefix_count", 2, "prefix count"),
        ("full_prefix_snapshot_id", "66" * 32, "commitments"),
        ("evidence_snapshot_id", "77" * 32, "commitments"),
    ),
)
def test_compact_snapshot_rejects_count_or_commitment_tampering(
    field: str,
    value: object,
    match: str,
) -> None:
    snapshot = _compact_snapshot_audit()
    snapshot[field] = value

    with pytest.raises(FactorialValidationError, match=match):
        validation._validate_compact_snapshot_commitments(
            snapshot,
            accepted_prefix_count=3,
            current_cutoff=3,
            full_prefix_snapshot_id="44" * 32,
            evidence_snapshot_id="55" * 32,
        )


def _snapshot_selection_trees(
    *wait_exempt_sets: tuple[int, ...],
) -> tuple[Tree, ...]:
    memberships = (
        (0, 1, 2, 3, 4, 5, 6),
        (1, 2, 3, 0, 4, 5, 6),
    )
    return tuple(
        Tree(
            tree_id=index,
            fanout=2,
            pipeline_stretch=2,
            members=memberships[index],
            wait_exempt=wait_exempt,
        )
        for index, wait_exempt in enumerate(wait_exempt_sets)
    )


def _snapshot_selection_manifest(
    manifest_id: str,
    contract: str | None,
) -> SimpleNamespace:
    responsive = SimpleNamespace()
    if contract is not None:
        responsive.evidence_snapshot_selection_contract = contract
    return SimpleNamespace(
        manifest_id=manifest_id,
        byzantine=SimpleNamespace(responsive_degradation=responsive),
    )


def test_v22_snapshot_selection_uses_full_prefix_for_guarded_fallback() -> None:
    assert validation._snapshot_uses_suffix(
        _snapshot_selection_manifest(
            validation.FROZEN_MANIFEST_ID,
            validation.EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1,
        ),
        _snapshot_selection_trees((), ()),
        membership=tuple(range(7)),
        required_nonresponsive=2,
        policy_intent="performance_optimization",
    ) is False


def test_v22_snapshot_selection_uses_suffix_for_exact_inherited_constraints(
) -> None:
    assert validation._snapshot_uses_suffix(
        _snapshot_selection_manifest(
            validation.FROZEN_MANIFEST_ID,
            validation.EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1,
        ),
        _snapshot_selection_trees((4, 5), (4, 5)),
        membership=tuple(range(7)),
        required_nonresponsive=2,
        policy_intent="fault_containment",
    ) is True


def test_v21_snapshot_selection_preserves_policy_intent_dispatch() -> None:
    manifest = _snapshot_selection_manifest(
        validation.V21_MANIFEST_ID,
        validation.EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1,
    )
    assert validation._snapshot_uses_suffix(
        manifest,
        _snapshot_selection_trees((4, 5), (4, 5)),
        membership=tuple(range(7)),
        required_nonresponsive=2,
        policy_intent="fault_containment",
    ) is False
    assert validation._snapshot_uses_suffix(
        manifest,
        _snapshot_selection_trees((), ()),
        membership=tuple(range(7)),
        required_nonresponsive=2,
        policy_intent="performance_optimization",
    ) is True


@pytest.mark.parametrize(
    "trees",
    (
        _snapshot_selection_trees((), (4, 5)),
        _snapshot_selection_trees((4,), (4,)),
        _snapshot_selection_trees((1, 2), (1, 2)),
        _snapshot_selection_trees((4, 5), (4, 6)),
        _snapshot_selection_trees((5, 4), (5, 4)),
        _snapshot_selection_trees((4, 4), (4, 4)),
        _snapshot_selection_trees((4, 7), (4, 7)),
    ),
)
def test_v22_snapshot_selection_rejects_malformed_inherited_constraints(
    trees: tuple[Tree, ...],
) -> None:
    with pytest.raises(
        FactorialValidationError,
        match="inherited consensus wait-exempt",
    ):
        validation._snapshot_uses_inherited_constraint_suffix(
            trees,
            membership=tuple(range(7)),
            required_nonresponsive=2,
        )


def test_v22_snapshot_suffix_preserves_cross_baseline_late_normalization() -> None:
    timeout = replace(
        _sparse_evidence_record(1, reporter_sequence=1),
        observation_id="aa" * 32,
        outcome="timeout",
        response_duration_us=0,
        signer_set=(),
    )
    crossing_late = replace(
        timeout,
        ingestion_sequence=2,
        acceptance_monotonic_ns=102,
        outcome="late",
        response_duration_us=25,
        reporter_monotonic_ns=202,
        reporter_sequence=2,
    )
    fresh = _sparse_evidence_record(3, reporter_sequence=3)

    selected = validation._snapshot_records(
        (timeout, crossing_late, fresh),
        baseline_cutoff=1,
        current_cutoff=3,
        suffix_only=True,
    )

    assert selected == (fresh,)


def _sparse_evidence_record(
    ingestion_sequence: int,
    *,
    reporter_sequence: int,
) -> validation._EvidenceRecord:
    return validation._EvidenceRecord(
        ingestion_sequence=ingestion_sequence,
        acceptance_monotonic_ns=100 + ingestion_sequence,
        observation_id=f"{ingestion_sequence:064x}",
        reporter_id=0,
        target_id=1,
        epoch_number=0,
        tree_id=0,
        epoch_digest="11" * 32,
        block_hash=f"{1000 + ingestion_sequence:064x}",
        message_type="direct_vote",
        outcome="on_time",
        response_duration_us=10,
        deadline_duration_us=20,
        reporter_monotonic_ns=200 + ingestion_sequence,
        reporter_sequence=reporter_sequence,
        signer_set=(1,),
    )


def _scoring_policy(policy_version: str) -> dict[str, object]:
    return {
        "policy_version": policy_version,
        "attempt_window": 128,
        "minimum_attempts": 60,
        "minimum_response_rate_ppm": 950_000,
        "maximum_timeout_rate_ppm": 50_000,
        "trailing_timeout_streak": 7,
        "latency_percentile_basis_points": 5_000,
    }


def _scoring_evidence_record(
    ingestion_sequence: int,
    *,
    target_id: int,
    message_type: str,
    outcome: str,
) -> validation._EvidenceRecord:
    return validation._EvidenceRecord(
        ingestion_sequence=ingestion_sequence,
        acceptance_monotonic_ns=1_000 + ingestion_sequence,
        observation_id=f"{ingestion_sequence:064x}",
        reporter_id=(target_id + 1) % 3,
        target_id=target_id,
        epoch_number=1,
        tree_id=0,
        epoch_digest="11" * 32,
        block_hash=f"{10_000 + ingestion_sequence:064x}",
        message_type=message_type,
        outcome=outcome,
        response_duration_us=10 if outcome == "on_time" else 0,
        deadline_duration_us=20,
        reporter_monotonic_ns=900 + ingestion_sequence,
        reporter_sequence=ingestion_sequence,
        signer_set=(target_id,) if outcome == "on_time" else (),
    )


def _cohort_scoring_records() -> tuple[validation._EvidenceRecord, ...]:
    records: list[validation._EvidenceRecord] = []
    sequence = 1
    for target_id, direct_timeouts in ((0, 0), (1, 1), (2, 10)):
        for ordinal in range(60):
            records.append(
                _scoring_evidence_record(
                    sequence,
                    target_id=target_id,
                    message_type="direct_vote",
                    outcome=(
                        "timeout"
                        if ordinal >= 60 - direct_timeouts
                        else "on_time"
                    ),
                )
            )
            sequence += 1
    return tuple(records)


def test_v13_scoring_ignores_mixed_aggregate_noise_only_for_new_policy() -> None:
    records = _cohort_scoring_records()
    noisy = tuple(
        _scoring_evidence_record(
            len(records) + ordinal,
            target_id=0,
            message_type="aggregate_relay",
            outcome="timeout",
        )
        for ordinal in range(1, 9)
    )

    v13_scores = validation._score_snapshot(
        (*records, *noisy),
        3,
        _scoring_policy("shape25-direct-vote-responsiveness-v2"),
    )
    v12_scores = validation._score_snapshot(
        (*records, *noisy),
        3,
        _scoring_policy("shape25-sensitive-responsiveness-v1"),
    )

    v13_fast = next(score for score in v13_scores if score.replica_id == 0)
    v12_fast = next(score for score in v12_scores if score.replica_id == 0)
    assert (v13_fast.attempt_count, v13_fast.timeout_rate_ppm) == (60, 0)
    assert (v13_fast.classification, v13_fast.eligible) == ("responsive", True)
    assert (v12_fast.attempt_count, v12_fast.timeout_rate_ppm) == (68, 117_647)
    assert (v12_fast.classification, v12_fast.eligible) == (
        "nonresponsive",
        False,
    )


def test_v13_direct_vote_timeouts_produce_strict_cohort_ranking() -> None:
    scores = validation._score_snapshot(
        _cohort_scoring_records(),
        3,
        _scoring_policy("shape25-direct-vote-responsiveness-v2"),
    )

    assert tuple(score.replica_id for score in scores) == (0, 1, 2)
    assert (
        scores[0].classification,
        scores[0].attempt_count,
        scores[0].timeout_rate_ppm,
    ) == ("responsive", 60, 0)
    assert (
        scores[1].classification,
        scores[1].attempt_count,
        scores[1].timeout_rate_ppm,
    ) == ("responsive", 60, 16_666)
    assert (
        scores[2].classification,
        scores[2].attempt_count,
        scores[2].timeout_rate_ppm,
    ) == ("nonresponsive", 60, 166_666)


def test_v13_requires_minimum_direct_vote_attempts() -> None:
    direct = tuple(
        _scoring_evidence_record(
            ordinal,
            target_id=0,
            message_type="direct_vote",
            outcome="on_time",
        )
        for ordinal in range(1, 60)
    )
    aggregate = _scoring_evidence_record(
        60,
        target_id=0,
        message_type="aggregate_relay",
        outcome="on_time",
    )

    v13_score = validation._score_snapshot(
        (*direct, aggregate),
        1,
        _scoring_policy("shape25-direct-vote-responsiveness-v2"),
    )[0]
    v12_score = validation._score_snapshot(
        (*direct, aggregate),
        1,
        _scoring_policy("shape25-sensitive-responsiveness-v1"),
    )[0]

    assert (v13_score.attempt_count, v13_score.classification) == (
        59,
        "insufficient_evidence",
    )
    assert (v12_score.attempt_count, v12_score.classification) == (
        60,
        "responsive",
    )


def test_manifest_policy_routes_v12_pooled_and_v13_direct_vote_scoring() -> None:
    v12 = load_frozen_manifest(V12_MANIFEST_PATH)
    v13 = load_frozen_manifest(V13_MANIFEST_PATH)
    records = tuple(
        _scoring_evidence_record(
            ordinal,
            target_id=0,
            message_type="direct_vote",
            outcome="on_time",
        )
        for ordinal in range(1, 60)
    ) + (
        _scoring_evidence_record(
            60,
            target_id=0,
            message_type="aggregate_relay",
            outcome="on_time",
        ),
    )

    assert v12.responsiveness_policy.policy_version == (
        "shape25-sensitive-responsiveness-v1"
    )
    assert v13.responsiveness_policy.policy_version == (
        "shape25-direct-vote-responsiveness-v2"
    )
    v12_score = validation._score_snapshot(
        records,
        1,
        v12.responsiveness_policy.as_document(),
    )[0]
    v13_score = validation._score_snapshot(
        records,
        1,
        v13.responsiveness_policy.as_document(),
    )[0]
    assert (v12_score.attempt_count, v12_score.classification) == (
        60,
        "responsive",
    )
    assert (v13_score.attempt_count, v13_score.classification) == (
        59,
        "insufficient_evidence",
    )


def test_v13_snapshot_digest_still_binds_ignored_aggregate_records() -> None:
    direct, aggregate = (
        _scoring_evidence_record(
            ordinal,
            target_id=0,
            message_type=message_type,
            outcome="on_time",
        )
        for ordinal, message_type in (
            (1, "direct_vote"),
            (2, "aggregate_relay"),
        )
    )
    policy = _scoring_policy("shape25-direct-vote-responsiveness-v2")
    mutated_aggregate = replace(aggregate, response_duration_us=11)

    assert validation._score_snapshot((direct, aggregate), 1, policy) == (
        validation._score_snapshot((direct, mutated_aggregate), 1, policy)
    )
    common = {
        "replica_count": 1,
        "epoch_number": 1,
        "epoch_digest": "11" * 32,
        "cutoff": 2,
        "policy": policy,
    }
    assert validation._snapshot_id((direct, aggregate), **common) != (
        validation._snapshot_id((direct, mutated_aggregate), **common)
    )


def test_compact_snapshot_preserves_sparse_accepted_ledger_integrity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = (
        _sparse_evidence_record(1, reporter_sequence=1),
        _sparse_evidence_record(3, reporter_sequence=2),
    )
    events = tuple(
        _native_event(
            source_id="adaptive-manager",
            sequence=index,
            monotonic_ns=record.acceptance_monotonic_ns,
            event_type="evidence.observation_accepted",
            payload={"record_index": index - 1},
        )
        for index, record in enumerate(records, start=1)
    )
    monkeypatch.setattr(
        validation,
        "_evidence_record",
        lambda event, _membership: records[event.payload["record_index"]],
    )

    with pytest.raises(FactorialValidationError, match="non-contiguous"):
        validation._accepted_evidence(events, 4)
    grouped = validation._accepted_evidence(
        events,
        4,
        allow_ingestion_sequence_gaps=True,
    )
    assert grouped[(0, "11" * 32)] == records

    with pytest.raises(FactorialValidationError, match="exact accepted prefix"):
        validation._snapshot_records(
            records,
            baseline_cutoff=1,
            current_cutoff=4,
            suffix_only=False,
        )
    assert validation._snapshot_records(
        records,
        baseline_cutoff=1,
        current_cutoff=4,
        suffix_only=False,
        allow_high_watermark_gaps=True,
    ) == records


def test_epoch_command_signature_is_independently_verified() -> None:
    signing_bytes = (
        validation._AUTHORIZED_COMMAND_DOMAIN
        + (1).to_bytes(4, "big")
        + (2).to_bytes(1, "big")
        + (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + bytes.fromhex("11" * 32)
        + bytes.fromhex("22" * 32)
        + (5).to_bytes(8, "big")
    )
    private_key = 1
    nonce = 2
    nonce_point = validation._secp256k1_multiply(
        nonce,
        (validation._SECP256K1_GX, validation._SECP256K1_GY),
    )
    assert nonce_point is not None
    r = nonce_point[0] % validation._SECP256K1_ORDER
    z = int.from_bytes(hashlib.sha256(signing_bytes).digest(), "big")
    s = (
        pow(nonce, -1, validation._SECP256K1_ORDER)
        * (z + r * private_key)
    ) % validation._SECP256K1_ORDER
    if s > validation._SECP256K1_ORDER // 2:
        s = validation._SECP256K1_ORDER - s
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    public_key = "02" + f"{validation._SECP256K1_GX:064x}"

    validation._verify_secp256k1_signature(
        signing_bytes,
        signature,
        public_key,
    )
    tampered = bytearray(signature)
    tampered[0] ^= 1
    with pytest.raises(FactorialValidationError, match="signature"):
        validation._verify_secp256k1_signature(
            signing_bytes,
            bytes(tampered),
            public_key,
        )


def test_schedule_mismatch_is_rejected_by_independent_derivation() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = json.loads(canonical_plan_bytes(build_factorial_plan(manifest)))
    first = plan["execution_schedule"][0]
    first["arm_order"] = list(reversed(first["arm_order"]))

    with pytest.raises(FactorialValidationError, match="schedule differs"):
        validate_schedule_document(plan, manifest)


@pytest.mark.parametrize(
    "leak",
    (
        ("adaptation-manager", "--experiment-rotating-omission-actors", "9,10,12"),
        ("adaptation-manager",),
    ),
)
def test_manager_actor_truth_leak_is_rejected(leak: tuple[str, ...]) -> None:
    events = () if len(leak) > 1 else ({"actor_ids": [9, 10, 12]},)
    with pytest.raises(FactorialValidationError, match="actor truth"):
        validate_manager_blinding(leak, events)


def test_forged_selector_winner_is_rejected_even_with_refreshed_outer_digest() -> None:
    trees = tuple(
        Tree(
            tree_id=tree_id,
            fanout=2,
            pipeline_stretch=2,
            members=tuple((member + tree_id) % 4 for member in range(4)),
            wait_exempt=(),
        )
        for tree_id in range(3)
    )
    scores = tuple(
        ReplicaScore(
            replica_id=replica_id,
            classification="responsive",
            eligible=True,
            attempt_count=32,
            response_rate_ppm=1_000_000,
            timeout_rate_ppm=0,
            latency_percentile_us=100,
        )
        for replica_id in range(4)
    )
    arguments = {
        "epoch_number": 1,
        "epoch_digest": "12" * 32,
        "trees": trees,
        "scores": scores,
        "evidence_cutoff": 64,
        "candidate_fanouts": (2, 3),
        "tree_count": 3,
        "pipeline_stretch": 2,
        "deterministic_seed": 41719,
        "apply_selected": True,
    }
    valid = validation._recompute_shape_decision(**arguments)
    forged = copy.deepcopy(valid)
    forged["selected_fanout"] = 2 if valid["selected_fanout"] == 3 else 3
    forged["applied_fanout"] = forged["selected_fanout"]
    forged["decision_digest"] = validation._shape_decision_digest(forged)

    with pytest.raises(FactorialValidationError, match="independent selector"):
        validate_shape_decision(forged, **arguments)


def _cycle_audit_event(
    event_type: str,
    cycle: int,
    sequence: int,
) -> validation._NativeEvent:
    return replace(
        _native_event(
            source_id="adaptive-manager",
            sequence=sequence,
            monotonic_ns=sequence * 1_000,
            event_type=event_type,
            payload={"cycle_ordinal": cycle},
        ),
        source_kind="adaptation_manager",
    )


def _shape_contract_manifest(
    value: str | None,
    *,
    manifest_id: str = validation.V17_MANIFEST_ID,
) -> SimpleNamespace:
    responsive = SimpleNamespace()
    if value is not None:
        responsive.precontainment_shape_evaluation_contract = value
    return SimpleNamespace(
        manifest_id=manifest_id,
        byzantine=SimpleNamespace(responsive_degradation=responsive)
    )


def test_v17_cycle_zero_selection_uses_snapshot_without_shape_v1() -> None:
    contract = validation.PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1
    snapshot_zero = _cycle_audit_event("adaptive_v2_evidence_snapshot", 0, 10)
    snapshot_one = _cycle_audit_event("adaptive_v2_evidence_snapshot", 1, 20)
    shape_one = _cycle_audit_event("adaptive_v2_shape_decision", 1, 21)

    selection_events, shape_events = validation._adaptation_cycle_event_contract(
        snapshot_events=(snapshot_zero, snapshot_one),
        shape_events=(shape_one,),
        manifest=_shape_contract_manifest(contract),
    )

    assert selection_events == (snapshot_zero, shape_one)
    assert shape_events == {1: shape_one}


def test_v17_rejects_any_cycle_zero_shape_v1_decision() -> None:
    contract = validation.PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1
    snapshots = (
        _cycle_audit_event("adaptive_v2_evidence_snapshot", 0, 10),
        _cycle_audit_event("adaptive_v2_evidence_snapshot", 1, 20),
    )
    shapes = (
        _cycle_audit_event("adaptive_v2_shape_decision", 0, 11),
        _cycle_audit_event("adaptive_v2_shape_decision", 1, 21),
    )

    with pytest.raises(
        FactorialValidationError,
        match="zero cycle-0 shape decisions and one cycle-1 shape decision",
    ):
        validation._adaptation_cycle_event_contract(
            snapshot_events=snapshots,
            shape_events=shapes,
            manifest=_shape_contract_manifest(contract),
        )

    with pytest.raises(
        FactorialValidationError,
        match="zero cycle-0 shape decisions and one cycle-1 shape decision",
    ):
        validation._adaptation_cycle_event_contract(
            snapshot_events=snapshots,
            shape_events=(),
            manifest=_shape_contract_manifest(contract),
        )


def test_v16_contract_still_requires_one_shape_v1_event_per_cycle() -> None:
    snapshot_zero = _cycle_audit_event("adaptive_v2_evidence_snapshot", 0, 10)
    snapshot_one = _cycle_audit_event("adaptive_v2_evidence_snapshot", 1, 20)
    shape_zero = _cycle_audit_event("adaptive_v2_shape_decision", 0, 11)
    shape_one = _cycle_audit_event("adaptive_v2_shape_decision", 1, 21)

    selection_events, shape_events = validation._adaptation_cycle_event_contract(
        snapshot_events=(snapshot_zero, snapshot_one),
        shape_events=(shape_zero, shape_one),
        manifest=_shape_contract_manifest(
            None,
            manifest_id=validation.V16_MANIFEST_ID,
        ),
    )

    assert selection_events == (shape_zero, shape_one)
    assert shape_events == {0: shape_zero, 1: shape_one}

    with pytest.raises(
        FactorialValidationError,
        match="exactly one shape decision per cycle",
    ):
        validation._adaptation_cycle_event_contract(
            snapshot_events=(snapshot_zero, snapshot_one),
            shape_events=(shape_one,),
            manifest=_shape_contract_manifest(
                validation.PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1,
                manifest_id=validation.V16_MANIFEST_ID,
            ),
        )


def test_precontainment_preserved_fanout_must_match_live_and_configured() -> None:
    trees = (
        Tree(0, 5, 2, (0, 1, 2, 3), ()),
        Tree(1, 5, 2, (1, 2, 3, 0), ()),
    )
    assert (
        validation._validated_precontainment_preserved_fanout(
            trees,
            configured_fanout=5,
        )
        == 5
    )

    with pytest.raises(FactorialValidationError, match="configured current fanout"):
        validation._validated_precontainment_preserved_fanout(
            trees,
            configured_fanout=3,
        )
    with pytest.raises(FactorialValidationError, match="uniform predecessor fanout"):
        validation._validated_precontainment_preserved_fanout(
            (*trees[:1], replace(trees[1], fanout=3)),
            configured_fanout=5,
        )


def test_duplicate_authoritative_commit_is_rejected() -> None:
    windows = {
        phase: (1 + index * 5_000_000_000, 1 + (index + 1) * 5_000_000_000, 1)
        for index, phase in enumerate(validation.PHASES)
    }
    event = _commit_event(1, windows["baseline"][0] + 1, 1)
    duplicate = replace(event, source_sequence=2, line_number=2, monotonic_ns=event.monotonic_ns + 1)

    with pytest.raises(FactorialValidationError, match="duplicated authoritative"):
        validate_throughput_document(
            _throughput_document(windows),
            slot_id="slot-test",
            replica_events={0: (event, duplicate)},
            phase_windows=windows,
            phase_configurations=_throughput_phase_configurations(),
            bucket_width_s=5,
            observer_id="replica-0",
            observer_instance="slot-replica-0",
        )


def _v35_authoritative_commit_pair(
) -> tuple[validation._NativeEvent, validation._NativeEvent]:
    committed = _commit_event(2, 102, 1)
    observed = _commit_observed_event(
        committed,
        sequence=1,
        monotonic_ns=101,
    )
    return observed, committed


def _validate_v35_authoritative_commit_pair(
    events: tuple[validation._NativeEvent, ...],
) -> None:
    validation._validate_manifest_authoritative_observer_commit_completeness(
        validation.V35_MANIFEST_ID,
        events,
        observer_id="replica-0",
        observer_instance="slot-replica-0",
        tree_ids_by_configuration={(0, "11" * 32): (0, 1)},
    )


def test_v35_authoritative_observer_commit_bijection_accepts_exact_pair() -> None:
    observed, committed = _v35_authoritative_commit_pair()

    _validate_v35_authoritative_commit_pair((observed, committed))


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-committed",
        "missing-observed",
        "height",
        "hash",
        "parent",
        "transactions",
        "batch-index",
        "duplicate-observed",
        "duplicate-committed",
        "late-observed",
        "source-id",
        "source-instance",
        "source-path",
        "epoch",
        "tree",
        "configuration",
        "missing-generation",
        "generation-epoch",
        "generation-tree",
    ),
)
def test_v35_authoritative_observer_commit_bijection_is_fail_closed(
    mutation: str,
) -> None:
    observed, committed = _v35_authoritative_commit_pair()
    events = (observed, committed)
    if mutation == "missing-committed":
        events = (observed,)
    elif mutation == "missing-observed":
        events = (committed,)
    elif mutation == "height":
        observed = replace(
            observed,
            payload={**observed.payload, "block_height": 2},
        )
        events = (observed, committed)
    elif mutation == "hash":
        observed = replace(
            observed,
            payload={**observed.payload, "block_hash": "99" * 32},
        )
        events = (observed, committed)
    elif mutation == "parent":
        observed = replace(
            observed,
            payload={**observed.payload, "parent_hash": "99" * 32},
        )
        events = (observed, committed)
    elif mutation == "transactions":
        observed = replace(
            observed,
            payload={**observed.payload, "transaction_count": 999},
        )
        events = (observed, committed)
    elif mutation == "batch-index":
        observed = replace(
            observed,
            payload={**observed.payload, "commit_batch_index": 1},
        )
        events = (observed, committed)
    elif mutation == "duplicate-observed":
        duplicate = replace(
            observed,
            source_sequence=3,
            line_number=3,
            monotonic_ns=103,
        )
        events = (observed, committed, duplicate)
    elif mutation == "duplicate-committed":
        duplicate = replace(
            committed,
            source_sequence=3,
            line_number=3,
            monotonic_ns=103,
        )
        events = (observed, committed, duplicate)
    elif mutation == "late-observed":
        observed = replace(
            observed,
            source_sequence=3,
            line_number=3,
            monotonic_ns=103,
        )
        events = (committed, observed)
    elif mutation == "source-id":
        observed = replace(observed, source_id="replica-1")
        events = (observed, committed)
    elif mutation == "source-instance":
        observed = replace(observed, source_instance="other-instance")
        events = (observed, committed)
    elif mutation == "source-path":
        observed = replace(observed, relative_path="raw/renamed-observer.jsonl")
        events = (observed, committed)
    elif mutation in {"epoch", "tree", "configuration"}:
        proof = dict(committed.payload["decision_proof"])
        if mutation == "epoch":
            proof["epoch_number"] = 1
        elif mutation == "tree":
            proof["tree_id"] = 2
        else:
            proof["epoch_digest"] = "22" * 32
        committed = replace(
            committed,
            payload={**committed.payload, "decision_proof": proof},
        )
        events = (observed, committed)
    elif mutation == "missing-generation":
        committed = replace(
            committed,
            payload={**committed.payload, "view_generation": None},
        )
        events = (observed, committed)
    elif mutation == "generation-epoch":
        committed = replace(
            committed,
            payload={**committed.payload, "view_generation": (1 << 32) + 1},
        )
        events = (observed, committed)
    else:
        committed = replace(
            committed,
            payload={**committed.payload, "view_generation": 2},
        )
        events = (observed, committed)

    with pytest.raises(FactorialValidationError, match="authoritative observer"):
        _validate_v35_authoritative_commit_pair(events)


def test_v35_authoritative_completeness_dispatch_is_exact_and_historical_v34_is_unchanged(
) -> None:
    observed, _committed = _v35_authoritative_commit_pair()
    assert validation._uses_authoritative_observer_commit_completeness(
        validation.V35_MANIFEST_ID
    )
    assert validation._uses_authoritative_observer_commit_completeness(
        validation.V37_MANIFEST_ID
    )
    assert validation._uses_authoritative_observer_commit_completeness(
        validation.FROZEN_MANIFEST_ID
    )
    assert not validation._uses_authoritative_observer_commit_completeness(
        validation.V34_MANIFEST_ID
    )
    assert not validation._uses_authoritative_observer_commit_completeness(
        validation.V33_MANIFEST_ID
    )
    validation._validate_manifest_authoritative_observer_commit_completeness(
        validation.V34_MANIFEST_ID,
        (observed,),
        observer_id="replica-0",
        observer_instance="slot-replica-0",
        tree_ids_by_configuration={(0, "11" * 32): (0, 1)},
    )


def test_v35_authoritative_completeness_blocks_renamed_poison_bypass(
    tmp_path: Path,
) -> None:
    relative = "raw/process/replica-0.stderr.log"
    log = tmp_path / relative
    log.parent.mkdir(parents=True)
    log.write_text(
        "[EPOCH] Authoritative commit identity unavailable after commit\n",
        encoding="utf-8",
    )
    assert validation._relay_ingress_witnesses(
        tmp_path,
        {0: (relative,)},
        allow_shared_outbox_after_convergence=True,
    ) == ((), ())
    observed, _committed = _v35_authoritative_commit_pair()

    with pytest.raises(FactorialValidationError, match="authoritative observer"):
        _validate_v35_authoritative_commit_pair((observed,))


@pytest.mark.parametrize(
    ("line", "accepted"),
    (
        (
            "[EPOCH] Adaptive-v2 convergence evidence unhealthy: "
            "shared_outbox_delivery_failed\n",
            True,
        ),
        (
            "[EPOCH] Adaptive-v2 convergence evidence unhealthy: "
            "authoritative_commit_identity_missing_or_mismatched\n",
            False,
        ),
        (
            "[EPOCH] Adaptive-v2 convergence evidence unhealthy: "
            "shared_outbox_delivery_failed trailing\n",
            False,
        ),
        (
            "KAURI_FAULT Adaptive-v2 convergence evidence unhealthy: "
            "shared_outbox_delivery_failed\n",
            False,
        ),
    ),
)
def test_v35_convergence_poison_allowlist_remains_exact(
    tmp_path: Path,
    line: str,
    accepted: bool,
) -> None:
    relative = "raw/process/replica-0.stderr.log"
    log = tmp_path / relative
    log.parent.mkdir(parents=True)
    log.write_text(line, encoding="utf-8")
    if accepted:
        assert validation._relay_ingress_witnesses(
            tmp_path,
            {0: (relative,)},
            allow_shared_outbox_after_convergence=True,
        ) == ((), ())
        return

    with pytest.raises(FactorialValidationError, match="unknown convergence poison"):
        validation._relay_ingress_witnesses(
            tmp_path,
            {0: (relative,)},
            allow_shared_outbox_after_convergence=True,
        )


def _v36_commit_identity_streams(
    *,
    rich_reporters: tuple[int, ...] = (0, 1, 2),
    gap_reporters: tuple[int, ...] = (3,),
) -> dict[int, tuple[validation._NativeEvent, ...]]:
    streams: dict[int, tuple[validation._NativeEvent, ...]] = {}
    for replica_id in range(4):
        committed = _commit_event(
            2,
            110 + replica_id,
            7,
            replica_id=replica_id,
        )
        observed = _commit_observed_event(
            committed,
            sequence=1,
            monotonic_ns=101 + replica_id,
        )
        if replica_id in rich_reporters:
            streams[replica_id] = (observed, committed)
        elif replica_id in gap_reporters:
            streams[replica_id] = (
                observed,
                _commit_identity_unavailable_event(
                    observed,
                    sequence=2,
                    monotonic_ns=110 + replica_id,
                ),
            )
        else:
            streams[replica_id] = (observed,)
    return streams


def _validate_v36_commit_identity_streams(
    streams: dict[int, tuple[validation._NativeEvent, ...]],
    *,
    manifest_id: str = validation.V36_MANIFEST_ID,
) -> None:
    validation._validate_manifest_commit_identity_unavailable(
        manifest_id,
        streams,
        replica_count=4,
        observer_id="replica-0",
        observer_instance="slot-replica-0",
        final_cycle_terminal_ns=100,
        hard_deadline_ns=200,
    )


@pytest.mark.parametrize(
    "manifest_id",
    (
        validation.V36_MANIFEST_ID,
        validation.V37_MANIFEST_ID,
        validation.FROZEN_MANIFEST_ID,
    ),
)
def test_v36_plus_commit_identity_unavailable_accepts_exact_post_terminal_q_proof(
    manifest_id: str,
) -> None:
    _validate_v36_commit_identity_streams(
        _v36_commit_identity_streams(),
        manifest_id=manifest_id,
    )


def test_v36_commit_identity_unavailable_accepts_zero_gap_markers() -> None:
    _validate_v36_commit_identity_streams(
        _v36_commit_identity_streams(
            rich_reporters=(0, 1, 2, 3),
            gap_reporters=(),
        )
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("pre-terminal", "strictly follow.*terminal"),
        ("equal-terminal", "strictly follow.*terminal"),
        ("post-hard-deadline", "strictly precede.*hard deadline"),
        ("observer", "designated observer"),
        ("sub-quorum", "quorum"),
        ("missing-all-source", "every configured replica"),
        ("observation-conflict", "same observed commit tuple"),
        ("configuration-drift", "configuration/generation"),
        ("generation-drift", "configuration/generation"),
        ("missing-marker", "lacks.*unavailable marker"),
        ("duplicate-marker", "unavailable marker is duplicated"),
        ("nonadjacent-marker", "immediate same-source successor"),
        ("malformed-marker", "invalid field set"),
        ("height-bound", "block_height"),
        ("height-overflow", "block_height.*uint64"),
        ("hash-bound", "block_hash"),
        ("parent-bound", "parent_hash"),
        ("transaction-bound", "transaction_count"),
        ("batch-bound", "commit_batch_index"),
        ("batch-overflow", "commit_batch_index.*uint64"),
        ("pending-marker", "pending disposition"),
        ("reason-drift", "reason"),
        ("source-mismatch", "invalid exact reporter"),
        ("rich-gap-coexistence", "both rich and unavailable"),
        ("marker-mismatch", "same observed commit tuple"),
        ("duplicate-observed", "exactly one.*observation"),
        ("duplicate-rich", "duplicated a rich commit proof"),
    ),
)
def test_v36_commit_identity_unavailable_is_fail_closed(
    mutation: str,
    reason: str,
) -> None:
    streams = _v36_commit_identity_streams()
    marker = streams[3][1]
    if mutation in {"pre-terminal", "equal-terminal"}:
        marker = replace(
            marker,
            monotonic_ns=99 if mutation == "pre-terminal" else 100,
        )
        streams[3] = (streams[3][0], marker)
    elif mutation == "post-hard-deadline":
        streams[3] = (
            streams[3][0],
            replace(marker, monotonic_ns=201),
        )
    elif mutation == "observer":
        streams = _v36_commit_identity_streams(
            rich_reporters=(1, 2, 3),
            gap_reporters=(0,),
        )
    elif mutation == "sub-quorum":
        streams = _v36_commit_identity_streams(
            rich_reporters=(0, 1),
            gap_reporters=(2, 3),
        )
    elif mutation == "missing-all-source":
        streams[2] = ()
    elif mutation == "observation-conflict":
        observed, committed = streams[2]
        streams[2] = (
            replace(
                observed,
                payload={**observed.payload, "block_hash": "99" * 32},
            ),
            committed,
        )
    elif mutation in {"configuration-drift", "generation-drift"}:
        observed, committed = streams[2]
        payload = dict(committed.payload)
        if mutation == "configuration-drift":
            payload["decision_proof"] = {
                **payload["decision_proof"],
                "epoch_digest": "22" * 32,
            }
        else:
            payload["view_generation"] = 2
        streams[2] = (observed, replace(committed, payload=payload))
    elif mutation == "missing-marker":
        streams[3] = (streams[3][0],)
    elif mutation == "duplicate-marker":
        streams[3] = (
            *streams[3],
            replace(
                marker,
                source_sequence=3,
                line_number=3,
                monotonic_ns=marker.monotonic_ns + 1,
            ),
        )
    elif mutation == "nonadjacent-marker":
        intervening = _native_event(
            source_id="replica-3",
            sequence=2,
            monotonic_ns=marker.monotonic_ns - 1,
            event_type="test.intervening",
            payload={},
        )
        streams[3] = (
            streams[3][0],
            intervening,
            replace(marker, source_sequence=3, line_number=3),
        )
    elif mutation == "malformed-marker":
        streams[3] = (
            streams[3][0],
            replace(marker, payload={**marker.payload, "extra": 1}),
        )
    elif mutation in {
        "height-bound",
        "height-overflow",
        "hash-bound",
        "parent-bound",
        "transaction-bound",
        "batch-bound",
        "batch-overflow",
    }:
        field, value = {
            "height-bound": ("block_height", 0),
            "height-overflow": ("block_height", 1 << 64),
            "hash-bound": ("block_hash", "not-a-digest"),
            "parent-bound": ("parent_hash", "not-a-digest"),
            "transaction-bound": ("transaction_count", 1 << 64),
            "batch-bound": ("commit_batch_index", -1),
            "batch-overflow": ("commit_batch_index", 1 << 64),
        }[mutation]
        streams[3] = (
            streams[3][0],
            replace(marker, payload={**marker.payload, field: value}),
        )
    elif mutation == "pending-marker":
        streams[3] = (
            streams[3][0],
            replace(
                marker,
                payload={
                    **marker.payload,
                    "convergence_identity_pending": True,
                },
            ),
        )
    elif mutation == "reason-drift":
        streams[3] = (
            streams[3][0],
            replace(marker, payload={**marker.payload, "reason": "legal_qc_skip"}),
        )
    elif mutation == "source-mismatch":
        streams[3] = (
            streams[3][0],
            replace(marker, source_id="replica-2"),
        )
    elif mutation == "rich-gap-coexistence":
        rich = _commit_event(3, marker.monotonic_ns + 1, 7, replica_id=3)
        streams[3] = (*streams[3], rich)
    elif mutation == "marker-mismatch":
        streams[3] = (
            streams[3][0],
            replace(
                marker,
                payload={**marker.payload, "block_hash": "99" * 32},
            ),
        )
    elif mutation == "duplicate-rich":
        observed, committed = streams[2]
        streams[2] = (
            observed,
            committed,
            replace(
                committed,
                source_sequence=3,
                line_number=3,
                monotonic_ns=committed.monotonic_ns + 1,
            ),
        )
    else:
        observed = streams[3][0]
        duplicate = replace(
            observed,
            source_sequence=2,
            line_number=2,
            monotonic_ns=observed.monotonic_ns + 1,
        )
        streams[3] = (
            observed,
            duplicate,
            replace(marker, source_sequence=3, line_number=3),
        )

    with pytest.raises(FactorialValidationError, match=reason):
        _validate_v36_commit_identity_streams(streams)


def test_v35_rejects_v36_commit_identity_unavailable_marker() -> None:
    with pytest.raises(
        FactorialValidationError,
        match=r"only permitted by exact v36\+ evidence contracts",
    ):
        _validate_v36_commit_identity_streams(
            _v36_commit_identity_streams(),
            manifest_id=validation.V35_MANIFEST_ID,
        )


def test_v36_rejects_rich_commit_without_same_source_observation() -> None:
    streams = _v36_commit_identity_streams(
        rich_reporters=(0, 1, 2, 3),
        gap_reporters=(),
    )
    streams[2] = (streams[2][1],)

    with pytest.raises(
        FactorialValidationError,
        match="rich commit proof lacks exactly one same-source observation",
    ):
        _validate_v36_commit_identity_streams(streams)


@pytest.mark.parametrize(
    ("evidence", "monotonic_ns", "reason"),
    (
        ("observation", 200, "matching observation.*strictly precede"),
        ("observation", 201, "matching observation.*strictly precede"),
        ("rich-proof", 200, "rich commit proof.*strictly precede"),
        ("rich-proof", 201, "rich commit proof.*strictly precede"),
    ),
)
def test_v36_gap_rejects_supporting_evidence_at_or_after_hard_deadline(
    evidence: str,
    monotonic_ns: int,
    reason: str,
) -> None:
    streams = _v36_commit_identity_streams()
    observed, committed = streams[2]
    if evidence == "observation":
        streams[2] = (
            replace(observed, monotonic_ns=monotonic_ns),
            replace(committed, monotonic_ns=monotonic_ns + 1),
        )
    else:
        streams[2] = (observed, replace(committed, monotonic_ns=monotonic_ns))

    with pytest.raises(FactorialValidationError, match=reason):
        _validate_v36_commit_identity_streams(streams)


_V38_GAP_HEIGHT = 7
_V38_GAP_HASH = f"{_V38_GAP_HEIGHT:064x}"
_V38_GAP_PARENT = f"{_V38_GAP_HEIGHT - 1:064x}"
_V38_GAP_DIGEST = "33" * 32
_V38_GAP_GENERATION = (2 << 32) + 6
_V38_GAP_REPORTERS = (0, 1, 15, 18, 26)


def _v38_rich_commit(
    replica_id: int,
    *,
    height: int,
    sequence: int,
    monotonic_ns: int,
    transactions: int,
) -> validation._NativeEvent:
    event = _commit_event(
        sequence,
        monotonic_ns,
        height,
        epoch_number=2,
        epoch_digest=_V38_GAP_DIGEST,
        tree_id=5,
        replica_id=replica_id,
    )
    return replace(
        event,
        payload={
            **event.payload,
            "transaction_count": transactions,
            "view_generation": _V38_GAP_GENERATION,
        },
    )


def _v38_observer_zero_gap_streams(
    *,
    gap_reporters: tuple[int, ...] = _V38_GAP_REPORTERS,
) -> dict[int, tuple[validation._NativeEvent, ...]]:
    streams: dict[int, tuple[validation._NativeEvent, ...]] = {}
    for replica_id in range(31):
        gap_commit = _v38_rich_commit(
            replica_id,
            height=_V38_GAP_HEIGHT,
            sequence=4 if replica_id == 0 else 2,
            monotonic_ns=111 + replica_id,
            transactions=0,
        )
        gap_observed = _commit_observed_event(
            gap_commit,
            sequence=3 if replica_id == 0 else 1,
            monotonic_ns=110 + replica_id,
        )
        disposition = (
            _commit_identity_unavailable_event(
                gap_observed,
                sequence=gap_commit.source_sequence,
                monotonic_ns=gap_commit.monotonic_ns,
            )
            if replica_id in gap_reporters
            else gap_commit
        )
        if replica_id != 0:
            streams[replica_id] = (gap_observed, disposition)
            continue

        predecessor = _v38_rich_commit(
            0,
            height=_V38_GAP_HEIGHT - 1,
            sequence=2,
            monotonic_ns=102,
            transactions=1000,
        )
        successor = _v38_rich_commit(
            0,
            height=_V38_GAP_HEIGHT + 1,
            sequence=6,
            monotonic_ns=121,
            transactions=1000,
        )
        streams[0] = (
            _commit_observed_event(
                predecessor,
                sequence=1,
                monotonic_ns=101,
            ),
            predecessor,
            gap_observed,
            disposition,
            _commit_observed_event(
                successor,
                sequence=5,
                monotonic_ns=120,
            ),
            successor,
        )
    return streams


def _v38_gap_key() -> tuple[int, str, str, int, int]:
    return (
        _V38_GAP_HEIGHT,
        _V38_GAP_HASH,
        _V38_GAP_PARENT,
        0,
        0,
    )


def _validate_v38_commit_identity_streams(
    streams: dict[int, tuple[validation._NativeEvent, ...]],
    *,
    manifest_id: str | None = None,
    protected_block_identities: frozenset[tuple[int, str]] = frozenset(),
) -> tuple[int, str, str | None, int, int] | None:
    selected_manifest_id = (
        validation.V38_MANIFEST_ID if manifest_id is None else manifest_id
    )
    return validation._validate_manifest_commit_identity_evidence(
        selected_manifest_id,
        streams,
        replica_count=31,
        observer_id="replica-0",
        observer_instance="slot-replica-0",
        tree_ids_by_configuration={
            (2, _V38_GAP_DIGEST): tuple(range(21)),
        },
        final_cycle_terminal_ns=100,
        hard_deadline_ns=1_000,
        protected_block_identities=protected_block_identities,
    )


def _replace_payload_field(
    event: validation._NativeEvent,
    field: str,
    value: object,
) -> validation._NativeEvent:
    payload = {**event.payload, field: value}
    if event.event_type == "block.committed" and field == "block_hash":
        payload["decision_proof"] = {
            **event.payload["decision_proof"],
            "block_hash": value,
        }
    return replace(event, payload=payload)


def test_v38_accepts_exact_post_terminal_zero_transaction_observer_gap() -> None:
    streams = _v38_observer_zero_gap_streams()

    assert _validate_v38_commit_identity_streams(streams) == _v38_gap_key()


def test_v38_production_commit_identity_orchestration_is_gap_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    allowed_key = _v38_gap_key()
    protected = frozenset({(_V38_GAP_HEIGHT, _V38_GAP_HASH)})

    def validate_gaps(*_args: object, **kwargs: object):
        calls.append("gaps")
        assert kwargs["protected_block_identities"] == protected
        return allowed_key

    def validate_observer(*_args: object, **kwargs: object) -> None:
        calls.append("observer")
        assert kwargs["allowed_unavailable_key"] == allowed_key

    monkeypatch.setattr(
        validation,
        "_validate_manifest_commit_identity_unavailable",
        validate_gaps,
    )
    monkeypatch.setattr(
        validation,
        "_validate_manifest_authoritative_observer_commit_completeness",
        validate_observer,
    )

    assert validation._validate_manifest_commit_identity_evidence(
        validation.V38_MANIFEST_ID,
        {0: ()},
        replica_count=1,
        observer_id="replica-0",
        observer_instance="slot-replica-0",
        tree_ids_by_configuration={},
        final_cycle_terminal_ns=100,
        hard_deadline_ns=1_000,
        protected_block_identities=protected,
    ) == allowed_key
    assert calls == ["gaps", "observer"]


@pytest.mark.parametrize(
    "manifest_name",
    ("V35_MANIFEST_ID", "V36_MANIFEST_ID", "V37_MANIFEST_ID"),
)
def test_v35_through_v37_reject_v38_observer_gap(manifest_name: str) -> None:
    with pytest.raises(FactorialValidationError):
        _validate_v38_commit_identity_streams(
            _v38_observer_zero_gap_streams(),
            manifest_id=getattr(validation, manifest_name),
        )


def test_v37_official_observer_gap_failure_reason_is_preserved() -> None:
    with pytest.raises(
        FactorialValidationError,
        match="authoritative observer commit observation lacks exactly one committed proof",
    ):
        validation._validate_manifest_commit_identity_evidence(
            validation.V37_MANIFEST_ID,
            _v38_observer_zero_gap_streams(),
            replica_count=31,
            observer_id="replica-0",
            observer_instance="slot-replica-0",
            tree_ids_by_configuration={
                (2, _V38_GAP_DIGEST): tuple(range(21)),
            },
            final_cycle_terminal_ns=100,
            hard_deadline_ns=1_000,
        )


def test_v38_observer_gap_rejects_positive_transactions() -> None:
    streams = _v38_observer_zero_gap_streams()
    for replica_id, events in streams.items():
        streams[replica_id] = tuple(
            _replace_payload_field(event, "transaction_count", 1)
            if event.payload.get("block_height") == _V38_GAP_HEIGHT
            else event
            for event in events
        )

    with pytest.raises(FactorialValidationError, match="zero transactions"):
        _validate_v38_commit_identity_streams(streams)


def test_v38_rejects_a_second_designated_observer_zero_gap() -> None:
    streams = _v38_observer_zero_gap_streams()
    second_commit = _v38_rich_commit(
        0,
        height=10,
        sequence=8,
        monotonic_ns=131,
        transactions=0,
    )
    second_observed = _commit_observed_event(
        second_commit,
        sequence=7,
        monotonic_ns=130,
    )
    streams[0] = (
        *streams[0],
        second_observed,
        _commit_identity_unavailable_event(
            second_observed,
            sequence=8,
            monotonic_ns=131,
        ),
    )

    with pytest.raises(FactorialValidationError, match="at most one"):
        _validate_v38_commit_identity_streams(streams)


@pytest.mark.parametrize(
    ("monotonic_ns", "reason"),
    ((100, "strictly follow"), (1_000, "strictly precede")),
)
def test_v38_observer_gap_rejects_terminal_and_hard_equalities(
    monotonic_ns: int,
    reason: str,
) -> None:
    streams = _v38_observer_zero_gap_streams()
    observer = list(streams[0])
    observer[3] = replace(observer[3], monotonic_ns=monotonic_ns)
    streams[0] = tuple(observer)

    with pytest.raises(FactorialValidationError, match=reason):
        _validate_v38_commit_identity_streams(streams)


@pytest.mark.parametrize("duplicate_source", (False, True))
def test_v38_observer_gap_requires_derived_distinct_source_quorum(
    duplicate_source: bool,
) -> None:
    streams = _v38_observer_zero_gap_streams(
        gap_reporters=tuple(range(11)),
    )
    if duplicate_source:
        duplicate = streams[11][1]
        streams[11] = (
            *streams[11],
            replace(
                duplicate,
                source_sequence=3,
                line_number=3,
                monotonic_ns=duplicate.monotonic_ns + 1,
            ),
        )

    with pytest.raises(FactorialValidationError, match="rich commit quorum"):
        _validate_v38_commit_identity_streams(streams)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("missing", "every configured replica"),
        ("duplicate", "exactly one.*observation"),
        ("conflicting", "same observed commit tuple"),
    ),
)
def test_v38_observer_gap_requires_one_matching_observation_per_replica(
    mutation: str,
    reason: str,
) -> None:
    streams = _v38_observer_zero_gap_streams()
    if mutation == "missing":
        streams[30] = ()
    elif mutation == "duplicate":
        observed, committed = streams[2]
        streams[2] = (
            observed,
            replace(
                observed,
                source_sequence=2,
                line_number=2,
                monotonic_ns=observed.monotonic_ns + 1,
            ),
            replace(committed, source_sequence=3, line_number=3),
        )
    else:
        observed, committed = streams[2]
        streams[2] = (
            _replace_payload_field(observed, "block_hash", "99" * 32),
            committed,
        )

    with pytest.raises(FactorialValidationError, match=reason):
        _validate_v38_commit_identity_streams(streams)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("block_height", 9),
        ("block_hash", "99" * 32),
        ("parent_hash", "88" * 32),
        ("transaction_count", 1),
        ("commit_batch_index", 1),
    ),
)
def test_v38_observer_gap_rejects_rich_commit_tuple_drift(
    field: str,
    value: object,
) -> None:
    streams = _v38_observer_zero_gap_streams()
    observed, committed = streams[2]
    streams[2] = (observed, _replace_payload_field(committed, field, value))

    with pytest.raises(FactorialValidationError):
        _validate_v38_commit_identity_streams(streams)


@pytest.mark.parametrize(
    "field",
    ("epoch_number", "tree_id", "epoch_digest", "view_generation"),
)
def test_v38_observer_gap_rejects_rich_configuration_generation_drift(
    field: str,
) -> None:
    streams = _v38_observer_zero_gap_streams()
    observed, committed = streams[2]
    payload = dict(committed.payload)
    if field == "view_generation":
        payload[field] = _V38_GAP_GENERATION + 1
    else:
        payload["decision_proof"] = {
            **payload["decision_proof"],
            field: {
                "epoch_number": 3,
                "tree_id": 6,
                "epoch_digest": "44" * 32,
            }[field],
        }
    streams[2] = (observed, replace(committed, payload=payload))

    with pytest.raises(
        FactorialValidationError,
        match="configuration/generation identity drift",
    ):
        _validate_v38_commit_identity_streams(streams)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("malformed", "invalid field set"),
        ("nonadjacent", "immediate same-source successor"),
        ("reason", "unavailable reason"),
        ("pending", "pending disposition"),
        ("rich-marker", "both rich and unavailable"),
    ),
)
def test_v38_observer_marker_remains_fail_closed(
    mutation: str,
    reason: str,
) -> None:
    streams = _v38_observer_zero_gap_streams()
    observer = list(streams[0])
    marker = observer[3]
    if mutation == "malformed":
        observer[3] = replace(marker, payload={**marker.payload, "extra": 1})
    elif mutation == "nonadjacent":
        intervening = _native_event(
            source_id="replica-0",
            sequence=4,
            monotonic_ns=marker.monotonic_ns - 1,
            event_type="test.intervening",
            payload={},
        )
        observer = (
            *observer[:3],
            intervening,
            replace(marker, source_sequence=5, line_number=5),
            *(
                replace(
                    event,
                    source_sequence=event.source_sequence + 1,
                    line_number=event.line_number + 1,
                )
                for event in observer[4:]
            ),
        )
    elif mutation == "reason":
        observer[3] = replace(
            marker,
            payload={**marker.payload, "reason": "legal_qc_skip"},
        )
    elif mutation == "pending":
        observer[3] = replace(
            marker,
            payload={**marker.payload, "convergence_identity_pending": True},
        )
    else:
        observer.append(
            _v38_rich_commit(
                0,
                height=_V38_GAP_HEIGHT,
                sequence=7,
                monotonic_ns=130,
                transactions=0,
            )
        )
    streams[0] = tuple(observer)

    with pytest.raises(FactorialValidationError, match=reason):
        _validate_v38_commit_identity_streams(streams)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("missing-predecessor", "predecessor"),
        ("missing-successor", "successor"),
        ("parent", "parent chain"),
        ("successor-parent", "parent chain"),
        ("configuration", "configuration/generation closure"),
        ("generation", "configuration/generation closure"),
    ),
)
def test_v38_observer_gap_requires_exact_rich_bridge(
    mutation: str,
    reason: str,
) -> None:
    streams = _v38_observer_zero_gap_streams()
    observer = list(streams[0])
    if mutation == "missing-predecessor":
        observer = observer[2:]
    elif mutation == "missing-successor":
        observer = observer[:-2]
    elif mutation == "parent":
        for replica_id, events in streams.items():
            streams[replica_id] = tuple(
                _replace_payload_field(event, "parent_hash", "99" * 32)
                if event.payload.get("block_height") == _V38_GAP_HEIGHT
                else event
                for event in events
            )
        observer = list(streams[0])
    elif mutation == "successor-parent":
        observer[-2:] = [
            _replace_payload_field(event, "parent_hash", "99" * 32)
            for event in observer[-2:]
        ]
    else:
        predecessor = observer[1]
        proof = dict(predecessor.payload["decision_proof"])
        payload = dict(predecessor.payload)
        if mutation == "configuration":
            proof["epoch_number"] = 1
            proof["epoch_digest"] = "22" * 32
            payload["view_generation"] = (1 << 32) + 6
        else:
            payload["view_generation"] = _V38_GAP_GENERATION + 1
        payload["decision_proof"] = proof
        observer[1] = replace(predecessor, payload=payload)
    streams[0] = tuple(observer)

    with pytest.raises(FactorialValidationError, match=reason):
        _validate_v38_commit_identity_streams(streams)


def test_v38_observer_gap_cannot_be_transition_or_cutoff_evidence() -> None:
    with pytest.raises(FactorialValidationError, match="transition or cutoff"):
        _validate_v38_commit_identity_streams(
            _v38_observer_zero_gap_streams(),
            protected_block_identities=frozenset(
                {(_V38_GAP_HEIGHT, _V38_GAP_HASH)}
            ),
        )


def test_v38_production_protection_collects_transition_and_cutoff_blocks() -> None:
    cutoff = _v38_rich_commit(
        0,
        height=_V38_GAP_HEIGHT,
        sequence=1,
        monotonic_ns=110,
        transactions=0,
    )
    transition = _native_event(
        source_id="replica-1",
        sequence=1,
        monotonic_ns=120,
        event_type="epoch.command_committed",
        payload={
            "command_block_height": 8,
            "command_block_hash": "88" * 32,
        },
    )
    command_cutoff = _native_event(
        source_id="replica-2",
        sequence=1,
        monotonic_ns=125,
        event_type="epoch.command_committed",
        payload={
            "command_block_height": 10,
            "command_block_hash": "aa" * 32,
        },
    )
    terminal = _native_event(
        source_id="adaptive-manager",
        sequence=1,
        monotonic_ns=130,
        event_type="adaptive_v2_session_terminal",
        payload={
            "winning_activation": {
                "command_block_height": 9,
                "command_block_hash": "99" * 32,
            },
        },
    )

    protected = validation._transition_and_cutoff_block_identities(
        cutoff_document={
            "cutoffs": [
                {
                    "source_path": cutoff.relative_path,
                    "source_sequence": cutoff.source_sequence,
                },
                {
                    "source_path": command_cutoff.relative_path,
                    "source_sequence": command_cutoff.source_sequence,
                },
            ],
        },
        events_by_ref={
            (cutoff.relative_path, cutoff.source_sequence): cutoff,
            (
                command_cutoff.relative_path,
                command_cutoff.source_sequence,
            ): command_cutoff,
        },
        manager_events=(terminal,),
        replica_events={0: (cutoff,), 1: (transition,)},
    )

    assert protected == frozenset(
        {
            (_V38_GAP_HEIGHT, _V38_GAP_HASH),
            (8, "88" * 32),
            (9, "99" * 32),
            (10, "aa" * 32),
        }
    )


def test_v38_observer_gap_does_not_synthesize_throughput_authority() -> None:
    streams = _v38_observer_zero_gap_streams()
    assert _validate_v38_commit_identity_streams(streams) == _v38_gap_key()

    authoritative = validation._authoritative_commits(streams)
    assert [commit["height"] for commit in authoritative] == [6, 8]
    assert sum(commit["transactions"] for commit in authoritative) == 2_000
    assert all(commit["hash"] != _V38_GAP_HASH for commit in authoritative)


@pytest.mark.parametrize("field", ("block_height", "commit_batch_index"))
def test_commit_payload_rejects_height_and_batch_u64_overflow(field: str) -> None:
    committed = _commit_event(2, 110, 7)
    observed = _commit_observed_event(
        committed,
        sequence=1,
        monotonic_ns=101,
        payload_updates={field: 1 << 64},
    )

    with pytest.raises(FactorialValidationError, match=f"{field}.*uint64"):
        validation._commit_payload(observed, authoritative=False)


def test_missing_zero_bucket_is_not_equivalent_to_explicit_zero() -> None:
    windows = {
        phase: (1 + index * 5_000_000_000, 1 + (index + 1) * 5_000_000_000, 1)
        for index, phase in enumerate(validation.PHASES)
    }
    event = _commit_event(1, windows["baseline"][0] + 1, 1)
    valid = _throughput_document(windows)
    metrics = validate_throughput_document(
        valid,
        slot_id="slot-test",
        replica_events={0: (event,)},
        phase_windows=windows,
        phase_configurations=_throughput_phase_configurations(),
        bucket_width_s=5,
        observer_id="replica-0",
        observer_instance="slot-replica-0",
    )
    assert next(metric for metric in metrics if metric.phase == "fault_evidence").buckets_tps == (0.0,)

    with pytest.raises(FactorialValidationError, match="omits an explicit bucket"):
        validate_throughput_document(
            _throughput_document(windows, omit_zero_bucket=True),
            slot_id="slot-test",
            replica_events={0: (event,)},
            phase_windows=windows,
            phase_configurations=_throughput_phase_configurations(),
            bucket_width_s=5,
            observer_id="replica-0",
            observer_instance="slot-replica-0",
        )


def test_epoch0_commits_cannot_populate_epoch1_throughput() -> None:
    windows = {
        phase: (1 + index * 5_000_000_000, 1 + (index + 1) * 5_000_000_000, 1)
        for index, phase in enumerate(validation.PHASES)
    }
    epoch0 = _commit_event(1, windows["epoch1_stable"][0] + 1, 1)
    document = _throughput_document(windows)
    epoch1 = next(
        phase for phase in document["phases"] if phase["phase"] == "epoch1_stable"
    )
    epoch1["buckets"][0]["transactions"] = 1000
    epoch1["buckets"][0]["tps"] = 200.0
    epoch1["transactions"] = 1000
    epoch1["mean_tps"] = 200.0
    baseline = next(
        phase for phase in document["phases"] if phase["phase"] == "baseline"
    )
    baseline["buckets"][0]["transactions"] = 0
    baseline["buckets"][0]["tps"] = 0.0
    baseline["transactions"] = 0
    baseline["mean_tps"] = 0.0

    with pytest.raises(FactorialValidationError, match="epoch1_stable"):
        validate_throughput_document(
            document,
            slot_id="slot-test",
            replica_events={0: (epoch0,)},
            phase_windows=windows,
            phase_configurations={
                "baseline": (0, "11" * 32, frozenset({0})),
                "fault_evidence": (0, "11" * 32, frozenset({0})),
                "epoch1_stable": (1, "22" * 32, frozenset({0, 1})),
                "epoch2_stable": (2, "33" * 32, frozenset({0, 1})),
            },
            bucket_width_s=5,
            observer_id="replica-0",
            observer_instance="slot-replica-0",
        )


def test_huge_numeric_tps_is_rejected_without_overflow() -> None:
    windows = {
        phase: (1 + index * 5_000_000_000, 1 + (index + 1) * 5_000_000_000, 1)
        for index, phase in enumerate(validation.PHASES)
    }
    event = _commit_event(1, windows["baseline"][0] + 1, 1)
    document = _throughput_document(windows)
    document["phases"][0]["buckets"][0]["tps"] = 10**10_000

    with pytest.raises(FactorialValidationError, match="numeric bound"):
        validate_throughput_document(
            document,
            slot_id="slot-test",
            replica_events={0: (event,)},
            phase_windows=windows,
            phase_configurations=_throughput_phase_configurations(),
            bucket_width_s=5,
            observer_id="replica-0",
            observer_instance="slot-replica-0",
        )


@pytest.mark.parametrize(
    "line",
    (
        "KAURI_FAULT marker_skipped marker=aggregate_omitted reason=capacity\n",
        (
            "KAURI_FAULT fault=rotating_intermittent_omission_v1 "
            "proposal_epoch=0 proposal_tree=0 proposal_epoch_digest={digest} "
            "proposal_block_hash={digest} window=w "
            "window_start_monotonic_ns=1 window_end_monotonic_ns=10 "
            "actor=1 action=capacity_exhausted monotonic_ns=2\n"
        ).format(digest="11" * 32),
    ),
)
def test_capacity_or_skipped_fault_marker_fails_closed(tmp_path: Path, line: str) -> None:
    log = tmp_path / "replica.log"
    log.write_text(line, encoding="utf-8")

    with pytest.raises(FactorialValidationError, match="skipped|capacity"):
        validation._fault_markers(tmp_path, {0: ("replica.log",)})


def _response_attempt_arm_line(
    *,
    reporter: int = 0,
    child: int = 2,
    start_ns: int = 1_900_000,
    duration_us: int = 500,
    absolute_deadline_ns: int | None = None,
) -> str:
    deadline_ns = (
        start_ns + duration_us * 1_000
        if absolute_deadline_ns is None
        else absolute_deadline_ns
    )
    return (
        "2026-08-05 12:00:00 [hotstuff info] "
        "KAURI_EVIDENCE response_attempt_armed "
        f"reporter={reporter} child={child} epoch=1 tree=4 "
        f"epoch_digest={'11' * 32} block={900:064x} "
        "expected_message_type=aggregate_relay "
        f"start_monotonic_ns={start_ns} "
        f"deadline_duration_us={duration_us} "
        f"absolute_deadline_ns={deadline_ns}\n"
    )


def test_v9_response_attempt_arm_parser_accepts_exact_marker(tmp_path: Path) -> None:
    log = tmp_path / "replica.log"
    log.write_text(_response_attempt_arm_line(), encoding="utf-8")

    marker = validation._response_attempt_arm_markers(
        tmp_path,
        {0: ("replica.log",)},
    )[0]

    assert marker.source_replica == marker.reporter_id == 0
    assert marker.child_id == 2
    assert marker.expected_message_type == "aggregate_relay"
    assert marker.absolute_deadline_ns == (
        marker.start_monotonic_ns + marker.deadline_duration_us * 1_000
    )


@pytest.mark.parametrize(
    "line",
    (
        _response_attempt_arm_line().replace(" child=2", ""),
        (
            "KAURI_EVIDENCE response_attempt_arm_marker_failed "
            "reason=deadline_counter reporter=0 epoch=1 tree=4\n"
        ),
    ),
)
def test_v9_response_attempt_arm_parser_rejects_malformed_or_failure_marker(
    tmp_path: Path,
    line: str,
) -> None:
    log = tmp_path / "replica.log"
    log.write_text(line, encoding="utf-8")

    with pytest.raises(FactorialValidationError, match="malformed|failed"):
        validation._response_attempt_arm_markers(
            tmp_path,
            {0: ("replica.log",)},
        )


def test_v9_response_attempt_arm_parser_rejects_source_reporter_mismatch(
    tmp_path: Path,
) -> None:
    log = tmp_path / "replica.log"
    log.write_text(_response_attempt_arm_line(reporter=1), encoding="utf-8")

    with pytest.raises(FactorialValidationError, match="source.*reporter"):
        validation._response_attempt_arm_markers(
            tmp_path,
            {0: ("replica.log",)},
        )


def test_full_causal_gate_rejects_disjoint_persistent_interior_proposals() -> None:
    replica_count = 13
    actors = (9, 10, 12)
    epoch_digest = "22" * 32
    epoch1_digest = "44" * 32
    epoch2_digest = "66" * 32
    trees = tuple(
        Tree(
            tree_id=tree_id,
            fanout=5,
            pipeline_stretch=2,
            members=tuple((member + tree_id) % replica_count for member in range(replica_count)),
            wait_exempt=(),
        )
        for tree_id in range(replica_count)
    )
    epoch1_trees = tuple(
        Tree(
            tree_id=tree_id,
            fanout=5,
            pipeline_stretch=2,
            members=tuple(
                member
                for member in range(replica_count)
                if member != actors[tree_id % len(actors)]
            )
            + (actors[tree_id % len(actors)],),
            wait_exempt=actors,
        )
        for tree_id in range(replica_count)
    )
    epoch2_trees = epoch1_trees
    markers: list[FaultMarker] = []
    evidence: list[validation._EvidenceRecord] = []
    events_by_actor: dict[int, list[validation._NativeEvent]] = {
        actor: [] for actor in actors
    }
    evidence_sequence = 2

    def selected_hash(
        *, epoch: int, tree_id: int, digest: str, actor: int, base: int
    ) -> str:
        counter = base
        while True:
            block_hash = f"{counter:064x}"
            _, selected = fnv1a_rotating_actor(
                actors,
                epoch_number=epoch,
                tree_id=tree_id,
                epoch_digest=digest,
                block_hash=block_hash,
            )
            if selected == actor:
                return block_hash
            counter += 1

    def adaptive_payload(
        *, epoch: int, tree_id: int, digest: str, block_hash: str, actor: int
    ) -> dict[str, object]:
        return {
            "epoch_number": epoch,
            "tree_id": tree_id,
            "epoch_digest": digest,
            "block_hash": block_hash,
            "context_generation": 1,
            "observer_replica": actor,
            "wait_exempt_signers": [],
            "accepted_signers": [],
            "absent_direct_children": [],
            "missing_optional_signers": [],
            "required_branch_gaps": [],
            "root_signer_count": 0,
            "global_quorum": 0,
            "rejection_reason": None,
        }

    for offset, actor in enumerate(actors):
        for guard_offset, tree_id in enumerate((actor - 1, actor - 2)):
            block_hash = selected_hash(
                epoch=0,
                tree_id=tree_id,
                digest=epoch_digest,
                actor=actor,
                base=1 + offset * 100 + guard_offset * 10,
            )
            marker_ns = 200 + offset * 10 + guard_offset
            markers.append(
                FaultMarker(
                    source_replica=actor,
                    line_number=len(markers) + 1,
                    fault_mode="rotating_intermittent_omission_v1",
                    epoch_number=0,
                    tree_id=tree_id,
                    epoch_digest=epoch_digest,
                    block_hash=block_hash,
                    window="w",
                    window_start_ns=100,
                    window_end_ns=1200,
                    actor=actor,
                    action="omit_aggregate",
                    monotonic_ns=marker_ns,
                    raw_line_sha256="33" * 32,
                )
            )
            position = trees[tree_id].members.index(actor)
            parent = trees[tree_id].members[(position - 1) // trees[tree_id].fanout]
            evidence.append(
                validation._EvidenceRecord(
                    ingestion_sequence=evidence_sequence,
                    acceptance_monotonic_ns=300 + offset * 10 + guard_offset,
                    observation_id=f"{evidence_sequence:064x}",
                    reporter_id=parent,
                    target_id=actor,
                    epoch_number=0,
                    tree_id=tree_id,
                    epoch_digest=epoch_digest,
                    block_hash=block_hash,
                    message_type="aggregate_relay",
                    outcome="timeout",
                    response_duration_us=0,
                    deadline_duration_us=10,
                    reporter_monotonic_ns=250 + offset * 10 + guard_offset,
                    reporter_sequence=guard_offset + 1,
                    signer_set=(),
                )
            )
            evidence_sequence += 1
            events_by_actor[actor].append(
                _native_event(
                    source_id=f"replica-{actor}",
                    sequence=len(events_by_actor[actor]) + 1,
                    monotonic_ns=marker_ns - 10,
                    event_type="aggregation.required_set_ready",
                    payload=adaptive_payload(
                        epoch=0,
                        tree_id=tree_id,
                        digest=epoch_digest,
                        block_hash=block_hash,
                        actor=actor,
                    ),
                )
            )
        epoch1_tree_id = offset
        epoch1_block_hash = selected_hash(
            epoch=1,
            tree_id=epoch1_tree_id,
            digest=epoch1_digest,
            actor=actor,
            base=1000 + offset * 100,
        )
        markers.append(
            FaultMarker(
                source_replica=actor,
                line_number=len(markers) + 1,
                fault_mode="rotating_intermittent_omission_v1",
                epoch_number=1,
                tree_id=epoch1_tree_id,
                epoch_digest=epoch1_digest,
                block_hash=epoch1_block_hash,
                window="w",
                window_start_ns=100,
                window_end_ns=1200,
                actor=actor,
                action="omit_direct_vote",
                monotonic_ns=700 + offset,
                raw_line_sha256="55" * 32,
            )
        )
        events_by_actor[actor].append(
            _native_event(
                source_id=f"replica-{actor}",
                sequence=len(events_by_actor[actor]) + 1,
                monotonic_ns=690 + offset,
                event_type="aggregation.required_set_ready",
                payload=adaptive_payload(
                    epoch=1,
                    tree_id=epoch1_tree_id,
                    digest=epoch1_digest,
                    block_hash=epoch1_block_hash,
                    actor=actor,
                ),
            )
        )
        epoch2_tree_id = offset
        epoch2_block_hash = selected_hash(
            epoch=2,
            tree_id=epoch2_tree_id,
            digest=epoch2_digest,
            actor=actor,
            base=2000 + offset * 100,
        )
        markers.append(
            FaultMarker(
                source_replica=actor,
                line_number=len(markers) + 1,
                fault_mode="rotating_intermittent_omission_v1",
                epoch_number=2,
                tree_id=epoch2_tree_id,
                epoch_digest=epoch2_digest,
                block_hash=epoch2_block_hash,
                window="w",
                window_start_ns=100,
                window_end_ns=1200,
                actor=actor,
                action="omit_direct_vote",
                monotonic_ns=1000 + offset,
                raw_line_sha256="77" * 32,
            )
        )
        events_by_actor[actor].append(
            _native_event(
                source_id=f"replica-{actor}",
                sequence=len(events_by_actor[actor]) + 1,
                monotonic_ns=990 + offset,
                event_type="aggregation.required_set_ready",
                payload=adaptive_payload(
                    epoch=2,
                    tree_id=epoch2_tree_id,
                    digest=epoch2_digest,
                    block_hash=epoch2_block_hash,
                    actor=actor,
                ),
            )
        )
    events = {actor: tuple(rows) for actor, rows in events_by_actor.items()}
    phase_windows = {
        "baseline": (10, 90, 1),
        "fault_evidence": (100, 400, 1),
        "epoch1_stable": (650, 800, 1),
        "epoch2_stable": (950, 1100, 1),
    }
    arguments = dict(
        replica_events=events,
        actor_ids=actors,
        fault_mode="rotating_intermittent_omission_v1",
        max_omissions_per_proposal=1,
        initial_epoch_digest=epoch_digest,
        initial_trees=trees,
        window_id="w",
        window_start_ns=100,
        window_end_ns=1200,
        epoch1_command_ns=500,
        epoch1_activation_ns=600,
        epoch2_command_ns=900,
        epoch1_digest=epoch1_digest,
        epoch1_trees=epoch1_trees,
        epoch2_digest=epoch2_digest,
        epoch2_trees=epoch2_trees,
        phase_windows=phase_windows,
        required_reporters=2,
        accepted_epoch0=evidence,
        baseline_cutoff=1,
        current_cutoff=10,
    )
    validate_fault_causality(markers=markers, **arguments)

    actor = actors[0]
    actor_events = events[actor]
    first_proposal = actor_events[0]
    cross_observer = 0
    cross_observer_event = _native_event(
        source_id=f"replica-{cross_observer}",
        sequence=1,
        monotonic_ns=first_proposal.monotonic_ns,
        event_type=first_proposal.event_type,
        payload={**first_proposal.payload, "observer_replica": cross_observer},
    )
    cross_observer_events = {
        **events,
        actor: actor_events[1:],
        cross_observer: (cross_observer_event,),
    }
    validate_fault_causality(
        markers=markers,
        **{**arguments, "replica_events": cross_observer_events},
    )

    authoritative_commit = _native_event(
        source_id="replica-0",
        sequence=1,
        monotonic_ns=first_proposal.monotonic_ns + 20,
        event_type="block.committed",
        payload={
            "block_height": 1,
            "block_hash": first_proposal.payload["block_hash"],
            "parent_hash": None,
            "transaction_count": 1000,
            "designated_observer": True,
            "decision_proof": {
                "epoch_number": first_proposal.payload["epoch_number"],
                "tree_id": first_proposal.payload["tree_id"],
                "epoch_digest": first_proposal.payload["epoch_digest"],
                "block_hash": first_proposal.payload["block_hash"],
            },
            "view_generation": 1,
            "commit_batch_index": 0,
        },
    )
    commit_proved_events = {
        **events,
        actor: actor_events[1:],
        0: (authoritative_commit,),
    }
    validate_fault_causality(
        markers=markers,
        **{**arguments, "replica_events": commit_proved_events},
    )

    noncausal_commit = replace(
        authoritative_commit,
        monotonic_ns=markers[0].monotonic_ns,
    )
    with pytest.raises(FactorialValidationError, match="does not precede"):
        validate_fault_causality(
            markers=markers,
            **{
                **arguments,
                "replica_events": {
                    **commit_proved_events,
                    0: (noncausal_commit,),
                },
            },
        )

    non_authoritative_commit = replace(
        authoritative_commit,
        payload={**authoritative_commit.payload, "designated_observer": False},
    )
    with pytest.raises(FactorialValidationError, match="designated-observer"):
        validate_fault_causality(
            markers=markers,
            **{
                **arguments,
                "replica_events": {
                    **commit_proved_events,
                    0: (non_authoritative_commit,),
                },
            },
        )

    with pytest.raises(FactorialValidationError, match="invalid designated-observer"):
        validate_fault_causality(
            markers=markers,
            **{
                **arguments,
                "replica_events": {
                    **events,
                    actor: actor_events[1:],
                    1: (authoritative_commit,),
                },
            },
        )

    mismatched_observer = replace(
        first_proposal,
        payload={**first_proposal.payload, "observer_replica": cross_observer},
    )
    with pytest.raises(FactorialValidationError, match="observer differs"):
        validate_fault_causality(
            markers=markers,
            **{
                **arguments,
                "replica_events": {
                    **events,
                    actor: (mismatched_observer, *actor_events[1:]),
                },
            },
        )

    mismatched_proposal = replace(
        first_proposal,
        payload={**first_proposal.payload, "block_hash": "99" * 32},
    )
    with pytest.raises(FactorialValidationError, match="no matching native proposal"):
        validate_fault_causality(
            markers=markers,
            **{
                **arguments,
                "replica_events": {
                    **events,
                    actor: (mismatched_proposal, *actor_events[1:]),
                },
            },
        )

    actor_internal = [
        marker
        for marker in markers
        if marker.actor == actors[-1] and marker.action == "omit_aggregate"
    ]
    missing_guard = [marker for marker in markers if marker != actor_internal[-1]]
    with pytest.raises(FactorialValidationError, match=r"full causally bound f\+1"):
        validate_fault_causality(markers=missing_guard, **arguments)

    missing_epoch1 = [
        marker
        for marker in markers
        if not (marker.actor == actors[-1] and marker.epoch_number == 1)
    ]
    with pytest.raises(FactorialValidationError, match="Epoch1-stable"):
        validate_fault_causality(markers=missing_epoch1, **arguments)

    missing_epoch2 = [
        marker
        for marker in markers
        if not (marker.actor == actors[-1] and marker.epoch_number == 2)
    ]
    with pytest.raises(FactorialValidationError, match="Epoch2-stable"):
        validate_fault_causality(markers=missing_epoch2, **arguments)

    mismatched_evidence = [
        replace(evidence[0], block_hash="99" * 32),
        *evidence[1:],
    ]
    with pytest.raises(FactorialValidationError, match="no exact outstanding timeout"):
        validate_fault_causality(
            markers=markers,
            **{**arguments, "accepted_epoch0": mismatched_evidence},
        )

    wrong_message_type = [
        replace(evidence[0], message_type="direct_vote"),
        *evidence[1:],
    ]
    with pytest.raises(FactorialValidationError, match="no exact outstanding timeout"):
        validate_fault_causality(
            markers=markers,
            **{**arguments, "accepted_epoch0": wrong_message_type},
        )

    wrong_reporter = [
        replace(evidence[0], reporter_id=(evidence[0].reporter_id + 1) % replica_count),
        *evidence[1:],
    ]
    with pytest.raises(FactorialValidationError, match="no exact outstanding timeout"):
        validate_fault_causality(
            markers=markers,
            **{**arguments, "accepted_epoch0": wrong_reporter},
        )

    persistent_markers = tuple(
        replace(marker, fault_mode="persistent_selected_omission_v1")
        for marker in markers
    )
    persistent_windows = {
        phase: (start, end, 6)
        for phase, (start, end, _bucket_count) in phase_windows.items()
    }
    with pytest.raises(
        FactorialValidationError,
        match="persistent interior proposal actor/action set",
    ):
        validate_fault_causality(
            markers=persistent_markers,
            **{
                **arguments,
                "fault_mode": "persistent_selected_omission_v1",
                "max_omissions_per_proposal": 3,
                "phase_windows": persistent_windows,
            },
        )


def test_persistent_fault_schedule_bounds_selected_actors_per_proposal() -> None:
    actors = (9, 10, 12)
    markers = tuple(
        FaultMarker(
            source_replica=actor,
            line_number=index,
            fault_mode="persistent_selected_omission_v1",
            epoch_number=0,
            tree_id=4,
            epoch_digest="11" * 32,
            block_hash="22" * 32,
            window="persistent-window",
            window_start_ns=100,
            window_end_ns=1_000,
            actor=actor,
            action="omit_aggregate",
            monotonic_ns=200 + index,
            raw_line_sha256=f"{index:064x}",
        )
        for index, actor in enumerate(actors, start=1)
    )

    validation._validate_fault_marker_schedule(
        markers,
        actor_ids=actors,
        fault_mode="persistent_selected_omission_v1",
        max_omissions_per_proposal=3,
    )

    validation._validate_fault_marker_schedule(
        markers[:-1],
        actor_ids=actors,
        fault_mode="persistent_selected_omission_v1",
        max_omissions_per_proposal=3,
    )
    with pytest.raises(FactorialValidationError, match="mode/actor"):
        validation._validate_fault_marker_schedule(
            (replace(markers[0], actor=8, source_replica=8),),
            actor_ids=actors,
            fault_mode="persistent_selected_omission_v1",
            max_omissions_per_proposal=3,
        )
    with pytest.raises(FactorialValidationError, match="no-op forward"):
        validation._validate_fault_marker_schedule(
            (replace(markers[0], action="forward"),),
            actor_ids=actors,
            fault_mode="persistent_selected_omission_v1",
            max_omissions_per_proposal=3,
        )
    with pytest.raises(FactorialValidationError, match="omission bound"):
        validation._validate_fault_marker_schedule(
            (*markers, replace(markers[0], line_number=99)),
            actor_ids=actors,
            fault_mode="persistent_selected_omission_v1",
            max_omissions_per_proposal=3,
        )


def _tiered_marker(
    *,
    actor: int,
    cohort: str,
    ordinal: int,
    block_ordinal: int,
    action: str,
    fault_mode: str = "tiered_persistent_responsive_omission_v1",
    contribution_role: str | None = None,
    role_contribution_ordinal: int | None = None,
) -> FaultMarker:
    return FaultMarker(
        source_replica=actor,
        line_number=block_ordinal,
        fault_mode=fault_mode,
        epoch_number=1,
        tree_id=4,
        epoch_digest="11" * 32,
        block_hash=f"{block_ordinal:064x}",
        window="tiered-window",
        window_start_ns=100,
        window_end_ns=10_000,
        actor=actor,
        action=action,
        monotonic_ns=200 + block_ordinal,
        raw_line_sha256=f"{actor * 100 + block_ordinal:064x}",
        cohort=cohort,
        hard_actor_count=3,
        responsive_degraded_actor_count=1,
        fault_threshold=4,
        max_omissions_per_proposal=4,
        responsive_omission_period=41,
        contribution_ordinal=ordinal,
        contribution_role=contribution_role,
        role_contribution_ordinal=role_contribution_ordinal,
    )


def _v14_opportunity_event(
    marker: FaultMarker,
    tree: Tree,
    *,
    sequence: int = 1,
    payload_overrides: dict[str, object] | None = None,
    source_id: str | None = None,
) -> validation._NativeEvent:
    position = tree.members.index(marker.actor)
    assert position > 0
    leaf_start = validation._first_leaf_index(len(tree.members), tree.fanout)
    physical_role = "internal" if position < leaf_start else "leaf"
    payload: dict[str, object] = {
        "actor": marker.actor,
        "proposal": {
            "epoch_number": marker.epoch_number,
            "tree_id": marker.tree_id,
            "epoch_digest": marker.epoch_digest,
            "block_hash": marker.block_hash,
        },
        "view_generation": 7,
        "physical_role": physical_role,
        "parent_replica": tree.members[(position - 1) // tree.fanout],
        "expected_message_type": (
            "aggregate_relay" if physical_role == "internal" else "direct_vote"
        ),
        "cohort": marker.cohort,
        "diagnostic_window": marker.window,
        "window_start_monotonic_ns": marker.window_start_ns,
        "window_end_monotonic_ns": marker.window_end_ns,
        "decision_monotonic_ns": marker.monotonic_ns,
        "contribution_ordinal": marker.contribution_ordinal,
        "role_contribution_ordinal": marker.role_contribution_ordinal,
        "scheduled_action": marker.action,
        "responsive_omission_period": marker.responsive_omission_period,
        "fault_threshold": marker.fault_threshold,
        "hard_actor_count": marker.hard_actor_count,
        "responsive_degraded_actor_count": (
            marker.responsive_degraded_actor_count
        ),
        "fault_mode": marker.fault_mode,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return _native_event(
        source_id=source_id or f"replica-{marker.actor}",
        sequence=sequence,
        monotonic_ns=marker.monotonic_ns + 1,
        event_type="fault.contribution_opportunity",
        payload=payload,
    )


def _v14_marker(
    *,
    actor: int,
    tree: Tree,
    epoch_number: int,
    epoch_digest: str,
    block_ordinal: int,
    monotonic_ns: int,
    cohort: str,
    contribution_ordinal: int,
    role_contribution_ordinal: int,
) -> FaultMarker:
    position = tree.members.index(actor)
    leaf_start = validation._first_leaf_index(len(tree.members), tree.fanout)
    role = "internal" if position < leaf_start else "leaf"
    should_omit = (
        cohort == "hard"
        or role_contribution_ordinal % validation._RESPONSIVE_OMISSION_PERIOD == 0
    )
    action = "forward"
    if should_omit:
        action = "omit_aggregate" if role == "internal" else "omit_direct_vote"
    return FaultMarker(
        source_replica=actor,
        line_number=block_ordinal,
        fault_mode="tiered_persistent_responsive_omission_v2",
        epoch_number=epoch_number,
        tree_id=tree.tree_id,
        epoch_digest=epoch_digest,
        block_hash=f"{block_ordinal:064x}",
        window="v14-window",
        window_start_ns=100,
        window_end_ns=2_000,
        actor=actor,
        action=action,
        monotonic_ns=monotonic_ns,
        raw_line_sha256=f"{actor * 1000 + block_ordinal:064x}",
        cohort=cohort,
        hard_actor_count=1,
        responsive_degraded_actor_count=1,
        fault_threshold=2,
        max_omissions_per_proposal=2,
        responsive_omission_period=validation._RESPONSIVE_OMISSION_PERIOD,
        contribution_ordinal=(0 if cohort == "hard" else contribution_ordinal),
        contribution_role=role,
        role_contribution_ordinal=(
            0 if cohort == "hard" else role_contribution_ordinal
        ),
    )


def test_v14_opportunity_parser_and_bijection_accept_internal_and_leaf() -> None:
    tree = Tree(
        tree_id=4,
        fanout=2,
        pipeline_stretch=2,
        members=(0, 2, 1, 9, 3, 4, 5),
        wait_exempt=(9,),
    )
    digest = "11" * 32
    internal = _v14_marker(
        actor=2,
        tree=tree,
        epoch_number=0,
        epoch_digest=digest,
        block_ordinal=1,
        monotonic_ns=300,
        cohort="responsive_degraded",
        contribution_ordinal=1,
        role_contribution_ordinal=1,
    )
    leaf = _v14_marker(
        actor=9,
        tree=tree,
        epoch_number=0,
        epoch_digest=digest,
        block_ordinal=2,
        monotonic_ns=301,
        cohort="hard",
        contribution_ordinal=0,
        role_contribution_ordinal=0,
    )
    replica_events = {
        2: (_v14_opportunity_event(internal, tree),),
        9: (_v14_opportunity_event(leaf, tree),),
    }
    opportunities = validation._fault_contribution_opportunities(replica_events)

    validation._validate_fault_contribution_opportunity_bijection(
        markers=(internal, leaf),
        opportunities=opportunities,
        fault_actor_ids=(2, 9),
        phase_windows={"fault_evidence": (100, 700, 6)},
        phase_configurations=(
            ("fault_evidence", 0, digest, {tree.tree_id: tree}),
        ),
    )

    by_actor = {opportunity.actor: opportunity for opportunity in opportunities}
    assert by_actor[2].physical_role == "internal"
    assert by_actor[2].expected_message_type == "aggregate_relay"
    assert by_actor[9].physical_role == "leaf"
    assert by_actor[9].expected_message_type == "direct_vote"


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("extra_field", "payload schema"),
        ("source", "source actor"),
        ("generation", "view generation"),
    ),
)
def test_v14_opportunity_parser_rejects_schema_source_and_generation(
    mutation: str,
    message: str,
) -> None:
    tree = Tree(0, 2, 2, (0, 2, 1), ())
    marker = _v14_marker(
        actor=2,
        tree=tree,
        epoch_number=0,
        epoch_digest="11" * 32,
        block_ordinal=1,
        monotonic_ns=300,
        cohort="responsive_degraded",
        contribution_ordinal=1,
        role_contribution_ordinal=1,
    )
    payload_overrides = None
    source_id = None
    if mutation == "extra_field":
        payload_overrides = {"unexpected": 1}
    elif mutation == "source":
        source_id = "replica-1"
    else:
        payload_overrides = {"view_generation": 0}
    event = _v14_opportunity_event(
        marker,
        tree,
        payload_overrides=payload_overrides,
        source_id=source_id,
    )

    with pytest.raises(FactorialValidationError, match=message):
        validation._fault_contribution_opportunities({2: (event,)})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("physical_role", "leaf", "physical role"),
        ("parent_replica", 1, "physical parent"),
        ("expected_message_type", "direct_vote", "message type"),
    ),
)
def test_v14_opportunity_bijection_rejects_topology_drift(
    field: str,
    value: object,
    message: str,
) -> None:
    tree = Tree(0, 2, 2, (0, 2, 1, 3, 4, 5, 6), ())
    marker = _v14_marker(
        actor=2,
        tree=tree,
        epoch_number=0,
        epoch_digest="11" * 32,
        block_ordinal=1,
        monotonic_ns=300,
        cohort="responsive_degraded",
        contribution_ordinal=1,
        role_contribution_ordinal=1,
    )
    opportunities = validation._fault_contribution_opportunities(
        {2: (_v14_opportunity_event(marker, tree),)}
    )
    opportunity = replace(opportunities[0], **{field: value})

    with pytest.raises(FactorialValidationError, match=message):
        validation._validate_fault_contribution_opportunity_bijection(
            markers=(marker,),
            opportunities=(opportunity,),
            fault_actor_ids=(2,),
            phase_windows={"fault_evidence": (100, 700, 6)},
            phase_configurations=(
                ("fault_evidence", 0, marker.epoch_digest, {0: tree}),
            ),
        )


def test_v14_opportunity_bijection_rejects_missing_duplicate_and_shared_drift() -> None:
    tree = Tree(0, 2, 2, (0, 2, 1), ())
    marker = _v14_marker(
        actor=2,
        tree=tree,
        epoch_number=0,
        epoch_digest="11" * 32,
        block_ordinal=1,
        monotonic_ns=300,
        cohort="responsive_degraded",
        contribution_ordinal=1,
        role_contribution_ordinal=1,
    )
    event = _v14_opportunity_event(marker, tree)
    opportunity = validation._fault_contribution_opportunities({2: (event,)})[0]
    arguments = {
        "fault_actor_ids": (2,),
        "phase_windows": {"fault_evidence": (100, 700, 6)},
        "phase_configurations": (
            ("fault_evidence", 0, marker.epoch_digest, {0: tree}),
        ),
    }

    with pytest.raises(FactorialValidationError, match="marker-only"):
        validation._validate_fault_contribution_opportunity_bijection(
            markers=(marker,), opportunities=(), **arguments
        )
    with pytest.raises(FactorialValidationError, match="event-only"):
        validation._validate_fault_contribution_opportunity_bijection(
            markers=(), opportunities=(opportunity,), **arguments
        )
    with pytest.raises(FactorialValidationError, match="duplicate"):
        validation._fault_contribution_opportunities({2: (event, event)})
    with pytest.raises(FactorialValidationError, match="shared fields"):
        validation._validate_fault_contribution_opportunity_bijection(
            markers=(marker,),
            opportunities=(replace(opportunity, scheduled_action="omit_aggregate"),),
            **arguments,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("fault_mode", "tiered_persistent_responsive_omission_v1"),
        ("window", "other-window"),
        ("window_start_ns", 101),
        ("window_end_ns", 1_999),
        ("decision_monotonic_ns", 301),
        ("cohort", "hard"),
        ("scheduled_action", "omit_direct_vote"),
        ("contribution_ordinal", 2),
        ("role_contribution_ordinal", 2),
        ("responsive_omission_period", 40),
        ("fault_threshold", 3),
        ("hard_actor_count", 2),
        ("responsive_degraded_actor_count", 2),
    ),
)
def test_v14_opportunity_pair_requires_every_shared_audit_field(
    field: str,
    value: object,
) -> None:
    tree = Tree(0, 2, 2, (0, 2, 1), ())
    marker = _v14_marker(
        actor=2,
        tree=tree,
        epoch_number=0,
        epoch_digest="11" * 32,
        block_ordinal=1,
        monotonic_ns=300,
        cohort="responsive_degraded",
        contribution_ordinal=1,
        role_contribution_ordinal=1,
    )
    opportunity = validation._fault_contribution_opportunities(
        {2: (_v14_opportunity_event(marker, tree),)}
    )[0]

    with pytest.raises(FactorialValidationError, match="shared fields"):
        validation._validate_fault_contribution_opportunity_bijection(
            markers=(marker,),
            opportunities=(replace(opportunity, **{field: value}),),
            fault_actor_ids=(2,),
            phase_windows={"fault_evidence": (100, 700, 6)},
            phase_configurations=(
                ("fault_evidence", 0, marker.epoch_digest, {0: tree}),
            ),
        )


def test_v14_opportunity_nonvacuity_is_per_phase_and_actor_not_per_root_qc() -> None:
    actors = (2, 9)
    tree = Tree(0, 2, 2, (0, 2, 1, 9, 3, 4, 5), (9,))
    phase_rows = (
        ("fault_evidence", 0, "11" * 32, 300),
        ("epoch1_stable", 1, "22" * 32, 900),
        ("epoch2_stable", 2, "33" * 32, 1_500),
    )
    phase_windows = {
        "fault_evidence": (100, 700, 6),
        "epoch1_stable": (700, 1_300, 6),
        "epoch2_stable": (1_300, 1_900, 6),
    }
    markers: list[FaultMarker] = []
    events: dict[int, list[validation._NativeEvent]] = {actor: [] for actor in actors}
    for phase_index, (_phase, epoch, digest, timestamp) in enumerate(phase_rows):
        for actor in actors:
            marker = _v14_marker(
                actor=actor,
                tree=tree,
                epoch_number=epoch,
                epoch_digest=digest,
                block_ordinal=phase_index * 10 + actor,
                monotonic_ns=timestamp + actor,
                cohort="responsive_degraded" if actor == 2 else "hard",
                contribution_ordinal=phase_index + 1,
                role_contribution_ordinal=phase_index + 1,
            )
            markers.append(marker)
            events[actor].append(
                _v14_opportunity_event(
                    marker,
                    tree,
                    sequence=len(events[actor]) + 1,
                )
            )
    # A committed root-only proposal has no contribution hook and is therefore
    # not required to manufacture either side of the opportunity/marker pair.
    events[0] = [
        _commit_event(
            1,
            350,
            99,
            epoch_number=0,
            epoch_digest=phase_rows[0][2],
            tree_id=tree.tree_id,
        )
    ]
    opportunities = validation._fault_contribution_opportunities(
        {replica: tuple(rows) for replica, rows in events.items()}
    )
    arguments = {
        "fault_actor_ids": actors,
        "phase_windows": phase_windows,
        "phase_configurations": tuple(
            (phase, epoch, digest, {tree.tree_id: tree})
            for phase, epoch, digest, _ in phase_rows
        ),
    }

    validation._validate_fault_contribution_opportunity_bijection(
        markers=tuple(markers), opportunities=opportunities, **arguments
    )

    omitted = next(
        marker
        for marker in markers
        if marker.actor == 9 and marker.epoch_number == 1
    )
    with pytest.raises(FactorialValidationError, match="epoch1_stable.*actor.*9"):
        validation._validate_fault_contribution_opportunity_bijection(
            markers=tuple(marker for marker in markers if marker != omitted),
            opportunities=tuple(
                opportunity
                for opportunity in opportunities
                if opportunity.identity != (omitted.actor, *(
                    omitted.epoch_number,
                    omitted.tree_id,
                    omitted.epoch_digest,
                    omitted.block_hash,
                ))
            ),
            **arguments,
        )


def _valid_tiered_markers() -> tuple[FaultMarker, ...]:
    markers = [
        _tiered_marker(
            actor=2,
            cohort="responsive_degraded",
            ordinal=ordinal,
            block_ordinal=ordinal,
            action="omit_aggregate" if ordinal % 41 == 0 else "forward",
        )
        for ordinal in range(1, 42)
    ]
    markers.extend(
        _tiered_marker(
            actor=actor,
            cohort="hard",
            ordinal=0,
            block_ordinal=41,
            action="omit_aggregate",
        )
        for actor in (9, 10, 12)
    )
    return tuple(markers)


def test_tiered_marker_parser_requires_exact_appended_audit_fields(
    tmp_path: Path,
) -> None:
    line = (
        "KAURI_FAULT fault=tiered_persistent_responsive_omission_v1 "
        "proposal_epoch=1 proposal_tree=4 proposal_epoch_digest={digest} "
        "proposal_block_hash={block} window=tiered-window "
        "window_start_monotonic_ns=100 window_end_monotonic_ns=10000 "
        "actor=2 action=forward monotonic_ns=201 "
        "cohort=responsive_degraded hard_actor_count=3 "
        "responsive_degraded_actor_count=1 fault_threshold=4 "
        "max_omissions_per_proposal=4 responsive_omission_period=41 "
        "contribution_ordinal=1\n"
    ).format(digest="11" * 32, block=f"{1:064x}")
    log = tmp_path / "replica.log"
    log.write_text(line, encoding="utf-8")

    marker = validation._fault_markers(tmp_path, {2: ("replica.log",)})[0]
    assert marker.cohort == "responsive_degraded"
    assert marker.responsive_omission_period == 41
    assert marker.contribution_ordinal == 1

    log.write_text(
        line.replace(" contribution_ordinal=1", ""),
        encoding="utf-8",
    )
    with pytest.raises(FactorialValidationError, match="malformed"):
        validation._fault_markers(tmp_path, {2: ("replica.log",)})


def test_role_scoped_tiered_marker_parser_requires_exact_role_fields(
    tmp_path: Path,
) -> None:
    line = (
        "KAURI_FAULT fault=tiered_persistent_responsive_omission_v2 "
        "proposal_epoch=1 proposal_tree=4 proposal_epoch_digest={digest} "
        "proposal_block_hash={block} window=tiered-window "
        "window_start_monotonic_ns=100 window_end_monotonic_ns=10000 "
        "actor=2 action=forward monotonic_ns=201 "
        "cohort=responsive_degraded hard_actor_count=3 "
        "responsive_degraded_actor_count=1 fault_threshold=4 "
        "max_omissions_per_proposal=4 responsive_omission_period=41 "
        "contribution_ordinal=1 contribution_role=internal "
        "role_contribution_ordinal=1\n"
    ).format(digest="11" * 32, block=f"{1:064x}")
    log = tmp_path / "replica.log"
    log.write_text(line, encoding="utf-8")

    marker = validation._fault_markers(tmp_path, {2: ("replica.log",)})[0]
    assert marker.fault_mode == "tiered_persistent_responsive_omission_v2"
    assert marker.contribution_ordinal == 1
    assert marker.contribution_role == "internal"
    assert marker.role_contribution_ordinal == 1

    for missing in (
        " contribution_role=internal",
        " role_contribution_ordinal=1",
    ):
        log.write_text(line.replace(missing, ""), encoding="utf-8")
        with pytest.raises(FactorialValidationError, match="malformed"):
            validation._fault_markers(tmp_path, {2: ("replica.log",)})


def test_tiered_marker_schedule_proves_exact_period_ordinals_and_f_bound() -> None:
    markers = _valid_tiered_markers()
    arguments = {
        "actor_ids": (9, 10, 12),
        "responsive_degraded_actor_ids": (2,),
        "fault_mode": "tiered_persistent_responsive_omission_v1",
        "fault_threshold": 4,
        "max_omissions_per_proposal": 4,
        "responsive_omission_period": 41,
    }
    validation._validate_fault_marker_schedule(markers, **arguments)

    with pytest.raises(FactorialValidationError, match="audit fields"):
        validation._validate_fault_marker_schedule(
            (replace(markers[0], responsive_omission_period=40), *markers[1:]),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="not contiguous"):
        validation._validate_fault_marker_schedule(
            (*markers[:10], replace(markers[10], contribution_ordinal=12), *markers[11:]),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="not contiguous"):
        validation._validate_fault_marker_schedule(
            (markers[1], markers[0], *markers[2:]),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="every-41st"):
        validation._validate_fault_marker_schedule(
            (*markers[:40], replace(markers[40], action="forward"), *markers[41:]),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="persistently omit"):
        validation._validate_fault_marker_schedule(
            (*markers[:-1], replace(markers[-1], action="forward")),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="omission bound"):
        validation._validate_fault_marker_schedule(
            (*markers, replace(markers[-1], line_number=999)),
            **arguments,
        )


def _valid_role_scoped_tiered_markers() -> tuple[FaultMarker, ...]:
    markers: list[FaultMarker] = []
    for global_ordinal in range(1, 83):
        contribution_role = "internal" if global_ordinal % 2 else "leaf"
        role_ordinal = (global_ordinal + 1) // 2
        action = "forward"
        if role_ordinal % 41 == 0:
            action = (
                "omit_aggregate"
                if contribution_role == "internal"
                else "omit_direct_vote"
            )
        markers.append(
            _tiered_marker(
                actor=2,
                cohort="responsive_degraded",
                ordinal=global_ordinal,
                block_ordinal=global_ordinal,
                action=action,
                fault_mode="tiered_persistent_responsive_omission_v2",
                contribution_role=contribution_role,
                role_contribution_ordinal=role_ordinal,
            )
        )
    markers.extend(
        _tiered_marker(
            actor=actor,
            cohort="hard",
            ordinal=0,
            block_ordinal=82,
            action="omit_direct_vote",
            fault_mode="tiered_persistent_responsive_omission_v2",
            contribution_role="leaf",
            role_contribution_ordinal=0,
        )
        for actor in (9, 10, 12)
    )
    return tuple(markers)


def test_role_scoped_tiered_schedule_proves_global_and_per_role_ordinals() -> None:
    markers = _valid_role_scoped_tiered_markers()
    arguments = {
        "actor_ids": (9, 10, 12),
        "responsive_degraded_actor_ids": (2,),
        "fault_mode": "tiered_persistent_responsive_omission_v2",
        "fault_threshold": 4,
        "max_omissions_per_proposal": 4,
        "responsive_omission_period": 41,
    }
    validation._validate_fault_marker_schedule(markers, **arguments)

    reset_in_new_configuration = replace(
        markers[0],
        line_number=83,
        epoch_number=2,
        epoch_digest="22" * 32,
        block_hash=f"{83:064x}",
        monotonic_ns=283,
        contribution_ordinal=83,
        contribution_role="internal",
        role_contribution_ordinal=1,
    )
    validation._validate_fault_marker_schedule(
        (*markers[:-3], reset_in_new_configuration, *markers[-3:]),
        **arguments,
    )

    with pytest.raises(FactorialValidationError, match="role.*contiguous"):
        validation._validate_fault_marker_schedule(
            (
                *markers[:4],
                replace(markers[4], role_contribution_ordinal=4),
                *markers[5:],
            ),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="role/action"):
        validation._validate_fault_marker_schedule(
            (
                *markers[:80],
                replace(markers[80], contribution_role="leaf"),
                *markers[81:],
            ),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="role audit fields"):
        validation._validate_fault_marker_schedule(
            (replace(markers[0], contribution_role=None), *markers[1:]),
            **arguments,
        )


def test_v1_tiered_schedule_rejects_v2_role_fields() -> None:
    markers = _valid_tiered_markers()
    with pytest.raises(FactorialValidationError, match="v1.*role audit"):
        validation._validate_fault_marker_schedule(
            (
                replace(
                    markers[0],
                    contribution_role="internal",
                    role_contribution_ordinal=1,
                ),
                *markers[1:],
            ),
            actor_ids=(9, 10, 12),
            responsive_degraded_actor_ids=(2,),
            fault_mode="tiered_persistent_responsive_omission_v1",
            fault_threshold=4,
            max_omissions_per_proposal=4,
            responsive_omission_period=41,
        )


def test_role_scoped_schedule_requires_41_epoch1_internal_opportunities() -> None:
    actor = 2
    digest = "11" * 32
    tree = Tree(
        tree_id=4,
        fanout=2,
        pipeline_stretch=2,
        members=(0, actor, 1, 3, 4, 5, 6),
        wait_exempt=(),
    )
    markers = tuple(
        _tiered_marker(
            actor=actor,
            cohort="responsive_degraded",
            ordinal=ordinal,
            block_ordinal=ordinal,
            action="omit_aggregate" if ordinal == 41 else "forward",
            fault_mode="tiered_persistent_responsive_omission_v2",
            contribution_role="internal",
            role_contribution_ordinal=ordinal,
        )
        for ordinal in range(1, 42)
    )
    arguments = {
        "responsive_degraded_actor_ids": (actor,),
        "epoch1_digest": digest,
        "epoch1_trees": {tree.tree_id: tree},
        "epoch2_selection_ns": 1_000,
        "responsive_omission_period": 41,
    }

    validation._validate_role_scoped_epoch1_internal_opportunities(
        markers=markers,
        **arguments,
    )

    with pytest.raises(FactorialValidationError, match="internal opportunity count"):
        validation._validate_role_scoped_epoch1_internal_opportunities(
            markers=markers[:40],
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="audited physical role"):
        validation._validate_role_scoped_epoch1_internal_opportunities(
            markers=(replace(markers[0], contribution_role="leaf"), *markers[1:]),
            **arguments,
        )


def _v24_primary_epoch1_internal_opportunities(
    *,
    count: int = 82,
) -> tuple[
    tuple[validation.FaultContributionOpportunity, ...],
    Tree,
    str,
]:
    actor = 2
    digest = "11" * 32
    tree = Tree(
        tree_id=4,
        fanout=2,
        pipeline_stretch=2,
        members=(0, actor, 1, 3, 4, 5, 6),
        wait_exempt=(),
    )
    opportunities = tuple(
        validation.FaultContributionOpportunity(
            source_replica=actor,
            relative_path=f"raw/replica-{actor}.jsonl",
            line_number=ordinal,
            source_sequence=ordinal,
            event_monotonic_ns=1_000 + ordinal,
            line_sha256=f"{ordinal:064x}",
            actor=actor,
            epoch_number=1,
            tree_id=tree.tree_id,
            epoch_digest=digest,
            block_hash=f"{ordinal + 100:064x}",
            view_generation=(1 << 32) + 5,
            physical_role="internal",
            parent_replica=0,
            expected_message_type="aggregate_relay",
            cohort="responsive_degraded",
            window="v24-window",
            window_start_ns=100,
            window_end_ns=10_000,
            decision_monotonic_ns=1_000 + ordinal,
            contribution_ordinal=ordinal,
            role_contribution_ordinal=ordinal,
            scheduled_action=(
                "omit_aggregate" if ordinal in {41, 82} else "forward"
            ),
            responsive_omission_period=41,
            fault_threshold=2,
            hard_actor_count=1,
            responsive_degraded_actor_count=1,
            fault_mode="tiered_persistent_responsive_omission_v2",
        )
        for ordinal in range(1, count + 1)
    )
    return opportunities, tree, digest


def test_v24_primary_requires_82_source_bound_epoch1_internal_opportunities() -> None:
    opportunities, tree, digest = _v24_primary_epoch1_internal_opportunities()
    arguments = {
        "responsive_degraded_actor_ids": (2,),
        "epoch1_digest": digest,
        "epoch1_trees": {tree.tree_id: tree},
        "epoch2_selection_ns": 2_000,
        "minimum_opportunities": 82,
        "responsive_omission_period": 41,
    }

    validation._validate_primary_epoch1_internal_role_opportunity_exposure(
        opportunities=opportunities,
        **arguments,
    )

    with pytest.raises(FactorialValidationError, match="82.*internal.*opportunities"):
        validation._validate_primary_epoch1_internal_role_opportunity_exposure(
            opportunities=opportunities[:81],
            **arguments,
        )


@pytest.mark.parametrize(
    "mutation,reason",
    (
        (
            lambda rows: (
                *rows[:40],
                replace(rows[40], role_contribution_ordinal=42),
                *rows[41:],
            ),
            "contiguous",
        ),
        (
            lambda rows: (
                *rows[:40],
                replace(rows[40], scheduled_action="forward"),
                *rows[41:],
            ),
            "41.*82",
        ),
        (
            lambda rows: (
                replace(rows[0], physical_role="leaf"),
                *rows[1:],
            ),
            "82.*internal.*opportunities",
        ),
        (
            lambda rows: (
                replace(rows[0], event_monotonic_ns=2_000),
                *rows[1:],
            ),
            "82.*internal.*opportunities",
        ),
        (
            lambda rows: (
                replace(rows[0], decision_monotonic_ns=2_000),
                *rows[1:],
            ),
            "82.*internal.*opportunities",
        ),
        (
            lambda rows: (
                *rows[:40],
                replace(rows[40], source_sequence=40),
                *rows[41:],
            ),
            "source/cohort/order",
        ),
    ),
)
def test_v24_primary_internal_opportunity_prefix_is_fail_closed(
    mutation: object,
    reason: str,
) -> None:
    opportunities, tree, digest = _v24_primary_epoch1_internal_opportunities()
    assert callable(mutation)
    mutated = mutation(opportunities)

    with pytest.raises(FactorialValidationError, match=reason):
        validation._validate_primary_epoch1_internal_role_opportunity_exposure(
            opportunities=mutated,
            responsive_degraded_actor_ids=(2,),
            epoch1_digest=digest,
            epoch1_trees={tree.tree_id: tree},
            epoch2_selection_ns=2_000,
            minimum_opportunities=82,
            responsive_omission_period=41,
        )


def test_v24_epoch1_preselection_residency_uses_later_activation_convergence_anchor() -> None:
    arguments = {
        "epoch1_activation_ns": 1_000_000_000,
        "epoch1_convergence_ns": 2_000_000_000,
        "minimum_residency_ms": 60_000,
    }
    validation._validate_epoch1_preselection_residency(
        epoch2_selection_ns=62_000_000_000,
        **arguments,
    )

    with pytest.raises(FactorialValidationError, match="60.*residency"):
        validation._validate_epoch1_preselection_residency(
            epoch2_selection_ns=61_999_999_999,
            **arguments,
        )

    with pytest.raises(FactorialValidationError, match="anchor"):
        validation._validate_epoch1_preselection_residency(
            epoch1_activation_ns=3_000_000_000,
            epoch1_convergence_ns=2_000_000_000,
            epoch2_selection_ns=62_000_000_000,
            minimum_residency_ms=60_000,
        )


def _v24_preselection_manifest() -> SimpleNamespace:
    return SimpleNamespace(
        manifest_id="shape-placement-factorial-v24",
        workload=SimpleNamespace(
            bucket_width_s=5,
            epoch1_stable_bucket_count=6,
            epoch1_preselection_residency_ms=60_000,
        ),
        byzantine=SimpleNamespace(
            responsive_degradation=SimpleNamespace(
                minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection=82,
            )
        ),
    )


def test_v24_preselection_contract_is_version_exact_and_keeps_six_measured_buckets() -> None:
    assert validation._v24_preselection_contract(
        _v24_preselection_manifest()
    ) == (60_000, 82)

    v25 = _v24_preselection_manifest()
    v25.manifest_id = "shape-placement-factorial-v25"
    assert validation._v24_preselection_contract(v25) == (60_000, 82)

    v23 = _v24_preselection_manifest()
    v23.manifest_id = "shape-placement-factorial-v23"
    v23.workload.epoch1_preselection_residency_ms = 30_000
    v23.byzantine.responsive_degradation.minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection = 41
    assert validation._v24_preselection_contract(v23) is None


@pytest.mark.parametrize(
    "path,value",
    (
        ("residency", 59_999),
        ("opportunities", 81),
        ("bucket_width", 6),
        ("bucket_count", 12),
    ),
)
def test_v24_preselection_contract_rejects_field_or_measurement_drift(
    path: str,
    value: int,
) -> None:
    manifest = _v24_preselection_manifest()
    if path == "residency":
        manifest.workload.epoch1_preselection_residency_ms = value
    elif path == "opportunities":
        manifest.byzantine.responsive_degradation.minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection = value
    elif path == "bucket_width":
        manifest.workload.bucket_width_s = value
    else:
        manifest.workload.epoch1_stable_bucket_count = value

    with pytest.raises(FactorialValidationError, match="v24.*preselection"):
        validation._v24_preselection_contract(manifest)


@pytest.mark.parametrize(
    "manifest_id,replica_count,initial_fanout,arm_code,campaign_member,coverage_smoke,expected",
    (
        ("shape-placement-factorial-v24", 31, 5, "P", True, False, 82),
        ("shape-placement-factorial-v24", 31, 5, "PS", False, True, 82),
        ("shape-placement-factorial-v23", 31, 5, "P", True, False, None),
        ("shape-placement-factorial-v24", 7, 5, "P", True, False, None),
        ("shape-placement-factorial-v24", 31, 2, "P", True, False, None),
        ("shape-placement-factorial-v24", 31, 5, "S", True, False, None),
        ("shape-placement-factorial-v24", 31, 5, "00", True, False, None),
        ("shape-placement-factorial-v24", 31, 5, "P", False, False, None),
    ),
)
def test_v24_primary_exposure_dispatch_is_exact(
    manifest_id: str,
    replica_count: int,
    initial_fanout: int,
    arm_code: str,
    campaign_member: bool,
    coverage_smoke: bool,
    expected: int | None,
) -> None:
    manifest = _v24_preselection_manifest()
    manifest.manifest_id = manifest_id
    assert validation._primary_epoch1_internal_role_opportunity_minimum(
        manifest,
        replica_count=replica_count,
        initial_fanout=initial_fanout,
        arm_code=arm_code,
        campaign_member=campaign_member,
        coverage_smoke=coverage_smoke,
    ) == expected


def test_v24_primary_exposure_requires_source_bound_opportunity_dispatch() -> None:
    with pytest.raises(FactorialValidationError, match="source-bound"):
        validate_fault_causality(
            **_epoch1_causal_prefix_fixture(),
            minimum_epoch1_internal_role_opportunities_per_actor_before_selection=82,
        )


def _v9_cross_commit_witness_fixture() -> dict[str, object]:
    digest = "11" * 32
    markers: list[FaultMarker] = []
    arm_markers: list[validation.ResponseAttemptArmMarker] = []
    trees: dict[int, Tree] = {}
    commit_times: dict[tuple[int, int, str, str], int] = {}
    proposal_commit_times: dict[
        int,
        dict[tuple[int, int, str, str], tuple[int, ...]],
    ] = {}
    timeout_index: dict[
        tuple[int, int, int, str, str], tuple[validation._EvidenceRecord, ...]
    ] = {}
    for offset, actor in enumerate((2, 5)):
        reporter = 1 + offset * 2
        tree_id = 4 + offset
        block_hash = f"{900 + offset:064x}"
        marker_ns = 2_000_000 + offset * 3_000_000
        start_ns = marker_ns - 100_000
        deadline_duration_us = 500
        absolute_deadline_ns = start_ns + deadline_duration_us * 1_000
        members = (
            reporter,
            actor,
            *(
                member
                for member in range(13)
                if member not in {reporter, actor}
            ),
        )
        trees[tree_id] = Tree(
            tree_id=tree_id,
            fanout=5,
            pipeline_stretch=2,
            members=members,
            wait_exempt=(9, 10, 12),
        )
        marker = replace(
            _tiered_marker(
                actor=actor,
                cohort="responsive_degraded",
                ordinal=41,
                block_ordinal=900 + offset,
                action="omit_aggregate",
            ),
            tree_id=tree_id,
            epoch_digest=digest,
            block_hash=block_hash,
            monotonic_ns=marker_ns,
        )
        markers.append(marker)
        proposal_key = (1, tree_id, digest, block_hash)
        commit_times[proposal_key] = marker_ns + 50_000
        proposal_commit_times.setdefault(reporter, {})[proposal_key] = (
            marker_ns + 100_000,
        )
        arm_markers.append(
            validation.ResponseAttemptArmMarker(
                source_replica=reporter,
                line_number=offset + 1,
                reporter_id=reporter,
                child_id=actor,
                epoch_number=1,
                tree_id=tree_id,
                epoch_digest=digest,
                block_hash=block_hash,
                expected_message_type="aggregate_relay",
                start_monotonic_ns=start_ns,
                deadline_duration_us=deadline_duration_us,
                absolute_deadline_ns=absolute_deadline_ns,
                raw_line_sha256=f"{offset + 1:064x}",
            )
        )
        timeout_index[(actor, *proposal_key)] = (
            validation._EvidenceRecord(
                ingestion_sequence=offset + 1,
                acceptance_monotonic_ns=absolute_deadline_ns + 50_000,
                observation_id=f"{offset + 1:064x}",
                reporter_id=reporter,
                target_id=actor,
                epoch_number=1,
                tree_id=tree_id,
                epoch_digest=digest,
                block_hash=block_hash,
                message_type="aggregate_relay",
                outcome="timeout",
                response_duration_us=0,
                deadline_duration_us=deadline_duration_us,
                reporter_monotonic_ns=absolute_deadline_ns,
                reporter_sequence=offset + 1,
                signer_set=(),
            ),
        )
    return {
        "markers": tuple(markers),
        "arm_markers": tuple(arm_markers),
        "responsive_degraded_actor_ids": (2, 5),
        "authoritative_commit_ns": commit_times,
        "proposal_commit_ns_by_replica": proposal_commit_times,
        "epoch1_trees": trees,
        "epoch1_timeout_index": timeout_index,
        "epoch2_selection_ns": 10_000_000,
    }


def _epoch1_causal_prefix_fixture() -> dict[str, object]:
    actor = 2
    epoch1_digest = "11" * 32
    tree = Tree(
        tree_id=0,
        fanout=2,
        pipeline_stretch=2,
        members=(0, 1, actor),
        wait_exempt=(),
    )
    markers = tuple(
        FaultMarker(
            source_replica=actor,
            line_number=index,
            fault_mode="tiered_persistent_responsive_omission_v1",
            epoch_number=1,
            tree_id=tree.tree_id,
            epoch_digest=epoch1_digest,
            block_hash=f"{height:064x}",
            window="causal-prefix-window",
            window_start_ns=100,
            window_end_ns=10_000,
            actor=actor,
            action="omit_direct_vote",
            monotonic_ns=marker_ns,
            raw_line_sha256=f"{index:064x}",
            cohort="responsive_degraded",
            hard_actor_count=0,
            responsive_degraded_actor_count=1,
            fault_threshold=1,
            max_omissions_per_proposal=1,
            responsive_omission_period=41,
            contribution_ordinal=41,
        )
        for index, (height, marker_ns) in enumerate(
            ((900, 2_000), (901, 4_000)),
            start=1,
        )
    )

    def timeout(
        marker: FaultMarker,
        *,
        ingestion_sequence: int,
        observation_id: str,
        reporter_ns: int,
    ) -> validation._EvidenceRecord:
        return validation._EvidenceRecord(
            ingestion_sequence=ingestion_sequence,
            acceptance_monotonic_ns=reporter_ns + 10,
            observation_id=observation_id,
            reporter_id=0,
            target_id=actor,
            epoch_number=marker.epoch_number,
            tree_id=marker.tree_id,
            epoch_digest=marker.epoch_digest,
            block_hash=marker.block_hash,
            message_type="direct_vote",
            outcome="timeout",
            response_duration_us=0,
            deadline_duration_us=100,
            reporter_monotonic_ns=reporter_ns,
            reporter_sequence=ingestion_sequence,
            signer_set=(),
        )

    accepted_epoch1 = (
        timeout(
            markers[0],
            ingestion_sequence=1,
            observation_id="a" * 64,
            reporter_ns=2_500,
        ),
        timeout(
            markers[1],
            ingestion_sequence=2,
            observation_id="b" * 64,
            reporter_ns=4_500,
        ),
        timeout(
            markers[1],
            ingestion_sequence=3,
            observation_id="c" * 64,
            reporter_ns=4_600,
        ),
    )
    replica_events = {
        0: tuple(
            _commit_event(
                index,
                marker.monotonic_ns + 100,
                899 + index,
                epoch_number=1,
                epoch_digest=epoch1_digest,
                tree_id=tree.tree_id,
            )
            for index, marker in enumerate(markers, start=1)
        )
    }
    return {
        "markers": markers,
        "replica_events": replica_events,
        "actor_ids": (),
        "fault_mode": "tiered_persistent_responsive_omission_v1",
        "max_omissions_per_proposal": 1,
        "initial_epoch_digest": "00" * 32,
        "initial_trees": (tree,),
        "window_id": "causal-prefix-window",
        "window_start_ns": 100,
        "window_end_ns": 10_000,
        "epoch1_command_ns": 900,
        "epoch1_activation_ns": 1_000,
        "epoch2_command_ns": 9_000,
        "epoch1_digest": epoch1_digest,
        "epoch1_trees": (tree,),
        "epoch2_digest": "22" * 32,
        "epoch2_trees": (tree,),
        "phase_windows": {
            "baseline": (100, 150, 1),
            "fault_evidence": (200, 800, 1),
            "epoch1_stable": (1_200, 8_000, 1),
            "epoch2_stable": (9_200, 9_800, 1),
        },
        "required_reporters": 1,
        "accepted_epoch1": accepted_epoch1,
        "epoch1_baseline_cutoff": 2,
        "epoch1_current_cutoff": 3,
        "responsive_degraded_actor_ids": (actor,),
    }


def _v34_post_fault_epoch2_causality_fixture() -> dict[str, object]:
    arguments = _epoch1_causal_prefix_fixture()
    fault_end_ns = 9_000
    markers = tuple(
        replace(marker, window_end_ns=fault_end_ns)
        for marker in arguments["markers"]
    )
    return {
        **arguments,
        "markers": markers,
        "window_end_ns": fault_end_ns,
        "fault_active_phase_contract": (
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
        ),
    }


def test_v34_slot037_accepts_post_fault_epoch2_stable_window_only_with_v2_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)

    assert validate_fault_causality(
        **_v34_post_fault_epoch2_causality_fixture(),
        explicit_causal_linkage_windows=True,
    ) == 0


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("missing-token", "outside their exact live bounds"),
        ("unknown-token", "unknown fault-active phase contract"),
        ("epoch1-end-at-fault-end", "physical-fault bounds"),
        ("epoch2-start-at-fault-end", "physical-fault bounds"),
        ("epoch2-marker", "Epoch2 KAURI_FAULT marker"),
        ("epoch2-opportunity", "Epoch2 contribution opportunity"),
        ("epoch2-marker-and-opportunity", "Epoch2 KAURI_FAULT marker"),
        ("out-of-window-marker", "exact slot actor/window"),
    ),
)
def test_v34_post_fault_epoch2_causality_scope_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    reason: str,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _v34_post_fault_epoch2_causality_fixture()
    if mutation == "missing-token":
        arguments.pop("fault_active_phase_contract")
    elif mutation == "unknown-token":
        arguments["fault_active_phase_contract"] += "-drift"
    elif mutation == "epoch1-end-at-fault-end":
        phase_windows = dict(arguments["phase_windows"])
        start, _end, count = phase_windows["epoch1_stable"]
        phase_windows["epoch1_stable"] = (
            start,
            arguments["window_end_ns"],
            count,
        )
        arguments["phase_windows"] = phase_windows
    elif mutation == "epoch2-start-at-fault-end":
        phase_windows = dict(arguments["phase_windows"])
        _start, end, count = phase_windows["epoch2_stable"]
        phase_windows["epoch2_stable"] = (
            arguments["window_end_ns"],
            end,
            count,
        )
        arguments["phase_windows"] = phase_windows
    elif mutation in {"epoch2-marker", "epoch2-marker-and-opportunity"}:
        marker = arguments["markers"][0]
        arguments["markers"] = (
            *arguments["markers"],
            replace(
                marker,
                epoch_number=2,
                epoch_digest=arguments["epoch2_digest"],
                block_hash="fe" * 32,
                line_number=99,
                monotonic_ns=8_500,
                raw_line_sha256="ef" * 32,
            ),
        )
    if mutation in {"epoch2-opportunity", "epoch2-marker-and-opportunity"}:
        arguments["contribution_opportunities"] = (
            validation.FaultContributionOpportunity(
                source_replica=2,
                relative_path="raw/replica-2.jsonl",
                line_number=1,
                source_sequence=1,
                event_monotonic_ns=8_500,
                line_sha256="aa" * 32,
                actor=2,
                epoch_number=2,
                tree_id=0,
                epoch_digest=arguments["epoch2_digest"],
                block_hash="fe" * 32,
                view_generation=1,
                physical_role="leaf",
                parent_replica=0,
                expected_message_type="direct_vote",
                cohort="responsive_degraded",
                window="causal-prefix-window",
                window_start_ns=100,
                window_end_ns=9_000,
                decision_monotonic_ns=8_500,
                contribution_ordinal=1,
                role_contribution_ordinal=1,
                scheduled_action="forward",
                responsive_omission_period=41,
                fault_threshold=1,
                hard_actor_count=0,
                responsive_degraded_actor_count=1,
                fault_mode="tiered_persistent_responsive_omission_v1",
            ),
        )
    elif mutation == "out-of-window-marker":
        arguments["markers"] = (
            replace(
                arguments["markers"][0],
                monotonic_ns=arguments["window_end_ns"],
            ),
            *arguments["markers"][1:],
        )

    with pytest.raises(FactorialValidationError, match=reason):
        validate_fault_causality(
            **arguments,
            explicit_causal_linkage_windows=True,
        )


def _v34_fault_active_opportunity_fixture() -> dict[str, object]:
    actors = (2, 9)
    tree = Tree(0, 2, 2, (0, 2, 1, 9, 3, 4, 5), (9,))
    phase_rows = (
        ("fault_evidence", 0, "11" * 32, 300),
        ("epoch1_stable", 1, "22" * 32, 1_300),
    )
    phase_windows = {
        "baseline": (10, 90, 1),
        "fault_evidence": (100, 700, 6),
        "epoch1_stable": (1_100, 1_700, 6),
        "epoch2_stable": (2_200, 2_800, 6),
    }
    markers: list[FaultMarker] = []
    events: dict[int, list[validation._NativeEvent]] = {
        actor: [] for actor in actors
    }
    for phase_index, (_phase, epoch, digest, timestamp) in enumerate(phase_rows):
        for actor in actors:
            marker = _v14_marker(
                actor=actor,
                tree=tree,
                epoch_number=epoch,
                epoch_digest=digest,
                block_ordinal=phase_index * 10 + actor,
                monotonic_ns=timestamp + actor,
                cohort="responsive_degraded" if actor == 2 else "hard",
                contribution_ordinal=phase_index + 1,
                role_contribution_ordinal=phase_index + 1,
            )
            markers.append(marker)
            events[actor].append(
                _v14_opportunity_event(
                    marker,
                    tree,
                    sequence=len(events[actor]) + 1,
                )
            )
    return {
        "markers": tuple(markers),
        "opportunities": validation._fault_contribution_opportunities(
            {actor: tuple(rows) for actor, rows in events.items()}
        ),
        "fault_actor_ids": actors,
        "phase_windows": phase_windows,
        "phase_configurations": tuple(
            (phase, epoch, digest, {tree.tree_id: tree})
            for phase, epoch, digest, _timestamp in phase_rows
        ),
    }


@pytest.mark.parametrize(
    ("phase", "actor"),
    (("fault_evidence", 9), ("epoch1_stable", 2)),
)
def test_v34_fault_active_scope_requires_each_fault_and_epoch1_opportunity(
    phase: str,
    actor: int,
) -> None:
    arguments = _v34_fault_active_opportunity_fixture()
    epoch = 0 if phase == "fault_evidence" else 1
    markers = tuple(
        marker
        for marker in arguments["markers"]
        if not (marker.epoch_number == epoch and marker.actor == actor)
    )
    opportunities = tuple(
        opportunity
        for opportunity in arguments["opportunities"]
        if not (opportunity.epoch_number == epoch and opportunity.actor == actor)
    )

    with pytest.raises(
        FactorialValidationError,
        match=rf"{phase} scheduled actor {actor} lacks",
    ):
        validation._validate_fault_contribution_opportunity_bijection(
            **{
                **arguments,
                "markers": markers,
                "opportunities": opportunities,
            }
        )


def test_v34_source_bound_scope_passes_only_fault_and_epoch1_configurations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _v34_post_fault_epoch2_causality_fixture()
    tree = arguments["epoch1_trees"][0]
    markers = tuple(
        replace(
            marker,
            fault_mode="tiered_persistent_responsive_omission_v2",
            contribution_role="leaf",
            role_contribution_ordinal=marker.contribution_ordinal,
            hard_actor_count=1,
            fault_threshold=2,
        )
        for marker in arguments["markers"]
    )
    opportunity_events = tuple(
        _v14_opportunity_event(marker, tree, sequence=index)
        for index, marker in enumerate(markers, start=1)
    )
    opportunities = validation._fault_contribution_opportunities(
        {2: opportunity_events}
    )
    observed: list[tuple[str, tuple[str, ...]]] = []

    def capture_bijection(**kwargs: object) -> None:
        configurations = kwargs["phase_configurations"]
        observed.append(
            ("bijection", tuple(row[0] for row in configurations))
        )

    def capture_witnesses(**kwargs: object) -> set[tuple[int, int, str, str]]:
        configurations = kwargs["phase_configurations"]
        observed.append(
            ("proposal", tuple(row[0] for row in configurations))
        )
        return set()

    monkeypatch.setattr(
        validation,
        "_validate_fault_contribution_opportunity_bijection",
        capture_bijection,
    )
    monkeypatch.setattr(
        validation,
        "_source_bound_proposal_configuration_witnesses",
        capture_witnesses,
    )
    monkeypatch.setattr(
        validation,
        "_validate_role_scoped_epoch1_internal_opportunities",
        lambda **_kwargs: None,
    )

    assert validate_fault_causality(
        **{
            **arguments,
            "markers": markers,
            "contribution_opportunities": opportunities,
            "fault_mode": "tiered_persistent_responsive_omission_v2",
            "source_bound_contribution_opportunities": True,
            "source_bound_proposal_configuration_witnesses": True,
        },
        explicit_causal_linkage_windows=True,
    ) == 0
    assert observed == [
        ("bijection", ("fault_evidence", "epoch1_stable")),
        ("proposal", ("fault_evidence", "epoch1_stable")),
    ]


def _skip_tiered_schedule_shape_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        validation,
        "_validate_fault_marker_schedule",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        validation,
        "_validate_tiered_observed_marker_completeness",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        validation,
        "_validate_persistent_interior_proposals",
        lambda *a, **k: None,
    )


def test_epoch1_causality_uses_full_prefix_and_selection_uses_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)

    assert validate_fault_causality(
        **_epoch1_causal_prefix_fixture(),
        explicit_causal_linkage_windows=True,
    ) == 0


def test_role_scoped_causality_binds_marker_role_to_physical_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        validation,
        "_validate_role_scoped_epoch1_internal_opportunities",
        lambda **arguments: observed.append(arguments),
    )
    arguments = _epoch1_causal_prefix_fixture()
    markers = tuple(
        replace(
            marker,
            fault_mode="tiered_persistent_responsive_omission_v2",
            contribution_role="leaf",
            role_contribution_ordinal=index,
        )
        for index, marker in enumerate(arguments["markers"], start=1)
    )
    arguments.update(
        {
            "markers": markers,
            "fault_mode": "tiered_persistent_responsive_omission_v2",
        }
    )

    assert validate_fault_causality(
        **arguments,
        explicit_causal_linkage_windows=True,
    ) == 0
    assert len(observed) == 1

    with pytest.raises(FactorialValidationError, match="physical tree role"):
        validate_fault_causality(
            **{
                **arguments,
                "markers": (
                    replace(markers[0], contribution_role="internal"),
                    *markers[1:],
                ),
            },
            explicit_causal_linkage_windows=True,
        )


def test_v13_through_v17_causality_dispatch_keeps_structural_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        validation,
        "_validate_fault_marker_schedule",
        lambda *args, **kwargs: calls.append("schedule"),
    )
    monkeypatch.setattr(
        validation,
        "_validate_tiered_observed_marker_completeness",
        lambda *args, **kwargs: calls.append("legacy_completeness"),
    )
    monkeypatch.setattr(
        validation,
        "_validate_persistent_interior_proposals",
        lambda *args, **kwargs: calls.append("legacy_persistent"),
    )
    monkeypatch.setattr(
        validation,
        "_validate_fault_contribution_opportunity_bijection",
        lambda *args, **kwargs: calls.append("v14_bijection"),
    )
    monkeypatch.setattr(
        validation,
        "_validate_role_scoped_epoch1_internal_opportunities",
        lambda *args, **kwargs: calls.append("role41"),
    )

    def cross_commit(**_arguments: object) -> tuple[int, ...]:
        calls.append("cross_commit")
        return (2,)

    monkeypatch.setattr(
        validation,
        "_validate_v9_cross_commit_retention_witnesses",
        cross_commit,
    )
    arguments = _epoch1_causal_prefix_fixture()
    markers = tuple(
        replace(
            marker,
            fault_mode="tiered_persistent_responsive_omission_v2",
            contribution_role="leaf",
            role_contribution_ordinal=index,
        )
        for index, marker in enumerate(arguments["markers"], start=1)
    )
    common = {
        **arguments,
        "markers": markers,
        "fault_mode": "tiered_persistent_responsive_omission_v2",
        "require_cross_commit_retention_witnesses": True,
        "explicit_causal_linkage_windows": True,
    }

    assert validate_fault_causality(**common) == 1
    assert calls == [
        "schedule",
        "legacy_completeness",
        "legacy_persistent",
        "role41",
        "cross_commit",
    ]

    calls.clear()
    assert validate_fault_causality(
        **common,
        source_bound_contribution_opportunities=True,
        selection_visible_hard_timeout_witnesses=True,
    ) == 1
    assert calls == ["schedule", "v14_bijection", "role41", "cross_commit"]


def test_v9_epoch1_causality_retains_its_frozen_suffix_interpretation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded omission has no exact outstanding raw timeout",
    ):
        validate_fault_causality(**_epoch1_causal_prefix_fixture())


def test_v9_cross_commit_receives_the_full_epoch1_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    captured_sequences: set[int] = set()

    def capture_cross_commit_prefix(**arguments: object) -> tuple[int, ...]:
        timeout_index = arguments["epoch1_timeout_index"]
        assert isinstance(timeout_index, dict)
        captured_sequences.update(
            record.ingestion_sequence
            for records in timeout_index.values()
            for record in records
        )
        return (2,)

    monkeypatch.setattr(
        validation,
        "_validate_v9_cross_commit_retention_witnesses",
        capture_cross_commit_prefix,
    )
    arguments = _epoch1_causal_prefix_fixture()

    assert validate_fault_causality(
        **{
            **arguments,
            "require_cross_commit_retention_witnesses": True,
            "explicit_causal_linkage_windows": True,
        }
    ) == 1
    assert captured_sequences == {1, 2, 3}


def test_epoch1_causality_rejects_missing_prebaseline_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _epoch1_causal_prefix_fixture()
    accepted = arguments["accepted_epoch1"]

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded omission has no exact outstanding raw timeout",
    ):
        validate_fault_causality(
            **{
                **arguments,
                "accepted_epoch1": accepted[1:],
                "explicit_causal_linkage_windows": True,
            }
        )


def test_epoch1_selection_linkage_rejects_missing_suffix_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _epoch1_causal_prefix_fixture()
    accepted = arguments["accepted_epoch1"]

    with pytest.raises(
        FactorialValidationError,
        match="performance-selection evidence",
    ):
        validate_fault_causality(
            **{
                **arguments,
                "accepted_epoch1": accepted[:2],
                "explicit_causal_linkage_windows": True,
            }
        )


def test_timeout_causality_excludes_only_exact_arms_maturing_after_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _epoch1_causal_prefix_fixture()
    original_markers = arguments["markers"]
    assert isinstance(original_markers, tuple)
    actor = original_markers[0].actor
    late_marker = replace(
        original_markers[-1],
        line_number=3,
        block_hash=f"{902:064x}",
        monotonic_ns=8_500,
        raw_line_sha256="d" * 64,
    )
    markers = (*original_markers, late_marker)
    arm_markers = tuple(
        validation.ResponseAttemptArmMarker(
            source_replica=0,
            line_number=index,
            reporter_id=0,
            child_id=actor,
            epoch_number=marker.epoch_number,
            tree_id=marker.tree_id,
            epoch_digest=marker.epoch_digest,
            block_hash=marker.block_hash,
            expected_message_type="direct_vote",
            start_monotonic_ns=(
                marker.monotonic_ns - 500
                if marker is not late_marker
                else 7_500
            ),
            deadline_duration_us=(
                1 if marker is not late_marker else 2
            ),
            absolute_deadline_ns=(
                marker.monotonic_ns + 500
                if marker is not late_marker
                else 9_500
            ),
            raw_line_sha256=f"{index + 10:064x}",
        )
        for index, marker in enumerate(markers, start=1)
    )
    replica_events = dict(arguments["replica_events"])
    replica_events[0] = (
        *replica_events[0],
        _commit_event(
            3,
            8_600,
            902,
            epoch_number=1,
            epoch_digest=late_marker.epoch_digest,
            tree_id=late_marker.tree_id,
        ),
    )
    explicit_arguments = {
        **arguments,
        "markers": markers,
        "arm_markers": arm_markers,
        "replica_events": replica_events,
        "accepted_epoch1": tuple(
            replace(record, deadline_duration_us=1)
            for record in arguments["accepted_epoch1"]
        ),
        "explicit_causal_linkage_windows": True,
        "explicit_phase_edge_eligibility": True,
        "epoch1_selection_ns": 800,
        "epoch2_selection_ns": 8_900,
    }

    assert validate_fault_causality(**explicit_arguments) == 0

    marker_by_hash = {marker.block_hash: marker for marker in original_markers}
    raced_arm_markers = tuple(
        replace(
            arm,
            start_monotonic_ns=marker_by_hash[arm.block_hash].monotonic_ns + 100,
            absolute_deadline_ns=(
                marker_by_hash[arm.block_hash].monotonic_ns + 1_100
            ),
        )
        if arm.block_hash in marker_by_hash
        else arm
        for arm in arm_markers
    )
    raced_timeouts = tuple(
        replace(
            record,
            reporter_monotonic_ns=(
                marker_by_hash[record.block_hash].monotonic_ns + 1_100
            ),
            acceptance_monotonic_ns=(
                marker_by_hash[record.block_hash].monotonic_ns + 1_110
            ),
        )
        for record in explicit_arguments["accepted_epoch1"]
    )
    assert validate_fault_causality(
        **{
            **explicit_arguments,
            "arm_markers": raced_arm_markers,
            "accepted_epoch1": raced_timeouts,
        }
    ) == 0

    equality_arm = replace(
        arm_markers[-1],
        start_monotonic_ns=7_900,
        deadline_duration_us=1,
        absolute_deadline_ns=8_900,
    )
    assert validate_fault_causality(
        **{
            **explicit_arguments,
            "arm_markers": (*arm_markers[:-1], equality_arm),
        }
    ) == 0

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded omission has no exact outstanding raw timeout",
    ):
        validate_fault_causality(
            **{
                **explicit_arguments,
                "arm_markers": (
                    *arm_markers[:-1],
                    replace(
                        equality_arm,
                        start_monotonic_ns=7_899,
                        absolute_deadline_ns=8_899,
                    ),
                ),
            }
        )

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded omission has no exact outstanding raw timeout",
    ):
        validate_fault_causality(
            **{
                **explicit_arguments,
                "explicit_phase_edge_eligibility": False,
            }
        )

    with pytest.raises(
        FactorialValidationError,
        match="exact parent response-attempt arm",
    ):
        validate_fault_causality(
            **{
                **explicit_arguments,
                "arm_markers": arm_markers[:-1],
            }
        )

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded omission has no exact outstanding raw timeout",
    ):
        validate_fault_causality(
            **{
                **explicit_arguments,
                "accepted_epoch1": tuple(
                    replace(record, deadline_duration_us=2)
                    if record.block_hash == original_markers[1].block_hash
                    else record
                    for record in explicit_arguments["accepted_epoch1"]
                ),
            }
        )


def _selection_visible_responsive_tail_fixture() -> dict[str, object]:
    """Model the sealed N31 acceptance-lag tail at a compact time scale."""

    arguments = _epoch1_causal_prefix_fixture()
    original_markers = arguments["markers"]
    assert isinstance(original_markers, tuple)
    actor = original_markers[0].actor
    tail_marker = replace(
        original_markers[-1],
        line_number=3,
        block_hash=f"{902:064x}",
        monotonic_ns=8_500,
        raw_line_sha256="d" * 64,
    )
    markers = (*original_markers, tail_marker)
    arm_markers = tuple(
        validation.ResponseAttemptArmMarker(
            source_replica=0,
            line_number=index,
            reporter_id=0,
            child_id=actor,
            epoch_number=marker.epoch_number,
            tree_id=marker.tree_id,
            epoch_digest=marker.epoch_digest,
            block_hash=marker.block_hash,
            expected_message_type="direct_vote",
            start_monotonic_ns=(
                marker.monotonic_ns - 500
                if marker is not tail_marker
                else 7_899
            ),
            deadline_duration_us=1,
            absolute_deadline_ns=(
                marker.monotonic_ns + 500
                if marker is not tail_marker
                else 8_899
            ),
            raw_line_sha256=f"{index + 10:064x}",
        )
        for index, marker in enumerate(markers, start=1)
    )
    replica_events = dict(arguments["replica_events"])
    replica_events[0] = (
        *replica_events[0],
        _commit_event(
            3,
            8_600,
            902,
            epoch_number=1,
            epoch_digest=tail_marker.epoch_digest,
            tree_id=tail_marker.tree_id,
        ),
    )
    accepted_epoch1 = tuple(
        replace(record, deadline_duration_us=1)
        for record in arguments["accepted_epoch1"]
    )
    tail_timeout = validation._EvidenceRecord(
        ingestion_sequence=4,
        acceptance_monotonic_ns=8_920,
        observation_id="d" * 64,
        reporter_id=0,
        target_id=actor,
        epoch_number=tail_marker.epoch_number,
        tree_id=tail_marker.tree_id,
        epoch_digest=tail_marker.epoch_digest,
        block_hash=tail_marker.block_hash,
        message_type="direct_vote",
        outcome="timeout",
        response_duration_us=0,
        deadline_duration_us=1,
        reporter_monotonic_ns=8_899,
        reporter_sequence=4,
        signer_set=(),
    )
    return {
        **arguments,
        "markers": markers,
        "arm_markers": arm_markers,
        "replica_events": replica_events,
        "accepted_epoch1": (*accepted_epoch1, tail_timeout),
        "explicit_causal_linkage_windows": True,
        "explicit_phase_edge_eligibility": True,
        "epoch1_selection_ns": 800,
        "epoch2_selection_ns": 8_900,
    }


def test_v21_responsive_acceptance_lag_tail_is_a_nonwitness_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _selection_visible_responsive_tail_fixture()

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded omission has no exact outstanding raw timeout",
    ):
        validate_fault_causality(**arguments)

    assert validate_fault_causality(
        **arguments,
        selection_visible_responsive_timeout_nonwitnesses=True,
    ) == 0

    accepted_epoch1 = arguments["accepted_epoch1"]
    assert isinstance(accepted_epoch1, tuple)
    assert validate_fault_causality(
        **{
            **arguments,
            "accepted_epoch1": accepted_epoch1[:-1],
            "selection_visible_responsive_timeout_nonwitnesses": True,
        }
    ) == 0

    arm_markers = arguments["arm_markers"]
    assert isinstance(arm_markers, tuple)
    with pytest.raises(
        FactorialValidationError,
        match="exact parent response-attempt arm",
    ):
        validate_fault_causality(
            **{
                **arguments,
                "arm_markers": arm_markers[:-1],
                "selection_visible_responsive_timeout_nonwitnesses": True,
            }
        )

    with pytest.raises(
        FactorialValidationError,
        match="responsive timeout nonwitnesses require explicit phase-edge",
    ):
        validate_fault_causality(
            **{
                **arguments,
                "explicit_phase_edge_eligibility": False,
                "selection_visible_responsive_timeout_nonwitnesses": True,
            }
        )


@pytest.mark.parametrize(
    "mutation",
    (
        {"reporter_id": 1},
        {"message_type": "aggregate_relay"},
        {"deadline_duration_us": 2},
        {"reporter_monotonic_ns": 8_400},
        {"acceptance_monotonic_ns": 8_900},
    ),
)
def test_v21_responsive_present_prefix_timeout_mismatch_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, object],
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _selection_visible_responsive_tail_fixture()
    accepted_epoch1 = arguments["accepted_epoch1"]
    assert isinstance(accepted_epoch1, tuple)
    valid_prefix_tail = replace(
        accepted_epoch1[-1],
        ingestion_sequence=3,
        acceptance_monotonic_ns=8_899,
    )
    mismatched_prefix_tail = replace(valid_prefix_tail, **mutation)

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded selection-prefix timeout fails its exact",
    ):
        validate_fault_causality(
            **{
                **arguments,
                "accepted_epoch1": (
                    *accepted_epoch1[:-1],
                    mismatched_prefix_tail,
                ),
                "selection_visible_responsive_timeout_nonwitnesses": True,
            }
        )


def test_v21_responsive_mismatch_cannot_hide_beside_an_exact_prefix_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _selection_visible_responsive_tail_fixture()
    accepted_epoch1 = arguments["accepted_epoch1"]
    assert isinstance(accepted_epoch1, tuple)
    valid_prefix_tail = replace(
        accepted_epoch1[-1],
        ingestion_sequence=3,
        acceptance_monotonic_ns=8_899,
    )
    mismatched_prefix_tail = replace(
        valid_prefix_tail,
        observation_id="e" * 64,
        reporter_id=1,
    )

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded selection-prefix timeout fails its exact",
    ):
        validate_fault_causality(
            **{
                **arguments,
                "accepted_epoch1": (
                    *accepted_epoch1[:-1],
                    valid_prefix_tail,
                    mismatched_prefix_tail,
                ),
                "selection_visible_responsive_timeout_nonwitnesses": True,
            }
        )


def test_v21_responsive_immature_arm_cannot_hide_a_prefix_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _selection_visible_responsive_tail_fixture()
    arm_markers = arguments["arm_markers"]
    accepted_epoch1 = arguments["accepted_epoch1"]
    assert isinstance(arm_markers, tuple)
    assert isinstance(accepted_epoch1, tuple)

    immature_arm = replace(
        arm_markers[-1],
        absolute_deadline_ns=9_899,
        deadline_duration_us=2,
    )
    prefix_tail = replace(
        accepted_epoch1[-1],
        ingestion_sequence=3,
        acceptance_monotonic_ns=8_899,
    )
    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded selection-prefix timeout fails its exact",
    ):
        validate_fault_causality(
            **{
                **arguments,
                "arm_markers": (*arm_markers[:-1], immature_arm),
                "accepted_epoch1": (*accepted_epoch1[:-1], prefix_tail),
                "selection_visible_responsive_timeout_nonwitnesses": True,
            }
        )


def test_v21_responsive_nonwitness_cannot_replace_actor_selection_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _selection_visible_responsive_tail_fixture()
    accepted_epoch1 = arguments["accepted_epoch1"]
    assert isinstance(accepted_epoch1, tuple)

    with pytest.raises(
        FactorialValidationError,
        match="performance-selection evidence",
    ):
        validate_fault_causality(
            **{
                **arguments,
                "accepted_epoch1": (accepted_epoch1[-1],),
                "selection_visible_responsive_timeout_nonwitnesses": True,
            }
        )


def test_v9_proposal_commit_identity_binds_the_replica_event_stream() -> None:
    event = _commit_event(
        1,
        2_000_000,
        900,
        epoch_number=1,
        tree_id=4,
        replica_id=1,
    )
    assert validation._proposal_commit_identity(
        event,
        replica_id=1,
        require_exact_source_binding=True,
    ) == (1, 4, "11" * 32, f"{900:064x}")

    with pytest.raises(FactorialValidationError, match="source.*replica event stream"):
        validation._proposal_commit_identity(
            replace(event, source_id="replica-7"),
            replica_id=1,
            require_exact_source_binding=True,
        )


def test_v9_cross_commit_retention_witness_requires_every_degraded_actor() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    assert validation._validate_v9_cross_commit_retention_witnesses(
        **arguments
    ) == (2, 5)

    commit_times = dict(arguments["authoritative_commit_ns"])
    missing_key = next(
        key
        for key in commit_times
        if key[3] == f"{901:064x}"
    )
    commit_times.pop(missing_key)
    with pytest.raises(FactorialValidationError, match=r"cross-commit.*\[5\]"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{**arguments, "authoritative_commit_ns": commit_times}
        )


def test_v36_sealed_s037_cross_commit_failure_reason_remains_exact() -> None:
    arguments = _v9_cross_commit_witness_fixture()

    with pytest.raises(
        FactorialValidationError,
        match=(
            r"responsive-degraded actors lack a v9 Epoch1 cross-commit "
            r"retention witness.*\[7, 8\]"
        ),
    ):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "responsive_degraded_actor_ids": (7, 8),
            }
        )


def test_v9_cross_commit_race_does_not_erase_strict_actor_witnesses() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    valid_marker = arguments["markers"][0]
    valid_arm = arguments["arm_markers"][0]
    raced_marker = replace(
        valid_marker,
        line_number=99,
        block_hash="ff" * 32,
        monotonic_ns=valid_marker.monotonic_ns + 1_000_000,
        raw_line_sha256="fe" * 32,
    )
    raced_arm = replace(
        valid_arm,
        line_number=99,
        block_hash=raced_marker.block_hash,
        start_monotonic_ns=raced_marker.monotonic_ns + 100_000,
        absolute_deadline_ns=raced_marker.monotonic_ns + 600_000,
        raw_line_sha256="fd" * 32,
    )
    proposal_key = raced_arm.proposal_key
    timeout_key = (raced_marker.actor, *proposal_key)
    timeout_index = dict(arguments["epoch1_timeout_index"])
    timeout_index[timeout_key] = (
        replace(
            next(iter(timeout_index.values()))[0],
            observation_id="fc" * 32,
            target_id=raced_marker.actor,
            epoch_number=raced_marker.epoch_number,
            tree_id=raced_marker.tree_id,
            epoch_digest=raced_marker.epoch_digest,
            block_hash=raced_marker.block_hash,
            reporter_id=raced_arm.reporter_id,
            message_type=raced_arm.expected_message_type,
            reporter_monotonic_ns=raced_arm.absolute_deadline_ns,
            acceptance_monotonic_ns=raced_arm.absolute_deadline_ns + 10,
        ),
    )
    authoritative = dict(arguments["authoritative_commit_ns"])
    authoritative[proposal_key] = raced_marker.monotonic_ns + 50_000
    proposal_commits = {
        replica_id: dict(commits)
        for replica_id, commits in arguments[
            "proposal_commit_ns_by_replica"
        ].items()
    }
    proposal_commits[raced_arm.reporter_id][proposal_key] = (
        raced_marker.monotonic_ns + 200_000,
    )

    assert validation._validate_v9_cross_commit_retention_witnesses(
        **{
            **arguments,
            "markers": (*arguments["markers"], raced_marker),
            "arm_markers": (*arguments["arm_markers"], raced_arm),
            "authoritative_commit_ns": authoritative,
            "proposal_commit_ns_by_replica": proposal_commits,
            "epoch1_timeout_index": timeout_index,
            "order_failures_are_nonwitness_candidates": True,
        }
    ) == (2, 5)

    with pytest.raises(FactorialValidationError, match="arm ordering"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "markers": (*arguments["markers"], raced_marker),
                "arm_markers": (*arguments["arm_markers"], raced_arm),
                "authoritative_commit_ns": authoritative,
                "proposal_commit_ns_by_replica": proposal_commits,
                "epoch1_timeout_index": timeout_index,
            }
        )


def test_v9_cross_commit_retention_rejects_missing_arm_marker() -> None:
    arguments = _v9_cross_commit_witness_fixture()

    with pytest.raises(FactorialValidationError, match=r"arm.*\[2\]"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "arm_markers": tuple(
                    arm
                    for arm in arguments["arm_markers"]
                    if arm.child_id != 2
                ),
            }
        )


@pytest.mark.parametrize(
    "binding",
    ("parent", "child", "proposal", "message_type"),
)
def test_v9_cross_commit_retention_binds_exact_arm_identity(binding: str) -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arms = list(arguments["arm_markers"])
    arm = arms[0]
    if binding == "parent":
        arms[0] = replace(
            arm,
            source_replica=arm.reporter_id + 1,
            reporter_id=arm.reporter_id + 1,
        )
    elif binding == "child":
        arms[0] = replace(arm, child_id=3)
    elif binding == "proposal":
        arms[0] = replace(arm, block_hash="ff" * 32)
    else:
        arms[0] = replace(arm, expected_message_type="direct_vote")

    with pytest.raises(FactorialValidationError, match="physical parent|missing"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{**arguments, "arm_markers": tuple(arms)}
        )


def test_v9_cross_commit_retention_rejects_post_commit_arm() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arms = list(arguments["arm_markers"])
    proposal_commit_times = arguments["proposal_commit_ns_by_replica"]
    timeout_index = dict(arguments["epoch1_timeout_index"])
    arm = arms[0]
    proposal_key = (
        arm.epoch_number,
        arm.tree_id,
        arm.epoch_digest,
        arm.block_hash,
    )
    post_commit_start = (
        proposal_commit_times[arm.reporter_id][proposal_key][0] + 1
    )
    post_commit_deadline = post_commit_start + arm.deadline_duration_us * 1_000
    arms[0] = replace(
        arm,
        start_monotonic_ns=post_commit_start,
        absolute_deadline_ns=post_commit_deadline,
    )
    timeout_key = (arm.child_id, *proposal_key)
    timeout_index[timeout_key] = (
        replace(
            timeout_index[timeout_key][0],
            reporter_monotonic_ns=post_commit_deadline,
            acceptance_monotonic_ns=post_commit_deadline + 1,
        ),
    )

    with pytest.raises(FactorialValidationError, match=r"cross-commit.*\[2\]"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "arm_markers": tuple(arms),
                "epoch1_timeout_index": timeout_index,
            }
        )


@pytest.mark.parametrize(
    "changed_field",
    ("absolute_deadline_ns", "deadline_duration_us"),
)
def test_v9_cross_commit_retention_rejects_changed_arm_deadline_or_duration(
    changed_field: str,
) -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arms = list(arguments["arm_markers"])
    arm = arms[0]
    if changed_field == "absolute_deadline_ns":
        arms[0] = replace(arm, absolute_deadline_ns=arm.absolute_deadline_ns + 1)
        expected_error = "absolute deadline"
    else:
        arms[0] = replace(
            arm,
            deadline_duration_us=arm.deadline_duration_us + 1,
            absolute_deadline_ns=(
                arm.start_monotonic_ns + (arm.deadline_duration_us + 1) * 1_000
            ),
        )
        expected_error = "duration"

    with pytest.raises(FactorialValidationError, match=expected_error):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{**arguments, "arm_markers": tuple(arms)}
        )


def test_v9_cross_commit_retention_rejects_cancel_and_rearm() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arms = list(arguments["arm_markers"])
    original = arms[0]
    proposal_commit_times = arguments["proposal_commit_ns_by_replica"]
    proposal_key = (
        original.epoch_number,
        original.tree_id,
        original.epoch_digest,
        original.block_hash,
    )
    rearm_start = (
        proposal_commit_times[original.reporter_id][proposal_key][0] + 1
    )
    arms.append(
        replace(
            original,
            line_number=99,
            start_monotonic_ns=rearm_start,
            absolute_deadline_ns=(
                rearm_start + original.deadline_duration_us * 1_000
            ),
            raw_line_sha256="99" * 32,
        )
    )

    with pytest.raises(FactorialValidationError, match="duplicate|rearm"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{**arguments, "arm_markers": tuple(arms)}
        )


@pytest.mark.parametrize("reporter_offset", (-1, 0))
def test_v9_cross_commit_retention_rejects_timeout_before_or_at_commit(
    reporter_offset: int,
) -> None:
    arguments = _v9_cross_commit_witness_fixture()
    proposal_commit_times = arguments["proposal_commit_ns_by_replica"]
    timeout_index = dict(arguments["epoch1_timeout_index"])
    timeout_key = next(key for key in timeout_index if key[0] == 2)
    timeout = timeout_index[timeout_key][0]
    proposal_key = timeout_key[1:]
    local_commit_ns = proposal_commit_times[timeout.reporter_id][proposal_key][0]
    timeout_index[timeout_key] = (
        replace(
            timeout,
            reporter_monotonic_ns=local_commit_ns + reporter_offset,
        ),
    )

    with pytest.raises(FactorialValidationError, match=r"cross-commit.*\[2\]"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{**arguments, "epoch1_timeout_index": timeout_index}
        )


def test_v9_cross_commit_uses_reporter_commit_not_earlier_authoritative_commit() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arm = arguments["arm_markers"][0]
    proposal_key = arm.proposal_key
    proposal_commit_times = {
        replica_id: dict(commits)
        for replica_id, commits in arguments[
            "proposal_commit_ns_by_replica"
        ].items()
    }
    proposal_commit_times[arm.reporter_id][proposal_key] = (
        arm.absolute_deadline_ns + 1,
    )

    with pytest.raises(FactorialValidationError, match="reporter-local commit"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "proposal_commit_ns_by_replica": proposal_commit_times,
            }
        )


def test_v9_cross_commit_keeps_later_authoritative_commit_as_separate_link() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arm = arguments["arm_markers"][0]
    proposal_key = arm.proposal_key
    timeout = arguments["epoch1_timeout_index"][(arm.child_id, *proposal_key)][0]
    authoritative = dict(arguments["authoritative_commit_ns"])
    authoritative[proposal_key] = timeout.acceptance_monotonic_ns + 1

    validation._validate_v9_cross_commit_retention_witnesses(
        **{**arguments, "authoritative_commit_ns": authoritative}
    )


@pytest.mark.parametrize("corruption", ("missing", "duplicate", "proposal", "reporter"))
def test_v9_cross_commit_rejects_nonexact_reporter_local_commit(
    corruption: str,
) -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arm = arguments["arm_markers"][0]
    proposal_key = arm.proposal_key
    proposal_commit_times = {
        replica_id: dict(commits)
        for replica_id, commits in arguments[
            "proposal_commit_ns_by_replica"
        ].items()
    }
    original = proposal_commit_times[arm.reporter_id].pop(proposal_key)
    if corruption == "duplicate":
        proposal_commit_times[arm.reporter_id][proposal_key] = (
            original[0],
            original[0] + 1,
        )
    elif corruption == "proposal":
        proposal_commit_times[arm.reporter_id][
            (*proposal_key[:3], "ff" * 32)
        ] = original
    elif corruption == "reporter":
        proposal_commit_times.setdefault(arm.reporter_id + 1, {})[
            proposal_key
        ] = original

    with pytest.raises(
        FactorialValidationError,
        match="missing exact reporter-local|duplicated/ambiguous exact reporter-local",
    ):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "proposal_commit_ns_by_replica": proposal_commit_times,
            }
        )


def test_v9_cross_commit_retention_requires_an_internal_aggregate_witness() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    markers = tuple(
        replace(marker, action="omit_direct_vote")
        for marker in arguments["markers"]
    )
    trees: dict[int, Tree] = {}
    for marker in markers:
        tree = arguments["epoch1_trees"][marker.tree_id]
        trees[marker.tree_id] = replace(
            tree,
            members=(
                *(member for member in tree.members if member != marker.actor),
                marker.actor,
            ),
        )
    timeout_index = dict(arguments["epoch1_timeout_index"])
    proposal_commit_times: dict[
        int,
        dict[tuple[int, int, str, str], tuple[int, ...]],
    ] = {}
    arm_markers = {
        arm.child_id: arm for arm in arguments["arm_markers"]
    }
    for marker in markers:
        proposal_key = (
            marker.epoch_number,
            marker.tree_id,
            marker.epoch_digest,
            marker.block_hash,
        )
        timeout_key = (marker.actor, *proposal_key)
        tree = trees[marker.tree_id]
        position = tree.members.index(marker.actor)
        parent = tree.members[(position - 1) // tree.fanout]
        timeout_index[timeout_key] = (
            replace(
                timeout_index[timeout_key][0],
                reporter_id=parent,
                message_type="direct_vote",
            ),
        )
        arm_markers[marker.actor] = replace(
            arm_markers[marker.actor],
            source_replica=parent,
            reporter_id=parent,
            expected_message_type="direct_vote",
        )
        proposal_commit_times.setdefault(parent, {})[proposal_key] = (
            marker.monotonic_ns + 100_000,
        )

    with pytest.raises(
        FactorialValidationError,
        match=r"internal-role omit_aggregate",
    ):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "markers": markers,
                "epoch1_trees": trees,
                "epoch1_timeout_index": timeout_index,
                "arm_markers": tuple(arm_markers.values()),
                "proposal_commit_ns_by_replica": proposal_commit_times,
            }
        )


def test_primary_v9_cross_commit_requires_each_actor_to_be_internal() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    leaf_only_actor = 5
    markers = tuple(
        replace(marker, action="omit_direct_vote")
        if marker.actor == leaf_only_actor
        else marker
        for marker in arguments["markers"]
    )
    marker = next(marker for marker in markers if marker.actor == leaf_only_actor)
    proposal_key = (
        marker.epoch_number,
        marker.tree_id,
        marker.epoch_digest,
        marker.block_hash,
    )
    trees = dict(arguments["epoch1_trees"])
    original_tree = trees[marker.tree_id]
    trees[marker.tree_id] = replace(
        original_tree,
        members=(
            *(member for member in original_tree.members if member != leaf_only_actor),
            leaf_only_actor,
        ),
    )
    position = trees[marker.tree_id].members.index(leaf_only_actor)
    parent = trees[marker.tree_id].members[
        (position - 1) // trees[marker.tree_id].fanout
    ]
    timeout_key = (leaf_only_actor, *proposal_key)
    timeout_index = dict(arguments["epoch1_timeout_index"])
    timeout_index[timeout_key] = (
        replace(
            timeout_index[timeout_key][0],
            reporter_id=parent,
            message_type="direct_vote",
        ),
    )
    arms = tuple(
        replace(
            arm,
            source_replica=parent,
            reporter_id=parent,
            expected_message_type="direct_vote",
        )
        if arm.child_id == leaf_only_actor
        else arm
        for arm in arguments["arm_markers"]
    )
    proposal_commit_times = {
        replica_id: dict(commits)
        for replica_id, commits in arguments[
            "proposal_commit_ns_by_replica"
        ].items()
    }
    original_arm = next(
        arm for arm in arguments["arm_markers"] if arm.child_id == leaf_only_actor
    )
    local_commit = proposal_commit_times[original_arm.reporter_id].pop(
        proposal_key
    )
    proposal_commit_times.setdefault(parent, {})[proposal_key] = local_commit

    with pytest.raises(FactorialValidationError, match=r"own.*\[5\]"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "markers": markers,
                "epoch1_trees": trees,
                "epoch1_timeout_index": timeout_index,
                "arm_markers": arms,
                "proposal_commit_ns_by_replica": proposal_commit_times,
                "require_each_degraded_actor_internal_witness": True,
            }
        )


def test_tiered_completeness_rejects_a_whole_missing_observed_context() -> None:
    digest = "11" * 32
    tree = Tree(
        tree_id=0,
        fanout=5,
        pipeline_stretch=2,
        members=tuple(range(13)),
        wait_exempt=(),
    )
    actors = (2, 9, 10, 12)
    first = (0, 0, digest, "01" * 32)
    missing = (0, 0, digest, "02" * 32)

    def marker(actor: int, cohort: str, action: str) -> FaultMarker:
        return replace(
            _tiered_marker(
                actor=actor,
                cohort=cohort,
                ordinal=1 if cohort == "responsive_degraded" else 0,
                block_ordinal=actor,
                action=action,
            ),
            epoch_number=first[0],
            tree_id=first[1],
            epoch_digest=first[2],
            block_hash=first[3],
        )

    markers = (
        marker(2, "responsive_degraded", "forward"),
        marker(9, "hard", "omit_direct_vote"),
        marker(10, "hard", "omit_direct_vote"),
        marker(12, "hard", "omit_direct_vote"),
    )
    observations = {
        first: {actor: (300,) for actor in actors},
        missing: {actor: (400,) for actor in actors},
    }
    with pytest.raises(FactorialValidationError, match="exact tiered actor marker set"):
        validation._validate_tiered_observed_marker_completeness(
            markers,
            fault_actor_ids=actors,
            proposal_observations=observations,
            phase_windows={"fault_evidence": (100, 700, 6)},
            phase_configurations=(
                ("fault_evidence", 0, digest, {0: tree}),
            ),
        )


def test_tiered_completeness_uses_root_activity_not_delayed_commits() -> None:
    digest = "11" * 32
    tree = Tree(
        tree_id=0,
        fanout=2,
        pipeline_stretch=2,
        members=(0, 1, 2),
        wait_exempt=(),
    )
    actor = 2
    represented = (0, 0, digest, "01" * 32)
    prewindow_delayed_commit = (0, 0, digest, "02" * 32)
    marker = replace(
        _tiered_marker(
            actor=actor,
            cohort="responsive_degraded",
            ordinal=1,
            block_ordinal=1,
            action="forward",
        ),
        epoch_number=represented[0],
        tree_id=represented[1],
        epoch_digest=represented[2],
        block_hash=represented[3],
    )
    delayed_commit_observations = {
        represented: {actor: (300,)},
        prewindow_delayed_commit: {actor: (400,)},
    }
    root_aggregation_observations = {
        represented: {0: (300,)},
        prewindow_delayed_commit: {0: (150,)},
    }
    arguments = {
        "fault_actor_ids": (actor,),
        "proposal_observations": delayed_commit_observations,
        "root_aggregation_observations": root_aggregation_observations,
        "phase_windows": {"fault_evidence": (100, 700, 6)},
        "phase_configurations": (
            ("fault_evidence", 0, digest, {0: tree}),
        ),
    }

    validation._validate_tiered_observed_marker_completeness(
        (marker,),
        **arguments,
    )

    with pytest.raises(
        FactorialValidationError,
        match="exact tiered actor marker set",
    ):
        validation._validate_tiered_observed_marker_completeness(
            (),
            **arguments,
        )


def _tiered_scores() -> tuple[ReplicaScore, ...]:
    fast = (0, 1, 3, 4, 5, 6, 7, 8, 11)
    return (
        *(
            ReplicaScore(
                replica_id=replica,
                classification="responsive",
                eligible=True,
                attempt_count=41,
                response_rate_ppm=1_000_000,
                timeout_rate_ppm=0,
                latency_percentile_us=10,
            )
            for replica in fast
        ),
        ReplicaScore(2, "responsive", True, 41, 975_610, 24_390, 20),
        *(
            ReplicaScore(
                replica_id=replica,
                classification="nonresponsive",
                eligible=False,
                attempt_count=41,
                response_rate_ppm=0,
                timeout_rate_ppm=1_000_000,
                latency_percentile_us=None,
            )
            for replica in (9, 10, 12)
        ),
    )


def test_tiered_ranking_requires_real_degradation_and_exact_top_q() -> None:
    scores = _tiered_scores()
    policy = {
        "minimum_attempts": 41,
        "minimum_response_rate_ppm": 950_000,
        "maximum_timeout_rate_ppm": 50_000,
    }
    fast = tuple(score.replica_id for score in scores[:9])
    arguments = {
        "hard_actor_ids": (9, 10, 12),
        "responsive_degraded_actor_ids": (2,),
        "fast_replica_ids": fast,
        "tree_count": 9,
        "expected_roots": fast,
        "policy": policy,
        "responsive_omission_period": 41,
    }
    assert validation._validate_tiered_performance_ranking(scores, **arguments) == 1

    with pytest.raises(FactorialValidationError, match="real nonzero degradation"):
        validation._validate_tiered_performance_ranking(
            (*scores[:9], replace(scores[9], classification="nonresponsive", eligible=False), *scores[10:]),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="real nonzero degradation"):
        validation._validate_tiered_performance_ranking(
            (*scores[:9], replace(scores[9], response_rate_ppm=1_000_000, timeout_rate_ppm=0), *scores[10:]),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="top-Q"):
        validation._validate_tiered_performance_ranking(
            scores,
            **{**arguments, "expected_roots": (*fast[:-1], 2)},
        )


def _tiered_expected_slot() -> validation._ExpectedSlot:
    return validation._ExpectedSlot(
        slot_id="tiered-test",
        block_id="n13-f5-test",
        arm_code="P",
        ordinal=1,
        slot_nonce=0,
        block_index=1,
        blocks_in_cell=1,
        block_execution_ordinal=1,
        arm_execution_position=1,
        execution_ordinal=1,
        scientific_seed=41_719,
        replica_count=13,
        f=4,
        q=9,
        tree_count=9,
        initial_fanout=5,
        initial_depth=2,
        candidate_depths=((2, 3), (3, 2), (5, 2)),
        worst_candidate_depth=3,
        candidate_fanouts=(2, 3, 5),
        pipeline_stretch=2,
        placement_adaptation=True,
        shape_adaptation=False,
        actor_ids=(9, 10, 12),
        responsive_degraded_actor_ids=(2,),
        fast_replica_ids=(0, 1, 3, 4, 5, 6, 7, 8, 11),
        peer_base=1,
        client_base=2,
        manager_port=3,
    )


def _tiered_bundle(
    *,
    expected: validation._ExpectedSlot,
    epoch_number: int,
    roots: tuple[int, ...],
    epoch1: bool,
) -> validation.DecodedBundle:
    trees: list[Tree] = []
    membership = tuple(range(expected.replica_count))
    hard = set(expected.actor_ids)
    for tree_id, root in enumerate(roots):
        available_influential = [
            member
            for member in (
                membership if epoch1 else expected.fast_replica_ids
            )
            if member != root and member not in hard
        ]
        if epoch1 and tree_id == 0 and 2 != root:
            available_influential.remove(2)
            influential = [2, available_influential[0]]
        else:
            influential = available_influential[:2]
        prefix = (root, *influential)
        members = (*prefix, *(member for member in membership if member not in prefix))
        trees.append(
            Tree(
                tree_id=tree_id,
                fanout=5,
                pipeline_stretch=2,
                members=members,
                wait_exempt=expected.actor_ids,
            )
        )
    digest = f"{epoch_number + 1:064x}"
    return validation.DecodedBundle(
        command=validation.DecodedCommand(
            issuer_id=1,
            successor_epoch_number=epoch_number,
            predecessor_epoch_digest="11" * 32,
            successor_epoch_digest=digest,
            activation_delay_blocks=5,
            payload_digest="22" * 32,
            signature=b"test",
        ),
        epoch_number=epoch_number,
        epoch_digest=digest,
        previous_epoch_digest="11" * 32,
        membership_digest=validation._membership_digest(membership),
        generation_seed=validation._SNAPSHOT_SEED,
        policy_version=validation._PLACEMENT_POLICY_VERSION,
        evidence_snapshot_id="33" * 32,
        evidence_cutoff=1,
        trees=tuple(trees),
    )


def test_tiered_successor_structure_proves_e1_exposure_and_e2_hierarchy() -> None:
    expected = _tiered_expected_slot()
    epoch1_roots = tuple(range(expected.q))
    epoch1 = _tiered_bundle(
        expected=expected,
        epoch_number=1,
        roots=epoch1_roots,
        epoch1=True,
    )
    assert validation._validate_successor_trees(
        epoch1,
        expected=expected,
        actor_ids=expected.actor_ids,
        expected_roots=epoch1_roots,
        expected_fanout=5,
        cycle=0,
        intent="fault_containment",
    ) == (1, 1, 0, 0, 0)

    epoch2_roots = expected.fast_replica_ids
    epoch2 = _tiered_bundle(
        expected=expected,
        epoch_number=2,
        roots=epoch2_roots,
        epoch1=False,
    )
    proof = validation._validate_successor_trees(
        epoch2,
        expected=expected,
        actor_ids=expected.actor_ids,
        expected_roots=epoch2_roots,
        expected_fanout=5,
        cycle=1,
        intent="performance_optimization",
    )
    assert proof[:3] == (0, 0, expected.f * expected.q)
    assert proof[3] == proof[4] == 3 * expected.q

    first = epoch2.trees[0]
    degraded_position = first.members.index(2)
    tampered_members = list(first.members)
    tampered_members[1], tampered_members[degraded_position] = (
        tampered_members[degraded_position],
        tampered_members[1],
    )
    degraded_internal = replace(
        epoch2,
        trees=(replace(first, members=tuple(tampered_members)), *epoch2.trees[1:]),
    )
    with pytest.raises(FactorialValidationError, match="non-fast"):
        validation._validate_successor_trees(
            degraded_internal,
            expected=expected,
            actor_ids=expected.actor_ids,
            expected_roots=epoch2_roots,
            expected_fanout=5,
            cycle=1,
            intent="performance_optimization",
        )

    degraded_wait_exempt = replace(
        epoch2,
        trees=(
            replace(first, wait_exempt=(*expected.actor_ids, 2)),
            *epoch2.trees[1:],
        ),
    )
    with pytest.raises(FactorialValidationError, match="wait-exempt"):
        validation._validate_successor_trees(
            degraded_wait_exempt,
            expected=expected,
            actor_ids=expected.actor_ids,
            expected_roots=epoch2_roots,
            expected_fanout=5,
            cycle=1,
            intent="performance_optimization",
        )


def test_v34_scope_retains_epoch2_hard_actor_leaf_and_wait_exempt_checks() -> None:
    expected = _tiered_expected_slot()
    roots = expected.fast_replica_ids
    epoch2 = _tiered_bundle(
        expected=expected,
        epoch_number=2,
        roots=roots,
        epoch1=False,
    )
    hard_actor = expected.actor_ids[0]
    first = epoch2.trees[0]
    members = list(first.members)
    hard_position = members.index(hard_actor)
    members[1], members[hard_position] = members[hard_position], members[1]

    with pytest.raises(FactorialValidationError, match="physical successor leaf"):
        validation._validate_successor_trees(
            replace(
                epoch2,
                trees=(
                    replace(first, members=tuple(members)),
                    *epoch2.trees[1:],
                ),
            ),
            expected=expected,
            actor_ids=expected.actor_ids,
            expected_roots=roots,
            expected_fanout=5,
            cycle=1,
            intent="performance_optimization",
        )

    with pytest.raises(FactorialValidationError, match="wait-exempt set"):
        validation._validate_successor_trees(
            replace(
                epoch2,
                trees=(
                    replace(first, wait_exempt=()),
                    *epoch2.trees[1:],
                ),
            ),
            expected=expected,
            actor_ids=expected.actor_ids,
            expected_roots=roots,
            expected_fanout=5,
            cycle=1,
            intent="performance_optimization",
        )


def _v25_inherited_placement_expected_slot() -> validation._ExpectedSlot:
    return replace(
        _tiered_expected_slot(),
        slot_id="slot-037-n31-f2-b04-00",
        block_id="n31-f2-b04",
        arm_code="00",
        ordinal=37,
        slot_nonce=36,
        block_index=4,
        blocks_in_cell=5,
        block_execution_ordinal=2,
        arm_execution_position=1,
        execution_ordinal=5,
        scientific_seed=41_728,
        replica_count=31,
        f=10,
        q=21,
        tree_count=21,
        initial_fanout=2,
        initial_depth=4,
        candidate_depths=((2, 4), (3, 3), (5, 2)),
        worst_candidate_depth=4,
        candidate_fanouts=(2, 3, 5),
        pipeline_stretch=2,
        placement_adaptation=False,
        shape_adaptation=False,
        actor_ids=(26, 27, 28),
        responsive_degraded_actor_ids=(1, 7, 8, 12, 16, 19, 20),
        fast_replica_ids=(
            0,
            2,
            3,
            4,
            5,
            6,
            9,
            10,
            11,
            13,
            14,
            15,
            17,
            18,
            21,
            22,
            23,
            24,
            25,
            29,
            30,
        ),
        peer_base=28_700,
        client_base=29_700,
        manager_port=30_700,
    )


def _v25_inherited_placement_trees(
    expected: validation._ExpectedSlot,
) -> tuple[Tree, ...]:
    membership = tuple(range(expected.replica_count))
    return tuple(
        Tree(
            tree_id=tree_id,
            fanout=expected.initial_fanout,
            pipeline_stretch=expected.pipeline_stretch,
            members=(root, *(member for member in membership if member != root)),
            wait_exempt=expected.actor_ids,
        )
        for tree_id, root in enumerate(range(expected.q))
    )


def _v25_inherited_placement_scores(
    expected: validation._ExpectedSlot,
) -> tuple[ReplicaScore, ...]:
    return tuple(
        ReplicaScore(
            replica_id=replica_id,
            classification="responsive",
            eligible=True,
            attempt_count=1,
            response_rate_ppm=1_000_000,
            timeout_rate_ppm=0,
            latency_percentile_us=10,
        )
        for replica_id in range(expected.replica_count)
    )


def _v26_duplicate_delivery_live_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], Path]:
    expected = _v25_inherited_placement_expected_slot()
    predecessor_trees = _v25_inherited_placement_trees(expected)
    successor_trees = _v25_inherited_placement_trees(expected)
    predecessor_digest = "11" * 32
    successor_digest = "22" * 32
    command_payload_digest = "33" * 32
    command_block_hash = "44" * 32
    proposal_block_hash = "88" * 32
    reporter = 6
    child = 1
    tree_id = 6
    view_generation = ((1 << 32) | tree_id) + 1
    activation_delay = 5
    cycle0_predecessor_digest = "00" * 32
    cycle0_command_payload_digest = "aa" * 32
    cycle0_command_block_hash = "bb" * 32
    cycle0_command_height = 50
    command_height = 100
    cycle0_command_payload = {
        "command_block_height": cycle0_command_height,
        "command_block_hash": cycle0_command_block_hash,
        "payload_digest": cycle0_command_payload_digest,
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": cycle0_predecessor_digest,
        "successor_epoch_number": 1,
        "successor_epoch_digest": predecessor_digest,
        "activation_delay_blocks": activation_delay,
        "activation_height": cycle0_command_height + activation_delay,
    }
    command_payload = {
        "command_block_height": command_height,
        "command_block_hash": command_block_hash,
        "payload_digest": command_payload_digest,
        "predecessor_epoch_number": 1,
        "predecessor_epoch_digest": predecessor_digest,
        "successor_epoch_number": 2,
        "successor_epoch_digest": successor_digest,
        "activation_delay_blocks": activation_delay,
        "activation_height": command_height + activation_delay,
    }
    replica_events = {
        replica_id: (
            _native_event(
                source_id=f"replica-{replica_id}",
                sequence=1,
                monotonic_ns=100 + replica_id,
                event_type="epoch.command_committed",
                payload=dict(cycle0_command_payload),
            ),
            _native_event(
                source_id=f"replica-{replica_id}",
                sequence=2,
                monotonic_ns=500 + replica_id,
                event_type="epoch.activated",
                payload={
                    "epoch_number": 1,
                    "tree_id": 0,
                    "epoch_digest": predecessor_digest,
                    "activation_height": cycle0_command_height
                    + activation_delay,
                },
            ),
            _native_event(
                source_id=f"replica-{replica_id}",
                sequence=3,
                monotonic_ns=1_000 + replica_id,
                event_type="epoch.command_committed",
                payload=dict(command_payload),
            ),
            _native_event(
                source_id=f"replica-{replica_id}",
                sequence=4,
                monotonic_ns=2_000 + replica_id,
                event_type="epoch.activated",
                payload={
                    "epoch_number": 2,
                    "tree_id": 0,
                    "epoch_digest": successor_digest,
                    "activation_height": command_height + activation_delay,
                },
            ),
        )
        for replica_id in range(expected.replica_count)
    }
    reporter_events = replica_events[reporter]
    replica_events[reporter] = (
        reporter_events[0],
        reporter_events[1],
        _native_event(
            source_id=f"replica-{reporter}",
            sequence=3,
            monotonic_ns=975,
            event_type="block.committed",
            payload={
                "block_height": 75,
                "block_hash": proposal_block_hash,
                "parent_hash": "77" * 32,
                "transaction_count": 1000,
                "designated_observer": False,
                "decision_proof": {
                    "epoch_number": 1,
                    "tree_id": tree_id,
                    "epoch_digest": predecessor_digest,
                    "block_hash": proposal_block_hash,
                },
                "view_generation": view_generation,
                "commit_batch_index": 0,
            },
        ),
        replace(
            reporter_events[2],
            line_number=4,
            source_sequence=4,
        ),
        replace(
            reporter_events[3],
            line_number=5,
            source_sequence=5,
        ),
    )
    convergence_payload = {
        "replica_id": None,
        "delivery_attempt": None,
        "disposition": None,
        "identity": {
            "predecessor_epoch_number": 1,
            "predecessor_epoch_digest": predecessor_digest,
            "successor_epoch_number": 2,
            "successor_epoch_digest": successor_digest,
            "command_payload_digest": command_payload_digest,
            "command_block_height": command_height,
            "command_block_hash": command_block_hash,
            "activation_delay_blocks": activation_delay,
            "activation_height": command_height + activation_delay,
        },
        "accepted_commit_count": expected.q,
        "accepted_activation_count": expected.q,
        "required_activation_count": expected.q,
        "canonical_payload_digest": None,
        "failure_reason": None,
    }
    cycle0_convergence_payload = {
        **convergence_payload,
        "identity": {
            **cycle0_command_payload,
            "command_payload_digest": cycle0_command_payload_digest,
        },
    }
    cycle0_convergence_payload["identity"].pop("payload_digest")
    terminal_payload = {
        "cycle_ordinal": 1,
        "outcome": "advanced",
        "reason": "successor_converged",
        "predecessor_epoch_number": 1,
        "predecessor_epoch_digest": predecessor_digest,
        "successor_epoch_number": 2,
        "successor_epoch_digest": successor_digest,
        "command_payload_digest": command_payload_digest,
    }
    manager_events = (
        validation._NativeEvent(
            relative_path="raw/adaptive-manager.jsonl",
            line_number=1,
            source_kind="adaptation_manager",
            source_id="adaptive-manager",
            source_instance="slot-adaptive-manager",
            source_sequence=1,
            monotonic_ns=800,
            event_type="adaptive_v2_converged",
            payload=cycle0_convergence_payload,
            line_sha256="53" * 32,
        ),
        validation._NativeEvent(
            relative_path="raw/adaptive-manager.jsonl",
            line_number=2,
            source_kind="adaptation_manager",
            source_id="adaptive-manager",
            source_instance="slot-adaptive-manager",
            source_sequence=2,
            monotonic_ns=900,
            event_type="adaptive_v2_shape_decision",
            payload={"cycle_ordinal": 1},
            line_sha256="54" * 32,
        ),
        validation._NativeEvent(
            relative_path="raw/adaptive-manager.jsonl",
            line_number=3,
            source_kind="adaptation_manager",
            source_id="adaptive-manager",
            source_instance="slot-adaptive-manager",
            source_sequence=3,
            monotonic_ns=3_000,
            event_type="adaptive_v2_converged",
            payload=convergence_payload,
            line_sha256="55" * 32,
        ),
        validation._NativeEvent(
            relative_path="raw/adaptive-manager.jsonl",
            line_number=4,
            source_kind="adaptation_manager",
            source_id="adaptive-manager",
            source_instance="slot-adaptive-manager",
            source_sequence=4,
            monotonic_ns=3_100,
            event_type="adaptive_v2_session_terminal",
            payload=terminal_payload,
            line_sha256="66" * 32,
        ),
    )
    paths_by_replica: dict[int, tuple[str, ...]] = {}
    for replica_id in range(expected.replica_count):
        relative = f"raw/process/replica-{replica_id}.stderr.log"
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        paths_by_replica[replica_id] = (relative,)
    log_path = tmp_path / paths_by_replica[reporter][0]
    log_path.write_text(
        "\n".join(
            (
                "KAURI_EVIDENCE response_attempt_armed "
                f"reporter={reporter} child={child} epoch=1 tree={tree_id} "
                f"epoch_digest={predecessor_digest} block={proposal_block_hash} "
                "expected_message_type=aggregate_relay start_monotonic_ns=10 "
                "deadline_duration_us=20 absolute_deadline_ns=20010",
                "KAURI_RELAY_INGRESS stage=begin "
                f"recipient={reporter} source_replica={child}",
                "KAURI_RELAY_INGRESS stage=result "
                f"recipient={reporter} source_replica={child} root={reporter} "
                "error=0 wire_error=0 permission=3 envelope=1 "
                f"epoch=1 tree={tree_id} block={proposal_block_hash} "
                f"generation={view_generation}",
                "KAURI_RELAY_INGRESS stage=dispatch_complete "
                f"recipient={reporter} source_replica={child} root={reporter} "
                "dispatched=1",
                "KAURI_RELAY_INGRESS stage=begin "
                f"recipient={reporter} source_replica={child}",
                "KAURI_RESPONSE_EVIDENCE disposition=idempotent_duplicate "
                f"reporter={reporter} child={child} epoch=1 tree={tree_id} "
                f"digest={predecessor_digest} block={proposal_block_hash} "
                "message_type=aggregate_relay attempt_generation=2942 "
                "response_monotonic_ns=900",
                "KAURI_RELAY_INGRESS stage=result "
                f"recipient={reporter} source_replica={child} root={reporter} "
                "error=0 wire_error=0 permission=3 envelope=1 "
                f"epoch=1 tree={tree_id} block={proposal_block_hash} "
                f"generation={view_generation}",
                "KAURI_RELAY_INGRESS stage=dispatch_complete "
                f"recipient={reporter} source_replica={child} root={reporter} "
                "dispatched=1",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return (
        {
            "manifest_id": validation.V26_MANIFEST_ID,
            "coverage_smoke": True,
            "expected": expected,
            "slot_root": tmp_path,
            "paths_by_replica": paths_by_replica,
            "manager_events": manager_events,
            "replica_events": replica_events,
            "predecessor_epoch_digest": predecessor_digest,
            "predecessor_trees": predecessor_trees,
            "successor_epoch_digest": successor_digest,
            "successor_trees": successor_trees,
            "command_payload_digest": command_payload_digest,
            "activation_delay_blocks": activation_delay,
        },
        log_path,
    )


def _cycle_convergence_arguments(
    arguments: dict[str, object],
) -> dict[str, object]:
    return {
        name: arguments[name]
        for name in (
            "expected",
            "manager_events",
            "replica_events",
            "predecessor_epoch_digest",
            "predecessor_trees",
            "successor_epoch_digest",
            "successor_trees",
            "command_payload_digest",
            "activation_delay_blocks",
        )
    }


def test_v33_convergence_completion_accepts_exact_all_n31_before_hard_deadline(
    tmp_path: Path,
) -> None:
    arguments, _ = _v26_duplicate_delivery_live_fixture(tmp_path)

    validation._v26_cycle1_convergence_command(
        **_cycle_convergence_arguments(arguments),
        hard_deadline_ns=3_200,
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("missing-command", "cycle-1 commit/activation convergence"),
        ("missing-activation", "cycle-1 commit/activation convergence"),
        (
            "last-command-at-deadline",
            "all raw command/activation witnesses.*hard deadline",
        ),
        (
            "last-activation-at-deadline",
            "all raw command/activation witnesses.*hard deadline",
        ),
        (
            "q21-at-deadline",
            "raw activation quorum.*hard deadline",
        ),
        (
            "convergence-at-deadline",
            "adaptive_v2_converged.*hard deadline",
        ),
        (
            "terminal-at-deadline",
            "cycle-1 terminal.*hard deadline",
        ),
    ),
)
def test_v33_convergence_completion_is_strict_and_fail_closed_before_hard_deadline(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    arguments, _ = _v26_duplicate_delivery_live_fixture(tmp_path)
    hard_deadline_ns = 3_200
    replica_events = {
        replica_id: list(events)
        for replica_id, events in arguments["replica_events"].items()
    }
    manager_events = list(arguments["manager_events"])

    if mutation in {"missing-command", "missing-activation"}:
        event_type = (
            "epoch.command_committed"
            if mutation == "missing-command"
            else "epoch.activated"
        )
        replica_events[30] = [
            event
            for event in replica_events[30]
            if not (
                event.event_type == event_type
                and event.payload.get(
                    "successor_epoch_number"
                    if event_type == "epoch.command_committed"
                    else "epoch_number"
                )
                == 2
            )
        ]
    elif mutation == "last-command-at-deadline":
        replica_events[30] = [
            replace(event, monotonic_ns=hard_deadline_ns)
            if event.event_type == "epoch.command_committed"
            and event.payload.get("successor_epoch_number") == 2
            else replace(event, monotonic_ns=hard_deadline_ns + 1)
            if event.event_type == "epoch.activated"
            and event.payload.get("epoch_number") == 2
            else event
            for event in replica_events[30]
        ]
    elif mutation == "last-activation-at-deadline":
        replica_events[30] = [
            replace(event, monotonic_ns=hard_deadline_ns)
            if event.event_type == "epoch.activated"
            and event.payload.get("epoch_number") == 2
            else event
            for event in replica_events[30]
        ]
    elif mutation == "q21-at-deadline":
        for replica_id in range(20, 31):
            replica_events[replica_id] = [
                replace(
                    event,
                    monotonic_ns=hard_deadline_ns + replica_id - 20,
                )
                if event.event_type == "epoch.activated"
                and event.payload.get("epoch_number") == 2
                else event
                for event in replica_events[replica_id]
            ]
    elif mutation == "convergence-at-deadline":
        manager_events = [
            replace(event, monotonic_ns=hard_deadline_ns)
            if event.event_type == "adaptive_v2_converged"
            and event.payload["identity"]["successor_epoch_number"] == 2
            else event
            for event in manager_events
        ]
    else:
        manager_events = [
            replace(event, monotonic_ns=hard_deadline_ns)
            if event.event_type == "adaptive_v2_session_terminal"
            and event.payload.get("cycle_ordinal") == 1
            else event
            for event in manager_events
        ]

    with pytest.raises(FactorialValidationError, match=reason):
        validation._v26_cycle1_convergence_command(
            **{
                **_cycle_convergence_arguments(arguments),
                "manager_events": tuple(manager_events),
                "replica_events": {
                    replica_id: tuple(events)
                    for replica_id, events in replica_events.items()
                },
            },
            hard_deadline_ns=hard_deadline_ns,
        )


def test_v26_slot037_requires_live_idempotent_duplicate_delivery_proof(
    tmp_path: Path,
) -> None:
    arguments, _ = _v26_duplicate_delivery_live_fixture(tmp_path)
    assert validation._validate_v26_verified_response_duplicate_live_exercise(
        **arguments
    )
    assert not validation._validate_v26_verified_response_duplicate_live_exercise(
        **{**arguments, "manifest_id": validation.V27_MANIFEST_ID}
    )
    assert not validation._validate_v27_verified_response_duplicate_live_exercise(
        **arguments
    )


@pytest.mark.parametrize(
    "mutation,reason",
    (
        ("missing-marker", "idempotent duplicate guard marker"),
        ("single-ingress", "repeated authenticated aggregate ingress"),
        ("wrong-child", "repeated authenticated aggregate ingress|active topology"),
        ("direct-vote", "aggregate relay"),
        ("early-marker", "active topology/response-attempt arm"),
        ("late-marker", "active topology/response-attempt arm"),
        ("deadline-poison", "convergence poison"),
        ("missing-activation", "cycle-1 commit/activation convergence"),
        ("missing-cycle0-command", "cycle-0 commit/activation convergence"),
        ("noncanonical-cycle0-command", "cycle-0 commit/activation convergence"),
        ("cycle0-command-after-activation", "cycle-0.*activation drifted"),
        ("cycle0-premature-convergence", "cycle-0.*raw activation quorum"),
        ("cycle0-activation-identity", "cycle-0.*activation drifted"),
        ("selection-after-command", "cycle-1 selection chronology"),
        ("command-after-activation", "convergence activation drifted"),
        ("premature-convergence", "raw activation quorum"),
        ("convergence-after-terminal", "convergence terminal drifted"),
        ("missing-converged", "cycle-1 adaptive_v2_converged"),
        ("missing-cycle0-converged", "cycle-0 adaptive_v2_converged"),
        ("duplicate-cycle0-converged", "cycle-0 adaptive_v2_converged"),
        ("drifted-cycle0-converged", "cycle-0 adaptive_v2_converged identity"),
        (
            "drifted-cycle0-predecessor-digest",
            "cycle-0 adaptive_v2_converged identity",
        ),
        (
            "drifted-cycle0-payload-digest",
            "cycle-0 adaptive_v2_converged identity",
        ),
        (
            "drifted-cycle0-block-hash",
            "cycle-0 adaptive_v2_converged identity",
        ),
        (
            "drifted-cycle0-block-height",
            "cycle-0 adaptive_v2_converged identity",
        ),
        ("duplicate-cycle1-converged", "cycle-1 adaptive_v2_converged"),
        ("unknown-converged-transition", "unknown transition"),
        ("malformed-converged-transition", "adaptive_v2_converged identity"),
        ("drifted-converged", "cycle-1 adaptive_v2_converged"),
        ("short-converged", "cycle-1 adaptive_v2_converged"),
        ("required-count-drift", "cycle-1 adaptive_v2_converged"),
        ("failed-terminal", "cycle-1 commit/activation convergence"),
    ),
)
def test_v26_slot037_duplicate_delivery_proof_is_fail_closed(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    arguments, log_path = _v26_duplicate_delivery_live_fixture(tmp_path)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    manager_events = list(arguments["manager_events"])
    selection_index = next(
        index
        for index, event in enumerate(manager_events)
        if event.event_type == "adaptive_v2_shape_decision"
    )
    cycle0_converged_index = next(
        index
        for index, event in enumerate(manager_events)
        if event.event_type == "adaptive_v2_converged"
        and event.payload["identity"]["predecessor_epoch_number"] == 0
    )
    cycle1_converged_index = next(
        index
        for index, event in enumerate(manager_events)
        if event.event_type == "adaptive_v2_converged"
        and event.payload["identity"]["predecessor_epoch_number"] == 1
    )
    terminal_index = next(
        index
        for index, event in enumerate(manager_events)
        if event.event_type == "adaptive_v2_session_terminal"
    )
    if mutation == "missing-marker":
        lines = [line for line in lines if "KAURI_RESPONSE_EVIDENCE" not in line]
    elif mutation == "single-ingress":
        lines = lines[4:]
    elif mutation == "wrong-child":
        lines[5] = lines[5].replace("child=1", "child=2")
    elif mutation == "direct-vote":
        lines[5] = lines[5].replace(
            "message_type=aggregate_relay", "message_type=direct_vote"
        )
    elif mutation == "early-marker":
        lines[5] = lines[5].replace(
            "response_monotonic_ns=900", "response_monotonic_ns=9"
        )
    elif mutation == "late-marker":
        lines[5] = lines[5].replace(
            "response_monotonic_ns=900", "response_monotonic_ns=1006"
        )
    elif mutation == "deadline-poison":
        lines.append(
            "[EPOCH] Adaptive-v2 convergence evidence unhealthy: "
            "response_deadline_evidence_failed"
        )
    elif mutation == "missing-activation":
        replica_events = dict(arguments["replica_events"])
        replica_events[30] = tuple(
            event
            for event in replica_events[30]
            if not (
                event.event_type == "epoch.activated"
                and event.payload.get("epoch_number") == 2
            )
        )
        arguments["replica_events"] = replica_events
    elif mutation == "missing-cycle0-command":
        replica_events = dict(arguments["replica_events"])
        replica_events[30] = tuple(
            event
            for event in replica_events[30]
            if not (
                event.event_type == "epoch.command_committed"
                and event.payload.get("successor_epoch_number") == 1
            )
        )
        arguments["replica_events"] = replica_events
    elif mutation == "noncanonical-cycle0-command":
        replica_events = dict(arguments["replica_events"])
        replica_events[30] = tuple(
            replace(
                event,
                payload={**event.payload, "payload_digest": "cc" * 32},
            )
            if event.event_type == "epoch.command_committed"
            and event.payload.get("successor_epoch_number") == 1
            else event
            for event in replica_events[30]
        )
        arguments["replica_events"] = replica_events
    elif mutation == "cycle0-command-after-activation":
        replica_events = dict(arguments["replica_events"])
        cycle0_activation_ns = next(
            event.monotonic_ns
            for event in replica_events[0]
            if event.event_type == "epoch.activated"
            and event.payload.get("epoch_number") == 1
        )
        replica_events[0] = tuple(
            replace(event, monotonic_ns=cycle0_activation_ns + 1)
            if event.event_type == "epoch.command_committed"
            and event.payload.get("successor_epoch_number") == 1
            else event
            for event in replica_events[0]
        )
        arguments["replica_events"] = replica_events
    elif mutation == "cycle0-premature-convergence":
        manager_events[cycle0_converged_index] = replace(
            manager_events[cycle0_converged_index], monotonic_ns=519
        )
    elif mutation == "cycle0-activation-identity":
        replica_events = dict(arguments["replica_events"])
        replica_events[0] = tuple(
            replace(
                event,
                payload={**event.payload, "epoch_digest": "cc" * 32},
            )
            if event.event_type == "epoch.activated"
            and event.payload.get("epoch_number") == 1
            else event
            for event in replica_events[0]
        )
        arguments["replica_events"] = replica_events
    elif mutation == "selection-after-command":
        manager_events[selection_index] = replace(
            manager_events[selection_index], monotonic_ns=1_001
        )
    elif mutation == "command-after-activation":
        replica_events = dict(arguments["replica_events"])
        cycle1_activation_ns = next(
            event.monotonic_ns
            for event in replica_events[0]
            if event.event_type == "epoch.activated"
            and event.payload.get("epoch_number") == 2
        )
        replica_events[0] = tuple(
            replace(event, monotonic_ns=cycle1_activation_ns + 1)
            if event.event_type == "epoch.command_committed"
            and event.payload.get("successor_epoch_number") == 2
            else event
            for event in replica_events[0]
        )
        arguments["replica_events"] = replica_events
    elif mutation == "premature-convergence":
        manager_events[cycle1_converged_index] = replace(
            manager_events[cycle1_converged_index], monotonic_ns=2_019
        )
    elif mutation == "convergence-after-terminal":
        manager_events[terminal_index] = replace(
            manager_events[terminal_index],
            monotonic_ns=(
                manager_events[cycle1_converged_index].monotonic_ns - 1
            ),
        )
    elif mutation == "missing-converged":
        manager_events = [
            event
            for index, event in enumerate(manager_events)
            if index != cycle1_converged_index
        ]
    elif mutation == "missing-cycle0-converged":
        manager_events = [
            event
            for index, event in enumerate(manager_events)
            if index != cycle0_converged_index
        ]
    elif mutation == "duplicate-cycle0-converged":
        manager_events.insert(
            selection_index,
            replace(
                manager_events[cycle0_converged_index],
                line_number=29,
                source_sequence=29,
                monotonic_ns=801,
                line_sha256="52" * 32,
            ),
        )
    elif mutation == "drifted-cycle0-converged":
        converged = manager_events[cycle0_converged_index]
        identity = dict(converged.payload["identity"])
        identity["successor_epoch_digest"] = "cc" * 32
        manager_events[cycle0_converged_index] = replace(
            converged,
            payload={**converged.payload, "identity": identity},
        )
    elif mutation.startswith("drifted-cycle0-"):
        converged = manager_events[cycle0_converged_index]
        identity = dict(converged.payload["identity"])
        if mutation == "drifted-cycle0-predecessor-digest":
            identity["predecessor_epoch_digest"] = "cc" * 32
        elif mutation == "drifted-cycle0-payload-digest":
            identity["command_payload_digest"] = "cc" * 32
        elif mutation == "drifted-cycle0-block-hash":
            identity["command_block_hash"] = "cc" * 32
        else:
            identity["command_block_height"] += 1
            identity["activation_height"] += 1
        manager_events[cycle0_converged_index] = replace(
            converged,
            payload={**converged.payload, "identity": identity},
        )
    elif mutation == "duplicate-cycle1-converged":
        manager_events.insert(
            terminal_index,
            replace(
                manager_events[cycle1_converged_index],
                line_number=30,
                source_sequence=30,
                monotonic_ns=3_001,
                line_sha256="57" * 32,
            ),
        )
    elif mutation == "unknown-converged-transition":
        identity = dict(manager_events[cycle0_converged_index].payload["identity"])
        identity.update(
            {
                "predecessor_epoch_number": 2,
                "predecessor_epoch_digest": "cc" * 32,
                "successor_epoch_number": 3,
                "successor_epoch_digest": "dd" * 32,
            }
        )
        manager_events.append(
            replace(
                manager_events[cycle0_converged_index],
                line_number=31,
                source_sequence=31,
                monotonic_ns=3_050,
                payload={
                    **manager_events[cycle0_converged_index].payload,
                    "identity": identity,
                },
                line_sha256="58" * 32,
            )
        )
    elif mutation == "malformed-converged-transition":
        manager_events.append(
            replace(
                manager_events[cycle0_converged_index],
                line_number=32,
                source_sequence=32,
                monotonic_ns=3_050,
                payload={
                    **manager_events[cycle0_converged_index].payload,
                    "identity": {"predecessor_epoch_number": 9},
                },
                line_sha256="59" * 32,
            )
        )
    elif mutation == "drifted-converged":
        converged = manager_events[cycle1_converged_index]
        identity = dict(converged.payload["identity"])
        identity["command_block_hash"] = "77" * 32
        manager_events[cycle1_converged_index] = replace(
            converged,
            payload={**converged.payload, "identity": identity},
        )
    elif mutation == "short-converged":
        converged = manager_events[cycle1_converged_index]
        expected = arguments["expected"]
        manager_events[cycle1_converged_index] = replace(
            converged,
            payload={
                **converged.payload,
                "accepted_commit_count": expected.q - 1,
            },
        )
    elif mutation == "required-count-drift":
        converged = manager_events[cycle1_converged_index]
        expected = arguments["expected"]
        manager_events[cycle1_converged_index] = replace(
            converged,
            payload={
                **converged.payload,
                "accepted_commit_count": expected.q + 1,
                "accepted_activation_count": expected.q + 1,
                "required_activation_count": expected.q + 1,
            },
        )
    else:
        terminal = manager_events[terminal_index]
        manager_events[terminal_index] = replace(
            terminal,
            payload={**terminal.payload, "reason": "convergence_retry_exhausted"},
        )
    arguments["manager_events"] = tuple(manager_events)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(FactorialValidationError, match=reason):
        validation._validate_v26_verified_response_duplicate_live_exercise(
            **arguments
        )


def _v27_duplicate_delivery_live_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], Path]:
    arguments, log_path = _v26_duplicate_delivery_live_fixture(tmp_path)
    arguments["manifest_id"] = validation.V27_MANIFEST_ID
    lines = log_path.read_text(encoding="utf-8").splitlines()
    log_path.write_text(
        "\n".join(
            (
                *lines[:5],
                lines[6],
                lines[7],
                lines[5],
                "[EPOCH] Adaptive-v2 convergence evidence unhealthy: "
                "shared_outbox_delivery_failed",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return arguments, log_path


def test_v27_slot037_binds_post_dispatch_duplicate_and_permits_proven_tail(
    tmp_path: Path,
) -> None:
    arguments, _ = _v27_duplicate_delivery_live_fixture(tmp_path)
    assert validation._validate_v27_verified_response_duplicate_live_exercise(
        **arguments
    )
    assert not validation._validate_v26_verified_response_duplicate_live_exercise(
        **arguments
    )


def _v28_duplicate_probe_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], Path]:
    arguments, log_path = _v26_duplicate_delivery_live_fixture(tmp_path)
    predecessor_digest = arguments["predecessor_epoch_digest"]
    proposal_block_hash = "88" * 32
    reporter = 6
    child = 1
    tree_id = 6
    response_ns = 900
    fault_end_ns = 500
    lines = log_path.read_text(encoding="utf-8").splitlines()
    guard = lines[5]
    probe = (
        "KAURI_EXPERIMENT response_duplicate_probe "
        f"mode={RESPONSE_DUPLICATE_PROBE_MODE} consensus_accepted=1 "
        f"reporter={reporter} child={child} epoch=1 tree={tree_id} "
        f"digest={predecessor_digest} block={proposal_block_hash} "
        "message_type=aggregate_relay "
        f"response_monotonic_ns={response_ns} "
        f"window_end_monotonic_ns={fault_end_ns} "
        "first_call_recorded=1 second_call_recorded=0"
    )
    log_path.write_text(
        "\n".join(
            (
                *lines[:4],
                guard,
                probe,
                "[EPOCH] Adaptive-v2 convergence evidence unhealthy: "
                "shared_outbox_delivery_failed",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    evidence = validation._EvidenceRecord(
        ingestion_sequence=1,
        acceptance_monotonic_ns=950,
        observation_id="99" * 32,
        reporter_id=reporter,
        target_id=child,
        epoch_number=1,
        tree_id=tree_id,
        epoch_digest=predecessor_digest,
        block_hash=proposal_block_hash,
        message_type="aggregate_relay",
        outcome="on_time",
        response_duration_us=20,
        deadline_duration_us=20,
        reporter_monotonic_ns=response_ns,
        reporter_sequence=1,
        signer_set=(1, 3),
    )
    arguments.update(
        {
            "manifest_id": "shape-placement-factorial-v28",
            "accepted": {(1, predecessor_digest): (evidence,)},
            "fault_window_end_ns": fault_end_ns,
        }
    )
    return arguments, log_path


def test_v32_probe_accepts_opaque_bridge_generation_distinct_from_packed_view(
    tmp_path: Path,
) -> None:
    arguments, _ = _v28_duplicate_probe_fixture(tmp_path)

    assert validation._validate_v28_verified_response_duplicate_probe(**arguments)


def test_v32_probe_accepts_same_packed_view_with_distinct_bridge_generations(
    tmp_path: Path,
) -> None:
    arguments, _ = _v28_duplicate_probe_fixture(tmp_path)
    reporter = 2
    child = 7
    tree_id = 6
    response_ns = 901
    view_generation = ((1 << 32) | tree_id) + 1
    predecessor_digest = arguments["predecessor_epoch_digest"]
    block_hash = "88" * 32
    relative = f"raw/process/replica-{reporter}.stderr.log"
    log_path = tmp_path / relative
    log_path.write_text(
        "\n".join(
            (
                "KAURI_EVIDENCE response_attempt_armed "
                f"reporter={reporter} child={child} epoch=1 tree={tree_id} "
                f"epoch_digest={predecessor_digest} block={block_hash} "
                "expected_message_type=aggregate_relay start_monotonic_ns=11 "
                "deadline_duration_us=20 absolute_deadline_ns=20011",
                "KAURI_RELAY_INGRESS stage=begin "
                f"recipient={reporter} source_replica={child}",
                "KAURI_RELAY_INGRESS stage=result "
                f"recipient={reporter} source_replica={child} root=6 "
                "error=0 wire_error=0 permission=3 envelope=1 "
                f"epoch=1 tree={tree_id} block={block_hash} "
                f"generation={view_generation}",
                "KAURI_RELAY_INGRESS stage=dispatch_complete "
                f"recipient={reporter} source_replica={child} root=6 dispatched=1",
                "KAURI_RESPONSE_EVIDENCE disposition=idempotent_duplicate "
                f"reporter={reporter} child={child} epoch=1 tree={tree_id} "
                f"digest={predecessor_digest} block={block_hash} "
                "message_type=aggregate_relay attempt_generation=2951 "
                f"response_monotonic_ns={response_ns}",
                "KAURI_EXPERIMENT response_duplicate_probe "
                f"mode={RESPONSE_DUPLICATE_PROBE_MODE} consensus_accepted=1 "
                f"reporter={reporter} child={child} epoch=1 tree={tree_id} "
                f"digest={predecessor_digest} block={block_hash} "
                "message_type=aggregate_relay "
                f"response_monotonic_ns={response_ns} "
                "window_end_monotonic_ns=500 "
                "first_call_recorded=1 second_call_recorded=0",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    replica_events = dict(arguments["replica_events"])
    reporter_events = replica_events[reporter]
    replica_events[reporter] = (
        reporter_events[0],
        reporter_events[1],
        _native_event(
            source_id=f"replica-{reporter}",
            sequence=3,
            monotonic_ns=976,
            event_type="block.committed",
            payload={
                "block_height": 75,
                "block_hash": block_hash,
                "parent_hash": "77" * 32,
                "transaction_count": 1000,
                "designated_observer": False,
                "decision_proof": {
                    "epoch_number": 1,
                    "tree_id": tree_id,
                    "epoch_digest": predecessor_digest,
                    "block_hash": block_hash,
                },
                "view_generation": view_generation,
                "commit_batch_index": 0,
            },
        ),
        replace(reporter_events[2], line_number=4, source_sequence=4),
        replace(reporter_events[3], line_number=5, source_sequence=5),
    )
    evidence = arguments["accepted"][(1, predecessor_digest)][0]
    arguments["accepted"] = {
        (1, predecessor_digest): (
            evidence,
            replace(
                evidence,
                ingestion_sequence=2,
                observation_id="98" * 32,
                reporter_id=reporter,
                target_id=child,
                acceptance_monotonic_ns=951,
                reporter_monotonic_ns=response_ns,
                reporter_sequence=2,
                signer_set=(7, 8),
            ),
        )
    }
    arguments["replica_events"] = replica_events

    assert validation._validate_v28_verified_response_duplicate_probe(**arguments)


def _v31_relay_result_schema_fixture(
    tmp_path: Path,
) -> tuple[dict[int, tuple[str, ...]], Path]:
    reporter = 6
    child = 1
    proposal_block_hash = "88" * 32
    relative = f"raw/process/replica-{reporter}.stderr.log"
    log_path = tmp_path / relative
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            (
                "KAURI_RELAY_INGRESS stage=begin "
                f"recipient={reporter} source_replica={child}",
                "KAURI_RELAY_INGRESS stage=result "
                f"recipient={reporter} source_replica={child} root={reporter} "
                "error=0 wire_error=0 permission=3 envelope=1 "
                f"epoch=1 tree={reporter} block={proposal_block_hash} "
                "generation=4294967303",
                "KAURI_RELAY_INGRESS stage=dispatch_complete "
                f"recipient={reporter} source_replica={child} root={reporter} "
                "dispatched=1",
                "KAURI_RELAY_INGRESS stage=begin "
                f"recipient={reporter} source_replica={child}",
                "KAURI_RELAY_INGRESS stage=result "
                f"recipient={reporter} source_replica={child} root=0 "
                "error=0 wire_error=0 permission=3 envelope=1 "
                f"epoch=0 tree=0 block={proposal_block_hash} generation=1",
                "KAURI_RELAY_INGRESS stage=dispatch_complete "
                f"recipient={reporter} source_replica={child} root=0 dispatched=1",
                "KAURI_RELAY_INGRESS stage=begin "
                f"recipient={reporter} source_replica={child}",
                "KAURI_RELAY_INGRESS stage=result "
                f"recipient={reporter} source_replica={child} root=0 "
                "error=3 wire_error=0 permission=5 envelope=0 "
                "epoch=0 tree=0 block=none generation=0",
                "KAURI_RELAY_INGRESS stage=dispatch_complete "
                f"recipient={reporter} source_replica={child} root=0 dispatched=0",
                "KAURI_RELAY_INGRESS stage=begin "
                f"recipient={reporter} source_replica={child}",
                "KAURI_RELAY_INGRESS stage=result "
                f"recipient={reporter} source_replica={child} root=0 "
                "error=2 wire_error=1 permission=5 envelope=0 "
                "epoch=0 tree=0 block=none generation=0",
                "KAURI_RELAY_INGRESS stage=dispatch_complete "
                f"recipient={reporter} source_replica={child} root=0 dispatched=0",
                "KAURI_RELAY_INGRESS stage=begin "
                f"recipient={reporter} source_replica={child}",
                "KAURI_RELAY_INGRESS stage=result "
                f"recipient={reporter} source_replica={child} root=0 "
                "error=2 wire_error=9 permission=5 envelope=0 "
                "epoch=0 tree=0 block=none generation=0",
                "KAURI_RELAY_INGRESS stage=dispatch_complete "
                f"recipient={reporter} source_replica={child} root=0 dispatched=0",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return {reporter: (relative,)}, log_path


def test_v31_relay_result_schema_accepts_exact_rejected_no_envelope_sentinel(
    tmp_path: Path,
) -> None:
    paths_by_replica, _ = _v31_relay_result_schema_fixture(tmp_path)
    witnesses, _ = validation._relay_ingress_witnesses(
        tmp_path,
        paths_by_replica,
    )

    assert len(witnesses) == 5
    (
        accepted,
        accepted_zero_identity,
        state_rejected,
        decode_rejected_low,
        decode_rejected_high,
    ) = witnesses
    assert accepted.accepted
    assert accepted_zero_identity.accepted
    assert (
        accepted_zero_identity.root_id,
        accepted_zero_identity.epoch_number,
        accepted_zero_identity.tree_id,
    ) == (0, 0, 0)
    assert not state_rejected.accepted
    assert not decode_rejected_low.accepted
    assert not decode_rejected_high.accepted
    assert (
        state_rejected.ingress_error,
        state_rejected.wire_error,
        state_rejected.permission,
        state_rejected.envelope_present,
        state_rejected.root_id,
        state_rejected.epoch_number,
        state_rejected.tree_id,
        state_rejected.block_hash,
        state_rejected.view_generation,
        state_rejected.dispatched,
    ) == (3, 0, 5, 0, 0, 0, 0, "none", 0, 0)
    assert (decode_rejected_low.ingress_error, decode_rejected_low.wire_error) == (
        2,
        1,
    )
    assert (decode_rejected_high.ingress_error, decode_rejected_high.wire_error) == (
        2,
        9,
    )


@pytest.mark.parametrize(
    "mutation,reason",
    (
        ("envelope-one-generation-zero", "enveloped relay result generation"),
        ("envelope-one-block-none", "enveloped relay result block"),
        ("envelope-zero-generation", "no-envelope relay result sentinel"),
        ("envelope-zero-root", "no-envelope relay result sentinel"),
        ("envelope-zero-epoch", "no-envelope relay result sentinel"),
        ("envelope-zero-tree", "no-envelope relay result sentinel"),
        ("envelope-zero-block", "no-envelope relay result sentinel"),
        ("decode-reject-wire-zero", "no-envelope relay result sentinel"),
        ("state-reject-wire-nonzero", "no-envelope relay result sentinel"),
        ("envelope-zero-unreachable-error", "no-envelope relay result sentinel"),
        ("envelope-zero-other-error", "no-envelope relay result sentinel"),
        ("envelope-zero-permission", "no-envelope relay result sentinel"),
        ("envelope-zero-dispatched", "no-envelope relay result sentinel"),
        ("envelope-outside-binary", "relay result envelope indicator"),
        ("accepted-error", "enveloped relay result tuple"),
        ("accepted-wire-error", "enveloped relay result tuple"),
        ("accepted-permission", "enveloped relay result tuple"),
        ("accepted-not-dispatched", "enveloped relay result tuple"),
        ("begin-recipient-overflow", "relay begin recipient.*unsigned 16-bit"),
        ("begin-source-overflow", "relay begin source.*unsigned 16-bit"),
        ("result-recipient-overflow", "relay result recipient.*unsigned 16-bit"),
        ("result-source-overflow", "relay result source.*unsigned 16-bit"),
        ("result-root-overflow", "relay result root.*unsigned 16-bit"),
        ("result-epoch-overflow", "relay epoch.*unsigned 32-bit"),
        ("result-tree-overflow", "relay tree.*unsigned 32-bit"),
        ("complete-recipient-overflow", "relay complete recipient.*unsigned 16-bit"),
        ("complete-source-overflow", "relay complete source.*unsigned 16-bit"),
        ("complete-root-overflow", "relay complete root.*unsigned 16-bit"),
        ("generation-overflow", "relay view generation.*unsigned 64-bit"),
    ),
)
def test_v31_relay_result_schema_is_fail_closed(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    paths_by_replica, log_path = _v31_relay_result_schema_fixture(tmp_path)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    accepted_result = 1
    accepted_complete = 2
    rejected_result = 7
    rejected_complete = 8
    if mutation == "envelope-one-generation-zero":
        lines[accepted_result] = lines[accepted_result].replace(
            "generation=4294967303", "generation=0"
        )
    elif mutation == "envelope-one-block-none":
        lines[accepted_result] = lines[accepted_result].replace(
            f"block={'88' * 32}", "block=none"
        )
    elif mutation == "envelope-zero-generation":
        lines[rejected_result] = lines[rejected_result].replace(
            "generation=0", "generation=1"
        )
    elif mutation == "envelope-zero-root":
        lines[rejected_result] = lines[rejected_result].replace("root=0", "root=1")
    elif mutation == "envelope-zero-epoch":
        lines[rejected_result] = lines[rejected_result].replace(
            "epoch=0", "epoch=1"
        )
    elif mutation == "envelope-zero-tree":
        lines[rejected_result] = lines[rejected_result].replace("tree=0", "tree=1")
    elif mutation == "envelope-zero-block":
        lines[rejected_result] = lines[rejected_result].replace(
            "block=none", f"block={'99' * 32}"
        )
    elif mutation == "decode-reject-wire-zero":
        lines[rejected_result] = lines[rejected_result].replace("error=3", "error=2")
    elif mutation == "state-reject-wire-nonzero":
        lines[rejected_result] = lines[rejected_result].replace(
            "wire_error=0", "wire_error=1"
        )
    elif mutation == "envelope-zero-unreachable-error":
        lines[rejected_result] = lines[rejected_result].replace("error=3", "error=1")
    elif mutation == "envelope-zero-other-error":
        lines[rejected_result] = lines[rejected_result].replace("error=3", "error=4")
    elif mutation == "envelope-zero-permission":
        lines[rejected_result] = lines[rejected_result].replace(
            "permission=5", "permission=3"
        )
    elif mutation == "envelope-zero-dispatched":
        lines[rejected_complete] = lines[rejected_complete].replace(
            "dispatched=0", "dispatched=1"
        )
    elif mutation == "envelope-outside-binary":
        lines[rejected_result] = lines[rejected_result].replace(
            "envelope=0", "envelope=2"
        )
    elif mutation == "accepted-error":
        lines[accepted_result] = lines[accepted_result].replace(
            " error=0 wire_error=0 ",
            " error=1 wire_error=0 ",
        )
    elif mutation == "accepted-wire-error":
        lines[accepted_result] = lines[accepted_result].replace(
            "wire_error=0", "wire_error=1"
        )
    elif mutation == "accepted-permission":
        lines[accepted_result] = lines[accepted_result].replace(
            "permission=3", "permission=5"
        )
    elif mutation == "accepted-not-dispatched":
        lines[accepted_complete] = lines[accepted_complete].replace(
            "dispatched=1", "dispatched=0"
        )
    elif mutation == "begin-recipient-overflow":
        lines[0] = lines[0].replace("recipient=6", "recipient=65536")
    elif mutation == "begin-source-overflow":
        lines[0] = lines[0].replace("source_replica=1", "source_replica=65536")
    elif mutation == "result-recipient-overflow":
        lines[accepted_result] = lines[accepted_result].replace(
            "recipient=6", "recipient=65536"
        )
    elif mutation == "result-source-overflow":
        lines[accepted_result] = lines[accepted_result].replace(
            "source_replica=1", "source_replica=65536"
        )
    elif mutation == "result-root-overflow":
        lines[accepted_result] = lines[accepted_result].replace("root=6", "root=65536")
    elif mutation == "result-epoch-overflow":
        lines[accepted_result] = lines[accepted_result].replace(
            "epoch=1", "epoch=4294967296"
        )
    elif mutation == "result-tree-overflow":
        lines[accepted_result] = lines[accepted_result].replace(
            "tree=6", "tree=4294967296"
        )
    elif mutation == "complete-recipient-overflow":
        lines[accepted_complete] = lines[accepted_complete].replace(
            "recipient=6", "recipient=65536"
        )
    elif mutation == "complete-source-overflow":
        lines[accepted_complete] = lines[accepted_complete].replace(
            "source_replica=1", "source_replica=65536"
        )
    elif mutation == "complete-root-overflow":
        lines[accepted_complete] = lines[accepted_complete].replace(
            "root=6", "root=65536"
        )
    else:
        lines[accepted_result] = lines[accepted_result].replace(
            "generation=4294967303", "generation=18446744073709551616"
        )
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(FactorialValidationError, match=reason):
        validation._relay_ingress_witnesses(tmp_path, paths_by_replica)


@pytest.mark.parametrize("error,wire_error", ((3, 0), (2, 1), (2, 9)))
def test_v31_rejected_no_envelope_sentinel_cannot_satisfy_probe_ingress(
    tmp_path: Path,
    error: int,
    wire_error: int,
) -> None:
    arguments, log_path = _v28_duplicate_probe_fixture(tmp_path)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    lines[1:4] = (
        "KAURI_RELAY_INGRESS stage=begin recipient=6 source_replica=1",
        "KAURI_RELAY_INGRESS stage=result recipient=6 source_replica=1 root=0 "
        f"error={error} wire_error={wire_error} permission=5 envelope=0 "
        "epoch=0 tree=0 "
        "block=none generation=0",
        "KAURI_RELAY_INGRESS stage=dispatch_complete recipient=6 "
        "source_replica=1 root=0 dispatched=0",
    )
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(
        FactorialValidationError,
        match="lacks its original accepted ingress",
    ):
        validation._validate_v28_verified_response_duplicate_probe(**arguments)


def test_v31_repair_probe_permits_exact_rejected_no_envelope_sentinels(
    tmp_path: Path,
) -> None:
    arguments, log_path = _v28_duplicate_probe_fixture(tmp_path)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    for error, wire_error in ((3, 0), (2, 1), (2, 9)):
        lines.extend(
            (
                "KAURI_RELAY_INGRESS stage=begin recipient=6 source_replica=1",
                "KAURI_RELAY_INGRESS stage=result recipient=6 source_replica=1 "
                f"root=0 error={error} wire_error={wire_error} permission=5 "
                "envelope=0 epoch=0 tree=0 block=none generation=0",
                "KAURI_RELAY_INGRESS stage=dispatch_complete recipient=6 "
                "source_replica=1 root=0 dispatched=0",
            )
        )
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert validation._validate_v28_verified_response_duplicate_probe(**arguments)


@pytest.mark.parametrize(
    "manifest_id",
    (
        validation.V28_MANIFEST_ID,
        validation.V29_MANIFEST_ID,
        validation.V30_MANIFEST_ID,
        validation.V31_MANIFEST_ID,
        validation.V32_MANIFEST_ID,
        validation.V33_MANIFEST_ID,
        validation.V34_MANIFEST_ID,
        validation.FROZEN_MANIFEST_ID,
    ),
)
def test_v28_through_v35_slot037_bind_exact_bridge_only_duplicate_probe(
    tmp_path: Path,
    manifest_id: str,
) -> None:
    arguments, _ = _v28_duplicate_probe_fixture(tmp_path)
    arguments["manifest_id"] = manifest_id
    assert validation._validate_v28_verified_response_duplicate_probe(
        **arguments
    )


def test_v28_probe_pairing_permits_unrelated_interleaved_logging(
    tmp_path: Path,
) -> None:
    arguments, log_path = _v28_duplicate_probe_fixture(tmp_path)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    probe_index = next(
        index for index, line in enumerate(lines) if "response_duplicate_probe" in line
    )
    lines.insert(probe_index, "unrelated native diagnostic")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert validation._validate_v28_verified_response_duplicate_probe(
        **arguments
    )


@pytest.mark.parametrize(
    "mutation,reason",
    (
        ("missing-probe", "lacks a response duplicate probe"),
        ("missing-guard", "lacks its preceding guard"),
        ("wrong-mode", "contract fields"),
        ("consensus-rejected", "contract fields"),
        ("wrong-result-bits", "contract fields"),
        ("wrong-window", "contract fields"),
        ("at-fault-end", "contract fields"),
        ("at-reporter-command", "pre-reporter-command"),
        ("wrong-child", "contract fields"),
        ("responsive-leaf-child", "internal-child topology"),
        ("wrong-epoch", "contract fields"),
        ("wrong-type", "contract fields"),
        ("guard-identity", "probe/guard identity"),
        ("guard-timestamp", "probe/guard identity"),
        ("guard-generation-zero", "duplicate attempt generation.*unsigned 64-bit"),
        (
            "guard-generation-overflow",
            "duplicate attempt generation.*unsigned 64-bit",
        ),
        ("ingress-generation-zero", "enveloped relay result generation"),
        (
            "ingress-generation-overflow",
            "relay view generation.*unsigned 64-bit",
        ),
        ("ingress-packed-epoch", "ingress packed view identity"),
        ("ingress-packed-tree", "ingress packed view identity"),
        ("missing-ingress", "original accepted ingress"),
        ("rejected-ingress", "original accepted ingress"),
        ("missing-arm", "exact arm"),
        ("duplicate-arm", "duplicate/rearm response-attempt arm identity"),
        ("missing-commit", "same-block structured commit"),
        ("commit-view-generation", "structured commit view generation"),
        ("missing-observation", "Epoch1 accepted evidence"),
        ("timeout-observation", "on-time observation"),
        ("duplicate-reporter", "more than one.*per reporter"),
        ("intervening-guard", "probe/guard identity"),
        ("duplicate-exact-guard", "probe guard is absent or duplicated"),
        ("intervening-begin", "not immediately followed by its probe"),
        ("deadline-poison", "response deadline evidence failed"),
        ("unknown-poison", "unknown convergence poison"),
        ("missing-convergence", "converged event"),
        ("missing-activation", "commit/activation convergence"),
        ("campaign-leak", "escaped the exact excluded repair smoke"),
    ),
)
def test_v28_response_duplicate_probe_is_fail_closed(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    arguments, log_path = _v28_duplicate_probe_fixture(tmp_path)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    probe_index = next(
        index for index, line in enumerate(lines) if "response_duplicate_probe" in line
    )
    guard_index = next(
        index
        for index, line in enumerate(lines)
        if "KAURI_RESPONSE_EVIDENCE" in line
    )
    if mutation == "missing-probe":
        del lines[probe_index]
    elif mutation == "missing-guard":
        del lines[guard_index]
    elif mutation == "wrong-mode":
        lines[probe_index] = lines[probe_index].replace(
            RESPONSE_DUPLICATE_PROBE_MODE,
            RESPONSE_DUPLICATE_PROBE_MODE + "-drift",
        )
    elif mutation == "consensus-rejected":
        lines[probe_index] = lines[probe_index].replace(
            "consensus_accepted=1", "consensus_accepted=0"
        )
    elif mutation == "wrong-result-bits":
        lines[probe_index] = lines[probe_index].replace(
            "first_call_recorded=1 second_call_recorded=0",
            "first_call_recorded=0 second_call_recorded=1",
        )
    elif mutation == "wrong-window":
        lines[probe_index] = lines[probe_index].replace(
            "window_end_monotonic_ns=500", "window_end_monotonic_ns=501"
        )
    elif mutation == "at-fault-end":
        lines[probe_index] = lines[probe_index].replace(
            "response_monotonic_ns=900", "response_monotonic_ns=500"
        )
    elif mutation == "at-reporter-command":
        lines[probe_index] = lines[probe_index].replace(
            "response_monotonic_ns=900", "response_monotonic_ns=1006"
        )
        lines[guard_index] = lines[guard_index].replace(
            "response_monotonic_ns=900", "response_monotonic_ns=1006"
        )
        evidence = arguments["accepted"][(1, arguments["predecessor_epoch_digest"])][0]
        arguments["accepted"] = {
            (1, arguments["predecessor_epoch_digest"]): (
                replace(
                    evidence,
                    reporter_monotonic_ns=1006,
                    acceptance_monotonic_ns=1006,
                ),
            )
        }
    elif mutation == "wrong-child":
        lines[probe_index] = lines[probe_index].replace("child=1", "child=2")
    elif mutation == "responsive-leaf-child":
        lines = [
            line.replace("child=1", "child=16").replace(
                "source_replica=1", "source_replica=16"
            )
            for line in lines
        ]
        key = (1, arguments["predecessor_epoch_digest"])
        evidence = arguments["accepted"][key][0]
        arguments["accepted"] = {key: (replace(evidence, target_id=16),)}
    elif mutation == "wrong-epoch":
        lines[probe_index] = lines[probe_index].replace("epoch=1", "epoch=0")
    elif mutation == "wrong-type":
        lines[probe_index] = lines[probe_index].replace(
            "message_type=aggregate_relay", "message_type=direct_vote"
        )
    elif mutation == "guard-identity":
        lines[guard_index] = lines[guard_index].replace("block=8888", "block=9988")
    elif mutation == "guard-timestamp":
        lines[guard_index] = lines[guard_index].replace(
            "response_monotonic_ns=900", "response_monotonic_ns=901"
        )
    elif mutation == "guard-generation-zero":
        lines[guard_index] = lines[guard_index].replace(
            "attempt_generation=2942", "attempt_generation=0"
        )
    elif mutation == "guard-generation-overflow":
        lines[guard_index] = lines[guard_index].replace(
            "attempt_generation=2942", "attempt_generation=18446744073709551616"
        )
    elif mutation in {
        "ingress-generation-zero",
        "ingress-generation-overflow",
        "ingress-packed-epoch",
        "ingress-packed-tree",
    }:
        result_index = next(
            index for index, line in enumerate(lines) if "stage=result" in line
        )
        generation = {
            "ingress-generation-zero": 0,
            "ingress-generation-overflow": 1 << 64,
            "ingress-packed-epoch": ((2 << 32) | 6) + 1,
            "ingress-packed-tree": ((1 << 32) | 7) + 1,
        }[mutation]
        lines[result_index] = lines[result_index].replace(
            "generation=4294967303", f"generation={generation}"
        )
    elif mutation == "missing-ingress":
        lines = [line for line in lines if "KAURI_RELAY_INGRESS" not in line]
    elif mutation == "rejected-ingress":
        result_index = next(
            index for index, line in enumerate(lines) if "stage=result" in line
        )
        lines[result_index] = (
            "KAURI_RELAY_INGRESS stage=result recipient=6 "
            "source_replica=1 root=0 error=3 wire_error=0 permission=5 "
            "envelope=0 epoch=0 tree=0 block=none generation=0"
        )
        lines[result_index + 1] = (
            "KAURI_RELAY_INGRESS stage=dispatch_complete recipient=6 "
            "source_replica=1 root=0 dispatched=0"
        )
    elif mutation == "missing-arm":
        lines = [line for line in lines if "response_attempt_armed" not in line]
    elif mutation == "duplicate-arm":
        lines.insert(1, lines[0])
    elif mutation in {"missing-commit", "commit-view-generation"}:
        replica_events = dict(arguments["replica_events"])
        reporter_events = tuple(replica_events[6])
        if mutation == "missing-commit":
            replica_events[6] = tuple(
                event
                for event in reporter_events
                if event.event_type != "block.committed"
            )
        else:
            replica_events[6] = tuple(
                replace(
                    event,
                    payload={**event.payload, "view_generation": 4294967304},
                )
                if event.event_type == "block.committed"
                else event
                for event in reporter_events
            )
        arguments["replica_events"] = replica_events
    elif mutation in {"missing-observation", "timeout-observation"}:
        key = (1, arguments["predecessor_epoch_digest"])
        evidence = arguments["accepted"][key][0]
        arguments["accepted"] = (
            {}
            if mutation == "missing-observation"
            else {key: (replace(evidence, outcome="timeout"),)}
        )
    elif mutation == "duplicate-reporter":
        lines.insert(probe_index + 1, lines[probe_index])
    elif mutation == "intervening-guard":
        lines.insert(probe_index, lines[guard_index].replace("block=8888", "block=9988"))
    elif mutation == "duplicate-exact-guard":
        lines.insert(probe_index, lines[guard_index])
    elif mutation == "intervening-begin":
        lines.insert(
            probe_index,
            "KAURI_RELAY_INGRESS stage=begin recipient=6 source_replica=1",
        )
    elif mutation == "deadline-poison":
        lines.append(
            "[EPOCH] Adaptive-v2 convergence evidence unhealthy: "
            "response_deadline_evidence_failed"
        )
    elif mutation == "unknown-poison":
        lines.append(
            "[EPOCH] Adaptive-v2 convergence evidence unhealthy: unknown_failure"
        )
    elif mutation == "missing-convergence":
        arguments["manager_events"] = tuple(
            event
            for event in arguments["manager_events"]
            if event.event_type != "adaptive_v2_converged"
        )
    elif mutation == "missing-activation":
        replica_events = dict(arguments["replica_events"])
        replica_events[30] = tuple(
            event
            for event in replica_events[30]
            if not (
                event.event_type == "epoch.activated"
                and event.payload.get("epoch_number") == 2
            )
        )
        arguments["replica_events"] = replica_events
    else:
        arguments["coverage_smoke"] = False
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(FactorialValidationError, match=reason):
        validation._validate_v28_verified_response_duplicate_probe(**arguments)


@pytest.mark.parametrize(
    "mutation,reason",
    (
        ("embedded-only", "intervening relay begin"),
        ("single-ingress", "earlier identical accepted ingress"),
        ("rejected-ingress", "earlier identical accepted ingress"),
        ("intervening-begin", "intervening relay begin"),
        ("identity-drift", "identity"),
        ("type-drift", "aggregate relay"),
        ("zero-timestamp", "response monotonic timestamp"),
        ("late-timestamp", "precedes.*command|response timestamp"),
        ("deadline-poison", "response deadline evidence failed"),
        ("unknown-unhealthy", "unknown convergence poison"),
        ("outbox-without-convergence", "converged event"),
        ("no-full-convergence", "commit/activation convergence"),
    ),
)
def test_v27_slot037_duplicate_delivery_proof_is_fail_closed(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    arguments, log_path = _v27_duplicate_delivery_live_fixture(tmp_path)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    if mutation == "embedded-only":
        lines = (*lines[:5], lines[7], lines[5], lines[6], *lines[8:])
    elif mutation == "single-ingress":
        lines = (*lines[:1], *lines[4:])
    elif mutation == "rejected-ingress":
        lines[2] = (
            "KAURI_RELAY_INGRESS stage=result recipient=6 "
            "source_replica=1 root=0 error=3 wire_error=0 permission=5 "
            "envelope=0 epoch=0 tree=0 block=none generation=0"
        )
        lines[3] = (
            "KAURI_RELAY_INGRESS stage=dispatch_complete recipient=6 "
            "source_replica=1 root=0 dispatched=0"
        )
    elif mutation == "intervening-begin":
        lines.insert(
            7,
            "KAURI_RELAY_INGRESS stage=begin recipient=6 source_replica=1",
        )
    elif mutation == "identity-drift":
        lines[7] = lines[7].replace("block=8888", "block=9988")
    elif mutation == "type-drift":
        lines[7] = lines[7].replace(
            "message_type=aggregate_relay",
            "message_type=direct_vote",
        )
    elif mutation == "zero-timestamp":
        lines[7] = lines[7].replace(
            "response_monotonic_ns=900",
            "response_monotonic_ns=0",
        )
    elif mutation == "late-timestamp":
        lines[7] = lines[7].replace(
            "response_monotonic_ns=900",
            "response_monotonic_ns=1006",
        )
    elif mutation == "deadline-poison":
        lines.append(
            "[EPOCH] Adaptive-v2 convergence evidence unhealthy: "
            "response_deadline_evidence_failed"
        )
    elif mutation == "unknown-unhealthy":
        lines.append(
            "[EPOCH] Adaptive-v2 convergence evidence unhealthy: "
            "response_deadline_result_exception"
        )
    elif mutation == "outbox-without-convergence":
        arguments["manager_events"] = tuple(
            event
            for event in arguments["manager_events"]
            if event.event_type != "adaptive_v2_converged"
        )
    else:
        replica_events = dict(arguments["replica_events"])
        replica_events[30] = tuple(
            event
            for event in replica_events[30]
            if not (
                event.event_type == "epoch.activated"
                and event.payload.get("epoch_number") == 2
            )
        )
        arguments["replica_events"] = replica_events
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(FactorialValidationError, match=reason):
        validation._validate_v27_verified_response_duplicate_live_exercise(
            **arguments
        )


def test_v25_slot037_coverage_proves_exact_inherited_leaf_placement() -> None:
    expected = _v25_inherited_placement_expected_slot()
    trees = _v25_inherited_placement_trees(expected)
    arguments = {
        "manifest_id": validation.V25_MANIFEST_ID,
        "coverage_smoke": True,
        "expected": expected,
        "cycle": 1,
        "intent": "fault_containment",
        "predecessor_trees": trees,
        "successor_trees": trees,
        "scores": _v25_inherited_placement_scores(expected),
    }

    assert validation._validate_v25_inherited_wait_exempt_placement_live_exercise(
        **arguments
    )
    assert validation._validate_v25_inherited_wait_exempt_placement_live_exercise(
        **{**arguments, "manifest_id": validation.V29_MANIFEST_ID}
    )
    assert validation._validate_v25_inherited_wait_exempt_placement_live_exercise(
        **{**arguments, "manifest_id": validation.V30_MANIFEST_ID}
    )
    assert validation._validate_v25_inherited_wait_exempt_placement_live_exercise(
        **{**arguments, "manifest_id": validation.FROZEN_MANIFEST_ID}
    )
    assert not validation._validate_v25_inherited_wait_exempt_placement_live_exercise(
        **{**arguments, "manifest_id": validation.V24_MANIFEST_ID}
    )
    assert not validation._validate_v25_inherited_wait_exempt_placement_live_exercise(
        **{**arguments, "coverage_smoke": False}
    )
    assert not validation._validate_v25_inherited_wait_exempt_placement_live_exercise(
        **{**arguments, "expected": replace(expected, slot_id="campaign-slot")}
    )


@pytest.mark.parametrize(
    "mutation,reason",
    (
        ("missing-score", "responsive and eligible"),
        ("wrong-score", "responsive and eligible"),
        ("stale-selected", "exact selected actor"),
        ("unsorted-selected", "sorted"),
        ("mixed-selected", "differ|mix"),
        ("selected-root", "physical leaf"),
        ("selected-internal", "physical leaf"),
        ("wrong-successor", "successor.*wait-exempt"),
    ),
)
def test_v25_slot037_coverage_placement_witness_is_fail_closed(
    mutation: str,
    reason: str,
) -> None:
    expected = _v25_inherited_placement_expected_slot()
    predecessor = list(_v25_inherited_placement_trees(expected))
    successor = list(_v25_inherited_placement_trees(expected))
    scores = list(_v25_inherited_placement_scores(expected))

    if mutation == "missing-score":
        scores = [score for score in scores if score.replica_id != 26]
    elif mutation == "wrong-score":
        scores[26] = replace(scores[26], classification="nonresponsive", eligible=False)
    elif mutation == "stale-selected":
        predecessor = [
            replace(tree, wait_exempt=(21, 27, 28)) for tree in predecessor
        ]
    elif mutation == "unsorted-selected":
        predecessor = [
            replace(tree, wait_exempt=(28, 27, 26)) for tree in predecessor
        ]
    elif mutation == "mixed-selected":
        predecessor[1] = replace(predecessor[1], wait_exempt=())
    elif mutation in {"selected-root", "selected-internal"}:
        selected = 26
        target_position = 0 if mutation == "selected-root" else 1
        members = list(successor[0].members)
        selected_position = members.index(selected)
        members[target_position], members[selected_position] = (
            members[selected_position],
            members[target_position],
        )
        successor[0] = replace(successor[0], members=tuple(members))
    else:
        successor[0] = replace(successor[0], wait_exempt=(26, 27))

    with pytest.raises(FactorialValidationError, match=reason):
        validation._validate_v25_inherited_wait_exempt_placement_live_exercise(
            manifest_id=validation.V25_MANIFEST_ID,
            coverage_smoke=True,
            expected=expected,
            cycle=1,
            intent="fault_containment",
            predecessor_trees=tuple(predecessor),
            successor_trees=tuple(successor),
            scores=tuple(scores),
        )


@pytest.mark.parametrize(
    "cycle,intent",
    ((0, "fault_containment"), (1, "performance_optimization")),
)
def test_v25_slot037_coverage_requires_cycle1_fault_containment(
    cycle: int,
    intent: str,
) -> None:
    expected = _v25_inherited_placement_expected_slot()
    trees = _v25_inherited_placement_trees(expected)
    with pytest.raises(FactorialValidationError, match="cycle-1 fault containment"):
        validation._validate_v25_inherited_wait_exempt_placement_live_exercise(
            manifest_id=validation.V25_MANIFEST_ID,
            coverage_smoke=True,
            expected=expected,
            cycle=cycle,
            intent=intent,
            predecessor_trees=trees,
            successor_trees=trees,
            scores=_v25_inherited_placement_scores(expected),
        )


def test_persistent_interior_proposal_accepts_exact_non_root_actor_actions() -> None:
    actors = (1, 2, 9)
    tree = Tree(
        tree_id=4,
        fanout=5,
        pipeline_stretch=2,
        members=tuple(range(13)),
        wait_exempt=actors,
    )
    phase_windows = {
        "fault_evidence": (100, 700, 6),
        "epoch1_stable": (800, 1_400, 6),
        "epoch2_stable": (1_500, 2_100, 6),
    }
    markers: list[FaultMarker] = []
    actions = {1: "omit_aggregate", 2: "omit_aggregate", 9: "omit_direct_vote"}
    for epoch, digest, timestamp in (
        (0, "11" * 32, 300),
        (1, "22" * 32, 1_000),
        (2, "33" * 32, 1_700),
    ):
        for actor in actors:
            markers.append(
                FaultMarker(
                    source_replica=actor,
                    line_number=len(markers) + 1,
                    fault_mode="persistent_selected_omission_v1",
                    epoch_number=epoch,
                    tree_id=tree.tree_id,
                    epoch_digest=digest,
                    block_hash=f"{epoch + 1:064x}",
                    window="persistent-window",
                    window_start_ns=100,
                    window_end_ns=2_100,
                    actor=actor,
                    action=actions[actor],
                    monotonic_ns=timestamp + actor,
                    raw_line_sha256=f"{len(markers) + 1:064x}",
                )
            )

    validation._validate_persistent_interior_proposals(
        markers,
        actor_ids=actors,
        phase_windows=phase_windows,
        phase_configurations=(
            ("fault_evidence", 0, "11" * 32, {tree.tree_id: tree}),
            ("epoch1_stable", 1, "22" * 32, {tree.tree_id: tree}),
            ("epoch2_stable", 2, "33" * 32, {tree.tree_id: tree}),
        ),
    )


@pytest.mark.parametrize("elapsed_s", (21, 50))
def test_post_selection_deadline_is_v2_only_for_legacy_compatibility(
    elapsed_s: int,
) -> None:
    shape_ns = 100 * validation._NANOSECONDS_PER_SECOND
    terminal_ns = shape_ns + elapsed_s * validation._NANOSECONDS_PER_SECOND

    validation._validate_transition_terminal_deadline(
        shape_ns=shape_ns,
        terminal_ns=terminal_ns,
        observation_bound_rule="phase_deadline_v1",
        convergence_deadline_s=20,
    )
    with pytest.raises(FactorialValidationError, match="post-selection deadline"):
        validation._validate_transition_terminal_deadline(
            shape_ns=shape_ns,
            terminal_ns=terminal_ns,
            observation_bound_rule=(
                "shared_slot_hard_deadline_until_manager_selection_v1"
            ),
            convergence_deadline_s=20,
        )


def test_native_epoch_activation_payload_is_flat_and_exact() -> None:
    payload = {
        "epoch_number": 1,
        "tree_id": 0,
        "epoch_digest": (
            "a1134b5b2bc72cd93fcfcc5a7e754c8b3e8be49ed9eabdd47e0cf045b0936e37"
        ),
        "activation_height": 2950,
    }

    assert (
        validation._activation_identity(payload, "epoch.activated.payload") == payload
    )

    stale_nested = {
        "configuration": {
            "epoch_number": payload["epoch_number"],
            "tree_id": payload["tree_id"],
            "epoch_digest": payload["epoch_digest"],
        },
        "activation_height": payload["activation_height"],
    }
    with pytest.raises(FactorialValidationError, match="invalid field set"):
        validation._activation_identity(
            stale_nested,
            "epoch.activated.payload",
        )


def test_native_activation_identity_is_bound_across_replicas_and_bundle() -> None:
    digest = "22" * 32
    command = validation.DecodedCommand(
        issuer_id=0,
        successor_epoch_number=1,
        predecessor_epoch_digest="11" * 32,
        successor_epoch_digest=digest,
        activation_delay_blocks=5,
        payload_digest="33" * 32,
        signature=b"signature",
    )
    bundle = validation.DecodedBundle(
        command=command,
        epoch_number=1,
        epoch_digest=digest,
        previous_epoch_digest="11" * 32,
        membership_digest="44" * 32,
        generation_seed=1,
        policy_version="test",
        evidence_snapshot_id="55" * 32,
        evidence_cutoff=1,
        trees=(
            Tree(0, 2, 2, (0, 1, 2), (1, 2)),
            Tree(1, 2, 2, (1, 2, 0), (2, 0)),
        ),
    )
    canonical: dict[int, dict[str, object]] = {}
    activation = {
        "epoch_number": 1,
        "tree_id": 0,
        "epoch_digest": digest,
        "activation_height": 2_950,
    }
    validation._record_activation_identity(
        activation,
        bundle=bundle,
        canonical_by_epoch=canonical,
    )

    with pytest.raises(FactorialValidationError, match="replicas disagree"):
        validation._record_activation_identity(
            {**activation, "tree_id": 1},
            bundle=bundle,
            canonical_by_epoch=canonical,
        )

    with pytest.raises(FactorialValidationError, match="tree absent"):
        validation._record_activation_identity(
            {**activation, "tree_id": 7},
            bundle=bundle,
            canonical_by_epoch={},
        )


def _phase_cutoff_fixture() -> tuple[
    dict[str, object],
    dict[tuple[str, int], validation._NativeEvent],
    tuple[validation._NativeEvent, ...],
    dict[int, tuple[validation._NativeEvent, ...]],
]:
    timestamps = {
        "baseline_stable": 45_000_000_000,
        "fault_window_open": 50_000_000_000,
        "epoch1_command": 55_000_000_000,
        "epoch1_activation": 56_000_000_000,
        "epoch1_stable": 57_000_000_000,
        "shape_v1_computed": 62_000_000_000,
        "epoch2_command": 63_000_000_000,
        "epoch2_activation": 64_000_000_000,
        "epoch2_stable": 65_000_000_000,
        "epoch2_drain_complete": 75_000_000_000,
    }
    event_types = {
        "baseline_stable": "block.committed",
        "epoch1_command": "epoch.command_committed",
        "epoch1_activation": "epoch.activated",
        "epoch1_stable": "block.committed",
        "shape_v1_computed": "adaptive_v2_shape_decision",
        "epoch2_command": "epoch.command_committed",
        "epoch2_activation": "epoch.activated",
        "epoch2_stable": "block.committed",
        "epoch2_drain_complete": "block.committed",
    }
    manager_events = [
        _native_event(
            source_id="adaptive-manager",
            sequence=1,
            monotonic_ns=5_000_000_000,
            event_type="process.ready",
            payload={},
        )
    ]
    replica_streams: dict[int, list[validation._NativeEvent]] = {
        0: [
            _native_event(
                source_id="replica-0",
                sequence=1,
                monotonic_ns=4_000_000_000,
                event_type="process.ready",
                payload={},
            )
        ],
        1: [
            _native_event(
                source_id="replica-1",
                sequence=1,
                monotonic_ns=4_500_000_000,
                event_type="process.ready",
                payload={},
            )
        ],
    }

    def committed(
        timestamp_ns: int,
        height: int,
        *,
        epoch_number: int = 0,
        epoch_digest: str = "11" * 32,
    ) -> validation._NativeEvent:
        event = _commit_event(
            len(replica_streams[0]) + 1,
            timestamp_ns,
            height,
            epoch_number=epoch_number,
            epoch_digest=epoch_digest,
        )
        replica_streams[0].append(event)
        return event

    def observed(
        replica_id: int,
        timestamp_ns: int,
        commit: validation._NativeEvent,
    ) -> validation._NativeEvent:
        payload = commit.payload
        event = _native_event(
            source_id=f"replica-{replica_id}",
            sequence=len(replica_streams[replica_id]) + 1,
            monotonic_ns=timestamp_ns,
            event_type="block.commit_observed",
            payload={
                "block_height": payload["block_height"],
                "block_hash": payload["block_hash"],
                "parent_hash": payload["parent_hash"],
                "transaction_count": payload["transaction_count"],
                "commit_batch_index": payload["commit_batch_index"],
            },
        )
        replica_streams[replica_id].append(event)
        return event

    baseline_common = committed(41_000_000_000, 10)
    baseline_witnesses = (
        observed(0, 42_000_000_000, baseline_common),
        observed(1, 43_000_000_000, baseline_common),
    )
    cutoff_commits = {
        "baseline_stable": committed(45_000_000_000, 11),
        "baseline_unqualified_later": committed(48_000_000_000, 12),
        "fault_progress": committed(52_000_000_000, 13),
        "epoch1_stable": committed(
            57_000_000_000, 14, epoch_number=1, epoch_digest="22" * 32
        ),
    }
    epoch1_witnesses = (
        observed(0, 58_000_000_000, cutoff_commits["epoch1_stable"]),
        observed(1, 59_000_000_000, cutoff_commits["epoch1_stable"]),
    )
    cutoff_commits["epoch1_later"] = committed(
        60_000_000_000, 15, epoch_number=1, epoch_digest="22" * 32
    )
    cutoff_commits["epoch2_stable"] = committed(
        65_000_000_000, 16, epoch_number=2, epoch_digest="33" * 32
    )
    epoch2_witnesses = (
        observed(0, 66_000_000_000, cutoff_commits["epoch2_stable"]),
        observed(1, 67_000_000_000, cutoff_commits["epoch2_stable"]),
    )
    cutoff_commits["early_drain"] = committed(
        72_000_000_000, 17, epoch_number=2, epoch_digest="33" * 32
    )
    cutoff_commits["epoch2_drain_complete"] = committed(
        75_000_000_000, 18, epoch_number=2, epoch_digest="33" * 32
    )

    transition_events: dict[str, validation._NativeEvent] = {}
    for name in (
        "epoch1_command",
        "epoch1_activation",
        "shape_v1_computed",
        "epoch2_command",
        "epoch2_activation",
    ):
        manager_source = "shape" in name
        payload: dict[str, object] = {}
        if name == "epoch1_activation":
            payload = {
                "epoch_number": 1,
                "tree_id": 0,
                "epoch_digest": "22" * 32,
                "activation_height": 14,
            }
        elif name == "epoch2_activation":
            payload = {
                "epoch_number": 2,
                "tree_id": 0,
                "epoch_digest": "33" * 32,
                "activation_height": 16,
            }
        event = _native_event(
            source_id="adaptive-manager" if manager_source else "replica-0",
            sequence=(
                len(manager_events) + 1
                if manager_source
                else len(replica_streams[0]) + 1
            ),
            monotonic_ns=timestamps[name],
            event_type=event_types[name],
            payload=payload,
        )
        transition_events[name] = event
        if manager_source:
            manager_events.append(event)
        else:
            replica_streams[0].append(event)

    named_events = {
        "baseline_stable": cutoff_commits["baseline_stable"],
        "epoch1_command": transition_events["epoch1_command"],
        "epoch1_activation": transition_events["epoch1_activation"],
        "epoch1_stable": cutoff_commits["epoch1_stable"],
        "shape_v1_computed": transition_events["shape_v1_computed"],
        "epoch2_command": transition_events["epoch2_command"],
        "epoch2_activation": transition_events["epoch2_activation"],
        "epoch2_stable": cutoff_commits["epoch2_stable"],
        "epoch2_drain_complete": cutoff_commits["epoch2_drain_complete"],
    }
    rows: list[dict[str, object]] = []
    for name in validation.CUTOFF_NAMES:
        if name == "fault_window_open":
            rows.append(
                {
                    "name": name,
                    "source_path": "slot.json",
                    "source_sequence": 0,
                    "event_type": "fault_window.open",
                    "source_monotonic_ns": timestamps[name],
                    "event_sha256": "aa" * 32,
                }
            )
            continue
        event = named_events[name]
        rows.append(
            {
                "name": name,
                "source_path": event.relative_path,
                "source_sequence": event.source_sequence,
                "event_type": event.event_type,
                "source_monotonic_ns": event.monotonic_ns,
                "event_sha256": event.line_sha256,
            }
        )
    phases = [
        ("baseline", 40_000_000_000, 0, "11" * 32),
        ("fault_evidence", 50_000_000_000, 0, "11" * 32),
        ("epoch1_stable", 57_000_000_000, 1, "22" * 32),
        ("epoch2_stable", 65_000_000_000, 2, "33" * 32),
    ]
    document: dict[str, object] = {
        "schema_version": 1,
        "slot_id": "slot-test",
        "cutoff_rule": validation.CUTOFF_RULE,
        "cutoffs": rows,
        "phases": [
            {
                "phase": name,
                "start_monotonic_ns": start,
                "end_monotonic_ns": start + 5_000_000_000,
                "bucket_count": 1,
                "configuration": {
                    "epoch_number": epoch_number,
                    "epoch_digest": epoch_digest,
                },
            }
            for name, start, epoch_number, epoch_digest in phases
        ],
        "phase_qualifications": [],
    }

    def reference(event: validation._NativeEvent) -> dict[str, object]:
        return {
            "relative_path": event.relative_path,
            "line_number": event.line_number,
            "source_id": event.source_id,
            "source_sequence": event.source_sequence,
            "source_monotonic_ns": event.monotonic_ns,
            "event_type": event.event_type,
            "line_sha256": event.line_sha256,
        }

    for name, observer, witnesses in (
        ("baseline", baseline_common, baseline_witnesses),
        ("epoch1_stable", cutoff_commits["epoch1_stable"], epoch1_witnesses),
        ("epoch2_stable", cutoff_commits["epoch2_stable"], epoch2_witnesses),
    ):
        payload = observer.payload
        document["phase_qualifications"].append(
            {
                "phase": name,
                "common_commit": {
                    "identity": {
                        "block_height": payload["block_height"],
                        "block_hash": payload["block_hash"],
                        "parent_hash": payload["parent_hash"],
                        "transaction_count": payload["transaction_count"],
                        "decision_proof": payload["decision_proof"],
                    },
                    "observer": reference(observer),
                    "witnesses": [reference(witness) for witness in witnesses],
                    "common_monotonic_ns": max(
                        observer.monotonic_ns,
                        *(witness.monotonic_ns for witness in witnesses),
                    ),
                },
            }
        )
    replica_events = {
        replica_id: tuple(stream) for replica_id, stream in replica_streams.items()
    }
    all_events = (*manager_events, *(event for stream in replica_events.values() for event in stream))
    events = {
        (event.relative_path, event.source_sequence): event for event in all_events
    }
    return document, events, tuple(manager_events), replica_events


def test_phase_cutoffs_enforce_prefault_baseline_and_full_observation_hold() -> None:
    document, events, manager_events, replica_events = _phase_cutoff_fixture()
    arguments = {
        "slot_id": "slot-test",
        "slot_receipt_sha256": "aa" * 32,
        "window_start_ns": 50_000_000_000,
        "window_end_ns": 80_000_000_000,
        "bucket_width_s": 5,
        "bucket_counts": {phase: 1 for phase in validation.PHASES},
        "events_by_ref": events,
        "manager_events": manager_events,
        "replica_events": replica_events,
        "replica_count": 2,
        "quorum": 2,
        "drain_margin_s": 5,
    }
    validation._validate_phase_cutoffs(document, **arguments)

    epoch1_activation_row = document["cutoffs"][3]
    epoch1_activation_key = (
        epoch1_activation_row["source_path"],
        epoch1_activation_row["source_sequence"],
    )
    epoch1_activation = events[epoch1_activation_key]
    stale_nested_activation = replace(
        epoch1_activation,
        payload={
            "configuration": {
                "epoch_number": epoch1_activation.payload["epoch_number"],
                "tree_id": epoch1_activation.payload["tree_id"],
                "epoch_digest": epoch1_activation.payload["epoch_digest"],
            },
            "activation_height": epoch1_activation.payload["activation_height"],
        },
    )
    with pytest.raises(FactorialValidationError, match="invalid field set"):
        validation._validate_phase_cutoffs(
            document,
            **{
                **arguments,
                "events_by_ref": {
                    **events,
                    epoch1_activation_key: stale_nested_activation,
                },
            },
        )

    with pytest.raises(FactorialValidationError, match="during the fault window"):
        validation._validate_phase_cutoffs(
            document, **{**arguments, "window_end_ns": 75_000_000_000}
        )

    early_command = copy.deepcopy(document)
    early_command["cutoffs"][2]["source_monotonic_ns"] -= 1
    early_row = early_command["cutoffs"][2]
    early_key = (early_row["source_path"], early_row["source_sequence"])
    early_event = events[early_key]
    early_command_events = dict(events)
    early_command_events[early_key] = replace(
        early_event, monotonic_ns=early_event.monotonic_ns - 1
    )
    with pytest.raises(FactorialValidationError, match="phase windows"):
        validation._validate_phase_cutoffs(
            early_command, **{**arguments, "events_by_ref": early_command_events}
        )

    cherry_picked = copy.deepcopy(document)
    later = next(
        event
        for event in replica_events[0]
        if event.event_type == "block.committed"
        and event.monotonic_ns == 48_000_000_000
    )
    cherry_picked["cutoffs"][0] = {
        "name": "baseline_stable",
        "source_path": later.relative_path,
        "source_sequence": later.source_sequence,
        "event_type": later.event_type,
        "source_monotonic_ns": later.monotonic_ns,
        "event_sha256": later.line_sha256,
    }
    cherry_picked["phases"][0]["start_monotonic_ns"] = 43_000_000_000
    cherry_picked["phases"][0]["end_monotonic_ns"] = 48_000_000_000
    with pytest.raises(FactorialValidationError, match="latest independently qualified"):
        validation._validate_phase_cutoffs(
            cherry_picked, **arguments
        )

    missing_ready = {
        **replica_events,
        1: tuple(
            event
            for event in replica_events[1]
            if event.event_type != "process.ready"
        ),
    }
    with pytest.raises(FactorialValidationError, match="exactly one process.ready"):
        validation._validate_phase_cutoffs(
            document, **{**arguments, "replica_events": missing_ready}
        )

    missing_witness = {
        **replica_events,
        1: tuple(
            event
            for event in replica_events[1]
            if not (
                event.event_type == "block.commit_observed"
                and event.monotonic_ns == 59_000_000_000
            )
        ),
    }
    with pytest.raises(FactorialValidationError, match="lacks an exact common-Q"):
        validation._validate_phase_cutoffs(
            document, **{**arguments, "replica_events": missing_witness}
        )

    epoch1_commit = next(
        event
        for event in replica_events[0]
        if event.event_type == "block.committed"
        and event.monotonic_ns == 57_000_000_000
    )
    epoch0_payload = copy.deepcopy(epoch1_commit.payload)
    epoch0_payload["decision_proof"]["epoch_number"] = 0
    epoch0_payload["decision_proof"]["epoch_digest"] = "11" * 32
    epoch0_commit = replace(epoch1_commit, payload=epoch0_payload)
    wrong_epoch_events = {
        **replica_events,
        0: tuple(
            epoch0_commit if event is epoch1_commit else event
            for event in replica_events[0]
        ),
    }
    wrong_epoch_refs = dict(events)
    wrong_epoch_refs[(epoch0_commit.relative_path, epoch0_commit.source_sequence)] = (
        epoch0_commit
    )
    with pytest.raises(FactorialValidationError, match="configuration-bound"):
        validation._validate_phase_cutoffs(
            document,
            **{
                **arguments,
                "events_by_ref": wrong_epoch_refs,
                "replica_events": wrong_epoch_events,
            },
        )

    early_drain = copy.deepcopy(document)
    early = next(
        event
        for event in replica_events[0]
        if event.event_type == "block.committed"
        and event.monotonic_ns == 72_000_000_000
    )
    early_drain["cutoffs"][-1] = {
        "name": "epoch2_drain_complete",
        "source_path": early.relative_path,
        "source_sequence": early.source_sequence,
        "event_type": early.event_type,
        "source_monotonic_ns": early.monotonic_ns,
        "event_sha256": early.line_sha256,
    }
    with pytest.raises(FactorialValidationError, match="frozen drain margin"):
        validation._validate_phase_cutoffs(early_drain, **arguments)


def _v28_repair_observation_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: int = 28,
    fault_end_ns: int = 500,
    hard_deadline_ns: int | None = None,
    epoch2_selection_ns: int | None = None,
    epoch2_command_ns_by_replica: dict[int, int] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    manifest = {
        28: _v28_candidate_manifest,
        29: _v29_candidate_manifest,
        30: _v30_candidate_manifest,
        31: _v31_candidate_manifest,
        32: _v32_candidate_manifest,
        33: _v33_candidate_manifest,
        34: _v34_candidate_manifest,
        35: _v35_candidate_manifest,
        36: _v36_candidate_manifest,
        37: _v37_candidate_manifest,
        38: _v38_candidate_manifest,
        39: _v39_candidate_manifest,
    }[version](monkeypatch)
    expected = _v25_inherited_placement_expected_slot()
    digests = ("10" * 32, "11" * 32, "22" * 32)
    v39_gate_target_ns = fault_end_ns + 5_000_000_000
    resolved_epoch2_selection_ns = (
        epoch2_selection_ns
        if epoch2_selection_ns is not None
        else v39_gate_target_ns + 1
        if version == 39
        else 600
    )
    resolved_epoch2_command_ns = {
        replica_id: (
            epoch2_command_ns_by_replica[replica_id]
            if epoch2_command_ns_by_replica is not None
            else v39_gate_target_ns + 20 + replica_id
            if version == 39
            else 620 + replica_id
        )
        for replica_id in range(expected.replica_count)
    }
    epoch2_tail_base_ns = (
        max(
            resolved_epoch2_selection_ns,
            max(resolved_epoch2_command_ns.values()),
        )
        + (50 if version == 39 else 20)
    )
    resolved_hard_deadline_ns = (
        hard_deadline_ns
        if hard_deadline_ns is not None
        else epoch2_tail_base_ns + 330
        if version == 39
        else 1_000
    )
    manager_events = (
        _native_event(
            source_id="adaptive-manager",
            sequence=1,
            monotonic_ns=100,
            event_type="adaptive_v2_evidence_snapshot",
            payload={"cycle_ordinal": 0},
        ),
        _native_event(
            source_id="adaptive-manager",
            sequence=2,
            monotonic_ns=190,
            event_type="adaptive_v2_session_terminal",
            payload={"cycle_ordinal": 0},
        ),
        _native_event(
            source_id="adaptive-manager",
            sequence=3,
            monotonic_ns=resolved_epoch2_selection_ns - 50,
            event_type="adaptive_v2_evidence_snapshot",
            payload={"cycle_ordinal": 1},
        ),
        _native_event(
            source_id="adaptive-manager",
            sequence=4,
            monotonic_ns=resolved_epoch2_selection_ns,
            event_type="adaptive_v2_shape_decision",
            payload={"cycle_ordinal": 1},
        ),
        _native_event(
            source_id="adaptive-manager",
            sequence=5,
            monotonic_ns=epoch2_tail_base_ns + 80,
            event_type="adaptive_v2_session_terminal",
            payload={"cycle_ordinal": 1},
        ),
    )
    replica_streams: dict[int, list[validation._NativeEvent]] = {}
    for replica_id in range(expected.replica_count):
        stream: list[validation._NativeEvent] = []
        for epoch, command_ns, activation_ns in (
            (1, 120 + replica_id, 151 + replica_id),
            (
                2,
                resolved_epoch2_command_ns[replica_id],
                epoch2_tail_base_ns + replica_id,
            ),
        ):
            command_height = 100 + epoch * 10
            stream.extend(
                (
                    _native_event(
                        source_id=f"replica-{replica_id}",
                        sequence=len(stream) + 1,
                        monotonic_ns=command_ns,
                        event_type="epoch.command_committed",
                        payload={
                            "command_block_height": command_height,
                            "command_block_hash": f"{epoch:064x}",
                            "payload_digest": f"{epoch + 10:064x}",
                            "predecessor_epoch_number": epoch - 1,
                            "predecessor_epoch_digest": digests[epoch - 1],
                            "successor_epoch_number": epoch,
                            "successor_epoch_digest": digests[epoch],
                            "activation_delay_blocks": 5,
                            "activation_height": command_height + 5,
                        },
                    ),
                    _native_event(
                        source_id=f"replica-{replica_id}",
                        sequence=len(stream) + 2,
                        monotonic_ns=activation_ns,
                        event_type="epoch.activated",
                        payload={
                            "epoch_number": epoch,
                            "tree_id": 0,
                            "epoch_digest": digests[epoch],
                            "activation_height": command_height + 5,
                        },
                    ),
                )
            )
        replica_streams[replica_id] = stream
    drain = _native_event(
        source_id="replica-0",
        sequence=5,
        monotonic_ns=epoch2_tail_base_ns + 230,
        event_type="block.committed",
        payload={},
    )
    replica_streams[0].append(drain)
    replica_events = {
        replica_id: tuple(stream)
        for replica_id, stream in replica_streams.items()
    }
    all_events = (
        *manager_events,
        *(event for stream in replica_events.values() for event in stream),
    )
    events_by_ref = {
        (event.relative_path, event.source_sequence): event for event in all_events
    }
    commands = {
        epoch: max(
            (
                event
                for stream in replica_events.values()
                for event in stream
                if event.event_type == "epoch.command_committed"
                and event.payload["successor_epoch_number"] == epoch
            ),
            key=lambda event: (event.monotonic_ns, event.source_id),
        )
        for epoch in (1, 2)
    }
    activations = {
        epoch: max(
            (
                event
                for stream in replica_events.values()
                for event in stream
                if event.event_type == "epoch.activated"
                and event.payload["epoch_number"] == epoch
            ),
            key=lambda event: (event.monotonic_ns, event.source_id),
        )
        for epoch in (1, 2)
    }

    def native_row(name: str, event: validation._NativeEvent) -> dict[str, object]:
        return {
            "name": name,
            "monotonic_ns": event.monotonic_ns,
            "event": validation._native_event_reference(event),
        }

    observation_contract = (
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
        if version == 39
        else (
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
            if version in {37, 38}
            else (
                EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
                if version in {34, 35, 36}
                else EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT
            )
        )
    )
    observation = {
        "schema_version": 1,
        "observation_contract": observation_contract,
        "fault_window_end_monotonic_ns": fault_end_ns,
        "hard_deadline_monotonic_ns": resolved_hard_deadline_ns,
        "rows": [
            native_row("epoch1_selection", manager_events[0]),
            native_row("epoch1_command", commands[1]),
            native_row("epoch1_activation", activations[1]),
            native_row("epoch1_terminal", manager_events[1]),
            {
                "name": "epoch1_stable_end",
                "monotonic_ns": 200,
                "derivation": "phase.epoch1_stable.end_monotonic_ns",
            },
            {
                "name": "fault_window_end",
                "monotonic_ns": fault_end_ns,
                "derivation": "shared_anchor_plus_fault_start_and_duration",
            },
            native_row("epoch2_selection", manager_events[3]),
            native_row("epoch2_command", commands[2]),
            native_row("epoch2_activation", activations[2]),
            native_row("epoch2_terminal", manager_events[4]),
            {
                "name": "epoch2_stable_end",
                "monotonic_ns": epoch2_tail_base_ns + 130,
                "derivation": "phase.epoch2_stable.end_monotonic_ns",
            },
            native_row("epoch2_drain_complete", drain),
        ],
    }
    document = {
        "cutoffs": [
            {
                "name": name,
                "source_path": event.relative_path,
                "source_sequence": event.source_sequence,
            }
            for name, event in (
                ("epoch1_command", commands[1]),
                ("epoch1_activation", activations[1]),
                ("epoch2_command", commands[2]),
                ("epoch2_activation", activations[2]),
                ("epoch2_drain_complete", drain),
            )
        ],
        "excluded_repair_observation": observation,
    }
    arguments: dict[str, object] = {
        "manifest": manifest,
        "expected": expected,
        "manager_events": manager_events,
        "replica_events": replica_events,
        "events_by_ref": events_by_ref,
        "cutoff_times": {
            "epoch1_command": commands[1].monotonic_ns,
            "epoch1_activation": activations[1].monotonic_ns,
            "epoch2_command": commands[2].monotonic_ns,
            "epoch2_activation": activations[2].monotonic_ns,
            "epoch2_drain_complete": drain.monotonic_ns,
        },
        "phase_windows": {
            "epoch1_stable": (150, 200, 1),
            "epoch2_stable": (
                epoch2_tail_base_ns + 100,
                epoch2_tail_base_ns + 130,
                1,
            ),
        },
        "fault_window_end_ns": fault_end_ns,
        "hard_deadline_ns": resolved_hard_deadline_ns,
    }
    return document, arguments


@pytest.mark.parametrize(
    "version", (28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39)
)
def test_v28_through_v39_excluded_repair_observation_bind_exact_grouped_chronology(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
) -> None:
    document, arguments = _v28_repair_observation_fixture(
        monkeypatch,
        version=version,
    )
    validation._validate_v28_excluded_repair_observation(document, **arguments)


_EXCLUDED_REPAIR_FAIL_CLOSED_MUTATIONS = (
    ("wrong-contract", "document drifted"),
    ("extra-row", "document drifted"),
    ("wrong-reference", "document drifted"),
    ("wrong-derivation", "document drifted"),
    ("epoch1-at-fault-end", "Epoch1 proof"),
    ("epoch1-stable-at-fault-end", "Epoch1 proof"),
    ("epoch2-selection-at-fault-end", "selection/all-replica"),
    ("one-epoch2-command-before-fault", "all-replica commands"),
    ("one-epoch2-command-at-fault-end", "all-replica commands"),
    ("epoch2-stable-at-hard-deadline", "Epoch2 proof"),
    ("epoch2-at-hard-deadline", "Epoch2 proof"),
    ("missing-replica-command", "all-replica"),
)


@pytest.mark.parametrize(
    "mutation,reason",
    _EXCLUDED_REPAIR_FAIL_CLOSED_MUTATIONS,
)
@pytest.mark.parametrize("version", (28, 34, 35, 36, 37, 38))
def test_v28_v34_through_v38_excluded_repair_observation_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    reason: str,
    version: int,
) -> None:
    fault_end_ns = 190 if mutation == "epoch1-at-fault-end" else 500
    if mutation == "epoch1-stable-at-fault-end":
        fault_end_ns = 200
    if mutation == "epoch2-selection-at-fault-end":
        fault_end_ns = 600
    hard_deadline_ns = {
        "epoch2-stable-at-hard-deadline": 800,
        "epoch2-at-hard-deadline": 900,
    }.get(mutation, 1_000)
    document, arguments = _v28_repair_observation_fixture(
        monkeypatch,
        version=version,
        fault_end_ns=fault_end_ns,
        hard_deadline_ns=hard_deadline_ns,
    )
    observation = document["excluded_repair_observation"]
    if mutation == "wrong-contract":
        observation["observation_contract"] += "-drift"
    elif mutation == "extra-row":
        observation["rows"].append(copy.deepcopy(observation["rows"][-1]))
    elif mutation == "wrong-reference":
        observation["rows"][0]["event"]["line_sha256"] = "ff" * 32
    elif mutation == "wrong-derivation":
        observation["rows"][4]["derivation"] = "phase.drift"
    elif mutation in {
        "missing-replica-command",
        "one-epoch2-command-before-fault",
        "one-epoch2-command-at-fault-end",
    }:
        replica_events = dict(arguments["replica_events"])
        if mutation == "missing-replica-command":
            replica_events[30] = tuple(
                event
                for event in replica_events[30]
                if not (
                    event.event_type == "epoch.command_committed"
                    and event.payload["successor_epoch_number"] == 1
                )
            )
        else:
            replica_events[0] = tuple(
                replace(
                    event,
                    monotonic_ns=(
                        500
                        if mutation == "one-epoch2-command-at-fault-end"
                        else 499
                    ),
                )
                if event.event_type == "epoch.command_committed"
                and event.payload["successor_epoch_number"] == 2
                else event
                for event in replica_events[0]
            )
        arguments["replica_events"] = replica_events
    with pytest.raises(FactorialValidationError, match=reason):
        validation._validate_v28_excluded_repair_observation(document, **arguments)


def test_v38_live_chronology_retains_bare_fault_end_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, arguments = _v28_repair_observation_fixture(
        monkeypatch,
        version=38,
        fault_end_ns=500,
        epoch2_selection_ns=501,
        epoch2_command_ns_by_replica={replica_id: 501 for replica_id in range(31)},
    )

    validation._validate_v28_excluded_repair_observation(document, **arguments)


def test_v38_live_chronology_retains_official_fault_end_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, arguments = _v28_repair_observation_fixture(
        monkeypatch,
        version=38,
        fault_end_ns=600,
    )

    with pytest.raises(FactorialValidationError) as excinfo:
        validation._validate_v28_excluded_repair_observation(document, **arguments)

    assert str(excinfo.value) == (
        "v28 excluded repair Epoch2 selection/all-replica commands did not "
        "follow fault end"
    )


def test_independent_config_reconstruction_accepts_only_exact_launcher_bytes(
    tmp_path: Path,
) -> None:
    from experiments.adaptive.kauri_experiment import factorial_execution as execution

    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    slot = plan.slots[0]
    spec = next(item for item in runtime.slots if item.slot_id == slot.slot_id)
    runtime_document = json.loads(canonical_runtime_bytes(runtime))
    spec_document = next(
        item for item in runtime_document["slots"] if item["slot_id"] == slot.slot_id
    )
    expected = next(
        item for item in validation._expected_slots(manifest)
        if item.slot_id == slot.slot_id
    )
    slot_root = tmp_path / slot.slot_id
    (slot_root / "runtime").mkdir(parents=True)
    identities = execution.IdentityMaterial(
        bls=tuple(
            {"pub": f"{replica + 1:064x}", "sec": f"{replica + 101:064x}"}
            for replica in range(slot.replica_count)
        ),
        tls=tuple(
            {
                "crt": f"{replica + 201:064x}",
                "sec": f"{replica + 301:064x}",
                "cid": f"{replica + 401:064x}",
            }
            for replica in range(slot.replica_count + 1)
        ),
        issuer={"pub": f"{901:064x}", "sec": f"{902:064x}"},
    )
    identity_rows = {
        "bls": "".join(
            f"pub:{row['pub']} sec:{row['sec']}\n" for row in identities.bls
        ),
        "tls": "".join(
            f"crt:{row['crt']} sec:{row['sec']} cid:{row['cid']}\n"
            for row in identities.tls
        ),
        "issuer": (
            f"pub:{identities.issuer['pub']} sec:{identities.issuer['sec']}\n"
        ),
    }
    for name, payload in identity_rows.items():
        (slot_root / f"runtime/{name}-identities.txt").write_text(
            payload, encoding="ascii"
        )
    execution.write_slot_configs(
        slot,
        spec,
        slot_directory=slot_root,
        identities=identities,
    )

    validation._validate_materialized_configs(
        slot_root,
        runtime=spec_document,
        expected=expected,
        manifest=manifest,
    )

    with (slot_root / "runtime/main.conf").open("ab") as output:
        output.write(b"unexpected = true\n")
    with pytest.raises(FactorialValidationError, match="independent reconstruction"):
        validation._validate_materialized_configs(
            slot_root,
            runtime=spec_document,
            expected=expected,
            manifest=manifest,
        )


def _relocated_receipt_fixture(
    tmp_path: Path,
) -> tuple[
    Path,
    Path,
    dict[str, object],
    validation._ExpectedSlot,
    dict[str, object],
    bytes,
]:
    from experiments.adaptive.kauri_experiment import factorial_execution as execution

    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    spec = runtime.slots[0]
    slot = next(item for item in plan.slots if item.slot_id == spec.slot_id)
    expected = next(
        item
        for item in validation._expected_slots(manifest)
        if item.slot_id == spec.slot_id
    )
    runtime_document = json.loads(canonical_runtime_bytes(runtime))
    spec_document = next(
        item for item in runtime_document["slots"] if item["slot_id"] == spec.slot_id
    )
    original_repository = tmp_path / "removed-original" / "Kauri"
    original_slot = original_repository / spec.result_path
    recovered_slot = tmp_path / "recovered-archive" / spec.slot_id
    (recovered_slot / "runtime").mkdir(parents=True)
    tls = tuple(
        {
            "crt": f"{replica + 201:064x}",
            "sec": f"{replica + 301:064x}",
            "cid": f"{replica + 401:064x}",
        }
        for replica in range(spec.replica_count + 1)
    )
    issuer = {"pub": f"{901:064x}", "sec": f"{902:064x}"}
    (recovered_slot / "runtime/tls-identities.txt").write_text(
        "".join(
            f"crt:{row['crt']} sec:{row['sec']} cid:{row['cid']}\n" for row in tls
        ),
        encoding="ascii",
    )
    (recovered_slot / "runtime/issuer-identities.txt").write_text(
        f"pub:{issuer['pub']} sec:{issuer['sec']}\n",
        encoding="ascii",
    )
    authorization_bytes = _canonical(
        {"authorization_id": "test-relocation-authorization"}
    )
    redaction_key = execution._derive_redaction_key(
        authorization_bytes, spec.slot_id
    )
    secrets = ManagerSecretMaterial(
        manager_tls_private_key_der_hex=tls[spec.replica_count]["sec"],
        manager_tls_certificate_der_hex=tls[spec.replica_count]["crt"],
        issuer_private_key_hex=issuer["sec"],
        replica_tls_certificate_der_hex=tuple(
            row["crt"] for row in tls[: spec.replica_count]
        ),
    )
    anchor = 1_000_000_000
    manager = execution._redact_manager_argv(
        materialize_manager_argv(
            spec,
            original_slot,
            secrets,
            shared_raw_clock_anchor_ns=anchor,
        ),
        key=redaction_key,
        key_id=hashlib.sha256(redaction_key).hexdigest()[:16],
    )
    replicas = materialize_replica_argv(spec, original_slot, anchor)
    start = anchor + spec.fault_window.start_after_prelaunch_anchor_s * 1_000_000_000
    end = start + spec.fault_window.duration_s * 1_000_000_000
    receipt: dict[str, object] = {
        "schema_version": 1,
        "slot_id": spec.slot_id,
        "runtime_artifact_id": spec.artifact_id,
        "manifest_sha256": validation.V24_MANIFEST_SHA256,
        "plan_sha256": validation.V24_PLAN_SHA256,
        "runtime_sha256": validation.V24_RUNTIME_SHA256,
        "execution_ordinal": spec.execution_ordinal,
        "attempt_ordinal": 1,
        "retry_of": None,
        "replacement_for": None,
        "shared_raw_clock_anchor_ns": anchor,
        "fault_window_start_ns": start,
        "fault_window_end_ns": end,
        "redaction_key_id": hashlib.sha256(redaction_key).hexdigest()[:16],
        "manager_argv": list(manager),
        "replica_argv": [
            {"replica_id": process.replica_id, "argv": list(process.argv)}
            for process in replicas
        ],
    }
    assert not original_repository.exists()
    return (
        recovered_slot,
        original_repository,
        receipt,
        expected,
        spec_document,
        authorization_bytes,
    )


def _v39_repair_receipt_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[
    object,
    Path,
    dict[str, object],
    validation._ExpectedSlot,
    dict[str, object],
    bytes,
]:
    manifest = _v39_candidate_manifest(monkeypatch)
    plan = build_factorial_plan(manifest)
    primary = next(
        slot for slot in plan.slots if slot.slot_id == "slot-066-n31-f5-b05-P"
    )
    repair = next(
        slot for slot in plan.slots if slot.slot_id == "slot-037-n31-f2-b04-00"
    )
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    spec = next(
        slot for slot in coverage.runtime.slots if slot.slot_id == repair.slot_id
    )
    expected = next(
        item
        for item in validation._expected_slots(manifest)
        if item.slot_id == spec.slot_id
    )
    runtime_document = json.loads(json.dumps(coverage.runtime.as_document()))
    spec_document = next(
        item
        for item in runtime_document["slots"]
        if item["slot_id"] == spec.slot_id
    )
    original_repository = tmp_path / "removed-v39-original" / "Kauri"
    original_slot = original_repository / spec.result_path
    recovered_slot = tmp_path / "recovered-v39-archive" / spec.slot_id
    (recovered_slot / "runtime").mkdir(parents=True)
    tls = tuple(
        {
            "crt": f"{replica + 1201:064x}",
            "sec": f"{replica + 1301:064x}",
            "cid": f"{replica + 1401:064x}",
        }
        for replica in range(spec.replica_count + 1)
    )
    issuer = {"pub": f"{1901:064x}", "sec": f"{1902:064x}"}
    (recovered_slot / "runtime/tls-identities.txt").write_text(
        "".join(
            f"crt:{row['crt']} sec:{row['sec']} cid:{row['cid']}\n" for row in tls
        ),
        encoding="ascii",
    )
    (recovered_slot / "runtime/issuer-identities.txt").write_text(
        f"pub:{issuer['pub']} sec:{issuer['sec']}\n",
        encoding="ascii",
    )
    authorization_bytes = _canonical(
        {"authorization_id": "test-v39-repair-receipt-authorization"}
    )
    redaction_key = execution._derive_redaction_key(
        authorization_bytes,
        spec.slot_id,
    )
    key_id = hashlib.sha256(redaction_key).hexdigest()[:16]
    secrets = ManagerSecretMaterial(
        manager_tls_private_key_der_hex=tls[spec.replica_count]["sec"],
        manager_tls_certificate_der_hex=tls[spec.replica_count]["crt"],
        issuer_private_key_hex=issuer["sec"],
        replica_tls_certificate_der_hex=tuple(
            row["crt"] for row in tls[: spec.replica_count]
        ),
    )
    anchor = 1_000_000_000
    manager = execution._redact_manager_argv(
        materialize_manager_argv(
            spec,
            original_slot,
            secrets,
            shared_raw_clock_anchor_ns=anchor,
        ),
        key=redaction_key,
        key_id=key_id,
    )
    replicas = materialize_replica_argv(spec, original_slot, anchor)
    start = anchor + spec.fault_window.start_after_prelaunch_anchor_s * 1_000_000_000
    end = start + spec.fault_window.duration_s * 1_000_000_000
    receipt: dict[str, object] = {
        "schema_version": 1,
        "slot_id": spec.slot_id,
        "runtime_artifact_id": spec.artifact_id,
        "manifest_sha256": validation.FROZEN_MANIFEST_SHA256,
        "plan_sha256": validation.FROZEN_PLAN_SHA256,
        "runtime_sha256": validation.FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
        "execution_ordinal": spec.execution_ordinal,
        "attempt_ordinal": 1,
        "retry_of": None,
        "replacement_for": None,
        "shared_raw_clock_anchor_ns": anchor,
        "fault_window_start_ns": start,
        "fault_window_end_ns": end,
        "redaction_key_id": key_id,
        "manager_argv": list(manager),
        "replica_argv": [
            {"replica_id": process.replica_id, "argv": list(process.argv)}
            for process in replicas
        ],
    }
    return (
        manifest,
        recovered_slot,
        receipt,
        expected,
        spec_document,
        authorization_bytes,
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "missing",
        "duplicate",
        "four-seconds",
        "six-seconds",
        "minus-one-nanosecond",
        "plus-one-nanosecond",
    ),
)
def test_v39_repair_receipt_requires_exact_materialized_cycle1_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    (
        manifest,
        slot_root,
        receipt,
        expected,
        runtime,
        authorization_bytes,
    ) = _v39_repair_receipt_fixture(monkeypatch, tmp_path)
    validation._validate_slot_receipt(
        receipt,
        slot_root=slot_root,
        manifest=manifest,
        expected=expected,
        runtime=runtime,
        runtime_sha256=validation.FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
        authorization_bytes=authorization_bytes,
    )

    drifted = copy.deepcopy(receipt)
    argv = drifted["manager_argv"]
    option_index = argv.index(CYCLE1_SELECTION_NOT_BEFORE_OPTION)
    if mutation == "missing":
        del argv[option_index : option_index + 2]
    elif mutation == "duplicate":
        argv[option_index:option_index] = argv[option_index : option_index + 2]
    elif mutation == "four-seconds":
        argv[option_index + 1] = str(
            int(drifted["fault_window_end_ns"]) + 4_000_000_000
        )
    elif mutation == "six-seconds":
        argv[option_index + 1] = str(
            int(drifted["fault_window_end_ns"]) + 6_000_000_000
        )
    elif mutation == "minus-one-nanosecond":
        argv[option_index + 1] = str(int(argv[option_index + 1]) - 1)
    else:
        argv[option_index + 1] = str(int(argv[option_index + 1]) + 1)

    with pytest.raises(
        FactorialValidationError,
        match="manager argv|cycle-1 selection",
    ):
        validation._validate_slot_receipt(
            drifted,
            slot_root=slot_root,
            manifest=manifest,
            expected=expected,
            runtime=runtime,
            runtime_sha256=validation.FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
            authorization_bytes=authorization_bytes,
        )


def test_v39_repair_receipt_rejects_fault_end_uint64_overflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        manifest,
        slot_root,
        receipt,
        expected,
        runtime,
        authorization_bytes,
    ) = _v39_repair_receipt_fixture(monkeypatch, tmp_path)
    overflowed = copy.deepcopy(receipt)
    anchor = validation._UINT64_MAX - 100
    overflowed["shared_raw_clock_anchor_ns"] = anchor
    overflowed["fault_window_start_ns"] = anchor + 150 * 1_000_000_000
    overflowed["fault_window_end_ns"] = anchor + 480 * 1_000_000_000

    with pytest.raises(FactorialValidationError, match="raw-clock window"):
        validation._validate_slot_receipt(
            overflowed,
            slot_root=slot_root,
            manifest=manifest,
            expected=expected,
            runtime=runtime,
            runtime_sha256=validation.FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
            authorization_bytes=authorization_bytes,
        )


def test_v39_repair_receipt_rejects_gate_target_uint64_overflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        manifest,
        slot_root,
        receipt,
        expected,
        runtime,
        authorization_bytes,
    ) = _v39_repair_receipt_fixture(monkeypatch, tmp_path)
    overflowed = copy.deepcopy(receipt)
    anchor = validation._UINT64_MAX - 480 * 1_000_000_000
    overflowed["shared_raw_clock_anchor_ns"] = anchor
    overflowed["fault_window_start_ns"] = anchor + 150 * 1_000_000_000
    overflowed["fault_window_end_ns"] = validation._UINT64_MAX

    with pytest.raises(FactorialValidationError, match="does not fit"):
        validation._validate_slot_receipt(
            overflowed,
            slot_root=slot_root,
            manifest=manifest,
            expected=expected,
            runtime=runtime,
            runtime_sha256=validation.FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
            authorization_bytes=authorization_bytes,
        )


def test_v39_repair_receipt_requires_gate_strictly_before_hard_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        manifest,
        slot_root,
        receipt,
        expected,
        runtime,
        authorization_bytes,
    ) = _v39_repair_receipt_fixture(monkeypatch, tmp_path)
    equal_hard = copy.deepcopy(runtime)
    equal_hard["fault_window"]["hard_timeout_s"] = (
        equal_hard["fault_window"]["start_after_prelaunch_anchor_s"]
        + equal_hard["fault_window"]["duration_s"]
        + 5
    )

    with pytest.raises(FactorialValidationError, match="shared hard deadline"):
        validation._validate_slot_receipt(
            receipt,
            slot_root=slot_root,
            manifest=manifest,
            expected=expected,
            runtime=equal_hard,
            runtime_sha256=validation.FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
            authorization_bytes=authorization_bytes,
        )


def _v39_receipt_bound_live_chronology_fixture(
    monkeypatch: pytest.MonkeyPatch,
    receipt_fixture: tuple[
        object,
        Path,
        dict[str, object],
        validation._ExpectedSlot,
        dict[str, object],
        bytes,
    ],
    *,
    epoch2_selection_ns: int,
    epoch2_command_ns_by_replica: dict[int, int],
) -> tuple[dict[str, object], dict[str, object]]:
    (
        manifest,
        slot_root,
        receipt,
        expected,
        runtime,
        authorization_bytes,
    ) = receipt_fixture
    validation._validate_slot_receipt(
        receipt,
        slot_root=slot_root,
        manifest=manifest,
        expected=expected,
        runtime=runtime,
        runtime_sha256=validation.FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
        authorization_bytes=authorization_bytes,
    )
    fault_end_ns = int(receipt["fault_window_end_ns"])
    manager_argv = receipt["manager_argv"]
    option_index = manager_argv.index(CYCLE1_SELECTION_NOT_BEFORE_OPTION)
    gate_target_ns = int(manager_argv[option_index + 1])
    assert gate_target_ns == fault_end_ns + 5_000_000_000
    hard_deadline_ns = int(receipt["shared_raw_clock_anchor_ns"]) + (
        int(runtime["fault_window"]["hard_timeout_s"]) * 1_000_000_000
    )
    document, arguments = _v28_repair_observation_fixture(
        monkeypatch,
        version=39,
        fault_end_ns=fault_end_ns,
        hard_deadline_ns=hard_deadline_ns,
        epoch2_selection_ns=epoch2_selection_ns,
        epoch2_command_ns_by_replica=epoch2_command_ns_by_replica,
    )
    arguments["manifest"] = manifest
    arguments["expected"] = expected
    return document, arguments


def _validate_v39_receipt_bound_live_chronology(
    monkeypatch: pytest.MonkeyPatch,
    receipt_fixture: tuple[
        object,
        Path,
        dict[str, object],
        validation._ExpectedSlot,
        dict[str, object],
        bytes,
    ],
    *,
    epoch2_selection_ns: int,
    epoch2_command_ns_by_replica: dict[int, int],
) -> None:
    document, arguments = _v39_receipt_bound_live_chronology_fixture(
        monkeypatch,
        receipt_fixture,
        epoch2_selection_ns=epoch2_selection_ns,
        epoch2_command_ns_by_replica=epoch2_command_ns_by_replica,
    )
    validation._validate_v28_excluded_repair_observation(document, **arguments)


@pytest.mark.parametrize("offset_ns", (0, -1))
@pytest.mark.parametrize(
    "boundary",
    ("selection", "command-barrier", "one-replica-command"),
)
def test_v39_live_chronology_rejects_gate_target_equality_and_target_minus_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
    offset_ns: int,
) -> None:
    receipt_fixture = _v39_repair_receipt_fixture(monkeypatch, tmp_path)
    receipt = receipt_fixture[2]
    fault_end_ns = int(receipt["fault_window_end_ns"])
    gate_target_ns = fault_end_ns + 5_000_000_000
    selection_ns = gate_target_ns + 1
    command_times = {
        replica_id: gate_target_ns + 20 + replica_id
        for replica_id in range(31)
    }
    if boundary == "selection":
        selection_ns = gate_target_ns + offset_ns
    elif boundary == "command-barrier":
        command_times = {
            replica_id: gate_target_ns + offset_ns
            for replica_id in range(31)
        }
    else:
        command_times[0] = gate_target_ns + offset_ns

    with pytest.raises(
        FactorialValidationError,
        match="selection/all-replica commands",
    ):
        _validate_v39_receipt_bound_live_chronology(
            monkeypatch,
            receipt_fixture,
            epoch2_selection_ns=selection_ns,
            epoch2_command_ns_by_replica=command_times,
        )


def test_v39_live_chronology_accepts_all_gate_boundaries_at_target_plus_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_fixture = _v39_repair_receipt_fixture(monkeypatch, tmp_path)
    receipt = receipt_fixture[2]
    gate_target_ns = int(receipt["fault_window_end_ns"]) + 5_000_000_000

    _validate_v39_receipt_bound_live_chronology(
        monkeypatch,
        receipt_fixture,
        epoch2_selection_ns=gate_target_ns + 1,
        epoch2_command_ns_by_replica={
            replica_id: gate_target_ns + 1 for replica_id in range(31)
        },
    )


@pytest.mark.parametrize(
    "mutation,reason",
    _EXCLUDED_REPAIR_FAIL_CLOSED_MUTATIONS,
)
def test_v39_receipt_bound_live_chronology_preserves_full_fail_closed_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    receipt_fixture = _v39_repair_receipt_fixture(monkeypatch, tmp_path)
    receipt = receipt_fixture[2]
    fault_end_ns = int(receipt["fault_window_end_ns"])
    manager_argv = receipt["manager_argv"]
    option_index = manager_argv.index(CYCLE1_SELECTION_NOT_BEFORE_OPTION)
    gate_target_ns = int(manager_argv[option_index + 1])
    document, arguments = _v39_receipt_bound_live_chronology_fixture(
        monkeypatch,
        receipt_fixture,
        epoch2_selection_ns=gate_target_ns + 1,
        epoch2_command_ns_by_replica={
            replica_id: gate_target_ns + 20 + replica_id
            for replica_id in range(31)
        },
    )
    validation._validate_v28_excluded_repair_observation(document, **arguments)

    observation = document["excluded_repair_observation"]
    rows = {row["name"]: row for row in observation["rows"]}

    def replace_manager_event(
        index: int,
        monotonic_ns: int,
    ) -> validation._NativeEvent:
        manager_events = list(arguments["manager_events"])
        updated = replace(manager_events[index], monotonic_ns=monotonic_ns)
        manager_events[index] = updated
        arguments["manager_events"] = tuple(manager_events)
        events_by_ref = dict(arguments["events_by_ref"])
        events_by_ref[(updated.relative_path, updated.source_sequence)] = updated
        arguments["events_by_ref"] = events_by_ref
        return updated

    def replace_replica_event(
        replica_id: int,
        *,
        event_type: str,
        successor_epoch_number: int | None,
        monotonic_ns: int,
    ) -> validation._NativeEvent:
        replica_events = dict(arguments["replica_events"])
        stream = list(replica_events[replica_id])
        index = next(
            index
            for index, event in enumerate(stream)
            if event.event_type == event_type
            and (
                successor_epoch_number is None
                or event.payload.get("successor_epoch_number")
                == successor_epoch_number
            )
        )
        updated = replace(stream[index], monotonic_ns=monotonic_ns)
        stream[index] = updated
        replica_events[replica_id] = tuple(stream)
        arguments["replica_events"] = replica_events
        events_by_ref = dict(arguments["events_by_ref"])
        events_by_ref[(updated.relative_path, updated.source_sequence)] = updated
        arguments["events_by_ref"] = events_by_ref
        return updated

    def update_native_row(name: str, event: validation._NativeEvent) -> None:
        rows[name]["monotonic_ns"] = event.monotonic_ns
        rows[name]["event"] = validation._native_event_reference(event)

    if mutation == "wrong-contract":
        observation["observation_contract"] += "-drift"
    elif mutation == "extra-row":
        observation["rows"].append(copy.deepcopy(observation["rows"][-1]))
    elif mutation == "wrong-reference":
        rows["epoch1_selection"]["event"]["line_sha256"] = "ff" * 32
    elif mutation == "wrong-derivation":
        rows["epoch1_stable_end"]["derivation"] = "phase.drift"
    elif mutation == "epoch1-at-fault-end":
        terminal = replace_manager_event(1, fault_end_ns)
        update_native_row("epoch1_terminal", terminal)
    elif mutation == "epoch1-stable-at-fault-end":
        phase_windows = dict(arguments["phase_windows"])
        start, _, count = phase_windows["epoch1_stable"]
        phase_windows["epoch1_stable"] = (start, fault_end_ns, count)
        arguments["phase_windows"] = phase_windows
        rows["epoch1_stable_end"]["monotonic_ns"] = fault_end_ns
    elif mutation == "epoch2-selection-at-fault-end":
        replace_manager_event(2, fault_end_ns - 1)
        selection = replace_manager_event(3, fault_end_ns)
        update_native_row("epoch2_selection", selection)
    elif mutation in {
        "one-epoch2-command-before-fault",
        "one-epoch2-command-at-fault-end",
    }:
        replace_replica_event(
            0,
            event_type="epoch.command_committed",
            successor_epoch_number=2,
            monotonic_ns=(
                fault_end_ns
                if mutation == "one-epoch2-command-at-fault-end"
                else fault_end_ns - 1
            ),
        )
    elif mutation == "epoch2-stable-at-hard-deadline":
        hard_deadline_ns = int(arguments["hard_deadline_ns"])
        phase_windows = dict(arguments["phase_windows"])
        start, _, count = phase_windows["epoch2_stable"]
        phase_windows["epoch2_stable"] = (start, hard_deadline_ns, count)
        arguments["phase_windows"] = phase_windows
        rows["epoch2_stable_end"]["monotonic_ns"] = hard_deadline_ns
    elif mutation == "epoch2-at-hard-deadline":
        hard_deadline_ns = int(arguments["hard_deadline_ns"])
        drain = replace_replica_event(
            0,
            event_type="block.committed",
            successor_epoch_number=None,
            monotonic_ns=hard_deadline_ns,
        )
        update_native_row("epoch2_drain_complete", drain)
        cutoff_times = dict(arguments["cutoff_times"])
        cutoff_times["epoch2_drain_complete"] = hard_deadline_ns
        arguments["cutoff_times"] = cutoff_times
    else:
        replica_events = dict(arguments["replica_events"])
        replica_events[30] = tuple(
            event
            for event in replica_events[30]
            if not (
                event.event_type == "epoch.command_committed"
                and event.payload.get("successor_epoch_number") == 1
            )
        )
        arguments["replica_events"] = replica_events

    with pytest.raises(FactorialValidationError, match=reason):
        validation._validate_v28_excluded_repair_observation(document, **arguments)


def test_smoke_slot_authorization_must_match_the_claimed_root_envelope(
    tmp_path: Path,
) -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    runtime_bytes = execution._canonical_json_bytes(smoke.runtime.as_document())
    static_artifacts = {
        validation.MANIFEST_FILENAME: MANIFEST_PATH.read_bytes(),
        validation.PLAN_FILENAME: plan.canonical_bytes,
        validation.RUNTIME_FILENAME: runtime_bytes,
    }
    authorization = execution.build_execution_authorization_receipt(
        scope="excluded_n7_smoke",
        approval_reference="test thesis-author approval",
        approved_utc="2026-08-04T00:00:00+00:00",
        kauri_revision="ab" * 20,
        slot_ids=(smoke.slot.slot_id,),
        result_root=f"{manifest.results_root}-smoke",
        static_artifacts=static_artifacts,
        build_provenance_sha256="cd" * 32,
    )
    result_root = tmp_path / "smoke-root"
    slot_root = result_root / smoke.slot.slot_id
    slot_root.mkdir(parents=True)
    (slot_root / validation.AUTHORIZATION_FILENAME).write_bytes(authorization)
    root_authorization = result_root / validation.SMOKE_AUTHORIZATION_FILENAME
    root_authorization.write_bytes(authorization)
    expected = replace(
        validation._expected_slots(manifest)[0],
        slot_id=smoke.slot.slot_id,
    )

    document, payload = validation._validate_execution_authorization(
        slot_root,
        manifest=manifest,
        expected=expected,
        plan=json.loads(plan.canonical_bytes),
        runtime_sha256=hashlib.sha256(runtime_bytes).hexdigest(),
        campaign_member=False,
    )
    assert payload == authorization
    assert document["scope"] == "excluded_n7_smoke"

    root_authorization.write_bytes(authorization + b" ")
    with pytest.raises(FactorialValidationError, match="smoke root authorization"):
        validation._validate_execution_authorization(
            slot_root,
            manifest=manifest,
            expected=expected,
            plan=json.loads(plan.canonical_bytes),
            runtime_sha256=hashlib.sha256(runtime_bytes).hexdigest(),
            campaign_member=False,
        )


def test_n31_coverage_smoke_exclusion_depends_on_the_exact_parent_root() -> None:
    campaign = (
        Path("results/shape-placement-factorial-v19")
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    coverage = (
        Path(validation.EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    v34_coverage = (
        Path(validation.V34_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    v35_coverage = (
        Path(validation.V35_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    v36_coverage = (
        Path(validation.V36_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    v37_coverage = (
        Path(validation.V37_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    v38_coverage = (
        Path(validation.V38_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    v29_coverage = (
        Path(validation.V29_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    v15_coverage = (
        Path(validation.V15_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    v16_coverage = (
        Path(validation.V16_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    v17_coverage = (
        Path(validation.V17_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    v18_coverage = (
        Path(validation.V18_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )

    assert not validation._is_excluded_coverage_smoke_slot(campaign)
    assert validation._is_excluded_coverage_smoke_slot(coverage)
    assert validation._is_excluded_coverage_smoke_slot(
        v38_coverage,
        manifest_id=validation.V38_MANIFEST_ID,
    )
    assert not validation._is_excluded_coverage_smoke_slot(
        v38_coverage,
        manifest_id=validation.FROZEN_MANIFEST_ID,
    )
    assert validation._is_excluded_coverage_smoke_slot(
        v34_coverage,
        manifest_id=validation.V34_MANIFEST_ID,
    )
    assert not validation._is_excluded_coverage_smoke_slot(
        v34_coverage,
        manifest_id=validation.FROZEN_MANIFEST_ID,
    )
    assert validation._is_excluded_coverage_smoke_slot(
        v35_coverage,
        manifest_id=validation.V35_MANIFEST_ID,
    )
    assert not validation._is_excluded_coverage_smoke_slot(
        v35_coverage,
        manifest_id=validation.FROZEN_MANIFEST_ID,
    )
    assert validation._is_excluded_coverage_smoke_slot(
        v36_coverage,
        manifest_id=validation.V36_MANIFEST_ID,
    )
    assert not validation._is_excluded_coverage_smoke_slot(
        v36_coverage,
        manifest_id=validation.FROZEN_MANIFEST_ID,
    )
    assert validation._is_excluded_coverage_smoke_slot(
        v37_coverage,
        manifest_id=validation.V37_MANIFEST_ID,
    )
    assert not validation._is_excluded_coverage_smoke_slot(
        v37_coverage,
        manifest_id=validation.FROZEN_MANIFEST_ID,
    )
    assert validation._is_excluded_coverage_smoke_slot(
        v29_coverage,
        manifest_id=validation.V29_MANIFEST_ID,
    )
    assert not validation._is_excluded_coverage_smoke_slot(
        v29_coverage,
        manifest_id=validation.FROZEN_MANIFEST_ID,
    )
    assert validation._is_excluded_coverage_smoke_slot(
        v15_coverage,
        manifest_id=validation.V15_MANIFEST_ID,
    )
    assert validation._is_excluded_coverage_smoke_slot(
        v16_coverage,
        manifest_id=validation.V16_MANIFEST_ID,
    )
    assert validation._is_excluded_coverage_smoke_slot(
        v17_coverage,
        manifest_id=validation.V17_MANIFEST_ID,
    )
    assert validation._is_excluded_coverage_smoke_slot(
        v18_coverage,
        manifest_id=validation.V18_MANIFEST_ID,
    )
    assert not validation._is_excluded_coverage_smoke_slot(
        v15_coverage,
        manifest_id=validation.FROZEN_MANIFEST_ID,
    )
    assert not validation._is_excluded_coverage_smoke_slot(
        v16_coverage,
        manifest_id=validation.FROZEN_MANIFEST_ID,
    )
    assert not validation._is_excluded_coverage_smoke_slot(
        v17_coverage,
        manifest_id=validation.FROZEN_MANIFEST_ID,
    )
    assert not validation._is_excluded_coverage_smoke_slot(
        v18_coverage,
        manifest_id=validation.FROZEN_MANIFEST_ID,
    )
    assert not validation._is_excluded_coverage_smoke_slot(
        coverage,
        manifest_id=validation.V14_MANIFEST_ID,
    )


def test_v25_coverage_smoke_slot_order_is_exact_and_v24_is_preserved() -> None:
    assert validation._coverage_smoke_slot_ids(validation.V24_MANIFEST_ID) == (
        "slot-066-n31-f5-b05-P",
    )
    assert validation._coverage_smoke_slot_ids(validation.V25_MANIFEST_ID) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
    assert validation._coverage_smoke_slot_ids(validation.V26_MANIFEST_ID) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
    assert validation._coverage_smoke_slot_ids(validation.V29_MANIFEST_ID) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
    assert validation._coverage_smoke_slot_ids(validation.V30_MANIFEST_ID) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
    assert validation._coverage_smoke_slot_ids(validation.V32_MANIFEST_ID) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
    assert validation._coverage_smoke_slot_ids(validation.V34_MANIFEST_ID) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
    assert validation._coverage_smoke_slot_ids(validation.V35_MANIFEST_ID) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
    assert validation._coverage_smoke_slot_ids(validation.V36_MANIFEST_ID) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
    assert validation._coverage_smoke_slot_ids(validation.V37_MANIFEST_ID) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
    assert validation._coverage_smoke_slot_ids(validation.V38_MANIFEST_ID) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
    assert validation._coverage_smoke_slot_ids(validation.FROZEN_MANIFEST_ID) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )

    v25_root = Path(validation.V25_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
    assert validation._is_excluded_coverage_smoke_slot(
        v25_root / "slot-066-n31-f5-b05-P",
        manifest_id=validation.V25_MANIFEST_ID,
    )
    assert validation._is_excluded_coverage_smoke_slot(
        v25_root / "slot-037-n31-f2-b04-00",
        manifest_id=validation.V25_MANIFEST_ID,
    )
    assert not validation._is_excluded_coverage_smoke_slot(
        Path(validation.V24_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT)
        / "slot-037-n31-f2-b04-00",
        manifest_id=validation.V24_MANIFEST_ID,
    )


@pytest.mark.parametrize(
    "result_root",
    (
        validation.V25_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT,
        validation.V26_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT,
        validation.V29_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT,
        validation.V32_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT,
        validation.V35_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT,
        validation.V36_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT,
        validation.V38_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT,
        validation.EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT,
    ),
)
def test_v25_plus_coverage_membership_is_preserved_before_static_load(
    tmp_path: Path,
    result_root: str,
) -> None:
    slot_root = (
        tmp_path
        / Path(result_root).name
        / validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    slot_root.mkdir(parents=True)

    result = validation.validate_slot(slot_root)

    assert result.outcome == "INCOMPLETE"
    assert result.campaign_member is False


def _v25_coverage_runtime_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: int = 25,
) -> tuple[
    object,
    dict[str, object],
    dict[str, validation._ExpectedSlot],
]:
    manifest = {
        25: _v25_candidate_manifest,
        26: _v26_candidate_manifest,
        27: _v27_candidate_manifest,
        28: _v28_candidate_manifest,
        29: _v29_candidate_manifest,
        30: _v30_candidate_manifest,
        31: _v31_candidate_manifest,
        32: _v32_candidate_manifest,
        33: _v33_candidate_manifest,
        34: _v34_candidate_manifest,
        35: _v35_candidate_manifest,
        36: _v36_candidate_manifest,
        37: _v37_candidate_manifest,
        38: _v38_candidate_manifest,
        39: _v39_candidate_manifest,
    }[version](monkeypatch)
    plan = build_factorial_plan(manifest)
    primary = next(
        slot for slot in plan.slots if slot.slot_id == "slot-066-n31-f5-b05-P"
    )
    repair = next(
        slot for slot in plan.slots if slot.slot_id == "slot-037-n31-f2-b04-00"
    )
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    runtime = json.loads(json.dumps(coverage.runtime.as_document()))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    return manifest, runtime, expected_by_id


@pytest.mark.parametrize(
    "version", (28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39)
)
def test_v28_through_v39_repair_observation_dispatch_preserves_version_parity(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
) -> None:
    manifest, _runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=version,
    )
    primary_id, repair_id = validation._coverage_smoke_slot_ids(
        manifest.manifest_id
    )

    assert validation._uses_excluded_repair_observation(
        manifest_id=manifest.manifest_id,
        coverage_smoke=True,
        slot_id=repair_id,
    )
    assert not validation._uses_excluded_repair_observation(
        manifest_id=manifest.manifest_id,
        coverage_smoke=False,
        slot_id=repair_id,
    )
    assert not validation._uses_excluded_repair_observation(
        manifest_id=manifest.manifest_id,
        coverage_smoke=True,
        slot_id=primary_id,
    )
    expected_contract = (
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
        if version == 39
        else (
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
            if version in {37, 38}
            else (
                EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
                if version in {34, 35, 36}
                else EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT
            )
        )
    )
    assert validation._expected_excluded_repair_observation_contract(
        manifest.manifest_id
    ) == expected_contract
    assert expected_by_id[repair_id].slot_id == repair_id


@pytest.mark.parametrize("version", (38, 39))
def test_v38_history_and_v39_repair_runtime_gate_scope_are_exact(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
) -> None:
    manifest, coverage_runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=version,
    )
    validated = validation._validate_v25_coverage_runtime_document(
        coverage_runtime,
        manifest=manifest,
        expected_by_id=expected_by_id,
    )
    primary_id, repair_id = validation._coverage_smoke_slot_ids(
        manifest.manifest_id
    )
    primary = validated[primary_id]
    repair = validated[repair_id]
    primary_argv = primary["manager_argv_template"]["argv"]
    repair_argv = repair["manager_argv_template"]["argv"]
    probe = repair["excluded_repair_smoke_probe"]

    assert CYCLE1_SELECTION_NOT_BEFORE_OPTION not in primary_argv
    if version == 38:
        assert CYCLE1_SELECTION_NOT_BEFORE_OPTION not in repair_argv
        assert probe["semantic_delta"] == EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2
        assert (
            probe["observation_contract"]
            == EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
        )
    else:
        assert repair_argv.count(CYCLE1_SELECTION_NOT_BEFORE_OPTION) == 1
        option_index = repair_argv.index(CYCLE1_SELECTION_NOT_BEFORE_OPTION)
        assert repair_argv[option_index + 1] == CYCLE1_SELECTION_NOT_BEFORE_TOKEN
        assert repair_argv.count(CYCLE1_SELECTION_NOT_BEFORE_TOKEN) == 1
        assert probe["semantic_delta"] == EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V3
        assert (
            probe["observation_contract"]
            == EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
        )

    plan = build_factorial_plan(manifest)
    campaign = json.loads(canonical_runtime_bytes(build_factorial_runtime(plan)))
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0]).runtime.as_document()
    assert all(
        CYCLE1_SELECTION_NOT_BEFORE_OPTION
        not in slot["manager_argv_template"]["argv"]
        for slot in campaign["slots"]
    )
    assert (
        CYCLE1_SELECTION_NOT_BEFORE_OPTION
        not in smoke["manager_argv_template"]["argv"]
    )


@pytest.mark.parametrize(
    ("version", "mutation"),
    (
        (38, "gate"),
        (38, "v4"),
        (38, "delta-v3"),
        (39, "missing-gate"),
        (39, "v3"),
        (39, "delta-v2"),
    ),
)
def test_v38_v39_repair_runtime_gate_and_contracts_are_cross_version_closed(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    mutation: str,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=version,
    )
    repair_id = validation._coverage_smoke_slot_ids(manifest.manifest_id)[1]
    repair = next(slot for slot in runtime["slots"] if slot["slot_id"] == repair_id)
    argv = repair["manager_argv_template"]["argv"]
    probe = repair["excluded_repair_smoke_probe"]
    if mutation == "gate":
        argv.extend(
            (
                CYCLE1_SELECTION_NOT_BEFORE_OPTION,
                CYCLE1_SELECTION_NOT_BEFORE_TOKEN,
            )
        )
    elif mutation == "v4":
        probe["observation_contract"] = EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
        repair["causal_acceptance"][
            "excluded_repair_smoke_observation_contract"
        ] = EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
    elif mutation == "delta-v3":
        probe["semantic_delta"] = EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V3
    elif mutation == "missing-gate":
        index = argv.index(CYCLE1_SELECTION_NOT_BEFORE_OPTION)
        del argv[index : index + 2]
    elif mutation == "v3":
        probe["observation_contract"] = EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
        repair["causal_acceptance"][
            "excluded_repair_smoke_observation_contract"
        ] = EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
    else:
        probe["semantic_delta"] = EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2

    with pytest.raises(FactorialValidationError):
        validation._validate_v25_coverage_runtime_document(
            runtime,
            manifest=manifest,
            expected_by_id=expected_by_id,
        )


def test_v39_repair_manager_materialization_binds_exact_fault_end_plus_grace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _v39_candidate_manifest(monkeypatch)
    plan = build_factorial_plan(manifest)
    primary = next(
        slot for slot in plan.slots if slot.slot_id == "slot-066-n31-f5-b05-P"
    )
    repair = next(
        slot for slot in plan.slots if slot.slot_id == "slot-037-n31-f2-b04-00"
    )
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    spec = next(
        slot for slot in coverage.runtime.slots if slot.slot_id == repair.slot_id
    )
    secrets = ManagerSecretMaterial(
        manager_tls_private_key_der_hex="11" * 32,
        manager_tls_certificate_der_hex="22" * 32,
        issuer_private_key_hex="33" * 32,
        replica_tls_certificate_der_hex=tuple(
            f"{replica_id + 1:064x}" for replica_id in range(spec.replica_count)
        ),
    )
    anchor_ns = 1_000_000_000
    slot_root = tmp_path / "Kauri" / spec.result_path
    argv = materialize_manager_argv(
        spec,
        slot_root,
        secrets,
        shared_raw_clock_anchor_ns=anchor_ns,
    )
    expected_fault_end_ns = anchor_ns + (
        spec.fault_window.start_after_prelaunch_anchor_s
        + spec.fault_window.duration_s
    ) * 1_000_000_000
    expected_gate_ns = expected_fault_end_ns + 5_000_000_000
    hard_deadline_ns = anchor_ns + spec.fault_window.hard_timeout_s * 1_000_000_000

    assert argv.count(CYCLE1_SELECTION_NOT_BEFORE_OPTION) == 1
    option_index = argv.index(CYCLE1_SELECTION_NOT_BEFORE_OPTION)
    assert argv[option_index + 1] == str(expected_gate_ns)
    assert hard_deadline_ns - expected_gate_ns == 165_000_000_000
    assert all(CYCLE1_SELECTION_NOT_BEFORE_TOKEN not in value for value in argv)

    with pytest.raises(
        manifest_module.FactorialManifestError,
        match="cycle-1 selection|uint64|monotonic",
    ):
        materialize_manager_argv(
            spec,
            slot_root,
            secrets,
            shared_raw_clock_anchor_ns=(
                validation._UINT64_MAX
                - spec.fault_window.start_after_prelaunch_anchor_s
                * 1_000_000_000
            ),
        )


def test_v33_through_v39_preserve_all_inherited_semantic_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = (
        _v33_candidate_manifest(monkeypatch),
        _v34_candidate_manifest(monkeypatch),
        _v35_candidate_manifest(monkeypatch),
        _v36_candidate_manifest(monkeypatch),
        _v37_candidate_manifest(monkeypatch),
        _v38_candidate_manifest(monkeypatch),
        _v39_candidate_manifest(monkeypatch),
    )
    predicates = (
        validation._uses_selection_visible_hard_timeout_witnesses,
        validation._uses_selection_visible_responsive_timeout_nonwitnesses,
        validation._uses_source_bound_contribution_opportunities,
        validation._uses_strict_sigint_cleanup,
        validation._uses_precontainment_fault_coverage,
        validation._uses_precontainment_shape_preservation,
        validation._uses_precontainment_guarded_selection_contract,
        validation._uses_future_tree_proposal_delivery_contract,
        validation._uses_source_bound_proposal_witness_contract,
        validation._uses_evidence_snapshot_selection_contract,
        validation._uses_inherited_consensus_wait_exempt_placement_contract,
        validation._uses_verified_response_duplicate_delivery_contract,
    )

    for manifest in manifests:
        assert manifest.manifest_id in validation._CAUSAL_MEASUREMENT_MANIFEST_IDS
        assert validation._v24_preselection_contract(manifest) == (60_000, 82)
        assert all(predicate(manifest) for predicate in predicates)


def test_v36_runtime_rejects_missing_post_final_unmatched_commit_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=36,
    )
    runtime["slots"][0]["causal_acceptance"].pop(
        "post_final_convergence_unmatched_commit_evidence_contract"
    )

    with pytest.raises(
        FactorialValidationError,
        match="causal acceptance contract",
    ):
        validation._validate_v25_coverage_runtime_document(
            runtime,
            manifest=manifest,
            expected_by_id=expected_by_id,
        )


@pytest.mark.parametrize("version", (34, 35, 36, 37, 38, 39))
def test_v34_plus_fault_active_phase_contract_dispatches_only_exact_repair_runtime(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
) -> None:
    manifest, coverage_runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=version,
    )
    validated = validation._validate_v25_coverage_runtime_document(
        coverage_runtime,
        manifest=manifest,
        expected_by_id=expected_by_id,
    )
    primary_id, repair_id = validation._coverage_smoke_slot_ids(
        manifest.manifest_id
    )
    expected_contract = (
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
        if version == 39
        else (
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
            if version in {37, 38}
            else EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
        )
    )
    assert validation._v34_excluded_repair_fault_active_phase_contract(
        manifest=manifest,
        expected=expected_by_id[repair_id],
        runtime=validated[repair_id],
        coverage_smoke=True,
        validated_observation_contract=expected_contract,
    ) == expected_contract

    plan = build_factorial_plan(manifest)
    campaign_runtime = json.loads(
        canonical_runtime_bytes(build_factorial_runtime(plan))
    )
    assert manifest.common_timers.aggregation_timeout_ms_per_depth == 125
    campaign_repair = next(
        slot for slot in campaign_runtime["slots"] if slot["slot_id"] == repair_id
    )
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    assert validation._v34_excluded_repair_fault_active_phase_contract(
        manifest=manifest,
        expected=expected_by_id[repair_id],
        runtime=campaign_repair,
        coverage_smoke=False,
        validated_observation_contract=None,
    ) is None
    assert validation._v34_excluded_repair_fault_active_phase_contract(
        manifest=manifest,
        expected=expected_by_id[primary_id],
        runtime=validated[primary_id],
        coverage_smoke=True,
        validated_observation_contract=None,
    ) is None
    assert validation._v34_excluded_repair_fault_active_phase_contract(
        manifest=manifest,
        expected=validation._expected_excluded_smoke(manifest),
        runtime=smoke.runtime.as_document(),
        coverage_smoke=False,
        validated_observation_contract=None,
    ) is None

    v33_manifest, v33_runtime, v33_expected = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=33,
    )
    _, v33_repair_id = validation._coverage_smoke_slot_ids(
        v33_manifest.manifest_id
    )
    assert validation._v34_excluded_repair_fault_active_phase_contract(
        manifest=v33_manifest,
        expected=v33_expected[v33_repair_id],
        runtime=next(
            slot
            for slot in v33_runtime["slots"]
            if slot["slot_id"] == v33_repair_id
        ),
        coverage_smoke=True,
        validated_observation_contract=(
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT
        ),
    ) is None


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-validated-observation",
        "probe-observation",
        "runtime-observation",
        "duration",
        "result-path",
    ),
)
@pytest.mark.parametrize("version", (34, 35, 36, 37, 38, 39))
def test_v34_plus_fault_active_phase_contract_rejects_any_repair_binding_drift(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    version: int,
) -> None:
    manifest, coverage_runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=version,
    )
    repair_id = validation._coverage_smoke_slot_ids(manifest.manifest_id)[1]
    repair = copy.deepcopy(
        next(
            slot
            for slot in coverage_runtime["slots"]
            if slot["slot_id"] == repair_id
        )
    )
    expected_contract = (
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
        if version == 39
        else (
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
            if version in {37, 38}
            else EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
        )
    )
    drift_contract = (
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
        if version == 39
        else (
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
            if version in {37, 38}
            else EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT
        )
    )
    validated_contract = expected_contract
    if mutation == "missing-validated-observation":
        validated_contract = None
    elif mutation == "probe-observation":
        repair["excluded_repair_smoke_probe"]["observation_contract"] = (
            drift_contract
        )
    elif mutation == "runtime-observation":
        repair["causal_acceptance"][
            "excluded_repair_smoke_observation_contract"
        ] = drift_contract
    elif mutation == "duration":
        repair["fault_window"]["duration_s"] = 300 if version == 37 else 450
    else:
        repair["result_path"] = (
            f"results/shape-placement-factorial-v{version}/"
            "slot-037-n31-f2-b04-00"
        )

    with pytest.raises(FactorialValidationError, match="scope binding drifted"):
        validation._v34_excluded_repair_fault_active_phase_contract(
            manifest=manifest,
            expected=expected_by_id[repair_id],
            runtime=repair,
            coverage_smoke=True,
            validated_observation_contract=validated_contract,
        )


def test_v25_coverage_runtime_binds_exact_order_and_stop_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(monkeypatch)
    validated = validation._validate_v25_coverage_runtime_document(
        runtime,
        manifest=manifest,
        expected_by_id=expected_by_id,
    )

    assert tuple(validated) == validation.V25_EXCLUDED_COVERAGE_SMOKE_SLOT_IDS
    assert runtime["automatic_retries"] == 0
    assert runtime["replacement_policy"] == "none"
    assert runtime["stop_on_first_non_pass"] is True
    assert runtime["minimum_free_bytes"] == 10_000_000_000


def test_v26_coverage_runtime_inherits_exact_two_slot_lifecycle_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=26,
    )
    validated = validation._validate_v25_coverage_runtime_document(
        runtime,
        manifest=manifest,
        expected_by_id=expected_by_id,
    )

    assert tuple(validated) == validation.V26_EXCLUDED_COVERAGE_SMOKE_SLOT_IDS
    assert runtime["runtime_id"] == (
        "shape-placement-factorial-v26-excluded-n31-coverage-smoke-v1"
    )
    assert runtime["automatic_retries"] == 0
    assert runtime["replacement_policy"] == "none"
    assert runtime["stop_on_first_non_pass"] is True
    assert all(
        slot["causal_acceptance"][
            "verified_response_duplicate_delivery_contract"
        ]
        == VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT
        for slot in runtime["slots"]
    )


def test_v27_coverage_runtime_binds_v2_timing_and_ordered_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=27,
    )
    validated = validation._validate_v25_coverage_runtime_document(
        runtime,
        manifest=manifest,
        expected_by_id=expected_by_id,
    )

    assert tuple(validated) == validation.V27_EXCLUDED_COVERAGE_SMOKE_SLOT_IDS
    assert runtime["runtime_id"] == (
        "shape-placement-factorial-v27-excluded-n31-coverage-smoke-v1"
    )
    assert runtime["automatic_retries"] == 0
    assert runtime["replacement_policy"] == "none"
    assert runtime["stop_on_first_non_pass"] is True
    assert all(
        slot["fault_window"]["duration_s"] == 450
        and slot["fault_window"]["hard_timeout_s"] == 650
        and slot["causal_acceptance"][
            "verified_response_duplicate_delivery_contract"
        ]
        == VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2
        for slot in runtime["slots"]
    )


def test_v33_profile_runtime_slot_and_argv_bind_exact_deadline_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=33,
    )
    validation._validate_v33_static_timing_contract(manifest)
    validated = validation._validate_v25_coverage_runtime_document(
        runtime,
        manifest=manifest,
        expected_by_id=expected_by_id,
    )

    assert manifest.common_timers.transition_convergence_deadline_s == 30
    assert manifest.common_timers.leader_progress_timeout_ms == 20_000
    assert manifest.byzantine.duration_s == 450
    assert manifest.common_timers.hard_timeout_s == 650
    primary_id, repair_id = validation._coverage_smoke_slot_ids(
        manifest.manifest_id
    )
    assert validated[primary_id]["fault_window"] == {
        **validated[primary_id]["fault_window"],
        "duration_s": 450,
        "transition_convergence_deadline_s": 30,
        "hard_timeout_s": 650,
    }
    assert validated[repair_id]["fault_window"]["duration_s"] == 300
    for slot in validated.values():
        argv = slot["manager_argv_template"]["argv"]
        index = argv.index("--convergence-deadline-seconds")
        assert argv[index + 1] == "30"
        assert "leader-progress-timeout = 20" in slot["main_config"]["lines"]


@pytest.mark.parametrize(
    "mutation",
    ("deadline", "leader-timeout", "duration", "hard-timeout"),
)
def test_v33_profile_timing_contract_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    manifest = _v33_candidate_manifest(monkeypatch)
    timers = manifest.common_timers
    byzantine = manifest.byzantine
    if mutation == "deadline":
        timers = replace(timers, transition_convergence_deadline_s=29)
    elif mutation == "leader-timeout":
        timers = replace(timers, leader_progress_timeout_ms_per_depth=4_999)
    elif mutation == "duration":
        byzantine = replace(byzantine, duration_s=449)
    else:
        timers = replace(timers, hard_timeout_s=649)

    with pytest.raises(FactorialValidationError, match="v33 exact 30s"):
        validation._validate_v33_static_timing_contract(
            replace(manifest, common_timers=timers, byzantine=byzantine)
        )


@pytest.mark.parametrize("version", (37, 38, 39))
def test_v37_through_v39_static_timing_preserves_125ms_aggregation_per_depth(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
) -> None:
    manifest = {
        37: _v37_candidate_manifest,
        38: _v38_candidate_manifest,
        39: _v39_candidate_manifest,
    }[version](monkeypatch)
    validation._validate_v33_static_timing_contract(manifest)

    with pytest.raises(FactorialValidationError, match="v37 exact 125ms"):
        validation._validate_v33_static_timing_contract(
            replace(
                manifest,
                common_timers=replace(
                    manifest.common_timers,
                    aggregation_timeout_ms_per_depth=126,
                ),
            )
        )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("fault-window-deadline", "fault-window"),
        ("manager-argv-deadline", "exact 30s convergence deadline"),
        ("leader-timeout", "main configuration/timers"),
        ("duration", "fault-window"),
        ("hard-timeout", "fault-window"),
    ),
)
def test_v33_runtime_slot_rejects_any_deadline_or_schedule_drift(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    reason: str,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=33,
    )
    primary = runtime["slots"][0]
    if mutation == "fault-window-deadline":
        primary["fault_window"]["transition_convergence_deadline_s"] = 29
    elif mutation == "manager-argv-deadline":
        argv = primary["manager_argv_template"]["argv"]
        argv[argv.index("--convergence-deadline-seconds") + 1] = "29"
    elif mutation == "leader-timeout":
        lines = primary["main_config"]["lines"]
        lines[lines.index("leader-progress-timeout = 20")] = (
            "leader-progress-timeout = 19"
        )
    elif mutation == "duration":
        primary["fault_window"]["duration_s"] = 449
    else:
        primary["fault_window"]["hard_timeout_s"] = 649

    with pytest.raises(FactorialValidationError, match=reason):
        validation._validate_v25_coverage_runtime_document(
            runtime,
            manifest=manifest,
            expected_by_id=expected_by_id,
        )


@pytest.mark.parametrize(
    ("version", "manifest_id", "source_result_path"),
    (
        (
            28,
            validation.V28_MANIFEST_ID,
            "results/shape-placement-factorial-v28/slot-037-n31-f2-b04-00",
        ),
        (
            29,
            validation.V29_MANIFEST_ID,
            "results/shape-placement-factorial-v29/slot-037-n31-f2-b04-00",
        ),
        (
            30,
            validation.V30_MANIFEST_ID,
            "results/shape-placement-factorial-v30/slot-037-n31-f2-b04-00",
        ),
        (
            31,
            validation.V31_MANIFEST_ID,
            "results/shape-placement-factorial-v31/slot-037-n31-f2-b04-00",
        ),
        (
            32,
            validation.V32_MANIFEST_ID,
            "results/shape-placement-factorial-v32/slot-037-n31-f2-b04-00",
        ),
        (
            33,
            validation.V33_MANIFEST_ID,
            "results/shape-placement-factorial-v33/slot-037-n31-f2-b04-00",
        ),
        (
            34,
            validation.V34_MANIFEST_ID,
            "results/shape-placement-factorial-v34/slot-037-n31-f2-b04-00",
        ),
        (
            35,
            validation.V35_MANIFEST_ID,
            "results/shape-placement-factorial-v35/slot-037-n31-f2-b04-00",
        ),
        (
            36,
            validation.V36_MANIFEST_ID,
            "results/shape-placement-factorial-v36/slot-037-n31-f2-b04-00",
        ),
        (
            37,
            validation.V37_MANIFEST_ID,
            "results/shape-placement-factorial-v37/slot-037-n31-f2-b04-00",
        ),
        (
            38,
            validation.V38_MANIFEST_ID,
            "results/shape-placement-factorial-v38/slot-037-n31-f2-b04-00",
        ),
        (
            39,
            validation.FROZEN_MANIFEST_ID,
            "results/shape-placement-factorial-v39/slot-037-n31-f2-b04-00",
        ),
    ),
)
def test_v28_through_v39_campaign_and_exact_repair_runtime_are_independently_bound(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    manifest_id: str,
    source_result_path: str,
) -> None:
    manifest, coverage_runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=version,
    )
    plan = build_factorial_plan(manifest)
    campaign_runtime = json.loads(
        canonical_runtime_bytes(build_factorial_runtime(plan))
    )
    for slot_id in validation._coverage_smoke_slot_ids(manifest_id):
        campaign_slot = next(
            slot for slot in campaign_runtime["slots"] if slot["slot_id"] == slot_id
        )
        validation._validate_runtime_slot(
            campaign_slot,
            expected_by_id[slot_id],
            manifest,
        )
        assert campaign_slot["fault_window"]["duration_s"] == 450
        assert "excluded_repair_smoke_probe" not in campaign_slot
        assert all(
            "--experiment-response-evidence-duplicate-probe" not in row["argv"]
            for row in campaign_slot["replica_argv_templates"]
        )
    n7_runtime = execution.build_n7_ps_smoke_slot(
        plan.slots[0]
    ).runtime.as_document()
    assert n7_runtime["fault_window"]["duration_s"] == 450
    assert "excluded_repair_smoke_probe" not in n7_runtime

    validated = validation._validate_v25_coverage_runtime_document(
        coverage_runtime,
        manifest=manifest,
        expected_by_id=expected_by_id,
    )
    primary_id, repair_id = validation._coverage_smoke_slot_ids(manifest_id)
    assert primary_id == "slot-066-n31-f5-b05-P"
    assert repair_id == "slot-037-n31-f2-b04-00"
    primary = validated[primary_id]
    repair = validated[repair_id]
    probe = repair["excluded_repair_smoke_probe"]
    expected_repair_duration_s = 330 if version in {37, 38, 39} else 300
    assert primary["fault_window"]["duration_s"] == 450
    assert "excluded_repair_smoke_probe" not in primary
    assert repair["fault_window"]["duration_s"] == expected_repair_duration_s
    assert repair["fault_window"]["hard_timeout_s"] == 650
    assert probe == coverage_runtime["excluded_repair_smoke_probe"]
    assert probe["source_campaign_slot_id"] == repair["slot_id"]
    assert probe["source_campaign_result_path"] == source_result_path
    assert probe["semantic_delta"] == (
        EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V3
        if version == 39
        else (
            EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2
            if version in {37, 38}
            else f"byzantine.window.duration_s:450->{expected_repair_duration_s}"
        )
    )
    assert probe["source_fault_window_duration_s"] == 450
    assert probe["effective_fault_window_duration_s"] == (
        expected_repair_duration_s
    )
    assert probe["hard_timeout_s"] == 650
    assert probe["observation_contract"] == (
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
        if version == 39
        else (
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
            if version in {37, 38}
            else (
                EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
                if version in {34, 35, 36}
                else EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT
            )
        )
    )
    assert probe["verified_response_duplicate_probe_contract"] == (
        EXCLUDED_REPAIR_SMOKE_VERIFIED_RESPONSE_DUPLICATE_PROBE_CONTRACT
    )
    assert all(
        row["argv"].count("--experiment-response-evidence-duplicate-probe") == 1
        and row["argv"].count(RESPONSE_DUPLICATE_PROBE_MODE) == 1
        for row in repair["replica_argv_templates"]
    )


@pytest.mark.parametrize(
    "version",
    (28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39),
)
@pytest.mark.parametrize(
    "mutation,reason",
    (
        ("missing-top-probe", "invalid field set"),
        ("wrong-source-artifact", "probe runtime contract"),
        ("wrong-delta", "probe runtime contract"),
        ("wrong-duration", "probe runtime contract|fault-window"),
        ("missing-repair-probe", "probe runtime contract"),
        ("missing-probe-argv", "omission actor/cap contract"),
        ("primary-probe-argv", "omission actor/cap contract"),
        ("missing-causal-contract", "causal acceptance contract"),
    ),
)
def test_v28_through_v39_exact_repair_runtime_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    mutation: str,
    reason: str,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=version,
    )
    primary, repair = runtime["slots"]
    if mutation == "missing-top-probe":
        runtime.pop("excluded_repair_smoke_probe")
    elif mutation == "wrong-source-artifact":
        repair["excluded_repair_smoke_probe"]["source_campaign_artifact_id"] = (
            "slot-runtime-" + "0" * 24
        )
    elif mutation == "wrong-delta":
        repair["excluded_repair_smoke_probe"]["semantic_delta"] = "duration-drift"
    elif mutation == "wrong-duration":
        repair["excluded_repair_smoke_probe"][
            "effective_fault_window_duration_s"
        ] = 331 if version in {37, 38, 39} else 301
    elif mutation == "missing-repair-probe":
        repair.pop("excluded_repair_smoke_probe")
    elif mutation == "missing-probe-argv":
        argv = repair["replica_argv_templates"][0]["argv"]
        index = argv.index("--experiment-response-evidence-duplicate-probe")
        del argv[index : index + 2]
    elif mutation == "primary-probe-argv":
        primary["replica_argv_templates"][0]["argv"].extend(
            [
                "--experiment-response-evidence-duplicate-probe",
                RESPONSE_DUPLICATE_PROBE_MODE,
            ]
        )
    else:
        repair["causal_acceptance"].pop(
            "excluded_repair_smoke_observation_contract"
        )
    with pytest.raises(FactorialValidationError, match=reason):
        validation._validate_v25_coverage_runtime_document(
            runtime,
            manifest=manifest,
            expected_by_id=expected_by_id,
        )


@pytest.mark.parametrize("duration_s", (300, 329, 331, 360))
@pytest.mark.parametrize("version", (37, 38, 39))
def test_v37_through_v39_s037_runtime_reject_every_non_b330_duration(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    duration_s: int,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=version,
    )
    repair = runtime["slots"][1]
    repair["fault_window"]["duration_s"] = duration_s

    with pytest.raises(FactorialValidationError, match="fault-window"):
        validation._validate_v25_coverage_runtime_document(
            runtime,
            manifest=manifest,
            expected_by_id=expected_by_id,
        )


@pytest.mark.parametrize("duration_s", (300, 329, 331, 360))
@pytest.mark.parametrize("version", (37, 38, 39))
def test_v37_through_v39_s037_probe_reject_every_non_b330_duration(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    duration_s: int,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=version,
    )
    probe = runtime["slots"][1]["excluded_repair_smoke_probe"]
    probe["effective_fault_window_duration_s"] = duration_s
    runtime["excluded_repair_smoke_probe"] = copy.deepcopy(probe)

    with pytest.raises(FactorialValidationError, match="probe runtime contract"):
        validation._validate_v25_coverage_runtime_document(
            runtime,
            manifest=manifest,
            expected_by_id=expected_by_id,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_fault_window_duration_s", 449),
        ("semantic_delta", "byzantine.window.duration_s:450->300"),
        ("hard_timeout_s", 649),
        ("observation_contract", EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2),
    ),
)
@pytest.mark.parametrize("version", (37, 38))
def test_v37_v38_s037_probe_rejects_source_delta_hard_or_v3_drift(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    field: str,
    value: object,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=version,
    )
    probe = runtime["slots"][1]["excluded_repair_smoke_probe"]
    probe[field] = value
    runtime["excluded_repair_smoke_probe"] = copy.deepcopy(probe)

    with pytest.raises(FactorialValidationError, match="probe runtime contract"):
        validation._validate_v25_coverage_runtime_document(
            runtime,
            manifest=manifest,
            expected_by_id=expected_by_id,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_fault_window_duration_s", 449),
        ("semantic_delta", EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2),
        ("hard_timeout_s", 649),
        ("observation_contract", EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3),
    ),
)
def test_v39_s037_probe_rejects_source_delta_hard_or_v4_drift(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=39,
    )
    probe = runtime["slots"][1]["excluded_repair_smoke_probe"]
    probe[field] = value
    runtime["excluded_repair_smoke_probe"] = copy.deepcopy(probe)

    with pytest.raises(FactorialValidationError, match="probe runtime contract"):
        validation._validate_v25_coverage_runtime_document(
            runtime,
            manifest=manifest,
            expected_by_id=expected_by_id,
        )


@pytest.mark.parametrize("version", (37, 38))
def test_v37_v38_s037_reject_v2_causal_observation_contract(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=version,
    )
    runtime["slots"][1]["causal_acceptance"][
        "excluded_repair_smoke_observation_contract"
    ] = EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2

    with pytest.raises(FactorialValidationError, match="causal acceptance contract"):
        validation._validate_v25_coverage_runtime_document(
            runtime,
            manifest=manifest,
            expected_by_id=expected_by_id,
        )


@pytest.mark.parametrize(
    ("older_version", "newer_version"),
    ((36, 37), (37, 38), (38, 39)),
)
def test_repair_runtimes_reject_cross_version_validation(
    monkeypatch: pytest.MonkeyPatch,
    older_version: int,
    newer_version: int,
) -> None:
    older_manifest, older_runtime, older_expected = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=older_version,
    )
    newer_manifest, newer_runtime, newer_expected = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=newer_version,
    )
    validation._validate_v25_coverage_runtime_document(
        older_runtime,
        manifest=older_manifest,
        expected_by_id=older_expected,
    )
    validation._validate_v25_coverage_runtime_document(
        newer_runtime,
        manifest=newer_manifest,
        expected_by_id=newer_expected,
    )

    with pytest.raises(FactorialValidationError):
        validation._validate_v25_coverage_runtime_document(
            newer_runtime,
            manifest=older_manifest,
            expected_by_id=older_expected,
        )
    with pytest.raises(FactorialValidationError):
        validation._validate_v25_coverage_runtime_document(
            older_runtime,
            manifest=newer_manifest,
            expected_by_id=newer_expected,
        )


@pytest.mark.parametrize(
    "mutation,reason",
    (
        ("missing-slot", "slot order"),
        ("reversed", "slot order"),
        ("duplicate", "slot order"),
        ("wrong-runtime-id", "sequencing"),
        ("retry", "sequencing"),
        ("replacement", "sequencing"),
        ("continue-after-failure", "sequencing"),
        ("missing-minimum-free", "invalid field set"),
        ("zero-minimum-free", "sequencing"),
        ("wrong-minimum-free", "sequencing"),
        ("wrong-result-path", "result path"),
        ("missing-causal-field", "causal acceptance contract"),
    ),
)
def test_v25_coverage_runtime_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    reason: str,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(monkeypatch)
    slots = runtime["slots"]
    assert isinstance(slots, list)
    if mutation == "missing-slot":
        runtime["slots"] = slots[:1]
    elif mutation == "reversed":
        runtime["slots"] = list(reversed(slots))
    elif mutation == "duplicate":
        runtime["slots"] = [slots[0], slots[0]]
    elif mutation == "wrong-runtime-id":
        runtime["runtime_id"] = f"{runtime['runtime_id']}-drift"
    elif mutation == "retry":
        runtime["automatic_retries"] = 1
    elif mutation == "replacement":
        runtime["replacement_policy"] = "replace"
    elif mutation == "continue-after-failure":
        runtime["stop_on_first_non_pass"] = False
    elif mutation == "missing-minimum-free":
        del runtime["minimum_free_bytes"]
    elif mutation == "zero-minimum-free":
        runtime["minimum_free_bytes"] = 0
    elif mutation == "wrong-minimum-free":
        runtime["minimum_free_bytes"] += 1
    elif mutation == "wrong-result-path":
        slots[1]["result_path"] = slots[0]["result_path"]
    else:
        del slots[1]["causal_acceptance"][
            "inherited_consensus_wait_exempt_placement_contract"
        ]

    with pytest.raises(FactorialValidationError, match=reason):
        validation._validate_v25_coverage_runtime_document(
            runtime,
            manifest=manifest,
            expected_by_id=expected_by_id,
        )


def test_v25_runtime_binds_placement_contract_and_v24_rejects_forgery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, runtime, expected_by_id = _v25_coverage_runtime_fixture(monkeypatch)
    slot = runtime["slots"][1]
    assert slot["causal_acceptance"][
        "inherited_consensus_wait_exempt_placement_contract"
    ] == INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT
    validation._validate_runtime_slot(
        slot,
        expected_by_id["slot-037-n31-f2-b04-00"],
        manifest,
    )

    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    missing_manifest_contract = replace(
        manifest,
        byzantine=replace(
            manifest.byzantine,
            responsive_degradation=replace(
                responsive,
                inherited_consensus_wait_exempt_placement_contract=None,
            ),
        ),
    )
    with pytest.raises(FactorialValidationError, match="placement contract"):
        validation._validate_runtime_slot(
            slot,
            expected_by_id["slot-037-n31-f2-b04-00"],
            missing_manifest_contract,
        )

    v24 = load_frozen_manifest(MANIFEST_PATH)
    v24_slot = build_factorial_runtime(build_factorial_plan(v24)).slots[0]
    v24_document = json.loads(json.dumps(v24_slot.as_document()))
    v24_document["causal_acceptance"][
        "inherited_consensus_wait_exempt_placement_contract"
    ] = INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT
    v24_expected = {
        expected.slot_id: expected for expected in validation._expected_slots(v24)
    }
    with pytest.raises(FactorialValidationError, match="causal acceptance contract"):
        validation._validate_runtime_slot(
            v24_document,
            v24_expected[v24_slot.slot_id],
            v24,
        )


@pytest.mark.parametrize(
    "authorized_slot_ids",
    (
        ("slot-066-n31-f5-b05-P",),
        ("slot-037-n31-f2-b04-00",),
        ("slot-037-n31-f2-b04-00", "slot-066-n31-f5-b05-P"),
        (
            "slot-066-n31-f5-b05-P",
            "slot-037-n31-f2-b04-00",
            "slot-001-n13-f2-b01-00",
        ),
    ),
)
def test_v25_coverage_authorization_rejects_missing_reversed_or_extra_slots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authorized_slot_ids: tuple[str, ...],
) -> None:
    manifest = _v25_candidate_manifest(monkeypatch)
    plan = build_factorial_plan(manifest)
    primary = next(
        slot for slot in plan.slots if slot.slot_id == "slot-066-n31-f5-b05-P"
    )
    repair = next(
        slot for slot in plan.slots if slot.slot_id == "slot-037-n31-f2-b04-00"
    )
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    runtime_bytes = execution._canonical_json_bytes(coverage.runtime.as_document())
    static_artifacts = {
        validation.MANIFEST_FILENAME: V25_MANIFEST_PATH.read_bytes(),
        validation.PLAN_FILENAME: plan.canonical_bytes,
        validation.RUNTIME_FILENAME: runtime_bytes,
    }
    authorization = execution.build_execution_authorization_receipt(
        scope="excluded_n31_coverage_smoke",
        approval_reference="test thesis-author approval",
        approved_utc="2026-08-10T00:00:00+00:00",
        kauri_revision="ab" * 20,
        slot_ids=authorized_slot_ids,
        result_root="results/shape-placement-factorial-v25-coverage-smoke",
        static_artifacts=static_artifacts,
        build_provenance_sha256="cd" * 32,
    )
    monkeypatch.setattr(
        validation,
        "V25_MANIFEST_SHA256",
        hashlib.sha256(static_artifacts[validation.MANIFEST_FILENAME]).hexdigest(),
    )
    monkeypatch.setattr(validation, "V25_PLAN_SHA256", plan.plan_sha256)
    monkeypatch.setattr(
        validation,
        "V25_COVERAGE_SMOKE_RUNTIME_SHA256",
        hashlib.sha256(runtime_bytes).hexdigest(),
    )
    result_root = tmp_path / "shape-placement-factorial-v25-coverage-smoke"
    result_root.mkdir()
    (result_root / validation.COVERAGE_SMOKE_AUTHORIZATION_FILENAME).write_bytes(
        authorization
    )
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    for slot in coverage.slots:
        slot_root = result_root / slot.slot_id
        slot_root.mkdir()
        (slot_root / validation.RUNTIME_FILENAME).write_bytes(runtime_bytes)
        (slot_root / validation.AUTHORIZATION_FILENAME).write_bytes(authorization)
        with pytest.raises(FactorialValidationError, match="authorization receipt"):
            validation._validate_execution_authorization(
                slot_root,
                manifest=manifest,
                expected=expected_by_id[slot.slot_id],
                plan=json.loads(plan.canonical_bytes),
                runtime_sha256=hashlib.sha256(runtime_bytes).hexdigest(),
                campaign_member=False,
            )


def test_v25_coverage_authorization_binds_same_ordered_pair_to_each_slot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _v25_candidate_manifest(monkeypatch)
    plan = build_factorial_plan(manifest)
    primary = next(
        slot for slot in plan.slots if slot.slot_id == "slot-066-n31-f5-b05-P"
    )
    repair = next(
        slot for slot in plan.slots if slot.slot_id == "slot-037-n31-f2-b04-00"
    )
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    runtime_bytes = execution._canonical_json_bytes(coverage.runtime.as_document())
    static_artifacts = {
        validation.MANIFEST_FILENAME: V25_MANIFEST_PATH.read_bytes(),
        validation.PLAN_FILENAME: plan.canonical_bytes,
        validation.RUNTIME_FILENAME: runtime_bytes,
    }
    ordered_ids = tuple(slot.slot_id for slot in coverage.slots)
    authorization = execution.build_execution_authorization_receipt(
        scope="excluded_n31_coverage_smoke",
        approval_reference="test thesis-author approval",
        approved_utc="2026-08-10T00:00:00+00:00",
        kauri_revision="ab" * 20,
        slot_ids=ordered_ids,
        result_root="results/shape-placement-factorial-v25-coverage-smoke",
        static_artifacts=static_artifacts,
        build_provenance_sha256="cd" * 32,
    )
    monkeypatch.setattr(
        validation,
        "V25_MANIFEST_SHA256",
        hashlib.sha256(static_artifacts[validation.MANIFEST_FILENAME]).hexdigest(),
    )
    monkeypatch.setattr(validation, "V25_PLAN_SHA256", plan.plan_sha256)
    monkeypatch.setattr(
        validation,
        "V25_COVERAGE_SMOKE_RUNTIME_SHA256",
        hashlib.sha256(runtime_bytes).hexdigest(),
    )
    result_root = tmp_path / "shape-placement-factorial-v25-coverage-smoke"
    result_root.mkdir()
    (result_root / validation.COVERAGE_SMOKE_AUTHORIZATION_FILENAME).write_bytes(
        authorization
    )
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    for slot in coverage.slots:
        slot_root = result_root / slot.slot_id
        slot_root.mkdir()
        (slot_root / validation.RUNTIME_FILENAME).write_bytes(runtime_bytes)
        (slot_root / validation.AUTHORIZATION_FILENAME).write_bytes(authorization)
        document, _ = validation._validate_execution_authorization(
            slot_root,
            manifest=manifest,
            expected=expected_by_id[slot.slot_id],
            plan=json.loads(plan.canonical_bytes),
            runtime_sha256=hashlib.sha256(runtime_bytes).hexdigest(),
            campaign_member=False,
        )
        assert document["slot_ids"] == list(ordered_ids)


def test_v25_static_loader_extracts_each_exact_constituent_from_shared_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _v25_candidate_manifest(monkeypatch)
    plan = build_factorial_plan(manifest)
    primary = next(
        slot for slot in plan.slots if slot.slot_id == "slot-066-n31-f5-b05-P"
    )
    repair = next(
        slot for slot in plan.slots if slot.slot_id == "slot-037-n31-f2-b04-00"
    )
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    manifest_bytes = V25_MANIFEST_PATH.read_bytes()
    runtime_bytes = execution._canonical_json_bytes(coverage.runtime.as_document())
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    runtime_sha256 = hashlib.sha256(runtime_bytes).hexdigest()
    monkeypatch.setattr(
        manifest_module,
        "V25_MANIFEST_SHA256",
        manifest_sha256,
    )
    monkeypatch.setattr(validation, "V25_MANIFEST_SHA256", manifest_sha256)
    monkeypatch.setattr(validation, "V25_PLAN_SHA256", plan.plan_sha256)
    monkeypatch.setattr(
        validation,
        "V25_COVERAGE_SMOKE_RUNTIME_SHA256",
        runtime_sha256,
    )

    result_root = tmp_path / "shape-placement-factorial-v25-coverage-smoke"
    for slot in coverage.slots:
        slot_root = result_root / slot.slot_id
        slot_root.mkdir(parents=True)
        (slot_root / validation.MANIFEST_FILENAME).write_bytes(manifest_bytes)
        (slot_root / validation.PLAN_FILENAME).write_bytes(plan.canonical_bytes)
        (slot_root / validation.RUNTIME_FILENAME).write_bytes(runtime_bytes)
        loaded_manifest, _, loaded_runtime, expected, loaded_sha256 = (
            validation._load_static_contracts(slot_root)
        )
        assert loaded_manifest.manifest_id == validation.V25_MANIFEST_ID
        assert expected.slot_id == slot.slot_id
        assert loaded_runtime["slot_id"] == slot.slot_id
        assert loaded_sha256 == runtime_sha256


def _pass_coverage_validation(
    expected: validation._ExpectedSlot,
) -> validation.SlotValidationResult:
    return validation.SlotValidationResult(
        slot_id=expected.slot_id,
        outcome="PASS",
        reason=None,
        block_id=expected.block_id,
        arm_code=expected.arm_code,
        replica_count=expected.replica_count,
        initial_fanout=expected.initial_fanout,
        integrity_valid=True,
        campaign_member=False,
    )


def _seal_pass_outcome(slot_root: Path, slot_id: str) -> bytes:
    sealed_files = {
        path.relative_to(slot_root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(slot_root.rglob("*"))
        if path.is_file() and path.name != validation.OUTCOME_FILENAME
    }
    payload = _canonical(
        {
            "schema_version": 1,
            "slot_id": slot_id,
            "history": [
                {"sequence": 0, "state": "NOT_STARTED", "reason": None},
                {"sequence": 1, "state": "PASS", "reason": None},
            ],
            "sealed_files": sealed_files,
        }
    )
    (slot_root / validation.OUTCOME_FILENAME).write_bytes(payload)
    return payload


def _v25_coverage_lifecycle_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    row_count: int,
    version: int = 25,
) -> dict[str, object]:
    manifest, runtime_document, expected_by_id = _v25_coverage_runtime_fixture(
        monkeypatch,
        version=version,
    )
    plan = build_factorial_plan(manifest)
    primary = next(
        slot for slot in plan.slots if slot.slot_id == "slot-066-n31-f5-b05-P"
    )
    repair = next(
        slot for slot in plan.slots if slot.slot_id == "slot-037-n31-f2-b04-00"
    )
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    version_label = f"v{version}"
    root = (
        tmp_path / f"shape-placement-factorial-{version_label}-coverage-smoke"
    ).resolve()
    root.mkdir()
    (root / validation.BUILD_EVIDENCE_DIRECTORY).mkdir()
    manifest_bytes = {
        25: V25_MANIFEST_PATH,
        26: V26_MANIFEST_PATH,
        27: V27_MANIFEST_PATH,
        28: V28_MANIFEST_PATH,
        29: V29_MANIFEST_PATH,
        30: V30_MANIFEST_PATH,
        31: V31_MANIFEST_PATH,
        32: V32_MANIFEST_PATH,
        33: V33_MANIFEST_PATH,
        34: V34_MANIFEST_PATH,
    }[version].read_bytes()
    runtime_bytes = _canonical(runtime_document)
    static_artifacts = {
        validation.MANIFEST_FILENAME: manifest_bytes,
        validation.PLAN_FILENAME: plan.canonical_bytes,
        validation.RUNTIME_FILENAME: runtime_bytes,
    }
    revision = "ab" * 20
    build_provenance = {
        "schema_version": 1,
        "revision": revision,
        "producer": "validator-lifecycle-test",
    }
    build_provenance_bytes = _canonical(build_provenance)
    authorization_bytes = execution.build_execution_authorization_receipt(
        scope="excluded_n31_coverage_smoke",
        approval_reference="test thesis-author approval",
        approved_utc="2026-08-10T00:00:00+00:00",
        kauri_revision=revision,
        slot_ids=tuple(slot.slot_id for slot in coverage.slots),
        result_root=(
            f"results/shape-placement-factorial-{version_label}-coverage-smoke"
        ),
        static_artifacts=static_artifacts,
        build_provenance_sha256=hashlib.sha256(
            build_provenance_bytes
        ).hexdigest(),
    )
    authorization = json.loads(authorization_bytes)
    contract = execution.build_coverage_smoke_execution_contract(
        runtime=coverage.runtime,
        static_artifacts=static_artifacts,
        authorization=authorization,
        authorization_payload=authorization_bytes,
        build_provenance=build_provenance,
    )
    contract_bytes = _canonical(contract)
    (root / execution.COVERAGE_SMOKE_AUTHORIZATION_FILENAME).write_bytes(
        authorization_bytes
    )
    (root / execution.COVERAGE_SMOKE_CONTRACT_FILENAME).write_bytes(
        contract_bytes
    )

    expected_validation = {
        "outcome": "PASS",
        "reason": None,
        "integrity_valid": True,
        "campaign_member": False,
        "figure_eligible": False,
    }
    rows: list[bytes] = []
    previous = "0" * 64
    for index, spec in enumerate(coverage.runtimes, 1):
        preflight = SimpleNamespace(
            revision=revision,
            result_root=root,
            slot_directory=root / spec.slot_id,
            free_bytes=20_000_000_000,
            build_provenance=build_provenance,
        )
        started = execution.build_coverage_smoke_started_record(
            runtime=coverage.runtime,
            spec=spec,
            coverage_execution_ordinal=index,
            preflight=preflight,
            static_artifacts=static_artifacts,
            authorization=authorization,
            authorization_payload=authorization_bytes,
            contract_payload=contract_bytes,
            previous_record_sha256=previous,
            recorded_utc=f"2026-08-10T00:00:0{index * 2 - 1}+00:00",
            recorded_monotonic_ns=index * 2 - 1,
        )
        started_bytes = _canonical(started)
        rows.append(started_bytes)
        previous = hashlib.sha256(started_bytes).hexdigest()
        terminal = execution.build_coverage_smoke_terminal_record(
            runtime=coverage.runtime,
            spec=spec,
            coverage_execution_ordinal=index,
            execution=SimpleNamespace(
                slot_directory=root / spec.slot_id,
                outcome="PASS",
                reason=None,
                launch_count=spec.replica_count + 1,
            ),
            validation=expected_validation,
            static_artifacts=static_artifacts,
            authorization=authorization,
            authorization_payload=authorization_bytes,
            contract_payload=contract_bytes,
            build_provenance=build_provenance,
            previous_record_sha256=previous,
            recorded_utc=f"2026-08-10T00:00:0{index * 2}+00:00",
            recorded_monotonic_ns=index * 2,
        )
        terminal_bytes = _canonical(terminal)
        rows.append(terminal_bytes)
        previous = hashlib.sha256(terminal_bytes).hexdigest()
    ledger_bytes = b"".join(rows[:row_count])
    (root / execution.COVERAGE_SMOKE_LEDGER_FILENAME).write_bytes(ledger_bytes)

    present_count = 1 if row_count <= 2 else 2
    slot_roots: dict[str, Path] = {}
    for index, spec in enumerate(coverage.runtimes[:present_count], 1):
        slot_root = root / spec.slot_id
        slot_root.mkdir()
        slot_roots[spec.slot_id] = slot_root
        for name, payload in static_artifacts.items():
            (slot_root / name).write_bytes(payload)
        (slot_root / validation.AUTHORIZATION_FILENAME).write_bytes(
            authorization_bytes
        )
        provenance = slot_root / validation.BUILD_PROVENANCE_FILENAME
        provenance.parent.mkdir()
        provenance.write_bytes(build_provenance_bytes)
        (slot_root / execution.COVERAGE_SMOKE_CONTRACT_FILENAME).write_bytes(
            contract_bytes
        )
        prefix_count = 1 if index == 1 else 3
        (slot_root / execution.COVERAGE_SMOKE_LEDGER_PREFIX_FILENAME).write_bytes(
            b"".join(rows[:prefix_count])
        )

    primary_id, repair_id = validation._coverage_smoke_slot_ids(
        manifest.manifest_id
    )
    primary_root = slot_roots[primary_id]
    primary_outcome_bytes = _seal_pass_outcome(primary_root, primary_id)
    if repair_id in slot_roots:
        primary_outcome = json.loads(primary_outcome_bytes)
        receipt = {
            "schema_version": 1,
            "coverage_smoke_id": coverage.runtime.runtime_id,
            "contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
            "authorization_id": authorization["authorization_id"],
            "authorization_sha256": hashlib.sha256(
                authorization_bytes
            ).hexdigest(),
            "predecessor_coverage_execution_ordinal": 1,
            "predecessor_slot_id": primary_id,
            "predecessor_terminal_record_sha256": hashlib.sha256(
                rows[1]
            ).hexdigest(),
            "predecessor_outcome_sha256": hashlib.sha256(
                primary_outcome_bytes
            ).hexdigest(),
            "predecessor_sealed_files_sha256": hashlib.sha256(
                _canonical(primary_outcome["sealed_files"])
            ).hexdigest(),
            "predecessor_validation": expected_validation,
        }
        repair_root = slot_roots[repair_id]
        (
            repair_root
            / execution.COVERAGE_SMOKE_PREDECESSOR_RECEIPT_FILENAME
        ).write_bytes(_canonical(receipt))
        _seal_pass_outcome(repair_root, repair_id)

    return {
        "manifest": manifest,
        "plan": json.loads(plan.canonical_bytes),
        "runtime_sha256": hashlib.sha256(runtime_bytes).hexdigest(),
        "authorization": authorization,
        "authorization_bytes": authorization_bytes,
        "build_provenance_bytes": build_provenance_bytes,
        "recorded_result_root": root,
        "expected_by_id": expected_by_id,
        "slot_roots": slot_roots,
        "rows": rows,
    }


def _validate_v25_coverage_fixture(
    fixture: dict[str, object],
    slot_id: str,
    *,
    predecessor_result: validation.SlotValidationResult | None = None,
    allow_predecessor_replay: bool = False,
) -> None:
    expected_by_id = fixture["expected_by_id"]
    assert isinstance(expected_by_id, dict)
    expected = expected_by_id[slot_id]
    slot_roots = fixture["slot_roots"]
    assert isinstance(slot_roots, dict)
    validation._validate_v25_coverage_execution_lifecycle(
        slot_root=slot_roots[slot_id],
        manifest=fixture["manifest"],
        plan=fixture["plan"],
        expected=expected,
        runtime_sha256=fixture["runtime_sha256"],
        authorization=fixture["authorization"],
        authorization_bytes=fixture["authorization_bytes"],
        build_provenance_bytes=fixture["build_provenance_bytes"],
        recorded_result_root=fixture["recorded_result_root"],
        result=_pass_coverage_validation(expected),
        predecessor_result=predecessor_result,
        allow_predecessor_replay=allow_predecessor_replay,
    )


@pytest.mark.parametrize(
    ("row_count", "slot_id"),
    (
        (1, "slot-066-n31-f5-b05-P"),
        (2, "slot-066-n31-f5-b05-P"),
        (4, "slot-066-n31-f5-b05-P"),
    ),
)
def test_v25_primary_coverage_lifecycle_accepts_only_exact_valid_stages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    row_count: int,
    slot_id: str,
) -> None:
    fixture = _v25_coverage_lifecycle_fixture(
        monkeypatch,
        tmp_path,
        row_count=row_count,
    )
    _validate_v25_coverage_fixture(fixture, slot_id)


@pytest.mark.parametrize("row_count", (3, 4))
def test_v25_repair_coverage_lifecycle_binds_independent_predecessor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    row_count: int,
) -> None:
    fixture = _v25_coverage_lifecycle_fixture(
        monkeypatch,
        tmp_path,
        row_count=row_count,
    )
    expected_by_id = fixture["expected_by_id"]
    assert isinstance(expected_by_id, dict)
    _validate_v25_coverage_fixture(
        fixture,
        "slot-037-n31-f2-b04-00",
        predecessor_result=_pass_coverage_validation(
            expected_by_id["slot-066-n31-f5-b05-P"]
        ),
    )


@pytest.mark.parametrize("version", (26, 27, 28, 29, 30, 31, 32, 33, 34))
def test_v26_through_v34_repair_coverage_lifecycle_inherit_exact_predecessor_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    version: int,
) -> None:
    fixture = _v25_coverage_lifecycle_fixture(
        monkeypatch,
        tmp_path,
        row_count=4,
        version=version,
    )
    expected_by_id = fixture["expected_by_id"]
    assert isinstance(expected_by_id, dict)
    _validate_v25_coverage_fixture(
        fixture,
        "slot-037-n31-f2-b04-00",
        predecessor_result=_pass_coverage_validation(
            expected_by_id["slot-066-n31-f5-b05-P"]
        ),
    )


def test_v28_three_row_prelaunch_rejection_cannot_validate_as_completed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _v25_coverage_lifecycle_fixture(
        monkeypatch,
        tmp_path,
        row_count=3,
        version=28,
    )
    slot_roots = fixture["slot_roots"]
    assert isinstance(slot_roots, dict)
    repair_id = "slot-037-n31-f2-b04-00"
    shutil.rmtree(slot_roots.pop(repair_id))

    with pytest.raises(FactorialValidationError, match="ledger stage"):
        _validate_v25_coverage_fixture(
            fixture,
            "slot-066-n31-f5-b05-P",
        )


@pytest.mark.parametrize(
    ("version", "official_reason"),
    (
        (
            29,
            "v26 cycle-1 adaptive_v2_converged event is absent or duplicated",
        ),
        (
            30,
            "relay view generation exceeds its unsigned 64-bit bound",
        ),
        (
            31,
            "v28 slot037 response duplicate probe guard/ingress generation drifted",
        ),
        (
            33,
            "fault causality phase windows are outside their exact live bounds",
        ),
    ),
)
def test_v29_through_v33_preserved_official_failures_cannot_validate_as_completed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    version: int,
    official_reason: str,
) -> None:
    fixture = _v25_coverage_lifecycle_fixture(
        monkeypatch,
        tmp_path,
        row_count=4,
        version=version,
    )
    root = fixture["recorded_result_root"]
    assert isinstance(root, Path)
    ledger_path = root / execution.COVERAGE_SMOKE_LEDGER_FILENAME
    rows = [json.loads(raw) for raw in ledger_path.read_bytes().splitlines()]
    rows[-1]["validation"] = {
        "campaign_member": False,
        "figure_eligible": False,
        "integrity_valid": False,
        "outcome": "FAIL",
        "reason": official_reason,
    }
    ledger_path.write_bytes(b"".join(_canonical(row) for row in rows))
    expected_by_id = fixture["expected_by_id"]
    assert isinstance(expected_by_id, dict)

    with pytest.raises(FactorialValidationError, match="non-PASS terminal"):
        _validate_v25_coverage_fixture(
            fixture,
            "slot-037-n31-f2-b04-00",
            predecessor_result=_pass_coverage_validation(
                expected_by_id["slot-066-n31-f5-b05-P"]
            ),
        )


def test_v32_preserved_official_primary_incomplete_cannot_validate_as_completed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _v25_coverage_lifecycle_fixture(
        monkeypatch,
        tmp_path,
        row_count=2,
        version=32,
    )
    root = fixture["recorded_result_root"]
    assert isinstance(root, Path)
    ledger_path = root / execution.COVERAGE_SMOKE_LEDGER_FILENAME
    rows = [json.loads(raw) for raw in ledger_path.read_bytes().splitlines()]
    reason = "unexpected pre-cleanup exit: adaptive-manager=1"
    rows[-1]["execution_outcome"] = "INCOMPLETE"
    rows[-1]["execution_reason"] = reason
    rows[-1]["validation"] = {
        "campaign_member": False,
        "figure_eligible": False,
        "integrity_valid": False,
        "outcome": "INCOMPLETE",
        "reason": reason,
    }
    ledger_path.write_bytes(b"".join(_canonical(row) for row in rows))

    with pytest.raises(FactorialValidationError, match="non-PASS terminal"):
        _validate_v25_coverage_fixture(
            fixture,
            "slot-066-n31-f5-b05-P",
        )


def test_v25_primary_rejects_later_partial_except_scoped_predecessor_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _v25_coverage_lifecycle_fixture(monkeypatch, tmp_path, row_count=3)
    with pytest.raises(FactorialValidationError, match="ledger stage"):
        _validate_v25_coverage_fixture(
            fixture,
            "slot-066-n31-f5-b05-P",
        )
    _validate_v25_coverage_fixture(
        fixture,
        "slot-066-n31-f5-b05-P",
        allow_predecessor_replay=True,
    )


def test_v25_private_predecessor_replay_accepts_row3_before_repair_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _v25_coverage_lifecycle_fixture(monkeypatch, tmp_path, row_count=3)
    slot_roots = fixture["slot_roots"]
    assert isinstance(slot_roots, dict)
    repair_id = "slot-037-n31-f2-b04-00"
    shutil.rmtree(slot_roots.pop(repair_id))

    with pytest.raises(FactorialValidationError, match="ledger stage"):
        _validate_v25_coverage_fixture(
            fixture,
            "slot-066-n31-f5-b05-P",
        )
    _validate_v25_coverage_fixture(
        fixture,
        "slot-066-n31-f5-b05-P",
        allow_predecessor_replay=True,
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("missing-contract-copy", "sealed contract"),
        ("missing-prefix", "prelaunch prefix"),
        ("missing-receipt", "predecessor receipt"),
        ("tampered-chain", "hash chain"),
        ("reversed", "identity/order"),
        ("extra-row", "ledger stage"),
        ("retry", "identity/order"),
        ("replacement", "identity/order"),
        ("wrong-predecessor-validation", "predecessor receipt"),
        ("wrong-predecessor-outcome", "predecessor receipt"),
        ("receipt-extra-field", "predecessor receipt"),
        ("contract-extra-field", "exact schedule"),
        ("ledger-extra-field", "invalid field set"),
        ("missing-root-contract", "execution-contract"),
        ("missing-root-ledger", "attempt-ledger"),
        ("prior-zero-free", "preflight"),
        ("prior-below-free", "preflight"),
        ("current-zero-free", "preflight"),
        ("current-below-free", "preflight"),
        ("wrong-slot-path", "slot path"),
        ("timestamp-regression", "chronology"),
        ("extra-root-state", "lifecycle state"),
        ("shared-build-drift", "authorization/build"),
        ("continued-after-nonpass", "non-PASS"),
    ),
)
def test_v25_coverage_lifecycle_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    fixture = _v25_coverage_lifecycle_fixture(monkeypatch, tmp_path, row_count=4)
    root = fixture["recorded_result_root"]
    assert isinstance(root, Path)
    slot_roots = fixture["slot_roots"]
    assert isinstance(slot_roots, dict)
    repair_root = slot_roots["slot-037-n31-f2-b04-00"]
    ledger_path = root / execution.COVERAGE_SMOKE_LEDGER_FILENAME
    rows = [json.loads(raw) for raw in ledger_path.read_bytes().splitlines()]
    if mutation == "missing-contract-copy":
        (repair_root / execution.COVERAGE_SMOKE_CONTRACT_FILENAME).unlink()
    elif mutation == "missing-prefix":
        (repair_root / execution.COVERAGE_SMOKE_LEDGER_PREFIX_FILENAME).unlink()
    elif mutation == "missing-receipt":
        (
            repair_root
            / execution.COVERAGE_SMOKE_PREDECESSOR_RECEIPT_FILENAME
        ).unlink()
    elif mutation == "tampered-chain":
        rows[2]["previous_record_sha256"] = "ff" * 32
    elif mutation == "reversed":
        rows[0]["slot_id"], rows[2]["slot_id"] = (
            rows[2]["slot_id"],
            rows[0]["slot_id"],
        )
    elif mutation == "extra-row":
        rows.append(dict(rows[-1]))
    elif mutation == "retry":
        rows[2]["attempt_ordinal"] = 2
    elif mutation == "replacement":
        rows[2]["replacement_policy"] = "replace"
    elif mutation == "wrong-predecessor-validation":
        receipt_path = (
            repair_root
            / execution.COVERAGE_SMOKE_PREDECESSOR_RECEIPT_FILENAME
        )
        receipt = json.loads(receipt_path.read_bytes())
        receipt["predecessor_validation"]["integrity_valid"] = False
        receipt_path.write_bytes(_canonical(receipt))
    elif mutation == "wrong-predecessor-outcome":
        receipt_path = (
            repair_root
            / execution.COVERAGE_SMOKE_PREDECESSOR_RECEIPT_FILENAME
        )
        receipt = json.loads(receipt_path.read_bytes())
        receipt["predecessor_outcome_sha256"] = "ff" * 32
        receipt_path.write_bytes(_canonical(receipt))
    elif mutation == "receipt-extra-field":
        receipt_path = (
            repair_root
            / execution.COVERAGE_SMOKE_PREDECESSOR_RECEIPT_FILENAME
        )
        receipt = json.loads(receipt_path.read_bytes())
        receipt["unexpected"] = True
        receipt_path.write_bytes(_canonical(receipt))
    elif mutation == "contract-extra-field":
        contract_path = root / execution.COVERAGE_SMOKE_CONTRACT_FILENAME
        contract = json.loads(contract_path.read_bytes())
        contract["unexpected"] = True
        contract_path.write_bytes(_canonical(contract))
    elif mutation == "ledger-extra-field":
        rows[2]["unexpected"] = True
    elif mutation == "missing-root-contract":
        (root / execution.COVERAGE_SMOKE_CONTRACT_FILENAME).unlink()
    elif mutation == "missing-root-ledger":
        ledger_path.unlink()
    elif mutation == "prior-zero-free":
        rows[0]["preflight_free_bytes"] = 0
    elif mutation == "prior-below-free":
        rows[0]["preflight_free_bytes"] = 9_999_999_999
    elif mutation == "current-zero-free":
        rows[2]["preflight_free_bytes"] = 0
    elif mutation == "current-below-free":
        rows[2]["preflight_free_bytes"] = 9_999_999_999
    elif mutation == "wrong-slot-path":
        rows[2]["slot_directory"] = rows[0]["slot_directory"]
    elif mutation == "timestamp-regression":
        rows[2]["recorded_monotonic_ns"] = rows[1][
            "recorded_monotonic_ns"
        ]
    elif mutation == "extra-root-state":
        (root / "unexpected.json").write_bytes(_canonical({"unexpected": True}))
    elif mutation == "shared-build-drift":
        provenance_path = repair_root / validation.BUILD_PROVENANCE_FILENAME
        provenance_path.write_bytes(_canonical({"drift": True}))
    else:
        rows[1]["validation"]["integrity_valid"] = False

    if mutation in {
        "tampered-chain",
        "reversed",
        "extra-row",
        "retry",
        "replacement",
        "ledger-extra-field",
        "prior-zero-free",
        "prior-below-free",
        "current-zero-free",
        "current-below-free",
        "wrong-slot-path",
        "timestamp-regression",
        "continued-after-nonpass",
    }:
        ledger_path.write_bytes(b"".join(_canonical(row) for row in rows))
    expected_by_id = fixture["expected_by_id"]
    assert isinstance(expected_by_id, dict)
    with pytest.raises(FactorialValidationError, match=reason):
        _validate_v25_coverage_fixture(
            fixture,
            "slot-037-n31-f2-b04-00",
            predecessor_result=_pass_coverage_validation(
                expected_by_id["slot-066-n31-f5-b05-P"]
            ),
        )


def test_v25_coverage_lifecycle_supports_relocated_sealed_copy_but_not_path_tamper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _v25_coverage_lifecycle_fixture(monkeypatch, tmp_path, row_count=4)
    original_root = fixture["recorded_result_root"]
    assert isinstance(original_root, Path)
    relocated_root = tmp_path / "relocated-coverage-smoke"
    original_root.rename(relocated_root)
    fixture["slot_roots"] = {
        slot_id: relocated_root / slot_id
        for slot_id in validation.V25_EXCLUDED_COVERAGE_SMOKE_SLOT_IDS
    }
    expected_by_id = fixture["expected_by_id"]
    assert isinstance(expected_by_id, dict)
    predecessor = _pass_coverage_validation(
        expected_by_id["slot-066-n31-f5-b05-P"]
    )
    _validate_v25_coverage_fixture(
        fixture,
        "slot-037-n31-f2-b04-00",
        predecessor_result=predecessor,
    )

    fixture["recorded_result_root"] = relocated_root
    with pytest.raises(FactorialValidationError, match="slot path"):
        _validate_v25_coverage_fixture(
            fixture,
            "slot-037-n31-f2-b04-00",
            predecessor_result=predecessor,
        )


def test_n31_coverage_smoke_relocates_by_exact_runtime_and_authorization(
    tmp_path: Path,
) -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    source_slot = next(
        slot
        for slot in plan.slots
        if slot.slot_id == validation.EXCLUDED_COVERAGE_SMOKE_SLOT_ID
    )
    coverage = execution.build_n31_coverage_smoke_slot(source_slot)
    runtime_bytes = execution._canonical_json_bytes(
        coverage.runtime.as_document()
    )
    static_artifacts = {
        validation.MANIFEST_FILENAME: MANIFEST_PATH.read_bytes(),
        validation.PLAN_FILENAME: plan.canonical_bytes,
        validation.RUNTIME_FILENAME: runtime_bytes,
    }
    authorization = execution.build_execution_authorization_receipt(
        scope="excluded_n31_coverage_smoke",
        approval_reference="test thesis-author approval",
        approved_utc="2026-08-09T00:00:00+00:00",
        kauri_revision="ab" * 20,
        slot_ids=(coverage.slot.slot_id,),
        result_root=validation.V24_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT,
        static_artifacts=static_artifacts,
        build_provenance_sha256="cd" * 32,
    )
    result_root = tmp_path / "renamed-coverage-smoke-archive"
    slot_root = result_root / coverage.slot.slot_id
    slot_root.mkdir(parents=True)
    (slot_root / validation.MANIFEST_FILENAME).write_bytes(
        static_artifacts[validation.MANIFEST_FILENAME]
    )
    (slot_root / validation.PLAN_FILENAME).write_bytes(
        static_artifacts[validation.PLAN_FILENAME]
    )
    (slot_root / validation.RUNTIME_FILENAME).write_bytes(runtime_bytes)
    (slot_root / validation.AUTHORIZATION_FILENAME).write_bytes(authorization)
    (
        result_root / validation.COVERAGE_SMOKE_AUTHORIZATION_FILENAME
    ).write_bytes(authorization)
    (
        loaded_manifest,
        loaded_plan,
        _,
        expected,
        runtime_sha256,
    ) = validation._load_static_contracts(slot_root)
    assert loaded_manifest == manifest
    assert loaded_plan == json.loads(plan.canonical_bytes)
    assert runtime_sha256 == validation.V24_COVERAGE_SMOKE_RUNTIME_SHA256
    assert validation._is_excluded_coverage_smoke_slot(
        slot_root,
        manifest_id=manifest.manifest_id,
    )

    document, _ = validation._validate_execution_authorization(
        slot_root,
        manifest=loaded_manifest,
        expected=expected,
        plan=loaded_plan,
        runtime_sha256=runtime_sha256,
        campaign_member=False,
    )

    assert document["scope"] == "excluded_n31_coverage_smoke"
    assert document["slot_ids"] == [coverage.slot.slot_id]
    assert document["result_root"] == (
        validation.V24_EXCLUDED_COVERAGE_SMOKE_RESULT_ROOT
    )


def _attach_relocated_build_evidence(
    result_root: Path,
    provenance: dict[str, object],
) -> None:
    """Model a durable archive copied with its already-preserved slot tree."""

    preservation_root = result_root.with_name(f"{result_root.name}-build-source")
    execution.preserve_build_evidence(preservation_root, provenance)
    shutil.move(
        str(preservation_root / execution.BUILD_EVIDENCE_DIRECTORY),
        str(result_root / execution.BUILD_EVIDENCE_DIRECTORY),
    )
    preservation_root.rmdir()


def test_receipt_and_build_provenance_validate_after_archive_relocation(
    tmp_path: Path,
) -> None:
    (
        recovered_slot,
        original_repository,
        receipt,
        expected,
        runtime,
        authorization_bytes,
    ) = _relocated_receipt_fixture(tmp_path)

    validated = validation._validate_slot_receipt(
        receipt,
        slot_root=recovered_slot,
        manifest=load_frozen_manifest(MANIFEST_PATH),
        expected=expected,
        runtime=runtime,
        runtime_sha256=validation.V24_RUNTIME_SHA256,
        authorization_bytes=authorization_bytes,
    )

    assert validated[3] == original_repository / runtime["result_path"]
    assert validated[4] == original_repository

    build_directory = original_repository / "build-adaptive"
    metadata_paths = {
        "adaptation_manager_link": build_directory
        / "examples/CMakeFiles/adaptation-manager.dir/link.txt",
        "cmake_cache": build_directory / "CMakeCache.txt",
        "compile_commands": build_directory / "compile_commands.json",
        "epoch_profile_digest_link": build_directory
        / "examples/CMakeFiles/epoch-profile-digest.dir/link.txt",
        "hotstuff_app_link": build_directory
        / "examples/CMakeFiles/hotstuff-app.dir/link.txt",
        "hotstuff_keygen_link": build_directory / "CMakeFiles/hotstuff-keygen.dir/link.txt",
        "hotstuff_tls_keygen_link": build_directory
        / "CMakeFiles/hotstuff-tls-keygen.dir/link.txt",
    }
    binary_paths = {
        "app": build_directory / "examples/hotstuff-app",
        "manager": build_directory / "examples/adaptation-manager",
        "keygen": build_directory / "hotstuff-keygen",
        "tls_keygen": build_directory / "hotstuff-tls-keygen",
        "epoch_profile_digest": build_directory / "examples/epoch-profile-digest",
    }
    all_paths = {**metadata_paths, **binary_paths}
    for name, path in all_paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"exact-build-evidence:{name}\n".encode())

    def row(path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "path": str(path),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    provenance = {
        "schema_version": 1,
        "revision": "12" * 20,
        "repository": str(original_repository),
        "build_directory": str(build_directory),
        "cmake_cache_sha256": row(metadata_paths["cmake_cache"])["sha256"],
        "build_command": [
            "cmake",
            "--build",
            str(build_directory),
            "--clean-first",
            "--parallel",
            "4",
            "--target",
            "hotstuff-app",
            "adaptation-manager",
            "hotstuff-keygen",
            "hotstuff-tls-keygen",
            "epoch-profile-digest",
        ],
        "build_metadata": {name: row(path) for name, path in metadata_paths.items()},
        "binaries": {name: row(path) for name, path in binary_paths.items()},
    }
    (recovered_slot / validation.BUILD_PROVENANCE_FILENAME).write_bytes(
        _canonical(provenance)
    )
    _attach_relocated_build_evidence(recovered_slot.parent, provenance)
    shutil.rmtree(original_repository)
    assert not original_repository.exists()
    validation._validate_build_provenance(
        recovered_slot,
        revision="12" * 20,
        recorded_repository=original_repository,
    )

    archived = recovered_slot.parent / "build-evidence/binaries/app"
    archived.chmod(0o700)
    archived.write_bytes(archived.read_bytes() + b"tamper")
    with pytest.raises(FactorialValidationError, match="build evidence"):
        validation._validate_build_provenance(
            recovered_slot,
            revision="12" * 20,
            recorded_repository=original_repository,
        )


@pytest.mark.parametrize(
    ("option", "replacement"),
    (
        ("--fault-containment-evidence-start-monotonic-ns", "1"),
        ("--fault-containment-required-tree-coverage", "21"),
    ),
)
def test_v15_receipt_rejects_fault_open_or_q_sized_manager_coverage(
    tmp_path: Path,
    option: str,
    replacement: str,
) -> None:
    recovered, _, receipt, expected, runtime, authorization = (
        _relocated_receipt_fixture(tmp_path)
    )
    argv = list(receipt["manager_argv"])
    argv[argv.index(option) + 1] = replacement
    receipt["manager_argv"] = argv

    with pytest.raises(FactorialValidationError, match="manager argv"):
        validation._validate_slot_receipt(
            receipt,
            slot_root=recovered,
            manifest=load_frozen_manifest(MANIFEST_PATH),
            expected=expected,
            runtime=runtime,
            runtime_sha256=validation.V24_RUNTIME_SHA256,
            authorization_bytes=authorization,
        )


def test_v9_receipt_rejects_an_exact_legacy_manifest_plan_pair(
    tmp_path: Path,
) -> None:
    recovered, _, receipt, expected, runtime, authorization = (
        _relocated_receipt_fixture(tmp_path)
    )
    receipt["manifest_sha256"] = validation.LEGACY_MANIFEST_SHA256
    receipt["plan_sha256"] = validation.LEGACY_PLAN_SHA256

    with pytest.raises(FactorialValidationError, match="receipt identity"):
        validation._validate_slot_receipt(
            receipt,
            slot_root=recovered,
            manifest=load_frozen_manifest(MANIFEST_PATH),
            expected=expected,
            runtime=runtime,
            runtime_sha256=validation.V24_RUNTIME_SHA256,
            authorization_bytes=authorization,
        )


def test_build_evidence_rejects_extra_and_symlink_entries(tmp_path: Path) -> None:
    (
        recovered_slot,
        original_repository,
        _receipt,
        _expected,
        _runtime,
        _authorization,
    ) = _relocated_receipt_fixture(tmp_path)
    build_directory = original_repository / "build-adaptive"
    binary_paths = {
        "app": build_directory / "examples/hotstuff-app",
        "manager": build_directory / "examples/adaptation-manager",
        "keygen": build_directory / "hotstuff-keygen",
        "tls_keygen": build_directory / "hotstuff-tls-keygen",
        "epoch_profile_digest": build_directory / "examples/epoch-profile-digest",
    }
    metadata_paths = {
        "adaptation_manager_link": build_directory
        / "examples/CMakeFiles/adaptation-manager.dir/link.txt",
        "cmake_cache": build_directory / "CMakeCache.txt",
        "compile_commands": build_directory / "compile_commands.json",
        "epoch_profile_digest_link": build_directory
        / "examples/CMakeFiles/epoch-profile-digest.dir/link.txt",
        "hotstuff_app_link": build_directory
        / "examples/CMakeFiles/hotstuff-app.dir/link.txt",
        "hotstuff_keygen_link": build_directory / "CMakeFiles/hotstuff-keygen.dir/link.txt",
        "hotstuff_tls_keygen_link": build_directory
        / "CMakeFiles/hotstuff-tls-keygen.dir/link.txt",
    }
    for name, path in {**binary_paths, **metadata_paths}.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())

    def row(path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "path": str(path),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    provenance = {
        "binaries": {name: row(path) for name, path in binary_paths.items()},
        "build_metadata": {name: row(path) for name, path in metadata_paths.items()},
    }
    _attach_relocated_build_evidence(recovered_slot.parent, provenance)
    evidence = recovered_slot.parent / "build-evidence"
    (evidence / "extra").write_bytes(b"extra")
    with pytest.raises(FactorialValidationError, match="unexpected"):
        validation._validate_build_evidence_archive(
            recovered_slot.parent,
            provenance,
        )
    (evidence / "extra").unlink()
    archived = evidence / "binaries/app"
    archived.unlink()
    archived.symlink_to(binary_paths["app"])
    with pytest.raises(FactorialValidationError, match="symlink"):
        validation._validate_build_evidence_archive(
            recovered_slot.parent,
            provenance,
        )


def test_relocated_receipt_rejects_authorization_tamper_and_identity_symlink(
    tmp_path: Path,
) -> None:
    recovered, _, receipt, expected, runtime, authorization = (
        _relocated_receipt_fixture(tmp_path)
    )
    with pytest.raises(FactorialValidationError, match="redaction key ID"):
        validation._validate_slot_receipt(
            receipt,
            slot_root=recovered,
            manifest=load_frozen_manifest(MANIFEST_PATH),
            expected=expected,
            runtime=runtime,
            runtime_sha256=validation.V24_RUNTIME_SHA256,
            authorization_bytes=authorization + b"tamper",
        )

    tls_path = recovered / "runtime/tls-identities.txt"
    tls_copy = recovered / "runtime/tls-identities-copy.txt"
    tls_copy.write_bytes(tls_path.read_bytes())
    tls_path.unlink()
    tls_path.symlink_to(tls_copy.name)
    with pytest.raises(FactorialValidationError, match="non-symlink"):
        validation._validate_slot_receipt(
            receipt,
            slot_root=recovered,
            manifest=load_frozen_manifest(MANIFEST_PATH),
            expected=expected,
            runtime=runtime,
            runtime_sha256=validation.V24_RUNTIME_SHA256,
            authorization_bytes=authorization,
        )


def test_outcome_seal_rejects_raw_hash_tamper(tmp_path: Path) -> None:
    raw = tmp_path / "raw/events.jsonl"
    raw.parent.mkdir()
    raw.write_bytes(b"original\n")
    outcome = {
        "schema_version": 1,
        "slot_id": tmp_path.name,
        "history": [
            {"sequence": 0, "state": "NOT_STARTED", "reason": None},
            {"sequence": 1, "state": "PASS", "reason": None},
        ],
        "sealed_files": {"raw/events.jsonl": hashlib.sha256(b"original\n").hexdigest()},
    }
    validation._validate_outcome(tmp_path, outcome, tmp_path.name)
    raw.write_bytes(b"tampered\n")
    with pytest.raises(FactorialValidationError, match="seal digest mismatch"):
        validation._validate_outcome(tmp_path, outcome, tmp_path.name)


def test_cleanup_validator_rejects_uncredible_signaled_exit(tmp_path: Path) -> None:
    import signal

    manifest = load_frozen_manifest(MANIFEST_PATH)
    expected = validation._expected_excluded_smoke(manifest)
    cleanup_started = 1_000
    rows = []
    for index, name in enumerate(
        ("adaptive-manager", *(f"replica-{replica}" for replica in range(expected.replica_count)))
    ):
        replica_id = None if index == 0 else index - 1
        rows.append(
            {
                "name": name,
                "replica_id": replica_id,
                "pid": 10_000 + index,
                "pgid": 10_000 + index,
                "cleanup_started_monotonic_ns": cleanup_started,
                "signal_number": int(signal.SIGTERM),
                "returncode": -int(signal.SIGTERM),
                "classification": "expected_cleanup",
                "exit_authorization": None,
            }
        )
    document = {
        "schema_version": 1,
        "slot_id": expected.slot_id,
        "cleanup_started_monotonic_ns": cleanup_started,
        "cleanup_completed": True,
        "streams_closed": True,
        "ports_clear": True,
        "final_streams_complete": True,
        "processes": rows,
        "error": None,
    }
    (tmp_path / validation.CLEANUP_LEDGER_FILENAME).write_bytes(_canonical(document))
    terminal = _native_event(
        source_id="adaptive-manager",
        sequence=1,
        monotonic_ns=500,
        event_type="adaptive_v2_session_terminal",
        payload={
            "cycle_ordinal": 1,
            "outcome": "advanced",
            "reason": "successor_converged",
        },
    )
    validation._validate_cleanup_ledger(
        tmp_path,
        expected=expected,
        manager_events=(terminal,),
        drain_complete_ns=900,
    )

    with pytest.raises(FactorialValidationError, match="SIGINT-only"):
        validation._validate_cleanup_ledger(
            tmp_path,
            expected=expected,
            manager_events=(terminal,),
            drain_complete_ns=900,
            strict_sigint_contract=True,
        )

    for process in document["processes"]:
        process["signal_number"] = int(signal.SIGINT)
        process["returncode"] = -int(signal.SIGINT)
    (tmp_path / validation.CLEANUP_LEDGER_FILENAME).write_bytes(_canonical(document))
    validation._validate_cleanup_ledger(
        tmp_path,
        expected=expected,
        manager_events=(terminal,),
        drain_complete_ns=900,
        strict_sigint_contract=True,
    )

    manager = document["processes"][0]
    manager.update(
        {
            "signal_number": None,
            "returncode": 0,
            "classification": "expected_clean_exit",
            "exit_authorization": {
                "relative_path": terminal.relative_path,
                "line_number": terminal.line_number,
                "source_id": terminal.source_id,
                "source_sequence": terminal.source_sequence,
                "source_monotonic_ns": terminal.monotonic_ns,
                "event_type": terminal.event_type,
                "line_sha256": terminal.line_sha256,
            },
        }
    )
    (tmp_path / validation.CLEANUP_LEDGER_FILENAME).write_bytes(_canonical(document))
    validation._validate_cleanup_ledger(
        tmp_path,
        expected=expected,
        manager_events=(terminal,),
        drain_complete_ns=900,
        strict_sigint_contract=True,
    )

    document["processes"][1]["returncode"] = 7
    (tmp_path / validation.CLEANUP_LEDGER_FILENAME).write_bytes(_canonical(document))
    with pytest.raises(FactorialValidationError, match="replica lifecycle"):
        validation._validate_cleanup_ledger(
            tmp_path,
            expected=expected,
            manager_events=(terminal,),
            drain_complete_ns=900,
        )


def _write_static_slot(
    tmp_path: Path, *, runtime_mutation: str | None = None
) -> Path:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    first = plan.slots[0]
    root = tmp_path / first.slot_id
    root.mkdir()
    (root / validation.MANIFEST_FILENAME).write_bytes(MANIFEST_PATH.read_bytes())
    (root / validation.PLAN_FILENAME).write_bytes(canonical_plan_bytes(plan))
    runtime_document = runtime.as_document()
    if runtime_mutation is not None:
        runtime_slot = next(
            slot
            for slot in runtime_document["slots"]
            if slot["slot_id"] == first.slot_id
        )
        if runtime_mutation == "actors":
            runtime_slot["actor_ids"] = []
        elif runtime_mutation == "observation_hold":
            runtime_slot["transitions"][0]["request"][
                "minimum_post_baseline_observation_ms"
            ] -= 1
        elif runtime_mutation == "fault_window":
            runtime_slot["fault_window"]["duration_s"] = 999
        elif runtime_mutation == "result_path":
            runtime_slot["result_path"] = "evil/path"
        else:  # pragma: no cover - test-helper misuse
            raise AssertionError(f"unknown mutation: {runtime_mutation}")
        runtime_payload = _canonical(runtime_document)
    else:
        runtime_payload = canonical_runtime_bytes(runtime)
    (root / validation.RUNTIME_FILENAME).write_bytes(runtime_payload)
    return root


def test_interrupted_slot_is_incomplete_not_pass(tmp_path: Path) -> None:
    result = validate_slot(_write_static_slot(tmp_path))

    assert result.outcome == "INCOMPLETE"
    assert "execution-authorization.json" in (result.reason or "")


def test_runtime_drift_fails_before_missing_raw_artifacts(tmp_path: Path) -> None:
    result = validate_slot(_write_static_slot(tmp_path, runtime_mutation="actors"))

    assert result.outcome == "FAIL"
    assert "drifted" in (result.reason or "")


@pytest.mark.parametrize("mutation", ("fault_window", "result_path"))
def test_previously_unbound_runtime_fields_fail_exact_byte_identity(
    tmp_path: Path, mutation: str
) -> None:
    result = validate_slot(_write_static_slot(tmp_path, runtime_mutation=mutation))

    assert result.outcome == "FAIL"
    assert "exact frozen campaign identity" in (result.reason or "")


def test_epoch1_observation_hold_drift_fails_before_raw_artifacts(
    tmp_path: Path,
) -> None:
    result = validate_slot(
        _write_static_slot(tmp_path, runtime_mutation="observation_hold")
    )

    assert result.outcome == "FAIL"
    assert "exact frozen campaign identity" in (result.reason or "")


def test_cross_run_jsonl_evidence_is_rejected(tmp_path: Path) -> None:
    event = {
        "event_schema_version": 1,
        "run_id": "other-slot",
        "source_kind": "replica",
        "source_id": "replica-0",
        "source_instance": "slot-replica-0",
        "source_sequence": 1,
        "source_monotonic_ns": 1,
        "event_type": "process.started",
        "payload": {"exit_status": None},
    }
    (tmp_path / "events.jsonl").write_bytes(_canonical(event))

    with pytest.raises(FactorialValidationError, match="mixed-run"):
        validation._read_jsonl(
            tmp_path,
            "events.jsonl",
            run_id="slot-test",
            source_kind="replica",
            source_id="replica-0",
            source_instance="slot-replica-0",
        )


def _coverage_evidence_record(
    *,
    sequence: int,
    tree_id: int,
    epoch_digest: str,
    fault_open_ns: int,
    reporter_id: int = 0,
    target_id: int = 1,
    outcome: str = "on_time",
    message_type: str = "direct_vote",
) -> validation._EvidenceRecord:
    duration_us = 100
    return validation._EvidenceRecord(
        ingestion_sequence=sequence,
        acceptance_monotonic_ns=fault_open_ns + 1_000_000 + sequence,
        observation_id=f"{sequence:064x}",
        reporter_id=reporter_id,
        target_id=target_id,
        epoch_number=0,
        tree_id=tree_id,
        epoch_digest=epoch_digest,
        block_hash=f"{10_000 + sequence:064x}",
        message_type=message_type,
        outcome=outcome,
        response_duration_us=duration_us if outcome == "on_time" else 0,
        deadline_duration_us=1_000,
        reporter_monotonic_ns=(
            fault_open_ns + duration_us * 1_000 + 999 + sequence
        ),
        reporter_sequence=sequence,
        signer_set=(target_id,) if outcome == "on_time" else (),
        acceptance_source_sequence=sequence,
    )


def _coverage_ready_event(
    *,
    records: tuple[validation._EvidenceRecord, ...],
    epoch_digest: str,
    fault_open_ns: int,
    transition_artifact_id: str,
) -> validation._NativeEvent:
    tree_ids = sorted({record.tree_id for record in records})
    return validation._NativeEvent(
        relative_path=validation.MANAGER_EVENTS_FILENAME,
        line_number=100,
        source_kind="adaptation_manager",
        source_id="adaptive-manager",
        source_instance="slot-adaptive-manager",
        source_sequence=100,
        monotonic_ns=fault_open_ns + 2_000_000,
        event_type="adaptive_v2.fault_containment_coverage_ready",
        payload={
            "cycle_ordinal": 0,
            "transition_artifact_id": transition_artifact_id,
            "predecessor_epoch_number": 0,
            "predecessor_epoch_digest": epoch_digest,
            "fault_evidence_start_monotonic_ns": fault_open_ns,
            "evidence_cutoff": len(records),
            "required_tree_ids": tree_ids,
            "observed_tree_ids": tree_ids,
        },
        line_sha256="ab" * 32,
    )


def test_v15_coverage_ready_rebuilds_every_exact_predecessor_tree_and_key() -> None:
    epoch_digest = "11" * 32
    fault_open_ns = 1_000_000_000
    trees = tuple(Tree(tree_id, 5, 2, tuple(range(13)), ()) for tree_id in range(13))
    records = tuple(
        _coverage_evidence_record(
            sequence=tree_id + 1,
            tree_id=tree_id,
            epoch_digest=epoch_digest,
            fault_open_ns=fault_open_ns,
        )
        for tree_id in range(13)
    )
    event = _coverage_ready_event(
        records=records,
        epoch_digest=epoch_digest,
        fault_open_ns=fault_open_ns,
        transition_artifact_id="transition-0",
    )

    qualifying = validation._validate_fault_containment_coverage_ready(
        manager_events=(event,),
        accepted_epoch0=records,
        predecessor_epoch_digest=epoch_digest,
        predecessor_trees=trees,
        fault_open_ns=fault_open_ns,
        transition_artifact_id="transition-0",
        selection_current_cutoff=len(records),
        epoch1_selection_ns=event.monotonic_ns + 1,
        epoch1_selection_source_sequence=event.source_sequence + 1,
    )

    assert len(qualifying) == 13
    assert {key[1] for key in qualifying} == set(range(13))


def test_v15_coverage_ready_rejects_an_earlier_full_n_cutoff() -> None:
    epoch_digest = "11" * 32
    fault_open_ns = 1_000_000_000
    trees = tuple(Tree(tree_id, 5, 2, tuple(range(13)), ()) for tree_id in range(13))
    event_records = tuple(
        _coverage_evidence_record(
            sequence=tree_id + 1,
            tree_id=tree_id,
            epoch_digest=epoch_digest,
            fault_open_ns=fault_open_ns,
        )
        for tree_id in range(13)
    )
    event = _coverage_ready_event(
        records=event_records,
        epoch_digest=epoch_digest,
        fault_open_ns=fault_open_ns,
        transition_artifact_id="transition-0",
    )
    later = replace(
        _coverage_evidence_record(
            sequence=14,
            tree_id=0,
            epoch_digest=epoch_digest,
            fault_open_ns=fault_open_ns,
        ),
        acceptance_monotonic_ns=event.monotonic_ns + 1,
        acceptance_source_sequence=event.source_sequence + 1,
    )

    with pytest.raises(FactorialValidationError, match="selection cutoff"):
        validation._validate_fault_containment_coverage_ready(
            manager_events=(event,),
            accepted_epoch0=(*event_records, later),
            predecessor_epoch_digest=epoch_digest,
            predecessor_trees=trees,
            fault_open_ns=fault_open_ns,
            transition_artifact_id="transition-0",
            selection_current_cutoff=14,
            epoch1_selection_ns=event.monotonic_ns + 2,
            epoch1_selection_source_sequence=event.source_sequence + 2,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("missing", "exactly one"),
        ("duplicate", "exactly one"),
        ("pre_window", "fault window"),
        ("post_selection", "before Epoch1 selection"),
        ("wrong_epoch", "predecessor"),
        ("wrong_digest", "predecessor"),
        ("wrong_start", "fault-open"),
        ("wrong_cutoff", "cutoff"),
        ("partial_13_tree", "tree IDs"),
        ("wrong_observed", "tree IDs"),
        ("boolean_cycle", "cycle ordinal"),
        ("overflow_cycle", "cycle ordinal exceeds uint64"),
        ("boolean_epoch", "predecessor epoch number"),
        ("overflow_epoch", "predecessor epoch number exceeds uint32"),
        ("floating_start", "evidence start"),
        ("overflow_start", "evidence start exceeds uint64"),
        ("extra_field", "field set"),
    ),
)
def test_v15_coverage_ready_rejects_nonexact_event(
    mutation: str,
    message: str,
) -> None:
    epoch_digest = "11" * 32
    fault_open_ns = 1_000_000_000
    trees = tuple(Tree(tree_id, 5, 2, tuple(range(13)), ()) for tree_id in range(13))
    records = tuple(
        _coverage_evidence_record(
            sequence=tree_id + 1,
            tree_id=tree_id,
            epoch_digest=epoch_digest,
            fault_open_ns=fault_open_ns,
        )
        for tree_id in range(13)
    )
    event = _coverage_ready_event(
        records=records,
        epoch_digest=epoch_digest,
        fault_open_ns=fault_open_ns,
        transition_artifact_id="transition-0",
    )
    events = [event]
    payload = dict(event.payload)
    selection_ns = event.monotonic_ns + 10
    selection_sequence = event.source_sequence + 1
    if mutation == "missing":
        events = []
    elif mutation == "duplicate":
        events.append(replace(event, source_sequence=101, line_number=101))
    elif mutation == "pre_window":
        events[0] = replace(event, monotonic_ns=fault_open_ns - 1)
    elif mutation == "post_selection":
        selection_sequence = event.source_sequence
    elif mutation == "wrong_epoch":
        payload["predecessor_epoch_number"] = 1
    elif mutation == "wrong_digest":
        payload["predecessor_epoch_digest"] = "22" * 32
    elif mutation == "wrong_start":
        payload["fault_evidence_start_monotonic_ns"] = fault_open_ns + 1
    elif mutation == "wrong_cutoff":
        payload["evidence_cutoff"] = len(records) - 1
    elif mutation == "partial_13_tree":
        payload["required_tree_ids"] = list(range(12))
        payload["observed_tree_ids"] = list(range(12))
    elif mutation == "wrong_observed":
        payload["observed_tree_ids"] = list(range(12))
    elif mutation == "boolean_cycle":
        payload["cycle_ordinal"] = False
    elif mutation == "overflow_cycle":
        payload["cycle_ordinal"] = validation._UINT64_MAX + 1
    elif mutation == "boolean_epoch":
        payload["predecessor_epoch_number"] = False
    elif mutation == "overflow_epoch":
        payload["predecessor_epoch_number"] = 1 << 32
    elif mutation == "floating_start":
        payload["fault_evidence_start_monotonic_ns"] = float(fault_open_ns)
    elif mutation == "overflow_start":
        payload["fault_evidence_start_monotonic_ns"] = validation._UINT64_MAX + 1
    elif mutation == "extra_field":
        payload["actor_ids"] = [9, 10, 12]
    if mutation in {
        "wrong_epoch",
        "wrong_digest",
        "wrong_start",
        "wrong_cutoff",
        "partial_13_tree",
        "wrong_observed",
        "boolean_cycle",
        "overflow_cycle",
        "boolean_epoch",
        "overflow_epoch",
        "floating_start",
        "overflow_start",
        "extra_field",
    }:
        events[0] = replace(event, payload=payload)

    with pytest.raises(FactorialValidationError, match=message):
        validation._validate_fault_containment_coverage_ready(
            manager_events=tuple(events),
            accepted_epoch0=records,
            predecessor_epoch_digest=epoch_digest,
            predecessor_trees=trees,
            fault_open_ns=fault_open_ns,
            transition_artifact_id="transition-0",
            selection_current_cutoff=len(records),
            epoch1_selection_ns=selection_ns,
            epoch1_selection_source_sequence=selection_sequence,
        )


def test_v15_coverage_uses_conservative_attempt_start_and_is_actor_blind() -> None:
    epoch_digest = "11" * 32
    fault_open_ns = 1_000_000_000
    valid = _coverage_evidence_record(
        sequence=1,
        tree_id=0,
        epoch_digest=epoch_digest,
        fault_open_ns=fault_open_ns,
    )
    pre_fault = replace(
        valid,
        reporter_monotonic_ns=(
            fault_open_ns + valid.response_duration_us * 1_000 + 998
        ),
    )
    zero_duration = replace(
        valid,
        response_duration_us=0,
        reporter_monotonic_ns=fault_open_ns + 999,
    )
    zero_duration_pre_fault = replace(
        zero_duration,
        reporter_monotonic_ns=fault_open_ns + 998,
    )
    negative_duration = replace(valid, response_duration_us=-1)
    overflow = replace(valid, response_duration_us=validation._UINT64_MAX)
    wrong_message = replace(valid, message_type="aggregate_relay")
    wrong_epoch = replace(valid, epoch_number=1)
    for nonqualifying in (
        pre_fault,
        zero_duration_pre_fault,
        negative_duration,
        overflow,
        wrong_message,
        wrong_epoch,
    ):
        assert not validation._qualifying_fault_proposal_keys(
            (nonqualifying,),
            epoch_number=0,
            epoch_digest=epoch_digest,
            evidence_cutoff=1,
            fault_open_ns=fault_open_ns,
        )

    assert validation._qualifying_fault_proposal_keys(
        (zero_duration,),
        epoch_number=0,
        epoch_digest=epoch_digest,
        evidence_cutoff=1,
        fault_open_ns=fault_open_ns,
    )
    original = validation._qualifying_fault_proposal_keys(
        (valid,),
        epoch_number=0,
        epoch_digest=epoch_digest,
        evidence_cutoff=1,
        fault_open_ns=fault_open_ns,
    )
    actor_mutated = validation._qualifying_fault_proposal_keys(
        (replace(valid, reporter_id=7, target_id=8),),
        epoch_number=0,
        epoch_digest=epoch_digest,
        evidence_cutoff=1,
        fault_open_ns=fault_open_ns,
    )
    assert actor_mutated == original


def test_v15_guard_rejects_timeout_reuse_from_a_nonqualifying_proposal() -> None:
    epoch_digest = "11" * 32
    fault_open_ns = 1_000_000_000
    record = replace(
        _coverage_evidence_record(
            sequence=2,
            tree_id=0,
            epoch_digest=epoch_digest,
            fault_open_ns=fault_open_ns,
            reporter_id=4,
            target_id=9,
        ),
        observation_id="33" * 32,
        outcome="timeout",
        response_duration_us=0,
        deadline_duration_us=100,
        reporter_monotonic_ns=fault_open_ns + 100 * 1_000 + 999,
        signer_set=(),
    )
    other_key = (
        record.epoch_number,
        record.tree_id,
        record.epoch_digest,
        "44" * 32,
    )

    with pytest.raises(FactorialValidationError, match="lacks.*reporters"):
        validation._validate_guarded_actor_evidence(
            (record,),
            baseline_cutoff=1,
            current_cutoff=2,
            actor_ids=(9,),
            required_reporters=1,
            eligible_proposal_keys={other_key},
        )


def test_v18_guarded_witness_replay_stays_exact_and_drawdown_independent() -> None:
    epoch_digest = "11" * 32
    fault_open_ns = 1_000_000_000
    records = tuple(
        _coverage_evidence_record(
            sequence=sequence,
            tree_id=sequence - 1,
            epoch_digest=epoch_digest,
            fault_open_ns=fault_open_ns,
            reporter_id=reporter_id,
            target_id=6,
            outcome="timeout",
            message_type="aggregate_relay",
        )
        for sequence, reporter_id in enumerate((2, 3, 4, 5), start=1)
    )
    proposal_keys = tuple(
        (
            record.epoch_number,
            record.tree_id,
            record.epoch_digest,
            record.block_hash,
        )
        for record in records
    )

    # The fourth timeout is intentionally outside the exact post-fault
    # ProposalKey set.  It is neither a witness nor an input from which this
    # independent validator may infer the native reputation drawdown.
    validation._validate_guarded_actor_evidence(
        records,
        baseline_cutoff=0,
        current_cutoff=4,
        actor_ids=(6,),
        required_reporters=3,
        eligible_proposal_keys=proposal_keys[:3],
    )

    with pytest.raises(FactorialValidationError, match="lacks.*reporters"):
        validation._validate_guarded_actor_evidence(
            records,
            baseline_cutoff=0,
            current_cutoff=4,
            actor_ids=(6,),
            required_reporters=3,
            eligible_proposal_keys=proposal_keys[:2],
        )


def test_v15_hard_causal_window_extends_to_selection_only_for_qualifying_keys() -> None:
    key = (0, 17, "11" * 32, "22" * 32)
    arguments = {
        "proposal_key": key,
        "marker_monotonic_ns": 450,
        "fault_phase": (100, 400),
        "fault_open_ns": 100,
        "epoch1_selection_ns": 500,
    }

    assert not validation._is_epoch0_hard_causal_candidate(
        **arguments,
        qualifying_proposal_keys=None,
    )
    assert validation._is_epoch0_hard_causal_candidate(
        **arguments,
        qualifying_proposal_keys={key},
    )
    assert not validation._is_epoch0_hard_causal_candidate(
        **arguments,
        qualifying_proposal_keys={(0, 18, key[2], key[3])},
    )


def _v16_hard_timeout_witness_fixture() -> dict[str, object]:
    actor = 5
    epoch0_digest = "11" * 32
    epoch1_digest = "22" * 32
    epoch2_digest = "33" * 32
    initial_trees = (
        Tree(0, 2, 2, (0, 1, actor, 2, 3, 4, 6), ()),
        Tree(1, 2, 2, (1, 0, actor, 2, 3, 4, 6), ()),
        Tree(2, 2, 2, (2, 0, actor, 1, 3, 4, 6), ()),
    )
    contained_tree = Tree(
        0,
        2,
        2,
        (0, 1, 2, 3, 4, 6, actor),
        (actor,),
    )

    def marker(
        *,
        line_number: int,
        epoch_number: int,
        tree_id: int,
        epoch_digest: str,
        block_number: int,
        action: str,
        monotonic_ns: int,
    ) -> FaultMarker:
        return FaultMarker(
            source_replica=actor,
            line_number=line_number,
            fault_mode="rotating_intermittent_omission_v1",
            epoch_number=epoch_number,
            tree_id=tree_id,
            epoch_digest=epoch_digest,
            block_hash=f"{block_number:064x}",
            window="selection-visible-v2",
            window_start_ns=100,
            window_end_ns=1_200,
            actor=actor,
            action=action,
            monotonic_ns=monotonic_ns,
            raw_line_sha256=f"{line_number:064x}",
        )

    epoch0_markers = (
        marker(
            line_number=1,
            epoch_number=0,
            tree_id=0,
            epoch_digest=epoch0_digest,
            block_number=100,
            action="omit_aggregate",
            monotonic_ns=200,
        ),
        marker(
            line_number=2,
            epoch_number=0,
            tree_id=1,
            epoch_digest=epoch0_digest,
            block_number=101,
            action="omit_aggregate",
            monotonic_ns=220,
        ),
        marker(
            line_number=3,
            epoch_number=0,
            tree_id=2,
            epoch_digest=epoch0_digest,
            block_number=102,
            action="omit_aggregate",
            monotonic_ns=240,
        ),
    )
    markers = (
        *epoch0_markers,
        marker(
            line_number=4,
            epoch_number=1,
            tree_id=0,
            epoch_digest=epoch1_digest,
            block_number=200,
            action="omit_direct_vote",
            monotonic_ns=700,
        ),
        marker(
            line_number=5,
            epoch_number=2,
            tree_id=0,
            epoch_digest=epoch2_digest,
            block_number=300,
            action="omit_direct_vote",
            monotonic_ns=1_000,
        ),
    )

    def proposal_event(marker: FaultMarker, sequence: int) -> validation._NativeEvent:
        return _native_event(
            source_id=f"replica-{actor}",
            sequence=sequence,
            monotonic_ns=marker.monotonic_ns - 10,
            event_type="aggregation.required_set_ready",
            payload={
                "epoch_number": marker.epoch_number,
                "tree_id": marker.tree_id,
                "epoch_digest": marker.epoch_digest,
                "block_hash": marker.block_hash,
                "context_generation": 1,
                "observer_replica": actor,
                "wait_exempt_signers": [],
                "accepted_signers": [],
                "absent_direct_children": [],
                "missing_optional_signers": [],
                "required_branch_gaps": [],
                "root_signer_count": 0,
                "global_quorum": 0,
                "rejection_reason": None,
            },
        )

    def timeout_record(
        marker: FaultMarker,
        *,
        ingestion_sequence: int,
        reporter_id: int,
        reporter_monotonic_ns: int,
    ) -> validation._EvidenceRecord:
        return validation._EvidenceRecord(
            ingestion_sequence=ingestion_sequence,
            acceptance_monotonic_ns=reporter_monotonic_ns + 1,
            observation_id=f"{ingestion_sequence:064x}",
            reporter_id=reporter_id,
            target_id=actor,
            epoch_number=marker.epoch_number,
            tree_id=marker.tree_id,
            epoch_digest=marker.epoch_digest,
            block_hash=marker.block_hash,
            message_type="aggregate_relay",
            outcome="timeout",
            response_duration_us=0,
            deadline_duration_us=10,
            reporter_monotonic_ns=reporter_monotonic_ns,
            reporter_sequence=ingestion_sequence,
            signer_set=(),
        )

    accepted_epoch0 = (
        timeout_record(
            epoch0_markers[0],
            ingestion_sequence=2,
            reporter_id=0,
            reporter_monotonic_ns=250,
        ),
        timeout_record(
            epoch0_markers[1],
            ingestion_sequence=3,
            reporter_id=1,
            reporter_monotonic_ns=270,
        ),
        # This exact timeout exists in the retained evidence prefix but is not
        # visible at the selecting cutoff.  It is the immature-tail case.
        timeout_record(
            epoch0_markers[2],
            ingestion_sequence=4,
            reporter_id=2,
            reporter_monotonic_ns=290,
        ),
    )
    return {
        "markers": markers,
        "replica_events": {
            actor: tuple(
                proposal_event(item, sequence)
                for sequence, item in enumerate(markers, start=1)
            )
        },
        "actor_ids": (actor,),
        "fault_mode": "rotating_intermittent_omission_v1",
        "max_omissions_per_proposal": 1,
        "initial_epoch_digest": epoch0_digest,
        "initial_trees": initial_trees,
        "window_id": "selection-visible-v2",
        "window_start_ns": 100,
        "window_end_ns": 1_200,
        "epoch1_command_ns": 500,
        "epoch1_activation_ns": 600,
        "epoch2_command_ns": 900,
        "epoch1_selection_ns": 450,
        "epoch2_selection_ns": 850,
        "explicit_phase_edge_eligibility": True,
        "epoch1_digest": epoch1_digest,
        "epoch1_trees": (contained_tree,),
        "epoch2_digest": epoch2_digest,
        "epoch2_trees": (contained_tree,),
        "phase_windows": {
            "baseline": (10, 90, 1),
            "fault_evidence": (100, 400, 1),
            "epoch1_stable": (650, 800, 1),
            "epoch2_stable": (950, 1_100, 1),
        },
        "required_reporters": 2,
        "accepted_epoch0": accepted_epoch0,
        "baseline_cutoff": 1,
        "current_cutoff": 3,
        "epoch0_qualifying_proposal_keys": {
            (
                item.epoch_number,
                item.tree_id,
                item.epoch_digest,
                item.block_hash,
            )
            for item in epoch0_markers
        },
    }


def _v20_shutdown_tail_witness_fixture() -> dict[str, object]:
    """Convert the exact hard-actor fixture to a source-bound v20 tail."""

    arguments = _v16_hard_timeout_witness_fixture()
    legacy_markers = arguments["markers"]
    assert isinstance(legacy_markers, tuple)
    initial_trees = {
        tree.tree_id: tree for tree in arguments["initial_trees"]
    }
    epoch1_trees = {tree.tree_id: tree for tree in arguments["epoch1_trees"]}
    epoch2_trees = {tree.tree_id: tree for tree in arguments["epoch2_trees"]}
    trees_by_epoch = {
        0: initial_trees,
        1: epoch1_trees,
        2: epoch2_trees,
    }
    markers: list[FaultMarker] = []
    opportunities: list[validation.FaultContributionOpportunity] = []
    for legacy in legacy_markers:
        tree = trees_by_epoch[legacy.epoch_number][legacy.tree_id]
        position = tree.members.index(legacy.actor)
        leaf_start = validation._first_leaf_index(len(tree.members), tree.fanout)
        role = "internal" if position < leaf_start else "leaf"
        marker = replace(
            legacy,
            fault_mode="tiered_persistent_responsive_omission_v2",
            cohort="hard",
            hard_actor_count=1,
            responsive_degraded_actor_count=0,
            fault_threshold=1,
            max_omissions_per_proposal=1,
            responsive_omission_period=41,
            contribution_ordinal=0,
            contribution_role=role,
            role_contribution_ordinal=0,
        )
        markers.append(marker)
        opportunities.append(
            validation.FaultContributionOpportunity(
                source_replica=marker.actor,
                relative_path=f"raw/replica-{marker.actor}.jsonl",
                line_number=len(opportunities) + 1,
                source_sequence=len(opportunities) + 1,
                event_monotonic_ns=marker.monotonic_ns,
                line_sha256=f"{len(opportunities) + 100:064x}",
                actor=marker.actor,
                epoch_number=marker.epoch_number,
                tree_id=marker.tree_id,
                epoch_digest=marker.epoch_digest,
                block_hash=marker.block_hash,
                view_generation=marker.epoch_number * (1 << 32) + marker.tree_id + 1,
                physical_role=role,
                parent_replica=tree.members[(position - 1) // tree.fanout],
                expected_message_type=(
                    "aggregate_relay" if role == "internal" else "direct_vote"
                ),
                cohort="hard",
                window=marker.window,
                window_start_ns=marker.window_start_ns,
                window_end_ns=marker.window_end_ns,
                decision_monotonic_ns=marker.monotonic_ns,
                contribution_ordinal=0,
                role_contribution_ordinal=0,
                scheduled_action=marker.action,
                responsive_omission_period=41,
                fault_threshold=1,
                hard_actor_count=1,
                responsive_degraded_actor_count=0,
                fault_mode=marker.fault_mode,
            )
        )

    actor = markers[0].actor
    legacy_events = arguments["replica_events"]
    assert isinstance(legacy_events, dict)
    proposal_events = legacy_events[actor]
    assert isinstance(proposal_events, tuple)
    # The final active proposal is interrupted after its exact opportunity and
    # marker but before any aggregation/configuration event or commit.
    combined_events = [*proposal_events[:-1]]
    combined_events.extend(
        _native_event(
            source_id=f"replica-{actor}",
            sequence=index,
            monotonic_ns=opportunity.event_monotonic_ns,
            event_type="fault.contribution_opportunity",
            payload={
                "proposal": {
                    "epoch_number": opportunity.epoch_number,
                    "tree_id": opportunity.tree_id,
                    "epoch_digest": opportunity.epoch_digest,
                    "block_hash": opportunity.block_hash,
                }
            },
        )
        for index, opportunity in enumerate(opportunities, start=1)
    )
    combined_events.append(
        _native_event(
            source_id=f"replica-{actor}",
            sequence=1,
            monotonic_ns=1_150,
            event_type="process.stopping",
            payload={"exit_status": None},
        )
    )
    combined_events = [
        replace(event, line_number=index, source_sequence=index)
        for index, event in enumerate(
            sorted(combined_events, key=lambda event: event.monotonic_ns),
            start=1,
        )
    ]
    opportunity_event_by_key = {
        (
            event.payload["proposal"]["epoch_number"],
            event.payload["proposal"]["tree_id"],
            event.payload["proposal"]["epoch_digest"],
            event.payload["proposal"]["block_hash"],
        ): event
        for event in combined_events
        if event.event_type == "fault.contribution_opportunity"
    }
    opportunities = [
        replace(
            opportunity,
            relative_path=opportunity_event_by_key[
                opportunity.proposal_key
            ].relative_path,
            line_number=opportunity_event_by_key[
                opportunity.proposal_key
            ].line_number,
            source_sequence=opportunity_event_by_key[
                opportunity.proposal_key
            ].source_sequence,
            event_monotonic_ns=opportunity_event_by_key[
                opportunity.proposal_key
            ].monotonic_ns,
            line_sha256=opportunity_event_by_key[
                opportunity.proposal_key
            ].line_sha256,
        )
        for opportunity in opportunities
    ]
    arguments.update(
        {
            "markers": tuple(markers),
            "contribution_opportunities": tuple(opportunities),
            "replica_events": {actor: tuple(combined_events)},
            "fault_mode": "tiered_persistent_responsive_omission_v2",
            "source_bound_contribution_opportunities": True,
            "selection_visible_hard_timeout_witnesses": True,
            "phase_windows": {
                phase: (start, end, 6 if phase != "baseline" else bucket_count)
                for phase, (start, end, bucket_count) in arguments[
                    "phase_windows"
                ].items()
            },
        }
    )
    return arguments


def _isolate_v20_shutdown_tail_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the synthetic fixture focused on the proposal-witness join."""

    monkeypatch.setattr(
        validation,
        "_validate_fault_marker_schedule",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        validation,
        "_validate_role_scoped_epoch1_internal_opportunities",
        lambda *args, **kwargs: None,
    )


def test_v20_shutdown_tail_opportunity_witness_is_version_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _v20_shutdown_tail_witness_fixture()
    # The fixture deliberately isolates the witness join from the tiered
    # cohort-cardinality and responsive-degraded role-coverage checks; the
    # real opportunity bijection/topology and hard-actor causal gates run.
    _isolate_v20_shutdown_tail_join(monkeypatch)

    with pytest.raises(
        FactorialValidationError,
        match="no matching native proposal/configuration event",
    ):
        validate_fault_causality(**arguments)

    assert validate_fault_causality(
        **arguments,
        source_bound_proposal_configuration_witnesses=True,
    ) == 0


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("marker_only", "marker-only"),
        ("event_only", "event-only"),
        ("mismatched_key", "marker-only"),
    ),
)
def test_v20_proposal_witness_preserves_exact_pairing_rejection(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    arguments = _v20_shutdown_tail_witness_fixture()
    _isolate_v20_shutdown_tail_join(monkeypatch)
    opportunities = arguments["contribution_opportunities"]
    assert isinstance(opportunities, tuple)
    if mutation == "marker_only":
        mutated = opportunities[:-1]
    elif mutation == "event_only":
        mutated = (
            *opportunities,
            replace(opportunities[-1], block_hash="ff" * 32),
        )
    else:
        mutated = (
            *opportunities[:-1],
            replace(opportunities[-1], block_hash="ff" * 32),
        )

    with pytest.raises(FactorialValidationError, match=message):
        validate_fault_causality(
            **{
                **arguments,
                "contribution_opportunities": mutated,
            },
            source_bound_proposal_configuration_witnesses=True,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("before_decision", "decision-to-process.stopping interval"),
        ("at_stopping", "decision-to-process.stopping interval"),
        ("missing_stopping", "lacks same-source process.stopping"),
    ),
)
def test_v20_proposal_witness_requires_exact_source_shutdown_order(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    arguments = _v20_shutdown_tail_witness_fixture()
    _isolate_v20_shutdown_tail_join(monkeypatch)
    opportunities = arguments["contribution_opportunities"]
    replica_events = arguments["replica_events"]
    assert isinstance(opportunities, tuple)
    assert isinstance(replica_events, dict)
    tail = opportunities[-1]
    actor = tail.source_replica
    events = replica_events[actor]
    assert isinstance(events, tuple)
    stopping = next(event for event in events if event.event_type == "process.stopping")
    source_event = next(
        event
        for event in events
        if (
            event.event_type == "fault.contribution_opportunity"
            and event.line_number == tail.line_number
        )
    )
    if mutation == "before_decision":
        event_monotonic_ns = tail.decision_monotonic_ns - 1
        opportunities = (
            *opportunities[:-1],
            replace(
                tail,
                event_monotonic_ns=event_monotonic_ns,
            ),
        )
    elif mutation == "at_stopping":
        event_monotonic_ns = stopping.monotonic_ns
        opportunities = (
            *opportunities[:-1],
            replace(tail, event_monotonic_ns=event_monotonic_ns),
        )
    else:
        replica_events = {
            **replica_events,
            actor: tuple(
                event for event in events if event.event_type != "process.stopping"
            ),
        }
    if mutation != "missing_stopping":
        replica_events = {
            **replica_events,
            actor: tuple(
                replace(event, monotonic_ns=event_monotonic_ns)
                if event is source_event
                else event
                for event in events
            ),
        }

    with pytest.raises(FactorialValidationError, match=message):
        validate_fault_causality(
            **{
                **arguments,
                "contribution_opportunities": opportunities,
                "replica_events": replica_events,
            },
            source_bound_proposal_configuration_witnesses=True,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("wrong_epoch", "view generation epoch"),
        ("wrong_tree", "view generation rotation"),
    ),
)
def test_v20_proposal_witness_binds_canonical_generation_topology(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    arguments = _v20_shutdown_tail_witness_fixture()
    _isolate_v20_shutdown_tail_join(monkeypatch)
    opportunities = arguments["contribution_opportunities"]
    assert isinstance(opportunities, tuple)
    target_index = -1 if mutation == "wrong_epoch" else 0
    target = opportunities[target_index]
    mutated = replace(
        target,
        view_generation=(1 if mutation == "wrong_epoch" else 2),
    )
    opportunities = tuple(
        mutated if index == target_index % len(opportunities) else opportunity
        for index, opportunity in enumerate(opportunities)
    )

    with pytest.raises(FactorialValidationError, match=message):
        validate_fault_causality(
            **{
                **arguments,
                "contribution_opportunities": opportunities,
            },
            source_bound_proposal_configuration_witnesses=True,
        )


def test_v20_proposal_witness_rejects_cross_actor_generation_conflict() -> None:
    arguments = _v20_shutdown_tail_witness_fixture()
    opportunities = arguments["contribution_opportunities"]
    replica_events = arguments["replica_events"]
    assert isinstance(opportunities, tuple)
    assert isinstance(replica_events, dict)
    original = opportunities[-1]
    original_events = replica_events[original.source_replica]
    assert isinstance(original_events, tuple)
    source_event = next(
        event
        for event in original_events
        if (
            event.event_type == "fault.contribution_opportunity"
            and event.line_number == original.line_number
        )
    )
    stopping = next(
        event
        for event in original_events
        if event.event_type == "process.stopping"
    )
    other_actor = 2
    other_path = f"raw/replica-{other_actor}.jsonl"
    other_instance = f"slot-replica-{other_actor}"
    other_line_sha256 = "fe" * 32
    other_source_event = replace(
        source_event,
        relative_path=other_path,
        line_number=1,
        source_id=f"replica-{other_actor}",
        source_instance=other_instance,
        source_sequence=1,
        line_sha256=other_line_sha256,
    )
    other_stopping = replace(
        stopping,
        relative_path=other_path,
        line_number=2,
        source_id=f"replica-{other_actor}",
        source_instance=other_instance,
        source_sequence=2,
    )
    conflicting = replace(
        original,
        source_replica=other_actor,
        relative_path=other_path,
        line_number=1,
        source_sequence=1,
        line_sha256=other_line_sha256,
        actor=other_actor,
        view_generation=(
            original.view_generation + len(arguments["epoch2_trees"])
        ),
    )
    phase_configurations = (
        (
            "fault_evidence",
            0,
            arguments["initial_epoch_digest"],
            {tree.tree_id: tree for tree in arguments["initial_trees"]},
        ),
        (
            "epoch1_stable",
            1,
            arguments["epoch1_digest"],
            {tree.tree_id: tree for tree in arguments["epoch1_trees"]},
        ),
        (
            "epoch2_stable",
            2,
            arguments["epoch2_digest"],
            {tree.tree_id: tree for tree in arguments["epoch2_trees"]},
        ),
    )

    with pytest.raises(
        FactorialValidationError,
        match="conflicting view generations",
    ):
        validation._source_bound_proposal_configuration_witnesses(
            opportunities=(*opportunities, conflicting),
            replica_events={
                **replica_events,
                other_actor: (other_source_event, other_stopping),
            },
            phase_configurations=phase_configurations,
        )


def test_v20_proposal_witness_cannot_bypass_source_bound_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _v20_shutdown_tail_witness_fixture()
    _isolate_v20_shutdown_tail_join(monkeypatch)

    with pytest.raises(
        FactorialValidationError,
        match="require strict contribution opportunity validation",
    ):
        validate_fault_causality(
            **{
                **arguments,
                "source_bound_contribution_opportunities": False,
            },
            source_bound_proposal_configuration_witnesses=True,
        )


def test_v16_tail_timeout_after_selection_cutoff_is_a_nonwitness() -> None:
    assert validate_fault_causality(
        **_v16_hard_timeout_witness_fixture(),
        selection_visible_hard_timeout_witnesses=True,
    ) == 0


def test_v15_preserves_universal_qualifying_hard_marker_timeout_gate() -> None:
    with pytest.raises(
        FactorialValidationError,
        match="pre-containment omission has no exact outstanding timeout",
    ):
        validate_fault_causality(**_v16_hard_timeout_witness_fixture())


def test_v16_pending_only_and_post_cutoff_timeouts_fail_final_gates() -> None:
    arguments = _v16_hard_timeout_witness_fixture()
    accepted = arguments["accepted_epoch0"]
    assert isinstance(accepted, tuple)
    pending_only = tuple(
        replace(
            record,
            outcome="on_time",
            response_duration_us=1,
            signer_set=(record.target_id,),
        )
        for record in accepted
    )
    with pytest.raises(FactorialValidationError, match="lack pre-Epoch1.*internal"):
        validate_fault_causality(
            **{**arguments, "accepted_epoch0": pending_only},
            selection_visible_hard_timeout_witnesses=True,
        )

    with pytest.raises(FactorialValidationError, match=r"full causally bound f\+1"):
        validate_fault_causality(
            **{**arguments, "current_cutoff": 2},
            selection_visible_hard_timeout_witnesses=True,
        )


def test_v16_selection_visible_mismatched_timeout_fails_closed() -> None:
    arguments = _v16_hard_timeout_witness_fixture()
    accepted = arguments["accepted_epoch0"]
    assert isinstance(accepted, tuple)

    with pytest.raises(
        FactorialValidationError,
        match="selection-visible.*fails its exact role/message/timing join",
    ):
        validate_fault_causality(
            **{
                **arguments,
                "accepted_epoch0": (
                    replace(accepted[0], message_type="direct_vote"),
                    *accepted[1:],
                ),
            },
            selection_visible_hard_timeout_witnesses=True,
        )


def test_on_time_evidence_at_deadline_is_rejected() -> None:
    reporter = 0
    target = 1
    epoch = 0
    tree_id = 0
    epoch_digest = "11" * 32
    block_hash = "22" * 32
    observation_id = hashlib.sha256(
        validation._OBSERVATION_DOMAIN
        + validation._u(reporter, 2)
        + validation._u(target, 2)
        + validation._u(epoch, 4)
        + validation._u(tree_id, 4)
        + bytes.fromhex(epoch_digest)
        + bytes.fromhex(block_hash)
        + validation._u(validation._MESSAGE_TYPE_CODE["direct_vote"], 1)
    ).hexdigest()
    event = _native_event(
        source_id="adaptive-manager",
        sequence=1,
        monotonic_ns=100,
        event_type="evidence.observation_accepted",
        payload={
            "ingestion_sequence": 1,
            "observation": {
                "schema_version": 1,
                "observation_id": observation_id,
                "reporter_id": reporter,
                "observed_replica_id": target,
                "configuration": {
                    "epoch_number": epoch,
                    "tree_id": tree_id,
                    "epoch_digest": epoch_digest,
                },
                "block_hash": block_hash,
                "expected_message_type": "direct_vote",
                "outcome": "on_time",
                "response_duration_us": 10,
                "deadline_duration_us": 10,
                "reporter_monotonic_ns": 90,
                "reporter_sequence": 1,
                "signer_set": [target],
            },
        },
    )

    with pytest.raises(FactorialValidationError, match="timing/signer"):
        validation._evidence_record(event, {reporter, target})


def test_validator_has_no_runtime_decision_or_validator_dependency() -> None:
    source = Path(validation.__file__).read_text(encoding="utf-8")

    assert "from .factorial_runtime" not in source
    assert "import factorial_runtime" not in source
    assert "subprocess" not in source


def test_matched_estimate_uses_frozen_df4_interval_and_strict_claim_rule() -> None:
    estimate = validation._matched_estimate(
        "placement",
        tuple(f"b{index}" for index in range(1, 6)),
        (1.0, 2.0, 3.0, 4.0, 5.0),
    )

    assert estimate.mean_tps == pytest.approx(3.0)
    assert estimate.sample_standard_deviation_tps == pytest.approx(2.5**0.5)
    assert estimate.ci95_lower_tps == pytest.approx(1.0367568385)
    assert estimate.ci95_upper_tps == pytest.approx(4.9632431615)
    assert estimate.positive_block_count == 5
    assert estimate.directional_claim_supported is True

    inconclusive = validation._matched_estimate(
        "placement",
        tuple(f"b{index}" for index in range(1, 6)),
        (-1.0, 1.0, 1.0, 1.0, 1.0),
    )
    assert inconclusive.ci95_lower_tps < 0
    assert inconclusive.directional_claim_supported is False


def _synthetic_breakthrough_results(
    block_effects: tuple[float, ...],
    *,
    secondary_block_effects: tuple[float, ...] | None = None,
    zero_mean: tuple[int, str, str] | None = None,
    p_changed_blocks: int = 5,
    ps_changed_blocks: int = 5,
    secondary_p_changed_blocks: int = 5,
    secondary_ps_changed_blocks: int = 5,
) -> dict[str, validation.SlotValidationResult]:
    assert len(block_effects) == 5
    if secondary_block_effects is None:
        secondary_block_effects = (0.0,) * 5
    assert len(secondary_block_effects) == 5
    manifest = load_frozen_manifest(MANIFEST_PATH)
    expected_by_pair = {
        (slot.block_id, slot.arm_code): slot
        for slot in validation._expected_slots(manifest)
    }
    results: dict[str, validation.SlotValidationResult] = {}
    for fanout in (5, 2):
        for block_index, effect in enumerate(block_effects, 1):
            cell_effect = (
                effect
                if fanout == 5
                else secondary_block_effects[block_index - 1]
            )
            block_id = f"n31-f{fanout}-b{block_index:02d}"
            for arm in ("00", "P", "S", "PS"):
                baseline = 100.0
                fault_evidence = 50.0
                epoch1 = 100.0
                epoch2 = (
                    100.0 * math.exp(cell_effect)
                    if arm in ("P", "PS")
                    else 100.0
                )
                if zero_mean == (block_index, arm, "baseline"):
                    baseline = 0.0
                if zero_mean == (block_index, arm, "fault_evidence"):
                    fault_evidence = 0.0
                if zero_mean == (block_index, arm, "epoch1_stable"):
                    epoch1 = 0.0
                if zero_mean == (block_index, arm, "epoch2_stable"):
                    epoch2 = 0.0
                means = {
                    "baseline": baseline,
                    "fault_evidence": fault_evidence,
                    "epoch1_stable": epoch1,
                    "epoch2_stable": epoch2,
                }
                metrics = tuple(
                    validation.PhaseMetric(
                        phase=phase,
                        transactions=int(mean * 30),
                        mean_tps=mean,
                        buckets_tps=(mean,) * 6,
                    )
                    for phase, mean in means.items()
                )
                slot_id = f"synthetic-{block_id}-{arm}"
                expected = expected_by_pair[(block_id, arm)]
                epoch1_roots = tuple(range(expected.q))
                changed_limit = (
                    (p_changed_blocks if fanout == 5 else secondary_p_changed_blocks)
                    if arm == "P"
                    else (
                        ps_changed_blocks
                        if fanout == 5
                        else secondary_ps_changed_blocks
                    )
                    if arm == "PS"
                    else 0
                )
                placement_changed = block_index <= changed_limit
                epoch2_roots = (
                    expected.fast_replica_ids
                    if placement_changed
                    else epoch1_roots
                )
                promoted = tuple(sorted(set(epoch2_roots) - set(epoch1_roots)))
                demoted = tuple(sorted(set(epoch1_roots) - set(epoch2_roots)))
                hierarchy_required = arm in {"P", "PS"}
                results[slot_id] = validation.SlotValidationResult(
                    slot_id=slot_id,
                    outcome="PASS",
                    reason=None,
                    block_id=block_id,
                    arm_code=arm,
                    replica_count=31,
                    initial_fanout=fanout,
                    metrics=metrics,
                    integrity_valid=True,
                    epoch1_roots=epoch1_roots,
                    epoch2_roots=epoch2_roots,
                    promoted_replica_ids=promoted,
                    demoted_replica_ids=demoted,
                    placement_changed=placement_changed,
                    hard_actor_ids=expected.actor_ids,
                    responsive_degraded_actor_ids=(
                        expected.responsive_degraded_actor_ids
                    ),
                    fast_replica_ids=expected.fast_replica_ids,
                    degraded_rank_proof_count=(
                        len(expected.responsive_degraded_actor_ids)
                        if hierarchy_required
                        else 0
                    ),
                    epoch1_degraded_root_proof_count=(
                        len(expected.responsive_degraded_actor_ids)
                        if hierarchy_required
                        else 0
                    ),
                    epoch1_degraded_internal_proof_count=(
                        len(expected.responsive_degraded_actor_ids)
                        if hierarchy_required
                        else 0
                    ),
                    epoch1_degraded_internal_cross_commit_witness_count=(
                        len(expected.responsive_degraded_actor_ids)
                        if fanout == 5 and hierarchy_required
                        else 0
                    ),
                    epoch2_constrained_leaf_proof_count=(
                        expected.f * expected.q if hierarchy_required else 0
                    ),
                    epoch2_fast_root_internal_position_proof_count=(
                        expected.q if hierarchy_required else 0
                    ),
                    epoch2_fast_root_internal_position_required_count=(
                        expected.q if hierarchy_required else 0
                    ),
                    full_hierarchy_gate_passed=(
                        True if hierarchy_required else None
                    ),
                )
    return results


def _with_placebo_component_effects(
    results: dict[str, validation.SlotValidationResult],
    *,
    p_effects: tuple[float, ...],
    ps_effects: tuple[float, ...],
    initial_fanout: int = 5,
) -> dict[str, validation.SlotValidationResult]:
    assert len(p_effects) == len(ps_effects) == 5
    updated = dict(results)
    by_arm = {"P": p_effects, "PS": ps_effects}
    for slot_id, result in results.items():
        if (
            result.initial_fanout != initial_fanout
            or result.arm_code not in by_arm
        ):
            continue
        block_index = int(result.block_id.rsplit("b", 1)[1]) - 1
        fault_mean = 50.0 * math.exp(by_arm[result.arm_code][block_index])
        updated[slot_id] = replace(
            result,
            metrics=tuple(
                replace(metric, mean_tps=fault_mean)
                if metric.phase == "fault_evidence"
                else metric
                for metric in result.metrics
            ),
        )
    return updated


def test_primary_throughput_estimate_is_the_exact_matched_log_ratio() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    expected_effects = (0.1, 0.2, 0.3, 0.4, 0.5)
    effects = validation._headline_effects(
        manifest,
        _synthetic_breakthrough_results(expected_effects),
    )

    assert effects is not None
    estimate = effects.primary_throughput_log_ratio
    assert estimate is not None
    assert estimate.block_effects_log_ratio == pytest.approx(expected_effects)
    assert estimate.mean_log_ratio == pytest.approx(0.3)
    assert estimate.sample_standard_deviation_log_ratio == pytest.approx(2.5**0.5 / 10)
    assert estimate.ci95_lower_log_ratio == pytest.approx(0.10367568385)
    assert estimate.ci95_upper_log_ratio == pytest.approx(0.49632431615)
    assert estimate.geometric_mean_ratio == pytest.approx(math.exp(0.3))
    assert estimate.geometric_mean_percent_change == pytest.approx(
        (math.exp(0.3) - 1.0) * 100.0
    )
    assert estimate.ci95_lower_ratio == pytest.approx(
        math.exp(estimate.ci95_lower_log_ratio)
    )
    assert estimate.ci95_upper_ratio == pytest.approx(
        math.exp(estimate.ci95_upper_log_ratio)
    )
    for placebo in (
        effects.pre_epoch1_placebo_p_log_ratio,
        effects.pre_epoch1_placebo_ps_log_ratio,
    ):
        assert placebo is not None
        assert placebo.block_effects_log_ratio == pytest.approx((0.0,) * 5)
        assert placebo.mean_log_ratio == pytest.approx(0.0)
        assert placebo.sample_standard_deviation_log_ratio == pytest.approx(0.0)
        assert placebo.ci90_lower_log_ratio == pytest.approx(0.0)
        assert placebo.ci90_upper_log_ratio == pytest.approx(0.0)
        assert placebo.equivalence_margin_log_ratio == pytest.approx(math.log(1.1))
        assert placebo.equivalence_supported is True


def test_secondary_f2_status_depends_on_primary_without_changing_primary() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)

    both = _synthetic_breakthrough_results(
        (0.2,) * 5,
        secondary_block_effects=(0.2,) * 5,
    )
    both_effects = validation._headline_effects(manifest, both)
    assert both_effects is not None
    primary_both = validation._breakthrough_verdict(
        manifest,
        both,
        campaign_outcome="PASS",
        effects=both_effects,
    )
    secondary_both = validation._secondary_placement_verdict(
        manifest,
        both,
        primary=primary_both,
        effects=both_effects,
    )
    assert primary_both.status == "SUPPORTED"
    assert secondary_both is not None
    assert secondary_both.status == "SUPPORTED"
    assert secondary_both.structural_validated_slot_count == 10
    assert secondary_both.placement_changed_p_block_count == 5
    assert secondary_both.placement_changed_ps_block_count == 5
    assert secondary_both.throughput_positive_block_count == 5
    assert secondary_both.placebo_equivalence_rule_passed is True

    only_f5 = _synthetic_breakthrough_results((0.2,) * 5)
    only_f5_effects = validation._headline_effects(manifest, only_f5)
    assert only_f5_effects is not None
    primary_only_f5 = validation._breakthrough_verdict(
        manifest,
        only_f5,
        campaign_outcome="PASS",
        effects=only_f5_effects,
    )
    secondary_only_f5 = validation._secondary_placement_verdict(
        manifest,
        only_f5,
        primary=primary_only_f5,
        effects=only_f5_effects,
    )
    assert primary_only_f5.status == "SUPPORTED"
    assert secondary_only_f5 is not None
    assert secondary_only_f5.status == "NOT_SUPPORTED"
    assert secondary_only_f5.throughput_rule_passed is False

    only_f2 = _synthetic_breakthrough_results(
        (-0.1,) * 5,
        secondary_block_effects=(0.2,) * 5,
    )
    only_f2_effects = validation._headline_effects(manifest, only_f2)
    assert only_f2_effects is not None
    primary_only_f2 = validation._breakthrough_verdict(
        manifest,
        only_f2,
        campaign_outcome="PASS",
        effects=only_f2_effects,
    )
    secondary_only_f2 = validation._secondary_placement_verdict(
        manifest,
        only_f2,
        primary=primary_only_f2,
        effects=only_f2_effects,
    )
    assert primary_only_f2.status == "NOT_SUPPORTED"
    assert secondary_only_f2 is not None
    assert secondary_only_f2.status == "DESCRIPTIVE_ONLY"
    assert secondary_only_f2.throughput_rule_passed is True


def test_secondary_f2_requires_its_own_structural_and_placebo_gates() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _synthetic_breakthrough_results(
        (0.2,) * 5,
        secondary_block_effects=(0.2,) * 5,
    )
    structurally_invalid = dict(results)
    slot_id = "synthetic-n31-f2-b05-P"
    structurally_invalid[slot_id] = replace(
        structurally_invalid[slot_id],
        full_hierarchy_gate_passed=False,
    )
    structural_effects = validation._headline_effects(
        manifest,
        structurally_invalid,
    )
    assert structural_effects is not None
    primary = validation._breakthrough_verdict(
        manifest,
        structurally_invalid,
        campaign_outcome="PASS",
        effects=structural_effects,
    )
    structural_secondary = validation._secondary_placement_verdict(
        manifest,
        structurally_invalid,
        primary=primary,
        effects=structural_effects,
    )
    assert primary.status == "SUPPORTED"
    assert structural_secondary is not None
    assert structural_secondary.status == "NOT_SUPPORTED"
    assert structural_secondary.structural_validated_slot_count == 9

    placebo_invalid = _with_placebo_component_effects(
        results,
        p_effects=(math.log(1.2),) * 5,
        ps_effects=(0.0,) * 5,
        initial_fanout=2,
    )
    placebo_effects = validation._headline_effects(manifest, placebo_invalid)
    assert placebo_effects is not None
    primary = validation._breakthrough_verdict(
        manifest,
        placebo_invalid,
        campaign_outcome="PASS",
        effects=placebo_effects,
    )
    placebo_secondary = validation._secondary_placement_verdict(
        manifest,
        placebo_invalid,
        primary=primary,
        effects=placebo_effects,
    )
    assert primary.status == "SUPPORTED"
    assert placebo_secondary is not None
    assert placebo_secondary.status == "NOT_SUPPORTED"
    assert placebo_secondary.placebo_p_equivalence_rule_passed is False
    assert placebo_secondary.placebo_ps_equivalence_rule_passed is True


def test_v8_breakthrough_hierarchy_does_not_require_v9_internal_witnesses() -> None:
    manifest = load_frozen_manifest(V8_MANIFEST_PATH)
    results = {
        slot_id: replace(
            result,
            epoch1_degraded_internal_cross_commit_witness_count=0,
        )
        for slot_id, result in _synthetic_breakthrough_results((0.2,) * 5).items()
    }

    summary = validation._breakthrough_hierarchy_summary(manifest, results)

    assert summary["validated_slot_count"] == 10
    assert summary["epoch1_degraded_internal_cross_commit_required_count"] == 0
    assert summary["full_hierarchy_gate_passed"] is True


def test_breakthrough_requires_lower_log_bound_and_four_positive_blocks() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    four_positive = _synthetic_breakthrough_results(
        (0.2, 0.2, 0.2, 0.2, -0.001),
        p_changed_blocks=5,
        ps_changed_blocks=5,
    )
    effects = validation._headline_effects(manifest, four_positive)
    assert effects is not None
    estimate = effects.primary_throughput_log_ratio
    assert estimate is not None
    assert estimate.ci95_lower_log_ratio > 0
    assert estimate.positive_block_count == 4
    assert estimate.directional_claim_supported is True

    supported = validation._breakthrough_verdict(
        manifest,
        four_positive,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert supported.status == "SUPPORTED"
    assert supported.structural_required_slot_count == 10
    assert supported.structural_validated_slot_count == 10
    assert supported.structural_gate_passed is True
    assert (
        supported.epoch1_degraded_internal_cross_commit_required_count == 70
    )
    assert (
        supported.epoch1_degraded_internal_cross_commit_validated_count == 70
    )
    assert supported.placement_changed_p_block_count == 5
    assert supported.placement_changed_ps_block_count == 5
    assert supported.realized_placement_gate_passed is True
    assert supported.throughput_rule_passed is True
    assert supported.placebo_p_estimate_available is True
    assert supported.placebo_p_ci90_lower_log_ratio == pytest.approx(0.0)
    assert supported.placebo_p_ci90_upper_log_ratio == pytest.approx(0.0)
    assert supported.placebo_p_equivalence_rule_passed is True
    assert supported.placebo_ps_estimate_available is True
    assert supported.placebo_ps_ci90_lower_log_ratio == pytest.approx(0.0)
    assert supported.placebo_ps_ci90_upper_log_ratio == pytest.approx(0.0)
    assert supported.placebo_ps_equivalence_rule_passed is True
    assert supported.placebo_equivalence_rule_passed is True
    assert supported.failed_requirements == ()

    missing_structural_slot = dict(four_positive)
    missing_structural_slot.pop("synthetic-n31-f5-b05-P")
    structural_failure = validation._breakthrough_verdict(
        manifest,
        missing_structural_slot,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert structural_failure.status == "NOT_SUPPORTED"
    assert structural_failure.structural_validated_slot_count == 9
    assert (
        structural_failure.epoch1_degraded_internal_cross_commit_validated_count
        == 63
    )
    assert structural_failure.structural_gate_passed is False
    assert structural_failure.realized_placement_gate_passed is False
    assert structural_failure.failed_requirements == (
        "structural gate validated 9 of 10 required slots",
        "P realized placement changed in 4 of 5 blocks; requires at least 5",
    )

    three_positive = _synthetic_breakthrough_results(
        (0.2, 0.2, 0.2, -0.001, -0.001)
    )
    unsupported_effects = validation._headline_effects(manifest, three_positive)
    assert unsupported_effects is not None
    unsupported = validation._breakthrough_verdict(
        manifest,
        three_positive,
        campaign_outcome="PASS",
        effects=unsupported_effects,
    )
    assert unsupported.status == "NOT_SUPPORTED"
    assert unsupported.throughput_positive_block_count == 3
    assert unsupported.throughput_rule_passed is False
    assert unsupported.failed_requirements


def test_relative_gain_cannot_hide_absolute_p_and_ps_throughput_decline() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _synthetic_breakthrough_results((0.2,) * 5)
    for slot_id, result in tuple(results.items()):
        if result.initial_fanout != 5:
            continue
        phase_means = {
            "baseline": 100.0,
            "fault_evidence": 50.0,
            "epoch1_stable": 100.0,
            "epoch2_stable": (
                90.0 if result.arm_code in {"P", "PS"} else 50.0
            ),
        }
        results[slot_id] = replace(
            result,
            metrics=tuple(
                replace(metric, mean_tps=phase_means[metric.phase])
                for metric in result.metrics
            ),
        )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    assert effects.primary_throughput_log_ratio is not None
    assert effects.primary_throughput_log_ratio.directional_claim_supported is True
    assert effects.optimization_gain.mean_tps == pytest.approx(-10.0)

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.throughput_rule_passed is True
    assert verdict.optimization_gain_rule_passed is False
    assert verdict.p_absolute_optimization_positive_block_count == 0
    assert verdict.ps_absolute_optimization_positive_block_count == 0
    assert verdict.per_arm_absolute_optimization_rule_passed is False
    assert verdict.absolute_sequence_gate_passed is False
    assert any(
        "absolute optimization-gain" in failure
        for failure in verdict.failed_requirements
    )
    assert validation._campaign_figure_eligible("PASS", effects) is True


def test_pre_epoch1_arm_specific_trajectory_blocks_breakthrough() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    arm_pretrend = (math.log(1.2),) * 5
    results = _with_placebo_component_effects(
        _synthetic_breakthrough_results((0.2,) * 5),
        p_effects=arm_pretrend,
        ps_effects=arm_pretrend,
    )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    assert effects.primary_throughput_log_ratio is not None
    assert effects.primary_throughput_log_ratio.directional_claim_supported
    for placebo in (
        effects.pre_epoch1_placebo_p_log_ratio,
        effects.pre_epoch1_placebo_ps_log_ratio,
    ):
        assert placebo is not None
        assert placebo.mean_log_ratio == pytest.approx(math.log(1.2))
        assert placebo.ci90_lower_log_ratio == pytest.approx(math.log(1.2))
        assert placebo.ci90_upper_log_ratio == pytest.approx(math.log(1.2))
        assert placebo.equivalence_supported is False

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.throughput_rule_passed is True
    assert verdict.absolute_sequence_gate_passed is True
    assert verdict.placebo_p_estimate_available is True
    assert verdict.placebo_p_equivalence_rule_passed is False
    assert verdict.placebo_ps_estimate_available is True
    assert verdict.placebo_ps_equivalence_rule_passed is False
    assert verdict.placebo_equivalence_rule_passed is False
    assert verdict.failed_requirements == (
        "P/00 pre-Epoch1 placebo 90% log-ratio interval is not strictly "
        "within the prespecified equivalence margin",
        "PS/S pre-Epoch1 placebo 90% log-ratio interval is not strictly "
        "within the prespecified equivalence margin",
    )


def test_both_placebo_components_accept_small_nonzero_matched_variation() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    small = (-0.02, -0.01, 0.0, 0.01, 0.02)
    results = _with_placebo_component_effects(
        _synthetic_breakthrough_results((0.2,) * 5),
        p_effects=small,
        ps_effects=tuple(reversed(small)),
    )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    for placebo in (
        effects.pre_epoch1_placebo_p_log_ratio,
        effects.pre_epoch1_placebo_ps_log_ratio,
    ):
        assert placebo is not None
        assert placebo.sample_standard_deviation_log_ratio > 0
        assert placebo.ci90_lower_log_ratio > -math.log(1.1)
        assert placebo.ci90_upper_log_ratio < math.log(1.1)
        assert placebo.equivalence_supported is True

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "SUPPORTED"
    assert verdict.placebo_equivalence_rule_passed is True


def test_opposite_placebo_components_cannot_cancel_into_support() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _with_placebo_component_effects(
        _synthetic_breakthrough_results((0.2,) * 5),
        p_effects=(math.log(2.0),) * 5,
        ps_effects=(math.log(0.5),) * 5,
    )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    assert effects.fault_drop.ci95_lower_tps == pytest.approx(37.5)
    assert effects.pre_epoch1_placebo_p_log_ratio is not None
    assert effects.pre_epoch1_placebo_ps_log_ratio is not None
    assert effects.pre_epoch1_placebo_p_log_ratio.mean_log_ratio == pytest.approx(
        math.log(2.0)
    )
    assert effects.pre_epoch1_placebo_ps_log_ratio.mean_log_ratio == pytest.approx(
        math.log(0.5)
    )

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.throughput_rule_passed is True
    assert verdict.absolute_sequence_gate_passed is True
    assert verdict.placebo_p_equivalence_rule_passed is False
    assert verdict.placebo_ps_equivalence_rule_passed is False
    assert verdict.placebo_equivalence_rule_passed is False


def test_placebo_rejects_ci_crossing_margin_despite_zero_mean() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _with_placebo_component_effects(
        _synthetic_breakthrough_results((0.2,) * 5),
        p_effects=(-0.2, 0.0, 0.0, 0.0, 0.2),
        ps_effects=(0.0,) * 5,
    )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    placebo_p = effects.pre_epoch1_placebo_p_log_ratio
    assert placebo_p is not None
    assert placebo_p.mean_log_ratio == pytest.approx(0.0)
    assert placebo_p.ci90_lower_log_ratio < -math.log(1.1)
    assert placebo_p.ci90_upper_log_ratio > math.log(1.1)
    assert placebo_p.equivalence_supported is False

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.placebo_p_equivalence_rule_passed is False
    assert verdict.placebo_ps_equivalence_rule_passed is True


@pytest.mark.parametrize("boundary", (math.log(1.1), -math.log(1.1)))
def test_placebo_rejects_exact_equivalence_margin(boundary: float) -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _with_placebo_component_effects(
        _synthetic_breakthrough_results((0.2,) * 5),
        p_effects=(boundary,) * 5,
        ps_effects=(0.0,) * 5,
    )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    placebo_p = effects.pre_epoch1_placebo_p_log_ratio
    assert placebo_p is not None
    assert placebo_p.ci90_lower_log_ratio == pytest.approx(boundary)
    assert placebo_p.ci90_upper_log_ratio == pytest.approx(boundary)
    assert placebo_p.equivalence_supported is False

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.placebo_p_equivalence_rule_passed is False
    assert verdict.placebo_equivalence_rule_passed is False


def test_nonpositive_placebo_mean_is_unavailable_and_not_supported() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _synthetic_breakthrough_results(
        (0.2,) * 5,
        zero_mean=(1, "P", "fault_evidence"),
    )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    assert effects.pre_epoch1_placebo_p_log_ratio is None
    assert effects.pre_epoch1_placebo_ps_log_ratio is not None

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.placebo_p_estimate_available is False
    assert verdict.placebo_p_equivalence_rule_passed is None
    assert verdict.placebo_ps_estimate_available is True
    assert verdict.placebo_ps_equivalence_rule_passed is True
    assert verdict.placebo_equivalence_rule_passed is False
    assert any(
        "P/00 pre-Epoch1 placebo requires strictly positive" in failure
        for failure in verdict.failed_requirements
    )


def test_breakthrough_requires_realized_root_changes_in_each_adaptive_arm() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    throughput_passes = (0.2,) * 5

    unchanged = _synthetic_breakthrough_results(
        throughput_passes,
        p_changed_blocks=0,
        ps_changed_blocks=0,
    )
    unchanged_effects = validation._headline_effects(manifest, unchanged)
    assert unchanged_effects is not None
    assert unchanged_effects.primary_throughput_log_ratio is not None
    assert unchanged_effects.primary_throughput_log_ratio.directional_claim_supported
    unchanged_verdict = validation._breakthrough_verdict(
        manifest,
        unchanged,
        campaign_outcome="PASS",
        effects=unchanged_effects,
    )
    assert unchanged_verdict.status == "NOT_SUPPORTED"
    assert unchanged_verdict.placement_changed_p_block_count == 0
    assert unchanged_verdict.placement_changed_ps_block_count == 0
    assert unchanged_verdict.realized_placement_gate_passed is False
    assert validation._campaign_figure_eligible("PASS", unchanged_effects) is True

    one_arm_short = _synthetic_breakthrough_results(
        throughput_passes,
        p_changed_blocks=4,
        ps_changed_blocks=5,
    )
    one_arm_short_effects = validation._headline_effects(manifest, one_arm_short)
    assert one_arm_short_effects is not None
    one_arm_short_verdict = validation._breakthrough_verdict(
        manifest,
        one_arm_short,
        campaign_outcome="PASS",
        effects=one_arm_short_effects,
    )
    assert one_arm_short_verdict.status == "NOT_SUPPORTED"
    assert one_arm_short_verdict.placement_changed_p_block_count == 4
    assert one_arm_short_verdict.placement_changed_ps_block_count == 5
    assert one_arm_short_verdict.failed_requirements == (
        "P realized placement changed in 4 of 5 blocks; requires at least 5",
    )


def test_breakthrough_verdict_is_not_evaluable_without_complete_valid_data() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _synthetic_breakthrough_results((0.2,) * 5)
    effects = validation._headline_effects(manifest, results)
    assert effects is not None

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="INCOMPLETE",
        effects=effects,
    )
    assert verdict.status == "NOT_EVALUABLE"
    assert verdict.structural_validated_slot_count == 10
    assert verdict.throughput_rule_passed is None
    assert verdict.failed_requirements == (
        "campaign lacks complete valid data for the prespecified scope",
    )


def test_zero_mean_rejects_breakthrough_without_invalidating_campaign_evidence() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _synthetic_breakthrough_results(
        (0.2,) * 5,
        zero_mean=(1, "P", "epoch1_stable"),
    )
    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    assert effects.primary_throughput_log_ratio is None

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.throughput_estimate_available is False
    assert "strictly positive" in verdict.failed_requirements[-1]
    assert validation._campaign_figure_eligible("PASS", effects) is True

    negative_results = _synthetic_breakthrough_results((-0.1,) * 5)
    negative_effects = validation._headline_effects(manifest, negative_results)
    assert negative_effects is not None
    negative = validation._breakthrough_verdict(
        manifest,
        negative_results,
        campaign_outcome="PASS",
        effects=negative_effects,
    )
    assert negative.status == "NOT_SUPPORTED"
    assert validation._campaign_figure_eligible("PASS", negative_effects) is True


def _campaign_chronology_rows(
    monotonic_ns: tuple[object, ...] = (1, 2, 3, 4),
    recorded_utc: tuple[str, ...] = (
        "2026-08-04T00:00:01+00:00",
        "2026-08-04T00:00:02+00:00",
        "2026-08-04T00:00:03+00:00",
        "2026-08-04T00:00:04+00:00",
    ),
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "recorded_monotonic_ns": timestamp,
            "recorded_utc": wall_time,
        }
        for timestamp, wall_time in zip(monotonic_ns, recorded_utc)
    )


def test_campaign_chronology_accepts_strict_full_monotonic_chain() -> None:
    validation._validate_campaign_chronology(
        approved_utc="2026-08-04T00:00:00+00:00",
        ledger_rows=_campaign_chronology_rows(),
        completed_utc="2026-08-04T00:00:04+00:00",
    )


@pytest.mark.parametrize(
    "monotonic_ns",
    (
        (1, 2, 2, 4),
        (1, 2, 1, 4),
    ),
)
def test_campaign_chronology_rejects_equal_or_regressing_monotonic_chain(
    monotonic_ns: tuple[object, ...],
) -> None:
    with pytest.raises(FactorialValidationError, match="strictly increasing"):
        validation._validate_campaign_chronology(
            approved_utc="2026-08-04T00:00:00+00:00",
            ledger_rows=_campaign_chronology_rows(monotonic_ns),
            completed_utc="2026-08-04T00:00:04+00:00",
        )


@pytest.mark.parametrize("invalid", (0, True, 1.0, "1"))
def test_campaign_chronology_requires_exact_positive_monotonic_integers(
    invalid: object,
) -> None:
    with pytest.raises(FactorialValidationError, match=r"integer >= 1"):
        validation._validate_campaign_chronology(
            approved_utc="2026-08-04T00:00:00+00:00",
            ledger_rows=_campaign_chronology_rows((1, invalid, 3, 4)),
            completed_utc="2026-08-04T00:00:04+00:00",
        )


@pytest.mark.parametrize(
    "recorded_utc",
    (
        (
            "2026-08-03T23:59:59+00:00",
            "2026-08-04T00:00:02+00:00",
            "2026-08-04T00:00:03+00:00",
            "2026-08-04T00:00:04+00:00",
        ),
        (
            "2026-08-04T00:00:01+00:00",
            "2026-08-04T00:00:00+00:00",
            "2026-08-04T00:00:03+00:00",
            "2026-08-04T00:00:04+00:00",
        ),
    ),
)
def test_campaign_chronology_rejects_preapproval_or_regressing_row_utc(
    recorded_utc: tuple[str, ...],
) -> None:
    with pytest.raises(FactorialValidationError, match="UTC chronology"):
        validation._validate_campaign_chronology(
            approved_utc="2026-08-04T00:00:00+00:00",
            ledger_rows=_campaign_chronology_rows(recorded_utc=recorded_utc),
            completed_utc="2026-08-04T00:00:04+00:00",
        )


@pytest.mark.parametrize(
    "completed_utc",
    (
        "2026-08-03T23:59:59+00:00",
        "2026-08-04T00:00:03+00:00",
    ),
)
def test_campaign_chronology_rejects_summary_before_approval_or_terminal(
    completed_utc: str,
) -> None:
    with pytest.raises(FactorialValidationError, match="completion UTC precedes"):
        validation._validate_campaign_chronology(
            approved_utc="2026-08-04T00:00:00+00:00",
            ledger_rows=_campaign_chronology_rows(),
            completed_utc=completed_utc,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("approved_utc", "2026-08-04T00:00:00"),
        ("recorded_utc", "2026-08-04T00:00:01"),
        ("completed_utc", "2026-08-04T00:00:04"),
    ),
)
def test_campaign_chronology_requires_aware_wall_timestamps(
    field: str,
    value: str,
) -> None:
    arguments: dict[str, object] = {
        "approved_utc": "2026-08-04T00:00:00+00:00",
        "ledger_rows": _campaign_chronology_rows(),
        "completed_utc": "2026-08-04T00:00:04+00:00",
    }
    if field == "recorded_utc":
        rows = list(arguments["ledger_rows"])
        rows[0] = {**rows[0], "recorded_utc": value}
        arguments["ledger_rows"] = tuple(rows)
    else:
        arguments[field] = value
    with pytest.raises(FactorialValidationError, match="timezone-aware"):
        validation._validate_campaign_chronology(**arguments)


def test_campaign_ledger_replays_one_shot_prefix_and_rejects_continuation(
    tmp_path: Path,
) -> None:
    from experiments.adaptive.kauri_experiment import factorial_execution as execution

    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime_plan = build_factorial_runtime(plan)
    runtime = runtime_plan.as_document()
    expected_slots = validation._expected_slots(manifest)
    ordered = tuple(sorted(expected_slots, key=lambda slot: slot.execution_ordinal))
    runtime_by_id = {slot.slot_id: slot for slot in runtime_plan.slots}
    static_artifacts = {
        validation.MANIFEST_FILENAME: MANIFEST_PATH.read_bytes(),
        validation.PLAN_FILENAME: canonical_plan_bytes(plan),
        validation.RUNTIME_FILENAME: canonical_runtime_bytes(runtime_plan),
    }
    authorization_bytes = execution.build_execution_authorization_receipt(
        scope="shape25_campaign",
        approval_reference="unit-test thesis-author approval",
        approved_utc="2026-08-04T00:00:00+00:00",
        kauri_revision="cd" * 20,
        slot_ids=tuple(slot.slot_id for slot in plan.slots),
        result_root=manifest.results_root,
        static_artifacts=static_artifacts,
        build_provenance_sha256=hashlib.sha256(
            _canonical({"revision": "cd" * 20})
        ).hexdigest(),
    )
    authorization = json.loads(authorization_bytes)
    root = tmp_path / "campaign"
    root.mkdir()
    (root / validation.CAMPAIGN_AUTHORIZATION_FILENAME).write_bytes(
        authorization_bytes
    )
    build_bytes = _canonical({"revision": "cd" * 20})
    build_digest = hashlib.sha256(build_bytes).hexdigest()
    contract = {
        "schema_version": 1,
        "campaign_id": runtime_plan.runtime_id,
        "manifest_id": runtime_plan.manifest_id,
        "manifest_sha256": validation.V24_MANIFEST_SHA256,
        "plan_sha256": validation.V24_PLAN_SHA256,
        "runtime_sha256": validation.V24_RUNTIME_SHA256,
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": hashlib.sha256(authorization_bytes).hexdigest(),
        "kauri_revision": "cd" * 20,
        "build_provenance_sha256": build_digest,
        "execution_mode": "fixed_sequential",
        "automatic_retries": 0,
        "replacement_policy": "none",
        "outcome_dependent_order": False,
        "expected_slot_count": len(ordered),
        "execution_schedule": [
            {
                "execution_ordinal": slot.execution_ordinal,
                "slot_id": slot.slot_id,
                "block_id": slot.block_id,
                "arm_code": slot.arm_code,
            }
            for slot in ordered
        ],
    }
    contract_bytes = _canonical(contract)
    (root / validation.CAMPAIGN_CONTRACT_FILENAME).write_bytes(contract_bytes)
    contract_digest = hashlib.sha256(contract_bytes).hexdigest()

    def preserve_minimal_slot(expected: validation._ExpectedSlot) -> tuple[Path, str]:
        spec = runtime_by_id[expected.slot_id]
        recovered = root / expected.slot_id
        (recovered / "runtime").mkdir(parents=True)
        original = tmp_path / "removed" / "Kauri" / spec.result_path
        (recovered / validation.SLOT_FILENAME).write_bytes(
            _canonical(
                {
                    "replica_argv": [
                        {
                            "replica_id": 0,
                            "argv": [
                                "hotstuff-app",
                                "--conf",
                                f"{original}/runtime/main.conf",
                            ],
                        }
                    ]
                }
            )
        )
        (recovered / validation.AUTHORIZATION_FILENAME).write_bytes(
            authorization_bytes
        )
        (recovered / validation.BUILD_PROVENANCE_FILENAME).write_bytes(build_bytes)
        return recovered, str(original)

    first = ordered[0]
    first_path, first_original = preserve_minimal_slot(first)
    result = validation.SlotValidationResult(
        slot_id=first.slot_id,
        outcome="INCOMPLETE",
        reason="forced unit-test stop",
        block_id=first.block_id,
        arm_code=first.arm_code,
        replica_count=first.replica_count,
        initial_fanout=first.initial_fanout,
        integrity_valid=False,
        campaign_member=True,
    )

    def common(expected: validation._ExpectedSlot, state: str, timestamp: int) -> dict[str, object]:
        return {
            "schema_version": 1,
            "campaign_id": runtime_plan.runtime_id,
            "manifest_sha256": validation.V24_MANIFEST_SHA256,
            "source_plan_sha256": validation.V24_PLAN_SHA256,
            "runtime_sha256": validation.V24_RUNTIME_SHA256,
            "contract_sha256": contract_digest,
            "authorization_id": authorization["authorization_id"],
            "authorization_sha256": hashlib.sha256(authorization_bytes).hexdigest(),
            "kauri_revision": "cd" * 20,
            "execution_ordinal": expected.execution_ordinal,
            "slot_id": expected.slot_id,
            "block_id": expected.block_id,
            "arm_code": expected.arm_code,
            "attempt_ordinal": 1,
            "automatic_retries": 0,
            "replacement_policy": "none",
            "state": state,
            "recorded_utc": "2026-08-04T00:00:00+00:00",
            "recorded_monotonic_ns": timestamp,
        }

    rows = [
        {
            **common(first, "STARTED", 1),
            "slot_directory": first_original,
            "preflight_revision": "cd" * 20,
            "preflight_free_bytes": runtime_plan.minimum_free_bytes,
            "build_provenance_sha256": build_digest,
        },
        {
            **common(first, "TERMINAL", 2),
            "slot_directory": first_original,
            "execution_outcome": "INCOMPLETE",
            "execution_reason": "forced unit-test stop",
            "launch_count": 0,
            "validation": {
                "outcome": result.outcome,
                "reason": result.reason,
                "integrity_valid": result.integrity_valid,
                "campaign_member": result.campaign_member,
                "figure_eligible": result.figure_eligible,
            },
        },
    ]
    ledger_path = root / validation.CAMPAIGN_LEDGER_FILENAME
    ledger_path.write_bytes(b"".join(_canonical(row) for row in rows))
    summary = {
        "schema_version": 1,
        "campaign_id": runtime_plan.runtime_id,
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": hashlib.sha256(authorization_bytes).hexdigest(),
        "contract_sha256": contract_digest,
        "ledger_sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
        "expected_slot_count": len(ordered),
        "attempted_slot_count": 1,
        "next_execution_ordinal": 2,
        "execution_complete": False,
        "stopped_reason": "forced unit-test stop",
        "completed_utc": "2026-08-04T00:00:00+00:00",
    }
    summary_path = root / validation.CAMPAIGN_SUMMARY_FILENAME
    summary_path.write_bytes(_canonical(summary))
    assert validation._validate_campaign_execution_ledger(
        root,
        manifest=manifest,
        plan=json.loads(canonical_plan_bytes(plan)),
        runtime=runtime,
        expected_slots=expected_slots,
        actual_by_id={first.slot_id: first_path},
        results_by_id={first.slot_id: result},
        campaign_outcome="INCOMPLETE",
    ) == 1

    equal_monotonic_rows = [dict(row) for row in rows]
    equal_monotonic_rows[1]["recorded_monotonic_ns"] = 1
    ledger_path.write_bytes(
        b"".join(_canonical(row) for row in equal_monotonic_rows)
    )
    equal_monotonic_summary = {
        **summary,
        "ledger_sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
    }
    summary_path.write_bytes(_canonical(equal_monotonic_summary))
    with pytest.raises(FactorialValidationError, match="strictly increasing"):
        validation._validate_campaign_execution_ledger(
            root,
            manifest=manifest,
            plan=json.loads(canonical_plan_bytes(plan)),
            runtime=runtime,
            expected_slots=expected_slots,
            actual_by_id={first.slot_id: first_path},
            results_by_id={first.slot_id: result},
            campaign_outcome="INCOMPLETE",
        )

    terminal_after_summary_rows = [dict(row) for row in rows]
    terminal_after_summary_rows[0]["recorded_utc"] = (
        "2026-08-04T00:00:01+00:00"
    )
    terminal_after_summary_rows[1]["recorded_utc"] = (
        "2026-08-04T00:00:02+00:00"
    )
    ledger_path.write_bytes(
        b"".join(_canonical(row) for row in terminal_after_summary_rows)
    )
    early_summary = {
        **summary,
        "ledger_sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
        "completed_utc": "2026-08-04T00:00:01+00:00",
    }
    summary_path.write_bytes(_canonical(early_summary))
    with pytest.raises(FactorialValidationError, match="completion UTC precedes"):
        validation._validate_campaign_execution_ledger(
            root,
            manifest=manifest,
            plan=json.loads(canonical_plan_bytes(plan)),
            runtime=runtime,
            expected_slots=expected_slots,
            actual_by_id={first.slot_id: first_path},
            results_by_id={first.slot_id: result},
            campaign_outcome="INCOMPLETE",
        )

    ledger_path.write_bytes(b"".join(_canonical(row) for row in rows))
    summary_path.write_bytes(_canonical(summary))

    typed_contract = dict(contract)
    typed_contract["outcome_dependent_order"] = 0
    (root / validation.CAMPAIGN_CONTRACT_FILENAME).write_bytes(
        _canonical(typed_contract)
    )
    with pytest.raises(FactorialValidationError, match="frozen schedule"):
        validation._validate_campaign_execution_ledger(
            root,
            manifest=manifest,
            plan=json.loads(canonical_plan_bytes(plan)),
            runtime=runtime,
            expected_slots=expected_slots,
            actual_by_id={first.slot_id: first_path},
            results_by_id={first.slot_id: result},
            campaign_outcome="INCOMPLETE",
        )
    (root / validation.CAMPAIGN_CONTRACT_FILENAME).write_bytes(contract_bytes)

    typed_rows = [dict(row) for row in rows]
    typed_rows[0]["schema_version"] = True
    ledger_path.write_bytes(b"".join(_canonical(row) for row in typed_rows))
    with pytest.raises(FactorialValidationError, match="identity/order"):
        validation._validate_campaign_execution_ledger(
            root,
            manifest=manifest,
            plan=json.loads(canonical_plan_bytes(plan)),
            runtime=runtime,
            expected_slots=expected_slots,
            actual_by_id={first.slot_id: first_path},
            results_by_id={first.slot_id: result},
            campaign_outcome="INCOMPLETE",
        )
    ledger_path.write_bytes(b"".join(_canonical(row) for row in rows))

    typed_summary = dict(summary)
    typed_summary["expected_slot_count"] = True
    typed_summary["ledger_sha256"] = hashlib.sha256(
        ledger_path.read_bytes()
    ).hexdigest()
    summary_path.write_bytes(_canonical(typed_summary))
    with pytest.raises(FactorialValidationError, match="summary"):
        validation._validate_campaign_execution_ledger(
            root,
            manifest=manifest,
            plan=json.loads(canonical_plan_bytes(plan)),
            runtime=runtime,
            expected_slots=expected_slots,
            actual_by_id={first.slot_id: first_path},
            results_by_id={first.slot_id: result},
            campaign_outcome="INCOMPLETE",
        )
    summary_path.write_bytes(_canonical(summary))

    summary_path.unlink()
    with pytest.raises(FactorialValidationError, match="campaign-execution-summary"):
        validation._validate_campaign_execution_ledger(
            root,
            manifest=manifest,
            plan=json.loads(canonical_plan_bytes(plan)),
            runtime=runtime,
            expected_slots=expected_slots,
            actual_by_id={first.slot_id: first_path},
            results_by_id={first.slot_id: result},
            campaign_outcome="INCOMPLETE",
        )
    summary_path.write_bytes(_canonical(summary))

    second = ordered[1]
    _, second_original = preserve_minimal_slot(second)
    rows.extend(
        (
            {
                **common(second, "STARTED", 3),
                "slot_directory": second_original,
                "preflight_revision": "cd" * 20,
                "preflight_free_bytes": runtime_plan.minimum_free_bytes,
                "build_provenance_sha256": build_digest,
            },
            {
                **common(second, "TERMINAL", 4),
                "slot_directory": second_original,
                "execution_outcome": "PASS",
                "execution_reason": None,
                "launch_count": second.replica_count + 1,
                "validation": {
                    "outcome": "PASS",
                    "reason": None,
                    "integrity_valid": True,
                    "campaign_member": True,
                    "figure_eligible": True,
                },
            },
        )
    )
    ledger_path.write_bytes(b"".join(_canonical(row) for row in rows))
    with pytest.raises(FactorialValidationError, match="continued after"):
        validation._validate_campaign_execution_ledger(
            root,
            manifest=manifest,
            plan=json.loads(canonical_plan_bytes(plan)),
            runtime=runtime,
            expected_slots=expected_slots,
            actual_by_id={first.slot_id: first_path},
            results_by_id={first.slot_id: result},
            campaign_outcome="INCOMPLETE",
        )
