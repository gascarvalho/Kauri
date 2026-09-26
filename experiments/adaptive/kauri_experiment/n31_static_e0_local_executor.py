"""Fail-closed preparation seam for the W16 local N=31 feasibility run.

This module deliberately contains *no launch path*.  It specifies and checks
the immutable inputs a later reviewed launcher would require, while keeping
the current experiment incapable of starting a replica or a cluster job.
In particular, a preflight is not execution authority: it must be bound to
the exact executables, written tree file, native epoch-zero digest helper,
and a held non-listening manager socket.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import platform
import signal
from pathlib import Path
import socket
import subprocess
import time
import uuid
from typing import Callable, Mapping

from . import n31_static_e0_feasibility as feasibility
from . import cpu_quota
from . import profiled_fault_runtime as runtime
from .processes import ProcessRegistry


SCHEMA = "kauri-n31-static-e0-local-executor-v1"
_BINARY_NAMES = ("app", "keygen", "tls_keygen", "native_digest")


class LocalExecutorError(RuntimeError):
    """A local feasibility run lacks a proof required before execution."""


@dataclass(frozen=True, slots=True)
class BoundNoListener:
    """A bound, non-listening endpoint retained by the future launcher."""

    socket_handle: socket.socket
    host: str
    port: int

    def close(self) -> None:
        self.socket_handle.close()


@dataclass(frozen=True, slots=True)
class LaunchInputs:
    """Immutable, source-bound inputs for exactly one W16 arm."""

    run_id: str
    arm: str
    revision: str
    profile_sha256: str
    treegen_path: Path
    treegen_sha256: str
    epoch_zero_digest: str
    binaries: Mapping[str, Path]
    binary_sha256: Mapping[str, str]
    source_instances: Mapping[str, str]
    hard_deadline_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class CleanupVerification:
    """Evidence needed before sealing either an abort or a success receipt."""

    registered_replica_ids: tuple[int, ...]
    every_group_inactive: bool
    no_listener_released: bool
    cleanup_error: str | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exact_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LocalExecutorError(f"{name} must be a mapping")
    return value


def _validate_preflight(preflight: Mapping[str, object], plan: feasibility.FeasibilityPlan) -> None:
    """Require an execution-specific, hash-bound preflight receipt.

    The present preflight producer intentionally does not have these hashes.
    Thus it correctly cannot authorize this executor until the producer is
    extended and independently reviewed.
    """

    if preflight.get("verdict") != "PREFLIGHT_OK_NO_EXECUTION":
        raise LocalExecutorError("preflight did not pass without execution")
    if preflight.get("kind") != feasibility.SCHEMA:
        raise LocalExecutorError("preflight schema is not the W16 schema")
    if preflight.get("arm") != plan.arm:
        raise LocalExecutorError("preflight arm differs from frozen plan")
    if preflight.get("profile_sha256") != plan.profile.sha256:
        raise LocalExecutorError("preflight profile hash differs from frozen plan")
    if preflight.get("treegen_sha256") != plan.treegen_sha256:
        raise LocalExecutorError("preflight tree hash differs from frozen plan")
    revision = preflight.get("revision")
    if not isinstance(revision, str) or len(revision) != 40:
        raise LocalExecutorError("preflight lacks an exact source revision")
    binaries = _require_mapping(preflight.get("binaries"), "preflight binaries")
    hashes = _require_mapping(preflight.get("binary_sha256"), "preflight binary hashes")
    if set(binaries) != set(_BINARY_NAMES) or set(hashes) != set(_BINARY_NAMES):
        raise LocalExecutorError("preflight must bind exactly four executable paths and hashes")
    if any(not _exact_sha256(hashes[name]) for name in _BINARY_NAMES):
        raise LocalExecutorError("preflight contains a malformed executable hash")


def reserve_no_listener(
    *,
    host: str,
    port: int,
    socket_factory: Callable[..., socket.socket] = socket.socket,
) -> BoundNoListener:
    """Bind, but never listen on, the reviewed local manager endpoint."""

    if (host, port) != (
        feasibility.PINNED_MANAGER_HOST,
        feasibility.PINNED_MANAGER_PORT,
    ):
        raise LocalExecutorError("manager endpoint differs from the frozen no-listener condition")
    held = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
    try:
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        held.bind((host, port))
    except BaseException:
        held.close()
        raise
    return BoundNoListener(socket_handle=held, host=host, port=port)


def _native_digest(*, helper: Path, arm: str, treegen_path: Path, timeout_s: float) -> str:
    if timeout_s <= 0:
        raise LocalExecutorError("native digest helper has no remaining global timeout")
    try:
        completed = subprocess.run(
            (str(helper), arm, str(treegen_path)),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise LocalExecutorError("native digest helper exceeded global timeout") from error
    if completed.returncode != 0:
        raise LocalExecutorError("native digest helper rejected frozen tree input")
    digest = completed.stdout.strip()
    if not _exact_sha256(digest):
        raise LocalExecutorError("native digest helper emitted a malformed digest")
    return digest


def prepare_launch(
    *,
    plan: feasibility.FeasibilityPlan,
    preflight: Mapping[str, object],
    treegen_path: Path,
    run_id: str,
    hard_timeout_s: float,
    fixed_deadline_monotonic_ns: int | None = None,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> LaunchInputs:
    """Verify immutable inputs without generating keys or launching Kauri.

    The caller must retain :func:`reserve_no_listener`'s result from before
    process creation through cleanup.  This method intentionally does not
    reserve it itself, because returning after releasing that socket would be
    a misleading proof.
    """

    if not isinstance(run_id, str) or not run_id:
        raise LocalExecutorError("run ID must be a non-empty immutable value")
    if hard_timeout_s <= 0:
        raise LocalExecutorError("global hard timeout must be positive")
    _validate_preflight(preflight, plan)
    treegen_path = treegen_path.resolve()
    if not treegen_path.is_file() or _sha256(treegen_path) != plan.treegen_sha256:
        raise LocalExecutorError("written tree input does not equal frozen plan bytes")
    binaries_doc = _require_mapping(preflight["binaries"], "preflight binaries")
    hashes_doc = _require_mapping(preflight["binary_sha256"], "preflight binary hashes")
    binaries = {name: Path(str(binaries_doc[name])).resolve() for name in _BINARY_NAMES}
    for name, path in binaries.items():
        if not path.is_file() or not path.stat().st_mode & 0o111:
            raise LocalExecutorError(f"{name} executable is missing or not executable")
        if _sha256(path) != hashes_doc[name]:
            raise LocalExecutorError(f"{name} executable hash differs from preflight")
    started = monotonic_ns()
    deadline = (fixed_deadline_monotonic_ns if fixed_deadline_monotonic_ns is not None
                else started + int(hard_timeout_s * 1_000_000_000))
    if deadline <= started:
        raise LocalExecutorError("global deadline expired before native digest proof")
    remaining_s = (deadline - monotonic_ns()) / 1_000_000_000
    digest = _native_digest(
        helper=binaries["native_digest"], arm=plan.arm,
        treegen_path=treegen_path, timeout_s=remaining_s,
    )
    source_instances = {
        f"replica-{replica}": f"{run_id}-replica-{replica}"
        for replica in plan.profile.replica_ids
    }
    return LaunchInputs(
        run_id=run_id,
        arm=plan.arm,
        revision=str(preflight["revision"]),
        profile_sha256=plan.profile.sha256,
        treegen_path=treegen_path,
        treegen_sha256=plan.treegen_sha256,
        epoch_zero_digest=digest,
        binaries=binaries,
        binary_sha256={name: str(hashes_doc[name]) for name in _BINARY_NAMES},
        source_instances=source_instances,
        hard_deadline_monotonic_ns=deadline,
    )


def assert_exactly_31_registered(registry: ProcessRegistry) -> None:
    """Reject an incomplete or duplicated process registry before event trust."""

    records = registry.records
    expected = tuple(range(feasibility.REPLICA_COUNT))
    if tuple(sorted(record.replica_id for record in records)) != expected:
        raise LocalExecutorError("launcher did not register exactly the 31 replica groups")


def seal_outcome(
    *,
    directory: Path,
    inputs: LaunchInputs,
    cleanup: CleanupVerification,
    streams: Mapping[str, list[Mapping[str, object]]],
    event_gate: Callable[..., tuple[bool, str]] = feasibility.event_gate,
) -> Path:
    """Seal one receipt/abort only after all owned groups are verified inactive.

    A failure of the evidence gate becomes a sealed abort, never a success.
    This helper is not called by a launcher while launch remains disabled.
    """

    if tuple(sorted(cleanup.registered_replica_ids)) != tuple(range(feasibility.REPLICA_COUNT)):
        raise LocalExecutorError("cannot seal outcome without all 31 registered groups")
    if not cleanup.every_group_inactive or not cleanup.no_listener_released or cleanup.cleanup_error:
        raise LocalExecutorError("cannot seal outcome before verified complete cleanup")
    passed, detail = event_gate(
        streams,
        observer=2,
        run_id=inputs.run_id,
        source_instances=inputs.source_instances,
        epoch_digest=inputs.epoch_zero_digest,
        expected_terminal_reason="shared_outbox_delivery_failed",
    )
    payload = {
        "schema": SCHEMA,
        "run_id": inputs.run_id,
        "attempts": 1,
        "retries": 0,
        "input": {
            "arm": inputs.arm,
            "revision": inputs.revision,
            "profile_sha256": inputs.profile_sha256,
            "treegen_sha256": inputs.treegen_sha256,
            "epoch_zero_digest": inputs.epoch_zero_digest,
            "binary_sha256": dict(inputs.binary_sha256),
            "hard_deadline_monotonic_ns": inputs.hard_deadline_monotonic_ns,
        },
        "cleanup": asdict(cleanup),
        "event_gate": {"passed": passed, "detail": detail},
        "verdict": "PASS" if passed else "ABORT",
    }
    directory = directory.resolve()
    target = directory / ("feasibility-receipt.json" if passed else "feasibility-abort.json")
    if target.exists() or (directory / "feasibility-receipt.json").exists() or (directory / "feasibility-abort.json").exists():
        raise LocalExecutorError("one outcome has already been sealed; relaunch is forbidden")
    runtime.write_json_exclusive(target, payload)
    return target


def _generate_identities_bounded(
    *, plan: feasibility.FeasibilityPlan, inputs: Mapping[str, Path],
    config_directory: Path, deadline_ns: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, str]]:
    """Generate each exact identity file within the one global deadline."""

    commands = runtime.identity_generation_commands(
        plan.profile, keygen_binary=inputs["keygen"],
        tls_keygen_binary=inputs["tls_keygen"],
    )
    outputs: dict[str, str] = {}
    for label, command in commands.items():
        remaining_s = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining_s <= 0:
            raise LocalExecutorError("identity generation exceeded global deadline")
        try:
            result = subprocess.run(
                command, cwd=config_directory, check=False, capture_output=True,
                text=True, timeout=remaining_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalExecutorError(f"{label} identity generation timed out") from exc
        if result.returncode != 0:
            raise LocalExecutorError(f"{label} identity generation exited {result.returncode}")
        outputs[label] = result.stdout
        runtime.write_exclusive(
            config_directory / f"{label}-identities.txt", result.stdout.encode()
        )
    count = len(plan.profile.replica_ids)
    return (
        runtime._parse_identity_output(
            outputs["bls"], expected_count=count,
            expected_fields=frozenset({"pub", "sec"}), label="BLS keygen"
        ),
        runtime._parse_identity_output(
            outputs["tls"], expected_count=count + 1,
            expected_fields=frozenset({"crt", "sec", "cid"}), label="TLS keygen"
        ),
        runtime._parse_identity_output(
            outputs["issuer"], expected_count=1,
            expected_fields=frozenset({"pub", "sec"}), label="issuer keygen"
        )[0],
    )


def execute_once(
    *, plan: feasibility.FeasibilityPlan, preflight: Mapping[str, object],
    directory: Path, hard_timeout_s: float = 180.0,
    quota_contract: cpu_quota.CpuQuotaContract | None = None,
    required_complete_cycles: int = 1,
) -> dict[str, object]:
    """One no-manager, zero-retry feasibility attempt.

    This is not a validated throughput study. The caller must add an
    external hard watchdog (for example GNU timeout) as a final kill boundary.
    All in-process failures are sealed, including partial spawn/cleanup truth.
    """

    if hard_timeout_s <= 20 or hard_timeout_s > 300:
        raise LocalExecutorError("local hard timeout must be in (20, 300] seconds")
    if required_complete_cycles < 1 or required_complete_cycles > 20:
        raise LocalExecutorError("required complete cycle count is outside 1..20")
    if quota_contract is not None and platform.system() != "Linux":
        raise LocalExecutorError("CPU quota mode requires Linux")
    directory = directory.resolve()
    if directory.exists():
        raise LocalExecutorError("one attempt requires an exact fresh output directory")
    _validate_preflight(preflight, plan)
    directory.mkdir(parents=True, mode=0o700)
    for subdirectory in ("config", "logs", "raw"):
        (directory / subdirectory).mkdir(mode=0o700)
    started_ns = time.monotonic_ns()
    deadline_ns = started_ns + int(hard_timeout_s * 1_000_000_000)
    work_deadline_ns = deadline_ns - 10_000_000_000
    run_id = f"w16-local-{uuid.uuid4().hex}"
    registry = ProcessRegistry()
    quota_runtime: cpu_quota.CpuQuotaRuntime | None = None
    log_handles = []
    held: BoundNoListener | None = None
    inputs: LaunchInputs | None = None
    witness = False
    failure: str | None = None
    cleanup_error: str | None = None
    quota_cleanup: dict[str, object] | None = None
    raw_hashes: dict[str, str] = {}
    artifact_hashes: dict[str, str] = {}
    old_alarm = signal.getsignal(signal.SIGALRM)

    def _alarm(_signal: int, _frame: object) -> None:
        raise TimeoutError("local W16 work deadline elapsed")

    try:
        if old_alarm != signal.SIG_DFL:
            raise LocalExecutorError("caller already owns SIGALRM")
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, hard_timeout_s - 10)
        held = reserve_no_listener(
            host=feasibility.PINNED_MANAGER_HOST,
            port=feasibility.PINNED_MANAGER_PORT,
        )
        binaries_doc = _require_mapping(preflight["binaries"], "preflight binaries")
        binaries = {name: Path(str(binaries_doc[name])).resolve() for name in _BINARY_NAMES}
        for name, executable in binaries.items():
            if _sha256(executable) != preflight["binary_sha256"][name]:
                raise LocalExecutorError(f"{name} binary changed before key generation")
        bls, tls, issuer = _generate_identities_bounded(
            plan=plan, inputs=binaries, config_directory=directory / "config",
            deadline_ns=work_deadline_ns,
        )
        source_instances = {
            f"replica-{replica}": f"{run_id}-replica-{replica}"
            for replica in plan.profile.replica_ids
        }
        paths = feasibility.write_prepared_inputs(
            plan=plan, directory=directory, bls=bls, tls=tls, issuer=issuer,
            run_id=run_id, source_instances=source_instances,
        )
        inputs = prepare_launch(
            plan=plan, preflight=preflight, treegen_path=Path(paths["treegen"]),
            run_id=run_id, hard_timeout_s=hard_timeout_s,
            fixed_deadline_monotonic_ns=work_deadline_ns,
        )
        if quota_contract is not None:
            quota_runtime = cpu_quota.CpuQuotaRuntime(
                quota_contract, run_id=run_id, run_directory=directory
            )
        for replica in plan.profile.replica_ids:
            if time.monotonic_ns() >= work_deadline_ns:
                raise TimeoutError("local W16 deadline elapsed while launching replicas")
            spawn = (quota_runtime.spawn_owned_process if quota_runtime is not None
                     else runtime.spawn_owned_process)
            record, handle = spawn(
                registry, name=f"replica-{replica}", replica_id=replica,
                command=(str(inputs.binaries["app"]), "--conf", paths["main"],
                         "--conf", str(directory / "config" / f"replica-{replica}.conf")),
                log_path=directory / "logs" / f"replica-{replica}.log",
                working_directory=directory,
            )
            log_handles.append(handle)
            if record.process.poll() is not None:
                raise LocalExecutorError(f"replica {replica} exited during launch")
        assert_exactly_31_registered(registry)
        if quota_runtime is not None:
            quota_runtime.start_monitor()
        while time.monotonic_ns() < work_deadline_ns:
            if any(record.process.poll() is not None for record in registry.records):
                raise LocalExecutorError("replica exited before owned cleanup")
            streams = {
                f"replica-{replica}": runtime.read_jsonl(
                    directory / "raw" / f"replica-{replica}.jsonl",
                    allow_partial=True,
                ) for replica in plan.profile.replica_ids
            }
            event_passed, _detail = feasibility.event_gate(
                streams, observer=plan.profile.authoritative_observer,
                run_id=run_id, source_instances=inputs.source_instances,
                epoch_digest=inputs.epoch_zero_digest,
                expected_terminal_reason="shared_outbox_delivery_failed",
            )
            if event_passed:
                observer_events = streams[f"replica-{plan.profile.authoritative_observer}"]
                terminal_sequence = max(
                    event["source_sequence"] for event in observer_events
                    if event["event_type"] == "adaptive_v2_reporting_terminal"
                )
                tree_zero = [event for event in observer_events
                             if event["event_type"] == "adaptive.configuration_active"
                             and event["payload"].get("epoch_number") == 0
                             and event["payload"].get("tree_id") == 0
                             and event["payload"].get("epoch_digest") == inputs.epoch_zero_digest
                             and (quota_runtime is None or
                                  event["source_sequence"] > terminal_sequence)]
                if len(tree_zero) >= required_complete_cycles + 1:
                    witness = True
                    break
            time.sleep(1.0)
        if not witness:
            raise TimeoutError("local W16 event witness was not complete by deadline")
    except BaseException as exc:
        failure = str(exc).strip() or type(exc).__name__
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_alarm)
        if quota_runtime is not None:
            _stopped, monitor_error = quota_runtime.stop_monitor()
            if monitor_error is not None:
                cleanup_error = f"CPU quota monitor failed: {monitor_error}"
        try:
            registry.cleanup(timeout_s=0.2)
        except BaseException as exc:
            cleanup_error = str(exc).strip() or type(exc).__name__
        if quota_runtime is not None:
            try:
                quota_cleanup = quota_runtime.verify_cleanup()
            except BaseException as exc:
                cleanup_error = (cleanup_error or "") + (
                    f"; CPU quota cleanup failed: {exc}"
                )
        for handle in log_handles:
            handle.close()
        if held is not None:
            held.close()
        for replica in plan.profile.replica_ids:
            path = directory / "raw" / f"replica-{replica}.jsonl"
            try:
                if path.is_file():
                    raw_hashes[f"replica-{replica}"] = _sha256(path)
            except BaseException as exc:
                cleanup_error = (cleanup_error or "") + (
                    f"; cannot hash replica {replica} raw stream: {exc}"
                )
        try:
            for path in sorted(directory.rglob("*")):
                if path.is_symlink():
                    raise LocalExecutorError("artifact tree contains an unowned symlink")
                if path.is_file():
                    artifact_hashes[str(path.relative_to(directory))] = _sha256(path)
        except BaseException as exc:
            cleanup_error = (cleanup_error or "") + f"; cannot inventory artifacts: {exc}"

    records = registry.records
    all_exited = all(record.process.poll() is not None for record in records)
    complete = tuple(sorted(record.replica_id for record in records)) == tuple(range(31))
    if failure is None and not (complete and all_exited and cleanup_error is None):
        failure = "owned process cleanup did not prove all 31 replica exits"
    if failure is None and inputs is not None:
        try:
            final_streams = {
                f"replica-{replica}": runtime.read_jsonl(
                    directory / "raw" / f"replica-{replica}.jsonl"
                ) for replica in plan.profile.replica_ids
            }
            passed, detail = feasibility.event_gate(
                final_streams, observer=plan.profile.authoritative_observer,
                run_id=run_id, source_instances=inputs.source_instances,
                epoch_digest=inputs.epoch_zero_digest,
                expected_terminal_reason="shared_outbox_delivery_failed",
            )
            if not passed:
                failure = f"post-cleanup exact stream gate failed: {detail}"
        except BaseException as exc:
            failure = f"post-cleanup raw stream validation failed: {exc}"
    if failure is None and cleanup_error is not None:
        failure = "raw artifact inventory or cleanup is incomplete"
    payload: dict[str, object] = {
        "schema": SCHEMA, "run_id": run_id, "attempts": 1, "retries": 0,
        "verdict": "PASS" if failure is None else "ABORT",
        "failure": failure, "cleanup_error": cleanup_error,
        "registered_replica_ids": [record.replica_id for record in records],
        "all_registered_exited": all_exited,
        "treegen_sha256": plan.treegen_sha256,
        "raw_sha256": raw_hashes,
        "artifact_sha256": artifact_hashes,
        "quota_contract_sha256": (
            quota_contract.contract_sha256 if quota_contract is not None else None
        ),
        "quota_cleanup": quota_cleanup,
        "required_complete_cycles": required_complete_cycles,
        "preflight": dict(preflight),
        "epoch_zero_digest": inputs.epoch_zero_digest if inputs is not None else None,
        "started_monotonic_ns": started_ns,
        "ended_monotonic_ns": time.monotonic_ns(),
    }
    name = "feasibility-receipt.json" if failure is None else "feasibility-abort.json"
    runtime.write_json_exclusive(directory / name, payload)
    return payload
