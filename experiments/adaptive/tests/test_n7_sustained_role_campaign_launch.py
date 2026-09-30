from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "n7-path-timeout-quorum" / "sustained_role_campaign_launch.py"
SPEC = importlib.util.spec_from_file_location("n7_sustained_role_campaign_launch", MODULE)
assert SPEC is not None and SPEC.loader is not None
subject = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = subject
SPEC.loader.exec_module(subject)


def test_scope_command_is_one_cell_and_cgroup_bounded(tmp_path: Path) -> None:
    command = subject.scope_command(unit="kauri-w19-cell-01-abc", arm="fixed_e0",
                                    run_root=tmp_path / "cell", authorization=tmp_path / "approval.json",
                                    authorization_sha256="a" * 64)
    assert command[:5] == ["systemd-run", "--user", "--scope", "--unit", "kauri-w19-cell-01-abc"]
    assert "--wait" not in command
    assert "RuntimeMaxSec=210s" in command
    assert "KillMode=control-group" in command
    assert command[-1] == "--execute"
    assert any("sustained_role_fixed_e0_launcher.py" in item for item in command)
    assert command[-3:-1] == ["--expected-authorization-sha256", "a" * 64]


def test_scope_command_selects_only_the_adaptive_one_shot_launcher(tmp_path: Path) -> None:
    command = subject.scope_command(unit="kauri-w19-cell-02-abc", arm="adaptive_e1",
                                    run_root=tmp_path / "cell", authorization=tmp_path / "approval.json",
                                    authorization_sha256="b" * 64)
    assert any("sustained_role_adaptive_e1_launcher.py" in item for item in command)
    assert "sustained_role_fixed_e0_launcher.py" not in command
    assert command[-3:-1] == ["--expected-authorization-sha256", "b" * 64]


def test_scope_abort_is_immutable_and_no_retry(tmp_path: Path) -> None:
    cell = {"ordinal": 1, "arm": "fixed_e0", "run_id": "w19-cell-01"}
    subject._seal_abort(tmp_path, cell=cell, detail="scope terminated")
    path = tmp_path / subject.ABORT_PATH
    raw = path.read_bytes(); payload = json.loads(raw)
    assert raw == subject._canonical(payload)
    assert payload["state"] == "ABORTED_NO_RETRY_SCOPE_CLEAN"
    assert payload["no_retry"] is True
    subject._seal_abort(tmp_path, cell=cell, detail="replacement")
    assert path.read_bytes() == raw


def test_scope_requires_inactive_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 0
        stdout = "ActiveState=active\nControlGroup=/user.slice/example.scope\n"
        stderr = ""
    with pytest.raises(subject.CampaignLaunchError, match="did not become inactive"):
        subject._scope_empty("example.scope", lambda *_args, **_kwargs: Result())


def test_scope_accepts_systemd_timeout_terminal_after_control_group_kill() -> None:
    class Result:
        returncode = 0
        stdout = "ActiveState=failed\nSubState=failed\nResult=timeout\nControlGroup=/user.slice/example.scope\n"
        stderr = ""
    proof = subject._scope_empty("example.scope", lambda *_args, **_kwargs: Result(),
                                 cgroup_empty=lambda _group: True)
    assert "Result=timeout" in proof["terminal_properties"]
    assert proof["cgroup_population"] == "zero"


def test_scope_does_not_accept_terminal_status_while_cgroup_lists_a_member() -> None:
    class Systemctl:
        returncode = 0
        stdout = "ActiveState=failed\nSubState=failed\nResult=timeout\nControlGroup=/user.slice/example.scope\n"
        stderr = ""
    calls = 0
    def runner(command: list[str], **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return Systemctl()
    ticks = iter((0.0, 9.0))
    with pytest.raises(subject.CampaignLaunchError, match="did not become inactive"):
        subject._scope_empty("example.scope", runner, monotonic=lambda: next(ticks), sleep=lambda _value: None,
                             cgroup_empty=lambda _group: False)
    assert calls >= 1


def test_scope_membership_query_error_fails_closed() -> None:
    class Systemctl:
        returncode = 0
        stdout = "ActiveState=failed\nResult=timeout\nControlGroup=/user.slice/example.scope\n"
        stderr = ""
    with pytest.raises(subject.CampaignLaunchError, match="unavailable"):
        subject._scope_empty("example.scope", lambda *_args, **_kwargs: Systemctl(),
                             cgroup_empty=lambda _group: (_ for _ in ()).throw(
                                 subject.CampaignLaunchError("scope cgroup events file is unavailable")))


def test_scope_query_error_after_active_sample_never_means_gc() -> None:
    active = SimpleNamespace(returncode=0,
                             stdout="ActiveState=active\nControlGroup=/user.slice/example.scope\n", stderr="")
    error = SimpleNamespace(returncode=1, stdout="", stderr="D-Bus error")
    samples = iter((active, error))
    with pytest.raises(subject.CampaignLaunchError, match="cannot verify transient scope"):
        subject._scope_empty("example", lambda *_args, **_kwargs: next(samples),
                             monotonic=lambda: 0.0, sleep=lambda _value: None)


def test_scope_accepts_gc_after_terminal_scope() -> None:
    class Systemctl:
        returncode = 0
        stdout = "ActiveState=inactive\nResult=success\nControlGroup=\n"
        stderr = ""
    proof = subject._scope_empty("example.scope", lambda *_args, **_kwargs: Systemctl())
    assert "ActiveState=inactive" in proof["terminal_properties"]
    assert proof["cgroup_population"] == "collected"


def test_scope_status_queries_scope_not_default_service() -> None:
    class Systemctl:
        returncode = 0
        stdout = "ActiveState=inactive\nResult=success\nControlGroup=\n"
        stderr = ""
    calls: list[list[str]] = []
    def runner(command: list[str], **_kwargs: object) -> object:
        calls.append(command)
        return Systemctl()
    subject._scope_empty("kauri-w19-cell01", runner)
    assert calls[0][3] == "kauri-w19-cell01.scope"


def _prepared_cell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                   *, staged_boot: str = "boot-a", approved_utc: str = "2026-09-30T16:01:00Z") -> tuple[Path, Path]:
    cell = {"ordinal": 1, "pair_index": 1, "arm": "fixed_e0", "run_id": "cell01",
            "run_root": "cells/cell01", "target_host": "proteina02"}
    run_root = tmp_path / "cells/cell01"
    (run_root / "runtime").mkdir(parents=True)
    stage = {"host_identity": {"hostname": "proteina02", "linux_boot_id": staged_boot}}
    (run_root / subject.STAGE_PATH).write_bytes(subject._canonical(stage))
    plan = {"plan_sha256": "a" * 64, "repository_revision": "b" * 40,
            "scheduled_window": {"start_monotonic_ns": 150_000_000_000,
                                 "end_monotonic_ns": 220_000_000_000}}
    request = {"execution_plan_sha256": "a" * 64, "arm": "fixed_e0", "no_retry": True}
    (run_root / "runtime/sustained-role-execution-plan.json").write_bytes(subject._canonical(plan))
    request_raw = subject._canonical(request)
    (run_root / "runtime/sustained-role-authorization-request.json").write_bytes(request_raw)
    request_sha = hashlib.sha256(request_raw).hexdigest()
    approval_path = tmp_path / "approval.json"
    approval = {"schema_version": 1,
                "kind": "kauri-n7-sustained-role-fixed-e0-launch-authorization-v1",
                "request_sha256": request_sha, "plan_sha256": "a" * 64,
                "approval_reference": "approved-campaign", "approved_utc": approved_utc,
                "no_retry": True}
    approval_path.write_bytes(subject._canonical(approval))
    monkeypatch.setattr(subject.operator, "validate_manifest", lambda *_args: (
        {"schema_version": 2, "repository_revision": "b" * 40,
         "campaign_approval_reference": "approved-campaign",
         "frozen_utc": "2026-09-30T16:00:00Z"}, [cell]))
    monkeypatch.setattr(subject, "_git_clean_pinned", lambda *_args: None)
    monkeypatch.setattr(subject.evaluator, "_check_campaign_manifest", lambda *_args: [cell])
    monkeypatch.setattr(subject.evaluator, "_stage_receipt", lambda *_args: request_sha)
    monkeypatch.setattr(subject.evaluator, "_verify_campaign_build_archive", lambda *_args: None)
    monkeypatch.setattr(subject.evaluator, "_check_freeze", lambda *_args: datetime(2026, 9, 30, 16, tzinfo=timezone.utc))
    monkeypatch.setattr(subject.operator, "_host_boot_identity", lambda: {
        "hostname": "proteina02", "linux_boot_id": "boot-a"})
    monkeypatch.setattr(subject, "_raw_clock", lambda: 60_000_000_000)
    return run_root, approval_path


def test_prelaunch_rejects_changed_linux_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run_root, approval = _prepared_cell(tmp_path, monkeypatch, staged_boot="boot-old")
    with pytest.raises(subject.CampaignLaunchError, match="Linux boot"):
        subject.verify_one_cell({}, {}, campaign_root=tmp_path, ordinal=1, authorization=approval)


def test_prelaunch_rejects_legacy_freeze_before_spawning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run_root, approval = _prepared_cell(tmp_path, monkeypatch)
    monkeypatch.setattr(subject.operator, "validate_manifest", lambda *_args: ({"schema_version": 1}, []))
    with pytest.raises(subject.CampaignLaunchError, match="legacy campaign freeze"):
        subject.verify_one_cell({}, {}, campaign_root=tmp_path, ordinal=1, authorization=approval)


@pytest.mark.parametrize("archive_state", ["missing", "tampered"])
def test_prelaunch_rejects_archived_build_drift_before_scope_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_state: str,
) -> None:
    _run_root, approval = _prepared_cell(tmp_path, monkeypatch)
    monkeypatch.setattr(subject.evaluator, "_verify_campaign_build_archive", lambda *_args: (_ for _ in ()).throw(
        ValueError(f"archived build {archive_state}")))
    with pytest.raises(subject.CampaignLaunchError, match="archived build inputs"):
        subject.verify_one_cell({}, {}, campaign_root=tmp_path, ordinal=1, authorization=approval)


def test_prelaunch_pins_resolved_external_approval_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_root, approval = _prepared_cell(tmp_path, monkeypatch)
    prepared = subject.verify_one_cell({}, {}, campaign_root=tmp_path, ordinal=1,
                                       authorization=approval)
    assert prepared["authorization_path"] == approval.resolve()
    assert prepared["authorization_sha256"] == hashlib.sha256(approval.read_bytes()).hexdigest()


def test_prelaunch_accepts_a_complete_v2_build_bound_stage_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, approval = _prepared_cell(tmp_path, monkeypatch)
    cell = {"ordinal": 1, "pair_index": 1, "arm": "fixed_e0", "run_id": "cell01",
            "run_root": "cells/cell01", "target_host": "proteina02", "hard_timeout_seconds": 210}
    provenance = {
        "raw_sha256": "c" * 64, "repository_revision": "b" * 40,
        "repository_branch": "feature/adaptive-epoch-throughput",
        "host_identity": {"hostname": "proteina02", "linux_boot_id": "boot-a"},
        "binary_sha256": {
            "hotstuff_app": "d" * 64, "adaptation_manager": "e" * 64,
            "hotstuff_keygen": "f" * 64, "hotstuff_tls_keygen": "0" * 64,
            "epoch0_treefile_digest": "1" * 64,
        },
    }
    # Use a valid boot UUID for the evaluator's strict v2 schema.
    provenance["host_identity"]["linux_boot_id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    monkeypatch.setattr(subject.operator, "_host_boot_identity", lambda: dict(provenance["host_identity"]))
    freeze = subject.evaluator.build_campaign_freeze(
        campaign_id="w19-v6", frozen_utc="2026-09-30T16:00:00Z",
        campaign_approval_reference="approved-campaign", repository_revision="b" * 40,
        build_provenance=provenance)
    manifest = {"manifest_sha256": "d" * 64}
    request_raw = (run_root / "runtime/sustained-role-authorization-request.json").read_bytes()
    request_sha = hashlib.sha256(request_raw).hexdigest()
    stage = {
        "schema_version": 2, "kind": subject.evaluator.STAGE_RECEIPT_KIND,
        "state": "MATERIALIZED_NO_LAUNCH_EXTERNAL_EXACT_APPROVAL_REQUIRED",
        "manifest_sha256": manifest["manifest_sha256"], "freeze_sha256": freeze["freeze_sha256"],
        "ordinal": 1, "pair_index": 1, "arm": "fixed_e0", "run_id": "cell01",
        "run_root": "cells/cell01", "target_host": "proteina02",
        "host_identity": provenance["host_identity"], "clock": "CLOCK_MONOTONIC_RAW",
        "scheduled_window": {"start_monotonic_ns": 150_000_000_000,
                             "end_monotonic_ns": 220_000_000_000},
        "hard_timeout_seconds": 210, "no_retry": True,
        "authorization_request_sha256": request_sha,
        "build_provenance_raw_sha256": provenance["raw_sha256"],
        "build_provenance_binary_sha256": provenance["binary_sha256"],
        "claim_eligible": False, "figure_eligible": False,
    }
    stage["stage_receipt_sha256"] = hashlib.sha256(subject._canonical(stage)).hexdigest()
    stage_raw = subject._canonical(stage)
    (run_root / subject.STAGE_PATH).write_bytes(stage_raw)
    monkeypatch.setattr(subject.operator, "validate_manifest", lambda *_args: (freeze, [cell]))
    monkeypatch.setattr(subject.evaluator, "_check_campaign_manifest", lambda *_args: [cell])
    monkeypatch.setattr(subject.evaluator, "_check_freeze", lambda *_args: datetime(2026, 9, 30, 16, tzinfo=timezone.utc))
    prepared = subject.verify_one_cell(freeze, manifest, campaign_root=tmp_path, ordinal=1,
                                       authorization=approval)
    assert prepared["stage_receipt_sha256"] == hashlib.sha256(stage_raw).hexdigest()


def test_prelaunch_rejects_approval_before_freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run_root, approval = _prepared_cell(tmp_path, monkeypatch, approved_utc="2026-09-30T15:59:59Z")
    with pytest.raises(subject.CampaignLaunchError, match="postdate"):
        subject.verify_one_cell({}, {}, campaign_root=tmp_path, ordinal=1, authorization=approval)


@pytest.mark.parametrize("sample", [121_000_000_001, 1_000_000_000])
def test_prelaunch_rejects_raw_clock_outside_safe_scope_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample: int,
) -> None:
    _run_root, approval = _prepared_cell(tmp_path, monkeypatch)
    monkeypatch.setattr(subject, "_raw_clock", lambda: sample)
    with pytest.raises(subject.CampaignLaunchError, match="scope cleanup reserve"):
        subject.verify_one_cell({}, {}, campaign_root=tmp_path, ordinal=1, authorization=approval)


def test_prelaunch_rejects_manifest_cell_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_root, approval = _prepared_cell(tmp_path, monkeypatch)
    alternate = tmp_path / "alternate"
    run_root.rename(alternate)
    run_root.symlink_to(alternate, target_is_directory=True)
    with pytest.raises(subject.operator.CampaignOperatorError, match="symlink ancestor"):
        subject.verify_one_cell({}, {}, campaign_root=tmp_path, ordinal=1, authorization=approval)


def test_prelaunch_rejects_parent_traversal_disguised_as_external_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, approval = _prepared_cell(tmp_path, monkeypatch)
    internal = run_root / "runtime/inside-approval.json"
    internal.write_bytes(approval.read_bytes())
    (tmp_path / "outside").mkdir()
    disguised = tmp_path / "outside/../cells/cell01/runtime/inside-approval.json"
    with pytest.raises(subject.CampaignLaunchError, match="parent directories"):
        subject.verify_one_cell({}, {}, campaign_root=tmp_path, ordinal=1, authorization=disguised)


def test_prelaunch_rejects_dangling_abort_symlink_as_used_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, approval = _prepared_cell(tmp_path, monkeypatch)
    (run_root / subject.ABORT_PATH).symlink_to(tmp_path / "absent-abort.json")
    with pytest.raises(subject.CampaignLaunchError, match="reuse is forbidden"):
        subject.verify_one_cell({}, {}, campaign_root=tmp_path, ordinal=1, authorization=approval)


def test_scope_status_uncertainty_seals_honest_no_retry_abort(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    run_root = tmp_path / "cells/cell01"
    run_root.mkdir(parents=True)
    cell = {"ordinal": 1, "pair_index": 1, "arm": "fixed_e0", "run_id": "cell01"}
    monkeypatch.setattr(subject, "verify_one_cell", lambda *_args, **_kwargs: {
        "cell": cell, "run_root": run_root, "stage_receipt_path": str(subject.STAGE_PATH),
        "stage_receipt_sha256": "a" * 64,
        "authorization_path": tmp_path / "verified-approval.json", "authorization_sha256": "b" * 64})
    monkeypatch.setattr(subject.uuid, "uuid4", lambda: SimpleNamespace(hex="deadbeefcafe"))
    monkeypatch.setattr(subject, "_scope_empty", lambda *_args: (_ for _ in ()).throw(
        subject.CampaignLaunchError("scope status unavailable")))
    def runner(_command: list[str], **_kwargs: object) -> object:
        assert _command[_command.index("--authorization") + 1] == str(tmp_path / "verified-approval.json")
        assert _command[_command.index("--expected-authorization-sha256") + 1] == "b" * 64
        return SimpleNamespace(returncode=0, stdout="Running scope as unit: kauri-w19-cell01-deadbeefcafe.scope\n", stderr="")
    with pytest.raises(subject.CampaignLaunchError, match="cleanup could not be verified"):
        subject.execute_one_cell({}, {}, campaign_root=tmp_path, ordinal=1,
                                 authorization=tmp_path / "approval.json", runner=runner)
    abort = json.loads((run_root / subject.ABORT_PATH).read_bytes())
    assert abort["state"] == "ABORTED_NO_RETRY_SCOPE_STATUS_UNVERIFIED"
    assert abort["no_retry"] is True


def test_failed_scope_seals_bounded_stderr_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "cells/cell01"
    run_root.mkdir(parents=True)
    cell = {"ordinal": 1, "pair_index": 1, "arm": "fixed_e0", "run_id": "cell01"}
    monkeypatch.setattr(subject, "verify_one_cell", lambda *_args, **_kwargs: {
        "cell": cell, "run_root": run_root, "stage_receipt_path": str(subject.STAGE_PATH),
        "stage_receipt_sha256": "a" * 64,
        "authorization_path": tmp_path / "verified-approval.json", "authorization_sha256": "b" * 64})
    monkeypatch.setattr(subject.uuid, "uuid4", lambda: SimpleNamespace(hex="deadbeefcafe"))
    stderr = "x" * 600 + "\nFAIL\t"
    def runner(_command: list[str], **_kwargs: object) -> object:
        return SimpleNamespace(returncode=1, stdout="", stderr=stderr)
    with pytest.raises(subject.CampaignLaunchError, match="did not confirm"):
        subject.execute_one_cell({}, {}, campaign_root=tmp_path, ordinal=1,
                                 authorization=tmp_path / "caller-path.json", runner=runner)
    abort = json.loads((run_root / subject.ABORT_PATH).read_bytes())
    diagnostic = abort["scope_diagnostics"]
    assert diagnostic["scope_unit"] == "kauri-w19-cell01-deadbeefcafe.scope"
    assert diagnostic["exit_code"] == 1
    assert diagnostic["stderr_sha256"] == hashlib.sha256(stderr.encode()).hexdigest()
    assert len(diagnostic["stderr_tail_escaped"]) <= 270
    assert "\\nFAIL\\t" in diagnostic["stderr_tail_escaped"]


def test_immediate_raw_replay_failure_seals_rejection_abort(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    run_root = tmp_path / "cells/cell01"
    run_root.mkdir(parents=True)
    cell = {"ordinal": 1, "pair_index": 1, "arm": "fixed_e0", "run_id": "cell01"}
    monkeypatch.setattr(subject, "verify_one_cell", lambda *_args, **_kwargs: {
        "cell": cell, "run_root": run_root, "stage_receipt_path": str(subject.STAGE_PATH),
        "stage_receipt_sha256": "a" * 64,
        "authorization_path": tmp_path / "verified-approval.json", "authorization_sha256": "b" * 64})
    monkeypatch.setattr(subject.uuid, "uuid4", lambda: SimpleNamespace(hex="deadbeefcafe"))
    monkeypatch.setattr(subject, "_scope_empty", lambda *_args: {
        "terminal_properties": "ActiveState=inactive\nControlGroup=\n",
        "cgroup_population": "collected",
    })
    monkeypatch.setattr(subject.evaluator, "_check_freeze", lambda *_args: datetime(2026, 9, 30, 16, tzinfo=timezone.utc))
    monkeypatch.setattr(subject.evaluator, "_one_cell", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ValueError("raw replay rejected")))
    (run_root / subject.RECEIPTS["fixed_e0"]).write_bytes(b"sealed raw receipt fixture")
    def runner(_command: list[str], **_kwargs: object) -> object:
        return SimpleNamespace(returncode=0, stdout="Running scope as unit: kauri-w19-cell01-deadbeefcafe.scope\n", stderr="")
    with pytest.raises(subject.CampaignLaunchError, match="replay rejected"):
        subject.execute_one_cell({}, {}, campaign_root=tmp_path, ordinal=1,
                                 authorization=tmp_path / "approval.json", runner=runner)
    abort = json.loads((run_root / subject.ABORT_PATH).read_bytes())
    assert abort["state"] == "ABORTED_NO_RETRY_VALIDATION_REJECTED_SCOPE_CLEAN"


def test_successful_wrapper_seals_receipt_before_returning_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "cells/cell01"
    run_root.mkdir(parents=True)
    cell = {"ordinal": 1, "pair_index": 1, "arm": "fixed_e0", "run_id": "cell01"}
    authorization_sha256 = "b" * 64
    monkeypatch.setattr(subject, "verify_one_cell", lambda *_args, **_kwargs: {
        "cell": cell, "run_root": run_root, "stage_receipt_path": str(subject.STAGE_PATH),
        "stage_receipt_sha256": "a" * 64,
        "authorization_path": tmp_path / "verified-approval.json",
        "authorization_sha256": authorization_sha256,
    })
    monkeypatch.setattr(subject.uuid, "uuid4", lambda: SimpleNamespace(hex="deadbeefcafe"))
    monkeypatch.setattr(subject, "_scope_empty", lambda *_args: {
        "terminal_properties": "ActiveState=inactive\nControlGroup=\n",
        "cgroup_population": "collected",
    })
    monkeypatch.setattr(subject.evaluator, "_check_freeze", lambda *_args: datetime(2026, 9, 30, 16, tzinfo=timezone.utc))
    monkeypatch.setattr(subject.evaluator, "_one_cell", lambda *_args, **_kwargs: {"accepted": True})
    raw = b"sealed raw receipt fixture"
    (run_root / subject.RECEIPTS["fixed_e0"]).write_bytes(raw)

    def runner(_command: list[str], **_kwargs: object) -> object:
        return SimpleNamespace(returncode=0,
                               stdout="Running scope as unit: kauri-w19-cell01-deadbeefcafe.scope\n",
                               stderr="")

    record = subject.execute_one_cell({"freeze_sha256": "c" * 64}, {"manifest_sha256": "d" * 64},
                                      campaign_root=tmp_path, ordinal=1,
                                      authorization=tmp_path / "approval.json", runner=runner)
    success_path = run_root / subject.SUCCESS_PATH
    success_raw = success_path.read_bytes()
    success = json.loads(success_raw)
    assert success_raw == subject._canonical(success)
    assert success["state"] == "SUCCEEDED_SCOPE_CLEAN_REPLAY_ACCEPTED"
    assert success["authorization_sha256"] == authorization_sha256
    assert success["raw_receipt_sha256"] == hashlib.sha256(raw).hexdigest()
    assert record["success_receipt_path"] == str(subject.SUCCESS_PATH)
    assert record["success_receipt_sha256"] == hashlib.sha256(success_raw).hexdigest()
