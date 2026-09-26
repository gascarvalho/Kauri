"""Focused tests for the static N31 CPU/topology mechanism fixture."""

from __future__ import annotations

from copy import deepcopy

import pytest

from experiments.adaptive.kauri_experiment import static_topology_n31 as topology


REVISION = "a" * 40


def test_static_schedule_is_deterministic_and_has_exact_shape() -> None:
    baseline = topology.build_schedule("slow-roots")
    candidate = topology.build_schedule("fast-roots")

    assert topology.build_schedule("slow-roots") == baseline
    assert topology.build_schedule("fast-roots") == candidate
    assert len(baseline["trees"]) == 21
    assert [tree["tree_id"] for tree in baseline["trees"]] == list(range(21))
    assert [tree["members_breadth_first"][0] for tree in baseline["trees"]] == list(range(21))
    assert [tree["members_breadth_first"][0] for tree in candidate["trees"]][:6] == list(range(21, 27))
    assert [tree["members_breadth_first"][0] for tree in candidate["trees"]][6:] == list(range(6, 21))
    for tree in candidate["trees"]:
        members = tree["members_breadth_first"]
        assert set(members) == set(range(31))
        assert len(members) == 31
        assert all(members.index(slow) > 5 for slow in range(6))


def test_rendered_bytes_are_accepted_by_the_client_grammar_model() -> None:
    candidate = topology.build_schedule("fast-roots")
    payload = topology.render_treegen_bytes(candidate)

    parsed = topology.parse_client_treegen_bytes(payload)

    assert len(parsed) == 21
    assert parsed == [tree["members_breadth_first"] for tree in candidate["trees"]]
    assert payload.count(b"\n") == 21
    assert payload.startswith(b"fan:5 pipe:2 ")


def test_change_summary_is_six_minimal_two_position_swaps() -> None:
    baseline = topology.build_schedule("slow-roots")
    candidate = topology.build_schedule("fast-roots")

    summary = topology.changed_position_summary(baseline, candidate)

    assert summary["changed_tree_count"] == 6
    assert summary["changed_position_count"] == 12
    assert [item["tree_id"] for item in summary["trees"]] == list(range(6))
    for tree_id, item in enumerate(summary["trees"]):
        positions = item["positions"]
        assert positions[0] == {"position": 0, "from_replica_id": tree_id, "to_replica_id": 21 + tree_id}
        assert positions[1]["from_replica_id"] == 21 + tree_id
        assert positions[1]["to_replica_id"] == tree_id
        assert positions[1]["position"] > 5


def test_source_config_identity_binds_canonical_schedule_and_treegen_bytes() -> None:
    candidate = topology.build_schedule("fast-roots")
    identity = topology.source_config_identity(candidate, REVISION)

    assert identity["source_revision"] == REVISION
    assert len(identity["schedule_sha256"]) == 64
    assert len(identity["treegen_sha256"]) == 64
    assert len(identity["source_config_sha256"]) == 64
    assert identity == topology.source_config_identity(candidate, REVISION)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "newline"),
        (b"fan:5 pipe:2 " + b" ".join(str(i).encode() for i in range(31)) + b"\n", "exactly 21"),
        ((b"fan:2 pipe:2 " + b" ".join(str(i).encode() for i in range(31)) + b"\n") * 21, "unexpected fanout"),
        ((b"fan:5 pipe:2 " + b" ".join(str(i).encode() for i in range(30)) + b"\n") * 21, "syntax"),
    ],
)
def test_parser_rejects_invalid_client_lines(payload: bytes, message: str) -> None:
    with pytest.raises(topology.StaticTopologyError, match=message):
        topology.parse_client_treegen_bytes(payload)


def test_validator_rejects_role_membership_and_count_drift() -> None:
    candidate = topology.build_schedule("fast-roots")

    wrong_root = deepcopy(candidate)
    members = wrong_root["trees"][0]["members_breadth_first"]
    members[0], members[6] = members[6], members[0]
    with pytest.raises(topology.StaticTopologyError, match="tree 0"):
        topology.validate_schedule(wrong_root)

    duplicate_member = deepcopy(candidate)
    duplicate_member["trees"][2]["members_breadth_first"][6] = 30
    with pytest.raises(topology.StaticTopologyError, match="exactly once"):
        topology.validate_schedule(duplicate_member)

    missing_tree = deepcopy(candidate)
    missing_tree["trees"].pop()
    with pytest.raises(topology.StaticTopologyError, match="exactly 21"):
        topology.validate_schedule(missing_tree)

    with pytest.raises(topology.StaticTopologyError, match="Git SHA-1"):
        topology.source_config_identity(candidate, "not-a-revision")
