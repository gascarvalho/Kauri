"""External, manager-blind CPU quotas for figure-ineligible Linux experiments."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import threading
import time
from typing import Any

_CONTRACT_KEYS = frozenset(
    {
        "schema_version",
        "contract_id",
        "enabled",
        "figure_eligible",
        "launcher",
        "manager_visibility",
        "sampling_interval_ms",
        "base_profile_id",
        "base_profile_sha256",
        "base_profile_canonical_sha256",
        "assignments",
    }
)
_ASSIGNMENT_KEYS = frozenset({"replica_id", "capacity_class", "cpu_quota_percent"})
_LAUNCHER = "systemd-user-scope-cpu-quota-v1"
_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,63}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CGROUP_STAT_KEYS = (
    "usage_usec",
    "user_usec",
    "system_usec",
    "nr_periods",
    "nr_throttled",
    "throttled_usec",
)
_AUTHORIZATION_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "mode",
        "base_authorization_request_sha256",
        "base_profile_canonical_sha256",
        "contract_id",
        "contract_sha256",
        "contract_semantic_sha256",
        "environment_sha256",
        "output_root",
        "automatic_retries",
        "replacement_policy",
        "figure_eligible",
        "authorization_nonce",
    }
)


class CpuQuotaContractError(RuntimeError):
    """The external resource contract cannot be trusted or applied exactly."""


@dataclass(frozen=True, slots=True)
class CpuQuotaAssignment:
    replica_id: int
    capacity_class: str
    cpu_quota_percent: int


@dataclass(frozen=True, slots=True)
class CpuQuotaContract:
    schema_version: int
    contract_id: str
    enabled: bool
    figure_eligible: bool
    launcher: str
    manager_visibility: str
    sampling_interval_ms: int
    base_profile_id: str
    base_profile_sha256: str
    base_profile_canonical_sha256: str
    assignments: tuple[CpuQuotaAssignment, ...]
    contract_sha256: str

    @property
    def replica_ids(self) -> tuple[int, ...]:
        return tuple(assignment.replica_id for assignment in self.assignments)

    def assignment(self, replica_id: int) -> CpuQuotaAssignment:
        try:
            return self.assignments[self.replica_ids.index(replica_id)]
        except ValueError as exc:
            raise CpuQuotaContractError(
                f"replica {replica_id} is absent from the CPU-quota contract"
            ) from exc

    def quota_percent(self, replica_id: int) -> int:
        return self.assignment(replica_id).cpu_quota_percent

    def capacity_class(self, replica_id: int) -> str:
        return self.assignment(replica_id).capacity_class


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


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _contract_document(contract: CpuQuotaContract) -> dict[str, object]:
    return {
        "schema_version": contract.schema_version,
        "contract_id": contract.contract_id,
        "enabled": contract.enabled,
        "figure_eligible": contract.figure_eligible,
        "launcher": contract.launcher,
        "manager_visibility": contract.manager_visibility,
        "sampling_interval_ms": contract.sampling_interval_ms,
        "base_profile_id": contract.base_profile_id,
        "base_profile_sha256": contract.base_profile_sha256,
        "base_profile_canonical_sha256": contract.base_profile_canonical_sha256,
        "assignments": [
            {
                "replica_id": assignment.replica_id,
                "capacity_class": assignment.capacity_class,
                "cpu_quota_percent": assignment.cpu_quota_percent,
            }
            for assignment in contract.assignments
        ],
    }


def contract_digest(contract: CpuQuotaContract) -> str:
    """Return the semantic digest used by tests and generated receipts."""

    return _sha256(_canonical(_contract_document(contract)))


def build_authorization_request(
    contract: CpuQuotaContract,
    *,
    base_authorization_request: bytes,
    environment: Mapping[str, object],
    output_root: Path,
) -> bytes:
    """Bind the excluded smoke to one base run, host probe, and result root."""

    try:
        base = json.loads(base_authorization_request)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CpuQuotaContractError(
            "base authorization request is invalid JSON"
        ) from exc
    if (
        not isinstance(base, dict)
        or _canonical(base) != base_authorization_request
        or base.get("mode") != "pair"
        or base.get("pair_count") != 1
        or base.get("profile_sha256") != contract.base_profile_canonical_sha256
        or base.get("output_root") != str(Path(output_root).resolve())
        or base.get("automatic_retries") != 0
        or base.get("replacement_policy") != "none"
    ):
        raise CpuQuotaContractError(
            "base authorization does not bind the excluded N=31 smoke"
        )
    environment_document = dict(environment)
    if environment_document.get("verified") is not True:
        raise CpuQuotaContractError("CPU-quota environment is not verified")
    base_sha = _sha256(base_authorization_request)
    environment_sha = _sha256(_canonical(environment_document))
    nonce = _sha256(
        (
            f"heterogeneity-smoke:{base_sha}:{contract.contract_sha256}:"
            f"{environment_sha}:{Path(output_root).resolve()}"
        ).encode("utf-8")
    )
    request = {
        "schema_version": 1,
        "kind": "kauri-cpu-quota-heterogeneity-authorization-v1",
        "mode": "heterogeneity-smoke",
        "base_authorization_request_sha256": base_sha,
        "base_profile_canonical_sha256": contract.base_profile_canonical_sha256,
        "contract_id": contract.contract_id,
        "contract_sha256": contract.contract_sha256,
        "contract_semantic_sha256": contract_digest(contract),
        "environment_sha256": environment_sha,
        "output_root": str(Path(output_root).resolve()),
        "automatic_retries": 0,
        "replacement_policy": "none",
        "figure_eligible": False,
        "authorization_nonce": nonce,
    }
    return _canonical(request)


def verify_authorization_receipt(
    request: bytes, receipt: Mapping[str, object]
) -> dict[str, object]:
    """Verify exact human approval for one figure-ineligible quota smoke."""

    try:
        expected = json.loads(request)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CpuQuotaContractError(
            "CPU-quota authorization request is invalid"
        ) from exc
    if (
        not isinstance(expected, dict)
        or set(expected) != _AUTHORIZATION_REQUEST_KEYS
        or _canonical(expected) != request
    ):
        raise CpuQuotaContractError("CPU-quota authorization request schema drifted")
    document = dict(receipt)
    receipt_keys = {
        *_AUTHORIZATION_REQUEST_KEYS,
        "request_sha256",
        "approval_reference",
        "approved_utc",
    }
    if set(document) != receipt_keys or any(
        document.get(key) != expected[key] for key in _AUTHORIZATION_REQUEST_KEYS
    ):
        raise CpuQuotaContractError("CPU-quota authorization receipt drifted")
    if document.get("request_sha256") != _sha256(request):
        raise CpuQuotaContractError("CPU-quota authorization digest drifted")
    if (
        not isinstance(document.get("approval_reference"), str)
        or not document["approval_reference"]
    ):
        raise CpuQuotaContractError("CPU-quota approval reference is absent")
    if (
        not isinstance(document.get("approved_utc"), str)
        or not document["approved_utc"]
    ):
        raise CpuQuotaContractError("CPU-quota approval time is absent")
    return document


def _regular_bytes(path: Path, label: str) -> bytes:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise CpuQuotaContractError(f"{label} must be a regular non-symlink file")
    try:
        return candidate.read_bytes()
    except OSError as exc:
        raise CpuQuotaContractError(f"cannot read {label}") from exc


def _profile_canonical_sha256(profile: Mapping[str, object]) -> str:
    identity = json.loads(json.dumps(dict(profile)))
    topology = identity.get("topology")
    if not isinstance(topology, dict):
        raise CpuQuotaContractError("base profile topology is invalid")
    topology.pop("proof_sha256", None)
    return _sha256(_canonical(identity))


def load_cpu_quota_contract(
    path: Path,
    *,
    base_profile_path: Path,
    expected_replica_ids: Sequence[int],
) -> CpuQuotaContract:
    """Load an exact, complete contract without accepting inferred defaults."""

    payload = _regular_bytes(path, "CPU-quota contract")
    profile_payload = _regular_bytes(base_profile_path, "base profile")
    try:
        document = json.loads(payload)
        profile = json.loads(profile_payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CpuQuotaContractError(
            "CPU-quota contract or base profile is invalid JSON"
        ) from exc
    if not isinstance(document, dict) or set(document) != _CONTRACT_KEYS:
        raise CpuQuotaContractError("CPU-quota contract schema drifted")
    if not isinstance(profile, dict):
        raise CpuQuotaContractError("base profile must contain one object")
    expected_ids = tuple(expected_replica_ids)
    if (
        not expected_ids
        or any(type(replica) is not int or replica < 0 for replica in expected_ids)
        or len(set(expected_ids)) != len(expected_ids)
    ):
        raise CpuQuotaContractError("expected replica membership is invalid")
    if (
        document.get("schema_version") != 1
        or document.get("enabled") is not True
        or document.get("figure_eligible") is not False
        or document.get("launcher") != _LAUNCHER
        or document.get("manager_visibility") != "none"
        or not isinstance(document.get("contract_id"), str)
        or _IDENTIFIER.fullmatch(str(document["contract_id"])) is None
        or type(document.get("sampling_interval_ms")) is not int
        or not 100 <= int(document["sampling_interval_ms"]) <= 10_000
        or document.get("base_profile_id") != profile.get("profile_id")
        or document.get("base_profile_sha256") != _sha256(profile_payload)
        or _DIGEST.fullmatch(str(document.get("base_profile_sha256", ""))) is None
        or document.get("base_profile_canonical_sha256")
        != _profile_canonical_sha256(profile)
        or _DIGEST.fullmatch(str(document.get("base_profile_canonical_sha256", "")))
        is None
    ):
        raise CpuQuotaContractError(
            "CPU-quota contract identity or safety flags drifted"
        )
    raw_assignments = document.get("assignments")
    if not isinstance(raw_assignments, list):
        raise CpuQuotaContractError("CPU-quota assignments must be a list")
    assignments: list[CpuQuotaAssignment] = []
    class_quotas: dict[str, int] = {}
    for raw in raw_assignments:
        if not isinstance(raw, dict) or set(raw) != _ASSIGNMENT_KEYS:
            raise CpuQuotaContractError("CPU-quota assignment schema drifted")
        replica = raw.get("replica_id")
        label = raw.get("capacity_class")
        quota = raw.get("cpu_quota_percent")
        if (
            type(replica) is not int
            or replica < 0
            or not isinstance(label, str)
            or _IDENTIFIER.fullmatch(label) is None
            or type(quota) is not int
            or not 1 <= quota <= 1_000
        ):
            raise CpuQuotaContractError("CPU-quota assignment value is invalid")
        previous = class_quotas.setdefault(label, quota)
        if previous != quota:
            raise CpuQuotaContractError("one capacity class maps to multiple quotas")
        assignments.append(CpuQuotaAssignment(replica, label, quota))
    if tuple(assignment.replica_id for assignment in assignments) != expected_ids:
        raise CpuQuotaContractError(
            "CPU-quota assignments do not exactly cover membership"
        )
    if len(class_quotas) < 2 or len(set(class_quotas.values())) != len(class_quotas):
        raise CpuQuotaContractError("CPU-quota classes are not materially distinct")
    for assignment in assignments:
        if class_quotas[assignment.capacity_class] != assignment.cpu_quota_percent:
            raise CpuQuotaContractError("CPU-quota class mapping drifted")
    contract = CpuQuotaContract(
        schema_version=1,
        contract_id=str(document["contract_id"]),
        enabled=True,
        figure_eligible=False,
        launcher=_LAUNCHER,
        manager_visibility="none",
        sampling_interval_ms=int(document["sampling_interval_ms"]),
        base_profile_id=str(document["base_profile_id"]),
        base_profile_sha256=str(document["base_profile_sha256"]),
        base_profile_canonical_sha256=str(document["base_profile_canonical_sha256"]),
        assignments=tuple(assignments),
        contract_sha256=_sha256(payload),
    )
    return contract


def _unit_name(run_id: str, replica_id: int) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", run_id.lower()).strip("-")
    if not normalized:
        raise CpuQuotaContractError("run ID cannot produce a systemd unit identity")
    normalized = normalized[:96].rstrip("-")
    return f"kauri-{normalized}-r{replica_id}.scope"


def systemd_scope_command(
    contract: CpuQuotaContract,
    *,
    run_id: str,
    replica_id: int,
    command: Sequence[str],
) -> tuple[tuple[str, ...], str]:
    """Wrap one replica argv directly; no shell or string re-parsing is used."""

    original = tuple(command)
    if not original or any(
        not isinstance(argument, str) or not argument for argument in original
    ):
        raise CpuQuotaContractError("replica command contains an invalid argument")
    quota = contract.quota_percent(replica_id)
    unit = _unit_name(run_id, replica_id)
    return (
        (
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={unit}",
            "--property=CPUAccounting=yes",
            f"--property=CPUQuota={quota}%",
            "--",
            *original,
        ),
        unit,
    )


def parse_systemctl_show(payload: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in payload.splitlines():
        key, separator, value = raw_line.partition("=")
        if not separator or not key or key in result:
            raise CpuQuotaContractError("systemd property output is malformed")
        result[key] = value
    required = {"ActiveState", "SubState", "CPUQuotaPerSecUSec", "ControlGroup"}
    if not required.issubset(result) or not set(result).issubset(
        required | {"LoadState"}
    ):
        raise CpuQuotaContractError("systemd property output schema drifted")
    return result


def quota_per_second_usec(properties: Mapping[str, str]) -> int:
    raw = properties.get("CPUQuotaPerSecUSec")
    if not isinstance(raw, str):
        raise CpuQuotaContractError("CPUQuotaPerSecUSec is absent")
    match = re.fullmatch(r"([0-9]+)(us|ms|s)", raw)
    if match is None:
        raise CpuQuotaContractError("CPUQuotaPerSecUSec is not a bounded duration")
    scale = {"us": 1, "ms": 1_000, "s": 1_000_000}[match.group(2)]
    return int(match.group(1)) * scale


def parse_cpu_stat(payload: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for raw_line in payload.splitlines():
        fields = raw_line.split()
        if len(fields) != 2 or fields[0] in values:
            raise CpuQuotaContractError("cgroup cpu.stat is malformed")
        try:
            value = int(fields[1])
        except ValueError as exc:
            raise CpuQuotaContractError(
                "cgroup cpu.stat value is not an integer"
            ) from exc
        if value < 0:
            raise CpuQuotaContractError("cgroup cpu.stat value is negative")
        values[fields[0]] = value
    if not set(_CGROUP_STAT_KEYS).issubset(values):
        raise CpuQuotaContractError("cgroup cpu.stat lacks required accounting fields")
    return {key: values[key] for key in _CGROUP_STAT_KEYS}


def _replace_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical(value))
    os.replace(temporary, path)


def verify_linux_environment(
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    system_name: Callable[[], str] = platform.system,
    find_executable: Callable[[str], str | None] = shutil.which,
    run_command: Callable[..., Any] = subprocess.run,
) -> dict[str, object]:
    """Run a no-workload capability probe for the exact Linux launcher."""

    if system_name() != "Linux":
        raise CpuQuotaContractError("CPU-quota execution requires Linux")
    controllers_path = Path(cgroup_root) / "cgroup.controllers"
    try:
        controllers = tuple(
            sorted(controllers_path.read_text(encoding="ascii").split())
        )
    except OSError as exc:
        raise CpuQuotaContractError("cgroup v2 controllers are unavailable") from exc
    if "cpu" not in controllers:
        raise CpuQuotaContractError("cgroup v2 CPU controller is unavailable")
    executables: dict[str, str] = {}
    for name in ("systemctl", "systemd-run"):
        resolved = find_executable(name)
        if not isinstance(resolved, str) or not Path(resolved).is_absolute():
            raise CpuQuotaContractError(f"{name} is unavailable")
        executables[name] = resolved
    user_manager = run_command(
        (executables["systemctl"], "--user", "is-system-running"),
        check=False,
        capture_output=True,
        text=True,
    )
    if user_manager.returncode not in {0, 1}:
        detail = user_manager.stderr.strip() or user_manager.stdout.strip()
        raise CpuQuotaContractError(f"systemd user manager is unavailable: {detail}")
    probe_unit = f"kauri-preflight-{os.getpid()}-{time.monotonic_ns()}.scope"
    probe = run_command(
        (
            executables["systemd-run"],
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={probe_unit}",
            "--property=CPUAccounting=yes",
            "--property=CPUQuota=25%",
            "--",
            "/usr/bin/true",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        detail = probe.stderr.strip() or probe.stdout.strip()
        raise CpuQuotaContractError(f"transient CPU-quota scope failed: {detail}")
    return {
        "schema_version": 1,
        "kind": "kauri-cpu-quota-environment-v1",
        "verified": True,
        "kernel": platform.release(),
        "cgroup_version": 2,
        "controllers": list(controllers),
        "systemctl_path": executables["systemctl"],
        "systemd_run_path": executables["systemd-run"],
        "probe_quota_percent": 25,
        "probe_exit_code": 0,
    }


class CpuQuotaRuntime:
    """Apply, observe, and clean up one contract without exposing it to Kauri."""

    def __init__(
        self,
        contract: CpuQuotaContract,
        *,
        run_id: str,
        run_directory: Path,
        base_spawn: Callable[..., tuple[object, object]] | None = None,
        show_unit: Callable[[str], str] | None = None,
        read_cpu_stat: Callable[[Path], Mapping[str, int]] | None = None,
        read_cgroup_procs: Callable[[Path], Sequence[int]] | None = None,
        process_group: Callable[[int], int] = os.getpgid,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if base_spawn is None:
            from .profiled_fault_runtime import spawn_owned_process

            base_spawn = spawn_owned_process
        self.contract = contract
        self.run_id = run_id
        self.root = Path(run_directory)
        self._base_spawn = base_spawn
        self._show_unit = show_unit or self._default_show_unit
        self._read_cpu_stat = read_cpu_stat or self._default_read_cpu_stat
        self._read_cgroup_procs = read_cgroup_procs or self._default_read_cgroup_procs
        self._process_group = process_group
        self._monotonic_ns = monotonic_ns
        self._units: dict[int, dict[str, object]] = {}
        self._monitor_stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._monitor_error: BaseException | None = None
        _replace_json(
            self.root / "runtime/cpu-quota-contract.json", _contract_document(contract)
        )

    @staticmethod
    def _default_show_unit(unit: str) -> str:
        result = subprocess.run(
            (
                "systemctl",
                "--user",
                "show",
                unit,
                "--no-pager",
                "--property=ActiveState",
                "--property=SubState",
                "--property=LoadState",
                "--property=CPUQuotaPerSecUSec",
                "--property=ControlGroup",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        if "LoadState=not-found" in result.stdout:
            return (
                "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
                "CPUQuotaPerSecUSec=0us\nControlGroup=\n"
            )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise CpuQuotaContractError(
                f"cannot inspect transient unit {unit}: {detail}"
            )
        if not result.stdout.strip():
            return (
                "ActiveState=inactive\nSubState=dead\n"
                "CPUQuotaPerSecUSec=0us\nControlGroup=\n"
            )
        return result.stdout

    @staticmethod
    def _default_read_cpu_stat(path: Path) -> Mapping[str, int]:
        try:
            return parse_cpu_stat(path.read_text(encoding="ascii"))
        except OSError as exc:
            raise CpuQuotaContractError(
                f"cannot read cgroup accounting at {path}"
            ) from exc

    @staticmethod
    def _default_read_cgroup_procs(path: Path) -> Sequence[int]:
        try:
            rows = path.read_text(encoding="ascii").splitlines()
            pids = tuple(int(row) for row in rows)
        except (OSError, ValueError) as exc:
            raise CpuQuotaContractError(
                f"cannot read cgroup ownership at {path}"
            ) from exc
        if not pids or any(pid <= 0 for pid in pids) or len(set(pids)) != len(pids):
            raise CpuQuotaContractError("cgroup ownership is empty or malformed")
        return pids

    def _active_properties(self, replica_id: int, unit: str) -> dict[str, str]:
        expected_quota = self.contract.quota_percent(replica_id) * 10_000
        last: dict[str, str] | None = None
        for _attempt in range(40):
            properties = parse_systemctl_show(self._show_unit(unit))
            last = properties
            cgroup = properties["ControlGroup"]
            if (
                properties["ActiveState"] == "active"
                and properties["SubState"] in {"running", "start"}
                and cgroup.startswith("/")
                and ".." not in Path(cgroup).parts
                and quota_per_second_usec(properties) == expected_quota
            ):
                return properties
            time.sleep(0.05)
        raise CpuQuotaContractError(
            f"transient unit {unit} did not expose the exact active quota: {last}"
        )

    def _write_launch_receipt(self) -> None:
        _replace_json(
            self.root / "runtime/cpu-quota-launch.json",
            {
                "schema_version": 1,
                "launcher": self.contract.launcher,
                "contract_id": self.contract.contract_id,
                "contract_sha256": self.contract.contract_sha256,
                "manager_visibility": "none",
                "replicas": [self._units[replica] for replica in sorted(self._units)],
            },
        )

    def spawn_owned_process(
        self, registry: object, **kwargs: object
    ) -> tuple[object, object]:
        replica_id = int(kwargs["replica_id"])
        if replica_id < 0:
            return self._base_spawn(registry, **kwargs)
        wrapped, unit = systemd_scope_command(
            self.contract,
            run_id=self.run_id,
            replica_id=replica_id,
            command=tuple(kwargs["command"]),
        )
        record, log = self._base_spawn(registry, **{**kwargs, "command": wrapped})
        properties = self._active_properties(replica_id, unit)
        cgroup = properties["ControlGroup"]
        cgroup_path = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
        stat_path = cgroup_path / "cpu.stat"
        owned_pid = getattr(record, "pid", None)
        owned_pgid = getattr(record, "pgid", None)
        cgroup_pids = tuple(self._read_cgroup_procs(cgroup_path / "cgroup.procs"))
        try:
            cgroup_pgids = tuple(self._process_group(pid) for pid in cgroup_pids)
        except OSError as exc:
            raise CpuQuotaContractError(
                f"cannot verify process-group ownership for {unit}"
            ) from exc
        if (
            type(owned_pid) is not int
            or type(owned_pgid) is not int
            or owned_pid <= 0
            or owned_pid != owned_pgid
            or owned_pgid not in cgroup_pgids
        ):
            raise CpuQuotaContractError(
                f"transient unit {unit} does not own the launched process group"
            )
        self._units[replica_id] = {
            "replica_id": replica_id,
            "cpu_quota_percent": self.contract.quota_percent(replica_id),
            "unit": unit,
            "control_group": cgroup,
            "cpu_stat_path": str(stat_path),
            "owned_pid": owned_pid,
            "owned_pgid": owned_pgid,
            "cgroup_pids": list(cgroup_pids),
            "active_state": properties["ActiveState"],
            "sub_state": properties["SubState"],
            "cpu_quota_per_second_usec": quota_per_second_usec(properties),
        }
        self._write_launch_receipt()
        return record, log

    def sample_once(self) -> list[dict[str, object]]:
        timestamp = self._monotonic_ns()
        rows: list[dict[str, object]] = []
        for replica_id in sorted(self._units):
            unit = self._units[replica_id]
            properties = parse_systemctl_show(self._show_unit(str(unit["unit"])))
            row: dict[str, object] = {
                "schema_version": 1,
                "source_monotonic_ns": timestamp,
                "replica_id": replica_id,
                "cpu_quota_percent": unit["cpu_quota_percent"],
                "active_state": properties["ActiveState"],
                "sub_state": properties["SubState"],
            }
            if properties["ActiveState"] == "active":
                row["cpu_stat"] = dict(
                    self._read_cpu_stat(Path(str(unit["cpu_stat_path"])))
                )
            rows.append(row)
        path = self.root / "raw/cpu-quota-samples.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as output:
            for row in rows:
                output.write(_canonical(row))
            output.flush()
            os.fsync(output.fileno())
        return rows

    def _monitor_loop(self) -> None:
        try:
            while not self._monitor_stop.is_set():
                self.sample_once()
                self._monitor_stop.wait(self.contract.sampling_interval_ms / 1_000)
        except BaseException as exc:
            self._monitor_error = exc
            self._monitor_stop.set()

    def start_monitor(self) -> None:
        if (
            set(self._units) != set(self.contract.replica_ids)
            or self._monitor is not None
        ):
            raise CpuQuotaContractError(
                "CPU-quota monitor requires every exact replica"
            )
        self._monitor = threading.Thread(
            target=self._monitor_loop,
            name=f"kauri-cpu-quota-{self.run_id}",
            daemon=False,
        )
        self._monitor.start()

    def stop_monitor(self) -> None:
        monitor = self._monitor
        if monitor is None:
            return
        self._monitor_stop.set()
        monitor.join(timeout=5.0)
        if monitor.is_alive():
            raise CpuQuotaContractError("CPU-quota monitor did not stop")
        if self._monitor_error is not None:
            raise CpuQuotaContractError(
                "CPU-quota monitor failed"
            ) from self._monitor_error

    def verify_cleanup(self) -> dict[str, object]:
        self.stop_monitor()
        rows: list[dict[str, object]] = []
        complete = True
        for replica_id in sorted(self._units):
            unit = self._units[replica_id]
            properties: dict[str, str] | None = None
            inactive = False
            for _attempt in range(40):
                properties = parse_systemctl_show(self._show_unit(str(unit["unit"])))
                inactive = (
                    properties["ActiveState"] == "inactive"
                    and properties["ControlGroup"] == ""
                )
                if inactive:
                    break
                time.sleep(0.05)
            assert properties is not None
            complete = complete and inactive
            rows.append(
                {
                    "replica_id": replica_id,
                    "unit": unit["unit"],
                    "load_state": properties.get("LoadState", "loaded"),
                    "active_state": properties["ActiveState"],
                    "sub_state": properties["SubState"],
                    "control_group": properties["ControlGroup"],
                }
            )
        result = {"schema_version": 1, "complete": complete, "units": rows}
        _replace_json(self.root / "runtime/cpu-quota-cleanup.json", result)
        if not complete:
            raise CpuQuotaContractError(
                "one or more transient CPU-quota units remain active"
            )
        return result
