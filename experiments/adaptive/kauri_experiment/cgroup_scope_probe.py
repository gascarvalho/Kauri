"""One-shot, identity-bound cgroup cleanup probe for the W16 launcher.

This is deliberately a *disposable probe*, not a replica launcher.  It starts
one CPU-unconstrained ``sleep`` command in a cryptographically named transient
user scope, proves the live systemd and cgroup identities, then kills the
cgroup through a directory file descriptor.  It never stops a unit by name.

The module is not invoked by any experiment runner.  Its only purpose is to
make the cgroup-kill ownership primitive independently testable before W16 is
allowed to use it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
import os
from pathlib import PurePosixPath
import secrets
import subprocess
import time
from typing import Protocol


_SCOPE_PREFIX = "kauri-w16-cgroup-probe-"
_SCOPE_SUFFIX = ".scope"
_REQUIRED_SHOW_KEYS = frozenset({"ActiveState", "InvocationID", "ControlGroup"})
_POLL_INTERVAL_S = 0.05


class CgroupScopeProbeError(RuntimeError):
    """A one-shot probe failed, with its immutable partial receipt attached."""

    def __init__(self, message: str, *, receipt: CgroupScopeProbeReceipt) -> None:
        super().__init__(message)
        self.receipt = receipt


class _ScopeNotReady(RuntimeError):
    """The expected transient scope has not become active yet."""


class _Worker(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float) -> int: ...


class _CgroupDirectory(Protocol):
    """A directory-fd-bound cgroup object; paths are never re-opened by name."""

    dev: int
    ino: int

    def member_pids(self) -> tuple[int, ...]: ...

    def kill(self) -> None: ...

    def populated(self) -> int: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ScopeIdentity:
    """The systemd identity that must remain unchanged through cleanup."""

    unit: str
    invocation_id: str
    control_group: str


@dataclass(frozen=True, slots=True)
class CgroupScopeProbeReceipt:
    """Complete success or failure truth for exactly one disposable probe."""

    schema_version: int
    unit: str
    argv: tuple[str, ...]
    worker_pid: int | None
    scope_identity: ScopeIdentity | None
    cgroup_dev: int | None
    cgroup_ino: int | None
    initial_member_pids: tuple[int, ...]
    ownership_verified: bool
    identity_revalidated: bool
    cgroup_kill_attempted: bool
    populated_after_kill: int | None
    worker_exit_code: int | None
    deadline_exhausted: bool
    completed: bool
    failure: str | None


class _OsCgroupDirectory:
    """Linux cgroup-v2 directory operations rooted at one verified dirfd."""

    def __init__(self, *, fd: int, os_api: object) -> None:
        self._fd = fd
        self._os = os_api
        stat_result = self._os.fstat(fd)
        self.dev = int(stat_result.st_dev)
        self.ino = int(stat_result.st_ino)

    def member_pids(self) -> tuple[int, ...]:
        payload = self._read_file("cgroup.procs")
        try:
            pids = tuple(int(line) for line in payload.decode("ascii").splitlines())
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("cgroup.procs is not an ASCII PID list") from exc
        if not pids or any(pid <= 0 for pid in pids) or len(set(pids)) != len(pids):
            raise RuntimeError("cgroup.procs is empty or malformed")
        return pids

    def kill(self) -> None:
        flags = self._os.O_WRONLY | self._os.O_NOFOLLOW
        child_fd = self._os.open("cgroup.kill", flags, dir_fd=self._fd)
        try:
            written = self._os.write(child_fd, b"1")
            if written != 1:
                raise RuntimeError("cgroup.kill did not accept exactly one byte")
        finally:
            self._os.close(child_fd)

    def populated(self) -> int:
        payload = self._read_file("cgroup.events")
        rows: dict[str, str] = {}
        try:
            for raw in payload.decode("ascii").splitlines():
                key, value = raw.split(" ", 1)
                if key in rows:
                    raise ValueError("duplicate key")
                rows[key] = value
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("cgroup.events is malformed") from exc
        if set(rows) != {"populated", "frozen"} or rows["populated"] not in {"0", "1"}:
            raise RuntimeError("cgroup.events lacks an exact populated state")
        return int(rows["populated"])

    def close(self) -> None:
        self._os.close(self._fd)

    def _read_file(self, name: str) -> bytes:
        flags = self._os.O_RDONLY | self._os.O_NOFOLLOW
        child_fd = self._os.open(name, flags, dir_fd=self._fd)
        try:
            chunks: list[bytes] = []
            while True:
                chunk = self._os.read(child_fd, 65_536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            self._os.close(child_fd)


def _open_cgroup_directory(
    control_group: str,
    *,
    cgroup_root: str = "/sys/fs/cgroup",
    os_api: object = os,
) -> _CgroupDirectory:
    """Open only a verified cgroup-v2 child with O_NOFOLLOW and O_DIRECTORY."""

    group = _validate_control_group(control_group)
    root = PurePosixPath(cgroup_root)
    if not root.is_absolute():
        raise ValueError("cgroup root must be absolute")
    # ``group`` is absolute by systemd convention; join only validated pieces.
    path = str(root.joinpath(*PurePosixPath(group).parts[1:]))
    flags = os_api.O_RDONLY | os_api.O_DIRECTORY | os_api.O_NOFOLLOW
    fd = os_api.open(path, flags)
    try:
        return _OsCgroupDirectory(fd=fd, os_api=os_api)
    except BaseException:
        os_api.close(fd)
        raise


class CgroupScopeProbe:
    """Run one bounded identity-stable cgroup cleanup probe.

    All non-trivial dependencies are injectable so unit tests can model
    post-launch failures without creating scopes or touching ``/sys/fs/cgroup``.
    ``run`` intentionally performs no retry and has a hard 60-second budget.
    """

    def __init__(
        self,
        *,
        spawn: Callable[[Sequence[str]], _Worker] | None = None,
        show: Callable[[str, float], str] | None = None,
        open_cgroup: Callable[[str], _CgroupDirectory] | None = None,
        token_hex: Callable[[int], str] = secrets.token_hex,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        getpgid: Callable[[int], int] = os.getpgid,
        launcher_pgid: Callable[[], int] = os.getpgrp,
    ) -> None:
        self._spawn = spawn or self._default_spawn
        self._show = show or self._default_show
        self._open_cgroup = open_cgroup or _open_cgroup_directory
        self._token_hex = token_hex
        self._monotonic = monotonic
        self._sleep = sleep
        self._getpgid = getpgid
        self._launcher_pgid = launcher_pgid

    def run(self) -> CgroupScopeProbeReceipt:
        """Launch once, kill through dirfd once, and return a sealed receipt."""

        started = self._monotonic()
        deadline = started + 60.0
        unit = self._new_unit()
        argv = (
            "systemd-run",
            "--user",
            "--scope",
            "--collect",
            "--quiet",
            f"--unit={unit}",
            "--",
            "sleep",
            "60",
        )
        receipt = CgroupScopeProbeReceipt(
            schema_version=1,
            unit=unit,
            argv=argv,
            worker_pid=None,
            scope_identity=None,
            cgroup_dev=None,
            cgroup_ino=None,
            initial_member_pids=(),
            ownership_verified=False,
            identity_revalidated=False,
            cgroup_kill_attempted=False,
            populated_after_kill=None,
            worker_exit_code=None,
            deadline_exhausted=False,
            completed=False,
            failure=None,
        )
        worker: _Worker | None = None
        directory: _CgroupDirectory | None = None
        try:
            worker = self._spawn(argv)
            receipt = replace(receipt, worker_pid=int(worker.pid))
            identity = self._wait_for_identity(unit, deadline)
            receipt = replace(receipt, scope_identity=identity)
            directory = self._open_cgroup(identity.control_group)
            pids = directory.member_pids()
            receipt = replace(
                receipt,
                cgroup_dev=directory.dev,
                cgroup_ino=directory.ino,
                initial_member_pids=pids,
            )
            worker_pid = int(worker.pid)
            if (
                worker.poll() is not None
                or worker_pid <= 1
                or worker_pid not in pids
                or self._getpgid(worker_pid) != worker_pid
                or worker_pid == self._launcher_pgid()
            ):
                raise RuntimeError("opened cgroup does not contain the live private worker group")
            receipt = replace(receipt, ownership_verified=True)
            # A unit name is reusable after collection.  Re-read live systemd
            # state immediately before the irreversible cgroup.kill write.
            current = self._wait_for_identity(unit, deadline)
            if current != identity:
                raise RuntimeError("scope identity changed before cgroup.kill")
            receipt = replace(receipt, identity_revalidated=True)
            # Persist the attempt before the write: the kernel may apply it
            # and then surface an I/O error.  A failed call is never retried.
            receipt = replace(receipt, cgroup_kill_attempted=True)
            directory.kill()
            populated = self._wait_for_unpopulated(directory, deadline)
            exit_code = self._wait_for_worker(worker, deadline)
            return replace(
                receipt,
                populated_after_kill=populated,
                worker_exit_code=exit_code,
                completed=True,
            )
        except BaseException as exc:
            # If a verified directory exists but the regular path failed before
            # cgroup.kill, do one best-effort kill.  The receipt distinguishes
            # the attempt from successful emptiness; no name-based stop occurs.
            if directory is not None and receipt.ownership_verified and not receipt.cgroup_kill_attempted:
                try:
                    receipt = replace(receipt, cgroup_kill_attempted=True)
                    directory.kill()
                except BaseException as cleanup_exc:
                    exc = RuntimeError(f"{self._detail(exc)}; cleanup kill failed: {self._detail(cleanup_exc)}")
            if directory is not None and receipt.cgroup_kill_attempted:
                try:
                    receipt = replace(
                        receipt,
                        populated_after_kill=self._wait_for_unpopulated(directory, deadline),
                    )
                except BaseException:
                    pass
            if worker is not None:
                try:
                    receipt = replace(
                        receipt,
                        worker_exit_code=self._wait_for_worker(worker, deadline),
                    )
                except BaseException:
                    pass
            exhausted = self._monotonic() >= deadline
            failed = replace(
                receipt,
                deadline_exhausted=exhausted,
                failure=self._detail(exc),
            )
            raise CgroupScopeProbeError(failed.failure or "cgroup scope probe failed", receipt=failed) from exc
        finally:
            if directory is not None:
                directory.close()

    def _new_unit(self) -> str:
        token = self._token_hex(16)
        if len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
            raise RuntimeError("scope random token is not a 128-bit lowercase hexadecimal value")
        return f"{_SCOPE_PREFIX}{token}{_SCOPE_SUFFIX}"

    def _wait_for_identity(self, unit: str, deadline: float) -> ScopeIdentity:
        last: str | None = None
        while self._monotonic() < deadline:
            try:
                last = self._show(unit, self._remaining(deadline))
                return _parse_active_identity(unit, last)
            except _ScopeNotReady:
                self._sleep(min(_POLL_INTERVAL_S, self._remaining(deadline)))
        raise TimeoutError(f"scope {unit} did not expose an active identity: {last}")

    def _wait_for_unpopulated(self, directory: _CgroupDirectory, deadline: float) -> int:
        while self._monotonic() < deadline:
            populated = directory.populated()
            if populated == 0:
                return populated
            self._sleep(min(_POLL_INTERVAL_S, self._remaining(deadline)))
        raise TimeoutError("cgroup.events did not report populated 0")

    def _wait_for_worker(self, worker: _Worker, deadline: float) -> int:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            raise TimeoutError("hard probe deadline elapsed before worker exit")
        return int(worker.wait(timeout=remaining))

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - self._monotonic())

    @staticmethod
    def _default_spawn(argv: Sequence[str]) -> _Worker:
        return subprocess.Popen(
            tuple(argv), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    def _default_show(unit: str, timeout: float) -> str:
        if timeout <= 0:
            raise TimeoutError("hard probe deadline elapsed before systemd inspection")
        completed = subprocess.run(
            (
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=ActiveState",
                "--property=InvocationID",
                "--property=ControlGroup",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode != 0 and (
            "LoadState=not-found" in completed.stdout
            or "could not be found" in completed.stderr.lower()
        ):
            raise _ScopeNotReady("transient scope not registered yet")
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "systemctl show failed")
        return completed.stdout

    @staticmethod
    def _detail(exc: BaseException) -> str:
        return str(exc).strip() or type(exc).__name__


def _parse_active_identity(unit: str, payload: str) -> ScopeIdentity:
    rows: dict[str, str] = {}
    for raw in payload.splitlines():
        key, separator, value = raw.partition("=")
        if not separator or not key or key in rows:
            raise RuntimeError("systemd show output is malformed")
        rows[key] = value
    if set(rows) != _REQUIRED_SHOW_KEYS:
        raise RuntimeError("systemd show output schema drifted")
    if rows["ActiveState"] != "active" or not rows["InvocationID"]:
        raise _ScopeNotReady("scope is not active with an invocation identity")
    return ScopeIdentity(
        unit=unit,
        invocation_id=rows["InvocationID"],
        control_group=_validate_control_group(rows["ControlGroup"]),
    )


def _validate_control_group(value: str) -> str:
    path = PurePosixPath(value)
    if not isinstance(value, str) or not path.is_absolute() or value == "/":
        raise RuntimeError("systemd control group is not an absolute child path")
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise RuntimeError("systemd control group contains an unsafe path component")
    return str(path)
