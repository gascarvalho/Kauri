"""Independent fail-closed validator tests for the frozen SHAPE25 campaign."""

from __future__ import annotations

from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment import factorial_validation as validation
from experiments.adaptive.kauri_experiment.factorial_manifest import (
    build_factorial_plan,
    canonical_plan_bytes,
    load_frozen_manifest,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    ManagerSecretMaterial,
    build_factorial_runtime,
    canonical_runtime_bytes,
    materialize_manager_argv,
    materialize_replica_argv,
)
from experiments.adaptive.kauri_experiment.factorial_validation import (
    FactorialValidationError,
    FaultMarker,
    ReplicaScore,
    Tree,
    derive_actor_ids,
    fnv1a_rotating_actor,
    validate_fault_causality,
    validate_manager_blinding,
    validate_schedule_document,
    validate_shape_decision,
    validate_slot,
    validate_throughput_document,
)

REPOSITORY = Path(__file__).resolve().parents[3]
MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v2.json"
)
LEGACY_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v1.json"
)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _native_event(
    *,
    source_id: str,
    sequence: int,
    monotonic_ns: int,
    event_type: str,
    payload: dict[str, object],
) -> validation._NativeEvent:
    return validation._NativeEvent(
        relative_path=f"raw/{source_id}.jsonl",
        line_number=sequence,
        source_kind="replica",
        source_id=source_id,
        source_instance=f"slot-{source_id}",
        source_sequence=sequence,
        monotonic_ns=monotonic_ns,
        event_type=event_type,
        payload=payload,
        line_sha256=hashlib.sha256(str(payload).encode()).hexdigest(),
    )


def _commit_event(
    sequence: int,
    monotonic_ns: int,
    height: int,
    *,
    epoch_number: int = 0,
    epoch_digest: str = "11" * 32,
    tree_id: int = 0,
) -> validation._NativeEvent:
    block_hash = f"{height:064x}"
    parent_hash = None if height == 1 else f"{height - 1:064x}"
    return _native_event(
        source_id="replica-0",
        sequence=sequence,
        monotonic_ns=monotonic_ns,
        event_type="block.committed",
        payload={
            "block_height": height,
            "block_hash": block_hash,
            "parent_hash": parent_hash,
            "transaction_count": 1000,
            "designated_observer": True,
            "decision_proof": {
                "epoch_number": epoch_number,
                "tree_id": tree_id,
                "epoch_digest": epoch_digest,
                "block_hash": block_hash,
            },
            "view_generation": 1,
            "commit_batch_index": 0,
        },
    )


def _throughput_document(
    phase_windows: dict[str, tuple[int, int, int]],
    *,
    omit_zero_bucket: bool = False,
) -> dict[str, object]:
    width_s = 5
    phases: list[dict[str, object]] = []
    phase_configurations = {
        "baseline": (0, "11" * 32),
        "fault_evidence": (0, "11" * 32),
        "epoch1_stable": (1, "22" * 32),
        "epoch2_stable": (2, "33" * 32),
    }
    for name in validation.PHASES:
        start, end, count = phase_windows[name]
        buckets = []
        for index in range(count):
            transactions = 1000 if name == "baseline" and index == 0 else 0
            buckets.append(
                {
                    "bucket_index": index,
                    "start_monotonic_ns": start + index * width_s * 1_000_000_000,
                    "end_monotonic_ns": start + (index + 1) * width_s * 1_000_000_000,
                    "transactions": transactions,
                    "tps": transactions / width_s,
                }
            )
        if omit_zero_bucket and name == "fault_evidence":
            buckets.pop()
        transactions = 1000 if name == "baseline" else 0
        phases.append(
            {
                "phase": name,
                "start_monotonic_ns": start,
                "end_monotonic_ns": end,
                "configuration": {
                    "epoch_number": phase_configurations[name][0],
                    "epoch_digest": phase_configurations[name][1],
                },
                "buckets": buckets,
                "transactions": transactions,
                "mean_tps": transactions / (count * width_s),
            }
        )
    return {
        "schema_version": 1,
        "slot_id": "slot-test",
        "bucket_width_s": width_s,
        "authority": {
            "event_type": "block.committed",
            "source_id": "replica-0",
            "source_instance": "slot-replica-0",
            "unique_commit_rule": "block_height_and_hash_exactly_once_v1",
        },
        "phases": phases,
    }


def _throughput_phase_configurations(
) -> dict[str, tuple[int, str, frozenset[int]]]:
    return {
        "baseline": (0, "11" * 32, frozenset({0})),
        "fault_evidence": (0, "11" * 32, frozenset({0})),
        "epoch1_stable": (1, "22" * 32, frozenset({0, 1})),
        "epoch2_stable": (2, "33" * 32, frozenset({0, 1})),
    }


def test_actor_and_fnv_vectors_recompute_without_runtime_decision_code() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)

    for vector in manifest.byzantine.actor_selection_vectors:
        assert derive_actor_ids(
            vector.replica_count,
            manifest.byzantine.actor_count,
            vector.scientific_seed,
        ) == vector.selected_actor_ids
    legacy = load_frozen_manifest(LEGACY_MANIFEST_PATH)
    for vector in legacy.byzantine.actor_rotation_vectors:
        assert fnv1a_rotating_actor(
            vector.sorted_actor_ids,
            epoch_number=vector.epoch_number,
            tree_id=vector.tree_id,
            epoch_digest=vector.epoch_digest,
            block_hash=vector.block_hash,
        ) == (vector.fnv1a64, vector.selected_actor)


def test_epoch_command_signature_is_independently_verified() -> None:
    signing_bytes = (
        validation._AUTHORIZED_COMMAND_DOMAIN
        + (1).to_bytes(4, "big")
        + (2).to_bytes(1, "big")
        + (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + bytes.fromhex("11" * 32)
        + bytes.fromhex("22" * 32)
        + (5).to_bytes(8, "big")
    )
    private_key = 1
    nonce = 2
    nonce_point = validation._secp256k1_multiply(
        nonce,
        (validation._SECP256K1_GX, validation._SECP256K1_GY),
    )
    assert nonce_point is not None
    r = nonce_point[0] % validation._SECP256K1_ORDER
    z = int.from_bytes(hashlib.sha256(signing_bytes).digest(), "big")
    s = (
        pow(nonce, -1, validation._SECP256K1_ORDER)
        * (z + r * private_key)
    ) % validation._SECP256K1_ORDER
    if s > validation._SECP256K1_ORDER // 2:
        s = validation._SECP256K1_ORDER - s
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    public_key = "02" + f"{validation._SECP256K1_GX:064x}"

    validation._verify_secp256k1_signature(
        signing_bytes,
        signature,
        public_key,
    )
    tampered = bytearray(signature)
    tampered[0] ^= 1
    with pytest.raises(FactorialValidationError, match="signature"):
        validation._verify_secp256k1_signature(
            signing_bytes,
            bytes(tampered),
            public_key,
        )


def test_schedule_mismatch_is_rejected_by_independent_derivation() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = json.loads(canonical_plan_bytes(build_factorial_plan(manifest)))
    first = plan["execution_schedule"][0]
    first["arm_order"] = list(reversed(first["arm_order"]))

    with pytest.raises(FactorialValidationError, match="schedule differs"):
        validate_schedule_document(plan, manifest)


@pytest.mark.parametrize(
    "leak",
    (
        ("adaptation-manager", "--experiment-rotating-omission-actors", "9,10,12"),
        ("adaptation-manager",),
    ),
)
def test_manager_actor_truth_leak_is_rejected(leak: tuple[str, ...]) -> None:
    events = () if len(leak) > 1 else ({"actor_ids": [9, 10, 12]},)
    with pytest.raises(FactorialValidationError, match="actor truth"):
        validate_manager_blinding(leak, events)


def test_forged_selector_winner_is_rejected_even_with_refreshed_outer_digest() -> None:
    trees = tuple(
        Tree(
            tree_id=tree_id,
            fanout=2,
            pipeline_stretch=2,
            members=tuple((member + tree_id) % 4 for member in range(4)),
            wait_exempt=(),
        )
        for tree_id in range(3)
    )
    scores = tuple(
        ReplicaScore(
            replica_id=replica_id,
            classification="responsive",
            eligible=True,
            attempt_count=32,
            response_rate_ppm=1_000_000,
            timeout_rate_ppm=0,
            latency_percentile_us=100,
        )
        for replica_id in range(4)
    )
    arguments = {
        "epoch_number": 1,
        "epoch_digest": "12" * 32,
        "trees": trees,
        "scores": scores,
        "evidence_cutoff": 64,
        "candidate_fanouts": (2, 3),
        "tree_count": 3,
        "pipeline_stretch": 2,
        "deterministic_seed": 41719,
        "apply_selected": True,
    }
    valid = validation._recompute_shape_decision(**arguments)
    forged = copy.deepcopy(valid)
    forged["selected_fanout"] = 2 if valid["selected_fanout"] == 3 else 3
    forged["applied_fanout"] = forged["selected_fanout"]
    forged["decision_digest"] = validation._shape_decision_digest(forged)

    with pytest.raises(FactorialValidationError, match="independent selector"):
        validate_shape_decision(forged, **arguments)


def test_duplicate_authoritative_commit_is_rejected() -> None:
    windows = {
        phase: (1 + index * 5_000_000_000, 1 + (index + 1) * 5_000_000_000, 1)
        for index, phase in enumerate(validation.PHASES)
    }
    event = _commit_event(1, windows["baseline"][0] + 1, 1)
    duplicate = replace(event, source_sequence=2, line_number=2, monotonic_ns=event.monotonic_ns + 1)

    with pytest.raises(FactorialValidationError, match="duplicated authoritative"):
        validate_throughput_document(
            _throughput_document(windows),
            slot_id="slot-test",
            replica_events={0: (event, duplicate)},
            phase_windows=windows,
            phase_configurations=_throughput_phase_configurations(),
            bucket_width_s=5,
            observer_id="replica-0",
            observer_instance="slot-replica-0",
        )


def test_missing_zero_bucket_is_not_equivalent_to_explicit_zero() -> None:
    windows = {
        phase: (1 + index * 5_000_000_000, 1 + (index + 1) * 5_000_000_000, 1)
        for index, phase in enumerate(validation.PHASES)
    }
    event = _commit_event(1, windows["baseline"][0] + 1, 1)
    valid = _throughput_document(windows)
    metrics = validate_throughput_document(
        valid,
        slot_id="slot-test",
        replica_events={0: (event,)},
        phase_windows=windows,
        phase_configurations=_throughput_phase_configurations(),
        bucket_width_s=5,
        observer_id="replica-0",
        observer_instance="slot-replica-0",
    )
    assert next(metric for metric in metrics if metric.phase == "fault_evidence").buckets_tps == (0.0,)

    with pytest.raises(FactorialValidationError, match="omits an explicit bucket"):
        validate_throughput_document(
            _throughput_document(windows, omit_zero_bucket=True),
            slot_id="slot-test",
            replica_events={0: (event,)},
            phase_windows=windows,
            phase_configurations=_throughput_phase_configurations(),
            bucket_width_s=5,
            observer_id="replica-0",
            observer_instance="slot-replica-0",
        )


def test_epoch0_commits_cannot_populate_epoch1_throughput() -> None:
    windows = {
        phase: (1 + index * 5_000_000_000, 1 + (index + 1) * 5_000_000_000, 1)
        for index, phase in enumerate(validation.PHASES)
    }
    epoch0 = _commit_event(1, windows["epoch1_stable"][0] + 1, 1)
    document = _throughput_document(windows)
    epoch1 = next(
        phase for phase in document["phases"] if phase["phase"] == "epoch1_stable"
    )
    epoch1["buckets"][0]["transactions"] = 1000
    epoch1["buckets"][0]["tps"] = 200.0
    epoch1["transactions"] = 1000
    epoch1["mean_tps"] = 200.0
    baseline = next(
        phase for phase in document["phases"] if phase["phase"] == "baseline"
    )
    baseline["buckets"][0]["transactions"] = 0
    baseline["buckets"][0]["tps"] = 0.0
    baseline["transactions"] = 0
    baseline["mean_tps"] = 0.0

    with pytest.raises(FactorialValidationError, match="epoch1_stable"):
        validate_throughput_document(
            document,
            slot_id="slot-test",
            replica_events={0: (epoch0,)},
            phase_windows=windows,
            phase_configurations={
                "baseline": (0, "11" * 32, frozenset({0})),
                "fault_evidence": (0, "11" * 32, frozenset({0})),
                "epoch1_stable": (1, "22" * 32, frozenset({0, 1})),
                "epoch2_stable": (2, "33" * 32, frozenset({0, 1})),
            },
            bucket_width_s=5,
            observer_id="replica-0",
            observer_instance="slot-replica-0",
        )


def test_huge_numeric_tps_is_rejected_without_overflow() -> None:
    windows = {
        phase: (1 + index * 5_000_000_000, 1 + (index + 1) * 5_000_000_000, 1)
        for index, phase in enumerate(validation.PHASES)
    }
    event = _commit_event(1, windows["baseline"][0] + 1, 1)
    document = _throughput_document(windows)
    document["phases"][0]["buckets"][0]["tps"] = 10**10_000

    with pytest.raises(FactorialValidationError, match="numeric bound"):
        validate_throughput_document(
            document,
            slot_id="slot-test",
            replica_events={0: (event,)},
            phase_windows=windows,
            phase_configurations=_throughput_phase_configurations(),
            bucket_width_s=5,
            observer_id="replica-0",
            observer_instance="slot-replica-0",
        )


@pytest.mark.parametrize(
    "line",
    (
        "KAURI_FAULT marker_skipped marker=aggregate_omitted reason=capacity\n",
        (
            "KAURI_FAULT fault=rotating_intermittent_omission_v1 "
            "proposal_epoch=0 proposal_tree=0 proposal_epoch_digest={digest} "
            "proposal_block_hash={digest} window=w "
            "window_start_monotonic_ns=1 window_end_monotonic_ns=10 "
            "actor=1 action=capacity_exhausted monotonic_ns=2\n"
        ).format(digest="11" * 32),
    ),
)
def test_capacity_or_skipped_fault_marker_fails_closed(tmp_path: Path, line: str) -> None:
    log = tmp_path / "replica.log"
    log.write_text(line, encoding="utf-8")

    with pytest.raises(FactorialValidationError, match="skipped|capacity"):
        validation._fault_markers(tmp_path, {0: ("replica.log",)})


def test_full_causal_gate_rejects_disjoint_persistent_interior_proposals() -> None:
    replica_count = 13
    actors = (9, 10, 12)
    epoch_digest = "22" * 32
    epoch1_digest = "44" * 32
    epoch2_digest = "66" * 32
    trees = tuple(
        Tree(
            tree_id=tree_id,
            fanout=5,
            pipeline_stretch=2,
            members=tuple((member + tree_id) % replica_count for member in range(replica_count)),
            wait_exempt=(),
        )
        for tree_id in range(replica_count)
    )
    epoch1_trees = tuple(
        Tree(
            tree_id=tree_id,
            fanout=5,
            pipeline_stretch=2,
            members=tuple(
                member
                for member in range(replica_count)
                if member != actors[tree_id % len(actors)]
            )
            + (actors[tree_id % len(actors)],),
            wait_exempt=actors,
        )
        for tree_id in range(replica_count)
    )
    epoch2_trees = epoch1_trees
    markers: list[FaultMarker] = []
    evidence: list[validation._EvidenceRecord] = []
    events_by_actor: dict[int, list[validation._NativeEvent]] = {
        actor: [] for actor in actors
    }
    evidence_sequence = 2

    def selected_hash(
        *, epoch: int, tree_id: int, digest: str, actor: int, base: int
    ) -> str:
        counter = base
        while True:
            block_hash = f"{counter:064x}"
            _, selected = fnv1a_rotating_actor(
                actors,
                epoch_number=epoch,
                tree_id=tree_id,
                epoch_digest=digest,
                block_hash=block_hash,
            )
            if selected == actor:
                return block_hash
            counter += 1

    for offset, actor in enumerate(actors):
        for guard_offset, tree_id in enumerate((actor - 1, actor - 2)):
            block_hash = selected_hash(
                epoch=0,
                tree_id=tree_id,
                digest=epoch_digest,
                actor=actor,
                base=1 + offset * 100 + guard_offset * 10,
            )
            marker_ns = 200 + offset * 10 + guard_offset
            markers.append(
                FaultMarker(
                    source_replica=actor,
                    line_number=len(markers) + 1,
                    fault_mode="rotating_intermittent_omission_v1",
                    epoch_number=0,
                    tree_id=tree_id,
                    epoch_digest=epoch_digest,
                    block_hash=block_hash,
                    window="w",
                    window_start_ns=100,
                    window_end_ns=1200,
                    actor=actor,
                    action="omit_aggregate",
                    monotonic_ns=marker_ns,
                    raw_line_sha256="33" * 32,
                )
            )
            position = trees[tree_id].members.index(actor)
            parent = trees[tree_id].members[(position - 1) // trees[tree_id].fanout]
            evidence.append(
                validation._EvidenceRecord(
                    ingestion_sequence=evidence_sequence,
                    acceptance_monotonic_ns=300 + offset * 10 + guard_offset,
                    observation_id=f"{evidence_sequence:064x}",
                    reporter_id=parent,
                    target_id=actor,
                    epoch_number=0,
                    tree_id=tree_id,
                    epoch_digest=epoch_digest,
                    block_hash=block_hash,
                    message_type="aggregate_relay",
                    outcome="timeout",
                    response_duration_us=0,
                    deadline_duration_us=10,
                    reporter_monotonic_ns=250 + offset * 10 + guard_offset,
                    reporter_sequence=guard_offset + 1,
                    signer_set=(),
                )
            )
            evidence_sequence += 1
            events_by_actor[actor].append(
                _native_event(
                    source_id=f"replica-{actor}",
                    sequence=len(events_by_actor[actor]) + 1,
                    monotonic_ns=marker_ns - 10,
                    event_type="proposal.received",
                    payload={
                        "configuration": {
                            "epoch_number": 0,
                            "tree_id": tree_id,
                            "epoch_digest": epoch_digest,
                        },
                        "block_hash": block_hash,
                        "observer_replica": actor,
                    },
                )
            )
        epoch1_tree_id = offset
        epoch1_block_hash = selected_hash(
            epoch=1,
            tree_id=epoch1_tree_id,
            digest=epoch1_digest,
            actor=actor,
            base=1000 + offset * 100,
        )
        markers.append(
            FaultMarker(
                source_replica=actor,
                line_number=len(markers) + 1,
                fault_mode="rotating_intermittent_omission_v1",
                epoch_number=1,
                tree_id=epoch1_tree_id,
                epoch_digest=epoch1_digest,
                block_hash=epoch1_block_hash,
                window="w",
                window_start_ns=100,
                window_end_ns=1200,
                actor=actor,
                action="omit_direct_vote",
                monotonic_ns=700 + offset,
                raw_line_sha256="55" * 32,
            )
        )
        events_by_actor[actor].append(
            _native_event(
                source_id=f"replica-{actor}",
                sequence=len(events_by_actor[actor]) + 1,
                monotonic_ns=690 + offset,
                event_type="proposal.received",
                payload={
                    "configuration": {
                        "epoch_number": 1,
                        "tree_id": epoch1_tree_id,
                        "epoch_digest": epoch1_digest,
                    },
                    "block_hash": epoch1_block_hash,
                    "observer_replica": actor,
                },
            )
        )
        epoch2_tree_id = offset
        epoch2_block_hash = selected_hash(
            epoch=2,
            tree_id=epoch2_tree_id,
            digest=epoch2_digest,
            actor=actor,
            base=2000 + offset * 100,
        )
        markers.append(
            FaultMarker(
                source_replica=actor,
                line_number=len(markers) + 1,
                fault_mode="rotating_intermittent_omission_v1",
                epoch_number=2,
                tree_id=epoch2_tree_id,
                epoch_digest=epoch2_digest,
                block_hash=epoch2_block_hash,
                window="w",
                window_start_ns=100,
                window_end_ns=1200,
                actor=actor,
                action="omit_direct_vote",
                monotonic_ns=1000 + offset,
                raw_line_sha256="77" * 32,
            )
        )
        events_by_actor[actor].append(
            _native_event(
                source_id=f"replica-{actor}",
                sequence=len(events_by_actor[actor]) + 1,
                monotonic_ns=990 + offset,
                event_type="proposal.received",
                payload={
                    "configuration": {
                        "epoch_number": 2,
                        "tree_id": epoch2_tree_id,
                        "epoch_digest": epoch2_digest,
                    },
                    "block_hash": epoch2_block_hash,
                    "observer_replica": actor,
                },
            )
        )
    events = {actor: tuple(rows) for actor, rows in events_by_actor.items()}
    phase_windows = {
        "baseline": (10, 90, 1),
        "fault_evidence": (100, 400, 1),
        "epoch1_stable": (650, 800, 1),
        "epoch2_stable": (950, 1100, 1),
    }
    arguments = dict(
        replica_events=events,
        actor_ids=actors,
        fault_mode="rotating_intermittent_omission_v1",
        max_omissions_per_proposal=1,
        initial_epoch_digest=epoch_digest,
        initial_trees=trees,
        window_id="w",
        window_start_ns=100,
        window_end_ns=1200,
        epoch1_command_ns=500,
        epoch1_activation_ns=600,
        epoch2_command_ns=900,
        epoch1_digest=epoch1_digest,
        epoch1_trees=epoch1_trees,
        epoch2_digest=epoch2_digest,
        epoch2_trees=epoch2_trees,
        phase_windows=phase_windows,
        required_reporters=2,
        accepted_epoch0=evidence,
        baseline_cutoff=1,
        current_cutoff=10,
    )
    validate_fault_causality(markers=markers, **arguments)

    actor_internal = [
        marker
        for marker in markers
        if marker.actor == actors[-1] and marker.action == "omit_aggregate"
    ]
    missing_guard = [marker for marker in markers if marker != actor_internal[-1]]
    with pytest.raises(FactorialValidationError, match=r"full causally bound f\+1"):
        validate_fault_causality(markers=missing_guard, **arguments)

    missing_epoch1 = [
        marker
        for marker in markers
        if not (marker.actor == actors[-1] and marker.epoch_number == 1)
    ]
    with pytest.raises(FactorialValidationError, match="Epoch1-stable"):
        validate_fault_causality(markers=missing_epoch1, **arguments)

    missing_epoch2 = [
        marker
        for marker in markers
        if not (marker.actor == actors[-1] and marker.epoch_number == 2)
    ]
    with pytest.raises(FactorialValidationError, match="Epoch2-stable"):
        validate_fault_causality(markers=missing_epoch2, **arguments)

    mismatched_evidence = [
        replace(evidence[0], block_hash="99" * 32),
        *evidence[1:],
    ]
    with pytest.raises(FactorialValidationError, match="no exact outstanding timeout"):
        validate_fault_causality(
            markers=markers,
            **{**arguments, "accepted_epoch0": mismatched_evidence},
        )

    wrong_message_type = [
        replace(evidence[0], message_type="direct_vote"),
        *evidence[1:],
    ]
    with pytest.raises(FactorialValidationError, match="no exact outstanding timeout"):
        validate_fault_causality(
            markers=markers,
            **{**arguments, "accepted_epoch0": wrong_message_type},
        )

    wrong_reporter = [
        replace(evidence[0], reporter_id=(evidence[0].reporter_id + 1) % replica_count),
        *evidence[1:],
    ]
    with pytest.raises(FactorialValidationError, match="no exact outstanding timeout"):
        validate_fault_causality(
            markers=markers,
            **{**arguments, "accepted_epoch0": wrong_reporter},
        )

    persistent_markers = tuple(
        replace(marker, fault_mode="persistent_selected_omission_v1")
        for marker in markers
    )
    persistent_windows = {
        phase: (start, end, 6)
        for phase, (start, end, _bucket_count) in phase_windows.items()
    }
    with pytest.raises(
        FactorialValidationError,
        match="persistent interior proposal actor/action set",
    ):
        validate_fault_causality(
            markers=persistent_markers,
            **{
                **arguments,
                "fault_mode": "persistent_selected_omission_v1",
                "max_omissions_per_proposal": 3,
                "phase_windows": persistent_windows,
            },
        )


def test_persistent_fault_schedule_bounds_selected_actors_per_proposal() -> None:
    actors = (9, 10, 12)
    markers = tuple(
        FaultMarker(
            source_replica=actor,
            line_number=index,
            fault_mode="persistent_selected_omission_v1",
            epoch_number=0,
            tree_id=4,
            epoch_digest="11" * 32,
            block_hash="22" * 32,
            window="persistent-window",
            window_start_ns=100,
            window_end_ns=1_000,
            actor=actor,
            action="omit_aggregate",
            monotonic_ns=200 + index,
            raw_line_sha256=f"{index:064x}",
        )
        for index, actor in enumerate(actors, start=1)
    )

    validation._validate_fault_marker_schedule(
        markers,
        actor_ids=actors,
        fault_mode="persistent_selected_omission_v1",
        max_omissions_per_proposal=3,
    )

    validation._validate_fault_marker_schedule(
        markers[:-1],
        actor_ids=actors,
        fault_mode="persistent_selected_omission_v1",
        max_omissions_per_proposal=3,
    )
    with pytest.raises(FactorialValidationError, match="mode/actor"):
        validation._validate_fault_marker_schedule(
            (replace(markers[0], actor=8, source_replica=8),),
            actor_ids=actors,
            fault_mode="persistent_selected_omission_v1",
            max_omissions_per_proposal=3,
        )
    with pytest.raises(FactorialValidationError, match="no-op forward"):
        validation._validate_fault_marker_schedule(
            (replace(markers[0], action="forward"),),
            actor_ids=actors,
            fault_mode="persistent_selected_omission_v1",
            max_omissions_per_proposal=3,
        )
    with pytest.raises(FactorialValidationError, match="omission bound"):
        validation._validate_fault_marker_schedule(
            (*markers, replace(markers[0], line_number=99)),
            actor_ids=actors,
            fault_mode="persistent_selected_omission_v1",
            max_omissions_per_proposal=3,
        )


def test_persistent_interior_proposal_accepts_exact_non_root_actor_actions() -> None:
    actors = (1, 2, 9)
    tree = Tree(
        tree_id=4,
        fanout=5,
        pipeline_stretch=2,
        members=tuple(range(13)),
        wait_exempt=actors,
    )
    phase_windows = {
        "fault_evidence": (100, 700, 6),
        "epoch1_stable": (800, 1_400, 6),
        "epoch2_stable": (1_500, 2_100, 6),
    }
    markers: list[FaultMarker] = []
    actions = {1: "omit_aggregate", 2: "omit_aggregate", 9: "omit_direct_vote"}
    for epoch, digest, timestamp in (
        (0, "11" * 32, 300),
        (1, "22" * 32, 1_000),
        (2, "33" * 32, 1_700),
    ):
        for actor in actors:
            markers.append(
                FaultMarker(
                    source_replica=actor,
                    line_number=len(markers) + 1,
                    fault_mode="persistent_selected_omission_v1",
                    epoch_number=epoch,
                    tree_id=tree.tree_id,
                    epoch_digest=digest,
                    block_hash=f"{epoch + 1:064x}",
                    window="persistent-window",
                    window_start_ns=100,
                    window_end_ns=2_100,
                    actor=actor,
                    action=actions[actor],
                    monotonic_ns=timestamp + actor,
                    raw_line_sha256=f"{len(markers) + 1:064x}",
                )
            )

    validation._validate_persistent_interior_proposals(
        markers,
        actor_ids=actors,
        phase_windows=phase_windows,
        phase_configurations=(
            ("fault_evidence", 0, "11" * 32, {tree.tree_id: tree}),
            ("epoch1_stable", 1, "22" * 32, {tree.tree_id: tree}),
            ("epoch2_stable", 2, "33" * 32, {tree.tree_id: tree}),
        ),
    )


@pytest.mark.parametrize("elapsed_s", (21, 50))
def test_post_selection_deadline_is_v2_only_for_legacy_compatibility(
    elapsed_s: int,
) -> None:
    shape_ns = 100 * validation._NANOSECONDS_PER_SECOND
    terminal_ns = shape_ns + elapsed_s * validation._NANOSECONDS_PER_SECOND

    validation._validate_transition_terminal_deadline(
        shape_ns=shape_ns,
        terminal_ns=terminal_ns,
        observation_bound_rule="phase_deadline_v1",
        convergence_deadline_s=20,
    )
    with pytest.raises(FactorialValidationError, match="post-selection deadline"):
        validation._validate_transition_terminal_deadline(
            shape_ns=shape_ns,
            terminal_ns=terminal_ns,
            observation_bound_rule=(
                "shared_slot_hard_deadline_until_manager_selection_v1"
            ),
            convergence_deadline_s=20,
        )


def _phase_cutoff_fixture() -> tuple[
    dict[str, object],
    dict[tuple[str, int], validation._NativeEvent],
    tuple[validation._NativeEvent, ...],
    dict[int, tuple[validation._NativeEvent, ...]],
]:
    timestamps = {
        "baseline_stable": 45_000_000_000,
        "fault_window_open": 50_000_000_000,
        "epoch1_command": 55_000_000_000,
        "epoch1_activation": 56_000_000_000,
        "epoch1_stable": 57_000_000_000,
        "shape_v1_computed": 62_000_000_000,
        "epoch2_command": 63_000_000_000,
        "epoch2_activation": 64_000_000_000,
        "epoch2_stable": 65_000_000_000,
        "epoch2_drain_complete": 75_000_000_000,
    }
    event_types = {
        "baseline_stable": "block.committed",
        "epoch1_command": "epoch.command_committed",
        "epoch1_activation": "epoch.activated",
        "epoch1_stable": "block.committed",
        "shape_v1_computed": "adaptive_v2_shape_decision",
        "epoch2_command": "epoch.command_committed",
        "epoch2_activation": "epoch.activated",
        "epoch2_stable": "block.committed",
        "epoch2_drain_complete": "block.committed",
    }
    manager_events = [
        _native_event(
            source_id="adaptive-manager",
            sequence=1,
            monotonic_ns=5_000_000_000,
            event_type="process.ready",
            payload={},
        )
    ]
    replica_streams: dict[int, list[validation._NativeEvent]] = {
        0: [
            _native_event(
                source_id="replica-0",
                sequence=1,
                monotonic_ns=4_000_000_000,
                event_type="process.ready",
                payload={},
            )
        ],
        1: [
            _native_event(
                source_id="replica-1",
                sequence=1,
                monotonic_ns=4_500_000_000,
                event_type="process.ready",
                payload={},
            )
        ],
    }

    def committed(
        timestamp_ns: int,
        height: int,
        *,
        epoch_number: int = 0,
        epoch_digest: str = "11" * 32,
    ) -> validation._NativeEvent:
        event = _commit_event(
            len(replica_streams[0]) + 1,
            timestamp_ns,
            height,
            epoch_number=epoch_number,
            epoch_digest=epoch_digest,
        )
        replica_streams[0].append(event)
        return event

    def observed(
        replica_id: int,
        timestamp_ns: int,
        commit: validation._NativeEvent,
    ) -> validation._NativeEvent:
        payload = commit.payload
        event = _native_event(
            source_id=f"replica-{replica_id}",
            sequence=len(replica_streams[replica_id]) + 1,
            monotonic_ns=timestamp_ns,
            event_type="block.commit_observed",
            payload={
                "block_height": payload["block_height"],
                "block_hash": payload["block_hash"],
                "parent_hash": payload["parent_hash"],
                "transaction_count": payload["transaction_count"],
                "commit_batch_index": payload["commit_batch_index"],
            },
        )
        replica_streams[replica_id].append(event)
        return event

    baseline_common = committed(41_000_000_000, 10)
    baseline_witnesses = (
        observed(0, 42_000_000_000, baseline_common),
        observed(1, 43_000_000_000, baseline_common),
    )
    cutoff_commits = {
        "baseline_stable": committed(45_000_000_000, 11),
        "baseline_unqualified_later": committed(48_000_000_000, 12),
        "fault_progress": committed(52_000_000_000, 13),
        "epoch1_stable": committed(
            57_000_000_000, 14, epoch_number=1, epoch_digest="22" * 32
        ),
    }
    epoch1_witnesses = (
        observed(0, 58_000_000_000, cutoff_commits["epoch1_stable"]),
        observed(1, 59_000_000_000, cutoff_commits["epoch1_stable"]),
    )
    cutoff_commits["epoch1_later"] = committed(
        60_000_000_000, 15, epoch_number=1, epoch_digest="22" * 32
    )
    cutoff_commits["epoch2_stable"] = committed(
        65_000_000_000, 16, epoch_number=2, epoch_digest="33" * 32
    )
    epoch2_witnesses = (
        observed(0, 66_000_000_000, cutoff_commits["epoch2_stable"]),
        observed(1, 67_000_000_000, cutoff_commits["epoch2_stable"]),
    )
    cutoff_commits["early_drain"] = committed(
        72_000_000_000, 17, epoch_number=2, epoch_digest="33" * 32
    )
    cutoff_commits["epoch2_drain_complete"] = committed(
        75_000_000_000, 18, epoch_number=2, epoch_digest="33" * 32
    )

    transition_events: dict[str, validation._NativeEvent] = {}
    for name in (
        "epoch1_command",
        "epoch1_activation",
        "shape_v1_computed",
        "epoch2_command",
        "epoch2_activation",
    ):
        manager_source = "shape" in name
        payload: dict[str, object] = {}
        if name == "epoch1_activation":
            payload = {
                "configuration": {
                    "epoch_number": 1,
                    "tree_id": 0,
                    "epoch_digest": "22" * 32,
                },
                "activation_height": 14,
            }
        elif name == "epoch2_activation":
            payload = {
                "configuration": {
                    "epoch_number": 2,
                    "tree_id": 0,
                    "epoch_digest": "33" * 32,
                },
                "activation_height": 16,
            }
        event = _native_event(
            source_id="adaptive-manager" if manager_source else "replica-0",
            sequence=(
                len(manager_events) + 1
                if manager_source
                else len(replica_streams[0]) + 1
            ),
            monotonic_ns=timestamps[name],
            event_type=event_types[name],
            payload=payload,
        )
        transition_events[name] = event
        if manager_source:
            manager_events.append(event)
        else:
            replica_streams[0].append(event)

    named_events = {
        "baseline_stable": cutoff_commits["baseline_stable"],
        "epoch1_command": transition_events["epoch1_command"],
        "epoch1_activation": transition_events["epoch1_activation"],
        "epoch1_stable": cutoff_commits["epoch1_stable"],
        "shape_v1_computed": transition_events["shape_v1_computed"],
        "epoch2_command": transition_events["epoch2_command"],
        "epoch2_activation": transition_events["epoch2_activation"],
        "epoch2_stable": cutoff_commits["epoch2_stable"],
        "epoch2_drain_complete": cutoff_commits["epoch2_drain_complete"],
    }
    rows: list[dict[str, object]] = []
    for name in validation.CUTOFF_NAMES:
        if name == "fault_window_open":
            rows.append(
                {
                    "name": name,
                    "source_path": "slot.json",
                    "source_sequence": 0,
                    "event_type": "fault_window.open",
                    "source_monotonic_ns": timestamps[name],
                    "event_sha256": "aa" * 32,
                }
            )
            continue
        event = named_events[name]
        rows.append(
            {
                "name": name,
                "source_path": event.relative_path,
                "source_sequence": event.source_sequence,
                "event_type": event.event_type,
                "source_monotonic_ns": event.monotonic_ns,
                "event_sha256": event.line_sha256,
            }
        )
    phases = [
        ("baseline", 40_000_000_000, 0, "11" * 32),
        ("fault_evidence", 50_000_000_000, 0, "11" * 32),
        ("epoch1_stable", 57_000_000_000, 1, "22" * 32),
        ("epoch2_stable", 65_000_000_000, 2, "33" * 32),
    ]
    document: dict[str, object] = {
        "schema_version": 1,
        "slot_id": "slot-test",
        "cutoff_rule": validation.CUTOFF_RULE,
        "cutoffs": rows,
        "phases": [
            {
                "phase": name,
                "start_monotonic_ns": start,
                "end_monotonic_ns": start + 5_000_000_000,
                "bucket_count": 1,
                "configuration": {
                    "epoch_number": epoch_number,
                    "epoch_digest": epoch_digest,
                },
            }
            for name, start, epoch_number, epoch_digest in phases
        ],
        "phase_qualifications": [],
    }

    def reference(event: validation._NativeEvent) -> dict[str, object]:
        return {
            "relative_path": event.relative_path,
            "line_number": event.line_number,
            "source_id": event.source_id,
            "source_sequence": event.source_sequence,
            "source_monotonic_ns": event.monotonic_ns,
            "event_type": event.event_type,
            "line_sha256": event.line_sha256,
        }

    for name, observer, witnesses in (
        ("baseline", baseline_common, baseline_witnesses),
        ("epoch1_stable", cutoff_commits["epoch1_stable"], epoch1_witnesses),
        ("epoch2_stable", cutoff_commits["epoch2_stable"], epoch2_witnesses),
    ):
        payload = observer.payload
        document["phase_qualifications"].append(
            {
                "phase": name,
                "common_commit": {
                    "identity": {
                        "block_height": payload["block_height"],
                        "block_hash": payload["block_hash"],
                        "parent_hash": payload["parent_hash"],
                        "transaction_count": payload["transaction_count"],
                        "decision_proof": payload["decision_proof"],
                    },
                    "observer": reference(observer),
                    "witnesses": [reference(witness) for witness in witnesses],
                    "common_monotonic_ns": max(
                        observer.monotonic_ns,
                        *(witness.monotonic_ns for witness in witnesses),
                    ),
                },
            }
        )
    replica_events = {
        replica_id: tuple(stream) for replica_id, stream in replica_streams.items()
    }
    all_events = (*manager_events, *(event for stream in replica_events.values() for event in stream))
    events = {
        (event.relative_path, event.source_sequence): event for event in all_events
    }
    return document, events, tuple(manager_events), replica_events


def test_phase_cutoffs_enforce_prefault_baseline_and_full_observation_hold() -> None:
    document, events, manager_events, replica_events = _phase_cutoff_fixture()
    arguments = {
        "slot_id": "slot-test",
        "slot_receipt_sha256": "aa" * 32,
        "window_start_ns": 50_000_000_000,
        "window_end_ns": 80_000_000_000,
        "bucket_width_s": 5,
        "bucket_counts": {phase: 1 for phase in validation.PHASES},
        "events_by_ref": events,
        "manager_events": manager_events,
        "replica_events": replica_events,
        "replica_count": 2,
        "quorum": 2,
        "drain_margin_s": 5,
    }
    validation._validate_phase_cutoffs(document, **arguments)

    with pytest.raises(FactorialValidationError, match="during the fault window"):
        validation._validate_phase_cutoffs(
            document, **{**arguments, "window_end_ns": 75_000_000_000}
        )

    early_command = copy.deepcopy(document)
    early_command["cutoffs"][2]["source_monotonic_ns"] -= 1
    early_row = early_command["cutoffs"][2]
    early_key = (early_row["source_path"], early_row["source_sequence"])
    early_event = events[early_key]
    early_command_events = dict(events)
    early_command_events[early_key] = replace(
        early_event, monotonic_ns=early_event.monotonic_ns - 1
    )
    with pytest.raises(FactorialValidationError, match="phase windows"):
        validation._validate_phase_cutoffs(
            early_command, **{**arguments, "events_by_ref": early_command_events}
        )

    cherry_picked = copy.deepcopy(document)
    later = next(
        event
        for event in replica_events[0]
        if event.event_type == "block.committed"
        and event.monotonic_ns == 48_000_000_000
    )
    cherry_picked["cutoffs"][0] = {
        "name": "baseline_stable",
        "source_path": later.relative_path,
        "source_sequence": later.source_sequence,
        "event_type": later.event_type,
        "source_monotonic_ns": later.monotonic_ns,
        "event_sha256": later.line_sha256,
    }
    cherry_picked["phases"][0]["start_monotonic_ns"] = 43_000_000_000
    cherry_picked["phases"][0]["end_monotonic_ns"] = 48_000_000_000
    with pytest.raises(FactorialValidationError, match="latest independently qualified"):
        validation._validate_phase_cutoffs(
            cherry_picked, **arguments
        )

    missing_ready = {
        **replica_events,
        1: tuple(
            event
            for event in replica_events[1]
            if event.event_type != "process.ready"
        ),
    }
    with pytest.raises(FactorialValidationError, match="exactly one process.ready"):
        validation._validate_phase_cutoffs(
            document, **{**arguments, "replica_events": missing_ready}
        )

    missing_witness = {
        **replica_events,
        1: tuple(
            event
            for event in replica_events[1]
            if not (
                event.event_type == "block.commit_observed"
                and event.monotonic_ns == 59_000_000_000
            )
        ),
    }
    with pytest.raises(FactorialValidationError, match="lacks an exact common-Q"):
        validation._validate_phase_cutoffs(
            document, **{**arguments, "replica_events": missing_witness}
        )

    epoch1_commit = next(
        event
        for event in replica_events[0]
        if event.event_type == "block.committed"
        and event.monotonic_ns == 57_000_000_000
    )
    epoch0_payload = copy.deepcopy(epoch1_commit.payload)
    epoch0_payload["decision_proof"]["epoch_number"] = 0
    epoch0_payload["decision_proof"]["epoch_digest"] = "11" * 32
    epoch0_commit = replace(epoch1_commit, payload=epoch0_payload)
    wrong_epoch_events = {
        **replica_events,
        0: tuple(
            epoch0_commit if event is epoch1_commit else event
            for event in replica_events[0]
        ),
    }
    wrong_epoch_refs = dict(events)
    wrong_epoch_refs[(epoch0_commit.relative_path, epoch0_commit.source_sequence)] = (
        epoch0_commit
    )
    with pytest.raises(FactorialValidationError, match="configuration-bound"):
        validation._validate_phase_cutoffs(
            document,
            **{
                **arguments,
                "events_by_ref": wrong_epoch_refs,
                "replica_events": wrong_epoch_events,
            },
        )

    early_drain = copy.deepcopy(document)
    early = next(
        event
        for event in replica_events[0]
        if event.event_type == "block.committed"
        and event.monotonic_ns == 72_000_000_000
    )
    early_drain["cutoffs"][-1] = {
        "name": "epoch2_drain_complete",
        "source_path": early.relative_path,
        "source_sequence": early.source_sequence,
        "event_type": early.event_type,
        "source_monotonic_ns": early.monotonic_ns,
        "event_sha256": early.line_sha256,
    }
    with pytest.raises(FactorialValidationError, match="frozen drain margin"):
        validation._validate_phase_cutoffs(early_drain, **arguments)


def test_independent_config_reconstruction_accepts_only_exact_launcher_bytes(
    tmp_path: Path,
) -> None:
    from experiments.adaptive.kauri_experiment import factorial_execution as execution

    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    slot = plan.slots[0]
    spec = next(item for item in runtime.slots if item.slot_id == slot.slot_id)
    runtime_document = json.loads(canonical_runtime_bytes(runtime))
    spec_document = next(
        item for item in runtime_document["slots"] if item["slot_id"] == slot.slot_id
    )
    expected = next(
        item for item in validation._expected_slots(manifest)
        if item.slot_id == slot.slot_id
    )
    slot_root = tmp_path / slot.slot_id
    (slot_root / "runtime").mkdir(parents=True)
    identities = execution.IdentityMaterial(
        bls=tuple(
            {"pub": f"{replica + 1:064x}", "sec": f"{replica + 101:064x}"}
            for replica in range(slot.replica_count)
        ),
        tls=tuple(
            {
                "crt": f"{replica + 201:064x}",
                "sec": f"{replica + 301:064x}",
                "cid": f"{replica + 401:064x}",
            }
            for replica in range(slot.replica_count + 1)
        ),
        issuer={"pub": f"{901:064x}", "sec": f"{902:064x}"},
    )
    identity_rows = {
        "bls": "".join(
            f"pub:{row['pub']} sec:{row['sec']}\n" for row in identities.bls
        ),
        "tls": "".join(
            f"crt:{row['crt']} sec:{row['sec']} cid:{row['cid']}\n"
            for row in identities.tls
        ),
        "issuer": (
            f"pub:{identities.issuer['pub']} sec:{identities.issuer['sec']}\n"
        ),
    }
    for name, payload in identity_rows.items():
        (slot_root / f"runtime/{name}-identities.txt").write_text(
            payload, encoding="ascii"
        )
    execution.write_slot_configs(
        slot,
        spec,
        slot_directory=slot_root,
        identities=identities,
    )

    validation._validate_materialized_configs(
        slot_root,
        runtime=spec_document,
        expected=expected,
        manifest=manifest,
    )

    with (slot_root / "runtime/main.conf").open("ab") as output:
        output.write(b"unexpected = true\n")
    with pytest.raises(FactorialValidationError, match="independent reconstruction"):
        validation._validate_materialized_configs(
            slot_root,
            runtime=spec_document,
            expected=expected,
            manifest=manifest,
        )


def _relocated_receipt_fixture(
    tmp_path: Path,
) -> tuple[
    Path,
    Path,
    dict[str, object],
    validation._ExpectedSlot,
    dict[str, object],
    bytes,
]:
    from experiments.adaptive.kauri_experiment import factorial_execution as execution

    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    spec = runtime.slots[0]
    slot = next(item for item in plan.slots if item.slot_id == spec.slot_id)
    expected = next(
        item
        for item in validation._expected_slots(manifest)
        if item.slot_id == spec.slot_id
    )
    runtime_document = json.loads(canonical_runtime_bytes(runtime))
    spec_document = next(
        item for item in runtime_document["slots"] if item["slot_id"] == spec.slot_id
    )
    original_repository = tmp_path / "removed-original" / "Kauri"
    original_slot = original_repository / spec.result_path
    recovered_slot = tmp_path / "recovered-archive" / spec.slot_id
    (recovered_slot / "runtime").mkdir(parents=True)
    tls = tuple(
        {
            "crt": f"{replica + 201:064x}",
            "sec": f"{replica + 301:064x}",
            "cid": f"{replica + 401:064x}",
        }
        for replica in range(spec.replica_count + 1)
    )
    issuer = {"pub": f"{901:064x}", "sec": f"{902:064x}"}
    (recovered_slot / "runtime/tls-identities.txt").write_text(
        "".join(
            f"crt:{row['crt']} sec:{row['sec']} cid:{row['cid']}\n" for row in tls
        ),
        encoding="ascii",
    )
    (recovered_slot / "runtime/issuer-identities.txt").write_text(
        f"pub:{issuer['pub']} sec:{issuer['sec']}\n",
        encoding="ascii",
    )
    authorization_bytes = _canonical(
        {"authorization_id": "test-relocation-authorization"}
    )
    redaction_key = execution._derive_redaction_key(
        authorization_bytes, spec.slot_id
    )
    secrets = ManagerSecretMaterial(
        manager_tls_private_key_der_hex=tls[spec.replica_count]["sec"],
        manager_tls_certificate_der_hex=tls[spec.replica_count]["crt"],
        issuer_private_key_hex=issuer["sec"],
        replica_tls_certificate_der_hex=tuple(
            row["crt"] for row in tls[: spec.replica_count]
        ),
    )
    manager = execution._redact_manager_argv(
        materialize_manager_argv(spec, original_slot, secrets),
        key=redaction_key,
        key_id=hashlib.sha256(redaction_key).hexdigest()[:16],
    )
    anchor = 1_000_000_000
    replicas = materialize_replica_argv(spec, original_slot, anchor)
    start = anchor + spec.fault_window.start_after_prelaunch_anchor_s * 1_000_000_000
    end = start + spec.fault_window.duration_s * 1_000_000_000
    receipt: dict[str, object] = {
        "schema_version": 1,
        "slot_id": spec.slot_id,
        "runtime_artifact_id": spec.artifact_id,
        "manifest_sha256": validation.FROZEN_MANIFEST_SHA256,
        "plan_sha256": validation.FROZEN_PLAN_SHA256,
        "runtime_sha256": validation.FROZEN_RUNTIME_SHA256,
        "execution_ordinal": spec.execution_ordinal,
        "attempt_ordinal": 1,
        "retry_of": None,
        "replacement_for": None,
        "shared_raw_clock_anchor_ns": anchor,
        "fault_window_start_ns": start,
        "fault_window_end_ns": end,
        "redaction_key_id": hashlib.sha256(redaction_key).hexdigest()[:16],
        "manager_argv": list(manager),
        "replica_argv": [
            {"replica_id": process.replica_id, "argv": list(process.argv)}
            for process in replicas
        ],
    }
    assert not original_repository.exists()
    return (
        recovered_slot,
        original_repository,
        receipt,
        expected,
        spec_document,
        authorization_bytes,
    )


def test_smoke_slot_authorization_must_match_the_claimed_root_envelope(
    tmp_path: Path,
) -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    runtime_bytes = execution._canonical_json_bytes(smoke.runtime.as_document())
    static_artifacts = {
        validation.MANIFEST_FILENAME: MANIFEST_PATH.read_bytes(),
        validation.PLAN_FILENAME: plan.canonical_bytes,
        validation.RUNTIME_FILENAME: runtime_bytes,
    }
    authorization = execution.build_execution_authorization_receipt(
        scope="excluded_n7_smoke",
        approval_reference="test thesis-author approval",
        approved_utc="2026-08-04T00:00:00+00:00",
        kauri_revision="ab" * 20,
        slot_ids=(smoke.slot.slot_id,),
        static_artifacts=static_artifacts,
        build_provenance_sha256="cd" * 32,
    )
    result_root = tmp_path / "smoke-root"
    slot_root = result_root / smoke.slot.slot_id
    slot_root.mkdir(parents=True)
    (slot_root / validation.AUTHORIZATION_FILENAME).write_bytes(authorization)
    root_authorization = result_root / validation.SMOKE_AUTHORIZATION_FILENAME
    root_authorization.write_bytes(authorization)
    expected = replace(
        validation._expected_slots(manifest)[0],
        slot_id=smoke.slot.slot_id,
    )

    document, payload = validation._validate_execution_authorization(
        slot_root,
        manifest=manifest,
        expected=expected,
        plan=json.loads(plan.canonical_bytes),
        runtime_sha256=hashlib.sha256(runtime_bytes).hexdigest(),
        campaign_member=False,
    )
    assert payload == authorization
    assert document["scope"] == "excluded_n7_smoke"

    root_authorization.write_bytes(authorization + b" ")
    with pytest.raises(FactorialValidationError, match="smoke root authorization"):
        validation._validate_execution_authorization(
            slot_root,
            manifest=manifest,
            expected=expected,
            plan=json.loads(plan.canonical_bytes),
            runtime_sha256=hashlib.sha256(runtime_bytes).hexdigest(),
            campaign_member=False,
        )


def _attach_relocated_build_evidence(
    result_root: Path,
    provenance: dict[str, object],
) -> None:
    """Model a durable archive copied with its already-preserved slot tree."""

    preservation_root = result_root.with_name(f"{result_root.name}-build-source")
    execution.preserve_build_evidence(preservation_root, provenance)
    shutil.move(
        str(preservation_root / execution.BUILD_EVIDENCE_DIRECTORY),
        str(result_root / execution.BUILD_EVIDENCE_DIRECTORY),
    )
    preservation_root.rmdir()


def test_receipt_and_build_provenance_validate_after_archive_relocation(
    tmp_path: Path,
) -> None:
    (
        recovered_slot,
        original_repository,
        receipt,
        expected,
        runtime,
        authorization_bytes,
    ) = _relocated_receipt_fixture(tmp_path)

    validated = validation._validate_slot_receipt(
        receipt,
        slot_root=recovered_slot,
        manifest=load_frozen_manifest(MANIFEST_PATH),
        expected=expected,
        runtime=runtime,
        runtime_sha256=validation.FROZEN_RUNTIME_SHA256,
        authorization_bytes=authorization_bytes,
    )

    assert validated[3] == original_repository / runtime["result_path"]
    assert validated[4] == original_repository

    build_directory = original_repository / "build-adaptive"
    metadata_paths = {
        "adaptation_manager_link": build_directory
        / "examples/CMakeFiles/adaptation-manager.dir/link.txt",
        "cmake_cache": build_directory / "CMakeCache.txt",
        "compile_commands": build_directory / "compile_commands.json",
        "epoch_profile_digest_link": build_directory
        / "examples/CMakeFiles/epoch-profile-digest.dir/link.txt",
        "hotstuff_app_link": build_directory
        / "examples/CMakeFiles/hotstuff-app.dir/link.txt",
        "hotstuff_keygen_link": build_directory / "CMakeFiles/hotstuff-keygen.dir/link.txt",
        "hotstuff_tls_keygen_link": build_directory
        / "CMakeFiles/hotstuff-tls-keygen.dir/link.txt",
    }
    binary_paths = {
        "app": build_directory / "examples/hotstuff-app",
        "manager": build_directory / "examples/adaptation-manager",
        "keygen": build_directory / "hotstuff-keygen",
        "tls_keygen": build_directory / "hotstuff-tls-keygen",
        "epoch_profile_digest": build_directory / "examples/epoch-profile-digest",
    }
    all_paths = {**metadata_paths, **binary_paths}
    for name, path in all_paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"exact-build-evidence:{name}\n".encode())

    def row(path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "path": str(path),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    provenance = {
        "schema_version": 1,
        "revision": "12" * 20,
        "repository": str(original_repository),
        "build_directory": str(build_directory),
        "cmake_cache_sha256": row(metadata_paths["cmake_cache"])["sha256"],
        "build_command": [
            "cmake",
            "--build",
            str(build_directory),
            "--clean-first",
            "--parallel",
            "4",
            "--target",
            "hotstuff-app",
            "adaptation-manager",
            "hotstuff-keygen",
            "hotstuff-tls-keygen",
            "epoch-profile-digest",
        ],
        "build_metadata": {name: row(path) for name, path in metadata_paths.items()},
        "binaries": {name: row(path) for name, path in binary_paths.items()},
    }
    (recovered_slot / validation.BUILD_PROVENANCE_FILENAME).write_bytes(
        _canonical(provenance)
    )
    _attach_relocated_build_evidence(recovered_slot.parent, provenance)
    shutil.rmtree(original_repository)
    assert not original_repository.exists()
    validation._validate_build_provenance(
        recovered_slot,
        revision="12" * 20,
        recorded_repository=original_repository,
    )

    archived = recovered_slot.parent / "build-evidence/binaries/app"
    archived.chmod(0o700)
    archived.write_bytes(archived.read_bytes() + b"tamper")
    with pytest.raises(FactorialValidationError, match="build evidence"):
        validation._validate_build_provenance(
            recovered_slot,
            revision="12" * 20,
            recorded_repository=original_repository,
        )


def test_v2_receipt_rejects_an_exact_legacy_manifest_plan_pair(
    tmp_path: Path,
) -> None:
    recovered, _, receipt, expected, runtime, authorization = (
        _relocated_receipt_fixture(tmp_path)
    )
    receipt["manifest_sha256"] = validation.LEGACY_MANIFEST_SHA256
    receipt["plan_sha256"] = validation.LEGACY_PLAN_SHA256

    with pytest.raises(FactorialValidationError, match="receipt identity"):
        validation._validate_slot_receipt(
            receipt,
            slot_root=recovered,
            manifest=load_frozen_manifest(MANIFEST_PATH),
            expected=expected,
            runtime=runtime,
            runtime_sha256=validation.FROZEN_RUNTIME_SHA256,
            authorization_bytes=authorization,
        )


def test_build_evidence_rejects_extra_and_symlink_entries(tmp_path: Path) -> None:
    (
        recovered_slot,
        original_repository,
        _receipt,
        _expected,
        _runtime,
        _authorization,
    ) = _relocated_receipt_fixture(tmp_path)
    build_directory = original_repository / "build-adaptive"
    binary_paths = {
        "app": build_directory / "examples/hotstuff-app",
        "manager": build_directory / "examples/adaptation-manager",
        "keygen": build_directory / "hotstuff-keygen",
        "tls_keygen": build_directory / "hotstuff-tls-keygen",
        "epoch_profile_digest": build_directory / "examples/epoch-profile-digest",
    }
    metadata_paths = {
        "adaptation_manager_link": build_directory
        / "examples/CMakeFiles/adaptation-manager.dir/link.txt",
        "cmake_cache": build_directory / "CMakeCache.txt",
        "compile_commands": build_directory / "compile_commands.json",
        "epoch_profile_digest_link": build_directory
        / "examples/CMakeFiles/epoch-profile-digest.dir/link.txt",
        "hotstuff_app_link": build_directory
        / "examples/CMakeFiles/hotstuff-app.dir/link.txt",
        "hotstuff_keygen_link": build_directory / "CMakeFiles/hotstuff-keygen.dir/link.txt",
        "hotstuff_tls_keygen_link": build_directory
        / "CMakeFiles/hotstuff-tls-keygen.dir/link.txt",
    }
    for name, path in {**binary_paths, **metadata_paths}.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())

    def row(path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "path": str(path),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    provenance = {
        "binaries": {name: row(path) for name, path in binary_paths.items()},
        "build_metadata": {name: row(path) for name, path in metadata_paths.items()},
    }
    _attach_relocated_build_evidence(recovered_slot.parent, provenance)
    evidence = recovered_slot.parent / "build-evidence"
    (evidence / "extra").write_bytes(b"extra")
    with pytest.raises(FactorialValidationError, match="unexpected"):
        validation._validate_build_evidence_archive(
            recovered_slot.parent,
            provenance,
        )
    (evidence / "extra").unlink()
    archived = evidence / "binaries/app"
    archived.unlink()
    archived.symlink_to(binary_paths["app"])
    with pytest.raises(FactorialValidationError, match="symlink"):
        validation._validate_build_evidence_archive(
            recovered_slot.parent,
            provenance,
        )


def test_relocated_receipt_rejects_authorization_tamper_and_identity_symlink(
    tmp_path: Path,
) -> None:
    recovered, _, receipt, expected, runtime, authorization = (
        _relocated_receipt_fixture(tmp_path)
    )
    with pytest.raises(FactorialValidationError, match="redaction key ID"):
        validation._validate_slot_receipt(
            receipt,
            slot_root=recovered,
            manifest=load_frozen_manifest(MANIFEST_PATH),
            expected=expected,
            runtime=runtime,
            runtime_sha256=validation.FROZEN_RUNTIME_SHA256,
            authorization_bytes=authorization + b"tamper",
        )

    tls_path = recovered / "runtime/tls-identities.txt"
    tls_copy = recovered / "runtime/tls-identities-copy.txt"
    tls_copy.write_bytes(tls_path.read_bytes())
    tls_path.unlink()
    tls_path.symlink_to(tls_copy.name)
    with pytest.raises(FactorialValidationError, match="non-symlink"):
        validation._validate_slot_receipt(
            receipt,
            slot_root=recovered,
            manifest=load_frozen_manifest(MANIFEST_PATH),
            expected=expected,
            runtime=runtime,
            runtime_sha256=validation.FROZEN_RUNTIME_SHA256,
            authorization_bytes=authorization,
        )


def test_outcome_seal_rejects_raw_hash_tamper(tmp_path: Path) -> None:
    raw = tmp_path / "raw/events.jsonl"
    raw.parent.mkdir()
    raw.write_bytes(b"original\n")
    outcome = {
        "schema_version": 1,
        "slot_id": tmp_path.name,
        "history": [
            {"sequence": 0, "state": "NOT_STARTED", "reason": None},
            {"sequence": 1, "state": "PASS", "reason": None},
        ],
        "sealed_files": {"raw/events.jsonl": hashlib.sha256(b"original\n").hexdigest()},
    }
    validation._validate_outcome(tmp_path, outcome, tmp_path.name)
    raw.write_bytes(b"tampered\n")
    with pytest.raises(FactorialValidationError, match="seal digest mismatch"):
        validation._validate_outcome(tmp_path, outcome, tmp_path.name)


def test_cleanup_validator_rejects_uncredible_signaled_exit(tmp_path: Path) -> None:
    import signal

    manifest = load_frozen_manifest(MANIFEST_PATH)
    expected = validation._expected_excluded_smoke(manifest)
    cleanup_started = 1_000
    rows = []
    for index, name in enumerate(
        ("adaptive-manager", *(f"replica-{replica}" for replica in range(expected.replica_count)))
    ):
        replica_id = None if index == 0 else index - 1
        rows.append(
            {
                "name": name,
                "replica_id": replica_id,
                "pid": 10_000 + index,
                "pgid": 10_000 + index,
                "cleanup_started_monotonic_ns": cleanup_started,
                "signal_number": int(signal.SIGTERM),
                "returncode": -int(signal.SIGTERM),
                "classification": "expected_cleanup",
                "exit_authorization": None,
            }
        )
    document = {
        "schema_version": 1,
        "slot_id": expected.slot_id,
        "cleanup_started_monotonic_ns": cleanup_started,
        "cleanup_completed": True,
        "streams_closed": True,
        "ports_clear": True,
        "final_streams_complete": True,
        "processes": rows,
        "error": None,
    }
    (tmp_path / validation.CLEANUP_LEDGER_FILENAME).write_bytes(_canonical(document))
    terminal = _native_event(
        source_id="adaptive-manager",
        sequence=1,
        monotonic_ns=500,
        event_type="adaptive_v2_session_terminal",
        payload={
            "cycle_ordinal": 1,
            "outcome": "advanced",
            "reason": "successor_converged",
        },
    )
    validation._validate_cleanup_ledger(
        tmp_path,
        expected=expected,
        manager_events=(terminal,),
        drain_complete_ns=900,
    )

    document["processes"][1]["returncode"] = 7
    (tmp_path / validation.CLEANUP_LEDGER_FILENAME).write_bytes(_canonical(document))
    with pytest.raises(FactorialValidationError, match="replica lifecycle"):
        validation._validate_cleanup_ledger(
            tmp_path,
            expected=expected,
            manager_events=(terminal,),
            drain_complete_ns=900,
        )


def _write_static_slot(
    tmp_path: Path, *, runtime_mutation: str | None = None
) -> Path:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    first = plan.slots[0]
    root = tmp_path / first.slot_id
    root.mkdir()
    (root / validation.MANIFEST_FILENAME).write_bytes(MANIFEST_PATH.read_bytes())
    (root / validation.PLAN_FILENAME).write_bytes(canonical_plan_bytes(plan))
    runtime_document = runtime.as_document()
    if runtime_mutation is not None:
        runtime_slot = next(
            slot
            for slot in runtime_document["slots"]
            if slot["slot_id"] == first.slot_id
        )
        if runtime_mutation == "actors":
            runtime_slot["actor_ids"] = []
        elif runtime_mutation == "observation_hold":
            runtime_slot["transitions"][0]["request"][
                "minimum_post_baseline_observation_ms"
            ] -= 1
        elif runtime_mutation == "fault_window":
            runtime_slot["fault_window"]["duration_s"] = 999
        elif runtime_mutation == "result_path":
            runtime_slot["result_path"] = "evil/path"
        else:  # pragma: no cover - test-helper misuse
            raise AssertionError(f"unknown mutation: {runtime_mutation}")
        runtime_payload = _canonical(runtime_document)
    else:
        runtime_payload = canonical_runtime_bytes(runtime)
    (root / validation.RUNTIME_FILENAME).write_bytes(runtime_payload)
    return root


def test_interrupted_slot_is_incomplete_not_pass(tmp_path: Path) -> None:
    result = validate_slot(_write_static_slot(tmp_path))

    assert result.outcome == "INCOMPLETE"
    assert "execution-authorization.json" in (result.reason or "")


def test_runtime_drift_fails_before_missing_raw_artifacts(tmp_path: Path) -> None:
    result = validate_slot(_write_static_slot(tmp_path, runtime_mutation="actors"))

    assert result.outcome == "FAIL"
    assert "drifted" in (result.reason or "")


@pytest.mark.parametrize("mutation", ("fault_window", "result_path"))
def test_previously_unbound_runtime_fields_fail_exact_byte_identity(
    tmp_path: Path, mutation: str
) -> None:
    result = validate_slot(_write_static_slot(tmp_path, runtime_mutation=mutation))

    assert result.outcome == "FAIL"
    assert "exact frozen campaign identity" in (result.reason or "")


def test_epoch1_observation_hold_drift_fails_before_raw_artifacts(
    tmp_path: Path,
) -> None:
    result = validate_slot(
        _write_static_slot(tmp_path, runtime_mutation="observation_hold")
    )

    assert result.outcome == "FAIL"
    assert "exact frozen campaign identity" in (result.reason or "")


def test_cross_run_jsonl_evidence_is_rejected(tmp_path: Path) -> None:
    event = {
        "event_schema_version": 1,
        "run_id": "other-slot",
        "source_kind": "replica",
        "source_id": "replica-0",
        "source_instance": "slot-replica-0",
        "source_sequence": 1,
        "source_monotonic_ns": 1,
        "event_type": "process.started",
        "payload": {"exit_status": None},
    }
    (tmp_path / "events.jsonl").write_bytes(_canonical(event))

    with pytest.raises(FactorialValidationError, match="mixed-run"):
        validation._read_jsonl(
            tmp_path,
            "events.jsonl",
            run_id="slot-test",
            source_kind="replica",
            source_id="replica-0",
            source_instance="slot-replica-0",
        )


def test_on_time_evidence_at_deadline_is_rejected() -> None:
    reporter = 0
    target = 1
    epoch = 0
    tree_id = 0
    epoch_digest = "11" * 32
    block_hash = "22" * 32
    observation_id = hashlib.sha256(
        validation._OBSERVATION_DOMAIN
        + validation._u(reporter, 2)
        + validation._u(target, 2)
        + validation._u(epoch, 4)
        + validation._u(tree_id, 4)
        + bytes.fromhex(epoch_digest)
        + bytes.fromhex(block_hash)
        + validation._u(validation._MESSAGE_TYPE_CODE["direct_vote"], 1)
    ).hexdigest()
    event = _native_event(
        source_id="adaptive-manager",
        sequence=1,
        monotonic_ns=100,
        event_type="evidence.observation_accepted",
        payload={
            "ingestion_sequence": 1,
            "observation": {
                "schema_version": 1,
                "observation_id": observation_id,
                "reporter_id": reporter,
                "observed_replica_id": target,
                "configuration": {
                    "epoch_number": epoch,
                    "tree_id": tree_id,
                    "epoch_digest": epoch_digest,
                },
                "block_hash": block_hash,
                "expected_message_type": "direct_vote",
                "outcome": "on_time",
                "response_duration_us": 10,
                "deadline_duration_us": 10,
                "reporter_monotonic_ns": 90,
                "reporter_sequence": 1,
                "signer_set": [target],
            },
        },
    )

    with pytest.raises(FactorialValidationError, match="timing/signer"):
        validation._evidence_record(event, {reporter, target})


def test_validator_has_no_runtime_decision_or_validator_dependency() -> None:
    source = Path(validation.__file__).read_text(encoding="utf-8")

    assert "from .factorial_runtime" not in source
    assert "import factorial_runtime" not in source
    assert "subprocess" not in source


def test_matched_estimate_uses_frozen_df4_interval_and_strict_claim_rule() -> None:
    estimate = validation._matched_estimate(
        "placement",
        tuple(f"b{index}" for index in range(1, 6)),
        (1.0, 2.0, 3.0, 4.0, 5.0),
    )

    assert estimate.mean_tps == pytest.approx(3.0)
    assert estimate.sample_standard_deviation_tps == pytest.approx(2.5**0.5)
    assert estimate.ci95_lower_tps == pytest.approx(1.0367568385)
    assert estimate.ci95_upper_tps == pytest.approx(4.9632431615)
    assert estimate.positive_block_count == 5
    assert estimate.directional_claim_supported is True

    inconclusive = validation._matched_estimate(
        "placement",
        tuple(f"b{index}" for index in range(1, 6)),
        (-1.0, 1.0, 1.0, 1.0, 1.0),
    )
    assert inconclusive.ci95_lower_tps < 0
    assert inconclusive.directional_claim_supported is False


def test_campaign_ledger_replays_one_shot_prefix_and_rejects_continuation(
    tmp_path: Path,
) -> None:
    from experiments.adaptive.kauri_experiment import factorial_execution as execution

    manifest = load_frozen_manifest(MANIFEST_PATH)
    plan = build_factorial_plan(manifest)
    runtime_plan = build_factorial_runtime(plan)
    runtime = runtime_plan.as_document()
    expected_slots = validation._expected_slots(manifest)
    ordered = tuple(sorted(expected_slots, key=lambda slot: slot.execution_ordinal))
    runtime_by_id = {slot.slot_id: slot for slot in runtime_plan.slots}
    static_artifacts = {
        validation.MANIFEST_FILENAME: MANIFEST_PATH.read_bytes(),
        validation.PLAN_FILENAME: canonical_plan_bytes(plan),
        validation.RUNTIME_FILENAME: canonical_runtime_bytes(runtime_plan),
    }
    authorization_bytes = execution.build_execution_authorization_receipt(
        scope="shape25_campaign",
        approval_reference="unit-test thesis-author approval",
        approved_utc="2026-08-04T00:00:00+00:00",
        kauri_revision="cd" * 20,
        slot_ids=tuple(slot.slot_id for slot in plan.slots),
        static_artifacts=static_artifacts,
        build_provenance_sha256=hashlib.sha256(
            _canonical({"revision": "cd" * 20})
        ).hexdigest(),
    )
    authorization = json.loads(authorization_bytes)
    root = tmp_path / "campaign"
    root.mkdir()
    (root / validation.CAMPAIGN_AUTHORIZATION_FILENAME).write_bytes(
        authorization_bytes
    )
    build_bytes = _canonical({"revision": "cd" * 20})
    build_digest = hashlib.sha256(build_bytes).hexdigest()
    contract = {
        "schema_version": 1,
        "campaign_id": runtime_plan.runtime_id,
        "manifest_id": runtime_plan.manifest_id,
        "manifest_sha256": validation.FROZEN_MANIFEST_SHA256,
        "plan_sha256": validation.FROZEN_PLAN_SHA256,
        "runtime_sha256": validation.FROZEN_RUNTIME_SHA256,
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": hashlib.sha256(authorization_bytes).hexdigest(),
        "kauri_revision": "cd" * 20,
        "build_provenance_sha256": build_digest,
        "execution_mode": "fixed_sequential",
        "automatic_retries": 0,
        "replacement_policy": "none",
        "outcome_dependent_order": False,
        "expected_slot_count": len(ordered),
        "execution_schedule": [
            {
                "execution_ordinal": slot.execution_ordinal,
                "slot_id": slot.slot_id,
                "block_id": slot.block_id,
                "arm_code": slot.arm_code,
            }
            for slot in ordered
        ],
    }
    contract_bytes = _canonical(contract)
    (root / validation.CAMPAIGN_CONTRACT_FILENAME).write_bytes(contract_bytes)
    contract_digest = hashlib.sha256(contract_bytes).hexdigest()

    def preserve_minimal_slot(expected: validation._ExpectedSlot) -> tuple[Path, str]:
        spec = runtime_by_id[expected.slot_id]
        recovered = root / expected.slot_id
        (recovered / "runtime").mkdir(parents=True)
        original = tmp_path / "removed" / "Kauri" / spec.result_path
        (recovered / validation.SLOT_FILENAME).write_bytes(
            _canonical(
                {
                    "replica_argv": [
                        {
                            "replica_id": 0,
                            "argv": [
                                "hotstuff-app",
                                "--conf",
                                f"{original}/runtime/main.conf",
                            ],
                        }
                    ]
                }
            )
        )
        (recovered / validation.AUTHORIZATION_FILENAME).write_bytes(
            authorization_bytes
        )
        (recovered / validation.BUILD_PROVENANCE_FILENAME).write_bytes(build_bytes)
        return recovered, str(original)

    first = ordered[0]
    first_path, first_original = preserve_minimal_slot(first)
    result = validation.SlotValidationResult(
        slot_id=first.slot_id,
        outcome="INCOMPLETE",
        reason="forced unit-test stop",
        block_id=first.block_id,
        arm_code=first.arm_code,
        replica_count=first.replica_count,
        initial_fanout=first.initial_fanout,
        integrity_valid=False,
        campaign_member=True,
    )

    def common(expected: validation._ExpectedSlot, state: str, timestamp: int) -> dict[str, object]:
        return {
            "schema_version": 1,
            "campaign_id": runtime_plan.runtime_id,
            "manifest_sha256": validation.FROZEN_MANIFEST_SHA256,
            "source_plan_sha256": validation.FROZEN_PLAN_SHA256,
            "runtime_sha256": validation.FROZEN_RUNTIME_SHA256,
            "contract_sha256": contract_digest,
            "authorization_id": authorization["authorization_id"],
            "authorization_sha256": hashlib.sha256(authorization_bytes).hexdigest(),
            "kauri_revision": "cd" * 20,
            "execution_ordinal": expected.execution_ordinal,
            "slot_id": expected.slot_id,
            "block_id": expected.block_id,
            "arm_code": expected.arm_code,
            "attempt_ordinal": 1,
            "automatic_retries": 0,
            "replacement_policy": "none",
            "state": state,
            "recorded_utc": "2026-08-04T00:00:00+00:00",
            "recorded_monotonic_ns": timestamp,
        }

    rows = [
        {
            **common(first, "STARTED", 1),
            "slot_directory": first_original,
            "preflight_revision": "cd" * 20,
            "preflight_free_bytes": runtime_plan.minimum_free_bytes,
            "build_provenance_sha256": build_digest,
        },
        {
            **common(first, "TERMINAL", 2),
            "slot_directory": first_original,
            "execution_outcome": "INCOMPLETE",
            "execution_reason": "forced unit-test stop",
            "launch_count": 0,
            "validation": {
                "outcome": result.outcome,
                "reason": result.reason,
                "integrity_valid": result.integrity_valid,
                "campaign_member": result.campaign_member,
                "figure_eligible": result.figure_eligible,
            },
        },
    ]
    ledger_path = root / validation.CAMPAIGN_LEDGER_FILENAME
    ledger_path.write_bytes(b"".join(_canonical(row) for row in rows))
    summary = {
        "schema_version": 1,
        "campaign_id": runtime_plan.runtime_id,
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": hashlib.sha256(authorization_bytes).hexdigest(),
        "contract_sha256": contract_digest,
        "ledger_sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
        "expected_slot_count": len(ordered),
        "attempted_slot_count": 1,
        "next_execution_ordinal": 2,
        "execution_complete": False,
        "stopped_reason": "forced unit-test stop",
        "completed_utc": "2026-08-04T00:00:00+00:00",
    }
    summary_path = root / validation.CAMPAIGN_SUMMARY_FILENAME
    summary_path.write_bytes(_canonical(summary))
    assert validation._validate_campaign_execution_ledger(
        root,
        manifest=manifest,
        plan=json.loads(canonical_plan_bytes(plan)),
        runtime=runtime,
        expected_slots=expected_slots,
        actual_by_id={first.slot_id: first_path},
        results_by_id={first.slot_id: result},
        campaign_outcome="INCOMPLETE",
    ) == 1

    summary_path.unlink()
    with pytest.raises(FactorialValidationError, match="campaign-execution-summary"):
        validation._validate_campaign_execution_ledger(
            root,
            manifest=manifest,
            plan=json.loads(canonical_plan_bytes(plan)),
            runtime=runtime,
            expected_slots=expected_slots,
            actual_by_id={first.slot_id: first_path},
            results_by_id={first.slot_id: result},
            campaign_outcome="INCOMPLETE",
        )
    summary_path.write_bytes(_canonical(summary))

    second = ordered[1]
    _, second_original = preserve_minimal_slot(second)
    rows.extend(
        (
            {
                **common(second, "STARTED", 3),
                "slot_directory": second_original,
                "preflight_revision": "cd" * 20,
                "preflight_free_bytes": runtime_plan.minimum_free_bytes,
                "build_provenance_sha256": build_digest,
            },
            {
                **common(second, "TERMINAL", 4),
                "slot_directory": second_original,
                "execution_outcome": "PASS",
                "execution_reason": None,
                "launch_count": second.replica_count + 1,
                "validation": {
                    "outcome": "PASS",
                    "reason": None,
                    "integrity_valid": True,
                    "campaign_member": True,
                    "figure_eligible": True,
                },
            },
        )
    )
    ledger_path.write_bytes(b"".join(_canonical(row) for row in rows))
    with pytest.raises(FactorialValidationError, match="continued after"):
        validation._validate_campaign_execution_ledger(
            root,
            manifest=manifest,
            plan=json.loads(canonical_plan_bytes(plan)),
            runtime=runtime,
            expected_slots=expected_slots,
            actual_by_id={first.slot_id: first_path},
            results_by_id={first.slot_id: result},
            campaign_outcome="INCOMPLETE",
        )
