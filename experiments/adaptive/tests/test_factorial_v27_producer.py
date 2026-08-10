"""Prospective producer contract for the v27 observer repair."""

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
V27_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v27.json"
)
V26_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v26.json"
)
DUPLICATE_FIELD = "verified_response_duplicate_delivery_contract"
DUPLICATE_CONTRACT_V2 = (
    "exact_duplicate_verified_child_response_is_idempotent_guard_marker_follows_"
    "completed_accepted_ingress_and_cannot_fail_the_response_deadline_or_suppress_"
    "later_convergence_observations_v2"
)


def _encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _parse_candidate(
    monkeypatch: pytest.MonkeyPatch,
    document: dict[str, object] | None = None,
):
    source = json.loads(V27_MANIFEST.read_bytes()) if document is None else document
    semantic = _encoded(source)
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_SEMANTIC_SHA256",
        hashlib.sha256(semantic).hexdigest(),
    )
    return manifest_module.parse_manifest_bytes(semantic)


def _candidate_plan(monkeypatch: pytest.MonkeyPatch):
    manifest = _parse_candidate(monkeypatch)
    plan = manifest_module.build_factorial_plan(manifest)
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_MANIFEST_SHA256",
        manifest.manifest_sha256,
    )
    monkeypatch.setattr(manifest_module, "FROZEN_PLAN_SHA256", plan.plan_sha256)
    return manifest, plan


def test_v27_profile_delta_is_exactly_five_authorized_json_paths() -> None:
    v27 = json.loads(V27_MANIFEST.read_bytes())
    v26 = json.loads(V26_MANIFEST.read_bytes())

    assert v27.pop("manifest_id") == "shape-placement-factorial-v27"
    assert v26.pop("manifest_id") == "shape-placement-factorial-v26"
    assert v27["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v27"
    )
    assert v26["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v26"
    )
    assert v27["byzantine"]["window"].pop("duration_s") == 450  # type: ignore[index]
    assert v26["byzantine"]["window"].pop("duration_s") == 300  # type: ignore[index]
    assert v27["timers"].pop("hard_timeout_s") == 650  # type: ignore[index]
    assert v26["timers"].pop("hard_timeout_s") == 500  # type: ignore[index]
    assert v27["byzantine"]["responsive_degradation"].pop(  # type: ignore[index]
        DUPLICATE_FIELD
    ) == DUPLICATE_CONTRACT_V2
    assert v26["byzantine"]["responsive_degradation"].pop(  # type: ignore[index]
        DUPLICATE_FIELD
    ) == manifest_module.VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V1
    assert v27 == v26


def test_v27_requires_exact_duplicate_delivery_v2_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(V27_MANIFEST.read_bytes())
    missing = json.loads(json.dumps(document))
    missing["byzantine"]["responsive_degradation"].pop(DUPLICATE_FIELD)
    with pytest.raises(manifest_module.FactorialManifestError, match="duplicate"):
        _parse_candidate(monkeypatch, missing)

    document["byzantine"]["responsive_degradation"][DUPLICATE_FIELD] = (
        manifest_module.VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V1
    )
    with pytest.raises(manifest_module.FactorialManifestError, match="duplicate"):
        _parse_candidate(monkeypatch, document)


def test_v27_propagates_timing_and_contract_without_dispatch_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, plan = _candidate_plan(monkeypatch)
    prior = manifest_module.build_factorial_plan(
        manifest_module.load_frozen_manifest(V26_MANIFEST)
    )

    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    assert getattr(responsive, DUPLICATE_FIELD) == DUPLICATE_CONTRACT_V2
    assert manifest.byzantine.duration_s == 450
    assert manifest.common_timers.hard_timeout_s == 650
    assert len(plan.slots) == len(prior.slots) == 68
    assert plan.execution_schedule == prior.execution_schedule
    assert plan.automatic_retries == prior.automatic_retries == 0
    assert plan.replacement_policy == prior.replacement_policy == "none"
    for slot, previous in zip(plan.slots, prior.slots, strict=True):
        slot_responsive = slot.byzantine.responsive_degradation
        assert slot_responsive is not None
        assert getattr(slot_responsive, DUPLICATE_FIELD) == DUPLICATE_CONTRACT_V2
        assert slot.byzantine.duration_s == 450
        assert slot.common_timers.hard_timeout_s == 650
        assert slot.execution_ordinal == previous.execution_ordinal
        assert slot.block_id == previous.block_id
        assert slot.arm_code == previous.arm_code
        spec = runtime_module.build_slot_runtime(slot)
        assert spec.fault_window.duration_s == 450
        assert spec.fault_window.hard_timeout_s == 650
        assert getattr(spec.causal_acceptance, DUPLICATE_FIELD) == (
            DUPLICATE_CONTRACT_V2
        )


def test_v27_contract_and_timing_are_bound_into_every_slot_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    for slot in plan.slots:
        responsive = slot.byzantine.responsive_degradation
        assert responsive is not None
        candidate = runtime_module.build_slot_runtime(slot)
        duration_drift = replace(
            slot,
            byzantine=replace(slot.byzantine, duration_s=300),
        )
        hard_timeout_drift = replace(
            slot,
            common_timers=replace(slot.common_timers, hard_timeout_s=500),
        )
        contract_drift = replace(
            slot,
            byzantine=replace(
                slot.byzantine,
                responsive_degradation=replace(
                    responsive,
                    verified_response_duplicate_delivery_contract=(
                        manifest_module.VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V1
                    ),
                ),
            ),
        )
        assert runtime_module.build_slot_runtime(duration_drift).artifact_id != (
            candidate.artifact_id
        )
        assert runtime_module.build_slot_runtime(hard_timeout_drift).artifact_id != (
            candidate.artifact_id
        )
        assert runtime_module.build_slot_runtime(contract_drift).artifact_id != (
            candidate.artifact_id
        )


def test_v27_freezes_all_six_recomputed_identities() -> None:
    manifest = manifest_module.load_frozen_manifest(V27_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    assert (
        manifest_module.V27_MANIFEST_SHA256,
        manifest_module.V27_SEMANTIC_SHA256,
        manifest_module.V27_PLAN_SHA256,
        runtime_module.V27_RUNTIME_SHA256,
        runtime_module.V27_SMOKE_RUNTIME_SHA256,
        runtime_module.V27_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        manifest.manifest_sha256,
        hashlib.sha256(_encoded(json.loads(V27_MANIFEST.read_bytes()))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(runtime_module.canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(smoke.runtime.as_document())
        ).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest(),
    )


def test_v27_is_validation_only_and_preserves_v26_alias(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v28.json"
    assert cli.main(["--manifest", str(V27_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v27 are validation-only" in refusal["reason"]
    assert cli.main(["--manifest", str(V26_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v27 are validation-only" in refusal["reason"]

    _, plan = _candidate_plan(monkeypatch)
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    assert tuple(slot.result_path for slot in coverage.slots) == (
        "results/shape-placement-factorial-v27-coverage-smoke/slot-066-n31-f5-b05-P",
        "results/shape-placement-factorial-v27-coverage-smoke/slot-037-n31-f2-b04-00",
    )
    assert isinstance(coverage.runtime, execution.N31CoverageSmokeRuntime)
    assert coverage.runtime.runtime_id == (
        "shape-placement-factorial-v27-excluded-n31-coverage-smoke-v1"
    )

    v26_manifest = manifest_module.load_frozen_manifest(V26_MANIFEST)
    v26_plan = manifest_module.build_factorial_plan(v26_manifest)
    v26_runtime = runtime_module.build_factorial_runtime(v26_plan)
    assert v26_manifest.manifest_sha256 == manifest_module.V26_MANIFEST_SHA256
    assert v26_plan.plan_sha256 == manifest_module.V26_PLAN_SHA256
    assert hashlib.sha256(
        runtime_module.canonical_runtime_bytes(v26_runtime)
    ).hexdigest() == runtime_module.V26_RUNTIME_SHA256


def test_v27_coverage_derivation_rejects_timing_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)

    with pytest.raises(execution.FactorialExecutionError, match="slot 066"):
        execution.build_n31_coverage_smoke_slot(
            replace(
                primary,
                byzantine=replace(primary.byzantine, duration_s=300),
            ),
            repair_template=repair,
        )

    with pytest.raises(execution.FactorialExecutionError, match="slot 037"):
        execution.build_n31_coverage_smoke_slot(
            primary,
            repair_template=replace(
                repair,
                common_timers=replace(repair.common_timers, hard_timeout_s=500),
            ),
        )
