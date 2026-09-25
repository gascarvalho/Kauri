"""Tests for the frozen CERT13 ranking-repeatability producer."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[3]
SCRIPT = REPOSITORY / "experiments/adaptive/ranking_repeatability.py"
SPEC = importlib.util.spec_from_file_location("ranking_repeatability", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
producer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(producer)


@pytest.fixture
def committed_producer(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    provenance = {
        "analysis_revision": "a" * 40,
        "generator_source_sha256": producer._sha256_bytes(SCRIPT.read_bytes()),
    }
    monkeypatch.setattr(
        producer,
        "_committed_source_provenance",
        lambda: provenance,
    )
    return provenance


def test_pairwise_metrics_use_only_common_replica_order() -> None:
    left = {"pair_id": "pair-01", "raw_eligible_ranking": [0, 1, 2]}
    right = {"pair_id": "pair-02", "raw_eligible_ranking": [2, 0, 3]}

    result = producer._pairwise(left, right)

    assert result == {
        "left_pair_id": "pair-01",
        "right_pair_id": "pair-02",
        "common_root_count": 2,
        "union_root_count": 4,
        "jaccard": {"numerator": 2, "denominator": 4, "decimal": 0.5},
        "kendall_common": {
            "common_item_count": 2,
            "comparable_pair_count": 1,
            "concordant_pair_count": 0,
            "discordant_pair_count": 1,
            "numerator": -1,
            "denominator": 1,
            "tau": -1.0,
        },
        "exact_position_match_count": 0,
    }


def test_root_list_rejects_duplicates_and_boolean_ids() -> None:
    with pytest.raises(producer.RankingRepeatabilityError, match="21 unique"):
        producer._root_list([0] * 21, "roots")
    with pytest.raises(producer.RankingRepeatabilityError, match="21 unique"):
        producer._root_list([*range(20), True], "roots")


def test_raw_event_selection_is_unique_and_policy_bound(tmp_path: Path) -> None:
    stream = tmp_path / "events.jsonl"
    event = {
        "event_type": "adaptive_v2_evidence_snapshot",
        "source_sequence": 7,
        "payload": {
            "cycle_ordinal": 1,
            "policy_intent": "performance_optimization",
            "transition_artifact_id": "e1-to-e2-optimization",
        },
    }
    stream.write_text(json.dumps(event) + "\n", encoding="ascii")
    assert producer._raw_optimization_event(stream) == event

    stream.write_text(
        json.dumps(event) + "\n" + json.dumps(event) + "\n", encoding="ascii"
    )
    with pytest.raises(producer.RankingRepeatabilityError, match="exactly one"):
        producer._raw_optimization_event(stream)


def test_cli_rejects_nonfrozen_hash_assertion(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        producer.main(
            [
                "--campaign-root",
                "/unused",
                "--campaign-validation",
                "/unused-validation",
                "--placement-contrast",
                "/unused-placement",
                "--output",
                "/unused-output",
                "--expected-placement-contrast-sha256",
                "0" * 64,
            ]
        )
    assert exc.value.code == 2
    assert "differs from frozen input" in capsys.readouterr().err


def test_real_accepted_inputs_reproduce_frozen_metrics(
    tmp_path: Path,
    committed_producer: dict[str, str],
) -> None:
    workspace = REPOSITORY.parent
    campaign_root = REPOSITORY / "results/cert13-n31-campaign-v13-7adabc83-r50"
    campaign_validation = (
        REPOSITORY
        / "results/.cert13-n31-campaign-v13-7adabc83-r50-ba797cca2eca5d82-preflight/campaign-validation.json"
    )
    placement = (
        workspace
        / "docs/context/thesis/research/evidence/cert13-placement-contrast-e7ab5506/placement-contrast.json"
    )
    if not campaign_root.is_dir() or not placement.is_file():
        pytest.skip("accepted CERT13 evidence is not present")
    output = tmp_path / "ranking-repeatability.json"

    producer.write_report(campaign_root, campaign_validation, placement, output)

    raw = output.read_bytes()
    report = json.loads(raw)
    assert raw == producer.canonical_json(report).encode("ascii")
    assert report["verdict"] == "PASS"
    assert report["source"]["analysis_revision"] == committed_producer[
        "analysis_revision"
    ]
    assert report["source"]["generator_source_sha256"] == committed_producer[
        "generator_source_sha256"
    ]
    assert report["claim_boundary"] == producer.CLAIM_BOUNDARY
    assert len(report["pairs"]) == 5
    assert len(report["pairwise_comparisons"]) == 10
    assert report["aggregate"]["survivor_union_ids"] == [
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
    ]
    assert report["aggregate"]["survivor_intersection_ids"] == [
        10,
        12,
        13,
        15,
        16,
        17,
        18,
        19,
        20,
        24,
        25,
        26,
    ]
    assert report["aggregate"]["root_set_overlap"] == {
        "minimum": 14,
        "median": 17.0,
        "maximum": 19,
    }
    assert report["aggregate"]["exact_position_matches"] == {
        "minimum": 0,
        "median": 1.0,
        "maximum": 3,
    }
    assert report["aggregate"]["validated_paired_ratio_ppm"] == {
        "values": [974137, 965951, 974648, 975090, 1043785],
        "median": 974648,
        "negative_count": 4,
        "positive_count": 1,
        "zero_count": 0,
    }


def test_real_producer_rejects_modified_placement_bytes(
    tmp_path: Path,
    committed_producer: dict[str, str],
) -> None:
    workspace = REPOSITORY.parent
    campaign_root = REPOSITORY / "results/cert13-n31-campaign-v13-7adabc83-r50"
    campaign_validation = (
        REPOSITORY
        / "results/.cert13-n31-campaign-v13-7adabc83-r50-ba797cca2eca5d82-preflight/campaign-validation.json"
    )
    placement = (
        workspace
        / "docs/context/thesis/research/evidence/cert13-placement-contrast-e7ab5506/placement-contrast.json"
    )
    if not campaign_root.is_dir() or not placement.is_file():
        pytest.skip("accepted CERT13 evidence is not present")
    modified = tmp_path / "placement.json"
    modified.write_bytes(placement.read_bytes() + b" ")

    with pytest.raises(producer.RankingRepeatabilityError, match="SHA-256 differs"):
        producer.build_report(
            campaign_root,
            campaign_validation,
            modified,
            tmp_path / "output.json",
        )


def test_producer_provenance_rejects_worktree_bytes_not_in_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def git_bytes(*arguments: str) -> bytes:
        if arguments == ("rev-parse", "HEAD"):
            return ("b" * 40 + "\n").encode("ascii")
        if arguments == ("show", f"{'b' * 40}:{producer.GENERATOR_ID}"):
            return b"different committed bytes\n"
        raise AssertionError(arguments)

    monkeypatch.setattr(producer, "_git_bytes", git_bytes)
    with pytest.raises(
        producer.RankingRepeatabilityError,
        match="differs from the recorded analysis revision",
    ):
        producer._committed_source_provenance()


def test_producer_provenance_binds_exact_committed_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "c" * 40
    source = SCRIPT.read_bytes()

    def git_bytes(*arguments: str) -> bytes:
        if arguments == ("rev-parse", "HEAD"):
            return (revision + "\n").encode("ascii")
        if arguments == ("show", f"{revision}:{producer.GENERATOR_ID}"):
            return source
        raise AssertionError(arguments)

    monkeypatch.setattr(producer, "_git_bytes", git_bytes)
    assert producer._committed_source_provenance() == {
        "analysis_revision": revision,
        "generator_source_sha256": producer._sha256_bytes(source),
    }
