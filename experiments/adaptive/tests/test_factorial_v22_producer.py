"""Prospective producer contract for topology-derived snapshot selection v22."""

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
    EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1,
    V21_MANIFEST_ID,
    V21_MANIFEST_SHA256,
    V21_PLAN_SHA256,
    V21_SEMANTIC_SHA256,
    V22_MANIFEST_SHA256,
    V22_PLAN_SHA256,
    V22_SEMANTIC_SHA256,
    FactorialManifestError,
    build_factorial_plan,
    load_frozen_manifest,
    parse_manifest_bytes,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    V21_COVERAGE_SMOKE_RUNTIME_SHA256,
    V21_RUNTIME_SHA256,
    V21_SMOKE_RUNTIME_SHA256,
    V22_COVERAGE_SMOKE_RUNTIME_SHA256,
    V22_RUNTIME_SHA256,
    V22_SMOKE_RUNTIME_SHA256,
    build_factorial_runtime,
    build_slot_runtime,
    canonical_runtime_bytes,
)

REPOSITORY = Path(__file__).resolve().parents[3]
V22_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v22.json"
)
V21_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v21.json"
)
SNAPSHOT_SELECTION_CONTRACT_V1 = (
    "full_prefix_without_inherited_consensus_wait_exempt_and_baseline_exclusive_"
    "suffix_with_exact_inherited_consensus_wait_exempt_v1"
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


def test_v22_only_changes_identity_root_and_snapshot_selection_contract() -> None:
    v22 = json.loads(V22_MANIFEST.read_bytes())
    v21 = json.loads(V21_MANIFEST.read_bytes())

    assert v22.pop("manifest_id") == "shape-placement-factorial-v22"
    assert v21.pop("manifest_id") == V21_MANIFEST_ID
    assert v22["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v22"
    )
    assert v21["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v21"
    )
    v22_responsive = v22["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v21_responsive = v21["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert v22_responsive.pop("evidence_snapshot_selection_contract") == (
        SNAPSHOT_SELECTION_CONTRACT_V1
    )
    assert "evidence_snapshot_selection_contract" not in v21_responsive
    assert v22 == v21

    assert EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1 == (
        SNAPSHOT_SELECTION_CONTRACT_V1
    )
    assert manifest_module.V22_MANIFEST_ID == "shape-placement-factorial-v22"


@pytest.mark.parametrize("replacement", (None, "intent_selected_snapshot_v0"))
def test_v22_rejects_missing_or_forged_snapshot_selection_contract(
    replacement: str | None,
) -> None:
    document = json.loads(V22_MANIFEST.read_bytes())
    responsive = document["byzantine"]["responsive_degradation"]
    if replacement is None:
        responsive.pop("evidence_snapshot_selection_contract")
    else:
        responsive["evidence_snapshot_selection_contract"] = replacement

    with pytest.raises(
        FactorialManifestError,
        match="snapshot selection|fields are not frozen",
    ):
        parse_manifest_bytes(_encoded(document))


def test_v21_preserves_all_identities_and_rejects_v22_contract() -> None:
    assert _runtime_identities(V21_MANIFEST) == (
        V21_MANIFEST_SHA256,
        V21_SEMANTIC_SHA256,
        V21_PLAN_SHA256,
        V21_RUNTIME_SHA256,
        V21_SMOKE_RUNTIME_SHA256,
        V21_COVERAGE_SMOKE_RUNTIME_SHA256,
    )
    document = json.loads(V21_MANIFEST.read_bytes())
    document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        "evidence_snapshot_selection_contract"
    ] = SNAPSHOT_SELECTION_CONTRACT_V1
    with pytest.raises(FactorialManifestError, match="snapshot selection|fields"):
        parse_manifest_bytes(_encoded(document))


def test_v22_runtime_mirrors_snapshot_selection_without_launch_drift() -> None:
    v22_plan = build_factorial_plan(load_frozen_manifest(V22_MANIFEST))
    v21_plan = build_factorial_plan(load_frozen_manifest(V21_MANIFEST))
    v22_runtime = build_factorial_runtime(v22_plan)
    v21_runtime = build_factorial_runtime(v21_plan)

    assert len(v22_runtime.slots) == len(v21_runtime.slots)
    for v22_slot, v21_slot in zip(v22_runtime.slots, v21_runtime.slots, strict=True):
        assert (
            v22_slot.causal_acceptance.evidence_snapshot_selection_contract
            == SNAPSHOT_SELECTION_CONTRACT_V1
        )
        assert (
            v21_slot.causal_acceptance.evidence_snapshot_selection_contract is None
        )
        assert "evidence_snapshot_selection_contract" not in (
            v22_slot.tiered_cohorts.as_document()  # type: ignore[union-attr]
        )
        assert v22_slot.fault_window == v21_slot.fault_window
        assert v22_slot.cutoff_contract == v21_slot.cutoff_contract
        assert v22_slot.responsiveness_policy == v21_slot.responsiveness_policy
        assert v22_slot.manager_argv_template == v21_slot.manager_argv_template
        assert v22_slot.replica_argv_templates == v21_slot.replica_argv_templates
        assert v22_slot.transitions == v21_slot.transitions


def test_v22_snapshot_selection_is_bound_into_slot_artifact_identity() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V22_MANIFEST))
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
                evidence_snapshot_selection_contract=None,
            ),
        ),
    )
    assert build_slot_runtime(downgraded).artifact_id != runtime.artifact_id


def test_v22_all_six_static_identities_are_frozen() -> None:
    assert _runtime_identities(V22_MANIFEST) == (
        V22_MANIFEST_SHA256,
        V22_SEMANTIC_SHA256,
        V22_PLAN_SHA256,
        V22_RUNTIME_SHA256,
        V22_SMOKE_RUNTIME_SHA256,
        V22_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_v22_is_validation_only_and_keeps_ordered_frozen_roots(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST != V22_MANIFEST
    assert cli.main(["--manifest", str(V22_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v42 are validation-only" in refusal["reason"]

    plan = build_factorial_plan(load_frozen_manifest(V22_MANIFEST))
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    n31 = execution.build_n31_coverage_smoke_slot(first)
    assert n7.slot.result_path == (
        "results/shape-placement-factorial-v22-smoke/smoke-n7-f2-PS"
    )
    assert n31.slot == replace(
        first,
        result_path=(
            "results/shape-placement-factorial-v22-coverage-smoke/"
            "slot-066-n31-f5-b05-P"
        ),
    )
    assert plan.automatic_retries == 0
    assert plan.replacement_policy == "none"
    assert build_factorial_runtime(plan).automatic_retries == 0


def test_v22_n31_coverage_smoke_rejects_missing_selection_contract() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V22_MANIFEST))
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
                        evidence_snapshot_selection_contract=None,
                    ),
                ),
            )
        )
