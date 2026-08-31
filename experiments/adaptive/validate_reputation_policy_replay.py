#!/usr/bin/env python3
"""Independently validate the frozen N=7 reputation-policy replay."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from statistics import median
import subprocess
import sys
from typing import Any, Mapping, Sequence


KAURI_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MECHANISMS = ("responsiveness", "latency-priority")


class ValidationError(ValueError):
    """Raised when replay evidence does not reconstruct exactly."""


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValidationError(f"{path} is not an object")
    return value


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} is not an object")
    return value


def _array(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} is not an array")
    return value


def _int(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValidationError(f"{label} is invalid")
    return value


@dataclass
class _Attempt:
    first_sequence: int
    replica: int
    state: str
    latency_us: int
    deadline_us: int


def _accepted(raw_path: Path, epoch_number: int) -> tuple[list[dict[str, Any]], str]:
    accepted: list[dict[str, Any]] = []
    digest: str | None = None
    last_sequence = 0
    with raw_path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValidationError(
                    f"{raw_path}:{line_number} is invalid JSON"
                ) from error
            if event.get("event_type") != "evidence.observation_accepted":
                continue
            payload = _object(event.get("payload"), "accepted payload")
            sequence = _int(payload.get("ingestion_sequence"), "sequence", 1)
            if sequence <= last_sequence:
                raise ValidationError("accepted sequence is not strictly increasing")
            last_sequence = sequence
            observation = dict(_object(payload.get("observation"), "observation"))
            configuration = _object(
                observation.get("configuration"), "configuration"
            )
            if configuration.get("epoch_number") != epoch_number:
                raise ValidationError("observation is outside the frozen epoch")
            current_digest = configuration.get("epoch_digest")
            if not isinstance(current_digest, str) or len(current_digest) != 64:
                raise ValidationError("epoch digest is invalid")
            if digest is None:
                digest = current_digest
            elif current_digest != digest:
                raise ValidationError("accepted stream mixes epoch digests")
            observation["_sequence"] = sequence
            accepted.append(observation)
    if not accepted or digest is None:
        raise ValidationError("accepted stream is empty")
    return accepted, digest


def _replay_attempts(
    observations: Sequence[Mapping[str, Any]], membership: set[int]
) -> list[_Attempt]:
    attempts: dict[str, _Attempt] = {}
    for observation in observations:
        identity = observation.get("observation_id")
        if not isinstance(identity, str) or len(identity) != 64:
            raise ValidationError("observation id is invalid")
        replica = _int(observation.get("observed_replica_id"), "replica")
        if replica not in membership:
            raise ValidationError("observation names a nonmember")
        if observation.get("expected_message_type") not in {
            "aggregate_relay",
            "direct_vote",
        }:
            raise ValidationError("message type is outside the frozen domain")
        outcome = observation.get("outcome")
        if outcome not in {"on_time", "timeout", "late"}:
            raise ValidationError("outcome is invalid")
        latency = _int(observation.get("response_duration_us"), "latency")
        deadline = _int(observation.get("deadline_duration_us"), "deadline", 1)
        sequence = _int(observation.get("_sequence"), "sequence", 1)
        prior = attempts.get(identity)
        if prior is None:
            if outcome == "late" or (outcome == "timeout" and latency != 0):
                raise ValidationError("attempt starts in an invalid state")
            attempts[identity] = _Attempt(
                first_sequence=sequence,
                replica=replica,
                state="timeout_only" if outcome == "timeout" else "on_time",
                latency_us=latency,
                deadline_us=deadline,
            )
        else:
            if (
                prior.replica != replica
                or prior.state != "timeout_only"
                or outcome != "late"
                or prior.deadline_us != deadline
                or latency < deadline
            ):
                raise ValidationError("timeout-to-late transition is invalid")
            prior.state = "late"
            prior.latency_us = latency
    return list(attempts.values())


def _independent_rows(
    observations: Sequence[Mapping[str, Any]],
    membership: Sequence[int],
    policy: Mapping[str, Any],
    mechanism: str,
) -> list[dict[str, Any]]:
    grouped: dict[int, list[_Attempt]] = defaultdict(list)
    for attempt in _replay_attempts(observations, set(membership)):
        grouped[attempt.replica].append(attempt)
    rows: list[dict[str, Any]] = []
    for replica in membership:
        attempts = sorted(
            grouped[replica], key=lambda item: item.first_sequence
        )[-_int(policy.get("attempt_window"), "attempt window", 1) :]
        counts = Counter(item.state for item in attempts)
        attempt_count = len(attempts)
        response_count = counts["on_time"] + counts["late"]
        timeout_count = counts["timeout_only"] + counts["late"]
        response_rate = (
            response_count * 1_000_000 // attempt_count if attempt_count else 0
        )
        timeout_rate = (
            timeout_count * 1_000_000 // attempt_count if attempt_count else 0
        )
        trailing = 0
        for attempt in reversed(attempts):
            if attempt.state != "timeout_only":
                break
            trailing += 1
        latencies = sorted(
            item.latency_us
            for item in attempts
            if item.state in {"on_time", "late"}
        )
        percentile = _int(
            policy.get("latency_percentile_basis_points"), "percentile", 1
        )
        latency = (
            latencies[math.ceil(percentile * len(latencies) / 10_000) - 1]
            if latencies
            else None
        )
        reasons: list[str] = []
        if attempt_count < _int(policy.get("minimum_attempts"), "minimum", 1):
            classification = "insufficient_evidence"
            reasons.append("insufficient_attempts")
        else:
            if response_rate < _int(
                policy.get("minimum_response_rate_ppm"), "response floor"
            ):
                reasons.append("response_rate_below_minimum")
            if timeout_rate > _int(
                policy.get("maximum_timeout_rate_ppm"), "timeout ceiling"
            ):
                reasons.append("timeout_rate_above_maximum")
            if trailing >= _int(
                policy.get("trailing_timeout_streak"), "timeout streak", 2
            ):
                reasons.append("persistent_timeout_streak")
            classification = "nonresponsive" if reasons else "responsive"
        rows.append(
            {
                "replica_id": replica,
                "attempt_count": attempt_count,
                "on_time_count": counts["on_time"],
                "timeout_only_count": counts["timeout_only"],
                "late_count": counts["late"],
                "response_count": response_count,
                "timeout_count": timeout_count,
                "trailing_timeout_count": trailing,
                "response_rate_ppm": response_rate,
                "timeout_rate_ppm": timeout_rate,
                "latency_percentile_us": latency,
                "classification": classification,
                "eligible": classification == "responsive",
                "reasons": reasons,
            }
        )

    def key(row: Mapping[str, Any]) -> tuple[Any, ...]:
        latency_value = row["latency_percentile_us"]
        latency_key = (0 if latency_value is not None else 1, latency_value or 0)
        if mechanism == "latency-priority":
            return (
                0 if row["eligible"] else 1,
                *latency_key,
                -int(row["response_rate_ppm"]),
                int(row["timeout_rate_ppm"]),
                -int(row["attempt_count"]),
                int(row["replica_id"]),
            )
        return (
            0 if row["eligible"] else 1,
            -int(row["response_rate_ppm"]),
            int(row["timeout_rate_ppm"]),
            *latency_key,
            -int(row["attempt_count"]),
            int(row["replica_id"]),
        )

    rows.sort(key=key)
    for index, row in enumerate(rows):
        row["rank"] = index
    return rows


def _discover_run(campaign_root: Path, ordinal: int, arm: str) -> Path:
    root = campaign_root / f"attempt-{ordinal:02d}-results" / arm
    candidates = sorted(path for path in root.iterdir() if path.is_dir())
    if len(candidates) != 1:
        raise ValidationError("source attempt does not bind exactly one run")
    return candidates[0]


def validate(artifact_path: Path, profile_path: Path) -> dict[str, Any]:
    artifact = _load(artifact_path)
    profile = _load(profile_path)
    if profile.get("frozen") is not True or profile.get("schema_version") != 1:
        raise ValidationError("profile is not frozen schema v1")
    if artifact.get("verdict") != "PASS" or artifact.get("schema_version") != 1:
        raise ValidationError("producer artifact is not schema-v1 PASS")
    current_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=KAURI_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if (
        artifact.get("analysis_revision") != current_revision
        or artifact.get("revision_verification")
        != "verified_current_clean_head"
    ):
        raise ValidationError("artifact is not bound to the current clean revision")
    if artifact.get("profile_sha256") != _hash(profile_path):
        raise ValidationError("producer profile hash does not match")
    source = _object(profile.get("source_campaign"), "source campaign")
    campaign_root = KAURI_REPOSITORY_ROOT / str(source["relative_path"])
    plan_path = campaign_root / "campaign-plan.json"
    verdict_path = campaign_root / "n7-fault-repetition-campaign.json"
    if _hash(plan_path) != source.get("campaign_plan_sha256"):
        raise ValidationError("source campaign plan hash drifted")
    if _hash(verdict_path) != source.get("campaign_verdict_sha256"):
        raise ValidationError("source campaign verdict hash drifted")
    if _load(verdict_path).get("verdict") != "PASS":
        raise ValidationError("source campaign verdict is not PASS")
    plan = _load(plan_path)
    if plan.get("retry_policy") != "none":
        raise ValidationError("source campaign allowed retries")
    scheduled = _array(plan.get("scheduled_attempts"), "scheduled attempts")
    artifact_runs = {
        _int(run.get("ordinal"), "artifact ordinal", 1): run
        for run in (
            _object(item, "artifact run")
            for item in _array(artifact.get("runs"), "artifact runs")
        )
    }
    if len(artifact_runs) != len(scheduled) == source.get("expected_run_count"):
        raise ValidationError("replay run count is not exact")
    membership = tuple(_int(item, "membership") for item in profile["membership"])
    policy = _object(profile.get("responsiveness_policy"), "policy")
    projection = _object(profile.get("role_projection"), "projection")
    influential_count = _int(projection.get("root_count"), "root count", 1) + _int(
        projection.get("internal_count"), "internal count"
    )
    arm_counts: Counter[str] = Counter()
    ranking_differences = 0
    source_observations = 0
    summary_inputs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for slot_value in scheduled:
        slot = _object(slot_value, "scheduled attempt")
        ordinal = _int(slot.get("ordinal"), "ordinal", 1)
        arm = slot.get("arm")
        if not isinstance(arm, str):
            raise ValidationError("arm is invalid")
        arm_counts[arm] += 1
        recorded = artifact_runs[ordinal]
        if recorded.get("arm") != arm:
            raise ValidationError("artifact arm differs from frozen schedule")
        execution_path = campaign_root / f"attempt-{ordinal:02d}-execution.json"
        run_directory = _discover_run(campaign_root, ordinal, arm)
        raw_path = run_directory / "raw/adaptive-manager.jsonl"
        fault_path = run_directory / "fault-plan.json"
        if (
            recorded.get("source_execution_sha256") != _hash(execution_path)
            or recorded.get("source_manager_sha256") != _hash(raw_path)
            or recorded.get("source_fault_plan_sha256") != _hash(fault_path)
        ):
            raise ValidationError("artifact source hash binding drifted")
        observations, epoch_digest = _accepted(
            raw_path, int(profile["evidence_window"]["epoch_number"])
        )
        source_observations += len(observations)
        if (
            recorded.get("epoch_digest") != epoch_digest
            or recorded.get("evidence_cutoff") != observations[-1]["_sequence"]
            or recorded.get("accepted_observation_count") != len(observations)
        ):
            raise ValidationError("artifact evidence boundary drifted")
        reconstructed_eligibility: set[tuple[int, ...]] = set()
        reconstructed_results: dict[str, dict[str, Any]] = {}
        for mechanism in MECHANISMS:
            rows = _independent_rows(observations, membership, policy, mechanism)
            result = _object(
                _object(recorded.get("policy_results"), "policy results").get(
                    mechanism
                ),
                "mechanism result",
            )
            if result.get("ranking") != rows:
                raise ValidationError(
                    f"independent {mechanism} ranking differs in attempt {ordinal}"
                )
            eligible = tuple(
                row["replica_id"] for row in rows if row["eligible"]
            )
            reconstructed_eligibility.add(tuple(sorted(eligible)))
            if result.get("eligible_ids") != list(eligible):
                raise ValidationError("eligible ranking differs")
            if len(eligible) < influential_count:
                raise ValidationError("eligible set cannot fill projected roles")
            expected_projection = {
                "root_id": eligible[0],
                "internal_ids": list(eligible[1:influential_count]),
                "influential_ids": list(eligible[:influential_count]),
            }
            if result.get("role_projection") != expected_projection:
                raise ValidationError("role projection is not top-ranked eligibility")
            reconstructed_results[mechanism] = {
                "rows": rows,
                "projection": expected_projection,
            }
        if len(reconstructed_eligibility) != 1:
            raise ValidationError("mechanisms changed the shared eligibility boundary")
        policy_results = _object(recorded.get("policy_results"), "policy results")
        left = [
            row["replica_id"]
            for row in _object(
                policy_results.get("responsiveness"), "responsiveness result"
            )["ranking"]
        ]
        right = [
            row["replica_id"]
            for row in _object(
                policy_results.get("latency-priority"), "latency result"
            )["ranking"]
        ]
        inversions = sum(
            right.index(left[first]) > right.index(left[second])
            for first in range(len(left))
            for second in range(first + 1, len(left))
        )
        comparison = _object(recorded.get("comparison"), "comparison")
        left_projection = reconstructed_results["responsiveness"]["projection"]
        right_projection = reconstructed_results["latency-priority"]["projection"]
        expected_comparison = {
            "kendall_inversion_count": inversions,
            "root_changed": (
                left_projection["root_id"] != right_projection["root_id"]
            ),
            "influential_cohort_changed": (
                set(left_projection["influential_ids"])
                != set(right_projection["influential_ids"])
            ),
            "influential_overlap_count": len(
                set(left_projection["influential_ids"])
                & set(right_projection["influential_ids"])
            ),
        }
        if dict(comparison) != expected_comparison:
            raise ValidationError("policy comparison metrics differ")
        ranking_differences += inversions > 0
        actions = _array(_load(fault_path).get("actions"), "fault actions")
        if len(actions) != 1:
            raise ValidationError("fault plan does not contain exactly one action")
        action = _object(actions[0], "fault action")
        post_hoc = _object(
            recorded.get("post_hoc_fault_evaluation"), "post-hoc evaluation"
        )
        if post_hoc.get("fault_action") != action:
            raise ValidationError("post-hoc ground-truth label drifted")
        if action.get("kind") in {
            "replica_group_sigkill",
            "static_persistent_omission",
        }:
            actor = _int(action.get("replica_id"), "fault actor")
            target: int | None = None
        elif action.get("kind") == "static_authenticated_false_report":
            actor = _int(action.get("reporter_id"), "false reporter")
            target = _int(action.get("target_id"), "false-report target")
        else:
            raise ValidationError("fault action is outside the frozen set")
        expected_post_hoc: dict[str, Any] = {
            "fault_action": dict(action),
            "actor_id": actor,
            "target_id": target,
            "by_mechanism": {},
        }
        for mechanism in MECHANISMS:
            rows = reconstructed_results[mechanism]["rows"]
            positions = {row["replica_id"]: row["rank"] for row in rows}
            influential = reconstructed_results[mechanism]["projection"][
                "influential_ids"
            ]
            expected_post_hoc["by_mechanism"][mechanism] = {
                "actor_rank": positions[actor],
                "actor_in_influential_roles": actor in influential,
                "target_rank": positions[target] if target is not None else None,
                "target_in_influential_roles": (
                    target in influential if target is not None else None
                ),
            }
        if dict(post_hoc) != expected_post_hoc:
            raise ValidationError("post-hoc evaluation differs")
        summary_inputs[arm].append(
            {
                "comparison": expected_comparison,
                "post_hoc": expected_post_hoc,
            }
        )

    expected_arms = set(source["expected_arms"])
    repetitions = int(source["expected_repetitions_per_arm"])
    if set(arm_counts) != expected_arms or any(
        count != repetitions for count in arm_counts.values()
    ):
        raise ValidationError("source campaign balance drifted")
    if artifact.get("arm_counts") != dict(sorted(arm_counts.items())):
        raise ValidationError("artifact arm summary drifted")
    if artifact.get("run_count") != len(scheduled):
        raise ValidationError("artifact run-count summary drifted")
    expected_summaries: dict[str, Any] = {}
    for arm in source["expected_arms"]:
        arm_runs = summary_inputs[arm]
        mechanism_summaries: dict[str, Any] = {}
        for mechanism in MECHANISMS:
            evaluations = [
                item["post_hoc"]["by_mechanism"][mechanism]
                for item in arm_runs
            ]
            mechanism_summaries[mechanism] = {
                "median_actor_rank_zero_based": float(
                    median(item["actor_rank"] for item in evaluations)
                ),
                "actor_excluded_from_influential_roles_count": sum(
                    not item["actor_in_influential_roles"]
                    for item in evaluations
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
        expected_summaries[arm] = {
            "run_count": len(arm_runs),
            "root_changed_count": sum(
                item["comparison"]["root_changed"] for item in arm_runs
            ),
            "influential_cohort_changed_count": sum(
                item["comparison"]["influential_cohort_changed"]
                for item in arm_runs
            ),
            "median_kendall_inversion_count": float(
                median(
                    item["comparison"]["kendall_inversion_count"]
                    for item in arm_runs
                )
            ),
            "mechanisms": mechanism_summaries,
        }
    if artifact.get("summaries") != expected_summaries:
        raise ValidationError("artifact arm summaries drifted")
    return {
        "schema_version": 1,
        "scenario": "n7-reputation-policy-offline-replay-validation",
        "verdict": "PASS",
        "artifact_sha256": _hash(artifact_path),
        "profile_sha256": _hash(profile_path),
        "source_campaign_plan_sha256": _hash(plan_path),
        "source_campaign_verdict_sha256": _hash(verdict_path),
        "independently_reconstructed_runs": len(scheduled),
        "accepted_observations_replayed": source_observations,
        "runs_with_distinct_rankings": ranking_differences,
        "checks": [
            "source campaign is exact no-retry PASS",
            "all source file hashes match",
            "both rankings independently reconstruct from accepted observations",
            "both mechanisms preserve one shared eligibility boundary",
            "projected roles are filled only from eligible rankings",
            "fault identities are used only in post-hoc evaluation",
        ],
        "claim_scope": "offline policy replay over accepted live evidence",
        "claims_not_made": profile["claims_not_made"],
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument(
        "--profile",
        type=Path,
        default=(
            KAURI_REPOSITORY_ROOT
            / "experiments/adaptive/profiles/n7-reputation-policy-replay-v1.json"
        ),
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    output = arguments.output or arguments.artifact.with_name("validation.json")
    try:
        if output.exists():
            raise ValidationError("validation output already exists")
        verdict = validate(arguments.artifact.resolve(), arguments.profile.resolve())
        output.write_text(
            json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, subprocess.CalledProcessError, ValidationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
