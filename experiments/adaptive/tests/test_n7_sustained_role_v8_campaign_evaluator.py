from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "n7-path-timeout-quorum" / "sustained_role_v8_campaign_evaluator.py"
SPEC = importlib.util.spec_from_file_location("n7_sustained_role_v8_campaign_evaluator", MODULE)
assert SPEC and SPEC.loader
subject = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = subject
SPEC.loader.exec_module(subject)


def _freeze() -> dict[str, object]:
    return subject.freeze(subject.frozen_design(
        campaign_id="w19-v8-example", repository_revision="a" * 40,
        comparability_sha256="b" * 64, approval_reference="user-confirmed-campaign",
    ))


def _receipts(*, adaptive_counts: tuple[int, ...] = (12, 12, 12, 12, 12, 12),
              fixed_counts: tuple[int, ...] = (10, 10, 10, 10, 10, 10)) -> list[dict[str, object]]:
    freeze = _freeze()
    output: list[dict[str, object]] = []
    for ordinal in range(1, 13):
        pair_index = (ordinal + 1) // 2
        order = subject.FROZEN_PAIR_SCHEDULE[pair_index - 1]
        arm = order[(ordinal - 1) % 2]
        anchor = 10_000_000_000 + ordinal
        timing = subject.profile.deadlines(anchor)
        count = adaptive_counts[pair_index - 1] if arm == subject.ADAPTIVE_ARM else fixed_counts[pair_index - 1]
        output.append({
            "schema_version": 1, "kind": subject.ARM_RECEIPT_KIND,
            "campaign_id": freeze["campaign_id"], "freeze_sha256": freeze["freeze_sha256"],
            "repository_revision": freeze["repository_revision"],
            "comparability_sha256": freeze["comparability_sha256"], "ordinal": ordinal,
            "pair_index": pair_index, "order": list(order), "arm": arm,
            "run_id": f"run-{ordinal}", "run_root": f"/campaign/cell-{ordinal}",
            "pilot": False, "no_retry": True, "validator_verdict": subject.ARM_VERDICT,
            "anchor_monotonic_ns": anchor,
            "metric": {"name": freeze["metric"]["name"], "clock": "CLOCK_MONOTONIC_RAW",
                       "window_start_ns": timing["measurement_start_ns"],
                       "window_end_ns": timing["measurement_end_ns"], "commit_count": count},
            "raw_bundle_sha256": f"{ordinal:064x}",
        })
    return output


def _validated(receipt: dict[str, object]) -> dict[str, object]:
    metric = receipt["metric"]
    assert isinstance(metric, dict)
    return {"profile_id": subject.profile.PROFILE_ID,
            "measurement_window": [metric["window_start_ns"], metric["window_end_ns"]],
            "raw_bundle_sha256": receipt["raw_bundle_sha256"],
            "common_committed_block_count": metric["commit_count"],
            "no_retry": True, "abort_present": False, "physical_exposure_bound": True,
            "selection_to_convergence_bound": receipt["arm"] == subject.ADAPTIVE_ARM,
            "convergence_to_bundle_bound": receipt["arm"] == subject.ADAPTIVE_ARM,
            "fixed_e0_no_successor_bound": receipt["arm"] == subject.FIXED_ARM}


def test_v8_components_do_not_issue_a_campaign_verdict_without_sealed_roots() -> None:
    result = subject._evaluate_unsealed_components(_freeze(), _receipts(), validate_arm=_validated)
    assert result["verdict"] == "COMPONENTS_ONLY_NO_CAMPAIGN_VERDICT"
    assert result["positive_pairs"] == 6
    assert result["positive_pairs_by_order"] == {"fixed_e0,adaptive_e1": 3, "adaptive_e1,fixed_e0": 3}
    assert result["aggregate"] == {"fixed_count": 60, "adaptive_count": 72,
                                   "ratio_numerator": 72, "ratio_denominator": 60}
    assert result["claim_eligible"] is False
    with pytest.raises(subject.V8CampaignError, match="receipt materialization is not integrated"):
        subject.evaluate(_freeze(), _receipts(), validate_arm=_validated)


def test_v8_campaign_rejects_less_than_two_positive_pairs_in_one_order() -> None:
    # Pairs 2, 4 and 6 are BA; make two of them non-positive while retaining
    # five-looking cells in total.  Counterbalancing must be a hard gate.
    with pytest.raises(subject.V8CampaignError, match="improvement gate"):
        subject.evaluate(_freeze(), _receipts(adaptive_counts=(12, 9, 12, 9, 12, 12)), validate_arm=_validated)


def test_v8_campaign_rejects_pilot_and_wrong_metric_window() -> None:
    receipts = _receipts()
    receipts[0]["pilot"] = True
    with pytest.raises(subject.V8CampaignError, match="frozen cell"):
        subject.evaluate(_freeze(), receipts, validate_arm=_validated)
    receipts = _receipts()
    metric = receipts[0]["metric"]
    assert isinstance(metric, dict)
    metric["window_start_ns"] = int(metric["window_start_ns"]) - 1
    with pytest.raises(subject.V8CampaignError, match="A\\+32"):
        subject.evaluate(_freeze(), receipts, validate_arm=_validated)


def test_v8_campaign_rejects_reused_raw_bundle_and_no_fixed_denominator() -> None:
    receipts = _receipts()
    receipts[1]["raw_bundle_sha256"] = receipts[0]["raw_bundle_sha256"]
    with pytest.raises(subject.V8CampaignError, match="reuses"):
        subject.evaluate(_freeze(), receipts, validate_arm=_validated)
    with pytest.raises(subject.V8CampaignError, match="denominator is zero"):
        subject.evaluate(_freeze(), _receipts(fixed_counts=(0, 0, 0, 0, 0, 0),
                                               adaptive_counts=(1, 1, 1, 1, 1, 1)), validate_arm=_validated)


def test_v8_campaign_rejects_producer_count_that_the_validator_cannot_recompute() -> None:
    receipts = _receipts()
    def wrong(receipt: dict[str, object]) -> dict[str, object]:
        result = _validated(receipt)
        result["common_committed_block_count"] = int(result["common_committed_block_count"]) + 1
        return result
    with pytest.raises(subject.V8CampaignError, match="differs from validator recomputation"):
        subject.evaluate(_freeze(), receipts, validate_arm=wrong)


def test_v8_campaign_rejects_adaptive_arm_without_selection_to_bundle_chain() -> None:
    def unbound(receipt: dict[str, object]) -> dict[str, object]:
        result = _validated(receipt)
        if receipt["arm"] == subject.ADAPTIVE_ARM:
            result["selection_to_convergence_bound"] = False
        return result
    with pytest.raises(subject.V8CampaignError, match="causal identity chain"):
        subject.evaluate(_freeze(), _receipts(), validate_arm=unbound)


def test_v8_components_reject_abort_or_missing_no_retry_proof() -> None:
    def aborted(receipt: dict[str, object]) -> dict[str, object]:
        result = _validated(receipt)
        result["abort_present"] = True
        return result
    with pytest.raises(subject.V8CampaignError, match="does not bind"):
        subject._evaluate_unsealed_components(_freeze(), _receipts(), validate_arm=aborted)


def test_v8_freeze_is_self_hashing_and_rejects_v7_window() -> None:
    freeze = _freeze()
    subject._check_freeze(freeze)
    changed = dict(freeze)
    changed["metric"] = dict(changed["metric"])
    changed["metric"]["window"] = "[anchor+20s,anchor+60s)"
    with pytest.raises(subject.V8CampaignError, match="metric or improvement gate drifted"):
        subject._check_freeze(changed)
