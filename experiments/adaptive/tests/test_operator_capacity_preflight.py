from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from experiments.adaptive.kauri_experiment.operator_capacity_preflight import PreflightError, canonical_request

_FIXED_TEST_TOOL_DIGESTS = {
    "adaptation_manager": "9a3a45d01531a20e89ac6ae10b0b0beb0492acd7216a368aa062d1a5fecaf9cd",
    "capacity_digest": "31302ea6e369f7b21b5060984271dfe06fcbfe005e474f9b5921e9d6c3915551",
    "epoch0_digest": "31302ea6e369f7b21b5060984271dfe06fcbfe005e474f9b5921e9d6c3915551",
    "keygen": "9a3a45d01531a20e89ac6ae10b0b0beb0492acd7216a368aa062d1a5fecaf9cd",
    "stage_a_envelope_verifier": "088c07b4b29d61e609c3f8867d0c4b9a3c0c85e85f2b4f53d39a94093460b8c4",
    "tls_keygen": "9a3a45d01531a20e89ac6ae10b0b0beb0492acd7216a368aa062d1a5fecaf9cd",
}


def _helper(path: Path, digest: str) -> Path:
    path.write_text(f"#!/bin/sh\nprintf '{digest}\\n'\n", encoding="ascii")
    path.chmod(0o755)
    return path


def _clean_repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(("git", "-C", str(path), "init", "-q"), check=True)
    subprocess.run(("git", "-C", str(path), "config", "user.email", "test@example.invalid"), check=True)
    subprocess.run(("git", "-C", str(path), "config", "user.name", "Test"), check=True)
    (path / "tracked").write_bytes(b"fixture")
    subprocess.run(("git", "-C", str(path), "add", "tracked"), check=True)
    environment = dict(os.environ, GIT_AUTHOR_DATE="2000-01-01T00:00:00+0000",
                       GIT_COMMITTER_DATE="2000-01-01T00:00:00+0000")
    subprocess.run(("git", "-C", str(path), "commit", "-qm", "fixture"), check=True,
                   env=environment)
    return path


def _quota_payload() -> dict[str, object]:
    return {
        "assignments": [{"capacity_class": "slow" if replica < 6 else "fast", "cpu_quota_percent": 25 if replica < 6 else 100, "replica_id": replica} for replica in range(31)],
        "base_profile_id": "n31-static-resource-cpu-sham-v1",
        "base_profile_canonical_sha256": "2ed182ed95fe8514c80eb861ed2e86654afaaf6b881a2ede6fc1a03d0565b766",
        "base_profile_sha256": "285aa55cb33637009ccd491d74830cd7485bcd83cb993c33c488dbff6fe4bf09",
        "contract_id": "n31-static-resource-cpu-sham-quota-v1", "enabled": True, "figure_eligible": False,
        "launcher": "systemd-user-scope-cpu-quota-v1", "manager_visibility": "none", "sampling_interval_ms": 1000, "schema_version": 1,
    }


def _write_verifier(path: Path, receipt: dict[str, object], *, succeeds: bool = True) -> Path:
    payload = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    body = "exit 2\n" if not succeeds else (
        "out=''\nwhile [ \"$#\" -gt 0 ]; do\n"
        "if [ \"$1\" = '--output' ]; then out=$2; shift 2; else shift; fi\ndone\n"
        "[ -n \"$out\" ] || exit 2\nprintf '%s' '" + payload + "' > \"$out\"\n")
    path.write_text("#!/bin/sh\n" + body, encoding="ascii")
    path.chmod(0o755)
    return path


def _write_identity_approval(path: Path, binaries: dict[str, Path], *, mutate_fixture: bool = False) -> Path:
    """Freeze test fixture bytes before the adversarial mutation under test."""
    path.write_text(json.dumps({
        "schema_version": 1,
        "kind": "kauri-n31-operator-capacity-tool-identity-document-v1",
        "verdict": "UNVERIFIED_TOOL_IDENTITY_DOCUMENT",
        "approval_ref": "test-fixture-approved-before-mutation",
        "binary_sha256": ({name: hashlib.sha256(binary.read_bytes()).hexdigest()
                            for name, binary in sorted(binaries.items())}
                          if mutate_fixture else _FIXED_TEST_TOOL_DIGESTS),
    }, sort_keys=True, separators=(",", ":")), encoding="ascii")
    return path


def _valid_inputs(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    digest = "a" * 64
    snapshot, envelope, tree, quota = (tmp_path / "snapshot", tmp_path / "envelope", tmp_path / "tree", tmp_path / "quota")
    snapshot.write_bytes(b"capacity-snapshot"); envelope.write_bytes(b"signed-stage-a-envelope"); tree.write_bytes(b"fixture")
    quota.write_text(json.dumps(_quota_payload()), encoding="ascii")
    capacity, epoch0 = _helper(tmp_path / "capacity", digest), _helper(tmp_path / "epoch0", digest)
    repository = _clean_repository(tmp_path / "repo")
    revision = subprocess.run(("git", "-C", str(repository), "rev-parse", "HEAD"), text=True, capture_output=True, check=True).stdout.strip()
    receipt = {"schema_version": 1, "kind": "kauri-operator-capacity-native-envelope-verification-receipt-v1", "verdict": "NATIVE_ENVELOPE_VERIFIED_NO_EXECUTION", "envelope_wire_sha256": hashlib.sha256(envelope.read_bytes()).hexdigest(), "envelope_canonical_digest": "d" * 64, "approved_capacity_digest": digest, "issuer_id": 73, "issuer_reference": "n31-w18-fixture", "issuer_public_key_fingerprint": "c" * 64, "arm": "exact_copy_sham", "source_revision": revision, "verification_monotonic_raw_ns": 1, "epoch0_tree_file_sha256": hashlib.sha256(tree.read_bytes()).hexdigest(), "epoch0_consensus_digest": digest, "epoch0_topology_digest": "e" * 64}
    verifier = _write_verifier(tmp_path / "verifier", receipt)
    binaries = {"adaptation_manager": tmp_path / "app", "keygen": tmp_path / "keygen", "tls_keygen": tmp_path / "tls", "capacity_digest": capacity, "epoch0_digest": epoch0, "stage_a_envelope_verifier": verifier}
    for name, binary in binaries.items():
        if name not in {"capacity_digest", "epoch0_digest", "stage_a_envelope_verifier"}: binary.write_bytes(b"binary")
    approval = _write_identity_approval(tmp_path / "identity-approval", binaries)
    return {"repository": repository, "capacity_snapshot_wire": snapshot, "stage_a_envelope_wire": envelope, "capacity_digest_binary": capacity, "epoch0_digest_binary": epoch0, "epoch0_arm": "slow-roots", "epoch0_tree_file": tree, "arm": "sham", "quota_profile": quota, "output_root": tmp_path / "fresh", "issuer_id": 73, "issuer_reference": "n31-w18-fixture", "issuer_public_key_fingerprint": "c" * 64, "issuer_public_key_hex": "02" + "a" * 64, "approved_capacity_digest": digest, "epoch0_topology_digest": "e" * 64, "native_envelope_verifier_binary": verifier, "native_envelope_receipt_output": tmp_path / "native-receipt", "tool_identity_document": approval, "binaries": binaries, "_receipt": receipt}


def _rewrite_verifier(values: dict[str, object], **changes: object) -> None:
    receipt = dict(values["_receipt"]); receipt.update(changes); values["_receipt"] = receipt
    verifier = values["native_envelope_verifier_binary"]; assert isinstance(verifier, Path)
    _write_verifier(verifier, receipt)
    approval = values["tool_identity_document"]
    binaries = values["binaries"]
    assert isinstance(approval, Path) and isinstance(binaries, dict)
    _write_identity_approval(approval, binaries, mutate_fixture=True)


def _request(values: dict[str, object]) -> dict[str, object]:
    return canonical_request(**{key: value for key, value in values.items() if key != "_receipt"})  # type: ignore[arg-type]


def test_self_authorized_fake_tools_remain_unverified_and_do_not_execute(tmp_path: Path) -> None:
    values = _valid_inputs(tmp_path)
    request = _request(values)
    assert request["verdict"] == "UNVERIFIED_TOOL_IDENTITY_DOCUMENT"
    assert request["tool_identity_document_matches_observed_binaries"] is True
    assert request["claim_eligible"] is False
    receipt_path = values["native_envelope_receipt_output"]
    assert isinstance(receipt_path, Path) and not receipt_path.exists()


def test_preflight_stays_pending_without_external_tool_identity_approval(tmp_path: Path) -> None:
    values = _valid_inputs(tmp_path); values["tool_identity_document"] = None
    request = _request(values)
    assert request["verdict"] == "PENDING_TOOL_IDENTITY_APPROVAL"
    assert request["tool_identity_authorization_required"] is True


@pytest.mark.parametrize("field", ("capacity_snapshot_wire", "stage_a_envelope_wire", "epoch0_tree_file", "quota_profile"))
def test_preflight_rejects_symlink_inputs(tmp_path: Path, field: str) -> None:
    values = _valid_inputs(tmp_path)
    target = tmp_path / f"{field}-target"; target.write_bytes(b"fixture")
    link = tmp_path / f"{field}-link"; link.symlink_to(target); values[field] = link
    with pytest.raises(PreflightError, match="regular file"): _request(values)


@pytest.mark.parametrize("field,limit", (("capacity_snapshot_wire", 16 * 1024), ("stage_a_envelope_wire", 32 * 1024), ("epoch0_tree_file", 8 * 1024), ("quota_profile", 64 * 1024)))
def test_preflight_rejects_oversized_inputs(tmp_path: Path, field: str, limit: int) -> None:
    values = _valid_inputs(tmp_path); path = values[field]; assert isinstance(path, Path)
    path.write_bytes(b"x" * (limit + 1))
    with pytest.raises(PreflightError, match="exceeds byte limit"): _request(values)


def test_preflight_rejects_dirty_repo_and_fabricated_receipt(tmp_path: Path) -> None:
    values = _valid_inputs(tmp_path); repository = values["repository"]; assert isinstance(repository, Path)
    (repository / "dirty").write_bytes(b"x")
    with pytest.raises(PreflightError, match="not clean"): _request(values)
    (repository / "dirty").unlink()
    output = values["native_envelope_receipt_output"]; assert isinstance(output, Path)
    output.write_text('{"forged":true}', encoding="ascii")
    with pytest.raises(PreflightError, match="receipt output must be fresh"): _request(values)


def test_preflight_marks_counterfeit_verifier_unverified_and_rejects_unbound_path(tmp_path: Path) -> None:
    values = _valid_inputs(tmp_path); verifier = values["native_envelope_verifier_binary"]; assert isinstance(verifier, Path)
    _write_verifier(verifier, {}, succeeds=False)
    request = _request(values)
    assert request["verdict"] == "UNVERIFIED_TOOL_IDENTITY_DOCUMENT"
    assert request["tool_identity_document_matches_observed_binaries"] is False
    values = _valid_inputs(tmp_path / "other")
    values["native_envelope_verifier_binary"] = _helper(tmp_path / "other-verifier", "a" * 64)
    with pytest.raises(PreflightError, match="invoked verifier identity is not bound"): _request(values)


def test_preflight_rejects_fifo_without_blocking_and_quota_contract(tmp_path: Path) -> None:
    values = _valid_inputs(tmp_path); fifo = tmp_path / "snapshot.fifo"
    import os
    os.mkfifo(fifo); values["capacity_snapshot_wire"] = fifo
    with pytest.raises(PreflightError, match="regular file"): _request(values)
    values = _valid_inputs(tmp_path / "contract"); quota = values["quota_profile"]; assert isinstance(quota, Path)
    quota.write_text('{"schema_version":1,"schema_version":1}', encoding="ascii")
    with pytest.raises(PreflightError, match="duplicate JSON field"): _request(values)


def test_preflight_does_not_run_or_claim_envelope_verification(tmp_path: Path) -> None:
    values = _valid_inputs(tmp_path); envelope = values["stage_a_envelope_wire"]; assert isinstance(envelope, Path)
    envelope.write_bytes(b"different-envelope")
    request = _request(values)
    assert request["verdict"] == "UNVERIFIED_TOOL_IDENTITY_DOCUMENT"
    assert "stage_a_native_verification_receipt_sha256" not in request
