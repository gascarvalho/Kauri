"""Fail-closed composition of model and live thesis evidence.

The composed artifact deliberately keeps two evidence layers separate:

* a synthetic, exhaustive finite-horizon topology game; and
* three bounded live Kauri fault demonstrations.

Combining them does not turn the model selector into live protocol behavior.
The strict checks here exist to prevent a graph from silently making that
promotion or from mixing revisions, fault plans, incomplete runs, or edited
summary values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
import json
import re
from typing import Any

from .comparison import (
    ComparisonError,
    build_n7_comparison,
    summarize_comparison,
)
from .robust_topology_evaluation import evaluate_robust_topology


_REVISION = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")

ARM_NAMES = (
    "sigkill_crash",
    "static_authenticated_false_report",
    "static_persistent_omission",
)

THESIS_COMPARISON_SEED = 41_719
THESIS_DIAGNOSTIC_WINDOW = "n7-epoch0-tree6-tree0-static-v1"

SUPPORTED_CLAIM = (
    "finite-horizon planning reduces terminal ambiguity by 25% under the "
    "declared model with the same cost constraints"
)

CLAIMS_NOT_MADE = (
    "no general Byzantine identification",
    "no live planner activation",
    "no throughput speedup",
    "single live development run per fault arm; no statistical inference",
    "structural exposure is not a consensus-safety or liveness proof",
)


class ThesisEvaluationError(ValueError):
    """The supplied evidence cannot support the bounded thesis claim."""


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ThesisEvaluationError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ThesisEvaluationError(
        f"non-finite JSON number is not canonical: {value}"
    )


def parse_thesis_json_object(source: str, label: str) -> dict[str, Any]:
    """Parse one canonical JSON object without duplicate or non-finite values."""

    try:
        value = json.loads(
            source,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as error:
        raise ThesisEvaluationError(f"cannot parse {label}: {error}") from error
    if not isinstance(value, dict):
        raise ThesisEvaluationError(f"{label} must contain an object")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ThesisEvaluationError(f"{label} must be an object")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ThesisEvaluationError(f"{label} must be an integer")
    return value


def _positive_integer(value: object, label: str) -> int:
    number = _integer(value, label)
    if number <= 0:
        raise ThesisEvaluationError(f"{label} must be positive")
    return number


def _revision(value: object, label: str) -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise ThesisEvaluationError(
            f"{label} revision must be 40 lowercase hexadecimal characters"
        )
    return value


def _separation(model: Mapping[str, Any]) -> Mapping[str, Any]:
    game = _mapping(
        model.get("passive_reconfiguration_game"),
        "passive reconfiguration game",
    )
    return _mapping(
        game.get("two_epoch_greedy_separation"),
        "two-epoch greedy separation",
    )


def _selected_cost(
    separation: Mapping[str, Any],
    field: str,
    greedy_id: str,
    lookahead_id: str,
) -> int:
    costs = _mapping(separation.get(field), field.replace("_", " "))
    greedy = _integer(costs.get(greedy_id), f"greedy {field}")
    lookahead = _integer(costs.get(lookahead_id), f"lookahead {field}")
    if greedy < 0 or greedy != lookahead:
        raise ThesisEvaluationError(
            f"model cost regression: selected {field} values differ or are negative"
        )
    return greedy


def _validate_model(model: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    if _integer(model.get("schema_version"), "model schema version") != 2:
        raise ThesisEvaluationError("model evidence must use schema version 2")
    revision = _revision(model.get("kauri_revision"), "model evidence")
    if model.get("revision_verification") != "verified_current_clean_head":
        raise ThesisEvaluationError(
            "model evidence is not bound to a verified clean revision"
        )

    canonical = evaluate_robust_topology(
        revision,
        revision_verification="verified_current_clean_head",
    )
    try:
        supplied_json = json.dumps(
            dict(model),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        canonical_json = json.dumps(
            canonical,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ThesisEvaluationError("model evidence is not canonical JSON") from error
    if supplied_json != canonical_json:
        raise ThesisEvaluationError(
            "model evidence does not exactly match the canonical exhaustive proof"
        )

    separation = _separation(model)
    greedy = _mapping(separation.get("greedy_policy"), "greedy policy")
    lookahead = _mapping(separation.get("lookahead_policy"), "lookahead policy")
    greedy_id = greedy.get("first_candidate_id")
    lookahead_id = lookahead.get("first_candidate_id")
    if not isinstance(greedy_id, str) or not isinstance(lookahead_id, str):
        raise ThesisEvaluationError("model policies must identify their first tree")

    initial = _integer(
        separation.get("compatible_singleton_fault_hypotheses"),
        "initial compatible hypotheses",
    )
    greedy_immediate = _integer(
        greedy.get("immediate_worst_surviving_hypotheses"),
        "greedy immediate ambiguity",
    )
    greedy_terminal = _integer(
        greedy.get("two_epoch_worst_surviving_hypotheses"),
        "greedy terminal ambiguity",
    )
    lookahead_immediate = _integer(
        lookahead.get("immediate_worst_surviving_hypotheses"),
        "lookahead immediate ambiguity",
    )
    lookahead_terminal = _integer(
        lookahead.get("two_epoch_worst_surviving_hypotheses"),
        "lookahead terminal ambiguity",
    )
    if (
        initial,
        greedy_immediate,
        greedy_terminal,
        lookahead_immediate,
        lookahead_terminal,
    ) != (9, 5, 4, 6, 3):
        raise ThesisEvaluationError(
            "model does not contain the declared 9 / 5-to-4 / 6-to-3 witness"
        )

    reduction_ppm = _integer(
        separation.get("lookahead_reduction_ppm"),
        "lookahead reduction",
    )
    recomputed_reduction = (
        (greedy_terminal - lookahead_terminal) * 1_000_000
        // greedy_terminal
    )
    if reduction_ppm != recomputed_reduction or reduction_ppm != 250_000:
        raise ThesisEvaluationError("terminal ambiguity reduction is not 25%")

    budget = _integer(
        separation.get("reconfiguration_budget_epochs"),
        "reconfiguration budget",
    )
    extra_reconfigurations = _integer(
        separation.get("extra_reconfigurations_vs_greedy"),
        "extra reconfigurations",
    )
    additional_messages = _integer(
        separation.get("additional_diagnostic_messages"),
        "additional diagnostic messages",
    )
    if (budget, extra_reconfigurations, additional_messages) != (2, 0, 0):
        raise ThesisEvaluationError("model does not preserve the declared cost budget")

    exposure = _selected_cost(
        separation,
        "worst_case_exposure_by_candidate",
        greedy_id,
        lookahead_id,
    )
    predicted_latency_us = _selected_cost(
        separation,
        "predicted_latency_us_by_candidate",
        greedy_id,
        lookahead_id,
    )
    if (exposure, predicted_latency_us) != (1, 10):
        raise ThesisEvaluationError("model selected-cost witness is not canonical")

    return revision, {
        "ambiguity_paths": {
            "greedy": [initial, greedy_immediate, greedy_terminal],
            "lookahead": [initial, lookahead_immediate, lookahead_terminal],
        },
        "terminal_ambiguity_reduction_ppm": reduction_ppm,
        "reconfiguration_budget_epochs": budget,
        "additional_diagnostic_messages": additional_messages,
        "extra_reconfigurations_vs_greedy": extra_reconfigurations,
        "selected_worst_case_exposure": exposure,
        "selected_predicted_latency_us": predicted_latency_us,
        "replica_count": _integer(separation.get("replica_count"), "replica count"),
        "compatible_singleton_fault_hypotheses": initial,
    }


def _validate_commit(
    value: object,
    label: str,
    *,
    expected_participants: list[int],
) -> tuple[Mapping[str, Any], int]:
    commit = _mapping(value, label)
    height = _integer(commit.get("block_height"), f"{label} height")
    if height < 0:
        raise ThesisEvaluationError(f"{label} height must be non-negative")
    block_hash = commit.get("block_hash")
    if not isinstance(block_hash, str) or _HASH.fullmatch(block_hash) is None:
        raise ThesisEvaluationError(f"{label} has an invalid block hash")
    participants = commit.get("participants")
    if (
        not isinstance(participants, list)
        or any(
            isinstance(participant, bool) or not isinstance(participant, int)
            for participant in participants
        )
        or participants != expected_participants
    ):
        raise ThesisEvaluationError(f"{label} has the wrong exact participants")
    timestamp = _positive_integer(
        commit.get("common_monotonic_raw_ns"),
        f"{label} timestamp",
    )
    return commit, timestamp


def _validate_fault_binding(arm: Mapping[str, Any], name: str) -> int:
    if _integer(arm.get("schema_version"), f"{name} schema version") != 2:
        raise ThesisEvaluationError(f"{name} must use arm schema version 2")
    if arm.get("scenario") != "n7-static-fault-comparison":
        raise ThesisEvaluationError(f"{name} belongs to the wrong scenario")
    if arm.get("interrupted") is not False:
        raise ThesisEvaluationError(f"{name} was interrupted")
    if arm.get("runtime_error") is not None:
        raise ThesisEvaluationError(f"{name} has a runtime error")

    context = _mapping(
        arm.get("fixed_context_observation"),
        f"{name} fixed context",
    )
    if set(context) != {
        "active_configuration_records",
        "configured_fault_threshold",
        "configured_quorum",
        "configured_replica_count",
        "invalid_records",
    }:
        raise ThesisEvaluationError(f"{name} has an incomplete fixed context")
    if (
        _positive_integer(
            context.get("active_configuration_records"),
            f"{name} active configuration records",
        )
        <= 0
        or _integer(
            context.get("configured_fault_threshold"),
            f"{name} configured fault threshold",
        )
        != 2
        or _integer(
            context.get("configured_quorum"),
            f"{name} configured quorum",
        )
        != 5
        or _integer(
            context.get("configured_replica_count"),
            f"{name} configured replica count",
        )
        != 7
        or context.get("invalid_records") != []
    ):
        raise ThesisEvaluationError(f"{name} did not prove fixed N=7, f=2, Q=5")

    plan_sha256 = arm.get("fault_plan_sha256")
    if not isinstance(plan_sha256, str) or _HASH.fullmatch(plan_sha256) is None:
        raise ThesisEvaluationError(f"{name} has an invalid fault-plan hash")
    action = _mapping(arm.get("action_observation"), f"{name} fault action")
    evidence = _mapping(
        action.get("fault_evidence"),
        f"{name} fault evidence",
    )
    if (
        evidence.get("terminal_status") != "succeeded"
        or evidence.get("plan_sha256") != plan_sha256
        or evidence.get("plan_path") != "fault-plan.json"
        or evidence.get("journal_path") != "raw/fault-orchestrator.jsonl"
        or _positive_integer(
            evidence.get("journal_event_count"),
            f"{name} fault journal event count",
        )
        < 2
    ):
        raise ThesisEvaluationError(f"{name} is not bound to successful raw fault evidence")

    if name == "sigkill_crash":
        if (
            action.get("kind") != "replica_group_sigkill"
            or _integer(action.get("replica_id"), "crash replica id") != 1
            or action.get("signal") != "SIGKILL"
            or _integer(action.get("returncode"), "crash return code") != -9
        ):
            raise ThesisEvaluationError("crash arm did not prove SIGKILL of replica 1")
        requested = _positive_integer(
            action.get("requested_monotonic_raw_ns"),
            "crash request timestamp",
        )
        confirmed = _positive_integer(
            action.get("confirmed_monotonic_raw_ns"),
            "crash confirmation timestamp",
        )
        if confirmed < requested:
            raise ThesisEvaluationError("crash confirmation precedes its request")
        return confirmed

    observed = _positive_integer(
        action.get("observed_monotonic_raw_ns"),
        f"{name} observation timestamp",
    )
    manager_accepted = _positive_integer(
        action.get("manager_acceptance_observed_monotonic_raw_ns"),
        f"{name} manager-acceptance timestamp",
    )
    if manager_accepted < observed:
        raise ThesisEvaluationError(f"{name} manager acceptance precedes the action")
    return manager_accepted


def _validate_settlement(
    arm: str,
    certificate_value: object,
) -> tuple[dict[str, Any], list[str]]:
    certificate = _mapping(certificate_value, f"{arm} diagnostic certificate")
    if certificate.get("status") != "settled":
        raise ThesisEvaluationError(f"{arm} diagnostic certificate is not settled")
    if _integer(
        certificate.get("compatible_hypothesis_count"),
        f"{arm} compatible hypothesis count",
    ) != 1:
        raise ThesisEvaluationError(f"{arm} did not settle one hypothesis")
    settled = _mapping(
        certificate.get("settled_hypothesis"),
        f"{arm} settled hypothesis",
    )
    expected_hypothesis = (
        {"false_reporters": [6], "persistent_omitters": []}
        if arm == "static_authenticated_false_report"
        else {"false_reporters": [], "persistent_omitters": [1]}
    )
    if dict(settled) != expected_hypothesis:
        raise ThesisEvaluationError(f"{arm} settled the wrong fault hypothesis")

    observations = certificate.get("observations")
    if not isinstance(observations, list) or len(observations) != 2:
        raise ThesisEvaluationError(f"{arm} has no two-observation certificate")
    outcomes: list[str] = []
    for index, observation_value in enumerate(observations):
        observation = _mapping(
            observation_value,
            f"{arm} diagnostic observation {index}",
        )
        outcome = observation.get("outcome")
        if not isinstance(outcome, str):
            raise ThesisEvaluationError(f"{arm} has an invalid diagnostic outcome")
        outcomes.append(outcome)
    expected_outcomes = (
        ["timeout", "response"]
        if arm == "static_authenticated_false_report"
        else ["timeout", "timeout"]
    )
    if outcomes != expected_outcomes:
        raise ThesisEvaluationError(f"{arm} has the wrong diagnostic outcomes")
    return expected_hypothesis, outcomes


def _validate_arms(
    arm_verdicts: Iterable[Mapping[str, Any]],
    revision: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    if isinstance(arm_verdicts, (str, bytes, Mapping)):
        raise ThesisEvaluationError("arm verdicts must be an iterable of objects")
    try:
        supplied_arms = list(arm_verdicts)
    except TypeError as error:
        raise ThesisEvaluationError("arm verdicts must be iterable") from error
    if any(not isinstance(arm, Mapping) for arm in supplied_arms):
        raise ThesisEvaluationError("each arm verdict must be an object")

    names = [arm.get("arm") for arm in supplied_arms]
    if any(not isinstance(name, str) for name in names):
        raise ThesisEvaluationError("each arm verdict must have a string arm name")
    if len(supplied_arms) != len(ARM_NAMES) or set(names) != set(ARM_NAMES):
        raise ThesisEvaluationError(
            "arm verdicts must contain each required fault arm exactly once"
        )
    if len(set(names)) != len(names):
        raise ThesisEvaluationError("arm verdicts contain a duplicate arm")

    by_name = {str(arm["arm"]): arm for arm in supplied_arms}
    comparison = build_n7_comparison(
        kauri_revision=revision,
        seed=THESIS_COMPARISON_SEED,
        crash_replica_id=1,
        false_reporter_id=6,
        false_report_target_id=1,
        persistent_omitter_id=1,
        diagnostic_window=THESIS_DIAGNOSTIC_WINDOW,
    )
    try:
        comparison_summary = summarize_comparison(comparison, by_name)
    except ComparisonError as error:
        raise ThesisEvaluationError(
            f"live comparison contract failed: {error}"
        ) from error

    comparison_arms = {
        str(arm["name"]): arm
        for arm in comparison_summary["arms"]
        if isinstance(arm, Mapping)
    }
    all_participants = list(range(7))
    summaries: dict[str, Any] = {}
    canonical_arms: list[dict[str, Any]] = []
    for name in ARM_NAMES:
        arm = by_name[name]
        if arm.get("verdict") != "PASS":
            raise ThesisEvaluationError(f"{name} arm must have a PASS verdict")
        if _revision(arm.get("kauri_revision"), name) != revision:
            raise ThesisEvaluationError("model and live arm revisions differ")
        if arm.get("conflicting_commits") != []:
            raise ThesisEvaluationError(f"{name} arm observed conflicting commits")

        fault_timestamp = _validate_fault_binding(arm, name)
        after_participants = (
            [0, 2, 3, 4, 5, 6]
            if name == "sigkill_crash"
            else all_participants
        )
        before, before_timestamp = _validate_commit(
            arm.get("common_commit_before"),
            f"{name} before",
            expected_participants=all_participants,
        )
        after, after_timestamp = _validate_commit(
            arm.get("common_commit_after"),
            f"{name} after",
            expected_participants=after_participants,
        )
        before_height = int(before["block_height"])
        after_height = int(after["block_height"])
        if after_height <= before_height:
            raise ThesisEvaluationError(f"{name} did not advance common commits")
        if before.get("block_hash") == after.get("block_hash"):
            raise ThesisEvaluationError(f"{name} reused the same commit hash")
        if not (before_timestamp < fault_timestamp < after_timestamp):
            raise ThesisEvaluationError(
                f"{name} does not bracket fault evidence with common commits"
            )

        strict_arm = _mapping(
            comparison_arms.get(name),
            f"{name} comparison summary",
        )
        summary: dict[str, Any] = {
            "run_id": strict_arm["run_id"],
            "fault_plan_sha256": strict_arm["fault_plan_sha256"],
            "common_commit_heights": [before_height, after_height],
            "additional_common_commit_heights": after_height - before_height,
            "post_fault_common_commit": True,
            "conflicting_commits": 0,
        }
        if name != "sigkill_crash":
            hypothesis, outcomes = _validate_settlement(
                name,
                arm.get("diagnostic_certificate"),
            )
            summary["settled_hypothesis"] = hypothesis
            summary["observation_outcomes"] = outcomes
            summary["compatible_hypotheses_by_observation"] = [2, 1]
        summaries[name] = summary
        canonical_arms.append(deepcopy(dict(arm)))

    return canonical_arms, summaries, deepcopy(comparison_summary)


def build_thesis_evaluation(
    model_evidence: Mapping[str, Any],
    arm_verdicts: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate and compose one bounded, revision-consistent evidence bundle."""

    model = _mapping(model_evidence, "model evidence")
    revision, model_metrics = _validate_model(model)
    arms, live_metrics, comparison_summary = _validate_arms(
        arm_verdicts,
        revision,
    )
    return {
        "schema_version": 2,
        "scenario": "joint-hypothesis-thesis-evaluation",
        "verdict": "PASS",
        "kauri_revision": revision,
        "supported_claim": SUPPORTED_CLAIM,
        "claims_not_made": list(CLAIMS_NOT_MADE),
        "model_evidence": deepcopy(dict(model)),
        "live_evidence": {
            "comparison_summary": comparison_summary,
            "arm_verdicts": arms,
        },
        "metrics": {
            "model": model_metrics,
            "live": live_metrics,
        },
    }


def canonical_thesis_evaluation_json(evaluation: Mapping[str, Any]) -> str:
    """Return deterministic, human-readable JSON."""

    if not isinstance(evaluation, Mapping):
        raise ThesisEvaluationError("thesis evaluation must be an object")
    try:
        return json.dumps(
            evaluation,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ThesisEvaluationError(
            "thesis evaluation is not canonical JSON"
        ) from error


__all__ = (
    "ARM_NAMES",
    "CLAIMS_NOT_MADE",
    "SUPPORTED_CLAIM",
    "THESIS_COMPARISON_SEED",
    "THESIS_DIAGNOSTIC_WINDOW",
    "ThesisEvaluationError",
    "build_thesis_evaluation",
    "canonical_thesis_evaluation_json",
    "parse_thesis_json_object",
)
