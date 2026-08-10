"""Prospective producer contract for the validation-only v31 version roll."""

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
V31_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v31.json"
)
V30_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v30.json"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V31_MANIFEST.read_bytes()
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
        "runtime.json": execution._canonical_json_bytes(coverage.runtime.as_document()),
    }


def test_v31_profile_is_one_lf_and_delta_is_only_identity_and_results_root() -> None:
    v31_payload = V31_MANIFEST.read_bytes()
    v30_payload = V30_MANIFEST.read_bytes()

    assert v31_payload.endswith(b"\n")
    assert not v31_payload.endswith(b"\n\n")
    assert v31_payload.count(b"shape-placement-factorial-v31") == 2
    assert (
        v31_payload.replace(
            b"shape-placement-factorial-v31",
            b"shape-placement-factorial-v30",
        )
        == v30_payload
    )
    v31 = json.loads(v31_payload)
    v30 = json.loads(v30_payload)
    assert v31.pop("manifest_id") == "shape-placement-factorial-v31"
    assert v30.pop("manifest_id") == "shape-placement-factorial-v30"
    assert v31["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v31"
    )
    assert v30["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v30"
    )
    assert v31 == v30


def test_v30_identities_are_explicit_historical_aliases() -> None:
    assert (
        manifest_module.V30_MANIFEST_ID,
        manifest_module.V30_MANIFEST_SHA256,
        manifest_module.V30_SEMANTIC_SHA256,
        manifest_module.V30_PLAN_SHA256,
        runtime_module.V30_RUNTIME_SHA256,
        runtime_module.V30_SMOKE_RUNTIME_SHA256,
        runtime_module.V30_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "shape-placement-factorial-v30",
        "c868d9ebce0f3afbdfd6011787f385395b6df43b633f6cfad284726c2d3f52e4",
        "4d3130b1a02c9a0f4c67f8a29fb22000e15c2d9bf64ebd87af32518f22fb15b2",
        "3ed7a8a9b80f18c4b48d2a3daf16c44fba458f47ae84c6e4a4bdf907b4d65e1f",
        "a0dfe6f503b0e013e757707697221c63711074d39490e7075887754987a22a44",
        "6e8540045d60601810672d8f84adc8b4b7800bf5989e386ae39c3f6fa0d22beb",
        "14429819aefef1a4e664f3fd32837cf659a5a10203929526e1137bc392389862",
    )


def test_v31_six_identities_are_exact_historical_aliases() -> None:
    assert manifest_module.V31_MANIFEST_ID == "shape-placement-factorial-v31"
    assert (
        manifest_module.V31_MANIFEST_SHA256,
        manifest_module.V31_SEMANTIC_SHA256,
        manifest_module.V31_PLAN_SHA256,
        runtime_module.V31_RUNTIME_SHA256,
        runtime_module.V31_SMOKE_RUNTIME_SHA256,
        runtime_module.V31_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "fce9417a0f50b5a741069730d479dcb58d9502c86d3bc53a51bb06def07592c7",
        "d09a082b895e88360b236b49998d8671440189dedbacbe1bfdd9d2ae5090a95c",
        "f589888a4456910f3188ee602565e990ea7c4fb45dedd798d8804b679b1591fa",
        "935e3f2418b3d24e4e535fcffb4ebc096eafe4ccc93f5468331e4c8d9552621c",
        "d7026d8577928eb4660dd54240f7ce014a77f46d037b67801a7c0c6764706203",
        "2a01a9cfad5b5ca7df9dfe3ce9b58bf894f0a1e7da523423ba8b98b5177fafe1",
    )


def test_v30_exact_history_remains_loadable() -> None:
    manifest = manifest_module.load_frozen_manifest(V30_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    coverage = _coverage(plan)

    assert manifest.manifest_id == manifest_module.V30_MANIFEST_ID
    assert manifest.manifest_sha256 == manifest_module.V30_MANIFEST_SHA256
    assert plan.plan_sha256 == manifest_module.V30_PLAN_SHA256
    assert hashlib.sha256(
        runtime_module.canonical_runtime_bytes(runtime)
    ).hexdigest() == (runtime_module.V30_RUNTIME_SHA256)
    assert (
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest()
        == runtime_module.V30_COVERAGE_SMOKE_RUNTIME_SHA256
    )


def test_v31_preserves_v30_timing_and_exact_repair_contract(
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
        "results/shape-placement-factorial-v31/slot-037-n31-f2-b04-00"
    )
    assert coverage.runtime.excluded_repair_smoke_probe == probe


def test_v30_and_v31_exact_static_bindings_pass_but_cross_binding_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v30_manifest = manifest_module.load_frozen_manifest(V30_MANIFEST)
    v30_plan = manifest_module.build_factorial_plan(v30_manifest)
    v30_coverage = _coverage(v30_plan)
    _, v31_plan, v31_coverage = _candidate_coverage(monkeypatch)
    v30_artifacts = _static_artifacts(V30_MANIFEST, v30_plan, v30_coverage)
    v31_artifacts = _static_artifacts(V31_MANIFEST, v31_plan, v31_coverage)

    execution._bind_static_artifacts(
        v30_coverage.slots[1],
        v30_coverage.runtimes[1],
        v30_artifacts,
        campaign_member=False,
    )
    execution._bind_static_artifacts(
        v31_coverage.slots[1],
        v31_coverage.runtimes[1],
        v31_artifacts,
        campaign_member=False,
    )
    with pytest.raises(execution.FactorialExecutionError):
        execution._bind_static_artifacts(
            v31_coverage.slots[1],
            v31_coverage.runtimes[1],
            v30_artifacts,
            campaign_member=False,
        )
    with pytest.raises(execution.FactorialExecutionError):
        execution._bind_static_artifacts(
            v30_coverage.slots[1],
            v30_coverage.runtimes[1],
            v31_artifacts,
            campaign_member=False,
        )


def test_v30_runtime_cannot_bind_to_v31_repair_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v30_manifest = manifest_module.load_frozen_manifest(V30_MANIFEST)
    v30_plan = manifest_module.build_factorial_plan(v30_manifest)
    v30_coverage = _coverage(v30_plan)
    _, v31_plan, v31_coverage = _candidate_coverage(monkeypatch)

    with pytest.raises(
        execution.FactorialExecutionError,
        match=(
            "slot/runtime derivation failed: excluded repair smoke probe source, "
            "delta, contract, mode, or timing drifted"
        ),
    ):
        execution._bind_static_artifacts(
            v31_coverage.slots[1],
            v30_coverage.runtimes[1],
            _static_artifacts(V31_MANIFEST, v31_plan, v31_coverage),
            campaign_member=False,
        )


def test_v31_is_validation_only_under_v33_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v34.json"
    assert cli.main(["--manifest", str(V31_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v33 are validation-only" in refusal["reason"]
