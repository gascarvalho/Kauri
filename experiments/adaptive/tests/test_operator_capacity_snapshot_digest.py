"""CLI contract for the native operator-capacity snapshot digest helper."""
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[3]
BIN = ROOT / "build-adaptive/examples/operator-capacity-snapshot-digest"


def test_operator_capacity_snapshot_digest_accepts_native_encoded_fixture(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture.snapshot"
    encoded = subprocess.run((str(BIN), "--emit-test-fixture"),
                             capture_output=True, check=True)
    fixture.write_bytes(encoded.stdout)
    result = subprocess.run((str(BIN), str(fixture)), text=True,
                            capture_output=True, check=True)
    assert len(result.stdout.strip()) == 64
    assert set(result.stdout.strip()) <= set("0123456789abcdef")
    n31 = subprocess.run((str(BIN), "--validate-n31", "0" * 64, str(fixture)),
                         text=True, capture_output=True, check=True)
    assert n31.stdout == result.stdout
    assert subprocess.run((str(BIN), "--validate-n31", "f" * 64,
                           str(fixture)), capture_output=True).returncode != 0

    one_label = tmp_path / "one-label.snapshot"
    one_label.write_bytes(subprocess.run(
        (str(BIN), "--emit-test-fixture-one-label"),
        capture_output=True, check=True).stdout)
    assert subprocess.run((str(BIN), str(one_label)), capture_output=True).returncode == 0
    assert subprocess.run((str(BIN), "--validate-n31", "0" * 64,
                           str(one_label)), capture_output=True).returncode != 0

    swapped = tmp_path / "swapped-labels.snapshot"
    swapped.write_bytes(subprocess.run(
        (str(BIN), "--emit-test-fixture-swapped-labels"),
        capture_output=True, check=True).stdout)
    assert subprocess.run((str(BIN), str(swapped)), capture_output=True).returncode == 0
    assert subprocess.run((str(BIN), "--validate-n31", "0" * 64,
                           str(swapped)), capture_output=True).returncode != 0


def test_operator_capacity_snapshot_digest_rejects_malformed_and_oversized(
    tmp_path: Path,
) -> None:
    malformed = tmp_path / "malformed.snapshot"
    malformed.write_bytes(b"not-a-kauri-snapshot")
    assert subprocess.run((str(BIN), str(malformed)), capture_output=True).returncode != 0

    oversized = tmp_path / "oversized.snapshot"
    oversized.write_bytes(b"x" * (16 * 1024 + 1))
    assert subprocess.run((str(BIN), str(oversized)), capture_output=True).returncode != 0
