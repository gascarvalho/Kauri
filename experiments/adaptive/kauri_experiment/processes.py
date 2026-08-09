"""Process-group ownership and lifecycle controls for adaptive experiments.

The registry is deliberately protocol agnostic.  It only knows about local
process identities that the experiment launcher created in their own POSIX
sessions.  Fault adapters can therefore signal a registered process group
without accepting an arbitrary PID or PGID from a fault plan.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import signal
import subprocess
import time
from typing import Callable, Protocol, Sequence


class ManagedProcess(Protocol):
    """Small process interface needed by the registry."""

    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float) -> int: ...


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    """Immutable identity of one launcher-owned process group."""

    name: str
    replica_id: int
    pid: int
    pgid: int
    process: ManagedProcess


@dataclass(frozen=True, slots=True)
class SigkillOutcome:
    """Bounded evidence for one confirmed process-group crash."""

    fault_id: str
    name: str
    replica_id: int
    pid: int
    pgid: int
    signal_number: int
    returncode: int
    requested_monotonic_ns: int
    confirmed_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class SigkillBatchResult:
    """Ordered truth for one reserved action in a SIGKILL batch."""

    fault_id: str
    replica_id: int
    status: str
    outcome: SigkillOutcome | None
    error: str | None
    name: str
    pid: int
    pgid: int
    signal_number: int
    requested_monotonic_ns: int | None


class SigkillBatchError(RuntimeError):
    """A partially or wholly failed SIGKILL batch with per-action truth."""

    def __init__(self, results: Sequence[SigkillBatchResult]) -> None:
        ordered = tuple(results)
        if not ordered:
            raise ValueError("SIGKILL batch error requires result evidence")
        self.results = ordered
        failures = [
            result.error
            for result in ordered
            if result.status != "succeeded" and result.error is not None
        ]
        detail = "; ".join(failures) or "incomplete per-action outcomes"
        super().__init__(f"SIGKILL batch failed: {detail}")


@dataclass(frozen=True, slots=True)
class CleanupOutcome:
    """Bounded cleanup result for a process that needed a signal."""

    name: str
    replica_id: int
    pid: int
    pgid: int
    signal_number: int
    returncode: int


@dataclass(frozen=True, slots=True)
class CleanupEscalationResult:
    """Diagnostic result returned before cleanup advances past SIGINT."""

    status: str
    artifact_relative_path: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class CleanupEscalationOutcome:
    """Identity-bound record of one pre-escalation diagnostic attempt."""

    name: str
    replica_id: int
    pid: int
    pgid: int
    after_signal_number: int
    before_signal_number: int
    status: str
    artifact_relative_path: str | None
    error: str | None


class ProcessRegistry:
    """Own the safe signalling boundary for launcher-created processes."""

    _CLEANUP_SIGNALS = (
        int(signal.SIGINT),
        int(signal.SIGTERM),
        int(signal.SIGKILL),
    )

    def __init__(
        self,
        *,
        getpgid: Callable[[int], int] = os.getpgid,
        killpg: Callable[[int, int], None] = os.killpg,
        get_launcher_pgid: Callable[[], int] = os.getpgrp,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        wait_for_exit: Callable[[ManagedProcess, float], int] | None = None,
        cleanup_escalation_hook: (
            Callable[[ProcessRecord], CleanupEscalationResult] | None
        ) = None,
    ) -> None:
        self._getpgid = getpgid
        self._killpg = killpg
        self._get_launcher_pgid = get_launcher_pgid
        self._monotonic_ns = monotonic_ns
        self._wait_for_exit = wait_for_exit or self._default_wait_for_exit
        self._cleanup_escalation_hook = cleanup_escalation_hook

        self._records_by_name: dict[str, ProcessRecord] = {}
        self._records_by_replica: dict[int, ProcessRecord] = {}
        self._records_by_pid: dict[int, ProcessRecord] = {}
        self._records_by_pgid: dict[int, ProcessRecord] = {}
        self._sigkill_fault_ids: set[str] = set()
        self._sigkill_replica_ids: set[int] = set()
        self._confirmed_sigkill_replica_ids: set[int] = set()
        self._cleanup_started = False
        self._cleanup_outcomes: tuple[CleanupOutcome, ...] = ()
        self._cleanup_escalations: tuple[CleanupEscalationOutcome, ...] = ()

    @staticmethod
    def _default_wait_for_exit(
        process: ManagedProcess,
        timeout: float,
    ) -> int:
        return process.wait(timeout=timeout)

    @property
    def records(self) -> tuple[ProcessRecord, ...]:
        """Return records in registration order."""

        return tuple(self._records_by_name.values())

    @property
    def injected_sigkill_replica_ids(self) -> frozenset[int]:
        """Return replica identities reserved for deliberate hard faults."""

        return frozenset(self._confirmed_sigkill_replica_ids)

    @property
    def cleanup_escalations(self) -> tuple[CleanupEscalationOutcome, ...]:
        """Return diagnostics captured after SIGINT failed to stop a group."""

        return self._cleanup_escalations

    def record_for_replica(self, replica_id: int) -> ProcessRecord:
        """Return a registered replica or fail without accepting raw PIDs."""

        try:
            return self._records_by_replica[replica_id]
        except KeyError as exc:
            raise KeyError(f"replica {replica_id} is not registered") from exc

    def register(
        self,
        *,
        name: str,
        replica_id: int,
        process: ManagedProcess,
    ) -> ProcessRecord:
        """Register one live process that leads its own safe process group."""

        if self._cleanup_started:
            raise RuntimeError("cannot register after cleanup has started")
        if not isinstance(name, str) or not name:
            raise ValueError("process name must be a non-empty string")
        if name in self._records_by_name:
            raise ValueError(f"duplicate process name: {name}")
        if replica_id in self._records_by_replica:
            raise ValueError(f"duplicate replica identity: {replica_id}")

        pid = int(process.pid)
        if pid in self._records_by_pid:
            raise ValueError(f"duplicate pid identity: {pid}")
        if process.poll() is not None:
            raise ValueError(f"process {name} has already exited")

        try:
            pgid = int(self._getpgid(pid))
        except ProcessLookupError as exc:
            raise ValueError(f"process {name} is not running") from exc

        self._validate_group_shape(name=name, pid=pid, pgid=pgid)
        if pgid in self._records_by_pgid:
            raise ValueError(f"duplicate pgid identity: {pgid}")

        record = ProcessRecord(
            name=name,
            replica_id=replica_id,
            pid=pid,
            pgid=pgid,
            process=process,
        )
        self._records_by_name[name] = record
        self._records_by_replica[replica_id] = record
        self._records_by_pid[pid] = record
        self._records_by_pgid[pgid] = record
        return record

    def sigkill_replica_group(
        self,
        *,
        fault_id: str,
        replica_id: int,
        timeout_s: float,
    ) -> SigkillOutcome:
        """SIGKILL one registered replica group and confirm the exact exit."""

        return self.sigkill_replica_groups(
            ((fault_id, replica_id),),
            timeout_s=timeout_s,
        )[0]

    def sigkill_replica_groups(
        self,
        requests: Sequence[tuple[str, int]],
        timeout_s: float,
    ) -> tuple[SigkillOutcome, ...]:
        """SIGKILL a validated batch before waiting for exact exits.

        The complete request is validated and reserved before the first
        signal.  Once signalling begins, every identity remains reserved even
        if an individual signal or wait fails, preventing an unsafe retry from
        targeting the same process group twice.
        """

        self._validate_timeout(timeout_s)
        if self._cleanup_started:
            raise RuntimeError("cannot inject SIGKILL after cleanup has started")
        if isinstance(requests, (str, bytes)) or not isinstance(
            requests,
            Sequence,
        ):
            raise TypeError("requests must be a sequence of (fault_id, replica_id)")

        validated: list[tuple[str, ProcessRecord]] = []
        batch_fault_ids: set[str] = set()
        batch_replica_ids: set[int] = set()
        for index, request in enumerate(requests):
            if not isinstance(request, tuple) or len(request) != 2:
                raise TypeError(
                    f"SIGKILL request {index} must be a "
                    "(fault_id, replica_id) tuple"
                )
            fault_id, replica_id = request
            if not isinstance(fault_id, str) or not fault_id.strip():
                raise ValueError("fault_id must be a non-empty string")
            if isinstance(replica_id, bool) or not isinstance(replica_id, int):
                raise ValueError("replica_id must be an integer")
            if fault_id in self._sigkill_fault_ids:
                raise RuntimeError(f"fault {fault_id} was already attempted")
            if fault_id in batch_fault_ids:
                raise ValueError(
                    f"fault {fault_id} appears more than once in SIGKILL batch"
                )
            if replica_id in self._sigkill_replica_ids:
                raise RuntimeError(
                    f"replica {replica_id} was already targeted by SIGKILL"
                )
            if replica_id in batch_replica_ids:
                raise ValueError(
                    f"replica {replica_id} appears more than once "
                    "in SIGKILL batch"
                )

            record = self.record_for_replica(replica_id)
            self._revalidate_live_group(record)
            validated.append((fault_id, record))
            batch_fault_ids.add(fault_id)
            batch_replica_ids.add(replica_id)

        # The reservation is deliberately all-or-nothing and occurs only
        # after the entire batch, including every live PGID, was validated.
        self._sigkill_fault_ids.update(batch_fault_ids)
        self._sigkill_replica_ids.update(batch_replica_ids)

        requested_ns_by_fault: dict[str, int] = {}
        signalled: list[tuple[str, ProcessRecord, int]] = []
        results_by_fault: dict[str, SigkillBatchResult] = {}
        signal_number = int(signal.SIGKILL)
        batch_interruption: BaseException | None = None
        for index, (fault_id, record) in enumerate(validated):
            try:
                requested_ns = int(self._monotonic_ns())
                requested_ns_by_fault[fault_id] = requested_ns
                self._killpg(record.pgid, signal_number)
            except BaseException as exc:
                if isinstance(exc, ProcessLookupError):
                    error = (
                        f"fault {fault_id}: registered process group "
                        f"{record.pgid} was already absent"
                    )
                else:
                    error = (
                        f"fault {fault_id}: could not SIGKILL registered "
                        f"process group {record.pgid}: "
                        f"{str(exc).strip() or type(exc).__name__}"
                    )
                results_by_fault[fault_id] = SigkillBatchResult(
                    fault_id=fault_id,
                    replica_id=record.replica_id,
                    status="failed",
                    outcome=None,
                    error=error,
                    name=record.name,
                    pid=record.pid,
                    pgid=record.pgid,
                    signal_number=signal_number,
                    requested_monotonic_ns=requested_ns_by_fault.get(fault_id),
                )
                if isinstance(exc, Exception):
                    continue

                batch_interruption = exc
                for pending_fault_id, pending_record in validated[index + 1 :]:
                    results_by_fault[pending_fault_id] = SigkillBatchResult(
                        fault_id=pending_fault_id,
                        replica_id=pending_record.replica_id,
                        status="not_signalled",
                        outcome=None,
                        error=(
                            f"fault {pending_fault_id}: not signalled because "
                            f"fault {fault_id} interrupted batch signalling"
                        ),
                        name=pending_record.name,
                        pid=pending_record.pid,
                        pgid=pending_record.pgid,
                        signal_number=signal_number,
                        requested_monotonic_ns=None,
                    )
                break
            else:
                signalled.append((fault_id, record, requested_ns))

        for index, (fault_id, record, requested_ns) in enumerate(signalled):
            try:
                returncode = self._wait_for_exit(record.process, timeout_s)
            except (subprocess.TimeoutExpired, TimeoutError):
                error = (
                    f"fault {fault_id}: {record.name} did not exit from "
                    f"SIGKILL within {timeout_s}s"
                )
            except BaseException as exc:
                error = (
                    f"fault {fault_id}: could not confirm {record.name} "
                    f"SIGKILL exit: "
                    f"{str(exc).strip() or type(exc).__name__}"
                )
                if not isinstance(exc, Exception):
                    batch_interruption = exc
                    results_by_fault[fault_id] = SigkillBatchResult(
                        fault_id=fault_id,
                        replica_id=record.replica_id,
                        status="failed",
                        outcome=None,
                        error=error,
                        name=record.name,
                        pid=record.pid,
                        pgid=record.pgid,
                        signal_number=signal_number,
                        requested_monotonic_ns=requested_ns,
                    )
                    for (
                        pending_fault_id,
                        pending_record,
                        pending_requested_ns,
                    ) in signalled[index + 1 :]:
                        results_by_fault[pending_fault_id] = (
                            SigkillBatchResult(
                                fault_id=pending_fault_id,
                                replica_id=pending_record.replica_id,
                                status="failed",
                                outcome=None,
                                error=(
                                    f"fault {pending_fault_id}: SIGKILL was "
                                    "sent but exit confirmation was skipped "
                                    f"after fault {fault_id} interrupted "
                                    "batch confirmation"
                                ),
                                name=pending_record.name,
                                pid=pending_record.pid,
                                pgid=pending_record.pgid,
                                signal_number=signal_number,
                                requested_monotonic_ns=(
                                    pending_requested_ns
                                ),
                            )
                        )
                    break
            else:
                if returncode == -signal_number:
                    try:
                        confirmed_ns = int(self._monotonic_ns())
                    except Exception as exc:
                        error = (
                            f"fault {fault_id}: could not timestamp confirmed "
                            f"{record.name} SIGKILL exit: {exc}"
                        )
                    else:
                        outcome = SigkillOutcome(
                            fault_id=fault_id,
                            name=record.name,
                            replica_id=record.replica_id,
                            pid=record.pid,
                            pgid=record.pgid,
                            signal_number=signal_number,
                            returncode=returncode,
                            requested_monotonic_ns=requested_ns,
                            confirmed_monotonic_ns=confirmed_ns,
                        )
                        results_by_fault[fault_id] = SigkillBatchResult(
                            fault_id=fault_id,
                            replica_id=record.replica_id,
                            status="succeeded",
                            outcome=outcome,
                            error=None,
                            name=record.name,
                            pid=record.pid,
                            pgid=record.pgid,
                            signal_number=signal_number,
                            requested_monotonic_ns=requested_ns,
                        )
                        self._confirmed_sigkill_replica_ids.add(
                            record.replica_id
                        )
                        continue
                else:
                    error = (
                        f"fault {fault_id}: {record.name} did not exit from "
                        f"SIGKILL: {returncode}"
                    )
            results_by_fault[fault_id] = SigkillBatchResult(
                fault_id=fault_id,
                name=record.name,
                replica_id=record.replica_id,
                pid=record.pid,
                pgid=record.pgid,
                signal_number=signal_number,
                status="failed",
                outcome=None,
                error=error,
                requested_monotonic_ns=requested_ns,
            )

        results = tuple(
            results_by_fault[fault_id]
            for fault_id, _record in validated
        )
        if any(result.status != "succeeded" for result in results):
            error = SigkillBatchError(results)
            if batch_interruption is not None:
                raise error from batch_interruption
            raise error

        return tuple(
            result.outcome
            for result in results
            if result.outcome is not None
        )

    def _capture_cleanup_escalation(
        self,
        record: ProcessRecord,
    ) -> CleanupEscalationOutcome:
        hook = self._cleanup_escalation_hook
        if hook is None:
            raise RuntimeError("cleanup escalation hook is not configured")
        try:
            result = hook(record)
            if not isinstance(result, CleanupEscalationResult):
                raise TypeError(
                    "cleanup escalation hook returned an invalid result"
                )
            if not result.status:
                raise ValueError("cleanup escalation result status is empty")
        except Exception as exc:
            result = CleanupEscalationResult(
                status="failed",
                artifact_relative_path=None,
                error=str(exc).strip() or type(exc).__name__,
            )
        return CleanupEscalationOutcome(
            name=record.name,
            replica_id=record.replica_id,
            pid=record.pid,
            pgid=record.pgid,
            after_signal_number=int(signal.SIGINT),
            before_signal_number=int(signal.SIGTERM),
            status=result.status,
            artifact_relative_path=result.artifact_relative_path,
            error=result.error,
        )

    def cleanup(self, *, timeout_s: float) -> tuple[CleanupOutcome, ...]:
        """Stop all still-live registered groups, escalating at most once."""

        self._validate_timeout(timeout_s)
        if self._cleanup_started:
            return self._cleanup_outcomes
        self._cleanup_started = True

        outcomes: list[CleanupOutcome] = []
        escalations: list[CleanupEscalationOutcome] = []
        try:
            for signal_number in self._CLEANUP_SIGNALS:
                active = [
                    record
                    for record in self.records
                    if record.process.poll() is None
                ]
                if not active:
                    break

                if (
                    signal_number == int(signal.SIGTERM)
                    and self._cleanup_escalation_hook is not None
                ):
                    for record in active:
                        if record.process.poll() is not None:
                            continue
                        escalations.append(
                            self._capture_cleanup_escalation(record)
                        )
                    self._cleanup_escalations = tuple(escalations)

                signalled: list[ProcessRecord] = []
                for record in active:
                    try:
                        self._revalidate_live_group(record)
                    except RuntimeError:
                        # A process may exit naturally between the active
                        # snapshot and the live identity check.
                        if record.process.poll() is not None:
                            continue
                        raise
                    try:
                        self._killpg(record.pgid, signal_number)
                    except ProcessLookupError:
                        continue
                    signalled.append(record)

                for record in signalled:
                    try:
                        returncode = self._wait_for_exit(
                            record.process,
                            timeout_s,
                        )
                    except (subprocess.TimeoutExpired, TimeoutError):
                        continue
                    outcomes.append(
                        CleanupOutcome(
                            name=record.name,
                            replica_id=record.replica_id,
                            pid=record.pid,
                            pgid=record.pgid,
                            signal_number=signal_number,
                            returncode=returncode,
                        )
                    )

            remaining = [
                record.name
                for record in self.records
                if record.process.poll() is None
            ]
            if remaining:
                raise RuntimeError(
                    "registered process groups remained after cleanup: "
                    + ", ".join(remaining)
                )

            self._cleanup_outcomes = tuple(outcomes)
            self._cleanup_escalations = tuple(escalations)
            return self._cleanup_outcomes
        finally:
            if not self._cleanup_outcomes:
                self._cleanup_outcomes = tuple(outcomes)
            if not self._cleanup_escalations:
                self._cleanup_escalations = tuple(escalations)

    def _validate_group_shape(
        self,
        *,
        name: str,
        pid: int,
        pgid: int,
    ) -> None:
        if pid <= 1 or pgid <= 1:
            raise ValueError(
                f"process {name} PID and PGID must be greater than 1"
            )
        if pgid != pid:
            raise ValueError(
                f"process {name} must lead its own process group"
            )
        if pgid == int(self._get_launcher_pgid()):
            raise ValueError(
                f"process {name} may not share the launcher process group"
            )

    def _revalidate_live_group(self, record: ProcessRecord) -> None:
        if int(record.process.pid) != record.pid:
            raise RuntimeError(
                f"{record.name} process identity changed after registration"
            )
        if record.process.poll() is not None:
            raise RuntimeError(f"{record.name} has already exited")
        try:
            live_pgid = int(self._getpgid(record.pid))
        except ProcessLookupError as exc:
            raise RuntimeError(
                f"{record.name} process group is no longer live"
            ) from exc
        if live_pgid != record.pgid:
            raise RuntimeError(
                f"{record.name} process group changed from "
                f"{record.pgid} to {live_pgid}"
            )
        try:
            self._validate_group_shape(
                name=record.name,
                pid=record.pid,
                pgid=live_pgid,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    @staticmethod
    def _validate_timeout(timeout_s: float) -> None:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and greater than zero")
