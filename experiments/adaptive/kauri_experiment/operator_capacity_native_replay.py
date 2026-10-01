"""Source-level N=31 commit replay for the prospective W18 CPU study.

This is a component, not a raw-bundle validator or a thesis verdict.  The
caller must first bind the 31 streams, their file hashes, launch arguments,
epoch bundle, quota records, and measurement window to one sealed run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


REPLICAS = frozenset(range(31))
_COMMIT_FIELDS = frozenset({
    "block_height", "block_hash", "parent_hash", "transaction_count",
    "designated_observer", "decision_proof", "view_generation",
    "commit_batch_index",
})
_COMMIT_OPTIONAL_FIELDS = frozenset({"reporter_local_commit_monotonic_ns"})
_OBSERVED_FIELDS = frozenset({
    "block_height", "block_hash", "parent_hash", "transaction_count",
    "commit_batch_index",
})
_PROOF_FIELDS = frozenset({"epoch_number", "tree_id", "epoch_digest", "block_hash"})
_ACTIVATION_FIELDS = frozenset({"epoch_number", "tree_id", "epoch_digest", "activation_height"})
_ACTIVATION_V3_FIELDS = frozenset({
    "certificate_apply_committed_height", "activation_readiness_certificate_digest",
})
_HEX = frozenset("0123456789abcdef")


class NativeReplayError(ValueError):
    """Native evidence cannot support a complete post-E1 commit series."""


@dataclass(frozen=True)
class CommonCommit:
    height: int
    block_hash: str
    designated_ns: int
    completion_ns: int
    transaction_count: int


def _hex64(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in _HEX for char in value):
        raise NativeReplayError(f"{label} is not canonical 64-hex")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise NativeReplayError(f"{label} is not a positive integer")
    return value


def _metadata(payload: Mapping[str, Any]) -> tuple[object, int]:
    parent = payload["parent_hash"]
    if parent is not None:
        _hex64(parent, "commit parent hash")
    transaction_count = payload["transaction_count"]
    batch_index = payload["commit_batch_index"]
    if (type(transaction_count) is not int or transaction_count < 0 or
            type(batch_index) is not int or batch_index < 0):
        raise NativeReplayError("commit counters are invalid")
    # The batch index belongs to the reporter's local queue. It is not a
    # cross-replica block identity and must never be compared across peers.
    return parent, transaction_count


def replay_post_e1_common_commits(
    streams: Mapping[int, Sequence[Mapping[str, Any]]], *, run_id: str,
    epoch1_digest: str, window_start_ns: int, window_end_ns: int,
    designated_replica: int = 0,
) -> tuple[CommonCommit, ...]:
    """Reconstruct complete E1 commits, counting only all-31 witnesses.

    The half-open interval is fixed by the caller's *predeclared* measurement
    contract, never inferred from the observed commit rate.  A designated
    commit must occur inside it and all peer witnesses must arrive before its
    end. In-flight commits at the right boundary are censored, not counted.
    Completion is the latest of the 31 source timestamps.
    """
    if set(streams) != REPLICAS or designated_replica not in REPLICAS:
        raise NativeReplayError("exactly 31 native replica streams are required")
    if not isinstance(run_id, str) or not run_id:
        raise NativeReplayError("run id is missing")
    _hex64(epoch1_digest, "E1 digest")
    start = _positive(window_start_ns, "measurement start")
    end = _positive(window_end_ns, "measurement end")
    if end <= start:
        raise NativeReplayError("measurement window is empty")

    activations: dict[int, int] = {}
    designated: dict[tuple[int, str], tuple[int, tuple[object, int]]] = {}
    witnesses: dict[tuple[int, str], dict[int, tuple[int, tuple[object, int]]]] = {}
    heights: dict[int, str] = {}
    source_instances: set[str] = set()
    for replica, events in streams.items():
        previous_sequence = 0
        previous_time = 0
        source_instance: str | None = None
        last_observed: tuple[int, tuple[int, str], tuple[object, int]] | None = None
        for event in events:
            if not isinstance(event, Mapping):
                raise NativeReplayError("native event is not an object")
            if (type(event.get("event_schema_version")) is not int or
                    event.get("event_schema_version") != 1 or event.get("run_id") != run_id or
                    event.get("source_kind") != "replica" or
                    event.get("source_id") != f"replica-{replica}"):
                raise NativeReplayError("native event source envelope drifted")
            instance = event.get("source_instance")
            if not isinstance(instance, str) or not instance:
                raise NativeReplayError("native source instance is missing")
            if source_instance is None:
                source_instance = instance
                if instance in source_instances:
                    raise NativeReplayError("native source instance is reused")
                source_instances.add(instance)
            elif source_instance != instance:
                raise NativeReplayError("native source restarted within one run")
            sequence = _positive(event.get("source_sequence"), "native sequence")
            timestamp = _positive(event.get("source_monotonic_ns"), "native timestamp")
            if sequence <= previous_sequence or timestamp < previous_time:
                raise NativeReplayError("native stream order regressed")
            previous_sequence, previous_time = sequence, timestamp
            payload = event.get("payload")
            event_type = event.get("event_type")
            if event_type == "epoch.activated":
                if (not isinstance(payload, Mapping) or
                        set(payload) != _ACTIVATION_FIELDS | _ACTIVATION_V3_FIELDS):
                    raise NativeReplayError("activation payload drifted")
                if payload["epoch_number"] == 1:
                    if (replica in activations or payload["epoch_digest"] != epoch1_digest or
                            type(payload["tree_id"]) is not int or not 0 <= payload["tree_id"] < 21 or
                            type(payload["activation_height"]) is not int or payload["activation_height"] <= 0 or
                            type(payload["certificate_apply_committed_height"]) is not int or
                            payload["certificate_apply_committed_height"] <= 0):
                        raise NativeReplayError("E1 activation identity drifted")
                    _hex64(payload["activation_readiness_certificate_digest"], "E1 readiness certificate digest")
                    activations[replica] = timestamp
                continue
            if event_type not in {"block.committed", "block.commit_observed"}:
                continue
            fields = _COMMIT_FIELDS if event_type == "block.committed" else _OBSERVED_FIELDS
            if (not isinstance(payload, Mapping) or
                    (set(payload) not in (fields, fields | _COMMIT_OPTIONAL_FIELDS)
                     if event_type == "block.committed" else set(payload) != fields)):
                raise NativeReplayError("commit payload drifted")
            height = _positive(payload["block_height"], "commit height")
            block_hash = _hex64(payload["block_hash"], "commit block hash")
            if heights.setdefault(height, block_hash) != block_hash:
                raise NativeReplayError("conflicting commit hashes at one height")
            key = height, block_hash
            metadata = _metadata(payload)
            if event_type == "block.committed":
                if type(payload["designated_observer"]) is not bool:
                    raise NativeReplayError("designated observer flag drifted")
                view = payload["view_generation"]
                if view is not None and (type(view) is not int or view < 0):
                    raise NativeReplayError("commit view generation drifted")
                if "reporter_local_commit_monotonic_ns" in payload:
                    _positive(payload["reporter_local_commit_monotonic_ns"], "reporter-local commit time")
                proof = payload["decision_proof"]
                if not isinstance(proof, Mapping) or set(proof) != _PROOF_FIELDS:
                    raise NativeReplayError("decision proof drifted")
                _hex64(proof["epoch_digest"], "decision epoch digest")
                if proof["block_hash"] != block_hash:
                    raise NativeReplayError("decision proof block hash drifted")
                if proof["epoch_number"] != 1:
                    continue
                if (proof["epoch_digest"] != epoch1_digest or
                        type(proof["tree_id"]) is not int or not 0 <= proof["tree_id"] < 21):
                    raise NativeReplayError("E1 decision proof drifted")
                if replica == designated_replica:
                    if payload["designated_observer"] is not True or key in designated:
                        raise NativeReplayError("E1 designated commit is duplicated or mislabeled")
                    if last_observed != (sequence - 1, key, metadata):
                        raise NativeReplayError("E1 designated commit lacks adjacent local observation")
                    designated[key] = timestamp, metadata
                elif payload["designated_observer"] is True:
                    raise NativeReplayError("another replica claims designated observer")
            else:
                peers = witnesses.setdefault(key, {})
                if replica in peers:
                    raise NativeReplayError("peer commit witness is duplicated")
                peers[replica] = timestamp, metadata
                last_observed = sequence, key, metadata

    if set(activations) != REPLICAS or max(activations.values()) > start:
        raise NativeReplayError("all 31 E1 activations must precede measurement")
    accepted: list[CommonCommit] = []
    for (height, block_hash), (designated_ns, metadata) in designated.items():
        if not start <= designated_ns < end:
            continue
        peers = witnesses.get((height, block_hash), {})
        if not REPLICAS.difference({designated_replica}).issubset(peers):
            continue
        if any(peer_metadata != metadata for replica, (_when, peer_metadata) in peers.items()
               if replica != designated_replica):
            raise NativeReplayError("peer commit metadata conflicts with designated commit")
        completion = max(designated_ns, *(peers[replica][0] for replica in REPLICAS if replica != designated_replica))
        if completion >= end:
            continue
        if metadata[1] == 0:
            # Empty pipeline padding is valid protocol traffic.  It cannot
            # contribute to the predeclared one-command commit metric.
            continue
        if metadata[1] != 1:
            raise NativeReplayError("counted E1 common commit does not contain exactly one synthetic command")
        accepted.append(CommonCommit(height, block_hash, designated_ns, completion, metadata[1]))
    accepted.sort(key=lambda item: item.height)
    if any(later.height <= earlier.height or later.completion_ns < earlier.completion_ns
           for earlier, later in zip(accepted, accepted[1:])):
        raise NativeReplayError("E1 common commit order regressed")
    return tuple(accepted)
