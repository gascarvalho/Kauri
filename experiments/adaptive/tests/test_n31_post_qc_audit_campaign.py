"""Pure contract tests for the frozen N=31 PQAR repetition campaign."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive.kauri_experiment.n31_post_qc_audit_campaign import (
    ARM_NAMES,
    ATTEMPTS_PER_ARM,
    CAMPAIGN_PROFILE_ID,
    CAMPAIGN_PROFILE_SHA256,
    CAMPAIGN_SCENARIO,
    CLASSIFICATION_NAMES,
    N31PostQcAuditCampaignError,
    SCHEDULED_ATTEMPTS,
    TIMING_METRICS_NS,
    build_campaign_plan,
    clopper_pearson_interval,
    derive_frozen_campaign_schedule,
    exact_median_interval,
    load_frozen_campaign_profile,
    nanosecond_metric_summary,
    source_blind_order_key,
    summarize_n31_pqar_campaign,
)

REPOSITORY = Path(__file__).resolve().parents[3]
PROFILE_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/n31-f5-post-qc-audit-campaign-v3.json"
)
V1_PROFILE_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/n31-f5-post-qc-audit-campaign-v1.json"
)
V2_PROFILE_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/n31-f5-post-qc-audit-campaign-v2.json"
)
REVISION = "a" * 40


def _profile():
    return load_frozen_campaign_profile(PROFILE_PATH)


def _plan():
    return build_campaign_plan(
        _profile(),
        kauri_revision=REVISION,
        trusted_provenance_sha256="b" * 64,
        pilot_gate={"sealed": True},
        frozen_preflight_sha256="c" * 64,
    )


def _records_and_observations(
    *,
    outcomes: tuple[str, ...] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    profile = _profile()
    schedule = derive_frozen_campaign_schedule(profile)
    if outcomes is None:
        outcomes = ("PASS",) * SCHEDULED_ATTEMPTS
    records: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    for slot, outcome in zip(schedule, outcomes, strict=True):
        ordinal = int(slot["ordinal"])
        arm = str(slot["arm"])
        run_directory = f"{slot['results_root']}/run-{ordinal:03d}"
        records.append(
            {
                "schema_version": 1,
                "scenario": f"{CAMPAIGN_SCENARIO}-slot-execution",
                **deepcopy(slot),
                "launch_status": "returned",
                "started_utc": "2026-08-04T10:00:00+00:00",
                "finished_utc": "2026-08-04T10:01:00+00:00",
                "elapsed_ns": ordinal,
                "run_directory": run_directory,
                "original_verdict": outcome,
                "child_tree_sha256": f"{ordinal:064x}",
                "child_seal_sha256": f"{ordinal + 100:064x}",
                "intent_tree_sha256": "d" * 64,
                "intent_seal_sha256": "e" * 64,
                "exception": None,
            }
        )
        observations.append(
            {
                "schema_version": 1,
                "scenario": f"{CAMPAIGN_SCENARIO}-blind-observation",
                "ordinal": ordinal,
                "run_directory": run_directory,
                "observation": {
                    "predicted_classification": profile.expected_classification(arm),
                    "timing": {
                        metric: ordinal * 1_000 + index
                        for index, metric in enumerate(TIMING_METRICS_NS)
                    },
                    "evidence_seal_sha256": f"{ordinal + 100:064x}",
                },
                "observation_error": None,
            }
        )
    return records, observations


def test_campaign_profile_is_bound_to_exact_shipped_bytes(tmp_path: Path) -> None:
    profile = _profile()

    assert hashlib.sha256(PROFILE_PATH.read_bytes()).hexdigest() == (
        CAMPAIGN_PROFILE_SHA256
    )
    assert profile.profile_sha256 == CAMPAIGN_PROFILE_SHA256
    assert profile.profile_id == CAMPAIGN_PROFILE_ID
    assert profile.order_seed == 41_719
    assert profile.audit_profile_sha256 == (
        "847883f4547776f2f6642b7be9fc02045d918a733711f7e7a3450dcbeb67673a"
    )
    assert json.loads(PROFILE_PATH.read_text(encoding="utf-8"))[
        "interpretation_scope"
    ].endswith(
        "audit-v5 is an outcome-informed correction aligning exact target-side "
        "omission ground truth with the frozen post-baseline lifecycle rather "
        "than reporter-local arming"
    )
    assert hashlib.sha256(V1_PROFILE_PATH.read_bytes()).hexdigest() == (
        "f904cb118d95ea975578b85e79b0ffb7dfcc3f0e6d87dfccb8859feb0488ea25"
    )
    assert hashlib.sha256(V2_PROFILE_PATH.read_bytes()).hexdigest() == (
        "acc1191e467901af1743d6930f4e7a36ca6f7ac45dcb6df698a39e668eab996d"
    )

    changed = tmp_path / "changed.json"
    changed.write_bytes(PROFILE_PATH.read_bytes() + b"\n")
    with pytest.raises(N31PostQcAuditCampaignError, match="profile bytes"):
        load_frozen_campaign_profile(changed)


@pytest.mark.parametrize("published", (V1_PROFILE_PATH, V2_PROFILE_PATH))
def test_published_campaign_profiles_are_preserved_but_not_reused(
    published: Path,
) -> None:
    with pytest.raises(N31PostQcAuditCampaignError, match="profile bytes"):
        load_frozen_campaign_profile(published)


def test_source_blind_rank_uses_only_sealed_child_identity() -> None:
    record = {
        "ordinal": 1,
        "arm": "truth-must-not-affect-rank",
        "run_directory": "chronological/path/must-not-affect-rank",
        "child_tree_sha256": "1" * 64,
        "child_seal_sha256": "2" * 64,
    }
    changed_truth = {
        **record,
        "ordinal": 90,
        "arm": "different-truth",
        "run_directory": "different/chronological/path",
    }

    assert source_blind_order_key(record) == source_blind_order_key(changed_truth)
    assert _plan()["source_blind_extraction_order"] == (
        "sha256-ranked-sealed-child-identity-v1"
    )
    assert _plan()["source_blind_isolation"] == "random-opaque-v1"


def test_schedule_uses_every_permutation_five_times_and_is_position_balanced() -> None:
    schedule = derive_frozen_campaign_schedule(_profile())

    assert len(schedule) == 90
    assert [slot["ordinal"] for slot in schedule] == list(range(1, 91))
    assert [slot["arm"] for slot in schedule[:3]] == [
        "static_persistent_direct_vote_omission",
        "static_authenticated_sham",
        "static_authenticated_false_report",
    ]
    blocks = Counter(
        tuple(str(slot["arm"]) for slot in schedule[index : index + 3])
        for index in range(0, len(schedule), 3)
    )
    assert len(blocks) == 6
    assert set(blocks.values()) == {5}
    assert Counter(str(slot["arm"]) for slot in schedule) == Counter(
        {arm: 30 for arm in ARM_NAMES}
    )
    assert Counter(
        (str(slot["arm"]), int(slot["ordinal_position"])) for slot in schedule
    ) == Counter({(arm, position): 10 for arm in ARM_NAMES for position in (1, 2, 3)})
    assert schedule == derive_frozen_campaign_schedule(_profile())


def test_exact_binomial_interval_matches_boundary_anchors() -> None:
    assert clopper_pearson_interval(30, 30) == pytest.approx(
        (0.8842966918, 1.0), abs=1e-10
    )
    assert clopper_pearson_interval(0, 30) == pytest.approx(
        (0.0, 0.1157033082), abs=1e-10
    )
    with pytest.raises(N31PostQcAuditCampaignError):
        clopper_pearson_interval(31, 30)


def test_n30_median_interval_uses_order_statistics_ten_through_twenty_one() -> None:
    interval = exact_median_interval(tuple(range(1, 31)))

    assert interval["lower_order_index"] == 10
    assert interval["upper_order_index"] == 21
    assert interval["lower_ns"] == 10
    assert interval["upper_ns"] == 21
    assert interval["achieved_coverage"] == "0.9572260547"
    assert exact_median_interval((1, 2, 3, 4, 5))["interval"] is None

    summary = nanosecond_metric_summary(tuple(range(1, 22)))
    assert summary["available_count"] == 21
    assert summary["missing_count"] == 9
    assert summary["median_interval"]["lower_order_index"] >= 1

    even_summary = nanosecond_metric_summary(tuple(range(1, 31)))
    assert even_summary["q1_ns"] == 8
    assert even_summary["median_ns"] == 15.5
    assert even_summary["q3_ns"] == 23


def test_all_qualified_campaign_is_accepted_with_fixed_denominators() -> None:
    records, observations = _records_and_observations()
    summary = summarize_n31_pqar_campaign(_profile(), _plan(), records, observations)

    assert summary["campaign_acceptance"] == "ACCEPTED"
    assert summary["outcome_status"] == "ALL_QUALIFIED"
    assert summary["figure_eligible"] is True
    assert summary["returned_invocations"] == 90
    assert summary["outcome_counts"] == {
        "PASS": 90,
        "FAIL": 0,
        "INCOMPLETE": 0,
    }
    assert summary["invocation_status_counts"] == {
        "returned": 90,
        "raised": 0,
        "not_started": 0,
    }
    confusion = summary["confusion_table"]
    assert isinstance(confusion, dict)
    for arm in ARM_NAMES:
        assert sum(confusion[arm].values()) == ATTEMPTS_PER_ARM
        assert confusion[arm][_profile().expected_classification(arm)] == 30
        assert summary["rates"][arm]["classification_correctness"]["trials"] == 30
        assert summary["rates"][arm]["strict_harness_qualification"]["trials"] == 30
        for metric in TIMING_METRICS_NS:
            metric_summary = summary["timing_summaries_ns"][arm][metric]
            assert metric_summary["available_count"] == 30
            assert metric_summary["missing_count"] == 0
            assert metric_summary["median_interval"]["lower_order_index"] == 10
            assert metric_summary["median_interval"]["upper_order_index"] == 21


def test_mixed_adverse_outcomes_and_wrong_labels_remain_integrity_accepted() -> None:
    outcomes = tuple(
        "FAIL" if index % 11 == 0 else "INCOMPLETE" if index % 7 == 0 else "PASS"
        for index in range(1, 91)
    )
    records, observations = _records_and_observations(outcomes=outcomes)
    observations[0]["observation"] = {"classification": "sham", "timing": {}}
    observations[1]["observation"] = {
        "classification": "unclassified",
        "timing": {},
    }
    summary = summarize_n31_pqar_campaign(_profile(), _plan(), records, observations)

    assert summary["campaign_acceptance"] == "ACCEPTED"
    assert summary["outcome_status"] == "MIXED"
    assert summary["figure_eligible"] is True
    assert summary["outcome_counts"]["FAIL"] > 0
    assert summary["outcome_counts"]["INCOMPLETE"] > 0
    assert sum(sum(row.values()) for row in summary["confusion_table"].values()) == 90
    assert any(
        row["unclassified"]
        or sum(
            value
            for classification, value in row.items()
            if classification != _profile().expected_classification(arm)
            and classification != "unclassified"
        )
        for arm, row in summary["confusion_table"].items()
    )
    for arm in ARM_NAMES:
        assert summary["rates"][arm]["classification_correctness"]["trials"] == 30
        assert summary["rates"][arm]["strict_harness_qualification"]["trials"] == 30


def test_missing_invocation_is_rejected_without_shrinking_confusion_rows() -> None:
    records, observations = _records_and_observations()
    records.pop()
    observations.pop()
    summary = summarize_n31_pqar_campaign(_profile(), _plan(), records, observations)

    assert summary["campaign_acceptance"] == "REJECTED"
    assert summary["figure_eligible"] is False
    assert summary["outcome_counts"]["INCOMPLETE"] == 1
    assert summary["invocation_status_counts"]["not_started"] == 1
    assert all(
        sum(summary["confusion_table"][arm][name] for name in CLASSIFICATION_NAMES)
        == 30
        for arm in ARM_NAMES
    )


def test_truth_field_in_blind_observation_rejects_campaign() -> None:
    records, observations = _records_and_observations()
    observation = observations[0]["observation"]
    assert isinstance(observation, dict)
    observation["arm"] = records[0]["arm"]

    summary = summarize_n31_pqar_campaign(_profile(), _plan(), records, observations)

    assert summary["campaign_acceptance"] == "REJECTED"
    assert any(
        "contains truth field arm" in item for item in summary["integrity_failures"]
    )


@pytest.mark.parametrize(
    "payload",
    (
        {"classification": "unknown"},
        {
            "classification": "sham",
            "predicted_classification": "false_reporter",
        },
        {},
    ),
)
def test_unknown_missing_or_inconsistent_blind_classification_is_rejected(
    payload: dict[str, object],
) -> None:
    records, observations = _records_and_observations()
    observations[0]["observation"] = payload

    summary = summarize_n31_pqar_campaign(_profile(), _plan(), records, observations)

    assert summary["campaign_acceptance"] == "REJECTED"
    assert summary["confusion_table"][records[0]["arm"]]["unclassified"] == 1
    assert any(
        "source-blind classification" in item or "lacks a classification" in item
        for item in summary["integrity_failures"]
    )
