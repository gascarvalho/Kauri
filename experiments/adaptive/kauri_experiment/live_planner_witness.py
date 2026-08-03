"""Signer-aware LIVE22A model-soundness gate.

The frozen C-020 seed is structurally liftable to complete Kauri epochs, but
its strict greedy-versus-lookahead result disappears once the complete
manager-accepted signer evidence is retained.  This module builds the
deterministic rejection artifact.  It neither activates an epoch nor claims a
live or throughput result.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from itertools import combinations
import json
from typing import Any, Iterable

MEMBERSHIP = tuple(range(31))
FANOUT = 5
PIPELINE_STRETCH = 2
CONSENSUS_FAULT_THRESHOLD = 10
QUORUM = 21
DIAGNOSTIC_FAULT_BOUND = 2
WAIT_EXEMPT_LEAVES = (4, 5, 6, 8, 10, 12, 14, 21, 22)
DIAGNOSTIC_CONSTRAINED_LEAVES = (16,)
CONSTRAINED_LEAVES = frozenset(WAIT_EXEMPT_LEAVES + DIAGNOSTIC_CONSTRAINED_LEAVES)
ELIGIBLE_ROOTS = tuple(
    replica for replica in MEMBERSHIP if replica not in CONSTRAINED_LEAVES
)

PRECURSOR_TREE = (
    0,
    1,
    2,
    3,
    7,
    9,
    4,
    5,
    6,
    8,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    24,
    25,
    26,
    27,
    28,
    29,
    30,
)
PRECURSOR_TIMEOUT_EDGE = (3, 16)
CANDIDATE_IDS = tuple(f"live22a-n31-c{index:03d}" for index in range(5))
CANDIDATE_TREES = (
    (
        0,
        13,
        24,
        2,
        9,
        1,
        22,
        8,
        20,
        4,
        23,
        17,
        29,
        25,
        16,
        7,
        3,
        12,
        21,
        6,
        30,
        10,
        14,
        26,
        11,
        27,
        5,
        19,
        15,
        28,
        18,
    ),
    (
        0,
        27,
        23,
        3,
        7,
        9,
        24,
        15,
        5,
        21,
        22,
        6,
        30,
        14,
        16,
        4,
        18,
        25,
        2,
        12,
        26,
        17,
        1,
        11,
        29,
        13,
        10,
        8,
        28,
        19,
        20,
    ),
    (
        11,
        24,
        28,
        29,
        0,
        3,
        4,
        10,
        17,
        30,
        26,
        22,
        15,
        27,
        2,
        18,
        6,
        19,
        14,
        5,
        12,
        21,
        20,
        13,
        9,
        8,
        16,
        1,
        25,
        7,
        23,
    ),
    (
        30,
        11,
        7,
        15,
        3,
        17,
        14,
        12,
        18,
        27,
        24,
        8,
        19,
        25,
        23,
        20,
        28,
        26,
        13,
        5,
        9,
        0,
        29,
        10,
        16,
        1,
        4,
        6,
        2,
        22,
        21,
    ),
    (
        30,
        18,
        3,
        29,
        24,
        7,
        2,
        14,
        0,
        11,
        19,
        20,
        27,
        17,
        12,
        10,
        5,
        1,
        28,
        8,
        26,
        13,
        15,
        16,
        22,
        25,
        23,
        21,
        9,
        4,
        6,
    ),
)
EXPECTED_CANDIDATE_HASHES = (
    "10473b5f1a8a366277cc4fb89e8dfe1d0da592bd11edc802bec62982b204b307",
    "d41c5b8d925cfc6234d1bfdee063abee06cfc5182664814ef7e4f0f4ae21c8b2",
    "86a4bfb806f0e03ebfe4ad1f6a46f4780c99ec4fdffdb43d0166a400ff118e55",
    "a641cbbf16e62e744c652bd1cfdb1371d786cd3929ceb4793d6219748d944a2e",
    "1e13793e58b1021e0972e39703b0ad1d486237631e34323771f0f5c4ce0c2ddc",
)
EXPECTED_PRECURSOR_HASH = (
    "ad5552a951c785547c86781c6489e3eb8d279fa4c6c9b2aa4e43ad2a48453866"
)


@dataclass(frozen=True, order=True, slots=True)
class _Hypothesis:
    false_reporters: tuple[int, ...]
    persistent_omitters: tuple[int, ...]

    @property
    def identifier(self) -> str:
        reporters = ",".join(map(str, self.false_reporters)) or "-"
        omitters = ",".join(map(str, self.persistent_omitters)) or "-"
        return f"L[{reporters}]C[{omitters}]"


Record = tuple[str, frozenset[int]]


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _edges(order: tuple[int, ...]) -> tuple[tuple[int, int], ...]:
    if len(order) != 31 or set(order) != set(MEMBERSHIP):
        raise ValueError("tree must be an exact N=31 membership permutation")
    return tuple(
        (order[(position - 1) // FANOUT], order[position])
        for position in range(1, len(order))
    )


def _subtree_positions(root_position: int) -> tuple[int, ...]:
    positions: list[int] = []
    for position in range(root_position, len(MEMBERSHIP)):
        ancestor = position
        while ancestor > root_position:
            ancestor = (ancestor - 1) // FANOUT
        if ancestor == root_position:
            positions.append(position)
    return tuple(positions)


def _reachable_signers(
    order: tuple[int, ...],
    target_position: int,
    persistent_omitters: Iterable[int],
) -> frozenset[int]:
    omitters = frozenset(persistent_omitters)
    if order[target_position] in omitters:
        return frozenset()
    reachable: set[int] = set()
    for position in _subtree_positions(target_position):
        ancestor = position
        while True:
            if order[ancestor] in omitters:
                break
            if ancestor == target_position:
                reachable.add(order[position])
                break
            ancestor = (ancestor - 1) // FANOUT
    return frozenset(reachable)


def _enumerate_hypotheses() -> tuple[_Hypothesis, ...]:
    hypotheses: list[_Hypothesis] = []
    for reporter_count in range(DIAGNOSTIC_FAULT_BOUND + 1):
        for reporters in combinations(MEMBERSHIP, reporter_count):
            remaining = tuple(
                member for member in MEMBERSHIP if member not in reporters
            )
            for omitter_count in range(DIAGNOSTIC_FAULT_BOUND - reporter_count + 1):
                hypotheses.extend(
                    _Hypothesis(reporters, omitters)
                    for omitters in combinations(remaining, omitter_count)
                )
    result = tuple(sorted(hypotheses))
    if len(result) != 1_923:
        raise ValueError("bounded hypothesis enumeration drifted")
    return result


ALL_HYPOTHESES = _enumerate_hypotheses()


def _true_records(
    order: tuple[int, ...],
    *,
    persistent_omitters: Iterable[int] = (16,),
    false_timeout_edges: Iterable[tuple[int, int]] = (),
) -> tuple[Record, ...]:
    omitters = frozenset(persistent_omitters)
    false_timeouts = frozenset(false_timeout_edges)
    records: list[Record] = []
    for edge_position, edge in enumerate(_edges(order)):
        _, target = edge
        signers = _reachable_signers(order, edge_position + 1, omitters)
        records.append(
            ("timeout", frozenset())
            if target in omitters or edge in false_timeouts
            else ("response", signers)
        )
    return tuple(records)


def _compatible(
    hypothesis: _Hypothesis,
    order: tuple[int, ...],
    edge_position: int,
    record: Record,
) -> bool:
    reporter, target = _edges(order)[edge_position]
    outcome, signers = record
    reporters = frozenset(hypothesis.false_reporters)
    reachable = _reachable_signers(
        order,
        edge_position + 1,
        hypothesis.persistent_omitters,
    )
    if outcome == "timeout":
        return reporter in reporters or target in hypothesis.persistent_omitters
    if outcome != "response" or not signers or not reachable:
        return False
    # A false reporter may withhold verified members, but cannot forge a
    # signer whose contribution could not traverse an omitting branch.
    if reporter in reporters:
        return signers <= reachable
    target_position = edge_position + 1
    if target_position > FANOUT:
        return signers == frozenset({target})
    # An honest aggregate may be sent as soon as every required branch is
    # complete. Wait-exempt leaves can arrive before or after that send, so
    # their presence is informative but their absence is not.
    mandatory = reachable - frozenset(WAIT_EXEMPT_LEAVES)
    return mandatory <= signers <= reachable


def _belief(
    order: tuple[int, ...],
    records: tuple[Record, ...],
    hypotheses: tuple[_Hypothesis, ...] = ALL_HYPOTHESES,
) -> tuple[_Hypothesis, ...]:
    return tuple(
        hypothesis
        for hypothesis in hypotheses
        if all(
            _compatible(hypothesis, order, edge_position, record)
            for edge_position, record in enumerate(records)
        )
    )


def _response_only_precursor_belief() -> tuple[_Hypothesis, ...]:
    records = _true_records(PRECURSOR_TREE)
    return tuple(
        hypothesis
        for hypothesis in ALL_HYPOTHESES
        if all(
            reporter in hypothesis.false_reporters
            or (outcome == "timeout" and target in hypothesis.persistent_omitters)
            or (outcome == "response" and target not in hypothesis.persistent_omitters)
            for (reporter, target), (outcome, _) in zip(
                _edges(PRECURSOR_TREE), records, strict=True
            )
        )
    )


def _nonempty_subsets(values: frozenset[int]) -> Iterable[frozenset[int]]:
    canonical = tuple(sorted(values))
    for size in range(1, len(canonical) + 1):
        yield from map(frozenset, combinations(canonical, size))


def _candidate_branches(
    order: tuple[int, ...],
    initial_belief: tuple[_Hypothesis, ...],
) -> tuple[int, ...]:
    full = (1 << len(initial_belief)) - 1
    branches = {full}
    for edge_position in range(len(MEMBERSHIP) - 1):
        structural_signers = frozenset(
            order[position] for position in _subtree_positions(edge_position + 1)
        )
        possible_records = (("timeout", frozenset()),) + tuple(
            ("response", signers) for signers in _nonempty_subsets(structural_signers)
        )
        local_masks = {
            sum(
                1 << hypothesis_index
                for hypothesis_index, hypothesis in enumerate(initial_belief)
                if _compatible(hypothesis, order, edge_position, record)
            )
            for record in possible_records
        }
        local_masks.discard(0)
        branches = {
            branch & local_mask
            for branch in branches
            for local_mask in local_masks
            if branch & local_mask
        }
    return tuple(sorted(branches))


def _one_step(belief: int, branches: tuple[int, ...]) -> int:
    return max((belief & branch).bit_count() for branch in branches)


def _full_epoch(diagnostic_tree: tuple[int, ...]) -> dict[str, Any]:
    roots = (diagnostic_tree[0],) + tuple(
        root for root in ELIGIBLE_ROOTS if root != diagnostic_tree[0]
    )
    internal_by_tree: list[list[int]] = []
    for root in roots:
        root_position = ELIGIBLE_ROOTS.index(root)
        internal_by_tree.append(
            [
                ELIGIBLE_ROOTS[(root_position + offset) % len(ELIGIBLE_ROOTS)]
                for offset in range(1, 6)
            ]
        )
    baseline = set(internal_by_tree[0])
    desired = set(diagnostic_tree[1:6])
    internal_by_tree[0] = list(diagnostic_tree[1:6])
    for excess, missing in zip(
        sorted(desired - baseline), sorted(baseline - desired), strict=True
    ):
        for tree_id in range(1, len(roots)):
            internals = internal_by_tree[tree_id]
            if (
                excess in internals
                and missing not in internals
                and roots[tree_id] != missing
            ):
                internals[internals.index(excess)] = missing
                break
        else:
            raise ValueError("full-epoch internal-role repair failed")

    trees: list[dict[str, Any]] = []
    internal_counts = {replica: 0 for replica in ELIGIBLE_ROOTS}
    for tree_id, (root, internals) in enumerate(
        zip(roots, internal_by_tree, strict=True)
    ):
        if tree_id == 0:
            order = diagnostic_tree
        else:
            placed = {root, *internals}
            order = (
                root,
                *internals,
                *(replica for replica in MEMBERSHIP if replica not in placed),
            )
        if (
            len(order) != len(set(order))
            or set(order) != set(MEMBERSHIP)
            or not CONSTRAINED_LEAVES.issubset(order[6:])
        ):
            raise ValueError("full epoch violates membership or leaf constraints")
        for replica in order[1:6]:
            internal_counts[replica] += 1
        trees.append(
            {
                "tree_id": tree_id,
                "members_breadth_first": list(order),
                "fanout": FANOUT,
                "pipeline_stretch": PIPELINE_STRETCH,
            }
        )
    if set(internal_counts.values()) != {5}:
        raise ValueError("full epoch does not balance internal roles")
    return {
        "schema": "live22a-full-epoch-candidate-v2",
        "membership": list(MEMBERSHIP),
        "wait_exempt_leaves": list(WAIT_EXEMPT_LEAVES),
        "diagnostic_constrained_leaves": list(DIAGNOSTIC_CONSTRAINED_LEAVES),
        "diagnostic_projection": {
            "tree_id": 0,
            "edge_positions": list(range(30)),
        },
        "trees": trees,
    }


def _record_values(
    order: tuple[int, ...], records: tuple[Record, ...]
) -> list[dict[str, Any]]:
    return [
        {
            "edge_position": edge_position,
            "reporter_id": reporter,
            "target_id": target,
            "expected_message_type": (
                "aggregate_relay" if edge_position < FANOUT else "direct_vote"
            ),
            "outcome": outcome,
            "signer_set": sorted(signers),
        }
        for edge_position, ((reporter, target), (outcome, signers)) in enumerate(
            zip(_edges(order), records, strict=True)
        )
    ]


def _core_hypotheses() -> tuple[_Hypothesis, ...]:
    return tuple(
        _Hypothesis(reporters, (16,))
        for reporters in ((),)
        + tuple((replica,) for replica in MEMBERSHIP if replica != 16)
    )


def build_live_planner_witness() -> dict[str, Any]:
    """Return the deterministic, fail-closed LIVE22A rejection artifact."""

    response_only = _response_only_precursor_belief()
    precursor_records = _true_records(PRECURSOR_TREE)
    signer_belief = _belief(PRECURSOR_TREE, precursor_records)
    core = _core_hypotheses()
    if len(response_only) != 68 or len(signer_belief) != 33:
        raise ValueError("precursor belief counts drifted")
    if set(signer_belief) != set(core) | {
        _Hypothesis((), (0, 16)),
        _Hypothesis((0, 3), ()),
    }:
        raise ValueError("signer-aware precursor hypotheses drifted")

    full_belief = (1 << len(signer_belief)) - 1
    branches = tuple(
        _candidate_branches(order, signer_belief) for order in CANDIDATE_TREES
    )
    immediate = tuple(_one_step(full_belief, values) for values in branches)

    def best_second(belief: int) -> int:
        return min(_one_step(belief, values) for values in branches)

    horizon_two = tuple(
        max(best_second(branch) for branch in values) for values in branches
    )
    true_masks = tuple(
        sum(
            1 << index
            for index, hypothesis in enumerate(signer_belief)
            if all(
                _compatible(hypothesis, order, edge_position, record)
                for edge_position, record in enumerate(_true_records(order))
            )
        )
        for order in CANDIDATE_TREES
    )
    if (
        tuple(map(len, branches)) != (8, 8, 9, 9, 9)
        or immediate != (32, 32, 31, 31, 31)
        or horizon_two != (31, 31, 31, 31, 31)
        or tuple(mask.bit_count() for mask in true_masks) != (32, 32, 31, 31, 31)
    ):
        raise ValueError("signer-aware policy audit drifted")

    epochs = tuple(_full_epoch(order) for order in CANDIDATE_TREES)
    hashes = tuple(_canonical_hash(epoch) for epoch in epochs)
    precursor_epoch = _full_epoch(PRECURSOR_TREE)
    if (
        hashes != EXPECTED_CANDIDATE_HASHES
        or _canonical_hash(precursor_epoch) != EXPECTED_PRECURSOR_HASH
    ):
        raise ValueError("full-epoch candidate hash drifted")

    c004_order = CANDIDATE_TREES[4]
    false_timeout_records = _true_records(
        c004_order,
        persistent_omitters=(),
        false_timeout_edges=((24, 16),),
    )
    root_to_parent = _edges(c004_order).index((30, 24))
    parent_to_target = _edges(c004_order).index((24, 16))
    if (
        16 not in false_timeout_records[root_to_parent][1]
        or 16 in _true_records(c004_order)[root_to_parent][1]
        or false_timeout_records[parent_to_target][0] != "timeout"
    ):
        raise ValueError("false-timeout signer cross-check drifted")

    return {
        "schema": "live22a-signer-aware-gate-v1",
        "verdict": "REJECTED",
        "reason": (
            "complete signer-bearing runtime evidence lets a best-tied "
            "one-step policy reach the irreducible belief floor"
        ),
        "parameters": {
            "membership": list(MEMBERSHIP),
            "fanout": FANOUT,
            "pipeline_stretch": PIPELINE_STRETCH,
            "consensus_fault_threshold": CONSENSUS_FAULT_THRESHOLD,
            "quorum": QUORUM,
            "diagnostic_fault_bound": DIAGNOSTIC_FAULT_BOUND,
            "wait_exempt_leaves": list(WAIT_EXEMPT_LEAVES),
            "diagnostic_constrained_leaves": list(DIAGNOSTIC_CONSTRAINED_LEAVES),
        },
        "precursor": {
            "epoch": precursor_epoch,
            "canonical_sha256": _canonical_hash(precursor_epoch),
            "records": _record_values(PRECURSOR_TREE, precursor_records),
            "response_only_belief_count": len(response_only),
            "signer_aware_belief_count": len(signer_belief),
            "signer_aware_hypothesis_ids": [
                hypothesis.identifier for hypothesis in signer_belief
            ],
        },
        "candidates": [
            {
                "candidate_id": candidate_id,
                "epoch": epoch,
                "canonical_sha256": digest,
                "reachable_branch_supports": len(candidate_branches),
                "immediate_worst_survivors": immediate_value,
                "horizon_two_worst_survivors": horizon_value,
                "true_c16_survivors_after_first": true_mask.bit_count(),
            }
            for (
                candidate_id,
                epoch,
                digest,
                candidate_branches,
                immediate_value,
                horizon_value,
                true_mask,
            ) in zip(
                CANDIDATE_IDS,
                epochs,
                hashes,
                branches,
                immediate,
                horizon_two,
                true_masks,
                strict=True,
            )
        ],
        "policy_audit": {
            "reachable_branch_support_counts": list(map(len, branches)),
            "immediate_worst_survivors": list(immediate),
            "horizon_two_worst_survivors": list(horizon_two),
            "true_c16_survivors_after_first": [mask.bit_count() for mask in true_masks],
            "greedy_first_is_unique": False,
            "best_tied_greedy_terminal": 31,
            "joint_two_epoch_terminal": 31,
            "strict_joint_advantage": False,
        },
        "irreducible_core": {
            "count": len(core),
            "hypothesis_ids": [hypothesis.identifier for hypothesis in core],
            "lower_bound_reason": (
                "a latent false reporter may emit the truthful C16 stream "
                "forever and is intentionally unidentifiable under D-012"
            ),
            "c004_true_branch_equals_core": (
                {
                    signer_belief[index]
                    for index in range(len(signer_belief))
                    if true_masks[4] & (1 << index)
                }
                == set(core)
            ),
        },
        "signer_crosscheck": {
            "candidate_id": CANDIDATE_IDS[4],
            "false_timeout_edge": [24, 16],
            "ancestor_aggregate_edge": [30, 24],
            "healthy_false_timeout_aggregate_contains_signer_16": True,
            "true_c16_aggregate_contains_signer_16": False,
        },
        "signer_semantics": {
            "false_reporter_may_withhold_reachable_signers": True,
            "false_reporter_may_forge_blocked_signers": False,
            "honest_aggregate_requires_all_non_wait_exempt_reachable_signers": True,
            "wait_exempt_signer_absence_is_non_identifying": True,
            "direct_vote_response_requires_exact_target_signer": True,
        },
        "gate": {
            "live_planner_wiring_authorized": False,
            "matched_greedy_joint_campaign_authorized": False,
            "claim_state": "rejected",
            "preserved_claims": ["C-019", "C-020", "D-017"],
            "claim_exclusions": [
                "live planner advantage",
                "faster recovery from joint planning",
                "fewer plausible attackers",
            ],
        },
    }


def canonical_live_planner_witness_json(witness: object) -> str:
    """Serialize the artifact canonically and reject non-finite values."""

    return json.dumps(
        witness,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = (
    "build_live_planner_witness",
    "canonical_live_planner_witness_json",
)
