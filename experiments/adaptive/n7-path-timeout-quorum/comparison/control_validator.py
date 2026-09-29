"""Planning-only source-blind validator for a future fixed-tree N=7 control.

It consumes structured raw events only; it starts no process and deliberately
does not know the injected actor.  A future producer must bind the contract to
raw file hashes before this can validate an execution artifact.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


class ValidationError(ValueError):
    pass


_HEX = frozenset("0123456789abcdef")
_REPLICAS = frozenset(range(7))
_ALLOWED_OMISSION_CONTEXTS = frozenset({(4, 4), (5, 5), (6, 6)})
_FORBIDDEN_FAMILY = ("adaptive_", "epoch.", "transition", "readiness", "bundle", "selection")


def _hex64(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in _HEX for ch in value):
        raise ValidationError(f"{label} must be a lower-case SHA-256")
    return value


def _event_digest(event: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


def _event(event: object, *, run_id: str, source_kind: str, source_id: str) -> Mapping[str, Any]:
    required = {"event_schema_version", "run_id", "source_kind", "source_id", "source_instance", "source_sequence", "source_monotonic_ns", "event_type", "payload"}
    if not isinstance(event, Mapping) or set(event) != required:
        raise ValidationError("structured event schema drift")
    if (event.get("event_schema_version") != 1 or event.get("run_id") != run_id or
            event.get("source_kind") != source_kind or event.get("source_id") != source_id or
            not isinstance(event.get("source_instance"), str) or not event["source_instance"] or
            type(event.get("source_sequence")) is not int or event["source_sequence"] <= 0 or
            type(event.get("source_monotonic_ns")) is not int or event["source_monotonic_ns"] <= 0 or
            not isinstance(event.get("event_type"), str)):
        raise ValidationError("structured event identity drift")
    return event


def _reject_transition_or_non_e0(event: Mapping[str, Any], digest: str) -> None:
    # A fixed-E0 control ends through the manager's explicit no-op terminal.
    # It is checked exactly by ``validate_raw_bundle``; it is not an epoch
    # transition merely because its native event name contains "adaptive".
    if (event["event_type"] != "adaptive_v2_session_terminal" and
            any(token in event["event_type"] for token in _FORBIDDEN_FAMILY)):
        raise ValidationError("control contains a forbidden transition event family")
    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            if "epoch_number" in value and (
                type(value["epoch_number"]) is not int or value["epoch_number"] != 0
            ):
                raise ValidationError("control event carries a non-E0 identity")
            if "epoch_digest" in value and value["epoch_digest"] != digest:
                raise ValidationError("control event carries a non-E0 identity")
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(event["payload"])


def validate_control(contract: Mapping[str, Any], manager_events: Sequence[object], replica_streams: Mapping[str, Sequence[object]]) -> dict[str, Any]:
    """Validate a no-successor control over one fixed post-drop horizon."""
    required = {"schema_version", "state", "run_id", "manifest_sha256", "epoch0", "fault_window_arm", "omission_gate_sha256", "omission_context", "designated_observer", "horizon_ns"}
    if (not isinstance(contract, Mapping) or set(contract) != required or
            type(contract.get("schema_version")) is not int or contract["schema_version"] != 2 or
            contract.get("state") not in {
                "PLANNING_ONLY_NO_LAUNCH", "FROZEN_EXECUTION_NO_SUCCESSOR"
            }):
        raise ValidationError("control contract schema drift")
    run_id = contract.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValidationError("control contract run ID is invalid")
    _hex64(contract.get("manifest_sha256"), "manifest hash")
    gate_sha256 = _hex64(contract.get("omission_gate_sha256"), "omission gate hash")
    epoch0 = contract.get("epoch0")
    if (not isinstance(epoch0, Mapping) or set(epoch0) != {"epoch_number", "epoch_digest", "tree_file_sha256"} or
            type(epoch0.get("epoch_number")) is not int or epoch0["epoch_number"] != 0):
        raise ValidationError("control contract E0 identity drift")
    digest = _hex64(epoch0.get("epoch_digest"), "E0 digest")
    _hex64(epoch0.get("tree_file_sha256"), "E0 tree hash")
    if contract.get("horizon_ns") != 60_000_000_000:
        raise ValidationError("control horizon must be exactly 60 seconds")
    if type(contract.get("designated_observer")) is not int or contract["designated_observer"] not in _REPLICAS:
        raise ValidationError("control designated observer is invalid")
    context = contract.get("omission_context")
    if (not isinstance(context, Mapping) or set(context) != {"tree_id", "parent_replica", "expected_message_type"} or
            type(context.get("tree_id")) is not int or
            type(context.get("parent_replica")) is not int or
            (context.get("tree_id"), context.get("parent_replica")) not in _ALLOWED_OMISSION_CONTEXTS or
            context.get("expected_message_type") != "aggregate_relay"):
        raise ValidationError("control omission context drift")
    arm = contract.get("fault_window_arm")
    if not isinstance(arm, Mapping) or set(arm) != {"source_sequence", "event_sha256"} or type(arm.get("source_sequence")) is not int:
        raise ValidationError("control fault-window binding drift")
    _hex64(arm.get("event_sha256"), "fault-window event hash")
    if set(replica_streams) != {f"replica-{item}" for item in _REPLICAS}:
        raise ValidationError("control must retain exactly seven replica streams")

    arm_event = None
    previous = 0
    manager_instance = None
    manager_ns = 0
    for raw in manager_events:
        event = _event(raw, run_id=run_id, source_kind="adaptation_manager", source_id="adaptive-manager")
        if event["source_sequence"] <= previous:
            raise ValidationError("manager source sequence is not strictly increasing")
        if manager_instance is None: manager_instance = event["source_instance"]
        elif manager_instance != event["source_instance"]: raise ValidationError("manager source instance changed")
        if event["source_monotonic_ns"] <= manager_ns: raise ValidationError("manager monotonic time is not strictly increasing")
        manager_ns = event["source_monotonic_ns"]
        previous = event["source_sequence"]
        _reject_transition_or_non_e0(event, digest)
        if event["source_sequence"] == arm["source_sequence"]:
            arm_event = event
    if arm_event is None or arm_event["event_type"] != "fault_window_armed":
        raise ValidationError("control fault-window event is not source-bound")
    arm_payload = arm_event["payload"]
    if (not isinstance(arm_payload, Mapping) or type(arm_payload.get("epoch_number")) is not int or
            arm_payload["epoch_number"] != 0 or arm_payload.get("epoch_digest") != digest):
        raise ValidationError("control fault-window event does not bind E0")
    arm_ns = arm_event["source_monotonic_ns"]

    drops: list[Mapping[str, Any]] = []
    first_omission_contexts: set[tuple[object, ...]] = set()
    designated: dict[int, tuple[str, Mapping[str, Any]]] = {}
    observations: dict[tuple[int, str], dict[int, Mapping[str, Any]]] = {}
    all_heights: dict[int, str] = {}
    for replica in sorted(_REPLICAS):
        source = f"replica-{replica}"
        previous = 0
        previous_ns = 0
        source_instance = None
        for raw in replica_streams[source]:
            event = _event(raw, run_id=run_id, source_kind="replica", source_id=source)
            if event["source_sequence"] <= previous:
                raise ValidationError("replica source sequence is not strictly increasing")
            if source_instance is None: source_instance = event["source_instance"]
            elif source_instance != event["source_instance"]: raise ValidationError("replica source instance changed")
            if event["source_monotonic_ns"] <= previous_ns: raise ValidationError("replica monotonic time is not strictly increasing")
            previous_ns = event["source_monotonic_ns"]
            previous = event["source_sequence"]
            _reject_transition_or_non_e0(event, digest)
            payload = event["payload"]
            if event["event_type"] == "fault.aggregate_omitted":
                fields = {"actor", "parent_replica", "epoch_number", "tree_id", "epoch_digest", "block_hash", "gate_sha256", "first_for_context"}
                if (replica != 1 or not isinstance(payload, Mapping) or set(payload) != fields or
                        type(payload.get("actor")) is not int or
                        type(payload.get("parent_replica")) is not int or
                        type(payload.get("epoch_number")) is not int or payload["epoch_number"] != 0 or
                        type(payload.get("tree_id")) is not int or
                        payload.get("epoch_digest") != digest or type(payload.get("first_for_context")) is not bool or
                        (payload.get("tree_id"), payload.get("parent_replica")) not in _ALLOWED_OMISSION_CONTEXTS or
                        event["source_monotonic_ns"] <= arm_ns):
                    raise ValidationError("physical omission does not bind post-arm E0")
                _hex64(payload.get("block_hash"), "physical omission block hash")
                _hex64(payload.get("gate_sha256"), "physical omission gate hash")
                # Do not compare actor to a declared fault label: derive only from raw source.
                if payload.get("actor") != 1 or payload.get("gate_sha256") != gate_sha256:
                    raise ValidationError("physical omission actor differs from its raw source")
                omission_context = (
                    payload["actor"], payload["parent_replica"],
                    payload["epoch_number"], payload["tree_id"],
                    payload["epoch_digest"], payload["block_hash"],
                    payload["gate_sha256"],
                )
                if payload["first_for_context"]:
                    first_omission_contexts.add(omission_context)
                    drops.append(event)
                elif omission_context not in first_omission_contexts:
                    raise ValidationError(
                        "later omission lacks an identical prior true context"
                    )
            elif event["event_type"] == "block.committed" and isinstance(payload, Mapping) and payload.get("designated_observer") is True:
                if replica != contract["designated_observer"]:
                    raise ValidationError("control designated observer differs from contract")
                proof = payload.get("decision_proof")
                if (set(payload) != {"block_height", "block_hash", "parent_hash", "transaction_count", "designated_observer", "decision_proof", "view_generation", "commit_batch_index"} or
                        not isinstance(proof, Mapping) or set(proof) != {"epoch_number", "tree_id", "epoch_digest", "block_hash"} or
                        type(proof.get("epoch_number")) is not int or proof["epoch_number"] != 0 or
                        type(proof.get("tree_id")) is not int or
                        not 0 <= proof["tree_id"] < 7 or proof.get("epoch_digest") != digest or
                        proof.get("block_hash") != payload.get("block_hash") or
                        (payload.get("view_generation") is not None and
                         (type(payload["view_generation"]) is not int or payload["view_generation"] <= 0)) or
                        type(payload.get("transaction_count")) is not int or payload["transaction_count"] < 0 or
                        type(payload.get("commit_batch_index")) is not int or payload["commit_batch_index"] < 0):
                    raise ValidationError("control designated commit is not an E0 proof")
                height = payload.get("block_height")
                block_hash = _hex64(payload.get("block_hash"), "control designated block hash")
                _hex64(proof.get("block_hash"), "control decision proof block hash")
                if payload.get("parent_hash") is not None:
                    _hex64(payload["parent_hash"], "control designated parent hash")
                if type(height) is not int or height <= 0:
                    raise ValidationError("control designated commit identity is invalid")
                if all_heights.setdefault(height, block_hash) != block_hash:
                    raise ValidationError("control commits conflict at one height")
                if height in designated:
                    raise ValidationError("control repeats a designated commit height")
                designated[height] = (block_hash, event)
            elif event["event_type"] == "block.commit_observed":
                if not isinstance(payload, Mapping):
                    raise ValidationError("control witness native metadata has schema drift")
                height, block_hash = payload.get("block_height"), payload.get("block_hash")
                if (set(payload) != {"block_height", "block_hash", "parent_hash", "transaction_count", "commit_batch_index"} or
                        type(height) is not int or height <= 0 or
                        type(payload["transaction_count"]) is not int or
                        payload["transaction_count"] < 0 or
                        type(payload["commit_batch_index"]) is not int or
                        payload["commit_batch_index"] < 0):
                    raise ValidationError("control witness native metadata has schema drift")
                block_hash = _hex64(block_hash, "control witness block hash")
                if payload["parent_hash"] is not None:
                    _hex64(payload["parent_hash"], "control witness parent hash")
                if all_heights.setdefault(height, block_hash) != block_hash:
                    raise ValidationError("control commits conflict at one height")
                witnesses = observations.setdefault((height, block_hash), {})
                if replica in witnesses:
                    raise ValidationError("control repeats a peer commit witness")
                witnesses[replica] = event

    if not drops:
        raise ValidationError("control lacks a first physical omission")
    anchor = min(drops, key=lambda event: (event["source_monotonic_ns"], event["source_sequence"]))
    if (anchor["payload"].get("tree_id"), anchor["payload"].get("parent_replica")) != (
            context["tree_id"], context["parent_replica"]):
        raise ValidationError("control contract does not select the earliest admitted physical omission")
    start, end = anchor["source_monotonic_ns"], anchor["source_monotonic_ns"] + contract["horizon_ns"]
    commits = []
    commit_times = []
    for height, (block_hash, event) in sorted(designated.items()):
        if not start <= event["source_monotonic_ns"] <= end:
            continue
        source = int(event["source_id"].split("-")[1])
        witnesses = observations.get((height, block_hash), {})
        committed = event["payload"]
        expected_metadata = (
            committed["parent_hash"], committed["transaction_count"], committed["commit_batch_index"],
        )
        if (set(witnesses) != _REPLICAS or
                any(not max(start, event["source_monotonic_ns"]) <= witness["source_monotonic_ns"] <= end
                    for witness in witnesses.values()) or
                any((witness["payload"]["parent_hash"], witness["payload"]["transaction_count"],
                     witness["payload"]["commit_batch_index"]) != expected_metadata
                    for witness in witnesses.values())):
            raise ValidationError("control authoritative commit lacks all six peer witnesses")
        commits.append({"block_height": height, "block_hash": block_hash})
        commit_times.append(event["source_monotonic_ns"])
    if commit_times != sorted(commit_times):
        raise ValidationError("control designated commit time regresses with height")
    gaps = [later - earlier for earlier, later in zip(commit_times, commit_times[1:])]
    is_live_contract = contract["state"] == "FROZEN_EXECUTION_NO_SUCCESSOR"
    return {
        "verdict": (
            "CONTROL_RAW_BUNDLE_VALIDATED_PROSPECTIVE"
            if is_live_contract else "CONTROL_PLANNING_ONLY_VALID"
        ),
        "anchor_monotonic_ns": start,
        "horizon_end_monotonic_ns": end,
        "authoritative_common_commits": commits,
        "common_commit_count": len(commits),
        "maximum_inter_commit_gap_ns": max(gaps) if gaps else None,
        "claim_boundary": (
            "A retained, source-bound fixed-E0 control trace only; it does not "
            "establish a paired advantage, throughput, or a general Byzantine claim."
            if is_live_contract else
            "Planning-only raw-event validation; a native no-successor producer and "
            "raw-file receipt are still required."
        ),
    }


def _safe_child(root: Path, relative: object) -> Path:
    if not isinstance(relative, str):
        raise ValidationError("raw receipt artifact path is invalid")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValidationError("raw receipt artifact path escapes root") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ValidationError("raw receipt artifact is not a regular file")
    return candidate


def _read_jsonl(root: Path, descriptor: object, *, run_id: str, retain_line_hashes: bool = False) -> list[object] | tuple[list[object], dict[int, str]]:
    if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "sha256"}:
        raise ValidationError("raw receipt artifact descriptor drift")
    expected = _hex64(descriptor.get("sha256"), "raw receipt artifact hash")
    path = _safe_child(root, descriptor.get("path"))
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected or not raw.endswith(b"\n"):
        raise ValidationError("raw receipt artifact hash or framing drift")
    events = []
    line_hashes: dict[int, str] = {}
    for line_number, line in enumerate(raw.splitlines(), 1):
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("raw receipt structured-event stream is malformed") from exc
        if not isinstance(event, Mapping) or event.get("run_id") != run_id:
            raise ValidationError("raw receipt structured-event run identity drift")
        events.append(event)
        sequence = event.get("source_sequence")
        if type(sequence) is not int or sequence in line_hashes:
            raise ValidationError("raw receipt source sequence cannot identify one native JSONL line")
        line_hashes[sequence] = _sha256(line)
    if retain_line_hashes:
        return events, line_hashes
    return events


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
    except (TypeError, ValueError) as exc:
        raise ValidationError("control document is not canonical JSON") from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_document(root: Path, descriptor: object, label: str) -> tuple[dict[str, Any], bytes]:
    if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "sha256"}:
        raise ValidationError(f"{label} descriptor drift")
    path = _safe_child(root, descriptor.get("path"))
    raw = path.read_bytes()
    if _sha256(raw) != _hex64(descriptor.get("sha256"), f"{label} hash"):
        raise ValidationError(f"{label} hash drift")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{label} is not JSON") from exc
    if not isinstance(document, dict) or raw != _canonical(document):
        raise ValidationError(f"{label} is not canonical JSON")
    return document, raw


def _digest_without(document: Mapping[str, Any], field: str) -> str:
    return _sha256(_canonical({key: value for key, value in document.items() if key != field})[:-1])


def _external_descriptor(descriptor: object, label: str) -> dict[str, str]:
    """Verify a pinned executable outside a run root without accepting a symlink."""
    if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "sha256"}:
        raise ValidationError(f"{label} descriptor drift")
    path_text = descriptor.get("path")
    if not isinstance(path_text, str) or not path_text:
        raise ValidationError(f"{label} path drift")
    path = Path(path_text)
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"{label} is not a regular executable")
    expected = _hex64(descriptor.get("sha256"), f"{label} hash")
    if _sha256(path.read_bytes()) != expected:
        raise ValidationError(f"{label} hash drift")
    return {"path": str(path.resolve()), "sha256": expected}


def _validate_cleanup_document(document: object, run_id: str) -> None:
    """Require exact retained shutdown outcomes, including a clean manager."""
    expected_sources = ("adaptive-manager", *(f"replica-{item}" for item in sorted(_REPLICAS)))
    expected_fields = {"source_id", "pid", "pgid", "returncode", "termination"}
    if (not isinstance(document, Mapping) or
            set(document) != {"schema_version", "run_id", "complete", "processes"} or
            document.get("schema_version") != 1 or document.get("run_id") != run_id or
            document.get("complete") is not True or not isinstance(document.get("processes"), list) or
            len(document["processes"]) != len(expected_sources)):
        raise ValidationError("control cleanup receipt is incomplete")
    by_source: dict[str, Mapping[str, Any]] = {}
    for item in document["processes"]:
        if (not isinstance(item, Mapping) or set(item) != expected_fields or
                not isinstance(item.get("source_id"), str) or
                type(item.get("pid")) is not int or item["pid"] <= 1 or
                type(item.get("pgid")) is not int or item["pgid"] <= 1 or
                type(item.get("returncode")) is not int or
                item.get("termination") not in {"clean-exit", "terminated"} or
                item["source_id"] in by_source):
            raise ValidationError("control cleanup process outcome is invalid")
        if item["returncode"] != 0 or item["termination"] != "clean-exit":
            raise ValidationError("control cleanup process did not exit cleanly")
        by_source[item["source_id"]] = item
    if tuple(by_source) != expected_sources:
        raise ValidationError("control cleanup receipt does not cover exact process outcomes")
    manager = by_source["adaptive-manager"]
    if manager["returncode"] != 0 or manager["termination"] != "clean-exit":
        raise ValidationError("control cleanup manager did not exit cleanly")


def _validate_clean_fixed_e0_terminal(
    manager_events: Sequence[Mapping[str, Any]], *, run_id: str,
    epoch_digest: str, horizon_end_ns: int,
) -> None:
    terminal_fields = {
        "cycle_ordinal", "policy_intent", "outcome", "reason",
        "transition_artifact_id", "predecessor_epoch_number",
        "predecessor_epoch_digest", "successor_epoch_number",
        "successor_epoch_digest", "command_payload_digest",
        "winning_activation", "controller_failure",
        "evidence_window_activation_generation", "baseline_evidence_cutoff",
        "current_evidence_cutoff",
    }
    terminals = [
        event for event in manager_events
        if event.get("event_type") == "adaptive_v2_session_terminal"
    ]
    if len(terminals) != 1:
        raise ValidationError("control lacks exactly one clean fixed-E0 no-op terminal")
    terminal = terminals[0]
    payload = terminal.get("payload")
    if (not isinstance(payload, Mapping) or set(payload) != terminal_fields or
            terminal.get("source_monotonic_ns", 0) < horizon_end_ns or
            payload.get("cycle_ordinal") != 0 or
            payload.get("policy_intent") != "fault_containment" or
            payload.get("outcome") != "no_op" or
            payload.get("reason") != "explicit_no_op" or
            payload.get("transition_artifact_id") != f"fixed-e0-control/{run_id}" or
            payload.get("predecessor_epoch_number") != 0 or
            payload.get("predecessor_epoch_digest") != epoch_digest or
            payload.get("successor_epoch_number") is not None or
            payload.get("successor_epoch_digest") is not None or
            payload.get("command_payload_digest") is not None or
            payload.get("winning_activation") is not None or
            payload.get("controller_failure") is not None or
            type(payload.get("evidence_window_activation_generation")) is not int or
            payload["evidence_window_activation_generation"] <= 0 or
            type(payload.get("baseline_evidence_cutoff")) is not int or
            payload["baseline_evidence_cutoff"] < 0 or
            type(payload.get("current_evidence_cutoff")) is not int or
            payload["current_evidence_cutoff"] < payload["baseline_evidence_cutoff"]):
        raise ValidationError("control clean fixed-E0 no-op terminal semantics drift")


def _load_adaptive_runner():
    here = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "n7_control_raw_rederivation", here / "run_local.py"
    )
    if spec is None or spec.loader is None:
        raise ValidationError("cannot load N7 command rederivation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _strip_successor_options(command: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    cursor = 0
    removed = {"--transition-request": 0, "--bundle-output": 0}
    while cursor < len(command):
        option = command[cursor]
        if option in removed:
            if cursor + 1 >= len(command) or not command[cursor + 1]:
                raise ValidationError("rederived manager argv has malformed successor option")
            removed[option] += 1
            cursor += 2
        else:
            result.append(option)
            cursor += 1
    if any(value != 1 for value in removed.values()):
        raise ValidationError("rederived manager argv lacks exact successor inputs")
    control_option = "--fault-window-arm-control-only"
    if control_option in result:
        raise ValidationError("rederived manager argv already carries control mode")
    return tuple((*result, control_option))


def _launch_contract(plan: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "run_id", "epoch0", "hard_timeout_seconds", "declared_ports", "bindings",
        "manager_command", "replica_commands", "executables", "required_native_mode",
        "no_successor_guards",
    )
    return {field: plan[field] for field in fields}


def _verify_v2_authority_chain(root: Path, artifacts: Mapping[str, Any]) -> Mapping[str, Any]:
    """Replay every approval and command identity from retained immutable bytes."""
    prepared, prepared_bytes = _read_document(root, artifacts["prepared_plan"], "prepared control plan")
    request, request_bytes = _read_document(root, artifacts["authorization_request"], "control request")
    approval, approval_bytes = _read_document(root, artifacts["approved_authorization"], "control approval")
    finalization, _ = _read_document(root, artifacts["finalization_receipt"], "control finalization")
    base_plan, base_plan_bytes = _read_document(root, artifacts["base_plan"], "base plan")
    plan_fields = {
        "schema_version", "kind", "state", "claim_boundary", "base_plan", "base_plan_sha256",
        "run_id", "epoch0", "hard_timeout_seconds", "declared_ports", "bindings",
        "fault_window_arm", "omission_gate", "manager_events", "manager_command",
        "replica_commands", "executables", "required_native_mode", "no_successor_guards",
        "launch_contract_sha256", "prepared_plan_sha256",
    }
    if (set(prepared) != plan_fields or prepared.get("schema_version") != 2 or
            prepared.get("kind") != "kauri-n7-fixed-e0-control-plan-v2" or
            prepared.get("state") != "PREPARED_EXTERNAL_APPROVAL_REQUIRED" or
            prepared.get("prepared_plan_sha256") != _digest_without(prepared, "prepared_plan_sha256") or
            prepared.get("base_plan") != artifacts["base_plan"] or
            prepared.get("base_plan_sha256") != base_plan.get("plan_sha256") or
            prepared.get("launch_contract_sha256") != _sha256(_canonical(_launch_contract(prepared)))):
        raise ValidationError("prepared control authority plan drift")
    request_fields = {
        "schema_version", "kind", "prepared_plan_sha256", "base_plan_sha256", "run_id",
        "epoch0", "launch_contract_sha256", "manager_command_sha256",
        "replica_1_launch_argv_sha256", "hard_timeout_seconds", "declared_ports", "no_retry",
        "claim_boundary",
    }
    expected_request = {
        "schema_version": 2,
        "kind": "kauri-n7-fixed-e0-control-authorization-request-v2",
        "prepared_plan_sha256": prepared["prepared_plan_sha256"],
        "base_plan_sha256": prepared["base_plan_sha256"], "run_id": prepared["run_id"],
        "epoch0": prepared["epoch0"], "launch_contract_sha256": prepared["launch_contract_sha256"],
        "manager_command_sha256": _sha256(_canonical(prepared["manager_command"])),
        "replica_1_launch_argv_sha256": prepared["bindings"]["replica_1_launch_argv_sha256"],
        "hard_timeout_seconds": prepared["hard_timeout_seconds"], "declared_ports": prepared["declared_ports"],
        "no_retry": True, "claim_boundary": "Approval inputs only; no process launched.",
    }
    approval_fields = {
        "schema_version", "kind", "request_sha256", "prepared_plan_sha256",
        "approval_reference", "approved_utc", "no_retry",
    }
    final_fields = {
        "schema_version", "kind", "state", "prepared_plan_sha256",
        "authorization_request_sha256", "authorization_sha256", "claim_boundary",
        "finalization_receipt_sha256",
    }
    if (set(request) != request_fields or request != expected_request or
            set(approval) != approval_fields or approval.get("schema_version") != 2 or
            approval.get("kind") != "kauri-n7-fixed-e0-control-authorization-v2" or
            approval.get("request_sha256") != _sha256(request_bytes) or
            approval.get("prepared_plan_sha256") != prepared["prepared_plan_sha256"] or
            approval.get("no_retry") is not True or not isinstance(approval.get("approval_reference"), str) or
            not approval["approval_reference"].strip() or not isinstance(approval.get("approved_utc"), str) or
            not approval["approved_utc"].endswith("Z") or set(finalization) != final_fields or
            finalization.get("schema_version") != 2 or
            finalization.get("kind") != "kauri-n7-fixed-e0-control-finalization-receipt-v2" or
            finalization.get("state") != "FINALIZED_EXECUTION_EXPLICITLY_REQUIRED" or
            finalization.get("prepared_plan_sha256") != prepared["prepared_plan_sha256"] or
            finalization.get("authorization_request_sha256") != _sha256(request_bytes) or
            finalization.get("authorization_sha256") != _sha256(approval_bytes) or
            finalization.get("finalization_receipt_sha256") != _digest_without(finalization, "finalization_receipt_sha256")):
        raise ValidationError("control authority receipt chain drift")
    adaptive = _load_adaptive_runner()
    if base_plan.get("plan_sha256") != adaptive.adapter._plan_digest(base_plan):
        raise ValidationError("base plan digest drift")
    try:
        manager, replicas = adaptive.adapter._verify_executable_local_plan(root, base_plan)
        manager, replicas, bindings = adaptive._final_commands(
            root, base_plan, manager, replicas,
            hard_timeout_seconds=prepared["hard_timeout_seconds"],
        )
    except (KeyError, TypeError, adaptive.ProducerError, adaptive.adapter.AdapterError) as exc:
        raise ValidationError(f"cannot rederive prepared control argv: {exc}") from exc
    manager = _strip_successor_options(manager)
    app, manager_binary = Path(replicas[0][0]).resolve(), Path(manager[0]).resolve()
    expected_executables = {
        "hotstuff_app": _external_descriptor(prepared["executables"].get("hotstuff_app"), "hotstuff app"),
        "adaptation_manager": _external_descriptor(prepared["executables"].get("adaptation_manager"), "adaptation manager"),
    }
    if (prepared.get("manager_command") != list(manager) or
            prepared.get("replica_commands") != [list(command) for command in replicas] or
            prepared.get("bindings") != bindings or prepared.get("executables") != expected_executables or
            expected_executables != artifacts["executables"] or
            expected_executables["hotstuff_app"] != {"path": str(app), "sha256": _sha256(app.read_bytes())} or
            expected_executables["adaptation_manager"] != {"path": str(manager_binary), "sha256": _sha256(manager_binary.read_bytes())} or
            prepared.get("declared_ports") != list(adaptive._declared_ports(root, base_plan, manager))):
        raise ValidationError("prepared argv or executable identity is not rederived")
    return prepared


def validate_raw_bundle(run_root: Path, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one retained, fixed-E0 control bundle without launching anything.

    The receipt pins every event stream by path and digest before parsing it.
    It deliberately accepts no caller-supplied event arrays.
    """
    if run_root.is_symlink() or not run_root.is_dir():
        raise ValidationError("control run root is invalid")
    required = {"schema_version", "kind", "contract", "artifacts"}
    if (not isinstance(receipt, Mapping) or set(receipt) != required or
            receipt.get("schema_version") != 2 or
            receipt.get("kind") != "kauri-n7-fixed-e0-control-raw-bundle-v2"):
        raise ValidationError("control raw receipt schema drift")
    contract = receipt.get("contract")
    if not isinstance(contract, Mapping) or contract.get("state") != "FROZEN_EXECUTION_NO_SUCCESSOR":
        raise ValidationError("control raw receipt is not a frozen no-successor contract")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {
        "manager_events", "replica_streams", "fault_window_arm", "omission_gate", "cleanup", "manifest",
        "prepared_plan", "authorization_request", "approved_authorization",
        "finalization_receipt", "base_plan", "executables",
    }:
        raise ValidationError("control raw receipt artifact set drift")
    streams = artifacts["replica_streams"]
    if not isinstance(streams, Mapping) or set(streams) != {f"replica-{item}" for item in _REPLICAS}:
        raise ValidationError("control raw receipt replica artifact set drift")
    run_id = contract.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValidationError("control raw receipt run ID is invalid")
    manager_events, manager_line_hashes = _read_jsonl(
        run_root, artifacts["manager_events"], run_id=run_id, retain_line_hashes=True
    )
    replica_events = {
        source: _read_jsonl(run_root, descriptor, run_id=run_id)
        for source, descriptor in streams.items()
    }
    prepared = _verify_v2_authority_chain(run_root, artifacts)
    if prepared.get("run_id") != run_id or prepared.get("epoch0") != contract.get("epoch0"):
        raise ValidationError("raw contract differs from the independently replayed authority chain")
    if prepared.get("hard_timeout_seconds") != 600:
        raise ValidationError("control hard timeout differs from the frozen 600-second manifest")
    # Hash the two one-shot causal artifacts and cleanup. Their exact semantic
    # binding is verified by the source events and contract below.
    causal_hashes: dict[str, str] = {}
    causal_bytes: dict[str, bytes] = {}
    for name in ("fault_window_arm", "omission_gate", "cleanup", "manifest"):
        descriptor = artifacts[name]
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "sha256"}:
            raise ValidationError("control raw receipt causal descriptor drift")
        path = _safe_child(run_root, descriptor.get("path"))
        expected = _hex64(descriptor.get("sha256"), "control causal artifact hash")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValidationError("control raw receipt causal artifact hash drift")
        causal_hashes[name] = expected
        causal_bytes[name] = path.read_bytes()
    try:
        arm_document = json.loads(causal_bytes["fault_window_arm"].decode("utf-8"))
        gate_document = json.loads(causal_bytes["omission_gate"].decode("utf-8"))
        cleanup_document = json.loads(causal_bytes["cleanup"].decode("utf-8"))
        manifest_document = json.loads(causal_bytes["manifest"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("control causal artifact is not JSON") from exc
    epoch0 = contract["epoch0"]
    if (not isinstance(manifest_document, Mapping) or
            manifest_document.get("manifest_sha256") != contract["manifest_sha256"] or
            causal_bytes["manifest"] != (Path(__file__).resolve().parent / "matched_manifest.json").read_bytes()):
        raise ValidationError("control manifest bytes do not bind the frozen manifest hash")
    if (not isinstance(arm_document, Mapping) or arm_document.get("run_id") != run_id or
            type(arm_document.get("epoch_number")) is not int or arm_document["epoch_number"] != 0 or
            arm_document.get("epoch_digest") != epoch0["epoch_digest"]):
        raise ValidationError("control arm bytes do not bind the fixed-E0 contract")
    required_gate_fields = {
        "schema_version", "kind", "profile_sha256", "tree_file_sha256", "epoch_digest",
        "replica_id", "launch_argv_sha256", "manager_run_id", "manager_source_instance",
        "manager_source_sequence", "fault_window_arm_event_sha256", "activation_monotonic_ns",
    }
    if (not isinstance(gate_document, Mapping) or set(gate_document) != required_gate_fields or
            type(gate_document.get("schema_version")) is not int or gate_document["schema_version"] != 1 or
            gate_document.get("kind") != "kauri-n7-static-aggregate-omission-gate-v1" or
            gate_document.get("manager_run_id") != run_id or
            gate_document.get("epoch_digest") != epoch0["epoch_digest"] or
            type(gate_document.get("replica_id")) is not int or gate_document["replica_id"] != 1 or
            type(gate_document.get("manager_source_sequence")) is not int or
            gate_document.get("manager_source_sequence") != contract["fault_window_arm"]["source_sequence"] or
            not isinstance(gate_document.get("manager_source_instance"), str) or
            not gate_document["manager_source_instance"] or
            gate_document.get("fault_window_arm_event_sha256") != contract["fault_window_arm"]["event_sha256"] or
            type(gate_document.get("activation_monotonic_ns")) is not int or
            gate_document["activation_monotonic_ns"] <= 0 or
            any(_hex64(gate_document.get(field), f"control gate {field}") is None
                for field in ("profile_sha256", "tree_file_sha256", "launch_argv_sha256")) or
            causal_hashes["omission_gate"] != contract["omission_gate_sha256"]):
        raise ValidationError("control gate bytes do not bind the arm event")
    _validate_cleanup_document(cleanup_document, run_id)
    if ("--transition-request" in prepared.get("manager_command", []) or
            "--bundle-output" in prepared.get("manager_command", []) or
            "--fault-window-arm-control-only" not in prepared.get("manager_command", [])):
        raise ValidationError("control plan does not prove native no-successor mode")
    arm = contract["fault_window_arm"]
    arm_events = [
        event for event in manager_events
        if event.get("source_sequence") == arm["source_sequence"]
    ]
    if (len(arm_events) != 1 or not isinstance(arm_events[0].get("payload"), Mapping) or
            arm_events[0]["payload"].get("fault_window_arm_sha256") != causal_hashes["fault_window_arm"] or
            arm_events[0].get("source_instance") != gate_document["manager_source_instance"] or
            manager_line_hashes.get(contract["fault_window_arm"]["source_sequence"])
            != contract["fault_window_arm"]["event_sha256"]):
        raise ValidationError("control raw receipt does not bind arm event to arm bytes")
    injections = [
        event for event in replica_events["replica-1"]
        if event.get("event_type") == "fault.injection_armed"
    ]
    injection_fields = {
        "actor", "gate_sha256", "manager_fault_window_arm_event_sha256",
        "profile_sha256", "tree_file_sha256", "launch_argv_sha256",
        "activation_monotonic_ns",
    }
    if (len(injections) != 1 or not isinstance(injections[0].get("payload"), Mapping) or
            set(injections[0]["payload"]) != injection_fields or
            type(injections[0]["payload"].get("actor")) is not int or
            injections[0]["payload"]["actor"] != 1 or
            type(injections[0]["payload"].get("activation_monotonic_ns")) is not int or
            injections[0]["payload"].get("gate_sha256") != causal_hashes["omission_gate"] or
            injections[0]["payload"].get("manager_fault_window_arm_event_sha256") != contract["fault_window_arm"]["event_sha256"] or
            any(injections[0]["payload"].get(field) != gate_document[field]
                for field in ("profile_sha256", "tree_file_sha256", "launch_argv_sha256")) or
            injections[0]["payload"].get("activation_monotonic_ns") != gate_document["activation_monotonic_ns"]):
        raise ValidationError("control raw receipt injection does not bind its gate bytes")
    result = validate_control(contract, manager_events, replica_events)
    if not (gate_document["activation_monotonic_ns"] <= injections[0]["source_monotonic_ns"] < result["anchor_monotonic_ns"]):
        raise ValidationError("control gate/injection/anchor chronology drift")
    horizon_end = result["horizon_end_monotonic_ns"]
    if any(not events or events[-1].get("source_monotonic_ns", 0) < horizon_end
           for events in replica_events.values()):
        raise ValidationError("control replica raw streams do not cover first omission plus 60s")
    observations = [
        event for event in manager_events
        if event.get("event_type") == "fixed_e0_control.observation"
        and isinstance(event.get("payload"), Mapping)
        and set(event["payload"]) == {"fault_window_arm_sha256"}
        and event["payload"].get("fault_window_arm_sha256") == causal_hashes["fault_window_arm"]
        and event.get("source_monotonic_ns", 0) >= horizon_end
    ]
    if not observations:
        raise ValidationError("control manager lacks a post-horizon pinned-arm observation")
    _validate_clean_fixed_e0_terminal(
        manager_events, run_id=run_id, epoch_digest=epoch0["epoch_digest"],
        horizon_end_ns=horizon_end,
    )
    return result
