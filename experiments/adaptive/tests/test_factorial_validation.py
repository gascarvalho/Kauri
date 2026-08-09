"""Independent fail-closed validator tests for the frozen SHAPE25 campaign."""

from __future__ import annotations

from dataclasses import replace
import copy
import hashlib
import json
import math
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
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v11.json"
)
V10_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v10.json"
)
V9_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v9.json"
)
V8_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v8.json"
)
V7_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v7.json"
)
V6_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v6.json"
)
V5_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v5.json"
)
V4_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v4.json"
)
V3_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v3.json"
)
V2_MANIFEST_PATH = (
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
    replica_id: int = 0,
) -> validation._NativeEvent:
    block_hash = f"{height:064x}"
    parent_hash = None if height == 1 else f"{height - 1:064x}"
    return _native_event(
        source_id=f"replica-{replica_id}",
        sequence=sequence,
        monotonic_ns=monotonic_ns,
        event_type="block.committed",
        payload={
            "block_height": height,
            "block_hash": block_hash,
            "parent_hash": parent_hash,
            "transaction_count": 1000,
            "designated_observer": replica_id == 0,
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


def test_responsive_degraded_vectors_recompute_with_observer_zero_isolated() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    for vector in responsive.actor_selection_vectors:
        hard = derive_actor_ids(
            vector.replica_count,
            manifest.byzantine.actor_count,
            vector.scientific_seed,
        )
        degraded = validation.derive_responsive_degraded_actor_ids(
            vector.replica_count,
            hard,
            vector.scientific_seed,
        )
        assert degraded == vector.selected_actor_ids
        assert 0 not in degraded
        assert not set(degraded).intersection(hard)
        assert len((*hard, *degraded)) == (vector.replica_count - 1) // 3


def test_validator_retains_exact_v1_through_v11_artifact_identities() -> None:
    identities = {
        version: validation._frozen_artifact_identity(
            load_frozen_manifest(path).manifest_id
        )
        for version, path in (
            (1, LEGACY_MANIFEST_PATH),
            (2, V2_MANIFEST_PATH),
            (3, V3_MANIFEST_PATH),
            (4, V4_MANIFEST_PATH),
            (5, V5_MANIFEST_PATH),
            (6, V6_MANIFEST_PATH),
            (7, V7_MANIFEST_PATH),
            (8, V8_MANIFEST_PATH),
            (9, V9_MANIFEST_PATH),
            (10, V10_MANIFEST_PATH),
            (11, MANIFEST_PATH),
        )
    }

    assert identities[1].manifest_sha256 == validation.LEGACY_MANIFEST_SHA256
    assert identities[2].manifest_sha256 == validation.V2_MANIFEST_SHA256
    assert identities[2].runtime_sha256 == validation.V2_RUNTIME_SHA256
    assert identities[3].manifest_sha256 == validation.V3_MANIFEST_SHA256
    assert identities[3].runtime_sha256 == validation.V3_RUNTIME_SHA256
    assert identities[3].smoke_runtime_sha256 == validation.V3_SMOKE_RUNTIME_SHA256
    assert identities[4].manifest_sha256 == validation.V4_MANIFEST_SHA256
    assert identities[4].runtime_sha256 == validation.V4_RUNTIME_SHA256
    assert identities[4].smoke_runtime_sha256 == validation.V4_SMOKE_RUNTIME_SHA256
    assert identities[5].manifest_sha256 == validation.V5_MANIFEST_SHA256
    assert identities[5].runtime_sha256 == validation.V5_RUNTIME_SHA256
    assert identities[5].smoke_runtime_sha256 == validation.V5_SMOKE_RUNTIME_SHA256
    assert identities[6].manifest_sha256 == validation.V6_MANIFEST_SHA256
    assert identities[6].runtime_sha256 == validation.V6_RUNTIME_SHA256
    assert identities[6].smoke_runtime_sha256 == validation.V6_SMOKE_RUNTIME_SHA256
    assert identities[7].manifest_sha256 == validation.V7_MANIFEST_SHA256
    assert identities[7].runtime_sha256 == validation.V7_RUNTIME_SHA256
    assert identities[7].smoke_runtime_sha256 == validation.V7_SMOKE_RUNTIME_SHA256
    assert identities[8].manifest_sha256 == validation.V8_MANIFEST_SHA256
    assert identities[8].runtime_sha256 == validation.V8_RUNTIME_SHA256
    assert identities[8].smoke_runtime_sha256 == validation.V8_SMOKE_RUNTIME_SHA256
    assert identities[9].manifest_sha256 == validation.V9_MANIFEST_SHA256
    assert identities[9].runtime_sha256 == validation.V9_RUNTIME_SHA256
    assert identities[9].smoke_runtime_sha256 == validation.V9_SMOKE_RUNTIME_SHA256
    assert identities[10].manifest_sha256 == validation.V10_MANIFEST_SHA256
    assert identities[10].runtime_sha256 == validation.V10_RUNTIME_SHA256
    assert (
        identities[10].smoke_runtime_sha256
        == validation.V10_SMOKE_RUNTIME_SHA256
    )
    assert identities[11].manifest_sha256 == validation.FROZEN_MANIFEST_SHA256
    assert identities[11].runtime_sha256 == validation.FROZEN_RUNTIME_SHA256
    assert (
        identities[11].smoke_runtime_sha256
        == validation.FROZEN_SMOKE_RUNTIME_SHA256
    )


@pytest.mark.parametrize(
    "manifest_path",
    (
        V4_MANIFEST_PATH,
        V5_MANIFEST_PATH,
        V6_MANIFEST_PATH,
        V7_MANIFEST_PATH,
        V8_MANIFEST_PATH,
        V9_MANIFEST_PATH,
        V10_MANIFEST_PATH,
    ),
)
def test_exact_prior_runtime_remains_validator_compatible(
    manifest_path: Path,
) -> None:
    manifest = load_frozen_manifest(manifest_path)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected for expected in validation._expected_slots(manifest)
    }
    runtime_slot = runtime.slots[0]

    validation._validate_runtime_slot(
        json.loads(json.dumps(runtime_slot.as_document())),
        expected_by_id[runtime_slot.slot_id],
        manifest,
    )


def test_validator_requires_v9_through_v11_causal_contracts_but_accepts_v8() -> None:
    explicit_v10_fields = {
        "causal_timeout_provenance_window": (
            validation.RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1
        ),
        "causal_internal_witness_candidates": (
            validation.RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1
        ),
        "causal_selection_linkage_window": (
            validation.RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1
        ),
    }
    explicit_v11_fields = {
        "marker_completeness_witness": (
            validation.RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1
        ),
        "causal_timeout_eligibility": (
            validation.RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1
        ),
    }
    for manifest_path in (
        V8_MANIFEST_PATH,
        V9_MANIFEST_PATH,
        V10_MANIFEST_PATH,
        MANIFEST_PATH,
    ):
        manifest = load_frozen_manifest(manifest_path)
        runtime = build_factorial_runtime(build_factorial_plan(manifest))
        expected_by_id = {
            expected.slot_id: expected
            for expected in validation._expected_slots(manifest)
        }
        slot = runtime.slots[0]
        document = json.loads(json.dumps(slot.as_document()))

        validation._validate_runtime_slot(
            document,
            expected_by_id[slot.slot_id],
            manifest,
        )
        if manifest_path == V8_MANIFEST_PATH:
            assert "pending_attempt_retention" not in document["tiered_cohorts"]
            assert "causal_timeout_linkage" not in document["tiered_cohorts"]
            assert not set(explicit_v10_fields).intersection(
                document["tiered_cohorts"]
            )
            assert not set(explicit_v11_fields).intersection(
                document["tiered_cohorts"]
            )
            continue

        assert document["tiered_cohorts"]["pending_attempt_retention"] == (
            validation.RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1
        )
        assert document["tiered_cohorts"]["causal_timeout_linkage"] == (
            validation.RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1
        )
        if manifest_path == V9_MANIFEST_PATH:
            assert not set(explicit_v10_fields).intersection(
                document["tiered_cohorts"]
            )
        else:
            assert {
                field: document["tiered_cohorts"][field]
                for field in explicit_v10_fields
            } == explicit_v10_fields
        if manifest_path == MANIFEST_PATH:
            assert {
                field: document["tiered_cohorts"][field]
                for field in explicit_v11_fields
            } == explicit_v11_fields
        else:
            assert not set(explicit_v11_fields).intersection(
                document["tiered_cohorts"]
            )

        drifted = copy.deepcopy(document)
        del drifted["tiered_cohorts"]["pending_attempt_retention"]
        with pytest.raises(
            FactorialValidationError,
            match="tiered cohort contract",
        ):
            validation._validate_runtime_slot(
                drifted,
                expected_by_id[slot.slot_id],
                manifest,
            )


def test_only_v10_and_v11_route_through_explicit_causal_linkage_windows() -> None:
    assert not validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V9_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(V10_MANIFEST_PATH)
    )
    assert validation._uses_explicit_causal_linkage_windows(
        load_frozen_manifest(MANIFEST_PATH)
    )


def test_only_v11_routes_through_explicit_phase_edge_eligibility() -> None:
    assert not validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(V10_MANIFEST_PATH)
    )
    assert validation._uses_explicit_phase_edge_eligibility(
        load_frozen_manifest(MANIFEST_PATH)
    )


@pytest.mark.parametrize(
    "field",
    (
        "causal_timeout_provenance_window",
        "causal_internal_witness_candidates",
        "causal_selection_linkage_window",
    ),
)
@pytest.mark.parametrize("manifest_path", (V10_MANIFEST_PATH, MANIFEST_PATH))
def test_validator_requires_each_explicit_causal_linkage_field(
    field: str,
    manifest_path: Path,
) -> None:
    manifest = load_frozen_manifest(manifest_path)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))
    del document["tiered_cohorts"][field]

    with pytest.raises(FactorialValidationError, match="tiered cohort contract"):
        validation._validate_runtime_slot(
            document,
            expected_by_id[slot.slot_id],
            manifest,
        )


@pytest.mark.parametrize(
    "field",
    ("marker_completeness_witness", "causal_timeout_eligibility"),
)
def test_validator_requires_each_explicit_v11_phase_edge_field(
    field: str,
) -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    runtime = build_factorial_runtime(build_factorial_plan(manifest))
    expected_by_id = {
        expected.slot_id: expected
        for expected in validation._expected_slots(manifest)
    }
    slot = runtime.slots[0]
    document = json.loads(json.dumps(slot.as_document()))
    del document["tiered_cohorts"][field]

    with pytest.raises(FactorialValidationError, match="tiered cohort contract"):
        validation._validate_runtime_slot(
            document,
            expected_by_id[slot.slot_id],
            manifest,
        )


def _compact_snapshot_audit() -> dict[str, object]:
    return {
        "schema_version": 2,
        "cycle_ordinal": 1,
        "policy_intent": "performance_optimization",
        "transition_artifact_id": "slot-test-epoch2",
        "predecessor_epoch_number": 1,
        "predecessor_epoch_digest": "33" * 32,
        "activation_generation": 4_294_967_297,
        "baseline_cutoff": 1,
        "current_cutoff": 3,
        "full_prefix_snapshot_id": "44" * 32,
        "evidence_snapshot_id": "55" * 32,
        "accepted_prefix_count": 3,
        "eligible_ranking": [0, 1, 2],
    }


def test_compact_snapshot_schema_and_legacy_snapshot_schema_are_exact() -> None:
    compact = _compact_snapshot_audit()
    assert validation._validate_snapshot_audit_schema(
        compact,
        evidence_snapshot_format="digest_commitment_v2",
        label="test compact snapshot",
    ) is True

    legacy = {
        key: value
        for key, value in compact.items()
        if key
        not in {
            "schema_version",
            "full_prefix_snapshot_id",
            "evidence_snapshot_id",
            "accepted_prefix_count",
        }
    }
    legacy["observations"] = []
    assert validation._validate_snapshot_audit_schema(
        legacy,
        evidence_snapshot_format="full_prefix_v1",
        label="test legacy snapshot",
    ) is False

    with pytest.raises(FactorialValidationError, match="invalid field set"):
        validation._validate_snapshot_audit_schema(
            {**compact, "observations": []},
            evidence_snapshot_format="digest_commitment_v2",
            label="test compact snapshot",
        )
    with pytest.raises(FactorialValidationError, match="schema version"):
        validation._validate_snapshot_audit_schema(
            {**compact, "schema_version": 1},
            evidence_snapshot_format="digest_commitment_v2",
            label="test compact snapshot",
        )


def test_activation_generation_matches_native_epoch_packing() -> None:
    assert validation._expected_activation_generation(0) == 1
    assert validation._expected_activation_generation(1) == 4_294_967_297
    assert validation._expected_activation_generation(1, 7) == 4_294_967_304
    assert validation._expected_activation_generation(
        0xFFFF_FFFF, 0xFFFF_FFFE
    ) == 0xFFFF_FFFF_FFFF_FFFF
    assert validation._validated_activation_generation(
        4_294_967_297,
        predecessor_epoch_number=1,
        label="test generation",
    ) == 4_294_967_297

    with pytest.raises(FactorialValidationError, match="exceed uint32"):
        validation._expected_activation_generation(0x1_0000_0000)
    with pytest.raises(FactorialValidationError, match="overflows uint64"):
        validation._expected_activation_generation(0xFFFF_FFFF, 0xFFFF_FFFF)
    with pytest.raises(FactorialValidationError, match="integer"):
        validation._validated_activation_generation(
            True,
            predecessor_epoch_number=0,
            label="test generation",
        )
    with pytest.raises(FactorialValidationError, match="canonical predecessor"):
        validation._validated_activation_generation(
            2,
            predecessor_epoch_number=0,
            label="test generation",
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("accepted_prefix_count", 2, "prefix count"),
        ("full_prefix_snapshot_id", "66" * 32, "commitments"),
        ("evidence_snapshot_id", "77" * 32, "commitments"),
    ),
)
def test_compact_snapshot_rejects_count_or_commitment_tampering(
    field: str,
    value: object,
    match: str,
) -> None:
    snapshot = _compact_snapshot_audit()
    snapshot[field] = value

    with pytest.raises(FactorialValidationError, match=match):
        validation._validate_compact_snapshot_commitments(
            snapshot,
            accepted_prefix_count=3,
            current_cutoff=3,
            full_prefix_snapshot_id="44" * 32,
            evidence_snapshot_id="55" * 32,
        )


def _sparse_evidence_record(
    ingestion_sequence: int,
    *,
    reporter_sequence: int,
) -> validation._EvidenceRecord:
    return validation._EvidenceRecord(
        ingestion_sequence=ingestion_sequence,
        acceptance_monotonic_ns=100 + ingestion_sequence,
        observation_id=f"{ingestion_sequence:064x}",
        reporter_id=0,
        target_id=1,
        epoch_number=0,
        tree_id=0,
        epoch_digest="11" * 32,
        block_hash=f"{1000 + ingestion_sequence:064x}",
        message_type="direct_vote",
        outcome="on_time",
        response_duration_us=10,
        deadline_duration_us=20,
        reporter_monotonic_ns=200 + ingestion_sequence,
        reporter_sequence=reporter_sequence,
        signer_set=(1,),
    )


def test_compact_snapshot_preserves_sparse_accepted_ledger_integrity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = (
        _sparse_evidence_record(1, reporter_sequence=1),
        _sparse_evidence_record(3, reporter_sequence=2),
    )
    events = tuple(
        _native_event(
            source_id="adaptive-manager",
            sequence=index,
            monotonic_ns=record.acceptance_monotonic_ns,
            event_type="evidence.observation_accepted",
            payload={"record_index": index - 1},
        )
        for index, record in enumerate(records, start=1)
    )
    monkeypatch.setattr(
        validation,
        "_evidence_record",
        lambda event, _membership: records[event.payload["record_index"]],
    )

    with pytest.raises(FactorialValidationError, match="non-contiguous"):
        validation._accepted_evidence(events, 4)
    grouped = validation._accepted_evidence(
        events,
        4,
        allow_ingestion_sequence_gaps=True,
    )
    assert grouped[(0, "11" * 32)] == records

    with pytest.raises(FactorialValidationError, match="exact accepted prefix"):
        validation._snapshot_records(
            records,
            baseline_cutoff=1,
            current_cutoff=4,
            suffix_only=False,
        )
    assert validation._snapshot_records(
        records,
        baseline_cutoff=1,
        current_cutoff=4,
        suffix_only=False,
        allow_high_watermark_gaps=True,
    ) == records


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


def _response_attempt_arm_line(
    *,
    reporter: int = 0,
    child: int = 2,
    start_ns: int = 1_900_000,
    duration_us: int = 500,
    absolute_deadline_ns: int | None = None,
) -> str:
    deadline_ns = (
        start_ns + duration_us * 1_000
        if absolute_deadline_ns is None
        else absolute_deadline_ns
    )
    return (
        "2026-08-05 12:00:00 [hotstuff info] "
        "KAURI_EVIDENCE response_attempt_armed "
        f"reporter={reporter} child={child} epoch=1 tree=4 "
        f"epoch_digest={'11' * 32} block={900:064x} "
        "expected_message_type=aggregate_relay "
        f"start_monotonic_ns={start_ns} "
        f"deadline_duration_us={duration_us} "
        f"absolute_deadline_ns={deadline_ns}\n"
    )


def test_v9_response_attempt_arm_parser_accepts_exact_marker(tmp_path: Path) -> None:
    log = tmp_path / "replica.log"
    log.write_text(_response_attempt_arm_line(), encoding="utf-8")

    marker = validation._response_attempt_arm_markers(
        tmp_path,
        {0: ("replica.log",)},
    )[0]

    assert marker.source_replica == marker.reporter_id == 0
    assert marker.child_id == 2
    assert marker.expected_message_type == "aggregate_relay"
    assert marker.absolute_deadline_ns == (
        marker.start_monotonic_ns + marker.deadline_duration_us * 1_000
    )


@pytest.mark.parametrize(
    "line",
    (
        _response_attempt_arm_line().replace(" child=2", ""),
        (
            "KAURI_EVIDENCE response_attempt_arm_marker_failed "
            "reason=deadline_counter reporter=0 epoch=1 tree=4\n"
        ),
    ),
)
def test_v9_response_attempt_arm_parser_rejects_malformed_or_failure_marker(
    tmp_path: Path,
    line: str,
) -> None:
    log = tmp_path / "replica.log"
    log.write_text(line, encoding="utf-8")

    with pytest.raises(FactorialValidationError, match="malformed|failed"):
        validation._response_attempt_arm_markers(
            tmp_path,
            {0: ("replica.log",)},
        )


def test_v9_response_attempt_arm_parser_rejects_source_reporter_mismatch(
    tmp_path: Path,
) -> None:
    log = tmp_path / "replica.log"
    log.write_text(_response_attempt_arm_line(reporter=1), encoding="utf-8")

    with pytest.raises(FactorialValidationError, match="source.*reporter"):
        validation._response_attempt_arm_markers(
            tmp_path,
            {0: ("replica.log",)},
        )


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

    def adaptive_payload(
        *, epoch: int, tree_id: int, digest: str, block_hash: str, actor: int
    ) -> dict[str, object]:
        return {
            "epoch_number": epoch,
            "tree_id": tree_id,
            "epoch_digest": digest,
            "block_hash": block_hash,
            "context_generation": 1,
            "observer_replica": actor,
            "wait_exempt_signers": [],
            "accepted_signers": [],
            "absent_direct_children": [],
            "missing_optional_signers": [],
            "required_branch_gaps": [],
            "root_signer_count": 0,
            "global_quorum": 0,
            "rejection_reason": None,
        }

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
                    event_type="aggregation.required_set_ready",
                    payload=adaptive_payload(
                        epoch=0,
                        tree_id=tree_id,
                        digest=epoch_digest,
                        block_hash=block_hash,
                        actor=actor,
                    ),
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
                event_type="aggregation.required_set_ready",
                payload=adaptive_payload(
                    epoch=1,
                    tree_id=epoch1_tree_id,
                    digest=epoch1_digest,
                    block_hash=epoch1_block_hash,
                    actor=actor,
                ),
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
                event_type="aggregation.required_set_ready",
                payload=adaptive_payload(
                    epoch=2,
                    tree_id=epoch2_tree_id,
                    digest=epoch2_digest,
                    block_hash=epoch2_block_hash,
                    actor=actor,
                ),
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

    actor = actors[0]
    actor_events = events[actor]
    first_proposal = actor_events[0]
    cross_observer = 0
    cross_observer_event = _native_event(
        source_id=f"replica-{cross_observer}",
        sequence=1,
        monotonic_ns=first_proposal.monotonic_ns,
        event_type=first_proposal.event_type,
        payload={**first_proposal.payload, "observer_replica": cross_observer},
    )
    cross_observer_events = {
        **events,
        actor: actor_events[1:],
        cross_observer: (cross_observer_event,),
    }
    validate_fault_causality(
        markers=markers,
        **{**arguments, "replica_events": cross_observer_events},
    )

    authoritative_commit = _native_event(
        source_id="replica-0",
        sequence=1,
        monotonic_ns=first_proposal.monotonic_ns + 20,
        event_type="block.committed",
        payload={
            "block_height": 1,
            "block_hash": first_proposal.payload["block_hash"],
            "parent_hash": None,
            "transaction_count": 1000,
            "designated_observer": True,
            "decision_proof": {
                "epoch_number": first_proposal.payload["epoch_number"],
                "tree_id": first_proposal.payload["tree_id"],
                "epoch_digest": first_proposal.payload["epoch_digest"],
                "block_hash": first_proposal.payload["block_hash"],
            },
            "view_generation": 1,
            "commit_batch_index": 0,
        },
    )
    commit_proved_events = {
        **events,
        actor: actor_events[1:],
        0: (authoritative_commit,),
    }
    validate_fault_causality(
        markers=markers,
        **{**arguments, "replica_events": commit_proved_events},
    )

    noncausal_commit = replace(
        authoritative_commit,
        monotonic_ns=markers[0].monotonic_ns,
    )
    with pytest.raises(FactorialValidationError, match="does not precede"):
        validate_fault_causality(
            markers=markers,
            **{
                **arguments,
                "replica_events": {
                    **commit_proved_events,
                    0: (noncausal_commit,),
                },
            },
        )

    non_authoritative_commit = replace(
        authoritative_commit,
        payload={**authoritative_commit.payload, "designated_observer": False},
    )
    with pytest.raises(FactorialValidationError, match="designated-observer"):
        validate_fault_causality(
            markers=markers,
            **{
                **arguments,
                "replica_events": {
                    **commit_proved_events,
                    0: (non_authoritative_commit,),
                },
            },
        )

    with pytest.raises(FactorialValidationError, match="invalid designated-observer"):
        validate_fault_causality(
            markers=markers,
            **{
                **arguments,
                "replica_events": {
                    **events,
                    actor: actor_events[1:],
                    1: (authoritative_commit,),
                },
            },
        )

    mismatched_observer = replace(
        first_proposal,
        payload={**first_proposal.payload, "observer_replica": cross_observer},
    )
    with pytest.raises(FactorialValidationError, match="observer differs"):
        validate_fault_causality(
            markers=markers,
            **{
                **arguments,
                "replica_events": {
                    **events,
                    actor: (mismatched_observer, *actor_events[1:]),
                },
            },
        )

    mismatched_proposal = replace(
        first_proposal,
        payload={**first_proposal.payload, "block_hash": "99" * 32},
    )
    with pytest.raises(FactorialValidationError, match="no matching native proposal"):
        validate_fault_causality(
            markers=markers,
            **{
                **arguments,
                "replica_events": {
                    **events,
                    actor: (mismatched_proposal, *actor_events[1:]),
                },
            },
        )

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


def _tiered_marker(
    *,
    actor: int,
    cohort: str,
    ordinal: int,
    block_ordinal: int,
    action: str,
) -> FaultMarker:
    return FaultMarker(
        source_replica=actor,
        line_number=block_ordinal,
        fault_mode="tiered_persistent_responsive_omission_v1",
        epoch_number=1,
        tree_id=4,
        epoch_digest="11" * 32,
        block_hash=f"{block_ordinal:064x}",
        window="tiered-window",
        window_start_ns=100,
        window_end_ns=10_000,
        actor=actor,
        action=action,
        monotonic_ns=200 + block_ordinal,
        raw_line_sha256=f"{actor * 100 + block_ordinal:064x}",
        cohort=cohort,
        hard_actor_count=3,
        responsive_degraded_actor_count=1,
        fault_threshold=4,
        max_omissions_per_proposal=4,
        responsive_omission_period=41,
        contribution_ordinal=ordinal,
    )


def _valid_tiered_markers() -> tuple[FaultMarker, ...]:
    markers = [
        _tiered_marker(
            actor=2,
            cohort="responsive_degraded",
            ordinal=ordinal,
            block_ordinal=ordinal,
            action="omit_aggregate" if ordinal % 41 == 0 else "forward",
        )
        for ordinal in range(1, 42)
    ]
    markers.extend(
        _tiered_marker(
            actor=actor,
            cohort="hard",
            ordinal=0,
            block_ordinal=41,
            action="omit_aggregate",
        )
        for actor in (9, 10, 12)
    )
    return tuple(markers)


def test_tiered_marker_parser_requires_exact_appended_audit_fields(
    tmp_path: Path,
) -> None:
    line = (
        "KAURI_FAULT fault=tiered_persistent_responsive_omission_v1 "
        "proposal_epoch=1 proposal_tree=4 proposal_epoch_digest={digest} "
        "proposal_block_hash={block} window=tiered-window "
        "window_start_monotonic_ns=100 window_end_monotonic_ns=10000 "
        "actor=2 action=forward monotonic_ns=201 "
        "cohort=responsive_degraded hard_actor_count=3 "
        "responsive_degraded_actor_count=1 fault_threshold=4 "
        "max_omissions_per_proposal=4 responsive_omission_period=41 "
        "contribution_ordinal=1\n"
    ).format(digest="11" * 32, block=f"{1:064x}")
    log = tmp_path / "replica.log"
    log.write_text(line, encoding="utf-8")

    marker = validation._fault_markers(tmp_path, {2: ("replica.log",)})[0]
    assert marker.cohort == "responsive_degraded"
    assert marker.responsive_omission_period == 41
    assert marker.contribution_ordinal == 1

    log.write_text(
        line.replace(" contribution_ordinal=1", ""),
        encoding="utf-8",
    )
    with pytest.raises(FactorialValidationError, match="malformed"):
        validation._fault_markers(tmp_path, {2: ("replica.log",)})


def test_tiered_marker_schedule_proves_exact_period_ordinals_and_f_bound() -> None:
    markers = _valid_tiered_markers()
    arguments = {
        "actor_ids": (9, 10, 12),
        "responsive_degraded_actor_ids": (2,),
        "fault_mode": "tiered_persistent_responsive_omission_v1",
        "fault_threshold": 4,
        "max_omissions_per_proposal": 4,
        "responsive_omission_period": 41,
    }
    validation._validate_fault_marker_schedule(markers, **arguments)

    with pytest.raises(FactorialValidationError, match="audit fields"):
        validation._validate_fault_marker_schedule(
            (replace(markers[0], responsive_omission_period=40), *markers[1:]),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="not contiguous"):
        validation._validate_fault_marker_schedule(
            (*markers[:10], replace(markers[10], contribution_ordinal=12), *markers[11:]),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="not contiguous"):
        validation._validate_fault_marker_schedule(
            (markers[1], markers[0], *markers[2:]),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="every-41st"):
        validation._validate_fault_marker_schedule(
            (*markers[:40], replace(markers[40], action="forward"), *markers[41:]),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="persistently omit"):
        validation._validate_fault_marker_schedule(
            (*markers[:-1], replace(markers[-1], action="forward")),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="omission bound"):
        validation._validate_fault_marker_schedule(
            (*markers, replace(markers[-1], line_number=999)),
            **arguments,
        )


def _v9_cross_commit_witness_fixture() -> dict[str, object]:
    digest = "11" * 32
    markers: list[FaultMarker] = []
    arm_markers: list[validation.ResponseAttemptArmMarker] = []
    trees: dict[int, Tree] = {}
    commit_times: dict[tuple[int, int, str, str], int] = {}
    proposal_commit_times: dict[
        int,
        dict[tuple[int, int, str, str], tuple[int, ...]],
    ] = {}
    timeout_index: dict[
        tuple[int, int, int, str, str], tuple[validation._EvidenceRecord, ...]
    ] = {}
    for offset, actor in enumerate((2, 5)):
        reporter = 1 + offset * 2
        tree_id = 4 + offset
        block_hash = f"{900 + offset:064x}"
        marker_ns = 2_000_000 + offset * 3_000_000
        start_ns = marker_ns - 100_000
        deadline_duration_us = 500
        absolute_deadline_ns = start_ns + deadline_duration_us * 1_000
        members = (
            reporter,
            actor,
            *(
                member
                for member in range(13)
                if member not in {reporter, actor}
            ),
        )
        trees[tree_id] = Tree(
            tree_id=tree_id,
            fanout=5,
            pipeline_stretch=2,
            members=members,
            wait_exempt=(9, 10, 12),
        )
        marker = replace(
            _tiered_marker(
                actor=actor,
                cohort="responsive_degraded",
                ordinal=41,
                block_ordinal=900 + offset,
                action="omit_aggregate",
            ),
            tree_id=tree_id,
            epoch_digest=digest,
            block_hash=block_hash,
            monotonic_ns=marker_ns,
        )
        markers.append(marker)
        proposal_key = (1, tree_id, digest, block_hash)
        commit_times[proposal_key] = marker_ns + 50_000
        proposal_commit_times.setdefault(reporter, {})[proposal_key] = (
            marker_ns + 100_000,
        )
        arm_markers.append(
            validation.ResponseAttemptArmMarker(
                source_replica=reporter,
                line_number=offset + 1,
                reporter_id=reporter,
                child_id=actor,
                epoch_number=1,
                tree_id=tree_id,
                epoch_digest=digest,
                block_hash=block_hash,
                expected_message_type="aggregate_relay",
                start_monotonic_ns=start_ns,
                deadline_duration_us=deadline_duration_us,
                absolute_deadline_ns=absolute_deadline_ns,
                raw_line_sha256=f"{offset + 1:064x}",
            )
        )
        timeout_index[(actor, *proposal_key)] = (
            validation._EvidenceRecord(
                ingestion_sequence=offset + 1,
                acceptance_monotonic_ns=absolute_deadline_ns + 50_000,
                observation_id=f"{offset + 1:064x}",
                reporter_id=reporter,
                target_id=actor,
                epoch_number=1,
                tree_id=tree_id,
                epoch_digest=digest,
                block_hash=block_hash,
                message_type="aggregate_relay",
                outcome="timeout",
                response_duration_us=0,
                deadline_duration_us=deadline_duration_us,
                reporter_monotonic_ns=absolute_deadline_ns,
                reporter_sequence=offset + 1,
                signer_set=(),
            ),
        )
    return {
        "markers": tuple(markers),
        "arm_markers": tuple(arm_markers),
        "responsive_degraded_actor_ids": (2, 5),
        "authoritative_commit_ns": commit_times,
        "proposal_commit_ns_by_replica": proposal_commit_times,
        "epoch1_trees": trees,
        "epoch1_timeout_index": timeout_index,
        "epoch2_selection_ns": 10_000_000,
    }


def _epoch1_causal_prefix_fixture() -> dict[str, object]:
    actor = 2
    epoch1_digest = "11" * 32
    tree = Tree(
        tree_id=0,
        fanout=2,
        pipeline_stretch=2,
        members=(0, 1, actor),
        wait_exempt=(),
    )
    markers = tuple(
        FaultMarker(
            source_replica=actor,
            line_number=index,
            fault_mode="tiered_persistent_responsive_omission_v1",
            epoch_number=1,
            tree_id=tree.tree_id,
            epoch_digest=epoch1_digest,
            block_hash=f"{height:064x}",
            window="causal-prefix-window",
            window_start_ns=100,
            window_end_ns=10_000,
            actor=actor,
            action="omit_direct_vote",
            monotonic_ns=marker_ns,
            raw_line_sha256=f"{index:064x}",
            cohort="responsive_degraded",
            hard_actor_count=0,
            responsive_degraded_actor_count=1,
            fault_threshold=1,
            max_omissions_per_proposal=1,
            responsive_omission_period=41,
            contribution_ordinal=41,
        )
        for index, (height, marker_ns) in enumerate(
            ((900, 2_000), (901, 4_000)),
            start=1,
        )
    )

    def timeout(
        marker: FaultMarker,
        *,
        ingestion_sequence: int,
        observation_id: str,
        reporter_ns: int,
    ) -> validation._EvidenceRecord:
        return validation._EvidenceRecord(
            ingestion_sequence=ingestion_sequence,
            acceptance_monotonic_ns=reporter_ns + 10,
            observation_id=observation_id,
            reporter_id=0,
            target_id=actor,
            epoch_number=marker.epoch_number,
            tree_id=marker.tree_id,
            epoch_digest=marker.epoch_digest,
            block_hash=marker.block_hash,
            message_type="direct_vote",
            outcome="timeout",
            response_duration_us=0,
            deadline_duration_us=100,
            reporter_monotonic_ns=reporter_ns,
            reporter_sequence=ingestion_sequence,
            signer_set=(),
        )

    accepted_epoch1 = (
        timeout(
            markers[0],
            ingestion_sequence=1,
            observation_id="a" * 64,
            reporter_ns=2_500,
        ),
        timeout(
            markers[1],
            ingestion_sequence=2,
            observation_id="b" * 64,
            reporter_ns=4_500,
        ),
        timeout(
            markers[1],
            ingestion_sequence=3,
            observation_id="c" * 64,
            reporter_ns=4_600,
        ),
    )
    replica_events = {
        0: tuple(
            _commit_event(
                index,
                marker.monotonic_ns + 100,
                899 + index,
                epoch_number=1,
                epoch_digest=epoch1_digest,
                tree_id=tree.tree_id,
            )
            for index, marker in enumerate(markers, start=1)
        )
    }
    return {
        "markers": markers,
        "replica_events": replica_events,
        "actor_ids": (),
        "fault_mode": "tiered_persistent_responsive_omission_v1",
        "max_omissions_per_proposal": 1,
        "initial_epoch_digest": "00" * 32,
        "initial_trees": (tree,),
        "window_id": "causal-prefix-window",
        "window_start_ns": 100,
        "window_end_ns": 10_000,
        "epoch1_command_ns": 900,
        "epoch1_activation_ns": 1_000,
        "epoch2_command_ns": 9_000,
        "epoch1_digest": epoch1_digest,
        "epoch1_trees": (tree,),
        "epoch2_digest": "22" * 32,
        "epoch2_trees": (tree,),
        "phase_windows": {
            "baseline": (100, 150, 1),
            "fault_evidence": (200, 800, 1),
            "epoch1_stable": (1_200, 8_000, 1),
            "epoch2_stable": (9_200, 9_800, 1),
        },
        "required_reporters": 1,
        "accepted_epoch1": accepted_epoch1,
        "epoch1_baseline_cutoff": 2,
        "epoch1_current_cutoff": 3,
        "responsive_degraded_actor_ids": (actor,),
    }


def _skip_tiered_schedule_shape_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        validation,
        "_validate_fault_marker_schedule",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        validation,
        "_validate_tiered_observed_marker_completeness",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        validation,
        "_validate_persistent_interior_proposals",
        lambda *a, **k: None,
    )


def test_epoch1_causality_uses_full_prefix_and_selection_uses_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)

    assert validate_fault_causality(
        **_epoch1_causal_prefix_fixture(),
        explicit_causal_linkage_windows=True,
    ) == 0


def test_v9_epoch1_causality_retains_its_frozen_suffix_interpretation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded omission has no exact outstanding raw timeout",
    ):
        validate_fault_causality(**_epoch1_causal_prefix_fixture())


def test_v9_cross_commit_receives_the_full_epoch1_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    captured_sequences: set[int] = set()

    def capture_cross_commit_prefix(**arguments: object) -> tuple[int, ...]:
        timeout_index = arguments["epoch1_timeout_index"]
        assert isinstance(timeout_index, dict)
        captured_sequences.update(
            record.ingestion_sequence
            for records in timeout_index.values()
            for record in records
        )
        return (2,)

    monkeypatch.setattr(
        validation,
        "_validate_v9_cross_commit_retention_witnesses",
        capture_cross_commit_prefix,
    )
    arguments = _epoch1_causal_prefix_fixture()

    assert validate_fault_causality(
        **{
            **arguments,
            "require_cross_commit_retention_witnesses": True,
            "explicit_causal_linkage_windows": True,
        }
    ) == 1
    assert captured_sequences == {1, 2, 3}


def test_epoch1_causality_rejects_missing_prebaseline_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _epoch1_causal_prefix_fixture()
    accepted = arguments["accepted_epoch1"]

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded omission has no exact outstanding raw timeout",
    ):
        validate_fault_causality(
            **{
                **arguments,
                "accepted_epoch1": accepted[1:],
                "explicit_causal_linkage_windows": True,
            }
        )


def test_epoch1_selection_linkage_rejects_missing_suffix_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _epoch1_causal_prefix_fixture()
    accepted = arguments["accepted_epoch1"]

    with pytest.raises(
        FactorialValidationError,
        match="performance-selection evidence",
    ):
        validate_fault_causality(
            **{
                **arguments,
                "accepted_epoch1": accepted[:2],
                "explicit_causal_linkage_windows": True,
            }
        )


def test_timeout_causality_excludes_only_exact_arms_maturing_after_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_tiered_schedule_shape_checks(monkeypatch)
    arguments = _epoch1_causal_prefix_fixture()
    original_markers = arguments["markers"]
    assert isinstance(original_markers, tuple)
    actor = original_markers[0].actor
    late_marker = replace(
        original_markers[-1],
        line_number=3,
        block_hash=f"{902:064x}",
        monotonic_ns=8_500,
        raw_line_sha256="d" * 64,
    )
    markers = (*original_markers, late_marker)
    arm_markers = tuple(
        validation.ResponseAttemptArmMarker(
            source_replica=0,
            line_number=index,
            reporter_id=0,
            child_id=actor,
            epoch_number=marker.epoch_number,
            tree_id=marker.tree_id,
            epoch_digest=marker.epoch_digest,
            block_hash=marker.block_hash,
            expected_message_type="direct_vote",
            start_monotonic_ns=(
                marker.monotonic_ns - 500
                if marker is not late_marker
                else 7_500
            ),
            deadline_duration_us=(
                1 if marker is not late_marker else 2
            ),
            absolute_deadline_ns=(
                marker.monotonic_ns + 500
                if marker is not late_marker
                else 9_500
            ),
            raw_line_sha256=f"{index + 10:064x}",
        )
        for index, marker in enumerate(markers, start=1)
    )
    replica_events = dict(arguments["replica_events"])
    replica_events[0] = (
        *replica_events[0],
        _commit_event(
            3,
            8_600,
            902,
            epoch_number=1,
            epoch_digest=late_marker.epoch_digest,
            tree_id=late_marker.tree_id,
        ),
    )
    explicit_arguments = {
        **arguments,
        "markers": markers,
        "arm_markers": arm_markers,
        "replica_events": replica_events,
        "accepted_epoch1": tuple(
            replace(record, deadline_duration_us=1)
            for record in arguments["accepted_epoch1"]
        ),
        "explicit_causal_linkage_windows": True,
        "explicit_phase_edge_eligibility": True,
        "epoch1_selection_ns": 800,
        "epoch2_selection_ns": 8_900,
    }

    assert validate_fault_causality(**explicit_arguments) == 0

    marker_by_hash = {marker.block_hash: marker for marker in original_markers}
    raced_arm_markers = tuple(
        replace(
            arm,
            start_monotonic_ns=marker_by_hash[arm.block_hash].monotonic_ns + 100,
            absolute_deadline_ns=(
                marker_by_hash[arm.block_hash].monotonic_ns + 1_100
            ),
        )
        if arm.block_hash in marker_by_hash
        else arm
        for arm in arm_markers
    )
    raced_timeouts = tuple(
        replace(
            record,
            reporter_monotonic_ns=(
                marker_by_hash[record.block_hash].monotonic_ns + 1_100
            ),
            acceptance_monotonic_ns=(
                marker_by_hash[record.block_hash].monotonic_ns + 1_110
            ),
        )
        for record in explicit_arguments["accepted_epoch1"]
    )
    assert validate_fault_causality(
        **{
            **explicit_arguments,
            "arm_markers": raced_arm_markers,
            "accepted_epoch1": raced_timeouts,
        }
    ) == 0

    equality_arm = replace(
        arm_markers[-1],
        start_monotonic_ns=7_900,
        deadline_duration_us=1,
        absolute_deadline_ns=8_900,
    )
    assert validate_fault_causality(
        **{
            **explicit_arguments,
            "arm_markers": (*arm_markers[:-1], equality_arm),
        }
    ) == 0

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded omission has no exact outstanding raw timeout",
    ):
        validate_fault_causality(
            **{
                **explicit_arguments,
                "arm_markers": (
                    *arm_markers[:-1],
                    replace(
                        equality_arm,
                        start_monotonic_ns=7_899,
                        absolute_deadline_ns=8_899,
                    ),
                ),
            }
        )

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded omission has no exact outstanding raw timeout",
    ):
        validate_fault_causality(
            **{
                **explicit_arguments,
                "explicit_phase_edge_eligibility": False,
            }
        )

    with pytest.raises(
        FactorialValidationError,
        match="exact parent response-attempt arm",
    ):
        validate_fault_causality(
            **{
                **explicit_arguments,
                "arm_markers": arm_markers[:-1],
            }
        )

    with pytest.raises(
        FactorialValidationError,
        match="responsive-degraded omission has no exact outstanding raw timeout",
    ):
        validate_fault_causality(
            **{
                **explicit_arguments,
                "accepted_epoch1": tuple(
                    replace(record, deadline_duration_us=2)
                    if record.block_hash == original_markers[1].block_hash
                    else record
                    for record in explicit_arguments["accepted_epoch1"]
                ),
            }
        )


def test_v9_proposal_commit_identity_binds_the_replica_event_stream() -> None:
    event = _commit_event(
        1,
        2_000_000,
        900,
        epoch_number=1,
        tree_id=4,
        replica_id=1,
    )
    assert validation._proposal_commit_identity(
        event,
        replica_id=1,
        require_exact_source_binding=True,
    ) == (1, 4, "11" * 32, f"{900:064x}")

    with pytest.raises(FactorialValidationError, match="source.*replica event stream"):
        validation._proposal_commit_identity(
            replace(event, source_id="replica-7"),
            replica_id=1,
            require_exact_source_binding=True,
        )


def test_v9_cross_commit_retention_witness_requires_every_degraded_actor() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    assert validation._validate_v9_cross_commit_retention_witnesses(
        **arguments
    ) == (2, 5)

    commit_times = dict(arguments["authoritative_commit_ns"])
    missing_key = next(
        key
        for key in commit_times
        if key[3] == f"{901:064x}"
    )
    commit_times.pop(missing_key)
    with pytest.raises(FactorialValidationError, match=r"cross-commit.*\[5\]"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{**arguments, "authoritative_commit_ns": commit_times}
        )


def test_v9_cross_commit_race_does_not_erase_strict_actor_witnesses() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    valid_marker = arguments["markers"][0]
    valid_arm = arguments["arm_markers"][0]
    raced_marker = replace(
        valid_marker,
        line_number=99,
        block_hash="ff" * 32,
        monotonic_ns=valid_marker.monotonic_ns + 1_000_000,
        raw_line_sha256="fe" * 32,
    )
    raced_arm = replace(
        valid_arm,
        line_number=99,
        block_hash=raced_marker.block_hash,
        start_monotonic_ns=raced_marker.monotonic_ns + 100_000,
        absolute_deadline_ns=raced_marker.monotonic_ns + 600_000,
        raw_line_sha256="fd" * 32,
    )
    proposal_key = raced_arm.proposal_key
    timeout_key = (raced_marker.actor, *proposal_key)
    timeout_index = dict(arguments["epoch1_timeout_index"])
    timeout_index[timeout_key] = (
        replace(
            next(iter(timeout_index.values()))[0],
            observation_id="fc" * 32,
            target_id=raced_marker.actor,
            epoch_number=raced_marker.epoch_number,
            tree_id=raced_marker.tree_id,
            epoch_digest=raced_marker.epoch_digest,
            block_hash=raced_marker.block_hash,
            reporter_id=raced_arm.reporter_id,
            message_type=raced_arm.expected_message_type,
            reporter_monotonic_ns=raced_arm.absolute_deadline_ns,
            acceptance_monotonic_ns=raced_arm.absolute_deadline_ns + 10,
        ),
    )
    authoritative = dict(arguments["authoritative_commit_ns"])
    authoritative[proposal_key] = raced_marker.monotonic_ns + 50_000
    proposal_commits = {
        replica_id: dict(commits)
        for replica_id, commits in arguments[
            "proposal_commit_ns_by_replica"
        ].items()
    }
    proposal_commits[raced_arm.reporter_id][proposal_key] = (
        raced_marker.monotonic_ns + 200_000,
    )

    assert validation._validate_v9_cross_commit_retention_witnesses(
        **{
            **arguments,
            "markers": (*arguments["markers"], raced_marker),
            "arm_markers": (*arguments["arm_markers"], raced_arm),
            "authoritative_commit_ns": authoritative,
            "proposal_commit_ns_by_replica": proposal_commits,
            "epoch1_timeout_index": timeout_index,
            "order_failures_are_nonwitness_candidates": True,
        }
    ) == (2, 5)

    with pytest.raises(FactorialValidationError, match="arm ordering"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "markers": (*arguments["markers"], raced_marker),
                "arm_markers": (*arguments["arm_markers"], raced_arm),
                "authoritative_commit_ns": authoritative,
                "proposal_commit_ns_by_replica": proposal_commits,
                "epoch1_timeout_index": timeout_index,
            }
        )


def test_v9_cross_commit_retention_rejects_missing_arm_marker() -> None:
    arguments = _v9_cross_commit_witness_fixture()

    with pytest.raises(FactorialValidationError, match=r"arm.*\[2\]"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "arm_markers": tuple(
                    arm
                    for arm in arguments["arm_markers"]
                    if arm.child_id != 2
                ),
            }
        )


@pytest.mark.parametrize(
    "binding",
    ("parent", "child", "proposal", "message_type"),
)
def test_v9_cross_commit_retention_binds_exact_arm_identity(binding: str) -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arms = list(arguments["arm_markers"])
    arm = arms[0]
    if binding == "parent":
        arms[0] = replace(
            arm,
            source_replica=arm.reporter_id + 1,
            reporter_id=arm.reporter_id + 1,
        )
    elif binding == "child":
        arms[0] = replace(arm, child_id=3)
    elif binding == "proposal":
        arms[0] = replace(arm, block_hash="ff" * 32)
    else:
        arms[0] = replace(arm, expected_message_type="direct_vote")

    with pytest.raises(FactorialValidationError, match="physical parent|missing"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{**arguments, "arm_markers": tuple(arms)}
        )


def test_v9_cross_commit_retention_rejects_post_commit_arm() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arms = list(arguments["arm_markers"])
    proposal_commit_times = arguments["proposal_commit_ns_by_replica"]
    timeout_index = dict(arguments["epoch1_timeout_index"])
    arm = arms[0]
    proposal_key = (
        arm.epoch_number,
        arm.tree_id,
        arm.epoch_digest,
        arm.block_hash,
    )
    post_commit_start = (
        proposal_commit_times[arm.reporter_id][proposal_key][0] + 1
    )
    post_commit_deadline = post_commit_start + arm.deadline_duration_us * 1_000
    arms[0] = replace(
        arm,
        start_monotonic_ns=post_commit_start,
        absolute_deadline_ns=post_commit_deadline,
    )
    timeout_key = (arm.child_id, *proposal_key)
    timeout_index[timeout_key] = (
        replace(
            timeout_index[timeout_key][0],
            reporter_monotonic_ns=post_commit_deadline,
            acceptance_monotonic_ns=post_commit_deadline + 1,
        ),
    )

    with pytest.raises(FactorialValidationError, match=r"cross-commit.*\[2\]"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "arm_markers": tuple(arms),
                "epoch1_timeout_index": timeout_index,
            }
        )


@pytest.mark.parametrize(
    "changed_field",
    ("absolute_deadline_ns", "deadline_duration_us"),
)
def test_v9_cross_commit_retention_rejects_changed_arm_deadline_or_duration(
    changed_field: str,
) -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arms = list(arguments["arm_markers"])
    arm = arms[0]
    if changed_field == "absolute_deadline_ns":
        arms[0] = replace(arm, absolute_deadline_ns=arm.absolute_deadline_ns + 1)
        expected_error = "absolute deadline"
    else:
        arms[0] = replace(
            arm,
            deadline_duration_us=arm.deadline_duration_us + 1,
            absolute_deadline_ns=(
                arm.start_monotonic_ns + (arm.deadline_duration_us + 1) * 1_000
            ),
        )
        expected_error = "duration"

    with pytest.raises(FactorialValidationError, match=expected_error):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{**arguments, "arm_markers": tuple(arms)}
        )


def test_v9_cross_commit_retention_rejects_cancel_and_rearm() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arms = list(arguments["arm_markers"])
    original = arms[0]
    proposal_commit_times = arguments["proposal_commit_ns_by_replica"]
    proposal_key = (
        original.epoch_number,
        original.tree_id,
        original.epoch_digest,
        original.block_hash,
    )
    rearm_start = (
        proposal_commit_times[original.reporter_id][proposal_key][0] + 1
    )
    arms.append(
        replace(
            original,
            line_number=99,
            start_monotonic_ns=rearm_start,
            absolute_deadline_ns=(
                rearm_start + original.deadline_duration_us * 1_000
            ),
            raw_line_sha256="99" * 32,
        )
    )

    with pytest.raises(FactorialValidationError, match="duplicate|rearm"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{**arguments, "arm_markers": tuple(arms)}
        )


@pytest.mark.parametrize("reporter_offset", (-1, 0))
def test_v9_cross_commit_retention_rejects_timeout_before_or_at_commit(
    reporter_offset: int,
) -> None:
    arguments = _v9_cross_commit_witness_fixture()
    proposal_commit_times = arguments["proposal_commit_ns_by_replica"]
    timeout_index = dict(arguments["epoch1_timeout_index"])
    timeout_key = next(key for key in timeout_index if key[0] == 2)
    timeout = timeout_index[timeout_key][0]
    proposal_key = timeout_key[1:]
    local_commit_ns = proposal_commit_times[timeout.reporter_id][proposal_key][0]
    timeout_index[timeout_key] = (
        replace(
            timeout,
            reporter_monotonic_ns=local_commit_ns + reporter_offset,
        ),
    )

    with pytest.raises(FactorialValidationError, match=r"cross-commit.*\[2\]"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{**arguments, "epoch1_timeout_index": timeout_index}
        )


def test_v9_cross_commit_uses_reporter_commit_not_earlier_authoritative_commit() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arm = arguments["arm_markers"][0]
    proposal_key = arm.proposal_key
    proposal_commit_times = {
        replica_id: dict(commits)
        for replica_id, commits in arguments[
            "proposal_commit_ns_by_replica"
        ].items()
    }
    proposal_commit_times[arm.reporter_id][proposal_key] = (
        arm.absolute_deadline_ns + 1,
    )

    with pytest.raises(FactorialValidationError, match="reporter-local commit"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "proposal_commit_ns_by_replica": proposal_commit_times,
            }
        )


def test_v9_cross_commit_keeps_later_authoritative_commit_as_separate_link() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arm = arguments["arm_markers"][0]
    proposal_key = arm.proposal_key
    timeout = arguments["epoch1_timeout_index"][(arm.child_id, *proposal_key)][0]
    authoritative = dict(arguments["authoritative_commit_ns"])
    authoritative[proposal_key] = timeout.acceptance_monotonic_ns + 1

    validation._validate_v9_cross_commit_retention_witnesses(
        **{**arguments, "authoritative_commit_ns": authoritative}
    )


@pytest.mark.parametrize("corruption", ("missing", "duplicate", "proposal", "reporter"))
def test_v9_cross_commit_rejects_nonexact_reporter_local_commit(
    corruption: str,
) -> None:
    arguments = _v9_cross_commit_witness_fixture()
    arm = arguments["arm_markers"][0]
    proposal_key = arm.proposal_key
    proposal_commit_times = {
        replica_id: dict(commits)
        for replica_id, commits in arguments[
            "proposal_commit_ns_by_replica"
        ].items()
    }
    original = proposal_commit_times[arm.reporter_id].pop(proposal_key)
    if corruption == "duplicate":
        proposal_commit_times[arm.reporter_id][proposal_key] = (
            original[0],
            original[0] + 1,
        )
    elif corruption == "proposal":
        proposal_commit_times[arm.reporter_id][
            (*proposal_key[:3], "ff" * 32)
        ] = original
    elif corruption == "reporter":
        proposal_commit_times.setdefault(arm.reporter_id + 1, {})[
            proposal_key
        ] = original

    with pytest.raises(
        FactorialValidationError,
        match="missing exact reporter-local|duplicated/ambiguous exact reporter-local",
    ):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "proposal_commit_ns_by_replica": proposal_commit_times,
            }
        )


def test_v9_cross_commit_retention_requires_an_internal_aggregate_witness() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    markers = tuple(
        replace(marker, action="omit_direct_vote")
        for marker in arguments["markers"]
    )
    trees: dict[int, Tree] = {}
    for marker in markers:
        tree = arguments["epoch1_trees"][marker.tree_id]
        trees[marker.tree_id] = replace(
            tree,
            members=(
                *(member for member in tree.members if member != marker.actor),
                marker.actor,
            ),
        )
    timeout_index = dict(arguments["epoch1_timeout_index"])
    proposal_commit_times: dict[
        int,
        dict[tuple[int, int, str, str], tuple[int, ...]],
    ] = {}
    arm_markers = {
        arm.child_id: arm for arm in arguments["arm_markers"]
    }
    for marker in markers:
        proposal_key = (
            marker.epoch_number,
            marker.tree_id,
            marker.epoch_digest,
            marker.block_hash,
        )
        timeout_key = (marker.actor, *proposal_key)
        tree = trees[marker.tree_id]
        position = tree.members.index(marker.actor)
        parent = tree.members[(position - 1) // tree.fanout]
        timeout_index[timeout_key] = (
            replace(
                timeout_index[timeout_key][0],
                reporter_id=parent,
                message_type="direct_vote",
            ),
        )
        arm_markers[marker.actor] = replace(
            arm_markers[marker.actor],
            source_replica=parent,
            reporter_id=parent,
            expected_message_type="direct_vote",
        )
        proposal_commit_times.setdefault(parent, {})[proposal_key] = (
            marker.monotonic_ns + 100_000,
        )

    with pytest.raises(
        FactorialValidationError,
        match=r"internal-role omit_aggregate",
    ):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "markers": markers,
                "epoch1_trees": trees,
                "epoch1_timeout_index": timeout_index,
                "arm_markers": tuple(arm_markers.values()),
                "proposal_commit_ns_by_replica": proposal_commit_times,
            }
        )


def test_primary_v9_cross_commit_requires_each_actor_to_be_internal() -> None:
    arguments = _v9_cross_commit_witness_fixture()
    leaf_only_actor = 5
    markers = tuple(
        replace(marker, action="omit_direct_vote")
        if marker.actor == leaf_only_actor
        else marker
        for marker in arguments["markers"]
    )
    marker = next(marker for marker in markers if marker.actor == leaf_only_actor)
    proposal_key = (
        marker.epoch_number,
        marker.tree_id,
        marker.epoch_digest,
        marker.block_hash,
    )
    trees = dict(arguments["epoch1_trees"])
    original_tree = trees[marker.tree_id]
    trees[marker.tree_id] = replace(
        original_tree,
        members=(
            *(member for member in original_tree.members if member != leaf_only_actor),
            leaf_only_actor,
        ),
    )
    position = trees[marker.tree_id].members.index(leaf_only_actor)
    parent = trees[marker.tree_id].members[
        (position - 1) // trees[marker.tree_id].fanout
    ]
    timeout_key = (leaf_only_actor, *proposal_key)
    timeout_index = dict(arguments["epoch1_timeout_index"])
    timeout_index[timeout_key] = (
        replace(
            timeout_index[timeout_key][0],
            reporter_id=parent,
            message_type="direct_vote",
        ),
    )
    arms = tuple(
        replace(
            arm,
            source_replica=parent,
            reporter_id=parent,
            expected_message_type="direct_vote",
        )
        if arm.child_id == leaf_only_actor
        else arm
        for arm in arguments["arm_markers"]
    )
    proposal_commit_times = {
        replica_id: dict(commits)
        for replica_id, commits in arguments[
            "proposal_commit_ns_by_replica"
        ].items()
    }
    original_arm = next(
        arm for arm in arguments["arm_markers"] if arm.child_id == leaf_only_actor
    )
    local_commit = proposal_commit_times[original_arm.reporter_id].pop(
        proposal_key
    )
    proposal_commit_times.setdefault(parent, {})[proposal_key] = local_commit

    with pytest.raises(FactorialValidationError, match=r"own.*\[5\]"):
        validation._validate_v9_cross_commit_retention_witnesses(
            **{
                **arguments,
                "markers": markers,
                "epoch1_trees": trees,
                "epoch1_timeout_index": timeout_index,
                "arm_markers": arms,
                "proposal_commit_ns_by_replica": proposal_commit_times,
                "require_each_degraded_actor_internal_witness": True,
            }
        )


def test_tiered_completeness_rejects_a_whole_missing_observed_context() -> None:
    digest = "11" * 32
    tree = Tree(
        tree_id=0,
        fanout=5,
        pipeline_stretch=2,
        members=tuple(range(13)),
        wait_exempt=(),
    )
    actors = (2, 9, 10, 12)
    first = (0, 0, digest, "01" * 32)
    missing = (0, 0, digest, "02" * 32)

    def marker(actor: int, cohort: str, action: str) -> FaultMarker:
        return replace(
            _tiered_marker(
                actor=actor,
                cohort=cohort,
                ordinal=1 if cohort == "responsive_degraded" else 0,
                block_ordinal=actor,
                action=action,
            ),
            epoch_number=first[0],
            tree_id=first[1],
            epoch_digest=first[2],
            block_hash=first[3],
        )

    markers = (
        marker(2, "responsive_degraded", "forward"),
        marker(9, "hard", "omit_direct_vote"),
        marker(10, "hard", "omit_direct_vote"),
        marker(12, "hard", "omit_direct_vote"),
    )
    observations = {
        first: {actor: (300,) for actor in actors},
        missing: {actor: (400,) for actor in actors},
    }
    with pytest.raises(FactorialValidationError, match="exact tiered actor marker set"):
        validation._validate_tiered_observed_marker_completeness(
            markers,
            fault_actor_ids=actors,
            proposal_observations=observations,
            phase_windows={"fault_evidence": (100, 700, 6)},
            phase_configurations=(
                ("fault_evidence", 0, digest, {0: tree}),
            ),
        )


def test_tiered_completeness_uses_root_activity_not_delayed_commits() -> None:
    digest = "11" * 32
    tree = Tree(
        tree_id=0,
        fanout=2,
        pipeline_stretch=2,
        members=(0, 1, 2),
        wait_exempt=(),
    )
    actor = 2
    represented = (0, 0, digest, "01" * 32)
    prewindow_delayed_commit = (0, 0, digest, "02" * 32)
    marker = replace(
        _tiered_marker(
            actor=actor,
            cohort="responsive_degraded",
            ordinal=1,
            block_ordinal=1,
            action="forward",
        ),
        epoch_number=represented[0],
        tree_id=represented[1],
        epoch_digest=represented[2],
        block_hash=represented[3],
    )
    delayed_commit_observations = {
        represented: {actor: (300,)},
        prewindow_delayed_commit: {actor: (400,)},
    }
    root_aggregation_observations = {
        represented: {0: (300,)},
        prewindow_delayed_commit: {0: (150,)},
    }
    arguments = {
        "fault_actor_ids": (actor,),
        "proposal_observations": delayed_commit_observations,
        "root_aggregation_observations": root_aggregation_observations,
        "phase_windows": {"fault_evidence": (100, 700, 6)},
        "phase_configurations": (
            ("fault_evidence", 0, digest, {0: tree}),
        ),
    }

    validation._validate_tiered_observed_marker_completeness(
        (marker,),
        **arguments,
    )

    with pytest.raises(
        FactorialValidationError,
        match="exact tiered actor marker set",
    ):
        validation._validate_tiered_observed_marker_completeness(
            (),
            **arguments,
        )


def _tiered_scores() -> tuple[ReplicaScore, ...]:
    fast = (0, 1, 3, 4, 5, 6, 7, 8, 11)
    return (
        *(
            ReplicaScore(
                replica_id=replica,
                classification="responsive",
                eligible=True,
                attempt_count=41,
                response_rate_ppm=1_000_000,
                timeout_rate_ppm=0,
                latency_percentile_us=10,
            )
            for replica in fast
        ),
        ReplicaScore(2, "responsive", True, 41, 975_610, 24_390, 20),
        *(
            ReplicaScore(
                replica_id=replica,
                classification="nonresponsive",
                eligible=False,
                attempt_count=41,
                response_rate_ppm=0,
                timeout_rate_ppm=1_000_000,
                latency_percentile_us=None,
            )
            for replica in (9, 10, 12)
        ),
    )


def test_tiered_ranking_requires_real_degradation_and_exact_top_q() -> None:
    scores = _tiered_scores()
    policy = {
        "minimum_attempts": 41,
        "minimum_response_rate_ppm": 950_000,
        "maximum_timeout_rate_ppm": 50_000,
    }
    fast = tuple(score.replica_id for score in scores[:9])
    arguments = {
        "hard_actor_ids": (9, 10, 12),
        "responsive_degraded_actor_ids": (2,),
        "fast_replica_ids": fast,
        "tree_count": 9,
        "expected_roots": fast,
        "policy": policy,
        "responsive_omission_period": 41,
    }
    assert validation._validate_tiered_performance_ranking(scores, **arguments) == 1

    with pytest.raises(FactorialValidationError, match="real nonzero degradation"):
        validation._validate_tiered_performance_ranking(
            (*scores[:9], replace(scores[9], classification="nonresponsive", eligible=False), *scores[10:]),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="real nonzero degradation"):
        validation._validate_tiered_performance_ranking(
            (*scores[:9], replace(scores[9], response_rate_ppm=1_000_000, timeout_rate_ppm=0), *scores[10:]),
            **arguments,
        )
    with pytest.raises(FactorialValidationError, match="top-Q"):
        validation._validate_tiered_performance_ranking(
            scores,
            **{**arguments, "expected_roots": (*fast[:-1], 2)},
        )


def _tiered_expected_slot() -> validation._ExpectedSlot:
    return validation._ExpectedSlot(
        slot_id="tiered-test",
        block_id="n13-f5-test",
        arm_code="P",
        ordinal=1,
        slot_nonce=0,
        block_index=1,
        blocks_in_cell=1,
        block_execution_ordinal=1,
        arm_execution_position=1,
        execution_ordinal=1,
        scientific_seed=41_719,
        replica_count=13,
        f=4,
        q=9,
        tree_count=9,
        initial_fanout=5,
        initial_depth=2,
        candidate_depths=((2, 3), (3, 2), (5, 2)),
        worst_candidate_depth=3,
        candidate_fanouts=(2, 3, 5),
        pipeline_stretch=2,
        placement_adaptation=True,
        shape_adaptation=False,
        actor_ids=(9, 10, 12),
        responsive_degraded_actor_ids=(2,),
        fast_replica_ids=(0, 1, 3, 4, 5, 6, 7, 8, 11),
        peer_base=1,
        client_base=2,
        manager_port=3,
    )


def _tiered_bundle(
    *,
    expected: validation._ExpectedSlot,
    epoch_number: int,
    roots: tuple[int, ...],
    epoch1: bool,
) -> validation.DecodedBundle:
    trees: list[Tree] = []
    membership = tuple(range(expected.replica_count))
    hard = set(expected.actor_ids)
    for tree_id, root in enumerate(roots):
        available_influential = [
            member
            for member in (
                membership if epoch1 else expected.fast_replica_ids
            )
            if member != root and member not in hard
        ]
        if epoch1 and tree_id == 0 and 2 != root:
            available_influential.remove(2)
            influential = [2, available_influential[0]]
        else:
            influential = available_influential[:2]
        prefix = (root, *influential)
        members = (*prefix, *(member for member in membership if member not in prefix))
        trees.append(
            Tree(
                tree_id=tree_id,
                fanout=5,
                pipeline_stretch=2,
                members=members,
                wait_exempt=expected.actor_ids,
            )
        )
    digest = f"{epoch_number + 1:064x}"
    return validation.DecodedBundle(
        command=validation.DecodedCommand(
            issuer_id=1,
            successor_epoch_number=epoch_number,
            predecessor_epoch_digest="11" * 32,
            successor_epoch_digest=digest,
            activation_delay_blocks=5,
            payload_digest="22" * 32,
            signature=b"test",
        ),
        epoch_number=epoch_number,
        epoch_digest=digest,
        previous_epoch_digest="11" * 32,
        membership_digest=validation._membership_digest(membership),
        generation_seed=validation._SNAPSHOT_SEED,
        policy_version=validation._PLACEMENT_POLICY_VERSION,
        evidence_snapshot_id="33" * 32,
        evidence_cutoff=1,
        trees=tuple(trees),
    )


def test_tiered_successor_structure_proves_e1_exposure_and_e2_hierarchy() -> None:
    expected = _tiered_expected_slot()
    epoch1_roots = tuple(range(expected.q))
    epoch1 = _tiered_bundle(
        expected=expected,
        epoch_number=1,
        roots=epoch1_roots,
        epoch1=True,
    )
    assert validation._validate_successor_trees(
        epoch1,
        expected=expected,
        actor_ids=expected.actor_ids,
        expected_roots=epoch1_roots,
        expected_fanout=5,
        cycle=0,
        intent="fault_containment",
    ) == (1, 1, 0, 0, 0)

    epoch2_roots = expected.fast_replica_ids
    epoch2 = _tiered_bundle(
        expected=expected,
        epoch_number=2,
        roots=epoch2_roots,
        epoch1=False,
    )
    proof = validation._validate_successor_trees(
        epoch2,
        expected=expected,
        actor_ids=expected.actor_ids,
        expected_roots=epoch2_roots,
        expected_fanout=5,
        cycle=1,
        intent="performance_optimization",
    )
    assert proof[:3] == (0, 0, expected.f * expected.q)
    assert proof[3] == proof[4] == 3 * expected.q

    first = epoch2.trees[0]
    degraded_position = first.members.index(2)
    tampered_members = list(first.members)
    tampered_members[1], tampered_members[degraded_position] = (
        tampered_members[degraded_position],
        tampered_members[1],
    )
    degraded_internal = replace(
        epoch2,
        trees=(replace(first, members=tuple(tampered_members)), *epoch2.trees[1:]),
    )
    with pytest.raises(FactorialValidationError, match="non-fast"):
        validation._validate_successor_trees(
            degraded_internal,
            expected=expected,
            actor_ids=expected.actor_ids,
            expected_roots=epoch2_roots,
            expected_fanout=5,
            cycle=1,
            intent="performance_optimization",
        )

    degraded_wait_exempt = replace(
        epoch2,
        trees=(
            replace(first, wait_exempt=(*expected.actor_ids, 2)),
            *epoch2.trees[1:],
        ),
    )
    with pytest.raises(FactorialValidationError, match="wait-exempt"):
        validation._validate_successor_trees(
            degraded_wait_exempt,
            expected=expected,
            actor_ids=expected.actor_ids,
            expected_roots=epoch2_roots,
            expected_fanout=5,
            cycle=1,
            intent="performance_optimization",
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


def test_native_epoch_activation_payload_is_flat_and_exact() -> None:
    payload = {
        "epoch_number": 1,
        "tree_id": 0,
        "epoch_digest": (
            "a1134b5b2bc72cd93fcfcc5a7e754c8b3e8be49ed9eabdd47e0cf045b0936e37"
        ),
        "activation_height": 2950,
    }

    assert (
        validation._activation_identity(payload, "epoch.activated.payload") == payload
    )

    stale_nested = {
        "configuration": {
            "epoch_number": payload["epoch_number"],
            "tree_id": payload["tree_id"],
            "epoch_digest": payload["epoch_digest"],
        },
        "activation_height": payload["activation_height"],
    }
    with pytest.raises(FactorialValidationError, match="invalid field set"):
        validation._activation_identity(
            stale_nested,
            "epoch.activated.payload",
        )


def test_native_activation_identity_is_bound_across_replicas_and_bundle() -> None:
    digest = "22" * 32
    command = validation.DecodedCommand(
        issuer_id=0,
        successor_epoch_number=1,
        predecessor_epoch_digest="11" * 32,
        successor_epoch_digest=digest,
        activation_delay_blocks=5,
        payload_digest="33" * 32,
        signature=b"signature",
    )
    bundle = validation.DecodedBundle(
        command=command,
        epoch_number=1,
        epoch_digest=digest,
        previous_epoch_digest="11" * 32,
        membership_digest="44" * 32,
        generation_seed=1,
        policy_version="test",
        evidence_snapshot_id="55" * 32,
        evidence_cutoff=1,
        trees=(
            Tree(0, 2, 2, (0, 1, 2), (1, 2)),
            Tree(1, 2, 2, (1, 2, 0), (2, 0)),
        ),
    )
    canonical: dict[int, dict[str, object]] = {}
    activation = {
        "epoch_number": 1,
        "tree_id": 0,
        "epoch_digest": digest,
        "activation_height": 2_950,
    }
    validation._record_activation_identity(
        activation,
        bundle=bundle,
        canonical_by_epoch=canonical,
    )

    with pytest.raises(FactorialValidationError, match="replicas disagree"):
        validation._record_activation_identity(
            {**activation, "tree_id": 1},
            bundle=bundle,
            canonical_by_epoch=canonical,
        )

    with pytest.raises(FactorialValidationError, match="tree absent"):
        validation._record_activation_identity(
            {**activation, "tree_id": 7},
            bundle=bundle,
            canonical_by_epoch={},
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
                "epoch_number": 1,
                "tree_id": 0,
                "epoch_digest": "22" * 32,
                "activation_height": 14,
            }
        elif name == "epoch2_activation":
            payload = {
                "epoch_number": 2,
                "tree_id": 0,
                "epoch_digest": "33" * 32,
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

    epoch1_activation_row = document["cutoffs"][3]
    epoch1_activation_key = (
        epoch1_activation_row["source_path"],
        epoch1_activation_row["source_sequence"],
    )
    epoch1_activation = events[epoch1_activation_key]
    stale_nested_activation = replace(
        epoch1_activation,
        payload={
            "configuration": {
                "epoch_number": epoch1_activation.payload["epoch_number"],
                "tree_id": epoch1_activation.payload["tree_id"],
                "epoch_digest": epoch1_activation.payload["epoch_digest"],
            },
            "activation_height": epoch1_activation.payload["activation_height"],
        },
    )
    with pytest.raises(FactorialValidationError, match="invalid field set"):
        validation._validate_phase_cutoffs(
            document,
            **{
                **arguments,
                "events_by_ref": {
                    **events,
                    epoch1_activation_key: stale_nested_activation,
                },
            },
        )

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
        result_root=f"{manifest.results_root}-smoke",
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


def test_v9_receipt_rejects_an_exact_legacy_manifest_plan_pair(
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


def _synthetic_breakthrough_results(
    block_effects: tuple[float, ...],
    *,
    secondary_block_effects: tuple[float, ...] | None = None,
    zero_mean: tuple[int, str, str] | None = None,
    p_changed_blocks: int = 5,
    ps_changed_blocks: int = 5,
    secondary_p_changed_blocks: int = 5,
    secondary_ps_changed_blocks: int = 5,
) -> dict[str, validation.SlotValidationResult]:
    assert len(block_effects) == 5
    if secondary_block_effects is None:
        secondary_block_effects = (0.0,) * 5
    assert len(secondary_block_effects) == 5
    manifest = load_frozen_manifest(MANIFEST_PATH)
    expected_by_pair = {
        (slot.block_id, slot.arm_code): slot
        for slot in validation._expected_slots(manifest)
    }
    results: dict[str, validation.SlotValidationResult] = {}
    for fanout in (5, 2):
        for block_index, effect in enumerate(block_effects, 1):
            cell_effect = (
                effect
                if fanout == 5
                else secondary_block_effects[block_index - 1]
            )
            block_id = f"n31-f{fanout}-b{block_index:02d}"
            for arm in ("00", "P", "S", "PS"):
                baseline = 100.0
                fault_evidence = 50.0
                epoch1 = 100.0
                epoch2 = (
                    100.0 * math.exp(cell_effect)
                    if arm in ("P", "PS")
                    else 100.0
                )
                if zero_mean == (block_index, arm, "baseline"):
                    baseline = 0.0
                if zero_mean == (block_index, arm, "fault_evidence"):
                    fault_evidence = 0.0
                if zero_mean == (block_index, arm, "epoch1_stable"):
                    epoch1 = 0.0
                if zero_mean == (block_index, arm, "epoch2_stable"):
                    epoch2 = 0.0
                means = {
                    "baseline": baseline,
                    "fault_evidence": fault_evidence,
                    "epoch1_stable": epoch1,
                    "epoch2_stable": epoch2,
                }
                metrics = tuple(
                    validation.PhaseMetric(
                        phase=phase,
                        transactions=int(mean * 30),
                        mean_tps=mean,
                        buckets_tps=(mean,) * 6,
                    )
                    for phase, mean in means.items()
                )
                slot_id = f"synthetic-{block_id}-{arm}"
                expected = expected_by_pair[(block_id, arm)]
                epoch1_roots = tuple(range(expected.q))
                changed_limit = (
                    (p_changed_blocks if fanout == 5 else secondary_p_changed_blocks)
                    if arm == "P"
                    else (
                        ps_changed_blocks
                        if fanout == 5
                        else secondary_ps_changed_blocks
                    )
                    if arm == "PS"
                    else 0
                )
                placement_changed = block_index <= changed_limit
                epoch2_roots = (
                    expected.fast_replica_ids
                    if placement_changed
                    else epoch1_roots
                )
                promoted = tuple(sorted(set(epoch2_roots) - set(epoch1_roots)))
                demoted = tuple(sorted(set(epoch1_roots) - set(epoch2_roots)))
                hierarchy_required = arm in {"P", "PS"}
                results[slot_id] = validation.SlotValidationResult(
                    slot_id=slot_id,
                    outcome="PASS",
                    reason=None,
                    block_id=block_id,
                    arm_code=arm,
                    replica_count=31,
                    initial_fanout=fanout,
                    metrics=metrics,
                    integrity_valid=True,
                    epoch1_roots=epoch1_roots,
                    epoch2_roots=epoch2_roots,
                    promoted_replica_ids=promoted,
                    demoted_replica_ids=demoted,
                    placement_changed=placement_changed,
                    hard_actor_ids=expected.actor_ids,
                    responsive_degraded_actor_ids=(
                        expected.responsive_degraded_actor_ids
                    ),
                    fast_replica_ids=expected.fast_replica_ids,
                    degraded_rank_proof_count=(
                        len(expected.responsive_degraded_actor_ids)
                        if hierarchy_required
                        else 0
                    ),
                    epoch1_degraded_root_proof_count=(
                        len(expected.responsive_degraded_actor_ids)
                        if hierarchy_required
                        else 0
                    ),
                    epoch1_degraded_internal_proof_count=(
                        len(expected.responsive_degraded_actor_ids)
                        if hierarchy_required
                        else 0
                    ),
                    epoch1_degraded_internal_cross_commit_witness_count=(
                        len(expected.responsive_degraded_actor_ids)
                        if fanout == 5 and hierarchy_required
                        else 0
                    ),
                    epoch2_constrained_leaf_proof_count=(
                        expected.f * expected.q if hierarchy_required else 0
                    ),
                    epoch2_fast_root_internal_position_proof_count=(
                        expected.q if hierarchy_required else 0
                    ),
                    epoch2_fast_root_internal_position_required_count=(
                        expected.q if hierarchy_required else 0
                    ),
                    full_hierarchy_gate_passed=(
                        True if hierarchy_required else None
                    ),
                )
    return results


def _with_placebo_component_effects(
    results: dict[str, validation.SlotValidationResult],
    *,
    p_effects: tuple[float, ...],
    ps_effects: tuple[float, ...],
    initial_fanout: int = 5,
) -> dict[str, validation.SlotValidationResult]:
    assert len(p_effects) == len(ps_effects) == 5
    updated = dict(results)
    by_arm = {"P": p_effects, "PS": ps_effects}
    for slot_id, result in results.items():
        if (
            result.initial_fanout != initial_fanout
            or result.arm_code not in by_arm
        ):
            continue
        block_index = int(result.block_id.rsplit("b", 1)[1]) - 1
        fault_mean = 50.0 * math.exp(by_arm[result.arm_code][block_index])
        updated[slot_id] = replace(
            result,
            metrics=tuple(
                replace(metric, mean_tps=fault_mean)
                if metric.phase == "fault_evidence"
                else metric
                for metric in result.metrics
            ),
        )
    return updated


def test_primary_throughput_estimate_is_the_exact_matched_log_ratio() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    expected_effects = (0.1, 0.2, 0.3, 0.4, 0.5)
    effects = validation._headline_effects(
        manifest,
        _synthetic_breakthrough_results(expected_effects),
    )

    assert effects is not None
    estimate = effects.primary_throughput_log_ratio
    assert estimate is not None
    assert estimate.block_effects_log_ratio == pytest.approx(expected_effects)
    assert estimate.mean_log_ratio == pytest.approx(0.3)
    assert estimate.sample_standard_deviation_log_ratio == pytest.approx(2.5**0.5 / 10)
    assert estimate.ci95_lower_log_ratio == pytest.approx(0.10367568385)
    assert estimate.ci95_upper_log_ratio == pytest.approx(0.49632431615)
    assert estimate.geometric_mean_ratio == pytest.approx(math.exp(0.3))
    assert estimate.geometric_mean_percent_change == pytest.approx(
        (math.exp(0.3) - 1.0) * 100.0
    )
    assert estimate.ci95_lower_ratio == pytest.approx(
        math.exp(estimate.ci95_lower_log_ratio)
    )
    assert estimate.ci95_upper_ratio == pytest.approx(
        math.exp(estimate.ci95_upper_log_ratio)
    )
    for placebo in (
        effects.pre_epoch1_placebo_p_log_ratio,
        effects.pre_epoch1_placebo_ps_log_ratio,
    ):
        assert placebo is not None
        assert placebo.block_effects_log_ratio == pytest.approx((0.0,) * 5)
        assert placebo.mean_log_ratio == pytest.approx(0.0)
        assert placebo.sample_standard_deviation_log_ratio == pytest.approx(0.0)
        assert placebo.ci90_lower_log_ratio == pytest.approx(0.0)
        assert placebo.ci90_upper_log_ratio == pytest.approx(0.0)
        assert placebo.equivalence_margin_log_ratio == pytest.approx(math.log(1.1))
        assert placebo.equivalence_supported is True


def test_secondary_f2_status_depends_on_primary_without_changing_primary() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)

    both = _synthetic_breakthrough_results(
        (0.2,) * 5,
        secondary_block_effects=(0.2,) * 5,
    )
    both_effects = validation._headline_effects(manifest, both)
    assert both_effects is not None
    primary_both = validation._breakthrough_verdict(
        manifest,
        both,
        campaign_outcome="PASS",
        effects=both_effects,
    )
    secondary_both = validation._secondary_placement_verdict(
        manifest,
        both,
        primary=primary_both,
        effects=both_effects,
    )
    assert primary_both.status == "SUPPORTED"
    assert secondary_both is not None
    assert secondary_both.status == "SUPPORTED"
    assert secondary_both.structural_validated_slot_count == 10
    assert secondary_both.placement_changed_p_block_count == 5
    assert secondary_both.placement_changed_ps_block_count == 5
    assert secondary_both.throughput_positive_block_count == 5
    assert secondary_both.placebo_equivalence_rule_passed is True

    only_f5 = _synthetic_breakthrough_results((0.2,) * 5)
    only_f5_effects = validation._headline_effects(manifest, only_f5)
    assert only_f5_effects is not None
    primary_only_f5 = validation._breakthrough_verdict(
        manifest,
        only_f5,
        campaign_outcome="PASS",
        effects=only_f5_effects,
    )
    secondary_only_f5 = validation._secondary_placement_verdict(
        manifest,
        only_f5,
        primary=primary_only_f5,
        effects=only_f5_effects,
    )
    assert primary_only_f5.status == "SUPPORTED"
    assert secondary_only_f5 is not None
    assert secondary_only_f5.status == "NOT_SUPPORTED"
    assert secondary_only_f5.throughput_rule_passed is False

    only_f2 = _synthetic_breakthrough_results(
        (-0.1,) * 5,
        secondary_block_effects=(0.2,) * 5,
    )
    only_f2_effects = validation._headline_effects(manifest, only_f2)
    assert only_f2_effects is not None
    primary_only_f2 = validation._breakthrough_verdict(
        manifest,
        only_f2,
        campaign_outcome="PASS",
        effects=only_f2_effects,
    )
    secondary_only_f2 = validation._secondary_placement_verdict(
        manifest,
        only_f2,
        primary=primary_only_f2,
        effects=only_f2_effects,
    )
    assert primary_only_f2.status == "NOT_SUPPORTED"
    assert secondary_only_f2 is not None
    assert secondary_only_f2.status == "DESCRIPTIVE_ONLY"
    assert secondary_only_f2.throughput_rule_passed is True


def test_secondary_f2_requires_its_own_structural_and_placebo_gates() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _synthetic_breakthrough_results(
        (0.2,) * 5,
        secondary_block_effects=(0.2,) * 5,
    )
    structurally_invalid = dict(results)
    slot_id = "synthetic-n31-f2-b05-P"
    structurally_invalid[slot_id] = replace(
        structurally_invalid[slot_id],
        full_hierarchy_gate_passed=False,
    )
    structural_effects = validation._headline_effects(
        manifest,
        structurally_invalid,
    )
    assert structural_effects is not None
    primary = validation._breakthrough_verdict(
        manifest,
        structurally_invalid,
        campaign_outcome="PASS",
        effects=structural_effects,
    )
    structural_secondary = validation._secondary_placement_verdict(
        manifest,
        structurally_invalid,
        primary=primary,
        effects=structural_effects,
    )
    assert primary.status == "SUPPORTED"
    assert structural_secondary is not None
    assert structural_secondary.status == "NOT_SUPPORTED"
    assert structural_secondary.structural_validated_slot_count == 9

    placebo_invalid = _with_placebo_component_effects(
        results,
        p_effects=(math.log(1.2),) * 5,
        ps_effects=(0.0,) * 5,
        initial_fanout=2,
    )
    placebo_effects = validation._headline_effects(manifest, placebo_invalid)
    assert placebo_effects is not None
    primary = validation._breakthrough_verdict(
        manifest,
        placebo_invalid,
        campaign_outcome="PASS",
        effects=placebo_effects,
    )
    placebo_secondary = validation._secondary_placement_verdict(
        manifest,
        placebo_invalid,
        primary=primary,
        effects=placebo_effects,
    )
    assert primary.status == "SUPPORTED"
    assert placebo_secondary is not None
    assert placebo_secondary.status == "NOT_SUPPORTED"
    assert placebo_secondary.placebo_p_equivalence_rule_passed is False
    assert placebo_secondary.placebo_ps_equivalence_rule_passed is True


def test_v8_breakthrough_hierarchy_does_not_require_v9_internal_witnesses() -> None:
    manifest = load_frozen_manifest(V8_MANIFEST_PATH)
    results = {
        slot_id: replace(
            result,
            epoch1_degraded_internal_cross_commit_witness_count=0,
        )
        for slot_id, result in _synthetic_breakthrough_results((0.2,) * 5).items()
    }

    summary = validation._breakthrough_hierarchy_summary(manifest, results)

    assert summary["validated_slot_count"] == 10
    assert summary["epoch1_degraded_internal_cross_commit_required_count"] == 0
    assert summary["full_hierarchy_gate_passed"] is True


def test_breakthrough_requires_lower_log_bound_and_four_positive_blocks() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    four_positive = _synthetic_breakthrough_results(
        (0.2, 0.2, 0.2, 0.2, -0.001),
        p_changed_blocks=5,
        ps_changed_blocks=5,
    )
    effects = validation._headline_effects(manifest, four_positive)
    assert effects is not None
    estimate = effects.primary_throughput_log_ratio
    assert estimate is not None
    assert estimate.ci95_lower_log_ratio > 0
    assert estimate.positive_block_count == 4
    assert estimate.directional_claim_supported is True

    supported = validation._breakthrough_verdict(
        manifest,
        four_positive,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert supported.status == "SUPPORTED"
    assert supported.structural_required_slot_count == 10
    assert supported.structural_validated_slot_count == 10
    assert supported.structural_gate_passed is True
    assert (
        supported.epoch1_degraded_internal_cross_commit_required_count == 70
    )
    assert (
        supported.epoch1_degraded_internal_cross_commit_validated_count == 70
    )
    assert supported.placement_changed_p_block_count == 5
    assert supported.placement_changed_ps_block_count == 5
    assert supported.realized_placement_gate_passed is True
    assert supported.throughput_rule_passed is True
    assert supported.placebo_p_estimate_available is True
    assert supported.placebo_p_ci90_lower_log_ratio == pytest.approx(0.0)
    assert supported.placebo_p_ci90_upper_log_ratio == pytest.approx(0.0)
    assert supported.placebo_p_equivalence_rule_passed is True
    assert supported.placebo_ps_estimate_available is True
    assert supported.placebo_ps_ci90_lower_log_ratio == pytest.approx(0.0)
    assert supported.placebo_ps_ci90_upper_log_ratio == pytest.approx(0.0)
    assert supported.placebo_ps_equivalence_rule_passed is True
    assert supported.placebo_equivalence_rule_passed is True
    assert supported.failed_requirements == ()

    missing_structural_slot = dict(four_positive)
    missing_structural_slot.pop("synthetic-n31-f5-b05-P")
    structural_failure = validation._breakthrough_verdict(
        manifest,
        missing_structural_slot,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert structural_failure.status == "NOT_SUPPORTED"
    assert structural_failure.structural_validated_slot_count == 9
    assert (
        structural_failure.epoch1_degraded_internal_cross_commit_validated_count
        == 63
    )
    assert structural_failure.structural_gate_passed is False
    assert structural_failure.realized_placement_gate_passed is False
    assert structural_failure.failed_requirements == (
        "structural gate validated 9 of 10 required slots",
        "P realized placement changed in 4 of 5 blocks; requires at least 5",
    )

    three_positive = _synthetic_breakthrough_results(
        (0.2, 0.2, 0.2, -0.001, -0.001)
    )
    unsupported_effects = validation._headline_effects(manifest, three_positive)
    assert unsupported_effects is not None
    unsupported = validation._breakthrough_verdict(
        manifest,
        three_positive,
        campaign_outcome="PASS",
        effects=unsupported_effects,
    )
    assert unsupported.status == "NOT_SUPPORTED"
    assert unsupported.throughput_positive_block_count == 3
    assert unsupported.throughput_rule_passed is False
    assert unsupported.failed_requirements


def test_relative_gain_cannot_hide_absolute_p_and_ps_throughput_decline() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _synthetic_breakthrough_results((0.2,) * 5)
    for slot_id, result in tuple(results.items()):
        if result.initial_fanout != 5:
            continue
        phase_means = {
            "baseline": 100.0,
            "fault_evidence": 50.0,
            "epoch1_stable": 100.0,
            "epoch2_stable": (
                90.0 if result.arm_code in {"P", "PS"} else 50.0
            ),
        }
        results[slot_id] = replace(
            result,
            metrics=tuple(
                replace(metric, mean_tps=phase_means[metric.phase])
                for metric in result.metrics
            ),
        )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    assert effects.primary_throughput_log_ratio is not None
    assert effects.primary_throughput_log_ratio.directional_claim_supported is True
    assert effects.optimization_gain.mean_tps == pytest.approx(-10.0)

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.throughput_rule_passed is True
    assert verdict.optimization_gain_rule_passed is False
    assert verdict.p_absolute_optimization_positive_block_count == 0
    assert verdict.ps_absolute_optimization_positive_block_count == 0
    assert verdict.per_arm_absolute_optimization_rule_passed is False
    assert verdict.absolute_sequence_gate_passed is False
    assert any(
        "absolute optimization-gain" in failure
        for failure in verdict.failed_requirements
    )
    assert validation._campaign_figure_eligible("PASS", effects) is True


def test_pre_epoch1_arm_specific_trajectory_blocks_breakthrough() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    arm_pretrend = (math.log(1.2),) * 5
    results = _with_placebo_component_effects(
        _synthetic_breakthrough_results((0.2,) * 5),
        p_effects=arm_pretrend,
        ps_effects=arm_pretrend,
    )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    assert effects.primary_throughput_log_ratio is not None
    assert effects.primary_throughput_log_ratio.directional_claim_supported
    for placebo in (
        effects.pre_epoch1_placebo_p_log_ratio,
        effects.pre_epoch1_placebo_ps_log_ratio,
    ):
        assert placebo is not None
        assert placebo.mean_log_ratio == pytest.approx(math.log(1.2))
        assert placebo.ci90_lower_log_ratio == pytest.approx(math.log(1.2))
        assert placebo.ci90_upper_log_ratio == pytest.approx(math.log(1.2))
        assert placebo.equivalence_supported is False

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.throughput_rule_passed is True
    assert verdict.absolute_sequence_gate_passed is True
    assert verdict.placebo_p_estimate_available is True
    assert verdict.placebo_p_equivalence_rule_passed is False
    assert verdict.placebo_ps_estimate_available is True
    assert verdict.placebo_ps_equivalence_rule_passed is False
    assert verdict.placebo_equivalence_rule_passed is False
    assert verdict.failed_requirements == (
        "P/00 pre-Epoch1 placebo 90% log-ratio interval is not strictly "
        "within the prespecified equivalence margin",
        "PS/S pre-Epoch1 placebo 90% log-ratio interval is not strictly "
        "within the prespecified equivalence margin",
    )


def test_both_placebo_components_accept_small_nonzero_matched_variation() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    small = (-0.02, -0.01, 0.0, 0.01, 0.02)
    results = _with_placebo_component_effects(
        _synthetic_breakthrough_results((0.2,) * 5),
        p_effects=small,
        ps_effects=tuple(reversed(small)),
    )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    for placebo in (
        effects.pre_epoch1_placebo_p_log_ratio,
        effects.pre_epoch1_placebo_ps_log_ratio,
    ):
        assert placebo is not None
        assert placebo.sample_standard_deviation_log_ratio > 0
        assert placebo.ci90_lower_log_ratio > -math.log(1.1)
        assert placebo.ci90_upper_log_ratio < math.log(1.1)
        assert placebo.equivalence_supported is True

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "SUPPORTED"
    assert verdict.placebo_equivalence_rule_passed is True


def test_opposite_placebo_components_cannot_cancel_into_support() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _with_placebo_component_effects(
        _synthetic_breakthrough_results((0.2,) * 5),
        p_effects=(math.log(2.0),) * 5,
        ps_effects=(math.log(0.5),) * 5,
    )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    assert effects.fault_drop.ci95_lower_tps == pytest.approx(37.5)
    assert effects.pre_epoch1_placebo_p_log_ratio is not None
    assert effects.pre_epoch1_placebo_ps_log_ratio is not None
    assert effects.pre_epoch1_placebo_p_log_ratio.mean_log_ratio == pytest.approx(
        math.log(2.0)
    )
    assert effects.pre_epoch1_placebo_ps_log_ratio.mean_log_ratio == pytest.approx(
        math.log(0.5)
    )

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.throughput_rule_passed is True
    assert verdict.absolute_sequence_gate_passed is True
    assert verdict.placebo_p_equivalence_rule_passed is False
    assert verdict.placebo_ps_equivalence_rule_passed is False
    assert verdict.placebo_equivalence_rule_passed is False


def test_placebo_rejects_ci_crossing_margin_despite_zero_mean() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _with_placebo_component_effects(
        _synthetic_breakthrough_results((0.2,) * 5),
        p_effects=(-0.2, 0.0, 0.0, 0.0, 0.2),
        ps_effects=(0.0,) * 5,
    )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    placebo_p = effects.pre_epoch1_placebo_p_log_ratio
    assert placebo_p is not None
    assert placebo_p.mean_log_ratio == pytest.approx(0.0)
    assert placebo_p.ci90_lower_log_ratio < -math.log(1.1)
    assert placebo_p.ci90_upper_log_ratio > math.log(1.1)
    assert placebo_p.equivalence_supported is False

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.placebo_p_equivalence_rule_passed is False
    assert verdict.placebo_ps_equivalence_rule_passed is True


@pytest.mark.parametrize("boundary", (math.log(1.1), -math.log(1.1)))
def test_placebo_rejects_exact_equivalence_margin(boundary: float) -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _with_placebo_component_effects(
        _synthetic_breakthrough_results((0.2,) * 5),
        p_effects=(boundary,) * 5,
        ps_effects=(0.0,) * 5,
    )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    placebo_p = effects.pre_epoch1_placebo_p_log_ratio
    assert placebo_p is not None
    assert placebo_p.ci90_lower_log_ratio == pytest.approx(boundary)
    assert placebo_p.ci90_upper_log_ratio == pytest.approx(boundary)
    assert placebo_p.equivalence_supported is False

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.placebo_p_equivalence_rule_passed is False
    assert verdict.placebo_equivalence_rule_passed is False


def test_nonpositive_placebo_mean_is_unavailable_and_not_supported() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _synthetic_breakthrough_results(
        (0.2,) * 5,
        zero_mean=(1, "P", "fault_evidence"),
    )

    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    assert effects.pre_epoch1_placebo_p_log_ratio is None
    assert effects.pre_epoch1_placebo_ps_log_ratio is not None

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.placebo_p_estimate_available is False
    assert verdict.placebo_p_equivalence_rule_passed is None
    assert verdict.placebo_ps_estimate_available is True
    assert verdict.placebo_ps_equivalence_rule_passed is True
    assert verdict.placebo_equivalence_rule_passed is False
    assert any(
        "P/00 pre-Epoch1 placebo requires strictly positive" in failure
        for failure in verdict.failed_requirements
    )


def test_breakthrough_requires_realized_root_changes_in_each_adaptive_arm() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    throughput_passes = (0.2,) * 5

    unchanged = _synthetic_breakthrough_results(
        throughput_passes,
        p_changed_blocks=0,
        ps_changed_blocks=0,
    )
    unchanged_effects = validation._headline_effects(manifest, unchanged)
    assert unchanged_effects is not None
    assert unchanged_effects.primary_throughput_log_ratio is not None
    assert unchanged_effects.primary_throughput_log_ratio.directional_claim_supported
    unchanged_verdict = validation._breakthrough_verdict(
        manifest,
        unchanged,
        campaign_outcome="PASS",
        effects=unchanged_effects,
    )
    assert unchanged_verdict.status == "NOT_SUPPORTED"
    assert unchanged_verdict.placement_changed_p_block_count == 0
    assert unchanged_verdict.placement_changed_ps_block_count == 0
    assert unchanged_verdict.realized_placement_gate_passed is False
    assert validation._campaign_figure_eligible("PASS", unchanged_effects) is True

    one_arm_short = _synthetic_breakthrough_results(
        throughput_passes,
        p_changed_blocks=4,
        ps_changed_blocks=5,
    )
    one_arm_short_effects = validation._headline_effects(manifest, one_arm_short)
    assert one_arm_short_effects is not None
    one_arm_short_verdict = validation._breakthrough_verdict(
        manifest,
        one_arm_short,
        campaign_outcome="PASS",
        effects=one_arm_short_effects,
    )
    assert one_arm_short_verdict.status == "NOT_SUPPORTED"
    assert one_arm_short_verdict.placement_changed_p_block_count == 4
    assert one_arm_short_verdict.placement_changed_ps_block_count == 5
    assert one_arm_short_verdict.failed_requirements == (
        "P realized placement changed in 4 of 5 blocks; requires at least 5",
    )


def test_breakthrough_verdict_is_not_evaluable_without_complete_valid_data() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _synthetic_breakthrough_results((0.2,) * 5)
    effects = validation._headline_effects(manifest, results)
    assert effects is not None

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="INCOMPLETE",
        effects=effects,
    )
    assert verdict.status == "NOT_EVALUABLE"
    assert verdict.structural_validated_slot_count == 10
    assert verdict.throughput_rule_passed is None
    assert verdict.failed_requirements == (
        "campaign lacks complete valid data for the prespecified scope",
    )


def test_zero_mean_rejects_breakthrough_without_invalidating_campaign_evidence() -> None:
    manifest = load_frozen_manifest(MANIFEST_PATH)
    results = _synthetic_breakthrough_results(
        (0.2,) * 5,
        zero_mean=(1, "P", "epoch1_stable"),
    )
    effects = validation._headline_effects(manifest, results)
    assert effects is not None
    assert effects.primary_throughput_log_ratio is None

    verdict = validation._breakthrough_verdict(
        manifest,
        results,
        campaign_outcome="PASS",
        effects=effects,
    )
    assert verdict.status == "NOT_SUPPORTED"
    assert verdict.throughput_estimate_available is False
    assert "strictly positive" in verdict.failed_requirements[-1]
    assert validation._campaign_figure_eligible("PASS", effects) is True

    negative_results = _synthetic_breakthrough_results((-0.1,) * 5)
    negative_effects = validation._headline_effects(manifest, negative_results)
    assert negative_effects is not None
    negative = validation._breakthrough_verdict(
        manifest,
        negative_results,
        campaign_outcome="PASS",
        effects=negative_effects,
    )
    assert negative.status == "NOT_SUPPORTED"
    assert validation._campaign_figure_eligible("PASS", negative_effects) is True


def _campaign_chronology_rows(
    monotonic_ns: tuple[object, ...] = (1, 2, 3, 4),
    recorded_utc: tuple[str, ...] = (
        "2026-08-04T00:00:01+00:00",
        "2026-08-04T00:00:02+00:00",
        "2026-08-04T00:00:03+00:00",
        "2026-08-04T00:00:04+00:00",
    ),
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "recorded_monotonic_ns": timestamp,
            "recorded_utc": wall_time,
        }
        for timestamp, wall_time in zip(monotonic_ns, recorded_utc)
    )


def test_campaign_chronology_accepts_strict_full_monotonic_chain() -> None:
    validation._validate_campaign_chronology(
        approved_utc="2026-08-04T00:00:00+00:00",
        ledger_rows=_campaign_chronology_rows(),
        completed_utc="2026-08-04T00:00:04+00:00",
    )


@pytest.mark.parametrize(
    "monotonic_ns",
    (
        (1, 2, 2, 4),
        (1, 2, 1, 4),
    ),
)
def test_campaign_chronology_rejects_equal_or_regressing_monotonic_chain(
    monotonic_ns: tuple[object, ...],
) -> None:
    with pytest.raises(FactorialValidationError, match="strictly increasing"):
        validation._validate_campaign_chronology(
            approved_utc="2026-08-04T00:00:00+00:00",
            ledger_rows=_campaign_chronology_rows(monotonic_ns),
            completed_utc="2026-08-04T00:00:04+00:00",
        )


@pytest.mark.parametrize("invalid", (0, True, 1.0, "1"))
def test_campaign_chronology_requires_exact_positive_monotonic_integers(
    invalid: object,
) -> None:
    with pytest.raises(FactorialValidationError, match=r"integer >= 1"):
        validation._validate_campaign_chronology(
            approved_utc="2026-08-04T00:00:00+00:00",
            ledger_rows=_campaign_chronology_rows((1, invalid, 3, 4)),
            completed_utc="2026-08-04T00:00:04+00:00",
        )


@pytest.mark.parametrize(
    "recorded_utc",
    (
        (
            "2026-08-03T23:59:59+00:00",
            "2026-08-04T00:00:02+00:00",
            "2026-08-04T00:00:03+00:00",
            "2026-08-04T00:00:04+00:00",
        ),
        (
            "2026-08-04T00:00:01+00:00",
            "2026-08-04T00:00:00+00:00",
            "2026-08-04T00:00:03+00:00",
            "2026-08-04T00:00:04+00:00",
        ),
    ),
)
def test_campaign_chronology_rejects_preapproval_or_regressing_row_utc(
    recorded_utc: tuple[str, ...],
) -> None:
    with pytest.raises(FactorialValidationError, match="UTC chronology"):
        validation._validate_campaign_chronology(
            approved_utc="2026-08-04T00:00:00+00:00",
            ledger_rows=_campaign_chronology_rows(recorded_utc=recorded_utc),
            completed_utc="2026-08-04T00:00:04+00:00",
        )


@pytest.mark.parametrize(
    "completed_utc",
    (
        "2026-08-03T23:59:59+00:00",
        "2026-08-04T00:00:03+00:00",
    ),
)
def test_campaign_chronology_rejects_summary_before_approval_or_terminal(
    completed_utc: str,
) -> None:
    with pytest.raises(FactorialValidationError, match="completion UTC precedes"):
        validation._validate_campaign_chronology(
            approved_utc="2026-08-04T00:00:00+00:00",
            ledger_rows=_campaign_chronology_rows(),
            completed_utc=completed_utc,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("approved_utc", "2026-08-04T00:00:00"),
        ("recorded_utc", "2026-08-04T00:00:01"),
        ("completed_utc", "2026-08-04T00:00:04"),
    ),
)
def test_campaign_chronology_requires_aware_wall_timestamps(
    field: str,
    value: str,
) -> None:
    arguments: dict[str, object] = {
        "approved_utc": "2026-08-04T00:00:00+00:00",
        "ledger_rows": _campaign_chronology_rows(),
        "completed_utc": "2026-08-04T00:00:04+00:00",
    }
    if field == "recorded_utc":
        rows = list(arguments["ledger_rows"])
        rows[0] = {**rows[0], "recorded_utc": value}
        arguments["ledger_rows"] = tuple(rows)
    else:
        arguments[field] = value
    with pytest.raises(FactorialValidationError, match="timezone-aware"):
        validation._validate_campaign_chronology(**arguments)


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
        result_root=manifest.results_root,
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

    equal_monotonic_rows = [dict(row) for row in rows]
    equal_monotonic_rows[1]["recorded_monotonic_ns"] = 1
    ledger_path.write_bytes(
        b"".join(_canonical(row) for row in equal_monotonic_rows)
    )
    equal_monotonic_summary = {
        **summary,
        "ledger_sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
    }
    summary_path.write_bytes(_canonical(equal_monotonic_summary))
    with pytest.raises(FactorialValidationError, match="strictly increasing"):
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

    terminal_after_summary_rows = [dict(row) for row in rows]
    terminal_after_summary_rows[0]["recorded_utc"] = (
        "2026-08-04T00:00:01+00:00"
    )
    terminal_after_summary_rows[1]["recorded_utc"] = (
        "2026-08-04T00:00:02+00:00"
    )
    ledger_path.write_bytes(
        b"".join(_canonical(row) for row in terminal_after_summary_rows)
    )
    early_summary = {
        **summary,
        "ledger_sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
        "completed_utc": "2026-08-04T00:00:01+00:00",
    }
    summary_path.write_bytes(_canonical(early_summary))
    with pytest.raises(FactorialValidationError, match="completion UTC precedes"):
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

    ledger_path.write_bytes(b"".join(_canonical(row) for row in rows))
    summary_path.write_bytes(_canonical(summary))

    typed_contract = dict(contract)
    typed_contract["outcome_dependent_order"] = 0
    (root / validation.CAMPAIGN_CONTRACT_FILENAME).write_bytes(
        _canonical(typed_contract)
    )
    with pytest.raises(FactorialValidationError, match="frozen schedule"):
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
    (root / validation.CAMPAIGN_CONTRACT_FILENAME).write_bytes(contract_bytes)

    typed_rows = [dict(row) for row in rows]
    typed_rows[0]["schema_version"] = True
    ledger_path.write_bytes(b"".join(_canonical(row) for row in typed_rows))
    with pytest.raises(FactorialValidationError, match="identity/order"):
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
    ledger_path.write_bytes(b"".join(_canonical(row) for row in rows))

    typed_summary = dict(summary)
    typed_summary["expected_slot_count"] = True
    typed_summary["ledger_sha256"] = hashlib.sha256(
        ledger_path.read_bytes()
    ).hexdigest()
    summary_path.write_bytes(_canonical(typed_summary))
    with pytest.raises(FactorialValidationError, match="summary"):
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
