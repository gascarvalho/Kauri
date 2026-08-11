"""Prospective producer contract for the scoped-evidence v36 version roll."""

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
V36_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v36.json"
)
V35_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v35.json"
)
POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1 = (
    "exact_v36_non_designated_legal_qc_skipped_ancestor_without_authenticated_"
    "exact_identity_source_authoritative_commit_identity_absence_strictly_after_"
    "final_cycle_successor_converged_terminal_is_scoped_only_when_each_gap_has_"
    "one_same_source_native_marker_every_replica_has_exactly_one_matching_commit_"
    "observed_the_designated_observer_stream_is_complete_and_at_least_derived_q_"
    "distinct_source_bound_rich_block_committed_proofs_match_height_hash_parent_"
    "transaction_count_commit_batch_index_epoch_tree_digest_and_view_generation_"
    "without_synthesizing_commit_evidence_or_changing_consensus_or_throughput_"
    "authority_v1"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V36_MANIFEST.read_bytes()
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


def test_v36_profile_is_one_lf_and_exact_three_path_delta() -> None:
    v36_payload = V36_MANIFEST.read_bytes()
    v35_payload = V35_MANIFEST.read_bytes()

    assert v36_payload.endswith(b"\n")
    assert not v36_payload.endswith(b"\n\n")
    assert v36_payload.count(b"shape-placement-factorial-v36") == 2
    v36 = json.loads(v36_payload)
    v35 = json.loads(v35_payload)
    assert v36.pop("manifest_id") == "shape-placement-factorial-v36"
    assert v35.pop("manifest_id") == "shape-placement-factorial-v35"
    assert v36["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v36"
    )
    assert v35["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v35"
    )
    v36_responsive = v36["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v35_responsive = v35["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert v36_responsive.pop(
        "post_final_convergence_unmatched_commit_evidence_contract"
    ) == POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
    assert "post_final_convergence_unmatched_commit_evidence_contract" not in (
        v35_responsive
    )
    assert v36 == v35


def test_v35_identities_are_explicit_historical_aliases() -> None:
    assert (
        getattr(manifest_module, "V35_MANIFEST_ID", None),
        getattr(manifest_module, "V35_MANIFEST_SHA256", None),
        getattr(manifest_module, "V35_SEMANTIC_SHA256", None),
        getattr(manifest_module, "V35_PLAN_SHA256", None),
        getattr(runtime_module, "V35_RUNTIME_SHA256", None),
        getattr(runtime_module, "V35_SMOKE_RUNTIME_SHA256", None),
        getattr(runtime_module, "V35_COVERAGE_SMOKE_RUNTIME_SHA256", None),
    ) == (
        "shape-placement-factorial-v35",
        "28303487578594d7eac64aa1eda11ea891a85b8a6d6ff386d42e920dc8b82b95",
        "40422f2b8ac74e695fdfa18b687fce2b31b516f36bd2b6bfe51f53602bd4d0c8",
        "6c558653d0db5e5f646de58c1a853d656fedbd7e61257e13755ef01ea9d42025",
        "05ffcdc81d0cd0ec8a264cd0d5545e88fbf14dba1569629e6ec0f02c3a16615c",
        "785abe70a6500a66c331e00dd81eeada1065457ff4dace4e6f9035b9006aaee6",
        "4fa44f57128bc096c7fd40bbf9c054c184a273d07ca8f2bdc43fdda7c5e93ad2",
    )


def test_v36_six_identities_are_explicit_historical_aliases() -> None:
    assert manifest_module.V36_MANIFEST_ID == "shape-placement-factorial-v36"
    assert (
        manifest_module.V36_MANIFEST_SHA256,
        manifest_module.V36_SEMANTIC_SHA256,
        manifest_module.V36_PLAN_SHA256,
        runtime_module.V36_RUNTIME_SHA256,
        runtime_module.V36_SMOKE_RUNTIME_SHA256,
        runtime_module.V36_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "50761ebcd8693c33ca30257b3abee6f44f992481b6f10e57732d098b029073d1",
        "87cd28e7df12aeb9f54386623b207526ea71d096d3699d687dc07784219d63ed",
        "d5075db22099788a1c687ddc72cc4953a2d665fc1fa09104ba69ab128f91a65f",
        "5b088b4d3e2a0a484f2829664b94db6d51e32fb0e8a0bc904fdf342098a85c89",
        "3ad3f26401c190827591351b21578e2b3ec088f0222e8262f2cfa6f29a402c7a",
        "34df26b4aaff8c3f417d1634aa1e52e7f40551b8265c2830720456dc068acc81",
    )


def test_v35_exact_history_remains_loadable() -> None:
    manifest = manifest_module.load_frozen_manifest(V35_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    coverage = _coverage(plan)

    assert manifest.manifest_id == manifest_module.V35_MANIFEST_ID
    assert manifest.manifest_sha256 == manifest_module.V35_MANIFEST_SHA256
    assert plan.plan_sha256 == manifest_module.V35_PLAN_SHA256
    assert hashlib.sha256(
        runtime_module.canonical_runtime_bytes(runtime)
    ).hexdigest() == runtime_module.V35_RUNTIME_SHA256
    assert hashlib.sha256(
        execution._canonical_json_bytes(coverage.runtime.as_document())
    ).hexdigest() == runtime_module.V35_COVERAGE_SMOKE_RUNTIME_SHA256
    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    assert responsive.post_final_convergence_unmatched_commit_evidence_contract is None


def test_v36_threads_global_scope_without_timing_or_repair_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, plan, coverage = _candidate_coverage(monkeypatch)
    campaign_runtime = runtime_module.build_factorial_runtime(plan)
    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    assert (
        responsive.post_final_convergence_unmatched_commit_evidence_contract
        == POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
    )
    assert all(
        slot.byzantine.responsive_degradation is not None
        and slot.byzantine.responsive_degradation.post_final_convergence_unmatched_commit_evidence_contract
        == POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
        for slot in plan.slots
    )
    assert all(slot.byzantine.duration_s == 450 for slot in plan.slots)
    assert all(slot.common_timers.hard_timeout_s == 650 for slot in plan.slots)
    assert all(
        slot.common_timers.transition_convergence_deadline_s == 30
        for slot in plan.slots
    )
    assert all(
        runtime.causal_acceptance.post_final_convergence_unmatched_commit_evidence_contract
        == POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
        for runtime in campaign_runtime.slots
    )
    primary_runtime, repair_runtime = coverage.runtimes
    assert (
        primary_runtime.causal_acceptance.post_final_convergence_unmatched_commit_evidence_contract
        == POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
    )
    assert (
        repair_runtime.causal_acceptance.post_final_convergence_unmatched_commit_evidence_contract
        == POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
    )
    assert primary_runtime.fault_window.duration_s == 450
    assert repair_runtime.fault_window.duration_s == 300
    assert primary_runtime.fault_window.hard_timeout_s == 650
    assert repair_runtime.fault_window.hard_timeout_s == 650
    assert repair_runtime.excluded_repair_smoke_probe is not None
    assert repair_runtime.excluded_repair_smoke_probe.source_campaign_result_path == (
        "results/shape-placement-factorial-v36/slot-037-n31-f2-b04-00"
    )


def test_v36_contract_is_required_and_rejected_from_historical_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v36 = json.loads(V36_MANIFEST.read_bytes())
    responsive = v36["byzantine"]["responsive_degradation"]
    responsive.pop("post_final_convergence_unmatched_commit_evidence_contract")
    missing_payload = _canonical(v36)
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_SEMANTIC_SHA256",
        hashlib.sha256(missing_payload).hexdigest(),
    )
    with pytest.raises(
        manifest_module.FactorialManifestError,
        match="post-final-convergence unmatched commit evidence contract",
    ):
        manifest_module.parse_manifest_bytes(missing_payload)

    v35 = json.loads(V35_MANIFEST.read_bytes())
    v35["byzantine"]["responsive_degradation"][
        "post_final_convergence_unmatched_commit_evidence_contract"
    ] = POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
    with pytest.raises(
        manifest_module.FactorialManifestError,
        match="responsive-degradation contract fields are not frozen",
    ):
        manifest_module.parse_manifest_bytes(_canonical(v35))


def test_v35_and_v36_exact_static_bindings_pass_but_cross_binding_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v35_manifest = manifest_module.load_frozen_manifest(V35_MANIFEST)
    v35_plan = manifest_module.build_factorial_plan(v35_manifest)
    v35_coverage = _coverage(v35_plan)
    _, v36_plan, v36_coverage = _candidate_coverage(monkeypatch)
    v35_artifacts = _static_artifacts(V35_MANIFEST, v35_plan, v35_coverage)
    v36_artifacts = _static_artifacts(V36_MANIFEST, v36_plan, v36_coverage)

    execution._bind_static_artifacts(
        v35_coverage.slots[1],
        v35_coverage.runtimes[1],
        v35_artifacts,
        campaign_member=False,
    )
    execution._bind_static_artifacts(
        v36_coverage.slots[1],
        v36_coverage.runtimes[1],
        v36_artifacts,
        campaign_member=False,
    )
    with pytest.raises(execution.FactorialExecutionError):
        execution._bind_static_artifacts(
            v36_coverage.slots[1],
            v36_coverage.runtimes[1],
            v35_artifacts,
            campaign_member=False,
        )
    with pytest.raises(execution.FactorialExecutionError):
        execution._bind_static_artifacts(
            v35_coverage.slots[1],
            v35_coverage.runtimes[1],
            v36_artifacts,
            campaign_member=False,
        )
    with pytest.raises(execution.FactorialExecutionError):
        execution._bind_static_artifacts(
            v36_coverage.slots[1],
            v35_coverage.runtimes[1],
            v36_artifacts,
            campaign_member=False,
        )
    with pytest.raises(execution.FactorialExecutionError):
        execution._bind_static_artifacts(
            v35_coverage.slots[1],
            v36_coverage.runtimes[1],
            v35_artifacts,
            campaign_member=False,
        )


def test_v36_is_validation_only_after_the_v37_roll(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v40.json"
    assert cli.main(["--manifest", str(V36_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v39 are validation-only" in refusal["reason"]
