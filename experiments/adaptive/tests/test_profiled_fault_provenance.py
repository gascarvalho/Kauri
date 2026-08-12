"""Exact-revision build-provenance tests for the profiled runner."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _runtime():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.profiled_fault_runtime"
    )


def _layout(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    repository = tmp_path / "Kauri"
    build = repository / "build-adaptive"
    examples = build / "examples"
    examples.mkdir(parents=True)
    (build / "CMakeCache.txt").write_text(
        f"CMAKE_HOME_DIRECTORY:INTERNAL={repository}\n",
        encoding="utf-8",
    )
    binaries = {
        "app": examples / "hotstuff-app",
        "client": examples / "hotstuff-client",
        "manager": examples / "adaptation-manager",
        "keygen": build / "hotstuff-keygen",
        "tls_keygen": build / "hotstuff-tls-keygen",
        "epoch_profile_digest": examples / "epoch-profile-digest",
    }
    for name, path in binaries.items():
        path.write_bytes(f"{name}-exact-build".encode())
        path.chmod(0o700)
    for name, path in _runtime().exact_build_metadata_paths(build).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(f"{name}-metadata".encode())
    return repository, build, binaries


def _write_provenance(
    runtime: object,
    repository: Path,
    build: Path,
    binaries: dict[str, Path],
    *,
    revision: str = "a" * 40,
) -> Path:
    record = {
        "schema_version": 1,
        "revision": revision,
        "repository": str(repository.resolve()),
        "build_directory": str(build.resolve()),
        "cmake_cache_sha256": runtime.sha256_file(build / "CMakeCache.txt"),
        "build_command": runtime.exact_build_command(build),
        "build_metadata": {
            name: {
                "path": str(path.resolve()),
                "size_bytes": path.stat().st_size,
                "sha256": runtime.sha256_file(path),
            }
            for name, path in runtime.exact_build_metadata_paths(build).items()
        },
        "binaries": {
            name: {
                "path": str(path.resolve()),
                "size_bytes": path.stat().st_size,
                "sha256": runtime.sha256_file(path),
            }
            for name, path in binaries.items()
        },
    }
    path = build / runtime.BUILD_PROVENANCE_FILENAME
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_verify_build_provenance_binds_exact_revision_and_binary_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    repository, build, binaries = _layout(tmp_path)
    provenance = _write_provenance(runtime, repository, build, binaries)
    monkeypatch.setattr(runtime, "verify_repository_state", lambda _repo: "a" * 40)

    record = runtime.verify_exact_build_provenance(
        repository=repository,
        build_directory=build,
        provenance_path=provenance,
        binaries=binaries,
    )

    assert record["revision"] == "a" * 40
    assert set(record["binaries"]) == set(binaries)
    assert record["binaries"]["client"] == {
        "path": str(binaries["client"].resolve()),
        "size_bytes": binaries["client"].stat().st_size,
        "sha256": runtime.sha256_file(binaries["client"]),
    }
    assert "hotstuff-client" in record["build_command"]


@pytest.mark.parametrize("drift", ("revision", "binary", "path"))
def test_verify_build_provenance_rejects_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    runtime = _runtime()
    repository, build, binaries = _layout(tmp_path)
    provenance = _write_provenance(runtime, repository, build, binaries)
    monkeypatch.setattr(runtime, "verify_repository_state", lambda _repo: "a" * 40)
    supplied = dict(binaries)
    if drift == "revision":
        monkeypatch.setattr(runtime, "verify_repository_state", lambda _repo: "b" * 40)
    elif drift == "binary":
        binaries["app"].write_bytes(b"stale-or-mutated")
    else:
        arbitrary = build / "arbitrary-app"
        arbitrary.write_bytes(binaries["app"].read_bytes())
        arbitrary.chmod(0o700)
        supplied["app"] = arbitrary

    with pytest.raises(
        runtime.ProfiledFaultRuntimeError,
        match="provenance|revision|binary|path|hash|exact",
    ):
        runtime.verify_exact_build_provenance(
            repository=repository,
            build_directory=build,
            provenance_path=provenance,
            binaries=supplied,
        )
