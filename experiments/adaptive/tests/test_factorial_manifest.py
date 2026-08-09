"""Contract tests for the frozen SHAPE25 planning manifest."""

from __future__ import annotations

from collections import Counter
from dataclasses import FrozenInstanceError
import hashlib
import json
import math
from pathlib import Path

import pytest

from experiments.adaptive.kauri_experiment.factorial_manifest import (
    EXPECTED_SLOT_COUNT,
    FROZEN_MANIFEST_ID,
    FROZEN_MANIFEST_SHA256,
    FROZEN_PLAN_SHA256,
    FROZEN_SEMANTIC_SHA256,
    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1,
    RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1,
    RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1,
    RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
    RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1,
    RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1,
    RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
    V2_MANIFEST_ID,
    V2_MANIFEST_SHA256,
    V3_MANIFEST_ID,
    V3_MANIFEST_SHA256,
    V3_PLAN_SHA256,
    V4_MANIFEST_ID,
    V4_MANIFEST_SHA256,
    V4_PLAN_SHA256,
    V5_MANIFEST_ID,
    V5_MANIFEST_SHA256,
    V5_PLAN_SHA256,
    V6_MANIFEST_ID,
    V6_MANIFEST_SHA256,
    V6_PLAN_SHA256,
    V7_MANIFEST_ID,
    V7_MANIFEST_SHA256,
    V7_PLAN_SHA256,
    V8_MANIFEST_ID,
    V8_MANIFEST_SHA256,
    V8_PLAN_SHA256,
    V9_MANIFEST_ID,
    V9_MANIFEST_SHA256,
    V9_PLAN_SHA256,
    V9_SEMANTIC_SHA256,
    V10_MANIFEST_ID,
    V10_MANIFEST_SHA256,
    V10_PLAN_SHA256,
    V10_SEMANTIC_SHA256,
    V11_MANIFEST_ID,
    V11_MANIFEST_SHA256,
    V11_PLAN_SHA256,
    V11_SEMANTIC_SHA256,
    FactorialManifestError,
    build_factorial_plan,
    canonical_plan_bytes,
    derive_actor_ids,
    derive_consensus_shape,
    derive_stratified_execution_schedule,
    derive_responsive_degraded_actor_ids,
    derive_slot_nonce,
    derive_tiered_cohorts,
    epoch0_distinct_parent_ids,
    epoch0_internal_tree_ids,
    load_frozen_manifest,
    load_frozen_manifest_bytes,
    parse_manifest_bytes,
    rotating_omission_actor,
    tree_depth,
)

REPOSITORY = Path(__file__).resolve().parents[3]
MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v12.json"
)
V11_MANIFEST_PATH = (
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
    assert hashlib.sha256(
        json.dumps(
            json.loads(MANIFEST_PATH.read_bytes()),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    ).hexdigest() == FROZEN_SEMANTIC_SHA256
    assert manifest.evidence_snapshot_format == "digest_commitment_v2"

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
        slot.result_path == f"results/shape-placement-factorial-v12/{slot.slot_id}"
        for slot in first.slots
    )

    with pytest.raises(FrozenInstanceError):
        first.slots[0].scientific_seed = 0  # type: ignore[misc]


def test_each_slot_derives_disjoint_tiered_cohorts_and_common_timers() -> None:
    manifest = _manifest()
    plan = build_factorial_plan(manifest)

    assert manifest.byzantine.mode == "tiered_persistent_responsive_omission_v2"
    assert manifest.byzantine.actor_count == 3
    assert manifest.byzantine.actor_count_rule == "fixed_3_bounded_by_derived_f"
    assert manifest.byzantine.actor_selection == (
        "sha256_ranked_canonical_epoch0_non_reference_roots_v1"
    )
    assert manifest.byzantine.actor_selection_preimage == (
        "ascii_csv_membership_nul_decimal_q_nul_decimal_scientific_"
        "seed_nul_decimal_replica_id_v1"
    )
    assert manifest.byzantine.actor_schedule == "all_hard_actors_per_proposal_v1"
    assert manifest.byzantine.maximum_rotating_contexts == 100_000
    assert manifest.byzantine.actions.as_document() == {
        "internal": "omit_aggregate",
        "leaf": "omit_direct_vote",
        "root": "normal",
    }
    assert manifest.byzantine.start_after_prelaunch_anchor_s == 150
    assert manifest.byzantine.duration_s == 300
    assert manifest.byzantine.max_omissions_per_proposal is None
    assert (
        manifest.byzantine.max_omissions_per_proposal_rule
        == "derived_f_per_slot_v1"
    )
    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    assert responsive.actor_count_rule == "derived_f_minus_hard_actor_count_v1"
    assert responsive.actor_selection == (
        "sha256_ranked_canonical_epoch0_reference_roots_"
        "excluding_commit_observer_v1"
    )
    assert responsive.actor_selection_preimage == (
        r"kauri.shape25.responsive-degraded.v1\0{membership_csv}"
        r"\0{q}\0{scientific_seed}\0{replica_id}"
    )
    assert responsive.observer_isolation == (
        "replica_0_reserved_authoritative_commit_observer_v1"
    )
    assert responsive.actor_schedule == (
        "omit_every_41st_unique_non_root_contribution_per_"
        "exact_epoch_identity_physical_role_stream_v1"
    )
    assert responsive.omission_period == 41
    assert (
        responsive.pending_attempt_retention
        == RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1
    )
    assert responsive.causal_timeout_linkage == RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1
    assert responsive.causal_timeout_provenance_window == (
        RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1
    )
    assert responsive.causal_internal_witness_candidates == (
        RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1
    )
    assert responsive.causal_selection_linkage_window == (
        RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1
    )
    assert responsive.marker_completeness_witness == (
        RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1
    )
    assert responsive.causal_timeout_eligibility == (
        RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1
    )

    actors_by_block: dict[str, tuple[int, ...]] = {}
    degraded_by_block: dict[str, tuple[int, ...]] = {}
    fast_by_block: dict[str, tuple[int, ...]] = {}
    for slot in plan.slots:
        assert len(slot.byzantine_actor_ids) == 3
        assert len(set(slot.byzantine_actor_ids)) == 3
        assert all(
            slot.q <= actor < slot.replica_count for actor in slot.byzantine_actor_ids
        )
        assert len(slot.responsive_degraded_actor_ids) == slot.f - 3
        assert all(
            1 <= actor < slot.q for actor in slot.responsive_degraded_actor_ids
        )
        assert 0 not in slot.responsive_degraded_actor_ids
        worse = set(
            (*slot.byzantine_actor_ids, *slot.responsive_degraded_actor_ids)
        )
        assert len(worse) == slot.f
        assert set(slot.byzantine_actor_ids).isdisjoint(
            slot.responsive_degraded_actor_ids
        )
        assert slot.fast_replica_ids == tuple(
            member for member in range(slot.replica_count) if member not in worse
        )
        assert len(slot.fast_replica_ids) == slot.q
        assert 0 in slot.fast_replica_ids
        assert slot.max_omissions_per_proposal == slot.f
        assert slot.maximum_omissions_per_proposal == slot.f
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
            "transition_observation_bound_rule": (
                "shared_slot_hard_deadline_until_manager_selection_v1"
            ),
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
        assert (
            degraded_by_block.setdefault(
                slot.block_id, slot.responsive_degraded_actor_ids
            )
            == slot.responsive_degraded_actor_ids
        )
        assert (
            fast_by_block.setdefault(slot.block_id, slot.fast_replica_ids)
            == slot.fast_replica_ids
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
    for vector in responsive.actor_selection_vectors:
        membership = ",".join(map(str, range(vector.replica_count)))
        hard = derive_actor_ids(
            vector.replica_count,
            vector.q,
            3,
            vector.scientific_seed,
        )
        expected_count = (vector.replica_count - 1) // 3 - len(hard)
        ranked = sorted(
            range(1, vector.q),
            key=lambda member: (
                hashlib.sha256(
                    (
                        "kauri.shape25.responsive-degraded.v1"
                        f"\x00{membership}\x00{vector.q}\x00"
                        f"{vector.scientific_seed}\x00{member}"
                    ).encode("ascii")
                ).digest(),
                member,
            ),
        )
        assert vector.selected_actor_ids == tuple(
            sorted(ranked[:expected_count])
        )
        assert vector.selected_actor_ids == derive_responsive_degraded_actor_ids(
            vector.replica_count,
            vector.q,
            hard,
            vector.scientific_seed,
        )
        cohorts = derive_tiered_cohorts(
            vector.replica_count,
            vector.q,
            3,
            vector.scientific_seed,
        )
        assert cohorts.hard_actor_ids == hard
        assert cohorts.responsive_degraded_actor_ids == vector.selected_actor_ids
        assert len(cohorts.fast_replica_ids) == vector.q
    repeated_actor_sets = {
        (slot.initial_fanout, slot.block_id): slot.byzantine_actor_ids
        for slot in plan.slots
        if slot.replica_count == 31
        and slot.initial_fanout in (2, 5)
        and slot.arm_code == "00"
    }
    assert len(set(repeated_actor_sets.values())) > 1


def test_role_scoped_schedule_conservatively_never_exceeds_five_percent() -> None:
    manifest = _manifest()
    responsive = manifest.byzantine.responsive_degradation
    assert responsive is not None
    period = responsive.omission_period
    assert period == 41
    worst_by_attempt_count = {
        attempt_count: max(
            (
                (math.ceil(internal_attempts / period) if internal_attempts else 0)
                + (math.ceil(leaf_attempts / period) if leaf_attempts else 0)
            )
            / attempt_count
            for internal_attempts in range(attempt_count + 1)
            for leaf_attempts in (attempt_count - internal_attempts,)
        )
        for attempt_count in range(
            manifest.responsiveness_policy.minimum_attempts,
            manifest.responsiveness_policy.attempt_window + 1,
        )
    }

    assert worst_by_attempt_count[60] == 3 / 60
    assert worst_by_attempt_count[84] == 4 / 84
    assert worst_by_attempt_count[125] == 5 / 125
    assert all(rate <= 0.05 for rate in worst_by_attempt_count.values())
    rate_eligible_timeout_counts = {
        attempt_count: max(
            timeout_count
            for timeout_count in range(attempt_count + 1)
            if timeout_count * 1_000_000 // attempt_count
            <= manifest.responsiveness_policy.maximum_timeout_rate_ppm
        )
        for attempt_count in range(
            manifest.responsiveness_policy.minimum_attempts,
            manifest.responsiveness_policy.attempt_window + 1,
        )
    }
    maximum_rate_eligible_timeout_count = max(
        rate_eligible_timeout_counts.values()
    )
    assert rate_eligible_timeout_counts[119] == 5
    assert all(rate_eligible_timeout_counts[count] == 6 for count in range(120, 129))
    assert maximum_rate_eligible_timeout_count == 6
    assert manifest.responsiveness_policy.trailing_timeout_streak == (
        maximum_rate_eligible_timeout_count + 1
    )


def test_tiered_schema_rejects_observer_injection_schedule_and_bound_drift() -> None:
    document = _mutable_document()
    document["byzantine"]["responsive_degradation"]["omission_period"] = 40  # type: ignore[index]
    with pytest.raises(FactorialManifestError, match="responsive-degradation"):
        parse_manifest_bytes(_encoded(document))

    document = _mutable_document()
    document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        "actor_selection_vectors"
    ][0]["selected_actor_ids"] = [0]
    with pytest.raises(FactorialManifestError, match="vector"):
        parse_manifest_bytes(_encoded(document))

    document = _mutable_document()
    document["byzantine"]["max_omissions_per_proposal"] = 3  # type: ignore[index]
    with pytest.raises(FactorialManifestError, match="derived per slot"):
        parse_manifest_bytes(_encoded(document))

    document = _mutable_document()
    document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        "observer_isolation"
    ] = "none"
    with pytest.raises(FactorialManifestError, match="responsive-degradation"):
        parse_manifest_bytes(_encoded(document))

    document = _mutable_document()
    document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        "pending_attempt_retention"
    ] = "retire_at_commit"
    with pytest.raises(FactorialManifestError, match="causal measurement"):
        parse_manifest_bytes(_encoded(document))

    document = _mutable_document()
    del document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        "causal_timeout_linkage"
    ]
    with pytest.raises(FactorialManifestError, match="fields"):
        parse_manifest_bytes(_encoded(document))

    document = _mutable_document()
    document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        "marker_completeness_witness"
    ] = "commit_observation_only"
    with pytest.raises(FactorialManifestError, match="causal edge eligibility"):
        parse_manifest_bytes(_encoded(document))

    document = _mutable_document()
    del document["byzantine"]["responsive_degradation"][  # type: ignore[index]
        "causal_timeout_eligibility"
    ]
    with pytest.raises(FactorialManifestError, match="fields"):
        parse_manifest_bytes(_encoded(document))

    with pytest.raises(FactorialManifestError, match="proper subset"):
        derive_responsive_degraded_actor_ids(7, 5, (5, 6), 41_700)
    with pytest.raises(FactorialManifestError, match=r"\[Q, N\)"):
        derive_responsive_degraded_actor_ids(7, 5, (4,), 41_700)


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
            assert len(
                epoch0_distinct_parent_ids(
                    slot.replica_count,
                    initial_fanout=slot.initial_fanout,
                    replica_id=actor,
                )
            ) >= slot.f + 1
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
        "breakthrough_scope": (
            "n31_f5_placement_arms_p_and_ps_five_matched_blocks_v1"
        ),
        "breakthrough_structural_gate": (
            "all_n31_f5_p_ps_slots_validate_tiered_markers_match_hard_and_"
            "every_41st_unique_non_root_responsive_degraded_omission_schedule_"
            "and_each_hard_actor_has_f_plus_1_distinct_exact_role_bound_timeout_"
            "reporters_and_at_least_one_internal_omit_aggregate_proof_and_each_"
            "responsive_degraded_actor_has_its_own_exact_reporter_local_epoch1_"
            "internal_omit_aggregate_cross_commit_witness_and_responsive_"
            "degraded_replicas_rank_below_every_fast_replica_and_epoch1_places_"
            "every_responsive_degraded_replica_as_a_root_and_exposes_each_in_an_"
            "internal_role_and_epoch2_roots_equal_top_q_fast_replicas_with_only_"
            "fast_replicas_in_root_and_internal_roles_and_all_f_worse_replicas_"
            "as_physical_leaves_and_only_hard_cohort_wait_exempt_v3"
        ),
        "breakthrough_structural_required_slot_count": 10,
        "breakthrough_realized_placement_rule": (
            "for_each_p_and_ps_arm_all_5_of_5_n31_f5_blocks_have_"
            "epoch1_to_epoch2_demoted_set_exactly_responsive_degraded_cohort_"
            "and_promoted_set_exactly_canonical_non_reference_root_pool_minus_"
            "hard_cohort_v2"
        ),
        "breakthrough_realized_placement_per_arm_requirement": 5,
        "breakthrough_primary_throughput_estimand": (
            "d_b=0.5*[log((P_e2/P_e1)/(00_e2/00_e1))+"
            "log((PS_e2/PS_e1)/(S_e2/S_e1))]"
        ),
        "breakthrough_throughput_claim_rule": (
            "two_sided_student_t_95_df4_lower_log_bound_strictly_greater_than_"
            "zero_and_at_least_4_of_5_block_effects_strictly_greater_than_zero_"
            "and_absolute_fault_drop_containment_recovery_and_pooled_optimization_"
            "each_two_sided_student_t_95_df4_lower_tps_bound_strictly_greater_"
            "than_zero_and_at_least_4_of_5_blocks_strictly_greater_than_zero_and_"
            "p_and_ps_each_absolute_epoch2_minus_epoch1_tps_strictly_greater_than_"
            "zero_in_at_least_4_of_5_blocks_v2"
        ),
        "breakthrough_positive_block_requirement": 4,
        "breakthrough_epoch1_baseline_ratio_role": (
            "descriptive_only_no_noninferiority_threshold_v1"
        ),
        "breakthrough_absolute_phase_sequence_estimands": (
            "fault_drop_b=0.5*((P_baseline-P_fault)+(PS_baseline-PS_fault));"
            "containment_recovery_b=0.5*((P_e1-P_fault)+(PS_e1-PS_fault));"
            "pooled_optimization_b=0.5*((P_e2-P_e1)+(PS_e2-PS_e1));"
            "per_arm_optimization_b=(P_e2-P_e1,PS_e2-PS_e1)_v1"
        ),
        "breakthrough_phase_window_interpretation": (
            "fixed_six_5_second_bucket_windows_with_epoch_windows_anchored_at_"
            "first_authoritative_post_activation_commit_and_a_common_q_commit_"
            "required_within_each_window_not_steady_state_v1"
        ),
        "breakthrough_pre_epoch1_placebo_estimand": (
            "pP_b=log((P_fault/P_baseline)/(00_fault/00_baseline));"
            "pPS_b=log((PS_fault/PS_baseline)/(S_fault/S_baseline))"
        ),
        "breakthrough_placebo_equivalence_rule": (
            "both_component_two_one_sided_5_percent_tests_df4_90_cis_"
            "strictly_within_plus_minus_log_1p10_v2"
        ),
        "breakthrough_placebo_equivalence_margin_log": 0.09531017980432493,
        "breakthrough_secondary_scope": (
            "n31_f2_placement_arms_p_and_ps_five_matched_blocks_"
            "prespecified_secondary_v1"
        ),
        "breakthrough_secondary_status_rule": (
            "supported_only_if_primary_f5_supported_and_all_secondary_f2_gates_"
            "pass_not_supported_if_primary_f5_supported_and_any_secondary_gate_"
            "fails_descriptive_only_if_primary_f5_not_supported_v1"
        ),
        "breakthrough_secondary_structural_gate": (
            "all_n31_f2_p_ps_slots_validate_existing_tiered_full_hierarchy_and_"
            "exact_epoch1_to_epoch2_degraded_demotion_and_fast_promotion_proofs_v1"
        ),
        "breakthrough_secondary_structural_required_slot_count": 10,
        "breakthrough_secondary_realized_placement_rule": (
            "for_each_p_and_ps_arm_all_5_of_5_n31_f2_blocks_have_epoch1_to_"
            "epoch2_demoted_set_exactly_responsive_degraded_cohort_and_promoted_"
            "set_exactly_canonical_non_reference_root_pool_minus_hard_cohort_v1"
        ),
        "breakthrough_secondary_realized_placement_per_arm_requirement": 5,
        "breakthrough_secondary_throughput_estimand": (
            "d2_b=0.5*[log((P_e2/P_e1)/(00_e2/00_e1))+"
            "log((PS_e2/PS_e1)/(S_e2/S_e1))]"
        ),
        "breakthrough_secondary_throughput_claim_rule": (
            "two_sided_student_t_95_df4_lower_log_bound_strictly_greater_than_"
            "zero_and_at_least_4_of_5_block_effects_strictly_greater_than_zero_v1"
        ),
        "breakthrough_secondary_positive_block_requirement": 4,
        "breakthrough_secondary_pre_epoch1_placebo_estimand": (
            "pP2_b=log((P_fault/P_baseline)/(00_fault/00_baseline));"
            "pPS2_b=log((PS_fault/PS_baseline)/(S_fault/S_baseline))"
        ),
        "breakthrough_secondary_placebo_equivalence_rule": (
            "both_component_two_one_sided_5_percent_tests_df4_90_cis_strictly_"
            "within_plus_minus_log_1p10_v2"
        ),
        "breakthrough_secondary_placebo_equivalence_margin_log": (
            0.09531017980432493
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
        "minimum_attempts": 60,
        "minimum_response_rate_ppm": 950000,
        "policy_version": "shape25-sensitive-responsiveness-v1",
        "trailing_timeout_streak": 7,
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
        ("minimum_attempts", 40),
        ("minimum_response_rate_ppm", 949_999),
        ("maximum_timeout_rate_ppm", 50_001),
        ("trailing_timeout_streak", 2),
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


def test_v8_adds_the_tiered_failure_model_and_breakthrough_scope_to_v7() -> None:
    v3 = load_frozen_manifest(V3_MANIFEST_PATH)
    assert v3.manifest_id == V3_MANIFEST_ID
    assert v3.manifest_sha256 == V3_MANIFEST_SHA256
    assert build_factorial_plan(v3).plan_sha256 == V3_PLAN_SHA256
    assert v3.evidence_snapshot_format == "digest_commitment_v2"

    v4 = load_frozen_manifest(V4_MANIFEST_PATH)
    assert v4.manifest_id == V4_MANIFEST_ID
    assert v4.manifest_sha256 == V4_MANIFEST_SHA256
    assert build_factorial_plan(v4).plan_sha256 == V4_PLAN_SHA256

    v5 = load_frozen_manifest(V5_MANIFEST_PATH)
    assert v5.manifest_id == V5_MANIFEST_ID
    assert v5.manifest_sha256 == V5_MANIFEST_SHA256
    assert build_factorial_plan(v5).plan_sha256 == V5_PLAN_SHA256

    v6 = load_frozen_manifest(V6_MANIFEST_PATH)
    assert v6.manifest_id == V6_MANIFEST_ID
    assert v6.manifest_sha256 == V6_MANIFEST_SHA256
    assert build_factorial_plan(v6).plan_sha256 == V6_PLAN_SHA256

    v7 = load_frozen_manifest(V7_MANIFEST_PATH)
    assert v7.manifest_id == V7_MANIFEST_ID
    assert v7.manifest_sha256 == V7_MANIFEST_SHA256
    assert build_factorial_plan(v7).plan_sha256 == V7_PLAN_SHA256

    v8 = load_frozen_manifest(V8_MANIFEST_PATH)
    assert v8.manifest_id == V8_MANIFEST_ID
    assert v8.manifest_sha256 == V8_MANIFEST_SHA256
    assert build_factorial_plan(v8).plan_sha256 == V8_PLAN_SHA256

    v8_document = json.loads(V8_MANIFEST_PATH.read_bytes())
    v7_document = json.loads(V7_MANIFEST_PATH.read_bytes())
    assert v8_document.pop("manifest_id") == "shape-placement-factorial-v8"
    assert v7_document.pop("manifest_id") == "shape-placement-factorial-v7"
    assert v8_document["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v8"
    )
    assert v7_document["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v7"
    )
    v8_scope = v8_document["claim_scope"]  # type: ignore[index]
    frozen_additions = {
        key: v8_scope.pop(key)  # type: ignore[union-attr]
        for key in (
            "breakthrough_scope",
            "breakthrough_structural_gate",
            "breakthrough_structural_required_slot_count",
            "breakthrough_realized_placement_rule",
            "breakthrough_realized_placement_per_arm_requirement",
            "breakthrough_primary_throughput_estimand",
            "breakthrough_throughput_claim_rule",
            "breakthrough_positive_block_requirement",
            "breakthrough_epoch1_baseline_ratio_role",
            "breakthrough_absolute_phase_sequence_estimands",
            "breakthrough_phase_window_interpretation",
            "breakthrough_pre_epoch1_placebo_estimand",
            "breakthrough_placebo_equivalence_rule",
            "breakthrough_placebo_equivalence_margin_log",
        )
    }
    assert frozen_additions == {
        "breakthrough_scope": (
            "n31_f5_placement_arms_p_and_ps_five_matched_blocks_v1"
        ),
        "breakthrough_structural_gate": (
            "all_n31_f5_p_ps_slots_validate_tiered_markers_match_hard_and_"
            "every_32nd_unique_non_root_responsive_degraded_omission_schedule_"
            "and_each_hard_actor_has_f_plus_1_distinct_exact_role_bound_timeout_"
            "reporters_and_at_least_one_internal_omit_aggregate_proof_and_"
            "responsive_degraded_replicas_rank_below_every_fast_replica_"
            "and_epoch1_places_every_responsive_degraded_replica_as_a_root_and_"
            "exposes_each_in_an_internal_role_and_epoch2_roots_equal_top_q_fast_"
            "replicas_with_only_fast_replicas_in_root_and_internal_roles_and_"
            "all_f_worse_replicas_as_physical_leaves_and_only_hard_cohort_wait_"
            "exempt_v2"
        ),
        "breakthrough_structural_required_slot_count": 10,
        "breakthrough_realized_placement_rule": (
            "for_each_p_and_ps_arm_all_5_of_5_n31_f5_blocks_have_"
            "epoch1_to_epoch2_demoted_set_exactly_responsive_degraded_cohort_"
            "and_promoted_set_exactly_canonical_non_reference_root_pool_minus_"
            "hard_cohort_v2"
        ),
        "breakthrough_realized_placement_per_arm_requirement": 5,
        "breakthrough_primary_throughput_estimand": (
            "d_b=0.5*[log((P_e2/P_e1)/(00_e2/00_e1))+"
            "log((PS_e2/PS_e1)/(S_e2/S_e1))]"
        ),
        "breakthrough_throughput_claim_rule": (
            "two_sided_student_t_95_df4_lower_log_bound_strictly_greater_than_"
            "zero_and_at_least_4_of_5_block_effects_strictly_greater_than_zero_"
            "and_absolute_fault_drop_containment_recovery_and_pooled_optimization_"
            "each_two_sided_student_t_95_df4_lower_tps_bound_strictly_greater_"
            "than_zero_and_at_least_4_of_5_blocks_strictly_greater_than_zero_and_"
            "p_and_ps_each_absolute_epoch2_minus_epoch1_tps_strictly_greater_than_"
            "zero_in_at_least_4_of_5_blocks_v2"
        ),
        "breakthrough_positive_block_requirement": 4,
        "breakthrough_epoch1_baseline_ratio_role": (
            "descriptive_only_no_noninferiority_threshold_v1"
        ),
        "breakthrough_absolute_phase_sequence_estimands": (
            "fault_drop_b=0.5*((P_baseline-P_fault)+(PS_baseline-PS_fault));"
            "containment_recovery_b=0.5*((P_e1-P_fault)+(PS_e1-PS_fault));"
            "pooled_optimization_b=0.5*((P_e2-P_e1)+(PS_e2-PS_e1));"
            "per_arm_optimization_b=(P_e2-P_e1,PS_e2-PS_e1)_v1"
        ),
        "breakthrough_phase_window_interpretation": (
            "fixed_six_5_second_bucket_windows_with_epoch_windows_anchored_at_"
            "first_authoritative_post_activation_commit_and_a_common_q_commit_"
            "required_within_each_window_not_steady_state_v1"
        ),
        "breakthrough_pre_epoch1_placebo_estimand": (
            "pP_b=log((P_fault/P_baseline)/(00_fault/00_baseline));"
            "pPS_b=log((PS_fault/PS_baseline)/(S_fault/S_baseline))"
        ),
        "breakthrough_placebo_equivalence_rule": (
            "both_component_two_one_sided_5_percent_tests_df4_90_cis_"
            "strictly_within_plus_minus_log_1p10_v2"
        ),
        "breakthrough_placebo_equivalence_margin_log": 0.09531017980432493,
    }
    v8_byzantine = v8_document["byzantine"]  # type: ignore[index]
    assert v8_byzantine["mode"] == "tiered_persistent_responsive_omission_v1"
    assert v8_byzantine["actor_schedule"] == "all_hard_actors_per_proposal_v1"
    responsive_document = v8_byzantine.pop("responsive_degradation")
    assert responsive_document == {
        "actor_count_rule": "derived_f_minus_hard_actor_count_v1",
        "actor_selection": (
            "sha256_ranked_canonical_epoch0_reference_roots_"
            "excluding_commit_observer_v1"
        ),
        "actor_selection_preimage": (
            r"kauri.shape25.responsive-degraded.v1\0{membership_csv}"
            r"\0{q}\0{scientific_seed}\0{replica_id}"
        ),
        "actor_selection_inputs": [
            "membership",
            "derived_q",
            "canonical_epoch0_reference_roots_1_through_q_minus_1_v1",
            "replica_0_reserved_authoritative_commit_observer_v1",
            "scientific_block_seed",
        ],
        "actor_selection_vectors": [
            {
                "replica_count": 13,
                "q": 9,
                "scientific_seed": 41_719,
                "selected_actor_ids": [2],
            },
            {
                "replica_count": 22,
                "q": 15,
                "scientific_seed": 41_722,
                "selected_actor_ids": [1, 9, 10, 14],
            },
            {
                "replica_count": 31,
                "q": 21,
                "scientific_seed": 41_725,
                "selected_actor_ids": [2, 5, 7, 8, 12, 18, 19],
            },
        ],
        "observer_isolation": (
            "replica_0_reserved_authoritative_commit_observer_v1"
        ),
        "actor_schedule": (
            "omit_every_32nd_unique_non_root_contribution_per_"
            "responsive_degraded_actor_v1"
        ),
        "omission_period": 32,
    }
    assert v8_byzantine.pop("max_omissions_per_proposal_rule") == (
        "derived_f_per_slot_v1"
    )
    v8_byzantine["mode"] = "persistent_selected_omission_v1"
    v8_byzantine["actor_schedule"] = "all_selected_actors_per_proposal_v1"
    v8_byzantine["max_omissions_per_proposal"] = 3
    assert v8_document["scheduling"].pop("arm_counterbalancing") == (
        "stratified_greedy_minimum_position_imbalance_sha256_tiebreak_v2"
    )
    v8_document["scheduling"]["arm_counterbalancing"] = (
        "greedy_minimum_position_imbalance_sha256_tiebreak_v1"
    )
    assert v8_document == v7_document

    legacy_v2 = load_frozen_manifest(V2_MANIFEST_PATH)
    assert legacy_v2.manifest_id == V2_MANIFEST_ID
    assert legacy_v2.manifest_sha256 == V2_MANIFEST_SHA256
    assert legacy_v2.evidence_snapshot_format == "full_prefix_v1"

    document = _mutable_document()
    document["artifacts"]["evidence_snapshot_format"] = "full_prefix_v1"  # type: ignore[index]
    with pytest.raises(FactorialManifestError, match="digest_commitment_v2"):
        parse_manifest_bytes(_encoded(document))


def test_v9_adds_causal_measurement_and_predeclared_breakthrough_corrections() -> None:
    v9 = load_frozen_manifest(V9_MANIFEST_PATH)
    assert v9.manifest_id == V9_MANIFEST_ID
    assert v9.manifest_sha256 == V9_MANIFEST_SHA256
    assert build_factorial_plan(v9).plan_sha256 == V9_PLAN_SHA256
    assert hashlib.sha256(
        json.dumps(
            json.loads(V9_MANIFEST_PATH.read_bytes()),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    ).hexdigest() == V9_SEMANTIC_SHA256

    v9_document = json.loads(V9_MANIFEST_PATH.read_bytes())
    v8_document = json.loads(V8_MANIFEST_PATH.read_bytes())

    assert v9_document.pop("manifest_id") == "shape-placement-factorial-v9"
    assert v8_document.pop("manifest_id") == "shape-placement-factorial-v8"
    assert v9_document["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v9"
    )
    assert v8_document["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v8"
    )
    responsive = v9_document["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert responsive.pop("pending_attempt_retention") == (  # type: ignore[union-attr]
        RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1
    )
    assert responsive.pop("causal_timeout_linkage") == (  # type: ignore[union-attr]
        RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1
    )
    v8_responsive = v8_document["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert responsive["actor_schedule"] == (  # type: ignore[index]
        "omit_every_41st_unique_non_root_contribution_per_"
        "responsive_degraded_actor_v2"
    )
    assert responsive["omission_period"] == 41  # type: ignore[index]
    responsive["actor_schedule"] = v8_responsive["actor_schedule"]  # type: ignore[index]
    responsive["omission_period"] = v8_responsive["omission_period"]  # type: ignore[index]
    assert v9_document["responsiveness_policy"]["minimum_attempts"] == 41  # type: ignore[index]
    v9_document["responsiveness_policy"]["minimum_attempts"] = (  # type: ignore[index]
        v8_document["responsiveness_policy"]["minimum_attempts"]  # type: ignore[index]
    )
    v9_claim = v9_document["claim_scope"]  # type: ignore[index]
    v8_claim = v8_document["claim_scope"]  # type: ignore[index]
    secondary_fields = {
        key: v9_claim.pop(key)  # type: ignore[union-attr]
        for key in tuple(v9_claim)  # type: ignore[arg-type]
        if key.startswith("breakthrough_secondary_")
    }
    assert secondary_fields == {
        "breakthrough_secondary_scope": (
            "n31_f2_placement_arms_p_and_ps_five_matched_blocks_"
            "prespecified_secondary_v1"
        ),
        "breakthrough_secondary_status_rule": (
            "supported_only_if_primary_f5_supported_and_all_secondary_f2_gates_"
            "pass_not_supported_if_primary_f5_supported_and_any_secondary_gate_"
            "fails_descriptive_only_if_primary_f5_not_supported_v1"
        ),
        "breakthrough_secondary_structural_gate": (
            "all_n31_f2_p_ps_slots_validate_existing_tiered_full_hierarchy_and_"
            "exact_epoch1_to_epoch2_degraded_demotion_and_fast_promotion_proofs_v1"
        ),
        "breakthrough_secondary_structural_required_slot_count": 10,
        "breakthrough_secondary_realized_placement_rule": (
            "for_each_p_and_ps_arm_all_5_of_5_n31_f2_blocks_have_epoch1_to_"
            "epoch2_demoted_set_exactly_responsive_degraded_cohort_and_promoted_"
            "set_exactly_canonical_non_reference_root_pool_minus_hard_cohort_v1"
        ),
        "breakthrough_secondary_realized_placement_per_arm_requirement": 5,
        "breakthrough_secondary_throughput_estimand": (
            "d2_b=0.5*[log((P_e2/P_e1)/(00_e2/00_e1))+"
            "log((PS_e2/PS_e1)/(S_e2/S_e1))]"
        ),
        "breakthrough_secondary_throughput_claim_rule": (
            "two_sided_student_t_95_df4_lower_log_bound_strictly_greater_than_"
            "zero_and_at_least_4_of_5_block_effects_strictly_greater_than_zero_v1"
        ),
        "breakthrough_secondary_positive_block_requirement": 4,
        "breakthrough_secondary_pre_epoch1_placebo_estimand": (
            "pP2_b=log((P_fault/P_baseline)/(00_fault/00_baseline));"
            "pPS2_b=log((PS_fault/PS_baseline)/(S_fault/S_baseline))"
        ),
        "breakthrough_secondary_placebo_equivalence_rule": (
            "both_component_two_one_sided_5_percent_tests_df4_90_cis_strictly_"
            "within_plus_minus_log_1p10_v2"
        ),
        "breakthrough_secondary_placebo_equivalence_margin_log": (
            0.09531017980432493
        ),
    }
    assert "every_41st" in v9_claim["breakthrough_structural_gate"]  # type: ignore[index]
    assert "each_responsive_degraded_actor_has_its_own" in (  # type: ignore[index]
        v9_claim["breakthrough_structural_gate"]
    )
    v9_claim["breakthrough_structural_gate"] = v8_claim[  # type: ignore[index]
        "breakthrough_structural_gate"
    ]
    assert v9_document == v8_document


def test_v10_only_adds_explicit_causal_linkage_windows_to_v9() -> None:
    v10 = load_frozen_manifest(V10_MANIFEST_PATH)
    assert v10.manifest_id == V10_MANIFEST_ID
    assert v10.manifest_sha256 == V10_MANIFEST_SHA256
    assert build_factorial_plan(v10).plan_sha256 == V10_PLAN_SHA256
    assert hashlib.sha256(
        json.dumps(
            json.loads(V10_MANIFEST_PATH.read_bytes()),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    ).hexdigest() == V10_SEMANTIC_SHA256

    v10_document = json.loads(V10_MANIFEST_PATH.read_bytes())
    v9_document = json.loads(V9_MANIFEST_PATH.read_bytes())

    assert v10_document.pop("manifest_id") == "shape-placement-factorial-v10"
    assert v9_document.pop("manifest_id") == "shape-placement-factorial-v9"
    assert v10_document["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v10"
    )
    assert v9_document["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v9"
    )

    responsive = v10_document["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert responsive.pop("causal_timeout_provenance_window") == (  # type: ignore[union-attr]
        RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1
    )
    assert responsive.pop("causal_internal_witness_candidates") == (  # type: ignore[union-attr]
        RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1
    )
    assert responsive.pop("causal_selection_linkage_window") == (  # type: ignore[union-attr]
        RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1
    )
    assert v10_document == v9_document


def test_v11_only_adds_exact_causal_edge_eligibility_to_v10() -> None:
    v11 = load_frozen_manifest(V11_MANIFEST_PATH)
    assert v11.manifest_id == V11_MANIFEST_ID
    assert v11.manifest_sha256 == V11_MANIFEST_SHA256
    assert build_factorial_plan(v11).plan_sha256 == V11_PLAN_SHA256
    assert hashlib.sha256(
        json.dumps(
            json.loads(V11_MANIFEST_PATH.read_bytes()),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    ).hexdigest() == V11_SEMANTIC_SHA256

    v11_document = json.loads(V11_MANIFEST_PATH.read_bytes())
    v10_document = json.loads(V10_MANIFEST_PATH.read_bytes())

    assert v11_document.pop("manifest_id") == "shape-placement-factorial-v11"
    assert v10_document.pop("manifest_id") == "shape-placement-factorial-v10"
    assert v11_document["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v11"
    )
    assert v10_document["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v10"
    )

    responsive = v11_document["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert responsive.pop("marker_completeness_witness") == (  # type: ignore[union-attr]
        RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1
    )
    assert responsive.pop("causal_timeout_eligibility") == (  # type: ignore[union-attr]
        RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1
    )
    assert v11_document == v10_document


def test_v12_only_changes_the_role_scoped_omission_contract_from_v11() -> None:
    v12_document = json.loads(MANIFEST_PATH.read_bytes())
    v11_document = json.loads(V11_MANIFEST_PATH.read_bytes())

    assert v12_document.pop("manifest_id") == "shape-placement-factorial-v12"
    assert v11_document.pop("manifest_id") == "shape-placement-factorial-v11"
    assert v12_document["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v12"
    )
    assert v11_document["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v11"
    )
    assert v12_document["byzantine"].pop("mode") == (  # type: ignore[index]
        "tiered_persistent_responsive_omission_v2"
    )
    assert v11_document["byzantine"].pop("mode") == (  # type: ignore[index]
        "tiered_persistent_responsive_omission_v1"
    )
    v12_responsive = v12_document["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v11_responsive = v11_document["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert v12_responsive.pop("actor_schedule") == (  # type: ignore[union-attr]
        "omit_every_41st_unique_non_root_contribution_per_exact_"
        "epoch_identity_physical_role_stream_v1"
    )
    assert v11_responsive.pop("actor_schedule") == (  # type: ignore[union-attr]
        "omit_every_41st_unique_non_root_contribution_per_responsive_"
        "degraded_actor_v2"
    )
    assert v12_document["responsiveness_policy"].pop("minimum_attempts") == 60  # type: ignore[index]
    assert v11_document["responsiveness_policy"].pop("minimum_attempts") == 41  # type: ignore[index]
    assert v12_document["responsiveness_policy"].pop("trailing_timeout_streak") == 7  # type: ignore[index]
    assert v11_document["responsiveness_policy"].pop("trailing_timeout_streak") == 2  # type: ignore[index]

    assert v12_document == v11_document


def test_actor_rotation_vectors_bind_the_native_fnv1a_contract() -> None:
    manifest = load_frozen_manifest(LEGACY_MANIFEST_PATH)

    for vector in manifest.byzantine.actor_rotation_vectors:
        assert rotating_omission_actor(
            vector.sorted_actor_ids,
            epoch_number=vector.epoch_number,
            tree_id=vector.tree_id,
            epoch_digest=vector.epoch_digest,
            block_hash=vector.block_hash,
        ) == (vector.fnv1a64, vector.selected_actor)

    document = json.loads(LEGACY_MANIFEST_PATH.read_bytes())
    document["byzantine"]["actor_rotation_vectors"][0][  # type: ignore[index]
        "selected_actor"
    ] = 1
    with pytest.raises(FactorialManifestError, match="FNV-1a reference"):
        parse_manifest_bytes(_encoded(document))


def test_execution_schedule_is_predeclared_balanced_and_identity_preserving() -> None:
    manifest = _manifest()
    plan = build_factorial_plan(manifest)
    recomputed = derive_stratified_execution_schedule(
        tuple(reversed(tuple(item.block_id for item in plan.execution_schedule))),
        tuple(arm.code for arm in manifest.arms),
        manifest.campaign_order_seed,
    )

    assert recomputed == plan.execution_schedule
    assert plan.execution_block_order == "sha256_ranked_block_ids_v1"
    assert plan.arm_counterbalancing == (
        "stratified_greedy_minimum_position_imbalance_sha256_tiebreak_v2"
    )
    for headline_fanout in (2, 5):
        headline_slots = tuple(
            slot
            for slot in plan.slots
            if slot.replica_count == 31
            and slot.initial_fanout == headline_fanout
        )
        position_counts = Counter(
            (slot.arm_code, slot.arm_execution_position)
            for slot in headline_slots
        )
        assert set(position_counts.values()) == {1, 2}
        assert len(position_counts) == 16
        headline_orders = {
            scheduled.arm_order
            for scheduled in plan.execution_schedule
            if scheduled.block_id.startswith(f"n31-f{headline_fanout}-")
        }
        assert len(headline_orders) > 1
        if headline_fanout == 5:
            ordered = tuple(headline_orders)
            assert len(ordered) == 5
            reverse_pair_count = sum(
                right == tuple(reversed(left))
                for index, left in enumerate(ordered)
                for right in ordered[index + 1 :]
            )
            assert reverse_pair_count == 2
            position_only_effects = []
            for order in ordered:
                position = {arm: index for index, arm in enumerate(order, start=1)}
                position_only_effects.append(
                    0.5
                    * (
                        position["P"]
                        - position["00"]
                        + position["PS"]
                        - position["S"]
                    )
                )
            assert sum(effect > 0 for effect in position_only_effects) <= 3
            assert sum(effect < 0 for effect in position_only_effects) <= 3

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
