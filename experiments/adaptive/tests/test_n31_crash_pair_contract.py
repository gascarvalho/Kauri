"""Red-first contracts for the focused N=31 crash-pair evidence boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
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
BASELINE_EVIDENCE_CUTOFF = N
EVIDENCE_CUTOFF = N + len(CRASHED) + 2 * N
ISSUER_PRIVATE_KEY = 1
ISSUER_PUBLIC_KEY = "02" + f"{factorial_validation._SECP256K1_GX:064x}"
NATIVE_SNAPSHOT_SEED = 41_719
NATIVE_PLACEMENT_POLICY = "adaptive-v2-performance-optimization-v1"
NATIVE_RESPONSIVENESS_POLICY = {
    "schema_version": 1,
    "policy_version": "adaptive-v2-controller-responsiveness-v1",
    "attempt_window": 32,
    "minimum_attempts": 2,
    "minimum_response_rate_ppm": 750_000,
    "maximum_timeout_rate_ppm": 250_000,
    "trailing_timeout_streak": 2,
    "latency_percentile_basis_points": 5_000,
}
TLS_PRIVATE_KEY_DER_HEX = (
    "308187020100301306072a8648ce3d020106082a8648ce3d030107046d306b0201010420"
    "b82c0bdb45a4f35aa0d93cbe748f004569f21718d0defac048191504793df7aea1440342"
    "00046e69536068e139a32004510031c7386f14317bd9ac12a2017097db665e8cc69754e2"
    "23f8c4336a0969b342e41d2ca2a47d63e2086807492e27ef1bb3418b8d2b"
)
MISMATCHED_TLS_PRIVATE_KEY_DER_HEX = (
    "308187020100301306072a8648ce3d020106082a8648ce3d030107046d306b0201010420"
    "6e2465c805f69dce7b8a3fdc3e283a5e98ff8865359be05cb01c247eafb82a37a1440342"
    "0004c52c0d8fc13377057c7ef874f0b4d62ec31aa537cd2f288cb40b742ddfc5a27224cd"
    "5639da641a61ddb20688aefb66d626d92d21e42da5bef4fffc62dd10cf77"
)
TLS_CERTIFICATE_DER_HEX = (
    "3082017e30820125a00302010202141c7bac9fe94894fbde767b2dd08d586589fe46fc300a"
    "06082a8648ce3d04030230153113301106035504030c0a6b617572692d74657374301e170d"
    "3236303831313232313231365a170d3336303830383232313231365a301531133011060355"
    "04030c0a6b617572692d746573743059301306072a8648ce3d020106082a8648ce3d030107"
    "034200046e69536068e139a32004510031c7386f14317bd9ac12a2017097db665e8cc69754"
    "e223f8c4336a0969b342e41d2ca2a47d63e2086807492e27ef1bb3418b8d2ba353305130"
    "1d0603551d0e04160414bf9792b0eefe85658a4118d62f1cca91f3cd9105301f0603551d"
    "23041830168014bf9792b0eefe85658a4118d62f1cca91f3cd9105300f0603551d130101ff"
    "040530030101ff300a06082a8648ce3d040302034700304402204bdf256a6a8e8564b7e3f"
    "722fcf86f41d63c194c04418dcfc025f208d0d0650a02201f4aa3ba8b44147d116bf6ff1"
    "40a83b85b0e21713888ca52e3a1a62ef523799b"
)


def _replica_certificate_der_hex(replica: int) -> str:
    certificate = bytearray.fromhex(TLS_CERTIFICATE_DER_HEX)
    serial = bytes.fromhex("1c7bac9fe94894fbde767b2dd08d586589fe46fc")
    offset = certificate.index(serial)
    certificate[offset + len(serial) - 1] ^= replica + 1
    return certificate.hex()


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


def _safe_manager_boundary() -> tuple[tuple[str, ...], dict[str, object]]:
    transitions = (
        {
            "apply_shape_selection": False,
            "bundle_path": "transitions/e0-to-e1-containment/successor.bundle",
            "containment_baseline_root_source": "live_predecessor_roots",
            "evidence_snapshot_path": "transitions/e0-to-e1-containment/evidence-snapshot.json",
            "evidence_window_rule": "fresh_exact_predecessor_after_common_commit",
            "minimum_post_baseline_observation_ms": 0,
            "minimum_predecessor_residency_ms": 0,
            "policy_intent": "fault_containment",
            "policy_parameters": {},
            "predecessor_epoch_number": 0,
            "successor_epoch_number": 1,
            "transition_artifact_id": "e0-to-e1-containment",
        },
        {
            "apply_shape_selection": True,
            "bundle_path": "transitions/e1-to-e2-optimization/successor.bundle",
            "evidence_snapshot_path": "transitions/e1-to-e2-optimization/evidence-snapshot.json",
            "evidence_window_rule": "fresh_exact_predecessor_after_common_commit",
            "minimum_post_baseline_observation_ms": 0,
            "minimum_predecessor_residency_ms": 40_000,
            "policy_intent": "performance_optimization",
            "policy_parameters": {},
            "predecessor_epoch_number": 1,
            "successor_epoch_number": 2,
            "transition_artifact_id": "e1-to-e2-optimization",
        },
    )
    transition_json = tuple(_canonical(item).decode("ascii").rstrip("\n") for item in transitions)
    argv = (
        "adaptation-manager",
        "--listen", "127.0.0.1:19000",
        "--tls-privkey", TLS_PRIVATE_KEY_DER_HEX,
        "--tls-cert", TLS_CERTIFICATE_DER_HEX,
        "--issuer-id", "1",
        "--issuer-private-key", f"{ISSUER_PRIVATE_KEY:064x}",
        "--activation-delay-blocks", "5",
        "--convergence-deadline-seconds", "30",
        "--tree-fanout", str(FANOUT),
        "--pipeline-stretch", str(PIPELINE),
        "--shape-candidate-fanouts", str(FANOUT),
        "--shape-deterministic-seed", str(NATIVE_SNAPSHOT_SEED),
        "--responsiveness-policy-version", str(NATIVE_RESPONSIVENESS_POLICY["policy_version"]),
        "--required-nonresponsive", str(len(CRASHED)),
        "--responsiveness-attempt-window", str(NATIVE_RESPONSIVENESS_POLICY["attempt_window"]),
        "--responsiveness-minimum-attempts", str(NATIVE_RESPONSIVENESS_POLICY["minimum_attempts"]),
        "--responsiveness-minimum-response-rate-ppm", str(NATIVE_RESPONSIVENESS_POLICY["minimum_response_rate_ppm"]),
        "--responsiveness-maximum-timeout-rate-ppm", str(NATIVE_RESPONSIVENESS_POLICY["maximum_timeout_rate_ppm"]),
        "--responsiveness-trailing-timeout-streak", str(NATIVE_RESPONSIVENESS_POLICY["trailing_timeout_streak"]),
        "--responsiveness-latency-percentile-basis-points", str(NATIVE_RESPONSIVENESS_POLICY["latency_percentile_basis_points"]),
        "--transition-request", transition_json[0],
        "--bundle-output", "/tmp/kauri-n31/transitions/e0-to-e1-containment/successor.bundle",
        "--transition-request", transition_json[1],
        "--bundle-output", "/tmp/kauri-n31/transitions/e1-to-e2-optimization/successor.bundle",
        "--structured-event-run-id", RUN_ID,
        "--structured-event-source-instance", f"{RUN_ID}-adaptive-manager",
        "--structured-event-output", "/tmp/kauri-n31/raw/manager-events.jsonl",
        *tuple(
            value
            for replica in range(N)
            for value in (
                "--replica",
                f"{replica},127.0.0.1:{20_000 + replica},{_replica_certificate_der_hex(replica)}",
            )
        ),
    )
    return argv, {
        "input_source": "normalized_manager_launch_boundary_v1",
        "requested_argv": list(argv),
        "observed_argv": list(argv),
        "stdin": "closed",
    }


@pytest.mark.parametrize(
    "mutation",
    (
        "truth",
        "input",
        "unknown",
        "experiment",
        "mismatch",
        "relative-output",
        "duplicate-cert",
        "manager-cert-collision",
        "duplicate-address",
        "odd-hex",
        "non-der",
        "outer-tlv",
        "malformed-x509",
        "malformed-pkcs8",
        "key-cert-mismatch",
    ),
)
def test_manager_boundary_rejects_fault_truth(mutation: str) -> None:
    plan, _, _, _ = _fault_evidence()
    safe_argv, safe_input = _safe_manager_boundary()
    replica_arguments = [
        safe_argv[index + 1]
        for index, value in enumerate(safe_argv)
        if value == "--replica"
    ]
    replica_certificates = [value.rsplit(",", 1)[1] for value in replica_arguments]
    assert len(set(replica_arguments)) == len(set(replica_certificates)) == N
    assert TLS_CERTIFICATE_DER_HEX not in replica_certificates
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
    assert proof["requested_observed_argv_identical"] is True
    assert proof["input_source"] == "normalized_manager_launch_boundary_v1"

    manager_argv = list(safe_argv)
    manager_input = deepcopy(safe_input)
    if mutation == "truth":
        manager_argv.extend(("--crash-targets", "22,23,24"))
    elif mutation == "input":
        manager_input["target_pgids"] = [20_022, 20_023, 20_024]
    elif mutation == "unknown":
        manager_argv.extend(("--unreviewed-option", "1"))
    elif mutation == "experiment":
        manager_argv.extend(("--experiment-drop-bundle-attempt", "1"))
    elif mutation == "mismatch":
        manager_input["observed_argv"] = list(safe_argv[:-2])
    elif mutation == "relative-output":
        index = manager_argv.index("--bundle-output") + 1
        manager_argv[index] = manager_argv[index][1:]
    elif mutation in {"duplicate-cert", "manager-cert-collision", "duplicate-address"}:
        indices = [
            index + 1
            for index, value in enumerate(manager_argv)
            if value == "--replica"
        ]
        if mutation == "duplicate-cert":
            second = manager_argv[indices[1]].rsplit(",", 1)[0]
            first_certificate = manager_argv[indices[0]].rsplit(",", 1)[1]
            manager_argv[indices[1]] = f"{second},{first_certificate}"
        elif mutation == "manager-cert-collision":
            first = manager_argv[indices[0]].rsplit(",", 1)[0]
            manager_argv[indices[0]] = f"{first},{TLS_CERTIFICATE_DER_HEX}"
        else:
            first_id, first_address, _first_certificate = manager_argv[
                indices[0]
            ].split(",")
            second_id, _second_address, second_certificate = manager_argv[
                indices[1]
            ].split(",")
            assert first_id != second_id
            manager_argv[indices[1]] = (
                f"{second_id},{first_address},{second_certificate}"
            )
    elif mutation == "odd-hex":
        manager_argv[manager_argv.index("--tls-cert") + 1] = "abc"
    elif mutation == "non-der":
        manager_argv[manager_argv.index("--tls-cert") + 1] = "00"
    elif mutation == "outer-tlv":
        manager_argv[manager_argv.index("--tls-cert") + 1] = "3000"
    elif mutation == "malformed-x509":
        manager_argv[manager_argv.index("--tls-cert") + 1] = "3003010100"
    elif mutation == "malformed-pkcs8":
        manager_argv[manager_argv.index("--tls-privkey") + 1] = "3003010100"
    else:
        manager_argv[manager_argv.index("--tls-privkey") + 1] = (
            MISMATCHED_TLS_PRIVATE_KEY_DER_HEX
        )

    if mutation not in {"input", "mismatch"}:
        manager_input["requested_argv"] = list(manager_argv)
        manager_input["observed_argv"] = list(manager_argv)

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
    epoch_number: int,
    block_hash: str,
    epoch_digest: str,
) -> str:
    payload = b"".join(
        (
            b"kauri-response-observation-v1",
            reporter_id.to_bytes(2, "big"),
            observed_replica_id.to_bytes(2, "big"),
            epoch_number.to_bytes(4, "big"),
            (0).to_bytes(4, "big"),
            bytes.fromhex(epoch_digest),
            bytes.fromhex(block_hash),
            (1).to_bytes(1, "big"),
        )
    )
    return hashlib.sha256(payload).hexdigest()


def _response_latency(replica: int) -> int:
    rank = RANKED_SURVIVORS.index(replica)
    return 100 if replica in {0, 1} else 100 + rank


def _accepted_ranking_evidence() -> list[dict[str, object]]:
    accepted_events: list[dict[str, object]] = []
    records: list[Any] = []
    attempts: list[tuple[int, str, str, int, int]] = []
    for replica in range(N):
        attempts.append(
            (
                replica,
                "baseline",
                "on_time" if replica in SURVIVORS else "timeout",
                _response_latency(replica) if replica in SURVIVORS else 0,
                replica + 1,
            )
        )
    for replica in CRASHED:
        attempts.append(
            (replica, "baseline", "late", 2_000 + replica, replica + 1)
        )
    for attempt_number, attempt_name in enumerate(("fresh-a", "fresh-b"), start=1):
        for replica in range(N):
            attempts.append(
                (
                    replica,
                    attempt_name,
                    "on_time" if replica in SURVIVORS else "timeout",
                    (
                        _response_latency(replica) + attempt_number
                        if replica in SURVIVORS
                        else 0
                    ),
                    N * attempt_number + replica + 1,
                )
            )

    block_hashes = {
        (replica, attempt): hashlib.sha256(
            f"ranking-{replica}-{attempt}".encode()
        ).hexdigest()
        for replica, attempt, _outcome, _latency, _reporter_sequence in attempts
    }
    for sequence, (replica, attempt, outcome, latency, _reporter_sequence) in enumerate(
        attempts,
        start=1,
    ):
        block_hash = block_hashes[(replica, attempt)]
        accepted_events.append(
            _envelope(
                "adaptive-manager",
                sequence,
                100_000 + sequence,
                "evidence.observation_accepted",
                {
                    "ingestion_sequence": sequence,
                    "observation": {
                        "schema_version": 1,
                        "observation_id": _observation_id(
                            reporter_id=30,
                            observed_replica_id=replica,
                            epoch_number=1,
                            block_hash=block_hash,
                            epoch_digest=E1_DIGEST,
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
                        "response_duration_us": latency,
                        "deadline_duration_us": 1_000,
                        "reporter_monotonic_ns": 90_000 + sequence,
                        "reporter_sequence": sequence,
                        "signer_set": [replica] if outcome != "timeout" else [],
                    },
                },
            )
        )
        observation = accepted_events[-1]["payload"]["observation"]  # type: ignore[index]
        records.append(
            factorial_validation._EvidenceRecord(
                ingestion_sequence=sequence,
                acceptance_monotonic_ns=100_000 + sequence,
                observation_id=str(observation["observation_id"]),
                reporter_id=30,
                target_id=replica,
                epoch_number=1,
                tree_id=0,
                epoch_digest=E1_DIGEST,
                block_hash=block_hash,
                message_type="direct_vote",
                outcome=outcome,
                response_duration_us=latency,
                deadline_duration_us=1_000,
                reporter_monotonic_ns=90_000 + sequence,
                reporter_sequence=sequence,
                signer_set=(replica,) if outcome != "timeout" else (),
                acceptance_source_sequence=sequence + 1,
            )
        )
    events = [
        _envelope(
            "adaptive-manager",
            1,
            99_999,
            "process.started",
            {"exit_status": None},
        )
    ]
    for source_sequence, event in enumerate(accepted_events, start=2):
        event["source_sequence"] = source_sequence
        event["source_monotonic_ns"] = 100_000 + source_sequence
        events.append(event)
    full_snapshot_id = factorial_validation._snapshot_id(
        records,
        replica_count=N,
        epoch_number=1,
        epoch_digest=E1_DIGEST,
        cutoff=EVIDENCE_CUTOFF,
        policy=NATIVE_RESPONSIVENESS_POLICY,
        seed=NATIVE_SNAPSHOT_SEED,
    )
    selected_records = factorial_validation._snapshot_records(
        records,
        baseline_cutoff=BASELINE_EVIDENCE_CUTOFF,
        current_cutoff=EVIDENCE_CUTOFF,
        suffix_only=True,
    )
    selected_snapshot_id = factorial_validation._snapshot_id(
        selected_records,
        replica_count=N,
        epoch_number=1,
        epoch_digest=E1_DIGEST,
        cutoff=EVIDENCE_CUTOFF,
        policy=NATIVE_RESPONSIVENESS_POLICY,
        seed=NATIVE_SNAPSHOT_SEED,
    )
    events.append(
        _envelope(
            "adaptive-manager",
            len(events) + 1,
            100_000 + len(events) + 1,
            "adaptive_v2_evidence_snapshot",
            {
                "schema_version": 2,
                "cycle_ordinal": 1,
                "policy_intent": "performance_optimization",
                "transition_artifact_id": "e1-to-e2-optimization",
                "predecessor_epoch_number": 1,
                "predecessor_epoch_digest": E1_DIGEST,
                "activation_generation": (1 << 32) + 1,
                "baseline_cutoff": BASELINE_EVIDENCE_CUTOFF,
                "current_cutoff": EVIDENCE_CUTOFF,
                "full_prefix_snapshot_id": full_snapshot_id,
                "evidence_snapshot_id": selected_snapshot_id,
                "accepted_prefix_count": EVIDENCE_CUTOFF,
                "eligible_ranking": list(RANKED_SURVIVORS[:Q]),
            },
        )
    )
    assert len(accepted_events) == EVIDENCE_CUTOFF
    return events


def _native_ranking_snapshot(
    evidence: Sequence[Mapping[str, object]] | None = None,
    **overrides: object,
) -> dict[str, object]:
    arguments: dict[str, object] = {
        "membership_replica_ids": tuple(range(N)),
        "predecessor_epoch_number": 1,
        "predecessor_epoch_digest": E1_DIGEST,
        "baseline_evidence_cutoff": BASELINE_EVIDENCE_CUTOFF,
        "current_evidence_cutoff": EVIDENCE_CUTOFF,
        "policy": NATIVE_RESPONSIVENESS_POLICY,
        "seed": NATIVE_SNAPSHOT_SEED,
        "suffix_only": True,
    }
    arguments.update(overrides)
    return _document(
        factorial_validation.replay_native_adaptation_snapshot(
            list(evidence or _accepted_ranking_evidence()),
            **arguments,
        )
    )


def test_ranking_replays_native_fresh_suffix_with_manager_default_policy() -> None:
    ranking = _native_ranking_snapshot()
    assert ranking["seed"] == NATIVE_SNAPSHOT_SEED
    assert ranking["policy"] == NATIVE_RESPONSIVENESS_POLICY
    assert ranking["baseline_evidence_cutoff"] == BASELINE_EVIDENCE_CUTOFF
    assert ranking["current_evidence_cutoff"] == EVIDENCE_CUTOFF
    assert ranking["accepted_record_count"] == 2 * N
    assert [row["replica_id"] for row in ranking["ranking"][:Q]] == list(
        RANKED_SURVIVORS[:Q]
    )
    by_replica = {row["replica_id"]: row for row in ranking["ranking"]}
    assert all(by_replica[replica]["classification"] == "responsive" for replica in SURVIVORS)
    assert all(by_replica[replica]["classification"] == "nonresponsive" for replica in CRASHED)


@pytest.mark.parametrize(
    "mutation",
    ("instance", "timestamp", "reporter", "predecessor", "cutoff"),
)
def test_ranking_rejects_unbound_or_noncanonical_evidence(mutation: str) -> None:
    evidence = deepcopy(_accepted_ranking_evidence())
    overrides: dict[str, object] = {}
    if mutation == "instance":
        evidence[1]["source_instance"] = "restarted-manager"
    elif mutation == "timestamp":
        evidence[1]["source_monotonic_ns"] = 1
    elif mutation == "reporter":
        evidence[-2]["payload"]["observation"][  # type: ignore[index]
            "reporter_monotonic_ns"
        ] = int(evidence[-3]["payload"]["observation"]["reporter_monotonic_ns"]) - 1  # type: ignore[index]
    elif mutation == "predecessor":
        overrides["predecessor_epoch_digest"] = "de" * 32
    else:
        overrides["current_evidence_cutoff"] = EVIDENCE_CUTOFF + 1
    with pytest.raises(factorial_validation.FactorialValidationError):
        _native_ranking_snapshot(evidence, **overrides)


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


def _epoch1_replay_evidence(
    attempts_per_replica: int,
) -> tuple[list[dict[str, object]], tuple[Any, ...]]:
    accepted_events: list[dict[str, object]] = []
    records: list[Any] = []
    sequence = 0
    for attempt in range(attempts_per_replica):
        for replica in range(N):
            sequence += 1
            reporter = (replica + 1) % N
            outcome = "timeout" if replica in CRASHED else "on_time"
            latency = 0 if outcome == "timeout" else 200 + replica + attempt
            block_hash = hashlib.sha256(
                f"epoch0-{attempt}-{replica}".encode("ascii")
            ).hexdigest()
            observation_id = _observation_id(
                reporter_id=reporter,
                observed_replica_id=replica,
                epoch_number=0,
                block_hash=block_hash,
                epoch_digest=E0_DIGEST,
            )
            reporter_ns = 40_000 + sequence
            accepted_ns = 50_000 + sequence
            signers = (replica,) if outcome == "on_time" else ()
            observation = {
                "schema_version": 1,
                "observation_id": observation_id,
                "reporter_id": reporter,
                "observed_replica_id": replica,
                "configuration": {
                    "epoch_number": 0,
                    "tree_id": 0,
                    "epoch_digest": E0_DIGEST,
                },
                "block_hash": block_hash,
                "expected_message_type": "direct_vote",
                "outcome": outcome,
                "response_duration_us": latency,
                "deadline_duration_us": 1_000,
                "reporter_monotonic_ns": reporter_ns,
                "reporter_sequence": sequence,
                "signer_set": list(signers),
            }
            accepted_events.append(
                _envelope(
                    "adaptive-manager",
                    sequence,
                    accepted_ns,
                    "evidence.observation_accepted",
                    {
                        "ingestion_sequence": sequence,
                        "observation": observation,
                    },
                )
            )
            records.append(
                factorial_validation._EvidenceRecord(
                    ingestion_sequence=sequence,
                    acceptance_monotonic_ns=accepted_ns,
                    observation_id=observation_id,
                    reporter_id=reporter,
                    target_id=replica,
                    epoch_number=0,
                    tree_id=0,
                    epoch_digest=E0_DIGEST,
                    block_hash=block_hash,
                    message_type="direct_vote",
                    outcome=outcome,
                    response_duration_us=latency,
                    deadline_duration_us=1_000,
                    reporter_monotonic_ns=reporter_ns,
                    reporter_sequence=sequence,
                    signer_set=signers,
                    acceptance_source_sequence=sequence,
                )
            )
    events = [
        _envelope(
            "adaptive-manager",
            1,
            49_999,
            "process.started",
            {"exit_status": None},
        )
    ]
    rebound_records: list[Any] = []
    for source_sequence, (event, record) in enumerate(
        zip(accepted_events, records, strict=True),
        start=2,
    ):
        timestamp = 50_000 + source_sequence
        event["source_sequence"] = source_sequence
        event["source_monotonic_ns"] = timestamp
        events.append(event)
        rebound_records.append(
            replace(
                record,
                acceptance_monotonic_ns=timestamp,
                acceptance_source_sequence=source_sequence,
            )
        )
    full_snapshot_id = factorial_validation._snapshot_id(
        rebound_records,
        replica_count=N,
        epoch_number=0,
        epoch_digest=E0_DIGEST,
        cutoff=len(records),
        policy=NATIVE_RESPONSIVENESS_POLICY,
        seed=NATIVE_SNAPSHOT_SEED,
    )
    selected_snapshot_id = factorial_validation._snapshot_id(
        rebound_records,
        replica_count=N,
        epoch_number=0,
        epoch_digest=E0_DIGEST,
        cutoff=len(records),
        policy=NATIVE_RESPONSIVENESS_POLICY,
        seed=NATIVE_SNAPSHOT_SEED,
    )
    events.append(
        _envelope(
            "adaptive-manager",
            len(events) + 1,
            50_000 + len(events) + 1,
            "adaptive_v2_evidence_snapshot",
            {
                "schema_version": 2,
                "cycle_ordinal": 0,
                "policy_intent": "fault_containment",
                "transition_artifact_id": "e0-to-e1-containment",
                "predecessor_epoch_number": 0,
                "predecessor_epoch_digest": E0_DIGEST,
                "activation_generation": 1,
                "baseline_cutoff": 0,
                "current_cutoff": len(records),
                "full_prefix_snapshot_id": full_snapshot_id,
                "evidence_snapshot_id": selected_snapshot_id,
                "accepted_prefix_count": len(records),
                "eligible_ranking": list(range(Q)),
            },
        )
    )
    return events, tuple(rebound_records)


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
CONTROL_E1_CUTOFF = 3 * N
ADAPTIVE_E1_CUTOFF = 4 * N
_, _CONTROL_E1_RECORDS = _epoch1_replay_evidence(3)
_, _ADAPTIVE_E1_RECORDS = _epoch1_replay_evidence(4)
CONTROL_E1_SNAPSHOT_ID = factorial_validation._snapshot_id(
    _CONTROL_E1_RECORDS,
    replica_count=N,
    epoch_number=0,
    epoch_digest=E0_DIGEST,
    cutoff=CONTROL_E1_CUTOFF,
    policy=NATIVE_RESPONSIVENESS_POLICY,
)
ADAPTIVE_E1_SNAPSHOT_ID = factorial_validation._snapshot_id(
    _ADAPTIVE_E1_RECORDS,
    replica_count=N,
    epoch_number=0,
    epoch_digest=E0_DIGEST,
    cutoff=ADAPTIVE_E1_CUTOFF,
    policy=NATIVE_RESPONSIVENESS_POLICY,
)
CONTROL_E1_DIGEST = hashlib.sha256(
    _epoch_canonical_bytes(
        1,
        E0_DIGEST,
        NATIVE_SNAPSHOT_SEED,
        NATIVE_PLACEMENT_POLICY,
        E1_TREES,
        evidence_snapshot_id=CONTROL_E1_SNAPSHOT_ID,
        evidence_cutoff=CONTROL_E1_CUTOFF,
    )
).hexdigest()
E1_DIGEST = hashlib.sha256(
    _epoch_canonical_bytes(
        1,
        E0_DIGEST,
        NATIVE_SNAPSHOT_SEED,
        NATIVE_PLACEMENT_POLICY,
        E1_TREES,
        evidence_snapshot_id=ADAPTIVE_E1_SNAPSHOT_ID,
        evidence_cutoff=ADAPTIVE_E1_CUTOFF,
    )
).hexdigest()
E2_TREES = _canonical_trees(E2_ROOTS, CRASHED)
E2_DIGEST = hashlib.sha256(
    _epoch_canonical_bytes(
        2,
        E1_DIGEST,
        NATIVE_SNAPSHOT_SEED,
        NATIVE_PLACEMENT_POLICY,
        E2_TREES,
        evidence_snapshot_id="pair-01-native-ranking",
        evidence_cutoff=EVIDENCE_CUTOFF,
    )
).hexdigest()
ISSUER_KEY_SHA256 = hashlib.sha256(bytes.fromhex(ISSUER_PUBLIC_KEY)).hexdigest()


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


def _independent_epoch1_bundles(
    *, verify_replay: bool = False
) -> tuple[bytes, Any, bytes, Any]:
    assert _native_membership_digest() == MEMBERSHIP_DIGEST
    control_snapshot_id = CONTROL_E1_SNAPSHOT_ID
    control_cutoff = CONTROL_E1_CUTOFF
    adaptive_snapshot_id = ADAPTIVE_E1_SNAPSHOT_ID
    adaptive_cutoff = ADAPTIVE_E1_CUTOFF
    if verify_replay:
        control_events, _ = _epoch1_replay_evidence(3)
        adaptive_events, _ = _epoch1_replay_evidence(4)
        common = {
            "membership_replica_ids": tuple(range(N)),
            "predecessor_epoch_number": 0,
            "predecessor_epoch_digest": E0_DIGEST,
            "baseline_evidence_cutoff": 0,
            "policy": NATIVE_RESPONSIVENESS_POLICY,
            "seed": NATIVE_SNAPSHOT_SEED,
            "suffix_only": False,
        }
        control_snapshot = _document(
            factorial_validation.replay_native_adaptation_snapshot(
                control_events,
                current_evidence_cutoff=CONTROL_E1_CUTOFF,
                **common,
            )
        )
        adaptive_snapshot = _document(
            factorial_validation.replay_native_adaptation_snapshot(
                adaptive_events,
                current_evidence_cutoff=ADAPTIVE_E1_CUTOFF,
                **common,
            )
        )
        control_snapshot_id = str(control_snapshot["snapshot_id"])
        control_cutoff = int(control_snapshot["current_evidence_cutoff"])
        adaptive_snapshot_id = str(adaptive_snapshot["snapshot_id"])
        adaptive_cutoff = int(adaptive_snapshot["current_evidence_cutoff"])
        assert control_snapshot_id == CONTROL_E1_SNAPSHOT_ID
        assert adaptive_snapshot_id == ADAPTIVE_E1_SNAPSHOT_ID
        assert all(
            len(value) == 64 and all(character in "0123456789abcdef" for character in value)
            for value in (control_snapshot_id, adaptive_snapshot_id)
        )
    control_wire, control_epoch1 = _encode_native_epoch_bundle(
        1,
        E0_DIGEST,
        NATIVE_SNAPSHOT_SEED,
        NATIVE_PLACEMENT_POLICY,
        E1_TREES,
        evidence_snapshot_id=control_snapshot_id,
        evidence_cutoff=control_cutoff,
    )
    adaptive_wire, adaptive_epoch1 = _encode_native_epoch_bundle(
        1,
        E0_DIGEST,
        NATIVE_SNAPSHOT_SEED,
        NATIVE_PLACEMENT_POLICY,
        E1_TREES,
        evidence_snapshot_id=adaptive_snapshot_id,
        evidence_cutoff=adaptive_cutoff,
    )
    assert control_epoch1.epoch_digest == CONTROL_E1_DIGEST
    assert adaptive_epoch1.epoch_digest == E1_DIGEST
    assert control_wire != adaptive_wire
    assert control_epoch1.command.signature != adaptive_epoch1.command.signature
    return control_wire, control_epoch1, adaptive_wire, adaptive_epoch1


def _native_epoch_chain(
    *, evidence_snapshot_id: str = "pair-01-native-ranking"
) -> tuple[bytes, Any, bytes, Any]:
    _, _, epoch1_wire, epoch1 = _independent_epoch1_bundles()
    epoch2_wire, epoch2 = _encode_native_epoch_bundle(
        2,
        epoch1.epoch_digest,
        NATIVE_SNAPSHOT_SEED,
        NATIVE_PLACEMENT_POLICY,
        E2_TREES,
        evidence_snapshot_id=evidence_snapshot_id,
        evidence_cutoff=EVIDENCE_CUTOFF,
    )
    _assert_decoded_epoch(
        epoch1,
        epoch_number=1,
        previous_digest=E0_DIGEST,
        generation_seed=NATIVE_SNAPSHOT_SEED,
        roots=E1_ROOTS,
    )
    _assert_decoded_epoch(
        epoch2,
        epoch_number=2,
        previous_digest=epoch1.epoch_digest,
        generation_seed=NATIVE_SNAPSHOT_SEED,
        roots=E2_ROOTS,
    )
    assert epoch1.epoch_digest == E1_DIGEST
    if evidence_snapshot_id == "pair-01-native-ranking":
        assert epoch2.epoch_digest == E2_DIGEST
    return epoch1_wire, epoch1, epoch2_wire, epoch2


def _epoch1_replay_binding(
    attempts_per_replica: int,
) -> tuple[list[dict[str, object]], dict[str, object], dict[str, object]]:
    events, _ = _epoch1_replay_evidence(attempts_per_replica)
    replay_input: dict[str, object] = {
        "membership_replica_ids": list(range(N)),
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": E0_DIGEST,
        "baseline_evidence_cutoff": 0,
        "current_evidence_cutoff": attempts_per_replica * N,
        "policy": deepcopy(NATIVE_RESPONSIVENESS_POLICY),
        "seed": NATIVE_SNAPSHOT_SEED,
        "suffix_only": False,
    }
    snapshot = _document(
        factorial_validation.replay_native_adaptation_snapshot(
            events,
            **replay_input,
        )
    )
    return events, replay_input, snapshot


def _matched_pair() -> tuple[dict[str, object], dict[str, object]]:
    control_epoch1_wire, control_epoch1, adaptive_epoch1_wire, adaptive_epoch1 = (
        _independent_epoch1_bundles(verify_replay=True)
    )
    evidence = _accepted_ranking_evidence()
    control_e1_events, control_e1_input, control_e1_snapshot = (
        _epoch1_replay_binding(3)
    )
    adaptive_e1_events, adaptive_e1_input, adaptive_e1_snapshot = (
        _epoch1_replay_binding(4)
    )
    ranking = _native_ranking_snapshot(evidence)
    ranking["selected_root_ids"] = [
        row["replica_id"] for row in ranking["ranking"][:Q]
    ]
    epoch2_wire, epoch2 = _encode_native_epoch_bundle(
        2,
        adaptive_epoch1.epoch_digest,
        NATIVE_SNAPSHOT_SEED,
        NATIVE_PLACEMENT_POLICY,
        E2_TREES,
        evidence_snapshot_id=str(ranking["snapshot_id"]),
        evidence_cutoff=EVIDENCE_CUTOFF,
    )
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
    }
    control = {
        **common,
        "arm": "control",
        "epoch1_bundle": control_epoch1_wire,
        "epoch1_bundle_sha256": hashlib.sha256(control_epoch1_wire).hexdigest(),
        "epoch1_issuer_public_key": ISSUER_PUBLIC_KEY,
        "epoch1_decoded": _document(control_epoch1),
        "epoch1_replay_events": control_e1_events,
        "epoch1_replay_input": control_e1_input,
        "epoch1_replay_snapshot": control_e1_snapshot,
        "epoch2_bundle": None,
        "epoch2_bundle_sha256": None,
        "epoch2_issuer_public_key": None,
        "epoch2_decoded": None,
    }
    adaptive = {
        **common,
        "arm": "adaptive",
        "epoch1_bundle": adaptive_epoch1_wire,
        "epoch1_bundle_sha256": hashlib.sha256(adaptive_epoch1_wire).hexdigest(),
        "epoch1_issuer_public_key": ISSUER_PUBLIC_KEY,
        "epoch1_decoded": _document(adaptive_epoch1),
        "epoch1_replay_events": adaptive_e1_events,
        "epoch1_replay_input": adaptive_e1_input,
        "epoch1_replay_snapshot": adaptive_e1_snapshot,
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
    adaptive_epoch1 = factorial_validation.decode_epoch_change_bundle(
        adaptive["epoch1_bundle"],  # type: ignore[arg-type]
        issuer_public_key=str(adaptive["epoch1_issuer_public_key"]),
    )
    adaptive_epoch2 = factorial_validation.decode_epoch_change_bundle(
        adaptive["epoch2_bundle"],  # type: ignore[arg-type]
        issuer_public_key=str(adaptive["epoch2_issuer_public_key"]),
    )
    _assert_decoded_epoch(
        control_epoch1,
        epoch_number=1,
        previous_digest=E0_DIGEST,
        generation_seed=NATIVE_SNAPSHOT_SEED,
        roots=E1_ROOTS,
    )
    _assert_decoded_epoch(
        adaptive_epoch1,
        epoch_number=1,
        previous_digest=E0_DIGEST,
        generation_seed=NATIVE_SNAPSHOT_SEED,
        roots=E1_ROOTS,
    )
    _assert_decoded_epoch(
        adaptive_epoch2,
        epoch_number=2,
        previous_digest=adaptive_epoch1.epoch_digest,
        generation_seed=NATIVE_SNAPSHOT_SEED,
        roots=E2_ROOTS,
    )
    proof = _document(_subject().validate_matched_pair(control, adaptive))
    assert proof["matched"] is True
    assert proof["epoch1_structurally_identical"] is True
    assert proof["epoch1_replays_bound"] is True
    assert proof["control_has_epoch2"] is False
    assert proof["adaptive_epoch2_bound_to_fresh_evidence"] is True
    assert proof["adaptive_epoch2_roots_are_top_q"] is True
    assert proof["verified_epoch_numbers"] == [1, 2]
    assert proof["epoch1_tree_count"] == Q
    assert proof["epoch2_tree_count"] == Q
    assert control["epoch1_bundle"] != adaptive["epoch1_bundle"]
    assert control_epoch1.epoch_digest != adaptive_epoch1.epoch_digest
    assert control_epoch1.command.signature != adaptive_epoch1.command.signature
    assert control_epoch1.evidence_snapshot_id != adaptive_epoch1.evidence_snapshot_id
    assert control_epoch1.evidence_cutoff != adaptive_epoch1.evidence_cutoff
    assert control_epoch1.generation_seed == adaptive_epoch1.generation_seed == NATIVE_SNAPSHOT_SEED
    assert control_epoch1.policy_version == adaptive_epoch1.policy_version == NATIVE_PLACEMENT_POLICY
    assert control_epoch1.command.issuer_id == adaptive_epoch1.command.issuer_id
    assert control_epoch1.command.activation_delay_blocks == adaptive_epoch1.command.activation_delay_blocks
    for arm, decoded in ((control, control_epoch1), (adaptive, adaptive_epoch1)):
        replay = arm["epoch1_replay_snapshot"]
        assert decoded.evidence_snapshot_id == replay["snapshot_id"]  # type: ignore[index]
        assert decoded.evidence_cutoff == replay["current_evidence_cutoff"]  # type: ignore[index]
        assert replay["ranking"]  # type: ignore[index]
    assert [list(tree.members) for tree in control_epoch1.trees] == [
        tree["members"] for tree in E1_TREES
    ]
    assert [asdict(tree) for tree in control_epoch1.trees] == [
        asdict(tree) for tree in adaptive_epoch1.trees
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
    (
        "issuer",
        "signature",
        "digest",
        "predecessor",
        "epoch1",
        "epoch1-snapshot",
        "ranking",
    ),
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
            NATIVE_SNAPSHOT_SEED,
            NATIVE_PLACEMENT_POLICY,
            E2_TREES,
            evidence_snapshot_id="pair-01-native-ranking",
            evidence_cutoff=EVIDENCE_CUTOFF,
        )
        adaptive["epoch2_bundle"] = wrong_wire
        adaptive["epoch2_bundle_sha256"] = hashlib.sha256(wrong_wire).hexdigest()
        adaptive["epoch2_decoded"] = _document(wrong_decoded)
    elif mutation == "epoch1":
        changed_trees = deepcopy(E1_TREES)
        changed_trees[0]["members"][1], changed_trees[0]["members"][2] = (  # type: ignore[index]
            changed_trees[0]["members"][2],  # type: ignore[index]
            changed_trees[0]["members"][1],  # type: ignore[index]
        )
        changed_wire, changed_decoded = _encode_native_epoch_bundle(
            1,
            E0_DIGEST,
            NATIVE_SNAPSHOT_SEED,
            NATIVE_PLACEMENT_POLICY,
            changed_trees,
            evidence_snapshot_id=ADAPTIVE_E1_SNAPSHOT_ID,
            evidence_cutoff=ADAPTIVE_E1_CUTOFF,
        )
        adaptive["epoch1_bundle"] = changed_wire
        adaptive["epoch1_bundle_sha256"] = hashlib.sha256(
            changed_wire
        ).hexdigest()
        adaptive["epoch1_decoded"] = _document(changed_decoded)
    elif mutation == "epoch1-snapshot":
        changed_wire, changed_decoded = _encode_native_epoch_bundle(
            1,
            E0_DIGEST,
            NATIVE_SNAPSHOT_SEED,
            NATIVE_PLACEMENT_POLICY,
            E1_TREES,
            evidence_snapshot_id="f" * 64,
            evidence_cutoff=ADAPTIVE_E1_CUTOFF + 1,
        )
        adaptive["epoch1_bundle"] = changed_wire
        adaptive["epoch1_bundle_sha256"] = hashlib.sha256(changed_wire).hexdigest()
        adaptive["epoch1_decoded"] = _document(changed_decoded)
    else:
        adaptive["ranking_snapshot"]["selected_root_ids"] = list(  # type: ignore[index]
            reversed(E2_ROOTS)
        )
    with pytest.raises(_subject().N31CrashPairError):
        _subject().validate_matched_pair(control, adaptive)


@pytest.mark.parametrize("arm_name", ("control", "adaptive"))
@pytest.mark.parametrize(
    "component",
    ("events", "input-cutoff", "input-policy", "input-seed", "snapshot"),
)
def test_matched_pair_recomputes_each_arm_epoch1_replay_component(
    arm_name: str,
    component: str,
) -> None:
    baseline_control, baseline_adaptive = _matched_pair()
    assert _document(
        _subject().validate_matched_pair(baseline_control, baseline_adaptive)
    )["epoch1_replays_bound"] is True

    control, adaptive = _matched_pair()
    arm = control if arm_name == "control" else adaptive
    before = {
        key: deepcopy(arm[key])
        for key in (
            "epoch1_replay_events",
            "epoch1_replay_input",
            "epoch1_replay_snapshot",
        )
    }
    if component == "events":
        accepted = next(
            event
            for event in arm["epoch1_replay_events"]  # type: ignore[union-attr]
            if event["event_type"] == "evidence.observation_accepted"
        )
        observation = accepted["payload"]["observation"]
        observation["response_duration_us"] = int(
            observation["response_duration_us"]
        ) + 1
    elif component == "input-cutoff":
        replay_input = arm["epoch1_replay_input"]
        replay_input["current_evidence_cutoff"] = int(
            replay_input["current_evidence_cutoff"]
        ) - 1
    elif component == "input-policy":
        arm["epoch1_replay_input"]["policy"]["minimum_attempts"] = 3
    elif component == "input-seed":
        arm["epoch1_replay_input"]["seed"] = NATIVE_SNAPSHOT_SEED + 1
    else:
        arm["epoch1_replay_snapshot"]["snapshot_id"] = "e" * 64

    changed_group = (
        "epoch1_replay_events"
        if component == "events"
        else "epoch1_replay_snapshot"
        if component == "snapshot"
        else "epoch1_replay_input"
    )
    assert all(
        arm[key] == value
        for key, value in before.items()
        if key != changed_group
    )

    with pytest.raises(_subject().N31CrashPairError):
        _subject().validate_matched_pair(control, adaptive)


def test_matched_pair_rejects_coherent_earlier_control_epoch1_replay() -> None:
    baseline_control, baseline_adaptive = _matched_pair()
    assert _document(
        _subject().validate_matched_pair(baseline_control, baseline_adaptive)
    )["epoch1_replays_bound"] is True

    control, adaptive = _matched_pair()
    events = control["epoch1_replay_events"]
    removed = events.pop(-2)
    assert removed["event_type"] == "evidence.observation_accepted"
    audit = events[-1]
    audit["source_sequence"] = int(audit["source_sequence"]) - 1
    audit["source_monotonic_ns"] = int(events[-2]["source_monotonic_ns"]) + 1
    earlier_cutoff = CONTROL_E1_CUTOFF - 1
    _, complete_records = _epoch1_replay_evidence(3)
    prefix = tuple(
        record
        for record in complete_records
        if record.ingestion_sequence <= earlier_cutoff
    )
    selected = factorial_validation._snapshot_records(
        complete_records,
        baseline_cutoff=0,
        current_cutoff=earlier_cutoff,
        suffix_only=False,
    )
    audit_payload = audit["payload"]
    audit_payload["current_cutoff"] = earlier_cutoff
    audit_payload["accepted_prefix_count"] = len(prefix)
    audit_payload["full_prefix_snapshot_id"] = factorial_validation._snapshot_id(
        prefix,
        replica_count=N,
        epoch_number=0,
        epoch_digest=E0_DIGEST,
        cutoff=earlier_cutoff,
        policy=NATIVE_RESPONSIVENESS_POLICY,
        seed=NATIVE_SNAPSHOT_SEED,
    )
    audit_payload["evidence_snapshot_id"] = factorial_validation._snapshot_id(
        selected,
        replica_count=N,
        epoch_number=0,
        epoch_digest=E0_DIGEST,
        cutoff=earlier_cutoff,
        policy=NATIVE_RESPONSIVENESS_POLICY,
        seed=NATIVE_SNAPSHOT_SEED,
    )
    replay_input = control["epoch1_replay_input"]
    replay_input["current_evidence_cutoff"] = earlier_cutoff
    replay = _document(
        factorial_validation.replay_native_adaptation_snapshot(
            events,
            **replay_input,
        )
    )
    control["epoch1_replay_snapshot"] = replay
    wire, decoded = _encode_native_epoch_bundle(
        1,
        E0_DIGEST,
        NATIVE_SNAPSHOT_SEED,
        NATIVE_PLACEMENT_POLICY,
        E1_TREES,
        evidence_snapshot_id=str(replay["snapshot_id"]),
        evidence_cutoff=earlier_cutoff,
    )
    control["epoch1_bundle"] = wire
    control["epoch1_bundle_sha256"] = hashlib.sha256(wire).hexdigest()
    control["epoch1_decoded"] = _document(decoded)

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
