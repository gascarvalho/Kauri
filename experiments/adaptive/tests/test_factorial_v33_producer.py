"""Prospective producer contract for the validation-only v33 version roll."""

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
V33_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v33.json"
)
V32_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v32.json"
)
V34_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v34.json"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V33_MANIFEST.read_bytes()
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


def test_v33_profile_is_one_lf_and_delta_is_only_identity_root_and_deadline() -> None:
    v33_payload = V33_MANIFEST.read_bytes()
    v32_payload = V32_MANIFEST.read_bytes()

    assert v33_payload.endswith(b"\n")
    assert not v33_payload.endswith(b"\n\n")
    assert v33_payload.count(b"shape-placement-factorial-v33") == 2
    normalized_v33 = v33_payload.replace(
        b"shape-placement-factorial-v33",
        b"shape-placement-factorial-v32",
    ).replace(
        b'"transition_convergence_deadline_s": 30',
        b'"transition_convergence_deadline_s": 20',
    )
    assert normalized_v33 == v32_payload
    v33 = json.loads(v33_payload)
    v32 = json.loads(v32_payload)
    assert v33.pop("manifest_id") == "shape-placement-factorial-v33"
    assert v32.pop("manifest_id") == "shape-placement-factorial-v32"
    assert v33["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v33"
    )
    assert v32["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v32"
    )
    assert v33["timers"].pop("transition_convergence_deadline_s") == 30  # type: ignore[index]
    assert v32["timers"].pop("transition_convergence_deadline_s") == 20  # type: ignore[index]
    assert v33 == v32


def test_v32_identities_are_explicit_historical_aliases() -> None:
    assert (
        manifest_module.V32_MANIFEST_ID,
        manifest_module.V32_MANIFEST_SHA256,
        manifest_module.V32_SEMANTIC_SHA256,
        manifest_module.V32_PLAN_SHA256,
        runtime_module.V32_RUNTIME_SHA256,
        runtime_module.V32_SMOKE_RUNTIME_SHA256,
        runtime_module.V32_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "shape-placement-factorial-v32",
        "cbc03d8c58b8192b70b5c0f8c0a504dc07076f8e78895a3a2c802b98658d691d",
        "eb44b9c5c229db10fc967b7d3834029f8780c48f6ae4740b213a692c9af8cb42",
        "3325d3d1b0b1bf2569686db9d28e3cc8d6cd6e9b6d6fc4ddf97e48b56c1bc2fa",
        "5771dcfa48a4d6550231221b4a7dd409af190b6389797511c1e84419e1b4b395",
        "5a27efa53d8324068c67ead555a304c77d7727d82b69bb1d84f4cc80b6d46e74",
        "45da6535ee0bf9035b9f61375e03afd7737b6f0d8920431cb5f0248a58a06f86",
    )


def test_v33_six_identities_are_frozen_after_semantic_ack() -> None:
    assert manifest_module.V33_MANIFEST_ID == "shape-placement-factorial-v33"
    assert (
        manifest_module.V33_MANIFEST_SHA256,
        manifest_module.V33_SEMANTIC_SHA256,
        manifest_module.V33_PLAN_SHA256,
        runtime_module.V33_RUNTIME_SHA256,
        runtime_module.V33_SMOKE_RUNTIME_SHA256,
        runtime_module.V33_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "aa109a401ef88f7d135ff7f590ae1f372329ed5165c223ce0e3bf44536cf61fc",
        "8faee7b8cf1148a7acec48b75f02ca3f5b864ee6981555ae78624664e246509f",
        "0897fc233ae6fc25898ee576066403f122ad65416322dec2d4e99e03503fd81c",
        "597bddecd5ada141dfaf1830cabb645fa5f50aaf20abd198c134a48e2d1e4e2b",
        "c0b96dfaaf73a374113a0c6ba98f87c06a934eb6b40202d96a51b6182c714c05",
        "18d9dd6841b3dc69a3797d9473aa0f14df2bc793cf3354abd67be91000e7007e",
    )


def test_v32_exact_history_remains_loadable() -> None:
    manifest = manifest_module.load_frozen_manifest(V32_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    coverage = _coverage(plan)

    assert manifest.manifest_id == manifest_module.V32_MANIFEST_ID
    assert manifest.manifest_sha256 == manifest_module.V32_MANIFEST_SHA256
    assert plan.plan_sha256 == manifest_module.V32_PLAN_SHA256
    assert hashlib.sha256(
        runtime_module.canonical_runtime_bytes(runtime)
    ).hexdigest() == (runtime_module.V32_RUNTIME_SHA256)
    assert (
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest()
        == runtime_module.V32_COVERAGE_SMOKE_RUNTIME_SHA256
    )


def test_v33_changes_only_convergence_deadline_in_derived_timing_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, plan, coverage = _candidate_coverage(monkeypatch)
    campaign_runtime = runtime_module.build_factorial_runtime(plan)
    primary_slot, repair_slot = coverage.slots
    primary_runtime, repair_runtime = coverage.runtimes

    assert manifest.automatic_retries == 0
    assert plan.automatic_retries == 0
    assert coverage.runtime.automatic_retries == 0
    assert coverage.runtime.replacement_policy == "none"
    assert coverage.runtime.stop_on_first_non_pass is True
    assert all(slot.byzantine.duration_s == 450 for slot in plan.slots)
    assert all(slot.common_timers.hard_timeout_s == 650 for slot in plan.slots)
    assert all(
        slot.common_timers.leader_progress_timeout_ms == 20_000
        for slot in plan.slots
    )
    assert all(
        slot.common_timers.transition_convergence_deadline_s == 30
        for slot in plan.slots
    )
    assert all(
        slot.fault_window.transition_convergence_deadline_s == 30
        for slot in campaign_runtime.slots
    )
    assert all(
        "leader-progress-timeout = 20" in slot.main_config.lines
        for slot in campaign_runtime.slots
    )
    assert primary_slot.byzantine.duration_s == 450
    assert primary_slot.common_timers.hard_timeout_s == 650
    assert primary_slot.common_timers.transition_convergence_deadline_s == 30
    assert primary_slot.q == 21
    assert primary_runtime.fault_window.duration_s == 450
    assert primary_runtime.fault_window.hard_timeout_s == 650
    assert primary_runtime.fault_window.transition_convergence_deadline_s == 30
    assert primary_runtime.excluded_repair_smoke_probe is None
    assert repair_slot.byzantine.duration_s == 300
    assert repair_slot.common_timers.hard_timeout_s == 650
    assert repair_slot.common_timers.transition_convergence_deadline_s == 30
    assert repair_slot.q == 21
    assert repair_runtime.fault_window.duration_s == 300
    assert repair_runtime.fault_window.hard_timeout_s == 650
    assert repair_runtime.fault_window.transition_convergence_deadline_s == 30
    for runtime in (primary_runtime, repair_runtime):
        argv = runtime.manager_argv_template.argv
        deadline_index = argv.index("--convergence-deadline-seconds")
        assert argv[deadline_index + 1] == "30"
    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    assert probe.source_campaign_result_path == (
        "results/shape-placement-factorial-v33/slot-037-n31-f2-b04-00"
    )
    assert coverage.runtime.excluded_repair_smoke_probe == probe


def test_v32_and_v33_exact_static_bindings_pass_but_cross_binding_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v32_manifest = manifest_module.load_frozen_manifest(V32_MANIFEST)
    v32_plan = manifest_module.build_factorial_plan(v32_manifest)
    v32_coverage = _coverage(v32_plan)
    _, v33_plan, v33_coverage = _candidate_coverage(monkeypatch)
    v32_artifacts = _static_artifacts(V32_MANIFEST, v32_plan, v32_coverage)
    v33_artifacts = _static_artifacts(V33_MANIFEST, v33_plan, v33_coverage)

    execution._bind_static_artifacts(
        v32_coverage.slots[1],
        v32_coverage.runtimes[1],
        v32_artifacts,
        campaign_member=False,
    )
    execution._bind_static_artifacts(
        v33_coverage.slots[1],
        v33_coverage.runtimes[1],
        v33_artifacts,
        campaign_member=False,
    )
    with pytest.raises(execution.FactorialExecutionError):
        execution._bind_static_artifacts(
            v33_coverage.slots[1],
            v33_coverage.runtimes[1],
            v32_artifacts,
            campaign_member=False,
        )
    with pytest.raises(execution.FactorialExecutionError):
        execution._bind_static_artifacts(
            v32_coverage.slots[1],
            v32_coverage.runtimes[1],
            v33_artifacts,
            campaign_member=False,
        )


def test_v32_and_v33_repair_runtimes_reject_bidirectional_cross_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v32_manifest = manifest_module.load_frozen_manifest(V32_MANIFEST)
    v32_plan = manifest_module.build_factorial_plan(v32_manifest)
    v32_coverage = _coverage(v32_plan)
    _, v33_plan, v33_coverage = _candidate_coverage(monkeypatch)

    with pytest.raises(
        execution.FactorialExecutionError,
        match=(
            "slot/runtime derivation failed: excluded repair smoke probe source, "
            "delta, contract, mode, or timing drifted"
        ),
    ):
        execution._bind_static_artifacts(
            v33_coverage.slots[1],
            v32_coverage.runtimes[1],
            _static_artifacts(V33_MANIFEST, v33_plan, v33_coverage),
            campaign_member=False,
        )
    with pytest.raises(
        execution.FactorialExecutionError,
        match=(
            "slot/runtime derivation failed: excluded repair smoke probe source, "
            "delta, contract, mode, or timing drifted"
        ),
    ):
        execution._bind_static_artifacts(
            v32_coverage.slots[1],
            v33_coverage.runtimes[1],
            _static_artifacts(V32_MANIFEST, v32_plan, v32_coverage),
            campaign_member=False,
        )


def test_v34_is_default_and_v33_is_validation_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST == V34_MANIFEST
    assert cli.main(["--manifest", str(V33_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v33 are validation-only" in refusal["reason"]
