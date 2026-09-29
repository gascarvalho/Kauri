from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "comparison" / "validator.py"
spec = importlib.util.spec_from_file_location("n7_path_matched_comparison_test", PATH)
assert spec and spec.loader
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def _manifest():
    manifest = comparison.load_manifest()
    manifest["manifest_sha256"] = comparison.manifest_digest(manifest)
    return manifest


def _outcome(manifest, pair_id: str, arm_id: str, *, status: str = "PASS"):
    base = {
        "pair_id": pair_id,
        "arm_id": arm_id,
        "status": status,
        "manifest_sha256": manifest["manifest_sha256"],
        "shared_inputs": deepcopy(manifest["shared_inputs"]),
    }
    if status != "PASS":
        return {**base, "terminal_validator": {"verdict": status, "reason": "retained synthetic terminal outcome"}}
    proof = {
        "authoritative_replica": 0,
        "block_height": 58,
        "block_hash": "a" * 64,
        "witness_replicas": [1, 2, 3, 4, 5, 6],
        "after_fault_window": True,
    }
    metric = {
        "anchor_monotonic_ns": 100,
        "horizon_end_monotonic_ns": 60_000_000_100,
        "authoritative_common_commit_count": 1,
        "maximum_inter_commit_gap_ns": None,
    }
    terminal = (
        {"verdict": "RAW_BUNDLE_VALIDATED", "selection_replicas": [1], "successor_activated": True, "authoritative_common_commit": proof, "fixed_horizon": metric}
        if arm_id == "A"
        else {"verdict": "CONTROL_RAW_BUNDLE_VALIDATED_PROSPECTIVE", "no_adaptive_successor": True, "authoritative_common_commit": proof, "fixed_horizon": metric}
    )
    return {**base, "terminal_validator": terminal}


def test_manifest_is_planning_only_no_launch_scaffold_with_three_counterbalanced_pairs():
    manifest = _manifest()
    assert comparison.validate_manifest(manifest)["verdict"] == "MANIFEST_VALID"
    assert [pair["arm_order"] for pair in manifest["pairs"]] == [["A", "C"], ["C", "A"], ["A", "C"]]


@pytest.mark.parametrize(
    "mutation",
    ["quorum", "throughput", "order", "replacement", "workload", "fault_basis", "timeout", "control_policy", "seed_negative", "seed_duplicate"],
)
def test_manifest_rejects_consensus_or_campaign_contract_drift(mutation: str):
    manifest = _manifest()
    if mutation == "quorum":
        manifest["shared_inputs"]["quorum"] = 4
    elif mutation == "throughput":
        manifest["primary_outcome"]["throughput"] = "tps"
    elif mutation == "order":
        manifest["pairs"][1]["arm_order"] = ["A", "C"]
    elif mutation == "replacement":
        manifest["failure_accounting"]["replacement_runs"] = True
    elif mutation == "workload":
        manifest["shared_inputs"]["workload"] = "different"
    elif mutation == "fault_basis":
        manifest["shared_inputs"]["fault_window_basis"] = "unbound"
    elif mutation == "timeout":
        manifest["shared_inputs"]["hard_timeout_seconds"] = 1
    elif mutation == "control_policy":
        manifest["arms"]["control"]["policy"] = "adaptive_sham"
    elif mutation == "seed_negative":
        manifest["pairs"][0]["seed"] = -1
    else:
        manifest["pairs"][1]["seed"] = manifest["pairs"][0]["seed"]
    manifest["manifest_sha256"] = comparison.manifest_digest(manifest)
    with pytest.raises(comparison.ValidationError):
        comparison.validate_manifest(manifest)


def test_pair_requires_two_matched_passes_and_all_authoritative_witnesses():
    manifest = _manifest()
    result = comparison.validate_pair(manifest, "P1", [_outcome(manifest, "P1", "A"), _outcome(manifest, "P1", "C")])
    assert result["verdict"] == "PAIR_PLANNING_ONLY_UNBOUND"
    assert all(
        outcome["eligible_for_primary_outcome"] is False
        for outcome in result["outcomes"]
    )
    assert all(
        outcome["evidence_state"] == "CALLER_SUMMARY_ONLY"
        for outcome in result["outcomes"]
    )
    broken = _outcome(manifest, "P1", "C")
    broken["terminal_validator"]["authoritative_common_commit"]["witness_replicas"] = [1, 2]
    with pytest.raises(comparison.ValidationError, match="all six"):
        comparison.validate_pair(manifest, "P1", [_outcome(manifest, "P1", "A"), broken])


def test_caller_authored_pass_cannot_claim_raw_artifact_acceptance():
    manifest = _manifest()
    adaptive = _outcome(manifest, "P1", "A")
    control = _outcome(manifest, "P1", "C")
    result = comparison.validate_pair(manifest, "P1", [adaptive, control])
    assert result["verdict"] == "PAIR_PLANNING_ONLY_UNBOUND"
    assert "caller-authored PASS summaries are not evidence" in result["claim_boundary"]
    assert all(
        "not independently reopened" in outcome["claim_boundary"]
        for outcome in result["outcomes"]
    )

    control["raw_receipt_path"] = "forged/raw-receipt.json"
    control["validator_verdict_sha256"] = "a" * 64
    with pytest.raises(comparison.ValidationError, match="schema drift"):
        comparison.validate_pair(manifest, "P1", [adaptive, control])


def test_pair_retains_abort_instead_of_excluding_or_replacing_it():
    manifest = _manifest()
    result = comparison.validate_pair(manifest, "P2", [_outcome(manifest, "P2", "C", status="ABORTED"), _outcome(manifest, "P2", "A")])
    assert result["verdict"] == "PAIR_RETAINED_NONPASS"
    assert result["outcomes"][0]["status"] == "ABORTED"


def test_pair_rejects_shared_input_or_control_policy_drift():
    manifest = _manifest()
    control = _outcome(manifest, "P3", "C")
    control["shared_inputs"]["workload"] = "different"
    with pytest.raises(comparison.ValidationError, match="E0, fault, workload"):
        comparison.validate_pair(manifest, "P3", [_outcome(manifest, "P3", "A"), control])
    control = _outcome(manifest, "P3", "C")
    control["terminal_validator"]["no_adaptive_successor"] = False
    with pytest.raises(comparison.ValidationError, match="unexpectedly"):
        comparison.validate_pair(manifest, "P3", [_outcome(manifest, "P3", "A"), control])


def test_pair_requires_the_same_fixed_horizon_measurement_schema_for_both_arms():
    manifest = _manifest()
    control = _outcome(manifest, "P1", "C")
    del control["terminal_validator"]["fixed_horizon"]
    with pytest.raises(comparison.ValidationError, match="terminal validator"):
        comparison.validate_pair(manifest, "P1", [_outcome(manifest, "P1", "A"), control])


@pytest.mark.parametrize("duration", [59_000_000_000, 61_000_000_000])
def test_pair_rejects_any_horizon_other_than_exactly_sixty_seconds(duration):
    manifest = _manifest()
    adaptive = _outcome(manifest, "P1", "A")
    metric = adaptive["terminal_validator"]["fixed_horizon"]
    metric["horizon_end_monotonic_ns"] = metric["anchor_monotonic_ns"] + duration
    with pytest.raises(comparison.ValidationError, match="fixed-horizon"):
        comparison.validate_pair(manifest, "P1", [adaptive, _outcome(manifest, "P1", "C")])


def test_manifest_rejects_a_shifted_measurement_anchor_identity():
    manifest = _manifest()
    manifest["shared_inputs"]["measurement_anchor_source"] = "replica-2:fault.aggregate_omitted"
    manifest["manifest_sha256"] = comparison.manifest_digest(manifest)
    with pytest.raises(comparison.ValidationError, match="fixed shared input"):
        comparison.validate_manifest(manifest)
