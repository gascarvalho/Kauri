from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kauri_experiment import operator_capacity_v3_materializer as subject


def _hex(number: int, length: int = 64) -> str:
    return f"{number:0{length}x}"[-length:]


Kauri = Path(__file__).resolve().parents[3]
BUILD = Kauri / "build-adaptive"


def _run(*command: str) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    return result.stdout


def _key_pair(line: str) -> dict[str, str]:
    public, secret = line.split()
    assert public.startswith("pub:") and secret.startswith("sec:")
    return {"pub": public.removeprefix("pub:"), "sec": secret.removeprefix("sec:")}


@pytest.fixture(scope="module")
def native_identities(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """Generate one real owner-only W18 identity bundle and native receipt."""
    binaries = {
        "adaptation_manager": BUILD / "examples/adaptation-manager",
        "hotstuff_app": BUILD / "examples/hotstuff-app",
        "identity_parity_verifier": BUILD / "examples/operator-capacity-identity-parity-verify",
    }
    for binary in (*binaries.values(), BUILD / "hotstuff-keygen", BUILD / "hotstuff-tls-keygen"):
        assert binary.is_file(), f"missing native test binary: {binary}"
    root = tmp_path_factory.mktemp("w18-native-identities")
    bls = [_key_pair(line) for line in _run(
        str(BUILD / "hotstuff-keygen"), "--secure-bls-preallocation", "--num", "31", "--algo", "bls"
    ).splitlines()]
    tls = []
    for line in _run(str(BUILD / "hotstuff-tls-keygen"), "--num", "32").splitlines():
        certificate, secret, certificate_id = line.split()
        tls.append({"crt": certificate.removeprefix("crt:"), "sec": secret.removeprefix("sec:"),
                    "cid": certificate_id.removeprefix("cid:")})
    issuer = _key_pair(_run(
        str(BUILD / "hotstuff-keygen"), "--num", "1", "--algo", "secp256k1"
    ).strip())
    identities = {"bls": bls, "tls": tls, "issuer": issuer}
    parsed_bls, parsed_tls, parsed_issuer = subject._identities(identities)
    bundle = root / "identities.bundle"
    descriptor = os.open(bundle, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, subject._identity_bundle(parsed_bls, parsed_tls, parsed_issuer))
    finally:
        os.close(descriptor)
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600
    revision = "e" * 40
    receipt = root / "identity-parity-receipt.json"
    _run(str(binaries["identity_parity_verifier"]), "--identity-bundle", str(bundle),
         "--source-revision", revision, "--expected-public-fingerprint",
         subject._identity_public_fingerprint(parsed_bls, parsed_tls, parsed_issuer), "--output", str(receipt))
    return {"identities": identities, "receipt": receipt, "binaries": binaries}


def _approval(tmp_path: Path, *, binaries: dict[str, Path], revision: str) -> dict[str, object]:
    native_tools = {
        "adaptation_manager": binaries["adaptation_manager"],
        "hotstuff_app": binaries["hotstuff_app"],
        "keygen": BUILD / "hotstuff-keygen",
        "tls_keygen": BUILD / "hotstuff-tls-keygen",
        "capacity_digest": BUILD / "examples/operator-capacity-snapshot-digest",
        "epoch0_digest": BUILD / "examples/operator-capacity-snapshot-digest",
        "stage_a_envelope_signer": BUILD / "examples/operator-capacity-label-envelope-sign",
        "stage_a_envelope_verifier": BUILD / "examples/operator-capacity-label-envelope-verify",
        "stage_b_authorization_verifier": BUILD / "examples/operator-capacity-authorization-verify",
        "identity_parity_verifier": binaries["identity_parity_verifier"],
    }
    assert set(native_tools) == subject.REQUIRED_BINARIES
    document = {"schema_version": 1, "kind": "kauri-n31-operator-capacity-tool-identity-approval-v1",
                "verdict": "EXTERNAL_TOOL_IDENTITY_APPROVED", "revision": revision,
                "approval_ref": "test-external-w18", "approved_at_utc": "2026-09-29T00:00:00Z",
                "binary_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for name, path in native_tools.items()}}
    path = tmp_path / "tool-identity-approval.json"
    path.write_bytes(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n")
    return {"path": path, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _inputs(tmp_path: Path, native_identities: dict[str, object]) -> dict[str, object]:
    envelope = tmp_path / "stage-a.wire"; epoch0 = tmp_path / "epoch0-approved.tree"
    for path, raw in ((envelope, b"envelope"), (epoch0, subject.canonical_e0_tree())):
        path.write_bytes(raw)
    envelope_sha = hashlib.sha256(b"envelope").hexdigest()
    tree_sha = hashlib.sha256(subject.canonical_e0_tree()).hexdigest()
    identities = deepcopy(native_identities["identities"])
    stage_a = {"envelope_path": envelope, "envelope_sha256": envelope_sha,
               "label_issuer_id": 73, "label_issuer_reference": "w18-label", "label_issuer_public_key_hex": "a" * 66,
               "label_issuer_public_key_fingerprint": "b" * 64, "approved_capacity_digest": "c" * 64}
    revision = "e" * 40
    receipt = tmp_path / "stage-a-native-receipt.json"
    receipt_value = {
        "schema_version": 1, "kind": "kauri-operator-capacity-native-envelope-verification-receipt-v1",
        "verdict": "NATIVE_ENVELOPE_VERIFIED_NO_EXECUTION", "envelope_wire_sha256": envelope_sha,
        "envelope_canonical_digest": "f" * 64, "approved_capacity_digest": "c" * 64,
        "issuer_id": 73, "issuer_reference": "w18-label", "issuer_public_key_fingerprint": "b" * 64,
        "arm": "fast_priority_treatment", "source_revision": revision, "verification_monotonic_raw_ns": 1,
        "epoch0_tree_file_sha256": tree_sha, "epoch0_consensus_digest": "a" * 64,
        "epoch0_topology_digest": "d" * 64,
    }
    receipt.write_bytes(json.dumps(receipt_value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n")
    identity_receipt = native_identities["receipt"]
    binaries = native_identities["binaries"]
    return {
        "arm": "treatment", "run_id": "w18-local-001", "source_instance": "manager-001",
        "ports": {"peer_base": 18000, "client_base": 19000, "manager": 20000},
        "binaries": binaries,
        "identities": identities,
        "identity_parity_receipt": {"path": identity_receipt, "sha256": hashlib.sha256(identity_receipt.read_bytes()).hexdigest()},
        "tool_identity_approval": _approval(tmp_path, binaries=binaries, revision=revision),
        "stage_a": stage_a,
        "epoch0_tree": {"path": epoch0, "sha256": tree_sha, "topology_digest": "d" * 64},
        "stage_a_verifier_receipt": {"path": receipt, "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()},
        "source_revision": revision,
        "stage_b_issuer_reference": "w18-manager", "hard_deadline_ns": 1000000,
    }


def test_materializes_canonical_all_live_v3_input_without_execution(tmp_path: Path, native_identities: dict[str, object]) -> None:
    values = _inputs(tmp_path, native_identities); result = subject.materialize_operator_capacity_v3(tmp_path / "out", **values)
    manifest = result["manifest"]; manager = result["manager_argv"]; replicas = result["replica_argv"]
    assert manifest["verdict"] == "MATERIALIZED_NO_EXECUTION"
    assert manifest["claim_eligible"] is False and len(replicas) == 31
    assert manager.count("--activation-readiness-member") == 31
    assert manager[manager.index("--activation-readiness-release-count") + 1] == "31"
    assert manager[manager.index("--activation-readiness-maximum-delivery-attempts") + 1] == "1"
    assert manager[manager.index("--activation-readiness-retry-interval-ticks") + 1] == "30000"
    assert "--protocol-mode" in manager and manager[manager.index("--protocol-mode") + 1] == "adaptive_v3"
    assert "--experiment-byzantine-mode" not in manager
    assert all("--experiment-byzantine-mode" not in argv for argv in replicas)
    assert "experiment-exact-timeout-attempt-evidence-v3 = true" in (
        tmp_path / "out/config/hotstuff.gen.conf").read_text().splitlines()
    readiness = [line.split(" = ", 1)[1] for line in
                 (tmp_path / "out/config/hotstuff.gen.conf").read_text().splitlines()
                 if line.startswith("activation-readiness-member = ")]
    assert readiness == [f"{replica},{key['pub']}" for replica, key in
                         enumerate(values["identities"]["bls"])]
    lines = (tmp_path / "out/config/epoch0.tree").read_text().splitlines()
    assert len(lines) == 21 and all(line.startswith("fan:5 pipe:2 ") for line in lines)
    assert {int(line.split()[2]) for line in lines} == set(range(6))
    assert (tmp_path / "out/materialization-manifest.json").is_file()
    assert (tmp_path / "out/config/epoch0.tree").read_bytes() == (tmp_path / "epoch0-approved.tree").read_bytes()
    assert manifest["epoch0_tree"]["topology_digest"] == "d" * 64
    assert manifest["stage_a_verifier_receipt_sha256"] == values["stage_a_verifier_receipt"]["sha256"]
    assert manifest["identity_parity_receipt_sha256"] == values["identity_parity_receipt"]["sha256"]
    for path in (tmp_path / "out/raw", tmp_path / "out/transitions", tmp_path / "out/transitions/e0-to-e1-operator-capacity"):
        assert path.is_dir() and stat.S_IMODE(path.stat().st_mode) == 0o700
    assert list((tmp_path / "out/raw").iterdir()) == []
    for argv in replicas:
        assert argv[argv.index("--structured-event-commit-observer-id") + 1] == "replica-0"
        assert argv[argv.index("--structured-event-commit-observer-instance") + 1] == "manager-001-replica-0"


def test_rejects_stage_a_hash_drift_without_creating_root(tmp_path: Path, native_identities: dict[str, object]) -> None:
    values = _inputs(tmp_path, native_identities); values["stage_a"]["envelope_sha256"] = "0" * 64
    with pytest.raises(subject.OperatorCapacityV3MaterializerError, match="differ"):
        subject.materialize_operator_capacity_v3(tmp_path / "out", **values)
    assert not (tmp_path / "out").exists()


def test_rejects_unapproved_replica_executable_before_materialization(
    tmp_path: Path, native_identities: dict[str, object],
) -> None:
    values = _inputs(tmp_path, native_identities)
    replacement = tmp_path / "unapproved-hotstuff-app"
    replacement.write_bytes(b"unapproved replica executable")
    values["binaries"]["hotstuff_app"] = replacement
    with pytest.raises(subject.OperatorCapacityV3MaterializerError, match="hotstuff app differs from external approval"):
        subject.materialize_operator_capacity_v3(tmp_path / "out", **values)
    assert not (tmp_path / "out").exists()


def test_rejects_duplicate_identity_before_materialization(tmp_path: Path, native_identities: dict[str, object]) -> None:
    values = _inputs(tmp_path, native_identities); values["identities"]["bls"][1]["pub"] = values["identities"]["bls"][0]["pub"]
    with pytest.raises(subject.OperatorCapacityV3MaterializerError, match="not unique"):
        subject.materialize_operator_capacity_v3(tmp_path / "out", **values)
    assert not (tmp_path / "out").exists()


def test_rejects_epoch0_digest_drift_before_materialization(tmp_path: Path, native_identities: dict[str, object]) -> None:
    values = _inputs(tmp_path, native_identities); values["epoch0_tree"]["sha256"] = "0" * 64
    with pytest.raises(subject.OperatorCapacityV3MaterializerError, match="caller-pinned"):
        subject.materialize_operator_capacity_v3(tmp_path / "out", **values)
    assert not (tmp_path / "out").exists()


def test_rejects_noncanonical_e0_schedule_even_with_matching_digest(tmp_path: Path, native_identities: dict[str, object]) -> None:
    values = _inputs(tmp_path, native_identities)
    epoch0 = tmp_path / "epoch0-approved.tree"
    lines = epoch0.read_text().splitlines(); fields = lines[0].split(); fields[2], fields[3] = fields[3], fields[2]
    epoch0.write_text("\n".join([" ".join(fields), *lines[1:]]) + "\n")
    values["epoch0_tree"]["sha256"] = hashlib.sha256(epoch0.read_bytes()).hexdigest()
    with pytest.raises(subject.OperatorCapacityV3MaterializerError, match="slow-root schedule"):
        subject.materialize_operator_capacity_v3(tmp_path / "out", **values)
    assert not (tmp_path / "out").exists()


def test_rejects_stage_a_receipt_with_swapped_valid_tree_binding(tmp_path: Path, native_identities: dict[str, object]) -> None:
    values = _inputs(tmp_path, native_identities)
    epoch0 = Path(values["epoch0_tree"]["path"])
    lines = epoch0.read_text().splitlines(); fields = lines[0].split(); fields[3], fields[4] = fields[4], fields[3]
    epoch0.write_text("\n".join([" ".join(fields), *lines[1:]]) + "\n")
    values["epoch0_tree"]["sha256"] = hashlib.sha256(epoch0.read_bytes()).hexdigest()
    with pytest.raises(subject.OperatorCapacityV3MaterializerError, match="does not bind"):
        subject.materialize_operator_capacity_v3(tmp_path / "out", **values)
    assert not (tmp_path / "out").exists()


def test_rejects_zero_topology_digest_even_with_valid_receipt_pin(tmp_path: Path, native_identities: dict[str, object]) -> None:
    values = _inputs(tmp_path, native_identities)
    values["epoch0_tree"]["topology_digest"] = "0" * 64
    with pytest.raises(subject.OperatorCapacityV3MaterializerError, match="must not be zero"):
        subject.materialize_operator_capacity_v3(tmp_path / "out", **values)
    assert not (tmp_path / "out").exists()


def test_rejects_identity_secret_drift_after_native_parity_check(tmp_path: Path, native_identities: dict[str, object]) -> None:
    values = _inputs(tmp_path, native_identities); values["identities"]["bls"][0]["sec"] = _hex(999)
    with pytest.raises(subject.OperatorCapacityV3MaterializerError, match="exact identity inputs"):
        subject.materialize_operator_capacity_v3(tmp_path / "out", **values)
    assert not (tmp_path / "out").exists()


def test_rejects_fake_identities_with_a_self_authored_matching_receipt(
    tmp_path: Path, native_identities: dict[str, object],
) -> None:
    """Only the pinned CLI, not JSON fields, can attest key-pair parity."""
    values = _inputs(tmp_path, native_identities)
    fake = {
        "bls": [{"pub": _hex(index + 1, 96), "sec": _hex(index + 101)} for index in range(31)],
        "tls": [{"crt": _hex(index + 201), "sec": _hex(index + 301), "cid": _hex(index + 401)} for index in range(32)],
        "issuer": {"pub": "02" + _hex(501), "sec": _hex(601)},
    }
    bls, tls, issuer = subject._identities(fake)
    self_authored = {
        "schema_version": 1, "kind": "kauri-operator-capacity-native-identity-parity-receipt-v1",
        "verdict": "NATIVE_IDENTITY_PARITY_VERIFIED_NO_EXECUTION", "source_revision": values["source_revision"],
        "identity_bundle_sha256": hashlib.sha256(subject._identity_bundle(bls, tls, issuer)).hexdigest(),
        "public_identity_fingerprint": subject._identity_public_fingerprint(bls, tls, issuer),
        "bls_replicas": 31, "tls_identities": 32,
    }
    receipt = tmp_path / "self-authored-fake-identity-receipt.json"
    receipt.write_bytes(json.dumps(self_authored, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n")
    values["identities"] = fake
    values["identity_parity_receipt"] = {"path": receipt, "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}
    with pytest.raises(subject.OperatorCapacityV3MaterializerError, match="pinned native identity-parity verifier rejected"):
        subject.materialize_operator_capacity_v3(tmp_path / "out", **values)
    assert not (tmp_path / "out").exists()


def test_rejects_overlapping_replica_ports_before_materialization(tmp_path: Path, native_identities: dict[str, object]) -> None:
    values = _inputs(tmp_path, native_identities); values["ports"] = {"peer_base": 18000, "client_base": 18030, "manager": 20000}
    with pytest.raises(subject.OperatorCapacityV3MaterializerError, match="overlap"):
        subject.materialize_operator_capacity_v3(tmp_path / "out", **values)
    assert not (tmp_path / "out").exists()
