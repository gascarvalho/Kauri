"""Frozen repetition and evidence contracts for the N=31 PQAR campaign.

The module is deliberately split from the live v4 runtime.  It fixes the
balanced 90-slot order, joins source-blind observations to declared arms only
after every child has been observed, and rebuilds campaign statistics from
sealed evidence.  Experimental outcomes never change the schedule or the
integrity acceptance decision.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from itertools import permutations
import json
import math
from pathlib import Path
import re
import secrets
import shutil
import tempfile
from typing import Any, Callable

from .n31_post_qc_audit import (
    ARM_FALSE_REPORT,
    ARM_OMISSION,
    ARM_SHAM,
    SHIPPED_PROFILE_ID,
    SHIPPED_PROFILE_SHA256,
    N31PostQcAuditError,
    load_frozen_profile as load_audit_profile,
)
from .n31_static_diagnosis_runtime import (
    N31StaticDiagnosisRuntimeError,
    TrustedProvenance,
    load_trusted_provenance,
)
from .profiled_fault_archive import (
    EvidenceSealError,
    verify_evidence_seal,
)
from .profiled_fault_evaluation import (
    ProfiledFaultEvaluationError,
    load_frozen_profile as load_runtime_profile,
)

CAMPAIGN_PROFILE_ID = "n31-f5-q21-post-qc-audit-campaign-v2"
CAMPAIGN_PROFILE_SHA256 = (
    "acc1191e467901af1743d6930f4e7a36ca6f7ac45dcb6df698a39e668eab996d"
)
CAMPAIGN_SCENARIO = "n31-post-qc-audit-repetition-campaign-v2"
CAMPAIGN_ORDER_SEED = 41_719
SOURCE_BLIND_ORDER_ALGORITHM = "sha256-ranked-sealed-child-identity-v1"
SOURCE_BLIND_ISOLATION = "random-opaque-v1"
CYCLES = 5
PERMUTATIONS_PER_CYCLE = 6
SLOTS_PER_PERMUTATION = 3
SCHEDULED_BLOCKS = CYCLES * PERMUTATIONS_PER_CYCLE
SCHEDULED_ATTEMPTS = SCHEDULED_BLOCKS * SLOTS_PER_PERMUTATION
ATTEMPTS_PER_ARM = 30
ATTEMPTS_PER_POSITION_PER_ARM = 10

ARM_NAMES = (ARM_FALSE_REPORT, ARM_OMISSION, ARM_SHAM)
CLASSIFICATION_NAMES = (
    "false_reporter",
    "omission_compatible",
    "sham",
    "unclassified",
)
EXPECTED_CLASSIFICATION = {
    ARM_FALSE_REPORT: "false_reporter",
    ARM_OMISSION: "omission_compatible",
    ARM_SHAM: "sham",
}
TIMING_METRICS_NS = (
    "qc_to_deadline_slack_ns",
    "target_to_deadline_slack_ns",
    "relay_to_root_latency_ns",
    "root_verification_latency_ns",
    "qc_to_audit_latency_ns",
    "expiry_to_later_commit_latency_ns",
)

_ARM_TOKEN = {
    ARM_FALSE_REPORT: "false-report",
    ARM_OMISSION: "direct-vote-omission",
    ARM_SHAM: "sham",
}
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_FORBIDDEN_BLIND_KEYS = frozenset(
    {
        "actor_replica_id",
        "arm",
        "expected_classification",
        "expected_source_blind_classification",
        "fault_id",
        "ground_truth",
        "outcome",
        "true_arm",
        "verdict",
    }
)


class N31PostQcAuditCampaignError(ValueError):
    """The supplied profile or preserved campaign violates the frozen gate."""


@dataclass(frozen=True, slots=True)
class FrozenCampaignProfile:
    """Validated byte identity and scheduling bindings for the campaign."""

    profile_id: str
    profile_sha256: str
    order_seed: int
    cycles: int
    audit_profile_path: str
    audit_profile_id: str
    audit_profile_sha256: str
    runtime_profile_path: str
    runtime_profile_id: str
    runtime_profile_sha256: str
    arm_names: tuple[str, ...]
    timing_metrics_ns: tuple[str, ...]

    def expected_classification(self, arm: str) -> str:
        try:
            return EXPECTED_CLASSIFICATION[arm]
        except KeyError as error:
            raise N31PostQcAuditCampaignError(f"unknown campaign arm: {arm}") from error


def _error(message: str) -> None:
    raise N31PostQcAuditCampaignError(message)


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _error(f"JSON document contains duplicate key: {key}")
        result[key] = value
    return result


def _json_value(payload: bytes, label: str) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=lambda value: _error(
                f"{label} contains non-finite constant: {value}"
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise N31PostQcAuditCampaignError(
            f"{label} is not strict UTF-8 JSON"
        ) from error


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error(f"{label} must be a JSON object")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        _error(f"{label} must be an integer")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _HEX_256.fullmatch(value) is None:
        _error(f"{label} must be a lowercase SHA-256")
    return value


def canonical_json_bytes(value: object) -> bytes:
    """Return the one canonical JSON representation used for semantic hashes."""

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
        raise N31PostQcAuditCampaignError(
            "campaign document is not canonical JSON"
        ) from error


def semantic_document_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise N31PostQcAuditCampaignError(f"cannot read {label}: {path}") from error
    value = _json_value(payload, label)
    if not isinstance(value, dict):
        _error(f"{label} must be a JSON object")
    return value


def load_frozen_campaign_profile(path: Path) -> FrozenCampaignProfile:
    """Load only the exact shipped campaign-profile bytes."""

    try:
        payload = path.resolve().read_bytes()
    except OSError as error:
        raise N31PostQcAuditCampaignError(
            f"cannot read campaign profile: {path}"
        ) from error
    digest = hashlib.sha256(payload).hexdigest()
    if digest != CAMPAIGN_PROFILE_SHA256:
        _error("campaign profile bytes differ from the shipped frozen SHA-256")
    value = _json_value(payload, "campaign profile")
    profile = _mapping(value, "campaign profile")
    audit = _mapping(profile.get("audit_profile"), "bound audit profile")
    runtime = _mapping(profile.get("runtime_profile"), "bound runtime profile")
    source_blind = _mapping(profile.get("source_blind_policy"), "source-blind policy")
    arms_value = profile.get("arms")
    metrics_value = profile.get("timing_metrics_ns")
    if not isinstance(arms_value, list) or not isinstance(metrics_value, list):
        _error("campaign arms and timing metrics must be arrays")
    arm_names: list[str] = []
    for index, item in enumerate(arms_value, start=1):
        arm = _mapping(item, f"campaign arm {index}")
        name = arm.get("name")
        expected = arm.get("expected_source_blind_classification")
        if (
            not isinstance(name, str)
            or name not in EXPECTED_CLASSIFICATION
            or expected != EXPECTED_CLASSIFICATION[name]
        ):
            _error(f"campaign arm {index} differs from the frozen truth join")
        arm_names.append(name)
    metrics = tuple(metrics_value)
    if any(not isinstance(metric, str) for metric in metrics):
        _error("campaign timing metric names must be strings")
    if (
        profile.get("schema_version") != 1
        or profile.get("profile_id") != CAMPAIGN_PROFILE_ID
        or profile.get("frozen") is not True
        or profile.get("campaign_order_seed") != CAMPAIGN_ORDER_SEED
        or profile.get("order_algorithm") != "sha256-ranked-all-six-permutations-v1"
        or profile.get("cycles") != CYCLES
        or profile.get("permutations_per_cycle") != PERMUTATIONS_PER_CYCLE
        or profile.get("slots_per_permutation") != SLOTS_PER_PERMUTATION
        or profile.get("scheduled_permutation_blocks") != SCHEDULED_BLOCKS
        or profile.get("scheduled_attempts") != SCHEDULED_ATTEMPTS
        or profile.get("attempts_per_arm") != ATTEMPTS_PER_ARM
        or profile.get("attempts_per_ordinal_position_per_arm")
        != ATTEMPTS_PER_POSITION_PER_ARM
        or profile.get("automatic_retries") != 0
        or profile.get("replacement_policy") != "none"
        or profile.get("outcome_scanning") is not False
        or tuple(arm_names) != ARM_NAMES
        or metrics != TIMING_METRICS_NS
        or audit.get("profile_id") != SHIPPED_PROFILE_ID
        or audit.get("sha256") != SHIPPED_PROFILE_SHA256
        or runtime.get("profile_id") != "n31-f5-q21-internal1-sigkill-shakedown-v1"
        or runtime.get("sha256")
        != "0defdaa9b69c949365eea3b3029da75cee3ea8334f845401103e2f7af8507650"
        or source_blind.get("observe_all_children_before_truth_join") is not True
        or source_blind.get("classifier_accepts_arm") is not False
        or source_blind.get("extraction_order") != SOURCE_BLIND_ORDER_ALGORITHM
        or source_blind.get("isolated_child_basename") != SOURCE_BLIND_ISOLATION
        or source_blind.get("unclassified_column") is not True
    ):
        _error("campaign profile differs from the frozen repetition contract")
    audit_path = audit.get("path")
    runtime_path = runtime.get("path")
    if not isinstance(audit_path, str) or not isinstance(runtime_path, str):
        _error("bound profile paths must be strings")
    return FrozenCampaignProfile(
        profile_id=CAMPAIGN_PROFILE_ID,
        profile_sha256=digest,
        order_seed=CAMPAIGN_ORDER_SEED,
        cycles=CYCLES,
        audit_profile_path=audit_path,
        audit_profile_id=SHIPPED_PROFILE_ID,
        audit_profile_sha256=SHIPPED_PROFILE_SHA256,
        runtime_profile_path=runtime_path,
        runtime_profile_id=str(runtime["profile_id"]),
        runtime_profile_sha256=str(runtime["sha256"]),
        arm_names=ARM_NAMES,
        timing_metrics_ns=TIMING_METRICS_NS,
    )


def derive_frozen_campaign_schedule(
    profile: FrozenCampaignProfile,
) -> tuple[dict[str, object], ...]:
    """Derive the exact six-permutation-by-five-cycle 90-slot schedule."""

    if type(profile) is not FrozenCampaignProfile:
        _error("schedule derivation requires the exact frozen campaign profile")
    lexical_permutations = tuple(sorted(permutations(profile.arm_names)))
    if len(lexical_permutations) != PERMUTATIONS_PER_CYCLE:
        _error("campaign arms do not yield exactly six unique permutations")
    slots: list[dict[str, object]] = []
    block = 0
    ordinal = 0
    observed_permutations: Counter[tuple[str, ...]] = Counter()
    arm_counts: Counter[str] = Counter()
    position_counts: Counter[tuple[str, int]] = Counter()
    for cycle in range(1, profile.cycles + 1):
        ranked = sorted(
            lexical_permutations,
            key=lambda order: (
                hashlib.sha256(
                    (f"{profile.order_seed}|{cycle}|" + "|".join(order)).encode("utf-8")
                ).hexdigest(),
                order,
            ),
        )
        for cycle_rank, order in enumerate(ranked, start=1):
            block += 1
            observed_permutations[order] += 1
            permutation_id = (
                f"cycle-{cycle:02d}-rank-{cycle_rank:02d}-"
                + "-then-".join(_ARM_TOKEN[arm] for arm in order)
            )
            for position, arm in enumerate(order, start=1):
                ordinal += 1
                arm_counts[arm] += 1
                position_counts[(arm, position)] += 1
                slots.append(
                    {
                        "ordinal": ordinal,
                        "cycle": cycle,
                        "permutation_block": block,
                        "cycle_permutation_rank": cycle_rank,
                        "permutation_id": permutation_id,
                        "ordinal_position": position,
                        "arm": arm,
                        "results_root": "children",
                    }
                )
    if (
        len(slots) != SCHEDULED_ATTEMPTS
        or set(observed_permutations.values()) != {CYCLES}
        or arm_counts != Counter({arm: ATTEMPTS_PER_ARM for arm in ARM_NAMES})
        or any(
            position_counts[(arm, position)] != ATTEMPTS_PER_POSITION_PER_ARM
            for arm in ARM_NAMES
            for position in range(1, SLOTS_PER_PERMUTATION + 1)
        )
    ):
        _error("derived schedule failed its frozen balance invariants")
    return tuple(slots)


def build_campaign_plan(
    profile: FrozenCampaignProfile,
    *,
    kauri_revision: str,
    trusted_provenance_sha256: str,
    pilot_gate: Mapping[str, Any],
    frozen_preflight_sha256: str,
) -> dict[str, object]:
    """Build the complete truth-bearing plan that is sealed before slot one."""

    if _REVISION.fullmatch(kauri_revision) is None:
        _error("campaign revision must be a full lowercase Git revision")
    trusted_digest = _sha256(trusted_provenance_sha256, "trusted provenance digest")
    preflight_digest = _sha256(frozen_preflight_sha256, "frozen preflight digest")
    gate = deepcopy(dict(_mapping(pilot_gate, "pilot gate")))
    schedule = [deepcopy(slot) for slot in derive_frozen_campaign_schedule(profile)]
    return {
        "schema_version": 1,
        "scenario": f"{CAMPAIGN_SCENARIO}-plan",
        "campaign_profile": {
            "profile_id": profile.profile_id,
            "sha256": profile.profile_sha256,
        },
        "audit_profile": {
            "path": profile.audit_profile_path,
            "profile_id": profile.audit_profile_id,
            "sha256": profile.audit_profile_sha256,
        },
        "runtime_profile": {
            "path": profile.runtime_profile_path,
            "profile_id": profile.runtime_profile_id,
            "sha256": profile.runtime_profile_sha256,
        },
        "kauri_revision": kauri_revision,
        "trusted_provenance_sha256": trusted_digest,
        "frozen_preflight_sha256": preflight_digest,
        "pilot_gate_sha256": semantic_document_sha256(gate),
        "campaign_order_seed": profile.order_seed,
        "order_algorithm": "sha256-ranked-all-six-permutations-v1",
        "cycles": profile.cycles,
        "permutations_per_cycle": PERMUTATIONS_PER_CYCLE,
        "scheduled_permutation_blocks": SCHEDULED_BLOCKS,
        "scheduled_attempts": SCHEDULED_ATTEMPTS,
        "attempts_per_arm": ATTEMPTS_PER_ARM,
        "attempts_per_ordinal_position_per_arm": (ATTEMPTS_PER_POSITION_PER_ARM),
        "automatic_retries": 0,
        "replacement_policy": "none",
        "outcome_scanning": False,
        "execution_mode": "fixed_sequential_fresh_processes",
        "resume": False,
        "source_blind_extraction_order": SOURCE_BLIND_ORDER_ALGORITHM,
        "source_blind_isolation": SOURCE_BLIND_ISOLATION,
        "statistical_contract": {
            "confusion_shape": [3, 4],
            "classification_columns": list(CLASSIFICATION_NAMES),
            "fixed_denominator_per_arm": ATTEMPTS_PER_ARM,
            "binomial_interval": "two-sided-clopper-pearson-exact-95-percent",
            "median_interval": "two-sided-exact-sign-test-order-statistic",
            "complete_n30_median_interval_order_statistics": [10, 21],
        },
        "scheduled_slots": schedule,
    }


def _validate_plan(
    profile: FrozenCampaignProfile,
    plan: Mapping[str, Any],
    *,
    pilot_gate: Mapping[str, Any] | None = None,
) -> None:
    expected_schedule = list(derive_frozen_campaign_schedule(profile))
    if (
        plan.get("schema_version") != 1
        or plan.get("scenario") != f"{CAMPAIGN_SCENARIO}-plan"
        or plan.get("campaign_profile")
        != {"profile_id": profile.profile_id, "sha256": profile.profile_sha256}
        or plan.get("audit_profile")
        != {
            "path": profile.audit_profile_path,
            "profile_id": profile.audit_profile_id,
            "sha256": profile.audit_profile_sha256,
        }
        or plan.get("runtime_profile")
        != {
            "path": profile.runtime_profile_path,
            "profile_id": profile.runtime_profile_id,
            "sha256": profile.runtime_profile_sha256,
        }
        or not isinstance(plan.get("kauri_revision"), str)
        or _REVISION.fullmatch(str(plan.get("kauri_revision"))) is None
        or plan.get("campaign_order_seed") != CAMPAIGN_ORDER_SEED
        or plan.get("order_algorithm") != "sha256-ranked-all-six-permutations-v1"
        or plan.get("cycles") != CYCLES
        or plan.get("permutations_per_cycle") != PERMUTATIONS_PER_CYCLE
        or plan.get("scheduled_permutation_blocks") != SCHEDULED_BLOCKS
        or plan.get("scheduled_attempts") != SCHEDULED_ATTEMPTS
        or plan.get("attempts_per_arm") != ATTEMPTS_PER_ARM
        or plan.get("attempts_per_ordinal_position_per_arm")
        != ATTEMPTS_PER_POSITION_PER_ARM
        or plan.get("automatic_retries") != 0
        or plan.get("replacement_policy") != "none"
        or plan.get("outcome_scanning") is not False
        or plan.get("execution_mode") != "fixed_sequential_fresh_processes"
        or plan.get("resume") is not False
        or plan.get("source_blind_extraction_order") != SOURCE_BLIND_ORDER_ALGORITHM
        or plan.get("source_blind_isolation") != SOURCE_BLIND_ISOLATION
        or plan.get("scheduled_slots") != expected_schedule
    ):
        _error("campaign plan differs from the frozen profile and schedule")
    _sha256(plan.get("trusted_provenance_sha256"), "plan provenance digest")
    _sha256(plan.get("frozen_preflight_sha256"), "plan preflight digest")
    supplied_gate_digest = _sha256(
        plan.get("pilot_gate_sha256"), "plan pilot gate digest"
    )
    if pilot_gate is not None and supplied_gate_digest != semantic_document_sha256(
        pilot_gate
    ):
        _error("campaign plan does not bind the sealed pilot gate")


def _binomial_probability(n: int, k: int, p: float) -> float:
    return math.comb(n, k) * (p**k) * ((1.0 - p) ** (n - k))


def _binomial_lower_tail(n: int, k: int, p: float) -> float:
    return math.fsum(_binomial_probability(n, index, p) for index in range(k + 1))


def _binomial_upper_tail(n: int, k: int, p: float) -> float:
    return math.fsum(_binomial_probability(n, index, p) for index in range(k, n + 1))


def clopper_pearson_interval(
    successes: int,
    trials: int,
    *,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Return a dependency-free two-sided exact binomial interval."""

    if (
        type(successes) is not int
        or type(trials) is not int
        or trials <= 0
        or successes < 0
        or successes > trials
        or not 0.0 < confidence < 1.0
    ):
        _error("Clopper-Pearson inputs are outside their valid range")
    alpha = 1.0 - confidence
    if successes == 0:
        lower = 0.0
    else:
        low, high = 0.0, 1.0
        for _ in range(100):
            middle = (low + high) / 2.0
            if _binomial_upper_tail(trials, successes, middle) < alpha / 2.0:
                low = middle
            else:
                high = middle
        lower = (low + high) / 2.0
    if successes == trials:
        upper = 1.0
    else:
        low, high = 0.0, 1.0
        for _ in range(100):
            middle = (low + high) / 2.0
            if _binomial_lower_tail(trials, successes, middle) > alpha / 2.0:
                low = middle
            else:
                high = middle
        upper = (low + high) / 2.0
    return lower, upper


def _probability_string(value: float) -> str:
    return f"{value:.10f}"


def _rate_document(successes: int, trials: int) -> dict[str, object]:
    lower, upper = clopper_pearson_interval(successes, trials)
    return {
        "successes": successes,
        "trials": trials,
        "estimate": _probability_string(successes / trials),
        "confidence_level": "0.95",
        "method": "two-sided-clopper-pearson-exact",
        "lower": _probability_string(lower),
        "upper": _probability_string(upper),
    }


def exact_median_interval(values: Sequence[int]) -> dict[str, object]:
    """Return the narrowest bounded exact sign-test interval at >=95%."""

    if isinstance(values, (str, bytes)) or any(
        type(value) is not int for value in values
    ):
        _error("median interval values must be integer nanoseconds")
    ordered = sorted(values)
    sample_count = len(ordered)
    selected_index: int | None = None
    selected_coverage = 0.0
    for lower_index in range(1, (sample_count + 1) // 2 + 1):
        tail = math.fsum(
            math.comb(sample_count, count) * (0.5**sample_count)
            for count in range(lower_index)
        )
        coverage = 1.0 - 2.0 * tail
        if coverage >= 0.95:
            selected_index = lower_index
            selected_coverage = coverage
    if selected_index is None:
        return {
            "method": "two-sided-exact-sign-test-order-statistic",
            "confidence_target": "0.95",
            "sample_count": sample_count,
            "interval": None,
            "reason": "too_few_observations_for_bounded_95_percent_interval",
        }
    upper_index = sample_count - selected_index + 1
    return {
        "method": "two-sided-exact-sign-test-order-statistic",
        "confidence_target": "0.95",
        "sample_count": sample_count,
        "achieved_coverage": _probability_string(selected_coverage),
        "lower_order_index": selected_index,
        "upper_order_index": upper_index,
        "lower_ns": ordered[selected_index - 1],
        "upper_ns": ordered[upper_index - 1],
    }


def _sample_median(values: Sequence[int]) -> int | float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    total = ordered[midpoint - 1] + ordered[midpoint]
    return total // 2 if total % 2 == 0 else total / 2


def nanosecond_metric_summary(
    values: Sequence[int],
    *,
    scheduled_trials: int = ATTEMPTS_PER_ARM,
) -> dict[str, object]:
    """Summarize available integer-nanosecond samples without imputation."""

    if (
        type(scheduled_trials) is not int
        or scheduled_trials <= 0
        or isinstance(values, (str, bytes))
        or any(type(value) is not int for value in values)
        or len(values) > scheduled_trials
    ):
        _error("nanosecond summary inputs are malformed")
    ordered = sorted(values)
    base: dict[str, object] = {
        "scheduled_count": scheduled_trials,
        "available_count": len(ordered),
        "missing_count": scheduled_trials - len(ordered),
        "unit": "nanoseconds",
    }
    if not ordered:
        return {
            **base,
            "minimum_ns": None,
            "q1_ns": None,
            "median_ns": None,
            "q3_ns": None,
            "maximum_ns": None,
            "median_interval": exact_median_interval(ordered),
        }
    midpoint = len(ordered) // 2
    lower_half = ordered[:midpoint] or ordered
    upper_half = ordered[-midpoint:] if midpoint else ordered
    return {
        **base,
        "minimum_ns": ordered[0],
        "q1_ns": _sample_median(lower_half),
        "median_ns": _sample_median(ordered),
        "q3_ns": _sample_median(upper_half),
        "maximum_ns": ordered[-1],
        "median_interval": exact_median_interval(ordered),
    }


def _truth_bearing_key(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in _FORBIDDEN_BLIND_KEYS or key.startswith("expected_"):
                return str(key)
            nested = _truth_bearing_key(child)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for child in value:
            nested = _truth_bearing_key(child)
            if nested is not None:
                return nested
    return None


def source_blind_order_key(record: Mapping[str, Any]) -> str:
    """Rank one child using sealed identity only, never slot or arm metadata."""

    tree_sha256 = _sha256(
        record.get("child_tree_sha256"), "source-blind child tree digest"
    )
    seal_sha256 = _sha256(
        record.get("child_seal_sha256"), "source-blind child seal digest"
    )
    payload = (f"{SOURCE_BLIND_ORDER_ALGORITHM}\0{tree_sha256}\0{seal_sha256}").encode(
        "ascii"
    )
    return hashlib.sha256(payload).hexdigest()


def isolated_source_blind_observation(
    run_directory: Path,
    *,
    trusted_provenance: TrustedProvenance,
    classifier: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify a byte-identical child from an isolated private temporary root.

    The classifier never receives a path inside the truth-bearing campaign.
    The byte-identical copy receives a fresh random neutral basename.  Its
    temporary parent contains no plan, start, execution, or sibling records
    that could reveal its scheduled arm or chronological position.
    """

    source = run_directory.resolve()
    try:
        source_seal = verify_evidence_seal(source)
    except EvidenceSealError as error:
        raise N31PostQcAuditCampaignError(
            f"source-blind child evidence seal rejected: {error}"
        ) from error
    with tempfile.TemporaryDirectory(prefix="kauri-pqar-source-blind-") as temporary:
        private_root = Path(temporary).resolve()
        isolated_child = private_root / f"opaque-{secrets.token_hex(16)}"
        try:
            shutil.copytree(source, isolated_child, symlinks=False)
            copied_seal = verify_evidence_seal(isolated_child)
        except (OSError, EvidenceSealError) as error:
            raise N31PostQcAuditCampaignError(
                f"cannot create byte-identical isolated child: {error}"
            ) from error
        if (
            copied_seal.entries != source_seal.entries
            or copied_seal.tree_sha256 != source_seal.tree_sha256
            or copied_seal.seal_sha256 != source_seal.seal_sha256
        ):
            _error("isolated child seal differs from preserved source evidence")
        observation = classifier(
            isolated_child,
            trusted_provenance=trusted_provenance,
        )
        if not isinstance(observation, Mapping):
            _error("source-blind preserved-run classifier returned no object")
        normalized = deepcopy(dict(observation))
        child = normalized.get("child")
        if not isinstance(child, Mapping):
            _error("source-blind observation lacks its generic child binding")
        normalized_child = deepcopy(dict(child))
        if normalized_child.get("path") != str(isolated_child):
            _error("source-blind classifier returned an unexpected child path")
        opaque_identity = f"opaque-{source_seal.tree_sha256[:16]}"
        normalized_child["path"] = f"opaque-child/{source_seal.tree_sha256}"
        normalized_child["run_id"] = opaque_identity
        normalized["child"] = normalized_child
        serialized = json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
        )
        if (
            str(private_root) in serialized
            or isolated_child.name in serialized
            or source.name in serialized
        ):
            _error("source-blind observation leaked a chronological child identity")
        return normalized


def _classification(
    observation: Mapping[str, Any] | None,
) -> tuple[str, str | None]:
    if observation is None:
        return "unclassified", "source-blind observation payload is absent"
    supplied = [
        observation[key]
        for key in (
            "classification",
            "predicted_classification",
            "source_blind_classification",
        )
        if key in observation
    ]
    if not supplied:
        return "unclassified", "source-blind observation lacks a classification"
    if (
        any(value != supplied[0] for value in supplied[1:])
        or supplied[0] not in CLASSIFICATION_NAMES
    ):
        return "unclassified", "source-blind classification is invalid or inconsistent"
    return str(supplied[0]), None


def _metric_value(observation: Mapping[str, Any] | None, metric: str) -> int | None:
    if observation is None:
        return None
    containers: list[Mapping[str, Any]] = [observation]
    for key in ("metrics_ns", "quantitative_audit", "timing"):
        value = observation.get(key)
        if isinstance(value, Mapping):
            containers.insert(0, value)
    for container in containers:
        value = container.get(metric)
        if value is None:
            continue
        if type(value) is not int:
            return None
        return value
    return None


def _records_by_ordinal(
    values: Iterable[Mapping[str, Any]],
    *,
    label: str,
) -> tuple[dict[int, dict[str, Any]], list[str]]:
    if isinstance(values, (str, bytes, Mapping)):
        _error(f"{label} must be an iterable of objects")
    records: dict[int, dict[str, Any]] = {}
    failures: list[str] = []
    try:
        supplied = list(values)
    except TypeError as error:
        raise N31PostQcAuditCampaignError(f"{label} must be iterable") from error
    for index, value in enumerate(supplied, start=1):
        if not isinstance(value, Mapping):
            failures.append(f"{label} item {index} is not an object")
            continue
        ordinal = value.get("ordinal")
        if type(ordinal) is not int or not 1 <= ordinal <= SCHEDULED_ATTEMPTS:
            failures.append(f"{label} item {index} has an invalid ordinal")
            continue
        if ordinal in records:
            failures.append(f"{label} contains duplicate ordinal {ordinal}")
            continue
        records[ordinal] = deepcopy(dict(value))
    return records, failures


def summarize_n31_pqar_campaign(
    profile: FrozenCampaignProfile,
    campaign_plan: Mapping[str, Any],
    execution_records: Iterable[Mapping[str, Any]],
    blind_observations: Iterable[Mapping[str, Any]],
    *,
    controller_failures: Iterable[str] = (),
) -> dict[str, object]:
    """Join blind observations to truth and rebuild fixed-denominator output."""

    plan = deepcopy(dict(_mapping(campaign_plan, "campaign plan")))
    _validate_plan(profile, plan)
    records, integrity_failures = _records_by_ordinal(
        execution_records, label="execution records"
    )
    observations, observation_failures = _records_by_ordinal(
        blind_observations, label="blind observations"
    )
    integrity_failures.extend(observation_failures)
    if isinstance(controller_failures, (str, bytes, Mapping)):
        _error("controller failures must be an iterable of strings")
    supplied_controller_failures = list(controller_failures)
    if any(
        not isinstance(failure, str) or not failure
        for failure in supplied_controller_failures
    ):
        _error("controller failure records must be non-empty strings")
    integrity_failures.extend(
        f"controller finalization failure: {failure}"
        for failure in supplied_controller_failures
    )
    schedule = derive_frozen_campaign_schedule(profile)
    confusion: dict[str, Counter[str]] = {
        arm: Counter({classification: 0 for classification in CLASSIFICATION_NAMES})
        for arm in ARM_NAMES
    }
    outcome_counts: Counter[str] = Counter({"PASS": 0, "FAIL": 0, "INCOMPLETE": 0})
    invocation_status_counts: Counter[str] = Counter(
        {"returned": 0, "raised": 0, "not_started": 0}
    )
    correct_counts: Counter[str] = Counter()
    qualification_counts: Counter[str] = Counter()
    timing_values: dict[str, dict[str, list[int]]] = {
        arm: {metric: [] for metric in TIMING_METRICS_NS} for arm in ARM_NAMES
    }
    returned_count = 0
    for slot in schedule:
        ordinal = int(slot["ordinal"])
        arm = str(slot["arm"])
        record = records.get(ordinal)
        observation_record = observations.get(ordinal)
        observation: Mapping[str, Any] | None = None
        if record is None:
            integrity_failures.append(f"execution record {ordinal} is missing")
            outcome_counts["INCOMPLETE"] += 1
            invocation_status_counts["not_started"] += 1
        else:
            expected_identity = {
                key: slot[key]
                for key in (
                    "ordinal",
                    "cycle",
                    "permutation_block",
                    "cycle_permutation_rank",
                    "permutation_id",
                    "ordinal_position",
                    "arm",
                    "results_root",
                )
            }
            if any(
                record.get(key) != value for key, value in expected_identity.items()
            ):
                integrity_failures.append(
                    f"execution record {ordinal} differs from its scheduled slot"
                )
            launch_status = record.get("launch_status")
            if launch_status == "returned":
                invocation_status_counts["returned"] += 1
                returned_count += 1
                verdict = record.get("original_verdict")
                if verdict not in {"PASS", "FAIL", "INCOMPLETE"}:
                    integrity_failures.append(
                        f"execution record {ordinal} has an invalid returned verdict"
                    )
                    outcome_counts["INCOMPLETE"] += 1
                else:
                    outcome_counts[str(verdict)] += 1
                    if verdict == "PASS":
                        qualification_counts[arm] += 1
                if (
                    not isinstance(record.get("run_directory"), str)
                    or record.get("exception") is not None
                    or _HEX_256.fullmatch(str(record.get("child_tree_sha256"))) is None
                    or _HEX_256.fullmatch(str(record.get("child_seal_sha256"))) is None
                ):
                    integrity_failures.append(
                        f"execution record {ordinal} lacks exact sealed-child binding"
                    )
                if observation_record is None:
                    integrity_failures.append(
                        f"blind observation {ordinal} is missing for returned child"
                    )
                else:
                    if observation_record.get("run_directory") != record.get(
                        "run_directory"
                    ):
                        integrity_failures.append(
                            f"blind observation {ordinal} references the wrong child"
                        )
                    candidate = observation_record.get("observation")
                    if candidate is not None and not isinstance(candidate, Mapping):
                        integrity_failures.append(
                            f"blind observation {ordinal} payload is not an object"
                        )
                    elif isinstance(candidate, Mapping):
                        observation = candidate
                        forbidden = _truth_bearing_key(candidate)
                        if forbidden is not None:
                            integrity_failures.append(
                                f"blind observation {ordinal} contains truth field {forbidden}"
                            )
                    error_value = observation_record.get("observation_error")
                    if error_value is not None:
                        integrity_failures.append(
                            f"blind observation {ordinal} failed: {error_value}"
                        )
            elif launch_status == "raised":
                invocation_status_counts["raised"] += 1
                outcome_counts["INCOMPLETE"] += 1
                integrity_failures.append(
                    f"scheduled invocation {ordinal} raised before returning a child"
                )
            elif launch_status == "not_started":
                invocation_status_counts["not_started"] += 1
                outcome_counts["INCOMPLETE"] += 1
                integrity_failures.append(
                    f"scheduled invocation {ordinal} was not reached"
                )
            else:
                invocation_status_counts["raised"] += 1
                outcome_counts["INCOMPLETE"] += 1
                integrity_failures.append(
                    f"execution record {ordinal} has an invalid launch status"
                )
        predicted, classification_error = _classification(observation)
        if classification_error is not None:
            integrity_failures.append(
                f"blind observation {ordinal} {classification_error}"
            )
        confusion[arm][predicted] += 1
        if predicted == profile.expected_classification(arm):
            correct_counts[arm] += 1
        for metric in TIMING_METRICS_NS:
            metric_value = _metric_value(observation, metric)
            if metric_value is not None:
                timing_values[arm][metric].append(metric_value)

    extra_observations = sorted(set(observations) - set(records))
    if extra_observations:
        integrity_failures.append(
            f"blind observations have no execution record: {extra_observations}"
        )
    if len(records) != SCHEDULED_ATTEMPTS:
        integrity_failures.append(
            f"execution record count is {len(records)}, expected {SCHEDULED_ATTEMPTS}"
        )
    for arm in ARM_NAMES:
        if sum(confusion[arm].values()) != ATTEMPTS_PER_ARM:
            integrity_failures.append(
                f"confusion row {arm} does not preserve denominator 30"
            )
    failures = sorted(set(integrity_failures))
    campaign_acceptance = "ACCEPTED" if not failures else "REJECTED"
    outcome_status = (
        "ALL_QUALIFIED" if outcome_counts["PASS"] == SCHEDULED_ATTEMPTS else "MIXED"
    )
    return {
        "schema_version": 1,
        "scenario": CAMPAIGN_SCENARIO,
        "campaign_profile": {
            "profile_id": profile.profile_id,
            "sha256": profile.profile_sha256,
        },
        "kauri_revision": plan["kauri_revision"],
        "campaign_plan_sha256": semantic_document_sha256(plan),
        "trusted_provenance_sha256": plan["trusted_provenance_sha256"],
        "pilot_gate_sha256": plan["pilot_gate_sha256"],
        "campaign_acceptance": campaign_acceptance,
        "outcome_status": outcome_status,
        "figure_eligible": campaign_acceptance == "ACCEPTED",
        "integrity_failures": failures,
        "controller_failures": supplied_controller_failures,
        "scheduled_attempts": SCHEDULED_ATTEMPTS,
        "returned_invocations": returned_count,
        "fixed_denominator_per_arm": ATTEMPTS_PER_ARM,
        "outcome_counts": dict(outcome_counts),
        "invocation_status_counts": dict(invocation_status_counts),
        "confusion_table": {
            arm: {
                classification: confusion[arm][classification]
                for classification in CLASSIFICATION_NAMES
            }
            for arm in ARM_NAMES
        },
        "rates": {
            arm: {
                "classification_correctness": _rate_document(
                    correct_counts[arm], ATTEMPTS_PER_ARM
                ),
                "strict_harness_qualification": _rate_document(
                    qualification_counts[arm], ATTEMPTS_PER_ARM
                ),
            }
            for arm in ARM_NAMES
        },
        "timing_summaries_ns": {
            arm: {
                metric: nanosecond_metric_summary(
                    timing_values[arm][metric],
                    scheduled_trials=ATTEMPTS_PER_ARM,
                )
                for metric in TIMING_METRICS_NS
            }
            for arm in ARM_NAMES
        },
        "execution_records": [records[index] for index in sorted(records)],
        "blind_observations": [observations[index] for index in sorted(observations)],
        "statistical_scope": (
            "same-host descriptive nanosecond timings; exact binomial intervals "
            "assume Bernoulli repetitions that only approximate independence "
            "and identical distribution"
        ),
        "claim_boundary": (
            "omission-compatible classification is not persistent-omitter "
            "identification, throughput evidence, or a consensus-safety proof"
        ),
    }


def canonical_n31_pqar_campaign_json(campaign: Mapping[str, Any]) -> str:
    """Serialize a campaign summary using canonical semantic JSON."""

    return canonical_json_bytes(dict(_mapping(campaign, "campaign summary"))).decode(
        "utf-8"
    )


def _path_beneath(root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        _error(f"{label} must be a non-empty relative path")
    candidate_value = Path(relative)
    if candidate_value.is_absolute() or ".." in candidate_value.parts:
        _error(f"{label} must be a canonical relative path")
    candidate = (root / candidate_value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise N31PostQcAuditCampaignError(f"{label} escapes campaign root") from error
    return candidate


def _exact_directory_entries(path: Path, expected: set[str], label: str) -> None:
    try:
        entries = {entry.name for entry in path.iterdir()}
    except OSError as error:
        raise N31PostQcAuditCampaignError(f"cannot inspect {label}") from error
    if entries != expected:
        _error(
            f"{label} membership differs; missing={sorted(expected - entries)!r}; "
            f"extra={sorted(entries - expected)!r}"
        )


def validate_n31_pqar_campaign(
    campaign_directory: Path,
    *,
    trusted_provenance: TrustedProvenance,
    classify_preserved_run_source_blind: Callable[..., Mapping[str, Any]] | None = None,
    validate_preserved_run: Callable[..., Mapping[str, Any]] | None = None,
    validate_pilot_sequence: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, object]:
    """Rebuild a sealed campaign from exact membership and raw child evidence."""

    if type(trusted_provenance) is not TrustedProvenance:
        _error("campaign validation requires an exact trusted provenance receipt")
    if (
        classify_preserved_run_source_blind is None
        or validate_preserved_run is None
        or validate_pilot_sequence is None
    ):
        from . import n31_post_qc_audit_runtime as audit_runtime

        if classify_preserved_run_source_blind is None:
            classify_preserved_run_source_blind = getattr(
                audit_runtime, "classify_preserved_run_source_blind", None
            )
        if validate_preserved_run is None:
            validate_preserved_run = audit_runtime.validate_preserved_run
        if validate_pilot_sequence is None:
            validate_pilot_sequence = audit_runtime.validate_pilot_sequence
    if not callable(classify_preserved_run_source_blind):
        _error(
            "v4 runtime lacks classify_preserved_run_source_blind; campaign "
            "validation refuses a truth-aware fallback"
        )
    assert callable(validate_preserved_run)
    assert callable(validate_pilot_sequence)

    root = campaign_directory.resolve()
    try:
        verify_evidence_seal(root)
    except EvidenceSealError as error:
        raise N31PostQcAuditCampaignError(
            f"outer campaign seal rejected: {error}"
        ) from error
    _exact_directory_entries(
        root,
        {
            "blind-observations.json",
            "campaign-summary.json",
            "children",
            "controller-failures.json",
            "evidence-seal.json",
            "executions",
            "intent",
            "starts",
        },
        "campaign root",
    )
    intent = root / "intent"
    try:
        intent_seal = verify_evidence_seal(intent)
    except EvidenceSealError as error:
        raise N31PostQcAuditCampaignError(
            f"campaign intent seal rejected: {error}"
        ) from error
    _exact_directory_entries(
        intent,
        {
            "audit-profile.json",
            "campaign-plan.json",
            "campaign-profile.json",
            "evidence-seal.json",
            "frozen-preflight.json",
            "pilot-gate.json",
            "runtime-profile.json",
            "trusted-provenance.json",
        },
        "campaign intent",
    )
    profile = load_frozen_campaign_profile(intent / "campaign-profile.json")
    try:
        audit_profile = load_audit_profile(intent / "audit-profile.json")
        runtime_profile = load_runtime_profile(intent / "runtime-profile.json")
        observed_trusted = load_trusted_provenance(intent / "trusted-provenance.json")
    except (
        N31PostQcAuditError,
        N31StaticDiagnosisRuntimeError,
        ProfiledFaultEvaluationError,
    ) as error:
        raise N31PostQcAuditCampaignError(
            f"sealed intent profile/provenance rejected: {error}"
        ) from error
    if (
        audit_profile.profile_id != profile.audit_profile_id
        or audit_profile.profile_sha256 != profile.audit_profile_sha256
        or runtime_profile.profile_id != profile.runtime_profile_id
        or runtime_profile.profile_sha256 != profile.runtime_profile_sha256
    ):
        _error("sealed intent profile copies differ from campaign bindings")
    if observed_trusted != trusted_provenance:
        _error("sealed campaign provenance differs from external trusted receipt")
    plan = _read_json_object(intent / "campaign-plan.json", "campaign plan")
    pilot_gate = _read_json_object(intent / "pilot-gate.json", "pilot gate")
    frozen_preflight = _read_json_object(
        intent / "frozen-preflight.json", "frozen preflight"
    )
    _validate_plan(profile, plan, pilot_gate=pilot_gate)
    if (
        plan.get("kauri_revision") != trusted_provenance.revision
        or plan.get("trusted_provenance_sha256") != trusted_provenance.sha256
        or plan.get("frozen_preflight_sha256")
        != semantic_document_sha256(frozen_preflight)
        or frozen_preflight.get("revision") != trusted_provenance.revision
    ):
        _error("intent revision, provenance, or preflight digest drifted")
    if (
        pilot_gate.get("schema_version") != 1
        or pilot_gate.get("scenario") != f"{CAMPAIGN_SCENARIO}-pilot-gate"
        or pilot_gate.get("required_verdict") != "PASS"
        or pilot_gate.get("kauri_revision") != trusted_provenance.revision
        or pilot_gate.get("trusted_provenance_sha256") != trusted_provenance.sha256
        or pilot_gate.get("audit_profile_sha256") != profile.audit_profile_sha256
        or pilot_gate.get("runtime_profile_sha256") != profile.runtime_profile_sha256
        or pilot_gate.get("figure_eligible") is not False
    ):
        _error("sealed pilot gate differs from the campaign contract")
    pilot_path_value = pilot_gate.get("pilot_sequence_directory")
    if (
        not isinstance(pilot_path_value, str)
        or not Path(pilot_path_value).is_absolute()
    ):
        _error("pilot gate lacks one exact absolute sequence directory")
    pilot_result = validate_pilot_sequence(
        Path(pilot_path_value), trusted_provenance=trusted_provenance
    )
    if (
        pilot_result.get("verdict") != "PASS"
        or pilot_result.get("evidence_tree_sha256")
        != pilot_gate.get("pilot_tree_sha256")
        or pilot_result.get("evidence_seal_sha256")
        != pilot_gate.get("pilot_seal_sha256")
    ):
        _error("live pilot revalidation no longer satisfies the sealed gate")

    schedule = derive_frozen_campaign_schedule(profile)
    execution_names = {f"slot-{ordinal:03d}.json" for ordinal in range(1, 91)}
    _exact_directory_entries(root / "executions", execution_names, "executions")
    execution_records = [
        _read_json_object(
            root / "executions" / f"slot-{ordinal:03d}.json",
            f"execution record {ordinal}",
        )
        for ordinal in range(1, 91)
    ]
    for slot, record in zip(schedule, execution_records, strict=True):
        ordinal = int(slot["ordinal"])
        if (
            record.get("schema_version") != 1
            or record.get("scenario") != f"{CAMPAIGN_SCENARIO}-slot-execution"
            or any(record.get(key) != value for key, value in slot.items())
            or record.get("intent_tree_sha256") != intent_seal.tree_sha256
            or record.get("intent_seal_sha256") != intent_seal.seal_sha256
        ):
            _error(f"execution record {ordinal} differs from sealed intent")
        launch_status = record.get("launch_status")
        if launch_status in {"returned", "raised"}:
            if (
                not isinstance(record.get("started_utc"), str)
                or not isinstance(record.get("finished_utc"), str)
                or type(record.get("started_monotonic_ns")) is not int
                or type(record.get("finished_monotonic_ns")) is not int
                or type(record.get("elapsed_ns")) is not int
                or int(record["elapsed_ns"]) < 0
            ):
                _error(f"execution record {ordinal} has malformed timing")
        if launch_status == "returned":
            if (
                record.get("original_verdict") not in {"PASS", "FAIL", "INCOMPLETE"}
                or record.get("exception") is not None
                or not isinstance(record.get("run_directory"), str)
            ):
                _error(f"returned execution record {ordinal} is malformed")
        elif launch_status == "raised":
            if (
                record.get("run_directory") is not None
                or record.get("original_verdict") is not None
                or not isinstance(record.get("exception"), str)
                or not record["exception"]
            ):
                _error(f"raised execution record {ordinal} is malformed")
        elif launch_status == "not_started":
            if any(
                record.get(key) is not None
                for key in (
                    "started_utc",
                    "finished_utc",
                    "started_monotonic_ns",
                    "finished_monotonic_ns",
                    "elapsed_ns",
                    "run_directory",
                    "original_verdict",
                    "child_tree_sha256",
                    "child_seal_sha256",
                )
            ) or not isinstance(record.get("exception"), str):
                _error(f"not-started execution record {ordinal} is malformed")
        else:
            _error(f"execution record {ordinal} has an unsupported launch status")
    started_ordinals = {
        int(record["ordinal"])
        for record in execution_records
        if record.get("launch_status") != "not_started"
        and type(record.get("ordinal")) is int
    }
    start_names = {f"slot-{ordinal:03d}.json" for ordinal in started_ordinals}
    _exact_directory_entries(root / "starts", start_names, "slot starts")
    start_records: dict[int, dict[str, Any]] = {}
    for ordinal in sorted(started_ordinals):
        start = _read_json_object(
            root / "starts" / f"slot-{ordinal:03d}.json",
            f"slot start {ordinal}",
        )
        slot = schedule[ordinal - 1]
        if (
            start.get("schema_version") != 1
            or start.get("scenario") != f"{CAMPAIGN_SCENARIO}-slot-start"
            or any(start.get(key) != slot[key] for key in slot)
            or start.get("intent_tree_sha256") != intent_seal.tree_sha256
            or start.get("intent_seal_sha256") != intent_seal.seal_sha256
        ):
            _error(f"slot start {ordinal} differs from sealed intent")
        start_records[ordinal] = start

    previous_finished_ns: int | None = None
    for ordinal in sorted(started_ordinals):
        start = start_records[ordinal]
        record = execution_records[ordinal - 1]
        started_ns = start.get("started_monotonic_ns")
        finished_ns = record.get("finished_monotonic_ns")
        if (
            type(started_ns) is not int
            or started_ns <= 0
            or record.get("started_monotonic_ns") != started_ns
            or record.get("started_utc") != start.get("started_utc")
            or type(finished_ns) is not int
            or finished_ns < started_ns
            or record.get("elapsed_ns") != finished_ns - started_ns
            or (previous_finished_ns is not None and started_ns < previous_finished_ns)
        ):
            _error(f"slot {ordinal} violates the frozen sequential chronology")
        previous_finished_ns = finished_ns

    returned: list[tuple[dict[str, Any], Path]] = []
    accounted_children: set[str] = set()
    children_root = root / "children"
    for slot, record in zip(schedule, execution_records, strict=True):
        ordinal = int(slot["ordinal"])
        launch_status = record.get("launch_status")
        if launch_status == "not_started":
            if record.get("observed_children") != []:
                _error(f"not-started slot {ordinal} accounts child evidence")
            continue
        results_root = _path_beneath(root, slot["results_root"], "child results root")
        observed_children = record.get("observed_children")
        if (
            not isinstance(observed_children, list)
            or any(
                not isinstance(name, str) or not name or Path(name).name != name
                for name in observed_children
            )
            or len(observed_children) != len(set(observed_children))
        ):
            _error(f"execution record {ordinal} has malformed child accounting")
        if accounted_children.intersection(observed_children):
            _error("campaign child accounting reuses one opaque run directory")
        accounted_children.update(observed_children)
        for child_name in observed_children:
            child_path = results_root / child_name
            if child_path.is_symlink() or not child_path.is_dir():
                _error(f"slot {ordinal} observed child is not a real directory")
        if launch_status != "returned":
            continue
        run_directory = _path_beneath(
            root, record.get("run_directory"), "returned run directory"
        )
        if run_directory.parent != results_root:
            _error(f"returned child {ordinal} is not the exact direct slot child")
        if observed_children != [run_directory.name]:
            _error(f"returned child {ordinal} differs from direct child accounting")
        returned.append((record, run_directory))
    _exact_directory_entries(
        children_root,
        accounted_children,
        "opaque campaign children",
    )

    # Pass one: rank by sealed child identities so call count cannot reproduce
    # the slot schedule, then extract from isolated paths.  No plan, arm, truth
    # map, ordinal, or expected label is passed to the classifier.
    blind_ranked = sorted(
        returned,
        key=lambda item: source_blind_order_key(item[0]),
    )
    blind_rank_keys = [source_blind_order_key(record) for record, _ in blind_ranked]
    if len(blind_rank_keys) != len(set(blind_rank_keys)):
        _error("returned children do not have unique source-blind identities")
    rebuilt_observations: list[dict[str, object]] = []
    for record, run_directory in blind_ranked:
        observation = isolated_source_blind_observation(
            run_directory,
            trusted_provenance=trusted_provenance,
            classifier=classify_preserved_run_source_blind,
        )
        if not isinstance(observation, Mapping):
            _error("source-blind preserved-run classifier returned no object")
        forbidden = _truth_bearing_key(observation)
        if forbidden is not None:
            _error(
                f"source-blind preserved-run output contains truth field {forbidden}"
            )
        rebuilt_observations.append(
            {
                "schema_version": 1,
                "scenario": f"{CAMPAIGN_SCENARIO}-blind-observation",
                "ordinal": record["ordinal"],
                "run_directory": record["run_directory"],
                "observation": deepcopy(dict(observation)),
                "observation_error": None,
            }
        )

    # Pass two: only after every child has a blind observation may truth-aware
    # child validation and plan joining occur.
    for record, run_directory in returned:
        try:
            child_seal = verify_evidence_seal(run_directory)
        except EvidenceSealError as error:
            raise N31PostQcAuditCampaignError(
                f"returned child evidence seal rejected: {error}"
            ) from error
        if child_seal.tree_sha256 != record.get(
            "child_tree_sha256"
        ) or child_seal.seal_sha256 != record.get("child_seal_sha256"):
            _error("execution record child seal binding drifted")
        preserved = validate_preserved_run(
            run_directory, trusted_provenance=trusted_provenance
        )
        if preserved.get("verdict") != record.get("original_verdict") or preserved.get(
            "arm"
        ) != record.get("arm"):
            _error("execution record changed a preserved child arm or verdict")

    persisted_observations = _read_json_object(
        root / "blind-observations.json", "blind observations"
    )
    expected_observation_document = {
        "schema_version": 1,
        "scenario": f"{CAMPAIGN_SCENARIO}-blind-observations",
        "observations": rebuilt_observations,
    }
    if canonical_json_bytes(persisted_observations) != canonical_json_bytes(
        expected_observation_document
    ):
        _error("persisted blind observations differ from raw child extraction")
    controller_document = _read_json_object(
        root / "controller-failures.json", "controller failures"
    )
    controller_failures = controller_document.get("failures")
    if (
        controller_document.get("schema_version") != 1
        or controller_document.get("scenario")
        != f"{CAMPAIGN_SCENARIO}-controller-failures"
        or not isinstance(controller_failures, list)
        or any(
            not isinstance(failure, str) or not failure
            for failure in controller_failures
        )
    ):
        _error("controller failure accounting is malformed")
    rebuilt = summarize_n31_pqar_campaign(
        profile,
        plan,
        execution_records,
        rebuilt_observations,
        controller_failures=controller_failures,
    )
    summary_path = root / "campaign-summary.json"
    try:
        summary_payload = summary_path.read_bytes()
    except OSError as error:
        raise N31PostQcAuditCampaignError("campaign summary is unreadable") from error
    if summary_payload != canonical_json_bytes(rebuilt):
        _error("campaign summary differs from canonical evidence rebuild")
    return deepcopy(rebuilt)


__all__ = (
    "ARM_NAMES",
    "ATTEMPTS_PER_ARM",
    "ATTEMPTS_PER_POSITION_PER_ARM",
    "CAMPAIGN_ORDER_SEED",
    "CAMPAIGN_PROFILE_ID",
    "CAMPAIGN_PROFILE_SHA256",
    "CAMPAIGN_SCENARIO",
    "CLASSIFICATION_NAMES",
    "CYCLES",
    "FrozenCampaignProfile",
    "N31PostQcAuditCampaignError",
    "SCHEDULED_ATTEMPTS",
    "SCHEDULED_BLOCKS",
    "SOURCE_BLIND_ISOLATION",
    "SOURCE_BLIND_ORDER_ALGORITHM",
    "TIMING_METRICS_NS",
    "build_campaign_plan",
    "canonical_json_bytes",
    "canonical_n31_pqar_campaign_json",
    "clopper_pearson_interval",
    "derive_frozen_campaign_schedule",
    "exact_median_interval",
    "isolated_source_blind_observation",
    "load_frozen_campaign_profile",
    "nanosecond_metric_summary",
    "semantic_document_sha256",
    "source_blind_order_key",
    "summarize_n31_pqar_campaign",
    "validate_n31_pqar_campaign",
)
