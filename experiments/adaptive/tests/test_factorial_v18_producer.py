"""Prospective producer contract for independent guarded-selection domains v18."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive import run_shape_factorial_campaign as cli
from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment import factorial_manifest as manifest_module
from experiments.adaptive.kauri_experiment.factorial_manifest import (
    FactorialManifestError,
    PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1,
    V17_MANIFEST_SHA256,
    V17_PLAN_SHA256,
    V17_SEMANTIC_SHA256,
    V18_MANIFEST_SHA256,
    V18_PLAN_SHA256,
    V18_SEMANTIC_SHA256,
    build_factorial_plan,
    load_frozen_manifest,
    parse_manifest_bytes,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    V17_COVERAGE_SMOKE_RUNTIME_SHA256,
    V17_RUNTIME_SHA256,
    V17_SMOKE_RUNTIME_SHA256,
    V18_COVERAGE_SMOKE_RUNTIME_SHA256,
    V18_RUNTIME_SHA256,
    V18_SMOKE_RUNTIME_SHA256,
    build_factorial_runtime,
    build_slot_runtime,
    canonical_runtime_bytes,
)

REPOSITORY = Path(__file__).resolve().parents[3]
V18_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v18.json"
)
V17_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v17.json"
)
PRECONTAINMENT_GUARDED_SELECTION_CONTRACT = (
    "post_baseline_high_water_drawdown_and_exact_post_fault_proposal_key_timeout_"
    "witnesses_are_independent_factory_validated_domains_v1"
)


def _encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _runtime_identities(path: Path) -> tuple[str, str, str, str, str, str]:
    manifest = load_frozen_manifest(path)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    n31 = execution.build_n31_coverage_smoke_slot(first)
    semantic = _encoded(json.loads(path.read_bytes()))
    return (
        manifest.manifest_sha256,
        hashlib.sha256(semantic).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(n7.runtime.as_document())
        ).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(n31.runtime.as_document())
        ).hexdigest(),
    )


def test_v18_only_changes_identity_root_and_guarded_selection_contract() -> None:
    v18 = json.loads(V18_MANIFEST.read_bytes())
    v17 = json.loads(V17_MANIFEST.read_bytes())

    assert v18.pop("manifest_id") == "shape-placement-factorial-v18"
    assert v17.pop("manifest_id") == "shape-placement-factorial-v17"
    assert v18["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v18"
    )
    assert v17["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v17"
    )
    v18_responsive = v18["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v17_responsive = v17["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert (
        v18_responsive.pop(  # type: ignore[union-attr]
            "precontainment_guarded_selection_contract"
        )
        == PRECONTAINMENT_GUARDED_SELECTION_CONTRACT
    )
    assert "precontainment_guarded_selection_contract" not in v17_responsive
    assert v18 == v17

    assert PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1 == (
        PRECONTAINMENT_GUARDED_SELECTION_CONTRACT
    )
    assert manifest_module.V18_MANIFEST_ID == "shape-placement-factorial-v18"


@pytest.mark.parametrize("replacement", (None, "shared_timeout_and_drawdown_domain"))
def test_v18_rejects_missing_or_forged_guarded_selection_contract(
    replacement: str | None,
) -> None:
    document = json.loads(V18_MANIFEST.read_bytes())
    responsive = document["byzantine"]["responsive_degradation"]
    if replacement is None:
        responsive.pop("precontainment_guarded_selection_contract")
    else:
        responsive["precontainment_guarded_selection_contract"] = replacement

    with pytest.raises(
        FactorialManifestError,
        match="guarded selection contract|fields are not frozen",
    ):
        parse_manifest_bytes(_encoded(document))


def test_v18_runtime_mirrors_guard_contract_without_argv_or_timing_drift() -> None:
    v18_plan = build_factorial_plan(load_frozen_manifest(V18_MANIFEST))
    v17_plan = build_factorial_plan(load_frozen_manifest(V17_MANIFEST))
    v18_runtime = build_factorial_runtime(v18_plan)
    v17_runtime = build_factorial_runtime(v17_plan)

    assert len(v18_runtime.slots) == len(v17_runtime.slots)
    for v18_slot, v17_slot in zip(v18_runtime.slots, v17_runtime.slots, strict=True):
        assert (
            v18_slot.causal_acceptance.precontainment_guarded_selection_contract
            == PRECONTAINMENT_GUARDED_SELECTION_CONTRACT
        )
        assert (
            v17_slot.causal_acceptance.precontainment_guarded_selection_contract
            is None
        )
        assert v18_slot.fault_window == v17_slot.fault_window
        assert v18_slot.cutoff_contract == v17_slot.cutoff_contract
        assert v18_slot.responsiveness_policy == v17_slot.responsiveness_policy
        assert v18_slot.manager_argv_template == v17_slot.manager_argv_template
        assert v18_slot.replica_argv_templates == v17_slot.replica_argv_templates
        assert v18_slot.transitions == v17_slot.transitions


def test_v18_guard_contract_is_bound_into_slot_artifact_identity() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V18_MANIFEST))
    slot = plan.slots[0]
    responsive = slot.byzantine.responsive_degradation
    assert responsive is not None
    runtime = build_slot_runtime(slot)

    missing = replace(
        slot,
        byzantine=replace(
            slot.byzantine,
            responsive_degradation=replace(
                responsive,
                precontainment_guarded_selection_contract=None,
            ),
        ),
    )
    assert build_slot_runtime(missing).artifact_id != runtime.artifact_id


def test_v17_six_identities_remain_byte_exact() -> None:
    assert _runtime_identities(V17_MANIFEST) == (
        V17_MANIFEST_SHA256,
        V17_SEMANTIC_SHA256,
        V17_PLAN_SHA256,
        V17_RUNTIME_SHA256,
        V17_SMOKE_RUNTIME_SHA256,
        V17_COVERAGE_SMOKE_RUNTIME_SHA256,
    )
    assert _runtime_identities(V17_MANIFEST) == (
        "2683754d39fc0107d45a80eff606284adc5cf74cb325c8ff0813790d80c4aae2",
        "1e6aaa830d47a9a92c9c98d008ee1a3eaf58b7797d0e7292f759cdfee7b71ad1",
        "9dfcf4753febfd519ad7927e54e49fbf9bb53d60c5a0765bf779eb9c15280d89",
        "4de3d25cc3f6ed0325e39cc670c27d5db67c3facb7bb081bf8f93707094c0fac",
        "28aa251fb7a21296eec0ef3b49f9e1c6c68dc30d4cda83998f9ee1559896ae07",
        "7c4a5b7be38b7e985326e3c2286cac71891c2dc32488087064b4f85566d81d70",
    )


def test_v18_all_six_static_identities_are_frozen() -> None:
    assert _runtime_identities(V18_MANIFEST) == (
        V18_MANIFEST_SHA256,
        V18_SEMANTIC_SHA256,
        V18_PLAN_SHA256,
        V18_RUNTIME_SHA256,
        V18_SMOKE_RUNTIME_SHA256,
        V18_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_v18_is_validation_only_and_keeps_historical_direct_roots(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST != V18_MANIFEST
    assert cli.main(["--manifest", str(V18_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v37 are validation-only" in refusal["reason"]

    plan = build_factorial_plan(load_frozen_manifest(V18_MANIFEST))
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    n31 = execution.build_n31_coverage_smoke_slot(first)
    assert n7.slot.result_path == (
        "results/shape-placement-factorial-v18-smoke/smoke-n7-f2-PS"
    )
    assert n31.slot == replace(
        first,
        result_path=(
            "results/shape-placement-factorial-v18-coverage-smoke/"
            "slot-066-n31-f5-b05-P"
        ),
    )
    assert plan.automatic_retries == 0
    assert build_factorial_runtime(plan).automatic_retries == 0


def test_v18_n31_coverage_smoke_rejects_missing_guard_contract() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V18_MANIFEST))
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    responsive = first.byzantine.responsive_degradation
    assert responsive is not None

    with pytest.raises(
        execution.FactorialExecutionError,
        match="exact frozen campaign slot 066",
    ):
        execution.build_n31_coverage_smoke_slot(
            replace(
                first,
                byzantine=replace(
                    first.byzantine,
                    responsive_degradation=replace(
                        responsive,
                        precontainment_guarded_selection_contract=None,
                    ),
                ),
            )
        )
