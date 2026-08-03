"""Frozen N=31 same-proposal signer diagnosis and source-blind validator.

Reporter 0's leaf-5 timeout is cross-checked against root 30's authenticated
aggregate for the exact same proposal.  A response-only projection remains
ambiguous; the complete signer set settles one of two declared adapter modes.
The result is a harness-validation pilot, never campaign or figure evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .faults import (
    FaultPlan,
    ScenarioContext,
    StaticAuthenticatedFalseReport,
    StaticPersistentDirectVoteOmission,
)

MANAGER_SOURCE_ID = "adaptive-manager"
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_EVENT_FIELDS = {
    "event_schema_version",
    "run_id",
    "source_kind",
    "source_id",
    "source_instance",
    "source_sequence",
    "source_monotonic_ns",
    "event_type",
    "payload",
}
_OBSERVATION_FIELDS = {
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
}
_CONFIGURATION_FIELDS = {
    "epoch_number",
    "tree_id",
    "epoch_digest",
}
_CONFIGURATION_ACTIVE_FIELDS = {
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
_COMMIT_OBSERVED_FIELDS = {
    "block_height",
    "block_hash",
    "parent_hash",
    "transaction_count",
    "commit_batch_index",
}
_COMMITTED_FIELDS = {
    *_COMMIT_OBSERVED_FIELDS,
    "designated_observer",
    "decision_proof",
    "view_generation",
}
_DECISION_PROOF_FIELDS = {
    "epoch_number",
    "tree_id",
    "epoch_digest",
    "block_hash",
}
_BOUNDARY_FIELDS = {
    "epoch_number",
    "tree_id",
    "root_replica",
    "epoch_digest",
    "global_quorum",
    "members_breadth_first",
    "replica_evidence",
}
_BOUNDARY_REFERENCE_FIELDS = {
    "source_id",
    "source_sequence",
    "source_monotonic_ns",
}
_CLEANUP_FIELDS = {
    "name",
    "replica_id",
    "pid",
    "pgid",
    "signals_sent",
    "returncode",
    "classification",
    "cleanup_errors",
    "cleanup_started_ns",
    "cleanup_started_after_post_window",
}
_MANIFEST_FIELDS = {
    "schema_version",
    "scenario",
    "run_id",
    "kauri_revision",
    "profile",
    "runtime_profile",
    "arm",
    "fault_plan_sha256",
    "attempt",
    "attempt_scope",
    "retry_policy",
    "complete",
    "started_utc",
    "finished_utc",
    "preflight",
    "runtime_artifacts",
    "source_instances",
    "sources",
    "actor_log",
    "authoritative_observer",
    "configuration_boundaries",
    "cleanup_ledger",
    "runtime_error",
}
_PREFLIGHT_FIELDS = {
    "schema_version",
    "scenario",
    "verdict",
    "diagnosis_profile",
    "runtime_profile",
    "revision",
    "commit_witnesses",
    "launch_contracts",
    "profiled_runtime",
}
_PROFILED_PREFLIGHT_FIELDS = {
    "schema_version",
    "verdict",
    "profile_id",
    "profile_sha256",
    "revision",
    "required_port_count",
    "fd_soft_limit",
    "free_disk_bytes",
    "clock",
    "build_provenance",
    "epoch_zero_witness",
    "executables",
}
_BUILD_PROVENANCE_FIELDS = {
    "schema_version",
    "revision",
    "repository",
    "build_directory",
    "cmake_cache_sha256",
    "build_command",
    "build_metadata",
    "binaries",
}
_BUILD_METADATA_NAMES = {
    "cmake_cache",
    "compile_commands",
    "hotstuff_app_link",
    "adaptation_manager_link",
    "epoch_profile_digest_link",
    "hotstuff_keygen_link",
    "hotstuff_tls_keygen_link",
}
_BINARY_NAMES = {
    "app",
    "manager",
    "keygen",
    "tls_keygen",
    "epoch_profile_digest",
}
_SOURCE_DESCRIPTOR_FIELDS = {
    "source_kind",
    "source_id",
    "source_instance",
    "pid",
    "pgid",
    "path",
    "sha256",
}
_RUNTIME_ARTIFACT_FIELDS = {"kind", "replica_id", "path", "sha256"}
_ACTOR_LOG_FIELDS = {"path", "sha256"}
_JOURNAL_FIELDS = {
    "fault_id",
    "lifecycle",
    "plan_sha256",
    "schema_version",
    "source_id",
    "source_monotonic_ns",
    "source_sequence",
}
AUTHORITATIVE_OBSERVER = 2


class N31StaticDiagnosisError(ValueError):
    """The frozen profile or supplied evidence fails its exact contract."""


def _error(message: str) -> None:
    raise N31StaticDiagnosisError(message)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        _error(f"{label} must be an array")
    return value


def _integer(value: object, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _error(f"{label} must be an integer")
    if value < (1 if positive else 0):
        _error(f"{label} is outside its allowed range")
    return value


def _hex256(value: object, label: str) -> str:
    if not isinstance(value, str) or _HEX_256.fullmatch(value) is None:
        _error(f"{label} must be lowercase 256-bit hex")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        _error(f"{label} schema drifted")


def _integer_tuple(value: object, label: str) -> tuple[int, ...]:
    values = _sequence(value, label)
    if any(isinstance(item, bool) or not isinstance(item, int) for item in values):
        _error(f"{label} must contain only integers")
    return tuple(values)


def _breadth_first_subtree(
    members: tuple[int, ...], fanout: int, target_id: int
) -> tuple[int, ...]:
    try:
        target_position = members.index(target_id)
    except ValueError as error:
        raise N31StaticDiagnosisError(
            "target is absent from breadth-first topology membership"
        ) from error
    pending = [target_position]
    result: list[int] = []
    while pending:
        position = pending.pop(0)
        result.append(members[position])
        first_child = position * fanout + 1
        pending.extend(range(first_child, min(first_child + fanout, len(members))))
    return tuple(result)


# ---------------------------------------------------------------------------
# Frozen signer-aware contract
# ---------------------------------------------------------------------------

SCENARIO = "n31-f5-same-context-signer-diagnosis"
SHIPPED_PROFILE_ID = "n31-f5-q21-signer-aware-diagnosis-v1"
SHIPPED_PROFILE_SHA256 = (
    "407b847fc5495433a2a5298f031020974e05be42d6a9b4542454d07d004d3b83"
)
ARM_NAMES = (
    "static_authenticated_false_report",
    "static_persistent_direct_vote_omission",
)
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
    "diagnostic_fault_bound",
    "snapshot_seed",
    "diagnostic_window",
    "diagnostic_mode",
    "marker_clock",
    "fault_lifecycle_start",
    "target_id",
    "reporter_id",
    "witness_reporter_id",
    "phase",
    "arms",
    "attempt_count",
    "retry_failed_attempts",
    "require_successor_activation",
    "tree_switch_period_blocks",
    "byzantine_context_limit",
    "signer_crosscheck",
    "evidence_policy",
    "runtime_profile",
}
_FAULT_OUTCOME_FIELDS = {
    "status",
    "fault_id",
    "kind",
    "source_id",
    "actor_replica_id",
    "reporter_id",
    "target_id",
    "configuration",
    "block_hash",
    "context_limit",
    "line",
    "log_path",
    "log_start_offset",
    "log_terminal_offset",
    "matching_line_count",
    "marker_monotonic_ns",
    "false_positive_suppression",
    "pair_observed_monotonic_ns",
    "diagnostic_certificate_sha256",
}
_SUPPRESSION_FIELDS = {
    "kind",
    "source_id",
    "reporter_id",
    "target_id",
    "configuration",
    "block_hash",
    "line",
    "marker_monotonic_ns",
}


@dataclass(frozen=True, slots=True)
class DiagnosticPhase:
    label: str
    tree_id: int
    root_replica: int
    reporter_id: int
    members_breadth_first: tuple[int, ...]
    reporter_subtree: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DiagnosticArm:
    name: str
    fault_id: str
    actor_replica_id: int
    runtime_marker: str
    positive_suppression_marker: str | None
    witness_signer_set: tuple[int, ...]
    settled_hypothesis_kind: str
    settled_hypothesis_replica_id: int

    @property
    def settled_hypothesis(self) -> dict[str, object]:
        return {
            "kind": self.settled_hypothesis_kind,
            "replica_id": self.settled_hypothesis_replica_id,
        }


@dataclass(frozen=True, slots=True)
class FrozenN31StaticDiagnosisProfile:
    profile_id: str
    profile_sha256: str
    frozen: bool
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
    diagnostic_fault_bound: int
    snapshot_seed: int
    diagnostic_window: str
    diagnostic_mode: str
    marker_clock: str
    fault_lifecycle_start: str
    target_id: int
    reporter_id: int
    witness_reporter_id: int
    phase: DiagnosticPhase
    arms: tuple[DiagnosticArm, ...]
    attempt_count: int
    retry_failed_attempts: bool
    require_successor_activation: bool
    tree_switch_period_blocks: int
    byzantine_context_limit: int
    classifier: str
    selection_order: str
    same_context_fields: tuple[str, ...]
    claim_expected_message_type: str
    witness_expected_message_type: str
    response_only_hypothesis_count: int
    signer_aware_hypothesis_count: int
    exact_reporter_subtree: tuple[int, ...]
    boundary_max_skew_ns: int
    clock_comparability: str
    limitation: str
    clean_baseline_policy: str
    later_commit_policy: str
    pilot_ceiling: str
    figure_eligibility: str
    runtime_profile_path: str
    runtime_profile_id: str
    runtime_profile_sha256: str

    @property
    def phases(self) -> tuple[DiagnosticPhase, ...]:
        """Compatibility view: the replacement contains exactly one phase."""

        return (self.phase,)

    @property
    def ordering_basis(self) -> str:
        return self.selection_order

    @property
    def expected_message_type(self) -> str:
        return self.claim_expected_message_type

    @property
    def expected_hypothesis_counts(self) -> tuple[int, int]:
        return (
            self.response_only_hypothesis_count,
            self.signer_aware_hypothesis_count,
        )

    @property
    def diagnostic_projection_label(self) -> str:
        return (
            "response-only projection intentionally omits signer_set and "
            "retains both declared hypotheses"
        )


def _parse_signer_aware_phase(
    value: object,
    *,
    membership: tuple[int, ...],
    fanout: int,
    reporter_id: int,
) -> DiagnosticPhase:
    phase = _mapping(value, "diagnostic phase")
    _exact_keys(
        phase,
        {
            "tree_id",
            "root_replica",
            "members_breadth_first",
            "reporter_subtree",
        },
        "diagnostic phase",
    )
    tree_id = _integer(phase.get("tree_id"), "diagnostic tree id")
    root_replica = _integer(phase.get("root_replica"), "diagnostic root")
    if tree_id != 30 or root_replica != 30:
        _error("signer-aware diagnosis must use root/tree 30")
    members = _integer_tuple(
        phase.get("members_breadth_first"),
        "diagnostic breadth-first membership",
    )
    if members != membership[30:] + membership[:30]:
        _error("tree-30 breadth-first membership drifted")
    reporter_subtree = _integer_tuple(phase.get("reporter_subtree"), "reporter subtree")
    if reporter_subtree != _breadth_first_subtree(members, fanout, reporter_id):
        _error("reporter subtree drifted from the frozen tree-30 geometry")
    if reporter_subtree != (0, 5, 6, 7, 8, 9):
        _error("reporter-0 subtree must be the exact six-signer branch")
    return DiagnosticPhase(
        "diagnostic",
        tree_id,
        root_replica,
        reporter_id,
        members,
        reporter_subtree,
    )


def _parse_signer_aware_arms(value: object) -> tuple[DiagnosticArm, ...]:
    values = _sequence(value, "diagnostic arms")
    if len(values) != 2:
        _error("profile must freeze exactly two declared adapter modes")
    expected = (
        {
            "name": ARM_NAMES[0],
            "fault_id": "n31-false-report-0-to-5",
            "actor_replica_id": 0,
            "runtime_marker": "false_timeout_emitted",
            "positive_suppression_marker": "false_report_positive_suppressed",
            "witness_signer_set": (0, 5, 6, 7, 8, 9),
            "settled_hypothesis": {
                "kind": "false_reporter",
                "replica_id": 0,
            },
        },
        {
            "name": ARM_NAMES[1],
            "fault_id": "n31-direct-vote-omission-5-to-0",
            "actor_replica_id": 5,
            "runtime_marker": "direct_vote_omitted",
            "positive_suppression_marker": None,
            "witness_signer_set": (0, 6, 7, 8, 9),
            "settled_hypothesis": {
                "kind": "direct_vote_omitter",
                "replica_id": 5,
            },
        },
    )
    fields = {
        "name",
        "fault_id",
        "actor_replica_id",
        "runtime_marker",
        "positive_suppression_marker",
        "witness_signer_set",
        "settled_hypothesis",
    }
    parsed: list[DiagnosticArm] = []
    for index, (raw, frozen) in enumerate(zip(values, expected, strict=True)):
        arm = _mapping(raw, f"diagnostic arm {index}")
        _exact_keys(arm, fields, f"diagnostic arm {index}")
        signer_set = _integer_tuple(
            arm.get("witness_signer_set"),
            f"diagnostic arm {index} witness signer set",
        )
        hypothesis = _mapping(
            arm.get("settled_hypothesis"),
            f"diagnostic arm {index} settled hypothesis",
        )
        _exact_keys(
            hypothesis,
            {"kind", "replica_id"},
            f"diagnostic arm {index} settled hypothesis",
        )
        observed = {
            **dict(arm),
            "witness_signer_set": signer_set,
            "settled_hypothesis": dict(hypothesis),
        }
        if observed != frozen:
            _error(f"diagnostic arm {index} differs from its frozen mode")
        parsed.append(
            DiagnosticArm(
                str(arm["name"]),
                str(arm["fault_id"]),
                int(arm["actor_replica_id"]),
                str(arm["runtime_marker"]),
                (
                    str(arm["positive_suppression_marker"])
                    if arm["positive_suppression_marker"] is not None
                    else None
                ),
                signer_set,
                str(hypothesis["kind"]),
                int(hypothesis["replica_id"]),
            )
        )
    return tuple(parsed)


def load_frozen_profile(path: Path) -> FrozenN31StaticDiagnosisProfile:
    """Load and independently derive the one-phase signer-aware contract."""

    try:
        raw_bytes = Path(path).read_bytes()
        decoded = json.loads(raw_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise N31StaticDiagnosisError("profile must be readable JSON") from error
    profile = _mapping(decoded, "profile")
    _exact_keys(profile, _PROFILE_FIELDS, "profile")
    if (
        profile.get("schema_version") != 1
        or profile.get("profile_id") != SHIPPED_PROFILE_ID
        or profile.get("frozen") is not True
    ):
        _error("profile identity, version, or frozen state drifted")
    membership = _integer_tuple(profile.get("replica_ids"), "replica membership")
    if membership != tuple(range(31)):
        _error("replica membership must be canonical N=31")
    fault_threshold = _integer(profile.get("fault_threshold"), "fault threshold")
    quorum = _integer(profile.get("quorum"), "quorum")
    if fault_threshold != 10 or quorum != 21 or quorum != 2 * fault_threshold + 1:
        _error("fault threshold and quorum must remain f=10 and Q=21")
    commit_witnesses = _integer_tuple(
        profile.get("commit_witnesses"), "commit witnesses"
    )
    if commit_witnesses != FIXED_COMMIT_WITNESSES or {0, 5} & set(commit_witnesses):
        _error("fixed Q21 witnesses must exclude both possible fault actors")
    fanout = _integer(profile.get("fanout"), "fanout", positive=True)
    topology_depth = _integer(profile.get("topology_depth"), "topology depth")
    topology_shape = _integer_tuple(profile.get("topology_shape"), "topology shape")
    pipeline_stretch = _integer(
        profile.get("pipeline_stretch"), "pipeline stretch", positive=True
    )
    if (
        fanout != 5
        or topology_depth != 2
        or topology_shape != (1, 5, 25)
        or pipeline_stretch != 2
    ):
        _error("N31 geometry must remain fanout five, depth two, pipeline two")
    epoch_number = _integer(profile.get("epoch_number"), "epoch number")
    epoch_digest = _hex256(profile.get("epoch_digest"), "epoch digest")
    if epoch_number != 0 or epoch_digest != (
        "145fac093343fa9cff20fcf49d85ad544" "3e93db14146f7854b17e28cf44f6d7a"
    ):
        _error("diagnosis must remain in the pinned epoch-zero definition")
    if _integer(profile.get("diagnostic_fault_bound"), "diagnostic fault bound") != 1:
        _error("diagnostic fault bound must remain one")
    if _integer(profile.get("snapshot_seed"), "snapshot seed") != 41_719:
        _error("snapshot seed differs from the frozen profile")
    if (
        profile.get("diagnostic_window") != "n31-epoch0-tree30-signer-crosscheck-v1"
        or profile.get("diagnostic_mode") != "stationary_same_context_signer_crosscheck"
        or profile.get("marker_clock") != "CLOCK_MONOTONIC_RAW"
        or profile.get("fault_lifecycle_start")
        != "after_clean_baseline_before_tree30_tail_snapshot"
    ):
        _error("diagnostic window, mode, clock, or lifecycle semantics drifted")
    target_id = _integer(profile.get("target_id"), "diagnostic target")
    reporter_id = _integer(profile.get("reporter_id"), "diagnostic reporter")
    witness_reporter_id = _integer(
        profile.get("witness_reporter_id"), "witness reporter"
    )
    if (target_id, reporter_id, witness_reporter_id) != (5, 0, 30):
        _error("diagnosis must bind leaf 5, reporter 0, and root witness 30")
    phase = _parse_signer_aware_phase(
        profile.get("phase"),
        membership=membership,
        fanout=fanout,
        reporter_id=reporter_id,
    )
    target_position = phase.members_breadth_first.index(target_id)
    parent_position = (target_position - 1) // fanout
    if phase.members_breadth_first[parent_position] != reporter_id:
        _error("leaf 5 is not a direct child of reporter 0 in tree 30")
    arms = _parse_signer_aware_arms(profile.get("arms"))
    if (
        _integer(profile.get("attempt_count"), "attempt count") != 1
        or profile.get("retry_failed_attempts") is not False
        or profile.get("require_successor_activation") is not False
        or _integer(profile.get("tree_switch_period_blocks"), "tree switch period") != 1
        or _integer(
            profile.get("byzantine_context_limit"),
            "Byzantine context limit",
            positive=True,
        )
        != 64
    ):
        _error("attempt, retry, successor, tree, or context bounds drifted")

    crosscheck = _mapping(profile.get("signer_crosscheck"), "signer cross-check")
    crosscheck_fields = {
        "classifier",
        "selection_order",
        "same_context_fields",
        "claim_expected_message_type",
        "witness_expected_message_type",
        "response_only_hypothesis_count",
        "signer_aware_hypothesis_count",
        "exact_reporter_subtree",
        "boundary_max_skew_ns",
        "clock_comparability",
        "added_protocol_messages",
        "added_protocol_trees",
        "forced_tree_rotations",
        "limitation",
    }
    _exact_keys(crosscheck, crosscheck_fields, "signer cross-check")
    same_context_fields = tuple(
        str(item)
        for item in _sequence(
            crosscheck.get("same_context_fields"), "same-context fields"
        )
    )
    exact_subtree = _integer_tuple(
        crosscheck.get("exact_reporter_subtree"), "exact reporter subtree"
    )
    limitation = crosscheck.get("limitation")
    if (
        crosscheck.get("classifier") != "same_context_ancestor_signer_crosscheck"
        or crosscheck.get("selection_order") != "order_independent_same_proposal"
        or same_context_fields
        != ("epoch_number", "tree_id", "epoch_digest", "block_hash")
        or crosscheck.get("claim_expected_message_type") != "direct_vote"
        or crosscheck.get("witness_expected_message_type") != "aggregate_relay"
        or crosscheck.get("response_only_hypothesis_count") != 2
        or crosscheck.get("signer_aware_hypothesis_count") != 1
        or exact_subtree != phase.reporter_subtree
        or crosscheck.get("boundary_max_skew_ns") != 500_000_000
        or crosscheck.get("clock_comparability") != "single_host_CLOCK_MONOTONIC_RAW"
        or crosscheck.get("added_protocol_messages") != 0
        or crosscheck.get("added_protocol_trees") != 0
        or crosscheck.get("forced_tree_rotations") != 0
        or not isinstance(limitation, str)
        or "two" not in limitation
        or "arbitrary Byzantine reporter" not in limitation
        or "censor" not in limitation
    ):
        _error("same-context signer classifier or limitation drifted")

    evidence_policy = _mapping(profile.get("evidence_policy"), "evidence policy")
    _exact_keys(
        evidence_policy,
        {"clean_baseline", "later_commit", "pilot_ceiling", "figure_eligibility"},
        "evidence policy",
    )
    if evidence_policy != {
        "clean_baseline": "fixed_Q21_before_matching_action",
        "later_commit": "fixed_Q21_preserved_ancestry_after_diagnosis",
        "pilot_ceiling": "harness_validation_only",
        "figure_eligibility": "campaign_PASS_only",
    }:
        _error("pilot, campaign, or commit evidence policy drifted")

    runtime_profile = _mapping(profile.get("runtime_profile"), "runtime profile")
    _exact_keys(runtime_profile, {"path", "profile_id", "sha256"}, "runtime profile")
    if runtime_profile != {
        "path": "experiments/adaptive/profiles/n31-f5-internal1-crash-shakedown-v1.json",
        "profile_id": "n31-f5-q21-internal1-sigkill-shakedown-v1",
        "sha256": "0defdaa9b69c949365eea3b3029da75cee3ea8334f845401103e2f7af8507650",
    }:
        _error("runtime profile binding drifted")
    profile_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if profile_sha256 != SHIPPED_PROFILE_SHA256:
        _error("profile SHA-256 does not match the shipped frozen bytes")
    return FrozenN31StaticDiagnosisProfile(
        SHIPPED_PROFILE_ID,
        profile_sha256,
        True,
        membership,
        fault_threshold,
        quorum,
        commit_witnesses,
        fanout,
        topology_depth,
        topology_shape,
        pipeline_stretch,
        epoch_number,
        epoch_digest,
        1,
        41_719,
        str(profile["diagnostic_window"]),
        str(profile["diagnostic_mode"]),
        str(profile["marker_clock"]),
        str(profile["fault_lifecycle_start"]),
        target_id,
        reporter_id,
        witness_reporter_id,
        phase,
        arms,
        1,
        False,
        False,
        1,
        64,
        str(crosscheck["classifier"]),
        str(crosscheck["selection_order"]),
        same_context_fields,
        str(crosscheck["claim_expected_message_type"]),
        str(crosscheck["witness_expected_message_type"]),
        2,
        1,
        exact_subtree,
        500_000_000,
        str(crosscheck["clock_comparability"]),
        str(limitation),
        str(evidence_policy["clean_baseline"]),
        str(evidence_policy["later_commit"]),
        str(evidence_policy["pilot_ceiling"]),
        str(evidence_policy["figure_eligibility"]),
        str(runtime_profile["path"]),
        str(runtime_profile["profile_id"]),
        str(runtime_profile["sha256"]),
    )


def _arm(profile: FrozenN31StaticDiagnosisProfile, name: str) -> DiagnosticArm:
    if name not in ARM_NAMES:
        _error(f"unknown N31 signer-aware diagnosis arm: {name}")
    return next(arm for arm in profile.arms if arm.name == name)


def _require_exact_profile_instance(
    profile: FrozenN31StaticDiagnosisProfile,
) -> None:
    if type(profile) is not FrozenN31StaticDiagnosisProfile:
        _error("profile must be an exact frozen N31 profile value")
    expected_members = tuple(range(31))
    expected_tree = expected_members[30:] + expected_members[:30]
    if (
        profile.profile_id != SHIPPED_PROFILE_ID
        or profile.profile_sha256 != SHIPPED_PROFILE_SHA256
        or profile.frozen is not True
        or profile.replica_ids != expected_members
        or profile.fault_threshold != 10
        or profile.quorum != 21
        or profile.commit_witnesses != FIXED_COMMIT_WITNESSES
        or {0, 5} & set(profile.commit_witnesses)
        or profile.fanout != 5
        or profile.topology_depth != 2
        or profile.topology_shape != (1, 5, 25)
        or profile.pipeline_stretch != 2
        or profile.epoch_number != 0
        or profile.epoch_digest
        != "145fac093343fa9cff20fcf49d85ad5443e93db14146f7854b17e28cf44f6d7a"
        or profile.diagnostic_fault_bound != 1
        or profile.snapshot_seed != 41_719
        or profile.diagnostic_window != "n31-epoch0-tree30-signer-crosscheck-v1"
        or profile.diagnostic_mode != "stationary_same_context_signer_crosscheck"
        or profile.marker_clock != "CLOCK_MONOTONIC_RAW"
        or profile.fault_lifecycle_start
        != "after_clean_baseline_before_tree30_tail_snapshot"
        or (profile.target_id, profile.reporter_id, profile.witness_reporter_id)
        != (5, 0, 30)
        or (
            profile.phase.label,
            profile.phase.tree_id,
            profile.phase.root_replica,
            profile.phase.reporter_id,
            profile.phase.members_breadth_first,
            profile.phase.reporter_subtree,
        )
        != ("diagnostic", 30, 30, 0, expected_tree, (0, 5, 6, 7, 8, 9))
        or tuple(
            (
                arm.name,
                arm.fault_id,
                arm.actor_replica_id,
                arm.runtime_marker,
                arm.positive_suppression_marker,
                arm.witness_signer_set,
                arm.settled_hypothesis_kind,
                arm.settled_hypothesis_replica_id,
            )
            for arm in profile.arms
        )
        != (
            (
                ARM_NAMES[0],
                "n31-false-report-0-to-5",
                0,
                "false_timeout_emitted",
                "false_report_positive_suppressed",
                (0, 5, 6, 7, 8, 9),
                "false_reporter",
                0,
            ),
            (
                ARM_NAMES[1],
                "n31-direct-vote-omission-5-to-0",
                5,
                "direct_vote_omitted",
                None,
                (0, 6, 7, 8, 9),
                "direct_vote_omitter",
                5,
            ),
        )
        or profile.classifier != "same_context_ancestor_signer_crosscheck"
        or profile.selection_order != "order_independent_same_proposal"
        or profile.claim_expected_message_type != "direct_vote"
        or profile.witness_expected_message_type != "aggregate_relay"
        or profile.expected_hypothesis_counts != (2, 1)
        or profile.exact_reporter_subtree != (0, 5, 6, 7, 8, 9)
        or profile.boundary_max_skew_ns != 500_000_000
        or profile.pilot_ceiling != "harness_validation_only"
        or profile.figure_eligibility != "campaign_PASS_only"
    ):
        _error("profile object differs from the shipped signer-aware contract")


def build_fault_plan(
    profile: FrozenN31StaticDiagnosisProfile, arm: DiagnosticArm
) -> FaultPlan:
    """Build the exact canonical one-actor FI-Core plan."""

    _require_exact_profile_instance(profile)
    if arm not in profile.arms:
        _error("diagnostic arm is not part of the frozen profile")
    context = ScenarioContext(
        profile.replica_ids,
        profile.quorum,
        profile.fault_threshold,
        1,
        profile.diagnostic_fault_bound,
    )
    if arm.name == ARM_NAMES[0]:
        action = StaticAuthenticatedFalseReport(
            arm.fault_id,
            profile.reporter_id,
            profile.target_id,
            "timeout",
            profile.diagnostic_window,
        )
    else:
        action = StaticPersistentDirectVoteOmission(
            arm.fault_id,
            profile.target_id,
            profile.reporter_id,
            profile.diagnostic_window,
        )
    return FaultPlan(context, profile.snapshot_seed, (action,))


def build_launch_contract(
    profile: FrozenN31StaticDiagnosisProfile,
    *,
    arm: str,
    kauri_revision: str,
) -> dict[str, object]:
    """Build the immutable single-tree actor-local launch overlay."""

    _require_exact_profile_instance(profile)
    if (
        not isinstance(kauri_revision, str)
        or _REVISION.fullmatch(kauri_revision) is None
    ):
        _error("Kauri revision must be a full lowercase SHA")
    selected = _arm(profile, arm)
    plan = build_fault_plan(profile, selected)
    configuration = (
        f"{profile.epoch_number}:{profile.phase.tree_id}:{profile.epoch_digest}"
    )
    overlays: list[dict[str, object]] = []
    for replica_id in profile.replica_ids:
        arguments = list(plan.replica_cli_args(replica_id))
        if arguments:
            arguments.extend(
                (
                    "--experiment-byzantine-configuration",
                    configuration,
                    "--experiment-byzantine-context-limit",
                    str(profile.byzantine_context_limit),
                )
            )
        overlays.append({"replica_id": replica_id, "argv": arguments})
    if plan.manager_cli_args():
        _error("diagnostic ground truth must not enter manager arguments")
    return {
        "schema_version": 1,
        "scenario": SCENARIO,
        "profile_id": profile.profile_id,
        "profile_sha256": profile.profile_sha256,
        "kauri_revision": kauri_revision,
        "arm": selected.name,
        "fault_plan_sha256": plan.sha256,
        "attempt_count": 1,
        "retry_policy": "none",
        "required_ready_sources": 32,
        "fixed_context": {
            "replica_count": 31,
            "fault_threshold": 10,
            "quorum": 21,
            "commit_witnesses": list(profile.commit_witnesses),
            "fanout": 5,
            "topology_depth": 2,
            "pipeline_stretch": 2,
            "epoch_number": 0,
            "epoch_digest": profile.epoch_digest,
            "diagnostic_fault_bound": 1,
            "diagnostic_mode": profile.diagnostic_mode,
            "marker_clock": profile.marker_clock,
            "fault_lifecycle_start": profile.fault_lifecycle_start,
            "tree_id": 30,
            "root_replica": 30,
            "reporter_id": 0,
            "target_id": 5,
            "witness_reporter_id": 30,
            "reporter_subtree": list(profile.phase.reporter_subtree),
            "require_successor_activation": False,
        },
        "runtime_profile": {
            "path": profile.runtime_profile_path,
            "profile_id": profile.runtime_profile_id,
            "sha256": profile.runtime_profile_sha256,
        },
        "replica_overlays": overlays,
        "manager_overlay": [],
        "diagnostic_classifier": {
            "name": profile.classifier,
            "selection_order": profile.selection_order,
            "claim_expected_message_type": profile.claim_expected_message_type,
            "witness_expected_message_type": profile.witness_expected_message_type,
            "response_only_hypothesis_count": 2,
            "signer_aware_hypothesis_count": 1,
            "authoritative_raw_field": "signer_set",
            "limitation": profile.limitation,
        },
        "evidence_policy": {
            "pilot_ceiling": profile.pilot_ceiling,
            "figure_eligibility": profile.figure_eligibility,
        },
    }


def _live_relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _error(f"{label} path is invalid")
    path = Path(value)
    if path.is_absolute() or value == "." or ".." in path.parts:
        _error(f"{label} path escapes the preserved run")
    return value


def _live_validate_preflight(
    profile: FrozenN31StaticDiagnosisProfile,
    *,
    value: object,
    revision: str,
) -> None:
    preflight = _mapping(value, "manifest preflight")
    _exact_keys(preflight, _PREFLIGHT_FIELDS, "manifest preflight")
    diagnosis = _mapping(preflight.get("diagnosis_profile"), "preflight profile")
    _exact_keys(diagnosis, {"profile_id", "sha256"}, "preflight profile")
    runtime_profile = _mapping(
        preflight.get("runtime_profile"), "preflight runtime profile"
    )
    _exact_keys(
        runtime_profile,
        {"profile_id", "sha256", "path"},
        "preflight runtime profile",
    )
    runtime_path = runtime_profile.get("path")
    if (
        preflight.get("schema_version") != 1
        or preflight.get("scenario") != SCENARIO
        or preflight.get("verdict") != "PASS"
        or preflight.get("revision") != revision
        or diagnosis
        != {"profile_id": profile.profile_id, "sha256": profile.profile_sha256}
        or runtime_profile.get("profile_id") != profile.runtime_profile_id
        or runtime_profile.get("sha256") != profile.runtime_profile_sha256
        or not isinstance(runtime_path, str)
        or not runtime_path.endswith(profile.runtime_profile_path)
        or preflight.get("commit_witnesses") != list(profile.commit_witnesses)
    ):
        _error("preflight revision or frozen profile binding drifted")

    launch_contracts = _mapping(
        preflight.get("launch_contracts"), "preflight launch contracts"
    )
    if set(launch_contracts) != set(ARM_NAMES) or any(
        launch_contracts.get(arm_name)
        != build_launch_contract(profile, arm=arm_name, kauri_revision=revision)
        for arm_name in ARM_NAMES
    ):
        _error("preflight actor-only launch contracts drifted")

    profiled = _mapping(preflight.get("profiled_runtime"), "profiled-runtime preflight")
    _exact_keys(profiled, _PROFILED_PREFLIGHT_FIELDS, "profiled-runtime preflight")
    if (
        profiled.get("schema_version") != 1
        or profiled.get("verdict") != "PASS"
        or profiled.get("profile_id") != profile.runtime_profile_id
        or profiled.get("profile_sha256") != profile.runtime_profile_sha256
        or profiled.get("revision") != revision
        or profiled.get("clock") != "CLOCK_MONOTONIC_RAW"
        or _integer(
            profiled.get("required_port_count"),
            "preflight required port count",
            positive=True,
        )
        <= 0
        or _integer(
            profiled.get("fd_soft_limit"), "preflight FD soft limit", positive=True
        )
        < 1024
        or _integer(
            profiled.get("free_disk_bytes"),
            "preflight free disk bytes",
            positive=True,
        )
        < 1024**3
    ):
        _error("profiled-runtime preflight contract drifted")

    provenance = _mapping(
        profiled.get("build_provenance"), "preflight build provenance"
    )
    _exact_keys(
        provenance,
        _BUILD_PROVENANCE_FIELDS,
        "preflight build provenance",
    )
    if (
        provenance.get("schema_version") != 1
        or provenance.get("revision") != revision
        or _HEX_256.fullmatch(str(provenance.get("cmake_cache_sha256"))) is None
    ):
        _error("preflight build provenance is not revision-bound")
    binaries = _mapping(provenance.get("binaries"), "build provenance binaries")
    metadata = _mapping(provenance.get("build_metadata"), "build provenance metadata")
    executables = _mapping(profiled.get("executables"), "preflight executables")
    if (
        set(binaries) != _BINARY_NAMES
        or set(executables) != _BINARY_NAMES
        or set(metadata) != _BUILD_METADATA_NAMES
    ):
        _error("preflight build artifact membership drifted")
    for name, raw_binary in binaries.items():
        binary = _mapping(raw_binary, f"build binary {name}")
        executable = _mapping(executables[name], f"preflight executable {name}")
        _exact_keys(binary, {"path", "size_bytes", "sha256"}, f"build binary {name}")
        _exact_keys(executable, {"path", "sha256"}, f"preflight executable {name}")
        if (
            not isinstance(binary.get("path"), str)
            or not binary["path"]
            or _integer(
                binary.get("size_bytes"), f"build binary {name} size", positive=True
            )
            <= 0
            or _hex256(binary.get("sha256"), f"build binary {name} SHA")
            != executable.get("sha256")
            or binary.get("path") != executable.get("path")
        ):
            _error("preflight executable differs from build provenance")
    for name, raw_metadata in metadata.items():
        item = _mapping(raw_metadata, f"build metadata {name}")
        _exact_keys(
            item,
            {"path", "size_bytes", "sha256"},
            f"build metadata {name}",
        )
        if (
            not isinstance(item.get("path"), str)
            or not item["path"]
            or _integer(item.get("size_bytes"), f"build metadata {name} size") < 0
        ):
            _error("preflight build metadata identity drifted")
        _hex256(item.get("sha256"), f"build metadata {name} SHA")

    witness = _mapping(profiled.get("epoch_zero_witness"), "epoch-zero witness")
    epoch = _mapping(witness.get("epoch_zero"), "epoch-zero witness payload")
    if (
        witness.get("schema") != "kauri-adaptive-v2-epoch-profile-digest-v1"
        or witness.get("replica_count") != len(profile.replica_ids)
        or witness.get("fault_threshold") != profile.fault_threshold
        or witness.get("quorum") != profile.quorum
        or witness.get("fanout") != profile.fanout
        or witness.get("pipeline_stretch") != profile.pipeline_stretch
        or witness.get("membership") != list(profile.replica_ids)
        or epoch.get("schema_version") != 2
        or epoch.get("epoch_number") != profile.epoch_number
        or epoch.get("epoch_digest") != profile.epoch_digest
        or epoch.get("tree_count") != len(profile.replica_ids)
    ):
        _error("preflight epoch-zero witness differs from frozen N31")


def _live_validate_preserved_inputs(
    profile: FrozenN31StaticDiagnosisProfile,
    *,
    manifest: Mapping[str, object],
    arm: DiagnosticArm,
    source_instances: Mapping[str, object],
) -> Mapping[str, Any]:
    artifacts = _sequence(manifest.get("runtime_artifacts"), "runtime artifacts")
    if not artifacts:
        _error("runtime artifact inventory is absent")
    paths: set[str] = set()
    for index, raw_artifact in enumerate(artifacts):
        artifact = _mapping(raw_artifact, f"runtime artifact {index}")
        _exact_keys(
            artifact,
            _RUNTIME_ARTIFACT_FIELDS,
            f"runtime artifact {index}",
        )
        path = _live_relative_path(artifact.get("path"), f"runtime artifact {index}")
        if path in paths or path in {
            "manifest.json",
            "validation.json",
            "evidence-seal.json",
        }:
            _error("runtime artifact inventory is duplicate or circular")
        paths.add(path)
        if not isinstance(artifact.get("kind"), str) or not artifact["kind"]:
            _error("runtime artifact kind is invalid")
        replica_id = artifact.get("replica_id")
        if replica_id is not None and (
            isinstance(replica_id, bool)
            or not isinstance(replica_id, int)
            or replica_id not in profile.replica_ids
        ):
            _error("runtime artifact replica identity is outside N31")
        _hex256(artifact.get("sha256"), f"runtime artifact {index} SHA")
    required = {
        "profile.json",
        "runtime-profile.json",
        "runtime/build-provenance.json",
        "runtime/epoch-zero-witness.json",
        "runtime/launch-contract.json",
        "runtime/launch-arguments.json",
    }
    if not required <= paths:
        _error("runtime artifact inventory lacks frozen launch provenance")

    expected_sources = [
        *(f"replica-{replica_id}" for replica_id in profile.replica_ids),
        MANAGER_SOURCE_ID,
    ]
    sources = _sequence(manifest.get("sources"), "source descriptors")
    if len(sources) != len(expected_sources):
        _error("source descriptors require exact N31 plus manager membership")
    cleanup_entries = _sequence(manifest.get("cleanup_ledger"), "cleanup ledger")
    cleanup_by_name = {
        entry.get("name"): entry
        for entry in cleanup_entries
        if isinstance(entry, Mapping)
    }
    process_groups: set[int] = set()
    for expected_source, raw_source in zip(expected_sources, sources, strict=True):
        source = _mapping(raw_source, f"source descriptor {expected_source}")
        _exact_keys(
            source,
            _SOURCE_DESCRIPTOR_FIELDS,
            f"source descriptor {expected_source}",
        )
        expected_kind = (
            "adaptation_manager" if expected_source == MANAGER_SOURCE_ID else "replica"
        )
        pid = _integer(source.get("pid"), f"{expected_source} PID", positive=True)
        pgid = _integer(source.get("pgid"), f"{expected_source} PGID", positive=True)
        cleanup = _mapping(
            cleanup_by_name.get(expected_source),
            f"cleanup identity for {expected_source}",
        )
        if (
            source.get("source_kind") != expected_kind
            or source.get("source_id") != expected_source
            or source.get("source_instance") != source_instances[expected_source]
            or pid <= 1
            or pgid != pid
            or pgid in process_groups
            or cleanup.get("pid") != pid
            or cleanup.get("pgid") != pgid
            or _live_relative_path(source.get("path"), expected_source)
            != f"raw/{expected_source}.jsonl"
        ):
            _error("source descriptor identity or owned process group drifted")
        process_groups.add(pgid)
        _hex256(source.get("sha256"), f"{expected_source} stream SHA")

    actor_log = _mapping(manifest.get("actor_log"), "actor log descriptor")
    _exact_keys(actor_log, _ACTOR_LOG_FIELDS, "actor log descriptor")
    if (
        _live_relative_path(actor_log.get("path"), "actor log")
        != f"logs/replica-{arm.actor_replica_id}.log"
    ):
        _error("actor log descriptor does not identify the selected actor")
    _hex256(actor_log.get("sha256"), "actor log SHA")
    return actor_log


def _live_validate_cleanup(
    profile: FrozenN31StaticDiagnosisProfile,
    value: object,
) -> int:
    entries = _sequence(value, "cleanup ledger")
    expected_names = {
        *(f"replica-{replica_id}" for replica_id in profile.replica_ids),
        MANAGER_SOURCE_ID,
    }
    if len(entries) != len(expected_names):
        _error("cleanup ledger must contain the exact 32 owned processes")
    seen: set[str] = set()
    cleanup_times: set[int] = set()
    for index, raw_entry in enumerate(entries):
        entry = _mapping(raw_entry, f"cleanup ledger entry {index}")
        _exact_keys(entry, _CLEANUP_FIELDS, f"cleanup ledger entry {index}")
        name = entry.get("name")
        if not isinstance(name, str) or name not in expected_names or name in seen:
            _error("cleanup ledger source membership drifted")
        seen.add(name)
        replica_id = entry.get("replica_id")
        expected_replica = (
            None if name == MANAGER_SOURCE_ID else int(name.removeprefix("replica-"))
        )
        if replica_id != expected_replica:
            _error("cleanup ledger replica identity drifted")
        pid = _integer(entry.get("pid"), "cleanup pid", positive=True)
        pgid = _integer(entry.get("pgid"), "cleanup process group", positive=True)
        if pid <= 1 or pgid != pid:
            _error("cleanup ledger does not bind an exact owned process group")
        signals = _integer_tuple(entry.get("signals_sent"), "cleanup signals")
        if (
            not signals
            or signals != tuple(dict.fromkeys(signals))
            or any(signal_number not in {2, 9, 15} for signal_number in signals)
        ):
            _error("cleanup signal sequence is invalid")
        returncode = entry.get("returncode")
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            _error("cleanup return code is invalid")
        classification = entry.get("classification")
        if classification == "expected_cleanup":
            if not (
                (name == MANAGER_SOURCE_ID and returncode == 1)
                or (name != MANAGER_SOURCE_ID and returncode in {0, -2})
            ):
                _error("expected cleanup classification disagrees with return code")
        elif classification == "expected_forced_cleanup":
            if name == MANAGER_SOURCE_ID or returncode != -9 or signals != (2, 15, 9):
                _error("forced cleanup classification is not canonical")
        else:
            _error("static diagnosis cleanup contains an unexpected exit")
        if entry.get("cleanup_errors") != []:
            _error("cleanup ledger contains an owned-process cleanup failure")
        cleanup_started = _integer(
            entry.get("cleanup_started_ns"),
            "cleanup timestamp",
            positive=True,
        )
        cleanup_times.add(cleanup_started)
        if entry.get("cleanup_started_after_post_window") is not True:
            _error("cleanup began before the later common-commit boundary")
    if seen != expected_names or len(cleanup_times) != 1:
        _error("cleanup ledger membership or timestamp drifted")
    return next(iter(cleanup_times))


def _live_validate_configuration_payload(
    profile: FrozenN31StaticDiagnosisProfile,
    payload: Mapping[str, Any],
    *,
    replica_id: int,
) -> None:
    _exact_keys(payload, _CONFIGURATION_ACTIVE_FIELDS, "active configuration")
    if (
        payload.get("epoch_number") != profile.epoch_number
        or payload.get("tree_id") not in profile.replica_ids
        or payload.get("epoch_digest") != profile.epoch_digest
        or payload.get("observer_replica") != replica_id
        or payload.get("block_hash") is not None
        or payload.get("context_generation") is not None
        or payload.get("wait_exempt_signers") != []
        or payload.get("accepted_signers") != []
        or payload.get("absent_direct_children") != []
        or payload.get("missing_optional_signers") != []
        or payload.get("required_branch_gaps") != []
        or payload.get("root_signer_count") != 0
        or payload.get("global_quorum") != profile.quorum
        or payload.get("rejection_reason") is not None
    ):
        _error("active configuration is not canonical fixed-Q21 epoch zero")


def _live_validate_event_streams(
    profile: FrozenN31StaticDiagnosisProfile,
    *,
    streams: Mapping[str, Sequence[Mapping[str, object]]],
    run_id: str,
    source_instances: Mapping[str, object],
    cleanup_started_ns: int,
) -> tuple[dict[str, tuple[Mapping[str, Any], ...]], dict[str, int]]:
    expected_sources = {
        *(f"replica-{replica_id}" for replica_id in profile.replica_ids),
        MANAGER_SOURCE_ID,
    }
    if set(streams) != expected_sources:
        _error("readiness requires the exact 32 manager and replica sources")
    normalized: dict[str, tuple[Mapping[str, Any], ...]] = {}
    ready_timestamps: dict[str, int] = {}
    for source_id in sorted(expected_sources):
        raw_events = streams[source_id]
        if isinstance(raw_events, (str, bytes, Mapping)) or not isinstance(
            raw_events, Sequence
        ):
            _error(f"structured stream {source_id} must be an event array")
        expected_kind = (
            "adaptation_manager" if source_id == MANAGER_SOURCE_ID else "replica"
        )
        previous_timestamp: int | None = None
        started: list[Mapping[str, Any]] = []
        ready: list[Mapping[str, Any]] = []
        events: list[Mapping[str, Any]] = []
        for expected_sequence, raw_event in enumerate(raw_events, start=1):
            event = _mapping(raw_event, f"structured event from {source_id}")
            _exact_keys(event, _EVENT_FIELDS, f"structured event from {source_id}")
            if (
                event.get("event_schema_version") != 1
                or event.get("run_id") != run_id
                or event.get("source_kind") != expected_kind
                or event.get("source_id") != source_id
                or event.get("source_instance") != source_instances[source_id]
                or event.get("source_sequence") != expected_sequence
            ):
                _error(
                    "structured source-instance identity or sequence drifted "
                    f"for {source_id}"
                )
            timestamp = _integer(
                event.get("source_monotonic_ns"),
                f"{source_id} source timestamp",
                positive=True,
            )
            if previous_timestamp is not None and timestamp < previous_timestamp:
                _error(f"structured timestamp regressed in {source_id}")
            previous_timestamp = timestamp
            payload = _mapping(event.get("payload"), f"{source_id} event payload")
            event_type = event.get("event_type")
            if not isinstance(event_type, str) or not event_type:
                _error(f"{source_id} event type is invalid")
            if event_type == "process.started":
                _exact_keys(payload, {"exit_status"}, "process.started payload")
                if payload.get("exit_status") is not None:
                    _error("process.started exit status is non-null")
                started.append(event)
            elif event_type == "process.ready":
                _exact_keys(payload, {"exit_status"}, "process.ready payload")
                if payload.get("exit_status") is not None:
                    _error("process.ready exit status is non-null")
                ready.append(event)
            elif event_type in {"process.restarted", "runtime.restarted"}:
                _error(f"process restart observed for {source_id}")
            elif event_type == "adaptive.configuration_active":
                if source_id == MANAGER_SOURCE_ID:
                    _error("manager cannot emit replica configuration evidence")
                _live_validate_configuration_payload(
                    profile,
                    payload,
                    replica_id=int(source_id.removeprefix("replica-")),
                )
            if event_type in {
                "epoch.generated",
                "epoch.staged",
                "epoch.acknowledged",
                "epoch.activation_armed",
                "epoch.activated",
                "epoch.command_committed",
            }:
                _error("unexpected successor activity observed")
            if event_type.startswith("adaptive_v2"):
                if (
                    event_type != "adaptive_v2_session_terminal"
                    or source_id != MANAGER_SOURCE_ID
                    or timestamp < cleanup_started_ns
                    or payload.get("cycle_ordinal") != 0
                    or payload.get("outcome") != "failed"
                    or payload.get("reason") != "caller_failed"
                ):
                    _error("unexpected adaptive-v2 successor activity observed")
            for field in (
                "epoch_number",
                "predecessor_epoch_number",
                "successor_epoch_number",
            ):
                value = payload.get(field)
                if isinstance(value, int) and value > profile.epoch_number:
                    _error(f"unexpected epoch above zero in {source_id}:{event_type}")
            decision_proof = payload.get("decision_proof")
            if (
                isinstance(decision_proof, Mapping)
                and decision_proof.get("epoch_number") != profile.epoch_number
            ):
                _error("commit decision proof is not canonical epoch zero")
            events.append(event)
        if len(started) != 1 or len(ready) != 1:
            _error(
                f"{source_id} requires exactly one process.started and "
                "process.ready without restart"
            )
        if int(started[0]["source_sequence"]) >= int(
            ready[0]["source_sequence"]
        ) or int(started[0]["source_monotonic_ns"]) > int(
            ready[0]["source_monotonic_ns"]
        ):
            _error(f"{source_id} readiness precedes process start")
        ready_timestamps[source_id] = int(ready[0]["source_monotonic_ns"])
        normalized[source_id] = tuple(events)
    return normalized, ready_timestamps


def _live_validate_boundary(
    profile: FrozenN31StaticDiagnosisProfile,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    value: object,
    phase: DiagnosticPhase,
) -> dict[str, Any]:
    boundary = _mapping(value, f"tree-{phase.tree_id} boundary")
    _exact_keys(boundary, _BOUNDARY_FIELDS, f"tree-{phase.tree_id} boundary")
    if (
        boundary.get("epoch_number") != profile.epoch_number
        or boundary.get("tree_id") != phase.tree_id
        or boundary.get("root_replica") != phase.tree_id
        or boundary.get("epoch_digest") != profile.epoch_digest
        or boundary.get("global_quorum") != profile.quorum
        or boundary.get("members_breadth_first") != list(phase.members_breadth_first)
    ):
        _error(f"tree-{phase.tree_id} boundary differs from the frozen topology")
    references = _sequence(
        boundary.get("replica_evidence"), f"tree-{phase.tree_id} boundary evidence"
    )
    if len(references) != len(profile.replica_ids):
        _error("configuration boundary lacks exact N31 replica evidence")
    sequences: dict[str, int] = {}
    timestamps: dict[str, int] = {}
    for replica_id, raw_reference in zip(profile.replica_ids, references, strict=True):
        source_id = f"replica-{replica_id}"
        reference = _mapping(raw_reference, f"{source_id} boundary reference")
        _exact_keys(
            reference,
            _BOUNDARY_REFERENCE_FIELDS,
            f"{source_id} boundary reference",
        )
        sequence = _integer(
            reference.get("source_sequence"),
            f"{source_id} boundary sequence",
            positive=True,
        )
        timestamp = _integer(
            reference.get("source_monotonic_ns"),
            f"{source_id} boundary timestamp",
            positive=True,
        )
        if reference.get("source_id") != source_id:
            _error("configuration boundary source order drifted")
        resolved = [
            event
            for event in streams[source_id]
            if event.get("source_sequence") == sequence
            and event.get("source_monotonic_ns") == timestamp
        ]
        if len(resolved) != 1:
            _error("configuration boundary reference is not exact")
        payload = _mapping(resolved[0].get("payload"), "boundary payload")
        if (
            resolved[0].get("event_type") != "adaptive.configuration_active"
            or payload.get("epoch_number") != profile.epoch_number
            or payload.get("tree_id") != phase.tree_id
            or payload.get("epoch_digest") != profile.epoch_digest
            or payload.get("observer_replica") != replica_id
            or payload.get("global_quorum") != profile.quorum
        ):
            _error("configuration boundary does not resolve to the exact active tree")
        sequences[source_id] = sequence
        timestamps[source_id] = timestamp
    return {
        "boundary": deepcopy(dict(boundary)),
        "sequences": sequences,
        "timestamps": timestamps,
        "minimum_ns": min(timestamps.values()),
        "maximum_ns": max(timestamps.values()),
    }


def _live_parse_observations(
    profile: FrozenN31StaticDiagnosisProfile,
    manager_events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    accepted: list[dict[str, Any]] = []
    observation_ids: set[str] = set()
    prior_ingestion = 0
    for event in manager_events:
        if event.get("event_type") != "evidence.observation_accepted":
            continue
        payload = _mapping(event.get("payload"), "manager observation payload")
        _exact_keys(
            payload,
            {"ingestion_sequence", "observation"},
            "manager observation payload",
        )
        ingestion = _integer(
            payload.get("ingestion_sequence"),
            "manager ingestion sequence",
            positive=True,
        )
        if ingestion <= prior_ingestion:
            _error("duplicate or out-of-order manager ingestion")
        prior_ingestion = ingestion
        observation = _mapping(payload.get("observation"), "accepted observation")
        _exact_keys(observation, _OBSERVATION_FIELDS, "accepted observation")
        if observation.get("schema_version") != 1:
            _error("accepted observation schema version drifted")
        observation_id = _hex256(observation.get("observation_id"), "observation id")
        if observation_id in observation_ids:
            _error("duplicate or state-drifting accepted observation id")
        observation_ids.add(observation_id)
        configuration = _mapping(
            observation.get("configuration"), "observation configuration"
        )
        _exact_keys(configuration, _CONFIGURATION_FIELDS, "observation configuration")
        if (
            _integer(configuration.get("epoch_number"), "observation epoch")
            != profile.epoch_number
            or _integer(configuration.get("tree_id"), "observation tree")
            not in profile.replica_ids
            or _hex256(configuration.get("epoch_digest"), "observation epoch digest")
            != profile.epoch_digest
        ):
            _error("accepted observation is outside exact N31 epoch zero")
        _hex256(observation.get("block_hash"), "observation block hash")
        for field in ("reporter_id", "observed_replica_id"):
            replica_id = _integer(observation.get(field), f"observation {field}")
            if replica_id not in profile.replica_ids:
                _error("observation endpoint is outside N31 membership")
        response = _integer(
            observation.get("response_duration_us"), "response duration"
        )
        deadline = _integer(
            observation.get("deadline_duration_us"),
            "deadline duration",
            positive=True,
        )
        reporter_ns = _integer(
            observation.get("reporter_monotonic_ns"),
            "reporter timestamp",
            positive=True,
        )
        _integer(
            observation.get("reporter_sequence"),
            "reporter sequence",
            positive=True,
        )
        signer_set = _integer_tuple(
            observation.get("signer_set"), "observation signer set"
        )
        if signer_set != tuple(sorted(set(signer_set))) or any(
            signer not in profile.replica_ids for signer in signer_set
        ):
            _error("observation signer set is non-canonical or outside membership")
        outcome = observation.get("outcome")
        if outcome == "timeout":
            if response != 0 or signer_set:
                _error("timeout observation has response material")
        elif outcome == "on_time":
            if not signer_set or response > deadline:
                _error("on-time observation has invalid signer/timing evidence")
        elif outcome == "late":
            if not signer_set or response < deadline:
                _error("late observation has invalid signer/timing evidence")
        else:
            _error("accepted observation outcome is unsupported")
        if observation.get("expected_message_type") not in {
            "direct_vote",
            "aggregate_relay",
            "leader_progress",
        }:
            _error("accepted observation message type is unsupported")
        if reporter_ns > int(event["source_monotonic_ns"]):
            _error("reporter evidence occurs after manager acceptance")
        accepted.append(
            {
                "event": event,
                "ingestion_sequence": ingestion,
                "observation": deepcopy(dict(observation)),
                "signer_set": signer_set,
            }
        )
    if len(accepted) < 2:
        _error("diagnostic run requires at least two accepted observations")
    return tuple(accepted)


def _live_commit_identity(
    payload: Mapping[str, Any], *, rich: bool
) -> tuple[int, str, str, int, int]:
    _exact_keys(
        payload,
        _COMMITTED_FIELDS if rich else _COMMIT_OBSERVED_FIELDS,
        "commit payload",
    )
    height = _integer(payload.get("block_height"), "commit height", positive=True)
    block_hash = _hex256(payload.get("block_hash"), "commit block hash")
    parent_hash = _hex256(payload.get("parent_hash"), "commit parent hash")
    transaction_count = _integer(
        payload.get("transaction_count"), "commit transaction count"
    )
    batch_index = _integer(payload.get("commit_batch_index"), "commit batch index")
    return height, block_hash, parent_hash, transaction_count, batch_index


def _live_parse_commits(
    profile: FrozenN31StaticDiagnosisProfile,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    witness_events: dict[int, dict[tuple[object, ...], Mapping[str, Any]]] = {
        replica_id: {} for replica_id in profile.replica_ids
    }
    observer_events: list[tuple[tuple[object, ...], Mapping[str, Any]]] = []
    hashes_by_height: dict[int, set[str]] = {}
    heights_by_hash: dict[str, set[int]] = {}
    metadata: dict[tuple[int, str], set[tuple[object, ...]]] = {}
    for replica_id in profile.replica_ids:
        source_id = f"replica-{replica_id}"
        pending: dict[tuple[int, str], tuple[tuple[object, ...], int]] = {}
        rich_seen: set[tuple[int, str]] = set()
        for event in streams[source_id]:
            event_type = event.get("event_type")
            if event_type not in {"block.commit_observed", "block.committed"}:
                continue
            payload = _mapping(event.get("payload"), "commit payload")
            rich = event_type == "block.committed"
            identity = _live_commit_identity(payload, rich=rich)
            height, block_hash, parent_hash, transaction_count, batch_index = identity
            shared = (height, block_hash, parent_hash, transaction_count)
            hashes_by_height.setdefault(height, set()).add(block_hash)
            heights_by_hash.setdefault(block_hash, set()).add(height)
            metadata.setdefault((height, block_hash), set()).add(
                (parent_hash, transaction_count)
            )
            short = (height, block_hash)
            if not rich:
                if shared in witness_events[replica_id]:
                    _error("duplicate same-source commit witness")
                witness_events[replica_id][shared] = event
                pending[short] = (identity[2:], int(event["source_sequence"]))
                continue
            previous = pending.get(short)
            if (
                previous is None
                or previous[0] != identity[2:]
                or previous[1] >= int(event["source_sequence"])
            ):
                _error("rich commit lacks one matching preceding source witness")
            del pending[short]
            proof = _mapping(payload.get("decision_proof"), "commit decision proof")
            _exact_keys(proof, _DECISION_PROOF_FIELDS, "commit decision proof")
            if (
                proof.get("epoch_number") != profile.epoch_number
                or proof.get("tree_id") not in profile.replica_ids
                or proof.get("epoch_digest") != profile.epoch_digest
                or proof.get("block_hash") != block_hash
            ):
                _error("commit decision proof is outside exact epoch zero")
            if not isinstance(payload.get("designated_observer"), bool):
                _error("commit designated-observer flag is invalid")
            is_observer = replica_id == AUTHORITATIVE_OBSERVER
            if payload.get("designated_observer") is not is_observer:
                _error("commit designated-observer flag disagrees with its source")
            _integer(payload.get("view_generation"), "commit view generation")
            if short in rich_seen:
                _error("duplicate rich commit identity from one source")
            rich_seen.add(short)
            if is_observer:
                observer_events.append((shared, event))
    if any(len(hashes) > 1 for hashes in hashes_by_height.values()):
        _error("conflicting committed hashes were observed at one height")
    if any(len(heights) > 1 for heights in heights_by_hash.values()):
        _error("one committed hash was reused at multiple heights")
    if any(len(values) > 1 for values in metadata.values()):
        _error("same committed identity has metadata drift")
    return {"witnesses": witness_events, "observer": observer_events}


def _live_validate_fault_plan(
    expected: FaultPlan,
    value: bytes | str | Mapping[str, object],
) -> None:
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise N31StaticDiagnosisError("fault plan is not UTF-8") from error
    elif isinstance(value, str):
        text = value
    elif isinstance(value, Mapping):
        try:
            text = json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise N31StaticDiagnosisError("fault plan is not canonical JSON") from error
    else:
        _error("fault plan must be canonical JSON bytes, text, or object")
    if text != expected.canonical_json():
        _error("persisted fault plan differs from the canonical frozen arm")


# ---------------------------------------------------------------------------
# Source-blind signer-aware validator
# ---------------------------------------------------------------------------


def _signer_validate_manifest(
    profile: FrozenN31StaticDiagnosisProfile,
    manifest: Mapping[str, object],
) -> dict[str, Any]:
    _exact_keys(manifest, _MANIFEST_FIELDS, "manifest")
    if manifest.get("schema_version") != 1 or manifest.get("scenario") != SCENARIO:
        _error("manifest scenario or schema identity drifted")
    profile_record = _mapping(manifest.get("profile"), "manifest profile")
    _exact_keys(profile_record, {"profile_id", "sha256"}, "manifest profile")
    if profile_record != {
        "profile_id": profile.profile_id,
        "sha256": profile.profile_sha256,
    }:
        _error("manifest diagnosis profile identity drifted")
    runtime_record = _mapping(
        manifest.get("runtime_profile"), "manifest runtime profile"
    )
    _exact_keys(
        runtime_record,
        {"profile_id", "sha256"},
        "manifest runtime profile",
    )
    if runtime_record != {
        "profile_id": profile.runtime_profile_id,
        "sha256": profile.runtime_profile_sha256,
    }:
        _error("manifest runtime profile binding drifted")
    revision = manifest.get("kauri_revision")
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        _error("manifest Kauri revision must be a full lowercase SHA")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        _error("manifest run identity must be non-empty")
    arm_name = manifest.get("arm")
    if not isinstance(arm_name, str):
        _error("manifest arm identity is invalid")
    arm = _arm(profile, arm_name)
    plan = build_fault_plan(profile, arm)
    if manifest.get("fault_plan_sha256") != plan.sha256:
        _error("manifest fault-plan SHA differs from the frozen arm")
    if (
        type(manifest.get("attempt")) is not int
        or manifest.get("attempt") != 1
        or manifest.get("attempt_scope") != "one_invocation_without_automatic_retry"
        or manifest.get("retry_policy") != "none"
        or manifest.get("complete") is not True
        or manifest.get("runtime_error") is not None
    ):
        _error("only one complete, no-retry, runtime-error-free pilot can PASS")
    for field in ("started_utc", "finished_utc"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            _error(f"manifest {field} is invalid")
    expected_sources = {
        *(f"replica-{replica_id}" for replica_id in profile.replica_ids),
        MANAGER_SOURCE_ID,
    }
    instances = _mapping(manifest.get("source_instances"), "source instances")
    if set(instances) != expected_sources:
        _error("source-instance membership is not exact N31 plus manager")
    instance_values = tuple(instances[source] for source in sorted(expected_sources))
    if (
        any(not isinstance(value, str) or not value for value in instance_values)
        or len(set(instance_values)) != 32
    ):
        _error("source instances are invalid or non-unique")
    _live_validate_preflight(
        profile, value=manifest.get("preflight"), revision=revision
    )
    actor_log_descriptor = _live_validate_preserved_inputs(
        profile,
        manifest=manifest,
        arm=arm,
        source_instances=instances,
    )
    if manifest.get("authoritative_observer") != AUTHORITATIVE_OBSERVER:
        _error("manifest authoritative observer must remain replica 2")
    boundaries = _mapping(
        manifest.get("configuration_boundaries"), "configuration boundaries"
    )
    _exact_keys(boundaries, {"diagnostic"}, "configuration boundaries")
    cleanup_started_ns = _live_validate_cleanup(profile, manifest.get("cleanup_ledger"))
    return {
        "revision": revision,
        "run_id": run_id,
        "arm": arm,
        "plan": plan,
        "source_instances": dict(instances),
        "boundary": boundaries["diagnostic"],
        "cleanup_started_ns": cleanup_started_ns,
        "actor_log_descriptor": actor_log_descriptor,
    }


def _signer_validate_fault_journal(
    profile: FrozenN31StaticDiagnosisProfile,
    *,
    arm: DiagnosticArm,
    plan: FaultPlan,
    fault_journal: Sequence[Mapping[str, object]],
) -> dict[str, Any]:
    if isinstance(fault_journal, (str, bytes, Mapping)) or not isinstance(
        fault_journal, Sequence
    ):
        _error("fault journal must be an event array")
    if len(fault_journal) != 2:
        _error("fault journal requires exact started and terminal records")
    started = _mapping(fault_journal[0], "fault started record")
    terminal = _mapping(fault_journal[1], "fault terminal record")
    _exact_keys(started, _JOURNAL_FIELDS, "fault started record")
    _exact_keys(terminal, {*_JOURNAL_FIELDS, "outcome"}, "fault terminal record")
    for sequence, lifecycle, record in (
        (0, "started", started),
        (1, "terminal", terminal),
    ):
        if (
            record.get("schema_version") != 1
            or record.get("source_id") != "fault-orchestrator"
            or record.get("source_sequence") != sequence
            or record.get("lifecycle") != lifecycle
            or record.get("fault_id") != arm.fault_id
            or record.get("plan_sha256") != plan.sha256
        ):
            _error("fault journal lifecycle or canonical plan identity drifted")
    started_ns = _integer(
        started.get("source_monotonic_ns"), "fault started timestamp", positive=True
    )
    terminal_ns = _integer(
        terminal.get("source_monotonic_ns"), "fault terminal timestamp", positive=True
    )
    if terminal_ns <= started_ns:
        _error("fault lifecycle did not advance monotonically")
    outcome = _mapping(terminal.get("outcome"), "fault terminal outcome")
    _exact_keys(outcome, _FAULT_OUTCOME_FIELDS, "fault terminal outcome")
    expected_source = f"replica-{arm.actor_replica_id}"
    expected_configuration = (
        f"{profile.epoch_number}:{profile.phase.tree_id}:{profile.epoch_digest}"
    )
    if (
        outcome.get("status") != "succeeded"
        or outcome.get("fault_id") != arm.fault_id
        or outcome.get("kind") != arm.runtime_marker
        or outcome.get("source_id") != expected_source
        or outcome.get("actor_replica_id") != arm.actor_replica_id
        or outcome.get("reporter_id") != profile.reporter_id
        or outcome.get("target_id") != profile.target_id
        or outcome.get("configuration") != expected_configuration
        or outcome.get("context_limit") != profile.byzantine_context_limit
        or outcome.get("log_path") != f"logs/{expected_source}.log"
    ):
        _error("fault terminal outcome arm or marker identity drifted")
    _hex256(outcome.get("block_hash"), "fault marker block hash")
    for field in (
        "log_start_offset",
        "log_terminal_offset",
        "matching_line_count",
        "marker_monotonic_ns",
        "pair_observed_monotonic_ns",
    ):
        _integer(
            outcome.get(field),
            f"fault terminal {field}",
            positive=field != "log_start_offset",
        )
    if (
        int(outcome["log_terminal_offset"]) <= int(outcome["log_start_offset"])
        or int(outcome["matching_line_count"]) > profile.byzantine_context_limit
        or not isinstance(outcome.get("line"), str)
        or not outcome["line"]
    ):
        _error("fault terminal log closure is invalid")
    _hex256(
        outcome.get("diagnostic_certificate_sha256"),
        "diagnostic certificate SHA",
    )
    pair_ns = int(outcome["pair_observed_monotonic_ns"])
    marker_ns = int(outcome["marker_monotonic_ns"])
    if marker_ns > pair_ns or pair_ns > terminal_ns:
        _error("marker, pair-observed, and terminal clocks are inconsistent")
    suppression = outcome.get("false_positive_suppression")
    if arm.name == ARM_NAMES[0]:
        record = _mapping(suppression, "false positive suppression marker")
        _exact_keys(record, _SUPPRESSION_FIELDS, "false positive suppression marker")
        if (
            record.get("kind") != arm.positive_suppression_marker
            or record.get("source_id") != expected_source
            or record.get("reporter_id") != profile.reporter_id
            or record.get("target_id") != profile.target_id
            or record.get("configuration") != expected_configuration
            or record.get("block_hash") != outcome.get("block_hash")
            or not isinstance(record.get("line"), str)
            or not record["line"]
        ):
            _error("false positive suppression marker identity drifted")
        suppression_ns = _integer(
            record.get("marker_monotonic_ns"),
            "false positive suppression marker timestamp",
            positive=True,
        )
        if suppression_ns > marker_ns:
            _error("false positive suppression does not precede false timeout")
    elif suppression is not None:
        _error("direct-vote omission arm cannot contain a suppression marker")
    return {
        "started_ns": started_ns,
        "terminal_ns": terminal_ns,
        "outcome": deepcopy(dict(outcome)),
    }


def _marker_tokens(line: str) -> dict[str, str]:
    pairs = re.findall(r"(?:^| )([a-z_]+)=([^ ]+)", line)
    tokens = {key: value for key, value in pairs}
    if len(tokens) != len(pairs):
        _error("Byzantine marker contains duplicate fields")
    return tokens


def _has_exact_marker(line: str, marker: str) -> bool:
    """Match a complete marker name while allowing a logger prefix."""

    return f"{marker} " in line


def _signer_validate_actor_log(
    profile: FrozenN31StaticDiagnosisProfile,
    *,
    arm: DiagnosticArm,
    actor_log: bytes,
    outcome: Mapping[str, Any],
) -> dict[str, object]:
    if not isinstance(actor_log, bytes):
        _error("actor log must be preserved raw bytes")
    start_offset = int(outcome["log_start_offset"])
    terminal_offset = int(outcome["log_terminal_offset"])
    if start_offset > len(actor_log) or terminal_offset > len(actor_log):
        _error("fault log offsets exceed the preserved actor log")
    if (
        terminal_offset <= start_offset
        or actor_log[terminal_offset - 1 : terminal_offset] != b"\n"
    ):
        _error("fault log terminal offset is not one complete-line boundary")
    lines: list[tuple[int, int, str]] = []
    cursor = 0
    for raw_line in actor_log.splitlines(keepends=True):
        end = cursor + len(raw_line)
        lines.append(
            (cursor, end, raw_line.decode("utf-8", errors="replace").rstrip("\r\n"))
        )
        cursor = end
    if cursor < len(actor_log):
        lines.append(
            (
                cursor,
                len(actor_log),
                actor_log[cursor:].decode("utf-8", errors="replace"),
            )
        )
    if any(
        start < offset < end
        for offset in (start_offset, terminal_offset)
        for start, end, _line in lines
    ):
        _error("fault log snapshot or terminal offset bisects a line")
    marker_text = f"KAURI_FAULT {arm.runtime_marker}"
    if any(
        _has_exact_marker(line, marker_text)
        for start, _end, line in lines
        if start < start_offset
    ):
        _error("matching tree-30 fault marker appeared before the clean baseline")
    markers: list[dict[str, object]] = []
    identities: set[str] = set()
    for start, _end, line in lines:
        if (
            start < start_offset
            or start >= terminal_offset
            or not _has_exact_marker(line, marker_text)
        ):
            continue
        tokens = _marker_tokens(line)
        block_hash = tokens.get("block")
        marker_ns_raw = tokens.get("monotonic_ns")
        try:
            marker_ns = int(marker_ns_raw or "")
        except ValueError as error:
            raise N31StaticDiagnosisError(
                "Byzantine marker monotonic timestamp is invalid"
            ) from error
        if (
            block_hash is None
            or _HEX_256.fullmatch(block_hash) is None
            or marker_ns <= 0
            or tokens.get("epoch") != str(profile.epoch_number)
            or tokens.get("tree") != str(profile.phase.tree_id)
            or tokens.get("window") != profile.diagnostic_window
        ):
            _error("Byzantine marker proposal identity or clock drifted")
        if arm.name == ARM_NAMES[0]:
            if tokens.get("reporter") != str(profile.reporter_id) or tokens.get(
                "target"
            ) != str(profile.target_id):
                _error("false-report marker endpoints drifted")
        elif tokens.get("replica") != str(profile.target_id) or tokens.get(
            "parent"
        ) != str(profile.reporter_id):
            _error("direct-vote omission marker endpoints drifted")
        if block_hash in identities:
            _error("duplicate Byzantine marker for one proposal context")
        identities.add(block_hash)
        markers.append(
            {
                "block_hash": block_hash,
                "marker_monotonic_ns": marker_ns,
                "line": line[-1024:],
            }
        )
    if not markers:
        _error("preserved actor log lacks a post-baseline tree-30 marker")
    selected = [
        marker
        for marker in markers
        if marker["block_hash"] == outcome.get("block_hash")
    ]
    if (
        len(selected) != 1
        or selected[0]["line"] != outcome.get("line")
        or selected[0]["marker_monotonic_ns"] != outcome.get("marker_monotonic_ns")
        or len(markers) != outcome.get("matching_line_count")
    ):
        _error("fault journal does not bind the exact preserved marker")
    suppression_record: dict[str, object] | None = None
    suppression_text = "KAURI_FAULT false_report_positive_suppressed"
    if arm.name == ARM_NAMES[0]:
        if any(
            _has_exact_marker(line, suppression_text)
            for start, _end, line in lines
            if start < start_offset
        ):
            _error("matching positive-suppression marker appeared before baseline")
        suppressions: list[dict[str, object]] = []
        for start, _end, line in lines:
            if (
                start < start_offset
                or start >= terminal_offset
                or not _has_exact_marker(line, suppression_text)
            ):
                continue
            tokens = _marker_tokens(line)
            if (
                tokens.get("reporter") != str(profile.reporter_id)
                or tokens.get("target") != str(profile.target_id)
                or tokens.get("epoch") != str(profile.epoch_number)
                or tokens.get("tree") != str(profile.phase.tree_id)
                or tokens.get("window") != profile.diagnostic_window
                or tokens.get("block") != outcome.get("block_hash")
            ):
                continue
            try:
                suppression_ns = int(tokens.get("monotonic_ns", ""))
            except ValueError as error:
                raise N31StaticDiagnosisError(
                    "false positive suppression timestamp is invalid"
                ) from error
            if suppression_ns <= 0:
                _error("false positive suppression timestamp must be positive")
            suppressions.append(
                {
                    "kind": arm.positive_suppression_marker,
                    "source_id": f"replica-{arm.actor_replica_id}",
                    "reporter_id": profile.reporter_id,
                    "target_id": profile.target_id,
                    "configuration": (
                        f"{profile.epoch_number}:{profile.phase.tree_id}:"
                        f"{profile.epoch_digest}"
                    ),
                    "block_hash": outcome["block_hash"],
                    "line": line[-1024:],
                    "marker_monotonic_ns": suppression_ns,
                }
            )
        if len(suppressions) != 1:
            _error("selected false-report context lacks one positive suppression")
        suppression_record = suppressions[0]
        recorded = _mapping(
            outcome.get("false_positive_suppression"),
            "recorded false positive suppression",
        )
        if dict(recorded) != suppression_record:
            _error("fault journal does not bind the exact suppression marker")
        if int(suppression_record["marker_monotonic_ns"]) > int(
            selected[0]["marker_monotonic_ns"]
        ):
            _error("positive suppression does not precede false timeout")
    elif outcome.get("false_positive_suppression") is not None:
        _error("omission outcome contains a false-report suppression marker")
    if arm.name == ARM_NAMES[1]:
        selected_block = str(outcome["block_hash"])
        prohibited = (
            "KAURI_FAULT aggregate_omitted",
            "KAURI_FAULT direct_vote_fallback_sent",
            "KAURI_FAULT timeout_delta_relayed",
        )
        if any(
            selected_block in line and any(token in line for token in prohibited)
            for start, _end, line in lines
            if start_offset <= start < terminal_offset
        ):
            _error("direct-vote omission context contains compensating relay evidence")
    return {"primary": tuple(markers), "suppression": suppression_record}


def _exact_signer_context(
    profile: FrozenN31StaticDiagnosisProfile,
    observation: Mapping[str, Any],
    *,
    block_hash: str,
) -> bool:
    configuration = observation.get("configuration")
    return (
        isinstance(configuration, Mapping)
        and configuration
        == {
            "epoch_number": profile.epoch_number,
            "tree_id": profile.phase.tree_id,
            "epoch_digest": profile.epoch_digest,
        }
        and observation.get("block_hash") == block_hash
    )


def _signer_select_pair(
    profile: FrozenN31StaticDiagnosisProfile,
    accepted: Sequence[Mapping[str, Any]],
    *,
    arm: DiagnosticArm,
    block_hash: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    same_context = [
        candidate
        for candidate in accepted
        if _exact_signer_context(
            profile,
            _mapping(candidate.get("observation"), "candidate observation"),
            block_hash=block_hash,
        )
    ]
    claims = [
        candidate
        for candidate in same_context
        if candidate["observation"].get("reporter_id") == profile.reporter_id
        and candidate["observation"].get("observed_replica_id") == profile.target_id
        and candidate["observation"].get("expected_message_type")
        == profile.claim_expected_message_type
    ]
    witnesses = [
        candidate
        for candidate in same_context
        if candidate["observation"].get("reporter_id") == profile.witness_reporter_id
        and candidate["observation"].get("observed_replica_id") == profile.reporter_id
        and candidate["observation"].get("expected_message_type")
        == profile.witness_expected_message_type
    ]
    if len(claims) != 1:
        _error("same-context reporter-0 direct-vote claim is missing or duplicate")
    if len(witnesses) != 1:
        _error("same-context root-30 aggregate witness is missing or duplicate")
    claim = claims[0]
    witness = witnesses[0]
    if (
        claim["observation"].get("outcome") != "timeout"
        or claim["observation"].get("signer_set") != []
    ):
        _error("reporter-0 claim is not the exact leaf-5 timeout")
    if (
        witness["observation"].get("outcome") != "on_time"
        or tuple(witness["observation"].get("signer_set", ())) != arm.witness_signer_set
    ):
        _error("root-30 aggregate does not carry the arm's exact full signer set")
    if claim["observation"].get("observation_id") == witness["observation"].get(
        "observation_id"
    ):
        _error("claim and witness observation identities are not independent")
    if arm.name == ARM_NAMES[1]:
        compensating = [
            candidate
            for candidate in same_context
            if candidate["observation"].get("reporter_id")
            == profile.witness_reporter_id
            and profile.target_id
            in tuple(candidate["observation"].get("signer_set", ()))
        ]
        if compensating:
            _error("leaf 5 reappeared in root-30 evidence for the omitted proposal")
    return claim, witness


def _signer_select_root_qc(
    profile: FrozenN31StaticDiagnosisProfile,
    events: Sequence[Mapping[str, Any]],
    *,
    arm: DiagnosticArm,
    block_hash: str,
    boundary_max_ns: int,
) -> Mapping[str, Any]:
    same_context: list[Mapping[str, Any]] = []
    qcs: list[Mapping[str, Any]] = []
    for event in events:
        if event.get("event_type") not in {
            "aggregation.root_quorum_progress",
            "aggregation.root_qc_published",
        }:
            continue
        payload = _mapping(event.get("payload"), "root progress payload")
        if (
            payload.get("epoch_number") != profile.epoch_number
            or payload.get("tree_id") != profile.phase.tree_id
            or payload.get("epoch_digest") != profile.epoch_digest
            or payload.get("block_hash") != block_hash
            or payload.get("observer_replica") != profile.witness_reporter_id
        ):
            continue
        _exact_keys(payload, _CONFIGURATION_ACTIVE_FIELDS, "root progress payload")
        context_generation = _integer(
            payload.get("context_generation"),
            "root progress context generation",
            positive=True,
        )
        accepted = _integer_tuple(
            payload.get("accepted_signers"), "root progress accepted signers"
        )
        auxiliary: dict[str, tuple[int, ...]] = {}
        for field in (
            "wait_exempt_signers",
            "absent_direct_children",
            "missing_optional_signers",
        ):
            values = _integer_tuple(payload.get(field), f"root progress {field}")
            if values != tuple(sorted(set(values))) or any(
                signer not in profile.replica_ids for signer in values
            ):
                _error(f"root progress {field} is non-canonical")
            auxiliary[field] = values
        gaps = _sequence(
            payload.get("required_branch_gaps"),
            "root progress required branch gaps",
        )
        previous_child = -1
        for raw_gap in gaps:
            gap = _mapping(raw_gap, "root progress required branch gap")
            _exact_keys(
                gap,
                {"direct_child", "missing_required_signers"},
                "root progress required branch gap",
            )
            child = _integer(gap.get("direct_child"), "root progress direct child")
            missing = _integer_tuple(
                gap.get("missing_required_signers"),
                "root progress missing required signers",
            )
            if (
                child not in profile.replica_ids
                or child <= previous_child
                or not missing
                or missing != tuple(sorted(set(missing)))
                or any(signer not in profile.replica_ids for signer in missing)
            ):
                _error("root progress required branch gap is non-canonical")
            previous_child = child
        if (
            context_generation <= 0
            or accepted != tuple(sorted(set(accepted)))
            or any(signer not in profile.replica_ids for signer in accepted)
            or auxiliary["wait_exempt_signers"] != ()
            or _integer(payload.get("root_signer_count"), "root signer count")
            != len(accepted)
            or _integer(payload.get("global_quorum"), "root global quorum")
            != profile.quorum
            or payload.get("rejection_reason") is not None
            or _integer(
                event.get("source_monotonic_ns"),
                "root progress timestamp",
                positive=True,
            )
            <= boundary_max_ns
        ):
            _error("same-context root progress payload is non-canonical")
        same_context.append(event)
        if arm.name == ARM_NAMES[1] and profile.target_id in accepted:
            _error("leaf 5 reappeared in same-context root progress or QC")
        if event.get("event_type") == "aggregation.root_qc_published":
            if len(accepted) < profile.quorum:
                _error("root QC publication does not preserve Q21")
            qcs.append(event)
    if len(qcs) != 1:
        _error("selected proposal requires exactly one replica-30 root QC")
    qc_payload = _mapping(qcs[0].get("payload"), "selected root QC payload")
    accepted = tuple(qc_payload["accepted_signers"])
    if not set(arm.witness_signer_set) <= set(accepted):
        _error("root QC does not include the complete witnessed branch aggregate")
    expected_target_present = arm.name == ARM_NAMES[0]
    if (profile.target_id in accepted) != expected_target_present:
        _error("root QC leaf-5 membership disagrees with the declared arm")
    return qcs[0]


def build_signer_aware_certificates(
    profile: FrozenN31StaticDiagnosisProfile,
    arm: DiagnosticArm,
    claim: Mapping[str, Any],
    witness: Mapping[str, Any],
) -> tuple[dict[str, object], dict[str, object]]:
    claim_observation = _mapping(claim.get("observation"), "claim observation")
    witness_observation = _mapping(witness.get("observation"), "witness observation")
    hypotheses = (
        {"kind": "false_reporter", "replica_id": profile.reporter_id},
        {"kind": "direct_vote_omitter", "replica_id": profile.target_id},
    )
    proposal = {
        "epoch_number": profile.epoch_number,
        "tree_id": profile.phase.tree_id,
        "epoch_digest": profile.epoch_digest,
        "block_hash": claim_observation["block_hash"],
    }
    response_only: dict[str, object] = {
        "schema_version": 1,
        "classifier": "response_only_projection",
        "status": "ambiguous",
        "proposal": proposal,
        "observation_ids": [
            claim_observation["observation_id"],
            witness_observation["observation_id"],
        ],
        "projected_edges": [
            {
                "reporter_id": profile.reporter_id,
                "observed_replica_id": profile.target_id,
                "outcome": "timeout",
            },
            {
                "reporter_id": profile.witness_reporter_id,
                "observed_replica_id": profile.reporter_id,
                "outcome": "response",
            },
        ],
        "omitted_field": "signer_set",
        "compatible_hypothesis_count": 2,
        "compatible_hypotheses": list(hypotheses),
    }
    response_only["certificate_sha256"] = hashlib.sha256(
        json.dumps(
            response_only,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    signer_aware: dict[str, object] = {
        "schema_version": 1,
        "classifier": profile.classifier,
        "status": "settled",
        "proposal": proposal,
        "observation_ids": [
            claim_observation["observation_id"],
            witness_observation["observation_id"],
        ],
        "claim": {
            "reporter_id": profile.reporter_id,
            "observed_replica_id": profile.target_id,
            "expected_message_type": profile.claim_expected_message_type,
            "outcome": "timeout",
            "signer_set": [],
        },
        "witness": {
            "reporter_id": profile.witness_reporter_id,
            "observed_replica_id": profile.reporter_id,
            "expected_message_type": profile.witness_expected_message_type,
            "outcome": "on_time",
            "signer_set": list(arm.witness_signer_set),
        },
        "declared_mode_count": 2,
        "compatible_hypothesis_count": 1,
        "settled_hypothesis": arm.settled_hypothesis,
        "limitation": profile.limitation,
    }
    signer_aware["certificate_sha256"] = hashlib.sha256(
        json.dumps(
            signer_aware,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return response_only, signer_aware


def _signer_common_commits(
    profile: FrozenN31StaticDiagnosisProfile,
    commits: Mapping[str, Any],
) -> tuple[tuple[int, dict[str, object]], ...]:
    values: list[tuple[int, dict[str, object]]] = []
    witness_maps = commits["witnesses"]
    for shared, observer_event in commits["observer"]:
        witnesses = [
            witness_maps[replica_id].get(shared)
            for replica_id in profile.commit_witnesses
        ]
        if any(witness is None for witness in witnesses):
            continue
        typed_witnesses = [
            _mapping(witness, "fixed-Q21 commit witness") for witness in witnesses
        ]
        observer_ns = int(observer_event["source_monotonic_ns"])
        common_ns = max(
            observer_ns,
            *(int(event["source_monotonic_ns"]) for event in typed_witnesses),
        )
        payload = _mapping(observer_event["payload"], "authoritative commit payload")
        values.append(
            (
                common_ns,
                {
                    "block_height": shared[0],
                    "block_hash": shared[1],
                    "parent_hash": shared[2],
                    "transaction_count": shared[3],
                    "authoritative_observer": AUTHORITATIVE_OBSERVER,
                    "observer_monotonic_ns": observer_ns,
                    "common_monotonic_ns": common_ns,
                    "participant_count": profile.quorum,
                    "participants": list(profile.commit_witnesses),
                    "decision_proof": deepcopy(dict(payload["decision_proof"])),
                },
            )
        )
    if not values:
        _error("no fixed-Q21 observer/witness commit is preserved")
    return tuple(values)


def _signer_select_ancestry_commit(
    common_commits: Sequence[tuple[int, Mapping[str, object]]],
    *,
    observer_commit_events: Mapping[tuple[object, ...], Mapping[str, Any]],
    baseline: Mapping[str, object],
    after_ns: int,
    before_ns: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    observer_by_hash = {
        str(shared[1]): (shared, event)
        for shared, event in observer_commit_events.items()
    }
    baseline_hash = str(baseline["block_hash"])
    baseline_height = int(baseline["block_height"])
    candidates: list[tuple[int, dict[str, object], list[dict[str, object]]]] = []
    for timestamp, raw_candidate in common_commits:
        if (
            timestamp <= after_ns
            or timestamp >= before_ns
            or int(raw_candidate["block_height"]) <= baseline_height
        ):
            continue
        chain_events: list[tuple[tuple[object, ...], Mapping[str, Any]]] = []
        current_hash = str(raw_candidate["block_hash"])
        seen = {current_hash}
        valid = False
        while True:
            current_entry = observer_by_hash.get(current_hash)
            if current_entry is None:
                break
            shared, event = current_entry
            chain_events.append(current_entry)
            current_height = int(shared[0])
            parent_hash = str(shared[2])
            if parent_hash == baseline_hash:
                if current_height != baseline_height + 1:
                    break
                valid = True
                break
            parent_entry = observer_by_hash.get(parent_hash)
            if parent_entry is None or parent_hash in seen:
                break
            parent_shared, _parent_event = parent_entry
            if int(parent_shared[0]) != current_height - 1:
                break
            seen.add(parent_hash)
            current_hash = parent_hash
        if valid:
            chain_events.reverse()
            baseline_entry = observer_by_hash.get(baseline_hash)
            if baseline_entry is None:
                _error("replica-2 baseline commit witness is absent")
            chain_events.insert(0, baseline_entry)
            sequences = [
                int(event["source_sequence"]) for _shared, event in chain_events
            ]
            timestamps = [
                int(event["source_monotonic_ns"]) for _shared, event in chain_events
            ]
            if any(
                right <= left for left, right in zip(sequences, sequences[1:])
            ) or any(right < left for left, right in zip(timestamps, timestamps[1:])):
                _error("replica-2 commit ancestry order is non-monotonic")
            chain = [
                {
                    "block_height": shared[0],
                    "block_hash": shared[1],
                    "parent_hash": shared[2],
                }
                for shared, _event in chain_events
            ]
            candidates.append((timestamp, deepcopy(dict(raw_candidate)), chain))
    if not candidates:
        _error("no later fixed-Q21 commit has preserved ancestry to the baseline")
    _timestamp, later, chain = min(candidates, key=lambda value: value[0])
    return later, chain


def _earliest_bindable_signer_marker(
    profile: FrozenN31StaticDiagnosisProfile,
    markers: Sequence[Mapping[str, object]],
    accepted: Sequence[Mapping[str, Any]],
    *,
    arm: DiagnosticArm,
    selected_block_hash: str,
) -> None:
    positions = [
        index
        for index, marker in enumerate(markers)
        if marker.get("block_hash") == selected_block_hash
    ]
    if len(positions) != 1:
        _error("fault journal does not select one preserved marker")
    for marker in markers[: positions[0]]:
        try:
            _signer_select_pair(
                profile,
                accepted,
                arm=arm,
                block_hash=str(marker["block_hash"]),
            )
        except N31StaticDiagnosisError:
            continue
        _error("fault journal did not select the earliest bindable marker")


def validate_n31_static_diagnosis_run(
    profile: FrozenN31StaticDiagnosisProfile,
    *,
    manifest: Mapping[str, object],
    streams: Mapping[str, Sequence[Mapping[str, object]]],
    fault_plan: bytes | str | Mapping[str, object],
    fault_journal: Sequence[Mapping[str, object]],
    actor_log: bytes,
) -> dict[str, object]:
    """Validate one sealed, no-retry, signer-aware N31 pilot from raw sources."""

    _require_exact_profile_instance(profile)
    manifest_result = _signer_validate_manifest(profile, _mapping(manifest, "manifest"))
    revision = str(manifest_result["revision"])
    run_id = str(manifest_result["run_id"])
    arm = manifest_result["arm"]
    plan = manifest_result["plan"]
    if not isinstance(arm, DiagnosticArm) or not isinstance(plan, FaultPlan):
        _error("manifest arm or fault plan is malformed")
    if not isinstance(actor_log, bytes):
        _error("actor log must be preserved raw bytes")
    actor_log_descriptor = _mapping(
        manifest_result["actor_log_descriptor"], "actor log descriptor"
    )
    if actor_log_descriptor.get("sha256") != hashlib.sha256(actor_log).hexdigest():
        _error("actor log bytes differ from the manifest SHA-256")
    _live_validate_fault_plan(plan, fault_plan)
    if not isinstance(streams, Mapping):
        _error("structured streams must be an object")
    normalized, ready_timestamps = _live_validate_event_streams(
        profile,
        streams=streams,
        run_id=run_id,
        source_instances=manifest_result["source_instances"],
        cleanup_started_ns=int(manifest_result["cleanup_started_ns"]),
    )
    boundary = _live_validate_boundary(
        profile,
        normalized,
        value=manifest_result["boundary"],
        phase=profile.phase,
    )
    boundary_min_ns = int(boundary["minimum_ns"])
    boundary_max_ns = int(boundary["maximum_ns"])
    if boundary_max_ns - boundary_min_ns > profile.boundary_max_skew_ns:
        _error("common tree-30 boundary exceeds the frozen 0.5-second skew")
    for replica_id in profile.replica_ids:
        source = f"replica-{replica_id}"
        if boundary["timestamps"][source] <= ready_timestamps[source]:
            _error("tree-30 boundary does not follow exact all-source readiness")

    journal = _signer_validate_fault_journal(
        profile,
        arm=arm,
        plan=plan,
        fault_journal=fault_journal,
    )
    outcome = _mapping(journal["outcome"], "fault terminal outcome")
    actor_markers = _signer_validate_actor_log(
        profile,
        arm=arm,
        actor_log=actor_log,
        outcome=outcome,
    )
    accepted = _live_parse_observations(profile, normalized[MANAGER_SOURCE_ID])
    selected_block_hash = str(outcome["block_hash"])
    root_qc_event = _signer_select_root_qc(
        profile,
        normalized[f"replica-{profile.witness_reporter_id}"],
        arm=arm,
        block_hash=selected_block_hash,
        boundary_max_ns=boundary_max_ns,
    )
    root_qc_ns = int(root_qc_event["source_monotonic_ns"])
    _earliest_bindable_signer_marker(
        profile,
        _sequence(actor_markers["primary"], "primary actor markers"),
        accepted,
        arm=arm,
        selected_block_hash=selected_block_hash,
    )
    claim, witness = _signer_select_pair(
        profile,
        accepted,
        arm=arm,
        block_hash=selected_block_hash,
    )
    claim_event = _mapping(claim["event"], "claim manager event")
    witness_event = _mapping(witness["event"], "witness manager event")
    claim_observation = _mapping(claim["observation"], "claim observation")
    witness_observation = _mapping(witness["observation"], "witness observation")
    claim_reporter_ns = int(claim_observation["reporter_monotonic_ns"])
    witness_reporter_ns = int(witness_observation["reporter_monotonic_ns"])
    claim_manager_ns = int(claim_event["source_monotonic_ns"])
    witness_manager_ns = int(witness_event["source_monotonic_ns"])
    settled_ns = max(claim_manager_ns, witness_manager_ns)
    marker_ns = int(outcome["marker_monotonic_ns"])
    pair_ns = int(outcome["pair_observed_monotonic_ns"])
    if not (
        boundary_max_ns < claim_reporter_ns <= claim_manager_ns <= pair_ns
        and boundary_max_ns < witness_reporter_ns <= witness_manager_ns <= pair_ns
        and boundary_max_ns < marker_ns <= pair_ns
    ):
        _error("tree-30 marker or observations do not follow the common boundary")
    if witness_reporter_ns > root_qc_ns:
        _error("root aggregate witness was timestamped after root QC publication")
    if arm.name == ARM_NAMES[0]:
        if claim_reporter_ns > marker_ns:
            _error("false-timeout marker precedes the reporter's timeout evidence")
        suppression = _mapping(
            actor_markers.get("suppression"), "false positive suppression"
        )
        suppression_ns = int(suppression["marker_monotonic_ns"])
        if not boundary_max_ns < suppression_ns <= marker_ns:
            _error("positive suppression is outside boundary-to-timeout causality")
        if suppression_ns > witness_reporter_ns:
            _error("positive suppression follows the honest root aggregate witness")
    elif marker_ns > min(claim_reporter_ns, witness_reporter_ns):
        _error("direct-vote omission marker follows timeout or aggregate evidence")
    if root_qc_ns > pair_ns:
        _error("root QC was not observed before diagnostic lifecycle closure")
    if int(journal["started_ns"]) > boundary_min_ns:
        _error("fault lifecycle started after the selected tree-30 boundary")
    if int(journal["terminal_ns"]) >= int(manifest_result["cleanup_started_ns"]):
        _error("fault lifecycle did not finish before cleanup")

    response_only, signer_aware = build_signer_aware_certificates(
        profile, arm, claim, witness
    )
    if (
        response_only.get("compatible_hypothesis_count") != 2
        or signer_aware.get("compatible_hypothesis_count") != 1
        or signer_aware.get("settled_hypothesis") != arm.settled_hypothesis
    ):
        _error("signer-aware diagnosis did not narrow B2 to B1")
    if outcome.get("diagnostic_certificate_sha256") != signer_aware.get(
        "certificate_sha256"
    ):
        _error("fault journal certificate digest differs from raw evidence")

    commits = _live_parse_commits(profile, normalized)
    common_commits = _signer_common_commits(profile, commits)
    ready_barrier_ns = max(ready_timestamps.values())
    baseline_candidates = [
        (timestamp, commit)
        for timestamp, commit in common_commits
        if ready_barrier_ns < timestamp < int(journal["started_ns"])
    ]
    if not baseline_candidates:
        _error("no clean fixed-Q21 baseline precedes fault lifecycle start")
    baseline_ns, baseline = max(baseline_candidates, key=lambda value: value[0])
    if baseline_ns >= boundary_min_ns:
        _error("clean baseline does not precede the tree-30 action boundary")
    later, ancestry = _signer_select_ancestry_commit(
        common_commits,
        observer_commit_events=commits["witnesses"][AUTHORITATIVE_OBSERVER],
        baseline=baseline,
        after_ns=pair_ns,
        before_ns=int(manifest_result["cleanup_started_ns"]),
    )
    if int(later["common_monotonic_ns"]) >= int(manifest_result["cleanup_started_ns"]):
        _error("cleanup precedes the later ancestry-qualified commit")

    raw_observations = [
        deepcopy(dict(claim_observation)),
        deepcopy(dict(witness_observation)),
    ]
    return {
        "schema_version": 1,
        "scenario": SCENARIO,
        "verdict": "PASS",
        "claim_state": "live-verified",
        "profile_id": profile.profile_id,
        "profile_sha256": profile.profile_sha256,
        "kauri_revision": revision,
        "run_id": run_id,
        "arm": arm.name,
        "attempt": 1,
        "evidence_ceiling": profile.pilot_ceiling,
        "figure_eligible": False,
        "figure_eligibility_rule": profile.figure_eligibility,
        "fixed_context": {
            "replica_count": 31,
            "fault_threshold": 10,
            "quorum": 21,
            "commit_witnesses": list(profile.commit_witnesses),
            "fanout": 5,
            "topology_depth": 2,
            "pipeline_stretch": 2,
            "epoch_number": 0,
            "tree_id": 30,
            "epoch_digest": profile.epoch_digest,
            "reporter_subtree": list(profile.phase.reporter_subtree),
        },
        "readiness": {
            "required_sources": 32,
            "observed_sources": len(ready_timestamps),
            "barrier_monotonic_ns": ready_barrier_ns,
        },
        "clean_baseline_common_commit": deepcopy(dict(baseline)),
        "diagnostic": {
            "classifier": profile.classifier,
            "selection_order": profile.selection_order,
            "same_proposal_identity": {
                "epoch_number": 0,
                "tree_id": 30,
                "epoch_digest": profile.epoch_digest,
                "block_hash": selected_block_hash,
            },
            "response_only_hypothesis_count": 2,
            "signer_aware_hypothesis_count": 1,
            "response_only_certificate": response_only,
            "signer_aware_certificate": signer_aware,
            "raw_observations": raw_observations,
            "claim_manager_receipt_ns": claim_manager_ns,
            "witness_manager_receipt_ns": witness_manager_ns,
            "settled_monotonic_ns": settled_ns,
            "settlement_latency_ns": settled_ns - boundary_max_ns,
            "marker_monotonic_ns": marker_ns,
            "pair_observed_monotonic_ns": pair_ns,
            "root_qc_crosscheck": {
                "source_id": f"replica-{profile.witness_reporter_id}",
                "source_sequence": root_qc_event["source_sequence"],
                "source_monotonic_ns": root_qc_ns,
                "accepted_signers": list(root_qc_event["payload"]["accepted_signers"]),
                "target_present": profile.target_id
                in root_qc_event["payload"]["accepted_signers"],
            },
            "added_protocol_messages": 0,
            "added_protocol_trees": 0,
            "forced_tree_rotations": 0,
            "limitation": profile.limitation,
        },
        "later_common_commit": later,
        "commit_ancestry": {
            "baseline_height": baseline["block_height"],
            "later_height": later["block_height"],
            "link_count": len(ancestry) - 1,
            "chain": [
                {
                    "block_height": commit["block_height"],
                    "block_hash": commit["block_hash"],
                    "parent_hash": commit["parent_hash"],
                }
                for commit in ancestry
            ],
        },
        "safety_checks": {
            "successor_activations": 0,
            "conflicting_commits": 0,
            "retries": 0,
            "restarts": 0,
            "fault_actor_commit_witnesses": 0,
        },
    }


__all__ = (
    "ARM_NAMES",
    "DiagnosticArm",
    "DiagnosticPhase",
    "FrozenN31StaticDiagnosisProfile",
    "N31StaticDiagnosisError",
    "SCENARIO",
    "SHIPPED_PROFILE_ID",
    "SHIPPED_PROFILE_SHA256",
    "build_fault_plan",
    "build_launch_contract",
    "build_signer_aware_certificates",
    "load_frozen_profile",
    "validate_n31_static_diagnosis_run",
)
