"""Fixed N=7 development comparison for three bounded fault modes.

This module freezes one crash identity and two Byzantine causes that produce
the same initial reporter-target timeout syndrome.  It binds three validated
run verdicts into one summary and performs no diagnosis, rematching,
statistical analysis, or campaign selection.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from .faults import (
    FaultPlan,
    ReplicaGroupSigkill,
    ScenarioContext,
    StaticAuthenticatedFalseReport,
    StaticPersistentOmission,
)


_ARM_NAMES = (
    "sigkill_crash",
    "static_authenticated_false_report",
    "static_persistent_omission",
)
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class ComparisonError(ValueError):
    """The fixed comparison or one of its verdicts is incomplete."""


@dataclass(frozen=True, slots=True)
class FaultComparisonArm:
    name: str
    kauri_revision: str
    faulty_replica_id: int
    plan: FaultPlan


@dataclass(frozen=True, slots=True)
class FaultComparison:
    kauri_revision: str
    seed: int
    crash_replica_id: int
    false_reporter_id: int
    false_report_target_id: int
    persistent_omitter_id: int
    diagnostic_fault_bound: int
    arms: tuple[FaultComparisonArm, ...]


def _lower_hex_256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _matched_syndrome_observation_id(
    comparison: FaultComparison,
    arm: FaultComparisonArm,
    verdict: Mapping[str, object],
    *,
    run_id: str,
) -> str | None:
    """Validate the evidence behind the shareable syndrome claim."""

    accepted = verdict.get("manager_accepted_timeout_observation")
    if arm.name == "sigkill_crash":
        if accepted is not None:
            raise ComparisonError(
                "crash arm unexpectedly carries Byzantine timeout evidence"
            )
        return None
    action = verdict.get("action_observation")
    if not isinstance(action, Mapping) or not isinstance(accepted, Mapping):
        raise ComparisonError(
            f"arm {arm.name} has no matched manager timeout evidence"
        )
    payload = accepted.get("payload")
    if not isinstance(payload, Mapping):
        raise ComparisonError(
            f"arm {arm.name} has malformed manager timeout evidence"
        )
    observation = payload.get("observation")
    if not isinstance(observation, Mapping):
        raise ComparisonError(
            f"arm {arm.name} has malformed manager timeout observation"
        )
    configuration = observation.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ComparisonError(
            f"arm {arm.name} has malformed timeout configuration"
        )
    observation_id = observation.get("observation_id")
    block_hash = observation.get("block_hash")
    epoch_digest = configuration.get("epoch_digest")
    exact_configuration = (
        f"0:6:{epoch_digest}" if isinstance(epoch_digest, str) else None
    )
    expected_action = {
        "static_authenticated_false_report": (
            "false_timeout_emitted",
            f"replica-{comparison.false_reporter_id}",
        ),
        "static_persistent_omission": (
            "aggregate_omitted",
            f"replica-{comparison.persistent_omitter_id}",
        ),
    }.get(arm.name)
    if not (
        expected_action is not None
        and accepted.get("event_schema_version") == 1
        and accepted.get("run_id") == run_id
        and accepted.get("source_kind") == "adaptation_manager"
        and accepted.get("source_id") == "adaptive-manager"
        and accepted.get("event_type") == "evidence.observation_accepted"
        and isinstance(accepted.get("source_instance"), str)
        and bool(accepted.get("source_instance"))
        and isinstance(accepted.get("source_sequence"), int)
        and not isinstance(accepted.get("source_sequence"), bool)
        and accepted["source_sequence"] > 0
        and isinstance(payload.get("ingestion_sequence"), int)
        and not isinstance(payload.get("ingestion_sequence"), bool)
        and payload["ingestion_sequence"] > 0
        and _lower_hex_256(observation_id)
        and observation.get("reporter_id")
        == comparison.false_reporter_id
        and observation.get("observed_replica_id")
        == comparison.false_report_target_id
        and configuration.get("epoch_number") == 0
        and configuration.get("tree_id") == 6
        and _lower_hex_256(epoch_digest)
        and _lower_hex_256(block_hash)
        and action.get("block_hash") == block_hash
        and action.get("configuration") == exact_configuration
        and action.get("kind") == expected_action[0]
        and action.get("source_id") == expected_action[1]
        and observation.get("expected_message_type")
        == "aggregate_relay"
        and observation.get("outcome") == "timeout"
        and observation.get("response_duration_us") == 0
        and observation.get("signer_set") == []
    ):
        raise ComparisonError(
            f"arm {arm.name} does not prove the matched timeout syndrome"
        )
    assert isinstance(observation_id, str)
    return observation_id


def build_n7_comparison(
    *,
    kauri_revision: str,
    seed: int,
    crash_replica_id: int,
    false_reporter_id: int,
    false_report_target_id: int,
    persistent_omitter_id: int,
    diagnostic_window: str,
) -> FaultComparison:
    """Build the one fixed three-arm N=7 comparison contract."""
    if not isinstance(kauri_revision, str) or not _REVISION.fullmatch(
        kauri_revision
    ):
        raise ComparisonError("Kauri revision must be a full lowercase SHA")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ComparisonError("seed must be an integer")
    membership = tuple(range(7))
    if crash_replica_id not in membership:
        raise ComparisonError("crash replica is outside N=7 membership")
    if false_reporter_id not in membership:
        raise ComparisonError("false reporter is outside N=7 membership")
    if false_report_target_id not in membership:
        raise ComparisonError(
            "false-report target is outside N=7 membership"
        )
    if persistent_omitter_id not in membership:
        raise ComparisonError(
            "persistent omitter is outside N=7 membership"
        )
    if false_report_target_id == false_reporter_id:
        raise ComparisonError("false reporter and target must be distinct")
    if persistent_omitter_id != false_report_target_id:
        raise ComparisonError(
            "Byzantine arms must share the same reporter-target syndrome"
        )
    if not isinstance(diagnostic_window, str) or not diagnostic_window.strip():
        raise ComparisonError("diagnostic window must be non-empty")

    context = ScenarioContext(
        replica_ids=membership,
        quorum=5,
        crash_budget=2,
        successor_bundle_retry_limit=5,
        diagnostic_fault_bound=1,
    )
    plans = (
        FaultPlan(
            context=context,
            seed=seed,
            actions=(
                ReplicaGroupSigkill(
                    fault_id=f"crash-replica-{crash_replica_id}",
                    replica_id=crash_replica_id,
                ),
            ),
        ),
        FaultPlan(
            context=context,
            seed=seed,
            actions=(
                StaticAuthenticatedFalseReport(
                    fault_id=(
                        f"false-report-{false_reporter_id}-to-"
                        f"{false_report_target_id}"
                    ),
                    reporter_id=false_reporter_id,
                    target_id=false_report_target_id,
                    reported_outcome="timeout",
                    diagnostic_window=diagnostic_window,
                ),
            ),
        ),
        FaultPlan(
            context=context,
            seed=seed,
            actions=(
                StaticPersistentOmission(
                    fault_id=(
                        f"persistent-omission-{persistent_omitter_id}"
                    ),
                    replica_id=persistent_omitter_id,
                    diagnostic_window=diagnostic_window,
                ),
            ),
        ),
    )
    faulty_replicas = (
        crash_replica_id,
        false_reporter_id,
        persistent_omitter_id,
    )
    arms = tuple(
        FaultComparisonArm(
            name=name,
            kauri_revision=kauri_revision,
            faulty_replica_id=faulty_replica,
            plan=plan,
        )
        for name, faulty_replica, plan in zip(
            _ARM_NAMES,
            faulty_replicas,
            plans,
            strict=True,
        )
    )
    return FaultComparison(
        kauri_revision=kauri_revision,
        seed=seed,
        crash_replica_id=crash_replica_id,
        false_reporter_id=false_reporter_id,
        false_report_target_id=false_report_target_id,
        persistent_omitter_id=persistent_omitter_id,
        diagnostic_fault_bound=1,
        arms=arms,
    )


def summarize_comparison(
    comparison: FaultComparison,
    verdicts: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """Return a summary only when all exact arms independently pass."""
    expected = {arm.name for arm in comparison.arms}
    if set(verdicts) != expected or tuple(
        arm.name for arm in comparison.arms
    ) != _ARM_NAMES:
        raise ComparisonError(
            "comparison requires exactly three named arm verdicts"
        )

    summary_arms: list[dict[str, object]] = []
    for arm in comparison.arms:
        verdict = verdicts[arm.name]
        if verdict.get("verdict") != "PASS":
            raise ComparisonError(
                f"arm {arm.name} verdict must be PASS"
            )
        if verdict.get("kauri_revision") != comparison.kauri_revision:
            raise ComparisonError(
                f"arm {arm.name} is not bound to the frozen revision"
            )
        if verdict.get("fault_plan_sha256") != arm.plan.sha256:
            raise ComparisonError(
                f"arm {arm.name} is not bound to its fault plan sha256"
            )
        run_id = verdict.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ComparisonError(f"arm {arm.name} has no run id")
        accepted_observation_id = _matched_syndrome_observation_id(
            comparison,
            arm,
            verdict,
            run_id=run_id,
        )
        summary_arms.append(
            {
                "name": arm.name,
                "run_id": run_id,
                "verdict": "PASS",
                "fault_plan_sha256": arm.plan.sha256,
                "manager_accepted_observation_id": (
                    accepted_observation_id
                ),
            }
        )

    return {
        "schema_version": 1,
        "scenario": "n7-static-fault-comparison",
        "kauri_revision": comparison.kauri_revision,
        "seed": comparison.seed,
        "fault_identities": {
            "crash_replica_id": comparison.crash_replica_id,
            "false_reporter_id": comparison.false_reporter_id,
            "false_report_target_id": (
                comparison.false_report_target_id
            ),
            "persistent_omitter_id": (
                comparison.persistent_omitter_id
            ),
        },
        "initial_byzantine_syndrome": {
            "reporter_id": comparison.false_reporter_id,
            "target_id": comparison.false_report_target_id,
            "outcome": "timeout",
        },
        "diagnostic_fault_bound": (
            comparison.diagnostic_fault_bound
        ),
        "arms": summary_arms,
    }
