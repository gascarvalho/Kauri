"""Prospective producer contract for source-bound precontainment coverage v15."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from experiments.adaptive import run_shape_factorial_campaign as cli
from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment.factorial_manifest import (
    FROZEN_MANIFEST_ID,
    FROZEN_MANIFEST_SHA256,
    FROZEN_PLAN_SHA256,
    PRECONTAINMENT_FAULT_COVERAGE_GATE_V1,
    V14_MANIFEST_ID,
    V14_MANIFEST_SHA256,
    V14_PLAN_SHA256,
    build_factorial_plan,
    load_frozen_manifest,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
    FROZEN_RUNTIME_SHA256,
    FROZEN_SMOKE_RUNTIME_SHA256,
    ManagerSecretMaterial,
    V14_RUNTIME_SHA256,
    V14_SMOKE_RUNTIME_SHA256,
    build_factorial_runtime,
    canonical_runtime_bytes,
    materialize_manager_argv,
)


REPOSITORY = Path(__file__).resolve().parents[3]
V15_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v15.json"
)
V14_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v14.json"
)


def _secrets(replica_count: int) -> ManagerSecretMaterial:
    return ManagerSecretMaterial(
        manager_tls_private_key_der_hex="11",
        manager_tls_certificate_der_hex="22",
        issuer_private_key_hex="33" * 32,
        replica_tls_certificate_der_hex=tuple("44" for _ in range(replica_count)),
    )


def test_v15_is_frozen_and_v14_bytes_remain_loadable() -> None:
    manifest = load_frozen_manifest(V15_MANIFEST)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)

    assert manifest.manifest_id == FROZEN_MANIFEST_ID == "shape-placement-factorial-v15"
    assert manifest.manifest_sha256 == FROZEN_MANIFEST_SHA256
    assert plan.plan_sha256 == FROZEN_PLAN_SHA256
    assert runtime.runtime_sha256 == FROZEN_RUNTIME_SHA256
    assert manifest.byzantine.responsive_degradation is not None
    assert (
        manifest.byzantine.responsive_degradation.precontainment_fault_coverage_gate
        == PRECONTAINMENT_FAULT_COVERAGE_GATE_V1
    )

    prior = load_frozen_manifest(V14_MANIFEST)
    prior_plan = build_factorial_plan(prior)
    prior_runtime = build_factorial_runtime(prior_plan)
    assert prior.manifest_id == V14_MANIFEST_ID
    assert prior.manifest_sha256 == V14_MANIFEST_SHA256
    assert prior_plan.plan_sha256 == V14_PLAN_SHA256
    assert prior_runtime.runtime_sha256 == V14_RUNTIME_SHA256
    prior_smoke = execution.build_n7_ps_smoke_slot(prior_plan.slots[0])
    assert hashlib.sha256(
        execution._canonical_json_bytes(prior_smoke.runtime.as_document())
    ).hexdigest() == V14_SMOKE_RUNTIME_SHA256


def test_v15_manager_materializes_exact_fault_open_and_full_tree_coverage(
    tmp_path: Path,
) -> None:
    plan = build_factorial_plan(load_frozen_manifest(V15_MANIFEST))
    slot = next(item for item in plan.slots if item.execution_ordinal == 1)
    spec = execution.build_slot_runtime(slot)
    anchor_ns = 7_000_000_000
    argv = materialize_manager_argv(
        spec,
        tmp_path / spec.slot_id,
        _secrets(spec.replica_count),
        shared_raw_clock_anchor_ns=anchor_ns,
    )

    start_option = "--fault-containment-evidence-start-monotonic-ns"
    coverage_option = "--fault-containment-required-tree-coverage"
    assert argv.count(start_option) == 1
    assert argv[argv.index(start_option) + 1] == str(
        anchor_ns
        + spec.fault_window.start_after_prelaunch_anchor_s * 1_000_000_000
    )
    assert argv.count(coverage_option) == 1
    assert spec.tree_count == 21
    assert argv[argv.index(coverage_option) + 1] == str(spec.replica_count) == "31"

    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    n7_argv = materialize_manager_argv(
        n7.runtime,
        tmp_path / n7.runtime.slot_id,
        _secrets(n7.runtime.replica_count),
        shared_raw_clock_anchor_ns=anchor_ns,
    )
    assert n7.runtime.tree_count == 5
    assert n7_argv[n7_argv.index(coverage_option) + 1] == "7"

    prior_plan = build_factorial_plan(load_frozen_manifest(V14_MANIFEST))
    prior_spec = execution.build_slot_runtime(prior_plan.slots[0])
    prior_argv = materialize_manager_argv(
        prior_spec,
        tmp_path / prior_spec.slot_id,
        _secrets(prior_spec.replica_count),
        shared_raw_clock_anchor_ns=anchor_ns,
    )
    assert start_option not in prior_argv
    assert coverage_option not in prior_argv


def test_v15_excluded_n31_coverage_smoke_is_exact_first_campaign_slot() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V15_MANIFEST))
    first = next(item for item in plan.slots if item.execution_ordinal == 1)
    coverage = execution.build_n31_coverage_smoke_slot(first)

    assert first.slot_id == "slot-066-n31-f5-b05-P"
    assert coverage.slot == replace(
        first,
        result_path=(
            "results/shape-placement-factorial-v15-coverage-smoke/"
            "slot-066-n31-f5-b05-P"
        ),
    )
    assert coverage.runtime == execution.build_slot_runtime(coverage.slot)
    assert coverage.campaign_member is False
    assert coverage.figure_eligible is False
    assert coverage.denominator_contribution == 0
    assert hashlib.sha256(
        execution._canonical_json_bytes(coverage.runtime.as_document())
    ).hexdigest() == FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256

    with pytest.raises(
        execution.FactorialExecutionError,
        match="exact v15 campaign slot 066",
    ):
        execution.build_n31_coverage_smoke_slot(
            replace(first, fast_replica_ids=tuple(reversed(first.fast_replica_ids)))
        )


def test_v15_static_runtime_identities_are_distinct() -> None:
    plan = build_factorial_plan(load_frozen_manifest(V15_MANIFEST))
    runtime = build_factorial_runtime(plan)
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    n31 = execution.build_n31_coverage_smoke_slot(
        next(item for item in plan.slots if item.execution_ordinal == 1)
    )

    assert hashlib.sha256(canonical_runtime_bytes(runtime)).hexdigest() == (
        FROZEN_RUNTIME_SHA256
    )
    assert hashlib.sha256(
        execution._canonical_json_bytes(n7.runtime.as_document())
    ).hexdigest() == FROZEN_SMOKE_RUNTIME_SHA256
    assert hashlib.sha256(
        execution._canonical_json_bytes(n31.runtime.as_document())
    ).hexdigest() == FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256
    assert len(
        {
            FROZEN_RUNTIME_SHA256,
            FROZEN_SMOKE_RUNTIME_SHA256,
            FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
        }
    ) == 3


def test_cli_exposes_ordered_two_smoke_workflow() -> None:
    parser = cli._parser()
    assert parser.parse_args(["smoke"]).command == "smoke"
    assert parser.parse_args(["coverage-smoke"]).command == "coverage-smoke"
    assert (
        parser.parse_args(["validate-coverage-smoke"]).command
        == "validate-coverage-smoke"
    )
    assert (
        parser.parse_args(
            ["preflight", "--preflight-target", "coverage-smoke"]
        ).preflight_target
        == "coverage-smoke"
    )
