#!/usr/bin/env python3
"""Fail-closed campaign gate for the prospective N=7 W19 v8 study.

This is intentionally a new evaluator.  It neither launches a process nor
loads the v7 evaluator: the v8 metric starts at A+32 seconds and must not be
silently compared with legacy A+20-second evidence.  Arm receipts are inputs
to this boundary only after the per-arm raw validator has issued its own
versioned, no-claim verdict.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("w19_v8_profile_campaign", HERE / "sustained_role_v8_profile.py")
assert _SPEC and _SPEC.loader
profile = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(profile)

FIXED_ARM = "fixed_e0"
ADAPTIVE_ARM = "adaptive_e1"
PAIR_COUNT = 6
FROZEN_PAIR_SCHEDULE = (
    (FIXED_ARM, ADAPTIVE_ARM), (ADAPTIVE_ARM, FIXED_ARM),
    (FIXED_ARM, ADAPTIVE_ARM), (ADAPTIVE_ARM, FIXED_ARM),
    (FIXED_ARM, ADAPTIVE_ARM), (ADAPTIVE_ARM, FIXED_ARM),
)
FREEZE_KIND = "kauri-n7-sustained-role-v8-campaign-freeze-v1"
# Matches the root-owned, immutable replay receipt checked by
# ``sustained_role_v8_campaign_operator``.  The public evaluator stays closed
# until it can consume that on-disk receipt rather than caller mappings.
ARM_RECEIPT_KIND = "kauri-n7-sustained-role-v8-replay-receipt-v1"
RESULT_KIND = "kauri-n7-sustained-role-v8-campaign-result-v1"
ARM_VERDICT = "PASS_V8_ARM_VALIDATED_NO_CLAIM"
_HEX = frozenset("0123456789abcdef")


class V8CampaignError(ValueError):
    """A v8 freeze or arm receipt is insufficient for a campaign result."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise V8CampaignError("value is not canonical ASCII JSON") from exc


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _hex40(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(char not in _HEX for char in value):
        raise V8CampaignError(f"{label} is not a lower-case 40-character revision")
    return value


def _hex64(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in _HEX for char in value):
        raise V8CampaignError(f"{label} is not a lower-case SHA-256")
    return value


def _require_exact(value: Mapping[str, object], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise V8CampaignError(f"{label} schema drifted")


def frozen_design(*, campaign_id: str, repository_revision: str,
                  comparability_sha256: str, approval_reference: str) -> dict[str, object]:
    """Build an unfrozen v8 design.  :func:`freeze` seals it once."""
    if not isinstance(campaign_id, str) or not campaign_id:
        raise V8CampaignError("campaign ID is missing")
    if not isinstance(approval_reference, str) or not approval_reference:
        raise V8CampaignError("campaign approval reference is missing")
    return {
        "schema_version": 1, "kind": FREEZE_KIND, "campaign_id": campaign_id,
        "repository_revision": _hex40(repository_revision, "repository revision"),
        "comparability_sha256": _hex64(comparability_sha256, "comparability binding"),
        "approval_reference": approval_reference,
        "profile": dict(profile.FROZEN_PROFILE),
        "pair_schedule": [list(pair) for pair in FROZEN_PAIR_SCHEDULE],
        "retry_policy": "none", "pilot_policy": "excluded",
        "metric": {
            "name": "all-seven-common-authoritative-committed-block-count-v2",
            "clock": "CLOCK_MONOTONIC_RAW", "start_offset_ns": profile.ANCHOR_TO_ACTIVATION_NS,
            "end_offset_ns": profile.ANCHOR_TO_HORIZON_NS,
            "window": "[anchor+32s,anchor+72s)", "designated_observer": 2,
        },
        "improvement_gate": {
            "positive_pair_ratio_numerator": 11, "positive_pair_ratio_denominator": 10,
            "required_positive_pairs": 5, "required_positive_pairs_per_order": 2,
            "aggregate_ratio_numerator": 11, "aggregate_ratio_denominator": 10,
        },
    }


def freeze(design: Mapping[str, object]) -> dict[str, object]:
    """Return a canonical, self-hashing v8 campaign freeze."""
    required = {"schema_version", "kind", "campaign_id", "repository_revision", "comparability_sha256",
                "approval_reference", "profile", "pair_schedule", "retry_policy", "pilot_policy",
                "metric", "improvement_gate"}
    _require_exact(design, required, "v8 campaign design")
    if design.get("schema_version") != 1 or design.get("kind") != FREEZE_KIND:
        raise V8CampaignError("campaign design version differs from v8")
    _hex40(design.get("repository_revision"), "repository revision")
    _hex64(design.get("comparability_sha256"), "comparability binding")
    if (not isinstance(design.get("campaign_id"), str) or not design["campaign_id"] or
            not isinstance(design.get("approval_reference"), str) or not design["approval_reference"]):
        raise V8CampaignError("campaign identity is incomplete")
    try:
        profile.validate_profile(design["profile"])
    except Exception as exc:
        raise V8CampaignError("v8 profile differs from frozen contract") from exc
    if design.get("pair_schedule") != [list(pair) for pair in FROZEN_PAIR_SCHEDULE]:
        raise V8CampaignError("campaign order differs from frozen AB/BA schedule")
    if design.get("retry_policy") != "none" or design.get("pilot_policy") != "excluded":
        raise V8CampaignError("campaign retry or pilot policy drifted")
    expected_metric = frozen_design(campaign_id=str(design["campaign_id"]),
                                    repository_revision=str(design["repository_revision"]),
                                    comparability_sha256=str(design["comparability_sha256"]),
                                    approval_reference=str(design["approval_reference"]))["metric"]
    expected_gate = frozen_design(campaign_id=str(design["campaign_id"]),
                                  repository_revision=str(design["repository_revision"]),
                                  comparability_sha256=str(design["comparability_sha256"]),
                                  approval_reference=str(design["approval_reference"]))["improvement_gate"]
    if design.get("metric") != expected_metric or design.get("improvement_gate") != expected_gate:
        raise V8CampaignError("v8 metric or improvement gate drifted")
    frozen = deepcopy(dict(design))
    frozen["freeze_sha256"] = _sha(frozen)
    return frozen


def _check_freeze(value: Mapping[str, object]) -> dict[str, object]:
    required = {"schema_version", "kind", "campaign_id", "repository_revision", "comparability_sha256",
                "approval_reference", "profile", "pair_schedule", "retry_policy", "pilot_policy",
                "metric", "improvement_gate", "freeze_sha256"}
    _require_exact(value, required, "v8 campaign freeze")
    supplied = _hex64(value.get("freeze_sha256"), "freeze SHA-256")
    design = {key: item for key, item in value.items() if key != "freeze_sha256"}
    expected = freeze(design)
    if supplied != expected["freeze_sha256"]:
        raise V8CampaignError("campaign freeze SHA-256 does not recompute")
    return expected


def _receipt(receipt: Mapping[str, object], freeze: Mapping[str, object], ordinal: int) -> dict[str, object]:
    required = {"schema_version", "kind", "campaign_id", "freeze_sha256", "repository_revision",
                "comparability_sha256", "ordinal", "pair_index", "order", "arm", "run_id", "run_root",
                "pilot", "no_retry", "validator_verdict", "anchor_monotonic_ns", "metric", "raw_bundle_sha256"}
    _require_exact(receipt, required, f"v8 arm receipt {ordinal}")
    pair_index = (ordinal + 1) // 2
    expected_arm = FROZEN_PAIR_SCHEDULE[pair_index - 1][(ordinal - 1) % 2]
    if (receipt.get("schema_version") != 1 or receipt.get("kind") != ARM_RECEIPT_KIND or
            receipt.get("campaign_id") != freeze["campaign_id"] or receipt.get("freeze_sha256") != freeze["freeze_sha256"] or
            receipt.get("repository_revision") != freeze["repository_revision"] or
            receipt.get("comparability_sha256") != freeze["comparability_sha256"] or
            receipt.get("ordinal") != ordinal or receipt.get("pair_index") != pair_index or
            receipt.get("order") != list(FROZEN_PAIR_SCHEDULE[pair_index - 1]) or receipt.get("arm") != expected_arm or
            receipt.get("pilot") is not False or receipt.get("no_retry") is not True or
            receipt.get("validator_verdict") != ARM_VERDICT):
        raise V8CampaignError(f"v8 arm receipt {ordinal} is not the frozen cell")
    if (not isinstance(receipt.get("run_id"), str) or not receipt["run_id"] or
            not isinstance(receipt.get("run_root"), str) or not receipt["run_root"]):
        raise V8CampaignError(f"v8 arm receipt {ordinal} has no unique execution identity")
    _hex64(receipt.get("raw_bundle_sha256"), f"v8 arm receipt {ordinal} raw bundle")
    anchor = receipt.get("anchor_monotonic_ns")
    if type(anchor) is not int or anchor < 0:
        raise V8CampaignError(f"v8 arm receipt {ordinal} has an invalid anchor")
    metric = receipt.get("metric")
    if not isinstance(metric, Mapping):
        raise V8CampaignError(f"v8 arm receipt {ordinal} metric is missing")
    _require_exact(metric, {"name", "clock", "window_start_ns", "window_end_ns", "commit_count"},
                   f"v8 arm receipt {ordinal} metric")
    deadlines = profile.deadlines(anchor)
    if (metric.get("name") != freeze["metric"]["name"] or metric.get("clock") != "CLOCK_MONOTONIC_RAW" or
            metric.get("window_start_ns") != deadlines["measurement_start_ns"] or
            metric.get("window_end_ns") != deadlines["measurement_end_ns"] or
            type(metric.get("commit_count")) is not int or metric["commit_count"] < 0):
        raise V8CampaignError(f"v8 arm receipt {ordinal} does not bind the equal A+32..A+72 metric")
    return dict(receipt)


def _recomputed_observation(observation: Mapping[str, object], receipt: Mapping[str, object],
                            freeze: Mapping[str, object], ordinal: int) -> int:
    """Verify the independent validator's recomputation, not a producer count.

    The callable passed to :func:`evaluate` must reopen the receipt-pinned raw
    bundle and call the v8 per-arm validator.  Its result is kept intentionally
    small here so the campaign gate cannot accidentally become a second raw
    parser with different consensus semantics.
    """
    required = {"profile_id", "measurement_window", "raw_bundle_sha256",
                "common_committed_block_count", "no_retry", "abort_present",
                "selection_to_convergence_bound", "convergence_to_bundle_bound",
                "fixed_e0_no_successor_bound", "physical_exposure_bound"}
    _require_exact(observation, required, f"v8 validator observation {ordinal}")
    anchor = receipt["anchor_monotonic_ns"]
    assert isinstance(anchor, int)
    timing = profile.deadlines(anchor)
    if (observation.get("profile_id") != profile.PROFILE_ID or
            observation.get("measurement_window") != [timing["measurement_start_ns"], timing["measurement_end_ns"]] or
            observation.get("raw_bundle_sha256") != receipt["raw_bundle_sha256"] or
            observation.get("no_retry") is not True or observation.get("abort_present") is not False or
            observation.get("physical_exposure_bound") is not True or
            type(observation.get("common_committed_block_count")) is not int or
            observation["common_committed_block_count"] < 0):
        raise V8CampaignError(f"v8 validator observation {ordinal} does not bind the receipt")
    if receipt["arm"] == ADAPTIVE_ARM:
        if (observation.get("selection_to_convergence_bound") is not True or
                observation.get("convergence_to_bundle_bound") is not True or
                observation.get("fixed_e0_no_successor_bound") is not False):
            raise V8CampaignError(f"v8 adaptive observation {ordinal} lacks its causal identity chain")
    elif (observation.get("selection_to_convergence_bound") is not False or
          observation.get("convergence_to_bundle_bound") is not False or
          observation.get("fixed_e0_no_successor_bound") is not True):
        raise V8CampaignError(f"v8 fixed observation {ordinal} lacks its fixed-E0 authority proof")
    count = int(observation["common_committed_block_count"])
    metric = receipt["metric"]
    assert isinstance(metric, Mapping)
    if count != metric["commit_count"]:
        raise V8CampaignError(f"v8 arm receipt {ordinal} producer metric differs from validator recomputation")
    return count


def _evaluate_unsealed_components(freeze_value: Mapping[str, object],
                                  receipts: Sequence[Mapping[str, object],], *,
                                  validate_arm: Any) -> dict[str, object]:
    """Check prospective component invariants, never issue a campaign verdict.

    This helper remains private because it accepts mappings supplied by its
    caller.  A future public evaluator must first reopen immutable run roots,
    verify the archived authorization, raw descriptors, no-abort finalization,
    and then invoke the arm-specific validator on those bytes.  It must not
    treat this helper's arithmetic as evidence.
    """
    freeze_checked = _check_freeze(freeze_value)
    if not callable(validate_arm):
        raise V8CampaignError("v8 campaign requires an independent per-arm validator")
    if len(receipts) != PAIR_COUNT * 2:
        raise V8CampaignError("v8 campaign requires exactly twelve arm receipts")
    cells = [_receipt(item, freeze_checked, ordinal) for ordinal, item in enumerate(receipts, start=1)]
    recomputed_counts = [_recomputed_observation(validate_arm(cell), cell, freeze_checked, ordinal)
                         for ordinal, cell in enumerate(cells, start=1)]
    run_ids = [str(cell["run_id"]) for cell in cells]
    run_roots = [str(cell["run_root"]) for cell in cells]
    raw_bundles = [str(cell["raw_bundle_sha256"]) for cell in cells]
    if (len(set(run_ids)) != len(run_ids) or len(set(run_roots)) != len(run_roots) or
            len(set(raw_bundles)) != len(raw_bundles)):
        raise V8CampaignError("v8 campaign reuses a run identity, root, or raw bundle")
    rows: list[dict[str, object]] = []
    positives_by_order = {"fixed_e0,adaptive_e1": 0, "adaptive_e1,fixed_e0": 0}
    total_fixed = total_adaptive = 0
    gate = freeze_checked["improvement_gate"]
    assert isinstance(gate, Mapping)
    numerator = int(gate["positive_pair_ratio_numerator"])
    denominator = int(gate["positive_pair_ratio_denominator"])
    for pair_index, order in enumerate(FROZEN_PAIR_SCHEDULE, start=1):
        fixed = cells[(pair_index - 1) * 2 + order.index(FIXED_ARM)]
        adaptive = cells[(pair_index - 1) * 2 + order.index(ADAPTIVE_ARM)]
        fixed_count = recomputed_counts[(pair_index - 1) * 2 + order.index(FIXED_ARM)]
        adaptive_count = recomputed_counts[(pair_index - 1) * 2 + order.index(ADAPTIVE_ARM)]
        # A zero fixed denominator is deliberately not a positive pair: it
        # cannot demonstrate the predeclared multiplicative improvement.
        positive = fixed_count > 0 and adaptive_count * denominator >= fixed_count * numerator
        order_key = ",".join(order)
        positives_by_order[order_key] += int(positive)
        total_fixed += fixed_count
        total_adaptive += adaptive_count
        rows.append({"pair_index": pair_index, "order": list(order), "fixed_count": fixed_count,
                     "adaptive_count": adaptive_count, "positive": positive})
    positive_pairs = sum(int(bool(row["positive"])) for row in rows)
    if total_fixed <= 0:
        raise V8CampaignError("v8 aggregate fixed denominator is zero")
    if (positive_pairs < int(gate["required_positive_pairs"]) or
            any(count < int(gate["required_positive_pairs_per_order"])
                for count in positives_by_order.values()) or
            total_adaptive * int(gate["aggregate_ratio_denominator"]) <
            total_fixed * int(gate["aggregate_ratio_numerator"])):
        raise V8CampaignError("v8 campaign did not meet its frozen improvement gate")
    return {"schema_version": 1, "kind": RESULT_KIND, "campaign_id": freeze_checked["campaign_id"],
              "freeze_sha256": freeze_checked["freeze_sha256"], "verdict": "COMPONENTS_ONLY_NO_CAMPAIGN_VERDICT",
              "claim_eligible": False, "figure_eligible": False, "pairs": rows,
              "positive_pairs": positive_pairs, "positive_pairs_by_order": positives_by_order,
              "aggregate": {"fixed_count": total_fixed, "adaptive_count": total_adaptive,
                            "ratio_numerator": total_adaptive, "ratio_denominator": total_fixed},
              "metric_window": freeze_checked["metric"]["window"],
              "estimand": "all-seven common committed-block cadence under pinned workload; not transaction throughput"}


def evaluate(freeze_value: Mapping[str, object], receipts: Sequence[Mapping[str, object],], *,
             validate_arm: Any) -> dict[str, object]:
    """Refuse a campaign verdict until sealed v8 receipt integration exists.

    This explicit hard stop prevents a caller from presenting fabricated
    mappings or a callback result as a cluster campaign.  It is removed only
    when the launch/materializer/validator interface can reopen the immutable
    per-cell archive and check raw hashes, authorization, cleanup and abort
    state before any count reaches :func:`_evaluate_unsealed_components`.
    """
    _evaluate_unsealed_components(freeze_value, receipts, validate_arm=validate_arm)
    raise V8CampaignError(
        "v8 campaign receipt materialization is not integrated; no campaign verdict may be issued"
    )
