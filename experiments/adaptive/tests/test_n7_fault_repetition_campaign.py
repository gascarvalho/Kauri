"""Strict synthetic contracts for the five-by-three N=7 repetition gate.

Fixtures are derived from the existing comparison tests and contain no live
process orchestration.  Every complete repetition must still pass through the
existing three-arm comparison validator.
"""

from __future__ import annotations

from copy import deepcopy
import importlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from experiments.adaptive.kauri_experiment import (
    n7_fault_repetition_campaign as campaign_module,
)
from experiments.adaptive.kauri_experiment.comparison import (
    build_n7_comparison,
)
from experiments.adaptive.kauri_experiment.n7_fault_repetition_campaign import (
    REPETITIONS_PER_ARM,
    build_n7_execution_binding,
    canonical_n7_fault_repetition_json,
    semantic_n7_campaign_sha256,
    summarize_n7_fault_repetition_campaign,
    validate_n7_fault_repetition_campaign,
)
from experiments.adaptive.tests.test_fault_comparison import (
    _verdicts as comparison_verdicts,
)


ARM_NAMES = (
    "sigkill_crash",
    "static_authenticated_false_report",
    "static_persistent_omission",
)
REVISION = "a" * 40
SEED = 41_719
DIAGNOSTIC_WINDOW = "n7-epoch0-tree6-tree0-static-v1"


def _comparison() -> Any:
    return build_n7_comparison(
        kauri_revision=REVISION,
        seed=SEED,
        crash_replica_id=1,
        false_reporter_id=6,
        false_report_target_id=1,
        persistent_omitter_id=1,
        diagnostic_window=DIAGNOSTIC_WINDOW,
    )


def _strict_attempts(comparison: Any) -> list[dict[str, object]]:
    attempts: list[dict[str, object]] = []
    for repetition in range(1, REPETITIONS_PER_ARM + 1):
        verdicts = comparison_verdicts(comparison)
        base_ns = repetition * 1_000_000_000
        action_ns = base_ns + 30_000_000
        settlement_delay_ms = repetition * 10
        for arm_index, arm_name in enumerate(ARM_NAMES):
            verdict = deepcopy(verdicts[arm_name])
            run_id = f"repetition-{repetition}-{arm_name}"
            verdict.update(
                {
                    "schema_version": 2,
                    "scenario": "n7-static-fault-comparison",
                    "arm": arm_name,
                    "campaign_repetition": repetition,
                    "run_id": run_id,
                    "interrupted": False,
                    "runtime_error": None,
                    "fixed_context_observation": {
                        "active_configuration_records": 12,
                        "configured_fault_threshold": 2,
                        "configured_quorum": 5,
                        "configured_replica_count": 7,
                        "invalid_records": [],
                    },
                    "common_commit_before": {
                        "block_height": repetition * 100,
                        "block_hash": f"{repetition:064x}",
                        "participants": list(range(7)),
                        "common_monotonic_raw_ns": base_ns + 10_000_000,
                    },
                    "common_commit_after": {
                        "block_height": repetition * 100 + 10,
                        "block_hash": f"{100 + repetition + arm_index:064x}",
                        "participants": (
                            [0, 2, 3, 4, 5, 6]
                            if arm_name == "sigkill_crash"
                            else list(range(7))
                        ),
                        "common_monotonic_raw_ns": action_ns
                        + (100 * (arm_index + 1) + repetition)
                        * 1_000_000,
                    },
                    "conflicting_commits": [],
                }
            )
            action = verdict["action_observation"]
            assert isinstance(action, dict)
            action["fault_evidence"] = {
                "journal_event_count": 2,
                "journal_path": "raw/fault-orchestrator.jsonl",
                "plan_path": "fault-plan.json",
                "plan_sha256": verdict["fault_plan_sha256"],
                "terminal_status": "succeeded",
            }
            if arm_name == "sigkill_crash":
                action.update(
                    {
                        "replica_id": 1,
                        "signal": "SIGKILL",
                        "returncode": -9,
                        "requested_monotonic_raw_ns": base_ns + 20_000_000,
                        "confirmed_monotonic_raw_ns": action_ns,
                    }
                )
            else:
                action.update(
                    {
                        "observed_monotonic_raw_ns": base_ns + 20_000_000,
                        "manager_acceptance_observed_monotonic_raw_ns": (
                            action_ns
                        ),
                    }
                )
                initial = verdict["manager_accepted_timeout_observation"]
                followup = verdict["followup_manager_observation"]
                assert isinstance(initial, dict)
                assert isinstance(followup, dict)
                initial["run_id"] = run_id
                initial["source_monotonic_ns"] = action_ns
                followup["run_id"] = run_id
                followup["source_monotonic_ns"] = (
                    action_ns + settlement_delay_ms * 1_000_000
                )
            attempts.append(verdict)
    return attempts


def _execution_binding(
    attempts: list[dict[str, object]],
    root: Path = Path("/frozen/n7-campaign"),
) -> dict[str, object]:
    by_slot = {
        (attempt["campaign_repetition"], attempt["arm"]): attempt
        for attempt in attempts
    }
    scheduled: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    for ordinal, (repetition, arm) in enumerate(
        campaign_module.FROZEN_ATTEMPT_SCHEDULE,
        start=1,
    ):
        results_root = root / f"attempt-{ordinal:02d}-results"
        command = [
            "/frozen/python",
            "/frozen/run_fault_comparison.py",
            "--arm",
            arm,
            "--results-root",
            str(results_root),
        ]
        scheduled.append(
            {
                "ordinal": ordinal,
                "campaign_repetition": repetition,
                "arm": arm,
                "results_root": str(results_root),
                "command": command,
            }
        )
        attempt = by_slot.get((repetition, arm))
        arm_verdict = None
        evidence_error = "missing arm verdict"
        returncode = 1
        if attempt is not None:
            semantic_attempt = deepcopy(attempt)
            semantic_attempt.pop("campaign_repetition")
            arm_verdict = {
                "path": str(
                    results_root / arm / str(attempt["run_id"]) / "arm-verdict.json"
                ),
                "raw_sha256": f"{ordinal:064x}",
                "canonical_sha256": semantic_n7_campaign_sha256(
                    semantic_attempt
                ),
                "run_id": attempt["run_id"],
            }
            evidence_error = None
            returncode = 0 if attempt["verdict"] == "PASS" else 1
        records.append(
            {
                "schema_version": 1,
                "scenario": "n7-static-fault-repetition-execution",
                "ordinal": ordinal,
                "campaign_repetition": repetition,
                "arm": arm,
                "command": command,
                "results_root": str(results_root),
                "returncode": returncode,
                "started_utc": "2026-07-31T10:00:00+00:00",
                "finished_utc": "2026-07-31T10:01:00+00:00",
                "elapsed_ms": 60_000,
                "stdout": {
                    "path": str(root / f"attempt-{ordinal:02d}.stdout.txt"),
                    "sha256": "a" * 64,
                },
                "stderr": {
                    "path": str(root / f"attempt-{ordinal:02d}.stderr.txt"),
                    "sha256": "b" * 64,
                },
                "arm_verdict": arm_verdict,
                "evidence_error": evidence_error,
            }
        )
    plan = {
        "schema_version": 1,
        "scenario": "n7-static-fault-repetition-plan",
        "kauri_revision": REVISION,
        "repetitions_per_arm": REPETITIONS_PER_ARM,
        "scheduled_attempt_count": len(scheduled),
        "retry_policy": "none",
        "execution_order": "sequential",
        "scheduled_attempts": scheduled,
    }
    return build_n7_execution_binding(plan, records)


def _stats(
    sample_count: int,
    minimum: int,
    median: int,
    maximum: int,
) -> dict[str, int]:
    return {
        "sample_count": sample_count,
        "minimum": minimum,
        "median": median,
        "maximum": maximum,
    }


def _summarize(
    comparison: Any,
    attempts: list[dict[str, object]],
) -> dict[str, object]:
    return summarize_n7_fault_repetition_campaign(
        comparison,
        attempts,
        _execution_binding(attempts),
    )


def test_accepts_exactly_five_validated_repetitions_per_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comparison = _comparison()
    attempts = _strict_attempts(comparison)
    original = campaign_module.summarize_comparison
    calls: list[tuple[int, tuple[str, ...]]] = []

    def observed_validator(
        frozen_comparison: Any,
        verdicts: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        calls.append(
            (
                next(iter(verdicts.values()))["campaign_repetition"],
                tuple(verdicts),
            )
        )
        return original(frozen_comparison, verdicts)

    monkeypatch.setattr(
        campaign_module,
        "summarize_comparison",
        observed_validator,
    )
    summary = _summarize(comparison, attempts)

    assert REPETITIONS_PER_ARM == 5
    assert calls == [(index, ARM_NAMES) for index in range(1, 6)]
    assert summary["schema_version"] == 1
    assert summary["scenario"] == "n7-static-fault-repetition-campaign"
    assert summary["verdict"] == "PASS"
    assert summary["kauri_revision"] == REVISION
    assert summary["seed"] == SEED
    assert summary["repetitions_per_arm"] == 5
    assert summary["scheduled_attempts"] == 15
    assert summary["preserved_attempts"] == 15
    assert summary["pass_counts"] == {name: 5 for name in ARM_NAMES}
    assert summary["missing_attempts"] == []
    assert summary["failed_attempts"] == []
    assert summary["diagnostic_outcomes"] == {
        "sigkill_crash": {"not_applicable": 5},
        "static_authenticated_false_report": {
            "settled_false_reporter": 5
        },
        "static_persistent_omission": {
            "settled_persistent_omitter": 5
        },
    }
    assert summary["post_fault_commit_continuity"] == {
        name: {"witnessed": 5, "scheduled": 5} for name in ARM_NAMES
    }
    assert summary["descriptive_timing_ms"] == {
        "sigkill_crash": {
            "diagnostic_settlement_delay_ms": None,
            "post_fault_commit_delay_ms": _stats(5, 101, 103, 105),
        },
        "static_authenticated_false_report": {
            "diagnostic_settlement_delay_ms": _stats(5, 10, 30, 50),
            "post_fault_commit_delay_ms": _stats(5, 201, 203, 205),
        },
        "static_persistent_omission": {
            "diagnostic_settlement_delay_ms": _stats(5, 10, 30, 50),
            "post_fault_commit_delay_ms": _stats(5, 301, 303, 305),
        },
    }
    assert summary["timing_scope"] == (
        "descriptive same-host CLOCK_MONOTONIC_RAW continuity witness; "
        "not throughput or an inferential performance claim"
    )

    repetitions = summary["repetitions"]
    assert [item["campaign_repetition"] for item in repetitions] == list(
        range(1, 6)
    )
    assert all(item["verdict"] == "PASS" for item in repetitions)
    assert all(item["comparison_summary"] is not None for item in repetitions)
    assert all(
        tuple(attempt["arm"] for attempt in item["attempts"]) == ARM_NAMES
        for item in repetitions
    )
    assert len(
        {
            attempt["run_id"]
            for item in repetitions
            for attempt in item["attempts"]
        }
    ) == 15

    attempts[0]["runtime_error"] = "late mutation"
    assert summary["repetitions"][0]["attempts"][0]["runtime_error"] is None


def test_preserves_a_scheduled_nonpass_without_replacement() -> None:
    comparison = _comparison()
    attempts = _strict_attempts(comparison)
    failed = next(
        attempt
        for attempt in attempts
        if attempt["campaign_repetition"] == 3
        and attempt["arm"] == "static_persistent_omission"
    )
    failed["verdict"] = "INCOMPLETE"
    failed["runtime_error"] = "injected incomplete attempt"

    summary = _summarize(comparison, attempts)

    assert summary["verdict"] == "INCOMPLETE"
    assert summary["pass_counts"]["static_persistent_omission"] == 4
    assert summary["failed_attempts"] == [
        {
            "campaign_repetition": 3,
            "arm": "static_persistent_omission",
            "run_id": "repetition-3-static_persistent_omission",
            "verdict": "INCOMPLETE",
            "runtime_error": "injected incomplete attempt",
        }
    ]
    repetition = summary["repetitions"][2]
    assert repetition["verdict"] == "INCOMPLETE"
    assert repetition["comparison_summary"] is None
    assert repetition["attempts"][2]["runtime_error"] == (
        "injected incomplete attempt"
    )
    assert validate_n7_fault_repetition_campaign(comparison, summary) == summary


def test_preserves_a_missing_scheduled_attempt_as_incomplete() -> None:
    comparison = _comparison()
    attempts = _strict_attempts(comparison)
    attempts = [
        attempt
        for attempt in attempts
        if not (
            attempt["campaign_repetition"] == 4
            and attempt["arm"] == "static_authenticated_false_report"
        )
    ]

    summary = _summarize(comparison, attempts)

    assert summary["verdict"] == "INCOMPLETE"
    assert summary["preserved_attempts"] == 14
    assert summary["missing_attempts"] == [
        {
            "campaign_repetition": 4,
            "arm": "static_authenticated_false_report",
        }
    ]
    assert summary["pass_counts"][
        "static_authenticated_false_report"
    ] == 4
    assert summary["repetitions"][3]["verdict"] == "INCOMPLETE"
    assert len(summary["repetitions"][3]["attempts"]) == 2


@pytest.mark.parametrize(
    "case",
    (
        "duplicate_slot",
        "duplicate_run_id",
        "boolean_repetition",
        "out_of_range_repetition",
        "revision_mismatch",
        "fault_plan_mismatch",
    ),
)
def test_rejects_malformed_or_mismatched_completed_attempts(case: str) -> None:
    comparison = _comparison()
    attempts = _strict_attempts(comparison)
    if case == "duplicate_slot":
        replacement = deepcopy(attempts[0])
        replacement["run_id"] = "selective-replacement"
        attempts.append(replacement)
    elif case == "duplicate_run_id":
        attempts[1]["run_id"] = attempts[0]["run_id"]
    elif case == "boolean_repetition":
        attempts[0]["campaign_repetition"] = True
    elif case == "out_of_range_repetition":
        attempts[0]["campaign_repetition"] = 6
    elif case == "revision_mismatch":
        attempts[0]["kauri_revision"] = "b" * 40
    elif case == "fault_plan_mismatch":
        attempts[0]["fault_plan_sha256"] = "0" * 64
    else:  # pragma: no cover - parametrization exhausts this branch.
        raise AssertionError(case)

    with pytest.raises(ValueError):
        summarize_n7_fault_repetition_campaign(comparison, attempts)


def test_summary_is_canonical_deterministic_and_tamper_evident() -> None:
    comparison = _comparison()
    first_attempts = _strict_attempts(comparison)
    second_attempts = _strict_attempts(comparison)
    first = _summarize(comparison, first_attempts)
    second = _summarize(comparison, second_attempts)

    assert first == second
    assert validate_n7_fault_repetition_campaign(comparison, first) == first
    encoded = canonical_n7_fault_repetition_json(first)
    assert encoded == json.dumps(
        first,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert json.loads(encoded) == first
    assert encoded == canonical_n7_fault_repetition_json(second)

    tampered = deepcopy(first)
    tampered["pass_counts"]["sigkill_crash"] = 5.0
    with pytest.raises(ValueError):
        validate_n7_fault_repetition_campaign(comparison, tampered)

    nonfinite = deepcopy(first)
    nonfinite["descriptive_timing_ms"]["sigkill_crash"][
        "post_fault_commit_delay_ms"
    ]["median"] = float("nan")
    with pytest.raises(ValueError):
        canonical_n7_fault_repetition_json(nonfinite)


def test_direct_attempts_without_execution_binding_cannot_pass() -> None:
    comparison = _comparison()

    summary = summarize_n7_fault_repetition_campaign(
        comparison,
        _strict_attempts(comparison),
    )

    assert summary["verdict"] == "INCOMPLETE"
    assert summary["execution_binding"] is None
    assert summary["execution_binding_failures"] == [
        "campaign execution binding is absent"
    ]


def test_strict_invalid_pass_triple_is_preserved_as_incomplete() -> None:
    comparison = _comparison()
    attempts = _strict_attempts(comparison)
    action = attempts[0]["action_observation"]
    after = attempts[0]["common_commit_after"]
    assert isinstance(action, dict)
    assert isinstance(after, dict)
    after["common_monotonic_raw_ns"] = action["confirmed_monotonic_raw_ns"] - 1

    summary = _summarize(comparison, attempts)

    assert summary["verdict"] == "INCOMPLETE"
    assert summary["strict_validation_failures"] == [
        {
            "campaign_repetition": 1,
            "error": (
                "sigkill_crash does not bracket fault evidence with common commits"
            ),
        }
    ]
    assert summary["repetitions"][0]["verdict"] == "INCOMPLETE"
    assert summary["preserved_attempts"] == 15


@pytest.mark.parametrize(
    "case",
    (
        "absent_binding",
        "plan_hash",
        "record_replacement",
        "record_order",
        "returncode",
        "results_path",
        "raw_verdict_hash",
        "canonical_verdict_hash",
    ),
)
def test_pass_artifact_rejects_execution_binding_exploits(case: str) -> None:
    comparison = _comparison()
    summary = _summarize(comparison, _strict_attempts(comparison))
    assert summary["verdict"] == "PASS"
    tampered = deepcopy(summary)
    binding = tampered["execution_binding"]
    assert isinstance(binding, dict)
    records = binding["execution_records"]
    assert isinstance(records, list)
    if case == "absent_binding":
        tampered["execution_binding"] = None
    elif case == "plan_hash":
        binding["campaign_plan_sha256"] = "0" * 64
    elif case == "record_replacement":
        records[1] = deepcopy(records[0])
    elif case == "record_order":
        records[0], records[1] = records[1], records[0]
    elif case == "returncode":
        records[0]["returncode"] = 1
    elif case == "results_path":
        records[0]["arm_verdict"]["path"] = records[1]["arm_verdict"]["path"]
    elif case == "raw_verdict_hash":
        records[0]["arm_verdict"]["raw_sha256"] = "0" * 64
    elif case == "canonical_verdict_hash":
        records[0]["arm_verdict"]["canonical_sha256"] = "0" * 64
    else:  # pragma: no cover - parametrization exhausts this branch.
        raise AssertionError(case)

    with pytest.raises(ValueError):
        validate_n7_fault_repetition_campaign(comparison, tampered)


def _command_option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def _install_fake_arm_runner(
    monkeypatch: pytest.MonkeyPatch,
    runner: Any,
    comparison: Any,
    *,
    missing_ordinal: int | None = None,
    malformed_ordinal: int | None = None,
    unsupported_ordinal: int | None = None,
    strict_invalid_ordinal: int | None = None,
) -> list[list[str]]:
    fixture_by_slot = {
        (attempt["campaign_repetition"], attempt["arm"]): attempt
        for attempt in _strict_attempts(comparison)
    }
    calls: list[list[str]] = []

    def fake_run(command: list[object], **_: object) -> subprocess.CompletedProcess[str]:
        normalized = [str(value) for value in command]
        calls.append(normalized)
        ordinal = len(calls)
        repetition, expected_arm = campaign_module.FROZEN_ATTEMPT_SCHEDULE[
            ordinal - 1
        ]
        assert _command_option(normalized, "--arm") == expected_arm
        results_root = Path(_command_option(normalized, "--results-root"))
        if ordinal != missing_ordinal:
            verdict = deepcopy(fixture_by_slot[(repetition, expected_arm)])
            verdict.pop("campaign_repetition")
            run_directory = (
                results_root / expected_arm / f"synthetic-run-{ordinal:02d}"
            )
            run_directory.mkdir(parents=True)
            verdict_path = run_directory / "arm-verdict.json"
            if ordinal == malformed_ordinal:
                verdict_path.write_text('{"verdict":', encoding="utf-8")
            else:
                if ordinal == unsupported_ordinal:
                    verdict["verdict"] = "UNSUPPORTED"
                if ordinal == strict_invalid_ordinal:
                    action = verdict["action_observation"]
                    after = verdict["common_commit_after"]
                    assert isinstance(action, dict)
                    assert isinstance(after, dict)
                    timestamp_key = (
                        "confirmed_monotonic_raw_ns"
                        if expected_arm == "sigkill_crash"
                        else "manager_acceptance_observed_monotonic_raw_ns"
                    )
                    after["common_monotonic_raw_ns"] = action[timestamp_key] - 1
                verdict_path.write_text(
                    json.dumps(verdict),
                    encoding="utf-8",
                )
        return subprocess.CompletedProcess(
            normalized,
            1 if ordinal == missing_ordinal else 0,
            stdout="",
            stderr=(
                "synthetic missing verdict"
                if ordinal == missing_ordinal
                else ""
            ),
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    return calls


def test_runner_executes_the_frozen_rotated_15_slot_schedule(
    repository_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module(
        "experiments.adaptive.run_n7_fault_repetition_campaign"
    )
    comparison = _comparison()
    calls = _install_fake_arm_runner(monkeypatch, runner, comparison)
    output = tmp_path / "n7-repetitions"

    artifact = runner.run_campaign(
        repository=repository_root,
        output_directory=output,
        kauri_revision=REVISION,
    )

    assert len(calls) == 15
    assert [
        _command_option(command, "--arm") for command in calls
    ] == [arm for _, arm in campaign_module.FROZEN_ATTEMPT_SCHEDULE]
    result_roots = [
        _command_option(command, "--results-root") for command in calls
    ]
    assert len(set(result_roots)) == 15
    assert all(
        str(repository_root / "experiments/adaptive/run_fault_comparison.py")
        in command
        for command in calls
    )

    observed = json.loads(Path(artifact).read_text(encoding="utf-8"))
    assert observed["verdict"] == "PASS"
    assert observed["schedule"] == [
        {
            "ordinal": ordinal,
            "campaign_repetition": repetition,
            "arm": arm,
        }
        for ordinal, (repetition, arm) in enumerate(
            campaign_module.FROZEN_ATTEMPT_SCHEDULE,
            start=1,
        )
    ]
    binding = observed["execution_binding"]
    assert binding["campaign_plan_sha256"] == semantic_n7_campaign_sha256(
        binding["campaign_plan"]
    )
    assert len(binding["execution_records"]) == 15
    assert all(record["returncode"] == 0 for record in binding["execution_records"])
    assert len(
        {record["results_root"] for record in binding["execution_records"]}
    ) == 15
    assert validate_n7_fault_repetition_campaign(
        comparison,
        observed,
        evidence_root=output,
    ) == observed

    relocated = tmp_path / "relocated-n7-repetitions"
    output.rename(relocated)
    assert not output.exists()
    assert validate_n7_fault_repetition_campaign(
        comparison,
        observed,
        evidence_root=relocated,
    ) == observed

    first_record = binding["execution_records"][0]
    recorded_results_root = Path(first_record["results_root"])
    recorded_verdict = Path(first_record["arm_verdict"]["path"])
    relative_verdict = recorded_verdict.relative_to(recorded_results_root)
    relocated_verdict = (
        relocated / recorded_results_root.name / relative_verdict
    )
    outside_verdict = tmp_path / "outside-arm-verdict.json"
    relocated_verdict.rename(outside_verdict)
    relocated_verdict.symlink_to(outside_verdict)
    with pytest.raises(
        campaign_module.N7FaultRepetitionCampaignError,
        match="verdict path escapes the campaign",
    ):
        validate_n7_fault_repetition_campaign(
            comparison,
            observed,
            evidence_root=relocated,
        )

    relocated_verdict.unlink()
    outside_verdict.rename(relocated_verdict)
    relocated_verdict.write_bytes(relocated_verdict.read_bytes() + b"\n")
    with pytest.raises(
        campaign_module.N7FaultRepetitionCampaignError,
        match="raw verdict hash changed",
    ):
        validate_n7_fault_repetition_campaign(
            comparison,
            observed,
            evidence_root=relocated,
        )


def test_runner_never_retries_and_preserves_a_missing_verdict(
    repository_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module(
        "experiments.adaptive.run_n7_fault_repetition_campaign"
    )
    comparison = _comparison()
    missing_ordinal = 8
    missing_repetition, missing_arm = (
        campaign_module.FROZEN_ATTEMPT_SCHEDULE[missing_ordinal - 1]
    )
    calls = _install_fake_arm_runner(
        monkeypatch,
        runner,
        comparison,
        missing_ordinal=missing_ordinal,
    )
    output = tmp_path / "n7-incomplete"

    artifact = runner.run_campaign(
        repository=repository_root,
        output_directory=output,
        kauri_revision=REVISION,
    )

    assert len(calls) == 15
    assert [
        _command_option(command, "--arm") for command in calls
    ].count(missing_arm) == 5
    observed = json.loads(Path(artifact).read_text(encoding="utf-8"))
    assert observed["verdict"] == "INCOMPLETE"
    assert observed["preserved_attempts"] == 14
    assert observed["missing_attempts"] == [
        {
            "campaign_repetition": missing_repetition,
            "arm": missing_arm,
        }
    ]


@pytest.mark.parametrize(
    ("failure_kind", "expected_preserved", "expected_strict_failures"),
    (
        ("malformed", 14, 0),
        ("unsupported", 14, 0),
        ("strict_invalid", 15, 1),
    ),
)
def test_runner_finishes_all_slots_and_emits_incomplete_for_invalid_evidence(
    repository_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    expected_preserved: int,
    expected_strict_failures: int,
) -> None:
    runner = importlib.import_module(
        "experiments.adaptive.run_n7_fault_repetition_campaign"
    )
    comparison = _comparison()
    invalid_ordinal = 5
    injection = {f"{failure_kind}_ordinal": invalid_ordinal}
    calls = _install_fake_arm_runner(
        monkeypatch,
        runner,
        comparison,
        **injection,
    )
    output = tmp_path / f"n7-{failure_kind}"

    artifact = runner.run_campaign(
        repository=repository_root,
        output_directory=output,
        kauri_revision=REVISION,
    )

    assert len(calls) == 15
    observed = json.loads(artifact.read_text(encoding="utf-8"))
    assert observed["verdict"] == "INCOMPLETE"
    assert observed["preserved_attempts"] == expected_preserved
    assert len(observed["strict_validation_failures"]) == expected_strict_failures
    records = observed["execution_binding"]["execution_records"]
    assert len(records) == 15
    invalid_record = records[invalid_ordinal - 1]
    if failure_kind == "strict_invalid":
        assert invalid_record["evidence_error"] is None
        assert invalid_record["arm_verdict"]["canonical_sha256"]
    else:
        assert invalid_record["evidence_error"]
        assert invalid_record["arm_verdict"]["raw_sha256"]
        assert invalid_record["arm_verdict"]["canonical_sha256"] is None
    assert validate_n7_fault_repetition_campaign(comparison, observed) == observed
