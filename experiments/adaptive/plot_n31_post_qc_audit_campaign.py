#!/usr/bin/env python3
"""Render validator-gated figures for the sealed N=31 PQAR campaign."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Iterator

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.n31_post_qc_audit_campaign import (  # noqa: E402
    ARM_NAMES,
    ATTEMPTS_PER_ARM,
    CAMPAIGN_SCENARIO,
    CLASSIFICATION_NAMES,
    N31PostQcAuditCampaignError,
    SCHEDULED_ATTEMPTS,
    TIMING_METRICS_NS,
    canonical_json_bytes,
    clopper_pearson_interval,
    nanosecond_metric_summary,
    semantic_document_sha256,
    validate_n31_pqar_campaign,
)
from experiments.adaptive.kauri_experiment.n31_static_diagnosis_runtime import (  # noqa: E402
    TrustedProvenance,
    load_trusted_provenance,
)
from experiments.adaptive.kauri_experiment.profiled_fault_archive import (  # noqa: E402
    EvidenceSealError,
    EvidenceSealMetadata,
    verify_evidence_seal,
)

EXPECTED_CLASSIFICATION = {
    "static_authenticated_false_report": "false_reporter",
    "static_persistent_direct_vote_omission": "omission_compatible",
    "static_authenticated_sham": "sham",
}
ARM_LABELS = {
    "static_authenticated_false_report": "False report",
    "static_persistent_direct_vote_omission": "Direct-vote\nomission",
    "static_authenticated_sham": "Sham",
}
ARM_SHORT_LABELS = {
    "static_authenticated_false_report": "F",
    "static_persistent_direct_vote_omission": "O",
    "static_authenticated_sham": "S",
}
CLASSIFICATION_LABELS = {
    "false_reporter": "False\nreporter",
    "omission_compatible": "Omission-\ncompatible",
    "sham": "Sham",
    "unclassified": "Unclassified",
}
METRIC_LABELS = {
    "qc_to_deadline_slack_ns": "QC-to-deadline slack",
    "target_to_deadline_slack_ns": "Target-to-deadline slack",
    "relay_to_root_latency_ns": "Relay-to-root latency",
    "root_verification_latency_ns": "Root-verification latency",
    "qc_to_audit_latency_ns": "QC-to-audit latency",
    "expiry_to_later_commit_latency_ns": "Expiry-to-Q21 observation latency",
}
ARM_COLORS = ("#2563eb", "#dc2626", "#0f766e")
ARM_MARKERS = ("o", "s", "^")
FIGURE_STEMS = (
    "n31-pqar-confusion-matrix",
    "n31-pqar-rates-outcomes",
    "n31-pqar-timings",
)
CAVEATS = (
    "Omission-compatible means the observation is consistent with omission; "
    "it is not detection of a persistent omitter.",
    "The figures do not establish actor culpability or identify arbitrary "
    "Byzantine behavior.",
    "Timing values are same-host descriptive CLOCK_MONOTONIC_RAW-derived "
    "measurements, with missing values retained and no imputation.",
    "The campaign is not throughput evidence and is not a consensus-safety proof.",
)


class PlotError(ValueError):
    """The supplied campaign cannot safely produce figures."""


@dataclass(frozen=True, slots=True)
class RatePoint:
    """One fixed-denominator rate and its exact interval."""

    successes: int
    trials: int
    estimate: float
    lower: float
    upper: float


@dataclass(frozen=True, slots=True)
class TimingSeries:
    """Available raw points and the exact median interval for one arm/metric."""

    ordinals: tuple[int, ...]
    values_ns: tuple[int, ...]
    scheduled_count: int
    missing_count: int
    median_ns: int | float | None
    median_lower_ns: int | None
    median_upper_ns: int | None


@dataclass(frozen=True, slots=True)
class CampaignPlotData:
    """Narrow, checked view of the validator's campaign summary."""

    summary: dict[str, Any]
    campaign_profile: dict[str, str]
    confusion: dict[str, dict[str, int]]
    rates: dict[str, dict[str, RatePoint]]
    outcome_counts_by_arm: dict[str, dict[str, int]]
    timings: dict[str, dict[str, TimingSeries]]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlotError(f"{label} must be an object")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PlotError(f"{label} must be a list")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise PlotError(f"{label} must be an integer at least {minimum}")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlotError(f"{label} must be a non-empty string")
    return value


def _sha256(value: object, label: str) -> str:
    text = _string(value, label)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise PlotError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _revision(value: object) -> str:
    text = _string(value, "Kauri revision")
    if len(text) != 40 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise PlotError("Kauri revision must be a full lowercase Git revision")
    return text


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise PlotError(f"{label} must be a finite number")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise PlotError(f"{label} must be a finite number") from error
    if not math.isfinite(number):
        raise PlotError(f"{label} must be a finite number")
    return number


def _integer_or_half(value: object, label: str) -> int | float:
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value) and (value * 2.0).is_integer():
        return value
    raise PlotError(f"{label} must be integer or exact half-integer nanoseconds")


def _reject_nonfinite_numbers(value: object, label: str = "campaign summary") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise PlotError(f"{label} contains a non-finite number")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_nonfinite_numbers(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nonfinite_numbers(child, f"{label}[{index}]")


def _exact_keys(value: Mapping[str, Any], expected: Sequence[str], label: str) -> None:
    supplied = set(value)
    required = set(expected)
    if supplied != required:
        raise PlotError(
            f"{label} fields drifted; missing={sorted(required - supplied)!r}; "
            f"extra={sorted(supplied - required)!r}"
        )


def _classification(observation: Mapping[str, Any], *, ordinal: int) -> str:
    supplied = [
        observation[key]
        for key in (
            "classification",
            "predicted_classification",
            "source_blind_classification",
        )
        if key in observation
    ]
    if (
        not supplied
        or any(value != supplied[0] for value in supplied[1:])
        or supplied[0] not in CLASSIFICATION_NAMES
    ):
        raise PlotError(
            f"blind observation {ordinal} lacks one valid consistent classification"
        )
    return str(supplied[0])


def _metric_value(
    observation: Mapping[str, Any] | None,
    metric: str,
    *,
    ordinal: int,
) -> int | None:
    if observation is None:
        return None
    containers: list[tuple[str, Mapping[str, Any]]] = [("observation", observation)]
    for container_name in ("metrics_ns", "quantitative_audit", "timing"):
        candidate = observation.get(container_name)
        if candidate is not None and not isinstance(candidate, Mapping):
            raise PlotError(
                f"blind observation {ordinal} {container_name} must be an object"
            )
        if isinstance(candidate, Mapping):
            containers.append((container_name, candidate))
    supplied: list[int] = []
    for container_name, container in containers:
        if metric not in container or container[metric] is None:
            continue
        value = container[metric]
        if type(value) is not int:
            raise PlotError(
                f"blind observation {ordinal} timing field {container_name}.{metric} "
                "must be integer nanoseconds"
            )
        supplied.append(value)
    if len(set(supplied)) > 1:
        raise PlotError(
            f"blind observation {ordinal} has conflicting values for timing {metric}"
        )
    return supplied[0] if supplied else None


def _rate_point(
    value: object,
    *,
    label: str,
    expected_successes: int,
) -> RatePoint:
    rate = _mapping(value, label)
    required_fields = (
        "successes",
        "trials",
        "estimate",
        "confidence_level",
        "method",
        "lower",
        "upper",
    )
    for field in required_fields:
        if field not in rate:
            raise PlotError(f"{label} is missing required field {field}")
    successes = _integer(rate["successes"], f"{label} successes")
    trials = _integer(rate["trials"], f"{label} trials", minimum=1)
    if successes != expected_successes or trials != ATTEMPTS_PER_ARM:
        raise PlotError(f"{label} fixed denominator or success count drifted")
    if (
        rate["confidence_level"] != "0.95"
        or rate["method"] != "two-sided-clopper-pearson-exact"
    ):
        raise PlotError(f"{label} must use the exact 95% Clopper-Pearson interval")
    estimate = _finite_number(rate["estimate"], f"{label} estimate")
    lower = _finite_number(rate["lower"], f"{label} lower interval")
    upper = _finite_number(rate["upper"], f"{label} upper interval")
    exact_lower, exact_upper = clopper_pearson_interval(successes, trials)
    if (
        not math.isclose(estimate, successes / trials, rel_tol=0.0, abs_tol=5e-10)
        or not math.isclose(lower, exact_lower, rel_tol=0.0, abs_tol=5e-10)
        or not math.isclose(upper, exact_upper, rel_tol=0.0, abs_tol=5e-10)
        or not 0.0 <= lower <= estimate <= upper <= 1.0
    ):
        raise PlotError(f"{label} exact Clopper-Pearson interval drifted")
    return RatePoint(
        successes=successes,
        trials=trials,
        estimate=successes / trials,
        lower=exact_lower,
        upper=exact_upper,
    )


def _validate_timing_summary(
    value: object,
    *,
    label: str,
    ordinals: Sequence[int],
    values: Sequence[int],
) -> TimingSeries:
    supplied = _mapping(value, label)
    expected = nanosecond_metric_summary(values, scheduled_trials=ATTEMPTS_PER_ARM)
    for key, expected_value in expected.items():
        if key not in supplied:
            raise PlotError(f"{label} is missing required timing field {key}")
        if supplied[key] != expected_value:
            raise PlotError(
                f"{label} differs from raw ordinal timing values at {key}; "
                "hidden dropping is forbidden"
            )
    interval = _mapping(expected["median_interval"], f"{label} median interval")
    for field in ("minimum_ns", "maximum_ns"):
        endpoint = expected[field]
        if endpoint is not None and type(endpoint) is not int:
            raise PlotError(f"{label} {field} must be integer nanoseconds")
    for field in ("q1_ns", "median_ns", "q3_ns"):
        quantile = expected[field]
        if quantile is not None:
            _integer_or_half(quantile, f"{label} {field}")
    median = expected["median_ns"]
    lower = interval.get("lower_ns")
    upper = interval.get("upper_ns")
    if lower is not None and type(lower) is not int:
        raise PlotError(f"{label} median lower bound must be integer nanoseconds")
    if upper is not None and type(upper) is not int:
        raise PlotError(f"{label} median upper bound must be integer nanoseconds")
    return TimingSeries(
        ordinals=tuple(ordinals),
        values_ns=tuple(values),
        scheduled_count=ATTEMPTS_PER_ARM,
        missing_count=ATTEMPTS_PER_ARM - len(values),
        median_ns=median,
        median_lower_ns=lower,
        median_upper_ns=upper,
    )


def _adapt_validated_summary(value: object) -> CampaignPlotData:
    """Validate only the semantics consumed by the figures and manifest."""

    summary_mapping = _mapping(value, "validated campaign summary")
    summary = deepcopy(dict(summary_mapping))
    _reject_nonfinite_numbers(summary)
    if summary.get("campaign_acceptance") != "ACCEPTED":
        raise PlotError("only campaign_acceptance == ACCEPTED may produce figures")
    if summary.get("figure_eligible") is not True:
        raise PlotError("only a figure-eligible accepted campaign may produce figures")
    if (
        summary.get("schema_version") != 1
        or summary.get("scenario") != CAMPAIGN_SCENARIO
    ):
        raise PlotError(
            "validated campaign schema or scenario is not the N=31 campaign"
        )
    if summary.get("outcome_status") not in {"ALL_QUALIFIED", "MIXED"}:
        raise PlotError("validated campaign outcome status is unsupported")
    failures = _list(summary.get("integrity_failures"), "integrity failures")
    if failures:
        raise PlotError("an accepted campaign cannot retain integrity failures")
    controller_failures = _list(
        summary.get("controller_failures"), "controller failures"
    )
    if controller_failures:
        raise PlotError("an accepted campaign cannot retain controller failures")
    if (
        summary.get("scheduled_attempts") != SCHEDULED_ATTEMPTS
        or summary.get("fixed_denominator_per_arm") != ATTEMPTS_PER_ARM
    ):
        raise PlotError("campaign fixed denominator drifted from 30 attempts per arm")
    if summary.get("returned_invocations") != SCHEDULED_ATTEMPTS:
        raise PlotError("accepted campaign must have all 90 invocations returned")

    profile_value = _mapping(summary.get("campaign_profile"), "campaign profile")
    profile_id = _string(profile_value.get("profile_id"), "campaign profile id")
    profile_sha256 = _sha256(profile_value.get("sha256"), "campaign profile digest")
    campaign_profile = {"profile_id": profile_id, "sha256": profile_sha256}
    _revision(summary.get("kauri_revision"))
    for field, label in (
        ("campaign_plan_sha256", "campaign plan digest"),
        ("trusted_provenance_sha256", "trusted provenance digest"),
        ("pilot_gate_sha256", "pilot gate digest"),
    ):
        _sha256(summary.get(field), label)

    execution_values = _list(summary.get("execution_records"), "execution records")
    if len(execution_values) != SCHEDULED_ATTEMPTS:
        raise PlotError("execution records must preserve all 90 scheduled attempts")
    records: dict[int, Mapping[str, Any]] = {}
    arm_counts: Counter[str] = Counter()
    outcomes_by_arm: dict[str, Counter[str]] = {
        arm: Counter({"PASS": 0, "FAIL": 0, "INCOMPLETE": 0}) for arm in ARM_NAMES
    }
    for index, record_value in enumerate(execution_values, start=1):
        record = _mapping(record_value, f"execution record {index}")
        ordinal = _integer(
            record.get("ordinal"), f"execution record {index} ordinal", minimum=1
        )
        if ordinal > SCHEDULED_ATTEMPTS or ordinal in records:
            raise PlotError("execution record ordinals must be unique in [1, 90]")
        arm = record.get("arm")
        if arm not in ARM_NAMES:
            raise PlotError(f"execution record {ordinal} has an unknown arm")
        if record.get("launch_status") != "returned":
            raise PlotError(
                "accepted campaign contains an interrupted or not-invoked slot"
            )
        verdict = record.get("original_verdict")
        if verdict not in {"PASS", "FAIL", "INCOMPLETE"}:
            raise PlotError(f"execution record {ordinal} has an invalid verdict")
        records[ordinal] = record
        arm_counts[str(arm)] += 1
        outcomes_by_arm[str(arm)][str(verdict)] += 1
    if set(records) != set(range(1, SCHEDULED_ATTEMPTS + 1)) or any(
        arm_counts[arm] != ATTEMPTS_PER_ARM for arm in ARM_NAMES
    ):
        raise PlotError("execution records drifted from fixed per-arm denominators")

    outcome_value = _mapping(summary.get("outcome_counts"), "outcome counts")
    _exact_keys(
        outcome_value,
        ("PASS", "FAIL", "INCOMPLETE"),
        "outcome counts",
    )
    rebuilt_outcomes = {
        verdict: sum(outcomes_by_arm[arm][verdict] for arm in ARM_NAMES)
        for verdict in ("PASS", "FAIL", "INCOMPLETE")
    }
    supplied_outcomes = {
        verdict: _integer(outcome_value[verdict], f"outcome count {verdict}")
        for verdict in outcome_value
    }
    if supplied_outcomes != rebuilt_outcomes:
        raise PlotError("campaign outcome counts differ from raw execution records")
    invocation_value = _mapping(
        summary.get("invocation_status_counts"), "invocation status counts"
    )
    _exact_keys(
        invocation_value,
        ("returned", "raised", "not_started"),
        "invocation status counts",
    )
    supplied_invocations = {
        status: _integer(invocation_value[status], f"invocation status count {status}")
        for status in invocation_value
    }
    if supplied_invocations != {
        "returned": SCHEDULED_ATTEMPTS,
        "raised": 0,
        "not_started": 0,
    }:
        raise PlotError(
            "accepted campaign invocation counts contain an interrupted suffix"
        )
    expected_status = (
        "ALL_QUALIFIED" if rebuilt_outcomes["PASS"] == SCHEDULED_ATTEMPTS else "MIXED"
    )
    if summary["outcome_status"] != expected_status:
        raise PlotError("campaign outcome status differs from preserved outcomes")

    observation_values = _list(summary.get("blind_observations"), "blind observations")
    if len(observation_values) != SCHEDULED_ATTEMPTS:
        raise PlotError("accepted campaign must preserve 90 blind observations")
    observations: dict[int, Mapping[str, Any] | None] = {}
    for index, observation_record_value in enumerate(observation_values, start=1):
        observation_record = _mapping(
            observation_record_value, f"blind observation record {index}"
        )
        ordinal = _integer(
            observation_record.get("ordinal"),
            f"blind observation record {index} ordinal",
            minimum=1,
        )
        if ordinal not in records or ordinal in observations:
            raise PlotError("blind observation ordinals must match execution records")
        if "observation" not in observation_record:
            raise PlotError(f"blind observation {ordinal} is missing its payload field")
        candidate = observation_record["observation"]
        if not isinstance(candidate, Mapping):
            raise PlotError(f"blind observation {ordinal} payload must be an object")
        observations[ordinal] = candidate
    if set(observations) != set(records):
        raise PlotError("blind observations do not cover all scheduled ordinals")

    rebuilt_confusion: dict[str, Counter[str]] = {
        arm: Counter({classification: 0 for classification in CLASSIFICATION_NAMES})
        for arm in ARM_NAMES
    }
    timing_ordinals: dict[str, dict[str, list[int]]] = {
        arm: {metric: [] for metric in TIMING_METRICS_NS} for arm in ARM_NAMES
    }
    timing_values: dict[str, dict[str, list[int]]] = {
        arm: {metric: [] for metric in TIMING_METRICS_NS} for arm in ARM_NAMES
    }
    for ordinal in sorted(records):
        arm = str(records[ordinal]["arm"])
        observation = observations[ordinal]
        assert observation is not None
        rebuilt_confusion[arm][_classification(observation, ordinal=ordinal)] += 1
        for metric in TIMING_METRICS_NS:
            metric_value = _metric_value(observation, metric, ordinal=ordinal)
            if metric_value is not None:
                timing_ordinals[arm][metric].append(ordinal)
                timing_values[arm][metric].append(metric_value)

    confusion_value = _mapping(summary.get("confusion_table"), "confusion table")
    _exact_keys(confusion_value, ARM_NAMES, "confusion table")
    confusion: dict[str, dict[str, int]] = {}
    for arm in ARM_NAMES:
        row_value = _mapping(confusion_value[arm], f"confusion row {arm}")
        _exact_keys(row_value, CLASSIFICATION_NAMES, f"confusion row {arm}")
        row = {
            classification: _integer(
                row_value[classification],
                f"confusion row {arm} classification {classification}",
            )
            for classification in CLASSIFICATION_NAMES
        }
        if sum(row.values()) != ATTEMPTS_PER_ARM:
            raise PlotError(f"confusion row {arm} denominator is not 30")
        if row != dict(rebuilt_confusion[arm]):
            raise PlotError(f"confusion row {arm} differs from raw blind observations")
        confusion[arm] = row

    rates_value = _mapping(summary.get("rates"), "rates")
    _exact_keys(rates_value, ARM_NAMES, "rates")
    rates: dict[str, dict[str, RatePoint]] = {}
    for arm in ARM_NAMES:
        arm_rates = _mapping(rates_value[arm], f"rates for {arm}")
        rate_names = (
            "classification_correctness",
            "strict_harness_qualification",
        )
        _exact_keys(arm_rates, rate_names, f"rates for {arm}")
        rates[arm] = {
            "classification_correctness": _rate_point(
                arm_rates["classification_correctness"],
                label=f"classification correctness for {arm}",
                expected_successes=confusion[arm][EXPECTED_CLASSIFICATION[arm]],
            ),
            "strict_harness_qualification": _rate_point(
                arm_rates["strict_harness_qualification"],
                label=f"strict harness qualification for {arm}",
                expected_successes=outcomes_by_arm[arm]["PASS"],
            ),
        }

    timing_summary_value = _mapping(
        summary.get("timing_summaries_ns"), "timing summaries"
    )
    _exact_keys(timing_summary_value, ARM_NAMES, "timing summaries")
    timings: dict[str, dict[str, TimingSeries]] = {}
    for arm in ARM_NAMES:
        arm_timings = _mapping(timing_summary_value[arm], f"timing summaries for {arm}")
        _exact_keys(arm_timings, TIMING_METRICS_NS, f"timing summaries for {arm}")
        timings[arm] = {
            metric: _validate_timing_summary(
                arm_timings[metric],
                label=f"timing summary {arm}.{metric}",
                ordinals=timing_ordinals[arm][metric],
                values=timing_values[arm][metric],
            )
            for metric in TIMING_METRICS_NS
        }

    statistical_scope = _string(
        summary.get("statistical_scope"), "statistical scope"
    ).lower()
    claim_boundary = _string(summary.get("claim_boundary"), "claim boundary").lower()
    if "same-host" not in statistical_scope:
        raise PlotError("statistical scope must retain the same-host timing caveat")
    if not all(
        token in claim_boundary
        for token in ("omission-compatible", "throughput", "safety")
    ):
        raise PlotError("claim boundary omits required scientific caveats")

    return CampaignPlotData(
        summary=summary,
        campaign_profile=campaign_profile,
        confusion=confusion,
        rates=rates,
        outcome_counts_by_arm={
            arm: {
                verdict: outcomes_by_arm[arm][verdict]
                for verdict in ("PASS", "FAIL", "INCOMPLETE")
            }
            for arm in ARM_NAMES
        },
        timings=timings,
    )


def _validated_source(
    campaign_directory: Path,
    *,
    trusted_provenance: TrustedProvenance,
) -> tuple[CampaignPlotData, EvidenceSealMetadata]:
    root = campaign_directory.resolve()
    try:
        validated = validate_n31_pqar_campaign(
            root, trusted_provenance=trusted_provenance
        )
    except (
        N31PostQcAuditCampaignError,
        EvidenceSealError,
        OSError,
        ValueError,
    ) as error:
        raise PlotError(f"campaign failed strict validation: {error}") from error
    data = _adapt_validated_summary(validated)
    try:
        seal = verify_evidence_seal(root)
    except (EvidenceSealError, OSError) as error:
        raise PlotError(f"campaign seal failed final verification: {error}") from error
    _sha256(seal.tree_sha256, "campaign evidence-tree digest")
    _sha256(seal.seal_sha256, "campaign evidence-seal digest")
    return data, seal


def _output_path(campaign_directory: Path, output_directory: Path) -> Path:
    campaign = campaign_directory.resolve()
    output = output_directory.resolve()
    try:
        output.relative_to(campaign)
    except ValueError:
        pass
    else:
        raise PlotError("output directory must be outside the sealed campaign root")
    if output.exists() or output.is_symlink():
        raise PlotError("output directory already exists")
    return output


@contextmanager
def _matplotlib_agg(output: Path) -> Iterator[Any]:
    """Import Agg with a writable cache under the already-approved output root."""

    previous_cache = os.environ.get("MPLCONFIGDIR")
    previous_xdg_cache = os.environ.get("XDG_CACHE_HOME")
    with tempfile.TemporaryDirectory(prefix=".matplotlib-cache-", dir=output) as cache:
        os.environ["MPLCONFIGDIR"] = cache
        os.environ["XDG_CACHE_HOME"] = cache
        matplotlib_module: Any | None = None
        original_get_cachedir: Any | None = None
        original_get_configdir: Any | None = None
        try:
            import matplotlib

            matplotlib_module = matplotlib
            matplotlib.use("Agg", force=True)
            uncached_get_cachedir = getattr(
                matplotlib.get_cachedir, "__wrapped__", matplotlib.get_cachedir
            )
            uncached_get_configdir = getattr(
                matplotlib.get_configdir, "__wrapped__", matplotlib.get_configdir
            )
            if (
                Path(uncached_get_cachedir()).resolve() != Path(cache).resolve()
                or Path(uncached_get_configdir()).resolve() != Path(cache).resolve()
            ):
                raise PlotError("matplotlib did not honor the explicit writable cache")
            # Matplotlib memoizes these helpers. Override them during rendering
            # so a second call in one process cannot reuse a HOME-derived path.
            original_get_cachedir = matplotlib.get_cachedir
            original_get_configdir = matplotlib.get_configdir
            matplotlib.get_cachedir = lambda: cache
            matplotlib.get_configdir = lambda: cache
            import matplotlib.pyplot as plt

            yield plt
        except ImportError as error:
            raise PlotError(
                "matplotlib is required to render campaign figures"
            ) from error
        finally:
            if matplotlib_module is not None and original_get_cachedir is not None:
                matplotlib_module.get_cachedir = original_get_cachedir
            if matplotlib_module is not None and original_get_configdir is not None:
                matplotlib_module.get_configdir = original_get_configdir
            if previous_cache is None:
                os.environ.pop("MPLCONFIGDIR", None)
            else:
                os.environ["MPLCONFIGDIR"] = previous_cache
            if previous_xdg_cache is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = previous_xdg_cache


def _save_pair(
    figure: Any,
    output: Path,
    stem: str,
    *,
    subject: str,
) -> tuple[Path, Path]:
    png = output / f"{stem}.png"
    pdf = output / f"{stem}.pdf"
    fixed_date = datetime(2020, 1, 1, tzinfo=timezone.utc)
    figure.savefig(
        png,
        format="png",
        dpi=180,
        metadata={
            "Software": "Kauri thesis evidence renderer",
            "Title": stem,
            "Description": (
                "Omission-compatible same-host descriptive evidence; not "
                "detection, culpability, throughput evidence, or a safety proof."
            ),
        },
    )
    figure.savefig(
        pdf,
        format="pdf",
        metadata={
            "Title": stem,
            "Author": "Kauri thesis evidence renderer",
            "Subject": subject,
            "Keywords": (
                "Kauri, omission-compatible, same-host, no detection claim, "
                "no culpability claim, no throughput claim, no safety proof"
            ),
            "Creator": "Kauri thesis evidence renderer",
            "Producer": "Kauri thesis evidence renderer",
            "CreationDate": fixed_date,
            "ModDate": fixed_date,
        },
    )
    return png, pdf


def _render_confusion(plt: Any, data: CampaignPlotData, output: Path) -> None:
    matrix = [
        [data.confusion[arm][classification] for classification in CLASSIFICATION_NAMES]
        for arm in ARM_NAMES
    ]
    figure, axis = plt.subplots(figsize=(10.8, 6.3))
    figure.subplots_adjust(left=0.22, right=0.91, top=0.82, bottom=0.28)
    image = axis.imshow(matrix, cmap="Blues", vmin=0, vmax=ATTEMPTS_PER_ARM)
    for row, values in enumerate(matrix):
        for column, count in enumerate(values):
            axis.text(
                column,
                row,
                str(count),
                ha="center",
                va="center",
                color="white" if count >= 16 else "#0f172a",
                fontsize=12,
                fontweight="bold",
            )
    axis.set_xticks(
        range(len(CLASSIFICATION_NAMES)),
        [CLASSIFICATION_LABELS[name] for name in CLASSIFICATION_NAMES],
    )
    axis.set_yticks(
        range(len(ARM_NAMES)), [ARM_LABELS[arm].replace("\n", " ") for arm in ARM_NAMES]
    )
    axis.set_xlabel("Source-blind classification")
    axis.set_ylabel("Declared arm joined after blind extraction")
    axis.set_title("N=31 PQAR classification confusion (fixed n=30 per row)")
    colorbar = figure.colorbar(image, ax=axis, fraction=0.046, pad=0.04)
    colorbar.set_label("Scheduled attempts")
    figure.text(
        0.5,
        0.09,
        "Omission-compatible is an observation class—not detection or actor "
        "culpability. Same-host evidence only; no throughput or safety proof.",
        ha="center",
        va="bottom",
        fontsize=9,
        color="#7f1d1d",
    )
    _save_pair(
        figure,
        output,
        FIGURE_STEMS[0],
        subject=(
            "Fixed-denominator N=31 PQAR confusion; omission-compatible and "
            "same-host only, without detection, culpability, throughput, or safety claims"
        ),
    )
    plt.close(figure)


def _render_rates_and_outcomes(plt: Any, data: CampaignPlotData, output: Path) -> None:
    figure, axes = plt.subplots(1, 2, figsize=(13.5, 6.5))
    figure.subplots_adjust(left=0.075, right=0.98, top=0.80, bottom=0.27, wspace=0.28)
    positions = tuple(range(len(ARM_NAMES)))
    rate_specs = (
        ("classification_correctness", "Classification-correct", "#2563eb", -0.13),
        ("strict_harness_qualification", "Strict-qualified", "#7c3aed", 0.13),
    )
    for rate_name, label, color, offset in rate_specs:
        points = [data.rates[arm][rate_name] for arm in ARM_NAMES]
        x_values = [position + offset for position in positions]
        estimates = [point.estimate for point in points]
        axes[0].errorbar(
            x_values,
            estimates,
            yerr=(
                [
                    estimate - point.lower
                    for estimate, point in zip(estimates, points, strict=True)
                ],
                [
                    point.upper - estimate
                    for estimate, point in zip(estimates, points, strict=True)
                ],
            ),
            fmt="o",
            markersize=7,
            capsize=4,
            linewidth=1.6,
            color=color,
            label=label,
        )
        for x_value, point in zip(x_values, points, strict=True):
            axes[0].text(
                x_value,
                max(0.02, point.lower - 0.06),
                f"{point.successes}/30",
                ha="center",
                va="top",
                fontsize=8,
                color=color,
            )
    axes[0].set_xticks(positions, [ARM_LABELS[arm] for arm in ARM_NAMES])
    axes[0].set_ylim(0.0, 1.06)
    axes[0].set_ylabel("Proportion (exact 95% CP interval)")
    axes[0].set_title("Fixed-denominator rates")
    axes[0].legend(loc="lower left", fontsize=8)

    bottoms = [0, 0, 0]
    outcome_specs = (
        ("PASS", "#15803d"),
        ("FAIL", "#dc2626"),
        ("INCOMPLETE", "#94a3b8"),
    )
    for verdict, color in outcome_specs:
        values = [data.outcome_counts_by_arm[arm][verdict] for arm in ARM_NAMES]
        axes[1].bar(
            positions,
            values,
            bottom=bottoms,
            width=0.64,
            color=color,
            label=verdict,
        )
        for position, value, bottom in zip(positions, values, bottoms, strict=True):
            if value:
                axes[1].text(
                    position,
                    bottom + value / 2,
                    str(value),
                    ha="center",
                    va="center",
                    fontsize=9,
                    color="white" if verdict != "INCOMPLETE" else "#0f172a",
                    fontweight="bold",
                )
        bottoms = [
            bottom + value for bottom, value in zip(bottoms, values, strict=True)
        ]
    axes[1].set_xticks(positions, [ARM_LABELS[arm] for arm in ARM_NAMES])
    axes[1].set_ylim(0, 32)
    axes[1].set_yticks(range(0, 31, 5))
    axes[1].set_ylabel("Preserved scheduled attempts")
    axes[1].set_title(
        "Original outcomes retained"
        + (" (MIXED)" if data.summary["outcome_status"] == "MIXED" else "")
    )
    axes[1].legend(loc="upper center", ncol=3, fontsize=8)
    for axis in axes:
        axis.grid(axis="y", color="#cbd5e1", alpha=0.7)
        axis.set_axisbelow(True)
    figure.suptitle(
        "N=31 PQAR classification and strict-harness qualification",
        fontsize=15,
        fontweight="bold",
    )
    figure.text(
        0.5,
        0.07,
        "All rates retain n=30 and exact two-sided 95% Clopper–Pearson whiskers; "
        "FAIL/INCOMPLETE outcomes are never dropped.\nOmission-compatible "
        "same-host evidence is not detection, culpability, throughput, or safety proof.",
        ha="center",
        va="bottom",
        fontsize=8.8,
        color="#7f1d1d",
    )
    _save_pair(
        figure,
        output,
        FIGURE_STEMS[1],
        subject=(
            "Exact fixed-denominator PQAR rates and retained outcomes; same-host "
            "omission-compatible evidence without detection, culpability, throughput, "
            "or safety claims"
        ),
    )
    plt.close(figure)


def _render_timings(plt: Any, data: CampaignPlotData, output: Path) -> None:
    figure, axes = plt.subplots(2, 3, figsize=(15.2, 9.2))
    figure.subplots_adjust(
        left=0.075, right=0.985, top=0.84, bottom=0.18, hspace=0.37, wspace=0.25
    )
    median_positions = (96, 101, 106)
    for axis, metric in zip(axes.flat, TIMING_METRICS_NS, strict=True):
        available_labels: list[str] = []
        for arm_index, arm in enumerate(ARM_NAMES):
            series = data.timings[arm][metric]
            values_ms = [value / 1_000_000 for value in series.values_ns]
            axis.scatter(
                series.ordinals,
                values_ms,
                s=22,
                marker=ARM_MARKERS[arm_index],
                color=ARM_COLORS[arm_index],
                alpha=0.78,
                label=ARM_LABELS[arm].replace("\n", " "),
            )
            available_labels.append(
                f"{ARM_SHORT_LABELS[arm]} n={len(series.values_ns)}/30"
            )
            if series.median_ns is None:
                continue
            median_ms = series.median_ns / 1_000_000
            median_position = median_positions[arm_index]
            if (
                series.median_lower_ns is not None
                and series.median_upper_ns is not None
            ):
                lower_ms = series.median_lower_ns / 1_000_000
                upper_ms = series.median_upper_ns / 1_000_000
                axis.errorbar(
                    median_position,
                    median_ms,
                    yerr=(
                        [median_ms - lower_ms],
                        [upper_ms - median_ms],
                    ),
                    fmt="D",
                    markersize=5,
                    capsize=3,
                    linewidth=1.4,
                    color=ARM_COLORS[arm_index],
                )
            else:
                axis.scatter(
                    [median_position],
                    [median_ms],
                    marker="D",
                    s=32,
                    color=ARM_COLORS[arm_index],
                )
        axis.set_xlim(0, 109)
        axis.set_xticks((1, 30, 60, 90), ("1", "30", "60", "90"), fontsize=7)
        axis.set_title(METRIC_LABELS[metric], fontsize=10, fontweight="bold")
        axis.set_ylabel("Milliseconds (from integer ns)")
        axis.grid(axis="y", color="#cbd5e1", alpha=0.7)
        axis.set_axisbelow(True)
        axis.text(
            0.02,
            0.98,
            " | ".join(available_labels),
            transform=axis.transAxes,
            ha="left",
            va="top",
            fontsize=7,
            color="#475569",
        )
        axis.text(
            0.99,
            0.02,
            "right diamonds: F/O/S medians",
            transform=axis.transAxes,
            ha="right",
            va="bottom",
            fontsize=6.8,
            color="#475569",
        )
    for axis in axes[1]:
        axis.set_xlabel("Campaign ordinal; right diamonds are arm medians")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    figure.legend(
        handles, labels, loc="upper center", ncol=3, bbox_to_anchor=(0.5, 0.90)
    )
    figure.suptitle(
        "Same-host N=31 PQAR raw ordinal timings and exact median intervals",
        fontsize=15,
        fontweight="bold",
    )
    figure.text(
        0.5,
        0.055,
        "Every available raw integer-ns point is shown by ordinal; n/30 exposes "
        "missingness without imputation.\nDiamonds show medians and exact ≥95% "
        "sign-test intervals when bounded. Descriptive same-host evidence only—not "
        "detection, culpability, throughput evidence, or a safety proof.",
        ha="center",
        va="bottom",
        fontsize=8.8,
        color="#7f1d1d",
    )
    _save_pair(
        figure,
        output,
        FIGURE_STEMS[2],
        subject=(
            "Raw ordinal same-host PQAR timings with exact median intervals and "
            "explicit missingness; no detection, culpability, throughput, or safety claim"
        ),
    )
    plt.close(figure)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(
    output: Path,
    *,
    data: CampaignPlotData,
    seal: EvidenceSealMetadata,
) -> Path:
    figure_paths = sorted(
        path
        for path in output.iterdir()
        if path.is_file() and path.suffix in {".png", ".pdf"}
    )
    expected_names = {
        f"{stem}.{suffix}" for stem in FIGURE_STEMS for suffix in ("png", "pdf")
    }
    if {path.name for path in figure_paths} != expected_names:
        raise PlotError("rendered figure membership is incomplete or contains extras")
    files = [
        {
            "path": path.name,
            "sha256": _file_sha256(path),
            "size_bytes": path.stat().st_size,
        }
        for path in figure_paths
    ]
    manifest = {
        "schema_version": 1,
        "scenario": "n31-pqar-campaign-figure-manifest-v1",
        "source": {
            "campaign_acceptance": data.summary["campaign_acceptance"],
            "campaign_plan_sha256": data.summary["campaign_plan_sha256"],
            "campaign_profile": data.campaign_profile,
            "evidence_seal_sha256": seal.seal_sha256,
            "evidence_tree_sha256": seal.tree_sha256,
            "figure_eligible": data.summary["figure_eligible"],
            "fixed_denominator_per_arm": ATTEMPTS_PER_ARM,
            "kauri_revision": data.summary["kauri_revision"],
            "outcome_status": data.summary["outcome_status"],
            "pilot_gate_sha256": data.summary["pilot_gate_sha256"],
            "scheduled_attempts": SCHEDULED_ATTEMPTS,
            "trusted_provenance_sha256": data.summary["trusted_provenance_sha256"],
            "validated_summary_sha256": semantic_document_sha256(data.summary),
        },
        "files": files,
        "output_membership": [
            "figure-manifest.json",
            *[item["path"] for item in files],
        ],
        "rendering": {
            "backend": "Agg",
            "deterministic_metadata_epoch": "2020-01-01T00:00:00+00:00",
            "matplotlib_cache": "explicit-temporary-directory-outside-campaign",
        },
        "caveats": list(CAVEATS),
    }
    destination = output / "figure-manifest.json"
    try:
        with destination.open("xb") as stream:
            stream.write(canonical_json_bytes(manifest))
    except FileExistsError as error:
        raise PlotError("figure manifest already exists") from error
    return destination


def generate_figures(
    campaign_directory: Path,
    output_directory: Path,
    *,
    trusted_provenance: TrustedProvenance,
) -> tuple[Path, ...]:
    """Revalidate raw sealed evidence, then write three deterministic figure pairs."""

    campaign = campaign_directory.resolve()
    data, seal = _validated_source(campaign, trusted_provenance=trusted_provenance)
    output = _output_path(campaign, output_directory)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    try:
        with _matplotlib_agg(output) as plt:
            _render_confusion(plt, data, output)
            _render_rates_and_outcomes(plt, data, output)
            _render_timings(plt, data, output)
        _write_manifest(output, data=data, seal=seal)
        outputs = tuple(sorted(output.iterdir()))
        expected_names = {
            "figure-manifest.json",
            *{f"{stem}.{suffix}" for stem in FIGURE_STEMS for suffix in ("png", "pdf")},
        }
        if {path.name for path in outputs} != expected_names:
            raise PlotError("final figure output membership drifted")
        return outputs
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


def generate_figure(
    campaign_directory: Path,
    output_directory: Path,
    *,
    trusted_provenance: TrustedProvenance,
) -> tuple[Path, ...]:
    """Compatibility entry point matching the existing campaign plotters."""

    return generate_figures(
        campaign_directory,
        output_directory,
        trusted_provenance=trusted_provenance,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campaign-root",
        "--campaign",
        dest="campaign_root",
        required=True,
        type=Path,
    )
    parser.add_argument("--trusted-provenance", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        trusted = load_trusted_provenance(arguments.trusted_provenance.resolve())
        outputs = generate_figures(
            arguments.campaign_root,
            arguments.output_dir,
            trusted_provenance=trusted,
        )
    except (OSError, PlotError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
