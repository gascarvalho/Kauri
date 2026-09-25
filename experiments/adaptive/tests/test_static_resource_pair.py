from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive.kauri_experiment import static_resource_pair as study
from experiments.adaptive.kauri_experiment.profiled_fault_archive import verify_evidence_seal


ADAPTIVE = Path(__file__).resolve().parents[1]
PROFILE = ADAPTIVE / "profiles/n31-static-resource-cpu-sham-v1.json"
CONTRACT = ADAPTIVE / "profiles/n31-static-resource-cpu-sham-quota-v1.json"


def test_frozen_profile_and_contract_cover_all_live_members() -> None:
    profile = study.load_profile(PROFILE)
    contract = study.load_cpu_contract(CONTRACT, profile_path=PROFILE)

    assert profile.slow_ids == tuple(range(6))
    assert profile.fast_ids == tuple(range(6, 31))
    assert profile.baseline_root_ids == tuple(range(6))
    assert tuple(contract.quota_percent(replica) for replica in range(31)) == (25,) * 6 + (100,) * 25


def test_preflight_is_read_only_non_authorizing_and_binds_two_arm_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        study.cpu_quota,
        "verify_linux_environment",
        lambda: pytest.fail("read-only preflight must not probe systemd"),
    )
    result = study.prepare_preflight(
        profile_path=PROFILE,
        contract_path=CONTRACT,
        output_root=tmp_path / "result",
    )

    request = Path(str(result["authorization_request_path"])).read_bytes()
    document = json.loads(request)
    assert document["arms"] == ["sham", "adaptive"]
    assert document["claim_eligible"] is False
    assert document["figure_eligible"] is False
    assert result["execution_authorized"] is False
    assert result["launch_permitted"] is False
    assert result["live_probe_authorization_required"] is True
    assert document["output_root"] == str((tmp_path / "result").resolve())
    with pytest.raises(study.StaticResourcePairError, match="exclusive preflight"):
        study.prepare_preflight(
            profile_path=PROFILE,
            contract_path=CONTRACT,
            output_root=tmp_path / "result",
        )


def test_receipt_must_bind_exact_request(tmp_path: Path) -> None:
    profile = study.load_profile(PROFILE)
    contract = study.load_cpu_contract(CONTRACT, profile_path=PROFILE)
    request = study.build_authorization_request(
        profile=profile, contract=contract, output_root=tmp_path / "result"
    )
    document = json.loads(request)
    receipt = {
        **document,
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "approval_reference": "user-confirmation:2026-09-25:static-resource-cpu-study",
        "approved_utc": "2026-09-25T12:00:00Z",
    }
    assert study.verify_authorization_receipt(request, receipt)["profile_id"] == profile.profile_id
    receipt["arms"] = ["adaptive", "sham"]
    with pytest.raises(study.StaticResourcePairError, match="not bound"):
        study.verify_authorization_receipt(request, receipt)


def test_live_probe_request_requires_a_bound_preflight_receipt(tmp_path: Path) -> None:
    profile = study.load_profile(PROFILE)
    contract = study.load_cpu_contract(CONTRACT, profile_path=PROFILE)
    request = study.build_authorization_request(profile=profile, contract=contract, output_root=tmp_path / "result")
    receipt = {
        **json.loads(request),
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "approval_reference": "user-confirmation:2026-09-25:static-resource-cpu-study",
        "approved_utc": "2026-09-25T12:00:00Z",
    }
    probe = json.loads(study.build_live_probe_request(request, receipt))
    assert probe["launch_permitted"] is False
    assert probe["scope"] == "one-cgroup-v2-systemd-user-scope-probe-no-workload"


def test_execution_is_hard_fail_closed() -> None:
    with pytest.raises(study.StaticResourcePairError, match="launch is prohibited"):
        study.execution_not_implemented()


def test_completed_backend_outputs_are_sealed_without_a_runner_verdict(tmp_path: Path) -> None:
    root = tmp_path / "pair"
    for arm in ("sham", "adaptive"):
        arm_root = root / arm
        arm_root.mkdir(parents=True)
        manifest = {
                    "schema_version": 1,
                    "study_id": "static-resource-n31-pair-v1",
                    "pair_id": "s4-01",
                    "arm": arm,
                    "attempt": 1,
                    "retry_count": 0,
                    "issuer_key_path": f"{arm}/raw/issuer-public-key.txt",
                    "epoch0_path": f"{arm}/epoch0.bundle",
                    "epoch1_path": f"{arm}/epoch1.bundle",
                    "manager_path": f"{arm}/raw/manager.jsonl",
                    "manager_argv_path": f"{arm}/runtime/manager-argv.json",
                    "activation_path": f"{arm}/activation.json",
                    "resource_receipt_path": f"{arm}/resource-receipt.json",
                    "commit_path": f"{arm}/commit-window.json",
                    "cleanup_path": f"{arm}/cleanup.json",
        }
        (arm_root / "arm-manifest.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
        for field in (
            "issuer_key_path", "epoch0_path", "epoch1_path", "manager_path",
            "manager_argv_path", "activation_path", "resource_receipt_path",
            "commit_path", "cleanup_path",
        ):
            source = root / str(manifest[field])
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("{}\n", encoding="ascii")
    (root / "backend-raw-evidence.json").write_text("{}\n", encoding="ascii")
    result = study.seal_completed_pair(
        output_root=root,
        profile_path=PROFILE,
        contract_path=CONTRACT,
        pair_id="s4-01",
        provenance={
            "revision": "a" * 40,
            "build_sha256": "b" * 64,
            "workload_sha256": "c" * 64,
            "successor_schedule_sha256": "d" * 64,
            "host_identity_sha256": "e" * 64,
        },
        shared_epoch0_projection_sha256="f" * 64,
        shared_epoch0_digest="e" * 64,
        issuer_public_key_sha256="d" * 64,
        backend_raw_evidence_path="backend-raw-evidence.json",
    )

    manifest = json.loads((root / "pair-manifest.json").read_bytes())
    assert result["claim_eligible"] is False
    assert manifest["execution"]["schedule"] == ["sham", "adaptive"]
    assert manifest["arms"]["sham"]["manifest_path"] == "sham/arm-manifest.json"
    assert (root / "evidence-seal.json").is_file()
    sealed_paths = [entry.path for entry in verify_evidence_seal(root).entries]
    assert manifest["paths"] == sealed_paths
    assert "sham/evidence-seal.json" in sealed_paths
    assert "adaptive/evidence-seal.json" in sealed_paths


def _seal_arguments(root: Path, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "output_root": root,
        "profile_path": PROFILE,
        "contract_path": CONTRACT,
        "pair_id": "s4-01",
        "provenance": {
            "revision": "a" * 40,
            "build_sha256": "b" * 64,
            "workload_sha256": "c" * 64,
            "successor_schedule_sha256": "d" * 64,
            "host_identity_sha256": "e" * 64,
        },
        "shared_epoch0_projection_sha256": "f" * 64,
        "shared_epoch0_digest": "e" * 64,
        "issuer_public_key_sha256": "d" * 64,
        "backend_raw_evidence_path": "backend-raw-evidence.json",
    }
    return {**values, **overrides}


def test_sealer_rejects_a_symlinked_pair_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "backend-raw-evidence.json").write_text("{}\n", encoding="ascii")
    marker = target / "must-remain-unchanged.txt"
    marker.write_text("before\n", encoding="ascii")
    # Populate enough of a normally sealable pair that following ``link``
    # would reach the first archive write rather than failing on setup.
    for arm in ("sham", "adaptive"):
        arm_root = target / arm
        arm_root.mkdir()
        manifest = {
            "schema_version": 1, "study_id": "static-resource-n31-pair-v1",
            "pair_id": "s4-01", "arm": arm, "attempt": 1, "retry_count": 0,
            "issuer_key_path": f"{arm}/raw/issuer-public-key.txt",
            "epoch0_path": f"{arm}/epoch0.bundle", "epoch1_path": f"{arm}/epoch1.bundle",
            "manager_path": f"{arm}/raw/manager.jsonl",
            "manager_argv_path": f"{arm}/runtime/manager-argv.json",
            "activation_path": f"{arm}/activation.json",
            "resource_receipt_path": f"{arm}/resource-receipt.json",
            "commit_path": f"{arm}/commit-window.json", "cleanup_path": f"{arm}/cleanup.json",
        }
        (arm_root / "arm-manifest.json").write_text(json.dumps(manifest) + "\n", encoding="ascii")
        for field in ("issuer_key_path", "epoch0_path", "epoch1_path", "manager_path", "manager_argv_path", "activation_path", "resource_receipt_path", "commit_path", "cleanup_path"):
            source = target / str(manifest[field]); source.parent.mkdir(parents=True, exist_ok=True); source.write_text("{}\n", encoding="ascii")
    link = tmp_path / "pair-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(study.StaticResourcePairError, match="completed pair root must be a directory"):
        study.seal_completed_pair(**_seal_arguments(link))
    assert marker.read_text(encoding="ascii") == "before\n"
    assert not (target / "profile.json").exists()
    assert not (target / "pair-manifest.json").exists()


def test_sealer_rejects_broken_profile_archive_symlink(tmp_path: Path) -> None:
    root = tmp_path / "pair"
    root.mkdir()
    (root / "backend-raw-evidence.json").write_text("{}\n", encoding="ascii")
    (root / "profile.json").symlink_to(root / "missing-profile.json")
    with pytest.raises(study.StaticResourcePairError, match="exclusive profile archive"):
        study.seal_completed_pair(**_seal_arguments(root))


def test_sealer_rejects_backend_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "pair"
    root.mkdir()
    with pytest.raises(study.StaticResourcePairError, match="escapes completed pair root"):
        study.seal_completed_pair(
            **_seal_arguments(root, backend_raw_evidence_path="../outside.json")
        )
