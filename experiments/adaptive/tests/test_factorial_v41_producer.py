"""Producer contract for the v41 marker-window binding roll."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive import run_shape_factorial_campaign as cli
from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment import factorial_manifest as manifest_module
from experiments.adaptive.kauri_experiment import factorial_runtime as runtime_module


REPOSITORY = Path(__file__).resolve().parents[3]
V41_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v41.json"
)
V40_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v40.json"
)
V40_SIX = (
    "a058ad5ac30aebbf866ba96c3ca60411b831875c36e198d6f6a4de08fbfa476d",
    "6669bb5c4165eb83e26771348cb2ab06723782a7b3a111342ed56f9740746e5a",
    "561f6f74fd27e165eed38d24e1aeca6c6fd4acc3f93743a7b2387e8e8da2e8d7",
    "5286f8815b1812afa13f39d84e872af8dbfe78d675f53e8a02ea1c56e5acccdc",
    "4522bf796e10f67d2394ceab3399b5755ee41914dc30375ce0f87a8e0e5459eb",
    "70847285deb39a40addbbd1cc118fff0e3ed97498cb300e1a45564cc13f7d79e",
)
V41_SIX = (
    "6f9c5b53c465afd56d467421437dadd238928f7550c4e589a8a18988fd8441d5",
    "23c8be6ccd4059868b0822cfb0dd6269d17b164ed37241ac2e5082312df5e2ed",
    "f5e0cfcfe7e0e57b33358b4c0603acb9547dfc61900d3b47f5c0dbd033ffee08",
    "e52d8641add32d484ffa829b6e7f740fd22388a93930a6f345c2bea9fa466d4c",
    "0d74f500c32d9a210148a75ee8921b2a0d62737c17169bb3faa557848d69429d",
    "e27e4848510b4c92440edebc2a658a38a5d69464adab795219f09482af3154e6",
)
V6 = (
    "exact_excluded_repair_smoke_source_fault_window_duration_450s_effective_"
    "fault_window_duration_360s_hard_deadline_650s_post_fault_cycle1_selection_"
    "observation_grace_5s_with_exact_cycle1_manager_selection_gate_bound_to_the_"
    "materialized_shared_clock_fault_end_plus_the_frozen_observation_grace_as_an_"
    "exclusive_lower_bound_and_exact_cycle1_inherited_wait_exempt_eligibility_"
    "gate_requiring_every_canonical_inherited_wait_exempt_replica_to_be_"
    "responsive_and_eligible_in_the_exact_baseline_exclusive_selected_suffix_at_"
    "the_same_cycle1_selection_cutoff_and_exact_cycle1_authenticated_reporter_"
    "cross_commit_retention_readiness_gate_requiring_one_schema_v2_attempt_start_"
    "and_reporter_local_commit_timeout_fact_per_canonical_responsive_degraded_"
    "actor_and_at_least_one_aggregate_relay_fact_at_the_same_evidence_high_water_"
    "cutoff_and_exact_admitted_actor_origin_fault_marker_window_binding_to_the_"
    "materialized_slot_omission_window_id_with_fault_evidence_and_epoch1_stable_"
    "as_the_only_fault_active_causal_phases_with_epoch1_selection_terminal_all_"
    "replica_command_activation_and_stable_end_before_fault_end_then_epoch2_as_"
    "post_fault_recovery_and_stability_with_fault_end_before_cycle1_selection_"
    "and_all_replica_commands_then_epoch2_terminal_all_replica_activation_stable_"
    "end_drain_and_runner_terminal_before_shared_hard_deadline_v6"
)
DELTA_V5 = (
    "byzantine.window.duration_s:450->360;response_evidence.timeout_retention_"
    "fact:absent->schema_v2_attempt_start_and_reporter_local_commit;cycle1."
    "selection.exclusive_lower_bound:unset->effective_fault_window_end_plus_5s;"
    "cycle1.selection.inherited_wait_exempt_responsive_eligible_gate:disabled->"
    "required_at_same_selected_suffix_cutoff;cycle1.selection.responsive_cross_"
    "commit_retention_readiness_gate:disabled->required_at_same_evidence_high_"
    "water_cutoff;cycle1.selection.responsive_cross_commit_retention.actor_"
    "origin_fault_marker_window_binding:unset->materialized_slot_omission_window_id"
)
SELECTION_OPTION = "--cycle-1-selection-not-before-monotonic-ns"
ELIGIBILITY_OPTION = "--cycle-1-inherited-wait-exempt-eligibility-gate"
RETENTION_OPTION = "--cycle-1-responsive-cross-commit-retention-readiness-gate"
RESPONSIVE_ACTORS_OPTION = "--cycle-1-responsive-degraded-actors"
REPORTER_V2_CONFIG_LINE = (
    "experiment-responsive-cross-commit-retention-v2 = true"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_v41_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V41_MANIFEST.read_bytes()
    semantic_sha256 = hashlib.sha256(_canonical(json.loads(payload))).hexdigest()
    monkeypatch.setattr(manifest_module, "FROZEN_SEMANTIC_SHA256", semantic_sha256)
    manifest = manifest_module.parse_manifest_bytes(payload)
    plan = manifest_module.build_factorial_plan(manifest)
    monkeypatch.setattr(
        manifest_module, "FROZEN_MANIFEST_SHA256", manifest.manifest_sha256
    )
    monkeypatch.setattr(manifest_module, "FROZEN_PLAN_SHA256", plan.plan_sha256)
    return manifest, plan


def _coverage(plan):
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    return execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )


def test_v41_profile_is_one_lf_and_exact_three_path_delta() -> None:
    v41_payload = V41_MANIFEST.read_bytes()
    v40_payload = V40_MANIFEST.read_bytes()
    assert v41_payload.endswith(b"\n")
    assert not v41_payload.endswith(b"\n\n")
    assert v41_payload.count(b"shape-placement-factorial-v41") == 2
    v41 = json.loads(v41_payload)
    v40 = json.loads(v40_payload)
    assert v41.pop("manifest_id") == "shape-placement-factorial-v41"
    assert v40.pop("manifest_id") == "shape-placement-factorial-v40"
    assert v41["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v41"
    )
    assert v40["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v40"
    )
    v41_responsive = v41["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v40_responsive = v40["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert v41_responsive.pop("excluded_repair_smoke_observation_contract") == V6
    assert v40_responsive.pop("excluded_repair_smoke_observation_contract") == (
        manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V5
    )
    assert v41 == v40


def test_v40_and_v41_are_historical() -> None:
    assert manifest_module.V40_MANIFEST_ID == "shape-placement-factorial-v40"
    assert (
        manifest_module.V40_MANIFEST_SHA256,
        manifest_module.V40_SEMANTIC_SHA256,
        manifest_module.V40_PLAN_SHA256,
        runtime_module.V40_RUNTIME_SHA256,
        runtime_module.V40_SMOKE_RUNTIME_SHA256,
        runtime_module.V40_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == V40_SIX
    assert manifest_module.V41_MANIFEST_ID == "shape-placement-factorial-v41"
    assert (
        manifest_module.V41_MANIFEST_SHA256,
        manifest_module.V41_SEMANTIC_SHA256,
        manifest_module.V41_PLAN_SHA256,
        runtime_module.V41_RUNTIME_SHA256,
        runtime_module.V41_SMOKE_RUNTIME_SHA256,
        runtime_module.V41_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == V41_SIX


def test_v41_threads_exact_v6_and_delta_v5_literals() -> None:
    assert manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V6 == V6
    assert runtime_module.EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V5 == DELTA_V5


def test_v40_six_identities_still_rehash_exactly() -> None:
    manifest_payload = V40_MANIFEST.read_bytes()
    manifest = manifest_module.load_frozen_manifest(V40_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    coverage = _coverage(plan)

    assert (
        hashlib.sha256(manifest_payload).hexdigest(),
        hashlib.sha256(_canonical(json.loads(manifest_payload))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(runtime_module.canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(execution._canonical_json_bytes(smoke.runtime.as_document())).hexdigest(),
        hashlib.sha256(execution._canonical_json_bytes(coverage.runtime.as_document())).hexdigest(),
    ) == V40_SIX


def test_v41_readiness_and_selection_gates_are_s037_coverage_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_v41_plan(monkeypatch)
    campaign = runtime_module.build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    coverage = _coverage(plan)
    primary, repair = coverage.runtimes
    gated_options = {
        SELECTION_OPTION,
        ELIGIBILITY_OPTION,
        RETENTION_OPTION,
        RESPONSIVE_ACTORS_OPTION,
    }

    assert all(
        gated_options.isdisjoint(slot.manager_argv_template.argv)
        and REPORTER_V2_CONFIG_LINE not in slot.main_config.lines
        for slot in campaign.slots
    )
    assert gated_options.isdisjoint(smoke.runtime.manager_argv_template.argv)
    assert REPORTER_V2_CONFIG_LINE not in smoke.runtime.main_config.lines
    assert gated_options.isdisjoint(primary.manager_argv_template.argv)
    assert REPORTER_V2_CONFIG_LINE not in primary.main_config.lines

    probe = repair.excluded_repair_smoke_probe
    assert probe is not None
    assert repair.result_path == (
        "results/shape-placement-factorial-v41-coverage-smoke/"
        "slot-037-n31-f2-b04-00"
    )
    assert probe.source_campaign_result_path == (
        "results/shape-placement-factorial-v41/slot-037-n31-f2-b04-00"
    )
    assert probe.observation_contract == V6
    assert probe.semantic_delta == DELTA_V5
    assert gated_options.issubset(set(repair.manager_argv_template.argv))
    assert repair.main_config.lines.count(REPORTER_V2_CONFIG_LINE) == 1

    materialized = runtime_module.materialize_manager_argv(
        repair,
        tmp_path / repair.slot_id,
        runtime_module.ManagerSecretMaterial(
            manager_tls_private_key_der_hex="a1b2",
            manager_tls_certificate_der_hex="c3d4",
            issuer_private_key_hex="11" * 32,
            replica_tls_certificate_der_hex=tuple(
                f"{replica_id + 1:02x}" for replica_id in range(repair.replica_count)
            ),
        ),
        shared_raw_clock_anchor_ns=7_000_000_000,
    )
    expected_selection_ns = 7_000_000_000 + (150 + 360 + 5) * 1_000_000_000
    assert materialized[materialized.index(SELECTION_OPTION) + 1] == str(
        expected_selection_ns
    )


def test_v41_is_historical_and_v40_is_validation_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST != V41_MANIFEST
    assert cli.main(["--manifest", str(V40_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v41 are validation-only" in refusal["reason"]
