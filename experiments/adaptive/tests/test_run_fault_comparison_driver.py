"""Focused tests for the isolated N=7 fault-comparison driver."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _driver():
    return importlib.import_module(
        "experiments.adaptive.run_fault_comparison"
    )


def _arm(name: str):
    return _driver().build_arm(name, "a" * 40)


@pytest.mark.parametrize("arm_name", _driver().ARM_NAMES)
def test_arm_freezes_one_n7_fault_and_never_configures_manager(
    arm_name: str,
) -> None:
    driver = _driver()
    arm = _arm(arm_name)

    assert arm.name == arm_name
    assert arm.faulty_replica_id == driver.FAULTY_REPLICA_ID
    assert arm.plan.seed == driver.SNAPSHOT_SEED
    assert arm.plan.context.replica_ids == driver.REPLICA_IDS
    assert arm.plan.context.quorum == driver.QUORUM
    assert arm.plan.context.crash_budget == driver.FAULT_THRESHOLD
    assert arm.plan.manager_cli_args() == ()
    assert len(arm.plan.actions) == 1


@pytest.mark.parametrize(
    ("arm_name", "mode_option"),
    (
        (
            "static_authenticated_false_report",
            "--experiment-false-report-target",
        ),
        (
            "static_persistent_omission",
            "--experiment-omit-outbound-aggregate",
        ),
    ),
)
def test_byzantine_overlays_bind_only_faulty_replica_to_exact_tree6(
    arm_name: str,
    mode_option: str,
) -> None:
    driver = _driver()
    overlays = driver.replica_launch_overlays(
        _arm(arm_name),
        context_limit=3,
    )

    assert all(
        overlays[replica_id] == ()
        for replica_id in driver.REPLICA_IDS
        if replica_id != driver.FAULTY_REPLICA_ID
    )
    faulty = overlays[driver.FAULTY_REPLICA_ID]
    assert mode_option in faulty
    assert faulty[
        faulty.index("--experiment-byzantine-configuration") + 1
    ] == driver.EXACT_CONFIGURATION
    assert faulty[
        faulty.index("--experiment-byzantine-window") + 1
    ] == driver.DIAGNOSTIC_WINDOW
    assert faulty[
        faulty.index("--experiment-byzantine-context-limit") + 1
    ] == "3"


def test_crash_arm_has_no_replica_or_manager_byzantine_overlay() -> None:
    driver = _driver()
    arm = _arm("sigkill_crash")

    assert driver.replica_launch_overlays(arm) == ((),) * 7
    assert arm.plan.manager_cli_args() == ()


def test_launch_bundle_is_explicitly_claim_limited(tmp_path: Path) -> None:
    driver = _driver()
    arm = _arm("static_authenticated_false_report")

    bundle = driver.launch_bundle(
        arm,
        kauri_revision="a" * 40,
        profile_path=tmp_path / "profile.json",
        profile_sha256="b" * 64,
        context_limit=5,
    )

    assert bundle["arm"] == arm.name
    assert bundle["fault_plan_sha256"] == arm.plan.sha256
    assert bundle["manager_overlay"] == []
    assert bundle["fixed_context"] == {
        "replica_ids": list(range(7)),
        "fault_threshold": 2,
        "quorum": 5,
        "seed": driver.SNAPSHOT_SEED,
        "faulty_replica_id": 1,
        "false_report_target_id": 4,
        "tree_id": 6,
        "tree_members_breadth_first": [6, 0, 1, 2, 3, 4, 5],
        "epoch0_digest": driver.EPOCH0_DIGEST,
        "diagnostic_window": driver.DIAGNOSTIC_WINDOW,
        "byzantine_context_limit": 5,
    }
    claims = bundle["claims_not_made"]
    assert any("diagnosis" in claim for claim in claims)
    assert any("statistical" in claim for claim in claims)


def test_pass_verdict_requires_all_modest_live_observations() -> None:
    driver = _driver()
    arm = _arm("static_persistent_omission")

    verdict = driver.build_arm_verdict(
        arm=arm,
        run_id="run-1",
        kauri_revision="a" * 40,
        action_observation={"kind": "aggregate_omitted"},
        before_commit={"block_height": 10, "block_hash": "1" * 64},
        after_commit={"block_height": 11, "block_hash": "2" * 64},
        fixed_quorum={
            "configured_replica_count": 7,
            "configured_fault_threshold": 2,
            "configured_quorum": 5,
            "active_configuration_records": 7,
            "invalid_records": [],
        },
        conflicts=(),
        runtime_error=None,
    )

    assert verdict["verdict"] == "PASS"
    assert verdict["claims_not_made"] == list(driver.LIMITATIONS)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("action_observation", None),
        ("after_commit", None),
        (
            "fixed_quorum",
            {
                "configured_quorum": 5,
                "invalid_records": [{"global_quorum": 4}],
            },
        ),
        (
            "conflicts",
            (
                {
                    "block_height": 11,
                    "hashes": ["1" * 64, "2" * 64],
                },
            ),
        ),
        ("runtime_error", "fault marker timeout"),
    ),
)
def test_verdict_refuses_missing_or_conflicting_evidence(
    field: str,
    value: object,
) -> None:
    driver = _driver()
    arguments = {
        "arm": _arm("static_persistent_omission"),
        "run_id": "run-1",
        "kauri_revision": "a" * 40,
        "action_observation": {"kind": "aggregate_omitted"},
        "before_commit": {"block_height": 10, "block_hash": "1" * 64},
        "after_commit": {"block_height": 11, "block_hash": "2" * 64},
        "fixed_quorum": {
            "configured_replica_count": 7,
            "configured_fault_threshold": 2,
            "configured_quorum": 5,
            "active_configuration_records": 7,
            "invalid_records": [],
        },
        "conflicts": (),
        "runtime_error": None,
    }
    arguments[field] = value

    assert driver.build_arm_verdict(**arguments)["verdict"] == "INCOMPLETE"


def test_conflicting_commit_scan_reports_only_same_height_hash_split() -> None:
    driver = _driver()

    def committed(height: int, block_hash: str) -> dict[str, object]:
        return {
            "event_type": "block.committed",
            "payload": {
                "block_height": height,
                "block_hash": block_hash,
            },
        }

    runner = SimpleNamespace(
        _commit_key=lambda event: (
            event["payload"]["block_height"],
            event["payload"]["block_hash"],
        )
    )
    streams = {
        "replica-0": [committed(4, "a" * 64)],
        "replica-1": [
            committed(4, "b" * 64),
            committed(5, "c" * 64),
        ],
        "adaptive-manager": [committed(4, "d" * 64)],
    }

    assert driver.conflicting_commits(runner, streams) == [
        {
            "block_height": 4,
            "hashes": [
                {
                    "block_hash": "a" * 64,
                    "sources": ["replica-0"],
                },
                {
                    "block_hash": "b" * 64,
                    "sources": ["replica-1"],
                },
            ],
        }
    ]


def test_common_commit_uses_runner_witness_contract() -> None:
    driver = _driver()
    observer = {
        "event_type": "block.committed",
        "payload": {
            "block_height": 9,
            "block_hash": "a" * 64,
        },
    }
    calls: dict[str, object] = {}

    def witnesses(streams, participants):
        calls["witness_participants"] = participants
        return {replica_id: {} for replica_id in participants}

    def find(observer_events, evidence, **kwargs):
        calls["observer_events"] = observer_events
        calls["evidence"] = evidence
        calls["kwargs"] = kwargs
        return SimpleNamespace(observer_event=observer, common_ns=123)

    runner = SimpleNamespace(
        commit_witness_timestamps=witnesses,
        find_first_common_epoch_commit=find,
        _commit_key=lambda event: (
            event["payload"]["block_height"],
            event["payload"]["block_hash"],
        ),
    )
    streams = {"replica-2": [observer]}

    assert driver.common_commit_after(
        runner,
        streams,
        participants=(0, 2, 3),
        after_ns=100,
    ) == {
        "block_height": 9,
        "block_hash": "a" * 64,
        "common_monotonic_raw_ns": 123,
        "participants": [0, 2, 3],
    }
    assert calls["witness_participants"] == (0, 2, 3)
    assert calls["kwargs"] == {
        "participants": (0, 2, 3),
        "epoch_number": 0,
        "strictly_after_ns": 100,
    }


def test_fault_marker_cursor_excludes_pre_baseline_marker(
    tmp_path: Path,
) -> None:
    driver = _driver()
    run_directory = tmp_path / "run"
    log_directory = run_directory / "logs"
    log_directory.mkdir(parents=True)
    log_path = log_directory / "replica-1.log"
    marker = (
        "KAURI_FAULT aggregate_omitted replica=1 parent=6 "
        "epoch=0 tree=6 block="
        + "a" * 64
        + " window="
        + driver.DIAGNOSTIC_WINDOW
        + "\n"
    )
    log_path.write_text(marker, encoding="utf-8")
    baseline_cursor = driver.fault_log_cursor(run_directory)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(marker.replace("a" * 64, "b" * 64))

    before = driver.find_fault_marker(
        run_directory,
        "static_persistent_omission",
        end_offset=baseline_cursor,
    )
    after = driver.find_fault_marker(
        run_directory,
        "static_persistent_omission",
        start_offset=baseline_cursor,
    )

    assert before is not None
    assert "a" * 64 in before["line"]
    assert after is not None
    assert after["matching_line_count"] == 1
    assert "b" * 64 in after["line"]


def test_fault_evidence_validation_binds_plan_and_terminal(
    tmp_path: Path,
) -> None:
    driver = _driver()
    faults = importlib.import_module(
        "experiments.adaptive.kauri_experiment.faults"
    )
    arm = _arm("static_persistent_omission")
    run_directory = tmp_path / "run"
    (run_directory / "raw").mkdir(parents=True)
    timestamps = iter((10, 20))
    with faults.FaultEvidence(
        run_directory,
        arm.plan,
        monotonic_ns=lambda: next(timestamps),
    ) as lifecycle:
        lifecycle.start(arm.plan.actions[0].fault_id)
        lifecycle.terminal(
            arm.plan.actions[0].fault_id,
            "succeeded",
        )

    assert driver.validate_fault_evidence(
        run_directory,
        arm,
        expected_status="succeeded",
    ) == {
        "plan_path": "fault-plan.json",
        "journal_path": "raw/fault-orchestrator.jsonl",
        "plan_sha256": arm.plan.sha256,
        "terminal_status": "succeeded",
        "journal_event_count": 2,
    }


def test_dry_run_persists_bundle_plan_journal_and_non_live_verdict(
    tmp_path: Path,
) -> None:
    driver = _driver()
    arm = _arm("static_authenticated_false_report")
    run_directory = tmp_path / "dry-run"
    (run_directory / "raw").mkdir(parents=True)

    def write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    timestamps = iter((10,))
    runner = SimpleNamespace(
        _write_json_exclusive=write_json,
        monotonic_raw_ns=lambda: next(timestamps),
    )

    assert driver._dry_run(
        runner=runner,
        arm=arm,
        run_directory=run_directory,
        revision="a" * 40,
        profile_path=tmp_path / "profile.json",
        profile_sha256="b" * 64,
        context_limit=4,
    ) == 0

    bundle = json.loads(
        (run_directory / "launch-bundle.json").read_text(
            encoding="utf-8"
        )
    )
    verdict = json.loads(
        (run_directory / "arm-verdict.json").read_text(
            encoding="utf-8"
        )
    )
    journal = [
        json.loads(line)
        for line in (
            run_directory / "raw" / "fault-orchestrator.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert bundle["fault_plan_sha256"] == arm.plan.sha256
    assert verdict["verdict"] == "DRY_RUN"
    assert verdict["action_observation"] is None
    assert journal[0]["lifecycle"] == "terminal"
    assert journal[0]["outcome"] == {"status": "not_reached"}


@pytest.mark.parametrize("context_limit", (0, -1, 1025, True))
def test_context_limit_is_positive_and_bounded(context_limit: object) -> None:
    driver = _driver()

    with pytest.raises(
        driver.ComparisonRunError,
        match="context limit",
    ):
        driver.replica_launch_overlays(
            _arm("static_persistent_omission"),
            context_limit=context_limit,
        )
