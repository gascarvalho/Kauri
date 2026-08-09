"""Bounded, non-claim diagnosis for the v13 N=31/fanout-five P slot.

This module deliberately does not call the factorial campaign executor.  It
derives the exact frozen slot inputs, but places each diagnostic attempt in a
separate archive and never contributes a run, replacement, denominator row,
figure, or performance claim to SHAPE25.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid as uuid_module
from typing import Any

from . import factorial_execution as _factorial
from .factorial_execution import (
    ExecutionBinaries,
    FactorialExecutionError,
    generate_identities,
    materialize_launch,
    preserve_build_evidence,
    read_event_streams,
    slot_ports,
    spawn_exclusive_owned_process,
    verify_evidence_preflight,
    write_slot_configs,
)
from .factorial_manifest import (
    FactorialSlot,
    build_factorial_plan,
    load_frozen_manifest,
)
from .factorial_runtime import (
    SlotRuntimeSpec,
    build_factorial_runtime,
    canonical_runtime_bytes,
)
from .factorial_validation import (
    FactorialValidationError,
    decode_epoch_change_bundle,
)
from .processes import CleanupOutcome, ProcessRecord, ProcessRegistry
from .profiled_fault_archive import (
    EvidenceSealError,
    create_evidence_seal as _create_archive_seal,
    verify_evidence_seal as _verify_archive_seal,
)


REPOSITORY = Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class N31LivenessShakedownRelease:
    profile_id: str
    profile_path: Path
    profile_sha256: str
    canonical_results_relative_path: Path
    approval_scope: str


SHIPPED_RELEASES = (
    N31LivenessShakedownRelease(
        profile_id="n31-f5-p-liveness-shakedown-v1",
        profile_path=(
            REPOSITORY
            / "experiments/adaptive/profiles/n31-f5-p-liveness-shakedown-v1.json"
        ),
        profile_sha256=(
            "906a62db3c3fc636acc9961c9cd65c23625b2eb75ff39f6d49f3c21fc5845b6f"
        ),
        canonical_results_relative_path=Path(
            "results/n31-f5-p-liveness-shakedown-v1"
        ),
        approval_scope="n31_f5_p_liveness_shakedown_non_claim_v1",
    ),
    N31LivenessShakedownRelease(
        profile_id="n31-f5-p-liveness-shakedown-v2",
        profile_path=(
            REPOSITORY
            / "experiments/adaptive/profiles/n31-f5-p-liveness-shakedown-v2.json"
        ),
        profile_sha256=(
            "a1dd1e1c53c7ba01b0f7410e60bf13d409742d5cd5be8423e8403d22d31a3d28"
        ),
        canonical_results_relative_path=Path(
            "results/n31-f5-p-liveness-shakedown-v2"
        ),
        approval_scope="n31_f5_p_liveness_shakedown_non_claim_v2",
    ),
)
DEFAULT_RELEASE = SHIPPED_RELEASES[-1]
DEFAULT_PROFILE_PATH = DEFAULT_RELEASE.profile_path
SOURCE_MANIFEST_RELATIVE_PATH = Path(
    "experiments/adaptive/profiles/shape-placement-factorial-v13.json"
)
PROFILE_ID = DEFAULT_RELEASE.profile_id
SOURCE_SLOT_ID = "slot-066-n31-f5-b05-P"
CANONICAL_RESULTS_RELATIVE_PATH = DEFAULT_RELEASE.canonical_results_relative_path
SHIPPED_PROFILE_SHA256 = DEFAULT_RELEASE.profile_sha256
APPROVAL_SCOPE = DEFAULT_RELEASE.approval_scope
VERDICTS = frozenset({"REPRODUCED", "NOT_REPRODUCED", "INCOMPLETE"})
FROZEN_REPLICA_COUNT = 31
NANOSECONDS_PER_SECOND = 1_000_000_000
ATTEMPT_NAME = re.compile(
    r"^attempt-(?P<ordinal>0[12])-(?P<timestamp>[0-9]{8}T[0-9]{6}\.[0-9]{6}Z)-"
    r"pid(?P<pid>[1-9][0-9]*)-(?P<uuid>[0-9a-f-]{36})$"
)
TIMESTAMP = re.compile(r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z$")
LOWER_HEX = frozenset("0123456789abcdef")
LEADER_TIMEOUT_PATTERN = re.compile(
    rb"\[EPOCH\] Rotated active epoch=(?P<epoch>[0-9]+) to tree="
    rb"(?P<tree>[0-9]+) after leader timeout"
)
QUEUE_BLOCKED_EVENT_TYPE = "pipeline.root_qc_queue_blocked"
QUEUE_BLOCKED_FIELDS = frozenset(
    {
        "epoch_number",
        "tree_id",
        "epoch_digest",
        "observer_replica",
        "global_quorum",
        "queue_head_position",
        "queued_candidate_position",
        "queue_head_context_generation",
        "queued_candidate_context_generation",
        "queue_head_block_height",
        "queue_head_block_hash",
        "queued_candidate_block_height",
        "queued_candidate_block_hash",
        "queued_candidate_parent_hash",
        "queue_head_signer_count",
        "queued_candidate_signer_count",
        "queued_candidate_qc_ready",
        "queued_candidate_qc_published",
    }
)


class N31LivenessShakedownError(RuntimeError):
    """The diagnostic attempt cannot be trusted or executed safely."""


def _release_for_profile_id(profile_id: object) -> N31LivenessShakedownRelease:
    for release in SHIPPED_RELEASES:
        if profile_id == release.profile_id:
            return release
    raise N31LivenessShakedownError(
        "profile id is not a shipped liveness release"
    )


@dataclass(frozen=True, slots=True)
class FrozenN31LivenessProfile:
    schema_version: int
    profile_id: str
    frozen: bool
    diagnostic_only: bool
    source: dict[str, object]
    consensus: dict[str, object]
    adaptation: dict[str, object]
    fault_cohorts: dict[str, object]
    workload: dict[str, object]
    responsiveness_policy: dict[str, object]
    timers: dict[str, object]
    transitions: list[dict[str, object]]
    fault_window: dict[str, object]
    phase_windows: dict[str, object]
    ports: dict[str, object]
    attempt_policy: dict[str, object]
    evidence_scope: dict[str, object]
    observation: dict[str, object]
    profile_sha256: str


def _release_for_profile(
    profile: FrozenN31LivenessProfile,
) -> N31LivenessShakedownRelease:
    release = _release_for_profile_id(profile.profile_id)
    if profile.profile_sha256 != release.profile_sha256:
        raise N31LivenessShakedownError(
            "profile release identity is inconsistent"
        )
    return release


def _shipped_profile_bytes(profile: FrozenN31LivenessProfile) -> bytes:
    release = _release_for_profile(profile)
    try:
        payload = release.profile_path.read_bytes()
    except OSError as error:
        raise N31LivenessShakedownError(
            f"cannot read shipped profile: {error}"
        ) from error
    document = _json_object(payload, "shipped profile")
    if (
        document.get("profile_id") != release.profile_id
        or _sha256_bytes(payload) != release.profile_sha256
    ):
        raise N31LivenessShakedownError(
            "shipped profile release bytes drifted"
        )
    return payload


@dataclass(frozen=True, slots=True)
class SourceSlot:
    slot: FactorialSlot
    runtime: SlotRuntimeSpec
    manifest_bytes: bytes
    plan_bytes: bytes
    runtime_bytes: bytes


def _canonical_json_bytes(value: object, *, newline: bool = False) -> bytes:
    payload = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in LOWER_HEX for character in value)
    )


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise N31LivenessShakedownError(
                    f"{label} contains duplicate key: {key}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise N31LivenessShakedownError(
            f"{label} contains invalid JSON constant: {value}"
        )

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise N31LivenessShakedownError(
            f"{label} must be valid UTF-8 JSON"
        ) from error
    if not isinstance(value, dict):
        raise N31LivenessShakedownError(f"{label} must be a JSON object")
    return value


def _source_slot_from_manifest(manifest_path: Path) -> SourceSlot:
    manifest_path = Path(manifest_path)
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = load_frozen_manifest(manifest_path)
        plan = build_factorial_plan(manifest)
        runtime_plan = build_factorial_runtime(plan)
    except (OSError, ValueError) as error:
        raise N31LivenessShakedownError(
            f"cannot derive exact v13 source slot: {error}"
        ) from error
    slots = tuple(slot for slot in plan.slots if slot.slot_id == SOURCE_SLOT_ID)
    runtimes = tuple(
        runtime for runtime in runtime_plan.slots if runtime.slot_id == SOURCE_SLOT_ID
    )
    if len(slots) != 1 or len(runtimes) != 1:
        raise N31LivenessShakedownError(
            "v13 source does not contain exactly one slot-066 P runtime"
        )
    return SourceSlot(
        slot=slots[0],
        runtime=runtimes[0],
        manifest_bytes=manifest_bytes,
        plan_bytes=plan.canonical_bytes,
        runtime_bytes=canonical_runtime_bytes(runtime_plan),
    )


def _source_slot(repository: Path = REPOSITORY) -> SourceSlot:
    repository = Path(repository).resolve()
    return _source_slot_from_manifest(
        repository / SOURCE_MANIFEST_RELATIVE_PATH
    )


def _validate_profile_against_source(
    document: Mapping[str, Any],
    source: SourceSlot,
) -> None:
    slot_document = source.slot.as_document()
    runtime_document = source.runtime.as_document()
    expected_source = {
        "manifest_id": "shape-placement-factorial-v13",
        "manifest_sha256": _sha256_bytes(source.manifest_bytes),
        "plan_sha256": _sha256_bytes(source.plan_bytes),
        "slot_id": SOURCE_SLOT_ID,
        "slot_ordinal": 66,
        "block_id": "n31-f5-b05",
        "arm_code": "P",
        "scientific_seed": 41735,
    }
    if document.get("source") != expected_source:
        raise N31LivenessShakedownError(
            "profile source binding differs from exact v13 slot-066"
        )
    comparisons = {
        "consensus": {
            "replica_count": slot_document["consensus"]["replica_count"],
            "fault_threshold": slot_document["consensus"]["f"],
            "quorum": slot_document["consensus"]["q"],
            "initial_fanout": slot_document["consensus"]["initial_fanout"],
            "pipeline_stretch": slot_document["pipeline_stretch"],
            "tree_count": slot_document["consensus"]["tree_count"],
        },
        "adaptation": {
            "placement": slot_document["arm"]["placement_adaptation"],
            "shape": slot_document["arm"]["shape_adaptation"],
            "candidate_fanouts": list(slot_document["candidate_fanouts"]),
            "epoch_fanout_policy": slot_document["epoch_fanout_policy"],
            "pipeline_policy": slot_document["pipeline_policy"],
        },
        "workload": {
            "block_size": slot_document["workload"]["block_size"],
            "piped_latency_ms": slot_document["workload"]["piped_latency_ms"],
            "tree_switch_period_blocks": slot_document["workload"][
                "tree_switch_period_blocks"
            ],
        },
        "responsiveness_policy": slot_document["responsiveness_policy"],
        "ports": slot_document["ports"],
    }
    for field, expected in comparisons.items():
        if document.get(field) != expected:
            raise N31LivenessShakedownError(
                f"profile {field} differs from exact v13 slot-066"
            )
    byzantine = slot_document["byzantine"]
    responsive = byzantine["responsive_degradation"]
    if not isinstance(responsive, Mapping):
        raise N31LivenessShakedownError("v13 responsive degradation is absent")
    expected_faults = {
        "mode": byzantine["mode"],
        "hard_actor_ids": list(slot_document["byzantine_actor_ids"]),
        "responsive_degraded_actor_ids": list(
            slot_document["responsive_degraded_actor_ids"]
        ),
        "responsive_omission_period": responsive["omission_period"],
        "hard_actions": byzantine["actions"],
    }
    if document.get("fault_cohorts") != expected_faults:
        raise N31LivenessShakedownError(
            "profile fault cohorts differ from exact v13 slot-066"
        )
    timers = slot_document["common_timers"]
    expected_timers = {
        key: timers[key]
        for key in (
            "global_worst_candidate_depth",
            "aggregation_timeout_ms_per_depth",
            "leader_progress_timeout_ms_per_depth",
            "leader_activation_grace_ms",
            "activation_delay_blocks",
            "transition_convergence_deadline_s",
            "schedule_slack_s",
            "drain_margin_s",
            "startup_timeout_s",
            "hard_timeout_s",
        )
    }
    if document.get("timers") != expected_timers:
        raise N31LivenessShakedownError(
            "profile timers differ from exact v13 slot-066"
        )
    expected_transitions = [
        {
            "successor_epoch": item["successor_epoch"],
            "policy_intent": item["request"]["policy_intent"],
            "minimum_post_baseline_observation_ms": item["request"][
                "minimum_post_baseline_observation_ms"
            ],
            "minimum_predecessor_residency_ms": item["request"][
                "minimum_predecessor_residency_ms"
            ],
            "apply_shape_selection": item["request"]["apply_shape_selection"],
        }
        for item in runtime_document["transitions"]
    ]
    if document.get("transitions") != expected_transitions:
        raise N31LivenessShakedownError(
            "profile transitions differ from exact v13 slot-066"
        )
    expected_fault_window = {
        "start_after_prelaunch_anchor_s": slot_document["byzantine"][
            "start_after_prelaunch_anchor_s"
        ],
        "duration_s": slot_document["byzantine"]["duration_s"],
        "maximum_omissions_per_proposal": slot_document[
            "max_omissions_per_proposal"
        ],
    }
    if document.get("fault_window") != expected_fault_window:
        raise N31LivenessShakedownError(
            "profile fault window differs from exact v13 slot-066"
        )
    expected_windows = {
        "bucket_width_s": slot_document["workload"]["bucket_width_s"],
        "baseline_bucket_count": slot_document["workload"][
            "baseline_bucket_count"
        ],
        "fault_evidence_bucket_count": slot_document["workload"][
            "fault_evidence_bucket_count"
        ],
        "epoch1_stable_bucket_count": slot_document["workload"][
            "epoch1_stable_bucket_count"
        ],
        "epoch2_stable_bucket_count": slot_document["workload"][
            "epoch2_stable_bucket_count"
        ],
    }
    if document.get("phase_windows") != expected_windows:
        raise N31LivenessShakedownError(
            "profile phase windows differ from exact v13 slot-066"
        )


def load_frozen_profile(path: Path = DEFAULT_PROFILE_PATH) -> FrozenN31LivenessProfile:
    """Load only the byte-exact shipped diagnostic profile."""

    try:
        payload = Path(path).read_bytes()
    except OSError as error:
        raise N31LivenessShakedownError(f"cannot read profile: {error}") from error
    document = _json_object(payload, "profile")
    release = _release_for_profile_id(document.get("profile_id"))
    digest = _sha256_bytes(payload)
    if digest != release.profile_sha256:
        raise N31LivenessShakedownError(
            "profile bytes differ from the shipped liveness shakedown profile"
        )
    expected_fields = {
        "schema_version",
        "profile_id",
        "frozen",
        "diagnostic_only",
        "source",
        "consensus",
        "adaptation",
        "fault_cohorts",
        "workload",
        "responsiveness_policy",
        "timers",
        "transitions",
        "fault_window",
        "phase_windows",
        "ports",
        "attempt_policy",
        "evidence_scope",
        "observation",
    }
    if set(document) != expected_fields:
        raise N31LivenessShakedownError("profile schema drifted")
    if (
        document["schema_version"] != 1
        or document["profile_id"] != release.profile_id
        or document["frozen"] is not True
        or document["diagnostic_only"] is not True
        or document["attempt_policy"]
        != {
            "maximum_attempts": 2,
            "attempts_per_invocation": 2,
            "automatic_retries": 0,
            "replacement_policy": "none",
            "outcome_dependent_launch": False,
        }
        or document["evidence_scope"]
        != {
            "claim_eligible": False,
            "campaign_member": False,
            "denominator_contribution": 0,
            "figure_eligible": False,
        }
        or document["observation"]
        != {
            "anchor": "first_common_epoch2_commit",
            "duration_s": 45,
            "required_selected_root_count": 21,
            "require_every_selected_root_exercised": True,
        }
    ):
        raise N31LivenessShakedownError(
            "profile diagnostic or one-shot evidence policy drifted"
        )
    _validate_profile_against_source(document, _source_slot())
    return FrozenN31LivenessProfile(
        **document,
        profile_sha256=digest,
    )


def _approval_document(value: Mapping[str, object]) -> dict[str, object]:
    document = dict(value)
    expected_fields = {
        "schema_version",
        "scope",
        "authorized_by",
        "approval_reference",
        "approved_utc",
        "profile_id",
        "kauri_revision",
        "build_provenance_sha256",
        "maximum_attempts",
        "attempts_per_invocation",
        "automatic_retries",
        "replacement_policy",
        "outcome_dependent_launch",
        "campaign_member",
        "denominator_contribution",
        "figure_eligible",
    }
    if set(document) != expected_fields:
        raise N31LivenessShakedownError("approval receipt schema is invalid")
    reference = document.get("approval_reference")
    if (
        not isinstance(reference, str)
        or not reference.strip()
        or reference != reference.strip()
        or len(reference) > 512
        or any(ord(character) < 0x20 for character in reference)
    ):
        raise N31LivenessShakedownError("approval reference is required")
    try:
        approved = dt.datetime.fromisoformat(
            str(document["approved_utc"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise N31LivenessShakedownError(
            "approval timestamp is invalid"
        ) from error
    if (
        approved.tzinfo is None
        or approved.utcoffset() is None
        or approved.utcoffset() != dt.timedelta(0)
    ):
        raise N31LivenessShakedownError("approval timestamp must include UTC")
    return document


def _validate_approval_contract(
    profile: FrozenN31LivenessProfile,
    approval_receipt: Mapping[str, object],
    *,
    revision: object,
    build_provenance_sha256: object,
) -> dict[str, object]:
    """Replay the exact external authorization and non-claim policy."""

    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(character not in LOWER_HEX for character in revision)
        or not _valid_sha256(build_provenance_sha256)
    ):
        raise N31LivenessShakedownError(
            "approval receipt revision/build identity is invalid"
        )
    approval = _approval_document(approval_receipt)
    release = _release_for_profile(profile)
    required = {
        "schema_version": 1,
        "scope": release.approval_scope,
        "authorized_by": "thesis_author",
        "profile_id": profile.profile_id,
        "kauri_revision": revision,
        "build_provenance_sha256": build_provenance_sha256,
        "maximum_attempts": 2,
        "attempts_per_invocation": 2,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "outcome_dependent_launch": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }
    if any(approval.get(key) != value for key, value in required.items()):
        raise N31LivenessShakedownError(
            "approval receipt policy violates revision/build/profile, attempt, "
            "or figure scope"
        )
    return approval


def load_approval_receipt(path: Path) -> dict[str, object]:
    receipt_path = Path(path)
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise N31LivenessShakedownError(
            "approval receipt must be an external regular file"
        )
    try:
        return _approval_document(
            _json_object(receipt_path.read_bytes(), "approval receipt")
        )
    except OSError as error:
        raise N31LivenessShakedownError(
            f"cannot read approval receipt: {error}"
        ) from error


def _coerce_approval_receipt(
    value: Mapping[str, object] | Path,
) -> dict[str, object]:
    if isinstance(value, Mapping):
        return _approval_document(value)
    return load_approval_receipt(Path(value))


def build_approval_receipt(
    profile: FrozenN31LivenessProfile,
    *,
    approval_reference: str,
    approved_utc: str,
    kauri_revision: str,
    build_provenance_sha256: str,
) -> dict[str, object]:
    """Build the exact external thesis-author receipt for this two-run scope."""

    release = _release_for_profile(profile)
    return _approval_document(
        {
            "schema_version": 1,
            "scope": release.approval_scope,
            "authorized_by": "thesis_author",
            "approval_reference": approval_reference,
            "approved_utc": approved_utc,
            "profile_id": profile.profile_id,
            "kauri_revision": kauri_revision,
            "build_provenance_sha256": build_provenance_sha256,
            "maximum_attempts": 2,
            "attempts_per_invocation": 2,
            "automatic_retries": 0,
            "replacement_policy": "none",
            "outcome_dependent_launch": False,
            "campaign_member": False,
            "denominator_contribution": 0,
            "figure_eligible": False,
        }
    )


def preflight_from_documents(
    profile: FrozenN31LivenessProfile,
    *,
    current_revision: str,
    build_provenance: Mapping[str, object],
    approval_receipt: Mapping[str, object],
) -> dict[str, object]:
    """Bind the frozen diagnostic to one exact current revision and build."""

    if (
        not isinstance(current_revision, str)
        or len(current_revision) != 40
        or any(character not in LOWER_HEX for character in current_revision)
    ):
        raise N31LivenessShakedownError("current revision is invalid")
    build = dict(build_provenance)
    if build.get("revision") != current_revision:
        raise N31LivenessShakedownError(
            "build provenance does not bind the current revision"
        )
    build_digest = _sha256_bytes(_canonical_json_bytes(build))
    approval = _validate_approval_contract(
        profile,
        approval_receipt,
        revision=current_revision,
        build_provenance_sha256=build_digest,
    )
    return {
        "schema_version": 1,
        "status": "READY",
        "profile_id": profile.profile_id,
        "profile_sha256": profile.profile_sha256,
        "source_slot_id": SOURCE_SLOT_ID,
        "revision": current_revision,
        "build_provenance_sha256": build_digest,
        "approval_receipt_sha256": _sha256_bytes(
            _canonical_json_bytes(approval)
        ),
        "maximum_attempts": 2,
        "attempts_per_invocation": 2,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "outcome_dependent_launch": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }


def preflight(
    *,
    profile_path: Path = DEFAULT_PROFILE_PATH,
    repository: Path = REPOSITORY,
    build_directory: Path | None = None,
    build_provenance_path: Path | None = None,
    results_root: Path,
    approval_receipt_path: Path,
    minimum_free_bytes: int = 0,
) -> dict[str, object]:
    """Perform the read-only source/build/port/approval gate for one pair."""

    profile = load_frozen_profile(profile_path)
    release = _release_for_profile(profile)
    repository = Path(repository).resolve()
    build_directory = (
        repository / "build-adaptive"
        if build_directory is None
        else Path(build_directory).resolve()
    )
    build_provenance_path = (
        build_directory / "n31-exact-build-provenance.json"
        if build_provenance_path is None
        else Path(build_provenance_path).resolve()
    )
    results_root = Path(results_root).resolve()
    canonical_results_root = (
        repository / release.canonical_results_relative_path
    ).resolve()
    if results_root != canonical_results_root:
        raise N31LivenessShakedownError(
            "live shakedown uses one canonical result root for the two-attempt cap"
        )
    if results_root.exists() and any(results_root.iterdir()):
        raise N31LivenessShakedownError(
            "the indivisible two-attempt liveness pair is already consumed"
        )
    approval_path = Path(approval_receipt_path).resolve()
    try:
        approval_path.relative_to(results_root)
    except ValueError:
        pass
    else:
        raise N31LivenessShakedownError(
            "approval receipt must remain outside the diagnostic result root"
        )
    source = _source_slot(repository)
    try:
        live = verify_evidence_preflight(
            source.slot,
            repository=repository,
            build_directory=build_directory,
            build_provenance_path=build_provenance_path,
            result_root=results_root,
            minimum_free_bytes=minimum_free_bytes,
        )
    except FactorialExecutionError as error:
        raise N31LivenessShakedownError(str(error)) from error
    return preflight_from_documents(
        profile,
        current_revision=live.revision,
        build_provenance=live.build_provenance,
        approval_receipt=load_approval_receipt(approval_path),
    )


def prepare_approval_receipt(
    *,
    profile_path: Path = DEFAULT_PROFILE_PATH,
    repository: Path = REPOSITORY,
    build_directory: Path | None = None,
    build_provenance_path: Path | None = None,
    results_root: Path,
    approval_receipt_path: Path,
    approval_reference: str,
    approved_utc: str,
    minimum_free_bytes: int = 0,
) -> dict[str, object]:
    """Create the external approval receipt after the exact live preflight."""

    profile = load_frozen_profile(profile_path)
    release = _release_for_profile(profile)
    repository = Path(repository).resolve()
    build_directory = (
        repository / "build-adaptive"
        if build_directory is None
        else Path(build_directory).resolve()
    )
    build_provenance_path = (
        build_directory / "n31-exact-build-provenance.json"
        if build_provenance_path is None
        else Path(build_provenance_path).resolve()
    )
    results_root = Path(results_root).resolve()
    if results_root != (
        repository / release.canonical_results_relative_path
    ).resolve():
        raise N31LivenessShakedownError(
            "live shakedown uses one canonical result root for the two-attempt cap"
        )
    if results_root.exists() and any(results_root.iterdir()):
        raise N31LivenessShakedownError(
            "approval receipt must be created before the attempt pair"
        )
    approval_path = Path(approval_receipt_path).resolve()
    try:
        approval_path.relative_to(results_root)
    except ValueError:
        pass
    else:
        raise N31LivenessShakedownError(
            "approval receipt must remain outside the diagnostic result root"
        )
    if approval_path.exists() or approval_path.is_symlink():
        raise N31LivenessShakedownError(
            "approval receipt already exists; refusing replacement"
        )
    source = _source_slot(repository)
    try:
        live = verify_evidence_preflight(
            source.slot,
            repository=repository,
            build_directory=build_directory,
            build_provenance_path=build_provenance_path,
            result_root=results_root,
            minimum_free_bytes=minimum_free_bytes,
        )
    except FactorialExecutionError as error:
        raise N31LivenessShakedownError(str(error)) from error
    build_digest = _sha256_bytes(_canonical_json_bytes(live.build_provenance))
    approval = build_approval_receipt(
        profile,
        approval_reference=approval_reference,
        approved_utc=approved_utc,
        kauri_revision=live.revision,
        build_provenance_sha256=build_digest,
    )
    _write_exclusive(
        approval_path,
        _canonical_json_bytes(approval, newline=True),
    )
    result = preflight_from_documents(
        profile,
        current_revision=live.revision,
        build_provenance=live.build_provenance,
        approval_receipt=approval,
    )
    return {
        **result,
        "approval_receipt_path": str(approval_path),
    }


def _validate_preflight_receipt(
    profile: FrozenN31LivenessProfile,
    preflight_receipt: Mapping[str, object],
    approval_receipt: Mapping[str, object],
) -> dict[str, object]:
    receipt = dict(preflight_receipt)
    required = {
        "schema_version": 1,
        "status": "READY",
        "profile_id": profile.profile_id,
        "profile_sha256": profile.profile_sha256,
        "source_slot_id": SOURCE_SLOT_ID,
        "maximum_attempts": 2,
        "attempts_per_invocation": 2,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "outcome_dependent_launch": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }
    expected_fields = {
        *required,
        "revision",
        "build_provenance_sha256",
        "approval_receipt_sha256",
    }
    if (
        set(receipt) != expected_fields
        or any(receipt.get(key) != value for key, value in required.items())
    ):
        if set(receipt) != expected_fields:
            raise N31LivenessShakedownError(
                "preflight receipt schema is invalid"
            )
        raise N31LivenessShakedownError("preflight receipt policy is invalid")
    if not _valid_sha256(receipt.get("build_provenance_sha256")):
        raise N31LivenessShakedownError("preflight build provenance is invalid")
    approval = _validate_approval_contract(
        profile,
        approval_receipt,
        revision=receipt.get("revision"),
        build_provenance_sha256=receipt.get("build_provenance_sha256"),
    )
    if (
        receipt.get("revision") != approval.get("kauri_revision")
        or receipt.get("build_provenance_sha256")
        != approval.get("build_provenance_sha256")
        or receipt.get("approval_receipt_sha256")
        != _sha256_bytes(_canonical_json_bytes(approval))
    ):
        raise N31LivenessShakedownError(
            "preflight and approval receipts do not have the same identity"
        )
    return receipt


def _attempt_directories(root: Path) -> tuple[Path, ...]:
    directories: list[Path] = []
    for path in root.iterdir():
        if path.name in {
            "pair-start.json",
            "pair-ledger.json",
            "evidence-seal.json",
        }:
            if path.is_symlink() or not path.is_file():
                raise N31LivenessShakedownError(
                    f"attempt root contains an unsafe pair entry: {path.name}"
                )
            continue
        if path.is_symlink() or not path.is_dir() or ATTEMPT_NAME.fullmatch(path.name) is None:
            raise N31LivenessShakedownError(
                f"attempt root contains an unknown entry: {path.name}"
            )
        directories.append(path)
    ordered = tuple(sorted(directories, key=lambda path: path.name))
    ordinals = [
        int(ATTEMPT_NAME.fullmatch(path.name).group("ordinal"))  # type: ignore[union-attr]
        for path in ordered
    ]
    if ordinals != list(range(1, len(ordered) + 1)):
        raise N31LivenessShakedownError(
            "attempt root does not contain a contiguous one-shot history"
        )
    return ordered


def _attempt_identity_components(
    timestamp_utc: str,
    process_id: int,
    attempt_uuid: str,
) -> dt.datetime:
    if not isinstance(timestamp_utc, str) or not TIMESTAMP.fullmatch(
        timestamp_utc
    ):
        raise N31LivenessShakedownError(
            "attempt timestamp is not canonical UTC"
        )
    try:
        timestamp = dt.datetime.strptime(
            timestamp_utc, "%Y%m%dT%H%M%S.%fZ"
        )
    except ValueError as error:
        raise N31LivenessShakedownError(
            "attempt timestamp is not a real UTC instant"
        ) from error
    if type(process_id) is not int or process_id <= 1:
        raise N31LivenessShakedownError("attempt PID is invalid")
    try:
        parsed_uuid = uuid_module.UUID(attempt_uuid)
    except (AttributeError, ValueError) as error:
        raise N31LivenessShakedownError("attempt UUID is invalid") from error
    if str(parsed_uuid) != attempt_uuid or parsed_uuid.version != 4:
        raise N31LivenessShakedownError(
            "attempt UUID must be canonical version 4"
        )
    return timestamp


def _attempt_identity_document(
    ordinal: int,
    timestamp_utc: str,
    process_id: int,
    attempt_uuid: str,
) -> dict[str, object]:
    if ordinal not in (1, 2):
        raise N31LivenessShakedownError("attempt ordinal is invalid")
    _attempt_identity_components(timestamp_utc, process_id, attempt_uuid)
    return {
        "ordinal": ordinal,
        "timestamp_utc": timestamp_utc,
        "pid": process_id,
        "attempt_uuid": attempt_uuid,
        "attempt_directory": (
            f"attempt-{ordinal:02d}-{timestamp_utc}-pid{process_id}-"
            f"{attempt_uuid}"
        ),
    }


def _validated_intended_attempts(
    value: object,
) -> tuple[dict[str, object], dict[str, object]]:
    if not isinstance(value, list) or len(value) != 2:
        raise N31LivenessShakedownError(
            "pair start must predeclare exactly two attempts"
        )
    documents: list[dict[str, object]] = []
    timestamps: list[dt.datetime] = []
    for ordinal, row in enumerate(value, 1):
        if not isinstance(row, Mapping):
            raise N31LivenessShakedownError(
                "pair start attempt identity is malformed"
            )
        expected = _attempt_identity_document(
            ordinal,
            row.get("timestamp_utc"),  # type: ignore[arg-type]
            row.get("pid"),  # type: ignore[arg-type]
            row.get("attempt_uuid"),  # type: ignore[arg-type]
        )
        if dict(row) != expected:
            raise N31LivenessShakedownError(
                "pair start attempt identity schema drifted"
            )
        documents.append(expected)
        timestamps.append(
            _attempt_identity_components(
                str(expected["timestamp_utc"]),
                int(expected["pid"]),
                str(expected["attempt_uuid"]),
            )
        )
    if (
        timestamps[1] <= timestamps[0]
        or documents[0]["attempt_uuid"] == documents[1]["attempt_uuid"]
    ):
        raise N31LivenessShakedownError(
            "pair attempt identities are not unique and chronological"
        )
    return documents[0], documents[1]


def allocate_attempt_directory(
    results_root: Path,
    *,
    timestamp_utc: str,
    process_id: int,
    attempt_uuid: str,
) -> Path:
    """Atomically claim one of the two prespecified diagnostic attempts."""

    root = Path(results_root)
    _attempt_identity_components(timestamp_utc, process_id, attempt_uuid)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise N31LivenessShakedownError("attempt root is unsafe")
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.mkdir(mode=0o700, exist_ok=True)
    lock_path = root.parent / f".{root.name}.allocation.lock"
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise N31LivenessShakedownError(
            f"cannot acquire safe attempt-allocation lock: {error}"
        ) from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        attempts = _attempt_directories(root)
        suffix = f"-{timestamp_utc}-pid{process_id}-{attempt_uuid}"
        if any(path.name.endswith(suffix) for path in attempts):
            raise N31LivenessShakedownError(
                "attempt identity collision; refusing directory reuse"
            )
        if len(attempts) >= 2:
            raise N31LivenessShakedownError(
                "the profile permits exactly two attempts; refusing a third"
            )
        ordinal = len(attempts) + 1
        name = f"attempt-{ordinal:02d}{suffix}"
        destination = root / name
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError as error:
            raise N31LivenessShakedownError(
                "attempt identity collision; refusing directory reuse"
            ) from error
        return destination
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _integer(value: object, *, minimum: int = 0) -> int | None:
    return value if type(value) is int and value >= minimum else None


def _leader_timeout_ns(profile: FrozenN31LivenessProfile) -> int:
    return (
        int(profile.timers["global_worst_candidate_depth"])
        * int(profile.timers["leader_progress_timeout_ms_per_depth"])
        * 1_000_000
    )


def _qualifying_stall(
    profile: FrozenN31LivenessProfile,
    value: object,
    selected_roots: frozenset[int],
    activated_epoch_digest: str,
) -> bool:
    stall = _mapping(value)
    if stall is None:
        return False
    q = int(profile.consensus["quorum"])
    replica_id = _integer(stall.get("replica_id"), minimum=0)
    root_id = _integer(stall.get("root_id"), minimum=0)
    head_count = _integer(stall.get("head_verified_signer_count"), minimum=0)
    later_count = _integer(stall.get("later_verified_signer_count"), minimum=0)
    commit_gap_ns = _integer(stall.get("commit_gap_ns"), minimum=0)
    head_context = _integer(
        stall.get("head_context_generation"), minimum=1
    )
    later_context = _integer(
        stall.get("later_context_generation"), minimum=1
    )
    head_height = _integer(stall.get("head_block_height"), minimum=1)
    later_height = _integer(stall.get("later_block_height"), minimum=1)
    leader_timeout_ns = _leader_timeout_ns(profile)
    hashes = stall.get("source_event_sha256s")
    hard = frozenset(int(value) for value in profile.fault_cohorts["hard_actor_ids"])
    degraded = frozenset(
        int(value)
        for value in profile.fault_cohorts["responsive_degraded_actor_ids"]
    )
    return (
        replica_id is not None
        and root_id == replica_id
        and root_id in selected_roots
        and replica_id not in hard | degraded
        and stall.get("non_injected") is True
        and stall.get("lag_observed") is True
        and stall.get("survived_sigint") is True
        and stall.get("survived_sigterm") is True
        and stall.get("pre_kill_stack_valid") is True
        and _valid_sha256(stall.get("stack_sample_sha256"))
        and stall.get("peer_timeout_view_progress") is True
        and stall.get("activated_epoch_digest") == activated_epoch_digest
        and _valid_sha256(stall.get("queue_evidence_sha256"))
        and stall.get("queue_head_position") == 0
        and stall.get("later_queue_position") == 1
        and head_context is not None
        and later_context is not None
        and later_context > head_context
        and head_height is not None
        and later_height == head_height + 1
        and isinstance(stall.get("head_block_hash"), str)
        and _valid_sha256(stall.get("head_block_hash"))
        and isinstance(stall.get("later_block_hash"), str)
        and _valid_sha256(stall.get("later_block_hash"))
        and stall.get("head_block_hash") != stall.get("later_block_hash")
        and stall.get("later_parent_hash") == stall.get("head_block_hash")
        and head_count is not None
        and head_count < q
        and later_count is not None
        and later_count >= q
        and stall.get("quorum") == q
        and stall.get("later_qc_ready") is True
        and stall.get("later_qc_published") is False
        and stall.get("blocked_by_queue_head") is True
        and stall.get("leader_progress_timeout_observed") is True
        and commit_gap_ns is not None
        and commit_gap_ns >= leader_timeout_ns
        and isinstance(hashes, list)
        and len(hashes) >= 3
        and len(set(hashes)) == len(hashes)
        and all(_valid_sha256(digest) for digest in hashes)
        and stall.get("queue_evidence_sha256") in hashes
    )


def _validate_observation_schema(
    profile: FrozenN31LivenessProfile,
    observation: Mapping[str, object],
) -> None:
    expected_fields = {
        "schema_version",
        "profile_id",
        "source_slot_id",
        "pair_id",
        "revision",
        "build_provenance_sha256",
        "approval_receipt_sha256",
        "attempt",
        "integrity",
        "execution",
        "epoch2",
        "liveness",
        "configuration_progress",
    }
    integrity = _mapping(observation.get("integrity"))
    if (
        set(observation) != expected_fields
        or observation.get("schema_version") != 1
        or observation.get("profile_id") != profile.profile_id
        or observation.get("source_slot_id") != SOURCE_SLOT_ID
        or integrity is None
        or set(integrity)
        != {
            "profile_exact",
            "provenance_exact",
            "approval_exact",
            "raw_evidence_complete",
            "seal_valid",
        }
        or integrity.get("profile_exact") is not True
        or integrity.get("provenance_exact") is not True
        or integrity.get("approval_exact") is not True
        or type(integrity.get("raw_evidence_complete")) is not bool
        or integrity.get("seal_valid") is not True
    ):
        raise N31LivenessShakedownError(
            "sealed observation schema or integrity identity is invalid"
        )


def evaluate_verdict(
    profile: FrozenN31LivenessProfile,
    observation: Mapping[str, object],
) -> str:
    """Evaluate only the three frozen diagnostic outcomes, fail closed."""

    try:
        attempt = _mapping(observation.get("attempt"))
        integrity = _mapping(observation.get("integrity"))
        execution = _mapping(observation.get("execution"))
        epoch2 = _mapping(observation.get("epoch2"))
        liveness = _mapping(observation.get("liveness"))
        if None in (attempt, integrity, execution, epoch2, liveness):
            return "INCOMPLETE"
        assert attempt is not None
        assert integrity is not None
        assert execution is not None
        assert epoch2 is not None
        assert liveness is not None
        if (
            observation.get("schema_version") != 1
            or observation.get("profile_id") != profile.profile_id
            or observation.get("source_slot_id") != SOURCE_SLOT_ID
            or not isinstance(observation.get("pair_id"), str)
            or _canonical_pair_uuid(str(observation.get("pair_id")))
            != observation.get("pair_id")
            or not isinstance(observation.get("revision"), str)
            or len(str(observation.get("revision"))) != 40
            or not _valid_sha256(observation.get("build_provenance_sha256"))
            or not _valid_sha256(observation.get("approval_receipt_sha256"))
            or attempt.get("launch_count") != 1
            or attempt.get("pair_id") != observation.get("pair_id")
            or attempt.get("ordinal") not in (1, 2)
            or attempt.get("retry_count") != 0
            or attempt.get("replacement_count") != 0
            or attempt.get("revision") != observation.get("revision")
            or attempt.get("build_provenance_sha256")
            != observation.get("build_provenance_sha256")
            or attempt.get("approval_receipt_sha256")
            != observation.get("approval_receipt_sha256")
            or any(
                integrity.get(key) is not True
                for key in (
                    "profile_exact",
                    "provenance_exact",
                    "approval_exact",
                    "raw_evidence_complete",
                    "seal_valid",
                )
            )
            or execution.get("cleanup_complete") is not True
            or execution.get("unexpected_process_exit_count") != 0
            or execution.get("consensus_conflict_count") != 0
        ):
            return "INCOMPLETE"
        first = _integer(epoch2.get("first_common_commit_ns"), minimum=1)
        deadline = _integer(epoch2.get("observation_deadline_ns"), minimum=1)
        end = _integer(epoch2.get("observation_end_ns"), minimum=1)
        final = _integer(epoch2.get("final_common_commit_ns"), minimum=1)
        duration_ns = int(profile.observation["duration_s"]) * NANOSECONDS_PER_SECOND
        selected = epoch2.get("selected_root_ids")
        exercised = epoch2.get("exercised_root_ids")
        activated_epoch_digest = epoch2.get("activated_epoch_digest")
        required = int(profile.observation["required_selected_root_count"])
        if (
            first is None
            or deadline != first + duration_ns
            or end is None
            or end < deadline
            or final is None
            or not isinstance(selected, list)
            or not isinstance(exercised, list)
            or len(selected) != required
            or not _valid_sha256(activated_epoch_digest)
            or len(set(selected)) != required
            or any(type(root) is not int or root < 0 for root in selected)
            or len(exercised) != required
            or len(set(exercised)) != required
            or frozenset(exercised) != frozenset(selected)
        ):
            return "INCOMPLETE"
        selected_roots = frozenset(int(root) for root in selected)
        stalls = liveness.get("head_of_line_stalls")
        gap = _integer(liveness.get("max_authoritative_commit_gap_ns"), minimum=0)
        common_gap = _integer(
            liveness.get("max_common_q21_commit_gap_ns"), minimum=0
        )
        if not isinstance(stalls, list) or gap is None or common_gap is None:
            return "INCOMPLETE"
        if stalls:
            if (
                _integer(liveness.get("leader_progress_timeout_count"), minimum=1)
                is None
                or not all(
                    _qualifying_stall(
                        profile,
                        stall,
                        selected_roots,
                        str(activated_epoch_digest),
                    )
                    for stall in stalls
                )
            ):
                return "INCOMPLETE"
            return "REPRODUCED"
        leader_timeout_ns = _leader_timeout_ns(profile)
        if (
            liveness.get("leader_progress_timeout_count") == 0
            and liveness.get("q21_progress_continued") is True
            and execution.get("all_processes_exited_after_sigint") is True
            and final >= deadline - leader_timeout_ns
            and gap < leader_timeout_ns
            and common_gap < leader_timeout_ns
        ):
            return "NOT_REPRODUCED"
        return "INCOMPLETE"
    except (KeyError, TypeError, ValueError, N31LivenessShakedownError):
        return "INCOMPLETE"


def create_evidence_seal(attempt_directory: Path) -> str:
    try:
        return _create_archive_seal(Path(attempt_directory)).seal_sha256
    except (EvidenceSealError, OSError) as error:
        raise N31LivenessShakedownError(f"cannot create evidence seal: {error}") from error


def verify_evidence_seal(attempt_directory: Path) -> str:
    try:
        return _verify_archive_seal(Path(attempt_directory)).seal_sha256
    except (EvidenceSealError, OSError) as error:
        raise N31LivenessShakedownError(f"evidence seal verification failed: {error}") from error


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _attempt_ordinal(attempt_directory: Path) -> int:
    match = ATTEMPT_NAME.fullmatch(attempt_directory.name)
    if match is None:
        raise N31LivenessShakedownError("allocated attempt identity is malformed")
    return int(match.group("ordinal"))


def _run_attempt_once(
    *,
    profile: FrozenN31LivenessProfile,
    preflight_receipt: Mapping[str, object],
    approval_receipt: Mapping[str, object] | Path,
    results_root: Path,
    pair_id: str,
    execute_attempt: Callable[[Path, FrozenN31LivenessProfile], object] | None = None,
    timestamp_utc: str | None = None,
    process_id: int | None = None,
    attempt_uuid: str | None = None,
    repository: Path = REPOSITORY,
    build_directory: Path | None = None,
    build_provenance_path: Path | None = None,
    minimum_free_bytes: int = 0,
) -> tuple[Path, str]:
    """Execute one member of an already-authorized pair and seal its outcome."""

    pair_id = _canonical_pair_uuid(pair_id)
    approval = _coerce_approval_receipt(approval_receipt)
    receipt = _validate_preflight_receipt(
        profile,
        preflight_receipt,
        approval,
    )
    now = dt.datetime.now(dt.timezone.utc)
    timestamp_utc = timestamp_utc or now.strftime("%Y%m%dT%H%M%S.%fZ")
    process_id = os.getpid() if process_id is None else process_id
    attempt_uuid = attempt_uuid or str(uuid_module.uuid4())
    attempt_directory = allocate_attempt_directory(
        results_root,
        timestamp_utc=timestamp_utc,
        process_id=process_id,
        attempt_uuid=attempt_uuid,
    )
    ordinal = _attempt_ordinal(attempt_directory)
    _write_exclusive(
        attempt_directory / "profile.json",
        _shipped_profile_bytes(profile),
    )
    _write_exclusive(
        attempt_directory / "approval-receipt.json",
        _canonical_json_bytes(approval, newline=True),
    )
    _write_exclusive(
        attempt_directory / "preflight-receipt.json",
        _canonical_json_bytes(receipt, newline=True),
    )
    _write_exclusive(
        attempt_directory / "attempt.json",
        _canonical_json_bytes(
            {
                "schema_version": 1,
                "profile_id": profile.profile_id,
                "source_slot_id": SOURCE_SLOT_ID,
                "pair_id": pair_id,
                "attempt_ordinal": ordinal,
                "attempt_directory": attempt_directory.name,
                "launch_count": 1,
                "retry_count": 0,
                "replacement_count": 0,
                "automatic_retries": 0,
                "replacement_policy": "none",
                "claim_eligible": False,
                "campaign_member": False,
                "denominator_contribution": 0,
                "figure_eligible": False,
            },
            newline=True,
        ),
    )
    verdict = "INCOMPLETE"
    reason: str | None = None
    try:
        if execute_attempt is None:
            build_directory = build_directory or (Path(repository) / "build-adaptive")
            build_provenance_path = build_provenance_path or (
                build_directory / "n31-exact-build-provenance.json"
            )
            result = _execute_live_attempt(
                attempt_directory,
                profile,
                receipt=receipt,
                approval_receipt=approval,
                repository=Path(repository),
                build_directory=Path(build_directory),
                build_provenance_path=Path(build_provenance_path),
                results_root=Path(results_root),
                pair_id=pair_id,
                minimum_free_bytes=minimum_free_bytes,
            )
        else:
            result = execute_attempt(attempt_directory, profile)
        observation_path = attempt_directory / "observation.json"
        if isinstance(result, Mapping):
            if observation_path.exists():
                raise N31LivenessShakedownError(
                    "attempt executor returned and persisted duplicate observations"
                )
            _write_exclusive(
                observation_path,
                _canonical_json_bytes(dict(result), newline=True),
            )
        if not observation_path.is_file() or observation_path.is_symlink():
            raise N31LivenessShakedownError(
                "attempt executor did not preserve observation.json"
            )
        observation = _json_object(observation_path.read_bytes(), "observation")
        _validate_observation_schema(profile, observation)
        verdict = evaluate_verdict(profile, observation)
        if verdict == "INCOMPLETE":
            reason = "preserved observation did not satisfy a complete verdict"
        else:
            execution_root = _live_execution_root(attempt_directory)
        if verdict != "INCOMPLETE" and not all(
            path.is_file() and not path.is_symlink()
            for path in (
                execution_root / "runtime/exact-build-provenance.json",
                execution_root / "runtime/execution-provenance.json",
                execution_root / "source/manifest.json",
                execution_root / "source/plan.json",
                execution_root / "source/runtime.json",
                execution_root / "cleanup-ledger.json",
            )
        ):
            verdict = "INCOMPLETE"
            reason = "complete verdict lacks the required live evidence archive"
        elif verdict != "INCOMPLETE":
            _validate_complete_live_capture(
                attempt_directory,
                profile,
                observation,
            )
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            reason = f"{type(error).__name__}: interrupted one-shot attempt"
        else:
            reason = f"{type(error).__name__}: {error}"
        verdict = "INCOMPLETE"
    terminal = {
        "schema_version": 1,
        "profile_id": profile.profile_id,
        "source_slot_id": SOURCE_SLOT_ID,
        "pair_id": pair_id,
        "attempt_ordinal": ordinal,
        "verdict": verdict,
        "reason": reason,
        "launch_count": 1,
        "retry_count": 0,
        "replacement_count": 0,
        "claim_eligible": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }
    _write_exclusive(
        attempt_directory / "terminal.json",
        _canonical_json_bytes(terminal, newline=True),
    )
    create_evidence_seal(attempt_directory)
    return attempt_directory, verdict


def _canonical_pair_uuid(value: str) -> str:
    try:
        parsed = uuid_module.UUID(value)
    except (AttributeError, ValueError) as error:
        raise N31LivenessShakedownError("pair UUID is invalid") from error
    if str(parsed) != value or parsed.version != 4:
        raise N31LivenessShakedownError(
            "pair UUID must be canonical version 4"
        )
    return value


def run_pair(
    *,
    profile: FrozenN31LivenessProfile,
    preflight_receipt: Mapping[str, object],
    approval_receipt: Mapping[str, object] | Path,
    results_root: Path,
    execute_attempt: Callable[[Path, FrozenN31LivenessProfile], object] | None = None,
    pair_uuid: str | None = None,
    attempt_identities: Sequence[tuple[str, int, str]] | None = None,
    repository: Path = REPOSITORY,
    build_directory: Path | None = None,
    build_provenance_path: Path | None = None,
    minimum_free_bytes: int = 0,
) -> tuple[tuple[Path, str], tuple[Path, str]]:
    """Execute both prespecified attempts without observing the first outcome."""

    approval = _coerce_approval_receipt(approval_receipt)
    receipt = _validate_preflight_receipt(profile, preflight_receipt, approval)
    pair_id = _canonical_pair_uuid(pair_uuid or str(uuid_module.uuid4()))
    if attempt_identities is None:
        process_id = os.getpid()
        first = dt.datetime.now(dt.timezone.utc)
        second = dt.datetime.now(dt.timezone.utc)
        if second <= first:
            second = first + dt.timedelta(microseconds=1)
        identities: list[tuple[str, int, str]] = [
            (
                timestamp.strftime("%Y%m%dT%H%M%S.%fZ"),
                process_id,
                str(uuid_module.uuid4()),
            )
            for timestamp in (first, second)
        ]
    else:
        identities = list(attempt_identities)
    if len(identities) != 2 or any(
        not isinstance(identity, tuple) or len(identity) != 3
        for identity in identities
    ):
        raise N31LivenessShakedownError(
            "one pair requires exactly two prespecified attempt identities"
        )
    intended_attempts = _validated_intended_attempts(
        [
            _attempt_identity_document(ordinal, *identity)
            for ordinal, identity in enumerate(identities, 1)
        ]
    )
    root = Path(results_root)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise N31LivenessShakedownError("attempt root is unsafe")
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.mkdir(mode=0o700, exist_ok=True)
    if any(root.iterdir()):
        raise N31LivenessShakedownError(
            "the indivisible two-attempt pair requires an empty result root"
        )
    pair_start_path = root / "pair-start.json"
    pair_start_payload = _canonical_json_bytes(
        {
            "schema_version": 1,
            "profile_id": profile.profile_id,
            "profile_sha256": profile.profile_sha256,
            "source_slot_id": SOURCE_SLOT_ID,
            "pair_id": pair_id,
            "expected_attempt_count": 2,
            "outcome_dependent_launch": False,
            "intended_attempts": list(intended_attempts),
            "revision": receipt["revision"],
            "build_provenance_sha256": receipt[
                "build_provenance_sha256"
            ],
            "approval_receipt_sha256": receipt[
                "approval_receipt_sha256"
            ],
            "claim_eligible": False,
            "campaign_member": False,
            "denominator_contribution": 0,
            "figure_eligible": False,
        },
        newline=True,
    )
    _write_exclusive(pair_start_path, pair_start_payload)

    def assert_pair_start_unchanged() -> None:
        try:
            current = pair_start_path.read_bytes()
        except OSError as error:
            raise N31LivenessShakedownError(
                f"pair start changed after precommit: {error}"
            ) from error
        if pair_start_path.is_symlink() or current != pair_start_payload:
            raise N31LivenessShakedownError(
                "pair start changed after precommit"
            )

    results: list[tuple[Path, str]] = []
    for intended in intended_attempts:
        assert_pair_start_unchanged()
        result = _run_attempt_once(
            profile=profile,
            preflight_receipt=receipt,
            approval_receipt=approval,
            results_root=root,
            pair_id=pair_id,
            execute_attempt=execute_attempt,
            timestamp_utc=str(intended["timestamp_utc"]),
            process_id=int(intended["pid"]),
            attempt_uuid=str(intended["attempt_uuid"]),
            repository=repository,
            build_directory=build_directory,
            build_provenance_path=build_provenance_path,
            minimum_free_bytes=minimum_free_bytes,
        )
        if result[0].name != intended["attempt_directory"]:
            raise N31LivenessShakedownError(
                "executed attempt differs from pair-start precommit"
            )
        results.append(result)
        assert_pair_start_unchanged()
    if len(results) != 2:
        raise N31LivenessShakedownError(
            "pair execution did not preserve both attempts"
        )
    rows = []
    for ordinal, (attempt, verdict) in enumerate(results, 1):
        if _attempt_ordinal(attempt) != ordinal:
            raise N31LivenessShakedownError("pair attempt order drifted")
        rows.append(
            {
                "ordinal": ordinal,
                "attempt_directory": attempt.name,
                "verdict": verdict,
                "evidence_seal_sha256": verify_evidence_seal(attempt),
            }
        )
    _write_exclusive(
        root / "pair-ledger.json",
        _canonical_json_bytes(
            {
                "schema_version": 1,
                "profile_id": profile.profile_id,
                "profile_sha256": profile.profile_sha256,
                "source_slot_id": SOURCE_SLOT_ID,
                "pair_id": pair_id,
                "attempt_count": 2,
                "outcome_dependent_launch": False,
                "revision": receipt["revision"],
                "build_provenance_sha256": receipt[
                    "build_provenance_sha256"
                ],
                "approval_receipt_sha256": receipt[
                    "approval_receipt_sha256"
                ],
                "pair_start_sha256": _sha256_bytes(pair_start_payload),
                "attempts": rows,
                "claim_eligible": False,
                "campaign_member": False,
                "denominator_contribution": 0,
                "figure_eligible": False,
            },
            newline=True,
        ),
    )
    create_evidence_seal(root)
    return results[0], results[1]


def _event_payload(event: Any) -> Mapping[str, object]:
    payload = event.value.get("payload")
    if not isinstance(payload, Mapping):
        raise N31LivenessShakedownError(
            f"structured event has no payload: {event.relative_path}:{event.line_number}"
        )
    return payload


def _commit_key(event: Any) -> tuple[int, str, str | None, int]:
    payload = _event_payload(event)
    height = payload.get("block_height")
    block_hash = payload.get("block_hash")
    parent_hash = payload.get("parent_hash")
    transaction_count = payload.get("transaction_count")
    if (
        type(height) is not int
        or height <= 0
        or not _valid_sha256(block_hash)
        or (parent_hash is not None and not _valid_sha256(parent_hash))
        or type(transaction_count) is not int
        or transaction_count < 0
    ):
        raise N31LivenessShakedownError("structured commit identity is malformed")
    return height, str(block_hash), (
        str(parent_hash) if parent_hash is not None else None
    ), transaction_count


def _decision_proof(event: Any) -> tuple[int, int, str, str]:
    payload = _event_payload(event)
    proof = payload.get("decision_proof")
    if not isinstance(proof, Mapping):
        raise N31LivenessShakedownError(
            "authoritative commit lacks a decision proof"
        )
    epoch = proof.get("epoch_number")
    tree = proof.get("tree_id")
    digest = proof.get("epoch_digest")
    block_hash = proof.get("block_hash")
    if (
        type(epoch) is not int
        or epoch < 0
        or type(tree) is not int
        or tree < 0
        or not _valid_sha256(digest)
        or block_hash != payload.get("block_hash")
    ):
        raise N31LivenessShakedownError(
            "authoritative commit decision proof is malformed"
        )
    return epoch, tree, str(digest), str(block_hash)


def _event_reference(event: Any) -> dict[str, object]:
    return {
        "source": event.source,
        "relative_path": event.relative_path,
        "line_number": event.line_number,
        "source_monotonic_ns": event.timestamp_ns,
        "line_sha256": event.line_sha256,
    }


def _validate_authoritative_commit_chain(
    by_height: Mapping[int, tuple[int, str, str | None, int]],
) -> None:
    hashes: set[str] = set()
    ordered = sorted(by_height.items())
    for _height, key in ordered:
        if key[1] in hashes:
            raise N31LivenessShakedownError(
                "authoritative observer duplicated a commit hash"
            )
        hashes.add(key[1])
    for (previous_height, previous), (current_height, current) in zip(
        ordered, ordered[1:]
    ):
        if (
            current_height == previous_height + 1
            and current[2] != previous[1]
        ):
            raise N31LivenessShakedownError(
                "authoritative commit chain has a conflicting parent"
            )


def _common_epoch2_commits(
    spec: SlotRuntimeSpec,
    streams: Mapping[str, Sequence[Any]],
    *,
    earliest_ns: int = 0,
    latest_ns: int | None = None,
    expected_epoch_digest: str | None = None,
) -> list[dict[str, object]]:
    """Return authoritative Epoch2 commits observed by any exact Q21 peers."""

    observations: dict[
        int, dict[int, tuple[tuple[int, str, str | None, int], Any]]
    ] = {}
    shared_observations: dict[int, tuple[int, str, str | None, int]] = {}
    for replica_id in range(spec.replica_count):
        by_height: dict[
            int, tuple[tuple[int, str, str | None, int], Any]
        ] = {}
        for event in streams[f"replica-{replica_id}"]:
            if event.value.get("event_type") != "block.commit_observed":
                continue
            key = _commit_key(event)
            previous = by_height.get(key[0])
            if previous is not None:
                detail = "duplicate" if previous[0] == key else "conflicting"
                raise N31LivenessShakedownError(
                    f"replica-{replica_id} {detail} commit observation at height "
                    f"{key[0]}"
                )
            shared = shared_observations.setdefault(key[0], key)
            if shared != key:
                raise N31LivenessShakedownError(
                    f"replicas conflict on commit observation at height {key[0]}"
                )
            by_height[key[0]] = (key, event)
        _validate_authoritative_commit_chain(
            {height: observed[0] for height, observed in by_height.items()}
        )
        observations[replica_id] = by_height

    commits: list[dict[str, object]] = []
    seen_authoritative: set[tuple[int, str, str | None, int]] = set()
    authoritative_by_height: dict[
        int, tuple[int, str, str | None, int]
    ] = {}
    observer_events = streams[spec.structured_events.commit_observer_id]
    for event in observer_events:
        if event.value.get("event_type") != "block.committed":
            continue
        epoch, tree_id, epoch_digest, _block_hash = _decision_proof(event)
        if epoch != 2:
            continue
        if (
            expected_epoch_digest is not None
            and epoch_digest != expected_epoch_digest
        ):
            raise N31LivenessShakedownError(
                "Epoch2 commit digest differs from unanimous activation"
            )
        key = _commit_key(event)
        previous = authoritative_by_height.setdefault(key[0], key)
        if previous != key:
            raise N31LivenessShakedownError(
                "authoritative observer committed conflicting hashes at one height"
            )
        if key in seen_authoritative:
            raise N31LivenessShakedownError(
                "authoritative observer duplicated an Epoch2 commit"
            )
        seen_authoritative.add(key)
        matches: list[tuple[int, int, Any]] = []
        for replica_id, by_height in observations.items():
            observed = by_height.get(key[0])
            if observed is None:
                continue
            observed_key, candidate = observed
            if observed_key != key:
                raise N31LivenessShakedownError(
                    f"replica-{replica_id} conflicts with the authoritative "
                    f"commit at height {key[0]}"
                )
            matches.append((candidate.timestamp_ns, replica_id, candidate))
        matches.sort(key=lambda row: (row[0], row[1]))
        if len(matches) < spec.q:
            continue
        witnesses = matches[: spec.q]
        common_ns = max(event.timestamp_ns, witnesses[-1][0])
        if common_ns < earliest_ns or (
            latest_ns is not None and common_ns > latest_ns
        ):
            continue
        commits.append(
            {
                "block_height": key[0],
                "block_hash": key[1],
                "parent_hash": key[2],
                "transaction_count": key[3],
                "epoch_number": epoch,
                "tree_id": tree_id,
                "epoch_digest": epoch_digest,
                "authoritative_monotonic_ns": event.timestamp_ns,
                "common_monotonic_ns": common_ns,
                "view_generation": _event_payload(event).get("view_generation"),
                "observer": _event_reference(event),
                "witness_replica_ids": [replica_id for _, replica_id, _ in witnesses],
                "witnesses": [
                    _event_reference(candidate)
                    for _, _, candidate in witnesses
                ],
            }
        )
    _validate_authoritative_commit_chain(authoritative_by_height)
    return sorted(
        commits,
        key=lambda row: (
            int(row["common_monotonic_ns"]),
            int(row["block_height"]),
            str(row["block_hash"]),
        ),
    )


def _authoritative_epoch2_commits(
    spec: SlotRuntimeSpec,
    streams: Mapping[str, Sequence[Any]],
    *,
    start_ns: int,
    end_ns: int,
    expected_epoch_digest: str,
    selected_roots: Sequence[int],
) -> list[dict[str, object]]:
    """Rebuild authoritative progress with exact digest/tree safety checks."""

    root_by_tree = dict(enumerate(selected_roots))
    by_height: dict[int, tuple[int, str, str | None, int]] = {}
    seen: set[tuple[int, str, str | None, int]] = set()
    rows: list[dict[str, object]] = []
    for event in streams[spec.structured_events.commit_observer_id]:
        if event.value.get("event_type") != "block.committed":
            continue
        epoch, tree_id, digest, block_hash = _decision_proof(event)
        if epoch != 2:
            continue
        if digest != expected_epoch_digest:
            raise N31LivenessShakedownError(
                "authoritative Epoch2 commit differs from activated digest"
            )
        key = _commit_key(event)
        previous = by_height.setdefault(key[0], key)
        if previous != key:
            raise N31LivenessShakedownError(
                "authoritative observer committed conflicting hashes at one height"
            )
        if key in seen:
            raise N31LivenessShakedownError(
                "authoritative observer duplicated an Epoch2 commit"
            )
        seen.add(key)
        root_id = root_by_tree.get(tree_id)
        if root_id is None:
            raise N31LivenessShakedownError(
                "authoritative Epoch2 commit uses an unknown signed-bundle tree"
            )
        if not start_ns <= event.timestamp_ns <= end_ns:
            continue
        rows.append(
            {
                "block_height": key[0],
                "block_hash": block_hash,
                "parent_hash": key[2],
                "transaction_count": key[3],
                "tree_id": tree_id,
                "root_id": root_id,
                "epoch_digest": digest,
                "source_monotonic_ns": event.timestamp_ns,
                "view_generation": _event_payload(event).get(
                    "view_generation"
                ),
                "line_sha256": event.line_sha256,
            }
        )
    _validate_authoritative_commit_chain(by_height)
    return rows


def _issuer_public_key(attempt_directory: Path) -> str:
    path = attempt_directory / "runtime/issuer-identities.txt"
    try:
        payload = path.read_bytes()
        text = payload.decode("ascii")
    except (OSError, UnicodeDecodeError) as error:
        raise N31LivenessShakedownError(
            f"cannot read preserved epoch issuer identity: {error}"
        ) from error
    if not text.endswith("\n") or "\r" in text or len(text.splitlines()) != 1:
        raise N31LivenessShakedownError(
            "preserved epoch issuer identity is not one canonical line"
        )
    fields: dict[str, str] = {}
    for token in text.rstrip("\n").split(" "):
        if not token or ":" not in token:
            raise N31LivenessShakedownError(
                "preserved epoch issuer identity is malformed"
            )
        key, value = token.split(":", 1)
        if key in fields or not value or any(
            character not in LOWER_HEX for character in value
        ):
            raise N31LivenessShakedownError(
                "preserved epoch issuer identity is malformed"
            )
        fields[key] = value
    if set(fields) != {"pub", "sec"} or len(fields["pub"]) != 66:
        raise N31LivenessShakedownError(
            "preserved epoch issuer identity schema drifted"
        )
    return fields["pub"]


def _epoch2_bundle_identity(
    spec: SlotRuntimeSpec,
    attempt_directory: Path,
    profile: FrozenN31LivenessProfile,
) -> tuple[tuple[int, ...], str, int]:
    if len(spec.transitions) != 2:
        raise N31LivenessShakedownError(
            "runtime does not preserve exactly two successor bundles"
        )
    bundle_path = attempt_directory / spec.transitions[1].bundle_relative_path
    try:
        bundle = decode_epoch_change_bundle(
            bundle_path.read_bytes(),
            issuer_public_key=_issuer_public_key(attempt_directory),
        )
    except (FactorialValidationError, OSError) as error:
        raise N31LivenessShakedownError(
            f"cannot decode exact signed Epoch2 successor bundle: {error}"
        ) from error
    required = int(profile.observation["required_selected_root_count"])
    if (
        bundle.epoch_number != 2
        or not _valid_sha256(bundle.epoch_digest)
        or len(bundle.trees) != required
        or tuple(tree.tree_id for tree in bundle.trees) != tuple(range(required))
    ):
        raise N31LivenessShakedownError(
            "signed Epoch2 bundle identity/tree cardinality drifted"
        )
    expected_members = frozenset(range(spec.replica_count))
    roots: list[int] = []
    for tree in bundle.trees:
        if (
            tree.fanout != int(profile.consensus["initial_fanout"])
            or tree.pipeline_stretch
            != int(profile.consensus["pipeline_stretch"])
            or len(tree.members) != spec.replica_count
            or frozenset(tree.members) != expected_members
        ):
            raise N31LivenessShakedownError(
                "signed Epoch2 bundle contains a non-frozen tree"
            )
        roots.append(int(tree.members[0]))
    if len(set(roots)) != required:
        raise N31LivenessShakedownError(
            "signed Epoch2 bundle does not contain distinct roots"
        )
    hard = {
        int(replica_id) for replica_id in profile.fault_cohorts["hard_actor_ids"]
    }
    degraded = {
        int(replica_id)
        for replica_id in profile.fault_cohorts["responsive_degraded_actor_ids"]
    }
    frozen_fast = frozenset(range(spec.replica_count)) - hard - degraded
    if frozenset(roots) != frozen_fast or len(frozen_fast) != spec.q:
        raise N31LivenessShakedownError(
            "Epoch2 selected roots differ from the frozen fast Q21 cohort"
        )
    return tuple(roots), str(bundle.epoch_digest), int(bundle.trees[0].tree_id)


def _epoch2_activation_progress(
    spec: SlotRuntimeSpec,
    streams: Mapping[str, Sequence[Any]],
    *,
    expected_initial_tree_id: int,
) -> dict[str, object]:
    identities: set[tuple[int, int, str, int]] = set()
    references: list[dict[str, object]] = []
    for replica_id in range(spec.replica_count):
        matches = []
        for event in streams[f"replica-{replica_id}"]:
            if event.value.get("event_type") != "epoch.activated":
                continue
            payload = _event_payload(event)
            if payload.get("epoch_number") == 2:
                matches.append(event)
        if len(matches) != 1:
            raise N31LivenessShakedownError(
                f"replica-{replica_id} lacks one exact Epoch2 activation"
            )
        payload = _event_payload(matches[0])
        identity = (
            int(payload["epoch_number"]),
            int(payload["tree_id"]),
            str(payload["epoch_digest"]),
            int(payload["activation_height"]),
        )
        if not _valid_sha256(identity[2]) or identity[3] <= 0:
            raise N31LivenessShakedownError("Epoch2 activation identity is malformed")
        identities.add(identity)
        references.append(_event_reference(matches[0]))
    if len(identities) != 1:
        raise N31LivenessShakedownError(
            "replicas disagree on the exact Epoch2 activation"
        )
    epoch, tree_id, digest, height = next(iter(identities))
    if tree_id != expected_initial_tree_id:
        raise N31LivenessShakedownError(
            "Epoch2 activation tree differs from the signed successor bundle"
        )
    return {
        "epoch_number": epoch,
        "initial_tree_id": tree_id,
        "epoch_digest": digest,
        "activation_height": height,
        "replica_witness_count": len(references),
        "witnesses": references,
    }


def _complete_log_offset(path: Path) -> int:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return 0
    if not payload or payload.endswith(b"\n"):
        return len(payload)
    newline = payload.rfind(b"\n")
    return newline + 1 if newline >= 0 else 0


def _leader_timeout_offsets(
    spec: SlotRuntimeSpec,
    attempt_directory: Path,
) -> dict[str, int]:
    result: dict[str, int] = {}
    for replica_id in range(spec.replica_count):
        for relative in (
            spec.process_logs.replica_stdout_relative_paths[replica_id],
            spec.process_logs.replica_stderr_relative_paths[replica_id],
        ):
            result[relative] = _complete_log_offset(attempt_directory / relative)
    return result


def _read_leader_timeouts(
    spec: SlotRuntimeSpec,
    attempt_directory: Path,
    offsets: Mapping[str, int],
    *,
    end_offsets: Mapping[str, int] | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for replica_id in range(spec.replica_count):
        for relative in (
            spec.process_logs.replica_stdout_relative_paths[replica_id],
            spec.process_logs.replica_stderr_relative_paths[replica_id],
        ):
            path = attempt_directory / relative
            try:
                with path.open("rb") as source:
                    start_offset = offsets[relative]
                    end_offset = (
                        end_offsets[relative]
                        if end_offsets is not None
                        else path.stat().st_size
                    )
                    if (
                        type(start_offset) is not int
                        or type(end_offset) is not int
                        or start_offset < 0
                        or end_offset < start_offset
                        or end_offset > path.stat().st_size
                    ):
                        raise N31LivenessShakedownError(
                            "leader-timeout log capture range is invalid"
                        )
                    source.seek(start_offset)
                    payload = source.read(end_offset - start_offset)
            except (KeyError, OSError) as error:
                raise N31LivenessShakedownError(
                    f"cannot read replica-{replica_id} timeout log: {error}"
                ) from error
            if payload and not payload.endswith(b"\n"):
                raise N31LivenessShakedownError(
                    "leader-timeout log capture ends with a partial line"
                )
            byte_offset = start_offset
            for line in payload.splitlines(keepends=True):
                match = LEADER_TIMEOUT_PATTERN.search(line)
                if match is None or int(match.group("epoch")) != 2:
                    byte_offset += len(line)
                    continue
                rows.append(
                    {
                        "replica_id": replica_id,
                        "epoch_number": 2,
                        "next_tree_id": int(match.group("tree")),
                        "line_sha256": _sha256_bytes(line),
                        "relative_path": relative,
                        "byte_offset": byte_offset,
                    }
                )
                byte_offset += len(line)
    return rows


def _timeout_capture_document(
    *,
    profile_id: str,
    first_common_commit_ns: int,
    observation_deadline_ns: int,
    start_offsets: Mapping[str, int],
    end_offsets: Mapping[str, int],
) -> dict[str, object]:
    if set(start_offsets) != set(end_offsets):
        raise N31LivenessShakedownError(
            "leader-timeout capture path membership drifted"
        )
    ranges: dict[str, object] = {}
    for relative in sorted(start_offsets):
        start = start_offsets[relative]
        end = end_offsets[relative]
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end < start
        ):
            raise N31LivenessShakedownError(
                "leader-timeout capture contains an invalid range"
            )
        ranges[relative] = {
            "start_offset": start,
            "end_offset": end,
        }
    return {
        "schema_version": 1,
        "profile_id": profile_id,
        "source_slot_id": SOURCE_SLOT_ID,
        "capture_rule": "pre_anchor_search_through_observation_deadline_v1",
        "first_common_commit_ns": first_common_commit_ns,
        "observation_deadline_ns": observation_deadline_ns,
        "ranges": ranges,
    }


def _read_timeout_capture(
    spec: SlotRuntimeSpec,
    attempt_directory: Path,
    *,
    profile_id: str,
    first_common_commit_ns: int,
    observation_deadline_ns: int,
) -> tuple[dict[str, int], dict[str, int]]:
    path = attempt_directory / "raw/diagnostics/leader-timeout-window.json"
    try:
        document = _json_object(path.read_bytes(), "leader-timeout capture")
    except OSError as error:
        raise N31LivenessShakedownError(
            f"leader-timeout capture metadata is unavailable: {error}"
        ) from error
    if (
        set(document)
        != {
            "schema_version",
            "profile_id",
            "source_slot_id",
            "capture_rule",
            "first_common_commit_ns",
            "observation_deadline_ns",
            "ranges",
        }
        or document.get("schema_version") != 1
        or document.get("profile_id") != profile_id
        or document.get("source_slot_id") != SOURCE_SLOT_ID
        or document.get("capture_rule")
        != "pre_anchor_search_through_observation_deadline_v1"
        or document.get("first_common_commit_ns") != first_common_commit_ns
        or document.get("observation_deadline_ns") != observation_deadline_ns
        or not isinstance(document.get("ranges"), Mapping)
    ):
        raise N31LivenessShakedownError(
            "leader-timeout capture metadata identity drifted"
        )
    expected_paths = {
        relative
        for replica_id in range(spec.replica_count)
        for relative in (
            spec.process_logs.replica_stdout_relative_paths[replica_id],
            spec.process_logs.replica_stderr_relative_paths[replica_id],
        )
    }
    raw_ranges = document["ranges"]
    assert isinstance(raw_ranges, Mapping)
    if set(raw_ranges) != expected_paths:
        raise N31LivenessShakedownError(
            "leader-timeout capture log membership drifted"
        )
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    for relative in sorted(expected_paths):
        row = raw_ranges[relative]
        if not isinstance(row, Mapping) or set(row) != {
            "start_offset",
            "end_offset",
        }:
            raise N31LivenessShakedownError(
                "leader-timeout capture range schema drifted"
            )
        start = row.get("start_offset")
        end = row.get("end_offset")
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end < start
        ):
            raise N31LivenessShakedownError(
                "leader-timeout capture contains an invalid range"
            )
        starts[relative] = start
        ends[relative] = end
    return starts, ends


def _max_gap_ns(
    start_ns: int,
    end_ns: int,
    timestamps: Sequence[int],
) -> int:
    points = [start_ns]
    points.extend(
        sorted(timestamp for timestamp in timestamps if start_ns <= timestamp <= end_ns)
    )
    points.append(end_ns)
    return max(right - left for left, right in zip(points, points[1:]))


def _detect_head_of_line_stalls(
    profile: FrozenN31LivenessProfile,
    streams: Mapping[str, Sequence[Any]],
    *,
    start_ns: int,
    end_ns: int,
    selected_roots: Sequence[int],
    activated_epoch_digest: str,
    leader_timeouts: Sequence[Mapping[str, object]],
    common_commits: Sequence[Mapping[str, object]],
    max_commit_gap_ns: int,
) -> list[dict[str, object]]:
    """Accept only native queue-decision evidence, never infer queue state."""

    q = int(profile.consensus["quorum"])
    root_by_tree = dict(enumerate(selected_roots))
    timeout_hashes = [str(row["line_sha256"]) for row in leader_timeouts]
    timeout_reporters = {
        int(row["replica_id"]) for row in leader_timeouts
    }
    view_generations = [
        int(row["view_generation"])
        for row in common_commits
        if type(row.get("view_generation")) is int
    ]
    peer_progress = (
        len(timeout_reporters) >= 2
        and len(view_generations) >= 2
        and max(view_generations) > min(view_generations)
    )
    contexts: dict[
        tuple[str, int, int, str, int, str], list[dict[str, object]]
    ] = {}
    queue_events: list[Any] = []
    for source, events in streams.items():
        if not source.startswith("replica-"):
            continue
        for event in events:
            event_type = event.value.get("event_type")
            if event_type == QUEUE_BLOCKED_EVENT_TYPE:
                if start_ns <= event.timestamp_ns <= end_ns:
                    queue_events.append(event)
                continue
            if event_type not in {
                "aggregation.root_quorum_progress",
                "aggregation.root_qc_published",
            } or event.timestamp_ns > end_ns:
                continue
            payload = _event_payload(event)
            if payload.get("epoch_number") != 2:
                continue
            tree_id = payload.get("tree_id")
            context = payload.get("context_generation")
            block_hash = payload.get("block_hash")
            digest = payload.get("epoch_digest")
            signer_count = payload.get("root_signer_count")
            if (
                type(tree_id) is not int
                or tree_id not in root_by_tree
                or type(context) is not int
                or context <= 0
                or not _valid_sha256(block_hash)
                or digest != activated_epoch_digest
                or type(signer_count) is not int
                or signer_count < 0
                or payload.get("global_quorum") != q
            ):
                raise N31LivenessShakedownError(
                    "Epoch2 root aggregation event is malformed"
                )
            expected_source = f"replica-{root_by_tree[tree_id]}"
            if (
                source != expected_source
                or payload.get("observer_replica") != root_by_tree[tree_id]
            ):
                raise N31LivenessShakedownError(
                    "Epoch2 root aggregation event has the wrong root source"
                )
            key = (source, 2, tree_id, str(digest), context, str(block_hash))
            contexts.setdefault(key, []).append(
                {
                    "source_monotonic_ns": event.timestamp_ns,
                    "line_number": event.line_number,
                    "signer_count": signer_count,
                    "published": event_type == "aggregation.root_qc_published",
                    "reference": _event_reference(event),
                }
            )

    def context_at_queue(
        key: tuple[str, int, int, str, int, str],
        queue_event: Any,
    ) -> dict[str, object] | None:
        rows = [
            row
            for row in contexts.get(key, ())
            if (
                int(row["source_monotonic_ns"]),
                int(row["line_number"]),
            )
            <= (queue_event.timestamp_ns, queue_event.line_number)
        ]
        if not rows:
            return None
        return {
            "max_signer_count": max(int(row["signer_count"]) for row in rows),
            "published": any(bool(row["published"]) for row in rows),
            "references": [row["reference"] for row in rows],
        }

    hard = frozenset(
        int(value) for value in profile.fault_cohorts["hard_actor_ids"]
    )
    degraded = frozenset(
        int(value)
        for value in profile.fault_cohorts["responsive_degraded_actor_ids"]
    )
    stalls: list[dict[str, object]] = []
    for event in queue_events:
        source = event.source
        payload = _event_payload(event)
        if set(payload) != QUEUE_BLOCKED_FIELDS:
            raise N31LivenessShakedownError(
                "queue-blocked structured evidence schema drifted"
            )
        tree_id = payload.get("tree_id")
        root_id = root_by_tree.get(tree_id) if type(tree_id) is int else None
        head_context = payload.get("queue_head_context_generation")
        later_context = payload.get("queued_candidate_context_generation")
        head_height = payload.get("queue_head_block_height")
        later_height = payload.get("queued_candidate_block_height")
        head_hash = payload.get("queue_head_block_hash")
        later_hash = payload.get("queued_candidate_block_hash")
        later_parent = payload.get("queued_candidate_parent_hash")
        head_count = payload.get("queue_head_signer_count")
        later_count = payload.get("queued_candidate_signer_count")
        if payload.get("epoch_digest") != activated_epoch_digest:
            raise N31LivenessShakedownError(
                "queue evidence differs from unanimously activated Epoch2"
            )
        if (
            payload.get("epoch_number") != 2
            or root_id is None
            or source != f"replica-{root_id}"
            or payload.get("observer_replica") != root_id
            or payload.get("global_quorum") != q
            or payload.get("queue_head_position") != 0
            or payload.get("queued_candidate_position") != 1
            or type(head_context) is not int
            or type(later_context) is not int
            or head_context <= 0
            or later_context <= head_context
            or type(head_height) is not int
            or type(later_height) is not int
            or head_height <= 0
            or later_height != head_height + 1
            or not _valid_sha256(head_hash)
            or not _valid_sha256(later_hash)
            or later_parent != head_hash
            or later_hash == head_hash
            or type(head_count) is not int
            or type(later_count) is not int
            or not 0 <= head_count < q <= later_count
            or payload.get("queued_candidate_qc_ready") is not True
            or payload.get("queued_candidate_qc_published") is not False
        ):
            raise N31LivenessShakedownError(
                "queue-blocked structured evidence is malformed"
            )
        head_key = (
            source,
            2,
            int(tree_id),
            activated_epoch_digest,
            head_context,
            str(head_hash),
        )
        later_key = (
            source,
            2,
            int(tree_id),
            activated_epoch_digest,
            later_context,
            str(later_hash),
        )
        head = context_at_queue(head_key, event)
        later = context_at_queue(later_key, event)
        if (
            head is None
            or later is None
            or int(head["max_signer_count"]) != head_count
            or int(later["max_signer_count"]) != later_count
            or bool(head["published"])
            or bool(later["published"])
        ):
            raise N31LivenessShakedownError(
                "queue evidence does not bind exact aggregation contexts"
            )
        if root_id in hard | degraded:
            continue
        references = [*head["references"], *later["references"]]
        assert all(isinstance(reference, Mapping) for reference in references)
        source_hashes = [
            str(reference["line_sha256"]) for reference in references
        ]
        source_hashes.extend(timeout_hashes)
        source_hashes.append(event.line_sha256)
        stalls.append(
            {
                "replica_id": root_id,
                "root_id": root_id,
                "non_injected": True,
                "lag_observed": max_commit_gap_ns
                >= _leader_timeout_ns(profile),
                "survived_sigint": False,
                "survived_sigterm": False,
                "pre_kill_stack_valid": False,
                "peer_timeout_view_progress": (
                    peer_progress and root_id in timeout_reporters
                ),
                "activated_epoch_digest": activated_epoch_digest,
                "queue_evidence_sha256": event.line_sha256,
                "queue_head_position": 0,
                "later_queue_position": 1,
                "head_context_generation": head_context,
                "later_context_generation": later_context,
                "head_block_height": head_height,
                "later_block_height": later_height,
                "head_block_hash": str(head_hash),
                "later_block_hash": str(later_hash),
                "later_parent_hash": str(later_parent),
                "head_verified_signer_count": head_count,
                "later_verified_signer_count": later_count,
                "quorum": q,
                "later_qc_ready": True,
                "later_qc_published": False,
                "blocked_by_queue_head": True,
                "leader_progress_timeout_observed": (
                    root_id in timeout_reporters
                ),
                "commit_gap_ns": max_commit_gap_ns,
                "source_event_sha256s": sorted(set(source_hashes)),
            }
        )
    return stalls


def _observe_liveness_window(
    profile: FrozenN31LivenessProfile,
    spec: SlotRuntimeSpec,
    attempt_directory: Path,
    records: Sequence[ProcessRecord],
    *,
    hard_deadline_ns: int,
    raw_now_ns: Callable[[], int] = _factorial.monotonic_raw_ns,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_s: float = 0.05,
) -> dict[str, object]:
    """Observe exactly 45 seconds from the first any-Q21 Epoch2 commit."""

    expected_clean_exits: set[str] = set()

    def authorize(record: ProcessRecord) -> bool:
        if record.name in expected_clean_exits:
            return True
        try:
            accepted = _factorial._authorize_manager_clean_exit(  # noqa: SLF001
                spec,
                attempt_directory,
                record,
            )
        except FactorialExecutionError as error:
            raise N31LivenessShakedownError(str(error)) from error
        if accepted:
            expected_clean_exits.add(record.name)
        return accepted

    first: dict[str, object] | None = None
    streams: Mapping[str, Sequence[Any]] = {}
    anchor_ns = hard_deadline_ns - (
        int(profile.timers["hard_timeout_s"]) * NANOSECONDS_PER_SECOND
    )
    ready_deadline_ns = anchor_ns + (
        int(profile.timers["startup_timeout_s"]) * NANOSECONDS_PER_SECOND
    )
    while True:
        try:
            streams = read_event_streams(spec, attempt_directory, allow_partial=True)
            ready = all(
                sum(
                    event.value.get("event_type") == "process.ready"
                    for event in events
                )
                == 1
                for events in streams.values()
            )
            _factorial._assert_process_health(  # noqa: SLF001
                records,
                expected_clean_exits=expected_clean_exits,
                clean_exit_authorizer=authorize,
            )
        except FactorialExecutionError as error:
            raise N31LivenessShakedownError(str(error)) from error
        if ready:
            break
        if raw_now_ns() >= ready_deadline_ns:
            raise N31LivenessShakedownError(
                "startup timeout expired before every process.ready event"
            )
        sleep(poll_interval_s)
    offsets = _leader_timeout_offsets(spec, attempt_directory)
    while first is None:
        try:
            streams = read_event_streams(spec, attempt_directory, allow_partial=True)
            commits = _common_epoch2_commits(spec, streams)
            first = commits[0] if commits else None
            _factorial._assert_process_health(  # noqa: SLF001
                records,
                expected_clean_exits=expected_clean_exits,
                clean_exit_authorizer=authorize,
            )
        except FactorialExecutionError as error:
            raise N31LivenessShakedownError(str(error)) from error
        now_ns = raw_now_ns()
        if now_ns >= hard_deadline_ns:
            raise N31LivenessShakedownError(
                "hard deadline expired before the first common Epoch2 commit"
            )
        if first is None:
            sleep(poll_interval_s)
    first_ns = int(first["common_monotonic_ns"])
    deadline_ns = first_ns + (
        int(profile.observation["duration_s"]) * NANOSECONDS_PER_SECOND
    )
    if raw_now_ns() >= deadline_ns:
        raise N31LivenessShakedownError(
            "first common Epoch2 commit was not observed in time to start the window"
        )
    selected_roots, bundle_digest, initial_tree_id = _epoch2_bundle_identity(
        spec, attempt_directory, profile
    )
    activation = _epoch2_activation_progress(
        spec,
        streams,
        expected_initial_tree_id=initial_tree_id,
    )
    if activation["epoch_digest"] != bundle_digest:
        raise N31LivenessShakedownError(
            "unanimous Epoch2 activation differs from signed successor bundle"
        )
    anchored_commits = _common_epoch2_commits(
        spec,
        streams,
        expected_epoch_digest=bundle_digest,
    )
    if not anchored_commits or any(
        anchored_commits[0].get(field) != first.get(field)
        for field in (
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "common_monotonic_ns",
        )
    ):
        raise N31LivenessShakedownError(
            "first common Epoch2 commit differs from signed-bundle evidence"
        )
    while raw_now_ns() < deadline_ns:
        try:
            streams = read_event_streams(spec, attempt_directory, allow_partial=True)
            _factorial._assert_process_health(  # noqa: SLF001
                records,
                expected_clean_exits=expected_clean_exits,
                clean_exit_authorizer=authorize,
            )
        except FactorialExecutionError as error:
            raise N31LivenessShakedownError(str(error)) from error
        if raw_now_ns() >= hard_deadline_ns:
            raise N31LivenessShakedownError(
                "hard deadline expired during the 45-second Epoch2 window"
            )
        sleep(
            min(
                poll_interval_s,
                max(0.0, (deadline_ns - raw_now_ns()) / NANOSECONDS_PER_SECOND),
            )
        )
    end_ns = raw_now_ns()
    end_offsets = _leader_timeout_offsets(spec, attempt_directory)
    try:
        streams = read_event_streams(spec, attempt_directory, allow_partial=True)
    except FactorialExecutionError as error:
        raise N31LivenessShakedownError(str(error)) from error
    commits = _common_epoch2_commits(
        spec,
        streams,
        earliest_ns=first_ns,
        latest_ns=deadline_ns,
        expected_epoch_digest=bundle_digest,
    )
    if not commits or any(
        commits[0].get(field) != first.get(field)
        for field in (
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "common_monotonic_ns",
        )
    ):
        raise N31LivenessShakedownError(
            "first common Epoch2 commit identity drifted during observation"
        )
    authoritative = _authoritative_epoch2_commits(
        spec,
        streams,
        start_ns=first_ns,
        end_ns=deadline_ns,
        expected_epoch_digest=bundle_digest,
        selected_roots=selected_roots,
    )
    if not authoritative:
        raise N31LivenessShakedownError(
            "authoritative observer produced no Epoch2 commit in the window"
        )
    exercised = []
    for row in authoritative:
        root = int(row["root_id"])
        if root not in exercised:
            exercised.append(root)
    timeout_rows = _read_leader_timeouts(
        spec,
        attempt_directory,
        offsets,
        end_offsets=end_offsets,
    )
    _write_exclusive(
        attempt_directory / "raw/diagnostics/leader-timeout-window.json",
        _canonical_json_bytes(
            _timeout_capture_document(
                profile_id=profile.profile_id,
                first_common_commit_ns=first_ns,
                observation_deadline_ns=deadline_ns,
                start_offsets=offsets,
                end_offsets=end_offsets,
            ),
            newline=True,
        ),
    )
    authoritative_timestamps = [
        int(row["source_monotonic_ns"]) for row in authoritative
    ]
    common_timestamps = [int(row["common_monotonic_ns"]) for row in commits]
    max_authoritative_gap = _max_gap_ns(
        first_ns,
        deadline_ns,
        authoritative_timestamps,
    )
    max_common_gap = _max_gap_ns(first_ns, deadline_ns, common_timestamps)
    stalls = _detect_head_of_line_stalls(
        profile,
        streams,
        start_ns=first_ns,
        end_ns=deadline_ns,
        selected_roots=selected_roots,
        activated_epoch_digest=bundle_digest,
        leader_timeouts=timeout_rows,
        common_commits=commits,
        max_commit_gap_ns=max_authoritative_gap,
    )
    leader_timeout_ns = _leader_timeout_ns(profile)
    final_common_ns = int(commits[-1]["common_monotonic_ns"])
    return {
        "epoch2": {
            "first_common_commit_ns": first_ns,
            "observation_deadline_ns": deadline_ns,
            "observation_end_ns": end_ns,
            "final_common_commit_ns": final_common_ns,
            "selected_root_ids": list(selected_roots),
            "exercised_root_ids": exercised,
            "activated_epoch_digest": bundle_digest,
        },
        "liveness": {
            "head_of_line_stalls": stalls,
            "leader_progress_timeout_count": len(timeout_rows),
            "leader_progress_timeouts": timeout_rows,
            "q21_progress_continued": (
                max_common_gap < leader_timeout_ns
                and final_common_ns >= deadline_ns - leader_timeout_ns
            ),
            "max_authoritative_commit_gap_ns": max_authoritative_gap,
            "max_common_q21_commit_gap_ns": max_common_gap,
        },
        "configuration_progress": {
            "epoch2_activation": activation,
            "authoritative_commit_count": len(authoritative),
            "common_q21_commit_count": len(commits),
            "first_block_height": min(
                int(row["block_height"]) for row in authoritative
            ),
            "last_block_height": max(
                int(row["block_height"]) for row in authoritative
            ),
            "epoch_digest_count": 1,
            "tree_ids": sorted({int(row["tree_id"]) for row in authoritative}),
            "authoritative_commits": authoritative,
            "common_q21_commits": commits,
        },
        "consensus_conflict_count": 0,
    }


def _sample_rows(registry: ProcessRegistry) -> list[dict[str, object]]:
    return [
        {
            "name": outcome.name,
            "replica_id": (
                outcome.replica_id if outcome.replica_id >= 0 else None
            ),
            "pid": outcome.pid,
            "pgid": outcome.pgid,
            "after_signal_number": outcome.after_signal_number,
            "before_signal_number": outcome.before_signal_number,
            "status": outcome.status,
            "artifact_relative_path": outcome.artifact_relative_path,
            "error": outcome.error,
        }
        for outcome in registry.cleanup_escalations
    ]


def _recompute_cleanup_facts(
    attempt_directory: Path,
    cleanup_rows: Sequence[Mapping[str, object]],
    sample_rows: Sequence[Mapping[str, object]],
    *,
    diagnostic_replica_ids: frozenset[int],
    expected_clean_exits: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Derive strict cleanup truth from identity-bound rows and sample bytes."""

    expected_authorizations = expected_clean_exits or {}
    cleanup_by_name: dict[str, Mapping[str, object]] = {}
    cleanup_by_replica: dict[int, Mapping[str, object]] = {}
    pids: set[int] = set()
    for row in cleanup_rows:
        name = row.get("name")
        replica_id = row.get("replica_id")
        pid = row.get("pid")
        pgid = row.get("pgid")
        signal_number = row.get("signal_number")
        returncode = row.get("returncode")
        exact_actor_identity = (
            name == "adaptive-manager" and replica_id is None
        ) or (
            type(replica_id) is int
            and 0 <= replica_id < FROZEN_REPLICA_COUNT
            and name == f"replica-{replica_id}"
        )
        authorized_manager_clean_exit = (
            name == "adaptive-manager"
            and row.get("classification") == "expected_clean_exit"
            and row.get("exit_authorization")
            == expected_authorizations.get("adaptive-manager")
            and "adaptive-manager" in expected_authorizations
        )
        if (
            not isinstance(name, str)
            or not name
            or not exact_actor_identity
            or name in cleanup_by_name
            or type(pid) is not int
            or pid <= 1
            or pid in pids
            or type(pgid) is not int
            or pgid <= 1
            or pgid != pid
            or type(returncode) is not int
            or (
                type(signal_number) is not int
                and not (
                    signal_number is None
                    and authorized_manager_clean_exit
                )
            )
            or (
                replica_id is not None
                and replica_id in cleanup_by_replica
            )
        ):
            raise N31LivenessShakedownError(
                "cleanup process identity is malformed or duplicated"
            )
        cleanup_by_name[name] = row
        pids.add(pid)
        if type(replica_id) is int:
            cleanup_by_replica[replica_id] = row
    expected_names = {
        "adaptive-manager",
        *(f"replica-{replica_id}" for replica_id in range(FROZEN_REPLICA_COUNT)),
    }
    if (
        set(cleanup_by_name) != expected_names
        or set(cleanup_by_replica) != set(range(FROZEN_REPLICA_COUNT))
    ):
        raise N31LivenessShakedownError(
            "cleanup process identity membership is incomplete"
        )

    sample_hashes: dict[int, str] = {}
    sample_names: set[str] = set()
    capture_complete = True
    for row in sample_rows:
        name = row.get("name")
        replica_id = row.get("replica_id")
        cleanup = cleanup_by_name.get(name) if isinstance(name, str) else None
        if (
            cleanup is None
            or name in sample_names
            or type(replica_id) is not int
            or type(row.get("pid")) is not int
            or type(row.get("pgid")) is not int
            or type(row.get("after_signal_number")) is not int
            or type(row.get("before_signal_number")) is not int
            or cleanup.get("replica_id") != replica_id
            or cleanup.get("pid") != row.get("pid")
            or cleanup.get("pgid") != row.get("pgid")
            or row.get("after_signal_number") != int(signal.SIGINT)
            or row.get("before_signal_number") != int(signal.SIGTERM)
        ):
            raise N31LivenessShakedownError(
                "cleanup sample identity does not bind its replica/PID"
            )
        if cleanup.get("signal_number") not in {
            int(signal.SIGTERM),
            int(signal.SIGKILL),
        }:
            raise N31LivenessShakedownError(
                "cleanup sample contradicts a claimed SIGINT exit"
            )
        sample_names.add(name)
        expected_relative = f"raw/diagnostics/{name}.sample.txt"
        relative = row.get("artifact_relative_path")
        captured = (
            row.get("status") == "captured"
            and relative == expected_relative
            and row.get("error") is None
        )
        if not captured:
            capture_complete = False
            continue
        sample_path = attempt_directory / expected_relative
        try:
            sample_path.resolve().relative_to(attempt_directory.resolve())
            payload = sample_path.read_bytes()
        except (OSError, ValueError) as error:
            raise N31LivenessShakedownError(
                f"cleanup sample artifact is unsafe or absent: {error}"
            ) from error
        if sample_path.is_symlink() or not payload:
            raise N31LivenessShakedownError(
                "cleanup sample artifact is empty or unsafe"
            )
        pid_text = str(row["pid"]).encode("ascii")
        process_header = re.compile(
            rb"(?m)^Process:[^\r\n]*\[\s*" + pid_text + rb"\s*\]\s*$"
        )
        sampling_header = re.compile(
            rb"(?m)^(?:Sampling process|Analysis of sampling process)\s+"
            + pid_text
            + rb"\b"
        )
        if (
            process_header.search(payload) is None
            and sampling_header.search(payload) is None
        ) or b"Call graph:" not in payload:
            raise N31LivenessShakedownError(
                "cleanup sample bytes do not bind the claimed PID/stack"
            )
        sample_hashes[replica_id] = _sha256_bytes(payload)

    diagnostic_escalation_ids: set[int] = set()
    unexpected_names: set[str] = set()
    all_sigint = bool(cleanup_rows)
    for row in cleanup_rows:
        replica_id = row.get("replica_id")
        signal_number = row.get("signal_number")
        returncode = row.get("returncode")
        sigint_exit = (
            signal_number == int(signal.SIGINT)
            and returncode in (0, -int(signal.SIGINT))
            and row.get("classification") == "expected_cleanup"
            and row.get("exit_authorization") is None
        )
        clean_exit = (
            row.get("name") == "adaptive-manager"
            and row.get("classification") == "expected_clean_exit"
            and signal_number is None
            and returncode == 0
            and row.get("exit_authorization")
            == expected_authorizations.get("adaptive-manager")
            and "adaptive-manager" in expected_authorizations
        )
        all_sigint = all_sigint and (sigint_exit or clean_exit)
        diagnostic_exit = (
            type(replica_id) is int
            and replica_id in diagnostic_replica_ids
            and signal_number == int(signal.SIGKILL)
            and returncode == -int(signal.SIGKILL)
            and row.get("classification")
            == "unexpected_cleanup_escalation"
            and row.get("exit_authorization") is None
            and replica_id in sample_hashes
        )
        if diagnostic_exit:
            diagnostic_escalation_ids.add(replica_id)
        elif not sigint_exit and not clean_exit:
            unexpected_names.add(str(row["name"]))
    unexpected_names.update(
        f"replica-{replica_id}"
        for replica_id in diagnostic_replica_ids - diagnostic_escalation_ids
    )
    return {
        "unexpected_process_exit_count": len(unexpected_names),
        "all_processes_exited_after_sigint": all_sigint,
        "capture_complete": capture_complete,
        "diagnostic_escalation_ids": frozenset(diagnostic_escalation_ids),
        "sample_sha256_by_replica": sample_hashes,
    }


def _merge_cleanup_observation(
    profile: FrozenN31LivenessProfile,
    attempt_directory: Path,
    observed: Mapping[str, object],
    cleanup_rows: Sequence[Mapping[str, object]],
    sample_rows: Sequence[Mapping[str, object]],
    *,
    cleanup_complete: bool,
    streams_closed: bool,
    ports_clear: bool,
    final_events_complete: bool,
    expected_clean_exits: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    liveness = dict(observed["liveness"])
    stalls = [dict(value) for value in liveness["head_of_line_stalls"]]
    diagnostic_replica_ids = frozenset(
        int(stall["replica_id"]) for stall in stalls
    )
    facts = _recompute_cleanup_facts(
        attempt_directory,
        cleanup_rows,
        sample_rows,
        diagnostic_replica_ids=diagnostic_replica_ids,
        expected_clean_exits=expected_clean_exits,
    )
    escalation_ids = facts["diagnostic_escalation_ids"]
    sample_hashes = facts["sample_sha256_by_replica"]
    assert isinstance(escalation_ids, frozenset)
    assert isinstance(sample_hashes, Mapping)
    for stall in stalls:
        replica_id = int(stall["replica_id"])
        killed = replica_id in escalation_ids
        sample_digest = sample_hashes.get(replica_id)
        stall["survived_sigint"] = killed
        stall["survived_sigterm"] = killed
        stall["pre_kill_stack_valid"] = isinstance(sample_digest, str)
        stall["stack_sample_sha256"] = sample_digest
    liveness["head_of_line_stalls"] = stalls
    return {
        "liveness": liveness,
        "execution": {
            "cleanup_complete": cleanup_complete
            and streams_closed
            and ports_clear
            and final_events_complete
            and facts["capture_complete"] is True,
            "unexpected_process_exit_count": facts[
                "unexpected_process_exit_count"
            ],
            "consensus_conflict_count": int(
                observed["consensus_conflict_count"]
            ),
            "all_processes_exited_after_sigint": facts[
                "all_processes_exited_after_sigint"
            ],
            "processes": list(cleanup_rows),
            "cleanup_samples": list(sample_rows),
        },
    }


def _live_execution_root(
    attempt_directory: Path,
    *,
    create: bool = False,
) -> Path:
    """Return the one slot-named live-evidence root inside an attempt."""

    attempt = Path(attempt_directory)
    if attempt.is_symlink() or not attempt.is_dir():
        raise N31LivenessShakedownError(
            "live execution requires a safe allocated attempt directory"
        )
    attempt = attempt.resolve()
    if ATTEMPT_NAME.fullmatch(attempt.name) is None:
        raise N31LivenessShakedownError(
            "live execution attempt identity is malformed"
        )
    execution_root = attempt / SOURCE_SLOT_ID
    if create:
        try:
            execution_root.mkdir(mode=0o700, exist_ok=False)
        except OSError as error:
            raise N31LivenessShakedownError(
                f"cannot claim live execution root: {error}"
            ) from error
    elif execution_root.exists() and (
        execution_root.is_symlink() or not execution_root.is_dir()
    ):
        raise N31LivenessShakedownError(
            "live execution root is not a safe directory"
        )
    return execution_root


def _create_runtime_directories(
    execution_root: Path,
    spec: SlotRuntimeSpec,
) -> None:
    for relative in (
        "runtime",
        "raw",
        "raw/process",
        "raw/diagnostics",
        "transitions",
        "source",
    ):
        path = execution_root / relative
        path.mkdir(mode=0o700, exist_ok=False)
    for transition in spec.transitions:
        (execution_root / transition.bundle_relative_path).parent.mkdir(
            parents=True,
            exist_ok=False,
            mode=0o700,
        )


def _preserved_binaries(root: Path) -> ExecutionBinaries:
    binary_root = root / "build-evidence/binaries"
    binaries = ExecutionBinaries(
        app=binary_root / "app",
        manager=binary_root / "manager",
        keygen=binary_root / "keygen",
        tls_keygen=binary_root / "tls_keygen",
    )
    if any(
        path.is_symlink() or not path.is_file()
        for path in binaries.as_mapping().values()
    ):
        raise N31LivenessShakedownError(
            "preserved build evidence lacks an execution binary"
        )
    return binaries


def _binary_identities(binaries: ExecutionBinaries) -> dict[str, object]:
    return {
        name: {
            "relative_path": str(path),
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for name, path in binaries.as_mapping().items()
    }


def _launch_document(
    source: SourceSlot,
    materialized: Any,
    *,
    profile_id: str,
    pair_id: str,
    attempt_ordinal: int,
    anchor_ns: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "profile_id": profile_id,
        "source_slot_id": SOURCE_SLOT_ID,
        "pair_id": pair_id,
        "source_runtime_artifact_id": source.runtime.artifact_id,
        "attempt_ordinal": attempt_ordinal,
        "launch_count": 1,
        "retry_count": 0,
        "replacement_count": 0,
        "shared_raw_clock_anchor_ns": anchor_ns,
        "manifest_sha256": _sha256_bytes(source.manifest_bytes),
        "plan_sha256": _sha256_bytes(source.plan_bytes),
        "runtime_sha256": _sha256_bytes(source.runtime_bytes),
        "redaction_key_id": materialized.redaction_key_id,
        "manager_argv": list(materialized.redacted_manager_argv),
        "replica_argv": [
            {
                "replica_id": process.replica_id,
                "argv": list(process.argv),
            }
            for process in materialized.redacted_replica_argv
        ],
        "claim_eligible": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }


def _write_cleanup_artifacts(
    attempt_directory: Path,
    cleanup_rows: Sequence[Mapping[str, object]],
    sample_rows: Sequence[Mapping[str, object]],
    *,
    profile_id: str,
    cleanup_started_ns: int,
    cleanup_complete: bool,
    streams_closed: bool,
    ports_clear: bool,
    final_events_complete: bool,
    error: str | None,
) -> None:
    _write_exclusive(
        attempt_directory / "raw/diagnostics/cleanup-samples.json",
        _canonical_json_bytes(
            {
                "schema_version": 1,
                "profile_id": profile_id,
                "attempts": list(sample_rows),
            },
            newline=True,
        ),
    )
    _write_exclusive(
        attempt_directory / "cleanup-ledger.json",
        _canonical_json_bytes(
            {
                "schema_version": 1,
                "profile_id": profile_id,
                "cleanup_started_monotonic_ns": cleanup_started_ns,
                "cleanup_complete": cleanup_complete,
                "streams_closed": streams_closed,
                "ports_clear": ports_clear,
                "final_events_complete": final_events_complete,
                "processes": list(cleanup_rows),
                "error": error,
            },
            newline=True,
        ),
    )


def _execute_live_attempt(
    attempt_directory: Path,
    profile: FrozenN31LivenessProfile,
    *,
    receipt: Mapping[str, object],
    approval_receipt: Mapping[str, object],
    repository: Path,
    build_directory: Path,
    build_provenance_path: Path,
    results_root: Path,
    pair_id: str,
    minimum_free_bytes: int,
    raw_now_ns: Callable[[], int] = _factorial.monotonic_raw_ns,
    run_command: Callable[..., Any] = subprocess.run,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    registry_factory: Callable[..., ProcessRegistry] = ProcessRegistry,
    wait_ports_clear: Callable[[Sequence[int], float], None] = (
        _factorial._legacy_runtime.wait_ports_clear  # noqa: SLF001
    ),
    cleanup_timeout_s: float = _factorial.DEFAULT_CLEANUP_TIMEOUT_S,
    sample_run_command: Callable[..., Any] = subprocess.run,
    sample_platform: str = sys.platform,
) -> Mapping[str, object]:
    """Launch the exact v13 inputs once and return a sealed-ready observation."""

    repository = repository.resolve()
    results_root = results_root.resolve()
    release = _release_for_profile(profile)
    if results_root != (
        repository / release.canonical_results_relative_path
    ).resolve():
        raise N31LivenessShakedownError(
            "live shakedown cannot move outside its canonical two-attempt root"
        )
    attempt_directory = Path(attempt_directory)
    if (
        attempt_directory.is_symlink()
        or not attempt_directory.is_dir()
        or attempt_directory.resolve().parent != results_root
    ):
        raise N31LivenessShakedownError(
            "live shakedown attempt is not inside the canonical pair root"
        )
    expected_execution_root = _live_execution_root(attempt_directory)
    source = _source_slot(repository)
    try:
        live = verify_evidence_preflight(
            source.slot,
            repository=repository,
            build_directory=build_directory.resolve(),
            build_provenance_path=build_provenance_path.resolve(),
            result_root=attempt_directory.resolve(),
            minimum_free_bytes=minimum_free_bytes,
        )
    except FactorialExecutionError as error:
        raise N31LivenessShakedownError(str(error)) from error
    current_receipt = preflight_from_documents(
        profile,
        current_revision=live.revision,
        build_provenance=live.build_provenance,
        approval_receipt=approval_receipt,
    )
    if _canonical_json_bytes(current_receipt) != _canonical_json_bytes(receipt):
        raise N31LivenessShakedownError(
            "revision/build/approval provenance drifted after preflight"
        )
    if live.slot_directory != expected_execution_root:
        raise N31LivenessShakedownError(
            "live preflight selected an unexpected execution root"
        )

    execution_root = _live_execution_root(attempt_directory, create=True)
    _create_runtime_directories(execution_root, source.runtime)
    provenance_root = execution_root / "exact-build"
    try:
        preserve_build_evidence(provenance_root, live.build_provenance)
    except FactorialExecutionError as error:
        raise N31LivenessShakedownError(str(error)) from error
    binaries = _preserved_binaries(provenance_root)
    _write_exclusive(
        execution_root / "source/manifest.json",
        source.manifest_bytes,
    )
    _write_exclusive(
        execution_root / "source/plan.json",
        source.plan_bytes,
    )
    _write_exclusive(
        execution_root / "source/runtime.json",
        source.runtime_bytes,
    )
    _write_exclusive(
        execution_root / "source/slot.json",
        _canonical_json_bytes(source.slot.as_document(), newline=True),
    )
    _write_exclusive(
        execution_root / "source/slot-runtime.json",
        _canonical_json_bytes(source.runtime.as_document(), newline=True),
    )
    _write_exclusive(
        execution_root / "runtime/exact-build-provenance.json",
        _canonical_json_bytes(live.build_provenance, newline=True),
    )

    spawned: list[Any] = []
    records: list[ProcessRecord] = []
    observation: Mapping[str, object] | None = None
    runtime_error: BaseException | None = None
    cleanup_error: str | None = None
    cleanup_rows: tuple[Mapping[str, object], ...] = ()
    sample_rows: list[dict[str, object]] = []
    cleanup_started_ns = raw_now_ns()
    cleanup_complete = False
    streams_closed = False
    ports_clear = False
    final_events_complete = False
    registry = registry_factory(
        monotonic_ns=raw_now_ns,
        cleanup_escalation_hook=lambda record: _factorial._capture_cleanup_sample(  # noqa: SLF001
            record,
            slot_directory=execution_root,
            platform_name=sample_platform,
            run_command=sample_run_command,
        ),
    )
    try:
        identities = generate_identities(
            source.runtime,
            binaries=binaries,
            runtime_directory=execution_root / "runtime",
            run_command=run_command,
        )
        inputs = write_slot_configs(
            source.slot,
            source.runtime,
            slot_directory=execution_root,
            identities=identities,
        )
        anchor_ns = raw_now_ns()
        attempt_ordinal = _attempt_ordinal(attempt_directory)
        materialized = materialize_launch(
            source.slot,
            source.runtime,
            slot_directory=execution_root,
            binaries=binaries,
            identities=identities,
            input_artifacts=inputs,
            shared_raw_clock_anchor_ns=anchor_ns,
            redaction_key=_factorial._derive_redaction_key(  # noqa: SLF001
                _canonical_json_bytes(approval_receipt),
                f"{SOURCE_SLOT_ID}:attempt-{attempt_ordinal}",
            ),
        )
        launch_document = _launch_document(
            source,
            materialized,
            profile_id=profile.profile_id,
            pair_id=pair_id,
            attempt_ordinal=attempt_ordinal,
            anchor_ns=anchor_ns,
        )
        _write_exclusive(
            execution_root / "launch.json",
            _canonical_json_bytes(launch_document, newline=True),
        )
        _write_exclusive(
            execution_root / "runtime/execution-provenance.json",
            _canonical_json_bytes(
                {
                    "schema_version": 1,
                    "profile_id": profile.profile_id,
                    "source_slot_id": SOURCE_SLOT_ID,
                    "pair_id": pair_id,
                    "attempt_ordinal": attempt_ordinal,
                    "revision": live.revision,
                    "build_provenance_sha256": receipt[
                        "build_provenance_sha256"
                    ],
                    "approval_receipt_sha256": receipt[
                        "approval_receipt_sha256"
                    ],
                    "binaries": _binary_identities(binaries),
                    "input_artifacts": list(inputs),
                    "launch_sha256": _sha256_bytes(
                        _canonical_json_bytes(launch_document, newline=True)
                    ),
                    "launch_count": 1,
                    "retry_count": 0,
                    "replacement_count": 0,
                    "claim_eligible": False,
                    "campaign_member": False,
                    "denominator_contribution": 0,
                    "figure_eligible": False,
                },
                newline=True,
            ),
        )
        manager = spawn_exclusive_owned_process(
            registry,
            name="adaptive-manager",
            replica_id=_factorial.MANAGER_REPLICA_ID,
            command=materialized.manager_argv,
            stdout_path=(
                execution_root
                / source.runtime.process_logs.manager_stdout_relative_path
            ),
            stderr_path=(
                execution_root
                / source.runtime.process_logs.manager_stderr_relative_path
            ),
            working_directory=execution_root,
            popen_factory=popen_factory,
        )
        spawned.append(manager)
        records.append(manager.record)
        for process in materialized.replica_argv:
            replica_id = process.replica_id
            launched = spawn_exclusive_owned_process(
                registry,
                name=f"replica-{replica_id}",
                replica_id=replica_id,
                command=process.argv,
                stdout_path=(
                    execution_root
                    / source.runtime.process_logs.replica_stdout_relative_paths[
                        replica_id
                    ]
                ),
                stderr_path=(
                    execution_root
                    / source.runtime.process_logs.replica_stderr_relative_paths[
                        replica_id
                    ]
                ),
                working_directory=execution_root,
                popen_factory=popen_factory,
            )
            spawned.append(launched)
            records.append(launched.record)
        if len(records) != source.runtime.replica_count + 1:
            raise N31LivenessShakedownError(
                "one-shot process launch cardinality drifted"
            )
        hard_deadline_ns = anchor_ns + (
            int(profile.timers["hard_timeout_s"]) * NANOSECONDS_PER_SECOND
        )
        observation = _observe_liveness_window(
            profile,
            source.runtime,
            execution_root,
            records,
            hard_deadline_ns=hard_deadline_ns,
            raw_now_ns=raw_now_ns,
        )
    except BaseException as error:
        runtime_error = error
    finally:
        cleanup_started_ns = raw_now_ns()
        outcomes: tuple[CleanupOutcome, ...] = ()
        try:
            outcomes = registry.cleanup(timeout_s=cleanup_timeout_s)
            cleanup_complete = True
        except BaseException as error:
            cleanup_error = f"{type(error).__name__}: {error}"
        sample_rows = _sample_rows(registry)
        streams_closed = True
        for launched in spawned:
            for stream in (launched.stdout, launched.stderr):
                try:
                    stream.close()
                except OSError as error:
                    streams_closed = False
                    cleanup_error = cleanup_error or str(error)
        ports_clear = True
        try:
            wait_ports_clear(slot_ports(source.slot), cleanup_timeout_s)
        except BaseException as error:
            ports_clear = False
            cleanup_error = cleanup_error or str(error)
        expected_clean_exits: dict[str, Mapping[str, object]] = {}
        if cleanup_complete and streams_closed:
            try:
                expected_clean_exits = _factorial._final_expected_clean_exits(  # noqa: SLF001
                    source.runtime,
                    execution_root,
                )
                read_event_streams(
                    source.runtime,
                    execution_root,
                    allow_partial=False,
                )
                final_events_complete = True
            except (FactorialExecutionError, OSError, ValueError) as error:
                cleanup_error = cleanup_error or (
                    f"final structured-event capture failed: {error}"
                )
        cleanup_rows = _factorial._cleanup_ledger(  # noqa: SLF001
            records,
            outcomes,
            cleanup_started_ns=cleanup_started_ns,
            expected_clean_exits=expected_clean_exits,
            cleanup_completed=cleanup_complete,
            injected_replica_ids=(),
        )
        _write_cleanup_artifacts(
            execution_root,
            cleanup_rows,
            sample_rows,
            profile_id=profile.profile_id,
            cleanup_started_ns=cleanup_started_ns,
            cleanup_complete=cleanup_complete,
            streams_closed=streams_closed,
            ports_clear=ports_clear,
            final_events_complete=final_events_complete,
            error=cleanup_error,
        )

    if runtime_error is not None:
        raise runtime_error
    if observation is None:
        raise N31LivenessShakedownError("live observer returned no evidence")
    merged = _merge_cleanup_observation(
        profile,
        execution_root,
        observation,
        cleanup_rows,
        sample_rows,
        cleanup_complete=cleanup_complete,
        streams_closed=streams_closed,
        ports_clear=ports_clear,
        final_events_complete=final_events_complete,
        expected_clean_exits=expected_clean_exits,
    )
    result = {
        "schema_version": 1,
        "profile_id": profile.profile_id,
        "source_slot_id": SOURCE_SLOT_ID,
        "pair_id": pair_id,
        "revision": receipt["revision"],
        "build_provenance_sha256": receipt["build_provenance_sha256"],
        "approval_receipt_sha256": receipt["approval_receipt_sha256"],
        "attempt": {
            "pair_id": pair_id,
            "ordinal": attempt_ordinal,
            "launch_count": 1,
            "retry_count": 0,
            "replacement_count": 0,
            "revision": receipt["revision"],
            "build_provenance_sha256": receipt["build_provenance_sha256"],
            "approval_receipt_sha256": receipt["approval_receipt_sha256"],
        },
        "integrity": {
            "profile_exact": True,
            "provenance_exact": True,
            "approval_exact": True,
            "raw_evidence_complete": final_events_complete,
            "seal_valid": True,
        },
        "execution": merged["execution"],
        "epoch2": observation["epoch2"],
        "liveness": merged["liveness"],
        "configuration_progress": observation["configuration_progress"],
    }
    if cleanup_error is not None:
        execution = dict(result["execution"])
        execution["cleanup_error"] = cleanup_error
        result["execution"] = execution
    return result


def _expected_clean_exit_authorizations(
    streams: Mapping[str, Sequence[Any]],
) -> dict[str, Mapping[str, object]]:
    try:
        terminal = _factorial._successful_manager_terminal(  # noqa: SLF001
            streams,
            cycle_ordinal=1,
        )
    except FactorialExecutionError as error:
        raise N31LivenessShakedownError(str(error)) from error
    if terminal is None:
        return {}
    reference = getattr(terminal, "reference", None)
    if not callable(reference):
        raise N31LivenessShakedownError(
            "manager terminal event cannot produce an exact authorization"
        )
    value = reference()
    if not isinstance(value, Mapping):
        raise N31LivenessShakedownError(
            "manager terminal authorization is malformed"
        )
    return {"adaptive-manager": dict(value)}


def _validate_epoch2_root_aggregation_digests(
    streams: Mapping[str, Sequence[Any]],
    expected_digest: str,
) -> None:
    for source, events in streams.items():
        if not source.startswith("replica-"):
            continue
        for event in events:
            if event.value.get("event_type") not in {
                "aggregation.root_quorum_progress",
                "aggregation.root_qc_published",
                QUEUE_BLOCKED_EVENT_TYPE,
            }:
                continue
            payload = _event_payload(event)
            if payload.get("epoch_number") == 2 and (
                payload.get("epoch_digest") != expected_digest
            ):
                raise N31LivenessShakedownError(
                    "Epoch2 root aggregation differs from activated digest"
                )


def _validate_cleanup_lifecycle_start(
    *,
    cleanup_started_monotonic_ns: object,
    observation_end_ns: int,
) -> int:
    if type(cleanup_started_monotonic_ns) is not int:
        raise N31LivenessShakedownError(
            "cleanup start clock is malformed"
        )
    if cleanup_started_monotonic_ns < observation_end_ns:
        raise N31LivenessShakedownError(
            "cleanup started before the observation ended"
        )
    return cleanup_started_monotonic_ns


def _validate_complete_live_capture(
    root: Path,
    profile: FrozenN31LivenessProfile,
    observation: Mapping[str, object],
) -> None:
    """Replay the decisive complete-verdict facts from sealed raw artifacts."""

    root = _live_execution_root(root)
    try:
        launch = _json_object((root / "launch.json").read_bytes(), "launch")
        cleanup = _json_object(
            (root / "cleanup-ledger.json").read_bytes(),
            "cleanup ledger",
        )
        samples = _json_object(
            (root / "raw/diagnostics/cleanup-samples.json").read_bytes(),
            "cleanup samples",
        )
    except OSError as error:
        raise N31LivenessShakedownError(
            f"complete verdict lacks live capture metadata: {error}"
        ) from error
    anchor_ns = _integer(launch.get("shared_raw_clock_anchor_ns"), minimum=1)
    epoch2 = _mapping(observation.get("epoch2"))
    liveness = _mapping(observation.get("liveness"))
    execution = _mapping(observation.get("execution"))
    configuration = _mapping(observation.get("configuration_progress"))
    if None in (anchor_ns, epoch2, liveness, execution, configuration):
        raise N31LivenessShakedownError("complete observation identity is malformed")
    assert anchor_ns is not None
    assert epoch2 is not None
    assert liveness is not None
    assert execution is not None
    assert configuration is not None
    first_ns = _integer(epoch2.get("first_common_commit_ns"), minimum=1)
    deadline_ns = _integer(epoch2.get("observation_deadline_ns"), minimum=1)
    end_ns = _integer(epoch2.get("observation_end_ns"), minimum=1)
    if (
        first_ns is None
        or deadline_ns is None
        or end_ns is None
        or first_ns < anchor_ns
        or deadline_ns
        != first_ns
        + int(profile.observation["duration_s"]) * NANOSECONDS_PER_SECOND
        or end_ns < deadline_ns
        or end_ns
        > anchor_ns
        + int(profile.timers["hard_timeout_s"]) * NANOSECONDS_PER_SECOND
        or launch.get("pair_id") != observation.get("pair_id")
    ):
        raise N31LivenessShakedownError(
            "observation window is not bound to the sealed launch anchor"
        )
    source = _source_slot_from_manifest(root / "source/manifest.json")
    try:
        if (
            (root / "source/manifest.json").read_bytes()
            != source.manifest_bytes
            or (root / "source/plan.json").read_bytes() != source.plan_bytes
            or (root / "source/runtime.json").read_bytes()
            != source.runtime_bytes
            or (root / "source/slot.json").read_bytes()
            != _canonical_json_bytes(source.slot.as_document(), newline=True)
            or (root / "source/slot-runtime.json").read_bytes()
            != _canonical_json_bytes(source.runtime.as_document(), newline=True)
        ):
            raise N31LivenessShakedownError(
                "sealed source documents differ from the derived frozen slot"
            )
    except OSError as error:
        raise N31LivenessShakedownError(
            f"sealed source documents are incomplete: {error}"
        ) from error
    try:
        streams = read_event_streams(
            source.runtime,
            root,
            allow_partial=False,
        )
    except FactorialExecutionError as error:
        raise N31LivenessShakedownError(
            f"sealed structured-event capture is invalid: {error}"
        ) from error
    if any(
        not events or any(event.timestamp_ns < anchor_ns for event in events)
        for events in streams.values()
    ):
        raise N31LivenessShakedownError(
            "structured-event streams are empty or predate the sealed launch"
        )
    selected, bundle_digest, initial_tree_id = _epoch2_bundle_identity(
        source.runtime, root, profile
    )
    activation = _epoch2_activation_progress(
        source.runtime,
        streams,
        expected_initial_tree_id=initial_tree_id,
    )
    if activation.get("epoch_digest") != bundle_digest:
        raise N31LivenessShakedownError(
            "unanimous Epoch2 activation differs from signed successor bundle"
        )
    _validate_epoch2_root_aggregation_digests(streams, bundle_digest)
    all_common = _common_epoch2_commits(
        source.runtime,
        streams,
        expected_epoch_digest=bundle_digest,
    )
    if not all_common or int(all_common[0]["common_monotonic_ns"]) != first_ns:
        raise N31LivenessShakedownError(
            "sealed raw events do not reproduce the first common Epoch2 commit"
        )
    window_common = [
        row
        for row in all_common
        if first_ns <= int(row["common_monotonic_ns"]) <= deadline_ns
    ]
    if (
        not window_common
        or int(window_common[-1]["common_monotonic_ns"])
        != epoch2.get("final_common_commit_ns")
    ):
        raise N31LivenessShakedownError(
            "sealed raw events do not reproduce final Q21 progress"
        )
    authoritative = _authoritative_epoch2_commits(
        source.runtime,
        streams,
        start_ns=first_ns,
        end_ns=deadline_ns,
        expected_epoch_digest=bundle_digest,
        selected_roots=selected,
    )
    if not authoritative:
        raise N31LivenessShakedownError(
            "sealed observer has no authoritative Epoch2 progress"
        )
    exercised: list[int] = []
    for row in authoritative:
        root_id = int(row["root_id"])
        if root_id not in exercised:
            exercised.append(root_id)
    authoritative_timestamps = [
        int(row["source_monotonic_ns"]) for row in authoritative
    ]
    maximum_gap = _max_gap_ns(
        first_ns,
        deadline_ns,
        authoritative_timestamps,
    )
    maximum_common_gap = _max_gap_ns(
        first_ns,
        deadline_ns,
        [int(row["common_monotonic_ns"]) for row in window_common],
    )
    start_offsets, end_offsets = _read_timeout_capture(
        source.runtime,
        root,
        profile_id=profile.profile_id,
        first_common_commit_ns=first_ns,
        observation_deadline_ns=deadline_ns,
    )
    timeout_rows = _read_leader_timeouts(
        source.runtime,
        root,
        start_offsets,
        end_offsets=end_offsets,
    )
    raw_stalls = _detect_head_of_line_stalls(
        profile,
        streams,
        start_ns=first_ns,
        end_ns=deadline_ns,
        selected_roots=selected,
        activated_epoch_digest=bundle_digest,
        leader_timeouts=timeout_rows,
        common_commits=window_common,
        max_commit_gap_ns=maximum_gap,
    )

    raw_samples = samples.get("attempts")
    cleanup_rows = cleanup.get("processes")
    if (
        set(cleanup)
        != {
            "schema_version",
            "profile_id",
            "cleanup_started_monotonic_ns",
            "cleanup_complete",
            "streams_closed",
            "ports_clear",
            "final_events_complete",
            "processes",
            "error",
        }
        or cleanup.get("schema_version") != 1
        or cleanup.get("profile_id") != profile.profile_id
        or cleanup.get("cleanup_complete") is not True
        or cleanup.get("streams_closed") is not True
        or cleanup.get("ports_clear") is not True
        or cleanup.get("final_events_complete") is not True
        or cleanup.get("error") is not None
        or not isinstance(cleanup_rows, list)
        or len(cleanup_rows) != source.runtime.replica_count + 1
        or set(samples) != {"schema_version", "profile_id", "attempts"}
        or samples.get("schema_version") != 1
        or samples.get("profile_id") != profile.profile_id
        or not isinstance(raw_samples, list)
    ):
        raise N31LivenessShakedownError(
            "complete cleanup/sample capture schema is invalid"
        )
    expected_names = {
        "adaptive-manager",
        *(f"replica-{replica_id}" for replica_id in range(source.runtime.replica_count)),
    }
    cleanup_start = _validate_cleanup_lifecycle_start(
        cleanup_started_monotonic_ns=cleanup.get(
            "cleanup_started_monotonic_ns"
        ),
        observation_end_ns=end_ns,
    )
    if (
        {
            str(row.get("name"))
            for row in cleanup_rows
            if isinstance(row, Mapping)
        }
        != expected_names
    ):
        raise N31LivenessShakedownError(
            "cleanup process cardinality/identity drifted"
        )
    cleanup_fields = {
        "name",
        "replica_id",
        "pid",
        "pgid",
        "cleanup_started_monotonic_ns",
        "signal_number",
        "returncode",
        "classification",
        "exit_authorization",
    }
    for row in cleanup_rows:
        if (
            not isinstance(row, Mapping)
            or set(row) != cleanup_fields
            or row.get("cleanup_started_monotonic_ns") != cleanup_start
        ):
            raise N31LivenessShakedownError(
                "cleanup process row schema/clock drifted"
            )
    sample_fields = {
        "name",
        "replica_id",
        "pid",
        "pgid",
        "after_signal_number",
        "before_signal_number",
        "status",
        "artifact_relative_path",
        "error",
    }
    for row in raw_samples:
        if not isinstance(row, Mapping) or set(row) != sample_fields:
            raise N31LivenessShakedownError(
                "cleanup sample row schema drifted"
            )
    expected_clean_exits = _expected_clean_exit_authorizations(streams)
    merged = _merge_cleanup_observation(
        profile,
        root,
        {
            "liveness": {
                "head_of_line_stalls": raw_stalls,
                "leader_progress_timeout_count": len(timeout_rows),
                "leader_progress_timeouts": timeout_rows,
                "q21_progress_continued": (
                    maximum_common_gap < _leader_timeout_ns(profile)
                    and int(window_common[-1]["common_monotonic_ns"])
                    >= deadline_ns - _leader_timeout_ns(profile)
                ),
                "max_authoritative_commit_gap_ns": maximum_gap,
                "max_common_q21_commit_gap_ns": maximum_common_gap,
            },
            "consensus_conflict_count": 0,
        },
        cleanup_rows,
        raw_samples,
        cleanup_complete=True,
        streams_closed=True,
        ports_clear=True,
        final_events_complete=True,
        expected_clean_exits=expected_clean_exits,
    )
    expected_epoch2 = {
        "first_common_commit_ns": first_ns,
        "observation_deadline_ns": deadline_ns,
        "observation_end_ns": end_ns,
        "final_common_commit_ns": int(
            window_common[-1]["common_monotonic_ns"]
        ),
        "selected_root_ids": list(selected),
        "exercised_root_ids": exercised,
        "activated_epoch_digest": bundle_digest,
    }
    expected_configuration = {
        "epoch2_activation": activation,
        "authoritative_commit_count": len(authoritative),
        "common_q21_commit_count": len(window_common),
        "first_block_height": min(
            int(row["block_height"]) for row in authoritative
        ),
        "last_block_height": max(
            int(row["block_height"]) for row in authoritative
        ),
        "epoch_digest_count": 1,
        "tree_ids": sorted(
            {int(row["tree_id"]) for row in authoritative}
        ),
        "authoritative_commits": authoritative,
        "common_q21_commits": window_common,
    }
    if (
        dict(epoch2) != expected_epoch2
        or dict(liveness) != merged["liveness"]
        or dict(execution) != merged["execution"]
        or dict(configuration) != expected_configuration
    ):
        raise N31LivenessShakedownError(
            "complete observation differs from independently replayed raw evidence"
        )


def _validate_attempt_launch_provenance(
    attempt_root: Path,
    profile: FrozenN31LivenessProfile,
    receipt: Mapping[str, object],
    approval_receipt: Mapping[str, object],
    *,
    pair_id: str,
    ordinal: int,
) -> bool:
    root = _live_execution_root(attempt_root)
    launch_path = root / "launch.json"
    provenance_path = root / "runtime/execution-provenance.json"
    launch_present = launch_path.is_file() and not launch_path.is_symlink()
    provenance_present = (
        provenance_path.is_file() and not provenance_path.is_symlink()
    )
    if launch_present != provenance_present:
        raise N31LivenessShakedownError(
            "launch and execution provenance are not preserved together"
        )
    if not launch_present:
        if launch_path.exists() or provenance_path.exists():
            raise N31LivenessShakedownError(
                "launch or execution provenance path is unsafe"
            )
        if any(
            path.exists()
            for path in (
                Path(attempt_root) / "launch.json",
                Path(attempt_root) / "runtime/execution-provenance.json",
            )
        ):
            raise N31LivenessShakedownError(
                "launch provenance is outside the slot-bound execution root"
            )
        return False
    try:
        launch_payload = launch_path.read_bytes()
        provenance_payload = provenance_path.read_bytes()
        launch = _json_object(launch_payload, "launch")
        provenance = _json_object(
            provenance_payload,
            "execution provenance",
        )
    except OSError as error:
        raise N31LivenessShakedownError(
            f"cannot read launch/execution provenance: {error}"
        ) from error
    if (
        launch_payload != _canonical_json_bytes(launch, newline=True)
        or provenance_payload
        != _canonical_json_bytes(provenance, newline=True)
    ):
        raise N31LivenessShakedownError(
            "launch or execution provenance is not canonical"
        )

    source = _source_slot()
    launch_fields = {
        "schema_version",
        "profile_id",
        "source_slot_id",
        "pair_id",
        "source_runtime_artifact_id",
        "attempt_ordinal",
        "launch_count",
        "retry_count",
        "replacement_count",
        "shared_raw_clock_anchor_ns",
        "manifest_sha256",
        "plan_sha256",
        "runtime_sha256",
        "redaction_key_id",
        "manager_argv",
        "replica_argv",
        "claim_eligible",
        "campaign_member",
        "denominator_contribution",
        "figure_eligible",
    }
    launch_required = {
        "schema_version": 1,
        "profile_id": profile.profile_id,
        "source_slot_id": SOURCE_SLOT_ID,
        "pair_id": pair_id,
        "source_runtime_artifact_id": source.runtime.artifact_id,
        "attempt_ordinal": ordinal,
        "launch_count": 1,
        "retry_count": 0,
        "replacement_count": 0,
        "manifest_sha256": profile.source["manifest_sha256"],
        "plan_sha256": profile.source["plan_sha256"],
        "runtime_sha256": _sha256_bytes(source.runtime_bytes),
        "claim_eligible": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }
    if (
        set(launch) != launch_fields
        or any(launch.get(key) != value for key, value in launch_required.items())
        or _integer(launch.get("shared_raw_clock_anchor_ns"), minimum=1) is None
        or not isinstance(launch.get("redaction_key_id"), str)
        or len(launch["redaction_key_id"]) != 16
        or any(character not in LOWER_HEX for character in launch["redaction_key_id"])
        or not isinstance(launch.get("manager_argv"), list)
        or not launch["manager_argv"]
        or not all(
            isinstance(value, str) and value
            for value in launch["manager_argv"]
        )
        or not isinstance(launch.get("replica_argv"), list)
        or len(launch["replica_argv"]) != FROZEN_REPLICA_COUNT
    ):
        raise N31LivenessShakedownError("sealed launch identity is invalid")
    replica_argv = launch["replica_argv"]
    assert isinstance(replica_argv, list)
    launched_replica_ids: set[int] = set()
    for row in replica_argv:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"replica_id", "argv"}
            or type(row.get("replica_id")) is not int
            or row["replica_id"] in launched_replica_ids
            or not isinstance(row.get("argv"), list)
            or not row["argv"]
            or not all(
                isinstance(value, str) and value for value in row["argv"]
            )
        ):
            raise N31LivenessShakedownError(
                "sealed launch replica argv schema is invalid"
            )
        launched_replica_ids.add(int(row["replica_id"]))
    if launched_replica_ids != set(range(FROZEN_REPLICA_COUNT)):
        raise N31LivenessShakedownError(
            "sealed launch replica argv membership is invalid"
        )

    provenance_fields = {
        "schema_version",
        "profile_id",
        "source_slot_id",
        "pair_id",
        "attempt_ordinal",
        "revision",
        "build_provenance_sha256",
        "approval_receipt_sha256",
        "binaries",
        "input_artifacts",
        "launch_sha256",
        "launch_count",
        "retry_count",
        "replacement_count",
        "claim_eligible",
        "campaign_member",
        "denominator_contribution",
        "figure_eligible",
    }
    provenance_required = {
        "schema_version": 1,
        "profile_id": profile.profile_id,
        "source_slot_id": SOURCE_SLOT_ID,
        "pair_id": pair_id,
        "attempt_ordinal": ordinal,
        "revision": receipt["revision"],
        "build_provenance_sha256": receipt["build_provenance_sha256"],
        "approval_receipt_sha256": receipt["approval_receipt_sha256"],
        "launch_sha256": _sha256_bytes(launch_payload),
        "launch_count": 1,
        "retry_count": 0,
        "replacement_count": 0,
        "claim_eligible": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }
    if (
        set(provenance) != provenance_fields
        or any(
            provenance.get(key) != value
            for key, value in provenance_required.items()
        )
        or not isinstance(provenance.get("binaries"), Mapping)
        or not isinstance(provenance.get("input_artifacts"), list)
    ):
        raise N31LivenessShakedownError(
            "sealed execution provenance identity is invalid"
        )
    binaries = provenance["binaries"]
    inputs = provenance["input_artifacts"]
    assert isinstance(binaries, Mapping)
    assert isinstance(inputs, list)
    expected_binary_names = {"app", "manager", "keygen", "tls_keygen"}
    if set(binaries) != expected_binary_names:
        raise N31LivenessShakedownError(
            "sealed execution provenance binary membership is invalid"
        )
    for name in expected_binary_names:
        row = binaries[name]
        if (
            not isinstance(row, Mapping)
            or set(row) != {"relative_path", "size_bytes", "sha256"}
            or not isinstance(row.get("relative_path"), str)
            or not row["relative_path"]
            or _integer(row.get("size_bytes"), minimum=1) is None
            or not _valid_sha256(row.get("sha256"))
        ):
            raise N31LivenessShakedownError(
                f"sealed execution provenance binary is invalid: {name}"
            )
    inputs_by_path: dict[str, Mapping[str, object]] = {}
    for row in inputs:
        if (
            not isinstance(row, Mapping)
            or set(row)
            != {"kind", "replica_id", "relative_path", "sha256", "size_bytes"}
            or not isinstance(row.get("kind"), str)
            or not row["kind"]
            or not isinstance(row.get("relative_path"), str)
            or not row["relative_path"]
            or row["relative_path"] in inputs_by_path
            or not _valid_sha256(row.get("sha256"))
            or _integer(row.get("size_bytes"), minimum=1) is None
        ):
            raise N31LivenessShakedownError(
                "sealed execution provenance input artifact is invalid"
            )
        inputs_by_path[str(row["relative_path"])] = row
    if not inputs_by_path:
        raise N31LivenessShakedownError(
            "sealed execution provenance input artifacts are empty"
        )

    replay_binaries = ExecutionBinaries(
        **{
            name: root / "exact-build/build-evidence/binaries" / name
            for name in expected_binary_names
        }
    )
    if any(
        binaries[name]["relative_path"] != str(path)
        for name, path in replay_binaries.as_mapping().items()
    ):
        raise N31LivenessShakedownError(
            "sealed execution provenance binary paths are invalid"
        )

    expected_input_paths = {
        "runtime/main.conf",
        "runtime/bls-identities.txt",
        "runtime/tls-identities.txt",
        "runtime/issuer-identities.txt",
        *(
            f"runtime/replica-{replica_id}.conf"
            for replica_id in range(FROZEN_REPLICA_COUNT)
        ),
    }
    if set(inputs_by_path) != expected_input_paths:
        raise N31LivenessShakedownError(
            "sealed execution input artifact membership is invalid"
        )
    for relative, row in inputs_by_path.items():
        path = root / relative
        try:
            path.resolve().relative_to(root)
            payload = path.read_bytes()
        except (OSError, ValueError) as error:
            raise N31LivenessShakedownError(
                f"sealed execution input artifact is unsafe: {error}"
            ) from error
        if (
            path.is_symlink()
            or not payload
            or row["sha256"] != _sha256_bytes(payload)
            or row["size_bytes"] != len(payload)
        ):
            raise N31LivenessShakedownError(
                f"sealed execution input artifact bytes drifted: {relative}"
            )
        if relative == "runtime/main.conf":
            expected_kind, expected_replica = "main_config", None
        elif relative.endswith("-identities.txt"):
            expected_kind = (
                relative.removeprefix("runtime/")
                .removesuffix("-identities.txt")
                + "_identity_input"
            )
            expected_replica = None
        else:
            match = re.fullmatch(r"runtime/replica-(\d+)\.conf", relative)
            if match is None:
                raise N31LivenessShakedownError(
                    "sealed execution input path is invalid"
                )
            expected_kind = "replica_config"
            expected_replica = int(match.group(1))
        if row["kind"] != expected_kind or row["replica_id"] != expected_replica:
            raise N31LivenessShakedownError(
                f"sealed execution input metadata drifted: {relative}"
            )

    exact_build_path = root / "runtime/exact-build-provenance.json"
    live_archive_present = (
        exact_build_path.is_file() and not exact_build_path.is_symlink()
    )
    if live_archive_present:
        try:
            replay_binaries = _preserved_binaries(root / "exact-build")
            expected_binaries = _binary_identities(replay_binaries)
        except OSError as error:
            raise N31LivenessShakedownError(
                f"cannot replay preserved execution binaries: {error}"
            ) from error
        if dict(binaries) != expected_binaries:
            raise N31LivenessShakedownError(
                "sealed execution binaries differ from preserved build evidence"
            )

    try:
        identities = _factorial.IdentityMaterial(
            bls=_factorial._parse_identity_output(  # noqa: SLF001
                (root / "runtime/bls-identities.txt").read_text(encoding="utf-8"),
                expected_count=source.runtime.replica_count,
                expected_fields=frozenset({"pub", "sec"}),
                label="sealed BLS identities",
            ),
            tls=_factorial._parse_identity_output(  # noqa: SLF001
                (root / "runtime/tls-identities.txt").read_text(encoding="utf-8"),
                expected_count=source.runtime.replica_count + 1,
                expected_fields=frozenset({"crt", "sec", "cid"}),
                label="sealed TLS identities",
            ),
            issuer=_factorial._parse_identity_output(  # noqa: SLF001
                (root / "runtime/issuer-identities.txt").read_text(
                    encoding="utf-8"
                ),
                expected_count=1,
                expected_fields=frozenset({"pub", "sec"}),
                label="sealed issuer identity",
            )[0],
        )
        if (
            (root / "runtime/main.conf").read_bytes()
            != _factorial._main_config_payload(  # noqa: SLF001
                source.slot,
                source.runtime,
                identities,
            )
            or any(
                (root / f"runtime/replica-{replica_id}.conf").read_bytes()
                != _factorial._replica_config_payload(  # noqa: SLF001
                    replica_id,
                    identities,
                )
                for replica_id in range(source.runtime.replica_count)
            )
        ):
            raise N31LivenessShakedownError(
                "sealed execution provenance input identity differs from exact configs"
            )
        materialized = materialize_launch(
            source.slot,
            source.runtime,
            slot_directory=root,
            binaries=replay_binaries,
            identities=identities,
            input_artifacts=inputs,
            shared_raw_clock_anchor_ns=int(launch["shared_raw_clock_anchor_ns"]),
            redaction_key=_factorial._derive_redaction_key(  # noqa: SLF001
                _canonical_json_bytes(approval_receipt),
                f"{SOURCE_SLOT_ID}:attempt-{ordinal}",
            ),
        )
    except (FactorialExecutionError, OSError, UnicodeError) as error:
        raise N31LivenessShakedownError(
            f"cannot replay sealed launch identity: {error}"
        ) from error
    if launch != _launch_document(
        source,
        materialized,
        profile_id=profile.profile_id,
        pair_id=pair_id,
        attempt_ordinal=ordinal,
        anchor_ns=int(launch["shared_raw_clock_anchor_ns"]),
    ):
        raise N31LivenessShakedownError(
            "sealed launch identity differs from exact frozen materialization"
        )
    return True


def _validate_attempt(
    attempt_directory: Path,
    *,
    approval_receipt: Mapping[str, object] | Path | None = None,
    approval_receipt_path: Path | None = None,
    expected_pair_id: str,
) -> dict[str, object]:
    """Validate one sealed attempt without launching or replacing anything."""

    root = Path(attempt_directory).resolve()
    ordinal = _attempt_ordinal(root)
    seal_sha256 = verify_evidence_seal(root)
    profile = load_frozen_profile(root / "profile.json")
    stored_approval = load_approval_receipt(root / "approval-receipt.json")
    external_value: Mapping[str, object] | Path
    if approval_receipt is not None and approval_receipt_path is not None:
        raise N31LivenessShakedownError(
            "validate accepts exactly one external approval receipt"
        )
    if approval_receipt is not None:
        external_value = approval_receipt
    elif approval_receipt_path is not None:
        external_value = approval_receipt_path
    else:
        raise N31LivenessShakedownError(
            "validate requires the external approval receipt"
        )
    external_approval = _coerce_approval_receipt(external_value)
    if _canonical_json_bytes(external_approval) != _canonical_json_bytes(
        stored_approval
    ):
        raise N31LivenessShakedownError(
            "external approval receipt differs from the sealed attempt"
        )
    try:
        preflight_receipt = _json_object(
            (root / "preflight-receipt.json").read_bytes(),
            "preflight receipt",
        )
        attempt = _json_object(
            (root / "attempt.json").read_bytes(),
            "attempt identity",
        )
        terminal = _json_object(
            (root / "terminal.json").read_bytes(),
            "terminal verdict",
        )
    except OSError as error:
        raise N31LivenessShakedownError(
            f"sealed attempt lacks required metadata: {error}"
        ) from error
    receipt = _validate_preflight_receipt(
        profile,
        preflight_receipt,
        external_approval,
    )
    expected_attempt = {
        "schema_version": 1,
        "profile_id": profile.profile_id,
        "source_slot_id": SOURCE_SLOT_ID,
        "pair_id": expected_pair_id,
        "attempt_ordinal": ordinal,
        "attempt_directory": root.name,
        "launch_count": 1,
        "retry_count": 0,
        "replacement_count": 0,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "claim_eligible": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }
    if attempt != expected_attempt:
        raise N31LivenessShakedownError("sealed attempt identity is invalid")
    observation_path = root / "observation.json"
    if observation_path.is_file() and not observation_path.is_symlink():
        observation = _json_object(observation_path.read_bytes(), "observation")
        _validate_observation_schema(profile, observation)
        observation_attempt = _mapping(observation.get("attempt"))
        expected_observation_attempt = {
            "pair_id": expected_pair_id,
            "ordinal": ordinal,
            "launch_count": 1,
            "retry_count": 0,
            "replacement_count": 0,
            "revision": receipt["revision"],
            "build_provenance_sha256": receipt[
                "build_provenance_sha256"
            ],
            "approval_receipt_sha256": receipt[
                "approval_receipt_sha256"
            ],
        }
        if (
            observation_attempt is None
            or dict(observation_attempt) != expected_observation_attempt
            or observation.get("pair_id") != expected_pair_id
            or observation.get("revision") != receipt["revision"]
            or observation.get("build_provenance_sha256")
            != receipt["build_provenance_sha256"]
            or observation.get("approval_receipt_sha256")
            != receipt["approval_receipt_sha256"]
        ):
            raise N31LivenessShakedownError(
                "sealed observation attempt identity is invalid"
            )
        verdict = evaluate_verdict(profile, observation)
    else:
        if observation_path.exists():
            raise N31LivenessShakedownError(
                "sealed observation path is unsafe"
            )
        observation = {}
        verdict = "INCOMPLETE"
    launch_preserved = _validate_attempt_launch_provenance(
        root,
        profile,
        receipt,
        stored_approval,
        pair_id=expected_pair_id,
        ordinal=ordinal,
    )
    execution_root = _live_execution_root(root)
    provenance_path = execution_root / "runtime/exact-build-provenance.json"
    live_archive_present = provenance_path.is_file() and not provenance_path.is_symlink()
    if live_archive_present and not launch_preserved and (
        verdict != "INCOMPLETE" or terminal.get("verdict") != "INCOMPLETE"
    ):
        raise N31LivenessShakedownError(
            "sealed live archive lacks launch/execution provenance"
        )
    if launch_preserved and not live_archive_present:
        raise N31LivenessShakedownError(
            "sealed launch/execution provenance lacks exact build evidence"
        )
    if live_archive_present:
        try:
            provenance = _json_object(
                provenance_path.read_bytes(),
                "exact build provenance",
            )
            if (
                _sha256_bytes(_canonical_json_bytes(provenance))
                != preflight_receipt.get("build_provenance_sha256")
            ):
                raise N31LivenessShakedownError(
                    "sealed build provenance differs from preflight"
                )
            _factorial.verify_preserved_build_evidence(
                execution_root / "exact-build",
                provenance,
            )
            if (
                _sha256_bytes(
                    (execution_root / "source/manifest.json").read_bytes()
                )
                != profile.source["manifest_sha256"]
                or _sha256_bytes(
                    (execution_root / "source/plan.json").read_bytes()
                )
                != profile.source["plan_sha256"]
            ):
                raise N31LivenessShakedownError(
                    "sealed source inputs differ from the frozen v13 binding"
                )
        except (FactorialExecutionError, OSError) as error:
            raise N31LivenessShakedownError(
                f"sealed live provenance cannot be verified: {error}"
            ) from error
    if verdict != "INCOMPLETE" and not live_archive_present:
        raise N31LivenessShakedownError(
            "complete diagnostic verdict lacks the exact build/source archive"
        )
    if verdict != "INCOMPLETE":
        _validate_complete_live_capture(root, profile, observation)
    terminal_verdict = terminal.get("verdict")
    terminal_reason = terminal.get("reason")
    expected_terminal = {
        "schema_version": 1,
        "profile_id": profile.profile_id,
        "source_slot_id": SOURCE_SLOT_ID,
        "pair_id": expected_pair_id,
        "attempt_ordinal": ordinal,
        "verdict": terminal_verdict,
        "reason": terminal_reason,
        "launch_count": 1,
        "retry_count": 0,
        "replacement_count": 0,
        "claim_eligible": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }
    if (
        terminal != expected_terminal
        or terminal_verdict not in VERDICTS
        or terminal_verdict != verdict
        or (
            terminal_verdict == "INCOMPLETE"
            and (
                not isinstance(terminal_reason, str)
                or not terminal_reason
            )
        )
        or (terminal_verdict != "INCOMPLETE" and terminal_reason is not None)
    ):
        raise N31LivenessShakedownError(
            "sealed terminal verdict differs from independent validation"
        )
    return {
        "schema_version": 1,
        "profile_id": profile.profile_id,
        "pair_id": expected_pair_id,
        "attempt_ordinal": ordinal,
        "verdict": verdict,
        "evidence_seal_sha256": seal_sha256,
        "claim_eligible": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }


def validate_pair(
    run_directory: Path,
    *,
    approval_receipt: Mapping[str, object] | Path | None = None,
    approval_receipt_path: Path | None = None,
) -> dict[str, object]:
    """Validate the sealed two-attempt unit and its fixed launch order."""

    root = Path(run_directory).resolve()
    root_seal_sha256 = verify_evidence_seal(root)
    attempts = _attempt_directories(root)
    if len(attempts) != 2:
        raise N31LivenessShakedownError(
            "sealed pair does not contain exactly two attempts"
        )
    expected_root_entries = {
        "pair-start.json",
        "pair-ledger.json",
        "evidence-seal.json",
        *(attempt.name for attempt in attempts),
    }
    if {path.name for path in root.iterdir()} != expected_root_entries:
        raise N31LivenessShakedownError("sealed pair root membership drifted")
    try:
        pair_start_payload = (root / "pair-start.json").read_bytes()
        pair_ledger_payload = (root / "pair-ledger.json").read_bytes()
        started = _json_object(pair_start_payload, "pair start")
        ledger = _json_object(pair_ledger_payload, "pair ledger")
    except OSError as error:
        raise N31LivenessShakedownError(
            f"sealed pair metadata is unavailable: {error}"
        ) from error
    if (
        pair_start_payload != _canonical_json_bytes(started, newline=True)
        or pair_ledger_payload != _canonical_json_bytes(ledger, newline=True)
    ):
        raise N31LivenessShakedownError(
            "sealed pair metadata is not canonical"
        )
    pair_id = _canonical_pair_uuid(str(started.get("pair_id")))
    profile = load_frozen_profile(attempts[0] / "profile.json")
    if any(
        (
            candidate.profile_id,
            candidate.profile_sha256,
        )
        != (profile.profile_id, profile.profile_sha256)
        for candidate in (
            load_frozen_profile(attempt / "profile.json")
            for attempt in attempts[1:]
        )
    ):
        raise N31LivenessShakedownError(
            "sealed pair contains mixed liveness profile releases"
        )
    intended_attempts = _validated_intended_attempts(
        started.get("intended_attempts")
    )
    shared = {
        "schema_version": 1,
        "profile_id": profile.profile_id,
        "profile_sha256": profile.profile_sha256,
        "source_slot_id": SOURCE_SLOT_ID,
        "pair_id": pair_id,
        "outcome_dependent_launch": False,
        "claim_eligible": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }
    started_required = {
        **shared,
        "expected_attempt_count": 2,
        "intended_attempts": list(intended_attempts),
    }
    ledger_required = {
        **shared,
        "attempt_count": 2,
        "pair_start_sha256": _sha256_bytes(pair_start_payload),
    }
    if any(started.get(key) != value for key, value in started_required.items()):
        raise N31LivenessShakedownError("sealed pair start identity drifted")
    if any(ledger.get(key) != value for key, value in ledger_required.items()):
        raise N31LivenessShakedownError("sealed pair ledger identity drifted")
    identity_fields = (
        "revision",
        "build_provenance_sha256",
        "approval_receipt_sha256",
    )
    if set(started) != {*started_required, *identity_fields}:
        raise N31LivenessShakedownError("sealed pair start schema drifted")
    if set(ledger) != {*ledger_required, *identity_fields, "attempts"}:
        raise N31LivenessShakedownError("sealed pair ledger schema drifted")
    if any(
        started.get(field) != ledger.get(field) for field in identity_fields
    ):
        raise N31LivenessShakedownError(
            "sealed pair provenance identity drifted"
        )
    rows = ledger.get("attempts")
    if not isinstance(rows, list) or len(rows) != 2:
        raise N31LivenessShakedownError("sealed pair attempt ledger is malformed")
    if approval_receipt is not None and approval_receipt_path is not None:
        raise N31LivenessShakedownError(
            "validate accepts exactly one external approval receipt"
        )
    external: Mapping[str, object] | Path | None = (
        approval_receipt
        if approval_receipt is not None
        else approval_receipt_path
    )
    if external is None:
        raise N31LivenessShakedownError(
            "validate requires the external approval receipt"
        )
    external_document = _coerce_approval_receipt(external)
    if (
        ledger.get("revision") != external_document.get("kauri_revision")
        or ledger.get("build_provenance_sha256")
        != external_document.get("build_provenance_sha256")
        or ledger.get("approval_receipt_sha256")
        != _sha256_bytes(_canonical_json_bytes(external_document))
    ):
        raise N31LivenessShakedownError(
            "sealed pair provenance differs from external approval"
        )
    validated: list[dict[str, object]] = []
    for ordinal, (attempt, raw_row, intended) in enumerate(
        zip(attempts, rows, intended_attempts, strict=True), 1
    ):
        if not isinstance(raw_row, Mapping):
            raise N31LivenessShakedownError(
                "sealed pair attempt row is malformed"
            )
        expected_row = {
            "ordinal": ordinal,
            "attempt_directory": intended["attempt_directory"],
            "verdict": raw_row.get("verdict"),
            "evidence_seal_sha256": raw_row.get("evidence_seal_sha256"),
        }
        if (
            attempt.name != intended["attempt_directory"]
            or dict(raw_row) != expected_row
        ):
            raise N31LivenessShakedownError(
                "sealed pair attempt row schema/order drifted"
            )
        result = _validate_attempt(
            attempt,
            approval_receipt=external,
            expected_pair_id=pair_id,
        )
        if (
            result["attempt_ordinal"] != ordinal
            or result["verdict"] != raw_row.get("verdict")
            or result["evidence_seal_sha256"]
            != raw_row.get("evidence_seal_sha256")
        ):
            raise N31LivenessShakedownError(
                "independent attempt validation differs from pair ledger"
            )
        validated.append(result)
    return {
        "schema_version": 1,
        "profile_id": profile.profile_id,
        "pair_id": pair_id,
        "pair_complete": all(
            row["verdict"] != "INCOMPLETE" for row in validated
        ),
        "attempts": validated,
        "evidence_seal_sha256": root_seal_sha256,
        "claim_eligible": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }


__all__ = (
    "APPROVAL_SCOPE",
    "DEFAULT_PROFILE_PATH",
    "FrozenN31LivenessProfile",
    "N31LivenessShakedownRelease",
    "N31LivenessShakedownError",
    "SHIPPED_RELEASES",
    "SHIPPED_PROFILE_SHA256",
    "VERDICTS",
    "allocate_attempt_directory",
    "build_approval_receipt",
    "create_evidence_seal",
    "evaluate_verdict",
    "load_approval_receipt",
    "load_frozen_profile",
    "preflight",
    "preflight_from_documents",
    "prepare_approval_receipt",
    "run_pair",
    "validate_pair",
    "verify_evidence_seal",
)
