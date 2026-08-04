"""Frozen N=31 post-QC audit pilot and source-blind validator.

The validator deliberately classifies replica-authenticated audit records before
it is allowed to inspect the arm declaration.  Manager observations are not an
input to this module.  A single pilot can validate the harness contract, but it
is never campaign or figure evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any


class N31PostQcAuditError(ValueError):
    """Raised when the frozen contract or evidence is incomplete or invalid."""


SHIPPED_PROFILE_ID = "n31-f5-q21-post-qc-audit-v3"
SHIPPED_PROFILE_SHA256 = (
    "84039370562a7846efdc5d09e66b1096a6e6252fd1b6b49fe42fd7299800cff5"
)
SCENARIO = "n31-post-qc-audit-v3"
ARM_FALSE_REPORT = "static_authenticated_false_report"
ARM_OMISSION = "static_persistent_direct_vote_omission"
ARM_SHAM = "static_authenticated_sham"
ARM_NAMES = (ARM_FALSE_REPORT, ARM_OMISSION, ARM_SHAM)
# Execution is deliberately fixed and differs from the profile's canonical arm
# table.  Results never influence which arm runs next.
PILOT_EXECUTION_ORDER = (ARM_SHAM, ARM_FALSE_REPORT, ARM_OMISSION)
FULL_WITNESS_SIGNERS = (0, 5, 6, 7, 8, 9)
OMISSION_WITNESS_SIGNERS = (0, 6, 7, 8, 9)
FIXED_COMMIT_WITNESSES = (
    1,
    2,
    3,
    4,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
)

_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOW = re.compile(r"^[A-Za-z0-9._-]+$")
_PROFILE_FIELDS = {
    "schema_version",
    "profile_id",
    "frozen",
    "replica_ids",
    "fault_threshold",
    "quorum",
    "commit_witnesses",
    "fanout",
    "topology_depth",
    "topology_shape",
    "pipeline_stretch",
    "epoch_number",
    "epoch_digest",
    "snapshot_seed",
    "diagnostic_window",
    "marker_clock",
    "clock_scope",
    "fault_lifecycle_start",
    "target_id",
    "reporter_id",
    "root_id",
    "phase",
    "post_qc_audit",
    "arms",
    "attempt_count",
    "retry_failed_attempts",
    "require_successor_activation",
    "tree_switch_period_blocks",
    "evidence_policy",
    "runtime_profile",
}
_PHASE_FIELDS = {
    "tree_id",
    "root_replica",
    "members_breadth_first",
    "reporter_subtree",
}
_AUDIT_FIELDS = {
    "deadline_ms",
    "root_retention_ms",
    "qc_snapshot_max_skew_ns",
    "expected_qc_signers",
    "context_limit",
    "selection",
    "deadline_tie_rule",
    "consensus_authority",
    "manager_observations",
    "automatic_retries",
    "outcome_scanning",
}
_ARM_FIELDS = {
    "name",
    "fault_id",
    "actor_replica_id",
    "forge_missing_claim",
    "omit_outbound_aggregate",
    "omit_outbound_direct_vote",
    "expected_source_blind_classification",
    "expected_witness_signers",
}
_POLICY_FIELDS = {
    "clean_baseline",
    "root_qc",
    "later_commit",
    "pilot_ceiling",
    "figure_eligibility",
    "non_inclusion_claim",
}
_RUNTIME_FIELDS = {"path", "profile_id", "sha256"}


@dataclass(frozen=True, slots=True)
class PqarArm:
    name: str
    fault_id: str
    actor_replica_id: int | None
    forge_missing_claim: bool
    omit_outbound_aggregate: bool
    omit_outbound_direct_vote: bool
    expected_classification: str
    expected_witness_signers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SourceBlindPqarContract:
    """Arm-free inputs available to the classifier."""

    reporter_id: int
    target_id: int
    root_id: int
    epoch_number: int
    tree_id: int
    epoch_digest: str
    diagnostic_window: str
    reporter_subtree: tuple[int, ...]
    quorum: int
    expected_qc_signers: tuple[int, ...]
    deadline_ms: int
    root_retention_ms: int


@dataclass(frozen=True, slots=True)
class FrozenPqarProfile:
    profile_id: str
    profile_sha256: str
    replica_ids: tuple[int, ...]
    fault_threshold: int
    quorum: int
    commit_witnesses: tuple[int, ...]
    fanout: int
    topology_depth: int
    topology_shape: tuple[int, ...]
    pipeline_stretch: int
    epoch_number: int
    epoch_digest: str
    snapshot_seed: int
    diagnostic_window: str
    marker_clock: str
    clock_scope: str
    fault_lifecycle_start: str
    target_id: int
    reporter_id: int
    root_id: int
    tree_id: int
    members_breadth_first: tuple[int, ...]
    reporter_subtree: tuple[int, ...]
    deadline_ms: int
    root_retention_ms: int
    qc_snapshot_max_skew_ns: int
    expected_qc_signers: tuple[int, ...]
    context_limit: int
    selection: str
    deadline_tie_rule: str
    arms: tuple[PqarArm, ...]
    attempt_count: int
    retry_failed_attempts: bool
    tree_switch_period_blocks: int
    runtime_profile_path: str
    runtime_profile_id: str
    runtime_profile_sha256: str

    def source_blind_contract(self) -> SourceBlindPqarContract:
        return SourceBlindPqarContract(
            self.reporter_id,
            self.target_id,
            self.root_id,
            self.epoch_number,
            self.tree_id,
            self.epoch_digest,
            self.diagnostic_window,
            self.reporter_subtree,
            self.quorum,
            self.expected_qc_signers,
            self.deadline_ms,
            self.root_retention_ms,
        )

    def arm(self, name: str) -> PqarArm:
        try:
            return next(arm for arm in self.arms if arm.name == name)
        except StopIteration as error:
            raise N31PostQcAuditError(f"unknown audit arm: {name}") from error


@dataclass(frozen=True, slots=True)
class AuditIdentity:
    reporter: int
    target: int
    root: int
    epoch: int
    tree: int
    epoch_digest: str
    block: str
    generation: int
    window: str


@dataclass(frozen=True, slots=True)
class AuditMarker:
    kind: str
    source_replica: int
    identity: AuditIdentity
    timestamp_ns: int
    fields: Mapping[str, str]
    line: str

    @property
    def signers(self) -> tuple[int, ...]:
        return _parse_signers(self.fields["signers"], f"{self.kind} signers")


@dataclass(frozen=True, slots=True)
class SourceBlindClassification:
    classification: str
    identity: AuditIdentity
    witness_signers: tuple[int, ...]
    qc_signers: tuple[int, ...]
    armed_ns: int
    deadline_ns: int | None
    target_arrival_ns: int | None
    claim_ns: int | None
    relay_sent_ns: int | None
    qc_published_ns: int
    root_received_ns: int | None
    root_verified_ns: int | None
    qc_to_audit_latency_ns: int | None
    audit_expiry_ns: int
    relay_wire_bytes: int
    root_wire_bytes: int
    frozen_qc_signers_before: tuple[int, ...]
    frozen_qc_signers_after: tuple[int, ...]
    frozen_qc_hash_before: str
    frozen_qc_hash_after: str
    root_context_generation: int
    markers: tuple[AuditMarker, ...]


@dataclass(frozen=True, slots=True)
class ConsensusEvidence:
    baseline_block: str
    baseline_commit_ns: int
    baseline_witnesses: tuple[int, ...]
    selected_block: str
    root_qc_ns: int
    root_qc_signers: tuple[int, ...]
    later_block: str
    later_commit_ns: int
    later_witnesses: tuple[int, ...]
    later_ancestry: tuple[str, ...]
    conflict_count: int
    restart_count: int
    retry_count: int
    unique_commit_buckets: tuple[tuple[int, str, int], ...]


@dataclass(frozen=True, slots=True)
class PqarValidation:
    verdict: str
    source_blind_classification: str
    arm: str
    identity: AuditIdentity
    witness_signers: tuple[int, ...]
    qc_signers: tuple[int, ...]
    armed_ns: int
    deadline_ns: int
    target_arrival_ns: int | None
    claim_deadline_ns: int | None
    claim_ns: int | None
    relay_sent_ns: int | None
    qc_published_ns: int
    root_received_ns: int | None
    root_verified_ns: int | None
    audit_expiry_ns: int
    qc_to_deadline_slack_ns: int
    target_to_deadline_slack_ns: int | None
    relay_to_root_latency_ns: int | None
    root_verification_latency_ns: int | None
    qc_to_audit_latency_ns: int | None
    relay_wire_bytes: int
    root_wire_bytes: int
    frozen_qc_signers_before: tuple[int, ...]
    frozen_qc_signers_after: tuple[int, ...]
    frozen_qc_hash_before: str
    frozen_qc_hash_after: str
    root_context_generation: int
    later_commit_ns: int
    expiry_to_later_commit_latency_ns: int
    unique_commit_buckets: tuple[tuple[int, str, int], ...]
    evidence_ceiling: str = "harness_validation_only"
    figure_eligible: bool = False


def _error(message: str) -> None:
    raise N31PostQcAuditError(message)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        _error(f"{label} must be an object with string keys")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _error(f"{label} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        _error(
            f"{label} fields drifted: missing={sorted(expected - set(value))}, "
            f"extra={sorted(set(value) - expected)}"
        )


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _error(f"{label} must be an integer")
    return value


def _integer_tuple(value: object, label: str) -> tuple[int, ...]:
    values = _sequence(value, label)
    parsed = tuple(_integer(item, label) for item in values)
    if len(parsed) != len(set(parsed)):
        _error(f"{label} must not contain duplicates")
    return parsed


def _strict_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        _error(f"{label} must be boolean")
    return value


def _hex256(value: object, label: str) -> str:
    if not isinstance(value, str) or _HEX_256.fullmatch(value) is None:
        _error(f"{label} must be a lowercase SHA-256 value")
    return value


def _parse_arms(value: object) -> tuple[PqarArm, ...]:
    raw_arms = _sequence(value, "audit arms")
    expected = (
        (
            ARM_FALSE_REPORT,
            "n31-pqar-forged-missing-claim-0-to-5",
            0,
            True,
            True,
            False,
            "false_reporter",
            FULL_WITNESS_SIGNERS,
        ),
        (
            ARM_OMISSION,
            "n31-pqar-direct-vote-omission-5-to-0",
            5,
            False,
            False,
            True,
            "omission_compatible",
            OMISSION_WITNESS_SIGNERS,
        ),
        (
            ARM_SHAM,
            "n31-pqar-sham-5-to-0",
            None,
            False,
            True,
            False,
            "sham",
            (),
        ),
    )
    if len(raw_arms) != len(expected):
        _error("profile must freeze exactly three audit arms")
    parsed: list[PqarArm] = []
    for index, (raw, frozen) in enumerate(zip(raw_arms, expected, strict=True)):
        arm = _mapping(raw, f"audit arm {index}")
        _exact_keys(arm, _ARM_FIELDS, f"audit arm {index}")
        observed = (
            arm.get("name"),
            arm.get("fault_id"),
            arm.get("actor_replica_id"),
            _strict_bool(arm.get("forge_missing_claim"), "forge flag"),
            _strict_bool(arm.get("omit_outbound_aggregate"), "aggregate omission flag"),
            _strict_bool(arm.get("omit_outbound_direct_vote"), "omission flag"),
            arm.get("expected_source_blind_classification"),
            _integer_tuple(arm.get("expected_witness_signers"), "witness signers"),
        )
        if observed != frozen:
            _error(f"audit arm {index} differs from its frozen contract")
        parsed.append(PqarArm(*observed))
    return tuple(parsed)


def load_frozen_profile(path: Path) -> FrozenPqarProfile:
    """Load the shipped profile while independently deriving every invariant."""

    try:
        raw_bytes = Path(path).read_bytes()
        decoded = json.loads(raw_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise N31PostQcAuditError("profile must be readable JSON") from error
    raw = _mapping(decoded, "profile")
    _exact_keys(raw, _PROFILE_FIELDS, "profile")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    if digest != SHIPPED_PROFILE_SHA256:
        _error("profile bytes differ from the reviewed frozen profile")
    if (
        raw.get("schema_version") != 1
        or raw.get("profile_id") != SHIPPED_PROFILE_ID
        or raw.get("frozen") is not True
    ):
        _error("profile identity, version, or frozen state drifted")

    replicas = _integer_tuple(raw.get("replica_ids"), "replica membership")
    fault_threshold = _integer(raw.get("fault_threshold"), "fault threshold")
    quorum = _integer(raw.get("quorum"), "quorum")
    witnesses = _integer_tuple(raw.get("commit_witnesses"), "commit witnesses")
    if replicas != tuple(range(31)):
        _error("replica membership must be canonical N=31")
    if (fault_threshold, quorum) != (10, 21) or quorum != 2 * fault_threshold + 1:
        _error("fault threshold and quorum must remain f=10 and Q=21")
    if witnesses != FIXED_COMMIT_WITNESSES or {0, 5} & set(witnesses):
        _error("fixed Q21 commit witnesses must exclude reporter 0 and target 5")

    fanout = _integer(raw.get("fanout"), "fanout")
    depth = _integer(raw.get("topology_depth"), "topology depth")
    shape = _integer_tuple(raw.get("topology_shape"), "topology shape")
    pipeline = _integer(raw.get("pipeline_stretch"), "pipeline stretch")
    if (fanout, depth, shape, pipeline) != (5, 2, (1, 5, 25), 2):
        _error("N31 geometry must remain fanout five, depth two, pipeline two")
    epoch = _integer(raw.get("epoch_number"), "epoch number")
    epoch_digest = _hex256(raw.get("epoch_digest"), "epoch digest")
    if epoch != 0 or epoch_digest != (
        "145fac093343fa9cff20fcf49d85ad5443e93db14146f7854b17e28cf44f6d7a"
    ):
        _error("audit must remain in the pinned epoch-zero definition")
    seed = _integer(raw.get("snapshot_seed"), "snapshot seed")
    window = raw.get("diagnostic_window")
    if seed != 41_719 or not isinstance(window, str) or not _WINDOW.fullmatch(window):
        _error("snapshot seed or diagnostic window drifted")
    if (
        window != "n31-epoch0-tree30-post-qc-audit-v3"
        or raw.get("marker_clock") != "CLOCK_MONOTONIC_RAW"
        or raw.get("clock_scope") != "single_host_shared_kernel"
        or raw.get("fault_lifecycle_start")
        != "after_clean_baseline_before_first_eligible_tree30_proposal"
    ):
        _error("audit window, clock, or lifecycle semantics drifted")

    target = _integer(raw.get("target_id"), "target replica")
    reporter = _integer(raw.get("reporter_id"), "reporter replica")
    root = _integer(raw.get("root_id"), "root replica")
    if (reporter, target, root) != (0, 5, 30):
        _error("audit roles must remain reporter 0, target 5, and root 30")
    phase = _mapping(raw.get("phase"), "phase")
    _exact_keys(phase, _PHASE_FIELDS, "phase")
    tree = _integer(phase.get("tree_id"), "tree id")
    phase_root = _integer(phase.get("root_replica"), "phase root")
    members = _integer_tuple(phase.get("members_breadth_first"), "tree membership")
    subtree = _integer_tuple(phase.get("reporter_subtree"), "reporter subtree")
    if tree != 30 or phase_root != root or members != replicas[30:] + replicas[:30]:
        _error("tree-30 breadth-first phase drifted")
    if subtree != FULL_WITNESS_SIGNERS:
        _error("reporter-0 subtree must remain exactly {0,5,6,7,8,9}")
    target_position = members.index(target)
    if members[(target_position - 1) // fanout] != reporter:
        _error("target 5 must remain a direct child of reporter 0")

    audit = _mapping(raw.get("post_qc_audit"), "post-QC audit")
    _exact_keys(audit, _AUDIT_FIELDS, "post-QC audit")
    deadline = _integer(audit.get("deadline_ms"), "audit deadline")
    retention = _integer(audit.get("root_retention_ms"), "root retention")
    snapshot_skew = _integer(audit.get("qc_snapshot_max_skew_ns"), "QC snapshot skew")
    expected_qc_signers = _integer_tuple(
        audit.get("expected_qc_signers"), "expected QC signers"
    )
    context_limit = _integer(audit.get("context_limit"), "context limit")
    selection = audit.get("selection")
    tie_rule = audit.get("deadline_tie_rule")
    if (deadline, retention, snapshot_skew, context_limit) != (150, 250, 1_000_000, 1):
        _error("deadline, retention, QC skew, or context limit drifted")
    if expected_qc_signers != tuple(
        replica for replica in replicas if replica not in set(subtree)
    ):
        _error("QC signers must be the exact 25-replica reporter-subtree complement")
    if (
        selection != "first_post_baseline_eligible_tree30_proposal"
        or tie_rule != "target_arrival_must_be_strictly_before_deadline"
        or audit.get("consensus_authority") != "none"
        or audit.get("manager_observations") != "forbidden"
        or audit.get("automatic_retries") != 0
        or audit.get("outcome_scanning") is not False
    ):
        _error("post-QC selection, authority, or retry policy drifted")

    arms = _parse_arms(raw.get("arms"))
    if (
        _integer(raw.get("attempt_count"), "attempt count") != 1
        or raw.get("retry_failed_attempts") is not False
        or raw.get("require_successor_activation") is not False
        or _integer(raw.get("tree_switch_period_blocks"), "tree switch period") != 1
    ):
        _error("pilot must remain one attempt with no retry or successor gate")
    policy = _mapping(raw.get("evidence_policy"), "evidence policy")
    _exact_keys(policy, _POLICY_FIELDS, "evidence policy")
    if dict(policy) != {
        "clean_baseline": "fixed_Q21_before_matching_action",
        "root_qc": "exact_25_signer_complement_before_audit_witness_or_sham_expiry",
        "later_commit": "fixed_Q21_preserved_ancestry_after_audit_expiry",
        "pilot_ceiling": "harness_validation_only",
        "figure_eligibility": "never_for_single_attempt_pilot",
        "non_inclusion_claim": "omission_compatible_only",
    }:
        _error("evidence policy drifted")
    runtime = _mapping(raw.get("runtime_profile"), "runtime profile")
    _exact_keys(runtime, _RUNTIME_FIELDS, "runtime profile")
    if dict(runtime) != {
        "path": "experiments/adaptive/profiles/n31-f5-internal1-crash-shakedown-v1.json",
        "profile_id": "n31-f5-q21-internal1-sigkill-shakedown-v1",
        "sha256": "0defdaa9b69c949365eea3b3029da75cee3ea8334f845401103e2f7af8507650",
    }:
        _error("runtime profile binding drifted")

    return FrozenPqarProfile(
        SHIPPED_PROFILE_ID,
        digest,
        replicas,
        fault_threshold,
        quorum,
        witnesses,
        fanout,
        depth,
        shape,
        pipeline,
        epoch,
        epoch_digest,
        seed,
        window,
        str(raw["marker_clock"]),
        str(raw["clock_scope"]),
        str(raw["fault_lifecycle_start"]),
        target,
        reporter,
        root,
        tree,
        members,
        subtree,
        deadline,
        retention,
        snapshot_skew,
        expected_qc_signers,
        context_limit,
        str(selection),
        str(tie_rule),
        arms,
        1,
        False,
        1,
        str(runtime["path"]),
        str(runtime["profile_id"]),
        str(runtime["sha256"]),
    )


def _configuration(profile: FrozenPqarProfile) -> str:
    return f"{profile.epoch_number}:{profile.tree_id}:{profile.epoch_digest}"


def build_launch_contract(profile: FrozenPqarProfile, *, arm: str) -> dict[str, object]:
    """Build exact per-replica native arguments for one declared pilot arm."""

    selected = profile.arm(arm)
    common = [
        "--experiment-post-qc-audit-configuration",
        _configuration(profile),
        "--experiment-post-qc-audit-window",
        profile.diagnostic_window,
        "--experiment-post-qc-audit-reporter",
        str(profile.reporter_id),
        "--experiment-post-qc-audit-target",
        str(profile.target_id),
        "--experiment-post-qc-audit-root",
        str(profile.root_id),
        "--experiment-post-qc-audit-deadline-ms",
        str(profile.deadline_ms),
        "--experiment-post-qc-audit-retention-ms",
        str(profile.root_retention_ms),
        "--experiment-post-qc-audit-context-limit",
        str(profile.context_limit),
    ]
    overlays = {replica: list(common) for replica in profile.replica_ids}
    if selected.forge_missing_claim:
        overlays[profile.reporter_id].append(
            "--experiment-post-qc-audit-forge-missing-claim"
        )
    if selected.omit_outbound_aggregate:
        overlays[profile.reporter_id].extend(
            [
                "--experiment-byzantine-configuration",
                _configuration(profile),
                "--experiment-byzantine-window",
                profile.diagnostic_window,
                "--experiment-omit-outbound-aggregate",
                "--experiment-byzantine-context-limit",
                "1",
            ]
        )
    if selected.omit_outbound_direct_vote:
        overlays[profile.target_id].extend(
            [
                "--experiment-byzantine-configuration",
                _configuration(profile),
                "--experiment-byzantine-window",
                profile.diagnostic_window,
                "--experiment-omit-outbound-direct-vote",
                "--experiment-byzantine-context-limit",
                "1",
            ]
        )
    return {
        "schema_version": 1,
        "scenario": SCENARIO,
        "profile_id": profile.profile_id,
        "profile_sha256": profile.profile_sha256,
        "arm": arm,
        "attempt_count": 1,
        "automatic_retries": 0,
        "outcome_scanning": False,
        "replica_arguments": {str(key): value for key, value in overlays.items()},
    }


_IDENTITY_FIELD_ORDER = (
    "reporter",
    "target",
    "root",
    "epoch",
    "tree",
    "epoch_digest",
    "block",
    "generation",
    "window",
)
_ROOT_IDENTITY_FIELD_ORDER = (
    *_IDENTITY_FIELD_ORDER[:-1],
    "context_generation",
    "window",
)
_MARKER_FIELD_ORDER = {
    "missing_claim": (
        "claim",
        *_IDENTITY_FIELD_ORDER,
        "armed_ns",
        "deadline_ns",
        "emitted_ns",
        "signers",
    ),
    "target_verified": (
        "phase",
        *_IDENTITY_FIELD_ORDER,
        "armed_ns",
        "deadline_ns",
        "arrival_ns",
        "signers",
    ),
    "relay_sent": (
        *_IDENTITY_FIELD_ORDER,
        "deadline_ns",
        "sent_ns",
        "signers",
        "wire_bytes",
    ),
    "root_prepared": (
        "phase",
        *_ROOT_IDENTITY_FIELD_ORDER,
        "prepared_ns",
        "qc_signers",
        "qc_fingerprint",
    ),
    "root_snapshot": (
        "phase",
        *_ROOT_IDENTITY_FIELD_ORDER,
        "prepared_ns",
        "qc_published_ns",
        "retention_deadline_ns",
        "qc_signers",
        "qc_fingerprint",
        "consensus_context",
        "qc_unchanged",
    ),
    "root_witness": (
        "phase",
        *_ROOT_IDENTITY_FIELD_ORDER,
        "prepared_ns",
        "qc_published_ns",
        "received_ns",
        "verified_ns",
        "retention_deadline_ns",
        "deadline_ns",
        "signers",
        "qc_signers",
        "wire_bytes",
        "qc_fingerprint",
        "consensus_context",
        "qc_unchanged",
    ),
}


def _parse_uint(value: str, label: str) -> int:
    if not value.isascii() or not value.isdecimal():
        _error(f"{label} must be an unsigned decimal integer")
    return int(value)


def _parse_signers(value: str, label: str) -> tuple[int, ...]:
    if value == "-":
        return ()
    parts = value.split(",")
    parsed = tuple(_parse_uint(part, label) for part in parts)
    if len(parsed) != len(set(parsed)) or tuple(sorted(parsed)) != parsed:
        _error(f"{label} must be sorted and unique")
    if any(replica < 0 or replica > 30 for replica in parsed):
        _error(f"{label} contains a replica outside N=31")
    return parsed


def _parse_marker_line(source_replica: int, line: str) -> AuditMarker | None:
    prefix = "KAURI_AUDIT "
    position = line.find(prefix)
    if position < 0:
        return None
    payload = line[position + len(prefix) :].strip()
    tokens = payload.split()
    if not tokens or tokens[0] not in _MARKER_FIELD_ORDER:
        _error("unknown KAURI_AUDIT marker")
    kind = tokens[0]
    fields: dict[str, str] = {}
    for token in tokens[1:]:
        key, separator, value = token.partition("=")
        if not separator or not key or not value or key in fields:
            _error(f"malformed or duplicate field in {kind} marker")
        fields[key] = value
    observed_order = tuple(fields)
    expected_order = _MARKER_FIELD_ORDER[kind]
    if observed_order != expected_order:
        _error(
            f"{kind} marker fields or order drifted: "
            f"expected={list(expected_order)}, observed={list(observed_order)}"
        )
    identity = AuditIdentity(
        _parse_uint(fields["reporter"], "reporter"),
        _parse_uint(fields["target"], "target"),
        _parse_uint(fields["root"], "root"),
        _parse_uint(fields["epoch"], "epoch"),
        _parse_uint(fields["tree"], "tree"),
        _hex256(fields["epoch_digest"], "epoch digest"),
        _hex256(fields["block"], "block"),
        _parse_uint(fields["generation"], "generation"),
        fields["window"],
    )
    timestamp_field = {
        "missing_claim": "emitted_ns",
        "target_verified": "arrival_ns",
        "relay_sent": "sent_ns",
    }.get(kind)
    if timestamp_field is None:
        timestamp_field = {
            "root_prepared": "prepared_ns",
            "root_snapshot": "qc_published_ns",
            "root_witness": "verified_ns",
        }[kind]
    timestamp = _parse_uint(fields[timestamp_field], f"{kind} timestamp")
    if "signers" in fields:
        _parse_signers(fields["signers"], f"{kind} signers")
    if kind in {"root_prepared", "root_snapshot", "root_witness"}:
        _parse_uint(fields["context_generation"], "root context generation")
        _parse_signers(fields["qc_signers"], "root QC signers")
    if kind in {"root_prepared", "root_snapshot", "root_witness"}:
        _hex256(fields["qc_fingerprint"], "QC fingerprint")
    return AuditMarker(kind, source_replica, identity, timestamp, fields, line.rstrip())


def parse_replica_audit_logs(
    replica_logs: Mapping[int, str | Sequence[str]],
) -> tuple[AuditMarker, ...]:
    """Parse only authenticated replica log sources; manager logs are invalid."""

    if not isinstance(replica_logs, Mapping) or not replica_logs:
        _error("replica audit logs must be a non-empty mapping")
    markers: list[AuditMarker] = []
    for source, content in replica_logs.items():
        if (
            isinstance(source, bool)
            or not isinstance(source, int)
            or source not in range(31)
        ):
            _error("audit source must be a replica id in N=31")
        if isinstance(content, str):
            lines = content.splitlines()
        elif isinstance(content, Sequence) and all(
            isinstance(line, str) for line in content
        ):
            lines = list(content)
        else:
            _error(f"replica {source} audit log must contain text lines")
        for line in lines:
            marker = _parse_marker_line(source, line)
            if marker is not None:
                markers.append(marker)
    markers.sort(key=lambda marker: (marker.timestamp_ns, marker.source_replica))
    return tuple(markers)


def _one(markers: Sequence[AuditMarker], kind: str) -> AuditMarker:
    selected = [marker for marker in markers if marker.kind == kind]
    if len(selected) != 1:
        _error(f"audit requires exactly one {kind} marker, found {len(selected)}")
    return selected[0]


def classify_source_blind(
    profile: SourceBlindPqarContract,
    replica_logs: Mapping[int, str | Sequence[str]],
    *,
    clean_boundary_ns: int,
) -> SourceBlindClassification:
    """Classify authenticated records without accepting arm ground truth."""

    if type(profile) is not SourceBlindPqarContract:
        _error("classifier requires the exact arm-free source-blind contract")
    if isinstance(clean_boundary_ns, bool) or clean_boundary_ns < 0:
        _error("clean boundary must be a non-negative monotonic timestamp")
    markers = parse_replica_audit_logs(replica_logs)
    if not markers:
        _error("no replica-authenticated KAURI_AUDIT markers were found")
    if any(marker.timestamp_ns <= clean_boundary_ns for marker in markers):
        _error("audit marker did not occur strictly after the clean boundary")
    identities = {marker.identity for marker in markers}
    if len(identities) != 1:
        _error("multiple audit identities reveal substitution or outcome scanning")
    identity = next(iter(identities))
    if (
        identity.reporter,
        identity.target,
        identity.root,
        identity.epoch,
        identity.tree,
        identity.epoch_digest,
        identity.window,
    ) != (
        profile.reporter_id,
        profile.target_id,
        profile.root_id,
        profile.epoch_number,
        profile.tree_id,
        profile.epoch_digest,
        profile.diagnostic_window,
    ):
        _error("audit identity differs from the frozen reporter/target/root phase")
    if identity.generation <= 0:
        _error("audit context generation must be positive")

    missing = [marker for marker in markers if marker.kind == "missing_claim"]
    targets = [marker for marker in markers if marker.kind == "target_verified"]
    relays = [marker for marker in markers if marker.kind == "relay_sent"]
    prepared_markers = [marker for marker in markers if marker.kind == "root_prepared"]
    snapshots = [marker for marker in markers if marker.kind == "root_snapshot"]
    witnesses = [marker for marker in markers if marker.kind == "root_witness"]
    if any(
        marker.source_replica != profile.reporter_id
        for marker in missing + targets + relays
    ):
        _error("reporter audit markers came from the wrong replica log")
    if any(
        marker.source_replica != profile.root_id
        for marker in prepared_markers + snapshots + witnesses
    ):
        _error("root audit marker came from the wrong replica log")
    root_context_generations = {
        _parse_uint(marker.fields["context_generation"], "root context generation")
        for marker in prepared_markers + snapshots + witnesses
    }
    if len(root_context_generations) != 1 or next(iter(root_context_generations)) <= 0:
        _error("root audit markers do not share one positive context generation")
    root_context_generation = next(iter(root_context_generations))
    if len(targets) > 1 or any(
        marker.fields["phase"] not in {"open", "post_close"} for marker in targets
    ):
        _error("audit permits at most one valid target marker")
    if any(
        profile.target_id not in marker.signers
        or not set(marker.signers).issubset(profile.reporter_subtree)
        for marker in targets
    ):
        _error("target marker signer set is not a verified reporter-subtree subset")
    target_arrival_ns = targets[0].timestamp_ns if targets else None

    prepared = _one(prepared_markers, "root_prepared")
    snapshot = _one(snapshots, "root_snapshot")
    if prepared.fields["phase"] != "pre_qc":
        _error("root_prepared must describe the pre_qc frozen candidate")
    if snapshot.fields["phase"] != "post_qc":
        _error("root_snapshot must describe the post_qc terminal certificate")
    prepared_ns = _parse_uint(prepared.fields["prepared_ns"], "QC prepare time")
    snapshot_prepared_ns = _parse_uint(
        snapshot.fields["prepared_ns"], "snapshot prepare time"
    )
    qc_published_ns = _parse_uint(
        snapshot.fields["qc_published_ns"], "root QC publication time"
    )
    expiry_ns = _parse_uint(
        snapshot.fields["retention_deadline_ns"], "root retention deadline"
    )
    pre_qc_signers = _parse_signers(
        prepared.fields["qc_signers"], "prepared QC signers"
    )
    qc_signers = _parse_signers(snapshot.fields["qc_signers"], "root QC signers")
    pre_qc_fingerprint = _hex256(
        prepared.fields["qc_fingerprint"], "prepared QC fingerprint"
    )
    post_qc_fingerprint = _hex256(
        snapshot.fields["qc_fingerprint"], "published QC fingerprint"
    )
    if (
        snapshot.fields["consensus_context"] != "terminal"
        or snapshot.fields["qc_unchanged"] != "1"
        or prepared_ns != snapshot_prepared_ns
        or prepared_ns > qc_published_ns
        or expiry_ns != qc_published_ns + profile.root_retention_ms * 1_000_000
        or expiry_ns <= qc_published_ns
        or pre_qc_signers != qc_signers
        or pre_qc_fingerprint != post_qc_fingerprint
        or qc_signers != profile.expected_qc_signers
    ):
        _error("frozen QC changed or its publication/retention order drifted")

    armed_values = {
        _parse_uint(marker.fields["armed_ns"], "armed time")
        for marker in missing + targets
    }
    deadline_values = {
        _parse_uint(marker.fields["deadline_ns"], "audit deadline")
        for marker in missing + targets + relays + witnesses
    }
    if len(armed_values) != 1 or len(deadline_values) != 1:
        _error("audit markers do not share one armed time and deadline")
    armed_ns = next(iter(armed_values))
    frozen_deadline_ns = next(iter(deadline_values))
    if armed_ns <= clean_boundary_ns:
        _error("audit armed at or before the clean baseline boundary")
    if frozen_deadline_ns != armed_ns + profile.deadline_ms * 1_000_000:
        _error("effective audit deadline differs from the frozen 150 ms")
    if not armed_ns <= prepared_ns <= qc_published_ns < frozen_deadline_ns:
        _error("single-host audit chronology must be armed<=prepared<=QC<deadline")

    if not missing:
        if relays or witnesses:
            _error("sham classification requires zero relay and audit witness")
        target = _one(targets, "target_verified")
        if (
            target.fields["phase"] not in {"open", "post_close"}
            or target.timestamp_ns >= frozen_deadline_ns
            or profile.target_id not in target.signers
            or not set(target.signers).issubset(profile.reporter_subtree)
        ):
            _error("no-claim record lacks a timely authenticated target")
        classification = "sham"
        witness_signers = ()
        deadline_ns = frozen_deadline_ns
        claim_ns = None
        relay_sent_ns = None
        relay_wire_bytes = 0
        root_wire_bytes = 0
        received_ns = None
        verified_ns = None
        qc_to_audit_latency_ns = None
    else:
        claim = _one(missing, "missing_claim")
        if claim.fields["claim"] != "missing_target":
            _error("missing claim is not the frozen missing_target assertion")
        relay = _one(relays, "relay_sent")
        post_qc = _one(witnesses, "root_witness")
        if post_qc.fields["phase"] != "post_qc":
            _error("root_witness must be post_qc")
        witness_signers = post_qc.signers
        witness_qc_signers = _parse_signers(
            post_qc.fields["qc_signers"], "witness QC signers"
        )
        witness_qc_fingerprint = _hex256(
            post_qc.fields["qc_fingerprint"], "witness QC fingerprint"
        )
        witness_prepared_ns = _parse_uint(
            post_qc.fields["prepared_ns"], "witness prepare time"
        )
        witness_published_ns = _parse_uint(
            post_qc.fields["qc_published_ns"], "witness QC publication time"
        )
        witness_expiry_ns = _parse_uint(
            post_qc.fields["retention_deadline_ns"], "witness retention deadline"
        )
        received_ns = _parse_uint(post_qc.fields["received_ns"], "root receive time")
        verified_ns = _parse_uint(post_qc.fields["verified_ns"], "root verify time")
        root_wire_bytes = _parse_uint(post_qc.fields["wire_bytes"], "root wire bytes")
        if (
            post_qc.fields["consensus_context"] != "terminal"
            or post_qc.fields["qc_unchanged"] != "1"
            or witness_qc_signers != qc_signers
            or witness_qc_fingerprint != post_qc_fingerprint
            or witness_prepared_ns != prepared_ns
            or witness_published_ns != qc_published_ns
            or witness_expiry_ns != expiry_ns
            or not (qc_published_ns <= received_ns <= verified_ns < expiry_ns)
            or root_wire_bytes <= 0
        ):
            _error("post-QC witness mutated the QC or violated audit ordering")
        deadline_ns = _parse_uint(claim.fields["deadline_ns"], "claim deadline")
        relay_deadline = _parse_uint(relay.fields["deadline_ns"], "relay deadline")
        if deadline_ns != relay_deadline:
            _error("claim and relay deadlines differ")
        if claim.timestamp_ns < deadline_ns or relay.timestamp_ns < claim.timestamp_ns:
            _error("missing claim or relay occurred before its eligible time")
        if claim.signers != relay.signers or relay.signers != witness_signers:
            _error("deadline claim, relay, and root witness signer sets differ")
        if not set(claim.signers).issubset(profile.reporter_subtree):
            _error("claim signer set lies outside the reporter subtree")
        relay_wire_bytes = _parse_uint(relay.fields["wire_bytes"], "relay wire bytes")
        if relay_wire_bytes <= 0 or relay_wire_bytes != root_wire_bytes:
            _error("relay/root wire byte accounting is absent or inconsistent")
        if relay.timestamp_ns > received_ns:
            _error("relay was sent after root receipt or audit expiry")
        timely_targets = [
            marker
            for marker in targets
            if marker.fields["phase"] in {"open", "post_close"}
            and marker.timestamp_ns < deadline_ns
        ]
        if (
            witness_signers == FULL_WITNESS_SIGNERS
            and profile.target_id in witness_signers
        ):
            if len(timely_targets) != 1:
                _error(
                    "false-report classification requires one timely verified target"
                )
            classification = "false_reporter"
        elif witness_signers == OMISSION_WITNESS_SIGNERS:
            # A target at or after the deadline is deliberately not accepted as
            # evidence for the strict-before-deadline false-report class.
            if timely_targets:
                _error("omission-compatible record contains a timely target")
            classification = "omission_compatible"
        else:
            _error("root witness signer set is outside the two frozen classes")
        claim_ns = claim.timestamp_ns
        relay_sent_ns = relay.timestamp_ns
        qc_to_audit_latency_ns = verified_ns - qc_published_ns

    return SourceBlindClassification(
        classification=classification,
        identity=identity,
        witness_signers=witness_signers,
        qc_signers=qc_signers,
        armed_ns=armed_ns,
        deadline_ns=deadline_ns,
        target_arrival_ns=target_arrival_ns,
        claim_ns=claim_ns,
        relay_sent_ns=relay_sent_ns,
        qc_published_ns=qc_published_ns,
        root_received_ns=received_ns,
        root_verified_ns=verified_ns,
        qc_to_audit_latency_ns=qc_to_audit_latency_ns,
        audit_expiry_ns=expiry_ns,
        relay_wire_bytes=relay_wire_bytes,
        root_wire_bytes=root_wire_bytes,
        frozen_qc_signers_before=pre_qc_signers,
        frozen_qc_signers_after=qc_signers,
        frozen_qc_hash_before=pre_qc_fingerprint,
        frozen_qc_hash_after=post_qc_fingerprint,
        root_context_generation=root_context_generation,
        markers=markers,
    )


def validate_ground_truth(
    profile: FrozenPqarProfile,
    *,
    arm: str,
    classification: SourceBlindClassification,
) -> None:
    """Compare ground truth only after source-blind classification has returned."""

    selected = profile.arm(arm)
    if classification.classification != selected.expected_classification:
        _error("source-blind classification differs from the declared arm")
    if classification.witness_signers != selected.expected_witness_signers:
        _error("authenticated witness set differs from the declared arm")


def validate_consensus_evidence(
    profile: FrozenPqarProfile,
    classification: SourceBlindClassification,
    evidence: ConsensusEvidence,
) -> None:
    """Bind the audit to a clean Q21 baseline, terminal QC, and later ancestry."""

    if evidence.baseline_witnesses != profile.commit_witnesses:
        _error("clean baseline lacks the fixed Q21 witness set")
    if evidence.later_witnesses != profile.commit_witnesses:
        _error("later commit lacks the fixed Q21 witness set")
    if evidence.baseline_commit_ns >= min(
        marker.timestamp_ns for marker in classification.markers
    ):
        _error("clean baseline does not precede the audit action")
    if evidence.selected_block != classification.identity.block:
        _error("terminal QC does not identify the selected audit proposal")
    if evidence.root_qc_signers != classification.qc_signers:
        _error("structured root QC signers differ from the replica audit record")
    if len(evidence.root_qc_signers) < profile.quorum:
        _error("structured evidence lacks a Q21 root QC")
    qc_snapshot_skew = classification.qc_published_ns - evidence.root_qc_ns
    if not 0 <= qc_snapshot_skew <= profile.qc_snapshot_max_skew_ns:
        _error("structured QC event exceeds the frozen root_snapshot skew bound")
    post_qc = [
        marker
        for marker in classification.markers
        if marker.kind == "root_witness" and marker.fields["phase"] == "post_qc"
    ]
    if classification.classification == "sham":
        if post_qc:
            _error("sham unexpectedly contains a post-QC relay witness")
    elif len(post_qc) != 1 or evidence.root_qc_ns > post_qc[0].timestamp_ns:
        _error("post-QC witness is absent or precedes the terminal QC")
    if evidence.later_commit_ns <= classification.audit_expiry_ns:
        _error("later Q21 commit does not occur after audit expiry")
    if classification.identity.block not in evidence.later_ancestry:
        _error("later Q21 commit does not preserve selected-block ancestry")
    if evidence.later_block == evidence.selected_block:
        _error("later commit must be distinct from the selected proposal")
    if any(
        value != 0
        for value in (
            evidence.conflict_count,
            evidence.restart_count,
            evidence.retry_count,
        )
    ):
        _error("audit evidence contains a conflict, restart, or retry")
    if not evidence.unique_commit_buckets:
        _error("unique authoritative commit buckets are missing")
    bucket_keys = [
        (height, block) for height, block, _batch in evidence.unique_commit_buckets
    ]
    if len(bucket_keys) != len(set(bucket_keys)):
        _error("authoritative commit buckets contain duplicate height/hash keys")
    if any(
        height < 0
        or not isinstance(batch, int)
        or batch < 0
        or _HEX_256.fullmatch(block) is None
        for height, block, batch in evidence.unique_commit_buckets
    ):
        _error("authoritative commit bucket is malformed")


def validate_pilot(
    profile: FrozenPqarProfile,
    replica_logs: Mapping[int, str | Sequence[str]],
    *,
    clean_boundary_ns: int,
    arm: str,
    consensus: ConsensusEvidence,
) -> PqarValidation:
    """Validate one pilot without ever promoting it to campaign evidence."""

    classification = classify_source_blind(
        profile.source_blind_contract(),
        replica_logs,
        clean_boundary_ns=clean_boundary_ns,
    )
    validate_ground_truth(
        profile,
        arm=arm,
        classification=classification,
    )
    validate_consensus_evidence(profile, classification, consensus)
    deadline_ns = classification.deadline_ns
    if deadline_ns is None:
        _error("validated audit classification is missing its frozen deadline")
    qc_to_deadline_slack_ns = deadline_ns - classification.qc_published_ns
    if qc_to_deadline_slack_ns <= 0:
        _error("validated QC does not precede the frozen audit deadline")
    target_to_deadline_slack_ns = (
        deadline_ns - classification.target_arrival_ns
        if classification.target_arrival_ns is not None
        else None
    )
    relay_to_root_latency_ns = (
        classification.root_received_ns - classification.relay_sent_ns
        if classification.root_received_ns is not None
        and classification.relay_sent_ns is not None
        else None
    )
    root_verification_latency_ns = (
        classification.root_verified_ns - classification.root_received_ns
        if classification.root_verified_ns is not None
        and classification.root_received_ns is not None
        else None
    )
    if relay_to_root_latency_ns is not None and relay_to_root_latency_ns < 0:
        _error("validated relay-to-root latency is negative")
    if root_verification_latency_ns is not None and root_verification_latency_ns < 0:
        _error("validated root verification latency is negative")
    expiry_to_later_commit_latency_ns = (
        consensus.later_commit_ns - classification.audit_expiry_ns
    )
    if expiry_to_later_commit_latency_ns <= 0:
        _error("validated later commit does not follow audit expiry")
    return PqarValidation(
        verdict="PASS",
        source_blind_classification=classification.classification,
        arm=arm,
        identity=classification.identity,
        witness_signers=classification.witness_signers,
        qc_signers=classification.qc_signers,
        armed_ns=classification.armed_ns,
        deadline_ns=deadline_ns,
        target_arrival_ns=classification.target_arrival_ns,
        claim_deadline_ns=deadline_ns,
        claim_ns=classification.claim_ns,
        relay_sent_ns=classification.relay_sent_ns,
        qc_published_ns=classification.qc_published_ns,
        root_received_ns=classification.root_received_ns,
        root_verified_ns=classification.root_verified_ns,
        audit_expiry_ns=classification.audit_expiry_ns,
        qc_to_deadline_slack_ns=qc_to_deadline_slack_ns,
        target_to_deadline_slack_ns=target_to_deadline_slack_ns,
        relay_to_root_latency_ns=relay_to_root_latency_ns,
        root_verification_latency_ns=root_verification_latency_ns,
        qc_to_audit_latency_ns=classification.qc_to_audit_latency_ns,
        relay_wire_bytes=classification.relay_wire_bytes,
        root_wire_bytes=classification.root_wire_bytes,
        frozen_qc_signers_before=classification.frozen_qc_signers_before,
        frozen_qc_signers_after=classification.frozen_qc_signers_after,
        frozen_qc_hash_before=classification.frozen_qc_hash_before,
        frozen_qc_hash_after=classification.frozen_qc_hash_after,
        root_context_generation=classification.root_context_generation,
        later_commit_ns=consensus.later_commit_ns,
        expiry_to_later_commit_latency_ns=expiry_to_later_commit_latency_ns,
        unique_commit_buckets=consensus.unique_commit_buckets,
    )


__all__ = [
    "ARM_FALSE_REPORT",
    "ARM_NAMES",
    "ARM_OMISSION",
    "ARM_SHAM",
    "AuditIdentity",
    "AuditMarker",
    "ConsensusEvidence",
    "FIXED_COMMIT_WITNESSES",
    "FrozenPqarProfile",
    "FULL_WITNESS_SIGNERS",
    "N31PostQcAuditError",
    "OMISSION_WITNESS_SIGNERS",
    "PILOT_EXECUTION_ORDER",
    "PqarArm",
    "PqarValidation",
    "SCENARIO",
    "SHIPPED_PROFILE_ID",
    "SHIPPED_PROFILE_SHA256",
    "SourceBlindClassification",
    "SourceBlindPqarContract",
    "build_launch_contract",
    "classify_source_blind",
    "load_frozen_profile",
    "parse_replica_audit_logs",
    "validate_consensus_evidence",
    "validate_ground_truth",
    "validate_pilot",
]
