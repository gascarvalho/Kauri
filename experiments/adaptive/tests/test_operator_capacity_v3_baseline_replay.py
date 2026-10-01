import hashlib
import json
import struct

import pytest

from kauri_experiment import factorial_validation as fv
from kauri_experiment import operator_capacity_v3_baseline_replay as subject


def _fixture():
    events = []
    for target in range(31):
        for attempt in range(2):
            index = len(events) + 1
            reporter = (target + 1) % 31
            start = index * 1_000_000
            block = hashlib.sha256(f"block-{index}".encode()).hexdigest()
            identity = (fv._OBSERVATION_V3_DOMAIN + struct.pack('>HHII', reporter, target, 0, 0)
                        + bytes.fromhex('a' * 64) + bytes.fromhex(block)
                        + struct.pack('>BQQ', 1, start, 1_500_000))
            observation = {
                "schema_version": 3, "observation_id": hashlib.sha256(identity).hexdigest(),
                "reporter_id": reporter, "observed_replica_id": target,
                "configuration": {"epoch_number": 0, "tree_id": 0, "epoch_digest": 'a' * 64},
                "block_hash": block, "expected_message_type": "direct_vote", "outcome": "on_time",
                "response_duration_us": 10, "deadline_duration_us": 1_500_000,
                "reporter_monotonic_ns": start + 10_000, "reporter_sequence": attempt + 1,
                "attempt_start_monotonic_ns": start, "reporter_local_commit_monotonic_ns": 0,
                "signer_set": [target],
            }
            events.append({"event_schema_version": 1, "run_id": "baseline-unit",
                "source_kind": "adaptation_manager", "source_id": "adaptive-manager",
                "source_instance": "baseline-manager", "source_sequence": index,
                "source_monotonic_ns": start + 20_000, "event_type": "evidence.observation_accepted",
                "payload": {"ingestion_sequence": index * 2, "observation": observation}})
    consumption = {"run_id": "baseline-unit", "source_instance": "baseline-manager",
        "epoch0_digest": 'a' * 64, "baseline_evidence_cutoff": 124,
        "decision_monotonic_raw_ns": 100_000_000}
    native = [fv._NativeEvent(relative_path='fixture', line_number=i,
        source_kind=e['source_kind'], source_id=e['source_id'], source_instance=e['source_instance'],
        source_sequence=i, monotonic_ns=e['source_monotonic_ns'], event_type=e['event_type'],
        payload=e['payload'], line_sha256=hashlib.sha256(json.dumps(e).encode()).hexdigest())
        for i, e in enumerate(events, 1)]
    records = fv._accepted_evidence(native, 31, allow_ingestion_sequence_gaps=True, allowed_schema_versions={3})
    consumption['baseline_snapshot_id'] = fv._snapshot_id(records[(0, 'a' * 64)], replica_count=31,
        epoch_number=0, epoch_digest='a' * 64, cutoff=124, policy=subject.POLICY, seed=subject.SEED)
    return events, consumption


def _replay(events, record, argv=None):
    return subject.replay_operator_capacity_baseline(events, consumption_record=record, manager_argv=argv or ['manager'])


def test_reconstructs_all_responsive_prefix_with_legal_ingestion_gaps():
    events, record = _fixture()
    result = _replay(events, record)
    assert result['baseline_snapshot_id'] == record['baseline_snapshot_id']
    assert result['accepted_prefix_count'] == 62
    assert result['responsive_replica_ids'] == list(range(31))


@pytest.mark.parametrize('mutation', ['missing', 'snapshot', 'cutoff', 'decision', 'source', 'identity', 'duplicate'])
def test_rejects_incomplete_or_mutated_baseline(mutation):
    events, record = _fixture()
    if mutation == 'missing':
        events.pop()
    elif mutation == 'snapshot':
        record['baseline_snapshot_id'] = 'f' * 64
    elif mutation == 'cutoff':
        record['baseline_evidence_cutoff'] = 123
    elif mutation == 'decision':
        record['decision_monotonic_raw_ns'] = events[-1]['source_monotonic_ns'] - 1
    elif mutation == 'source':
        events[-1]['source_instance'] = 'restart'
    elif mutation == 'identity':
        events[-1]['payload']['observation']['observation_id'] = 'f' * 64
    elif mutation == 'duplicate':
        events[-1]['payload']['ingestion_sequence'] = events[-2]['payload']['ingestion_sequence']
    with pytest.raises(subject.BaselineReplayError):
        _replay(events, record)


@pytest.mark.parametrize('argv', [
    ['manager', '--responsiveness-minimum-attempts', '1'],
    ['manager', '--reputation-mechanism', 'latency_priority'],
    ['manager', '--responsiveness-minimum-attempts=1'],
    ['manager', '--responsiveness-minimum-attempts', '2', '--responsiveness-minimum-attempts', '2'],
])
def test_rejects_altered_or_ambiguous_policy(argv):
    events, record = _fixture()
    with pytest.raises(subject.BaselineReplayError, match='policy differs'):
        _replay(events, record, argv)


def test_insufficient_evidence_is_rejected_even_with_consistent_snapshot():
    events, record = _fixture()
    # Leave replica 30 with one attempt, then compute its consistent native ID.
    events.pop()
    native = [fv._NativeEvent(relative_path='fixture', line_number=i,
        source_kind=e['source_kind'], source_id=e['source_id'], source_instance=e['source_instance'],
        source_sequence=i, monotonic_ns=e['source_monotonic_ns'], event_type=e['event_type'],
        payload=e['payload'], line_sha256='a' * 64) for i, e in enumerate(events, 1)]
    records = fv._accepted_evidence(native, 31, allow_ingestion_sequence_gaps=True, allowed_schema_versions={3})
    record['baseline_snapshot_id'] = fv._snapshot_id(records[(0, 'a' * 64)], replica_count=31,
        epoch_number=0, epoch_digest='a' * 64, cutoff=124, policy=subject.POLICY, seed=subject.SEED)
    with pytest.raises(subject.BaselineReplayError, match='all 31'):
        _replay(events, record)
