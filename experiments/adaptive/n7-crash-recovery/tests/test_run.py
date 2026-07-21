"""Unit tests for the real N=7 campaign runner.

These tests use only synthetic bytes and process doubles.  They never launch
replicas, signal real processes, or produce experiment evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import signal
from types import SimpleNamespace
from typing import Any

import pytest

import run as campaign


def _u8(value: int) -> bytes:
    return value.to_bytes(1, "big")


def _u16(value: int) -> bytes:
    return value.to_bytes(2, "big")


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "big")


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "big")


def _component(value: bytes) -> bytes:
    return _u32(len(value)) + value


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _u32(len(encoded)) + encoded


def _synthetic_bundle(
    *,
    snapshot_seed: int = campaign.SNAPSHOT_SEED,
    root_order: tuple[int, ...] = campaign.SURVIVORS,
) -> bytes:
    predecessor = bytes.fromhex("aa" * 32)
    successor = bytes.fromhex("bb" * 32)
    command = b"".join(
        (
            campaign.AUTHORIZED_COMMAND_DOMAIN,
            _u32(1),
            _u8(2),
            _u32(7),
            _u32(1),
            predecessor,
            successor,
            _u64(5),
            bytes.fromhex("11" * 64),
        )
    )
    trees = []
    for tree_id, root in enumerate(root_order):
        members = [root, *[member for member in range(2, 7) if member != root], 0, 1]
        trees.append(
            b"".join(
                (
                    _u32(tree_id),
                    _u32(2),
                    _u32(2),
                    _u32(7),
                    b"".join(_u16(member) for member in members),
                    _u32(2),
                    _u16(0),
                    _u16(1),
                )
            )
        )
    definition = b"".join(
        (
            _u32(2),
            _u8(2),
            _u8(6),
            successor,
            _u32(2),
            _u32(1),
            predecessor,
            bytes.fromhex("cc" * 32),
            _u64(snapshot_seed),
            _string("adaptive-v2-performance-optimization-v1"),
            _string("synthetic-snapshot"),
            _u64(19),
            _u32(5),
            b"".join(trees),
        )
    )
    return b"".join(
        (
            campaign.BUNDLE_DOMAIN,
            _u32(1),
            _u8(2),
            _component(command),
            _component(definition),
        )
    )


def _event(
    *,
    sequence: int,
    timestamp_ns: int,
    height: int,
    epoch: int,
    tree: int,
    block_hash: str | None = None,
) -> dict[str, Any]:
    digest = "a" * 64 if epoch == 0 else "b" * 64
    block_hash = block_hash or f"{height:064x}"
    return {
        "event_schema_version": 1,
        "run_id": "synthetic-non-evidence",
        "source_kind": "replica",
        "source_id": "replica-2",
        "source_instance": "synthetic-instance",
        "source_sequence": sequence,
        "source_monotonic_ns": timestamp_ns,
        "event_type": "block.committed",
        "payload": {
            "block_height": height,
            "block_hash": block_hash,
            "parent_hash": None,
            "transaction_count": 1,
            "designated_observer": True,
            "decision_proof": {
                "epoch_number": epoch,
                "tree_id": tree,
                "epoch_digest": digest,
                "block_hash": block_hash,
            },
            "view_generation": 1,
            "commit_batch_index": 0,
        },
    }


def _commit_observed_event(
    *,
    replica: int,
    sequence: int,
    timestamp_ns: int,
    height: int,
    block_hash: str | None = None,
) -> dict[str, Any]:
    block_hash = block_hash or f"{height:064x}"
    return {
        "event_schema_version": 1,
        "run_id": "synthetic-non-evidence",
        "source_kind": "replica",
        "source_id": f"replica-{replica}",
        "source_instance": f"synthetic-replica-{replica}",
        "source_sequence": sequence,
        "source_monotonic_ns": timestamp_ns,
        "event_type": "block.commit_observed",
        "payload": {
            "block_height": height,
            "block_hash": block_hash,
            "parent_hash": None,
            "transaction_count": 1,
            "commit_batch_index": 0,
        },
    }


def _configuration_active_event(
    *,
    replica: int,
    sequence: int,
    timestamp_ns: int,
    tree: int,
    digest: str = "a" * 64,
) -> dict[str, Any]:
    return {
        "event_schema_version": 1,
        "run_id": "synthetic-non-evidence",
        "source_kind": "replica",
        "source_id": f"replica-{replica}",
        "source_instance": f"synthetic-replica-{replica}",
        "source_sequence": sequence,
        "source_monotonic_ns": timestamp_ns,
        "event_type": "adaptive.configuration_active",
        "payload": {
            "epoch_number": 0,
            "tree_id": tree,
            "epoch_digest": digest,
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
        },
    }


class _ProcessDouble:
    def __init__(self, return_code: int | None = None) -> None:
        self.return_code = return_code

    def poll(self) -> int | None:
        return self.return_code


def _record(replica: int, return_code: int | None = None) -> campaign.ProcessRecord:
    return campaign.ProcessRecord(
        name=f"replica-{replica}",
        pid=10_000 + replica,
        pgid=20_000 + replica,
        command=("hotstuff-app",),
        log_path=Path(f"replica-{replica}.log"),
        process=_ProcessDouble(return_code),  # type: ignore[arg-type]
        log_handle=SimpleNamespace(close=lambda: None),
        replica_id=replica,
    )


def test_decodes_exact_manager_bundle_without_inventing_topology() -> None:
    decoded = campaign.decode_epoch_change_bundle(_synthetic_bundle())

    assert decoded.command.issuer_id == 7
    assert decoded.command.successor_epoch_number == 1
    assert decoded.command.predecessor_epoch_digest == "aa" * 32
    assert decoded.command.successor_epoch_digest == "bb" * 32
    assert decoded.command.activation_delay_blocks == 5
    assert decoded.epoch_number == 1
    assert decoded.epoch_digest == "bb" * 32
    assert decoded.generation_seed == 0xA2F7
    assert [tree.members[0] for tree in decoded.trees] == [2, 3, 4, 5, 6]
    assert all(tree.wait_exempt == (0, 1) for tree in decoded.trees)


def test_preserves_ranked_successor_root_order() -> None:
    root_order = (2, 6, 3, 5, 4)

    decoded = campaign.decode_epoch_change_bundle(
        _synthetic_bundle(root_order=root_order)
    )

    assert tuple(tree.members[0] for tree in decoded.trees) == root_order


def test_rejects_duplicate_successor_root() -> None:
    with pytest.raises(campaign.RunnerError, match="five surviving replicas"):
        campaign.decode_epoch_change_bundle(
            _synthetic_bundle(root_order=(2, 2, 3, 4, 5))
        )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: b"wrong" + value[len(campaign.BUNDLE_DOMAIN) :],
        lambda value: value + b"trailing",
        lambda value: value[:-1],
    ),
)
def test_rejects_noncanonical_or_truncated_bundle(mutation: Any) -> None:
    with pytest.raises(campaign.RunnerError):
        campaign.decode_epoch_change_bundle(mutation(_synthetic_bundle()))


def test_rejects_bundle_with_noncanonical_snapshot_seed() -> None:
    with pytest.raises(campaign.RunnerError, match="command and definition identities"):
        campaign.decode_epoch_change_bundle(_synthetic_bundle(snapshot_seed=1))


def test_baseline_gate_requires_complete_common_root_cycle() -> None:
    events = [
        _event(
            sequence=index + 1,
            timestamp_ns=(index + 1) * 1_000_000_000,
            height=index + 1,
            epoch=0,
            tree=tree,
        )
        for index, tree in enumerate((0, 0, 1, 2, 3, 4, 5, 6))
    ]
    streams = {
        f"replica-{replica}": [
            _commit_observed_event(
                replica=replica,
                sequence=index + 1,
                timestamp_ns=(index + 1) * 1_000_000_000 + replica,
                height=int(event["payload"]["block_height"]),
                block_hash=str(event["payload"]["block_hash"]),
            )
            for index, event in enumerate(events)
        ]
        for replica in range(7)
    }
    common = campaign.commit_witness_timestamps(streams, tuple(range(7)))

    endpoint = campaign.find_common_root_cycle(
        events,
        common,
        participants=tuple(range(7)),
        epoch_number=0,
        tree_roots={replica: replica for replica in range(7)},
        expected_roots=tuple(range(7)),
        require_terminal=False,
    )

    assert endpoint is not None
    assert endpoint["payload"]["block_height"] == 8
    later_events = [
        *events,
        _event(
            sequence=9,
            timestamp_ns=9_000_000_000,
            height=9,
            epoch=0,
            tree=0,
        ),
    ]
    assert campaign.find_common_root_cycle(
        later_events,
        common,
        participants=tuple(range(7)),
        epoch_number=0,
        tree_roots={replica: replica for replica in range(7)},
        expected_roots=tuple(range(7)),
        require_terminal=False,
    ) is not None

    streams["replica-3"] = [
        event
        for event in streams["replica-3"]
        if event["payload"]["block_height"] != 4
    ]
    assert campaign.find_common_root_cycle(
        later_events,
        campaign.commit_witness_timestamps(streams, tuple(range(7))),
        participants=tuple(range(7)),
        epoch_number=0,
        tree_roots={replica: replica for replica in range(7)},
        expected_roots=tuple(range(7)),
        require_terminal=False,
    ) is None


def test_first_common_successor_commit_requires_every_survivor_witness() -> None:
    first = _event(
        sequence=1,
        timestamp_ns=1_000_000_000,
        height=10,
        epoch=1,
        tree=0,
    )
    second = _event(
        sequence=2,
        timestamp_ns=2_000_000_000,
        height=11,
        epoch=1,
        tree=1,
    )
    streams = {
        f"replica-{replica}": [
            _commit_observed_event(
                replica=replica,
                sequence=1,
                timestamp_ns=1_000_000_000 + replica,
                height=10,
                block_hash=str(first["payload"]["block_hash"]),
            ),
            _commit_observed_event(
                replica=replica,
                sequence=2,
                timestamp_ns=2_000_000_000 + replica,
                height=11,
                block_hash=str(second["payload"]["block_hash"]),
            ),
        ]
        for replica in campaign.SURVIVORS
    }
    streams["replica-6"] = streams["replica-6"][1:]

    result = campaign.find_first_common_epoch_commit(
        [first, second],
        campaign.commit_witness_timestamps(streams, campaign.SURVIVORS),
        participants=campaign.SURVIVORS,
        epoch_number=1,
    )

    assert result is not None
    assert result.observer_event == second
    assert result.common_ns == 2_000_000_006
    streams["replica-6"] = []
    assert campaign.find_first_common_epoch_commit(
        [first, second],
        campaign.commit_witness_timestamps(streams, campaign.SURVIVORS),
        participants=campaign.SURVIVORS,
        epoch_number=1,
    ) is None


def test_common_successor_deadline_includes_latest_survivor_witness() -> None:
    activation_ns = 1_000_000_000
    deadline_ns = activation_ns + 10_000_000_000
    observer_event = _event(
        sequence=1,
        timestamp_ns=10_000_000_000,
        height=10,
        epoch=1,
        tree=0,
    )
    streams = {
        f"replica-{replica}": [
            _commit_observed_event(
                replica=replica,
                sequence=1,
                timestamp_ns=(
                    12_000_000_000 if replica == 6 else 10_000_000_000 + replica
                ),
                height=10,
            )
        ]
        for replica in campaign.SURVIVORS
    }
    streams["replica-6"].append(
        _commit_observed_event(
            replica=6,
            sequence=2,
            timestamp_ns=13_000_000_000,
            height=10,
        )
    )

    result = campaign.find_first_common_epoch_commit(
        [observer_event],
        campaign.commit_witness_timestamps(streams, campaign.SURVIVORS),
        participants=campaign.SURVIVORS,
        epoch_number=1,
    )

    assert result is not None
    assert result.common_ns == 12_000_000_000
    with pytest.raises(campaign.RunnerError, match="exceeded"):
        campaign.enforce_common_epoch_commit_deadline(
            result,
            now_ns=result.common_ns,
            deadline_ns=deadline_ns,
        )
    with pytest.raises(campaign.RunnerError, match="exceeded"):
        campaign.enforce_common_epoch_commit_deadline(
            None,
            now_ns=deadline_ns + 1,
            deadline_ns=deadline_ns,
        )
    assert campaign.post_measurement_boundaries(
        activation_ns=activation_ns,
        first_common_successor_ns=result.common_ns,
        minimum_post_activation_grace_ns=1_000_000_000,
    ) == (2_000_000_000, 12_000_000_000)


@pytest.mark.parametrize(
    ("first_common_successor_ns", "expected"),
    (
        (120, (150, 150)),
        (180, (150, 180)),
    ),
)
def test_post_measurement_boundary_is_dynamic(
    first_common_successor_ns: int,
    expected: tuple[int, int],
) -> None:
    assert campaign.post_measurement_boundaries(
        activation_ns=100,
        first_common_successor_ns=first_common_successor_ns,
        minimum_post_activation_grace_ns=50,
    ) == expected


@pytest.mark.parametrize(
    "field",
    (
        campaign.MINIMUM_POST_ACTIVATION_GRACE_PROFILE_FIELD,
        campaign.MAXIMUM_ACTIVATION_TO_SUCCESSOR_PROFILE_FIELD,
    ),
)
def test_frozen_profile_timing_fields_are_positive_seconds(field: str) -> None:
    assert campaign._profile_duration_ns({field: 1}, field) == 1_000_000_000
    with pytest.raises(campaign.RunnerError, match=field):
        campaign._profile_duration_ns({field: 0}, field)
    with pytest.raises(campaign.RunnerError, match=field):
        campaign._profile_duration_ns({}, field)


def test_fresh_common_root_six_boundary_binds_all_sources() -> None:
    watermarks = {f"replica-{replica}": 10 for replica in range(7)}
    streams = {
        f"replica-{replica}": [
            _configuration_active_event(
                replica=replica,
                sequence=11,
                timestamp_ns=1_000_000_000 + replica,
                tree=6,
            )
        ]
        for replica in range(7)
    }

    boundary = campaign.common_active_configuration_boundary(
        streams,
        minimum_source_sequences=watermarks,
        epoch_number=0,
        tree_id=6,
        root_replica=6,
        members_breadth_first=(6, 0, 1, 2, 3, 4, 5),
        fanout=2,
        maximum_skew_ns=500_000_000,
        observed_ns=1_100_000_000,
    )

    assert boundary is not None
    assert boundary["epoch_number"] == 0
    assert boundary["tree_id"] == 6
    assert boundary["root_replica"] == 6
    assert boundary["epoch_digest"] == "a" * 64
    assert boundary["context_generation"] is None
    assert [item["source_id"] for item in boundary["replica_evidence"]] == [
        f"replica-{replica}" for replica in range(7)
    ]
    campaign.assert_crash_boundary_held(
        boundary,
        streams,
        crash_request_ns=1_300_000_000,
    )

    streams["replica-2"].append(
        _event(
            sequence=12,
            timestamp_ns=1_000_000_004,
            height=1,
            epoch=0,
            tree=0,
        )
    )
    with pytest.raises(campaign.RunnerError, match="authoritative root changed"):
        campaign.assert_crash_boundary_held(
            boundary,
            streams,
            crash_request_ns=1_300_000_000,
        )
    streams["replica-2"].pop()

    streams["replica-3"].append(
        _configuration_active_event(
            replica=3,
            sequence=12,
            timestamp_ns=1_200_000_000,
            tree=0,
        )
    )
    assert campaign.common_active_configuration_boundary(
        streams,
        minimum_source_sequences=watermarks,
        epoch_number=0,
        tree_id=6,
        root_replica=6,
        members_breadth_first=(6, 0, 1, 2, 3, 4, 5),
        fanout=2,
        maximum_skew_ns=500_000_000,
        observed_ns=1_300_000_000,
    ) is None
    with pytest.raises(campaign.RunnerError, match="configuration changed"):
        campaign.assert_crash_boundary_held(
            boundary,
            streams,
            crash_request_ns=1_300_000_000,
        )


def test_configuration_poller_tails_fresh_events_incrementally(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    for replica in range(7):
        history = [
            _configuration_active_event(
                replica=replica,
                sequence=sequence,
                timestamp_ns=sequence * 1_000_000 + replica,
                tree=6,
            )
            for sequence in range(1, 513)
        ]
        (raw / f"replica-{replica}.jsonl").write_text(
            "".join(
                json.dumps(event, separators=(",", ":")) + "\n"
                for event in history
            ),
            encoding="utf-8",
        )

    watermarks, offsets = campaign.replica_event_tail_snapshot(tmp_path)
    assert set(watermarks.values()) == {512}
    poller = campaign.FreshConfigurationPoller(
        tmp_path,
        watermarks,
        start_offsets=offsets,
        maximum_skew_ns=500_000_000,
        clock_ns=lambda: 1_100_000_000,
    )
    assert poller.poll() is None

    for replica in range(7):
        event = _configuration_active_event(
            replica=replica,
            sequence=513,
            timestamp_ns=1_000_000_000 + replica,
            tree=6,
        )
        with (raw / f"replica-{replica}.jsonl").open(
            "a", encoding="utf-8"
        ) as stream:
            stream.write(json.dumps(event, separators=(",", ":")) + "\n")

    boundary = poller.poll()

    assert boundary is not None
    assert boundary["tree_id"] == 6
    assert len(boundary["replica_evidence"]) == 7


def test_crash_injection_targets_only_registered_replica_groups() -> None:
    records = {replica: _record(replica, -signal.SIGKILL) for replica in range(7)}
    killed: list[tuple[int, int]] = []
    samples = iter((101, 102, 201, 202))

    markers = campaign.inject_sigkill_crashes(
        records,
        (0, 1),
        timeout_s=1,
        clock_ns=lambda: next(samples),
        kill_group=lambda pgid, sig: killed.append((pgid, sig)),
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )

    assert killed == [(20_000, signal.SIGKILL), (20_001, signal.SIGKILL)]
    assert [marker["replica_id"] for marker in markers] == [0, 1]
    assert [marker["requested_monotonic_raw_ns"] for marker in markers] == [101, 102]
    assert [marker["confirmed_exit"]["observed_monotonic_raw_ns"] for marker in markers] == [201, 202]


def test_manager_command_has_no_crash_ground_truth(tmp_path: Path) -> None:
    tls = [
        {"crt": f"crt-{index}", "sec": f"sec-{index}", "cid": f"cid-{index}"}
        for index in range(8)
    ]
    command = campaign.build_manager_command(
        Path("/bin/adaptation-manager"),
        replicas_tls=tls[:7],
        manager_tls=tls[7],
        issuer={"pub": "issuer-pub", "sec": "issuer-sec"},
        manager_port=27000,
        peer_port=25000,
        activation_delay_blocks=5,
        run_id="synthetic-non-evidence",
        source_instance="synthetic-manager-instance",
        structured_event_path=tmp_path / "manager.jsonl",
        bundle_path=tmp_path / "successor.bundle",
    )

    assert "--crash-target" not in command
    assert "--crash-replicas" not in command
    assert command.count("--replica") == 7
    assert "--issuer-private-key" in command
    assert "--structured-event-output" in command


def test_process_check_accepts_only_guarded_clean_manager_exit(
    tmp_path: Path,
) -> None:
    manager = campaign.ProcessRecord(
        name=campaign.MANAGER_SOURCE_ID,
        pid=11_000,
        pgid=21_000,
        command=("adaptation-manager",),
        log_path=tmp_path / "manager.log",
        process=_ProcessDouble(0),  # type: ignore[arg-type]
        log_handle=SimpleNamespace(close=lambda: None),
        replica_id=None,
    )

    with pytest.raises(campaign.RunnerError, match="exited unexpectedly"):
        campaign._check_processes([manager], set())

    campaign._check_processes(
        [manager],
        set(),
        allow_clean_exit=lambda record: record.name == campaign.MANAGER_SOURCE_ID,
    )

    manager.process.return_code = 1  # type: ignore[attr-defined]
    with pytest.raises(campaign.RunnerError, match="status 1"):
        campaign._check_processes(
            [manager],
            set(),
            allow_clean_exit=lambda _: True,
        )


def test_manager_clean_exit_gate_requires_one_canonical_ready_event() -> None:
    identity = {
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": "a" * 64,
        "successor_epoch_number": 1,
        "successor_epoch_digest": "b" * 64,
        "command_payload_digest": "c" * 64,
        "command_block_height": 12,
        "command_block_hash": "d" * 64,
        "activation_delay_blocks": 5,
        "activation_height": 17,
    }
    ready = {
        "source_sequence": 10,
        "source_monotonic_ns": 20,
        "event_type": "adaptive_v2_ready",
        "payload": {
            "replica_id": None,
            "delivery_attempt": None,
            "disposition": None,
            "identity": identity,
            "accepted_commit_count": 3,
            "accepted_activation_count": 5,
            "required_activation_count": 5,
            "canonical_payload_digest": None,
            "failure_reason": None,
        },
    }

    assert campaign.manager_convergence_ready_event([]) is None
    assert campaign.manager_convergence_ready_event([ready]) == ready
    with pytest.raises(campaign.RunnerError, match="duplicate"):
        campaign.manager_convergence_ready_event([ready, ready])
    with pytest.raises(campaign.RunnerError, match="convergence_failure"):
        campaign.manager_convergence_ready_event(
            [{**ready, "event_type": "adaptive_v2_convergence_failure"}]
        )
    invalid = json.loads(json.dumps(ready))
    invalid["payload"]["accepted_activation_count"] = 4
    with pytest.raises(campaign.RunnerError, match="fixed quorum"):
        campaign.manager_convergence_ready_event([invalid])
    invalid = json.loads(json.dumps(ready))
    invalid["payload"]["required_activation_count"] = 5.0
    with pytest.raises(campaign.RunnerError, match="fixed quorum"):
        campaign.manager_convergence_ready_event([invalid])


def test_runtime_inputs_bind_five_block_delay_and_one_block_rotation(
    tmp_path: Path,
) -> None:
    for directory in ("config", "raw", "logs"):
        (tmp_path / directory).mkdir()
    profile = {
        "block_size": 1,
        "pipeline_depth": 2,
        "aggregation_timeout_s": 0.5,
        "leader_progress_timeout_s": 5.0,
        "leader_activation_grace_s": 1.0,
        "activation_delay_blocks": 5,
        "fanout": 2,
        "tree_switch_period_blocks": 1,
        "snapshot_seed": campaign.SNAPSHOT_SEED,
    }
    bls = [
        {
            "pub": f"{replica + 1:02x}" * 48,
            "sec": f"{replica + 17:02x}" * 32,
        }
        for replica in range(7)
    ]
    tls = [
        {
            "crt": f"{replica + 33:02x}" * 64,
            "sec": f"{replica + 49:02x}" * 64,
            "cid": f"cid-{replica}",
        }
        for replica in range(8)
    ]
    issuer = {"pub": "61" * 33, "sec": "62" * 32}
    app_binary = tmp_path / "hotstuff-app"
    manager_binary = tmp_path / "adaptation-manager"
    app_binary.write_bytes(b"synthetic hotstuff app executable")
    manager_binary.write_bytes(b"synthetic adaptation manager executable")
    instances = {
        **{f"replica-{replica}": f"instance-{replica}" for replica in range(7)},
        "adaptive-manager": "manager-instance",
    }

    main_config, _, manager_command, replica_commands, artifacts = (
        campaign.write_runtime_inputs(
            tmp_path,
            profile,
            bls,
            tls,
            issuer,
            peer_port=25_000,
            client_port=26_000,
            manager_port=27_000,
            run_id="synthetic-non-evidence",
            source_instances=instances,
            app_binary=app_binary,
            manager_binary=manager_binary,
        )
    )

    config = main_config.read_text(encoding="utf-8")
    assert "tree-switch-period = 1\n" in config
    assert "epoch-change-minimum-activation-delay = 5\n" in config
    assert "epoch-change-maximum-activation-delay = 5\n" in config
    assert hashlib.sha256(main_config.read_bytes()).hexdigest() == (
        "3cc7809056713164037a4f9bc11a9e8cc2e32ea1984601813f5c2776962d3508"
    )
    replica_zero_payload = (
        tmp_path / "config" / "replica-0.conf"
    ).read_bytes().replace(
        str(tmp_path / "raw").encode(),
        b"/synthetic/run/raw",
    )
    assert hashlib.sha256(replica_zero_payload).hexdigest() == (
        "a6e9412cd67b07d70062c20b56f8dc916828e06c57dcbccf6a7d86d9df510be3"
    )
    assert manager_command[manager_command.index("--activation-delay-blocks") + 1] == "5"
    assert manager_command.count("--convergence-deadline-seconds") == 1
    assert manager_command[
        manager_command.index("--convergence-deadline-seconds") + 1
    ] == str(campaign.MANAGER_CONVERGENCE_DEADLINE_S)
    assert len(replica_commands) == 7
    assert [artifact["path"] for artifact in artifacts] == [
        *[f"runtime/replica-{replica}.effective.json" for replica in range(7)],
        "runtime/epoch-input.json",
        "runtime/launch-arguments.json",
    ]
    effective = json.loads(
        (tmp_path / "runtime" / "replica-0.effective.json").read_text()
    )
    assert effective["tree_switch_period_blocks"] == 1
    assert hashlib.sha256(
        (tmp_path / "runtime" / "replica-0.effective.json").read_bytes()
    ).hexdigest() == (
        "8f61ecdc9ff9abd1a6d3c01d1c1925a82924b6bc10409522a08a038b00d4c37f"
    )
    assert hashlib.sha256(
        (tmp_path / "runtime" / "epoch-input.json").read_bytes()
    ).hexdigest() == (
        "97d85da2d5395f6c443f89a8271f395f53c6d7e62f54d3f1d2447e3b19331850"
    )
    launch = json.loads(
        (tmp_path / "runtime" / "launch-arguments.json").read_text()
    )
    manager_launch = launch["processes"][-1]
    assert manager_launch["argv"].count("--convergence-deadline-seconds") == 1
    assert manager_launch["argv"][
        manager_launch["argv"].index("--convergence-deadline-seconds") + 1
    ] == str(campaign.MANAGER_CONVERGENCE_DEADLINE_S)
    assert manager_launch["effective_options"]["activation_delay_blocks"] == 5
    assert manager_launch["effective_options"]["snapshot_seed"] == 0xA2F7
    assert manager_launch["effective_options"]["manager_limits"] == (
        campaign.MANAGER_LIMITS
    )
    assert manager_launch["argv"][manager_launch["argv"].index("--tls-privkey") + 1] == (
        "<redacted>"
    )
    assert manager_launch["argv"][
        manager_launch["argv"].index("--issuer-private-key") + 1
    ] == "<redacted>"
    assert manager_launch["argv"][manager_launch["argv"].index("--tls-cert") + 1] == (
        "<fingerprinted>"
    )
    replica_arguments = [
        manager_launch["argv"][index + 1]
        for index, value in enumerate(manager_launch["argv"])
        if value == "--replica"
    ]
    assert len(replica_arguments) == 7
    assert all(value.endswith(",<fingerprinted>") for value in replica_arguments)

    runtime = campaign.runtime_parameters(
        profile,
        app_binary=app_binary,
        manager_binary=manager_binary,
    )
    assert runtime["snapshot_seed"] == 0xA2F7
    assert runtime["manager_limits"] == campaign.MANAGER_LIMITS
    assert runtime["executables"]["hotstuff_app"]["sha256"] == hashlib.sha256(
        app_binary.read_bytes()
    ).hexdigest()
    assert launch["processes"][0]["effective_options"]["binary_sha256"] == (
        runtime["executables"]["hotstuff_app"]["sha256"]
    )
    assert launch["processes"][0]["effective_options"]["main_config_sha256"] == (
        hashlib.sha256(main_config.read_bytes()).hexdigest()
    )
    assert launch["processes"][0]["effective_options"][
        "replica_config_sha256"
    ] == hashlib.sha256((tmp_path / "config" / "replica-0.conf").read_bytes()).hexdigest()

    records = [_record(replica) for replica in range(7)]
    manager = campaign.ProcessRecord(
        name="adaptive-manager",
        pid=11_000,
        pgid=21_000,
        command=manager_command,
        log_path=tmp_path / "manager.log",
        process=_ProcessDouble(),  # type: ignore[arg-type]
        log_handle=SimpleNamespace(close=lambda: None),
        replica_id=None,
    )
    manifest = campaign.build_manifest(
        run_id="synthetic-non-evidence",
        revision="2" * 40,
        profile_bytes=b"{}\n",
        records=[*records, manager],
        source_instances=instances,
        minimum_post_activation_grace_ns=1_000_000_000,
        baseline_start_ns=100,
        end_ns=200,
        crash_markers=[],
        complete=False,
        interrupted=False,
        runtime_error="synthetic stop",
        unexpected_survivor_exits=[],
        runtime=runtime,
        runtime_artifacts=artifacts,
    )
    persisted_evidence = "\n".join(
        [
            *(path.read_text(encoding="utf-8") for path in (tmp_path / "runtime").iterdir()),
            json.dumps(manifest),
        ]
    )
    for secret in [
        *(identity["sec"] for identity in bls),
        *(identity["sec"] for identity in tls),
        issuer["sec"],
    ]:
        assert secret not in persisted_evidence


def test_repository_gate_requires_fixed_clean_pushed_branch(tmp_path: Path) -> None:
    revision = "1" * 40

    def clean_git(arguments: tuple[str, ...]) -> str:
        if arguments == ("rev-parse", "--show-toplevel"):
            return str(tmp_path)
        if arguments == ("branch", "--show-current"):
            return campaign.REQUIRED_BRANCH
        if arguments == ("rev-parse", "HEAD"):
            return revision
        if arguments == ("rev-parse", f"origin/{campaign.REQUIRED_BRANCH}"):
            return revision
        if arguments[:2] == ("status", "--porcelain=v1"):
            return ""
        raise AssertionError(arguments)

    snapshot = campaign.verify_repository_state(tmp_path, git=clean_git)
    assert snapshot.revision == revision
    assert snapshot.worktree_clean is True

    def dirty_git(arguments: tuple[str, ...]) -> str:
        if arguments[:2] == ("status", "--porcelain=v1"):
            return " M src/hotstuff.cpp"
        return clean_git(arguments)

    with pytest.raises(campaign.RunnerError, match="worktree is not clean"):
        campaign.verify_repository_state(tmp_path, git=dirty_git)


def test_manifest_uses_validator_schema_and_explicit_false_ground_truth(tmp_path: Path) -> None:
    profile = b'{"profile_id":"n7-f2-q5-crash-recovery-v1"}\n'
    records = [_record(replica) for replica in range(7)]
    manager = campaign.ProcessRecord(
        name="adaptive-manager",
        pid=11_000,
        pgid=21_000,
        command=("adaptation-manager",),
        log_path=tmp_path / "manager.log",
        process=_ProcessDouble(),  # type: ignore[arg-type]
        log_handle=SimpleNamespace(close=lambda: None),
        replica_id=None,
    )
    instances = {f"replica-{replica}": f"instance-{replica}" for replica in range(7)}
    instances["adaptive-manager"] = "manager-instance"

    manifest = campaign.build_manifest(
        run_id="synthetic-non-evidence",
        revision="2" * 40,
        profile_bytes=profile,
        records=[*records, manager],
        source_instances=instances,
        minimum_post_activation_grace_ns=1_000_000_000,
        baseline_start_ns=100,
        end_ns=200,
        crash_markers=[],
        complete=False,
        interrupted=False,
        runtime_error="synthetic stop",
        unexpected_survivor_exits=[],
    )

    assert set(manifest) == campaign.MANIFEST_FIELDS
    assert manifest["manager"] == {
        "source_id": "adaptive-manager",
        "receives_crash_ground_truth": False,
    }
    assert manifest["replica_count"] == 7
    assert manifest["fault_threshold"] == 2
    assert manifest["quorum"] == 5
    assert manifest["minimum_post_activation_grace_ns"] == 1_000_000_000
    assert manifest["profile"]["sha256"] == hashlib.sha256(profile).hexdigest()
    assert len(manifest["sources"]) == 8
    json.dumps(manifest)
