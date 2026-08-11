"""Pure launch contracts for the frozen SHAPE37 factorial campaign.

This module does not predict adaptive outcomes and never starts a process.
It freezes only the inputs, live-evidence acceptance predicates, relative
timing, argv templates, and the result-independent execution order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import string
from typing import Any

from .factorial_manifest import (
    EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1,
    EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V1,
    EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2,
    EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3,
    EXCLUDED_REPAIR_SMOKE_VERIFIED_RESPONSE_DUPLICATE_PROBE_CONTRACT_V1,
    EXECUTION_CLEANUP_CONTRACT_V1,
    FROZEN_MANIFEST_ID,
    FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1,
    FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2,
    INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT_V1,
    PRECONTAINMENT_FAULT_COVERAGE_GATE_V1,
    PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1,
    PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1,
    POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1,
    SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1,
    V10_MANIFEST_ID,
    V11_MANIFEST_ID,
    V12_MANIFEST_ID,
    V13_MANIFEST_ID,
    V14_MANIFEST_ID,
    V15_MANIFEST_ID,
    V16_MANIFEST_ID,
    V17_MANIFEST_ID,
    V18_MANIFEST_ID,
    V19_MANIFEST_ID,
    V20_MANIFEST_ID,
    V21_MANIFEST_ID,
    V22_MANIFEST_ID,
    V23_MANIFEST_ID,
    V24_MANIFEST_ID,
    V25_MANIFEST_ID,
    V26_MANIFEST_ID,
    V27_MANIFEST_ID,
    V28_MANIFEST_ID,
    V29_MANIFEST_ID,
    V30_MANIFEST_ID,
    V31_MANIFEST_ID,
    V32_MANIFEST_ID,
    V33_MANIFEST_ID,
    V34_MANIFEST_ID,
    V35_MANIFEST_ID,
    V36_MANIFEST_ID,
    V9_MANIFEST_ID,
    VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V1,
    VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2,
    FactorialManifestError,
    FactorialPlan,
    FactorialSlot,
    RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1,
    RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1,
    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1,
    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2,
    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V3,
    RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
    RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1,
    RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1,
    RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2,
    RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
    RESPONSIVE_ROLE_SCOPED_SCHEDULE_V1,
    ResponsivenessPolicyContract,
    derive_tiered_cohorts,
)

V11_RUNTIME_SHA256 = (
    "5a8674342f4f7c954e71b0ead0b027b7792c1be279445f107a98b5726f6f82d4"
)
V11_SMOKE_RUNTIME_SHA256 = (
    "dc4e13ac0ed9f333f72003c0c49e6ab7820024d3533cd07b0fd6013f8a34a3e6"
)
V12_RUNTIME_SHA256 = (
    "fc65a289fa6bf574eae2cc37ac4117f352a9d499cf2d7d32d422cda67835dbfb"
)
V12_SMOKE_RUNTIME_SHA256 = (
    "21661e6b936bbeaa5983b66c669d67a4ffb846c1e534cdc9e6563856f039978e"
)
V13_RUNTIME_SHA256 = (
    "613e78129d1f600f9690a9fe3dad18c2f9f7515c247958e0215a1f0d4ef7a693"
)
V13_SMOKE_RUNTIME_SHA256 = (
    "5f70c7b117be7a5f425e9cd19b421cbc7958a4904daf7336b2989eea4fce641e"
)
V14_RUNTIME_SHA256 = (
    "1aacea1a7c7e72158df7661514acdc26f474fd4292eed9b108ecabc645c604e6"
)
V14_SMOKE_RUNTIME_SHA256 = (
    "82b5f4bf0f90b93f34e374b96f09de55b8a2953a9ef83d2e533d3a16362be6d3"
)
V15_RUNTIME_SHA256 = (
    "97a6222f8d51aca78cbaa64ada641cc0227614c217e0f2f734144e21315c734c"
)
V15_SMOKE_RUNTIME_SHA256 = (
    "b2593316eb97bb683dc5156490ebcc20fb2f58d70b57b0953a14c28b896d6d48"
)
V15_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "e5ce14245c9c225ac748c66e1447151eda802397f9bf9f84e5378bf09de99209"
)
V16_RUNTIME_SHA256 = (
    "f79b565f53fe5f0ec950be14ae1c0e1d0faf4d9c2ef955f759aa3e1de1de70da"
)
V16_SMOKE_RUNTIME_SHA256 = (
    "0e5e239655b166eb6b1ebee91c8a34105d586a3e288f90b79217e52fdffde67c"
)
V16_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "e466b66c35795d671aae050831870aba45e89ecf4f4c68033185e2b8ecb89e2a"
)
V17_RUNTIME_SHA256 = (
    "4de3d25cc3f6ed0325e39cc670c27d5db67c3facb7bb081bf8f93707094c0fac"
)
V17_SMOKE_RUNTIME_SHA256 = (
    "28aa251fb7a21296eec0ef3b49f9e1c6c68dc30d4cda83998f9ee1559896ae07"
)
V17_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "7c4a5b7be38b7e985326e3c2286cac71891c2dc32488087064b4f85566d81d70"
)
V18_RUNTIME_SHA256 = (
    "a418a0d80e9deefd7bbc988fd92129ddc82b971d1a981e9d204b757e7f75ea37"
)
V18_SMOKE_RUNTIME_SHA256 = (
    "ba095ceb0ac43a42aba395963bc81f47dc8315ce914e6f06dd43878a3ed36d3b"
)
V18_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "0912ff8e9f5a0f8581e0c8422e8d55c5ff6942a6573864d635bec39d69d1835a"
)
V19_RUNTIME_SHA256 = (
    "1b81ec26e48439d05ac54b68d2a1dafb3d38c4140f835ea6e1f0f3b07545046d"
)
V19_SMOKE_RUNTIME_SHA256 = (
    "f0c999258453cc23bcf2956cb7b8b50ee3b3b05e6b7158ad5dd5d621046c03a0"
)
V19_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "538740a2d5421d80994bc7007421fe57a10e965fb1e5f71f1bc57b78c037b3d6"
)
V20_RUNTIME_SHA256 = (
    "df50a0202818bcafc699dd266dab6f97147659b0a09c4362207b90bd74d773a7"
)
V20_SMOKE_RUNTIME_SHA256 = (
    "cdcb497c77d92ef66618980b3c710d3702d9e43d92802a8c95e745ace611b96e"
)
V20_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "31f2a2a6d35c5f7c115b1dc6c321e92dc54f9edfd37ba9a9d9ac61c45d21f611"
)
V21_RUNTIME_SHA256 = (
    "c553c6bbbb6ff9014d5935eb1e0d939757eb9ea90a16038b7fefd34fada3ad8b"
)
V21_SMOKE_RUNTIME_SHA256 = (
    "c6d0a7e10b45c37072fd1dcc363f72d982efb726e57f1efe0f88dddf8f2f843c"
)
V21_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "5f0fa01ac8f5368cc09eff70d0a30f22156275e86683a627f54840caaa0eaa0c"
)
V22_RUNTIME_SHA256 = (
    "ca423f6513cf54f774ce198af970af4bc0bccae2bb0143ab3e48a842ff680caa"
)
V22_SMOKE_RUNTIME_SHA256 = (
    "9f8ecd3fbf44fe067e376bd24efe0adcb0938d8b383291e81106432ea1136583"
)
V22_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "b45fdba31d55c70107751cb0f3a00e26a2bf6b49352d50b342cd9768740af4db"
)
V23_RUNTIME_SHA256 = (
    "2eadce094849280fa632cba7064bed822b2e172485e49d084131d09be9e73e42"
)
V23_SMOKE_RUNTIME_SHA256 = (
    "aa86c344be1df785a3dd0f8602cda5534316b4b0f5e09d45b8605d14f74976d0"
)
V23_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "8706bc9292691b48c5f5db7728e22bebb6eaae399643da9cee5778d78adb9bd6"
)
V24_RUNTIME_SHA256 = (
    "e8ff4013c5b28c1d6c7650ff1eadce275f8296e6e719d0fbdb39e64d0f1c8b2d"
)
V24_SMOKE_RUNTIME_SHA256 = (
    "dee2b34183787ab7f55555c0fc5ab6e08ea1234e9c1c52aa63967a51b5e63f2c"
)
V24_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "eff1bc9bdbbda5d7dc20a77dd5217ade97d67173b4ac877b9a68f717281088fd"
)
V25_RUNTIME_SHA256 = (
    "c0b5195113defdc86cb959f32181436e288241eb2d3091c6d7f43accd2ebaea2"
)
V25_SMOKE_RUNTIME_SHA256 = (
    "a6f5cc32088a7d8006623536b8aada8378493fa97b72e21d2d5b9ed4e3ed0d47"
)
V25_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "034bd2a7e0b12677845554a1ed46c6053457e1c23476a696d4a246e3ef070149"
)
V26_RUNTIME_SHA256 = (
    "35697be425688fb82aaff82452f3699ae5e275b5fb2f6d4759079b07b1b7f641"
)
V26_SMOKE_RUNTIME_SHA256 = (
    "e5c36ce0dfd244869d622db8b12a05e120f6faf4a5407f651849a2dd539ec12b"
)
V26_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "34b7cfacebf3c3998a01b1432a3b7b5a10efc51ea23c4e807365b085f838e62f"
)
V27_RUNTIME_SHA256 = (
    "fac92d4b69431f4e3ce8f4839adbea5772862eb2f6dceb3f5271309a7a914c50"
)
V27_SMOKE_RUNTIME_SHA256 = (
    "af2ecb0e0a338643ec31debc523590260efdc2a7b22fc1fb60b4a227b64cbd3a"
)
V27_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "6b50e59233cb2552c8160c6d2293aa6158920b14235ff4fdc602f65f1b567c7c"
)
V28_RUNTIME_SHA256 = (
    "7925f0de66f0d7fd7412dfae3e9754dcbceb8d0516b875552f98b3f637225fba"
)
V28_SMOKE_RUNTIME_SHA256 = (
    "de9599ac4d22582fea1746eda8deb885e6bc9f2c1d79c9fd723de299cc0edb74"
)
V28_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "af28c19dafb2f01461c5b3c3a648a7ac4f200a71880a965b2ff9da892792fa84"
)
V29_RUNTIME_SHA256 = (
    "162531c501ba866b51033af411b5debb7d1e805c254d5a8fedcd29b59337a026"
)
V29_SMOKE_RUNTIME_SHA256 = (
    "5780c65662cbb1312f61f753e8d66237005ef666264bf99351b2d33f15d6680c"
)
V29_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "4042c313856f722a5abef6e537a8a73de1699dfdde17da0d9ab2bc8a628c995a"
)
V30_RUNTIME_SHA256 = (
    "a0dfe6f503b0e013e757707697221c63711074d39490e7075887754987a22a44"
)
V30_SMOKE_RUNTIME_SHA256 = (
    "6e8540045d60601810672d8f84adc8b4b7800bf5989e386ae39c3f6fa0d22beb"
)
V30_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "14429819aefef1a4e664f3fd32837cf659a5a10203929526e1137bc392389862"
)
V31_RUNTIME_SHA256 = (
    "935e3f2418b3d24e4e535fcffb4ebc096eafe4ccc93f5468331e4c8d9552621c"
)
V31_SMOKE_RUNTIME_SHA256 = (
    "d7026d8577928eb4660dd54240f7ce014a77f46d037b67801a7c0c6764706203"
)
V31_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "2a01a9cfad5b5ca7df9dfe3ce9b58bf894f0a1e7da523423ba8b98b5177fafe1"
)
V32_RUNTIME_SHA256 = (
    "5771dcfa48a4d6550231221b4a7dd409af190b6389797511c1e84419e1b4b395"
)
V32_SMOKE_RUNTIME_SHA256 = (
    "5a27efa53d8324068c67ead555a304c77d7727d82b69bb1d84f4cc80b6d46e74"
)
V32_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "45da6535ee0bf9035b9f61375e03afd7737b6f0d8920431cb5f0248a58a06f86"
)
V33_RUNTIME_SHA256 = (
    "597bddecd5ada141dfaf1830cabb645fa5f50aaf20abd198c134a48e2d1e4e2b"
)
V33_SMOKE_RUNTIME_SHA256 = (
    "c0b96dfaaf73a374113a0c6ba98f87c06a934eb6b40202d96a51b6182c714c05"
)
V33_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "18d9dd6841b3dc69a3797d9473aa0f14df2bc793cf3354abd67be91000e7007e"
)
V34_RUNTIME_SHA256 = (
    "73dc1bd18235fe2ef9a565b2486cd48ba49fe10b3e89adc7666d5baee6ba985b"
)
V34_SMOKE_RUNTIME_SHA256 = (
    "b004afbe823f75bc96521dcc7202cfdbb91930eea6286730f17e316308c0107f"
)
V34_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "f79fa73e1af6c2be6b2284d9d9a0b56224dcca94c153f7e91e2220ae49266d5e"
)
V35_RUNTIME_SHA256 = (
    "05ffcdc81d0cd0ec8a264cd0d5545e88fbf14dba1569629e6ec0f02c3a16615c"
)
V35_SMOKE_RUNTIME_SHA256 = (
    "785abe70a6500a66c331e00dd81eeada1065457ff4dace4e6f9035b9006aaee6"
)
V35_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "4fa44f57128bc096c7fd40bbf9c054c184a273d07ca8f2bdc43fdda7c5e93ad2"
)
V36_RUNTIME_SHA256 = (
    "5b088b4d3e2a0a484f2829664b94db6d51e32fb0e8a0bc904fdf342098a85c89"
)
V36_SMOKE_RUNTIME_SHA256 = (
    "3ad3f26401c190827591351b21578e2b3ec088f0222e8262f2cfa6f29a402c7a"
)
V36_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "34df26b4aaff8c3f417d1634aa1e52e7f40551b8265c2830720456dc068acc81"
)
FROZEN_RUNTIME_SHA256 = (
    "a05f26983a34f626f95d326847db7a049a05c2258fdadf3bbaddd4ec3850c5d7"
)
FROZEN_SMOKE_RUNTIME_SHA256 = (
    "1eda50b8e2887ab4d0f1763816f82344136dabf480d752f5a4d82f48272e8f63"
)
FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256 = (
    "7aa68f246e06bee0a666734113e5a5bda7a747c912b3cc51db6f81d03b2a6d81"
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
_FAULT_CONTAINMENT_EVIDENCE_START_TOKEN = (
    "{{fault_containment_evidence_start_monotonic_ns}}"
)
EXCLUDED_REPAIR_SMOKE_PROBE_MODE_V1 = (
    "exact_once_post_fault_epoch1_responsive_internal_child_v1"
)
EXCLUDED_REPAIR_SMOKE_PROBE_OPTION = (
    "--experiment-response-evidence-duplicate-probe"
)
EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V1 = (
    "byzantine.window.duration_s:450->300"
)
EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2 = (
    "byzantine.window.duration_s:450->330"
)
_FAULT_CONTAINMENT_COVERAGE_READY_EVENT = (
    "adaptive_v2.fault_containment_coverage_ready"
)
_FAULT_CONTAINMENT_TREE_COVERAGE_RULE = "all_exact_predecessor_tree_ids_v1"
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

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        if self.transition_observation_bound_rule == "phase_deadline_v1":
            document.pop("transition_observation_bound_rule")
        return document


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
    only_hard_cohort_is_wait_exempt: bool
    all_worse_replicas_are_physical_leaves: bool
    root_and_internal_roles_are_fast_only: bool
    roots_equal_live_top_q_fast_replicas: bool

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        if not self.only_hard_cohort_is_wait_exempt:
            for field in (
                "only_hard_cohort_is_wait_exempt",
                "all_worse_replicas_are_physical_leaves",
                "root_and_internal_roles_are_fast_only",
                "roots_equal_live_top_q_fast_replicas",
            ):
                document.pop(field)
        return document


@dataclass(frozen=True, slots=True)
class TieredCohortContract(_Document):
    mode: str
    hard_actor_ids: tuple[int, ...]
    responsive_degraded_actor_ids: tuple[int, ...]
    fast_replica_ids: tuple[int, ...]
    responsive_omission_period: int
    responsive_actor_schedule: str
    max_omissions_per_proposal: int
    hard_cohort_wait_exempt: bool
    responsive_degraded_cohort_wait_exempt: bool
    observer_isolation: str
    tiered_marker_schedule_required: bool
    responsive_degraded_rank_below_every_fast_replica: bool
    epoch1_responsive_degraded_are_roots: bool
    epoch1_responsive_degraded_internal_role_exposure_required: bool
    pending_attempt_retention: str | None = None
    causal_timeout_linkage: str | None = None
    causal_timeout_provenance_window: str | None = None
    causal_internal_witness_candidates: str | None = None
    causal_selection_linkage_window: str | None = None
    marker_completeness_witness: str | None = None
    causal_timeout_eligibility: str | None = None
    precontainment_fault_coverage_gate: str | None = None

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        for field in (
            "pending_attempt_retention",
            "causal_timeout_linkage",
            "causal_timeout_provenance_window",
            "causal_internal_witness_candidates",
            "causal_selection_linkage_window",
            "marker_completeness_witness",
            "causal_timeout_eligibility",
            "precontainment_fault_coverage_gate",
        ):
            if document[field] is None:
                document.pop(field)
        return document


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
    precontainment_fault_coverage_gate: str | None = None
    precontainment_coverage_ready_event_type: str | None = None
    precontainment_required_tree_coverage_rule: str | None = None
    precontainment_shape_evaluation_contract: str | None = None
    precontainment_guarded_selection_contract: str | None = None
    future_tree_proposal_delivery_contract: str | None = None
    source_bound_proposal_witness_contract: str | None = None
    evidence_snapshot_selection_contract: str | None = None
    inherited_consensus_wait_exempt_placement_contract: str | None = None
    verified_response_duplicate_delivery_contract: str | None = None
    excluded_repair_smoke_observation_contract: str | None = None
    excluded_repair_smoke_verified_response_duplicate_probe_contract: str | None = (
        None
    )
    post_final_convergence_unmatched_commit_evidence_contract: str | None = None
    epoch1_preselection_residency_ms: int | None = None
    minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection: (
        int | None
    ) = None

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        for field in (
            "precontainment_fault_coverage_gate",
            "precontainment_coverage_ready_event_type",
            "precontainment_required_tree_coverage_rule",
            "precontainment_shape_evaluation_contract",
            "precontainment_guarded_selection_contract",
            "future_tree_proposal_delivery_contract",
            "source_bound_proposal_witness_contract",
            "evidence_snapshot_selection_contract",
            "inherited_consensus_wait_exempt_placement_contract",
            "verified_response_duplicate_delivery_contract",
            "excluded_repair_smoke_observation_contract",
            "excluded_repair_smoke_verified_response_duplicate_probe_contract",
            "post_final_convergence_unmatched_commit_evidence_contract",
            "epoch1_preselection_residency_ms",
            "minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection",
        ):
            if document[field] is None:
                document.pop(field)
        return document


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
class ExcludedRepairSmokeProbeContract(_Document):
    source_campaign_slot_id: str
    source_campaign_result_path: str
    source_campaign_artifact_id: str
    semantic_delta: str
    source_fault_window_duration_s: int
    effective_fault_window_duration_s: int
    hard_timeout_s: int
    observation_contract: str
    verified_response_duplicate_probe_contract: str
    verified_response_duplicate_probe_mode: str


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
    tiered_cohorts: TieredCohortContract | None
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
    excluded_repair_smoke_probe: ExcludedRepairSmokeProbeContract | None = None
    cleanup_contract: str | None = None

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        document["fault_window"] = self.fault_window.as_document()
        document["causal_acceptance"] = self.causal_acceptance.as_document()
        document["epoch1_placement"] = self.epoch1_placement.as_document()
        document["epoch2_placement"] = self.epoch2_placement.as_document()
        if self.cleanup_contract is None:
            document.pop("cleanup_contract")
        if self.excluded_repair_smoke_probe is None:
            document.pop("excluded_repair_smoke_probe")
        if self.tiered_cohorts is None:
            document.pop("tiered_cohorts")
        else:
            document["tiered_cohorts"] = self.tiered_cohorts.as_document()
        return document


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
        document["slots"] = tuple(slot.as_document() for slot in self.slots)
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


def _responsive_actor_schedule(mode: str, omission_period: int) -> str:
    if mode == "tiered_persistent_responsive_omission_v2":
        return RESPONSIVE_ROLE_SCOPED_SCHEDULE_V1
    if mode != "tiered_persistent_responsive_omission_v1":
        raise FactorialManifestError("unknown tiered Byzantine omission mode")
    if omission_period == 41:
        return (
            "omit_every_41st_unique_non_root_contribution_per_responsive_"
            "degraded_actor_v2"
        )
    if omission_period == 32:
        return (
            "omit_every_32nd_unique_non_root_contribution_per_responsive_"
            "degraded_actor_v1"
        )
    raise FactorialManifestError("unknown responsive omission period")


def _tiered_cohort_contract(
    slot: FactorialSlot,
) -> TieredCohortContract | None:
    responsive = slot.byzantine.responsive_degradation
    if responsive is None:
        if slot.responsive_degraded_actor_ids or slot.fast_replica_ids:
            raise FactorialManifestError(
                "legacy Byzantine mode cannot carry tiered cohort identities"
            )
        return None
    if slot.byzantine.mode not in {
        "tiered_persistent_responsive_omission_v1",
        "tiered_persistent_responsive_omission_v2",
    }:
        raise FactorialManifestError(
            "responsive-degradation contract requires the frozen tiered mode"
        )
    hard = slot.byzantine_actor_ids
    degraded = slot.responsive_degraded_actor_ids
    fast = slot.fast_replica_ids
    worse = frozenset((*hard, *degraded))
    measurement_contract = (
        responsive.pending_attempt_retention,
        responsive.causal_timeout_linkage,
        responsive.causal_timeout_provenance_window,
        responsive.causal_internal_witness_candidates,
        responsive.causal_selection_linkage_window,
        responsive.marker_completeness_witness,
        responsive.causal_timeout_eligibility,
    )
    if measurement_contract not in {
        (None, None, None, None, None, None, None),
        (
            RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
            RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
            RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
            RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1,
            RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1,
            RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1,
            None,
            None,
        ),
        (
            RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
            RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
            RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1,
            RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1,
            RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1,
            RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1,
            RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1,
        ),
        (
            RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
            RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
            RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1,
            RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1,
            RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1,
            RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2,
            RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1,
        ),
        (
            RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
            RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
            RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1,
            RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1,
            RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1,
            RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2,
            RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2,
        ),
        (
            RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
            RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
            RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1,
            RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1,
            RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1,
            RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2,
            RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V3,
        ),
    }:
        raise FactorialManifestError(
            "responsive-degradation measurement contract drifted"
        )
    if responsive.precontainment_fault_coverage_gate not in {
        None,
        PRECONTAINMENT_FAULT_COVERAGE_GATE_V1,
    }:
        raise FactorialManifestError(
            "responsive-degradation precontainment coverage gate drifted"
        )
    expected_responsive_period = (
        41
        if measurement_contract[0] == RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1
        else 32
    )
    expected_responsive_schedule = _responsive_actor_schedule(
        slot.byzantine.mode,
        expected_responsive_period,
    )
    expected_fast = tuple(
        member for member in range(slot.replica_count) if member not in worse
    )
    if (
        len(hard) != slot.byzantine.actor_count
        or len(degraded) != slot.f - len(hard)
        or len(worse) != slot.f
        or len(fast) != slot.q
        or fast != expected_fast
        or 0 not in fast
        or any(actor < slot.q or actor >= slot.replica_count for actor in hard)
        or any(actor < 1 or actor >= slot.q for actor in degraded)
        or responsive.omission_period != expected_responsive_period
        or responsive.actor_schedule != expected_responsive_schedule
        or slot.maximum_omissions_per_proposal != slot.f
    ):
        raise FactorialManifestError("slot tiered cohort derivation drifted")
    return TieredCohortContract(
        mode=slot.byzantine.mode,
        hard_actor_ids=hard,
        responsive_degraded_actor_ids=degraded,
        fast_replica_ids=fast,
        responsive_omission_period=responsive.omission_period,
        responsive_actor_schedule=responsive.actor_schedule,
        max_omissions_per_proposal=slot.maximum_omissions_per_proposal,
        hard_cohort_wait_exempt=True,
        responsive_degraded_cohort_wait_exempt=False,
        observer_isolation=responsive.observer_isolation,
        tiered_marker_schedule_required=True,
        responsive_degraded_rank_below_every_fast_replica=True,
        epoch1_responsive_degraded_are_roots=True,
        epoch1_responsive_degraded_internal_role_exposure_required=True,
        pending_attempt_retention=responsive.pending_attempt_retention,
        causal_timeout_linkage=responsive.causal_timeout_linkage,
        causal_timeout_provenance_window=(
            responsive.causal_timeout_provenance_window
        ),
        causal_internal_witness_candidates=(
            responsive.causal_internal_witness_candidates
        ),
        causal_selection_linkage_window=(
            responsive.causal_selection_linkage_window
        ),
        marker_completeness_witness=responsive.marker_completeness_witness,
        causal_timeout_eligibility=responsive.causal_timeout_eligibility,
        precontainment_fault_coverage_gate=(
            responsive.precontainment_fault_coverage_gate
        ),
    )


def _placement_contract(
    policy_intent: str,
    *,
    tiered: bool,
) -> PlacementAcceptanceContract:
    optimized = policy_intent == "performance_optimization"
    optimized_tiered = optimized and tiered
    return PlacementAcceptanceContract(
        policy_intent=policy_intent,
        actors_are_wait_exempt_leaves=True,
        actor_truth_is_policy_input=False,
        roots_equal_live_highest_ranked_eligible=optimized,
        internal_assignment_uses_live_evidence_ranking=True,
        influential_order_source="live_accepted_evidence_ranking",
        only_hard_cohort_is_wait_exempt=tiered,
        all_worse_replicas_are_physical_leaves=optimized_tiered,
        root_and_internal_roles_are_fast_only=optimized_tiered,
        roots_equal_live_top_q_fast_replicas=optimized_tiered,
    )


def _causal_acceptance(
    coverage_gate: str | None,
    shape_evaluation_contract: str | None,
    guarded_selection_contract: str | None,
    future_tree_proposal_delivery_contract: str | None,
    source_bound_proposal_witness_contract: str | None,
    evidence_snapshot_selection_contract: str | None,
    inherited_wait_exempt_placement_contract: str | None,
    verified_response_duplicate_delivery_contract: str | None,
    excluded_repair_smoke_observation_contract: str | None,
    excluded_repair_smoke_duplicate_probe_contract: str | None,
    post_final_convergence_unmatched_commit_evidence_contract: str | None,
    epoch1_preselection_residency_ms: int | None,
    minimum_primary_internal_opportunities: int | None,
) -> CausalAcceptanceContract:
    if coverage_gate not in {None, PRECONTAINMENT_FAULT_COVERAGE_GATE_V1}:
        raise FactorialManifestError(
            "causal acceptance precontainment coverage gate drifted"
        )
    if shape_evaluation_contract not in {
        None,
        PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1,
    }:
        raise FactorialManifestError(
            "causal acceptance precontainment shape evaluation contract drifted"
        )
    if shape_evaluation_contract is not None and coverage_gate is None:
        raise FactorialManifestError(
            "precontainment shape evaluation requires the coverage-gated contract"
        )
    if guarded_selection_contract not in {
        None,
        PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1,
    }:
        raise FactorialManifestError(
            "causal acceptance precontainment guarded selection contract drifted"
        )
    if guarded_selection_contract is not None and (
        coverage_gate is None or shape_evaluation_contract is None
    ):
        raise FactorialManifestError(
            "guarded selection domains require coverage and shape contracts"
        )
    if future_tree_proposal_delivery_contract not in {
        None,
        FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1,
        FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2,
    }:
        raise FactorialManifestError(
            "causal acceptance future-tree proposal delivery contract drifted"
        )
    if future_tree_proposal_delivery_contract is not None and (
        guarded_selection_contract is None
    ):
        raise FactorialManifestError(
            "future-tree proposal delivery requires guarded selection domains"
        )
    if source_bound_proposal_witness_contract not in {
        None,
        SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1,
    }:
        raise FactorialManifestError(
            "causal acceptance source-bound proposal witness contract drifted"
        )
    if source_bound_proposal_witness_contract is not None and (
        future_tree_proposal_delivery_contract is None
    ):
        raise FactorialManifestError(
            "source-bound proposal witnesses require future-tree delivery"
        )
    if evidence_snapshot_selection_contract not in {
        None,
        EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1,
    }:
        raise FactorialManifestError(
            "causal acceptance evidence snapshot selection contract drifted"
        )
    if evidence_snapshot_selection_contract is not None and (
        source_bound_proposal_witness_contract is None
    ):
        raise FactorialManifestError(
            "evidence snapshot selection requires source-bound proposal witnesses"
        )
    if inherited_wait_exempt_placement_contract not in {
        None,
        INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT_V1,
    }:
        raise FactorialManifestError(
            "causal acceptance inherited wait-exempt placement contract drifted"
        )
    if inherited_wait_exempt_placement_contract is not None and (
        evidence_snapshot_selection_contract is None
    ):
        raise FactorialManifestError(
            "inherited wait-exempt placement requires evidence snapshot selection"
        )
    if verified_response_duplicate_delivery_contract not in {
        None,
        VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V1,
        VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2,
    }:
        raise FactorialManifestError(
            "causal acceptance verified-response duplicate delivery contract drifted"
        )
    if verified_response_duplicate_delivery_contract is not None and (
        inherited_wait_exempt_placement_contract is None
    ):
        raise FactorialManifestError(
            "verified-response duplicate delivery requires inherited placement"
        )
    if excluded_repair_smoke_observation_contract not in {
        None,
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V1,
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2,
        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3,
    }:
        raise FactorialManifestError(
            "causal acceptance excluded repair observation contract drifted"
        )
    if excluded_repair_smoke_duplicate_probe_contract not in {
        None,
        EXCLUDED_REPAIR_SMOKE_VERIFIED_RESPONSE_DUPLICATE_PROBE_CONTRACT_V1,
    }:
        raise FactorialManifestError(
            "causal acceptance excluded repair duplicate probe contract drifted"
        )
    if (excluded_repair_smoke_observation_contract is None) != (
        excluded_repair_smoke_duplicate_probe_contract is None
    ):
        raise FactorialManifestError(
            "causal acceptance excluded repair smoke contracts must be paired"
        )
    if excluded_repair_smoke_observation_contract is not None and (
        verified_response_duplicate_delivery_contract
        != VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2
    ):
        raise FactorialManifestError(
            "excluded repair smoke contracts require duplicate delivery v2"
        )
    if post_final_convergence_unmatched_commit_evidence_contract not in {
        None,
        POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1,
    }:
        raise FactorialManifestError(
            "causal acceptance post-final-convergence unmatched commit "
            "evidence contract drifted"
        )
    if (
        post_final_convergence_unmatched_commit_evidence_contract is not None
        and excluded_repair_smoke_observation_contract
        not in {
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2,
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3,
        }
    ):
        raise FactorialManifestError(
            "post-final-convergence unmatched commit evidence requires the "
            "repair observation v2 contract"
        )
    if epoch1_preselection_residency_ms not in {None, 60_000}:
        raise FactorialManifestError(
            "causal acceptance Epoch-1 preselection residency drifted"
        )
    if minimum_primary_internal_opportunities not in {None, 82}:
        raise FactorialManifestError(
            "causal acceptance primary N31/f5 internal opportunity minimum drifted"
        )
    if (epoch1_preselection_residency_ms is None) != (
        minimum_primary_internal_opportunities is None
    ):
        raise FactorialManifestError(
            "causal acceptance v24/v25/v26/v27/v28/v29/v30/v31/v32/v33/v34/v35/v36/v37 timing and opportunity "
            "gates must "
            "be paired"
        )
    return CausalAcceptanceContract(
        proof_source="independent_raw_artifact_validation",
        pre_epoch1_required_role="internal",
        pre_epoch1_required_action="omit_aggregate",
        pre_epoch1_actor_coverage_rule="each_declared_actor_has_source_bound_marker",
        post_containment_required_role="leaf",
        post_containment_wait_exempt=True,
        post_containment_actor_coverage_rule="every_declared_actor",
        declared_or_synthetic_outcomes_accepted=False,
        precontainment_fault_coverage_gate=coverage_gate,
        precontainment_coverage_ready_event_type=(
            _FAULT_CONTAINMENT_COVERAGE_READY_EVENT
            if coverage_gate is not None
            else None
        ),
        precontainment_required_tree_coverage_rule=(
            _FAULT_CONTAINMENT_TREE_COVERAGE_RULE
            if coverage_gate is not None
            else None
        ),
        precontainment_shape_evaluation_contract=shape_evaluation_contract,
        precontainment_guarded_selection_contract=guarded_selection_contract,
        future_tree_proposal_delivery_contract=(
            future_tree_proposal_delivery_contract
        ),
        source_bound_proposal_witness_contract=(
            source_bound_proposal_witness_contract
        ),
        evidence_snapshot_selection_contract=(
            evidence_snapshot_selection_contract
        ),
        inherited_consensus_wait_exempt_placement_contract=(
            inherited_wait_exempt_placement_contract
        ),
        verified_response_duplicate_delivery_contract=(
            verified_response_duplicate_delivery_contract
        ),
        excluded_repair_smoke_observation_contract=(
            excluded_repair_smoke_observation_contract
        ),
        excluded_repair_smoke_verified_response_duplicate_probe_contract=(
            excluded_repair_smoke_duplicate_probe_contract
        ),
        post_final_convergence_unmatched_commit_evidence_contract=(
            post_final_convergence_unmatched_commit_evidence_contract
        ),
        epoch1_preselection_residency_ms=epoch1_preselection_residency_ms,
        minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection=(
            minimum_primary_internal_opportunities
        ),
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
            slot.workload.epoch1_preselection_residency_ms
            if slot.workload.epoch1_preselection_residency_ms is not None
            else (
                slot.workload.epoch1_stable_bucket_count
                * slot.workload.bucket_width_s
                * 1_000
            )
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
    responsive = slot.byzantine.responsive_degradation
    if (
        responsive is not None
        and responsive.precontainment_fault_coverage_gate
        == PRECONTAINMENT_FAULT_COVERAGE_GATE_V1
    ):
        command.extend(
            (
                "--fault-containment-evidence-start-monotonic-ns",
                _FAULT_CONTAINMENT_EVIDENCE_START_TOKEN,
                "--fault-containment-required-tree-coverage",
                str(slot.replica_count),
            )
        )
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
    tiered: TieredCohortContract | None,
    excluded_repair_smoke_probe_mode: str | None,
) -> tuple[ReplicaProcessSpec, ...]:
    actors = ",".join(map(str, slot.byzantine_actor_ids))
    window_suffix = {
        "rotating_intermittent_omission_v1": "rotating-omission-v1",
        "persistent_selected_omission_v1": "persistent-omission-v1",
        "tiered_persistent_responsive_omission_v1": (
            "tiered-responsive-omission-v1"
        ),
        "tiered_persistent_responsive_omission_v2": (
            "tiered-responsive-omission-v2"
        ),
    }.get(slot.byzantine.mode)
    if window_suffix is None:
        raise FactorialManifestError("unknown Byzantine omission mode")
    window_id = f"{slot.block_id}-{window_suffix}"
    tiered_arguments = (
        (
            "--experiment-responsive-degraded-omission-actors",
            ",".join(map(str, tiered.responsive_degraded_actor_ids)),
            "--experiment-responsive-omission-period",
            str(tiered.responsive_omission_period),
        )
        if tiered is not None
        else ()
    )
    probe_arguments = (
        (
            EXCLUDED_REPAIR_SMOKE_PROBE_OPTION,
            excluded_repair_smoke_probe_mode,
        )
        if excluded_repair_smoke_probe_mode is not None
        else ()
    )
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
            *tiered_arguments,
            *probe_arguments,
            "--experiment-byzantine-max-omissions-per-proposal",
            str(slot.maximum_omissions_per_proposal),
            "--experiment-rotating-omission-context-limit",
            str(slot.byzantine.maximum_rotating_contexts),
        )
        result.append(ReplicaProcessSpec(replica_id=replica_id, argv=argv))
    return tuple(result)


def _validate_excluded_repair_smoke_probe(
    slot: FactorialSlot,
    probe: ExcludedRepairSmokeProbeContract | None,
) -> None:
    if probe is None:
        return
    if not isinstance(probe, ExcludedRepairSmokeProbeContract):
        raise FactorialManifestError(
            "excluded repair smoke probe must use the frozen runtime contract"
        )
    versioned_paths = {
        version: (
            f"results/shape-placement-factorial-{version}/"
            "slot-037-n31-f2-b04-00",
            f"results/shape-placement-factorial-{version}-coverage-smoke/"
            "slot-037-n31-f2-b04-00",
        )
        for version in (
            "v28",
            "v29",
            "v30",
            "v31",
            "v32",
            "v33",
            "v34",
            "v35",
            "v36",
            "v37",
        )
    }
    matching_paths = tuple(
        (version, paths)
        for version, paths in versioned_paths.items()
        if slot.result_path == paths[1]
    )
    if (
        len(matching_paths) != 1
        or slot.slot_id != "slot-037-n31-f2-b04-00"
        or slot.byzantine.duration_s
        != (330 if matching_paths and matching_paths[0][0] == "v37" else 300)
        or slot.common_timers.hard_timeout_s != 650
    ):
        raise FactorialManifestError(
            "excluded repair smoke probe is restricted to exact v28/v29/v30/v31/v32/v33/v34/v35/v36/v37 slot 037"
        )
    version, (source_path, _) = matching_paths[0]
    effective_duration_s = 330 if version == "v37" else 300
    source_slot = replace(
        slot,
        result_path=source_path,
        byzantine=replace(slot.byzantine, duration_s=450),
    )
    source_runtime = build_slot_runtime(source_slot)
    expected = ExcludedRepairSmokeProbeContract(
        source_campaign_slot_id=source_slot.slot_id,
        source_campaign_result_path=source_path,
        source_campaign_artifact_id=source_runtime.artifact_id,
        semantic_delta=(
            EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2
            if version == "v37"
            else EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V1
        ),
        source_fault_window_duration_s=450,
        effective_fault_window_duration_s=effective_duration_s,
        hard_timeout_s=650,
        observation_contract=(
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
            if version == "v37"
            else EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
            if version in {"v34", "v35", "v36"}
            else EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V1
        ),
        verified_response_duplicate_probe_contract=(
            EXCLUDED_REPAIR_SMOKE_VERIFIED_RESPONSE_DUPLICATE_PROBE_CONTRACT_V1
        ),
        verified_response_duplicate_probe_mode=(
            EXCLUDED_REPAIR_SMOKE_PROBE_MODE_V1
        ),
    )
    if probe != expected:
        raise FactorialManifestError(
            "excluded repair smoke probe source, delta, contract, mode, or timing drifted"
        )


def build_slot_runtime(
    slot: FactorialSlot,
    *,
    excluded_repair_smoke_probe: ExcludedRepairSmokeProbeContract | None = None,
) -> SlotRuntimeSpec:
    """Build one pure slot contract without sampling clocks or live evidence."""

    if not isinstance(slot, FactorialSlot):
        raise FactorialManifestError("slot runtime input must be a FactorialSlot")
    _validate_excluded_repair_smoke_probe(slot, excluded_repair_smoke_probe)
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
    tiered_cohorts = _tiered_cohort_contract(slot)
    identity = {
        "arm_code": slot.arm_code,
        "block_id": slot.block_id,
        "scientific_seed": slot.scientific_seed,
        "slot_id": slot.slot_id,
        "slot_nonce": slot.slot_nonce,
    }
    if tiered_cohorts is not None:
        identity.update(
            {
                "byzantine_mode": slot.byzantine.mode,
                "hard_actor_ids": slot.byzantine_actor_ids,
                "responsive_degraded_actor_ids": (
                    slot.responsive_degraded_actor_ids
                ),
                "fast_replica_ids": slot.fast_replica_ids,
                "max_omissions_per_proposal": (
                    slot.maximum_omissions_per_proposal
                ),
                "responsive_omission_period": (
                    tiered_cohorts.responsive_omission_period
                ),
            }
        )
        if tiered_cohorts.mode == "tiered_persistent_responsive_omission_v2":
            identity["responsive_actor_schedule"] = (
                tiered_cohorts.responsive_actor_schedule
            )
        if tiered_cohorts.pending_attempt_retention is not None:
            identity.update(
                {
                    "pending_attempt_retention": (
                        tiered_cohorts.pending_attempt_retention
                    ),
                    "causal_timeout_linkage": (
                        tiered_cohorts.causal_timeout_linkage
                    ),
                }
            )
        if tiered_cohorts.causal_timeout_provenance_window is not None:
            identity.update(
                {
                    "causal_timeout_provenance_window": (
                        tiered_cohorts.causal_timeout_provenance_window
                    ),
                    "causal_internal_witness_candidates": (
                        tiered_cohorts.causal_internal_witness_candidates
                    ),
                    "causal_selection_linkage_window": (
                        tiered_cohorts.causal_selection_linkage_window
                    ),
                }
            )
        if tiered_cohorts.marker_completeness_witness is not None:
            identity.update(
                {
                    "marker_completeness_witness": (
                        tiered_cohorts.marker_completeness_witness
                    ),
                    "causal_timeout_eligibility": (
                        tiered_cohorts.causal_timeout_eligibility
                    ),
                }
            )
        if tiered_cohorts.precontainment_fault_coverage_gate is not None:
            identity["precontainment_fault_coverage_gate"] = (
                tiered_cohorts.precontainment_fault_coverage_gate
            )
        responsive = slot.byzantine.responsive_degradation
        if (
            responsive is not None
            and responsive.precontainment_shape_evaluation_contract is not None
        ):
            identity["precontainment_shape_evaluation_contract"] = (
                responsive.precontainment_shape_evaluation_contract
            )
        if (
            responsive is not None
            and responsive.precontainment_guarded_selection_contract is not None
        ):
            identity["precontainment_guarded_selection_contract"] = (
                responsive.precontainment_guarded_selection_contract
            )
        if (
            responsive is not None
            and responsive.future_tree_proposal_delivery_contract is not None
        ):
            identity["future_tree_proposal_delivery_contract"] = (
                responsive.future_tree_proposal_delivery_contract
            )
        if (
            responsive is not None
            and responsive.source_bound_proposal_witness_contract is not None
        ):
            identity["source_bound_proposal_witness_contract"] = (
                responsive.source_bound_proposal_witness_contract
            )
        if (
            responsive is not None
            and responsive.evidence_snapshot_selection_contract is not None
        ):
            identity["evidence_snapshot_selection_contract"] = (
                responsive.evidence_snapshot_selection_contract
            )
        if (
            responsive is not None
            and responsive.inherited_consensus_wait_exempt_placement_contract
            is not None
        ):
            identity["inherited_consensus_wait_exempt_placement_contract"] = (
                responsive.inherited_consensus_wait_exempt_placement_contract
            )
        if (
            responsive is not None
            and responsive.verified_response_duplicate_delivery_contract is not None
        ):
            identity["verified_response_duplicate_delivery_contract"] = (
                responsive.verified_response_duplicate_delivery_contract
            )
            if (
                responsive.verified_response_duplicate_delivery_contract
                == VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2
            ):
                identity.update(
                    {
                        "fault_window_duration_s": slot.byzantine.duration_s,
                        "hard_timeout_s": slot.common_timers.hard_timeout_s,
                    }
                )
        if (
            responsive is not None
            and responsive.excluded_repair_smoke_observation_contract is not None
        ):
            identity.update(
                {
                    "excluded_repair_smoke_observation_contract": (
                        responsive.excluded_repair_smoke_observation_contract
                    ),
                    "excluded_repair_smoke_verified_response_duplicate_probe_contract": (
                        responsive.excluded_repair_smoke_verified_response_duplicate_probe_contract
                    ),
                }
            )
        if (
            responsive is not None
            and responsive.post_final_convergence_unmatched_commit_evidence_contract
            is not None
        ):
            identity[
                "post_final_convergence_unmatched_commit_evidence_contract"
            ] = (
                responsive.post_final_convergence_unmatched_commit_evidence_contract
            )
        if slot.workload.epoch1_preselection_residency_ms is not None:
            identity["epoch1_preselection_residency_ms"] = (
                slot.workload.epoch1_preselection_residency_ms
            )
        if (
            responsive is not None
            and responsive.minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection
            is not None
        ):
            identity[
                "minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection"
            ] = responsive.minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection
    if slot.cleanup_contract is not None:
        identity["cleanup_contract"] = slot.cleanup_contract
    if excluded_repair_smoke_probe is not None:
        identity["excluded_repair_smoke_probe"] = (
            excluded_repair_smoke_probe.as_document()
        )
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
        tiered_cohorts=tiered_cohorts,
        result_path=slot.result_path,
        responsiveness_policy=slot.responsiveness_policy,
        fault_window=_fault_window(slot),
        cutoff_contract=_cutoff_contract(slot),
        shape_invocation=_shape_invocation(slot),
        causal_acceptance=_causal_acceptance(
            (
                tiered_cohorts.precontainment_fault_coverage_gate
                if tiered_cohorts is not None
                else None
            ),
            (
                slot.byzantine.responsive_degradation.precontainment_shape_evaluation_contract
                if slot.byzantine.responsive_degradation is not None
                else None
            ),
            (
                slot.byzantine.responsive_degradation.precontainment_guarded_selection_contract
                if slot.byzantine.responsive_degradation is not None
                else None
            ),
            (
                slot.byzantine.responsive_degradation.future_tree_proposal_delivery_contract
                if slot.byzantine.responsive_degradation is not None
                else None
            ),
            (
                slot.byzantine.responsive_degradation.source_bound_proposal_witness_contract
                if slot.byzantine.responsive_degradation is not None
                else None
            ),
            (
                slot.byzantine.responsive_degradation.evidence_snapshot_selection_contract
                if slot.byzantine.responsive_degradation is not None
                else None
            ),
            (
                slot.byzantine.responsive_degradation.inherited_consensus_wait_exempt_placement_contract
                if slot.byzantine.responsive_degradation is not None
                else None
            ),
            (
                slot.byzantine.responsive_degradation.verified_response_duplicate_delivery_contract
                if slot.byzantine.responsive_degradation is not None
                else None
            ),
            (
                slot.byzantine.responsive_degradation.excluded_repair_smoke_observation_contract
                if slot.byzantine.responsive_degradation is not None
                else None
            ),
            (
                slot.byzantine.responsive_degradation.excluded_repair_smoke_verified_response_duplicate_probe_contract
                if slot.byzantine.responsive_degradation is not None
                else None
            ),
            (
                slot.byzantine.responsive_degradation.post_final_convergence_unmatched_commit_evidence_contract
                if slot.byzantine.responsive_degradation is not None
                else None
            ),
            slot.workload.epoch1_preselection_residency_ms,
            (
                slot.byzantine.responsive_degradation.minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection
                if slot.byzantine.responsive_degradation is not None
                else None
            ),
        ),
        epoch1_placement=_placement_contract(
            epoch1_policy, tiered=tiered_cohorts is not None
        ),
        epoch2_placement=_placement_contract(
            epoch2_policy, tiered=tiered_cohorts is not None
        ),
        transition_sequence=_transition_sequence(slot),
        transitions=transitions,
        main_config=main_config,
        structured_events=structured_events,
        process_logs=process_logs,
        manager_argv_template=_manager_argv_template(
            slot, transitions, structured_events
        ),
        replica_argv_templates=_replica_argv_templates(
            slot,
            main_config,
            structured_events,
            tiered_cohorts,
            (
                excluded_repair_smoke_probe.verified_response_duplicate_probe_mode
                if excluded_repair_smoke_probe is not None
                else None
            ),
        ),
        excluded_repair_smoke_probe=excluded_repair_smoke_probe,
        cleanup_contract=slot.cleanup_contract,
    )


def materialize_manager_argv(
    spec: SlotRuntimeSpec,
    absolute_slot_directory: str | Path,
    secrets: ManagerSecretMaterial,
    *,
    shared_raw_clock_anchor_ns: int | None = None,
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
    manager_template = spec.manager_argv_template.argv
    coverage_enabled = (
        spec.causal_acceptance.precontainment_fault_coverage_gate
        == PRECONTAINMENT_FAULT_COVERAGE_GATE_V1
    )
    coverage_options = (
        "--fault-containment-evidence-start-monotonic-ns",
        "--fault-containment-required-tree-coverage",
    )
    if coverage_enabled:
        if (
            type(shared_raw_clock_anchor_ns) is not int
            or shared_raw_clock_anchor_ns < 0
            or shared_raw_clock_anchor_ns > _MAXIMUM_MONOTONIC_NS
        ):
            raise FactorialManifestError(
                "coverage-gated manager argv requires the shared "
                "CLOCK_MONOTONIC_RAW anchor"
            )
        evidence_start_ns = shared_raw_clock_anchor_ns + (
            spec.fault_window.start_after_prelaunch_anchor_s
            * _NANOSECONDS_PER_SECOND
        )
        if evidence_start_ns > _MAXIMUM_MONOTONIC_NS:
            raise FactorialManifestError(
                "fault-containment evidence start exceeds uint64"
            )
        if (
            manager_template.count(coverage_options[0]) != 1
            or manager_template.count(coverage_options[1]) != 1
            or manager_template[
                manager_template.index(coverage_options[0]) + 1
            ]
            != _FAULT_CONTAINMENT_EVIDENCE_START_TOKEN
            or manager_template[
                manager_template.index(coverage_options[1]) + 1
            ]
            != str(spec.replica_count)
        ):
            raise FactorialManifestError(
                "coverage-gated manager precontainment argv drifted"
            )
    elif any(option in manager_template for option in coverage_options):
        raise FactorialManifestError(
            "legacy manager argv must not carry precontainment coverage options"
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
    if coverage_enabled:
        replacements[_FAULT_CONTAINMENT_EVIDENCE_START_TOKEN] = str(
            evidence_start_ns
        )
    for replica_id, certificate in enumerate(secrets.replica_tls_certificate_der_hex):
        replacements[_replica_certificate_token(replica_id)] = _canonical_hex(
            certificate,
            f"replica-{replica_id} TLS certificate DER",
        )

    materialized: list[str] = []
    for argument in manager_template:
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
        expected_cleanup_contract = (
            EXECUTION_CLEANUP_CONTRACT_V1
            if runtime.manifest_id
            in {
                V14_MANIFEST_ID,
                V15_MANIFEST_ID,
                V16_MANIFEST_ID,
                V17_MANIFEST_ID,
                V18_MANIFEST_ID,
                V19_MANIFEST_ID,
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None
        )
        if slot.cleanup_contract != expected_cleanup_contract:
            raise FactorialManifestError(
                f"slot cleanup contract drifted: {slot.slot_id}"
            )
        manager_template = slot.manager_argv_template.argv
        policy = slot.responsiveness_policy
        expected_residencies_ms = (
            0,
            (
                60_000
                if runtime.manifest_id
                in {
                    V24_MANIFEST_ID,
                    V25_MANIFEST_ID,
                    V26_MANIFEST_ID,
                    V27_MANIFEST_ID,
                    V28_MANIFEST_ID,
                    V29_MANIFEST_ID,
                    V30_MANIFEST_ID,
                    V31_MANIFEST_ID,
                    V32_MANIFEST_ID,
                    V33_MANIFEST_ID,
                    V34_MANIFEST_ID,
                    V35_MANIFEST_ID,
                    V36_MANIFEST_ID,
                    FROZEN_MANIFEST_ID,
                }
                else slot.cutoff_contract.epoch1_stable_bucket_count
                * slot.cutoff_contract.bucket_width_s
                * 1_000
            ),
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
        tiered = slot.tiered_cohorts
        if tiered is not None:
            hard = tiered.hard_actor_ids
            degraded = tiered.responsive_degraded_actor_ids
            worse = frozenset((*hard, *degraded))
            expected_fast = tuple(
                member
                for member in range(slot.replica_count)
                if member not in worse
            )
            optimized = slot.arm_code in {"P", "PS"}
            exact_n7_smoke = (
                slot.slot_id == "smoke-n7-f2-PS"
                and slot.block_id == "n7-f2-smoke-b01"
                and slot.replica_count == 7
                and slot.f == 2
                and slot.q == 5
                and slot.arm_code == "PS"
            )
            expected_hard_count = 1 if exact_n7_smoke else 3
            expected_cohorts = derive_tiered_cohorts(
                slot.replica_count,
                slot.q,
                expected_hard_count,
                slot.scientific_seed,
            )
            expected_measurement_contract = (
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
            if runtime.manifest_id == V9_MANIFEST_ID:
                expected_measurement_contract = (
                    RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
                    RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            elif runtime.manifest_id == V10_MANIFEST_ID:
                expected_measurement_contract = (
                    RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
                    RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
                    RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1,
                    RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1,
                    RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1,
                    None,
                    None,
                )
            elif runtime.manifest_id in {
                V11_MANIFEST_ID,
                V12_MANIFEST_ID,
                V13_MANIFEST_ID,
            }:
                expected_measurement_contract = (
                    RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
                    RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
                    RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1,
                    RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1,
                    RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1,
                    RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1,
                    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1,
                )
            elif runtime.manifest_id in {V14_MANIFEST_ID, V15_MANIFEST_ID}:
                expected_measurement_contract = (
                    RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
                    RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
                    RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1,
                    RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1,
                    RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1,
                    RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2,
                    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1,
                )
            elif runtime.manifest_id in {
                V16_MANIFEST_ID,
                V17_MANIFEST_ID,
                V18_MANIFEST_ID,
                V19_MANIFEST_ID,
                V20_MANIFEST_ID,
            }:
                expected_measurement_contract = (
                    RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
                    RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
                    RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1,
                    RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1,
                    RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1,
                    RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2,
                    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2,
                )
            elif runtime.manifest_id in {
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }:
                expected_measurement_contract = (
                    RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1,
                    RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1,
                    RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1,
                    RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1,
                    RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1,
                    RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2,
                    RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V3,
                )
            expected_responsive_period = (
                41
                if runtime.manifest_id
                in {
                    V9_MANIFEST_ID,
                    V10_MANIFEST_ID,
                    V11_MANIFEST_ID,
                    V12_MANIFEST_ID,
                    V13_MANIFEST_ID,
                    V14_MANIFEST_ID,
                    V15_MANIFEST_ID,
                    V16_MANIFEST_ID,
                    V17_MANIFEST_ID,
                    V18_MANIFEST_ID,
                    V19_MANIFEST_ID,
                    V20_MANIFEST_ID,
                    V21_MANIFEST_ID,
                    V22_MANIFEST_ID,
                    V23_MANIFEST_ID,
                    V24_MANIFEST_ID,
                    V25_MANIFEST_ID,
                    V26_MANIFEST_ID,
                    V27_MANIFEST_ID,
                    V28_MANIFEST_ID,
                    V29_MANIFEST_ID,
                    V30_MANIFEST_ID,
                    V31_MANIFEST_ID,
                    V32_MANIFEST_ID,
                    V33_MANIFEST_ID,
                    V34_MANIFEST_ID,
                    V35_MANIFEST_ID,
                    V36_MANIFEST_ID,
                    FROZEN_MANIFEST_ID,
                }
                else 32
            )
            expected_tiered_mode = (
                "tiered_persistent_responsive_omission_v2"
                if runtime.manifest_id
                in {
                    V12_MANIFEST_ID,
                    V13_MANIFEST_ID,
                    V14_MANIFEST_ID,
                    V15_MANIFEST_ID,
                    V16_MANIFEST_ID,
                    V17_MANIFEST_ID,
                    V18_MANIFEST_ID,
                    V19_MANIFEST_ID,
                    V20_MANIFEST_ID,
                    V21_MANIFEST_ID,
                    V22_MANIFEST_ID,
                    V23_MANIFEST_ID,
                    V24_MANIFEST_ID,
                    V25_MANIFEST_ID,
                    V26_MANIFEST_ID,
                    V27_MANIFEST_ID,
                    V28_MANIFEST_ID,
                    V29_MANIFEST_ID,
                    V30_MANIFEST_ID,
                    V31_MANIFEST_ID,
                    V32_MANIFEST_ID,
                    V33_MANIFEST_ID,
                    V34_MANIFEST_ID,
                    V35_MANIFEST_ID,
                    V36_MANIFEST_ID,
                    FROZEN_MANIFEST_ID,
                }
                else "tiered_persistent_responsive_omission_v1"
            )
            expected_responsive_schedule = _responsive_actor_schedule(
                expected_tiered_mode,
                expected_responsive_period,
            )
            if (
                tiered.mode != expected_tiered_mode
                or hard != slot.actor_ids
                or len(hard) != expected_hard_count
                or hard != expected_cohorts.hard_actor_ids
                or degraded != expected_cohorts.responsive_degraded_actor_ids
                or tiered.fast_replica_ids != expected_cohorts.fast_replica_ids
                or len(degraded) != slot.f - len(hard)
                or len(worse) != slot.f
                or tiered.fast_replica_ids != expected_fast
                or len(expected_fast) != slot.q
                or 0 not in expected_fast
                or any(actor < slot.q for actor in hard)
                or any(actor < 1 or actor >= slot.q for actor in degraded)
                or tiered.responsive_omission_period != expected_responsive_period
                or tiered.responsive_actor_schedule
                != expected_responsive_schedule
                or tiered.max_omissions_per_proposal != slot.f
                or not tiered.hard_cohort_wait_exempt
                or tiered.responsive_degraded_cohort_wait_exempt
                or tiered.observer_isolation
                != "replica_0_reserved_authoritative_commit_observer_v1"
                or not tiered.tiered_marker_schedule_required
                or not tiered.responsive_degraded_rank_below_every_fast_replica
                or not tiered.epoch1_responsive_degraded_are_roots
                or not tiered.epoch1_responsive_degraded_internal_role_exposure_required
                or (
                    tiered.pending_attempt_retention,
                    tiered.causal_timeout_linkage,
                    tiered.causal_timeout_provenance_window,
                    tiered.causal_internal_witness_candidates,
                    tiered.causal_selection_linkage_window,
                    tiered.marker_completeness_witness,
                    tiered.causal_timeout_eligibility,
                )
                != expected_measurement_contract
                or tiered.precontainment_fault_coverage_gate
                != (
                    PRECONTAINMENT_FAULT_COVERAGE_GATE_V1
                    if runtime.manifest_id
                    in {
                        V15_MANIFEST_ID,
                        V16_MANIFEST_ID,
                        V17_MANIFEST_ID,
                        V18_MANIFEST_ID,
                        V19_MANIFEST_ID,
                        V20_MANIFEST_ID,
                        V21_MANIFEST_ID,
                        V22_MANIFEST_ID,
                        V23_MANIFEST_ID,
                        V24_MANIFEST_ID,
                        V25_MANIFEST_ID,
                        V26_MANIFEST_ID,
                        V27_MANIFEST_ID,
                        V28_MANIFEST_ID,
                        V29_MANIFEST_ID,
                        V30_MANIFEST_ID,
                        V31_MANIFEST_ID,
                        V32_MANIFEST_ID,
                        V33_MANIFEST_ID,
                        V34_MANIFEST_ID,
                        V35_MANIFEST_ID,
                        V36_MANIFEST_ID,
                        FROZEN_MANIFEST_ID,
                    }
                    else None
                )
                or not slot.epoch1_placement.only_hard_cohort_is_wait_exempt
                or slot.epoch1_placement.all_worse_replicas_are_physical_leaves
                or slot.epoch1_placement.root_and_internal_roles_are_fast_only
                or slot.epoch1_placement.roots_equal_live_top_q_fast_replicas
                or not slot.epoch2_placement.only_hard_cohort_is_wait_exempt
                or slot.epoch2_placement.all_worse_replicas_are_physical_leaves
                is not optimized
                or slot.epoch2_placement.root_and_internal_roles_are_fast_only
                is not optimized
                or slot.epoch2_placement.roots_equal_live_top_q_fast_replicas
                is not optimized
            ):
                raise FactorialManifestError(
                    f"slot has an invalid tiered cohort contract: {slot.slot_id}"
                )
            hard_csv = ",".join(map(str, hard))
            degraded_csv = ",".join(map(str, degraded))
            for process in slot.replica_argv_templates:
                argv = process.argv
                if (
                    argv.count("--experiment-rotating-omission-actors") != 1
                    or argv.count("--experiment-byzantine-mode") != 1
                    or argv[argv.index("--experiment-byzantine-mode") + 1]
                    != expected_tiered_mode
                    or argv[
                        argv.index("--experiment-rotating-omission-actors") + 1
                    ]
                    != hard_csv
                    or argv.count(
                        "--experiment-responsive-degraded-omission-actors"
                    )
                    != 1
                    or argv[
                        argv.index(
                            "--experiment-responsive-degraded-omission-actors"
                        )
                        + 1
                    ]
                    != degraded_csv
                    or argv.count("--experiment-responsive-omission-period")
                    != 1
                    or argv[
                        argv.index("--experiment-responsive-omission-period") + 1
                    ]
                    != str(expected_responsive_period)
                    or argv.count(
                        "--experiment-byzantine-max-omissions-per-proposal"
                    )
                    != 1
                    or argv[
                        argv.index(
                            "--experiment-byzantine-max-omissions-per-proposal"
                        )
                        + 1
                    ]
                    != str(slot.f)
                ):
                    raise FactorialManifestError(
                        f"slot tiered replica argv drifted: {slot.slot_id}"
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
        coverage_options = (
            "--fault-containment-evidence-start-monotonic-ns",
            "--fault-containment-required-tree-coverage",
        )
        expected_coverage_enabled = runtime.manifest_id in {
            V15_MANIFEST_ID,
            V16_MANIFEST_ID,
            V17_MANIFEST_ID,
            V18_MANIFEST_ID,
            V19_MANIFEST_ID,
            V20_MANIFEST_ID,
            V21_MANIFEST_ID,
            V22_MANIFEST_ID,
            V23_MANIFEST_ID,
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }
        if (
            (manager_template.count(coverage_options[0]) == 1)
            is not expected_coverage_enabled
            or (manager_template.count(coverage_options[1]) == 1)
            is not expected_coverage_enabled
            or (
                expected_coverage_enabled
                and (
                    manager_template[
                        manager_template.index(coverage_options[0]) + 1
                    ]
                    != _FAULT_CONTAINMENT_EVIDENCE_START_TOKEN
                    or manager_template[
                        manager_template.index(coverage_options[1]) + 1
                    ]
                    != str(slot.replica_count)
                )
            )
        ):
            raise FactorialManifestError(
                f"slot precontainment manager argv drifted: {slot.slot_id}"
            )
        if slot.causal_acceptance != _causal_acceptance(
            PRECONTAINMENT_FAULT_COVERAGE_GATE_V1
            if expected_coverage_enabled
            else None,
            PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1
            if runtime.manifest_id
            in {
                V17_MANIFEST_ID,
                V18_MANIFEST_ID,
                V19_MANIFEST_ID,
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None,
            PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1
            if runtime.manifest_id
            in {
                V18_MANIFEST_ID,
                V19_MANIFEST_ID,
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None,
            (
                FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2
                if runtime.manifest_id
                in {
                    V23_MANIFEST_ID,
                    V24_MANIFEST_ID,
                    V25_MANIFEST_ID,
                    V26_MANIFEST_ID,
                    V27_MANIFEST_ID,
                    V28_MANIFEST_ID,
                    V29_MANIFEST_ID,
                    V30_MANIFEST_ID,
                    V31_MANIFEST_ID,
                    V32_MANIFEST_ID,
                    V33_MANIFEST_ID,
                    V34_MANIFEST_ID,
                    V35_MANIFEST_ID,
                    V36_MANIFEST_ID,
                    FROZEN_MANIFEST_ID,
                }
                else FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1
            )
            if runtime.manifest_id
            in {
                V19_MANIFEST_ID,
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None,
            SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1
            if runtime.manifest_id
            in {
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None,
            EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1
            if runtime.manifest_id
            in {
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None,
            INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT_V1
            if runtime.manifest_id
            in {
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None,
            VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2
            if runtime.manifest_id
            in {
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else (
                VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V1
                if runtime.manifest_id == V26_MANIFEST_ID
                else None
            ),
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
            if runtime.manifest_id == FROZEN_MANIFEST_ID
            else (
                EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
                if runtime.manifest_id
                in {V34_MANIFEST_ID, V35_MANIFEST_ID, V36_MANIFEST_ID}
                else (
                    EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V1
                    if runtime.manifest_id
                    in {
                        V28_MANIFEST_ID,
                        V29_MANIFEST_ID,
                        V30_MANIFEST_ID,
                        V31_MANIFEST_ID,
                        V32_MANIFEST_ID,
                        V33_MANIFEST_ID,
                    }
                    else None
                )
            ),
            EXCLUDED_REPAIR_SMOKE_VERIFIED_RESPONSE_DUPLICATE_PROBE_CONTRACT_V1
            if runtime.manifest_id in {V28_MANIFEST_ID, V29_MANIFEST_ID, V30_MANIFEST_ID, V31_MANIFEST_ID, V32_MANIFEST_ID, V33_MANIFEST_ID, V34_MANIFEST_ID, V35_MANIFEST_ID, V36_MANIFEST_ID, FROZEN_MANIFEST_ID}
            else None,
            POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
            if runtime.manifest_id in {V36_MANIFEST_ID, FROZEN_MANIFEST_ID}
            else None,
            60_000
            if runtime.manifest_id
            in {
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None,
            82
            if runtime.manifest_id
            in {
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None,
        ):
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
    "ExcludedRepairSmokeProbeContract",
    "EXCLUDED_REPAIR_SMOKE_PROBE_MODE_V1",
    "EXCLUDED_REPAIR_SMOKE_PROBE_OPTION",
    "EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V1",
    "EXCLUDED_REPAIR_SMOKE_SEMANTIC_DELTA_V2",
    "FactorialRuntimePlan",
    "FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256",
    "FROZEN_RUNTIME_SHA256",
    "FROZEN_SMOKE_RUNTIME_SHA256",
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
    "TieredCohortContract",
    "TransitionContract",
    "TransitionRequestContract",
    "TransitionSequenceContract",
    "V11_RUNTIME_SHA256",
    "V11_SMOKE_RUNTIME_SHA256",
    "V12_RUNTIME_SHA256",
    "V12_SMOKE_RUNTIME_SHA256",
    "V13_RUNTIME_SHA256",
    "V13_SMOKE_RUNTIME_SHA256",
    "V14_RUNTIME_SHA256",
    "V14_SMOKE_RUNTIME_SHA256",
    "V15_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V15_RUNTIME_SHA256",
    "V15_SMOKE_RUNTIME_SHA256",
    "V16_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V16_RUNTIME_SHA256",
    "V16_SMOKE_RUNTIME_SHA256",
    "V17_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V17_RUNTIME_SHA256",
    "V17_SMOKE_RUNTIME_SHA256",
    "V18_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V18_RUNTIME_SHA256",
    "V18_SMOKE_RUNTIME_SHA256",
    "V19_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V19_RUNTIME_SHA256",
    "V19_SMOKE_RUNTIME_SHA256",
    "V20_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V20_RUNTIME_SHA256",
    "V20_SMOKE_RUNTIME_SHA256",
    "V21_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V21_RUNTIME_SHA256",
    "V21_SMOKE_RUNTIME_SHA256",
    "V22_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V22_RUNTIME_SHA256",
    "V22_SMOKE_RUNTIME_SHA256",
    "V23_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V23_RUNTIME_SHA256",
    "V23_SMOKE_RUNTIME_SHA256",
    "V24_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V24_RUNTIME_SHA256",
    "V24_SMOKE_RUNTIME_SHA256",
    "V25_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V25_RUNTIME_SHA256",
    "V25_SMOKE_RUNTIME_SHA256",
    "V26_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V26_RUNTIME_SHA256",
    "V26_SMOKE_RUNTIME_SHA256",
    "V27_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V27_RUNTIME_SHA256",
    "V27_SMOKE_RUNTIME_SHA256",
    "V28_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V28_RUNTIME_SHA256",
    "V28_SMOKE_RUNTIME_SHA256",
    "V29_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V29_RUNTIME_SHA256",
    "V29_SMOKE_RUNTIME_SHA256",
    "V30_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V30_RUNTIME_SHA256",
    "V30_SMOKE_RUNTIME_SHA256",
    "V31_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V31_RUNTIME_SHA256",
    "V31_SMOKE_RUNTIME_SHA256",
    "V32_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V32_RUNTIME_SHA256",
    "V32_SMOKE_RUNTIME_SHA256",
    "V33_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V33_RUNTIME_SHA256",
    "V33_SMOKE_RUNTIME_SHA256",
    "V34_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V34_RUNTIME_SHA256",
    "V34_SMOKE_RUNTIME_SHA256",
    "V35_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V35_RUNTIME_SHA256",
    "V35_SMOKE_RUNTIME_SHA256",
    "V36_COVERAGE_SMOKE_RUNTIME_SHA256",
    "V36_RUNTIME_SHA256",
    "V36_SMOKE_RUNTIME_SHA256",
    "build_factorial_runtime",
    "build_slot_runtime",
    "build_smoke_metadata",
    "canonical_runtime_bytes",
    "materialize_manager_argv",
    "materialize_replica_argv",
    "runtime_preflight",
)
