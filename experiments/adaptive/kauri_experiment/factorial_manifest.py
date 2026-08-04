"""Frozen planning/preflight contract for the SHAPE25 factorial."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any

LEGACY_MANIFEST_ID = "shape-placement-factorial-v1"
LEGACY_MANIFEST_SHA256 = (
    "58ebd0cc8ceb5ae4dc5981e9475747ea4b2a5b63fe334bfa6a44f552b8495542"
)
LEGACY_SEMANTIC_SHA256 = (
    "3ca6665ec108c8547b8d483ca509a25ae93be037595e9059276422c28cf77687"
)
LEGACY_PLAN_SHA256 = "e5e1acf5228446795810e8f2ad0ef2f9af25203a2a213a2a427b218f19907fb1"

FROZEN_MANIFEST_ID = "shape-placement-factorial-v2"
FROZEN_MANIFEST_SHA256 = (
    "ef1133f2d3b5204bdb8fd4be5ebd4990801b77e2aafa3b6a03cc5c262a40a8fd"
)
FROZEN_SEMANTIC_SHA256 = (
    "5d10ff2498fb265b19c3990df87e06db212687d9a55585b37545b4dbeb2f96c9"
)
FROZEN_PLAN_SHA256 = "b17fb3fa654d44080ad52e01b24aeed8c38726e33149229d0f6e540dc7f97516"
EXPECTED_REPLICA_COUNTS = (13, 22, 31)
EXPECTED_INITIAL_FANOUTS = (2, 3, 5)
EXPECTED_CANDIDATE_FANOUTS = (2, 3, 5)
EXPECTED_ARM_CODES = ("00", "P", "S", "PS")
EXPECTED_BLOCK_COUNT = 17
EXPECTED_SLOT_COUNT = 68

_FORBIDDEN_AUTHORITY_FIELDS = frozenset(
    {"f", "Q", "fault_threshold", "quorum", "tree_count"}
)


class FactorialManifestError(ValueError):
    """The manifest or derived plan violates the frozen SHAPE25 contract."""


class _Document:
    def as_document(self) -> dict[str, object]:
        return dict(asdict(self))


@dataclass(frozen=True, slots=True)
class FactorialArm(_Document):
    code: str
    placement_adaptation: bool
    shape_adaptation: bool


@dataclass(frozen=True, slots=True)
class ByzantineActions(_Document):
    root: str
    internal: str
    leaf: str


@dataclass(frozen=True, slots=True)
class ActorRotationVector(_Document):
    epoch_number: int
    tree_id: int
    epoch_digest: str
    block_hash: str
    sorted_actor_ids: tuple[int, ...]
    fnv1a64: int
    selected_actor: int


@dataclass(frozen=True, slots=True)
class ActorSelectionVector(_Document):
    replica_count: int
    q: int
    scientific_seed: int
    selected_actor_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ByzantineContract(_Document):
    mode: str
    actor_count: int
    actor_count_rule: str
    actor_selection: str
    actor_selection_preimage: str
    actor_selection_inputs: tuple[str, ...]
    actor_selection_vectors: tuple[ActorSelectionVector, ...]
    actor_schedule: str
    actor_rotation_vectors: tuple[ActorRotationVector, ...]
    maximum_rotating_contexts: int
    start_after_prelaunch_anchor_s: int
    duration_s: int
    max_omissions_per_proposal: int
    actions: ByzantineActions


@dataclass(frozen=True, slots=True)
class WorkloadContract(_Document):
    block_size: int
    piped_latency_ms: int
    tree_switch_period_blocks: int
    bucket_width_s: int
    baseline_bucket_count: int
    fault_evidence_bucket_count: int
    epoch1_stable_bucket_count: int
    epoch2_stable_bucket_count: int


@dataclass(frozen=True, slots=True)
class ResponsivenessPolicyContract(_Document):
    policy_version: str
    attempt_window: int
    minimum_attempts: int
    minimum_response_rate_ppm: int
    maximum_timeout_rate_ppm: int
    trailing_timeout_streak: int
    latency_percentile_basis_points: int


@dataclass(frozen=True, slots=True)
class CommonTimers(_Document):
    depth_policy: str
    global_worst_candidate_depth: int
    aggregation_timeout_ms_per_depth: int
    leader_progress_timeout_ms_per_depth: int
    leader_activation_grace_ms: int
    activation_delay_blocks: int
    transition_convergence_deadline_s: int
    schedule_slack_s: int
    drain_margin_s: int
    startup_timeout_s: int
    hard_timeout_s: int
    transition_observation_bound_rule: str = "phase_deadline_v1"

    @property
    def aggregation_timeout_ms(self) -> int:
        return self.aggregation_timeout_ms_per_depth * self.global_worst_candidate_depth

    @property
    def leader_progress_timeout_ms(self) -> int:
        return (
            self.leader_progress_timeout_ms_per_depth
            * self.global_worst_candidate_depth
        )


@dataclass(frozen=True, slots=True)
class ResourceContract(_Document):
    minimum_free_bytes: int
    minimum_free_bytes_interpretation: str
    max_parallel_slots: int
    peer_port_base: int
    client_port_base: int
    manager_port_base: int
    slot_port_stride: int


@dataclass(frozen=True, slots=True)
class ClaimScope(_Document):
    other_cells: str
    causal_inference: str
    headline_estimator: str
    uncertainty_interval: str
    directional_claim_rule: str
    phase_sequence_endpoint: str
    placement_headline_replica_count: int
    placement_headline_initial_fanout: int
    placement_headline_block_count: int
    shape_and_joint_headline_replica_count: int
    shape_and_joint_headline_initial_fanout: int
    shape_and_joint_headline_block_count: int


@dataclass(frozen=True, slots=True)
class BlockExecutionSchedule(_Document):
    block_id: str
    block_execution_ordinal: int
    arm_order: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortAllocation(_Document):
    peer_base: int
    client_base: int
    manager: int


@dataclass(frozen=True, slots=True)
class ConsensusShape(_Document):
    replica_count: int
    f: int
    q: int
    tree_count: int
    initial_fanout: int
    initial_depth: int
    candidate_depths: tuple[tuple[int, int], ...]
    worst_candidate_depth: int


@dataclass(frozen=True, slots=True)
class FrozenFactorialManifest:
    manifest_id: str
    manifest_sha256: str
    replica_counts: tuple[int, ...]
    initial_fanouts: tuple[int, ...]
    candidate_fanouts: tuple[int, ...]
    pipeline_stretch: int
    epoch_fanout_policy: str
    pipeline_policy: str
    arms: tuple[FactorialArm, ...]
    default_blocks_per_cell: int
    repetition_overrides: tuple[tuple[int, int, int], ...]
    byzantine: ByzantineContract
    workload: WorkloadContract
    responsiveness_policy: ResponsivenessPolicyContract
    common_timers: CommonTimers
    scientific_seed_base: int
    scientific_seed_rule: str
    slot_order: str
    slot_nonce_rule: str
    campaign_order_seed: int
    execution_block_order: str
    arm_counterbalancing: str
    scheduling_outcome_dependent_order: bool
    claim_scope: ClaimScope
    results_root: str
    canonical_plan_filename: str
    one_directory_per_slot: bool
    preserve_outcomes: tuple[str, ...]
    resources: ResourceContract
    execution_mode: str
    automatic_retries: int
    replacement_policy: str
    execution_outcome_dependent_order: bool
    execution_authorized: bool
    execution_receipt_required: bool

    def blocks_for(self, replica_count: int, initial_fanout: int) -> int:
        for override_n, override_fanout, blocks in self.repetition_overrides:
            if (replica_count, initial_fanout) == (override_n, override_fanout):
                return blocks
        return self.default_blocks_per_cell


@dataclass(frozen=True, slots=True)
class FactorialSlot(_Document):
    ordinal: int
    slot_nonce: int
    slot_id: str
    block_id: str
    block_index: int
    blocks_in_cell: int
    block_execution_ordinal: int
    arm_execution_position: int
    execution_ordinal: int
    scientific_seed: int
    consensus: ConsensusShape
    candidate_fanouts: tuple[int, ...]
    pipeline_stretch: int
    epoch_fanout_policy: str
    pipeline_policy: str
    arm: FactorialArm
    byzantine: ByzantineContract
    byzantine_actor_ids: tuple[int, ...]
    workload: WorkloadContract
    responsiveness_policy: ResponsivenessPolicyContract
    common_timers: CommonTimers
    ports: PortAllocation
    result_path: str

    @property
    def replica_count(self) -> int:
        return self.consensus.replica_count

    @property
    def f(self) -> int:
        return self.consensus.f

    @property
    def q(self) -> int:
        return self.consensus.q

    @property
    def tree_count(self) -> int:
        return self.consensus.tree_count

    @property
    def initial_fanout(self) -> int:
        return self.consensus.initial_fanout

    @property
    def initial_depth(self) -> int:
        return self.consensus.initial_depth

    @property
    def candidate_depths(self) -> tuple[tuple[int, int], ...]:
        return self.consensus.candidate_depths

    @property
    def worst_candidate_depth(self) -> int:
        return self.consensus.worst_candidate_depth

    @property
    def arm_code(self) -> str:
        return self.arm.code

    @property
    def placement_adaptation(self) -> bool:
        return self.arm.placement_adaptation

    @property
    def shape_adaptation(self) -> bool:
        return self.arm.shape_adaptation

    @property
    def seed(self) -> int:
        return self.scientific_seed


@dataclass(frozen=True, slots=True)
class FactorialPlan(_Document):
    manifest_id: str
    manifest_sha256: str
    execution_authorized: bool
    execution_receipt_required: bool
    execution_mode: str
    automatic_retries: int
    replacement_policy: str
    outcome_dependent_order: bool
    campaign_order_seed: int
    execution_block_order: str
    arm_counterbalancing: str
    execution_schedule: tuple[BlockExecutionSchedule, ...]
    claim_scope: ClaimScope
    preserve_outcomes: tuple[str, ...]
    results_root: str
    canonical_plan_filename: str
    minimum_free_bytes: int
    minimum_free_bytes_interpretation: str
    max_parallel_slots: int
    global_worst_candidate_depth: int
    slots: tuple[FactorialSlot, ...]

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        document.update(
            {
                "plan_id": f"{self.manifest_id}-plan-v1",
                "schema_version": 1,
                "slot_count": len(self.slots),
            }
        )
        return document

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_plan_bytes(self)

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    def require_execution_authorized(self) -> None:
        if not self.execution_authorized or self.execution_receipt_required:
            _error(
                "factorial execution is not authorized; a later sealed "
                "execution receipt is required"
            )


def _error(message: str) -> None:
    raise FactorialManifestError(message)


def _duplicate_rejecting_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _error(f"JSON document contains duplicate key: {key}")
        result[key] = value
    return result


def _parse_json(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=lambda constant: _error(
                f"manifest contains non-finite constant: {constant}"
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FactorialManifestError("manifest is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        _error("manifest must be a JSON object")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error(f"{label} must be a JSON object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        _error(f"{label} must be a JSON array")
    return value


def _integer(value: object, label: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        _error(f"{label} must be an integer >= {minimum}")
    return value


def _fanouts(value: object, label: str) -> tuple[int, ...]:
    fanouts = tuple(
        _integer(item, f"{label}[{index}]")
        for index, item in enumerate(_array(value, label))
    )
    noun = label[:-1] if label.endswith("s") else label
    if any(fanout > 255 for fanout in fanouts):
        _error(f"{noun} must be 1..255")
    if len(set(fanouts)) != len(fanouts):
        _error(f"{noun} values must be unique")
    return fanouts


def _reject_authority_fields(value: object, label: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in _FORBIDDEN_AUTHORITY_FIELDS:
                _error(f"{label}.{key} is derived from N and cannot be supplied")
            _reject_authority_fields(nested, f"{label}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_authority_fields(nested, f"{label}[{index}]")


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
        raise FactorialManifestError("document is not canonical JSON") from error


def rotating_omission_actor(
    actor_ids: Sequence[int],
    *,
    epoch_number: int,
    tree_id: int,
    epoch_digest: str,
    block_hash: str,
) -> tuple[int, int]:
    """Return the FNV-1a hash and actor used by the native adapter."""

    actors = tuple(
        sorted(_integer(actor, "rotating actor", minimum=0) for actor in actor_ids)
    )
    if not actors or len(set(actors)) != len(actors):
        _error("rotating actors must be a non-empty unique set")
    epoch = _integer(epoch_number, "proposal epoch", minimum=0)
    tree = _integer(tree_id, "proposal tree", minimum=0)
    if epoch > 0xFFFF_FFFF or tree > 0xFFFF_FFFF:
        _error("proposal epoch and tree must fit uint32")
    try:
        epoch_bytes = bytes.fromhex(epoch_digest)
        block_bytes = bytes.fromhex(block_hash)
    except ValueError as error:
        raise FactorialManifestError("proposal digests must be hexadecimal") from error
    if len(epoch_bytes) != 32 or len(block_bytes) != 32:
        _error("proposal digests must each contain exactly 32 bytes")

    value = 14_695_981_039_346_656_037
    for byte in (
        epoch.to_bytes(4, "big") + tree.to_bytes(4, "big") + epoch_bytes + block_bytes
    ):
        value ^= byte
        value = (value * 1_099_511_628_211) & 0xFFFF_FFFF_FFFF_FFFF
    return value, actors[value % len(actors)]


def _validate_frozen_semantics(document: Mapping[str, Any]) -> None:
    manifest_id = document.get("manifest_id")
    if manifest_id not in {LEGACY_MANIFEST_ID, FROZEN_MANIFEST_ID}:
        _error("manifest ID is not a known frozen SHAPE25 contract")
    persistent = manifest_id == FROZEN_MANIFEST_ID
    replica_counts = tuple(
        _integer(item, f"replica_counts[{index}]")
        for index, item in enumerate(
            _array(document.get("replica_counts"), "replica_counts")
        )
    )
    if replica_counts != EXPECTED_REPLICA_COUNTS or 7 in replica_counts:
        _error("replica_counts must be exactly 13, 22, 31; N=7 smoke is excluded")
    if any((replica_count - 1) % 3 for replica_count in replica_counts):
        _error("every replica count must satisfy N = 3f + 1")

    responsiveness = _mapping(
        document.get("responsiveness_policy"), "responsiveness_policy"
    )
    if responsiveness.get("policy_version") != "shape25-sensitive-responsiveness-v1":
        _error("responsiveness policy version must be the frozen SHAPE25 policy")
    attempt_window = _integer(
        responsiveness.get("attempt_window"),
        "responsiveness_policy.attempt_window",
    )
    minimum_attempts = _integer(
        responsiveness.get("minimum_attempts"),
        "responsiveness_policy.minimum_attempts",
    )
    minimum_response_rate_ppm = _integer(
        responsiveness.get("minimum_response_rate_ppm"),
        "responsiveness_policy.minimum_response_rate_ppm",
        minimum=0,
    )
    maximum_timeout_rate_ppm = _integer(
        responsiveness.get("maximum_timeout_rate_ppm"),
        "responsiveness_policy.maximum_timeout_rate_ppm",
        minimum=0,
    )
    trailing_timeout_streak = _integer(
        responsiveness.get("trailing_timeout_streak"),
        "responsiveness_policy.trailing_timeout_streak",
        minimum=2,
    )
    latency_percentile_basis_points = _integer(
        responsiveness.get("latency_percentile_basis_points"),
        "responsiveness_policy.latency_percentile_basis_points",
    )
    if (
        attempt_window != 128
        or minimum_attempts != 32
        or minimum_response_rate_ppm != 950_000
        or maximum_timeout_rate_ppm != 50_000
        or trailing_timeout_streak != 2
        or latency_percentile_basis_points != 5_000
    ):
        _error("responsiveness policy must equal the frozen sensitive profile")
    if (
        minimum_attempts > attempt_window
        or trailing_timeout_streak > attempt_window
        or minimum_response_rate_ppm > 1_000_000
        or maximum_timeout_rate_ppm > 1_000_000
        or latency_percentile_basis_points > 10_000
    ):
        _error("responsiveness policy values are outside fixed bounds")
    if 1_000_000 // 3 <= maximum_timeout_rate_ppm:
        _error(
            "selected actor expected timeout share must strictly exceed the "
            "frozen timeout threshold for every replica count"
        )

    initial_fanouts = _fanouts(document.get("initial_fanouts"), "initial fanouts")
    candidate_fanouts = _fanouts(document.get("candidate_fanouts"), "candidate fanouts")
    if initial_fanouts != EXPECTED_INITIAL_FANOUTS:
        _error("initial fanouts must be the exact frozen 2/3/5 set")
    if candidate_fanouts != EXPECTED_CANDIDATE_FANOUTS:
        _error("candidate fanouts must be the exact frozen 2/3/5 set")

    byzantine = _mapping(document.get("byzantine"), "byzantine")
    if (
        byzantine.get("actor_count") != 3
        or byzantine.get("actor_count_rule") != "fixed_3_bounded_by_derived_f"
        or byzantine.get("actor_selection")
        != "sha256_ranked_canonical_epoch0_non_reference_roots_v1"
        or byzantine.get("actor_selection_preimage")
        != (
            "ascii_csv_membership_nul_decimal_q_nul_decimal_scientific_"
            "seed_nul_decimal_replica_id_v1"
        )
        or byzantine.get("actor_selection_inputs")
        != [
            "membership",
            "derived_q",
            "canonical_epoch0_reference_roots_0_through_q_minus_1_v1",
            "scientific_block_seed",
        ]
    ):
        _error(
            "actors must be three seed-ranked members of the canonical "
            "epoch-0 non-reference-root pool"
        )
    if any(3 > (replica_count - 1) // 3 for replica_count in replica_counts):
        _error("fixed campaign actor count must not exceed derived f")
    expected_mode = (
        "persistent_selected_omission_v1"
        if persistent
        else "rotating_intermittent_omission_v1"
    )
    if byzantine.get("mode") != expected_mode:
        _error(f"{manifest_id} requires Byzantine mode {expected_mode}")
    if byzantine.get("actions") != {
        "root": "normal",
        "internal": "omit_aggregate",
        "leaf": "omit_direct_vote",
    }:
        _error("Byzantine actions must match the role-aware omission contract")
    selection_vectors = _array(
        byzantine.get("actor_selection_vectors"),
        "byzantine.actor_selection_vectors",
    )
    expected_selection_vector_inputs = (
        (13, 9, 41_719),
        (22, 15, 41_722),
        (31, 21, 41_725),
    )
    if len(selection_vectors) != len(expected_selection_vector_inputs):
        _error("actor selection requires the three frozen ranking vectors")
    for index, (raw_vector, expected_inputs) in enumerate(
        zip(selection_vectors, expected_selection_vector_inputs)
    ):
        vector = _mapping(raw_vector, f"actor selection vector {index}")
        replica_count, q, scientific_seed = expected_inputs
        expected_selected = derive_actor_ids(
            replica_count,
            q,
            3,
            scientific_seed,
        )
        if vector != {
            "replica_count": replica_count,
            "q": q,
            "scientific_seed": scientific_seed,
            "selected_actor_ids": list(expected_selected),
        }:
            _error("actor selection vector disagrees with the SHA-256 ranking")
    expected_actor_schedule = (
        "all_selected_actors_per_proposal_v1"
        if persistent
        else (
            "fnv1a64_be_epoch_tree_epoch_digest_block_hash_"
            "modulo_sorted_actors_v1"
        )
    )
    actor_schedule = byzantine.get(
        "actor_schedule" if persistent else "actor_rotation"
    )
    if actor_schedule != expected_actor_schedule:
        _error("actor schedule must name the exact native omission contract")
    if persistent and "actor_rotation" in byzantine:
        _error("persistent omission must not be mislabeled as actor rotation")
    if not persistent and "actor_schedule" in byzantine:
        _error("legacy rotating omission must retain its frozen schema")
    if (
        _integer(
            byzantine.get("maximum_rotating_contexts"),
            "byzantine.maximum_rotating_contexts",
        )
        != 100_000
    ):
        _error("rotating context capacity must equal the frozen bound")
    if persistent and "actor_rotation_vectors" in byzantine:
        _error("persistent omission must not carry legacy rotation vectors")
    vectors = _array(
        byzantine.get("actor_rotation_vectors", []),
        "byzantine.actor_rotation_vectors",
    )
    if len(vectors) != (0 if persistent else 3):
        _error(
            "persistent omission has no rotation vectors; rotating omission "
            "requires the three frozen cross-language vectors"
        )
    for index, raw_vector in enumerate(vectors):
        vector = _mapping(raw_vector, f"actor rotation vector {index}")
        computed_hash, computed_actor = rotating_omission_actor(
            _array(vector.get("sorted_actor_ids"), "sorted actor IDs"),
            epoch_number=vector.get("epoch_number"),
            tree_id=vector.get("tree_id"),
            epoch_digest=vector.get("epoch_digest"),
            block_hash=vector.get("block_hash"),
        )
        if (
            vector.get("fnv1a64") != computed_hash
            or vector.get("selected_actor") != computed_actor
        ):
            _error("actor rotation vector disagrees with the FNV-1a reference")
    expected_max_omissions = 3 if persistent else 1
    if (
        _integer(
            byzantine.get("max_omissions_per_proposal"),
            "byzantine.max_omissions_per_proposal",
        )
        != expected_max_omissions
    ):
        _error(
            "maximum omissions per proposal must equal the frozen actor "
            "schedule cardinality"
        )

    global_worst_candidate_depth = max(
        tree_depth(replica_count, fanout)
        for replica_count in replica_counts
        for fanout in candidate_fanouts
    )
    timers = _mapping(document.get("timers"), "timers")
    if timers.get("depth_policy") != "global_worst_candidate_depth_linear_v1":
        _error("timer depth policy must use the frozen global-depth rule")
    timer_depth = _integer(
        timers.get("global_worst_candidate_depth"),
        "timers.global_worst_candidate_depth",
    )
    if timer_depth != global_worst_candidate_depth:
        _error("timer depth must equal the global worst candidate depth")
    aggregation_ms_per_depth = _integer(
        timers.get("aggregation_timeout_ms_per_depth"),
        "timers.aggregation_timeout_ms_per_depth",
    )
    leader_progress_ms_per_depth = _integer(
        timers.get("leader_progress_timeout_ms_per_depth"),
        "timers.leader_progress_timeout_ms_per_depth",
    )
    if aggregation_ms_per_depth * timer_depth != 500:
        _error("aggregation timer policy must preserve 500 ms at depth 4")
    if leader_progress_ms_per_depth * timer_depth != 20_000:
        _error("leader-progress timer policy must preserve 20000 ms at depth 4")
    _integer(
        timers.get("leader_activation_grace_ms"),
        "timers.leader_activation_grace_ms",
    )
    startup_timeout_s = _integer(
        timers.get("startup_timeout_s"), "timers.startup_timeout_s"
    )
    hard_timeout_s = _integer(timers.get("hard_timeout_s"), "timers.hard_timeout_s")
    observation_bound_rule = timers.get(
        "transition_observation_bound_rule", "phase_deadline_v1"
    )
    expected_observation_bound_rule = (
        "shared_slot_hard_deadline_until_manager_selection_v1"
        if persistent
        else "phase_deadline_v1"
    )
    if observation_bound_rule != expected_observation_bound_rule:
        _error(
            "transition observation must use the frozen manager-selection "
            "clock boundary"
        )
    convergence_deadline_s = _integer(
        timers.get("transition_convergence_deadline_s"),
        "timers.transition_convergence_deadline_s",
    )
    schedule_slack_s = _integer(
        timers.get("schedule_slack_s"),
        "timers.schedule_slack_s",
    )
    if schedule_slack_s < 30:
        _error("transition schedule requires at least 30 seconds of explicit slack")
    drain_margin_s = _integer(timers.get("drain_margin_s"), "timers.drain_margin_s")
    workload = _mapping(document.get("workload"), "workload")
    if (
        _integer(
            workload.get("piped_latency_ms"),
            "workload.piped_latency_ms",
        )
        != 1
    ):
        _error("piped latency must equal the frozen explicit 1 ms value")
    bucket_width_s = _integer(workload.get("bucket_width_s"), "workload.bucket_width_s")
    baseline_bucket_count = _integer(
        workload.get("baseline_bucket_count"),
        "workload.baseline_bucket_count",
    )
    fault_evidence_bucket_count = _integer(
        workload.get("fault_evidence_bucket_count"),
        "workload.fault_evidence_bucket_count",
    )
    epoch1_stable_bucket_count = _integer(
        workload.get("epoch1_stable_bucket_count"),
        "workload.epoch1_stable_bucket_count",
    )
    epoch2_stable_bucket_count = _integer(
        workload.get("epoch2_stable_bucket_count"),
        "workload.epoch2_stable_bucket_count",
    )
    window = _mapping(byzantine.get("window"), "byzantine.window")
    start_after_prelaunch_anchor_s = _integer(
        window.get("start_after_prelaunch_anchor_s"),
        "byzantine.window.start_after_prelaunch_anchor_s",
    )
    expected_start_s = startup_timeout_s + (baseline_bucket_count * bucket_width_s)
    if start_after_prelaunch_anchor_s != expected_start_s:
        _error(
            "fault window start must equal startup timeout plus the clean "
            "baseline duration from the shared pre-launch anchor"
        )
    duration_s = _integer(window.get("duration_s"), "byzantine.window.duration_s")
    minimum_duration_s = (
        fault_evidence_bucket_count * bucket_width_s
        + 2 * convergence_deadline_s
        + epoch1_stable_bucket_count * bucket_width_s
        + epoch2_stable_bucket_count * bucket_width_s
        + drain_margin_s
        + schedule_slack_s
    )
    if duration_s < minimum_duration_s:
        _error(
            "fault window duration must cover fault evidence, both transition "
            "convergence deadlines, both stable windows, the drain margin, "
            "and explicit schedule slack"
        )
    if hard_timeout_s < (start_after_prelaunch_anchor_s + duration_s + drain_margin_s):
        _error(
            "hard timeout must cover the pre-launch offset, full fault "
            "window, and final drain margin"
        )

    constraints = _mapping(document.get("shape_constraints"), "shape_constraints")
    if constraints.get("epoch_fanout_policy") != "one_uniform_fanout_per_epoch":
        _error("mixed fanout within an epoch is forbidden")
    if constraints.get("pipeline_policy") != "fixed_first_slice":
        _error("adaptive pipeline selection is forbidden in the first slice")

    execution = _mapping(document.get("execution"), "execution")
    scheduling = _mapping(document.get("scheduling"), "scheduling")
    resources = _mapping(document.get("resources"), "resources")
    artifacts = _mapping(document.get("artifacts"), "artifacts")
    claim_scope = _mapping(document.get("claim_scope"), "claim_scope")
    if (
        scheduling.get("execution_block_order") != "sha256_ranked_block_ids_v1"
        or scheduling.get("arm_counterbalancing")
        != "greedy_minimum_position_imbalance_sha256_tiebreak_v1"
        or type(scheduling.get("campaign_order_seed")) is not int
        or scheduling.get("campaign_order_seed") < 0
    ):
        _error("execution schedule must use the frozen counterbalancing rule")
    if claim_scope != {
        "other_cells": "parameter_coverage_only",
        "causal_inference": ("matched_repeated_blocks_within_prespecified_strata_only"),
        "headline_estimator": (
            "arithmetic_mean_of_five_matched_block_contrasts_v1"
        ),
        "uncertainty_interval": "two_sided_student_t_95_df4_v1",
        "directional_claim_rule": (
            "lower_95_ci_strictly_greater_than_zero_v1"
        ),
        "phase_sequence_endpoint": (
            "adaptive_arms_mean_tps_matched_by_block_v1"
        ),
        "placement_headline_replica_count": 31,
        "placement_headline_initial_fanout": 5,
        "placement_headline_block_count": 5,
        "shape_and_joint_headline_replica_count": 31,
        "shape_and_joint_headline_initial_fanout": 2,
        "shape_and_joint_headline_block_count": 5,
    }:
        _error(
            "claim scope must stratify placement at N31/F5 and shape/joint " "at N31/F2"
        )
    if (
        document.get("execution_authorized") is not False
        or document.get("execution_receipt_required") is not True
    ):
        _error("planning manifest must require a later execution receipt")
    if (
        execution.get("mode") != "fixed_sequential"
        or execution.get("automatic_retries") != 0
        or execution.get("replacement_policy") != "none"
        or execution.get("outcome_dependent_order") is not False
        or scheduling.get("outcome_dependent_order") is not False
        or resources.get("max_parallel_slots") != 1
    ):
        _error("execution must be sequential, no-retry, and no-replacement")
    if artifacts.get("preserve_outcomes") != [
        "NOT_STARTED",
        "PASS",
        "FAIL",
        "INCOMPLETE",
    ]:
        _error("all terminal and unstarted slot outcomes must be preserved")

    semantic_sha256 = hashlib.sha256(_canonical_json_bytes(document)).hexdigest()
    expected_semantic_sha256 = (
        FROZEN_SEMANTIC_SHA256 if persistent else LEGACY_SEMANTIC_SHA256
    )
    if semantic_sha256 != expected_semantic_sha256:
        _error("manifest differs from the frozen semantic contract")


def tree_depth(replica_count: int, fanout: int) -> int:
    """Return the minimum edge depth of a uniform-fanout tree covering N."""

    n = _integer(replica_count, "replica count")
    width = _integer(fanout, "fanout")
    if width > 255:
        _error("fanout must be 1..255")
    if n == 1:
        return 0
    depth, covered, level_width = 0, 1, 1
    while covered < n:
        level_width *= width
        covered += level_width
        depth += 1
    return depth


def epoch0_internal_tree_ids(
    replica_count: int,
    *,
    initial_fanout: int,
    replica_id: int,
) -> tuple[int, ...]:
    """Return active epoch-0 tree IDs where a member is physically internal.

    Native ``tree-generation = default`` rotates canonical membership left by
    the tree ID, then constructs a breadth-first uniform-fanout tree.  Epoch 0
    actively rotates all N trees; the Q-tree prefix is only a shape-scoring
    reference set.
    """

    n = _integer(replica_count, "replica count")
    fanout = _integer(initial_fanout, "initial fanout")
    member = _integer(replica_id, "replica ID", minimum=0)
    if member >= n:
        _error("epoch-0 tree role input is outside membership")
    return tuple(
        tree_id
        for tree_id in range(n)
        if (position := (member - tree_id) % n) != 0 and position * fanout + 1 < n
    )


def derive_consensus_shape(
    replica_count: int,
    *,
    initial_fanout: int,
    candidate_fanouts: Sequence[int],
) -> ConsensusShape:
    """Derive f, Q, tree count, and uniform tree depths from N."""

    n = _integer(replica_count, "N")
    if (n - 1) % 3:
        _error("replica count must satisfy N = 3f + 1")
    initial = _fanouts([initial_fanout], "initial fanouts")[0]
    candidates = _fanouts(list(candidate_fanouts), "candidate fanouts")
    f = (n - 1) // 3
    q = 2 * f + 1
    if q > 255:
        _error("derived Q/tree count exceeds the uint8 runtime bound")
    candidate_depths = tuple((fanout, tree_depth(n, fanout)) for fanout in candidates)
    return ConsensusShape(
        replica_count=n,
        f=f,
        q=q,
        tree_count=q,
        initial_fanout=initial,
        initial_depth=tree_depth(n, initial),
        candidate_depths=candidate_depths,
        worst_candidate_depth=max(depth for _, depth in candidate_depths),
    )


def parse_manifest_bytes(payload: bytes) -> FrozenFactorialManifest:
    """Validate semantics while retaining the exact source-byte digest."""

    document = _parse_json(payload)
    _reject_authority_fields(document)
    _validate_frozen_semantics(document)

    byzantine = document["byzantine"]
    window = byzantine["window"]
    resources = document["resources"]
    ports = resources["port_bases"]
    repetitions = document["repetitions"]
    scheduling = document["scheduling"]
    artifacts = document["artifacts"]
    execution = document["execution"]
    return FrozenFactorialManifest(
        manifest_id=document["manifest_id"],
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
        replica_counts=tuple(document["replica_counts"]),
        initial_fanouts=tuple(document["initial_fanouts"]),
        candidate_fanouts=tuple(document["candidate_fanouts"]),
        pipeline_stretch=document["pipeline_stretch"],
        epoch_fanout_policy=document["shape_constraints"]["epoch_fanout_policy"],
        pipeline_policy=document["shape_constraints"]["pipeline_policy"],
        arms=tuple(FactorialArm(**arm) for arm in document["arms"]),
        default_blocks_per_cell=repetitions["default_blocks_per_cell"],
        repetition_overrides=tuple(
            (item["replica_count"], item["initial_fanout"], item["blocks"])
            for item in repetitions["cell_overrides"]
        ),
        byzantine=ByzantineContract(
            mode=byzantine["mode"],
            actor_count=byzantine["actor_count"],
            actor_count_rule=byzantine["actor_count_rule"],
            actor_selection=byzantine["actor_selection"],
            actor_selection_preimage=byzantine["actor_selection_preimage"],
            actor_selection_inputs=tuple(byzantine["actor_selection_inputs"]),
            actor_selection_vectors=tuple(
                ActorSelectionVector(
                    replica_count=vector["replica_count"],
                    q=vector["q"],
                    scientific_seed=vector["scientific_seed"],
                    selected_actor_ids=tuple(vector["selected_actor_ids"]),
                )
                for vector in byzantine["actor_selection_vectors"]
            ),
            actor_schedule=byzantine.get(
                "actor_schedule", byzantine.get("actor_rotation")
            ),
            actor_rotation_vectors=tuple(
                ActorRotationVector(
                    epoch_number=vector["epoch_number"],
                    tree_id=vector["tree_id"],
                    epoch_digest=vector["epoch_digest"],
                    block_hash=vector["block_hash"],
                    sorted_actor_ids=tuple(vector["sorted_actor_ids"]),
                    fnv1a64=vector["fnv1a64"],
                    selected_actor=vector["selected_actor"],
                )
                for vector in byzantine.get("actor_rotation_vectors", [])
            ),
            maximum_rotating_contexts=byzantine["maximum_rotating_contexts"],
            start_after_prelaunch_anchor_s=window["start_after_prelaunch_anchor_s"],
            duration_s=window["duration_s"],
            max_omissions_per_proposal=byzantine["max_omissions_per_proposal"],
            actions=ByzantineActions(**byzantine["actions"]),
        ),
        workload=WorkloadContract(**document["workload"]),
        responsiveness_policy=ResponsivenessPolicyContract(
            **document["responsiveness_policy"]
        ),
        common_timers=CommonTimers(**document["timers"]),
        scientific_seed_base=scheduling["scientific_seed_base"],
        scientific_seed_rule=scheduling["scientific_seed_rule"],
        slot_order=scheduling["slot_order"],
        slot_nonce_rule=scheduling["slot_nonce_rule"],
        campaign_order_seed=scheduling["campaign_order_seed"],
        execution_block_order=scheduling["execution_block_order"],
        arm_counterbalancing=scheduling["arm_counterbalancing"],
        scheduling_outcome_dependent_order=scheduling["outcome_dependent_order"],
        claim_scope=ClaimScope(**document["claim_scope"]),
        results_root=artifacts["results_root"],
        canonical_plan_filename=artifacts["canonical_plan_filename"],
        one_directory_per_slot=artifacts["one_directory_per_slot"],
        preserve_outcomes=tuple(artifacts["preserve_outcomes"]),
        resources=ResourceContract(
            minimum_free_bytes=resources["minimum_free_bytes"],
            minimum_free_bytes_interpretation=(
                resources["minimum_free_bytes_interpretation"]
            ),
            max_parallel_slots=resources["max_parallel_slots"],
            peer_port_base=ports["peer"],
            client_port_base=ports["client"],
            manager_port_base=ports["manager"],
            slot_port_stride=resources["slot_port_stride"],
        ),
        execution_mode=execution["mode"],
        automatic_retries=execution["automatic_retries"],
        replacement_policy=execution["replacement_policy"],
        execution_outcome_dependent_order=execution["outcome_dependent_order"],
        execution_authorized=document["execution_authorized"],
        execution_receipt_required=document["execution_receipt_required"],
    )


def load_frozen_manifest_bytes(payload: bytes) -> FrozenFactorialManifest:
    manifest = parse_manifest_bytes(payload)
    expected_sha256 = {
        LEGACY_MANIFEST_ID: LEGACY_MANIFEST_SHA256,
        FROZEN_MANIFEST_ID: FROZEN_MANIFEST_SHA256,
    }.get(manifest.manifest_id)
    if manifest.manifest_sha256 != expected_sha256:
        _error("input does not match the exact frozen manifest bytes")
    return manifest


def load_frozen_manifest(path: Path) -> FrozenFactorialManifest:
    try:
        return load_frozen_manifest_bytes(path.read_bytes())
    except OSError as error:
        raise FactorialManifestError(f"cannot read manifest: {path}") from error


def _frozen_block_ids() -> tuple[str, ...]:
    result: list[str] = []
    for replica_count in EXPECTED_REPLICA_COUNTS:
        for initial_fanout in EXPECTED_INITIAL_FANOUTS:
            blocks = 5 if replica_count == 31 and initial_fanout in (2, 5) else 1
            result.extend(
                f"n{replica_count}-f{initial_fanout}-b{index:02d}"
                for index in range(1, blocks + 1)
            )
    return tuple(result)


def derive_slot_nonce(block_id: str, arm_code: str) -> int:
    """Derive a non-scientific identity/port/path nonce from block and arm."""

    try:
        block_ordinal = _frozen_block_ids().index(block_id)
        arm_ordinal = EXPECTED_ARM_CODES.index(arm_code)
    except ValueError as error:
        raise FactorialManifestError(
            f"unknown frozen block/arm pair: {block_id}/{arm_code}"
        ) from error
    return block_ordinal * len(EXPECTED_ARM_CODES) + arm_ordinal


def derive_execution_schedule(
    block_ids: Sequence[str],
    arm_codes: Sequence[str],
    campaign_order_seed: int,
) -> tuple[BlockExecutionSchedule, ...]:
    """Predeclare a result-independent, position-balanced launch order."""

    blocks = tuple(block_ids)
    arms = tuple(arm_codes)
    seed = _integer(campaign_order_seed, "campaign order seed", minimum=0)
    if not blocks or len(set(blocks)) != len(blocks):
        _error("execution schedule block IDs must be non-empty and unique")
    if not arms or len(set(arms)) != len(arms):
        _error("execution schedule arm codes must be non-empty and unique")

    def block_rank(block_id: str) -> tuple[bytes, str]:
        payload = f"{seed}\x00{block_id}".encode("ascii")
        return hashlib.sha256(payload).digest(), block_id

    ranked_blocks = tuple(sorted(blocks, key=block_rank))
    counts = {(arm, position): 0 for arm in arms for position in range(len(arms))}
    result: list[BlockExecutionSchedule] = []
    for block_ordinal, block_id in enumerate(ranked_blocks, start=1):
        scored: list[tuple[tuple[int, int, bytes], tuple[str, ...]]] = []
        for permutation in itertools.permutations(arms):
            prospective = dict(counts)
            for position, arm in enumerate(permutation):
                prospective[arm, position] += 1
            values = tuple(prospective.values())
            tie_payload = (f"{seed}\x00{block_id}\x00{','.join(permutation)}").encode(
                "ascii"
            )
            score = (
                max(values) - min(values),
                sum(value * value for value in values),
                hashlib.sha256(tie_payload).digest(),
            )
            scored.append((score, permutation))
        _, arm_order = min(scored)
        for position, arm in enumerate(arm_order):
            counts[arm, position] += 1
        result.append(
            BlockExecutionSchedule(
                block_id=block_id,
                block_execution_ordinal=block_ordinal,
                arm_order=arm_order,
            )
        )
    lower = len(blocks) // len(arms)
    upper = lower + int(len(blocks) % len(arms) != 0)
    if set(counts.values()) - {lower, upper}:
        _error("execution schedule did not balance arm positions")
    return tuple(result)


def derive_actor_ids(
    replica_count: int,
    quorum: int,
    actor_count: int,
    scientific_seed: int,
) -> tuple[int, ...]:
    pool = tuple(range(quorum, replica_count))
    if actor_count > len(pool):
        _error("campaign actor count exceeds the canonical non-reference-root pool")
    membership = ",".join(str(member) for member in range(replica_count))

    def rank(member: int) -> tuple[bytes, int]:
        value = (f"{membership}\x00{quorum}\x00{scientific_seed}\x00{member}").encode(
            "ascii"
        )
        return hashlib.sha256(value).digest(), member

    return tuple(sorted(sorted(pool, key=rank)[:actor_count]))


def build_factorial_plan(manifest: FrozenFactorialManifest) -> FactorialPlan:
    """Derive the immutable 17-block, 68-slot preflight plan."""

    slots: list[FactorialSlot] = []
    execution_schedule = derive_execution_schedule(
        _frozen_block_ids(),
        tuple(arm.code for arm in manifest.arms),
        manifest.campaign_order_seed,
    )
    execution_by_block = {
        scheduled.block_id: scheduled for scheduled in execution_schedule
    }
    block_ordinal = 0
    for replica_count in manifest.replica_counts:
        for initial_fanout in manifest.initial_fanouts:
            consensus = derive_consensus_shape(
                replica_count,
                initial_fanout=initial_fanout,
                candidate_fanouts=manifest.candidate_fanouts,
            )
            if any(
                not epoch0_internal_tree_ids(
                    replica_count,
                    initial_fanout=initial_fanout,
                    replica_id=actor,
                )
                for actor in range(consensus.q, replica_count)
            ):
                _error(
                    "canonical actor pool contains a member that cannot be "
                    "internal in any active epoch-0 tree"
                )
            blocks_in_cell = manifest.blocks_for(replica_count, initial_fanout)
            for block_index in range(1, blocks_in_cell + 1):
                block_id = f"n{replica_count}-f{initial_fanout}-b{block_index:02d}"
                scientific_seed = manifest.scientific_seed_base + block_ordinal
                actor_ids = derive_actor_ids(
                    replica_count,
                    consensus.q,
                    manifest.byzantine.actor_count,
                    scientific_seed,
                )
                if any(
                    not epoch0_internal_tree_ids(
                        replica_count,
                        initial_fanout=initial_fanout,
                        replica_id=actor,
                    )
                    for actor in actor_ids
                ):
                    _error("selected actor cannot be internal in an epoch-0 tree")
                scheduled = execution_by_block[block_id]
                for arm in manifest.arms:
                    nonce = derive_slot_nonce(block_id, arm.code)
                    if nonce != len(slots):
                        _error("derived slot nonce disagrees with frozen order")
                    ordinal = nonce + 1
                    arm_execution_position = scheduled.arm_order.index(arm.code) + 1
                    execution_ordinal = (scheduled.block_execution_ordinal - 1) * len(
                        manifest.arms
                    ) + arm_execution_position
                    slot_id = f"slot-{ordinal:03d}-{block_id}-{arm.code}"
                    port_offset = nonce * manifest.resources.slot_port_stride
                    ports = PortAllocation(
                        peer_base=manifest.resources.peer_port_base + port_offset,
                        client_base=manifest.resources.client_port_base + port_offset,
                        manager=manifest.resources.manager_port_base + port_offset,
                    )
                    if (
                        max(
                            ports.peer_base + replica_count - 1,
                            ports.client_base + replica_count - 1,
                            ports.manager,
                        )
                        > 65_535
                    ):
                        _error(f"derived ports exceed uint16 for {slot_id}")
                    slots.append(
                        FactorialSlot(
                            ordinal=ordinal,
                            slot_nonce=nonce,
                            slot_id=slot_id,
                            block_id=block_id,
                            block_index=block_index,
                            blocks_in_cell=blocks_in_cell,
                            block_execution_ordinal=(scheduled.block_execution_ordinal),
                            arm_execution_position=arm_execution_position,
                            execution_ordinal=execution_ordinal,
                            scientific_seed=scientific_seed,
                            consensus=consensus,
                            candidate_fanouts=manifest.candidate_fanouts,
                            pipeline_stretch=manifest.pipeline_stretch,
                            epoch_fanout_policy=manifest.epoch_fanout_policy,
                            pipeline_policy=manifest.pipeline_policy,
                            arm=arm,
                            byzantine=manifest.byzantine,
                            byzantine_actor_ids=actor_ids,
                            workload=manifest.workload,
                            responsiveness_policy=manifest.responsiveness_policy,
                            common_timers=manifest.common_timers,
                            ports=ports,
                            result_path=f"{manifest.results_root}/{slot_id}",
                        )
                    )
                block_ordinal += 1

    if (block_ordinal, len(slots)) != (EXPECTED_BLOCK_COUNT, EXPECTED_SLOT_COUNT):
        _error("frozen matrix must derive exactly 17 blocks and 68 slots")
    if len({slot.slot_id for slot in slots}) != EXPECTED_SLOT_COUNT:
        _error("derived slot IDs are not unique")
    if {slot.execution_ordinal for slot in slots} != set(
        range(1, EXPECTED_SLOT_COUNT + 1)
    ):
        _error("execution schedule must cover each global ordinal exactly once")

    plan = FactorialPlan(
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest.manifest_sha256,
        execution_authorized=manifest.execution_authorized,
        execution_receipt_required=manifest.execution_receipt_required,
        execution_mode=manifest.execution_mode,
        automatic_retries=manifest.automatic_retries,
        replacement_policy=manifest.replacement_policy,
        outcome_dependent_order=(
            manifest.scheduling_outcome_dependent_order
            or manifest.execution_outcome_dependent_order
        ),
        campaign_order_seed=manifest.campaign_order_seed,
        execution_block_order=manifest.execution_block_order,
        arm_counterbalancing=manifest.arm_counterbalancing,
        execution_schedule=execution_schedule,
        claim_scope=manifest.claim_scope,
        preserve_outcomes=manifest.preserve_outcomes,
        results_root=manifest.results_root,
        canonical_plan_filename=manifest.canonical_plan_filename,
        minimum_free_bytes=manifest.resources.minimum_free_bytes,
        minimum_free_bytes_interpretation=(
            manifest.resources.minimum_free_bytes_interpretation
        ),
        max_parallel_slots=manifest.resources.max_parallel_slots,
        global_worst_candidate_depth=max(slot.worst_candidate_depth for slot in slots),
        slots=tuple(slots),
    )
    if (
        manifest.common_timers.global_worst_candidate_depth
        != plan.global_worst_candidate_depth
    ):
        _error("common timer depth disagrees with the derived global depth")
    if (
        manifest.manifest_sha256 == FROZEN_MANIFEST_SHA256
        and plan.plan_sha256 != FROZEN_PLAN_SHA256
    ):
        _error("canonical plan bytes differ from the frozen plan identity")
    return plan


def canonical_plan_bytes(plan: FactorialPlan) -> bytes:
    if not isinstance(plan, FactorialPlan):
        _error("canonical plan input must be a FactorialPlan")
    return _canonical_json_bytes(plan.as_document())


__all__ = (
    "EXPECTED_ARM_CODES",
    "EXPECTED_BLOCK_COUNT",
    "EXPECTED_CANDIDATE_FANOUTS",
    "EXPECTED_INITIAL_FANOUTS",
    "EXPECTED_REPLICA_COUNTS",
    "EXPECTED_SLOT_COUNT",
    "FROZEN_MANIFEST_ID",
    "FROZEN_MANIFEST_SHA256",
    "FROZEN_PLAN_SHA256",
    "FROZEN_SEMANTIC_SHA256",
    "ActorSelectionVector",
    "ActorRotationVector",
    "ByzantineActions",
    "ByzantineContract",
    "BlockExecutionSchedule",
    "ClaimScope",
    "CommonTimers",
    "ConsensusShape",
    "FactorialArm",
    "FactorialManifestError",
    "FactorialPlan",
    "FactorialSlot",
    "FrozenFactorialManifest",
    "PortAllocation",
    "ResponsivenessPolicyContract",
    "ResourceContract",
    "WorkloadContract",
    "build_factorial_plan",
    "canonical_plan_bytes",
    "derive_actor_ids",
    "derive_consensus_shape",
    "derive_execution_schedule",
    "derive_slot_nonce",
    "epoch0_internal_tree_ids",
    "load_frozen_manifest",
    "load_frozen_manifest_bytes",
    "parse_manifest_bytes",
    "rotating_omission_actor",
    "tree_depth",
)
