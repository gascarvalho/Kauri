"""Synthetic non-evidence run builder for validator and plotter tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


RUN_ID = "synthetic-validator-non-evidence"
REVISION = "1" * 40
EPOCH_0_DIGEST = "a" * 64
EPOCH_1_DIGEST = "b" * 64
COMMAND_HASH = f"{12:064x}"
PAYLOAD_DIGEST = "d" * 64
BASELINE_NS = 1_000_000_000
CRASH_0_NS = 36_000_000_000
CRASH_1_NS = 36_100_000_000
COMMAND_NS = 54_100_000_000
ACTIVATION_NS = 74_100_000_000
MINIMUM_POST_ACTIVATION_GRACE_NS = 1_000_000_000
END_NS = 111_006_000_000
MANAGER_READY_NS = ACTIVATION_NS + 10_000_000
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
    digest = EPOCH_0_DIGEST if epoch == 0 else EPOCH_1_DIGEST
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


def command_payload() -> dict[str, Any]:
    return {
        "command_block_height": 12,
        "command_block_hash": COMMAND_HASH,
        "payload_digest": PAYLOAD_DIGEST,
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": EPOCH_0_DIGEST,
        "successor_epoch_number": 1,
        "successor_epoch_digest": EPOCH_1_DIGEST,
        "activation_delay_blocks": 5,
        "activation_height": 17,
    }


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


def epochs_document() -> dict[str, Any]:
    initial_trees = [
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
    successor_trees = []
    for tree_id, root in enumerate(range(2, 7)):
        survivors = [replica for replica in range(2, 7) if replica != root]
        successor_trees.append(
            {
                "tree_id": tree_id,
                "fanout": 2,
                "members_breadth_first": [root, *survivors, 0, 1],
                "wait_exempt": [0, 1],
            }
        )
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
                "trees": initial_trees,
                "command": None,
            },
            {
                "epoch_number": 1,
                "epoch_digest": EPOCH_1_DIGEST,
                "trees": successor_trees,
                "command": command_payload(),
            },
        ],
    }


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


def _write_stream(path: Path, events: list[dict[str, Any]]) -> None:
    events.sort(key=lambda event: int(event["source_monotonic_ns"]))
    for sequence, event in enumerate(events, start=1):
        event["source_sequence"] = sequence
    path.write_text(
        "\n".join(_json_line(event) for event in events) + "\n",
        encoding="utf-8",
    )


def _runtime_parameters(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
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
        "successor_roots": list(profile["successor_roots"]),
        "successor_wait_exempt": list(profile["successor_wait_exempt"]),
        "tree_switch_period_blocks": profile["tree_switch_period_blocks"],
        "snapshot_seed": profile["snapshot_seed"],
        "manager_limits": dict(MANAGER_LIMITS),
    }


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
        "--tls-privkey",
        "<redacted>",
        "--tls-cert",
        "<fingerprinted>",
        "--issuer-private-key",
        "<redacted>",
        "--activation-delay-blocks",
        str(runtime["activation_delay_blocks"]),
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

    profile_bytes = (Path(__file__).resolve().parents[1] / "profile.json").read_bytes()
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


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
