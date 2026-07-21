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


def test_repository_profile_has_exact_v2_recovery_fields() -> None:
    profile_path = Path(validator.__file__).resolve().with_name("profile.json")
    profile_bytes = profile_path.read_bytes()
    profile = json.loads(profile_bytes)

    assert profile["profile_id"] == "n7-f2-q5-crash-recovery-v2"
    assert profile["minimum_post_activation_grace_s"] == 1
    assert profile["maximum_activation_to_successor_s"] == 10
    assert "activation_grace_s" not in profile
    assert hashlib.sha256(profile_bytes).hexdigest() == (
        validator.FROZEN_PROFILE_SHA256
    )


def test_frozen_profile_rejects_boolean_numeric_alias() -> None:
    profile_path = Path(validator.__file__).resolve().with_name("profile.json")
    profile = json.loads(profile_path.read_bytes())
    profile["maximum_activation_to_successor_s"] = True

    with pytest.raises(validator.ValidationError, match="exact frozen v2"):
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
        validator.FROZEN_PROFILE_SHA256
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
