"""Fail-closed, byte-only replay for the prospective W19 v8 fixed-E0 arm.

This is deliberately a component validator.  It parses eight raw JSONL
streams and the actor-one fault log, but has no run-root inventory and no
fixed-arm plan/driver authority.  A passing result is therefore never launch,
claim, figure, or paired-arm authority.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
_RAW_SPEC = importlib.util.spec_from_file_location("w19_v8_fixed_raw_parser", HERE / "sustained_role_v8_raw_replay.py")
assert _RAW_SPEC and _RAW_SPEC.loader
raw = importlib.util.module_from_spec(_RAW_SPEC)
_RAW_SPEC.loader.exec_module(raw)

_REPLICAS = tuple(range(7))
_SOURCES = ("adaptive-manager", *(f"replica-{replica}" for replica in _REPLICAS))
_NS_32 = 32_000_000_000
_NS_72 = 72_000_000_000
_NS_82 = 82_000_000_000
_OBSERVED_FIELDS = frozenset({"block_height", "block_hash", "parent_hash", "transaction_count", "commit_batch_index"})
_COMMITTED_FIELDS = frozenset({"block_height", "block_hash", "parent_hash", "transaction_count", "designated_observer", "decision_proof", "view_generation", "commit_batch_index"})
_PROOF_FIELDS = frozenset({"epoch_number", "tree_id", "epoch_digest", "block_hash"})


class V8FixedE0RawReplayError(ValueError):
    """Raw evidence does not prove the narrow fixed-E0 component contract."""


def _fail(message: str) -> None:
    raise V8FixedE0RawReplayError(message)


def _object(raw_bytes: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise V8FixedE0RawReplayError(f"{label} is not strict JSON") from exc
    if not isinstance(value, Mapping):
        _fail(f"{label} is not an object")
    return value


def _hex64(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        _fail(f"{label} is not a lower-case SHA-256")
    return value


def _timestamp(event: Mapping[str, Any], label: str) -> int:
    value = event.get("source_monotonic_ns")
    if type(value) is not int or value < 0:
        _fail(f"{label} lacks a CLOCK_MONOTONIC_RAW timestamp")
    return value


def _commit(payload: object, *, authoritative: bool, label: str) -> Mapping[str, Any]:
    fields = _COMMITTED_FIELDS if authoritative else _OBSERVED_FIELDS
    if not isinstance(payload, Mapping) or frozenset(payload) != fields:
        _fail(f"{label} payload schema drifted")
    if type(payload["block_height"]) is not int or payload["block_height"] <= 0:
        _fail(f"{label} block height is invalid")
    _hex64(payload["block_hash"], f"{label} block hash")
    if payload["parent_hash"] is not None:
        _hex64(payload["parent_hash"], f"{label} parent hash")
    if (type(payload["transaction_count"]) is not int or payload["transaction_count"] not in (0, 1) or
            type(payload["commit_batch_index"]) is not int or payload["commit_batch_index"] < 0):
        _fail(f"{label} exceeds the one-command maximum")
    if authoritative:
        if type(payload["designated_observer"]) is not bool or type(payload["view_generation"]) is not int or payload["view_generation"] < 0:
            _fail(f"{label} authority fields are invalid")
        proof = payload["decision_proof"]
        if not isinstance(proof, Mapping) or frozenset(proof) != _PROOF_FIELDS or proof["block_hash"] != payload["block_hash"]:
            _fail(f"{label} decision proof schema drifted")
        if type(proof["epoch_number"]) is not int or type(proof["tree_id"]) is not int:
            _fail(f"{label} decision proof counters are invalid")
        _hex64(proof["epoch_digest"], f"{label} decision proof digest")
    return payload


def _cleanup(cleanup_raw: bytes, *, run_id: str) -> None:
    receipt = _object(cleanup_raw, "cleanup receipt")
    rows = receipt.get("processes")
    if (frozenset(receipt) != {"schema_version", "run_id", "complete", "processes"} or
            receipt.get("schema_version") != 1 or receipt.get("run_id") != run_id or receipt.get("complete") is not True or
            not isinstance(rows, list) or len(rows) != len(_SOURCES)):
        _fail("cleanup receipt does not prove eight clean exits")
    source_ids: set[str] = set()
    for row in rows:
        if (not isinstance(row, Mapping) or frozenset(row) != {"source_id", "pid", "pgid", "returncode", "termination"} or
                row.get("source_id") not in _SOURCES or row.get("source_id") in source_ids or
                row.get("returncode") != 0 or row.get("termination") != "clean-exit"):
            _fail("cleanup receipt does not prove eight unique clean exits")
        source_ids.add(row["source_id"])
    if source_ids != set(_SOURCES):
        _fail("cleanup receipt source set differs from the eight raw sources")


def _anchor(streams: Mapping[str, Sequence[Mapping[str, Any]]], logs: Sequence[bytes], *, e0_digest: str) -> tuple[int, Mapping[str, Any], int]:
    opportunities: list[tuple[tuple[str, ...], Mapping[str, Any]]] = []
    for replica in _REPLICAS:
        source = f"replica-{replica}"
        candidates = [event for event in streams[source] if event.get("event_type") == "fault.contribution_opportunity"]
        if replica != 1 and candidates:
            _fail("fault opportunity appears outside physical actor 1")
        if replica != 1 and raw._markers(logs[replica]):
            _fail("KAURI_FAULT marker appears outside physical actor 1")
        opportunities.extend((raw._opportunity(event), event) for event in candidates)
    markers = [raw._marker_identity(marker) for marker in raw._markers(logs[1])]
    identities = [identity for identity, _event in opportunities]
    if not identities or len(identities) != len(set(identities)) or sorted(identities) != sorted(markers):
        _fail("fixed-E0 actor-1 opportunities do not biject unique native markers")
    identity, event = opportunities[0]
    if (identity[1] != "0" or identity[2] != "4" or identity[3] != e0_digest or
            identity[17:19] != ("internal", "0") or identity[9] != "omit_aggregate"):
        _fail("anchor is not the exact physical actor-1 E0 tree-4 internal omission")
    decision_ns = int(identity[10])
    if _timestamp(event, "physical omission") < decision_ns:
        _fail("physical omission source clock precedes native decision clock")
    roles = {0: "leaf", 2: "leaf", 3: "leaf", 4: "internal", 5: "internal", 6: "internal"}
    late_count = 0
    for candidate, candidate_event in opportunities:
        tree_id = int(candidate[2])
        decision = int(candidate[10])
        role = roles.get(tree_id)
        action = "omit_aggregate" if role == "internal" else "omit_direct_vote"
        if (candidate[1] != "0" or candidate[3] != e0_digest or role is None or
                candidate[17] != role or candidate[9] != action or decision < decision_ns or
                _timestamp(candidate_event, "physical omission") < decision):
            _fail("fixed-E0 physical omission leaves the frozen actor-1 E0 role schedule")
        if decision_ns + _NS_32 <= decision < decision_ns + _NS_72:
            late_count += 1
    if late_count == 0:
        _fail("fixed-E0 arm has no physical omission in A+32..A+72")
    return decision_ns, event, late_count


def _reject_successor_authority(streams, *, run_id, e0_digest, scheduled_end_ns):
    no_ops = []
    for source, events in streams.items():
        for event in events:
            kind = event.get("event_type")
            if kind in {"epoch.command_committed", "epoch.activated"}:
                _fail(f"{source} contains forbidden successor authority")
            if source == "adaptive-manager" and kind in {"adaptive_v2.convergence_started", "adaptive_v2.selection_decided"}:
                _fail("fixed manager contains forbidden adaptive transition evidence")
            if kind == "adaptive_v2_session_terminal":
                p = event.get("payload")
                if not isinstance(p, Mapping):
                    _fail("fixed manager no-op payload is malformed")
                cutoff = p.get("baseline_evidence_cutoff")
                expected = {"cycle_ordinal": 0, "policy_intent": "fault_containment", "outcome": "no_op",
                    "reason": "explicit_no_op", "transition_artifact_id": "scheduled-fixed-e0-control/" + run_id,
                    "predecessor_epoch_number": 0, "predecessor_epoch_digest": e0_digest,
                    "successor_epoch_number": None, "successor_epoch_digest": None,
                    "command_payload_digest": None, "winning_activation": None,
                    "evidence_window_activation_generation": 1, "baseline_evidence_cutoff": cutoff,
                    "current_evidence_cutoff": cutoff, "controller_failure": None}
                if source != "adaptive-manager" or p != expected or type(cutoff) is not int or cutoff <= 0 or _timestamp(event, "fixed no-op") < scheduled_end_ns:
                    _fail("fixed manager terminal is not the exact scheduled no-op")
                no_ops.append(event)
    if len(no_ops) != 1:
        _fail("fixed manager lacks exactly one scheduled no-op terminal")


def _manager_control(events: Sequence[Mapping[str, Any]], *, run_id: str,
                     profile_sha256: str, e0_digest: str, start_ns: int,
                     end_ns: int, anchor_ns: int) -> None:
    expected = {"run_id": run_id, "profile_sha256": profile_sha256,
                "epoch_zero_digest": e0_digest,
                "window_start_monotonic_ns": start_ns,
                "window_end_monotonic_ns": end_ns}
    observations: list[int] = []
    terminal_ns: int | None = None
    for event in events:
        event_type = event.get("event_type")
        if event_type not in {"scheduled_fixed_e0_control.observation",
                              "scheduled_fixed_e0_control.terminal"}:
            continue
        if event.get("payload") != expected:
            _fail("native fixed-E0 manager observation differs from sealed control binding")
        timestamp = _timestamp(event, "fixed-E0 manager control")
        if event_type == "scheduled_fixed_e0_control.observation":
            if terminal_ns is not None or not start_ns <= timestamp < end_ns:
                _fail("native fixed-E0 observation is outside the live control interval")
            observations.append(timestamp)
        elif terminal_ns is not None or timestamp < end_ns:
            _fail("native fixed-E0 terminal is duplicated or precedes scheduled end")
        else:
            terminal_ns = timestamp
    if (not observations or observations[0] > anchor_ns or
            not any(anchor_ns + _NS_32 <= timestamp < anchor_ns + _NS_72
                    for timestamp in observations) or terminal_ns is None):
        _fail("native fixed-E0 manager lacks observation through the metric window and terminal")


def _metric(streams: Mapping[str, Sequence[Mapping[str, Any]]], *, start_ns: int, end_ns: int, e0_digest: str) -> int:
    authoritative: dict[tuple[int, str], tuple[object, object]] = {}
    observed: dict[tuple[int, str], dict[str, tuple[object, object]]] = {}
    heights: dict[int, str] = {}
    for source, events in streams.items():
        for event in events:
            timestamp = _timestamp(event, f"{source} metric event")
            if not start_ns <= timestamp < end_ns:
                continue
            event_type = event.get("event_type")
            if event_type == "block.commit_observed":
                payload = _commit(event.get("payload"), authoritative=False, label=f"{source} observed commit")
                key = (payload["block_height"], payload["block_hash"])
                if heights.setdefault(payload["block_height"], payload["block_hash"]) != payload["block_hash"]:
                    _fail("conflicting E0 commit hashes at one height")
                if source in observed.setdefault(key, {}):
                    _fail(f"{source} repeats observed E0 commit identity")
                observed[key][source] = (payload["parent_hash"], payload["transaction_count"])
            elif event_type == "block.committed":
                payload = _commit(event.get("payload"), authoritative=True, label=f"{source} authoritative commit")
                if payload["designated_observer"] is not (source == "replica-2"):
                    _fail("designated observer flag disagrees with configured replica 2")
                proof = payload["decision_proof"]
                if proof["epoch_number"] != 0 or proof["epoch_digest"] != e0_digest:
                    _fail("committed metric block is not exact E0 authority")
                if source != "replica-2":
                    continue
                key = (payload["block_height"], payload["block_hash"])
                if heights.setdefault(payload["block_height"], payload["block_hash"]) != payload["block_hash"]:
                    _fail("conflicting E0 commit hashes at one height")
                if key in authoritative:
                    _fail("replica 2 repeats authoritative E0 commit identity")
                authoritative[key] = (payload["parent_hash"], payload["transaction_count"])
    if not authoritative:
        _fail("measurement window lacks a designated authoritative E0 block.committed event")
    # Only replicas witness commits; the manager is a separate eighth raw source.
    expected_sources = {f"replica-{replica}" for replica in _REPLICAS}
    for key, metadata in authoritative.items():
        if any(value != metadata for value in observed.get(key, {}).values()):
            _fail("E0 observed metadata contradicts authoritative metadata")
    count = sum(metadata[1] == 1 and observed.get(key) == {source: metadata for source in expected_sources} for key, metadata in authoritative.items())
    if count == 0: _fail("no all-seven common one-command commit in metric window")
    return count


def replay_fixed_e0_raw(*, run_id: str, e0_digest: str, profile_sha256: str,
                        scheduled_start_monotonic_ns: int,
                        scheduled_end_monotonic_ns: int, manager_raw: bytes,
                        replica_raw: Sequence[bytes], replica_logs: Sequence[bytes],
                        cleanup_raw: bytes, abort_raw: bytes | None = None,
                        terminal_raw: bytes | None = None) -> dict[str, Any]:
    """Replay narrow fixed-E0 raw evidence; successful output remains no-launch.

    ``abort_raw`` and ``terminal_raw`` are supplied by a higher-level sealed
    inventory.  Any such marker excludes this component result.  Their
    absence is not itself a root-inventory proof and is listed as a missing
    closure below.
    """
    if not isinstance(run_id, str) or not run_id or not isinstance(e0_digest, str) or not isinstance(profile_sha256, str):
        _fail("run ID, E0 digest, and profile digest are required")
    _hex64(e0_digest, "E0 digest")
    _hex64(profile_sha256, "v8 profile digest")
    start_ns, end_ns = scheduled_start_monotonic_ns, scheduled_end_monotonic_ns
    if (type(start_ns) is not int or type(end_ns) is not int or start_ns <= 0 or
            end_ns - start_ns < _NS_82):
        _fail("fixed-E0 scheduled window is not the frozen 82-second exposure")
    if len(replica_raw) != 7 or len(replica_logs) != 7:
        _fail("exact N=7 replica raw streams and logs are required")
    if abort_raw is not None or terminal_raw is not None:
        _fail("abort or terminal marker excludes a fixed-E0 replay result")
    streams: dict[str, Sequence[Mapping[str, Any]]] = {
        "adaptive-manager": raw._stream(manager_raw, run_id=run_id, source_id="adaptive-manager")
    }
    streams.update({f"replica-{replica}": raw._stream(replica_raw[replica], run_id=run_id, source_id=f"replica-{replica}") for replica in _REPLICAS})
    instances = [events[0]["source_instance"] for events in streams.values()]
    if len(set(instances)) != len(_SOURCES):
        _fail("eight raw sources do not bind eight unique source instances")
    _reject_successor_authority(streams, run_id=run_id, e0_digest=e0_digest, scheduled_end_ns=end_ns)
    anchor_ns, anchor_event, late_omission_count = _anchor(streams, replica_logs, e0_digest=e0_digest)
    if not start_ns <= anchor_ns <= start_ns + 10_000_000_000 or end_ns < anchor_ns + _NS_72:
        _fail("fixed-E0 physical anchor is outside the sealed scheduled window")
    _manager_control(streams["adaptive-manager"], run_id=run_id, profile_sha256=profile_sha256,
                     e0_digest=e0_digest, start_ns=start_ns, end_ns=end_ns, anchor_ns=anchor_ns)
    horizon_ns = anchor_ns + _NS_72
    # All sources must retain structured coverage through the full fixed-E0 horizon.
    for source, events in streams.items():
        if not any(_timestamp(event, source) >= horizon_ns for event in events):
            _fail(f"{source} lacks raw coverage through A+72 seconds")
    _cleanup(cleanup_raw, run_id=run_id)
    count = _metric(streams, start_ns=anchor_ns + _NS_32, end_ns=horizon_ns, e0_digest=e0_digest)
    return {
        "verdict": "COMPONENT_VALIDATED_NO_LAUNCH",
        "arm": "fixed_e0",
        "clock": "CLOCK_MONOTONIC_RAW",
        "anchor_monotonic_ns": anchor_ns,
        "measurement_window": {"start_monotonic_ns": anchor_ns + _NS_32, "end_monotonic_ns": horizon_ns},
        "e0_digest": e0_digest,
        "profile_sha256": profile_sha256,
        "anchor_source": {"source_id": anchor_event["source_id"], "source_sequence": anchor_event["source_sequence"], "source_instance": anchor_event["source_instance"]},
        "late_e0_physical_omission_count": late_omission_count,
        "common_authoritative_e0_commit_count": count,
        "claim_eligible": False,
        "figure_eligible": False,
        "missing_fixed_input_closure": [
            "canonical fixed-v8 full-input plan and native manager argv",
            "immutable replica-local one-command driver descriptor",
            "archived E0 tree, identity-helper, config, executable, and source-instance bindings",
            "sealed root inventory proving marker absence and external approval/host/build authority",
        ],
    }
