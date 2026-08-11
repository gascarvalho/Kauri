"""Prospective producer contract for the v38 commit-evidence roll."""

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
V38_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v38.json"
)
V37_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v37.json"
)
EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3 = (
    "exact_excluded_repair_smoke_source_fault_window_duration_450s_effective_"
    "fault_window_duration_330s_hard_deadline_650s_with_fault_evidence_and_"
    "epoch1_stable_as_the_only_fault_active_causal_phases_with_epoch1_selection_"
    "terminal_all_replica_command_activation_and_stable_end_before_fault_end_"
    "then_epoch2_as_post_fault_recovery_and_stability_with_fault_end_before_"
    "cycle1_selection_then_epoch2_terminal_all_replica_command_activation_"
    "stable_end_and_drain_before_shared_hard_deadline_v3"
)
EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2 = (
    "byzantine.window.duration_s:450->330"
)
POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V2 = (
    "exact_v38_legal_qc_skipped_ancestor_without_authenticated_exact_identity_"
    "source_authoritative_commit_identity_absence_strictly_after_final_cycle_"
    "successor_converged_terminal_is_scoped_only_when_each_gap_has_one_same_"
    "source_native_marker_every_replica_has_exactly_one_matching_commit_observed_"
    "and_at_least_derived_q_distinct_source_bound_rich_block_committed_proofs_"
    "match_height_hash_parent_transaction_count_commit_batch_index_epoch_tree_"
    "digest_and_view_generation_non_designated_gaps_require_the_designated_"
    "observer_rich_proof_while_at_most_one_designated_observer_gap_is_permitted_"
    "only_for_zero_transactions_with_exact_height_adjacent_designated_observer_"
    "rich_predecessor_and_successor_parent_chain_and_configuration_generation_"
    "closure_every_positive_transaction_designated_observer_observation_remains_"
    "complete_without_synthesizing_commit_evidence_or_changing_consensus_or_"
    "transaction_throughput_authority_v2"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V38_MANIFEST.read_bytes()
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


def test_v38_profile_is_one_lf_and_exact_three_path_delta() -> None:
    v38_payload = V38_MANIFEST.read_bytes()
    v37_payload = V37_MANIFEST.read_bytes()

    assert v38_payload.endswith(b"\n")
    assert not v38_payload.endswith(b"\n\n")
    assert v38_payload.count(b"shape-placement-factorial-v38") == 2
    v38 = json.loads(v38_payload)
    v37 = json.loads(v37_payload)
    assert v38.pop("manifest_id") == "shape-placement-factorial-v38"
    assert v37.pop("manifest_id") == "shape-placement-factorial-v37"
    assert v38["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v38"
    )
    assert v37["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v37"
    )
    v38_responsive = v38["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v37_responsive = v37["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert v38_responsive.pop(
        "post_final_convergence_unmatched_commit_evidence_contract"
    ) == POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V2
    assert (
        manifest_module.POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V2
        == POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V2
    )
    assert v37_responsive.pop(
        "post_final_convergence_unmatched_commit_evidence_contract"
    ) == manifest_module.POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
    assert v38 == v37


def test_v37_v38_v39_identities_are_historical() -> None:
    assert (
        getattr(manifest_module, "V37_MANIFEST_ID", None),
        getattr(manifest_module, "V37_MANIFEST_SHA256", None),
        getattr(manifest_module, "V37_SEMANTIC_SHA256", None),
        getattr(manifest_module, "V37_PLAN_SHA256", None),
        getattr(runtime_module, "V37_RUNTIME_SHA256", None),
        getattr(runtime_module, "V37_SMOKE_RUNTIME_SHA256", None),
        getattr(runtime_module, "V37_COVERAGE_SMOKE_RUNTIME_SHA256", None),
    ) == (
        "shape-placement-factorial-v37",
        "a926192d3c6a5129ea8304504317f921a8a1a681b489e1503e715dcbd3e8be11",
        "63efea700bd3a0fa3d59b7876602e3a5f99a3d71edb4e57ed218a8e6ea4bec8c",
        "e413ff98733b5e462b058018ce25ca4c77013a760fffe730dcbfd50150379e36",
        "a05f26983a34f626f95d326847db7a049a05c2258fdadf3bbaddd4ec3850c5d7",
        "1eda50b8e2887ab4d0f1763816f82344136dabf480d752f5a4d82f48272e8f63",
        "7aa68f246e06bee0a666734113e5a5bda7a747c912b3cc51db6f81d03b2a6d81",
    )
    assert (
        manifest_module.V38_MANIFEST_ID,
        manifest_module.V38_MANIFEST_SHA256,
        manifest_module.V38_SEMANTIC_SHA256,
        manifest_module.V38_PLAN_SHA256,
        runtime_module.V38_RUNTIME_SHA256,
        runtime_module.V38_SMOKE_RUNTIME_SHA256,
        runtime_module.V38_COVERAGE_SMOKE_RUNTIME_SHA256,
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


def test_v38_preserves_exact_runtime_timing_and_repair_330(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, plan, coverage = _candidate_coverage(monkeypatch)
    campaign_runtime = runtime_module.build_factorial_runtime(plan)
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])

    assert manifest.common_timers.aggregation_timeout_ms_per_depth == 125
    assert manifest.common_timers.leader_progress_timeout_ms == 20_000
    assert manifest.common_timers.transition_convergence_deadline_s == 30
    assert manifest.common_timers.hard_timeout_s == 650
    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    assert responsive.excluded_repair_smoke_observation_contract == (
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
    )
    assert responsive.post_final_convergence_unmatched_commit_evidence_contract == (
        POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V2
    )
    assert all(slot.byzantine.duration_s == 450 for slot in plan.slots)
    assert all(slot.fault_window.duration_s == 450 for slot in campaign_runtime.slots)
    assert all(slot.fault_window.hard_timeout_s == 650 for slot in campaign_runtime.slots)
    assert all(
        slot.excluded_repair_smoke_probe is None
        and all(
            runtime_module.EXCLUDED_REPAIR_SMOKE_PROBE_OPTION not in process.argv
            for process in slot.replica_argv_templates
        )
        for slot in campaign_runtime.slots
    )
    assert n7.slot.byzantine.duration_s == 450
    assert n7.runtime.fault_window.duration_s == 450
    assert n7.runtime.excluded_repair_smoke_probe is None

    primary_runtime, repair_runtime = coverage.runtimes
    assert primary_runtime.slot_id == "slot-066-n31-f5-b05-P"
    assert primary_runtime.fault_window.duration_s == 450
    assert primary_runtime.excluded_repair_smoke_probe is None
    assert repair_runtime.slot_id == "slot-037-n31-f2-b04-00"
    assert coverage.slots[1].byzantine.duration_s == 330
    assert repair_runtime.fault_window.duration_s == 330
    assert repair_runtime.fault_window.hard_timeout_s == 650
    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    assert probe.source_fault_window_duration_s == 450
    assert probe.effective_fault_window_duration_s == 330
    assert probe.hard_timeout_s == 650
    assert probe.semantic_delta == EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2
    assert probe.observation_contract == EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
    assert all(
        process.argv.count(runtime_module.EXCLUDED_REPAIR_SMOKE_PROBE_OPTION) == 1
        and process.argv.count(runtime_module.EXCLUDED_REPAIR_SMOKE_PROBE_MODE_V1)
        == 1
        for process in repair_runtime.replica_argv_templates
    )


def test_v37_history_preserves_v1_commit_contract_and_330_repair() -> None:
    manifest = manifest_module.load_frozen_manifest(V37_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    coverage = _coverage(plan)
    primary_runtime, repair_runtime = coverage.runtimes

    assert manifest.manifest_id == manifest_module.V37_MANIFEST_ID
    assert manifest.manifest_sha256 == manifest_module.V37_MANIFEST_SHA256
    assert plan.plan_sha256 == manifest_module.V37_PLAN_SHA256
    assert primary_runtime.fault_window.duration_s == 450
    assert repair_runtime.fault_window.duration_s == 330
    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    assert responsive.post_final_convergence_unmatched_commit_evidence_contract == (
        manifest_module.POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
    )
    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    assert probe.semantic_delta == runtime_module.EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2
    assert probe.effective_fault_window_duration_s == 330
    assert probe.observation_contract == EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3


def test_v37_v1_and_v38_v2_contracts_thread_into_every_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v37_manifest = manifest_module.load_frozen_manifest(V37_MANIFEST)
    v37_plan = manifest_module.build_factorial_plan(v37_manifest)
    v38_manifest, v38_plan = _candidate_plan(monkeypatch)

    for plan, expected in (
        (
            v37_plan,
            manifest_module.POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1,
        ),
        (v38_plan, POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V2),
    ):
        campaign = runtime_module.build_factorial_runtime(plan)
        n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
        coverage = _coverage(plan)
        assert all(
            slot.causal_acceptance.post_final_convergence_unmatched_commit_evidence_contract
            == expected
            for slot in campaign.slots
        )
        assert (
            n7.runtime.causal_acceptance.post_final_convergence_unmatched_commit_evidence_contract
            == expected
        )
        assert all(
            slot.causal_acceptance.post_final_convergence_unmatched_commit_evidence_contract
            == expected
            for slot in coverage.runtimes
        )


@pytest.mark.parametrize(
    ("manifest_path", "replacement", "semantic_name"),
    (
        (
            V38_MANIFEST,
            manifest_module.POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1,
            "FROZEN_SEMANTIC_SHA256",
        ),
        (
            V37_MANIFEST,
            POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V2,
            "V37_SEMANTIC_SHA256",
        ),
    ),
)
def test_v37_v38_reject_cross_version_contract_tokens(
    manifest_path: Path,
    replacement: str,
    semantic_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(manifest_path.read_bytes())
    document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        "post_final_convergence_unmatched_commit_evidence_contract"
    ] = replacement
    payload = _canonical(document)
    monkeypatch.setattr(
        manifest_module,
        semantic_name,
        hashlib.sha256(payload).hexdigest(),
    )
    with pytest.raises(
        manifest_module.FactorialManifestError,
        match="post-final-convergence unmatched commit evidence contract drifted",
    ):
        manifest_module.parse_manifest_bytes(payload)


@pytest.mark.parametrize("duration_s", (300, 329, 331, 360, 450))
def test_v38_rejects_tampered_repair_duration(
    duration_s: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coverage = _candidate_coverage(monkeypatch)
    repair_slot = coverage.slots[1]
    repair_runtime = coverage.runtimes[1]
    probe = repair_runtime.excluded_repair_smoke_probe
    assert probe is not None
    tampered_slot = replace(
        repair_slot,
        byzantine=replace(repair_slot.byzantine, duration_s=duration_s),
    )
    tampered_probe = replace(
        probe,
        effective_fault_window_duration_s=duration_s,
    )
    with pytest.raises(manifest_module.FactorialManifestError):
        runtime_module.build_slot_runtime(
            tampered_slot,
            excluded_repair_smoke_probe=tampered_probe,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        (
            "semantic_delta",
            runtime_module.EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V1,
        ),
        (
            "observation_contract",
            manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2,
        ),
        ("source_fault_window_duration_s", 330),
        ("hard_timeout_s", 649),
    ),
)
def test_v38_rejects_tampered_repair_probe_contract(
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coverage = _candidate_coverage(monkeypatch)
    repair_slot = coverage.slots[1]
    probe = coverage.runtimes[1].excluded_repair_smoke_probe
    assert probe is not None
    with pytest.raises(manifest_module.FactorialManifestError):
        runtime_module.build_slot_runtime(
            repair_slot,
            excluded_repair_smoke_probe=replace(probe, **{field: value}),
        )


def test_v37_and_v38_exact_static_bindings_reject_cross_version_mix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v37_manifest = manifest_module.load_frozen_manifest(V37_MANIFEST)
    v37_plan = manifest_module.build_factorial_plan(v37_manifest)
    v37_coverage = _coverage(v37_plan)
    _, v38_plan, v38_coverage = _candidate_coverage(monkeypatch)
    v37_artifacts = _static_artifacts(V37_MANIFEST, v37_plan, v37_coverage)
    v38_artifacts = _static_artifacts(V38_MANIFEST, v38_plan, v38_coverage)

    execution._bind_static_artifacts(
        v37_coverage.slots[1],
        v37_coverage.runtimes[1],
        v37_artifacts,
        campaign_member=False,
    )
    execution._bind_static_artifacts(
        v38_coverage.slots[1],
        v38_coverage.runtimes[1],
        v38_artifacts,
        campaign_member=False,
    )
    for slot, runtime, artifacts in (
        (v38_coverage.slots[1], v38_coverage.runtimes[1], v37_artifacts),
        (v37_coverage.slots[1], v37_coverage.runtimes[1], v38_artifacts),
        (v38_coverage.slots[1], v37_coverage.runtimes[1], v38_artifacts),
        (v37_coverage.slots[1], v38_coverage.runtimes[1], v37_artifacts),
    ):
        with pytest.raises(execution.FactorialExecutionError):
            execution._bind_static_artifacts(
                slot,
                runtime,
                artifacts,
                campaign_member=False,
            )


def test_v39_is_default_and_v38_is_validation_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v46.json"
    assert cli.main(["--manifest", str(V38_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v45 are validation-only" in refusal["reason"]
