"""Direct scientific-integrity tests for the profiled N=31 runtime."""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

PROFILE_PATH = Path(__file__).parents[1] / "profiles" / "n31-f5-crash-shakedown-v1.json"
RUN_ID = "synthetic-n31-final"
BASELINE_START_NS = 1_000_000_000
BASELINE_END_NS = 31_000_000_000
CRASH_REQUEST_NS = 35_000_000_000
CRASH_CONFIRMED_NS = 36_000_000_000
POST_START_NS = 40_000_000_000
POST_END_NS = 70_000_000_000
CLEANUP_STARTED_NS = 80_000_000_000
EPOCH_DIGEST = "145fac093343fa9cff20fcf49d85ad5443e93db14146f7854b17e28cf44f6d7a"
READY_BASE_NS = 100_000_000


def _runtime():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.profiled_fault_runtime"
    )


def _evaluation():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.profiled_fault_evaluation"
    )


def _append_event(
    streams: dict[str, list[dict[str, Any]]],
    instances: dict[str, str],
    source: str,
    timestamp_ns: int,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    event = {
        "event_schema_version": 1,
        "run_id": RUN_ID,
        "source_kind": (
            "replica" if source.startswith("replica-") else "adaptation_manager"
        ),
        "source_id": source,
        "source_instance": instances[source],
        "source_sequence": len(streams[source]) + 1,
        "source_monotonic_ns": timestamp_ns,
        "event_type": event_type,
        "payload": payload,
    }
    streams[source].append(event)
    return event


def _commit_payload(height: int) -> dict[str, Any]:
    return {
        "block_height": height,
        "block_hash": f"{height:064x}",
        "parent_hash": f"{height - 1:064x}",
        "transaction_count": 1000,
        "commit_batch_index": height,
    }


def _configuration_payload(replica: int) -> dict[str, Any]:
    return {
        "epoch_number": 0,
        "tree_id": 30,
        "epoch_digest": EPOCH_DIGEST,
        "block_hash": None,
        "context_generation": None,
        "observer_replica": replica,
        "wait_exempt_signers": [],
        "accepted_signers": [],
        "absent_direct_children": [],
        "missing_optional_signers": [],
        "required_branch_gaps": [],
        "root_signer_count": 0,
        "global_quorum": 21,
        "rejection_reason": None,
    }


def _cleanup_terminal_payload() -> dict[str, Any]:
    return {
        "cycle_ordinal": 0,
        "policy_intent": "fault_containment",
        "outcome": "failed",
        "reason": "caller_failed",
        "transition_artifact_id": ("e0-to-e1-shakedown-containment"),
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": EPOCH_DIGEST,
        "successor_epoch_number": None,
        "successor_epoch_digest": None,
        "command_payload_digest": None,
        "winning_activation": None,
        "evidence_window_activation_generation": 1,
        "baseline_evidence_cutoff": 0,
        "current_evidence_cutoff": 0,
    }


def _fixture() -> dict[str, Any]:
    evaluation = _evaluation()
    runtime = _runtime()
    profile = evaluation.load_frozen_profile(PROFILE_PATH)
    sources = [f"replica-{replica}" for replica in profile.replica_ids]
    sources.append(runtime.MANAGER_SOURCE_ID)
    instances = {source: f"{RUN_ID}-{source}" for source in sources}
    streams: dict[str, list[dict[str, Any]]] = {source: [] for source in sources}
    observer = f"replica-{profile.authoritative_observer}"

    for ordinal, source in enumerate(sources):
        _append_event(
            streams,
            instances,
            source,
            READY_BASE_NS + ordinal * 10_000_000,
            "process.ready",
            {"exit_status": None},
        )

    for bucket in range(profile.baseline_bucket_count):
        height = 100 + bucket
        timestamp = BASELINE_START_NS + bucket * 5_000_000_000
        payload = _commit_payload(height)
        _append_event(
            streams,
            instances,
            observer,
            timestamp,
            "block.committed",
            {
                **payload,
                "designated_observer": True,
                "decision_proof": {
                    "epoch_number": 0,
                    "tree_id": 30,
                    "epoch_digest": EPOCH_DIGEST,
                    "block_hash": payload["block_hash"],
                },
                "view_generation": 1,
            },
        )

    witnesses = evaluation.postfault_witnesses(profile)
    for replica in witnesses:
        _append_event(
            streams,
            instances,
            f"replica-{replica}",
            27_000_000_000,
            "block.commit_observed",
            _commit_payload(105),
        )

    boundary_events = {
        replica: _append_event(
            streams,
            instances,
            f"replica-{replica}",
            34_000_000_000,
            "adaptive.configuration_active",
            _configuration_payload(replica),
        )
        for replica in profile.replica_ids
    }

    for bucket in range(profile.post_bucket_count):
        height = 200 + bucket
        timestamp = POST_START_NS + bucket * 5_000_000_000 + 1_000_000_000
        payload = _commit_payload(height)
        _append_event(
            streams,
            instances,
            observer,
            timestamp,
            "block.committed",
            {
                **payload,
                "designated_observer": True,
                "decision_proof": {
                    "epoch_number": 0,
                    "tree_id": 30,
                    "epoch_digest": EPOCH_DIGEST,
                    "block_hash": payload["block_hash"],
                },
                "view_generation": 1,
            },
        )

    for replica in witnesses:
        _append_event(
            streams,
            instances,
            f"replica-{replica}",
            66_000_000_000,
            "block.commit_observed",
            _commit_payload(205),
        )

    _append_event(
        streams,
        instances,
        runtime.MANAGER_SOURCE_ID,
        CLEANUP_STARTED_NS,
        "adaptive_v2_session_terminal",
        _cleanup_terminal_payload(),
    )
    manifest = {
        "run_id": RUN_ID,
        "source_instances": instances,
        "measurement_windows": {
            "baseline": {
                "start_ns": BASELINE_START_NS,
                "end_ns": BASELINE_END_NS,
            },
            "postfault": {
                "start_ns": POST_START_NS,
                "end_ns": POST_END_NS,
            },
        },
        "crash_boundary": {
            "epoch_number": 0,
            "tree_id": 30,
            "root_replica": 30,
            "epoch_digest": EPOCH_DIGEST,
            "global_quorum": 21,
            "members_breadth_first": list(profile.epoch0_members_breadth_first),
            "replica_evidence": [
                {
                    "source_id": f"replica-{replica}",
                    "source_sequence": boundary_events[replica]["source_sequence"],
                    "source_monotonic_ns": boundary_events[replica][
                        "source_monotonic_ns"
                    ],
                }
                for replica in profile.replica_ids
            ],
        },
    }
    return {
        "profile": profile,
        "manifest": manifest,
        "streams": streams,
        "crash_marker": {
            "requested_monotonic_ns": CRASH_REQUEST_NS,
            "confirmed_monotonic_ns": CRASH_CONFIRMED_NS,
        },
    }


def _validate(fixture: dict[str, Any], run_directory: Path) -> dict[str, object]:
    return _runtime().validate_final_streams(
        fixture["profile"],
        manifest=fixture["manifest"],
        streams=fixture["streams"],
        crash_marker=fixture["crash_marker"],
        cleanup_started_ns=CLEANUP_STARTED_NS,
        run_directory=run_directory,
    )


def test_validate_final_streams_accepts_exact_rich_n31_evidence(
    tmp_path: Path,
) -> None:
    verdict = _validate(_fixture(), tmp_path)

    assert verdict["pre_fault_common_commit"]["block_height"] == 105
    assert verdict["post_fault_common_commit"]["block_height"] == 205
    assert len(verdict["baseline_rows"]) == 6
    assert len(verdict["postfault_rows"]) == 6
    assert all(row["tps"] == 200 for row in verdict["baseline_rows"])
    assert all(row["tps"] == 200 for row in verdict["postfault_rows"])


def test_validate_final_streams_accepts_measured_zero_postfault_bucket(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    observer = f"replica-{fixture['profile'].authoritative_observer}"
    fixture["streams"][observer] = [
        event
        for event in fixture["streams"][observer]
        if not (
            event["event_type"] == "block.committed"
            and event["payload"].get("block_height") == 200
        )
    ]
    for sequence, event in enumerate(fixture["streams"][observer], 1):
        event["source_sequence"] = sequence

    verdict = _validate(fixture, tmp_path)

    assert verdict["post_fault_common_commit"]["block_height"] == 205
    assert verdict["postfault_rows"][0]["tps"] == 0
    assert all(row["tps"] == 200 for row in verdict["postfault_rows"][1:])


def test_validate_final_streams_rejects_missing_process_ready(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    fixture["streams"]["replica-1"].pop(0)
    for sequence, event in enumerate(fixture["streams"]["replica-1"], 1):
        event["source_sequence"] = sequence

    with pytest.raises(
        _runtime().ProfiledFaultRuntimeError,
        match="exactly one process.ready",
    ):
        _validate(fixture, tmp_path)


def test_validate_final_streams_rejects_baseline_not_anchored_to_first_commit(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    fixture["manifest"]["measurement_windows"]["baseline"] = {
        "start_ns": BASELINE_START_NS + 1,
        "end_ns": BASELINE_END_NS + 1,
    }

    with pytest.raises(
        _runtime().ProfiledFaultRuntimeError,
        match="readiness barrier|first authoritative commit",
    ):
        _validate(fixture, tmp_path)


@pytest.mark.parametrize("phase", ("baseline", "postfault"))
def test_validate_final_streams_rejects_nonexact_window_duration(
    tmp_path: Path,
    phase: str,
) -> None:
    fixture = _fixture()
    fixture["manifest"]["measurement_windows"][phase]["end_ns"] -= 1

    with pytest.raises(
        _runtime().ProfiledFaultRuntimeError,
        match="window duration",
    ):
        _validate(fixture, tmp_path)


def test_terminal_pass_requires_preserved_semantic_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    calls: list[Path] = []
    monkeypatch.setattr(
        runtime, "create_evidence_seal", lambda path: calls.append(path)
    )
    monkeypatch.setattr(
        runtime, "verify_evidence_seal", lambda path: calls.append(path)
    )

    def reject(path: Path) -> dict[str, object]:
        calls.append(path)
        raise runtime.ProfiledFaultRuntimeError("synthetic semantic rejection")

    monkeypatch.setattr(runtime, "validate_preserved_run", reject)

    with pytest.raises(
        runtime.ProfiledFaultRuntimeError,
        match="preserved semantic revalidation.*synthetic semantic rejection",
    ):
        runtime._seal_and_validate_terminal_run(tmp_path, "PASS")

    assert calls == [tmp_path, tmp_path, tmp_path]


@pytest.mark.parametrize("verdict", ("FAIL", "INCOMPLETE"))
def test_terminal_nonpass_is_sealed_without_pass_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
) -> None:
    runtime = _runtime()
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        runtime,
        "create_evidence_seal",
        lambda path: calls.append(("create", path)),
    )
    monkeypatch.setattr(
        runtime,
        "verify_evidence_seal",
        lambda path: calls.append(("verify", path)),
    )
    monkeypatch.setattr(
        runtime,
        "validate_preserved_run",
        lambda _path: pytest.fail("non-PASS evidence must not be promoted"),
    )

    runtime._seal_and_validate_terminal_run(tmp_path, verdict)

    assert calls == [("create", tmp_path), ("verify", tmp_path)]


def test_validate_final_streams_rejects_cross_replica_height_conflict(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    event = next(
        event
        for event in fixture["streams"]["replica-1"]
        if event["event_type"] == "block.commit_observed"
    )
    event["payload"]["block_hash"] = "f" * 64

    with pytest.raises(
        _runtime().ProfiledFaultRuntimeError,
        match="same-height hash conflict",
    ):
        _validate(fixture, tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    (("source_sequence", 99), ("source_instance", "replacement-instance")),
)
def test_validate_final_streams_rejects_source_identity_or_sequence_drift(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    fixture = _fixture()
    fixture["streams"]["replica-1"][0][field] = value

    with pytest.raises(
        _runtime().ProfiledFaultRuntimeError,
        match="source identity or sequence drift",
    ):
        _validate(fixture, tmp_path)


def test_validate_final_streams_rejects_common_commit_outside_post_window(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    for events in fixture["streams"].values():
        for event in events:
            if event["payload"].get("block_height") == 205:
                event["source_monotonic_ns"] = POST_END_NS

    with pytest.raises(
        _runtime().ProfiledFaultRuntimeError,
        match="fixed Q21 common commit.*post|measurement window",
    ):
        _validate(fixture, tmp_path)


def test_validate_final_streams_rejects_decision_proof_epoch_digest_drift(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    observer = f"replica-{fixture['profile'].authoritative_observer}"
    committed = next(
        event
        for event in fixture["streams"][observer]
        if event["event_type"] == "block.committed"
    )
    committed["payload"]["decision_proof"]["epoch_digest"] = "c" * 64

    with pytest.raises(
        _runtime().ProfiledFaultRuntimeError,
        match="decision proof digest.*exact epoch zero",
    ):
        _validate(fixture, tmp_path)


def test_validate_final_streams_rejects_configuration_race_before_crash(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    events = fixture["streams"]["replica-1"]
    boundary_index = next(
        index
        for index, event in enumerate(events)
        if event["event_type"] == "adaptive.configuration_active"
    )
    later = copy.deepcopy(events[boundary_index])
    later["source_monotonic_ns"] = 34_500_000_000
    events.insert(boundary_index + 1, later)
    for sequence, event in enumerate(events, 1):
        event["source_sequence"] = sequence

    with pytest.raises(
        _runtime().ProfiledFaultRuntimeError,
        match="active configuration changed before the SIGKILL request",
    ):
        _validate(fixture, tmp_path)


def test_no_successor_activity_accepts_one_cleanup_caller_failed_terminal(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    manager_events = fixture["streams"][_runtime().MANAGER_SOURCE_ID]
    payload = next(
        event["payload"]
        for event in manager_events
        if event["event_type"] == "adaptive_v2_session_terminal"
    )
    assert payload["successor_epoch_number"] is None
    assert payload["successor_epoch_digest"] is None

    _runtime().assert_no_successor_activity(
        {_runtime().MANAGER_SOURCE_ID: manager_events},
        run_directory=tmp_path,
        cleanup_started_ns=CLEANUP_STARTED_NS,
        post_end_ns=POST_END_NS,
    )


def test_no_successor_activity_rejects_successor_event(tmp_path: Path) -> None:
    fixture = _fixture()
    streams = {
        _runtime().MANAGER_SOURCE_ID: fixture["streams"][_runtime().MANAGER_SOURCE_ID],
        "replica-1": [
            {
                "source_monotonic_ns": POST_END_NS,
                "event_type": "epoch.generated",
                "payload": {"epoch_number": 1},
            }
        ],
    }

    with pytest.raises(
        _runtime().ProfiledFaultRuntimeError,
        match="unexpected successor activity",
    ):
        _runtime().assert_no_successor_activity(
            streams,
            run_directory=tmp_path,
            cleanup_started_ns=CLEANUP_STARTED_NS,
            post_end_ns=POST_END_NS,
        )


def test_review_boundary_requires_exact_31_unique_sources(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    fixture["manifest"]["crash_boundary"]["replica_evidence"].pop()

    with pytest.raises(
        _runtime().ProfiledFaultRuntimeError,
        match="boundary.*31|exact.*source|membership",
    ):
        _validate(fixture, tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("reference_sequence", 999),
        ("epoch_number", 1),
        ("tree_id", 29),
        ("epoch_digest", "c" * 64),
        ("global_quorum", 20),
    ),
)
def test_review_boundary_reference_binds_canonical_tree30_event(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    fixture = _fixture()
    boundary = fixture["manifest"]["crash_boundary"]
    if field == "reference_sequence":
        boundary["replica_evidence"][0]["source_sequence"] = value
    else:
        boundary[field] = value

    with pytest.raises(
        _runtime().ProfiledFaultRuntimeError,
        match="boundary|configuration|epoch|tree|digest|quorum",
    ):
        _validate(fixture, tmp_path)


class _CleanupProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def test_review_manager_cleanup_requires_exit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    monkeypatch.setattr(runtime, "monotonic_raw_ns", lambda: CLEANUP_STARTED_NS)

    def classification(returncode: int) -> str:
        process = _CleanupProcess(7301)
        record = runtime.ProcessRecord(
            name=runtime.MANAGER_SOURCE_ID,
            replica_id=-1,
            pid=process.pid,
            pgid=process.pid,
            process=process,
        )
        monkeypatch.setattr(runtime.os, "getpgid", lambda pid: pid)

        def killpg(_pgid: int, _signal_number: int) -> None:
            process.returncode = returncode

        monkeypatch.setattr(runtime.os, "killpg", killpg)
        ledger, _ = runtime.concurrent_cleanup(
            (record,),
            post_end_ns=POST_END_NS,
        )
        return str(ledger[0]["classification"])

    assert classification(1) == "expected_cleanup"
    assert classification(0) == "unexpected_exit"


def test_review_zero_transaction_buckets_are_preserved() -> None:
    profile = _evaluation().load_frozen_profile(PROFILE_PATH)

    rows = _runtime().throughput_rows(
        profile,
        (),
        phase="baseline",
        start_ns=BASELINE_START_NS,
        bucket_count=profile.baseline_bucket_count,
    )

    assert len(rows) == profile.baseline_bucket_count
    assert all(row["transaction_count"] == 0 for row in rows)
    assert all(row["unique_commit_count"] == 0 for row in rows)
    assert all(row["tps"] == 0 for row in rows)


def test_configuration_boundary_poller_emits_exact_validatable_boundary(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    profile = _evaluation().load_frozen_profile(PROFILE_PATH)
    raw = tmp_path / "raw"
    raw.mkdir()
    sources = [f"replica-{replica}" for replica in profile.replica_ids]
    for replica, source in zip(profile.replica_ids, sources):
        event = {
            "source_sequence": 1,
            "source_monotonic_ns": 34_000_000_000,
            "event_type": "adaptive.configuration_active",
            "payload": _configuration_payload(replica),
        }
        (raw / f"{source}.jsonl").write_text(
            json.dumps(event, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    poller = runtime.ConfigurationBoundaryPoller(
        profile,
        tmp_path,
        watermarks={source: -1 for source in sources},
        offsets={source: 0 for source in sources},
    )

    boundary = poller.poll()

    assert boundary is not None
    assert boundary["global_quorum"] == profile.quorum
    assert boundary["epoch_digest"] == runtime.EXPECTED_EPOCH_ZERO_DIGEST
    runtime.assert_boundary_race_free(
        profile,
        boundary,
        {
            source: [json.loads((raw / f"{source}.jsonl").read_text())]
            for source in sources
        },
        crash_request_ns=CRASH_REQUEST_NS,
    )
