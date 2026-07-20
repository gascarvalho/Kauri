"""Contract tests for the convergence-only N=7 runner."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys
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

    assert profile["profile_id"] == "n7-f2-q5-epoch1-convergence-v2"
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
    assert before == convergence_run.BASE_PROFILE_SHA256


def test_base_runner_threads_optional_manager_extra_args_without_changing_default(
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
        "1",
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
    assert parameter.default == ()


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

    state = runner._loss_control_state(convergence_run.manager_events())

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
