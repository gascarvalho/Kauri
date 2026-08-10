"""Prospective producer contract for fixed Epoch-1 preselection residency v24."""

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
    V24_MANIFEST_SHA256,
    V24_PLAN_SHA256,
    V24_SEMANTIC_SHA256,
    V23_MANIFEST_ID,
    V23_MANIFEST_SHA256,
    V23_PLAN_SHA256,
    V23_SEMANTIC_SHA256,
    FactorialManifestError,
    build_factorial_plan,
    load_frozen_manifest,
    parse_manifest_bytes,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    V24_COVERAGE_SMOKE_RUNTIME_SHA256,
    V24_RUNTIME_SHA256,
    V24_SMOKE_RUNTIME_SHA256,
    V23_COVERAGE_SMOKE_RUNTIME_SHA256,
    V23_RUNTIME_SHA256,
    V23_SMOKE_RUNTIME_SHA256,
    build_factorial_runtime,
    build_slot_runtime,
    canonical_runtime_bytes,
)

REPOSITORY = Path(__file__).resolve().parents[3]
V24_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v24.json"
)
V23_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v23.json"
)
EPOCH1_PRESELECTION_RESIDENCY_MS = 60_000
MINIMUM_PRIMARY_INTERNAL_OPPORTUNITIES = 82
PRIMARY_GATE_FIELD = (
    "minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_"
    "before_selection"
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


def test_v24_profile_delta_is_exactly_identity_root_and_two_new_fields() -> None:
    v24 = json.loads(V24_MANIFEST.read_bytes())
    v23 = json.loads(V23_MANIFEST.read_bytes())

    assert v24.pop("manifest_id") == "shape-placement-factorial-v24"
    assert v23.pop("manifest_id") == V23_MANIFEST_ID
    assert v24["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v24"
    )
    assert v23["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v23"
    )
    assert v24["workload"].pop("epoch1_preselection_residency_ms") == (  # type: ignore[index]
        EPOCH1_PRESELECTION_RESIDENCY_MS
    )
    responsive = v24["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert responsive.pop(PRIMARY_GATE_FIELD) == MINIMUM_PRIMARY_INTERNAL_OPPORTUNITIES
    assert v24 == v23
    assert manifest_module.V24_MANIFEST_ID == "shape-placement-factorial-v24"


@pytest.mark.parametrize(
    ("section", "field", "replacement"),
    (
        ("workload", "epoch1_preselection_residency_ms", None),
        ("workload", "epoch1_preselection_residency_ms", 59_999),
        ("responsive", PRIMARY_GATE_FIELD, None),
        ("responsive", PRIMARY_GATE_FIELD, 81),
    ),
)
def test_v24_rejects_missing_or_drifted_new_fields(
    section: str,
    field: str,
    replacement: int | None,
) -> None:
    document = json.loads(V24_MANIFEST.read_bytes())
    target = (
        document["workload"]
        if section == "workload"
        else document["byzantine"]["responsive_degradation"]
    )
    if replacement is None:
        target.pop(field)
    else:
        target[field] = replacement

    with pytest.raises(FactorialManifestError):
        parse_manifest_bytes(_encoded(document))


def test_v23_preserves_all_six_identities_and_rejects_v24_fields() -> None:
    assert _runtime_identities(V23_MANIFEST) == (
        V23_MANIFEST_SHA256,
        V23_SEMANTIC_SHA256,
        V23_PLAN_SHA256,
        V23_RUNTIME_SHA256,
        V23_SMOKE_RUNTIME_SHA256,
        V23_COVERAGE_SMOKE_RUNTIME_SHA256,
    )
    document = json.loads(V23_MANIFEST.read_bytes())
    document["workload"][  # type: ignore[index]
        "epoch1_preselection_residency_ms"
    ] = EPOCH1_PRESELECTION_RESIDENCY_MS
    document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        PRIMARY_GATE_FIELD
    ] = MINIMUM_PRIMARY_INTERNAL_OPPORTUNITIES
    with pytest.raises(FactorialManifestError):
        parse_manifest_bytes(_encoded(document))


def test_v24_decouples_transition_residency_from_the_measured_epoch1_window() -> None:
    v24_plan = build_factorial_plan(load_frozen_manifest(V24_MANIFEST))
    v23_plan = build_factorial_plan(load_frozen_manifest(V23_MANIFEST))
    v24_runtime = build_factorial_runtime(v24_plan)
    v23_runtime = build_factorial_runtime(v23_plan)

    assert len(v24_runtime.slots) == len(v23_runtime.slots) == 68
    for v24_slot, v23_slot in zip(v24_runtime.slots, v23_runtime.slots, strict=True):
        assert v24_slot.cutoff_contract == v23_slot.cutoff_contract
        assert v24_slot.cutoff_contract.epoch1_stable_bucket_count == 6
        assert v24_slot.cutoff_contract.bucket_width_s == 5
        assert v24_slot.transitions[0] == v23_slot.transitions[0]
        assert (
            v24_slot.transitions[1].request.minimum_predecessor_residency_ms
            == EPOCH1_PRESELECTION_RESIDENCY_MS
        )
        assert (
            v23_slot.transitions[1].request.minimum_predecessor_residency_ms
            == 30_000
        )
        assert (
            v24_slot.causal_acceptance.epoch1_preselection_residency_ms
            == EPOCH1_PRESELECTION_RESIDENCY_MS
        )
        assert (
            getattr(v24_slot.causal_acceptance, PRIMARY_GATE_FIELD)
            == MINIMUM_PRIMARY_INTERNAL_OPPORTUNITIES
        )
        assert v24_slot.fault_window == v23_slot.fault_window
        assert v24_slot.responsiveness_policy == v23_slot.responsiveness_policy
        assert v24_slot.replica_argv_templates == v23_slot.replica_argv_templates


def test_v24_binds_both_new_fields_into_slot_artifact_identity() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V24_MANIFEST))
    slot = plan.slots[0]
    responsive = slot.byzantine.responsive_degradation
    assert responsive is not None
    runtime = build_slot_runtime(slot)

    historical_contract = replace(
        slot,
        workload=replace(slot.workload, epoch1_preselection_residency_ms=None),
        byzantine=replace(
            slot.byzantine,
            responsive_degradation=replace(
                responsive,
                minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection=None,
            ),
        ),
    )
    assert build_slot_runtime(historical_contract).artifact_id != runtime.artifact_id

    with pytest.raises(FactorialManifestError, match="preselection residency"):
        build_slot_runtime(
            replace(
                slot,
                workload=replace(
                    slot.workload, epoch1_preselection_residency_ms=60_001
                ),
            )
        )
    with pytest.raises(FactorialManifestError, match="opportunity minimum"):
        build_slot_runtime(
            replace(
                slot,
                byzantine=replace(
                    slot.byzantine,
                    responsive_degradation=replace(
                        responsive,
                        minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection=83,
                    ),
                ),
            )
        )


def test_v24_all_six_static_identities_are_frozen() -> None:
    assert _runtime_identities(V24_MANIFEST) == (
        V24_MANIFEST_SHA256,
        V24_SEMANTIC_SHA256,
        V24_PLAN_SHA256,
        V24_RUNTIME_SHA256,
        V24_SMOKE_RUNTIME_SHA256,
        V24_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_v24_is_validation_only_and_keeps_single_slot_zero_retry_roots(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v28.json"
    assert cli.main(["--manifest", str(V24_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v27 are validation-only" in refusal["reason"]

    plan = build_factorial_plan(load_frozen_manifest(V24_MANIFEST))
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    n31 = execution.build_n31_coverage_smoke_slot(first)
    assert n7.slot.result_path == (
        "results/shape-placement-factorial-v24-smoke/smoke-n7-f2-PS"
    )
    assert n31.slot == replace(
        first,
        result_path=(
            "results/shape-placement-factorial-v24-coverage-smoke/"
            "slot-066-n31-f5-b05-P"
        ),
    )
    assert n7.runtime.transitions[1].request.minimum_predecessor_residency_ms == 60_000
    assert n31.runtime.transitions[1].request.minimum_predecessor_residency_ms == 60_000
    assert plan.automatic_retries == 0
    assert plan.replacement_policy == "none"
    assert build_factorial_runtime(plan).automatic_retries == 0
