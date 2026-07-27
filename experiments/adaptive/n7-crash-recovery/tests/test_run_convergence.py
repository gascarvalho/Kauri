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
        "--experiment-drop-bundle-attempt",
        "2:1",
        "--experiment-drop-activation-ack",
        "5",
    )
    assert (
        runner.manager_convergence_arguments(profile)
        == convergence_arguments
    )
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
        manager_extra_args=convergence_arguments,
        **common,
    )
    assert convergence_command == default_command + convergence_arguments
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
    crash = source.index("crash_markers = base.inject_sigkill_crashes(")
    manifest = source.index("manifest = _manifest(", crash)
    audited_path = source[crash:manifest]
    assert "state[\"crash_markers\"] = crash_markers" in audited_path
    assert "state[\"crash_configuration_boundary\"] = boundary" in audited_path
    manifest_call = source[manifest:]
    assert "crash_markers=crash_markers" in manifest_call
    assert "crash_configuration_boundary=boundary" in manifest_call


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
