"""Prospective producer contract for bounded future-proposal delivery v23."""

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
    FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1,
    FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2,
    V22_MANIFEST_ID,
    V22_MANIFEST_SHA256,
    V22_PLAN_SHA256,
    V22_SEMANTIC_SHA256,
    V23_MANIFEST_SHA256,
    V23_PLAN_SHA256,
    V23_SEMANTIC_SHA256,
    FactorialManifestError,
    build_factorial_plan,
    load_frozen_manifest,
    parse_manifest_bytes,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    V22_COVERAGE_SMOKE_RUNTIME_SHA256,
    V22_RUNTIME_SHA256,
    V22_SMOKE_RUNTIME_SHA256,
    V23_COVERAGE_SMOKE_RUNTIME_SHA256,
    V23_RUNTIME_SHA256,
    V23_SMOKE_RUNTIME_SHA256,
    build_factorial_runtime,
    build_slot_runtime,
    canonical_runtime_bytes,
)

REPOSITORY = Path(__file__).resolve().parents[3]
V23_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v23.json"
)
V22_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v22.json"
)
DELIVERY_CONTRACT_V2 = (
    "exact_nonwrapping_same_epoch_tree_count_minus_one_future_proposal_horizon_"
    "is_capacity_bounded_relayed_and_buffered_without_pre_activation_protocol_"
    "effects_then_revalidated_and_replayed_once_after_each_exact_activation_v2"
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


def test_v23_only_changes_identity_root_and_future_delivery_contract() -> None:
    v23 = json.loads(V23_MANIFEST.read_bytes())
    v22 = json.loads(V22_MANIFEST.read_bytes())

    assert v23.pop("manifest_id") == "shape-placement-factorial-v23"
    assert v22.pop("manifest_id") == V22_MANIFEST_ID
    assert v23["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v23"
    )
    assert v22["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v22"
    )
    v23_responsive = v23["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v22_responsive = v22["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert v23_responsive.pop("future_tree_proposal_delivery_contract") == (
        DELIVERY_CONTRACT_V2
    )
    assert v22_responsive.pop("future_tree_proposal_delivery_contract") == (
        FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1
    )
    assert v23 == v22

    assert FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2 == DELIVERY_CONTRACT_V2
    assert manifest_module.V23_MANIFEST_ID == "shape-placement-factorial-v23"


@pytest.mark.parametrize("replacement", (None, FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1))
def test_v23_rejects_missing_or_downgraded_delivery_contract(
    replacement: str | None,
) -> None:
    document = json.loads(V23_MANIFEST.read_bytes())
    responsive = document["byzantine"]["responsive_degradation"]
    if replacement is None:
        responsive.pop("future_tree_proposal_delivery_contract")
    else:
        responsive["future_tree_proposal_delivery_contract"] = replacement

    with pytest.raises(
        FactorialManifestError,
        match="future-tree proposal delivery|fields are not frozen",
    ):
        parse_manifest_bytes(_encoded(document))


def test_v22_preserves_all_identities_and_rejects_v23_contract() -> None:
    assert _runtime_identities(V22_MANIFEST) == (
        V22_MANIFEST_SHA256,
        V22_SEMANTIC_SHA256,
        V22_PLAN_SHA256,
        V22_RUNTIME_SHA256,
        V22_SMOKE_RUNTIME_SHA256,
        V22_COVERAGE_SMOKE_RUNTIME_SHA256,
    )
    document = json.loads(V22_MANIFEST.read_bytes())
    document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        "future_tree_proposal_delivery_contract"
    ] = DELIVERY_CONTRACT_V2
    with pytest.raises(FactorialManifestError, match="future-tree proposal delivery"):
        parse_manifest_bytes(_encoded(document))


def test_v23_runtime_mirrors_delivery_contract_without_launch_drift() -> None:
    v23_plan = build_factorial_plan(load_frozen_manifest(V23_MANIFEST))
    v22_plan = build_factorial_plan(load_frozen_manifest(V22_MANIFEST))
    v23_runtime = build_factorial_runtime(v23_plan)
    v22_runtime = build_factorial_runtime(v22_plan)

    assert len(v23_runtime.slots) == len(v22_runtime.slots)
    for v23_slot, v22_slot in zip(v23_runtime.slots, v22_runtime.slots, strict=True):
        assert (
            v23_slot.causal_acceptance.future_tree_proposal_delivery_contract
            == DELIVERY_CONTRACT_V2
        )
        assert (
            v22_slot.causal_acceptance.future_tree_proposal_delivery_contract
            == FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1
        )
        assert v23_slot.tiered_cohorts == v22_slot.tiered_cohorts
        assert v23_slot.fault_window == v22_slot.fault_window
        assert v23_slot.cutoff_contract == v22_slot.cutoff_contract
        assert v23_slot.responsiveness_policy == v22_slot.responsiveness_policy
        assert v23_slot.manager_argv_template == v22_slot.manager_argv_template
        assert v23_slot.replica_argv_templates == v22_slot.replica_argv_templates
        assert v23_slot.transitions == v22_slot.transitions


def test_v23_delivery_contract_is_bound_into_slot_artifact_identity() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V23_MANIFEST))
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
                future_tree_proposal_delivery_contract=(
                    FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1
                ),
            ),
        ),
    )
    assert build_slot_runtime(downgraded).artifact_id != runtime.artifact_id


def test_v23_all_six_static_identities_are_frozen() -> None:
    assert _runtime_identities(V23_MANIFEST) == (
        V23_MANIFEST_SHA256,
        V23_SEMANTIC_SHA256,
        V23_PLAN_SHA256,
        V23_RUNTIME_SHA256,
        V23_SMOKE_RUNTIME_SHA256,
        V23_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_v23_is_validation_only_and_keeps_historical_ordered_roots(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--manifest", str(V23_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v42 are validation-only" in refusal["reason"]

    plan = build_factorial_plan(load_frozen_manifest(V23_MANIFEST))
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    n31 = execution.build_n31_coverage_smoke_slot(first)
    assert n7.slot.result_path == (
        "results/shape-placement-factorial-v23-smoke/smoke-n7-f2-PS"
    )
    assert n31.slot == replace(
        first,
        result_path=(
            "results/shape-placement-factorial-v23-coverage-smoke/"
            "slot-066-n31-f5-b05-P"
        ),
    )
    assert plan.automatic_retries == 0
    assert plan.replacement_policy == "none"
    assert build_factorial_runtime(plan).automatic_retries == 0


def test_v23_n31_coverage_smoke_rejects_v22_delivery_contract() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V23_MANIFEST))
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
                        future_tree_proposal_delivery_contract=(
                            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1
                        ),
                    ),
                ),
            )
        )
