#!/usr/bin/env python3
"""One-shot, opt-in W19 adaptive-E1 raw-bundle launcher.

This boundary deliberately records native bytes and observations; it does not
interpret a bundle as a valid successor.  The separately owned validator
reopens the archived issuer key and signed bundle before accepting anything.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
KAURI = HERE.parents[2]
PLAN = Path("runtime/sustained-role-execution-plan.json")
REQUEST = Path("runtime/sustained-role-authorization-request.json")
APPROVAL = Path("runtime/sustained-role-adaptive-e1-launch-authorization.json")
ATTESTATION = Path("runtime/sustained-role-adaptive-e1-prearm-attestation.json")
FINALIZATION = Path("runtime/sustained-role-adaptive-e1-finalization.json")
CLEANUP = Path("runtime/cleanup-receipt.json")
RECEIPT = Path("sustained-role-adaptive-e1-raw-bundle-receipt.json")
ABORT = Path("sustained-role-adaptive-e1-abort.json")
KIND = "kauri-n7-sustained-role-raw-bundle-receipt-v1"
AUTH_KIND = "kauri-n7-sustained-role-adaptive-e1-launch-authorization-v1"
FINAL_KIND = "kauri-n7-sustained-role-adaptive-e1-launch-finalization-v1"
_REPLICA_SOURCES = tuple(f"replica-{i}" for i in range(7))
_HORIZON_NS, _E1_DEADLINE_NS = 60_000_000_000, 20_000_000_000


class LaunchError(RuntimeError):
    pass


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise LaunchError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fixed = _load("w19_fixed_e0_launcher_for_adaptive", HERE / "sustained_role_fixed_e0_launcher.py")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _option(argv: Sequence[str], option: str) -> str:
    return fixed._option(argv, option)


def _adaptive_manager(plan: Mapping[str, Any]) -> tuple[str, ...]:
    manager = plan.get("commands", {}).get("manager", {})
    argv = tuple(manager.get("argv", ())) if isinstance(manager, Mapping) else ()
    if not argv or "--transition-request" not in argv or "--bundle-output" not in argv:
        raise LaunchError("adaptive plan lacks its native signed successor bindings")
    if "--scheduled-fixed-e0-control" in argv or "--fault-window-arm-control-only" in argv:
        raise LaunchError("adaptive plan contains a fixed-E0 control option")
    for option in ("--issuer-id", "--issuer-private-key", "--transition-request", "--bundle-output"):
        _option(argv, option)
    return argv


def _safe_child(root: Path, raw: str, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise LaunchError(f"{label} escapes run root") from exc
    return path


def _first_anchor(streams: Mapping[str, Sequence[Mapping[str, Any]]]) -> Mapping[str, Any] | None:
    for event in streams.get("replica-1", ()):
        payload = event.get("payload") if isinstance(event, Mapping) else None
        if (event.get("event_type") == "fault.contribution_opportunity" and isinstance(payload, Mapping)
                and payload.get("actor") == 1 and payload.get("fault_mode") == "role_scoped_persistent_selected_omission_v1"
                and payload.get("physical_role") == "internal" and payload.get("scheduled_action") == "omit_aggregate"
                and isinstance(event.get("source_monotonic_ns"), int)):
            return event
    return None


def _sealed_anchor_and_coverage(root: Path) -> dict[str, Any]:
    """Hash the identical native internal-aggregate event used to arm E1."""
    raw = (root / "raw/replica-1.jsonl").read_bytes()
    anchor: dict[str, Any] | None = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LaunchError("replica-1 raw stream is not JSONL") from exc
        selected = _first_anchor({"replica-1": (event,)})
        if selected is not None:
            anchor = {"source_id": "replica-1", "source_sequence": event.get("source_sequence"), "line_sha256": _sha(line), "monotonic_ns": event.get("source_monotonic_ns")}
            break
    if anchor is None or type(anchor["source_sequence"]) is not int or type(anchor["monotonic_ns"]) is not int:
        raise LaunchError("no native actor-1 internal aggregate omission anchor was recorded")
    horizon = anchor["monotonic_ns"] + _HORIZON_NS
    for source in _REPLICA_SOURCES:
        path = root / f"raw/{source}.jsonl"
        if path.is_symlink() or not path.is_file(): raise LaunchError(f"missing raw stream for {source}")
        try: events = [json.loads(line) for line in path.read_bytes().splitlines()]
        except json.JSONDecodeError as exc: raise LaunchError(f"raw stream for {source} is not JSONL") from exc
        if not events or not isinstance(events[-1], Mapping) or events[-1].get("source_monotonic_ns", -1) < horizon:
            raise LaunchError("raw streams do not cover the selected anchor plus full 60-second horizon")
    return anchor


def _adaptive_manager_success_terminal(root: Path) -> None:
    """Require the native manager's successful E1 terminal independently.

    The manager legitimately exits after sealing its successor; forcing its
    JSONL stream to extend through the replicas' 60-second horizon would make
    a successful run look incomplete.  The seven replicas still must cover
    that entire horizon.
    """
    path = root / "raw/adaptive-manager.jsonl"
    if path.is_symlink() or not path.is_file():
        raise LaunchError("missing adaptive manager raw stream")
    try:
        events = [json.loads(line) for line in path.read_bytes().splitlines()]
    except json.JSONDecodeError as exc:
        raise LaunchError("adaptive manager raw stream is not JSONL") from exc
    for event in events:
        payload = event.get("payload") if isinstance(event, Mapping) else None
        if (event.get("event_type") == "adaptive_v2_session_terminal" and isinstance(payload, Mapping)
                and payload.get("outcome") == "advanced"
                and payload.get("reason") == "successor_converged"
                and payload.get("successor_epoch_number") == 1):
            return
    raise LaunchError("native adaptive manager success terminal is absent")


def _all_seven_e1_activated(streams: Mapping[str, Sequence[Mapping[str, Any]]], *, deadline_ns: int) -> bool:
    """Only bind raw native activation envelopes; bundle semantics stay external."""
    identity = None
    for replica in range(7):
        found = False
        for event in streams.get(f"replica-{replica}", ()):
            payload = event.get("payload") if isinstance(event, Mapping) else None
            if (event.get("event_type") == "epoch.activated" and isinstance(payload, Mapping)
                    and set(payload) == {"epoch_number", "tree_id", "epoch_digest", "activation_height"}
                    and payload.get("epoch_number") == 1 and type(payload.get("tree_id")) is int
                    and payload["tree_id"] in range(7)
                    and isinstance(payload.get("activation_height"), int) and isinstance(payload.get("epoch_digest"), str)
                    and isinstance(event.get("source_monotonic_ns"), int)
                    and event["source_monotonic_ns"] <= deadline_ns):
                # A replica can locally enter the same certified epoch while
                # a different tree is active at its observation instant.
                candidate = (payload["epoch_number"], payload["epoch_digest"], payload["activation_height"])
                if identity is None: identity = candidate
                elif identity != candidate: return False
                found = True; break
        if not found:
            return False
    return True


def _materialized_authorities(root: Path) -> tuple[Path, Path]:
    """Require the local adapter's pre-spawn authority archive.

    This launcher cannot derive a public key from the manager private-key
    argument nor invent an E0 identity.  Both must have been atomically
    written by ``local_adapter.materialize_local_plan`` before authorization.
    """
    plan_path = root / "local-launch-plan.json"
    plan, _ = fixed._read(plan_path, "local-adapter launch plan")
    if plan.get("state") != "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED":
        raise LaunchError("local adapter did not materialize the pre-spawn authority plan")
    e0_rel, issuer_rel = plan.get("e0_identity_receipt"), plan.get("issuer_public_key")
    if not isinstance(e0_rel, str) or not isinstance(issuer_rel, str):
        raise LaunchError("local adapter plan lacks E0 identity or issuer public key")
    e0 = _safe_child(root, e0_rel, "local-adapter E0 identity")
    issuer = _safe_child(root, issuer_rel, "local-adapter issuer public key")
    for path, digest, label in ((e0, plan.get("e0_identity_receipt_sha256"), "E0 identity"), (issuer, plan.get("issuer_public_key_sha256"), "issuer public key")):
        if path.is_symlink() or not path.is_file() or not isinstance(digest, str) or _sha(path.read_bytes()) != digest:
            raise LaunchError(f"local-adapter {label} artifact is absent or hash-drifted")
    return e0, issuer


def _issuer_path(root: Path, plan: Mapping[str, Any]) -> Path:
    _e0, candidate = _materialized_authorities(root)
    raw = candidate.read_bytes()
    if not raw.endswith(b"\n") or len(raw) != 67:
        raise LaunchError("issuer public key is not canonical compressed-key text")
    return candidate


def _transition_artifacts(root: Path, manager: Sequence[str], bundle: Path) -> tuple[Path, Path]:
    """Archive the manager's exact canonical request and its native snapshot.

    The snapshot name is declared by the frozen request; accepting a guessed
    sibling would turn a convenient layout into evidence authority.
    """
    try:
        request = json.loads(_option(manager, "--transition-request"))
    except json.JSONDecodeError as exc:
        raise LaunchError("manager transition request is not JSON") from exc
    if not isinstance(request, Mapping) or not isinstance(request.get("evidence_snapshot_path"), str):
        raise LaunchError("manager transition request lacks evidence snapshot path")
    request_path = root / "runtime/launch-inputs/transition-request.json"
    request_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fixed._write_once(request_path, request)
    declared_bundle = request.get("bundle_path")
    if not isinstance(declared_bundle, str) or not declared_bundle or not str(bundle).endswith(declared_bundle):
        raise LaunchError("native bundle path does not bind transition request")
    prefix = str(bundle)[:-len(declared_bundle)]
    snapshot = _safe_child(root, prefix + request["evidence_snapshot_path"], "manager evidence snapshot")
    if not snapshot.is_file() or snapshot.is_symlink():
        raise LaunchError("native manager did not seal its evidence snapshot")
    return request_path, snapshot


def _hex64(value: object, label: str) -> str:
    if (not isinstance(value, str) or len(value) != 64 or
            any(character not in "0123456789abcdef" for character in value)):
        raise LaunchError(f"{label} is not a lower-case SHA-256")
    return value


def _fault_window_arm_startup_path(root: Path, manager: Sequence[str]) -> Path:
    """Enforce the native manager's absent-at-startup arm-path contract."""
    path = _safe_child(root, _option(manager, "--fault-window-arm-path"), "manager fault-window arm")
    if os.path.lexists(path):
        raise LaunchError("manager fault-window arm path must be absent at startup")
    return path


def _publish_arm_once(path: Path, document: Mapping[str, Any]) -> None:
    """Atomically install a fully fsynced arm without replacing an existing one.

    The native process is permitted to poll an absent path.  It must never see
    a partially written JSON object, and no second publisher may replace the
    arm.  A same-directory O_EXCL temporary plus hard-link installation gives
    the no-replace property that ``os.replace`` cannot provide portably.
    """
    if os.path.lexists(path):
        raise LaunchError("manager fault-window arm path already exists")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.publish")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise LaunchError("fault-window arm temporary path already exists; no retry permitted") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_canonical(document)); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise LaunchError("manager fault-window arm path already exists") from exc
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fault_window_arm_document(
    root: Path,
    manager: Sequence[str],
    approval: Mapping[str, Any],
    *,
    e0_digest: str,
    evidence_start_monotonic_ns: int,
) -> tuple[Path, dict[str, Any]]:
    """Write the sole schema-v4 manager arm after a verified E0 prearm.

    This is deliberately an evidence arm, not an omission gate.  The actor's
    scheduled native fault remains solely in its replica argv.  The manager
    consumes this immutable, exact document and emits the audit event that the
    subsequent validator joins to physical omission contexts.
    """
    if type(evidence_start_monotonic_ns) is not int or evidence_start_monotonic_ns <= 0:
        raise LaunchError("fault-window arm evidence start is invalid")
    arm_path = _safe_child(root, _option(manager, "--fault-window-arm-path"), "manager fault-window arm")
    epoch0_tree = _safe_child(root, _option(manager, "--epoch-zero-tree-file"), "Epoch-0 tree")
    if epoch0_tree.is_symlink() or not epoch0_tree.is_file():
        raise LaunchError("manager Epoch-0 tree is not a regular file")
    topology_sha256 = _hex64(
        _option(manager, "--fault-window-arm-topology-proof-sha256"),
        "manager topology proof",
    )
    if _sha(epoch0_tree.read_bytes()) != topology_sha256:
        raise LaunchError("manager topology proof differs from Epoch-0 tree bytes")
    request_sha256 = _hex64(
        _option(manager, "--fault-window-arm-request-sha256"),
        "manager transition request",
    )
    if request_sha256 != _sha(_option(manager, "--transition-request").encode("utf-8")):
        raise LaunchError("fault-window arm request hash differs from manager transition request")
    manager_e0 = _hex64(
        _option(manager, "--fault-window-arm-epoch-digest"),
        "manager Epoch-0 digest",
    )
    if manager_e0 != _hex64(e0_digest, "source-derived Epoch-0 digest"):
        raise LaunchError("manager Epoch-0 digest differs from source-derived identity")
    expected = {
        "--fault-window-arm-schema-version": "4",
        "--fault-window-arm-domain": "kauri-focused-fault-window-arm-v4",
        "--fault-window-arm-profile-id": "n7-path-local-timeout-quorum-v4",
        "--fault-window-arm-prefault-tree-id": "4",
        "--fault-window-arm-required-tree-positions": "3",
        "--fault-window-arm-timeout-evidence-basis": "exact_timeout_attempt_id_v1",
        "--fault-window-arm-required-observation-schema": "3",
        "--fault-window-arm-clock-domain": "same_host_clock_monotonic_raw",
        "--fault-window-arm-snapshot-evidence-basis": "exact_post_fault_path_timeout_quorum_v1",
        "--fault-window-arm-selection-cardinality-policy": "all_guarded_up_to_fault_bound_v1",
    }
    for option, value in expected.items():
        if _option(manager, option) != value:
            raise LaunchError(f"manager {option} differs from frozen fault-window arm")
    if _option(manager, "--fault-window-arm-run-id") != _option(manager, "--structured-event-run-id"):
        raise LaunchError("manager fault-window arm run id differs from structured run id")
    if _option(manager, "--fault-window-arm-epoch-number") != "0":
        raise LaunchError("manager fault-window arm epoch differs from Epoch 0")
    document = {
        "clock_domain": "same_host_clock_monotonic_raw",
        "epoch_digest": e0_digest,
        "epoch_number": 0,
        "evidence_start_monotonic_ns": evidence_start_monotonic_ns,
        "fault_receipt_sha256": _sha(_canonical(approval)),
        "kind": "kauri-focused-fault-window-arm-v4",
        "prefault_tree_id": 4,
        "profile_id": "n7-path-local-timeout-quorum-v4",
        "profile_sha256": _hex64(_option(manager, "--fault-window-arm-profile-sha256"), "manager selection profile"),
        "request_sha256": request_sha256,
        "required_observation_schema": 3,
        "required_tree_ids": [4, 5, 6],
        "required_tree_positions": 3,
        "run_id": _option(manager, "--structured-event-run-id"),
        "schema_version": 4,
        "selection_cardinality_policy": "all_guarded_up_to_fault_bound_v1",
        "snapshot_evidence_basis": "exact_post_fault_path_timeout_quorum_v1",
        "timeout_evidence_basis": "exact_timeout_attempt_id_v1",
        "topology_proof_sha256": topology_sha256,
    }
    _publish_arm_once(arm_path, document)
    return arm_path, document


def _fault_window_arm_ack(
    streams: Mapping[str, Sequence[Mapping[str, Any]]], document: Mapping[str, Any],
) -> bool:
    """Accept only the native manager audit of these exact arm bytes."""
    expected_payload = {
        **document,
        "fault_window_arm_sha256": _sha(_canonical(document)),
    }
    for event in streams.get("adaptive-manager", ()):
        if (event.get("event_type") == "fault_window_armed" and
                event.get("payload") == expected_payload):
            return True
    return False


def execute_adaptive_e1_pilot(run_directory: Path, authorization_path: Path, *,
        spawn: Callable[..., Any], event_streams: Callable[[Path], Mapping[str, Sequence[Mapping[str, Any]]]],
        cleanup: Callable[[Path, Sequence[Any]], Mapping[str, Any]], raw_clock: Callable[[], int],
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    root, records = Path(run_directory).resolve(), []
    try:
        plan, _ = fixed._read(root / PLAN, "sustained-role plan")
        request, request_bytes = fixed._read(root / REQUEST, "sustained-role request")
        if (plan.get("state") != "PREPARED_DRY_RUN_EXTERNAL_APPROVAL_REQUIRED" or plan.get("comparison", {}).get("arm") != "adaptive_e1"
                or plan.get("no_retry") is not True or fixed._plan_digest(plan) != plan.get("plan_sha256")
                or request.get("execution_plan_sha256") != plan.get("plan_sha256") or request.get("no_retry") is not True):
            raise LaunchError("adaptive-E1 plan/request identity drift")
        approval = fixed._exact_approval(
            root, Path(authorization_path), plan, request_bytes,
            kind=AUTH_KIND, archive_path=APPROVAL,
        )
        fixed._verify_approved_launch_inputs(root, plan)
        manager = _adaptive_manager(plan)
        bundle = _safe_child(root, _option(manager, "--bundle-output"), "E1 bundle output")
        if bundle.exists() or bundle.is_symlink():
            raise LaunchError("E1 bundle output already exists")
        adapter_e0, issuer = _materialized_authorities(root)
        e0_digest, e0_path = fixed._source_e0(root)
        e0_helper = fixed._e0_identity_helper(root, e0_path)
        if e0_path.resolve() != adapter_e0.resolve():
            raise LaunchError("local-adapter E0 identity path differs from launch identity receipt")
        # Preserve the format check separately from the adapter provenance
        # check above; neither is inferred from a private manager argument.
        issuer = _issuer_path(root, plan)
        replicas = plan.get("commands", {}).get("replicas", [])
        if not isinstance(replicas, list) or len(replicas) != 7:
            raise LaunchError("plan does not contain exactly seven replicas")
        start, end = plan["scheduled_window"]["start_monotonic_ns"], plan["scheduled_window"]["end_monotonic_ns"]
        if raw_clock() >= start: raise LaunchError("scheduled window has already begun; no retry permitted")
        # Native option parsing rejects a present arm file at process start.
        # Its later poll deliberately tolerates absence until our E0 prearm.
        _fault_window_arm_startup_path(root, manager)
        fixed._prepare_exclusive_output_dirs(root)
        records.append(spawn("adaptive-manager", manager, root / "logs/adaptive-manager.log", root, replica_id=None))
        for i, row in enumerate(replicas):
            argv = row.get("argv") if isinstance(row, Mapping) else None
            if not isinstance(argv, list) or not argv: raise LaunchError("replica argv schema drift")
            records.append(spawn(f"replica-{i}", tuple(argv), root / f"logs/replica-{i}.log", root, replica_id=i))
        deadline = monotonic() + plan["hard_timeout_seconds"]
        while not fixed._all_seven_e0_common(event_streams(root), start):
            if monotonic() >= deadline or raw_clock() >= start: raise LaunchError("all-seven E0 common commit was not observed before scheduled start")
            sleep(.02)
        prearm_ns = raw_clock()
        if prearm_ns >= start:
            raise LaunchError("scheduled window began before adaptive prearm attestation")
        fixed._write_once(root / ATTESTATION, {"schema_version": 1, "kind": "kauri-n7-sustained-role-adaptive-e1-prearm-v1", "run_id": _option(manager, "--structured-event-run-id"), "prearm_monotonic_ns": prearm_ns, "scheduled_start_monotonic_ns": start, "no_retry": True})
        arm_start_ns = raw_clock()
        if arm_start_ns >= start:
            raise LaunchError("scheduled window began before fault-window arm publication")
        arm_path, arm_document = _fault_window_arm_document(
            root, manager, approval, e0_digest=e0_digest,
            evidence_start_monotonic_ns=arm_start_ns,
        )
        if raw_clock() >= start:
            raise LaunchError("scheduled window began before manager fault-window arm acknowledgement")
        # The manager reads only this exact document.  Do not permit the
        # scheduled native omission to begin until its own audit sink has
        # acknowledged it; an absent acknowledgement is a one-shot abort.
        while not _fault_window_arm_ack(event_streams(root), arm_document):
            if monotonic() >= deadline or raw_clock() >= start:
                raise LaunchError("manager fault-window arm audit acknowledgement was not observed before scheduled start")
            sleep(.02)
        anchor = None
        while raw_clock() < end:
            if monotonic() >= deadline: raise LaunchError("hard timeout expired before raw horizon")
            streams = event_streams(root); anchor = anchor or _first_anchor(streams)
            if anchor is not None and not _all_seven_e1_activated(streams, deadline_ns=anchor["source_monotonic_ns"] + _E1_DEADLINE_NS):
                if raw_clock() > anchor["source_monotonic_ns"] + _E1_DEADLINE_NS: raise LaunchError("all-seven E1 activation missed anchor-plus-20-second deadline")
            sleep(.05)
        if anchor is None: raise LaunchError("no native E0-internal aggregate omission anchor was recorded")
        cleanup_receipt = cleanup(root, records); records = []
        exits = fixed._clean_exit_codes(cleanup_receipt); fixed._persist_cleanup(root, cleanup_receipt)
        _adaptive_manager_success_terminal(root)
        if not bundle.is_file() or bundle.is_symlink(): raise LaunchError("native manager did not seal the signed E1 bundle")
        request_path, evidence_snapshot = _transition_artifacts(root, manager, bundle)
        fixed._write_once(root / FINALIZATION, {"schema_version": 1, "kind": FINAL_KIND, "state": "ADAPTIVE_E1_RAW_HORIZON_COMPLETED", "plan_sha256": plan["plan_sha256"], "authorization_sha256": _sha(_canonical(approval)), "no_retry": True})
        archived = {"profile": fixed._archive_input(root, HERE / "sustained_role_profile.py", "sustained-role-profile.py"), "epoch0_tree": fixed._archive_input(root, Path(plan["epoch0"]["tree"]["path"]), "epoch0.tree"), "main_config": fixed._archive_input(root, Path(plan["configuration"]["main"]["path"]), "main.conf"), "hotstuff_app": fixed._archive_input(root, Path(replicas[0]["argv"][0]), "hotstuff-app"), "adaptation_manager": fixed._archive_input(root, Path(manager[0]), "adaptation-manager"), "e0_identity_helper": fixed._archive_input(root, e0_helper, "e0-identity-helper"), "bundle": bundle, "issuer": issuer}
        replica_configs = [fixed._archive_input(root, Path(row["path"]), f"replica-{i}.conf") for i, row in enumerate(plan["configuration"]["replicas"])]
        if not arm_path.is_file() or arm_path.is_symlink(): raise LaunchError("native manager did not seal its fault-window arm")
        sealed_anchor = _sealed_anchor_and_coverage(root)
        if sealed_anchor["source_sequence"] != anchor["source_sequence"] or sealed_anchor["monotonic_ns"] != anchor["source_monotonic_ns"]:
            raise LaunchError("sealed raw anchor differs from the E1 activation anchor")
        artifacts = {"profile": fixed._descriptor(root, archived["profile"]), "epoch0_tree": fixed._descriptor(root, archived["epoch0_tree"]), "main_config": fixed._descriptor(root, archived["main_config"]), "execution_plan": fixed._descriptor(root, root / PLAN), "authorization_request": fixed._descriptor(root, root / REQUEST), "approved_authorization": fixed._descriptor(root, root / APPROVAL), "e0_identity_receipt": fixed._descriptor(root, root / "runtime/e0-identity-receipt.json"), "e0_identity_helper": fixed._descriptor(root, archived["e0_identity_helper"]), "finalization_receipt": fixed._descriptor(root, root / FINALIZATION), "fault_window_attestation": fixed._descriptor(root, root / ATTESTATION), "manager_events": fixed._descriptor(root, root / "raw/adaptive-manager.jsonl"), "manager_log": fixed._descriptor(root, root / "logs/adaptive-manager.log"), "cleanup": fixed._descriptor(root, root / CLEANUP), "replica_events": [fixed._descriptor(root, root / f"raw/replica-{i}.jsonl") for i in range(7)], "replica_logs": [fixed._descriptor(root, root / f"logs/replica-{i}.log") for i in range(7)], "replica_configs": [fixed._descriptor(root, item) for item in replica_configs], "executables": {"hotstuff_app": fixed._descriptor(root, archived["hotstuff_app"]), "adaptation_manager": fixed._descriptor(root, archived["adaptation_manager"])}, "transition_request": fixed._descriptor(root, request_path), "issuer_public_key": fixed._descriptor(root, archived["issuer"]), "e1_bundle": fixed._descriptor(root, archived["bundle"]), "manager_evidence_snapshot": fixed._descriptor(root, evidence_snapshot), "manager_fault_window_arm": fixed._descriptor(root, arm_path)}
        receipt = {"schema_version": 1, "kind": KIND, "state": "SEALED_RAW_BUNDLE_NO_CLAIM", "arm": "adaptive_e1", "run_id": _option(manager, "--structured-event-run-id"), "plan_sha256": plan["plan_sha256"], "anchor": sealed_anchor, "horizon": {"clock": "CLOCK_MONOTONIC_RAW", "duration_ns": _HORIZON_NS, "late_offset_ns": _E1_DEADLINE_NS}, "artifacts": artifacts, "launch_binding": {"request_sha256": _sha(request_bytes), "approval_sha256": _sha(_canonical(approval)), "e0_digest": e0_digest, "scheduled_window": {"start_monotonic_ns": start, "end_monotonic_ns": end}, "manager_argv_sha256": _sha(_canonical({"argv": list(manager)})), "manager_executable_sha256": plan["commands"]["manager"]["executable_sha256"], "replica_argv_sha256": [row["sha256"] for row in replicas], "replica_executable_sha256": replicas[0]["executable_sha256"], "native_profile_sha256": plan["native_fault_schedule"]["descriptor"]["sha256"], "selection_profile_sha256": plan["manager_selection_policy"]["descriptor"]["sha256"], "exit_codes": exits}}
        receipt["receipt_sha256"] = _sha(_canonical(receipt)); fixed._write_once(root / RECEIPT, receipt)
        return {"status": "SEALED_RAW_BUNDLE_NO_CLAIM", "receipt_sha256": receipt["receipt_sha256"]}
    except BaseException as exc:
        if records:
            try: cleanup(root, records)
            except BaseException: pass
        if not (root / ABORT).exists():
            fixed._write_once(root / ABORT, {"schema_version": 1, "state": "ABORTED_NO_RETRY", "reason": str(exc)[:512], "no_retry": True, "claim_boundary": "No figure or thesis claim."})
        raise LaunchError(str(exc)) from exc


def _production_cleanup(root: Path, records: Sequence[Any]) -> Mapping[str, Any]:
    local = _load("w19_sustained_local_runtime_for_adaptive", HERE / "run_local.py")
    return local._cleanup_receipt(root, _option(fixed._read(root / PLAN, "sustained-role plan")[0]["commands"]["manager"]["argv"], "--structured-event-run-id"), records, ())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one authorized W19 adaptive-E1 local pilot")
    parser.add_argument("--run-root", type=Path, required=True); parser.add_argument("--authorization", type=Path, required=True); parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute: parser.error("refusing to launch without --execute")
    base = _load("w19_adaptive_base_runtime", KAURI / "experiments/adaptive/n7-crash-recovery/run.py")
    try:
        result = execute_adaptive_e1_pilot(args.run_root, args.authorization, spawn=base.spawn_process, event_streams=base._event_streams, cleanup=_production_cleanup, raw_clock=lambda: time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW))
    except (LaunchError, OSError, ValueError, KeyError, TypeError) as exc: parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":"))); return 0


if __name__ == "__main__": raise SystemExit(main())
