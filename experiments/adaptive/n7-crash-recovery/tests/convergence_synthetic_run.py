"""Synthetic convergence-only runs; never experiment or thesis evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

import synthetic_run


RUN_ID = "synthetic-n7-epoch1-convergence"
REVISION = "0123456789abcdef0123456789abcdef01234567"
PROFILE_ID = "n7-f2-q5-epoch1-convergence-v3"
BASE_PROFILE_ID = "n7-f2-q5-crash-recovery-v2"
BASE_PROFILE_SHA256 = (
    "768c33418937f9b738c607b523ad847a7cb38220c95a499e82823ac41aa1e038"
)
MEMBERSHIP = list(range(7))
CRASH_TARGETS = [0, 1]
SURVIVORS = [2, 3, 4, 5, 6]
EPOCH_0_DIGEST = synthetic_run.EPOCH_0_DIGEST
EPOCH_1_DIGEST = synthetic_run.EPOCH_1_DIGEST
BUNDLE_BYTES = b"synthetic canonical epoch-1 bundle; never evidence\n"
BUNDLE_DIGEST = hashlib.sha256(BUNDLE_BYTES).hexdigest()
CONFIGURATION_BOUNDARY_NS = 1_800_000_000
CRASH_REQUEST_NS = {0: 1_900_000_000, 1: 1_910_000_000}
COMMON_SUCCESSOR_HEIGHT = 18
COMMON_SUCCESSOR_HASH = f"{COMMON_SUCCESSOR_HEIGHT:064x}"
COMMON_SUCCESSOR_NS = 4_206_000_000
ACK_RECOVERY_NS = 4_110_000_000


def profile_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profile_id": PROFILE_ID,
        "frozen": True,
        "base_profile": {
            "identity": BASE_PROFILE_ID,
            "sha256": BASE_PROFILE_SHA256,
        },
        "claim_scope": "epoch1_convergence_only",
        "replica_ids": MEMBERSHIP,
        "fault_threshold": 2,
        "quorum": 5,
        "crash_targets": CRASH_TARGETS,
        "survivors": SURVIVORS,
        "successor_epoch": 1,
        "activation_delay_blocks": 5,
        "fault_injection": {
            "bundle_delivery": {"recipient": 2, "attempt": 1},
            "activation_ack": {"accepted_activation_ordinal": 5},
        },
        "requirements": {
            "matching_activation_sources": 5,
            "exactly_one_converged": True,
            "exactly_one_ready": True,
            "require_post_ready_ack_retransmission": True,
            "require_common_successor_commit": True,
            "forbid_epoch_above": 1,
            "throughput_claim": False,
        },
        "timeouts": {
            "startup_s": 90,
            "phase_s": 240,
            "manager_convergence_deadline_s": 120,
            "crash_confirm_s": 5,
            "ack_drain_s": 2,
        },
    }


def convergence_identity(
    *, command_block_hash: str = synthetic_run.COMMAND_HASH
) -> dict[str, Any]:
    return {
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": EPOCH_0_DIGEST,
        "successor_epoch_number": 1,
        "successor_epoch_digest": EPOCH_1_DIGEST,
        "command_payload_digest": synthetic_run.PAYLOAD_DIGEST,
        "command_block_height": 12,
        "command_block_hash": command_block_hash,
        "activation_delay_blocks": 5,
        "activation_height": 17,
    }


def activation_payload_digest(replica: int) -> str:
    return hashlib.sha256(
        f"canonical epoch-1 activation observation replica {replica}".encode()
    ).hexdigest()


def crash_configuration_boundary() -> dict[str, Any]:
    return {
        "epoch_number": 0,
        "tree_id": 6,
        "root_replica": 6,
        "epoch_digest": EPOCH_0_DIGEST,
        "context_generation": None,
        "replica_evidence": [
            {
                "source_id": f"replica-{replica}",
                "source_sequence": 3,
                "source_monotonic_ns": (
                    CONFIGURATION_BOUNDARY_NS + replica * 1_000_000
                ),
            }
            for replica in MEMBERSHIP
        ],
    }


def crash_markers() -> list[dict[str, Any]]:
    return [
        {
            "replica_id": replica,
            "pid": 10_000 + replica,
            "pgid": 20_000 + replica,
            "signal": "SIGKILL",
            "signal_number": 9,
            "requested_monotonic_raw_ns": CRASH_REQUEST_NS[replica],
            "confirmed_exit": {
                "pid": 10_000 + replica,
                "pgid": 20_000 + replica,
                "signal": "SIGKILL",
                "signal_number": 9,
                "observed_monotonic_raw_ns": (
                    CRASH_REQUEST_NS[replica] + 10_000_000
                ),
            },
        }
        for replica in CRASH_TARGETS
    ]


def first_common_successor_commit() -> dict[str, Any]:
    return {
        "epoch_number": 1,
        "block_height": COMMON_SUCCESSOR_HEIGHT,
        "block_hash": COMMON_SUCCESSOR_HASH,
        "common_monotonic_ns": COMMON_SUCCESSOR_NS,
        "participants": SURVIVORS,
        "authoritative_observer": "replica-2",
    }


def _envelope(
    *,
    source_kind: str,
    source_id: str,
    source_instance: str,
    timestamp_ns: int,
    event_type: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    event = synthetic_run._envelope(  # noqa: SLF001 - shared test fixture
        source_kind=source_kind,
        source_id=source_id,
        source_instance=source_instance,
        timestamp_ns=timestamp_ns,
        event_type=event_type,
        payload=payload,
    )
    event["run_id"] = RUN_ID
    return event


def _convergence_payload(
    *,
    replica_id: int | None = None,
    delivery_attempt: int | None = None,
    disposition: str | None = None,
    identity: Mapping[str, Any] | None = None,
    commit_count: int = 0,
    activation_count: int = 0,
    canonical_payload_digest: str | None = None,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "replica_id": replica_id,
        "delivery_attempt": delivery_attempt,
        "disposition": disposition,
        "identity": None if identity is None else dict(identity),
        "accepted_commit_count": commit_count,
        "accepted_activation_count": activation_count,
        "required_activation_count": 5,
        "canonical_payload_digest": canonical_payload_digest,
        "failure_reason": failure_reason,
    }


def manager_events() -> list[dict[str, Any]]:
    kind = "adaptation_manager"
    source = "adaptive-manager"
    instance = "synthetic-convergence-manager"
    winner = convergence_identity()
    events = [
        _envelope(
            source_kind=kind,
            source_id=source,
            source_instance=instance,
            timestamp_ns=100_000_000,
            event_type="process.started",
            payload={"exit_status": None},
        ),
        _envelope(
            source_kind=kind,
            source_id=source,
            source_instance=instance,
            timestamp_ns=200_000_000,
            event_type="process.ready",
            payload={"exit_status": None},
        ),
        _envelope(
            source_kind=kind,
            source_id=source,
            source_instance=instance,
            timestamp_ns=1_000_000_000,
            event_type="adaptive_v2_delivery_attempt",
            payload=_convergence_payload(
                replica_id=2,
                delivery_attempt=1,
                disposition="injected_drop",
                canonical_payload_digest=BUNDLE_DIGEST,
            ),
        ),
    ]
    for replica in MEMBERSHIP:
        if replica == 2:
            continue
        events.append(
            _envelope(
                source_kind=kind,
                source_id=source,
                source_instance=instance,
                timestamp_ns=1_100_000_000 + replica * 1_000_000,
                event_type="adaptive_v2_delivery_attempt",
                payload=_convergence_payload(
                    replica_id=replica,
                    delivery_attempt=1,
                    disposition="enqueued",
                    canonical_payload_digest=BUNDLE_DIGEST,
                ),
            )
        )
    events.append(
        _envelope(
            source_kind=kind,
            source_id=source,
            source_instance=instance,
            timestamp_ns=1_500_000_000,
            event_type="adaptive_v2_delivery_attempt",
            payload=_convergence_payload(
                replica_id=2,
                delivery_attempt=2,
                disposition="enqueued",
                canonical_payload_digest=BUNDLE_DIGEST,
            ),
        )
    )

    for count, replica in enumerate(SURVIVORS, start=1):
        events.append(
            _envelope(
                source_kind=kind,
                source_id=source,
                source_instance=instance,
                timestamp_ns=2_000_000_000 + replica * 1_000_000,
                event_type="adaptive_v2_commit_observed",
                payload=_convergence_payload(
                    replica_id=replica,
                    disposition="accepted",
                    identity=winner,
                    commit_count=count,
                    canonical_payload_digest=hashlib.sha256(
                        f"commit-{replica}".encode()
                    ).hexdigest(),
                ),
            )
        )

    for count, replica in enumerate(SURVIVORS, start=1):
        digest = activation_payload_digest(replica)
        events.append(
            _envelope(
                source_kind=kind,
                source_id=source,
                source_instance=instance,
                timestamp_ns=3_000_000_000 + count * 100_000_000,
                event_type="adaptive_v2_activation_observed",
                payload=_convergence_payload(
                    replica_id=replica,
                    disposition="accepted",
                    identity=winner,
                    commit_count=5,
                    activation_count=count,
                    canonical_payload_digest=digest,
                ),
            )
        )
        if replica == SURVIVORS[-1]:
            events.append(
                _envelope(
                    source_kind=kind,
                    source_id=source,
                    source_instance=instance,
                    timestamp_ns=3_510_000_000,
                    event_type="adaptive_v2_activation_observed",
                    payload=_convergence_payload(
                        replica_id=replica,
                        disposition="ack_injected_drop",
                        identity=winner,
                        commit_count=5,
                        activation_count=count,
                        canonical_payload_digest=digest,
                    ),
                )
            )
        else:
            events.append(
                _envelope(
                    source_kind=kind,
                    source_id=source,
                    source_instance=instance,
                    timestamp_ns=3_010_000_000 + count * 100_000_000,
                    event_type="adaptive_v2_activation_observed",
                    payload=_convergence_payload(
                        replica_id=replica,
                        disposition="ack_sent",
                        identity=winner,
                        commit_count=5,
                        activation_count=count,
                        canonical_payload_digest=digest,
                    ),
                )
            )

    ready_payload = _convergence_payload(
        identity=winner,
        commit_count=5,
        activation_count=5,
    )
    events.extend(
        (
            _envelope(
                source_kind=kind,
                source_id=source,
                source_instance=instance,
                timestamp_ns=3_550_000_000,
                event_type="adaptive_v2_converged",
                payload=ready_payload,
            ),
            _envelope(
                source_kind=kind,
                source_id=source,
                source_instance=instance,
                timestamp_ns=3_560_000_000,
                event_type="adaptive_v2_ready",
                payload=ready_payload,
            ),
            _envelope(
                source_kind=kind,
                source_id=source,
                source_instance=instance,
                timestamp_ns=4_100_000_000,
                event_type="adaptive_v2_activation_observed",
                payload=_convergence_payload(
                    replica_id=SURVIVORS[-1],
                    disposition="duplicate",
                    identity=winner,
                    commit_count=5,
                    activation_count=5,
                    canonical_payload_digest=activation_payload_digest(
                        SURVIVORS[-1]
                    ),
                ),
            ),
            _envelope(
                source_kind=kind,
                source_id=source,
                source_instance=instance,
                timestamp_ns=ACK_RECOVERY_NS,
                event_type="adaptive_v2_activation_observed",
                payload=_convergence_payload(
                    replica_id=SURVIVORS[-1],
                    disposition="ack_sent",
                    identity=winner,
                    commit_count=5,
                    activation_count=5,
                    canonical_payload_digest=activation_payload_digest(
                        SURVIVORS[-1]
                    ),
                ),
            ),
            _envelope(
                source_kind=kind,
                source_id=source,
                source_instance=instance,
                timestamp_ns=4_500_000_000,
                event_type="process.stopping",
                payload={"exit_status": None},
            ),
            _envelope(
                source_kind=kind,
                source_id=source,
                source_instance=instance,
                timestamp_ns=4_600_000_000,
                event_type="process.stopped",
                payload={"exit_status": None},
            ),
        )
    )
    return events


def replica_events(replica: int) -> list[dict[str, Any]]:
    source = f"replica-{replica}"
    instance = f"synthetic-convergence-{source}"
    events = [
        _envelope(
            source_kind="replica",
            source_id=source,
            source_instance=instance,
            timestamp_ns=100_000_000 + replica,
            event_type="process.started",
            payload={"exit_status": None},
        ),
        _envelope(
            source_kind="replica",
            source_id=source,
            source_instance=instance,
            timestamp_ns=200_000_000 + replica,
            event_type="process.ready",
            payload={"exit_status": None},
        ),
    ]
    events.append(
        _envelope(
            source_kind="replica",
            source_id=source,
            source_instance=instance,
            timestamp_ns=CONFIGURATION_BOUNDARY_NS + replica * 1_000_000,
            event_type="adaptive.configuration_active",
            payload=synthetic_run._configuration_active_payload(replica),
        )
    )
    if replica in CRASH_TARGETS:
        return events

    events.extend(
        (
            _envelope(
                source_kind="replica",
                source_id=source,
                source_instance=instance,
                timestamp_ns=2_500_000_000 + replica * 1_000_000,
                event_type="epoch.command_committed",
                payload=synthetic_run.command_payload(),
            ),
            _envelope(
                source_kind="replica",
                source_id=source,
                source_instance=instance,
                timestamp_ns=3_000_000_000 + replica * 100_000_000,
                event_type="epoch.activated",
                payload={
                    "epoch_number": 1,
                    "tree_id": replica - 2,
                    "epoch_digest": EPOCH_1_DIGEST,
                    "activation_height": 17,
                },
            ),
            _envelope(
                source_kind="replica",
                source_id=source,
                source_instance=instance,
                timestamp_ns=4_200_000_000 + replica * 1_000_000,
                event_type="block.commit_observed",
                payload=synthetic_run._commit_observed_payload(
                    height=COMMON_SUCCESSOR_HEIGHT,
                    transaction_count=100,
                ),
            ),
            _envelope(
                source_kind="replica",
                source_id=source,
                source_instance=instance,
                timestamp_ns=4_200_000_000 + replica * 1_000_000,
                event_type="block.committed",
                payload=synthetic_run._commit_payload(
                    height=COMMON_SUCCESSOR_HEIGHT,
                    epoch=1,
                    tree=0,
                    transaction_count=100,
                    designated=replica == 2,
                ),
            ),
        )
    )
    events.extend(
        (
            _envelope(
                source_kind="replica",
                source_id=source,
                source_instance=instance,
                timestamp_ns=4_500_000_000 + replica,
                event_type="process.stopping",
                payload={"exit_status": None},
            ),
            _envelope(
                source_kind="replica",
                source_id=source,
                source_instance=instance,
                timestamp_ns=4_600_000_000 + replica,
                event_type="process.stopped",
                payload={"exit_status": None},
            ),
        )
    )
    return events


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    events.sort(key=lambda event: int(event["source_monotonic_ns"]))
    for sequence, event in enumerate(events, start=1):
        event["source_sequence"] = sequence
    path.write_text(
        "\n".join(
            json.dumps(event, separators=(",", ":")) for event in events
        )
        + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_run(directory: Path) -> tuple[Path, Path]:
    """Create the one canonical PASS-shaped convergence-only run."""
    raw = directory / "raw"
    raw.mkdir(parents=True)
    sources: list[dict[str, Any]] = []
    for replica in MEMBERSHIP:
        relative = f"raw/replica-{replica}.jsonl"
        path = directory / relative
        _write_jsonl(path, replica_events(replica))
        sources.append(
            {
                "source_kind": "replica",
                "source_id": f"replica-{replica}",
                "source_instance": f"synthetic-convergence-replica-{replica}",
                "pid": 10_000 + replica,
                "pgid": 20_000 + replica,
                "path": relative,
                "sha256": _sha256(path),
            }
        )
    manager_relative = "raw/adaptive-manager.jsonl"
    manager_path = directory / manager_relative
    _write_jsonl(manager_path, manager_events())
    sources.append(
        {
            "source_kind": "adaptation_manager",
            "source_id": "adaptive-manager",
            "source_instance": "synthetic-convergence-manager",
            "pid": 30_000,
            "pgid": 40_000,
            "path": manager_relative,
            "sha256": _sha256(manager_path),
        }
    )

    profile_path = directory / "convergence-profile.json"
    profile_path.write_bytes(
        (Path(__file__).resolve().parents[1] / "convergence-profile.json")
        .read_bytes()
    )
    bundle_path = directory / "successor.bundle"
    bundle_path.write_bytes(BUNDLE_BYTES)
    epochs_path = directory / "epochs.json"
    save(epochs_path, synthetic_run.epochs_document())
    runner_state_path = directory / "runner-state.json"
    save(
        runner_state_path,
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "manager_exit_code": 0,
            "ready_event_count": 1,
            "crash_markers": crash_markers(),
            "crash_configuration_boundary": crash_configuration_boundary(),
            "loss_controls": {
                "bundle_delivery": {
                    "recipient": 2,
                    "attempt": 1,
                    "observed": True,
                    "canonical_payload_digest": BUNDLE_DIGEST,
                },
                "activation_ack": {
                    "accepted_activation_ordinal": 5,
                    "observed": True,
                    "replica_id": SURVIVORS[-1],
                    "canonical_payload_digest": activation_payload_digest(
                        SURVIVORS[-1]
                    ),
                    "ack_source_monotonic_ns": ACK_RECOVERY_NS,
                },
            },
            "first_common_successor_commit": first_common_successor_commit(),
        },
    )

    artifacts = []
    for kind, path in (
        ("profile", profile_path),
        ("successor_bundle", bundle_path),
        ("epochs", epochs_path),
        ("runner_state", runner_state_path),
    ):
        artifacts.append(
            {
                "kind": kind,
                "path": path.relative_to(directory).as_posix(),
                "sha256": _sha256(path),
            }
        )
    manifest = {
        "schema_version": 1,
        "scenario": "n7-epoch1-convergence",
        "run_id": RUN_ID,
        "kauri_revision": REVISION,
        "kauri_worktree_clean": True,
        "profile": {
            "identity": PROFILE_ID,
            "path": "convergence-profile.json",
            "sha256": _sha256(profile_path),
        },
        "run_completion": {
            "complete": True,
            "interrupted": False,
            "runtime_error": None,
            "manager_exit_code": 0,
            "unexpected_survivor_exits": [],
        },
        "replica_count": 7,
        "fault_threshold": 2,
        "quorum": 5,
        "membership": MEMBERSHIP,
        "crash_targets": CRASH_TARGETS,
        "survivors": SURVIVORS,
        "successor_epoch": 1,
        "crash_markers": crash_markers(),
        "crash_configuration_boundary": crash_configuration_boundary(),
        "sources": sources,
        "artifacts": artifacts,
        "manager_argv": [
            "adaptation-manager",
            "--convergence-deadline-seconds",
            "120",
            "--experiment-drop-bundle-attempt",
            "2:1",
            "--experiment-drop-activation-ack",
            "5",
        ],
    }
    manifest_path = directory / "manifest.json"
    save(manifest_path, manifest)
    return manifest_path, epochs_path


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def rewrite_events(
    manifest_path: Path,
    source_id: str,
    mutate: Callable[[list[dict[str, Any]]], None],
) -> None:
    manifest = load(manifest_path)
    source = next(
        item for item in manifest["sources"] if item["source_id"] == source_id
    )
    path = manifest_path.parent / source["path"]
    events = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    mutate(events)
    _write_jsonl(path, events)
    source["sha256"] = _sha256(path)
    save(manifest_path, manifest)


def mutate_artifact(
    manifest_path: Path,
    kind: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    manifest = load(manifest_path)
    artifact = next(item for item in manifest["artifacts"] if item["kind"] == kind)
    path = manifest_path.parent / artifact["path"]
    value = load(path)
    mutate(value)
    save(path, value)
    artifact["sha256"] = _sha256(path)
    save(manifest_path, manifest)
