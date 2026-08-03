"""Fail-closed synthetic verdict tests for the N=31 crash gate."""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
from typing import Callable

import pytest

SHIPPED_PROFILE = (
    Path(__file__).parents[1] / "profiles" / "n31-f5-crash-shakedown-v1.json"
)
INTERNAL1_PROFILE = (
    Path(__file__).parents[1]
    / "profiles"
    / "n31-f5-internal1-crash-shakedown-v1.json"
)
INTERNAL1_DIAGNOSTIC_PROFILE = (
    Path(__file__).parents[1]
    / "profiles"
    / "n31-f5-internal1-handoff-diagnostic-v1.json"
)
INTERNAL1_TAIL_DIAGNOSTIC_PROFILE = (
    Path(__file__).parents[1]
    / "profiles"
    / "n31-f5-internal1-handoff-tail-diagnostic-v2.json"
)
INTERNAL1_FORWARDING_TAIL_DIAGNOSTIC_PROFILE = (
    Path(__file__).parents[1]
    / "profiles"
    / "n31-f5-internal1-forwarding-tail-diagnostic-v3.json"
)
INTERNAL1_REPNET2_DIAGNOSTIC_PROFILE = (
    Path(__file__).parents[1]
    / "profiles"
    / "n31-f5-internal1-repnet2-diagnostic-v4.json"
)
INTERNAL1_MISSING_SIGNER_REPAIR_DIAGNOSTIC_PROFILE = (
    Path(__file__).parents[1]
    / "profiles"
    / "n31-f5-internal1-missing-signer-repair-diagnostic-v5.json"
)
INTERNAL1_STAGED_REPAIR_DIAGNOSTIC_PROFILE = (
    Path(__file__).parents[1]
    / "profiles"
    / "n31-f5-internal1-staged-repair-diagnostic-v6.json"
)
INTERNAL1_COMMIT_DWELL_DIAGNOSTIC_PROFILE = (
    Path(__file__).parents[1]
    / "profiles"
    / "n31-f5-internal1-commit-dwell-diagnostic-v7.json"
)
INTERNAL1_ACK_TAIL_DIAGNOSTIC_PROFILE = (
    Path(__file__).parents[1]
    / "profiles"
    / "n31-f5-internal1-ack-tail-diagnostic-v8.json"
)


def _api():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.profiled_fault_evaluation"
    )


def _runtime():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.profiled_fault_runtime"
    )


def _runner():
    return importlib.import_module("experiments.adaptive.run_profiled_fault_evaluation")


def _load_profile(tmp_path: Path):
    value = {
        "schema_version": 1,
        "profile_id": "n31-f5-q21-sigkill-shakedown-v1",
        "frozen": True,
        "replica_ids": list(range(31)),
        "fault_threshold": 10,
        "quorum": 21,
        "fanout": 5,
        "pipeline_depth": 2,
        "epoch0_roots": list(range(31)),
        "epoch0_members_breadth_first": [30] + list(range(30)),
        "authoritative_observer": 2,
        "snapshot_seed": 41719,
        "ports": {
            "peer_base": 25100,
            "client_base": 26100,
            "manager": 27100,
        },
        "fault": {
            "fault_id": "single-internal-sigkill",
            "kind": "replica_group_sigkill",
            "replica_id": 0,
            "tree_id": 30,
        },
        "attempt_count": 1,
        "retry_failed_attempts": False,
        "require_successor_activation": False,
    }
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return _api().load_frozen_profile(path)


def _commit(
    source: int,
    event_type: str,
    timestamp_ns: int,
    height: int,
    block_hash: str,
) -> dict[str, object]:
    return {
        "event_schema_version": 1,
        "run_id": "run-n31-pass",
        "source_kind": "replica",
        "source_id": f"replica-{source}",
        "source_instance": f"run-n31-pass-replica-{source}",
        "source_sequence": height,
        "source_monotonic_ns": timestamp_ns,
        "event_type": event_type,
        "payload": {
            "block_height": height,
            "block_hash": block_hash,
        },
    }


def _artifacts(profile):
    api = _api()
    plan = api.build_fault_plan(profile)
    assert len(plan.actions) == 1
    assert plan.context.quorum == 21
    assert plan.actions[0].replica_id == 0

    manifest = {
        "schema_version": 1,
        "run_id": "run-n31-pass",
        "kauri_revision": "a" * 40,
        "profile": {
            "profile_id": profile.profile_id,
            "sha256": profile.profile_sha256,
        },
        "attempt": 1,
        "runtime": {
            "replica_ids": list(range(31)),
            "fault_threshold": 10,
            "quorum": 21,
            "fanout": 5,
            "pipeline_depth": 2,
        },
    }
    journal = [
        {
            "schema_version": 1,
            "source_id": "fault-orchestrator",
            "source_sequence": 0,
            "source_monotonic_ns": 100,
            "fault_id": "single-internal-sigkill",
            "plan_sha256": plan.sha256,
            "lifecycle": "started",
        },
        {
            "schema_version": 1,
            "source_id": "fault-orchestrator",
            "source_sequence": 1,
            "source_monotonic_ns": 110,
            "fault_id": "single-internal-sigkill",
            "plan_sha256": plan.sha256,
            "lifecycle": "terminal",
            "outcome": {
                "status": "succeeded",
                "replica_id": 0,
                "signal_number": 9,
                "returncode": -9,
            },
        },
    ]
    streams: dict[str, list[dict[str, object]]] = {
        f"replica-{replica}": [] for replica in range(31)
    }
    streams["replica-2"].extend(
        (
            _commit(2, "block.committed", 50, 100, "a" * 64),
            _commit(2, "block.committed", 200, 101, "b" * 64),
        )
    )
    for replica in api.postfault_witnesses(profile):
        streams[f"replica-{replica}"].extend(
            (
                _commit(
                    replica,
                    "block.commit_observed",
                    60,
                    100,
                    "a" * 64,
                ),
                _commit(
                    replica,
                    "block.commit_observed",
                    210,
                    101,
                    "b" * 64,
                ),
            )
        )
    exits = [
        {
            "name": "replica-0",
            "replica_id": 0,
            "returncode": -9,
        }
    ]
    return manifest, journal, streams, exits


def test_synthetic_pass_requires_one_sigkill_and_quorum_common_commits(
    tmp_path: Path,
) -> None:
    api = _api()
    profile = _load_profile(tmp_path)
    manifest, journal, streams, exits = _artifacts(profile)

    verdict = api.validate_synthetic_run(
        profile,
        manifest=manifest,
        fault_journal=journal,
        streams=streams,
        process_exits=exits,
    )

    assert verdict["verdict"] == "PASS"
    assert verdict["claim_scope"] == "n31-fanout5-crash-shakedown"
    assert verdict["fault"]["terminal_status"] == "succeeded"
    assert verdict["pre_fault_common_commit"]["witness_count"] == 21
    assert verdict["post_fault_common_commit"]["witness_count"] == 21
    assert verdict["all_survivor_post_fault_common_commit"] is None
    assert verdict["successor_activation_required"] is False
    assert verdict["attempt_count"] == 1


def _profile_drift(manifest, _journal, _streams, _exits) -> None:
    manifest["profile"]["sha256"] = "f" * 64


def _conflict(_manifest, _journal, streams, _exits) -> None:
    streams["replica-2"].append(_commit(2, "block.committed", 205, 101, "c" * 64))


def _extra_exit(_manifest, _journal, _streams, exits) -> None:
    exits.append({"name": "replica-3", "replica_id": 3, "returncode": 1})


def _retry(manifest, _journal, _streams, _exits) -> None:
    manifest["attempt"] = 2


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        (_profile_drift, "profile|sha"),
        (_conflict, "conflict"),
        (_extra_exit, "exit"),
        (_retry, "attempt|retry"),
    ),
)
def test_synthetic_verdict_rejects_drift_conflict_extra_exit_and_retry(
    tmp_path: Path,
    mutation: Callable[..., None],
    error: str,
) -> None:
    api = _api()
    profile = _load_profile(tmp_path)
    manifest, journal, streams, exits = copy.deepcopy(_artifacts(profile))
    mutation(manifest, journal, streams, exits)

    with pytest.raises(api.ProfiledFaultEvaluationError, match=error):
        api.validate_synthetic_run(
            profile,
            manifest=manifest,
            fault_journal=journal,
            streams=streams,
            process_exits=exits,
        )


def test_repository_verification_excludes_only_known_clean_noise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    revision = "a" * 40
    observed: list[tuple[str, ...]] = []

    def fake_git(_repository: Path, arguments: tuple[str, ...]) -> str:
        observed.append(arguments)
        if arguments == ("rev-parse", "--show-toplevel"):
            return str(tmp_path.resolve())
        if arguments == ("branch", "--show-current"):
            return runtime.REQUIRED_BRANCH
        if arguments in {
            ("rev-parse", "HEAD"),
            ("rev-parse", f"origin/{runtime.REQUIRED_BRANCH}"),
        }:
            return revision
        if arguments[0] == "status":
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(runtime, "_git", fake_git)

    assert runtime.verify_repository_state(tmp_path) == revision
    status = next(arguments for arguments in observed if arguments[0] == "status")
    assert status == (
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        ".",
        ":(exclude).codex",
        ":(exclude)build/Testing",
    )


def test_transition_request_is_zero_residency_fault_containment() -> None:
    request = _runtime().transition_request()

    assert request["policy_intent"] == "fault_containment"
    assert request["predecessor_epoch_number"] == 0
    assert request["successor_epoch_number"] == 1
    assert request["minimum_predecessor_residency_ms"] == 0
    assert request["policy_parameters"]["containment_baseline_roots"] == [
        {"tree_id": replica, "replica_id": replica} for replica in range(21)
    ]


@pytest.mark.parametrize(
    "profile_path",
    (
        SHIPPED_PROFILE,
        INTERNAL1_PROFILE,
        INTERNAL1_DIAGNOSTIC_PROFILE,
        INTERNAL1_TAIL_DIAGNOSTIC_PROFILE,
        INTERNAL1_FORWARDING_TAIL_DIAGNOSTIC_PROFILE,
        INTERNAL1_REPNET2_DIAGNOSTIC_PROFILE,
        INTERNAL1_MISSING_SIGNER_REPAIR_DIAGNOSTIC_PROFILE,
        INTERNAL1_STAGED_REPAIR_DIAGNOSTIC_PROFILE,
        INTERNAL1_COMMIT_DWELL_DIAGNOSTIC_PROFILE,
        INTERNAL1_ACK_TAIL_DIAGNOSTIC_PROFILE,
    ),
)
def test_cli_pins_the_exact_shipped_profiles(profile_path: Path) -> None:
    runner = _runner()
    profile = _api().load_frozen_profile(profile_path)

    assert runner.EXPECTED_PROFILES[profile.profile_id] == profile.profile_sha256
    runner._verify_shipped_profile(profile_path)


def test_n31_config_and_argv_cardinality(tmp_path: Path) -> None:
    evaluation = _api()
    runtime = _runtime()
    profile = evaluation.load_frozen_profile(SHIPPED_PROFILE)
    bls = [
        {"pub": f"bls-pub-{replica}", "sec": f"bls-sec-{replica}"}
        for replica in profile.replica_ids
    ]
    tls = [
        {
            "crt": f"tls-crt-{identity}",
            "sec": f"tls-sec-{identity}",
            "cid": f"tls-cid-{identity}",
        }
        for identity in range(len(profile.replica_ids) + 1)
    ]
    issuer = {"pub": "issuer-pub", "sec": "issuer-sec"}

    main_config = runtime.main_config_payload(
        profile,
        bls=bls,
        tls=tls,
        issuer=issuer,
    ).decode()
    replica_commands = evaluation.replica_argvs(
        profile,
        app_binary=Path("/build/hotstuff-app"),
        config_directory=Path("/run/config"),
    )
    manager_command = runtime.manager_argv(
        profile,
        manager_binary=Path("/build/adaptation-manager"),
        tls=tls,
        issuer=issuer,
        run_directory=tmp_path,
        run_id="run-n31",
        source_instance="run-n31-manager",
    )

    assert sum(line.startswith("replica = ") for line in main_config.splitlines()) == 31
    stat_periods = [
        float(line.partition("=")[2])
        for line in main_config.splitlines()
        if line.startswith("stat-period = ")
    ]
    assert stat_periods == [profile.hard_timeout_s + 60.0]
    assert [
        line for line in main_config.splitlines()
        if line.startswith("repnworker = ")
    ] == ["repnworker = 2"]
    assert runtime.effective_runtime(profile)[
        "replica_network_workers"
    ] == 2
    assert len(replica_commands) == 31
    assert len(set(replica_commands)) == 31
    assert manager_command.count("--replica") == 31
    assert manager_command.count("--transition-request") == 1


def _cli_arguments(
    command: str,
    tmp_path: Path,
) -> list[str]:
    return [
        command,
        "--profile",
        str(tmp_path / "profile.json"),
        "--repository",
        str(tmp_path / "repository"),
        "--results-root",
        str(tmp_path / "results"),
        "--build-directory",
        str(tmp_path / "repository" / "build-adaptive"),
        "--app-binary",
        str(tmp_path / "hotstuff-app"),
        "--manager-binary",
        str(tmp_path / "adaptation-manager"),
        "--keygen-binary",
        str(tmp_path / "hotstuff-keygen"),
        "--tls-keygen-binary",
        str(tmp_path / "hotstuff-tls-keygen"),
        "--epoch-profile-digest-binary",
        str(tmp_path / "epoch-profile-digest"),
    ]


def test_cli_preflight_forwards_exact_runtime_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _runner()
    calls: list[dict[str, Path]] = []
    monkeypatch.setattr(runner, "_verify_shipped_profile", lambda _path: None)
    prepared: list[dict[str, Path]] = []
    monkeypatch.setattr(
        runner.runtime,
        "prepare_exact_revision_build",
        lambda **kwargs: prepared.append(kwargs),
    )
    monkeypatch.setattr(
        runner.runtime,
        "preflight",
        lambda **kwargs: calls.append(kwargs)
        or {"verdict": "PASS", "profile_id": "n31"},
    )
    monkeypatch.setattr(
        runner.runtime,
        "run_once",
        lambda **_kwargs: pytest.fail("preflight must not launch a run"),
    )

    assert runner.main(_cli_arguments("preflight", tmp_path)) == 0

    output = json.loads(capsys.readouterr().out)
    assert output == {"profile_id": "n31", "verdict": "PASS"}
    assert len(calls) == 1
    assert prepared == [
        {
            "repository": (tmp_path / "repository").resolve(),
            "build_directory": (tmp_path / "repository" / "build-adaptive").resolve(),
        }
    ]
    assert calls[0] == {
        "profile_path": (tmp_path / "profile.json").resolve(),
        "repository": (tmp_path / "repository").resolve(),
        "app_binary": (tmp_path / "hotstuff-app").resolve(),
        "manager_binary": (tmp_path / "adaptation-manager").resolve(),
        "keygen_binary": (tmp_path / "hotstuff-keygen").resolve(),
        "tls_keygen_binary": (tmp_path / "hotstuff-tls-keygen").resolve(),
        "epoch_profile_digest_binary": (tmp_path / "epoch-profile-digest").resolve(),
        "build_directory": (tmp_path / "repository" / "build-adaptive").resolve(),
        "build_provenance_path": (
            tmp_path
            / "repository"
            / "build-adaptive"
            / runner.runtime.BUILD_PROVENANCE_FILENAME
        ).resolve(),
    }


@pytest.mark.parametrize(
    ("verdict", "expected_exit"),
    (("PASS", 0), ("FAIL", 1), ("INCOMPLETE", 1)),
)
def test_cli_run_calls_once_without_retry_and_maps_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verdict: str,
    expected_exit: int,
) -> None:
    runner = _runner()
    calls: list[dict[str, Path]] = []
    run_directory = tmp_path / "results" / "attempt-1"
    monkeypatch.setattr(runner, "_verify_shipped_profile", lambda _path: None)
    monkeypatch.setattr(
        runner.runtime,
        "run_once",
        lambda **kwargs: calls.append(kwargs) or (run_directory, verdict),
    )
    monkeypatch.setattr(
        runner.runtime,
        "preflight",
        lambda **_kwargs: pytest.fail("run must delegate to run_once"),
    )

    assert runner.main(_cli_arguments("run", tmp_path)) == expected_exit

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "run_directory": str(run_directory),
        "verdict": verdict,
    }
    assert len(calls) == 1
    assert calls[0]["results_root"] == (tmp_path / "results").resolve()


def test_cli_validate_rechecks_only_the_preserved_run_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _runner()
    run_directory = tmp_path / "sealed-run"
    calls: list[Path] = []
    monkeypatch.setattr(
        runner.runtime,
        "validate_preserved_run",
        lambda path: calls.append(path)
        or {"verdict": "PASS", "evidence_tree_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        runner.runtime,
        "prepare_exact_revision_build",
        lambda **_kwargs: pytest.fail("validate must not build"),
    )

    assert runner.main(["validate", "--run-directory", str(run_directory)]) == 0

    assert calls == [run_directory.resolve()]
    assert json.loads(capsys.readouterr().out)["verdict"] == "PASS"


def test_cli_run_rejection_is_json_and_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _runner()
    calls = 0
    monkeypatch.setattr(runner, "_verify_shipped_profile", lambda _path: None)

    def reject(**_kwargs: Path) -> tuple[Path, str]:
        nonlocal calls
        calls += 1
        raise runner.runtime.ProfiledFaultRuntimeError("unsafe preflight")

    monkeypatch.setattr(runner.runtime, "run_once", reject)

    assert runner.main(_cli_arguments("run", tmp_path)) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": "unsafe preflight",
        "verdict": "REJECT",
    }
    assert calls == 1
