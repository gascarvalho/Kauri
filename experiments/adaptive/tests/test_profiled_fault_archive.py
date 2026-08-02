"""Tests for source-blind profiled-fault evidence sealing."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest


def _api():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.profiled_fault_archive"
    )


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def test_create_seal_records_every_file_in_canonical_path_order(
    tmp_path: Path,
) -> None:
    api = _api()
    (tmp_path / "z-last.bin").write_bytes(b"last")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "first.txt").write_bytes(b"first\n")
    (tmp_path / "a-first.json").write_bytes(b"{}\n")

    metadata = api.create_evidence_seal(tmp_path)
    document = json.loads((tmp_path / api.SEAL_FILENAME).read_bytes())
    expected_entries = [
        {
            "path": "a-first.json",
            "sha256": hashlib.sha256(b"{}\n").hexdigest(),
            "size_bytes": 3,
        },
        {
            "path": "nested/first.txt",
            "sha256": hashlib.sha256(b"first\n").hexdigest(),
            "size_bytes": 6,
        },
        {
            "path": "z-last.bin",
            "sha256": hashlib.sha256(b"last").hexdigest(),
            "size_bytes": 4,
        },
    ]
    expected_tree = hashlib.sha256(_canonical_bytes(expected_entries)).hexdigest()

    assert document == {
        "entries": expected_entries,
        "hash_algorithm": "sha256",
        "schema": "kauri.profiled-fault.evidence-seal",
        "schema_version": 1,
        "tree_sha256": expected_tree,
    }
    assert (tmp_path / api.SEAL_FILENAME).read_bytes() == _canonical_bytes(document)
    assert [entry.path for entry in metadata.entries] == [
        "a-first.json",
        "nested/first.txt",
        "z-last.bin",
    ]
    assert metadata.file_count == 3
    assert metadata.total_bytes == 13
    assert metadata.tree_sha256 == expected_tree
    assert metadata.seal_path == tmp_path / api.SEAL_FILENAME
    assert (
        metadata.seal_sha256
        == hashlib.sha256((tmp_path / api.SEAL_FILENAME).read_bytes()).hexdigest()
    )


def test_verify_seal_is_source_blind_and_returns_same_metadata(
    tmp_path: Path,
) -> None:
    api = _api()
    (tmp_path / "raw.log").write_bytes(b"closed evidence\n")

    created = api.create_evidence_seal(tmp_path)
    verified = api.verify_evidence_seal(tmp_path)

    assert verified == created


def test_create_seal_is_exclusive(tmp_path: Path) -> None:
    api = _api()
    (tmp_path / "manifest.json").write_bytes(b"{}")
    api.create_evidence_seal(tmp_path)

    with pytest.raises(api.EvidenceSealError, match="already exists|exclusive"):
        api.create_evidence_seal(tmp_path)


@pytest.mark.parametrize("change", ("extra", "missing", "mutated"))
def test_verify_rejects_membership_and_byte_drift(
    tmp_path: Path,
    change: str,
) -> None:
    api = _api()
    evidence = tmp_path / "throughput.csv"
    evidence.write_bytes(b"bucket,tps\n0,100\n")
    api.create_evidence_seal(tmp_path)

    if change == "extra":
        (tmp_path / "late.log").write_bytes(b"not sealed")
    elif change == "missing":
        evidence.unlink()
    else:
        evidence.write_bytes(b"bucket,tps\n0,999\n")

    with pytest.raises(
        api.EvidenceSealError,
        match="membership|missing|extra|size|hash|mutated",
    ):
        api.verify_evidence_seal(tmp_path)


def test_create_and_verify_reject_symlinks(tmp_path: Path) -> None:
    api = _api()
    outside = tmp_path.parent / "outside-evidence.txt"
    outside.write_bytes(b"outside")
    link = tmp_path / "linked.log"
    link.symlink_to(outside)

    with pytest.raises(api.EvidenceSealError, match="symlink|regular"):
        api.create_evidence_seal(tmp_path)

    link.unlink()
    (tmp_path / "real.log").write_bytes(b"inside")
    api.create_evidence_seal(tmp_path)
    link.symlink_to(outside)

    with pytest.raises(api.EvidenceSealError, match="symlink|regular"):
        api.verify_evidence_seal(tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_create_rejects_non_regular_files(tmp_path: Path) -> None:
    api = _api()
    os.mkfifo(tmp_path / "unfinished-stream")

    with pytest.raises(api.EvidenceSealError, match="non-regular|regular"):
        api.create_evidence_seal(tmp_path)


def test_verify_rejects_noncanonical_or_forged_seal(tmp_path: Path) -> None:
    api = _api()
    (tmp_path / "manifest.json").write_bytes(b"{}")
    api.create_evidence_seal(tmp_path)
    seal = tmp_path / api.SEAL_FILENAME

    document = json.loads(seal.read_bytes())
    document["entries"][0]["path"] = "../manifest.json"
    seal.write_bytes(_canonical_bytes(document))

    with pytest.raises(
        api.EvidenceSealError,
        match="path|relative|canonical|tree",
    ):
        api.verify_evidence_seal(tmp_path)
