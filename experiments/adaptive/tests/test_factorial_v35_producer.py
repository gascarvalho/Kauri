"""Prospective producer contract for the validation-only v35 version roll."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive import run_shape_factorial_campaign as cli
from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment import factorial_manifest as manifest_module
from experiments.adaptive.kauri_experiment import factorial_runtime as runtime_module

REPOSITORY = Path(__file__).resolve().parents[3]
V35_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v35.json"
)
V34_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v34.json"
)
V33_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v33.json"
)
OBSERVATION_CONTRACT_V1 = (
    "exact_excluded_repair_smoke_epoch1_selection_terminal_all_replica_command_"
    "activation_and_stable_end_before_fault_end_then_fault_end_before_cycle1_"
    "selection_then_epoch2_terminal_all_replica_command_activation_stable_end_"
    "and_drain_before_shared_hard_deadline_v1"
)
OBSERVATION_CONTRACT_V2 = (
    "exact_excluded_repair_smoke_fault_evidence_and_epoch1_stable_are_the_only_"
    "fault_active_causal_phases_with_epoch1_selection_terminal_all_replica_"
    "command_activation_and_stable_end_before_fault_end_then_epoch2_is_post_"
    "fault_recovery_and_stability_with_fault_end_before_cycle1_selection_then_"
    "epoch2_terminal_all_replica_command_activation_stable_end_and_drain_before_"
    "shared_hard_deadline_v2"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V35_MANIFEST.read_bytes()
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


def _coverage_binding(version: str) -> execution.CoverageSmokeLaunchBinding:
    contract = {
        "manifest_id": f"shape-placement-factorial-{version}",
        "coverage_smoke_id": (
            f"shape-placement-factorial-{version}-"
            "excluded-n31-coverage-smoke-v1"
        ),
        "expected_slot_count": 2,
        "execution_schedule": [
            {
                "coverage_execution_ordinal": 1,
                "source_campaign_execution_ordinal": 1,
                "slot_id": "slot-066-n31-f5-b05-P",
                "block_id": "n31-f5-b05",
                "arm_code": "P",
            },
            {
                "coverage_execution_ordinal": 2,
                "source_campaign_execution_ordinal": 5,
                "slot_id": "slot-037-n31-f2-b04-00",
                "block_id": "n31-f2-b04",
                "arm_code": "00",
            },
        ],
    }
    return execution.CoverageSmokeLaunchBinding(
        contract_payload=_canonical(contract),
        ledger_prefix_payload=b"",
        predecessor_receipt_payload=None,
    )


def test_v35_profile_is_one_lf_and_exact_two_value_delta() -> None:
    v35_payload = V35_MANIFEST.read_bytes()
    v34_payload = V34_MANIFEST.read_bytes()

    assert v35_payload.endswith(b"\n")
    assert not v35_payload.endswith(b"\n\n")
    assert v35_payload.count(b"shape-placement-factorial-v35") == 2
    normalized_v35 = v35_payload.replace(
        b"shape-placement-factorial-v35",
        b"shape-placement-factorial-v34",
    )
    assert normalized_v35 == v34_payload
    v35 = json.loads(v35_payload)
    v34 = json.loads(v34_payload)
    assert v35.pop("manifest_id") == "shape-placement-factorial-v35"
    assert v34.pop("manifest_id") == "shape-placement-factorial-v34"
    assert v35["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v35"
    )
    assert v34["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v34"
    )
    assert v35["timers"]["transition_convergence_deadline_s"] == 30  # type: ignore[index]
    assert v34["timers"]["transition_convergence_deadline_s"] == 30  # type: ignore[index]
    v35_responsive = v35["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v34_responsive = v34["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert v35_responsive.pop("excluded_repair_smoke_observation_contract") == (
        OBSERVATION_CONTRACT_V2
    )
    assert v34_responsive.pop("excluded_repair_smoke_observation_contract") == (
        OBSERVATION_CONTRACT_V2
    )
    assert v35 == v34


def test_v34_identities_are_explicit_historical_aliases() -> None:
    assert (
        manifest_module.V34_MANIFEST_ID,
        manifest_module.V34_MANIFEST_SHA256,
        manifest_module.V34_SEMANTIC_SHA256,
        manifest_module.V34_PLAN_SHA256,
        runtime_module.V34_RUNTIME_SHA256,
        runtime_module.V34_SMOKE_RUNTIME_SHA256,
        runtime_module.V34_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "shape-placement-factorial-v34",
        "1b0c22f5f517487c507c7699198f597093dbc879994925ddbb8220c67decedb0",
        "e963372c05c51367c6a2d54ff2f1065b1bfb136b4e0857f2dbb69793078fc0a4",
        "6e48cd511a0788ea1e2e5b42fb9b09e47a4c269d40fe18779cb559990b536e92",
        "73dc1bd18235fe2ef9a565b2486cd48ba49fe10b3e89adc7666d5baee6ba985b",
        "b004afbe823f75bc96521dcc7202cfdbb91930eea6286730f17e316308c0107f",
        "f79fa73e1af6c2be6b2284d9d9a0b56224dcca94c153f7e91e2220ae49266d5e",
    )


def test_v35_six_identities_are_preserved_as_historical_aliases() -> None:
    assert manifest_module.V35_MANIFEST_ID == "shape-placement-factorial-v35"
    assert (
        manifest_module.V35_MANIFEST_SHA256,
        manifest_module.V35_SEMANTIC_SHA256,
        manifest_module.V35_PLAN_SHA256,
        runtime_module.V35_RUNTIME_SHA256,
        runtime_module.V35_SMOKE_RUNTIME_SHA256,
        runtime_module.V35_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "28303487578594d7eac64aa1eda11ea891a85b8a6d6ff386d42e920dc8b82b95",
        "40422f2b8ac74e695fdfa18b687fce2b31b516f36bd2b6bfe51f53602bd4d0c8",
        "6c558653d0db5e5f646de58c1a853d656fedbd7e61257e13755ef01ea9d42025",
        "05ffcdc81d0cd0ec8a264cd0d5545e88fbf14dba1569629e6ec0f02c3a16615c",
        "785abe70a6500a66c331e00dd81eeada1065457ff4dace4e6f9035b9006aaee6",
        "4fa44f57128bc096c7fd40bbf9c054c184a273d07ca8f2bdc43fdda7c5e93ad2",
    )


def test_v34_exact_history_remains_loadable() -> None:
    manifest = manifest_module.load_frozen_manifest(V34_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    coverage = _coverage(plan)

    assert manifest.manifest_id == manifest_module.V34_MANIFEST_ID
    assert manifest.manifest_sha256 == manifest_module.V34_MANIFEST_SHA256
    assert plan.plan_sha256 == manifest_module.V34_PLAN_SHA256
    assert hashlib.sha256(
        runtime_module.canonical_runtime_bytes(runtime)
    ).hexdigest() == (runtime_module.V34_RUNTIME_SHA256)
    assert (
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest()
        == runtime_module.V34_COVERAGE_SMOKE_RUNTIME_SHA256
    )
    assert (
        manifest.byzantine.responsive_degradation.excluded_repair_smoke_observation_contract
        == OBSERVATION_CONTRACT_V2
    )


@pytest.mark.parametrize("manifest_path", (V33_MANIFEST, V34_MANIFEST))
def test_v33_and_v34_historical_runtime_preflight_remains_loadable(
    manifest_path: Path,
) -> None:
    manifest = manifest_module.load_frozen_manifest(manifest_path)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)

    result = runtime_module.runtime_preflight(
        runtime,
        available_free_bytes=runtime.minimum_free_bytes,
    )

    assert result["status"] == "PASS"


def test_v35_preserves_timing_and_threads_v2_observation_contract(
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
        slot.byzantine.responsive_degradation.excluded_repair_smoke_observation_contract
        == OBSERVATION_CONTRACT_V2
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
    assert (
        repair_runtime.causal_acceptance.excluded_repair_smoke_observation_contract
        == OBSERVATION_CONTRACT_V2
    )
    for runtime in (primary_runtime, repair_runtime):
        argv = runtime.manager_argv_template.argv
        deadline_index = argv.index("--convergence-deadline-seconds")
        assert argv[deadline_index + 1] == "30"
    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    assert probe.observation_contract == OBSERVATION_CONTRACT_V2
    assert probe.source_campaign_result_path == (
        "results/shape-placement-factorial-v35/slot-037-n31-f2-b04-00"
    )
    assert coverage.runtime.excluded_repair_smoke_probe == probe


def test_v34_and_v35_exact_static_bindings_pass_but_cross_binding_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v34_manifest = manifest_module.load_frozen_manifest(V34_MANIFEST)
    v34_plan = manifest_module.build_factorial_plan(v34_manifest)
    v34_coverage = _coverage(v34_plan)
    _, v35_plan, v35_coverage = _candidate_coverage(monkeypatch)
    v34_artifacts = _static_artifacts(V34_MANIFEST, v34_plan, v34_coverage)
    v35_artifacts = _static_artifacts(V35_MANIFEST, v35_plan, v35_coverage)

    execution._bind_static_artifacts(
        v34_coverage.slots[1],
        v34_coverage.runtimes[1],
        v34_artifacts,
        campaign_member=False,
    )
    execution._bind_static_artifacts(
        v35_coverage.slots[1],
        v35_coverage.runtimes[1],
        v35_artifacts,
        campaign_member=False,
    )
    with pytest.raises(execution.FactorialExecutionError):
        execution._bind_static_artifacts(
            v35_coverage.slots[1],
            v35_coverage.runtimes[1],
            v34_artifacts,
            campaign_member=False,
        )
    with pytest.raises(execution.FactorialExecutionError):
        execution._bind_static_artifacts(
            v34_coverage.slots[1],
            v34_coverage.runtimes[1],
            v35_artifacts,
            campaign_member=False,
        )


def test_v34_and_v35_repair_runtimes_reject_bidirectional_cross_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v34_manifest = manifest_module.load_frozen_manifest(V34_MANIFEST)
    v34_plan = manifest_module.build_factorial_plan(v34_manifest)
    v34_coverage = _coverage(v34_plan)
    _, v35_plan, v35_coverage = _candidate_coverage(monkeypatch)

    with pytest.raises(
        execution.FactorialExecutionError,
        match=(
            "slot/runtime derivation failed: excluded repair smoke probe source, "
            "delta, contract, mode, or timing drifted"
        ),
    ):
        execution._bind_static_artifacts(
            v35_coverage.slots[1],
            v34_coverage.runtimes[1],
            _static_artifacts(V35_MANIFEST, v35_plan, v35_coverage),
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
            v34_coverage.slots[1],
            v35_coverage.runtimes[1],
            _static_artifacts(V34_MANIFEST, v34_plan, v34_coverage),
            campaign_member=False,
        )


def test_v35_repair_runtime_and_execution_reject_self_consistent_v1_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coverage = _candidate_coverage(monkeypatch)
    repair_slot = coverage.slots[1]
    repair_runtime = coverage.runtimes[1]
    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    v1_probe = replace(probe, observation_contract=OBSERVATION_CONTRACT_V1)

    with pytest.raises(
        manifest_module.FactorialManifestError,
        match="excluded repair smoke probe source, delta, contract, mode, or timing drifted",
    ):
        runtime_module._validate_excluded_repair_smoke_probe(
            repair_slot,
            v1_probe,
        )

    v1_runtime = replace(
        repair_runtime,
        causal_acceptance=replace(
            repair_runtime.causal_acceptance,
            excluded_repair_smoke_observation_contract=OBSERVATION_CONTRACT_V1,
        ),
        excluded_repair_smoke_probe=v1_probe,
    )
    with pytest.raises(execution.FactorialExecutionError, match="binding drifted"):
        execution._uses_exact_excluded_repair_smoke_bound(
            v1_runtime,
            _coverage_binding("v35"),
        )


def test_v36_is_default_and_v35_is_validation_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v45.json"
    assert cli.main(["--manifest", str(V35_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v44 are validation-only" in refusal["reason"]
