from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

PATH = Path(__file__).resolve().parents[1] / "n7-path-timeout-quorum" / "sustained_role_v8_profile.py"
spec = importlib.util.spec_from_file_location("w19_v8_profile_test", PATH); assert spec and spec.loader
subject = importlib.util.module_from_spec(spec); spec.loader.exec_module(subject)

RUN = PATH.parents[1] / "n7-crash-recovery" / "run.py"
run_spec = importlib.util.spec_from_file_location("n7_v8_profile_writer_test", RUN)
assert run_spec and run_spec.loader
writer = importlib.util.module_from_spec(run_spec)
sys.modules[run_spec.name] = writer
run_spec.loader.exec_module(writer)


def test_v8_freezes_the_40_second_equal_metric_window() -> None:
    subject.validate_profile(subject.FROZEN_PROFILE)
    timing = subject.deadlines(10)
    assert timing["measurement_start_ns"] == 32_000_000_010
    assert timing["measurement_end_ns"] - timing["measurement_start_ns"] == 40_000_000_000


def test_v8_rejects_a_short_or_mutated_profile() -> None:
    mutated = dict(subject.FROZEN_PROFILE); mutated["anchor_to_horizon_ns"] = 60_000_000_000
    with pytest.raises(subject.V8ProfileError):
        subject.validate_profile(mutated)

    # Python considers 5 == 5.0, but the frozen JSON bytes do not.
    mutated = dict(subject.FROZEN_PROFILE); mutated["quorum"] = 5.0
    with pytest.raises(subject.V8ProfileError):
        subject.validate_profile(mutated)


def test_v8_freezes_exact_n7_writer_materialization_inputs() -> None:
    materialization = subject.materialization_profile()
    subject.validate_materialization_profile(materialization)
    assert materialization["profile_id"] == subject.PROFILE_ID
    assert materialization["transition_requests"] == [subject.CONSENSUS_PROFILE["transition"]]
    assert writer._profile_throughput_windows(materialization) == tuple(
        materialization["throughput_windows"]
    )

    materialization["profile_id"] = "n7-path-local-timeout-quorum-v4"
    with pytest.raises(subject.V8ProfileError, match="materialization"):
        subject.validate_materialization_profile(materialization)

    materialization = subject.materialization_profile()
    materialization["profile_version"] = 8.0
    with pytest.raises(subject.V8ProfileError, match="materialization"):
        subject.validate_materialization_profile(materialization)


def test_v8_native_overlay_freezes_actor_tree_and_82_second_reserve() -> None:
    start = 9
    overlay = subject.native_actor_overlay(
        scheduled_start_monotonic_ns=start,
        scheduled_end_monotonic_ns=start + subject.SCHEDULED_START_TO_HORIZON_NS,
    )
    assert overlay == (
        "--experiment-byzantine-mode", "role_scoped_persistent_selected_omission_v1",
        "--experiment-byzantine-window", subject.PROFILE_ID,
        "--experiment-rotating-omission-actors", "1",
        "--experiment-byzantine-window-start-monotonic-ns", str(start),
        "--experiment-byzantine-window-end-monotonic-ns", str(start + subject.SCHEDULED_START_TO_HORIZON_NS),
        "--experiment-byzantine-max-omissions-per-proposal", "1",
        "--experiment-rotating-omission-context-limit", "100000",
        "--experiment-byzantine-first-omission-tree", "4",
    )
    with pytest.raises(subject.V8ProfileError, match="82 seconds"):
        subject.native_actor_overlay(scheduled_start_monotonic_ns=start,
                                     scheduled_end_monotonic_ns=start + subject.SCHEDULED_START_TO_HORIZON_NS - 1)
    with pytest.raises(subject.V8ProfileError, match="82 seconds"):
        subject.native_actor_overlay(scheduled_start_monotonic_ns=0,
                                     scheduled_end_monotonic_ns=subject.SCHEDULED_START_TO_HORIZON_NS)


def test_writer_throughput_allowlist_does_not_admit_an_unrecognized_v8_lookalike() -> None:
    profile = subject.materialization_profile()
    profile["profile_id"] = subject.PROFILE_ID + "-lookalike"
    with pytest.raises(writer.RunnerError, match="exact throughput"):
        writer._profile_throughput_windows(profile)
