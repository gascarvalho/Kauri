"""Fail-closed raw-evidence validator scaffold for the prospective W16 study.

This module checks the frozen topology and native event shape only.  The W16
runner does not yet emit byte-bound process, cgroup, cleanup, or inventory
receipts, so this module is intentionally incapable of returning ``PASS``.
It deliberately calculates no throughput ratio and cannot turn a fixture or a
partial launch into a thesis result.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .static_topology_n31 import (
    REPLICA_COUNT,
    SLOW_REPLICA_IDS,
    TREE_COUNT,
    build_schedule,
    render_treegen_bytes,
    source_config_identity,
)


SCHEMA = "kauri-w16-static-n31-raw-evidence-v1"
RESULT_SCHEMA = "kauri-w16-static-n31-raw-validation-v1"
AUTHORITATIVE_OBSERVER = 2
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPLICA_KEYS = frozenset({"replica_id", "pid", "process_group_id", "source_instance", "cpu"})
_CPU_KEYS = frozenset({"quota_percent", "cgroup_path", "samples"})
_CPU_SAMPLE_KEYS = frozenset(
    {"source_monotonic_ns", "usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec"}
)
_CLEANUP_KEYS = frozenset({"replica_id", "process_group_id", "terminated", "cgroup_removed"})
_ARTIFACT_KEYS = frozenset({"path", "sha256", "bytes"})
_SUCCESSOR_EVENTS = frozenset({"epoch.command_committed", "adaptive.successor_activated"})


class StaticResourceValidationError(ValueError):
    """Raw W16 evidence is malformed or does not prove a required gate."""


def _fail(message: str) -> None:
    raise StaticResourceValidationError(message)


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{field} must be an object")
    return value


def _integer(value: object, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{field} must be an integer")
    if positive and value <= 0:
        _fail(f"{field} must be positive")
    return value


def _timestamp(value: object, field: str) -> int:
    return _integer(value, field, positive=True)


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(f"{field} must be a lowercase SHA-256")
    return value


def _event_time(event: Mapping[str, Any], source: str) -> int:
    return _timestamp(event.get("source_monotonic_ns"), f"{source} event timestamp")


def _event_payload(event: Mapping[str, Any], source: str) -> Mapping[str, Any]:
    return _mapping(event.get("payload"), f"{source} event payload")


def _has_cycle(tree_ids: Sequence[int]) -> bool:
    required = list(range(TREE_COUNT)) + [0]
    return any(tree_ids[index : index + len(required)] == required
               for index in range(len(tree_ids) - len(required) + 1))


def _expected_quota(quota_mode: str, replica_id: int) -> int:
    if quota_mode == "homogeneous-100":
        return 100
    if quota_mode == "heterogeneous-25-100":
        return 25 if replica_id in SLOW_REPLICA_IDS else 100
    _fail("measurement quota_mode must be homogeneous-100 or heterogeneous-25-100")


def _validate_identity_and_topology(raw: Mapping[str, Any]) -> tuple[str, str, bytes]:
    arm = raw.get("arm")
    if arm not in {"slow-roots", "fast-roots"}:
        _fail("arm must be slow-roots or fast-roots")
    identity = _mapping(raw.get("source_config_identity"), "source_config_identity")
    if set(identity) != {"schema", "source_revision", "schedule_sha256", "treegen_sha256", "source_config_sha256"}:
        _fail("source_config_identity fields differ from the topology fixture")
    revision = identity.get("source_revision")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        _fail("source revision must be a full lowercase Git SHA-1")
    expected = source_config_identity(build_schedule(arm), revision)
    if dict(identity) != expected:
        _fail("source_config_identity does not bind the reviewed arm and source revision")
    encoded = raw.get("treegen_base64")
    if not isinstance(encoded, str):
        _fail("treegen_base64 must be a string")
    try:
        treegen = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as error:
        raise StaticResourceValidationError("treegen_base64 is not strict base64") from error
    expected_treegen = render_treegen_bytes(build_schedule(arm))
    if treegen != expected_treegen:
        _fail("treegen bytes differ from the reviewed deterministic topology")
    if hashlib.sha256(treegen).hexdigest() != identity["treegen_sha256"]:
        _fail("treegen bytes do not match their source-bound identity")
    return arm, revision, treegen


def _validate_replicas(raw: Mapping[str, Any], start_ns: int, end_ns: int, quota_mode: str) -> tuple[dict[str, Mapping[str, Any]], int]:
    replicas = raw.get("replicas")
    if not isinstance(replicas, list) or len(replicas) != REPLICA_COUNT:
        _fail("replicas must contain exactly 31 process identities")
    by_source: dict[str, Mapping[str, Any]] = {}
    slow_throttle_delta = 0
    for item in replicas:
        replica = _mapping(item, "replica")
        if set(replica) != _REPLICA_KEYS:
            _fail("replica fields differ from the raw-evidence schema")
        replica_id = _integer(replica.get("replica_id"), "replica_id")
        if replica_id not in range(REPLICA_COUNT):
            _fail("replica_id is outside N31")
        source_instance = replica.get("source_instance")
        if not isinstance(source_instance, str) or not source_instance:
            _fail("replica source_instance must be non-empty")
        source = f"replica-{replica_id}"
        if source in by_source:
            _fail("replica identities are duplicated")
        _integer(replica.get("pid"), "replica pid", positive=True)
        _integer(replica.get("process_group_id"), "replica process_group_id", positive=True)
        cpu = _mapping(replica.get("cpu"), "replica cpu")
        if set(cpu) != _CPU_KEYS:
            _fail("cpu fields differ from the raw-evidence schema")
        if cpu.get("quota_percent") != _expected_quota(quota_mode, replica_id):
            _fail(f"{source} effective quota differs from the frozen assignment")
        if not isinstance(cpu.get("cgroup_path"), str) or not str(cpu["cgroup_path"]).startswith("/"):
            _fail(f"{source} cgroup_path is absent")
        samples = cpu.get("samples")
        if not isinstance(samples, list) or len(samples) < 2:
            _fail(f"{source} lacks timestamped cpu.stat coverage")
        timestamps: list[int] = []
        throttled: list[int] = []
        for sample in samples:
            sample_map = _mapping(sample, f"{source} cpu sample")
            if set(sample_map) != _CPU_SAMPLE_KEYS:
                _fail(f"{source} cpu.stat fields differ")
            timestamps.append(_timestamp(sample_map.get("source_monotonic_ns"), f"{source} cpu sample timestamp"))
            for field in _CPU_SAMPLE_KEYS - {"source_monotonic_ns"}:
                _integer(sample_map.get(field), f"{source} cpu {field}")
            throttled.append(_integer(sample_map.get("throttled_usec"), f"{source} throttled_usec"))
        if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
            _fail(f"{source} cpu.stat timestamps are not strictly increasing")
        if timestamps[0] > start_ns or timestamps[-1] < end_ns:
            _fail(f"{source} cpu.stat does not cover the complete measurement window")
        if throttled != sorted(throttled):
            _fail(f"{source} throttled_usec decreases")
        if replica_id in SLOW_REPLICA_IDS:
            slow_throttle_delta += throttled[-1] - throttled[0]
        by_source[source] = replica
    if set(by_source) != {f"replica-{item}" for item in range(REPLICA_COUNT)}:
        _fail("replica identities do not cover exactly N31")
    return by_source, slow_throttle_delta


def _validate_events(raw: Mapping[str, Any], replicas: Mapping[str, Mapping[str, Any]], start_ns: int, end_ns: int) -> int:
    streams = _mapping(raw.get("event_streams"), "event_streams")
    if set(streams) != set(replicas):
        _fail("event_streams must cover exactly the owned 31 replicas")
    active_digests: set[str] = set()
    terminal_times: list[int] = []
    observer_commits: list[tuple[int, Mapping[str, Any]]] = []
    for source, raw_events in streams.items():
        if not isinstance(raw_events, list) or not raw_events:
            _fail(f"{source} event stream is absent")
        expected_instance = replicas[source]["source_instance"]
        ready_count = 0
        tree_ids: list[int] = []
        for raw_event in raw_events:
            event = _mapping(raw_event, f"{source} event")
            if event.get("source_instance") != expected_instance:
                _fail(f"{source} event is not bound to its owned process identity")
            event_type = event.get("event_type")
            if not isinstance(event_type, str):
                _fail(f"{source} event_type is absent")
            timestamp = _event_time(event, source)
            if event_type in _SUCCESSOR_EVENTS:
                _fail("unexpected successor event")
            if event_type == "process.ready":
                ready_count += 1
            elif event_type == "adaptive_v2_reporting_terminal":
                terminal_times.append(timestamp)
            elif event_type == "adaptive.configuration_active":
                payload = _event_payload(event, source)
                tree_id = _integer(payload.get("tree_id"), f"{source} active tree_id")
                if tree_id not in range(TREE_COUNT):
                    _fail(f"{source} active tree ID is outside the 21-tree schedule")
                digest = _sha(payload.get("epoch_digest"), f"{source} active epoch digest")
                tree_ids.append(tree_id)
                active_digests.add(digest)
            elif source == f"replica-{AUTHORITATIVE_OBSERVER}" and event_type == "block.committed":
                observer_commits.append((timestamp, event))
        if ready_count != 1:
            _fail(f"{source} lacks exactly one process.ready")
        if not _has_cycle(tree_ids):
            _fail(f"{source} lacks a complete tree-0-to-tree-0 21-tree cycle")
    if len(active_digests) != 1:
        _fail("active Epoch-0 digest is absent or differs across replicas")
    if not terminal_times:
        _fail("reporting-terminal evidence is absent")
    terminal = max(terminal_times)
    commits = [item for item in observer_commits if start_ns <= item[0] <= end_ns and item[0] > terminal]
    if len(commits) < 2:
        _fail("measurement window lacks two authoritative commits after reporting terminal")
    commits.sort(key=lambda item: item[0])
    _validate_commit_chain(commits, next(iter(active_digests)))
    return len(commits)


def _validate_commit_chain(commits: Sequence[tuple[int, Mapping[str, Any]]], epoch_digest: str) -> None:
    previous_height: int | None = None
    previous_hash: str | None = None
    hashes: set[str] = set()
    for _timestamp_ns, event in commits:
        payload = _event_payload(event, "authoritative commit")
        height = _integer(
            payload.get("block_height"), "authoritative commit block_height", positive=True
        )
        block_hash = _sha(payload.get("block_hash"), "authoritative commit block_hash")
        parent_hash = _sha(payload.get("parent_hash"), "authoritative commit parent_hash")
        if payload.get("designated_observer") is not True:
            _fail("block.committed is not marked as the designated observer")
        decision_proof = _mapping(payload.get("decision_proof"), "authoritative decision_proof")
        configuration = _mapping(
            decision_proof.get("configuration"), "authoritative decision_proof configuration"
        )
        if configuration.get("epoch_digest") != epoch_digest:
            _fail("authoritative commit belongs to a different epoch digest")
        if decision_proof.get("block_hash") != block_hash:
            _fail("authoritative decision proof does not bind its committed block hash")
        if block_hash in hashes:
            _fail("authoritative commit chain has a duplicate block hash")
        if previous_height is not None and (height != previous_height + 1 or parent_hash != previous_hash):
            _fail("authoritative commits do not form one continuous chain")
        hashes.add(block_hash)
        previous_height, previous_hash = height, block_hash


def _validate_cleanup_and_inventory(raw: Mapping[str, Any], replicas: Mapping[str, Mapping[str, Any]]) -> None:
    cleanup = raw.get("cleanup")
    if not isinstance(cleanup, list) or len(cleanup) != REPLICA_COUNT:
        _fail("cleanup must contain one receipt per owned replica")
    observed: set[int] = set()
    for item in cleanup:
        receipt = _mapping(item, "cleanup receipt")
        if set(receipt) != _CLEANUP_KEYS:
            _fail("cleanup receipt fields differ")
        replica_id = _integer(receipt.get("replica_id"), "cleanup replica_id")
        source = f"replica-{replica_id}"
        if source not in replicas or replica_id in observed:
            _fail("cleanup receipt is missing or duplicated")
        if receipt.get("process_group_id") != replicas[source]["process_group_id"]:
            _fail("cleanup receipt does not bind the owned process group")
        if receipt.get("terminated") is not True or receipt.get("cgroup_removed") is not True:
            _fail("owned process or cgroup cleanup is unproven")
        observed.add(replica_id)
    inventory = raw.get("artifact_inventory")
    if not isinstance(inventory, list):
        _fail("artifact_inventory must be a list")
    paths: set[str] = set()
    for item in inventory:
        artifact = _mapping(item, "artifact inventory item")
        if set(artifact) != _ARTIFACT_KEYS:
            _fail("artifact inventory fields differ")
        path = artifact.get("path")
        if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
            _fail("artifact inventory path is not a safe relative path")
        _sha(artifact.get("sha256"), "artifact sha256")
        _integer(artifact.get("bytes"), "artifact bytes", positive=True)
        if path in paths:
            _fail("artifact inventory path is duplicated")
        paths.add(path)
    required = {"config/epoch0-treegen.conf"}
    required.update(f"raw/replica-{replica}.jsonl" for replica in range(REPLICA_COUNT))
    if not required.issubset(paths):
        _fail("artifact inventory lacks the tree bytes or a raw replica stream")


def validate_raw_evidence(raw: Mapping[str, object]) -> dict[str, object]:
    """Validate one raw arm, but never promote this scaffold to PASS.

    Any malformed, absent, or contradictory evidence returns ``INCOMPLETE``.
    The verdict means only that this arm's raw structural evidence is complete;
    it is explicitly not a throughput or adaptive-reputation conclusion.
    """

    try:
        document = _mapping(raw, "raw evidence")
        expected_keys = {
            "schema", "arm", "source_config_identity", "treegen_base64", "replicas",
            "event_streams", "measurement", "cleanup", "artifact_inventory",
        }
        if set(document) != expected_keys or document.get("schema") != SCHEMA:
            _fail("raw evidence schema or fields differ")
        measurement = _mapping(document.get("measurement"), "measurement")
        if set(measurement) != {"start_monotonic_ns", "end_monotonic_ns", "quota_mode"}:
            _fail("measurement fields differ")
        start_ns = _timestamp(measurement.get("start_monotonic_ns"), "measurement start")
        end_ns = _timestamp(measurement.get("end_monotonic_ns"), "measurement end")
        if end_ns <= start_ns:
            _fail("measurement end must follow measurement start")
        quota_mode = measurement.get("quota_mode")
        if not isinstance(quota_mode, str):
            _fail("measurement quota_mode is absent")
        arm, revision, _treegen = _validate_identity_and_topology(document)
        replicas, slow_throttle_delta = _validate_replicas(document, start_ns, end_ns, quota_mode)
        if quota_mode == "heterogeneous-25-100" and arm == "slow-roots" and slow_throttle_delta <= 0:
            _fail("root-exposed slow cohort lacks observed CPU throttling")
        commit_count = _validate_events(document, replicas, start_ns, end_ns)
        _validate_cleanup_and_inventory(document, replicas)
        return {
            "schema": RESULT_SCHEMA,
            "verdict": "INCOMPLETE",
            "arm": arm,
            "source_revision": revision,
            "quota_mode": quota_mode,
            "authoritative_commit_count": commit_count,
            "slow_cohort_throttled_usec_delta": slow_throttle_delta,
            "reason": (
                "PASS disabled: W16 has no producer-bound raw-byte receipts for "
                "process ownership, cpu.stat, cleanup, or artifact inventory."
            ),
            "limitation": "No throughput effect or adaptive claim.",
        }
    except StaticResourceValidationError as error:
        return {"schema": RESULT_SCHEMA, "verdict": "INCOMPLETE", "reason": str(error)}
