"""Prospective producer contract for causal timeout eligibility v21."""

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
    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V3,
    V20_MANIFEST_ID,
    V20_MANIFEST_SHA256,
    V20_PLAN_SHA256,
    V20_SEMANTIC_SHA256,
    V21_MANIFEST_ID,
    V21_MANIFEST_SHA256,
    V21_PLAN_SHA256,
    V21_SEMANTIC_SHA256,
    FactorialManifestError,
    build_factorial_plan,
    load_frozen_manifest,
    parse_manifest_bytes,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    V20_COVERAGE_SMOKE_RUNTIME_SHA256,
    V20_RUNTIME_SHA256,
    V20_SMOKE_RUNTIME_SHA256,
    V21_COVERAGE_SMOKE_RUNTIME_SHA256,
    V21_RUNTIME_SHA256,
    V21_SMOKE_RUNTIME_SHA256,
    build_factorial_runtime,
    build_slot_runtime,
    canonical_runtime_bytes,
)

REPOSITORY = Path(__file__).resolve().parents[3]
V21_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v21.json"
)
V20_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v20.json"
)
CAUSAL_TIMEOUT_ELIGIBILITY_V3 = (
    "selection_visible_exact_outstanding_timeout_witnesses_with_internal_and_f_plus_"
    "one_actor_gates_hard_and_responsive_degraded_absent_prefix_timeout_nonwitness_"
    "present_prefix_mismatch_fatal_v3"
)


def test_v21_only_changes_identity_root_and_causal_timeout_eligibility() -> None:
    v21 = json.loads(V21_MANIFEST.read_bytes())
    v20 = json.loads(V20_MANIFEST.read_bytes())

    assert v21.pop("manifest_id") == V21_MANIFEST_ID
    assert v20.pop("manifest_id") == V20_MANIFEST_ID
    assert v21["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v21"
    )
    assert v20["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v20"
    )
    v21_responsive = v21["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v20_responsive = v20["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert v21_responsive.pop("causal_timeout_eligibility") == (
        CAUSAL_TIMEOUT_ELIGIBILITY_V3
    )
    assert v20_responsive.pop("causal_timeout_eligibility") != (
        CAUSAL_TIMEOUT_ELIGIBILITY_V3
    )
    assert v21 == v20

    assert RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V3 == (
        CAUSAL_TIMEOUT_ELIGIBILITY_V3
    )
    assert manifest_module.V21_MANIFEST_ID == "shape-placement-factorial-v21"


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


@pytest.mark.parametrize("replacement", (None, "immature_tail_nonwitness_v2"))
def test_v21_rejects_missing_or_forged_timeout_eligibility(
    replacement: str | None,
) -> None:
    document = json.loads(V21_MANIFEST.read_bytes())
    responsive = document["byzantine"]["responsive_degradation"]
    if replacement is None:
        responsive.pop("causal_timeout_eligibility")
    else:
        responsive["causal_timeout_eligibility"] = replacement

    with pytest.raises(
        FactorialManifestError,
        match="causal edge eligibility|fields are not frozen",
    ):
        parse_manifest_bytes(_encoded(document))


def test_v20_preserves_all_identities_and_rejects_v21_eligibility() -> None:
    assert _runtime_identities(V20_MANIFEST) == (
        V20_MANIFEST_SHA256,
        V20_SEMANTIC_SHA256,
        V20_PLAN_SHA256,
        V20_RUNTIME_SHA256,
        V20_SMOKE_RUNTIME_SHA256,
        V20_COVERAGE_SMOKE_RUNTIME_SHA256,
    )
    document = json.loads(V20_MANIFEST.read_bytes())
    document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        "causal_timeout_eligibility"
    ] = CAUSAL_TIMEOUT_ELIGIBILITY_V3
    with pytest.raises(FactorialManifestError, match="causal edge eligibility"):
        parse_manifest_bytes(_encoded(document))


def test_v21_runtime_mirrors_eligibility_without_launch_drift() -> None:
    v21_plan = build_factorial_plan(load_frozen_manifest(V21_MANIFEST))
    v20_plan = build_factorial_plan(load_frozen_manifest(V20_MANIFEST))
    v21_runtime = build_factorial_runtime(v21_plan)
    v20_runtime = build_factorial_runtime(v20_plan)

    assert len(v21_runtime.slots) == len(v20_runtime.slots)
    for v21_slot, v20_slot in zip(v21_runtime.slots, v20_runtime.slots, strict=True):
        assert (
            v21_slot.tiered_cohorts.causal_timeout_eligibility
            == CAUSAL_TIMEOUT_ELIGIBILITY_V3
        )
        assert v20_slot.tiered_cohorts.causal_timeout_eligibility != (
            CAUSAL_TIMEOUT_ELIGIBILITY_V3
        )
        assert v21_slot.fault_window == v20_slot.fault_window
        assert v21_slot.cutoff_contract == v20_slot.cutoff_contract
        assert v21_slot.responsiveness_policy == v20_slot.responsiveness_policy
        assert v21_slot.manager_argv_template == v20_slot.manager_argv_template
        assert v21_slot.replica_argv_templates == v20_slot.replica_argv_templates
        assert v21_slot.transitions == v20_slot.transitions


def test_v21_eligibility_is_bound_into_slot_artifact_identity() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V21_MANIFEST))
    slot = plan.slots[0]
    responsive = slot.byzantine.responsive_degradation
    assert responsive is not None
    runtime = build_slot_runtime(slot)

    downgraded = replace(
        slot,
        byzantine=replace(
            slot.byzantine,
            responsive_degradation=replace(
                responsive,
                causal_timeout_eligibility=(
                    "selection_visible_exact_outstanding_timeout_witnesses_with_"
                    "internal_and_f_plus_one_actor_gates_immature_tail_nonwitness_v2"
                ),
            ),
        ),
    )
    assert build_slot_runtime(downgraded).artifact_id != runtime.artifact_id


def test_v21_all_six_static_identities_are_frozen() -> None:
    assert _runtime_identities(V21_MANIFEST) == (
        V21_MANIFEST_SHA256,
        V21_SEMANTIC_SHA256,
        V21_PLAN_SHA256,
        V21_RUNTIME_SHA256,
        V21_SMOKE_RUNTIME_SHA256,
        V21_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_v21_is_validation_only_and_keeps_ordered_frozen_roots(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST != V21_MANIFEST
    assert cli.main(["--manifest", str(V21_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v43 are validation-only" in refusal["reason"]

    plan = build_factorial_plan(load_frozen_manifest(V21_MANIFEST))
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    n31 = execution.build_n31_coverage_smoke_slot(first)
    assert n7.slot.result_path == (
        "results/shape-placement-factorial-v21-smoke/smoke-n7-f2-PS"
    )
    assert n31.slot == replace(
        first,
        result_path=(
            "results/shape-placement-factorial-v21-coverage-smoke/"
            "slot-066-n31-f5-b05-P"
        ),
    )
    assert plan.automatic_retries == 0
    assert plan.replacement_policy == "none"
    assert build_factorial_runtime(plan).automatic_retries == 0


def test_v21_n31_coverage_smoke_rejects_v20_eligibility() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V21_MANIFEST))
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
                        causal_timeout_eligibility=(
                            "selection_visible_exact_outstanding_timeout_witnesses_"
                            "with_internal_and_f_plus_one_actor_gates_immature_tail_"
                            "nonwitness_v2"
                        ),
                    ),
                ),
            )
        )
