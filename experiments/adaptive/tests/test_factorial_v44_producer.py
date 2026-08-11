"""Producer contract for the v44 two-stage retention witness rollover."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive import run_shape_factorial_campaign as cli
from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment import factorial_manifest as manifest_module
from experiments.adaptive.kauri_experiment import factorial_runtime as runtime_module


REPOSITORY = Path(__file__).resolve().parents[3]
V44_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v44.json"
)
V43_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v43.json"
)
V43_SIX = (
    "2a50d4d50b8518d50c1c2b40695ef74f82b7cec1d7f1e0ef8a54311468c119da",
    "23e07694e85686bbfbbe91cbe76fcb01d7a8ebf1009ad2747036af0956167347",
    "4ce4cb5e699c3fc87ec988755e9be73a1fec68edc71154faf050c64bed5f9366",
    "eab2f59024b777435b1713ec279adca86c8ea1ec18197da24c90f4480a441764",
    "48f966751d6da4fea160ed9787c560df8768ef9893d2e262d56a16af66ccee8c",
    "0ba89d2ba5e07db1a7adb03258330c527d95d1240ba7a1598686de3474541809",
)
V44_SIX = (
    "eaf0d7e756c3ed54a4ebbf2ebc10701494829f315bed4efb7c021ae14f8f93c2",
    "69db86cf3573bbf153150e1208c6bb76e791fb9f1642430797ffa6daeeff6498",
    "91725b2c029407b53247447c7c46c82e69a8025d9043907412adcde7d4a2ef80",
    "fa6cb7313c58aaa45a859d3193d499ebc0fd811253ae19350ac5af8bc21065a5",
    "17bb2b9be77edc679be895beabcd06e9714bf72ae39c4e41d2c3a38ac70fcee0",
    "5508460d3488e830e43c6898c5396dcffa0478c8f98d0035f61571cd708d66fa",
)
V44_READINESS_GATE = (
    "all_68_factorial_campaign_slots_and_ordered_s066_coverage_cycle1_online_"
    "authenticated_reporter_readiness_admitted_observation_ids_are_trigger_"
    "ids_proving_one_outstanding_epoch1_schema_v2_aggregate_relay_attempt_"
    "start_and_reporter_local_commit_timeout_fact_per_canonical_responsive_"
    "degraded_actor_at_the_same_evidence_high_water_cutoff_before_epoch2_"
    "selection_while_sealed_validation_independently_selects_the_earliest_"
    "exact_actor_origin_omit_aggregate_causal_witness_per_actor_from_the_same_"
    "cutoff_with_final_materialized_actor_marker_arm_shared_commit_sample_"
    "rich_commit_and_observer_join_while_excluded_n7_is_schema_v1_ungated_"
    "and_excluded_repair_s037_retains_v6_one_per_actor_with_at_least_one_"
    "global_aggregate_v1"
)
ONLINE_TRIGGER_IDS_CONTRACT = (
    "trigger_ids_proving_one_outstanding_epoch1_schema_v2_aggregate_relay_"
    "attempt_start_and_reporter_local_commit_timeout_fact_per_canonical_"
    "responsive_degraded_actor_at_the_evidence_high_water_cutoff_v1"
)
SEALED_CAUSAL_WITNESS_CONTRACT = (
    "independently_select_earliest_exact_actor_origin_omit_aggregate_causal_"
    "witness_per_actor_from_the_same_evidence_high_water_cutoff_v1"
)
CAMPAIGN_SCOPE = "factorial_campaign_v1"
S066_SCOPE = "ordered_s066_coverage_v1"
S037_SCOPE = "excluded_repair_s037_v1"
AGGREGATE_POLICY = "aggregate_relay_per_actor_v1"
LEGACY_POLICY = "one_per_actor_with_global_aggregate_v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_v44_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V44_MANIFEST.read_bytes()
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_SEMANTIC_SHA256",
        hashlib.sha256(_canonical(json.loads(payload))).hexdigest(),
    )
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
    return execution.build_n31_coverage_smoke_slot(primary, repair_template=repair)


def test_v44_profile_is_exact_three_field_delta_from_v43() -> None:
    assert V44_MANIFEST.is_file()
    v44_payload = V44_MANIFEST.read_bytes()
    v43_payload = V43_MANIFEST.read_bytes()
    assert v44_payload.endswith(b"\n")
    assert not v44_payload.endswith(b"\n\n")
    assert v44_payload.count(b"shape-placement-factorial-v44") == 2
    v44 = json.loads(v44_payload)
    v43 = json.loads(v43_payload)
    assert v44.pop("manifest_id") == "shape-placement-factorial-v44"
    assert v43.pop("manifest_id") == "shape-placement-factorial-v43"
    assert v44["artifacts"].pop("results_root") == (
        "results/shape-placement-factorial-v44"
    )
    assert v43["artifacts"].pop("results_root") == (
        "results/shape-placement-factorial-v43"
    )
    v44_gate = v44["byzantine"]["responsive_degradation"].pop(
        "causal_selection_reporter_retention_readiness_gate"
    )
    v43["byzantine"]["responsive_degradation"].pop(
        "causal_selection_reporter_retention_readiness_gate"
    )
    assert v44_gate == V44_READINESS_GATE
    assert v44 == v43


def test_v43_and_v44_are_exactly_historical() -> None:
    assert getattr(manifest_module, "V43_MANIFEST_ID", None) == (
        "shape-placement-factorial-v43"
    )
    assert (
        getattr(manifest_module, "V43_MANIFEST_SHA256", None),
        getattr(manifest_module, "V43_SEMANTIC_SHA256", None),
        getattr(manifest_module, "V43_PLAN_SHA256", None),
        getattr(runtime_module, "V43_RUNTIME_SHA256", None),
        getattr(runtime_module, "V43_SMOKE_RUNTIME_SHA256", None),
        getattr(runtime_module, "V43_COVERAGE_SMOKE_RUNTIME_SHA256", None),
    ) == V43_SIX
    assert (
        manifest_module.V44_MANIFEST_SHA256,
        manifest_module.V44_SEMANTIC_SHA256,
        manifest_module.V44_PLAN_SHA256,
        runtime_module.V44_RUNTIME_SHA256,
        runtime_module.V44_SMOKE_RUNTIME_SHA256,
        runtime_module.V44_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == V44_SIX


def test_v43_six_identities_still_rehash_exactly() -> None:
    payload = V43_MANIFEST.read_bytes()
    manifest = manifest_module.load_frozen_manifest(V43_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    coverage = _coverage(plan)
    assert (
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(_canonical(json.loads(payload))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(runtime_module.canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(_canonical(smoke.runtime.as_document())).hexdigest(),
        hashlib.sha256(_canonical(coverage.runtime.as_document())).hexdigest(),
    ) == V43_SIX


def test_v44_generic_retention_serializes_two_stage_contract_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_v44_plan(monkeypatch)
    campaign = runtime_module.build_factorial_runtime(plan)
    assert len(campaign.slots) == 68
    for spec in campaign.slots:
        retention = spec.cycle1_responsive_cross_commit_retention
        assert retention is not None
        assert (
            retention.scope,
            retention.observation_schema_version,
            retention.responsive_degraded_actor_ids,
            retention.admission_policy,
            retention.online_readiness_admitted_observation_ids_contract,
            retention.sealed_validation_causal_witness_selection_contract,
        ) == (
            CAMPAIGN_SCOPE,
            2,
            spec.tiered_cohorts.responsive_degraded_actor_ids,
            AGGREGATE_POLICY,
            ONLINE_TRIGGER_IDS_CONTRACT,
            SEALED_CAUSAL_WITNESS_CONTRACT,
        )

    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    assert smoke.runtime.result_path.startswith(
        "results/shape-placement-factorial-v44-smoke/"
    )
    assert smoke.runtime.cycle1_responsive_cross_commit_retention is None

    coverage = _coverage(plan)
    primary, repair = coverage.runtimes
    primary_retention = primary.cycle1_responsive_cross_commit_retention
    repair_retention = repair.cycle1_responsive_cross_commit_retention
    assert primary_retention is not None
    assert repair_retention is not None
    assert (
        primary_retention.scope,
        primary_retention.admission_policy,
        primary_retention.online_readiness_admitted_observation_ids_contract,
        primary_retention.sealed_validation_causal_witness_selection_contract,
    ) == (
        S066_SCOPE,
        AGGREGATE_POLICY,
        ONLINE_TRIGGER_IDS_CONTRACT,
        SEALED_CAUSAL_WITNESS_CONTRACT,
    )
    assert (
        repair_retention.scope,
        repair_retention.admission_policy,
        repair_retention.online_readiness_admitted_observation_ids_contract,
        repair_retention.sealed_validation_causal_witness_selection_contract,
    ) == (S037_SCOPE, LEGACY_POLICY, None, None)
    assert repair.excluded_repair_smoke_probe.observation_contract == (
        manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V6
    )
    assert repair.excluded_repair_smoke_probe.semantic_delta == (
        runtime_module.EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V5
    )

    artifacts = {
        "manifest.json": V44_MANIFEST.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": _canonical(coverage.runtime.as_document()),
    }
    for slot, spec in zip(coverage.slots, coverage.runtimes, strict=True):
        execution._bind_static_artifacts(
            slot,
            spec,
            artifacts,
            campaign_member=False,
        )


@pytest.mark.parametrize(
    ("source_version", "alias_version"),
    (("v43", "v44"), ("v44", "v43")),
)
def test_v44_coverage_binding_rejects_cross_version_repair_alias(
    source_version: str,
    alias_version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if source_version == "v44":
        _, plan = _candidate_v44_plan(monkeypatch)
        manifest_path = V44_MANIFEST
    else:
        plan = manifest_module.build_factorial_plan(
            manifest_module.load_frozen_manifest(V43_MANIFEST)
        )
        manifest_path = V43_MANIFEST
    coverage = _coverage(plan)
    aliased_slot = replace(
        coverage.slots[1],
        result_path=(
            f"results/shape-placement-factorial-{alias_version}-coverage-smoke/"
            "slot-037-n31-f2-b04-00"
        ),
    )
    aliased_spec = replace(coverage.runtimes[1], result_path=aliased_slot.result_path)
    artifacts = {
        "manifest.json": manifest_path.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": _canonical(aliased_spec.as_document()),
    }

    with pytest.raises(
        execution.FactorialExecutionError,
        match=(
            "not derivable from the exact frozen plan|"
            "slot/runtime derivation failed"
        ),
    ):
        execution._bind_static_artifacts(
            aliased_slot,
            aliased_spec,
            artifacts,
            campaign_member=False,
        )


@pytest.mark.parametrize(
    ("source_version", "alias_version"),
    (("v43", "v44"), ("v44", "v43")),
)
def test_v44_n7_binding_rejects_cross_version_result_path_alias(
    source_version: str,
    alias_version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if source_version == "v44":
        _, plan = _candidate_v44_plan(monkeypatch)
        manifest_path = V44_MANIFEST
    else:
        plan = manifest_module.build_factorial_plan(
            manifest_module.load_frozen_manifest(V43_MANIFEST)
        )
        manifest_path = V43_MANIFEST
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    aliased_slot = replace(
        smoke.slot,
        result_path=(
            f"results/shape-placement-factorial-{alias_version}-smoke/"
            "smoke-n7-f2-PS"
        ),
    )
    aliased_spec = runtime_module.build_slot_runtime(aliased_slot)
    artifacts = {
        "manifest.json": manifest_path.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": _canonical(aliased_spec.as_document()),
    }

    with pytest.raises(
        execution.FactorialExecutionError,
        match="not derivable from the exact frozen plan",
    ):
        execution._bind_static_artifacts(
            aliased_slot,
            aliased_spec,
            artifacts,
            campaign_member=False,
        )


def test_v44_native_manager_policy_and_timing_match_v43(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, v44_plan = _candidate_v44_plan(monkeypatch)
    v43_plan = manifest_module.build_factorial_plan(
        manifest_module.load_frozen_manifest(V43_MANIFEST)
    )
    v44_runtime = runtime_module.build_factorial_runtime(v44_plan)
    v43_runtime = runtime_module.build_factorial_runtime(v43_plan)
    for v44_slot, v43_slot, v44_spec, v43_spec in zip(
        v44_plan.slots,
        v43_plan.slots,
        v44_runtime.slots,
        v43_runtime.slots,
        strict=True,
    ):
        assert v44_spec.manager_argv_template == v43_spec.manager_argv_template
        assert v44_spec.fault_window == v43_spec.fault_window
        assert v44_spec.responsiveness_policy == v43_spec.responsiveness_policy
        assert v44_spec.cutoff_contract == v43_spec.cutoff_contract
        assert v44_slot.common_timers == v43_slot.common_timers
        assert v44_slot.workload == v43_slot.workload
        assert v44_slot.byzantine.actions == v43_slot.byzantine.actions
        assert (
            v44_spec.cycle1_responsive_cross_commit_retention.admission_policy
            == v43_spec.cycle1_responsive_cross_commit_retention.admission_policy
        )


def test_v45_is_default_and_v44_is_validation_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v45.json"
    assert cli.main(["--manifest", str(V44_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v44 are validation-only" in refusal["reason"]
