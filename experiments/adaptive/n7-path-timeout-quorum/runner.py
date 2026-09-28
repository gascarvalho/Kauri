#!/usr/bin/env python3
"""Fail-closed preflight for the prospective N=7 relay-omission run.

This is deliberately a *preflight*, not a campaign launcher.  It freezes the
only experiment-only inputs that a later live launcher may use: the seven
  initial trees and the three aggregate-relay omission paths.  It never
starts a process, changes a consensus configuration, or declares a result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence


REPLICA_IDS = tuple(range(7))
OMITTING_REPLICA = 1
EXPECTED_REPORTERS = (4, 5, 6)
TREE_IDS = (4, 5, 6)
TREE_FILE = Path(__file__).with_name("epoch0.tree")
SCENARIO = "n7-path-local-timeout-quorum-v3"


class PreflightError(ValueError):
    """The prospective run inputs do not meet the frozen contract."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_tree_file(path: Path) -> tuple[tuple[int, ...], ...]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise PreflightError(f"cannot read tree file: {exc}") from exc
    if len(lines) != len(REPLICA_IDS):
        raise PreflightError("tree file must contain exactly seven trees")
    trees: list[tuple[int, ...]] = []
    for tree_id, line in enumerate(lines):
        fields = line.split()
        if fields[:2] != ["fan:2", "pipe:2"] or len(fields) != 9:
            raise PreflightError(f"tree {tree_id} must be fan:2 pipe:2 plus seven IDs")
        try:
            members = tuple(int(value) for value in fields[2:])
        except ValueError as exc:
            raise PreflightError(f"tree {tree_id} has a non-integer member") from exc
        if set(members) != set(REPLICA_IDS) or len(set(members)) != 7:
            raise PreflightError(f"tree {tree_id} must contain each N=7 member once")
        if members[0] != tree_id:
            raise PreflightError(f"tree {tree_id} root must equal its tree ID")
        trees.append(members)
    return tuple(trees)


def relay_parent_reporters(trees: Sequence[Sequence[int]]) -> tuple[int, ...]:
    reporters: list[int] = []
    for members in trees:
        position = tuple(members).index(OMITTING_REPLICA)
        # For N=7/fanout=2, positions 1 and 2 are internal children of root.
        if position == 0 or position >= 3:
            continue
        reporters.append(int(members[(position - 1) // 2]))
    return tuple(reporters)


def omission_overlay(epoch_digest: str, *, context_limit: int = 9) -> tuple[str, ...]:
    if len(epoch_digest) != 64 or any(ch not in "0123456789abcdef" for ch in epoch_digest):
        raise PreflightError("epoch digest must be 64 lower-case hex characters")
    if context_limit != 9:
        raise PreflightError("three-reporter preflight requires exactly nine omission contexts")
    return (
        "--experiment-omit-outbound-aggregate",
        "--experiment-byzantine-configuration",
        f"0:4:{epoch_digest}",
        "--experiment-omission-additional-configurations",
        f"0:5:{epoch_digest},0:6:{epoch_digest}",
        "--experiment-byzantine-window",
        SCENARIO,
        "--experiment-byzantine-context-limit",
        str(context_limit),
        "--experiment-omission-contexts-per-configuration",
        "3",
    )


def preflight(epoch_digest: str, tree_file: Path = TREE_FILE) -> dict[str, Any]:
    trees = parse_tree_file(tree_file)
    reporters = relay_parent_reporters(trees)
    if reporters != EXPECTED_REPORTERS:
        raise PreflightError(
            f"relay 1 must have the exact parent reporters {EXPECTED_REPORTERS}, got {reporters}"
        )
    return {
        "schema_version": 1,
        "scenario": SCENARIO,
        "status": "PREFLIGHT_ONLY",
        "claim_boundary": "no process was launched and no adaptive epoch was activated",
        "tree_file": {
            "path": str(tree_file.resolve()),
            "sha256": sha256_file(tree_file),
            "trees": [list(tree) for tree in trees],
            "main_config_overrides": [
                "tree-generation = file",
                f"tree-generation-fpath = {tree_file.resolve()}",
            ],
        },
        "relay_omission": {
            "replica_id": OMITTING_REPLICA,
            "epoch_digest": epoch_digest,
            "tree_ids": list(TREE_IDS),
            "parent_reporters": list(reporters),
            "expected_message_type": "aggregate_relay",
            "required_qualifying_reporters": 3,
            "required_timeouts_per_reporter": 2,
            "total_omission_contexts": 9,
            "argv_overlay": list(omission_overlay(epoch_digest)),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epoch-digest", required=True)
    parser.add_argument("--tree-file", type=Path, default=TREE_FILE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = preflight(args.epoch_digest, args.tree_file)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, indent=2)
            stream.write("\n")
    except (OSError, PreflightError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
