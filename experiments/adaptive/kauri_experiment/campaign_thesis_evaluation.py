"""Compose the model-policy and live-observation thesis evidence.

The two source campaigns answer different questions.  The N=31 campaign
evaluates the planning policy in a deterministic model, while the N=7
campaign checks that the passive observations used by that model are present
in bounded live executions.  This module binds those results without
presenting the live campaign as a planner deployment.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .comparison import build_n7_comparison
from .n7_fault_repetition_campaign import (
    N7FaultRepetitionCampaignError,
    canonical_n7_fault_repetition_json,
    validate_n7_fault_repetition_campaign,
)
from .planning_breadth_campaign import (
    PlanningBreadthError,
    canonical_planning_breadth_json,
    validate_planning_breadth_campaign,
)

EVALUATION_CLASS = "model-policy evaluation with live observation calibration"
EXCLUSIONS = (
    "no live planner activation",
    "no throughput or latency speedup claim",
    "no general Byzantine identification or consensus-safety proof",
)

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_ARM_NAMES = (
    "sigkill_crash",
    "static_authenticated_false_report",
    "static_persistent_omission",
)


class CampaignThesisEvaluationError(ValueError):
    """The source campaigns cannot support the composed claim."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CampaignThesisEvaluationError(f"{label} must be an object")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CampaignThesisEvaluationError(f"{label} must be an integer")
    return value


def _revision(value: object, label: str) -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise CampaignThesisEvaluationError(
            f"{label} must be 40 lowercase hexadecimal characters"
        )
    return value


def _canonical_json(value: Mapping[str, Any], label: str) -> str:
    try:
        return json.dumps(
            dict(value),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise CampaignThesisEvaluationError(f"{label} is not canonical JSON") from error


def _sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _live_comparison(campaign: Mapping[str, Any]) -> Any:
    revision = _revision(campaign.get("kauri_revision"), "live revision")
    seed = _integer(campaign.get("seed"), "live seed")
    try:
        return build_n7_comparison(
            kauri_revision=revision,
            seed=seed,
            crash_replica_id=1,
            false_reporter_id=6,
            false_report_target_id=1,
            persistent_omitter_id=1,
            diagnostic_window="n7-epoch0-tree6-tree0-static-v1",
        )
    except ValueError as error:
        raise CampaignThesisEvaluationError(
            f"live comparison contract is invalid: {error}"
        ) from error


def _validate_planning_contract(
    campaign: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        validated = validate_planning_breadth_campaign(campaign)
    except PlanningBreadthError as error:
        raise CampaignThesisEvaluationError(
            f"planning source failed strict validation: {error}"
        ) from error
    if validated.get("revision_verification") != "verified_current_clean_head":
        raise CampaignThesisEvaluationError(
            "planning source is not bound to a verified current clean head"
        )

    validation = _mapping(validated.get("validation"), "planning validation")
    summary = _mapping(validated.get("summary"), "planning summary")
    canonical = _mapping(
        summary.get("canonical_greedy"),
        "canonical greedy summary",
    )
    if not (
        validated.get("verdict") == "PASS"
        and _integer(validated.get("scenario_count"), "scenario count") == 1_000
        and _integer(
            validation.get("solver_reference_agreements"),
            "solver agreements",
        )
        == 1_000
        and _integer(validation.get("lookahead_losses"), "lookahead losses") == 0
        and _integer(validation.get("cost_regressions"), "cost regressions") == 0
        and _integer(canonical.get("wins"), "canonical wins") == 23
        and _integer(canonical.get("ties"), "canonical ties") == 977
        and _integer(canonical.get("losses"), "canonical losses") == 0
        and _integer(
            summary.get("tie_robust_lookahead_wins"),
            "tie-robust wins",
        )
        == 4
        and _integer(summary.get("lookahead_losses"), "summary losses") == 0
        and _integer(
            summary.get("cost_regressions"),
            "summary cost regressions",
        )
        == 0
    ):
        raise CampaignThesisEvaluationError(
            "planning source does not meet the frozen thesis gate"
        )
    return deepcopy(validated)


def _validate_live_contract(
    campaign: Mapping[str, Any],
    *,
    evidence_root: Path,
) -> dict[str, Any]:
    if not isinstance(evidence_root, Path):
        raise CampaignThesisEvaluationError(
            "live source failed strict validation: a bound evidence root is required"
        )
    comparison = _live_comparison(campaign)
    try:
        bound_evidence_root = evidence_root.resolve(strict=True)
        validated = validate_n7_fault_repetition_campaign(
            comparison,
            campaign,
            evidence_root=bound_evidence_root,
        )
    except (N7FaultRepetitionCampaignError, OSError, ValueError) as error:
        raise CampaignThesisEvaluationError(
            f"live source failed strict validation: {error}"
        ) from error

    pass_counts = _mapping(validated.get("pass_counts"), "live pass counts")
    continuity = _mapping(
        validated.get("post_fault_commit_continuity"),
        "post-fault commit continuity",
    )
    outcomes = _mapping(
        validated.get("diagnostic_outcomes"),
        "diagnostic outcomes",
    )
    execution_binding = _mapping(
        validated.get("execution_binding"),
        "execution binding",
    )
    campaign_plan = _mapping(
        execution_binding.get("campaign_plan"),
        "campaign plan",
    )
    records = execution_binding.get("execution_records")
    if not isinstance(records, list):
        raise CampaignThesisEvaluationError("execution binding records must be a list")

    for arm in _ARM_NAMES:
        arm_continuity = _mapping(continuity.get(arm), f"{arm} continuity")
        if not (
            _integer(pass_counts.get(arm), f"{arm} pass count") == 5
            and _integer(arm_continuity.get("witnessed"), f"{arm} witnessed") == 5
            and _integer(arm_continuity.get("scheduled"), f"{arm} scheduled") == 5
        ):
            raise CampaignThesisEvaluationError(
                f"live source does not contain five valid {arm} slots"
            )

    false_report = _mapping(
        outcomes.get("static_authenticated_false_report"),
        "false-report outcomes",
    )
    omission = _mapping(
        outcomes.get("static_persistent_omission"),
        "omission outcomes",
    )
    if not (
        validated.get("verdict") == "PASS"
        and _integer(validated.get("scheduled_attempts"), "scheduled attempts") == 15
        and _integer(validated.get("preserved_attempts"), "preserved attempts") == 15
        and validated.get("missing_attempts") == []
        and validated.get("failed_attempts") == []
        and validated.get("strict_validation_failures") == []
        and validated.get("execution_binding_failures") == []
        and campaign_plan.get("retry_policy") == "none"
        and _integer(
            campaign_plan.get("scheduled_attempt_count"),
            "planned slot count",
        )
        == 15
        and len(records) == 15
        and _integer(
            false_report.get("settled_false_reporter"),
            "false-report settlements",
        )
        == 5
        and _integer(
            omission.get("settled_persistent_omitter"),
            "omission settlements",
        )
        == 5
    ):
        raise CampaignThesisEvaluationError(
            "live source does not meet the frozen thesis gate"
        )
    return deepcopy(validated)


def build_campaign_thesis_evaluation(
    planning_campaign: Mapping[str, Any],
    live_campaign: Mapping[str, Any],
    *,
    live_evidence_root: Path,
) -> dict[str, Any]:
    """Validate, bind, and summarize both campaigns and raw live evidence."""

    planning = _validate_planning_contract(planning_campaign)
    live = _validate_live_contract(
        live_campaign,
        evidence_root=live_evidence_root,
    )
    planning_revision = _revision(
        planning.get("kauri_revision"),
        "planning revision",
    )
    live_revision = _revision(live.get("kauri_revision"), "live revision")
    if planning_revision != live_revision:
        raise CampaignThesisEvaluationError(
            "source campaigns use different Kauri revisions"
        )

    planning_summary = _mapping(planning["summary"], "planning summary")
    canonical = _mapping(
        planning_summary["canonical_greedy"],
        "canonical greedy summary",
    )
    return {
        "schema_version": 1,
        "scenario": "campaign-level-thesis-evaluation",
        "verdict": "PASS",
        "kauri_revision": planning_revision,
        "evaluation_class": EVALUATION_CLASS,
        "supported_claim": (
            "under the frozen model and cost budget, exact two-epoch "
            "planning has no losses and a reproducible strict-win subset; "
            "separate bounded live repetitions confirm that the required "
            "passive fault observations and commit continuity are observable"
        ),
        "observations": {
            "model_policy": {
                "synthetic_scenarios": 1_000,
                "solver_reference_agreements": 1_000,
                "canonical_greedy_wins": int(canonical["wins"]),
                "tie_robust_wins": int(planning_summary["tie_robust_lookahead_wins"]),
                "lookahead_losses": 0,
                "cost_regressions": 0,
            },
            "live_observation_calibration": {
                "scheduled_no_retry_slots": 15,
                "validated_slots": sum(
                    int(value)
                    for value in _mapping(
                        live["pass_counts"],
                        "live pass counts",
                    ).values()
                ),
                "replacement_slots": 0,
                "passes_per_arm": {
                    arm: int(live["pass_counts"][arm]) for arm in _ARM_NAMES
                },
                "post_fault_common_commits": sum(
                    int(live["post_fault_commit_continuity"][arm]["witnessed"])
                    for arm in _ARM_NAMES
                ),
                "false_report_settlements": int(
                    live["diagnostic_outcomes"]["static_authenticated_false_report"][
                        "settled_false_reporter"
                    ]
                ),
                "omission_settlements": int(
                    live["diagnostic_outcomes"]["static_persistent_omission"][
                        "settled_persistent_omitter"
                    ]
                ),
                "missing_slots": 0,
                "failed_slots": 0,
            },
        },
        "exclusions": list(EXCLUSIONS),
        "source_canonical_sha256": {
            "planning_breadth_campaign": _sha256(
                canonical_planning_breadth_json(planning)
            ),
            "n7_fault_repetition_campaign": _sha256(
                canonical_n7_fault_repetition_json(live)
            ),
        },
        "source_artifacts": {
            "planning_breadth_campaign": planning,
            "n7_fault_repetition_campaign": live,
        },
    }


def validate_campaign_thesis_evaluation(
    campaign: Mapping[str, Any],
    *,
    live_evidence_root: Path,
) -> dict[str, Any]:
    """Reconstruct embedded sources and re-read the bound live evidence."""

    document = _mapping(campaign, "campaign thesis evaluation")
    sources = _mapping(document.get("source_artifacts"), "source artifacts")
    planning = _mapping(
        sources.get("planning_breadth_campaign"),
        "planning source artifact",
    )
    live = _mapping(
        sources.get("n7_fault_repetition_campaign"),
        "live source artifact",
    )
    rebuilt = build_campaign_thesis_evaluation(
        planning,
        live,
        live_evidence_root=live_evidence_root,
    )
    if _canonical_json(document, "campaign thesis evaluation") != (
        _canonical_json(rebuilt, "rebuilt campaign thesis evaluation")
    ):
        raise CampaignThesisEvaluationError(
            "campaign thesis evaluation does not match its embedded sources"
        )
    return deepcopy(rebuilt)


def canonical_campaign_thesis_evaluation_json(
    campaign: Mapping[str, Any],
    *,
    live_evidence_root: Path,
) -> str:
    """Re-read bound live evidence, then serialize deterministically."""

    validated = validate_campaign_thesis_evaluation(
        campaign,
        live_evidence_root=live_evidence_root,
    )
    return _canonical_json(validated, "campaign thesis evaluation")


__all__ = (
    "CampaignThesisEvaluationError",
    "EVALUATION_CLASS",
    "EXCLUSIONS",
    "build_campaign_thesis_evaluation",
    "canonical_campaign_thesis_evaluation_json",
    "validate_campaign_thesis_evaluation",
)
