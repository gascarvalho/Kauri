"""Deterministic, source-blind sealing for profiled-fault run evidence.

The seal deliberately contains no experiment-specific interpretation.  It binds
the relative path, byte size, and SHA-256 digest of every regular file in a run
directory so that a later verifier needs only the directory and this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

SEAL_FILENAME = "evidence-seal.json"
SCHEMA = "kauri.profiled-fault.evidence-seal"
SCHEMA_VERSION = 1
HASH_ALGORITHM = "sha256"

_BUFFER_SIZE = 1024 * 1024
_ROOT_KEYS = frozenset(
    {
        "entries",
        "hash_algorithm",
        "schema",
        "schema_version",
        "tree_sha256",
    }
)
_ENTRY_KEYS = frozenset({"path", "sha256", "size_bytes"})
_LOWER_HEX = frozenset("0123456789abcdef")


class EvidenceSealError(ValueError):
    """The run directory or its evidence seal is not admissible."""


@dataclass(frozen=True, slots=True)
class EvidenceSealEntry:
    """One sealed regular file, addressed relative to the run directory."""

    path: str
    size_bytes: int
    sha256: str

    def as_document(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class EvidenceSealMetadata:
    """Metadata returned after creating or independently verifying a seal."""

    seal_path: Path
    entries: tuple[EvidenceSealEntry, ...]
    tree_sha256: str
    seal_sha256: str

    @property
    def file_count(self) -> int:
        return len(self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self.entries)


def _error(message: str) -> None:
    raise EvidenceSealError(message)


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _tree_sha256(entries: tuple[EvidenceSealEntry, ...]) -> str:
    document = [entry.as_document() for entry in entries]
    return hashlib.sha256(_canonical_json_bytes(document)).hexdigest()


def _validate_run_directory(run_dir: Path) -> None:
    try:
        info = run_dir.lstat()
    except FileNotFoundError as exc:
        raise EvidenceSealError(f"run directory does not exist: {run_dir}") from exc
    except OSError as exc:
        raise EvidenceSealError(f"cannot inspect run directory: {run_dir}") from exc
    if stat.S_ISLNK(info.st_mode):
        _error(f"run directory must not be a symlink: {run_dir}")
    if not stat.S_ISDIR(info.st_mode):
        _error(f"run directory must be a directory: {run_dir}")


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _open_readonly_no_follow(path: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _hash_regular_file(
    path: Path,
    relative_path: str,
    discovered: os.stat_result,
) -> EvidenceSealEntry:
    try:
        descriptor = _open_readonly_no_follow(path)
    except OSError as exc:
        raise EvidenceSealError(f"cannot open sealed file: {relative_path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _error(f"sealed path is not a regular file: {relative_path}")
        if not _same_file_state(discovered, before):
            _error(f"sealed file changed while being opened: {relative_path}")
        digest = hashlib.sha256()
        byte_count = 0
        while True:
            chunk = os.read(descriptor, _BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            byte_count += len(chunk)
        after = os.fstat(descriptor)
        if not _same_file_state(before, after) or byte_count != after.st_size:
            _error(f"sealed file changed while being hashed: {relative_path}")
        return EvidenceSealEntry(
            path=relative_path,
            size_bytes=byte_count,
            sha256=digest.hexdigest(),
        )
    except OSError as exc:
        raise EvidenceSealError(f"cannot hash sealed file: {relative_path}") from exc
    finally:
        os.close(descriptor)


def _snapshot_regular_files(run_dir: Path) -> tuple[EvidenceSealEntry, ...]:
    discovered_files: list[tuple[str, Path, os.stat_result]] = []
    pending: list[tuple[Path, str]] = [(run_dir, "")]
    while pending:
        directory, relative_directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda child: child.name)
        except OSError as exc:
            label = relative_directory or "."
            raise EvidenceSealError(f"cannot scan run path: {label}") from exc
        for child in children:
            relative_path = (
                f"{relative_directory}/{child.name}"
                if relative_directory
                else child.name
            )
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise EvidenceSealError(
                    f"cannot inspect run path: {relative_path}"
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                _error(f"run evidence contains a symlink: {relative_path}")
            if stat.S_ISDIR(info.st_mode):
                pending.append((Path(child.path), relative_path))
                continue
            if not stat.S_ISREG(info.st_mode):
                _error(f"run evidence contains a non-regular file: {relative_path}")
            if relative_path == SEAL_FILENAME:
                continue
            discovered_files.append((relative_path, Path(child.path), info))

    entries = tuple(
        _hash_regular_file(path, relative_path, info)
        for relative_path, path, info in sorted(
            discovered_files,
            key=lambda item: item[0],
        )
    )
    paths = tuple(entry.path for entry in entries)
    if len(paths) != len(set(paths)):
        _error("run evidence contains duplicate canonical paths")
    return entries


def _seal_document(
    entries: tuple[EvidenceSealEntry, ...],
    tree_sha256: str,
) -> dict[str, object]:
    return {
        "entries": [entry.as_document() for entry in entries],
        "hash_algorithm": HASH_ALGORITHM,
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "tree_sha256": tree_sha256,
    }


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise EvidenceSealError(
            f"evidence seal already exists; creation is exclusive: {path}"
        ) from exc
    except OSError as exc:
        raise EvidenceSealError(f"cannot create evidence seal: {path}") from exc
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    except OSError as exc:
        raise EvidenceSealError(f"cannot write evidence seal: {path}") from exc
    finally:
        os.close(descriptor)


def create_evidence_seal(run_dir: Path) -> EvidenceSealMetadata:
    """Create ``evidence-seal.json`` once, after all run files are closed.

    Creation is exclusive and never replaces an existing seal.  The returned
    metadata can be copied into an external archive index without reopening
    or interpreting any experiment artifact.
    """

    root = Path(run_dir)
    _validate_run_directory(root)
    seal_path = root / SEAL_FILENAME
    try:
        seal_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise EvidenceSealError(f"cannot inspect evidence seal: {seal_path}") from exc
    else:
        _error(f"evidence seal already exists; creation is exclusive: {seal_path}")

    entries = _snapshot_regular_files(root)
    tree_sha256 = _tree_sha256(entries)
    payload = _canonical_json_bytes(_seal_document(entries, tree_sha256))
    _write_exclusive(seal_path, payload)
    return EvidenceSealMetadata(
        seal_path=seal_path,
        entries=entries,
        tree_sha256=tree_sha256,
        seal_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _error(f"evidence seal contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    _error(f"evidence seal contains a non-canonical JSON constant: {value}")


def _read_seal_bytes(seal_path: Path) -> bytes:
    try:
        discovered = seal_path.lstat()
    except FileNotFoundError as exc:
        raise EvidenceSealError(f"evidence seal is missing: {seal_path}") from exc
    except OSError as exc:
        raise EvidenceSealError(f"cannot inspect evidence seal: {seal_path}") from exc
    if stat.S_ISLNK(discovered.st_mode):
        _error(f"evidence seal must not be a symlink: {seal_path}")
    if not stat.S_ISREG(discovered.st_mode):
        _error(f"evidence seal must be a regular file: {seal_path}")
    try:
        descriptor = _open_readonly_no_follow(seal_path)
    except OSError as exc:
        raise EvidenceSealError(f"cannot open evidence seal: {seal_path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not _same_file_state(discovered, before):
            _error(f"evidence seal changed while being opened: {seal_path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, _BUFFER_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        payload = b"".join(chunks)
        if not _same_file_state(before, after) or len(payload) != after.st_size:
            _error(f"evidence seal changed while being read: {seal_path}")
        return payload
    except OSError as exc:
        raise EvidenceSealError(f"cannot read evidence seal: {seal_path}") from exc
    finally:
        os.close(descriptor)


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _LOWER_HEX for character in value)
    )


def _validate_relative_posix_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _error("sealed path must be a non-empty relative POSIX path")
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or parsed.as_posix() != value
        or any(part in ("", ".", "..") for part in parsed.parts)
        or value == SEAL_FILENAME
    ):
        _error(f"sealed path is not canonical and relative: {value}")
    return value


def _parse_seal(payload: bytes) -> tuple[tuple[EvidenceSealEntry, ...], str]:
    try:
        decoded = payload.decode("utf-8")
        document = json.loads(
            decoded,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceSealError("evidence seal must be valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or set(document) != _ROOT_KEYS:
        _error("evidence seal does not match the canonical root schema")
    if payload != _canonical_json_bytes(document):
        _error("evidence seal JSON encoding is not canonical")
    if document["schema"] != SCHEMA:
        _error("evidence seal schema is unsupported")
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != SCHEMA_VERSION
    ):
        _error("evidence seal schema version is unsupported")
    if document["hash_algorithm"] != HASH_ALGORITHM:
        _error("evidence seal hash algorithm is unsupported")
    raw_entries = document["entries"]
    if not isinstance(raw_entries, list):
        _error("evidence seal entries must be an array")

    entries: list[EvidenceSealEntry] = []
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict) or set(raw_entry) != _ENTRY_KEYS:
            _error(f"evidence seal entry {index} does not match the canonical schema")
        path = _validate_relative_posix_path(raw_entry["path"])
        size_bytes = raw_entry["size_bytes"]
        if type(size_bytes) is not int or size_bytes < 0:
            _error(f"sealed size must be a non-negative integer: {path}")
        digest = raw_entry["sha256"]
        if not _valid_sha256(digest):
            _error(f"sealed SHA-256 is not canonical: {path}")
        entries.append(
            EvidenceSealEntry(
                path=path,
                size_bytes=size_bytes,
                sha256=digest,
            )
        )
    paths = [entry.path for entry in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        _error("evidence seal paths must be sorted and unique")

    tree_sha256 = document["tree_sha256"]
    if not _valid_sha256(tree_sha256):
        _error("evidence seal tree SHA-256 is not canonical")
    canonical_entries = tuple(entries)
    if _tree_sha256(canonical_entries) != tree_sha256:
        _error("evidence seal tree digest does not match its canonical entries")
    return canonical_entries, tree_sha256


def verify_evidence_seal(run_dir: Path) -> EvidenceSealMetadata:
    """Verify sealed evidence without consulting its profile or source runtime.

    Verification is fail-closed for changed bytes, size drift, missing or extra
    files, symlinks, non-regular files, malformed metadata, and non-canonical
    seal encodings.
    """

    root = Path(run_dir)
    _validate_run_directory(root)
    seal_path = root / SEAL_FILENAME
    payload = _read_seal_bytes(seal_path)
    expected_entries, tree_sha256 = _parse_seal(payload)
    actual_entries = _snapshot_regular_files(root)

    expected_paths = {entry.path for entry in expected_entries}
    actual_paths = {entry.path for entry in actual_entries}
    if expected_paths != actual_paths:
        missing = sorted(expected_paths - actual_paths)
        extra = sorted(actual_paths - expected_paths)
        _error(
            "sealed file membership changed" f"; missing={missing!r}; extra={extra!r}"
        )
    for expected, actual in zip(expected_entries, actual_entries, strict=True):
        if expected.size_bytes != actual.size_bytes:
            _error(f"sealed file size changed: {expected.path}")
        if expected.sha256 != actual.sha256:
            _error(f"sealed file hash changed: {expected.path}")
    if _tree_sha256(actual_entries) != tree_sha256:
        _error("current evidence tree digest differs from the seal")

    return EvidenceSealMetadata(
        seal_path=seal_path,
        entries=expected_entries,
        tree_sha256=tree_sha256,
        seal_sha256=hashlib.sha256(payload).hexdigest(),
    )


__all__ = (
    "EvidenceSealEntry",
    "EvidenceSealError",
    "EvidenceSealMetadata",
    "HASH_ALGORITHM",
    "SCHEMA",
    "SCHEMA_VERSION",
    "SEAL_FILENAME",
    "create_evidence_seal",
    "verify_evidence_seal",
)
