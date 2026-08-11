"""Pure evidence contracts for the focused N=31 matched crash experiment.

This module validates already-serialized experiment evidence.  It never
launches a process, signals a process group, or authorizes an epoch change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
import hashlib
import json
import re
import signal
from typing import Any

from . import factorial_validation

_SCHEMA_VERSION = 1
_EXPERIMENT_ID = "n31-f5-q21-three-crash-pair-v1"
_REPLICA_COUNT = 31
_FAULT_THRESHOLD = 10
_QUORUM = 21
_FANOUT = 5
_PIPELINE_STRETCH = 2
_ACTIVE_TREE_ID = 20
_CRASHED = (22, 23, 24)
_EPOCH_ZERO_DIGEST = "145fac093343fa9cff20fcf49d85ad5443e93db14146f7854b17e28cf44f6d7a"
_MEMBERSHIP_DIGEST = "107f6e39481091529f50db1c6e32a72518cf50a971b074506bed095cb09769d9"
_EPOCH_CHANGE_PAYLOAD_DOMAIN = b"kauri-epoch-change-payload-v1"
_PHASE_NAMES = ("baseline", "fault", "epoch1", "epoch2")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_MATCHED_FIELDS = (
    "pair_id",
    "revision",
    "build_sha256",
    "pair_seed",
    "host_allocation_sha256",
    "workload_sha256",
    "crash_schedule_sha256",
    "impairment_sha256",
    "containment_policy_sha256",
    "stable_windows_sha256",
)
_EPOCH_ONE_FIELDS = {
    "epoch1_bundle",
    "epoch1_bundle_sha256",
    "epoch1_issuer_public_key",
    "epoch1_decoded",
}
_EPOCH_TWO_FIELDS = {
    "epoch2_bundle",
    "epoch2_bundle_sha256",
    "epoch2_issuer_public_key",
    "epoch2_decoded",
}
_CONTROL_ARM_KEYS = frozenset(
    {*_MATCHED_FIELDS, *_EPOCH_ONE_FIELDS, *_EPOCH_TWO_FIELDS, "arm"}
)
_ADAPTIVE_ARM_KEYS = frozenset(
    {
        *_CONTROL_ARM_KEYS,
        "post_containment_accepted_evidence",
        "ranking_snapshot",
    }
)


class N31CrashPairError(ValueError):
    """Focused crash-pair evidence is malformed, incomplete, or unbound."""


def _error(message: str) -> None:
    raise N31CrashPairError(message)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _error(f"{label} must be a sequence")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _error(f"{label} must be an integer >= {minimum}")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _error(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
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
    except (TypeError, ValueError) as error:
        raise N31CrashPairError("evidence is not canonical JSON") from error


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _replica_ids(value: object, label: str) -> tuple[int, ...]:
    ids = tuple(_integer(item, f"{label} entry") for item in _sequence(value, label))
    if len(ids) != len(set(ids)):
        _error(f"{label} must contain distinct replica IDs")
    return ids


def _require_frozen_membership(value: object, label: str) -> tuple[int, ...]:
    members = _replica_ids(value, label)
    if members != tuple(range(_REPLICA_COUNT)):
        _error(f"{label} differs from the frozen N31 membership")
    return members


def _validate_envelope(
    raw_event: object,
    *,
    expected_source_id: str | None = None,
) -> Mapping[str, Any]:
    event = _mapping(raw_event, "runtime event envelope")
    required = {
        "event_schema_version",
        "run_id",
        "source_kind",
        "source_id",
        "source_instance",
        "source_sequence",
        "source_monotonic_ns",
        "event_type",
        "payload",
    }
    if set(event) != required or event.get("event_schema_version") != 1:
        _error("runtime event envelope schema drifted")
    source_id = event.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        _error("runtime event source ID is malformed")
    if expected_source_id is not None and source_id != expected_source_id:
        _error("runtime event source binding drifted")
    if not isinstance(event.get("run_id"), str) or not event["run_id"]:
        _error("runtime event run identity is malformed")
    if (
        not isinstance(event.get("source_instance"), str)
        or not event["source_instance"]
    ):
        _error("runtime event source instance is malformed")
    _integer(event.get("source_sequence"), "runtime source sequence", minimum=1)
    _integer(event.get("source_monotonic_ns"), "runtime source timestamp")
    if not isinstance(event.get("event_type"), str) or not event["event_type"]:
        _error("runtime event type is malformed")
    _mapping(event.get("payload"), "runtime event payload")
    return event


def _validate_source_stream(
    raw_events: object,
    *,
    source_id: str,
) -> tuple[Mapping[str, Any], ...]:
    events = tuple(
        _validate_envelope(event, expected_source_id=source_id)
        for event in _sequence(raw_events, f"{source_id} events")
    )
    sequences = [event["source_sequence"] for event in events]
    if sequences != list(range(1, len(events) + 1)):
        _error(f"{source_id} source sequence is not contiguous")
    if (
        len({str(event["run_id"]) for event in events}) != 1
        or len({str(event["source_instance"]) for event in events}) != 1
    ):
        _error(f"{source_id} events span multiple run or source instances")
    timestamps = [int(event["source_monotonic_ns"]) for event in events]
    if timestamps != sorted(timestamps):
        _error(f"{source_id} source timestamps regressed")
    if any(event.get("source_kind") != "replica" for event in events):
        _error(f"{source_id} source kind is not replica")
    return events


def _commit_identity(payload: object, *, authoritative: bool) -> dict[str, object]:
    document = _mapping(payload, "commit payload")
    required = {
        "block_height",
        "block_hash",
        "parent_hash",
        "transaction_count",
        "commit_batch_index",
    }
    if authoritative:
        required |= {"designated_observer", "decision_proof", "view_generation"}
    if set(document) != required:
        _error("commit payload schema drifted")
    height = _integer(document.get("block_height"), "commit block height", minimum=1)
    block_hash = _sha256(document.get("block_hash"), "commit block hash")
    parent_hash = document.get("parent_hash")
    if parent_hash is not None:
        _sha256(parent_hash, "commit parent hash")
    transaction_count = _integer(
        document.get("transaction_count"), "commit transaction count"
    )
    batch_index = _integer(document.get("commit_batch_index"), "commit batch index")
    if authoritative:
        if document.get("designated_observer") is not True:
            _error("authoritative commit is not from the designated observer")
        _integer(document.get("view_generation"), "commit view generation", minimum=1)
        proof = _mapping(document.get("decision_proof"), "commit decision proof")
        if (
            set(proof) != {"epoch_number", "tree_id", "epoch_digest", "block_hash"}
            or proof.get("block_hash") != block_hash
        ):
            _error("authoritative commit decision proof is not identity-bound")
        _integer(proof.get("epoch_number"), "decision epoch")
        _integer(proof.get("tree_id"), "decision tree")
        _sha256(proof.get("epoch_digest"), "decision epoch digest")
    return {
        "block_height": height,
        "block_hash": block_hash,
        "parent_hash": parent_hash,
        "transaction_count": transaction_count,
        "commit_batch_index": batch_index,
    }


def validate_epoch0_topology_proof(
    witness: Mapping[str, Any],
    *,
    active_tree_id: int,
    candidate_ids: Sequence[int],
) -> dict[str, object]:
    """Bind crash targets to the exact native Epoch-0 N31 tree witness."""

    document = _mapping(witness, "epoch-zero witness")
    if set(document) != {
        "schema",
        "replica_count",
        "fault_threshold",
        "quorum",
        "fanout",
        "pipeline_stretch",
        "membership",
        "epoch_zero",
    }:
        _error("epoch-zero witness schema drifted")
    if (
        document.get("schema") != "kauri-adaptive-v2-epoch-profile-digest-v1"
        or document.get("replica_count") != _REPLICA_COUNT
        or document.get("fault_threshold") != _FAULT_THRESHOLD
        or document.get("quorum") != _QUORUM
        or document.get("fanout") != _FANOUT
        or document.get("pipeline_stretch") != _PIPELINE_STRETCH
        or document.get("membership") != list(range(_REPLICA_COUNT))
    ):
        _error("epoch-zero witness differs from the frozen N31/F5/Q21 shape")
    epoch = _mapping(document.get("epoch_zero"), "epoch-zero definition")
    if set(epoch) != {
        "schema_version",
        "epoch_number",
        "previous_epoch_digest",
        "membership_digest",
        "activation_height",
        "generation_seed",
        "policy_version",
        "evidence_snapshot_id",
        "evidence_cutoff",
        "canonical_size_bytes",
        "epoch_digest",
        "tree_count",
        "trees",
    }:
        _error("epoch-zero definition schema drifted")
    if (
        epoch.get("schema_version") != 2
        or epoch.get("epoch_number") != 0
        or epoch.get("previous_epoch_digest") != "0" * 64
        or epoch.get("membership_digest") != _MEMBERSHIP_DIGEST
        or epoch.get("activation_height") != 0
        or epoch.get("generation_seed") != 0
        or epoch.get("policy_version") != "adaptive-v2-bootstrap"
        or epoch.get("evidence_snapshot_id") != "adaptive-v2-bootstrap-epoch-zero"
        or epoch.get("evidence_cutoff") != 0
        or epoch.get("canonical_size_bytes") != 2720
        or epoch.get("epoch_digest") != _EPOCH_ZERO_DIGEST
        or epoch.get("tree_count") != _REPLICA_COUNT
    ):
        _error("epoch-zero identity differs from the frozen native definition")
    trees = _sequence(epoch.get("trees"), "epoch-zero trees")
    if len(trees) != _REPLICA_COUNT:
        _error("epoch-zero tree count differs from N31")
    for root, raw_tree in enumerate(trees):
        tree = _mapping(raw_tree, f"epoch-zero tree {root}")
        expected = {
            "tree_id": root,
            "fanout": _FANOUT,
            "pipeline_stretch": _PIPELINE_STRETCH,
            "members_breadth_first": [
                (root + offset) % _REPLICA_COUNT for offset in range(_REPLICA_COUNT)
            ],
            "wait_exempt_leaves": [],
        }
        if dict(tree) != expected:
            _error(f"epoch-zero tree {root} differs from the native topology")

    if _integer(active_tree_id, "active tree ID") != _ACTIVE_TREE_ID:
        _error("active tree differs from the frozen crash tree")
    targets = _replica_ids(candidate_ids, "crash candidates")
    if targets != _CRASHED:
        _error("crash candidates differ from the reviewed topology proof")
    active_tree = _mapping(trees[active_tree_id], "active crash tree")
    members = tuple(active_tree["members_breadth_first"])
    roles: dict[str, object] = {}
    descendants: dict[str, object] = {}
    target_descendants: list[set[int]] = []
    for position, replica in enumerate(members):
        depth = 0 if position == 0 else 1 if position <= _FANOUT else 2
        role = (
            "root" if position == 0 else "internal" if position <= _FANOUT else "leaf"
        )
        roles[str(replica)] = {
            "breadth_first_position": position,
            "depth": depth,
            "role": role,
        }
        if role != "leaf":
            children = (
                list(members[1:])
                if position == 0
                else list(
                    members[_FANOUT * position + 1 : _FANOUT * position + 1 + _FANOUT]
                )
            )
            descendants[str(replica)] = children
            if replica in targets:
                if position == 0 or depth != 1 or len(children) != _FANOUT:
                    _error("crash target is not a deepest internal nonleader")
                target_descendants.append(set(children))
    pairwise_disjoint = all(
        not left.intersection(right)
        for index, left in enumerate(target_descendants)
        for right in target_descendants[index + 1 :]
    )
    if len(target_descendants) != 3 or not pairwise_disjoint:
        _error("crash target descendant sets are not pairwise disjoint")
    return {
        "schema_version": 1,
        "experiment_id": _EXPERIMENT_ID,
        "replica_count": _REPLICA_COUNT,
        "fault_threshold": _FAULT_THRESHOLD,
        "quorum": _QUORUM,
        "fanout": _FANOUT,
        "pipeline_stretch": _PIPELINE_STRETCH,
        "epoch_number": 0,
        "generation_seed": 0,
        "epoch_digest": _EPOCH_ZERO_DIGEST,
        "membership_digest": _MEMBERSHIP_DIGEST,
        "active_tree_id": active_tree_id,
        "target_replica_ids": list(targets),
        "role_by_replica_id": roles,
        "descendant_ids_by_internal_replica_id": descendants,
        "pairwise_disjoint_target_descendants": pairwise_disjoint,
        "topology_sha256": _canonical_sha256(active_tree),
    }


def validate_runtime_evidence_graph(
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    membership_replica_ids: Sequence[int],
    crashed_replica_ids: Sequence[int],
    authoritative_replica_id: int,
    quorum: int,
    expected_epoch_digest: str,
) -> dict[str, object]:
    """Join an authoritative commit, Q observations, and all survivor activations."""

    membership = _require_frozen_membership(membership_replica_ids, "membership")
    crashed = _replica_ids(crashed_replica_ids, "crashed replicas")
    if crashed != _CRASHED:
        _error("runtime graph crashed set differs from the frozen fault cohort")
    if authoritative_replica_id != 0 or quorum != _QUORUM:
        _error("runtime graph authority or quorum differs from the frozen contract")
    epoch_digest = _sha256(expected_epoch_digest, "expected epoch digest")
    survivors = tuple(replica for replica in membership if replica not in crashed)
    expected_sources = {f"replica-{replica}" for replica in survivors}
    if set(streams) != expected_sources:
        _error("runtime graph does not contain exactly all 28 survivor streams")

    normalized: dict[int, tuple[Mapping[str, Any], ...]] = {}
    run_ids: set[str] = set()
    for replica in survivors:
        source = f"replica-{replica}"
        events = _validate_source_stream(streams[source], source_id=source)
        normalized[replica] = events
        run_ids.update(str(event["run_id"]) for event in events)
    if len(run_ids) != 1:
        _error("runtime graph events are not bound to one run")

    committed = [
        event
        for events in normalized.values()
        for event in events
        if event["event_type"] == "block.committed"
    ]
    if len(committed) != 1 or committed[0]["source_id"] != "replica-0":
        _error("runtime graph lacks one authoritative committed block")
    common_identity = _commit_identity(committed[0]["payload"], authoritative=True)
    decision = _mapping(committed[0]["payload"]["decision_proof"], "decision proof")
    if (
        decision.get("epoch_number") != 0
        or decision.get("tree_id") != _ACTIVE_TREE_ID
        or decision.get("epoch_digest") != _EPOCH_ZERO_DIGEST
    ):
        _error("authoritative commit is not bound to the frozen predecessor")

    observations: list[Mapping[str, Any]] = []
    witness_ids: list[int] = []
    observation_by_replica: dict[int, Mapping[str, Any]] = {}
    for replica, events in normalized.items():
        matching = [
            event for event in events if event["event_type"] == "block.commit_observed"
        ]
        for event in matching:
            if (
                _commit_identity(event["payload"], authoritative=False)
                != common_identity
            ):
                _error("common commit observation identity drifted")
            observations.append(event)
            witness_ids.append(replica)
            observation_by_replica[replica] = event
    if len(observations) != quorum or len(set(witness_ids)) != quorum:
        _error("runtime graph does not contain Q21 distinct common-commit witnesses")

    commands_by_replica = {
        replica: tuple(
            event
            for event in events
            if event["event_type"] == "epoch.command_committed"
        )
        for replica, events in normalized.items()
    }
    command_present = any(commands_by_replica.values())
    command_payload: Mapping[str, Any] | None = None
    if not command_present:
        _error("runtime graph lacks the required epoch command witness")
    if command_present:
        if any(len(events) != 1 for events in commands_by_replica.values()):
            _error("runtime graph requires one epoch command from all 28 survivors")
        command_payload = _mapping(
            next(iter(commands_by_replica.values()))[0]["payload"],
            "committed epoch command",
        )
        if set(command_payload) != {
            "command_block_height",
            "command_block_hash",
            "payload_digest",
            "predecessor_epoch_number",
            "predecessor_epoch_digest",
            "successor_epoch_number",
            "successor_epoch_digest",
            "activation_delay_blocks",
            "activation_height",
        }:
            _error("committed epoch command schema drifted")
        command_height = _integer(
            command_payload.get("command_block_height"), "epoch command height"
        )
        _sha256(command_payload.get("command_block_hash"), "epoch command block hash")
        predecessor_digest = _sha256(
            command_payload.get("predecessor_epoch_digest"),
            "epoch command predecessor digest",
        )
        successor_digest = _sha256(
            command_payload.get("successor_epoch_digest"),
            "epoch command successor digest",
        )
        delay = _integer(
            command_payload.get("activation_delay_blocks"),
            "epoch command activation delay",
            minimum=1,
        )
        command_activation_height = _integer(
            command_payload.get("activation_height"),
            "epoch command activation height",
        )
        payload_digest = _sha256(
            command_payload.get("payload_digest"), "epoch command payload digest"
        )
        expected_payload_digest = hashlib.sha256(
            _EPOCH_CHANGE_PAYLOAD_DOMAIN
            + (1).to_bytes(4, "big")
            + bytes.fromhex(predecessor_digest)
            + bytes.fromhex(successor_digest)
            + delay.to_bytes(8, "big")
        ).hexdigest()
        if (
            command_payload.get("predecessor_epoch_number") != 0
            or predecessor_digest != _EPOCH_ZERO_DIGEST
            or command_payload.get("successor_epoch_number") != 1
            or successor_digest != epoch_digest
            or command_activation_height != command_height + delay
            or payload_digest != expected_payload_digest
            or any(
                dict(_mapping(events[0]["payload"], "committed epoch command"))
                != dict(command_payload)
                for events in commands_by_replica.values()
            )
        ):
            _error("committed epoch command is not native-bundle identity-bound")

    transition_ids: list[int] = []
    transition_payload: Mapping[str, Any] | None = None
    for replica, events in normalized.items():
        activations = [
            event for event in events if event["event_type"] == "epoch.activated"
        ]
        if len(activations) != 1:
            _error("runtime graph requires one activation from all 28 survivors")
        activation = activations[0]
        payload = _mapping(activation["payload"], "activation payload")
        tree_id = _integer(payload.get("tree_id"), "activation tree ID")
        activation_height = _integer(
            payload.get("activation_height"), "activation height"
        )
        if (
            set(payload)
            != {"epoch_number", "tree_id", "epoch_digest", "activation_height"}
            or payload.get("epoch_number") != 1
            or payload.get("epoch_digest") != epoch_digest
            or tree_id >= quorum
        ):
            _error("survivor transition evidence is not epoch-identity-bound")
        activation_ns = int(activation["source_monotonic_ns"])
        observation = observation_by_replica.get(replica)
        if activation_ns <= int(committed[0]["source_monotonic_ns"]):
            _error("survivor activation precedes the authoritative common commit")
        if observation is not None and activation_ns <= int(
            observation["source_monotonic_ns"]
        ):
            _error("survivor activation precedes its common-commit observation")
        if command_payload is not None:
            command = commands_by_replica[replica][0]
            if (
                activation_ns <= int(command["source_monotonic_ns"])
                or activation_height != command_payload["activation_height"]
            ):
                _error("survivor activation is not causally bound to its command")
        if transition_payload is None:
            transition_payload = payload
        elif dict(payload) != dict(transition_payload):
            _error("survivors disagree on the activated epoch identity")
        transition_ids.append(replica)
    return {
        "schema_version": 1,
        "passed": True,
        "authoritative_commit": dict(committed[0]),
        "common_commit_observations": [dict(event) for event in observations],
        "common_commit_witness_replica_ids": sorted(witness_ids),
        "transition_witness_replica_ids": sorted(transition_ids),
        "source_sequence_contiguous": True,
    }


def _validate_fault_plan(
    fault_plan: object,
) -> tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...], str]:
    plan = _mapping(fault_plan, "fault plan")
    if (
        set(plan) != {"schema_version", "seed", "scenario", "actions"}
        or plan.get("schema_version") != 1
    ):
        _error("fault plan schema drifted")
    _integer(plan.get("seed"), "fault plan seed")
    scenario = _mapping(plan.get("scenario"), "fault scenario")
    if dict(scenario) != {
        "replica_ids": list(range(_REPLICA_COUNT)),
        "quorum": _QUORUM,
        "crash_budget": _FAULT_THRESHOLD,
        "successor_bundle_retry_limit": 1,
    }:
        _error("fault plan scenario differs from the frozen N31 contract")
    actions = tuple(
        _mapping(action, "fault action")
        for action in _sequence(plan.get("actions"), "fault actions")
    )
    expected = tuple(
        {
            "fault_id": f"crash-replica-{replica}",
            "kind": "replica_group_sigkill",
            "replica_id": replica,
        }
        for replica in _CRASHED
    )
    if tuple(dict(action) for action in actions) != expected:
        _error("fault plan is not the exact three-target SIGKILL batch")
    plan_bytes = _canonical_json_bytes(plan).rstrip(b"\n")
    return plan, actions, hashlib.sha256(plan_bytes).hexdigest()


def validate_atomic_sigkill_evidence(
    *,
    fault_plan: Mapping[str, Any],
    process_records: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
    journal_events: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    """Validate the serialized result of one ProcessRegistry SIGKILL batch."""

    _, actions, plan_sha256 = _validate_fault_plan(fault_plan)
    records = tuple(
        _mapping(record, "process record")
        for record in _sequence(process_records, "process records")
    )
    results = tuple(
        _mapping(outcome, "SIGKILL outcome")
        for outcome in _sequence(outcomes, "SIGKILL outcomes")
    )
    journal = tuple(
        _mapping(event, "fault journal event")
        for event in _sequence(journal_events, "fault journal")
    )
    if len(records) != 3 or len(results) != 3 or len(journal) != 6:
        _error("SIGKILL evidence does not cover the complete atomic batch")

    records_by_replica: dict[int, Mapping[str, Any]] = {}
    pids: set[int] = set()
    pgids: set[int] = set()
    for record in records:
        if set(record) != {"name", "replica_id", "pid", "pgid"}:
            _error("process registry record schema drifted")
        replica = _integer(record.get("replica_id"), "registered replica")
        pid = _integer(record.get("pid"), "registered PID", minimum=1)
        pgid = _integer(record.get("pgid"), "registered PGID", minimum=1)
        if record.get("name") != f"replica-{replica}" or pid != pgid:
            _error("process registry PID/PGID identity is unsafe")
        if replica in records_by_replica or pid in pids or pgid in pgids:
            _error("process registry contains duplicate PID/PGID identity")
        records_by_replica[replica] = record
        pids.add(pid)
        pgids.add(pgid)
    if tuple(records_by_replica) != _CRASHED:
        _error("process registry does not contain the exact crash targets")

    outcomes_by_fault: dict[str, Mapping[str, Any]] = {}
    requested: list[int] = []
    confirmed: list[int] = []
    for action, outcome in zip(actions, results, strict=True):
        replica = int(action["replica_id"])
        record = records_by_replica[replica]
        expected_identity = {
            "fault_id": action["fault_id"],
            "name": record["name"],
            "replica_id": replica,
            "pid": record["pid"],
            "pgid": record["pgid"],
            "signal_number": int(signal.SIGKILL),
            "returncode": -int(signal.SIGKILL),
        }
        if any(outcome.get(key) != value for key, value in expected_identity.items()):
            _error("SIGKILL outcome does not match its process identity or exit")
        if set(outcome) != set(expected_identity) | {
            "requested_monotonic_ns",
            "confirmed_monotonic_ns",
        }:
            _error("SIGKILL outcome schema drifted")
        request_ns = _integer(
            outcome.get("requested_monotonic_ns"), "SIGKILL request timestamp"
        )
        confirm_ns = _integer(
            outcome.get("confirmed_monotonic_ns"), "SIGKILL confirmation timestamp"
        )
        requested.append(request_ns)
        confirmed.append(confirm_ns)
        outcomes_by_fault[str(action["fault_id"])] = outcome
    if max(requested) >= min(confirmed):
        _error("all SIGKILL requests were not issued before confirmation began")
    if requested != sorted(requested) or confirmed != sorted(confirmed):
        _error("SIGKILL request or confirmation timestamps regressed")

    expected_fault_ids = [str(action["fault_id"]) for action in actions]
    journal_timestamps: list[int] = []
    for sequence, event in enumerate(journal):
        lifecycle = "started" if sequence < 3 else "terminal"
        fault_id = expected_fault_ids[sequence % 3]
        base = {
            "schema_version": 1,
            "source_id": "fault-orchestrator",
            "source_sequence": sequence,
            "plan_sha256": plan_sha256,
            "fault_id": fault_id,
            "lifecycle": lifecycle,
        }
        if any(event.get(key) != value for key, value in base.items()):
            _error("fault journal ordering or plan identity drifted")
        journal_timestamps.append(
            _integer(event.get("source_monotonic_ns"), "fault journal timestamp")
        )
        expected_keys = set(base) | {"source_monotonic_ns"}
        if lifecycle == "terminal":
            expected_keys.add("outcome")
            expected_outcome = {**outcomes_by_fault[fault_id], "status": "succeeded"}
            if event.get("outcome") != expected_outcome:
                _error("fault journal terminal outcome is not identity-bound")
        if set(event) != expected_keys:
            _error("fault journal event schema drifted")
    if journal_timestamps != sorted(journal_timestamps):
        _error("fault journal timestamps regressed")
    return {
        "schema_version": 1,
        "passed": True,
        "plan_sha256": plan_sha256,
        "target_replica_ids": list(_CRASHED),
        "all_requests_before_any_confirmation": True,
        "terminal_fault_ids": expected_fault_ids,
    }


def validate_manager_blinding(
    *,
    fault_plan: Mapping[str, Any],
    manager_cli_args: Sequence[str],
    manager_input: Mapping[str, Any],
) -> dict[str, object]:
    """Prove that crash truth is absent from the manager launch boundary."""

    _validate_fault_plan(fault_plan)
    arguments = tuple(_sequence(manager_cli_args, "manager CLI arguments"))
    if arguments != (
        "manager",
        "--runtime-evidence",
        "raw/runtime-events.jsonl",
        "--command-output",
        "raw/manager-commands.jsonl",
    ):
        _error("manager CLI contains fault truth or differs from the blinded boundary")
    document = _mapping(manager_input, "manager input")
    expected = {
        "input_source": "authenticated_runtime_evidence_only",
        "runtime_evidence_path": "raw/runtime-events.jsonl",
        "command_output_path": "raw/manager-commands.jsonl",
    }
    if dict(document) != expected:
        _error("manager input contains fault truth or unreviewed fields")
    return {
        "schema_version": 1,
        "blinded": True,
        "manager_cli_args_fault_truth_free": True,
        "manager_input_fault_truth_free": True,
        "input_source": expected["input_source"],
    }


def _observation_id(observation: Mapping[str, Any], source_epoch_digest: str) -> str:
    reporter = _integer(observation.get("reporter_id"), "observation reporter")
    target = _integer(observation.get("observed_replica_id"), "observed replica")
    configuration = _mapping(
        observation.get("configuration"), "observation configuration"
    )
    if (
        set(configuration) != {"epoch_number", "tree_id", "epoch_digest"}
        or _integer(configuration.get("epoch_number"), "observation epoch") != 1
        or _integer(configuration.get("tree_id"), "observation tree") != 0
        or configuration.get("epoch_digest") != source_epoch_digest
    ):
        _error("ranking observation is not bound to the source epoch")
    block_hash = _sha256(observation.get("block_hash"), "observation block hash")
    payload = b"".join(
        (
            b"kauri-response-observation-v1",
            reporter.to_bytes(2, "big"),
            target.to_bytes(2, "big"),
            (1).to_bytes(4, "big"),
            (0).to_bytes(4, "big"),
            bytes.fromhex(source_epoch_digest),
            bytes.fromhex(block_hash),
            (1).to_bytes(1, "big"),
        )
    )
    return hashlib.sha256(payload).hexdigest()


def rebuild_epoch2_ranking(
    accepted_evidence: Sequence[Mapping[str, Any]],
    *,
    source_epoch_digest: str,
    policy_version: str,
    evidence_cutoff: int,
    window_start_ns: int,
    window_end_ns: int,
    membership_replica_ids: Sequence[int],
) -> dict[str, object]:
    """Replay the frozen responsive ranking from accepted evidence only."""

    membership = _require_frozen_membership(
        membership_replica_ids, "ranking membership"
    )
    epoch_digest = _sha256(source_epoch_digest, "ranking source epoch")
    if policy_version != "responsive-rank-v1":
        _error("ranking policy is not the reviewed responsive policy")
    if evidence_cutoff != len(membership):
        _error("ranking evidence cutoff does not consume the complete window")
    start = _integer(window_start_ns, "ranking window start")
    end = _integer(window_end_ns, "ranking window end", minimum=1)
    if end <= start:
        _error("ranking evidence window is empty")
    events = tuple(
        _validate_envelope(event, expected_source_id="adaptive-manager")
        for event in _sequence(accepted_evidence, "accepted ranking evidence")
    )
    if len(events) != evidence_cutoff:
        _error("ranking evidence does not match the frozen cutoff")
    if [event["source_sequence"] for event in events] != list(
        range(1, evidence_cutoff + 1)
    ):
        _error("ranking evidence source sequence is not contiguous")
    if (
        len({str(event["run_id"]) for event in events}) != 1
        or len({str(event["source_instance"]) for event in events}) != 1
    ):
        _error("ranking evidence spans multiple manager source instances")
    envelope_timestamps = [int(event["source_monotonic_ns"]) for event in events]
    if envelope_timestamps != sorted(envelope_timestamps):
        _error("ranking manager envelope timestamps regressed")

    rows_by_replica: dict[int, dict[str, object]] = {}
    reporter_timestamps: list[int] = []
    for sequence, event in enumerate(events, start=1):
        if (
            event.get("source_kind") != "adaptation_manager"
            or event.get("event_type") != "evidence.observation_accepted"
            or not start <= int(event["source_monotonic_ns"]) < end
        ):
            _error("ranking evidence is not fresh accepted manager evidence")
        payload = _mapping(event.get("payload"), "accepted observation payload")
        if payload.get("ingestion_sequence") != sequence or set(payload) != {
            "ingestion_sequence",
            "observation",
        }:
            _error("accepted evidence ingestion sequence drifted")
        observation = _mapping(payload.get("observation"), "accepted observation")
        target = _integer(observation.get("observed_replica_id"), "observed replica")
        if target not in membership or target in rows_by_replica:
            _error("ranking evidence duplicates or omits a membership identity")
        if observation.get("schema_version") != 1 or observation.get(
            "observation_id"
        ) != _observation_id(observation, epoch_digest):
            _error("accepted observation identity does not recompute")
        if (
            observation.get("reporter_id") != 30
            or observation.get("expected_message_type") != "direct_vote"
        ):
            _error("accepted observation reporter/message binding drifted")
        if observation.get("reporter_sequence") != sequence:
            _error("accepted observation reporter sequence drifted")
        reporter_timestamp = _integer(
            observation.get("reporter_monotonic_ns"), "observation reporter timestamp"
        )
        if not start <= reporter_timestamp < end or reporter_timestamp > int(
            event["source_monotonic_ns"]
        ):
            _error("observation reporter timestamp is outside the frozen window")
        reporter_timestamps.append(reporter_timestamp)
        deadline = _integer(
            observation.get("deadline_duration_us"), "observation deadline", minimum=1
        )
        latency = _integer(
            observation.get("response_duration_us"), "observation latency"
        )
        outcome = observation.get("outcome")
        signer_set = _replica_ids(
            observation.get("signer_set"), "observation signer set"
        )
        eligible = outcome == "on_time"
        if eligible:
            if signer_set != (target,) or latency <= 0 or latency > deadline:
                _error("on-time observation has invalid signer or latency evidence")
        elif outcome == "timeout":
            if signer_set or latency != 0:
                _error("timeout observation contains fabricated response evidence")
        else:
            _error("ranking observation outcome is unsupported")
        rows_by_replica[target] = {
            "replica_id": target,
            "eligible": eligible,
            "attempt_count": 1,
            "response_rate_ppm": 1_000_000 if eligible else 0,
            "timeout_rate_ppm": 0 if eligible else 1_000_000,
            "latency_percentile_us": latency if eligible else None,
            "score_ppm": 1_000_000 - latency if eligible else -1,
        }
    if tuple(sorted(rows_by_replica)) != membership:
        _error("ranking evidence does not cover the complete membership")
    if reporter_timestamps != sorted(reporter_timestamps):
        _error("ranking reporter timestamps regressed")
    score_rows = [rows_by_replica[replica] for replica in membership]
    ranked = sorted(
        (row for row in score_rows if row["eligible"] is True),
        key=lambda row: (-int(row["score_ppm"]), int(row["replica_id"])),
    )
    ranked_ids = [int(row["replica_id"]) for row in ranked]
    return {
        "source_evidence_sha256": _canonical_sha256(accepted_evidence),
        "source_epoch_digest": epoch_digest,
        "policy_version": policy_version,
        "evidence_cutoff": evidence_cutoff,
        "window_start_ns": start,
        "window_end_ns": end,
        "ranked_eligible_replica_ids": ranked_ids,
        "selected_root_ids": ranked_ids[:_QUORUM],
        "score_rows": score_rows,
        "tie_break": "score_desc_replica_id_asc_v1",
    }


def _decode_bound_bundle(document: Mapping[str, Any], prefix: str) -> Any:
    wire = document.get(f"{prefix}_bundle")
    if not isinstance(wire, bytes) or not wire:
        _error(f"{prefix} native bundle is absent")
    digest = hashlib.sha256(wire).hexdigest()
    if document.get(f"{prefix}_bundle_sha256") != digest:
        _error(f"{prefix} bundle digest is not bound")
    public_key = document.get(f"{prefix}_issuer_public_key")
    if not isinstance(public_key, str):
        _error(f"{prefix} issuer public key is absent")
    try:
        decoded = factorial_validation.decode_epoch_change_bundle(
            wire,
            issuer_public_key=public_key,
        )
    except Exception as error:
        raise N31CrashPairError(
            f"{prefix} native bundle failed verification"
        ) from error
    if document.get(f"{prefix}_decoded") != asdict(decoded):
        _error(f"{prefix} decoded bundle projection drifted")
    return decoded


def _validate_epoch_trees(decoded: Any, *, expected_roots: Sequence[int]) -> None:
    if len(decoded.trees) != _QUORUM:
        _error("decoded epoch does not contain Q21 trees")
    if tuple(tree.tree_id for tree in decoded.trees) != tuple(range(_QUORUM)):
        _error("decoded epoch tree IDs are noncanonical")
    if tuple(tree.members[0] for tree in decoded.trees) != tuple(expected_roots):
        _error("decoded epoch roots differ from the bound ranking")
    for tree in decoded.trees:
        if (
            tree.fanout != _FANOUT
            or tree.pipeline_stretch != _PIPELINE_STRETCH
            or len(tree.members) != _REPLICA_COUNT
            or set(tree.members) != set(range(_REPLICA_COUNT))
            or tree.wait_exempt != _CRASHED
            or any(tree.members.index(replica) < 1 + _FANOUT for replica in _CRASHED)
        ):
            _error("decoded epoch tree violates the focused N31 topology")


def validate_matched_pair(
    control: Mapping[str, Any],
    adaptive: Mapping[str, Any],
) -> dict[str, object]:
    """Verify the common native E1 and adaptive-only evidence-bound native E2."""

    control_document = _mapping(control, "control arm")
    adaptive_document = _mapping(adaptive, "adaptive arm")
    if (
        set(control_document) != _CONTROL_ARM_KEYS
        or set(adaptive_document) != _ADAPTIVE_ARM_KEYS
    ):
        _error("matched pair arm schema contains missing or unreviewed fields")
    if (
        control_document.get("arm") != "control"
        or adaptive_document.get("arm") != "adaptive"
    ):
        _error("matched pair requires one control and one adaptive arm")
    if any(
        control_document.get(field) != adaptive_document.get(field)
        for field in _MATCHED_FIELDS
    ):
        _error("matched pair immutable inputs differ")
    if not _REVISION.fullmatch(str(control_document.get("revision"))):
        _error("matched pair revision is malformed")
    for field in _MATCHED_FIELDS:
        if field.endswith("sha256"):
            _sha256(control_document.get(field), f"matched pair {field}")
    _integer(control_document.get("pair_seed"), "matched pair seed")

    control_epoch1 = _decode_bound_bundle(control_document, "epoch1")
    adaptive_epoch1 = _decode_bound_bundle(adaptive_document, "epoch1")
    if (
        control_document["epoch1_bundle"] != adaptive_document["epoch1_bundle"]
        or control_document["epoch1_bundle_sha256"]
        != adaptive_document["epoch1_bundle_sha256"]
        or control_document["epoch1_issuer_public_key"]
        != adaptive_document["epoch1_issuer_public_key"]
        or asdict(control_epoch1) != asdict(adaptive_epoch1)
    ):
        _error("matched pair Epoch 1 is not exactly identical")
    if (
        control_epoch1.epoch_number != 1
        or control_epoch1.previous_epoch_digest != _EPOCH_ZERO_DIGEST
        or control_epoch1.membership_digest != _MEMBERSHIP_DIGEST
        or control_epoch1.generation_seed != 1
        or control_epoch1.policy_version != "focused-containment-v1"
        or control_epoch1.evidence_snapshot_id
        != f"{control_document['pair_id']}-containment"
        or control_epoch1.evidence_cutoff != 100
    ):
        _error("matched pair Epoch 1 identity drifted")
    _validate_epoch_trees(control_epoch1, expected_roots=tuple(range(_QUORUM)))
    if any(
        control_document.get(field) is not None
        for field in (
            "epoch2_bundle",
            "epoch2_bundle_sha256",
            "epoch2_issuer_public_key",
            "epoch2_decoded",
        )
    ):
        _error("matched pair control arm contains an Epoch 2")

    evidence = _sequence(
        adaptive_document.get("post_containment_accepted_evidence"),
        "post-containment accepted evidence",
    )
    snapshot = _mapping(adaptive_document.get("ranking_snapshot"), "ranking snapshot")
    rebuilt = rebuild_epoch2_ranking(
        evidence,
        source_epoch_digest=str(snapshot.get("source_epoch_digest")),
        policy_version=str(snapshot.get("policy_version")),
        evidence_cutoff=_integer(snapshot.get("evidence_cutoff"), "ranking cutoff"),
        window_start_ns=_integer(
            snapshot.get("window_start_ns"), "ranking window start"
        ),
        window_end_ns=_integer(snapshot.get("window_end_ns"), "ranking window end"),
        membership_replica_ids=tuple(range(_REPLICA_COUNT)),
    )
    if dict(snapshot) != rebuilt:
        _error("matched pair ranking is not the canonical evidence replay")
    adaptive_epoch2 = _decode_bound_bundle(adaptive_document, "epoch2")
    if (
        adaptive_document.get("epoch2_issuer_public_key")
        != control_document.get("epoch1_issuer_public_key")
        or adaptive_epoch2.epoch_number != 2
        or adaptive_epoch2.previous_epoch_digest != control_epoch1.epoch_digest
        or adaptive_epoch2.membership_digest != _MEMBERSHIP_DIGEST
        or adaptive_epoch2.generation_seed != 2
        or adaptive_epoch2.policy_version != "responsive-rank-v1"
        or adaptive_epoch2.evidence_snapshot_id
        != f"{adaptive_document['pair_id']}-ranking"
        or adaptive_epoch2.evidence_cutoff != len(evidence)
    ):
        _error("matched pair Epoch 2 is not bound to its predecessor and evidence")
    selected = _replica_ids(snapshot.get("selected_root_ids"), "ranked root selection")
    ranked = _replica_ids(
        snapshot.get("ranked_eligible_replica_ids"), "eligible ranking"
    )
    if selected != ranked[:_QUORUM]:
        _error("matched pair Epoch 2 roots are not the top Q ranking")
    _validate_epoch_trees(adaptive_epoch2, expected_roots=selected)
    return {
        "schema_version": 1,
        "matched": True,
        "epoch1_exactly_identical": True,
        "control_has_epoch2": False,
        "adaptive_epoch2_bound_to_fresh_evidence": True,
        "adaptive_epoch2_roots_are_top_q": True,
        "verified_epoch_numbers": [1, 2],
        "epoch1_tree_count": len(control_epoch1.trees),
        "epoch2_tree_count": len(adaptive_epoch2.trees),
    }


def compute_four_phase_throughput(
    events: Sequence[Mapping[str, Any]],
    *,
    phase_windows: Sequence[Mapping[str, Any]],
    authoritative_source_id: str,
    bucket_width_ns: int,
) -> dict[str, object]:
    """Compute zero-preserving TPS from unique authoritative commit envelopes."""

    if authoritative_source_id != "replica-0":
        _error("throughput source is not the frozen authoritative observer")
    normalized = _validate_source_stream(events, source_id=authoritative_source_id)
    if any(event["event_type"] != "block.committed" for event in normalized):
        _error("throughput input contains a non-commit event")
    width = _integer(bucket_width_ns, "throughput bucket width", minimum=1)
    if len(phase_windows) != len(_PHASE_NAMES):
        _error("throughput requires exactly four phase windows")
    windows: list[tuple[str, int, int]] = []
    prior_end: int | None = None
    for name, raw_window in zip(_PHASE_NAMES, phase_windows, strict=True):
        window = _mapping(raw_window, "throughput phase window")
        start = _integer(window.get("start_ns"), f"{name} phase start")
        end = _integer(window.get("end_ns"), f"{name} phase end", minimum=1)
        if (
            set(window) != {"phase", "start_ns", "end_ns"}
            or window.get("phase") != name
            or end <= start
        ):
            _error("throughput phase window schema or order drifted")
        if prior_end is not None and start != prior_end:
            _error("throughput phase windows are not contiguous")
        windows.append((name, start, end))
        prior_end = end

    commits: dict[int, tuple[str, int, int]] = {}
    for event in normalized:
        identity = _commit_identity(event["payload"], authoritative=True)
        height = int(identity["block_height"])
        block_hash = str(identity["block_hash"])
        previous = commits.get(height)
        if previous is not None and previous[0] != block_hash:
            _error("authoritative commit chain contains two hashes for one height")
        if previous is not None:
            _error("authoritative commit identity is duplicated")
        commits[height] = (
            block_hash,
            int(event["source_monotonic_ns"]),
            int(identity["transaction_count"]),
        )

    phases: list[dict[str, object]] = []
    for name, start, end in windows:
        buckets: list[dict[str, object]] = []
        bucket_start = start
        phase_transactions = 0
        bucket_index = 0
        while bucket_start < end:
            bucket_end = min(bucket_start + width, end)
            transactions = sum(
                transaction_count
                for _, timestamp, transaction_count in commits.values()
                if bucket_start <= timestamp < bucket_end
            )
            phase_transactions += transactions
            duration = (bucket_end - bucket_start) / 1_000_000_000
            buckets.append(
                {
                    "bucket_index": bucket_index,
                    "start_ns": bucket_start,
                    "end_ns": bucket_end,
                    "transactions": transactions,
                    "tps": transactions / duration,
                }
            )
            bucket_start = bucket_end
            bucket_index += 1
        phases.append(
            {
                "phase": name,
                "start_ns": start,
                "end_ns": end,
                "transactions": phase_transactions,
                "mean_tps": phase_transactions / ((end - start) / 1_000_000_000),
                "buckets": buckets,
            }
        )
    return {
        "schema_version": 1,
        "authority": {
            "event_type": "block.committed",
            "source_id": authoritative_source_id,
            "unique_commit_rule": "one_hash_per_height_v1",
        },
        "unique_authoritative_commit_count": len(commits),
        "phases": phases,
    }


__all__ = [
    "N31CrashPairError",
    "compute_four_phase_throughput",
    "rebuild_epoch2_ranking",
    "validate_atomic_sigkill_evidence",
    "validate_epoch0_topology_proof",
    "validate_manager_blinding",
    "validate_matched_pair",
    "validate_runtime_evidence_graph",
]
