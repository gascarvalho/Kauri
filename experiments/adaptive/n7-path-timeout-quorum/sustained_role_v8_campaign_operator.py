"""Read-only sealed-slot verifier for the prospective W19 v8 campaign.

It is deliberately not a launcher and does not issue a campaign result.  It
replays the strongest presently materialized component closure, but the public
entry point remains fail-closed until a native lifecycle/post-scope producer
exists.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import sys
from typing import Any, Mapping


ARM_RECEIPT_KIND = "kauri-n7-sustained-role-v8-replay-receipt-v1"
PROFILE_ID = "n7-sustained-role-proposal-boundary-v8"
SOURCES = ("adaptive-manager", *(f"replica-{item}" for item in range(7)))
ABORTS = (
    "runtime/sustained-role-abort-finalization-v8.json", "sustained-role-v8-abort.json",
    "runtime/sustained-role-campaign-staging-abort.json", "runtime/sustained-role-campaign-launch-abort.json",
    "sustained-role-fixed-e0-abort.json", "sustained-role-adaptive-e1-abort.json",
    "runtime/sustained-role-v8-campaign-abort.json", "runtime/sustained-role-v8-abort.json",
    "runtime/sustained-role-v8-materialization-abort.json",
    "runtime/sustained-role-v8-full-input-abort.json",
)
PLAN_PATH = "runtime/sustained-role-v8-full-input-plan.json"
REQUEST_PATH = "runtime/sustained-role-v8-full-input-request.json"
APPROVAL_PATH = "runtime/sustained-role-v8-adaptive-child-launch-authorization.json"
INTENT_PATH = "runtime/sustained-role-v8-adaptive-child-launch-intent.json"
COMMON_TERMINAL = "runtime/sustained-role-v8-pilot-terminal.json"
_DEFAULT_ARTIFACT_LIMIT = 16 * 1024 * 1024
_EXECUTABLE_ARTIFACT_LIMIT = 128 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")

_RAW_SPEC = importlib.util.spec_from_file_location(
    "w19_v8_campaign_operator_raw_replay",
    Path(__file__).resolve().with_name("sustained_role_v8_raw_replay.py"),
)
assert _RAW_SPEC and _RAW_SPEC.loader
raw_replay = importlib.util.module_from_spec(_RAW_SPEC)
sys.modules[_RAW_SPEC.name] = raw_replay
_RAW_SPEC.loader.exec_module(raw_replay)


def _load(name: str, filename: str) -> Any:
    specification = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().with_name(filename))
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


full_input = _load(
    "w19_v8_campaign_operator_full_input",
    "sustained_role_v8_full_input_producer.py",
)
validator_v8 = _load(
    "w19_v8_campaign_operator_validator",
    "sustained_role_v8_validator.py",
)
tree_runner = _load(
    "w19_v8_campaign_operator_tree_runner",
    "runner.py",
)


class V8SlotError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii") + b"\n"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _strict(raw: bytes, label: str, *, canonical: bool = True) -> dict[str, Any]:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise V8SlotError(f"{label} repeats a JSON key")
            value[key] = item
        return value
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        encoded = _canonical(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise V8SlotError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict) or (canonical and raw != encoded):
        raise V8SlotError(f"{label} is not canonical JSON")
    return value


def _safe(root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise V8SlotError(f"{label} path is invalid")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise V8SlotError(f"{label} path escapes cell root")
    candidate = root / path
    if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents if parent != root.parent):
        raise V8SlotError(f"{label} path traverses a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise V8SlotError(f"{label} path is unavailable") from exc
    return candidate


def _descriptor(root: Path, value: object, label: str, *,
                maximum_bytes: int = _DEFAULT_ARTIFACT_LIMIT) -> bytes:
    if not isinstance(value, Mapping) or set(value) != {"path", "size_bytes", "sha256"}:
        raise V8SlotError(f"{label} descriptor schema drifted")
    digest = value.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in _HEX for char in digest):
        raise V8SlotError(f"{label} descriptor digest is invalid")
    path = _safe(root, value.get("path"), label)
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or
            type(value["size_bytes"]) is not int or value["size_bytes"] != info.st_size or
            info.st_size > maximum_bytes):
        raise V8SlotError(f"{label} is not a bounded regular file")
    raw = path.read_bytes()
    if _sha(raw) != digest:
        raise V8SlotError(f"{label} differs from its sealed SHA-256")
    return raw


def _events(raw: bytes, label: str) -> list[dict[str, Any]]:
    # Native event writers retain their own field order. Their original
    # bytes remain SHA-bound; only operator-owned receipts are canonical JSON.
    if not raw.endswith(b"\n"):
        raise V8SlotError(f"{label} has incomplete JSONL framing")
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        event = _strict(line + b"\n", label, canonical=False)
        if type(event.get("source_monotonic_ns")) is not int or event["source_monotonic_ns"] < 0:
            raise V8SlotError(f"{label} contains an invalid raw clock")
        events.append(event)
    if not events:
        raise V8SlotError(f"{label} is empty")
    return events


def _reject_aborts(root: Path) -> None:
    if any((root / abort).exists() or (root / abort).is_symlink() for abort in ABORTS):
        raise V8SlotError("v8 cell has a sealed abort and is ineligible")


def _authority(root: Path, receipt: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reopen the genuine full-input/child authority chain."""
    plan_raw = _descriptor(root, receipt["plan"], "execution plan")
    request_raw = _descriptor(root, receipt["request"], "authorization request")
    approval_raw = _descriptor(root, receipt["external_authorization"], "archived authorization")
    intent_raw = _descriptor(root, receipt["launch_intent"], "launch intent")
    if (receipt["plan"].get("path") != PLAN_PATH or
            receipt["request"].get("path") != REQUEST_PATH or
            receipt["external_authorization"].get("path") != APPROVAL_PATH or
            receipt["launch_intent"].get("path") != INTENT_PATH):
        raise V8SlotError("authority artifacts are not at their canonical paths")
    plan = _strict(plan_raw, "execution plan")
    request = _strict(request_raw, "authorization request")
    approval = _strict(approval_raw, "archived authorization")
    intent = _strict(intent_raw, "launch intent")
    try:
        full_input.verify(root, expected_request_sha256=_sha(request_raw))
    except full_input.FullInputError as exc:
        raise V8SlotError(f"full-input closure rejected: {exc}") from exc
    if (request.get("plan_sha256") != plan.get("plan_sha256") or
            request.get("run_id") != receipt["run_id"] or
            request.get("no_launch") is not True or request.get("no_retry") is not True):
        raise V8SlotError("authorization request is not bound to the sealed plan")
    expected_approval = {
        "schema_version", "kind", "request_sha256", "plan_sha256", "run_id", "no_retry",
    }
    if (set(approval) != expected_approval or approval.get("schema_version") != 1 or
            approval.get("kind") != "kauri-n7-sustained-role-v8-adaptive-child-launch-authorization-v1" or
            approval.get("request_sha256") != _sha(request_raw) or
            approval.get("plan_sha256") != plan.get("plan_sha256") or
            approval.get("run_id") != receipt["run_id"] or approval.get("no_retry") is not True):
        raise V8SlotError("archived authorization does not bind the exact plan/request")
    expected_intent = {
        "schema_version", "kind", "state", "plan_sha256", "request_sha256",
        "approval_sha256", "build_binding", "run_id", "no_retry", "hard_scope_seconds",
        "claim_eligible", "figure_eligible", "intent_sha256",
    }
    intent_without_hash = {key: value for key, value in intent.items() if key != "intent_sha256"}
    if (set(intent) != expected_intent or intent.get("schema_version") != 1 or
            intent.get("kind") != "kauri-n7-sustained-role-v8-adaptive-child-launch-intent-v1" or
            intent.get("state") != "PRESPAWN_INTENT_SEALED_NO_LAUNCH" or
            intent.get("plan_sha256") != plan.get("plan_sha256") or
            intent.get("request_sha256") != _sha(request_raw) or
            intent.get("approval_sha256") != _sha(approval_raw) or
            intent.get("run_id") != receipt["run_id"] or intent.get("no_retry") is not True or
            intent.get("hard_scope_seconds") != 210 or intent.get("claim_eligible") is not False or
            intent.get("figure_eligible") is not False or
            intent.get("intent_sha256") != _sha(_canonical(intent_without_hash))):
        raise V8SlotError("launch intent does not bind the exact immutable authority chain")
    build = intent.get("build_binding")
    expected_build = {
        "kind", "state", "repository_revision", "target_host", "linux_boot_id",
        "receipt_sha256", "binaries", "live_attested", "launch_eligible",
    }
    binary_names = {
        "hotstuff_app": "hotstuff_app", "adaptation_manager": "adaptation_manager",
        "hotstuff_keygen": "hotstuff_keygen", "hotstuff_tls_keygen": "hotstuff_tls_keygen",
        "epoch0_treefile_digest": "e0_helper",
    }
    artifacts = plan.get("artifacts")
    if (not isinstance(build, Mapping) or set(build) != expected_build or
            build.get("kind") != "kauri-n7-sustained-role-v8-build-binding-v1" or
            build.get("state") != "BUILD_COMPONENT_VERIFIED_NOT_LIVE_ATTESTED" or
            build.get("repository_revision") != plan.get("repository_revision") or
            not isinstance(build.get("target_host"), str) or not build["target_host"] or
            not isinstance(build.get("linux_boot_id"), str) or not build["linux_boot_id"] or
            not isinstance(artifacts, Mapping) or
            not isinstance(artifacts.get("build_receipt"), Mapping) or
            build.get("receipt_sha256") != artifacts["build_receipt"].get("sha256") or
            build.get("live_attested") is not False or build.get("launch_eligible") is not False or
            not isinstance(build.get("binaries"), Mapping) or
            set(build["binaries"]) != set(binary_names)):
        raise V8SlotError("launch intent lacks the exact five-binary build component binding")
    for receipt_name, artifact_name in binary_names.items():
        row = build["binaries"][receipt_name]
        descriptor = artifacts.get(artifact_name)
        if (not isinstance(row, Mapping) or set(row) != {"path", "size_bytes", "sha256"} or
                not isinstance(descriptor, Mapping) or dict(row) != dict(descriptor)):
            raise V8SlotError("launch intent binary binding differs from the full-input plan")
    if (receipt.get("repository_revision") != plan.get("repository_revision") or
            receipt.get("profile") != {"id": plan.get("profile_id"), "sha256": plan.get("profile_sha256")}):
        raise V8SlotError("receipt identity differs from the full-input plan")
    return plan, intent


def _provenance(root: Path, receipt: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    provenance = receipt.get("provenance")
    required = {
        "build_receipt", "launch_arguments", "preparation", "e0_identity", "v8_profile",
        "selection_profile", "epoch0_tree", "main_config", "replica_configs", "executables",
    }
    if not isinstance(provenance, Mapping) or set(provenance) != required:
        raise V8SlotError("sealed v8 provenance schema drifted")
    plan_artifacts = plan.get("artifacts")
    if not isinstance(plan_artifacts, Mapping):
        raise V8SlotError("full-input plan artifacts are absent")
    scalar = required - {"replica_configs", "executables"}
    for name in scalar:
        if provenance[name] != plan_artifacts.get(name):
            raise V8SlotError(f"{name} provenance differs from the full-input plan")
        _descriptor(root, provenance[name], name)
    replicas = provenance["replica_configs"]
    if replicas != plan_artifacts.get("replica_configs") or not isinstance(replicas, list) or len(replicas) != 7:
        raise V8SlotError("replica-config provenance differs from the full-input plan")
    for replica, descriptor in enumerate(replicas):
        _descriptor(root, descriptor, f"replica-{replica} config")
    executables = provenance["executables"]
    executable_names = {
        "hotstuff_app", "adaptation_manager", "hotstuff_keygen", "hotstuff_tls_keygen", "e0_helper",
    }
    if not isinstance(executables, Mapping) or set(executables) != executable_names:
        raise V8SlotError("sealed v8 executable provenance drifted")
    for name in executable_names:
        if executables[name] != plan_artifacts.get(name):
            raise V8SlotError(f"executable {name} differs from the full-input plan")
        _descriptor(root, executables[name], f"executable {name}",
                    maximum_bytes=_EXECUTABLE_ARTIFACT_LIMIT)


def _cleanup(root: Path, descriptor: object, *, run_id: str) -> None:
    if not isinstance(descriptor, Mapping) or descriptor.get("path") != "runtime/cleanup-receipt.json":
        raise V8SlotError("cleanup receipt path is not canonical")
    value = _strict(_descriptor(root, descriptor, "cleanup receipt"), "cleanup receipt")
    if (set(value) != {"schema_version", "run_id", "complete", "processes"} or
            value.get("schema_version") != 1 or value.get("run_id") != run_id or
            value.get("complete") is not True or not isinstance(value.get("processes"), list) or
            len(value["processes"]) != 8):
        raise V8SlotError("cleanup receipt is incomplete")
    for expected, process in zip(SOURCES, value["processes"]):
        if (not isinstance(process, Mapping) or
                set(process) != {"source_id", "pid", "pgid", "returncode", "termination"} or
                process.get("source_id") != expected or type(process.get("pid")) is not int or
                process["pid"] <= 0 or type(process.get("pgid")) is not int or process["pgid"] <= 0 or
                type(process.get("returncode")) is not int or
                process.get("termination") not in {"clean-exit", "terminated"}):
            raise V8SlotError("cleanup receipt process closure drifted")


def _common_terminal(root: Path, *, run_id: str) -> None:
    path = _safe(root, COMMON_TERMINAL, "common terminal")
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or
            info.st_size > _DEFAULT_ARTIFACT_LIMIT):
        raise V8SlotError("common terminal is not a bounded regular file")
    raw = path.read_bytes()
    value = _strict(raw, "common terminal")
    required = {"schema_version", "kind", "state", "run_id", "no_retry",
                "claim_eligible", "figure_eligible"}
    if (set(value) != required or value.get("schema_version") != 1 or
            value.get("kind") != "kauri-n7-sustained-role-v8-pilot-terminal-v1" or
            value.get("state") != "SEALED_EXPLORATORY_NO_CLAIM" or
            value.get("run_id") != run_id or value.get("no_retry") is not True or
            value.get("claim_eligible") is not False or value.get("figure_eligible") is not False):
        raise V8SlotError("common terminal is not the exact sealed no-claim terminal")


def _one_command_driver_metric(*, replay: Mapping[str, Any], expected_identity: Mapping[str, Any],
                               replica_events: Mapping[int, list[dict[str, Any]]]) -> None:
    """Bind the cadence metric to the profile's replica-local one-command driver.

    This is not external-client throughput.  Each counted authoritative block
    and all seven matching observations must carry exactly the pinned one
    transaction/command.
    """
    window = replay.get("measurement_window")
    if (not isinstance(window, list) or len(window) != 2 or
            any(type(value) is not int for value in window)):
        raise V8SlotError("validator replay lacks the exact 40-second metric window")
    start_ns, end_ns = window
    authoritative: set[tuple[int, str]] = set()
    for event in replica_events[2]:
        payload = event.get("payload")
        proof = payload.get("decision_proof") if isinstance(payload, Mapping) else None
        timestamp = event.get("source_monotonic_ns")
        if (event.get("event_type") == "block.committed" and type(timestamp) is int and
                start_ns <= timestamp < end_ns and isinstance(proof, Mapping) and
                proof.get("epoch_number") == 1 and
                proof.get("epoch_digest") == expected_identity.get("successor_epoch_digest")):
            if payload.get("transaction_count") != 1:
                raise V8SlotError("counted E1 authority is not a one-command synthetic-driver block")
            authoritative.add((payload["block_height"], payload["block_hash"]))
    if len(authoritative) != replay.get("common_e1_committed_blocks"):
        raise V8SlotError("one-command authority count differs from the replayed common metric")
    for replica, events in replica_events.items():
        for height, block_hash in authoritative:
            witnesses = [event for event in events
                         if event.get("event_type") == "block.commit_observed" and
                         isinstance(event.get("payload"), Mapping) and
                         event["payload"].get("block_height") == height and
                         event["payload"].get("block_hash") == block_hash]
            if len(witnesses) != 1 or witnesses[0]["payload"].get("transaction_count") != 1:
                raise V8SlotError(
                    f"replica-{replica} does not bind the common metric to one command")


def _component_replay(root: Path, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Replay actual v8 bytes, while explicitly stopping short of outer success."""
    _reject_aborts(root)
    plan, _intent = _authority(root, receipt)
    _provenance(root, receipt, plan)
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {"bundle"}:
        raise V8SlotError("sealed adaptive receipt lacks the exact signed E1 bundle")
    bundle_bytes = _descriptor(root, artifacts["bundle"], "signed E1 bundle")
    plan_artifacts = plan["artifacts"]
    issuer_raw = _descriptor(root, plan_artifacts["issuer_public_key"], "issuer public key")
    try:
        issuer_public_key = issuer_raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise V8SlotError("issuer public key is not ASCII") from exc
    if issuer_raw != (issuer_public_key + "\n").encode("ascii"):
        raise V8SlotError("issuer public key is not canonical newline text")
    parsed_raw: dict[str, list[dict[str, Any]]] = {}
    raw_bytes: dict[str, bytes] = {}
    log_bytes: dict[str, bytes] = {}
    for label in ("raw", "logs"):
        streams = receipt[label]
        if not isinstance(streams, Mapping) or set(streams) != set(SOURCES):
            raise V8SlotError(f"sealed {label} lack the exact eight sources")
        for source in SOURCES:
            raw = _descriptor(root, streams[source], f"{label} {source}")
            if label == "raw":
                raw_bytes[source] = raw
                parsed_raw[source] = _events(raw, f"raw {source}")
            else:
                log_bytes[source] = raw
    try:
        fault_observation = raw_replay.replay_fault_evidence(
            run_id=receipt["run_id"],
            manager_raw=raw_bytes["adaptive-manager"],
            replica_raw=[raw_bytes[f"replica-{replica}"] for replica in range(7)],
            replica_logs=[log_bytes[f"replica-{replica}"] for replica in range(7)],
        )
    except raw_replay.V8RawReplayError as exc:
        raise V8SlotError(f"fixed raw replay rejected sealed bytes: {exc}") from exc
    process_rows = plan.get("processes")
    if not isinstance(process_rows, list):
        raise V8SlotError("full-input plan lacks process identities")
    instances = {row.get("source_id"): row.get("source_instance") for row in process_rows
                 if isinstance(row, Mapping)}
    if set(instances) != set(SOURCES) or any(
            events[0].get("source_instance") != instances[source]
            for source, events in parsed_raw.items()):
        raise V8SlotError("raw source instance differs from the full-input launch plan")
    anchor = receipt.get("anchor")
    derived = {
        "source_id": fault_observation["anchor_source_id"],
        "source_sequence": fault_observation["anchor_source_sequence"],
        "line_sha256": fault_observation["anchor_line_sha256"],
        "monotonic_ns": fault_observation["anchor_decision_monotonic_ns"],
    }
    if not isinstance(anchor, Mapping) or dict(anchor) != derived:
        raise V8SlotError("sealed v8 anchor is not derived from replica-1 raw evidence")
    window = receipt.get("fault_window")
    schedule = plan.get("scheduled_window")
    anchor_ns = int(derived["monotonic_ns"])
    if (not isinstance(window, Mapping) or
            set(window) != {"start_monotonic_ns", "end_monotonic_ns", "coverage_through_horizon"} or
            not isinstance(schedule, Mapping) or window.get("start_monotonic_ns") != schedule.get("start_ns") or
            window.get("end_monotonic_ns") != schedule.get("end_ns") or
            window.get("coverage_through_horizon") is not True or
            type(schedule.get("start_ns")) is not int or type(schedule.get("end_ns")) is not int or
            not schedule["start_ns"] <= anchor_ns <= schedule["start_ns"] + 10_000_000_000 or
            schedule["end_ns"] < anchor_ns + 72_000_000_000):
        raise V8SlotError("sealed v8 anchor or scheduled physical coverage drifted")
    tree_path = _safe(root, plan_artifacts["epoch0_tree"].get("path"), "Epoch-0 tree")
    try:
        trees = tree_runner.parse_tree_file(tree_path)
    except Exception as exc:
        raise V8SlotError("archived Epoch-0 tree is invalid") from exc
    roots = tuple(index for index, row in enumerate(trees) if row.index(1) == 0)
    internal = tuple(index for index, row in enumerate(trees) if 0 < row.index(1) < 3)
    leaves = tuple(index for index, row in enumerate(trees) if row.index(1) >= 3)
    if (len(trees) != 7 or (roots, internal, leaves) != ((1,), (4, 5, 6), (0, 2, 3))):
        raise V8SlotError("archived Epoch-0 tree lacks the frozen actor-1 internal paths")
    starts = [event for event in parsed_raw["adaptive-manager"]
              if event.get("event_type") == "adaptive_v2.convergence_started"]
    if len(starts) != 1 or not isinstance(starts[0].get("payload"), Mapping):
        raise V8SlotError("manager raw lacks one native convergence start")
    expected_identity = dict(starts[0]["payload"])
    expected_identity.pop("cycle_ordinal", None)
    if (fault_observation.get("anchor_epoch_digest") != expected_identity.get("predecessor_epoch_digest") or
            fault_observation.get("anchor_tree_id") != 4):
        raise V8SlotError("physical anchor differs from the selected predecessor identity")
    try:
        replay = validator_v8.validate_v8_raw_contract(
            anchor_monotonic_ns=anchor_ns,
            expected_identity=expected_identity,
            manager_events=parsed_raw["adaptive-manager"],
            replica_events={replica: parsed_raw[f"replica-{replica}"] for replica in range(7)},
            bundle_bytes=bundle_bytes,
            issuer_public_key=issuer_public_key,
            predecessor_tree_ids=frozenset(range(len(trees))),
        )
    except validator_v8.V8ValidationError as exc:
        raise V8SlotError(f"signed v8 raw validator rejected sealed bytes: {exc}") from exc
    replica_events = {replica: parsed_raw[f"replica-{replica}"] for replica in range(7)}
    _one_command_driver_metric(
        replay=replay, expected_identity=expected_identity, replica_events=replica_events)
    late = fault_observation["late_e1_leaf_candidates_untrusted_pending_root_join"]
    if not any(candidate.get("epoch_digest") == expected_identity.get("successor_epoch_digest") and
               candidate.get("tree_id") in range(5) for candidate in late):
        raise V8SlotError("late physical exposure does not join the active signed E1 layout")
    _cleanup(root, receipt["cleanup"], run_id=receipt["run_id"])
    validator_record = receipt.get("validator")
    if (not isinstance(validator_record, Mapping) or
            set(validator_record) != {"path", "size_bytes", "sha256", "profile_id"} or
            validator_record.get("profile_id") != PROFILE_ID):
        raise V8SlotError("sealed v8 validator record is missing")
    validator_raw = _descriptor(
        root, {key: validator_record[key] for key in ("path", "size_bytes", "sha256")},
        "validator record")
    if validator_raw != _canonical(replay):
        raise V8SlotError("validator record differs from immediate fixed replay")
    _reject_aborts(root)
    return {**replay, "authority_state": "COMPONENT_ONLY_NOT_LIVE_ATTESTED",
            "lifecycle_bound": False, "post_scope_bound": False,
            "workload_basis": "replica_local_synthetic_one_command_proposal_driver",
            "external_client_throughput": False,
            "claim_eligible": False, "figure_eligible": False}


def verify_sealed_slot(root: Path, *, receipt_relative: str,
                       expected_receipt_sha256: str) -> dict[str, object]:
    """Reopen one sealed cell with the fixed byte-level replay, then refuse it.

    Signed E1, exact generated inputs, source instances, cleanup, and the
    equal-window metric are replayed here.  Native lifecycle, authenticated
    live host/booking, and post-scope evidence do not yet have a v8 producer,
    so this public entry point intentionally has no success return.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise V8SlotError("v8 cell root is not a regular directory")
    root = root.resolve()
    if not isinstance(expected_receipt_sha256, str) or len(expected_receipt_sha256) != 64:
        raise V8SlotError("sealed receipt SHA-256 is invalid")
    _reject_aborts(root)
    receipt_path = _safe(root, receipt_relative, "sealed receipt")
    receipt_raw = receipt_path.read_bytes()
    if _sha(receipt_raw) != expected_receipt_sha256:
        raise V8SlotError("sealed receipt differs from its manifest pin")
    receipt = _strict(receipt_raw, "sealed receipt")
    required = {"schema_version", "kind", "run_id", "profile", "repository_revision", "arm", "no_retry",
                "claim_eligible", "figure_eligible", "plan", "request", "external_authorization",
                "launch_intent", "artifacts", "anchor", "fault_window", "provenance", "raw", "logs",
                "cleanup", "validator"}
    if (set(receipt) != required or receipt.get("schema_version") != 1 or receipt.get("kind") != ARM_RECEIPT_KIND or
            receipt.get("arm") != "adaptive_e1" or receipt.get("no_retry") is not True or
            receipt.get("claim_eligible") is not False or receipt.get("figure_eligible") is not False or
            not isinstance(receipt.get("run_id"), str) or not receipt["run_id"] or
            not isinstance(receipt.get("repository_revision"), str) or len(receipt["repository_revision"]) != 40 or
            any(char not in _HEX for char in receipt["repository_revision"])):
        raise V8SlotError("sealed v8 receipt identity drifted")
    profile = receipt.get("profile")
    if (not isinstance(profile, Mapping) or set(profile) != {"id", "sha256"} or
            profile.get("id") != PROFILE_ID or
            not isinstance(profile.get("sha256"), str) or len(profile["sha256"]) != 64 or
            any(char not in _HEX for char in profile["sha256"])):
        raise V8SlotError("sealed v8 receipt profile schema drifted")
    _component_replay(root, receipt)
    _common_terminal(root, run_id=receipt["run_id"])
    _reject_aborts(root)
    raise V8SlotError(
        "sealed v8 signed-E1 component replay passed, but authenticated live host/booking, "
        "native lifecycle, and post-scope proof are not produced; no pilot or campaign verdict may be issued"
    )
