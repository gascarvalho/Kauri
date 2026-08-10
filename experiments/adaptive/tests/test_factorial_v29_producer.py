"""Prospective producer contract for the v29 repair-binding correction."""

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
V29_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v29.json"
)
V28_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v28.json"
)
PROBE_OPTION = "--experiment-response-evidence-duplicate-probe"
PROBE_MODE = "exact_once_post_fault_epoch1_responsive_internal_child_v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V29_MANIFEST.read_bytes()
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


def _candidate_coverage(monkeypatch: pytest.MonkeyPatch):
    manifest, plan = _candidate_plan(monkeypatch)
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    coverage = execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )
    return manifest, plan, coverage


def test_v29_profile_delta_is_only_identity_and_results_root() -> None:
    v29 = json.loads(V29_MANIFEST.read_bytes())
    v28 = json.loads(V28_MANIFEST.read_bytes())

    assert v29.pop("manifest_id") == "shape-placement-factorial-v29"
    assert v28.pop("manifest_id") == "shape-placement-factorial-v28"
    assert v29["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v29"
    )
    assert v28["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v28"
    )
    assert v29 == v28


def test_v28_identities_are_explicit_historical_aliases() -> None:
    assert (
        manifest_module.V28_MANIFEST_ID,
        manifest_module.V28_MANIFEST_SHA256,
        manifest_module.V28_SEMANTIC_SHA256,
        manifest_module.V28_PLAN_SHA256,
        runtime_module.V28_RUNTIME_SHA256,
        runtime_module.V28_SMOKE_RUNTIME_SHA256,
        runtime_module.V28_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "shape-placement-factorial-v28",
        "fda2a5e79ebd04e67d9e7db10675b5c31b6072803d986dad229ef6b115e0a659",
        "42ca63b1fb6d256852cd0f758fbf852b01b31c594705c0bb051b6bc18ca1f989",
        "1b038d38cf9879b32581068e266c521fd5368aa5e57d50e21f979c25b2148f76",
        "7925f0de66f0d7fd7412dfae3e9754dcbceb8d0516b875552f98b3f637225fba",
        "de9599ac4d22582fea1746eda8deb885e6bc9f2c1d79c9fd723de299cc0edb74",
        "af28c19dafb2f01461c5b3c3a648a7ac4f200a71880a965b2ff9da892792fa84",
    )


def test_v29_six_identities_are_exact_historical_values() -> None:
    assert manifest_module.V29_MANIFEST_ID == "shape-placement-factorial-v29"
    assert (
        manifest_module.V29_MANIFEST_SHA256,
        manifest_module.V29_SEMANTIC_SHA256,
        manifest_module.V29_PLAN_SHA256,
        runtime_module.V29_RUNTIME_SHA256,
        runtime_module.V29_SMOKE_RUNTIME_SHA256,
        runtime_module.V29_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == (
        "e012be15b6193263138de7b5aee5f78eb67de10182bc07e4494373d648fde5fa",
        "605992c544a12c3652f1279bb960325bc94fe7cd72ebb251d1c5b8e101701361",
        "8ca31c9d0ae26c7deedbb1932a161652f146e2ee6cb54bf08e9bf4ae5088bc91",
        "162531c501ba866b51033af411b5debb7d1e805c254d5a8fedcd29b59337a026",
        "5780c65662cbb1312f61f753e8d66237005ef666264bf99351b2d33f15d6680c",
        "4042c313856f722a5abef6e537a8a73de1699dfdde17da0d9ab2bc8a628c995a",
    )


def test_v29_preserves_campaign_timing_and_exact_repair_delta(
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
        "results/shape-placement-factorial-v29/slot-037-n31-f2-b04-00"
    )
    assert probe.semantic_delta == "byzantine.window.duration_s:450->300"
    assert probe.verified_response_duplicate_probe_mode == PROBE_MODE
    assert coverage.runtime.excluded_repair_smoke_probe == probe
    for process in repair_runtime.replica_argv_templates:
        assert process.argv.count(PROBE_OPTION) == 1
        assert process.argv[process.argv.index(PROBE_OPTION) + 1] == PROBE_MODE


def test_v29_repair_runtime_passes_exact_static_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan, coverage = _candidate_coverage(monkeypatch)
    artifacts = {
        "manifest.json": V29_MANIFEST.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": execution._canonical_json_bytes(coverage.runtime.as_document()),
    }

    execution._bind_static_artifacts(
        coverage.slots[1],
        coverage.runtimes[1],
        artifacts,
        campaign_member=False,
    )


def test_v29_is_validation_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST != V29_MANIFEST
    assert cli.main(["--manifest", str(V29_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v32 are validation-only" in refusal["reason"]
