"""No-launch contract for the N=7 path-timeout-quorum Epoch-0 input."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import pytest


TREEFILE = (
    Path(__file__).resolve().parents[1]
    / "n7-path-timeout-quorum"
    / "epoch0.tree"
)
TREEFILE_SHA256 = "38a2baa37b7fcec43f5c58423be068d807cc253fedbdc379520a3e42428dffcc"
EXPECTED_ORDERS = (
    (0, 2, 3, 1, 4, 5, 6),
    (1, 2, 3, 4, 5, 6, 0),
    (2, 3, 4, 0, 1, 5, 6),
    (3, 4, 5, 6, 0, 1, 2),
    (4, 1, 5, 0, 2, 3, 6),
    (5, 1, 6, 0, 2, 3, 4),
    (6, 1, 0, 2, 3, 4, 5),
)


def test_treefile_bytes_and_all_seven_breadth_first_trees_are_frozen() -> None:
    payload = TREEFILE.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == TREEFILE_SHA256
    assert payload.endswith(b"\n")
    lines = payload.decode("ascii").splitlines()
    assert len(lines) == 7

    orders = []
    for tree_id, line in enumerate(lines):
        parts = line.split(" ")
        assert parts[:2] == ["fan:2", "pipe:2"]
        assert len(parts) == 9
        order = tuple(int(value) for value in parts[2:])
        assert order == EXPECTED_ORDERS[tree_id]
        assert set(order) == set(range(7))
        assert order[0] == tree_id
        orders.append(order)

    parent_reporters = {
        order[(order.index(1) - 1) // 2]
        for order in orders
        if 0 < order.index(1) < 3
    }
    assert parent_reporters == {4, 5, 6}
    assert orders[0].index(1) >= 3


def test_preflight_binds_nine_opportunities_but_only_six_required_timeouts() -> None:
    runner_path = TREEFILE.with_name("runner.py")
    spec = importlib.util.spec_from_file_location("n7_path_timeout_runner", runner_path)
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    preflight = runner.preflight("a" * 64)
    relay = preflight["relay_omission"]
    assert relay["total_omission_contexts"] == 9
    assert relay["required_qualifying_reporters"] == 3
    assert relay["required_timeouts_per_reporter"] == 2
    with pytest.raises(runner.PreflightError):
        runner.omission_overlay("a" * 64, context_limit=6)
