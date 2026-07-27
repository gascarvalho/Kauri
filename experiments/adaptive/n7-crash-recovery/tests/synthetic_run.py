"""Synthetic non-evidence run builder for validator and plotter tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


def _encode_unsigned(value: int, size: int) -> bytes:
    return value.to_bytes(size, "big")


def _encode_component(value: bytes) -> bytes:
    return _encode_unsigned(len(value), 4) + value


def _encode_string(value: str) -> bytes:
    return _encode_component(value.encode("utf-8"))


def _epoch_change_payload_digest(
    successor_epoch_number: int,
    predecessor_digest: str,
    successor_digest: str,
) -> str:
    payload = b"".join(
        (
            b"kauri-epoch-change-payload-v1",
            _encode_unsigned(successor_epoch_number, 4),
            bytes.fromhex(predecessor_digest),
            bytes.fromhex(successor_digest),
            _encode_unsigned(5, 8),
        )
    )
    return hashlib.sha256(payload).hexdigest()


def _synthetic_epoch_digest(
    epoch_number: int,
    predecessor_digest: str,
    root_order: tuple[int, ...],
    snapshot_id: str,
    evidence_cutoff: int,
) -> str:
    membership_payload = b"".join(
        (
            b"kauri-membership-v1",
            _encode_unsigned(7, 4),
            b"".join(_encode_unsigned(replica, 2) for replica in range(7)),
        )
    )
    membership_digest = hashlib.sha256(membership_payload).digest()
    trees = []
    for tree_id, root in enumerate(root_order):
        members = [root, *[item for item in root_order if item != root], 0, 1]
        trees.append(
            b"".join(
                (
                    _encode_unsigned(tree_id, 4),
                    _encode_unsigned(2, 4),
                    _encode_unsigned(2, 4),
                    _encode_unsigned(7, 4),
                    b"".join(_encode_unsigned(member, 2) for member in members),
                    _encode_unsigned(2, 4),
                    _encode_unsigned(0, 2),
                    _encode_unsigned(1, 2),
                )
            )
        )
    definition = b"".join(
        (
            b"kauri-epoch-definition-v2",
            _encode_unsigned(2, 4),
            _encode_unsigned(epoch_number, 4),
            bytes.fromhex(predecessor_digest),
            membership_digest,
            _encode_unsigned(0xA2F7, 8),
            _encode_string("adaptive-v2-performance-optimization-v1"),
            _encode_string(snapshot_id),
            _encode_unsigned(evidence_cutoff, 8),
            _encode_unsigned(5, 4),
            b"".join(trees),
        )
    )
    return hashlib.sha256(definition).hexdigest()


RUN_ID = "synthetic-validator-non-evidence"
REVISION = "1" * 40
CONTAINMENT_BASELINE_CUTOFF = 17
CONTAINMENT_CURRENT_CUTOFF = 34
OPTIMIZATION_BASELINE_CUTOFF = 12
OPTIMIZATION_CURRENT_CUTOFF = 23
EPOCH_0_DIGEST = "a" * 64
EPOCH_1_DIGEST = _synthetic_epoch_digest(
    1,
    EPOCH_0_DIGEST,
    (5, 6, 2, 3, 4),
    "synthetic-snapshot-0",
    CONTAINMENT_CURRENT_CUTOFF,
)
EPOCH_2_DIGEST = _synthetic_epoch_digest(
    2,
    EPOCH_1_DIGEST,
    (6, 5, 4, 3, 2),
    "synthetic-snapshot-1",
    OPTIMIZATION_CURRENT_CUTOFF,
)
COMMAND_HASH = f"{12:064x}"
SECOND_COMMAND_HASH = f"{28:064x}"
PAYLOAD_DIGEST = _epoch_change_payload_digest(
    1, EPOCH_0_DIGEST, EPOCH_1_DIGEST
)
SECOND_PAYLOAD_DIGEST = _epoch_change_payload_digest(
    2, EPOCH_1_DIGEST, EPOCH_2_DIGEST
)
BASELINE_NS = 1_000_000_000
CRASH_0_NS = 36_000_000_000
CRASH_1_NS = 36_100_000_000
COMMAND_NS = 54_100_000_000
ACTIVATION_NS = 74_100_000_000
RECURRING_COMMAND_NS = 71_000_000_000
SECOND_COMMAND_NS = 116_100_000_000
SECOND_ACTIVATION_NS = 136_100_000_000
MINIMUM_POST_ACTIVATION_GRACE_NS = 1_000_000_000
END_NS = 111_006_000_000
RECURRING_END_NS = 173_006_000_000
MANAGER_READY_NS = ACTIVATION_NS + 10_000_000
SECOND_MANAGER_READY_NS = SECOND_ACTIVATION_NS + 10_000_000
TRANSITION_ARTIFACT_IDS = (
    "e0-to-e1-containment",
    "e1-to-e2-optimization",
)
TRANSITION_BUNDLE_PATHS = (
    "transitions/e0-to-e1-containment/successor.bundle",
    "transitions/e1-to-e2-optimization/successor.bundle",
)
TRANSITION_SNAPSHOT_PATHS = (
    "transitions/e0-to-e1-containment/evidence-snapshot.json",
    "transitions/e1-to-e2-optimization/evidence-snapshot.json",
)
CONTAINMENT_BASELINE_ROOTS = (0, 1, 2, 3, 4)
CONTAINMENT_ROOTS = (5, 6, 2, 3, 4)
ELIGIBLE_OPTIMIZATION_RANKING = (6, 5, 4, 3, 2)
EVIDENCE_WINDOW_RULE = "fresh_exact_predecessor_after_common_commit"
MANAGER_LIMITS = {
    "maximum_members": 7,
    "readiness_wire_maximum_payload_bytes": 256,
    "lifecycle_wire_maximum_payload_bytes": 512,
    "evidence_wire_maximum_payload_bytes": 4096,
    "evidence_wire_maximum_observations": 8,
    "evidence_wire_maximum_signers_per_observation": 7,
    "proposal_maximum_exact_records": 8192,
    "proposal_maximum_retired_configurations": 16,
    "evidence_maximum_accepted_records": 131072,
    "evidence_maximum_rejected_records": 131072,
    "reputation_maximum_audit_updates": 131072,
    "quarantine_maximum_records": 1024,
    "quarantine_maximum_canonical_bytes": 256 * 1024,
    "quarantine_maximum_reporter_queues": 7,
    "quarantine_maximum_signer_entries": 8192,
    "quarantine_maximum_deduplication_entries": 1024,
    "quarantine_maximum_lifecycle_sources": 7,
    "quarantine_maximum_records_per_reporter": 128,
    "accounting_maximum_records": 1024,
    "accounting_maximum_canonical_bytes": 256 * 1024,
    "accounting_maximum_signer_entries": 8192,
    "maximum_pending_lifecycle_facts_per_source": 64,
}


def checked_activation_generation(epoch_number: int) -> int:
    return (epoch_number << 32) | 1

LEGACY_PROFILE_BYTES = b'''{
  "schema_version": 1,
  "profile_id": "n7-f2-q5-crash-recovery-v2",
  "frozen": true,
  "replica_ids": [0, 1, 2, 3, 4, 5, 6],
  "fault_threshold": 2,
  "quorum": 5,
  "authoritative_observer": 2,
  "epoch0_roots": [0, 1, 2, 3, 4, 5, 6],
  "crash_targets": [0, 1],
  "crash_epoch": 0,
  "crash_root": 6,
  "successor_epoch": 1,
  "successor_roots": [2, 3, 4, 5, 6],
  "successor_wait_exempt": [0, 1],
  "fanout": 2,
  "pipeline_depth": 2,
  "tree_switch_period_blocks": 1,
  "bucket_width_s": 5,
  "baseline_bucket_count": 7,
  "minimum_post_activation_grace_s": 1,
  "maximum_activation_to_successor_s": 10,
  "post_bucket_count": 7,
  "minimum_qualifying_reporters": 3,
  "minimum_timeout_observations_per_reporter": 2,
  "minimum_net_reputation_drop": 6,
  "response_reputation_delta": 1,
  "timeout_reputation_delta": -1,
  "aggregation_timeout_s": 0.5,
  "leader_progress_timeout_s": 5.0,
  "leader_activation_grace_s": 1.0,
  "activation_delay_blocks": 5,
  "maximum_stall_s": 10,
  "degraded_maximum_stall_s": 25,
  "block_size": 1,
  "snapshot_seed": 41719
}
'''


def _json_line(value: Mapping[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"))


def _envelope(
    *,
    source_kind: str,
    source_id: str,
    source_instance: str,
    timestamp_ns: int,
    event_type: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "event_schema_version": 1,
        "run_id": RUN_ID,
        "source_kind": source_kind,
        "source_id": source_id,
        "source_instance": source_instance,
        "source_sequence": 0,
        "source_monotonic_ns": timestamp_ns,
        "event_type": event_type,
        "payload": dict(payload),
    }


def _commit_payload(
    *,
    height: int,
    epoch: int,
    tree: int,
    transaction_count: int,
    designated: bool,
) -> dict[str, Any]:
    block_hash = f"{height:064x}"
    try:
        digest = (EPOCH_0_DIGEST, EPOCH_1_DIGEST, EPOCH_2_DIGEST)[epoch]
    except (IndexError, TypeError) as exc:
        raise ValueError("epoch must identify a synthetic epoch") from exc
    return {
        "block_height": height,
        "block_hash": block_hash,
        "parent_hash": None if height == 1 else f"{height - 1:064x}",
        "transaction_count": transaction_count,
        "designated_observer": designated,
        "decision_proof": {
            "epoch_number": epoch,
            "tree_id": tree,
            "epoch_digest": digest,
            "block_hash": block_hash,
        },
        "view_generation": 1,
        "commit_batch_index": 0,
    }


def _commit_observed_payload(
    *,
    height: int,
    transaction_count: int,
) -> dict[str, Any]:
    return {
        "block_height": height,
        "block_hash": f"{height:064x}",
        "parent_hash": None if height == 1 else f"{height - 1:064x}",
        "transaction_count": transaction_count,
        "commit_batch_index": 0,
    }


def command_payload(transition_index: int = 0) -> dict[str, Any]:
    commands = (
        {
            "command_block_height": 12,
            "command_block_hash": COMMAND_HASH,
            "payload_digest": PAYLOAD_DIGEST,
            "predecessor_epoch_number": 0,
            "predecessor_epoch_digest": EPOCH_0_DIGEST,
            "successor_epoch_number": 1,
            "successor_epoch_digest": EPOCH_1_DIGEST,
        },
        {
            "command_block_height": 28,
            "command_block_hash": SECOND_COMMAND_HASH,
            "payload_digest": SECOND_PAYLOAD_DIGEST,
            "predecessor_epoch_number": 1,
            "predecessor_epoch_digest": EPOCH_1_DIGEST,
            "successor_epoch_number": 2,
            "successor_epoch_digest": EPOCH_2_DIGEST,
        },
    )
    if type(transition_index) is not int or not 0 <= transition_index < len(commands):
        raise ValueError("transition_index must identify a synthetic transition")
    return {
        **commands[transition_index],
        "activation_delay_blocks": 5,
        "activation_height": (17, 33)[transition_index],
    }


def transition_identity(transition_index: int) -> dict[str, Any]:
    command = command_payload(transition_index)
    return {
        "predecessor_epoch_number": command["predecessor_epoch_number"],
        "predecessor_epoch_digest": command["predecessor_epoch_digest"],
        "successor_epoch_number": command["successor_epoch_number"],
        "successor_epoch_digest": command["successor_epoch_digest"],
        "command_payload_digest": command["payload_digest"],
        "command_block_height": command["command_block_height"],
        "command_block_hash": command["command_block_hash"],
        "activation_delay_blocks": command["activation_delay_blocks"],
        "activation_height": command["activation_height"],
    }


def recurring_transition_requests() -> list[dict[str, Any]]:
    return [
        {
            "policy_intent": "fault_containment",
            "evidence_window_rule": EVIDENCE_WINDOW_RULE,
            "transition_artifact_id": TRANSITION_ARTIFACT_IDS[0],
            "bundle_path": TRANSITION_BUNDLE_PATHS[0],
            "evidence_snapshot_path": TRANSITION_SNAPSHOT_PATHS[0],
            "predecessor_epoch_number": 0,
            "successor_epoch_number": 1,
            "minimum_predecessor_residency_ms": 0,
            "policy_parameters": {
                "containment_baseline_roots": [
                    {"tree_id": tree_id, "replica_id": root}
                    for tree_id, root in enumerate(CONTAINMENT_BASELINE_ROOTS)
                ]
            },
        },
        {
            "policy_intent": "performance_optimization",
            "evidence_window_rule": EVIDENCE_WINDOW_RULE,
            "transition_artifact_id": TRANSITION_ARTIFACT_IDS[1],
            "bundle_path": TRANSITION_BUNDLE_PATHS[1],
            "evidence_snapshot_path": TRANSITION_SNAPSHOT_PATHS[1],
            "predecessor_epoch_number": 1,
            "successor_epoch_number": 2,
            "minimum_predecessor_residency_ms": 40_000,
            "policy_parameters": {},
        },
    ]


def recurring_throughput_windows() -> list[dict[str, Any]]:
    return [
        {
            "phase": "baseline",
            "epoch_number": 0,
            "start_ns": BASELINE_NS,
            "end_ns": CRASH_0_NS,
        },
        {
            "phase": "degraded",
            "epoch_number": 0,
            "start_ns": CRASH_0_NS,
            "end_ns": RECURRING_COMMAND_NS + 2_000_000,
        },
        {
            "phase": "containment",
            "epoch_number": 1,
            "start_ns": 76_006_000_000,
            "end_ns": SECOND_COMMAND_NS + 2_000_000,
        },
        {
            "phase": "optimized",
            "epoch_number": 2,
            "start_ns": 138_006_000_000,
            "end_ns": RECURRING_END_NS,
        },
    ]


def _configuration_active_payload(replica: int, tree: int = 6) -> dict[str, Any]:
    return {
        "epoch_number": 0,
        "tree_id": tree,
        "epoch_digest": EPOCH_0_DIGEST,
        "block_hash": None,
        "context_generation": None,
        "observer_replica": replica,
        "wait_exempt_signers": [],
        "accepted_signers": [],
        "absent_direct_children": [],
        "missing_optional_signers": [],
        "required_branch_gaps": [],
        "root_signer_count": 0,
        "global_quorum": 5,
        "rejection_reason": None,
    }


def _initial_trees() -> list[dict[str, Any]]:
    return [
        {
            "tree_id": root,
            "fanout": 2,
            "members_breadth_first": [
                (root + offset) % 7 for offset in range(7)
            ],
            "wait_exempt": [],
        }
        for root in range(7)
    ]


def _contained_trees(root_order: tuple[int, ...]) -> list[dict[str, Any]]:
    trees = []
    for tree_id, root in enumerate(root_order):
        survivors = [replica for replica in root_order if replica != root]
        trees.append(
            {
                "tree_id": tree_id,
                "fanout": 2,
                "members_breadth_first": [root, *survivors, 0, 1],
                "wait_exempt": [0, 1],
            }
        )
    return trees


def epochs_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "replica_count": 7,
        "fault_threshold": 2,
        "quorum": 5,
        "membership": list(range(7)),
        "epochs": [
            {
                "epoch_number": 0,
                "epoch_digest": EPOCH_0_DIGEST,
                "trees": _initial_trees(),
                "command": None,
            },
            {
                "epoch_number": 1,
                "epoch_digest": EPOCH_1_DIGEST,
                "trees": _contained_trees(CONTAINMENT_ROOTS),
                "command": command_payload(),
            },
        ],
    }


def recurring_epochs_document() -> dict[str, Any]:
    """Return the exact synthetic E0 -> E1 -> E2 chain for M12-R02."""
    document = epochs_document()
    document["epochs"].append(
        {
            "epoch_number": 2,
            "epoch_digest": EPOCH_2_DIGEST,
            "trees": _contained_trees(ELIGIBLE_OPTIMIZATION_RANKING),
            "command": command_payload(1),
        }
    )
    return document


def recurring_transition_bundle(transition_index: int) -> bytes:
    """Encode one structurally canonical test-only adaptive-v2 bundle."""
    document = recurring_epochs_document()
    if type(transition_index) is not int or not 0 <= transition_index < 2:
        raise ValueError("transition_index must identify a synthetic transition")
    predecessor = document["epochs"][transition_index]
    successor = document["epochs"][transition_index + 1]
    command_value = successor["command"]
    assert isinstance(command_value, dict)

    command = b"".join(
        (
            b"kauri-authorized-epoch-change-v1",
            _encode_unsigned(1, 4),
            _encode_unsigned(2, 1),
            _encode_unsigned(1, 4),
            _encode_unsigned(successor["epoch_number"], 4),
            bytes.fromhex(predecessor["epoch_digest"]),
            bytes.fromhex(successor["epoch_digest"]),
            _encode_unsigned(command_value["activation_delay_blocks"], 8),
            bytes.fromhex("11" * 64),
        )
    )
    membership_payload = b"".join(
        (
            b"kauri-membership-v1",
            _encode_unsigned(7, 4),
            b"".join(_encode_unsigned(replica, 2) for replica in range(7)),
        )
    )
    membership_digest = hashlib.sha256(membership_payload).digest()
    tree_payloads = []
    for tree in successor["trees"]:
        tree_payloads.append(
            b"".join(
                (
                    _encode_unsigned(tree["tree_id"], 4),
                    _encode_unsigned(tree["fanout"], 4),
                    _encode_unsigned(2, 4),
                    _encode_unsigned(len(tree["members_breadth_first"]), 4),
                    b"".join(
                        _encode_unsigned(replica, 2)
                        for replica in tree["members_breadth_first"]
                    ),
                    _encode_unsigned(len(tree["wait_exempt"]), 4),
                    b"".join(
                        _encode_unsigned(replica, 2)
                        for replica in tree["wait_exempt"]
                    ),
                )
            )
        )
    definition = b"".join(
        (
            _encode_unsigned(2, 4),
            _encode_unsigned(2, 1),
            _encode_unsigned(6, 1),
            bytes.fromhex(successor["epoch_digest"]),
            _encode_unsigned(2, 4),
            _encode_unsigned(successor["epoch_number"], 4),
            bytes.fromhex(predecessor["epoch_digest"]),
            membership_digest,
            _encode_unsigned(0xA2F7, 8),
            _encode_string("adaptive-v2-performance-optimization-v1"),
            _encode_string(f"synthetic-snapshot-{transition_index}"),
            _encode_unsigned(
                (
                    CONTAINMENT_CURRENT_CUTOFF,
                    OPTIMIZATION_CURRENT_CUTOFF,
                )[transition_index],
                8,
            ),
            _encode_unsigned(len(tree_payloads), 4),
            b"".join(tree_payloads),
        )
    )
    return b"".join(
        (
            b"kauri-adaptive-v2-epoch-change-bundle-v1",
            _encode_unsigned(1, 4),
            _encode_unsigned(2, 1),
            _encode_component(command),
            _encode_component(definition),
        )
    )


def _replica_events(replica: int) -> list[dict[str, Any]]:
    source_id = f"replica-{replica}"
    instance = f"synthetic-{source_id}-instance"
    events = [
        _envelope(
            source_kind="replica",
            source_id=source_id,
            source_instance=instance,
            timestamp_ns=100_000_000,
            event_type="process.started",
            payload={"exit_status": None},
        )
    ]
    events.append(
        _envelope(
            source_kind="replica",
            source_id=source_id,
            source_instance=instance,
            timestamp_ns=200_000_000,
            event_type="process.ready",
            payload={"exit_status": None},
        )
    )
    schedule: list[tuple[int, int, int, int, int]] = []
    height = 1
    for root, timestamp in enumerate(range(2, 37, 5)):
        schedule.append((height, timestamp * 1_000_000_000, 0, root, 100))
        height += 1
    for tree, timestamp in enumerate((38, 42, 46, 50, 54, 58, 62, 66, 70, 74)):
        schedule.append((height, timestamp * 1_000_000_000, 0, tree % 7, 20))
        height += 1
    for index, timestamp in enumerate(
        (76, 81, 86, 91, 96, 101, 106, 107, 108, 109)
    ):
        schedule.append((height, timestamp * 1_000_000_000, 1, index % 5, 100))
        height += 1
    for height, timestamp, epoch, tree, transactions in schedule:
        if replica in (0, 1) and timestamp >= (CRASH_0_NS, CRASH_1_NS)[replica]:
            continue
        events.append(
            _envelope(
                source_kind="replica",
                source_id=source_id,
                source_instance=instance,
                timestamp_ns=timestamp + replica * 1_000_000,
                event_type="block.commit_observed",
                payload=_commit_observed_payload(
                    height=height,
                    transaction_count=transactions,
                ),
            )
        )
        events.append(
            _envelope(
                source_kind="replica",
                source_id=source_id,
                source_instance=instance,
                timestamp_ns=timestamp + replica * 1_000_000,
                event_type="block.committed",
                payload=_commit_payload(
                    height=height,
                    epoch=epoch,
                    tree=tree,
                    transaction_count=transactions,
                    designated=replica == 2,
                ),
            )
        )
    events.append(
        _envelope(
            source_kind="replica",
            source_id=source_id,
            source_instance=instance,
            timestamp_ns=35_000_000_000 + replica * 1_000_000,
            event_type="adaptive.configuration_active",
            payload=_configuration_active_payload(replica),
        )
    )
    if replica in (0, 1):
        return events
    events.append(
        _envelope(
            source_kind="replica",
            source_id=source_id,
            source_instance=instance,
            timestamp_ns=COMMAND_NS + replica * 1_000_000,
            event_type="epoch.command_committed",
            payload=command_payload(),
        )
    )
    events.append(
        _envelope(
            source_kind="replica",
            source_id=source_id,
            source_instance=instance,
            timestamp_ns=ACTIVATION_NS + replica * 1_000_000,
            event_type="epoch.activated",
            payload={
                "epoch_number": 1,
                "tree_id": 0,
                "epoch_digest": EPOCH_1_DIGEST,
                "activation_height": 17,
            },
        )
    )
    events.extend(
        (
            _envelope(
                source_kind="replica",
                source_id=source_id,
                source_instance=instance,
                timestamp_ns=112_000_000_000 + replica * 1_000_000,
                event_type="process.stopping",
                payload={"exit_status": None},
            ),
            _envelope(
                source_kind="replica",
                source_id=source_id,
                source_instance=instance,
                timestamp_ns=113_000_000_000 + replica * 1_000_000,
                event_type="process.stopped",
                payload={"exit_status": None},
            ),
        )
    )
    return events


def _recurring_replica_events(replica: int) -> list[dict[str, Any]]:
    events = [
        event
        for event in _replica_events(replica)
        if event["event_type"] not in ("process.stopping", "process.stopped")
    ]
    if replica in (0, 1):
        return events

    source_id = f"replica-{replica}"
    instance = f"synthetic-{source_id}-instance"
    first_command = next(
        event
        for event in events
        if event["event_type"] == "epoch.command_committed"
    )
    first_command["source_monotonic_ns"] = (
        RECURRING_COMMAND_NS + replica * 1_000_000
    )
    second_command = command_payload(1)
    events.append(
        _envelope(
            source_kind="replica",
            source_id=source_id,
            source_instance=instance,
            timestamp_ns=SECOND_COMMAND_NS + replica * 1_000_000,
            event_type="epoch.command_committed",
            payload=second_command,
        )
    )
    events.append(
        _envelope(
            source_kind="replica",
            source_id=source_id,
            source_instance=instance,
            timestamp_ns=SECOND_ACTIVATION_NS + replica * 1_000_000,
            event_type="epoch.activated",
            payload={
                "epoch_number": 2,
                "tree_id": 0,
                "epoch_digest": EPOCH_2_DIGEST,
                "activation_height": 33,
            },
        )
    )
    recurring_schedule = [
        *((height, timestamp, 1, (height - 18) % 5, 100)
          for height, timestamp in zip(
              range(28, 33),
              range(112_000_000_000, 132_000_000_000, 4_000_000_000),
          )),
        *((height, timestamp, 2, (height - 33) % 5, 140)
          for height, timestamp in zip(
              range(33, 41),
              (
                  138_000_000_000,
                  143_000_000_000,
                  148_000_000_000,
                  153_000_000_000,
                  158_000_000_000,
                  163_000_000_000,
                  168_000_000_000,
                  172_000_000_000,
              ),
          )),
    ]
    for height, timestamp_ns, epoch, tree, transactions in recurring_schedule:
        events.append(
            _envelope(
                source_kind="replica",
                source_id=source_id,
                source_instance=instance,
                timestamp_ns=timestamp_ns + replica * 1_000_000,
                event_type="block.commit_observed",
                payload=_commit_observed_payload(
                    height=height,
                    transaction_count=transactions,
                ),
            )
        )
        events.append(
            _envelope(
                source_kind="replica",
                source_id=source_id,
                source_instance=instance,
                timestamp_ns=timestamp_ns + replica * 1_000_000,
                event_type="block.committed",
                payload=_commit_payload(
                    height=height,
                    epoch=epoch,
                    tree=tree,
                    transaction_count=transactions,
                    designated=replica == 2,
                ),
            )
        )
    events.extend(
        (
            _envelope(
                source_kind="replica",
                source_id=source_id,
                source_instance=instance,
                timestamp_ns=174_000_000_000 + replica * 1_000_000,
                event_type="process.stopping",
                payload={"exit_status": None},
            ),
            _envelope(
                source_kind="replica",
                source_id=source_id,
                source_instance=instance,
                timestamp_ns=175_000_000_000 + replica * 1_000_000,
                event_type="process.stopped",
                payload={"exit_status": None},
            ),
        )
    )
    return events


def _manager_events() -> list[dict[str, Any]]:
    source_kind = "adaptation_manager"
    source_id = "adaptive-manager"
    instance = "synthetic-manager-instance"
    events = [
        _envelope(
            source_kind=source_kind,
            source_id=source_id,
            source_instance=instance,
            timestamp_ns=100_000_000,
            event_type="process.started",
            payload={"exit_status": None},
        )
    ]
    events.append(
        _envelope(
            source_kind=source_kind,
            source_id=source_id,
            source_instance=instance,
            timestamp_ns=200_000_000,
            event_type="process.ready",
            payload={"exit_status": None},
        )
    )
    scores = [0] * 7
    ingestion = 1

    def update(
        timestamp_ns: int, target: int, outcome: str, reporter: int | None = None
    ) -> None:
        nonlocal ingestion
        delta = -1 if outcome == "timeout" else 1
        scores[target] += delta
        events.append(
            _envelope(
                source_kind=source_kind,
                source_id=source_id,
                source_instance=instance,
                timestamp_ns=timestamp_ns,
                event_type="reputation.evidence_applied",
                payload={
                    "evidence_cutoff": ingestion,
                    "ingestion_sequence": ingestion,
                    "observation_id": f"{ingestion + 1000:064x}",
                    "reporter_id": (
                        (target + 1) % 7 if reporter is None else reporter
                    ),
                    "target_id": target,
                    "evidence_outcome": outcome,
                    "reputation_outcome": (
                        "timeout" if outcome == "timeout" else "response"
                    ),
                    "delta": delta,
                    "resulting_score": scores[target],
                },
            )
        )
        ingestion += 1

    for target in range(7):
        update((10 + target) * 1_000_000_000, target, "on_time")
    timeout_schedule = (
        (37, 0, 2),
        (38, 1, 2),
        (39, 0, 3),
        (40, 1, 3),
        (41, 0, 4),
        (42, 1, 4),
        (43, 0, 2),
        (44, 1, 2),
        (45, 0, 3),
        (46, 1, 3),
        (47, 0, 4),
        (48, 1, 4),
    )
    for second, target, reporter in timeout_schedule:
        update(second * 1_000_000_000, target, "timeout", reporter)
    for target in range(2, 7):
        update(
            48_000_000_000 + (target - 1) * 100_000_000,
            target,
            "on_time",
        )
    command = command_payload()
    events.append(
        _envelope(
            source_kind=source_kind,
            source_id=source_id,
            source_instance=instance,
            timestamp_ns=MANAGER_READY_NS,
            event_type="adaptive_v2_ready",
            payload={
                "replica_id": None,
                "delivery_attempt": None,
                "disposition": None,
                "identity": {
                    "predecessor_epoch_number": command[
                        "predecessor_epoch_number"
                    ],
                    "predecessor_epoch_digest": command[
                        "predecessor_epoch_digest"
                    ],
                    "successor_epoch_number": command["successor_epoch_number"],
                    "successor_epoch_digest": command["successor_epoch_digest"],
                    "command_payload_digest": command["payload_digest"],
                    "command_block_height": command["command_block_height"],
                    "command_block_hash": command["command_block_hash"],
                    "activation_delay_blocks": command["activation_delay_blocks"],
                    "activation_height": command["activation_height"],
                },
                "accepted_commit_count": 3,
                "accepted_activation_count": 5,
                "required_activation_count": 5,
                "canonical_payload_digest": None,
                "failure_reason": None,
            },
        )
    )
    events.extend(
        (
            _envelope(
                source_kind=source_kind,
                source_id=source_id,
                source_instance=instance,
                timestamp_ns=MANAGER_READY_NS + 1_000_000,
                event_type="process.stopping",
                payload={"exit_status": None},
            ),
            _envelope(
                source_kind=source_kind,
                source_id=source_id,
                source_instance=instance,
                timestamp_ns=MANAGER_READY_NS + 2_000_000,
                event_type="process.stopped",
                payload={"exit_status": None},
            ),
        )
    )
    return events


def _manager_ready_event(transition_index: int) -> dict[str, Any]:
    timestamp_ns = (MANAGER_READY_NS, SECOND_MANAGER_READY_NS)[transition_index]
    return _envelope(
        source_kind="adaptation_manager",
        source_id="adaptive-manager",
        source_instance="synthetic-manager-instance",
        timestamp_ns=timestamp_ns,
        event_type="adaptive_v2_ready",
        payload={
            "replica_id": None,
            "delivery_attempt": None,
            "disposition": None,
            "identity": transition_identity(transition_index),
            "accepted_commit_count": 3,
            "accepted_activation_count": 5,
            "required_activation_count": 5,
            "canonical_payload_digest": None,
            "failure_reason": None,
        },
    )


def _manager_terminal_event(transition_index: int) -> dict[str, Any]:
    request = recurring_transition_requests()[transition_index]
    identity = transition_identity(transition_index)
    timestamp_ns = (
        MANAGER_READY_NS + 1_000_000,
        SECOND_MANAGER_READY_NS + 1_000_000,
    )[transition_index]
    return _envelope(
        source_kind="adaptation_manager",
        source_id="adaptive-manager",
        source_instance="synthetic-manager-instance",
        timestamp_ns=timestamp_ns,
        event_type="adaptive_v2_session_terminal",
        payload={
            "cycle_ordinal": transition_index,
            "policy_intent": request["policy_intent"],
            "outcome": "advanced",
            "reason": "successor_converged",
            "transition_artifact_id": request["transition_artifact_id"],
            "predecessor_epoch_number": identity[
                "predecessor_epoch_number"
            ],
            "predecessor_epoch_digest": identity[
                "predecessor_epoch_digest"
            ],
            "successor_epoch_number": identity["successor_epoch_number"],
            "successor_epoch_digest": identity["successor_epoch_digest"],
            "command_payload_digest": identity["command_payload_digest"],
            "winning_activation": identity,
            "evidence_window_activation_generation": (
                checked_activation_generation(transition_index)
            ),
            "baseline_evidence_cutoff": (
                CONTAINMENT_BASELINE_CUTOFF,
                OPTIMIZATION_BASELINE_CUTOFF,
            )[transition_index],
            "current_evidence_cutoff": (
                CONTAINMENT_CURRENT_CUTOFF,
                OPTIMIZATION_CURRENT_CUTOFF,
            )[transition_index],
        },
    )


def recurring_evidence_snapshots() -> list[dict[str, Any]]:
    def observation(
        *,
        base_id: int,
        sequence: int,
        epoch_number: int,
        epoch_digest: str,
        reporter: int,
        target: int,
        outcome: str,
        latency_ns: int | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "observation_id": f"{base_id + sequence:064x}",
            "ingestion_sequence": sequence,
            "epoch_number": epoch_number,
            "epoch_digest": epoch_digest,
            "reporter_id": reporter,
            "target_id": target,
            "outcome": outcome,
        }
        if latency_ns is not None:
            value["latency_ns"] = latency_ns
        return value

    first_observations: list[dict[str, Any]] = []
    for target in range(7):
        for attempt in range(2):
            first_observations.append(
                observation(
                    base_id=2_000,
                    sequence=len(first_observations) + 1,
                    epoch_number=0,
                    epoch_digest=EPOCH_0_DIGEST,
                    reporter=(target + attempt + 1) % 7,
                    target=target,
                    outcome="on_time",
                )
            )
    for target, reporter in ((2, 3), (4, 5), (6, 2)):
        first_observations.append(
            observation(
                base_id=2_000,
                sequence=len(first_observations) + 1,
                epoch_number=0,
                epoch_digest=EPOCH_0_DIGEST,
                reporter=reporter,
                target=target,
                outcome="on_time",
            )
        )
    for target, reporter, latency_ns in (
        (2, 3, 900),
        (4, 5, 700),
        (6, 2, 500),
    ):
        first_observations.append(
            observation(
                base_id=2_000,
                sequence=len(first_observations) + 1,
                epoch_number=0,
                epoch_digest=EPOCH_0_DIGEST,
                reporter=reporter,
                target=target,
                outcome="on_time",
                latency_ns=latency_ns,
            )
        )
    for reporter in (2, 3, 4):
        for target in (0, 1):
            for _attempt in range(2):
                first_observations.append(
                    observation(
                        base_id=2_000,
                        sequence=len(first_observations) + 1,
                        epoch_number=0,
                        epoch_digest=EPOCH_0_DIGEST,
                        reporter=reporter,
                        target=target,
                        outcome="timeout",
                    )
                )
    for target, reporter in ((0, 5), (1, 6)):
        first_observations.append(
            observation(
                base_id=2_000,
                sequence=len(first_observations) + 1,
                epoch_number=0,
                epoch_digest=EPOCH_0_DIGEST,
                reporter=reporter,
                target=target,
                outcome="timeout",
            )
        )

    second_observations: list[dict[str, Any]] = []
    # The optimization window inherits E1's consensus-ordered wait-exempt set.
    # It therefore uses only fresh responsive evidence to rank eligible roots;
    # optional absence is deliberately not represented as timeout evidence.
    for target in range(2, 7):
        for attempt in range(2):
            second_observations.append(
                observation(
                    base_id=3_000,
                    sequence=len(second_observations) + 1,
                    epoch_number=1,
                    epoch_digest=EPOCH_1_DIGEST,
                    reporter=2 + ((target - 2 + attempt + 1) % 5),
                    target=target,
                    outcome="on_time",
                )
            )
    for target, reporter in ((2, 3), (6, 2)):
        second_observations.append(
            observation(
                base_id=3_000,
                sequence=len(second_observations) + 1,
                epoch_number=1,
                epoch_digest=EPOCH_1_DIGEST,
                reporter=reporter,
                target=target,
                outcome="on_time",
            )
        )
    latency_by_target = {
        2: (500, 520),
        3: (400, 420),
        4: (300, 320),
        5: (200, 220),
        6: (100, 120, 140),
    }
    for target, latencies in latency_by_target.items():
        for attempt, latency_ns in enumerate(latencies):
            second_observations.append(
                observation(
                    base_id=3_000,
                    sequence=len(second_observations) + 1,
                    epoch_number=1,
                    epoch_digest=EPOCH_1_DIGEST,
                    reporter=2 + ((target - 2 + attempt + 1) % 5),
                    target=target,
                    outcome="on_time",
                    latency_ns=latency_ns,
                )
            )
    return [
        {
            "cycle_ordinal": 0,
            "policy_intent": "fault_containment",
            "transition_artifact_id": TRANSITION_ARTIFACT_IDS[0],
            "predecessor_epoch_number": 0,
            "predecessor_epoch_digest": EPOCH_0_DIGEST,
            "activation_generation": checked_activation_generation(0),
            "baseline_cutoff": CONTAINMENT_BASELINE_CUTOFF,
            "current_cutoff": CONTAINMENT_CURRENT_CUTOFF,
            "observations": first_observations,
            "eligible_ranking": list(CONTAINMENT_ROOTS),
        },
        {
            "cycle_ordinal": 1,
            "policy_intent": "performance_optimization",
            "transition_artifact_id": TRANSITION_ARTIFACT_IDS[1],
            "predecessor_epoch_number": 1,
            "predecessor_epoch_digest": EPOCH_1_DIGEST,
            "activation_generation": checked_activation_generation(1),
            "baseline_cutoff": OPTIMIZATION_BASELINE_CUTOFF,
            "current_cutoff": OPTIMIZATION_CURRENT_CUTOFF,
            "observations": second_observations,
            "eligible_ranking": list(ELIGIBLE_OPTIMIZATION_RANKING),
        },
    ]


def recurring_manager_events(*, completed_cycles: int = 2) -> list[dict[str, Any]]:
    if type(completed_cycles) is not int or not 0 <= completed_cycles <= 2:
        raise ValueError("completed_cycles must be between zero and two")
    events = [
        event
        for event in _manager_events()
        if event["event_type"]
        not in (
            "adaptive_v2_ready",
            "process.stopping",
            "process.stopped",
            "reputation.evidence_applied",
        )
    ]
    snapshots = recurring_evidence_snapshots()
    snapshot_times = (53_000_000_000, 115_000_000_000)
    for transition_index in range(completed_cycles):
        cycle_scores = [0] * 7
        for observation in snapshots[transition_index]["observations"]:
            target = observation["target_id"]
            delta = -1 if observation["outcome"] == "timeout" else 1
            cycle_scores[target] += delta
            sequence = observation["ingestion_sequence"]
            if transition_index == 0:
                timestamp_ns = (
                    (8 + sequence) * 1_000_000_000
                    if sequence <= CONTAINMENT_BASELINE_CUTOFF
                    else 37_000_000_000
                    + (sequence - CONTAINMENT_BASELINE_CUTOFF)
                    * 400_000_000
                )
            else:
                timestamp_ns = (
                    (79 + sequence) * 1_000_000_000
                    if sequence <= OPTIMIZATION_BASELINE_CUTOFF
                    else (84 + sequence) * 1_000_000_000
                )
            events.append(
                _envelope(
                    source_kind="adaptation_manager",
                    source_id="adaptive-manager",
                    source_instance="synthetic-manager-instance",
                    timestamp_ns=timestamp_ns,
                    event_type="reputation.evidence_applied",
                    payload={
                        "evidence_cutoff": sequence,
                        "ingestion_sequence": sequence,
                        "observation_id": observation["observation_id"],
                        "reporter_id": observation["reporter_id"],
                        "target_id": target,
                        "evidence_outcome": observation["outcome"],
                        "reputation_outcome": (
                            "timeout"
                            if observation["outcome"] == "timeout"
                            else "response"
                        ),
                        "delta": delta,
                        "resulting_score": cycle_scores[target],
                    },
                )
            )
        snapshot = _envelope(
            source_kind="adaptation_manager",
            source_id="adaptive-manager",
            source_instance="synthetic-manager-instance",
            timestamp_ns=snapshot_times[transition_index],
            event_type="adaptive_v2_evidence_snapshot",
            payload=snapshots[transition_index],
        )
        events.extend(
            (
                snapshot,
                _manager_ready_event(transition_index),
                _manager_terminal_event(transition_index),
            )
        )
    final_ns = (
        53_100_000_000,
        MANAGER_READY_NS + 2_000_000,
        SECOND_MANAGER_READY_NS + 2_000_000,
    )[completed_cycles]
    events.extend(
        (
            _envelope(
                source_kind="adaptation_manager",
                source_id="adaptive-manager",
                source_instance="synthetic-manager-instance",
                timestamp_ns=final_ns,
                event_type="process.stopping",
                payload={"exit_status": None},
            ),
            _envelope(
                source_kind="adaptation_manager",
                source_id="adaptive-manager",
                source_instance="synthetic-manager-instance",
                timestamp_ns=final_ns + 1_000_000,
                event_type="process.stopped",
                payload={"exit_status": None},
            ),
        )
    )
    return events


def _write_stream(path: Path, events: list[dict[str, Any]]) -> None:
    events.sort(key=lambda event: int(event["source_monotonic_ns"]))
    for sequence, event in enumerate(events, start=1):
        event["source_sequence"] = sequence
    path.write_text(
        "\n".join(_json_line(event) for event in events) + "\n",
        encoding="utf-8",
    )


def _runtime_parameters(profile: Mapping[str, Any]) -> dict[str, Any]:
    runtime = {
        "block_size": profile["block_size"],
        "pipeline_depth": profile["pipeline_depth"],
        "aggregation_timeout_ms": int(profile["aggregation_timeout_s"] * 1000),
        "leader_progress_timeout_ms": int(
            profile["leader_progress_timeout_s"] * 1000
        ),
        "leader_activation_grace_ms": int(
            profile["leader_activation_grace_s"] * 1000
        ),
        "activation_delay_blocks": profile["activation_delay_blocks"],
        "fanout": profile["fanout"],
        "epoch0_roots": list(profile["epoch0_roots"]),
        "tree_switch_period_blocks": profile["tree_switch_period_blocks"],
        "snapshot_seed": profile["snapshot_seed"],
        "manager_limits": dict(MANAGER_LIMITS),
    }
    if "transition_requests" in profile:
        runtime["transition_requests"] = recurring_transition_requests()
        runtime["throughput_windows"] = [
            {
                "phase": window["phase"],
                "epoch_number": window["epoch_number"],
                "bucket_count": 7,
            }
            for window in recurring_throughput_windows()
        ]
    else:
        runtime["successor_roots"] = list(profile["successor_roots"])
        runtime["successor_wait_exempt"] = list(
            profile["successor_wait_exempt"]
        )
    return runtime


def recurring_profile() -> dict[str, Any]:
    profile_path = Path(__file__).resolve().parents[1] / "profile.json"
    profile = json.loads(profile_path.read_bytes())
    profile["profile_id"] = "n7-f2-q5-crash-recovery-recurring-v3"
    for field in (
        "successor_epoch",
        "successor_roots",
        "successor_wait_exempt",
        "baseline_bucket_count",
        "post_bucket_count",
    ):
        profile.pop(field, None)
    profile["transition_requests"] = recurring_transition_requests()
    profile["throughput_windows"] = [
        {
            "phase": window["phase"],
            "epoch_number": window["epoch_number"],
            "bucket_count": 7,
        }
        for window in recurring_throughput_windows()
    ]
    return profile


def _write_runtime_artifacts(
    directory: Path, runtime: dict[str, Any]
) -> list[dict[str, Any]]:
    runtime_directory = directory / "runtime"
    runtime_directory.mkdir()
    binary_directory = runtime_directory / "bin"
    binary_directory.mkdir()
    config_directory = directory / "config"
    config_directory.mkdir()
    app_binary = binary_directory / "hotstuff-app"
    manager_binary = binary_directory / "adaptation-manager"
    app_binary.write_bytes(b"synthetic hotstuff app; never experiment evidence\n")
    manager_binary.write_bytes(
        b"synthetic adaptation manager; never experiment evidence\n"
    )
    runtime["executables"] = {
        "hotstuff_app": {
            "path": str(app_binary.resolve()),
            "sha256": hashlib.sha256(app_binary.read_bytes()).hexdigest(),
        },
        "adaptation_manager": {
            "path": str(manager_binary.resolve()),
            "sha256": hashlib.sha256(manager_binary.read_bytes()).hexdigest(),
        },
    }
    main_config = config_directory / "hotstuff.gen.conf"
    main_config.write_text(
        "\n".join(
            (
                f"block-size = {runtime['block_size']}",
                f"fan-out = {runtime['fanout']}",
                f"async_blocks = {runtime['pipeline_depth']}",
                f"aggregation-timeout = {runtime['aggregation_timeout_ms'] / 1000}",
                f"leader-progress-timeout = {runtime['leader_progress_timeout_ms'] / 1000}",
                f"leader-activation-grace = {runtime['leader_activation_grace_ms'] / 1000}",
                f"tree-switch-period = {runtime['tree_switch_period_blocks']}",
                "epoch-protocol-mode = adaptive_v2",
                f"epoch-change-minimum-activation-delay = {runtime['activation_delay_blocks']}",
                f"epoch-change-maximum-activation-delay = {runtime['activation_delay_blocks']}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    descriptors: list[dict[str, Any]] = []

    def write(
        relative: str, value: Mapping[str, Any], kind: str, replica_id: int | None
    ) -> None:
        path = directory / relative
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        descriptors.append(
            {
                "kind": kind,
                "replica_id": replica_id,
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )

    replica_options = {
        "block_size": runtime["block_size"],
        "pipeline_depth": runtime["pipeline_depth"],
        "aggregation_timeout_ms": runtime["aggregation_timeout_ms"],
        "leader_progress_timeout_ms": runtime["leader_progress_timeout_ms"],
        "leader_activation_grace_ms": runtime["leader_activation_grace_ms"],
        "fanout": runtime["fanout"],
        "tree_switch_period_blocks": runtime["tree_switch_period_blocks"],
    }
    issuer_hash = hashlib.sha256(b"synthetic issuer public key").hexdigest()
    manager_tls_hash = hashlib.sha256(b"synthetic manager TLS certificate").hexdigest()
    replica_tls_hashes = [
        hashlib.sha256(f"synthetic replica {replica} TLS certificate".encode()).hexdigest()
        for replica in range(7)
    ]
    for replica in range(7):
        replica_config = config_directory / f"replica-{replica}.conf"
        replica_config.write_text(
            f"idx = {replica}\nsynthetic-private-key = <test-only>\n",
            encoding="utf-8",
        )
        write(
            f"runtime/replica-{replica}.effective.json",
            {
                "schema_version": 1,
                "replica_id": replica,
                "protocol_mode": "adaptive-v2",
                "replica_count": 7,
                "fault_threshold": 2,
                "quorum": 5,
                "membership": list(range(7)),
                "authoritative_observer": "replica-2",
                **replica_options,
            },
            "replica_config",
            replica,
        )

    write(
        "runtime/epoch-input.json",
        {
            "schema_version": 1,
            "replica_count": 7,
            "membership": list(range(7)),
            "fault_threshold": 2,
            "quorum": 5,
            "epoch0_trees": [
                {
                    "tree_id": root,
                    "fanout": runtime["fanout"],
                    "pipeline_depth": runtime["pipeline_depth"],
                    "members_breadth_first": [
                        (root + offset) % 7 for offset in range(7)
                    ],
                    "wait_exempt": [],
                }
                for root in range(7)
            ],
        },
        "epoch_input",
        None,
    )
    manager_argv = [
        runtime["executables"]["adaptation_manager"]["path"],
        "--listen",
        "127.0.0.1:27000",
        "--tls-privkey",
        "<redacted>",
        "--tls-cert",
        "<fingerprinted>",
        "--issuer-id",
        "1",
        "--issuer-private-key",
        "<redacted>",
        "--activation-delay-blocks",
        str(runtime["activation_delay_blocks"]),
        "--structured-event-run-id",
        RUN_ID,
        "--structured-event-source-instance",
        "synthetic-manager-instance",
        "--structured-event-output",
        str((directory / "raw/adaptive-manager.jsonl").resolve()),
        "--convergence-deadline-seconds",
        "120",
    ]
    for replica in range(7):
        manager_argv.extend(
            ("--replica", f"{replica},127.0.0.1:0,<fingerprinted>")
        )
    write(
        "runtime/launch-arguments.json",
        {
            "schema_version": 1,
            "processes": [
                *[
                    {
                        "source_kind": "replica",
                        "source_id": f"replica-{replica}",
                        "argv": [
                            runtime["executables"]["hotstuff_app"]["path"],
                            "--conf",
                            str(main_config.resolve()),
                            "--conf",
                            str(
                                (config_directory / f"replica-{replica}.conf").resolve()
                            ),
                        ],
                        "effective_options": {
                            **replica_options,
                            "binary_sha256": runtime["executables"][
                                "hotstuff_app"
                            ]["sha256"],
                            "main_config_sha256": hashlib.sha256(
                                main_config.read_bytes()
                            ).hexdigest(),
                            "replica_config_sha256": hashlib.sha256(
                                (
                                    config_directory
                                    / f"replica-{replica}.conf"
                                ).read_bytes()
                            ).hexdigest(),
                            "bls_public_key_sha256": hashlib.sha256(
                                f"synthetic replica {replica} BLS key".encode()
                            ).hexdigest(),
                            "tls_certificate_sha256": replica_tls_hashes[replica],
                            "issuer_public_key_sha256": issuer_hash,
                            "manager_tls_certificate_sha256": manager_tls_hash,
                        },
                    }
                    for replica in range(7)
                ],
                {
                    "source_kind": "adaptation_manager",
                    "source_id": "adaptive-manager",
                    "argv": manager_argv,
                    "effective_options": {
                        "activation_delay_blocks": runtime[
                            "activation_delay_blocks"
                        ],
                        "snapshot_seed": runtime["snapshot_seed"],
                        "manager_limits": runtime["manager_limits"],
                        "binary_sha256": runtime["executables"][
                            "adaptation_manager"
                        ]["sha256"],
                        "tls_certificate_sha256": manager_tls_hash,
                        "issuer_public_key_sha256": issuer_hash,
                        "replica_tls_certificate_sha256": replica_tls_hashes,
                    },
                },
            ],
        },
        "launch_arguments",
        None,
    )
    return descriptors


def create_run(directory: Path) -> tuple[Path, Path]:
    """Create a complete synthetic PASS-shaped run under a pytest temp dir."""
    raw_directory = directory / "raw"
    raw_directory.mkdir(parents=True)
    sources: list[dict[str, Any]] = []
    boundary_evidence: list[dict[str, Any]] = []
    for replica in range(7):
        source_id = f"replica-{replica}"
        relative = f"raw/{source_id}.jsonl"
        replica_events = _replica_events(replica)
        _write_stream(directory / relative, replica_events)
        active = next(
            event
            for event in replica_events
            if event["event_type"] == "adaptive.configuration_active"
        )
        boundary_evidence.append(
            {
                "source_id": source_id,
                "source_sequence": active["source_sequence"],
                "source_monotonic_ns": active["source_monotonic_ns"],
            }
        )
        sources.append(
            {
                "source_kind": "replica",
                "source_id": source_id,
                "source_instance": f"synthetic-{source_id}-instance",
                "pid": 10_000 + replica,
                "pgid": 20_000 + replica,
                "path": relative,
            }
        )
    manager_relative = "raw/adaptive-manager.jsonl"
    _write_stream(directory / manager_relative, _manager_events())
    sources.append(
        {
            "source_kind": "adaptation_manager",
            "source_id": "adaptive-manager",
            "source_instance": "synthetic-manager-instance",
            "pid": 11_000,
            "pgid": 21_000,
            "path": manager_relative,
        }
    )

    profile_bytes = LEGACY_PROFILE_BYTES
    (directory / "profile.json").write_bytes(profile_bytes)
    profile = json.loads(profile_bytes)
    profile_sha = hashlib.sha256(profile_bytes).hexdigest()
    runtime = _runtime_parameters(profile)
    runtime_artifacts = _write_runtime_artifacts(directory, runtime)
    manifest = {
        "schema_version": 1,
        "scenario": "n7-crash-recovery",
        "run_id": RUN_ID,
        "kauri_revision": REVISION,
        "kauri_worktree_clean": True,
        "profile": {
            "identity": "n7-f2-q5-crash-recovery-v2",
            "path": "profile.json",
            "sha256": profile_sha,
        },
        "run_completion": {
            "complete": True,
            "interrupted": False,
            "runtime_error": None,
            "unexpected_survivor_exits": [],
        },
        "replica_count": 7,
        "fault_threshold": 2,
        "quorum": 5,
        "membership": list(range(7)),
        "authoritative_observer": "replica-2",
        "manager": {
            "source_id": "adaptive-manager",
            "receives_crash_ground_truth": False,
        },
        "bucket_width_ns": 5_000_000_000,
        "minimum_post_activation_grace_ns": (
            MINIMUM_POST_ACTIVATION_GRACE_NS
        ),
        "baseline_start_ns": BASELINE_NS,
        "end_ns": END_NS,
        "sources": sources,
        "runtime": runtime,
        "runtime_artifacts": runtime_artifacts,
        "crash_configuration_boundary": {
            "epoch_number": 0,
            "tree_id": 6,
            "root_replica": 6,
            "epoch_digest": EPOCH_0_DIGEST,
            "context_generation": None,
            "replica_evidence": boundary_evidence,
        },
        "crash_markers": [
            {
                "replica_id": replica,
                "pid": 10_000 + replica,
                "pgid": 20_000 + replica,
                "signal": "SIGKILL",
                "signal_number": 9,
                "requested_monotonic_raw_ns": requested,
                "confirmed_exit": {
                    "pid": 10_000 + replica,
                    "pgid": 20_000 + replica,
                    "signal": "SIGKILL",
                    "signal_number": 9,
                    "observed_monotonic_raw_ns": requested + 10_000_000,
                },
            }
            for replica, requested in ((0, CRASH_0_NS), (1, CRASH_1_NS))
        ],
    }
    manifest_path = directory / "manifest.json"
    epochs_path = directory / "epochs.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    epochs_path.write_text(json.dumps(epochs_document()), encoding="utf-8")
    return manifest_path, epochs_path


def create_recurring_run(directory: Path) -> tuple[Path, Path]:
    """Create the intentional M12-R02 three-epoch synthetic contract."""
    manifest_path, epochs_path = create_run(directory)
    manifest = load(manifest_path)

    for replica in range(7):
        _write_stream(
            directory / "raw" / f"replica-{replica}.jsonl",
            _recurring_replica_events(replica),
        )
    _write_stream(
        directory / "raw" / "adaptive-manager.jsonl",
        recurring_manager_events(),
    )

    profile_path = Path(__file__).resolve().parents[1] / "profile.json"
    profile_bytes = profile_path.read_bytes()
    profile = json.loads(profile_bytes)
    (directory / "profile.json").write_bytes(profile_bytes)
    manifest["profile"] = {
        "identity": profile["profile_id"],
        "path": "profile.json",
        "sha256": hashlib.sha256(profile_bytes).hexdigest(),
    }
    manifest["end_ns"] = RECURRING_END_NS
    manifest["transition_requests"] = recurring_transition_requests()
    manifest["throughput_windows"] = recurring_throughput_windows()
    manifest["manager"]["transition_artifact_ids"] = list(
        TRANSITION_ARTIFACT_IDS
    )
    manifest["runtime"].pop("successor_roots")
    manifest["runtime"].pop("successor_wait_exempt")
    manifest["runtime"]["transition_requests"] = recurring_transition_requests()
    manifest["runtime"]["throughput_windows"] = [
        {
            "phase": window["phase"],
            "epoch_number": window["epoch_number"],
            "bucket_count": 7,
        }
        for window in recurring_throughput_windows()
    ]

    artifacts = manifest["runtime_artifacts"]

    def write_artifact(
        relative_path: str,
        payload: bytes,
        *,
        kind: str,
    ) -> None:
        path = directory / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        artifacts.append(
            {
                "kind": kind,
                "replica_id": None,
                "path": relative_path,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )

    request_bytes = (
        json.dumps(
            {
                "schema_version": 1,
                "requests": recurring_transition_requests(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    write_artifact(
        "runtime/transition-requests.json",
        request_bytes,
        kind="transition_requests",
    )
    snapshots = recurring_evidence_snapshots()
    for transition_index, relative_path in enumerate(TRANSITION_BUNDLE_PATHS):
        write_artifact(
            relative_path,
            recurring_transition_bundle(transition_index),
            kind="transition_bundle",
        )
        snapshot_bytes = (
            json.dumps(snapshots[transition_index], indent=2, sort_keys=True)
            + "\n"
        ).encode()
        write_artifact(
            TRANSITION_SNAPSHOT_PATHS[transition_index],
            snapshot_bytes,
            kind="evidence_snapshot",
        )

    launch_path = directory / "runtime" / "launch-arguments.json"
    launch = load(launch_path)
    manager = launch["processes"][-1]
    for request in recurring_transition_requests():
        manager["argv"].extend(
            (
                "--transition-request",
                json.dumps(request, sort_keys=True, separators=(",", ":")),
                "--bundle-output",
                str((directory / request["bundle_path"]).resolve()),
            )
        )
    manager["effective_options"]["transition_requests"] = (
        recurring_transition_requests()
    )
    launch_path.write_text(
        json.dumps(launch, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    launch_artifact = next(
        artifact
        for artifact in artifacts
        if artifact["path"] == "runtime/launch-arguments.json"
    )
    launch_artifact["sha256"] = hashlib.sha256(
        launch_path.read_bytes()
    ).hexdigest()

    save(manifest_path, manifest)
    save(epochs_path, recurring_epochs_document())
    return manifest_path, epochs_path


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
