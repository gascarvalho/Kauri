from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kauri_experiment import operator_capacity_stage_a_preflight as subject


REVISION = "a" * 40
DIGEST = "b" * 64


def _quota_payload() -> dict[str, object]:
    return {
        "assignments": [
            {
                "capacity_class": "slow" if replica < 6 else "fast",
                "cpu_quota_percent": 25 if replica < 6 else 100,
                "replica_id": replica,
            }
            for replica in range(31)
        ],
        "base_profile_id": "n31-static-resource-cpu-sham-v1",
        "base_profile_canonical_sha256":
            "2ed182ed95fe8514c80eb861ed2e86654afaaf6b881a2ede6fc1a03d0565b766",
        "base_profile_sha256":
            "285aa55cb33637009ccd491d74830cd7485bcd83cb993c33c488dbff6fe4bf09",
        "contract_id": "n31-static-resource-cpu-sham-quota-v1",
        "enabled": True,
        "figure_eligible": False,
        "launcher": "systemd-user-scope-cpu-quota-v1",
        "manager_visibility": "none",
        "sampling_interval_ms": 1000,
        "schema_version": 1,
    }


def _write(path: Path, value: bytes) -> None:
    path.write_bytes(value)


def _files(tmp_path: Path) -> dict[str, Path]:
    values = {name: tmp_path / name for name in subject.REQUIRED_BINARIES}
    for name, path in values.items():
        _write(path, name.encode("ascii"))
    for name in ("tree", "snapshot", "envelope", "quota"):
        _write(tmp_path / name, name.encode("ascii"))
    _write(tmp_path / "quota", subject.canonical_json(_quota_payload()))
    return values


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, approval_sha: str | None = None):
    binaries = _files(tmp_path)
    observed = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in binaries.items()}
    approval = {"schema_version": 1, "kind": subject.APPROVAL_KIND,
                "verdict": subject.APPROVAL_VERDICT, "revision": REVISION,
                "approval_ref": "external-20260929", "approved_at_utc": "2026-09-29T00:00:00Z",
                "binary_sha256": observed}
    approval_path = tmp_path / "approval.json"; _write(approval_path, subject.canonical_json(approval))
    receipt = tmp_path / "native-receipt.json"
    verifier = binaries["stage_a_envelope_verifier"]
    def fake_run(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=REVISION + "\n" if command[-1] == "HEAD" else "", stderr="")
        receipt_value = {"kind": subject.VERIFIER_KIND, "verdict": subject.VERIFIER_VERDICT,
                         "envelope_wire_sha256": hashlib.sha256((tmp_path / "envelope").read_bytes()).hexdigest(),
                         "approved_capacity_digest": DIGEST, "issuer_id": 73,
                         "issuer_reference": "issuer", "issuer_public_key_fingerprint": DIGEST,
                         "arm": "fast_priority_treatment", "source_revision": REVISION,
                         "epoch0_tree_file_sha256": hashlib.sha256((tmp_path / "tree").read_bytes()).hexdigest(),
                         "epoch0_topology_digest": DIGEST}
        _write(receipt, subject.canonical_json(receipt_value))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    monkeypatch.setattr(subject.subprocess, "run", fake_run)
    return dict(repository=tmp_path, arm="treatment", output_root=tmp_path / "result",
                epoch0_tree_file=tmp_path / "tree", capacity_snapshot_wire=tmp_path / "snapshot",
                stage_a_envelope_wire=tmp_path / "envelope", quota_profile=tmp_path / "quota",
                binaries=binaries, verifier_binary=verifier, verifier_receipt_output=receipt,
                issuer_id=73, issuer_reference="issuer", issuer_public_key_hex="c" * 66,
                issuer_public_key_fingerprint=DIGEST, approved_capacity_digest=DIGEST,
                epoch0_topology_digest=DIGEST, tool_identity_approval_receipt=approval_path,
                expected_tool_identity_approval_sha256=approval_sha or hashlib.sha256(approval_path.read_bytes()).hexdigest())


def test_stage_a_preflight_returns_request_but_never_authorization(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    kwargs = _prepare(monkeypatch, tmp_path)
    preflight, request = subject.prepare_stage_a_preflight(**kwargs)
    assert preflight["verdict"] == "PREFLIGHT_OK_NO_EXECUTION"
    assert request["verdict"] == "EXECUTION_AUTHORIZATION_REQUEST_REQUIRED"
    assert request["preflight_sha256"] == hashlib.sha256(subject.canonical_json(preflight)).hexdigest()
    assert not kwargs["output_root"].exists()


def test_stage_a_preflight_requires_caller_pinned_external_approval_hash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    kwargs = _prepare(monkeypatch, tmp_path, approval_sha="0" * 64)
    with pytest.raises(subject.StageAPreflightError, match="caller pin"):
        subject.prepare_stage_a_preflight(**kwargs)


def test_stage_a_preflight_rejects_missing_required_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    kwargs = _prepare(monkeypatch, tmp_path)
    kwargs["binaries"].pop("stage_b_authorization_verifier")
    with pytest.raises(subject.StageAPreflightError, match="identity set"):
        subject.prepare_stage_a_preflight(**kwargs)


def test_stage_a_preflight_rejects_missing_identity_parity_verifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    kwargs = _prepare(monkeypatch, tmp_path)
    kwargs["binaries"].pop("identity_parity_verifier")
    with pytest.raises(subject.StageAPreflightError, match="identity set"):
        subject.prepare_stage_a_preflight(**kwargs)


def test_stage_a_preflight_rejects_replica_binary_drift_after_external_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    kwargs = _prepare(monkeypatch, tmp_path)
    _write(kwargs["binaries"]["hotstuff_app"], b"changed-app")
    with pytest.raises(subject.StageAPreflightError, match="does not match observed binaries"):
        subject.prepare_stage_a_preflight(**kwargs)


@pytest.mark.parametrize(
    "payload",
    (
        b"not-json",
        b'{"schema_version":1,"schema_version":1}',
    ),
)
def test_stage_a_preflight_rejects_malformed_quota_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: bytes,
) -> None:
    kwargs = _prepare(monkeypatch, tmp_path)
    _write(kwargs["quota_profile"], payload)
    with pytest.raises(subject.StageAPreflightError, match="frozen N31 contract"):
        subject.prepare_stage_a_preflight(**kwargs)


@pytest.mark.parametrize(
    "field,value",
    (
        ("launcher", "uncontrolled-process"),
        ("manager_visibility", "capacity-labels-visible"),
    ),
)
def test_stage_a_preflight_rejects_quota_contract_metadata_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, value: str,
) -> None:
    kwargs = _prepare(monkeypatch, tmp_path)
    quota = _quota_payload()
    quota[field] = value
    _write(kwargs["quota_profile"], subject.canonical_json(quota))
    with pytest.raises(subject.StageAPreflightError, match="frozen N31 contract"):
        subject.prepare_stage_a_preflight(**kwargs)


def test_stage_a_preflight_rejects_one_replica_quota_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    kwargs = _prepare(monkeypatch, tmp_path)
    quota = _quota_payload()
    assignments = quota["assignments"]
    assert isinstance(assignments, list) and isinstance(assignments[0], dict)
    assignments[0]["cpu_quota_percent"] = 26
    _write(kwargs["quota_profile"], subject.canonical_json(quota))
    with pytest.raises(subject.StageAPreflightError, match="frozen N31 contract"):
        subject.prepare_stage_a_preflight(**kwargs)
