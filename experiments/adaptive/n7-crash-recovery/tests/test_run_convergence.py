"""Contract tests for the convergence-only N=7 runner."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

import convergence_synthetic_run as convergence_run
import run as base_runner


SCENARIO_DIRECTORY = Path(__file__).resolve().parents[1]


def _runner() -> Any:
    path = SCENARIO_DIRECTORY / "run_convergence.py"
    assert path.is_file(), "missing convergence-only N=7 runner"
    spec = importlib.util.spec_from_file_location("n7_convergence_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_repository_convergence_profile_is_the_exact_frozen_contract() -> None:
    path = SCENARIO_DIRECTORY / "convergence-profile.json"
    assert path.is_file(), "missing frozen convergence profile"
    payload = path.read_bytes()
    profile = json.loads(payload)

    assert profile == convergence_run.profile_document()
    runner = _runner()
    decoded, canonical = runner.load_frozen_profile(path)
    assert decoded == profile
    assert canonical == payload
    assert runner.PROFILE_ID == convergence_run.PROFILE_ID
    assert runner.PROFILE_SHA256 == hashlib.sha256(payload).hexdigest()


def test_convergence_profile_versions_the_live_manager_deadline() -> None:
    path = SCENARIO_DIRECTORY / "convergence-profile.json"
    profile = json.loads(path.read_bytes())

    assert profile["profile_id"] == "n7-f2-q5-epoch1-convergence-v3"
    assert (
        profile["fault_injection"]["activation_ack"][
            "accepted_activation_ordinal"
        ]
        == 5
    )
    assert profile["timeouts"]["manager_convergence_deadline_s"] == 120


def test_runner_reuses_existing_n7_helpers_without_mutating_base_profile() -> None:
    runner = _runner()

    assert runner.base is base_runner
    base_path = SCENARIO_DIRECTORY / "profile.json"
    before = hashlib.sha256(base_path.read_bytes()).hexdigest()
    runner.load_frozen_profile(
        SCENARIO_DIRECTORY / "convergence-profile.json"
    )
    assert hashlib.sha256(base_path.read_bytes()).hexdigest() == before
    assert before == base_runner.RECURRING_PROFILE_SHA256
    assert convergence_run.BASE_PROFILE_SHA256 == base_runner.PROFILE_SHA256


def test_runner_derives_one_exact_containment_request_from_recurring_profile() -> None:
    runner = _runner()
    base_profile = json.loads(
        (SCENARIO_DIRECTORY / "profile.json").read_bytes()
    )
    original = json.loads(json.dumps(base_profile))

    request = runner.convergence_transition_request(base_profile)
    runtime_profile = runner.convergence_runtime_profile(base_profile)

    assert request == {
        **base_profile["transition_requests"][0],
        "transition_artifact_id": "e0-to-e1-containment",
        "bundle_path": (
            "transitions/e0-to-e1-containment/successor.bundle"
        ),
        "predecessor_epoch_number": 0,
        "successor_epoch_number": 1,
        "policy_intent": "fault_containment",
    }
    assert runtime_profile["transition_requests"] == [request]
    assert len(base_profile["transition_requests"]) == 2
    assert base_profile == original

    request["policy_parameters"]["containment_baseline_roots"].clear()
    runtime_profile["transition_requests"][0]["bundle_path"] = "mutated"
    assert base_profile == original


def test_convergence_fault_plan_is_one_canonical_exact_profile_plan() -> None:
    runner = _runner()
    profile = convergence_run.profile_document()

    plan = runner.convergence_fault_plan(profile)

    assert plan.context == base_runner.n7_fault_context()
    assert plan.seed == base_runner.SNAPSHOT_SEED
    assert [
        (type(action).__name__, action.fault_id)
        for action in plan.actions
    ] == [
        ("ReplicaGroupSigkill", "crash-replica-0"),
        ("ReplicaGroupSigkill", "crash-replica-1"),
        ("SuccessorBundleAttemptDrop", "drop-successor-bundle-2-attempt-1"),
        ("ActivationAckDrop", "drop-activation-ack-quorum"),
    ]
    assert [
        action.replica_id
        for action in plan.actions_of_type(runner.ReplicaGroupSigkill)
    ] == [0, 1]
    bundle = plan.actions_of_type(runner.SuccessorBundleAttemptDrop)
    assert [(action.replica_id, action.attempt) for action in bundle] == [
        (2, 1)
    ]
    acknowledgement = plan.actions_of_type(runner.ActivationAckDrop)
    assert [
        action.accepted_activation_ordinal
        for action in acknowledgement
    ] == [base_runner.QUORUM]
    assert plan.manager_cli_args() == (
        "--experiment-drop-bundle-attempt",
        "2:1",
        "--experiment-drop-activation-ack",
        "5",
    )


def test_convergence_fault_plan_and_manager_arguments_require_exact_profile() -> None:
    runner = _runner()
    profile = convergence_run.profile_document()
    profile["fault_injection"]["activation_ack"][
        "accepted_activation_ordinal"
    ] = base_runner.QUORUM + 1

    with pytest.raises(runner.RunnerError):
        runner.convergence_fault_plan(profile)
    with pytest.raises(runner.RunnerError):
        runner.manager_convergence_arguments(profile)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing",
        "ambiguous",
        "mismatched_path",
    ),
)
def test_runner_rejects_non_exact_convergence_transition_contract(
    mutation: str,
) -> None:
    runner = _runner()
    profile = json.loads(
        (SCENARIO_DIRECTORY / "profile.json").read_bytes()
    )
    if mutation == "missing":
        profile.pop("transition_requests")
    elif mutation == "ambiguous":
        profile["transition_requests"].insert(
            1,
            json.loads(json.dumps(profile["transition_requests"][0])),
        )
    else:
        profile["transition_requests"][0]["bundle_path"] = (
            "transitions/e0-to-e1-containment/renamed.bundle"
        )

    with pytest.raises(runner.RunnerError):
        runner.convergence_transition_request(profile)
    with pytest.raises(runner.RunnerError):
        runner.convergence_runtime_profile(profile)


def test_runner_wires_only_the_exact_request_and_bundle_through_execution() -> None:
    runner = _runner()
    source = inspect.getsource(runner.run)

    runtime_profile = source.index(
        "runtime_profile = convergence_runtime_profile(base_profile)"
    )
    runtime_inputs = source.index(
        "base.write_runtime_inputs(", runtime_profile
    )
    launch = source.index('state["phase"] = "launch"', runtime_inputs)
    assert "runtime_profile," in source[runtime_inputs:launch]
    assert "base_profile," not in source[runtime_inputs:launch]

    bundle_read = source.index(
        "successor_bundle_path.read_bytes()", launch
    )
    manifest = source.index("manifest = _manifest(", bundle_read)
    assert '"successor.bundle"' not in source[launch:manifest]
    assert (
        "successor_bundle_path=successor_bundle_path"
        in source[manifest:]
    )


def _manifest_fixture(
    run_directory: Path,
) -> tuple[list[Any], dict[str, str], Path]:
    raw = run_directory / "raw"
    raw.mkdir(parents=True)
    process_records: list[Any] = []
    source_instances: dict[str, str] = {}
    for replica in base_runner.REPLICA_IDS:
        source_id = f"replica-{replica}"
        (raw / f"{source_id}.jsonl").write_text(
            f'{{"source_id":"{source_id}"}}\n',
            encoding="utf-8",
        )
        source_instances[source_id] = f"instance-{source_id}"
        process_records.append(
            SimpleNamespace(
                name=source_id,
                pid=10_000 + replica,
                pgid=20_000 + replica,
            )
        )
    manager = "adaptive-manager"
    (raw / f"{manager}.jsonl").write_text(
        f'{{"source_id":"{manager}"}}\n',
        encoding="utf-8",
    )
    source_instances[manager] = f"instance-{manager}"
    process_records.append(
        SimpleNamespace(name=manager, pid=30_000, pgid=40_000)
    )
    (run_directory / "convergence-profile.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (run_directory / "epochs.json").write_text("{}\n", encoding="utf-8")
    (run_directory / "runner-state.json").write_text(
        "{}\n", encoding="utf-8"
    )
    exact_bundle = (
        run_directory
        / "transitions"
        / "e0-to-e1-containment"
        / "successor.bundle"
    )
    exact_bundle.parent.mkdir(parents=True)
    return process_records, source_instances, exact_bundle


def _build_manifest(
    runner: Any,
    run_directory: Path,
    process_records: list[Any],
    source_instances: dict[str, str],
    successor_bundle_path: Path,
) -> dict[str, Any]:
    return runner._manifest(
        run_directory=run_directory,
        run_id="synthetic-convergence",
        revision="a" * 40,
        profile_bytes=b"{}\n",
        source_instances=source_instances,
        process_records=process_records,
        manager_command=(),
        manager_exit_code=0,
        crash_markers=(),
        crash_configuration_boundary={},
        successor_bundle_path=successor_bundle_path,
    )


def test_manifest_hashes_only_the_exact_qualified_containment_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    records, instances, exact_bundle = _manifest_fixture(tmp_path)
    exact_bundle.write_bytes(b"exact-qualified-bundle")
    legacy_bundle = tmp_path / "successor.bundle"
    legacy_bundle.write_bytes(b"stale-legacy-root-bundle")
    monkeypatch.setattr(
        base_runner, "normalized_manager_argv", lambda _command: []
    )

    manifest = _build_manifest(
        runner, tmp_path, records, instances, exact_bundle
    )

    artifact = next(
        item
        for item in manifest["artifacts"]
        if item["kind"] == "successor_bundle"
    )
    assert artifact == {
        "kind": "successor_bundle",
        "path": (
            "transitions/e0-to-e1-containment/successor.bundle"
        ),
        "sha256": hashlib.sha256(exact_bundle.read_bytes()).hexdigest(),
    }
    assert artifact["sha256"] != hashlib.sha256(
        legacy_bundle.read_bytes()
    ).hexdigest()


def test_manifest_rejects_missing_qualified_bundle_despite_legacy_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    records, instances, exact_bundle = _manifest_fixture(tmp_path)
    (tmp_path / "successor.bundle").write_bytes(b"stale-legacy-root-bundle")
    monkeypatch.setattr(
        base_runner, "normalized_manager_argv", lambda _command: []
    )

    with pytest.raises((runner.RunnerError, OSError)):
        _build_manifest(
            runner, tmp_path, records, instances, exact_bundle
        )


def test_base_runner_threads_optional_manager_extra_args_over_full_run_default(
    tmp_path: Path,
) -> None:
    runner = _runner()
    profile = convergence_run.profile_document()
    convergence_arguments = (
        "--convergence-deadline-seconds",
        "120",
    )
    plan_arguments = (
        "--experiment-drop-bundle-attempt",
        "2:1",
        "--experiment-drop-activation-ack",
        "5",
    )
    assert (
        runner.manager_convergence_arguments(profile)
        == convergence_arguments
    )
    assert (
        runner.convergence_fault_plan(profile).manager_cli_args()
        == plan_arguments
    )
    assert not hasattr(runner, "loss_control_arguments")
    common = {
        "replicas_tls": [{"crt": f"replica-{replica}.crt"} for replica in range(7)],
        "manager_tls": {"sec": "manager.key", "crt": "manager.crt"},
        "issuer": {"sec": "issuer.key"},
        "manager_port": 9000,
        "peer_port": 10000,
        "activation_delay_blocks": 5,
        "run_id": "synthetic-convergence",
        "source_instance": "synthetic-manager",
        "structured_event_path": tmp_path / "manager.jsonl",
        "bundle_path": tmp_path / "successor.bundle",
    }
    default_command = base_runner.build_manager_command(
        tmp_path / "adaptation-manager",
        **common,
    )
    assert "--convergence-deadline-seconds" not in default_command
    convergence_command = base_runner.build_manager_command(
        tmp_path / "adaptation-manager",
        manager_extra_args=(*convergence_arguments, *plan_arguments),
        **common,
    )
    assert convergence_command == (
        default_command + convergence_arguments + plan_arguments
    )
    runner._assert_convergence_manager_arguments(
        profile,
        runner.convergence_fault_plan(profile),
        convergence_command,
    )
    with pytest.raises(runner.RunnerError):
        runner._assert_convergence_manager_arguments(
            profile,
            runner.convergence_fault_plan(profile),
            (*convergence_command, "--experiment-drop-activation-ack", "5"),
        )
    parameter = inspect.signature(
        base_runner.write_runtime_inputs
    ).parameters["manager_extra_args"]
    assert parameter.default == base_runner.FULL_RUN_MANAGER_EXTRA_ARGS


@pytest.mark.parametrize(
    ("exit_code", "ready_count", "valid"),
    ((0, 1, True), (0, 0, False), (0, 2, False), (1, 1, False)),
)
def test_runner_accepts_manager_exit_zero_only_after_exactly_one_ready(
    exit_code: int, ready_count: int, valid: bool
) -> None:
    runner = _runner()

    if valid:
        runner.validate_manager_exit(exit_code, ready_count)
    else:
        with pytest.raises(runner.RunnerError):
            runner.validate_manager_exit(exit_code, ready_count)


def test_runner_uses_source_sequence_for_equal_timestamp_ack_recovery(
    tmp_path: Path,
) -> None:
    runner = _runner()
    manifest, _ = convergence_run.create_run(tmp_path / "equal-timestamps")
    document = convergence_run.load(manifest)
    manager_source = next(
        source
        for source in document["sources"]
        if source["source_id"] == "adaptive-manager"
    )
    events = [
        json.loads(line)
        for line in (manifest.parent / manager_source["path"])
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    ready = next(
        event for event in events if event["event_type"] == "adaptive_v2_ready"
    )
    recovered_ack = next(
        event
        for event in events
        if event["event_type"] == "adaptive_v2_activation_observed"
        and event["payload"].get("replica_id") == 6
        and event["payload"].get("disposition") == "ack_sent"
    )
    recovered_ack["source_monotonic_ns"] = ready["source_monotonic_ns"]

    loss_controls = runner._loss_control_state(events)

    assert loss_controls["activation_ack"]["observed"] is True
    assert (
        loss_controls["activation_ack"]["ack_source_monotonic_ns"]
        == ready["source_monotonic_ns"]
    )


def test_runner_preserves_exact_sigkill_markers_and_crash_boundary() -> None:
    runner = _runner()
    manifest_parameters = inspect.signature(runner._manifest).parameters
    source = inspect.getsource(runner.run)

    assert "crash_markers" in manifest_parameters
    assert "crash_configuration_boundary" in manifest_parameters
    crash = source.index("crash_markers = base.inject_fault_plan_crashes(")
    manifest = source.index("manifest = _manifest(", crash)
    audited_path = source[crash:manifest]
    assert "fault_registry" in audited_path
    assert "fault_plan" in audited_path
    assert "fault_journal" in audited_path
    assert "state[\"crash_markers\"] = crash_markers" in audited_path
    assert "state[\"crash_configuration_boundary\"] = boundary" in audited_path
    manifest_call = source[manifest:]
    assert "crash_markers=crash_markers" in manifest_call
    assert "crash_configuration_boundary=boundary" in manifest_call


def test_runner_wires_one_fault_plan_registry_and_journal_through_run() -> None:
    runner = _runner()
    source = inspect.getsource(runner.run)

    assert source.count("fault_plan = convergence_fault_plan(profile)") == 1
    assert "fault_plan=fault_plan" in source
    assert "fault_registry = base.ProcessRegistry(" in source
    assert "base.register_fault_replica(fault_registry, record)" in source
    manager_launch = source.index("manager = base.spawn_process(")
    replica_launch = source.index("for replica in base.REPLICA_IDS:")
    assert "register_fault_replica" not in source[
        manager_launch:replica_launch
    ]
    assert (
        'run_directory / "raw" / "fault-orchestrator.jsonl"'
        in source
    )
    assert "_assert_convergence_manager_arguments(" in source
    assert "base._shutdown_processes(records)" in source


def test_manager_fault_outcomes_are_terminal_only_after_exact_audit() -> None:
    runner = _runner()
    profile = convergence_run.profile_document()
    plan = runner.convergence_fault_plan(profile)
    events = sorted(
        convergence_run.manager_events(),
        key=lambda event: event["source_monotonic_ns"],
    )
    for sequence, event in enumerate(events, start=1):
        event["source_sequence"] = sequence
    loss_controls = runner._loss_control_state(events)
    appended: list[dict[str, Any]] = []

    class Journal:
        def append(self, **event: Any) -> None:
            appended.append(event)

    terminal_fault_ids: set[str] = set()
    runner._append_manager_fault_outcomes(
        plan,
        loss_controls,
        Journal(),
        terminal_fault_ids,
        manager_exit_code=0,
        manager_output_closed=True,
    )

    assert [event["fault_id"] for event in appended] == [
        "drop-successor-bundle-2-attempt-1",
        "drop-activation-ack-quorum",
    ]
    assert [event["lifecycle"] for event in appended] == [
        "terminal",
        "terminal",
    ]
    assert all(
        event["outcome"]["status"] == "succeeded"
        for event in appended
    )
    assert appended[0]["outcome"]["replica_id"] == 2
    assert appended[0]["outcome"]["attempt"] == 1
    assert appended[1]["outcome"]["accepted_activation_ordinal"] == 5
    assert appended[1]["outcome"]["replica_id"] == 6
    assert appended[1]["outcome"]["ack_source_monotonic_ns"] == 4_110_000_000
    assert terminal_fault_ids == {
        "drop-successor-bundle-2-attempt-1",
        "drop-activation-ack-quorum",
    }


@pytest.mark.parametrize(
    ("manager_exit_code", "manager_output_closed"),
    ((None, True), (1, True), (0, False)),
)
def test_manager_fault_success_requires_clean_exit_and_closed_output(
    manager_exit_code: int | None,
    manager_output_closed: bool,
) -> None:
    runner = _runner()
    plan = runner.convergence_fault_plan(convergence_run.profile_document())
    events = sorted(
        convergence_run.manager_events(),
        key=lambda event: event["source_monotonic_ns"],
    )
    for sequence, event in enumerate(events, start=1):
        event["source_sequence"] = sequence
    loss_controls = runner._loss_control_state(events)
    appended: list[dict[str, Any]] = []

    class Journal:
        def append(self, **event: Any) -> None:
            appended.append(event)

    with pytest.raises(runner.RunnerError, match="manager"):
        runner._append_manager_fault_outcomes(
            plan,
            loss_controls,
            Journal(),
            set(),
            manager_exit_code=manager_exit_code,
            manager_output_closed=manager_output_closed,
        )

    assert appended == []


@pytest.mark.parametrize(
    ("manager_launched", "expected_status"),
    ((False, "not_reached"), (True, "unobserved")),
)
def test_unproven_manager_faults_receive_non_success_terminal_outcomes(
    manager_launched: bool,
    expected_status: str,
) -> None:
    runner = _runner()
    plan = runner.convergence_fault_plan(convergence_run.profile_document())
    appended: list[dict[str, Any]] = []

    class Journal:
        def append(self, **event: Any) -> None:
            appended.append(event)

    terminal_fault_ids = {"drop-successor-bundle-2-attempt-1"}
    runner._append_pending_manager_fault_outcomes(
        plan,
        Journal(),
        terminal_fault_ids,
        manager_launched=manager_launched,
    )

    assert appended == [
        {
            "fault_id": "drop-activation-ack-quorum",
            "lifecycle": "terminal",
            "outcome": {
                "accepted_activation_ordinal": 5,
                "kind": "activation_ack_drop",
                "status": expected_status,
            },
        }
    ]
    assert terminal_fault_ids == {
        "drop-successor-bundle-2-attempt-1",
        "drop-activation-ack-quorum",
    }


def _exercise_cleanup_fault_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException | None,
    *,
    fail_runtime_inputs: bool = False,
    shutdown_failure: bool = False,
    expose_unclosed_manager_events: bool = False,
    manager_poll_available: bool = True,
    manager_poll_result: Any = 0,
    manager_output_closed: bool = True,
    complete_run: bool = False,
) -> tuple[int, dict[str, Any], list[dict[str, Any]], list[tuple[Any, ...]]]:
    runner = _runner()
    run_directory = tmp_path / "run"
    run_directory.mkdir(parents=True)
    profile = convergence_run.profile_document()
    base_profile = json.loads(
        (SCENARIO_DIRECTORY / "profile.json").read_bytes()
    )
    command = (
        "adaptation-manager",
        *runner.manager_convergence_arguments(profile),
        *runner.convergence_fault_plan(profile).manager_cli_args(),
    )
    trace: list[tuple[Any, ...]] = []
    shutdown_complete = False

    class Registry:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def register(
            self,
            *,
            name: str,
            replica_id: int,
            process: Any,
        ) -> Any:
            return SimpleNamespace(
                name=name,
                replica_id=replica_id,
                pid=process.pid,
                pgid=process.pid,
                process=process,
            )

        def sigkill_replica_groups(
            self,
            requests: tuple[tuple[str, int], ...],
            timeout_s: float,
        ) -> tuple[Any, ...]:
            assert timeout_s > 0
            return tuple(
                SimpleNamespace(
                    fault_id=fault_id,
                    name=f"replica-{replica_id}",
                    replica_id=replica_id,
                    pid=10_000 + replica_id,
                    pgid=10_000 + replica_id,
                    signal_number=9,
                    returncode=-9,
                    requested_monotonic_ns=100 + replica_id,
                    confirmed_monotonic_ns=200 + replica_id,
                )
                for fault_id, replica_id in requests
            )

        def sigkill_replica_group(
            self,
            *,
            fault_id: str,
            replica_id: int,
            timeout_s: float,
        ) -> Any:
            return self.sigkill_replica_groups(
                ((fault_id, replica_id),),
                timeout_s,
            )[0]

    real_journal = runner.FaultJournal

    class TracingJournal(real_journal):
        def append(self, **event: Any) -> None:
            trace.append(
                (
                    "journal",
                    event["fault_id"],
                    event["lifecycle"],
                    event.get("outcome", {}).get("status"),
                    shutdown_complete,
                )
            )
            super().append(**event)

    def spawn(name: str, *_args: Any, replica_id: int | None, **_kwargs: Any) -> Any:
        pid = 20_000 if replica_id is None else 10_000 + replica_id
        process = SimpleNamespace(pid=pid)
        if replica_id is None:
            if manager_poll_available:
                process.poll = lambda: manager_poll_result
        else:
            process.poll = lambda: (
                -9 if replica_id in base_runner.CRASH_TARGETS else None
            )
        return SimpleNamespace(
            name=name,
            replica_id=replica_id,
            pid=pid,
            pgid=pid,
            process=process,
            log_handle=SimpleNamespace(closed=manager_output_closed),
        )

    common_successor = SimpleNamespace(
        observer_event={
            "payload": {
                "block_height": 17,
                "block_hash": f"{17:064x}",
            }
        },
        common_ns=5_000_000_000,
    )
    waits = iter(
        (
            True,
            {},
            {"tree": 6},
            ({"epoch_number": 1}, {"ready": True}),
            common_successor,
        )
    )

    def wait(description: str, *_args: Any, **_kwargs: Any) -> Any:
        if (
            not fail_runtime_inputs
            and description == "exactly one manager convergence-ready event"
            and not complete_run
        ):
            assert failure is not None
            raise failure
        return next(waits)

    manager_events = sorted(
        convergence_run.manager_events(),
        key=lambda event: event["source_monotonic_ns"],
    )
    for sequence, event in enumerate(manager_events, start=1):
        event["source_sequence"] = sequence

    def event_streams(_directory: Path) -> dict[str, list[dict[str, Any]]]:
        trace.append(("read-events", shutdown_complete))
        return {
            runner.MANAGER_SOURCE_ID: (
                manager_events
                if (
                    shutdown_complete
                    or expose_unclosed_manager_events
                    or complete_run
                )
                else []
            ),
            **{
                f"replica-{replica_id}": []
                for replica_id in base_runner.REPLICA_IDS
            },
        }

    def shutdown(_records: Any) -> list[str]:
        nonlocal shutdown_complete
        trace.append(("shutdown",))
        if shutdown_failure:
            raise base_runner.RunnerError("forced shutdown failure")
        shutdown_complete = True
        return []

    def write_runtime_inputs(*_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
        if fail_runtime_inputs:
            assert failure is not None
            raise failure
        successor_bundle = (
            run_directory
            / "transitions"
            / "e0-to-e1-containment"
            / "successor.bundle"
        )
        successor_bundle.parent.mkdir(parents=True, exist_ok=True)
        successor_bundle.write_bytes(b"synthetic-successor-bundle")
        return (
            {},
            {},
            command,
            {
                replica_id: ("replica",)
                for replica_id in base_runner.REPLICA_IDS
            },
            [],
        )

    args = SimpleNamespace(
        repository=tmp_path,
        profile=tmp_path / "convergence-profile.json",
        base_profile=tmp_path / "profile.json",
        app_binary=tmp_path / "hotstuff-app",
        manager_binary=tmp_path / "adaptation-manager",
        keygen_binary=tmp_path / "hotstuff-keygen",
        tls_keygen_binary=tmp_path / "hotstuff-tls-keygen",
        results_root=tmp_path,
        peer_port=28_100,
        client_port=29_100,
        manager_port=30_100,
    )
    monkeypatch.setattr(runner, "_arguments", lambda _argv: args)
    monkeypatch.setattr(
        runner,
        "load_frozen_profile",
        lambda _path: (profile, b"convergence-profile\n"),
    )
    monkeypatch.setattr(
        base_runner,
        "load_frozen_profile",
        lambda _path: (base_profile, b"base-profile\n"),
    )
    monkeypatch.setattr(
        base_runner,
        "verify_repository_state",
        lambda _repository: SimpleNamespace(revision="a" * 40),
    )
    monkeypatch.setattr(base_runner, "_assert_executable", lambda *_args: None)
    monkeypatch.setattr(base_runner, "required_ports", lambda *_args: ())
    monkeypatch.setattr(base_runner, "ports_in_use", lambda _ports: set())
    monkeypatch.setattr(
        base_runner, "create_run_directory", lambda _root: run_directory
    )
    monkeypatch.setattr(
        base_runner,
        "generate_identities",
        lambda *_args: ([], [], {}),
    )
    monkeypatch.setattr(
        base_runner,
        "write_runtime_inputs",
        write_runtime_inputs,
    )
    monkeypatch.setattr(base_runner, "ProcessRegistry", Registry)
    monkeypatch.setattr(base_runner, "spawn_process", spawn)
    monkeypatch.setattr(base_runner, "_wait", wait)
    monkeypatch.setattr(
        base_runner,
        "runtime_parameters",
        lambda _profile: {"aggregation_timeout_ms": 100},
    )
    monkeypatch.setattr(
        base_runner,
        "replica_event_tail_snapshot",
        lambda _directory: ({}, {}),
    )
    monkeypatch.setattr(
        base_runner,
        "FreshConfigurationPoller",
        lambda *_args, **_kwargs: SimpleNamespace(poll=lambda: None),
    )
    monkeypatch.setattr(
        base_runner, "assert_crash_boundary_held", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        base_runner, "decode_epoch_change_bundle", lambda _payload: {}
    )
    monkeypatch.setattr(
        base_runner,
        "build_epochs_document",
        lambda _decoded, _command: {},
    )
    monkeypatch.setattr(base_runner, "_event_streams", event_streams)
    monkeypatch.setattr(base_runner, "_shutdown_processes", shutdown)
    monkeypatch.setattr(
        base_runner, "_wait_listeners_stopped", lambda *_args: []
    )
    monkeypatch.setattr(runner, "FaultJournal", TracingJournal)
    if complete_run:
        monkeypatch.setattr(
            runner,
            "_manifest",
            lambda **_kwargs: trace.append(("manifest",)) or {},
        )
        monkeypatch.setattr(
            runner.subprocess,
            "run",
            lambda *_args, **_kwargs: (
                trace.append(("validator",))
                or SimpleNamespace(returncode=0)
            ),
        )

    try:
        result = runner.run([])
    except KeyboardInterrupt as exc:
        raise AssertionError(
            "convergence runner leaked KeyboardInterrupt"
        ) from exc
    state = json.loads(
        (run_directory / "runner-state.json").read_text(encoding="utf-8")
    )
    events = [
        json.loads(line)
        for line in (
            run_directory / "raw" / "fault-orchestrator.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    return result, state, events, trace


def test_runner_drains_and_rereads_manager_before_fault_terminalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, state, events, trace = _exercise_cleanup_fault_finalization(
        tmp_path,
        monkeypatch,
        base_runner.RunnerError("forced pre-drain failure"),
    )

    assert result == 1
    assert state["runtime_error"] == "forced pre-drain failure"
    assert ("read-events", True) in trace
    manager_terminals = [
        event
        for event in events
        if event["fault_id"].startswith("drop-")
        and event["lifecycle"] == "terminal"
    ]
    assert [event["outcome"]["status"] for event in manager_terminals] == [
        "succeeded",
        "succeeded",
    ]
    assert all(
        item[-1] is True
        for item in trace
        if item[:3] in (
            ("journal", "drop-successor-bundle-2-attempt-1", "terminal"),
            ("journal", "drop-activation-ack-quorum", "terminal"),
        )
    )


def test_runner_does_not_infer_manager_success_without_integer_poll_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    poll_cases = (
        {"manager_poll_available": False},
        {"manager_poll_result": None},
        {"manager_poll_result": "0"},
    )

    for index, poll_case in enumerate(poll_cases):
        result, state, events, _trace = _exercise_cleanup_fault_finalization(
            tmp_path / f"poll-case-{index}",
            monkeypatch,
            base_runner.RunnerError("forced pre-drain failure"),
            **poll_case,
        )

        assert result != 0
        assert state["runtime_error"] == "forced pre-drain failure"
        manager_statuses = [
            event["outcome"]["status"]
            for event in events
            if event["fault_id"].startswith("drop-")
            and event["lifecycle"] == "terminal"
        ]
        assert manager_statuses == ["unobserved", "unobserved"]

    assert "INCOMPLETE:" in capsys.readouterr().out


def test_runner_rejects_exact_zero_exit_when_manager_output_is_not_drained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, state, events, trace = _exercise_cleanup_fault_finalization(
        tmp_path,
        monkeypatch,
        None,
        manager_poll_result=0,
        manager_output_closed=False,
        complete_run=True,
    )

    assert result != 0
    assert state["runtime_error"] is not None
    assert (
        "output" in state["runtime_error"].lower()
        or "drain" in state["runtime_error"].lower()
    )
    manager_statuses = [
        event["outcome"]["status"]
        for event in events
        if event["fault_id"].startswith("drop-")
        and event["lifecycle"] == "terminal"
    ]
    assert manager_statuses == ["unobserved", "unobserved"]
    assert not any(status == "succeeded" for status in manager_statuses)
    assert ("manifest",) not in trace
    assert ("validator",) not in trace
    assert "INCOMPLETE:" in capsys.readouterr().out


def test_runtime_input_failure_preserves_plan_and_terminalizes_every_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, state, events, _trace = _exercise_cleanup_fault_finalization(
        tmp_path,
        monkeypatch,
        OSError("forced runtime-input failure"),
        fail_runtime_inputs=True,
    )
    runner = _runner()
    plan = runner.convergence_fault_plan(convergence_run.profile_document())

    assert result == 1
    assert state["runtime_error"] == "forced runtime-input failure"
    assert (tmp_path / "run" / "fault-plan.json").read_bytes() == (
        plan.canonical_json().encode("utf-8")
    )
    terminals = [
        event for event in events if event["lifecycle"] == "terminal"
    ]
    assert [
        (event["fault_id"], event["outcome"]["status"])
        for event in terminals
    ] == [
        (action.fault_id, "not_reached") for action in plan.actions
    ]
    assert {event["plan_sha256"] for event in terminals} == {plan.sha256}


def test_failed_shutdown_cannot_audit_success_from_unclosed_manager_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, state, events, trace = _exercise_cleanup_fault_finalization(
        tmp_path,
        monkeypatch,
        base_runner.RunnerError("forced pre-drain failure"),
        shutdown_failure=True,
        expose_unclosed_manager_events=True,
    )

    assert result == 1
    assert state["runtime_error"] == "forced pre-drain failure"
    assert ("read-events", False) in trace or not any(
        item[0] == "read-events" for item in trace
    )
    manager_statuses = [
        event["outcome"]["status"]
        for event in events
        if event["fault_id"].startswith("drop-")
        and event["lifecycle"] == "terminal"
    ]
    assert manager_statuses == ["unobserved", "unobserved"]


def test_runner_keyboard_interrupt_preserves_error_and_complete_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, state, events, _trace = _exercise_cleanup_fault_finalization(
        tmp_path,
        monkeypatch,
        KeyboardInterrupt(),
    )

    assert result == 1
    assert state["phase"] == "finished"
    assert "interrupt" in state["runtime_error"].lower()
    terminal_counts = {
        action.fault_id: sum(
            event["fault_id"] == action.fault_id
            and event["lifecycle"] == "terminal"
            for event in events
        )
        for action in _runner()
        .convergence_fault_plan(convergence_run.profile_document())
        .actions
    }
    assert set(terminal_counts.values()) == {1}


def test_runner_manifest_sources_record_process_pid_and_pgid() -> None:
    runner = _runner()
    source = inspect.getsource(runner._manifest)

    assert '"pid"' in source
    assert '"pgid"' in source


def test_runner_waits_for_and_records_common_epoch_one_commit_after_ack_drain() -> None:
    runner = _runner()
    source = inspect.getsource(runner.run)

    ack_drain = source.index("manager_exit_code = _wait_manager_exit(")
    common_commit = source.index(
        "base.find_first_common_epoch_commit(", ack_drain
    )
    manifest = source.index("manifest = _manifest(", common_commit)
    audited_path = source[common_commit:manifest]
    assert "participants=base.SURVIVORS" in audited_path
    assert "epoch_number=1" in audited_path
    assert "strictly_after_ns=" in audited_path
    assert "ack_source_monotonic_ns" in audited_path
    assert "base._wait(" in audited_path
    assert "first_common_successor_commit" in audited_path
    assert ".common_ns" in audited_path


def test_loss_control_state_records_the_exact_ack_recovery_boundary() -> None:
    runner = _runner()
    events = sorted(
        convergence_run.manager_events(),
        key=lambda event: event["source_monotonic_ns"],
    )
    for sequence, event in enumerate(events, start=1):
        event["source_sequence"] = sequence

    state = runner._loss_control_state(events)

    assert state["activation_ack"]["ack_source_monotonic_ns"] == 4_110_000_000


def test_common_commit_helper_skips_pre_boundary_history() -> None:
    boundary_ns = 4_110_000_000
    earlier_key = (17, f"{17:064x}")
    later_key = (18, f"{18:064x}")

    def committed(key: tuple[int, str], timestamp_ns: int) -> dict[str, Any]:
        height, block_hash = key
        return {
            "event_type": "block.committed",
            "source_monotonic_ns": timestamp_ns,
            "payload": {
                "block_height": height,
                "block_hash": block_hash,
                "decision_proof": {"epoch_number": 1},
            },
        }

    witnesses = {
        replica: {
            earlier_key: boundary_ns - 20_000_000 + replica,
            later_key: boundary_ns + 20_000_000 + replica,
        }
        for replica in convergence_run.SURVIVORS
    }
    result = base_runner.find_first_common_epoch_commit(
        (
            committed(earlier_key, boundary_ns - 10_000_000),
            committed(later_key, boundary_ns + 10_000_000),
        ),
        witnesses,
        participants=convergence_run.SURVIVORS,
        epoch_number=1,
        strictly_after_ns=boundary_ns,
    )

    assert result is not None
    assert result.observer_event["payload"]["block_height"] == later_key[0]
    assert result.observer_event["payload"]["block_hash"] == later_key[1]


def test_runner_uses_a_separate_convergence_result_root_and_state_artifact() -> None:
    _runner()
    source = (SCENARIO_DIRECTORY / "run_convergence.py").read_text(
        encoding="utf-8"
    )
    assert "import run as base" in source
    assert "n7-epoch1-convergence" in source
    assert "runner-state.json" in source
    assert "canonical_payload_digest" in source
    assert "throughput.csv" not in source
    assert "plot.py" not in source
    assert "figure" not in source.lower()
