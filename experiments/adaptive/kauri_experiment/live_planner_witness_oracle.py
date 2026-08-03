"""Independent validator for the signer-aware LIVE22A rejection artifact.

This module deliberately does not import the artifact builder.  It recomputes
the bounded hypotheses, signer-bearing branch supports, policy values, and
full-epoch hashes directly from the JSON surface.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from itertools import combinations
import json
from typing import Any, Iterable

MEMBERSHIP = tuple(range(31))
FANOUT = 5
PIPELINE_STRETCH = 2
FAULT_THRESHOLD = 10
QUORUM = 21
TD = 2
WAIT_EXEMPT = (4, 5, 6, 8, 10, 12, 14, 21, 22)
DIAGNOSTIC_LEAVES = (16,)
CONSTRAINED = frozenset(WAIT_EXEMPT + DIAGNOSTIC_LEAVES)
ELIGIBLE_ROOTS = tuple(replica for replica in MEMBERSHIP if replica not in CONSTRAINED)
EXPECTED_PRECURSOR_HASH = (
    "ad5552a951c785547c86781c6489e3eb8d279fa4c6c9b2aa4e43ad2a48453866"
)
EXPECTED_CANDIDATE_HASHES = (
    "10473b5f1a8a366277cc4fb89e8dfe1d0da592bd11edc802bec62982b204b307",
    "d41c5b8d925cfc6234d1bfdee063abee06cfc5182664814ef7e4f0f4ae21c8b2",
    "86a4bfb806f0e03ebfe4ad1f6a46f4780c99ec4fdffdb43d0166a400ff118e55",
    "a641cbbf16e62e744c652bd1cfdb1371d786cd3929ceb4793d6219748d944a2e",
    "1e13793e58b1021e0972e39703b0ad1d486237631e34323771f0f5c4ce0c2ddc",
)


class LivePlannerWitnessOracleError(ValueError):
    """The artifact does not prove the frozen fail-closed verdict."""


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


def _fail(message: str) -> None:
    raise LivePlannerWitnessOracleError(message)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail(f"{label} must be an array")
    return value


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _edges(order: tuple[int, ...]) -> tuple[tuple[int, int], ...]:
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


def _reachable(
    order: tuple[int, ...],
    target_position: int,
    omitters: Iterable[int],
) -> frozenset[int]:
    blocked = frozenset(omitters)
    if order[target_position] in blocked:
        return frozenset()
    signers: set[int] = set()
    for position in _subtree_positions(target_position):
        ancestor = position
        while True:
            if order[ancestor] in blocked:
                break
            if ancestor == target_position:
                signers.add(order[position])
                break
            ancestor = (ancestor - 1) // FANOUT
    return frozenset(signers)


def _hypotheses() -> tuple[_Hypothesis, ...]:
    result: list[_Hypothesis] = []
    for reporter_count in range(TD + 1):
        for reporters in combinations(MEMBERSHIP, reporter_count):
            remaining = tuple(
                replica for replica in MEMBERSHIP if replica not in reporters
            )
            for omitter_count in range(TD - reporter_count + 1):
                result.extend(
                    _Hypothesis(reporters, omitters)
                    for omitters in combinations(remaining, omitter_count)
                )
    values = tuple(sorted(result))
    if len(values) != 1_923:
        _fail("independent hypothesis enumeration drifted")
    return values


def _compatible(
    hypothesis: _Hypothesis,
    order: tuple[int, ...],
    edge_position: int,
    record: Record,
) -> bool:
    reporter, target = _edges(order)[edge_position]
    outcome, signers = record
    reporters = frozenset(hypothesis.false_reporters)
    reachable = _reachable(order, edge_position + 1, hypothesis.persistent_omitters)
    if outcome == "timeout":
        return reporter in reporters or target in hypothesis.persistent_omitters
    if outcome != "response" or not signers or not reachable:
        return False
    if reporter in reporters:
        return signers <= reachable
    target_position = edge_position + 1
    if target_position > FANOUT:
        return signers == frozenset({target})
    mandatory = reachable - frozenset(WAIT_EXEMPT)
    return mandatory <= signers <= reachable


def _belief(
    hypotheses: tuple[_Hypothesis, ...],
    order: tuple[int, ...],
    records: tuple[Record, ...],
) -> tuple[_Hypothesis, ...]:
    return tuple(
        hypothesis
        for hypothesis in hypotheses
        if all(
            _compatible(hypothesis, order, edge_position, record)
            for edge_position, record in enumerate(records)
        )
    )


def _response_only_belief(
    hypotheses: tuple[_Hypothesis, ...],
    order: tuple[int, ...],
    records: tuple[Record, ...],
) -> tuple[_Hypothesis, ...]:
    return tuple(
        hypothesis
        for hypothesis in hypotheses
        if all(
            reporter in hypothesis.false_reporters
            or (outcome == "timeout" and target in hypothesis.persistent_omitters)
            or (outcome == "response" and target not in hypothesis.persistent_omitters)
            for (reporter, target), (outcome, _) in zip(
                _edges(order), records, strict=True
            )
        )
    )


def _nonempty_subsets(values: frozenset[int]) -> Iterable[frozenset[int]]:
    ordered = tuple(sorted(values))
    for size in range(1, len(ordered) + 1):
        yield from map(frozenset, combinations(ordered, size))


def _branches(
    belief: tuple[_Hypothesis, ...], order: tuple[int, ...]
) -> tuple[int, ...]:
    current = {(1 << len(belief)) - 1}
    for edge_position in range(30):
        structural = frozenset(
            order[position] for position in _subtree_positions(edge_position + 1)
        )
        records = (("timeout", frozenset()),) + tuple(
            ("response", signers) for signers in _nonempty_subsets(structural)
        )
        masks = {
            sum(
                1 << index
                for index, hypothesis in enumerate(belief)
                if _compatible(hypothesis, order, edge_position, record)
            )
            for record in records
        }
        masks.discard(0)
        current = {
            branch & mask for branch in current for mask in masks if branch & mask
        }
    return tuple(sorted(current))


def _one_step(belief: int, branches: tuple[int, ...]) -> int:
    return max((belief & branch).bit_count() for branch in branches)


def _validate_epoch(value: object, label: str) -> tuple[int, ...]:
    epoch = _mapping(value, label)
    expected_top = {
        "schema": "live22a-full-epoch-candidate-v2",
        "membership": list(MEMBERSHIP),
        "wait_exempt_leaves": list(WAIT_EXEMPT),
        "diagnostic_constrained_leaves": list(DIAGNOSTIC_LEAVES),
        "diagnostic_projection": {
            "tree_id": 0,
            "edge_positions": list(range(30)),
        },
    }
    if set(epoch) != set(expected_top) | {"trees"}:
        _fail(f"{label} schema fields drifted")
    for key, expected in expected_top.items():
        if epoch.get(key) != expected:
            _fail(f"{label}.{key} drifted")
    trees = _sequence(epoch.get("trees"), f"{label}.trees")
    if len(trees) != 21:
        _fail(f"{label} must have 21 trees")
    roots: list[int] = []
    internal_counts = {replica: 0 for replica in ELIGIBLE_ROOTS}
    tree_zero: tuple[int, ...] | None = None
    for tree_id, tree_value in enumerate(trees):
        tree = _mapping(tree_value, f"{label}.trees[{tree_id}]")
        if set(tree) != {
            "tree_id",
            "fanout",
            "pipeline_stretch",
            "members_breadth_first",
        }:
            _fail(f"{label}.trees[{tree_id}] schema fields drifted")
        if (
            tree.get("tree_id") != tree_id
            or tree.get("fanout") != FANOUT
            or tree.get("pipeline_stretch") != PIPELINE_STRETCH
        ):
            _fail(f"{label}.trees[{tree_id}] metadata drifted")
        members = _sequence(
            tree.get("members_breadth_first"),
            f"{label}.trees[{tree_id}].members_breadth_first",
        )
        if (
            len(members) != 31
            or any(type(replica) is not int for replica in members)
            or set(members) != set(MEMBERSHIP)
        ):
            _fail(f"{label}.trees[{tree_id}] is not a permutation")
        order = tuple(members)
        if not CONSTRAINED.issubset(order[6:]):
            _fail(f"{label}.trees[{tree_id}] violates leaf constraints")
        if order[0] not in internal_counts or any(
            replica not in internal_counts for replica in order[1:6]
        ):
            _fail(f"{label}.trees[{tree_id}] uses an ineligible role")
        roots.append(order[0])
        for replica in order[1:6]:
            internal_counts[replica] += 1
        if tree_id == 0:
            tree_zero = order
    if set(roots) != set(ELIGIBLE_ROOTS) or len(set(roots)) != 21:
        _fail(f"{label} roots are not exact")
    if set(internal_counts.values()) != {5}:
        _fail(f"{label} internal roles are not balanced")
    if tree_zero is None:
        _fail(f"{label} lacks tree zero")
    return tree_zero


def _records(value: object, order: tuple[int, ...]) -> tuple[Record, ...]:
    records_value = _sequence(value, "precursor.records")
    if len(records_value) != 30:
        _fail("precursor must retain all 30 exact edge records")
    records: list[Record] = []
    for edge_position, record_value in enumerate(records_value):
        record = _mapping(record_value, f"precursor.records[{edge_position}]")
        if set(record) != {
            "edge_position",
            "reporter_id",
            "target_id",
            "expected_message_type",
            "outcome",
            "signer_set",
        }:
            _fail(f"precursor record {edge_position} schema fields drifted")
        reporter, target = _edges(order)[edge_position]
        expected_type = "aggregate_relay" if edge_position < FANOUT else "direct_vote"
        if (
            record.get("edge_position") != edge_position
            or record.get("reporter_id") != reporter
            or record.get("target_id") != target
            or record.get("expected_message_type") != expected_type
        ):
            _fail(f"precursor record {edge_position} identity drifted")
        outcome = record.get("outcome")
        signer_values = _sequence(
            record.get("signer_set"),
            f"precursor.records[{edge_position}].signer_set",
        )
        if (
            outcome not in ("response", "timeout")
            or any(type(signer) is not int for signer in signer_values)
            or list(signer_values) != sorted(set(signer_values))
        ):
            _fail(f"precursor record {edge_position} is malformed")
        signers = frozenset(signer_values)
        if (outcome == "timeout") != (not signers):
            _fail(f"precursor record {edge_position} outcome/signers disagree")
        records.append((outcome, signers))
    return tuple(records)


def validate_live_planner_witness(value: object) -> dict[str, Any]:
    """Recompute the rejection verdict without trusting builder results."""

    witness = _mapping(value, "witness")
    expected_keys = {
        "schema",
        "verdict",
        "reason",
        "parameters",
        "precursor",
        "candidates",
        "policy_audit",
        "irreducible_core",
        "signer_crosscheck",
        "signer_semantics",
        "gate",
    }
    if set(witness) != expected_keys:
        _fail("witness schema fields drifted")
    if (
        witness.get("schema") != "live22a-signer-aware-gate-v1"
        or witness.get("verdict") != "REJECTED"
    ):
        _fail("witness schema or verdict drifted")
    expected_parameters = {
        "membership": list(MEMBERSHIP),
        "fanout": FANOUT,
        "pipeline_stretch": PIPELINE_STRETCH,
        "consensus_fault_threshold": FAULT_THRESHOLD,
        "quorum": QUORUM,
        "diagnostic_fault_bound": TD,
        "wait_exempt_leaves": list(WAIT_EXEMPT),
        "diagnostic_constrained_leaves": list(DIAGNOSTIC_LEAVES),
    }
    if dict(_mapping(witness.get("parameters"), "parameters")) != expected_parameters:
        _fail("parameters drifted")
    if witness.get("reason") != (
        "complete signer-bearing runtime evidence lets a best-tied one-step "
        "policy reach the irreducible belief floor"
    ):
        _fail("rejection reason drifted")
    expected_signer_semantics = {
        "false_reporter_may_withhold_reachable_signers": True,
        "false_reporter_may_forge_blocked_signers": False,
        "honest_aggregate_requires_all_non_wait_exempt_reachable_signers": True,
        "wait_exempt_signer_absence_is_non_identifying": True,
        "direct_vote_response_requires_exact_target_signer": True,
    }
    if (
        dict(_mapping(witness.get("signer_semantics"), "signer_semantics"))
        != expected_signer_semantics
    ):
        _fail("signer semantics drifted")

    precursor = _mapping(witness.get("precursor"), "precursor")
    if set(precursor) != {
        "epoch",
        "canonical_sha256",
        "records",
        "response_only_belief_count",
        "signer_aware_belief_count",
        "signer_aware_hypothesis_ids",
    }:
        _fail("precursor schema fields drifted")
    precursor_order = _validate_epoch(precursor.get("epoch"), "precursor.epoch")
    if (
        precursor.get("canonical_sha256") != EXPECTED_PRECURSOR_HASH
        or _canonical_hash(precursor.get("epoch")) != EXPECTED_PRECURSOR_HASH
    ):
        _fail("precursor hash drifted")
    records = _records(precursor.get("records"), precursor_order)
    all_hypotheses = _hypotheses()
    response_only_belief = _response_only_belief(
        all_hypotheses, precursor_order, records
    )
    belief = _belief(all_hypotheses, precursor_order, records)
    if len(response_only_belief) != 68 or len(belief) != 33:
        _fail("precursor must independently yield response-only B68 and signer B33")
    if (
        precursor.get("response_only_belief_count") != len(response_only_belief)
        or precursor.get("signer_aware_belief_count") != 33
        or precursor.get("signer_aware_hypothesis_ids")
        != [hypothesis.identifier for hypothesis in belief]
    ):
        _fail("precursor belief claims drifted")

    candidates = _sequence(witness.get("candidates"), "candidates")
    if len(candidates) != 5:
        _fail("the rejected seed must preserve all five candidates")
    orders: list[tuple[int, ...]] = []
    for index, candidate_value in enumerate(candidates):
        candidate = _mapping(candidate_value, f"candidates[{index}]")
        if set(candidate) != {
            "candidate_id",
            "epoch",
            "canonical_sha256",
            "reachable_branch_supports",
            "immediate_worst_survivors",
            "horizon_two_worst_survivors",
            "true_c16_survivors_after_first",
        }:
            _fail(f"candidate {index} schema fields drifted")
        if candidate.get("candidate_id") != f"live22a-n31-c{index:03d}":
            _fail(f"candidate {index} identifier drifted")
        order = _validate_epoch(candidate.get("epoch"), f"candidates[{index}].epoch")
        expected_hash = EXPECTED_CANDIDATE_HASHES[index]
        if (
            candidate.get("canonical_sha256") != expected_hash
            or _canonical_hash(candidate.get("epoch")) != expected_hash
        ):
            _fail(f"candidate {index} hash drifted")
        orders.append(order)

    branch_sets = tuple(_branches(belief, order) for order in orders)
    full = (1 << len(belief)) - 1
    immediate = tuple(_one_step(full, branches) for branches in branch_sets)

    def best_second(branch: int) -> int:
        return min(_one_step(branch, branches) for branches in branch_sets)

    horizon_two = tuple(
        max(best_second(branch) for branch in branches) for branches in branch_sets
    )
    true_masks = tuple(
        sum(
            1 << index
            for index, hypothesis in enumerate(belief)
            if all(
                _compatible(
                    hypothesis,
                    order,
                    edge_position,
                    (
                        ("timeout", frozenset())
                        if target == 16
                        else (
                            "response",
                            _reachable(order, edge_position + 1, (16,)),
                        )
                    ),
                )
                for edge_position, (_, target) in enumerate(_edges(order))
            )
        )
        for order in orders
    )
    counts = tuple(map(len, branch_sets))
    true_counts = tuple(mask.bit_count() for mask in true_masks)
    if (
        counts != (8, 8, 9, 9, 9)
        or immediate != (32, 32, 31, 31, 31)
        or horizon_two != (31, 31, 31, 31, 31)
        or true_counts != (32, 32, 31, 31, 31)
    ):
        _fail("independent policy replay drifted")
    for index, candidate_value in enumerate(candidates):
        candidate = _mapping(candidate_value, f"candidates[{index}]")
        claimed = (
            candidate.get("reachable_branch_supports"),
            candidate.get("immediate_worst_survivors"),
            candidate.get("horizon_two_worst_survivors"),
            candidate.get("true_c16_survivors_after_first"),
        )
        expected = (
            counts[index],
            immediate[index],
            horizon_two[index],
            true_counts[index],
        )
        if claimed != expected:
            _fail(f"candidate {index} policy claims drifted")

    policy = _mapping(witness.get("policy_audit"), "policy_audit")
    expected_policy = {
        "reachable_branch_support_counts": list(counts),
        "immediate_worst_survivors": list(immediate),
        "horizon_two_worst_survivors": list(horizon_two),
        "true_c16_survivors_after_first": list(true_counts),
        "greedy_first_is_unique": False,
        "best_tied_greedy_terminal": 31,
        "joint_two_epoch_terminal": 31,
        "strict_joint_advantage": False,
    }
    if dict(policy) != expected_policy:
        _fail("policy audit claims drifted")

    core = tuple(
        _Hypothesis(reporters, (16,))
        for reporters in ((),)
        + tuple((replica,) for replica in MEMBERSHIP if replica != 16)
    )
    core_value = _mapping(witness.get("irreducible_core"), "irreducible_core")
    expected_lower_bound_reason = (
        "a latent false reporter may emit the truthful C16 stream forever "
        "and is intentionally unidentifiable under D-012"
    )
    if (
        set(core_value)
        != {
            "count",
            "hypothesis_ids",
            "lower_bound_reason",
            "c004_true_branch_equals_core",
        }
        or core_value.get("count") != 31
        or core_value.get("hypothesis_ids")
        != [hypothesis.identifier for hypothesis in core]
        or core_value.get("lower_bound_reason") != expected_lower_bound_reason
        or core_value.get("c004_true_branch_equals_core") is not True
    ):
        _fail("irreducible core proof drifted")
    c004_true = {
        belief[index] for index in range(len(belief)) if true_masks[4] & (1 << index)
    }
    if c004_true != set(core):
        _fail("c004 does not attain the K31 lower bound")

    crosscheck = _mapping(witness.get("signer_crosscheck"), "signer_crosscheck")
    expected_crosscheck = {
        "candidate_id": "live22a-n31-c004",
        "false_timeout_edge": [24, 16],
        "ancestor_aggregate_edge": [30, 24],
        "healthy_false_timeout_aggregate_contains_signer_16": True,
        "true_c16_aggregate_contains_signer_16": False,
    }
    if dict(crosscheck) != expected_crosscheck:
        _fail("signer cross-check claims drifted")
    c004 = orders[4]
    root_edge = _edges(c004).index((30, 24))
    target_edge = _edges(c004).index((24, 16))
    healthy_root_signers = _reachable(c004, root_edge + 1, ())
    omitted_root_signers = _reachable(c004, root_edge + 1, (16,))
    if (
        16 not in healthy_root_signers
        or 16 in omitted_root_signers
        or _edges(c004)[target_edge] != (24, 16)
    ):
        _fail("independent signer cross-check replay drifted")

    gate = _mapping(witness.get("gate"), "gate")
    expected_gate = {
        "live_planner_wiring_authorized": False,
        "matched_greedy_joint_campaign_authorized": False,
        "claim_state": "rejected",
        "preserved_claims": ["C-019", "C-020", "D-017"],
        "claim_exclusions": [
            "live planner advantage",
            "faster recovery from joint planning",
            "fewer plausible attackers",
        ],
    }
    if dict(gate) != expected_gate:
        _fail("fail-closed gate drifted")
    return {
        "status": "PASS",
        "verdict": "REJECTED",
        "initial_belief_count": 33,
        "irreducible_core_count": 31,
        "immediate_worst_survivors": list(immediate),
        "horizon_two_worst_survivors": list(horizon_two),
        "candidate_hashes": [candidate["canonical_sha256"] for candidate in candidates],
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    _fail(f"JSON contains non-finite number {value}")


def parse_and_validate_live_planner_witness(
    payload: str | bytes,
) -> dict[str, Any]:
    """Parse strict JSON and validate the artifact independently."""

    if not isinstance(payload, (str, bytes)):
        _fail("artifact payload must be text or bytes")
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except LivePlannerWitnessOracleError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LivePlannerWitnessOracleError(
            f"artifact is not valid JSON: {error}"
        ) from error
    return validate_live_planner_witness(value)


__all__ = (
    "LivePlannerWitnessOracleError",
    "parse_and_validate_live_planner_witness",
    "validate_live_planner_witness",
)
