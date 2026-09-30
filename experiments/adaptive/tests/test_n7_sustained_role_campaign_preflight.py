from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "n7-path-timeout-quorum" / "sustained_role_campaign_preflight.py"
SPEC = importlib.util.spec_from_file_location("n7_sustained_role_campaign_preflight", MODULE)
assert SPEC is not None and SPEC.loader is not None
subject = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = subject
SPEC.loader.exec_module(subject)
REAL_GIT_CHECK = subject._git_clean_pinned


@pytest.fixture(autouse=True)
def _isolated_repository_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "_git_clean_pinned", lambda _revision: None)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"


def test_freeze_creates_canonical_12_cell_manifest_once(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    result = subject.create_campaign(
        campaign_root=root, campaign_id="w19-n7-v1", approval_reference="author-approval",
        repository_revision="a" * 40, target_host="proteina02", frozen_utc="2026-09-30T17:00:00Z")
    freeze = json.loads((root / subject.FREEZE_NAME).read_text())
    manifest_raw = (root / subject.MANIFEST_NAME).read_bytes()
    manifest = json.loads(manifest_raw)
    assert result["state"] == "FROZEN_NO_LAUNCH"
    assert result["launch_permitted"] is False
    assert manifest_raw == _canonical(manifest)
    assert len(manifest["cells"]) == 12
    assert [item["arm"] for item in manifest["cells"][:4]] == ["fixed_e0", "adaptive_e1", "adaptive_e1", "fixed_e0"]
    assert manifest["freeze_sha256"] == freeze["freeze_sha256"]
    with pytest.raises(subject.CampaignPreflightError, match="already exists"):
        subject.create_campaign(campaign_root=root, campaign_id="w19-n7-v1", approval_reference="author-approval",
                                repository_revision="a" * 40, target_host="proteina02")


def test_freeze_rejects_unsafe_campaign_id_and_parent_path(tmp_path: Path) -> None:
    with pytest.raises(subject.CampaignPreflightError, match="identifier"):
        subject.create_campaign(campaign_root=tmp_path / "campaign", campaign_id="not safe",
                                approval_reference="author", repository_revision="a" * 40, target_host="proteina02")
    with pytest.raises(subject.CampaignPreflightError, match="parent"):
        subject.create_campaign(campaign_root=tmp_path / "x" / ".." / "campaign", campaign_id="w19",
                                approval_reference="author", repository_revision="a" * 40, target_host="proteina02")


def test_port_bases_are_separated_and_bound_to_ordinal() -> None:
    first = subject._port_bases(1, None, None, None)
    second = subject._port_bases(2, None, None, None)
    assert first == {"peer_port": 18000, "client_port": 19000, "manager_port": 20000}
    assert second["peer_port"] > first["peer_port"]
    with pytest.raises(subject.CampaignPreflightError, match="overlap"):
        subject._port_bases(1, 18000, 18000, 20000)


def test_stage_passes_fresh_window_ports_binaries_and_no_prior_records(monkeypatch: pytest.MonkeyPatch,
                                                                          tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    subject.create_campaign(campaign_root=root, campaign_id="w19-n7-v1", approval_reference="author",
                            repository_revision="a" * 40, target_host="proteina02", frozen_utc="2026-09-30T17:00:00Z")
    binaries = {}
    for name in ("app", "manager", "keygen", "tls", "helper"):
        path = tmp_path / name
        path.write_text("binary")
        path.chmod(0o700)
        binaries[name] = path
    captured: dict[str, object] = {}
    def materialize(manifest: object, freeze: object, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"launch_permitted": False, "state": "MATERIALIZED_NO_LAUNCH_EXTERNAL_EXACT_APPROVAL_REQUIRED"}
    monkeypatch.setattr(subject.operator, "materialize_next_cell", materialize)
    result = subject.stage_campaign_cell(
        campaign_root=root, ordinal=1, prior_records=None,
        app_binary=binaries["app"], manager_binary=binaries["manager"], keygen_binary=binaries["keygen"],
        tls_keygen_binary=binaries["tls"], e0_helper_binary=binaries["helper"],
        raw_clock=lambda _clock: 1_000_000_000)
    assert result["launch_permitted"] is False
    assert captured["prior_validated_cells"] == []
    assert captured["window_start_monotonic_ns"] == 91_000_000_000
    assert captured["window_end_monotonic_ns"] == 161_000_000_000
    assert captured["prepare_kwargs"] == {"peer_port": 18000, "client_port": 19000, "manager_port": 20000,
                                            "app_binary": binaries["app"].resolve(),
                                            "manager_binary": binaries["manager"].resolve(),
                                            "keygen_binary": binaries["keygen"].resolve(),
                                            "tls_keygen_binary": binaries["tls"].resolve(),
                                            "e0_helper_binary": binaries["helper"].resolve()}


def test_later_stage_requires_canonical_complete_prior_records(tmp_path: Path) -> None:
    with pytest.raises(subject.CampaignPreflightError, match="require canonical prior"):
        subject._prior_records(None, 2)
    records = tmp_path / "records.json"
    records.write_bytes(b"[]\n")
    with pytest.raises(subject.CampaignPreflightError, match="do not match"):
        subject._prior_records(records, 2)


@pytest.mark.parametrize(("changed_command", "changed_output"), [
    (("branch", "--show-current"), "wrong-branch"),
    (("rev-parse", "HEAD"), "b" * 40),
    (("rev-parse", "refs/remotes/origin/feature/adaptive-epoch-throughput"), "b" * 40),
    (("status", "--porcelain=v1"), " M changed.py"),
])
def test_git_gate_rejects_wrong_or_unpushed_checkout(
    changed_command: tuple[str, ...], changed_output: str,
) -> None:
    answers = {
        ("branch", "--show-current"): "feature/adaptive-epoch-throughput",
        ("rev-parse", "HEAD"): "a" * 40,
        ("rev-parse", "refs/remotes/origin/feature/adaptive-epoch-throughput"): "a" * 40,
        ("status", "--porcelain=v1"): "",
    }
    answers[changed_command] = changed_output
    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=answers[tuple(command[3:])], stderr="")
    with pytest.raises(subject.CampaignPreflightError, match="differs from freeze"):
        REAL_GIT_CHECK("a" * 40, runner=runner)


def test_revision_mismatch_prevents_freeze_and_stage_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "campaign"
    monkeypatch.setattr(subject, "_git_clean_pinned", lambda _revision: (_ for _ in ()).throw(
        subject.CampaignPreflightError("revision mismatch")))
    with pytest.raises(subject.CampaignPreflightError, match="revision mismatch"):
        subject.create_campaign(campaign_root=root, campaign_id="w19-n7-v1",
                                approval_reference="author", repository_revision="a" * 40,
                                target_host="proteina02")
    assert not root.exists()
    monkeypatch.setattr(subject, "_git_clean_pinned", lambda _revision: None)
    subject.create_campaign(campaign_root=root, campaign_id="w19-n7-v1",
                            approval_reference="author", repository_revision="a" * 40,
                            target_host="proteina02")
    monkeypatch.setattr(subject, "_git_clean_pinned", lambda _revision: (_ for _ in ()).throw(
        subject.CampaignPreflightError("revision mismatch")))
    with pytest.raises(subject.CampaignPreflightError, match="revision mismatch"):
        subject.stage_campaign_cell(campaign_root=root, ordinal=1, prior_records=None)
    assert not (root / "cells/cell-01").exists()
