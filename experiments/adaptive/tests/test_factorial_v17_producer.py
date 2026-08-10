"""Prospective producer contract for shape-bypassed containment v17."""

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
    V17_MANIFEST_SHA256,
    V17_PLAN_SHA256,
    V17_SEMANTIC_SHA256,
    build_factorial_plan,
    load_frozen_manifest,
    parse_manifest_bytes,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    V17_COVERAGE_SMOKE_RUNTIME_SHA256,
    V17_RUNTIME_SHA256,
    V17_SMOKE_RUNTIME_SHA256,
    build_factorial_runtime,
    canonical_runtime_bytes,
)

REPOSITORY = Path(__file__).resolve().parents[3]
V17_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v17.json"
)
V16_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v16.json"
)
PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT = (
    "epoch_zero_fault_containment_preserves_current_fanout_without_shape_v1_"
    "decision_later_transition_retains_exact_shape_v1_v1"
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


def test_v17_only_changes_identity_root_and_precontainment_shape_contract() -> None:
    v17 = json.loads(V17_MANIFEST.read_bytes())
    v16 = json.loads(V16_MANIFEST.read_bytes())

    assert v17.pop("manifest_id") == "shape-placement-factorial-v17"
    assert v16.pop("manifest_id") == "shape-placement-factorial-v16"
    assert v17["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v17"
    )
    assert v16["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v16"
    )
    v17_responsive = v17["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v16_responsive = v16["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert (
        v17_responsive.pop(  # type: ignore[union-attr]
            "precontainment_shape_evaluation_contract"
        )
        == PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT
    )
    assert "precontainment_shape_evaluation_contract" not in v16_responsive
    assert v17 == v16

    assert manifest_module.V17_MANIFEST_ID == "shape-placement-factorial-v17"


@pytest.mark.parametrize("replacement", (None, "shape_v1_may_be_skipped"))
def test_v17_rejects_missing_or_forged_precontainment_shape_contract(
    replacement: str | None,
) -> None:
    document = json.loads(V17_MANIFEST.read_bytes())
    responsive = document["byzantine"]["responsive_degradation"]
    if replacement is None:
        responsive.pop("precontainment_shape_evaluation_contract")
    else:
        responsive["precontainment_shape_evaluation_contract"] = replacement

    with pytest.raises(
        FactorialManifestError,
        match="precontainment shape evaluation contract|fields are not frozen",
    ):
        parse_manifest_bytes(_encoded(document))


def test_v17_runtime_freezes_no_cycle0_shape_and_exact_cycle1_shape_v1() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V17_MANIFEST))
    runtime = build_factorial_runtime(plan)

    assert runtime.manifest_id == "shape-placement-factorial-v17"
    assert runtime.results_root == "results/shape-placement-factorial-v17"
    assert runtime.slots
    for slot in runtime.slots:
        assert (
            slot.causal_acceptance.precontainment_shape_evaluation_contract
            == PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT
        )
        assert slot.transitions[0].request.policy_intent == "fault_containment"
        assert slot.transitions[0].request.apply_shape_selection is False
        assert slot.shape_invocation.compute_live is True
        assert slot.shape_invocation.selector_version == "shape-v1"
        assert slot.transition_sequence.required_events.count("shape_v1_computed") == 1
        assert slot.transitions[1].request.apply_shape_selection is (
            slot.arm_code in {"S", "PS"}
        )


def test_v17_all_six_static_identities_are_frozen() -> None:
    assert _runtime_identities(V17_MANIFEST) == (
        V17_MANIFEST_SHA256,
        V17_SEMANTIC_SHA256,
        V17_PLAN_SHA256,
        V17_RUNTIME_SHA256,
        V17_SMOKE_RUNTIME_SHA256,
        V17_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_v17_is_validation_only_and_keeps_historical_smoke_roots(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v29.json"
    assert cli.main(["--manifest", str(V17_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v28 are validation-only" in refusal["reason"]

    plan = build_factorial_plan(load_frozen_manifest(V17_MANIFEST))
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    n31 = execution.build_n31_coverage_smoke_slot(first)
    assert n7.slot.result_path == (
        "results/shape-placement-factorial-v17-smoke/smoke-n7-f2-PS"
    )
    assert n31.slot == replace(
        first,
        result_path=(
            "results/shape-placement-factorial-v17-coverage-smoke/"
            "slot-066-n31-f5-b05-P"
        ),
    )


def test_v17_n31_coverage_smoke_rejects_missing_shape_bypass_contract() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V17_MANIFEST))
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
                        precontainment_shape_evaluation_contract=None,
                    ),
                ),
            )
        )
