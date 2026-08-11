"""Producer contract for the v39 cycle-1 absolute selection gate roll."""

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
V39_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v39.json"
)
V38_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v38.json"
)
ZERO_SHA256 = "0" * 64
CYCLE1_SELECTION_NOT_BEFORE_OPTION = (
    "--cycle-1-selection-not-before-monotonic-ns"
)
CYCLE1_SELECTION_NOT_BEFORE_TOKEN = (
    "{{cycle_1_selection_not_before_monotonic_ns}}"
)
EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4 = (
    "exact_excluded_repair_smoke_source_fault_window_duration_450s_effective_"
    "fault_window_duration_330s_hard_deadline_650s_post_fault_cycle1_selection_"
    "observation_grace_5s_with_exact_cycle1_manager_selection_gate_bound_to_the_"
    "materialized_shared_clock_fault_end_plus_the_frozen_observation_grace_as_"
    "an_exclusive_lower_bound_with_fault_evidence_and_epoch1_stable_as_the_only_"
    "fault_active_causal_phases_with_epoch1_selection_terminal_all_replica_"
    "command_activation_and_stable_end_before_fault_end_then_epoch2_as_post_"
    "fault_recovery_and_stability_with_fault_end_before_cycle1_selection_and_"
    "all_replica_commands_then_epoch2_terminal_all_replica_activation_stable_"
    "end_and_drain_before_shared_hard_deadline_v4"
)
EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V3 = (
    "byzantine.window.duration_s:450->330;cycle1.selection.exclusive_lower_"
    "bound:unset->effective_fault_window_end_plus_5s"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V39_MANIFEST.read_bytes()
    semantic_sha256 = hashlib.sha256(_canonical(json.loads(payload))).hexdigest()
    monkeypatch.setattr(manifest_module, "FROZEN_SEMANTIC_SHA256", semantic_sha256)
    manifest = manifest_module.parse_manifest_bytes(payload)
    plan = manifest_module.build_factorial_plan(manifest)
    monkeypatch.setattr(
        manifest_module, "FROZEN_MANIFEST_SHA256", manifest.manifest_sha256
    )
    monkeypatch.setattr(manifest_module, "FROZEN_PLAN_SHA256", plan.plan_sha256)
    return manifest, plan


def _coverage(plan):
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    return execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )


def _secrets(replica_count: int) -> runtime_module.ManagerSecretMaterial:
    return runtime_module.ManagerSecretMaterial(
        manager_tls_private_key_der_hex="a1b2",
        manager_tls_certificate_der_hex="c3d4",
        issuer_private_key_hex="11" * 32,
        replica_tls_certificate_der_hex=tuple(
            f"{replica_id + 1:02x}" for replica_id in range(replica_count)
        ),
    )


def _option(argv: tuple[str, ...], name: str) -> str:
    assert argv.count(name) == 1
    return argv[argv.index(name) + 1]


def test_v39_profile_is_one_lf_and_exact_three_path_delta() -> None:
    v39_payload = V39_MANIFEST.read_bytes()
    v38_payload = V38_MANIFEST.read_bytes()

    assert v39_payload.endswith(b"\n")
    assert not v39_payload.endswith(b"\n\n")
    assert v39_payload.count(b"shape-placement-factorial-v39") == 2
    v39 = json.loads(v39_payload)
    v38 = json.loads(v38_payload)
    assert v39.pop("manifest_id") == "shape-placement-factorial-v39"
    assert v38.pop("manifest_id") == "shape-placement-factorial-v38"
    assert v39["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v39"
    )
    assert v38["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v38"
    )
    v39_responsive = v39["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v38_responsive = v38["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert v39_responsive.pop(
        "excluded_repair_smoke_observation_contract"
    ) == EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
    assert v38_responsive.pop(
        "excluded_repair_smoke_observation_contract"
    ) == manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
    assert v39 == v38


def test_v38_v39_identities_are_historical() -> None:
    assert (
        getattr(manifest_module, "V38_MANIFEST_ID", None),
        getattr(manifest_module, "V38_MANIFEST_SHA256", None),
        getattr(manifest_module, "V38_SEMANTIC_SHA256", None),
        getattr(manifest_module, "V38_PLAN_SHA256", None),
        getattr(runtime_module, "V38_RUNTIME_SHA256", None),
        getattr(runtime_module, "V38_SMOKE_RUNTIME_SHA256", None),
        getattr(runtime_module, "V38_COVERAGE_SMOKE_RUNTIME_SHA256", None),
    ) == (
        "shape-placement-factorial-v38",
        "aec2c4f2a9cb53e7b3d8d212bc0b56c008679aa97ebba140db7bac9404415698",
        "9af592b436d934d13b1243439b84f78e1a78c19b035b37dbfe73bc168926b277",
        "7e731a7f36a49a5e49aa62601165bd1fe8c846e3eff20001d0fccfe37b6050e0",
        "2f080f10550c6eaac914435b436724a43f38615097063d3a70437a4459f69c3c",
        "f1ae1fcffdaf4b209a595f3dd034ec935ad88ada5821930c105694b525e2817a",
        "3cf676755478f686af64f12fd3fcb5993f4887a8f5165fbf540d289463544950",
    )
    assert manifest_module.V39_MANIFEST_ID == "shape-placement-factorial-v39"
    assert (
        manifest_module.V39_MANIFEST_SHA256,
        manifest_module.V39_SEMANTIC_SHA256,
        manifest_module.V39_PLAN_SHA256,
        runtime_module.V39_RUNTIME_SHA256,
        runtime_module.V39_SMOKE_RUNTIME_SHA256,
        runtime_module.V39_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "ce6fb4c999275b575f1cf522524a5f3b41d109a6dcb3a1b77e789946b67b042f",
        "14a3c3b910c89368481780d176e031e3bffeb15731b3448359fd73b10aee0a5a",
        "481b9df491a68355eade98bf241e9b2805430276681c148cf2178ad5e3c794a0",
        "8c46107ccea00424961501d31e9c85534d129353d8ed7fabba65e52c1049b17d",
        "ad75abbf4592661aee236625a18c61a476f730d2825378e6598b42a63882fe65",
        "aba47a1d783aa21769f153236e9f37c755cc7ef2ce6248c17cd21e84b505e856",
    )


def test_v39_threads_v4_delta_v3_and_exact_repair_only_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, plan = _candidate_plan(monkeypatch)
    campaign = runtime_module.build_factorial_runtime(plan)
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    coverage = _coverage(plan)

    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    assert responsive.excluded_repair_smoke_observation_contract == (
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
    )
    assert manifest.common_timers.aggregation_timeout_ms_per_depth == 125
    assert manifest.common_timers.leader_progress_timeout_ms == 20_000
    assert manifest.common_timers.transition_convergence_deadline_s == 30
    assert manifest.common_timers.hard_timeout_s == 650
    assert all(slot.fault_window.duration_s == 450 for slot in campaign.slots)
    assert all(
        CYCLE1_SELECTION_NOT_BEFORE_OPTION not in slot.manager_argv_template.argv
        for slot in campaign.slots
    )
    assert CYCLE1_SELECTION_NOT_BEFORE_OPTION not in n7.runtime.manager_argv_template.argv

    primary_runtime, repair_runtime = coverage.runtimes
    assert primary_runtime.slot_id == "slot-066-n31-f5-b05-P"
    assert primary_runtime.fault_window.duration_s == 450
    assert CYCLE1_SELECTION_NOT_BEFORE_OPTION not in (
        primary_runtime.manager_argv_template.argv
    )
    assert repair_runtime.slot_id == "slot-037-n31-f2-b04-00"
    assert repair_runtime.fault_window.duration_s == 330
    assert repair_runtime.fault_window.hard_timeout_s == 650
    assert runtime_module.EXCLUDED_REPAIR_SMOKE_POST_FAULT_OBSERVATION_GRACE_S == 5
    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    assert probe.semantic_delta == EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V3
    assert probe.observation_contract == EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
    manager = repair_runtime.manager_argv_template.argv
    assert manager.count(CYCLE1_SELECTION_NOT_BEFORE_OPTION) == 1
    assert _option(manager, CYCLE1_SELECTION_NOT_BEFORE_OPTION) == (
        CYCLE1_SELECTION_NOT_BEFORE_TOKEN
    )


def test_v39_materializes_gate_to_exact_shared_fault_end_and_seals_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    repair = _coverage(plan).runtimes[1]
    anchor_ns = 7_000_000_000
    expected_end_ns = anchor_ns + (150 + 330) * 1_000_000_000
    expected_gate_ns = expected_end_ns + 5 * 1_000_000_000
    manager = runtime_module.materialize_manager_argv(
        repair,
        tmp_path / repair.slot_id,
        _secrets(repair.replica_count),
        shared_raw_clock_anchor_ns=anchor_ns,
    )
    replicas = runtime_module.materialize_replica_argv(
        repair,
        tmp_path / repair.slot_id,
        anchor_ns,
    )
    assert _option(manager, CYCLE1_SELECTION_NOT_BEFORE_OPTION) == str(
        expected_gate_ns
    )
    assert all(
        _option(process.argv, "--experiment-byzantine-window-end-monotonic-ns")
        == str(expected_end_ns)
        for process in replicas
    )
    launch = execution.MaterializedLaunch(
        manager_argv=manager,
        replica_argv=replicas,
        redacted_manager_argv=manager,
        redacted_replica_argv=replicas,
        input_artifacts=(),
        redaction_key_id="test",
    )
    receipt = execution._launch_receipt(
        repair,
        launch,
        anchor_ns=anchor_ns,
        manifest_sha256="a" * 64,
        plan_sha256="b" * 64,
        runtime_sha256="c" * 64,
    )
    assert receipt["fault_window_end_ns"] == expected_end_ns
    assert _option(tuple(receipt["manager_argv"]), CYCLE1_SELECTION_NOT_BEFORE_OPTION) == str(  # type: ignore[arg-type]
        expected_gate_ns
    )


@pytest.mark.parametrize("grace_s", (4, 6))
def test_v39_receipt_rejects_drifted_post_fault_observation_grace(
    grace_s: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    repair = _coverage(plan).runtimes[1]
    anchor_ns = 7_000_000_000
    manager = list(
        runtime_module.materialize_manager_argv(
            repair,
            tmp_path / repair.slot_id,
            _secrets(repair.replica_count),
            shared_raw_clock_anchor_ns=anchor_ns,
        )
    )
    option_index = manager.index(CYCLE1_SELECTION_NOT_BEFORE_OPTION)
    manager[option_index + 1] = str(
        anchor_ns + (150 + 330 + grace_s) * 1_000_000_000
    )
    replicas = runtime_module.materialize_replica_argv(
        repair,
        tmp_path / repair.slot_id,
        anchor_ns,
    )
    launch = execution.MaterializedLaunch(
        manager_argv=tuple(manager),
        replica_argv=replicas,
        redacted_manager_argv=tuple(manager),
        redacted_replica_argv=replicas,
        input_artifacts=(),
        redaction_key_id="test",
    )
    with pytest.raises(
        execution.FactorialExecutionError,
        match="cycle-1 selection gate",
    ):
        execution._launch_receipt(
            repair,
            launch,
            anchor_ns=anchor_ns,
            manifest_sha256="a" * 64,
            plan_sha256="b" * 64,
            runtime_sha256="c" * 64,
        )


@pytest.mark.parametrize("mutation", ("missing", "duplicate", "tampered"))
def test_v39_rejects_drifted_repair_gate_template(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    repair = _coverage(plan).runtimes[1]
    argv = list(repair.manager_argv_template.argv)
    index = argv.index(CYCLE1_SELECTION_NOT_BEFORE_OPTION)
    if mutation == "missing":
        del argv[index : index + 2]
    elif mutation == "duplicate":
        argv.extend(
            (CYCLE1_SELECTION_NOT_BEFORE_OPTION, CYCLE1_SELECTION_NOT_BEFORE_TOKEN)
        )
    else:
        argv[index + 1] = "{{cycle_1_selection_not_before_monotonic_ns_tampered}}"
    candidate = replace(
        repair,
        manager_argv_template=replace(
            repair.manager_argv_template,
            argv=tuple(argv),
        ),
    )
    with pytest.raises(manifest_module.FactorialManifestError):
        runtime_module.materialize_manager_argv(
            candidate,
            tmp_path / candidate.slot_id,
            _secrets(candidate.replica_count),
            shared_raw_clock_anchor_ns=7_000_000_000,
        )


def test_v39_rejects_gate_injection_outside_exact_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    primary = _coverage(plan).runtimes[0]
    candidate = replace(
        primary,
        manager_argv_template=replace(
            primary.manager_argv_template,
            argv=(
                *primary.manager_argv_template.argv,
                CYCLE1_SELECTION_NOT_BEFORE_OPTION,
                CYCLE1_SELECTION_NOT_BEFORE_TOKEN,
            ),
        ),
    )
    with pytest.raises(manifest_module.FactorialManifestError):
        runtime_module.materialize_manager_argv(
            candidate,
            tmp_path / candidate.slot_id,
            _secrets(candidate.replica_count),
            shared_raw_clock_anchor_ns=7_000_000_000,
        )


@pytest.mark.parametrize("anchor_ns", (True, -1, (1 << 64) - 1 - 482_000_000_000))
def test_v39_rejects_invalid_or_overflowing_gate_anchor(
    anchor_ns: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    repair = _coverage(plan).runtimes[1]
    with pytest.raises(manifest_module.FactorialManifestError):
        runtime_module.materialize_manager_argv(
            repair,
            tmp_path / repair.slot_id,
            _secrets(repair.replica_count),
            shared_raw_clock_anchor_ns=anchor_ns,  # type: ignore[arg-type]
        )


def test_v38_history_has_no_cycle1_absolute_gate() -> None:
    manifest = manifest_module.load_frozen_manifest(V38_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    coverage = _coverage(plan)

    assert all(
        CYCLE1_SELECTION_NOT_BEFORE_OPTION not in runtime.manager_argv_template.argv
        for runtime in coverage.runtimes
    )
    probe = coverage.runtimes[1].excluded_repair_smoke_probe
    assert probe is not None
    assert probe.semantic_delta == runtime_module.EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2
    assert probe.observation_contract == (
        manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
    )


def test_v39_is_default_and_v38_is_validation_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v42.json"
    assert cli.main(["--manifest", str(V38_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v41 are validation-only" in refusal["reason"]
