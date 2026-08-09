"""Prospective producer contract for exact outstanding timeout witnesses v16."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive import run_shape_factorial_campaign as cli
from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment.factorial_manifest import (
    FROZEN_MANIFEST_ID,
    FROZEN_MANIFEST_SHA256,
    FROZEN_PLAN_SHA256,
    FROZEN_SEMANTIC_SHA256,
    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2,
    V15_MANIFEST_ID,
    V15_MANIFEST_SHA256,
    V15_PLAN_SHA256,
    V15_SEMANTIC_SHA256,
    build_factorial_plan,
    load_frozen_manifest,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
    FROZEN_RUNTIME_SHA256,
    FROZEN_SMOKE_RUNTIME_SHA256,
    V15_COVERAGE_SMOKE_RUNTIME_SHA256,
    V15_RUNTIME_SHA256,
    V15_SMOKE_RUNTIME_SHA256,
    build_factorial_runtime,
    canonical_runtime_bytes,
)


REPOSITORY = Path(__file__).resolve().parents[3]
V16_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v16.json"
)
V15_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v15.json"
)


def _runtime_identities(path: Path) -> tuple[str, str, str, str, str, str]:
    manifest = load_frozen_manifest(path)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    n31 = execution.build_n31_coverage_smoke_slot(first)
    semantic = (
        json.dumps(
            json.loads(path.read_bytes()),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
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


def test_v16_only_changes_identity_root_and_timeout_witness_contract() -> None:
    v16 = json.loads(V16_MANIFEST.read_bytes())
    v15 = json.loads(V15_MANIFEST.read_bytes())

    assert v16.pop("manifest_id") == FROZEN_MANIFEST_ID == (
        "shape-placement-factorial-v16"
    )
    assert v15.pop("manifest_id") == V15_MANIFEST_ID == (
        "shape-placement-factorial-v15"
    )
    assert v16["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v16"
    )
    assert v15["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v15"
    )
    v16_responsive = v16["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v15_responsive = v15["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert v16_responsive.pop("causal_timeout_eligibility") == (  # type: ignore[union-attr]
        RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2
    )
    assert v15_responsive.pop("causal_timeout_eligibility") == (  # type: ignore[union-attr]
        "exact_parent_attempt_absolute_deadline_strictly_before_"
        "selecting_transition_v1"
    )
    assert v16 == v15


def test_v16_and_v15_all_six_static_identities_are_frozen() -> None:
    assert _runtime_identities(V16_MANIFEST) == (
        FROZEN_MANIFEST_SHA256,
        FROZEN_SEMANTIC_SHA256,
        FROZEN_PLAN_SHA256,
        FROZEN_RUNTIME_SHA256,
        FROZEN_SMOKE_RUNTIME_SHA256,
        FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
    )
    assert _runtime_identities(V15_MANIFEST) == (
        V15_MANIFEST_SHA256,
        V15_SEMANTIC_SHA256,
        V15_PLAN_SHA256,
        V15_RUNTIME_SHA256,
        V15_SMOKE_RUNTIME_SHA256,
        V15_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_v16_default_refuses_v15_production_and_keeps_ordered_smokes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST == V16_MANIFEST
    assert cli.main(["--manifest", str(V15_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v15 are validation-only" in refusal["reason"]

    plan = build_factorial_plan(load_frozen_manifest(V16_MANIFEST))
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    n31 = execution.build_n31_coverage_smoke_slot(first)
    assert n7.slot.slot_id == "smoke-n7-f2-PS"
    assert n31.slot == replace(
        first,
        result_path=(
            "results/shape-placement-factorial-v16-coverage-smoke/"
            "slot-066-n31-f5-b05-P"
        ),
    )

    assert first.byzantine.responsive_degradation is not None
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
                        first.byzantine.responsive_degradation,
                        causal_timeout_eligibility=(
                            "exact_parent_attempt_absolute_deadline_strictly_before_"
                            "selecting_transition_v1"
                        ),
                    ),
                ),
            )
        )
