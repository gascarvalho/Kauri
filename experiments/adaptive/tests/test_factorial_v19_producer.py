"""Prospective producer contract for future-tree proposal delivery v19."""

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
    FactorialManifestError,
    V19_MANIFEST_SHA256,
    V19_PLAN_SHA256,
    V19_SEMANTIC_SHA256,
    V18_MANIFEST_SHA256,
    V18_PLAN_SHA256,
    V18_SEMANTIC_SHA256,
    build_factorial_plan,
    load_frozen_manifest,
    parse_manifest_bytes,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    V19_COVERAGE_SMOKE_RUNTIME_SHA256,
    V19_RUNTIME_SHA256,
    V19_SMOKE_RUNTIME_SHA256,
    V18_COVERAGE_SMOKE_RUNTIME_SHA256,
    V18_RUNTIME_SHA256,
    V18_SMOKE_RUNTIME_SHA256,
    build_factorial_runtime,
    build_slot_runtime,
    canonical_runtime_bytes,
)

REPOSITORY = Path(__file__).resolve().parents[3]
V19_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v19.json"
)
V18_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v18.json"
)
FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT = (
    "exact_immediate_successor_tree_proposals_relayed_and_buffered_without_pre_"
    "activation_protocol_effects_then_revalidated_and_replayed_once_after_exact_"
    "activation_v1"
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


def test_v19_only_changes_identity_root_and_future_tree_contract() -> None:
    v19 = json.loads(V19_MANIFEST.read_bytes())
    v18 = json.loads(V18_MANIFEST.read_bytes())

    assert v19.pop("manifest_id") == "shape-placement-factorial-v19"
    assert v18.pop("manifest_id") == "shape-placement-factorial-v18"
    assert v19["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v19"
    )
    assert v18["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v18"
    )
    v19_responsive = v19["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v18_responsive = v18["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert (
        v19_responsive.pop(  # type: ignore[union-attr]
            "future_tree_proposal_delivery_contract"
        )
        == FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT
    )
    assert "future_tree_proposal_delivery_contract" not in v18_responsive
    assert v19 == v18

    assert FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1 == (
        FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT
    )
    assert manifest_module.V19_MANIFEST_ID == "shape-placement-factorial-v19"


@pytest.mark.parametrize("replacement", (None, "relay_future_tree_without_revalidation"))
def test_v19_rejects_missing_or_forged_future_tree_contract(
    replacement: str | None,
) -> None:
    document = json.loads(V19_MANIFEST.read_bytes())
    responsive = document["byzantine"]["responsive_degradation"]
    if replacement is None:
        responsive.pop("future_tree_proposal_delivery_contract")
    else:
        responsive["future_tree_proposal_delivery_contract"] = replacement

    with pytest.raises(
        FactorialManifestError,
        match="future-tree proposal delivery contract|fields are not frozen",
    ):
        parse_manifest_bytes(_encoded(document))


def test_v18_rejects_forged_v19_field_and_still_dispatches() -> None:
    assert _runtime_identities(V18_MANIFEST) == (
        V18_MANIFEST_SHA256,
        V18_SEMANTIC_SHA256,
        V18_PLAN_SHA256,
        V18_RUNTIME_SHA256,
        V18_SMOKE_RUNTIME_SHA256,
        V18_COVERAGE_SMOKE_RUNTIME_SHA256,
    )
    document = json.loads(V18_MANIFEST.read_bytes())
    document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        "future_tree_proposal_delivery_contract"
    ] = FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT
    with pytest.raises(FactorialManifestError, match="fields are not frozen"):
        parse_manifest_bytes(_encoded(document))


def test_v19_runtime_mirrors_future_tree_contract_without_launch_drift() -> None:
    v19_plan = build_factorial_plan(load_frozen_manifest(V19_MANIFEST))
    v18_plan = build_factorial_plan(load_frozen_manifest(V18_MANIFEST))
    v19_runtime = build_factorial_runtime(v19_plan)
    v18_runtime = build_factorial_runtime(v18_plan)

    assert len(v19_runtime.slots) == len(v18_runtime.slots)
    for v19_slot, v18_slot in zip(v19_runtime.slots, v18_runtime.slots, strict=True):
        assert (
            v19_slot.causal_acceptance.future_tree_proposal_delivery_contract
            == FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT
        )
        assert v18_slot.causal_acceptance.future_tree_proposal_delivery_contract is None
        assert v19_slot.fault_window == v18_slot.fault_window
        assert v19_slot.cutoff_contract == v18_slot.cutoff_contract
        assert v19_slot.responsiveness_policy == v18_slot.responsiveness_policy
        assert v19_slot.manager_argv_template == v18_slot.manager_argv_template
        assert v19_slot.replica_argv_templates == v18_slot.replica_argv_templates
        assert v19_slot.transitions == v18_slot.transitions


def test_v19_future_tree_contract_is_bound_into_slot_artifact_identity() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V19_MANIFEST))
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
                future_tree_proposal_delivery_contract=None,
            ),
        ),
    )
    assert build_slot_runtime(missing).artifact_id != runtime.artifact_id


def test_v19_all_six_static_identities_are_frozen() -> None:
    assert _runtime_identities(V19_MANIFEST) == (
        V19_MANIFEST_SHA256,
        V19_SEMANTIC_SHA256,
        V19_PLAN_SHA256,
        V19_RUNTIME_SHA256,
        V19_SMOKE_RUNTIME_SHA256,
        V19_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_v19_is_validation_only_and_keeps_ordered_fresh_roots(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST != V19_MANIFEST
    assert cli.main(["--manifest", str(V19_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v36 are validation-only" in refusal["reason"]

    plan = build_factorial_plan(load_frozen_manifest(V19_MANIFEST))
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    n31 = execution.build_n31_coverage_smoke_slot(first)
    assert n7.slot.result_path == (
        "results/shape-placement-factorial-v19-smoke/smoke-n7-f2-PS"
    )
    assert n31.slot == replace(
        first,
        result_path=(
            "results/shape-placement-factorial-v19-coverage-smoke/"
            "slot-066-n31-f5-b05-P"
        ),
    )
    assert plan.automatic_retries == 0
    assert build_factorial_runtime(plan).automatic_retries == 0


def test_v19_n31_coverage_smoke_rejects_missing_future_tree_contract() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V19_MANIFEST))
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
                        future_tree_proposal_delivery_contract=None,
                    ),
                ),
            )
        )
