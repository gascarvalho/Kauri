"""Prospective producer contract for native proposal witnesses v20."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive import run_shape_factorial_campaign as cli
from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment.factorial_manifest import (
    FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1,
    SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1,
    FactorialManifestError,
    V20_MANIFEST_ID,
    V20_MANIFEST_SHA256,
    V20_PLAN_SHA256,
    V20_SEMANTIC_SHA256,
    V19_MANIFEST_SHA256,
    V19_PLAN_SHA256,
    V19_SEMANTIC_SHA256,
    build_factorial_plan,
    load_frozen_manifest,
    parse_manifest_bytes,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    V20_COVERAGE_SMOKE_RUNTIME_SHA256,
    V20_RUNTIME_SHA256,
    V20_SMOKE_RUNTIME_SHA256,
    V19_COVERAGE_SMOKE_RUNTIME_SHA256,
    V19_RUNTIME_SHA256,
    V19_SMOKE_RUNTIME_SHA256,
    build_factorial_runtime,
    build_slot_runtime,
    canonical_runtime_bytes,
)

REPOSITORY = Path(__file__).resolve().parents[3]
V20_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v20.json"
)
V19_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v19.json"
)
SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT = (
    "strictly_bijected_source_bound_fault_contribution_opportunity_proposal_"
    "keys_are_native_proposal_configuration_witnesses_after_exact_topology_"
    "validation_v1"
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


def test_v20_only_changes_identity_root_and_proposal_witness_contract() -> None:
    v20 = json.loads(V20_MANIFEST.read_bytes())
    v19 = json.loads(V19_MANIFEST.read_bytes())

    assert v20.pop("manifest_id") == "shape-placement-factorial-v20"
    assert v19.pop("manifest_id") == "shape-placement-factorial-v19"
    assert v20["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v20"
    )
    assert v19["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v19"
    )
    v20_responsive = v20["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v19_responsive = v19["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert (
        v20_responsive.pop(  # type: ignore[union-attr]
            "source_bound_proposal_witness_contract"
        )
        == SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT
    )
    assert "source_bound_proposal_witness_contract" not in v19_responsive
    assert v20 == v19

    assert SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1 == (
        SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT
    )
    assert V20_MANIFEST_ID == "shape-placement-factorial-v20"


@pytest.mark.parametrize("replacement", (None, "accept_unpaired_shutdown_marker"))
def test_v20_rejects_missing_or_forged_proposal_witness_contract(
    replacement: str | None,
) -> None:
    document = json.loads(V20_MANIFEST.read_bytes())
    responsive = document["byzantine"]["responsive_degradation"]
    if replacement is None:
        responsive.pop("source_bound_proposal_witness_contract")
    else:
        responsive["source_bound_proposal_witness_contract"] = replacement

    with pytest.raises(
        FactorialManifestError,
        match="source-bound proposal witness contract|fields are not frozen",
    ):
        parse_manifest_bytes(_encoded(document))


def test_v19_rejects_forged_v20_field_and_preserves_all_identities() -> None:
    assert _runtime_identities(V19_MANIFEST) == (
        V19_MANIFEST_SHA256,
        V19_SEMANTIC_SHA256,
        V19_PLAN_SHA256,
        V19_RUNTIME_SHA256,
        V19_SMOKE_RUNTIME_SHA256,
        V19_COVERAGE_SMOKE_RUNTIME_SHA256,
    )
    document = json.loads(V19_MANIFEST.read_bytes())
    document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        "source_bound_proposal_witness_contract"
    ] = SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT
    with pytest.raises(FactorialManifestError, match="fields are not frozen"):
        parse_manifest_bytes(_encoded(document))


def test_v20_runtime_mirrors_witness_contract_without_launch_drift() -> None:
    v20_plan = build_factorial_plan(load_frozen_manifest(V20_MANIFEST))
    v19_plan = build_factorial_plan(load_frozen_manifest(V19_MANIFEST))
    v20_runtime = build_factorial_runtime(v20_plan)
    v19_runtime = build_factorial_runtime(v19_plan)

    assert len(v20_runtime.slots) == len(v19_runtime.slots)
    for v20_slot, v19_slot in zip(v20_runtime.slots, v19_runtime.slots, strict=True):
        assert (
            v20_slot.causal_acceptance.source_bound_proposal_witness_contract
            == SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT
        )
        assert (
            v20_slot.causal_acceptance.future_tree_proposal_delivery_contract
            == FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1
        )
        assert v19_slot.causal_acceptance.source_bound_proposal_witness_contract is None
        assert v20_slot.fault_window == v19_slot.fault_window
        assert v20_slot.cutoff_contract == v19_slot.cutoff_contract
        assert v20_slot.responsiveness_policy == v19_slot.responsiveness_policy
        assert v20_slot.manager_argv_template == v19_slot.manager_argv_template
        assert v20_slot.replica_argv_templates == v19_slot.replica_argv_templates
        assert v20_slot.transitions == v19_slot.transitions


def test_v20_witness_contract_is_bound_into_slot_artifact_identity() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V20_MANIFEST))
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
                source_bound_proposal_witness_contract=None,
            ),
        ),
    )
    assert build_slot_runtime(missing).artifact_id != runtime.artifact_id


def test_v20_all_six_static_identities_are_frozen() -> None:
    assert _runtime_identities(V20_MANIFEST) == (
        V20_MANIFEST_SHA256,
        V20_SEMANTIC_SHA256,
        V20_PLAN_SHA256,
        V20_RUNTIME_SHA256,
        V20_SMOKE_RUNTIME_SHA256,
        V20_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_v20_is_validation_only_and_keeps_ordered_fresh_roots(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST != V20_MANIFEST
    assert cli.main(["--manifest", str(V20_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v39 are validation-only" in refusal["reason"]

    plan = build_factorial_plan(load_frozen_manifest(V20_MANIFEST))
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    n31 = execution.build_n31_coverage_smoke_slot(first)
    assert n7.slot.result_path == (
        "results/shape-placement-factorial-v20-smoke/smoke-n7-f2-PS"
    )
    assert n31.slot == replace(
        first,
        result_path=(
            "results/shape-placement-factorial-v20-coverage-smoke/"
            "slot-066-n31-f5-b05-P"
        ),
    )
    assert plan.automatic_retries == 0
    assert plan.replacement_policy == "none"
    assert build_factorial_runtime(plan).automatic_retries == 0


def test_v20_n31_coverage_smoke_rejects_missing_witness_contract() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V20_MANIFEST))
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
                        source_bound_proposal_witness_contract=None,
                    ),
                ),
            )
        )
