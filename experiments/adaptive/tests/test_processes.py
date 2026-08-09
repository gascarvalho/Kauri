"""Safety contract tests for the reusable FI-Core process registry."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import os
import signal
import subprocess
import sys
from types import ModuleType
from typing import Callable

import pytest


def _processes() -> ModuleType:
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.processes"
    )


@dataclass
class FakeProcess:
    pid: int
    wait_result: int = -int(signal.SIGKILL)
    returncode: int | None = None
    wait_calls: list[float] = field(default_factory=list)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        self.wait_calls.append(timeout)
        self.returncode = self.wait_result
        return self.wait_result


def _registry(
    processes: ModuleType,
    *,
    getpgid: Callable[[int], int],
    killpg: Callable[[int, int], None],
    launcher_pgid: int = 9999,
    timestamps: tuple[int, ...] = (100, 200, 300, 400),
) -> object:
    clock = iter(timestamps)
    return processes.ProcessRegistry(
        getpgid=getpgid,
        killpg=killpg,
        get_launcher_pgid=lambda: launcher_pgid,
        monotonic_ns=lambda: next(clock),
        wait_for_exit=lambda process, timeout: process.wait(timeout=timeout),
    )


def test_register_records_the_only_safe_process_group_shape() -> None:
    processes = _processes()
    process = FakeProcess(pid=100)
    registry = _registry(
        processes,
        getpgid=lambda pid: pid,
        killpg=lambda _pgid, _signal_number: None,
    )

    record = registry.register(
        name="replica-0",
        replica_id=0,
        process=process,
    )

    assert record.name == "replica-0"
    assert record.replica_id == 0
    assert record.pid == 100
    assert record.pgid == 100
    assert record.process is process


@pytest.mark.parametrize("duplicate", ["name", "replica", "pid"])
def test_register_rejects_duplicate_process_identity(
    duplicate: str,
) -> None:
    processes = _processes()
    pgids = {100: 100, 101: 101}
    registry = _registry(
        processes,
        getpgid=pgids.__getitem__,
        killpg=lambda _pgid, _signal_number: None,
    )
    registry.register(
        name="replica-0",
        replica_id=0,
        process=FakeProcess(pid=100),
    )
    second = {
        "name": "replica-0" if duplicate == "name" else "replica-1",
        "replica_id": 0 if duplicate == "replica" else 1,
        "process": FakeProcess(pid=100 if duplicate == "pid" else 101),
    }

    with pytest.raises(ValueError, match=duplicate):
        registry.register(**second)


@pytest.mark.parametrize(
    ("pid", "pgid", "launcher_pgid", "message"),
    [
        (1, 1, 9999, "greater than 1"),
        (100, 99, 9999, "own process group"),
        (100, 100, 100, "launcher"),
    ],
)
def test_register_rejects_unsafe_process_groups(
    pid: int,
    pgid: int,
    launcher_pgid: int,
    message: str,
) -> None:
    processes = _processes()
    registry = _registry(
        processes,
        getpgid=lambda _pid: pgid,
        killpg=lambda _pgid, _signal_number: None,
        launcher_pgid=launcher_pgid,
    )

    with pytest.raises(ValueError, match=message):
        registry.register(
            name="replica-0",
            replica_id=0,
            process=FakeProcess(pid=pid),
        )


def test_sigkill_refuses_an_unregistered_replica() -> None:
    processes = _processes()
    registry = _registry(
        processes,
        getpgid=lambda pid: pid,
        killpg=lambda _pgid, _signal_number: None,
    )

    with pytest.raises(KeyError, match="replica"):
        registry.sigkill_replica_group(
            fault_id="crash-0",
            replica_id=0,
            timeout_s=0.01,
        )


def test_sigkill_revalidates_live_pid_to_pgid_before_signalling() -> None:
    processes = _processes()
    observations = iter((100, 101))
    signals: list[tuple[int, int]] = []
    registry = _registry(
        processes,
        getpgid=lambda _pid: next(observations),
        killpg=lambda pgid, signal_number: signals.append(
            (pgid, signal_number)
        ),
    )
    registry.register(
        name="replica-0",
        replica_id=0,
        process=FakeProcess(pid=100),
    )

    with pytest.raises(RuntimeError, match="process group"):
        registry.sigkill_replica_group(
            fault_id="crash-0",
            replica_id=0,
            timeout_s=0.01,
        )

    assert signals == []


def test_sigkill_is_registered_at_most_once_and_confirms_exact_exit() -> None:
    processes = _processes()
    signals: list[tuple[int, int]] = []
    process = FakeProcess(pid=100)
    registry = _registry(
        processes,
        getpgid=lambda pid: pid,
        killpg=lambda pgid, signal_number: signals.append(
            (pgid, signal_number)
        ),
        timestamps=(1000, 2000),
    )
    registry.register(
        name="replica-0",
        replica_id=0,
        process=process,
    )

    outcome = registry.sigkill_replica_group(
        fault_id="crash-0",
        replica_id=0,
        timeout_s=0.25,
    )

    assert signals == [(100, int(signal.SIGKILL))]
    assert process.wait_calls == [0.25]
    assert outcome.fault_id == "crash-0"
    assert outcome.name == "replica-0"
    assert outcome.replica_id == 0
    assert outcome.pid == 100
    assert outcome.pgid == 100
    assert outcome.signal_number == int(signal.SIGKILL)
    assert outcome.returncode == -int(signal.SIGKILL)
    assert outcome.requested_monotonic_ns == 1000
    assert outcome.confirmed_monotonic_ns == 2000

    with pytest.raises(RuntimeError, match="already"):
        registry.sigkill_replica_group(
            fault_id="crash-0",
            replica_id=0,
            timeout_s=0.25,
        )
    assert signals == [(100, int(signal.SIGKILL))]


def test_sigkill_rejects_any_exit_other_than_negative_sigkill() -> None:
    processes = _processes()
    process = FakeProcess(pid=100, wait_result=0)
    registry = _registry(
        processes,
        getpgid=lambda pid: pid,
        killpg=lambda _pgid, _signal_number: None,
    )
    registry.register(
        name="replica-0",
        replica_id=0,
        process=process,
    )

    with pytest.raises(RuntimeError, match="SIGKILL"):
        registry.sigkill_replica_group(
            fault_id="crash-0",
            replica_id=0,
            timeout_s=0.01,
        )


def test_sigkill_batch_signals_every_group_before_waiting_in_request_order() -> None:
    processes = _processes()
    operations: list[tuple[str, int]] = []
    managed = {
        replica_id: FakeProcess(pid=100 + replica_id)
        for replica_id in (0, 1)
    }

    def wait_for_exit(process: FakeProcess, timeout: float) -> int:
        assert timeout == 0.25
        operations.append(("wait", process.pid))
        return process.wait(timeout)

    registry = processes.ProcessRegistry(
        getpgid=lambda pid: pid,
        killpg=lambda pgid, _signal_number: operations.append(
            ("kill", pgid)
        ),
        get_launcher_pgid=lambda: 9999,
        monotonic_ns=iter((1000, 1100, 2000, 2100)).__next__,
        wait_for_exit=wait_for_exit,
    )
    for replica_id, process in managed.items():
        registry.register(
            name=f"replica-{replica_id}",
            replica_id=replica_id,
            process=process,
        )

    outcomes = registry.sigkill_replica_groups(
        (("crash-0", 0), ("crash-1", 1)),
        timeout_s=0.25,
    )

    assert operations == [
        ("kill", 100),
        ("kill", 101),
        ("wait", 100),
        ("wait", 101),
    ]
    assert [
        (outcome.fault_id, outcome.replica_id, outcome.returncode)
        for outcome in outcomes
    ] == [
        ("crash-0", 0, -int(signal.SIGKILL)),
        ("crash-1", 1, -int(signal.SIGKILL)),
    ]


def test_sigkill_batch_prevalidates_every_target_before_any_signal() -> None:
    processes = _processes()
    observations = {100: iter((100, 100)), 101: iter((101, 102))}
    signals: list[tuple[int, int]] = []
    registry = _registry(
        processes,
        getpgid=lambda pid: next(observations[pid]),
        killpg=lambda pgid, signal_number: signals.append(
            (pgid, signal_number)
        ),
    )
    for replica_id in (0, 1):
        registry.register(
            name=f"replica-{replica_id}",
            replica_id=replica_id,
            process=FakeProcess(pid=100 + replica_id),
        )

    with pytest.raises(RuntimeError, match="process group"):
        registry.sigkill_replica_groups(
            (("crash-0", 0), ("crash-1", 1)),
            timeout_s=0.25,
        )

    assert signals == []


def test_sigkill_batch_failure_exposes_ordered_per_action_truth_and_reserves_all(
) -> None:
    processes = _processes()
    managed = {
        0: FakeProcess(pid=100, wait_result=-int(signal.SIGKILL)),
        1: FakeProcess(pid=101, wait_result=0),
    }
    signals: list[tuple[int, int]] = []
    registry = _registry(
        processes,
        getpgid=lambda pid: pid,
        killpg=lambda pgid, signal_number: signals.append(
            (pgid, signal_number)
        ),
        timestamps=(100, 200, 300),
    )
    for replica_id, process in managed.items():
        registry.register(
            name=f"replica-{replica_id}",
            replica_id=replica_id,
            process=process,
        )

    with pytest.raises(processes.SigkillBatchError) as raised:
        registry.sigkill_replica_groups(
            (("crash-0", 0), ("crash-1", 1)),
            timeout_s=0.25,
        )

    results = raised.value.results
    assert [
        (
            result.fault_id,
            result.replica_id,
            result.status,
            result.outcome is not None,
        )
        for result in results
    ] == [
        ("crash-0", 0, "succeeded", True),
        ("crash-1", 1, "failed", False),
    ]
    assert results[0].outcome.returncode == -int(signal.SIGKILL)
    assert results[0].error is None
    assert "did not exit from SIGKILL" in results[1].error
    assert registry.injected_sigkill_replica_ids == frozenset({0})
    assert signals == [
        (100, int(signal.SIGKILL)),
        (101, int(signal.SIGKILL)),
    ]

    for replica_id in managed:
        with pytest.raises(RuntimeError, match="already targeted"):
            registry.sigkill_replica_group(
                fault_id=f"retry-{replica_id}",
                replica_id=replica_id,
                timeout_s=0.25,
            )


def test_sigkill_singleton_delegates_to_batch_at_most_once() -> None:
    processes = _processes()
    signals: list[tuple[int, int]] = []
    process = FakeProcess(pid=100)
    registry = _registry(
        processes,
        getpgid=lambda pid: pid,
        killpg=lambda pgid, signal_number: signals.append(
            (pgid, signal_number)
        ),
        timestamps=(1000, 2000),
    )
    registry.register(
        name="replica-0",
        replica_id=0,
        process=process,
    )

    outcome = registry.sigkill_replica_group(
        fault_id="crash-0",
        replica_id=0,
        timeout_s=0.25,
    )

    assert outcome.fault_id == "crash-0"
    assert outcome.returncode == -int(signal.SIGKILL)
    assert registry.injected_sigkill_replica_ids == frozenset({0})
    with pytest.raises(RuntimeError, match="already"):
        registry.sigkill_replica_groups(
            (("crash-0-retry", 0),),
            timeout_s=0.25,
        )
    assert signals == [(100, int(signal.SIGKILL))]


def test_cleanup_is_idempotent_and_only_signals_registered_groups() -> None:
    processes = _processes()
    registered = FakeProcess(pid=100, wait_result=-int(signal.SIGINT))
    unrelated = FakeProcess(pid=200)
    by_pid = {registered.pid: registered}
    signals: list[tuple[int, int]] = []

    def killpg(pgid: int, signal_number: int) -> None:
        signals.append((pgid, signal_number))
        by_pid[pgid].returncode = -signal_number

    registry = _registry(
        processes,
        getpgid=lambda pid: pid,
        killpg=killpg,
    )
    registry.register(
        name="replica-0",
        replica_id=0,
        process=registered,
    )

    registry.cleanup(timeout_s=0.01)
    registry.cleanup(timeout_s=0.01)

    assert signals == [(100, int(signal.SIGINT))]
    assert unrelated.poll() is None
    assert all(pgid != unrelated.pid for pgid, _ in signals)


def test_cleanup_samples_after_sigint_timeout_before_sigterm_and_keeps_cleaning() -> None:
    processes = _processes()
    process = FakeProcess(pid=100, wait_result=-int(signal.SIGTERM))
    operations: list[tuple[str, int]] = []
    last_signal: int | None = None

    def killpg(pgid: int, signal_number: int) -> None:
        nonlocal last_signal
        assert pgid == process.pid
        last_signal = signal_number
        operations.append(("signal", signal_number))
        if signal_number == int(signal.SIGTERM):
            process.returncode = -signal_number

    def wait_for_exit(managed: FakeProcess, timeout: float) -> int:
        assert managed is process
        assert timeout == 0.01
        operations.append(("wait", int(last_signal or 0)))
        if last_signal == int(signal.SIGINT):
            raise subprocess.TimeoutExpired("replica-0", timeout)
        assert managed.returncode is not None
        return managed.returncode

    def sample(record: object) -> object:
        assert record.pid == process.pid
        operations.append(("sample", process.pid))
        raise RuntimeError("sample unavailable")

    registry = processes.ProcessRegistry(
        getpgid=lambda pid: pid,
        killpg=killpg,
        get_launcher_pgid=lambda: 9999,
        wait_for_exit=wait_for_exit,
        cleanup_escalation_hook=sample,
    )
    registry.register(
        name="replica-0",
        replica_id=0,
        process=process,
    )

    outcomes = registry.cleanup(timeout_s=0.01)

    assert operations == [
        ("signal", int(signal.SIGINT)),
        ("wait", int(signal.SIGINT)),
        ("sample", process.pid),
        ("signal", int(signal.SIGTERM)),
        ("wait", int(signal.SIGTERM)),
    ]
    assert outcomes[0].signal_number == int(signal.SIGTERM)
    assert registry.cleanup_escalations[0].status == "failed"
    assert registry.cleanup_escalations[0].error == "sample unavailable"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_real_sigkill_does_not_touch_an_unregistered_sentinel_group() -> None:
    processes = _processes()
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    target = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )

    try:
        registry = processes.ProcessRegistry()
        registry.register(
            name="replica-0",
            replica_id=0,
            process=target,
        )

        outcome = registry.sigkill_replica_group(
            fault_id="crash-real-0",
            replica_id=0,
            timeout_s=5.0,
        )

        assert outcome.returncode == -int(signal.SIGKILL)
        assert target.poll() == -int(signal.SIGKILL)
        assert sentinel.poll() is None
    finally:
        for process in (target, sentinel):
            if process.poll() is None:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=5.0)
