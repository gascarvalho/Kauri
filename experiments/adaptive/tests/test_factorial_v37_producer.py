"""Prospective producer contract for the v37 repair-observation roll."""

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
V37_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v37.json"
)
V36_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v36.json"
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
EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2 = (
    "byzantine.window.duration_s:450->330"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V37_MANIFEST.read_bytes()
    semantic_sha256 = hashlib.sha256(_canonical(json.loads(payload))).hexdigest()
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_SEMANTIC_SHA256",
        semantic_sha256,
    )
    manifest = manifest_module.parse_manifest_bytes(payload)
    plan = manifest_module.build_factorial_plan(manifest)
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_MANIFEST_SHA256",
        manifest.manifest_sha256,
    )
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_PLAN_SHA256",
        plan.plan_sha256,
    )
    return manifest, plan


def _coverage(plan):
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    return execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )


def _candidate_coverage(monkeypatch: pytest.MonkeyPatch):
    manifest, plan = _candidate_plan(monkeypatch)
    return manifest, plan, _coverage(plan)


def _static_artifacts(manifest_path: Path, plan, coverage) -> dict[str, bytes]:
    return {
        "manifest.json": manifest_path.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": execution._canonical_json_bytes(coverage.runtime.as_document()),
    }


def test_v37_profile_is_one_lf_and_exact_three_path_delta() -> None:
    v37_payload = V37_MANIFEST.read_bytes()
    v36_payload = V36_MANIFEST.read_bytes()

    assert v37_payload.endswith(b"\n")
    assert not v37_payload.endswith(b"\n\n")
    assert v37_payload.count(b"shape-placement-factorial-v37") == 2
    v37 = json.loads(v37_payload)
    v36 = json.loads(v36_payload)
    assert v37.pop("manifest_id") == "shape-placement-factorial-v37"
    assert v36.pop("manifest_id") == "shape-placement-factorial-v36"
    assert v37["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v37"
    )
    assert v36["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v36"
    )
    v37_responsive = v37["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v36_responsive = v36["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert v37_responsive.pop(
        "excluded_repair_smoke_observation_contract"
    ) == EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
    assert v36_responsive.pop(
        "excluded_repair_smoke_observation_contract"
    ) == manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
    assert v37 == v36


def test_v36_and_v37_identities_are_explicit_historical_aliases() -> None:
    assert (
        getattr(manifest_module, "V36_MANIFEST_ID", None),
        getattr(manifest_module, "V36_MANIFEST_SHA256", None),
        getattr(manifest_module, "V36_SEMANTIC_SHA256", None),
        getattr(manifest_module, "V36_PLAN_SHA256", None),
        getattr(runtime_module, "V36_RUNTIME_SHA256", None),
        getattr(runtime_module, "V36_SMOKE_RUNTIME_SHA256", None),
        getattr(runtime_module, "V36_COVERAGE_SMOKE_RUNTIME_SHA256", None),
    ) == (
        "shape-placement-factorial-v36",
        "50761ebcd8693c33ca30257b3abee6f44f992481b6f10e57732d098b029073d1",
        "87cd28e7df12aeb9f54386623b207526ea71d096d3699d687dc07784219d63ed",
        "d5075db22099788a1c687ddc72cc4953a2d665fc1fa09104ba69ab128f91a65f",
        "5b088b4d3e2a0a484f2829664b94db6d51e32fb0e8a0bc904fdf342098a85c89",
        "3ad3f26401c190827591351b21578e2b3ec088f0222e8262f2cfa6f29a402c7a",
        "34df26b4aaff8c3f417d1634aa1e52e7f40551b8265c2830720456dc068acc81",
    )
    assert manifest_module.V37_MANIFEST_ID == "shape-placement-factorial-v37"
    assert (
        manifest_module.V37_MANIFEST_SHA256,
        manifest_module.V37_SEMANTIC_SHA256,
        manifest_module.V37_PLAN_SHA256,
        runtime_module.V37_RUNTIME_SHA256,
        runtime_module.V37_SMOKE_RUNTIME_SHA256,
        runtime_module.V37_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "a926192d3c6a5129ea8304504317f921a8a1a681b489e1503e715dcbd3e8be11",
        "63efea700bd3a0fa3d59b7876602e3a5f99a3d71edb4e57ed218a8e6ea4bec8c",
        "e413ff98733b5e462b058018ce25ca4c77013a760fffe730dcbfd50150379e36",
        "a05f26983a34f626f95d326847db7a049a05c2258fdadf3bbaddd4ec3850c5d7",
        "1eda50b8e2887ab4d0f1763816f82344136dabf480d752f5a4d82f48272e8f63",
        "7aa68f246e06bee0a666734113e5a5bda7a747c912b3cc51db6f81d03b2a6d81",
    )


def test_v37_changes_only_the_exact_repair_runtime_to_330(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, plan, coverage = _candidate_coverage(monkeypatch)
    campaign_runtime = runtime_module.build_factorial_runtime(plan)
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])

    assert manifest.common_timers.aggregation_timeout_ms_per_depth == 125
    assert manifest.common_timers.leader_progress_timeout_ms == 20_000
    assert manifest.common_timers.transition_convergence_deadline_s == 30
    assert manifest.common_timers.hard_timeout_s == 650
    assert all(slot.byzantine.duration_s == 450 for slot in plan.slots)
    assert all(slot.fault_window.duration_s == 450 for slot in campaign_runtime.slots)
    assert all(slot.fault_window.hard_timeout_s == 650 for slot in campaign_runtime.slots)
    assert all(
        slot.excluded_repair_smoke_probe is None
        and all(
            runtime_module.EXCLUDED_REPAIR_SMOKE_PROBE_OPTION not in process.argv
            for process in slot.replica_argv_templates
        )
        for slot in campaign_runtime.slots
    )
    assert n7.slot.byzantine.duration_s == 450
    assert n7.runtime.fault_window.duration_s == 450
    assert n7.runtime.excluded_repair_smoke_probe is None

    primary_runtime, repair_runtime = coverage.runtimes
    assert primary_runtime.slot_id == "slot-066-n31-f5-b05-P"
    assert primary_runtime.fault_window.duration_s == 450
    assert primary_runtime.excluded_repair_smoke_probe is None
    assert repair_runtime.slot_id == "slot-037-n31-f2-b04-00"
    assert coverage.slots[1].byzantine.duration_s == 330
    assert repair_runtime.fault_window.duration_s == 330
    assert repair_runtime.fault_window.hard_timeout_s == 650
    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    assert probe.source_fault_window_duration_s == 450
    assert probe.effective_fault_window_duration_s == 330
    assert probe.hard_timeout_s == 650
    assert probe.semantic_delta == EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2
    assert probe.observation_contract == EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
    assert all(
        process.argv.count(runtime_module.EXCLUDED_REPAIR_SMOKE_PROBE_OPTION) == 1
        and process.argv.count(runtime_module.EXCLUDED_REPAIR_SMOKE_PROBE_MODE_V1)
        == 1
        for process in repair_runtime.replica_argv_templates
    )


def test_v36_history_preserves_300_second_repair_semantics() -> None:
    manifest = manifest_module.load_frozen_manifest(V36_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    coverage = _coverage(plan)
    primary_runtime, repair_runtime = coverage.runtimes

    assert manifest.manifest_id == manifest_module.V36_MANIFEST_ID
    assert manifest.manifest_sha256 == manifest_module.V36_MANIFEST_SHA256
    assert plan.plan_sha256 == manifest_module.V36_PLAN_SHA256
    assert primary_runtime.fault_window.duration_s == 450
    assert repair_runtime.fault_window.duration_s == 300
    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    assert probe.semantic_delta == runtime_module.EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V1
    assert probe.effective_fault_window_duration_s == 300
    assert probe.observation_contract == (
        manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
    )


@pytest.mark.parametrize("duration_s", (300, 329, 331, 360, 450))
def test_v37_rejects_tampered_repair_duration(
    duration_s: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coverage = _candidate_coverage(monkeypatch)
    repair_slot = coverage.slots[1]
    repair_runtime = coverage.runtimes[1]
    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    tampered_slot = replace(
        repair_slot,
        byzantine=replace(repair_slot.byzantine, duration_s=duration_s),
    )
    tampered_probe = replace(
        probe,
        effective_fault_window_duration_s=duration_s,
    )
    with pytest.raises(manifest_module.FactorialManifestError):
        runtime_module.build_slot_runtime(
            tampered_slot,
            excluded_repair_smoke_probe=tampered_probe,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        (
            "semantic_delta",
            runtime_module.EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V1,
        ),
        (
            "observation_contract",
            manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2,
        ),
        ("source_fault_window_duration_s", 330),
        ("hard_timeout_s", 649),
    ),
)
def test_v37_rejects_tampered_repair_probe_contract(
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coverage = _candidate_coverage(monkeypatch)
    repair_slot = coverage.slots[1]
    probe = coverage.runtimes[1].excluded_repair_smoke_probe
    assert probe is not None
    with pytest.raises(manifest_module.FactorialManifestError):
        runtime_module.build_slot_runtime(
            repair_slot,
            excluded_repair_smoke_probe=replace(probe, **{field: value}),
        )


def test_v36_and_v37_exact_static_bindings_reject_cross_version_mix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v36_manifest = manifest_module.load_frozen_manifest(V36_MANIFEST)
    v36_plan = manifest_module.build_factorial_plan(v36_manifest)
    v36_coverage = _coverage(v36_plan)
    _, v37_plan, v37_coverage = _candidate_coverage(monkeypatch)
    v36_artifacts = _static_artifacts(V36_MANIFEST, v36_plan, v36_coverage)
    v37_artifacts = _static_artifacts(V37_MANIFEST, v37_plan, v37_coverage)

    execution._bind_static_artifacts(
        v36_coverage.slots[1],
        v36_coverage.runtimes[1],
        v36_artifacts,
        campaign_member=False,
    )
    execution._bind_static_artifacts(
        v37_coverage.slots[1],
        v37_coverage.runtimes[1],
        v37_artifacts,
        campaign_member=False,
    )
    for slot, runtime, artifacts in (
        (v37_coverage.slots[1], v37_coverage.runtimes[1], v36_artifacts),
        (v36_coverage.slots[1], v36_coverage.runtimes[1], v37_artifacts),
        (v37_coverage.slots[1], v36_coverage.runtimes[1], v37_artifacts),
        (v36_coverage.slots[1], v37_coverage.runtimes[1], v36_artifacts),
    ):
        with pytest.raises(execution.FactorialExecutionError):
            execution._bind_static_artifacts(
                slot,
                runtime,
                artifacts,
                campaign_member=False,
            )


def test_v37_is_validation_only_after_the_v38_roll(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v43.json"
    assert cli.main(["--manifest", str(V37_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v42 are validation-only" in refusal["reason"]
