"""Producer contract for the frozen v43 static-artifact binding repair."""

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
V43_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v43.json"
)
V42_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v42.json"
)
V42_SIX = (
    "c88ad70de33e8f6d6b822f9b6e3dc6b50d51154a901666e542f582826217c48e",
    "617d74fa3b1043924b75b49d6abe75608c58fdcc29387ec2328a6daa527e7e01",
    "e1cb55c74215d6b2fb93f0212d0ce42c699dfc9d4afb927b2986a853d7a48826",
    "4239c82b2d4c67676c30765357a7a27404d15865add450ed798cc7c89ca37ea0",
    "2c699550b483316e7309de6cf2f74262462277fcf113dcbc21ed29d27827a4c9",
    "7ac3945187626a337f2c2866784400ae012b879bbcffd311efe6cb4a30764458",
)
V43_SIX = (
    "2a50d4d50b8518d50c1c2b40695ef74f82b7cec1d7f1e0ef8a54311468c119da",
    "23e07694e85686bbfbbe91cbe76fcb01d7a8ebf1009ad2747036af0956167347",
    "4ce4cb5e699c3fc87ec988755e9be73a1fec68edc71154faf050c64bed5f9366",
    "eab2f59024b777435b1713ec279adca86c8ea1ec18197da24c90f4480a441764",
    "48f966751d6da4fea160ed9787c560df8768ef9893d2e262d56a16af66ccee8c",
    "0ba89d2ba5e07db1a7adb03258330c527d95d1240ba7a1598686de3474541809",
)
CAMPAIGN_SCOPE = "factorial_campaign_v1"
S066_SCOPE = "ordered_s066_coverage_v1"
S037_SCOPE = "excluded_repair_s037_v1"
AGGREGATE_POLICY = "aggregate_relay_per_actor_v1"
LEGACY_POLICY = "one_per_actor_with_global_aggregate_v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_v43_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V43_MANIFEST.read_bytes()
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_SEMANTIC_SHA256",
        hashlib.sha256(_canonical(json.loads(payload))).hexdigest(),
    )
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


def test_v43_profile_is_exact_two_path_delta_from_v42() -> None:
    v43_payload = V43_MANIFEST.read_bytes()
    v42_payload = V42_MANIFEST.read_bytes()
    assert v43_payload.endswith(b"\n")
    assert not v43_payload.endswith(b"\n\n")
    assert v43_payload.count(b"shape-placement-factorial-v43") == 2
    v43 = json.loads(v43_payload)
    v42 = json.loads(v42_payload)
    assert v43.pop("manifest_id") == "shape-placement-factorial-v43"
    assert v42.pop("manifest_id") == "shape-placement-factorial-v42"
    assert v43["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v43"
    )
    assert v42["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v42"
    )
    assert v43 == v42


def test_v42_is_historical_and_v43_is_exactly_frozen() -> None:
    assert manifest_module.V42_MANIFEST_ID == "shape-placement-factorial-v42"
    assert (
        manifest_module.V42_MANIFEST_SHA256,
        manifest_module.V42_SEMANTIC_SHA256,
        manifest_module.V42_PLAN_SHA256,
        runtime_module.V42_RUNTIME_SHA256,
        runtime_module.V42_SMOKE_RUNTIME_SHA256,
        runtime_module.V42_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == V42_SIX
    assert manifest_module.FROZEN_MANIFEST_ID == "shape-placement-factorial-v43"
    assert (
        manifest_module.FROZEN_MANIFEST_SHA256,
        manifest_module.FROZEN_SEMANTIC_SHA256,
        manifest_module.FROZEN_PLAN_SHA256,
        runtime_module.FROZEN_RUNTIME_SHA256,
        runtime_module.FROZEN_SMOKE_RUNTIME_SHA256,
        runtime_module.FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == V43_SIX


def test_v42_six_identities_still_rehash_exactly() -> None:
    payload = V42_MANIFEST.read_bytes()
    manifest = manifest_module.load_frozen_manifest(V42_MANIFEST)
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
    ) == V42_SIX


def test_v43_preserves_v42_retention_scopes_and_static_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_v43_plan(monkeypatch)
    campaign = runtime_module.build_factorial_runtime(plan)
    assert len(campaign.slots) == 68
    assert {
        spec.cycle1_responsive_cross_commit_retention.scope
        for spec in campaign.slots
    } == {CAMPAIGN_SCOPE}
    assert {
        spec.cycle1_responsive_cross_commit_retention.admission_policy
        for spec in campaign.slots
    } == {AGGREGATE_POLICY}

    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    assert smoke.runtime.result_path.startswith(
        "results/shape-placement-factorial-v43-smoke/"
    )
    assert smoke.runtime.cycle1_responsive_cross_commit_retention is None

    coverage = _coverage(plan)
    primary, repair = coverage.runtimes
    assert primary.result_path.startswith(
        "results/shape-placement-factorial-v43-coverage-smoke/"
    )
    assert repair.result_path.startswith(
        "results/shape-placement-factorial-v43-coverage-smoke/"
    )
    assert (
        primary.cycle1_responsive_cross_commit_retention.scope,
        primary.cycle1_responsive_cross_commit_retention.admission_policy,
    ) == (S066_SCOPE, AGGREGATE_POLICY)
    assert (
        repair.cycle1_responsive_cross_commit_retention.scope,
        repair.cycle1_responsive_cross_commit_retention.admission_policy,
    ) == (S037_SCOPE, LEGACY_POLICY)
    assert repair.excluded_repair_smoke_probe.observation_contract == (
        manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V6
    )
    assert repair.excluded_repair_smoke_probe.semantic_delta == (
        runtime_module.EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V5
    )

    artifacts = {
        "manifest.json": V43_MANIFEST.read_bytes(),
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


def test_v43_repair_terminal_rejects_v42_result_path_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_v43_plan(monkeypatch)
    repair = _coverage(plan).runtimes[1]
    aliased = replace(
        repair,
        result_path=(
            "results/shape-placement-factorial-v42-coverage-smoke/"
            "slot-037-n31-f2-b04-00"
        ),
    )

    with pytest.raises(
        execution.FactorialExecutionError,
        match="exact v40/v41/v42/v43 repair runtime",
    ):
        execution._assert_v40_repair_runner_terminal_before_hard_deadline(
            aliased,
            shared_raw_clock_anchor_ns=7_000_000_000,
            runner_terminal_recorded_monotonic_ns=8_000_000_000,
        )


@pytest.mark.parametrize(
    ("source_version", "alias_version"),
    (("v42", "v43"), ("v43", "v42")),
)
def test_n7_static_binding_rejects_cross_version_result_path_alias(
    source_version: str,
    alias_version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if source_version == "v43":
        _, plan = _candidate_v43_plan(monkeypatch)
        manifest_path = V43_MANIFEST
    else:
        plan = manifest_module.build_factorial_plan(
            manifest_module.load_frozen_manifest(V42_MANIFEST)
        )
        manifest_path = V42_MANIFEST
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


def test_v43_is_default_and_v42_is_validation_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST == V43_MANIFEST
    assert cli.main(["--manifest", str(V42_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v42 are validation-only" in refusal["reason"]
