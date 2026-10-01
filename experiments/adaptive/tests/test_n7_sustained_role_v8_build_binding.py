from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "n7-path-timeout-quorum" / "sustained_role_v8_build_binding.py"
SPEC = importlib.util.spec_from_file_location("n7_sustained_role_v8_build_binding", MODULE)
assert SPEC is not None and SPEC.loader is not None
subject = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = subject
SPEC.loader.exec_module(subject)

REVISION = "a" * 40
HOST = "proteina02"
BOOT = "12345678-1234-1234-1234-123456789abc"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"


def _descriptor(root: Path, path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    return {"path": str(path.relative_to(root)), "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


def _root_and_plan(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "cell"
    runtime = root / "runtime"
    archive = root / "materialization-binaries"
    runtime.mkdir(parents=True)
    archive.mkdir()
    artifacts: dict[str, object] = {}
    receipt_binaries: dict[str, dict[str, object]] = {}
    mapping = {
        "hotstuff_app": "hotstuff-app", "adaptation_manager": "adaptation-manager",
        "hotstuff_keygen": "hotstuff-keygen", "hotstuff_tls_keygen": "hotstuff-tls-keygen",
        "epoch0_treefile_digest": "e0-identity-helper",
    }
    for receipt_name, archive_name in mapping.items():
        source = tmp_path / f"source-{receipt_name}"
        payload = f"strict-fixture-{receipt_name}".encode("ascii")
        source.write_bytes(payload)
        archived = archive / archive_name
        archived.write_bytes(payload)
        receipt_binaries[receipt_name] = {"path": str(source.resolve()),
                                          "sha256": hashlib.sha256(payload).hexdigest(),
                                          "size_bytes": len(payload)}
        artifact_name = subject._BINARY_ARTIFACTS[receipt_name]
        artifacts[artifact_name] = _descriptor(root, archived)
    log = tmp_path / "clean-build.log"
    log.write_bytes(b"clean build fixture log\n")
    receipt = {
        "schema_version": 1, "kind": subject.operator._BUILD_RECEIPT_KIND,
        "repository_revision": REVISION, "origin_revision": REVISION,
        "repository_branch": "feature/adaptive-epoch-throughput",
        "repository_clean_after_build": True, "host": HOST, "linux_boot_id": BOOT,
        "build_exit_code": 0, "build_command": list(subject.operator._BUILD_COMMAND),
        "build_log_path": str(log.resolve()),
        "build_log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "build_type": "RelWithDebInfo", "cmake_version": "3.30.0",
        "cxx_compiler": "clang++", "cxx_compiler_version": "18.0.0",
        "recorded_utc": "2026-10-01T12:00:00Z", "submodule_status": ["", ""],
        "binaries": receipt_binaries,
    }
    receipt_path = runtime / "external-clean-build-receipt.json"
    receipt_path.write_bytes(_canonical(receipt))
    artifacts["build_receipt"] = _descriptor(root, receipt_path)
    return root, {"repository_revision": REVISION, "artifacts": artifacts}


def test_binds_all_five_archived_preparation_binaries(tmp_path: Path) -> None:
    root, plan = _root_and_plan(tmp_path)

    result = subject.bind(root=root, plan=plan, observed_revision=REVISION,
                          target_host=HOST, linux_boot_id=BOOT)

    assert result["state"] == "BUILD_COMPONENT_VERIFIED_NOT_LIVE_ATTESTED"
    assert result["live_attested"] is False
    assert result["launch_eligible"] is False
    assert set(result["binaries"]) == set(subject._BINARY_ARTIFACTS)


@pytest.mark.parametrize("change", ["receipt", "keygen", "revision", "host", "boot"])
def test_rejects_receipt_and_all_identity_or_binary_mismatches(tmp_path: Path, change: str) -> None:
    root, plan = _root_and_plan(tmp_path)
    arguments = {"observed_revision": REVISION, "target_host": HOST, "linux_boot_id": BOOT}
    if change == "receipt":
        (root / "runtime/external-clean-build-receipt.json").write_bytes(b"fixture\n")
    elif change == "keygen":
        (root / "materialization-binaries/hotstuff-keygen").write_bytes(b"changed")
    elif change == "revision":
        arguments["observed_revision"] = "b" * 40
    elif change == "host":
        arguments["target_host"] = "other-host"
    else:
        arguments["linux_boot_id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    with pytest.raises(subject.V8BuildBindingError):
        subject.bind(root=root, plan=plan, **arguments)


def test_rejects_symlinked_archived_generator(tmp_path: Path) -> None:
    root, plan = _root_and_plan(tmp_path)
    archived = root / "materialization-binaries/hotstuff-tls-keygen"
    outside = tmp_path / "outside-generator"
    outside.write_bytes(archived.read_bytes())
    archived.unlink()
    archived.symlink_to(outside)

    with pytest.raises(subject.V8BuildBindingError):
        subject.bind(root=root, plan=plan, observed_revision=REVISION,
                     target_host=HOST, linux_boot_id=BOOT)
