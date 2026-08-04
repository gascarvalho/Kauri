"""Pure launch contracts for the frozen SHAPE25 factorial campaign.

This module does not predict adaptive outcomes and never starts a process.
It freezes only the inputs, live-evidence acceptance predicates, relative
timing, argv templates, and the result-independent execution order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import string
from typing import Any

from .factorial_manifest import (
    FactorialManifestError,
    FactorialPlan,
    FactorialSlot,
    ResponsivenessPolicyContract,
)

_NANOSECONDS_PER_SECOND = 1_000_000_000
_MAXIMUM_MONOTONIC_NS = (1 << 64) - 1
_EVIDENCE_WINDOW_RULE = "fresh_exact_predecessor_after_common_commit"
_SHAPE_SELECTOR_VERSION = "shape-v1"
_SHAPE_TIE_RULE = "lower-latency-risk-churn-current-canonical-v1"
_SHAPE_REFERENCE_TREE_RULE = "lowest-tree-id-prefix-q-v1"
_SLOT_DIRECTORY_TOKEN = "{{slot_directory}}"
_MANAGER_TLS_PRIVATE_KEY_TOKEN = "{{manager_tls_private_key_der_hex}}"
_MANAGER_TLS_CERTIFICATE_TOKEN = "{{manager_tls_certificate_der_hex}}"
_ISSUER_PRIVATE_KEY_TOKEN = "{{epoch_issuer_private_key_hex}}"
_REQUIRED_EVENTS = (
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


class _Document:
    def as_document(self) -> dict[str, object]:
        return dict(asdict(self))


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
        raise FactorialManifestError(
            "factorial runtime document is not canonical JSON"
        ) from error


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _seconds_from_milliseconds(milliseconds: int) -> str:
    whole, remainder = divmod(milliseconds, 1_000)
    if remainder == 0:
        return str(whole)
    return f"{whole}.{remainder:03d}".rstrip("0")


def _replica_certificate_token(replica_id: int) -> str:
    return f"{{{{replica_{replica_id}_tls_certificate_der_hex}}}}"


def _canonical_hex(value: object, label: str, *, exact_bytes: int = 0) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) % 2 != 0
        or any(character not in string.hexdigits for character in value)
        or value.lower() != value
        or (exact_bytes and len(value) != exact_bytes * 2)
    ):
        qualifier = f" with exactly {exact_bytes} bytes" if exact_bytes else ""
        raise FactorialManifestError(
            f"{label} must be nonempty canonical lowercase hexadecimal{qualifier}"
        )
    return value


def _absolute_slot_directory(value: str | Path, slot_id: str) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value:
        path = Path(value)
    else:
        raise FactorialManifestError("absolute slot directory is required")
    if not path.is_absolute() or ".." in path.parts or path.name != slot_id:
        raise FactorialManifestError(
            "slot directory must be an absolute canonical path ending in slot ID"
        )
    return path


@dataclass(frozen=True, slots=True)
class FaultWindowContract(_Document):
    clock: str
    bound_rule: str
    shared_anchor_per_slot: bool
    anchor_phase: str
    start_after_prelaunch_anchor_s: int
    duration_s: int
    transition_convergence_deadline_s: int
    schedule_slack_s: int
    drain_margin_s: int
    hard_timeout_s: int
    transition_observation_bound_rule: str


@dataclass(frozen=True, slots=True)
class CutoffContract(_Document):
    bucket_width_s: int
    baseline_bucket_count: int
    fault_evidence_bucket_count: int
    epoch1_stable_bucket_count: int
    epoch2_stable_bucket_count: int
    evidence_window_rule: str
    same_cutoff_rule_required: bool
    actual_cutoff_validation_rule: str
    actual_cutoffs_recorded_live: bool


@dataclass(frozen=True, slots=True)
class ShapeInvocationContract(_Document):
    selector_version: str
    tie_rule: str
    reference_tree_rule: str
    candidate_fanouts: tuple[int, ...]
    fixed_pipeline_stretch: int
    deterministic_seed: int
    compute_live: bool
    apply_selected_by_transition: tuple[bool, bool]

    def selector_input_document(self) -> str:
        """Return a comparable encoding that deliberately omits application."""

        return _canonical_json_bytes(
            {
                "candidate_fanouts": self.candidate_fanouts,
                "deterministic_seed": self.deterministic_seed,
                "fixed_pipeline_stretch": self.fixed_pipeline_stretch,
                "reference_tree_rule": self.reference_tree_rule,
                "selector_version": self.selector_version,
                "tie_rule": self.tie_rule,
            }
        ).decode("ascii")


@dataclass(frozen=True, slots=True)
class PlacementAcceptanceContract(_Document):
    policy_intent: str
    actors_are_wait_exempt_leaves: bool
    actor_truth_is_policy_input: bool
    roots_equal_live_highest_ranked_eligible: bool
    internal_assignment_uses_live_evidence_ranking: bool
    influential_order_source: str


@dataclass(frozen=True, slots=True)
class CausalAcceptanceContract(_Document):
    proof_source: str
    pre_epoch1_required_role: str
    pre_epoch1_required_action: str
    pre_epoch1_actor_coverage_rule: str
    post_containment_required_role: str
    post_containment_wait_exempt: bool
    post_containment_actor_coverage_rule: str
    declared_or_synthetic_outcomes_accepted: bool


@dataclass(frozen=True, slots=True)
class TransitionSequenceContract(_Document):
    required_events: tuple[str, ...]
    transition_count: int
    activation_delay_blocks: int
    total_activation_overhead_blocks: int


@dataclass(frozen=True, slots=True)
class TransitionRequestContract(_Document):
    policy_intent: str
    evidence_window_rule: str
    transition_artifact_id: str
    bundle_path: str
    evidence_snapshot_path: str
    predecessor_epoch_number: int
    successor_epoch_number: int
    minimum_predecessor_residency_ms: int
    minimum_post_baseline_observation_ms: int
    apply_shape_selection: bool
    containment_baseline_root_source: str
    containment_baseline_roots: tuple[tuple[int, int], ...]

    def as_native_document(self) -> dict[str, object]:
        parameters: dict[str, object]
        if self.policy_intent == "fault_containment":
            parameters = (
                {
                    "containment_baseline_roots": [
                        {"tree_id": tree_id, "replica_id": replica_id}
                        for tree_id, replica_id in self.containment_baseline_roots
                    ]
                }
                if self.containment_baseline_roots
                else {}
            )
        else:
            parameters = {}
        document: dict[str, object] = {
            "apply_shape_selection": self.apply_shape_selection,
            "bundle_path": self.bundle_path,
            "evidence_snapshot_path": self.evidence_snapshot_path,
            "evidence_window_rule": self.evidence_window_rule,
            "minimum_predecessor_residency_ms": (self.minimum_predecessor_residency_ms),
            "minimum_post_baseline_observation_ms": (
                self.minimum_post_baseline_observation_ms
            ),
            "policy_intent": self.policy_intent,
            "policy_parameters": parameters,
            "predecessor_epoch_number": self.predecessor_epoch_number,
            "successor_epoch_number": self.successor_epoch_number,
            "transition_artifact_id": self.transition_artifact_id,
        }
        if self.policy_intent == "fault_containment":
            document["containment_baseline_root_source"] = (
                self.containment_baseline_root_source
            )
        return document

    @property
    def canonical_json(self) -> str:
        return (
            _canonical_json_bytes(self.as_native_document())
            .decode("ascii")
            .rstrip("\n")
        )


@dataclass(frozen=True, slots=True)
class TransitionContract(_Document):
    artifact_id: str
    predecessor_epoch: int
    successor_epoch: int
    bundle_relative_path: str
    request: TransitionRequestContract


@dataclass(frozen=True, slots=True)
class ConfigContract(_Document):
    path: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplicaProcessSpec(_Document):
    replica_id: int
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagerArgvTemplate(_Document):
    argv: tuple[str, ...]
    slot_directory_token: str
    secret_tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagerSecretMaterial(_Document):
    manager_tls_private_key_der_hex: str
    manager_tls_certificate_der_hex: str
    issuer_private_key_hex: str
    replica_tls_certificate_der_hex: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StructuredEventContract(_Document):
    run_id: str
    manager_source_id: str
    manager_source_instance: str
    manager_output_relative_path: str
    replica_source_ids: tuple[str, ...]
    replica_source_instances: tuple[str, ...]
    replica_output_relative_paths: tuple[str, ...]
    commit_observer_id: str
    commit_observer_instance: str
    exclusive_output_per_process: bool


@dataclass(frozen=True, slots=True)
class ProcessLogContract(_Document):
    manager_stdout_relative_path: str
    manager_stderr_relative_path: str
    replica_stdout_relative_paths: tuple[str, ...]
    replica_stderr_relative_paths: tuple[str, ...]
    kauri_fault_marker_relative_paths: tuple[str, ...]
    exclusive_output_per_process: bool


@dataclass(frozen=True, slots=True)
class SmokeMetadata(_Document):
    campaign_member: bool
    figure_eligible: bool
    denominator_contribution: int
    launch_permitted: bool


@dataclass(frozen=True, slots=True)
class SlotRuntimeSpec(_Document):
    schema_version: int
    artifact_id: str
    slot_id: str
    block_id: str
    arm_code: str
    ordinal: int
    block_execution_ordinal: int
    arm_execution_position: int
    execution_ordinal: int
    scientific_seed: int
    replica_count: int
    f: int
    q: int
    tree_count: int
    initial_fanout: int
    candidate_fanouts: tuple[int, ...]
    pipeline_stretch: int
    actor_ids: tuple[int, ...]
    result_path: str
    responsiveness_policy: ResponsivenessPolicyContract
    fault_window: FaultWindowContract
    cutoff_contract: CutoffContract
    shape_invocation: ShapeInvocationContract
    causal_acceptance: CausalAcceptanceContract
    epoch1_placement: PlacementAcceptanceContract
    epoch2_placement: PlacementAcceptanceContract
    transition_sequence: TransitionSequenceContract
    transitions: tuple[TransitionContract, ...]
    main_config: ConfigContract
    structured_events: StructuredEventContract
    process_logs: ProcessLogContract
    manager_argv_template: ManagerArgvTemplate
    replica_argv_templates: tuple[ReplicaProcessSpec, ...]


@dataclass(frozen=True, slots=True)
class FactorialRuntimePlan(_Document):
    schema_version: int
    runtime_id: str
    manifest_id: str
    manifest_sha256: str
    source_plan_sha256: str
    execution_authorized: bool
    execution_receipt_required: bool
    execution_mode: str
    automatic_retries: int
    replacement_policy: str
    outcome_dependent_order: bool
    campaign_order_seed: int
    execution_block_order: str
    arm_counterbalancing: str
    preserve_outcomes: tuple[str, ...]
    results_root: str
    minimum_free_bytes: int
    minimum_free_bytes_interpretation: str
    smoke: SmokeMetadata
    slots: tuple[SlotRuntimeSpec, ...]

    @property
    def launch_permitted(self) -> bool:
        return self.execution_authorized and not self.execution_receipt_required

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        document.update(
            {
                "launch_permitted": self.launch_permitted,
                "slot_count": len(self.slots),
            }
        )
        return document

    @property
    def runtime_sha256(self) -> str:
        return hashlib.sha256(canonical_runtime_bytes(self)).hexdigest()

    def require_execution_authorized(self) -> None:
        if not self.launch_permitted:
            raise FactorialManifestError(
                "factorial execution is not authorized; a later sealed "
                "execution receipt is required"
            )


def build_smoke_metadata() -> SmokeMetadata:
    """Return metadata for a non-campaign, non-launching smoke check."""

    return SmokeMetadata(
        campaign_member=False,
        figure_eligible=False,
        denominator_contribution=0,
        launch_permitted=False,
    )


def _fault_window(slot: FactorialSlot) -> FaultWindowContract:
    return FaultWindowContract(
        clock="CLOCK_MONOTONIC_RAW",
        bound_rule="prelaunch_anchor_plus_offset_inclusive_start_exclusive_end",
        shared_anchor_per_slot=True,
        anchor_phase="sample_once_immediately_before_slot_launch",
        start_after_prelaunch_anchor_s=(slot.byzantine.start_after_prelaunch_anchor_s),
        duration_s=slot.byzantine.duration_s,
        transition_convergence_deadline_s=(
            slot.common_timers.transition_convergence_deadline_s
        ),
        schedule_slack_s=slot.common_timers.schedule_slack_s,
        drain_margin_s=slot.common_timers.drain_margin_s,
        hard_timeout_s=slot.common_timers.hard_timeout_s,
        transition_observation_bound_rule=(
            slot.common_timers.transition_observation_bound_rule
        ),
    )


def _cutoff_contract(slot: FactorialSlot) -> CutoffContract:
    workload = slot.workload
    return CutoffContract(
        bucket_width_s=workload.bucket_width_s,
        baseline_bucket_count=workload.baseline_bucket_count,
        fault_evidence_bucket_count=workload.fault_evidence_bucket_count,
        epoch1_stable_bucket_count=workload.epoch1_stable_bucket_count,
        epoch2_stable_bucket_count=workload.epoch2_stable_bucket_count,
        evidence_window_rule=_EVIDENCE_WINDOW_RULE,
        same_cutoff_rule_required=True,
        actual_cutoff_validation_rule="slot_local_monotonic_phase_order_v1",
        actual_cutoffs_recorded_live=True,
    )


def _shape_invocation(slot: FactorialSlot) -> ShapeInvocationContract:
    return ShapeInvocationContract(
        selector_version=_SHAPE_SELECTOR_VERSION,
        tie_rule=_SHAPE_TIE_RULE,
        reference_tree_rule=_SHAPE_REFERENCE_TREE_RULE,
        candidate_fanouts=slot.candidate_fanouts,
        fixed_pipeline_stretch=slot.pipeline_stretch,
        deterministic_seed=slot.scientific_seed,
        compute_live=True,
        apply_selected_by_transition=(False, slot.shape_adaptation),
    )


def _placement_contract(policy_intent: str) -> PlacementAcceptanceContract:
    optimized = policy_intent == "performance_optimization"
    return PlacementAcceptanceContract(
        policy_intent=policy_intent,
        actors_are_wait_exempt_leaves=True,
        actor_truth_is_policy_input=False,
        roots_equal_live_highest_ranked_eligible=optimized,
        internal_assignment_uses_live_evidence_ranking=True,
        influential_order_source="live_accepted_evidence_ranking",
    )


def _causal_acceptance() -> CausalAcceptanceContract:
    return CausalAcceptanceContract(
        proof_source="independent_raw_artifact_validation",
        pre_epoch1_required_role="internal",
        pre_epoch1_required_action="omit_aggregate",
        pre_epoch1_actor_coverage_rule="each_declared_actor_has_source_bound_marker",
        post_containment_required_role="leaf",
        post_containment_wait_exempt=True,
        post_containment_actor_coverage_rule="every_declared_actor",
        declared_or_synthetic_outcomes_accepted=False,
    )


def _transition_sequence(slot: FactorialSlot) -> TransitionSequenceContract:
    delay = slot.common_timers.activation_delay_blocks
    return TransitionSequenceContract(
        required_events=_REQUIRED_EVENTS,
        transition_count=2,
        activation_delay_blocks=delay,
        total_activation_overhead_blocks=2 * delay,
    )


def _transition(
    slot: FactorialSlot,
    *,
    predecessor: int,
    policy_intent: str,
) -> TransitionContract:
    successor = predecessor + 1
    artifact_id = f"{slot.slot_id}-epoch{successor}"
    relative_root = f"transitions/{artifact_id}"
    roots: tuple[tuple[int, int], ...] = ()
    root_source = (
        "live_predecessor_roots"
        if policy_intent == "fault_containment"
        else "not_applicable"
    )
    minimum_residency_ms = (
        0
        if predecessor == 0
        else (
            slot.workload.epoch1_stable_bucket_count
            * slot.workload.bucket_width_s
            * 1_000
        )
    )
    minimum_post_baseline_observation_ms = (
        (
            slot.byzantine.start_after_prelaunch_anchor_s
            + slot.workload.fault_evidence_bucket_count * slot.workload.bucket_width_s
        )
        * 1_000
        if predecessor == 0
        else 0
    )
    request = TransitionRequestContract(
        policy_intent=policy_intent,
        evidence_window_rule=_EVIDENCE_WINDOW_RULE,
        transition_artifact_id=artifact_id,
        bundle_path=f"{relative_root}/successor.bundle",
        evidence_snapshot_path=f"{relative_root}/evidence-snapshot.json",
        predecessor_epoch_number=predecessor,
        successor_epoch_number=successor,
        minimum_predecessor_residency_ms=minimum_residency_ms,
        minimum_post_baseline_observation_ms=(minimum_post_baseline_observation_ms),
        apply_shape_selection=(predecessor == 1 and slot.shape_adaptation),
        containment_baseline_root_source=root_source,
        containment_baseline_roots=roots,
    )
    return TransitionContract(
        artifact_id=artifact_id,
        predecessor_epoch=predecessor,
        successor_epoch=successor,
        bundle_relative_path=request.bundle_path,
        request=request,
    )


def _main_config(slot: FactorialSlot) -> ConfigContract:
    timers = slot.common_timers
    lines = (
        f"block-size = {slot.workload.block_size}",
        f"fan-out = {slot.initial_fanout}",
        f"piped_latency = {slot.workload.piped_latency_ms}",
        f"async_blocks = {slot.pipeline_stretch}",
        "epoch-protocol-mode = adaptive_v2",
        f"tree-switch-period = {slot.workload.tree_switch_period_blocks}",
        "aggregation-timeout = "
        f"{_seconds_from_milliseconds(timers.aggregation_timeout_ms)}",
        "leader-progress-timeout = "
        f"{_seconds_from_milliseconds(timers.leader_progress_timeout_ms)}",
        "leader-activation-grace = "
        f"{_seconds_from_milliseconds(timers.leader_activation_grace_ms)}",
        "epoch-change-minimum-activation-delay = " f"{timers.activation_delay_blocks}",
        "epoch-change-maximum-activation-delay = " f"{timers.activation_delay_blocks}",
        f"epoch-manager-address = 127.0.0.1:{slot.ports.manager}",
    )
    return ConfigContract(
        path="runtime/main.conf",
        lines=lines,
    )


def _structured_events(slot: FactorialSlot) -> StructuredEventContract:
    replica_source_ids = tuple(
        f"replica-{replica_id}" for replica_id in range(slot.replica_count)
    )
    replica_source_instances = tuple(
        f"{slot.slot_id}-{source_id}" for source_id in replica_source_ids
    )
    observer_id = replica_source_ids[0]
    return StructuredEventContract(
        run_id=slot.slot_id,
        manager_source_id="adaptive-manager",
        manager_source_instance=f"{slot.slot_id}-adaptive-manager",
        manager_output_relative_path="raw/adaptive-manager.jsonl",
        replica_source_ids=replica_source_ids,
        replica_source_instances=replica_source_instances,
        replica_output_relative_paths=tuple(
            f"raw/{source_id}.jsonl" for source_id in replica_source_ids
        ),
        commit_observer_id=observer_id,
        commit_observer_instance=replica_source_instances[0],
        exclusive_output_per_process=True,
    )


def _process_logs(slot: FactorialSlot) -> ProcessLogContract:
    replica_stdout = tuple(
        f"raw/process/replica-{replica_id}.stdout.log"
        for replica_id in range(slot.replica_count)
    )
    replica_stderr = tuple(
        f"raw/process/replica-{replica_id}.stderr.log"
        for replica_id in range(slot.replica_count)
    )
    return ProcessLogContract(
        manager_stdout_relative_path="raw/process/adaptive-manager.stdout.log",
        manager_stderr_relative_path="raw/process/adaptive-manager.stderr.log",
        replica_stdout_relative_paths=replica_stdout,
        replica_stderr_relative_paths=replica_stderr,
        kauri_fault_marker_relative_paths=tuple(
            path for pair in zip(replica_stdout, replica_stderr) for path in pair
        ),
        exclusive_output_per_process=True,
    )


def _manager_argv_template(
    slot: FactorialSlot,
    transitions: tuple[TransitionContract, ...],
    structured_events: StructuredEventContract,
) -> ManagerArgvTemplate:
    command: list[str] = [
        "adaptation-manager",
        "--listen",
        f"127.0.0.1:{slot.ports.manager}",
        "--tls-privkey",
        _MANAGER_TLS_PRIVATE_KEY_TOKEN,
        "--tls-cert",
        _MANAGER_TLS_CERTIFICATE_TOKEN,
        "--issuer-id",
        "1",
        "--issuer-private-key",
        _ISSUER_PRIVATE_KEY_TOKEN,
        "--activation-delay-blocks",
        str(slot.common_timers.activation_delay_blocks),
        "--convergence-deadline-seconds",
        str(slot.common_timers.transition_convergence_deadline_s),
        "--tree-fanout",
        str(slot.initial_fanout),
        "--pipeline-stretch",
        str(slot.pipeline_stretch),
        "--shape-candidate-fanouts",
        ",".join(map(str, slot.candidate_fanouts)),
        "--shape-deterministic-seed",
        str(slot.scientific_seed),
        "--responsiveness-policy-version",
        slot.responsiveness_policy.policy_version,
        "--required-nonresponsive",
        str(len(slot.byzantine_actor_ids)),
        "--responsiveness-attempt-window",
        str(slot.responsiveness_policy.attempt_window),
        "--responsiveness-minimum-attempts",
        str(slot.responsiveness_policy.minimum_attempts),
        "--responsiveness-minimum-response-rate-ppm",
        str(slot.responsiveness_policy.minimum_response_rate_ppm),
        "--responsiveness-maximum-timeout-rate-ppm",
        str(slot.responsiveness_policy.maximum_timeout_rate_ppm),
        "--responsiveness-trailing-timeout-streak",
        str(slot.responsiveness_policy.trailing_timeout_streak),
        "--responsiveness-latency-percentile-basis-points",
        str(slot.responsiveness_policy.latency_percentile_basis_points),
    ]
    for transition in transitions:
        command.extend(
            (
                "--transition-request",
                transition.request.canonical_json,
                "--bundle-output",
                f"{_SLOT_DIRECTORY_TOKEN}/{transition.bundle_relative_path}",
            )
        )
    command.extend(
        (
            "--structured-event-run-id",
            structured_events.run_id,
            "--structured-event-source-instance",
            structured_events.manager_source_instance,
            "--structured-event-output",
            f"{_SLOT_DIRECTORY_TOKEN}/"
            f"{structured_events.manager_output_relative_path}",
        )
    )
    for replica_id in range(slot.replica_count):
        command.extend(
            (
                "--replica",
                f"{replica_id},127.0.0.1:"
                f"{slot.ports.peer_base + replica_id},"
                f"{_replica_certificate_token(replica_id)}",
            )
        )
    return ManagerArgvTemplate(
        argv=tuple(command),
        slot_directory_token=_SLOT_DIRECTORY_TOKEN,
        secret_tokens=(
            _MANAGER_TLS_PRIVATE_KEY_TOKEN,
            _MANAGER_TLS_CERTIFICATE_TOKEN,
            _ISSUER_PRIVATE_KEY_TOKEN,
            *tuple(
                _replica_certificate_token(replica_id)
                for replica_id in range(slot.replica_count)
            ),
        ),
    )


def _replica_argv_templates(
    slot: FactorialSlot,
    main_config: ConfigContract,
    structured_events: StructuredEventContract,
) -> tuple[ReplicaProcessSpec, ...]:
    actors = ",".join(map(str, slot.byzantine_actor_ids))
    window_suffix = {
        "rotating_intermittent_omission_v1": "rotating-omission-v1",
        "persistent_selected_omission_v1": "persistent-omission-v1",
    }.get(slot.byzantine.mode)
    if window_suffix is None:
        raise FactorialManifestError("unknown Byzantine omission mode")
    window_id = f"{slot.block_id}-{window_suffix}"
    result: list[ReplicaProcessSpec] = []
    for replica_id in range(slot.replica_count):
        argv = (
            "hotstuff-app",
            "--conf",
            f"{_SLOT_DIRECTORY_TOKEN}/{main_config.path}",
            "--conf",
            f"{_SLOT_DIRECTORY_TOKEN}/runtime/replica-{replica_id}.conf",
            "--structured-event-run-id",
            structured_events.run_id,
            "--structured-event-source-instance",
            structured_events.replica_source_instances[replica_id],
            "--structured-event-output",
            f"{_SLOT_DIRECTORY_TOKEN}/"
            f"{structured_events.replica_output_relative_paths[replica_id]}",
            "--structured-event-commit-observer-id",
            structured_events.commit_observer_id,
            "--structured-event-commit-observer-instance",
            structured_events.commit_observer_instance,
            "--experiment-byzantine-mode",
            slot.byzantine.mode,
            "--experiment-byzantine-window",
            window_id,
            "--experiment-rotating-omission-actors",
            actors,
            "--experiment-byzantine-max-omissions-per-proposal",
            str(slot.byzantine.max_omissions_per_proposal),
            "--experiment-rotating-omission-context-limit",
            str(slot.byzantine.maximum_rotating_contexts),
        )
        result.append(ReplicaProcessSpec(replica_id=replica_id, argv=argv))
    return tuple(result)


def build_slot_runtime(slot: FactorialSlot) -> SlotRuntimeSpec:
    """Build one pure slot contract without sampling clocks or live evidence."""

    if not isinstance(slot, FactorialSlot):
        raise FactorialManifestError("slot runtime input must be a FactorialSlot")
    epoch1_policy = "fault_containment"
    epoch2_policy = (
        "performance_optimization" if slot.placement_adaptation else "fault_containment"
    )
    transitions = (
        _transition(slot, predecessor=0, policy_intent=epoch1_policy),
        _transition(slot, predecessor=1, policy_intent=epoch2_policy),
    )
    main_config = _main_config(slot)
    structured_events = _structured_events(slot)
    process_logs = _process_logs(slot)
    identity = {
        "arm_code": slot.arm_code,
        "block_id": slot.block_id,
        "scientific_seed": slot.scientific_seed,
        "slot_id": slot.slot_id,
        "slot_nonce": slot.slot_nonce,
    }
    return SlotRuntimeSpec(
        schema_version=1,
        artifact_id=f"slot-runtime-{_digest(identity)[:24]}",
        slot_id=slot.slot_id,
        block_id=slot.block_id,
        arm_code=slot.arm_code,
        ordinal=slot.ordinal,
        block_execution_ordinal=slot.block_execution_ordinal,
        arm_execution_position=slot.arm_execution_position,
        execution_ordinal=slot.execution_ordinal,
        scientific_seed=slot.scientific_seed,
        replica_count=slot.replica_count,
        f=slot.f,
        q=slot.q,
        tree_count=slot.tree_count,
        initial_fanout=slot.initial_fanout,
        candidate_fanouts=slot.candidate_fanouts,
        pipeline_stretch=slot.pipeline_stretch,
        actor_ids=slot.byzantine_actor_ids,
        result_path=slot.result_path,
        responsiveness_policy=slot.responsiveness_policy,
        fault_window=_fault_window(slot),
        cutoff_contract=_cutoff_contract(slot),
        shape_invocation=_shape_invocation(slot),
        causal_acceptance=_causal_acceptance(),
        epoch1_placement=_placement_contract(epoch1_policy),
        epoch2_placement=_placement_contract(epoch2_policy),
        transition_sequence=_transition_sequence(slot),
        transitions=transitions,
        main_config=main_config,
        structured_events=structured_events,
        process_logs=process_logs,
        manager_argv_template=_manager_argv_template(
            slot, transitions, structured_events
        ),
        replica_argv_templates=_replica_argv_templates(
            slot, main_config, structured_events
        ),
    )


def materialize_manager_argv(
    spec: SlotRuntimeSpec,
    absolute_slot_directory: str | Path,
    secrets: ManagerSecretMaterial,
) -> tuple[str, ...]:
    """Bind a manager template to an absolute slot root and supplied secrets."""

    if not isinstance(spec, SlotRuntimeSpec):
        raise FactorialManifestError(
            "manager argv materialization requires a SlotRuntimeSpec"
        )
    if not isinstance(secrets, ManagerSecretMaterial):
        raise FactorialManifestError(
            "manager argv materialization requires ManagerSecretMaterial"
        )
    slot_directory = _absolute_slot_directory(absolute_slot_directory, spec.slot_id)
    if len(secrets.replica_tls_certificate_der_hex) != spec.replica_count:
        raise FactorialManifestError(
            "manager secret material must cover the exact replica membership"
        )
    replacements = {
        _SLOT_DIRECTORY_TOKEN: str(slot_directory),
        _MANAGER_TLS_PRIVATE_KEY_TOKEN: _canonical_hex(
            secrets.manager_tls_private_key_der_hex,
            "manager TLS private key DER",
        ),
        _MANAGER_TLS_CERTIFICATE_TOKEN: _canonical_hex(
            secrets.manager_tls_certificate_der_hex,
            "manager TLS certificate DER",
        ),
        _ISSUER_PRIVATE_KEY_TOKEN: _canonical_hex(
            secrets.issuer_private_key_hex,
            "epoch issuer private key",
            exact_bytes=32,
        ),
    }
    for replica_id, certificate in enumerate(secrets.replica_tls_certificate_der_hex):
        replacements[_replica_certificate_token(replica_id)] = _canonical_hex(
            certificate,
            f"replica-{replica_id} TLS certificate DER",
        )

    materialized: list[str] = []
    for argument in spec.manager_argv_template.argv:
        value = argument
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        if "{{" in value or "}}" in value:
            raise FactorialManifestError(
                "manager argv contains an unresolved typed token"
            )
        materialized.append(value)

    result = tuple(materialized)
    bundle_outputs = tuple(
        result[index + 1]
        for index, argument in enumerate(result)
        if argument == "--bundle-output"
    )
    expected_outputs = tuple(
        str(slot_directory / transition.bundle_relative_path)
        for transition in spec.transitions
    )
    if bundle_outputs != expected_outputs or any(
        not Path(output).is_absolute() for output in bundle_outputs
    ):
        raise FactorialManifestError(
            "manager bundle outputs must be exact absolute slot paths"
        )
    request_documents = tuple(
        json.loads(result[index + 1])
        for index, argument in enumerate(result)
        if argument == "--transition-request"
    )
    if len(request_documents) != len(spec.transitions):
        raise FactorialManifestError(
            "manager transition requests must pair with every output"
        )
    for request, transition, bundle_output in zip(
        request_documents,
        spec.transitions,
        bundle_outputs,
    ):
        if (
            request.get("bundle_path") != transition.bundle_relative_path
            or request.get("evidence_snapshot_path")
            != transition.request.evidence_snapshot_path
            or bundle_output != str(slot_directory / transition.bundle_relative_path)
            or str(slot_directory / transition.request.evidence_snapshot_path)
            != str(Path(bundle_output).parent / "evidence-snapshot.json")
        ):
            raise FactorialManifestError(
                "manager relative transition paths do not map to exact "
                "absolute slot artifacts"
            )
    structured_output = result[result.index("--structured-event-output") + 1]
    if structured_output != str(
        slot_directory / spec.structured_events.manager_output_relative_path
    ):
        raise FactorialManifestError(
            "manager structured-event output is outside the slot contract"
        )
    return result


def materialize_replica_argv(
    spec: SlotRuntimeSpec,
    absolute_slot_directory: str | Path,
    shared_raw_clock_anchor_ns: int,
) -> tuple[ReplicaProcessSpec, ...]:
    """Bind replica paths and one launcher-sampled raw-clock anchor."""

    if not isinstance(spec, SlotRuntimeSpec):
        raise FactorialManifestError(
            "replica argv materialization requires a SlotRuntimeSpec"
        )
    slot_directory = _absolute_slot_directory(absolute_slot_directory, spec.slot_id)
    if (
        type(shared_raw_clock_anchor_ns) is not int
        or shared_raw_clock_anchor_ns < 0
        or shared_raw_clock_anchor_ns > _MAXIMUM_MONOTONIC_NS
    ):
        raise FactorialManifestError(
            "shared CLOCK_MONOTONIC_RAW anchor must fit uint64"
        )
    start_offset = (
        spec.fault_window.start_after_prelaunch_anchor_s * _NANOSECONDS_PER_SECOND
    )
    duration = spec.fault_window.duration_s * _NANOSECONDS_PER_SECOND
    start = shared_raw_clock_anchor_ns + start_offset
    end = start + duration
    if start > _MAXIMUM_MONOTONIC_NS or end > _MAXIMUM_MONOTONIC_NS:
        raise FactorialManifestError("materialized Byzantine window exceeds uint64")
    clock_arguments = (
        "--experiment-byzantine-window-start-monotonic-ns",
        str(start),
        "--experiment-byzantine-window-end-monotonic-ns",
        str(end),
    )
    materialized: list[ReplicaProcessSpec] = []
    for process in spec.replica_argv_templates:
        argv = tuple(
            argument.replace(_SLOT_DIRECTORY_TOKEN, str(slot_directory))
            for argument in process.argv
        )
        if any("{{" in argument or "}}" in argument for argument in argv):
            raise FactorialManifestError(
                "replica argv contains an unresolved typed token"
            )
        config_paths = tuple(
            argv[index + 1]
            for index, argument in enumerate(argv)
            if argument == "--conf"
        )
        expected_configs = (
            str(slot_directory / spec.main_config.path),
            str(slot_directory / f"runtime/replica-{process.replica_id}.conf"),
        )
        structured_output = argv[argv.index("--structured-event-output") + 1]
        expected_structured_output = str(
            slot_directory
            / spec.structured_events.replica_output_relative_paths[process.replica_id]
        )
        if (
            config_paths != expected_configs
            or structured_output != expected_structured_output
            or not all(Path(path).is_absolute() for path in config_paths)
            or not Path(structured_output).is_absolute()
        ):
            raise FactorialManifestError(
                "replica paths must be exact absolute slot descendants"
            )
        materialized.append(
            ReplicaProcessSpec(
                replica_id=process.replica_id,
                argv=(*argv, *clock_arguments),
            )
        )
    return tuple(materialized)


def build_factorial_runtime(plan: FactorialPlan) -> FactorialRuntimePlan:
    """Build all slots in the predeclared result-independent launch order."""

    if not isinstance(plan, FactorialPlan):
        raise FactorialManifestError("factorial runtime input must be a FactorialPlan")
    if (
        plan.execution_mode != "fixed_sequential"
        or plan.automatic_retries != 0
        or plan.replacement_policy != "none"
        or plan.outcome_dependent_order
        or plan.max_parallel_slots != 1
    ):
        raise FactorialManifestError(
            "runtime requires sequential, no-retry, no-replacement execution"
        )
    ordered = tuple(sorted(plan.slots, key=lambda slot: slot.execution_ordinal))
    if tuple(slot.execution_ordinal for slot in ordered) != tuple(
        range(1, len(ordered) + 1)
    ):
        raise FactorialManifestError("runtime execution ordinals must be contiguous")
    return FactorialRuntimePlan(
        schema_version=1,
        runtime_id=f"{plan.manifest_id}-runtime-v1",
        manifest_id=plan.manifest_id,
        manifest_sha256=plan.manifest_sha256,
        source_plan_sha256=plan.plan_sha256,
        execution_authorized=plan.execution_authorized,
        execution_receipt_required=plan.execution_receipt_required,
        execution_mode=plan.execution_mode,
        automatic_retries=plan.automatic_retries,
        replacement_policy=plan.replacement_policy,
        outcome_dependent_order=plan.outcome_dependent_order,
        campaign_order_seed=plan.campaign_order_seed,
        execution_block_order=plan.execution_block_order,
        arm_counterbalancing=plan.arm_counterbalancing,
        preserve_outcomes=plan.preserve_outcomes,
        results_root=plan.results_root,
        minimum_free_bytes=plan.minimum_free_bytes,
        minimum_free_bytes_interpretation=(plan.minimum_free_bytes_interpretation),
        smoke=build_smoke_metadata(),
        slots=tuple(build_slot_runtime(slot) for slot in ordered),
    )


def canonical_runtime_bytes(runtime: FactorialRuntimePlan) -> bytes:
    if not isinstance(runtime, FactorialRuntimePlan):
        raise FactorialManifestError(
            "canonical runtime input must be a FactorialRuntimePlan"
        )
    return _canonical_json_bytes(runtime.as_document())


def runtime_preflight(
    runtime: FactorialRuntimePlan,
    *,
    available_free_bytes: int,
) -> dict[str, Any]:
    """Validate the pure contract without authorizing or launching anything."""

    if not isinstance(runtime, FactorialRuntimePlan):
        raise FactorialManifestError(
            "runtime preflight input must be a FactorialRuntimePlan"
        )
    if type(available_free_bytes) is not int or available_free_bytes < 0:
        raise FactorialManifestError(
            "available result-volume bytes must be a nonnegative integer"
        )
    if available_free_bytes < runtime.minimum_free_bytes:
        raise FactorialManifestError(
            "result volume does not meet the frozen minimum-free-bytes threshold"
        )
    if len({slot.artifact_id for slot in runtime.slots}) != len(runtime.slots):
        raise FactorialManifestError("slot runtime artifact IDs are not unique")
    for slot in runtime.slots:
        manager_template = slot.manager_argv_template.argv
        policy = slot.responsiveness_policy
        expected_residencies_ms = (
            0,
            slot.cutoff_contract.epoch1_stable_bucket_count
            * slot.cutoff_contract.bucket_width_s
            * 1_000,
        )
        expected_post_baseline_observation_ms = (
            (
                slot.fault_window.start_after_prelaunch_anchor_s
                + slot.cutoff_contract.fault_evidence_bucket_count
                * slot.cutoff_contract.bucket_width_s
            )
            * 1_000,
            0,
        )
        if (
            slot.transition_sequence.transition_count != 2
            or len(slot.transitions) != 2
            or tuple(
                transition.request.minimum_predecessor_residency_ms
                for transition in slot.transitions
            )
            != expected_residencies_ms
            or tuple(
                transition.request.minimum_post_baseline_observation_ms
                for transition in slot.transitions
            )
            != expected_post_baseline_observation_ms
            or manager_template.count("--transition-request") != 2
            or manager_template.count("--bundle-output") != 2
            or manager_template.count("--required-nonresponsive") != 1
            or manager_template[manager_template.index("--required-nonresponsive") + 1]
            != str(len(slot.actor_ids))
            or "--shape-adaptation-enabled" in manager_template
            or tuple(
                transition.request.apply_shape_selection
                for transition in slot.transitions
            )
            != slot.shape_invocation.apply_selected_by_transition
        ):
            raise FactorialManifestError(
                f"slot has an invalid transition contract: {slot.slot_id}"
            )
        if (
            policy.minimum_attempts > policy.attempt_window
            or not 0 <= policy.minimum_response_rate_ppm <= 1_000_000
            or not 0 <= policy.maximum_timeout_rate_ppm <= 1_000_000
            or not 2 <= policy.trailing_timeout_streak <= policy.attempt_window
            or not 1 <= policy.latency_percentile_basis_points <= 10_000
            or 1_000_000 // len(slot.actor_ids) <= policy.maximum_timeout_rate_ppm
        ):
            raise FactorialManifestError(
                f"slot has an infeasible responsiveness policy: {slot.slot_id}"
            )
        minimum_fault_duration_s = (
            slot.cutoff_contract.fault_evidence_bucket_count
            * slot.cutoff_contract.bucket_width_s
            + slot.transition_sequence.transition_count
            * slot.fault_window.transition_convergence_deadline_s
            + slot.cutoff_contract.epoch1_stable_bucket_count
            * slot.cutoff_contract.bucket_width_s
            + slot.cutoff_contract.epoch2_stable_bucket_count
            * slot.cutoff_contract.bucket_width_s
            + slot.fault_window.drain_margin_s
            + slot.fault_window.schedule_slack_s
        )
        if (
            slot.fault_window.duration_s < minimum_fault_duration_s
            or slot.fault_window.schedule_slack_s < 30
            or slot.fault_window.hard_timeout_s
            < slot.fault_window.start_after_prelaunch_anchor_s
            + slot.fault_window.duration_s
            + slot.fault_window.drain_margin_s
        ):
            raise FactorialManifestError(
                f"slot fault window cannot cover both transitions: {slot.slot_id}"
            )
        event_contract = slot.structured_events
        event_paths = (
            event_contract.manager_output_relative_path,
            *event_contract.replica_output_relative_paths,
        )
        event_ids = (
            event_contract.manager_source_id,
            *event_contract.replica_source_ids,
        )
        event_instances = (
            event_contract.manager_source_instance,
            *event_contract.replica_source_instances,
        )
        if (
            manager_template.count("--structured-event-output") != 1
            or manager_template.count("--structured-event-source-instance") != 1
            or any(
                process.argv.count("--structured-event-output") != 1
                or process.argv.count("--structured-event-source-instance") != 1
                for process in slot.replica_argv_templates
            )
            or not event_contract.exclusive_output_per_process
            or len(event_contract.replica_source_ids) != slot.replica_count
            or len(event_contract.replica_source_instances) != slot.replica_count
            or len(event_contract.replica_output_relative_paths) != slot.replica_count
            or len(set(event_contract.replica_source_ids)) != slot.replica_count
            or len(set(event_ids)) != slot.replica_count + 1
            or len(set(event_instances)) != slot.replica_count + 1
            or len(set(event_paths)) != slot.replica_count + 1
            or event_contract.commit_observer_id != event_contract.replica_source_ids[0]
            or event_contract.commit_observer_instance
            != event_contract.replica_source_instances[0]
            or manager_template[manager_template.index("--structured-event-output") + 1]
            != (
                f"{_SLOT_DIRECTORY_TOKEN}/"
                f"{event_contract.manager_output_relative_path}"
            )
            or manager_template[
                manager_template.index("--structured-event-source-instance") + 1
            ]
            != event_contract.manager_source_instance
            or any(
                process.argv[process.argv.index("--structured-event-output") + 1]
                != (
                    f"{_SLOT_DIRECTORY_TOKEN}/"
                    f"{event_contract.replica_output_relative_paths[process.replica_id]}"
                )
                or process.argv[
                    process.argv.index("--structured-event-source-instance") + 1
                ]
                != event_contract.replica_source_instances[process.replica_id]
                for process in slot.replica_argv_templates
            )
        ):
            raise FactorialManifestError(
                f"slot lacks exact structured-event outputs: {slot.slot_id}"
            )
        process_logs = slot.process_logs
        replica_log_paths = (
            process_logs.replica_stdout_relative_paths
            + slot.process_logs.replica_stderr_relative_paths
        )
        all_log_paths = (
            process_logs.manager_stdout_relative_path,
            process_logs.manager_stderr_relative_path,
            *replica_log_paths,
        )
        if (
            not process_logs.exclusive_output_per_process
            or len(process_logs.replica_stdout_relative_paths) != slot.replica_count
            or len(process_logs.replica_stderr_relative_paths) != slot.replica_count
            or len(replica_log_paths) != 2 * slot.replica_count
            or len(set(all_log_paths)) != len(all_log_paths)
            or set(all_log_paths).intersection(event_paths)
            or process_logs.kauri_fault_marker_relative_paths
            != tuple(
                path
                for pair in zip(
                    process_logs.replica_stdout_relative_paths,
                    process_logs.replica_stderr_relative_paths,
                )
                for path in pair
            )
        ):
            raise FactorialManifestError(
                f"slot lacks complete KAURI_FAULT log inputs: {slot.slot_id}"
            )
        if any(
            "window-start-monotonic-ns" in argument
            or "window-end-monotonic-ns" in argument
            for process in slot.replica_argv_templates
            for argument in process.argv
        ):
            raise FactorialManifestError(
                "canonical replica argv templates contain absolute clock values"
            )
        if slot.causal_acceptance != _causal_acceptance():
            raise FactorialManifestError(
                f"slot lacks the frozen raw causal acceptance gate: {slot.slot_id}"
            )
    return {
        "available_free_bytes": available_free_bytes,
        "automatic_retries": runtime.automatic_retries,
        "execution_authorized": runtime.execution_authorized,
        "execution_receipt_required": runtime.execution_receipt_required,
        "launch_permitted": runtime.launch_permitted,
        "minimum_free_bytes": runtime.minimum_free_bytes,
        "replacement_policy": runtime.replacement_policy,
        "runtime_id": runtime.runtime_id,
        "runtime_sha256": runtime.runtime_sha256,
        "slot_count": len(runtime.slots),
        "status": "PASS",
    }


__all__ = (
    "ConfigContract",
    "CausalAcceptanceContract",
    "CutoffContract",
    "FactorialRuntimePlan",
    "FaultWindowContract",
    "ManagerArgvTemplate",
    "ManagerSecretMaterial",
    "PlacementAcceptanceContract",
    "ProcessLogContract",
    "ReplicaProcessSpec",
    "ShapeInvocationContract",
    "SlotRuntimeSpec",
    "SmokeMetadata",
    "StructuredEventContract",
    "TransitionContract",
    "TransitionRequestContract",
    "TransitionSequenceContract",
    "build_factorial_runtime",
    "build_slot_runtime",
    "build_smoke_metadata",
    "canonical_runtime_bytes",
    "materialize_manager_argv",
    "materialize_replica_argv",
    "runtime_preflight",
)
