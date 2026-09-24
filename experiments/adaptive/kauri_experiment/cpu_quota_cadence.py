"""Standalone 31-scope cadence gate for the external CPU-quota monitor."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import IO, Any

from . import cpu_quota
from .processes import ProcessRegistry
from .profiled_fault_runtime import spawn_owned_process

_SCHEMA_VERSION = 1
_KIND = "kauri-cpu-quota-cadence-v1"
_PROFILE_ROOT = Path(__file__).resolve().parents[1] / "profiles"
_DEFAULT_CONTRACT = _PROFILE_ROOT / "n31-cpu-quota-heterogeneity-smoke-v1.json"
_DEFAULT_PROFILE = _PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v13.json"


class CadenceGateError(RuntimeError):
    """The standalone cadence gate cannot produce trustworthy evidence."""


@dataclass(frozen=True, slots=True)
class CadencePlan:
    sample_seconds: int = 30
    worker_seconds: int = 45
    minimum_samples_per_replica: int = 25
    maximum_gap_multiplier: int = 2

    def __post_init__(self) -> None:
        if self.sample_seconds < 10:
            raise CadenceGateError("cadence sampling must run for at least 10 seconds")
        if self.worker_seconds <= self.sample_seconds:
            raise CadenceGateError("cadence workers must outlive the sampling window")
        if self.minimum_samples_per_replica < 3:
            raise CadenceGateError("cadence gate requires at least three samples")
        if self.maximum_gap_multiplier != 2:
            raise CadenceGateError("cadence gap multiplier must remain frozen at two")


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def _replace_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical(value))
    os.replace(temporary, path)


def _read_jsonl(path: Path) -> list[Mapping[str, object]]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
        rows = [json.loads(line) for line in lines]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CadenceGateError(f"cannot read cadence evidence at {path}") from exc
    if not rows or any(not isinstance(row, Mapping) for row in rows):
        raise CadenceGateError(f"cadence evidence at {path} is empty or malformed")
    return rows


def _failure(reason: str) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "verdict": "FAIL",
        "reason": reason,
    }


def evaluate_cadence(
    contract: cpu_quota.CpuQuotaContract,
    samples: Sequence[Mapping[str, object]],
    rounds: Sequence[Mapping[str, object]],
    *,
    minimum_samples_per_replica: int,
) -> dict[str, object]:
    """Evaluate the frozen two-interval gap bound over exact replica coverage."""

    expected = set(contract.replica_ids)
    timestamps: dict[int, list[int]] = {replica_id: [] for replica_id in expected}
    try:
        for sample in samples:
            replica_id = sample.get("replica_id")
            timestamp = sample.get("source_monotonic_ns")
            if (
                type(replica_id) is not int
                or replica_id not in expected
                or type(timestamp) is not int
                or timestamp <= 0
                or sample.get("active_state") != "active"
                or sample.get("cpu_quota_percent") != contract.quota_percent(replica_id)
                or sample.get("cpu_quota_per_second_usec")
                != contract.quota_percent(replica_id) * 10_000
            ):
                raise CadenceGateError("cadence sample identity or quota drifted")
            timestamps[replica_id].append(timestamp)
        if any(
            len(values) < minimum_samples_per_replica
            or any(right <= left for left, right in zip(values, values[1:]))
            for values in timestamps.values()
        ):
            raise CadenceGateError("cadence sample coverage is incomplete")
        counts = {len(values) for values in timestamps.values()}
        if len(counts) != 1 or len(rounds) != next(iter(counts)):
            raise CadenceGateError("cadence round coverage drifted")
        ordinals = [row.get("round_ordinal") for row in rounds]
        if ordinals != list(range(len(rounds))):
            raise CadenceGateError("cadence round ordinals drifted")
        durations = [row.get("duration_ns") for row in rounds]
        overruns = [row.get("completion_overrun_ns") for row in rounds]
        if any(type(value) is not int or value < 0 for value in durations + overruns):
            raise CadenceGateError("cadence round timing is malformed")
    except (KeyError, TypeError, ValueError, CadenceGateError) as exc:
        return _failure(str(exc) or type(exc).__name__)

    gaps = [
        right - left
        for values in timestamps.values()
        for left, right in zip(values, values[1:])
    ]
    tolerance_ns = contract.sampling_interval_ms * 1_000_000 * 2
    maximum_gap_ns = max(gaps)
    if maximum_gap_ns > tolerance_ns:
        return _failure("CPU-quota sampling cadence exceeded the frozen tolerance")
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "verdict": "PASS",
        "replica_count": len(expected),
        "samples_per_replica": next(iter(counts)),
        "sampling_interval_ms": contract.sampling_interval_ms,
        "validation_tolerance_ms": tolerance_ns // 1_000_000,
        "maximum_gap_ms": round(maximum_gap_ns / 1_000_000, 6),
        "maximum_round_duration_ms": round(max(durations) / 1_000_000, 6),
        "maximum_completion_overrun_ms": round(max(overruns) / 1_000_000, 6),
    }


def _worker_source(seconds: int) -> str:
    return (
        "import time\n"
        f"deadline = time.monotonic() + {seconds}.0\n"
        "value = 1\n"
        "while time.monotonic() < deadline:\n"
        "    value = (value * 1103515245 + 12345) & 0x7fffffff\n"
    )


def run_cadence_gate(
    plan: CadencePlan,
    *,
    output_root: Path,
    run_id: str,
    contract_path: Path = _DEFAULT_CONTRACT,
    profile_path: Path = _DEFAULT_PROFILE,
    environment_probe: Any = cpu_quota.verify_linux_environment,
    sleep: Any = time.sleep,
    utc_now: Any = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Run CPU-bound workers in 31 exact scopes without starting Kauri."""

    root = Path(output_root).resolve()
    if root.exists():
        raise CadenceGateError("cadence output root already exists")
    (root / "logs").mkdir(parents=True)
    contract = cpu_quota.load_cpu_quota_contract(
        contract_path,
        base_profile_path=profile_path,
        expected_replica_ids=tuple(range(31)),
    )
    started_utc = utc_now().isoformat()
    environment: Mapping[str, object] = {"verified": False}
    registry = ProcessRegistry()
    runtime: cpu_quota.CpuQuotaRuntime | None = None
    logs: list[IO[bytes]] = []
    failure: BaseException | None = None
    process_cleanup: list[dict[str, object]] = []
    quota_cleanup: Mapping[str, object] = {"complete": False}
    try:
        environment = environment_probe()
        runtime = cpu_quota.CpuQuotaRuntime(
            contract,
            run_id=run_id,
            run_directory=root,
            base_spawn=spawn_owned_process,
        )
        for replica_id in contract.replica_ids:
            _record, log = runtime.spawn_owned_process(
                registry,
                name=f"cadence-worker-{replica_id}",
                replica_id=replica_id,
                command=(sys.executable, "-c", _worker_source(plan.worker_seconds)),
                log_path=root / "logs" / f"worker-{replica_id}.log",
                working_directory=root,
            )
            logs.append(log)
        runtime.start_monitor()
        sleep(plan.sample_seconds)
    except BaseException as exc:
        failure = exc
    finally:
        if runtime is not None:
            _stopped, monitor_error = runtime.stop_monitor()
            if monitor_error is not None and failure is None:
                failure = monitor_error
        try:
            process_cleanup = [
                {
                    "replica_id": outcome.replica_id,
                    "signal_number": outcome.signal_number,
                    "returncode": outcome.returncode,
                }
                for outcome in registry.cleanup(timeout_s=2.0)
            ]
        except BaseException as exc:
            failure = failure or exc
        if runtime is not None:
            try:
                quota_cleanup = runtime.verify_cleanup()
            except cpu_quota.CpuQuotaCleanupError as exc:
                quota_cleanup = exc.cleanup
                failure = failure or exc
            except BaseException as exc:
                failure = failure or exc
        for log in logs:
            log.close()

    verdict = _failure(str(failure) or type(failure).__name__) if failure else None
    if verdict is None:
        try:
            verdict = evaluate_cadence(
                contract,
                _read_jsonl(root / "raw/cpu-quota-samples.jsonl"),
                _read_jsonl(root / "raw/cpu-quota-monitor-rounds.jsonl"),
                minimum_samples_per_replica=plan.minimum_samples_per_replica,
            )
        except BaseException as exc:
            verdict = _failure(str(exc) or type(exc).__name__)
    receipt = {
        **verdict,
        "run_id": run_id,
        "output_root": str(root),
        "contract_sha256": contract.contract_sha256,
        "plan": {
            "sample_seconds": plan.sample_seconds,
            "worker_seconds": plan.worker_seconds,
            "minimum_samples_per_replica": plan.minimum_samples_per_replica,
            "maximum_gap_multiplier": plan.maximum_gap_multiplier,
        },
        "environment": dict(environment),
        "process_cleanup": process_cleanup,
        "quota_cleanup": dict(quota_cleanup),
        "cleanup_complete": quota_cleanup.get("complete") is True,
        "started_utc": started_utc,
        "finished_utc": utc_now().isoformat(),
    }
    if receipt["cleanup_complete"] is not True and receipt["verdict"] == "PASS":
        receipt["verdict"] = "FAIL"
        receipt["reason"] = "cadence cleanup did not complete"
    receipt["receipt_canonical_sha256"] = hashlib.sha256(
        _canonical(receipt)
    ).hexdigest()
    _replace_json(root / "cpu-quota-cadence-receipt.json", receipt)
    return receipt


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prove 31-scope CPU-quota monitor cadence without Kauri."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        receipt = run_cadence_gate(
            CadencePlan(), output_root=args.output, run_id=str(args.run_id)
        )
    except (CadenceGateError, OSError) as exc:
        print(f"cadence_error={exc}", file=sys.stderr)
        return 2
    print(
        f"verdict={receipt['verdict']} "
        f"cleanup_complete={receipt['cleanup_complete']} "
        f"receipt={Path(args.output).resolve() / 'cpu-quota-cadence-receipt.json'}"
    )
    return 0 if receipt["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
