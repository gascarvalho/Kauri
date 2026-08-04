"""Contract tests for the frozen SHAPE25 planning manifest."""

from __future__ import annotations

from collections import Counter
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive.kauri_experiment.factorial_manifest import (
    EXPECTED_SLOT_COUNT,
    FROZEN_MANIFEST_ID,
    FROZEN_MANIFEST_SHA256,
    FROZEN_PLAN_SHA256,
    FactorialManifestError,
    build_factorial_plan,
    canonical_plan_bytes,
    derive_consensus_shape,
    derive_execution_schedule,
    derive_slot_nonce,
    epoch0_internal_tree_ids,
    load_frozen_manifest,
    load_frozen_manifest_bytes,
    parse_manifest_bytes,
    rotating_omission_actor,
    tree_depth,
)

REPOSITORY = Path(__file__).resolve().parents[3]
MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v1.json"
)


def _manifest():
    return load_frozen_manifest(MANIFEST_PATH)


def _mutable_document() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _encoded(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=True, indent=2, allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


def test_loader_binds_the_exact_duplicate_free_manifest_bytes(tmp_path: Path) -> None:
    manifest = _manifest()

    assert manifest.manifest_id == FROZEN_MANIFEST_ID
    assert manifest.manifest_sha256 == FROZEN_MANIFEST_SHA256
    assert hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest() == (
        FROZEN_MANIFEST_SHA256
    )

    changed = tmp_path / "changed.json"
    changed.write_bytes(MANIFEST_PATH.read_bytes() + b"\n")
    with pytest.raises(FactorialManifestError, match="exact frozen manifest bytes"):
        load_frozen_manifest(changed)

    duplicate = MANIFEST_PATH.read_bytes().replace(
        b'  "schema_version": 1,',
        b'  "schema_version": 1,\n  "schema_version": 1,',
        1,
    )
    with pytest.raises(FactorialManifestError, match="duplicate key"):
        parse_manifest_bytes(duplicate)


@pytest.mark.parametrize("forbidden", ("f", "Q", "fault_threshold", "quorum"))
def test_manifest_never_accepts_caller_supplied_consensus_authority(
    forbidden: str,
) -> None:
    document = _mutable_document()
    document[forbidden] = 1

    with pytest.raises(FactorialManifestError, match="derived|unknown field"):
        parse_manifest_bytes(_encoded(document))


@pytest.mark.parametrize("replica_count", (13, 22, 31))
def test_consensus_authority_and_depths_are_derived(replica_count: int) -> None:
    manifest = _manifest()
    shape = derive_consensus_shape(
        replica_count,
        initial_fanout=2,
        candidate_fanouts=manifest.candidate_fanouts,
    )

    expected_f = (replica_count - 1) // 3
    assert replica_count == 3 * expected_f + 1
    assert shape.f == expected_f
    assert shape.q == 2 * expected_f + 1
    assert shape.tree_count == shape.q
    assert shape.initial_depth == tree_depth(replica_count, 2)
    assert dict(shape.candidate_depths) == {
        fanout: tree_depth(replica_count, fanout)
        for fanout in manifest.candidate_fanouts
    }
    assert shape.worst_candidate_depth == max(dict(shape.candidate_depths).values())

    with pytest.raises(FactorialManifestError, match=r"N = 3f \+ 1"):
        derive_consensus_shape(
            replica_count + 1,
            initial_fanout=2,
            candidate_fanouts=manifest.candidate_fanouts,
        )
    with pytest.raises(FactorialManifestError, match="Q/tree count"):
        derive_consensus_shape(
            400,
            initial_fanout=2,
            candidate_fanouts=manifest.candidate_fanouts,
        )


@pytest.mark.parametrize(
    ("replica_count", "fanout", "expected"),
    (
        (13, 1, 12),
        (13, 2, 3),
        (13, 3, 2),
        (31, 2, 4),
        (31, 5, 2),
    ),
)
def test_tree_depth_is_the_minimum_uniform_fanout_depth(
    replica_count: int,
    fanout: int,
    expected: int,
) -> None:
    assert tree_depth(replica_count, fanout) == expected


@pytest.mark.parametrize(
    "candidate_fanouts",
    ([2, 2, 5], [0, 2, 5], [2, 5, 256], [True, 2, 5]),
)
def test_candidate_fanouts_are_unique_uint8_values(
    candidate_fanouts: list[object],
) -> None:
    document = _mutable_document()
    document["candidate_fanouts"] = candidate_fanouts

    with pytest.raises(FactorialManifestError, match="candidate fanout"):
        parse_manifest_bytes(_encoded(document))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("epoch_fanout_policy", "mixed_per_level"),
        ("pipeline_policy", "adaptive"),
    ),
)
def test_first_slice_rejects_mixed_fanout_and_adaptive_pipeline(
    field: str,
    value: str,
) -> None:
    document = _mutable_document()
    document["shape_constraints"][field] = value  # type: ignore[index]

    with pytest.raises(FactorialManifestError, match="fanout|pipeline"):
        parse_manifest_bytes(_encoded(document))


def test_manifest_freezes_the_exact_factorial_matrix_and_68_slots() -> None:
    manifest = _manifest()
    plan = build_factorial_plan(manifest)

    assert manifest.replica_counts == (13, 22, 31)
    assert manifest.initial_fanouts == (2, 3, 5)
    assert manifest.candidate_fanouts == (2, 3, 5)
    assert tuple(arm.code for arm in manifest.arms) == ("00", "P", "S", "PS")
    assert len(plan.slots) == EXPECTED_SLOT_COUNT == 68
    assert all(slot.replica_count != 7 for slot in plan.slots)

    slots_per_cell = Counter(
        (slot.replica_count, slot.initial_fanout) for slot in plan.slots
    )
    assert slots_per_cell == Counter(
        {
            (replica_count, fanout): (
                20 if replica_count == 31 and fanout in (2, 5) else 4
            )
            for replica_count in (13, 22, 31)
            for fanout in (2, 3, 5)
        }
    )
    assert Counter(slot.arm_code for slot in plan.slots) == Counter(
        {"00": 17, "P": 17, "S": 17, "PS": 17}
    )
    for repeated_fanout in (2, 5):
        assert Counter(
            slot.block_index
            for slot in plan.slots
            if (slot.replica_count, slot.initial_fanout) == (31, repeated_fanout)
        ) == Counter({index: 4 for index in range(1, 6)})


def test_slots_are_immutable_deterministic_and_self_contained() -> None:
    manifest = _manifest()
    first = build_factorial_plan(manifest)
    second = build_factorial_plan(manifest)

    assert first == second
    assert first.slots == second.slots
    assert len({slot.slot_id for slot in first.slots}) == EXPECTED_SLOT_COUNT
    assert len({slot.scientific_seed for slot in first.slots}) == 17
    assert Counter(slot.scientific_seed for slot in first.slots) == Counter(
        {seed: 4 for seed in range(41_719, 41_736)}
    )
    assert [slot.slot_nonce for slot in first.slots] == list(range(EXPECTED_SLOT_COUNT))
    assert all(
        slot.slot_nonce == derive_slot_nonce(slot.block_id, slot.arm_code)
        for slot in first.slots
    )
    assert len({slot.result_path for slot in first.slots}) == EXPECTED_SLOT_COUNT
    assert [slot.ordinal for slot in first.slots] == list(
        range(1, EXPECTED_SLOT_COUNT + 1)
    )
    assert {slot.execution_ordinal for slot in first.slots} == set(
        range(1, EXPECTED_SLOT_COUNT + 1)
    )
    assert first.slots[0].slot_id == "slot-001-n13-f2-b01-00"
    assert first.slots[-1].slot_id == "slot-068-n31-f5-b05-PS"
    assert first.slots[0].scientific_seed == 41_719
    assert first.slots[3].scientific_seed == 41_719
    assert first.slots[4].scientific_seed == 41_720
    assert first.slots[-1].scientific_seed == 41_735
    assert first.slots[0].ports.as_document() == {
        "client_base": 26100,
        "manager": 27100,
        "peer_base": 25100,
    }
    assert first.slots[1].ports.peer_base == 25200
    assert all(
        slot.result_path == f"results/shape-placement-factorial-v1/{slot.slot_id}"
        for slot in first.slots
    )

    with pytest.raises(FrozenInstanceError):
        first.slots[0].scientific_seed = 0  # type: ignore[misc]


def test_each_slot_derives_actors_and_uses_one_common_timer_contract() -> None:
    manifest = _manifest()
    plan = build_factorial_plan(manifest)

    assert manifest.byzantine.mode == "rotating_intermittent_omission_v1"
    assert manifest.byzantine.actor_count == 3
    assert manifest.byzantine.actor_count_rule == "fixed_3_bounded_by_derived_f"
    assert manifest.byzantine.actor_selection == (
        "sha256_ranked_canonical_epoch0_non_reference_roots_v1"
    )
    assert manifest.byzantine.actor_selection_preimage == (
        "ascii_csv_membership_nul_decimal_q_nul_decimal_scientific_"
        "seed_nul_decimal_replica_id_v1"
    )
    assert manifest.byzantine.actor_rotation == (
        "fnv1a64_be_epoch_tree_epoch_digest_block_hash_" "modulo_sorted_actors_v1"
    )
    assert manifest.byzantine.maximum_rotating_contexts == 100_000
    assert manifest.byzantine.actions.as_document() == {
        "internal": "omit_aggregate",
        "leaf": "omit_direct_vote",
        "root": "normal",
    }
    assert manifest.byzantine.start_after_prelaunch_anchor_s == 150
    assert manifest.byzantine.duration_s == 300
    assert manifest.byzantine.max_omissions_per_proposal == 1

    actors_by_block: dict[str, tuple[int, ...]] = {}
    for slot in plan.slots:
        assert len(slot.byzantine_actor_ids) == 3
        assert len(set(slot.byzantine_actor_ids)) == 3
        assert all(
            slot.q <= actor < slot.replica_count for actor in slot.byzantine_actor_ids
        )
        assert slot.common_timers == manifest.common_timers
        assert slot.common_timers.as_document() == {
            "activation_delay_blocks": 5,
            "aggregation_timeout_ms_per_depth": 125,
            "depth_policy": "global_worst_candidate_depth_linear_v1",
            "drain_margin_s": 20,
            "global_worst_candidate_depth": 4,
            "hard_timeout_s": 500,
            "leader_activation_grace_ms": 1000,
            "leader_progress_timeout_ms_per_depth": 5000,
            "schedule_slack_s": 30,
            "startup_timeout_s": 120,
            "transition_convergence_deadline_s": 20,
        }
        assert slot.common_timers.aggregation_timeout_ms == 500
        assert slot.common_timers.leader_progress_timeout_ms == 20_000
        assert slot.pipeline_stretch == 2
        assert slot.epoch_fanout_policy == "one_uniform_fanout_per_epoch"
        assert slot.pipeline_policy == "fixed_first_slice"
        assert (
            actors_by_block.setdefault(slot.block_id, slot.byzantine_actor_ids)
            == slot.byzantine_actor_ids
        )
    for vector in manifest.byzantine.actor_selection_vectors:
        membership = ",".join(map(str, range(vector.replica_count)))
        ranked = sorted(
            range(vector.q, vector.replica_count),
            key=lambda member: (
                hashlib.sha256(
                    (
                        f"{membership}\x00{vector.q}\x00"
                        f"{vector.scientific_seed}\x00{member}"
                    ).encode("ascii")
                ).digest(),
                member,
            ),
        )
        assert vector.selected_actor_ids == tuple(sorted(ranked[:3]))
    repeated_actor_sets = {
        (slot.initial_fanout, slot.block_id): slot.byzantine_actor_ids
        for slot in plan.slots
        if slot.replica_count == 31
        and slot.initial_fanout in (2, 5)
        and slot.arm_code == "00"
    }
    assert len(set(repeated_actor_sets.values())) > 1


def test_every_actor_candidate_is_physically_internal_in_an_active_epoch0_tree() -> (
    None
):
    plan = build_factorial_plan(_manifest())
    block_slots = tuple(slot for slot in plan.slots if slot.arm_code == "00")

    assert len(block_slots) == 17
    for slot in block_slots:
        for actor in range(slot.q, slot.replica_count):
            tree_ids = epoch0_internal_tree_ids(
                slot.replica_count,
                initial_fanout=slot.initial_fanout,
                replica_id=actor,
            )
            # Native epoch 0 rotates all N cyclic trees.  Tree actor-1 puts
            # this actor at breadth-first position one, which has children in
            # every frozen N/fanout cell.
            witness_tree = (actor - 1) % slot.replica_count
            members = tuple(
                (position + witness_tree) % slot.replica_count
                for position in range(slot.replica_count)
            )
            position = members.index(actor)
            assert witness_tree in tree_ids
            assert position == 1
            assert position * slot.initial_fanout + 1 < slot.replica_count
        assert set(slot.byzantine_actor_ids) <= set(range(slot.q, slot.replica_count))


def test_plan_seals_preflight_parameters_but_never_authorizes_execution() -> None:
    manifest = _manifest()
    plan = build_factorial_plan(manifest)

    assert manifest.workload.as_document() == {
        "baseline_bucket_count": 6,
        "block_size": 1000,
        "bucket_width_s": 5,
        "epoch1_stable_bucket_count": 6,
        "epoch2_stable_bucket_count": 6,
        "fault_evidence_bucket_count": 6,
        "piped_latency_ms": 1,
        "tree_switch_period_blocks": 1,
    }
    assert manifest.resources.minimum_free_bytes == 10_000_000_000
    assert manifest.resources.max_parallel_slots == 1
    assert plan.execution_authorized is False
    assert plan.execution_receipt_required is True
    assert plan.execution_mode == "fixed_sequential"
    assert plan.automatic_retries == 0
    assert plan.replacement_policy == "none"
    assert plan.outcome_dependent_order is False
    assert plan.preserve_outcomes == (
        "NOT_STARTED",
        "PASS",
        "FAIL",
        "INCOMPLETE",
    )
    assert plan.global_worst_candidate_depth == 4
    assert plan.claim_scope.as_document() == {
        "causal_inference": ("matched_repeated_blocks_within_prespecified_strata_only"),
        "directional_claim_rule": (
            "lower_95_ci_strictly_greater_than_zero_v1"
        ),
        "headline_estimator": (
            "arithmetic_mean_of_five_matched_block_contrasts_v1"
        ),
        "other_cells": "parameter_coverage_only",
        "phase_sequence_endpoint": (
            "adaptive_arms_mean_tps_matched_by_block_v1"
        ),
        "placement_headline_block_count": 5,
        "placement_headline_initial_fanout": 5,
        "placement_headline_replica_count": 31,
        "shape_and_joint_headline_block_count": 5,
        "shape_and_joint_headline_initial_fanout": 2,
        "shape_and_joint_headline_replica_count": 31,
        "uncertainty_interval": "two_sided_student_t_95_df4_v1",
    }
    assert manifest.responsiveness_policy.as_document() == {
        "attempt_window": 128,
        "latency_percentile_basis_points": 5000,
        "maximum_timeout_rate_ppm": 50000,
        "minimum_attempts": 32,
        "minimum_response_rate_ppm": 950000,
        "policy_version": "shape25-sensitive-responsiveness-v1",
        "trailing_timeout_streak": 2,
    }
    assert all(
        slot.responsiveness_policy == manifest.responsiveness_policy
        and 1_000_000 // len(slot.byzantine_actor_ids)
        > manifest.responsiveness_policy.maximum_timeout_rate_ppm
        for slot in plan.slots
    )
    with pytest.raises(FactorialManifestError, match="not authorized|receipt"):
        plan.require_execution_authorized()


def test_canonical_plan_bytes_are_stable_and_bind_source_byte_identity() -> None:
    manifest = _manifest()
    plan = build_factorial_plan(manifest)
    encoded = canonical_plan_bytes(plan)

    assert encoded.endswith(b"\n")
    assert encoded == canonical_plan_bytes(build_factorial_plan(manifest))
    assert json.loads(encoded)["manifest_sha256"] == FROZEN_MANIFEST_SHA256
    assert hashlib.sha256(encoded).hexdigest() == plan.plan_sha256
    assert plan.plan_sha256 == FROZEN_PLAN_SHA256

    changed_bytes = MANIFEST_PATH.read_bytes() + b" "
    changed_manifest = parse_manifest_bytes(changed_bytes)
    changed_plan = build_factorial_plan(changed_manifest)
    assert changed_manifest.manifest_sha256 != manifest.manifest_sha256
    assert canonical_plan_bytes(changed_plan) != encoded
    with pytest.raises(FactorialManifestError, match="exact frozen manifest bytes"):
        load_frozen_manifest_bytes(changed_bytes)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("global_worst_candidate_depth", 3, "global worst candidate depth"),
        ("aggregation_timeout_ms_per_depth", 124, "500 ms"),
        ("leader_progress_timeout_ms_per_depth", 4_999, "20000 ms"),
    ),
)
def test_timer_policy_is_integer_and_bound_to_global_worst_depth(
    field: str,
    value: int,
    match: str,
) -> None:
    document = _mutable_document()
    document["timers"][field] = value  # type: ignore[index]

    with pytest.raises(FactorialManifestError, match=match):
        parse_manifest_bytes(_encoded(document))


@pytest.mark.parametrize(
    ("section", "field", "value", "match"),
    (
        (
            "window",
            "start_after_prelaunch_anchor_s",
            149,
            "startup timeout plus the clean baseline",
        ),
        ("window", "duration_s", 179, "duration must cover"),
        ("timers", "hard_timeout_s", 349, "hard timeout must cover"),
        ("timers", "schedule_slack_s", 29, "at least 30 seconds"),
        (
            "timers",
            "transition_convergence_deadline_s",
            0,
            "transition_convergence_deadline_s",
        ),
    ),
)
def test_fault_window_timing_is_mechanical_and_hard_timeout_feasible(
    section: str,
    field: str,
    value: int,
    match: str,
) -> None:
    document = _mutable_document()
    if section == "window":
        document["byzantine"]["window"][field] = value  # type: ignore[index]
    else:
        document["timers"][field] = value  # type: ignore[index]

    with pytest.raises(FactorialManifestError, match=match):
        parse_manifest_bytes(_encoded(document))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("attempt_window", 127),
        ("minimum_attempts", 31),
        ("minimum_response_rate_ppm", 949_999),
        ("maximum_timeout_rate_ppm", 50_001),
        ("trailing_timeout_streak", 3),
        ("latency_percentile_basis_points", 4_999),
    ),
)
def test_responsiveness_policy_is_frozen_for_rotating_actor_feasibility(
    field: str,
    value: int,
) -> None:
    document = _mutable_document()
    document["responsiveness_policy"][field] = value  # type: ignore[index]

    with pytest.raises(FactorialManifestError, match="frozen sensitive profile"):
        parse_manifest_bytes(_encoded(document))


def test_manifest_preserves_not_started_slots() -> None:
    document = _mutable_document()
    document["artifacts"]["preserve_outcomes"] = [  # type: ignore[index]
        "PASS",
        "FAIL",
        "INCOMPLETE",
    ]

    with pytest.raises(FactorialManifestError, match="unstarted"):
        parse_manifest_bytes(_encoded(document))


def test_actor_rotation_vectors_bind_the_native_fnv1a_contract() -> None:
    manifest = _manifest()

    for vector in manifest.byzantine.actor_rotation_vectors:
        assert rotating_omission_actor(
            vector.sorted_actor_ids,
            epoch_number=vector.epoch_number,
            tree_id=vector.tree_id,
            epoch_digest=vector.epoch_digest,
            block_hash=vector.block_hash,
        ) == (vector.fnv1a64, vector.selected_actor)

    document = _mutable_document()
    document["byzantine"]["actor_rotation_vectors"][0][  # type: ignore[index]
        "selected_actor"
    ] = 1
    with pytest.raises(FactorialManifestError, match="FNV-1a reference"):
        parse_manifest_bytes(_encoded(document))


def test_execution_schedule_is_predeclared_balanced_and_identity_preserving() -> None:
    manifest = _manifest()
    plan = build_factorial_plan(manifest)
    recomputed = derive_execution_schedule(
        tuple(reversed(tuple(item.block_id for item in plan.execution_schedule))),
        tuple(arm.code for arm in manifest.arms),
        manifest.campaign_order_seed,
    )

    assert recomputed == plan.execution_schedule
    assert plan.execution_block_order == "sha256_ranked_block_ids_v1"
    assert plan.arm_counterbalancing == (
        "greedy_minimum_position_imbalance_sha256_tiebreak_v1"
    )
    position_counts = Counter(
        (slot.arm_code, slot.arm_execution_position) for slot in plan.slots
    )
    assert set(position_counts.values()) <= {4, 5}
    assert len(position_counts) == 16
    for headline_fanout in (2, 5):
        headline_orders = {
            scheduled.arm_order
            for scheduled in plan.execution_schedule
            if scheduled.block_id.startswith(f"n31-f{headline_fanout}-")
        }
        assert len(headline_orders) > 1

    for slot in plan.slots:
        assert slot.slot_nonce == derive_slot_nonce(slot.block_id, slot.arm_code)
        assert slot.ordinal == slot.slot_nonce + 1
        assert slot.result_path.endswith(slot.slot_id)
    assert tuple(slot.slot_id for slot in plan.slots[:4]) == (
        "slot-001-n13-f2-b01-00",
        "slot-002-n13-f2-b01-P",
        "slot-003-n13-f2-b01-S",
        "slot-004-n13-f2-b01-PS",
    )
    assert tuple(
        slot.execution_ordinal
        for slot in sorted(plan.slots, key=lambda item: item.execution_ordinal)
    ) == tuple(range(1, EXPECTED_SLOT_COUNT + 1))
