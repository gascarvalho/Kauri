"""Fail-closed local preflight for the W16 static Epoch-0 feasibility test.

This is intentionally *not* a cluster launcher and it never starts Kauri.
It freezes the exact configuration that a later, separately reviewed local
execution harness would need to test: 31 adaptive-v2 replicas, the reviewed
21-tree file schedule, and a pinned endpoint at which no manager is listening.
The no-listener behaviour is a protocol feasibility question, not a result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import time
import uuid
from typing import Callable, Mapping

from . import profiled_fault_runtime as runtime
from .static_topology_n31 import (
    FANOUT,
    PIPELINE_STRETCH,
    REPLICA_COUNT,
    TREE_COUNT,
    build_schedule,
    render_treegen_bytes,
)
from .processes import ProcessRegistry


REQUIRED_BRANCH = "feature/adaptive-epoch-throughput"
PINNED_MANAGER_HOST = "127.0.0.1"
PINNED_MANAGER_PORT = 27991
SCHEMA = "kauri-n31-static-e0-feasibility-preflight-v1"


class StaticE0FeasibilityError(RuntimeError):
    """The local-only feasibility attempt is not safe to prepare."""


@dataclass(frozen=True, slots=True)
class FeasibilityPlan:
    """All byte-level inputs for one arm; no process has been launched."""

    arm: str
    profile: "AllLiveProfile"
    treegen_bytes: bytes
    treegen_sha256: str
    manager_endpoint: str


@dataclass(frozen=True, slots=True)
class AllLiveProfile:
    """The entire local feasibility condition, without any fault plan."""

    profile_id: str = "n31-static-e0-local-feasibility-v1"
    replica_ids: tuple[int, ...] = tuple(range(REPLICA_COUNT))
    quorum: int = 21
    fault_threshold: int = 10
    fanout: int = FANOUT
    pipeline_depth: int = PIPELINE_STRETCH
    authoritative_observer: int = 2
    block_size: int = 1000
    peer_base: int = 29100
    client_base: int = 30100
    tree_switch_period_blocks: int = 1
    aggregation_timeout_s: float = 0.5
    leader_progress_timeout_s: float = 5.0
    leader_activation_grace_s: float = 1.0
    activation_delay_blocks: int = 5

    def document(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "replica_ids": list(self.replica_ids),
            "quorum": self.quorum,
            "fault_threshold": self.fault_threshold,
            "fanout": self.fanout,
            "pipeline_depth": self.pipeline_depth,
            "authoritative_observer": self.authoritative_observer,
            "block_size": self.block_size,
            "peer_base": self.peer_base,
            "client_base": self.client_base,
            "tree_switch_period_blocks": self.tree_switch_period_blocks,
            "aggregation_timeout_s": self.aggregation_timeout_s,
            "leader_progress_timeout_s": self.leader_progress_timeout_s,
            "leader_activation_grace_s": self.leader_activation_grace_s,
            "activation_delay_blocks": self.activation_delay_blocks,
            "faults": [],
            "cpu_quotas": [],
            "manager_mode": "pinned_no_listener",
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(self.document(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise StaticE0FeasibilityError(
            "cannot inspect repository state: " + result.stderr.strip()
        )
    return result.stdout


def _require_executable(path: Path, name: str) -> Path:
    candidate = path.resolve()
    if not candidate.is_file() or not candidate.stat().st_mode & 0o111:
        raise StaticE0FeasibilityError(f"{name} is not an executable file: {candidate}")
    return candidate


def _assert_no_listener(endpoint: tuple[str, int]) -> None:
    """Reject a live endpoint; this is only a race-aware preflight observation."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        if probe.connect_ex(endpoint) == 0:
            raise StaticE0FeasibilityError(
                "pinned manager endpoint has a listener; the no-listener "
                "feasibility test would not be the reviewed condition"
            )


def frozen_plan(*, arm: str) -> FeasibilityPlan:
    """Build the exact reviewed static file and retain only N=31/F5/P2 shape."""

    profile = AllLiveProfile()
    if (
        len(profile.replica_ids) != REPLICA_COUNT
        or profile.quorum != 21
        or profile.fault_threshold != 10
        or profile.fanout != FANOUT
        or profile.pipeline_depth != PIPELINE_STRETCH
    ):
        raise StaticE0FeasibilityError("profile is not the reviewed N31/F5/P2 shape")
    if arm not in {"slow-roots", "fast-roots"}:
        raise StaticE0FeasibilityError("arm must be slow-roots or fast-roots")
    treegen_bytes = render_treegen_bytes(build_schedule(arm))
    if treegen_bytes.count(b"\n") != TREE_COUNT:
        raise StaticE0FeasibilityError("static tree artifact does not contain 21 trees")
    return FeasibilityPlan(
        arm=arm,
        profile=profile,
        treegen_bytes=treegen_bytes,
        treegen_sha256=hashlib.sha256(treegen_bytes).hexdigest(),
        manager_endpoint=f"{PINNED_MANAGER_HOST}:{PINNED_MANAGER_PORT}",
    )


def preflight(
    *,
    repository: Path,
    app_binary: Path,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    native_digest_binary: Path,
    arm: str,
    endpoint_probe: Callable[[tuple[str, int]], None] = _assert_no_listener,
) -> dict[str, object]:
    """Return a reproducible local-only preflight receipt or fail before writes.

    A clean, remote-synchronised checkout is required because a later live
    observation must be attributable to one exact revision.  This deliberately
    makes the current concurrent dirty checkout a stop condition.
    """

    repository = repository.resolve()
    if _git(repository, "branch", "--show-current").strip() != REQUIRED_BRANCH:
        raise StaticE0FeasibilityError("repository is not on the required branch")
    if _git(repository, "status", "--porcelain").strip():
        raise StaticE0FeasibilityError("repository is dirty; no local process may launch")
    revision = _git(repository, "rev-parse", "HEAD").strip()
    remote = _git(repository, "rev-parse", f"origin/{REQUIRED_BRANCH}").strip()
    if revision != remote:
        raise StaticE0FeasibilityError("HEAD differs from the required remote revision")
    plan = frozen_plan(arm=arm)
    app = _require_executable(app_binary, "hotstuff-app")
    keygen = _require_executable(keygen_binary, "hotstuff-keygen")
    tls_keygen = _require_executable(tls_keygen_binary, "hotstuff-tls-keygen")
    native_digest = _require_executable(native_digest_binary, "static-epoch0-digest")
    endpoint_probe((PINNED_MANAGER_HOST, PINNED_MANAGER_PORT))
    binaries = {
        "app": app, "keygen": keygen, "tls_keygen": tls_keygen,
        "native_digest": native_digest,
    }
    return {
        "schema_version": 1,
        "kind": SCHEMA,
        "verdict": "PREFLIGHT_OK_NO_EXECUTION",
        "revision": revision,
        "profile_id": plan.profile.profile_id,
        "profile_sha256": plan.profile.sha256,
        "arm": plan.arm,
        "replica_count": REPLICA_COUNT,
        "quorum": plan.profile.quorum,
        "tree_count": TREE_COUNT,
        "treegen_sha256": plan.treegen_sha256,
        "manager_endpoint": plan.manager_endpoint,
        "binaries": {name: str(path) for name, path in binaries.items()},
        "binary_sha256": {
            name: runtime.sha256_file(path) for name, path in binaries.items()
        },
        "limitations": [
            "No process was launched.",
            "A later execution must generate identities, prove all 31 exact "
            "active topology digests, observe reporting terminal events, "
            "observe authoritative commits, and preserve cleanup evidence.",
            "This receipt does not authorize cluster use, CPU quotas, or a thesis claim.",
        ],
    }


def write_prepared_inputs(
    *,
    plan: FeasibilityPlan,
    directory: Path,
    bls: list[Mapping[str, str]],
    tls: list[Mapping[str, str]],
    issuer: Mapping[str, str],
    run_id: str,
    source_instances: Mapping[str, str],
) -> dict[str, str]:
    """Write exact non-secret runtime configuration after a passed preflight.

    This helper remains uncalled by the CLI.  Keeping it separate makes the
    initial executable harness reviewable without turning preflight into a
    hidden process launch.
    """

    if len(bls) != REPLICA_COUNT or len(tls) != REPLICA_COUNT + 1:
        raise StaticE0FeasibilityError("identity cardinality differs from N31")
    directory = directory.resolve()
    (directory / "config").mkdir(parents=True, exist_ok=True)
    (directory / "raw").mkdir(parents=True, exist_ok=True)
    tree_path = directory / "config" / "epoch0-treegen.conf"
    runtime.write_exclusive(tree_path, plan.treegen_bytes)
    lines = [
        f"block-size = {plan.profile.block_size}",
        "nworker = 2",
        "repnworker = 1",
        "stat-period = 120",
        "pace-maker = dummy",
        "proposer = 0",
        f"fan-out = {plan.profile.fanout}",
        "piped_latency = 1",
        f"async_blocks = {plan.profile.pipeline_depth}",
        "base-timeout = 2.0",
        "prop-delay = 0.1",
        f"aggregation-timeout = {plan.profile.aggregation_timeout_s}",
        f"leader-progress-timeout = {plan.profile.leader_progress_timeout_s}",
        f"leader-activation-grace = {plan.profile.leader_activation_grace_s}",
        "client-ip = 127.0.0.1",
        "tree-generation = file",
        f"tree-generation-fpath = {tree_path}",
        f"tree-switch-period = {plan.profile.tree_switch_period_blocks}",
        "epoch-protocol-mode = adaptive_v2",
        f"epoch-change-issuer-id = {runtime.ISSUER_ID}",
        f"epoch-change-issuer-public-key = {issuer['pub']}",
        "epoch-change-minimum-activation-delay = "
        f"{plan.profile.activation_delay_blocks}",
        "epoch-change-maximum-activation-delay = "
        f"{plan.profile.activation_delay_blocks}",
        "epoch-change-maximum-block-extra-bytes = 1048576",
        "epoch-change-maximum-ancestry-blocks = 4096",
        f"epoch-manager-address = {plan.manager_endpoint}",
        f"epoch-manager-tls-cert = {tls[REPLICA_COUNT]['crt']}",
        "max-rep-msg = 1048576",
    ]
    for replica in plan.profile.replica_ids:
        lines.append(
            "replica = "
            f"127.0.0.1:{plan.profile.peer_base + replica};"
            f"{plan.profile.client_base + replica}, "
            f"{bls[replica]['pub']}, {tls[replica]['cid']}"
        )
    payload = "\n".join(lines) + "\n"
    main_path = directory / "config" / "main.conf"
    runtime.write_exclusive(main_path, payload.encode("utf-8"))
    for replica_id in plan.profile.replica_ids:
        runtime.write_exclusive(
            directory / "config" / f"replica-{replica_id}.conf",
            (
                f"privkey = {bls[replica_id]['sec']}\n"
                f"tls-privkey = {tls[replica_id]['sec']}\n"
                f"tls-cert = {tls[replica_id]['crt']}\n"
                f"idx = {replica_id}\n"
                f"structured-event-run-id = {run_id}\n"
                f"structured-event-source-instance = {source_instances[f'replica-{replica_id}']}\n"
                "structured-event-commit-observer-id = "
                f"replica-{plan.profile.authoritative_observer}\n"
                "structured-event-commit-observer-instance = "
                f"{source_instances[f'replica-{plan.profile.authoritative_observer}']}\n"
                f"structured-event-output = {directory / 'raw' / f'replica-{replica_id}.jsonl'}\n"
            ).encode(),
        )
    return {
        "treegen": str(tree_path),
        "main": str(main_path),
        "treegen_sha256": plan.treegen_sha256,
    }


def canonical_json(value: Mapping[str, object]) -> bytes:
    """Canonical CLI output, also used by tests as a receipt boundary."""

    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FATAL_EVENT_TYPES = frozenset(
    {
        "process.forced_crash_requested",
        "adaptive_v2_convergence_failure",
        "adaptive_v2_session_terminal",
        "adaptive_v3_terminal",
        "adaptive_v3_command_terminal",
    }
)


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _exact_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX_DIGEST.fullmatch(value) is not None


def _event_envelope_is_exact(
    event: Mapping[str, object], *, run_id: str, source_id: str, source_instance: str
) -> bool:
    """Check the raw native envelope before trusting any payload field.

    The gate deliberately accepts no caller-provided shorthand.  Every event
    must retain the native emitter's schema, run, source kind, logical source,
    source-instance binding, sequence, and monotonic timestamp.
    """

    return (
        event.get("event_schema_version") == 1
        and event.get("run_id") == run_id
        and event.get("source_kind") == "replica"
        and event.get("source_id") == source_id
        and event.get("source_instance") == source_instance
        and _positive_integer(event.get("source_sequence"))
        and _positive_integer(event.get("source_monotonic_ns"))
        and isinstance(event.get("event_type"), str)
        and isinstance(event.get("payload"), Mapping)
    )


def _complete_epoch_zero_cycle(
    events: list[Mapping[str, object]], *, epoch_digest: str, source_id: str
) -> int | None:
    """Return the final event index of an exact 0..20 active cycle.

    A configuration-active record outside the reviewed Epoch-0 identity is a
    provenance failure, not an ignorable unrelated observation.  The cycle
    itself must be contiguous in the activation subsequence, so duplicate or
    omitted tree IDs cannot accidentally produce a pass.
    """

    active: list[tuple[int, Mapping[str, object]]] = []
    source_replica = int(source_id.removeprefix("replica-"))
    for index, event in enumerate(events):
        if event["event_type"] != "adaptive.configuration_active":
            continue
        payload = event["payload"]
        assert isinstance(payload, Mapping)
        if (
            payload.get("epoch_number") != 0
            or payload.get("epoch_digest") != epoch_digest
            or payload.get("observer_replica") != source_replica
            or payload.get("global_quorum") != 21
            or not isinstance(payload.get("tree_id"), int)
            or isinstance(payload.get("tree_id"), bool)
            or payload["tree_id"] not in range(TREE_COUNT)
        ):
            return None
        active.append((index, payload))
    tree_ids = [payload["tree_id"] for _, payload in active]
    expected = list(range(TREE_COUNT))
    for offset in range(len(tree_ids) - TREE_COUNT + 1):
        if tree_ids[offset : offset + TREE_COUNT] == expected:
            return active[offset + TREE_COUNT - 1][0]
    return None


def _valid_observer_commit(
    event: Mapping[str, object], *, epoch_digest: str
) -> bool:
    payload = event["payload"]
    assert isinstance(payload, Mapping)
    proof = payload.get("decision_proof")
    return (
        event["event_type"] == "block.committed"
        and payload.get("designated_observer") is True
        and _positive_integer(payload.get("block_height"))
        and _exact_digest(payload.get("block_hash"))
        and _positive_integer(payload.get("transaction_count"))
        and _nonnegative_integer(payload.get("commit_batch_index"))
        and isinstance(proof, Mapping)
        and proof.get("epoch_number") == 0
        and proof.get("epoch_digest") == epoch_digest
        and isinstance(proof.get("tree_id"), int)
        and not isinstance(proof.get("tree_id"), bool)
        and proof["tree_id"] in range(TREE_COUNT)
        and proof.get("block_hash") == payload.get("block_hash")
    )


def event_gate(
    streams: Mapping[str, list[Mapping[str, object]]],
    *,
    observer: int,
    run_id: str | None = None,
    source_instances: Mapping[str, str] | None = None,
    epoch_digest: str | None = None,
    expected_terminal_reason: str | None = None,
) -> tuple[bool, str]:
    """Validate raw native evidence for one exact W16 local feasibility run.

    This is deliberately source-bound: callers must supply the immutable run
    ID, every expected source instance, the native Epoch-0 digest, and the
    reviewed terminal reason.  Missing bindings fail closed.  It validates
    only an event stream; :func:`execute_once` remains disabled and this gate
    does not authorize cluster execution or a thesis claim.
    """

    expected = {f"replica-{replica}" for replica in range(REPLICA_COUNT)}
    if observer not in range(REPLICA_COUNT):
        return False, "authoritative observer is outside N31"
    if not isinstance(run_id, str) or not run_id:
        return False, "missing exact structured-event run binding"
    if source_instances is None or set(source_instances) != expected:
        return False, "missing exact source-instance bindings for N31"
    if any(not isinstance(source_instances[source], str) or not source_instances[source]
           for source in expected):
        return False, "source-instance bindings must be non-empty strings"
    if not _exact_digest(epoch_digest):
        return False, "missing exact native Epoch-0 digest"
    if not isinstance(expected_terminal_reason, str) or not expected_terminal_reason:
        return False, "missing exact expected reporting-terminal reason"
    if set(streams) != expected:
        return False, "raw streams do not cover exactly 31 replicas"

    observer_terminal_sequence: int | None = None
    observer_cycle_final_sequence: int | None = None
    for source in sorted(expected):
        events = streams[source]
        if not isinstance(events, list) or not events:
            return False, f"{source} raw stream is empty or malformed"
        previous_sequence = 0
        previous_monotonic = 0
        for event in events:
            if not isinstance(event, Mapping) or not _event_envelope_is_exact(
                event,
                run_id=run_id,
                source_id=source,
                source_instance=source_instances[source],
            ):
                return False, f"{source} has an unbound or malformed native envelope"
            sequence = event["source_sequence"]
            monotonic = event["source_monotonic_ns"]
            assert isinstance(sequence, int) and isinstance(monotonic, int)
            if sequence != previous_sequence + 1:
                return False, f"{source} source sequence is gapped or reordered"
            if monotonic < previous_monotonic:
                return False, f"{source} source monotonic clock regressed"
            previous_sequence = sequence
            previous_monotonic = monotonic
            if event["event_type"] in _FATAL_EVENT_TYPES:
                return False, f"{source} reports terminal or convergence failure"
            if event["event_type"] == "epoch.command_committed":
                return False, "unexpected successor command"

        ready = [index for index, event in enumerate(events)
                 if event["event_type"] == "process.ready"]
        if len(ready) != 1 or ready[0] != 0:
            return False, f"{source} lacks exactly one process.ready"
        cycle_final_index = _complete_epoch_zero_cycle(
            events, epoch_digest=epoch_digest, source_id=source
        )
        if cycle_final_index is None:
            return False, f"{source} lacks an exact ordered Epoch-0 activation cycle"
        terminals = [
            (index, event)
            for index, event in enumerate(events)
            if event["event_type"] == "adaptive_v2_reporting_terminal"
        ]
        if len(terminals) != 1:
            return False, f"{source} lacks exactly one reporting-terminal event"
        terminal_index, terminal = terminals[0]
        terminal_payload = terminal["payload"]
        assert isinstance(terminal_payload, Mapping)
        if (
            terminal_payload.get("reason") != expected_terminal_reason
            or not _positive_integer(terminal_payload.get("terminal_monotonic_ns"))
        ):
            return False, f"{source} reporting-terminal evidence is not the reviewed outcome"
        if source == f"replica-{observer}":
            observer_terminal_sequence = terminal["source_sequence"]
            observer_cycle_final_sequence = events[cycle_final_index]["source_sequence"]

    assert observer_terminal_sequence is not None
    assert observer_cycle_final_sequence is not None
    observer_events = streams[f"replica-{observer}"]
    post_boundary = max(observer_terminal_sequence, observer_cycle_final_sequence)
    commits = [event for event in observer_events
               if event["source_sequence"] > post_boundary
               and _valid_observer_commit(event, epoch_digest=epoch_digest)]
    for before, after in zip(commits, commits[1:]):
        left, right = before["payload"], after["payload"]
        assert isinstance(left, Mapping) and isinstance(right, Mapping)
        if (
            right["block_height"] == left["block_height"] + 1
            and right.get("parent_hash") == left["block_hash"]
            and right["block_hash"] != left["block_hash"]
        ):
            return True, "PASS: exact source-bound Epoch-0 cycle and post-terminal commit chain"
    return False, "no valid designated-observer commit chain after cycle and reporting terminal"


def execute_once(*, plan: FeasibilityPlan, directory: Path, app_binary: Path,
                 keygen_binary: Path, tls_keygen_binary: Path,
                 hard_timeout_s: float = 180.0) -> dict[str, object]:
    """Disabled prototype; no local or cluster launch is authorized here.

    The draft below has independently confirmed provenance, timeout, event
    authority, and cleanup defects. Keep it unreachable until those are
    corrected and re-reviewed; preflight alone cannot authorize execution.
    """
    raise StaticE0FeasibilityError(
        "W16 execute_once is disabled pending exact native digest, raw-event, "
        "provenance, hard-timeout, and cleanup review"
    )
    if hard_timeout_s <= 0 or directory.exists():
        raise StaticE0FeasibilityError("fresh output directory and positive hard timeout required")
    _assert_no_listener((PINNED_MANAGER_HOST, PINNED_MANAGER_PORT))
    directory.mkdir(parents=True, mode=0o700)
    (directory / "config").mkdir(mode=0o700)
    (directory / "logs").mkdir(mode=0o700)
    run_id = directory.name + "-" + uuid.uuid4().hex
    source_instances = {f"replica-{i}": f"{run_id}-replica-{i}" for i in range(REPLICA_COUNT)}
    registry = ProcessRegistry()
    logs = []
    records = []
    started = time.monotonic_ns()
    result: dict[str, object] = {"schema_version": 1, "run_id": run_id,
        "started_monotonic_ns": started, "attempts": 1, "retries": 0,
        "verdict": "ABORT", "error": None, "cleanup": []}
    try:
        bls, tls, issuer = runtime.generate_identities(plan.profile, keygen_binary=keygen_binary,
            tls_keygen_binary=tls_keygen_binary, config_directory=directory / "config")
        paths = write_prepared_inputs(plan=plan, directory=directory, bls=bls, tls=tls,
            issuer=issuer, run_id=run_id, source_instances=source_instances)
        result["treegen_sha256"] = paths["treegen_sha256"]
        for replica in plan.profile.replica_ids:
            record, log = runtime.spawn_owned_process(registry, name=f"replica-{replica}",
                replica_id=replica, command=(str(app_binary.resolve()), "--conf", paths["main"],
                "--conf", str(directory / "config" / f"replica-{replica}.conf")),
                log_path=directory / "logs" / f"replica-{replica}.log", working_directory=directory)
            records.append(record); logs.append(log)
        deadline = started + int(hard_timeout_s * 1_000_000_000)
        while time.monotonic_ns() < deadline:
            if any(record.process.poll() is not None for record in records):
                raise StaticE0FeasibilityError("replica exited before owned cleanup")
            streams = {f"replica-{i}": runtime.read_jsonl(directory / "raw" / f"replica-{i}.jsonl", allow_partial=True) for i in range(REPLICA_COUNT)}
            passed, detail = event_gate(streams, observer=plan.profile.authoritative_observer)
            if passed:
                result["verdict"] = "PASS"; result["detail"] = detail; break
            time.sleep(0.1)
        else:
            result["error"] = "hard timeout before all feasibility gates"
    except BaseException as error:
        result["error"] = str(error) or type(error).__name__
    finally:
        try:
            result["cleanup"] = [asdict(outcome) for outcome in registry.cleanup(timeout_s=5.0)]
        except BaseException as error:
            result["cleanup_error"] = str(error) or type(error).__name__
        for log in logs:
            log.close()
        result["ended_monotonic_ns"] = time.monotonic_ns()
        runtime.write_json_exclusive(directory / ("feasibility-receipt.json" if result["verdict"] == "PASS" else "feasibility-abort.json"), result)
    return result
