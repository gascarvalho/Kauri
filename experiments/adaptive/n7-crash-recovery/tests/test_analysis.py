"""Synthetic unit fixtures only; these results are not experiment evidence."""

from __future__ import annotations

from dataclasses import replace
import functools
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import pytest


SCENARIO_DIRECTORY = Path(__file__).resolve().parents[1]
ANALYSIS_PATH = SCENARIO_DIRECTORY / "analysis.py"
SYNTHETIC_RUN_ID = "synthetic-non-evidence"
SOURCE_INSTANCE = "synthetic-replica-2-instance"
DIGEST_0 = "a" * 64
DIGEST_1 = "b" * 64
DIGEST_2 = "c" * 64
HASH_1 = "1" * 64
HASH_2 = "2" * 64
HASH_3 = "3" * 64
HASH_4 = "4" * 64
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
LEADERS = {
    (0, DIGEST_0, 0): 0,
    (0, DIGEST_0, 1): 1,
    (1, DIGEST_1, 0): 2,
    (2, DIGEST_2, 0): 6,
}


@functools.cache
def analysis() -> Any:
    spec = importlib.util.spec_from_file_location(
        "n7_crash_recovery_analysis", ANALYSIS_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def event_line(
    *,
    sequence: int,
    timestamp_ns: int,
    height: int = 1,
    block_hash: str = HASH_1,
    transaction_count: int = 50,
    epoch_number: int = 0,
    tree_id: int = 0,
    epoch_digest: str = DIGEST_0,
    run_id: str = SYNTHETIC_RUN_ID,
    source_kind: str = "replica",
    source_id: str = "replica-2",
    source_instance: str = SOURCE_INSTANCE,
    designated_observer: bool = True,
    event_type: str = "block.committed",
    view_generation: int | None = 1,
    commit_batch_index: int = 0,
    extra_envelope: Mapping[str, object] | None = None,
    extra_payload: Mapping[str, object] | None = None,
    extra_proof: Mapping[str, object] | None = None,
) -> str:
    proof: dict[str, object] = {
        "epoch_number": epoch_number,
        "tree_id": tree_id,
        "epoch_digest": epoch_digest,
        "block_hash": block_hash,
    }
    if extra_proof:
        proof.update(extra_proof)
    payload: dict[str, object] = {
        "block_height": height,
        "block_hash": block_hash,
        "parent_hash": None,
        "transaction_count": transaction_count,
        "designated_observer": designated_observer,
        "decision_proof": proof,
        "view_generation": view_generation,
        "commit_batch_index": commit_batch_index,
    }
    if extra_payload:
        payload.update(extra_payload)
    envelope: dict[str, object] = {
        "event_schema_version": 1,
        "run_id": run_id,
        "source_kind": source_kind,
        "source_id": source_id,
        "source_instance": source_instance,
        "source_sequence": sequence,
        "source_monotonic_ns": timestamp_ns,
        "event_type": event_type,
        "payload": payload,
    }
    if extra_envelope:
        envelope.update(extra_envelope)
    return json.dumps(envelope, separators=(",", ":"))


def non_commit_line(*, sequence: int, timestamp_ns: int) -> str:
    envelope = {
        "event_schema_version": 1,
        "run_id": SYNTHETIC_RUN_ID,
        "source_kind": "replica",
        "source_id": "replica-2",
        "source_instance": SOURCE_INSTANCE,
        "source_sequence": sequence,
        "source_monotonic_ns": timestamp_ns,
        "event_type": "process.ready",
        "payload": {"exit_status": None},
    }
    return json.dumps(envelope, separators=(",", ":"))


def parse(text: str, **kwargs: object) -> tuple[object, ...]:
    expected_source_instance = kwargs.pop(
        "expected_source_instance", SOURCE_INSTANCE
    )
    leader_by_configuration = kwargs.pop(
        "leader_by_configuration", LEADERS
    )
    return analysis().parse_commit_events(
        text,
        expected_run_id=SYNTHETIC_RUN_ID,
        leader_by_configuration=leader_by_configuration,
        expected_source_instance=expected_source_instance,
        **kwargs,
    )


class TestCanonicalCommitParsing:
    def test_parses_only_authoritative_commits_and_resolves_leader(self) -> None:
        text = "\n".join(
            [
                non_commit_line(sequence=1, timestamp_ns=1_000_000_000),
                event_line(
                    sequence=2,
                    timestamp_ns=1_000_000_000,
                    epoch_number=1,
                    epoch_digest=DIGEST_1,
                    tree_id=0,
                ),
            ]
        )

        events = parse(text, expected_source_instance=SOURCE_INSTANCE)

        assert len(events) == 1
        commit = events[0]
        assert commit.observer_replica == 2
        assert (commit.source_kind, commit.source_id) == (
            "replica",
            "replica-2",
        )
        assert commit.timestamp_ns == 1_000_000_000
        assert commit.height == 1
        assert commit.block_hash == HASH_1
        assert commit.configuration_key == (1, DIGEST_1, 0)
        assert commit.leader_replica == 2
        assert commit.transaction_count == 50

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"event_schema_version": 2}, "unsupported event_schema_version"),
            ({"run_id": "another-run"}, "run_id mismatch"),
            ({"source_kind": "adaptation_manager"}, "source_kind"),
            ({"source_id": "replica-3"}, "expected replica-2"),
            ({"unknown": 1}, "unknown unknown"),
        ],
    )
    def test_rejects_wrong_or_unknown_envelope_fields(
        self, change: Mapping[str, object], message: str
    ) -> None:
        line = event_line(
            sequence=1,
            timestamp_ns=1,
            extra_envelope=change,
        )
        with pytest.raises(analysis().AnalysisError, match=message):
            parse(line)

    def test_rejects_non_designated_observer_and_wrong_instance(self) -> None:
        with pytest.raises(analysis().AnalysisError, match="designated"):
            parse(
                event_line(
                    sequence=1,
                    timestamp_ns=1,
                    designated_observer=False,
                )
            )

        text = "\n".join(
            [
                non_commit_line(sequence=1, timestamp_ns=1),
                event_line(
                    sequence=2,
                    timestamp_ns=2,
                    source_instance="replacement-instance",
                ),
            ]
        )
        with pytest.raises(analysis().AnalysisError, match="source_instance mismatch"):
            parse(text)

    def test_requires_manifest_bound_source_instance(self) -> None:
        line = event_line(sequence=1, timestamp_ns=1)
        with pytest.raises(analysis().AnalysisError, match="non-empty"):
            parse(line, expected_source_instance="")

    def test_rejects_unknown_commit_and_proof_fields(self) -> None:
        with pytest.raises(analysis().AnalysisError, match="unknown extra"):
            parse(
                event_line(
                    sequence=1,
                    timestamp_ns=1,
                    extra_payload={"extra": 1},
                )
            )
        with pytest.raises(analysis().AnalysisError, match="unknown extra"):
            parse(
                event_line(
                    sequence=1,
                    timestamp_ns=1,
                    extra_proof={"extra": 1},
                )
            )

    def test_rejects_noncanonical_hash_and_mismatched_proof(self) -> None:
        with pytest.raises(analysis().AnalysisError, match="canonical lowercase"):
            parse(
                event_line(
                    sequence=1,
                    timestamp_ns=1,
                    block_hash="A" * 64,
                )
            )
        with pytest.raises(analysis().AnalysisError, match="does not match"):
            parse(
                event_line(
                    sequence=1,
                    timestamp_ns=1,
                    extra_proof={"block_hash": HASH_2},
                )
            )

    def test_rejects_missing_validated_leader_mapping(self) -> None:
        line = event_line(
            sequence=1,
            timestamp_ns=1,
            epoch_number=7,
            epoch_digest="7" * 64,
            tree_id=4,
        )
        with pytest.raises(analysis().AnalysisError, match="missing validated leader"):
            parse(line)

    @pytest.mark.parametrize(
        "second_line, message",
        [
            (event_line(sequence=1, timestamp_ns=2, height=2, block_hash=HASH_2),
             "source_sequence"),
            (event_line(sequence=2, timestamp_ns=0, height=2, block_hash=HASH_2),
             "regressed"),
        ],
    )
    def test_rejects_non_monotonic_source_stream(
        self, second_line: str, message: str
    ) -> None:
        text = "\n".join(
            [event_line(sequence=1, timestamp_ns=1), second_line]
        )
        with pytest.raises(analysis().AnalysisError, match=message):
            parse(text)

    def test_equal_source_timestamps_remain_valid(self) -> None:
        text = "\n".join(
            [
                event_line(sequence=1, timestamp_ns=1),
                event_line(
                    sequence=2,
                    timestamp_ns=1,
                    height=2,
                    block_hash=HASH_2,
                ),
            ]
        )
        assert len(parse(text)) == 2

    def test_identical_hash_replay_is_counted_once(self) -> None:
        replay = "\n".join(
            [
                event_line(sequence=1, timestamp_ns=1),
                event_line(
                    sequence=2,
                    timestamp_ns=2,
                    block_hash=HASH_1,
                ),
            ]
        )

        events = parse(replay)

        assert len(events) == 1
        assert events[0].source_sequence == 1
        assert events[0].timestamp_ns == 1

    @pytest.mark.parametrize(
        "conflict",
        [
            {"height": 2},
            {"transaction_count": 51},
            {"extra_payload": {"parent_hash": "f" * 64}},
            {"tree_id": 1},
            {"view_generation": 2},
            {"commit_batch_index": 1},
        ],
    )
    def test_same_hash_with_conflicting_metadata_fails(
        self, conflict: Mapping[str, object]
    ) -> None:
        first = event_line(sequence=1, timestamp_ns=1)
        second = event_line(
            sequence=2,
            timestamp_ns=2,
            block_hash=HASH_1,
            **conflict,
        )
        with pytest.raises(
            analysis().AnalysisError, match="conflicting metadata"
        ):
            parse("\n".join([first, second]))

    def test_same_height_with_different_hash_fails(self) -> None:
        height_conflict = "\n".join(
            [
                event_line(sequence=1, timestamp_ns=1),
                event_line(
                    sequence=2,
                    timestamp_ns=2,
                    height=1,
                    block_hash=HASH_2,
                ),
            ]
        )
        with pytest.raises(analysis().AnalysisError, match="conflicting.*height 1"):
            parse(height_conflict)

    def test_accepts_maximum_producer_widths(self) -> None:
        leaders = {
            **LEADERS,
            (UINT32_MAX, DIGEST_0, UINT32_MAX): 6,
        }
        line = event_line(
            sequence=UINT64_MAX,
            timestamp_ns=UINT64_MAX,
            height=UINT64_MAX,
            transaction_count=UINT64_MAX,
            epoch_number=UINT32_MAX,
            tree_id=UINT32_MAX,
            view_generation=UINT64_MAX,
            commit_batch_index=UINT64_MAX,
        )

        events = parse(line, leader_by_configuration=leaders)

        assert len(events) == 1
        assert events[0].transaction_count == UINT64_MAX

    @pytest.mark.parametrize(
        "overflow",
        [
            {"sequence": UINT64_MAX + 1},
            {"timestamp_ns": UINT64_MAX + 1},
            {"height": UINT64_MAX + 1},
            {"transaction_count": UINT64_MAX + 1},
            {"epoch_number": UINT32_MAX + 1},
            {"tree_id": UINT32_MAX + 1},
            {"view_generation": UINT64_MAX + 1},
            {"commit_batch_index": UINT64_MAX + 1},
        ],
    )
    def test_rejects_values_wider_than_producer_types(
        self, overflow: Mapping[str, int]
    ) -> None:
        arguments: dict[str, object] = {
            "sequence": 1,
            "timestamp_ns": 1,
            **overflow,
        }
        line = event_line(**arguments)

        with pytest.raises(analysis().AnalysisError, match="must be"):
            parse(line)

    def test_rejects_duplicate_json_keys_and_legacy_prefix_by_default(self) -> None:
        valid = event_line(sequence=1, timestamp_ns=1)
        duplicate_key = valid.replace(
            '"event_schema_version":1',
            '"event_schema_version":1,"event_schema_version":1',
            1,
        )
        with pytest.raises(analysis().AnalysisError, match="duplicate JSON field"):
            parse(duplicate_key)
        with pytest.raises(analysis().AnalysisError, match="legacy KAURI_EVENT"):
            parse("KAURI_EVENT " + valid)

    def test_legacy_mixed_log_requires_explicit_diagnostic_mode(self) -> None:
        valid = event_line(sequence=1, timestamp_ns=1)
        mixed = "ordinary diagnostic line\nKAURI_EVENT " + valid

        with pytest.raises(analysis().AnalysisError, match="malformed"):
            parse(mixed)
        events = parse(mixed, allow_legacy_prefix=True)

        assert len(events) == 1


class TestRawThroughput:
    def boundaries(self) -> object:
        return analysis().PhaseBoundaries(
            baseline_start_ns=0,
            crash_ns=11_000_000_000,
            activation_ns=21_000_000_000,
            end_ns=27_000_000_000,
        )

    def commits(self) -> tuple[object, ...]:
        text = "\n".join(
            [
                event_line(
                    sequence=1,
                    timestamp_ns=1_000_000_000,
                    height=1,
                    block_hash=HASH_1,
                    transaction_count=50,
                    tree_id=0,
                ),
                event_line(
                    sequence=2,
                    timestamp_ns=11_000_000_000,
                    height=2,
                    block_hash=HASH_2,
                    transaction_count=20,
                    tree_id=1,
                ),
                event_line(
                    sequence=3,
                    timestamp_ns=21_000_000_000,
                    height=3,
                    block_hash=HASH_3,
                    transaction_count=100,
                    epoch_number=1,
                    epoch_digest=DIGEST_1,
                    tree_id=0,
                ),
            ]
        )
        return parse(text)

    def test_builds_half_open_phase_local_zero_filled_buckets(self) -> None:
        buckets = analysis().build_throughput_buckets(
            self.commits(), self.boundaries()
        )

        assert [bucket.phase for bucket in buckets] == [
            "baseline",
            "baseline",
            "baseline",
            "degraded",
            "degraded",
            "post",
            "post",
        ]
        assert [bucket.elapsed_seconds for bucket in buckets] == [
            5.0,
            5.0,
            1.0,
            5.0,
            5.0,
            5.0,
            1.0,
        ]
        assert [bucket.transaction_count for bucket in buckets] == [
            50,
            0,
            0,
            20,
            0,
            100,
            0,
        ]
        assert [bucket.aggregate_tps for bucket in buckets] == [
            10.0,
            0.0,
            0.0,
            4.0,
            0.0,
            20.0,
            0.0,
        ]
        assert buckets[3].leader_transactions[1] == 20
        assert buckets[5].leader_transactions[2] == 100

    def test_each_row_conserves_seven_leader_columns_exactly(self) -> None:
        buckets = analysis().build_throughput_buckets(
            self.commits(), self.boundaries()
        )
        for bucket in buckets:
            assert len(bucket.leader_tps) == 7
            assert sum(bucket.leader_tps) == bucket.aggregate_tps
            assert sum(bucket.leader_transactions) == bucket.transaction_count
            row = bucket.as_row()
            assert sum(row[f"leader_{leader}_tps"] for leader in range(7)) == (
                row["throughput_tps"]
            )

    def test_computes_raw_phase_medians_including_zeroes(self) -> None:
        result = analysis().analyze_throughput(
            self.commits(), self.boundaries()
        )

        assert result.medians.baseline_tps == pytest.approx(0.0)
        assert result.medians.degraded_tps == pytest.approx(2.0)
        assert result.medians.post_tps == pytest.approx(10.0)

    def test_event_at_activation_enters_post_phase(self) -> None:
        buckets = analysis().build_throughput_buckets(
            self.commits(), self.boundaries()
        )
        degraded_transactions = sum(
            bucket.transaction_count
            for bucket in buckets
            if bucket.phase == "degraded"
        )
        post_transactions = sum(
            bucket.transaction_count
            for bucket in buckets
            if bucket.phase == "post"
        )
        assert degraded_transactions == 20
        assert post_transactions == 100

    def test_events_outside_measurement_window_do_not_contribute(self) -> None:
        outside = analysis().CommitEvent(
            run_id=SYNTHETIC_RUN_ID,
            source_kind="replica",
            source_id="replica-2",
            source_instance=SOURCE_INSTANCE,
            source_sequence=1,
            timestamp_ns=28_000_000_000,
            observer_replica=2,
            height=9,
            block_hash="9" * 64,
            parent_hash=None,
            epoch_number=1,
            tree_id=0,
            epoch_digest=DIGEST_1,
            leader_replica=2,
            transaction_count=999,
            view_generation=1,
            commit_batch_index=0,
        )
        buckets = analysis().build_throughput_buckets(
            (*self.commits(), outside), self.boundaries()
        )
        assert sum(bucket.transaction_count for bucket in buckets) == 170

    def test_builder_rejects_oversized_transaction_before_arithmetic(self) -> None:
        oversized = replace(
            self.commits()[0], transaction_count=UINT64_MAX + 1
        )
        with pytest.raises(
            analysis().AnalysisError, match="throughput transaction_count"
        ):
            analysis().build_throughput_buckets(
                (oversized,), self.boundaries()
            )

    def test_builder_counts_an_exact_hash_replay_once(self) -> None:
        first = self.commits()[0]
        replay = replace(
            first,
            source_sequence=first.source_sequence + 1,
            timestamp_ns=first.timestamp_ns + 1,
        )

        buckets = analysis().build_throughput_buckets(
            (first, replay), self.boundaries()
        )

        assert sum(bucket.commit_count for bucket in buckets) == 1
        assert sum(bucket.transaction_count for bucket in buckets) == 50

    def test_builder_rejects_same_hash_metadata_conflict(self) -> None:
        first = self.commits()[0]
        conflicting = replace(
            first,
            source_sequence=first.source_sequence + 1,
            timestamp_ns=first.timestamp_ns + 1,
            transaction_count=first.transaction_count + 1,
        )
        with pytest.raises(analysis().AnalysisError, match="conflicting metadata"):
            analysis().build_throughput_buckets(
                (first, conflicting), self.boundaries()
            )

    def test_rejects_invalid_boundary_order(self) -> None:
        invalid = analysis().PhaseBoundaries(0, 10, 10, 20)
        with pytest.raises(analysis().AnalysisError, match="baseline < crash"):
            analysis().build_throughput_buckets((), invalid)


def test_builds_four_explicit_unique_commit_windows_with_zeroes_and_medians() -> None:
    windows = (
        analysis().PhaseWindow("baseline", 0, 0, 10_000_000_000),
        analysis().PhaseWindow(
            "degraded", 0, 10_000_000_000, 20_000_000_000
        ),
        analysis().PhaseWindow(
            "containment", 1, 30_000_000_000, 40_000_000_000
        ),
        analysis().PhaseWindow(
            "optimized", 2, 50_000_000_000, 60_000_000_000
        ),
    )
    commits = parse(
        "\n".join(
            (
                event_line(
                    sequence=1,
                    timestamp_ns=1_000_000_000,
                    height=1,
                    block_hash=HASH_1,
                    transaction_count=50,
                ),
                event_line(
                    sequence=2,
                    timestamp_ns=11_000_000_000,
                    height=2,
                    block_hash=HASH_2,
                    transaction_count=20,
                    tree_id=1,
                ),
                event_line(
                    sequence=3,
                    timestamp_ns=31_000_000_000,
                    height=3,
                    block_hash=HASH_3,
                    transaction_count=100,
                    epoch_number=1,
                    epoch_digest=DIGEST_1,
                ),
                event_line(
                    sequence=4,
                    timestamp_ns=31_000_000_001,
                    height=3,
                    block_hash=HASH_3,
                    transaction_count=100,
                    epoch_number=1,
                    epoch_digest=DIGEST_1,
                ),
                event_line(
                    sequence=5,
                    timestamp_ns=51_000_000_000,
                    height=4,
                    block_hash=HASH_4,
                    transaction_count=140,
                    epoch_number=2,
                    epoch_digest=DIGEST_2,
                ),
            )
        )
    )

    result = analysis().analyze_throughput(commits, windows)

    assert [bucket.phase for bucket in result.buckets] == [
        "baseline",
        "baseline",
        "degraded",
        "degraded",
        "containment",
        "containment",
        "optimized",
        "optimized",
    ]
    assert [bucket.transaction_count for bucket in result.buckets] == [
        50,
        0,
        20,
        0,
        100,
        0,
        140,
        0,
    ]
    assert sum(bucket.commit_count for bucket in result.buckets) == 4
    assert result.medians.baseline_tps == pytest.approx(5.0)
    assert result.medians.degraded_tps == pytest.approx(2.0)
    assert result.medians.containment_tps == pytest.approx(10.0)
    assert result.medians.optimized_tps == pytest.approx(14.0)
