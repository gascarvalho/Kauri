"""Focused failure-ownership tests for the future static-resource runner."""

from __future__ import annotations

import signal
import subprocess

import pytest

from experiments.adaptive.kauri_experiment.processes import ProcessRegistry
from experiments.adaptive.kauri_experiment.static_resource_process_owner import (
    OwnedScopeHandle,
    ScopeOperations,
    ScopeSnapshot,
    StaticResourceOwnershipError,
    StaticResourceProcessOwner,
)


class _Process:
    def __init__(self, pid: int, *, exited: bool = False) -> None:
        self.pid = pid
        self.returncode: int | None = 0 if exited else None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


class _Scopes(ScopeOperations):
    def __init__(
        self,
        *,
        inactive: bool = True,
        stop_raises: bool = False,
        race_on_stop: bool = False,
        inactive_with_members: bool = False,
        actions: list[str] | None = None,
    ) -> None:
        self.inactive = inactive
        self.stop_raises = stop_raises
        self.race_on_stop = race_on_stop
        self.inactive_with_members = inactive_with_members
        self.actions = actions
        self.stopped: list[str] = []
        self.checked: list[str] = []
        self.invocation_id = "original-invocation"
        self.control_group = "/user.slice/kauri-local-r0.scope"
        self.member_pids = (101, 102, 103, 104, 105, 106, 107)

    def stop_if_matches(self, handle: OwnedScopeHandle) -> bool:
        if self.actions is not None:
            self.actions.append("scope-stop")
        if self.race_on_stop:
            self.invocation_id = "recreated-during-stop"
            return False
        if (
            self.inactive
            or handle.invocation_id != self.invocation_id
            or handle.control_group != self.control_group
            or handle.pid not in self.member_pids
        ):
            return False
        self.stopped.append(handle.unit)
        self.inactive = True
        if self.stop_raises:
            raise RuntimeError("systemd connection reset after stop")
        return True

    def inspect(self, unit: str) -> ScopeSnapshot:
        self.checked.append(unit)
        return ScopeSnapshot(
            active=not self.inactive,
            invocation_id=self.invocation_id if not self.inactive else None,
            control_group=self.control_group if not self.inactive else None,
            member_pids=(
                self.member_pids
                if not self.inactive or self.inactive_with_members
                else ()
            ),
        )


def _scope_handle(record: object) -> OwnedScopeHandle:
    return OwnedScopeHandle(
        unit="kauri-local-r0.scope",
        pid=getattr(record, "pid"),
        pgid=getattr(record, "pgid"),
        invocation_id="original-invocation",
        control_group="/user.slice/kauri-local-r0.scope",
    )


def _owner(
    process: _Process,
    scopes: _Scopes,
    killed: list[tuple[int, int]],
    actions: list[str] | None = None,
) -> StaticResourceProcessOwner:
    def killpg(pgid: int, signal_number: int) -> None:
        if actions is not None:
            actions.append("pgid-signal")
        killed.append((pgid, signal_number))
        if pgid == process.pid:
            process.returncode = -signal_number

    return StaticResourceProcessOwner(
        scopes=scopes,
        getpgid=lambda _pid: process.pid,
        get_launcher_pgid=lambda: 999,
        killpg=killpg,
    )


def _registry(process: _Process, killed: list[tuple[int, int]]) -> ProcessRegistry:
    return ProcessRegistry(
        getpgid=lambda _pid: process.pid,
        get_launcher_pgid=lambda: 999,
        killpg=lambda pgid, sig: killed.append((pgid, sig)),
    )


def test_quota_verification_failure_before_scope_proof_never_stops_scope() -> None:
    process = _Process(101)
    killed: list[tuple[int, int]] = []
    scopes = _Scopes(inactive=False)
    owner = _owner(process, scopes, killed)

    with pytest.raises(StaticResourceOwnershipError, match="cleanup is unproven"):
        owner.spawn_registered_and_verified(
            registry=_registry(process, killed),
            name="replica-0",
            replica_id=0,
            unit="kauri-local-r0.scope",
            spawn=lambda: process,
            verify_quota=lambda _record: (_ for _ in ()).throw(RuntimeError("quota")),
            timeout_s=0.01,
        )

    assert killed == [(101, int(signal.SIGINT))]
    assert scopes.stopped == []
    assert scopes.checked == ["kauri-local-r0.scope"]


def test_registry_failure_after_spawn_never_stops_unattributed_scope() -> None:
    process = _Process(102)
    killed: list[tuple[int, int]] = []
    scopes = _Scopes()
    owner = _owner(process, scopes, killed)
    registry = _registry(process, killed)
    registry.register(name="already-used", replica_id=0, process=process)

    with pytest.raises(StaticResourceOwnershipError, match="cleanup is unproven") as raised:
        owner.spawn_registered_and_verified(
            registry=registry,
            name="duplicate-replica",
            replica_id=0,
            unit="kauri-local-r0.scope",
            spawn=lambda: process,
            verify_quota=_scope_handle,
            timeout_s=0.01,
        )

    assert killed == [(102, int(signal.SIGINT))]
    assert scopes.stopped == []
    assert raised.value.receipt is not None
    assert raised.value.receipt.scope_stop_attempted is False


def test_process_that_exits_before_registration_is_only_checked_read_only() -> None:
    process = _Process(103, exited=True)
    killed: list[tuple[int, int]] = []
    scopes = _Scopes()
    owner = _owner(process, scopes, killed)

    with pytest.raises(StaticResourceOwnershipError, match="cleanup is unproven"):
        owner.spawn_registered_and_verified(
            registry=_registry(process, killed),
            name="replica-0",
            replica_id=0,
            unit="kauri-local-r0.scope",
            spawn=lambda: process,
            verify_quota=_scope_handle,
            timeout_s=0.01,
        )

    assert killed == []
    assert scopes.stopped == []


def test_spawn_failure_never_stops_a_precomputed_scope_name() -> None:
    process = _Process(104)
    killed: list[tuple[int, int]] = []
    scopes = _Scopes()
    owner = _owner(process, scopes, killed)

    with pytest.raises(StaticResourceOwnershipError, match="cleanup is unproven"):
        owner.spawn_registered_and_verified(
            registry=_registry(process, killed),
            name="replica-0",
            replica_id=0,
            unit="kauri-local-r0.scope",
            spawn=lambda: (_ for _ in ()).throw(RuntimeError("systemd-run failed")),
            verify_quota=_scope_handle,
            timeout_s=0.01,
        )

    assert killed == []
    assert scopes.stopped == []
    assert scopes.checked == ["kauri-local-r0.scope"]


def test_unproven_group_fails_closed_without_any_kill() -> None:
    process = _Process(105)
    killed: list[tuple[int, int]] = []
    scopes = _Scopes()
    owner = StaticResourceProcessOwner(
        scopes=scopes,
        getpgid=lambda _pid: 777,
        get_launcher_pgid=lambda: 999,
        killpg=lambda pgid, sig: killed.append((pgid, sig)),
    )
    registry = ProcessRegistry(
        getpgid=lambda _pid: 105,
        get_launcher_pgid=lambda: 999,
        killpg=lambda _pgid, _sig: None,
    )

    with pytest.raises(StaticResourceOwnershipError, match="cleanup is unproven"):
        owner.spawn_registered_and_verified(
            registry=registry,
            name="replica-0",
            replica_id=0,
            unit="kauri-local-r0.scope",
            spawn=lambda: process,
            verify_quota=lambda _record: (_ for _ in ()).throw(RuntimeError("quota")),
            timeout_s=0.01,
        )

    assert killed == []
    assert scopes.stopped == []


def test_owned_handle_permits_exact_scope_stop_after_downstream_failure() -> None:
    process = _Process(106)
    killed: list[tuple[int, int]] = []
    scopes = _Scopes(inactive=False)
    owner = _owner(process, scopes, killed)
    launch = owner.spawn_registered_and_verified(
        registry=_registry(process, killed),
        name="replica-0",
        replica_id=0,
        unit="kauri-local-r0.scope",
        spawn=lambda: process,
        verify_quota=_scope_handle,
        timeout_s=0.01,
    )

    receipt = owner.cleanup_owned_launch(launch, timeout_s=0.01)

    assert receipt.scope_handle_bound is True
    assert receipt.scope_stop_attempted is True
    assert receipt.scope_inactive is True
    assert receipt.uncertainty is None
    assert killed == [(106, int(signal.SIGINT))]
    assert scopes.stopped == ["kauri-local-r0.scope"]


def test_recreated_scope_is_not_stopped_by_its_old_handle() -> None:
    process = _Process(107)
    killed: list[tuple[int, int]] = []
    scopes = _Scopes(inactive=False)
    owner = _owner(process, scopes, killed)
    launch = owner.spawn_registered_and_verified(
        registry=_registry(process, killed),
        name="replica-0",
        replica_id=0,
        unit="kauri-local-r0.scope",
        spawn=lambda: process,
        verify_quota=_scope_handle,
        timeout_s=0.01,
    )
    scopes.invocation_id = "recreated-invocation"

    receipt = owner.cleanup_owned_launch(launch, timeout_s=0.01)

    assert receipt.scope_stop_attempted is False
    assert "scope identity is gone or differs" in str(receipt.uncertainty)
    assert scopes.stopped == []


def test_scope_recreated_between_inspection_and_stop_is_not_stopped() -> None:
    process = _Process(107)
    killed: list[tuple[int, int]] = []
    scopes = _Scopes(inactive=False, race_on_stop=True)
    owner = _owner(process, scopes, killed)
    launch = owner.spawn_registered_and_verified(
        registry=_registry(process, killed),
        name="replica-0",
        replica_id=0,
        unit="kauri-local-r0.scope",
        spawn=lambda: process,
        verify_quota=_scope_handle,
        timeout_s=0.01,
    )

    receipt = owner.cleanup_owned_launch(launch, timeout_s=0.01)

    assert receipt.scope_stop_attempted is True
    assert "scope identity changed before conditioned stop" in str(receipt.uncertainty)
    assert scopes.stopped == []


def test_stop_attempt_is_recorded_when_systemd_raises_after_effect() -> None:
    process = _Process(107)
    killed: list[tuple[int, int]] = []
    scopes = _Scopes(inactive=False, stop_raises=True)
    owner = _owner(process, scopes, killed)
    launch = owner.spawn_registered_and_verified(
        registry=_registry(process, killed),
        name="replica-0",
        replica_id=0,
        unit="kauri-local-r0.scope",
        spawn=lambda: process,
        verify_quota=_scope_handle,
        timeout_s=0.01,
    )

    receipt = owner.cleanup_owned_launch(launch, timeout_s=0.01)

    assert receipt.scope_stop_attempted is True
    assert receipt.scope_inactive is True
    assert "scope cleanup failed" in str(receipt.uncertainty)
    assert scopes.stopped == ["kauri-local-r0.scope"]


def test_scope_stop_precedes_private_pgid_fallback_for_surviving_leader() -> None:
    process = _Process(107)
    killed: list[tuple[int, int]] = []
    actions: list[str] = []
    scopes = _Scopes(inactive=False, actions=actions)
    owner = _owner(process, scopes, killed, actions)
    launch = owner.spawn_registered_and_verified(
        registry=_registry(process, killed),
        name="replica-0",
        replica_id=0,
        unit="kauri-local-r0.scope",
        spawn=lambda: process,
        verify_quota=_scope_handle,
        timeout_s=0.01,
    )

    owner.cleanup_owned_launch(launch, timeout_s=0.01)

    assert actions == ["scope-stop", "pgid-signal"]


def test_inactive_scope_with_members_is_not_a_complete_cleanup() -> None:
    process = _Process(107)
    killed: list[tuple[int, int]] = []
    scopes = _Scopes(inactive=False, inactive_with_members=True)
    owner = _owner(process, scopes, killed)
    launch = owner.spawn_registered_and_verified(
        registry=_registry(process, killed),
        name="replica-0",
        replica_id=0,
        unit="kauri-local-r0.scope",
        spawn=lambda: process,
        verify_quota=_scope_handle,
        timeout_s=0.01,
    )

    receipt = owner.cleanup_owned_launch(launch, timeout_s=0.01)

    assert receipt.scope_inactive is False
    assert "did not become inactive" in str(receipt.uncertainty)


def test_group_identity_is_revalidated_before_each_escalation() -> None:
    process = _Process(107)
    killed: list[tuple[int, int]] = []
    scopes = _Scopes()
    pgids = iter((107, 777))
    owner = StaticResourceProcessOwner(
        scopes=scopes,
        getpgid=lambda _pid: next(pgids),
        get_launcher_pgid=lambda: 999,
        killpg=lambda pgid, sig: killed.append((pgid, sig)),
    )
    registry = ProcessRegistry(
        getpgid=lambda _pid: 107,
        get_launcher_pgid=lambda: 999,
        killpg=lambda _pgid, _sig: None,
    )

    with pytest.raises(StaticResourceOwnershipError, match="cleanup is unproven"):
        owner.spawn_registered_and_verified(
            registry=registry,
            name="replica-0",
            replica_id=0,
            unit="kauri-local-r0.scope",
            spawn=lambda: process,
            verify_quota=lambda _record: (_ for _ in ()).throw(RuntimeError("quota")),
            timeout_s=0.01,
        )

    assert killed == [(107, int(signal.SIGINT))]
    assert scopes.stopped == []
