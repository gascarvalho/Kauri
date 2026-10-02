"""Native signature replay for W18's all-member readiness certificate.

This is an offline component. It neither authorizes execution nor accepts a
campaign. A cluster caller must separately bind the verifier hash to its clean
build, and bind these raw files to its external authority receipt.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from . import factorial_validation
from . import focused_crash_pair_validation as readiness_codec
from . import operator_capacity_v3_authority as authority


class ReadinessReplayError(ValueError):
    pass


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(root: Path, relative: str, maximum: int = 64 * 1024 * 1024) -> bytes:
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ReadinessReplayError("readiness input traverses a symlink")
    return authority._read(path, relative, maximum)


def _json(raw: bytes, label: str) -> dict[str, Any]:
    return authority._json(raw, label)


def _events(root: Path, relative: str, run_id: str, source: str) -> list[dict[str, Any]]:
    raw = _read(root, relative)
    if not raw.endswith(b"\n"):
        raise ReadinessReplayError("readiness stream is incomplete")
    events = [_json(line, source) for line in raw.splitlines()]
    previous = -1
    instance = None
    for sequence, event in enumerate(events, 1):
        clock = event.get("source_monotonic_ns")
        if (event.get("run_id") != run_id or event.get("source_id") != source or
                event.get("source_sequence") != sequence or type(clock) is not int or clock < previous or
                event.get("event_schema_version") != 1 or
                event.get("source_kind") != ("adaptation_manager" if source == "adaptive-manager" else "replica")):
            raise ReadinessReplayError("readiness stream source, order or clock differs")
        current = event.get("source_instance")
        if not isinstance(current, str) or not current or (instance is not None and instance != current):
            raise ReadinessReplayError("readiness stream mixes source instances")
        instance, previous = current, clock
    if (not events or events[0]["event_type"] != "process.started" or events[-1]["event_type"] != "process.stopped" or
            sum(e["event_type"] == "process.started" for e in events) != 1 or
            sum(e["event_type"] == "process.stopped" for e in events) != 1):
        raise ReadinessReplayError("readiness stream lacks complete lifecycle bounds")
    return events


def _one(events: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    found = [event for event in events if event.get("event_type") == kind]
    if len(found) != 1 or not isinstance(found[0].get("payload"), dict):
        raise ReadinessReplayError("readiness event is missing or duplicated: " + kind)
    return found[0]["payload"]


def verify_native_readiness(root: Path, *, verifier_binary: Path,
                            expected_verifier_sha256: str,
                            manager_argv: list[str] | None = None) -> dict[str, Any]:
    """Join signed E1, all 31 activations and one BLS-verified certificate.

    Verification inputs and an immutable executable copy live in a temporary
    directory; the original evidence is never edited. Consensus Q remains 21;
    this experiment additionally requires all 31 certificate observations.
    """
    root = Path(root)
    if not root.is_absolute() or root != root.resolve() or root.is_symlink():
        raise ReadinessReplayError("readiness root must be canonical")
    manifest_raw = _read(root, "materialization-manifest.json", 256 * 1024)
    manifest = _json(manifest_raw, "materialization manifest")
    projection_raw = _read(root, "config/identity-public-projection.json", 256 * 1024)
    artifacts = manifest.get("artifact_sha256")
    if not isinstance(artifacts, dict) or artifacts.get("config/identity-public-projection.json") != _sha(projection_raw):
        raise ReadinessReplayError("readiness public keys differ from materialization")
    projection = _json(projection_raw, "public identity projection")
    argv_raw = (_read(root, "runtime/manager-argv.json", 256 * 1024) if manager_argv is None else
                json.dumps(manager_argv, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n")
    argv = json.loads(argv_raw)
    if (not isinstance(argv, list) or not all(isinstance(value, str) for value in argv) or
            _sha(argv_raw) != manifest.get("manager_argv_sha256") or
            argv.count("--structured-event-run-id") != 1):
        raise ReadinessReplayError("retained manager run identity differs from materialization")
    index = argv.index("--structured-event-run-id")
    if index + 1 >= len(argv) or not argv[index + 1]:
        raise ReadinessReplayError("retained manager lacks run identity")
    run_id = argv[index + 1]
    manager = _events(root, "raw/manager-events.jsonl", run_id, "adaptive-manager")
    certificate = _one(manager, "adaptive_v3.readiness_certificate_assembled")
    terminal = _one(manager, "adaptive_v3.readiness_terminal")
    identity = certificate.get("identity")
    identity_bytes = readiness_codec._v13_encode_ready_identity(identity)
    bundle = factorial_validation.decode_adaptive_v3_epoch_change_bundle(
        _read(root, "transitions/e0-to-e1-operator-capacity/successor.bundle"),
        issuer_public_key=projection["issuer_public_key"])
    if (identity["predecessor_boundary_configuration"]["epoch_number"] != 0 or
            identity["predecessor_boundary_configuration"]["epoch_digest"] != bundle.previous_epoch_digest or
            identity["successor_configuration"]["epoch_number"] != 1 or
            identity["successor_configuration"]["epoch_digest"] != bundle.epoch_digest or
            identity["command_payload_digest"] != bundle.command.payload_digest or
            identity["activation_delay_blocks"] != 5 or
            terminal.get("terminal_reason") != 1 or terminal.get("terminal_cycle_ordinal") != 0 or
            terminal.get("disposition") != "session_terminal" or
            terminal.get("observed_signers") != list(range(31)) or terminal.get("required_release_count") != 31 or
            terminal.get("identity") != identity or terminal.get("terminal_identity") != identity):
        raise ReadinessReplayError("readiness certificate is not the exact successful all-31 signed E1")
    deliveries = [e["payload"] for e in manager if e["event_type"] == "adaptive_v3.readiness_certificate_delivery"]
    if (len(deliveries) != 31 or {p.get("replica_id") for p in deliveries} != set(range(31)) or
            any(p.get("delivery_attempt") != 1 or p.get("delivery_enqueued") is not True for p in deliveries)):
        raise ReadinessReplayError("readiness delivery is not one attempt per member")
    digest = certificate.get("certificate_digest")
    for replica in range(31):
        events = _events(root, f"raw/replica-{replica}.jsonl", run_id, f"replica-{replica}")
        activation = _one(events, "epoch.activated")
        command = _one(events, "epoch.command_committed")
        if (activation.get("epoch_number") != 1 or activation.get("epoch_digest") != bundle.epoch_digest or
                activation.get("activation_readiness_certificate_digest") != digest or
                activation.get("activation_height") != identity["activation_height"] or
                command.get("successor_epoch_number") != 1 or
                command.get("successor_epoch_digest") != bundle.epoch_digest or
                command.get("predecessor_epoch_number") != 0 or
                command.get("predecessor_epoch_digest") != bundle.previous_epoch_digest or
                command.get("payload_digest") != identity["command_payload_digest"] or
                command.get("command_block_height") != identity["command_block_height"] or
                command.get("command_block_hash") != identity["command_block_hash"] or
                command.get("activation_delay_blocks") != 5 or
                command.get("activation_height") != identity["activation_height"]):
            raise ReadinessReplayError("replica activation or command differs from verified certificate")
    keys = projection.get("bls_public_keys")
    if not isinstance(keys, list) or len(keys) != 31 or len(set(keys)) != 31:
        raise ReadinessReplayError("readiness membership is not 31 distinct materialized public keys")
    public_manifest = {"algorithm": "bls-pop", "domain": "kauri-adaptive-v3-readiness-public-key-manifest-v1",
                       "members": [{"public_key_hex": key, "replica_id": i} for i, key in enumerate(keys)],
                       "membership_digest": identity["membership_digest"], "profile_id": run_id,
                       "profile_sha256": _sha(manifest_raw), "protocol_mode": "adaptive_v3", "schema_version": 1}
    binary = authority._read(Path(verifier_binary), "readiness verifier", 512 * 1024 * 1024)
    if _sha(binary) != expected_verifier_sha256:
        raise ReadinessReplayError("readiness verifier differs from pinned build")
    with tempfile.TemporaryDirectory(prefix="kauri-w18-readiness-") as directory:
        temporary = Path(directory)
        executable = temporary / "verified"
        executable.write_bytes(binary); executable.chmod(0o500)
        public = authority._canonical(public_manifest)
        (temporary / "public.json").write_bytes(public)
        wire_hex = certificate.get("canonical_wire_payload_hex")
        if not isinstance(wire_hex, str) or bytes.fromhex(wire_hex).hex() != wire_hex:
            raise ReadinessReplayError("readiness certificate bytes are noncanonical")
        (temporary / "certificate.hex").write_text(wire_hex + "\n", encoding="ascii")
        (temporary / "identity.hex").write_text(identity_bytes.hex() + "\n", encoding="ascii")
        completed = subprocess.run((str(executable), "--verify-adaptive-v3-readiness-v1",
            str(temporary / "public.json"), str(temporary / "certificate.hex"), str(temporary / "identity.hex")),
            capture_output=True, check=False, timeout=30)
        if completed.returncode != 0 or completed.stderr or _sha(executable.read_bytes()) != expected_verifier_sha256:
            raise ReadinessReplayError("native readiness signature verification failed")
        result = _json(completed.stdout, "native readiness verifier result")
    if (result.get("valid") is not True or result.get("schema") != "kauri-adaptive-v3-readiness-verification-v1" or
            result.get("member_count") != 31 or result.get("observation_count") != 31 or
            result.get("certificate_digest") != digest or result.get("membership_digest") != identity["membership_digest"] or
            result.get("certificate_payload_digest") != _sha(bytes.fromhex(wire_hex)) or
            result.get("expected_identity_payload_digest") != _sha(identity_bytes) or
            result.get("manifest_payload_digest") != _sha(public)):
        raise ReadinessReplayError("native readiness verifier output differs from exact raw joins")
    return {"kind": "kauri-w18-native-readiness-replay-component-v1", "native_verification": result,
            "verifier_sha256": expected_verifier_sha256, "all31_command_and_activation_join": True,
            "full_cluster_authority_verified": False, "claim_eligible": False, "figure_eligible": False}
