"""Synthetic convergence-only validation; generated runs are never evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Callable

import pytest

import convergence_synthetic_run as convergence_run


SCENARIO_DIRECTORY = Path(__file__).resolve().parents[1]


def _validator() -> Any:
    path = SCENARIO_DIRECTORY / "convergence_validator.py"
    assert path.is_file(), "missing convergence-only validator"
    spec = importlib.util.spec_from_file_location(
        "n7_convergence_validator", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _validate(tmp_path: Path) -> tuple[dict[str, Any], Path, Path, Path]:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")
    output = tmp_path / "validated"
    verdict = _validator().validate_run(manifest, epochs, output)
    return verdict, manifest, epochs, output


def _manager_mutation(
    manifest: Path, mutate: Callable[[list[dict[str, Any]]], None]
) -> None:
    convergence_run.rewrite_events(manifest, "adaptive-manager", mutate)


def _stream_events(manifest: Path, source_id: str) -> list[dict[str, Any]]:
    document = convergence_run.load(manifest)
    source = next(
        item for item in document["sources"] if item["source_id"] == source_id
    )
    return [
        json.loads(line)
        for line in (manifest.parent / source["path"])
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]


def _without_terminal_events(values: list[dict[str, Any]]) -> None:
    values[:] = [
        event
        for event in values
        if event["event_type"]
        not in ("adaptive_v2_converged", "adaptive_v2_ready")
    ]


def test_complete_exact_n7_f2_q5_convergence_run_passes_only_canonical_inputs(
    tmp_path: Path,
) -> None:
    verdict, _, _, output = _validate(tmp_path)

    assert verdict["verdict"] == "PASS"
    assert verdict["profile_identity"] == convergence_run.PROFILE_ID
    assert verdict["replica_count"] == 7
    assert verdict["fault_threshold"] == 2
    assert verdict["quorum"] == 5
    assert verdict["winning_identity"] == convergence_run.convergence_identity()
    assert verdict["activation_sources"] == convergence_run.SURVIVORS
    assert verdict["accepted_activation_count"] == 5
    assert verdict["converged_event_count"] == 1
    assert verdict["ready_event_count"] == 1
    assert verdict["bundle_retry"]["recipient"] == 2
    assert verdict["bundle_retry"]["byte_identical"] is True
    assert verdict["activation_ack_retry"]["replica_id"] == 6
    assert verdict["activation_ack_retry"]["byte_identical"] is True
    assert verdict["activation_ack_retry"]["acked_during_drain"] is True
    assert {path.name for path in output.iterdir()} == {
        "convergence.json",
        "manifest.json",
        "convergence-profile.json",
    }
    assert not any(
        token in path.name.lower()
        for path in output.rglob("*")
        for token in ("throughput", "tps", "figure", "plot")
    )


def test_manager_source_sequence_orders_equal_timestamp_ack_recovery(
    tmp_path: Path,
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "equal-timestamps")
    shared_timestamp_ns = 3_500_000_000

    def equalize_terminal_chain(values: list[dict[str, Any]]) -> None:
        for event in values:
            payload = event.get("payload", {})
            if event["event_type"] in {
                "adaptive_v2_converged",
                "adaptive_v2_ready",
            } or (
                event["event_type"] == "adaptive_v2_activation_observed"
                and payload.get("replica_id") == 6
                and payload.get("disposition")
                in {"accepted", "ack_injected_drop", "duplicate", "ack_sent"}
            ):
                event["source_monotonic_ns"] = shared_timestamp_ns

    _manager_mutation(manifest, equalize_terminal_chain)
    convergence_run.mutate_artifact(
        manifest,
        "runner_state",
        lambda state: state["loss_controls"]["activation_ack"].update(
            {"ack_source_monotonic_ns": shared_timestamp_ns}
        ),
    )
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated-equal-timestamps"
    )

    assert verdict["verdict"] == "PASS"


@pytest.mark.parametrize("mutation", ("missing", "wrong", "duplicate"))
def test_manager_deadline_control_must_be_exact_and_unique(
    tmp_path: Path, mutation: str
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / mutation)
    document = convergence_run.load(manifest)
    arguments = document["manager_argv"]
    option_index = arguments.index("--convergence-deadline-seconds")
    if mutation == "missing":
        del arguments[option_index : option_index + 2]
    elif mutation == "wrong":
        arguments[option_index + 1] = "119"
    else:
        arguments[option_index:option_index] = [
            "--convergence-deadline-seconds",
            "120",
        ]
    convergence_run.save(manifest, document)

    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{mutation}"
    )

    assert verdict["verdict"] == "FAIL"


def test_byte_frozen_profile_rejects_semantically_equivalent_reformat(
    tmp_path: Path,
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "reformatted")
    document = convergence_run.load(manifest)
    profile_artifact = next(
        item for item in document["artifacts"] if item["kind"] == "profile"
    )
    profile_path = manifest.parent / profile_artifact["path"]
    profile = convergence_run.load(profile_path)
    profile_path.write_text(json.dumps(profile, indent=1) + "\n", encoding="utf-8")
    digest = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    profile_artifact["sha256"] = digest
    document["profile"]["sha256"] = digest
    convergence_run.save(manifest, document)

    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated-reformatted"
    )

    assert verdict["verdict"] == "FAIL"


def test_synthetic_sigkill_streams_have_exact_runner_ground_truth(
    tmp_path: Path,
) -> None:
    manifest, _ = convergence_run.create_run(tmp_path / "run")
    document = convergence_run.load(manifest)

    assert document["crash_markers"] == convergence_run.crash_markers()
    assert document["crash_configuration_boundary"] == (
        convergence_run.crash_configuration_boundary()
    )
    for replica in convergence_run.CRASH_TARGETS:
        assert not any(
            event["event_type"] == "process.stopped"
            for event in _stream_events(manifest, f"replica-{replica}")
        )
    for source_id in (
        *(f"replica-{replica}" for replica in convergence_run.SURVIVORS),
        "adaptive-manager",
    ):
        stopped = [
            event
            for event in _stream_events(manifest, source_id)
            if event["event_type"] == "process.stopped"
        ]
        assert len(stopped) == 1
        assert stopped[0]["payload"]["exit_status"] is None


def test_synthetic_events_bind_exactly_to_manifest_run_and_revision(
    tmp_path: Path,
) -> None:
    manifest, _ = convergence_run.create_run(tmp_path / "run")
    document = convergence_run.load(manifest)

    assert document["run_id"] == convergence_run.RUN_ID
    assert document["kauri_revision"] == convergence_run.REVISION
    assert len(document["kauri_revision"]) == 40
    assert all(
        event["run_id"] == document["run_id"]
        for source in document["sources"]
        for event in _stream_events(manifest, source["source_id"])
    )


def test_pre_crash_tree_six_events_have_complete_canonical_fields(
    tmp_path: Path,
) -> None:
    manifest, _ = convergence_run.create_run(tmp_path / "run")
    canonical_fields = {
        "epoch_number",
        "tree_id",
        "epoch_digest",
        "block_hash",
        "context_generation",
        "observer_replica",
        "wait_exempt_signers",
        "accepted_signers",
        "absent_direct_children",
        "missing_optional_signers",
        "required_branch_gaps",
        "root_signer_count",
        "global_quorum",
        "rejection_reason",
    }

    for replica in convergence_run.MEMBERSHIP:
        boundary = next(
            event
            for event in _stream_events(manifest, f"replica-{replica}")
            if event["event_type"] == "adaptive.configuration_active"
        )
        assert set(boundary["payload"]) == canonical_fields
        assert boundary["payload"] == (
            convergence_run.synthetic_run._configuration_active_payload(replica)
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("block_hash", "f" * 64),
        ("rejection_reason", "unexpected_rejection"),
        ("wait_exempt_signers", [0]),
        ("accepted_signers", [0]),
        ("absent_direct_children", [0]),
        ("missing_optional_signers", [0]),
        ("required_branch_gaps", [{"child": 0, "missing_signers": [1]}]),
        ("root_signer_count", 1),
    ),
)
def test_pre_crash_tree_six_requires_exact_empty_boundary_semantics(
    tmp_path: Path, field: str, invalid_value: Any
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / field)

    def corrupt_boundary(values: list[dict[str, Any]]) -> None:
        event = next(
            value
            for value in values
            if value["event_type"] == "adaptive.configuration_active"
        )
        event["payload"][field] = json.loads(json.dumps(invalid_value))

    convergence_run.rewrite_events(manifest, "replica-3", corrupt_boundary)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{field}"
    )

    assert verdict["verdict"] == "FAIL"


def test_runner_state_crash_ground_truth_matches_manifest_exactly(
    tmp_path: Path,
) -> None:
    manifest, _ = convergence_run.create_run(tmp_path / "run")
    document = convergence_run.load(manifest)
    runner_state = next(
        item for item in document["artifacts"] if item["kind"] == "runner_state"
    )
    state = convergence_run.load(manifest.parent / runner_state["path"])

    assert state["crash_markers"] == document["crash_markers"]
    assert state["crash_configuration_boundary"] == (
        document["crash_configuration_boundary"]
    )


def test_manifest_sources_record_exact_process_pid_and_pgid(
    tmp_path: Path,
) -> None:
    manifest, _ = convergence_run.create_run(tmp_path / "run")
    document = convergence_run.load(manifest)
    sources = {source["source_id"]: source for source in document["sources"]}

    for marker in document["crash_markers"]:
        source = sources[f"replica-{marker['replica_id']}"]
        assert marker["pid"] == source["pid"]
        assert marker["pgid"] == source["pgid"]
    assert all(source["pid"] > 0 for source in sources.values())
    assert all(source["pgid"] > 0 for source in sources.values())


@pytest.mark.parametrize("replica", convergence_run.CRASH_TARGETS)
@pytest.mark.parametrize("field", ("pid", "pgid"))
def test_crash_marker_process_identity_must_match_manifest_source(
    tmp_path: Path, field: str, replica: int
) -> None:
    manifest, epochs = convergence_run.create_run(
        tmp_path / f"{field}-{replica}"
    )
    document = convergence_run.load(manifest)
    marker = next(
        value
        for value in document["crash_markers"]
        if value["replica_id"] == replica
    )
    marker[field] += 1_000
    marker["confirmed_exit"][field] = marker[field]
    convergence_run.save(manifest, document)

    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{field}-{replica}"
    )

    assert verdict["verdict"] == "FAIL"
    assert field in verdict["reason"].lower()
    assert "source" in verdict["reason"].lower()


@pytest.mark.parametrize("record", ("crash_markers", "crash_configuration_boundary"))
def test_runner_state_crash_ground_truth_mismatch_fails(
    tmp_path: Path, record: str
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / record)

    def mismatch_runner_state(state: dict[str, Any]) -> None:
        if record == "crash_markers":
            state[record][0]["requested_monotonic_raw_ns"] += 1
        else:
            state[record]["tree_id"] = 5

    convergence_run.mutate_artifact(
        manifest, "runner_state", mismatch_runner_state
    )
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{record}"
    )

    assert verdict["verdict"] == "FAIL"
    assert "runner state" in verdict["reason"].lower()


def test_truncated_crash_stream_without_both_sigkill_markers_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")
    document = convergence_run.load(manifest)
    document["crash_markers"] = document["crash_markers"][:1]
    convergence_run.save(manifest, document)

    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "INCOMPLETE"
    assert "sigkill" in verdict["reason"].lower()


def test_wrong_pre_crash_configuration_boundary_fails(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")
    document = convergence_run.load(manifest)
    document["crash_configuration_boundary"]["tree_id"] = 5
    convergence_run.save(manifest, document)

    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "tree-6" in verdict["reason"].lower()


@pytest.mark.parametrize("source_id", ("replica-2", "adaptive-manager"))
def test_survivors_and_manager_still_require_clean_stop(
    tmp_path: Path, source_id: str
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / source_id)

    def remove_stopped(values: list[dict[str, Any]]) -> None:
        values[:] = [
            event for event in values if event["event_type"] != "process.stopped"
        ]

    convergence_run.rewrite_events(manifest, source_id, remove_stopped)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{source_id}"
    )

    assert verdict["verdict"] == "INCOMPLETE"
    assert "process.stopped" in verdict["reason"]


@pytest.mark.parametrize("source_id", ("replica-2", "adaptive-manager"))
def test_self_reported_shutdown_status_must_remain_null(
    tmp_path: Path, source_id: str
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / source_id)

    def add_impossible_exit_status(values: list[dict[str, Any]]) -> None:
        stopped = next(
            event for event in values if event["event_type"] == "process.stopped"
        )
        stopped["payload"]["exit_status"] = 0

    convergence_run.rewrite_events(
        manifest, source_id, add_impossible_exit_status
    )
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{source_id}"
    )

    assert verdict["verdict"] == "FAIL"
    assert "exit_status" in verdict["reason"]


def test_unexpected_survivor_exit_keeps_run_incomplete(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")
    document = convergence_run.load(manifest)
    document["run_completion"]["unexpected_survivor_exits"] = [
        "replica-5 exited with status 1"
    ]
    convergence_run.save(manifest, document)

    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "INCOMPLETE"
    assert "unexpected" in verdict["reason"].lower()


@pytest.mark.parametrize(
    "event_type", ("epoch.command_committed", "epoch.activated")
)
def test_crash_target_cannot_emit_post_sigkill_successor_events(
    tmp_path: Path, event_type: str
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / event_type)
    template = next(
        event
        for event in _stream_events(manifest, "replica-2")
        if event["event_type"] == event_type
    )

    def add_impossible_event(values: list[dict[str, Any]]) -> None:
        event = json.loads(json.dumps(template))
        event["source_id"] = "replica-0"
        event["source_instance"] = "synthetic-convergence-replica-0"
        event["source_monotonic_ns"] = 2_600_000_000
        values.append(event)

    convergence_run.rewrite_events(manifest, "replica-0", add_impossible_event)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{event_type}"
    )

    assert verdict["verdict"] == "FAIL"
    assert "post-sigkill" in verdict["reason"].lower()


def test_missing_common_epoch_one_commit_is_incomplete(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def remove_witness(values: list[dict[str, Any]]) -> None:
        values[:] = [
            event
            for event in values
            if not (
                event["event_type"] == "block.commit_observed"
                and event["payload"].get("block_height")
                == convergence_run.COMMON_SUCCESSOR_HEIGHT
            )
        ]

    convergence_run.rewrite_events(manifest, "replica-4", remove_witness)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "INCOMPLETE"
    assert "common epoch-1 commit" in verdict["reason"].lower()


def test_mismatched_common_epoch_one_commit_fails(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def mismatch_witness(values: list[dict[str, Any]]) -> None:
        witness = next(
            event
            for event in values
            if event["event_type"] == "block.commit_observed"
        )
        witness["payload"]["block_hash"] = "e" * 64

    convergence_run.rewrite_events(manifest, "replica-4", mismatch_witness)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "successor commit disagreement" in verdict["reason"].lower()


@pytest.mark.parametrize("replica", convergence_run.SURVIVORS)
def test_any_same_height_survivor_hash_disagreement_fails_even_when_match_exists(
    tmp_path: Path, replica: int
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / f"replica-{replica}")
    conflicting_hash = "e" * 64

    def add_earlier_witness_disagreement(values: list[dict[str, Any]]) -> None:
        observed = next(
            event
            for event in values
            if event["event_type"] == "block.commit_observed"
        )
        earlier = json.loads(json.dumps(observed))
        earlier["source_monotonic_ns"] = 4_160_000_000
        earlier["payload"]["block_hash"] = conflicting_hash
        values.append(earlier)

    convergence_run.rewrite_events(
        manifest, f"replica-{replica}", add_earlier_witness_disagreement
    )
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "disagreement" in verdict["reason"].lower()


def test_witness_only_same_height_conflict_fails_without_committed_seed(
    tmp_path: Path,
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")
    conflict_height = 17

    def add_witness_only_conflict(
        values: list[dict[str, Any]], *, block_hash: str, timestamp_ns: int
    ) -> None:
        observed = next(
            event
            for event in values
            if event["event_type"] == "block.commit_observed"
        )
        conflicting = json.loads(json.dumps(observed))
        conflicting["source_monotonic_ns"] = timestamp_ns
        conflicting["payload"]["block_height"] = conflict_height
        conflicting["payload"]["block_hash"] = block_hash
        conflicting["payload"]["parent_hash"] = f"{conflict_height - 1:064x}"
        values.append(conflicting)

    convergence_run.rewrite_events(
        manifest,
        "replica-3",
        lambda values: add_witness_only_conflict(
            values,
            block_hash="a" * 64,
            timestamp_ns=4_150_000_000,
        ),
    )
    convergence_run.rewrite_events(
        manifest,
        "replica-4",
        lambda values: add_witness_only_conflict(
            values,
            block_hash="b" * 64,
            timestamp_ns=4_160_000_000,
        ),
    )
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "disagreement" in verdict["reason"].lower()


def test_pre_ack_common_candidate_does_not_mask_later_valid_common_commit(
    tmp_path: Path,
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")
    earlier_height = 17
    earlier_hash = f"{earlier_height:064x}"

    def add_pre_ack_candidate(
        values: list[dict[str, Any]], replica: int
    ) -> None:
        observed = next(
            event
            for event in values
            if event["event_type"] == "block.commit_observed"
        )
        witness = json.loads(json.dumps(observed))
        witness["source_monotonic_ns"] = 4_000_000_000 + replica * 1_000_000
        witness["payload"]["block_height"] = earlier_height
        witness["payload"]["block_hash"] = earlier_hash
        witness["payload"]["parent_hash"] = f"{earlier_height - 1:064x}"
        values.append(witness)
        if replica == 2:
            committed = next(
                event
                for event in values
                if event["event_type"] == "block.committed"
            )
            authoritative = json.loads(json.dumps(committed))
            authoritative["source_monotonic_ns"] = 4_009_000_000
            authoritative["payload"]["block_height"] = earlier_height
            authoritative["payload"]["block_hash"] = earlier_hash
            authoritative["payload"]["parent_hash"] = (
                f"{earlier_height - 1:064x}"
            )
            authoritative["payload"]["decision_proof"]["block_hash"] = (
                earlier_hash
            )
            values.append(authoritative)

    for replica in convergence_run.SURVIVORS:
        convergence_run.rewrite_events(
            manifest,
            f"replica-{replica}",
            lambda values, replica=replica: add_pre_ack_candidate(
                values, replica
            ),
        )
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "PASS"
    assert verdict["first_common_successor_commit"] == (
        convergence_run.first_common_successor_commit()
    )


def test_authoritative_successor_commit_itself_must_follow_ack_recovery(
    tmp_path: Path,
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def move_only_authoritative_commit_before_ack(
        values: list[dict[str, Any]],
    ) -> None:
        committed = next(
            event
            for event in values
            if event["event_type"] == "block.committed"
        )
        committed["source_monotonic_ns"] = 4_100_000_000

    convergence_run.rewrite_events(
        manifest, "replica-2", move_only_authoritative_commit_before_ack
    )
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "ack" in verdict["reason"].lower()


def test_runner_state_ack_recovery_timestamp_must_match_raw_ack(
    tmp_path: Path,
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")
    raw_ack = next(
        event
        for event in _stream_events(manifest, "adaptive-manager")
        if event["event_type"] == "adaptive_v2_activation_observed"
        and event["payload"].get("replica_id") == 6
        and event["payload"].get("disposition") == "ack_sent"
    )
    manifest_document = convergence_run.load(manifest)
    state_artifact = next(
        item
        for item in manifest_document["artifacts"]
        if item["kind"] == "runner_state"
    )
    state = convergence_run.load(manifest.parent / state_artifact["path"])
    assert (
        state["loss_controls"]["activation_ack"]["ack_source_monotonic_ns"]
        == raw_ack["source_monotonic_ns"]
        == convergence_run.ACK_RECOVERY_NS
    )

    convergence_run.mutate_artifact(
        manifest,
        "runner_state",
        lambda value: value["loss_controls"]["activation_ack"].update(
            {"ack_source_monotonic_ns": convergence_run.ACK_RECOVERY_NS + 1}
        ),
    )
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "ack" in verdict["reason"].lower()


@pytest.mark.parametrize("common_ns", (4_109_999_999, 4_110_000_000))
def test_common_epoch_one_commit_must_be_strictly_after_ack_completion(
    tmp_path: Path, common_ns: int
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / f"common-{common_ns}")

    def move_common_commit(values: list[dict[str, Any]]) -> None:
        for event in values:
            if event["event_type"] in (
                "block.commit_observed",
                "block.committed",
            ):
                event["source_monotonic_ns"] = common_ns

    for replica in convergence_run.SURVIVORS:
        convergence_run.rewrite_events(
            manifest, f"replica-{replica}", move_common_commit
        )
    convergence_run.mutate_artifact(
        manifest,
        "runner_state",
        lambda state: state["first_common_successor_commit"].update(
            {"common_monotonic_ns": common_ns}
        ),
    )
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{common_ns}"
    )

    assert verdict["verdict"] == "FAIL"
    assert "ack" in verdict["reason"].lower()


def test_manager_commit_observations_cannot_replace_survivor_commit_proof(
    tmp_path: Path,
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def remove_successor_commits(values: list[dict[str, Any]]) -> None:
        values[:] = [
            event
            for event in values
            if event["event_type"]
            not in ("block.commit_observed", "block.committed")
        ]

    for replica in convergence_run.SURVIVORS:
        convergence_run.rewrite_events(
            manifest, f"replica-{replica}", remove_successor_commits
        )
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "INCOMPLETE"
    assert "common epoch-1 commit" in verdict["reason"].lower()


@pytest.mark.parametrize("commit_count", (0, 4))
def test_manager_commit_observations_are_advisory_when_q_activations_match(
    tmp_path: Path, commit_count: int
) -> None:
    manifest, epochs = convergence_run.create_run(
        tmp_path / f"advisory-commits-{commit_count}"
    )
    retained = set(convergence_run.SURVIVORS[:commit_count])

    def retain_only_advisory_commits(values: list[dict[str, Any]]) -> None:
        values[:] = [
            event
            for event in values
            if not (
                event["event_type"] == "adaptive_v2_commit_observed"
                and event["payload"].get("replica_id") not in retained
            )
        ]
        for event in values:
            payload = event.get("payload")
            if (
                isinstance(payload, dict)
                and payload.get("identity") is not None
            ):
                payload["accepted_commit_count"] = commit_count

    _manager_mutation(manifest, retain_only_advisory_commits)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{commit_count}"
    )

    assert verdict["verdict"] == "PASS"
    assert verdict["accepted_activation_count"] == 5
    assert verdict["advisory_commit_sources"] == sorted(retained)
    assert verdict["advisory_commit_count"] == commit_count


def test_q_minus_one_observations_are_incomplete(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def remove_one_source(values: list[dict[str, Any]]) -> None:
        values[:] = [
            event
            for event in values
            if not (
                event["event_type"]
                in (
                    "adaptive_v2_commit_observed",
                    "adaptive_v2_activation_observed",
                )
                and event["payload"].get("replica_id") == 6
            )
        ]
        _without_terminal_events(values)

    _manager_mutation(manifest, remove_one_source)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "INCOMPLETE"


def test_q_commits_with_fewer_than_q_activations_never_becomes_ready(
    tmp_path: Path,
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def remove_fifth_activation(values: list[dict[str, Any]]) -> None:
        values[:] = [
            event
            for event in values
            if not (
                event["event_type"] == "adaptive_v2_activation_observed"
                and event["payload"].get("replica_id") == 6
            )
        ]
        _without_terminal_events(values)

    _manager_mutation(manifest, remove_fifth_activation)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "INCOMPLETE"
    assert "activation" in verdict["reason"].lower()


def test_duplicate_activation_source_does_not_count_twice(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def duplicate_source(values: list[dict[str, Any]]) -> None:
        for event in values:
            if (
                event["event_type"] == "adaptive_v2_activation_observed"
                and event["payload"].get("replica_id") == 6
            ):
                event["payload"]["replica_id"] = 5

    _manager_mutation(manifest, duplicate_source)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "distinct" in verdict["reason"].lower()


def test_split_full_activation_identity_fails(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def split_identity(values: list[dict[str, Any]]) -> None:
        for event in values:
            if (
                event["event_type"] == "adaptive_v2_activation_observed"
                and event["payload"].get("replica_id") == 6
                and event["payload"].get("identity") is not None
            ):
                event["payload"]["identity"]["command_block_hash"] = "e" * 64

    _manager_mutation(manifest, split_identity)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "identity" in verdict["reason"].lower()


def test_changed_bundle_retry_bytes_fail(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def change_retry_digest(values: list[dict[str, Any]]) -> None:
        retry = next(
            event
            for event in values
            if event["event_type"] == "adaptive_v2_delivery_attempt"
            and event["payload"].get("replica_id") == 2
            and event["payload"].get("delivery_attempt") == 2
        )
        retry["payload"]["canonical_payload_digest"] = "f" * 64

    _manager_mutation(manifest, change_retry_digest)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "bundle" in verdict["reason"].lower()


def test_more_than_one_bundle_injected_drop_fails(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def add_second_injected_drop(values: list[dict[str, Any]]) -> None:
        injected = next(
            event
            for event in values
            if event["event_type"] == "adaptive_v2_delivery_attempt"
            and event["payload"].get("disposition") == "injected_drop"
        )
        second = json.loads(json.dumps(injected))
        second["source_monotonic_ns"] = 1_050_000_000
        second["payload"]["replica_id"] = 3
        values.append(second)

    _manager_mutation(manifest, add_second_injected_drop)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "injected drop" in verdict["reason"].lower()


def test_changed_activation_retransmission_bytes_fail(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def change_retry_digest(values: list[dict[str, Any]]) -> None:
        retry = next(
            event
            for event in values
            if event["event_type"] == "adaptive_v2_activation_observed"
            and event["payload"].get("replica_id") == 6
            and event["payload"].get("disposition") == "duplicate"
        )
        retry["payload"]["canonical_payload_digest"] = "f" * 64

    _manager_mutation(manifest, change_retry_digest)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"


def test_ack_loss_must_target_the_q_completing_accepted_activation(
    tmp_path: Path,
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "early-target")

    def retarget_ack_loss(values: list[dict[str, Any]]) -> None:
        values[:] = [
            event
            for event in values
            if not (
                event["event_type"] == "adaptive_v2_activation_observed"
                and event["payload"].get("replica_id") == 2
                and event["payload"].get("disposition") == "ack_sent"
            )
        ]
        for event in values:
            payload = event.get("payload", {})
            if (
                event["event_type"] == "adaptive_v2_activation_observed"
                and payload.get("replica_id") == 6
                and payload.get("disposition")
                in {"ack_injected_drop", "duplicate", "ack_sent"}
            ):
                payload["replica_id"] = 2
                payload["canonical_payload_digest"] = (
                    convergence_run.activation_payload_digest(2)
                )

    _manager_mutation(manifest, retarget_ack_loss)
    convergence_run.mutate_artifact(
        manifest,
        "runner_state",
        lambda state: state["loss_controls"]["activation_ack"].update(
            {
                "replica_id": 2,
                "canonical_payload_digest": (
                    convergence_run.activation_payload_digest(2)
                ),
            }
        ),
    )
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated-early-target"
    )

    assert verdict["verdict"] == "FAIL"
    assert "q-completing" in verdict["reason"].lower()


@pytest.mark.parametrize("disposition", ("duplicate", "ack_sent"))
def test_ack_recovery_must_preserve_the_winning_identity(
    tmp_path: Path, disposition: str
) -> None:
    manifest, epochs = convergence_run.create_run(
        tmp_path / f"wrong-identity-{disposition}"
    )

    def change_identity(values: list[dict[str, Any]]) -> None:
        event = next(
            value
            for value in values
            if value["event_type"] == "adaptive_v2_activation_observed"
            and value["payload"].get("replica_id") == 6
            and value["payload"].get("disposition") == disposition
        )
        event["payload"]["identity"]["command_block_hash"] = "f" * 64

    _manager_mutation(manifest, change_identity)
    verdict = _validator().validate_run(
        manifest,
        epochs,
        tmp_path / f"validated-wrong-identity-{disposition}",
    )

    assert verdict["verdict"] == "FAIL"
    assert "winning identity" in verdict["reason"].lower()


@pytest.mark.parametrize("disposition", ("duplicate", "ack_sent"))
def test_ack_recovery_count_failure_is_distinct_from_identity_and_target(
    tmp_path: Path, disposition: str
) -> None:
    manifest, epochs = convergence_run.create_run(
        tmp_path / f"lost-terminal-counts-{disposition}"
    )

    def clear_terminal_counts(values: list[dict[str, Any]]) -> None:
        event = next(
            value
            for value in values
            if value["event_type"] == "adaptive_v2_activation_observed"
            and value["payload"].get("replica_id") == 6
            and value["payload"].get("disposition") == disposition
        )
        event["payload"]["accepted_commit_count"] = 0
        event["payload"]["accepted_activation_count"] = 0

    _manager_mutation(manifest, clear_terminal_counts)
    verdict = _validator().validate_run(
        manifest,
        epochs,
        tmp_path / f"validated-lost-terminal-counts-{disposition}",
    )

    reason = verdict["reason"].lower()
    assert verdict["verdict"] == "FAIL"
    assert "count" in reason
    assert "winning identity" not in reason
    assert "wrong replica" not in reason


@pytest.mark.parametrize("ack_sent_ns", (3_509_999_999, 3_510_000_000))
def test_ack_sent_before_or_at_dropped_ack_fails(
    tmp_path: Path, ack_sent_ns: int
) -> None:
    manifest, epochs = convergence_run.create_run(
        tmp_path / f"ack-sent-{ack_sent_ns}"
    )

    def add_early_ack_sent(values: list[dict[str, Any]]) -> None:
        sent = next(
            event
            for event in values
            if event["event_type"] == "adaptive_v2_activation_observed"
            and event["payload"].get("replica_id") == 6
            and event["payload"].get("disposition") == "ack_sent"
        )
        early = json.loads(json.dumps(sent))
        early["source_monotonic_ns"] = ack_sent_ns
        drop_index = next(
            index
            for index, event in enumerate(values)
            if event["event_type"] == "adaptive_v2_activation_observed"
            and event["payload"].get("replica_id") == 6
            and event["payload"].get("disposition") == "ack_injected_drop"
        )
        values.insert(drop_index, early)

    _manager_mutation(manifest, add_early_ack_sent)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{ack_sent_ns}"
    )

    assert verdict["verdict"] == "FAIL"
    assert "ack" in verdict["reason"].lower()


@pytest.mark.parametrize("missing", ("injection", "retry"))
def test_missing_bundle_injection_or_retry_fails(
    tmp_path: Path, missing: str
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / missing)

    def remove_required_event(values: list[dict[str, Any]]) -> None:
        disposition = "injected_drop" if missing == "injection" else "enqueued"
        attempt = 1 if missing == "injection" else 2
        values[:] = [
            event
            for event in values
            if not (
                event["event_type"] == "adaptive_v2_delivery_attempt"
                and event["payload"].get("replica_id") == 2
                and event["payload"].get("delivery_attempt") == attempt
                and event["payload"].get("disposition") == disposition
            )
        ]

    _manager_mutation(manifest, remove_required_event)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{missing}"
    )

    assert verdict["verdict"] == "FAIL"


@pytest.mark.parametrize(
    "disposition", ("ack_injected_drop", "duplicate", "ack_sent")
)
def test_missing_activation_ack_loss_recovery_step_fails(
    tmp_path: Path, disposition: str
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / disposition)

    def remove_step(values: list[dict[str, Any]]) -> None:
        values[:] = [
            event
            for event in values
            if not (
                event["event_type"] == "adaptive_v2_activation_observed"
                and event["payload"].get("replica_id") == 6
                and event["payload"].get("disposition") == disposition
            )
        ]

    _manager_mutation(manifest, remove_step)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{disposition}"
    )

    assert verdict["verdict"] == "FAIL"


@pytest.mark.parametrize(
    "event_type", ("adaptive_v2_converged", "adaptive_v2_ready")
)
def test_duplicate_terminal_convergence_event_fails(
    tmp_path: Path, event_type: str
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / event_type)

    def duplicate_terminal(values: list[dict[str, Any]]) -> None:
        event = next(value for value in values if value["event_type"] == event_type)
        duplicate = json.loads(json.dumps(event))
        duplicate["source_monotonic_ns"] += 1
        values.append(duplicate)

    _manager_mutation(manifest, duplicate_terminal)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{event_type}"
    )

    assert verdict["verdict"] == "FAIL"


def test_terminal_success_before_fifth_activation_fails(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def move_terminal_before_fifth_activation(
        values: list[dict[str, Any]],
    ) -> None:
        timestamps = {
            "adaptive_v2_converged": 3_490_000_000,
            "adaptive_v2_ready": 3_495_000_000,
        }
        for event in values:
            if event["event_type"] in timestamps:
                event["source_monotonic_ns"] = timestamps[event["event_type"]]

    _manager_mutation(manifest, move_terminal_before_fifth_activation)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "activation" in verdict["reason"].lower()


def test_any_convergence_failure_event_forces_failure(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def add_convergence_failure(values: list[dict[str, Any]]) -> None:
        terminal = next(
            event
            for event in values
            if event["event_type"] == "adaptive_v2_converged"
        )
        failure = json.loads(json.dumps(terminal))
        failure["source_monotonic_ns"] = 3_540_000_000
        failure["event_type"] = "adaptive_v2_convergence_failure"
        failure["payload"]["failure_reason"] = "synthetic conflicting identity"
        values.append(failure)

    _manager_mutation(manifest, add_convergence_failure)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "failure" in verdict["reason"].lower()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("replica_count", 8),
        ("fault_threshold", 1),
        ("quorum", 4),
        ("membership", [0, 1, 2, 3, 4, 5]),
    ),
)
def test_changed_n_f_q_or_membership_fails(
    tmp_path: Path, field: str, value: Any
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / field)
    document = convergence_run.load(manifest)
    document[field] = value
    convergence_run.save(manifest, document)

    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / f"validated-{field}"
    )

    assert verdict["verdict"] == "FAIL"


def test_survivors_must_all_emit_matching_epoch_one_activation(
    tmp_path: Path,
) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "run")

    def mismatch(values: list[dict[str, Any]]) -> None:
        activation = next(
            event for event in values if event["event_type"] == "epoch.activated"
        )
        activation["payload"]["epoch_digest"] = "e" * 64

    convergence_run.rewrite_events(manifest, "replica-6", mismatch)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated"
    )

    assert verdict["verdict"] == "FAIL"
    assert "epoch.activated" in verdict["reason"]


@pytest.mark.parametrize("replica", convergence_run.SURVIVORS)
@pytest.mark.parametrize(
    "event_type",
    ("epoch.command_committed", "block.committed", "epoch.activated"),
)
def test_every_survivor_rejects_any_event_claiming_epoch_above_one(
    tmp_path: Path, event_type: str, replica: int
) -> None:
    manifest, epochs = convergence_run.create_run(
        tmp_path / f"{event_type}-{replica}"
    )

    def add_epoch_two_event(values: list[dict[str, Any]]) -> None:
        original = next(
            event for event in values if event["event_type"] == event_type
        )
        epoch_two = json.loads(json.dumps(original))
        epoch_two["source_monotonic_ns"] = 4_300_000_000
        if event_type == "epoch.command_committed":
            epoch_two["payload"]["successor_epoch_number"] = 2
            epoch_two["payload"]["successor_epoch_digest"] = "c" * 64
        elif event_type == "block.committed":
            epoch_two["payload"]["decision_proof"]["epoch_number"] = 2
            epoch_two["payload"]["decision_proof"]["epoch_digest"] = "c" * 64
        else:
            epoch_two["payload"]["epoch_number"] = 2
            epoch_two["payload"]["epoch_digest"] = "c" * 64
        values.append(epoch_two)

    convergence_run.rewrite_events(
        manifest, f"replica-{replica}", add_epoch_two_event
    )
    verdict = _validator().validate_run(
        manifest,
        epochs,
        tmp_path / f"validated-{event_type}-{replica}",
    )

    assert verdict["verdict"] == "FAIL"
    assert "epoch" in verdict["reason"].lower()


def test_epoch_two_or_second_successor_bundle_fails(tmp_path: Path) -> None:
    manifest, epochs = convergence_run.create_run(tmp_path / "epoch-two")
    document = convergence_run.load(epochs)
    epoch_two = json.loads(json.dumps(document["epochs"][1]))
    epoch_two["epoch_number"] = 2
    epoch_two["epoch_digest"] = "c" * 64
    document["epochs"].append(epoch_two)
    convergence_run.save(epochs, document)
    manifest_document = convergence_run.load(manifest)
    epoch_artifact = next(
        item for item in manifest_document["artifacts"] if item["kind"] == "epochs"
    )
    import hashlib

    epoch_artifact["sha256"] = hashlib.sha256(epochs.read_bytes()).hexdigest()
    convergence_run.save(manifest, manifest_document)

    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated-epoch-two"
    )
    assert verdict["verdict"] == "FAIL"

    manifest, epochs = convergence_run.create_run(tmp_path / "second-bundle")
    second = manifest.parent / "second-successor.bundle"
    second.write_bytes(b"second bundle is forbidden")
    document = convergence_run.load(manifest)
    document["artifacts"].append(
        {
            "kind": "successor_bundle",
            "path": second.name,
            "sha256": __import__("hashlib").sha256(second.read_bytes()).hexdigest(),
        }
    )
    convergence_run.save(manifest, document)
    verdict = _validator().validate_run(
        manifest, epochs, tmp_path / "validated-second-bundle"
    )
    assert verdict["verdict"] == "FAIL"


@pytest.mark.parametrize(
    ("exit_code", "ready_count"), ((0, 0), (0, 2), (1, 1))
)
def test_manager_exit_success_requires_exactly_one_ready(
    tmp_path: Path, exit_code: int, ready_count: int
) -> None:
    manifest, epochs = convergence_run.create_run(
        tmp_path / f"exit-{exit_code}-ready-{ready_count}"
    )
    document = convergence_run.load(manifest)
    document["run_completion"]["manager_exit_code"] = exit_code
    convergence_run.save(manifest, document)

    def terminal_count(values: list[dict[str, Any]]) -> None:
        ready = [
            event for event in values if event["event_type"] == "adaptive_v2_ready"
        ]
        values[:] = [
            event for event in values if event["event_type"] != "adaptive_v2_ready"
        ]
        for offset in range(ready_count):
            event = json.loads(json.dumps(ready[0]))
            event["source_monotonic_ns"] += offset
            values.append(event)

    _manager_mutation(manifest, terminal_count)
    verdict = _validator().validate_run(
        manifest,
        epochs,
        tmp_path / f"validated-exit-{exit_code}-ready-{ready_count}",
    )

    assert verdict["verdict"] == "FAIL"
