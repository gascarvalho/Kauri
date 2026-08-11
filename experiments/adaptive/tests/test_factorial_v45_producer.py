"""Producer contract for the behavior-preserving v45 rollover."""

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
V45_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v45.json"
)
V44_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v44.json"
)
V44_SIX = (
    "eaf0d7e756c3ed54a4ebbf2ebc10701494829f315bed4efb7c021ae14f8f93c2",
    "69db86cf3573bbf153150e1208c6bb76e791fb9f1642430797ffa6daeeff6498",
    "91725b2c029407b53247447c7c46c82e69a8025d9043907412adcde7d4a2ef80",
    "fa6cb7313c58aaa45a859d3193d499ebc0fd811253ae19350ac5af8bc21065a5",
    "17bb2b9be77edc679be895beabcd06e9714bf72ae39c4e41d2c3a38ac70fcee0",
    "5508460d3488e830e43c6898c5396dcffa0478c8f98d0035f61571cd708d66fa",
)
V45_SIX = (
    "aab7a4f9155c3a0a25fb4254a1ace9561fd2e82e7b841e18d42fca78ae578b73",
    "6d5308d42d3a84746bc7169156a9ef756a7d4acd282c2dab9c53e62a9070841e",
    "785057ccebe1dbdd8185f2710374558ef41ffa5e1d60c1b4d6094df8909becdf",
    "b60c39867611e29f0a71fc13de903baac03a99a0069cb6efda659d697b9c2636",
    "1233efc18d8c10e04e0d6e82a5ab9fdd85b8f87023d68f3202aa9fa526bf9dad",
    "6d757b20042bd14d915f05f4eea2bc998655778bcb6b20ba2e934b9649e4f4f3",
)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
        + b"\n"
    )


def _candidate_v45(monkeypatch: pytest.MonkeyPatch):
    payload = V45_MANIFEST.read_bytes()
    semantic_sha256 = hashlib.sha256(_canonical(json.loads(payload))).hexdigest()
    monkeypatch.setattr(
        manifest_module, "FROZEN_SEMANTIC_SHA256", semantic_sha256
    )
    manifest = manifest_module.parse_manifest_bytes(payload)
    plan = manifest_module.build_factorial_plan(manifest)
    monkeypatch.setattr(
        manifest_module, "FROZEN_MANIFEST_SHA256", manifest.manifest_sha256
    )
    monkeypatch.setattr(manifest_module, "FROZEN_PLAN_SHA256", plan.plan_sha256)
    return manifest, plan, runtime_module.build_factorial_runtime(plan)


def _coverage(plan):
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    return execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )


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


def test_v45_profile_is_exact_two_field_delta_from_v44() -> None:
    assert V45_MANIFEST.is_file()
    v45_payload = V45_MANIFEST.read_bytes()
    v44_payload = V44_MANIFEST.read_bytes()
    assert v45_payload.endswith(b"\n")
    assert not v45_payload.endswith(b"\n\n")
    assert v45_payload.count(b"shape-placement-factorial-v45") == 2
    assert v45_payload == v44_payload.replace(
        b"shape-placement-factorial-v44",
        b"shape-placement-factorial-v45",
    )


def test_v45_rollover_surface_is_exactly_frozen() -> None:
    assert manifest_module.V44_MANIFEST_ID == "shape-placement-factorial-v44"
    assert manifest_module.FROZEN_MANIFEST_ID == "shape-placement-factorial-v45"
    assert (
        manifest_module.V44_MANIFEST_SHA256,
        manifest_module.V44_SEMANTIC_SHA256,
        manifest_module.V44_PLAN_SHA256,
        runtime_module.V44_RUNTIME_SHA256,
        runtime_module.V44_SMOKE_RUNTIME_SHA256,
        runtime_module.V44_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == V44_SIX
    assert (
        manifest_module.FROZEN_MANIFEST_SHA256,
        manifest_module.FROZEN_SEMANTIC_SHA256,
        manifest_module.FROZEN_PLAN_SHA256,
        runtime_module.FROZEN_RUNTIME_SHA256,
        runtime_module.FROZEN_SMOKE_RUNTIME_SHA256,
        runtime_module.FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == V45_SIX
    assert cli.DEFAULT_MANIFEST == V45_MANIFEST


def test_v44_all_six_identities_rehash_exactly() -> None:
    payload = V44_MANIFEST.read_bytes()
    manifest = manifest_module.load_frozen_manifest(V44_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    coverage = _coverage(plan)
    assert (
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(_canonical(json.loads(payload))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(runtime_module.canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(_canonical(smoke.runtime.as_document())).hexdigest(),
        hashlib.sha256(_canonical(coverage.runtime.as_document())).hexdigest(),
    ) == V44_SIX


def test_v45_preserves_v44_plan_and_runtime_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v44_manifest = manifest_module.load_frozen_manifest(V44_MANIFEST)
    v44_plan = manifest_module.build_factorial_plan(v44_manifest)
    v44_runtime = runtime_module.build_factorial_runtime(v44_plan)
    v45_manifest, v45_plan, v45_runtime = _candidate_v45(monkeypatch)

    v44_document = runtime_module.canonical_runtime_bytes(v44_runtime).decode()
    v45_document = runtime_module.canonical_runtime_bytes(v45_runtime).decode()
    normalized_v44 = (
        v44_document.replace(
            "shape-placement-factorial-v44", "shape-placement-factorial-VERSION"
        )
        .replace(v44_manifest.manifest_sha256, "MANIFEST_SHA256")
        .replace(v44_plan.plan_sha256, "PLAN_SHA256")
    )
    normalized_v45 = (
        v45_document.replace(
            "shape-placement-factorial-v45", "shape-placement-factorial-VERSION"
        )
        .replace(v45_manifest.manifest_sha256, "MANIFEST_SHA256")
        .replace(v45_plan.plan_sha256, "PLAN_SHA256")
    )
    assert normalized_v45 == normalized_v44
    assert len(v45_runtime.slots) == 68
    assert all(
        spec.cycle1_responsive_cross_commit_retention is not None
        and spec.cycle1_responsive_cross_commit_retention
        .online_readiness_admitted_observation_ids_contract
        == runtime_module.ONLINE_READINESS_ADMITTED_OBSERVATION_IDS_TRIGGER_CONTRACT_V1
        and spec.cycle1_responsive_cross_commit_retention
        .sealed_validation_causal_witness_selection_contract
        == runtime_module.SEALED_VALIDATION_CAUSAL_WITNESS_SELECTION_CONTRACT_V1
        for spec in v45_runtime.slots
    )


def test_v44_is_validation_only_before_any_result_claim(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--manifest", str(V44_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert refusal == {
        "reason": (
            "shape-placement-factorial-v1 through v44 are validation-only; "
            "production commands require shape-placement-factorial-v45"
        ),
        "status": "REJECT",
    }


def test_v45_s066_and_s037_bind_to_exact_static_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan, _ = _candidate_v45(monkeypatch)
    coverage = _coverage(plan)
    artifacts = {
        "manifest.json": V45_MANIFEST.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": _canonical(coverage.runtime.as_document()),
    }

    for slot, spec in zip(coverage.slots, coverage.runtimes, strict=True):
        execution._bind_static_artifacts(
            slot,
            spec,
            artifacts,
            campaign_member=False,
        )


def test_v45_n7_s066_and_s037_retention_partition_is_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan, _ = _candidate_v45(monkeypatch)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    coverage = _coverage(plan)
    s066, s037 = coverage.runtimes

    assert smoke.runtime.cycle1_responsive_cross_commit_retention is None

    s066_retention = s066.cycle1_responsive_cross_commit_retention
    assert s066_retention is not None
    assert (
        s066_retention.scope,
        s066_retention.observation_schema_version,
        s066_retention.responsive_degraded_actor_ids,
        s066_retention.admission_policy,
        s066_retention.online_readiness_admitted_observation_ids_contract,
        s066_retention.sealed_validation_causal_witness_selection_contract,
    ) == (
        runtime_module.ORDERED_S066_COVERAGE_RETENTION_SCOPE_V1,
        2,
        (1, 2, 3, 5, 7, 8, 16),
        runtime_module.AGGREGATE_RELAY_PER_ACTOR_ADMISSION_POLICY_V1,
        runtime_module.ONLINE_READINESS_ADMITTED_OBSERVATION_IDS_TRIGGER_CONTRACT_V1,
        runtime_module.SEALED_VALIDATION_CAUSAL_WITNESS_SELECTION_CONTRACT_V1,
    )

    s037_retention = s037.cycle1_responsive_cross_commit_retention
    assert s037_retention is not None
    assert (
        s037_retention.scope,
        s037_retention.observation_schema_version,
        s037_retention.responsive_degraded_actor_ids,
        s037_retention.admission_policy,
    ) == (
        runtime_module.EXCLUDED_REPAIR_S037_RETENTION_SCOPE_V1,
        2,
        (1, 7, 8, 12, 16, 19, 20),
        runtime_module.ONE_PER_ACTOR_WITH_GLOBAL_AGGREGATE_ADMISSION_POLICY_V1,
    )
    assert s037_retention.online_readiness_admitted_observation_ids_contract is None
    assert (
        s037_retention.sealed_validation_causal_witness_selection_contract is None
    )
    assert execution._uses_exact_excluded_repair_smoke_bound(
        s037,
        _coverage_binding("v45"),
    )


@pytest.mark.parametrize(("offset_ns", "passes"), ((-1, True), (0, False)))
def test_v45_s037_runner_terminal_is_strictly_before_hard_deadline(
    offset_ns: int,
    passes: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan, _ = _candidate_v45(monkeypatch)
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


@pytest.mark.parametrize(
    ("source_version", "alias_version"),
    (("v44", "v45"), ("v45", "v44")),
)
def test_v45_s066_binding_rejects_cross_version_result_path_alias(
    source_version: str,
    alias_version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if source_version == "v45":
        _, plan, _ = _candidate_v45(monkeypatch)
        manifest_path = V45_MANIFEST
    else:
        plan = manifest_module.build_factorial_plan(
            manifest_module.load_frozen_manifest(V44_MANIFEST)
        )
        manifest_path = V44_MANIFEST
    coverage = _coverage(plan)
    aliased_path = (
        f"results/shape-placement-factorial-{alias_version}-coverage-smoke/"
        "slot-066-n31-f5-b05-P"
    )
    aliased_slot = replace(coverage.slots[0], result_path=aliased_path)
    aliased_spec = replace(coverage.runtimes[0], result_path=aliased_path)
    artifacts = {
        "manifest.json": manifest_path.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": _canonical(aliased_spec.as_document()),
    }

    with pytest.raises(
        execution.FactorialExecutionError,
        match=(
            "not derivable from the exact frozen plan|"
            "slot/runtime derivation failed|"
            "runtime.json does not match the exact ordered N=31 "
            "coverage-smoke runtime document"
        ),
    ):
        execution._bind_static_artifacts(
            aliased_slot,
            aliased_spec,
            artifacts,
            campaign_member=False,
        )


@pytest.mark.parametrize(
    ("source_version", "alias_version"),
    (("v44", "v45"), ("v45", "v44")),
)
def test_v45_s037_binding_rejects_cross_version_contract_path_alias(
    source_version: str,
    alias_version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if source_version == "v45":
        _, plan, _ = _candidate_v45(monkeypatch)
        manifest_path = V45_MANIFEST
    else:
        plan = manifest_module.build_factorial_plan(
            manifest_module.load_frozen_manifest(V44_MANIFEST)
        )
        manifest_path = V44_MANIFEST
    coverage = _coverage(plan)
    aliased_path = (
        f"results/shape-placement-factorial-{alias_version}-coverage-smoke/"
        "slot-037-n31-f2-b04-00"
    )
    aliased_slot = replace(coverage.slots[1], result_path=aliased_path)
    aliased_spec = replace(coverage.runtimes[1], result_path=aliased_path)
    artifacts = {
        "manifest.json": manifest_path.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": _canonical(aliased_spec.as_document()),
    }

    with pytest.raises(
        execution.FactorialExecutionError,
        match=(
            "not derivable from the exact frozen plan|"
            "slot/runtime derivation failed"
        ),
    ):
        execution._bind_static_artifacts(
            aliased_slot,
            aliased_spec,
            artifacts,
            campaign_member=False,
        )


@pytest.mark.parametrize(
    ("source_version", "alias_version"),
    (("v44", "v45"), ("v45", "v44")),
)
def test_v45_n7_binding_rejects_cross_version_plan_result_path_alias(
    source_version: str,
    alias_version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if source_version == "v45":
        _, plan, _ = _candidate_v45(monkeypatch)
        manifest_path = V45_MANIFEST
    else:
        plan = manifest_module.build_factorial_plan(
            manifest_module.load_frozen_manifest(V44_MANIFEST)
        )
        manifest_path = V44_MANIFEST
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    aliased_slot = replace(
        smoke.slot,
        result_path=(
            f"results/shape-placement-factorial-{alias_version}-smoke/"
            "smoke-n7-f2-PS"
        ),
    )
    aliased_spec = runtime_module.build_slot_runtime(aliased_slot)
    artifacts = {
        "manifest.json": manifest_path.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": _canonical(aliased_spec.as_document()),
    }

    with pytest.raises(
        execution.FactorialExecutionError,
        match="not derivable from the exact frozen plan",
    ):
        execution._bind_static_artifacts(
            aliased_slot,
            aliased_spec,
            artifacts,
            campaign_member=False,
        )
