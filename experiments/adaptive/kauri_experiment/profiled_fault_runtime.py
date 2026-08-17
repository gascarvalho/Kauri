"""One-attempt local runtime for the frozen profiled fault shakedown.

The runtime intentionally supports only the evidence path needed by the
N=31/fanout-five crash shakedown.  It owns every child process group, never
retries an attempt, and leaves every failed or incomplete run in place.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
import csv
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import shutil
import signal
import socket
import stat
import statistics
import subprocess
import time
from typing import IO, Any
import uuid

from .faults import FaultEvidence, FaultLifecycle
from .processes import ProcessRecord, ProcessRegistry
from .profiled_fault_archive import (
    EvidenceSealError,
    create_evidence_seal,
    verify_evidence_seal,
)
from .profiled_fault_evaluation import (
    FrozenProfile,
    ProfiledFaultEvaluationError,
    build_fault_plan,
    identity_generation_commands,
    initial_epoch_input,
    load_frozen_profile,
    postfault_witnesses,
    replica_argvs,
    required_ports,
    validate_synthetic_run,
)

REQUIRED_BRANCH = "feature/adaptive-epoch-throughput"
SHIPPED_PROFILE_ID = "n31-f5-q21-sigkill-shakedown-v1"
SHIPPED_PROFILE_SHA256 = (
    "2ce1bcc8e8f6af3201710d34b23cd66c70d05a7658b7ec737e35b5a35e73bcfa"
)
INTERNAL1_PROFILE_ID = "n31-f5-q21-internal1-sigkill-shakedown-v1"
INTERNAL1_PROFILE_SHA256 = (
    "0defdaa9b69c949365eea3b3029da75cee3ea8334f845401103e2f7af8507650"
)
INTERNAL1_DIAGNOSTIC_PROFILE_ID = "n31-f5-q21-internal1-handoff-diagnostic-v1"
INTERNAL1_DIAGNOSTIC_PROFILE_SHA256 = (
    "daea7057ef840706dfc8d060c1fdcac538a54a23850084b5eb84ca431eab61c6"
)
INTERNAL1_TAIL_DIAGNOSTIC_PROFILE_ID = "n31-f5-q21-internal1-handoff-tail-diagnostic-v2"
INTERNAL1_TAIL_DIAGNOSTIC_PROFILE_SHA256 = (
    "369ba72c9a0ba92d74e128330905e2fcac2254a6e4b1e7e5d1377c418e033482"
)
INTERNAL1_FORWARDING_TAIL_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-forwarding-tail-diagnostic-v3"
)
INTERNAL1_FORWARDING_TAIL_DIAGNOSTIC_PROFILE_SHA256 = (
    "fca3d8e0b99b6cd7c576e5db2d69c406ba13b3f19af30a2445de4f604bcb9e00"
)
INTERNAL1_REPNET2_DIAGNOSTIC_PROFILE_ID = "n31-f5-q21-internal1-repnet2-diagnostic-v4"
INTERNAL1_REPNET2_DIAGNOSTIC_PROFILE_SHA256 = (
    "c61aad2d835f4ffdac79e18a7373196d82d6b03a109009abad15b17966fb4561"
)
INTERNAL1_MISSING_SIGNER_REPAIR_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-missing-signer-repair-diagnostic-v5"
)
INTERNAL1_MISSING_SIGNER_REPAIR_DIAGNOSTIC_PROFILE_SHA256 = (
    "512d69ffe4df0acb50035cb65017cc2abfdce0238f3d2fe5504957f762e3cabc"
)
INTERNAL1_STAGED_REPAIR_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-staged-repair-diagnostic-v6"
)
INTERNAL1_STAGED_REPAIR_DIAGNOSTIC_PROFILE_SHA256 = (
    "19717485f292628fdc13a99c79ab9069b728b6377cde8630ad38f0c783f7c1a0"
)
INTERNAL1_COMMIT_DWELL_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-commit-dwell-diagnostic-v7"
)
INTERNAL1_COMMIT_DWELL_DIAGNOSTIC_PROFILE_SHA256 = (
    "c9e8d6385ced8c7d70b78d22d865cea893097f6e75bf71475edbbee45a0ae84f"
)
INTERNAL1_ACK_TAIL_DIAGNOSTIC_PROFILE_ID = "n31-f5-q21-internal1-ack-tail-diagnostic-v8"
INTERNAL1_ACK_TAIL_DIAGNOSTIC_PROFILE_SHA256 = (
    "71f942e69735380216f4906e40d22c4456ee9a3cb3d5afa89c59af4690cc6750"
)
INTERNAL1_PRE_QC_CREDIT_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-pre-qc-credit-diagnostic-v9"
)
INTERNAL1_PRE_QC_CREDIT_DIAGNOSTIC_PROFILE_SHA256 = (
    "20be0d3e9a58c354e610a09441c9ad486d9db3f7f9fa5bd2b8d0ce58b382d539"
)
INTERNAL1_CONNECTION_REFRESH_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-connection-refresh-diagnostic-v10"
)
INTERNAL1_CONNECTION_REFRESH_DIAGNOSTIC_PROFILE_SHA256 = (
    "d85c9c97cf766730477dbbec601b70e4e3af637618425c682c8cc591a716f7bf"
)
INTERNAL1_CONNECTION_REFRESH_CAPACITY_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-connection-refresh-capacity-diagnostic-v11"
)
INTERNAL1_CONNECTION_REFRESH_CAPACITY_DIAGNOSTIC_PROFILE_SHA256 = (
    "36c4bdc9740a0bf92cb059f3e3068335102320d0a7db7f994ebbea6efb745b6e"
)
INTERNAL1_FRESH_CONNECTION_COALESCING_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-fresh-connection-coalescing-diagnostic-v12"
)
INTERNAL1_FRESH_CONNECTION_COALESCING_DIAGNOSTIC_PROFILE_SHA256 = (
    "be6fa779f0f6920f50172ff751a6b2523b95e4cc4e116d00fe31e3e3ebd1b42d"
)
INTERNAL1_FRESH_FIRST_DRAINING_REPAIR_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-fresh-first-draining-repair-diagnostic-v13"
)
INTERNAL1_FRESH_FIRST_DRAINING_REPAIR_DIAGNOSTIC_PROFILE_SHA256 = (
    "6058cf54c21d842c4b28b5ef2ff2ff4e6adb5a4e100c80b7a7c6ef28de6862a7"
)
INTERNAL1_PEER_IDENTITY_DISPATCH_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-peer-identity-dispatch-diagnostic-v14"
)
INTERNAL1_PEER_IDENTITY_DISPATCH_DIAGNOSTIC_PROFILE_SHA256 = (
    "0b73b9c3485cf9b3e74d09c68260cad61219d43022c8aa0972f933b54125f6b8"
)
INTERNAL1_STABLE_TREE_RECOVERY_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-stable-tree-recovery-diagnostic-v15"
)
INTERNAL1_STABLE_TREE_RECOVERY_DIAGNOSTIC_PROFILE_SHA256 = (
    "5a640ae1c12e6b4fe6a2420dc9efbc95d617388a0a2d0304a334e703392ac471"
)
INTERNAL1_FRESH_TAIL_COVERAGE_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-fresh-tail-coverage-diagnostic-v16"
)
INTERNAL1_FRESH_TAIL_COVERAGE_DIAGNOSTIC_PROFILE_SHA256 = (
    "96daff4a11185cd397105c1cfa5183ab7ce4fb378b997d94db81ab9d6d4677d2"
)
INTERNAL1_PARKED_TAIL_LATE_QC_DIAGNOSTIC_PROFILE_ID = (
    "n31-f5-q21-internal1-parked-tail-late-qc-diagnostic-v17"
)
INTERNAL1_PARKED_TAIL_LATE_QC_DIAGNOSTIC_PROFILE_SHA256 = (
    "e584f3949e384c9fa1099d62f7d28066e14a9f0a76043c8db2db9a630a54250d"
)
SHIPPED_PROFILES = {
    SHIPPED_PROFILE_ID: SHIPPED_PROFILE_SHA256,
    INTERNAL1_PROFILE_ID: INTERNAL1_PROFILE_SHA256,
    INTERNAL1_DIAGNOSTIC_PROFILE_ID: INTERNAL1_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_TAIL_DIAGNOSTIC_PROFILE_ID: INTERNAL1_TAIL_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_FORWARDING_TAIL_DIAGNOSTIC_PROFILE_ID: INTERNAL1_FORWARDING_TAIL_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_REPNET2_DIAGNOSTIC_PROFILE_ID: INTERNAL1_REPNET2_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_MISSING_SIGNER_REPAIR_DIAGNOSTIC_PROFILE_ID: INTERNAL1_MISSING_SIGNER_REPAIR_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_STAGED_REPAIR_DIAGNOSTIC_PROFILE_ID: INTERNAL1_STAGED_REPAIR_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_COMMIT_DWELL_DIAGNOSTIC_PROFILE_ID: INTERNAL1_COMMIT_DWELL_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_ACK_TAIL_DIAGNOSTIC_PROFILE_ID: INTERNAL1_ACK_TAIL_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_PRE_QC_CREDIT_DIAGNOSTIC_PROFILE_ID: INTERNAL1_PRE_QC_CREDIT_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_CONNECTION_REFRESH_DIAGNOSTIC_PROFILE_ID: INTERNAL1_CONNECTION_REFRESH_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_CONNECTION_REFRESH_CAPACITY_DIAGNOSTIC_PROFILE_ID: INTERNAL1_CONNECTION_REFRESH_CAPACITY_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_FRESH_CONNECTION_COALESCING_DIAGNOSTIC_PROFILE_ID: INTERNAL1_FRESH_CONNECTION_COALESCING_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_FRESH_FIRST_DRAINING_REPAIR_DIAGNOSTIC_PROFILE_ID: INTERNAL1_FRESH_FIRST_DRAINING_REPAIR_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_PEER_IDENTITY_DISPATCH_DIAGNOSTIC_PROFILE_ID: INTERNAL1_PEER_IDENTITY_DISPATCH_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_STABLE_TREE_RECOVERY_DIAGNOSTIC_PROFILE_ID: INTERNAL1_STABLE_TREE_RECOVERY_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_FRESH_TAIL_COVERAGE_DIAGNOSTIC_PROFILE_ID: INTERNAL1_FRESH_TAIL_COVERAGE_DIAGNOSTIC_PROFILE_SHA256,
    INTERNAL1_PARKED_TAIL_LATE_QC_DIAGNOSTIC_PROFILE_ID: INTERNAL1_PARKED_TAIL_LATE_QC_DIAGNOSTIC_PROFILE_SHA256,
}
EXPECTED_EPOCH_ZERO_DIGEST = (
    "145fac093343fa9cff20fcf49d85ad5443e93db14146f7854b17e28cf44f6d7a"
)
MANAGER_SOURCE_ID = "adaptive-manager"
ISSUER_ID = 1
MAX_REPLICA_MESSAGE_BYTES = 4 << 20
MAX_COMMAND_BYTES = 4096
MAX_ANCESTRY_BLOCKS = 128
TRANSITION_ARTIFACT_ID = "e0-to-e1-shakedown-containment"
MINIMUM_PREDECESSOR_RESIDENCY_MS = 0
REPLICA_NETWORK_WORKERS = 2
BUILD_PROVENANCE_FILENAME = "n31-exact-build-provenance.json"
EXACT_BUILD_TARGETS = (
    "hotstuff-app",
    "hotstuff-client",
    "adaptation-manager",
    "hotstuff-keygen",
    "hotstuff-tls-keygen",
    "epoch-profile-digest",
)
FORBIDDEN_TRANSITION_EVENTS = frozenset(
    {
        "epoch.generated",
        "epoch.staged",
        "epoch.acknowledged",
        "epoch.activation_armed",
        "epoch.activated",
        "epoch.command_committed",
    }
)
MANAGER_SESSION_TERMINAL_FIELDS = frozenset(
    {
        "cycle_ordinal",
        "policy_intent",
        "outcome",
        "reason",
        "transition_artifact_id",
        "predecessor_epoch_number",
        "predecessor_epoch_digest",
        "successor_epoch_number",
        "successor_epoch_digest",
        "command_payload_digest",
        "winning_activation",
        "evidence_window_activation_generation",
        "baseline_evidence_cutoff",
        "current_evidence_cutoff",
    }
)
PROCESS_READY_FIELDS = frozenset({"exit_status"})
UNREGISTERED_PROCESS_EXIT_TIMEOUT_S = 5.0


class ProfiledFaultRuntimeError(RuntimeError):
    """A run cannot safely continue or cannot qualify as evidence."""


class IncompleteProfiledFaultRun(ProfiledFaultRuntimeError):
    """The preserved attempt stopped before the frozen gate was complete."""


class RecoveryGateFailure(ProfiledFaultRuntimeError):
    """The complete source-blind window missed a frozen recovery requirement."""

    def __init__(
        self,
        message: str,
        recovery_gate: dict[str, object],
    ) -> None:
        super().__init__(message)
        self.recovery_gate = recovery_gate


def require_shipped_profile(profile: FrozenProfile) -> None:
    if SHIPPED_PROFILES.get(profile.profile_id) != profile.profile_sha256:
        raise ProfiledFaultRuntimeError(
            "live runtime requires the exact shipped frozen profile bytes"
        )


def monotonic_raw_ns() -> int:
    clock_id = getattr(time, "CLOCK_MONOTONIC_RAW", None)
    reader = getattr(time, "clock_gettime_ns", None)
    if clock_id is None or not callable(reader):
        raise ProfiledFaultRuntimeError(
            "CLOCK_MONOTONIC_RAW is required for experiment evidence"
        )
    value = int(reader(clock_id))
    if value <= 0:
        raise ProfiledFaultRuntimeError(
            "CLOCK_MONOTONIC_RAW returned a non-positive timestamp"
        )
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def write_exclusive(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def write_json_exclusive(path: Path, value: object, *, mode: int = 0o600) -> None:
    write_exclusive(path, _json_bytes(value), mode=mode)


def replace_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    write_exclusive(temporary, _json_bytes(value))
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def create_run_directory(results_root: Path) -> Path:
    results_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(results_root, 0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_directory = results_root / (f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    run_directory.mkdir(mode=0o700)
    for name in ("config", "logs", "raw", "runtime", "transitions"):
        (run_directory / name).mkdir(mode=0o700)
    return run_directory


def _git(repository: Path, arguments: Sequence[str]) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ProfiledFaultRuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def verify_repository_state(repository: Path) -> str:
    repository = repository.resolve()
    top = Path(_git(repository, ("rev-parse", "--show-toplevel"))).resolve()
    if top != repository:
        raise ProfiledFaultRuntimeError("repository is not the Kauri top level")
    branch = _git(repository, ("branch", "--show-current"))
    if branch != REQUIRED_BRANCH:
        raise ProfiledFaultRuntimeError(
            f"Kauri must remain on {REQUIRED_BRANCH}; found "
            f"{branch or 'detached HEAD'}"
        )
    revision = _git(repository, ("rev-parse", "HEAD"))
    remote = _git(repository, ("rev-parse", f"origin/{REQUIRED_BRANCH}"))
    if revision != remote:
        raise ProfiledFaultRuntimeError(
            "Kauri HEAD is not the exact pushed origin revision"
        )
    status = _git(
        repository,
        (
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude).codex",
            ":(exclude)build/Testing",
        ),
    )
    if status:
        raise ProfiledFaultRuntimeError(
            "Kauri worktree is not clean for evidence:\n" + status
        )
    return revision


def exact_build_command(build_directory: Path) -> list[str]:
    return [
        "cmake",
        "--build",
        str(build_directory.resolve()),
        "--clean-first",
        "--parallel",
        "4",
        "--target",
        *EXACT_BUILD_TARGETS,
    ]


def exact_binary_paths(repository: Path, build_directory: Path) -> dict[str, Path]:
    repository = repository.resolve()
    build_directory = build_directory.resolve()
    if build_directory != repository / "build-adaptive":
        raise ProfiledFaultRuntimeError(
            "exact build directory must be Kauri/build-adaptive"
        )
    return {
        "app": build_directory / "examples" / "hotstuff-app",
        "client": build_directory / "examples" / "hotstuff-client",
        "manager": build_directory / "examples" / "adaptation-manager",
        "keygen": build_directory / "hotstuff-keygen",
        "tls_keygen": build_directory / "hotstuff-tls-keygen",
        "epoch_profile_digest": (build_directory / "examples" / "epoch-profile-digest"),
    }


def exact_build_metadata_paths(build_directory: Path) -> dict[str, Path]:
    build_directory = build_directory.resolve()
    return {
        "cmake_cache": build_directory / "CMakeCache.txt",
        "compile_commands": build_directory / "compile_commands.json",
        "hotstuff_app_link": (
            build_directory / "examples/CMakeFiles/hotstuff-app.dir/link.txt"
        ),
        "adaptation_manager_link": (
            build_directory / "examples/CMakeFiles/adaptation-manager.dir/link.txt"
        ),
        "epoch_profile_digest_link": (
            build_directory / "examples/CMakeFiles/epoch-profile-digest.dir/link.txt"
        ),
        "hotstuff_keygen_link": (
            build_directory / "CMakeFiles/hotstuff-keygen.dir/link.txt"
        ),
        "hotstuff_tls_keygen_link": (
            build_directory / "CMakeFiles/hotstuff-tls-keygen.dir/link.txt"
        ),
    }


def _verify_cmake_cache(repository: Path, build_directory: Path) -> Path:
    cache = build_directory / "CMakeCache.txt"
    try:
        lines = cache.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProfiledFaultRuntimeError(
            f"cannot read exact-build CMake cache: {cache}"
        ) from exc
    expected = f"CMAKE_HOME_DIRECTORY:INTERNAL={repository.resolve()}"
    if expected not in lines:
        raise ProfiledFaultRuntimeError(
            "exact-build CMake cache is not bound to this Kauri source tree"
        )
    return cache


def prepare_exact_revision_build(*, repository: Path, build_directory: Path) -> Path:
    repository = repository.resolve()
    build_directory = build_directory.resolve()
    revision = verify_repository_state(repository)
    cache = _verify_cmake_cache(repository, build_directory)
    binaries = exact_binary_paths(repository, build_directory)
    provenance_path = build_directory / BUILD_PROVENANCE_FILENAME
    provenance_path.unlink(missing_ok=True)
    command = exact_build_command(build_directory)
    result = subprocess.run(
        command,
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ProfiledFaultRuntimeError(
            f"exact-revision clean build failed with exit {result.returncode}: {detail}"
        )
    if verify_repository_state(repository) != revision:
        raise ProfiledFaultRuntimeError(
            "repository changed during exact-revision build"
        )
    resolved_binaries = {
        name: _assert_executable(path, name) for name, path in binaries.items()
    }
    metadata_paths = exact_build_metadata_paths(build_directory)
    if any(not path.is_file() for path in metadata_paths.values()):
        raise ProfiledFaultRuntimeError(
            "exact build did not produce complete compiler and linker metadata"
        )
    record = {
        "schema_version": 1,
        "revision": revision,
        "repository": str(repository),
        "build_directory": str(build_directory),
        "cmake_cache_sha256": sha256_file(cache),
        "build_command": command,
        "build_metadata": {
            name: {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for name, path in metadata_paths.items()
        },
        "binaries": {
            name: {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for name, path in resolved_binaries.items()
        },
    }
    replace_json(provenance_path, record)
    return provenance_path


def verify_exact_build_provenance(
    *,
    repository: Path,
    build_directory: Path,
    provenance_path: Path,
    binaries: Mapping[str, Path],
) -> dict[str, object]:
    repository = repository.resolve()
    build_directory = build_directory.resolve()
    expected_binaries = exact_binary_paths(repository, build_directory)
    supplied_binaries = {name: path.resolve() for name, path in binaries.items()}
    if supplied_binaries != expected_binaries:
        raise ProfiledFaultRuntimeError(
            "binary paths do not match the exact-build provenance contract"
        )
    expected_provenance = build_directory / BUILD_PROVENANCE_FILENAME
    if provenance_path.resolve() != expected_provenance:
        raise ProfiledFaultRuntimeError("build provenance path is not exact")
    try:
        record = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfiledFaultRuntimeError("build provenance is unreadable") from exc
    if not isinstance(record, dict) or set(record) != {
        "schema_version",
        "revision",
        "repository",
        "build_directory",
        "cmake_cache_sha256",
        "build_command",
        "build_metadata",
        "binaries",
    }:
        raise ProfiledFaultRuntimeError("build provenance schema drifted")
    revision = verify_repository_state(repository)
    cache = _verify_cmake_cache(repository, build_directory)
    if (
        record.get("schema_version") != 1
        or record.get("revision") != revision
        or record.get("repository") != str(repository)
        or record.get("build_directory") != str(build_directory)
        or record.get("cmake_cache_sha256") != sha256_file(cache)
        or record.get("build_command") != exact_build_command(build_directory)
    ):
        raise ProfiledFaultRuntimeError(
            "build provenance is not bound to the exact current revision"
        )
    recorded_binaries = record.get("binaries")
    if not isinstance(recorded_binaries, dict) or set(recorded_binaries) != set(
        expected_binaries
    ):
        raise ProfiledFaultRuntimeError("build provenance binary membership drifted")
    for name, path in expected_binaries.items():
        exact = _assert_executable(path, name)
        item = recorded_binaries.get(name)
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size_bytes", "sha256"}
            or item.get("path") != str(exact)
            or item.get("size_bytes") != exact.stat().st_size
            or item.get("sha256") != sha256_file(exact)
        ):
            raise ProfiledFaultRuntimeError(
                f"build provenance binary bytes drifted: {name}"
            )
    recorded_metadata = record.get("build_metadata")
    metadata_paths = exact_build_metadata_paths(build_directory)
    if not isinstance(recorded_metadata, dict) or set(recorded_metadata) != set(
        metadata_paths
    ):
        raise ProfiledFaultRuntimeError("build metadata membership drifted")
    for name, path in metadata_paths.items():
        item = recorded_metadata.get(name)
        if (
            not path.is_file()
            or not isinstance(item, dict)
            or set(item) != {"path", "size_bytes", "sha256"}
            or item.get("path") != str(path)
            or item.get("size_bytes") != path.stat().st_size
            or item.get("sha256") != sha256_file(path)
        ):
            raise ProfiledFaultRuntimeError(
                f"build provenance compiler/linker metadata drifted: {name}"
            )
    return record


def _assert_executable(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ProfiledFaultRuntimeError(f"{label} is not executable: {resolved}")
    return resolved


def occupied_ports(ports: Iterable[int]) -> tuple[int, ...]:
    occupied: list[int] = []
    for port in ports:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            listener.bind(("127.0.0.1", port))
        except OSError:
            occupied.append(port)
        finally:
            listener.close()
    return tuple(occupied)


def listening_ports(ports: Iterable[int]) -> tuple[int, ...]:
    listening: list[int] = []
    for port in ports:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                listening.append(port)
        except OSError:
            pass
        finally:
            probe.close()
    return tuple(listening)


def epoch_zero_witness(
    profile: FrozenProfile, epoch_profile_digest_binary: Path
) -> dict[str, object]:
    binary = _assert_executable(epoch_profile_digest_binary, "epoch-profile-digest")
    result = subprocess.run(
        (
            str(binary),
            str(len(profile.replica_ids)),
            str(profile.fanout),
            str(profile.pipeline_depth),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ProfiledFaultRuntimeError(
            f"epoch-profile-digest failed with exit {result.returncode}"
        )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProfiledFaultRuntimeError(
            "epoch-profile-digest output is not JSON"
        ) from exc
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "replica_count",
        "fault_threshold",
        "quorum",
        "fanout",
        "pipeline_stretch",
        "membership",
        "epoch_zero",
    }:
        raise ProfiledFaultRuntimeError("epoch-profile-digest schema drifted")
    if (
        document.get("schema") != "kauri-adaptive-v2-epoch-profile-digest-v1"
        or document.get("replica_count") != len(profile.replica_ids)
        or document.get("fault_threshold") != profile.fault_threshold
        or document.get("quorum") != profile.quorum
        or document.get("fanout") != profile.fanout
        or document.get("pipeline_stretch") != profile.pipeline_depth
        or document.get("membership") != list(profile.replica_ids)
    ):
        raise ProfiledFaultRuntimeError(
            "epoch-profile-digest does not match the frozen profile"
        )
    epoch = document.get("epoch_zero")
    if not isinstance(epoch, dict) or set(epoch) != {
        "schema_version",
        "epoch_number",
        "previous_epoch_digest",
        "membership_digest",
        "activation_height",
        "generation_seed",
        "policy_version",
        "evidence_snapshot_id",
        "evidence_cutoff",
        "canonical_size_bytes",
        "epoch_digest",
        "tree_count",
        "trees",
    }:
        raise ProfiledFaultRuntimeError("epoch-zero witness schema drifted")
    trees = epoch.get("trees")
    if (
        epoch.get("schema_version") != 2
        or epoch.get("epoch_number") != 0
        or epoch.get("previous_epoch_digest") != "0" * 64
        or epoch.get("membership_digest")
        != "107f6e39481091529f50db1c6e32a72518cf50a971b074506bed095cb09769d9"
        or epoch.get("activation_height") != 0
        or epoch.get("generation_seed") != 0
        or epoch.get("policy_version") != "adaptive-v2-bootstrap"
        or epoch.get("evidence_snapshot_id") != "adaptive-v2-bootstrap-epoch-zero"
        or epoch.get("evidence_cutoff") != 0
        or epoch.get("canonical_size_bytes") != 2720
        or epoch.get("epoch_digest") != EXPECTED_EPOCH_ZERO_DIGEST
        or epoch.get("tree_count") != len(profile.replica_ids)
        or not isinstance(trees, list)
        or len(trees) != len(profile.replica_ids)
    ):
        raise ProfiledFaultRuntimeError(
            "epoch-zero witness does not match the pinned N31 definition"
        )
    count = len(profile.replica_ids)
    for root, tree in enumerate(trees):
        expected_members = [
            profile.replica_ids[(root + offset) % count] for offset in range(count)
        ]
        if not isinstance(tree, dict) or tree != {
            "tree_id": root,
            "fanout": profile.fanout,
            "pipeline_stretch": profile.pipeline_depth,
            "members_breadth_first": expected_members,
            "wait_exempt_leaves": [],
        }:
            raise ProfiledFaultRuntimeError(
                f"epoch-zero witness tree {root} is not exact"
            )
    return document


def preflight(
    *,
    profile_path: Path,
    repository: Path,
    app_binary: Path,
    manager_binary: Path,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    epoch_profile_digest_binary: Path,
    build_directory: Path,
    build_provenance_path: Path,
) -> dict[str, object]:
    profile = load_frozen_profile(profile_path.resolve())
    require_shipped_profile(profile)
    repository = repository.resolve()
    binaries = {
        "app": _assert_executable(app_binary, "hotstuff-app"),
        "manager": _assert_executable(manager_binary, "adaptation-manager"),
        "keygen": _assert_executable(keygen_binary, "hotstuff-keygen"),
        "tls_keygen": _assert_executable(tls_keygen_binary, "hotstuff-tls-keygen"),
        "epoch_profile_digest": _assert_executable(
            epoch_profile_digest_binary, "epoch-profile-digest"
        ),
    }
    build_provenance = verify_exact_build_provenance(
        repository=repository,
        build_directory=build_directory,
        provenance_path=build_provenance_path,
        binaries=binaries,
    )
    revision = str(build_provenance["revision"])
    epoch_witness = epoch_zero_witness(profile, binaries["epoch_profile_digest"])
    first_clock = monotonic_raw_ns()
    second_clock = monotonic_raw_ns()
    if second_clock < first_clock:
        raise ProfiledFaultRuntimeError("CLOCK_MONOTONIC_RAW regressed")
    ports = required_ports(profile)
    occupied = occupied_ports(ports)
    if occupied:
        raise ProfiledFaultRuntimeError(
            f"required ports are already in use: {list(occupied)}"
        )
    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft_limit < 1024:
        raise ProfiledFaultRuntimeError("FD soft limit must be at least 1024")
    free_bytes = shutil.disk_usage(repository).free
    if free_bytes < 1024**3:
        raise ProfiledFaultRuntimeError("free disk must be at least 1 GiB")
    return {
        "schema_version": 1,
        "verdict": "PASS",
        "profile_id": profile.profile_id,
        "profile_sha256": profile.profile_sha256,
        "revision": revision,
        "required_port_count": len(ports),
        "fd_soft_limit": soft_limit,
        "free_disk_bytes": free_bytes,
        "clock": "CLOCK_MONOTONIC_RAW",
        "build_provenance": build_provenance,
        "epoch_zero_witness": epoch_witness,
        "executables": {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in binaries.items()
        },
    }


def _parse_identity_output(
    text: str,
    *,
    expected_count: int,
    expected_fields: frozenset[str],
    label: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields: dict[str, str] = {}
        for token in line.split():
            if ":" not in token:
                raise ProfiledFaultRuntimeError(
                    f"{label} line {line_number} is malformed"
                )
            key, value = token.split(":", 1)
            if not key or not value or key in fields:
                raise ProfiledFaultRuntimeError(
                    f"{label} line {line_number} is malformed"
                )
            fields[key] = value
        if set(fields) != expected_fields:
            raise ProfiledFaultRuntimeError(
                f"{label} line {line_number} has schema drift"
            )
        rows.append(fields)
    if len(rows) != expected_count:
        raise ProfiledFaultRuntimeError(
            f"{label} produced {len(rows)} identities; expected " f"{expected_count}"
        )
    for field in expected_fields:
        if len({row[field] for row in rows}) != len(rows):
            raise ProfiledFaultRuntimeError(
                f"{label} produced duplicate {field} values"
            )
    return rows


def generate_identities(
    profile: FrozenProfile,
    *,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    config_directory: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, str]]:
    commands = identity_generation_commands(
        profile,
        keygen_binary=keygen_binary,
        tls_keygen_binary=tls_keygen_binary,
    )
    outputs: dict[str, str] = {}
    for label, command in commands.items():
        result = subprocess.run(
            command,
            cwd=config_directory,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ProfiledFaultRuntimeError(
                f"{label} identity generation failed with exit " f"{result.returncode}"
            )
        outputs[label] = result.stdout
        write_exclusive(
            config_directory / f"{label}-identities.txt",
            result.stdout.encode(),
        )
    count = len(profile.replica_ids)
    return (
        _parse_identity_output(
            outputs["bls"],
            expected_count=count,
            expected_fields=frozenset({"pub", "sec"}),
            label="BLS keygen",
        ),
        _parse_identity_output(
            outputs["tls"],
            expected_count=count + 1,
            expected_fields=frozenset({"crt", "sec", "cid"}),
            label="TLS keygen",
        ),
        _parse_identity_output(
            outputs["issuer"],
            expected_count=1,
            expected_fields=frozenset({"pub", "sec"}),
            label="issuer keygen",
        )[0],
    )


def transition_request() -> dict[str, object]:
    return {
        "policy_intent": "fault_containment",
        "evidence_window_rule": ("fresh_exact_predecessor_after_common_commit"),
        "transition_artifact_id": TRANSITION_ARTIFACT_ID,
        "bundle_path": f"transitions/{TRANSITION_ARTIFACT_ID}/successor.bundle",
        "evidence_snapshot_path": (
            f"transitions/{TRANSITION_ARTIFACT_ID}/evidence-snapshot.json"
        ),
        "predecessor_epoch_number": 0,
        "successor_epoch_number": 1,
        "minimum_predecessor_residency_ms": (MINIMUM_PREDECESSOR_RESIDENCY_MS),
        "policy_parameters": {
            "containment_baseline_roots": [
                {"tree_id": replica, "replica_id": replica} for replica in range(21)
            ]
        },
    }


def effective_runtime(profile: FrozenProfile) -> dict[str, object]:
    return {
        "schema_version": 1,
        "protocol_mode": "adaptive-v2",
        "replica_ids": list(profile.replica_ids),
        "fault_threshold": profile.fault_threshold,
        "quorum": profile.quorum,
        "fanout": profile.fanout,
        "pipeline_depth": profile.pipeline_depth,
        "replica_network_workers": REPLICA_NETWORK_WORKERS,
        "block_size": profile.block_size,
        "tree_switch_period_blocks": profile.tree_switch_period_blocks,
        "aggregation_timeout_ms": int(profile.aggregation_timeout_s * 1000),
        "leader_progress_timeout_ms": int(profile.leader_progress_timeout_s * 1000),
        "leader_activation_grace_ms": int(profile.leader_activation_grace_s * 1000),
        "activation_delay_blocks": profile.activation_delay_blocks,
        "authoritative_observer": profile.authoritative_observer,
        "fixed_postfault_witnesses": list(postfault_witnesses(profile)),
        "transition_request": transition_request(),
    }


def main_config_payload(
    profile: FrozenProfile,
    *,
    bls: Sequence[Mapping[str, str]],
    tls: Sequence[Mapping[str, str]],
    issuer: Mapping[str, str],
) -> bytes:
    count = len(profile.replica_ids)
    if len(bls) != count or len(tls) != count + 1:
        raise ProfiledFaultRuntimeError("identity cardinality drift")
    lines = [
        f"block-size = {profile.block_size}",
        "nworker = 2",
        f"repnworker = {REPLICA_NETWORK_WORKERS}",
        f"stat-period = {profile.hard_timeout_s + 60.0}",
        "pace-maker = dummy",
        "proposer = 0",
        f"fan-out = {profile.fanout}",
        "piped_latency = 1",
        f"async_blocks = {profile.pipeline_depth}",
        "base-timeout = 2.0",
        "prop-delay = 0.1",
        f"aggregation-timeout = {profile.aggregation_timeout_s}",
        f"leader-progress-timeout = {profile.leader_progress_timeout_s}",
        f"leader-activation-grace = {profile.leader_activation_grace_s}",
        "client-ip = 127.0.0.1",
        "tree-generation = default",
        f"tree-switch-period = {profile.tree_switch_period_blocks}",
        "epoch-protocol-mode = adaptive_v2",
        f"epoch-change-issuer-id = {ISSUER_ID}",
        f"epoch-change-issuer-public-key = {issuer['pub']}",
        (
            "epoch-change-minimum-activation-delay = "
            f"{profile.activation_delay_blocks}"
        ),
        (
            "epoch-change-maximum-activation-delay = "
            f"{profile.activation_delay_blocks}"
        ),
        f"epoch-change-maximum-block-extra-bytes = {MAX_COMMAND_BYTES}",
        f"epoch-change-maximum-ancestry-blocks = {MAX_ANCESTRY_BLOCKS}",
        f"epoch-manager-address = 127.0.0.1:{profile.manager_port}",
        f"epoch-manager-tls-cert = {tls[count]['crt']}",
        f"max-rep-msg = {MAX_REPLICA_MESSAGE_BYTES}",
    ]
    for replica in profile.replica_ids:
        lines.append(
            "replica = "
            f"127.0.0.1:{profile.peer_base + replica};"
            f"{profile.client_base + replica}, "
            f"{bls[replica]['pub']}, {tls[replica]['cid']}"
        )
    return ("\n".join(lines) + "\n").encode()


def replica_config_payload(
    profile: FrozenProfile,
    replica_id: int,
    *,
    bls: Sequence[Mapping[str, str]],
    tls: Sequence[Mapping[str, str]],
    raw_directory: Path,
    run_id: str,
    source_instances: Mapping[str, str],
) -> bytes:
    source_id = f"replica-{replica_id}"
    observer_id = f"replica-{profile.authoritative_observer}"
    return (
        f"privkey = {bls[replica_id]['sec']}\n"
        f"tls-privkey = {tls[replica_id]['sec']}\n"
        f"tls-cert = {tls[replica_id]['crt']}\n"
        f"idx = {replica_id}\n"
        f"structured-event-run-id = {run_id}\n"
        f"structured-event-source-instance = {source_instances[source_id]}\n"
        f"structured-event-output = {raw_directory / f'{source_id}.jsonl'}\n"
        f"structured-event-commit-observer-id = {observer_id}\n"
        "structured-event-commit-observer-instance = "
        f"{source_instances[observer_id]}\n"
    ).encode()


def manager_argv(
    profile: FrozenProfile,
    *,
    manager_binary: Path,
    tls: Sequence[Mapping[str, str]],
    issuer: Mapping[str, str],
    run_directory: Path,
    run_id: str,
    source_instance: str,
) -> tuple[str, ...]:
    count = len(profile.replica_ids)
    request = transition_request()
    bundle_path = run_directory / str(request["bundle_path"])
    bundle_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    command: list[str] = [
        str(manager_binary),
        "--listen",
        f"127.0.0.1:{profile.manager_port}",
        "--tls-privkey",
        tls[count]["sec"],
        "--tls-cert",
        tls[count]["crt"],
        "--issuer-id",
        str(ISSUER_ID),
        "--issuer-private-key",
        issuer["sec"],
        "--activation-delay-blocks",
        str(profile.activation_delay_blocks),
        "--convergence-deadline-seconds",
        "120",
        "--tree-fanout",
        str(profile.fanout),
        "--pipeline-stretch",
        str(profile.pipeline_depth),
        "--transition-request",
        json.dumps(request, sort_keys=True, separators=(",", ":")),
        "--bundle-output",
        str(bundle_path),
        "--structured-event-run-id",
        run_id,
        "--structured-event-source-instance",
        source_instance,
        "--structured-event-output",
        str(run_directory / "raw" / "adaptive-manager.jsonl"),
    ]
    for replica in profile.replica_ids:
        command.extend(
            (
                "--replica",
                f"{replica},127.0.0.1:{profile.peer_base + replica},"
                f"{tls[replica]['crt']}",
            )
        )
    return tuple(command)


def normalized_manager_argv(command: Sequence[str]) -> list[str]:
    normalized = list(command)
    for option, replacement in (
        ("--tls-privkey", "<redacted>"),
        ("--tls-cert", "<fingerprinted>"),
        ("--issuer-private-key", "<redacted>"),
    ):
        if normalized.count(option) != 1:
            raise ProfiledFaultRuntimeError(
                f"manager argv has invalid {option} cardinality"
            )
        position = normalized.index(option)
        normalized[position + 1] = replacement
    for index, value in enumerate(tuple(normalized)):
        if value != "--replica":
            continue
        fields = normalized[index + 1].split(",")
        if len(fields) != 3:
            raise ProfiledFaultRuntimeError("manager endpoint is malformed")
        fields[2] = "<fingerprinted>"
        normalized[index + 1] = ",".join(fields)
    return normalized


def apply_replica_overlays(
    profile: FrozenProfile,
    commands: Sequence[Sequence[str]],
    replica_overlays: Mapping[int, Sequence[str]],
) -> tuple[tuple[str, ...], ...]:
    """Append exact actor-local arguments without changing base launch order."""

    if len(commands) != len(profile.replica_ids):
        raise ProfiledFaultRuntimeError(
            "replica overlays require one base command per replica"
        )
    if set(replica_overlays) != set(profile.replica_ids):
        raise ProfiledFaultRuntimeError(
            "replica overlays require the exact frozen replica membership"
        )
    result: list[tuple[str, ...]] = []
    for position, replica_id in enumerate(profile.replica_ids):
        command = commands[position]
        overlay = replica_overlays[replica_id]
        if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
            raise ProfiledFaultRuntimeError(
                f"base replica command is malformed for replica-{replica_id}"
            )
        if isinstance(overlay, (str, bytes)) or not isinstance(overlay, Sequence):
            raise ProfiledFaultRuntimeError(
                f"replica overlay is malformed for replica-{replica_id}"
            )
        if any(not isinstance(argument, str) or not argument for argument in overlay):
            raise ProfiledFaultRuntimeError(
                f"replica overlay contains an invalid argument for replica-{replica_id}"
            )
        result.append((*tuple(command), *tuple(overlay)))
    return tuple(result)


def write_runtime_inputs(
    profile: FrozenProfile,
    *,
    run_directory: Path,
    app_binary: Path,
    manager_binary: Path,
    bls: Sequence[Mapping[str, str]],
    tls: Sequence[Mapping[str, str]],
    issuer: Mapping[str, str],
    run_id: str,
    source_instances: Mapping[str, str],
    replica_overlays: Mapping[int, Sequence[str]] | None = None,
    include_issuer_identity_artifact: bool = True,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...], list[dict[str, object]]]:
    config_directory = run_directory / "config"
    main_config = config_directory / "main.conf"
    write_exclusive(
        main_config,
        main_config_payload(profile, bls=bls, tls=tls, issuer=issuer),
    )
    replica_configs: list[Path] = []
    for replica in profile.replica_ids:
        path = config_directory / f"replica-{replica}.conf"
        write_exclusive(
            path,
            replica_config_payload(
                profile,
                replica,
                bls=bls,
                tls=tls,
                raw_directory=run_directory / "raw",
                run_id=run_id,
                source_instances=source_instances,
            ),
        )
        replica_configs.append(path)
    epoch_path = run_directory / "runtime" / "initial-epoch.json"
    runtime_path = run_directory / "runtime" / "effective-runtime.json"
    request_path = run_directory / "runtime" / "transition-request.json"
    write_json_exclusive(epoch_path, initial_epoch_input(profile))
    write_json_exclusive(runtime_path, effective_runtime(profile))
    write_json_exclusive(request_path, transition_request())
    replica_commands = replica_argvs(
        profile,
        app_binary=app_binary,
        config_directory=config_directory,
    )
    if replica_overlays is not None:
        replica_commands = apply_replica_overlays(
            profile,
            replica_commands,
            replica_overlays,
        )
    manager_command = manager_argv(
        profile,
        manager_binary=manager_binary,
        tls=tls,
        issuer=issuer,
        run_directory=run_directory,
        run_id=run_id,
        source_instance=source_instances[MANAGER_SOURCE_ID],
    )
    launch_path = run_directory / "runtime" / "launch-arguments.json"
    write_json_exclusive(
        launch_path,
        {
            "schema_version": 1,
            "manager": normalized_manager_argv(manager_command),
            "replicas": [list(command) for command in replica_commands],
        },
    )
    artifact_paths = [
        ("bls_identity_input", None, config_directory / "bls-identities.txt"),
        ("tls_identity_input", None, config_directory / "tls-identities.txt"),
        ("main_config", None, main_config),
        *[
            ("replica_config", replica, path)
            for replica, path in zip(profile.replica_ids, replica_configs)
        ],
        ("initial_epoch", None, epoch_path),
        ("effective_runtime", None, runtime_path),
        ("transition_request", None, request_path),
        ("launch_arguments", None, launch_path),
    ]
    if include_issuer_identity_artifact:
        artifact_paths.insert(
            2,
            (
                "issuer_identity_input",
                None,
                config_directory / "issuer-identities.txt",
            ),
        )
    artifacts = [
        {
            "kind": kind,
            "replica_id": replica,
            "path": str(path.relative_to(run_directory)),
            "sha256": sha256_file(path),
        }
        for kind, replica, path in artifact_paths
    ]
    return manager_command, replica_commands, artifacts


def read_jsonl(path: Path, *, allow_partial: bool = False) -> list[dict[str, Any]]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return []
    if allow_partial and payload and not payload.endswith(b"\n"):
        newline = payload.rfind(b"\n")
        payload = payload[: newline + 1] if newline >= 0 else b""
    elif payload and not payload.endswith(b"\n"):
        raise ProfiledFaultRuntimeError(
            f"structured stream has an incomplete final line: {path}"
        )
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(payload.splitlines(), 1):
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProfiledFaultRuntimeError(
                f"malformed JSONL at {path}:{line_number}"
            ) from exc
        if not isinstance(value, dict):
            raise ProfiledFaultRuntimeError(f"non-object JSONL at {path}:{line_number}")
        events.append(value)
    return events


def event_streams(
    profile: FrozenProfile,
    run_directory: Path,
    *,
    include_manager: bool = False,
    allow_partial: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    streams = {
        f"replica-{replica}": read_jsonl(
            run_directory / "raw" / f"replica-{replica}.jsonl",
            allow_partial=allow_partial,
        )
        for replica in profile.replica_ids
    }
    if include_manager:
        streams[MANAGER_SOURCE_ID] = read_jsonl(
            run_directory / "raw" / "adaptive-manager.jsonl",
            allow_partial=allow_partial,
        )
    return streams


def _timestamp(event: Mapping[str, Any]) -> int:
    value = event.get("source_monotonic_ns")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProfiledFaultRuntimeError("structured event timestamp is invalid")
    return value


def _sequence(event: Mapping[str, Any]) -> int:
    value = event.get("source_sequence")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProfiledFaultRuntimeError("structured event sequence is invalid")
    return value


def _commit_key(event: Mapping[str, Any]) -> tuple[object, ...]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ProfiledFaultRuntimeError("commit event lacks a payload object")
    fields = (
        payload.get("block_height"),
        payload.get("block_hash"),
        payload.get("parent_hash"),
        payload.get("transaction_count"),
        payload.get("commit_batch_index"),
    )
    height, block_hash, parent_hash, transaction_count, batch_index = fields
    if (
        isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
        or not isinstance(block_hash, str)
        or len(block_hash) != 64
        or not isinstance(parent_hash, str)
        or len(parent_hash) != 64
        or isinstance(transaction_count, bool)
        or not isinstance(transaction_count, int)
        or transaction_count < 0
        or isinstance(batch_index, bool)
        or not isinstance(batch_index, int)
        or batch_index < 0
    ):
        raise ProfiledFaultRuntimeError("commit identity is malformed")
    return fields


def _shared_commit_key(event: Mapping[str, Any]) -> tuple[object, ...]:
    return _commit_key(event)[:4]


def find_common_commit(
    profile: FrozenProfile,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    witnesses: Sequence[int],
    before_ns: int | None = None,
    after_ns: int | None = None,
) -> dict[str, object] | None:
    observer = f"replica-{profile.authoritative_observer}"
    observations: dict[int, dict[tuple[object, ...], int]] = {}
    for replica in witnesses:
        values: dict[tuple[object, ...], int] = {}
        for event in streams[f"replica-{replica}"]:
            if event.get("event_type") != "block.commit_observed":
                continue
            key = _shared_commit_key(event)
            timestamp = _timestamp(event)
            previous = values.get(key)
            values[key] = timestamp if previous is None else min(previous, timestamp)
        observations[replica] = values
    for event in streams[observer]:
        if event.get("event_type") != "block.committed":
            continue
        observer_key = _commit_key(event)
        key = observer_key[:4]
        timestamp = _timestamp(event)
        if before_ns is not None and timestamp >= before_ns:
            continue
        if after_ns is not None and timestamp < after_ns:
            continue
        witness_times = [observations[replica].get(key) for replica in witnesses]
        if not all(value is not None for value in witness_times):
            continue
        exact_times = [int(value) for value in witness_times if value is not None]
        if before_ns is not None and any(value >= before_ns for value in exact_times):
            continue
        if after_ns is not None and any(value < after_ns for value in exact_times):
            continue
        return {
            "block_height": key[0],
            "block_hash": key[1],
            "parent_hash": key[2],
            "transaction_count": key[3],
            "observer_commit_batch_index": observer_key[4],
            "observer_monotonic_ns": timestamp,
            "common_monotonic_ns": max(timestamp, *exact_times),
            "witnesses": list(witnesses),
        }
    return None


def throughput_rows(
    profile: FrozenProfile,
    observer_events: Sequence[Mapping[str, Any]],
    *,
    phase: str,
    start_ns: int,
    bucket_count: int,
) -> list[dict[str, object]]:
    width_ns = profile.bucket_width_s * 1_000_000_000
    end_ns = start_ns + bucket_count * width_ns
    seen: set[tuple[object, ...]] = set()
    transactions = [0 for _ in range(bucket_count)]
    commits = [0 for _ in range(bucket_count)]
    for event in observer_events:
        if event.get("event_type") != "block.committed":
            continue
        timestamp = _timestamp(event)
        if not start_ns <= timestamp < end_ns:
            continue
        key = _commit_key(event)
        identity = key[:2]
        if identity in seen:
            raise ProfiledFaultRuntimeError(
                "duplicate authoritative commit in throughput window"
            )
        seen.add(identity)
        index = (timestamp - start_ns) // width_ns
        transactions[index] += int(key[3])
        commits[index] += 1
    rows = [
        {
            "phase": phase,
            "bucket_index": index,
            "start_monotonic_ns": start_ns + index * width_ns,
            "end_monotonic_ns": start_ns + (index + 1) * width_ns,
            "transaction_count": transactions[index],
            "unique_commit_count": commits[index],
            "tps": transactions[index] / profile.bucket_width_s,
        }
        for index in range(bucket_count)
    ]
    return rows


def require_positive_buckets(
    rows: Sequence[Mapping[str, object]], *, phase: str
) -> None:
    if any(row.get("transaction_count", 0) <= 0 for row in rows):
        raise ProfiledFaultRuntimeError(
            f"{phase} does not have positive throughput in every complete bucket"
        )


def recovery_gate_evidence(
    profile: FrozenProfile,
    baseline_rows: Sequence[Mapping[str, object]],
    postfault_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Evaluate frozen recovery requirements from both complete windows."""
    if (
        len(baseline_rows) != profile.baseline_bucket_count
        or len(postfault_rows) != profile.post_bucket_count
    ):
        raise ProfiledFaultRuntimeError(
            "recovery gate requires both exact complete measurement windows"
        )
    baseline_mean = statistics.fmean(float(row["tps"]) for row in baseline_rows)
    postfault_mean = statistics.fmean(float(row["tps"]) for row in postfault_rows)
    if not math.isfinite(baseline_mean) or baseline_mean <= 0:
        raise ProfiledFaultRuntimeError(
            "recovery gate requires positive finite baseline mean throughput"
        )
    if not math.isfinite(postfault_mean) or postfault_mean < 0:
        raise ProfiledFaultRuntimeError(
            "recovery gate requires non-negative finite postfault mean throughput"
        )
    retention = postfault_mean / baseline_mean
    positive_postfault_buckets = sum(
        1 for row in postfault_rows if float(row["tps"]) > 0
    )
    violations: list[str] = []
    if positive_postfault_buckets < profile.minimum_positive_postfault_buckets:
        violations.append("minimum_positive_postfault_buckets")
    if retention < profile.minimum_mean_throughput_retention:
        violations.append("minimum_mean_throughput_retention")
    return {
        "requirements": {
            "minimum_positive_postfault_buckets": (
                profile.minimum_positive_postfault_buckets
            ),
            "minimum_mean_throughput_retention": (
                profile.minimum_mean_throughput_retention
            ),
        },
        "observations": {
            "positive_postfault_buckets": positive_postfault_buckets,
            "baseline_mean_tps": baseline_mean,
            "postfault_mean_tps": postfault_mean,
            "mean_throughput_retention": retention,
        },
        "passed": not violations,
        "violations": violations,
    }


def enforce_recovery_gate(
    profile: FrozenProfile,
    baseline_rows: Sequence[Mapping[str, object]],
    postfault_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    evidence = recovery_gate_evidence(profile, baseline_rows, postfault_rows)
    violations = evidence["violations"]
    if not violations:
        return evidence
    observations = evidence["observations"]
    requirements = evidence["requirements"]
    assert isinstance(observations, Mapping)
    assert isinstance(requirements, Mapping)
    details: list[str] = []
    if "minimum_positive_postfault_buckets" in violations:
        details.append(
            "positive postfault buckets "
            f"{observations['positive_postfault_buckets']} are below frozen "
            f"minimum {requirements['minimum_positive_postfault_buckets']}"
        )
    if "minimum_mean_throughput_retention" in violations:
        details.append(
            "mean throughput retention "
            f"{observations['mean_throughput_retention']} is below frozen "
            f"minimum {requirements['minimum_mean_throughput_retention']}"
        )
    raise RecoveryGateFailure("; ".join(details), evidence)


def evaluate_complete_postfault_window(
    profile: FrozenProfile,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    start_ns: int,
    end_ns: int,
    observed_ns: int,
) -> dict[str, Any] | None:
    """Qualify only the fixed post-fault window, including measured downtime."""
    if observed_ns < end_ns:
        return None
    rows = throughput_rows(
        profile,
        streams[f"replica-{profile.authoritative_observer}"],
        phase="postfault",
        start_ns=start_ns,
        bucket_count=profile.post_bucket_count,
    )
    common = find_common_commit(
        profile,
        streams,
        witnesses=postfault_witnesses(profile),
        after_ns=start_ns,
        before_ns=end_ns,
    )
    if common is None:
        raise IncompleteProfiledFaultRun(
            "fixed Q21 common commit is absent from the complete postfault window"
        )
    return {"rows": rows, "common_commit": common}


def write_throughput_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    if not rows:
        raise ProfiledFaultRuntimeError("throughput CSV requires rows")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


class ConfigurationBoundaryPoller:
    """Tightly tail all replicas for one common current fault-tree view."""

    def __init__(
        self,
        profile: FrozenProfile,
        run_directory: Path,
        *,
        watermarks: Mapping[str, int],
        offsets: Mapping[str, int],
        target_tree_id: int | None = None,
    ) -> None:
        self.profile = profile
        self.paths = {
            f"replica-{replica}": (run_directory / "raw" / f"replica-{replica}.jsonl")
            for replica in profile.replica_ids
        }
        if set(watermarks) != set(self.paths) or set(offsets) != set(self.paths):
            raise ProfiledFaultRuntimeError(
                "configuration poller requires exact source watermarks"
            )
        if target_tree_id is not None and target_tree_id not in profile.replica_ids:
            raise ProfiledFaultRuntimeError(
                "configuration poller target tree is outside membership"
            )
        self.target_tree_id = (
            profile.fault.tree_id if target_tree_id is None else target_tree_id
        )
        self.watermarks = dict(watermarks)
        self.offsets = dict(offsets)
        self.pending = {source: b"" for source in self.paths}
        self.events = {source: [] for source in self.paths}

    def _consume(self) -> None:
        for source, path in self.paths.items():
            try:
                with path.open("rb") as stream:
                    stream.seek(self.offsets[source])
                    chunk = stream.read()
            except FileNotFoundError:
                continue
            self.offsets[source] += len(chunk)
            payload = self.pending[source] + chunk
            newline = payload.rfind(b"\n")
            if newline < 0:
                if len(payload) > 128 * 1024:
                    raise ProfiledFaultRuntimeError(
                        f"unterminated structured event in {path}"
                    )
                self.pending[source] = payload
                continue
            complete = payload[: newline + 1]
            self.pending[source] = payload[newline + 1 :]
            for line in complete.splitlines():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ProfiledFaultRuntimeError(
                        f"non-object structured event in {path}"
                    )
                if (
                    value.get("event_type") == "adaptive.configuration_active"
                    and _sequence(value) > self.watermarks[source]
                ):
                    self.events[source].append(value)

    def poll(self) -> dict[str, object] | None:
        self._consume()
        target_tree = self.target_tree_id
        candidates: dict[str, list[dict[str, Any]]] = {}
        for replica in self.profile.replica_ids:
            source = f"replica-{replica}"
            values: list[dict[str, Any]] = []
            for event in self.events[source]:
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    raise ProfiledFaultRuntimeError(
                        "configuration-active payload is malformed"
                    )
                if (
                    payload.get("epoch_number") == 0
                    and payload.get("tree_id") == target_tree
                    and payload.get("global_quorum") == self.profile.quorum
                    and payload.get("observer_replica") == replica
                ):
                    values.append(event)
            if not values:
                return None
            candidates[source] = values
        skew_ns = int(self.profile.aggregation_timeout_s * 1_000_000_000)
        observer_source = f"replica-{self.profile.authoritative_observer}"
        for anchor in reversed(candidates[observer_source]):
            anchor_payload = anchor["payload"]
            digest = anchor_payload.get("epoch_digest")
            if not isinstance(digest, str) or len(digest) != 64:
                raise ProfiledFaultRuntimeError(
                    "configuration-active epoch digest is malformed"
                )
            if digest != EXPECTED_EPOCH_ZERO_DIGEST:
                raise ProfiledFaultRuntimeError(
                    "observed epoch-zero digest does not bind the exact N31 topology"
                )
            selected: list[dict[str, Any]] = []
            anchor_ns = _timestamp(anchor)
            for replica in self.profile.replica_ids:
                source = f"replica-{replica}"
                matches = [
                    event
                    for event in candidates[source]
                    if event["payload"].get("epoch_digest") == digest
                    and abs(_timestamp(event) - anchor_ns) <= skew_ns
                ]
                if not matches:
                    break
                selected.append(
                    min(matches, key=lambda event: abs(_timestamp(event) - anchor_ns))
                )
            if len(selected) != len(self.profile.replica_ids):
                continue
            timestamps = [_timestamp(event) for event in selected]
            if max(timestamps) - min(timestamps) > skew_ns:
                continue
            # Close the normal read/poll race before handing the boundary out,
            # then require the selected fault-tree event to remain the latest
            # exact activation in every source.  Reading from offset zero lets
            # a long-lived stable tree qualify without accepting a stale view.
            self._consume()
            if any(
                _sequence(event)
                != max(
                    (_sequence(candidate) for candidate in self.events[source]),
                    default=-1,
                )
                for source, event in zip(self.paths, selected)
            ):
                continue
            return {
                "epoch_number": 0,
                "tree_id": target_tree,
                "root_replica": target_tree,
                "epoch_digest": digest,
                "global_quorum": self.profile.quorum,
                "members_breadth_first": list(
                    self.profile.epoch0_members_breadth_first
                    if target_tree == self.profile.fault.tree_id
                    else (
                        self.profile.replica_ids[target_tree:]
                        + self.profile.replica_ids[:target_tree]
                    )
                ),
                "replica_evidence": [
                    {
                        "source_id": f"replica-{replica}",
                        "source_sequence": _sequence(event),
                        "source_monotonic_ns": _timestamp(event),
                    }
                    for replica, event in zip(self.profile.replica_ids, selected)
                ],
            }
        return None


def event_tail_snapshot(
    profile: FrozenProfile, run_directory: Path
) -> tuple[dict[str, int], dict[str, int]]:
    watermarks: dict[str, int] = {}
    offsets: dict[str, int] = {}
    for replica in profile.replica_ids:
        source = f"replica-{replica}"
        path = run_directory / "raw" / f"{source}.jsonl"
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            payload = b""
        newline = payload.rfind(b"\n")
        complete = payload[: newline + 1] if newline >= 0 else b""
        events: list[dict[str, Any]] = []
        for line in complete.splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ProfiledFaultRuntimeError(
                    f"non-object structured event in {path}"
                )
            events.append(value)
        watermarks[source] = max((_sequence(event) for event in events), default=-1)
        offsets[source] = len(complete)
    return watermarks, offsets


def assert_boundary_race_free(
    profile: FrozenProfile,
    boundary: Mapping[str, object],
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    crash_request_ns: int,
) -> None:
    if set(boundary) != {
        "epoch_number",
        "tree_id",
        "root_replica",
        "epoch_digest",
        "global_quorum",
        "members_breadth_first",
        "replica_evidence",
    }:
        raise ProfiledFaultRuntimeError("crash boundary schema drifted")
    digest = _digest(boundary.get("epoch_digest"), "crash boundary epoch digest")
    if (
        digest != EXPECTED_EPOCH_ZERO_DIGEST
        or boundary.get("epoch_number") != 0
        or boundary.get("tree_id") != profile.fault.tree_id
        or boundary.get("root_replica") != profile.fault.tree_id
        or boundary.get("global_quorum") != profile.quorum
        or boundary.get("members_breadth_first")
        != list(profile.epoch0_members_breadth_first)
    ):
        raise ProfiledFaultRuntimeError(
            "crash boundary is not the exact canonical epoch-zero fault tree"
        )

    evidence = boundary.get("replica_evidence")
    expected_sources = [f"replica-{replica}" for replica in profile.replica_ids]
    if (
        not isinstance(evidence, list)
        or len(evidence) != len(expected_sources)
        or [
            reference.get("source_id") if isinstance(reference, Mapping) else None
            for reference in evidence
        ]
        != expected_sources
        or set(streams) != set(expected_sources)
    ):
        raise ProfiledFaultRuntimeError(
            "crash boundary evidence must contain the exact replica membership"
        )

    for replica, reference in zip(profile.replica_ids, evidence):
        if not isinstance(reference, Mapping) or set(reference) != {
            "source_id",
            "source_sequence",
            "source_monotonic_ns",
        }:
            raise ProfiledFaultRuntimeError("crash boundary reference schema drifted")
        source = reference.get("source_id")
        sequence = reference.get("source_sequence")
        timestamp = reference.get("source_monotonic_ns")
        if (
            not isinstance(source, str)
            or source not in streams
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
        ):
            raise ProfiledFaultRuntimeError("crash boundary reference drifted")
        if timestamp >= crash_request_ns:
            raise ProfiledFaultRuntimeError(
                "crash request does not follow the exact fault-tree boundary"
            )

        referenced = [
            event
            for event in streams[source]
            if _sequence(event) == sequence and _timestamp(event) == timestamp
        ]
        if len(referenced) != 1:
            raise ProfiledFaultRuntimeError(
                "crash boundary reference does not resolve to one exact event"
            )
        payload = referenced[0].get("payload")
        if (
            referenced[0].get("event_type") != "adaptive.configuration_active"
            or not isinstance(payload, Mapping)
            or payload.get("epoch_number") != 0
            or payload.get("tree_id") != profile.fault.tree_id
            or payload.get("epoch_digest") != digest
            or payload.get("observer_replica") != replica
            or payload.get("global_quorum") != profile.quorum
        ):
            raise ProfiledFaultRuntimeError(
                "crash boundary reference is not the exact canonical fault-tree event"
            )

        for event in streams[source]:
            if (
                event.get("event_type") == "adaptive.configuration_active"
                and _sequence(event) > sequence
                and _timestamp(event) <= crash_request_ns
            ):
                raise ProfiledFaultRuntimeError(
                    "active configuration changed before the SIGKILL request"
                )


def assert_no_successor_activity(
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    run_directory: Path,
    cleanup_started_ns: int | None = None,
    post_end_ns: int | None = None,
) -> None:
    cleanup_terminal_count = 0
    for source, events in streams.items():
        for event in events:
            event_type = event.get("event_type")
            manager_adaptive = (
                source == MANAGER_SOURCE_ID
                and isinstance(event_type, str)
                and (
                    event_type.startswith("adaptive_v2.")
                    or event_type.startswith("adaptive_v2_")
                )
            )
            cleanup_terminal = False
            if manager_adaptive and event_type == "adaptive_v2_session_terminal":
                payload = event.get("payload")
                cleanup_terminal = (
                    cleanup_started_ns is not None
                    and post_end_ns is not None
                    and cleanup_started_ns >= post_end_ns
                    and _timestamp(event) >= cleanup_started_ns
                    and isinstance(payload, Mapping)
                    and set(payload) == MANAGER_SESSION_TERMINAL_FIELDS
                    and payload.get("cycle_ordinal") == 0
                    and payload.get("policy_intent") == "fault_containment"
                    and payload.get("outcome") == "failed"
                    and payload.get("reason") == "caller_failed"
                    and payload.get("transition_artifact_id") == TRANSITION_ARTIFACT_ID
                    and payload.get("predecessor_epoch_number") == 0
                    and payload.get("predecessor_epoch_digest")
                    == EXPECTED_EPOCH_ZERO_DIGEST
                    and payload.get("successor_epoch_number") is None
                    and payload.get("successor_epoch_digest") is None
                    and payload.get("command_payload_digest") is None
                    and payload.get("winning_activation") is None
                )
            if event_type in FORBIDDEN_TRANSITION_EVENTS or (
                manager_adaptive and not cleanup_terminal
            ):
                raise ProfiledFaultRuntimeError(
                    f"unexpected successor activity: {source}:{event_type}"
                )
            if cleanup_terminal:
                assert isinstance(payload, Mapping)
                _digest(
                    payload.get("predecessor_epoch_digest"),
                    "cleanup predecessor epoch digest",
                )
                for field in (
                    "evidence_window_activation_generation",
                    "baseline_evidence_cutoff",
                    "current_evidence_cutoff",
                ):
                    if not isinstance(payload.get(field), int):
                        raise ProfiledFaultRuntimeError(
                            f"manager cleanup terminal has invalid {field}"
                        )
                cleanup_terminal_count += 1
                continue
            payload = event.get("payload")
            if isinstance(payload, Mapping):
                for field in (
                    "epoch_number",
                    "predecessor_epoch_number",
                    "successor_epoch_number",
                ):
                    value = payload.get(field)
                    if isinstance(value, int) and value > 0:
                        raise ProfiledFaultRuntimeError(
                            f"unexpected epoch above zero in {source}:{event_type}"
                        )
                proof = payload.get("decision_proof")
                if isinstance(proof, Mapping) and proof.get("epoch_number") != 0:
                    raise ProfiledFaultRuntimeError(
                        "commit decision proof is not epoch zero"
                    )
    request = transition_request()
    for relative in (
        request["bundle_path"],
        request["evidence_snapshot_path"],
    ):
        if (run_directory / str(relative)).exists():
            raise ProfiledFaultRuntimeError(
                f"unexpected transition artifact was produced: {relative}"
            )
    if cleanup_started_ns is not None and cleanup_terminal_count != 1:
        raise ProfiledFaultRuntimeError(
            "manager cleanup requires exactly one caller_failed cycle terminal"
        )


def spawn_owned_process(
    registry: ProcessRegistry,
    *,
    name: str,
    replica_id: int,
    command: Sequence[str],
    log_path: Path,
    working_directory: Path,
) -> tuple[ProcessRecord, IO[bytes]]:
    log = log_path.open("xb", buffering=0)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            tuple(command),
            cwd=working_directory,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        record = registry.register(
            name=name,
            replica_id=replica_id,
            process=process,
        )
    except BaseException as registration_error:
        cleanup_error: BaseException | None = None
        if process is not None:
            try:
                _terminate_unregistered_process(process)
            except BaseException as exc:
                cleanup_error = exc
        log.close()
        if cleanup_error is not None:
            raise ProfiledFaultRuntimeError(
                f"process registration failed and exact child cleanup failed: "
                f"{cleanup_error}"
            ) from registration_error
        raise
    return record, log


def _terminate_unregistered_process(process: subprocess.Popen[bytes]) -> None:
    """Stop a just-spawned child that could not enter the owned registry."""

    if process.poll() is not None:
        return
    pid = int(process.pid)
    group_is_exact = False
    try:
        pgid = int(os.getpgid(pid))
        group_is_exact = pid > 1 and pgid == pid and pgid != int(os.getpgrp())
    except ProcessLookupError:
        pass
    except OSError:
        pass

    signal_error: BaseException | None = None
    if group_is_exact:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            signal_error = exc
    if not group_is_exact or signal_error is not None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            signal_error = signal_error or exc

    try:
        process.wait(timeout=UNREGISTERED_PROCESS_EXIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            signal_error = signal_error or exc
        try:
            process.wait(timeout=UNREGISTERED_PROCESS_EXIT_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProfiledFaultRuntimeError(
                f"unregistered process {pid} did not exit after SIGKILL"
            ) from exc
    except OSError as exc:
        raise ProfiledFaultRuntimeError(
            f"unregistered process {pid} could not be reaped"
        ) from exc
    if process.poll() is None:
        detail = f": {signal_error}" if signal_error is not None else ""
        raise ProfiledFaultRuntimeError(
            f"unregistered process {pid} remains live after cleanup{detail}"
        )


def assert_process_health(
    records: Sequence[ProcessRecord],
    *,
    crashed_replica: int | None,
) -> None:
    for record in records:
        returncode = record.process.poll()
        if returncode is None:
            continue
        if record.replica_id == crashed_replica and returncode == -signal.SIGKILL:
            continue
        raise IncompleteProfiledFaultRun(
            f"unexpected pre-cleanup exit: {record.name}={returncode}"
        )


def wait_until(
    description: str,
    predicate: Callable[[], object | None],
    *,
    phase_timeout_s: float,
    hard_deadline_ns: int,
    records: Sequence[ProcessRecord],
    crashed_replica: int | None,
    health: Callable[[], None] | None = None,
    poll_interval_s: float = 0.05,
) -> object:
    phase_deadline_ns = monotonic_raw_ns() + int(phase_timeout_s * 1_000_000_000)
    while True:
        assert_process_health(records, crashed_replica=crashed_replica)
        if health is not None:
            health()
        result = predicate()
        if result is not None:
            return result
        now = monotonic_raw_ns()
        if now >= hard_deadline_ns:
            raise IncompleteProfiledFaultRun(
                f"hard deadline expired while waiting for {description}"
            )
        if now >= phase_deadline_ns:
            raise IncompleteProfiledFaultRun(f"timed out waiting for {description}")
        time.sleep(poll_interval_s)


def wait_for_fixed_postfault_window(
    profile: FrozenProfile,
    *,
    predicate: Callable[[], object | None],
    hard_deadline_ns: int,
    records: Sequence[ProcessRecord],
) -> object:
    """Observe the full window; zero-throughput buckets are measured evidence."""
    return wait_until(
        "complete postfault window and a fixed Q21 common commit",
        predicate,
        phase_timeout_s=profile.startup_timeout_s,
        hard_deadline_ns=hard_deadline_ns,
        records=records,
        crashed_replica=profile.fault.replica_id,
        health=None,
    )


def concurrent_cleanup(
    records: Sequence[ProcessRecord],
    *,
    faulted_replica_id: int | None,
    post_end_ns: int | None,
) -> tuple[list[dict[str, object]], int]:
    cleanup_started_ns = monotonic_raw_ns()
    sent: dict[str, list[int]] = {record.name: [] for record in records}
    errors: dict[str, list[str]] = {record.name: [] for record in records}
    launcher_pgid = os.getpgrp()

    def record_error(record: ProcessRecord, message: str) -> None:
        if message not in errors[record.name]:
            errors[record.name].append(message)

    def signal_live_groups(signal_number: int) -> None:
        for record in records:
            if record.process.poll() is not None:
                continue
            if (
                int(record.process.pid) != record.pid
                or record.pid <= 1
                or record.pgid <= 1
                or record.pgid != record.pid
                or record.pgid == launcher_pgid
            ):
                record_error(
                    record,
                    f"owned process group identity changed: {record.name}",
                )
                continue
            try:
                live_pgid = os.getpgid(record.pid)
            except ProcessLookupError:
                if record.process.poll() is None:
                    record_error(
                        record,
                        f"owned process group disappeared before cleanup: {record.name}",
                    )
                continue
            except OSError as exc:
                record_error(
                    record,
                    f"could not verify owned process group {record.name}: {exc}",
                )
                continue
            if live_pgid != record.pgid:
                record_error(
                    record,
                    f"owned process group identity changed: {record.name}",
                )
                continue
            try:
                os.killpg(record.pgid, signal_number)
            except ProcessLookupError:
                if record.process.poll() is None:
                    record_error(
                        record,
                        f"owned process group vanished while signalling: {record.name}",
                    )
            except OSError as exc:
                record_error(
                    record,
                    f"could not signal owned process group {record.name}: {exc}",
                )
            else:
                sent[record.name].append(int(signal_number))

    def wait_for_stage(grace_s: float) -> None:
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if all(record.process.poll() is not None for record in records):
                break
            time.sleep(0.05)

    try:
        for signal_number, grace_s in (
            (signal.SIGINT, 10.0),
            (signal.SIGTERM, 3.0),
        ):
            if all(record.process.poll() is not None for record in records):
                break
            signal_live_groups(signal_number)
            wait_for_stage(grace_s)
    finally:
        # Every still-live, identity-validated group gets one final SIGKILL
        # attempt even if an earlier record failed validation or signalling.
        signal_live_groups(signal.SIGKILL)
        wait_for_stage(1.0)

    for record in records:
        if record.process.poll() is None:
            record_error(
                record,
                f"owned process group remained after cleanup: {record.name}",
            )

    ledger: list[dict[str, object]] = []
    for record in records:
        returncode = record.process.poll()
        if record.replica_id == faulted_replica_id and returncode == -signal.SIGKILL:
            classification = "expected_fault"
        elif record.name == MANAGER_SOURCE_ID and sent[record.name] and returncode == 1:
            classification = "expected_cleanup"
        elif (
            record.replica_id >= 0
            and sent[record.name]
            and (
                returncode == 0
                or (
                    faulted_replica_id is None
                    and returncode == -signal.SIGINT
                    and int(signal.SIGINT) in sent[record.name]
                )
            )
        ):
            classification = "expected_cleanup"
        elif (
            record.replica_id >= 0
            and returncode == -signal.SIGKILL
            and post_end_ns is not None
            and cleanup_started_ns >= post_end_ns
            and int(signal.SIGINT) in sent[record.name]
            and int(signal.SIGTERM) in sent[record.name]
            and int(signal.SIGKILL) in sent[record.name]
        ):
            classification = "expected_forced_cleanup"
        else:
            classification = "unexpected_exit"
        ledger.append(
            {
                "name": record.name,
                "replica_id": (record.replica_id if record.replica_id >= 0 else None),
                "pid": record.pid,
                "pgid": record.pgid,
                "signals_sent": sent[record.name],
                "returncode": returncode,
                "classification": classification,
                "cleanup_errors": errors[record.name],
                "cleanup_started_ns": cleanup_started_ns,
                "cleanup_started_after_post_window": (
                    post_end_ns is None or cleanup_started_ns >= post_end_ns
                ),
            }
        )
    return ledger, cleanup_started_ns


def wait_ports_clear(ports: Sequence[int], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        listening = listening_ports(ports)
        if not listening:
            return
        if time.monotonic() >= deadline:
            raise ProfiledFaultRuntimeError(
                f"owned listeners remained after cleanup: {list(listening)}"
            )
        time.sleep(0.05)


def _write_event_csvs(
    profile: FrozenProfile,
    run_directory: Path,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[Path, Path]:
    epoch_path = run_directory / "epochs.csv"
    reputation_path = run_directory / "reputation.csv"
    epoch_rows: list[dict[str, object]] = []
    reputation_rows: list[dict[str, object]] = []
    for source, events in streams.items():
        for event in events:
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            event_type = str(event.get("event_type", ""))
            if event_type == "adaptive.configuration_active":
                epoch_rows.append(
                    {
                        "source_id": source,
                        "source_sequence": event.get("source_sequence"),
                        "source_monotonic_ns": event.get("source_monotonic_ns"),
                        "epoch_number": payload.get("epoch_number"),
                        "tree_id": payload.get("tree_id"),
                        "epoch_digest": payload.get("epoch_digest"),
                        "global_quorum": payload.get("global_quorum"),
                    }
                )
            if "reputation" in event_type:
                reputation_rows.append(
                    {
                        "source_id": source,
                        "source_sequence": event.get("source_sequence"),
                        "source_monotonic_ns": event.get("source_monotonic_ns"),
                        "event_type": event_type,
                        "payload_json": json.dumps(
                            dict(payload), sort_keys=True, separators=(",", ":")
                        ),
                    }
                )

    def write_rows(
        path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, object]]
    ) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=list(fields))
                writer.writeheader()
                writer.writerows(rows)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    write_rows(
        epoch_path,
        (
            "source_id",
            "source_sequence",
            "source_monotonic_ns",
            "epoch_number",
            "tree_id",
            "epoch_digest",
            "global_quorum",
        ),
        epoch_rows,
    )
    write_rows(
        reputation_path,
        (
            "source_id",
            "source_sequence",
            "source_monotonic_ns",
            "event_type",
            "payload_json",
        ),
        reputation_rows,
    )
    return epoch_path, reputation_path


def _source_descriptors(
    profile: FrozenProfile,
    run_directory: Path,
    records: Sequence[ProcessRecord],
    source_instances: Mapping[str, str],
) -> list[dict[str, object]]:
    by_name = {record.name: record for record in records}
    ordered = [f"replica-{replica}" for replica in profile.replica_ids] + [
        MANAGER_SOURCE_ID
    ]
    descriptors: list[dict[str, object]] = []
    for source in ordered:
        path = run_directory / "raw" / f"{source}.jsonl"
        if not path.is_file():
            raise IncompleteProfiledFaultRun(f"missing structured stream: {path.name}")
        record = by_name[source]
        descriptors.append(
            {
                "source_kind": (
                    "replica" if source.startswith("replica-") else "adaptation_manager"
                ),
                "source_id": source,
                "source_instance": source_instances[source],
                "pid": record.pid,
                "pgid": record.pgid,
                "path": str(path.relative_to(run_directory)),
                "sha256": sha256_file(path),
            }
        )
    return descriptors


def _runtime_manifest(profile: FrozenProfile) -> dict[str, object]:
    return {
        "replica_ids": list(profile.replica_ids),
        "fault_threshold": profile.fault_threshold,
        "quorum": profile.quorum,
        "fanout": profile.fanout,
        "pipeline_depth": profile.pipeline_depth,
    }


EVENT_ENVELOPE_FIELDS = frozenset(
    {
        "event_schema_version",
        "run_id",
        "source_kind",
        "source_id",
        "source_instance",
        "source_sequence",
        "source_monotonic_ns",
        "event_type",
        "payload",
    }
)
CONFIGURATION_ACTIVE_FIELDS = frozenset(
    {
        "epoch_number",
        "tree_id",
        "epoch_digest",
        "block_hash",
        "context_generation",
        "observer_replica",
        "wait_exempt_signers",
        "accepted_signers",
        "absent_direct_children",
        "missing_optional_signers",
        "required_branch_gaps",
        "root_signer_count",
        "global_quorum",
        "rejection_reason",
    }
)
COMMIT_OBSERVED_FIELDS = frozenset(
    {
        "block_height",
        "block_hash",
        "parent_hash",
        "transaction_count",
        "commit_batch_index",
    }
)
COMMITTED_FIELDS = frozenset(
    {
        *COMMIT_OBSERVED_FIELDS,
        "designated_observer",
        "decision_proof",
        "view_generation",
    }
)
DECISION_PROOF_FIELDS = frozenset(
    {"epoch_number", "tree_id", "epoch_digest", "block_hash"}
)


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProfiledFaultRuntimeError(f"{label} is not a SHA-256 digest")
    return value


def validate_final_streams(
    profile: FrozenProfile,
    *,
    manifest: Mapping[str, object],
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    crash_marker: Mapping[str, object],
    cleanup_started_ns: int,
    run_directory: Path,
) -> dict[str, object]:
    run_id = manifest.get("run_id")
    instances = manifest.get("source_instances")
    windows = manifest.get("measurement_windows")
    if not isinstance(run_id, str) or not isinstance(instances, Mapping):
        raise ProfiledFaultRuntimeError("manifest source identity is malformed")
    if not isinstance(windows, Mapping):
        raise ProfiledFaultRuntimeError("manifest measurement windows are absent")
    expected_sources = {f"replica-{replica}" for replica in profile.replica_ids} | {
        MANAGER_SOURCE_ID
    }
    if set(streams) != expected_sources or set(instances) != expected_sources:
        raise ProfiledFaultRuntimeError("final source membership drifted")
    confirmed_ns = crash_marker.get("confirmed_monotonic_ns")
    requested_ns = crash_marker.get("requested_monotonic_ns")
    if not isinstance(confirmed_ns, int) or confirmed_ns <= 0:
        raise ProfiledFaultRuntimeError("confirmed SIGKILL time is malformed")
    if not isinstance(requested_ns, int) or not 0 < requested_ns <= confirmed_ns:
        raise ProfiledFaultRuntimeError("requested SIGKILL time is malformed")
    hashes_by_height: dict[int, set[str]] = {}
    heights_by_hash: dict[str, set[int]] = {}
    metadata_by_height_hash: dict[tuple[int, str], set[tuple[object, ...]]] = {}
    authoritative_keys: set[tuple[object, ...]] = set()
    ready_timestamps: dict[str, int] = {}
    for source, events in streams.items():
        expected_kind = (
            "replica" if source.startswith("replica-") else "adaptation_manager"
        )
        previous_timestamp: int | None = None
        pending_commit_witnesses: dict[
            tuple[object, ...], tuple[tuple[object, ...], int]
        ] = {}
        for expected_sequence, event in enumerate(events, 1):
            if set(event) != EVENT_ENVELOPE_FIELDS:
                raise ProfiledFaultRuntimeError(
                    f"structured envelope schema drift in {source}"
                )
            if (
                event.get("event_schema_version") != 1
                or event.get("run_id") != run_id
                or event.get("source_kind") != expected_kind
                or event.get("source_id") != source
                or event.get("source_instance") != instances[source]
                or event.get("source_sequence") != expected_sequence
            ):
                raise ProfiledFaultRuntimeError(
                    f"structured source identity or sequence drift in {source}"
                )
            timestamp = _timestamp(event)
            if previous_timestamp is not None and timestamp < previous_timestamp:
                raise ProfiledFaultRuntimeError(
                    f"structured timestamp regressed in {source}"
                )
            previous_timestamp = timestamp
            if (
                source == f"replica-{profile.fault.replica_id}"
                and timestamp >= confirmed_ns
            ):
                raise ProfiledFaultRuntimeError(
                    "crashed replica emitted an event after confirmed exit"
                )
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise ProfiledFaultRuntimeError(
                    f"structured payload is not an object in {source}"
                )
            event_type = event.get("event_type")
            if event_type == "process.ready":
                if (
                    source in ready_timestamps
                    or set(payload) != PROCESS_READY_FIELDS
                    or payload.get("exit_status") is not None
                ):
                    raise ProfiledFaultRuntimeError(
                        f"{source} requires exactly one process.ready "
                        "with canonical payload"
                    )
                ready_timestamps[source] = timestamp
            if event_type == "adaptive.configuration_active":
                if set(payload) != CONFIGURATION_ACTIVE_FIELDS:
                    raise ProfiledFaultRuntimeError("configuration-active schema drift")
                replica = int(source.removeprefix("replica-"))
                if (
                    payload.get("epoch_number") != 0
                    or payload.get("tree_id") not in profile.replica_ids
                    or payload.get("observer_replica") != replica
                    or payload.get("block_hash") is not None
                    or payload.get("context_generation") is not None
                    or payload.get("wait_exempt_signers") != []
                    or payload.get("accepted_signers") != []
                    or payload.get("absent_direct_children") != []
                    or payload.get("missing_optional_signers") != []
                    or payload.get("required_branch_gaps") != []
                    or payload.get("root_signer_count") != 0
                    or payload.get("global_quorum") != profile.quorum
                    or payload.get("rejection_reason") is not None
                ):
                    raise ProfiledFaultRuntimeError(
                        "configuration-active record is not canonical epoch zero"
                    )
                _digest(payload.get("epoch_digest"), "epoch digest")
                if payload.get("epoch_digest") != EXPECTED_EPOCH_ZERO_DIGEST:
                    raise ProfiledFaultRuntimeError(
                        "configuration-active digest does not bind exact epoch zero"
                    )
            if event_type not in {"block.committed", "block.commit_observed"}:
                continue
            expected_fields = (
                COMMITTED_FIELDS
                if event_type == "block.committed"
                else COMMIT_OBSERVED_FIELDS
            )
            if set(payload) != expected_fields:
                raise ProfiledFaultRuntimeError("commit payload schema drift")
            key = _commit_key(event)
            height = int(key[0])
            block_hash = _digest(key[1], "commit block hash")
            _digest(key[2], "commit parent hash")
            identity = key[:2]
            local_metadata = key[2:]
            hashes_by_height.setdefault(height, set()).add(block_hash)
            heights_by_hash.setdefault(block_hash, set()).add(height)
            metadata_by_height_hash.setdefault((height, block_hash), set()).add(
                key[2:4]
            )
            if event_type == "block.commit_observed":
                if identity in pending_commit_witnesses:
                    raise ProfiledFaultRuntimeError(
                        "duplicate pending same-source commit witness"
                    )
                pending_commit_witnesses[identity] = (
                    local_metadata,
                    _sequence(event),
                )
                continue
            witness = pending_commit_witnesses.get(identity)
            if witness is None:
                raise ProfiledFaultRuntimeError(
                    "rich commit lacks a preceding same-source commit witness"
                )
            witness_metadata, witness_sequence = witness
            if witness_metadata != local_metadata:
                raise ProfiledFaultRuntimeError(
                    "same-source commit metadata disagreement"
                )
            if witness_sequence >= _sequence(event):
                raise ProfiledFaultRuntimeError(
                    "rich commit lacks a preceding same-source commit witness"
                )
            del pending_commit_witnesses[identity]
            if event_type == "block.committed":
                proof = payload.get("decision_proof")
                if (
                    not isinstance(proof, Mapping)
                    or set(proof) != DECISION_PROOF_FIELDS
                ):
                    raise ProfiledFaultRuntimeError("decision proof schema drift")
                if (
                    proof.get("epoch_number") != 0
                    or proof.get("tree_id") not in profile.replica_ids
                    or proof.get("block_hash") != block_hash
                ):
                    raise ProfiledFaultRuntimeError(
                        "authoritative decision proof is not exact epoch zero"
                    )
                if (
                    _digest(proof.get("epoch_digest"), "decision epoch digest")
                    != EXPECTED_EPOCH_ZERO_DIGEST
                ):
                    raise ProfiledFaultRuntimeError(
                        "decision proof digest does not bind exact epoch zero"
                    )
                is_observer = source == f"replica-{profile.authoritative_observer}"
                if payload.get("designated_observer") is not is_observer:
                    raise ProfiledFaultRuntimeError(
                        "designated observer flag disagrees with source"
                    )
                if is_observer:
                    if identity in authoritative_keys:
                        raise ProfiledFaultRuntimeError(
                            "duplicate authoritative commit identity"
                        )
                    authoritative_keys.add(identity)
    if set(ready_timestamps) != expected_sources:
        raise ProfiledFaultRuntimeError(
            "every exact source requires exactly one process.ready "
            "with canonical payload"
        )
    if any(len(hashes) != 1 for hashes in hashes_by_height.values()):
        raise ProfiledFaultRuntimeError("global same-height hash conflict")
    if any(len(heights) != 1 for heights in heights_by_hash.values()):
        raise ProfiledFaultRuntimeError("block hash was reused at different heights")
    if any(len(values) != 1 for values in metadata_by_height_hash.values()):
        raise ProfiledFaultRuntimeError("rich commit metadata disagreement")

    crash_boundary = manifest.get("crash_boundary")
    if not isinstance(crash_boundary, Mapping):
        raise ProfiledFaultRuntimeError("final crash boundary is absent")
    assert_boundary_race_free(
        profile,
        crash_boundary,
        {
            source: events
            for source, events in streams.items()
            if source.startswith("replica-")
        },
        crash_request_ns=requested_ns,
    )

    baseline = windows.get("baseline")
    postfault = windows.get("postfault")
    if not isinstance(baseline, Mapping) or not isinstance(postfault, Mapping):
        raise ProfiledFaultRuntimeError("measurement windows are malformed")
    baseline_start = baseline.get("start_ns")
    baseline_end = baseline.get("end_ns")
    post_start = postfault.get("start_ns")
    post_end = postfault.get("end_ns")
    if not all(
        isinstance(value, int) and value > 0
        for value in (baseline_start, baseline_end, post_start, post_end)
    ):
        raise ProfiledFaultRuntimeError("measurement boundaries are invalid")
    if not (
        baseline_start < baseline_end < post_start < post_end <= cleanup_started_ns
        and post_start >= confirmed_ns
    ):
        raise ProfiledFaultRuntimeError("measurement window ordering drifted")
    bucket_width_ns = profile.bucket_width_s * 1_000_000_000
    if (
        baseline_end - baseline_start != profile.baseline_bucket_count * bucket_width_ns
        or post_end - post_start != profile.post_bucket_count * bucket_width_ns
    ):
        raise ProfiledFaultRuntimeError(
            "measurement window duration is not the exact frozen bucket span"
        )
    ready_barrier_ns = max(ready_timestamps.values())
    observer_source = f"replica-{profile.authoritative_observer}"
    observer_commits_after_ready = [
        _timestamp(event)
        for event in streams[observer_source]
        if event.get("event_type") == "block.committed"
        and _timestamp(event) >= ready_barrier_ns
    ]
    if not observer_commits_after_ready or baseline_start != min(
        observer_commits_after_ready
    ):
        raise ProfiledFaultRuntimeError(
            "baseline is not anchored to the first authoritative commit "
            "after the exact readiness barrier"
        )
    assert_no_successor_activity(
        streams,
        run_directory=run_directory,
        cleanup_started_ns=cleanup_started_ns,
        post_end_ns=post_end,
    )
    replica_streams = {
        source: events
        for source, events in streams.items()
        if source.startswith("replica-")
    }
    witnesses = postfault_witnesses(profile)
    pre_common = find_common_commit(
        profile,
        replica_streams,
        witnesses=witnesses,
        after_ns=baseline_start,
        before_ns=baseline_end,
    )
    post_common = find_common_commit(
        profile,
        replica_streams,
        witnesses=witnesses,
        after_ns=post_start,
        before_ns=post_end,
    )
    if pre_common is None or post_common is None:
        raise ProfiledFaultRuntimeError(
            "fixed Q21 common commit is absent from a measurement window"
        )
    baseline_rows = throughput_rows(
        profile,
        replica_streams[f"replica-{profile.authoritative_observer}"],
        phase="baseline",
        start_ns=baseline_start,
        bucket_count=profile.baseline_bucket_count,
    )
    require_positive_buckets(baseline_rows, phase="baseline")
    post_rows = throughput_rows(
        profile,
        replica_streams[f"replica-{profile.authoritative_observer}"],
        phase="postfault",
        start_ns=post_start,
        bucket_count=profile.post_bucket_count,
    )
    recovery_gate = enforce_recovery_gate(profile, baseline_rows, post_rows)
    return {
        "pre_fault_common_commit": pre_common,
        "post_fault_common_commit": post_common,
        "baseline_rows": baseline_rows,
        "postfault_rows": post_rows,
        "recovery_gate": recovery_gate,
    }


def final_artifact_inventory(
    run_directory: Path, *, exclude: Iterable[str] = ()
) -> list[dict[str, object]]:
    excluded = set(exclude) | {
        "manifest.json",
        "validation.json",
        "evidence-seal.json",
    }
    artifacts: list[dict[str, object]] = []
    pending = [run_directory]
    while pending:
        directory = pending.pop()
        for child in sorted(directory.iterdir(), key=lambda path: path.name):
            relative = child.relative_to(run_directory).as_posix()
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ProfiledFaultRuntimeError(
                    f"run artifact is a symlink: {relative}"
                )
            if stat.S_ISDIR(info.st_mode):
                pending.append(child)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ProfiledFaultRuntimeError(
                    f"run artifact is not a regular file: {relative}"
                )
            if relative in excluded:
                continue
            artifacts.append(
                {
                    "kind": "run_artifact",
                    "replica_id": None,
                    "path": relative,
                    "size_bytes": info.st_size,
                    "sha256": sha256_file(child),
                }
            )
    return sorted(artifacts, key=lambda item: str(item["path"]))


def _load_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfiledFaultRuntimeError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise ProfiledFaultRuntimeError(f"{label} is not a JSON object")
    return value


def _verify_preserved_artifact_inventory(
    run_directory: Path,
    manifest: Mapping[str, object],
    sealed_paths: set[str],
) -> None:
    values = manifest.get("runtime_artifacts")
    if not isinstance(values, list):
        raise ProfiledFaultRuntimeError("manifest artifact inventory is absent")
    expected_paths = sealed_paths - {"manifest.json", "validation.json"}
    observed_paths: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping) or set(item) != {
            "kind",
            "replica_id",
            "path",
            "size_bytes",
            "sha256",
        }:
            raise ProfiledFaultRuntimeError("manifest artifact schema drifted")
        relative = item.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or relative in observed_paths
        ):
            raise ProfiledFaultRuntimeError("manifest artifact path drifted")
        observed_paths.add(relative)
        path = run_directory / relative
        if (
            not path.is_file()
            or path.is_symlink()
            or item.get("size_bytes") != path.stat().st_size
            or item.get("sha256") != sha256_file(path)
        ):
            raise ProfiledFaultRuntimeError(
                f"manifest artifact bytes drifted: {relative}"
            )
    if observed_paths != expected_paths:
        raise ProfiledFaultRuntimeError(
            "manifest artifact inventory is not the exact sealed file set"
        )


def _verify_throughput_csv(
    path: Path, expected_rows: Sequence[Mapping[str, object]]
) -> None:
    try:
        with path.open("r", encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            rows = list(reader)
    except OSError as exc:
        raise ProfiledFaultRuntimeError("throughput CSV is unreadable") from exc
    expected_fields = list(expected_rows[0]) if expected_rows else []
    if reader.fieldnames != expected_fields or len(rows) != len(expected_rows):
        raise ProfiledFaultRuntimeError("throughput CSV schema or row count drifted")
    for actual, expected in zip(rows, expected_rows):
        canonical = {field: str(expected[field]) for field in expected_fields}
        if actual != canonical:
            raise ProfiledFaultRuntimeError("throughput CSV differs from raw events")


def _verify_recorded_recovery_gate(
    profile: FrozenProfile,
    recorded_validation: Mapping[str, object],
    expected: Mapping[str, object],
) -> None:
    strict = (
        profile.minimum_positive_postfault_buckets > 0
        or profile.minimum_mean_throughput_retention > 0
    )
    if "recovery_gate" not in recorded_validation and not strict:
        # Runs sealed before the optional gate existed remain admissible when
        # both frozen requirements default to their disabled zero values.
        return
    recorded = recorded_validation.get("recovery_gate")
    if recorded != expected:
        raise ProfiledFaultRuntimeError(
            "recorded recovery gate differs from closed raw evidence"
        )


def validate_preserved_run(run_directory: Path) -> dict[str, object]:
    run_directory = run_directory.resolve()
    try:
        seal = verify_evidence_seal(run_directory)
    except EvidenceSealError as exc:
        raise ProfiledFaultRuntimeError(f"evidence seal rejected: {exc}") from exc
    sealed_paths = {entry.path for entry in seal.entries}
    profile = load_frozen_profile(run_directory / "profile.json")
    require_shipped_profile(profile)
    manifest = _load_json_object(run_directory / "manifest.json", "manifest")
    recorded_validation = _load_json_object(
        run_directory / "validation.json", "validation"
    )
    _verify_preserved_artifact_inventory(
        run_directory,
        manifest,
        sealed_paths,
    )
    if (
        manifest.get("schema_version") != 1
        or manifest.get("run_id") != run_directory.name
        or manifest.get("attempt") != 1
        or manifest.get("attempt_scope") != "one_invocation_without_automatic_retry"
        or manifest.get("complete") is not True
        or manifest.get("runtime_error") is not None
        or recorded_validation.get("verdict") != "PASS"
    ):
        raise ProfiledFaultRuntimeError(
            "preserved run is not one complete PASS invocation"
        )
    preflight_record = manifest.get("preflight")
    if not isinstance(preflight_record, Mapping):
        raise ProfiledFaultRuntimeError("preserved preflight record is absent")
    provenance = preflight_record.get("build_provenance")
    witness = preflight_record.get("epoch_zero_witness")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("revision") != manifest.get("kauri_revision")
        or preflight_record.get("revision") != manifest.get("kauri_revision")
        or not isinstance(witness, Mapping)
        or not isinstance(witness.get("epoch_zero"), Mapping)
        or witness["epoch_zero"].get("epoch_digest") != EXPECTED_EPOCH_ZERO_DIGEST
        or _load_json_object(
            run_directory / "runtime" / "build-provenance.json",
            "copied build provenance",
        )
        != dict(provenance)
        or _load_json_object(
            run_directory / "runtime" / "epoch-zero-witness.json",
            "copied epoch-zero witness",
        )
        != dict(witness)
    ):
        raise ProfiledFaultRuntimeError(
            "preserved revision or epoch-zero provenance drifted"
        )
    cleanup = manifest.get("cleanup_ledger")
    expected_names = {MANAGER_SOURCE_ID} | {
        f"replica-{replica}" for replica in profile.replica_ids
    }
    if (
        not isinstance(cleanup, list)
        or {item.get("name") for item in cleanup if isinstance(item, Mapping)}
        != expected_names
    ):
        raise ProfiledFaultRuntimeError("cleanup ledger membership drifted")
    cleanup_times = {
        item.get("cleanup_started_ns") for item in cleanup if isinstance(item, Mapping)
    }
    if len(cleanup_times) != 1:
        raise ProfiledFaultRuntimeError("cleanup timestamp drifted")
    cleanup_started_ns = next(iter(cleanup_times))
    if not isinstance(cleanup_started_ns, int) or cleanup_started_ns <= 0:
        raise ProfiledFaultRuntimeError("cleanup timestamp is malformed")
    for item in cleanup:
        if (
            not isinstance(item, Mapping)
            or item.get("classification")
            not in {
                "expected_fault",
                "expected_cleanup",
                "expected_forced_cleanup",
            }
            or item.get("cleanup_errors") != []
        ):
            raise ProfiledFaultRuntimeError("cleanup ledger contains a failure")
    streams = event_streams(
        profile,
        run_directory,
        include_manager=True,
        allow_partial=False,
    )
    crash_marker = _load_json_object(
        run_directory / "crash-marker.json", "crash marker"
    )
    final_audit = validate_final_streams(
        profile,
        manifest=manifest,
        streams=streams,
        crash_marker=crash_marker,
        cleanup_started_ns=cleanup_started_ns,
        run_directory=run_directory,
    )
    journal = read_jsonl(run_directory / "raw" / "fault-orchestrator.jsonl")
    replica_streams = {
        source: events
        for source, events in streams.items()
        if source.startswith("replica-")
    }
    process_exits = manifest.get("process_exits")
    if not isinstance(process_exits, list):
        raise ProfiledFaultRuntimeError("process exit evidence is malformed")
    validate_synthetic_run(
        profile,
        manifest=manifest,
        fault_journal=journal,
        streams=replica_streams,
        process_exits=process_exits,
        crash_request_ns=int(crash_marker["requested_monotonic_ns"]),
    )
    rows = [*final_audit["baseline_rows"], *final_audit["postfault_rows"]]
    _verify_throughput_csv(run_directory / "throughput.csv", rows)
    baseline_mean = statistics.fmean(
        float(row["tps"]) for row in final_audit["baseline_rows"]
    )
    post_mean = statistics.fmean(
        float(row["tps"]) for row in final_audit["postfault_rows"]
    )
    recovery_gate = final_audit["recovery_gate"]
    if not isinstance(recovery_gate, Mapping):
        raise ProfiledFaultRuntimeError("source-blind recovery gate is malformed")
    _verify_recorded_recovery_gate(
        profile,
        recorded_validation,
        recovery_gate,
    )
    if (
        recorded_validation.get("baseline_mean_tps") != baseline_mean
        or recorded_validation.get("postfault_mean_tps") != post_mean
        or recorded_validation.get("throughput_retention") != post_mean / baseline_mean
        or recorded_validation.get("measurement_windows")
        != manifest.get("measurement_windows")
        or recorded_validation.get("pre_fault_common_commit")
        != final_audit["pre_fault_common_commit"]
        or recorded_validation.get("post_fault_common_commit")
        != final_audit["post_fault_common_commit"]
    ):
        raise ProfiledFaultRuntimeError(
            "recorded validation differs from closed raw evidence"
        )
    return {
        "schema_version": 1,
        "verdict": "PASS",
        "run_directory": str(run_directory),
        "run_id": manifest["run_id"],
        "revision": manifest["kauri_revision"],
        "profile_id": profile.profile_id,
        "evidence_tree_sha256": seal.tree_sha256,
        "evidence_seal_sha256": seal.seal_sha256,
        "baseline_mean_tps": baseline_mean,
        "postfault_mean_tps": post_mean,
        "throughput_retention": post_mean / baseline_mean,
        "recovery_gate": dict(recovery_gate),
    }


def _seal_and_validate_terminal_run(run_directory: Path, verdict: str) -> None:
    if verdict not in {"PASS", "FAIL", "INCOMPLETE"}:
        raise ProfiledFaultRuntimeError(f"unknown terminal verdict: {verdict}")
    try:
        create_evidence_seal(run_directory)
        verify_evidence_seal(run_directory)
    except (OSError, EvidenceSealError) as exc:
        raise ProfiledFaultRuntimeError(
            f"preserved run evidence could not be sealed: {exc}"
        ) from exc
    if verdict != "PASS":
        return
    try:
        independent = validate_preserved_run(run_directory)
    except (
        OSError,
        ValueError,
        ProfiledFaultEvaluationError,
        ProfiledFaultRuntimeError,
    ) as exc:
        raise ProfiledFaultRuntimeError(
            f"preserved semantic revalidation rejected {run_directory}: {exc}"
        ) from exc
    if independent.get("verdict") != "PASS":
        raise ProfiledFaultRuntimeError(
            f"preserved semantic revalidation did not PASS: {run_directory}"
        )


def run_once(
    *,
    profile_path: Path,
    repository: Path,
    results_root: Path,
    app_binary: Path,
    manager_binary: Path,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    epoch_profile_digest_binary: Path,
    build_directory: Path,
    build_provenance_path: Path,
) -> tuple[Path, str]:
    profile_path = profile_path.resolve()
    repository = repository.resolve()
    profile = load_frozen_profile(profile_path)
    require_shipped_profile(profile)
    if (
        len(profile.replica_ids) != 31
        or profile.quorum != 21
        or profile.fanout != 5
        or profile.pipeline_depth != 2
    ):
        raise ProfiledFaultRuntimeError(
            "live runner accepts only the frozen N31 fanout-five shakedown"
        )
    preflight_result = preflight(
        profile_path=profile_path,
        repository=repository,
        app_binary=app_binary,
        manager_binary=manager_binary,
        keygen_binary=keygen_binary,
        tls_keygen_binary=tls_keygen_binary,
        epoch_profile_digest_binary=epoch_profile_digest_binary,
        build_directory=build_directory,
        build_provenance_path=build_provenance_path,
    )
    revision = str(preflight_result["revision"])
    binaries = {
        "app": app_binary.resolve(),
        "manager": manager_binary.resolve(),
        "keygen": keygen_binary.resolve(),
        "tls_keygen": tls_keygen_binary.resolve(),
        "epoch_profile_digest": epoch_profile_digest_binary.resolve(),
    }
    run_directory = create_run_directory(results_root.resolve())
    run_id = run_directory.name
    write_exclusive(run_directory / "profile.json", profile_path.read_bytes())
    hard_deadline_ns = monotonic_raw_ns() + int(profile.hard_timeout_s * 1_000_000_000)
    started_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    state_path = run_directory / "runner-state.json"
    state: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "revision": revision,
        "started_utc": started_utc,
        "phase": "identity_generation",
        "runtime_error": None,
    }
    write_json_exclusive(state_path, state)
    source_instances = {
        f"replica-{replica}": (f"{run_id}-replica-{replica}-{uuid.uuid4().hex}")
        for replica in profile.replica_ids
    }
    source_instances[MANAGER_SOURCE_ID] = f"{run_id}-manager-{uuid.uuid4().hex}"
    plan = build_fault_plan(profile)
    registry = ProcessRegistry(monotonic_ns=monotonic_raw_ns)
    resources = ExitStack()
    log_handles: list[IO[bytes]] = []
    records: list[ProcessRecord] = []
    lifecycle: FaultLifecycle | None = None
    runtime_artifacts: list[dict[str, object]] = []
    boundary: dict[str, object] | None = None
    crash_marker: dict[str, object] | None = None
    baseline_rows: list[dict[str, object]] = []
    post_rows: list[dict[str, object]] = []
    baseline_start_ns: int | None = None
    baseline_end_ns: int | None = None
    post_start_ns: int | None = None
    post_end_ns: int | None = None
    runtime_error: str | None = None
    cleanup_error: str | None = None
    cleanup_ledger: list[dict[str, object]] = []
    cleanup_started_ns: int | None = None
    interrupted = False
    pre_cleanup_exits: list[dict[str, object]] = []
    previous_handlers: dict[int, Any] = {}

    def update_state(phase: str, **extra: object) -> None:
        state["phase"] = phase
        state.update(extra)
        replace_json(state_path, state)

    def request_shutdown(signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        raise KeyboardInterrupt(signal.Signals(signum).name)

    try:
        previous_handlers = {
            signum: signal.signal(signum, request_shutdown)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        lifecycle = resources.enter_context(
            FaultEvidence(
                run_directory,
                plan,
                monotonic_ns=monotonic_raw_ns,
            )
        )
        bls, tls, issuer = generate_identities(
            profile,
            keygen_binary=binaries["keygen"],
            tls_keygen_binary=binaries["tls_keygen"],
            config_directory=run_directory / "config",
        )
        manager_command, replica_commands, runtime_artifacts = write_runtime_inputs(
            profile,
            run_directory=run_directory,
            app_binary=binaries["app"],
            manager_binary=binaries["manager"],
            bls=bls,
            tls=tls,
            issuer=issuer,
            run_id=run_id,
            source_instances=source_instances,
        )
        build_provenance_copy = run_directory / "runtime" / "build-provenance.json"
        epoch_witness_copy = run_directory / "runtime" / "epoch-zero-witness.json"
        write_json_exclusive(
            build_provenance_copy,
            preflight_result["build_provenance"],
        )
        write_json_exclusive(
            epoch_witness_copy,
            preflight_result["epoch_zero_witness"],
        )
        update_state("launch")
        manager_record, manager_log = spawn_owned_process(
            registry,
            name=MANAGER_SOURCE_ID,
            replica_id=-1,
            command=manager_command,
            log_path=run_directory / "logs" / "adaptive-manager.log",
            working_directory=run_directory,
        )
        records.append(manager_record)
        log_handles.append(manager_log)
        for replica in profile.replica_ids:
            record, log = spawn_owned_process(
                registry,
                name=f"replica-{replica}",
                replica_id=replica,
                command=replica_commands[replica],
                log_path=run_directory / "logs" / f"replica-{replica}.log",
                working_directory=run_directory,
            )
            records.append(record)
            log_handles.append(log)

        def all_ready() -> int | None:
            streams = event_streams(
                profile, run_directory, include_manager=True, allow_partial=True
            )
            ready_events = [
                [
                    event
                    for event in events
                    if event.get("event_type") == "process.ready"
                ]
                for events in streams.values()
            ]
            if not all(len(events) == 1 for events in ready_events):
                return None
            return max(_timestamp(events[0]) for events in ready_events)

        ready_barrier_ns = int(
            wait_until(
                "all 32 exact process.ready events",
                all_ready,
                phase_timeout_s=profile.startup_timeout_s,
                hard_deadline_ns=hard_deadline_ns,
                records=records,
                crashed_replica=None,
            )
        )
        update_state("baseline", ready_barrier_ns=ready_barrier_ns)

        def first_observer_commit() -> int | None:
            streams = event_streams(profile, run_directory, allow_partial=True)
            values = [
                _timestamp(event)
                for event in streams[f"replica-{profile.authoritative_observer}"]
                if event.get("event_type") == "block.committed"
                and _timestamp(event) >= ready_barrier_ns
            ]
            return min(values) if values else None

        baseline_start_ns = int(
            wait_until(
                "first authoritative epoch-zero commit",
                first_observer_commit,
                phase_timeout_s=profile.startup_timeout_s,
                hard_deadline_ns=hard_deadline_ns,
                records=records,
                crashed_replica=None,
            )
        )
        baseline_end_ns = baseline_start_ns + (
            profile.baseline_bucket_count * profile.bucket_width_s * 1_000_000_000
        )
        update_state(
            "measuring_baseline",
            baseline_start_ns=baseline_start_ns,
            baseline_end_ns=baseline_end_ns,
        )

        def observer_progress_health() -> None:
            streams = event_streams(profile, run_directory, allow_partial=True)
            commits = [
                _timestamp(event)
                for event in streams[f"replica-{profile.authoritative_observer}"]
                if event.get("event_type") == "block.committed"
            ]
            reference = max(commits) if commits else baseline_start_ns
            if monotonic_raw_ns() - reference > int(
                profile.maximum_stall_s * 1_000_000_000
            ):
                raise IncompleteProfiledFaultRun(
                    "authoritative observer exceeded maximum stall"
                )

        def baseline_complete() -> dict[str, object] | None:
            if monotonic_raw_ns() < baseline_end_ns:
                return None
            streams = event_streams(profile, run_directory, allow_partial=True)
            rows = throughput_rows(
                profile,
                streams[f"replica-{profile.authoritative_observer}"],
                phase="baseline",
                start_ns=baseline_start_ns,
                bucket_count=profile.baseline_bucket_count,
            )
            require_positive_buckets(rows, phase="baseline")
            common = find_common_commit(
                profile,
                streams,
                witnesses=postfault_witnesses(profile),
                after_ns=baseline_start_ns,
                before_ns=baseline_end_ns,
            )
            if common is None:
                return None
            baseline_rows.extend(rows)
            return common

        wait_until(
            "six positive baseline buckets and a Q21 common commit",
            baseline_complete,
            phase_timeout_s=profile.startup_timeout_s,
            hard_deadline_ns=hard_deadline_ns,
            records=records,
            crashed_replica=None,
            health=observer_progress_health,
        )
        boundary_sources = {f"replica-{replica}" for replica in profile.replica_ids}
        boundary_poller = ConfigurationBoundaryPoller(
            profile,
            run_directory,
            watermarks={source: -1 for source in boundary_sources},
            offsets={source: 0 for source in boundary_sources},
        )
        update_state("awaiting_current_fault_tree")
        boundary = dict(
            wait_until(
                (
                    "current exact common epoch-zero tree-"
                    f"{profile.fault.tree_id} boundary"
                ),
                boundary_poller.poll,
                phase_timeout_s=profile.startup_timeout_s,
                hard_deadline_ns=hard_deadline_ns,
                records=records,
                crashed_replica=None,
                health=observer_progress_health,
                poll_interval_s=0.01,
            )
        )
        update_state("injecting_sigkill", crash_boundary=boundary)
        if lifecycle is None:
            raise ProfiledFaultRuntimeError("fault lifecycle was not opened")
        lifecycle.start(profile.fault.fault_id)
        outcome = registry.sigkill_replica_group(
            fault_id=profile.fault.fault_id,
            replica_id=profile.fault.replica_id,
            timeout_s=profile.crash_confirm_timeout_s,
        )
        lifecycle.terminal(
            profile.fault.fault_id,
            "succeeded",
            {
                "replica_id": outcome.replica_id,
                "name": outcome.name,
                "pid": outcome.pid,
                "pgid": outcome.pgid,
                "signal_number": outcome.signal_number,
                "returncode": outcome.returncode,
                "requested_monotonic_ns": outcome.requested_monotonic_ns,
                "confirmed_monotonic_ns": outcome.confirmed_monotonic_ns,
            },
        )
        crash_marker = {
            "fault_id": profile.fault.fault_id,
            "replica_id": outcome.replica_id,
            "pid": outcome.pid,
            "pgid": outcome.pgid,
            "signal": "SIGKILL",
            "signal_number": outcome.signal_number,
            "returncode": outcome.returncode,
            "requested_monotonic_ns": outcome.requested_monotonic_ns,
            "confirmed_monotonic_ns": outcome.confirmed_monotonic_ns,
        }
        write_json_exclusive(run_directory / "crash-marker.json", crash_marker)
        pre_cleanup_exits.append(
            {
                "name": outcome.name,
                "replica_id": outcome.replica_id,
                "returncode": outcome.returncode,
            }
        )
        streams_after_crash = event_streams(profile, run_directory, allow_partial=True)
        assert_boundary_race_free(
            profile,
            boundary,
            streams_after_crash,
            crash_request_ns=outcome.requested_monotonic_ns,
        )
        post_start_ns = outcome.confirmed_monotonic_ns
        post_end_ns = post_start_ns + (
            profile.post_bucket_count * profile.bucket_width_s * 1_000_000_000
        )
        update_state(
            "measuring_postfault",
            crash_marker=crash_marker,
            post_start_ns=post_start_ns,
            post_end_ns=post_end_ns,
        )

        def post_complete() -> dict[str, object] | None:
            observed_ns = monotonic_raw_ns()
            if observed_ns < post_end_ns:
                return None
            streams = event_streams(profile, run_directory, allow_partial=True)
            assert_no_successor_activity(
                {
                    **streams,
                    MANAGER_SOURCE_ID: read_jsonl(
                        run_directory / "raw" / "adaptive-manager.jsonl",
                        allow_partial=True,
                    ),
                },
                run_directory=run_directory,
            )
            qualification = evaluate_complete_postfault_window(
                profile,
                streams,
                start_ns=post_start_ns,
                end_ns=post_end_ns,
                observed_ns=observed_ns,
            )
            if qualification is None:
                return None
            post_rows.extend(qualification["rows"])
            return qualification["common_commit"]

        wait_for_fixed_postfault_window(
            profile,
            predicate=post_complete,
            hard_deadline_ns=hard_deadline_ns,
            records=records,
        )
        update_state("qualified_pending_cleanup")
    except KeyboardInterrupt as exc:
        runtime_error = f"interrupted: {exc}"
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        ProfiledFaultEvaluationError,
        ProfiledFaultRuntimeError,
    ) as exc:
        runtime_error = str(exc)
    finally:
        cleanup_messages: list[str] = []
        try:
            if records:
                cleanup_ledger, cleanup_started_ns = concurrent_cleanup(
                    records,
                    faulted_replica_id=profile.fault.replica_id,
                    post_end_ns=post_end_ns,
                )
                unexpected_cleanup = []
                for item in cleanup_ledger:
                    item_errors = item.get("cleanup_errors")
                    if isinstance(item_errors, list):
                        cleanup_messages.extend(str(error) for error in item_errors)
                    if item["classification"] == "unexpected_exit":
                        unexpected_cleanup.append(item)
                if unexpected_cleanup:
                    cleanup_messages.append(
                        "unexpected cleanup exits: "
                        + json.dumps(unexpected_cleanup, sort_keys=True)
                    )
        except (OSError, ProfiledFaultRuntimeError) as exc:
            cleanup_messages.append(str(exc))
        try:
            resources.close()
        except (OSError, RuntimeError, ValueError) as exc:
            cleanup_messages.append(f"fault evidence close failed: {exc}")
        for log in log_handles:
            try:
                log.close()
            except OSError as exc:
                cleanup_messages.append(f"process log close failed: {exc}")
        try:
            wait_ports_clear(required_ports(profile))
        except ProfiledFaultRuntimeError as exc:
            cleanup_messages.append(str(exc))
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, RuntimeError, ValueError) as exc:
                cleanup_messages.append(
                    f"could not restore signal handler {signum}: {exc}"
                )
        if cleanup_messages:
            cleanup_error = "; ".join(dict.fromkeys(cleanup_messages))
        if cleanup_error:
            runtime_error = runtime_error or cleanup_error

    finished_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    final_streams: dict[str, list[dict[str, Any]]] = {}
    source_error: str | None = None
    sources: list[dict[str, object]] = []
    try:
        final_streams = event_streams(
            profile,
            run_directory,
            include_manager=True,
            allow_partial=False,
        )
        epoch_path, reputation_path = _write_event_csvs(
            profile, run_directory, final_streams
        )
        if records:
            sources = _source_descriptors(
                profile,
                run_directory,
                records,
                source_instances,
            )
    except (OSError, ProfiledFaultRuntimeError) as exc:
        source_error = str(exc)
        runtime_error = runtime_error or source_error

    manifest: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "kauri_revision": revision,
        "profile": {
            "profile_id": profile.profile_id,
            "sha256": profile.profile_sha256,
        },
        "attempt": 1,
        "attempt_scope": "one_invocation_without_automatic_retry",
        "complete": runtime_error is None,
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "runtime": _runtime_manifest(profile),
        "effective_runtime": effective_runtime(profile),
        "preflight": preflight_result,
        "source_instances": source_instances,
        "sources": sources,
        "process_exits": pre_cleanup_exits,
        "cleanup_ledger": cleanup_ledger,
        "crash_boundary": boundary,
        "crash_marker": crash_marker,
        "measurement_windows": {
            "baseline": {
                "start_ns": baseline_start_ns,
                "end_ns": baseline_end_ns,
            },
            "fault_injection": {
                "start_ns": (
                    crash_marker.get("requested_monotonic_ns") if crash_marker else None
                ),
                "end_ns": post_start_ns,
                "excluded_from_throughput": True,
            },
            "postfault": {
                "start_ns": post_start_ns,
                "end_ns": post_end_ns,
            },
        },
        "runtime_artifacts": [],
        "runtime_error": runtime_error,
    }

    validation: dict[str, object]
    verdict = "INCOMPLETE"
    validation_error: str | None = None
    if runtime_error is not None:
        validation = {
            "schema_version": 1,
            "verdict": "INCOMPLETE",
            "error": runtime_error,
            "run_id": run_id,
        }
    else:
        try:
            if cleanup_started_ns is None:
                raise ProfiledFaultRuntimeError("cleanup timestamp is absent")
            assert_no_successor_activity(
                final_streams,
                run_directory=run_directory,
                cleanup_started_ns=cleanup_started_ns,
                post_end_ns=post_end_ns,
            )
            final_audit = validate_final_streams(
                profile,
                manifest=manifest,
                streams=final_streams,
                crash_marker=crash_marker or {},
                cleanup_started_ns=cleanup_started_ns,
                run_directory=run_directory,
            )
            journal = read_jsonl(run_directory / "raw" / "fault-orchestrator.jsonl")
            replica_streams = {
                source: events
                for source, events in final_streams.items()
                if source.startswith("replica-")
            }
            if crash_marker is None:
                raise ProfiledFaultRuntimeError("crash marker is absent")
            validation = validate_synthetic_run(
                profile,
                manifest=manifest,
                fault_journal=journal,
                streams=replica_streams,
                process_exits=pre_cleanup_exits,
                crash_request_ns=int(crash_marker["requested_monotonic_ns"]),
            )
            baseline_rows = list(final_audit["baseline_rows"])
            post_rows = list(final_audit["postfault_rows"])
            write_throughput_csv(
                run_directory / "throughput.csv",
                [*baseline_rows, *post_rows],
            )
            baseline_mean = statistics.fmean(float(row["tps"]) for row in baseline_rows)
            post_mean = statistics.fmean(float(row["tps"]) for row in post_rows)
            validation.update(
                {
                    "schema_version": 1,
                    "verdict": "PASS",
                    "baseline_mean_tps": baseline_mean,
                    "postfault_mean_tps": post_mean,
                    "throughput_retention": (
                        post_mean / baseline_mean if baseline_mean > 0 else None
                    ),
                    "throughput_retention_claim": "descriptive_only",
                    "recovery_gate": final_audit["recovery_gate"],
                    "measurement_windows": manifest["measurement_windows"],
                    "pre_fault_common_commit": final_audit["pre_fault_common_commit"],
                    "post_fault_common_commit": final_audit["post_fault_common_commit"],
                }
            )
            verdict = "PASS"
        except (
            OSError,
            ValueError,
            ProfiledFaultEvaluationError,
            ProfiledFaultRuntimeError,
        ) as exc:
            verdict = "FAIL"
            validation_error = str(exc)
            validation = {
                "schema_version": 1,
                "verdict": "FAIL",
                "error": validation_error,
                "run_id": run_id,
            }
            if isinstance(exc, RecoveryGateFailure):
                validation["recovery_gate"] = exc.recovery_gate

    try:
        runtime_artifacts = final_artifact_inventory(
            run_directory,
            exclude={"runner-state.json"},
        )
    except (OSError, ProfiledFaultRuntimeError) as exc:
        runtime_error = runtime_error or f"artifact inventory failed: {exc}"
        verdict = "INCOMPLETE"
        validation = {
            "schema_version": 1,
            "verdict": "INCOMPLETE",
            "error": runtime_error,
            "run_id": run_id,
        }

    state.update(
        {
            "phase": "finished",
            "runtime_error": runtime_error,
            "cleanup_error": cleanup_error,
            "validation_error": validation_error,
            "verdict": verdict,
            "interrupted": interrupted,
            "finished_utc": finished_utc,
        }
    )
    replace_json(state_path, state)
    runtime_artifacts.append(
        {
            "kind": "run_artifact",
            "replica_id": None,
            "path": "runner-state.json",
            "size_bytes": state_path.stat().st_size,
            "sha256": sha256_file(state_path),
        }
    )
    runtime_artifacts.sort(key=lambda item: str(item["path"]))
    manifest["complete"] = runtime_error is None
    manifest["runtime_error"] = runtime_error
    manifest["runtime_artifacts"] = runtime_artifacts
    write_json_exclusive(run_directory / "manifest.json", manifest)
    write_json_exclusive(run_directory / "validation.json", validation)
    _seal_and_validate_terminal_run(run_directory, verdict)
    return run_directory, verdict
