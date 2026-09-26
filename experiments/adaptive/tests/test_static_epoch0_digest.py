"""Native W16 epoch-zero digest helper contract."""
from __future__ import annotations
import subprocess
from pathlib import Path
import pytest

from experiments.adaptive.kauri_experiment.static_topology_n31 import build_schedule, render_treegen_bytes

ROOT = Path(__file__).parents[3]
BIN = ROOT / "build-adaptive/examples/static-epoch0-digest"

def test_static_digest_is_deterministic_and_arm_sensitive(tmp_path: Path) -> None:
    outputs = []
    for arm in ("slow-roots", "fast-roots"):
        path = tmp_path / f"{arm}.conf"; path.write_bytes(render_treegen_bytes(build_schedule(arm)))
        result = subprocess.run((str(BIN), arm, str(path)), text=True, capture_output=True, check=True)
        outputs.append(result.stdout.strip())
    assert outputs[0] == "827e7626c74f8d815bca6ae5cbe10e312bc4f00f287e67d41277b8d689b21c0f"
    assert len(outputs[1]) == 64 and outputs[0] != outputs[1]

def test_static_digest_rejects_noncanonical_membership(tmp_path: Path) -> None:
    path = tmp_path / "bad.conf"; path.write_text("fan:5 pipe:2 " + " ".join(["00"] * 31) + "\n", encoding="ascii")
    result = subprocess.run((str(BIN), "slow-roots", str(path)), text=True, capture_output=True)
    assert result.returncode == 2

def test_static_digest_rejects_wrong_arm_duplicate_and_overlong(tmp_path: Path) -> None:
    good = tmp_path / "good.conf"; good.write_bytes(render_treegen_bytes(build_schedule("slow-roots")))
    assert subprocess.run((str(BIN), "fast-roots", str(good)), capture_output=True).returncode == 2
    duplicate = tmp_path / "duplicate.conf"; duplicate.write_bytes(good.read_bytes().splitlines()[0] + b"\n")
    assert subprocess.run((str(BIN), "slow-roots", str(duplicate)), capture_output=True).returncode == 2
    overlong = tmp_path / "overlong.conf"; overlong.write_bytes(good.read_bytes() + b"x" * 8193)
    assert subprocess.run((str(BIN), "slow-roots", str(overlong)), capture_output=True).returncode == 2
