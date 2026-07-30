"""Fixed N=7 development comparison for three bounded fault modes.

This module freezes equivalent FI-Core plans and binds three validated run
verdicts into one summary.  It performs no diagnosis, rematching, statistical
analysis, or campaign selection.
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
    faulty_replica_id: int
    diagnostic_fault_bound: int
    arms: tuple[FaultComparisonArm, ...]


def build_n7_comparison(
    *,
    kauri_revision: str,
    seed: int,
    faulty_replica_id: int,
    false_report_target_id: int,
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
    if faulty_replica_id not in membership:
        raise ComparisonError("faulty replica is outside N=7 membership")
    if false_report_target_id not in membership:
        raise ComparisonError(
            "false-report target is outside N=7 membership"
        )
    if false_report_target_id == faulty_replica_id:
        raise ComparisonError("false reporter and target must be distinct")
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
                    fault_id=f"crash-replica-{faulty_replica_id}",
                    replica_id=faulty_replica_id,
                ),
            ),
        ),
        FaultPlan(
            context=context,
            seed=seed,
            actions=(
                StaticAuthenticatedFalseReport(
                    fault_id=(
                        f"false-report-{faulty_replica_id}-to-"
                        f"{false_report_target_id}"
                    ),
                    reporter_id=faulty_replica_id,
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
                        f"persistent-omission-{faulty_replica_id}"
                    ),
                    replica_id=faulty_replica_id,
                    diagnostic_window=diagnostic_window,
                ),
            ),
        ),
    )
    arms = tuple(
        FaultComparisonArm(
            name=name,
            kauri_revision=kauri_revision,
            faulty_replica_id=faulty_replica_id,
            plan=plan,
        )
        for name, plan in zip(_ARM_NAMES, plans, strict=True)
    )
    return FaultComparison(
        kauri_revision=kauri_revision,
        seed=seed,
        faulty_replica_id=faulty_replica_id,
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
        summary_arms.append(
            {
                "name": arm.name,
                "run_id": run_id,
                "verdict": "PASS",
                "fault_plan_sha256": arm.plan.sha256,
            }
        )

    return {
        "schema_version": 1,
        "scenario": "n7-static-fault-comparison",
        "kauri_revision": comparison.kauri_revision,
        "seed": comparison.seed,
        "faulty_replica_id": comparison.faulty_replica_id,
        "diagnostic_fault_bound": (
            comparison.diagnostic_fault_bound
        ),
        "arms": summary_arms,
    }
