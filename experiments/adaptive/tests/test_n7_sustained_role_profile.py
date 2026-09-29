from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "sustained_role_profile.py"
spec = importlib.util.spec_from_file_location("n7_sustained_role_profile_test", PATH)
assert spec and spec.loader
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


def test_profile_emits_only_the_role_scoped_scheduled_native_mode() -> None:
    start = 100
    end = start + subject.COMMON_HORIZON_NS
    overlay = subject.argv_overlay(
        window_start_monotonic_ns=start,
        window_end_monotonic_ns=end,
    )

    assert overlay == (
        "--experiment-byzantine-mode",
        "role_scoped_persistent_selected_omission_v1",
        "--experiment-byzantine-window",
        "n7-role-scoped-persistent-selected-omission-v1",
        "--experiment-rotating-omission-actors", "1",
        "--experiment-byzantine-window-start-monotonic-ns", str(start),
        "--experiment-byzantine-window-end-monotonic-ns", str(end),
        "--experiment-byzantine-max-omissions-per-proposal", "1",
        "--experiment-rotating-omission-context-limit", "100000",
    )
    assert "--experiment-omit-outbound-aggregate" not in overlay
    assert "--experiment-byzantine-configuration" not in overlay


@pytest.mark.parametrize(
    ("start", "end"),
    ((0, 60_000_000_000), (1, 1), (1, 60_000_000_000)),
)
def test_profile_rejects_windows_that_do_not_cover_the_common_horizon(
    start: int, end: int,
) -> None:
    with pytest.raises(subject.SustainedRoleProfileError, match="window"):
        subject.argv_overlay(
            window_start_monotonic_ns=start,
            window_end_monotonic_ns=end,
        )


def test_preflight_is_explicitly_non_executing_and_binds_one_hard_actor() -> None:
    preflight = subject.preflight(
        window_start_monotonic_ns=10,
        window_end_monotonic_ns=10 + subject.COMMON_HORIZON_NS + 1,
    )

    assert preflight["status"] == "PREFLIGHT_ONLY_NO_EXECUTION"
    assert preflight["protocol"] == {
        "replica_ids": list(range(7)), "fault_threshold": 2, "quorum": 5,
    }
    assert preflight["fault"]["actor_id"] == 1
    assert preflight["fault"]["hard_actor_count"] == 1
    assert preflight["fault"]["responsive_degraded_actor_count"] == 0
    assert preflight["fault"]["responsive_omission_period"] == 0
