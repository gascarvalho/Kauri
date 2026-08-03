"""Failure-oriented tests for profiled-run process cleanup."""

from __future__ import annotations

import importlib
from pathlib import Path
import signal

import pytest


def _runtime():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.profiled_fault_runtime"
    )


class _Process:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class _SpawnedProcess(_Process):
    def __init__(self, pid: int) -> None:
        super().__init__(pid)
        self.kill_calls = 0
        self.wait_timeouts: list[float] = []

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -signal.SIGKILL

    def wait(self, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        if self.returncode is None:
            raise AssertionError("wait called before the process was signalled")
        return self.returncode


class _RejectingRegistry:
    def register(self, **_kwargs: object) -> None:
        raise RuntimeError("synthetic registration failure")


def test_spawn_registration_failure_kills_unregistered_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    process = _SpawnedProcess(8001)
    popen_arguments: dict[str, object] = {}

    def popen(*_args: object, **kwargs: object) -> _SpawnedProcess:
        popen_arguments.update(kwargs)
        return process

    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(runtime.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(runtime.os, "getpgrp", lambda: 7000)
    signals: list[tuple[int, int]] = []

    def killpg(pgid: int, signum: int) -> None:
        signals.append((pgid, signum))
        process.returncode = -signum

    monkeypatch.setattr(runtime.os, "killpg", killpg)

    with pytest.raises(RuntimeError, match="synthetic registration failure"):
        runtime.spawn_owned_process(
            _RejectingRegistry(),
            name="replica-0",
            replica_id=0,
            command=("synthetic-child",),
            log_path=tmp_path / "replica-0.log",
            working_directory=tmp_path,
        )

    assert signals == [(process.pid, signal.SIGKILL)]
    assert process.kill_calls == 0
    assert process.wait_timeouts == [5.0]
    assert popen_arguments["stdout"].closed


def test_spawn_registration_failure_uses_exact_child_if_group_identity_drifted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    process = _SpawnedProcess(8002)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(runtime.os, "getpgid", lambda _pid: 9002)
    monkeypatch.setattr(runtime.os, "getpgrp", lambda: 7000)
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda *_args: pytest.fail("a drifted process group must not be signalled"),
    )

    with pytest.raises(RuntimeError, match="synthetic registration failure"):
        runtime.spawn_owned_process(
            _RejectingRegistry(),
            name="replica-0",
            replica_id=0,
            command=("synthetic-child",),
            log_path=tmp_path / "replica-0.log",
            working_directory=tmp_path,
        )

    assert process.kill_calls == 1
    assert process.wait_timeouts == [5.0]


def test_cleanup_continues_after_one_group_signal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    first = _Process(8101)
    second = _Process(8102)
    records = (
        runtime.ProcessRecord("replica-1", 1, 8101, 8101, first),
        runtime.ProcessRecord("replica-2", 2, 8102, 8102, second),
    )
    monkeypatch.setattr(runtime, "monotonic_raw_ns", lambda: 90_000_000_000)
    clock = iter(range(0, 10_000, 20))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime.os, "getpgid", lambda pid: pid)
    sent: list[tuple[int, int]] = []

    def killpg(pgid: int, signum: int) -> None:
        sent.append((pgid, signum))
        if pgid == first.pid and signum == signal.SIGINT:
            raise OSError("synthetic signal failure")
        (first if pgid == first.pid else second).returncode = 0

    monkeypatch.setattr(runtime.os, "killpg", killpg)

    ledger, started_ns = runtime.concurrent_cleanup(
        records,
        faulted_replica_id=0,
        post_end_ns=80,
    )

    assert started_ns == 90_000_000_000
    assert any(pgid == second.pid for pgid, _ in sent)
    assert first.poll() == 0
    assert second.poll() == 0
    first_row = next(row for row in ledger if row["name"] == "replica-1")
    assert "synthetic signal failure" in " ".join(first_row["cleanup_errors"])


def test_cleanup_never_signals_a_drifted_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    drifted = _Process(8201)
    healthy = _Process(8202)
    records = (
        runtime.ProcessRecord("replica-1", 1, 8201, 8201, drifted),
        runtime.ProcessRecord("replica-2", 2, 8202, 8202, healthy),
    )
    monkeypatch.setattr(runtime, "monotonic_raw_ns", lambda: 90_000_000_000)
    clock = iter(range(0, 10_000, 20))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        runtime.os,
        "getpgid",
        lambda pid: 9999 if pid == drifted.pid else pid,
    )
    sent: list[int] = []

    def killpg(pgid: int, _signum: int) -> None:
        sent.append(pgid)
        healthy.returncode = 0

    monkeypatch.setattr(runtime.os, "killpg", killpg)

    ledger, _ = runtime.concurrent_cleanup(
        records,
        faulted_replica_id=0,
        post_end_ns=80,
    )

    assert drifted.pid not in sent
    assert healthy.pid in sent
    drifted_row = next(row for row in ledger if row["name"] == "replica-1")
    assert drifted_row["classification"] == "unexpected_exit"
    assert "identity changed" in " ".join(drifted_row["cleanup_errors"])


def test_cleanup_classifies_the_profile_selected_fault_replica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    crashed = _Process(8301)
    crashed.returncode = -signal.SIGKILL
    record = runtime.ProcessRecord("replica-1", 1, 8301, 8301, crashed)
    monkeypatch.setattr(runtime, "monotonic_raw_ns", lambda: 90_000_000_000)
    monkeypatch.setattr(runtime.os, "getpgrp", lambda: 7000)

    ledger, _ = runtime.concurrent_cleanup(
        (record,),
        faulted_replica_id=1,
        post_end_ns=80,
    )

    assert ledger[0]["classification"] == "expected_fault"


def test_cleanup_records_post_window_sigkill_as_forced_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    process = _Process(8401)
    record = runtime.ProcessRecord(
        "replica-2", 2, process.pid, process.pid, process
    )
    monkeypatch.setattr(runtime, "monotonic_raw_ns", lambda: 90_000_000_000)
    clock = iter(range(0, 100_000, 20))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime.os, "getpgid", lambda pid: pid)

    def killpg(_pgid: int, signum: int) -> None:
        if signum == signal.SIGKILL:
            process.returncode = -signal.SIGKILL

    monkeypatch.setattr(runtime.os, "killpg", killpg)

    ledger, _ = runtime.concurrent_cleanup(
        (record,),
        faulted_replica_id=1,
        post_end_ns=80_000_000_000,
    )

    assert ledger[0]["classification"] == "expected_forced_cleanup"
    assert ledger[0]["signals_sent"] == [
        int(signal.SIGINT),
        int(signal.SIGTERM),
        int(signal.SIGKILL),
    ]
    assert ledger[0]["cleanup_errors"] == []


def test_listening_ports_does_not_use_bind_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()

    class _Probe:
        def settimeout(self, _timeout: float) -> None:
            pass

        def connect_ex(self, address: tuple[str, int]) -> int:
            return 0 if address[1] == 25100 else 61

        def close(self) -> None:
            pass

    monkeypatch.setattr(runtime.socket, "socket", lambda *_args: _Probe())

    assert runtime.listening_ports((25100, 25101)) == (25100,)
