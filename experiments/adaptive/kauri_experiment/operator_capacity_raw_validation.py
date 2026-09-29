"""Prospective, offline schema check for future W18 derived outputs.

This module deliberately checks a proposed retained-artifact schema only.  Its
inputs are not yet bound to native structured-event logs or to a reviewed
producer, so success is not raw replay.  It does not launch Kauri, regenerate a
successor, infer missing events, compare paired arms, or make an acceptance or
throughput claim.  A valid synthetic fixture is useful only for testing this
prospective schema's fail-closed boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Mapping, Sequence

from .operator_capacity_consumption_audit import (
    ConsumptionAuditError,
    _PIN_KEYS,
    audit_consumption_chain,
)


class OperatorCapacityRawValidationError(ValueError):
    """A required W18 raw evidence source is absent or does not bind."""


_HEX = frozenset("0123456789abcdef")
_ARMS = frozenset({"fast_priority_treatment", "exact_copy_sham"})
_REPLICA_IDS = frozenset(range(31))
_TREE_IDS = frozenset(range(21))
_RAW_DIR = "raw"
_REQUIRED_RAW = frozenset({
    "manager-events.jsonl", "activation-events.jsonl", "commit-events.jsonl",
    "manager-terminal.json", "roles.json", "quota-evidence.json", "cleanup.json",
    "measurement-window.json",
})
_RAW_ONLY_PIN_KEYS = frozenset({"quota_profile_sha256", "authoritative_replica_id"})


def _fail(message: str) -> None:
    raise OperatorCapacityRawValidationError(message)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_regular(path: Path, maximum: int, label: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        _fail(f"{label} is not a readable regular file")
        raise AssertionError from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > maximum:
            _fail(f"{label} size or type is invalid")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                _fail(f"{label} changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            _fail(f"{label} changed during read")
        return b"".join(chunks)
    except OSError as exc:
        _fail(f"cannot read {label}")
        raise AssertionError from exc
    finally:
        os.close(fd)


def _object_pairs(label: str):
    def decode(items: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                _fail(f"{label} repeats JSON field {key}")
            value[key] = item
        return value
    return decode


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs(label),
                           parse_constant=lambda value: _fail(f"{label} has non-finite constant"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"{label} is not JSON")
        raise AssertionError from exc
    if not isinstance(value, dict):
        _fail(f"{label} is not an object")
    return value


def _jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    raw = _read_regular(path, 8 * 1024 * 1024, label)
    if not raw.endswith(b"\n"):
        _fail(f"{label} lacks final newline")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line:
            _fail(f"{label}:{number} is blank")
        rows.append(_json_object(line, f"{label}:{number}"))
    if not rows:
        _fail(f"{label} is empty")
    return rows


def _hex(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in _HEX for c in value):
        _fail(f"{label} is not lower-case SHA-256")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0 or value > (1 << 64) - 1:
        _fail(f"{label} is not positive uint64")
    return value


def _replica(value: object, label: str) -> int:
    if type(value) is not int or value not in _REPLICA_IDS:
        _fail(f"{label} is not an N31 replica ID")
    return value


def _exact_keys(value: Mapping[str, Any], keys: frozenset[str], label: str) -> None:
    if set(value) != keys:
        _fail(f"{label} fields differ from strict schema")


def _raw_path(root: Path, name: str) -> Path:
    raw = root / _RAW_DIR
    if raw.is_symlink() or not raw.is_dir():
        _fail("raw directory is missing or unsafe")
    path = raw / name
    if path.parent != raw or PurePosixPath(name).name != name:
        _fail("unsafe raw artifact name")
    return path


def _roles(root: Path, *, arm: str, epoch0_digest: str) -> tuple[dict[str, Any], str]:
    raw = _read_regular(_raw_path(root, "roles.json"), 512 * 1024, "roles")
    value = _json_object(raw, "roles")
    _exact_keys(value, frozenset({"schema_version", "arm", "epoch0_digest", "epoch1_digest", "epoch0", "epoch1"}), "roles")
    if value["schema_version"] != 1 or value["arm"] != arm or value["epoch0_digest"] != epoch0_digest:
        _fail("roles identity differs from authority pins")
    epoch1 = _hex(value["epoch1_digest"], "roles Epoch-1 digest")
    orders: list[list[int]] = []
    for epoch in ("epoch0", "epoch1"):
        trees = value[epoch]
        if not isinstance(trees, list) or len(trees) != 21:
            _fail(f"roles {epoch} does not contain 21 trees")
        seen_trees: set[int] = set()
        current: list[list[int]] = []
        for expected_tree_id, row in enumerate(trees):
            if not isinstance(row, dict):
                _fail(f"roles {epoch} tree is not an object")
            _exact_keys(row, frozenset({"tree_id", "member_order"}), f"roles {epoch} tree")
            tree_id = row["tree_id"]
            if (type(tree_id) is not int or tree_id not in _TREE_IDS or tree_id in seen_trees or
                    tree_id != expected_tree_id):
                _fail(f"roles {epoch} tree IDs are not exactly 0..20")
            seen_trees.add(tree_id)
            order = row["member_order"]
            if not isinstance(order, list) or len(order) != 31 or set(order) != _REPLICA_IDS or any(type(x) is not int for x in order):
                _fail(f"roles {epoch} tree membership is not an exact N31 permutation")
            current.append(order)
        if seen_trees != _TREE_IDS:
            _fail(f"roles {epoch} misses a tree")
        orders.append(current)
    if arm == "exact_copy_sham" and orders[0] != orders[1]:
        _fail("exact-copy sham does not retain exact Epoch-0 roles")
    if arm == "fast_priority_treatment" and any(order[0] < 6 for order in orders[1]):
        _fail("treatment Epoch-1 root includes a frozen slow replica")
    return value, _sha(raw)


def _manager_events(root: Path, *, epoch0_digest: str, epoch1_digest: str, roles_sha: str,
                    decision: int) -> int:
    rows = _jsonl(_raw_path(root, "manager-events.jsonl"), "manager events")
    required = frozenset({"event_type", "epoch_number", "epoch_digest", "roles_sha256", "monotonic_raw_ns"})
    if len(rows) != 2:
        _fail("manager source must contain exactly one Epoch-0 and one Epoch-1 event")
    times: list[int] = []
    expected = ((0, epoch0_digest), (1, epoch1_digest))
    for row, (epoch, digest) in zip(rows, expected):
        _exact_keys(row, required, "manager event")
        if row["event_type"] != "epoch_active":
            _fail("manager event type is invalid")
        when = _positive(row["monotonic_raw_ns"], "manager event time")
        if (row["epoch_number"] != epoch or row["epoch_digest"] != digest or
                row["roles_sha256"] != roles_sha):
            _fail("manager epoch event does not bind retained roles or timing")
        times.append(when)
    if times[0] >= decision or times[1] < decision or times[1] < times[0]:
        _fail("manager epoch chronology differs from the prospective contract")
    return times[1]


def _manager_terminal(root: Path, *, pins: Mapping[str, object], successor_sha: str,
                      epoch1_active: int, deadline: int) -> None:
    raw = _read_regular(
        _raw_path(root, "manager-terminal.json"), 32 * 1024,
        "manager terminal",
    )
    terminal = _json_object(raw, "manager terminal")
    _exact_keys(terminal, frozenset({
        "schema_version", "run_id", "source_instance", "manager_exit_status",
        "session_terminal_reason", "terminal_monotonic_raw_ns",
        "successor_bundle_sha256", "hard_deadline_exhausted", "fatal_reason",
    }), "manager terminal")
    terminal_time = _positive(
        terminal["terminal_monotonic_raw_ns"], "manager terminal time",
    )
    if (terminal["schema_version"] != 1 or terminal["run_id"] != pins["run_id"] or
            terminal["source_instance"] != pins["source_instance"] or
            type(terminal["manager_exit_status"]) is not int or
            terminal["manager_exit_status"] != 0 or
            terminal["session_terminal_reason"] != "acknowledgements_complete" or
            terminal["successor_bundle_sha256"] != successor_sha or
            terminal["hard_deadline_exhausted"] is not False or
            terminal["fatal_reason"] is not None or terminal_time < epoch1_active or
            terminal_time >= deadline):
        _fail("manager terminal does not prove a successful pre-deadline exit")


def _activation_events(root: Path, *, epoch1_digest: str, successor_sha: str,
                       decision: int) -> int:
    rows = _jsonl(_raw_path(root, "activation-events.jsonl"), "activation events")
    required = frozenset({"replica_id", "epoch_number", "epoch_digest", "successor_bundle_sha256", "monotonic_raw_ns"})
    observed: set[int] = set()
    latest = decision
    for row in rows:
        _exact_keys(row, required, "activation event")
        replica = _replica(row["replica_id"], "activation replica")
        when = _positive(row["monotonic_raw_ns"], "activation time")
        if (replica in observed or row["epoch_number"] != 1 or row["epoch_digest"] != epoch1_digest or
                row["successor_bundle_sha256"] != successor_sha or when < decision):
            _fail("activation evidence is duplicate, incomplete, or unbound")
        observed.add(replica)
        latest = max(latest, when)
    if observed != _REPLICA_IDS:
        _fail("activation evidence does not contain all 31 replicas")
    return latest


def _measurement_and_commits(root: Path, *, activation_complete: int, decision: int,
                             deadline: int, authoritative_replica: int) -> dict[str, object]:
    window_raw = _read_regular(_raw_path(root, "measurement-window.json"), 16 * 1024, "measurement window")
    window = _json_object(window_raw, "measurement window")
    _exact_keys(window, frozenset({"schema_version", "start_monotonic_raw_ns", "end_monotonic_raw_ns"}), "measurement window")
    start = _positive(window["start_monotonic_raw_ns"], "measurement start")
    end = _positive(window["end_monotonic_raw_ns"], "measurement end")
    if window["schema_version"] != 1 or start < activation_complete or start < decision or end <= start or end > deadline:
        _fail("measurement window is outside the activated hard-deadline interval")
    rows = _jsonl(_raw_path(root, "commit-events.jsonl"), "commit events")
    required = frozenset({"block_height", "block_hash", "transaction_count", "monotonic_raw_ns", "source_replica_id", "authoritative"})
    commits: dict[int, str] = {}
    transactions = 0
    previous_height = 0
    previous_time = 0
    for row in rows:
        _exact_keys(row, required, "commit event")
        height = _positive(row["block_height"], "commit height")
        block_hash = _hex(row["block_hash"], "commit hash")
        count = _positive(row["transaction_count"], "commit transaction count")
        when = _positive(row["monotonic_raw_ns"], "commit time")
        source = _replica(row["source_replica_id"], "commit source replica")
        if row["authoritative"] is not True or source != authoritative_replica:
            _fail("commit evidence is not from the pinned authoritative source")
        if not (start <= when <= end):
            _fail("commit evidence is outside the measurement window")
        if height in commits:
            qualifier = "conflicting" if commits[height] != block_hash else "duplicate"
            _fail(f"commit evidence repeats one height with a {qualifier} hash")
        if height <= previous_height or when < previous_time:
            _fail("commit evidence height or time regressed")
        commits[height] = block_hash
        previous_height = height
        previous_time = when
        transactions += count
    if not commits:
        _fail("commit source lacks a unique authoritative commit")
    return {"commit_count": len(commits), "transaction_count": transactions,
            "window_start_monotonic_raw_ns": start, "window_end_monotonic_raw_ns": end}


def _quota_and_cleanup(root: Path, *, arm: str, quota_profile_sha256: str,
                       window_start: int, window_end: int) -> None:
    raw = _read_regular(_raw_path(root, "quota-evidence.json"), 128 * 1024, "quota evidence")
    quota = _json_object(raw, "quota evidence")
    _exact_keys(quota, frozenset({"schema_version", "quota_profile_sha256", "assignments"}), "quota evidence")
    assignments = quota["assignments"]
    if (quota["schema_version"] != 1 or quota["quota_profile_sha256"] != quota_profile_sha256 or
            not isinstance(assignments, list) or len(assignments) != 31):
        _fail("quota evidence has no exact N31 assignment")
    seen: set[int] = set()
    for row in assignments:
        if not isinstance(row, dict):
            _fail("quota assignment is not an object")
        _exact_keys(row, frozenset({"replica_id", "capacity_class", "cpu_quota_percent", "scope", "samples"}), "quota assignment")
        replica = _replica(row["replica_id"], "quota replica")
        expected_class = "slow" if replica < 6 else "fast"
        expected_quota = 25 if replica < 6 else 100
        if (replica in seen or row["capacity_class"] != expected_class or
                row["cpu_quota_percent"] != expected_quota):
            _fail("quota assignment differs from the frozen N31 CPU contract")
        if not isinstance(row["scope"], str) or not row["scope"] or not isinstance(row["samples"], list) or not row["samples"]:
            _fail("quota assignment lacks scope or samples")
        previous_time = -1
        previous_usage = -1
        previous_throttled = -1
        for sample in row["samples"]:
            if not isinstance(sample, dict):
                _fail("quota sample is not an object")
            _exact_keys(sample, frozenset({"monotonic_raw_ns", "usage_usec", "throttled_usec"}), "quota sample")
            sample_time = _positive(sample["monotonic_raw_ns"], "quota sample time")
            usage = sample["usage_usec"]
            throttled = sample["throttled_usec"]
            if (type(usage) is not int or usage < 0 or
                    type(throttled) is not int or throttled < 0):
                _fail("quota sample counter is not uint64")
            if (sample_time <= previous_time or usage < previous_usage or
                    throttled < previous_throttled):
                _fail("quota samples are not strictly timed with nonregressing counters")
            previous_time = sample_time
            previous_usage = usage
            previous_throttled = throttled
        if (row["samples"][0]["monotonic_raw_ns"] > window_start or
                row["samples"][-1]["monotonic_raw_ns"] < window_end):
            _fail("quota samples do not cover the measurement window")
        seen.add(replica)
    if seen != _REPLICA_IDS:
        _fail("quota evidence misses an N31 replica")
    if arm not in _ARMS:
        _fail("quota evidence arm is invalid")

    cleanup_raw = _read_regular(_raw_path(root, "cleanup.json"), 64 * 1024, "cleanup")
    cleanup = _json_object(cleanup_raw, "cleanup")
    _exact_keys(cleanup, frozenset({"schema_version", "replica_ids", "all_scopes_removed", "all_processes_stopped"}), "cleanup")
    if (cleanup["schema_version"] != 1 or not isinstance(cleanup["replica_ids"], list) or
            set(cleanup["replica_ids"]) != _REPLICA_IDS or any(type(x) is not int for x in cleanup["replica_ids"]) or
            cleanup["all_scopes_removed"] is not True or cleanup["all_processes_stopped"] is not True):
        _fail("cleanup does not prove all N31 processes and quota scopes ended")


def validate_operator_capacity_raw(
    root: Path, *, authority_paths: Mapping[str, Path], pins: Mapping[str, object],
) -> dict[str, object]:
    """Check the prospective W18 schema; never assert raw replay or a claim."""
    try:
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            _fail("output root is missing or unsafe")
        if set(authority_paths) != {
            "stage_a_wire", "stage_b_wire", "successor_bundle", "consumption_record",
            "stage_a_verifier_receipt", "stage_b_verifier_receipt",
        }:
            _fail("authority paths differ from the consumption-audit contract")
        expected_pin_keys = set(_PIN_KEYS) | set(_RAW_ONLY_PIN_KEYS)
        if set(pins) != expected_pin_keys:
            _fail("prospective schema pins differ from the exact contract")
        # The component auditor deliberately has a closed pin schema.  Keep the
        # W18 prospective-only pins outside that narrower component boundary.
        audit_pins = {key: pins[key] for key in _PIN_KEYS}
        chain = audit_consumption_chain(**authority_paths, pins=audit_pins)
        arm = pins.get("arm")
        epoch0 = pins.get("epoch0_consensus_digest")
        decision = pins.get("decision_monotonic_raw_ns")
        deadline = pins.get("hard_deadline_monotonic_raw_ns")
        if arm not in _ARMS or not isinstance(epoch0, str):
            _fail("authority pins lack a supported arm or Epoch-0 digest")
        decision = _positive(decision, "pinned decision time")
        deadline = _positive(deadline, "pinned hard deadline")
        quota_profile_sha256 = _hex(pins.get("quota_profile_sha256"), "pinned quota profile")
        authoritative_replica = _replica(
            pins.get("authoritative_replica_id"), "pinned authoritative replica",
        )
        roles, roles_sha = _roles(root, arm=arm, epoch0_digest=epoch0)
        epoch1 = _hex(roles["epoch1_digest"], "roles Epoch-1 digest")
        epoch1_active = _manager_events(
            root, epoch0_digest=epoch0, epoch1_digest=epoch1,
            roles_sha=roles_sha, decision=decision,
        )
        _manager_terminal(
            root, pins=pins, successor_sha=str(chain["successor_bundle_sha256"]),
            epoch1_active=epoch1_active, deadline=deadline,
        )
        activation_complete = _activation_events(
            root, epoch1_digest=epoch1, successor_sha=str(chain["successor_bundle_sha256"]), decision=decision,
        )
        commits = _measurement_and_commits(root, activation_complete=activation_complete,
                                            decision=decision, deadline=deadline,
                                            authoritative_replica=authoritative_replica)
        _quota_and_cleanup(
            root, arm=arm, quota_profile_sha256=quota_profile_sha256,
            window_start=int(commits["window_start_monotonic_raw_ns"]),
            window_end=int(commits["window_end_monotonic_raw_ns"]),
        )
        return {
            "verdict": "PROSPECTIVE_SCHEMA_VALID_NO_RAW_REPLAY_NO_CLAIM",
            "claim_eligible": False,
            "figure_eligible": False,
            "campaign_eligible": False,
            "arm": arm,
            "epoch1_digest": epoch1,
            "roles_sha256": roles_sha,
            "activation_complete_monotonic_raw_ns": activation_complete,
            **commits,
            "claim_boundary": (
                "Prospective schema check over derived artifacts only. Native structured-event "
                "bytes, producer identity, raw replay, matched pairs, campaign acceptance, "
                "figures, and thesis claims remain unverified."
            ),
        }
    except (ConsumptionAuditError, OperatorCapacityRawValidationError, OSError, TypeError, ValueError) as exc:
        return {
            "verdict": "PROSPECTIVE_SCHEMA_REJECTED_NO_RAW_REPLAY_NO_CLAIM",
            "claim_eligible": False,
            "figure_eligible": False,
            "campaign_eligible": False,
            "detail": str(exc) or type(exc).__name__,
        }
