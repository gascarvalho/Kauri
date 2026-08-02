"""Fail-closed helpers for the frozen N=3f+1 fault evaluation profile."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

from .faults import FaultPlan, ReplicaGroupSigkill, ScenarioContext


class ProfiledFaultEvaluationError(ValueError):
    """The frozen profile or its synthetic evidence is not admissible."""


@dataclass(frozen=True, slots=True)
class ProfileFault:
    fault_id: str
    kind: str
    replica_id: int
    tree_id: int


@dataclass(frozen=True, slots=True)
class FrozenProfile:
    profile_id: str
    profile_sha256: str
    replica_ids: tuple[int, ...]
    fault_threshold: int
    quorum: int
    fanout: int
    pipeline_depth: int
    epoch0_roots: tuple[int, ...]
    epoch0_members_breadth_first: tuple[int, ...]
    authoritative_observer: int
    snapshot_seed: int
    peer_base: int
    client_base: int
    manager_port: int
    fault: ProfileFault
    crash_subtree: tuple[int, ...]
    attempt_count: int
    retry_failed_attempts: bool
    require_successor_activation: bool
    block_size: int
    tree_switch_period_blocks: int
    bucket_width_s: int
    baseline_bucket_count: int
    post_bucket_count: int
    aggregation_timeout_s: float
    leader_progress_timeout_s: float
    leader_activation_grace_s: float
    activation_delay_blocks: int
    maximum_stall_s: float
    startup_timeout_s: float
    hard_timeout_s: float
    crash_confirm_timeout_s: float


def _error(message: str) -> None:
    raise ProfiledFaultEvaluationError(message)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _error(f"{name} must be an integer")
    return value


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _error(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        _error(f"{name} must be positive and finite")
    return result


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _error(f"{name} must be an object")
    return value


def _ids(value: object, name: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(x, bool) or not isinstance(x, int) for x in value
    ):
        _error(f"{name} must be an integer list")
    return tuple(value)


def breadth_first_subtree_at(
    members_breadth_first: Sequence[int], fanout: int, position: int
) -> tuple[int, ...]:
    """Return a breadth-first subtree, preserving canonical input order."""
    if fanout < 1 or position < 0 or position >= len(members_breadth_first):
        _error("fanout and subtree position must be valid")
    positions = [position]
    result: list[int] = []
    while positions:
        current = positions.pop(0)
        result.append(members_breadth_first[current])
        first = current * fanout + 1
        positions.extend(range(first, min(first + fanout, len(members_breadth_first))))
    return tuple(result)


def load_frozen_profile(path: Path) -> FrozenProfile:
    raw_bytes = Path(path).read_bytes()
    try:
        raw = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfiledFaultEvaluationError("profile must be valid JSON") from exc
    doc = _mapping(raw, "profile")
    if doc.get("schema_version") != 1 or doc.get("frozen") is not True:
        _error("profile must be frozen schema version 1")
    profile_id = doc.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id:
        _error("profile id must be non-empty")
    replica_ids = _ids(doc.get("replica_ids"), "replica ids")
    count = len(replica_ids)
    if count < 4 or count != 3 * ((count - 1) // 3) + 1:
        _error("replica membership must satisfy N=3f+1")
    if replica_ids != tuple(range(count)):
        _error("replica ids must be contiguous canonical membership")
    threshold = _integer(doc.get("fault_threshold"), "fault threshold")
    expected_f = (count - 1) // 3
    if threshold != expected_f:
        _error("fault threshold must equal declared N=3f+1 threshold")
    quorum = _integer(doc.get("quorum"), "quorum")
    if quorum != 2 * threshold + 1:
        _error("quorum must equal 2f+1")
    fanout = _integer(doc.get("fanout"), "fanout")
    if fanout < 1:
        _error("fanout must be positive")
    pipeline_depth = _integer(doc.get("pipeline_depth"), "pipeline depth")
    if pipeline_depth < 1:
        _error("pipeline depth must be positive")
    roots = _ids(doc.get("epoch0_roots"), "epoch0 roots")
    members = _ids(
        doc.get("epoch0_members_breadth_first"), "topology breadth-first membership"
    )
    if (
        roots != replica_ids
        or len(members) != count
        or set(members) != set(replica_ids)
    ):
        _error(
            "topology roots and breadth-first membership must be permutations of membership"
        )
    fault_doc = _mapping(doc.get("fault"), "fault")
    fault = ProfileFault(
        fault_id=str(fault_doc.get("fault_id", "")),
        kind=str(fault_doc.get("kind", "")),
        replica_id=_integer(fault_doc.get("replica_id"), "fault replica id"),
        tree_id=_integer(fault_doc.get("tree_id"), "fault tree id"),
    )
    if fault.kind != "replica_group_sigkill" or not fault.fault_id:
        _error("fault must be one replica_group_sigkill")
    if fault.tree_id not in replica_ids or fault.replica_id not in replica_ids:
        _error("fault target and tree must be in membership")
    root_position = replica_ids.index(fault.tree_id)
    expected_members = replica_ids[root_position:] + replica_ids[:root_position]
    if members != expected_members:
        _error(
            "fault topology breadth-first membership must be the exact "
            "cyclic rotation rooted at the declared tree"
        )
    position = members.index(fault.replica_id)
    if position == 0 or position * fanout + 1 >= count:
        _error("crash target must be an internal non-root, non-leaf replica")
    subtree = breadth_first_subtree_at(members, fanout, position)
    if count - len(subtree) < quorum:
        _error("crash branch must leave a quorum outside the subtree")
    ports = _mapping(doc.get("ports"), "ports")
    peer_base = _integer(ports.get("peer_base"), "peer port base")
    client_base = _integer(ports.get("client_base"), "client port base")
    manager_port = _integer(ports.get("manager"), "manager port")
    if not all(
        1 <= port <= 65535
        for port in (
            peer_base,
            peer_base + count - 1,
            client_base,
            client_base + count - 1,
            manager_port,
        )
    ):
        _error("ports must be within the TCP port range")
    if (
        len(
            set(range(peer_base, peer_base + count))
            | set(range(client_base, client_base + count))
            | {manager_port}
        )
        != 2 * count + 1
    ):
        _error("ports must be unique")
    attempts = _integer(doc.get("attempt_count"), "attempt count")
    if (
        attempts != 1
        or doc.get("retry_failed_attempts") is not False
        or doc.get("require_successor_activation") is not False
    ):
        _error("profile permits one attempt with no retry or successor activation")
    observer = _integer(doc.get("authoritative_observer"), "authoritative observer")
    if observer not in replica_ids or observer == fault.replica_id:
        _error("authoritative observer must be a non-crashed replica member")
    snapshot_seed = _integer(doc.get("snapshot_seed"), "snapshot seed")
    if not 0 <= snapshot_seed <= (1 << 64) - 1:
        _error("snapshot seed is outside the unsigned 64-bit range")

    block_size = _integer(doc.get("block_size", 1000), "block size")
    switch_period = _integer(
        doc.get("tree_switch_period_blocks", 1),
        "tree switch period",
    )
    bucket_width = _integer(doc.get("bucket_width_s", 5), "bucket width")
    baseline_buckets = _integer(
        doc.get("baseline_bucket_count", 6), "baseline bucket count"
    )
    post_buckets = _integer(doc.get("post_bucket_count", 6), "post bucket count")
    activation_delay = _integer(
        doc.get("activation_delay_blocks", 5), "activation delay blocks"
    )
    if (
        min(
            block_size,
            switch_period,
            bucket_width,
            baseline_buckets,
            post_buckets,
            activation_delay,
        )
        < 1
    ):
        _error("runtime sizes, buckets, and delays must be positive")
    aggregation_timeout = _positive_number(
        doc.get("aggregation_timeout_s", 0.5), "aggregation timeout"
    )
    leader_timeout = _positive_number(
        doc.get("leader_progress_timeout_s", 5.0),
        "leader progress timeout",
    )
    activation_grace = _positive_number(
        doc.get("leader_activation_grace_s", 1.0),
        "leader activation grace",
    )
    maximum_stall = _positive_number(doc.get("maximum_stall_s", 10.0), "maximum stall")
    startup_timeout = _positive_number(
        doc.get("startup_timeout_s", 120.0), "startup timeout"
    )
    hard_timeout = _positive_number(doc.get("hard_timeout_s", 360.0), "hard timeout")
    crash_timeout = _positive_number(
        doc.get("crash_confirm_timeout_s", 5.0),
        "crash confirmation timeout",
    )
    if hard_timeout <= bucket_width * (baseline_buckets + post_buckets):
        _error("hard timeout must exceed both measurement windows")
    if leader_timeout <= aggregation_timeout:
        _error("leader progress timeout must exceed aggregation timeout")
    if activation_grace >= leader_timeout:
        _error("leader activation grace must be shorter than leader timeout")
    return FrozenProfile(
        profile_id,
        hashlib.sha256(raw_bytes).hexdigest(),
        replica_ids,
        threshold,
        quorum,
        fanout,
        pipeline_depth,
        roots,
        members,
        observer,
        snapshot_seed,
        peer_base,
        client_base,
        manager_port,
        fault,
        subtree,
        attempts,
        False,
        False,
        block_size,
        switch_period,
        bucket_width,
        baseline_buckets,
        post_buckets,
        aggregation_timeout,
        leader_timeout,
        activation_grace,
        activation_delay,
        maximum_stall,
        startup_timeout,
        hard_timeout,
        crash_timeout,
    )


def required_ports(profile: FrozenProfile) -> tuple[int, ...]:
    return (
        tuple(range(profile.peer_base, profile.peer_base + len(profile.replica_ids)))
        + tuple(
            range(profile.client_base, profile.client_base + len(profile.replica_ids))
        )
        + (profile.manager_port,)
    )


def identity_generation_commands(
    profile: FrozenProfile, *, keygen_binary: Path, tls_keygen_binary: Path
) -> dict[str, tuple[str, ...]]:
    count = str(len(profile.replica_ids))
    return {
        "bls": (str(keygen_binary), "--num", count, "--algo", "bls"),
        "tls": (str(tls_keygen_binary), "--num", str(len(profile.replica_ids) + 1)),
        "issuer": (str(keygen_binary), "--num", "1", "--algo", "secp256k1"),
    }


def initial_epoch_input(profile: FrozenProfile) -> dict[str, object]:
    ids = list(profile.replica_ids)
    return {
        "schema_version": 1,
        "replica_count": len(ids),
        "membership": ids,
        "fault_threshold": profile.fault_threshold,
        "quorum": profile.quorum,
        "epoch0_trees": [
            {
                "tree_id": root,
                "root_id": root,
                "fanout": profile.fanout,
                "pipeline_depth": profile.pipeline_depth,
                "members_breadth_first": ids[index:] + ids[:index],
                "wait_exempt": [],
            }
            for index, root in enumerate(ids)
        ],
    }


def replica_argvs(
    profile: FrozenProfile, *, app_binary: Path, config_directory: Path
) -> tuple[tuple[str, ...], ...]:
    return tuple(
        (
            str(app_binary),
            "--conf",
            str(config_directory / "main.conf"),
            "--conf",
            str(config_directory / f"replica-{rid}.conf"),
        )
        for rid in profile.replica_ids
    )


def build_fault_plan(profile: FrozenProfile) -> FaultPlan:
    try:
        return FaultPlan(
            ScenarioContext(
                profile.replica_ids, profile.quorum, profile.fault_threshold, 1
            ),
            profile.snapshot_seed,
            (ReplicaGroupSigkill(profile.fault.fault_id, profile.fault.replica_id),),
        )
    except ValueError as exc:
        raise ProfiledFaultEvaluationError(str(exc)) from exc


def postfault_witnesses(profile: FrozenProfile) -> tuple[int, ...]:
    """Return the frozen lowest-ID Q-set outside the exposed crash branch."""
    outside = tuple(
        replica
        for replica in profile.replica_ids
        if replica not in frozenset(profile.crash_subtree)
    )
    if len(outside) < profile.quorum:
        _error("crash subtree leaves no fixed quorum witness set")
    witnesses = outside[: profile.quorum]
    if profile.authoritative_observer not in witnesses:
        _error("authoritative observer must belong to the fixed witness set")
    return witnesses


def validate_synthetic_run(
    profile: FrozenProfile,
    *,
    manifest: Mapping[str, object],
    fault_journal: Sequence[Mapping[str, object]],
    streams: Mapping[str, Sequence[Mapping[str, object]]],
    process_exits: Sequence[Mapping[str, object]],
    crash_request_ns: int | None = None,
) -> dict[str, object]:
    profile_info = _mapping(manifest.get("profile"), "manifest profile")
    runtime = _mapping(manifest.get("runtime"), "manifest runtime")
    if (
        profile_info.get("profile_id") != profile.profile_id
        or profile_info.get("sha256") != profile.profile_sha256
    ):
        _error("profile sha does not match frozen profile")
    if manifest.get("attempt") != 1 or profile.attempt_count != 1:
        _error("attempt/retry policy violated")
    expected_runtime = {
        "replica_ids": list(profile.replica_ids),
        "fault_threshold": profile.fault_threshold,
        "quorum": profile.quorum,
        "fanout": profile.fanout,
        "pipeline_depth": profile.pipeline_depth,
    }
    if dict(runtime) != expected_runtime:
        _error("runtime does not match frozen profile")
    plan = build_fault_plan(profile)
    starts = [event for event in fault_journal if event.get("lifecycle") == "started"]
    terminals = [
        event for event in fault_journal if event.get("lifecycle") == "terminal"
    ]
    if len(fault_journal) != 2 or len(starts) != 1 or len(terminals) != 1:
        _error("fault journal must contain exactly one SIGKILL terminal")
    if crash_request_ns is None:
        crash_request_ns = _integer(
            starts[0].get("source_monotonic_ns"),
            "fault request timestamp",
        )
    if crash_request_ns < 0:
        _error("fault request timestamp must be non-negative")
    outcome = _mapping(terminals[0].get("outcome"), "fault outcome")
    if (
        terminals[0].get("fault_id") != profile.fault.fault_id
        or terminals[0].get("plan_sha256") != plan.sha256
        or outcome.get("status") != "succeeded"
        or outcome.get("replica_id") != profile.fault.replica_id
        or outcome.get("signal_number") != 9
        or outcome.get("returncode") != -9
    ):
        _error("fault journal does not record the exact SIGKILL")
    if (
        len(process_exits) != 1
        or process_exits[0].get("replica_id") != profile.fault.replica_id
        or process_exits[0].get("returncode") != -9
    ):
        _error("unexpected process exit")
    expected_sources = {f"replica-{replica}" for replica in profile.replica_ids}
    if set(streams) != expected_sources:
        _error("structured streams must cover the exact replica membership")
    commits: dict[tuple[int, str], dict[int, int]] = {}
    hashes_by_height: dict[int, set[str]] = {}
    authoritative: list[tuple[int, str, int]] = []
    authoritative_keys: set[tuple[int, str]] = set()
    for source_name, events in streams.items():
        expected_replica = int(source_name.removeprefix("replica-"))
        for event in events:
            event_type = event.get("event_type")
            payload_value = event.get("payload")
            if event_type in {"epoch.activated", "adaptive.configuration_active"}:
                payload = _mapping(payload_value, "epoch event payload")
                epoch_number = payload.get("epoch_number")
                if isinstance(epoch_number, int) and epoch_number > 0:
                    _error("unexpected successor epoch activation")
                if (
                    event_type == "adaptive.configuration_active"
                    and "global_quorum" in payload
                    and payload.get("global_quorum") != profile.quorum
                ):
                    _error("configuration record drifted from fixed quorum")
            if event_type not in {"block.committed", "block.commit_observed"}:
                continue
            payload = _mapping(payload_value, "commit payload")
            height, block_hash = payload.get("block_height"), payload.get("block_hash")
            source = event.get("source_id")
            timestamp = event.get("source_monotonic_ns")
            if (
                isinstance(height, bool)
                or not isinstance(height, int)
                or height <= 0
                or not isinstance(block_hash, str)
                or len(block_hash) != 64
                or not isinstance(source, str)
                or source != source_name
                or isinstance(timestamp, bool)
                or not isinstance(timestamp, int)
                or timestamp < 0
            ):
                _error("invalid commit evidence")
            replica = int(source.removeprefix("replica-"))
            if replica != expected_replica:
                _error("commit source does not match its structured stream")
            hashes_by_height.setdefault(height, set()).add(block_hash)
            previous = commits.setdefault((height, block_hash), {}).get(replica)
            commits[(height, block_hash)][replica] = (
                timestamp if previous is None else min(previous, timestamp)
            )
            if event_type == "block.committed":
                if replica != profile.authoritative_observer:
                    continue
                key = (height, block_hash)
                if key in authoritative_keys:
                    _error("duplicate authoritative commit")
                authoritative_keys.add(key)
                authoritative.append((height, block_hash, timestamp))
    if any(len(hashes) != 1 for hashes in hashes_by_height.values()):
        _error("global conflicting same-height commit hashes")

    fixed_witnesses = postfault_witnesses(profile)
    survivor_set = frozenset(profile.replica_ids) - {profile.fault.replica_id}
    common: list[tuple[int, str, int, dict[int, int]]] = []
    all_survivor_common: list[tuple[int, str, int, dict[int, int]]] = []
    for height, block_hash, observer_ns in sorted(
        authoritative, key=lambda item: (item[2], item[0])
    ):
        witnesses = commits[(height, block_hash)]
        if all(replica in witnesses for replica in fixed_witnesses):
            common.append((height, block_hash, observer_ns, witnesses))
        if all(replica in witnesses for replica in survivor_set):
            all_survivor_common.append((height, block_hash, observer_ns, witnesses))
    before = [
        item
        for item in common
        if item[2] < crash_request_ns
        and all(item[3][replica] < crash_request_ns for replica in fixed_witnesses)
    ]
    after = [
        item
        for item in common
        if item[2] > crash_request_ns
        and all(item[3][replica] > crash_request_ns for replica in fixed_witnesses)
    ]
    if not before or not after:
        _error("quorum common commits required before and after crash")
    pre = max(before, key=lambda item: (item[2], item[0]))
    post = min(after, key=lambda item: (item[2], item[0]))

    def witness(
        item: tuple[int, str, int, dict[int, int]],
        required: Sequence[int],
    ) -> dict[str, object]:
        return {
            "height": item[0],
            "block_hash": item[1],
            "observer_monotonic_ns": item[2],
            "common_monotonic_ns": max(
                item[2], *(item[3][replica] for replica in required)
            ),
            "witness_count": len(required),
            "witnesses": list(required),
        }

    descriptive = next(
        (
            item
            for item in all_survivor_common
            if item[2] > crash_request_ns
            and all(item[3][replica] > crash_request_ns for replica in survivor_set)
        ),
        None,
    )
    return {
        "verdict": "PASS",
        "claim_scope": "n31-fanout5-crash-shakedown",
        "fault": {"terminal_status": "succeeded"},
        "pre_fault_common_commit": witness(pre, fixed_witnesses),
        "post_fault_common_commit": witness(post, fixed_witnesses),
        "all_survivor_post_fault_common_commit": (
            witness(descriptive, tuple(sorted(survivor_set)))
            if descriptive is not None
            else None
        ),
        "successor_activation_required": False,
        "attempt_count": 1,
        "fixed_postfault_witnesses": list(fixed_witnesses),
    }
