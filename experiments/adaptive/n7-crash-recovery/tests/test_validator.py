"""Synthetic validator tests only; generated runs are never thesis evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

import synthetic_run
import validator


def _rewrite_jsonl(path: Path, mutate: object) -> None:
    values = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    mutate(values)  # type: ignore[operator]
    for sequence, value in enumerate(values, start=1):
        value["source_sequence"] = sequence
    path.write_text(
        "\n".join(json.dumps(value, separators=(",", ":")) for value in values)
        + "\n",
        encoding="utf-8",
    )


def _rehash_runtime_artifact(manifest_path: Path, relative_path: str) -> None:
    manifest = synthetic_run.load(manifest_path)
    artifact = next(
        item
        for item in manifest["runtime_artifacts"]
        if item["path"] == relative_path
    )
    artifact["sha256"] = hashlib.sha256(
        (manifest_path.parent / relative_path).read_bytes()
    ).hexdigest()
    synthetic_run.save(manifest_path, manifest)


def test_complete_synthetic_run_passes_and_writes_only_canonical_inputs(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    output = tmp_path / "validated"

    verdict = validator.validate_run(manifest, epochs, output)

    assert verdict["verdict"] == "PASS"
    assert verdict["kauri_revision"] == synthetic_run.REVISION
    assert verdict["profile_identity"] == "n7-f2-q5-crash-recovery-v2"
    assert verdict["run_complete"] is True
    assert verdict["metrics"]["post_median_tps"] > verdict["metrics"][
        "degraded_median_tps"
    ]
    assert verdict["metrics"]["recovery_ratio"] == pytest.approx(1.0)
    assert verdict["metrics"]["complete_bucket_counts"]["baseline"] == 7
    assert verdict["metrics"]["complete_bucket_counts"]["post"] == 7
    assert verdict["metrics"]["maximum_commit_stall_seconds"] == {
        "baseline": pytest.approx(5.0),
        "degraded": pytest.approx(4.0),
        "post": pytest.approx(5.0),
    }
    assert verdict["metrics"]["final_reputation_scores"] == [
        -5,
        -5,
        2,
        2,
        2,
        2,
        2,
    ]
    assert verdict["boundaries"]["minimum_post_start_ns"] == (
        synthetic_run.ACTIVATION_NS
        + 2_000_000
        + synthetic_run.MINIMUM_POST_ACTIVATION_GRACE_NS
    )
    assert verdict["boundaries"]["first_common_successor_ns"] == 76_006_000_000
    assert verdict["boundaries"]["post_start_ns"] == 76_006_000_000
    assert validator._phase_for_timestamp(
        75_500_000_000,
        baseline_ns=synthetic_run.BASELINE_NS,
        crash_ns=synthetic_run.CRASH_0_NS,
        post_ns=verdict["boundaries"]["post_start_ns"],
    ) == "degraded"
    assert {path.name for path in output.iterdir()} == {
        "manifest.json",
        "profile.json",
        "epochs.json",
        "throughput.csv",
        "reputation.csv",
        "validation.json",
    }
    throughput = (output / "throughput.csv").read_text(encoding="utf-8")
    assert "leader_0_tps" in throughput
    assert "leader_6_tps" in throughput
    assert "baseline" in throughput
    assert "degraded" in throughput
    assert "post" in throughput


def test_complete_recurring_run_requires_exact_two_cycle_causality(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_recurring_run(tmp_path / "run")
    epoch_values = synthetic_run.load(epochs)["epochs"]
    assert [epoch["epoch_number"] for epoch in epoch_values] == [0, 1, 2]
    assert [
        (
            epoch["command"]["predecessor_epoch_number"],
            epoch["command"]["successor_epoch_number"],
        )
        for epoch in epoch_values[1:]
    ] == [(0, 1), (1, 2)]
    for epoch in epoch_values[1:]:
        for tree in epoch["trees"]:
            assert tree["wait_exempt"] == [0, 1]
            assert tree["members_breadth_first"].index(0) >= 3
            assert tree["members_breadth_first"].index(1) >= 3
    assert tuple(
        tree["members_breadth_first"][0]
        for tree in epoch_values[2]["trees"]
    ) == synthetic_run.ELIGIBLE_OPTIMIZATION_RANKING

    second_snapshot = synthetic_run.load(
        tmp_path / "run" / synthetic_run.TRANSITION_SNAPSHOT_PATHS[1]
    )
    assert second_snapshot["eligible_ranking"] == list(
        synthetic_run.ELIGIBLE_OPTIMIZATION_RANKING
    )
    assert (second_snapshot["baseline_cutoff"], second_snapshot["current_cutoff"]) == (
        synthetic_run.OPTIMIZATION_BASELINE_CUTOFF,
        synthetic_run.OPTIMIZATION_CURRENT_CUTOFF,
    )
    assert len(second_snapshot["observations"]) == (
        synthetic_run.OPTIMIZATION_CURRENT_CUTOFF
    )
    assert {
        observation["target_id"]
        for observation in second_snapshot["observations"][
            synthetic_run.OPTIMIZATION_BASELINE_CUTOFF:
        ]
    } == set(synthetic_run.ELIGIBLE_OPTIMIZATION_RANKING)
    assert all(
        observation["outcome"] == "on_time" and observation["latency_ns"] > 0
        for observation in second_snapshot["observations"][
            synthetic_run.OPTIMIZATION_BASELINE_CUTOFF:
        ]
    )
    assert all(
        sum(
            observation["target_id"] == target
            for observation in second_snapshot["observations"][
                synthetic_run.OPTIMIZATION_BASELINE_CUTOFF:
            ]
        )
        >= 2
        for target in synthetic_run.ELIGIBLE_OPTIMIZATION_RANKING
    )
    assert {
        (item["epoch_number"], item["epoch_digest"])
        for item in second_snapshot["observations"]
    } == {(1, synthetic_run.EPOCH_1_DIGEST)}

    manager_events = [
        json.loads(line)
        for line in (tmp_path / "run/raw/adaptive-manager.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [
        event["payload"]["identity"]["successor_epoch_number"]
        for event in manager_events
        if event["event_type"] == "adaptive_v2_ready"
    ] == [1, 2]
    terminals = [
        event
        for event in manager_events
        if event["event_type"] == "adaptive_v2_session_terminal"
    ]
    expected_terminal_fields = {
        "cycle_ordinal",
        "policy_intent",
        "outcome",
        "reason",
        "transition_artifact_id",
        "predecessor_epoch_number",
        "predecessor_epoch_digest",
        "successor_epoch_number",
        "successor_epoch_digest",
        "command_payload_digest",
        "winning_activation",
        "evidence_window_activation_generation",
        "baseline_evidence_cutoff",
        "current_evidence_cutoff",
    }
    assert all(set(event["payload"]) == expected_terminal_fields for event in terminals)
    assert [event["payload"]["cycle_ordinal"] for event in terminals] == [0, 1]
    assert [
        (
            event["payload"]["baseline_evidence_cutoff"],
            event["payload"]["current_evidence_cutoff"],
        )
        for event in terminals
    ] == [
        (
            synthetic_run.CONTAINMENT_BASELINE_CUTOFF,
            synthetic_run.CONTAINMENT_CURRENT_CUTOFF,
        ),
        (
            synthetic_run.OPTIMIZATION_BASELINE_CUTOFF,
            synthetic_run.OPTIMIZATION_CURRENT_CUTOFF,
        ),
    ]
    assert [
        event["payload"]["evidence_window_activation_generation"]
        for event in terminals
    ] == [
        synthetic_run.checked_activation_generation(0),
        synthetic_run.checked_activation_generation(1),
    ]
    assert len(
        {
            (
                event["payload"]["baseline_evidence_cutoff"],
                event["payload"]["current_evidence_cutoff"],
            )
            for event in terminals
        }
    ) == 2
    assert len(
        {
            event["payload"]["transition_artifact_id"]
            for event in terminals
        }
    ) == 2

    artifact_paths = [
        artifact["path"]
        for artifact in synthetic_run.load(manifest)["runtime_artifacts"]
        if artifact["kind"] in ("transition_bundle", "evidence_snapshot")
    ]
    assert artifact_paths == [
        synthetic_run.TRANSITION_BUNDLE_PATHS[0],
        synthetic_run.TRANSITION_SNAPSHOT_PATHS[0],
        synthetic_run.TRANSITION_BUNDLE_PATHS[1],
        synthetic_run.TRANSITION_SNAPSHOT_PATHS[1],
    ]
    assert len(set(artifact_paths)) == 4

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "PASS"
    assert verdict["profile_identity"] == (
        "n7-f2-q5-crash-recovery-recurring-v3"
    )
    assert verdict["metrics"]["complete_bucket_counts"] == {
        "baseline": 7,
        "degraded": 7,
        "containment": 8,
        "optimized": 7,
    }
    assert set(verdict["metrics"]["phase_median_tps"]) == {
        "baseline",
        "degraded",
        "containment",
        "optimized",
    }


@pytest.mark.parametrize(
    ("phase", "start_offset_ns", "end_offset_ns"),
    (
        ("degraded", 4_000_000_000, 4_000_000_000),
        ("containment", 0, -1_000_000_000),
    ),
)
def test_recurring_windows_require_exact_causal_boundaries(
    tmp_path: Path,
    phase: str,
    start_offset_ns: int,
    end_offset_ns: int,
) -> None:
    manifest, epochs = synthetic_run.create_recurring_run(tmp_path / "run")
    value = synthetic_run.load(manifest)
    window = next(
        item
        for item in value["throughput_windows"]
        if item["phase"] == phase
    )
    window["start_ns"] += start_offset_ns
    window["end_ns"] += end_offset_ns
    synthetic_run.save(manifest, value)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "exact causal boundaries" in verdict["reason"]


def test_recurring_manager_exit_after_only_first_cycle_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_recurring_run(tmp_path / "run")
    synthetic_run._write_stream(  # type: ignore[attr-defined]
        tmp_path / "run/raw/adaptive-manager.jsonl",
        synthetic_run.recurring_manager_events(completed_cycles=1),
    )

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert "second transition" in verdict["reason"]
    assert synthetic_run.load(tmp_path / "validated/validation.json") == verdict


@pytest.mark.parametrize("early_record", ("snapshot", "command"))
def test_second_transition_cannot_precede_minimum_predecessor_residency(
    tmp_path: Path,
    early_record: str,
) -> None:
    manifest, epochs = synthetic_run.create_recurring_run(tmp_path / "run")
    earliest_transition_ns = (
        synthetic_run.MANAGER_READY_NS
        + 1_000_000
        + 40_000_000_000
    )
    early_ns = earliest_transition_ns - 1

    if early_record == "snapshot":
        def move_second_snapshot(values: list[dict[str, object]]) -> None:
            snapshot = next(
                value
                for value in values
                if value["event_type"] == "adaptive_v2_evidence_snapshot"
                and value["payload"]["cycle_ordinal"] == 1  # type: ignore[index]
            )
            snapshot["source_monotonic_ns"] = early_ns
            values.sort(key=lambda value: int(value["source_monotonic_ns"]))

        _rewrite_jsonl(
            tmp_path / "run/raw/adaptive-manager.jsonl",
            move_second_snapshot,
        )
    else:
        for replica in synthetic_run.ELIGIBLE_OPTIMIZATION_RANKING:
            def move_second_command(
                values: list[dict[str, object]],
            ) -> None:
                command = next(
                    value
                    for value in values
                    if value["event_type"] == "epoch.command_committed"
                    and value["payload"]["successor_epoch_number"] == 2  # type: ignore[index]
                )
                command["source_monotonic_ns"] = early_ns
                values.sort(key=lambda value: int(value["source_monotonic_ns"]))

            _rewrite_jsonl(
                tmp_path / f"run/raw/replica-{replica}.jsonl",
                move_second_command,
            )

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "minimum predecessor residency" in verdict["reason"]


def test_second_transition_residency_starts_at_previous_manager_terminal(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_recurring_run(tmp_path / "run")

    def move_first_terminal(values: list[dict[str, object]]) -> None:
        terminal = next(
            value
            for value in values
            if value["event_type"] == "adaptive_v2_session_terminal"
            and value["payload"]["cycle_ordinal"] == 0  # type: ignore[index]
        )
        terminal["source_monotonic_ns"] = 79_500_000_000
        values.sort(key=lambda value: int(value["source_monotonic_ns"]))

    _rewrite_jsonl(
        tmp_path / "run/raw/adaptive-manager.jsonl",
        move_first_terminal,
    )

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "minimum predecessor residency" in verdict["reason"]


def test_rehashed_noncanonical_transition_bundle_is_rejected(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_recurring_run(tmp_path / "run")
    relative_path = synthetic_run.TRANSITION_BUNDLE_PATHS[1]
    (tmp_path / "run" / relative_path).write_bytes(b"not a canonical bundle")
    _rehash_runtime_artifact(manifest, relative_path)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "transition bundle" in verdict["reason"]


def test_rehashed_canonical_bundle_cannot_bind_the_wrong_transition(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_recurring_run(tmp_path / "run")
    first_path = tmp_path / "run" / synthetic_run.TRANSITION_BUNDLE_PATHS[0]
    second_relative = synthetic_run.TRANSITION_BUNDLE_PATHS[1]
    (tmp_path / "run" / second_relative).write_bytes(first_path.read_bytes())
    _rehash_runtime_artifact(manifest, second_relative)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "command identity differs from epochs.json" in verdict["reason"]


@pytest.mark.parametrize("nested", (False, True), ids=("snapshot", "observation"))
@pytest.mark.parametrize("mutation", ("extra", "missing"))
def test_evidence_snapshot_payloads_require_exact_fields(
    tmp_path: Path,
    nested: bool,
    mutation: str,
) -> None:
    manifest, epochs = synthetic_run.create_recurring_run(tmp_path / "run")
    relative_path = synthetic_run.TRANSITION_SNAPSHOT_PATHS[1]
    snapshot_path = tmp_path / "run" / relative_path
    snapshot = synthetic_run.load(snapshot_path)
    target = snapshot["observations"][0] if nested else snapshot
    field = "reporter_id" if nested else "eligible_ranking"
    if mutation == "extra":
        target["unexpected_behavior_switch"] = True
    else:
        target.pop(field)
    synthetic_run.save(snapshot_path, snapshot)
    _rehash_runtime_artifact(manifest, relative_path)

    def mutate_event(values: list[dict[str, object]]) -> None:
        event = next(
            value
            for value in values
            if value["event_type"] == "adaptive_v2_evidence_snapshot"
            and value["payload"]["cycle_ordinal"] == 1  # type: ignore[index]
        )
        payload = event["payload"]  # type: ignore[assignment]
        event_target = payload["observations"][0] if nested else payload  # type: ignore[index]
        if mutation == "extra":
            event_target["unexpected_behavior_switch"] = True
        else:
            event_target.pop(field)

    _rewrite_jsonl(
        tmp_path / "run/raw/adaptive-manager.jsonl",
        mutate_event,
    )

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "fields are invalid" in verdict["reason"]


def _window_observation(
    observations: list[dict[str, object]],
    *,
    target: int,
    reporter: int,
    outcome: str = "on_time",
    latency_ns: int | None = None,
    observation_id: str | None = None,
) -> None:
    sequence = len(observations) + 1
    observation: dict[str, object] = {
        "observation_id": observation_id or f"{50_000 + sequence:064x}",
        "ingestion_sequence": sequence,
        "epoch_number": 1,
        "epoch_digest": synthetic_run.EPOCH_1_DIGEST,
        "reporter_id": reporter,
        "target_id": target,
        "outcome": outcome,
    }
    if latency_ns is not None:
        observation["latency_ns"] = latency_ns
    observations.append(observation)


def _variable_containment_window(
) -> tuple[list[dict[str, object]], int]:
    observations: list[dict[str, object]] = []
    for target in range(7):
        for attempt in range(2):
            _window_observation(
                observations,
                target=target,
                reporter=(target + attempt + 1) % 7,
            )
    _window_observation(observations, target=2, reporter=3)
    baseline_cutoff = len(observations)
    _window_observation(observations, target=6, reporter=2, latency_ns=90)
    for target in (0, 1):
        for reporter in (2, 3, 4):
            for _attempt in range(2):
                _window_observation(
                    observations,
                    target=target,
                    reporter=reporter,
                    outcome="timeout",
                )
    _window_observation(
        observations,
        target=0,
        reporter=5,
        outcome="timeout",
    )
    return observations, baseline_cutoff


def _variable_optimization_window(
) -> tuple[list[dict[str, object]], int]:
    observations: list[dict[str, object]] = []
    for target in range(2, 7):
        for attempt in range(2):
            _window_observation(
                observations,
                target=target,
                reporter=2 + ((target - 2 + attempt + 1) % 5),
            )
    _window_observation(observations, target=2, reporter=3)
    _window_observation(observations, target=6, reporter=2)
    baseline_cutoff = len(observations)
    latencies = {
        2: (500, 520),
        3: (400, 420),
        4: (300, 320),
        5: (200, 220),
        6: (100, 120, 140),
    }
    for target, values in latencies.items():
        for attempt, latency_ns in enumerate(values):
            _window_observation(
                observations,
                target=target,
                reporter=2 + ((target - 2 + attempt + 1) % 5),
                latency_ns=latency_ns,
            )
    return observations, baseline_cutoff


def test_containment_evidence_window_accepts_variable_extra_records() -> None:
    observations, baseline_cutoff = _variable_containment_window()

    ranking = validator._validate_recurring_evidence_window(
        synthetic_run.recurring_transition_requests()[0],
        observations,
        baseline_cutoff=baseline_cutoff,
        minimum_attempts=2,
        minimum_reporters=3,
    )

    assert ranking is None


def test_containment_evidence_window_rejects_missing_reporter_threshold() -> None:
    observations, baseline_cutoff = _variable_containment_window()
    observations = [
        observation
        for observation in observations
        if not (
            observation["ingestion_sequence"] > baseline_cutoff
            and observation["target_id"] == 0
            and observation["reporter_id"] == 4
        )
    ]

    with pytest.raises(validator.ValidationError, match="guarded timeout"):
        validator._validate_recurring_evidence_window(
            synthetic_run.recurring_transition_requests()[0],
            observations,
            baseline_cutoff=baseline_cutoff,
            minimum_attempts=2,
            minimum_reporters=3,
        )


def test_optimization_evidence_window_ranks_a_variable_fresh_suffix() -> None:
    observations, baseline_cutoff = _variable_optimization_window()

    ranking = validator._validate_recurring_evidence_window(
        synthetic_run.recurring_transition_requests()[1],
        observations,
        baseline_cutoff=baseline_cutoff,
        minimum_attempts=2,
        minimum_reporters=3,
    )

    assert ranking == list(synthetic_run.ELIGIBLE_OPTIMIZATION_RANKING)


def test_optimization_evidence_window_rejects_missing_fresh_attempt() -> None:
    observations, baseline_cutoff = _variable_optimization_window()
    removed = False
    retained: list[dict[str, object]] = []
    for observation in observations:
        if (
            not removed
            and observation["ingestion_sequence"] > baseline_cutoff
            and observation["target_id"] == 2
        ):
            removed = True
            continue
        retained.append(observation)

    with pytest.raises(validator.ValidationError, match="fresh on-time attempts"):
        validator._validate_recurring_evidence_window(
            synthetic_run.recurring_transition_requests()[1],
            retained,
            baseline_cutoff=baseline_cutoff,
            minimum_attempts=2,
            minimum_reporters=3,
        )


def test_optimization_excludes_a_correlated_cross_baseline_late() -> None:
    observations, baseline_cutoff = _variable_optimization_window()
    boundary_id = f"{60_001:064x}"
    boundary_timeout = {
        **observations[0],
        "observation_id": boundary_id,
        "reporter_id": 3,
        "target_id": 2,
        "outcome": "timeout",
    }
    boundary_timeout.pop("latency_ns", None)
    observations.insert(baseline_cutoff, boundary_timeout)
    baseline_cutoff += 1
    boundary_late = {
        **boundary_timeout,
        "outcome": "late",
        "latency_ns": 1,
    }
    observations.insert(baseline_cutoff, boundary_late)
    for sequence, observation in enumerate(observations, start=1):
        observation["ingestion_sequence"] = sequence

    ranking = validator._validate_recurring_evidence_window(
        synthetic_run.recurring_transition_requests()[1],
        observations,
        baseline_cutoff=baseline_cutoff,
        minimum_attempts=2,
        minimum_reporters=3,
    )

    assert ranking == list(synthetic_run.ELIGIBLE_OPTIMIZATION_RANKING)
    attempts = validator._replay_snapshot_attempts(observations)
    fresh_scores = validator._score_snapshot_replicas(
        attempt
        for attempt in attempts.values()
        if attempt.first_ingestion_sequence > baseline_cutoff
    )
    assert fresh_scores[2].attempt_count == 2


def test_optimization_scores_tolerated_late_and_timeout_attempts() -> None:
    observations, baseline_cutoff = _variable_optimization_window()
    late_attempt = f"{60_002:064x}"
    _window_observation(
        observations,
        target=6,
        reporter=2,
        outcome="timeout",
        observation_id=late_attempt,
    )
    _window_observation(
        observations,
        target=6,
        reporter=2,
        outcome="late",
        latency_ns=50,
        observation_id=late_attempt,
    )
    _window_observation(
        observations,
        target=5,
        reporter=6,
        latency_ns=210,
    )
    _window_observation(
        observations,
        target=5,
        reporter=6,
        outcome="timeout",
    )

    ranking = validator._validate_recurring_evidence_window(
        synthetic_run.recurring_transition_requests()[1],
        observations,
        baseline_cutoff=baseline_cutoff,
        minimum_attempts=2,
        minimum_reporters=3,
    )

    # C++ ranks response rate before timeout rate and latency.  The late attempt
    # is one response plus one timeout; the timeout-only attempt is no response.
    assert ranking == [4, 3, 2, 6, 5]
    attempts = validator._replay_snapshot_attempts(observations)
    fresh_scores = validator._score_snapshot_replicas(
        attempt
        for attempt in attempts.values()
        if attempt.first_ingestion_sequence > baseline_cutoff
    )
    assert (
        fresh_scores[6].attempt_count,
        fresh_scores[6].response_rate_ppm,
        fresh_scores[6].timeout_rate_ppm,
    ) == (4, 1_000_000, 250_000)
    assert (
        fresh_scores[5].attempt_count,
        fresh_scores[5].response_rate_ppm,
        fresh_scores[5].timeout_rate_ppm,
    ) == (4, 750_000, 250_000)


def test_optimization_matches_cpp_percentile_count_and_id_ties() -> None:
    observations: list[dict[str, object]] = []
    for target in range(2, 7):
        for attempt in range(2):
            _window_observation(
                observations,
                target=target,
                reporter=2 + ((target - 2 + attempt + 1) % 5),
            )
    baseline_cutoff = len(observations)
    fresh_latencies = {
        2: (100, 200),
        3: (100, 100, 300),
        4: (100, 100, 300),
        5: (50, 50),
        6: (10, 10),
    }
    for target, latencies in fresh_latencies.items():
        for attempt, latency_ns in enumerate(latencies):
            _window_observation(
                observations,
                target=target,
                reporter=2 + ((target - 2 + attempt + 1) % 5),
                latency_ns=latency_ns,
            )

    ranking = validator._validate_recurring_evidence_window(
        synthetic_run.recurring_transition_requests()[1],
        observations,
        baseline_cutoff=baseline_cutoff,
        minimum_attempts=2,
        minimum_reporters=3,
    )

    # Targets 2/3/4 all have nearest-rank p50=100.  The 3-attempt targets
    # precede target 2, then replica id breaks the remaining exact tie.
    assert ranking == [6, 5, 3, 4, 2]


def test_optimization_excludes_responsive_inherited_constraints() -> None:
    observations, baseline_cutoff = _variable_optimization_window()
    for target in (0, 1):
        for attempt in range(2):
            _window_observation(
                observations,
                target=target,
                reporter=2 + attempt,
                latency_ns=1,
            )

    ranking = validator._validate_recurring_evidence_window(
        synthetic_run.recurring_transition_requests()[1],
        observations,
        baseline_cutoff=baseline_cutoff,
        minimum_attempts=2,
        minimum_reporters=3,
    )

    assert ranking == list(synthetic_run.ELIGIBLE_OPTIMIZATION_RANKING)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("standalone_late", "starts with a late"),
        ("mismatched_late", "timeout-to-late correlation"),
        ("duplicate_on_time", "transition is invalid"),
        ("second_timeout", "transition is invalid"),
    ),
)
def test_optimization_rejects_malformed_attempt_transitions(
    mutation: str,
    reason: str,
) -> None:
    observations, baseline_cutoff = _variable_optimization_window()
    attempt_id = f"{60_003:064x}"
    if mutation == "standalone_late":
        _window_observation(
            observations,
            target=6,
            reporter=2,
            outcome="late",
            latency_ns=50,
            observation_id=attempt_id,
        )
    elif mutation == "mismatched_late":
        _window_observation(
            observations,
            target=6,
            reporter=2,
            outcome="timeout",
            observation_id=attempt_id,
        )
        _window_observation(
            observations,
            target=6,
            reporter=3,
            outcome="late",
            latency_ns=50,
            observation_id=attempt_id,
        )
    elif mutation == "duplicate_on_time":
        _window_observation(
            observations,
            target=6,
            reporter=2,
            latency_ns=50,
            observation_id=attempt_id,
        )
        _window_observation(
            observations,
            target=6,
            reporter=2,
            latency_ns=50,
            observation_id=attempt_id,
        )
    else:
        _window_observation(
            observations,
            target=6,
            reporter=2,
            outcome="timeout",
            observation_id=attempt_id,
        )
        _window_observation(
            observations,
            target=6,
            reporter=2,
            outcome="timeout",
            observation_id=attempt_id,
        )

    with pytest.raises(validator.ValidationError, match=reason):
        validator._validate_recurring_evidence_window(
            synthetic_run.recurring_transition_requests()[1],
            observations,
            baseline_cutoff=baseline_cutoff,
            minimum_attempts=2,
            minimum_reporters=3,
        )


def test_optimization_rejects_timeout_rate_above_cpp_threshold() -> None:
    observations, baseline_cutoff = _variable_optimization_window()
    late_attempt = f"{60_004:064x}"
    _window_observation(
        observations,
        target=5,
        reporter=6,
        outcome="timeout",
        observation_id=late_attempt,
    )
    _window_observation(
        observations,
        target=5,
        reporter=6,
        outcome="late",
        latency_ns=50,
        observation_id=late_attempt,
    )

    with pytest.raises(validator.ValidationError, match="timeout rate"):
        validator._validate_recurring_evidence_window(
            synthetic_run.recurring_transition_requests()[1],
            observations,
            baseline_cutoff=baseline_cutoff,
            minimum_attempts=2,
            minimum_reporters=3,
        )


def test_optimization_rejects_cpp_trailing_timeout_streak() -> None:
    observations, baseline_cutoff = _variable_optimization_window()
    for _ in range(4):
        _window_observation(
            observations,
            target=5,
            reporter=6,
            latency_ns=210,
        )
    for reporter in (6, 2):
        _window_observation(
            observations,
            target=5,
            reporter=reporter,
            outcome="timeout",
        )

    with pytest.raises(validator.ValidationError, match="trailing timeout"):
        validator._validate_recurring_evidence_window(
            synthetic_run.recurring_transition_requests()[1],
            observations,
            baseline_cutoff=baseline_cutoff,
            minimum_attempts=2,
            minimum_reporters=3,
        )


def test_optimization_applies_cpp_attempt_window_before_ranking() -> None:
    observations, baseline_cutoff = _variable_optimization_window()
    first_fresh = next(
        index
        for index, observation in enumerate(observations)
        if observation["ingestion_sequence"] > baseline_cutoff
    )
    old_timeout = {
        **observations[first_fresh],
        "observation_id": f"{60_005:064x}",
        "reporter_id": 3,
        "target_id": 2,
        "outcome": "timeout",
    }
    old_timeout.pop("latency_ns", None)
    observations.insert(first_fresh, old_timeout)
    for _ in range(30):
        _window_observation(
            observations,
            target=2,
            reporter=3,
            latency_ns=500,
        )
    for sequence, observation in enumerate(observations, start=1):
        observation["ingestion_sequence"] = sequence

    ranking = validator._validate_recurring_evidence_window(
        synthetic_run.recurring_transition_requests()[1],
        observations,
        baseline_cutoff=baseline_cutoff,
        minimum_attempts=2,
        minimum_reporters=3,
    )

    assert ranking == list(synthetic_run.ELIGIBLE_OPTIMIZATION_RANKING)


def test_containment_accepts_tolerated_late_and_timeout_evidence() -> None:
    observations, baseline_cutoff = _variable_containment_window()
    late_attempt = f"{60_006:064x}"
    _window_observation(observations, target=3, reporter=4)
    _window_observation(
        observations,
        target=3,
        reporter=4,
        outcome="timeout",
    )
    _window_observation(
        observations,
        target=2,
        reporter=3,
        outcome="timeout",
        observation_id=late_attempt,
    )
    _window_observation(
        observations,
        target=2,
        reporter=3,
        outcome="late",
        latency_ns=90,
        observation_id=late_attempt,
    )

    ranking = validator._validate_recurring_evidence_window(
        synthetic_run.recurring_transition_requests()[0],
        observations,
        baseline_cutoff=baseline_cutoff,
        minimum_attempts=2,
        minimum_reporters=3,
    )

    assert ranking is None


def test_containment_guard_counts_only_unresolved_timeout_attempts() -> None:
    observations, baseline_cutoff = _variable_containment_window()
    timeout = next(
        observation
        for observation in observations
        if observation["ingestion_sequence"] > baseline_cutoff
        and observation["target_id"] == 0
        and observation["reporter_id"] == 4
    )
    _window_observation(
        observations,
        target=0,
        reporter=4,
        outcome="late",
        latency_ns=90,
        observation_id=str(timeout["observation_id"]),
    )

    with pytest.raises(validator.ValidationError, match="guarded timeout"):
        validator._validate_recurring_evidence_window(
            synthetic_run.recurring_transition_requests()[0],
            observations,
            baseline_cutoff=baseline_cutoff,
            minimum_attempts=2,
            minimum_reporters=3,
        )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("skipped_epoch", "epoch numbers must be contiguous"),
        ("forked_predecessor", "does not continue the exact predecessor"),
        ("reused_command", "command payload digest is reused"),
        ("reused_artifact_path", "transition artifact paths must be distinct"),
        ("overwritten_bundle", "runtime artifact SHA-256 mismatch"),
        ("duplicate_ready", "duplicate adaptive_v2_ready"),
        ("duplicate_terminal", "duplicate adaptive_v2_session_terminal"),
        ("retired_epoch_evidence", "fresh Epoch 1 evidence"),
    ),
)
def test_recurring_validator_rejects_noncausal_or_reused_evidence(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    manifest, epochs = synthetic_run.create_recurring_run(tmp_path / "run")
    if mutation in ("skipped_epoch", "forked_predecessor", "reused_command"):
        value = synthetic_run.load(epochs)
        second_command = value["epochs"][2]["command"]
        if mutation == "skipped_epoch":
            value["epochs"][2]["epoch_number"] = 3
            second_command["successor_epoch_number"] = 3
        elif mutation == "forked_predecessor":
            second_command["predecessor_epoch_number"] = 0
            second_command["predecessor_epoch_digest"] = (
                synthetic_run.EPOCH_0_DIGEST
            )
        else:
            second_command["payload_digest"] = synthetic_run.PAYLOAD_DIGEST
        synthetic_run.save(epochs, value)
    elif mutation == "reused_artifact_path":
        value = synthetic_run.load(manifest)
        bundles = [
            artifact
            for artifact in value["runtime_artifacts"]
            if artifact["kind"] == "transition_bundle"
        ]
        bundles[1]["path"] = bundles[0]["path"]
        synthetic_run.save(manifest, value)
    elif mutation == "overwritten_bundle":
        path = tmp_path / "run" / synthetic_run.TRANSITION_BUNDLE_PATHS[0]
        path.write_bytes(path.read_bytes() + b"overwritten")
    elif mutation in ("duplicate_ready", "duplicate_terminal"):
        manager = tmp_path / "run/raw/adaptive-manager.jsonl"
        event_type = (
            "adaptive_v2_ready"
            if mutation == "duplicate_ready"
            else "adaptive_v2_session_terminal"
        )

        def duplicate(values: list[dict[str, object]]) -> None:
            event = next(
                value
                for value in reversed(values)
                if value["event_type"] == event_type
            )
            copied = json.loads(json.dumps(event))
            copied["source_monotonic_ns"] = int(
                copied["source_monotonic_ns"]
            ) + 1
            values.append(copied)
            values.sort(key=lambda value: int(value["source_monotonic_ns"]))

        _rewrite_jsonl(manager, duplicate)
    else:
        snapshot_path = (
            tmp_path / "run" / synthetic_run.TRANSITION_SNAPSHOT_PATHS[1]
        )
        snapshot = synthetic_run.load(snapshot_path)
        retired = dict(
            synthetic_run.recurring_evidence_snapshots()[0]["observations"][0]
        )
        snapshot["observations"].append(retired)
        synthetic_run.save(snapshot_path, snapshot)
        _rehash_runtime_artifact(
            manifest, synthetic_run.TRANSITION_SNAPSHOT_PATHS[1]
        )

        def replay_into_second_snapshot(
            values: list[dict[str, object]],
        ) -> None:
            event = next(
                value
                for value in values
                if value["event_type"] == "adaptive_v2_evidence_snapshot"
                and value["payload"]["cycle_ordinal"] == 1
            )
            event["payload"]["observations"].append(retired)

        _rewrite_jsonl(
            tmp_path / "run/raw/adaptive-manager.jsonl",
            replay_into_second_snapshot,
        )

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert reason in verdict["reason"]


def test_ranked_successor_root_cycle_is_accepted(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    value = synthetic_run.load(epochs)
    root_order = (2, 6, 3, 5, 4)
    for tree_id, root in enumerate(root_order):
        other_survivors = [
            replica for replica in root_order if replica != root
        ]
        tree = value["epochs"][1]["trees"][tree_id]
        tree["members_breadth_first"] = [
            root,
            *other_survivors,
            0,
            1,
        ]
    synthetic_run.save(epochs, value)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "PASS"


def test_epoch_transition_accepts_predecessor_drain_beyond_minimum_grace() -> None:
    minimum_post_start_ns = 205
    commits = [
        SimpleNamespace(height=770, timestamp_ns=100, epoch_number=0),
        SimpleNamespace(height=771, timestamp_ns=210, epoch_number=0),
        SimpleNamespace(height=779, timestamp_ns=220, epoch_number=0),
        SimpleNamespace(height=780, timestamp_ns=230, epoch_number=1),
    ]
    assert commits[1].timestamp_ns > minimum_post_start_ns
    assert commits[2].timestamp_ns > minimum_post_start_ns

    first_successor = validator._validate_epoch_transition(
        commits,
        activation_height=770,
        activation_ns=105,
    )
    assert first_successor.height == 780
    assert first_successor.timestamp_ns == 230


def test_epoch_transition_rejects_predecessor_after_successor() -> None:
    commits = [
        SimpleNamespace(height=770, timestamp_ns=100, epoch_number=0),
        SimpleNamespace(height=771, timestamp_ns=120, epoch_number=1),
        SimpleNamespace(height=772, timestamp_ns=230, epoch_number=0),
    ]

    with pytest.raises(
        validator.ValidationError,
        match="follows a successor commit",
    ):
        validator._validate_epoch_transition(
            commits,
            activation_height=770,
            activation_ns=105,
        )


def test_successor_beyond_frozen_recovery_deadline_fails(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    observer = tmp_path / "run/raw/replica-2.jsonl"

    def delay_first_successor(values: list[dict[str, object]]) -> None:
        for value in values:
            if value["event_type"] not in (
                "block.commit_observed",
                "block.committed",
            ):
                continue
            height = value["payload"]["block_height"]
            if height == 18:
                value["source_monotonic_ns"] = 85_002_000_000
            elif height == 19:
                value["source_monotonic_ns"] = 86_002_000_000

    _rewrite_jsonl(observer, delay_first_successor)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "first common successor commit exceeds" in verdict["reason"]
    assert "10s" in verdict["reason"]


def test_delayed_survivor_witness_exceeds_common_recovery_deadline(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    survivor = tmp_path / "run/raw/replica-6.jsonl"

    def delay_survivor_successor(values: list[dict[str, object]]) -> None:
        for value in values:
            if value["event_type"] not in (
                "block.commit_observed",
                "block.committed",
            ):
                continue
            height = value["payload"]["block_height"]
            if height == 18:
                value["source_monotonic_ns"] = 85_006_000_000
            elif height == 19:
                value["source_monotonic_ns"] = 86_006_000_000

    _rewrite_jsonl(survivor, delay_survivor_successor)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "first common successor commit exceeds" in verdict["reason"]
    assert "10s" in verdict["reason"]


def test_activation_spread_uses_frozen_leader_grace(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    survivor = tmp_path / "run/raw/replica-6.jsonl"

    def delay_activation(values: list[dict[str, object]]) -> None:
        activation = next(
            value
            for value in values
            if value["event_type"] == "epoch.activated"
        )
        activation["source_monotonic_ns"] = 75_200_000_000

    _rewrite_jsonl(survivor, delay_activation)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "survivor activation spread exceeds frozen leader activation grace" in (
        verdict["reason"]
    )


def test_first_successor_commit_must_be_common_to_survivors(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    survivor = tmp_path / "run/raw/replica-4.jsonl"

    def remove_first_successor(values: list[dict[str, object]]) -> None:
        values[:] = [
            value
            for value in values
            if not (
                value["event_type"]
                in ("block.commit_observed", "block.committed")
                and value["payload"]["block_height"] == 18
            )
        ]

    _rewrite_jsonl(survivor, remove_first_successor)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert "replica-4 is missing first successor height 18" in verdict["reason"]


def test_repository_profile_freezes_the_recurring_n7_contract() -> None:
    profile_path = Path(validator.__file__).resolve().with_name("profile.json")
    profile_bytes = profile_path.read_bytes()
    profile = json.loads(profile_bytes)

    assert profile["profile_id"] == "n7-f2-q5-crash-recovery-recurring-v3"
    assert profile["replica_ids"] == list(range(7))
    assert (profile["fault_threshold"], profile["quorum"]) == (2, 5)
    assert profile["crash_targets"] == [0, 1]
    assert profile["transition_requests"] == (
        synthetic_run.recurring_transition_requests()
    )
    assert [
        request["minimum_predecessor_residency_ms"]
        for request in profile["transition_requests"]
    ] == [0, 40_000]
    assert profile["throughput_windows"] == [
        {
            "phase": phase,
            "epoch_number": epoch_number,
            "bucket_count": 7,
        }
        for phase, epoch_number in (
            ("baseline", 0),
            ("degraded", 0),
            ("containment", 1),
            ("optimized", 2),
        )
    ]
    assert not {
        "successor_epoch",
        "successor_roots",
        "successor_wait_exempt",
        "baseline_bucket_count",
        "post_bucket_count",
    }.intersection(profile)
    assert profile["minimum_post_activation_grace_s"] == 1
    assert profile["maximum_activation_to_successor_s"] == 10
    assert "activation_grace_s" not in profile
    assert hashlib.sha256(profile_bytes).hexdigest() == (
        validator.FROZEN_PROFILE_SHA256
    )


def test_recurring_profile_residency_covers_the_complete_predecessor_phase(
) -> None:
    profile = synthetic_run.recurring_profile()
    requests = validator._transition_requests(
        profile["transition_requests"],
        "transition_requests",
    )
    windows = validator._throughput_window_specs(
        profile["throughput_windows"],
        "throughput_windows",
    )

    validator._validate_transition_residencies(
        requests,
        windows,
        bucket_width_ns=5_000_000_000,
        post_activation_grace_ns=1_000_000_000,
    )

    mutated = synthetic_run.recurring_transition_requests()
    mutated[1]["minimum_predecessor_residency_ms"] = 35_999
    with pytest.raises(validator.ValidationError, match="complete throughput window"):
        validator._validate_transition_residencies(
            validator._transition_requests(mutated, "transition_requests"),
            windows,
            bucket_width_ns=5_000_000_000,
            post_activation_grace_ns=1_000_000_000,
        )


@pytest.mark.parametrize(
    "residency_ms",
    (True, -1, validator.MAXIMUM_PREDECESSOR_RESIDENCY_MS + 1),
)
def test_validator_transition_residency_has_strict_integer_bounds(
    residency_ms: object,
) -> None:
    requests = synthetic_run.recurring_transition_requests()
    requests[1]["minimum_predecessor_residency_ms"] = residency_ms

    with pytest.raises(
        validator.ValidationError,
        match="minimum_predecessor_residency_ms",
    ):
        validator._transition_requests(requests, "transition_requests")


def test_frozen_profile_rejects_boolean_numeric_alias() -> None:
    profile_path = Path(validator.__file__).resolve().with_name("profile.json")
    profile = json.loads(profile_path.read_bytes())
    profile["maximum_activation_to_successor_s"] = True

    with pytest.raises(validator.ValidationError, match="exact frozen"):
        validator._decode_frozen_profile(
            json.dumps(profile).encode(),
            "mutated frozen profile",
        )


def test_missing_manager_reputation_is_incomplete_and_writes_no_plot_inputs(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"

    def remove_reputation(values: list[dict[str, object]]) -> None:
        values[:] = [
            value
            for value in values
            if value["event_type"] != "reputation.evidence_applied"
        ]

    _rewrite_jsonl(manager, remove_reputation)
    output = tmp_path / "validated"

    verdict = validator.validate_run(manifest, epochs, output)

    assert verdict["verdict"] == "INCOMPLETE"
    assert "reputation.evidence_applied" in verdict["reason"]
    assert {path.name for path in output.iterdir()} == {"validation.json"}


def test_manager_convergence_ready_is_required(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"

    def remove_ready(values: list[dict[str, object]]) -> None:
        values[:] = [
            value
            for value in values
            if value["event_type"] != "adaptive_v2_ready"
        ]

    _rewrite_jsonl(manager, remove_ready)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert "exactly one adaptive_v2_ready" in verdict["reason"]


def test_duplicate_manager_convergence_ready_fails(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"

    def duplicate_ready(values: list[dict[str, object]]) -> None:
        ready = next(
            value for value in values if value["event_type"] == "adaptive_v2_ready"
        )
        duplicate = json.loads(json.dumps(ready))
        duplicate["source_monotonic_ns"] = int(ready["source_monotonic_ns"]) + 1
        values.append(duplicate)
        values.sort(key=lambda value: int(value["source_monotonic_ns"]))

    _rewrite_jsonl(manager, duplicate_ready)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "duplicate adaptive_v2_ready" in verdict["reason"]


def test_manager_convergence_failure_cannot_pass(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"

    def append_failure(values: list[dict[str, object]]) -> None:
        ready = next(
            value for value in values if value["event_type"] == "adaptive_v2_ready"
        )
        failure = json.loads(json.dumps(ready))
        failure["event_type"] = "adaptive_v2_convergence_failure"
        failure["source_monotonic_ns"] = int(ready["source_monotonic_ns"]) - 1
        failure["payload"]["failure_reason"] = "synthetic_failure"
        values.append(failure)
        values.sort(key=lambda value: int(value["source_monotonic_ns"]))

    _rewrite_jsonl(manager, append_failure)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "adaptive_v2_convergence_failure" in verdict["reason"]


def test_manager_ready_must_bind_exact_command_and_quorum(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"

    def change_ready(values: list[dict[str, object]]) -> None:
        ready = next(
            value for value in values if value["event_type"] == "adaptive_v2_ready"
        )
        ready["payload"]["accepted_activation_count"] = 4
        ready["payload"]["identity"]["command_block_hash"] = "f" * 64

    _rewrite_jsonl(manager, change_ready)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "adaptive_v2_ready" in verdict["reason"]


def test_manager_may_stop_only_after_convergence_ready(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"

    def move_ready_after_stop(values: list[dict[str, object]]) -> None:
        ready = next(
            value for value in values if value["event_type"] == "adaptive_v2_ready"
        )
        ready["source_monotonic_ns"] = synthetic_run.MANAGER_READY_NS + 3_000_000
        values.sort(key=lambda value: int(value["source_monotonic_ns"]))

    _rewrite_jsonl(manager, move_ready_after_stop)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "manager lifecycle is not ordered after convergence readiness" in (
        verdict["reason"]
    )


def test_manager_stopping_must_follow_ready_by_source_sequence(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"

    def reorder_equal_timestamp_lifecycle(
        values: list[dict[str, object]],
    ) -> None:
        ready_index = next(
            index
            for index, value in enumerate(values)
            if value["event_type"] == "adaptive_v2_ready"
        )
        stopping_index = next(
            index
            for index, value in enumerate(values)
            if value["event_type"] == "process.stopping"
        )
        stopping = values.pop(stopping_index)
        stopping["source_monotonic_ns"] = values[ready_index][
            "source_monotonic_ns"
        ]
        values.insert(ready_index, stopping)

    _rewrite_jsonl(manager, reorder_equal_timestamp_lifecycle)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "manager lifecycle is not ordered after convergence readiness" in (
        verdict["reason"]
    )


def test_manager_ready_must_follow_every_survivor_activation(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    survivor = tmp_path / "run/raw/replica-6.jsonl"

    def move_activation_after_ready(values: list[dict[str, object]]) -> None:
        activation = next(
            value for value in values if value["event_type"] == "epoch.activated"
        )
        activation["source_monotonic_ns"] = synthetic_run.MANAGER_READY_NS + 1
        values.sort(key=lambda value: int(value["source_monotonic_ns"]))

    _rewrite_jsonl(survivor, move_activation_after_ready)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "latest survivor activation <= manager readiness" in verdict["reason"]


def test_surviving_replica_still_cannot_stop_before_measurement_end(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    survivor = tmp_path / "run/raw/replica-2.jsonl"

    def stop_early(values: list[dict[str, object]]) -> None:
        stopping = next(
            value for value in values if value["event_type"] == "process.stopping"
        )
        stopped = next(
            value for value in values if value["event_type"] == "process.stopped"
        )
        stopping["source_monotonic_ns"] = synthetic_run.END_NS - 2
        stopped["source_monotonic_ns"] = synthetic_run.END_NS - 1
        values.sort(key=lambda value: int(value["source_monotonic_ns"]))

    _rewrite_jsonl(survivor, stop_early)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "replica-2 stopped before measurement end" in verdict["reason"]


def test_legacy_prefixed_events_are_diagnostic_only_and_fail_full_validation(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    observer = tmp_path / "run/raw/replica-2.jsonl"
    observer.write_text(
        "\n".join(
            "KAURI_EVENT " + line
            for line in observer.read_text(encoding="utf-8").splitlines()
        )
        + "\n",
        encoding="utf-8",
    )

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "legacy KAURI_EVENT prefix" in verdict["reason"]


def test_dirty_or_incomplete_run_cannot_pass(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "dirty")
    value = synthetic_run.load(manifest)
    value["kauri_worktree_clean"] = False
    synthetic_run.save(manifest, value)

    dirty = validator.validate_run(manifest, epochs, tmp_path / "dirty-output")

    assert dirty["verdict"] == "FAIL"
    assert "worktree_clean" in dirty["reason"]

    manifest, epochs = synthetic_run.create_run(tmp_path / "incomplete")
    value = synthetic_run.load(manifest)
    value["run_completion"]["complete"] = False
    synthetic_run.save(manifest, value)

    incomplete = validator.validate_run(
        manifest, epochs, tmp_path / "incomplete-output"
    )

    assert incomplete["verdict"] == "INCOMPLETE"
    assert "complete" in incomplete["reason"]


def test_successor_requires_failed_replicas_as_wait_exempt_physical_leaves(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    value = synthetic_run.load(epochs)
    tree = value["epochs"][1]["trees"][0]
    tree["members_breadth_first"] = [2, 0, 3, 4, 5, 6, 1]
    synthetic_run.save(epochs, value)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "not a physical leaf" in verdict["reason"]


def test_crash_requires_full_initial_root_cycle_common_to_all_seven(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    replica_zero = tmp_path / "run/raw/replica-0.jsonl"

    def contradict_root_three(values: list[dict[str, object]]) -> None:
        witness = next(
            value
            for value in values
            if value["event_type"] == "block.commit_observed"
            and value["payload"]["block_height"] == 4
        )
        witness["payload"]["block_hash"] = "f" * 64
        rich = next(
            value
            for value in values
            if value["event_type"] == "block.committed"
            and value["payload"]["block_height"] == 4
        )
        rich["payload"]["block_hash"] = "f" * 64
        rich["payload"]["decision_proof"]["block_hash"] = "f" * 64

    _rewrite_jsonl(replica_zero, contradict_root_three)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "root cycle 0..6" in verdict["reason"]


def test_crash_boundary_requires_exact_common_epoch_zero_root_six(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    value = synthetic_run.load(manifest)
    value["crash_configuration_boundary"]["tree_id"] = 0
    synthetic_run.save(manifest, value)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "root 6" in verdict["reason"]


def test_crash_boundary_rejects_intervening_configuration_activation(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    value = synthetic_run.load(manifest)
    evidence = next(
        item
        for item in value["crash_configuration_boundary"]["replica_evidence"]
        if item["source_id"] == "replica-2"
    )
    observer = tmp_path / "run/raw/replica-2.jsonl"

    def insert_root_zero(values: list[dict[str, object]]) -> None:
        boundary_index = next(
            index
            for index, item in enumerate(values)
            if item["source_sequence"] == evidence["source_sequence"]
        )
        event = json.loads(json.dumps(values[boundary_index]))
        event["source_monotonic_ns"] = synthetic_run.CRASH_0_NS - 1_000_000
        event["payload"]["tree_id"] = 0
        values.insert(boundary_index + 1, event)

    _rewrite_jsonl(observer, insert_root_zero)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "intervening configuration activation" in verdict["reason"]


def test_crash_boundary_rejects_boolean_integer_aliases(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    value = synthetic_run.load(manifest)
    reference = value["crash_configuration_boundary"]["replica_evidence"][0]
    replica_zero = tmp_path / "run/raw/replica-0.jsonl"

    def replace_epoch_with_false(values: list[dict[str, object]]) -> None:
        event = next(
            item
            for item in values
            if item["source_sequence"] == reference["source_sequence"]
        )
        event["payload"]["epoch_number"] = False

    _rewrite_jsonl(replica_zero, replace_epoch_with_false)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "epoch_number must be an integer" in verdict["reason"]


def test_crash_boundary_audits_authoritative_sequence_not_global_timestamp(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    value = synthetic_run.load(manifest)
    reference = value["crash_configuration_boundary"]["replica_evidence"][2]
    observer = tmp_path / "run/raw/replica-2.jsonl"

    def insert_root_change(values: list[dict[str, object]]) -> None:
        boundary_index = next(
            index
            for index, item in enumerate(values)
            if item["source_sequence"] == reference["source_sequence"]
        )
        event = json.loads(
            json.dumps(
                next(
                    item
                    for item in values
                    if item["event_type"] == "block.committed"
                    and item["payload"]["decision_proof"]["tree_id"] == 6
                )
            )
        )
        event["source_monotonic_ns"] = synthetic_run.CRASH_0_NS - 996_000_000
        event["payload"]["decision_proof"]["tree_id"] = 0
        values.insert(boundary_index + 1, event)

    _rewrite_jsonl(observer, insert_root_change)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "authoritative root changed" in verdict["reason"]


def test_conflicting_survivor_commit_witness_fails(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    survivor = tmp_path / "run/raw/replica-6.jsonl"

    def conflict(values: list[dict[str, object]]) -> None:
        witness = next(
            value
            for value in values
            if value["event_type"] == "block.commit_observed"
        )
        witness["payload"]["block_hash"] = "f" * 64
        rich = next(
            value
            for value in values
            if value["event_type"] == "block.committed"
            and value["payload"]["block_height"]
            == witness["payload"]["block_height"]
        )
        rich["payload"]["block_hash"] = "f" * 64
        rich["payload"]["decision_proof"]["block_hash"] = "f" * 64

    _rewrite_jsonl(survivor, conflict)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "disagreement at height 1" in verdict["reason"]


def test_nonobserver_cannot_claim_designated_commit_stream(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    survivor = tmp_path / "run/raw/replica-6.jsonl"

    def mark_designated(values: list[dict[str, object]]) -> None:
        commit = next(
            value for value in values if value["event_type"] == "block.committed"
        )
        commit["payload"]["designated_observer"] = True

    _rewrite_jsonl(survivor, mark_designated)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "designated-observer flag is inconsistent" in verdict["reason"]


def test_rich_commit_must_match_same_replica_witness(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    survivor = tmp_path / "run/raw/replica-5.jsonl"

    def contradict_witness(values: list[dict[str, object]]) -> None:
        commit = next(
            value for value in values if value["event_type"] == "block.committed"
        )
        commit["payload"]["block_hash"] = "f" * 64
        commit["payload"]["decision_proof"]["block_hash"] = "f" * 64

    _rewrite_jsonl(survivor, contradict_witness)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "rich commit disagrees with commit witness at height 1" in verdict["reason"]


def test_rich_commit_transaction_count_must_match_witness(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    observer = tmp_path / "run/raw/replica-2.jsonl"

    def change_transaction_count(values: list[dict[str, object]]) -> None:
        commit = next(
            value for value in values if value["event_type"] == "block.committed"
        )
        commit["payload"]["transaction_count"] = 101

    _rewrite_jsonl(observer, change_transaction_count)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "rich commit disagrees with commit witness at height 1" in verdict["reason"]


def test_commit_witness_must_precede_rich_event(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    survivor = tmp_path / "run/raw/replica-5.jsonl"

    def move_witness_after_rich(values: list[dict[str, object]]) -> None:
        witness_index = next(
            index
            for index, value in enumerate(values)
            if value["event_type"] == "block.commit_observed"
        )
        rich_index = next(
            index
            for index, value in enumerate(values)
            if value["event_type"] == "block.committed"
            and value["payload"]["block_height"]
            == values[witness_index]["payload"]["block_height"]
        )
        values[witness_index], values[rich_index] = (
            values[rich_index],
            values[witness_index],
        )

    _rewrite_jsonl(survivor, move_witness_after_rich)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "commit witness does not precede rich commit at height 1" in verdict["reason"]


def test_nonobserver_rich_commit_gap_is_covered_by_witness(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    survivor = tmp_path / "run/raw/replica-4.jsonl"

    def remove_rich_commit(values: list[dict[str, object]]) -> None:
        values[:] = [
            value
            for value in values
            if not (
                value["event_type"] == "block.committed"
                and value["payload"]["block_height"] == 10
            )
        ]

    _rewrite_jsonl(survivor, remove_rich_commit)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "PASS"


def test_authoritative_witness_without_rich_commit_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    observer = tmp_path / "run/raw/replica-2.jsonl"

    def remove_rich_commit(values: list[dict[str, object]]) -> None:
        values[:] = [
            value
            for value in values
            if not (
                value["event_type"] == "block.committed"
                and value["payload"]["block_height"] == 10
            )
        ]

    _rewrite_jsonl(observer, remove_rich_commit)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert "replica-2 commit witness at height 10 has no rich commit" in verdict[
        "reason"
    ]


def test_missing_survivor_commit_witness_is_incomplete(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    survivor = tmp_path / "run/raw/replica-4.jsonl"

    def remove_witness(values: list[dict[str, object]]) -> None:
        values[:] = [
            value
            for value in values
            if not (
                value["event_type"] == "block.commit_observed"
                and value["payload"]["block_height"] == 10
            )
        ]

    _rewrite_jsonl(survivor, remove_witness)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert "replica-4 rich commit at height 10 has no commit witness" in verdict["reason"]


def test_commit_witness_requires_exact_payload_fields(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    survivor = tmp_path / "run/raw/replica-5.jsonl"

    def remove_transaction_count(values: list[dict[str, object]]) -> None:
        witness = next(
            value
            for value in values
            if value["event_type"] == "block.commit_observed"
        )
        del witness["payload"]["transaction_count"]

    _rewrite_jsonl(survivor, remove_transaction_count)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "block.commit_observed payload fields are invalid" in verdict["reason"]
    assert "transaction_count" in verdict["reason"]


def test_terminal_verdict_is_immutable(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"
    manager.write_text("", encoding="utf-8")
    output = tmp_path / "validated"
    first = validator.validate_run(manifest, epochs, output)
    assert first["verdict"] == "INCOMPLETE"

    synthetic_run.create_run(tmp_path / "replacement")
    with pytest.raises(validator.ValidationError, match="preserve the run"):
        validator.validate_run(manifest, epochs, output)


def test_profile_content_is_hash_bound(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    (tmp_path / "run/profile.json").write_text("{}\n", encoding="utf-8")

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "profile sha256" in verdict["reason"]


@pytest.mark.parametrize(
    ("field", "replacement"),
    (("schema_version", 2), ("frozen", False), ("bucket_width_s", 4)),
)
def test_manifest_updated_self_hash_cannot_replace_frozen_profile(
    tmp_path: Path, field: str, replacement: object
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    profile_path = tmp_path / "run/profile.json"
    profile = synthetic_run.load(profile_path)
    profile[field] = replacement
    profile_bytes = (json.dumps(profile, sort_keys=True) + "\n").encode()
    profile_path.write_bytes(profile_bytes)
    manifest_value = synthetic_run.load(manifest)
    manifest_value["profile"]["sha256"] = hashlib.sha256(profile_bytes).hexdigest()
    synthetic_run.save(manifest, manifest_value)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "canonical frozen profile SHA-256" in verdict["reason"]


def test_pass_artifact_retains_pinned_repository_profile_sha(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "PASS"
    assert verdict["artifacts"]["profile"]["sha256"] == (
        validator.LEGACY_FROZEN_PROFILE_SHA256
    )


def test_missing_runtime_artifact_is_incomplete(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    (tmp_path / "run/runtime/replica-4.effective.json").unlink()

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert "missing runtime artifact" in verdict["reason"]


def test_manifest_runtime_must_equal_frozen_profile(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    value = synthetic_run.load(manifest)
    value["runtime"]["aggregation_timeout_ms"] = 750
    synthetic_run.save(manifest, value)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "exact frozen effective runtime" in verdict["reason"]


def test_manifest_runtime_must_bind_manager_capacity_envelope(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    value = synthetic_run.load(manifest)
    value["runtime"]["manager_limits"][
        "evidence_maximum_accepted_records"
    ] = 512
    synthetic_run.save(manifest, value)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "exact frozen effective runtime" in verdict["reason"]


def test_runtime_artifact_bytes_are_sha_bound(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    artifact = tmp_path / "run/runtime/replica-3.effective.json"
    artifact.write_text("{}\n", encoding="utf-8")

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "runtime artifact SHA-256 mismatch" in verdict["reason"]


def test_self_consistent_declared_hash_cannot_hide_main_config_mismatch(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    launch_path = tmp_path / "run/runtime/launch-arguments.json"
    launch = synthetic_run.load(launch_path)
    main_config_path = Path(launch["processes"][0]["argv"][2])
    main_config_path.write_text(
        main_config_path.read_text(encoding="utf-8").replace(
            "block-size = 1", "block-size = 2"
        ),
        encoding="utf-8",
    )
    changed_sha = hashlib.sha256(main_config_path.read_bytes()).hexdigest()
    for process in launch["processes"][:7]:
        process["effective_options"]["main_config_sha256"] = changed_sha
    synthetic_run.save(launch_path, launch)
    _rehash_runtime_artifact(manifest, "runtime/launch-arguments.json")

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "effective main config block-size differs" in verdict["reason"]


def test_launch_artifact_must_not_persist_private_manager_values(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    launch_path = tmp_path / "run/runtime/launch-arguments.json"
    launch = synthetic_run.load(launch_path)
    argv = launch["processes"][-1]["argv"]
    argv[argv.index("--issuer-private-key") + 1] = "deadbeef"
    synthetic_run.save(launch_path, launch)
    _rehash_runtime_artifact(manifest, "runtime/launch-arguments.json")

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "--issuer-private-key must be redacted" in verdict["reason"]


@pytest.mark.parametrize("mutation", ("missing", "wrong"))
def test_launch_artifact_pins_manager_convergence_deadline(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    launch_path = tmp_path / "run/runtime/launch-arguments.json"
    launch = synthetic_run.load(launch_path)
    argv = launch["processes"][-1]["argv"]
    index = argv.index("--convergence-deadline-seconds")
    if mutation == "missing":
        del argv[index : index + 2]
    else:
        argv[index + 1] = "12"
    synthetic_run.save(launch_path, launch)
    _rehash_runtime_artifact(manifest, "runtime/launch-arguments.json")

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "--convergence-deadline-seconds" in verdict["reason"]


def test_launch_artifact_rejects_unapproved_manager_flags(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_recurring_run(tmp_path / "run")
    launch_path = tmp_path / "run/runtime/launch-arguments.json"
    launch = synthetic_run.load(launch_path)
    launch["processes"][-1]["argv"].extend(
        ("--experiment-drop-bundle-attempt", "2:1")
    )
    synthetic_run.save(launch_path, launch)
    _rehash_runtime_artifact(manifest, "runtime/launch-arguments.json")

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "unsupported flag" in verdict["reason"]


def test_launched_executable_bytes_are_sha_bound(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    value = synthetic_run.load(manifest)
    app_path = Path(value["runtime"]["executables"]["hotstuff_app"]["path"])
    app_path.write_bytes(app_path.read_bytes() + b"tampered\n")

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "runtime executable SHA-256 mismatch" in verdict["reason"]


@pytest.mark.parametrize(
    ("field", "replacement", "phase"),
    (
        ("baseline_start_ns", 30_000_000_000, "baseline"),
        ("end_ns", 82_000_000_000, "post"),
    ),
)
def test_pass_requires_seven_complete_raw_buckets_in_baseline_and_post(
    tmp_path: Path, field: str, replacement: int, phase: str
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    value = synthetic_run.load(manifest)
    value[field] = replacement
    synthetic_run.save(manifest, value)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert f"{phase} phase" in verdict["reason"]
    assert "complete raw buckets" in verdict["reason"]
    assert "requires at least 7" in verdict["reason"]


def test_long_degraded_commit_stall_cannot_pass(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")

    def remove_degraded_progress(values: list[dict[str, object]]) -> None:
        values[:] = [
            value
            for value in values
            if not (
                value["event_type"] == "block.committed"
                and 8 <= value["payload"]["block_height"] <= 15
            )
        ]

    for replica in range(2, 7):
        _rewrite_jsonl(
            tmp_path / f"run/raw/replica-{replica}.jsonl",
            remove_degraded_progress,
        )

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "degraded authoritative commit stall" in verdict["reason"]
    assert "frozen maximum is 25s" in verdict["reason"]


def test_existing_canonical_output_is_never_overwritten(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    output = tmp_path / "validated"
    output.mkdir()
    preserved = output / "throughput.csv"
    preserved.write_text("user-owned\n", encoding="utf-8")

    with pytest.raises(validator.ValidationError, match="canonical output already exists"):
        validator.validate_run(manifest, epochs, output)

    assert preserved.read_text(encoding="utf-8") == "user-owned\n"
    assert not (output / "validation.json").exists()


def test_precommand_guard_requires_three_qualifying_reporters(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"

    def collapse_to_two(values: list[dict[str, object]]) -> None:
        for value in values:
            payload = value["payload"]
            if (
                value["event_type"] == "reputation.evidence_applied"
                and payload["target_id"] == 0
                and payload["evidence_outcome"] == "timeout"
                and payload["reporter_id"] == 4
            ):
                payload["reporter_id"] = 3

    _rewrite_jsonl(manager, collapse_to_two)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert "3 distinct reporters" in verdict["reason"]


def test_precommand_guard_requires_two_observations_per_qualifying_reporter(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"

    def leave_one_attempt(values: list[dict[str, object]]) -> None:
        changed = False
        for value in values:
            payload = value["payload"]
            if (
                not changed
                and value["event_type"] == "reputation.evidence_applied"
                and payload["target_id"] == 0
                and payload["evidence_outcome"] == "timeout"
                and payload["reporter_id"] == 2
            ):
                payload["reporter_id"] = 4
                changed = True

    _rewrite_jsonl(manager, leave_one_attempt)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert "at least 2 observations each" in verdict["reason"]


def _append_reputation_event(
    manager: Path,
    *,
    outcome: str,
    reporter: int,
    target: int,
    resulting_score: int,
) -> None:
    values = [
        json.loads(line)
        for line in manager.read_text(encoding="utf-8").splitlines()
        if line
    ]
    reputation = [
        value
        for value in values
        if value["event_type"] == "reputation.evidence_applied"
    ]
    ingestion = max(
        int(value["payload"]["ingestion_sequence"]) for value in reputation
    ) + 1
    delta = -1 if outcome == "timeout" else 1
    if outcome == "late":
        timeout = next(
            value
            for value in reversed(reputation)
            if value["payload"]["evidence_outcome"] == "timeout"
            and value["payload"]["reporter_id"] == reporter
            and value["payload"]["target_id"] == target
        )
        observation_id = timeout["payload"]["observation_id"]
    else:
        observation_id = f"{ingestion + 2000:064x}"
    values.append(
        {
            "event_schema_version": 1,
            "run_id": synthetic_run.RUN_ID,
            "source_kind": "adaptation_manager",
            "source_id": "adaptive-manager",
            "source_instance": "synthetic-manager-instance",
            "source_sequence": 0,
            "source_monotonic_ns": 49_000_000_000,
            "event_type": "reputation.evidence_applied",
            "payload": {
                "evidence_cutoff": ingestion,
                "ingestion_sequence": ingestion,
                "observation_id": observation_id,
                "reporter_id": reporter,
                "target_id": target,
                "evidence_outcome": outcome,
                "reputation_outcome": (
                    "timeout" if outcome == "timeout" else "response"
                ),
                "delta": delta,
                "resulting_score": resulting_score,
            },
        }
    )
    values.sort(key=lambda value: int(value["source_monotonic_ns"]))
    for sequence, value in enumerate(values, start=1):
        value["source_sequence"] = sequence
    manager.write_text(
        "\n".join(json.dumps(value, separators=(",", ":")) for value in values)
        + "\n",
        encoding="utf-8",
    )


def test_extra_partial_reporter_does_not_invalidate_three_by_two_guard(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"
    _append_reputation_event(
        manager,
        outcome="timeout",
        reporter=5,
        target=0,
        resulting_score=-6,
    )

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "PASS"


def test_net_drop_accounts_for_late_compensation(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"
    _append_reputation_event(
        manager,
        outcome="timeout",
        reporter=5,
        target=0,
        resulting_score=-6,
    )
    _append_reputation_event(
        manager,
        outcome="late",
        reporter=5,
        target=0,
        resulting_score=-5,
    )
    _append_reputation_event(
        manager,
        outcome="on_time",
        reporter=6,
        target=0,
        resulting_score=-4,
    )

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "net reputation drop" in verdict["reason"]


def test_late_cancels_its_timeout_id_from_reporter_guard(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"
    _append_reputation_event(
        manager,
        outcome="late",
        reporter=2,
        target=0,
        resulting_score=-4,
    )

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert "3 distinct reporters" in verdict["reason"]


def test_rejects_standalone_late_without_matching_timeout(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"
    _append_reputation_event(
        manager,
        outcome="on_time",
        reporter=5,
        target=0,
        resulting_score=-4,
    )

    def turn_new_response_into_standalone_late(
        values: list[dict[str, object]],
    ) -> None:
        event = next(
            value
            for value in reversed(values)
            if value["event_type"] == "reputation.evidence_applied"
            and value["payload"]["reporter_id"] == 5
            and value["payload"]["target_id"] == 0
        )
        event["payload"]["evidence_outcome"] = "late"

    _rewrite_jsonl(manager, turn_new_response_into_standalone_late)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "standalone late" in verdict["reason"]


def test_responsive_baseline_requires_on_time_evidence_for_every_replica(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"

    def remove_replica_six_on_time(values: list[dict[str, object]]) -> None:
        for value in values:
            payload = value["payload"]
            if (
                value["event_type"] == "reputation.evidence_applied"
                and payload["target_id"] == 6
                and value["source_monotonic_ns"] < synthetic_run.CRASH_0_NS
            ):
                payload["evidence_outcome"] = "timeout"
                payload["reputation_outcome"] = "timeout"
                payload["delta"] = -1
                payload["resulting_score"] = -1
            elif (
                value["event_type"] == "reputation.evidence_applied"
                and payload["target_id"] == 6
            ):
                payload["resulting_score"] = 0

    _rewrite_jsonl(manager, remove_replica_six_on_time)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert "responsive baseline" in verdict["reason"]
    assert "6" in verdict["reason"]


def test_timeout_to_late_reuses_one_production_observation_identity(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"
    _append_reputation_event(
        manager,
        outcome="timeout",
        reporter=5,
        target=0,
        resulting_score=-6,
    )
    _append_reputation_event(
        manager,
        outcome="late",
        reporter=5,
        target=0,
        resulting_score=-5,
    )

    values = [
        json.loads(line)
        for line in manager.read_text(encoding="utf-8").splitlines()
        if line
    ]
    correlated = [
        value
        for value in values
        if value["event_type"] == "reputation.evidence_applied"
        and value["payload"]["reporter_id"] == 5
        and value["payload"]["target_id"] == 0
    ]
    assert [value["payload"]["evidence_outcome"] for value in correlated] == [
        "timeout",
        "late",
    ]
    assert len({value["payload"]["observation_id"] for value in correlated}) == 1

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "PASS"


def test_rejects_a_second_late_transition_for_one_observation(tmp_path: Path) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"
    _append_reputation_event(
        manager,
        outcome="timeout",
        reporter=5,
        target=0,
        resulting_score=-6,
    )
    _append_reputation_event(
        manager,
        outcome="late",
        reporter=5,
        target=0,
        resulting_score=-5,
    )
    _append_reputation_event(
        manager,
        outcome="late",
        reporter=5,
        target=0,
        resulting_score=-4,
    )

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "ordered timeout-to-late" in verdict["reason"]


def test_replica_one_precrash_timeout_does_not_count_for_its_guard(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"

    def move_first_pair_before_replica_one_crash(
        values: list[dict[str, object]],
    ) -> None:
        first_zero = next(
            value
            for value in values
            if value["event_type"] == "reputation.evidence_applied"
            and value["payload"]["target_id"] == 0
            and value["payload"]["evidence_outcome"] == "timeout"
        )
        first_one = next(
            value
            for value in values
            if value["event_type"] == "reputation.evidence_applied"
            and value["payload"]["target_id"] == 1
            and value["payload"]["evidence_outcome"] == "timeout"
        )
        first_zero["source_monotonic_ns"] = 36_020_000_000
        first_one["source_monotonic_ns"] = 36_050_000_000

    _rewrite_jsonl(manager, move_first_pair_before_replica_one_crash)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert "replica 1" in verdict["reason"]
    assert "3 distinct reporters" in verdict["reason"]


def test_rejects_any_crashed_replica_event_after_confirmed_exit(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    replica_zero = tmp_path / "run/raw/replica-0.jsonl"

    def append_impossible_event(values: list[dict[str, object]]) -> None:
        values.append(
            {
                "event_schema_version": 1,
                "run_id": synthetic_run.RUN_ID,
                "source_kind": "replica",
                "source_id": "replica-0",
                "source_instance": "synthetic-replica-0-instance",
                "source_sequence": 0,
                "source_monotonic_ns": 37_000_000_000,
                "event_type": "epoch.generated",
                "payload": {
                    "epoch_number": 0,
                    "tree_id": 6,
                    "epoch_digest": synthetic_run.EPOCH_0_DIGEST,
                    "activation_height": 0,
                },
            }
        )

    _rewrite_jsonl(replica_zero, append_impossible_event)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "emitted after its confirmed SIGKILL exit" in verdict["reason"]


def test_empty_successor_members_produces_terminal_fail_not_exception(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    value = synthetic_run.load(epochs)
    value["epochs"][1]["trees"][0]["members_breadth_first"] = []
    synthetic_run.save(epochs, value)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "all seven replicas" in verdict["reason"]


def test_prebaseline_on_time_evidence_does_not_satisfy_responsive_baseline(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    manager = tmp_path / "run/raw/adaptive-manager.jsonl"

    def move_target_six_prebaseline(values: list[dict[str, object]]) -> None:
        target = next(
            value
            for value in values
            if value["event_type"] == "reputation.evidence_applied"
            and value["payload"]["target_id"] == 6
            and value["source_monotonic_ns"] < synthetic_run.CRASH_0_NS
        )
        target["source_monotonic_ns"] = 900_000_000
        values.sort(key=lambda value: int(value["source_monotonic_ns"]))
        ingestion = 0
        for value in values:
            if value["event_type"] == "reputation.evidence_applied":
                ingestion += 1
                value["payload"]["ingestion_sequence"] = ingestion
                value["payload"]["evidence_cutoff"] = ingestion

    _rewrite_jsonl(manager, move_target_six_prebaseline)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "INCOMPLETE"
    assert "responsive baseline" in verdict["reason"]
    assert "6" in verdict["reason"]


def test_crash_pid_and_pgid_are_bound_to_replica_source_process(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    value = synthetic_run.load(manifest)
    marker = value["crash_markers"][0]
    marker["pid"] = 99_999
    marker["confirmed_exit"]["pid"] = 99_999
    synthetic_run.save(manifest, value)

    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "does not match its source process" in verdict["reason"]


def test_command_event_is_joined_to_authoritative_consensus_block(
    tmp_path: Path,
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    observer = tmp_path / "run/raw/replica-2.jsonl"

    def change_command_block(values: list[dict[str, object]]) -> None:
        command_commit = next(
            value
            for value in values
            if value["event_type"] == "block.committed"
            and value["payload"]["block_height"] == 12
        )
        command_commit["payload"]["block_hash"] = "e" * 64
        command_commit["payload"]["decision_proof"]["block_hash"] = "e" * 64

    _rewrite_jsonl(observer, change_command_block)
    verdict = validator.validate_run(manifest, epochs, tmp_path / "validated")

    assert verdict["verdict"] == "FAIL"
    assert "command hash" in verdict["reason"]


def test_concurrent_validators_cannot_replace_one_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, epochs = synthetic_run.create_run(tmp_path / "run")
    output = tmp_path / "validated"
    entered = threading.Event()
    release = threading.Event()
    original = validator.evaluate

    def paused_evaluate(*args: object) -> object:
        entered.set()
        assert release.wait(timeout=10)
        return original(*args)

    monkeypatch.setattr(validator, "evaluate", paused_evaluate)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(validator.validate_run, manifest, epochs, output)
        assert entered.wait(timeout=10)
        with pytest.raises(validator.ValidationError, match="already claimed"):
            validator.validate_run(manifest, epochs, output)
        release.set()
        assert first.result(timeout=20)["verdict"] == "PASS"

    assert not (output / validator.VALIDATION_CLAIM).exists()
