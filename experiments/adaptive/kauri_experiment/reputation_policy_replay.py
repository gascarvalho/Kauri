"""Offline reputation-policy replay over accepted live N=7 evidence.

The producer deliberately consumes only manager events that were accepted by
the original live campaign. Fault-plan identities are copied into the output
only after rankings have been computed and are never policy inputs.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping, Sequence


class ReputationPolicyReplayError(ValueError):
    """Raised when source evidence or the frozen replay contract is invalid."""


MECHANISMS = ("responsiveness", "latency-priority")
ACCEPTED_EVENT = "evidence.observation_accepted"


def canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ReputationPolicyReplayError(f"{path} is not a JSON object")
    return value


def load_bound_arm_verdict(
    execution: Mapping[str, Any], verdict_path: Path
) -> dict[str, Any]:
    """Load the verdict named by an execution record and verify its raw binding."""

    binding = _mapping(execution.get("arm_verdict"), "arm verdict binding")
    recorded_path = binding.get("path")
    if not isinstance(recorded_path, str) or Path(recorded_path).name != (
        verdict_path.name
    ):
        raise ReputationPolicyReplayError("arm verdict path binding drifted")
    if sha256_file(verdict_path) != binding.get("raw_sha256"):
        raise ReputationPolicyReplayError("arm verdict raw hash drifted")
    verdict = load_json(verdict_path)
    if verdict.get("run_id") != binding.get("run_id"):
        raise ReputationPolicyReplayError("arm verdict run identity drifted")
    return verdict


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ReputationPolicyReplayError(f"{label} is invalid")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ReputationPolicyReplayError(f"{label} is not an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ReputationPolicyReplayError(f"{label} is not an array")
    return value


@dataclass
class Attempt:
    first_ingestion_sequence: int
    observed_replica_id: int
    outcome: str
    response_duration_us: int
    deadline_duration_us: int


def _validate_profile(profile: Mapping[str, Any]) -> None:
    if profile.get("schema_version") != 1 or profile.get("frozen") is not True:
        raise ReputationPolicyReplayError("replay profile is not frozen schema v1")
    if tuple(profile.get("reputation_mechanisms", ())) != MECHANISMS:
        raise ReputationPolicyReplayError("reputation mechanism set drifted")
    membership = tuple(profile.get("membership", ()))
    if membership != tuple(range(7)):
        raise ReputationPolicyReplayError("membership is not canonical N=7")
    evidence = _mapping(profile.get("evidence_window"), "evidence window")
    if evidence.get("ground_truth_available_to_policy") is not False:
        raise ReputationPolicyReplayError("policy must be blind to ground truth")
    if tuple(evidence.get("message_types", ())) != (
        "aggregate_relay",
        "direct_vote",
    ):
        raise ReputationPolicyReplayError("evidence message domain drifted")
    policy = _mapping(profile.get("responsiveness_policy"), "policy")
    attempt_window = _integer(policy.get("attempt_window"), "attempt window", 1)
    minimum_attempts = _integer(
        policy.get("minimum_attempts"), "minimum attempts", 1
    )
    trailing = _integer(
        policy.get("trailing_timeout_streak"), "trailing timeout streak", 2
    )
    if minimum_attempts > attempt_window or trailing > attempt_window:
        raise ReputationPolicyReplayError("policy attempt bounds are invalid")
    for field, maximum in (
        ("minimum_response_rate_ppm", 1_000_000),
        ("maximum_timeout_rate_ppm", 1_000_000),
        ("latency_percentile_basis_points", 10_000),
    ):
        value = _integer(policy.get(field), field, 1 if "latency" in field else 0)
        if value > maximum:
            raise ReputationPolicyReplayError(f"{field} exceeds its scale")


def _load_observations(raw_path: Path, *, epoch_number: int) -> tuple[list[dict[str, Any]], str]:
    observations: list[dict[str, Any]] = []
    epoch_digest: str | None = None
    previous_ingestion = 0
    with raw_path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ReputationPolicyReplayError(
                    f"{raw_path}:{line_number} is invalid JSON"
                ) from error
            if event.get("event_type") != ACCEPTED_EVENT:
                continue
            payload = _mapping(event.get("payload"), "accepted event payload")
            ingestion = _integer(
                payload.get("ingestion_sequence"), "ingestion sequence", 1
            )
            if ingestion <= previous_ingestion:
                raise ReputationPolicyReplayError(
                    "accepted observation ingestion sequence regressed"
                )
            previous_ingestion = ingestion
            observation = dict(
                _mapping(payload.get("observation"), "accepted observation")
            )
            configuration = _mapping(
                observation.get("configuration"), "observation configuration"
            )
            if configuration.get("epoch_number") != epoch_number:
                raise ReputationPolicyReplayError(
                    "accepted stream escaped the frozen epoch"
                )
            observed_digest = configuration.get("epoch_digest")
            if not isinstance(observed_digest, str) or len(observed_digest) != 64:
                raise ReputationPolicyReplayError("epoch digest is invalid")
            if epoch_digest is None:
                epoch_digest = observed_digest
            elif observed_digest != epoch_digest:
                raise ReputationPolicyReplayError("accepted stream mixes epoch digests")
            observation["ingestion_sequence"] = ingestion
            observations.append(observation)
    if not observations or epoch_digest is None:
        raise ReputationPolicyReplayError("accepted observation stream is empty")
    return observations, epoch_digest


def _attempts(
    observations: Iterable[Mapping[str, Any]], membership: set[int]
) -> list[Attempt]:
    attempts: dict[str, Attempt] = {}
    for observation in observations:
        observation_id = observation.get("observation_id")
        if not isinstance(observation_id, str) or len(observation_id) != 64:
            raise ReputationPolicyReplayError("observation identity is invalid")
        observed = _integer(
            observation.get("observed_replica_id"), "observed replica"
        )
        if observed not in membership:
            raise ReputationPolicyReplayError("observation targets a nonmember")
        message_type = observation.get("expected_message_type")
        if message_type not in {"aggregate_relay", "direct_vote"}:
            raise ReputationPolicyReplayError("observation message type is invalid")
        outcome = observation.get("outcome")
        if outcome not in {"on_time", "timeout", "late"}:
            raise ReputationPolicyReplayError("observation outcome is invalid")
        response = _integer(
            observation.get("response_duration_us"), "response duration"
        )
        deadline = _integer(
            observation.get("deadline_duration_us"), "deadline duration", 1
        )
        ingestion = _integer(
            observation.get("ingestion_sequence"), "ingestion sequence", 1
        )
        prior = attempts.get(observation_id)
        if prior is None:
            if outcome == "late":
                raise ReputationPolicyReplayError("attempt starts with a late response")
            if outcome == "timeout" and response != 0:
                raise ReputationPolicyReplayError("timeout has a response duration")
            attempts[observation_id] = Attempt(
                first_ingestion_sequence=ingestion,
                observed_replica_id=observed,
                outcome="timeout_only" if outcome == "timeout" else "on_time",
                response_duration_us=response,
                deadline_duration_us=deadline,
            )
            continue
        if (
            prior.observed_replica_id != observed
            or prior.outcome != "timeout_only"
            or outcome != "late"
            or prior.deadline_duration_us != deadline
            or response < deadline
        ):
            raise ReputationPolicyReplayError("timeout-to-late transition is invalid")
        prior.outcome = "late"
        prior.response_duration_us = response
    return list(attempts.values())


def _nearest_rank(values: list[int], basis_points: int) -> int | None:
    if not values:
        return None
    values.sort()
    rank = math.ceil(basis_points * len(values) / 10_000)
    return values[rank - 1]


def _score(
    attempts: Iterable[Attempt],
    membership: Sequence[int],
    policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    by_replica: dict[int, list[Attempt]] = defaultdict(list)
    for attempt in attempts:
        by_replica[attempt.observed_replica_id].append(attempt)
    rows: list[dict[str, Any]] = []
    for replica in membership:
        replica_attempts = sorted(
            by_replica[replica], key=lambda attempt: attempt.first_ingestion_sequence
        )[-int(policy["attempt_window"]) :]
        on_time = sum(item.outcome == "on_time" for item in replica_attempts)
        timeout_only = sum(
            item.outcome == "timeout_only" for item in replica_attempts
        )
        late = sum(item.outcome == "late" for item in replica_attempts)
        responses = on_time + late
        timeouts = timeout_only + late
        count = len(replica_attempts)
        trailing = 0
        for item in reversed(replica_attempts):
            if item.outcome != "timeout_only":
                break
            trailing += 1
        response_rate = responses * 1_000_000 // count if count else 0
        timeout_rate = timeouts * 1_000_000 // count if count else 0
        latency = _nearest_rank(
            [
                item.response_duration_us
                for item in replica_attempts
                if item.outcome in {"on_time", "late"}
            ],
            int(policy["latency_percentile_basis_points"]),
        )
        reasons: list[str] = []
        if count < int(policy["minimum_attempts"]):
            classification = "insufficient_evidence"
            reasons.append("insufficient_attempts")
        else:
            if response_rate < int(policy["minimum_response_rate_ppm"]):
                reasons.append("response_rate_below_minimum")
            if timeout_rate > int(policy["maximum_timeout_rate_ppm"]):
                reasons.append("timeout_rate_above_maximum")
            if trailing >= int(policy["trailing_timeout_streak"]):
                reasons.append("persistent_timeout_streak")
            classification = "nonresponsive" if reasons else "responsive"
        rows.append(
            {
                "replica_id": replica,
                "attempt_count": count,
                "on_time_count": on_time,
                "timeout_only_count": timeout_only,
                "late_count": late,
                "response_count": responses,
                "timeout_count": timeouts,
                "trailing_timeout_count": trailing,
                "response_rate_ppm": response_rate,
                "timeout_rate_ppm": timeout_rate,
                "latency_percentile_us": latency,
                "classification": classification,
                "eligible": classification == "responsive",
                "reasons": reasons,
            }
        )
    return rows


def _ranking_key(row: Mapping[str, Any], mechanism: str) -> tuple[Any, ...]:
    latency = row["latency_percentile_us"]
    availability = (0 if latency is not None else 1, latency or 0)
    common = (
        0 if row["eligible"] else 1,
        -int(row["response_rate_ppm"]),
        int(row["timeout_rate_ppm"]),
        *availability,
        -int(row["attempt_count"]),
        int(row["replica_id"]),
    )
    if mechanism == "responsiveness":
        return common
    if mechanism != "latency-priority":
        raise ReputationPolicyReplayError(f"unsupported mechanism {mechanism}")
    return (
        0 if row["eligible"] else 1,
        *availability,
        -int(row["response_rate_ppm"]),
        int(row["timeout_rate_ppm"]),
        -int(row["attempt_count"]),
        int(row["replica_id"]),
    )


def rank_replicas(
    observations: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
    mechanism: str,
) -> dict[str, Any]:
    membership = tuple(int(item) for item in profile["membership"])
    rows = _score(
        _attempts(observations, set(membership)),
        membership,
        _mapping(profile.get("responsiveness_policy"), "policy"),
    )
    rows.sort(key=lambda row: _ranking_key(row, mechanism))
    for rank, row in enumerate(rows):
        row["rank"] = rank
    eligible = [int(row["replica_id"]) for row in rows if row["eligible"]]
    projection = _mapping(profile.get("role_projection"), "role projection")
    influential_count = int(projection["root_count"]) + int(
        projection["internal_count"]
    )
    if len(eligible) < influential_count:
        raise ReputationPolicyReplayError(
            "eligible ranking cannot fill the role projection"
        )
    influential = eligible[:influential_count]
    return {
        "mechanism": mechanism,
        "ranking": rows,
        "eligible_ids": eligible,
        "role_projection": {
            "root_id": influential[0],
            "internal_ids": influential[1:],
            "influential_ids": influential,
        },
    }


def mechanisms_share_eligibility(policy_results: Mapping[str, Any]) -> bool:
    """Return whether every mechanism classifies the same replicas eligible."""

    eligible_sets = {
        frozenset(_mapping(result, "policy result").get("eligible_ids", ()))
        for result in policy_results.values()
    }
    return len(eligible_sets) == 1


def _kendall_inversions(left: Sequence[int], right: Sequence[int]) -> int:
    positions = {replica: index for index, replica in enumerate(right)}
    return sum(
        positions[left[first]] > positions[left[second]]
        for first in range(len(left))
        for second in range(first + 1, len(left))
    )


def _fault_evaluation(
    fault_plan: Mapping[str, Any], policy_results: Mapping[str, Any]
) -> dict[str, Any]:
    actions = _sequence(fault_plan.get("actions"), "fault actions")
    if len(actions) != 1:
        raise ReputationPolicyReplayError("run does not have exactly one fault action")
    action = dict(_mapping(actions[0], "fault action"))
    kind = action.get("kind")
    if kind == "replica_group_sigkill":
        actor = _integer(action.get("replica_id"), "crash actor")
        target = None
    elif kind == "static_persistent_omission":
        actor = _integer(action.get("replica_id"), "omission actor")
        target = None
    elif kind == "static_authenticated_false_report":
        actor = _integer(action.get("reporter_id"), "false reporter")
        target = _integer(action.get("target_id"), "false-report target")
    else:
        raise ReputationPolicyReplayError(f"unsupported fault action {kind}")
    evaluation: dict[str, Any] = {
        "fault_action": action,
        "actor_id": actor,
        "target_id": target,
        "by_mechanism": {},
    }
    for mechanism, result in policy_results.items():
        ranking = result["ranking"]
        positions = {row["replica_id"]: row["rank"] for row in ranking}
        influential = result["role_projection"]["influential_ids"]
        evaluation["by_mechanism"][mechanism] = {
            "actor_rank": positions[actor],
            "actor_in_influential_roles": actor in influential,
            "target_rank": positions[target] if target is not None else None,
            "target_in_influential_roles": (
                target in influential if target is not None else None
            ),
        }
    return evaluation


def _discover_run_directory(campaign_root: Path, ordinal: int, arm: str) -> Path:
    arm_root = campaign_root / f"attempt-{ordinal:02d}-results" / arm
    candidates = sorted(path for path in arm_root.iterdir() if path.is_dir())
    if len(candidates) != 1:
        raise ReputationPolicyReplayError(
            f"attempt {ordinal} does not bind exactly one run directory"
        )
    return candidates[0]


def _median(values: Sequence[int]) -> float:
    return float(median(values))


def build_campaign(
    repository: Path,
    profile_path: Path,
    *,
    analysis_revision: str,
    revision_verification: str,
) -> dict[str, Any]:
    profile = load_json(profile_path)
    _validate_profile(profile)
    source = _mapping(profile.get("source_campaign"), "source campaign")
    campaign_root = repository / str(source["relative_path"])
    plan_path = campaign_root / "campaign-plan.json"
    verdict_path = campaign_root / "n7-fault-repetition-campaign.json"
    if sha256_file(plan_path) != source.get("campaign_plan_sha256"):
        raise ReputationPolicyReplayError("source campaign plan hash drifted")
    if sha256_file(verdict_path) != source.get("campaign_verdict_sha256"):
        raise ReputationPolicyReplayError("source campaign verdict hash drifted")
    plan = load_json(plan_path)
    verdict = load_json(verdict_path)
    if plan.get("retry_policy") != "none" or verdict.get("verdict") != "PASS":
        raise ReputationPolicyReplayError("source campaign is not no-retry PASS")
    scheduled = _sequence(plan.get("scheduled_attempts"), "scheduled attempts")
    if len(scheduled) != source.get("expected_run_count"):
        raise ReputationPolicyReplayError("source campaign run count drifted")

    runs: list[dict[str, Any]] = []
    arm_counts: Counter[str] = Counter()
    epoch_number = int(profile["evidence_window"]["epoch_number"])
    for scheduled_attempt in scheduled:
        slot = _mapping(scheduled_attempt, "scheduled attempt")
        ordinal = _integer(slot.get("ordinal"), "attempt ordinal", 1)
        arm = slot.get("arm")
        repetition = _integer(slot.get("campaign_repetition"), "repetition", 1)
        if arm not in source.get("expected_arms", ()):
            raise ReputationPolicyReplayError("source arm is outside frozen set")
        arm_counts[str(arm)] += 1
        execution_path = campaign_root / f"attempt-{ordinal:02d}-execution.json"
        execution = load_json(execution_path)
        run_directory = _discover_run_directory(campaign_root, ordinal, str(arm))
        arm_verdict = load_bound_arm_verdict(
            execution, run_directory / "arm-verdict.json"
        )
        if (
            execution.get("returncode") != 0
            or execution.get("arm") != arm
            or execution.get("campaign_repetition") != repetition
            or arm_verdict.get("verdict") != "PASS"
        ):
            raise ReputationPolicyReplayError("source execution binding failed")
        raw_path = run_directory / "raw" / "adaptive-manager.jsonl"
        fault_plan_path = run_directory / "fault-plan.json"
        observations, epoch_digest = _load_observations(
            raw_path, epoch_number=epoch_number
        )
        policy_results = {
            mechanism: rank_replicas(observations, profile, mechanism)
            for mechanism in MECHANISMS
        }
        if not mechanisms_share_eligibility(policy_results):
            raise ReputationPolicyReplayError("mechanisms changed eligibility")
        responsiveness_ids = [
            row["replica_id"]
            for row in policy_results["responsiveness"]["ranking"]
        ]
        latency_ids = [
            row["replica_id"]
            for row in policy_results["latency-priority"]["ranking"]
        ]
        comparison = {
            "kendall_inversion_count": _kendall_inversions(
                responsiveness_ids, latency_ids
            ),
            "root_changed": (
                policy_results["responsiveness"]["role_projection"]["root_id"]
                != policy_results["latency-priority"]["role_projection"]["root_id"]
            ),
            "influential_cohort_changed": (
                set(
                    policy_results["responsiveness"]["role_projection"][
                        "influential_ids"
                    ]
                )
                != set(
                    policy_results["latency-priority"]["role_projection"][
                        "influential_ids"
                    ]
                )
            ),
            "influential_overlap_count": len(
                set(
                    policy_results["responsiveness"]["role_projection"][
                        "influential_ids"
                    ]
                )
                & set(
                    policy_results["latency-priority"]["role_projection"][
                        "influential_ids"
                    ]
                )
            ),
        }
        runs.append(
            {
                "ordinal": ordinal,
                "arm": arm,
                "campaign_repetition": repetition,
                "run_id": arm_verdict.get("run_id"),
                "source_run_relative_path": str(run_directory.relative_to(repository)),
                "source_execution_sha256": sha256_file(execution_path),
                "source_manager_sha256": sha256_file(raw_path),
                "source_fault_plan_sha256": sha256_file(fault_plan_path),
                "epoch_number": epoch_number,
                "epoch_digest": epoch_digest,
                "evidence_cutoff": observations[-1]["ingestion_sequence"],
                "accepted_observation_count": len(observations),
                "policy_results": policy_results,
                "comparison": comparison,
                "post_hoc_fault_evaluation": _fault_evaluation(
                    load_json(fault_plan_path), policy_results
                ),
            }
        )
    expected_repetitions = int(source["expected_repetitions_per_arm"])
    if set(arm_counts) != set(source["expected_arms"]) or any(
        count != expected_repetitions for count in arm_counts.values()
    ):
        raise ReputationPolicyReplayError("source campaign balance drifted")

    summaries: dict[str, Any] = {}
    for arm in source["expected_arms"]:
        arm_runs = [run for run in runs if run["arm"] == arm]
        mechanism_summary: dict[str, Any] = {}
        for mechanism in MECHANISMS:
            evaluations = [
                run["post_hoc_fault_evaluation"]["by_mechanism"][mechanism]
                for run in arm_runs
            ]
            mechanism_summary[mechanism] = {
                "median_actor_rank_zero_based": _median(
                    [item["actor_rank"] for item in evaluations]
                ),
                "actor_excluded_from_influential_roles_count": sum(
                    not item["actor_in_influential_roles"] for item in evaluations
                ),
                "target_excluded_from_influential_roles_count": (
                    sum(
                        not item["target_in_influential_roles"]
                        for item in evaluations
                    )
                    if evaluations[0]["target_in_influential_roles"] is not None
                    else None
                ),
            }
        summaries[arm] = {
            "run_count": len(arm_runs),
            "root_changed_count": sum(
                run["comparison"]["root_changed"] for run in arm_runs
            ),
            "influential_cohort_changed_count": sum(
                run["comparison"]["influential_cohort_changed"]
                for run in arm_runs
            ),
            "median_kendall_inversion_count": _median(
                [run["comparison"]["kendall_inversion_count"] for run in arm_runs]
            ),
            "mechanisms": mechanism_summary,
        }

    return {
        "schema_version": 1,
        "scenario": "n7-reputation-policy-offline-replay",
        "analysis_revision": analysis_revision,
        "revision_verification": revision_verification,
        "profile_relative_path": str(profile_path.relative_to(repository)),
        "profile_sha256": sha256_file(profile_path),
        "source_campaign": {
            "relative_path": source["relative_path"],
            "kauri_revision": source["kauri_revision"],
            "campaign_plan_sha256": source["campaign_plan_sha256"],
            "campaign_verdict_sha256": source["campaign_verdict_sha256"],
            "original_verdict": verdict["verdict"],
        },
        "run_count": len(runs),
        "arm_counts": dict(sorted(arm_counts.items())),
        "reputation_mechanisms": list(MECHANISMS),
        "shared_policy": profile["responsiveness_policy"],
        "role_projection": profile["role_projection"],
        "runs": runs,
        "summaries": summaries,
        "claims_not_made": profile["claims_not_made"],
        "verdict": "PASS",
    }


__all__ = [
    "ReputationPolicyReplayError",
    "build_campaign",
    "canonical_json",
    "load_bound_arm_verdict",
    "mechanisms_share_eligibility",
    "rank_replicas",
    "sha256_file",
]
