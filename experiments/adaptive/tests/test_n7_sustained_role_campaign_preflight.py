from __future__ import annotations

import importlib.util
import hashlib
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


def _build_receipt(tmp_path: Path, *, revision: str = "a" * 40,
                   host: str = "proteina02") -> tuple[Path, dict[str, str]]:
    binaries: dict[str, dict[str, object]] = {}
    hashes: dict[str, str] = {}
    for name in ("hotstuff_app", "adaptation_manager", "hotstuff_keygen",
                 "hotstuff_tls_keygen", "epoch0_treefile_digest"):
        path = tmp_path / name
        payload = name.encode("ascii")
        path.write_bytes(payload)
        path.chmod(0o700)
        digest = hashlib.sha256(payload).hexdigest()
        hashes[name] = digest
        binaries[name] = {"path": str(path.resolve()), "sha256": digest,
                          "size_bytes": len(payload)}
    log = tmp_path / "build.log"; log.write_bytes(b"clean build\n")
    receipt = {
        "schema_version": 1, "kind": "kauri-w19-cluster-build-provenance-v1",
        "repository_revision": revision, "origin_revision": revision,
        "repository_branch": "feature/adaptive-epoch-throughput",
        "repository_clean_after_build": True, "host": host,
        "linux_boot_id": "12345678-1234-1234-1234-123456789abc",
        "build_exit_code": 0, "build_command": list(subject.operator._BUILD_COMMAND),
        "build_type": "RelWithDebInfo", "cmake_version": "3.30.0",
        "cxx_compiler": "clang++", "cxx_compiler_version": "18.0.0",
        "recorded_utc": "2026-09-30T12:00:00Z", "submodule_status": ["", ""],
        "build_log_path": str(log.resolve()),
        "build_log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "binaries": binaries,
    }
    path = tmp_path / "postmortem-build-provenance.json"
    path.write_bytes(_canonical(receipt))
    return path, hashes


def test_freeze_creates_canonical_12_cell_manifest_once(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    receipt, _hashes = _build_receipt(tmp_path)
    result = subject.create_campaign(
        campaign_root=root, campaign_id="w19-n7-v1", approval_reference="author-approval",
        repository_revision="a" * 40, target_host="proteina02", build_provenance_path=receipt,
        frozen_utc="2026-09-30T17:00:00Z")
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
                                repository_revision="a" * 40, target_host="proteina02",
                                build_provenance_path=receipt)


def test_v6_freeze_pins_canonical_clean_build_receipt_and_all_binary_hashes(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    receipt, hashes = _build_receipt(tmp_path)
    subject.create_campaign(
        campaign_root=root, campaign_id="w19-n7-v6", approval_reference="author-approval",
        repository_revision="a" * 40, target_host="proteina02",
        frozen_utc="2026-09-30T22:00:00Z", build_provenance_path=receipt)
    freeze = json.loads((root / subject.FREEZE_NAME).read_text())
    assert freeze["schema_version"] == 2
    assert freeze["build_provenance"] == {
        "raw_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        "repository_revision": "a" * 40,
        "repository_branch": "feature/adaptive-epoch-throughput",
        "host_identity": {"hostname": "proteina02", "linux_boot_id": "12345678-1234-1234-1234-123456789abc"},
        "binary_sha256": hashes,
    }
    assert (root / subject.BUILD_PROVENANCE_NAME).read_bytes() == receipt.read_bytes()


def test_v6_freeze_rejects_unclean_or_wrong_host_build_receipt_before_root_write(
    tmp_path: Path,
) -> None:
    receipt, _hashes = _build_receipt(tmp_path, host="other-host")
    with pytest.raises(subject.CampaignPreflightError, match="clean-build provenance"):
        subject.create_campaign(
            campaign_root=tmp_path / "campaign", campaign_id="w19-n7-v6",
            approval_reference="author", repository_revision="a" * 40,
            target_host="proteina02", frozen_utc="2026-09-30T22:00:00Z",
            build_provenance_path=receipt)
    assert not (tmp_path / "campaign").exists()


def test_v6_freeze_rejects_build_receipt_recorded_after_freeze_before_root_write(
    tmp_path: Path,
) -> None:
    receipt, _hashes = _build_receipt(tmp_path)
    document = json.loads(receipt.read_text())
    document["recorded_utc"] = "2026-09-30T22:00:01Z"
    receipt.write_bytes(_canonical(document))
    with pytest.raises(subject.CampaignPreflightError, match="later than campaign freeze"):
        subject.create_campaign(
            campaign_root=tmp_path / "campaign", campaign_id="w19-n7-v6",
            approval_reference="author", repository_revision="a" * 40,
            target_host="proteina02", build_provenance_path=receipt,
            frozen_utc="2026-09-30T22:00:00Z")
    assert not (tmp_path / "campaign").exists()


def test_freeze_requires_build_receipt_before_creating_root(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="build_provenance_path"):
        subject.create_campaign(  # type: ignore[call-arg]
            campaign_root=tmp_path / "campaign", campaign_id="w19-n7-v6",
            approval_reference="author", repository_revision="a" * 40,
            target_host="proteina02")
    assert not (tmp_path / "campaign").exists()


def test_freeze_rejects_unsafe_campaign_id_and_parent_path(tmp_path: Path) -> None:
    receipt, _hashes = _build_receipt(tmp_path)
    with pytest.raises(subject.CampaignPreflightError, match="identifier"):
        subject.create_campaign(campaign_root=tmp_path / "campaign", campaign_id="not safe",
                                approval_reference="author", repository_revision="a" * 40, target_host="proteina02",
                                build_provenance_path=receipt)
    with pytest.raises(subject.CampaignPreflightError, match="parent"):
        subject.create_campaign(campaign_root=tmp_path / "x" / ".." / "campaign", campaign_id="w19",
                                approval_reference="author", repository_revision="a" * 40, target_host="proteina02",
                                build_provenance_path=receipt)


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
    receipt, _hashes = _build_receipt(tmp_path)
    subject.create_campaign(campaign_root=root, campaign_id="w19-n7-v1", approval_reference="author",
                            repository_revision="a" * 40, target_host="proteina02", build_provenance_path=receipt,
                            frozen_utc="2026-09-30T17:00:00Z")
    captured: dict[str, object] = {}
    def materialize(manifest: object, freeze: object, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"launch_permitted": False, "state": "MATERIALIZED_NO_LAUNCH_EXTERNAL_EXACT_APPROVAL_REQUIRED"}
    monkeypatch.setattr(subject.operator, "materialize_next_cell", materialize)
    result = subject.stage_campaign_cell(
        campaign_root=root, ordinal=1, prior_records=None,
        raw_clock=lambda _clock: 1_000_000_000)
    assert result["launch_permitted"] is False
    assert captured["prior_validated_cells"] == []
    assert captured["window_start_monotonic_ns"] == 91_000_000_000
    assert captured["window_end_monotonic_ns"] == 161_000_000_000
    assert captured["prepare_kwargs"] == {"peer_port": 18000, "client_port": 19000, "manager_port": 20000,
                                            "app_binary": (root / "build-snapshot/hotstuff_app").resolve(),
                                            "manager_binary": (root / "build-snapshot/adaptation_manager").resolve(),
                                            "keygen_binary": (root / "build-snapshot/hotstuff_keygen").resolve(),
                                            "tls_keygen_binary": (root / "build-snapshot/hotstuff_tls_keygen").resolve(),
                                            "e0_helper_binary": (root / "build-snapshot/epoch0_treefile_digest").resolve()}


def test_stage_uses_freeze_snapshots_after_build_tree_mutates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path, hashes = _build_receipt(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    root = tmp_path / "campaign"
    subject.create_campaign(campaign_root=root, campaign_id="w19-n7-v6", approval_reference="author",
                            repository_revision="a" * 40, target_host="proteina02",
                            build_provenance_path=receipt_path, frozen_utc="2026-09-30T22:00:00Z")
    Path(receipt["binaries"]["hotstuff_app"]["path"]).write_bytes(b"rebuilt-after-freeze")
    captured: dict[str, object] = {}
    monkeypatch.setattr(subject.operator, "materialize_next_cell", lambda *_args, **kwargs: (
        captured.update(kwargs) or {"launch_permitted": False}))
    subject.stage_campaign_cell(campaign_root=root, ordinal=1, prior_records=None,
                                raw_clock=lambda _clock: 1_000_000_000)
    snapshot = Path(captured["prepare_kwargs"]["app_binary"])
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == hashes["hotstuff_app"]


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
    receipt, _hashes = _build_receipt(tmp_path)
    monkeypatch.setattr(subject, "_git_clean_pinned", lambda _revision: (_ for _ in ()).throw(
        subject.CampaignPreflightError("revision mismatch")))
    with pytest.raises(subject.CampaignPreflightError, match="revision mismatch"):
        subject.create_campaign(campaign_root=root, campaign_id="w19-n7-v1",
                                approval_reference="author", repository_revision="a" * 40,
                                target_host="proteina02", build_provenance_path=receipt)
    assert not root.exists()
    monkeypatch.setattr(subject, "_git_clean_pinned", lambda _revision: None)
    subject.create_campaign(campaign_root=root, campaign_id="w19-n7-v1",
                            approval_reference="author", repository_revision="a" * 40,
                            target_host="proteina02", build_provenance_path=receipt)
    monkeypatch.setattr(subject, "_git_clean_pinned", lambda _revision: (_ for _ in ()).throw(
        subject.CampaignPreflightError("revision mismatch")))
    with pytest.raises(subject.CampaignPreflightError, match="revision mismatch"):
        subject.stage_campaign_cell(campaign_root=root, ordinal=1, prior_records=None)
    assert not (root / "cells/cell-01").exists()
