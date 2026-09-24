"""Fail-closed CPU-service calibration contracts for future Kauri runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.adaptive.kauri_experiment import cpu_quota_calibration as calibration


def _sample(
    cohort: str,
    quota: int,
    *,
    before: int,
    after: int,
) -> dict[str, object]:
    return {
        "cohort": cohort,
        "requested_quota_percent": quota,
        "observed_quota_per_second_usec": quota * 10_000,
        "before_cpu_stat": {
            "usage_usec": before,
            "user_usec": before - 10,
            "system_usec": 10,
        },
        "after_cpu_stat": {
            "usage_usec": after,
            "user_usec": after - 10,
            "system_usec": 10,
        },
    }


def test_evaluation_accepts_clear_service_separation_without_throttle_counters() -> None:
    plan = calibration.CalibrationPlan(measurement_seconds=10)

    verdict = calibration.evaluate_calibration(
        plan,
        (
            _sample("slow", 25, before=1_000, after=3_000_000),
            _sample("fast", 100, before=2_000, after=9_000_000),
        ),
        elapsed_usec=10_000_000,
    )

    assert verdict["verdict"] == "PASS"
    assert verdict["cohorts"]["slow"]["service_fraction"] == 0.2999
    assert verdict["cohorts"]["fast"]["service_fraction"] == 0.8998
    assert verdict["service_ratio_fast_to_slow"] > plan.minimum_service_ratio
    assert "throttled_usec" not in verdict["cohorts"]["slow"]


def test_evaluation_rejects_ratio_that_rounds_up_to_the_minimum() -> None:
    plan = calibration.CalibrationPlan(measurement_seconds=10)

    verdict = calibration.evaluate_calibration(
        plan,
        (
            _sample("slow", 25, before=100, after=2_500_500),
            _sample("fast", 100, before=100, after=5_000_100),
        ),
        elapsed_usec=10_000_000,
    )

    assert verdict["verdict"] == "FAIL"
    assert "ratio" in verdict["reason"]


def test_evaluation_accepts_one_microsecond_cpu_stat_rounding() -> None:
    plan = calibration.CalibrationPlan(measurement_seconds=10)
    slow = _sample("slow", 25, before=1_000, after=3_000_000)
    fast = _sample("fast", 100, before=2_000, after=9_000_000)
    slow["after_cpu_stat"]["user_usec"] += 1  # type: ignore[index]
    fast["after_cpu_stat"]["user_usec"] += 1  # type: ignore[index]

    verdict = calibration.evaluate_calibration(
        plan, (slow, fast), elapsed_usec=10_000_000
    )

    assert verdict["verdict"] == "PASS"


def test_evaluation_rejects_cpu_stat_excess_beyond_rounding_bound() -> None:
    plan = calibration.CalibrationPlan(measurement_seconds=10)
    slow = _sample("slow", 25, before=1_000, after=3_000_000)
    slow["after_cpu_stat"]["user_usec"] += 2  # type: ignore[index]

    verdict = calibration.evaluate_calibration(
        plan,
        (slow, _sample("fast", 100, before=2_000, after=9_000_000)),
        elapsed_usec=10_000_000,
    )

    assert verdict["verdict"] == "FAIL"
    assert verdict["reason"] == "cpu.stat component accounting exceeds usage"


@pytest.mark.parametrize(
    "samples, elapsed_usec, expected_reason",
    (
        (
            (
                _sample("slow", 25, before=5_000, after=4_999),
                _sample("fast", 100, before=2_000, after=9_000_000),
            ),
            10_000_000,
            "not monotonic",
        ),
        (
            (
                _sample("slow", 25, before=1_000, after=3_000_000),
                _sample("fast", 100, before=2_000, after=4_000_000),
            ),
            10_000_000,
            "ratio",
        ),
        (
            (
                _sample("slow", 25, before=1_000, after=3_000_000),
                {
                    **_sample("fast", 100, before=2_000, after=9_000_000),
                    "observed_quota_per_second_usec": 500_000,
                },
            ),
            10_000_000,
            "observed quota",
        ),
    ),
)
def test_evaluation_fails_closed_on_invalid_measurement(
    samples: tuple[dict[str, object], ...],
    elapsed_usec: int,
    expected_reason: str,
) -> None:
    verdict = calibration.evaluate_calibration(
        calibration.CalibrationPlan(measurement_seconds=10),
        samples,
        elapsed_usec=elapsed_usec,
    )

    assert verdict["verdict"] == "FAIL"
    assert expected_reason in verdict["reason"]


def test_plan_and_scope_command_are_bounded_and_shell_free() -> None:
    plan = calibration.CalibrationPlan()

    command, unit = calibration.calibration_scope_command(
        plan,
        run_id="20260924T120000Z-abcd",
        cohort="fast",
        python_executable="/usr/bin/python3",
    )

    assert plan.slow_quota_percent == 25
    assert plan.fast_quota_percent == 100
    assert plan.minimum_service_ratio == 2.0
    assert unit == "kauri-cpu-calibration-20260924t120000z-abcd-fast.scope"
    assert "--property=CPUQuota=100%" in command
    assert command[-3:] == ("/usr/bin/python3", "-c", calibration.CPU_BOUND_WORKER)
    assert not any(value in {"sh", "bash", "-c"} for value in command[:-2])


def test_plan_rejects_a_nonseparating_or_too_short_contract() -> None:
    with pytest.raises(calibration.CalibrationError, match="distinct"):
        calibration.CalibrationPlan(slow_quota_percent=25, fast_quota_percent=25)
    with pytest.raises(calibration.CalibrationError, match="at least"):
        calibration.CalibrationPlan(measurement_seconds=4)


class _Process:
    def __init__(self, cohort: str, states: dict[str, str], *, returncode: int = 0) -> None:
        self.cohort = cohort
        self._states = states
        self.returncode = returncode

    def communicate(self, *, timeout: int) -> tuple[str, str]:
        assert timeout >= 5
        self._states[self.cohort] = "inactive"
        return "", ""

    def kill(self) -> None:
        self.returncode = -9


def _run_dependencies(*, launch_failure: bool = False, cleanup_failure: bool = False):
    states = {"slow": "active", "fast": "active"}
    calls = {"slow": 0, "fast": 0}
    clock = iter((0, 1_000_000, 2_000_000, 12_000_000, 13_000_000, 14_000_000))

    def command_builder(plan: calibration.CalibrationPlan, *, run_id: str, cohort: str):
        return ("fake-systemd-run", cohort), f"unit-{run_id}-{cohort}.scope"

    def spawn(command: tuple[str, ...], **_kwargs: object) -> _Process:
        cohort = command[-1]
        if launch_failure and cohort == "fast":
            raise OSError("launch failed")
        return _Process(cohort, states, returncode=1 if cleanup_failure and cohort == "fast" else 0)

    def show_unit(unit: str) -> dict[str, str]:
        cohort = "slow" if unit.endswith("slow.scope") else "fast"
        active = states[cohort] == "active"
        return {
            "ActiveState": "active" if active else "inactive",
            "SubState": "running" if active else "dead",
            "CPUQuotaPerSecUSec": "250ms" if cohort == "slow" else "1s",
            "ControlGroup": f"/user.slice/{unit}" if active else "",
        }

    def read_cpu_stat(path: Path) -> dict[str, int]:
        cohort = "slow" if "slow.scope" in str(path) else "fast"
        calls[cohort] += 1
        usage = 1_000 if calls[cohort] == 1 else (2_501_000 if cohort == "slow" else 8_001_000)
        return {"usage_usec": usage, "user_usec": usage, "system_usec": 0}

    return {
        "environment_probe": lambda: {"verified": True, "cgroup_version": 2},
        "command_builder": command_builder,
        "spawn": spawn,
        "show_unit": show_unit,
        "read_cpu_stat": read_cpu_stat,
        "monotonic_ns": lambda: next(clock),
        "sleep": lambda _seconds: None,
        "utc_now": lambda: __import__("datetime").datetime(2026, 9, 24, 12, 0, tzinfo=__import__("datetime").timezone.utc),
    }


def test_run_calibration_persists_an_attributable_pass_receipt(tmp_path: Path) -> None:
    result = calibration.run_calibration(
        calibration.CalibrationPlan(measurement_seconds=10),
        output_root=tmp_path / "calibration",
        run_id="pilot-01",
        **_run_dependencies(),
    )

    receipt = json.loads((tmp_path / "calibration/cpu-quota-calibration-receipt.json").read_text())
    assert result == receipt
    assert receipt["verdict"] == "PASS"
    assert receipt["run_id"] == "pilot-01"
    assert receipt["output_root"] == str((tmp_path / "calibration").resolve())
    assert receipt["started_utc"].endswith("+00:00")
    assert receipt["finished_utc"].endswith("+00:00")
    assert receipt["units"] == [
        {"cohort": "slow", "unit": "unit-pilot-01-slow.scope"},
        {"cohort": "fast", "unit": "unit-pilot-01-fast.scope"},
    ]
    assert receipt["measurements"][0]["measurement_elapsed_usec"] > 0
    assert receipt["receipt_canonical_sha256"] == calibration.receipt_digest(receipt)


def test_run_calibration_persists_launch_failure(tmp_path: Path) -> None:
    result = calibration.run_calibration(
        calibration.CalibrationPlan(measurement_seconds=10),
        output_root=tmp_path / "launch-failure",
        run_id="pilot-02",
        **_run_dependencies(launch_failure=True),
    )

    assert result["verdict"] == "FAIL"
    assert "launch failed" in result["reason"]
    assert (tmp_path / "launch-failure/cpu-quota-calibration-receipt.json").is_file()


def test_run_calibration_fails_when_cleanup_is_not_clean(tmp_path: Path) -> None:
    result = calibration.run_calibration(
        calibration.CalibrationPlan(measurement_seconds=10),
        output_root=tmp_path / "cleanup-failure",
        run_id="pilot-03",
        **_run_dependencies(cleanup_failure=True),
    )

    assert result["verdict"] == "FAIL"
    assert result["reason"] == "calibration cleanup did not complete"
    assert result["cleanup"]["fast"]["returncode"] == 1


def test_cli_requires_an_output_root_and_run_identity() -> None:
    parser = calibration.build_argument_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(())
    with pytest.raises(SystemExit):
        parser.parse_args(("--output", "result"))
    with pytest.raises(SystemExit):
        parser.parse_args(("--run-id", "pilot"))


def test_cli_routes_the_frozen_plan_and_prints_compact_pass_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observed: dict[str, object] = {}

    def run(plan: calibration.CalibrationPlan, *, output_root: Path, run_id: str):
        observed.update({"plan": plan, "output_root": output_root, "run_id": run_id})
        return {"verdict": "PASS", "cleanup_complete": True}

    monkeypatch.setattr(calibration, "run_calibration", run)

    exit_code = calibration.main(("--output", str(tmp_path / "fresh"), "--run-id", "pilot-04"))

    assert exit_code == 0
    assert observed["plan"] == calibration.CalibrationPlan()
    assert observed["output_root"] == tmp_path / "fresh"
    assert observed["run_id"] == "pilot-04"
    assert "verdict=PASS cleanup_complete=True receipt=" in capsys.readouterr().out


def test_cli_returns_nonzero_for_a_failed_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        calibration,
        "run_calibration",
        lambda *_args, **_kwargs: {"verdict": "FAIL", "cleanup_complete": True},
    )

    exit_code = calibration.main(("--output", str(tmp_path / "fresh"), "--run-id", "pilot-05"))

    assert exit_code == 1
    assert "verdict=FAIL cleanup_complete=True receipt=" in capsys.readouterr().out
