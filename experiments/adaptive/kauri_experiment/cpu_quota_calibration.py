"""Standalone, manager-blind CPU-service calibration for Linux cgroup quotas.

This module deliberately does not start Kauri.  It establishes whether two
transient user scopes receive measurably different CPU service before a future
heterogeneous Kauri profile is allowed to run.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from . import cpu_quota


_SCHEMA_VERSION = 1
_KIND = "kauri-cpu-quota-calibration-v1"
_UNIT_PART = re.compile(r"[^a-z0-9]+")
_COHORTS = ("slow", "fast")

# A pure-Python loop keeps the calibration portable and avoids measuring a
# Kauri-specific code path.  The process has no inputs and produces no output.
CPU_BOUND_WORKER = (
    "import time\n"
    "deadline = time.monotonic() + 30.0\n"
    "value = 1\n"
    "while time.monotonic() < deadline:\n"
    "    value = (value * 1103515245 + 12345) & 0x7fffffff\n"
)


def _worker_source(run_seconds: int) -> str:
    if run_seconds == 30:
        return CPU_BOUND_WORKER
    return CPU_BOUND_WORKER.replace("+ 30.0", f"+ {run_seconds}.0")


class CalibrationError(RuntimeError):
    """The calibration contract or its execution cannot be trusted."""


@dataclass(frozen=True, slots=True)
class CalibrationPlan:
    """Frozen local calibration thresholds, independent of Kauri profiles."""

    slow_quota_percent: int = 25
    fast_quota_percent: int = 100
    run_seconds: int = 30
    measurement_seconds: int = 20
    minimum_service_ratio: float = 2.0

    def __post_init__(self) -> None:
        if not (0 < self.slow_quota_percent < self.fast_quota_percent <= 10_000):
            raise CalibrationError("CPU quotas must be distinct positive percentages")
        if self.run_seconds < 10 or self.measurement_seconds < 5:
            raise CalibrationError("calibration durations must be at least 10s and 5s")
        if self.measurement_seconds >= self.run_seconds:
            raise CalibrationError("measurement duration must be shorter than run duration")
        if not (1.0 < self.minimum_service_ratio <= self.fast_quota_percent / self.slow_quota_percent):
            raise CalibrationError("minimum service ratio is outside the quota separation")

    def quota_for(self, cohort: str) -> int:
        if cohort == "slow":
            return self.slow_quota_percent
        if cohort == "fast":
            return self.fast_quota_percent
        raise CalibrationError("unknown calibration cohort")


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")


def receipt_digest(receipt: Mapping[str, object]) -> str:
    """Digest the canonical receipt content without its derived digest field."""

    document = dict(receipt)
    document.pop("receipt_canonical_sha256", None)
    return hashlib.sha256(_canonical(document)).hexdigest()


def _replace_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical(value))
    os.replace(temporary, path)


def _unit_name(run_id: str, cohort: str) -> str:
    if cohort not in _COHORTS:
        raise CalibrationError("unknown calibration cohort")
    normalized = _UNIT_PART.sub("-", run_id.lower()).strip("-")[:96].rstrip("-")
    if not normalized:
        raise CalibrationError("run ID cannot produce a systemd unit identity")
    return f"kauri-cpu-calibration-{normalized}-{cohort}.scope"


def calibration_scope_command(
    plan: CalibrationPlan,
    *,
    run_id: str,
    cohort: str,
    python_executable: str = sys.executable,
) -> tuple[tuple[str, ...], str]:
    """Create a shell-free transient scope command for one CPU-bound worker."""

    if not Path(python_executable).is_absolute():
        raise CalibrationError("Python executable must be an absolute path")
    unit = _unit_name(run_id, cohort)
    return (
        (
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={unit}",
            "--property=CPUAccounting=yes",
            f"--property=CPUQuota={plan.quota_for(cohort)}%",
            "--",
            python_executable,
            "-c",
            _worker_source(plan.run_seconds),
        ),
        unit,
    )


def _stat_delta(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, int]:
    required = ("usage_usec", "user_usec", "system_usec")
    if not set(required).issubset(before) or not set(required).issubset(after):
        raise CalibrationError("cpu.stat lacks required accounting fields")
    delta: dict[str, int] = {}
    for key in required:
        left, right = before[key], after[key]
        if type(left) is not int or type(right) is not int or left < 0 or right < 0:
            raise CalibrationError("cpu.stat accounting is invalid")
        if right < left:
            raise CalibrationError("cpu.stat accounting is not monotonic")
        delta[key] = right - left
    if delta["user_usec"] + delta["system_usec"] > delta["usage_usec"]:
        raise CalibrationError("cpu.stat component accounting exceeds usage")
    return delta


def _failure(reason: str, *, plan: CalibrationPlan, samples: Sequence[Mapping[str, object]], elapsed_usec: int) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "verdict": "FAIL",
        "reason": reason,
        "plan": {
            "slow_quota_percent": plan.slow_quota_percent,
            "fast_quota_percent": plan.fast_quota_percent,
            "run_seconds": plan.run_seconds,
            "measurement_seconds": plan.measurement_seconds,
            "minimum_service_ratio": plan.minimum_service_ratio,
        },
        "elapsed_usec": elapsed_usec,
        "measurements": [dict(sample) for sample in samples],
    }


def evaluate_calibration(
    plan: CalibrationPlan,
    samples: Sequence[Mapping[str, object]],
    *,
    elapsed_usec: int,
) -> dict[str, object]:
    """Return a deterministic PASS/FAIL service-separation verdict.

    `cpu.stat` throttle counters are optional on cgroup v2 hosts.  This gate
    therefore relies only on mandatory usage/user/system accounting and fails
    closed if those counters cannot prove monotonic, quota-consistent service.
    """

    if elapsed_usec <= 0:
        return _failure("measurement elapsed time is invalid", plan=plan, samples=samples, elapsed_usec=elapsed_usec)
    by_cohort: dict[str, Mapping[str, object]] = {}
    for sample in samples:
        cohort = sample.get("cohort")
        if cohort not in _COHORTS or cohort in by_cohort:
            return _failure("calibration cohorts are not exact", plan=plan, samples=samples, elapsed_usec=elapsed_usec)
        by_cohort[cohort] = sample  # type: ignore[index]
    if set(by_cohort) != set(_COHORTS):
        return _failure("calibration cohorts are not exact", plan=plan, samples=samples, elapsed_usec=elapsed_usec)

    cohorts: dict[str, dict[str, object]] = {}
    raw_service: dict[str, float] = {}
    try:
        for cohort in _COHORTS:
            sample = by_cohort[cohort]
            quota = plan.quota_for(cohort)
            if sample.get("requested_quota_percent") != quota:
                raise CalibrationError("requested quota drifted")
            if sample.get("observed_quota_per_second_usec") != quota * 10_000:
                raise CalibrationError("observed quota does not match the request")
            before = sample.get("before_cpu_stat")
            after = sample.get("after_cpu_stat")
            if not isinstance(before, Mapping) or not isinstance(after, Mapping):
                raise CalibrationError("cpu.stat snapshots are absent")
            delta = _stat_delta(before, after)  # type: ignore[arg-type]
            sample_elapsed_usec = sample.get("measurement_elapsed_usec", elapsed_usec)
            if type(sample_elapsed_usec) is not int or sample_elapsed_usec <= 0:
                raise CalibrationError("cohort measurement elapsed time is invalid")
            service = delta["usage_usec"] / sample_elapsed_usec
            raw_service[cohort] = service
            cohorts[cohort] = {
                "requested_quota_percent": quota,
                "observed_quota_per_second_usec": quota * 10_000,
                "usage_delta_usec": delta["usage_usec"],
                "user_delta_usec": delta["user_usec"],
                "system_delta_usec": delta["system_usec"],
                "measurement_elapsed_usec": sample_elapsed_usec,
                "service_fraction": round(service, 4),
            }
    except CalibrationError as exc:
        return _failure(str(exc), plan=plan, samples=samples, elapsed_usec=elapsed_usec)

    # Preserve full precision for the acceptance gate.  Rounded values below
    # are receipt presentation only and must never turn a sub-threshold ratio
    # into a passing verdict.
    slow = raw_service["slow"]
    fast = raw_service["fast"]
    if slow <= 0.0:
        return _failure("slow cohort consumed no CPU service", plan=plan, samples=samples, elapsed_usec=elapsed_usec)
    ratio = fast / slow
    if ratio < plan.minimum_service_ratio:
        return _failure("fast-to-slow CPU service ratio is below the frozen minimum", plan=plan, samples=samples, elapsed_usec=elapsed_usec)
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "verdict": "PASS",
        "plan": {
            "slow_quota_percent": plan.slow_quota_percent,
            "fast_quota_percent": plan.fast_quota_percent,
            "run_seconds": plan.run_seconds,
            "measurement_seconds": plan.measurement_seconds,
            "minimum_service_ratio": plan.minimum_service_ratio,
        },
        "elapsed_usec": elapsed_usec,
        "cohorts": cohorts,
        "service_ratio_fast_to_slow": round(ratio, 4),
        "throttle_counters_required": False,
        "measurements": [dict(sample) for sample in samples],
    }


def _show_unit(unit: str) -> dict[str, str]:
    result = subprocess.run(
        (
            "systemctl", "--user", "show", unit, "--no-pager",
            "--property=ActiveState", "--property=SubState", "--property=LoadState",
            "--property=CPUQuotaPerSecUSec", "--property=ControlGroup",
        ),
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0 and "LoadState=not-found" not in result.stdout:
        raise CalibrationError(result.stderr.strip() or "cannot inspect calibration scope")
    return cpu_quota.parse_systemctl_show(result.stdout)


def _default_read_cpu_stat(path: Path) -> dict[str, int]:
    try:
        return cpu_quota.parse_cpu_stat(path.read_text(encoding="ascii"))
    except OSError as exc:
        raise CalibrationError("cannot read calibration cgroup cpu.stat") from exc


def _active_snapshot(
    plan: CalibrationPlan,
    *,
    cohort: str,
    unit: str,
    show_unit: Any = _show_unit,
    read_cpu_stat: Any = _default_read_cpu_stat,
    sleep: Any = time.sleep,
) -> tuple[dict[str, str], dict[str, int]]:
    for _attempt in range(40):
        properties = show_unit(unit)
        if (
            properties["ActiveState"] == "active"
            and properties["SubState"] in {"running", "start"}
            and cpu_quota.quota_per_second_usec(properties)
            == plan.quota_for(cohort) * 10_000
            and properties["ControlGroup"].startswith("/")
        ):
            path = (
                Path("/sys/fs/cgroup")
                / properties["ControlGroup"].lstrip("/")
                / "cpu.stat"
            )
            return properties, dict(read_cpu_stat(path))
        sleep(0.05)
    raise CalibrationError("calibration scope did not expose its exact active quota")


def _verify_inactive_unit(
    unit: str, *, show_unit: Any = _show_unit, sleep: Any = time.sleep
) -> dict[str, object]:
    for _attempt in range(40):
        properties = show_unit(unit)
        inactive = (
            properties["ActiveState"] == "inactive"
            and properties["ControlGroup"] == ""
        )
        if inactive:
            return {
                "active_state": properties["ActiveState"],
                "control_group": properties["ControlGroup"],
                "unit_inactive": True,
            }
        sleep(0.05)
    return {
        "active_state": properties["ActiveState"],
        "control_group": properties["ControlGroup"],
        "unit_inactive": False,
    }


def run_calibration(
    plan: CalibrationPlan,
    *,
    output_root: Path,
    run_id: str,
    environment_probe: Any = cpu_quota.verify_linux_environment,
    command_builder: Any = calibration_scope_command,
    spawn: Any = subprocess.Popen,
    show_unit: Any = _show_unit,
    read_cpu_stat: Any = _default_read_cpu_stat,
    monotonic_ns: Any = time.monotonic_ns,
    sleep: Any = time.sleep,
    utc_now: Any = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Run the local calibration and persist a final PASS/FAIL receipt.

    This is intentionally a separate local gate.  It does not import Kauri
    runtime code, take a profile, or launch a manager, replica, or client.
    """

    root = Path(output_root).resolve()
    if root.exists():
        raise CalibrationError("calibration output root already exists")
    root.mkdir(parents=True)
    processes: dict[str, subprocess.Popen[str]] = {}
    samples: list[dict[str, object]] = []
    started_ns = monotonic_ns()
    started_utc = utc_now().isoformat()
    environment: dict[str, object] = {"verified": False}
    try:
        environment = environment_probe()
        for cohort in _COHORTS:
            command, unit = command_builder(plan, run_id=run_id, cohort=cohort)
            processes[cohort] = spawn(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
            )
            samples.append({"cohort": cohort, "unit": unit})
        before: dict[str, dict[str, int]] = {}
        before_ns: dict[str, int] = {}
        observed: dict[str, int] = {}
        for sample in samples:
            cohort, unit = str(sample["cohort"]), str(sample["unit"])
            properties, before[cohort] = _active_snapshot(
                plan, cohort=cohort, unit=unit, show_unit=show_unit,
                read_cpu_stat=read_cpu_stat, sleep=sleep,
            )
            observed[cohort] = cpu_quota.quota_per_second_usec(properties)
            before_ns[cohort] = monotonic_ns()
        sleep(plan.measurement_seconds)
        elapsed_usec = max(1, (monotonic_ns() - started_ns) // 1_000)
        measurements: list[dict[str, object]] = []
        for sample in samples:
            cohort, unit = str(sample["cohort"]), str(sample["unit"])
            _properties, after = _active_snapshot(
                plan, cohort=cohort, unit=unit, show_unit=show_unit,
                read_cpu_stat=read_cpu_stat, sleep=sleep,
            )
            after_ns = monotonic_ns()
            measurements.append({
                **sample,
                "requested_quota_percent": plan.quota_for(cohort),
                "observed_quota_per_second_usec": observed[cohort],
                "before_cpu_stat": before[cohort],
                "after_cpu_stat": after,
                "measurement_started_monotonic_ns": before_ns[cohort],
                "measurement_finished_monotonic_ns": after_ns,
                "measurement_elapsed_usec": max(1, (after_ns - before_ns[cohort]) // 1_000),
            })
        verdict = evaluate_calibration(plan, measurements, elapsed_usec=elapsed_usec)
    except BaseException as exc:
        elapsed_usec = max(1, (monotonic_ns() - started_ns) // 1_000)
        verdict = _failure(str(exc) or type(exc).__name__, plan=plan, samples=samples, elapsed_usec=elapsed_usec)
    finally:
        cleanup: dict[str, object] = {}
        for cohort, process in processes.items():
            try:
                _stdout, stderr = process.communicate(timeout=max(5, plan.run_seconds))
                cleanup[cohort] = {"returncode": process.returncode, "stderr": stderr.strip()}
            except subprocess.TimeoutExpired:
                process.kill()
                _stdout, stderr = process.communicate()
                cleanup[cohort] = {"returncode": process.returncode, "stderr": stderr.strip(), "timed_out": True}
            unit = next(str(row["unit"]) for row in samples if row["cohort"] == cohort)
            try:
                cleanup[cohort].update(  # type: ignore[union-attr]
                    _verify_inactive_unit(unit, show_unit=show_unit, sleep=sleep)
                )
            except CalibrationError as exc:
                cleanup[cohort]["unit_inactive"] = False
                cleanup[cohort]["unit_check_error"] = str(exc)
        verdict["environment"] = environment
        verdict["run_id"] = run_id
        verdict["output_root"] = str(root)
        verdict["started_utc"] = started_utc
        verdict["finished_utc"] = utc_now().isoformat()
        verdict["units"] = [
            {"cohort": row["cohort"], "unit": row["unit"]} for row in samples
        ]
        verdict["cleanup"] = cleanup
        verdict["cleanup_complete"] = all(
            isinstance(row, Mapping)
            and row.get("returncode") == 0
            and not row.get("timed_out")
            and row.get("unit_inactive") is True
            for row in cleanup.values()
        ) and len(cleanup) == len(_COHORTS)
        if verdict["cleanup_complete"] is not True:
            if verdict["verdict"] != "FAIL":
                verdict["verdict"] = "FAIL"
                verdict["reason"] = "calibration cleanup did not complete"
        verdict["receipt_canonical_sha256"] = receipt_digest(verdict)
        _replace_json(root / "cpu-quota-calibration-receipt.json", verdict)
    return verdict


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the deliberately small standalone calibration CLI."""

    parser = argparse.ArgumentParser(
        description="Calibrate CPU quota service separation before a Kauri run."
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new, empty output root for the calibration receipt",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="stable identifier included in transient scope and receipt identities",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the frozen calibration contract and return a shell-friendly status."""

    args = build_argument_parser().parse_args(argv)
    output_root = Path(args.output)
    receipt_path = output_root.resolve() / "cpu-quota-calibration-receipt.json"
    try:
        result = run_calibration(
            CalibrationPlan(), output_root=output_root, run_id=str(args.run_id)
        )
    except CalibrationError as exc:
        print(f"calibration_error={exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"calibration_error={exc}", file=sys.stderr)
        return 2
    verdict = result.get("verdict")
    cleanup_complete = result.get("cleanup_complete") is True
    print(f"verdict={verdict} cleanup_complete={cleanup_complete} receipt={receipt_path}")
    return 0 if verdict == "PASS" and cleanup_complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
