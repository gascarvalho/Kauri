"""Prospective producer contract for the v28 excluded-repair probe."""

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
V28_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v28.json"
)
V27_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v27.json"
)
OBSERVATION_FIELD = "excluded_repair_smoke_observation_contract"
OBSERVATION_CONTRACT = (
    "exact_excluded_repair_smoke_epoch1_selection_terminal_all_replica_command_"
    "activation_and_stable_end_before_fault_end_then_fault_end_before_cycle1_"
    "selection_then_epoch2_terminal_all_replica_command_activation_stable_end_"
    "and_drain_before_shared_hard_deadline_v1"
)
PROBE_FIELD = "excluded_repair_smoke_verified_response_duplicate_probe_contract"
PROBE_CONTRACT = (
    "exact_excluded_repair_smoke_at_least_one_post_fault_epoch1_internal_"
    "responsive_child_response_is_replayed_only_into_response_evidence_bridge_"
    "after_consensus_acceptance_and_first_evidence_record_with_at_most_one_probe_"
    "per_reporter_v1"
)
PROBE_MODE = "exact_once_post_fault_epoch1_responsive_internal_child_v1"
PROBE_OPTION = "--experiment-response-evidence-duplicate-probe"


def _encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _parse_candidate(
    monkeypatch: pytest.MonkeyPatch,
    document: dict[str, object] | None = None,
):
    source = json.loads(V28_MANIFEST.read_bytes()) if document is None else document
    semantic = _encoded(source)
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_SEMANTIC_SHA256",
        hashlib.sha256(semantic).hexdigest(),
    )
    return manifest_module.parse_manifest_bytes(semantic)


def _candidate_plan(monkeypatch: pytest.MonkeyPatch):
    manifest = _parse_candidate(monkeypatch)
    plan = manifest_module.build_factorial_plan(manifest)
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_MANIFEST_SHA256",
        manifest.manifest_sha256,
    )
    monkeypatch.setattr(manifest_module, "FROZEN_PLAN_SHA256", plan.plan_sha256)
    return manifest, plan


def test_v28_profile_delta_is_only_identity_root_and_two_contracts() -> None:
    v28 = json.loads(V28_MANIFEST.read_bytes())
    v27 = json.loads(V27_MANIFEST.read_bytes())

    assert v28.pop("manifest_id") == "shape-placement-factorial-v28"
    assert v27.pop("manifest_id") == "shape-placement-factorial-v27"
    assert v28["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v28"
    )
    assert v27["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v27"
    )
    responsive_v28 = v28["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert responsive_v28.pop(OBSERVATION_FIELD) == OBSERVATION_CONTRACT
    assert responsive_v28.pop(PROBE_FIELD) == PROBE_CONTRACT
    assert v28 == v27


def test_v28_probe_contract_freezes_global_minimum_and_reporter_maximum() -> None:
    assert "at_least_one_post_fault" in PROBE_CONTRACT
    assert "with_at_most_one_probe_per_reporter" in PROBE_CONTRACT
    assert "once_per_reporter" not in PROBE_CONTRACT


@pytest.mark.parametrize("field", [OBSERVATION_FIELD, PROBE_FIELD])
def test_v28_requires_each_exact_repair_contract(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    document = json.loads(V28_MANIFEST.read_bytes())
    document["byzantine"]["responsive_degradation"].pop(field)
    with pytest.raises(manifest_module.FactorialManifestError, match="repair"):
        _parse_candidate(monkeypatch, document)

    document = json.loads(V28_MANIFEST.read_bytes())
    document["byzantine"]["responsive_degradation"][field] += "-drift"
    with pytest.raises(manifest_module.FactorialManifestError, match="repair"):
        _parse_candidate(monkeypatch, document)


def test_v28_campaign_preserves_v27_schedule_timing_and_has_no_probe_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, plan = _candidate_plan(monkeypatch)
    previous = manifest_module.build_factorial_plan(
        manifest_module.load_frozen_manifest(V27_MANIFEST)
    )

    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    assert getattr(responsive, OBSERVATION_FIELD) == OBSERVATION_CONTRACT
    assert getattr(responsive, PROBE_FIELD) == PROBE_CONTRACT
    assert manifest.byzantine.duration_s == 450
    assert manifest.common_timers.hard_timeout_s == 650
    assert plan.execution_schedule == previous.execution_schedule
    for slot, old_slot in zip(plan.slots, previous.slots, strict=True):
        assert slot.execution_ordinal == old_slot.execution_ordinal
        assert slot.block_id == old_slot.block_id
        assert slot.arm_code == old_slot.arm_code
        spec = runtime_module.build_slot_runtime(slot)
        assert spec.fault_window.duration_s == 450
        assert spec.fault_window.hard_timeout_s == 650
        assert getattr(spec.causal_acceptance, OBSERVATION_FIELD) == (
            OBSERVATION_CONTRACT
        )
        assert getattr(spec.causal_acceptance, PROBE_FIELD) == PROBE_CONTRACT
        assert spec.excluded_repair_smoke_probe is None
        assert PROBE_OPTION not in spec.manager_argv_template.argv
        assert all(
            PROBE_OPTION not in process.argv for process in spec.replica_argv_templates
        )


def test_v28_exact_slot037_repair_is_the_only_300_second_probe_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    source_repair_runtime = runtime_module.build_slot_runtime(repair)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])

    assert isinstance(coverage.runtime, execution.N31CoverageSmokeRuntime)
    primary_smoke, repair_smoke = coverage.slots
    primary_runtime, repair_runtime = coverage.runtimes
    assert primary_smoke.byzantine.duration_s == 450
    assert primary_runtime.fault_window.duration_s == 450
    assert primary_runtime.excluded_repair_smoke_probe is None
    assert n7.runtime.excluded_repair_smoke_probe is None
    assert all(
        PROBE_OPTION not in process.argv
        for process in n7.runtime.replica_argv_templates
    )
    assert repair.byzantine.duration_s == 450
    assert repair_smoke.byzantine.duration_s == 300
    assert repair_smoke.common_timers.hard_timeout_s == 650
    assert repair_runtime.fault_window.duration_s == 300
    assert repair_runtime.fault_window.hard_timeout_s == 650

    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    assert probe.source_campaign_slot_id == repair.slot_id
    assert probe.source_campaign_result_path == repair.result_path
    assert probe.source_campaign_artifact_id == source_repair_runtime.artifact_id
    assert probe.source_fault_window_duration_s == 450
    assert probe.effective_fault_window_duration_s == 300
    assert probe.hard_timeout_s == 650
    assert probe.semantic_delta == "byzantine.window.duration_s:450->300"
    assert probe.observation_contract == OBSERVATION_CONTRACT
    assert probe.verified_response_duplicate_probe_contract == PROBE_CONTRACT
    assert probe.verified_response_duplicate_probe_mode == PROBE_MODE
    assert coverage.runtime.excluded_repair_smoke_probe == probe

    for process in repair_runtime.replica_argv_templates:
        assert process.argv.count(PROBE_OPTION) == 1
        index = process.argv.index(PROBE_OPTION)
        assert process.argv[index + 1] == PROBE_MODE
        assert process.argv.count(PROBE_MODE) == 1
    assert PROBE_OPTION not in repair_runtime.manager_argv_template.argv
    assert all(
        PROBE_OPTION not in process.argv
        for process in primary_runtime.replica_argv_templates
    )


def test_v28_repair_probe_and_semantic_delta_are_artifact_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    candidate = coverage.runtimes[1]
    probe = candidate.excluded_repair_smoke_probe
    assert probe is not None

    for drift in (
        replace(probe, source_fault_window_duration_s=449),
        replace(probe, effective_fault_window_duration_s=301),
        replace(probe, hard_timeout_s=649),
        replace(probe, semantic_delta="duration-drift"),
        replace(probe, observation_contract=OBSERVATION_CONTRACT + "-drift"),
        replace(
            probe,
            verified_response_duplicate_probe_contract=PROBE_CONTRACT + "-drift",
        ),
        replace(probe, verified_response_duplicate_probe_mode=PROBE_MODE + "-drift"),
    ):
        with pytest.raises(manifest_module.FactorialManifestError, match="repair"):
            runtime_module.build_slot_runtime(
                coverage.slots[1],
                excluded_repair_smoke_probe=drift,
            )


def test_v27_history_has_no_probe_and_preserves_strict_450_runtime() -> None:
    manifest = manifest_module.load_frozen_manifest(V27_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    assert isinstance(coverage.runtime, execution.N31CoverageSmokeRuntime)
    assert coverage.runtime.excluded_repair_smoke_probe is None
    assert all(runtime.fault_window.duration_s == 450 for runtime in coverage.runtimes)
    assert all(
        PROBE_OPTION not in process.argv
        for runtime in coverage.runtimes
        for process in runtime.replica_argv_templates
    )


def test_v28_six_identities_are_exact_frozen_values() -> None:
    assert (
        manifest_module.V28_MANIFEST_SHA256,
        manifest_module.V28_SEMANTIC_SHA256,
        manifest_module.V28_PLAN_SHA256,
        runtime_module.V28_RUNTIME_SHA256,
        runtime_module.V28_SMOKE_RUNTIME_SHA256,
        runtime_module.V28_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "fda2a5e79ebd04e67d9e7db10675b5c31b6072803d986dad229ef6b115e0a659",
        "42ca63b1fb6d256852cd0f758fbf852b01b31c594705c0bb051b6bc18ca1f989",
        "1b038d38cf9879b32581068e266c521fd5368aa5e57d50e21f979c25b2148f76",
        "7925f0de66f0d7fd7412dfae3e9754dcbceb8d0516b875552f98b3f637225fba",
        "de9599ac4d22582fea1746eda8deb885e6bc9f2c1d79c9fd723de299cc0edb74",
        "af28c19dafb2f01461c5b3c3a648a7ac4f200a71880a965b2ff9da892792fa84",
    )


def _coverage_binding() -> execution.CoverageSmokeLaunchBinding:
    contract = {
        "manifest_id": "shape-placement-factorial-v28",
        "coverage_smoke_id": (
            "shape-placement-factorial-v28-excluded-n31-coverage-smoke-v1"
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
        contract_payload=execution._canonical_json_bytes(contract),
        ledger_prefix_payload=b"",
        predecessor_receipt_payload=None,
    )


def _native_row(name: str, timestamp_ns: int, event_type: str) -> dict[str, object]:
    return {
        "name": name,
        "monotonic_ns": timestamp_ns,
        "event": {
            "relative_path": "structured-events.jsonl",
            "line_number": 1,
            "source_id": "source",
            "source_sequence": 1,
            "source_monotonic_ns": timestamp_ns,
            "event_type": event_type,
            "line_sha256": "a" * 64,
        },
    }


def _repair_observation(
    spec: runtime_module.SlotRuntimeSpec,
    anchor_ns: int,
) -> dict[str, object]:
    second = 1_000_000_000
    fault_end = anchor_ns + 450 * second
    hard_end = anchor_ns + 650 * second
    timestamps = (
        anchor_ns + 250 * second,
        anchor_ns + 251 * second,
        anchor_ns + 252 * second,
        anchor_ns + 253 * second,
        anchor_ns + 290 * second,
        fault_end,
        anchor_ns + 470 * second,
        anchor_ns + 471 * second,
        anchor_ns + 472 * second,
        anchor_ns + 473 * second,
        anchor_ns + 520 * second,
        anchor_ns + 550 * second,
    )
    names = (
        "epoch1_selection",
        "epoch1_command",
        "epoch1_activation",
        "epoch1_terminal",
        "epoch1_stable_end",
        "fault_window_end",
        "epoch2_selection",
        "epoch2_command",
        "epoch2_activation",
        "epoch2_terminal",
        "epoch2_stable_end",
        "epoch2_drain_complete",
    )
    event_types = {
        0: "adaptive_v2_evidence_snapshot",
        1: "epoch.command_committed",
        2: "epoch.activated",
        3: "adaptive_v2_session_terminal",
        6: "adaptive_v2_shape_decision",
        7: "epoch.command_committed",
        8: "epoch.activated",
        9: "adaptive_v2_session_terminal",
        11: "block.committed",
    }
    rows: list[dict[str, object]] = []
    for index, (name, timestamp) in enumerate(zip(names, timestamps, strict=True)):
        if index in event_types:
            rows.append(_native_row(name, timestamp, event_types[index]))
        else:
            rows.append(
                {
                    "name": name,
                    "monotonic_ns": timestamp,
                    "derivation": {
                        4: "phase.epoch1_stable.end_monotonic_ns",
                        5: "shared_anchor_plus_fault_start_and_duration",
                        10: "phase.epoch2_stable.end_monotonic_ns",
                    }[index],
                }
            )
    return {
        "schema_version": 1,
        "phases": [
            {
                "phase": "epoch1_stable",
                "end_monotonic_ns": timestamps[4],
            },
            {
                "phase": "epoch2_stable",
                "end_monotonic_ns": timestamps[10],
            },
        ],
        "excluded_repair_observation": {
            "schema_version": 1,
            "observation_contract": OBSERVATION_CONTRACT,
            "fault_window_end_monotonic_ns": fault_end,
            "hard_deadline_monotonic_ns": hard_end,
            "rows": rows,
        },
    }


def test_v28_runner_special_bound_requires_exact_coverage_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    primary_runtime, repair_runtime = coverage.runtimes
    binding = _coverage_binding()

    assert not execution._uses_exact_excluded_repair_smoke_bound(
        primary_runtime,
        binding,
    )
    assert not execution._uses_exact_excluded_repair_smoke_bound(
        repair_runtime,
        None,
    )
    assert execution._uses_exact_excluded_repair_smoke_bound(
        repair_runtime,
        binding,
    )
    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    with pytest.raises(execution.FactorialExecutionError, match="binding drifted"):
        execution._uses_exact_excluded_repair_smoke_bound(
            replace(
                repair_runtime,
                excluded_repair_smoke_probe=replace(
                    probe,
                    verified_response_duplicate_probe_mode=PROBE_MODE + "-drift",
                ),
            ),
            binding,
        )


def test_v28_runner_accepts_exact_recovery_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    spec = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    ).runtimes[1]
    anchor_ns = 1_000_000_000
    execution._assert_excluded_repair_smoke_completion(
        spec,
        _repair_observation(spec, anchor_ns),
        shared_raw_clock_anchor_ns=anchor_ns,
    )


@pytest.mark.parametrize(
    ("row_index", "bound"),
    ((4, "fault"), (6, "fault"), (7, "fault"), (10, "hard"), (11, "hard")),
)
def test_v28_runner_rejects_equality_at_every_recovery_boundary(
    monkeypatch: pytest.MonkeyPatch,
    row_index: int,
    bound: str,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    spec = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    ).runtimes[1]
    anchor_ns = 1_000_000_000
    document = _repair_observation(spec, anchor_ns)
    observation = document["excluded_repair_observation"]
    rows = observation["rows"]  # type: ignore[index]
    boundary = observation[f"{bound}_window_end_monotonic_ns"] if bound == "fault" else observation["hard_deadline_monotonic_ns"]  # type: ignore[index]
    rows[row_index]["monotonic_ns"] = boundary  # type: ignore[index]
    if row_index in {4, 10}:
        phase_name = "epoch1_stable" if row_index == 4 else "epoch2_stable"
        next(
            phase
            for phase in document["phases"]  # type: ignore[index]
            if phase["phase"] == phase_name
        )["end_monotonic_ns"] = boundary
    else:
        rows[row_index]["event"]["source_monotonic_ns"] = boundary  # type: ignore[index]
    with pytest.raises(execution.IncompleteFactorialSlot, match="strictly"):
        execution._assert_excluded_repair_smoke_completion(
            spec,
            document,
            shared_raw_clock_anchor_ns=anchor_ns,
        )


def test_v28_is_validation_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST != V28_MANIFEST
    assert cli.main(["--manifest", str(V28_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v43 are validation-only" in refusal["reason"]
