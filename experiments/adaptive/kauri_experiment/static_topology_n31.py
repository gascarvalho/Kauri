"""Deterministic static N=31 CPU/topology A/B mechanism fixtures.

This module describes an *experimental* 21-tree static schedule.  It is not
Kauri's ordinary 31-tree rotating Epoch-0 schedule, and it does not implement
or evidence an adaptive epoch.  The rendered lines use the existing client
``treegen_algo=file`` grammar: ``fan:<n> pipe:<n> <replica ids...>``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import re
from typing import Any


REPLICA_COUNT = 31
TREE_COUNT = 21
FANOUT = 5
PIPELINE_STRETCH = 2
MEMBERSHIP = tuple(range(REPLICA_COUNT))
SLOW_REPLICA_IDS = tuple(range(6))
FAST_REPLICA_IDS = tuple(range(6, REPLICA_COUNT))
SCHEMA = "kauri-static-n31-cpu-topology-v1"
IDENTITY_SCHEMA = "kauri-static-n31-cpu-topology-identity-v1"
SUMMARY_SCHEMA = "kauri-static-n31-cpu-topology-change-summary-v1"
_TREE_LINE = re.compile(r"fan:(?P<fanout>[0-9]+) pipe:(?P<pipeline>[0-9]+)(?P<members>(?: [0-9]+){31})")


class StaticTopologyError(ValueError):
    """The static topology artifact is malformed or differs from the fixture."""


def _error(message: str) -> None:
    raise StaticTopologyError(message)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        _error(f"{field} must be a non-empty string")
    return value


def _require_revision(value: object) -> str:
    revision = _require_string(value, "source revision")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        _error("source revision must be a lowercase full Git SHA-1")
    return revision


def _require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error(f"{field} must be an object")
    return value


def _require_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _error(f"{field} must be an integer")
    return value


def _require_members(value: object, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) != REPLICA_COUNT:
        _error(f"{field} must contain exactly {REPLICA_COUNT} replica IDs")
    members = tuple(_require_integer(item, f"{field} replica ID") for item in value)
    if set(members) != set(MEMBERSHIP):
        _error(f"{field} must contain each N31 replica exactly once")
    return members


def _is_leaf(position: int) -> bool:
    return position > FANOUT


def _baseline_members(root: int) -> list[int]:
    """Place non-root slow replicas at leaves before the A/B swap.

    The first six breadth-first positions are non-leaf positions for fanout 5.
    Filling them with fast replicas isolates the intended treatment: arm B only
    needs a two-position swap in each of the six slow-root trees.
    """

    if root not in range(TREE_COUNT):
        _error("baseline root must be one of the 21 scheduled tree IDs")
    internal_fast = [replica for replica in FAST_REPLICA_IDS if replica != root][:FANOUT]
    remaining = [
        replica
        for replica in MEMBERSHIP
        if replica != root and replica not in internal_fast
    ]
    members = [root, *internal_fast, *remaining]
    if len(members) != REPLICA_COUNT or set(members) != set(MEMBERSHIP):
        _error("internal error constructing baseline membership")
    return members


def _schedule_document(arm: str, trees: Sequence[Sequence[int]]) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "arm": arm,
        "replica_count": REPLICA_COUNT,
        "tree_count": TREE_COUNT,
        "fanout": FANOUT,
        "pipeline_stretch": PIPELINE_STRETCH,
        "membership": list(MEMBERSHIP),
        "trees": [
            {"tree_id": tree_id, "members_breadth_first": list(members)}
            for tree_id, members in enumerate(trees)
        ],
    }


def build_schedule(arm: str) -> dict[str, object]:
    """Build the reviewed static arm, with no runtime or adaptive behavior."""

    if arm not in {"slow-roots", "fast-roots"}:
        _error("arm must be 'slow-roots' or 'fast-roots'")
    trees = [_baseline_members(root) for root in range(TREE_COUNT)]
    if arm == "fast-roots":
        for tree_id in SLOW_REPLICA_IDS:
            slow_replica = tree_id
            fast_replica = 21 + tree_id
            fast_position = trees[tree_id].index(fast_replica)
            if not _is_leaf(fast_position):
                _error("selected fast replacement must start at a leaf")
            trees[tree_id][0], trees[tree_id][fast_position] = (
                trees[tree_id][fast_position],
                trees[tree_id][0],
            )
    document = _schedule_document(arm, trees)
    validate_schedule(document)
    return document


def canonical_schedule_bytes(document: Mapping[str, object]) -> bytes:
    """Validate then canonicalize the source/config schedule bytes."""

    validate_schedule(document)
    return _canonical_bytes(document)


def render_treegen_bytes(document: Mapping[str, object]) -> bytes:
    """Render strict ASCII bytes accepted by Kauri's file-treegen parser."""

    validate_schedule(document)
    trees = document["trees"]
    assert isinstance(trees, list)
    lines = []
    for tree in trees:
        assert isinstance(tree, Mapping)
        members = tree["members_breadth_first"]
        assert isinstance(members, list)
        lines.append(f"fan:{FANOUT} pipe:{PIPELINE_STRETCH} " + " ".join(map(str, members)))
    return ("\n".join(lines) + "\n").encode("ascii")


def parse_client_treegen_bytes(payload: bytes) -> list[list[int]]:
    """Strictly model the client file-treegen grammar for this N31 experiment.

    The actual C++ client reads one ``fan``/``pipe`` tree per line.  This
    stricter parser intentionally rejects blank, truncated, and extra lines so
    the experimental artifact cannot rely on the client's permissive EOF loop.
    """

    if not isinstance(payload, bytes):
        _error("treegen payload must be bytes")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise StaticTopologyError("treegen payload must be ASCII") from error
    if not text.endswith("\n") or "\r" in text:
        _error("treegen payload must use newline-terminated LF lines")
    lines = text[:-1].split("\n")
    if len(lines) != TREE_COUNT or any(not line for line in lines):
        _error("treegen payload must contain exactly 21 non-empty tree lines")
    parsed: list[list[int]] = []
    for tree_id, line in enumerate(lines):
        match = _TREE_LINE.fullmatch(line)
        if match is None:
            _error(f"tree line {tree_id} has invalid client treegen syntax")
        if int(match.group("fanout")) != FANOUT or int(match.group("pipeline")) != PIPELINE_STRETCH:
            _error(f"tree line {tree_id} has an unexpected fanout or pipeline stretch")
        members = [int(item) for item in match.group("members").split()]
        _validate_tree_members(tree_id, members)
        parsed.append(members)
    return parsed


def _validate_tree_members(tree_id: int, members: Sequence[int]) -> None:
    if tree_id not in range(TREE_COUNT):
        _error("tree ID must be in the fixed 0..20 static schedule")
    if len(members) != REPLICA_COUNT or set(members) != set(MEMBERSHIP):
        _error(f"tree {tree_id} must contain every N31 member exactly once")


def validate_schedule(document: Mapping[str, object]) -> None:
    """Reject any role, count, membership, or source-artifact drift."""

    value = _require_mapping(document, "schedule")
    expected_keys = {
        "schema", "arm", "replica_count", "tree_count", "fanout",
        "pipeline_stretch", "membership", "trees",
    }
    if set(value) != expected_keys:
        _error("schedule fields differ from the reviewed static schema")
    if value.get("schema") != SCHEMA:
        _error("schedule schema differs")
    arm = value.get("arm")
    if arm not in {"slow-roots", "fast-roots"}:
        _error("schedule arm differs")
    if (
        value.get("replica_count") != REPLICA_COUNT
        or value.get("tree_count") != TREE_COUNT
        or value.get("fanout") != FANOUT
        or value.get("pipeline_stretch") != PIPELINE_STRETCH
        or value.get("membership") != list(MEMBERSHIP)
    ):
        _error("schedule shape differs from static N31/F5/P2")
    trees = value.get("trees")
    if not isinstance(trees, list) or len(trees) != TREE_COUNT:
        _error("schedule must contain exactly 21 tree definitions")
    for tree_id, raw_tree in enumerate(trees):
        tree = _require_mapping(raw_tree, f"tree {tree_id}")
        if set(tree) != {"tree_id", "members_breadth_first"} or tree.get("tree_id") != tree_id:
            _error(f"tree {tree_id} identity differs")
        members = _require_members(tree.get("members_breadth_first"), f"tree {tree_id}")
        expected = _baseline_members(tree_id)
        if arm == "fast-roots" and tree_id in SLOW_REPLICA_IDS:
            replacement = 21 + tree_id
            replacement_position = expected.index(replacement)
            expected[0], expected[replacement_position] = expected[replacement_position], expected[0]
        if list(members) != expected:
            _error(f"tree {tree_id} differs from the deterministic static placement")
        if arm == "slow-roots":
            if members[0] != tree_id:
                _error(f"slow-roots tree {tree_id} has an incorrect root")
        elif tree_id in SLOW_REPLICA_IDS:
            if members[0] != 21 + tree_id or not _is_leaf(members.index(tree_id)):
                _error(f"fast-roots tree {tree_id} does not demote its slow root")
        else:
            if members[0] != tree_id:
                _error(f"fast-roots tree {tree_id} changed an unchanged root")
        if any(not _is_leaf(members.index(replica)) for replica in SLOW_REPLICA_IDS if replica != members[0]):
            _error(f"tree {tree_id} has a non-root slow replica above a leaf")
        if arm == "fast-roots" and any(not _is_leaf(members.index(replica)) for replica in SLOW_REPLICA_IDS):
            _error(f"fast-roots tree {tree_id} has a slow replica above a leaf")


def changed_position_summary(
    baseline: Mapping[str, object], candidate: Mapping[str, object]
) -> dict[str, object]:
    """Produce validator-ready, position-level A/B differences."""

    validate_schedule(baseline)
    validate_schedule(candidate)
    if baseline.get("arm") != "slow-roots" or candidate.get("arm") != "fast-roots":
        _error("change summary requires slow-roots baseline and fast-roots candidate")
    baseline_trees = baseline["trees"]
    candidate_trees = candidate["trees"]
    assert isinstance(baseline_trees, list) and isinstance(candidate_trees, list)
    changes = []
    for tree_id, (before, after) in enumerate(zip(baseline_trees, candidate_trees, strict=True)):
        assert isinstance(before, Mapping) and isinstance(after, Mapping)
        before_members = before["members_breadth_first"]
        after_members = after["members_breadth_first"]
        assert isinstance(before_members, list) and isinstance(after_members, list)
        positions = [
            {"position": position, "from_replica_id": old, "to_replica_id": new}
            for position, (old, new) in enumerate(zip(before_members, after_members, strict=True))
            if old != new
        ]
        if positions:
            changes.append({"tree_id": tree_id, "positions": positions})
    summary = {
        "schema": SUMMARY_SCHEMA,
        "baseline_topology_sha256": _sha256(baseline),
        "candidate_topology_sha256": _sha256(candidate),
        "changed_tree_count": len(changes),
        "changed_position_count": sum(len(item["positions"]) for item in changes),
        "trees": changes,
    }
    if summary["changed_tree_count"] != 6 or summary["changed_position_count"] != 12:
        _error("static A/B comparison must contain exactly six two-position swaps")
    return summary


def source_config_identity(document: Mapping[str, object], source_revision: str) -> dict[str, object]:
    """Bind canonical schedule and client-treegen bytes to the exact source SHA."""

    revision = _require_revision(source_revision)
    schedule_bytes = canonical_schedule_bytes(document)
    treegen_bytes = render_treegen_bytes(document)
    # This confirms the same bytes are accepted by the strict client grammar.
    parse_client_treegen_bytes(treegen_bytes)
    identity = {
        "schema": IDENTITY_SCHEMA,
        "source_revision": revision,
        "schedule_sha256": hashlib.sha256(schedule_bytes).hexdigest(),
        "treegen_sha256": hashlib.sha256(treegen_bytes).hexdigest(),
    }
    identity["source_config_sha256"] = _sha256(identity)
    return identity
