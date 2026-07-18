from __future__ import annotations

import functools
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
from types import SimpleNamespace
from typing import Any, Mapping

import pytest


SMOKE_DIRECTORY = Path(__file__).resolve().parents[1]
RUNNER_PATH = SMOKE_DIRECTORY / "run_smoke.py"
PROFILE_PATH = SMOKE_DIRECTORY / "profile.json"
SOURCE_PATH = Path(__file__).resolve().parents[4] / "src" / "hotstuff.cpp"
TREE = "fan:2 pipe:2 0 1 2 3 4 5 6"
SURVIVORS = (0, 1, 2, 3, 5)
HASH_A = "a" * 64
HASH_B = "b" * 64


@functools.cache
def smoke() -> Any:
    if not RUNNER_PATH.is_file():
        pytest.fail(
            f"seven-replica smoke runner is missing: {RUNNER_PATH}",
            pytrace=False,
        )
    spec = importlib.util.spec_from_file_location(
        "adaptive_seven_replica_smoke", RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def field(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def permission_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def marker(
    *,
    replica: int = 0,
    height: int = 100,
    block_hash: str = HASH_A,
    tx_count: int = 1,
    monotonic_ns: int = 1_000_000_000,
    epoch: int = 0,
    tree: int = 0,
    root: int = 0,
) -> str:
    return (
        "2026-07-18 10:00:00 [hotstuff info] KAURI_DEMO commit "
        f"replica={replica} height={height} epoch={epoch} tree={tree} "
        f"root={root} "
        f"hash={block_hash} tx_count={tx_count} "
        f"monotonic_ns={monotonic_ns}"
    )


def event(
    *,
    replica: int = 0,
    height: int = 100,
    block_hash: str = HASH_A,
    tx_count: int = 1,
    monotonic_ns: int = 1_000_000_000,
    epoch: int = 0,
    tree: int = 0,
    root: int = 0,
) -> object:
    return smoke().parse_commit_marker(
        marker(
            replica=replica,
            height=height,
            block_hash=block_hash,
            tx_count=tx_count,
            monotonic_ns=monotonic_ns,
            epoch=epoch,
            tree=tree,
            root=root,
        ),
        source_replica=replica,
    )


def events_for_bucket_tps(
    values: tuple[float, ...], *, start_ns: int = 1_000_000_000
) -> list[object]:
    events: list[object] = []
    for bucket_index, tps in enumerate(values):
        tx_count = int(tps * 5)
        if tx_count == 0:
            continue
        events.append(
            event(
                height=100 + bucket_index,
                block_hash=f"{bucket_index + 1:064x}",
                tx_count=tx_count,
                monotonic_ns=start_ns
                + bucket_index * 5_000_000_000
                + 100_000_000,
            )
        )
    return events


def analyze_fixture(
    run_directory: Path,
    *,
    observer_schedule: tuple[tuple[float, int], ...],
    manager_log: str,
) -> dict[str, object]:
    """Analyze one deterministic run without launching replica processes."""
    measurement_start_ns = 1_000_000_000
    crash_offset_s = 15.0
    for replica in range(7):
        schedule = (
            observer_schedule
            if replica in SURVIVORS
            else tuple(
                entry
                for entry in observer_schedule
                if entry[0] < crash_offset_s
            )
        )
        lines = [
            marker(
                replica=replica,
                height=100 + index,
                block_hash=f"{index + 1:064x}",
                tx_count=tx_count,
                monotonic_ns=(
                    measurement_start_ns + int(offset_s * 1_000_000_000)
                ),
            )
            for index, (offset_s, tx_count) in enumerate(schedule)
        ]
        (run_directory / f"replica-{replica}.log").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    (run_directory / "manager.log").write_text(
        manager_log + "\n", encoding="utf-8"
    )

    crash_ns = measurement_start_ns + int(
        crash_offset_s * 1_000_000_000
    )
    crash_events = [
        {
            "replica": replica,
            "requested_monotonic_ns": crash_ns,
        }
        for replica in (4, 6)
    ]
    exit_observations = [
        {
            "replica": replica,
            "popen_return_code": -signal.SIGKILL,
            "signal_number": signal.SIGKILL,
            "phase": "post_crash",
        }
        for replica in (4, 6)
    ]
    return smoke().analyze_and_write_artifacts(
        run_directory,
        json.loads(PROFILE_PATH.read_text(encoding="utf-8")),
        measurement_start_event_ns=measurement_start_ns,
        event_clock_offset_ns=0,
        crash_events=crash_events,
        exit_observations=exit_observations,
        runtime_error=None,
        postflight_listeners=(),
    )


class TestFrozenProfile:
    def test_profile_has_exact_seven_replica_leaf_crash_scenario(self) -> None:
        assert PROFILE_PATH.is_file(), f"missing frozen profile: {PROFILE_PATH}"
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))

        assert profile["replica_ids"] == list(range(7))
        assert len(set(profile["replica_ids"])) == 7
        assert profile["f"] == 2
        assert profile["tree"] == TREE
        assert profile["observer"] == 0
        assert profile["crash_targets"] == [4, 6]
        assert profile["bucket_width_s"] == 5
        assert profile["baseline_bucket_count"] == 3
        assert profile["grace_bucket_count"] == 1
        assert profile["post_bucket_count"] == 3
        assert profile["minimum_recovery_ratio"] == pytest.approx(0.80)
        assert profile["max_stall_s"] == pytest.approx(10)

    def test_tree_files_keep_both_crash_targets_as_distinct_parent_leaves(
        self,
    ) -> None:
        epoch0 = SMOKE_DIRECTORY / "epoch0.tree"
        epoch1 = SMOKE_DIRECTORY / "epoch1.tree"
        assert epoch0.read_text(encoding="utf-8").strip() == TREE
        assert epoch1.read_text(encoding="utf-8").strip() == TREE

        members = tuple(int(token) for token in TREE.split()[2:])
        fanout = 2
        parents = {
            member: members[(index - 1) // fanout]
            for index, member in enumerate(members)
            if index > 0
        }
        parents_with_children = set(parents.values())
        leaves = set(members) - parents_with_children

        assert {4, 6} <= leaves
        assert parents[4] == 1
        assert parents[6] == 2
        assert parents[4] != parents[6]
        assert 0 not in {4, 6}


class TestCommitMarkerContract:
    def test_real_commit_marker_emits_measurement_fields(self) -> None:
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("void HotStuffBase::record_adaptive_commit_marker")
        end = source.index(
            "void HotStuffBase::finish_adaptive_epoch_commit", start
        )
        marker_source = source[start:end]

        assert '"hash=%s' in marker_source
        assert "tx_count=%" in marker_source
        assert "monotonic_ns=%" in marker_source
        assert "steady_clock" in marker_source
        assert "get_cmds().size()" in marker_source

    def test_parser_requires_full_hash_transaction_count_and_clock(self) -> None:
        parsed = event(tx_count=3, monotonic_ns=9_500_000_000)

        assert field(parsed, "source_replica") == 0
        assert field(parsed, "replica") == 0
        assert field(parsed, "height") == 100
        assert field(parsed, "block_hash") == HASH_A
        assert field(parsed, "tx_count") == 3
        assert field(parsed, "monotonic_ns") == 9_500_000_000

        required_tokens = (
            f" hash={HASH_A}",
            " tx_count=1",
            " monotonic_ns=1000000000",
        )
        for token in required_tokens:
            with pytest.raises(smoke().SmokeError):
                smoke().parse_commit_marker(
                    marker().replace(token, ""), source_replica=0
                )

    def test_parser_rejects_logger_source_replica_mismatch(self) -> None:
        with pytest.raises(smoke().SmokeError, match="source|replica"):
            smoke().parse_commit_marker(marker(replica=1), source_replica=0)


class TestCrashOwnership:
    def test_only_recorded_leaf_process_groups_receive_sigkill(self) -> None:
        records = {
            replica: SimpleNamespace(
                replica=replica,
                pid=1_000 + replica,
                pgid=2_000 + replica,
            )
            for replica in range(7)
        }
        calls: list[tuple[int, signal.Signals]] = []

        smoke().inject_crashes(
            records,
            crash_targets=(4, 6),
            killpg=lambda pgid, sig: calls.append((pgid, sig)),
        )

        assert calls == [
            (records[4].pgid, signal.SIGKILL),
            (records[6].pgid, signal.SIGKILL),
        ]

    def test_rejects_stale_stored_pgid_without_signalling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = SimpleNamespace(replica=4, pid=1_004, pgid=2_004)
        calls: list[tuple[int, signal.Signals]] = []
        monkeypatch.setattr(smoke().os, "getpgrp", lambda: 9_000)
        monkeypatch.setattr(
            smoke().os,
            "getpgid",
            lambda pid: 3_004 if pid == record.pid else 9_001,
        )

        with pytest.raises(
            smoke().SmokeError, match=r"PGID|process group|mismatch"
        ):
            smoke().inject_crashes(
                {4: record},
                crash_targets=(4,),
                killpg=lambda pgid, sig: calls.append((pgid, sig)),
            )

        assert calls == []

    def test_rejects_launchers_own_pgid_without_signalling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = SimpleNamespace(replica=4, pid=1_004, pgid=2_004)
        calls: list[tuple[int, signal.Signals]] = []
        monkeypatch.setattr(smoke().os, "getpgrp", lambda: record.pgid)
        monkeypatch.setattr(smoke().os, "getpgid", lambda pid: record.pgid)

        with pytest.raises(
            smoke().SmokeError, match=r"unsafe|launcher|process group"
        ):
            smoke().inject_crashes(
                {4: record},
                crash_targets=(4,),
                killpg=lambda pgid, sig: calls.append((pgid, sig)),
            )

        assert calls == []

    def test_expected_crash_exits_pass_but_survivor_exit_fails(self) -> None:
        expected = [
            {
                "replica": replica,
                "popen_return_code": -signal.SIGKILL,
                "signal_number": signal.SIGKILL,
                "phase": "post_crash",
            }
            for replica in (4, 6)
        ]
        smoke().validate_process_exits(expected, expected_crashes=(4, 6))

        with pytest.raises(smoke().SmokeError, match="survivor|replica 3"):
            smoke().validate_process_exits(
                expected
                + [
                    {
                        "replica": 3,
                        "popen_return_code": 1,
                        "signal_number": None,
                        "phase": "post_crash",
                    }
                ],
                expected_crashes=(4, 6),
            )


class TestCommitIntegrity:
    def test_deduplicates_by_full_hash_and_rejects_height_conflict(self) -> None:
        duplicate = event(monotonic_ns=1_100_000_000)
        deduplicated = smoke().validate_and_deduplicate_commits(
            [duplicate, event()]
        )
        assert len(deduplicated) == 1
        assert field(deduplicated[0], "block_hash") == HASH_A
        assert field(deduplicated[0], "monotonic_ns") == 1_000_000_000

        with pytest.raises(smoke().SmokeError, match="height|conflict"):
            smoke().validate_and_deduplicate_commits(
                [event(), event(block_hash=HASH_B)]
            )

    @pytest.mark.parametrize(
        ("field_name", "conflicting_value"),
        (
            ("tx_count", 2),
            ("epoch", 1),
            ("tree", 1),
            ("root", 1),
        ),
    )
    def test_rejects_same_hash_and_height_with_conflicting_metadata(
        self, field_name: str, conflicting_value: int
    ) -> None:
        conflicting = event(**{field_name: conflicting_value})

        with pytest.raises(
            smoke().SmokeError, match="metadata|conflict|ambiguous"
        ):
            smoke().validate_and_deduplicate_commits([event(), conflicting])

    def test_survivors_must_agree_on_hash_for_common_heights(self) -> None:
        agreeing = {
            replica: [
                event(replica=replica, height=100, block_hash=HASH_A),
                event(
                    replica=replica,
                    height=101,
                    block_hash=HASH_B,
                    monotonic_ns=2_000_000_000,
                ),
            ]
            for replica in SURVIVORS
        }
        smoke().validate_survivor_agreement(agreeing, SURVIVORS)

        disagreeing = dict(agreeing)
        disagreeing[5] = [
            event(replica=5, height=100, block_hash="c" * 64)
        ]
        with pytest.raises(smoke().SmokeError, match="height|conflict|agree"):
            smoke().validate_survivor_agreement(disagreeing, SURVIVORS)

    def test_final_validator_rejects_pre_crash_conflict_from_crash_target(
        self, tmp_path: Path
    ) -> None:
        observer_schedule = (
            (0.1, 50),
            (5.1, 50),
            (10.1, 50),
            (15.1, 1),
            (20.1, 40),
            (25.1, 40),
            (30.1, 40),
        )
        manager_log = (
            "KAURI_REPUTATION update reporter=1 target=4 "
            "outcome=timeout delta=-1 score=-1\n"
            "KAURI_REPUTATION update reporter=2 target=6 "
            "outcome=timeout delta=-1 score=-1"
        )
        passing = analyze_fixture(
            tmp_path,
            observer_schedule=observer_schedule,
            manager_log=manager_log,
        )
        assert passing["classification"] == "PASS"

        crash_target_log = tmp_path / "replica-4.log"
        agreed_marker = marker(
            replica=4,
            height=101,
            block_hash=f"{2:064x}",
            tx_count=50,
            monotonic_ns=6_100_000_000,
        )
        conflicting_marker = marker(
            replica=4,
            height=101,
            block_hash=HASH_B,
            tx_count=50,
            monotonic_ns=6_100_000_000,
        )
        crash_target_text = crash_target_log.read_text(encoding="utf-8")
        assert agreed_marker in crash_target_text
        crash_target_log.write_text(
            crash_target_text.replace(
                agreed_marker, conflicting_marker, 1
            ),
            encoding="utf-8",
        )

        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        crash_events = json.loads(
            (tmp_path / "crash-events.json").read_text(encoding="utf-8")
        )
        exit_observations = [
            {
                "replica": replica,
                "popen_return_code": -signal.SIGKILL,
                "signal_number": signal.SIGKILL,
                "phase": "post_crash",
            }
            for replica in (4, 6)
        ]
        result = smoke().analyze_and_write_artifacts(
            tmp_path,
            profile,
            measurement_start_event_ns=1_000_000_000,
            event_clock_offset_ns=0,
            crash_events=crash_events,
            exit_observations=exit_observations,
            runtime_error=None,
            postflight_listeners=(),
        )

        assert result["classification"] == "FAIL"
        assert any(
            "height 101" in reason
            and ("conflict" in reason or "agree" in reason)
            for reason in result["reasons"]
        )


class TestGeneratedSecretPermissions:
    @pytest.mark.parametrize(
        "ambient_umask", (0o000, 0o022), ids=("permissive", "default")
    )
    def test_generated_run_directories_are_owner_only(
        self, tmp_path: Path, ambient_umask: int
    ) -> None:
        results_root = tmp_path / f"results-{ambient_umask:o}"
        previous_umask = os.umask(ambient_umask)
        try:
            run_directory = smoke().create_run_directory(results_root)
        finally:
            os.umask(previous_umask)

        assert {
            "results_root": permission_mode(results_root),
            "run_directory": permission_mode(run_directory),
        } == {
            "results_root": 0o700,
            "run_directory": 0o700,
        }

    @pytest.mark.parametrize(
        "ambient_umask", (0o000, 0o022), ids=("permissive", "default")
    )
    def test_private_identity_and_replica_files_are_owner_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ambient_umask: int,
    ) -> None:
        replica_count = 7

        def fake_keygen(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if Path(command[0]).name == "bls-keygen":
                output = "".join(
                    f"pub:bls-pub-{replica} sec:bls-sec-{replica}\n"
                    for replica in range(replica_count)
                )
            else:
                output = "".join(
                    f"crt:tls-crt-{replica} sec:tls-sec-{replica} "
                    f"cid:tls-cid-{replica}\n"
                    for replica in range(replica_count)
                )
            return subprocess.CompletedProcess(command, 0, output, "")

        monkeypatch.setattr(smoke().subprocess, "run", fake_keygen)
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        previous_umask = os.umask(ambient_umask)
        try:
            run_directory = smoke().create_run_directory(
                tmp_path / f"secret-results-{ambient_umask:o}"
            )
            config_directory = run_directory / "config"
            config_directory.mkdir(mode=0o700)
            bls, tls = smoke().generate_identities(
                Path("bls-keygen"),
                Path("tls-keygen"),
                config_directory,
                replica_count,
            )
            _, replica_configs, _, _ = smoke().write_configs(
                run_directory,
                profile,
                bls,
                tls,
                peer_port=25_100,
                client_port=26_100,
            )
        finally:
            os.umask(previous_umask)

        private_paths = [
            config_directory / "bls-identities.txt",
            config_directory / "tls-identities.txt",
            *replica_configs,
        ]
        assert {
            path.name: permission_mode(path) for path in private_paths
        } == {path.name: 0o600 for path in private_paths}


class TestClockCalibration:
    def test_maps_runner_crash_boundary_into_the_cpp_event_clock(self) -> None:
        anchor_event_ns = 274_000_000_000_000
        observed_runner_ns = 210_000_000_000_000

        offset = smoke().calibrate_event_clock(
            anchor_event_ns, observed_runner_ns
        )

        assert offset == 64_000_000_000_000
        assert smoke().runner_to_event_clock(observed_runner_ns, offset) == (
            anchor_event_ns
        )
        runner_crash_ns = observed_runner_ns + 15_000_000_000
        assert smoke().runner_to_event_clock(runner_crash_ns, offset) == (
            anchor_event_ns + 15_000_000_000
        )

    def test_calibrated_measurement_start_retains_nonzero_buckets(self) -> None:
        anchor_event_ns = 274_000_000_000_000
        observed_runner_ns = 210_000_000_000_000
        offset = smoke().calibrate_event_clock(
            anchor_event_ns, observed_runner_ns
        )
        event_start_ns = smoke().runner_to_event_clock(
            observed_runner_ns, offset
        )
        commits = [
            event(
                height=100,
                block_hash=HASH_A,
                tx_count=10,
                monotonic_ns=event_start_ns + 100_000_000,
            ),
            event(
                height=101,
                block_hash=HASH_B,
                tx_count=5,
                monotonic_ns=event_start_ns + 5_100_000_000,
            ),
        ]

        uncalibrated = smoke().build_throughput_buckets(
            commits,
            measurement_start_ns=observed_runner_ns,
            bucket_width_s=5,
            baseline_bucket_count=3,
            grace_bucket_count=1,
            post_bucket_count=3,
        )
        calibrated = smoke().build_throughput_buckets(
            commits,
            measurement_start_ns=event_start_ns,
            bucket_width_s=5,
            baseline_bucket_count=3,
            grace_bucket_count=1,
            post_bucket_count=3,
        )

        assert sum(field(bucket, "tx_count") for bucket in uncalibrated) == 0
        assert [field(bucket, "tx_count") for bucket in calibrated[:2]] == [
            10,
            5,
        ]


class TestThroughputAnalysis:
    def test_half_open_five_second_buckets_retain_explicit_zeroes(self) -> None:
        start_ns = 1_000_000_000
        commits = [
            event(
                height=100,
                block_hash=HASH_A,
                tx_count=10,
                monotonic_ns=start_ns + 100_000_000,
            ),
            event(
                height=101,
                block_hash=HASH_B,
                tx_count=5,
                monotonic_ns=start_ns + 10_100_000_000,
            ),
        ]
        buckets = smoke().build_throughput_buckets(
            commits,
            measurement_start_ns=start_ns,
            bucket_width_s=5,
            baseline_bucket_count=3,
            grace_bucket_count=1,
            post_bucket_count=3,
        )

        assert len(buckets) == 7
        assert [field(bucket, "phase") for bucket in buckets] == [
            "baseline",
            "baseline",
            "baseline",
            "grace",
            "post",
            "post",
            "post",
        ]
        assert [field(bucket, "tx_count") for bucket in buckets] == [
            10,
            0,
            5,
            0,
            0,
            0,
            0,
        ]
        assert [field(bucket, "tps") for bucket in buckets[:3]] == [
            pytest.approx(2.0),
            pytest.approx(0.0),
            pytest.approx(1.0),
        ]
        assert field(buckets[3], "is_grace") is True

    def test_medians_and_exact_eighty_percent_boundary_pass(self) -> None:
        observer_events = events_for_bucket_tps((10, 10, 10, 0, 8, 8, 8))
        buckets = smoke().build_throughput_buckets(
            observer_events,
            measurement_start_ns=1_000_000_000,
            bucket_width_s=5,
            baseline_bucket_count=3,
            grace_bucket_count=1,
            post_bucket_count=3,
        )
        verdict = smoke().evaluate_throughput(
            buckets,
            observer_events=observer_events,
            measurement_start_ns=1_000_000_000,
            minimum_recovery_ratio=0.80,
            max_stall_s=10,
        )

        assert field(verdict, "classification") == "PASS"
        assert field(verdict, "baseline_median_tps") == pytest.approx(10)
        assert field(verdict, "post_median_tps") == pytest.approx(8)
        assert field(verdict, "recovery_ratio") == pytest.approx(0.8)

    def test_below_threshold_is_degraded_and_long_stall_is_not_hidden(
        self,
    ) -> None:
        degraded_events = events_for_bucket_tps(
            (10, 10, 10, 0, 7.8, 7.8, 7.8)
        )
        degraded = smoke().build_throughput_buckets(
            degraded_events,
            measurement_start_ns=1_000_000_000,
            bucket_width_s=5,
            baseline_bucket_count=3,
            grace_bucket_count=1,
            post_bucket_count=3,
        )
        degraded_verdict = smoke().evaluate_throughput(
            degraded,
            observer_events=degraded_events,
            measurement_start_ns=1_000_000_000,
            minimum_recovery_ratio=0.80,
            max_stall_s=10,
        )
        assert field(degraded_verdict, "classification") == "DEGRADED"
        assert field(degraded_verdict, "recovery_ratio") == pytest.approx(0.78)

        stalled_events = events_for_bucket_tps((10, 10, 10, 0, 0, 0, 8))
        stalled = smoke().build_throughput_buckets(
            stalled_events,
            measurement_start_ns=1_000_000_000,
            bucket_width_s=5,
            baseline_bucket_count=3,
            grace_bucket_count=1,
            post_bucket_count=3,
        )
        stalled_verdict = smoke().evaluate_throughput(
            stalled,
            observer_events=stalled_events,
            measurement_start_ns=1_000_000_000,
            minimum_recovery_ratio=0.80,
            max_stall_s=10,
        )
        assert field(stalled_verdict, "classification") == "FAIL"
        assert field(stalled_verdict, "max_zero_interval_s") == pytest.approx(
            15.1
        )

    def test_exact_observer_gap_fails_even_when_zero_bucket_run_is_short(
        self, tmp_path: Path
    ) -> None:
        result = analyze_fixture(
            tmp_path,
            observer_schedule=(
                (0.1, 50),
                (5.1, 50),
                (10.1, 50),
                (15.1, 1),
                (28.9, 40),
                (33.9, 40),
            ),
            manager_log=(
                "KAURI_REPUTATION update reporter=1 target=4 "
                "outcome=timeout delta=-1 score=-1\n"
                "KAURI_REPUTATION update reporter=2 target=6 "
                "outcome=timeout delta=-1 score=-1"
            ),
        )

        assert result["classification"] == "FAIL"
        assert field(
            result["throughput"], "max_zero_interval_s"
        ) == pytest.approx(13.8)


class TestSevenMemberReputationParsing:
    def test_member_six_is_valid_but_member_seven_is_outside_membership(
        self,
    ) -> None:
        valid = (
            "KAURI_REPUTATION update reporter=6 target=5 "
            "outcome=response delta=1 score=1"
        )
        accepted = smoke().evaluate_reputation_log(valid, tuple(range(7)))
        assert field(accepted, "passed") is True

        invalid = valid.replace("reporter=6", "reporter=7")
        rejected = smoke().evaluate_reputation_log(invalid, tuple(range(7)))
        assert field(rejected, "passed") is False
        assert any(
            "membership" in reason
            for reason in field(rejected, "reasons")
        )

    @pytest.mark.parametrize(
        "manager_log",
        (
            (
                "KAURI_REPUTATION update reporter=bad target=4 "
                "outcome=timeout delta=-1 score=-1"
            ),
            (
                "KAURI_REPUTATION update reporter=1 target=4 "
                "outcome=response delta=1 score=1"
            ),
        ),
        ids=("parse-diagnostic", "missing-timeout-diagnostic"),
    )
    def test_reputation_diagnostics_do_not_change_throughput_verdict(
        self, tmp_path: Path, manager_log: str
    ) -> None:
        result = analyze_fixture(
            tmp_path,
            observer_schedule=(
                (0.1, 50),
                (5.1, 50),
                (10.1, 50),
                (15.1, 1),
                (20.1, 40),
                (25.1, 40),
                (30.1, 40),
            ),
            manager_log=manager_log,
        )

        assert field(result["throughput"], "classification") == "PASS"
        assert result["classification"] == "PASS"
        assert result["observed_reputation_timeout_pairs"] == []
        assert result["reputation_diagnostic_passed"] is False
        assert result["reputation_diagnostics"]
