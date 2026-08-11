"""Producer contract for the v40 repair-readiness admission roll."""

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
V40_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v40.json"
)
V39_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v39.json"
)
V28_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v28.json"
)
V39_SIX = (
    "ce6fb4c999275b575f1cf522524a5f3b41d109a6dcb3a1b77e789946b67b042f",
    "14a3c3b910c89368481780d176e031e3bffeb15731b3448359fd73b10aee0a5a",
    "481b9df491a68355eade98bf241e9b2805430276681c148cf2178ad5e3c794a0",
    "8c46107ccea00424961501d31e9c85534d129353d8ed7fabba65e52c1049b17d",
    "ad75abbf4592661aee236625a18c61a476f730d2825378e6598b42a63882fe65",
    "aba47a1d783aa21769f153236e9f37c755cc7ef2ce6248c17cd21e84b505e856",
)
V40_SIX = (
    "a058ad5ac30aebbf866ba96c3ca60411b831875c36e198d6f6a4de08fbfa476d",
    "6669bb5c4165eb83e26771348cb2ab06723782a7b3a111342ed56f9740746e5a",
    "561f6f74fd27e165eed38d24e1aeca6c6fd4acc3f93743a7b2387e8e8da2e8d7",
    "5286f8815b1812afa13f39d84e872af8dbfe78d675f53e8a02ea1c56e5acccdc",
    "4522bf796e10f67d2394ceab3399b5755ee41914dc30375ce0f87a8e0e5459eb",
    "70847285deb39a40addbbd1cc118fff0e3ed97498cb300e1a45564cc13f7d79e",
)
V5 = (
    "exact_excluded_repair_smoke_source_fault_window_duration_450s_effective_"
    "fault_window_duration_360s_hard_deadline_650s_post_fault_cycle1_selection_"
    "observation_grace_5s_with_exact_cycle1_manager_selection_gate_bound_to_the_"
    "materialized_shared_clock_fault_end_plus_the_frozen_observation_grace_as_an_"
    "exclusive_lower_bound_and_exact_cycle1_inherited_wait_exempt_eligibility_"
    "gate_requiring_every_canonical_inherited_wait_exempt_replica_to_be_"
    "responsive_and_eligible_in_the_exact_baseline_exclusive_selected_suffix_at_"
    "the_same_cycle1_selection_cutoff_and_exact_cycle1_authenticated_reporter_"
    "cross_commit_retention_readiness_gate_requiring_one_schema_v2_attempt_start_"
    "and_reporter_local_commit_timeout_fact_per_canonical_responsive_degraded_"
    "actor_and_at_least_one_aggregate_relay_fact_at_the_same_evidence_high_water_"
    "cutoff_with_fault_evidence_and_epoch1_stable_as_the_only_fault_active_"
    "causal_phases_with_epoch1_selection_terminal_all_replica_command_activation_"
    "and_stable_end_before_fault_end_then_epoch2_as_post_fault_recovery_and_"
    "stability_with_fault_end_before_cycle1_selection_and_all_replica_commands_"
    "then_epoch2_terminal_all_replica_activation_stable_end_drain_and_runner_"
    "terminal_before_shared_hard_deadline_v5"
)
DELTA_V4 = (
    "byzantine.window.duration_s:450->360;response_evidence.timeout_retention_"
    "fact:absent->schema_v2_attempt_start_and_reporter_local_commit;cycle1."
    "selection.exclusive_lower_bound:unset->effective_fault_window_end_plus_5s;"
    "cycle1.selection.inherited_wait_exempt_responsive_eligible_gate:disabled->"
    "required_at_same_selected_suffix_cutoff;cycle1.selection.responsive_cross_"
    "commit_retention_readiness_gate:disabled->required_at_same_evidence_high_"
    "water_cutoff"
)
SELECTION_OPTION = "--cycle-1-selection-not-before-monotonic-ns"
SELECTION_TOKEN = "{{cycle_1_selection_not_before_monotonic_ns}}"
ELIGIBILITY_OPTION = "--cycle-1-inherited-wait-exempt-eligibility-gate"
RETENTION_OPTION = "--cycle-1-responsive-cross-commit-retention-readiness-gate"
RESPONSIVE_ACTORS_OPTION = "--cycle-1-responsive-degraded-actors"
RESPONSIVE_ACTORS = "1,7,8,12,16,19,20"
REPORTER_V2_CONFIG_KEY = "experiment-responsive-cross-commit-retention-v2"
REPORTER_V2_CONFIG_LINE = f"{REPORTER_V2_CONFIG_KEY} = true"


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V40_MANIFEST.read_bytes()
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
    return execution.build_n31_coverage_smoke_slot(primary, repair_template=repair)


@pytest.mark.parametrize(
    ("manifest_path", "expected_sha256"),
    (
        (V28_MANIFEST, runtime_module.V28_COVERAGE_SMOKE_RUNTIME_SHA256),
        (V39_MANIFEST, runtime_module.V39_COVERAGE_SMOKE_RUNTIME_SHA256),
    ),
)
def test_v40_optionals_preserve_exact_historical_coverage_runtime_bytes(
    manifest_path: Path,
    expected_sha256: str,
) -> None:
    manifest = manifest_module.load_frozen_manifest(manifest_path)
    plan = manifest_module.build_factorial_plan(manifest)
    coverage = _coverage(plan)

    payload = execution._canonical_json_bytes(coverage.runtime.as_document())
    assert hashlib.sha256(payload).hexdigest() == expected_sha256
    repair_document = json.loads(payload)["slots"][1][
        "excluded_repair_smoke_probe"
    ]
    assert set(repair_document).isdisjoint(
        {
            "post_fault_observation_grace_s",
            "cycle1_inherited_wait_exempt_eligibility_gate",
            "cycle1_responsive_cross_commit_retention_readiness_gate",
            "canonical_responsive_degraded_actor_ids",
            "response_evidence_timeout_retention_schema_version",
            "runner_terminal_strictly_before_hard_deadline",
        }
    )


def _materialized_repair(plan, root: Path):
    coverage = _coverage(plan)
    slot = coverage.slots[1]
    spec = coverage.runtimes[1]
    slot_directory = root / spec.slot_id
    execution._create_slot_directories(slot_directory, spec)
    for label in ("bls", "tls", "issuer"):
        execution._write_exclusive(
            slot_directory / f"runtime/{label}-identities.txt",
            f"{label}-identity-input\n".encode(),
        )
    identities = _identities(spec.replica_count)
    input_artifacts = execution.write_slot_configs(
        slot,
        spec,
        slot_directory=slot_directory,
        identities=identities,
    )
    materialized = execution.materialize_launch(
        slot,
        spec,
        slot_directory=slot_directory,
        binaries=execution.ExecutionBinaries(
            app=root / "bin/hotstuff-app",
            manager=root / "bin/adaptation-manager",
            keygen=root / "bin/hotstuff-keygen",
            tls_keygen=root / "bin/hotstuff-tls-keygen",
        ),
        identities=identities,
        input_artifacts=input_artifacts,
        shared_raw_clock_anchor_ns=7_000_000_000,
        redaction_key=b"r" * 32,
    )
    return slot, spec, materialized, input_artifacts, slot_directory


def _secrets(replica_count: int) -> runtime_module.ManagerSecretMaterial:
    return runtime_module.ManagerSecretMaterial(
        manager_tls_private_key_der_hex="a1b2",
        manager_tls_certificate_der_hex="c3d4",
        issuer_private_key_hex="11" * 32,
        replica_tls_certificate_der_hex=tuple(
            f"{replica_id + 1:02x}" for replica_id in range(replica_count)
        ),
    )


def _identities(replica_count: int) -> execution.IdentityMaterial:
    return execution.IdentityMaterial(
        bls=tuple(
            {"pub": f"{replica_id + 1:064x}", "sec": f"{replica_id + 101:064x}"}
            for replica_id in range(replica_count)
        ),
        tls=tuple(
            {
                "crt": f"{replica_id + 201:064x}",
                "sec": f"{replica_id + 301:064x}",
                "cid": f"cid-{replica_id}",
            }
            for replica_id in range(replica_count + 1)
        ),
        issuer={"pub": f"{901:064x}", "sec": f"{902:064x}"},
    )


def _option(argv: tuple[str, ...], name: str) -> str:
    assert argv.count(name) == 1
    return argv[argv.index(name) + 1]


def _coverage_binding(version: str) -> execution.CoverageSmokeLaunchBinding:
    return execution.CoverageSmokeLaunchBinding(
        contract_payload=_canonical(
            {
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
        ),
        ledger_prefix_payload=b"",
        predecessor_receipt_payload=None,
    )


def test_v40_profile_is_one_lf_and_exact_three_path_delta() -> None:
    v40_payload = V40_MANIFEST.read_bytes()
    v39_payload = V39_MANIFEST.read_bytes()
    assert v40_payload.endswith(b"\n")
    assert not v40_payload.endswith(b"\n\n")
    assert v40_payload.count(b"shape-placement-factorial-v40") == 2
    v40 = json.loads(v40_payload)
    v39 = json.loads(v39_payload)
    assert v40.pop("manifest_id") == "shape-placement-factorial-v40"
    assert v39.pop("manifest_id") == "shape-placement-factorial-v39"
    assert v40["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v40"
    )
    assert v39["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v39"
    )
    v40_responsive = v40["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v39_responsive = v39["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert v40_responsive.pop("excluded_repair_smoke_observation_contract") == V5
    assert v39_responsive.pop("excluded_repair_smoke_observation_contract") == (
        manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
    )
    assert v40 == v39


def test_v39_and_v40_identities_are_exactly_historical() -> None:
    assert (
        getattr(manifest_module, "V39_MANIFEST_SHA256", None),
        getattr(manifest_module, "V39_SEMANTIC_SHA256", None),
        getattr(manifest_module, "V39_PLAN_SHA256", None),
        getattr(runtime_module, "V39_RUNTIME_SHA256", None),
        getattr(runtime_module, "V39_SMOKE_RUNTIME_SHA256", None),
        getattr(runtime_module, "V39_COVERAGE_SMOKE_RUNTIME_SHA256", None),
    ) == V39_SIX
    assert getattr(manifest_module, "V39_MANIFEST_ID", None) == (
        "shape-placement-factorial-v39"
    )
    assert manifest_module.V40_MANIFEST_ID == "shape-placement-factorial-v40"
    assert (
        manifest_module.V40_MANIFEST_SHA256,
        manifest_module.V40_SEMANTIC_SHA256,
        manifest_module.V40_PLAN_SHA256,
        runtime_module.V40_RUNTIME_SHA256,
        runtime_module.V40_SMOKE_RUNTIME_SHA256,
        runtime_module.V40_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == V40_SIX


def test_v40_threads_v5_delta_v4_and_exact_repair_only_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, plan = _candidate_plan(monkeypatch)
    campaign = runtime_module.build_factorial_runtime(plan)
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    coverage = _coverage(plan)

    assert manifest.common_timers.aggregation_timeout_ms_per_depth == 125
    assert manifest.common_timers.hard_timeout_s == 650
    assert manifest.common_timers.transition_convergence_deadline_s == 30
    assert all("aggregation-timeout = 0.5" in slot.main_config.lines for slot in campaign.slots)
    assert all(slot.fault_window.duration_s == 450 for slot in campaign.slots)
    forbidden = {
        ELIGIBILITY_OPTION,
        RETENTION_OPTION,
        RESPONSIVE_ACTORS_OPTION,
    }
    assert all(
        forbidden.isdisjoint(slot.manager_argv_template.argv)
        and REPORTER_V2_CONFIG_LINE not in slot.main_config.lines
        for slot in campaign.slots
    )
    assert forbidden.isdisjoint(n7.runtime.manager_argv_template.argv)
    assert REPORTER_V2_CONFIG_LINE not in n7.runtime.main_config.lines

    primary, repair = coverage.runtimes
    assert primary.fault_window.duration_s == 450
    assert "aggregation-timeout = 0.5" in primary.main_config.lines
    assert forbidden.isdisjoint(primary.manager_argv_template.argv)
    assert REPORTER_V2_CONFIG_LINE not in primary.main_config.lines
    assert repair.fault_window.duration_s == 360
    assert repair.fault_window.hard_timeout_s == 650
    assert "aggregation-timeout = 0.5" in repair.main_config.lines
    probe = repair.excluded_repair_smoke_probe
    assert probe is not None
    assert probe.semantic_delta == DELTA_V4
    assert probe.observation_contract == V5
    assert probe.canonical_responsive_degraded_actor_ids == (1, 7, 8, 12, 16, 19, 20)
    assert probe.response_evidence_timeout_retention_schema_version == 2
    assert probe.runner_terminal_strictly_before_hard_deadline
    repair_document = repair.as_document()["excluded_repair_smoke_probe"]
    assert isinstance(repair_document, dict)
    assert {
        key: repair_document[key]
        for key in (
            "post_fault_observation_grace_s",
            "cycle1_inherited_wait_exempt_eligibility_gate",
            "cycle1_responsive_cross_commit_retention_readiness_gate",
            "canonical_responsive_degraded_actor_ids",
            "response_evidence_timeout_retention_schema_version",
            "runner_terminal_strictly_before_hard_deadline",
        )
    } == {
        "post_fault_observation_grace_s": 5,
        "cycle1_inherited_wait_exempt_eligibility_gate": True,
        "cycle1_responsive_cross_commit_retention_readiness_gate": True,
        "canonical_responsive_degraded_actor_ids": (1, 7, 8, 12, 16, 19, 20),
        "response_evidence_timeout_retention_schema_version": 2,
        "runner_terminal_strictly_before_hard_deadline": True,
    }
    manager = repair.manager_argv_template.argv
    assert _option(manager, SELECTION_OPTION) == SELECTION_TOKEN
    assert manager.count(ELIGIBILITY_OPTION) == 1
    assert manager.count(RETENTION_OPTION) == 1
    assert _option(manager, RESPONSIVE_ACTORS_OPTION) == RESPONSIVE_ACTORS
    assert repair.main_config.lines.count(REPORTER_V2_CONFIG_LINE) == 1
    assert all(
        REPORTER_V2_CONFIG_KEY not in argument
        for process in repair.replica_argv_templates
        for argument in process.argv
    )


def test_v40_materializes_and_receipts_all_repair_only_gates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    coverage = _coverage(plan)
    repair_slot = coverage.slots[1]
    repair = coverage.runtimes[1]
    anchor_ns = 7_000_000_000
    fault_end_ns = anchor_ns + (150 + 360) * 1_000_000_000
    selection_gate_ns = fault_end_ns + 5 * 1_000_000_000
    slot_directory = tmp_path / repair.slot_id
    execution._create_slot_directories(slot_directory, repair)
    for label in ("bls", "tls", "issuer"):
        execution._write_exclusive(
            slot_directory / f"runtime/{label}-identities.txt",
            f"{label}-identity-input\n".encode(),
        )
    identities = _identities(repair.replica_count)
    input_artifacts = execution.write_slot_configs(
        repair_slot,
        repair,
        slot_directory=slot_directory,
        identities=identities,
    )
    main_config = slot_directory / "runtime/main.conf"
    main_payload = main_config.read_bytes()
    assert main_payload.count((REPORTER_V2_CONFIG_LINE + "\n").encode()) == 1
    materialized = execution.materialize_launch(
        repair_slot,
        repair,
        slot_directory=slot_directory,
        binaries=execution.ExecutionBinaries(
            app=tmp_path / "bin/hotstuff-app",
            manager=tmp_path / "bin/adaptation-manager",
            keygen=tmp_path / "bin/hotstuff-keygen",
            tls_keygen=tmp_path / "bin/hotstuff-tls-keygen",
        ),
        identities=identities,
        input_artifacts=input_artifacts,
        shared_raw_clock_anchor_ns=anchor_ns,
        redaction_key=b"r" * 32,
    )
    manager = materialized.redacted_manager_argv
    replicas = materialized.redacted_replica_argv
    assert _option(manager, SELECTION_OPTION) == str(selection_gate_ns)
    assert manager.count(ELIGIBILITY_OPTION) == 1
    assert manager.count(RETENTION_OPTION) == 1
    assert _option(manager, RESPONSIVE_ACTORS_OPTION) == RESPONSIVE_ACTORS
    assert repair.main_config.lines.count(REPORTER_V2_CONFIG_LINE) == 1
    assert all(
        REPORTER_V2_CONFIG_KEY not in argument
        for process in replicas
        for argument in process.argv
    )
    receipt = execution._launch_receipt(
        repair,
        materialized,
        anchor_ns=anchor_ns,
        manifest_sha256="a" * 64,
        plan_sha256="b" * 64,
        runtime_sha256="c" * 64,
    )
    assert receipt["fault_window_end_ns"] == fault_end_ns
    assert _option(tuple(receipt["manager_argv"]), SELECTION_OPTION) == str(selection_gate_ns)  # type: ignore[arg-type]
    expected_main_sha256 = hashlib.sha256(main_payload).hexdigest()
    assert receipt["main_config_sha256"] == expected_main_sha256
    main_row = next(
        row for row in input_artifacts if row["relative_path"] == "runtime/main.conf"
    )
    assert main_row["sha256"] == expected_main_sha256


@pytest.mark.parametrize("mutation", ("missing", "false", "duplicate"))
def test_v40_prelaunch_rejects_materialized_reporter_v2_config_drift(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    slot, spec, _materialized, input_artifacts, slot_directory = (
        _materialized_repair(plan, tmp_path)
    )
    main_path = slot_directory / "runtime/main.conf"
    payload = main_path.read_text()
    if mutation == "missing":
        payload = payload.replace(REPORTER_V2_CONFIG_LINE + "\n", "")
    elif mutation == "false":
        payload = payload.replace(REPORTER_V2_CONFIG_LINE, f"{REPORTER_V2_CONFIG_KEY} = false")
    else:
        payload += REPORTER_V2_CONFIG_LINE + "\n"
    main_path.write_text(payload)
    with pytest.raises(
        execution.FactorialExecutionError,
        match="runtime input changed before argv binding",
    ):
        execution.materialize_launch(
            slot,
            spec,
            slot_directory=slot_directory,
            binaries=execution.ExecutionBinaries(
                app=tmp_path / "bin/hotstuff-app",
                manager=tmp_path / "bin/adaptation-manager",
                keygen=tmp_path / "bin/hotstuff-keygen",
                tls_keygen=tmp_path / "bin/hotstuff-tls-keygen",
            ),
            identities=_identities(spec.replica_count),
            input_artifacts=input_artifacts,
            shared_raw_clock_anchor_ns=7_000_000_000,
            redaction_key=b"r" * 32,
        )


@pytest.mark.parametrize("mutation", ("missing", "duplicate", "hash"))
def test_v40_receipt_rejects_main_config_input_hash_drift(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    _slot, spec, materialized, input_artifacts, _slot_directory = (
        _materialized_repair(plan, tmp_path)
    )
    main_row = next(
        row for row in input_artifacts if row["relative_path"] == "runtime/main.conf"
    )
    rows = list(input_artifacts)
    if mutation == "missing":
        rows.remove(main_row)
    elif mutation == "duplicate":
        rows.append(dict(main_row))
    else:
        rows[rows.index(main_row)] = {**main_row, "sha256": "f" * 64}
    drifted = replace(materialized, input_artifacts=tuple(rows))
    with pytest.raises(
        execution.FactorialExecutionError,
        match="readiness or main config",
    ):
        execution._launch_receipt(
            spec,
            drifted,
            anchor_ns=7_000_000_000,
            manifest_sha256="a" * 64,
            plan_sha256="b" * 64,
            runtime_sha256="c" * 64,
        )


@pytest.mark.parametrize("mutation", ("missing", "false", "duplicate"))
def test_v40_receipt_rejects_self_consistent_reporter_v2_config_drift(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    _slot, spec, materialized, input_artifacts, slot_directory = (
        _materialized_repair(plan, tmp_path)
    )
    main_path = slot_directory / "runtime/main.conf"
    payload = main_path.read_text()
    if mutation == "missing":
        payload = payload.replace(REPORTER_V2_CONFIG_LINE + "\n", "")
    elif mutation == "false":
        payload = payload.replace(
            REPORTER_V2_CONFIG_LINE,
            f"{REPORTER_V2_CONFIG_KEY} = false",
        )
    else:
        payload += REPORTER_V2_CONFIG_LINE + "\n"
    main_path.write_text(payload)
    encoded = main_path.read_bytes()
    rows = tuple(
        {
            **row,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "size_bytes": len(encoded),
        }
        if row["relative_path"] == "runtime/main.conf"
        else row
        for row in input_artifacts
    )
    with pytest.raises(
        execution.FactorialExecutionError,
        match="readiness or main config",
    ):
        execution._launch_receipt(
            spec,
            replace(materialized, input_artifacts=rows),
            anchor_ns=7_000_000_000,
            manifest_sha256="a" * 64,
            plan_sha256="b" * 64,
            runtime_sha256="c" * 64,
        )


@pytest.mark.parametrize(
    ("target", "mutation"),
    (
        ("manager", "missing_eligibility"),
        ("manager", "missing_retention"),
        ("manager", "tampered_actors"),
        ("config", "missing_v2"),
        ("config", "false_v2"),
        ("config", "duplicate_v2"),
    ),
)
def test_v40_rejects_readiness_argv_drift(
    target: str,
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    repair = _coverage(plan).runtimes[1]
    candidate = repair
    if target == "manager":
        argv = list(repair.manager_argv_template.argv)
        if mutation == "missing_eligibility":
            argv.remove(ELIGIBILITY_OPTION)
        elif mutation == "missing_retention":
            argv.remove(RETENTION_OPTION)
        else:
            argv[argv.index(RESPONSIVE_ACTORS_OPTION) + 1] = "1,7,8"
        candidate = replace(
            repair,
            manager_argv_template=replace(repair.manager_argv_template, argv=tuple(argv)),
        )
    else:
        lines = list(repair.main_config.lines)
        if mutation == "missing_v2":
            lines.remove(REPORTER_V2_CONFIG_LINE)
        elif mutation == "false_v2":
            lines[lines.index(REPORTER_V2_CONFIG_LINE)] = (
                f"{REPORTER_V2_CONFIG_KEY} = false"
            )
        else:
            lines.append(REPORTER_V2_CONFIG_LINE)
        candidate = replace(
            repair,
            main_config=replace(repair.main_config, lines=tuple(lines)),
        )
    with pytest.raises(manifest_module.FactorialManifestError):
        runtime_module.materialize_manager_argv(
            candidate,
            tmp_path / candidate.slot_id,
            _secrets(candidate.replica_count),
            shared_raw_clock_anchor_ns=7_000_000_000,
        )


def test_v39_history_keeps_v4_330_and_no_v40_readiness_flags() -> None:
    manifest = manifest_module.load_frozen_manifest(V39_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    repair = _coverage(plan).runtimes[1]
    probe = repair.excluded_repair_smoke_probe
    assert probe is not None
    assert repair.fault_window.duration_s == 330
    assert probe.observation_contract == manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
    assert probe.semantic_delta == runtime_module.EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V3
    assert ELIGIBILITY_OPTION not in repair.manager_argv_template.argv
    assert RETENTION_OPTION not in repair.manager_argv_template.argv
    assert RESPONSIVE_ACTORS_OPTION not in repair.manager_argv_template.argv
    assert REPORTER_V2_CONFIG_LINE not in repair.main_config.lines


def test_v39_launch_receipt_omits_v40_main_config_identity(
    tmp_path: Path,
) -> None:
    manifest = manifest_module.load_frozen_manifest(V39_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    _slot, spec, materialized, _input_artifacts, _slot_directory = (
        _materialized_repair(plan, tmp_path)
    )

    receipt = execution._launch_receipt(
        spec,
        materialized,
        anchor_ns=7_000_000_000,
        manifest_sha256="a" * 64,
        plan_sha256="b" * 64,
        runtime_sha256="c" * 64,
    )
    assert "main_config_sha256" not in receipt


def test_v39_v40_ordered_runtime_cross_binding_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, v40_plan = _candidate_plan(monkeypatch)
    v40_repair = _coverage(v40_plan).runtimes[1]
    v39_manifest = manifest_module.load_frozen_manifest(V39_MANIFEST)
    v39_repair = _coverage(
        manifest_module.build_factorial_plan(v39_manifest)
    ).runtimes[1]
    with pytest.raises(execution.FactorialExecutionError, match="runtime binding drifted"):
        execution._uses_exact_excluded_repair_smoke_bound(
            v40_repair,
            _coverage_binding("v39"),
        )
    with pytest.raises(execution.FactorialExecutionError, match="runtime binding drifted"):
        execution._uses_exact_excluded_repair_smoke_bound(
            v39_repair,
            _coverage_binding("v40"),
        )


def test_v40_rejects_schema_v2_config_injection_outside_exact_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    primary = _coverage(plan).runtimes[0]
    candidate = replace(
        primary,
        main_config=replace(
            primary.main_config,
            lines=(*primary.main_config.lines, REPORTER_V2_CONFIG_LINE),
        ),
    )
    with pytest.raises(manifest_module.FactorialManifestError):
        runtime_module.materialize_manager_argv(
            candidate,
            tmp_path / candidate.slot_id,
            _secrets(candidate.replica_count),
            shared_raw_clock_anchor_ns=7_000_000_000,
        )


@pytest.mark.parametrize(("offset_ns", "passes"), ((-1, True), (0, False), (1, False)))
def test_v40_runner_terminal_is_strictly_before_hard_deadline(
    offset_ns: int,
    passes: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    repair = _coverage(plan).runtimes[1]
    anchor_ns = 7_000_000_000
    terminal_ns = anchor_ns + 650 * 1_000_000_000 + offset_ns
    if passes:
        execution._assert_v40_repair_runner_terminal_before_hard_deadline(
            repair,
            shared_raw_clock_anchor_ns=anchor_ns,
            runner_terminal_recorded_monotonic_ns=terminal_ns,
        )
    else:
        with pytest.raises(
            execution.IncompleteFactorialSlot,
            match="runner terminal must be strictly before the shared hard deadline",
        ):
            execution._assert_v40_repair_runner_terminal_before_hard_deadline(
                repair,
                shared_raw_clock_anchor_ns=anchor_ns,
                runner_terminal_recorded_monotonic_ns=terminal_ns,
            )


def test_v40_runner_terminal_rejects_zero_raw_clock_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    repair = _coverage(plan).runtimes[1]

    with pytest.raises(
        execution.FactorialExecutionError,
        match="positive monotonic timestamps",
    ):
        execution._assert_v40_repair_runner_terminal_before_hard_deadline(
            repair,
            shared_raw_clock_anchor_ns=7_000_000_000,
            runner_terminal_recorded_monotonic_ns=0,
        )


def test_v40_terminal_production_path_persists_zero_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    repair = _coverage(plan).runtimes[1]
    samples: list[int] = []
    states: list[tuple[str, dict[str, object]]] = []
    history: list[dict[str, object]] = [
        {"sequence": 0, "state": "NOT_STARTED", "reason": None}
    ]

    def now() -> int:
        samples.append(0)
        return 0

    outcome, reason, recorded = execution._finalize_runner_terminal(
        repair,
        coverage_binding=_coverage_binding("v40"),
        shared_raw_clock_anchor_ns=7_000_000_000,
        runtime_error=None,
        cleanup_error=None,
        raw_now_ns=now,
        history=history,
        state_writer=lambda phase, **fields: states.append((phase, fields)),
    )

    assert samples == [0]
    assert recorded == 0
    assert outcome == "INCOMPLETE"
    assert reason == "runner terminal requires a positive monotonic timestamp"
    assert history[-1] == {
        "sequence": 1,
        "state": "INCOMPLETE",
        "reason": reason,
    }
    assert states == [
        (
            "terminal",
            {
                "recorded_monotonic_ns": 0,
                "outcome": "INCOMPLETE",
                "reason": reason,
            },
        )
    ]


@pytest.mark.parametrize(("offset_ns", "expected"), ((-1, "PASS"), (0, "INCOMPLETE"), (1, "INCOMPLETE")))
def test_v40_terminal_production_path_samples_once_and_persists_same_timestamp(
    offset_ns: int,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_plan(monkeypatch)
    repair = _coverage(plan).runtimes[1]
    anchor_ns = 7_000_000_000
    terminal_ns = anchor_ns + 650 * 1_000_000_000 + offset_ns
    samples: list[int] = []
    states: list[tuple[str, dict[str, object]]] = []
    history: list[dict[str, object]] = [
        {"sequence": 0, "state": "NOT_STARTED", "reason": None}
    ]

    def now() -> int:
        samples.append(terminal_ns)
        return terminal_ns

    def state(phase: str, **fields: object) -> None:
        states.append((phase, fields))

    outcome, reason, recorded = execution._finalize_runner_terminal(
        repair,
        coverage_binding=_coverage_binding("v40"),
        shared_raw_clock_anchor_ns=anchor_ns,
        runtime_error=None,
        cleanup_error=None,
        raw_now_ns=now,
        history=history,
        state_writer=state,
    )
    assert samples == [terminal_ns]
    assert recorded == terminal_ns
    assert outcome == expected
    assert history[-1]["state"] == expected
    assert states == [
        (
            "terminal",
            {
                "recorded_monotonic_ns": terminal_ns,
                "outcome": expected,
                "reason": reason,
            },
        )
    ]
    if expected == "PASS":
        assert reason is None
    else:
        assert reason == (
            "runner terminal must be strictly before the shared hard deadline"
        )


def test_v41_is_default_and_v39_is_validation_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v46.json"
    assert cli.main(["--manifest", str(V39_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v45 are validation-only" in refusal["reason"]
