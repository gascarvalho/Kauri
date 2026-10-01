"""Byte-only, no-claim v8 native fault-evidence parser.

Inputs are raw bytes already selected by a sealed-root replay.  This module
does not accept a run directory, artifact descriptors, or a producer verdict.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

_MARKER = re.compile(r"(?:^|\s)KAURI_FAULT\s+(?P<body>[^\n]+)")


class V8RawReplayError(ValueError): pass


def _object(raw: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result: raise V8RawReplayError(f"{label} has duplicate JSON key")
            result[key] = value
        return result
    try: value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise V8RawReplayError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict): raise V8RawReplayError(f"{label} is not an object")
    return value


def _stream(raw: bytes, *, run_id: str, source_id: str) -> list[dict[str, Any]]:
    expected_kind = "adaptation_manager" if source_id == "adaptive-manager" else "replica"
    events: list[dict[str, Any]] = []; prior_sequence = 0; prior_ns = -1; instance: str | None = None
    for line in raw.splitlines():
        event = _object(line, source_id)
        fields = {"event_schema_version", "run_id", "source_kind", "source_id", "source_instance", "source_sequence", "source_monotonic_ns", "event_type", "payload"}
        if set(event) != fields or event.get("event_schema_version") != 1 or event.get("run_id") != run_id or event.get("source_id") != source_id or event.get("source_kind") != expected_kind:
            raise V8RawReplayError(f"{source_id} envelope differs from this run")
        if not isinstance(event.get("source_instance"), str) or not event["source_instance"]:
            raise V8RawReplayError(f"{source_id} source instance is invalid")
        if instance is None: instance = event["source_instance"]
        elif instance != event["source_instance"]: raise V8RawReplayError(f"{source_id} mixes source instances")
        if type(event.get("source_sequence")) is not int or event["source_sequence"] != prior_sequence + 1:
            raise V8RawReplayError(f"{source_id} sequence is not contiguous")
        if type(event.get("source_monotonic_ns")) is not int or event["source_monotonic_ns"] < prior_ns:
            raise V8RawReplayError(f"{source_id} raw clock regressed")
        prior_sequence, prior_ns = event["source_sequence"], event["source_monotonic_ns"]
        event["_line_sha256"] = hashlib.sha256(line).hexdigest(); events.append(event)
    if not events: raise V8RawReplayError(f"{source_id} raw stream is empty")
    return events


def _markers(raw: bytes) -> list[dict[str, str]]:
    try: text = raw.decode("utf-8")
    except UnicodeDecodeError as exc: raise V8RawReplayError("fault log is not UTF-8") from exc
    result = []
    for match in _MARKER.finditer(text):
        row: dict[str, str] = {}
        for token in match.group("body").split():
            if "=" not in token: break
            key, value = token.split("=", 1)
            if not key or not value or key in row: raise V8RawReplayError("KAURI_FAULT marker is malformed")
            row[key] = value
        if row: result.append(row)
    return result


def _opportunity(event: Mapping[str, Any]) -> tuple[str, ...]:
    payload = event.get("payload"); proposal = payload.get("proposal") if isinstance(payload, Mapping) else None
    required = {
        "actor", "proposal", "physical_role", "parent_replica",
        "authenticated_proposal_source_replica", "expected_message_type", "cohort",
        "diagnostic_window", "window_start_monotonic_ns", "window_end_monotonic_ns",
        "decision_monotonic_ns", "contribution_ordinal", "role_contribution_ordinal",
        "scheduled_action", "responsive_omission_period", "fault_threshold",
        "hard_actor_count", "responsive_degraded_actor_count", "fault_mode", "view_generation",
    }
    if (event.get("event_type") != "fault.contribution_opportunity" or not isinstance(payload, Mapping) or not isinstance(proposal, Mapping) or
            set(payload) != required or set(proposal) != {"epoch_number", "tree_id", "epoch_digest", "block_hash"} or
            payload.get("actor") != 1 or payload.get("fault_mode") != "role_scoped_persistent_selected_omission_v1" or
            payload.get("cohort") != "hard" or payload.get("hard_actor_count") != 1 or payload.get("responsive_degraded_actor_count") != 0 or
            payload.get("fault_threshold") != 2 or payload.get("responsive_omission_period") != 0 or payload.get("contribution_ordinal") != 0 or
            payload.get("role_contribution_ordinal") != 0 or
            payload.get("authenticated_proposal_source_replica") != payload.get("parent_replica")):
        raise V8RawReplayError("fault opportunity differs from frozen one-actor schedule")
    if payload.get("physical_role") == "internal": expected = ("aggregate_relay", "omit_aggregate")
    elif payload.get("physical_role") == "leaf": expected = ("direct_vote", "omit_direct_vote")
    else: raise V8RawReplayError("fault opportunity role is unsupported")
    if (payload.get("expected_message_type"), payload.get("scheduled_action")) != expected: raise V8RawReplayError("fault opportunity action differs from role")
    if (type(proposal["epoch_number"]) is not int or proposal["epoch_number"] not in (0, 1) or
            type(proposal["tree_id"]) is not int or proposal["tree_id"] not in range(7) or
            not all(isinstance(proposal[key], str) and re.fullmatch(r"[0-9a-f]{64}", proposal[key])
                    for key in ("epoch_digest", "block_hash")) or
            type(payload["decision_monotonic_ns"]) is not int or
            type(payload["window_start_monotonic_ns"]) is not int or type(payload["window_end_monotonic_ns"]) is not int or
            not payload["window_start_monotonic_ns"] <= payload["decision_monotonic_ns"] < payload["window_end_monotonic_ns"]):
        raise V8RawReplayError("fault opportunity proposal is malformed")
    return tuple(str(value) for value in (
        payload["fault_mode"], proposal["epoch_number"], proposal["tree_id"], proposal["epoch_digest"],
        proposal["block_hash"], payload["diagnostic_window"], payload["window_start_monotonic_ns"],
        payload["window_end_monotonic_ns"], payload["actor"], payload["scheduled_action"],
        payload["decision_monotonic_ns"], payload["cohort"], payload["hard_actor_count"],
        payload["responsive_degraded_actor_count"], payload["fault_threshold"],
        payload["responsive_omission_period"], payload["contribution_ordinal"],
        payload["physical_role"], payload["role_contribution_ordinal"],
        payload["authenticated_proposal_source_replica"],
    ))


def _marker_identity(marker: Mapping[str, str]) -> tuple[str, ...]:
    keys = (
        "fault", "proposal_epoch", "proposal_tree", "proposal_epoch_digest", "proposal_block_hash",
        "window", "window_start_monotonic_ns", "window_end_monotonic_ns", "actor", "action",
        "monotonic_ns", "cohort", "hard_actor_count", "responsive_degraded_actor_count",
        "fault_threshold", "responsive_omission_period", "contribution_ordinal", "contribution_role",
        "role_contribution_ordinal", "authenticated_proposal_source_replica",
    )
    if any(key not in marker for key in keys): raise V8RawReplayError("KAURI_FAULT marker lacks identity fields")
    if (marker.get("max_omissions_per_proposal") != "1" or marker["fault_threshold"] != "2" or
            not re.fullmatch(r"[0-9a-f]{64}", marker["proposal_epoch_digest"]) or
            not re.fullmatch(r"[0-9a-f]{64}", marker["proposal_block_hash"])):
        raise V8RawReplayError("KAURI_FAULT marker differs from frozen one-omission schedule")
    return tuple(marker[key] for key in keys)


def replay_fault_evidence(*, run_id: str, manager_raw: bytes, replica_raw: Sequence[bytes], replica_logs: Sequence[bytes]) -> dict[str, Any]:
    """Parse eight streams and markers; return untrusted E1 candidates only.

    Raw evidence alone cannot prove that an E1 digest or tree is the signed,
    active configuration.  A sealed-root replay must perform that join.
    """
    if not isinstance(run_id, str) or not run_id or len(replica_raw) != 7 or len(replica_logs) != 7:
        raise V8RawReplayError("exact N=7 raw/log inputs are required")
    streams = {"adaptive-manager": _stream(manager_raw, run_id=run_id, source_id="adaptive-manager")}
    streams.update({f"replica-{i}": _stream(replica_raw[i], run_id=run_id, source_id=f"replica-{i}") for i in range(7)})
    opportunities: list[tuple[tuple[str, ...], Mapping[str, Any]]] = []
    for replica in range(7):
        source = f"replica-{replica}"; fault_events = [event for event in streams[source] if event["event_type"] == "fault.contribution_opportunity"]
        if replica != 1 and fault_events: raise V8RawReplayError("fault opportunity appears outside replica 1")
        if replica != 1 and _markers(replica_logs[replica]): raise V8RawReplayError("KAURI_FAULT marker appears outside replica 1")
        opportunities.extend((_opportunity(event), event) for event in fault_events)
    marker_rows = _markers(replica_logs[1])
    marker_ids = [_marker_identity(marker) for marker in marker_rows]
    ids = [identity for identity, _event in opportunities]
    if sorted(ids) != sorted(marker_ids) or len(ids) != len(set(ids)): raise V8RawReplayError("actor-1 opportunities do not biject KAURI_FAULT markers")
    if not opportunities: raise V8RawReplayError("actor 1 has no physical fault opportunity")
    for identity, event in opportunities:
        if event["source_monotonic_ns"] < int(identity[10]):
            raise V8RawReplayError("fault opportunity source clock precedes native decision clock")
    anchor_identity, anchor = opportunities[0]
    if not (anchor_identity[1] == "0" and anchor_identity[2] == "4" and
            anchor_identity[17:19] == ("internal", "0") and anchor_identity[9] == "omit_aggregate"):
        raise V8RawReplayError("first actor-1 opportunity is not the E0 tree-4 internal omission")
    anchor_decision_ns = int(anchor_identity[10])
    anchor_source_ns = anchor["source_monotonic_ns"]
    late = [(identity, event) for identity, event in opportunities
            if identity[1] == "1" and
            identity[17:19] == ("leaf", "0") and identity[9] == "omit_direct_vote" and
            anchor_decision_ns + 32_000_000_000 <= int(identity[10]) < anchor_decision_ns + 72_000_000_000]
    if not late: raise V8RawReplayError("no late E1 leaf direct-vote candidate in metric horizon")
    return {"verdict": "FAULT_EVIDENCE_COMPONENT_ONLY_NO_CLAIM",
            "anchor_decision_monotonic_ns": anchor_decision_ns, "anchor_source_monotonic_ns": anchor_source_ns,
            "anchor_source_id": anchor["source_id"], "anchor_source_sequence": anchor["source_sequence"],
            "anchor_line_sha256": anchor["_line_sha256"], "anchor_epoch_digest": anchor_identity[3],
            "anchor_tree_id": int(anchor_identity[2]),
            "late_e1_leaf_candidates_untrusted_pending_root_join": [
                {"source_id": event["source_id"], "source_sequence": event["source_sequence"],
                 "decision_monotonic_ns": int(identity[10]), "source_monotonic_ns": event["source_monotonic_ns"],
                 "line_sha256": event["_line_sha256"],
                 "epoch_digest": identity[3], "tree_id": int(identity[2])}
                for identity, event in late]}
