"""Prospective producer contract for idempotent duplicate response delivery v26."""

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
V26_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v26.json"
)
V25_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v25.json"
)
DUPLICATE_FIELD = "verified_response_duplicate_delivery_contract"
DUPLICATE_CONTRACT = (
    "exact_duplicate_verified_child_response_is_idempotent_and_cannot_fail_the_"
    "response_deadline_or_suppress_later_convergence_observations_v1"
)


def _encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _parse_candidate(
    monkeypatch: pytest.MonkeyPatch,
    document: dict[str, object] | None = None,
):
    source = json.loads(V26_MANIFEST.read_bytes()) if document is None else document
    semantic = _encoded(source)
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_SEMANTIC_SHA256",
        hashlib.sha256(semantic).hexdigest(),
    )
    return manifest_module.parse_manifest_bytes(semantic)


def _candidate_plan(
    monkeypatch: pytest.MonkeyPatch,
):
    manifest = _parse_candidate(monkeypatch)
    plan = manifest_module.build_factorial_plan(manifest)
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_MANIFEST_SHA256",
        manifest.manifest_sha256,
    )
    monkeypatch.setattr(manifest_module, "FROZEN_PLAN_SHA256", plan.plan_sha256)
    return manifest, plan


def test_v26_profile_delta_is_exactly_three_authorized_json_paths() -> None:
    v26 = json.loads(V26_MANIFEST.read_bytes())
    v25 = json.loads(V25_MANIFEST.read_bytes())

    assert v26.pop("manifest_id") == "shape-placement-factorial-v26"
    assert v25.pop("manifest_id") == "shape-placement-factorial-v25"
    assert v26["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v26"
    )
    assert v25["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v25"
    )
    assert v26["byzantine"]["responsive_degradation"].pop(  # type: ignore[index]
        DUPLICATE_FIELD
    ) == DUPLICATE_CONTRACT
    assert DUPLICATE_FIELD not in v25["byzantine"][  # type: ignore[operator]
        "responsive_degradation"
    ]
    assert v26 == v25


def test_v26_requires_exact_duplicate_delivery_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(V26_MANIFEST.read_bytes())
    missing = json.loads(json.dumps(document))
    missing["byzantine"]["responsive_degradation"].pop(DUPLICATE_FIELD)
    with pytest.raises(manifest_module.FactorialManifestError, match="duplicate"):
        _parse_candidate(monkeypatch, missing)

    document["byzantine"]["responsive_degradation"][DUPLICATE_FIELD] = (
        f"{DUPLICATE_CONTRACT}-drift"
    )
    with pytest.raises(manifest_module.FactorialManifestError, match="duplicate"):
        _parse_candidate(monkeypatch, document)


def test_v26_propagates_contract_without_schedule_or_dispatch_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, plan = _candidate_plan(monkeypatch)
    prior = manifest_module.build_factorial_plan(
        manifest_module.load_frozen_manifest(V25_MANIFEST)
    )

    assert manifest.byzantine.responsive_degradation is not None
    assert (
        getattr(manifest.byzantine.responsive_degradation, DUPLICATE_FIELD)
        == DUPLICATE_CONTRACT
    )
    assert len(plan.slots) == len(prior.slots) == 68
    assert plan.execution_schedule == prior.execution_schedule
    assert plan.automatic_retries == prior.automatic_retries == 0
    assert plan.replacement_policy == prior.replacement_policy == "none"
    for slot, previous in zip(plan.slots, prior.slots, strict=True):
        responsive = slot.byzantine.responsive_degradation
        assert responsive is not None
        assert getattr(responsive, DUPLICATE_FIELD) == DUPLICATE_CONTRACT
        assert slot.execution_ordinal == previous.execution_ordinal
        assert slot.block_id == previous.block_id
        assert slot.arm_code == previous.arm_code
        spec = runtime_module.build_slot_runtime(slot)
        assert getattr(spec.causal_acceptance, DUPLICATE_FIELD) == DUPLICATE_CONTRACT
        assert spec.causal_acceptance.as_document()[DUPLICATE_FIELD] == (
            DUPLICATE_CONTRACT
        )


def test_v26_contract_is_bound_into_every_slot_artifact_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    for slot in plan.slots:
        responsive = slot.byzantine.responsive_degradation
        assert responsive is not None
        candidate = runtime_module.build_slot_runtime(slot)
        historical = replace(
            slot,
            byzantine=replace(
                slot.byzantine,
                responsive_degradation=replace(
                    responsive,
                    verified_response_duplicate_delivery_contract=None,
                ),
            ),
        )
        assert runtime_module.build_slot_runtime(historical).artifact_id != (
            candidate.artifact_id
        )


def test_v26_freezes_all_six_recomputed_identities() -> None:
    manifest = manifest_module.load_frozen_manifest(V26_MANIFEST)
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
        manifest_module.V26_MANIFEST_SHA256,
        manifest_module.V26_SEMANTIC_SHA256,
        manifest_module.V26_PLAN_SHA256,
        runtime_module.V26_RUNTIME_SHA256,
        runtime_module.V26_SMOKE_RUNTIME_SHA256,
        runtime_module.V26_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        manifest.manifest_sha256,
        hashlib.sha256(
            _encoded(json.loads(V26_MANIFEST.read_bytes()))
        ).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(runtime_module.canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(smoke.runtime.as_document())
        ).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest(),
    )


def test_v26_is_validation_only_and_preserves_v25_coverage_alias(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v40.json"
    assert cli.main(["--manifest", str(V26_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v39 are validation-only" in refusal["reason"]

    _, plan = _candidate_plan(monkeypatch)
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    assert tuple(slot.slot_id for slot in coverage.runtimes) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
    assert tuple(slot.result_path for slot in coverage.slots) == (
        "results/shape-placement-factorial-v26-coverage-smoke/slot-066-n31-f5-b05-P",
        "results/shape-placement-factorial-v26-coverage-smoke/slot-037-n31-f2-b04-00",
    )
    assert isinstance(coverage.runtime, execution.N31CoverageSmokeRuntime)
    assert coverage.runtime.automatic_retries == 0
    assert coverage.runtime.replacement_policy == "none"
    assert coverage.runtime.stop_on_first_non_pass is True

    v25_plan = manifest_module.build_factorial_plan(
        manifest_module.load_frozen_manifest(V25_MANIFEST)
    )
    v25_coverage = execution.build_n31_coverage_smoke_slot(
        next(slot for slot in v25_plan.slots if slot.execution_ordinal == 1),
        repair_template=next(
            slot for slot in v25_plan.slots if slot.execution_ordinal == 5
        ),
    )
    assert isinstance(v25_coverage.runtime, execution.N31CoverageSmokeRuntime)
    assert tuple(slot.slot_id for slot in v25_coverage.runtimes) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
