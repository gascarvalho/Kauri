"""Fail-closed ownership seam for a future static-resource experiment runner.

The caller may stop a transient scope only after its quota verifier returns an
``OwnedScopeHandle`` bound to the registered process PID and PGID. Before that
handle exists, the scope name is untrusted: this module only observes it and
may signal a repeatedly revalidated private process group.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
import signal
import subprocess
from typing import Protocol

from .processes import ManagedProcess, ProcessRecord, ProcessRegistry


class StaticResourceOwnershipError(RuntimeError):
    """A process or scope could not be proved and cleaned safely."""

    def __init__(self, message: str, *, receipt: CleanupReceipt | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


class ScopeOperations(Protocol):
    """Injected exact-scope operations; callers must not use them directly."""

    def stop_if_matches(self, handle: OwnedScopeHandle) -> bool: ...

    def inspect(self, unit: str) -> ScopeSnapshot: ...


@dataclass(frozen=True, slots=True)
class ScopeSnapshot:
    """Read-only identity of one currently observed systemd scope."""

    active: bool
    invocation_id: str | None
    control_group: str | None
    member_pids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class OwnedScopeHandle:
    """Verifier-issued proof that this exact scope belongs to one process group."""

    unit: str
    pid: int
    pgid: int
    invocation_id: str
    control_group: str


@dataclass(frozen=True, slots=True)
class OwnedLaunch:
    """A registered process and its identity-bound scope cleanup capability."""

    record: ProcessRecord
    scope: OwnedScopeHandle


@dataclass(frozen=True, slots=True)
class CleanupReceipt:
    """Local outcome of a failed launch; suitable for a future abort receipt."""

    unit: str
    pid: int | None
    pgid: int | None
    group_proven: bool
    scope_handle_bound: bool
    scope_stop_attempted: bool
    scope_inactive: bool
    signals: tuple[int, ...]
    uncertainty: str | None


class StaticResourceProcessOwner:
    """Make process registration and scope proof an atomic ownership boundary."""

    _SIGNALS = (int(signal.SIGINT), int(signal.SIGTERM), int(signal.SIGKILL))

    def __init__(
        self,
        *,
        scopes: ScopeOperations,
        getpgid: Callable[[int], int] = os.getpgid,
        get_launcher_pgid: Callable[[], int] = os.getpgrp,
        killpg: Callable[[int, int], None] = os.killpg,
        wait_for_exit: Callable[[ManagedProcess, float], int] | None = None,
    ) -> None:
        self._scopes = scopes
        self._getpgid = getpgid
        self._get_launcher_pgid = get_launcher_pgid
        self._killpg = killpg
        self._wait_for_exit = wait_for_exit or self._default_wait_for_exit

    @staticmethod
    def _default_wait_for_exit(process: ManagedProcess, timeout_s: float) -> int:
        return process.wait(timeout=timeout_s)

    def spawn_registered_and_verified(
        self,
        *,
        registry: ProcessRegistry,
        name: str,
        replica_id: int,
        unit: str,
        spawn: Callable[[], ManagedProcess],
        verify_quota: Callable[[ProcessRecord], OwnedScopeHandle],
        timeout_s: float,
    ) -> OwnedLaunch:
        """Return an owned launch, or fail without mutating an unproved scope."""

        self._validate_inputs(unit=unit, timeout_s=timeout_s)
        process: ManagedProcess | None = None
        try:
            process = spawn()
            record = registry.register(
                name=name,
                replica_id=replica_id,
                process=process,
            )
            handle = verify_quota(record)
            self._validate_handle(handle, record=record, unit=unit)
            return OwnedLaunch(record=record, scope=handle)
        except BaseException as original:
            receipt = self._cleanup(
                process=process,
                unit=unit,
                scope_handle=None,
                timeout_s=timeout_s,
            )
            raise StaticResourceOwnershipError(
                "static-resource launch failed before an identity-bound scope "
                f"handle existed for {unit}; cleanup is unproven: "
                f"{receipt.uncertainty or 'scope only checked read-only'}",
                receipt=receipt,
            ) from original

    def cleanup_owned_launch(
        self, launch: OwnedLaunch, *, timeout_s: float
    ) -> CleanupReceipt:
        """Clean a launch only after validating its verifier-issued scope handle."""

        self._validate_inputs(unit=launch.scope.unit, timeout_s=timeout_s)
        self._validate_handle(launch.scope, record=launch.record, unit=launch.scope.unit)
        return self._cleanup(
            process=launch.record.process,
            unit=launch.scope.unit,
            scope_handle=launch.scope,
            timeout_s=timeout_s,
        )

    def _cleanup(
        self,
        *,
        process: ManagedProcess | None,
        unit: str,
        scope_handle: OwnedScopeHandle | None,
        timeout_s: float,
    ) -> CleanupReceipt:
        """Signal only a repeatedly revalidated private group; scope stop needs proof."""

        scope_stop_attempted = False
        uncertainty: str | None = None
        scope_handle_bound = scope_handle is not None
        if scope_handle is not None:
            snapshot, inspect_error = self._inspect_scope(unit)
            if inspect_error is not None:
                uncertainty = self._append_uncertainty(uncertainty, inspect_error)
            elif not self._scope_matches(snapshot=snapshot, handle=scope_handle):
                uncertainty = self._append_uncertainty(
                    uncertainty,
                    "scope identity is gone or differs from its bound invocation",
                )
            else:
                # Record the attempt before the call: systemd may apply the
                # stop and then report a transport/collection error.
                scope_stop_attempted = True
                try:
                    if not self._scopes.stop_if_matches(scope_handle):
                        uncertainty = self._append_uncertainty(
                            uncertainty,
                            "scope identity changed before conditioned stop",
                        )
                except BaseException as exc:
                    uncertainty = self._append_uncertainty(
                        uncertainty, f"scope cleanup failed: {self._detail(exc)}"
                    )

        # The scope has the whole cgroup and is the primary cleanup tool. A
        # private PGID is only a fallback for a still-live launcher process.
        pid, pgid, group_proven, signals, group_uncertainty = self._cleanup_group(
            process=process,
            timeout_s=timeout_s,
        )
        if group_uncertainty is not None:
            uncertainty = self._append_uncertainty(uncertainty, group_uncertainty)
        snapshot, inspect_error = self._inspect_scope(unit)
        if inspect_error is not None:
            scope_inactive = False
            uncertainty = self._append_uncertainty(uncertainty, inspect_error)
        else:
            scope_inactive = not snapshot.active and not snapshot.member_pids
        if not scope_handle_bound:
            uncertainty = self._append_uncertainty(
                uncertainty, "scope has no identity-bound ownership handle"
            )
        elif not scope_inactive:
            uncertainty = self._append_uncertainty(
                uncertainty, f"scope {unit} did not become inactive"
            )
        return CleanupReceipt(
            unit=unit,
            pid=pid,
            pgid=pgid,
            group_proven=group_proven,
            scope_handle_bound=scope_handle_bound,
            scope_stop_attempted=scope_stop_attempted,
            scope_inactive=scope_inactive,
            signals=tuple(signals),
            uncertainty=uncertainty,
        )

    def _cleanup_group(
        self, *, process: ManagedProcess | None, timeout_s: float
    ) -> tuple[int | None, int | None, bool, list[int], str | None]:
        """Use a private PGID only after scope cleanup had its chance."""

        if process is None:
            return None, None, False, [], None
        pid = int(process.pid)
        pgid: int | None = None
        signals: list[int] = []
        if process.poll() is not None:
            return pid, pgid, False, signals, None
        for signal_number in self._SIGNALS:
            if process.poll() is not None:
                return pid, pgid, True, signals, None
            try:
                candidate = self._private_group(pid)
            except OSError as exc:
                return pid, pgid, False, signals, f"cannot inspect spawned PID {pid}: {exc}"
            if candidate is None:
                return (
                    pid,
                    pgid,
                    False,
                    signals,
                    f"spawned PID {pid} is not a proven private process group",
                )
            pgid = candidate
            try:
                self._killpg(candidate, signal_number)
                signals.append(signal_number)
                self._wait_for_exit(process, timeout_s)
            except (subprocess.TimeoutExpired, TimeoutError):
                continue
            except OSError as exc:
                return (
                    pid,
                    pgid,
                    True,
                    signals,
                    f"cannot signal proven group {candidate}: {exc}",
                )
        if process.poll() is None:
            return pid, pgid, True, signals, f"proven process group {pgid} remained live"
        return pid, pgid, True, signals, None

    def _private_group(self, pid: int) -> int | None:
        pgid = int(self._getpgid(pid))
        if pid <= 1 or pgid != pid or pgid == int(self._get_launcher_pgid()):
            return None
        return pgid

    @staticmethod
    def _validate_handle(
        handle: OwnedScopeHandle,
        *,
        record: ProcessRecord,
        unit: str,
    ) -> None:
        if not isinstance(handle, OwnedScopeHandle):
            raise TypeError("quota verifier did not return an owned scope handle")
        if (handle.unit, handle.pid, handle.pgid) != (
            unit,
            record.pid,
            record.pgid,
        ):
            raise StaticResourceOwnershipError(
                "scope handle is not bound to the exact registered process identity"
            )
        if not handle.invocation_id or not handle.control_group:
            raise StaticResourceOwnershipError(
                "scope handle lacks stable invocation and control-group identity"
            )

    def _inspect_scope(self, unit: str) -> tuple[ScopeSnapshot | None, str | None]:
        try:
            snapshot = self._scopes.inspect(unit)
        except BaseException as exc:
            return None, f"cannot inspect scope: {self._detail(exc)}"
        if not isinstance(snapshot, ScopeSnapshot):
            return None, "scope inspector returned an invalid identity snapshot"
        return snapshot, None

    @staticmethod
    def _scope_matches(*, snapshot: ScopeSnapshot, handle: OwnedScopeHandle) -> bool:
        return (
            snapshot.active
            and snapshot.invocation_id == handle.invocation_id
            and snapshot.control_group == handle.control_group
            and handle.pid in snapshot.member_pids
        )

    @staticmethod
    def _append_uncertainty(current: str | None, added: str) -> str:
        return f"{current}; {added}" if current else added

    @staticmethod
    def _detail(exc: BaseException) -> str:
        return str(exc).strip() or type(exc).__name__

    @staticmethod
    def _validate_inputs(*, unit: str, timeout_s: float) -> None:
        if not isinstance(unit, str) or not unit.endswith(".scope"):
            raise ValueError("unit must be an exact transient .scope name")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be greater than zero")
