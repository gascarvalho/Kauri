"""Prospective producer contract for the validation-only v30 version roll."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive import run_shape_factorial_campaign as cli
from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment import factorial_manifest as manifest_module
from experiments.adaptive.kauri_experiment import factorial_runtime as runtime_module

REPOSITORY = Path(__file__).resolve().parents[3]
V30_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v30.json"
)
V29_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v29.json"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V30_MANIFEST.read_bytes()
    semantic_sha256 = hashlib.sha256(_canonical(json.loads(payload))).hexdigest()
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_SEMANTIC_SHA256",
        semantic_sha256,
    )
    manifest = manifest_module.parse_manifest_bytes(payload)
    plan = manifest_module.build_factorial_plan(manifest)
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_MANIFEST_SHA256",
        manifest.manifest_sha256,
    )
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_PLAN_SHA256",
        plan.plan_sha256,
    )
    return manifest, plan


def _coverage(plan):
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    return execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )


def _candidate_coverage(monkeypatch: pytest.MonkeyPatch):
    manifest, plan = _candidate_plan(monkeypatch)
    return manifest, plan, _coverage(plan)


def _static_artifacts(manifest_path: Path, plan, coverage) -> dict[str, bytes]:
    return {
        "manifest.json": manifest_path.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": execution._canonical_json_bytes(
            coverage.runtime.as_document()
        ),
    }


def test_v30_profile_delta_is_only_identity_and_results_root() -> None:
    v30 = json.loads(V30_MANIFEST.read_bytes())
    v29 = json.loads(V29_MANIFEST.read_bytes())

    assert v30.pop("manifest_id") == "shape-placement-factorial-v30"
    assert v29.pop("manifest_id") == "shape-placement-factorial-v29"
    assert v30["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v30"
    )
    assert v29["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v29"
    )
    assert v30 == v29


def test_v29_identities_are_explicit_historical_aliases() -> None:
    assert (
        manifest_module.V29_MANIFEST_ID,
        manifest_module.V29_MANIFEST_SHA256,
        manifest_module.V29_SEMANTIC_SHA256,
        manifest_module.V29_PLAN_SHA256,
        runtime_module.V29_RUNTIME_SHA256,
        runtime_module.V29_SMOKE_RUNTIME_SHA256,
        runtime_module.V29_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "shape-placement-factorial-v29",
        "e012be15b6193263138de7b5aee5f78eb67de10182bc07e4494373d648fde5fa",
        "605992c544a12c3652f1279bb960325bc94fe7cd72ebb251d1c5b8e101701361",
        "8ca31c9d0ae26c7deedbb1932a161652f146e2ee6cb54bf08e9bf4ae5088bc91",
        "162531c501ba866b51033af411b5debb7d1e805c254d5a8fedcd29b59337a026",
        "5780c65662cbb1312f61f753e8d66237005ef666264bf99351b2d33f15d6680c",
        "4042c313856f722a5abef6e537a8a73de1699dfdde17da0d9ab2bc8a628c995a",
    )


def test_v30_six_identities_are_exact_historical_values() -> None:
    assert manifest_module.V30_MANIFEST_ID == "shape-placement-factorial-v30"
    assert (
        manifest_module.V30_MANIFEST_SHA256,
        manifest_module.V30_SEMANTIC_SHA256,
        manifest_module.V30_PLAN_SHA256,
        runtime_module.V30_RUNTIME_SHA256,
        runtime_module.V30_SMOKE_RUNTIME_SHA256,
        runtime_module.V30_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "c868d9ebce0f3afbdfd6011787f385395b6df43b633f6cfad284726c2d3f52e4",
        "4d3130b1a02c9a0f4c67f8a29fb22000e15c2d9bf64ebd87af32518f22fb15b2",
        "3ed7a8a9b80f18c4b48d2a3daf16c44fba458f47ae84c6e4a4bdf907b4d65e1f",
        "a0dfe6f503b0e013e757707697221c63711074d39490e7075887754987a22a44",
        "6e8540045d60601810672d8f84adc8b4b7800bf5989e386ae39c3f6fa0d22beb",
        "14429819aefef1a4e664f3fd32837cf659a5a10203929526e1137bc392389862",
    )


def test_v29_exact_history_remains_loadable() -> None:
    manifest = manifest_module.load_frozen_manifest(V29_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    coverage = _coverage(plan)

    assert manifest.manifest_id == manifest_module.V29_MANIFEST_ID
    assert manifest.manifest_sha256 == manifest_module.V29_MANIFEST_SHA256
    assert plan.plan_sha256 == manifest_module.V29_PLAN_SHA256
    assert hashlib.sha256(runtime_module.canonical_runtime_bytes(runtime)).hexdigest() == (
        runtime_module.V29_RUNTIME_SHA256
    )
    assert hashlib.sha256(
        execution._canonical_json_bytes(coverage.runtime.as_document())
    ).hexdigest() == runtime_module.V29_COVERAGE_SMOKE_RUNTIME_SHA256


def test_v30_preserves_v29_timing_and_exact_repair_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coverage = _candidate_coverage(monkeypatch)
    primary_slot, repair_slot = coverage.slots
    primary_runtime, repair_runtime = coverage.runtimes

    assert primary_slot.byzantine.duration_s == 450
    assert primary_runtime.fault_window.duration_s == 450
    assert primary_runtime.excluded_repair_smoke_probe is None
    assert repair_slot.byzantine.duration_s == 300
    assert repair_slot.common_timers.hard_timeout_s == 650
    assert repair_runtime.fault_window.duration_s == 300
    assert repair_runtime.fault_window.hard_timeout_s == 650
    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    assert probe.source_campaign_result_path == (
        "results/shape-placement-factorial-v30/slot-037-n31-f2-b04-00"
    )
    assert coverage.runtime.excluded_repair_smoke_probe == probe


def test_v29_and_v30_exact_static_bindings_pass_but_cross_binding_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v29_manifest = manifest_module.load_frozen_manifest(V29_MANIFEST)
    v29_plan = manifest_module.build_factorial_plan(v29_manifest)
    v29_coverage = _coverage(v29_plan)
    _, v30_plan, v30_coverage = _candidate_coverage(monkeypatch)
    v29_artifacts = _static_artifacts(V29_MANIFEST, v29_plan, v29_coverage)
    v30_artifacts = _static_artifacts(V30_MANIFEST, v30_plan, v30_coverage)

    execution._bind_static_artifacts(
        v29_coverage.slots[1],
        v29_coverage.runtimes[1],
        v29_artifacts,
        campaign_member=False,
    )
    execution._bind_static_artifacts(
        v30_coverage.slots[1],
        v30_coverage.runtimes[1],
        v30_artifacts,
        campaign_member=False,
    )
    with pytest.raises(execution.FactorialExecutionError):
        execution._bind_static_artifacts(
            v30_coverage.slots[1],
            v30_coverage.runtimes[1],
            v29_artifacts,
            campaign_member=False,
        )
    with pytest.raises(execution.FactorialExecutionError):
        execution._bind_static_artifacts(
            v29_coverage.slots[1],
            v29_coverage.runtimes[1],
            v30_artifacts,
            campaign_member=False,
        )


def test_v29_runtime_cannot_bind_to_v30_repair_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v29_manifest = manifest_module.load_frozen_manifest(V29_MANIFEST)
    v29_plan = manifest_module.build_factorial_plan(v29_manifest)
    v29_coverage = _coverage(v29_plan)
    _, v30_plan, v30_coverage = _candidate_coverage(monkeypatch)

    with pytest.raises(
        execution.FactorialExecutionError,
        match=(
            "slot/runtime derivation failed: excluded repair smoke probe source, "
            "delta, contract, mode, or timing drifted"
        ),
    ):
        execution._bind_static_artifacts(
            v30_coverage.slots[1],
            v29_coverage.runtimes[1],
            _static_artifacts(V30_MANIFEST, v30_plan, v30_coverage),
            campaign_member=False,
        )


def test_v33_is_default_and_v30_is_validation_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v40.json"
    assert cli.main(["--manifest", str(V30_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v39 are validation-only" in refusal["reason"]
