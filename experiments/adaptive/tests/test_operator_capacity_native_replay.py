"""Synthetic native-schema checks for the W18 common-commit component."""

from __future__ import annotations

from copy import deepcopy

import pytest

from kauri_experiment.operator_capacity_native_replay import (
    NativeReplayError,
    replay_post_e1_common_commits,
)


RUN = "w18-test-cell"
E1 = "b" * 64
BLOCK = "c" * 64
PARENT = "a" * 64


def _event(replica: int, sequence: int, when: int, kind: str, payload: dict) -> dict:
    return {
        "event_schema_version": 1,
        "run_id": RUN,
        "source_kind": "replica",
        "source_id": f"replica-{replica}",
        "source_instance": f"{RUN}-replica-{replica}",
        "source_sequence": sequence,
        "source_monotonic_ns": when,
        "event_type": kind,
        "payload": payload,
    }


def _streams() -> dict[int, list[dict]]:
    result: dict[int, list[dict]] = {}
    for replica in range(31):
        activation = {
            "epoch_number": 1,
            "tree_id": 0,
            "epoch_digest": E1,
            "activation_height": 10,
            "certificate_apply_committed_height": 9,
            "activation_readiness_certificate_digest": "d" * 64,
        }
        observed = {
            "block_height": 11,
            "block_hash": BLOCK,
            "parent_hash": PARENT,
            "transaction_count": 1,
            # Native batch indexes are reporter-local, not comparable.
            "commit_batch_index": replica,
        }
        result[replica] = [
            _event(replica, 1, 100 + replica, "epoch.activated", activation),
            _event(replica, 2, 200 + replica, "block.commit_observed", observed),
        ]
    rich = {
        **result[0][1]["payload"],
        "designated_observer": True,
        "decision_proof": {
            "epoch_number": 1,
            "tree_id": 0,
            "epoch_digest": E1,
            "block_hash": BLOCK,
        },
        "view_generation": 1,
    }
    result[0].append(_event(0, 3, 201, "block.committed", rich))
    return result


def _replay(streams: dict[int, list[dict]]):
    return replay_post_e1_common_commits(
        streams, run_id=RUN, epoch1_digest=E1,
        window_start_ns=130, window_end_ns=300,
    )


def test_replays_only_complete_31_replica_e1_commit() -> None:
    commits = _replay(_streams())
    assert [(item.height, item.block_hash, item.completion_ns) for item in commits] == [
        (11, BLOCK, 230)
    ]


def test_censors_incomplete_right_boundary_without_inflating_throughput() -> None:
    streams = _streams()
    streams[30][1]["source_monotonic_ns"] = 300
    assert _replay(streams) == ()


@pytest.mark.parametrize("replica", [0, 1])
def test_censors_commit_with_a_peer_witness_before_the_common_window(replica: int) -> None:
    streams = _streams()
    streams[replica][1]["source_monotonic_ns"] = 129
    assert _replay(streams) == ()


def test_empty_pipeline_block_is_valid_but_unscored() -> None:
    streams = _streams()
    for events in streams.values():
        events[1]["payload"]["transaction_count"] = 0
    streams[0][2]["payload"]["transaction_count"] = 0
    assert _replay(streams) == ()


@pytest.mark.parametrize("count", [2])
def test_rejects_counted_e1_commit_without_exactly_one_synthetic_command(count: int) -> None:
    streams = _streams()
    for events in streams.values():
        events[1]["payload"]["transaction_count"] = count
    streams[0][2]["payload"]["transaction_count"] = count
    with pytest.raises(NativeReplayError, match="exactly one synthetic command"):
        _replay(streams)


def test_rejects_conflicting_commit_hash_and_identity() -> None:
    streams = _streams()
    streams[30][1]["payload"]["block_hash"] = "e" * 64
    with pytest.raises(NativeReplayError, match="conflicting commit hashes"):
        _replay(streams)


def test_rejects_missing_or_late_activation() -> None:
    streams = _streams()
    del streams[30][0]
    with pytest.raises(NativeReplayError, match="all 31 E1 activations"):
        _replay(streams)
    streams = _streams()
    streams[30][0]["source_monotonic_ns"] = 131
    with pytest.raises(NativeReplayError, match="native stream order regressed|all 31 E1 activations"):
        _replay(streams)


def test_rejects_source_restart_and_noncanonical_payload() -> None:
    streams = _streams()
    streams[0][2]["source_instance"] = "restart"
    with pytest.raises(NativeReplayError, match="source restarted"):
        _replay(streams)
    streams = deepcopy(_streams())
    del streams[0][0]["payload"]["activation_readiness_certificate_digest"]
    with pytest.raises(NativeReplayError, match="activation payload drifted"):
        _replay(streams)


def test_rejects_unpaired_designated_rich_commit() -> None:
    streams = _streams()
    streams[0][2]["source_sequence"] = 4
    with pytest.raises(NativeReplayError, match="adjacent local observation"):
        _replay(streams)
