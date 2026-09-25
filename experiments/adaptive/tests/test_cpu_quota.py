"""Contracts for the external per-replica CPU-quota launcher."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from experiments.adaptive.kauri_experiment import cpu_quota
from experiments.adaptive.kauri_experiment import focused_crash_pair_runtime

PROFILE_ROOT = Path(__file__).parents[1] / "profiles"
CONTRACT_PATH = PROFILE_ROOT / "n31-cpu-quota-heterogeneity-smoke-v1.json"
BASE_PROFILE_PATH = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v13.json"


def _contract() -> cpu_quota.CpuQuotaContract:
    return cpu_quota.load_cpu_quota_contract(
        CONTRACT_PATH,
        base_profile_path=BASE_PROFILE_PATH,
        expected_replica_ids=tuple(range(31)),
    )


def test_checked_in_contract_binds_profile_and_epoch1_capacity_layout() -> None:
    contract = _contract()

    assert contract.figure_eligible is False
    assert contract.manager_visibility == "none"
    assert contract.base_profile_sha256 != contract.base_profile_canonical_sha256
    assert contract.quota_percent(0) == 50
    assert contract.quota_percent(5) == 50
    assert contract.quota_percent(6) == 100
    assert contract.quota_percent(23) == 100
    assert contract.quota_percent(24) == 200
    assert contract.quota_percent(30) == 200
    assert contract.capacity_class(0) == "slow"
    assert contract.capacity_class(24) == "fast"
    assert contract.replica_ids == tuple(range(31))
    assert (
        len({assignment.cpu_quota_percent for assignment in contract.assignments}) == 3
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update(enabled=False),
        lambda value: value.update(figure_eligible=True),
        lambda value: value.update(manager_visibility="labels"),
        lambda value: value["assignments"].pop(),
        lambda value: value["assignments"].__setitem__(
            1, {**value["assignments"][1], "replica_id": 0}
        ),
        lambda value: value["assignments"][0].update(cpu_quota_percent=0),
        lambda value: value["assignments"][0].update(capacity_class="medium"),
    ),
)
def test_contract_rejects_noncanonical_or_unblinded_input(
    tmp_path: Path, mutate: object
) -> None:
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    mutate(value)
    candidate = tmp_path / "contract.json"
    candidate.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(cpu_quota.CpuQuotaContractError):
        cpu_quota.load_cpu_quota_contract(
            candidate,
            base_profile_path=BASE_PROFILE_PATH,
            expected_replica_ids=tuple(range(31)),
        )


def test_scope_command_is_shell_free_and_binds_exact_quota() -> None:
    contract = _contract()
    command, unit = cpu_quota.systemd_scope_command(
        contract,
        run_id="20260924T090000Z-1234-deadbeef",
        replica_id=24,
        command=("/opt/kauri/hotstuff-app", "--conf", "/tmp/replica-24.conf"),
    )

    assert unit == "kauri-20260924t090000z-1234-deadbeef-r24.scope"
    assert command == (
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        "--property=CPUAccounting=yes",
        "--property=CPUQuota=200%",
        "--",
        "/opt/kauri/hotstuff-app",
        "--conf",
        "/tmp/replica-24.conf",
    )
    assert not any("sh -c" in argument for argument in command)


def test_systemd_and_cgroup_parsers_are_exact() -> None:
    properties = cpu_quota.parse_systemctl_show(
        "ActiveState=active\nSubState=running\n"
        "CPUQuotaPerSecUSec=500ms\nControlGroup=/user.slice/demo.scope\n"
    )
    stats = cpu_quota.parse_cpu_stat(
        "usage_usec 1234\nuser_usec 1000\nsystem_usec 234\n"
        "nr_periods 9\nnr_throttled 4\nthrottled_usec 321\n"
    )

    assert cpu_quota.quota_per_second_usec(properties) == 500_000
    assert stats == {
        "usage_usec": 1234,
        "user_usec": 1000,
        "system_usec": 234,
        "nr_periods": 9,
        "nr_throttled": 4,
        "throttled_usec": 321,
    }
    assert cpu_quota.parse_cpu_stat(
        "usage_usec 1234\nuser_usec 1000\nsystem_usec 234\n"
    ) == {
        "usage_usec": 1234,
        "user_usec": 1000,
        "system_usec": 234,
    }
    assert cpu_quota.parse_cpu_max("50000 100000\n") == 500_000
    assert cpu_quota.parse_cpu_max("200000 100000\n") == 2_000_000
    with pytest.raises(cpu_quota.CpuQuotaContractError, match="finite quota"):
        cpu_quota.parse_cpu_max("max 100000\n")
    with pytest.raises(
        cpu_quota.CpuQuotaContractError,
        match="partial throttle accounting",
    ):
        cpu_quota.parse_cpu_stat(
            "usage_usec 1234\nuser_usec 1000\nsystem_usec 234\nnr_periods 9\n"
        )
    collected = cpu_quota.parse_systemctl_show(
        "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
        "CPUQuotaPerSecUSec=0us\nControlGroup=\n"
    )
    assert collected["LoadState"] == "not-found"


def test_runtime_wraps_only_replicas_and_verifies_cleanup(tmp_path: Path) -> None:
    contract = _contract()
    spawned: list[tuple[str, int, tuple[str, ...]]] = []
    unit_states: dict[str, str] = {}

    def base_spawn(_registry: object, **kwargs: object) -> tuple[object, object]:
        command = tuple(kwargs["command"])
        spawned.append((str(kwargs["name"]), int(kwargs["replica_id"]), command))
        return (
            SimpleNamespace(
                name=kwargs["name"],
                replica_id=kwargs["replica_id"],
                pid=100,
                pgid=100,
            ),
            object(),
        )

    def show_unit(unit: str) -> str:
        return unit_states.get(
            unit,
            "ActiveState=active\nSubState=running\n"
            f"CPUQuotaPerSecUSec={contract.quota_percent(int(unit.rsplit('r', 1)[1].split('.', 1)[0])) * 10}ms\n"
            f"ControlGroup=/user.slice/{unit}\n",
        )

    runtime = cpu_quota.CpuQuotaRuntime(
        contract,
        run_id="run-abc",
        run_directory=tmp_path,
        base_spawn=base_spawn,
        show_unit=show_unit,
        read_cpu_stat=lambda _path: {
            "usage_usec": 1,
            "user_usec": 1,
            "system_usec": 0,
            "nr_periods": 1,
            "nr_throttled": 0,
            "throttled_usec": 0,
        },
        read_cgroup_procs=lambda _path: (100,),
        process_group=lambda _pid: 100,
    )
    registry = object()
    runtime.spawn_owned_process(
        registry,
        name="adaptive-manager",
        replica_id=-1,
        command=("adaptation-manager", "--listen", "127.0.0.1:1"),
        log_path=tmp_path / "manager.log",
        working_directory=tmp_path,
    )
    runtime.spawn_owned_process(
        registry,
        name="replica-0",
        replica_id=0,
        command=("hotstuff-app", "--conf", "replica-0.conf"),
        log_path=tmp_path / "replica.log",
        working_directory=tmp_path,
    )

    assert spawned[0][2] == ("adaptation-manager", "--listen", "127.0.0.1:1")
    assert spawned[1][2][0:5] == (
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
    )
    receipt = json.loads((tmp_path / "runtime/cpu-quota-launch.json").read_text())
    assert receipt["contract_sha256"] == contract.contract_sha256
    assert receipt["replicas"][0]["cpu_quota_percent"] == 50
    assert "capacity_class" not in receipt["replicas"][0]

    unit = receipt["replicas"][0]["unit"]
    unit_states[unit] = (
        "ActiveState=inactive\nSubState=dead\n"
        "CPUQuotaPerSecUSec=500ms\nControlGroup=\n"
    )
    cleanup = runtime.verify_cleanup()
    assert cleanup["complete"] is True
    assert cleanup["units"][0]["active_state"] == "inactive"


def test_runtime_rejects_scope_that_does_not_own_launched_process(
    tmp_path: Path,
) -> None:
    base = _contract()
    contract = replace(base, assignments=(base.assignments[0],))

    def spawn(_registry: object, **_kwargs: object) -> tuple[object, object]:
        return SimpleNamespace(pid=100, pgid=100), object()

    runtime = cpu_quota.CpuQuotaRuntime(
        contract,
        run_id="ownership-mismatch",
        run_directory=tmp_path,
        base_spawn=spawn,
        show_unit=lambda unit: (
            "ActiveState=active\nSubState=running\n"
            "CPUQuotaPerSecUSec=500ms\n"
            f"ControlGroup=/user.slice/{unit}\n"
        ),
        read_cgroup_procs=lambda _path: (101,),
        process_group=lambda _pid: 101,
    )

    with pytest.raises(
        cpu_quota.CpuQuotaContractError,
        match="does not own the launched process group",
    ):
        runtime.spawn_owned_process(
            object(),
            name="replica-0",
            replica_id=0,
            command=("hotstuff-app", "--idx", "0"),
            log_path=tmp_path / "replica.log",
            working_directory=tmp_path,
        )


def test_sampling_reads_cgroup_files_without_polling_systemd(tmp_path: Path) -> None:
    base = _contract()
    contract = replace(base, assignments=(base.assignments[0],))
    show_calls = 0

    def spawn(_registry: object, **_kwargs: object) -> tuple[object, object]:
        return SimpleNamespace(pid=100, pgid=100), object()

    def show_unit(unit: str) -> str:
        nonlocal show_calls
        show_calls += 1
        return (
            "ActiveState=active\nSubState=running\n"
            "CPUQuotaPerSecUSec=500ms\n"
            f"ControlGroup=/user.slice/{unit}\n"
        )

    runtime = cpu_quota.CpuQuotaRuntime(
        contract,
        run_id="direct-cgroup-sampling",
        run_directory=tmp_path,
        base_spawn=spawn,
        show_unit=show_unit,
        read_cpu_max=lambda _path: 500_000,
        read_cpu_stat=lambda _path: {
            "usage_usec": 1,
            "user_usec": 1,
            "system_usec": 0,
        },
        read_cgroup_procs=lambda _path: (100,),
        process_group=lambda _pid: 100,
        monotonic_ns=lambda: 123,
    )
    runtime.spawn_owned_process(
        object(),
        name="replica-0",
        replica_id=0,
        command=("hotstuff-app", "--idx", "0"),
        log_path=tmp_path / "replica.log",
        working_directory=tmp_path,
    )
    assert show_calls == 1

    rows = runtime.sample_once()

    assert show_calls == 1
    assert rows == [
        {
            "schema_version": 1,
            "source_monotonic_ns": 123,
            "replica_id": 0,
            "cpu_quota_percent": 50,
            "unit": "kauri-direct-cgroup-sampling-r0.scope",
            "control_group": "/user.slice/kauri-direct-cgroup-sampling-r0.scope",
            "cpu_stat_path": "/sys/fs/cgroup/user.slice/kauri-direct-cgroup-sampling-r0.scope/cpu.stat",
            "cpu_quota_per_second_usec": 500_000,
            "active_state": "active",
            "sub_state": "running",
            "cpu_stat": {
                "usage_usec": 1,
                "user_usec": 1,
                "system_usec": 0,
            },
        }
    ]

    runtime._read_cpu_max = lambda _path: 250_000
    with pytest.raises(
        cpu_quota.CpuQuotaContractError,
        match="no longer matches",
    ):
        runtime.sample_once()
    assert show_calls == 1


def test_default_quota_samples_share_the_fault_evidence_raw_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _contract()
    contract = replace(base, assignments=(base.assignments[0],))
    raw_timestamp = 1_234_567_890_000

    def spawn(_registry: object, **_kwargs: object) -> tuple[object, object]:
        return SimpleNamespace(pid=100, pgid=100), object()

    runtime = cpu_quota.CpuQuotaRuntime(
        contract,
        run_id="raw-clock-sampling",
        run_directory=tmp_path,
        base_spawn=spawn,
        show_unit=lambda unit: (
            "ActiveState=active\nSubState=running\n"
            "CPUQuotaPerSecUSec=500ms\n"
            f"ControlGroup=/user.slice/{unit}\n"
        ),
        read_cpu_max=lambda _path: 500_000,
        read_cpu_stat=lambda _path: {
            "usage_usec": 1,
            "user_usec": 1,
            "system_usec": 0,
        },
        read_cgroup_procs=lambda _path: (100,),
        process_group=lambda _pid: 100,
    )
    runtime.spawn_owned_process(
        object(),
        name="replica-0",
        replica_id=0,
        command=("hotstuff-app", "--idx", "0"),
        log_path=tmp_path / "replica.log",
        working_directory=tmp_path,
    )
    native_clock = time.clock_gettime_ns

    def clock_gettime_ns(clock_id: int) -> int:
        if clock_id == time.CLOCK_MONOTONIC_RAW:
            return raw_timestamp
        return native_clock(clock_id)

    with monkeypatch.context() as patch:
        patch.setattr(time, "clock_gettime_ns", clock_gettime_ns)
        rows = runtime.sample_once()

    assert rows[0]["source_monotonic_ns"] == raw_timestamp


def test_sampling_uses_systemd_only_after_cgroup_disappears(tmp_path: Path) -> None:
    base = _contract()
    contract = replace(base, assignments=(base.assignments[0],))
    active = True
    show_calls = 0

    def spawn(_registry: object, **_kwargs: object) -> tuple[object, object]:
        return SimpleNamespace(pid=100, pgid=100), object()

    def show_unit(unit: str) -> str:
        nonlocal show_calls
        show_calls += 1
        if active:
            return (
                "ActiveState=active\nSubState=running\n"
                "CPUQuotaPerSecUSec=500ms\n"
                f"ControlGroup=/user.slice/{unit}\n"
            )
        return (
            "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
            "CPUQuotaPerSecUSec=0us\nControlGroup=\n"
        )

    def missing(_path: Path) -> int:
        raise cpu_quota._CgroupUnavailableError("cgroup disappeared")

    runtime = cpu_quota.CpuQuotaRuntime(
        contract,
        run_id="cgroup-disappeared",
        run_directory=tmp_path,
        base_spawn=spawn,
        show_unit=show_unit,
        read_cpu_max=missing,
        read_cgroup_procs=lambda _path: (100,),
        process_group=lambda _pid: 100,
        monotonic_ns=lambda: 456,
    )
    runtime.spawn_owned_process(
        object(),
        name="replica-0",
        replica_id=0,
        command=("hotstuff-app", "--idx", "0"),
        log_path=tmp_path / "replica.log",
        working_directory=tmp_path,
    )
    active = False

    rows = runtime.sample_once()

    assert rows[0]["active_state"] == "inactive"
    assert rows[0]["sub_state"] == "dead"
    assert rows[0]["cpu_quota_per_second_usec"] == 0
    assert "cpu_stat" not in rows[0]

    repeated = runtime.sample_once()

    assert repeated[0]["active_state"] == "inactive"
    assert repeated[0]["sub_state"] == "dead"
    assert "cpu_stat" not in repeated[0]
    assert show_calls == 2


def test_monitor_waits_only_until_the_next_fixed_deadline(tmp_path: Path) -> None:
    contract = _contract()

    class Clock:
        now = 0

        def read(self) -> int:
            return self.now

    clock = Clock()
    waits: list[float] = []

    class Stop:
        def is_set(self) -> bool:
            return len(waits) >= 2

        def set(self) -> None:
            return None

        def wait(self, seconds: float) -> bool:
            waits.append(seconds)
            clock.now += int(seconds * 1_000_000_000)
            return len(waits) >= 2

    runtime = cpu_quota.CpuQuotaRuntime(
        contract,
        run_id="fixed-deadline",
        run_directory=tmp_path,
        monotonic_ns=clock.read,
    )

    def sample_once() -> list[dict[str, object]]:
        timestamp = clock.now
        clock.now += 250_000_000
        return [{"source_monotonic_ns": timestamp}]

    runtime.sample_once = sample_once  # type: ignore[method-assign]
    runtime._monitor_stop = Stop()  # type: ignore[assignment]

    runtime._monitor_loop()

    assert waits == [0.75, 0.75]
    rounds = [
        json.loads(line)
        for line in (tmp_path / "raw/cpu-quota-monitor-rounds.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert [row["duration_ns"] for row in rounds] == [250_000_000, 250_000_000]
    assert [row["completion_overrun_ns"] for row in rounds] == [0, 0]


def test_monitor_failure_preserves_cause_after_verified_cleanup(
    tmp_path: Path,
) -> None:
    base = _contract()
    contract = replace(base, assignments=(base.assignments[0],))
    sampled = threading.Event()
    unit_states: dict[str, str] = {}

    def spawn(_registry: object, **_kwargs: object) -> tuple[object, object]:
        return SimpleNamespace(pid=100, pgid=100), object()

    def show_unit(unit: str) -> str:
        return unit_states.get(
            unit,
            "ActiveState=active\nSubState=running\n"
            "CPUQuotaPerSecUSec=500ms\n"
            f"ControlGroup=/user.slice/{unit}\n",
        )

    def fail_sample(_path: Path) -> Mapping[str, int]:
        sampled.set()
        raise cpu_quota.CpuQuotaContractError(
            "cgroup cpu.stat lacks required accounting fields"
        )

    runtime = cpu_quota.CpuQuotaRuntime(
        contract,
        run_id="monitor-failure",
        run_directory=tmp_path,
        base_spawn=spawn,
        show_unit=show_unit,
        read_cpu_max=lambda _path: 500_000,
        read_cpu_stat=fail_sample,
        read_cgroup_procs=lambda _path: (100,),
        process_group=lambda _pid: 100,
    )
    runtime.spawn_owned_process(
        object(),
        name="replica-0",
        replica_id=0,
        command=("hotstuff-app", "--idx", "0"),
        log_path=tmp_path / "replica.log",
        working_directory=tmp_path,
    )
    unit = json.loads(
        (tmp_path / "runtime/cpu-quota-launch.json").read_text(encoding="utf-8")
    )["replicas"][0]["unit"]
    runtime.start_monitor()
    assert sampled.wait(timeout=1.0)
    unit_states[unit] = (
        "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
        "CPUQuotaPerSecUSec=0us\nControlGroup=\n"
    )

    with pytest.raises(
        cpu_quota.CpuQuotaCleanupError,
        match="cgroup cpu.stat lacks required accounting fields",
    ) as raised:
        runtime.verify_cleanup()

    cleanup = raised.value.cleanup
    assert cleanup["complete"] is True
    assert cleanup["monitor"]["status"] == "FAILED"
    assert (
        cleanup["monitor"]["reason"]
        == "cgroup cpu.stat lacks required accounting fields"
    )
    assert cleanup == json.loads(
        (tmp_path / "runtime/cpu-quota-cleanup.json").read_text(encoding="utf-8")
    )


def test_backend_projects_quota_failure_with_complete_process_cleanup() -> None:
    quota_cleanup = {
        "schema_version": 1,
        "complete": True,
        "monitor": {"status": "FAILED", "stopped": True, "reason": "sample failed"},
        "units": [],
    }

    class Registry:
        def cleanup(self, *, timeout_s: float) -> tuple[object, ...]:
            assert timeout_s == 2.0
            return ()

    class Process:
        def poll(self) -> int:
            return 0

    failure = cpu_quota.CpuQuotaCleanupError(
        "CPU-quota monitor failed: sample failed", cleanup=quota_cleanup
    )

    class QuotaRuntime:
        def stop_monitor(self) -> tuple[bool, None]:
            return True, None

        def verify_cleanup(self) -> Mapping[str, object]:
            raise failure

    processes = SimpleNamespace(
        registry=Registry(),
        logs=(),
        evidence=None,
        cpu_quota_runtime=QuotaRuntime(),
        records=(SimpleNamespace(process=Process()),),
    )

    with pytest.raises(cpu_quota.CpuQuotaCleanupError) as raised:
        focused_crash_pair_runtime.FocusedLaunchBackend().cleanup({}, processes)

    assert raised.value is failure
    assert raised.value.cleanup == {
        "complete": True,
        "outcomes": [],
        "cpu_quota": quota_cleanup,
    }


def test_contract_digest_changes_when_assignment_changes() -> None:
    contract = _contract()
    assignments = list(contract.assignments)
    assignments[0] = replace(assignments[0], cpu_quota_percent=51)
    changed = replace(contract, assignments=tuple(assignments), contract_sha256="")

    assert cpu_quota.contract_digest(changed) != cpu_quota.contract_digest(contract)


def test_secondary_authorization_binds_contract_environment_and_base_run(
    tmp_path: Path,
) -> None:
    contract = _contract()
    output = tmp_path / "results"
    base = {
        "schema_version": 1,
        "mode": "pair",
        "pair_count": 1,
        "profile_sha256": contract.base_profile_canonical_sha256,
        "output_root": str(output.resolve()),
        "automatic_retries": 0,
        "replacement_policy": "none",
    }
    base_request = cpu_quota._canonical(base)
    environment = {
        "schema_version": 1,
        "kind": "kauri-cpu-quota-environment-v1",
        "verified": True,
        "kernel": "test-kernel",
        "cgroup_version": 2,
        "controllers": ["cpu", "memory"],
        "systemctl_path": "/usr/bin/systemctl",
        "systemd_run_path": "/usr/bin/systemd-run",
        "probe_quota_percent": 25,
        "probe_exit_code": 0,
    }
    request = cpu_quota.build_authorization_request(
        contract,
        base_authorization_request=base_request,
        environment=environment,
        output_root=output,
    )
    request_document = json.loads(request)
    assert (
        request_document["base_profile_canonical_sha256"]
        == contract.base_profile_canonical_sha256
    )
    assert "base_profile_sha256" not in request_document
    receipt = {
        **request_document,
        "request_sha256": cpu_quota._sha256(request),
        "approval_reference": "thesis-author-approved-excluded-smoke",
        "approved_utc": "2026-09-22T20:00:00+00:00",
    }

    assert (
        cpu_quota.verify_authorization_receipt(request, receipt)["figure_eligible"]
        is False
    )
    changed = {**receipt, "contract_sha256": "0" * 64}
    with pytest.raises(cpu_quota.CpuQuotaContractError):
        cpu_quota.verify_authorization_receipt(request, changed)
    with pytest.raises(cpu_quota.CpuQuotaContractError):
        cpu_quota.build_authorization_request(
            contract,
            base_authorization_request=base_request,
            environment={"verified": True},
            output_root=output,
        )


def test_linux_environment_probe_is_shell_free_and_requires_cgroup_v2(
    tmp_path: Path,
) -> None:
    controllers = tmp_path / "cgroup.controllers"
    controllers.write_text("memory cpu io\n", encoding="ascii")
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="running\n", stderr="")

    result = cpu_quota.verify_linux_environment(
        cgroup_root=tmp_path,
        system_name=lambda: "Linux",
        find_executable=lambda name: f"/usr/bin/{name}",
        run_command=run,
    )

    assert result["verified"] is True
    assert calls[0] == ("/usr/bin/systemctl", "--user", "is-system-running")
    assert calls[1][0:5] == (
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
    )
    assert calls[1][-1] == "/usr/bin/true"
    assert not any(argument in {"sh", "bash", "-c"} for argument in calls[1])


def test_focused_backend_preserves_normal_path_and_wraps_only_opted_in_replicas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    for name in ("logs", "raw", "runtime"):
        (tmp_path / name).mkdir()
    active = True
    spawned: list[tuple[str, int, tuple[str, ...]]] = []

    class Process:
        def poll(self) -> int:
            return 0

    class Registry:
        def cleanup(self, *, timeout_s: float) -> tuple[object, ...]:
            nonlocal active
            assert timeout_s == 2.0
            active = False
            return ()

    class Evidence:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    class Log:
        def close(self) -> None:
            return None

    def spawn(_registry: object, **kwargs: object) -> tuple[object, object]:
        command = tuple(kwargs["command"])
        spawned.append((str(kwargs["name"]), int(kwargs["replica_id"]), command))
        return (
            SimpleNamespace(
                name=kwargs["name"],
                replica_id=kwargs["replica_id"],
                pid=100,
                pgid=100,
                process=Process(),
            ),
            Log(),
        )

    def show_unit(unit: str) -> str:
        replica = int(unit.rsplit("r", 1)[1].split(".", 1)[0])
        quota_ms = contract.quota_percent(replica) * 10
        return (
            f"ActiveState={'active' if active else 'inactive'}\n"
            f"SubState={'running' if active else 'dead'}\n"
            f"CPUQuotaPerSecUSec={quota_ms}ms\n"
            f"ControlGroup={'/user.slice/' + unit if active else ''}\n"
        )

    def runtime_factory(
        selected: cpu_quota.CpuQuotaContract, **kwargs: object
    ) -> cpu_quota.CpuQuotaRuntime:
        return cpu_quota.CpuQuotaRuntime(
            selected,
            **kwargs,
            show_unit=show_unit,
            read_cpu_max=lambda path: contract.quota_percent(
                int(str(path).rsplit("r", 1)[1].split(".", 1)[0])
            )
            * 10_000,
            read_cpu_stat=lambda _path: {
                "usage_usec": 1,
                "user_usec": 1,
                "system_usec": 0,
                "nr_periods": 1,
                "nr_throttled": 0,
                "throttled_usec": 0,
            },
            read_cgroup_procs=lambda _path: (100,),
            process_group=lambda _pid: 100,
        )

    monkeypatch.setattr(
        focused_crash_pair_runtime, "ProcessRegistry", lambda **_kwargs: Registry()
    )
    monkeypatch.setattr(
        focused_crash_pair_runtime,
        "FaultEvidence",
        lambda *_args, **_kwargs: Evidence(),
    )
    manager = ("adaptation-manager", "--listen", "127.0.0.1:1")
    monkeypatch.setattr(
        focused_crash_pair_runtime, "_capture_process_argv", lambda _record: manager
    )
    monkeypatch.setattr(
        focused_crash_pair_runtime,
        "_validate_manager_launch_boundary",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        focused_crash_pair_runtime.profiled_fault_runtime,
        "normalized_manager_argv",
        lambda command: list(command),
    )
    backend = focused_crash_pair_runtime.FocusedLaunchBackend(
        spawn=spawn,
        cpu_quota_runtime_factory=runtime_factory,
    )
    processes = backend.spawn_processes(
        {
            "run_directory": tmp_path,
            "run_id": "quota-smoke",
            "profile": SimpleNamespace(
                replica_ids=tuple(range(31)), target_replica_ids=(21, 22, 23)
            ),
            "fault_plan": object(),
            "manager_command": manager,
            "replica_commands": tuple(
                ("hotstuff-app", "--idx", str(replica)) for replica in range(31)
            ),
            "client_command": ("hotstuff-client", "--iter", "-1"),
            "cpu_quota_contract": contract,
        }
    )
    cleanup = backend.cleanup({}, processes)

    assert spawned[0][2] == manager
    assert spawned[-1][2] == ("hotstuff-client", "--iter", "-1")
    assert all(row[2][0] == "systemd-run" for row in spawned[1:-1])
    assert "--property=CPUQuota=50%" in spawned[1][2]
    assert "--property=CPUQuota=200%" in spawned[25][2]
    assert cleanup["complete"] is True
    assert cleanup["cpu_quota"]["complete"] is True
