"""Adversarial tests for the W16 disposable cgroup-scope probe."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from experiments.adaptive.kauri_experiment.cgroup_scope_probe import (
    CgroupScopeProbe,
    CgroupScopeProbeError,
    _OsCgroupDirectory,
    _ScopeNotReady,
    _open_cgroup_directory,
)


class _Worker:
    pid = 7123

    def __init__(self) -> None:
        self.wait_timeouts: list[float] = []

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        return -9


@dataclass
class _Directory:
    dev: int = 81
    ino: int = 99
    members: tuple[int, ...] = (7123, 7124)
    after_kill_populated: int = 0
    kill_count: int = 0
    closed: bool = False
    kill_raises: bool = False

    def member_pids(self) -> tuple[int, ...]:
        return self.members

    def kill(self) -> None:
        self.kill_count += 1
        if self.kill_raises:
            raise OSError("uncertain write")

    def populated(self) -> int:
        return self.after_kill_populated

    def close(self) -> None:
        self.closed = True


def _active(invocation: str = "invocation-a", group: str = "/user.slice/probe.scope") -> str:
    return f"ActiveState=active\nInvocationID={invocation}\nControlGroup={group}\n"


def test_probe_uses_one_cpu_free_sleep_scope_and_proves_dirfd_cleanup() -> None:
    worker = _Worker()
    directory = _Directory()
    spawned: list[tuple[str, ...]] = []
    show_calls: list[str] = []

    probe = CgroupScopeProbe(
        spawn=lambda argv: (spawned.append(tuple(argv)) or worker),
        show=lambda unit, _timeout: (show_calls.append(unit) or _active()),
        open_cgroup=lambda _group: directory,
        token_hex=lambda _bytes: "ab" * 16,
        getpgid=lambda pid: pid,
        launcher_pgid=lambda: 1,
    )

    receipt = probe.run()

    assert len(spawned) == 1
    assert receipt.argv == (
        "systemd-run", "--user", "--scope", "--collect", "--quiet",
        "--unit=kauri-w16-cgroup-probe-" + "ab" * 16 + ".scope",
        "--", "sleep", "60",
    )
    assert not any("CPU" in argument for argument in receipt.argv)
    assert len(show_calls) == 2
    assert receipt.initial_member_pids == (7123, 7124)
    assert (receipt.cgroup_dev, receipt.cgroup_ino) == (81, 99)
    assert receipt.identity_revalidated is True
    assert receipt.cgroup_kill_attempted is True
    assert receipt.populated_after_kill == 0
    assert receipt.worker_exit_code == -9
    assert receipt.completed is True
    assert directory.kill_count == 1
    assert directory.closed is True


def test_identity_change_before_kill_fails_closed_without_killing_new_scope() -> None:
    worker = _Worker()
    directory = _Directory()
    identities = iter((_active("first"), _active("recreated")))
    probe = CgroupScopeProbe(
        spawn=lambda _argv: worker,
        show=lambda _unit, _timeout: next(identities),
        open_cgroup=lambda _group: directory,
        token_hex=lambda _bytes: "cd" * 16,
        getpgid=lambda pid: pid,
        launcher_pgid=lambda: 1,
    )

    with pytest.raises(CgroupScopeProbeError, match="identity changed") as raised:
        probe.run()

    # The directory is the original opened identity.  Its one cleanup kill is
    # allowed; no operation can target the recreated unit by its name.
    assert directory.kill_count == 1
    assert raised.value.receipt.identity_revalidated is False
    assert raised.value.receipt.cgroup_kill_attempted is True
    assert raised.value.receipt.completed is False
    assert raised.value.receipt.populated_after_kill == 0
    assert directory.closed is True


def test_failure_before_directory_open_preserves_truth_and_never_kills() -> None:
    worker = _Worker()
    opened = False
    probe = CgroupScopeProbe(
        spawn=lambda _argv: worker,
        show=lambda _unit, _timeout: _active(group="/../unsafe"),
        open_cgroup=lambda _group: (_ for _ in ()).throw(AssertionError("must not open")),
        token_hex=lambda _bytes: "ef" * 16,
        getpgid=lambda pid: pid,
        launcher_pgid=lambda: 1,
    )

    with pytest.raises(CgroupScopeProbeError, match="unsafe") as raised:
        probe.run()

    assert opened is False
    assert raised.value.receipt.cgroup_kill_attempted is False
    assert raised.value.receipt.worker_exit_code == -9
    assert raised.value.receipt.failure is not None


def test_uncertain_cgroup_kill_is_recorded_once_and_never_retried() -> None:
    worker = _Worker()
    directory = _Directory(kill_raises=True)
    probe = CgroupScopeProbe(
        spawn=lambda _argv: worker,
        show=lambda _unit, _timeout: _active(),
        open_cgroup=lambda _group: directory,
        token_hex=lambda _bytes: "12" * 16,
        getpgid=lambda pid: pid,
        launcher_pgid=lambda: 1,
    )

    with pytest.raises(CgroupScopeProbeError, match="uncertain write") as raised:
        probe.run()

    assert directory.kill_count == 1
    assert raised.value.receipt.cgroup_kill_attempted is True
    assert raised.value.receipt.completed is False
    assert raised.value.receipt.worker_exit_code == -9


def test_disjoint_cgroup_membership_never_kills_an_unowned_directory() -> None:
    directory = _Directory(members=(9999,))
    probe = CgroupScopeProbe(
        spawn=lambda _argv: _Worker(),
        show=lambda _unit, _timeout: _active(),
        open_cgroup=lambda _group: directory,
        token_hex=lambda _bytes: "34" * 16,
        getpgid=lambda pid: pid,
        launcher_pgid=lambda: 1,
    )
    with pytest.raises(CgroupScopeProbeError, match="does not contain") as raised:
        probe.run()
    assert directory.kill_count == 0
    assert raised.value.receipt.ownership_verified is False
    assert raised.value.receipt.cgroup_kill_attempted is False
    assert directory.closed is True


def test_not_yet_registered_scope_is_retried_before_identity_binding() -> None:
    calls = 0
    directory = _Directory()

    def show(_unit: str, _timeout: float) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _ScopeNotReady("transient scope not registered yet")
        return _active()

    probe = CgroupScopeProbe(
        spawn=lambda _argv: _Worker(),
        show=show,
        open_cgroup=lambda _group: directory,
        token_hex=lambda _bytes: "56" * 16,
        getpgid=lambda pid: pid,
        launcher_pgid=lambda: 1,
    )
    assert probe.run().completed is True
    assert calls == 3


class _Stat:
    st_dev = 123
    st_ino = 456


class _FakeOs:
    O_RDONLY = 1
    O_WRONLY = 2
    O_DIRECTORY = 4
    O_NOFOLLOW = 8

    def __init__(self) -> None:
        self.opens: list[tuple[str, int, int | None]] = []
        self.closed: list[int] = []
        self.writes: list[tuple[int, bytes]] = []
        self._next = 40
        self._payload_by_fd: dict[int, bytes] = {}
        self.files = {
            "cgroup.procs": b"11\n12\n",
            "cgroup.events": b"populated 0\nfrozen 0\n",
        }

    def open(self, path: str, flags: int, dir_fd: int | None = None) -> int:
        self.opens.append((path, flags, dir_fd))
        result = self._next
        self._next += 1
        if path in self.files:
            self._payload_by_fd[result] = self.files[path]
        return result

    def close(self, fd: int) -> None:
        self.closed.append(fd)

    def fstat(self, _fd: int) -> _Stat:
        return _Stat()

    def read(self, fd: int, _size: int) -> bytes:
        return self._payload_by_fd.pop(fd, b"")

    def write(self, fd: int, payload: bytes) -> int:
        self.writes.append((fd, payload))
        return len(payload)


def test_cgroup_files_are_opened_only_beneath_a_no_follow_directory_fd() -> None:
    fake_os = _FakeOs()
    directory = _open_cgroup_directory(
        "/user.slice/probe.scope", cgroup_root="/safe/cgroup", os_api=fake_os
    )
    assert directory.member_pids() == (11, 12)
    directory.kill()
    assert directory.populated() == 0
    directory.close()

    assert fake_os.opens[0] == (
        "/safe/cgroup/user.slice/probe.scope",
        fake_os.O_RDONLY | fake_os.O_DIRECTORY | fake_os.O_NOFOLLOW,
        None,
    )
    assert fake_os.opens[1] == (
        "cgroup.procs", fake_os.O_RDONLY | fake_os.O_NOFOLLOW, 40
    )
    assert fake_os.opens[2] == (
        "cgroup.kill", fake_os.O_WRONLY | fake_os.O_NOFOLLOW, 40
    )
    assert fake_os.opens[3] == (
        "cgroup.events", fake_os.O_RDONLY | fake_os.O_NOFOLLOW, 40
    )
    assert fake_os.writes == [(42, b"1")]


def test_malformed_or_symlink_escape_control_group_is_rejected_before_open() -> None:
    fake_os = _FakeOs()
    with pytest.raises(RuntimeError, match="unsafe"):
        _open_cgroup_directory("/user.slice/../escape", os_api=fake_os)
    assert fake_os.opens == []


def test_directory_rejects_nonempty_or_schema_drifted_events() -> None:
    fake_os = _FakeOs()
    fake_os.files["cgroup.events"] = b"populated 1\nfrozen 0\n"
    directory = _OsCgroupDirectory(fd=40, os_api=fake_os)
    assert directory.populated() == 1
    fake_os.files["cgroup.events"] = b"populated 0\n"
    with pytest.raises(RuntimeError, match="exact populated"):
        directory.populated()
