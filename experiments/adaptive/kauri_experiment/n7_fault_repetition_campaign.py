"""Strict five-by-three repetition gate for the bounded N=7 fault demo.

The campaign repeats the existing three-arm comparison five times.  It does
not retry failed slots and it does not convert commit-continuity timing into a
throughput claim.  A campaign can pass only when every repetition passes both
the comparison contract and the stricter thesis evidence contract.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import statistics
from typing import Any

from .comparison import (
    ComparisonError,
    FaultComparison,
    summarize_comparison,
)
from .robust_topology_evaluation import evaluate_robust_topology
from .thesis_evaluation import (
    ARM_NAMES,
    ThesisEvaluationError,
    build_thesis_evaluation,
)


REPETITIONS_PER_ARM = 5
SCHEDULED_ATTEMPTS = REPETITIONS_PER_ARM * len(ARM_NAMES)

# Balance the order across the five repetitions.  These are scheduled slots,
# never retry slots.  Keep the explicit tuple audit-friendly.
FROZEN_ATTEMPT_SCHEDULE = (
    (1, "sigkill_crash"),
    (1, "static_authenticated_false_report"),
    (1, "static_persistent_omission"),
    (2, "static_authenticated_false_report"),
    (2, "static_persistent_omission"),
    (2, "sigkill_crash"),
    (3, "static_persistent_omission"),
    (3, "sigkill_crash"),
    (3, "static_authenticated_false_report"),
    (4, "sigkill_crash"),
    (4, "static_persistent_omission"),
    (4, "static_authenticated_false_report"),
    (5, "static_persistent_omission"),
    (5, "static_authenticated_false_report"),
    (5, "sigkill_crash"),
)

TIMING_SCOPE = (
    "descriptive same-host CLOCK_MONOTONIC_RAW continuity witness; "
    "not throughput or an inferential performance claim"
)

_HASH = re.compile(r"^[0-9a-f]{64}$")


class N7FaultRepetitionCampaignError(ValueError):
    """The supplied attempts cannot form the frozen repetition campaign."""


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise N7FaultRepetitionCampaignError(f"{label} must be an integer")
    return value


def _positive_timestamp(value: object, label: str) -> int:
    timestamp = _integer(value, label)
    if timestamp <= 0:
        raise N7FaultRepetitionCampaignError(f"{label} must be positive")
    return timestamp


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise N7FaultRepetitionCampaignError(f"{label} must be an object")
    return value


def semantic_n7_campaign_sha256(value: Mapping[str, Any]) -> str:
    """Hash the canonical JSON meaning of one campaign document."""

    if not isinstance(value, Mapping):
        raise N7FaultRepetitionCampaignError("hashed value must be an object")
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise N7FaultRepetitionCampaignError(
            "hashed value is not canonical JSON"
        ) from error
    return hashlib.sha256(encoded).hexdigest()


def _semantic_attempt_sha256(attempt: Mapping[str, Any]) -> str:
    semantic = deepcopy(dict(attempt))
    semantic.pop("campaign_repetition", None)
    return semantic_n7_campaign_sha256(semantic)


def _sealed_execution_record(record: Mapping[str, Any]) -> dict[str, Any]:
    sealed = deepcopy(dict(record))
    sealed.pop("execution_record_sha256", None)
    sealed["execution_record_sha256"] = semantic_n7_campaign_sha256(sealed)
    return sealed


def build_n7_execution_binding(
    campaign_plan: Mapping[str, Any],
    execution_records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Seal the exact plan and ordinal execution records for one campaign."""

    if isinstance(execution_records, (str, bytes, Mapping)):
        raise N7FaultRepetitionCampaignError(
            "execution records must be an iterable of objects"
        )
    try:
        records = [_sealed_execution_record(record) for record in execution_records]
    except TypeError as error:
        raise N7FaultRepetitionCampaignError(
            "execution records must be iterable"
        ) from error
    plan = deepcopy(dict(_mapping(campaign_plan, "campaign plan")))
    return {
        "schema_version": 1,
        "scenario": "n7-static-fault-repetition-execution-binding",
        "campaign_plan": plan,
        "campaign_plan_sha256": semantic_n7_campaign_sha256(plan),
        "execution_records": records,
    }


def _comparison_contract(comparison: FaultComparison) -> dict[str, Any]:
    expected_names = tuple(arm.name for arm in comparison.arms)
    if expected_names != ARM_NAMES:
        raise N7FaultRepetitionCampaignError(
            "comparison does not contain the frozen three arms"
        )
    if (
        comparison.seed != 41_719
        or comparison.crash_replica_id != 1
        or comparison.false_reporter_id != 6
        or comparison.false_report_target_id != 1
        or comparison.persistent_omitter_id != 1
        or comparison.diagnostic_fault_bound != 1
    ):
        raise N7FaultRepetitionCampaignError(
            "comparison differs from the frozen N=7 campaign"
        )
    return {arm.name: arm for arm in comparison.arms}


def _elapsed_ms(start_ns: int, end_ns: int, label: str) -> int:
    if end_ns <= start_ns:
        raise N7FaultRepetitionCampaignError(
            f"{label} must end after its fault evidence"
        )
    # CLOCK_MONOTONIC_RAW is recorded in nanoseconds.  Whole milliseconds are
    # sufficient for the intentionally descriptive campaign graph.
    return (end_ns - start_ns) // 1_000_000


def _attempt_metrics(
    attempt: Mapping[str, Any],
    arm_name: str,
) -> tuple[int, int | None, str]:
    """Return locally checkable timing/outcome metrics for one PASS attempt."""

    action = _mapping(attempt.get("action_observation"), "fault action")
    after = _mapping(attempt.get("common_commit_after"), "post-fault commit")
    after_ns = _positive_timestamp(
        after.get("common_monotonic_raw_ns"),
        "post-fault commit timestamp",
    )
    if arm_name == "sigkill_crash":
        action_ns = _positive_timestamp(
            action.get("confirmed_monotonic_raw_ns"),
            "crash confirmation timestamp",
        )
        return (
            _elapsed_ms(action_ns, after_ns, "post-fault commit"),
            None,
            "not_applicable",
        )

    action_ns = _positive_timestamp(
        action.get("manager_acceptance_observed_monotonic_raw_ns"),
        "manager acceptance timestamp",
    )
    initial = _mapping(
        attempt.get("manager_accepted_timeout_observation"),
        "initial manager observation",
    )
    followup = _mapping(
        attempt.get("followup_manager_observation"),
        "followup manager observation",
    )
    initial_ns = _positive_timestamp(
        initial.get("source_monotonic_ns"),
        "initial manager source timestamp",
    )
    followup_ns = _positive_timestamp(
        followup.get("source_monotonic_ns"),
        "followup manager source timestamp",
    )
    settlement_ms = _elapsed_ms(
        initial_ns,
        followup_ns,
        "diagnostic settlement",
    )
    outcome = (
        "settled_false_reporter"
        if arm_name == "static_authenticated_false_report"
        else "settled_persistent_omitter"
    )
    return (
        _elapsed_ms(action_ns, after_ns, "post-fault commit"),
        settlement_ms,
        outcome,
    )


def _statistics(values: list[int]) -> dict[str, int] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "sample_count": len(ordered),
        "minimum": ordered[0],
        "median": int(statistics.median_low(ordered)),
        "maximum": ordered[-1],
    }


def _normalize_attempts(
    comparison: FaultComparison,
    attempts: Iterable[Mapping[str, Any]],
) -> dict[tuple[int, str], dict[str, Any]]:
    if isinstance(attempts, (str, bytes, Mapping)):
        raise N7FaultRepetitionCampaignError(
            "attempts must be an iterable of objects"
        )
    try:
        supplied = list(attempts)
    except TypeError as error:
        raise N7FaultRepetitionCampaignError(
            "attempts must be iterable"
        ) from error
    arm_contracts = _comparison_contract(comparison)
    slots: dict[tuple[int, str], dict[str, Any]] = {}
    run_ids: set[str] = set()
    for index, value in enumerate(supplied, start=1):
        attempt = _mapping(value, f"attempt {index}")
        repetition = _integer(
            attempt.get("campaign_repetition"),
            f"attempt {index} campaign repetition",
        )
        if repetition not in range(1, REPETITIONS_PER_ARM + 1):
            raise N7FaultRepetitionCampaignError(
                f"attempt {index} repetition is outside 1..5"
            )
        arm_name = attempt.get("arm")
        if not isinstance(arm_name, str) or arm_name not in ARM_NAMES:
            raise N7FaultRepetitionCampaignError(
                f"attempt {index} has an unknown arm"
            )
        slot = (repetition, arm_name)
        if slot in slots:
            raise N7FaultRepetitionCampaignError(
                f"campaign contains a duplicate scheduled slot: {slot}"
            )
        run_id = attempt.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise N7FaultRepetitionCampaignError(
                f"attempt {index} has no run id"
            )
        if run_id in run_ids:
            raise N7FaultRepetitionCampaignError(
                f"campaign contains duplicate run id: {run_id}"
            )
        if attempt.get("kauri_revision") != comparison.kauri_revision:
            raise N7FaultRepetitionCampaignError(
                f"attempt {index} is not bound to the frozen revision"
            )
        if (
            attempt.get("fault_plan_sha256")
            != arm_contracts[arm_name].plan.sha256
        ):
            raise N7FaultRepetitionCampaignError(
                f"attempt {index} is not bound to its frozen fault plan"
            )
        verdict = attempt.get("verdict")
        if verdict not in {"PASS", "INCOMPLETE"}:
            raise N7FaultRepetitionCampaignError(
                f"attempt {index} has an unsupported verdict"
            )
        runtime_error = attempt.get("runtime_error")
        if runtime_error is not None and not isinstance(runtime_error, str):
            raise N7FaultRepetitionCampaignError(
                f"attempt {index} has an invalid runtime error"
            )
        slots[slot] = deepcopy(dict(attempt))
        run_ids.add(run_id)
    return slots


def normalize_n7_fault_campaign_attempt(
    comparison: FaultComparison,
    attempt: Mapping[str, Any],
    *,
    campaign_repetition: int,
    arm: str,
) -> dict[str, Any]:
    """Validate one runner result before admitting it as campaign evidence."""

    normalized = deepcopy(dict(_mapping(attempt, "arm verdict")))
    normalized["campaign_repetition"] = campaign_repetition
    slots = _normalize_attempts(comparison, (normalized,))
    expected = (campaign_repetition, arm)
    if expected not in slots:
        raise N7FaultRepetitionCampaignError(
            "arm verdict does not match its scheduled slot"
        )
    return slots[expected]


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise N7FaultRepetitionCampaignError(
            f"{label} must be a lowercase SHA-256"
        )
    return value


def _path_within(path: str, root: str) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(
            Path(root).resolve(strict=False)
        )
    except (OSError, ValueError):
        return False
    return Path(path).resolve(strict=False) != Path(root).resolve(strict=False)


def _execution_binding_failures(
    comparison: FaultComparison,
    binding_value: object,
    slots: Mapping[tuple[int, str], Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Check that every admitted verdict came from its frozen execution slot."""

    if binding_value is None:
        return None, ["campaign execution binding is absent"]
    if not isinstance(binding_value, Mapping):
        return None, ["campaign execution binding is not an object"]
    binding = deepcopy(dict(binding_value))
    failures: list[str] = []
    if (
        binding.get("schema_version") != 1
        or binding.get("scenario")
        != "n7-static-fault-repetition-execution-binding"
    ):
        failures.append("campaign execution binding contract is unsupported")

    plan_value = binding.get("campaign_plan")
    if not isinstance(plan_value, Mapping):
        return binding, failures + ["campaign plan is absent"]
    plan = plan_value
    try:
        expected_plan_sha = semantic_n7_campaign_sha256(plan)
        supplied_plan_sha = _hash(
            binding.get("campaign_plan_sha256"),
            "campaign plan hash",
        )
        if supplied_plan_sha != expected_plan_sha:
            failures.append("campaign plan hash does not match the embedded plan")
    except N7FaultRepetitionCampaignError as error:
        failures.append(str(error))

    if not (
        plan.get("schema_version") == 1
        and plan.get("scenario") == "n7-static-fault-repetition-plan"
        and plan.get("kauri_revision") == comparison.kauri_revision
        and plan.get("repetitions_per_arm") == REPETITIONS_PER_ARM
        and plan.get("scheduled_attempt_count") == SCHEDULED_ATTEMPTS
        and plan.get("retry_policy") == "none"
        and plan.get("execution_order") == "sequential"
    ):
        failures.append("campaign plan differs from the frozen contract")

    planned_values = plan.get("scheduled_attempts")
    if not isinstance(planned_values, list) or len(planned_values) != SCHEDULED_ATTEMPTS:
        return binding, failures + ["campaign plan does not contain 15 slots"]
    planned: list[Mapping[str, Any]] = []
    results_roots: set[str] = set()
    for ordinal, ((repetition, arm), value) in enumerate(
        zip(FROZEN_ATTEMPT_SCHEDULE, planned_values, strict=True),
        start=1,
    ):
        if not isinstance(value, Mapping):
            failures.append(f"planned slot {ordinal} is not an object")
            planned.append({})
            continue
        planned.append(value)
        results_root = value.get("results_root")
        command = value.get("command")
        if not (
            value.get("ordinal") == ordinal
            and value.get("campaign_repetition") == repetition
            and value.get("arm") == arm
            and isinstance(results_root, str)
            and bool(results_root)
            and isinstance(command, list)
            and bool(command)
            and all(isinstance(part, str) and part for part in command)
        ):
            failures.append(f"planned slot {ordinal} differs from the frozen schedule")
            continue
        normalized_root = str(Path(results_root).resolve(strict=False))
        if normalized_root in results_roots:
            failures.append("campaign plan reuses a results root")
        results_roots.add(normalized_root)

    records_value = binding.get("execution_records")
    if not isinstance(records_value, list) or len(records_value) != SCHEDULED_ATTEMPTS:
        return binding, failures + ["execution binding does not contain 15 records"]
    for ordinal, ((repetition, arm), planned_item, value) in enumerate(
        zip(FROZEN_ATTEMPT_SCHEDULE, planned, records_value, strict=True),
        start=1,
    ):
        if not isinstance(value, Mapping):
            failures.append(f"execution record {ordinal} is not an object")
            continue
        record = value
        supplied_record_sha = record.get("execution_record_sha256")
        semantic_record = deepcopy(dict(record))
        semantic_record.pop("execution_record_sha256", None)
        try:
            if _hash(
                supplied_record_sha,
                f"execution record {ordinal} hash",
            ) != semantic_n7_campaign_sha256(semantic_record):
                failures.append(
                    f"execution record {ordinal} hash does not match its content"
                )
        except N7FaultRepetitionCampaignError as error:
            failures.append(str(error))

        results_root = planned_item.get("results_root")
        if not (
            record.get("schema_version") == 1
            and record.get("scenario")
            == "n7-static-fault-repetition-execution"
            and record.get("ordinal") == ordinal
            and record.get("campaign_repetition") == repetition
            and record.get("arm") == arm
            and record.get("command") == planned_item.get("command")
            and record.get("results_root") == results_root
        ):
            failures.append(f"execution record {ordinal} does not match its plan slot")
        returncode = record.get("returncode")
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            failures.append(f"execution record {ordinal} return code is invalid")

        attempt = slots.get((repetition, arm))
        verdict_binding = record.get("arm_verdict")
        evidence_error = record.get("evidence_error")
        if attempt is None:
            if not isinstance(evidence_error, str) or not evidence_error:
                failures.append(
                    f"execution record {ordinal} does not explain missing evidence"
                )
            continue
        if not isinstance(verdict_binding, Mapping):
            failures.append(f"execution record {ordinal} has no verdict binding")
            continue
        path = verdict_binding.get("path")
        try:
            raw_sha = _hash(
                verdict_binding.get("raw_sha256"),
                f"execution record {ordinal} raw verdict hash",
            )
            canonical_sha = _hash(
                verdict_binding.get("canonical_sha256"),
                f"execution record {ordinal} canonical verdict hash",
            )
            del raw_sha
            if canonical_sha != _semantic_attempt_sha256(attempt):
                failures.append(
                    f"execution record {ordinal} canonical verdict hash "
                    "does not match its embedded attempt"
                )
        except N7FaultRepetitionCampaignError as error:
            failures.append(str(error))
        if not (
            isinstance(path, str)
            and isinstance(results_root, str)
            and Path(path).name == "arm-verdict.json"
            and _path_within(path, results_root)
        ):
            failures.append(f"execution record {ordinal} verdict path is outside its results root")
        if verdict_binding.get("run_id") != attempt.get("run_id"):
            failures.append(f"execution record {ordinal} run id does not match its attempt")
        if evidence_error is not None:
            failures.append(f"execution record {ordinal} reports an evidence error")
        expected_returncode = 0 if attempt.get("verdict") == "PASS" else 1
        if returncode != expected_returncode:
            failures.append(
                f"execution record {ordinal} return code does not match its verdict"
            )
    return binding, failures


def summarize_n7_fault_repetition_campaign(
    comparison: FaultComparison,
    attempts: Iterable[Mapping[str, Any]],
    execution_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic PASS/INCOMPLETE five-repetition summary."""

    slots = _normalize_attempts(comparison, attempts)
    model = evaluate_robust_topology(
        comparison.kauri_revision,
        revision_verification="verified_current_clean_head",
    )
    repetitions: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    pass_counts = {name: 0 for name in ARM_NAMES}
    outcome_counts = {name: Counter() for name in ARM_NAMES}
    continuity_counts = {name: 0 for name in ARM_NAMES}
    post_fault_timings = {name: [] for name in ARM_NAMES}
    settlement_timings = {name: [] for name in ARM_NAMES}
    strict_validation_failures: list[dict[str, Any]] = []

    for repetition in range(1, REPETITIONS_PER_ARM + 1):
        ordered_attempts: list[dict[str, Any]] = []
        for arm_name in ARM_NAMES:
            attempt = slots.get((repetition, arm_name))
            if attempt is None:
                missing.append(
                    {
                        "campaign_repetition": repetition,
                        "arm": arm_name,
                    }
                )
                continue
            ordered_attempts.append(deepcopy(attempt))
            if attempt["verdict"] != "PASS":
                failures.append(
                    {
                        "campaign_repetition": repetition,
                        "arm": arm_name,
                        "run_id": attempt["run_id"],
                        "verdict": attempt["verdict"],
                        "runtime_error": attempt.get("runtime_error"),
                    }
                )

        repetition_passes = (
            len(ordered_attempts) == len(ARM_NAMES)
            and all(item["verdict"] == "PASS" for item in ordered_attempts)
        )
        comparison_summary: dict[str, object] | None = None
        if repetition_passes:
            by_name = {
                str(attempt["arm"]): attempt
                for attempt in ordered_attempts
            }
            try:
                comparison_summary = summarize_comparison(
                    comparison,
                    by_name,
                )
                # This second gate checks runtime context, raw fault binding,
                # exact commit bracketing and exact diagnostic settlement.
                build_thesis_evaluation(model, ordered_attempts)
                metrics = [
                    (attempt, _attempt_metrics(attempt, str(attempt["arm"])))
                    for attempt in ordered_attempts
                ]
            except (
                ComparisonError,
                ThesisEvaluationError,
                N7FaultRepetitionCampaignError,
            ) as error:
                repetition_passes = False
                comparison_summary = None
                strict_validation_failures.append(
                    {
                        "campaign_repetition": repetition,
                        "error": str(error),
                    }
                )
            else:
                for attempt, (post_ms, settlement_ms, outcome) in metrics:
                    arm_name = str(attempt["arm"])
                    pass_counts[arm_name] += 1
                    continuity_counts[arm_name] += 1
                    post_fault_timings[arm_name].append(post_ms)
                    if settlement_ms is not None:
                        settlement_timings[arm_name].append(settlement_ms)
                    outcome_counts[arm_name][outcome] += 1
        repetitions.append(
            {
                "campaign_repetition": repetition,
                "verdict": "PASS" if repetition_passes else "INCOMPLETE",
                "comparison_summary": deepcopy(comparison_summary),
                "attempts": ordered_attempts,
            }
        )

    normalized_binding, binding_failures = _execution_binding_failures(
        comparison,
        execution_binding,
        slots,
    )
    campaign_passes = (
        not missing
        and not failures
        and not strict_validation_failures
        and not binding_failures
    )
    diagnostic_outcomes = {
        name: dict(sorted(outcome_counts[name].items()))
        for name in ARM_NAMES
    }
    return {
        "schema_version": 1,
        "scenario": "n7-static-fault-repetition-campaign",
        "verdict": "PASS" if campaign_passes else "INCOMPLETE",
        "kauri_revision": comparison.kauri_revision,
        "seed": comparison.seed,
        "repetitions_per_arm": REPETITIONS_PER_ARM,
        "scheduled_attempts": SCHEDULED_ATTEMPTS,
        "preserved_attempts": len(slots),
        "pass_counts": pass_counts,
        "missing_attempts": missing,
        "failed_attempts": failures,
        "strict_validation_failures": strict_validation_failures,
        "diagnostic_outcomes": diagnostic_outcomes,
        "post_fault_commit_continuity": {
            name: {
                "witnessed": continuity_counts[name],
                "scheduled": REPETITIONS_PER_ARM,
            }
            for name in ARM_NAMES
        },
        "descriptive_timing_ms": {
            name: {
                "diagnostic_settlement_delay_ms": (
                    None
                    if name == "sigkill_crash"
                    else _statistics(settlement_timings[name])
                ),
                "post_fault_commit_delay_ms": _statistics(
                    post_fault_timings[name]
                ),
            }
            for name in ARM_NAMES
        },
        "timing_scope": TIMING_SCOPE,
        "schedule": [
            {
                "ordinal": ordinal,
                "campaign_repetition": repetition,
                "arm": arm,
            }
            for ordinal, (repetition, arm) in enumerate(
                FROZEN_ATTEMPT_SCHEDULE,
                start=1,
            )
        ],
        "execution_binding": normalized_binding,
        "execution_binding_failures": binding_failures,
        "repetitions": repetitions,
        "claims_not_made": [
            "no throughput comparison",
            "no inferential performance claim",
            "no live topology rematching",
            "no consensus-safety proof",
            "only the frozen crash and two static Byzantine modes are covered",
        ],
    }


def canonical_n7_fault_repetition_json(
    campaign: Mapping[str, Any],
) -> str:
    """Return compact deterministic JSON and reject non-finite values."""

    if not isinstance(campaign, Mapping):
        raise N7FaultRepetitionCampaignError("campaign must be an object")
    try:
        return json.dumps(
            campaign,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise N7FaultRepetitionCampaignError(
            "campaign is not canonical JSON"
        ) from error


def validate_n7_fault_repetition_campaign(
    comparison: FaultComparison,
    campaign: Mapping[str, Any],
    *,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    """Rebuild an artifact from embedded attempts and reject any edit."""

    document = _mapping(campaign, "campaign")
    repetition_values = document.get("repetitions")
    if not isinstance(repetition_values, list):
        raise N7FaultRepetitionCampaignError(
            "campaign repetitions must be a list"
        )
    attempts: list[Mapping[str, Any]] = []
    for index, repetition_value in enumerate(repetition_values, start=1):
        repetition = _mapping(
            repetition_value,
            f"campaign repetition {index}",
        )
        values = repetition.get("attempts")
        if not isinstance(values, list) or any(
            not isinstance(value, Mapping) for value in values
        ):
            raise N7FaultRepetitionCampaignError(
                f"campaign repetition {index} attempts must be objects"
            )
        attempts.extend(values)
    rebuilt = summarize_n7_fault_repetition_campaign(
        comparison,
        attempts,
        document.get("execution_binding"),
    )
    if canonical_n7_fault_repetition_json(document) != (
        canonical_n7_fault_repetition_json(rebuilt)
    ):
        raise N7FaultRepetitionCampaignError(
            "campaign does not exactly match its embedded attempts"
        )
    if evidence_root is not None and rebuilt.get("verdict") == "PASS":
        _verify_bound_evidence_files(rebuilt, evidence_root)
    return deepcopy(rebuilt)


def _read_json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
        source = payload.decode("utf-8")
        value = json.loads(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise N7FaultRepetitionCampaignError(
            f"cannot read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise N7FaultRepetitionCampaignError(f"{label} must be an object")
    return value, payload


def _verify_bound_evidence_files(
    campaign: Mapping[str, Any],
    evidence_root: Path,
) -> None:
    """Re-read the plan, records and raw verdicts before graph generation."""

    root = evidence_root.resolve(strict=True)
    binding = _mapping(campaign.get("execution_binding"), "execution binding")
    plan = _mapping(binding.get("campaign_plan"), "campaign plan")
    plan_path = root / "campaign-plan.json"
    observed_plan, _ = _read_json_object(plan_path, "campaign plan")
    if semantic_n7_campaign_sha256(observed_plan) != binding.get(
        "campaign_plan_sha256"
    ) or canonical_n7_fault_repetition_json(observed_plan) != (
        canonical_n7_fault_repetition_json(plan)
    ):
        raise N7FaultRepetitionCampaignError(
            "persisted campaign plan does not match its binding"
        )

    attempts_by_slot: dict[tuple[int, str], Mapping[str, Any]] = {}
    repetitions = campaign.get("repetitions")
    assert isinstance(repetitions, list)
    for repetition in repetitions:
        assert isinstance(repetition, Mapping)
        for attempt in repetition["attempts"]:
            assert isinstance(attempt, Mapping)
            attempts_by_slot[
                (int(attempt["campaign_repetition"]), str(attempt["arm"]))
            ] = attempt

    records = binding.get("execution_records")
    assert isinstance(records, list)
    for ordinal, ((repetition, arm), record) in enumerate(
        zip(FROZEN_ATTEMPT_SCHEDULE, records, strict=True),
        start=1,
    ):
        assert isinstance(record, Mapping)
        record_path = root / f"attempt-{ordinal:02d}-execution.json"
        observed_record, _ = _read_json_object(
            record_path,
            f"execution record {ordinal}",
        )
        if canonical_n7_fault_repetition_json(observed_record) != (
            canonical_n7_fault_repetition_json(record)
        ):
            raise N7FaultRepetitionCampaignError(
                f"persisted execution record {ordinal} differs from its binding"
            )
        verdict_binding = _mapping(
            record.get("arm_verdict"),
            f"execution record {ordinal} verdict binding",
        )
        verdict_path = Path(str(verdict_binding["path"]))
        try:
            verdict_path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as error:
            raise N7FaultRepetitionCampaignError(
                f"execution record {ordinal} verdict path escapes the campaign"
            ) from error
        observed_verdict, raw_payload = _read_json_object(
            verdict_path,
            f"execution record {ordinal} arm verdict",
        )
        if hashlib.sha256(raw_payload).hexdigest() != verdict_binding.get(
            "raw_sha256"
        ):
            raise N7FaultRepetitionCampaignError(
                f"execution record {ordinal} raw verdict hash changed"
            )
        attempt = attempts_by_slot[(repetition, arm)]
        if semantic_n7_campaign_sha256(observed_verdict) != verdict_binding.get(
            "canonical_sha256"
        ) or _semantic_attempt_sha256(attempt) != verdict_binding.get(
            "canonical_sha256"
        ):
            raise N7FaultRepetitionCampaignError(
                f"execution record {ordinal} semantic verdict changed"
            )


__all__ = (
    "FROZEN_ATTEMPT_SCHEDULE",
    "N7FaultRepetitionCampaignError",
    "REPETITIONS_PER_ARM",
    "SCHEDULED_ATTEMPTS",
    "TIMING_SCOPE",
    "canonical_n7_fault_repetition_json",
    "build_n7_execution_binding",
    "normalize_n7_fault_campaign_attempt",
    "semantic_n7_campaign_sha256",
    "summarize_n7_fault_repetition_campaign",
    "validate_n7_fault_repetition_campaign",
)
