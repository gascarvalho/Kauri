from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[1] / "n7-path-timeout-quorum"
sys.path.insert(0, str(HERE))
try:
    spec = importlib.util.spec_from_file_location("w19_v8_build_test", HERE / "sustained_role_v8_build.py")
    subject = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(subject)
finally:
    sys.path.remove(str(HERE))


def test_clean_build_command_matches_existing_receipt_contract():
    live = sys.modules["sustained_role_v8_live"]
    assert subject.BUILD == list(live.child.build_binding.operator._BUILD_COMMAND)
    assert "-DCMAKE_BUILD_TYPE=Release" in subject.CONFIGURE
    assert "-DHOTSTUFF_TWO_STEP=OFF" in subject.CONFIGURE


def test_wrong_source_cannot_create_build_output(tmp_path, monkeypatch):
    monkeypatch.setattr(subject.base, "verify_repository_state", lambda _: SimpleNamespace(revision="a" * 40))
    with pytest.raises(subject.LiveError, match="source"):
        subject.run(tmp_path / "fresh", "b" * 40)
    assert not (tmp_path / "fresh").exists()


def test_absent_booking_cannot_create_output_or_start_cmake(tmp_path, monkeypatch):
    monkeypatch.setattr(subject.base, "verify_repository_state", lambda _: SimpleNamespace(revision="a" * 40))
    monkeypatch.setattr(subject.socket, "gethostname", lambda: "proteina02")
    monkeypatch.setattr(subject, "output", lambda _: "| id | machine | user | mode | duration | start | end |\n")
    with pytest.raises(subject.LiveError, match="booking"):
        subject.run(tmp_path / "fresh", "a" * 40)
    assert not (tmp_path / "fresh").exists()
