from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from experiments.adaptive.kauri_experiment.operator_capacity_preflight import (
    PreflightError, canonical_request,
)


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
    subprocess.run(("git", "-C", str(path), "commit", "-qm", "fixture"), check=True)
    return path


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


def _write_quota(path: Path, payload: object | None = None) -> None:
    path.write_text(
        json.dumps(_quota_payload() if payload is None else payload),
        encoding="ascii",
    )


def test_preflight_derives_native_digests_and_emits_no_execution(tmp_path: Path) -> None:
    digest_a, digest_b = "a" * 64, "b" * 64
    snapshot, tree, quota = tmp_path / "snapshot", tmp_path / "tree", tmp_path / "quota"
    for path in (snapshot, tree): path.write_bytes(b"fixture")
    _write_quota(quota)
    capacity = _helper(tmp_path / "capacity", digest_a)
    epoch0 = _helper(tmp_path / "epoch0", digest_b)
    binaries = {"app": tmp_path / "app", "keygen": tmp_path / "keygen", "tls_keygen": tmp_path / "tls",
                "capacity_digest": capacity, "epoch0_digest": epoch0}
    for name, path in binaries.items():
        if name not in {"capacity_digest", "epoch0_digest"}: path.write_bytes(b"binary")
    request = canonical_request(repository=_clean_repository(tmp_path / "repo"), snapshot_wire=snapshot,
        capacity_digest_binary=capacity, epoch0_digest_binary=epoch0, epoch0_arm="slow-roots",
        epoch0_tree_file=tree, arm="treatment", quota_profile=quota, output_root=tmp_path / "fresh",
        issuer_public_key_fingerprint="c" * 64, binaries=binaries)
    assert request["verdict"] == "PREFLIGHT_OK_NO_EXECUTION"
    assert request["snapshot_semantic_digest"] == digest_a
    assert request["epoch0_tree_digest"] == digest_b
    assert request["quota_contract_id"] == "n31-static-resource-cpu-sham-quota-v1"
    assert request["slow_replica_ids"] == list(range(6))
    assert request["fast_replica_ids"] == list(range(6, 31))
    assert request["epoch0_exposed_slow_root_ids"] == list(range(6))
    assert request["claim_eligible"] is False


def test_preflight_rejects_missing_native_helper(tmp_path: Path) -> None:
    path = tmp_path / "input"; path.write_bytes(b"fixture")
    quota = tmp_path / "quota.json"; _write_quota(quota)
    with pytest.raises(PreflightError, match="native helper unavailable"):
        canonical_request(repository=_clean_repository(tmp_path / "repo"), snapshot_wire=path,
            capacity_digest_binary=tmp_path / "missing", epoch0_digest_binary=tmp_path / "missing2",
            epoch0_arm="slow-roots", epoch0_tree_file=path, arm="sham", quota_profile=quota,
            output_root=tmp_path / "fresh", issuer_public_key_fingerprint="c" * 64,
            binaries={"app": path, "keygen": path, "tls_keygen": path,
                      "capacity_digest": tmp_path / "missing", "epoch0_digest": tmp_path / "missing2"})


def _valid_inputs(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    digest = "a" * 64
    snapshot, tree, quota = tmp_path / "snapshot", tmp_path / "tree", tmp_path / "quota"
    for path in (snapshot, tree): path.write_bytes(b"fixture")
    _write_quota(quota)
    capacity, epoch0 = _helper(tmp_path / "capacity", digest), _helper(tmp_path / "epoch0", digest)
    binaries = {"app": tmp_path / "app", "keygen": tmp_path / "keygen", "tls_keygen": tmp_path / "tls",
                "capacity_digest": capacity, "epoch0_digest": epoch0}
    for name, path in binaries.items():
        if name not in {"capacity_digest", "epoch0_digest"}: path.write_bytes(b"binary")
    return {"repository": _clean_repository(tmp_path / "repo"), "snapshot_wire": snapshot,
            "capacity_digest_binary": capacity, "epoch0_digest_binary": epoch0,
            "epoch0_arm": "slow-roots", "epoch0_tree_file": tree, "arm": "sham",
            "quota_profile": quota, "output_root": tmp_path / "fresh",
            "issuer_public_key_fingerprint": "c" * 64, "binaries": binaries}


@pytest.mark.parametrize("field", ("snapshot_wire", "epoch0_tree_file", "quota_profile"))
def test_preflight_rejects_symlink_inputs(tmp_path: Path, field: str) -> None:
    values = _valid_inputs(tmp_path)
    target = tmp_path / f"{field}-target"; target.write_bytes(b"fixture")
    link = tmp_path / f"{field}-link"; link.symlink_to(target)
    values[field] = link
    with pytest.raises(PreflightError, match="regular file"):
        canonical_request(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field,limit", (("snapshot_wire", 16 * 1024),
                                           ("epoch0_tree_file", 8 * 1024),
                                           ("quota_profile", 64 * 1024)))
def test_preflight_rejects_oversized_inputs(tmp_path: Path, field: str, limit: int) -> None:
    values = _valid_inputs(tmp_path)
    path = values[field]; assert isinstance(path, Path)
    path.write_bytes(b"x" * (limit + 1))
    with pytest.raises(PreflightError, match="exceeds byte limit"):
        canonical_request(**values)  # type: ignore[arg-type]


def test_preflight_rejects_dirty_repo_failed_helper_existing_output_and_mismatch(tmp_path: Path) -> None:
    values = _valid_inputs(tmp_path)
    repository = values["repository"]; assert isinstance(repository, Path)
    (repository / "dirty").write_bytes(b"x")
    with pytest.raises(PreflightError, match="not clean"):
        canonical_request(**values)  # type: ignore[arg-type]
    (repository / "dirty").unlink()
    failed = _helper(tmp_path / "failed", "not-a-digest")
    values["capacity_digest_binary"] = failed
    with pytest.raises(PreflightError, match="identity is not bound"):
        canonical_request(**values)  # type: ignore[arg-type]
    values["binaries"]["capacity_digest"] = failed  # type: ignore[index]
    with pytest.raises(PreflightError, match="rejected input"):
        canonical_request(**values)  # type: ignore[arg-type]
    values = _valid_inputs(tmp_path / "second")
    output = values["output_root"]; assert isinstance(output, Path)
    output.mkdir()
    with pytest.raises(PreflightError, match="fresh"):
        canonical_request(**values)  # type: ignore[arg-type]


def test_preflight_rejects_input_mutation_during_native_derivation(tmp_path: Path) -> None:
    values = _valid_inputs(tmp_path)
    mutator = tmp_path / "mutator"
    mutator.write_text("#!/bin/sh\nprintf 'a%.0s' $(seq 1 64); printf '\\n'; printf x >> \"$3\"\n",
                       encoding="ascii")
    mutator.chmod(0o755)
    values["capacity_digest_binary"] = mutator
    values["binaries"]["capacity_digest"] = mutator  # type: ignore[index]
    with pytest.raises(PreflightError, match="changed during native derivation"):
        canonical_request(**values)  # type: ignore[arg-type]


def test_preflight_rejects_native_helper_replacing_its_own_bytes(
    tmp_path: Path,
) -> None:
    values = _valid_inputs(tmp_path)
    mutator = tmp_path / "self-mutating-capacity"
    mutator.write_text(
        "#!/bin/sh\nprintf 'a%.0s' $(seq 1 64); printf '\\n'\nprintf '# mutated\\n' >> \"$0\"\n",
        encoding="ascii",
    )
    mutator.chmod(0o755)
    values["capacity_digest_binary"] = mutator
    values["binaries"]["capacity_digest"] = mutator  # type: ignore[index]

    with pytest.raises(PreflightError, match="binary bytes changed during native derivation"):
        canonical_request(**values)  # type: ignore[arg-type]


def test_preflight_rejects_repository_drift_created_by_native_helper(
    tmp_path: Path,
) -> None:
    values = _valid_inputs(tmp_path)
    repository = values["repository"]
    assert isinstance(repository, Path)
    mutator = tmp_path / "repo-mutating-capacity"
    mutator.write_text(
        "#!/bin/sh\nprintf 'a%.0s' $(seq 1 64); printf '\\n'\nprintf x >> "
        + repr(str(repository / "tracked"))
        + "\n",
        encoding="ascii",
    )
    mutator.chmod(0o755)
    values["capacity_digest_binary"] = mutator
    values["binaries"]["capacity_digest"] = mutator  # type: ignore[index]

    with pytest.raises(PreflightError, match="repository state changed during native derivation"):
        canonical_request(**values)  # type: ignore[arg-type]


def test_preflight_rejects_fast_roots_epoch_zero(tmp_path: Path) -> None:
    values = _valid_inputs(tmp_path)
    values["epoch0_arm"] = "fast-roots"

    with pytest.raises(PreflightError, match="slow-roots baseline"):
        canonical_request(**values)  # type: ignore[arg-type]


def test_preflight_rejects_quota_assignment_with_same_class_counts(
    tmp_path: Path,
) -> None:
    values = _valid_inputs(tmp_path)
    quota = values["quota_profile"]
    assert isinstance(quota, Path)
    payload = _quota_payload()
    assignments = payload["assignments"]
    assert isinstance(assignments, list)
    assignments[0] = {
        "capacity_class": "fast", "cpu_quota_percent": 100, "replica_id": 0,
    }
    assignments[6] = {
        "capacity_class": "slow", "cpu_quota_percent": 25, "replica_id": 6,
    }
    _write_quota(quota, payload)

    with pytest.raises(PreflightError, match="frozen N31 contract"):
        canonical_request(**values)  # type: ignore[arg-type]


def test_preflight_rejects_quota_duplicate_json_key(tmp_path: Path) -> None:
    values = _valid_inputs(tmp_path)
    quota = values["quota_profile"]
    assert isinstance(quota, Path)
    quota.write_text('{"schema_version":1,"schema_version":1}', encoding="ascii")

    with pytest.raises(PreflightError, match="duplicate JSON field"):
        canonical_request(**values)  # type: ignore[arg-type]
