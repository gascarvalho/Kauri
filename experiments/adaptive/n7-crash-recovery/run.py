#!/usr/bin/env python3
"""Run the frozen real N=7 adaptive-v2 crash-recovery smoke.

The runner is intentionally evidence-first.  It launches exactly seven
replicas and one authenticated adaptation manager in isolated process groups,
waits for event-proven phase boundaries, kills only replicas 0 and 1 with
SIGKILL, and preserves every incomplete or failed run.  A successful launch is
then passed to the canonical validator and plotter; this module never decides
that a run passed on its own.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid


REPLICA_IDS = tuple(range(7))
CRASH_TARGETS = (0, 1)
SURVIVORS = (2, 3, 4, 5, 6)
FAULT_THRESHOLD = 2
QUORUM = 5
AUTHORITATIVE_OBSERVER = 2
AUTHORITATIVE_SOURCE_ID = "replica-2"
MANAGER_SOURCE_ID = "adaptive-manager"
REQUIRED_BRANCH = "feature/adaptive-epoch-throughput"
PROFILE_ID = "n7-f2-q5-crash-recovery-v2"
PROFILE_SHA256 = "768c33418937f9b738c607b523ad847a7cb38220c95a499e82823ac41aa1e038"
SCENARIO = "n7-crash-recovery"
BUCKET_WIDTH_NS = 5_000_000_000
MINIMUM_POST_ACTIVATION_GRACE_PROFILE_FIELD = "minimum_post_activation_grace_s"
MAXIMUM_ACTIVATION_TO_SUCCESSOR_PROFILE_FIELD = "maximum_activation_to_successor_s"
ACTIVATION_DELAY_BLOCKS = 5
TREE_SWITCH_PERIOD_BLOCKS = 1
SNAPSHOT_SEED = 0xA2F7
ISSUER_ID = 1
MAX_REPLICA_MESSAGE_BYTES = 4 << 20
MAX_COMMAND_BYTES = 4096
MAX_ANCESTRY_BLOCKS = 128
# Protocol ReplicaID is the uint16_t defined in include/hotstuff/type.h.
REPLICA_ID_BYTES = 2
MANAGER_LIMITS = {
    "maximum_members": 7,
    "readiness_wire_maximum_payload_bytes": 256,
    "lifecycle_wire_maximum_payload_bytes": 512,
    "evidence_wire_maximum_payload_bytes": 4096,
    "evidence_wire_maximum_observations": 8,
    "evidence_wire_maximum_signers_per_observation": 7,
    "proposal_maximum_exact_records": 8192,
    "proposal_maximum_retired_configurations": 16,
    "evidence_maximum_accepted_records": 131072,
    "evidence_maximum_rejected_records": 131072,
    "reputation_maximum_audit_updates": 131072,
    "quarantine_maximum_records": 1024,
    "quarantine_maximum_canonical_bytes": 256 * 1024,
    "quarantine_maximum_reporter_queues": 7,
    "quarantine_maximum_signer_entries": 8192,
    "quarantine_maximum_deduplication_entries": 1024,
    "quarantine_maximum_lifecycle_sources": 7,
    "quarantine_maximum_records_per_reporter": 128,
    "accounting_maximum_records": 1024,
    "accounting_maximum_canonical_bytes": 256 * 1024,
    "accounting_maximum_signer_entries": 8192,
    "maximum_pending_lifecycle_facts_per_source": 64,
}
BUNDLE_DOMAIN = b"kauri-adaptive-v2-epoch-change-bundle-v1"
AUTHORIZED_COMMAND_DOMAIN = b"kauri-authorized-epoch-change-v1"
FORBIDDEN_COMMAND_TOKENS = frozenset({"killall", "pkill", "sudo", "ssh"})

MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "scenario",
        "run_id",
        "kauri_revision",
        "kauri_worktree_clean",
        "profile",
        "run_completion",
        "replica_count",
        "fault_threshold",
        "quorum",
        "membership",
        "authoritative_observer",
        "manager",
        "bucket_width_ns",
        "minimum_post_activation_grace_ns",
        "baseline_start_ns",
        "end_ns",
        "sources",
        "crash_markers",
        "crash_configuration_boundary",
        "runtime",
        "runtime_artifacts",
    }
)

CONFIGURATION_ACTIVE_PAYLOAD_FIELDS = frozenset(
    {
        "epoch_number",
        "tree_id",
        "epoch_digest",
        "block_hash",
        "context_generation",
        "observer_replica",
        "wait_exempt_signers",
        "accepted_signers",
        "absent_direct_children",
        "missing_optional_signers",
        "required_branch_gaps",
        "root_signer_count",
        "global_quorum",
        "rejection_reason",
    }
)


class RunnerError(RuntimeError):
    """A deterministic precondition, orchestration, or evidence error."""


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    revision: str
    worktree_clean: bool


@dataclass(frozen=True, slots=True)
class CommonEpochCommit:
    observer_event: dict[str, Any]
    common_ns: int


@dataclass(slots=True)
class ProcessRecord:
    name: str
    pid: int
    pgid: int
    command: tuple[str, ...]
    log_path: Path
    process: subprocess.Popen[bytes]
    log_handle: Any
    replica_id: int | None = None


@dataclass(frozen=True, slots=True)
class DecodedCommand:
    issuer_id: int
    successor_epoch_number: int
    predecessor_epoch_digest: str
    successor_epoch_digest: str
    activation_delay_blocks: int


@dataclass(frozen=True, slots=True)
class DecodedTree:
    tree_id: int
    fanout: int
    pipeline_stretch: int
    members: tuple[int, ...]
    wait_exempt: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DecodedBundle:
    command: DecodedCommand
    epoch_number: int
    epoch_digest: str
    previous_epoch_digest: str
    membership_digest: str
    generation_seed: int
    policy_version: str
    evidence_snapshot_id: str
    evidence_cutoff: int
    trees: tuple[DecodedTree, ...]


class _ByteReader:
    def __init__(self, payload: bytes, label: str) -> None:
        self._payload = payload
        self._offset = 0
        self._label = label

    def _take(self, size: int) -> bytes:
        if size < 0 or self._offset > len(self._payload) or size > len(self._payload) - self._offset:
            raise RunnerError(f"{self._label} is truncated")
        value = self._payload[self._offset : self._offset + size]
        self._offset += size
        return value

    def domain(self, expected: bytes) -> None:
        if self._take(len(expected)) != expected:
            raise RunnerError(f"{self._label} has an invalid domain")

    def unsigned(self, size: int) -> int:
        return int.from_bytes(self._take(size), "big")

    def digest(self) -> str:
        return self._take(32).hex()

    def component(self, maximum: int) -> bytes:
        size = self.unsigned(4)
        if size > maximum:
            raise RunnerError(f"{self._label} component exceeds {maximum} bytes")
        return self._take(size)

    def string(self, maximum: int) -> str:
        size = self.unsigned(4)
        if size > maximum:
            raise RunnerError(f"{self._label} string exceeds {maximum} bytes")
        try:
            return self._take(size).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RunnerError(f"{self._label} contains non-UTF-8 text") from exc

    def finish(self) -> None:
        if self._offset != len(self._payload):
            raise RunnerError(f"{self._label} contains trailing bytes")


def decode_epoch_change_bundle(payload: bytes) -> DecodedBundle:
    """Decode the manager's canonical bundle without deriving a topology."""
    if not payload or len(payload) > 64 * 1024:
        raise RunnerError("successor bundle size is outside frozen bounds")
    bundle = _ByteReader(payload, "epoch-change bundle")
    bundle.domain(BUNDLE_DOMAIN)
    if bundle.unsigned(4) != 1 or bundle.unsigned(1) != 2:
        raise RunnerError("epoch-change bundle schema or mode is not adaptive-v2 v1")
    command_bytes = bundle.component(MAX_COMMAND_BYTES)
    definition_bytes = bundle.component(32 * 1024)
    bundle.finish()

    command_reader = _ByteReader(command_bytes, "authorized epoch command")
    command_reader.domain(AUTHORIZED_COMMAND_DOMAIN)
    if command_reader.unsigned(4) != 1 or command_reader.unsigned(1) != 2:
        raise RunnerError("authorized command schema or mode is invalid")
    issuer_id = command_reader.unsigned(4)
    successor_epoch_number = command_reader.unsigned(4)
    predecessor_digest = command_reader.digest()
    successor_digest = command_reader.digest()
    activation_delay = command_reader.unsigned(8)
    command_reader._take(64)
    command_reader.finish()

    definition = _ByteReader(definition_bytes, "successor epoch definition")
    if definition.unsigned(4) != 2 or definition.unsigned(1) != 2 or definition.unsigned(1) != 6:
        raise RunnerError("successor definition envelope is not adaptive-v2 reply v2")
    definition_digest = definition.digest()
    if definition.unsigned(4) != 2:
        raise RunnerError("successor definition schema is not v2")
    epoch_number = definition.unsigned(4)
    previous_epoch_digest = definition.digest()
    membership_digest = definition.digest()
    generation_seed = definition.unsigned(8)
    policy_version = definition.string(128)
    snapshot_id = definition.string(128)
    evidence_cutoff = definition.unsigned(8)
    tree_count = definition.unsigned(4)
    if tree_count != QUORUM:
        raise RunnerError("successor definition must contain exactly five trees")
    trees: list[DecodedTree] = []
    for expected_tree_id in range(tree_count):
        tree_id = definition.unsigned(4)
        fanout = definition.unsigned(4)
        pipeline = definition.unsigned(4)
        member_count = definition.unsigned(4)
        if tree_id != expected_tree_id or member_count != len(REPLICA_IDS):
            raise RunnerError("successor tree IDs or membership counts are invalid")
        members = tuple(
            definition.unsigned(REPLICA_ID_BYTES)
            for _ in range(member_count)
        )
        wait_count = definition.unsigned(4)
        wait_exempt = tuple(
            definition.unsigned(REPLICA_ID_BYTES)
            for _ in range(wait_count)
        )
        if len(set(members)) != len(REPLICA_IDS) or set(members) != set(REPLICA_IDS):
            raise RunnerError("successor tree does not preserve exact membership")
        if wait_exempt != CRASH_TARGETS:
            raise RunnerError("successor tree does not bind replicas 0 and 1 as wait-exempt")
        trees.append(DecodedTree(tree_id, fanout, pipeline, members, wait_exempt))
    definition.finish()

    if (
        epoch_number != successor_epoch_number
        or definition_digest != successor_digest
        or previous_epoch_digest != predecessor_digest
        or activation_delay != ACTIVATION_DELAY_BLOCKS
        or generation_seed != SNAPSHOT_SEED
    ):
        raise RunnerError("successor bundle command and definition identities disagree")
    successor_roots = tuple(tree.members[0] for tree in trees)
    if (
        len(set(successor_roots)) != QUORUM
        or set(successor_roots) != set(SURVIVORS)
    ):
        raise RunnerError("successor roots are not exactly the five surviving replicas")
    if any(tree.fanout != 2 or tree.pipeline_stretch != 2 for tree in trees):
        raise RunnerError("successor tree fanout or pipeline differs from the frozen profile")
    return DecodedBundle(
        command=DecodedCommand(
            issuer_id,
            successor_epoch_number,
            predecessor_digest,
            successor_digest,
            activation_delay,
        ),
        epoch_number=epoch_number,
        epoch_digest=definition_digest,
        previous_epoch_digest=previous_epoch_digest,
        membership_digest=membership_digest,
        generation_seed=generation_seed,
        policy_version=policy_version,
        evidence_snapshot_id=snapshot_id,
        evidence_cutoff=evidence_cutoff,
        trees=tuple(trees),
    )


def monotonic_raw_ns() -> int:
    clock_id = getattr(time, "CLOCK_MONOTONIC_RAW", None)
    reader = getattr(time, "clock_gettime_ns", None)
    if clock_id is None or not callable(reader):
        raise RunnerError("CLOCK_MONOTONIC_RAW is required for crash evidence")
    value = int(reader(clock_id))
    if value <= 0:
        raise RunnerError("CLOCK_MONOTONIC_RAW returned a non-positive value")
    return value


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_hex_value(value: str, label: str) -> str:
    """Fingerprint a canonical hex-encoded public identity by decoded bytes."""
    try:
        payload = bytes.fromhex(value)
    except ValueError as exc:
        raise RunnerError(f"{label} is not canonical hexadecimal data") from exc
    if not payload or value.lower() != value or value != payload.hex():
        raise RunnerError(f"{label} is not canonical lowercase hexadecimal data")
    return sha256_bytes(payload)


def _default_git(repository: Path, arguments: tuple[str, ...]) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.SubprocessError as exc:
        raise RunnerError(f"git {' '.join(arguments)} failed: {exc}") from exc
    return result.stdout.strip()


def verify_repository_state(
    repository: Path,
    *,
    git: Callable[[tuple[str, ...]], str] | None = None,
) -> RepositorySnapshot:
    """Require the fixed branch at the exact clean, already-pushed revision."""
    repository = repository.resolve()
    invoke = git or (lambda arguments: _default_git(repository, arguments))
    top = Path(invoke(("rev-parse", "--show-toplevel"))).resolve()
    if top != repository:
        raise RunnerError(f"repository path is not the Kauri top level: {repository}")
    branch = invoke(("branch", "--show-current"))
    if branch != REQUIRED_BRANCH:
        raise RunnerError(f"Kauri must remain on {REQUIRED_BRANCH}; found {branch or 'detached HEAD'}")
    revision = invoke(("rev-parse", "HEAD"))
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise RunnerError("Kauri HEAD is not a full lowercase Git revision")
    remote_revision = invoke(("rev-parse", f"origin/{REQUIRED_BRANCH}"))
    if remote_revision != revision:
        raise RunnerError("Kauri HEAD is not the exact pushed origin revision")
    status = invoke(
        (
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude).codex",
            ":(exclude)build/Testing",
        )
    )
    if status:
        raise RunnerError("Kauri worktree is not clean for evidence:\n" + status)
    return RepositorySnapshot(revision, True)


def load_frozen_profile(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError(f"cannot load frozen profile {path}: {exc}") from exc
    if sha256_bytes(payload) != PROFILE_SHA256:
        raise RunnerError("frozen profile SHA-256 differs from the canonical profile")
    if not isinstance(value, dict) or value.get("profile_id") != PROFILE_ID or value.get("frozen") is not True:
        raise RunnerError("profile is not the frozen N=7 crash-recovery profile")
    expected = {
        "replica_ids": list(REPLICA_IDS),
        "fault_threshold": FAULT_THRESHOLD,
        "quorum": QUORUM,
        "authoritative_observer": AUTHORITATIVE_OBSERVER,
        "crash_targets": list(CRASH_TARGETS),
        "epoch0_roots": list(REPLICA_IDS),
        "successor_roots": list(SURVIVORS),
        "successor_wait_exempt": list(CRASH_TARGETS),
        "fanout": 2,
        "pipeline_depth": 2,
        "block_size": 1,
        "tree_switch_period_blocks": TREE_SWITCH_PERIOD_BLOCKS,
        "activation_delay_blocks": ACTIVATION_DELAY_BLOCKS,
        "snapshot_seed": SNAPSHOT_SEED,
    }
    for field, expected_value in expected.items():
        if value.get(field) != expected_value:
            raise RunnerError(f"frozen profile field {field} differs from {expected_value!r}")
    for field in (
        MINIMUM_POST_ACTIVATION_GRACE_PROFILE_FIELD,
        MAXIMUM_ACTIVATION_TO_SUCCESSOR_PROFILE_FIELD,
    ):
        _profile_duration_ns(value, field)
    return value, payload


def _profile_duration_ns(profile: Mapping[str, Any], field: str) -> int:
    value = profile.get(field)
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise RunnerError(f"frozen profile field {field} must be positive seconds")
    nanoseconds = int(value * 1_000_000_000)
    if nanoseconds <= 0 or nanoseconds > (1 << 64) - 1:
        raise RunnerError(f"frozen profile field {field} is outside nanosecond range")
    return nanoseconds


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _write_json_exclusive(path: Path, value: Any, *, mode: int = 0o600) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _replace_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def create_run_directory(results_root: Path) -> Path:
    results_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_directory = results_root / f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    run_directory.mkdir(mode=0o700)
    (run_directory / "raw").mkdir(mode=0o700)
    (run_directory / "logs").mkdir(mode=0o700)
    (run_directory / "config").mkdir(mode=0o700)
    return run_directory


def _assert_executable(path: Path, label: str) -> None:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RunnerError(f"{label} is not executable: {path}")


def _assert_safe_command(command: Sequence[str]) -> None:
    if not command or any(not value for value in command):
        raise RunnerError("process command contains an empty argument")
    forbidden = {Path(value).name.lower() for value in command} & FORBIDDEN_COMMAND_TOKENS
    if forbidden:
        raise RunnerError(f"unsafe command token: {sorted(forbidden)[0]}")


def _parse_generator_output(
    text: str,
    *,
    expected_count: int,
    expected_fields: frozenset[str],
    label: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        fields: dict[str, str] = {}
        for token in line.split():
            if ":" not in token:
                raise RunnerError(f"{label} line {line_number} has an invalid token")
            key, value = token.split(":", 1)
            if not key or not value or key in fields:
                raise RunnerError(f"{label} line {line_number} has an invalid field")
            fields[key] = value
        if set(fields) != expected_fields:
            raise RunnerError(f"{label} line {line_number} has unexpected fields")
        rows.append(fields)
    if len(rows) != expected_count:
        raise RunnerError(f"{label} produced {len(rows)} identities; expected {expected_count}")
    for field in expected_fields:
        if len({row[field] for row in rows}) != len(rows):
            raise RunnerError(f"{label} produced duplicate {field} values")
    return rows


def _run_generator(command: Sequence[str], cwd: Path, label: str) -> str:
    _assert_safe_command(command)
    try:
        result = subprocess.run(
            list(command), cwd=cwd, check=True, capture_output=True, text=True
        )
    except subprocess.SubprocessError as exc:
        raise RunnerError(f"{label} failed: {exc}") from exc
    return result.stdout


def generate_identities(
    keygen_binary: Path,
    tls_keygen_binary: Path,
    config_directory: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, str]]:
    bls_output = _run_generator(
        (str(keygen_binary), "--num", "7", "--algo", "bls"),
        config_directory,
        "BLS identity generation",
    )
    tls_output = _run_generator(
        (str(tls_keygen_binary), "--num", "8"),
        config_directory,
        "TLS identity generation",
    )
    issuer_output = _run_generator(
        (str(keygen_binary), "--num", "1", "--algo", "secp256k1"),
        config_directory,
        "epoch issuer identity generation",
    )
    _write_private(config_directory / "bls-identities.txt", bls_output.encode())
    _write_private(config_directory / "tls-identities.txt", tls_output.encode())
    _write_private(config_directory / "issuer-identity.txt", issuer_output.encode())
    bls = _parse_generator_output(
        bls_output,
        expected_count=7,
        expected_fields=frozenset({"pub", "sec"}),
        label="BLS keygen",
    )
    tls = _parse_generator_output(
        tls_output,
        expected_count=8,
        expected_fields=frozenset({"crt", "sec", "cid"}),
        label="TLS keygen",
    )
    issuer = _parse_generator_output(
        issuer_output,
        expected_count=1,
        expected_fields=frozenset({"pub", "sec"}),
        label="issuer keygen",
    )[0]
    return bls, tls, issuer


def runtime_parameters(
    profile: Mapping[str, Any],
    *,
    app_binary: Path | None = None,
    manager_binary: Path | None = None,
) -> dict[str, Any]:
    runtime: dict[str, Any] = {
        "block_size": int(profile["block_size"]),
        "pipeline_depth": int(profile["pipeline_depth"]),
        "aggregation_timeout_ms": int(float(profile["aggregation_timeout_s"]) * 1000),
        "leader_progress_timeout_ms": int(float(profile["leader_progress_timeout_s"]) * 1000),
        "leader_activation_grace_ms": int(float(profile["leader_activation_grace_s"]) * 1000),
        "activation_delay_blocks": int(profile["activation_delay_blocks"]),
        "fanout": int(profile["fanout"]),
        "epoch0_roots": list(REPLICA_IDS),
        "successor_roots": list(SURVIVORS),
        "successor_wait_exempt": list(CRASH_TARGETS),
        "tree_switch_period_blocks": int(profile["tree_switch_period_blocks"]),
        "snapshot_seed": int(profile["snapshot_seed"]),
        "manager_limits": dict(MANAGER_LIMITS),
    }
    if (app_binary is None) != (manager_binary is None):
        raise RunnerError("runtime executable provenance requires both launched binaries")
    if app_binary is not None and manager_binary is not None:
        app_binary = app_binary.resolve()
        manager_binary = manager_binary.resolve()
        runtime["executables"] = {
            "hotstuff_app": {
                "path": str(app_binary),
                "sha256": sha256_file(app_binary),
            },
            "adaptation_manager": {
                "path": str(manager_binary),
                "sha256": sha256_file(manager_binary),
            },
        }
    return runtime


def normalized_manager_argv(command: Sequence[str]) -> list[str]:
    """Return persisted launch metadata without raw credentials or certificates."""
    normalized = list(command)
    for option, replacement in (
        ("--tls-privkey", "<redacted>"),
        ("--issuer-private-key", "<redacted>"),
        ("--tls-cert", "<fingerprinted>"),
    ):
        positions = [index for index, value in enumerate(normalized) if value == option]
        if len(positions) != 1 or positions[0] + 1 >= len(normalized):
            raise RunnerError(f"manager launch command has invalid {option} cardinality")
        normalized[positions[0] + 1] = replacement
    replica_positions = [
        index for index, value in enumerate(normalized) if value == "--replica"
    ]
    if len(replica_positions) != len(REPLICA_IDS):
        raise RunnerError("manager launch command does not bind seven replica endpoints")
    for position in replica_positions:
        if position + 1 >= len(normalized):
            raise RunnerError("manager launch command has a truncated replica endpoint")
        fields = normalized[position + 1].split(",")
        if len(fields) != 3:
            raise RunnerError("manager launch command has an invalid replica endpoint")
        fields[2] = "<fingerprinted>"
        normalized[position + 1] = ",".join(fields)
    return normalized


def build_replica_command(app_binary: Path, main_config: Path, replica_config: Path) -> tuple[str, ...]:
    command = (str(app_binary), "--conf", str(main_config), "--conf", str(replica_config))
    _assert_safe_command(command)
    return command


def build_manager_command(
    manager_binary: Path,
    *,
    replicas_tls: Sequence[Mapping[str, str]],
    manager_tls: Mapping[str, str],
    issuer: Mapping[str, str],
    manager_port: int,
    peer_port: int,
    activation_delay_blocks: int,
    run_id: str,
    source_instance: str,
    structured_event_path: Path,
    bundle_path: Path,
) -> tuple[str, ...]:
    if len(replicas_tls) != 7:
        raise RunnerError("manager command requires exactly seven replica TLS identities")
    command: list[str] = [
        str(manager_binary),
        "--listen",
        f"127.0.0.1:{manager_port}",
        "--tls-privkey",
        str(manager_tls["sec"]),
        "--tls-cert",
        str(manager_tls["crt"]),
        "--issuer-id",
        str(ISSUER_ID),
        "--issuer-private-key",
        str(issuer["sec"]),
        "--activation-delay-blocks",
        str(activation_delay_blocks),
        "--bundle-output",
        str(bundle_path),
        "--structured-event-run-id",
        run_id,
        "--structured-event-source-instance",
        source_instance,
        "--structured-event-output",
        str(structured_event_path),
    ]
    for replica_id, tls in enumerate(replicas_tls):
        command.extend(
            (
                "--replica",
                f"{replica_id},127.0.0.1:{peer_port + replica_id},{tls['crt']}",
            )
        )
    result = tuple(command)
    _assert_safe_command(result)
    return result


def _main_config_payload(
    profile: Mapping[str, Any],
    runtime: Mapping[str, Any],
    bls: Sequence[Mapping[str, str]],
    tls: Sequence[Mapping[str, str]],
    issuer: Mapping[str, str],
    *,
    peer_port: int,
    client_port: int,
    manager_port: int,
) -> bytes:
    lines = [
        f"block-size = {runtime['block_size']}",
        "nworker = 2",
        "repnworker = 1",
        "pace-maker = dummy",
        "proposer = 0",
        f"fan-out = {runtime['fanout']}",
        "piped_latency = 1",
        f"async_blocks = {runtime['pipeline_depth']}",
        "base-timeout = 2.0",
        "prop-delay = 0.1",
        f"aggregation-timeout = {float(profile['aggregation_timeout_s'])}",
        f"leader-progress-timeout = {float(profile['leader_progress_timeout_s'])}",
        f"leader-activation-grace = {float(profile['leader_activation_grace_s'])}",
        "client-ip = 127.0.0.1",
        "tree-generation = default",
        f"tree-switch-period = {runtime['tree_switch_period_blocks']}",
        "epoch-protocol-mode = adaptive_v2",
        f"epoch-change-issuer-id = {ISSUER_ID}",
        f"epoch-change-issuer-public-key = {issuer['pub']}",
        f"epoch-change-minimum-activation-delay = {runtime['activation_delay_blocks']}",
        f"epoch-change-maximum-activation-delay = {runtime['activation_delay_blocks']}",
        f"epoch-change-maximum-block-extra-bytes = {MAX_COMMAND_BYTES}",
        f"epoch-change-maximum-ancestry-blocks = {MAX_ANCESTRY_BLOCKS}",
        f"epoch-manager-address = 127.0.0.1:{manager_port}",
        f"epoch-manager-tls-cert = {tls[7]['crt']}",
        f"max-rep-msg = {MAX_REPLICA_MESSAGE_BYTES}",
    ]
    for replica_id in REPLICA_IDS:
        lines.append(
            "replica = "
            f"127.0.0.1:{peer_port + replica_id};{client_port + replica_id}, "
            f"{bls[replica_id]['pub']}, {tls[replica_id]['cid']}"
        )
    return ("\n".join(lines) + "\n").encode()


def _replica_config_payload(
    replica_id: int,
    bls: Sequence[Mapping[str, str]],
    tls: Sequence[Mapping[str, str]],
    *,
    raw_directory: Path,
    run_id: str,
    source_instances: Mapping[str, str],
) -> bytes:
    source_id = f"replica-{replica_id}"
    content = (
        f"privkey = {bls[replica_id]['sec']}\n"
        f"tls-privkey = {tls[replica_id]['sec']}\n"
        f"tls-cert = {tls[replica_id]['crt']}\n"
        f"idx = {replica_id}\n"
        f"structured-event-run-id = {run_id}\n"
        f"structured-event-source-instance = {source_instances[source_id]}\n"
        f"structured-event-output = {raw_directory / f'{source_id}.jsonl'}\n"
        f"structured-event-commit-observer-id = {AUTHORITATIVE_SOURCE_ID}\n"
        f"structured-event-commit-observer-instance = {source_instances[AUTHORITATIVE_SOURCE_ID]}\n"
    )
    return content.encode()


def _replica_effective_runtime(
    replica_id: int, runtime: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "replica_id": replica_id,
        "protocol_mode": "adaptive-v2",
        "replica_count": 7,
        "fault_threshold": 2,
        "quorum": 5,
        "membership": list(REPLICA_IDS),
        "authoritative_observer": AUTHORITATIVE_SOURCE_ID,
        "block_size": runtime["block_size"],
        "pipeline_depth": runtime["pipeline_depth"],
        "aggregation_timeout_ms": runtime["aggregation_timeout_ms"],
        "leader_progress_timeout_ms": runtime["leader_progress_timeout_ms"],
        "leader_activation_grace_ms": runtime["leader_activation_grace_ms"],
        "fanout": runtime["fanout"],
        "tree_switch_period_blocks": runtime["tree_switch_period_blocks"],
    }


def _initial_epoch_input() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "replica_count": 7,
        "membership": list(REPLICA_IDS),
        "fault_threshold": FAULT_THRESHOLD,
        "quorum": QUORUM,
        "epoch0_trees": [
            {
                "tree_id": root,
                "fanout": 2,
                "pipeline_depth": 2,
                "members_breadth_first": [
                    (root + offset) % 7 for offset in range(7)
                ],
                "wait_exempt": [],
            }
            for root in REPLICA_IDS
        ],
    }


def _runtime_artifact(
    run_directory: Path,
    path: Path,
    *,
    kind: str,
    replica_id: int | None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "replica_id": replica_id,
        "path": str(path.relative_to(run_directory)),
        "sha256": sha256_file(path),
    }


def _launch_arguments(
    *,
    runtime: Mapping[str, Any],
    replica_commands: Sequence[Sequence[str]],
    manager_command: Sequence[str],
    main_config: Path,
    replica_configs: Sequence[Path],
    bls: Sequence[Mapping[str, str]],
    tls: Sequence[Mapping[str, str]],
    issuer: Mapping[str, str],
) -> dict[str, Any]:
    app_sha256 = runtime["executables"]["hotstuff_app"]["sha256"]
    manager_sha256 = runtime["executables"]["adaptation_manager"]["sha256"]
    issuer_public_key_sha256 = sha256_hex_value(
        issuer["pub"], "epoch issuer public key"
    )
    manager_tls_certificate_sha256 = sha256_hex_value(
        tls[7]["crt"], "manager TLS certificate"
    )
    replica_tls_certificate_sha256 = [
        sha256_hex_value(
            tls[replica_id]["crt"],
            f"replica-{replica_id} TLS certificate",
        )
        for replica_id in REPLICA_IDS
    ]
    main_config_sha256 = sha256_file(main_config)
    return {
        "schema_version": 1,
        "processes": [
            *[
                {
                    "source_kind": "replica",
                    "source_id": f"replica-{replica_id}",
                    "argv": list(replica_commands[replica_id]),
                    "effective_options": {
                        "block_size": runtime["block_size"],
                        "pipeline_depth": runtime["pipeline_depth"],
                        "aggregation_timeout_ms": runtime[
                            "aggregation_timeout_ms"
                        ],
                        "leader_progress_timeout_ms": runtime[
                            "leader_progress_timeout_ms"
                        ],
                        "leader_activation_grace_ms": runtime[
                            "leader_activation_grace_ms"
                        ],
                        "fanout": runtime["fanout"],
                        "tree_switch_period_blocks": runtime[
                            "tree_switch_period_blocks"
                        ],
                        "binary_sha256": app_sha256,
                        "main_config_sha256": main_config_sha256,
                        "replica_config_sha256": sha256_file(
                            replica_configs[replica_id]
                        ),
                        "bls_public_key_sha256": sha256_hex_value(
                            bls[replica_id]["pub"],
                            f"replica-{replica_id} BLS public key",
                        ),
                        "tls_certificate_sha256": (
                            replica_tls_certificate_sha256[replica_id]
                        ),
                        "issuer_public_key_sha256": issuer_public_key_sha256,
                        "manager_tls_certificate_sha256": (
                            manager_tls_certificate_sha256
                        ),
                    },
                }
                for replica_id in REPLICA_IDS
            ],
            {
                "source_kind": "adaptation_manager",
                "source_id": MANAGER_SOURCE_ID,
                "argv": normalized_manager_argv(manager_command),
                "effective_options": {
                    "activation_delay_blocks": runtime[
                        "activation_delay_blocks"
                    ],
                    "snapshot_seed": runtime["snapshot_seed"],
                    "manager_limits": runtime["manager_limits"],
                    "binary_sha256": manager_sha256,
                    "tls_certificate_sha256": manager_tls_certificate_sha256,
                    "issuer_public_key_sha256": issuer_public_key_sha256,
                    "replica_tls_certificate_sha256": (
                        replica_tls_certificate_sha256
                    ),
                },
            },
        ],
    }


def write_runtime_inputs(
    run_directory: Path,
    profile: Mapping[str, Any],
    bls: Sequence[Mapping[str, str]],
    tls: Sequence[Mapping[str, str]],
    issuer: Mapping[str, str],
    *,
    peer_port: int,
    client_port: int,
    manager_port: int,
    run_id: str,
    source_instances: Mapping[str, str],
    app_binary: Path,
    manager_binary: Path,
) -> tuple[Path, list[Path], tuple[str, ...], list[tuple[str, ...]], list[dict[str, Any]]]:
    config_directory = run_directory / "config"
    runtime_directory = run_directory / "runtime"
    runtime_directory.mkdir(mode=0o700)
    raw_directory = run_directory / "raw"
    runtime = runtime_parameters(
        profile,
        app_binary=app_binary,
        manager_binary=manager_binary,
    )
    main_config = config_directory / "hotstuff.gen.conf"
    _write_private(
        main_config,
        _main_config_payload(
            profile,
            runtime,
            bls,
            tls,
            issuer,
            peer_port=peer_port,
            client_port=client_port,
            manager_port=manager_port,
        ),
    )

    replica_configs: list[Path] = []
    replica_commands: list[tuple[str, ...]] = []
    runtime_artifacts: list[dict[str, Any]] = []
    for replica_id in REPLICA_IDS:
        replica_config = config_directory / f"replica-{replica_id}.conf"
        _write_private(
            replica_config,
            _replica_config_payload(
                replica_id,
                bls,
                tls,
                raw_directory=raw_directory,
                run_id=run_id,
                source_instances=source_instances,
            ),
        )
        replica_configs.append(replica_config)
        replica_commands.append(
            build_replica_command(app_binary, main_config, replica_config)
        )

        normalized_path = runtime_directory / f"replica-{replica_id}.effective.json"
        _write_json_exclusive(
            normalized_path,
            _replica_effective_runtime(replica_id, runtime),
        )
        runtime_artifacts.append(
            _runtime_artifact(
                run_directory,
                normalized_path,
                kind="replica_config",
                replica_id=replica_id,
            )
        )

    manager_command = build_manager_command(
        manager_binary,
        replicas_tls=tls[:7],
        manager_tls=tls[7],
        issuer=issuer,
        manager_port=manager_port,
        peer_port=peer_port,
        activation_delay_blocks=runtime["activation_delay_blocks"],
        run_id=run_id,
        source_instance=source_instances[MANAGER_SOURCE_ID],
        structured_event_path=raw_directory / "adaptive-manager.jsonl",
        bundle_path=run_directory / "successor.bundle",
    )
    epoch_input_path = runtime_directory / "epoch-input.json"
    _write_json_exclusive(epoch_input_path, _initial_epoch_input())
    runtime_artifacts.append(
        _runtime_artifact(
            run_directory,
            epoch_input_path,
            kind="epoch_input",
            replica_id=None,
        )
    )
    launch_path = runtime_directory / "launch-arguments.json"
    _write_json_exclusive(
        launch_path,
        _launch_arguments(
            runtime=runtime,
            replica_commands=replica_commands,
            manager_command=manager_command,
            main_config=main_config,
            replica_configs=replica_configs,
            bls=bls,
            tls=tls,
            issuer=issuer,
        ),
    )
    runtime_artifacts.append(
        _runtime_artifact(
            run_directory,
            launch_path,
            kind="launch_arguments",
            replica_id=None,
        )
    )
    return main_config, replica_configs, manager_command, replica_commands, runtime_artifacts


def required_ports(peer_port: int, client_port: int, manager_port: int) -> tuple[int, ...]:
    ports = tuple(peer_port + replica for replica in REPLICA_IDS)
    ports += tuple(client_port + replica for replica in REPLICA_IDS)
    ports += (manager_port,)
    if any(port <= 1024 or port > 65535 for port in ports) or len(set(ports)) != len(ports):
        raise RunnerError("configured campaign ports are invalid or overlap")
    return ports


def ports_in_use(ports: Iterable[int]) -> list[int]:
    unavailable: list[int] = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                unavailable.append(port)
    return unavailable


def listening_ports(ports: Iterable[int]) -> list[int]:
    result: list[int] = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                result.append(port)
    return result


def spawn_process(
    name: str,
    command: Sequence[str],
    log_path: Path,
    cwd: Path,
    *,
    replica_id: int | None,
) -> ProcessRecord:
    _assert_safe_command(command)
    log_handle = log_path.open("xb")
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pgid = os.getpgid(process.pid)
        if pgid != process.pid or pgid <= 1 or pgid == os.getpgrp():
            raise RunnerError(f"{name} does not own a safe isolated process group")
        return ProcessRecord(
            name,
            process.pid,
            pgid,
            tuple(command),
            log_path,
            process,
            log_handle,
            replica_id,
        )
    except Exception:
        log_handle.close()
        raise


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RunnerError(f"cannot read structured events {path}: {exc}") from exc
    if not payload:
        return []
    lines = payload.split(b"\n")
    if lines[-1]:
        lines.pop()
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RunnerError(f"{path.name} line {line_number} is malformed: {exc.msg}") from exc
        if not isinstance(event, dict):
            raise RunnerError(f"{path.name} line {line_number} is not an object")
        events.append(event)
    return events


def _event_streams(run_directory: Path) -> dict[str, list[dict[str, Any]]]:
    streams = {
        f"replica-{replica}": _read_jsonl(run_directory / "raw" / f"replica-{replica}.jsonl")
        for replica in REPLICA_IDS
    }
    streams[MANAGER_SOURCE_ID] = _read_jsonl(run_directory / "raw" / "adaptive-manager.jsonl")
    return streams


def _source_sequence(event: Mapping[str, Any]) -> int:
    value = event.get("source_sequence")
    if type(value) is not int or value <= 0:
        raise RunnerError("structured event has an invalid source sequence")
    return value


def _event_timestamp(event: Mapping[str, Any]) -> int:
    value = event.get("source_monotonic_ns")
    if type(value) is not int or value <= 0:
        raise RunnerError("structured event has an invalid monotonic timestamp")
    return value


def _validate_crash_tree_roles(
    members_breadth_first: Sequence[int],
    *,
    fanout: int,
    root_replica: int,
) -> None:
    members = tuple(members_breadth_first)
    if members != (6, 0, 1, 2, 3, 4, 5) or fanout != 2 or root_replica != 6:
        raise RunnerError("frozen crash boundary must use epoch-0 tree-6 BFS")
    if members[0] != root_replica:
        raise RunnerError("frozen crash boundary root does not match tree-6 BFS")
    positions = [members.index(target) for target in CRASH_TARGETS]
    if len(set(positions)) != len(CRASH_TARGETS):
        raise RunnerError("crash targets must occupy distinct tree positions")
    for target, position in zip(CRASH_TARGETS, positions):
        if position == 0 or fanout * position + 1 >= len(members):
            raise RunnerError(
                f"crash target {target} is not a non-root internal tree-6 replica"
            )


def _active_configuration_payload(
    event: Mapping[str, Any],
    *,
    replica: int,
) -> Mapping[str, Any]:
    if event.get("event_type") != "adaptive.configuration_active":
        raise RunnerError("expected adaptive.configuration_active evidence")
    payload = event.get("payload")
    if not isinstance(payload, dict) or set(payload) != CONFIGURATION_ACTIVE_PAYLOAD_FIELDS:
        raise RunnerError("configuration-active evidence has schema drift")
    for field in (
        "epoch_number",
        "tree_id",
        "observer_replica",
        "root_signer_count",
        "global_quorum",
    ):
        if type(payload.get(field)) is not int or int(payload[field]) < 0:
            raise RunnerError("configuration-active evidence has an invalid integer")
    if payload.get("observer_replica") != replica:
        raise RunnerError("configuration-active observer does not match its source")
    if (
        payload.get("block_hash") is not None
        or payload.get("context_generation") is not None
        or payload.get("wait_exempt_signers") != []
        or payload.get("accepted_signers") != []
        or payload.get("absent_direct_children") != []
        or payload.get("missing_optional_signers") != []
        or payload.get("required_branch_gaps") != []
        or payload.get("root_signer_count") != 0
        or payload.get("global_quorum") != QUORUM
        or payload.get("rejection_reason") is not None
    ):
        raise RunnerError("configuration-active evidence is not canonical epoch-0 state")
    digest = payload.get("epoch_digest")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or digest == "0" * 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise RunnerError("configuration-active evidence has an invalid epoch digest")
    return payload


def common_active_configuration_boundary(
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    minimum_source_sequences: Mapping[str, int],
    epoch_number: int,
    tree_id: int,
    root_replica: int,
    members_breadth_first: Sequence[int],
    fanout: int,
    maximum_skew_ns: int,
    observed_ns: int,
) -> dict[str, Any] | None:
    _validate_crash_tree_roles(
        members_breadth_first,
        fanout=fanout,
        root_replica=root_replica,
    )
    if maximum_skew_ns <= 0 or observed_ns <= 0:
        raise RunnerError("configuration boundary clocks must be positive")
    expected_sources = {f"replica-{replica}" for replica in REPLICA_IDS}
    if set(streams) != expected_sources or set(minimum_source_sequences) != expected_sources:
        raise RunnerError("configuration boundary requires all seven replica streams")

    selected: list[dict[str, Any]] = []
    digests: set[str] = set()
    timestamps: list[int] = []
    for replica in REPLICA_IDS:
        source_id = f"replica-{replica}"
        candidates = [
            event
            for event in streams[source_id]
            if event.get("event_type") == "adaptive.configuration_active"
            and _source_sequence(event) > minimum_source_sequences[source_id]
        ]
        if not candidates:
            return None
        event = candidates[-1]
        payload = _active_configuration_payload(event, replica=replica)
        if payload.get("epoch_number") != epoch_number or payload.get("tree_id") != tree_id:
            return None
        sequence = _source_sequence(event)
        timestamp = _event_timestamp(event)
        if timestamp > observed_ns:
            raise RunnerError("configuration evidence timestamp follows observation")
        digest = str(payload["epoch_digest"])
        digests.add(digest)
        timestamps.append(timestamp)
        selected.append(
            {
                "source_id": source_id,
                "source_sequence": sequence,
                "source_monotonic_ns": timestamp,
            }
        )
    if len(digests) != 1:
        raise RunnerError("replicas disagree on the active epoch-0 digest")
    if max(timestamps) - min(timestamps) > maximum_skew_ns:
        return None
    return {
        "epoch_number": epoch_number,
        "tree_id": tree_id,
        "root_replica": root_replica,
        "epoch_digest": next(iter(digests)),
        "context_generation": None,
        "replica_evidence": selected,
    }


class FreshConfigurationPoller:
    """Incrementally tail the seven streams so the short tree-6 window is observable."""

    def __init__(
        self,
        run_directory: Path,
        minimum_source_sequences: Mapping[str, int],
        *,
        start_offsets: Mapping[str, int],
        maximum_skew_ns: int,
        clock_ns: Callable[[], int] = monotonic_raw_ns,
    ) -> None:
        self._paths = {
            f"replica-{replica}": run_directory / "raw" / f"replica-{replica}.jsonl"
            for replica in REPLICA_IDS
        }
        self._minimum = dict(minimum_source_sequences)
        if set(self._minimum) != set(self._paths) or set(start_offsets) != set(
            self._paths
        ):
            raise RunnerError("configuration poller requires seven source cursors")
        self._maximum_skew_ns = maximum_skew_ns
        self._clock_ns = clock_ns
        self._offsets = dict(start_offsets)
        if any(type(offset) is not int or offset < 0 for offset in self._offsets.values()):
            raise RunnerError("configuration poller offsets must be non-negative")
        self._pending = {source: b"" for source in self._paths}
        self._events: dict[str, list[dict[str, Any]]] = {
            source: [] for source in self._paths
        }

    def _consume(self) -> None:
        for source_id, path in self._paths.items():
            try:
                with path.open("rb") as stream:
                    stream.seek(self._offsets[source_id])
                    chunk = stream.read()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise RunnerError(f"cannot tail structured events {path}: {exc}") from exc
            self._offsets[source_id] += len(chunk)
            payload = self._pending[source_id] + chunk
            newline = payload.rfind(b"\n")
            if newline < 0:
                if len(payload) > 64 * 1024:
                    raise RunnerError(f"unterminated structured event exceeds limit in {path}")
                self._pending[source_id] = payload
                continue
            complete = payload[: newline + 1]
            self._pending[source_id] = payload[newline + 1 :]
            for line in complete.splitlines():
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RunnerError(
                        f"malformed incremental structured event in {path}: {exc.msg}"
                    ) from exc
                if not isinstance(event, dict):
                    raise RunnerError(f"non-object incremental structured event in {path}")
                if (
                    event.get("event_type") == "adaptive.configuration_active"
                    and _source_sequence(event) > self._minimum[source_id]
                ):
                    self._events[source_id].append(event)

    def poll(self) -> dict[str, Any] | None:
        self._consume()
        observed_ns = self._clock_ns()
        candidate = common_active_configuration_boundary(
            self._events,
            minimum_source_sequences=self._minimum,
            epoch_number=0,
            tree_id=6,
            root_replica=6,
            members_breadth_first=(6, 0, 1, 2, 3, 4, 5),
            fanout=2,
            maximum_skew_ns=self._maximum_skew_ns,
            observed_ns=observed_ns,
        )
        if candidate is None:
            return None
        # A second nonblocking pass closes the ordinary poll/read interleaving.
        # The post-SIGKILL audit below remains the fail-closed race detector.
        self._consume()
        return common_active_configuration_boundary(
            self._events,
            minimum_source_sequences=self._minimum,
            epoch_number=0,
            tree_id=6,
            root_replica=6,
            members_breadth_first=(6, 0, 1, 2, 3, 4, 5),
            fanout=2,
            maximum_skew_ns=self._maximum_skew_ns,
            observed_ns=self._clock_ns(),
        )


def replica_event_tail_snapshot(
    run_directory: Path,
) -> tuple[dict[str, int], dict[str, int]]:
    """Capture the last complete source sequence and byte boundary per replica."""
    watermarks: dict[str, int] = {}
    offsets: dict[str, int] = {}
    for replica in REPLICA_IDS:
        source_id = f"replica-{replica}"
        path = run_directory / "raw" / f"replica-{replica}.jsonl"
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise RunnerError(f"cannot snapshot structured events {path}: {exc}") from exc
        newline = payload.rfind(b"\n")
        if newline < 0:
            watermarks[source_id] = 0
            offsets[source_id] = 0
            continue
        end = newline
        while end > 0 and payload[end - 1 : end] == b"\n":
            end -= 1
        start = payload.rfind(b"\n", 0, end) + 1
        line = payload[start:end]
        if not line:
            watermarks[source_id] = 0
            offsets[source_id] = newline + 1
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RunnerError(
                f"malformed final complete structured event in {path}: {exc.msg}"
            ) from exc
        if not isinstance(event, dict):
            raise RunnerError(f"non-object final structured event in {path}")
        watermarks[source_id] = _source_sequence(event)
        offsets[source_id] = newline + 1
    return watermarks, offsets


def _commits(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(event) for event in events if event.get("event_type") == "block.committed"]


def _commit_observations(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        dict(event)
        for event in events
        if event.get("event_type") == "block.commit_observed"
    ]


def _commit_key(event: Mapping[str, Any]) -> tuple[int, str]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise RunnerError("commit event has no payload object")
    height = payload.get("block_height")
    block_hash = payload.get("block_hash")
    if type(height) is not int or height <= 0 or not isinstance(block_hash, str) or len(block_hash) != 64:
        raise RunnerError("commit event has an invalid height or hash")
    return height, block_hash


def commit_witness_timestamps(
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    participants: Sequence[int],
) -> dict[int, dict[tuple[int, str], int]]:
    result: dict[int, dict[tuple[int, str], int]] = {}
    for replica in participants:
        timestamps: dict[tuple[int, str], int] = {}
        for event in _commit_observations(streams[f"replica-{replica}"]):
            key = _commit_key(event)
            timestamp_ns = _event_timestamp(event)
            previous = timestamps.get(key)
            if previous is None or timestamp_ns < previous:
                timestamps[key] = timestamp_ns
        result[replica] = timestamps
    return result


def find_first_common_epoch_commit(
    observer_events: Sequence[Mapping[str, Any]],
    witnesses: Mapping[int, Mapping[tuple[int, str], int]],
    *,
    participants: Sequence[int],
    epoch_number: int,
) -> CommonEpochCommit | None:
    if not participants:
        raise RunnerError("common epoch commit requires at least one participant")
    for event in _commits(observer_events):
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        proof = payload.get("decision_proof")
        if not isinstance(proof, dict) or proof.get("epoch_number") != epoch_number:
            continue
        key = _commit_key(event)
        witness_timestamps = [
            witnesses.get(replica, {}).get(key) for replica in participants
        ]
        if all(timestamp is not None for timestamp in witness_timestamps):
            common_timestamps = [
                _event_timestamp(event),
                *(int(timestamp) for timestamp in witness_timestamps),
            ]
            return CommonEpochCommit(
                observer_event=event,
                common_ns=max(common_timestamps),
            )
    return None


def enforce_common_epoch_commit_deadline(
    result: CommonEpochCommit | None,
    *,
    now_ns: int,
    deadline_ns: int,
) -> CommonEpochCommit | None:
    if (
        type(now_ns) is not int
        or now_ns <= 0
        or type(deadline_ns) is not int
        or deadline_ns <= 0
    ):
        raise RunnerError("common epoch commit deadline inputs are invalid")
    if result is not None:
        if result.common_ns > deadline_ns:
            raise RunnerError(
                "first common successor commit exceeded "
                "maximum_activation_to_successor_s"
            )
        return result
    if now_ns > deadline_ns:
        raise RunnerError(
            "first common successor commit exceeded "
            "maximum_activation_to_successor_s"
        )
    return None


def post_measurement_boundaries(
    *,
    activation_ns: int,
    first_common_successor_ns: int,
    minimum_post_activation_grace_ns: int,
) -> tuple[int, int]:
    if (
        type(activation_ns) is not int
        or activation_ns <= 0
        or type(first_common_successor_ns) is not int
        or first_common_successor_ns < activation_ns
        or type(minimum_post_activation_grace_ns) is not int
        or minimum_post_activation_grace_ns <= 0
    ):
        raise RunnerError("post measurement boundary inputs are invalid")
    minimum_post_start_ns = activation_ns + minimum_post_activation_grace_ns
    return minimum_post_start_ns, max(
        minimum_post_start_ns, first_common_successor_ns
    )


def find_common_root_cycle(
    observer_events: Sequence[Mapping[str, Any]],
    common: Mapping[int, Mapping[tuple[int, str], int]],
    *,
    participants: Sequence[int],
    epoch_number: int,
    tree_roots: Mapping[int, int],
    expected_roots: Sequence[int],
    require_terminal: bool,
) -> dict[str, Any] | None:
    groups: list[tuple[int, dict[str, Any], bool]] = []
    for raw_event in observer_events:
        if raw_event.get("event_type") != "block.committed":
            continue
        payload = raw_event.get("payload")
        if not isinstance(payload, dict):
            continue
        proof = payload.get("decision_proof")
        if not isinstance(proof, dict) or proof.get("epoch_number") != epoch_number:
            continue
        tree_id = proof.get("tree_id")
        if type(tree_id) is not int or tree_id not in tree_roots:
            continue
        key = _commit_key(raw_event)
        is_common = all(
            key in common.get(replica, set()) for replica in participants
        )
        root = tree_roots[tree_id]
        event = dict(raw_event)
        if not groups or groups[-1][0] != root:
            groups.append((root, event, is_common))
        else:
            groups[-1] = (root, event, is_common)
    expected = tuple(expected_roots)
    if require_terminal:
        if tuple(root for root, _, _ in groups[-len(expected) :]) != expected:
            return None
        terminal = groups[-len(expected) :]
        return terminal[-1][1] if all(group[2] for group in terminal) else None
    for index in range(len(groups) - len(expected), -1, -1):
        if tuple(root for root, _, _ in groups[index : index + len(expected)]) == expected:
            cycle = groups[index : index + len(expected)]
            if all(group[2] for group in cycle):
                return cycle[-1][1]
    return None


def assert_crash_boundary_held(
    boundary: Mapping[str, Any],
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    crash_request_ns: int,
) -> None:
    evidence = boundary.get("replica_evidence")
    if not isinstance(evidence, list) or len(evidence) != len(REPLICA_IDS):
        raise RunnerError("tree-6 boundary lacks seven replica evidence references")
    latest_boundary_ns = 0
    observer_boundary_sequence: int | None = None
    for replica, reference in zip(REPLICA_IDS, evidence):
        if not isinstance(reference, dict):
            raise RunnerError("tree-6 boundary contains malformed replica evidence")
        source_id = f"replica-{replica}"
        if reference.get("source_id") != source_id:
            raise RunnerError("tree-6 boundary source order is not canonical")
        sequence = reference.get("source_sequence")
        timestamp = reference.get("source_monotonic_ns")
        if type(sequence) is not int or type(timestamp) is not int:
            raise RunnerError("tree-6 boundary reference is not integral")
        matches = [
            event for event in streams.get(source_id, ())
            if event.get("source_sequence") == sequence
        ]
        if len(matches) != 1 or _event_timestamp(matches[0]) != timestamp:
            raise RunnerError("tree-6 boundary no longer matches its source event")
        latest_boundary_ns = max(latest_boundary_ns, timestamp)
        if source_id == AUTHORITATIVE_SOURCE_ID:
            observer_boundary_sequence = sequence
        for event in streams[source_id]:
            if (
                event.get("event_type") == "adaptive.configuration_active"
                and _source_sequence(event) > sequence
                and _event_timestamp(event) <= crash_request_ns
            ):
                raise RunnerError(
                    "configuration changed after tree-6 boundary before crash request"
                )
    if latest_boundary_ns >= crash_request_ns:
        raise RunnerError("crash request does not follow the observed tree-6 boundary")
    if observer_boundary_sequence is None:
        raise RunnerError("tree-6 boundary lacks authoritative observer evidence")
    for event in _commits(streams[AUTHORITATIVE_SOURCE_ID]):
        timestamp = _event_timestamp(event)
        if (
            _source_sequence(event) <= observer_boundary_sequence
            or timestamp > crash_request_ns
        ):
            continue
        payload = event.get("payload")
        proof = payload.get("decision_proof") if isinstance(payload, dict) else None
        if not isinstance(proof, dict) or (
            proof.get("epoch_number"), proof.get("tree_id")
        ) != (0, 6):
            raise RunnerError(
                "authoritative root changed after tree-6 boundary before crash request"
            )


def _check_processes(records: Sequence[ProcessRecord], expected_crashed: set[int]) -> None:
    for record in records:
        return_code = record.process.poll()
        if return_code is None:
            continue
        if record.replica_id in expected_crashed and return_code == -signal.SIGKILL:
            continue
        raise RunnerError(f"{record.name} exited unexpectedly with status {return_code}")


def _enforce_observer_stall(
    observer_events: Sequence[Mapping[str, Any]],
    *,
    window_start_ns: int,
    now_ns: int,
    maximum_gap_ns: int,
) -> None:
    timestamps = sorted(
        _event_timestamp(event)
        for event in _commits(observer_events)
        if _event_timestamp(event) >= window_start_ns
    )
    if not timestamps:
        if now_ns - window_start_ns > maximum_gap_ns:
            raise RunnerError("authoritative observer committed no block within maximum_stall_s")
        return
    previous = window_start_ns
    for timestamp in timestamps:
        if timestamp - previous > maximum_gap_ns:
            raise RunnerError("authoritative commit gap exceeds maximum_stall_s")
        previous = timestamp
    if now_ns - previous > maximum_gap_ns:
        raise RunnerError("live authoritative commit gap exceeds maximum_stall_s")


def _wait(
    description: str,
    timeout_s: float,
    records: Sequence[ProcessRecord],
    predicate: Callable[[], Any],
    *,
    expected_crashed: set[int] | None = None,
    health: Callable[[], None] | None = None,
) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _check_processes(records, expected_crashed or set())
        if health is not None:
            health()
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise RunnerError(f"timed out waiting for {description}")


def inject_sigkill_crashes(
    records_by_replica: Mapping[int, ProcessRecord],
    targets: Sequence[int],
    *,
    timeout_s: float,
    clock_ns: Callable[[], int] = monotonic_raw_ns,
    kill_group: Callable[[int, int], None] = os.killpg,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    if tuple(targets) != CRASH_TARGETS or set(records_by_replica) != set(REPLICA_IDS):
        raise RunnerError("crash injection requires exact registered targets 0 then 1")
    if timeout_s <= 0:
        raise RunnerError("crash confirmation timeout must be positive")
    records = [records_by_replica[target] for target in targets]
    groups = [record.pgid for record in records]
    if len(set(groups)) != len(groups) or any(group <= 1 for group in groups) or os.getpgrp() in groups:
        raise RunnerError("refusing unsafe crash process-group targets")
    markers: list[dict[str, Any]] = []
    for record in records:
        requested = clock_ns()
        try:
            kill_group(record.pgid, signal.SIGKILL)
        except ProcessLookupError as exc:
            raise RunnerError(f"crash target {record.name} was already absent") from exc
        markers.append(
            {
                "replica_id": record.replica_id,
                "pid": record.pid,
                "pgid": record.pgid,
                "signal": "SIGKILL",
                "signal_number": int(signal.SIGKILL),
                "requested_monotonic_raw_ns": requested,
                "confirmed_exit": None,
            }
        )
    deadline = monotonic() + timeout_s
    pending = set(range(len(records)))
    while pending and monotonic() < deadline:
        for index in tuple(pending):
            return_code = records[index].process.poll()
            if return_code is None:
                continue
            if return_code != -signal.SIGKILL:
                raise RunnerError(f"{records[index].name} did not exit from SIGKILL")
            markers[index]["confirmed_exit"] = {
                "pid": records[index].pid,
                "pgid": records[index].pgid,
                "signal": "SIGKILL",
                "signal_number": int(signal.SIGKILL),
                "observed_monotonic_raw_ns": clock_ns(),
            }
            pending.remove(index)
        if pending:
            sleep(0.01)
    if pending:
        raise RunnerError(f"crash targets did not exit: {[records[index].replica_id for index in sorted(pending)]}")
    return markers


def _common_single_payload(
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    event_type: str,
) -> dict[str, Any] | None:
    values: list[dict[str, Any]] = []
    for replica in SURVIVORS:
        events = [event for event in streams[f"replica-{replica}"] if event.get("event_type") == event_type]
        if not events:
            return None
        if len(events) != 1 or not isinstance(events[0].get("payload"), dict):
            raise RunnerError(f"replica-{replica} emitted invalid {event_type} cardinality")
        values.append(dict(events[0]["payload"]))
    if any(value != values[0] for value in values[1:]):
        raise RunnerError(f"survivors disagree on {event_type} payload")
    return values[0]


def _observer_event(streams: Mapping[str, Sequence[Mapping[str, Any]]], event_type: str) -> dict[str, Any] | None:
    events = [dict(event) for event in streams[AUTHORITATIVE_SOURCE_ID] if event.get("event_type") == event_type]
    if not events:
        return None
    if len(events) != 1:
        raise RunnerError(f"authoritative observer emitted {len(events)} {event_type} events")
    return events[0]


def build_epochs_document(decoded: DecodedBundle, command_payload: Mapping[str, Any]) -> dict[str, Any]:
    expected_command_fields = {
        "command_block_height",
        "command_block_hash",
        "payload_digest",
        "predecessor_epoch_number",
        "predecessor_epoch_digest",
        "successor_epoch_number",
        "successor_epoch_digest",
        "activation_delay_blocks",
        "activation_height",
    }
    if set(command_payload) != expected_command_fields:
        raise RunnerError("epoch command event does not have the canonical field set")
    if (
        command_payload["predecessor_epoch_number"] != 0
        or command_payload["successor_epoch_number"] != decoded.epoch_number
        or command_payload["predecessor_epoch_digest"] != decoded.previous_epoch_digest
        or command_payload["successor_epoch_digest"] != decoded.epoch_digest
        or command_payload["activation_delay_blocks"] != ACTIVATION_DELAY_BLOCKS
    ):
        raise RunnerError("committed command does not match the manager bundle")
    return {
        "schema_version": 1,
        "replica_count": 7,
        "fault_threshold": 2,
        "quorum": 5,
        "membership": list(REPLICA_IDS),
        "epochs": [
            {
                "epoch_number": 0,
                "epoch_digest": decoded.previous_epoch_digest,
                "trees": [
                    {
                        "tree_id": root,
                        "fanout": 2,
                        "members_breadth_first": [(root + offset) % 7 for offset in range(7)],
                        "wait_exempt": [],
                    }
                    for root in REPLICA_IDS
                ],
                "command": None,
            },
            {
                "epoch_number": decoded.epoch_number,
                "epoch_digest": decoded.epoch_digest,
                "trees": [
                    {
                        "tree_id": tree.tree_id,
                        "fanout": tree.fanout,
                        "members_breadth_first": list(tree.members),
                        "wait_exempt": list(tree.wait_exempt),
                    }
                    for tree in decoded.trees
                ],
                "command": dict(command_payload),
            },
        ],
    }


def build_manifest(
    *,
    run_id: str,
    revision: str,
    profile_bytes: bytes,
    records: Sequence[ProcessRecord],
    source_instances: Mapping[str, str],
    minimum_post_activation_grace_ns: int,
    baseline_start_ns: int,
    end_ns: int,
    crash_markers: Sequence[Mapping[str, Any]],
    complete: bool,
    interrupted: bool,
    runtime_error: str | None,
    unexpected_survivor_exits: Sequence[Any],
    crash_configuration_boundary: Mapping[str, Any] | None = None,
    runtime: Mapping[str, Any] | None = None,
    runtime_artifacts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if (
        type(minimum_post_activation_grace_ns) is not int
        or minimum_post_activation_grace_ns <= 0
    ):
        raise RunnerError("manifest minimum post-activation grace must be positive")
    record_by_replica = {record.replica_id: record for record in records if record.replica_id is not None}
    manager = next((record for record in records if record.name == MANAGER_SOURCE_ID), None)
    if set(record_by_replica) != set(REPLICA_IDS) or manager is None:
        raise RunnerError("manifest requires seven replicas and one adaptive manager")
    sources = [
        {
            "source_kind": "replica",
            "source_id": f"replica-{replica}",
            "source_instance": source_instances[f"replica-{replica}"],
            "pid": record_by_replica[replica].pid,
            "pgid": record_by_replica[replica].pgid,
            "path": f"raw/replica-{replica}.jsonl",
        }
        for replica in REPLICA_IDS
    ]
    sources.append(
        {
            "source_kind": "adaptation_manager",
            "source_id": MANAGER_SOURCE_ID,
            "source_instance": source_instances[MANAGER_SOURCE_ID],
            "pid": manager.pid,
            "pgid": manager.pgid,
            "path": "raw/adaptive-manager.jsonl",
        }
    )
    manifest = {
        "schema_version": 1,
        "scenario": SCENARIO,
        "run_id": run_id,
        "kauri_revision": revision,
        "kauri_worktree_clean": True,
        "profile": {
            "identity": PROFILE_ID,
            "path": "profile.json",
            "sha256": sha256_bytes(profile_bytes),
        },
        "run_completion": {
            "complete": complete,
            "interrupted": interrupted,
            "runtime_error": runtime_error,
            "unexpected_survivor_exits": list(unexpected_survivor_exits),
        },
        "replica_count": 7,
        "fault_threshold": 2,
        "quorum": 5,
        "membership": list(REPLICA_IDS),
        "authoritative_observer": AUTHORITATIVE_SOURCE_ID,
        "manager": {
            "source_id": MANAGER_SOURCE_ID,
            "receives_crash_ground_truth": False,
        },
        "bucket_width_ns": BUCKET_WIDTH_NS,
        "minimum_post_activation_grace_ns": minimum_post_activation_grace_ns,
        "baseline_start_ns": baseline_start_ns,
        "end_ns": end_ns,
        "sources": sources,
        "crash_markers": [dict(marker) for marker in crash_markers],
        "crash_configuration_boundary": (
            dict(crash_configuration_boundary)
            if crash_configuration_boundary is not None
            else None
        ),
        "runtime": dict(runtime or {}),
        "runtime_artifacts": [dict(artifact) for artifact in runtime_artifacts],
    }
    if set(manifest) != MANIFEST_FIELDS:
        raise RunnerError("internal manifest schema drift")
    return manifest


def _shutdown_processes(records: Sequence[ProcessRecord]) -> list[str]:
    unexpected: list[str] = []
    groups = {record.pgid for record in records if record.process.poll() is None}
    if any(group <= 1 for group in groups) or os.getpgrp() in groups:
        raise RunnerError("refusing unsafe campaign cleanup groups")
    for sig, grace in ((signal.SIGINT, 10.0), (signal.SIGTERM, 3.0), (signal.SIGKILL, 1.0)):
        active = [record for record in records if record.process.poll() is None]
        for record in active:
            try:
                os.killpg(record.pgid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace
        while any(record.process.poll() is None for record in records) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not any(record.process.poll() is None for record in records):
            break
    for record in records:
        try:
            return_code = record.process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            return_code = record.process.poll()
        if record.replica_id in CRASH_TARGETS:
            if return_code != -signal.SIGKILL:
                unexpected.append(f"{record.name}:{return_code}")
        elif return_code != 0:
            unexpected.append(f"{record.name}:{return_code}")
        record.log_handle.close()
    return unexpected


def _wait_listeners_stopped(ports: Sequence[int], timeout_s: float) -> list[int]:
    deadline = time.monotonic() + timeout_s
    remaining = listening_ports(ports)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = listening_ports(ports)
    return remaining


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    scenario_directory = Path(__file__).resolve().parent
    repository = scenario_directory.parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=repository)
    parser.add_argument("--profile", type=Path, default=scenario_directory / "profile.json")
    parser.add_argument("--app-binary", type=Path, default=repository / "build-adaptive/examples/hotstuff-app")
    parser.add_argument("--manager-binary", type=Path, default=repository / "build-adaptive/examples/adaptation-manager")
    parser.add_argument("--keygen-binary", type=Path, default=repository / "build-adaptive/hotstuff-keygen")
    parser.add_argument("--tls-keygen-binary", type=Path, default=repository / "build-adaptive/hotstuff-tls-keygen")
    parser.add_argument("--results-root", type=Path, default=repository / "results/n7-crash-recovery")
    parser.add_argument("--peer-port", type=int, default=25100)
    parser.add_argument("--client-port", type=int, default=26100)
    parser.add_argument("--manager-port", type=int, default=27100)
    parser.add_argument("--startup-timeout", type=float, default=90.0)
    parser.add_argument("--phase-timeout", type=float, default=240.0)
    parser.add_argument("--crash-confirm-timeout", type=float, default=5.0)
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    repository = args.repository.resolve()
    profile_path = args.profile.resolve()
    profile, profile_bytes = load_frozen_profile(profile_path)
    minimum_post_activation_grace_ns = _profile_duration_ns(
        profile, MINIMUM_POST_ACTIVATION_GRACE_PROFILE_FIELD
    )
    maximum_activation_to_successor_ns = _profile_duration_ns(
        profile, MAXIMUM_ACTIVATION_TO_SUCCESSOR_PROFILE_FIELD
    )
    snapshot = verify_repository_state(repository)
    binaries = {
        "app": args.app_binary.resolve(),
        "manager": args.manager_binary.resolve(),
        "keygen": args.keygen_binary.resolve(),
        "tls_keygen": args.tls_keygen_binary.resolve(),
    }
    for label, path in binaries.items():
        _assert_executable(path, label)
    if min(args.startup_timeout, args.phase_timeout, args.crash_confirm_timeout) <= 0:
        raise RunnerError("campaign timeouts must be positive")
    monotonic_raw_ns()
    ports = required_ports(args.peer_port, args.client_port, args.manager_port)
    occupied = ports_in_use(ports)
    if occupied:
        raise RunnerError(f"campaign ports are already in use: {occupied}")

    run_directory = create_run_directory(args.results_root.resolve())
    run_id = run_directory.name
    _write_private(run_directory / "profile.json", profile_bytes)
    source_instances = {
        f"replica-{replica}": f"{run_id}-replica-{replica}-{uuid.uuid4().hex}"
        for replica in REPLICA_IDS
    }
    source_instances[MANAGER_SOURCE_ID] = f"{run_id}-manager-{uuid.uuid4().hex}"
    state_path = run_directory / "runner-state.json"
    state: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "revision": snapshot.revision,
        "phase": "identity_generation",
        "runtime_error": None,
        "binaries": {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in binaries.items()
        },
    }
    _write_json_exclusive(state_path, state)

    records: list[ProcessRecord] = []
    records_by_replica: dict[int, ProcessRecord] = {}
    crash_markers: list[dict[str, Any]] = []
    crash_configuration_boundary: dict[str, Any] | None = None
    runtime_artifacts: list[dict[str, Any]] = []
    runtime = runtime_parameters(
        profile,
        app_binary=binaries["app"],
        manager_binary=binaries["manager"],
    )
    baseline_start_ns = monotonic_raw_ns()
    end_ns = baseline_start_ns + 1
    runtime_error: str | None = None
    interrupted = False
    unexpected_exits: list[str] = []
    manifest_path = run_directory / "manifest.json"
    manifest_written = False

    def update_manifest(*, complete: bool = False) -> None:
        nonlocal manifest_written
        if len(records) != 8:
            return
        value = build_manifest(
            run_id=run_id,
            revision=snapshot.revision,
            profile_bytes=profile_bytes,
            records=records,
            source_instances=source_instances,
            minimum_post_activation_grace_ns=minimum_post_activation_grace_ns,
            baseline_start_ns=baseline_start_ns,
            end_ns=end_ns,
            crash_markers=crash_markers,
            crash_configuration_boundary=crash_configuration_boundary,
            complete=complete,
            interrupted=interrupted,
            runtime_error=runtime_error,
            unexpected_survivor_exits=unexpected_exits,
            runtime=runtime,
            runtime_artifacts=runtime_artifacts,
        )
        if manifest_written:
            _replace_json(manifest_path, value)
        else:
            _write_json_exclusive(manifest_path, value)
            manifest_written = True

    interrupted_signal: int | None = None

    def request_shutdown(signum: int, _frame: Any) -> None:
        nonlocal interrupted, interrupted_signal
        interrupted = True
        interrupted_signal = signum
        raise KeyboardInterrupt

    previous_handlers = {
        signum: signal.signal(signum, request_shutdown)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        bls, tls, issuer = generate_identities(
            binaries["keygen"], binaries["tls_keygen"], run_directory / "config"
        )
        _, _, manager_command, replica_commands, runtime_artifacts = write_runtime_inputs(
            run_directory,
            profile,
            bls,
            tls,
            issuer,
            peer_port=args.peer_port,
            client_port=args.client_port,
            manager_port=args.manager_port,
            run_id=run_id,
            source_instances=source_instances,
            app_binary=binaries["app"],
            manager_binary=binaries["manager"],
        )
        state["phase"] = "launch"
        _replace_json(state_path, state)
        manager = spawn_process(
            MANAGER_SOURCE_ID,
            manager_command,
            run_directory / "logs" / "adaptive-manager.log",
            run_directory,
            replica_id=None,
        )
        records.append(manager)
        for replica_id in REPLICA_IDS:
            record = spawn_process(
                f"replica-{replica_id}",
                replica_commands[replica_id],
                run_directory / "logs" / f"replica-{replica_id}.log",
                run_directory,
                replica_id=replica_id,
            )
            records.append(record)
            records_by_replica[replica_id] = record
        update_manifest()

        def all_ready() -> bool:
            streams = _event_streams(run_directory)
            return all(
                sum(event.get("event_type") == "process.ready" for event in streams[source]) == 1
                for source in (*[f"replica-{replica}" for replica in REPLICA_IDS], MANAGER_SOURCE_ID)
            )

        _wait("all structured process.ready events", args.startup_timeout, records, all_ready)

        def first_common_commit() -> CommonEpochCommit | None:
            streams = _event_streams(run_directory)
            return find_first_common_epoch_commit(
                streams[AUTHORITATIVE_SOURCE_ID],
                commit_witness_timestamps(streams, REPLICA_IDS),
                participants=REPLICA_IDS,
                epoch_number=0,
            )

        first_commit = _wait("first common authoritative commit", args.startup_timeout, records, first_common_commit)
        baseline_start_ns = first_commit.common_ns
        end_ns = baseline_start_ns + 1
        update_manifest()
        state["phase"] = "baseline"
        state["baseline_start_ns"] = baseline_start_ns
        _replace_json(state_path, state)
        baseline_duration_ns = int(profile["baseline_bucket_count"]) * BUCKET_WIDTH_NS
        maximum_gap_ns = int(float(profile["maximum_stall_s"]) * 1_000_000_000)
        degraded_maximum_gap_ns = int(
            float(profile["degraded_maximum_stall_s"]) * 1_000_000_000
        )

        def baseline_cycle() -> dict[str, Any] | None:
            if monotonic_raw_ns() < baseline_start_ns + baseline_duration_ns:
                return None
            streams = _event_streams(run_directory)
            return find_common_root_cycle(
                streams[AUTHORITATIVE_SOURCE_ID],
                commit_witness_timestamps(streams, REPLICA_IDS),
                participants=REPLICA_IDS,
                epoch_number=0,
                tree_roots={tree: tree for tree in REPLICA_IDS},
                expected_roots=REPLICA_IDS,
                require_terminal=False,
            )

        def baseline_health() -> None:
            streams = _event_streams(run_directory)
            _enforce_observer_stall(
                streams[AUTHORITATIVE_SOURCE_ID],
                window_start_ns=baseline_start_ns,
                now_ns=monotonic_raw_ns(),
                maximum_gap_ns=maximum_gap_ns,
            )

        _wait(
            "seven complete baseline buckets and one common root cycle 0..6",
            args.phase_timeout,
            records,
            baseline_cycle,
            health=baseline_health,
        )

        source_watermarks, source_offsets = replica_event_tail_snapshot(run_directory)
        configuration_poller = FreshConfigurationPoller(
            run_directory,
            source_watermarks,
            start_offsets=source_offsets,
            maximum_skew_ns=runtime["aggregation_timeout_ms"] * 1_000_000,
        )
        crash_configuration_boundary = _wait(
            "fresh common epoch-0 tree-6 active configuration",
            args.phase_timeout,
            records,
            configuration_poller.poll,
        )

        state["phase"] = "crash"
        state["crash_configuration_boundary"] = crash_configuration_boundary
        _replace_json(state_path, state)
        crash_markers = inject_sigkill_crashes(
            records_by_replica,
            CRASH_TARGETS,
            timeout_s=args.crash_confirm_timeout,
        )
        assert crash_configuration_boundary is not None
        assert_crash_boundary_held(
            crash_configuration_boundary,
            _event_streams(run_directory),
            crash_request_ns=max(
                marker["requested_monotonic_raw_ns"] for marker in crash_markers
            ),
        )
        update_manifest()

        def transition_ready() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
            streams = _event_streams(run_directory)
            command = _common_single_payload(streams, "epoch.command_committed")
            activation = _common_single_payload(streams, "epoch.activated")
            observer_command = _observer_event(streams, "epoch.command_committed")
            observer_activation = _observer_event(streams, "epoch.activated")
            if command is None or activation is None or observer_command is None or observer_activation is None:
                return None
            return command, observer_command, observer_activation

        def degraded_health() -> None:
            streams = _event_streams(run_directory)
            _enforce_observer_stall(
                streams[AUTHORITATIVE_SOURCE_ID],
                window_start_ns=min(marker["requested_monotonic_raw_ns"] for marker in crash_markers),
                now_ns=monotonic_raw_ns(),
                maximum_gap_ns=degraded_maximum_gap_ns,
            )

        state["phase"] = "awaiting_committed_successor"
        _replace_json(state_path, state)
        command_payload, observer_command, observer_activation = _wait(
            "one common signed command and exact successor activation",
            args.phase_timeout,
            records,
            transition_ready,
            expected_crashed=set(CRASH_TARGETS),
            health=degraded_health,
        )
        decoded = decode_epoch_change_bundle((run_directory / "successor.bundle").read_bytes())
        if decoded.generation_seed != runtime["snapshot_seed"]:
            raise RunnerError("manager bundle generation seed differs from frozen runtime")
        epochs_document = build_epochs_document(decoded, command_payload)
        _write_json_exclusive(run_directory / "epochs.json", epochs_document)
        observer_activation_ns = _event_timestamp(observer_activation)
        minimum_post_start_ns = (
            observer_activation_ns + minimum_post_activation_grace_ns
        )
        maximum_successor_deadline_ns = (
            observer_activation_ns + maximum_activation_to_successor_ns
        )
        state["phase"] = "awaiting_first_common_successor_commit"
        state["command_ns"] = _event_timestamp(observer_command)
        state["activation_ns"] = observer_activation_ns
        state["minimum_post_start_ns"] = minimum_post_start_ns
        _replace_json(state_path, state)

        def first_common_successor_commit() -> CommonEpochCommit | None:
            streams = _event_streams(run_directory)
            result = find_first_common_epoch_commit(
                streams[AUTHORITATIVE_SOURCE_ID],
                commit_witness_timestamps(streams, SURVIVORS),
                participants=SURVIVORS,
                epoch_number=1,
            )
            return enforce_common_epoch_commit_deadline(
                result,
                now_ns=monotonic_raw_ns(),
                deadline_ns=maximum_successor_deadline_ns,
            )

        def successor_commit_health() -> None:
            now_ns = monotonic_raw_ns()
            streams = _event_streams(run_directory)
            enforce_common_epoch_commit_deadline(
                find_first_common_epoch_commit(
                    streams[AUTHORITATIVE_SOURCE_ID],
                    commit_witness_timestamps(streams, SURVIVORS),
                    participants=SURVIVORS,
                    epoch_number=1,
                ),
                now_ns=now_ns,
                deadline_ns=maximum_successor_deadline_ns,
            )
            _enforce_observer_stall(
                streams[AUTHORITATIVE_SOURCE_ID],
                window_start_ns=min(
                    marker["requested_monotonic_raw_ns"]
                    for marker in crash_markers
                ),
                now_ns=now_ns,
                maximum_gap_ns=degraded_maximum_gap_ns,
            )

        first_common_successor = _wait(
            "first common epoch-1 commit from every survivor",
            float(profile[MAXIMUM_ACTIVATION_TO_SUCCESSOR_PROFILE_FIELD]) + 1.0,
            records,
            first_common_successor_commit,
            expected_crashed=set(CRASH_TARGETS),
            health=successor_commit_health,
        )
        first_common_successor_ns = first_common_successor.common_ns
        minimum_post_start_ns, post_start_ns = post_measurement_boundaries(
            activation_ns=observer_activation_ns,
            first_common_successor_ns=first_common_successor_ns,
            minimum_post_activation_grace_ns=minimum_post_activation_grace_ns,
        )
        post_duration_ns = int(profile["post_bucket_count"]) * BUCKET_WIDTH_NS
        tree_roots = {tree.tree_id: tree.members[0] for tree in decoded.trees}
        successor_root_cycle = tuple(tree.members[0] for tree in decoded.trees)

        def post_cycle() -> dict[str, Any] | None:
            if monotonic_raw_ns() < post_start_ns + post_duration_ns:
                return None
            streams = _event_streams(run_directory)
            post_observer_events = [
                event
                for event in streams[AUTHORITATIVE_SOURCE_ID]
                if event.get("event_type") == "block.committed"
                and _event_timestamp(event) >= post_start_ns
            ]
            return find_common_root_cycle(
                post_observer_events,
                commit_witness_timestamps(streams, SURVIVORS),
                participants=SURVIVORS,
                epoch_number=1,
                tree_roots=tree_roots,
                expected_roots=successor_root_cycle,
                require_terminal=False,
            )

        def post_health() -> None:
            streams = _event_streams(run_directory)
            _enforce_observer_stall(
                streams[AUTHORITATIVE_SOURCE_ID],
                window_start_ns=post_start_ns,
                now_ns=max(post_start_ns, monotonic_raw_ns()),
                maximum_gap_ns=maximum_gap_ns,
            )

        state["phase"] = "post_successor"
        state["first_common_successor_ns"] = first_common_successor_ns
        state["minimum_post_start_ns"] = minimum_post_start_ns
        state["post_start_ns"] = post_start_ns
        _replace_json(state_path, state)
        _wait(
            "seven complete post buckets and the ranked successor root cycle",
            args.phase_timeout,
            records,
            post_cycle,
            expected_crashed=set(CRASH_TARGETS),
            health=post_health,
        )
        end_ns = monotonic_raw_ns()
        update_manifest()
    except KeyboardInterrupt:
        name = signal.Signals(interrupted_signal).name if interrupted_signal is not None else "interrupt"
        runtime_error = f"campaign interrupted by {name}"
    except (RunnerError, OSError, subprocess.SubprocessError) as exc:
        runtime_error = str(exc)
    finally:
        state["phase"] = "cleanup"
        state["runtime_error"] = runtime_error
        _replace_json(state_path, state)
        if records:
            try:
                if runtime_error is None and not interrupted:
                    _check_processes(records, set(CRASH_TARGETS))
                unexpected_exits.extend(_shutdown_processes(records))
            except RunnerError as exc:
                runtime_error = runtime_error or str(exc)
        remaining = _wait_listeners_stopped(ports, 5.0)
        if remaining:
            runtime_error = runtime_error or f"listeners remained active after cleanup: {remaining}"
        if end_ns <= baseline_start_ns:
            end_ns = baseline_start_ns + 1
        if manifest_written:
            update_manifest(complete=runtime_error is None and not interrupted and not unexpected_exits)
        state["phase"] = "finished"
        state["runtime_error"] = runtime_error
        state["unexpected_survivor_exits"] = unexpected_exits
        state["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        _replace_json(state_path, state)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    print(f"results: {run_directory}")
    if runtime_error is not None or interrupted or unexpected_exits:
        print(f"INCOMPLETE: {runtime_error or unexpected_exits}")
        return 1
    if not (run_directory / "epochs.json").is_file() or not manifest_written:
        print("INCOMPLETE: canonical manifest or epoch input is absent")
        return 1

    validated_directory = run_directory / "validated"
    validator = Path(__file__).resolve().with_name("validator.py")
    plotter = Path(__file__).resolve().with_name("plot.py")
    validation = subprocess.run(
        [
            sys.executable,
            str(validator),
            "--manifest",
            str(manifest_path),
            "--epochs",
            str(run_directory / "epochs.json"),
            "--output-dir",
            str(validated_directory),
        ],
        cwd=repository,
        check=False,
    )
    if validation.returncode != 0:
        print("FAIL: canonical validator rejected the preserved run")
        return 1
    plotted = subprocess.run(
        [sys.executable, str(plotter), str(validated_directory)],
        cwd=repository,
        check=False,
    )
    if plotted.returncode != 0:
        print("FAIL: PASS evidence was preserved but figure generation failed")
        return 1
    print(f"PASS: {validated_directory}")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except RunnerError as exc:
        print(f"runner error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
