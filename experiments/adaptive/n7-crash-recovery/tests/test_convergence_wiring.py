"""Source contracts for deterministic experiment-only convergence loss."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import run as base


SCENARIO_DIRECTORY = Path(__file__).resolve().parents[1]
REPOSITORY = SCENARIO_DIRECTORY.parents[2]


def test_base_runner_has_one_default_off_manager_extra_args_seam() -> None:
    build = inspect.signature(base.build_manager_command).parameters
    runtime = inspect.signature(base.write_runtime_inputs).parameters

    assert "manager_extra_args" in build
    assert build["manager_extra_args"].default == ()
    assert "manager_extra_args" in runtime
    assert runtime["manager_extra_args"].default == ()

    source = (SCENARIO_DIRECTORY / "run.py").read_text(encoding="utf-8")
    assert "command.extend(manager_extra_args)" in source
    assert "manager_extra_args=manager_extra_args" in source


def test_manager_exposes_only_explicit_cli_loss_controls_and_audits_drops() -> None:
    source = (REPOSITORY / "examples/adaptation_manager.cpp").read_text(
        encoding="utf-8"
    )

    assert '"experiment-drop-bundle-attempt"' in source
    assert '"experiment-drop-activation-ack"' in source
    assert '"injected_drop"' in source
    assert '"ack_injected_drop"' in source
    assert '"ack_sent"' in source
    assert "canonical_payload_digest" in source
    assert "getenv(" not in source


def test_convergence_structured_event_carries_nullable_canonical_digest() -> None:
    header = (REPOSITORY / "include/hotstuff/structured_event.h").read_text(
        encoding="utf-8"
    )
    implementation = (REPOSITORY / "src/structured_event.cpp").read_text(
        encoding="utf-8"
    )

    assert "canonical_payload_digest" in header
    assert "canonical_payload_digest" in implementation
    assert '"injected_drop"' in implementation
    assert '"ack_injected_drop"' in implementation
    assert '"ack_sent"' in implementation


def test_convergence_runner_requests_exact_loss_controls_without_environment_flags() -> None:
    path = SCENARIO_DIRECTORY / "run_convergence.py"
    assert path.is_file(), "missing convergence-only N=7 runner"
    source = path.read_text(encoding="utf-8")

    assert "import run as base" in source
    assert '"--experiment-drop-bundle-attempt"' in source
    assert '"2:1"' in source
    assert '"--experiment-drop-activation-ack"' in source
    assert '"5"' in source
    assert "manager_extra_args=" in source
    assert "os.environ" not in source


def test_convergence_validator_does_not_reuse_throughput_pipeline() -> None:
    validator_path = SCENARIO_DIRECTORY / "convergence_validator.py"
    runner_path = SCENARIO_DIRECTORY / "run_convergence.py"
    validator_source = validator_path.read_text(encoding="utf-8")
    runner_source = runner_path.read_text(encoding="utf-8")
    imported_modules = {
        alias.name
        for node in ast.walk(ast.parse(validator_source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert imported_modules.isdisjoint({"analysis", "validator", "plot"})
    assert all(
        pipeline_path not in validator_source
        for pipeline_path in ("analysis.py", "validator.py", "plot.py")
    )
    assert "import run as base" not in validator_source
    assert "import run as base" in runner_source


def test_convergence_validator_has_no_synthetic_run_or_revision_alias() -> None:
    source = (SCENARIO_DIRECTORY / "convergence_validator.py").read_text(
        encoding="utf-8"
    )

    assert "_SYNTHETIC_MANIFEST_RUN_ID" not in source
    assert "_SYNTHETIC_EVENT_RUN_ID" not in source
    assert "_SYNTHETIC_REVISION" not in source
    assert "synthetic-validator-non-evidence" not in source
    assert '"2" * 40' not in source
