"""Fail-closed raw-evidence validator for the prospective W19 v8 pilot.

Activation timestamps alone are never sufficient: this component validator
requires independently decoded signed bytes, seven exact command commits, the
sealed manager delivery record, and an authoritative successor commit.
"""
from __future__ import annotations

import importlib
import importlib.util
import hashlib
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("w19_v8_profile_for_validator", HERE / "sustained_role_v8_profile.py")
assert SPEC and SPEC.loader
profile = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(profile)

_COMMAND_FIELDS = frozenset({"command_block_height", "command_block_hash", "payload_digest", "predecessor_epoch_number", "predecessor_epoch_digest", "successor_epoch_number", "successor_epoch_digest", "activation_delay_blocks", "activation_height"})
_ACTIVATION_FIELDS = frozenset({"epoch_number", "tree_id", "epoch_digest", "activation_height"})
_COMMIT_FIELDS = frozenset({"block_height", "block_hash", "parent_hash", "transaction_count", "designated_observer", "decision_proof", "view_generation", "commit_batch_index"})
_COMMIT_OBSERVED_FIELDS = frozenset({"block_height", "block_hash", "parent_hash", "transaction_count", "commit_batch_index"})
_PROOF_FIELDS = frozenset({"epoch_number", "tree_id", "epoch_digest", "block_hash"})
_MANAGER_CONVERGENCE_FIELDS = frozenset({
    "replica_id", "delivery_attempt", "disposition", "identity",
    "accepted_commit_count", "accepted_activation_count",
    "required_activation_count", "canonical_payload_digest", "failure_reason",
})
_MANAGER_IDENTITY_FIELDS = frozenset({
    "predecessor_epoch_number", "predecessor_epoch_digest",
    "successor_epoch_number", "successor_epoch_digest",
    "command_payload_digest", "command_block_height", "command_block_hash",
    "activation_delay_blocks", "activation_height",
})
_MANAGER_TERMINAL_FIELDS = frozenset({
    "cycle_ordinal", "policy_intent", "outcome", "reason",
    "transition_artifact_id", "predecessor_epoch_number",
    "predecessor_epoch_digest", "successor_epoch_number",
    "successor_epoch_digest", "command_payload_digest", "winning_activation",
    "controller_failure", "evidence_window_activation_generation",
    "baseline_evidence_cutoff", "current_evidence_cutoff",
})
_MANAGER_SUCCESS_BUDGET_NS = 12_000_000_000


class V8ValidationError(ValueError): pass


def _timestamp(event: Mapping[str, object], label: str) -> int:
    value = event.get("source_monotonic_ns")
    if type(value) is not int or value < 0: raise V8ValidationError(f"{label} lacks a CLOCK_MONOTONIC_RAW timestamp")
    return value


def _sequence(event: Mapping[str, object], label: str) -> int:
    value = event.get("source_sequence")
    if type(value) is not int or value < 0: raise V8ValidationError(f"{label} lacks a source sequence")
    return value


def _hex64(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise V8ValidationError(f"{label} is not a lowercase 64-hex digest")
    return value


def _identity(payload: Mapping[str, object]) -> dict[str, object]:
    if set(payload) != profile.CONVERGENCE_PAYLOAD_FIELDS: raise V8ValidationError("convergence-start payload schema drifted")
    actual = dict(payload)
    if (type(actual["cycle_ordinal"]) is not int or actual["cycle_ordinal"] < 0 or
            type(actual["predecessor_epoch_number"]) is not int or actual["predecessor_epoch_number"] < 0 or
            type(actual["successor_epoch_number"]) is not int or actual["successor_epoch_number"] != actual["predecessor_epoch_number"] + 1 or
            type(actual["baseline_evidence_cutoff"]) is not int or actual["baseline_evidence_cutoff"] <= 0 or
            type(actual["evidence_cutoff"]) is not int or actual["evidence_cutoff"] <= actual["baseline_evidence_cutoff"] or
            not isinstance(actual["evidence_snapshot_id"], str) or not actual["evidence_snapshot_id"]):
        raise V8ValidationError("convergence-start identity is malformed")
    for field in ("predecessor_epoch_digest", "successor_epoch_digest", "command_payload_digest"): _hex64(actual[field], field)
    actual.pop("cycle_ordinal")
    return actual


def _decode_bundle(bundle_bytes: bytes, issuer_public_key: str) -> Any:
    package_root = str(HERE.parents[1]); inserted = package_root not in sys.path
    if inserted: sys.path.insert(0, package_root)
    try:
        return importlib.import_module("kauri_experiment.factorial_validation").decode_epoch_change_bundle(bundle_bytes, issuer_public_key=issuer_public_key)
    except (ImportError, OSError, ValueError) as exc:
        raise V8ValidationError(f"signed bundle independently fails to decode: {exc}") from exc
    finally:
        if inserted: sys.path.remove(package_root)


def _bundle_identity(bundle: Any) -> dict[str, object]:
    try:
        command = bundle.command
        raw = {"cycle_ordinal": 0, "predecessor_epoch_number": 0,
            "predecessor_epoch_digest": command.predecessor_epoch_digest,
            "successor_epoch_number": command.successor_epoch_number,
            "successor_epoch_digest": command.successor_epoch_digest,
            "command_payload_digest": command.payload_digest,
            "evidence_snapshot_id": bundle.evidence_snapshot_id,
            "evidence_cutoff": bundle.evidence_cutoff}
    except AttributeError as exc: raise V8ValidationError("decoded bundle lacks its exact command identity") from exc
    # The native signed bundle contains snapshot id and final cutoff, but not
    # the baseline cutoff.  Baseline is bound separately by S->R below.
    for field in ("predecessor_epoch_digest", "successor_epoch_digest", "command_payload_digest"):
        _hex64(raw[field], field)
    if (type(raw["predecessor_epoch_number"]) is not int or raw["predecessor_epoch_number"] < 0 or
            type(raw["successor_epoch_number"]) is not int or raw["successor_epoch_number"] != raw["predecessor_epoch_number"] + 1 or
            type(raw["evidence_cutoff"]) is not int or raw["evidence_cutoff"] <= 0 or
            not isinstance(raw["evidence_snapshot_id"], str) or not raw["evidence_snapshot_id"]):
        raise V8ValidationError("decoded bundle identity is malformed")
    raw.pop("cycle_ordinal")
    return raw


def _validate_n7_containment_shape(bundle: Any) -> frozenset[int]:
    """Freeze the inherited five-tree N=7 containment layout from decoded bytes."""
    try: trees = tuple(bundle.trees)
    except AttributeError as exc: raise V8ValidationError("decoded bundle lacks E1 tree definitions") from exc
    if len(trees) != 5 or tuple(tree.tree_id for tree in trees) != tuple(range(5)):
        raise V8ValidationError("decoded bundle does not contain the frozen five E1 trees")
    for tree in trees:
        try:
            members, fanout, stretch, exempt = tuple(tree.members), tree.fanout, tree.pipeline_stretch, tuple(tree.wait_exempt)
        except AttributeError as exc: raise V8ValidationError("decoded E1 tree schema is incomplete") from exc
        first_leaf = (len(members) - 2) // fanout + 1 if isinstance(fanout, int) and fanout > 0 else -1
        if (tuple(sorted(members)) != tuple(range(7)) or fanout != 2 or stretch != 2 or exempt != (1,) or members.index(1) < first_leaf):
            raise V8ValidationError("decoded E1 tree does not retain actor 1 as wait-exempt leaf")
    return frozenset(tree.tree_id for tree in trees)


def _delivery(events: Sequence[Mapping[str, object]], *, start_ns: int, start_sequence: int, bundle_sha256: str) -> tuple[int, int]:
    matches: list[tuple[int, int]] = []
    for event in events:
        if event.get("event_type") != "adaptive_v2_delivery_attempt": continue
        payload = event.get("payload")
        if (isinstance(payload, Mapping) and payload.get("delivery_attempt") == 1 and payload.get("disposition") == "enqueued" and
                type(payload.get("replica_id")) is int and payload["replica_id"] in profile.REPLICA_IDS and
                payload.get("identity") is None and payload.get("canonical_payload_digest") == bundle_sha256):
            timestamp = _timestamp(event, "delivery attempt")
            if timestamp <= start_ns or _sequence(event, "delivery attempt") <= start_sequence:
                raise V8ValidationError("delivery attempt does not follow convergence-start")
            matches.append((timestamp, _sequence(event, "delivery attempt")))
    if not matches: raise V8ValidationError("sealed manager raw lacks an exact first enqueued delivery")
    return min(matches)


def _selection_before_start(events: Sequence[Mapping[str, object]], *, anchor_ns: int, start: Mapping[str, object],
                            selection_deadline_ns: int, identity: Mapping[str, object], cycle_ordinal: int) -> None:
    """Bind R to the one native selection record which has no successor digest."""
    matches = []
    required = {"schema_version", "cycle_ordinal", "predecessor_epoch_number", "predecessor_epoch_digest",
                "baseline_cutoff", "evidence_cutoff", "evidence_snapshot_id", "snapshot_evidence_basis",
                "selection_cardinality_policy", "selected_replicas"}
    for event in events:
        if event.get("event_type") != "adaptive_v2.selection_decided": continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or set(payload) != required: continue
        same_cycle = (event.get("source_kind") == "adaptation_manager" and
                      payload.get("cycle_ordinal") == cycle_ordinal and
                      payload.get("predecessor_epoch_number") == identity["predecessor_epoch_number"] and
                      payload.get("predecessor_epoch_digest") == identity["predecessor_epoch_digest"] and
                      anchor_ns < _timestamp(event, "selection") < _timestamp(start, "convergence-start"))
        if same_cycle and (payload.get("snapshot_evidence_basis") != "exact_post_fault_path_timeout_quorum_v1" or
                           payload.get("selection_cardinality_policy") != "all_guarded_up_to_fault_bound_v1" or
                           payload.get("selected_replicas") != [1] or
                           payload.get("baseline_cutoff") != identity["baseline_evidence_cutoff"] or
                           payload.get("evidence_cutoff") != identity["evidence_cutoff"] or
                           payload.get("evidence_snapshot_id") != identity["evidence_snapshot_id"]):
            raise V8ValidationError("same-cycle manager selection contradicts frozen W19 selection")
        if (payload.get("cycle_ordinal") == cycle_ordinal and
                payload.get("predecessor_epoch_number") == identity["predecessor_epoch_number"] and
                payload.get("predecessor_epoch_digest") == identity["predecessor_epoch_digest"] and
                payload.get("baseline_cutoff") == identity["baseline_evidence_cutoff"] and
                payload.get("evidence_cutoff") == identity["evidence_cutoff"] and
                payload.get("evidence_snapshot_id") == identity["evidence_snapshot_id"] and
                event.get("source_kind") == "adaptation_manager" and
                payload.get("snapshot_evidence_basis") == "exact_post_fault_path_timeout_quorum_v1" and
                payload.get("selection_cardinality_policy") == "all_guarded_up_to_fault_bound_v1" and
                payload.get("selected_replicas") == [1] and
                anchor_ns < _timestamp(event, "selection") < _timestamp(start, "convergence-start") and
                _sequence(event, "selection") < _sequence(start, "convergence-start") and
                _timestamp(event, "selection") <= selection_deadline_ns):
            matches.append(event)
    if len(matches) != 1:
        raise V8ValidationError("convergence-start lacks one exact preceding native selection_decided record")


def _command(event: Mapping[str, object], identity: Mapping[str, object], signed_delay: int) -> Mapping[str, object] | None:
    payload = event.get("payload")
    if event.get("event_type") != "epoch.command_committed" or not isinstance(payload, Mapping): return None
    if set(payload) != _COMMAND_FIELDS: raise V8ValidationError("epoch.command_committed payload schema drifted")
    if (payload.get("predecessor_epoch_number") != identity["predecessor_epoch_number"] or payload.get("predecessor_epoch_digest") != identity["predecessor_epoch_digest"] or
            payload.get("successor_epoch_number") != identity["successor_epoch_number"] or payload.get("successor_epoch_digest") != identity["successor_epoch_digest"] or
            payload.get("payload_digest") != identity["command_payload_digest"] or type(payload.get("command_block_height")) is not int or payload["command_block_height"] <= 0 or
            payload.get("activation_delay_blocks") != signed_delay or type(payload.get("activation_height")) is not int or
            payload["activation_height"] != payload["command_block_height"] + payload["activation_delay_blocks"]): return None
    _hex64(payload.get("command_block_hash"), "command block hash")
    return payload


def _activation(event: Mapping[str, object], command: Mapping[str, object], tree_ids: frozenset[int]) -> bool:
    payload = event.get("payload")
    return (event.get("event_type") == "epoch.activated" and isinstance(payload, Mapping) and set(payload) == _ACTIVATION_FIELDS and
            payload.get("epoch_number") == command["successor_epoch_number"] and type(payload.get("tree_id")) is int and payload["tree_id"] in tree_ids and
            payload.get("epoch_digest") == command["successor_epoch_digest"] and payload.get("activation_height") == command["activation_height"])


def _strict_commit(payload: object, *, observed: bool) -> Mapping[str, object]:
    fields = _COMMIT_OBSERVED_FIELDS if observed else _COMMIT_FIELDS
    if not isinstance(payload, Mapping) or set(payload) != fields:
        raise V8ValidationError("commit payload schema drifted")
    if type(payload["block_height"]) is not int or payload["block_height"] <= 0:
        raise V8ValidationError("commit block height is invalid")
    _hex64(payload["block_hash"], "commit block hash")
    if payload["parent_hash"] is not None: _hex64(payload["parent_hash"], "commit parent hash")
    if type(payload["transaction_count"]) is not int or payload["transaction_count"] < 0 or type(payload["commit_batch_index"]) is not int or payload["commit_batch_index"] < 0:
        raise V8ValidationError("commit counters are invalid")
    if not observed:
        if type(payload["designated_observer"]) is not bool or type(payload["view_generation"]) is not int or payload["view_generation"] < 0:
            raise V8ValidationError("authoritative commit fields are invalid")
        proof = payload["decision_proof"]
        if (not isinstance(proof, Mapping) or set(proof) != _PROOF_FIELDS or type(proof["epoch_number"]) is not int or
                proof["epoch_number"] < 0 or type(proof["tree_id"]) is not int or proof["tree_id"] < 0 or
                proof["block_hash"] != payload["block_hash"]):
            raise V8ValidationError("authoritative commit decision proof drifted")
        _hex64(proof["epoch_digest"], "commit proof digest")
    return payload


def _command_block_committed(streams: Mapping[int, Sequence[Mapping[str, object]]], commands: Mapping[int, Mapping[str, object]], command: Mapping[str, object], identity: Mapping[str, object], predecessor_tree_ids: frozenset[int], delivery_ns: int) -> None:
    """Join the E0 command block to replica-2 authority and every witness."""
    height, block_hash = command["command_block_height"], command["command_block_hash"]
    authority = []
    for event in streams[2]:
        if event.get("event_type") != "block.committed": continue
        payload = _strict_commit(event.get("payload"), observed=False); proof = payload["decision_proof"]
        if payload["block_height"] == height and payload["block_hash"] == block_hash:
            if not (payload["designated_observer"] is True and proof["epoch_number"] == identity["predecessor_epoch_number"] and proof["epoch_digest"] == identity["predecessor_epoch_digest"] and proof["tree_id"] in predecessor_tree_ids and proof["block_hash"] == block_hash):
                raise V8ValidationError("command block authority is not the exact designated E0 commit")
            if (_timestamp(event, "command block authority") <= delivery_ns or _sequence(event, "command block authority") >= _sequence(commands[2], "replica-2 command") or _timestamp(event, "command block authority") > _timestamp(commands[2], "replica-2 command")):
                raise V8ValidationError("command block authority does not precede replica-2 command commitment")
            authority.append((event, payload))
    if len(authority) != 1: raise V8ValidationError("command block lacks one designated E0 block.committed authority")
    metadata = (authority[0][1]["parent_hash"], authority[0][1]["transaction_count"])
    for replica, events in streams.items():
        matches = []
        for event in events:
            if event.get("event_type") != "block.commit_observed": continue
            payload = _strict_commit(event.get("payload"), observed=True)
            if payload["block_height"] == height and payload["block_hash"] == block_hash:
                if (payload["parent_hash"], payload["transaction_count"]) != metadata:
                    raise V8ValidationError("command-block witness metadata differs from authority")
                if (_timestamp(event, f"replica-{replica} command-block witness") <= delivery_ns or _sequence(event, f"replica-{replica} command-block witness") >= _sequence(commands[replica], f"replica-{replica} command") or _timestamp(event, f"replica-{replica} command-block witness") > _timestamp(commands[replica], f"replica-{replica} command")):
                    raise V8ValidationError("command-block witness does not precede same-replica command commitment")
                matches.append(event)
        if len(matches) != 1: raise V8ValidationError("command block lacks one exact commit_observed witness per replica")


def _manager_success(events: Sequence[Mapping[str, object]], *, start: Mapping[str, object],
                     delivery_sequence: int, delivery_ns: int, activation_deadline_ns: int,
                     identity: Mapping[str, object], command: Mapping[str, object]) -> None:
    """Audit manager-local terminal ordering, not cross-replica activation order."""
    start_ns = _timestamp(start, "convergence-start")
    command_identity = {
        "predecessor_epoch_number": command["predecessor_epoch_number"],
        "predecessor_epoch_digest": command["predecessor_epoch_digest"],
        "successor_epoch_number": command["successor_epoch_number"],
        "successor_epoch_digest": command["successor_epoch_digest"],
        "command_payload_digest": command["payload_digest"],
        "command_block_height": command["command_block_height"],
        "command_block_hash": command["command_block_hash"],
        "activation_delay_blocks": command["activation_delay_blocks"],
        "activation_height": command["activation_height"],
    }
    ready = [event for event in events if event.get("event_type") == "adaptive_v2_ready"]
    if len(ready) != 1:
        raise V8ValidationError("manager lacks one exact native Q5 adaptive_v2_ready record")
    ready_event = ready[0]
    ready_payload = ready_event.get("payload")
    if (not isinstance(ready_payload, Mapping) or set(ready_payload) != _MANAGER_CONVERGENCE_FIELDS or
            any(ready_payload[field] is not None for field in
                ("replica_id", "delivery_attempt", "disposition", "canonical_payload_digest", "failure_reason")) or
            type(ready_payload.get("accepted_commit_count")) is not int or
            not 0 <= ready_payload["accepted_commit_count"] <= len(profile.REPLICA_IDS) or
            ready_payload.get("accepted_activation_count") != profile.QUORUM or
            ready_payload.get("required_activation_count") != profile.QUORUM or
            not isinstance(ready_payload.get("identity"), Mapping) or
            set(ready_payload["identity"]) != _MANAGER_IDENTITY_FIELDS or
            dict(ready_payload["identity"]) != command_identity):
        raise V8ValidationError("manager adaptive_v2_ready does not prove exact native Q5 command identity")
    ready_ns = _timestamp(ready_event, "manager Q5 ready")
    ready_sequence = _sequence(ready_event, "manager Q5 ready")
    if ready_sequence <= delivery_sequence or ready_ns <= delivery_ns:
        raise V8ValidationError("manager Q5 ready is not ordered after manager evidence and exact delivery")

    terminals = [event for event in events if event.get("event_type") == "adaptive_v2_session_terminal"]
    for event in terminals:
        candidate = event.get("payload")
        if (isinstance(candidate, Mapping) and
                candidate.get("predecessor_epoch_number") == identity["predecessor_epoch_number"] and
                candidate.get("predecessor_epoch_digest") == identity["predecessor_epoch_digest"] and
                candidate.get("successor_epoch_number") == identity["successor_epoch_number"] and
                candidate.get("successor_epoch_digest") == identity["successor_epoch_digest"] and
                candidate.get("command_payload_digest") == identity["command_payload_digest"] and
                (candidate.get("outcome") != "advanced" or candidate.get("reason") != "successor_converged")):
            raise V8ValidationError("same-identity manager terminal contradicts successor success")
    if len(terminals) != 1:
        raise V8ValidationError("manager lacks one exact successor-converged terminal")
    terminal = terminals[0]
    payload = terminal.get("payload")
    if (not isinstance(payload, Mapping) or set(payload) != _MANAGER_TERMINAL_FIELDS or
            payload.get("cycle_ordinal") != 0 or payload.get("policy_intent") != "fault_containment" or
            payload.get("outcome") != "advanced" or payload.get("reason") != "successor_converged" or
            payload.get("transition_artifact_id") != "e0-to-e1-containment" or
            payload.get("predecessor_epoch_number") != identity["predecessor_epoch_number"] or
            payload.get("predecessor_epoch_digest") != identity["predecessor_epoch_digest"] or
            payload.get("successor_epoch_number") != identity["successor_epoch_number"] or
            payload.get("successor_epoch_digest") != identity["successor_epoch_digest"] or
            payload.get("command_payload_digest") != identity["command_payload_digest"] or
            payload.get("winning_activation") != command_identity or payload.get("controller_failure") is not None or
            type(payload.get("evidence_window_activation_generation")) is not int or
            payload["evidence_window_activation_generation"] <= 0 or
            payload.get("baseline_evidence_cutoff") != identity["baseline_evidence_cutoff"] or
            payload.get("current_evidence_cutoff") != identity["evidence_cutoff"]):
        raise V8ValidationError("manager terminal is not the complete native successor identity")
    terminal_ns = _timestamp(terminal, "manager success terminal")
    if (_sequence(terminal, "manager success terminal") <= max(
            _sequence(start, "convergence-start"), delivery_sequence, ready_sequence) or
            terminal_ns <= ready_ns or terminal_ns > activation_deadline_ns):
        raise V8ValidationError("manager success terminal is not ordered after manager evidence before A+32")
    # Conservative audit guard only.  The immutable root replay must also
    # bind --convergence-deadline-seconds 12; R is emitted after
    # start_convergence, so R+12 is not claimed as the native timer.
    if terminal_ns > start_ns + _MANAGER_SUCCESS_BUDGET_NS:
        raise V8ValidationError("manager success terminal exceeds prospective R-plus-12 audit bound")


def _designated_commit(streams: Mapping[int, Sequence[Mapping[str, object]]], *, earliest_ns: int, horizon_ns: int, successor_digest: object) -> None:
    """Only configured replica 2 is the designated authoritative observer."""
    for event in streams[2]:
        if event.get("event_type") != "block.committed": continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or set(payload) != _COMMIT_FIELDS: raise V8ValidationError("block.committed payload schema drifted")
        proof = payload.get("decision_proof"); timestamp = _timestamp(event, "designated commit")
        if (payload.get("designated_observer") is True and earliest_ns <= timestamp < horizon_ns and isinstance(proof, Mapping) and
                proof.get("epoch_number") == 1 and proof.get("epoch_digest") == successor_digest and proof.get("block_hash") == payload.get("block_hash")): return
    raise V8ValidationError("no authoritative designated-observer E1 block.committed event in measurement horizon")


def _common_e1_commit_count(streams: Mapping[int, Sequence[Mapping[str, object]]], *, start_ns: int,
                            end_ns: int, successor_digest: object, tree_ids: frozenset[int],
                            activation_boundaries: Mapping[int, tuple[int, int]]) -> int:
    """Recompute the all-seven common committed-block metric, never tx throughput."""
    authoritative: dict[tuple[int, str], tuple[object, object]] = {}
    observed: dict[tuple[int, str], dict[int, tuple[object, object]]] = {}
    height_hashes: dict[int, str] = {}
    for replica, events in streams.items():
        for event in events:
            timestamp = _timestamp(event, f"replica-{replica} commit metric")
            if not start_ns <= timestamp < end_ns: continue
            if event.get("event_type") in {"block.commit_observed", "block.committed"}:
                activation_ns, activation_sequence = activation_boundaries[replica]
                if (timestamp < activation_ns or
                        _sequence(event, f"replica-{replica} commit metric") <= activation_sequence):
                    raise V8ValidationError("committed-block metric event does not follow local E1 activation")
            if event.get("event_type") == "block.commit_observed":
                payload = _strict_commit(event.get("payload"), observed=True)
                if payload["transaction_count"] != 1:
                    raise V8ValidationError("scored E1 committed block must contain exactly one synthetic command")
                prior = height_hashes.setdefault(payload["block_height"], payload["block_hash"])
                if prior != payload["block_hash"]: raise V8ValidationError("conflicting commit hashes at one block height")
                key = (payload["block_height"], payload["block_hash"])
                if replica in observed.setdefault(key, {}): raise V8ValidationError("replica repeats commit-observed metric identity")
                observed[key][replica] = (payload["parent_hash"], payload["transaction_count"])
            elif event.get("event_type") == "block.committed":
                payload = _strict_commit(event.get("payload"), observed=False)
                if payload["transaction_count"] != 1:
                    raise V8ValidationError("scored E1 committed block must contain exactly one synthetic command")
                prior = height_hashes.setdefault(payload["block_height"], payload["block_hash"])
                if prior != payload["block_hash"]: raise V8ValidationError("conflicting commit hashes at one block height")
                proof = payload["decision_proof"]
                if payload["designated_observer"] is not (replica == 2):
                    raise V8ValidationError("designated-observer flag disagrees with configured replica-2 authority")
                if proof["epoch_number"] == 0:
                    raise V8ValidationError("predecessor authority appears in the E1 metric window")
                if proof["epoch_number"] != 1 or proof["epoch_digest"] != successor_digest or proof["tree_id"] not in tree_ids:
                    raise V8ValidationError("foreign E1 authority appears in committed-block metric window")
                if replica != 2: continue
                if proof["epoch_number"] != 1: continue
                key = (payload["block_height"], payload["block_hash"])
                if key in authoritative: raise V8ValidationError("replica-2 repeats authoritative E1 metric identity")
                authoritative[key] = (payload["parent_hash"], payload["transaction_count"])
    if not authoritative: raise V8ValidationError("measurement horizon lacks an authoritative E1 committed block")
    expected_sources = set(profile.REPLICA_IDS)
    for key, metadata in authoritative.items():
        if observed.get(key) != {replica: metadata for replica in expected_sources}:
            raise V8ValidationError("authoritative E1 committed block lacks matching all-seven observations")
    return len(authoritative)


def validate_v8_raw_contract(*, anchor_monotonic_ns: int, expected_identity: Mapping[str, object], manager_events: Sequence[Mapping[str, object]], replica_events: Mapping[int, Sequence[Mapping[str, object]]], bundle_bytes: bytes, issuer_public_key: str, predecessor_tree_ids: frozenset[int]) -> dict[str, Any]:
    """Require signed bundle + raw causal chain.  Passing remains no claim.

    ``anchor_monotonic_ns`` is the marker-bijected physical omission decision
    clock returned by sealed RAW replay, not the later structured source
    emission clock. ``predecessor_tree_ids`` must come from the immutable
    archived E0 tree configuration; this component does not invent topology
    or establish either caller-supplied input's provenance.
    """
    timing = profile.deadlines(anchor_monotonic_ns)
    if not isinstance(bundle_bytes, bytes) or not bundle_bytes or not isinstance(issuer_public_key, str): raise V8ValidationError("signed bundle bytes and issuer public key are required")
    bundle = _decode_bundle(bundle_bytes, issuer_public_key)
    signed_delay = getattr(getattr(bundle, "command", None), "activation_delay_blocks", None)
    if type(signed_delay) is not int or signed_delay <= 0: raise V8ValidationError("decoded signed bundle has invalid activation delay")
    if (not isinstance(predecessor_tree_ids, frozenset) or not predecessor_tree_ids or
            any(type(tree_id) is not int or tree_id < 0 for tree_id in predecessor_tree_ids)):
        raise V8ValidationError("root replay must bind a nonempty frozen predecessor tree-ID set")
    decoded = _bundle_identity(bundle)
    expected = _identity({"cycle_ordinal": 0, **dict(expected_identity)})
    if any(expected[key] != decoded[key] for key in decoded): raise V8ValidationError("selected identity differs from independently decoded signed bundle")
    identity = expected
    starts = [event for event in manager_events if event.get("event_type") == profile.CONVERGENCE_EVENT_TYPE]
    if len(starts) != 1 or starts[0].get("source_kind") != "adaptation_manager": raise V8ValidationError("exactly one manager convergence-start event is required")
    start = starts[0]; start_ns = _timestamp(start, "convergence-start")
    if start_ns > timing["convergence_deadline_ns"]: raise V8ValidationError("convergence-start exceeded A+20 seconds")
    payload = start.get("payload")
    if not isinstance(payload, Mapping) or payload.get("cycle_ordinal") != 0 or _identity(payload) != identity: raise V8ValidationError("convergence-start identity differs from frozen first-cycle command")
    _selection_before_start(manager_events, anchor_ns=anchor_monotonic_ns, start=start,
                            selection_deadline_ns=timing["convergence_deadline_ns"], identity=identity,
                            cycle_ordinal=payload["cycle_ordinal"])
    delivery_ns, delivery_sequence = _delivery(manager_events, start_ns=start_ns, start_sequence=_sequence(start, "convergence-start"), bundle_sha256=hashlib.sha256(bundle_bytes).hexdigest())
    if set(replica_events) != set(profile.REPLICA_IDS): raise V8ValidationError("v8 requires raw streams from all seven replicas")
    tree_ids = _validate_n7_containment_shape(bundle)
    activation_boundaries: dict[int, tuple[int, int]] = {}
    command_events: dict[int, Mapping[str, object]] = {}
    common_command: tuple[object, ...] | None = None
    for replica in profile.REPLICA_IDS:
        events = replica_events[replica]
        coverage = [event for event in events if
                    _timestamp(event, f"replica-{replica} coverage") >= timing["horizon_ns"] and
                    isinstance(event.get("event_type"), str) and bool(event["event_type"]) and
                    isinstance(event.get("payload"), Mapping)]
        if not coverage:
            raise V8ValidationError("replica raw lacks a structured payload-bearing event through A+72 seconds")
        commands = [(event, _command(event, identity, signed_delay)) for event in events]; commands = [(event, command) for event, command in commands if command is not None]
        if len(commands) != 1: raise V8ValidationError("each replica needs exactly one matching epoch.command_committed")
        command_event, command = commands[0]; command_ns = _timestamp(command_event, f"replica-{replica} command")
        if command_ns <= delivery_ns: raise V8ValidationError("command must be committed after the first enqueued delivery")
        command_identity = (command["command_block_height"], command["command_block_hash"], command["activation_delay_blocks"], command["activation_height"])
        if common_command is None: common_command = command_identity
        elif common_command != command_identity: raise V8ValidationError("all-seven commands do not bind one common command block and activation identity")
        candidates = [event for event in events if _activation(event, command, tree_ids)]
        if len(candidates) != 1: raise V8ValidationError("each replica needs exactly one matching epoch.activated")
        activation = candidates[0]; activation_ns = _timestamp(activation, f"replica-{replica} activation")
        if (_sequence(activation, f"replica-{replica} activation") <= _sequence(command_event, f"replica-{replica} command") or activation_ns <= command_ns or activation_ns > timing["activation_deadline_ns"]): raise V8ValidationError("activation is not after its command or misses A+32 seconds")
        activation_boundaries[replica] = (
            activation_ns, _sequence(activation, f"replica-{replica} activation"))
        command_events[replica] = command_event
        for event in events:
            event_ns = _timestamp(event, f"replica-{replica} authority scan")
            if event_ns <= start_ns or event_ns > timing["horizon_ns"]: continue
            payload = event.get("payload")
            if event.get("event_type") == "epoch.command_committed":
                if _command(event, identity, signed_delay) is None:
                    raise V8ValidationError("foreign or misbound successor command authority appears before horizon")
            if event.get("event_type") == "epoch.activated":
                if not _activation(event, command, tree_ids):
                    raise V8ValidationError("foreign or misbound epoch activation appears before horizon")
    assert common_command is not None
    _command_block_committed(replica_events, command_events, commands[0][1], identity, predecessor_tree_ids, delivery_ns)
    _manager_success(manager_events, start=start, delivery_sequence=delivery_sequence, delivery_ns=delivery_ns,
                     activation_deadline_ns=timing["activation_deadline_ns"], identity=identity,
                     command=commands[0][1])
    common_count = _common_e1_commit_count(
        replica_events, start_ns=timing["measurement_start_ns"],
        end_ns=timing["measurement_end_ns"],
        successor_digest=identity["successor_epoch_digest"], tree_ids=tree_ids,
        activation_boundaries=activation_boundaries)
    return {"verdict": "PASS_COMPONENT_ONLY_NO_CLAIM", "profile_id": profile.PROFILE_ID, "measurement_window": [timing["measurement_start_ns"], timing["measurement_end_ns"]], "convergence_start_monotonic_ns": start_ns, "first_delivery_monotonic_ns": delivery_ns, "common_e1_committed_blocks": common_count, "metric_kind": "all_seven_common_committed_block_count"}
