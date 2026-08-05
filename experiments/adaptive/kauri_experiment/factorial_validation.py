"""Independent, fail-closed validation for preserved SHAPE25 slot artifacts.

The validator intentionally imports no runtime decision function and no other
validator.  The only project dependency is the frozen manifest loader and its
identity constants.  Everything that can affect a result--slot derivation,
FNV actor rotation, epoch bundle decoding, responsiveness, shape scoring,
commit buckets, and factorial contrasts--is recomputed here from preserved
bytes.

Version-1 slot directories contain these immutable files::

    manifest.json                 exact frozen profile bytes
    plan.json                     exact canonical frozen plan
    runtime.json                  canonical pure runtime contract
    slot.json                     launch/materialization receipt
    phase-cutoffs.json            live event references and phase windows
    throughput.json               explicit commit-derived buckets
    outcome.json                  NOT_STARTED-first terminal history + seal
    raw/adaptive-manager.jsonl    native manager structured events
    raw/replica-<id>.jsonl        native replica structured events
    raw/process/replica-<id>.stdout.log
    raw/process/replica-<id>.stderr.log
                                    native KAURI_FAULT markers
    transitions/<id>/successor.bundle
    transitions/<id>/evidence-snapshot.json

``slot.json`` records the one shared CLOCK_MONOTONIC_RAW anchor and the fully
materialized manager/replica argv.  Secret argv values must be replaced with
``hmac-sha256:<key-id>:<64 lowercase hex>``; secret bytes are forbidden.
``outcome.json`` seals every other regular file by SHA-256.  PASS is possible
only when every native/raw proof is present.  Absent launcher state, outcome,
or raw streams is INCOMPLETE; malformed, duplicated, mixed-run, or forged
evidence is FAIL.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
import datetime as dt
import hashlib
import hmac
import itertools
import json
import math
from pathlib import Path
import re
import signal
from typing import Any

from .factorial_manifest import (
    EXPECTED_ARM_CODES,
    EXPECTED_BLOCK_COUNT,
    EXPECTED_SLOT_COUNT,
    FROZEN_MANIFEST_ID,
    FROZEN_MANIFEST_SHA256,
    FROZEN_PLAN_SHA256,
    LEGACY_MANIFEST_ID,
    LEGACY_MANIFEST_SHA256,
    LEGACY_PLAN_SHA256,
    V2_MANIFEST_ID,
    V2_MANIFEST_SHA256,
    V2_PLAN_SHA256,
    V3_MANIFEST_ID,
    V3_MANIFEST_SHA256,
    V3_PLAN_SHA256,
    V4_MANIFEST_ID,
    V4_MANIFEST_SHA256,
    V4_PLAN_SHA256,
    FrozenFactorialManifest,
    load_frozen_manifest_bytes,
)

ARTIFACT_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
PLAN_FILENAME = "plan.json"
RUNTIME_FILENAME = "runtime.json"
SLOT_FILENAME = "slot.json"
PHASE_CUTOFFS_FILENAME = "phase-cutoffs.json"
THROUGHPUT_FILENAME = "throughput.json"
OUTCOME_FILENAME = "outcome.json"
AUTHORIZATION_FILENAME = "execution-authorization.json"
BUILD_PROVENANCE_FILENAME = "runtime/exact-build-provenance.json"
EXECUTION_PROVENANCE_FILENAME = "runtime/execution-provenance.json"
CLEANUP_LEDGER_FILENAME = "cleanup-ledger.json"
RUNNER_STATE_FILENAME = "runner-state.jsonl"
SMOKE_AUTHORIZATION_FILENAME = "smoke-execution-authorization.json"
CAMPAIGN_AUTHORIZATION_FILENAME = "campaign-authorization.json"
CAMPAIGN_CONTRACT_FILENAME = "campaign-execution-contract.json"
CAMPAIGN_LEDGER_FILENAME = "campaign-attempt-ledger.jsonl"
CAMPAIGN_SUMMARY_FILENAME = "campaign-execution-summary.json"
BUILD_EVIDENCE_DIRECTORY = "build-evidence"
_BUILD_EVIDENCE_GROUPS = {
    "binaries": "binaries",
    "build_metadata": "build-metadata",
}
MANAGER_EVENTS_FILENAME = "raw/adaptive-manager.jsonl"
REPLICA_EVENTS_PATTERN = "raw/replica-{replica_id}.jsonl"
REPLICA_STDERR_PATTERN = "raw/process/replica-{replica_id}.stderr.log"
EXCLUDED_SMOKE_SLOT_ID = "smoke-n7-f2-PS"
V2_RUNTIME_SHA256 = (
    "2265155d61756385175baa6b5dd5e8a4fe03eef0a3b29a1cefda4c8a4c2a454a"
)
V2_SMOKE_RUNTIME_SHA256 = (
    "2be6930b5770b5bbe4991673675a1f1d8443d3526c388b744b601c59e9216a37"
)
V3_RUNTIME_SHA256 = (
    "ff62f12a584b5345e0043a3cf1d9567a2264261bafe25dec5bb9afe079513b92"
)
V3_SMOKE_RUNTIME_SHA256 = (
    "4e4231b3cd487549da5e468aafc67cefbc11c39459b79dcb37a679225d360c85"
)
V4_RUNTIME_SHA256 = (
    "27ca88025f5a22f74707bf724ca7e2155c7be8574043e96434cb7e553338a376"
)
V4_SMOKE_RUNTIME_SHA256 = (
    "80a54a9035ac2cc816fe985b4011c060fe21a81894da56156ce75dbda7e81046"
)
FROZEN_RUNTIME_SHA256 = (
    "5d66d23d9f2ac9c774c157d51435763ec7c7a19cfd1d1f5bc5ea67a32f1ecabf"
)
FROZEN_SMOKE_RUNTIME_SHA256 = (
    "455178e72413abb31ece95e37364e8f52d3318397c0d46b78d4451504ba03c2e"
)
LEGACY_RUNTIME_SHA256 = (
    "326927b131cdc50f5aa9d542a21a12de5c26f4ac81726f75eafd389c945af681"
)
LEGACY_SMOKE_RUNTIME_SHA256 = (
    "cf73c4e4ec7df8fbca29421c5897bafca7a2fa6e5f08afc571d014b8d66ebdae"
)

OUTCOMES = ("NOT_STARTED", "PASS", "FAIL", "INCOMPLETE")
PHASES = ("baseline", "fault_evidence", "epoch1_stable", "epoch2_stable")
CUTOFF_NAMES = (
    "baseline_stable",
    "fault_window_open",
    "epoch1_command",
    "epoch1_activation",
    "epoch1_stable",
    "shape_v1_computed",
    "epoch2_command",
    "epoch2_activation",
    "epoch2_stable",
    "epoch2_drain_complete",
)
CUTOFF_RULE = "slot_local_monotonic_phase_order_v1"
EVIDENCE_WINDOW_RULE = "fresh_exact_predecessor_after_common_commit"
SELECTOR_VERSION = "shape-v1"
SHAPE_TIE_RULE = "lower-latency-risk-churn-current-canonical-v1"
REFERENCE_TREE_RULE = "lowest-tree-id-prefix-q-v1"

_NANOSECONDS_PER_SECOND = 1_000_000_000
_UINT64_MAX = (1 << 64) - 1
_MAX_JSON_BYTES = 64 * 1024 * 1024
_MAX_EVENT_LINE_BYTES = 4 * 1024 * 1024
_MAX_EVENT_STREAM_BYTES = 1024 * 1024 * 1024
_SNAPSHOT_SEED = 0xA2F7
_REPLICA_NETWORK_WORKERS = 2
_MAX_REPLICA_MESSAGE_BYTES = 4 << 20
_MAX_COMMAND_BYTES = 4096
_MAX_ANCESTRY_BLOCKS = 128
_ISSUER_ID = 1
_PLACEMENT_POLICY_VERSION = "adaptive-v2-performance-optimization-v1"
_BUNDLE_DOMAIN = b"kauri-adaptive-v2-epoch-change-bundle-v1"
_AUTHORIZED_COMMAND_DOMAIN = b"kauri-authorized-epoch-change-v1"
_EPOCH_CHANGE_PAYLOAD_DOMAIN = b"kauri-epoch-change-payload-v1"
_EPOCH_DEFINITION_DOMAIN = b"kauri-epoch-definition-v2"
_MEMBERSHIP_DOMAIN = b"kauri-membership-v1"
_OBSERVATION_DOMAIN = b"kauri-response-observation-v1"
_SNAPSHOT_DOMAIN = b"kauri-adaptation-snapshot-v1"
_SHAPE_TOPOLOGY_DOMAIN = b"kauri-shape-v1-topology"
_SHAPE_EVIDENCE_DOMAIN = b"kauri-shape-v1-evidence"
_SHAPE_DECISION_DOMAIN = b"kauri-shape-v1-decision"
_SECP256K1_HALF_ORDER = bytes.fromhex(
    "7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0"
)
_SECP256K1_FIELD = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_HEX_DIGITS = frozenset("0123456789abcdef")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_REDACTION = re.compile(r"hmac-sha256:([A-Za-z0-9_.-]{1,64}):([0-9a-f]{64})")
_FAULT_MARKER = re.compile(
    r"(?:^|\s)KAURI_FAULT fault=([a-z0-9_]+) "
    r"proposal_epoch=(\d+) proposal_tree=(\d+) "
    r"proposal_epoch_digest=([0-9a-f]{64}) "
    r"proposal_block_hash=([0-9a-f]{64}) window=([^\s]+) "
    r"window_start_monotonic_ns=(\d+) window_end_monotonic_ns=(\d+) "
    r"actor=(\d+) action=(forward|omit_aggregate|omit_direct_vote|capacity_exhausted) "
    r"monotonic_ns=(\d+)(?:\s|$)"
)
_REDACTION_KEY_DOMAIN = b"kauri.shape25.launch-redaction-key.v1"
_FORBIDDEN_MANAGER_KEYS = frozenset(
    {
        "actor_ids",
        "byzantine_actor_ids",
        "fault_actor_ids",
        "fault_ids",
        "fault_schedule",
        "orchestrator_truth",
        "performance_label",
    }
)
_FORBIDDEN_MANAGER_TOKENS = (
    "--experiment-rotating-omission-actors",
    "byzantine_actor_ids",
    "fault_actor_ids",
    "orchestrator_truth",
)


class FactorialValidationError(ValueError):
    """A preserved artifact violates a scientific-integrity requirement."""


class _Incomplete(FactorialValidationError):
    pass


class _Reject(FactorialValidationError):
    pass


@dataclass(frozen=True, slots=True)
class _FrozenArtifactIdentity:
    manifest_id: str
    manifest_sha256: str
    plan_sha256: str
    runtime_sha256: str
    smoke_runtime_sha256: str


def _frozen_artifact_identity(manifest_id: str) -> _FrozenArtifactIdentity:
    identities = {
        LEGACY_MANIFEST_ID: _FrozenArtifactIdentity(
            manifest_id=LEGACY_MANIFEST_ID,
            manifest_sha256=LEGACY_MANIFEST_SHA256,
            plan_sha256=LEGACY_PLAN_SHA256,
            runtime_sha256=LEGACY_RUNTIME_SHA256,
            smoke_runtime_sha256=LEGACY_SMOKE_RUNTIME_SHA256,
        ),
        V2_MANIFEST_ID: _FrozenArtifactIdentity(
            manifest_id=V2_MANIFEST_ID,
            manifest_sha256=V2_MANIFEST_SHA256,
            plan_sha256=V2_PLAN_SHA256,
            runtime_sha256=V2_RUNTIME_SHA256,
            smoke_runtime_sha256=V2_SMOKE_RUNTIME_SHA256,
        ),
        V3_MANIFEST_ID: _FrozenArtifactIdentity(
            manifest_id=V3_MANIFEST_ID,
            manifest_sha256=V3_MANIFEST_SHA256,
            plan_sha256=V3_PLAN_SHA256,
            runtime_sha256=V3_RUNTIME_SHA256,
            smoke_runtime_sha256=V3_SMOKE_RUNTIME_SHA256,
        ),
        V4_MANIFEST_ID: _FrozenArtifactIdentity(
            manifest_id=V4_MANIFEST_ID,
            manifest_sha256=V4_MANIFEST_SHA256,
            plan_sha256=V4_PLAN_SHA256,
            runtime_sha256=V4_RUNTIME_SHA256,
            smoke_runtime_sha256=V4_SMOKE_RUNTIME_SHA256,
        ),
        FROZEN_MANIFEST_ID: _FrozenArtifactIdentity(
            manifest_id=FROZEN_MANIFEST_ID,
            manifest_sha256=FROZEN_MANIFEST_SHA256,
            plan_sha256=FROZEN_PLAN_SHA256,
            runtime_sha256=FROZEN_RUNTIME_SHA256,
            smoke_runtime_sha256=FROZEN_SMOKE_RUNTIME_SHA256,
        ),
    }
    identity = identities.get(manifest_id)
    if identity is None:
        _fail("manifest ID is not a known frozen SHAPE25 artifact identity")
    return identity


@dataclass(frozen=True, slots=True)
class Tree:
    tree_id: int
    fanout: int
    pipeline_stretch: int
    members: tuple[int, ...]
    wait_exempt: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DecodedCommand:
    issuer_id: int
    successor_epoch_number: int
    predecessor_epoch_digest: str
    successor_epoch_digest: str
    activation_delay_blocks: int
    payload_digest: str
    signature: bytes


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
    trees: tuple[Tree, ...]


@dataclass(frozen=True, slots=True)
class ReplicaScore:
    replica_id: int
    classification: str
    eligible: bool
    attempt_count: int
    response_rate_ppm: int
    timeout_rate_ppm: int
    latency_percentile_us: int | None


@dataclass(frozen=True, slots=True)
class FaultMarker:
    source_replica: int
    line_number: int
    fault_mode: str
    epoch_number: int
    tree_id: int
    epoch_digest: str
    block_hash: str
    window: str
    window_start_ns: int
    window_end_ns: int
    actor: int
    action: str
    monotonic_ns: int
    raw_line_sha256: str


@dataclass(frozen=True, slots=True)
class PhaseMetric:
    phase: str
    transactions: int
    mean_tps: float
    buckets_tps: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class SlotValidationResult:
    slot_id: str
    outcome: str
    reason: str | None
    block_id: str | None = None
    arm_code: str | None = None
    replica_count: int | None = None
    initial_fanout: int | None = None
    metrics: tuple[PhaseMetric, ...] = ()
    integrity_valid: bool = False
    schema_gaps: tuple[str, ...] = ()
    campaign_member: bool = True

    @property
    def figure_eligible(self) -> bool:
        return (
            self.campaign_member
            and self.outcome == "PASS"
            and self.integrity_valid
            and not self.schema_gaps
        )


@dataclass(frozen=True, slots=True)
class MatchedEstimate:
    contrast: str
    block_ids: tuple[str, ...]
    block_effects_tps: tuple[float, ...]
    mean_tps: float
    sample_standard_deviation_tps: float
    ci95_lower_tps: float
    ci95_upper_tps: float
    positive_block_count: int
    directional_claim_supported: bool


@dataclass(frozen=True, slots=True)
class FactorialEffects:
    endpoint: str
    estimator: str
    uncertainty_interval: str
    directional_claim_rule: str
    placement_main_effect: MatchedEstimate
    shape_main_effect: MatchedEstimate
    placement_shape_interaction: MatchedEstimate
    fault_drop: MatchedEstimate
    containment_recovery: MatchedEstimate
    optimization_gain: MatchedEstimate


@dataclass(frozen=True, slots=True)
class CampaignValidationResult:
    outcome: str
    reason: str | None
    slots: tuple[SlotValidationResult, ...]
    parameter_coverage: tuple[tuple[int, int, str], ...]
    headline_effects: FactorialEffects | None
    figure_eligible: bool


@dataclass(frozen=True, slots=True)
class _ExpectedSlot:
    slot_id: str
    block_id: str
    arm_code: str
    ordinal: int
    slot_nonce: int
    block_index: int
    blocks_in_cell: int
    block_execution_ordinal: int
    arm_execution_position: int
    execution_ordinal: int
    scientific_seed: int
    replica_count: int
    f: int
    q: int
    tree_count: int
    initial_fanout: int
    initial_depth: int
    candidate_depths: tuple[tuple[int, int], ...]
    worst_candidate_depth: int
    candidate_fanouts: tuple[int, ...]
    pipeline_stretch: int
    placement_adaptation: bool
    shape_adaptation: bool
    actor_ids: tuple[int, ...]
    peer_base: int
    client_base: int
    manager_port: int


@dataclass(frozen=True, slots=True)
class _NativeEvent:
    relative_path: str
    line_number: int
    source_kind: str
    source_id: str
    source_instance: str
    source_sequence: int
    monotonic_ns: int
    event_type: str
    payload: Mapping[str, Any]
    line_sha256: str


@dataclass(frozen=True, slots=True)
class _EvidenceRecord:
    ingestion_sequence: int
    acceptance_monotonic_ns: int
    observation_id: str
    reporter_id: int
    target_id: int
    epoch_number: int
    tree_id: int
    epoch_digest: str
    block_hash: str
    message_type: str
    outcome: str
    response_duration_us: int
    deadline_duration_us: int
    reporter_monotonic_ns: int
    reporter_sequence: int
    signer_set: tuple[int, ...]


def _fail(message: str) -> None:
    raise _Reject(message)


def _effective_omission_contract(
    manifest: FrozenFactorialManifest,
    expected: _ExpectedSlot,
) -> tuple[str, int]:
    window_suffix = {
        "rotating_intermittent_omission_v1": "rotating-omission-v1",
        "persistent_selected_omission_v1": "persistent-omission-v1",
    }.get(manifest.byzantine.mode)
    if window_suffix is None:
        _fail("manifest Byzantine omission mode is unknown")
    max_omissions = (
        len(expected.actor_ids)
        if expected.slot_id == EXCLUDED_SMOKE_SLOT_ID
        and manifest.byzantine.mode == "persistent_selected_omission_v1"
        else manifest.byzantine.max_omissions_per_proposal
    )
    return f"{expected.block_id}-{window_suffix}", max_omissions


def _incomplete(message: str) -> None:
    raise _Incomplete(message)


def _duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _parse_json_bytes(payload: bytes, label: str) -> dict[str, Any]:
    if not payload or len(payload) > _MAX_JSON_BYTES:
        _fail(f"{label} is empty or exceeds the fixed JSON bound")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_duplicates,
            parse_constant=lambda value: _fail(
                f"{label} contains non-finite constant {value}"
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _Reject(f"{label} is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as error:
        raise _Reject("value cannot be canonically JSON encoded") from error


def _safe_file(root: Path, relative: str, *, required: bool = True) -> Path | None:
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        _fail(f"artifact path is not canonical relative data: {relative!r}")
    path = root / relative
    if not path.exists():
        if required:
            _incomplete(f"required artifact is absent: {relative}")
        return None
    if path.is_symlink() or not path.is_file():
        _fail(f"artifact must be a regular non-symlink file: {relative}")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        _fail(f"artifact escapes its slot directory: {relative}")
    return path


def _read_bytes(root: Path, relative: str, *, required: bool = True) -> bytes | None:
    path = _safe_file(root, relative, required=required)
    if path is None:
        return None
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            _fail(f"artifact exceeds the fixed file bound: {relative}")
        return path.read_bytes()
    except OSError as error:
        raise _Incomplete(f"cannot read artifact {relative}") from error


def _read_json(
    root: Path,
    relative: str,
    *,
    required: bool = True,
    canonical: bool = True,
) -> tuple[dict[str, Any], bytes] | None:
    payload = _read_bytes(root, relative, required=required)
    if payload is None:
        return None
    document = _parse_json_bytes(payload, relative)
    if canonical and payload != _canonical_json_bytes(document):
        _fail(f"{relative} is not canonical JSON with one terminal newline")
    return document, payload


def _identity_rows(
    payload: bytes,
    *,
    expected_count: int,
    expected_fields: frozenset[str],
    label: str,
) -> tuple[Mapping[str, str], ...]:
    """Parse preserved key-generator output without trusting launcher code."""

    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise _Reject(f"{label} is not ASCII") from error
    if not text.endswith("\n") or "\r" in text:
        _fail(f"{label} must use canonical LF-terminated lines")
    rows: list[dict[str, str]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line:
            _fail(f"{label} contains an empty line")
        fields: dict[str, str] = {}
        for token in line.split(" "):
            if not token or ":" not in token:
                _fail(f"{label} line {line_number} is malformed")
            key, value = token.split(":", 1)
            if (
                not key
                or not value
                or key in fields
                or any(character not in _HEX_DIGITS for character in value)
                or len(value) % 2 != 0
            ):
                _fail(f"{label} line {line_number} is malformed")
            fields[key] = value
        if set(fields) != expected_fields:
            _fail(f"{label} line {line_number} schema drifted")
        rows.append(fields)
    if len(rows) != expected_count:
        _fail(f"{label} identity count drifted")
    for field in expected_fields:
        if len({row[field] for row in rows}) != len(rows):
            _fail(f"{label} contains duplicate {field} values")
    return tuple(rows)


def _validate_materialized_configs(
    slot_root: Path,
    *,
    runtime: Mapping[str, Any],
    expected: _ExpectedSlot,
    manifest: FrozenFactorialManifest,
) -> None:
    """Reconstruct the complete native config from independently parsed inputs."""

    bls_bytes = _read_bytes(slot_root, "runtime/bls-identities.txt")
    tls_bytes = _read_bytes(slot_root, "runtime/tls-identities.txt")
    issuer_bytes = _read_bytes(slot_root, "runtime/issuer-identities.txt")
    assert bls_bytes is not None and tls_bytes is not None and issuer_bytes is not None
    bls = _identity_rows(
        bls_bytes,
        expected_count=expected.replica_count,
        expected_fields=frozenset({"pub", "sec"}),
        label="BLS identities",
    )
    tls = _identity_rows(
        tls_bytes,
        expected_count=expected.replica_count + 1,
        expected_fields=frozenset({"crt", "sec", "cid"}),
        label="TLS identities",
    )
    issuer = _identity_rows(
        issuer_bytes,
        expected_count=1,
        expected_fields=frozenset({"pub", "sec"}),
        label="issuer identity",
    )[0]

    main_config = _mapping(runtime.get("main_config"), "runtime main config")
    config_path = _string(main_config.get("path"), "runtime main config path")
    core_lines = tuple(
        _string(line, "runtime main config line")
        for line in _array(main_config.get("lines"), "runtime main config lines")
    )
    public_lines = (
        *core_lines,
        "nworker = 2",
        f"repnworker = {_REPLICA_NETWORK_WORKERS}",
        f"stat-period = {manifest.common_timers.hard_timeout_s + 60}",
        "pace-maker = dummy",
        "proposer = 0",
        "base-timeout = 2.0",
        "prop-delay = 0.1",
        "client-ip = 127.0.0.1",
        "tree-generation = default",
        f"epoch-change-issuer-id = {_ISSUER_ID}",
        f"epoch-change-issuer-public-key = {issuer['pub']}",
        f"epoch-change-maximum-block-extra-bytes = {_MAX_COMMAND_BYTES}",
        f"epoch-change-maximum-ancestry-blocks = {_MAX_ANCESTRY_BLOCKS}",
        f"epoch-manager-tls-cert = {tls[expected.replica_count]['crt']}",
        f"max-rep-msg = {_MAX_REPLICA_MESSAGE_BYTES}",
    )
    keys = tuple(line.split("=", 1)[0].strip() for line in public_lines)
    if len(set(keys)) != len(keys):
        _fail("materialized main configuration contains duplicate singleton keys")
    replica_lines = tuple(
        "replica = "
        f"127.0.0.1:{expected.peer_base + replica_id};"
        f"{expected.client_base + replica_id}, "
        f"{bls[replica_id]['pub']}, {tls[replica_id]['cid']}"
        for replica_id in range(expected.replica_count)
    )
    expected_config = ("\n".join((*public_lines, *replica_lines)) + "\n").encode(
        "ascii"
    )
    config_bytes = _read_bytes(slot_root, config_path)
    assert config_bytes is not None
    if config_bytes != expected_config:
        _fail("materialized main configuration differs from independent reconstruction")

    for replica_id in range(expected.replica_count):
        relative = f"runtime/replica-{replica_id}.conf"
        replica_bytes = _read_bytes(slot_root, relative)
        assert replica_bytes is not None
        expected_replica = (
            f"privkey = {bls[replica_id]['sec']}\n"
            f"tls-privkey = {tls[replica_id]['sec']}\n"
            f"tls-cert = {tls[replica_id]['crt']}\n"
            f"idx = {replica_id}\n"
        ).encode("ascii")
        if replica_bytes != expected_replica:
            _fail(f"materialized replica configuration differs: {replica_id}")


def _read_jsonl(
    root: Path,
    relative: str,
    *,
    run_id: str,
    source_kind: str,
    source_id: str,
    source_instance: str,
) -> tuple[_NativeEvent, ...]:
    path = _safe_file(root, relative)
    assert path is not None
    try:
        if path.stat().st_size > _MAX_EVENT_STREAM_BYTES:
            _fail(f"structured event stream exceeds its bound: {relative}")
        events: list[_NativeEvent] = []
        previous_monotonic_ns = 0
        with path.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                if len(raw_line) > _MAX_EVENT_LINE_BYTES:
                    _fail(f"{relative}:{line_number} exceeds the native line bound")
                if not raw_line.endswith(b"\n") or raw_line == b"\n":
                    _fail(f"{relative}:{line_number} is not one complete JSON line")
                event = _parse_json_bytes(raw_line[:-1], f"{relative}:{line_number}")
                _fields(
                    event,
                    {
                        "event_schema_version",
                        "run_id",
                        "source_kind",
                        "source_id",
                        "source_instance",
                        "source_sequence",
                        "source_monotonic_ns",
                        "event_type",
                        "payload",
                    },
                    f"{relative}:{line_number}",
                )
                sequence = _integer(
                    event["source_sequence"], f"{relative}:{line_number}.source_sequence", 1
                )
                monotonic_ns = _integer(
                    event["source_monotonic_ns"],
                    f"{relative}:{line_number}.source_monotonic_ns",
                    1,
                )
                if (
                    event["event_schema_version"] != 1
                    or event["run_id"] != run_id
                    or event["source_kind"] != source_kind
                    or event["source_id"] != source_id
                    or event["source_instance"] != source_instance
                ):
                    _fail(f"{relative}:{line_number} contains mixed-run/source evidence")
                if sequence != line_number:
                    _fail(f"{relative} source sequence is duplicated or non-contiguous")
                if monotonic_ns < previous_monotonic_ns:
                    _fail(f"{relative} monotonic timestamps regress")
                previous_monotonic_ns = monotonic_ns
                events.append(
                    _NativeEvent(
                        relative_path=relative,
                        line_number=line_number,
                        source_kind=source_kind,
                        source_id=source_id,
                        source_instance=source_instance,
                        source_sequence=sequence,
                        monotonic_ns=monotonic_ns,
                        event_type=_string(
                            event["event_type"], f"{relative}:{line_number}.event_type"
                        ),
                        payload=_mapping(
                            event["payload"], f"{relative}:{line_number}.payload"
                        ),
                        line_sha256=_sha256(raw_line),
                    )
                )
    except OSError as error:
        raise _Incomplete(f"cannot read structured event stream {relative}") from error
    if not events:
        _incomplete(f"structured event stream is empty: {relative}")
    return tuple(events)


def _fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        _fail(
            f"{label} has an invalid field set; missing={sorted(expected-actual)}, "
            f"extra={sorted(actual-expected)}"
        )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{label} must be an array")
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail(f"{label} must be an integer >= {minimum}")
    return value


def _bounded_float(value: object, label: str) -> float:
    if type(value) not in (int, float):
        _fail(f"{label} must be numeric")
    if type(value) is int and not -_UINT64_MAX <= value <= _UINT64_MAX:
        _fail(f"{label} exceeds the fixed numeric bound")
    if type(value) is float and (
        not math.isfinite(value) or abs(value) > _UINT64_MAX
    ):
        _fail(f"{label} exceeds the fixed numeric bound")
    try:
        result = float(value)
    except OverflowError as error:
        raise _Reject(f"{label} exceeds the fixed numeric bound") from error
    if not math.isfinite(result):
        _fail(f"{label} exceeds the fixed numeric bound")
    return result


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a non-empty string")
    return value


def _digest(value: object, label: str) -> str:
    text = _string(value, label)
    if _HEX64.fullmatch(text) is None:
        _fail(f"{label} must be 32-byte lowercase hexadecimal")
    return text


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _u(value: int, size: int) -> bytes:
    if type(value) is not int or value < 0 or value >= 1 << (size * 8):
        _fail(f"unsigned integer {value!r} does not fit {size} bytes")
    return value.to_bytes(size, "big")


def _cstr(value: str) -> bytes:
    payload = value.encode("utf-8")
    return _u(len(payload), 4) + payload


def _tree_depth(replica_count: int, fanout: int) -> int:
    if replica_count <= 0 or fanout <= 0 or fanout > 255:
        _fail("tree depth input is outside the frozen bounds")
    depth, covered, level = 0, 1, 1
    while covered < replica_count:
        level *= fanout
        covered += level
        depth += 1
    return depth


def derive_actor_ids(
    replica_count: int,
    actor_count: int,
    scientific_seed: int,
) -> tuple[int, ...]:
    """Rank the canonical non-reference-root pool ``Q..N-1``."""

    if (
        replica_count <= 0
        or actor_count <= 0
        or actor_count > replica_count
        or scientific_seed < 0
    ):
        _fail("actor derivation input is invalid")
    if (replica_count - 1) % 3:
        _fail("actor derivation requires exact N = 3f + 1")
    fault_threshold = (replica_count - 1) // 3
    quorum = 2 * fault_threshold + 1
    pool = tuple(range(quorum, replica_count))
    if actor_count > len(pool):
        _fail("actor count exceeds the canonical non-reference-root pool")
    membership = ",".join(str(member) for member in range(replica_count))

    def rank(member: int) -> tuple[bytes, int]:
        preimage = (
            f"{membership}\x00{quorum}\x00{scientific_seed}\x00{member}"
        ).encode("ascii")
        return hashlib.sha256(preimage).digest(), member

    selected = sorted(pool, key=rank)[:actor_count]
    return tuple(sorted(selected))


def fnv1a_rotating_actor(
    actor_ids: Sequence[int],
    *,
    epoch_number: int,
    tree_id: int,
    epoch_digest: str,
    block_hash: str,
) -> tuple[int, int]:
    """Independently execute the native big-endian FNV-1a selector."""

    actors = tuple(sorted(actor_ids))
    if not actors or len(set(actors)) != len(actors) or any(x < 0 for x in actors):
        _fail("rotating actors must be a non-empty unique non-negative set")
    if not (0 <= epoch_number <= 0xFFFF_FFFF and 0 <= tree_id <= 0xFFFF_FFFF):
        _fail("rotating proposal epoch/tree does not fit uint32")
    epoch_bytes = bytes.fromhex(_digest(epoch_digest, "proposal epoch digest"))
    block_bytes = bytes.fromhex(_digest(block_hash, "proposal block hash"))
    value = 14_695_981_039_346_656_037
    for byte in _u(epoch_number, 4) + _u(tree_id, 4) + epoch_bytes + block_bytes:
        value ^= byte
        value = (value * 1_099_511_628_211) & _UINT64_MAX
    return value, actors[value % len(actors)]


class _WireReader:
    __slots__ = ("_payload", "_offset", "_label")

    def __init__(self, payload: bytes, label: str) -> None:
        self._payload = payload
        self._offset = 0
        self._label = label

    @property
    def remaining(self) -> int:
        return len(self._payload) - self._offset

    def take(self, size: int) -> bytes:
        if size < 0 or self.remaining < size:
            _fail(f"{self._label} is truncated")
        result = self._payload[self._offset : self._offset + size]
        self._offset += size
        return result

    def integer(self, size: int) -> int:
        return int.from_bytes(self.take(size), "big")

    def digest(self) -> str:
        return self.take(32).hex()

    def string(self) -> str:
        size = self.integer(4)
        if size > _MAX_EVENT_LINE_BYTES:
            _fail(f"{self._label} contains an oversized string")
        try:
            value = self.take(size).decode("utf-8")
        except UnicodeDecodeError as error:
            raise _Reject(f"{self._label} contains invalid UTF-8") from error
        if not value:
            _fail(f"{self._label} contains an empty identity string")
        return value

    def finish(self) -> None:
        if self.remaining:
            _fail(f"{self._label} contains trailing bytes")


def _membership_digest(members: Sequence[int]) -> str:
    ordered = tuple(sorted(members))
    if not ordered or len(set(ordered)) != len(ordered):
        _fail("membership must be non-empty and unique")
    return _sha256(
        _MEMBERSHIP_DOMAIN
        + _u(len(ordered), 4)
        + b"".join(_u(member, 2) for member in ordered)
    )


def _epoch_canonical_bytes(
    *,
    epoch_number: int,
    previous_epoch_digest: str,
    membership_digest: str,
    generation_seed: int,
    policy_version: str,
    evidence_snapshot_id: str,
    evidence_cutoff: int,
    trees: Sequence[Tree],
) -> bytes:
    ordered = tuple(sorted(trees, key=lambda tree: tree.tree_id))
    if len({tree.tree_id for tree in ordered}) != len(ordered):
        _fail("epoch definition contains duplicate tree IDs")
    result = bytearray(_EPOCH_DEFINITION_DOMAIN)
    result += _u(2, 4)
    result += _u(epoch_number, 4)
    result += bytes.fromhex(_digest(previous_epoch_digest, "previous epoch digest"))
    result += bytes.fromhex(_digest(membership_digest, "membership digest"))
    result += _u(generation_seed, 8)
    result += _cstr(policy_version)
    result += _cstr(evidence_snapshot_id)
    result += _u(evidence_cutoff, 8)
    result += _u(len(ordered), 4)
    for tree in ordered:
        result += _u(tree.tree_id, 4)
        result += _u(tree.fanout, 4)
        result += _u(tree.pipeline_stretch, 4)
        result += _u(len(tree.members), 4)
        result += b"".join(_u(member, 2) for member in tree.members)
        wait_exempt = tuple(sorted(tree.wait_exempt))
        if len(set(wait_exempt)) != len(wait_exempt):
            _fail("epoch tree contains duplicate wait-exempt replicas")
        result += _u(len(wait_exempt), 4)
        result += b"".join(_u(member, 2) for member in wait_exempt)
    return bytes(result)


def _initial_epoch(expected: _ExpectedSlot) -> tuple[str, tuple[Tree, ...]]:
    members = tuple(range(expected.replica_count))
    trees = tuple(
        Tree(
            tree_id=tree_id,
            fanout=expected.initial_fanout,
            pipeline_stretch=expected.pipeline_stretch,
            members=tuple(
                (position + tree_id) % expected.replica_count
                for position in range(expected.replica_count)
            ),
            wait_exempt=(),
        )
        for tree_id in range(expected.replica_count)
    )
    digest = _sha256(
        _epoch_canonical_bytes(
            epoch_number=0,
            previous_epoch_digest="0" * 64,
            membership_digest=_membership_digest(members),
            generation_seed=0,
            policy_version="adaptive-v2-bootstrap",
            evidence_snapshot_id="adaptive-v2-bootstrap-epoch-zero",
            evidence_cutoff=0,
            trees=trees,
        )
    )
    return digest, trees


_SecpPoint = tuple[int, int] | None


def _secp256k1_add(left: _SecpPoint, right: _SecpPoint) -> _SecpPoint:
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % _SECP256K1_FIELD == 0:
        return None
    if left == right:
        if y1 == 0:
            return None
        slope = (
            (3 * x1 * x1)
            * pow(2 * y1, -1, _SECP256K1_FIELD)
        ) % _SECP256K1_FIELD
    else:
        slope = ((y2 - y1) * pow(x2 - x1, -1, _SECP256K1_FIELD)) % (
            _SECP256K1_FIELD
        )
    x3 = (slope * slope - x1 - x2) % _SECP256K1_FIELD
    y3 = (slope * (x1 - x3) - y1) % _SECP256K1_FIELD
    return x3, y3


def _secp256k1_multiply(scalar: int, point: _SecpPoint) -> _SecpPoint:
    if type(scalar) is not int or not 0 <= scalar < _SECP256K1_ORDER:
        _fail("secp256k1 scalar is outside the fixed order")
    result: _SecpPoint = None
    addend = point
    value = scalar
    while value:
        if value & 1:
            result = _secp256k1_add(result, addend)
        addend = _secp256k1_add(addend, addend)
        value >>= 1
    return result


def _decode_secp256k1_public_key(public_key_hex: str) -> tuple[int, int]:
    if (
        not isinstance(public_key_hex, str)
        or len(public_key_hex) != 66
        or any(character not in _HEX_DIGITS for character in public_key_hex)
    ):
        _fail("epoch issuer public key is not compressed secp256k1")
    encoded = bytes.fromhex(public_key_hex)
    if encoded[0] not in (2, 3):
        _fail("epoch issuer public key has an invalid compression prefix")
    x = int.from_bytes(encoded[1:], "big")
    if x >= _SECP256K1_FIELD:
        _fail("epoch issuer public key x-coordinate is outside the field")
    rhs = (pow(x, 3, _SECP256K1_FIELD) + 7) % _SECP256K1_FIELD
    y = pow(rhs, (_SECP256K1_FIELD + 1) // 4, _SECP256K1_FIELD)
    if pow(y, 2, _SECP256K1_FIELD) != rhs:
        _fail("epoch issuer public key is not on secp256k1")
    if y & 1 != encoded[0] & 1:
        y = _SECP256K1_FIELD - y
    return x, y


def _verify_secp256k1_signature(
    signing_bytes: bytes,
    signature: bytes,
    issuer_public_key: str,
) -> None:
    """Verify native compact ECDSA without trusting native acceptance."""

    if not isinstance(signing_bytes, bytes) or not isinstance(signature, bytes):
        _fail("epoch command signature inputs are malformed")
    if len(signature) != 64:
        _fail("epoch command signature is not compact r||s")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (1 <= r < _SECP256K1_ORDER and 1 <= s <= _SECP256K1_ORDER // 2):
        _fail("epoch command signature scalars are invalid or non-low-S")
    public_key = _decode_secp256k1_public_key(issuer_public_key)
    z = int.from_bytes(hashlib.sha256(signing_bytes).digest(), "big")
    inverse = pow(s, -1, _SECP256K1_ORDER)
    point = _secp256k1_add(
        _secp256k1_multiply(
            (z * inverse) % _SECP256K1_ORDER,
            (_SECP256K1_GX, _SECP256K1_GY),
        ),
        _secp256k1_multiply(
            (r * inverse) % _SECP256K1_ORDER,
            public_key,
        ),
    )
    if point is None or point[0] % _SECP256K1_ORDER != r:
        _fail("epoch command signature does not verify under the archived issuer key")


def decode_epoch_change_bundle(
    payload: bytes,
    *,
    issuer_public_key: str,
) -> DecodedBundle:
    """Decode and re-canonicalize a native adaptive-v2 bundle."""

    if not payload or len(payload) > _MAX_JSON_BYTES:
        _fail("epoch-change bundle is empty or exceeds the validation bound")
    outer = _WireReader(payload, "epoch-change bundle")
    if outer.take(len(_BUNDLE_DOMAIN)) != _BUNDLE_DOMAIN:
        _fail("epoch-change bundle domain is invalid")
    if outer.integer(4) != 1 or outer.integer(1) != 2:
        _fail("epoch-change bundle schema/mode is invalid")
    command_bytes = outer.take(outer.integer(4))
    definition_bytes = outer.take(outer.integer(4))
    outer.finish()

    command_reader = _WireReader(command_bytes, "authorized epoch-change command")
    if command_reader.take(len(_AUTHORIZED_COMMAND_DOMAIN)) != _AUTHORIZED_COMMAND_DOMAIN:
        _fail("authorized epoch-change command domain is invalid")
    if command_reader.integer(4) != 1 or command_reader.integer(1) != 2:
        _fail("authorized epoch-change command schema/mode is invalid")
    issuer_id = command_reader.integer(4)
    successor_epoch_number = command_reader.integer(4)
    predecessor_epoch_digest = command_reader.digest()
    successor_epoch_digest = command_reader.digest()
    activation_delay_blocks = command_reader.integer(8)
    signature = command_reader.take(64)
    command_reader.finish()
    if signature[:32] == bytes(32) or signature[32:] == bytes(32):
        _fail("authorized epoch-change signature has a zero scalar")
    if signature[32:] > _SECP256K1_HALF_ORDER:
        _fail("authorized epoch-change signature is not canonical low-S")
    _verify_secp256k1_signature(
        command_bytes[:-64],
        signature,
        issuer_public_key,
    )
    payload_digest = _sha256(
        _EPOCH_CHANGE_PAYLOAD_DOMAIN
        + _u(successor_epoch_number, 4)
        + bytes.fromhex(predecessor_epoch_digest)
        + bytes.fromhex(successor_epoch_digest)
        + _u(activation_delay_blocks, 8)
    )

    definition_reader = _WireReader(definition_bytes, "epoch definition reply")
    if (
        definition_reader.integer(4) != 2
        or definition_reader.integer(1) != 2
        or definition_reader.integer(1) != 6
    ):
        _fail("epoch definition reply schema/mode/kind is invalid")
    reply_digest = definition_reader.digest()
    if definition_reader.integer(4) != 2:
        _fail("epoch definition does not use schema v2")
    epoch_number = definition_reader.integer(4)
    previous_epoch_digest = definition_reader.digest()
    membership_digest = definition_reader.digest()
    generation_seed = definition_reader.integer(8)
    policy_version = definition_reader.string()
    evidence_snapshot_id = definition_reader.string()
    evidence_cutoff = definition_reader.integer(8)
    tree_count = definition_reader.integer(4)
    if not 1 <= tree_count <= 255:
        _fail("epoch definition tree count is outside fixed bounds")
    trees: list[Tree] = []
    previous_tree_id: int | None = None
    for index in range(tree_count):
        tree_id = definition_reader.integer(4)
        fanout = definition_reader.integer(4)
        pipeline = definition_reader.integer(4)
        member_count = definition_reader.integer(4)
        if not 1 <= member_count <= 65_536:
            _fail(f"epoch tree {index} member count is outside fixed bounds")
        members = tuple(definition_reader.integer(2) for _ in range(member_count))
        wait_count = definition_reader.integer(4)
        if wait_count > member_count:
            _fail(f"epoch tree {index} wait-exempt count exceeds membership")
        wait_exempt = tuple(definition_reader.integer(2) for _ in range(wait_count))
        if (
            previous_tree_id is not None
            and tree_id <= previous_tree_id
            or fanout == 0
            or fanout > 255
            or pipeline == 0
            or len(set(members)) != len(members)
            or tuple(sorted(wait_exempt)) != wait_exempt
            or len(set(wait_exempt)) != len(wait_exempt)
            or not set(wait_exempt).issubset(members)
        ):
            _fail(f"epoch tree {index} is noncanonical")
        previous_tree_id = tree_id
        trees.append(Tree(tree_id, fanout, pipeline, members, wait_exempt))
    definition_reader.finish()

    computed_digest = _sha256(
        _epoch_canonical_bytes(
            epoch_number=epoch_number,
            previous_epoch_digest=previous_epoch_digest,
            membership_digest=membership_digest,
            generation_seed=generation_seed,
            policy_version=policy_version,
            evidence_snapshot_id=evidence_snapshot_id,
            evidence_cutoff=evidence_cutoff,
            trees=trees,
        )
    )
    if not (
        epoch_number == successor_epoch_number
        and previous_epoch_digest == predecessor_epoch_digest
        and reply_digest == successor_epoch_digest == computed_digest
    ):
        _fail("epoch-change bundle identities do not recompute exactly")
    return DecodedBundle(
        command=DecodedCommand(
            issuer_id=issuer_id,
            successor_epoch_number=successor_epoch_number,
            predecessor_epoch_digest=predecessor_epoch_digest,
            successor_epoch_digest=successor_epoch_digest,
            activation_delay_blocks=activation_delay_blocks,
            payload_digest=payload_digest,
            signature=signature,
        ),
        epoch_number=epoch_number,
        epoch_digest=computed_digest,
        previous_epoch_digest=previous_epoch_digest,
        membership_digest=membership_digest,
        generation_seed=generation_seed,
        policy_version=policy_version,
        evidence_snapshot_id=evidence_snapshot_id,
        evidence_cutoff=evidence_cutoff,
        trees=tuple(trees),
    )


def _block_ids(manifest: FrozenFactorialManifest) -> tuple[str, ...]:
    result: list[str] = []
    for replica_count in manifest.replica_counts:
        for fanout in manifest.initial_fanouts:
            result.extend(
                f"n{replica_count}-f{fanout}-b{index:02d}"
                for index in range(1, manifest.blocks_for(replica_count, fanout) + 1)
            )
    return tuple(result)


def _execution_schedule(
    block_ids: Sequence[str], arm_codes: Sequence[str], seed: int
) -> tuple[tuple[str, int, tuple[str, ...]], ...]:
    blocks, arms = tuple(block_ids), tuple(arm_codes)
    if not blocks or len(set(blocks)) != len(blocks):
        _fail("schedule block IDs are not unique")
    if tuple(arms) != tuple(EXPECTED_ARM_CODES):
        _fail("schedule arm codes differ from the frozen order")

    def block_rank(block_id: str) -> tuple[bytes, str]:
        return hashlib.sha256(f"{seed}\x00{block_id}".encode("ascii")).digest(), block_id

    counts = {(arm, position): 0 for arm in arms for position in range(len(arms))}
    result: list[tuple[str, int, tuple[str, ...]]] = []
    for ordinal, block_id in enumerate(sorted(blocks, key=block_rank), start=1):
        choices: list[tuple[tuple[int, int, bytes], tuple[str, ...]]] = []
        for order in itertools.permutations(arms):
            prospective = dict(counts)
            for position, arm in enumerate(order):
                prospective[arm, position] += 1
            values = tuple(prospective.values())
            tie = hashlib.sha256(
                f"{seed}\x00{block_id}\x00{','.join(order)}".encode("ascii")
            ).digest()
            choices.append(((max(values) - min(values), sum(x * x for x in values), tie), order))
        _, order = min(choices)
        for position, arm in enumerate(order):
            counts[arm, position] += 1
        result.append((block_id, ordinal, order))
    return tuple(result)


def _expected_slots(manifest: FrozenFactorialManifest) -> tuple[_ExpectedSlot, ...]:
    block_ids = _block_ids(manifest)
    if len(block_ids) != EXPECTED_BLOCK_COUNT:
        _fail(
            f"manifest does not derive exactly {EXPECTED_BLOCK_COUNT} blocks"
        )
    arms = tuple(arm.code for arm in manifest.arms)
    schedule = _execution_schedule(block_ids, arms, manifest.campaign_order_seed)
    scheduled = {block: (ordinal, order) for block, ordinal, order in schedule}
    result: list[_ExpectedSlot] = []
    block_ordinal = 0
    for replica_count in manifest.replica_counts:
        f = (replica_count - 1) // 3
        q = 2 * f + 1
        for fanout in manifest.initial_fanouts:
            blocks = manifest.blocks_for(replica_count, fanout)
            for block_index in range(1, blocks + 1):
                block_id = f"n{replica_count}-f{fanout}-b{block_index:02d}"
                block_execution_ordinal, order = scheduled[block_id]
                scientific_seed = manifest.scientific_seed_base + block_ordinal
                actor_ids = derive_actor_ids(
                    replica_count,
                    manifest.byzantine.actor_count,
                    scientific_seed,
                )
                candidate_depths = tuple(
                    (candidate, _tree_depth(replica_count, candidate))
                    for candidate in manifest.candidate_fanouts
                )
                for arm in manifest.arms:
                    nonce = len(result)
                    ordinal = nonce + 1
                    position = order.index(arm.code) + 1
                    result.append(
                        _ExpectedSlot(
                            slot_id=f"slot-{ordinal:03d}-{block_id}-{arm.code}",
                            block_id=block_id,
                            arm_code=arm.code,
                            ordinal=ordinal,
                            slot_nonce=nonce,
                            block_index=block_index,
                            blocks_in_cell=blocks,
                            block_execution_ordinal=block_execution_ordinal,
                            arm_execution_position=position,
                            execution_ordinal=(block_execution_ordinal - 1) * len(arms) + position,
                            scientific_seed=scientific_seed,
                            replica_count=replica_count,
                            f=f,
                            q=q,
                            tree_count=q,
                            initial_fanout=fanout,
                            initial_depth=_tree_depth(replica_count, fanout),
                            candidate_depths=candidate_depths,
                            worst_candidate_depth=max(depth for _, depth in candidate_depths),
                            candidate_fanouts=tuple(manifest.candidate_fanouts),
                            pipeline_stretch=manifest.pipeline_stretch,
                            placement_adaptation=arm.placement_adaptation,
                            shape_adaptation=arm.shape_adaptation,
                            actor_ids=actor_ids,
                            peer_base=manifest.resources.peer_port_base
                            + nonce * manifest.resources.slot_port_stride,
                            client_base=manifest.resources.client_port_base
                            + nonce * manifest.resources.slot_port_stride,
                            manager_port=manifest.resources.manager_port_base
                            + nonce * manifest.resources.slot_port_stride,
                        )
                    )
                block_ordinal += 1
    if len(result) != EXPECTED_SLOT_COUNT:
        _fail(f"manifest does not derive exactly {EXPECTED_SLOT_COUNT} slots")
    return tuple(result)


def _expected_excluded_smoke(
    manifest: FrozenFactorialManifest,
) -> _ExpectedSlot:
    replica_count = 7
    f = 2
    q = 5
    fanout = 2
    scientific_seed = 41_700
    candidate_depths = tuple(
        (candidate, _tree_depth(replica_count, candidate))
        for candidate in manifest.candidate_fanouts
    )
    return _ExpectedSlot(
        slot_id=EXCLUDED_SMOKE_SLOT_ID,
        block_id="n7-f2-smoke-b01",
        arm_code="PS",
        ordinal=1,
        slot_nonce=0,
        block_index=1,
        blocks_in_cell=1,
        block_execution_ordinal=1,
        arm_execution_position=1,
        execution_ordinal=1,
        scientific_seed=scientific_seed,
        replica_count=replica_count,
        f=f,
        q=q,
        tree_count=q,
        initial_fanout=fanout,
        initial_depth=_tree_depth(replica_count, fanout),
        candidate_depths=candidate_depths,
        worst_candidate_depth=max(depth for _, depth in candidate_depths),
        candidate_fanouts=tuple(manifest.candidate_fanouts),
        pipeline_stretch=manifest.pipeline_stretch,
        placement_adaptation=True,
        shape_adaptation=True,
        actor_ids=derive_actor_ids(replica_count, min(3, f), scientific_seed),
        peer_base=45_100,
        client_base=46_100,
        manager_port=47_100,
    )


def validate_schedule_document(plan: Mapping[str, Any], manifest: FrozenFactorialManifest) -> None:
    """Recompute and validate the frozen block/arm schedule."""

    expected = _execution_schedule(
        _block_ids(manifest), tuple(arm.code for arm in manifest.arms), manifest.campaign_order_seed
    )
    raw = _array(plan.get("execution_schedule"), "plan.execution_schedule")
    observed: list[tuple[str, int, tuple[str, ...]]] = []
    for index, item in enumerate(raw):
        entry = _mapping(item, f"plan.execution_schedule[{index}]")
        _fields(entry, {"block_id", "block_execution_ordinal", "arm_order"}, f"schedule[{index}]")
        observed.append(
            (
                _string(entry["block_id"], "schedule block_id"),
                _integer(entry["block_execution_ordinal"], "schedule ordinal", 1),
                tuple(_string(x, "schedule arm") for x in _array(entry["arm_order"], "schedule arm_order")),
            )
        )
    if tuple(observed) != expected:
        _fail("plan execution schedule differs from independent derivation")


def _load_static_contracts(
    slot_root: Path,
) -> tuple[FrozenFactorialManifest, dict[str, Any], dict[str, Any], _ExpectedSlot, str]:
    manifest_payload = _read_bytes(slot_root, MANIFEST_FILENAME)
    assert manifest_payload is not None
    manifest_document = _parse_json_bytes(manifest_payload, MANIFEST_FILENAME)
    identity = _frozen_artifact_identity(
        _string(manifest_document.get("manifest_id"), "manifest.manifest_id")
    )
    if _sha256(manifest_payload) != identity.manifest_sha256:
        _fail("manifest.json does not have the exact frozen byte identity")
    try:
        manifest = load_frozen_manifest_bytes(manifest_payload)
    except Exception as error:
        raise _Reject(f"manifest.json is not the frozen manifest: {error}") from error
    for vector in manifest.byzantine.actor_selection_vectors:
        if (
            vector.q != 2 * ((vector.replica_count - 1) // 3) + 1
            or derive_actor_ids(
                vector.replica_count,
                manifest.byzantine.actor_count,
                vector.scientific_seed,
            )
            != vector.selected_actor_ids
        ):
            _fail("frozen actor-selection vector failed independent recomputation")
    for vector in manifest.byzantine.actor_rotation_vectors:
        value, actor = fnv1a_rotating_actor(
            vector.sorted_actor_ids,
            epoch_number=vector.epoch_number,
            tree_id=vector.tree_id,
            epoch_digest=vector.epoch_digest,
            block_hash=vector.block_hash,
        )
        if value != vector.fnv1a64 or actor != vector.selected_actor:
            _fail("frozen FNV actor-rotation vector failed independent recomputation")

    loaded_plan = _read_json(slot_root, PLAN_FILENAME)
    assert loaded_plan is not None
    plan, plan_payload = loaded_plan
    if _sha256(plan_payload) != identity.plan_sha256:
        _fail("plan.json does not have the exact frozen plan identity")
    if (
        plan.get("manifest_id") != identity.manifest_id
        or plan.get("manifest_sha256") != identity.manifest_sha256
    ):
        _fail("plan.json is not bound to the frozen manifest")
    if (
        plan.get("plan_id") != f"{identity.manifest_id}-plan-v1"
        or plan.get("schema_version") != 1
        or plan.get("slot_count") != EXPECTED_SLOT_COUNT
        or plan.get("automatic_retries") != 0
        or plan.get("replacement_policy") != "none"
        or plan.get("outcome_dependent_order") is not False
        or plan.get("max_parallel_slots") != 1
    ):
        _fail("plan.json violates fixed no-retry sequential execution")
    validate_schedule_document(plan, manifest)

    expected_slots = _expected_slots(manifest)
    expected_by_id = {slot.slot_id: slot for slot in expected_slots}
    if slot_root.name == EXCLUDED_SMOKE_SLOT_ID:
        expected = _expected_excluded_smoke(manifest)
        loaded_runtime = _read_json(slot_root, RUNTIME_FILENAME)
        assert loaded_runtime is not None
        runtime, runtime_payload = loaded_runtime
        if _sha256(runtime_payload) != identity.smoke_runtime_sha256:
            _fail("runtime.json drifted from the exact frozen N=7 smoke identity")
        _validate_runtime_slot(runtime, expected, manifest)
        return manifest, plan, runtime, expected, _sha256(runtime_payload)
    if slot_root.name not in expected_by_id:
        _fail("slot directory name is not a frozen slot ID")
    expected = expected_by_id[slot_root.name]
    plan_slots = _array(plan.get("slots"), "plan.slots")
    if len(plan_slots) != EXPECTED_SLOT_COUNT:
        _fail(f"plan does not contain exactly {EXPECTED_SLOT_COUNT} slots")
    plan_slot_by_id: dict[str, Mapping[str, Any]] = {}
    for item in plan_slots:
        value = _mapping(item, "plan slot")
        slot_id = _string(value.get("slot_id"), "plan slot_id")
        if slot_id in plan_slot_by_id:
            _fail(f"plan contains duplicate slot {slot_id}")
        plan_slot_by_id[slot_id] = value
    if set(plan_slot_by_id) != set(expected_by_id):
        _fail("plan slot IDs differ from independent derivation")
    for slot_id, derived in expected_by_id.items():
        item = plan_slot_by_id[slot_id]
        consensus = _mapping(item.get("consensus"), f"{slot_id}.consensus")
        arm = _mapping(item.get("arm"), f"{slot_id}.arm")
        ports = _mapping(item.get("ports"), f"{slot_id}.ports")
        exact = {
            "ordinal": derived.ordinal,
            "slot_nonce": derived.slot_nonce,
            "block_id": derived.block_id,
            "block_index": derived.block_index,
            "blocks_in_cell": derived.blocks_in_cell,
            "block_execution_ordinal": derived.block_execution_ordinal,
            "arm_execution_position": derived.arm_execution_position,
            "execution_ordinal": derived.execution_ordinal,
            "scientific_seed": derived.scientific_seed,
            "candidate_fanouts": list(derived.candidate_fanouts),
            "pipeline_stretch": derived.pipeline_stretch,
            "byzantine_actor_ids": list(derived.actor_ids),
        }
        if any(item.get(key) != value for key, value in exact.items()):
            _fail(f"plan slot {slot_id} differs from independently derived identity")
        expected_consensus = {
            "replica_count": derived.replica_count,
            "f": derived.f,
            "q": derived.q,
            "tree_count": derived.tree_count,
            "initial_fanout": derived.initial_fanout,
            "initial_depth": derived.initial_depth,
            "candidate_depths": [list(value) for value in derived.candidate_depths],
            "worst_candidate_depth": derived.worst_candidate_depth,
        }
        if dict(consensus) != expected_consensus:
            _fail(f"plan slot {slot_id} consensus shape drifted")
        if dict(arm) != {
            "code": derived.arm_code,
            "placement_adaptation": derived.placement_adaptation,
            "shape_adaptation": derived.shape_adaptation,
        }:
            _fail(f"plan slot {slot_id} arm identity drifted")
        if dict(ports) != {
            "peer_base": derived.peer_base,
            "client_base": derived.client_base,
            "manager": derived.manager_port,
        }:
            _fail(f"plan slot {slot_id} port allocation drifted")

    loaded_runtime = _read_json(slot_root, RUNTIME_FILENAME)
    assert loaded_runtime is not None
    runtime, runtime_payload = loaded_runtime
    runtime_sha256 = _sha256(runtime_payload)
    if runtime_sha256 != identity.runtime_sha256:
        _fail("runtime.json drifted from the exact frozen campaign identity")
    if (
        runtime.get("schema_version") != 1
        or runtime.get("runtime_id") != f"{identity.manifest_id}-runtime-v1"
        or runtime.get("manifest_id") != identity.manifest_id
        or runtime.get("manifest_sha256") != identity.manifest_sha256
        or runtime.get("source_plan_sha256") != identity.plan_sha256
        or runtime.get("slot_count") != EXPECTED_SLOT_COUNT
        or runtime.get("execution_mode") != "fixed_sequential"
        or runtime.get("automatic_retries") != 0
        or runtime.get("replacement_policy") != "none"
        or runtime.get("outcome_dependent_order") is not False
    ):
        _fail("runtime.json identity or execution policy drifted")
    runtime_slots = _array(runtime.get("slots"), "runtime.slots")
    if len(runtime_slots) != EXPECTED_SLOT_COUNT:
        _fail(f"runtime.json does not contain {EXPECTED_SLOT_COUNT} slots")
    by_id: dict[str, Mapping[str, Any]] = {}
    execution_ordinals: set[int] = set()
    for item in runtime_slots:
        value = _mapping(item, "runtime slot")
        slot_id = _string(value.get("slot_id"), "runtime slot_id")
        if slot_id in by_id:
            _fail(f"runtime contains duplicate slot {slot_id}")
        by_id[slot_id] = value
        execution_ordinals.add(_integer(value.get("execution_ordinal"), "runtime execution ordinal", 1))
    if set(by_id) != set(expected_by_id) or execution_ordinals != set(range(1, EXPECTED_SLOT_COUNT + 1)):
        _fail("runtime slot identities/order differ from the frozen plan")
    runtime_slot = dict(by_id[expected.slot_id])
    _validate_runtime_slot(runtime_slot, expected, manifest)
    return manifest, plan, runtime_slot, expected, runtime_sha256


def _validate_runtime_slot(
    runtime: Mapping[str, Any], expected: _ExpectedSlot, manifest: FrozenFactorialManifest
) -> None:
    artifact_identity = {
        "arm_code": expected.arm_code,
        "block_id": expected.block_id,
        "scientific_seed": expected.scientific_seed,
        "slot_id": expected.slot_id,
        "slot_nonce": expected.slot_nonce,
    }
    checks = {
        "schema_version": 1,
        "artifact_id": "slot-runtime-"
        + _sha256(_canonical_json_bytes(artifact_identity))[:24],
        "slot_id": expected.slot_id,
        "block_id": expected.block_id,
        "arm_code": expected.arm_code,
        "ordinal": expected.ordinal,
        "block_execution_ordinal": expected.block_execution_ordinal,
        "arm_execution_position": expected.arm_execution_position,
        "execution_ordinal": expected.execution_ordinal,
        "scientific_seed": expected.scientific_seed,
        "replica_count": expected.replica_count,
        "f": expected.f,
        "q": expected.q,
        "tree_count": expected.tree_count,
        "initial_fanout": expected.initial_fanout,
        "candidate_fanouts": list(expected.candidate_fanouts),
        "pipeline_stretch": expected.pipeline_stretch,
        "actor_ids": list(expected.actor_ids),
    }
    if any(runtime.get(key) != value for key, value in checks.items()):
        _fail("runtime slot identity/consensus fields drifted")
    timers = manifest.common_timers
    config = _mapping(runtime.get("main_config"), "runtime main_config")
    expected_lines = {
        f"block-size = {manifest.workload.block_size}",
        f"fan-out = {expected.initial_fanout}",
        f"piped_latency = {manifest.workload.piped_latency_ms}",
        f"async_blocks = {expected.pipeline_stretch}",
        f"tree-switch-period = {manifest.workload.tree_switch_period_blocks}",
        f"aggregation-timeout = {timers.aggregation_timeout_ms / 1000:g}",
        f"leader-progress-timeout = {timers.leader_progress_timeout_ms / 1000:g}",
        f"leader-activation-grace = {timers.leader_activation_grace_ms / 1000:g}",
        f"epoch-change-minimum-activation-delay = {timers.activation_delay_blocks}",
        f"epoch-change-maximum-activation-delay = {timers.activation_delay_blocks}",
        f"epoch-manager-address = 127.0.0.1:{expected.manager_port}",
        "epoch-protocol-mode = adaptive_v2",
    }
    config_lines = _array(config.get("lines"), "main_config.lines")
    if len(config_lines) != len(expected_lines) or set(config_lines) != expected_lines:
        _fail("runtime main configuration/timers drifted")
    if config.get("path") != "runtime/main.conf":
        _fail("runtime main configuration path drifted")
    responsiveness = _mapping(runtime.get("responsiveness_policy"), "runtime responsiveness_policy")
    if dict(responsiveness) != {
        "policy_version": manifest.responsiveness_policy.policy_version,
        "attempt_window": manifest.responsiveness_policy.attempt_window,
        "minimum_attempts": manifest.responsiveness_policy.minimum_attempts,
        "minimum_response_rate_ppm": manifest.responsiveness_policy.minimum_response_rate_ppm,
        "maximum_timeout_rate_ppm": manifest.responsiveness_policy.maximum_timeout_rate_ppm,
        "trailing_timeout_streak": manifest.responsiveness_policy.trailing_timeout_streak,
        "latency_percentile_basis_points": manifest.responsiveness_policy.latency_percentile_basis_points,
    }:
        _fail("runtime responsiveness policy drifted")
    fault_window = _mapping(runtime.get("fault_window"), "runtime fault_window")
    expected_fault_window = {
        "clock": "CLOCK_MONOTONIC_RAW",
        "bound_rule": "prelaunch_anchor_plus_offset_inclusive_start_exclusive_end",
        "shared_anchor_per_slot": True,
        "anchor_phase": "sample_once_immediately_before_slot_launch",
        "start_after_prelaunch_anchor_s": (
            manifest.byzantine.start_after_prelaunch_anchor_s
        ),
        "duration_s": manifest.byzantine.duration_s,
        "transition_convergence_deadline_s": (
            manifest.common_timers.transition_convergence_deadline_s
        ),
        "schedule_slack_s": manifest.common_timers.schedule_slack_s,
        "drain_margin_s": manifest.common_timers.drain_margin_s,
        "hard_timeout_s": manifest.common_timers.hard_timeout_s,
    }
    if manifest.manifest_id in {
        V2_MANIFEST_ID,
        V3_MANIFEST_ID,
        V4_MANIFEST_ID,
        FROZEN_MANIFEST_ID,
    }:
        expected_fault_window["transition_observation_bound_rule"] = (
            "shared_slot_hard_deadline_until_manager_selection_v1"
        )
    if dict(fault_window) != expected_fault_window:
        _fail("runtime fault-window clock/bound contract drifted")
    shape = _mapping(runtime.get("shape_invocation"), "shape_invocation")
    if (
        shape.get("selector_version") != SELECTOR_VERSION
        or shape.get("tie_rule") != SHAPE_TIE_RULE
        or shape.get("reference_tree_rule") != REFERENCE_TREE_RULE
        or shape.get("candidate_fanouts") != list(expected.candidate_fanouts)
        or shape.get("fixed_pipeline_stretch") != expected.pipeline_stretch
        or shape.get("deterministic_seed") != expected.scientific_seed
        or shape.get("compute_live") is not True
        or shape.get("apply_selected_by_transition")
        != [False, expected.shape_adaptation]
    ):
        _fail("runtime shape invocation drifted")
    transitions = _array(runtime.get("transitions"), "runtime.transitions")
    if len(transitions) != 2:
        _fail("runtime must contain exactly two transition requests")
    expected_residencies_ms = (
        0,
        manifest.workload.epoch1_stable_bucket_count
        * manifest.workload.bucket_width_s
        * 1_000,
    )
    expected_post_baseline_observation_ms = (
        (
            manifest.byzantine.start_after_prelaunch_anchor_s
            + manifest.workload.fault_evidence_bucket_count
            * manifest.workload.bucket_width_s
        )
        * 1_000,
        0,
    )
    for index, raw in enumerate(transitions):
        transition = _mapping(raw, f"runtime.transitions[{index}]")
        request = _mapping(transition.get("request"), f"transition[{index}].request")
        expected_intent = "performance_optimization" if index == 1 and expected.placement_adaptation else "fault_containment"
        if (
            transition.get("predecessor_epoch") != index
            or transition.get("successor_epoch") != index + 1
            or request.get("predecessor_epoch_number") != index
            or request.get("successor_epoch_number") != index + 1
            or request.get("policy_intent") != expected_intent
            or request.get("evidence_window_rule") != EVIDENCE_WINDOW_RULE
            or request.get("minimum_predecessor_residency_ms")
            != expected_residencies_ms[index]
            or request.get("minimum_post_baseline_observation_ms")
            != expected_post_baseline_observation_ms[index]
            or request.get("apply_shape_selection") != (index == 1 and expected.shape_adaptation)
        ):
            _fail("runtime transition sequence/application contract drifted")
        if expected_intent == "fault_containment":
            if (
                request.get("containment_baseline_root_source")
                != "live_predecessor_roots"
                or request.get("containment_baseline_roots") != []
            ):
                _fail("containment request is not late-bound to live predecessor roots")
        elif (
            request.get("containment_baseline_root_source") != "not_applicable"
            or request.get("containment_baseline_roots") != []
        ):
            _fail("optimization request contains a containment-root binding")
    sequence = _mapping(runtime.get("transition_sequence"), "runtime transition_sequence")
    if (
        sequence.get("required_events") != list(CUTOFF_NAMES)
        or sequence.get("transition_count") != 2
        or sequence.get("activation_delay_blocks")
        != manifest.common_timers.activation_delay_blocks
        or sequence.get("total_activation_overhead_blocks")
        != 2 * manifest.common_timers.activation_delay_blocks
    ):
        _fail("runtime transition sequence drifted")
    cutoff = _mapping(runtime.get("cutoff_contract"), "runtime cutoff_contract")
    if dict(cutoff) != {
        "bucket_width_s": manifest.workload.bucket_width_s,
        "baseline_bucket_count": manifest.workload.baseline_bucket_count,
        "fault_evidence_bucket_count": manifest.workload.fault_evidence_bucket_count,
        "epoch1_stable_bucket_count": manifest.workload.epoch1_stable_bucket_count,
        "epoch2_stable_bucket_count": manifest.workload.epoch2_stable_bucket_count,
        "evidence_window_rule": EVIDENCE_WINDOW_RULE,
        "same_cutoff_rule_required": True,
        "actual_cutoff_validation_rule": CUTOFF_RULE,
        "actual_cutoffs_recorded_live": True,
    }:
        _fail("runtime cutoff contract drifted")
    events = _mapping(runtime.get("structured_events"), "runtime structured_events")
    expected_replica_ids = [f"replica-{replica_id}" for replica_id in range(expected.replica_count)]
    expected_instances = [f"{expected.slot_id}-{source_id}" for source_id in expected_replica_ids]
    if dict(events) != {
        "run_id": expected.slot_id,
        "manager_source_id": "adaptive-manager",
        "manager_source_instance": f"{expected.slot_id}-adaptive-manager",
        "manager_output_relative_path": MANAGER_EVENTS_FILENAME,
        "replica_source_ids": expected_replica_ids,
        "replica_source_instances": expected_instances,
        "replica_output_relative_paths": [
            REPLICA_EVENTS_PATTERN.format(replica_id=replica_id)
            for replica_id in range(expected.replica_count)
        ],
        "commit_observer_id": "replica-0",
        "commit_observer_instance": f"{expected.slot_id}-replica-0",
        "exclusive_output_per_process": True,
    }:
        _fail("runtime structured-event contract drifted")
    logs = _mapping(runtime.get("process_logs"), "runtime process_logs")
    stdout = [
        f"raw/process/replica-{replica_id}.stdout.log"
        for replica_id in range(expected.replica_count)
    ]
    stderr = [
        f"raw/process/replica-{replica_id}.stderr.log"
        for replica_id in range(expected.replica_count)
    ]
    if dict(logs) != {
        "manager_stdout_relative_path": "raw/process/adaptive-manager.stdout.log",
        "manager_stderr_relative_path": "raw/process/adaptive-manager.stderr.log",
        "replica_stdout_relative_paths": stdout,
        "replica_stderr_relative_paths": stderr,
        "kauri_fault_marker_relative_paths": [
            path for pair in zip(stdout, stderr) for path in pair
        ],
        "exclusive_output_per_process": True,
    }:
        _fail("runtime process-log contract drifted")
    manager_template = _mapping(runtime.get("manager_argv_template"), "manager argv template")
    manager_argv = tuple(
        _string(value, "manager argv")
        for value in _array(manager_template.get("argv"), "manager argv")
    )
    validate_manager_blinding(
        manager_argv,
        (),
    )
    required_nonresponsive = (
        manager_argv[manager_argv.index("--required-nonresponsive") + 1]
        if manager_argv.count("--required-nonresponsive") == 1
        and manager_argv.index("--required-nonresponsive") + 1 < len(manager_argv)
        else None
    )
    if (
        manager_argv.count("--transition-request") != 2
        or manager_argv.count("--bundle-output") != 2
        or required_nonresponsive != str(len(expected.actor_ids))
    ):
        _fail("manager template does not carry exactly two transition requests")
    replica_templates = _array(runtime.get("replica_argv_templates"), "runtime replica argv templates")
    if len(replica_templates) != expected.replica_count:
        _fail("runtime replica templates do not cover exact membership")
    expected_actors = ",".join(map(str, expected.actor_ids))
    expected_window, expected_max_omissions = _effective_omission_contract(
        manifest, expected
    )
    for replica_id, raw in enumerate(replica_templates):
        item = _mapping(raw, f"runtime replica template {replica_id}")
        if item.get("replica_id") != replica_id:
            _fail("runtime replica templates are not in exact member order")
        argv = tuple(
            _string(value, "runtime replica argv")
            for value in _array(item.get("argv"), "runtime replica argv")
        )
        options = {argv[index]: argv[index + 1] for index in range(len(argv) - 1) if argv[index].startswith("--")}
        if (
            options.get("--experiment-byzantine-mode") != manifest.byzantine.mode
            or options.get("--experiment-byzantine-window") != expected_window
            or options.get("--experiment-rotating-omission-actors")
            != expected_actors
            or options.get("--experiment-rotating-omission-context-limit")
            != str(manifest.byzantine.maximum_rotating_contexts)
            or options.get("--experiment-byzantine-max-omissions-per-proposal")
            != str(expected_max_omissions)
        ):
            _fail("runtime replica omission actor/cap contract drifted")


def validate_manager_blinding(
    manager_argv: Sequence[str], manager_events: Sequence[Mapping[str, Any]]
) -> None:
    """Reject explicit experiment-actor truth in the manager input/audit."""

    joined = "\x00".join(manager_argv)
    if any(token in joined for token in _FORBIDDEN_MANAGER_TOKENS):
        _fail("manager argv leaks experiment actor truth")

    def inspect(value: object) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if key in _FORBIDDEN_MANAGER_KEYS:
                    _fail(f"manager event leaks forbidden actor truth field {key}")
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)
        elif isinstance(value, str) and any(token in value for token in _FORBIDDEN_MANAGER_TOKENS):
            _fail("manager event text leaks experiment actor truth")

    for event in manager_events:
        inspect(event)


_MESSAGE_TYPE_CODE = {"direct_vote": 1, "aggregate_relay": 2}
_OUTCOME_CODE = {"on_time": 1, "timeout": 2, "late": 3}
_CLASSIFICATION_CODE = {
    "responsive": 1,
    "insufficient_evidence": 2,
    "nonresponsive": 3,
}
_SHAPE_REJECTION_CODE = {
    "none": 0,
    "fanout_out_of_range": 1,
    "wait_exempt_would_influence": 2,
    "missing_influential_evidence": 3,
    "score_overflow": 4,
}


def _evidence_record(event: _NativeEvent, membership: set[int]) -> _EvidenceRecord:
    label = f"{event.relative_path}:{event.line_number}"
    payload = event.payload
    _fields(payload, {"ingestion_sequence", "observation"}, f"{label}.payload")
    observation = _mapping(payload["observation"], f"{label}.observation")
    _fields(
        observation,
        {
            "schema_version",
            "observation_id",
            "reporter_id",
            "observed_replica_id",
            "configuration",
            "block_hash",
            "expected_message_type",
            "outcome",
            "response_duration_us",
            "deadline_duration_us",
            "reporter_monotonic_ns",
            "reporter_sequence",
            "signer_set",
        },
        f"{label}.observation",
    )
    configuration = _mapping(
        observation["configuration"], f"{label}.observation.configuration"
    )
    _fields(
        configuration,
        {"epoch_number", "tree_id", "epoch_digest"},
        f"{label}.observation.configuration",
    )
    reporter = _integer(observation["reporter_id"], f"{label}.reporter_id")
    target = _integer(
        observation["observed_replica_id"], f"{label}.observed_replica_id"
    )
    epoch = _integer(configuration["epoch_number"], f"{label}.epoch_number")
    tree_id = _integer(configuration["tree_id"], f"{label}.tree_id")
    epoch_digest = _digest(configuration["epoch_digest"], f"{label}.epoch_digest")
    block_hash = _digest(observation["block_hash"], f"{label}.block_hash")
    message_type = _string(
        observation["expected_message_type"], f"{label}.expected_message_type"
    )
    outcome = _string(observation["outcome"], f"{label}.outcome")
    if message_type not in _MESSAGE_TYPE_CODE or outcome not in _OUTCOME_CODE:
        _fail(f"{label} has an unsupported adaptation message/outcome")
    response_duration = _integer(
        observation["response_duration_us"], f"{label}.response_duration_us"
    )
    deadline = _integer(
        observation["deadline_duration_us"], f"{label}.deadline_duration_us", 1
    )
    reporter_monotonic = _integer(
        observation["reporter_monotonic_ns"], f"{label}.reporter_monotonic_ns", 1
    )
    if reporter_monotonic > event.monotonic_ns:
        _fail(f"{label} was accepted before its reporter timestamp")
    reporter_sequence = _integer(
        observation["reporter_sequence"], f"{label}.reporter_sequence", 1
    )
    signers = tuple(
        _integer(value, f"{label}.signer_set")
        for value in _array(observation["signer_set"], f"{label}.signer_set")
    )
    if reporter not in membership or target not in membership or not set(signers).issubset(membership):
        _fail(f"{label} contains evidence for a nonmember")
    if tuple(sorted(set(signers))) != signers:
        _fail(f"{label} signer set is not strictly increasing")
    if outcome == "timeout":
        if response_duration != 0 or signers:
            _fail(f"{label} timeout has a response or signer set")
    elif (
        not signers
        or (outcome == "late" and response_duration < deadline)
        or (outcome == "on_time" and response_duration >= deadline)
    ):
        _fail(f"{label} response timing/signer set is invalid")

    expected_id = _sha256(
        _OBSERVATION_DOMAIN
        + _u(reporter, 2)
        + _u(target, 2)
        + _u(epoch, 4)
        + _u(tree_id, 4)
        + bytes.fromhex(epoch_digest)
        + bytes.fromhex(block_hash)
        + _u(_MESSAGE_TYPE_CODE[message_type], 1)
    )
    observation_id = _digest(observation["observation_id"], f"{label}.observation_id")
    if observation["schema_version"] != 1 or observation_id != expected_id:
        _fail(f"{label} observation identity does not independently recompute")
    return _EvidenceRecord(
        ingestion_sequence=_integer(
            payload["ingestion_sequence"], f"{label}.ingestion_sequence", 1
        ),
        acceptance_monotonic_ns=event.monotonic_ns,
        observation_id=observation_id,
        reporter_id=reporter,
        target_id=target,
        epoch_number=epoch,
        tree_id=tree_id,
        epoch_digest=epoch_digest,
        block_hash=block_hash,
        message_type=message_type,
        outcome=outcome,
        response_duration_us=response_duration,
        deadline_duration_us=deadline,
        reporter_monotonic_ns=reporter_monotonic,
        reporter_sequence=reporter_sequence,
        signer_set=signers,
    )


def _accepted_evidence(
    manager_events: Sequence[_NativeEvent],
    replica_count: int,
    *,
    allow_ingestion_sequence_gaps: bool = False,
) -> dict[tuple[int, str], tuple[_EvidenceRecord, ...]]:
    membership = set(range(replica_count))
    grouped: dict[tuple[int, str], list[_EvidenceRecord]] = defaultdict(list)
    reporter_watermarks: dict[tuple[int, str, int], tuple[int, int]] = {}
    for event in manager_events:
        if event.event_type != "evidence.observation_accepted":
            continue
        record = _evidence_record(event, membership)
        epoch_id = (record.epoch_number, record.epoch_digest)
        grouped[epoch_id].append(record)
        reporter_key = (*epoch_id, record.reporter_id)
        previous = reporter_watermarks.get(reporter_key)
        if previous is not None and (
            record.reporter_sequence <= previous[0]
            or record.reporter_monotonic_ns < previous[1]
        ):
            _fail("accepted evidence reporter sequence/timestamp regressed")
        reporter_watermarks[reporter_key] = (
            record.reporter_sequence,
            record.reporter_monotonic_ns,
        )
    for epoch_id, records in grouped.items():
        sequences = tuple(record.ingestion_sequence for record in records)
        if allow_ingestion_sequence_gaps:
            if any(left >= right for left, right in zip(sequences, sequences[1:])):
                _fail(
                    f"accepted evidence for epoch {epoch_id[0]} is not "
                    "strictly increasing"
                )
        elif sequences != tuple(range(1, len(records) + 1)):
            _fail(f"accepted evidence for epoch {epoch_id[0]} is non-contiguous")
        attempts: dict[str, _EvidenceRecord] = {}
        for record in records:
            first = attempts.get(record.observation_id)
            if first is None:
                if record.outcome == "late":
                    _fail("accepted evidence starts an attempt with late")
                attempts[record.observation_id] = record
                continue
            if not (
                first.outcome == "timeout"
                and record.outcome == "late"
                and first.reporter_id == record.reporter_id
                and first.target_id == record.target_id
                and first.epoch_number == record.epoch_number
                and first.tree_id == record.tree_id
                and first.epoch_digest == record.epoch_digest
                and first.block_hash == record.block_hash
                and first.message_type == record.message_type
                and first.deadline_duration_us == record.deadline_duration_us
            ):
                _fail("accepted evidence contains a duplicate/invalid attempt transition")
            attempts[record.observation_id] = record
        grouped[epoch_id] = records
    return {key: tuple(value) for key, value in grouped.items()}


def _snapshot_records(
    records: Sequence[_EvidenceRecord],
    *,
    baseline_cutoff: int,
    current_cutoff: int,
    suffix_only: bool,
    allow_high_watermark_gaps: bool = False,
) -> tuple[_EvidenceRecord, ...]:
    prefix = tuple(record for record in records if record.ingestion_sequence <= current_cutoff)
    if not prefix:
        _fail("evidence snapshot contains no accepted prefix")
    if (
        not allow_high_watermark_gaps
        and prefix[-1].ingestion_sequence != current_cutoff
    ):
        _fail("evidence snapshot cutoff is not an exact accepted prefix")
    if not suffix_only:
        return prefix
    baseline_attempts: dict[str, _EvidenceRecord] = {}
    result: list[_EvidenceRecord] = []
    for record in prefix:
        if record.ingestion_sequence <= baseline_cutoff:
            first = baseline_attempts.get(record.observation_id)
            if first is None:
                if record.outcome == "late":
                    _fail("baseline evidence begins with a late response")
                baseline_attempts[record.observation_id] = record
            elif not (first.outcome == "timeout" and record.outcome == "late"):
                _fail("baseline evidence contains an invalid transition")
            else:
                baseline_attempts[record.observation_id] = record
            continue
        baseline = baseline_attempts.get(record.observation_id)
        if baseline is not None:
            if not (baseline.outcome == "timeout" and record.outcome == "late"):
                _fail("post-baseline evidence illegally reuses a baseline attempt")
            baseline_attempts[record.observation_id] = record
            continue
        result.append(record)
    return tuple(result)


def _score_snapshot(
    records: Sequence[_EvidenceRecord],
    replica_count: int,
    policy: Mapping[str, Any],
) -> tuple[ReplicaScore, ...]:
    attempts: dict[str, dict[str, Any]] = {}
    for record in records:
        attempt = attempts.get(record.observation_id)
        if attempt is None:
            if record.outcome == "late":
                _fail("snapshot evidence begins an attempt with late")
            attempts[record.observation_id] = {
                "target": record.target_id,
                "first": record.ingestion_sequence,
                "state": "timeout_only" if record.outcome == "timeout" else "on_time",
                "latency": record.response_duration_us,
                "deadline": record.deadline_duration_us,
            }
            continue
        if attempt["state"] != "timeout_only" or record.outcome != "late":
            _fail("snapshot evidence has an invalid timeout-to-late transition")
        attempt["state"] = "late"
        attempt["latency"] = record.response_duration_us

    attempt_window = _integer(policy.get("attempt_window"), "responsiveness.attempt_window", 1)
    minimum_attempts = _integer(policy.get("minimum_attempts"), "responsiveness.minimum_attempts", 1)
    minimum_response_rate = _integer(
        policy.get("minimum_response_rate_ppm"),
        "responsiveness.minimum_response_rate_ppm",
    )
    maximum_timeout_rate = _integer(
        policy.get("maximum_timeout_rate_ppm"),
        "responsiveness.maximum_timeout_rate_ppm",
    )
    trailing_streak = _integer(
        policy.get("trailing_timeout_streak"),
        "responsiveness.trailing_timeout_streak",
        2,
    )
    percentile = _integer(
        policy.get("latency_percentile_basis_points"),
        "responsiveness.latency_percentile_basis_points",
        1,
    )
    scores: list[ReplicaScore] = []
    for replica in range(replica_count):
        selected = sorted(
            (attempt for attempt in attempts.values() if attempt["target"] == replica),
            key=lambda attempt: attempt["first"],
        )[-attempt_window:]
        count = len(selected)
        response_count = sum(attempt["state"] in ("on_time", "late") for attempt in selected)
        timeout_count = sum(attempt["state"] in ("timeout_only", "late") for attempt in selected)
        trailing = 0
        for attempt in reversed(selected):
            if attempt["state"] != "timeout_only":
                break
            trailing += 1
        response_rate = 0 if count == 0 else response_count * 1_000_000 // count
        timeout_rate = 0 if count == 0 else timeout_count * 1_000_000 // count
        latencies = sorted(
            attempt["latency"]
            for attempt in selected
            if attempt["state"] in ("on_time", "late")
        )
        latency: int | None = None
        if latencies:
            rank = (percentile * len(latencies) + 9_999) // 10_000
            latency = latencies[rank - 1]
        if count < minimum_attempts:
            classification, eligible = "insufficient_evidence", False
        elif (
            response_rate < minimum_response_rate
            or timeout_rate > maximum_timeout_rate
            or trailing >= trailing_streak
        ):
            classification, eligible = "nonresponsive", False
        else:
            classification, eligible = "responsive", True
        scores.append(
            ReplicaScore(
                replica_id=replica,
                classification=classification,
                eligible=eligible,
                attempt_count=count,
                response_rate_ppm=response_rate,
                timeout_rate_ppm=timeout_rate,
                latency_percentile_us=latency,
            )
        )
    scores.sort(
        key=lambda score: (
            not score.eligible,
            -score.response_rate_ppm,
            score.timeout_rate_ppm,
            score.latency_percentile_us is None,
            score.latency_percentile_us if score.latency_percentile_us is not None else 0,
            -score.attempt_count,
            score.replica_id,
        )
    )
    return tuple(scores)


def _snapshot_id(
    records: Sequence[_EvidenceRecord],
    *,
    replica_count: int,
    epoch_number: int,
    epoch_digest: str,
    cutoff: int,
    policy: Mapping[str, Any],
) -> str:
    result = bytearray(_SNAPSHOT_DOMAIN)
    result += _u(1, 4) + _u(epoch_number, 4) + bytes.fromhex(epoch_digest)
    result += _u(cutoff, 8) + _u(_SNAPSHOT_SEED, 8)
    result += _u(replica_count, 4)
    result += b"".join(_u(member, 2) for member in range(replica_count))
    result += _u(1, 4)
    result += _cstr(_string(policy.get("policy_version"), "responsiveness.policy_version"))
    result += _u(_integer(policy.get("attempt_window"), "attempt_window", 1), 4)
    result += _u(_integer(policy.get("minimum_attempts"), "minimum_attempts", 1), 4)
    result += _u(_integer(policy.get("minimum_response_rate_ppm"), "minimum_response_rate_ppm"), 4)
    result += _u(_integer(policy.get("maximum_timeout_rate_ppm"), "maximum_timeout_rate_ppm"), 4)
    result += _u(_integer(policy.get("trailing_timeout_streak"), "trailing_timeout_streak", 2), 4)
    result += _u(_integer(policy.get("latency_percentile_basis_points"), "latency_percentile_basis_points", 1), 2)
    result += _u(len(records), 4)
    for record in records:
        result += _u(record.ingestion_sequence, 8)
        result += _u(1, 4) + bytes.fromhex(record.observation_id)
        result += _u(record.reporter_id, 2) + _u(record.target_id, 2)
        result += _u(record.epoch_number, 4) + _u(record.tree_id, 4)
        result += bytes.fromhex(record.epoch_digest) + bytes.fromhex(record.block_hash)
        result += _u(_MESSAGE_TYPE_CODE[record.message_type], 1)
        result += _u(_OUTCOME_CODE[record.outcome], 1)
        result += _u(record.response_duration_us, 8)
        result += _u(record.deadline_duration_us, 8)
        result += _u(record.reporter_monotonic_ns, 8)
        result += _u(record.reporter_sequence, 8)
        result += _u(len(record.signer_set), 4)
        result += b"".join(_u(signer, 2) for signer in record.signer_set)
    return _sha256(bytes(result))


def _first_leaf_index(member_count: int, fanout: int) -> int:
    if member_count <= 0 or fanout <= 0:
        _fail("leaf boundary requires non-empty membership and positive fanout")
    return 0 if member_count == 1 else (member_count - 2) // fanout + 1


def _shape_topology_digest(
    *,
    epoch_number: int,
    epoch_digest: str,
    tree_count: int,
    pipeline_stretch: int,
    trees: Sequence[Tree],
) -> str:
    ordered = tuple(sorted(trees, key=lambda tree: tree.tree_id))
    value = bytearray(_SHAPE_TOPOLOGY_DOMAIN)
    value += _u(epoch_number, 4) + bytes.fromhex(epoch_digest)
    value += _u(tree_count, 4) + _u(pipeline_stretch, 4) + _u(len(ordered), 4)
    for tree in ordered:
        value += _u(tree.tree_id, 4) + _u(tree.fanout, 4)
        value += _u(tree.pipeline_stretch, 4) + _u(len(tree.members), 4)
        value += b"".join(_u(member, 2) for member in tree.members)
        value += _u(len(tree.wait_exempt), 4)
        value += b"".join(_u(member, 2) for member in tree.wait_exempt)
    return _sha256(bytes(value))


def _shape_evidence_digest(
    *,
    epoch_number: int,
    epoch_digest: str,
    evidence_cutoff: int,
    scores: Sequence[ReplicaScore],
) -> str:
    ordered = tuple(sorted(scores, key=lambda score: score.replica_id))
    value = bytearray(_SHAPE_EVIDENCE_DOMAIN)
    value += _u(epoch_number, 4) + bytes.fromhex(epoch_digest)
    value += _u(evidence_cutoff, 8) + _u(len(ordered), 4)
    for score in ordered:
        value += _u(score.replica_id, 2)
        value += _u(_CLASSIFICATION_CODE[score.classification], 1)
        value += _u(score.attempt_count, 4) + _u(score.timeout_rate_ppm, 4)
        value += _u(int(score.latency_percentile_us is not None), 1)
        if score.latency_percentile_us is not None:
            value += _u(score.latency_percentile_us, 8)
    return _sha256(bytes(value))


def _shape_candidate(
    fanout: int,
    trees: Sequence[Tree],
    tree_count: int,
    scores: Mapping[int, ReplicaScore],
) -> dict[str, Any]:
    candidate = {
        "fanout": fanout,
        "rejection": "none",
        "depth": 0,
        "risk": 0,
        "latency": 0,
        "churn": 0,
        "switch_threshold_satisfied": False,
    }
    if not 1 <= fanout <= 255:
        candidate["rejection"] = "fanout_out_of_range"
        return candidate
    candidate["depth"] = _tree_depth(len(trees[0].members), fanout)
    for tree in tuple(sorted(trees, key=lambda item: item.tree_id))[:tree_count]:
        leaf_start = _first_leaf_index(len(tree.members), fanout)
        wait_exempt = set(tree.wait_exempt)
        if any(
            replica not in tree.members or tree.members.index(replica) < leaf_start
            for replica in wait_exempt
        ):
            candidate.update(rejection="wait_exempt_would_influence", risk=0, latency=0, churn=0)
            return candidate
        subtree_size = [0] * len(tree.members)
        path_latency = [0] * len(tree.members)
        for position, replica in enumerate(tree.members):
            if replica in wait_exempt:
                continue
            score = scores.get(replica)
            if score is None or score.attempt_count == 0 or score.latency_percentile_us is None:
                candidate.update(rejection="missing_influential_evidence", risk=0, latency=0, churn=0)
                return candidate
            subtree_size[position] = 1
            if position:
                parent = (position - 1) // fanout
                latency = path_latency[parent] + score.latency_percentile_us
                if latency > _UINT64_MAX:
                    candidate.update(rejection="score_overflow", risk=0, latency=0, churn=0)
                    return candidate
                path_latency[position] = latency
                candidate["latency"] = max(candidate["latency"], latency)
        for position in range(len(tree.members) - 1, 0, -1):
            parent = (position - 1) // fanout
            subtree_size[parent] += subtree_size[position]
            if subtree_size[parent] > _UINT64_MAX:
                candidate.update(rejection="score_overflow", risk=0, latency=0, churn=0)
                return candidate
        for position, replica in enumerate(tree.members):
            if replica in wait_exempt:
                continue
            exposure = scores[replica].timeout_rate_ppm * subtree_size[position]
            if exposure > _UINT64_MAX:
                candidate.update(rejection="score_overflow", risk=0, latency=0, churn=0)
                return candidate
            candidate["risk"] = max(candidate["risk"], exposure)
        for position in range(1, len(tree.members)):
            old_parent = (position - 1) // tree.fanout
            new_parent = (position - 1) // fanout
            if tree.members[old_parent] != tree.members[new_parent]:
                candidate["churn"] += 1
    return candidate


def _improves_five_percent(candidate: int, current: int) -> bool:
    if current == 0 or candidate >= current:
        return False
    required = current // 20 + int(current % 20 != 0)
    return candidate <= current - required


def _shape_decision_digest(decision: Mapping[str, Any]) -> str:
    status_code = {"selected": 1, "invalid_input": 2, "no_feasible_candidate": 3}
    value = bytearray(_SHAPE_DECISION_DOMAIN)
    value += _u(_integer(decision.get("schema_version"), "shape.schema_version", 1), 4)
    status = _string(decision.get("status"), "shape.status")
    if status not in status_code:
        _fail("shape decision status is invalid")
    value += _u(status_code[status], 1)
    value += _cstr(_string(decision.get("selector_version"), "shape.selector_version"))
    value += _cstr(_string(decision.get("tie_rule"), "shape.tie_rule"))
    value += _u(_integer(decision.get("epoch_number"), "shape.epoch_number"), 4)
    value += bytes.fromhex(_digest(decision.get("epoch_digest"), "shape.epoch_digest"))
    value += bytes.fromhex(
        _digest(decision.get("current_topology_digest"), "shape.current_topology_digest")
    )
    value += _u(_integer(decision.get("evidence_cutoff"), "shape.evidence_cutoff", 1), 8)
    value += bytes.fromhex(_digest(decision.get("evidence_digest"), "shape.evidence_digest"))
    for name in ("predecessor_tree_count", "tree_count", "fixed_pipeline_stretch"):
        value += _u(_integer(decision.get(name), f"shape.{name}", 1), 4)
    value += _u(_integer(decision.get("deterministic_seed"), "shape.deterministic_seed"), 8)
    for name in ("current_fanout", "selected_fanout", "applied_fanout"):
        value += _u(_integer(decision.get(name), f"shape.{name}", 1), 4)
    value += _cstr(_string(decision.get("reference_tree_rule"), "shape.reference_tree_rule"))
    candidates = _array(decision.get("candidates"), "shape.candidates")
    value += _u(len(candidates), 4)
    for index, raw in enumerate(candidates):
        candidate = _mapping(raw, f"shape.candidates[{index}]")
        rejection = _string(candidate.get("rejection"), f"shape.candidates[{index}].rejection")
        if rejection not in _SHAPE_REJECTION_CODE:
            _fail("shape candidate rejection is invalid")
        value += _u(_integer(candidate.get("fanout"), "candidate.fanout"), 4)
        value += _u(_SHAPE_REJECTION_CODE[rejection], 1)
        value += _u(_integer(candidate.get("depth"), "candidate.depth"), 4)
        value += _u(_integer(candidate.get("risk"), "candidate.risk"), 8)
        value += _u(_integer(candidate.get("latency"), "candidate.latency"), 8)
        value += _u(_integer(candidate.get("churn"), "candidate.churn"), 8)
        threshold = candidate.get("switch_threshold_satisfied")
        if type(threshold) is not bool:
            _fail("shape candidate threshold flag must be boolean")
        value += _u(int(threshold), 1)
    return _sha256(bytes(value))


def _recompute_shape_decision(
    *,
    epoch_number: int,
    epoch_digest: str,
    trees: Sequence[Tree],
    scores: Sequence[ReplicaScore],
    evidence_cutoff: int,
    candidate_fanouts: Sequence[int],
    tree_count: int,
    pipeline_stretch: int,
    deterministic_seed: int,
    apply_selected: bool,
) -> dict[str, Any]:
    ordered_trees = tuple(sorted(trees, key=lambda tree: tree.tree_id))
    if not ordered_trees or tree_count <= 0 or tree_count > len(ordered_trees):
        _fail("shape decision has an invalid predecessor tree prefix")
    current_fanout = ordered_trees[0].fanout
    membership = tuple(sorted(ordered_trees[0].members))
    for tree in ordered_trees:
        if (
            tuple(sorted(tree.members)) != membership
            or tree.fanout != current_fanout
            or tree.pipeline_stretch != pipeline_stretch
        ):
            _fail("shape predecessor topology is not uniform/canonical")
    candidates = tuple(sorted(set(candidate_fanouts)))
    if tuple(candidate_fanouts) != candidates or current_fanout not in candidates:
        _fail("shape candidate fanouts are not canonical or omit the current fanout")
    indexed_scores = {score.replica_id: score for score in scores}
    if set(indexed_scores) != set(membership):
        _fail("shape evidence does not cover membership exactly")
    table = [
        _shape_candidate(fanout, ordered_trees, tree_count, indexed_scores)
        for fanout in candidates
    ]
    current = next(
        (row for row in table if row["fanout"] == current_fanout and row["rejection"] == "none"),
        None,
    )
    if current is None:
        _fail("current shape is not a feasible selector candidate")
    qualifying: list[dict[str, Any]] = []
    for row in table:
        if row is current or row["rejection"] != "none":
            continue
        qualifies = (
            _improves_five_percent(row["latency"], current["latency"])
            and row["risk"] <= current["risk"]
        ) or (
            _improves_five_percent(row["risk"], current["risk"])
            and row["latency"] <= current["latency"]
        )
        row["switch_threshold_satisfied"] = qualifies
        if qualifies:
            qualifying.append(row)
    selected = current_fanout
    if qualifying:
        selected = min(
            qualifying,
            key=lambda row: (
                row["latency"],
                row["risk"],
                row["churn"],
                row["fanout"] != current_fanout,
                row["fanout"],
            ),
        )["fanout"]
    decision: dict[str, Any] = {
        "schema_version": 1,
        "status": "selected",
        "selector_version": SELECTOR_VERSION,
        "tie_rule": SHAPE_TIE_RULE,
        "epoch_number": epoch_number,
        "epoch_digest": epoch_digest,
        "current_topology_digest": _shape_topology_digest(
            epoch_number=epoch_number,
            epoch_digest=epoch_digest,
            tree_count=tree_count,
            pipeline_stretch=pipeline_stretch,
            trees=ordered_trees,
        ),
        "evidence_cutoff": evidence_cutoff,
        "evidence_digest": _shape_evidence_digest(
            epoch_number=epoch_number,
            epoch_digest=epoch_digest,
            evidence_cutoff=evidence_cutoff,
            scores=scores,
        ),
        "predecessor_tree_count": len(ordered_trees),
        "tree_count": tree_count,
        "fixed_pipeline_stretch": pipeline_stretch,
        "deterministic_seed": deterministic_seed,
        "current_fanout": current_fanout,
        "selected_fanout": selected,
        "applied_fanout": selected if apply_selected else current_fanout,
        "reference_tree_rule": REFERENCE_TREE_RULE,
        "candidates": table,
    }
    decision["decision_digest"] = _shape_decision_digest(decision)
    return decision


def validate_shape_decision(
    audit: Mapping[str, Any],
    *,
    epoch_number: int,
    epoch_digest: str,
    trees: Sequence[Tree],
    scores: Sequence[ReplicaScore],
    evidence_cutoff: int,
    candidate_fanouts: Sequence[int],
    tree_count: int,
    pipeline_stretch: int,
    deterministic_seed: int,
    apply_selected: bool,
) -> Mapping[str, Any]:
    """Reject a selector record unless every score and choice recomputes."""

    expected = _recompute_shape_decision(
        epoch_number=epoch_number,
        epoch_digest=epoch_digest,
        trees=trees,
        scores=scores,
        evidence_cutoff=evidence_cutoff,
        candidate_fanouts=candidate_fanouts,
        tree_count=tree_count,
        pipeline_stretch=pipeline_stretch,
        deterministic_seed=deterministic_seed,
        apply_selected=apply_selected,
    )
    if dict(audit) != expected:
        _fail("shape decision differs from independent selector recomputation")
    return expected


def _commit_payload(event: _NativeEvent, *, authoritative: bool) -> dict[str, Any]:
    label = f"{event.relative_path}:{event.line_number}.payload"
    payload = event.payload
    expected_fields = {
        "block_height",
        "block_hash",
        "parent_hash",
        "transaction_count",
        "commit_batch_index",
    }
    if event.event_type == "block.committed":
        expected_fields |= {"designated_observer", "decision_proof", "view_generation"}
    _fields(payload, expected_fields, label)
    height = _integer(payload["block_height"], f"{label}.block_height", 1)
    block_hash = _digest(payload["block_hash"], f"{label}.block_hash")
    parent = payload["parent_hash"]
    if parent is not None:
        parent = _digest(parent, f"{label}.parent_hash")
    transactions = _integer(
        payload["transaction_count"], f"{label}.transaction_count"
    )
    if transactions > _UINT64_MAX:
        _fail(f"{label}.transaction_count exceeds the fixed uint64 bound")
    batch_index = _integer(payload["commit_batch_index"], f"{label}.commit_batch_index")
    if event.event_type == "block.committed":
        if type(payload["designated_observer"]) is not bool:
            _fail(f"{label}.designated_observer must be boolean")
        if payload["designated_observer"] is not authoritative:
            _fail(f"{label} has an invalid designated-observer identity")
        proof = _mapping(payload["decision_proof"], f"{label}.decision_proof")
        _fields(
            proof,
            {"epoch_number", "tree_id", "epoch_digest", "block_hash"},
            f"{label}.decision_proof",
        )
        if _digest(proof["block_hash"], f"{label}.decision_proof.block_hash") != block_hash:
            _fail(f"{label} decision proof is for a different block")
        epoch_number = _integer(
            proof["epoch_number"], f"{label}.decision_proof.epoch_number"
        )
        tree_id = _integer(proof["tree_id"], f"{label}.decision_proof.tree_id")
        epoch_digest = _digest(
            proof["epoch_digest"], f"{label}.decision_proof.epoch_digest"
        )
        if payload["view_generation"] is not None:
            _integer(payload["view_generation"], f"{label}.view_generation")
    return {
        "height": height,
        "hash": block_hash,
        "parent": parent,
        "transactions": transactions,
        "batch_index": batch_index,
        "monotonic_ns": event.monotonic_ns,
        "epoch_number": epoch_number if event.event_type == "block.committed" else None,
        "tree_id": tree_id if event.event_type == "block.committed" else None,
        "epoch_digest": epoch_digest if event.event_type == "block.committed" else None,
    }


def _authoritative_commits(
    replica_events: Mapping[int, Sequence[_NativeEvent]],
) -> tuple[dict[str, Any], ...]:
    if 0 not in replica_events:
        _incomplete("designated observer replica-0 stream is absent")
    authoritative: list[dict[str, Any]] = []
    observed: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
    for replica_id, events in replica_events.items():
        for event in events:
            if event.event_type == "block.committed":
                commit = _commit_payload(event, authoritative=replica_id == 0)
                if replica_id == 0:
                    authoritative.append(commit)
            elif event.event_type == "block.commit_observed":
                commit = _commit_payload(event, authoritative=False)
                height = commit["height"]
                if height in observed[replica_id]:
                    _fail(f"replica-{replica_id} duplicated a commit observation at height {height}")
                observed[replica_id][height] = commit
    if not authoritative:
        _incomplete("designated observer emitted no authoritative commits")
    heights: dict[int, dict[str, Any]] = {}
    hashes: set[str] = set()
    previous_event_time = 0
    for commit in authoritative:
        height = commit["height"]
        if height in heights or commit["hash"] in hashes:
            _fail("designated observer contains a duplicated authoritative commit")
        if commit["monotonic_ns"] < previous_event_time:
            _fail("authoritative commit timestamps regress")
        previous_event_time = commit["monotonic_ns"]
        heights[height] = commit
        hashes.add(commit["hash"])
    ordered = tuple(sorted(authoritative, key=lambda commit: commit["height"]))
    for previous, current in zip(ordered, ordered[1:]):
        if current["height"] == previous["height"] + 1 and current["parent"] != previous["hash"]:
            _fail("authoritative commit chain has a conflicting parent")
    for replica_id, by_height in observed.items():
        for height, commit in by_height.items():
            authoritative_at_height = heights.get(height)
            if authoritative_at_height is not None and any(
                commit[key] != authoritative_at_height[key]
                for key in ("hash", "parent", "transactions")
            ):
                _fail(f"replica-{replica_id} conflicts with the authoritative commit at height {height}")
    return ordered


def validate_throughput_document(
    document: Mapping[str, Any],
    *,
    slot_id: str,
    replica_events: Mapping[int, Sequence[_NativeEvent]],
    phase_windows: Mapping[str, tuple[int, int, int]],
    phase_configurations: Mapping[str, tuple[int, str, Collection[int]]],
    bucket_width_s: int,
    observer_id: str,
    observer_instance: str,
) -> tuple[PhaseMetric, ...]:
    """Recompute all half-open, zero-filled TPS buckets from commits."""

    _fields(
        document,
        {"schema_version", "slot_id", "bucket_width_s", "authority", "phases"},
        "throughput.json",
    )
    if (
        document["schema_version"] != 1
        or document["slot_id"] != slot_id
        or document["bucket_width_s"] != bucket_width_s
    ):
        _fail("throughput document identity/bucket width drifted")
    authority = _mapping(document["authority"], "throughput.authority")
    if dict(authority) != {
        "event_type": "block.committed",
        "source_id": observer_id,
        "source_instance": observer_instance,
        "unique_commit_rule": "block_height_and_hash_exactly_once_v1",
    }:
        _fail("throughput authority is not the designated observer stream")
    commits = _authoritative_commits(replica_events)
    phases = _array(document["phases"], "throughput.phases")
    if len(phases) != len(PHASES):
        _fail("throughput document does not contain every phase exactly once")
    by_name: dict[str, Mapping[str, Any]] = {}
    for raw in phases:
        phase = _mapping(raw, "throughput phase")
        name = _string(phase.get("phase"), "throughput phase name")
        if name in by_name:
            _fail(f"throughput document duplicates phase {name}")
        by_name[name] = phase
    if (
        set(by_name) != set(PHASES)
        or set(phase_windows) != set(PHASES)
        or set(phase_configurations) != set(PHASES)
    ):
        _fail("throughput phase identities differ from the cutoff contract")
    width_ns = bucket_width_s * _NANOSECONDS_PER_SECOND
    metrics: list[PhaseMetric] = []
    for name in PHASES:
        start_ns, end_ns, bucket_count = phase_windows[name]
        if end_ns - start_ns != bucket_count * width_ns:
            _fail(f"phase {name} does not span its exact fixed bucket count")
        phase = by_name[name]
        _fields(
            phase,
            {
                "phase",
                "start_monotonic_ns",
                "end_monotonic_ns",
                "configuration",
                "buckets",
                "transactions",
                "mean_tps",
            },
            f"throughput.{name}",
        )
        if phase["start_monotonic_ns"] != start_ns or phase["end_monotonic_ns"] != end_ns:
            _fail(f"throughput phase {name} does not use the recorded live window")
        epoch_number, epoch_digest, allowed_tree_ids = phase_configurations[name]
        expected_configuration = {
            "epoch_number": epoch_number,
            "epoch_digest": epoch_digest,
        }
        if dict(
            _mapping(phase["configuration"], f"throughput.{name}.configuration")
        ) != expected_configuration:
            _fail(f"throughput phase {name} configuration identity drifted")
        allowed_trees = frozenset(allowed_tree_ids)
        if not allowed_trees:
            _fail(f"throughput phase {name} has no allowed trees")
        counts = [0] * bucket_count
        for commit in commits:
            timestamp = commit["monotonic_ns"]
            if start_ns <= timestamp < end_ns:
                if (
                    commit["epoch_number"] != epoch_number
                    or commit["epoch_digest"] != epoch_digest
                    or commit["tree_id"] not in allowed_trees
                ):
                    _fail(
                        f"throughput phase {name} contains a commit outside its "
                        "configuration-bound identity"
                    )
                counts[(timestamp - start_ns) // width_ns] += commit["transactions"]
        buckets = _array(phase["buckets"], f"throughput.{name}.buckets")
        if len(buckets) != bucket_count:
            _fail(f"throughput phase {name} omits an explicit bucket")
        rates: list[float] = []
        for index, raw_bucket in enumerate(buckets):
            bucket = _mapping(raw_bucket, f"throughput.{name}.buckets[{index}]")
            _fields(
                bucket,
                {"bucket_index", "start_monotonic_ns", "end_monotonic_ns", "transactions", "tps"},
                f"throughput.{name}.buckets[{index}]",
            )
            expected_start = start_ns + index * width_ns
            expected_end = expected_start + width_ns
            expected_tps = counts[index] / bucket_width_s
            recorded_tps = _bounded_float(
                bucket["tps"], f"throughput.{name}.buckets[{index}].tps"
            )
            if (
                bucket["bucket_index"] != index
                or bucket["start_monotonic_ns"] != expected_start
                or bucket["end_monotonic_ns"] != expected_end
                or bucket["transactions"] != counts[index]
                or not math.isclose(recorded_tps, expected_tps, rel_tol=0, abs_tol=1e-12)
            ):
                _fail(f"throughput bucket {name}[{index}] does not recompute")
            rates.append(expected_tps)
        transactions = sum(counts)
        mean_tps = transactions / (bucket_count * bucket_width_s)
        recorded_mean_tps = _bounded_float(
            phase["mean_tps"], f"throughput.{name}.mean_tps"
        )
        if (
            phase["transactions"] != transactions
            or not math.isclose(recorded_mean_tps, mean_tps, rel_tol=0, abs_tol=1e-12)
        ):
            _fail(f"throughput phase {name} aggregate does not recompute")
        metrics.append(PhaseMetric(name, transactions, mean_tps, tuple(rates)))
    return tuple(metrics)


def _fault_markers(
    slot_root: Path,
    paths_by_replica: Mapping[int, Sequence[str]],
) -> tuple[FaultMarker, ...]:
    markers: list[FaultMarker] = []
    seen: set[tuple[str, int, int, str, str, int, str]] = set()
    for replica_id, paths in paths_by_replica.items():
        for relative in paths:
            path = _safe_file(slot_root, relative)
            assert path is not None
            try:
                if path.stat().st_size > _MAX_EVENT_STREAM_BYTES:
                    _fail(f"process log exceeds its fixed bound: {relative}")
                with path.open("rb") as stream:
                    for line_number, raw in enumerate(stream, start=1):
                        if len(raw) > _MAX_EVENT_LINE_BYTES:
                            _fail(f"{relative}:{line_number} exceeds the line bound")
                        if b"KAURI_FAULT marker_skipped" in raw:
                            _fail(f"{relative}:{line_number} reports a skipped fault marker")
                        if b"KAURI_FAULT" not in raw:
                            continue
                        try:
                            line = raw.decode("utf-8", errors="strict").rstrip("\r\n")
                        except UnicodeDecodeError as error:
                            raise _Reject(f"{relative}:{line_number} fault marker is not UTF-8") from error
                        matches = tuple(_FAULT_MARKER.finditer(line))
                        if len(matches) != 1:
                            _fail(f"{relative}:{line_number} has a malformed/ambiguous fault marker")
                        match = matches[0]
                        marker = FaultMarker(
                            source_replica=replica_id,
                            line_number=line_number,
                            fault_mode=match.group(1),
                            epoch_number=int(match.group(2)),
                            tree_id=int(match.group(3)),
                            epoch_digest=match.group(4),
                            block_hash=match.group(5),
                            window=match.group(6),
                            window_start_ns=int(match.group(7)),
                            window_end_ns=int(match.group(8)),
                            actor=int(match.group(9)),
                            action=match.group(10),
                            monotonic_ns=int(match.group(11)),
                            raw_line_sha256=_sha256(raw),
                        )
                        if marker.action == "capacity_exhausted":
                            _fail(f"{relative}:{line_number} exhausted omission context capacity")
                        identity = (
                            marker.fault_mode,
                            marker.epoch_number,
                            marker.tree_id,
                            marker.epoch_digest,
                            marker.block_hash,
                            marker.actor,
                            marker.action,
                        )
                        if identity in seen:
                            _fail("native process logs contain a duplicate fault marker")
                        seen.add(identity)
                        markers.append(marker)
            except OSError as error:
                raise _Incomplete(f"cannot read process log {relative}") from error
    return tuple(markers)


def _adaptive_proposal_identity(event: _NativeEvent) -> tuple[int, int, str, str, int] | None:
    payload = event.payload
    configuration = payload.get("configuration")
    block_hash = payload.get("block_hash")
    observer = payload.get("observer_replica")
    if not isinstance(configuration, Mapping) or block_hash is None or type(observer) is not int:
        return None
    if set(configuration) != {"epoch_number", "tree_id", "epoch_digest"}:
        return None
    try:
        return (
            _integer(configuration["epoch_number"], "adaptive epoch"),
            _integer(configuration["tree_id"], "adaptive tree"),
            _digest(configuration["epoch_digest"], "adaptive epoch digest"),
            _digest(block_hash, "adaptive block hash"),
            _integer(observer, "adaptive observer"),
        )
    except FactorialValidationError:
        raise


def _validate_fault_marker_schedule(
    markers: Sequence[FaultMarker],
    *,
    actor_ids: Sequence[int],
    fault_mode: str,
    max_omissions_per_proposal: int,
) -> None:
    actors = tuple(sorted(actor_ids))
    actor_set = set(actors)
    if fault_mode not in {
        "rotating_intermittent_omission_v1",
        "persistent_selected_omission_v1",
    }:
        _fail("fault causality mode is not a known omission schedule")
    if not 1 <= max_omissions_per_proposal <= len(actors):
        _fail("fault causality proposal bound is outside the selected actor set")
    markers_by_proposal: dict[
        tuple[int, int, str, str], list[FaultMarker]
    ] = defaultdict(list)
    for marker in markers:
        if marker.fault_mode != fault_mode or marker.actor not in actor_set:
            _fail("fault marker mode/actor differs from the frozen schedule")
        if marker.action == "forward":
            _fail("fault marker cannot claim a no-op forward action")
        markers_by_proposal[
            (
                marker.epoch_number,
                marker.tree_id,
                marker.epoch_digest,
                marker.block_hash,
            )
        ].append(marker)
    for proposal, proposal_markers in markers_by_proposal.items():
        proposal_actors = [marker.actor for marker in proposal_markers]
        if (
            len(proposal_markers) > max_omissions_per_proposal
            or len(set(proposal_actors)) != len(proposal_actors)
        ):
            _fail(f"fault proposal exceeds its independent omission bound: {proposal}")


def _validate_persistent_interior_proposals(
    markers: Sequence[FaultMarker],
    *,
    actor_ids: Sequence[int],
    phase_windows: Mapping[str, tuple[int, int, int]],
    phase_configurations: Sequence[
        tuple[str, int, str, Mapping[int, Tree]]
    ],
) -> None:
    """Require complete persistent actor/action sets away from clock edges."""

    actors = frozenset(actor_ids)
    grouped: dict[tuple[int, int, str, str], list[FaultMarker]] = defaultdict(list)
    for marker in markers:
        grouped[
            (
                marker.epoch_number,
                marker.tree_id,
                marker.epoch_digest,
                marker.block_hash,
            )
        ].append(marker)
    for phase, epoch_number, epoch_digest, trees in phase_configurations:
        start_ns, end_ns, bucket_count = phase_windows[phase]
        span_ns = end_ns - start_ns
        if bucket_count <= 2 or span_ns <= 0 or span_ns % bucket_count:
            _fail(f"{phase} cannot derive its frozen interior bucket boundary")
        bucket_width_ns = span_ns // bucket_count
        interior_start = start_ns + bucket_width_ns
        interior_end = end_ns - bucket_width_ns
        represented = 0
        for (epoch, tree_id, digest, _), proposal_markers in grouped.items():
            if epoch != epoch_number or digest != epoch_digest or not any(
                interior_start <= marker.monotonic_ns < interior_end
                for marker in proposal_markers
            ):
                continue
            tree = trees.get(tree_id)
            if tree is None:
                _fail(f"{phase} persistent proposal references an unknown tree")
            leaf_start = _first_leaf_index(len(tree.members), tree.fanout)
            expected: dict[int, str] = {}
            for actor in actors:
                if actor not in tree.members:
                    _fail(f"{phase} persistent actor is absent from its proposal tree")
                position = tree.members.index(actor)
                if position == 0:
                    continue
                expected[actor] = (
                    "omit_aggregate" if position < leaf_start else "omit_direct_vote"
                )
            actual = {marker.actor: marker.action for marker in proposal_markers}
            if actual != expected:
                _fail(
                    f"{phase} persistent interior proposal actor/action set is "
                    "not the exact selected non-root set"
                )
            represented += 1
        if represented == 0:
            _incomplete(
                f"{phase} lacks a complete persistent proposal in its frozen interior"
            )


def validate_fault_causality(
    *,
    markers: Sequence[FaultMarker],
    replica_events: Mapping[int, Sequence[_NativeEvent]],
    actor_ids: Sequence[int],
    fault_mode: str,
    max_omissions_per_proposal: int,
    initial_epoch_digest: str,
    initial_trees: Sequence[Tree],
    window_id: str,
    window_start_ns: int,
    window_end_ns: int,
    epoch1_command_ns: int,
    epoch1_activation_ns: int,
    epoch2_command_ns: int,
    epoch1_digest: str,
    epoch1_trees: Sequence[Tree],
    epoch2_digest: str,
    epoch2_trees: Sequence[Tree],
    phase_windows: Mapping[str, tuple[int, int, int]],
    required_reporters: int,
    accepted_epoch0: Sequence[_EvidenceRecord] = (),
    baseline_cutoff: int = 0,
    current_cutoff: int = 0,
) -> None:
    """Bind omissions to exact guarded evidence and both physical roles."""

    actors = tuple(sorted(actor_ids))
    if not markers:
        _incomplete("native logs contain no KAURI_FAULT proposal markers")
    _validate_fault_marker_schedule(
        markers,
        actor_ids=actors,
        fault_mode=fault_mode,
        max_omissions_per_proposal=max_omissions_per_proposal,
    )
    proposals_by_replica: dict[int, set[tuple[int, int, str, str, int]]] = {}
    for replica_id, events in replica_events.items():
        proposals_by_replica[replica_id] = {
            identity
            for event in events
            if (identity := _adaptive_proposal_identity(event)) is not None
        }
    tree_by_id = {tree.tree_id: tree for tree in initial_trees}
    epoch1_tree_by_id = {tree.tree_id: tree for tree in epoch1_trees}
    epoch2_tree_by_id = {tree.tree_id: tree for tree in epoch2_trees}
    if set(phase_windows) != set(PHASES):
        _fail("fault causality does not cover the exact measured phases")
    fault_phase = phase_windows["fault_evidence"][:2]
    epoch1_phase = phase_windows["epoch1_stable"][:2]
    epoch2_phase = phase_windows["epoch2_stable"][:2]
    if not (
        window_start_ns <= fault_phase[0] < fault_phase[1]
        and epoch1_activation_ns <= epoch1_phase[0] < epoch1_phase[1]
        <= epoch2_command_ns
        and epoch2_phase[0] < epoch2_phase[1] <= window_end_ns
    ):
        _fail("fault causality phase windows are outside their exact live bounds")
    if fault_mode == "persistent_selected_omission_v1":
        _validate_persistent_interior_proposals(
            markers,
            actor_ids=actors,
            phase_windows=phase_windows,
            phase_configurations=(
                ("fault_evidence", 0, initial_epoch_digest, tree_by_id),
                ("epoch1_stable", 1, epoch1_digest, epoch1_tree_by_id),
                ("epoch2_stable", 2, epoch2_digest, epoch2_tree_by_id),
            ),
        )
    outstanding: dict[str, _EvidenceRecord] = {}
    for record in accepted_epoch0:
        if not baseline_cutoff < record.ingestion_sequence <= current_cutoff:
            continue
        if record.outcome == "timeout":
            outstanding[record.observation_id] = record
        elif record.outcome == "late":
            outstanding.pop(record.observation_id, None)
    timeouts_by_proposal: dict[
        tuple[int, int, int, str, str], list[_EvidenceRecord]
    ] = defaultdict(list)
    for record in outstanding.values():
        timeouts_by_proposal[
            (
                record.target_id,
                record.epoch_number,
                record.tree_id,
                record.epoch_digest,
                record.block_hash,
            )
        ].append(record)
    causal_reporters: dict[int, set[int]] = defaultdict(set)
    internal_actors: set[int] = set()
    epoch1_leaf_actors: set[int] = set()
    epoch2_leaf_actors: set[int] = set()
    for marker in markers:
        if (
            marker.fault_mode != fault_mode
            or marker.window != window_id
            or marker.window_start_ns != window_start_ns
            or marker.window_end_ns != window_end_ns
            or marker.actor != marker.source_replica
            or marker.actor not in actors
            or not window_start_ns <= marker.monotonic_ns < window_end_ns
        ):
            _fail("fault marker is not bound to the exact slot actor/window")
        if fault_mode == "rotating_intermittent_omission_v1":
            _, selected = fnv1a_rotating_actor(
                actors,
                epoch_number=marker.epoch_number,
                tree_id=marker.tree_id,
                epoch_digest=marker.epoch_digest,
                block_hash=marker.block_hash,
            )
            if selected != marker.actor:
                _fail("fault marker actor differs from independent FNV rotation")
        identity = (
            marker.epoch_number,
            marker.tree_id,
            marker.epoch_digest,
            marker.block_hash,
            marker.actor,
        )
        if identity not in proposals_by_replica.get(marker.actor, set()):
            _fail("fault marker has no matching native proposal/configuration event")
        if not (
            marker.epoch_number == 0
            and marker.epoch_digest == initial_epoch_digest
            and fault_phase[0] <= marker.monotonic_ns < fault_phase[1]
            and marker.monotonic_ns < epoch1_command_ns
        ):
            continue
        tree = tree_by_id.get(marker.tree_id)
        if tree is None or marker.actor not in tree.members:
            _fail("pre-containment omission references an unknown initial tree")
        position = tree.members.index(marker.actor)
        leaf_start = _first_leaf_index(len(tree.members), tree.fanout)
        if marker.action == "omit_aggregate":
            if not 0 < position < leaf_start:
                _fail("omit_aggregate marker actor was not physically internal")
            expected_message_type = "aggregate_relay"
            internal_actors.add(marker.actor)
        elif marker.action == "omit_direct_vote":
            if position < leaf_start:
                _fail("pre-containment omit_direct_vote actor was not physically a leaf")
            expected_message_type = "direct_vote"
        else:
            continue
        expected_reporter = tree.members[(position - 1) // tree.fanout]
        matched_timeouts = timeouts_by_proposal.get(
            (
                marker.actor,
                marker.epoch_number,
                marker.tree_id,
                marker.epoch_digest,
                marker.block_hash,
            ),
            (),
        )
        exact_timeouts = [
            timeout
            for timeout in matched_timeouts
            if (
            timeout.message_type == expected_message_type
            and timeout.reporter_id == expected_reporter
            and marker.monotonic_ns < timeout.reporter_monotonic_ns
            <= timeout.acceptance_monotonic_ns
            < epoch1_command_ns
            )
        ]
        if not exact_timeouts:
            _fail(
                "pre-containment omission has no exact outstanding timeout "
                "accepted before Epoch1 selection"
            )
        causal_reporters[marker.actor].add(expected_reporter)
    for marker in markers:
        if marker.action != "omit_direct_vote":
            continue
        if not (
            marker.epoch_number == 1
            and marker.epoch_digest == epoch1_digest
            and epoch1_activation_ns <= marker.monotonic_ns < epoch2_command_ns
        ):
            continue
        tree = epoch1_tree_by_id.get(marker.tree_id)
        if tree is None or marker.actor not in tree.members:
            _fail("post-containment omission references an unknown Epoch1 tree")
        position = tree.members.index(marker.actor)
        if (
            position < _first_leaf_index(len(tree.members), tree.fanout)
            or marker.actor not in tree.wait_exempt
        ):
            _fail("omit_direct_vote marker actor was not a wait-exempt leaf")
        if not epoch1_phase[0] <= marker.monotonic_ns < epoch1_phase[1]:
            continue
        epoch1_leaf_actors.add(marker.actor)
    for marker in markers:
        if marker.action != "omit_direct_vote":
            continue
        if not (
            marker.epoch_number == 2
            and marker.epoch_digest == epoch2_digest
            and epoch2_phase[0] <= marker.monotonic_ns < epoch2_phase[1]
        ):
            continue
        tree = epoch2_tree_by_id.get(marker.tree_id)
        if tree is None or marker.actor not in tree.members:
            _fail("Epoch2 omission references an unknown Epoch2 tree")
        position = tree.members.index(marker.actor)
        if (
            position < _first_leaf_index(len(tree.members), tree.fanout)
            or marker.actor not in tree.wait_exempt
        ):
            _fail("Epoch2 omit_direct_vote actor was not a wait-exempt leaf")
        epoch2_leaf_actors.add(marker.actor)
    if required_reporters <= 0:
        _fail("causal guard reporter threshold is invalid")
    missing_internal = set(actors) - internal_actors
    if missing_internal:
        _fail(
            "actors lack pre-Epoch1 causally bound internal omit_aggregate proof: "
            f"{sorted(missing_internal)}"
        )
    missing = {
        actor: required_reporters - len(causal_reporters.get(actor, set()))
        for actor in actors
        if len(causal_reporters.get(actor, set())) < required_reporters
    }
    if missing:
        _fail(
            "actors lack the full causally bound f+1 internal timeout guard: "
            f"{missing}"
        )
    missing_epoch1_leaf = set(actors) - epoch1_leaf_actors
    if missing_epoch1_leaf:
        _fail(
            "actors lack Epoch1-stable wait-exempt leaf omit_direct_vote proof: "
            f"{sorted(missing_epoch1_leaf)}"
        )
    missing_epoch2_leaf = set(actors) - epoch2_leaf_actors
    if missing_epoch2_leaf:
        _fail(
            "actors lack Epoch2-stable wait-exempt leaf omit_direct_vote proof: "
            f"{sorted(missing_epoch2_leaf)}"
        )


def _validate_slot_receipt(
    document: Mapping[str, Any],
    *,
    slot_root: Path,
    manifest: FrozenFactorialManifest,
    expected: _ExpectedSlot,
    runtime: Mapping[str, Any],
    runtime_sha256: str,
    authorization_bytes: bytes,
) -> tuple[int, int, int, Path, Path]:
    identity = _frozen_artifact_identity(manifest.manifest_id)
    _fields(
        document,
        {
            "schema_version",
            "slot_id",
            "runtime_artifact_id",
            "manifest_sha256",
            "plan_sha256",
            "runtime_sha256",
            "execution_ordinal",
            "attempt_ordinal",
            "retry_of",
            "replacement_for",
            "shared_raw_clock_anchor_ns",
            "fault_window_start_ns",
            "fault_window_end_ns",
            "redaction_key_id",
            "manager_argv",
            "replica_argv",
        },
        SLOT_FILENAME,
    )
    if (
        document["schema_version"] != 1
        or document["slot_id"] != expected.slot_id
        or document["runtime_artifact_id"] != runtime.get("artifact_id")
        or document["manifest_sha256"] != identity.manifest_sha256
        or document["plan_sha256"] != identity.plan_sha256
        or document["runtime_sha256"] != runtime_sha256
        or document["execution_ordinal"] != expected.execution_ordinal
        or document["attempt_ordinal"] != 1
        or document["retry_of"] is not None
        or document["replacement_for"] is not None
    ):
        _fail("slot receipt identity or no-retry/no-replacement contract drifted")
    anchor = _integer(
        document["shared_raw_clock_anchor_ns"], "slot.shared_raw_clock_anchor_ns"
    )
    window_start = _integer(document["fault_window_start_ns"], "slot.fault_window_start_ns")
    window_end = _integer(document["fault_window_end_ns"], "slot.fault_window_end_ns")
    fault_window = _mapping(runtime.get("fault_window"), "runtime.fault_window")
    expected_start = anchor + _integer(
        fault_window.get("start_after_prelaunch_anchor_s"),
        "runtime.fault_window.start_after_prelaunch_anchor_s",
    ) * _NANOSECONDS_PER_SECOND
    expected_end = expected_start + _integer(
        fault_window.get("duration_s"), "runtime.fault_window.duration_s", 1
    ) * _NANOSECONDS_PER_SECOND
    if (
        fault_window.get("clock") != "CLOCK_MONOTONIC_RAW"
        or fault_window.get("shared_anchor_per_slot") is not True
        or window_start != expected_start
        or window_end != expected_end
        or window_end > _UINT64_MAX
    ):
        _fail("slot receipt does not bind the exact shared raw-clock window")
    key_id = _string(document["redaction_key_id"], "slot.redaction_key_id")
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key_id) is None:
        _fail("slot redaction key ID is not canonical")
    slot_token = _string(
        _mapping(runtime.get("manager_argv_template"), "manager_argv_template").get(
            "slot_directory_token"
        ),
        "manager slot directory token",
    )
    raw_replicas = _array(document["replica_argv"], "slot.replica_argv")
    templates = _array(runtime.get("replica_argv_templates"), "runtime.replica_argv_templates")
    if len(raw_replicas) != expected.replica_count or len(templates) != expected.replica_count:
        _fail("replica argv receipt does not cover exact membership")
    actual_by_id: dict[int, tuple[str, ...]] = {}
    for raw in raw_replicas:
        item = _mapping(raw, "slot replica argv")
        _fields(item, {"replica_id", "argv"}, "slot replica argv")
        replica_id = _integer(item["replica_id"], "slot replica_id")
        if replica_id in actual_by_id:
            _fail("slot receipt duplicates a replica argv")
        actual_by_id[replica_id] = tuple(
            _string(value, "slot replica argv value")
            for value in _array(item["argv"], "slot replica argv")
        )
    if set(actual_by_id) != set(range(expected.replica_count)):
        _fail("slot receipt replica argv membership is incomplete")
    template_by_id: dict[int, tuple[str, ...]] = {}
    for raw in templates:
        item = _mapping(raw, "runtime replica argv template")
        replica_id = _integer(item.get("replica_id"), "runtime replica_id")
        template_by_id[replica_id] = tuple(
            _string(value, "runtime replica argv value")
            for value in _array(item.get("argv"), "runtime replica argv")
        )
    clock_tail = (
        "--experiment-byzantine-window-start-monotonic-ns",
        str(window_start),
        "--experiment-byzantine-window-end-monotonic-ns",
        str(window_end),
    )
    recorded_roots: set[str] = set()
    for replica_id in range(expected.replica_count):
        template = template_by_id[replica_id]
        actual = actual_by_id[replica_id]
        if len(actual) != len(template) + len(clock_tail) or tuple(
            actual[len(template) :]
        ) != clock_tail:
            _fail(f"replica-{replica_id} argv differs from exact clock-bound template")
        for expected_value, actual_value in zip(template, actual):
            if slot_token not in expected_value:
                if actual_value != expected_value:
                    _fail(
                        f"replica-{replica_id} argv differs from exact runtime template"
                    )
                continue
            if expected_value.count(slot_token) != 1:
                _fail("runtime slot-directory token cardinality drifted")
            prefix, suffix = expected_value.split(slot_token)
            if (
                not actual_value.startswith(prefix)
                or not actual_value.endswith(suffix)
                or len(actual_value) < len(prefix) + len(suffix) + 1
            ):
                _fail(
                    f"replica-{replica_id} argv does not preserve its original slot path"
                )
            end = len(actual_value) - len(suffix) if suffix else len(actual_value)
            recorded_roots.add(actual_value[len(prefix) : end])
    if len(recorded_roots) != 1:
        _fail("replica argv does not bind one exact original slot directory")
    recorded_slot_root = Path(next(iter(recorded_roots)))
    result_path = _string(runtime.get("result_path"), "runtime result path")
    result_parts = Path(result_path).parts
    if (
        not recorded_slot_root.is_absolute()
        or str(recorded_slot_root) != next(iter(recorded_roots))
        or not result_parts
        or result_path.startswith("/")
        or ".." in result_parts
        or tuple(recorded_slot_root.parts[-len(result_parts) :]) != result_parts
        or recorded_slot_root.name != expected.slot_id
    ):
        _fail("recorded original slot path is not the exact runtime result suffix")
    recorded_repository = recorded_slot_root
    for _ in result_parts:
        recorded_repository = recorded_repository.parent
    if not recorded_repository.is_absolute() or recorded_repository.name != "Kauri":
        _fail("recorded original slot path does not identify the Kauri repository")

    if not authorization_bytes:
        _fail("slot redaction is not bound to execution authorization bytes")
    slot_bytes = expected.slot_id.encode("utf-8")
    redaction_key = hashlib.sha256(
        _REDACTION_KEY_DOMAIN
        + b"\x00"
        + len(slot_bytes).to_bytes(4, "big")
        + slot_bytes
        + len(authorization_bytes).to_bytes(8, "big")
        + authorization_bytes
    ).digest()
    expected_key_id = hashlib.sha256(redaction_key).hexdigest()[:16]
    if key_id != expected_key_id:
        _fail("slot redaction key ID does not independently recompute")

    tls_bytes = _read_bytes(slot_root, "runtime/tls-identities.txt")
    issuer_bytes = _read_bytes(slot_root, "runtime/issuer-identities.txt")
    assert tls_bytes is not None and issuer_bytes is not None
    tls = _identity_rows(
        tls_bytes,
        expected_count=expected.replica_count + 1,
        expected_fields=frozenset({"crt", "sec", "cid"}),
        label="TLS identities",
    )
    issuer = _identity_rows(
        issuer_bytes,
        expected_count=1,
        expected_fields=frozenset({"pub", "sec"}),
        label="issuer identity",
    )[0]
    secret_values = {
        "{{manager_tls_private_key_der_hex}}": tls[expected.replica_count]["sec"],
        "{{manager_tls_certificate_der_hex}}": tls[expected.replica_count]["crt"],
        "{{epoch_issuer_private_key_hex}}": issuer["sec"],
        **{
            f"{{{{replica_{replica_id}_tls_certificate_der_hex}}}}": tls[
                replica_id
            ]["crt"]
            for replica_id in range(expected.replica_count)
        },
    }
    secret_tokens = tuple(
        _string(token, "manager secret token")
        for token in _array(
            _mapping(runtime.get("manager_argv_template"), "manager_argv_template").get(
                "secret_tokens"
            ),
            "manager secret tokens",
        )
    )
    if set(secret_tokens) != set(secret_values):
        _fail("manager secret token membership drifted")

    def redact(value: str) -> str:
        return (
            f"hmac-sha256:{key_id}:"
            + hmac.new(redaction_key, value.encode(), hashlib.sha256).hexdigest()
        )

    manager_template = tuple(
        _string(value, "runtime manager argv")
        for value in _array(
            _mapping(runtime.get("manager_argv_template"), "manager argv template").get(
                "argv"
            ),
            "runtime manager argv",
        )
    )
    expected_manager: list[str] = []
    for value in manager_template:
        rendered = value.replace(slot_token, str(recorded_slot_root))
        for token, secret in secret_values.items():
            rendered = rendered.replace(token, redact(secret))
        if "{{" in rendered or "}}" in rendered:
            _fail("manager argv template contains an unresolved token")
        expected_manager.append(rendered)
    manager_argv = tuple(
        _string(value, "receipt manager argv")
        for value in _array(document["manager_argv"], "slot.manager_argv")
    )
    if manager_argv != tuple(expected_manager):
        _fail("manager argv is not the reproducibly redacted runtime template")
    validate_manager_blinding(manager_argv, ())
    return (
        anchor,
        window_start,
        window_end,
        recorded_slot_root,
        recorded_repository,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise _Incomplete(f"cannot hash preserved artifact {path}") from error
    return digest.hexdigest()


def _validate_build_evidence_archive(
    result_root: Path,
    build_provenance: Mapping[str, Any],
) -> None:
    """Re-hash the exact root archive using only provenance row structure."""

    evidence_root = Path(result_root) / BUILD_EVIDENCE_DIRECTORY
    if evidence_root.is_symlink():
        _fail("build evidence root is a symlink")
    if not evidence_root.is_dir():
        _fail("build evidence root is absent")
    actual_groups = {path.name: path for path in evidence_root.iterdir()}
    expected_group_names = set(_BUILD_EVIDENCE_GROUPS.values())
    if set(actual_groups) != expected_group_names:
        _fail("build evidence contains unexpected root entries")
    for provenance_name, directory_name in _BUILD_EVIDENCE_GROUPS.items():
        raw_rows = _mapping(
            build_provenance.get(provenance_name),
            f"build evidence {provenance_name}",
        )
        directory = actual_groups[directory_name]
        if directory.is_symlink():
            _fail(f"build evidence contains a symlink: {directory_name}")
        if not directory.is_dir():
            _fail(f"build evidence group is not a directory: {directory_name}")
        actual = {path.name: path for path in directory.iterdir()}
        if set(actual) != set(raw_rows):
            _fail(f"build evidence {directory_name} membership drifted or is unexpected")
        for raw_name, raw_row in raw_rows.items():
            name = _string(raw_name, f"build evidence {directory_name} name")
            if name in {".", ".."} or "/" in name or "\\" in name:
                _fail(f"build evidence name is not canonical: {name}")
            row = _mapping(raw_row, f"build evidence {directory_name}/{name}")
            _fields(
                row,
                {"path", "size_bytes", "sha256"},
                f"build evidence {directory_name}/{name}",
            )
            path = actual[name]
            if path.is_symlink():
                _fail(f"build evidence contains a symlink: {directory_name}/{name}")
            try:
                file_size = path.stat().st_size
            except OSError as error:
                raise _Reject(
                    f"cannot inspect build evidence {directory_name}/{name}"
                ) from error
            if not path.is_file():
                _fail(f"build evidence is not a regular file: {directory_name}/{name}")
            if (
                file_size
                != _integer(
                    row.get("size_bytes"),
                    f"build evidence {directory_name}/{name} size",
                    1,
                )
                or _file_sha256(path)
                != _digest(
                    row.get("sha256"),
                    f"build evidence {directory_name}/{name} digest",
                )
            ):
                _fail(f"build evidence bytes drifted: {directory_name}/{name}")


def _validate_outcome(
    slot_root: Path, document: Mapping[str, Any], slot_id: str
) -> tuple[str, str | None]:
    _fields(document, {"schema_version", "slot_id", "history", "sealed_files"}, OUTCOME_FILENAME)
    if document["schema_version"] != 1 or document["slot_id"] != slot_id:
        _fail("outcome identity drifted")
    history = _array(document["history"], "outcome.history")
    if not 1 <= len(history) <= 2:
        _fail("outcome history must contain NOT_STARTED and at most one terminal")
    terminal_state = "NOT_STARTED"
    terminal_reason: str | None = None
    for index, raw in enumerate(history):
        item = _mapping(raw, f"outcome.history[{index}]")
        _fields(item, {"sequence", "state", "reason"}, f"outcome.history[{index}]")
        if item["sequence"] != index or item["state"] not in OUTCOMES:
            _fail("outcome history sequence/state is invalid")
        reason = item["reason"]
        if reason is not None and (not isinstance(reason, str) or not reason):
            _fail("outcome history reason must be null or non-empty text")
        if index == 0 and (item["state"] != "NOT_STARTED" or reason is not None):
            _fail("outcome history must start with NOT_STARTED and null reason")
        if index == 1:
            if item["state"] == "NOT_STARTED":
                _fail("outcome terminal cannot repeat NOT_STARTED")
            if item["state"] == "PASS" and reason is not None:
                _fail("PASS outcome cannot carry a failure reason")
            if item["state"] != "PASS" and reason is None:
                _fail("FAIL/INCOMPLETE outcome requires a reason")
            terminal_state, terminal_reason = item["state"], reason
    sealed = _mapping(document["sealed_files"], "outcome.sealed_files")
    actual: dict[str, str] = {}
    for path in slot_root.rglob("*"):
        if path.is_symlink():
            _fail(f"slot artifact tree contains a symlink: {path.relative_to(slot_root)}")
        if not path.is_file() or path.name == OUTCOME_FILENAME and path.parent == slot_root:
            continue
        relative = path.relative_to(slot_root).as_posix()
        actual[relative] = _file_sha256(path)
    if set(sealed) != set(actual):
        _fail("outcome seal does not cover every and only preserved regular file")
    for relative, digest in sealed.items():
        if _digest(digest, f"outcome.sealed_files[{relative}]") != actual[relative]:
            _fail(f"outcome seal digest mismatch for {relative}")
    return terminal_state, terminal_reason


def _validate_execution_authorization(
    slot_root: Path,
    *,
    manifest: FrozenFactorialManifest,
    expected: _ExpectedSlot,
    plan: Mapping[str, Any],
    runtime_sha256: str,
    campaign_member: bool,
) -> tuple[dict[str, Any], bytes]:
    identity = _frozen_artifact_identity(manifest.manifest_id)
    loaded = _read_json(slot_root, AUTHORIZATION_FILENAME)
    assert loaded is not None
    document, payload = loaded
    fields = {
        "schema_version",
        "scope",
        "authorized_by",
        "approval_reference",
        "approved_utc",
        "kauri_revision",
        "slot_ids",
        "static_artifacts_sha256",
        "build_provenance_sha256",
        "automatic_retries",
        "replacement_policy",
        "authorization_id",
    }
    _fields(document, fields, AUTHORIZATION_FILENAME)
    planned_ids = [
        _string(_mapping(item, "plan slot").get("slot_id"), "plan slot ID")
        for item in _array(plan.get("slots"), "plan slots")
    ]
    expected_scope = "shape25_campaign" if campaign_member else "excluded_n7_smoke"
    expected_slots = planned_ids if campaign_member else [expected.slot_id]
    static_hashes = _mapping(
        document["static_artifacts_sha256"], "authorization static hashes"
    )
    _digest(
        document["build_provenance_sha256"],
        "authorization build provenance digest",
    )
    if dict(static_hashes) != {
        MANIFEST_FILENAME: identity.manifest_sha256,
        PLAN_FILENAME: identity.plan_sha256,
        RUNTIME_FILENAME: runtime_sha256,
    }:
        _fail("execution authorization is not bound to the exact static artifacts")
    approval_reference = _string(
        document["approval_reference"], "authorization approval reference"
    )
    if len(approval_reference) > 512 or approval_reference != approval_reference.strip():
        _fail("execution authorization approval reference is not canonical")
    approved_utc = _string(document["approved_utc"], "authorization timestamp")
    try:
        approved = dt.datetime.fromisoformat(approved_utc.replace("Z", "+00:00"))
    except ValueError as error:
        raise _Reject("execution authorization timestamp is invalid") from error
    revision = _string(document["kauri_revision"], "authorization revision")
    if _HEX40.fullmatch(revision) is None:
        _fail("execution authorization revision is not a full Git identity")
    core = {key: value for key, value in document.items() if key != "authorization_id"}
    expected_id = "execution-authorization-" + _sha256(
        _canonical_json_bytes(core)
    )[:24]
    if (
        document["schema_version"] != 1
        or document["scope"] != expected_scope
        or document["authorized_by"] != "thesis_author"
        or approved.tzinfo is None
        or approved.utcoffset() is None
        or document["slot_ids"] != expected_slots
        or document["automatic_retries"] != 0
        or document["replacement_policy"] != "none"
        or document["authorization_id"] != expected_id
    ):
        _fail("execution authorization receipt is not exact")
    if not campaign_member:
        root_authorization = _read_bytes(
            slot_root.parent,
            SMOKE_AUTHORIZATION_FILENAME,
        )
        assert root_authorization is not None
        if root_authorization != payload:
            _fail("smoke root authorization differs from the slot authorization")
    return document, payload


def _validate_build_provenance(
    slot_root: Path,
    *,
    revision: str,
    recorded_repository: Path,
) -> tuple[dict[str, Any], bytes]:
    loaded = _read_json(slot_root, BUILD_PROVENANCE_FILENAME)
    assert loaded is not None
    document, payload = loaded
    _fields(
        document,
        {
            "schema_version",
            "revision",
            "repository",
            "build_directory",
            "cmake_cache_sha256",
            "build_command",
            "build_metadata",
            "binaries",
        },
        BUILD_PROVENANCE_FILENAME,
    )
    repository = recorded_repository
    build_directory = recorded_repository / "build-adaptive"
    expected_binary_paths = {
        "app": build_directory / "examples/hotstuff-app",
        "manager": build_directory / "examples/adaptation-manager",
        "keygen": build_directory / "hotstuff-keygen",
        "tls_keygen": build_directory / "hotstuff-tls-keygen",
        "epoch_profile_digest": build_directory / "examples/epoch-profile-digest",
    }
    expected_metadata_paths = {
        "adaptation_manager_link": build_directory
        / "examples/CMakeFiles/adaptation-manager.dir/link.txt",
        "cmake_cache": build_directory / "CMakeCache.txt",
        "compile_commands": build_directory / "compile_commands.json",
        "epoch_profile_digest_link": build_directory
        / "examples/CMakeFiles/epoch-profile-digest.dir/link.txt",
        "hotstuff_app_link": build_directory
        / "examples/CMakeFiles/hotstuff-app.dir/link.txt",
        "hotstuff_keygen_link": build_directory
        / "CMakeFiles/hotstuff-keygen.dir/link.txt",
        "hotstuff_tls_keygen_link": build_directory
        / "CMakeFiles/hotstuff-tls-keygen.dir/link.txt",
    }
    expected_command = [
        "cmake",
        "--build",
        str(build_directory),
        "--clean-first",
        "--parallel",
        "4",
        "--target",
        "hotstuff-app",
        "adaptation-manager",
        "hotstuff-keygen",
        "hotstuff-tls-keygen",
        "epoch-profile-digest",
    ]
    if (
        document["schema_version"] != 1
        or document["revision"] != revision
        or document["repository"] != str(repository)
        or document["build_directory"] != str(build_directory)
        or document["build_command"] != expected_command
    ):
        _fail("exact-build provenance identity drifted")
    cache_digest = _digest(
        document["cmake_cache_sha256"], "build provenance CMake cache digest"
    )

    def validate_rows(
        raw: object,
        expected_paths: Mapping[str, Path],
        label: str,
    ) -> dict[str, Mapping[str, Any]]:
        rows = _mapping(raw, label)
        if set(rows) != set(expected_paths):
            _fail(f"{label} membership drifted")
        result: dict[str, Mapping[str, Any]] = {}
        for name, expected_path in expected_paths.items():
            row = _mapping(rows[name], f"{label}.{name}")
            _fields(row, {"path", "size_bytes", "sha256"}, f"{label}.{name}")
            if (
                row["path"] != str(expected_path)
                or _integer(row["size_bytes"], f"{label}.{name}.size", 1) <= 0
            ):
                _fail(f"{label}.{name} identity drifted")
            _digest(row["sha256"], f"{label}.{name}.sha256")
            result[name] = row
        return result

    metadata = validate_rows(
        document["build_metadata"], expected_metadata_paths, "build metadata"
    )
    validate_rows(document["binaries"], expected_binary_paths, "build binaries")
    if metadata["cmake_cache"]["sha256"] != cache_digest:
        _fail("build provenance CMake cache hashes disagree")
    _validate_build_evidence_archive(slot_root.parent, document)
    return document, payload


def _validate_execution_provenance(
    slot_root: Path,
    *,
    recorded_result_root: Path,
    expected: _ExpectedSlot,
    campaign_member: bool,
    receipt: Mapping[str, Any],
    receipt_bytes: bytes,
    authorization: Mapping[str, Any],
    authorization_bytes: bytes,
    build_provenance: Mapping[str, Any],
    build_provenance_bytes: bytes,
) -> None:
    loaded = _read_json(slot_root, EXECUTION_PROVENANCE_FILENAME)
    assert loaded is not None
    document, _ = loaded
    _fields(
        document,
        {
            "schema_version",
            "slot_id",
            "campaign_member",
            "figure_eligible",
            "denominator_contribution",
            "kauri_revision",
            "build_provenance_sha256",
            "execution_authorization_id",
            "execution_authorization_sha256",
            "binaries",
            "input_artifacts",
            "launch_binding",
            "slot_receipt_sha256",
            "attempt_ordinal",
            "automatic_retries",
            "replacement_policy",
        },
        EXECUTION_PROVENANCE_FILENAME,
    )
    revision = authorization["kauri_revision"]
    if (
        document["schema_version"] != 1
        or document["slot_id"] != expected.slot_id
        or document["campaign_member"] is not campaign_member
        or document["figure_eligible"] is not campaign_member
        or document["denominator_contribution"] != (1 if campaign_member else 0)
        or document["kauri_revision"] != revision
        or document["build_provenance_sha256"] != _sha256(build_provenance_bytes)
        or document["execution_authorization_id"]
        != authorization["authorization_id"]
        or document["execution_authorization_sha256"]
        != _sha256(authorization_bytes)
        or document["slot_receipt_sha256"] != _sha256(receipt_bytes)
        or document["attempt_ordinal"] != 1
        or document["automatic_retries"] != 0
        or document["replacement_policy"] != "none"
        or build_provenance["revision"] != revision
    ):
        _fail("execution provenance identity/lifecycle contract drifted")
    binaries = _mapping(document["binaries"], "execution binaries")
    build_binaries = _mapping(build_provenance["binaries"], "build binaries")
    expected_binary_names = {"app", "manager", "keygen", "tls_keygen"}
    if set(binaries) != expected_binary_names:
        _fail("execution binary membership drifted")
    for name in expected_binary_names:
        row = _mapping(binaries[name], f"execution binary {name}")
        _fields(row, {"path", "size_bytes", "sha256"}, f"execution binary {name}")
        build_row = _mapping(build_binaries[name], f"build binary {name}")
        expected_path = (
            recorded_result_root / BUILD_EVIDENCE_DIRECTORY / "binaries" / name
        )
        if (
            row["path"] != str(expected_path)
            or row["size_bytes"] != build_row["size_bytes"]
            or row["sha256"] != build_row["sha256"]
        ):
            _fail(f"execution binary differs from exact build: {name}")

    raw_inputs = _array(document["input_artifacts"], "execution input artifacts")
    input_rows: list[Mapping[str, Any]] = []
    by_path: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(raw_inputs):
        row = _mapping(raw, f"execution input artifact {index}")
        _fields(
            row,
            {"kind", "replica_id", "relative_path", "sha256", "size_bytes"},
            f"execution input artifact {index}",
        )
        relative = _string(row["relative_path"], "execution input path")
        if relative in by_path:
            _fail("execution input artifacts contain a duplicate path")
        path = _safe_file(slot_root, relative)
        assert path is not None
        if (
            _digest(row["sha256"], f"execution input {relative} digest")
            != _file_sha256(path)
            or _integer(row["size_bytes"], f"execution input {relative} size", 1)
            != path.stat().st_size
        ):
            _fail(f"execution input artifact bytes drifted: {relative}")
        by_path[relative] = row
        input_rows.append(row)
    expected_paths = {
        "runtime/main.conf",
        "runtime/bls-identities.txt",
        "runtime/tls-identities.txt",
        "runtime/issuer-identities.txt",
        *(f"runtime/replica-{replica_id}.conf" for replica_id in range(expected.replica_count)),
    }
    if set(by_path) != expected_paths:
        _fail("execution input artifact membership drifted")
    for relative, row in by_path.items():
        if relative == "runtime/main.conf":
            expected_kind, expected_replica = "main_config", None
        elif relative.endswith("-identities.txt"):
            expected_kind = relative.removeprefix("runtime/").removesuffix(
                "-identities.txt"
            ) + "_identity_input"
            expected_replica = None
        else:
            match = re.fullmatch(r"runtime/replica-(\d+)\.conf", relative)
            if match is None:
                _fail("execution input path is not recognized")
            expected_kind, expected_replica = "replica_config", int(match.group(1))
        if row["kind"] != expected_kind or row["replica_id"] != expected_replica:
            _fail(f"execution input artifact metadata drifted: {relative}")

    binding = _mapping(document["launch_binding"], "launch binding")
    _fields(binding, {"algorithm", "canonicalization", "payload", "sha256"}, "launch binding")
    binding_payload = _mapping(binding["payload"], "launch binding payload")
    expected_input_hashes = [
        {"relative_path": row["relative_path"], "sha256": row["sha256"]}
        for row in input_rows
    ]
    expected_executables = {
        name: {
            "path": _mapping(binaries[name], f"execution binary {name}")["path"],
            "sha256": _mapping(binaries[name], f"execution binary {name}")[
                "sha256"
            ],
        }
        for name in sorted(expected_binary_names)
    }
    if (
        binding["algorithm"] != "sha256"
        or binding["canonicalization"]
        != "sorted_key_compact_json_utf8_lf_v1"
        or dict(binding_payload)
        != {
            "redacted_receipt_sha256": _sha256(_canonical_json_bytes(receipt)),
            "input_hashes": expected_input_hashes,
            "executables": expected_executables,
        }
        or binding["sha256"] != _sha256(_canonical_json_bytes(binding_payload))
    ):
        _fail("execution launch binding does not independently recompute")


def _validate_runner_state(
    slot_root: Path,
    *,
    expected: _ExpectedSlot,
    anchor_ns: int,
) -> None:
    payload = _read_bytes(slot_root, RUNNER_STATE_FILENAME)
    assert payload is not None
    if not payload.endswith(b"\n"):
        _fail("runner state has a partial final record")
    rows: list[Mapping[str, Any]] = []
    for index, line in enumerate(payload.splitlines(keepends=True)):
        row = _parse_json_bytes(line, f"{RUNNER_STATE_FILENAME}:{index + 1}")
        if line != _canonical_json_bytes(row):
            _fail("runner state record is not canonical JSONL")
        rows.append(row)
    expected_phases = (
        "identity_generation",
        "launch",
        "observing",
        "qualified_pending_cleanup",
        "terminal",
    )
    if len(rows) != len(expected_phases):
        _fail("runner state does not contain the exact one-shot lifecycle")
    timestamps: list[int] = []
    for phase, row in zip(expected_phases, rows):
        base_fields = {
            "schema_version",
            "slot_id",
            "phase",
            "recorded_utc",
            "recorded_monotonic_ns",
        }
        expected_fields = set(base_fields)
        if phase == "launch":
            expected_fields.add("shared_raw_clock_anchor_ns")
        elif phase == "observing":
            expected_fields.add("launch_count")
        elif phase == "terminal":
            expected_fields.update({"outcome", "reason"})
        _fields(row, expected_fields, f"runner state {phase}")
        timestamp = _integer(
            row["recorded_monotonic_ns"], f"runner state {phase} timestamp", 1
        )
        timestamps.append(timestamp)
        if (
            row["schema_version"] != 1
            or row["slot_id"] != expected.slot_id
            or row["phase"] != phase
            or not isinstance(row["recorded_utc"], str)
            or not row["recorded_utc"]
        ):
            _fail("runner state identity/order drifted")
    if timestamps != sorted(timestamps):
        _fail("runner state timestamps are not monotonic")
    if (
        rows[1]["shared_raw_clock_anchor_ns"] != anchor_ns
        or rows[2]["launch_count"] != expected.replica_count + 1
        or rows[4]["outcome"] != "PASS"
        or rows[4]["reason"] is not None
    ):
        _fail("runner state launch cardinality or terminal outcome drifted")


def _validate_cleanup_ledger(
    slot_root: Path,
    *,
    expected: _ExpectedSlot,
    manager_events: Sequence[_NativeEvent],
    drain_complete_ns: int,
) -> None:
    loaded = _read_json(slot_root, CLEANUP_LEDGER_FILENAME)
    assert loaded is not None
    document, _ = loaded
    _fields(
        document,
        {
            "schema_version",
            "slot_id",
            "cleanup_started_monotonic_ns",
            "cleanup_completed",
            "streams_closed",
            "ports_clear",
            "final_streams_complete",
            "processes",
            "error",
        },
        CLEANUP_LEDGER_FILENAME,
    )
    cleanup_started = _integer(
        document["cleanup_started_monotonic_ns"], "cleanup start", 1
    )
    if (
        document["schema_version"] != 1
        or document["slot_id"] != expected.slot_id
        or cleanup_started < drain_complete_ns
        or document["cleanup_completed"] is not True
        or document["streams_closed"] is not True
        or document["ports_clear"] is not True
        or document["final_streams_complete"] is not True
        or document["error"] is not None
    ):
        _fail("cleanup ledger does not prove a complete clean one-shot lifecycle")

    terminal_events = [
        event
        for event in manager_events
        if event.event_type == "adaptive_v2_session_terminal"
        and event.payload.get("cycle_ordinal") == 1
        and event.payload.get("outcome") == "advanced"
        and event.payload.get("reason") == "successor_converged"
    ]
    if len(terminal_events) != 1:
        _fail("cleanup ledger lacks one exact cycle-2 manager exit authorization")
    terminal = terminal_events[0]
    expected_exit_authorization = {
        "relative_path": terminal.relative_path,
        "line_number": terminal.line_number,
        "source_id": terminal.source_id,
        "source_sequence": terminal.source_sequence,
        "source_monotonic_ns": terminal.monotonic_ns,
        "event_type": terminal.event_type,
        "line_sha256": terminal.line_sha256,
    }
    rows = _array(document["processes"], "cleanup processes")
    if len(rows) != expected.replica_count + 1:
        _fail("cleanup ledger process cardinality drifted")
    expected_names = {"adaptive-manager", *(
        f"replica-{replica_id}" for replica_id in range(expected.replica_count)
    )}
    by_name: dict[str, Mapping[str, Any]] = {}
    pids: set[int] = set()
    for index, raw in enumerate(rows):
        row = _mapping(raw, f"cleanup process {index}")
        _fields(
            row,
            {
                "name",
                "replica_id",
                "pid",
                "pgid",
                "cleanup_started_monotonic_ns",
                "signal_number",
                "returncode",
                "classification",
                "exit_authorization",
            },
            f"cleanup process {index}",
        )
        name = _string(row["name"], "cleanup process name")
        pid = _integer(row["pid"], f"cleanup {name} pid", 1)
        pgid = _integer(row["pgid"], f"cleanup {name} pgid", 1)
        if (
            name in by_name
            or pid in pids
            or pid != pgid
            or row["cleanup_started_monotonic_ns"] != cleanup_started
            or type(row["returncode"]) is not int
        ):
            _fail("cleanup ledger process ownership/identity drifted")
        by_name[name] = row
        pids.add(pid)
    if set(by_name) != expected_names:
        _fail("cleanup ledger process names drifted")

    def credible_cleanup_exit(row: Mapping[str, Any]) -> bool:
        signal_number = row["signal_number"]
        returncode = row["returncode"]
        if signal_number in (signal.SIGINT, signal.SIGTERM):
            return returncode in (0, -signal_number)
        if signal_number == signal.SIGKILL:
            return returncode == -signal.SIGKILL
        return False

    for replica_id in range(expected.replica_count):
        row = by_name[f"replica-{replica_id}"]
        if (
            row["replica_id"] != replica_id
            or row["classification"] != "expected_cleanup"
            or row["exit_authorization"] is not None
            or not credible_cleanup_exit(row)
        ):
            _fail("cleanup ledger replica lifecycle drifted")
    manager = by_name["adaptive-manager"]
    if manager["replica_id"] is not None:
        _fail("cleanup ledger manager replica identity drifted")
    if manager["classification"] == "expected_clean_exit":
        if (
            manager["returncode"] != 0
            or manager["signal_number"] is not None
            or manager["exit_authorization"] != expected_exit_authorization
        ):
            _fail("manager clean exit lacks exact native terminal authorization")
    elif manager["classification"] == "expected_cleanup":
        if (
            manager["exit_authorization"] is not None
            or not credible_cleanup_exit(manager)
        ):
            _fail("cleaned-up manager carries an invalid exit authorization")
    else:
        _fail("cleanup ledger manager lifecycle is not accepted")


def _validate_phase_cutoffs(
    document: Mapping[str, Any],
    *,
    slot_id: str,
    slot_receipt_sha256: str,
    window_start_ns: int,
    window_end_ns: int,
    bucket_width_s: int,
    bucket_counts: Mapping[str, int],
    events_by_ref: Mapping[tuple[str, int], _NativeEvent],
    manager_events: Sequence[_NativeEvent],
    replica_events: Mapping[int, Sequence[_NativeEvent]],
    replica_count: int,
    quorum: int,
    drain_margin_s: int,
) -> tuple[dict[str, int], dict[str, tuple[int, int, int]]]:
    _fields(
        document,
        {
            "schema_version",
            "slot_id",
            "cutoff_rule",
            "cutoffs",
            "phases",
            "phase_qualifications",
        },
        PHASE_CUTOFFS_FILENAME,
    )
    if (
        document["schema_version"] != 1
        or document["slot_id"] != slot_id
        or document["cutoff_rule"] != CUTOFF_RULE
    ):
        _fail("phase cutoff document identity/rule drifted")
    expected_types = {
        "baseline_stable": "block.committed",
        "fault_window_open": "fault_window.open",
        "epoch1_command": "epoch.command_committed",
        "epoch1_activation": "epoch.activated",
        "epoch1_stable": "block.committed",
        "shape_v1_computed": "adaptive_v2_shape_decision",
        "epoch2_command": "epoch.command_committed",
        "epoch2_activation": "epoch.activated",
        "epoch2_stable": "block.committed",
        "epoch2_drain_complete": "block.committed",
    }
    cutoff_rows = _array(document["cutoffs"], "phase-cutoffs.cutoffs")
    if len(cutoff_rows) != len(CUTOFF_NAMES):
        _fail("phase cutoff document does not contain every cutoff exactly once")
    times: dict[str, int] = {}
    cutoff_events: dict[str, _NativeEvent] = {}
    for index, raw in enumerate(cutoff_rows):
        row = _mapping(raw, f"phase-cutoffs.cutoffs[{index}]")
        _fields(
            row,
            {"name", "source_path", "source_sequence", "event_type", "source_monotonic_ns", "event_sha256"},
            f"phase-cutoffs.cutoffs[{index}]",
        )
        name = _string(row["name"], "cutoff name")
        if name != CUTOFF_NAMES[index] or row["event_type"] != expected_types[name]:
            _fail("phase cutoffs are not in the frozen semantic order")
        timestamp = _integer(row["source_monotonic_ns"], f"cutoff {name} timestamp", 1)
        source_path = _string(row["source_path"], f"cutoff {name} source_path")
        sequence = _integer(row["source_sequence"], f"cutoff {name} source_sequence")
        digest = _digest(row["event_sha256"], f"cutoff {name} event_sha256")
        if name == "fault_window_open":
            if not (
                source_path == SLOT_FILENAME
                and sequence == 0
                and timestamp == window_start_ns
                and digest == slot_receipt_sha256
            ):
                _fail("fault-window cutoff is not bound to the slot clock receipt")
        else:
            event = events_by_ref.get((source_path, sequence))
            if event is None or (
                event.event_type != row["event_type"]
                or event.monotonic_ns != timestamp
                or event.line_sha256 != digest
            ):
                _fail(f"cutoff {name} does not reference one exact native event")
            cutoff_events[name] = event
        times[name] = timestamp
    if tuple(times[name] for name in CUTOFF_NAMES) != tuple(
        sorted(times[name] for name in CUTOFF_NAMES)
    ):
        _fail("live phase cutoffs are not monotonically ordered")

    phases = _array(document["phases"], "phase-cutoffs.phases")
    if len(phases) != len(PHASES):
        _fail("phase cutoff document does not contain every phase")
    windows: dict[str, tuple[int, int, int]] = {}
    phase_configurations: dict[str, tuple[int, str]] = {}
    for index, raw in enumerate(phases):
        phase = _mapping(raw, f"phase-cutoffs.phases[{index}]")
        _fields(
            phase,
            {
                "phase",
                "start_monotonic_ns",
                "end_monotonic_ns",
                "bucket_count",
                "configuration",
            },
            f"phase-cutoffs.phases[{index}]",
        )
        name = _string(phase["phase"], "phase name")
        if name != PHASES[index] or name in windows:
            _fail("phase windows are duplicated or out of frozen order")
        start = _integer(phase["start_monotonic_ns"], f"phase {name} start", 1)
        end = _integer(phase["end_monotonic_ns"], f"phase {name} end", 1)
        count = _integer(phase["bucket_count"], f"phase {name} bucket_count", 1)
        if count != bucket_counts[name] or end - start != count * bucket_width_s * _NANOSECONDS_PER_SECOND:
            _fail(f"phase {name} window does not have the fixed duration")
        configuration = _mapping(
            phase["configuration"], f"phase-cutoffs.{name}.configuration"
        )
        _fields(
            configuration,
            {"epoch_number", "epoch_digest"},
            f"phase-cutoffs.{name}.configuration",
        )
        phase_configurations[name] = (
            _integer(
                configuration["epoch_number"],
                f"phase-cutoffs.{name}.configuration.epoch_number",
            ),
            _digest(
                configuration["epoch_digest"],
                f"phase-cutoffs.{name}.configuration.epoch_digest",
            ),
        )
        windows[name] = (start, end, count)
    width_ns = bucket_width_s * _NANOSECONDS_PER_SECOND
    if not (
        windows["baseline"][1] == times["baseline_stable"]
        and windows["baseline"][1] < times["fault_window_open"]
        and windows["fault_evidence"][0] == times["fault_window_open"]
        and windows["fault_evidence"][1] <= times["epoch1_command"]
        and windows["epoch1_stable"][0] == times["epoch1_stable"]
        and windows["epoch1_stable"][1] <= times["shape_v1_computed"]
        and windows["epoch2_stable"][0] == times["epoch2_stable"]
        and windows["epoch2_stable"][1] <= times["epoch2_drain_complete"]
    ):
        _fail("phase windows are not bounded by their exact live cutoffs")
    if not (
        windows["epoch2_stable"][1] < window_end_ns
        and times["epoch2_drain_complete"] < window_end_ns
    ):
        _fail("Epoch2 stable measurement and drain did not finish during the fault window")

    if set(replica_events) != set(range(replica_count)) or not 0 < quorum <= replica_count:
        _fail("phase replay does not cover the exact replica membership/quorum")
    readiness_streams: list[tuple[str, Sequence[_NativeEvent]]] = [
        ("adaptive-manager", manager_events),
        *(
            (f"replica-{replica_id}", replica_events[replica_id])
            for replica_id in range(replica_count)
        ),
    ]
    ready_times: list[int] = []
    for source, stream in readiness_streams:
        ready = [event for event in stream if event.event_type == "process.ready"]
        if len(ready) != 1:
            _fail(f"{source} must emit exactly one process.ready event")
        ready_times.append(ready[0].monotonic_ns)
    if max(ready_times) > windows["baseline"][0]:
        _fail("baseline measurement began before every process was ready")

    observer_events = tuple(replica_events[0])

    def reference(event: _NativeEvent) -> dict[str, object]:
        return {
            "relative_path": event.relative_path,
            "line_number": event.line_number,
            "source_id": event.source_id,
            "source_sequence": event.source_sequence,
            "source_monotonic_ns": event.monotonic_ns,
            "event_type": event.event_type,
            "line_sha256": event.line_sha256,
        }

    def commit_key(event: _NativeEvent, *, authoritative: bool) -> tuple[object, ...]:
        commit = _commit_payload(event, authoritative=authoritative)
        return (
            commit["height"],
            commit["hash"],
            commit["parent"],
            commit["transactions"],
            commit["batch_index"],
        )

    def commit_has_configuration(
        event: _NativeEvent,
        expected_configuration: tuple[int, str],
        allowed_tree_ids: Collection[int],
    ) -> bool:
        commit = _commit_payload(event, authoritative=True)
        return (
            commit["epoch_number"] == expected_configuration[0]
            and commit["epoch_digest"] == expected_configuration[1]
            and commit["tree_id"] in allowed_tree_ids
        )

    def require_window_configuration(
        phase_name: str,
        expected_configuration: tuple[int, str],
        allowed_tree_ids: Collection[int],
    ) -> None:
        start_ns, end_ns, _ = windows[phase_name]
        for event in observer_events:
            if (
                event.event_type == "block.committed"
                and start_ns <= event.monotonic_ns < end_ns
                and not commit_has_configuration(
                    event, expected_configuration, allowed_tree_ids
                )
            ):
                _fail(
                    f"{phase_name} contains an authoritative commit outside its "
                    "configuration-bound identity"
                )

    def common_commit(
        start_ns: int,
        end_ns: int,
        *,
        expected_configuration: tuple[int, str],
        allowed_tree_ids: Collection[int],
    ) -> tuple[_NativeEvent, tuple[_NativeEvent, ...], dict[str, object]] | None:
        observations: dict[int, dict[tuple[object, ...], _NativeEvent]] = {}
        for replica_id in range(quorum):
            indexed: dict[tuple[object, ...], _NativeEvent] = {}
            for event in replica_events[replica_id]:
                if (
                    event.event_type != "block.commit_observed"
                    or not start_ns <= event.monotonic_ns < end_ns
                ):
                    continue
                key = commit_key(event, authoritative=False)
                if key in indexed:
                    _fail("common-commit witness stream duplicates one commit identity")
                indexed[key] = event
            observations[replica_id] = indexed
        for event in observer_events:
            if (
                event.event_type != "block.committed"
                or not start_ns <= event.monotonic_ns < end_ns
                or not commit_has_configuration(
                    event, expected_configuration, allowed_tree_ids
                )
            ):
                continue
            key = commit_key(event, authoritative=True)
            authoritative_commit = _commit_payload(event, authoritative=True)
            witnesses = tuple(
                observations[replica_id].get(key) for replica_id in range(quorum)
            )
            if any(witness is None for witness in witnesses):
                continue
            exact = tuple(witness for witness in witnesses if witness is not None)
            proof = {
                "identity": {
                    "block_height": key[0],
                    "block_hash": key[1],
                    "parent_hash": key[2],
                    "transaction_count": key[3],
                    "decision_proof": {
                        "epoch_number": authoritative_commit["epoch_number"],
                        "tree_id": authoritative_commit["tree_id"],
                        "epoch_digest": authoritative_commit["epoch_digest"],
                        "block_hash": authoritative_commit["hash"],
                    },
                },
                "observer": reference(event),
                "witnesses": [reference(witness) for witness in exact],
                "common_monotonic_ns": max(
                    event.monotonic_ns,
                    *(witness.monotonic_ns for witness in exact),
                ),
            }
            return event, exact, proof
        return None

    duration = bucket_counts["baseline"] * width_ns
    baseline_configuration = phase_configurations["baseline"]
    baseline_tree_ids = frozenset(range(replica_count))
    successor_tree_ids = frozenset(range(quorum))
    if phase_configurations["fault_evidence"] != baseline_configuration:
        _fail("baseline and fault-evidence phases do not share one configuration")
    baseline_candidates = sorted(
        (
            event
            for event in observer_events
            if event.event_type == "block.committed"
            and max(ready_times) + duration <= event.monotonic_ns < window_start_ns
            and commit_has_configuration(
                event, baseline_configuration, baseline_tree_ids
            )
        ),
        key=lambda event: event.monotonic_ns,
        reverse=True,
    )
    expected_baseline: tuple[
        _NativeEvent, tuple[_NativeEvent, ...], dict[str, object]
    ] | None = None
    expected_baseline_cutoff: _NativeEvent | None = None
    for candidate in baseline_candidates:
        proof = common_commit(
            candidate.monotonic_ns - duration,
            candidate.monotonic_ns,
            expected_configuration=baseline_configuration,
            allowed_tree_ids=baseline_tree_ids,
        )
        if proof is not None:
            expected_baseline_cutoff = candidate
            expected_baseline = proof
            break
    if expected_baseline is None or expected_baseline_cutoff is None:
        _fail("raw events do not independently qualify the frozen baseline")
    if reference(cutoff_events["baseline_stable"]) != reference(
        expected_baseline_cutoff
    ):
        _fail("baseline cutoff is not the latest independently qualified cutoff")

    require_window_configuration(
        "baseline", baseline_configuration, baseline_tree_ids
    )
    require_window_configuration(
        "fault_evidence", baseline_configuration, baseline_tree_ids
    )
    if not any(
        event.event_type == "block.committed"
        and windows["fault_evidence"][0]
        <= event.monotonic_ns
        < windows["fault_evidence"][1]
        and commit_has_configuration(
            event, baseline_configuration, baseline_tree_ids
        )
        for event in observer_events
    ):
        _fail("fault-evidence window contains no authoritative commit")

    stable_proofs: dict[str, dict[str, object]] = {
        "baseline": expected_baseline[2]
    }
    for epoch, phase_name in ((1, "epoch1_stable"), (2, "epoch2_stable")):
        activation_name = f"epoch{epoch}_activation"
        activation = _activation_identity(
            cutoff_events[activation_name].payload,
            f"{activation_name}.payload",
        )
        activated_identity = (
            activation["epoch_number"],
            activation["epoch_digest"],
        )
        expected_configuration = phase_configurations[phase_name]
        if activated_identity[0] != epoch or expected_configuration != activated_identity:
            _fail(
                f"{phase_name} does not use the exact activated configuration identity"
            )
        require_window_configuration(
            phase_name, expected_configuration, successor_tree_ids
        )
        if not commit_has_configuration(
            cutoff_events[phase_name],
            expected_configuration,
            successor_tree_ids,
        ):
            _fail(
                f"{phase_name} cutoff is outside its configuration-bound identity"
            )
        candidates = [
            event
            for event in observer_events
            if event.event_type == "block.committed"
            and event.monotonic_ns >= times[activation_name]
            and commit_has_configuration(
                event, expected_configuration, successor_tree_ids
            )
        ]
        if not candidates:
            _fail(f"raw events contain no Epoch{epoch} post-activation commit")
        first = min(candidates, key=lambda event: event.monotonic_ns)
        if reference(cutoff_events[phase_name]) != reference(first):
            _fail(
                f"{phase_name} cutoff is not the first authoritative post-activation commit"
            )
        proof = common_commit(
            windows[phase_name][0],
            windows[phase_name][1],
            expected_configuration=expected_configuration,
            allowed_tree_ids=successor_tree_ids,
        )
        if proof is None:
            _fail(f"{phase_name} lacks an exact common-Q commit")
        stable_proofs[phase_name] = proof[2]

    drain_not_before = windows["epoch2_stable"][1] + (
        drain_margin_s * _NANOSECONDS_PER_SECOND
    )
    drain_candidates = [
        event
        for event in observer_events
        if event.event_type == "block.committed"
        and event.monotonic_ns >= drain_not_before
        and commit_has_configuration(
            event,
            phase_configurations["epoch2_stable"],
            successor_tree_ids,
        )
    ]
    if not drain_candidates:
        _fail("raw events contain no authoritative post-drain commit")
    first_drain = min(drain_candidates, key=lambda event: event.monotonic_ns)
    if reference(cutoff_events["epoch2_drain_complete"]) != reference(first_drain):
        _fail("drain cutoff is not the first commit after the frozen drain margin")

    qualifications = _array(
        document["phase_qualifications"], "phase-cutoffs.phase_qualifications"
    )
    qualification_names = ("baseline", "epoch1_stable", "epoch2_stable")
    if len(qualifications) != len(qualification_names):
        _fail("phase qualifications do not cover all common-Q measurement windows")
    for index, phase_name in enumerate(qualification_names):
        row = _mapping(
            qualifications[index], f"phase-cutoffs.phase_qualifications[{index}]"
        )
        _fields(
            row,
            {"phase", "common_commit"},
            f"phase-cutoffs.phase_qualifications[{index}]",
        )
        if (
            row["phase"] != phase_name
            or dict(
                _mapping(
                    row["common_commit"],
                    f"phase qualification {phase_name} common commit",
                )
            )
            != stable_proofs[phase_name]
        ):
            _fail(f"{phase_name} common-Q qualification does not replay exactly")
    return times, windows


def _expected_snapshot_payload_observations(
    records: Sequence[_EvidenceRecord], cutoff: int
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in records:
        if record.ingestion_sequence > cutoff:
            continue
        item: dict[str, Any] = {
            "observation_id": record.observation_id,
            "ingestion_sequence": record.ingestion_sequence,
            "epoch_number": record.epoch_number,
            "epoch_digest": record.epoch_digest,
            "reporter_id": record.reporter_id,
            "target_id": record.target_id,
            "outcome": record.outcome,
        }
        if record.outcome != "timeout" and record.response_duration_us != 0:
            if record.response_duration_us > _UINT64_MAX // 1000:
                _fail("snapshot latency nanoseconds overflow")
            item["latency_ns"] = record.response_duration_us * 1000
        result.append(item)
    return result


_SNAPSHOT_AUDIT_COMMON_FIELDS = frozenset(
    {
        "cycle_ordinal",
        "policy_intent",
        "transition_artifact_id",
        "predecessor_epoch_number",
        "predecessor_epoch_digest",
        "activation_generation",
        "baseline_cutoff",
        "current_cutoff",
        "eligible_ranking",
    }
)
_COMPACT_SNAPSHOT_AUDIT_FIELDS = frozenset(
    {
        "schema_version",
        "full_prefix_snapshot_id",
        "evidence_snapshot_id",
        "accepted_prefix_count",
    }
)


def _validate_snapshot_audit_schema(
    snapshot: Mapping[str, Any],
    *,
    evidence_snapshot_format: str,
    label: str,
) -> bool:
    if evidence_snapshot_format == "digest_commitment_v2":
        compact = True
        expected = _SNAPSHOT_AUDIT_COMMON_FIELDS | _COMPACT_SNAPSHOT_AUDIT_FIELDS
    elif evidence_snapshot_format == "full_prefix_v1":
        compact = False
        expected = _SNAPSHOT_AUDIT_COMMON_FIELDS | {"observations"}
    else:
        _fail("manifest names an unsupported evidence snapshot format")
    _fields(snapshot, set(expected), label)
    if compact and snapshot["schema_version"] != 2:
        _fail(f"{label} compact schema version is not 2")
    return compact


def _validate_compact_snapshot_commitments(
    snapshot: Mapping[str, Any],
    *,
    accepted_prefix_count: int,
    current_cutoff: int,
    full_prefix_snapshot_id: str,
    evidence_snapshot_id: str,
) -> None:
    recorded_count = _integer(
        snapshot["accepted_prefix_count"],
        "snapshot accepted prefix count",
        1,
    )
    if recorded_count != accepted_prefix_count or recorded_count > current_cutoff:
        _fail("snapshot accepted prefix count differs from accepted raw evidence")
    if (
        _digest(
            snapshot["full_prefix_snapshot_id"],
            "snapshot full-prefix commitment",
        )
        != full_prefix_snapshot_id
        or _digest(
            snapshot["evidence_snapshot_id"],
            "snapshot selected-evidence commitment",
        )
        != evidence_snapshot_id
    ):
        _fail("compact snapshot commitments do not recompute from raw evidence")


def _expected_roots(
    *,
    predecessor_trees: Sequence[Tree],
    scores: Sequence[ReplicaScore],
    actor_ids: Sequence[int],
    tree_count: int,
    intent: str,
) -> tuple[int, ...]:
    actors = set(actor_ids)
    eligible = [
        score.replica_id
        for score in scores
        if score.eligible and score.replica_id not in actors
    ]
    if len(eligible) < tree_count:
        _fail("live ranking has insufficient responsive non-actor roots")
    if intent == "performance_optimization":
        return tuple(eligible[:tree_count])
    if intent != "fault_containment":
        _fail("transition policy intent is invalid")
    ordered_predecessor = tuple(sorted(predecessor_trees, key=lambda tree: tree.tree_id))
    if len(ordered_predecessor) < tree_count:
        _fail("predecessor does not expose the exact reference-tree prefix")
    baselines = tuple(tree.members[0] for tree in ordered_predecessor[:tree_count])
    reserved: set[int] = set()
    preserve_values: list[bool] = []
    for root in baselines:
        keep = root in eligible and root not in reserved
        preserve_values.append(keep)
        if keep:
            reserved.add(root)
    preserve = tuple(preserve_values)
    roots: list[int] = []
    used = set(reserved)
    for root, keep in zip(baselines, preserve):
        if keep:
            roots.append(root)
            continue
        replacement = next((candidate for candidate in eligible if candidate not in used), None)
        if replacement is None:
            _fail("containment roots cannot be filled from the live ranking")
        roots.append(replacement)
        used.add(replacement)
    return tuple(roots)


def _validate_successor_trees(
    bundle: DecodedBundle,
    *,
    expected: _ExpectedSlot,
    actor_ids: Sequence[int],
    expected_roots: Sequence[int],
    expected_fanout: int,
) -> None:
    membership = tuple(range(expected.replica_count))
    if (
        bundle.membership_digest != _membership_digest(membership)
        or bundle.generation_seed != _SNAPSHOT_SEED
        or bundle.policy_version != _PLACEMENT_POLICY_VERSION
        or len(bundle.trees) != expected.q
        or tuple(tree.tree_id for tree in bundle.trees) != tuple(range(expected.q))
        or tuple(tree.members[0] for tree in bundle.trees) != tuple(expected_roots)
    ):
        _fail("successor epoch identity/tree roots differ from live independent derivation")
    actors = tuple(sorted(actor_ids))
    for tree in bundle.trees:
        if (
            tuple(sorted(tree.members)) != membership
            or tree.fanout != expected_fanout
            or tree.pipeline_stretch != expected.pipeline_stretch
            or tree.wait_exempt != actors
        ):
            _fail("successor tree changed membership, Q, fanout, pipeline, or wait-exempt set")
        leaf_start = _first_leaf_index(len(tree.members), tree.fanout)
        for actor in actors:
            if tree.members.index(actor) < leaf_start:
                _fail("actor is wait-exempt but not a physical successor leaf")


def _activation_identity(payload: Mapping[str, Any], label: str) -> dict[str, Any]:
    """Decode the exact flat payload emitted by native epoch lifecycle events."""

    _fields(
        payload,
        {"epoch_number", "tree_id", "epoch_digest", "activation_height"},
        label,
    )
    return {
        "epoch_number": _integer(payload["epoch_number"], f"{label}.epoch_number"),
        "tree_id": _integer(payload["tree_id"], f"{label}.tree_id"),
        "epoch_digest": _digest(payload["epoch_digest"], f"{label}.epoch_digest"),
        "activation_height": _integer(
            payload["activation_height"], f"{label}.activation_height", 1
        ),
    }


def _record_activation_identity(
    activation: dict[str, Any],
    *,
    bundle: DecodedBundle,
    canonical_by_epoch: dict[int, dict[str, Any]],
) -> None:
    """Bind one activation to its bundle and the all-replica identity."""

    epoch_number = activation["epoch_number"]
    if activation["epoch_digest"] != bundle.epoch_digest:
        _fail("replica activated a digest different from the preserved bundle")
    if activation["tree_id"] not in {tree.tree_id for tree in bundle.trees}:
        _fail("replica activated a tree absent from the preserved bundle")
    canonical = canonical_by_epoch.setdefault(epoch_number, activation)
    if canonical != activation:
        _fail("replicas disagree on an epoch activation identity")


def _command_identity(payload: Mapping[str, Any], label: str) -> dict[str, Any]:
    _fields(
        payload,
        {
            "command_block_height",
            "command_block_hash",
            "payload_digest",
            "predecessor_epoch_number",
            "predecessor_epoch_digest",
            "successor_epoch_number",
            "successor_epoch_digest",
            "activation_delay_blocks",
            "activation_height",
        },
        label,
    )
    result = {
        "command_block_height": _integer(payload["command_block_height"], f"{label}.command_block_height", 1),
        "command_block_hash": _digest(payload["command_block_hash"], f"{label}.command_block_hash"),
        "payload_digest": _digest(payload["payload_digest"], f"{label}.payload_digest"),
        "predecessor_epoch_number": _integer(payload["predecessor_epoch_number"], f"{label}.predecessor_epoch_number"),
        "predecessor_epoch_digest": _digest(payload["predecessor_epoch_digest"], f"{label}.predecessor_epoch_digest"),
        "successor_epoch_number": _integer(payload["successor_epoch_number"], f"{label}.successor_epoch_number", 1),
        "successor_epoch_digest": _digest(payload["successor_epoch_digest"], f"{label}.successor_epoch_digest"),
        "activation_delay_blocks": _integer(payload["activation_delay_blocks"], f"{label}.activation_delay_blocks", 1),
        "activation_height": _integer(payload["activation_height"], f"{label}.activation_height", 1),
    }
    if result["activation_height"] != result["command_block_height"] + result["activation_delay_blocks"]:
        _fail(f"{label} activation height is not command height plus delay")
    return result


def _validate_transition_terminal_deadline(
    *,
    shape_ns: int,
    terminal_ns: int,
    observation_bound_rule: str,
    convergence_deadline_s: int,
) -> None:
    """Apply the post-selection deadline only to the v2 clock contract."""

    if observation_bound_rule == "phase_deadline_v1":
        return
    if observation_bound_rule != (
        "shared_slot_hard_deadline_until_manager_selection_v1"
    ):
        _fail("transition observation bound rule is unknown")
    if (
        convergence_deadline_s <= 0
        or not shape_ns <= terminal_ns
        <= shape_ns + convergence_deadline_s * _NANOSECONDS_PER_SECOND
    ):
        _fail("manager convergence exceeded its post-selection deadline")


def _validate_native_transitions(
    *,
    manager_events: Sequence[_NativeEvent],
    replica_events: Mapping[int, Sequence[_NativeEvent]],
    bundles: Sequence[DecodedBundle],
    initial_epoch_digest: str,
    expected: _ExpectedSlot,
    expected_activation_delay: int,
    expected_convergence_deadline_s: int,
    transition_observation_bound_rule: str,
    cutoff_times: Mapping[str, int],
) -> None:
    if len(bundles) != 2:
        _fail("slot does not contain exactly two decoded successor bundles")
    predecessor_digest = initial_epoch_digest
    command_times: dict[int, list[int]] = defaultdict(list)
    activation_times: dict[int, list[int]] = defaultdict(list)
    command_identity_by_epoch: dict[int, dict[str, Any]] = {}
    activation_identity_by_epoch: dict[int, dict[str, Any]] = {}
    for replica_id in range(expected.replica_count):
        events = replica_events.get(replica_id)
        if events is None:
            _incomplete(f"replica-{replica_id} structured event stream is absent")
        seen_commands: set[int] = set()
        seen_activations: set[int] = set()
        for event in events:
            if event.event_type == "epoch.command_committed":
                identity = _command_identity(
                    event.payload,
                    f"{event.relative_path}:{event.line_number}.payload",
                )
                successor = identity["successor_epoch_number"]
                if successor not in (1, 2) or successor in seen_commands:
                    _fail("replica emitted an extra or duplicate actual epoch command")
                seen_commands.add(successor)
                command_times[successor].append(event.monotonic_ns)
                canonical = command_identity_by_epoch.setdefault(successor, identity)
                if canonical != identity:
                    _fail("replicas disagree on an actual epoch command identity")
            elif event.event_type == "epoch.activated":
                activation = _activation_identity(
                    event.payload,
                    f"{event.relative_path}:{event.line_number}.payload",
                )
                epoch_number = activation["epoch_number"]
                if epoch_number == 0:
                    continue
                if epoch_number not in (1, 2) or epoch_number in seen_activations:
                    _fail("replica emitted an extra or duplicate epoch activation")
                seen_activations.add(epoch_number)
                activation_height = activation["activation_height"]
                bundle = bundles[epoch_number - 1]
                _record_activation_identity(
                    activation,
                    bundle=bundle,
                    canonical_by_epoch=activation_identity_by_epoch,
                )
                identity = command_identity_by_epoch.get(epoch_number)
                if identity is not None and activation_height != identity["activation_height"]:
                    _fail("replica activation height differs from its command")
                activation_times[epoch_number].append(event.monotonic_ns)
        if seen_commands != {1, 2} or seen_activations != {1, 2}:
            _incomplete(f"replica-{replica_id} lacks both exact command/activation witnesses")

    for cycle, bundle in enumerate(bundles):
        successor = cycle + 1
        command = command_identity_by_epoch.get(successor)
        if command is None:
            _incomplete(f"actual command for epoch {successor} is absent")
        expected_command = {
            "payload_digest": bundle.command.payload_digest,
            "predecessor_epoch_number": cycle,
            "predecessor_epoch_digest": predecessor_digest,
            "successor_epoch_number": successor,
            "successor_epoch_digest": bundle.epoch_digest,
            "activation_delay_blocks": expected_activation_delay,
        }
        if any(command[key] != value for key, value in expected_command.items()):
            _fail(f"actual epoch-{successor} command differs from its decoded bundle")
        if bundle.command.activation_delay_blocks != expected_activation_delay:
            _fail("decoded transition activation delay drifted")
        predecessor_digest = bundle.epoch_digest
    if (
        cutoff_times["epoch1_command"] != max(command_times[1])
        or cutoff_times["epoch2_command"] != max(command_times[2])
        or cutoff_times["epoch1_activation"] != max(activation_times[1])
        or cutoff_times["epoch2_activation"] != max(activation_times[2])
    ):
        _fail("actual transition cutoffs are not the last exact replica witnesses")

    terminals = [
        event for event in manager_events if event.event_type == "adaptive_v2_session_terminal"
    ]
    if len(terminals) != 2:
        _fail("manager did not emit exactly two actual transition terminals")
    for cycle, event in enumerate(terminals):
        payload = event.payload
        _fields(
            payload,
            {
                "cycle_ordinal",
                "policy_intent",
                "outcome",
                "reason",
                "transition_artifact_id",
                "predecessor_epoch_number",
                "predecessor_epoch_digest",
                "successor_epoch_number",
                "successor_epoch_digest",
                "command_payload_digest",
                "winning_activation",
                "evidence_window_activation_generation",
                "baseline_evidence_cutoff",
                "current_evidence_cutoff",
            },
            "manager terminal payload",
        )
        bundle = bundles[cycle]
        expected_intent = (
            "performance_optimization"
            if cycle == 1 and expected.placement_adaptation
            else "fault_containment"
        )
        snapshot = next(
            (
                candidate.payload
                for candidate in manager_events
                if candidate.event_type == "adaptive_v2_evidence_snapshot"
                and candidate.payload.get("cycle_ordinal") == cycle
            ),
            None,
        )
        shape_events = [
            candidate
            for candidate in manager_events
            if candidate.event_type == "adaptive_v2_shape_decision"
            and candidate.payload.get("cycle_ordinal") == cycle
        ]
        if len(shape_events) != 1:
            _fail("manager convergence start boundary is absent or duplicated")
        shape_event = shape_events[0]
        _validate_transition_terminal_deadline(
            shape_ns=shape_event.monotonic_ns,
            terminal_ns=event.monotonic_ns,
            observation_bound_rule=transition_observation_bound_rule,
            convergence_deadline_s=expected_convergence_deadline_s,
        )
        if (
            payload["cycle_ordinal"] != cycle
            or payload["policy_intent"] != expected_intent
            or payload["outcome"] != "advanced"
            or payload["reason"] != "successor_converged"
            or payload["transition_artifact_id"]
            != f"{expected.slot_id}-epoch{cycle + 1}"
            or payload["predecessor_epoch_number"] != cycle
            or payload["predecessor_epoch_digest"] != bundle.previous_epoch_digest
            or payload["successor_epoch_number"] != cycle + 1
            or payload["successor_epoch_digest"] != bundle.epoch_digest
            or payload["command_payload_digest"] != bundle.command.payload_digest
            or snapshot is None
            or payload["baseline_evidence_cutoff"] != snapshot.get("baseline_cutoff")
            or payload["current_evidence_cutoff"] != snapshot.get("current_cutoff")
        ):
            _fail("manager transition terminal is not the exact converged successor")
        winning = _mapping(payload["winning_activation"], "manager winning activation")
        identity = _command_identity(
            {
                "command_block_height": winning.get("command_block_height"),
                "command_block_hash": winning.get("command_block_hash"),
                "payload_digest": winning.get("command_payload_digest"),
                "predecessor_epoch_number": winning.get("predecessor_epoch_number"),
                "predecessor_epoch_digest": winning.get("predecessor_epoch_digest"),
                "successor_epoch_number": winning.get("successor_epoch_number"),
                "successor_epoch_digest": winning.get("successor_epoch_digest"),
                "activation_delay_blocks": winning.get("activation_delay_blocks"),
                "activation_height": winning.get("activation_height"),
            },
            "manager winning activation",
        )
        if identity != command_identity_by_epoch[cycle + 1]:
            _fail("manager winning activation differs from replica command consensus")


def _validate_guarded_actor_evidence(
    records: Sequence[_EvidenceRecord],
    *,
    baseline_cutoff: int,
    current_cutoff: int,
    actor_ids: Sequence[int],
    required_reporters: int,
) -> None:
    outstanding: dict[str, _EvidenceRecord] = {}
    for record in records:
        if not baseline_cutoff < record.ingestion_sequence <= current_cutoff:
            continue
        if record.outcome == "timeout":
            if record.observation_id in outstanding:
                _fail("guard evidence duplicates a post-baseline timeout")
            outstanding[record.observation_id] = record
        elif record.outcome == "late":
            outstanding.pop(record.observation_id, None)
    reporters: dict[int, set[int]] = defaultdict(set)
    for timeout in outstanding.values():
        reporters[timeout.target_id].add(timeout.reporter_id)
    for actor in actor_ids:
        if len(reporters.get(actor, set())) < required_reporters:
            _fail(
                f"actor {actor} lacks independently replayed guarded timeout reporters"
            )


def _validate_adaptation_cycles(
    *,
    slot_root: Path,
    manager_events: Sequence[_NativeEvent],
    accepted: Mapping[tuple[int, str], Sequence[_EvidenceRecord]],
    runtime: Mapping[str, Any],
    manifest: FrozenFactorialManifest,
    expected: _ExpectedSlot,
    initial_epoch_digest: str,
    initial_trees: Sequence[Tree],
    cutoff_times: Mapping[str, int],
) -> tuple[DecodedBundle, DecodedBundle]:
    compact_snapshot = manifest.evidence_snapshot_format == "digest_commitment_v2"
    issuer_payload = _read_bytes(slot_root, "runtime/issuer-identities.txt")
    assert issuer_payload is not None
    issuer_public_key = _identity_rows(
        issuer_payload,
        expected_count=1,
        expected_fields=frozenset({"pub", "sec"}),
        label="issuer identity",
    )[0]["pub"]
    snapshot_events = [
        event for event in manager_events if event.event_type == "adaptive_v2_evidence_snapshot"
    ]
    shape_events = [
        event for event in manager_events if event.event_type == "adaptive_v2_shape_decision"
    ]
    if len(snapshot_events) != 2 or len(shape_events) != 2:
        _fail("manager did not emit exactly two evidence snapshots and shape decisions")
    runtime_transitions = _array(runtime.get("transitions"), "runtime transitions")
    predecessor_digest = initial_epoch_digest
    predecessor_trees = tuple(initial_trees)
    bundles: list[DecodedBundle] = []
    for cycle in range(2):
        transition = _mapping(runtime_transitions[cycle], f"runtime transition {cycle}")
        request = _mapping(transition.get("request"), f"runtime transition {cycle} request")
        artifact_id = _string(transition.get("artifact_id"), "transition artifact ID")
        bundle_relative = _string(transition.get("bundle_relative_path"), "transition bundle path")
        bundle_payload = _read_bytes(slot_root, bundle_relative)
        assert bundle_payload is not None
        bundle = decode_epoch_change_bundle(
            bundle_payload,
            issuer_public_key=issuer_public_key,
        )
        if (
            bundle.command.issuer_id != 1
            or bundle.command.successor_epoch_number != cycle + 1
            or bundle.epoch_number != cycle + 1
            or bundle.previous_epoch_digest != predecessor_digest
            or bundle.command.predecessor_epoch_digest != predecessor_digest
        ):
            _fail("successor bundle does not continue the exact predecessor chain")

        snapshot_event = snapshot_events[cycle]
        snapshot = snapshot_event.payload
        if _validate_snapshot_audit_schema(
            snapshot,
            evidence_snapshot_format=manifest.evidence_snapshot_format,
            label=f"cycle-{cycle} evidence snapshot",
        ) != compact_snapshot:
            _fail("manifest evidence snapshot format changed within a slot")
        intent = _string(request.get("policy_intent"), "transition policy intent")
        baseline_cutoff = _integer(snapshot["baseline_cutoff"], "snapshot baseline cutoff", 1)
        current_cutoff = _integer(snapshot["current_cutoff"], "snapshot current cutoff", 1)
        if (
            snapshot["cycle_ordinal"] != cycle
            or snapshot["policy_intent"] != intent
            or snapshot["transition_artifact_id"] != artifact_id
            or snapshot["predecessor_epoch_number"] != cycle
            or snapshot["predecessor_epoch_digest"] != predecessor_digest
            or snapshot["activation_generation"] != cycle + 1
            or current_cutoff <= baseline_cutoff
        ):
            _fail("evidence snapshot is not bound to its exact live cycle")
        epoch_records = tuple(accepted.get((cycle, predecessor_digest), ()))
        if not epoch_records:
            _incomplete(f"cycle-{cycle} has no accepted raw evidence")
        if cycle == 0 and any(
            record.ingestion_sequence <= baseline_cutoff
            and (
                record.reporter_monotonic_ns >= cutoff_times["fault_window_open"]
                or record.acceptance_monotonic_ns
                >= cutoff_times["fault_window_open"]
            )
            for record in epoch_records
        ):
            _fail("cycle-0 baseline evidence does not strictly predate the fault window")
        full_prefix = _snapshot_records(
            epoch_records,
            baseline_cutoff=baseline_cutoff,
            current_cutoff=current_cutoff,
            suffix_only=False,
            allow_high_watermark_gaps=compact_snapshot,
        )
        if not compact_snapshot and snapshot[
            "observations"
        ] != _expected_snapshot_payload_observations(
            full_prefix, current_cutoff
        ):
            _fail("snapshot audit observations differ from accepted raw evidence")
        snapshot_path = _string(request.get("evidence_snapshot_path"), "snapshot artifact path")
        loaded_snapshot = _read_json(slot_root, snapshot_path, canonical=False)
        assert loaded_snapshot is not None
        snapshot_file, snapshot_bytes = loaded_snapshot
        if (
            snapshot_file != dict(snapshot)
            or not snapshot_bytes.endswith(b"\n")
            or snapshot_bytes.count(b"\n") != 1
        ):
            _fail("exclusive snapshot file differs from its native audit payload")

        suffix_only = intent == "performance_optimization"
        selected_records = _snapshot_records(
            epoch_records,
            baseline_cutoff=baseline_cutoff,
            current_cutoff=current_cutoff,
            suffix_only=suffix_only,
            allow_high_watermark_gaps=compact_snapshot,
        )
        policy = _mapping(runtime.get("responsiveness_policy"), "runtime responsiveness policy")
        scores = _score_snapshot(selected_records, expected.replica_count, policy)
        if cycle == 0 and any(
            next(score for score in scores if score.replica_id == actor).classification
            != "nonresponsive"
            for actor in expected.actor_ids
        ):
            _fail("selected actors are not nonresponsive in independently replayed evidence")
        if cycle == 0 and intent == "fault_containment":
            _validate_guarded_actor_evidence(
                epoch_records,
                baseline_cutoff=baseline_cutoff,
                current_cutoff=current_cutoff,
                actor_ids=expected.actor_ids,
                required_reporters=expected.f + 1,
            )
        computed_snapshot_id = _snapshot_id(
            selected_records,
            replica_count=expected.replica_count,
            epoch_number=cycle,
            epoch_digest=predecessor_digest,
            cutoff=current_cutoff,
            policy=policy,
        )
        computed_full_prefix_snapshot_id = _snapshot_id(
            full_prefix,
            replica_count=expected.replica_count,
            epoch_number=cycle,
            epoch_digest=predecessor_digest,
            cutoff=current_cutoff,
            policy=policy,
        )
        if compact_snapshot:
            _validate_compact_snapshot_commitments(
                snapshot,
                accepted_prefix_count=len(full_prefix),
                current_cutoff=current_cutoff,
                full_prefix_snapshot_id=computed_full_prefix_snapshot_id,
                evidence_snapshot_id=computed_snapshot_id,
            )
        if (
            bundle.evidence_snapshot_id != computed_snapshot_id
            or bundle.evidence_cutoff != current_cutoff
        ):
            _fail("successor bundle snapshot identity/cutoff does not recompute")
        roots = _expected_roots(
            predecessor_trees=predecessor_trees,
            scores=scores,
            actor_ids=expected.actor_ids,
            tree_count=expected.q,
            intent=intent,
        )
        if snapshot["eligible_ranking"] != list(roots):
            _fail("snapshot eligible roots differ from the live evidence ranking")

        shape_event = shape_events[cycle]
        shape_payload = shape_event.payload
        _fields(
            shape_payload,
            {"cycle_ordinal", "transition_artifact_id", "decision"},
            f"cycle-{cycle} shape event",
        )
        if (
            shape_payload["cycle_ordinal"] != cycle
            or shape_payload["transition_artifact_id"] != artifact_id
        ):
            _fail("shape decision is rebound to another transition")
        apply_selected = cycle == 1 and expected.shape_adaptation
        decision = validate_shape_decision(
            _mapping(shape_payload["decision"], f"cycle-{cycle} shape decision"),
            epoch_number=cycle,
            epoch_digest=predecessor_digest,
            trees=predecessor_trees,
            scores=scores,
            evidence_cutoff=current_cutoff,
            candidate_fanouts=expected.candidate_fanouts,
            tree_count=expected.q,
            pipeline_stretch=expected.pipeline_stretch,
            deterministic_seed=expected.scientific_seed,
            apply_selected=apply_selected,
        )
        _validate_successor_trees(
            bundle,
            expected=expected,
            actor_ids=expected.actor_ids,
            expected_roots=roots,
            expected_fanout=_integer(decision["applied_fanout"], "applied fanout", 1),
        )
        bundles.append(bundle)
        predecessor_digest = bundle.epoch_digest
        predecessor_trees = bundle.trees
    if cutoff_times["shape_v1_computed"] != shape_events[1].monotonic_ns:
        _fail("shape cutoff is not the actual second live selector event")
    validate_manager_blinding((), tuple(event.payload for event in manager_events))
    return bundles[0], bundles[1]


def validate_slot(slot_directory: str | Path) -> SlotValidationResult:
    """Validate one preserved slot without executing campaign code."""

    slot_root = Path(slot_directory)
    slot_id = slot_root.name
    campaign_member = slot_id != EXCLUDED_SMOKE_SLOT_ID
    expected: _ExpectedSlot | None = None
    try:
        if slot_root.is_symlink() or not slot_root.is_dir():
            _incomplete("slot directory is absent or is not a regular directory")
        manifest, plan, runtime, expected, runtime_sha256 = _load_static_contracts(slot_root)
        authorization, authorization_bytes = _validate_execution_authorization(
            slot_root,
            manifest=manifest,
            expected=expected,
            plan=plan,
            runtime_sha256=runtime_sha256,
            campaign_member=campaign_member,
        )
        loaded_receipt = _read_json(slot_root, SLOT_FILENAME)
        assert loaded_receipt is not None
        receipt, receipt_bytes = loaded_receipt
        (
            anchor_ns,
            window_start_ns,
            window_end_ns,
            recorded_slot_root,
            recorded_repository,
        ) = _validate_slot_receipt(
            receipt,
            slot_root=slot_root,
            manifest=manifest,
            expected=expected,
            runtime=runtime,
            runtime_sha256=runtime_sha256,
            authorization_bytes=authorization_bytes,
        )
        loaded_outcome = _read_json(slot_root, OUTCOME_FILENAME)
        assert loaded_outcome is not None
        outcome_document, _ = loaded_outcome
        declared_outcome, declared_reason = _validate_outcome(
            slot_root, outcome_document, expected.slot_id
        )
        if declared_outcome == "NOT_STARTED":
            _incomplete("slot outcome never advanced beyond NOT_STARTED")
        if declared_outcome in ("FAIL", "INCOMPLETE"):
            return SlotValidationResult(
                slot_id=expected.slot_id,
                outcome=declared_outcome,
                reason=declared_reason,
                block_id=expected.block_id,
                arm_code=expected.arm_code,
                replica_count=expected.replica_count,
                initial_fanout=expected.initial_fanout,
                integrity_valid=False,
                campaign_member=campaign_member,
            )

        build_provenance, build_provenance_bytes = _validate_build_provenance(
            slot_root,
            revision=_string(
                authorization["kauri_revision"], "authorization revision"
            ),
            recorded_repository=recorded_repository,
        )
        if authorization["build_provenance_sha256"] != _sha256(
            build_provenance_bytes
        ):
            _fail(
                "execution authorization does not bind the canonical build provenance"
            )
        _validate_execution_provenance(
            slot_root,
            recorded_result_root=recorded_slot_root.parent,
            expected=expected,
            campaign_member=campaign_member,
            receipt=receipt,
            receipt_bytes=receipt_bytes,
            authorization=authorization,
            authorization_bytes=authorization_bytes,
            build_provenance=build_provenance,
            build_provenance_bytes=build_provenance_bytes,
        )
        _validate_runner_state(
            slot_root,
            expected=expected,
            anchor_ns=anchor_ns,
        )

        _validate_materialized_configs(
            slot_root,
            runtime=runtime,
            expected=expected,
            manifest=manifest,
        )

        event_contract = _mapping(runtime.get("structured_events"), "runtime structured events")
        manager_events = _read_jsonl(
            slot_root,
            _string(event_contract.get("manager_output_relative_path"), "manager event path"),
            run_id=expected.slot_id,
            source_kind="adaptation_manager",
            source_id=_string(event_contract.get("manager_source_id"), "manager source ID"),
            source_instance=_string(
                event_contract.get("manager_source_instance"), "manager source instance"
            ),
        )
        replica_paths = _array(
            event_contract.get("replica_output_relative_paths"), "replica event paths"
        )
        replica_ids = _array(event_contract.get("replica_source_ids"), "replica source IDs")
        replica_instances = _array(
            event_contract.get("replica_source_instances"), "replica source instances"
        )
        replica_events: dict[int, tuple[_NativeEvent, ...]] = {}
        for replica_id in range(expected.replica_count):
            replica_events[replica_id] = _read_jsonl(
                slot_root,
                _string(replica_paths[replica_id], "replica event path"),
                run_id=expected.slot_id,
                source_kind="replica",
                source_id=_string(replica_ids[replica_id], "replica source ID"),
                source_instance=_string(
                    replica_instances[replica_id], "replica source instance"
                ),
            )
        all_events = (*manager_events, *(event for events in replica_events.values() for event in events))
        events_by_ref: dict[tuple[str, int], _NativeEvent] = {}
        for event in all_events:
            key = (event.relative_path, event.source_sequence)
            if key in events_by_ref:
                _fail("native event reference identity is duplicated")
            events_by_ref[key] = event

        cutoff_contract = _mapping(runtime.get("cutoff_contract"), "runtime cutoff contract")
        bucket_width_s = _integer(
            cutoff_contract.get("bucket_width_s"), "runtime bucket width", 1
        )
        bucket_counts = {
            "baseline": _integer(cutoff_contract.get("baseline_bucket_count"), "baseline buckets", 1),
            "fault_evidence": _integer(cutoff_contract.get("fault_evidence_bucket_count"), "fault buckets", 1),
            "epoch1_stable": _integer(cutoff_contract.get("epoch1_stable_bucket_count"), "epoch1 buckets", 1),
            "epoch2_stable": _integer(cutoff_contract.get("epoch2_stable_bucket_count"), "epoch2 buckets", 1),
        }
        loaded_cutoffs = _read_json(slot_root, PHASE_CUTOFFS_FILENAME)
        assert loaded_cutoffs is not None
        cutoff_document, _ = loaded_cutoffs
        cutoff_times, phase_windows = _validate_phase_cutoffs(
            cutoff_document,
            slot_id=expected.slot_id,
            slot_receipt_sha256=_sha256(receipt_bytes),
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
            bucket_width_s=bucket_width_s,
            bucket_counts=bucket_counts,
            events_by_ref=events_by_ref,
            manager_events=manager_events,
            replica_events=replica_events,
            replica_count=expected.replica_count,
            quorum=expected.q,
            drain_margin_s=_integer(
                _mapping(runtime.get("fault_window"), "runtime fault window").get(
                    "drain_margin_s"
                ),
                "runtime fault window drain margin",
                1,
            ),
        )

        initial_epoch_digest, initial_trees = _initial_epoch(expected)
        accepted = _accepted_evidence(
            manager_events,
            expected.replica_count,
            allow_ingestion_sequence_gaps=(
                manifest.evidence_snapshot_format == "digest_commitment_v2"
            ),
        )
        bundles = _validate_adaptation_cycles(
            slot_root=slot_root,
            manager_events=manager_events,
            accepted=accepted,
            runtime=runtime,
            manifest=manifest,
            expected=expected,
            initial_epoch_digest=initial_epoch_digest,
            initial_trees=initial_trees,
            cutoff_times=cutoff_times,
        )
        _validate_cleanup_ledger(
            slot_root,
            expected=expected,
            manager_events=manager_events,
            drain_complete_ns=cutoff_times["epoch2_drain_complete"],
        )
        _validate_native_transitions(
            manager_events=manager_events,
            replica_events=replica_events,
            bundles=bundles,
            initial_epoch_digest=initial_epoch_digest,
            expected=expected,
            expected_activation_delay=manifest.common_timers.activation_delay_blocks,
            expected_convergence_deadline_s=(
                manifest.common_timers.transition_convergence_deadline_s
            ),
            transition_observation_bound_rule=(
                manifest.common_timers.transition_observation_bound_rule
            ),
            cutoff_times=cutoff_times,
        )

        logs = _mapping(runtime.get("process_logs"), "runtime process logs")
        for name in ("manager_stdout_relative_path", "manager_stderr_relative_path"):
            _safe_file(slot_root, _string(logs.get(name), f"process_logs.{name}"))
        stdout = _array(logs.get("replica_stdout_relative_paths"), "replica stdout paths")
        stderr = _array(logs.get("replica_stderr_relative_paths"), "replica stderr paths")
        paths_by_replica = {
            replica_id: (
                _string(stdout[replica_id], "replica stdout path"),
                _string(stderr[replica_id], "replica stderr path"),
            )
            for replica_id in range(expected.replica_count)
        }
        markers = _fault_markers(slot_root, paths_by_replica)
        cycle0_snapshots = [
            event
            for event in manager_events
            if event.event_type == "adaptive_v2_evidence_snapshot"
            and event.payload.get("cycle_ordinal") == 0
        ]
        if len(cycle0_snapshots) != 1:
            _fail("cycle-0 evidence snapshot is absent or duplicated")
        cycle0_snapshot = cycle0_snapshots[0].payload
        expected_window, expected_max_omissions = _effective_omission_contract(
            manifest, expected
        )
        validate_fault_causality(
            markers=markers,
            replica_events=replica_events,
            actor_ids=expected.actor_ids,
            fault_mode=manifest.byzantine.mode,
            max_omissions_per_proposal=expected_max_omissions,
            initial_epoch_digest=initial_epoch_digest,
            initial_trees=initial_trees,
            window_id=expected_window,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
            epoch1_command_ns=cutoff_times["epoch1_command"],
            epoch1_activation_ns=cutoff_times["epoch1_activation"],
            epoch2_command_ns=cutoff_times["epoch2_command"],
            epoch1_digest=bundles[0].epoch_digest,
            epoch1_trees=bundles[0].trees,
            epoch2_digest=bundles[1].epoch_digest,
            epoch2_trees=bundles[1].trees,
            phase_windows=phase_windows,
            required_reporters=expected.f + 1,
            accepted_epoch0=accepted.get((0, initial_epoch_digest), ()),
            baseline_cutoff=_integer(
                cycle0_snapshot.get("baseline_cutoff"),
                "cycle-0 baseline cutoff",
                1,
            ),
            current_cutoff=_integer(
                cycle0_snapshot.get("current_cutoff"),
                "cycle-0 current cutoff",
                1,
            ),
        )

        loaded_throughput = _read_json(slot_root, THROUGHPUT_FILENAME)
        assert loaded_throughput is not None
        throughput, _ = loaded_throughput
        phase_configurations: dict[str, tuple[int, str, Collection[int]]] = {
            "baseline": (
                0,
                initial_epoch_digest,
                tuple(tree.tree_id for tree in initial_trees),
            ),
            "fault_evidence": (
                0,
                initial_epoch_digest,
                tuple(tree.tree_id for tree in initial_trees),
            ),
            "epoch1_stable": (
                1,
                bundles[0].epoch_digest,
                tuple(tree.tree_id for tree in bundles[0].trees),
            ),
            "epoch2_stable": (
                2,
                bundles[1].epoch_digest,
                tuple(tree.tree_id for tree in bundles[1].trees),
            ),
        }
        recorded_phase_configurations: dict[str, tuple[int, str]] = {}
        for index, raw_phase in enumerate(
            _array(cutoff_document.get("phases"), "phase-cutoffs.phases")
        ):
            phase = _mapping(raw_phase, f"phase-cutoffs.phases[{index}]")
            name = _string(phase.get("phase"), "phase-cutoffs phase name")
            configuration = _mapping(
                phase.get("configuration"),
                f"phase-cutoffs.{name}.configuration",
            )
            recorded_phase_configurations[name] = (
                _integer(
                    configuration.get("epoch_number"),
                    f"phase-cutoffs.{name}.configuration.epoch_number",
                ),
                _digest(
                    configuration.get("epoch_digest"),
                    f"phase-cutoffs.{name}.configuration.epoch_digest",
                ),
            )
        if recorded_phase_configurations != {
            name: (configuration[0], configuration[1])
            for name, configuration in phase_configurations.items()
        }:
            _fail(
                "phase-cutoff configurations do not match the independently "
                "reconstructed epoch identities"
            )
        metrics = validate_throughput_document(
            throughput,
            slot_id=expected.slot_id,
            replica_events=replica_events,
            phase_windows=phase_windows,
            phase_configurations=phase_configurations,
            bucket_width_s=bucket_width_s,
            observer_id=_string(event_contract.get("commit_observer_id"), "commit observer ID"),
            observer_instance=_string(
                event_contract.get("commit_observer_instance"), "commit observer instance"
            ),
        )
        return SlotValidationResult(
            slot_id=expected.slot_id,
            outcome="PASS",
            reason=None,
            block_id=expected.block_id,
            arm_code=expected.arm_code,
            replica_count=expected.replica_count,
            initial_fanout=expected.initial_fanout,
            metrics=metrics,
            integrity_valid=True,
            campaign_member=campaign_member,
        )
    except _Incomplete as error:
        return SlotValidationResult(
            slot_id=slot_id,
            outcome="INCOMPLETE",
            reason=str(error),
            block_id=None if expected is None else expected.block_id,
            arm_code=None if expected is None else expected.arm_code,
            replica_count=None if expected is None else expected.replica_count,
            initial_fanout=None if expected is None else expected.initial_fanout,
            integrity_valid=False,
            campaign_member=campaign_member,
        )
    except (_Reject, OSError, ValueError) as error:
        return SlotValidationResult(
            slot_id=slot_id,
            outcome="FAIL",
            reason=str(error),
            block_id=None if expected is None else expected.block_id,
            arm_code=None if expected is None else expected.arm_code,
            replica_count=None if expected is None else expected.replica_count,
            initial_fanout=None if expected is None else expected.initial_fanout,
            integrity_valid=False,
            campaign_member=campaign_member,
        )


def _matched_estimate(
    contrast: str,
    block_ids: Sequence[str],
    effects: Sequence[float],
) -> MatchedEstimate:
    values = tuple(float(value) for value in effects)
    blocks = tuple(block_ids)
    if len(values) != 5 or len(blocks) != len(values) or not all(
        math.isfinite(value) for value in values
    ):
        _fail("headline estimate requires five finite matched-block effects")
    mean = sum(values) / len(values)
    sample_variance = sum((value - mean) ** 2 for value in values) / (
        len(values) - 1
    )
    sample_standard_deviation = math.sqrt(sample_variance)
    # Frozen two-sided 95% Student-t interval for five matched blocks (df=4).
    margin = 2.7764451051977987 * sample_standard_deviation / math.sqrt(len(values))
    lower = mean - margin
    upper = mean + margin
    return MatchedEstimate(
        contrast=contrast,
        block_ids=blocks,
        block_effects_tps=values,
        mean_tps=mean,
        sample_standard_deviation_tps=sample_standard_deviation,
        ci95_lower_tps=lower,
        ci95_upper_tps=upper,
        positive_block_count=sum(value > 0 for value in values),
        directional_claim_supported=lower > 0,
    )


def _headline_effects(
    manifest: FrozenFactorialManifest,
    results: Mapping[str, SlotValidationResult],
) -> FactorialEffects | None:
    scope = manifest.claim_scope
    placement_blocks = tuple(
        f"n{scope.placement_headline_replica_count}-"
        f"f{scope.placement_headline_initial_fanout}-b{index:02d}"
        for index in range(1, scope.placement_headline_block_count + 1)
    )
    joint_blocks = tuple(
        f"n{scope.shape_and_joint_headline_replica_count}-"
        f"f{scope.shape_and_joint_headline_initial_fanout}-b{index:02d}"
        for index in range(1, scope.shape_and_joint_headline_block_count + 1)
    )

    def values(block_id: str) -> dict[str, dict[str, float]] | None:
        by_arm: dict[str, dict[str, float]] = {}
        for result in results.values():
            if result.block_id != block_id or result.arm_code is None:
                continue
            if not result.figure_eligible:
                return None
            metrics = {metric.phase: metric.mean_tps for metric in result.metrics}
            if set(metrics) != set(PHASES) or result.arm_code in by_arm:
                return None
            by_arm[result.arm_code] = metrics
        return by_arm if set(by_arm) == set(EXPECTED_ARM_CODES) else None

    placement_values = [values(block) for block in placement_blocks]
    joint_values = [values(block) for block in joint_blocks]
    if any(value is None for value in (*placement_values, *joint_values)):
        return None
    placement = [value for value in placement_values if value is not None]
    joint = [value for value in joint_values if value is not None]
    placement_effects = [
        0.5
        * (
            (value["P"]["epoch2_stable"] - value["00"]["epoch2_stable"])
            + (value["PS"]["epoch2_stable"] - value["S"]["epoch2_stable"])
        )
        for value in placement
    ]
    shape_effects = [
        0.5
        * (
            (value["S"]["epoch2_stable"] - value["00"]["epoch2_stable"])
            + (value["PS"]["epoch2_stable"] - value["P"]["epoch2_stable"])
        )
        for value in joint
    ]
    interactions = [
        value["PS"]["epoch2_stable"]
        - value["P"]["epoch2_stable"]
        - value["S"]["epoch2_stable"]
        + value["00"]["epoch2_stable"]
        for value in joint
    ]

    def adaptive_mean(
        value: Mapping[str, Mapping[str, float]], phase: str
    ) -> float:
        return 0.5 * (value["P"][phase] + value["PS"][phase])

    fault_drops = [
        adaptive_mean(value, "baseline")
        - adaptive_mean(value, "fault_evidence")
        for value in placement
    ]
    containment_recoveries = [
        adaptive_mean(value, "epoch1_stable")
        - adaptive_mean(value, "fault_evidence")
        for value in placement
    ]
    optimization_gains = [
        adaptive_mean(value, "epoch2_stable")
        - adaptive_mean(value, "epoch1_stable")
        for value in placement
    ]
    return FactorialEffects(
        endpoint="epoch2_stable_mean_tps",
        estimator=scope.headline_estimator,
        uncertainty_interval=scope.uncertainty_interval,
        directional_claim_rule=scope.directional_claim_rule,
        placement_main_effect=_matched_estimate(
            "placement_main_effect_epoch2_stable_tps",
            placement_blocks,
            placement_effects,
        ),
        shape_main_effect=_matched_estimate(
            "shape_main_effect_epoch2_stable_tps",
            joint_blocks,
            shape_effects,
        ),
        placement_shape_interaction=_matched_estimate(
            "placement_shape_interaction_epoch2_stable_tps",
            joint_blocks,
            interactions,
        ),
        fault_drop=_matched_estimate(
            "adaptive_arms_baseline_minus_fault_tps",
            placement_blocks,
            fault_drops,
        ),
        containment_recovery=_matched_estimate(
            "adaptive_arms_epoch1_minus_fault_tps",
            placement_blocks,
            containment_recoveries,
        ),
        optimization_gain=_matched_estimate(
            "adaptive_arms_epoch2_minus_epoch1_tps",
            placement_blocks,
            optimization_gains,
        ),
    )


def _validate_campaign_execution_ledger(
    root: Path,
    *,
    manifest: FrozenFactorialManifest,
    plan: Mapping[str, Any],
    runtime: Mapping[str, Any],
    expected_slots: Sequence[_ExpectedSlot],
    actual_by_id: Mapping[str, Path],
    results_by_id: Mapping[str, SlotValidationResult],
    campaign_outcome: str,
) -> int:
    """Replay the exact sequential no-retry campaign lifecycle."""

    identity = _frozen_artifact_identity(manifest.manifest_id)
    loaded_authorization = _read_json(root, CAMPAIGN_AUTHORIZATION_FILENAME)
    loaded_contract = _read_json(root, CAMPAIGN_CONTRACT_FILENAME)
    assert loaded_authorization is not None and loaded_contract is not None
    authorization, authorization_bytes = loaded_authorization
    contract, contract_bytes = loaded_contract
    authorization_fields = {
        "schema_version",
        "scope",
        "authorized_by",
        "approval_reference",
        "approved_utc",
        "kauri_revision",
        "slot_ids",
        "static_artifacts_sha256",
        "build_provenance_sha256",
        "automatic_retries",
        "replacement_policy",
        "authorization_id",
    }
    _fields(
        authorization,
        authorization_fields,
        CAMPAIGN_AUTHORIZATION_FILENAME,
    )
    planned_ids = [
        _string(_mapping(row, "campaign plan slot").get("slot_id"), "plan slot ID")
        for row in _array(plan.get("slots"), "campaign plan slots")
    ]
    authorization_core = {
        key: value for key, value in authorization.items() if key != "authorization_id"
    }
    expected_authorization_id = "execution-authorization-" + _sha256(
        _canonical_json_bytes(authorization_core)
    )[:24]
    try:
        approved = dt.datetime.fromisoformat(
            _string(authorization["approved_utc"], "campaign approval timestamp").replace(
                "Z", "+00:00"
            )
        )
    except ValueError as error:
        raise _Reject("campaign authorization timestamp is invalid") from error
    revision = _string(authorization["kauri_revision"], "campaign revision")
    if (
        authorization["schema_version"] != 1
        or authorization["scope"] != "shape25_campaign"
        or authorization["authorized_by"] != "thesis_author"
        or not _string(
            authorization["approval_reference"], "campaign approval reference"
        )
        or approved.tzinfo is None
        or approved.utcoffset() is None
        or _HEX40.fullmatch(revision) is None
        or authorization["slot_ids"] != planned_ids
        or dict(
            _mapping(
                authorization["static_artifacts_sha256"],
                "campaign authorization static hashes",
            )
        )
        != {
            MANIFEST_FILENAME: identity.manifest_sha256,
            PLAN_FILENAME: identity.plan_sha256,
            RUNTIME_FILENAME: identity.runtime_sha256,
        }
        or authorization["automatic_retries"] != 0
        or authorization["replacement_policy"] != "none"
        or authorization["authorization_id"] != expected_authorization_id
    ):
        _fail("campaign authorization does not bind the exact frozen one-shot run")

    schedule = [
        {
            "execution_ordinal": slot.execution_ordinal,
            "slot_id": slot.slot_id,
            "block_id": slot.block_id,
            "arm_code": slot.arm_code,
        }
        for slot in sorted(expected_slots, key=lambda value: value.execution_ordinal)
    ]
    build_provenance_sha256 = _digest(
        contract.get("build_provenance_sha256"),
        "campaign contract build provenance digest",
    )
    if (
        _digest(
            authorization["build_provenance_sha256"],
            "campaign authorization build provenance digest",
        )
        != build_provenance_sha256
    ):
        _fail("campaign authorization does not bind its canonical build provenance")
    expected_contract = {
        "schema_version": 1,
        "campaign_id": _string(runtime.get("runtime_id"), "campaign runtime ID"),
        "manifest_id": identity.manifest_id,
        "manifest_sha256": identity.manifest_sha256,
        "plan_sha256": identity.plan_sha256,
        "runtime_sha256": identity.runtime_sha256,
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": _sha256(authorization_bytes),
        "kauri_revision": revision,
        "build_provenance_sha256": build_provenance_sha256,
        "execution_mode": "fixed_sequential",
        "automatic_retries": 0,
        "replacement_policy": "none",
        "outcome_dependent_order": False,
        "expected_slot_count": EXPECTED_SLOT_COUNT,
        "execution_schedule": schedule,
    }
    if dict(contract) != expected_contract:
        _fail("campaign execution contract differs from the frozen schedule")
    contract_sha256 = _sha256(contract_bytes)

    ledger_bytes = _read_bytes(root, CAMPAIGN_LEDGER_FILENAME)
    assert ledger_bytes is not None
    if not ledger_bytes.endswith(b"\n"):
        _incomplete("campaign attempt ledger ends with a partial record")
    ledger_rows: list[Mapping[str, Any]] = []
    for index, raw in enumerate(ledger_bytes.splitlines(keepends=True), 1):
        row = _parse_json_bytes(raw, f"{CAMPAIGN_LEDGER_FILENAME}:{index}")
        if raw != _canonical_json_bytes(row):
            _fail("campaign attempt ledger contains a noncanonical record")
        ledger_rows.append(row)
    if not ledger_rows:
        _incomplete("campaign attempt ledger contains no started attempt")
    if len(ledger_rows) % 2:
        _incomplete("campaign stopped with one unpaired STARTED attempt")
    attempted = len(ledger_rows) // 2
    if attempted > EXPECTED_SLOT_COUNT:
        _fail("campaign attempt ledger exceeds the frozen denominator")

    ordered_slots = tuple(
        sorted(expected_slots, key=lambda value: value.execution_ordinal)
    )
    previous_monotonic_ns = 0
    attempted_ids: set[str] = set()
    all_attempts_accepted = True
    common_fields = {
        "schema_version",
        "campaign_id",
        "manifest_sha256",
        "source_plan_sha256",
        "runtime_sha256",
        "contract_sha256",
        "authorization_id",
        "authorization_sha256",
        "kauri_revision",
        "execution_ordinal",
        "slot_id",
        "block_id",
        "arm_code",
        "attempt_ordinal",
        "automatic_retries",
        "replacement_policy",
        "state",
        "recorded_utc",
        "recorded_monotonic_ns",
        "slot_directory",
    }

    def original_slot_path(slot_directory: Path) -> str:
        loaded = _read_json(slot_directory, SLOT_FILENAME)
        assert loaded is not None
        receipt, _ = loaded
        replica_rows = _array(receipt.get("replica_argv"), "slot replica argv")
        zero = [
            _mapping(row, "slot replica argv")
            for row in replica_rows
            if _mapping(row, "slot replica argv").get("replica_id") == 0
        ]
        if len(zero) != 1:
            _fail("campaign slot receipt lacks one replica-0 launch vector")
        argv = [
            _string(value, "slot replica argv value")
            for value in _array(zero[0].get("argv"), "slot replica argv")
        ]
        suffix = "/runtime/main.conf"
        candidates = [value[: -len(suffix)] for value in argv if value.endswith(suffix)]
        if len(candidates) != 1:
            _fail("campaign slot receipt does not expose one original slot root")
        return candidates[0]

    for index in range(attempted):
        expected = ordered_slots[index]
        started = ledger_rows[index * 2]
        terminal = ledger_rows[index * 2 + 1]
        _fields(
            started,
            common_fields
            | {
                "preflight_revision",
                "preflight_free_bytes",
                "build_provenance_sha256",
            },
            f"campaign STARTED ordinal {index + 1}",
        )
        _fields(
            terminal,
            common_fields
            | {
                "execution_outcome",
                "execution_reason",
                "launch_count",
                "validation",
            },
            f"campaign TERMINAL ordinal {index + 1}",
        )
        expected_common = {
            "schema_version": 1,
            "campaign_id": runtime["runtime_id"],
            "manifest_sha256": identity.manifest_sha256,
            "source_plan_sha256": identity.plan_sha256,
            "runtime_sha256": identity.runtime_sha256,
            "contract_sha256": contract_sha256,
            "authorization_id": authorization["authorization_id"],
            "authorization_sha256": _sha256(authorization_bytes),
            "kauri_revision": revision,
            "execution_ordinal": expected.execution_ordinal,
            "slot_id": expected.slot_id,
            "block_id": expected.block_id,
            "arm_code": expected.arm_code,
            "attempt_ordinal": 1,
            "automatic_retries": 0,
            "replacement_policy": "none",
        }
        for row, state in ((started, "STARTED"), (terminal, "TERMINAL")):
            if any(row[key] != value for key, value in expected_common.items()):
                _fail("campaign ledger identity/order/no-retry binding drifted")
            if row["state"] != state:
                _fail("campaign ledger does not alternate STARTED then TERMINAL")
            try:
                recorded_utc = dt.datetime.fromisoformat(
                    _string(row["recorded_utc"], "campaign ledger UTC").replace(
                        "Z", "+00:00"
                    )
                )
            except ValueError as error:
                raise _Reject("campaign ledger UTC timestamp is invalid") from error
            monotonic_ns = _integer(
                row["recorded_monotonic_ns"], "campaign ledger monotonic timestamp", 1
            )
            if (
                recorded_utc.tzinfo is None
                or recorded_utc.utcoffset() is None
                or monotonic_ns < previous_monotonic_ns
            ):
                _fail("campaign ledger timestamps are invalid or regress")
            previous_monotonic_ns = monotonic_ns
        slot_directory = actual_by_id.get(expected.slot_id)
        if slot_directory is None:
            _fail("campaign ledger references a slot directory that is not preserved")
        recorded_path = original_slot_path(slot_directory)
        if started["slot_directory"] != recorded_path or terminal["slot_directory"] != recorded_path:
            _fail("campaign ledger slot path differs from the exact launch receipt")
        if (
            started["preflight_revision"] != revision
            or _integer(
                started["preflight_free_bytes"], "campaign preflight free bytes", 1
            )
            < _integer(runtime.get("minimum_free_bytes"), "runtime minimum free bytes", 1)
            or started["build_provenance_sha256"] != build_provenance_sha256
        ):
            _fail("campaign STARTED record does not prove the exact preflight")
        build_bytes = _read_bytes(slot_directory, BUILD_PROVENANCE_FILENAME)
        assert build_bytes is not None
        if _sha256(build_bytes) != build_provenance_sha256:
            _fail("campaign slot build provenance differs from its execution contract")
        slot_authorization = _read_bytes(slot_directory, AUTHORIZATION_FILENAME)
        assert slot_authorization is not None
        if slot_authorization != authorization_bytes:
            _fail("campaign slot authorization differs from the root receipt")
        result = results_by_id[expected.slot_id]
        expected_validation = {
            "outcome": result.outcome,
            "reason": result.reason,
            "integrity_valid": result.integrity_valid,
            "campaign_member": result.campaign_member,
            "figure_eligible": result.figure_eligible,
        }
        if dict(_mapping(terminal["validation"], "terminal validation")) != expected_validation:
            _fail("campaign TERMINAL validator verdict does not independently replay")
        execution_outcome = terminal["execution_outcome"]
        execution_reason = terminal["execution_reason"]
        launch_count = _integer(terminal["launch_count"], "terminal launch count")
        if (
            execution_outcome not in ("PASS", "INCOMPLETE")
            or (execution_outcome == "PASS" and execution_reason is not None)
            or (
                execution_outcome == "INCOMPLETE"
                and (not isinstance(execution_reason, str) or not execution_reason)
            )
            or launch_count > expected.replica_count + 1
            or (execution_outcome == "PASS" and launch_count != expected.replica_count + 1)
        ):
            _fail("campaign TERMINAL execution lifecycle is invalid")
        accepted = (
            execution_outcome == "PASS"
            and result.outcome == "PASS"
            and result.integrity_valid
            and result.campaign_member
            and result.figure_eligible
        )
        all_attempts_accepted = all_attempts_accepted and accepted
        if not accepted and index + 1 != attempted:
            _fail("campaign continued after a non-PASS terminal validation")
        attempted_ids.add(expected.slot_id)

    if set(actual_by_id) != attempted_ids:
        _fail("campaign slot directories differ from the exact attempted prefix")
    if attempted == EXPECTED_SLOT_COUNT:
        if campaign_outcome != "PASS" or not all_attempts_accepted:
            _fail("complete campaign ledger contains a non-PASS slot")
    elif campaign_outcome == "PASS":
        _fail("partial campaign ledger cannot validate PASS")

    summary_loaded = _read_json(root, CAMPAIGN_SUMMARY_FILENAME)
    assert summary_loaded is not None
    summary, _ = summary_loaded
    expected_summary_fields = {
        "schema_version",
        "campaign_id",
        "authorization_id",
        "authorization_sha256",
        "contract_sha256",
        "ledger_sha256",
        "expected_slot_count",
        "attempted_slot_count",
        "next_execution_ordinal",
        "execution_complete",
        "stopped_reason",
        "completed_utc",
    }
    _fields(summary, expected_summary_fields, CAMPAIGN_SUMMARY_FILENAME)
    stopped_reason = summary["stopped_reason"]
    if stopped_reason is not None and (
        not isinstance(stopped_reason, str) or not stopped_reason
    ):
        _fail("campaign summary stopped reason is invalid")
    try:
        completed = dt.datetime.fromisoformat(
            _string(summary["completed_utc"], "campaign completion UTC").replace(
                "Z", "+00:00"
            )
        )
    except ValueError as error:
        raise _Reject("campaign completion timestamp is invalid") from error
    complete = attempted == EXPECTED_SLOT_COUNT and all_attempts_accepted
    if (
        summary["schema_version"] != 1
        or summary["campaign_id"] != runtime["runtime_id"]
        or summary["authorization_id"] != authorization["authorization_id"]
        or summary["authorization_sha256"] != _sha256(authorization_bytes)
        or summary["contract_sha256"] != contract_sha256
        or summary["ledger_sha256"] != _sha256(ledger_bytes)
        or summary["expected_slot_count"] != EXPECTED_SLOT_COUNT
        or summary["attempted_slot_count"] != attempted
        or summary["next_execution_ordinal"]
        != (None if attempted == EXPECTED_SLOT_COUNT else attempted + 1)
        or summary["execution_complete"] is not complete
        or (complete and stopped_reason is not None)
        or (not complete and stopped_reason is None)
        or completed.tzinfo is None
        or completed.utcoffset() is None
    ):
        _fail("campaign execution summary does not seal the replayed lifecycle")
    return attempted


def validate_campaign(campaign_directory: str | Path) -> CampaignValidationResult:
    """Validate all frozen slots and compute only prespecified contrasts."""

    root = Path(campaign_directory)
    try:
        if root.is_symlink() or not root.is_dir():
            _incomplete("campaign result directory is absent")
        allowed_root_files = {
            CAMPAIGN_AUTHORIZATION_FILENAME,
            CAMPAIGN_CONTRACT_FILENAME,
            CAMPAIGN_LEDGER_FILENAME,
            CAMPAIGN_SUMMARY_FILENAME,
        }
        for path in root.iterdir():
            if path.is_symlink():
                _fail(f"campaign root contains a symlink: {path.name}")
            if path.is_dir() and path.name.startswith("slot-"):
                continue
            if path.is_dir() and path.name == BUILD_EVIDENCE_DIRECTORY:
                continue
            if path.is_file() and path.name in allowed_root_files:
                continue
            _fail(f"campaign root contains an unexpected artifact: {path.name}")
        slot_directories = tuple(
            sorted(path for path in root.iterdir() if path.name.startswith("slot-"))
        )
        if not slot_directories:
            _incomplete("campaign contains no preserved slot directories")
        first_manifest = _read_bytes(slot_directories[0], MANIFEST_FILENAME)
        assert first_manifest is not None
        manifest = load_frozen_manifest_bytes(first_manifest)
        loaded_plan = _read_json(slot_directories[0], PLAN_FILENAME)
        loaded_runtime = _read_json(slot_directories[0], RUNTIME_FILENAME)
        assert loaded_plan is not None and loaded_runtime is not None
        plan, _ = loaded_plan
        runtime, _ = loaded_runtime
        validate_schedule_document(plan, manifest)
        expected_slots = _expected_slots(manifest)
        expected_by_id = {slot.slot_id: slot for slot in expected_slots}
        actual_by_id: dict[str, Path] = {}
        for path in slot_directories:
            if path.is_symlink() or not path.is_dir() or path.name in actual_by_id:
                _fail("campaign contains a duplicate/non-directory slot artifact")
            actual_by_id[path.name] = path
        extras = set(actual_by_id) - set(expected_by_id)
        if extras:
            _fail(f"campaign contains non-frozen slot IDs: {sorted(extras)}")
        results: list[SlotValidationResult] = []
        for expected in expected_slots:
            path = actual_by_id.get(expected.slot_id)
            if path is None:
                results.append(
                    SlotValidationResult(
                        slot_id=expected.slot_id,
                        outcome="INCOMPLETE",
                        reason="prespecified slot directory is absent; retries/replacements forbidden",
                        block_id=expected.block_id,
                        arm_code=expected.arm_code,
                        replica_count=expected.replica_count,
                        initial_fanout=expected.initial_fanout,
                    )
                )
            else:
                results.append(validate_slot(path))
        by_id = {result.slot_id: result for result in results}
        effects = _headline_effects(manifest, by_id)
        outcome = (
            "FAIL"
            if any(result.outcome == "FAIL" for result in results)
            else "INCOMPLETE"
            if any(result.outcome != "PASS" for result in results)
            else "PASS"
        )
        reason = None
        if outcome != "PASS":
            counts = {
                state: sum(result.outcome == state for result in results)
                for state in ("PASS", "FAIL", "INCOMPLETE")
            }
            reason = f"campaign slot outcomes: {counts}"
        coverage = tuple(
            sorted(
                {
                    (result.replica_count, result.initial_fanout, result.arm_code)
                    for result in results
                    if result.figure_eligible
                    and result.replica_count is not None
                    and result.initial_fanout is not None
                    and result.arm_code is not None
                }
            )
        )
        figure_eligible = outcome == "PASS" and effects is not None
        _validate_campaign_execution_ledger(
            root,
            manifest=manifest,
            plan=plan,
            runtime=runtime,
            expected_slots=expected_slots,
            actual_by_id=actual_by_id,
            results_by_id=by_id,
            campaign_outcome=outcome,
        )
        return CampaignValidationResult(
            outcome=outcome,
            reason=reason,
            slots=tuple(results),
            parameter_coverage=coverage,
            headline_effects=effects,
            figure_eligible=figure_eligible,
        )
    except _Incomplete as error:
        return CampaignValidationResult(
            outcome="INCOMPLETE",
            reason=str(error),
            slots=(),
            parameter_coverage=(),
            headline_effects=None,
            figure_eligible=False,
        )
    except Exception as error:
        return CampaignValidationResult(
            outcome="FAIL",
            reason=str(error),
            slots=(),
            parameter_coverage=(),
            headline_effects=None,
            figure_eligible=False,
        )


__all__ = (
    "ARTIFACT_SCHEMA_VERSION",
    "CUTOFF_NAMES",
    "CUTOFF_RULE",
    "MANAGER_EVENTS_FILENAME",
    "MANIFEST_FILENAME",
    "OUTCOME_FILENAME",
    "OUTCOMES",
    "PHASES",
    "PHASE_CUTOFFS_FILENAME",
    "PLAN_FILENAME",
    "REPLICA_EVENTS_PATTERN",
    "REPLICA_STDERR_PATTERN",
    "RUNTIME_FILENAME",
    "SLOT_FILENAME",
    "THROUGHPUT_FILENAME",
    "CampaignValidationResult",
    "DecodedBundle",
    "FactorialEffects",
    "FactorialValidationError",
    "FaultMarker",
    "MatchedEstimate",
    "PhaseMetric",
    "SlotValidationResult",
    "Tree",
    "decode_epoch_change_bundle",
    "derive_actor_ids",
    "fnv1a_rotating_actor",
    "validate_campaign",
    "validate_fault_causality",
    "validate_manager_blinding",
    "validate_schedule_document",
    "validate_shape_decision",
    "validate_slot",
    "validate_throughput_document",
)
