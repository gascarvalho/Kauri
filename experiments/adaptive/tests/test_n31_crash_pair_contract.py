"""Red-first contracts for the focused N=31 crash-pair evidence boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import importlib
import json
from typing import Any, Mapping, Sequence

import pytest

from experiments.adaptive.kauri_experiment import factorial_validation


SUBJECT = "experiments.adaptive.kauri_experiment.n31_crash_pair"
EXPERIMENT_ID = "n31-f5-q21-three-crash-pair-v1"
N = 31
FC = 10
Q = 21
FANOUT = 5
PIPELINE = 2
TREE_ID = 20
CRASHED = (22, 23, 24)
SURVIVORS = tuple(replica for replica in range(N) if replica not in CRASHED)
COMMON_WITNESSES = SURVIVORS[:Q]
E0_DIGEST = "145fac093343fa9cff20fcf49d85ad5443e93db14146f7854b17e28cf44f6d7a"
MEMBERSHIP_DIGEST = "107f6e39481091529f50db1c6e32a72518cf50a971b074506bed095cb09769d9"
RUN_ID = "n31-pair-contract"
EVIDENCE_CUTOFF = N
ISSUER_PRIVATE_KEY = 1
ISSUER_PUBLIC_KEY = "02" + f"{factorial_validation._SECP256K1_GX:064x}"


def _subject() -> Any:
    return importlib.import_module(SUBJECT)


def _document(value: Any) -> dict[str, Any]:
    result = asdict(value) if hasattr(value, "__dataclass_fields__") else value
    assert isinstance(result, dict)
    return result


def _canonical(value: object) -> bytes:
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


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _epoch_zero_witness() -> dict[str, object]:
    return {
        "schema": "kauri-adaptive-v2-epoch-profile-digest-v1",
        "replica_count": N,
        "fault_threshold": FC,
        "quorum": Q,
        "fanout": FANOUT,
        "pipeline_stretch": PIPELINE,
        "membership": list(range(N)),
        "epoch_zero": {
            "schema_version": 2,
            "epoch_number": 0,
            "previous_epoch_digest": "0" * 64,
            "membership_digest": MEMBERSHIP_DIGEST,
            "activation_height": 0,
            "generation_seed": 0,
            "policy_version": "adaptive-v2-bootstrap",
            "evidence_snapshot_id": "adaptive-v2-bootstrap-epoch-zero",
            "evidence_cutoff": 0,
            "canonical_size_bytes": 2720,
            "epoch_digest": E0_DIGEST,
            "tree_count": N,
            "trees": [
                {
                    "tree_id": root,
                    "fanout": FANOUT,
                    "pipeline_stretch": PIPELINE,
                    "members_breadth_first": [
                        (root + offset) % N for offset in range(N)
                    ],
                    "wait_exempt_leaves": [],
                }
                for root in range(N)
            ],
        },
    }


def test_native_epoch_zero_witness_proves_the_frozen_topology() -> None:
    proof = _document(
        _subject().validate_epoch0_topology_proof(
            _epoch_zero_witness(),
            active_tree_id=TREE_ID,
            candidate_ids=CRASHED,
        )
    )
    expected_identity = {
        "experiment_id": EXPERIMENT_ID,
        "replica_count": N,
        "fault_threshold": FC,
        "quorum": Q,
        "fanout": FANOUT,
        "pipeline_stretch": PIPELINE,
        "epoch_number": 0,
        "generation_seed": 0,
        "epoch_digest": E0_DIGEST,
        "membership_digest": MEMBERSHIP_DIGEST,
        "active_tree_id": TREE_ID,
        "target_replica_ids": list(CRASHED),
    }
    assert all(proof[key] == value for key, value in expected_identity.items())

    members = tuple((TREE_ID + offset) % N for offset in range(N))
    expected_roles = {
        str(replica): {
            "breadth_first_position": position,
            "depth": 0 if position == 0 else 1 if position <= 5 else 2,
            "role": (
                "root" if position == 0 else "internal" if position <= 5 else "leaf"
            ),
        }
        for position, replica in enumerate(members)
    }
    expected_descendants = {
        str(replica): (
            list(members[1:])
            if position == 0
            else list(members[5 * position + 1 : 5 * position + 6])
        )
        for position, replica in enumerate(members[:6])
    }
    assert proof["role_by_replica_id"] == expected_roles
    assert proof["descendant_ids_by_internal_replica_id"] == expected_descendants
    assert proof["pairwise_disjoint_target_descendants"] is True
    assert proof["topology_sha256"] == _sha(
        _epoch_zero_witness()["epoch_zero"]["trees"][TREE_ID]  # type: ignore[index]
    )


@pytest.mark.parametrize("mutation", ("shape", "identity", "tree"))
def test_epoch_zero_proof_rejects_any_nonfrozen_native_witness(mutation: str) -> None:
    witness = deepcopy(_epoch_zero_witness())
    if mutation == "shape":
        witness["fault_threshold"] = FC - 1
    elif mutation == "identity":
        witness["epoch_zero"]["generation_seed"] = 1  # type: ignore[index]
    else:
        tree = witness["epoch_zero"]["trees"][TREE_ID]  # type: ignore[index]
        tree["members_breadth_first"][1:3] = reversed(  # type: ignore[index]
            tree["members_breadth_first"][1:3]  # type: ignore[index]
        )
    with pytest.raises(_subject().N31CrashPairError):
        _subject().validate_epoch0_topology_proof(
            witness,
            active_tree_id=TREE_ID,
            candidate_ids=CRASHED,
        )


def _envelope(
    source_id: str,
    sequence: int,
    timestamp_ns: int,
    event_type: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    return {
        "event_schema_version": 1,
        "run_id": RUN_ID,
        "source_kind": (
            "replica" if source_id.startswith("replica-") else "adaptation_manager"
        ),
        "source_id": source_id,
        "source_instance": f"{RUN_ID}-{source_id}",
        "source_sequence": sequence,
        "source_monotonic_ns": timestamp_ns,
        "event_type": event_type,
        "payload": dict(payload),
    }


def _commit_payload(height: int, block_hash: str, transactions: int) -> dict[str, object]:
    return {
        "block_height": height,
        "block_hash": block_hash,
        "parent_hash": f"{height - 1:064x}",
        "transaction_count": transactions,
        "commit_batch_index": 0,
    }


def _runtime_streams() -> dict[str, list[dict[str, object]]]:
    _, epoch1, _, _ = _native_epoch_chain()
    commit = _commit_payload(101, "a" * 64, 1_000)
    command = {
        "command_block_height": 96,
        "command_block_hash": f"{96:064x}",
        "payload_digest": epoch1.command.payload_digest,
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": epoch1.previous_epoch_digest,
        "successor_epoch_number": epoch1.epoch_number,
        "successor_epoch_digest": epoch1.epoch_digest,
        "activation_delay_blocks": epoch1.command.activation_delay_blocks,
        "activation_height": 101,
    }
    transition = {
        "epoch_number": 1,
        "tree_id": 0,
        "epoch_digest": E1_DIGEST,
        "activation_height": 101,
    }
    streams: dict[str, list[dict[str, object]]] = {}
    for replica in SURVIVORS:
        source = f"replica-{replica}"
        events: list[dict[str, object]] = []
        if replica in COMMON_WITNESSES:
            events.append(
                _envelope(
                    source,
                    len(events) + 1,
                    2_000 + replica,
                    "block.commit_observed",
                    commit,
                )
            )
        if replica == 0:
            events.append(
                _envelope(
                    source,
                    len(events) + 1,
                    2_500,
                    "block.committed",
                    {
                        **commit,
                        "designated_observer": True,
                        "decision_proof": {
                            "epoch_number": 0,
                            "tree_id": TREE_ID,
                            "epoch_digest": E0_DIGEST,
                            "block_hash": commit["block_hash"],
                        },
                        "view_generation": 1,
                    },
                )
            )
        events.append(
            _envelope(
                source,
                len(events) + 1,
                3_000 + replica,
                "epoch.command_committed",
                command,
            )
        )
        events.append(
            _envelope(
                source,
                len(events) + 1,
                4_000 + replica,
                "epoch.activated",
                transition,
            )
        )
        streams[source] = events
    return streams


def test_runtime_graph_binds_authoritative_q21_and_all_28_transition_evidence() -> None:
    proof = _document(
        _subject().validate_runtime_evidence_graph(
            _runtime_streams(),
            membership_replica_ids=tuple(range(N)),
            crashed_replica_ids=CRASHED,
            authoritative_replica_id=0,
            quorum=Q,
            expected_epoch_digest=E1_DIGEST,
        )
    )
    assert proof["authoritative_commit"]["event_type"] == "block.committed"
    assert len(proof["common_commit_observations"]) == Q
    assert proof["common_commit_witness_replica_ids"] == list(COMMON_WITNESSES)
    assert proof["transition_witness_replica_ids"] == list(SURVIVORS)
    assert proof["source_sequence_contiguous"] is True


def _runtime_streams_with_native_command() -> tuple[
    dict[str, list[dict[str, object]]], bytes, Any
]:
    epoch1_wire, epoch1, _, _ = _native_epoch_chain()
    return _runtime_streams(), epoch1_wire, epoch1


def test_runtime_graph_rejects_invalid_or_causally_early_activation() -> None:
    streams, epoch1_wire, epoch1 = _runtime_streams_with_native_command()
    decoded = factorial_validation.decode_epoch_change_bundle(
        epoch1_wire,
        issuer_public_key=ISSUER_PUBLIC_KEY,
    )
    assert decoded == epoch1
    assert decoded.epoch_digest == E1_DIGEST
    assert tuple(tree.tree_id for tree in decoded.trees) == tuple(range(Q))

    commands = [
        event["payload"]
        for events in streams.values()
        for event in events
        if event["event_type"] == "epoch.command_committed"
    ]
    assert len(commands) == len(SURVIVORS)
    assert all(command == commands[0] for command in commands)
    assert commands[0] == {
        "command_block_height": 96,
        "command_block_hash": f"{96:064x}",
        "payload_digest": decoded.command.payload_digest,
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": decoded.previous_epoch_digest,
        "successor_epoch_number": decoded.epoch_number,
        "successor_epoch_digest": decoded.epoch_digest,
        "activation_delay_blocks": decoded.command.activation_delay_blocks,
        "activation_height": 101,
    }
    baseline = _document(
        _subject().validate_runtime_evidence_graph(
            streams,
            membership_replica_ids=tuple(range(N)),
            crashed_replica_ids=CRASHED,
            authoritative_replica_id=0,
            quorum=Q,
            expected_epoch_digest=decoded.epoch_digest,
        )
    )
    assert baseline["passed"] is True
    assert baseline["transition_witness_replica_ids"] == list(SURVIVORS)

    def all_activations(
        changed: dict[str, list[dict[str, object]]],
    ) -> list[dict[str, object]]:
        return [
            event
            for events in changed.values()
            for event in events
            if event["event_type"] == "epoch.activated"
        ]

    mutations: dict[str, dict[str, list[dict[str, object]]]] = {}
    for name, field, value in (
        ("non-integer-tree", "tree_id", "bogus"),
        ("out-of-range-tree", "tree_id", Q),
        ("negative-activation-height", "activation_height", -1),
    ):
        changed = deepcopy(streams)
        for event in all_activations(changed):
            event["payload"][field] = value  # type: ignore[index]
        mutations[name] = changed

    early = deepcopy(streams)
    witness_events = early["replica-1"]
    for event in witness_events:
        if event["event_type"] == "epoch.command_committed":
            event["source_monotonic_ns"] = 1_700
        elif event["event_type"] == "epoch.activated":
            event["source_monotonic_ns"] = 1_800
    witness_events.sort(key=lambda event: int(event["source_monotonic_ns"]))
    for sequence, event in enumerate(witness_events, start=1):
        event["source_sequence"] = sequence
    mutations["activation-before-common-commit"] = early

    unexpectedly_accepted: list[str] = []
    for name, changed in mutations.items():
        try:
            _subject().validate_runtime_evidence_graph(
                changed,
                membership_replica_ids=tuple(range(N)),
                crashed_replica_ids=CRASHED,
                authoritative_replica_id=0,
                quorum=Q,
                expected_epoch_digest=decoded.epoch_digest,
            )
        except _subject().N31CrashPairError:
            continue
        unexpectedly_accepted.append(name)
    assert unexpectedly_accepted == []


def test_runtime_graph_requires_epoch_command_from_every_survivor() -> None:
    streams, _, epoch1 = _runtime_streams_with_native_command()
    baseline = _document(
        _subject().validate_runtime_evidence_graph(
            streams,
            membership_replica_ids=tuple(range(N)),
            crashed_replica_ids=CRASHED,
            authoritative_replica_id=0,
            quorum=Q,
            expected_epoch_digest=epoch1.epoch_digest,
        )
    )
    assert baseline["passed"] is True
    assert sum(
        event["event_type"] == "epoch.command_committed"
        for events in streams.values()
        for event in events
    ) == len(SURVIVORS)

    without_commands = deepcopy(streams)
    for events in without_commands.values():
        events[:] = [
            event
            for event in events
            if event["event_type"] != "epoch.command_committed"
        ]
        for sequence, event in enumerate(events, start=1):
            event["source_sequence"] = sequence
    with pytest.raises(_subject().N31CrashPairError):
        _subject().validate_runtime_evidence_graph(
            without_commands,
            membership_replica_ids=tuple(range(N)),
            crashed_replica_ids=CRASHED,
            authoritative_replica_id=0,
            quorum=Q,
            expected_epoch_digest=epoch1.epoch_digest,
        )


@pytest.mark.parametrize("mutation", ("sequence", "commit", "transition"))
def test_runtime_graph_rejects_source_or_survivor_binding_drift(mutation: str) -> None:
    streams = deepcopy(_runtime_streams())
    if mutation == "sequence":
        streams["replica-1"][1]["source_sequence"] = 99
    elif mutation == "commit":
        streams[f"replica-{COMMON_WITNESSES[-1]}"][0]["payload"][  # type: ignore[index]
            "block_hash"
        ] = "b" * 64
    else:
        streams[f"replica-{SURVIVORS[-1]}"].pop()
    with pytest.raises(_subject().N31CrashPairError):
        _subject().validate_runtime_evidence_graph(
            streams,
            membership_replica_ids=tuple(range(N)),
            crashed_replica_ids=CRASHED,
            authoritative_replica_id=0,
            quorum=Q,
            expected_epoch_digest=E1_DIGEST,
        )


def _fault_evidence() -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    plan = {
        "schema_version": 1,
        "seed": 41_719,
        "scenario": {
            "replica_ids": list(range(N)),
            "quorum": Q,
            "crash_budget": FC,
            "successor_bundle_retry_limit": 1,
        },
        "actions": [
            {
                "fault_id": f"crash-replica-{replica}",
                "kind": "replica_group_sigkill",
                "replica_id": replica,
            }
            for replica in CRASHED
        ],
    }
    records = [
        {
            "name": f"replica-{replica}",
            "replica_id": replica,
            "pid": 20_000 + replica,
            "pgid": 20_000 + replica,
        }
        for replica in CRASHED
    ]
    outcomes = [
        {
            "fault_id": f"crash-replica-{replica}",
            "name": f"replica-{replica}",
            "replica_id": replica,
            "pid": 20_000 + replica,
            "pgid": 20_000 + replica,
            "signal_number": 9,
            "returncode": -9,
            "requested_monotonic_ns": 10_000 + ordinal,
            "confirmed_monotonic_ns": 20_000 + ordinal,
        }
        for ordinal, replica in enumerate(CRASHED)
    ]
    plan_sha256 = hashlib.sha256(_canonical(plan).rstrip(b"\n")).hexdigest()
    journal: list[dict[str, object]] = []
    for replica in CRASHED:
        journal.append(
            {
                "schema_version": 1,
                "source_id": "fault-orchestrator",
                "source_sequence": len(journal),
                "source_monotonic_ns": 9_000 + len(journal),
                "plan_sha256": plan_sha256,
                "fault_id": f"crash-replica-{replica}",
                "lifecycle": "started",
            }
        )
    for outcome in outcomes:
        journal.append(
            {
                "schema_version": 1,
                "source_id": "fault-orchestrator",
                "source_sequence": len(journal),
                "source_monotonic_ns": 21_000 + len(journal),
                "plan_sha256": plan_sha256,
                "fault_id": outcome["fault_id"],
                "lifecycle": "terminal",
                "outcome": {**outcome, "status": "succeeded"},
            }
        )
    return plan, records, outcomes, journal


def test_atomic_sigkill_validation_joins_plan_registry_outcomes_and_journal() -> None:
    plan, records, outcomes, journal = _fault_evidence()
    proof = _document(
        _subject().validate_atomic_sigkill_evidence(
            fault_plan=plan,
            process_records=records,
            outcomes=outcomes,
            journal_events=journal,
        )
    )
    assert proof["passed"] is True
    assert proof["target_replica_ids"] == list(CRASHED)
    assert proof["all_requests_before_any_confirmation"] is True
    assert max(row["requested_monotonic_ns"] for row in outcomes) < min(
        row["confirmed_monotonic_ns"] for row in outcomes
    )


@pytest.mark.parametrize("mutation", ("pgid", "timing", "outcome", "journal"))
def test_atomic_sigkill_validation_rejects_runtime_evidence_drift(mutation: str) -> None:
    plan, records, outcomes, journal = _fault_evidence()
    if mutation == "pgid":
        outcomes[0]["pgid"] = outcomes[1]["pgid"]
    elif mutation == "timing":
        outcomes[0]["confirmed_monotonic_ns"] = 10_001
    elif mutation == "outcome":
        outcomes[0]["returncode"] = 0
    else:
        journal.pop()
    with pytest.raises(_subject().N31CrashPairError):
        _subject().validate_atomic_sigkill_evidence(
            fault_plan=plan,
            process_records=records,
            outcomes=outcomes,
            journal_events=journal,
        )


@pytest.mark.parametrize(
    "manager_argv,manager_input",
    (
        (("manager", "--crash-targets", "22,23,24"), {"evidence_path": "runtime.jsonl"}),
        (("manager",), {"target_pgids": [20_022, 20_023, 20_024]}),
    ),
)
def test_manager_boundary_rejects_fault_truth(
    manager_argv: Sequence[str], manager_input: Mapping[str, object]
) -> None:
    plan, _, _, _ = _fault_evidence()
    safe_argv = (
        "manager",
        "--runtime-evidence",
        "raw/runtime-events.jsonl",
        "--command-output",
        "raw/manager-commands.jsonl",
    )
    safe_input = {
        "input_source": "authenticated_runtime_evidence_only",
        "runtime_evidence_path": "raw/runtime-events.jsonl",
        "command_output_path": "raw/manager-commands.jsonl",
    }
    proof = _document(
        _subject().validate_manager_blinding(
            fault_plan=plan,
            manager_cli_args=safe_argv,
            manager_input=safe_input,
        )
    )
    assert proof["blinded"] is True
    assert proof["manager_cli_args_fault_truth_free"] is True
    assert proof["manager_input_fault_truth_free"] is True
    assert proof["input_source"] == "authenticated_runtime_evidence_only"

    with pytest.raises(_subject().N31CrashPairError):
        _subject().validate_manager_blinding(
            fault_plan=plan,
            manager_cli_args=manager_argv,
            manager_input=manager_input,
        )


def _observation_id(
    *,
    reporter_id: int,
    observed_replica_id: int,
    block_hash: str,
) -> str:
    payload = b"".join(
        (
            b"kauri-response-observation-v1",
            reporter_id.to_bytes(2, "big"),
            observed_replica_id.to_bytes(2, "big"),
            (1).to_bytes(4, "big"),
            (0).to_bytes(4, "big"),
            bytes.fromhex(E1_DIGEST),
            bytes.fromhex(block_hash),
            (1).to_bytes(1, "big"),
        )
    )
    return hashlib.sha256(payload).hexdigest()


def _response_latency(replica: int) -> int:
    rank = RANKED_SURVIVORS.index(replica)
    return 100 if replica in {0, 1} else 100 + rank


def _accepted_ranking_evidence() -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for sequence, replica in enumerate(range(N), start=1):
        eligible = replica not in CRASHED
        outcome = "on_time" if eligible else "timeout"
        latency = _response_latency(replica) if eligible else 0
        block_hash = "9" * 64
        events.append(
            _envelope(
                "adaptive-manager",
                sequence,
                30_000 + sequence,
                "evidence.observation_accepted",
                {
                    "ingestion_sequence": sequence,
                    "observation": {
                        "schema_version": 1,
                        "observation_id": _observation_id(
                            reporter_id=30,
                            observed_replica_id=replica,
                            block_hash=block_hash,
                        ),
                        "reporter_id": 30,
                        "observed_replica_id": replica,
                        "configuration": {
                            "epoch_number": 1,
                            "tree_id": 0,
                            "epoch_digest": E1_DIGEST,
                        },
                        "block_hash": block_hash,
                        "expected_message_type": "direct_vote",
                        "outcome": outcome,
                        "response_duration_us": latency if eligible else 0,
                        "deadline_duration_us": 1_000,
                        "reporter_monotonic_ns": 29_000 + sequence,
                        "reporter_sequence": sequence,
                        "signer_set": [replica] if eligible else [],
                    },
                },
            )
        )
    return events


def _expected_score_rows() -> list[dict[str, object]]:
    return [
        {
            "replica_id": replica,
            "eligible": replica in SURVIVORS,
            "attempt_count": 1,
            "response_rate_ppm": 1_000_000 if replica in SURVIVORS else 0,
            "timeout_rate_ppm": 0 if replica in SURVIVORS else 1_000_000,
            "latency_percentile_us": (
                _response_latency(replica)
            )
            if replica in SURVIVORS
            else None,
            "score_ppm": (
                1_000_000 - _response_latency(replica)
                if replica in SURVIVORS
                else -1
            ),
        }
        for replica in range(N)
    ]


def test_ranking_is_rebuilt_from_fresh_accepted_evidence_with_id_tie_break() -> None:
    ranking = _document(
        _subject().rebuild_epoch2_ranking(
            _accepted_ranking_evidence(),
            source_epoch_digest=E1_DIGEST,
            policy_version="responsive-rank-v1",
            evidence_cutoff=EVIDENCE_CUTOFF,
            window_start_ns=25_000,
            window_end_ns=35_000,
            membership_replica_ids=tuple(range(N)),
        )
    )
    expected = list(RANKED_SURVIVORS[:Q])
    assert ranking["ranked_eligible_replica_ids"][:Q] == expected
    assert ranking["tie_break"] == "score_desc_replica_id_asc_v1"
    assert ranking["evidence_cutoff"] == EVIDENCE_CUTOFF
    assert ranking["policy_version"] == "responsive-rank-v1"
    assert ranking["score_rows"] == _expected_score_rows()


def test_ranking_requires_one_monotonic_authenticated_manager_stream() -> None:
    evidence = _accepted_ranking_evidence()
    baseline = _document(
        _subject().rebuild_epoch2_ranking(
            evidence,
            source_epoch_digest=E1_DIGEST,
            policy_version="responsive-rank-v1",
            evidence_cutoff=EVIDENCE_CUTOFF,
            window_start_ns=25_000,
            window_end_ns=35_000,
            membership_replica_ids=tuple(range(N)),
        )
    )
    assert baseline["source_epoch_digest"] == E1_DIGEST
    assert baseline["evidence_cutoff"] == EVIDENCE_CUTOFF

    mutations: dict[str, list[dict[str, object]]] = {}
    instance_drift = deepcopy(evidence)
    instance_drift[1]["source_instance"] = f"{RUN_ID}-adaptive-manager-restarted"
    mutations["source-instance-drift"] = instance_drift

    envelope_regression = deepcopy(evidence)
    envelope_regression[0]["source_monotonic_ns"] = 30_050
    mutations["envelope-timestamp-regression"] = envelope_regression

    reporter_before = deepcopy(evidence)
    reporter_before[0]["payload"]["observation"][  # type: ignore[index]
        "reporter_monotonic_ns"
    ] = 24_999
    mutations["reporter-before-window"] = reporter_before

    reporter_after = deepcopy(evidence)
    reporter_after[-1]["payload"]["observation"][  # type: ignore[index]
        "reporter_monotonic_ns"
    ] = 35_000
    mutations["reporter-after-window"] = reporter_after

    configuration_drift = deepcopy(evidence)
    configuration_drift[0]["payload"]["observation"]["configuration"][  # type: ignore[index]
        "epoch_number"
    ] = 2
    mutations["configuration-epoch-drift"] = configuration_drift

    unexpectedly_accepted: list[str] = []
    for name, changed in mutations.items():
        try:
            _subject().rebuild_epoch2_ranking(
                changed,
                source_epoch_digest=E1_DIGEST,
                policy_version="responsive-rank-v1",
                evidence_cutoff=EVIDENCE_CUTOFF,
                window_start_ns=25_000,
                window_end_ns=35_000,
                membership_replica_ids=tuple(range(N)),
            )
        except _subject().N31CrashPairError:
            continue
        unexpectedly_accepted.append(name)
    assert unexpectedly_accepted == []


@pytest.mark.parametrize("mutation", ("policy", "cutoff", "freshness", "tie"))
def test_ranking_rejects_unbound_or_noncanonical_evidence(mutation: str) -> None:
    evidence = deepcopy(_accepted_ranking_evidence())
    policy_version = "responsive-rank-v1"
    evidence_cutoff = EVIDENCE_CUTOFF
    if mutation == "policy":
        policy_version = "unreviewed-policy"
    elif mutation == "cutoff":
        evidence_cutoff -= 1
    elif mutation == "freshness":
        evidence[0]["source_monotonic_ns"] = 24_999
    else:
        evidence[1]["payload"]["observation"][  # type: ignore[index]
            "observed_replica_id"
        ] = 0
    with pytest.raises(_subject().N31CrashPairError):
        _subject().rebuild_epoch2_ranking(
            evidence,
            source_epoch_digest=E1_DIGEST,
            policy_version=policy_version,
            evidence_cutoff=evidence_cutoff,
            window_start_ns=25_000,
            window_end_ns=35_000,
            membership_replica_ids=tuple(range(N)),
        )


def _canonical_trees(
    roots: Sequence[int], wait_exempt: Sequence[int]
) -> list[dict[str, object]]:
    root_order = tuple(roots)
    trees: list[dict[str, object]] = []
    for tree_id, root in enumerate(root_order):
        internal = tuple(replica for replica in SURVIVORS if replica != root)[
            :FANOUT
        ]
        leaf_survivors = tuple(
            replica
            for replica in SURVIVORS
            if replica not in (root, *internal)
        )
        trees.append(
            {
                "tree_id": tree_id,
                "fanout": FANOUT,
                "pipeline_stretch": PIPELINE,
                "members": [root, *internal, *leaf_survivors, *CRASHED],
                "wait_exempt": sorted(wait_exempt),
            }
        )
    return trees


def _u(value: int, size: int) -> bytes:
    return value.to_bytes(size, "big")


def _component(value: bytes) -> bytes:
    return _u(len(value), 4) + value


def _string(value: str) -> bytes:
    return _component(value.encode("utf-8"))


def _native_membership_digest() -> str:
    payload = b"".join(
        (
            b"kauri-membership-v1",
            _u(N, 4),
            b"".join(_u(replica, 2) for replica in range(N)),
        )
    )
    return hashlib.sha256(payload).hexdigest()


def _epoch_canonical_bytes(
    epoch_number: int,
    previous_digest: str,
    generation_seed: int,
    policy_version: str,
    trees: Sequence[Mapping[str, object]],
    *,
    evidence_snapshot_id: str,
    evidence_cutoff: int,
) -> bytes:
    result = bytearray(b"kauri-epoch-definition-v2")
    result += _u(2, 4)
    result += _u(epoch_number, 4)
    result += bytes.fromhex(previous_digest)
    result += bytes.fromhex(MEMBERSHIP_DIGEST)
    result += _u(generation_seed, 8)
    result += _string(policy_version)
    result += _string(evidence_snapshot_id)
    result += _u(evidence_cutoff, 8)
    result += _u(len(trees), 4)
    for tree in trees:
        members = tuple(tree["members"])  # type: ignore[arg-type]
        wait_exempt = tuple(tree["wait_exempt"])  # type: ignore[arg-type]
        result += _u(int(tree["tree_id"]), 4)
        result += _u(int(tree["fanout"]), 4)
        result += _u(int(tree["pipeline_stretch"]), 4)
        result += _u(len(members), 4)
        result += b"".join(_u(int(member), 2) for member in members)
        result += _u(len(wait_exempt), 4)
        result += b"".join(_u(int(member), 2) for member in wait_exempt)
    return bytes(result)


def _low_s_signature(signing_bytes: bytes, *, nonce: int) -> bytes:
    point = factorial_validation._secp256k1_multiply(
        nonce,
        (
            factorial_validation._SECP256K1_GX,
            factorial_validation._SECP256K1_GY,
        ),
    )
    assert point is not None
    order = factorial_validation._SECP256K1_ORDER
    r = point[0] % order
    z = int.from_bytes(hashlib.sha256(signing_bytes).digest(), "big")
    s = (pow(nonce, -1, order) * (z + r * ISSUER_PRIVATE_KEY)) % order
    if s > order // 2:
        s = order - s
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def _encode_native_epoch_bundle(
    epoch_number: int,
    previous_digest: str,
    generation_seed: int,
    policy_version: str,
    trees: Sequence[Mapping[str, object]],
    *,
    evidence_snapshot_id: str,
    evidence_cutoff: int,
) -> tuple[bytes, Any]:
    canonical = _epoch_canonical_bytes(
        epoch_number,
        previous_digest,
        generation_seed,
        policy_version,
        trees,
        evidence_snapshot_id=evidence_snapshot_id,
        evidence_cutoff=evidence_cutoff,
    )
    successor_digest = hashlib.sha256(canonical).hexdigest()
    signing_bytes = b"".join(
        (
            b"kauri-authorized-epoch-change-v1",
            _u(1, 4),
            _u(2, 1),
            _u(1, 4),
            _u(epoch_number, 4),
            bytes.fromhex(previous_digest),
            bytes.fromhex(successor_digest),
            _u(5, 8),
        )
    )
    command = signing_bytes + _low_s_signature(
        signing_bytes,
        nonce=epoch_number + 1,
    )
    definition = b"".join(
        (
            _u(2, 4),
            _u(2, 1),
            _u(6, 1),
            bytes.fromhex(successor_digest),
            canonical[len(b"kauri-epoch-definition-v2") :],
        )
    )
    wire = b"".join(
        (
            b"kauri-adaptive-v2-epoch-change-bundle-v1",
            _u(1, 4),
            _u(2, 1),
            _component(command),
            _component(definition),
        )
    )
    decoded = factorial_validation.decode_epoch_change_bundle(
        wire,
        issuer_public_key=ISSUER_PUBLIC_KEY,
    )
    assert decoded.epoch_digest == successor_digest
    assert decoded.command.signature == command[-64:]
    assert len(decoded.trees) == Q
    return wire, decoded


E1_ROOTS = tuple(range(Q))
E2_ROOTS = (
    0,
    1,
    25,
    26,
    27,
    28,
    29,
    30,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
)
RANKED_SURVIVORS = E2_ROOTS + tuple(
    replica for replica in SURVIVORS if replica not in E2_ROOTS
)
E1_TREES = _canonical_trees(E1_ROOTS, CRASHED)
E1_DIGEST = hashlib.sha256(
    _epoch_canonical_bytes(
        1,
        E0_DIGEST,
        1,
        "focused-containment-v1",
        E1_TREES,
        evidence_snapshot_id="pair-01-containment",
        evidence_cutoff=100,
    )
).hexdigest()
E2_TREES = _canonical_trees(E2_ROOTS, CRASHED)
E2_DIGEST = hashlib.sha256(
    _epoch_canonical_bytes(
        2,
        E1_DIGEST,
        2,
        "responsive-rank-v1",
        E2_TREES,
        evidence_snapshot_id="pair-01-ranking",
        evidence_cutoff=EVIDENCE_CUTOFF,
    )
).hexdigest()
ISSUER_KEY_SHA256 = hashlib.sha256(bytes.fromhex(ISSUER_PUBLIC_KEY)).hexdigest()
E1_BUNDLE_SHA256 = "bda2376d586c20e8f0bec9c97e21d053432cac49a9c8e91128bc463d826fd791"
E2_BUNDLE_SHA256 = "c1e0b47cd39765d9cba4a3380874ba708e854275603a7679b06f1d3dc1ccaf4d"


def _assert_decoded_epoch(
    decoded: Any,
    *,
    epoch_number: int,
    previous_digest: str,
    generation_seed: int,
    roots: Sequence[int],
) -> None:
    assert decoded.command.issuer_id == 1
    assert decoded.command.successor_epoch_number == epoch_number
    assert decoded.command.predecessor_epoch_digest == previous_digest
    assert decoded.command.successor_epoch_digest == decoded.epoch_digest
    assert decoded.command.activation_delay_blocks == 5
    assert decoded.epoch_number == epoch_number
    assert decoded.previous_epoch_digest == previous_digest
    assert decoded.generation_seed == generation_seed
    assert decoded.membership_digest == MEMBERSHIP_DIGEST
    assert tuple(tree.tree_id for tree in decoded.trees) == tuple(range(Q))
    assert tuple(tree.members[0] for tree in decoded.trees) == tuple(roots)
    for tree in decoded.trees:
        assert tree.fanout == FANOUT
        assert tree.pipeline_stretch == PIPELINE
        assert set(tree.members) == set(range(N))
        assert tree.wait_exempt == CRASHED
        assert set(CRASHED).issubset(tree.members[1 + FANOUT :])


def _native_epoch_chain() -> tuple[bytes, Any, bytes, Any]:
    assert _native_membership_digest() == MEMBERSHIP_DIGEST
    epoch1_wire, epoch1 = _encode_native_epoch_bundle(
        1,
        E0_DIGEST,
        1,
        "focused-containment-v1",
        E1_TREES,
        evidence_snapshot_id="pair-01-containment",
        evidence_cutoff=100,
    )
    epoch2_wire, epoch2 = _encode_native_epoch_bundle(
        2,
        epoch1.epoch_digest,
        2,
        "responsive-rank-v1",
        E2_TREES,
        evidence_snapshot_id="pair-01-ranking",
        evidence_cutoff=EVIDENCE_CUTOFF,
    )
    _assert_decoded_epoch(
        epoch1,
        epoch_number=1,
        previous_digest=E0_DIGEST,
        generation_seed=1,
        roots=E1_ROOTS,
    )
    _assert_decoded_epoch(
        epoch2,
        epoch_number=2,
        previous_digest=epoch1.epoch_digest,
        generation_seed=2,
        roots=E2_ROOTS,
    )
    assert epoch1.epoch_digest == E1_DIGEST
    assert epoch2.epoch_digest == E2_DIGEST
    assert hashlib.sha256(epoch1_wire).hexdigest() == E1_BUNDLE_SHA256
    assert hashlib.sha256(epoch2_wire).hexdigest() == E2_BUNDLE_SHA256
    return epoch1_wire, epoch1, epoch2_wire, epoch2


def _matched_pair() -> tuple[dict[str, object], dict[str, object]]:
    epoch1_wire, epoch1, epoch2_wire, epoch2 = _native_epoch_chain()
    ranking_ids = list(RANKED_SURVIVORS)
    evidence = _accepted_ranking_evidence()
    ranking = {
        "source_evidence_sha256": _sha(evidence),
        "source_epoch_digest": E1_DIGEST,
        "policy_version": "responsive-rank-v1",
        "evidence_cutoff": EVIDENCE_CUTOFF,
        "window_start_ns": 25_000,
        "window_end_ns": 35_000,
        "ranked_eligible_replica_ids": ranking_ids,
        "selected_root_ids": ranking_ids[:Q],
        "score_rows": _expected_score_rows(),
        "tie_break": "score_desc_replica_id_asc_v1",
    }
    common = {
        "pair_id": "pair-01",
        "revision": "b" * 40,
        "build_sha256": "c" * 64,
        "pair_seed": 41_720,
        "host_allocation_sha256": "d" * 64,
        "workload_sha256": "e" * 64,
        "crash_schedule_sha256": "f" * 64,
        "impairment_sha256": "6" * 64,
        "containment_policy_sha256": "7" * 64,
        "stable_windows_sha256": "8" * 64,
        "epoch1_bundle": epoch1_wire,
        "epoch1_bundle_sha256": hashlib.sha256(epoch1_wire).hexdigest(),
        "epoch1_issuer_public_key": ISSUER_PUBLIC_KEY,
        "epoch1_decoded": _document(epoch1),
    }
    control = {
        **common,
        "arm": "control",
        "epoch2_bundle": None,
        "epoch2_bundle_sha256": None,
        "epoch2_issuer_public_key": None,
        "epoch2_decoded": None,
    }
    adaptive = {
        **common,
        "arm": "adaptive",
        "post_containment_accepted_evidence": evidence,
        "ranking_snapshot": ranking,
        "epoch2_bundle": epoch2_wire,
        "epoch2_bundle_sha256": hashlib.sha256(epoch2_wire).hexdigest(),
        "epoch2_issuer_public_key": ISSUER_PUBLIC_KEY,
        "epoch2_decoded": _document(epoch2),
    }
    return control, adaptive


def test_matched_pair_binds_verified_decoded_epochs_evidence_and_ranking() -> None:
    control, adaptive = _matched_pair()
    control_epoch1 = factorial_validation.decode_epoch_change_bundle(
        control["epoch1_bundle"],  # type: ignore[arg-type]
        issuer_public_key=str(control["epoch1_issuer_public_key"]),
    )
    adaptive_epoch2 = factorial_validation.decode_epoch_change_bundle(
        adaptive["epoch2_bundle"],  # type: ignore[arg-type]
        issuer_public_key=str(adaptive["epoch2_issuer_public_key"]),
    )
    _assert_decoded_epoch(
        control_epoch1,
        epoch_number=1,
        previous_digest=E0_DIGEST,
        generation_seed=1,
        roots=E1_ROOTS,
    )
    _assert_decoded_epoch(
        adaptive_epoch2,
        epoch_number=2,
        previous_digest=control_epoch1.epoch_digest,
        generation_seed=2,
        roots=E2_ROOTS,
    )
    proof = _document(_subject().validate_matched_pair(control, adaptive))
    assert proof["matched"] is True
    assert proof["epoch1_exactly_identical"] is True
    assert proof["control_has_epoch2"] is False
    assert proof["adaptive_epoch2_bound_to_fresh_evidence"] is True
    assert proof["adaptive_epoch2_roots_are_top_q"] is True
    assert proof["verified_epoch_numbers"] == [1, 2]
    assert proof["epoch1_tree_count"] == Q
    assert proof["epoch2_tree_count"] == Q
    assert control["epoch1_bundle"] == adaptive["epoch1_bundle"]
    assert [list(tree.members) for tree in control_epoch1.trees] == [
        tree["members"] for tree in E1_TREES
    ]
    assert [list(tree.members) for tree in adaptive_epoch2.trees] == [
        tree["members"] for tree in E2_TREES
    ]


def _flip_bundle_signature(wire: bytes) -> bytes:
    changed = bytearray(wire)
    command_size_offset = len(b"kauri-adaptive-v2-epoch-change-bundle-v1") + 5
    command_size = int.from_bytes(
        changed[command_size_offset : command_size_offset + 4],
        "big",
    )
    signature_offset = command_size_offset + 4 + command_size - 64
    changed[signature_offset] ^= 1
    return bytes(changed)


def _flip_bundle_reply_digest(wire: bytes) -> bytes:
    changed = bytearray(wire)
    command_size_offset = len(b"kauri-adaptive-v2-epoch-change-bundle-v1") + 5
    command_size = int.from_bytes(
        changed[command_size_offset : command_size_offset + 4],
        "big",
    )
    definition_size_offset = command_size_offset + 4 + command_size
    definition_offset = definition_size_offset + 4
    changed[definition_offset + 6] ^= 1
    return bytes(changed)


@pytest.mark.parametrize(
    "mutation",
    ("issuer", "signature", "digest", "predecessor", "epoch1", "ranking"),
)
def test_matched_pair_rejects_decoded_epoch_or_ranking_drift(mutation: str) -> None:
    baseline_control, baseline_adaptive = _matched_pair()
    baseline = _document(
        _subject().validate_matched_pair(baseline_control, baseline_adaptive)
    )
    assert baseline["matched"] is True

    control, adaptive = _matched_pair()
    if mutation == "issuer":
        adaptive["epoch2_issuer_public_key"] = "03" + ISSUER_PUBLIC_KEY[2:]
    elif mutation == "signature":
        adaptive["epoch2_bundle"] = _flip_bundle_signature(  # type: ignore[arg-type]
            adaptive["epoch2_bundle"]
        )
    elif mutation == "digest":
        adaptive["epoch2_bundle"] = _flip_bundle_reply_digest(  # type: ignore[arg-type]
            adaptive["epoch2_bundle"]
        )
    elif mutation == "predecessor":
        wrong_wire, wrong_decoded = _encode_native_epoch_bundle(
            2,
            E0_DIGEST,
            2,
            "responsive-rank-v1",
            E2_TREES,
            evidence_snapshot_id="pair-01-ranking",
            evidence_cutoff=EVIDENCE_CUTOFF,
        )
        adaptive["epoch2_bundle"] = wrong_wire
        adaptive["epoch2_bundle_sha256"] = hashlib.sha256(wrong_wire).hexdigest()
        adaptive["epoch2_decoded"] = _document(wrong_decoded)
    elif mutation == "epoch1":
        changed_wire, changed_decoded = _encode_native_epoch_bundle(
            1,
            E0_DIGEST,
            9,
            "focused-containment-v1",
            E1_TREES,
            evidence_snapshot_id="pair-01-containment",
            evidence_cutoff=100,
        )
        adaptive["epoch1_bundle"] = changed_wire
        adaptive["epoch1_bundle_sha256"] = hashlib.sha256(
            changed_wire
        ).hexdigest()
        adaptive["epoch1_decoded"] = _document(changed_decoded)
    else:
        adaptive["ranking_snapshot"]["selected_root_ids"] = list(  # type: ignore[index]
            reversed(E2_ROOTS)
        )
    with pytest.raises(_subject().N31CrashPairError):
        _subject().validate_matched_pair(control, adaptive)


def _throughput_events() -> list[dict[str, object]]:
    times = (1_000_000_000, 6_000_000_000, 11_000_000_000, 16_000_000_000)
    transactions = (500, 750, 1_000, 1_250)
    events: list[dict[str, object]] = []
    for sequence, (timestamp, count) in enumerate(zip(times, transactions), start=1):
        payload = _commit_payload(sequence, f"{sequence:064x}", count)
        events.append(
            _envelope(
                "replica-0",
                sequence,
                timestamp,
                "block.committed",
                {
                    **payload,
                    "designated_observer": True,
                    "decision_proof": {
                        "epoch_number": 0,
                        "tree_id": TREE_ID,
                        "epoch_digest": E0_DIGEST,
                        "block_hash": payload["block_hash"],
                    },
                    "view_generation": 1,
                },
            )
        )
    return events


def test_throughput_uses_one_hash_per_height_and_event_transaction_counts() -> None:
    result = _document(
        _subject().compute_four_phase_throughput(
            _throughput_events(),
            phase_windows=(
                {"phase": "baseline", "start_ns": 0, "end_ns": 5_000_000_000},
                {"phase": "fault", "start_ns": 5_000_000_000, "end_ns": 10_000_000_000},
                {"phase": "epoch1", "start_ns": 10_000_000_000, "end_ns": 15_000_000_000},
                {"phase": "epoch2", "start_ns": 15_000_000_000, "end_ns": 20_000_000_000},
            ),
            authoritative_source_id="replica-0",
            bucket_width_ns=5_000_000_000,
        )
    )
    assert result["authority"]["unique_commit_rule"] == "one_hash_per_height_v1"
    assert [phase["transactions"] for phase in result["phases"]] == [500, 750, 1_000, 1_250]
    assert [phase["mean_tps"] for phase in result["phases"]] == [100.0, 150.0, 200.0, 250.0]


def test_throughput_rejects_two_hashes_for_one_committed_height() -> None:
    events = _throughput_events()
    conflict = deepcopy(events[0])
    conflict["source_sequence"] = 2
    conflict["source_monotonic_ns"] = 2_000_000_000
    conflict["payload"]["block_hash"] = "f" * 64  # type: ignore[index]
    conflict["payload"]["decision_proof"]["block_hash"] = "f" * 64  # type: ignore[index]
    for event in events[1:]:
        event["source_sequence"] = int(event["source_sequence"]) + 1
    with pytest.raises(_subject().N31CrashPairError):
        _subject().compute_four_phase_throughput(
            [events[0], conflict, *events[1:]],
            phase_windows=(
                {"phase": "baseline", "start_ns": 0, "end_ns": 5_000_000_000},
                {"phase": "fault", "start_ns": 5_000_000_000, "end_ns": 10_000_000_000},
                {"phase": "epoch1", "start_ns": 10_000_000_000, "end_ns": 15_000_000_000},
                {"phase": "epoch2", "start_ns": 15_000_000_000, "end_ns": 20_000_000_000},
            ),
            authoritative_source_id="replica-0",
            bucket_width_ns=5_000_000_000,
        )
