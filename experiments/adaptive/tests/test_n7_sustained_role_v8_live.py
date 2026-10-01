from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "n7-path-timeout-quorum/sustained_role_v8_live.py"
spec = importlib.util.spec_from_file_location("w19_v8_live_test", PATH)
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


def prepared(tmp_path, arm):
    build = PATH.parents[3] / "build-adaptive"
    binaries = {"app_binary": build / "examples/hotstuff-app",
                "manager_binary": build / "examples/adaptation-manager",
                "keygen_binary": build / "hotstuff-keygen", "tls_keygen_binary": build / "hotstuff-tls-keygen",
                "e0_helper_binary": build / "examples/n7-epoch0-treefile-digest"}
    if not all(path.is_file() for path in binaries.values()):
        pytest.skip("native preparation binaries absent")
    receipt = tmp_path / "build.json"; receipt.write_bytes(b"fixture\n")
    root = tmp_path / "root"
    subject.inputs.prepare(root, arm=arm, run_id="live-fixture", ports=(18472, 19472, 20472),
        start_ns=1, end_ns=82_000_000_001, binaries=binaries, build_receipt=receipt,
        repository_verifier=lambda _: SimpleNamespace(revision="a" * 40, worktree_clean=True, origin_revision="a" * 40))
    plan, _ = subject.read(root / subject.paths(arm)[0])
    return root, plan


@pytest.mark.parametrize("arm", ["adaptive_e1", "fixed_e0"])
def test_real_generated_credentials_restore_only_redacted_fields(tmp_path, arm):
    root, plan = prepared(tmp_path, arm)
    argv = subject.unseal_manager(root, plan)
    assert "<redacted>" not in argv and "<fingerprinted>" not in argv
    assert subject.base.normalized_manager_argv(argv) == next(row["argv"] for row in plan["processes"] if row["source_id"] == "adaptive-manager")
    assert not (root / "raw").exists()


def test_private_identity_mutation_cannot_spawn(tmp_path):
    root, plan = prepared(tmp_path, "adaptive_e1")
    path = root / "config/issuer-identity.txt"
    private = subject.base._parse_generator_output(path.read_text(), expected_count=1,
        expected_fields=frozenset({"pub", "sec"}), label="fixture")[0]
    path.write_text("pub:" + "02" + "a" * 64 + " sec:" + private["sec"] + "\n")
    with pytest.raises(subject.LiveError, match="trust identity"):
        subject.unseal_manager(root, plan)


def table():
    return "| id | machine | user | mode | duration | start | end |\n| " + subject.BOOKING + " | proteina02 | gascarvalho | EXCLUSIVE | 2 hours | 2026-10-02 00:00 | 2026-10-02 02:00 |\n"


def test_exact_current_booking_uses_lisbon_not_utc_dates():
    row = subject.booking_row(table(), now=datetime(2026, 10, 1, 23, 10, tzinfo=timezone.utc))
    assert row[0] == subject.BOOKING


@pytest.mark.parametrize("now", [datetime(2026, 10, 1, 22, 59, tzinfo=timezone.utc),
                                 datetime(2026, 10, 2, 0, 59, tzinfo=timezone.utc)])
def test_booking_rejects_inactive_or_insufficient_reserve(now):
    with pytest.raises(subject.LiveError, match="inactive"):
        subject.booking_row(table(), now=now)


def test_wrong_event_is_not_booking_capacity():
    with pytest.raises(subject.LiveError, match="absent"):
        subject.booking_row(table().replace(subject.BOOKING, "other"), now=datetime(2026, 10, 1, 23, 10, tzinfo=timezone.utc))


def lifecycle():
    streams = {source: [{"event_type": "process.started", "payload": {"exit_status": None}},
                        {"event_type": "process.stopped", "payload": {"exit_status": None}}] for source in subject.SOURCES}
    cleanup = {"complete": True, "processes": [{"source_id": source, "returncode": 0,
               "termination": "clean-exit"} for source in subject.SOURCES]}
    return streams, cleanup


def test_native_null_status_requires_real_clean_wait_status():
    streams, cleanup = lifecycle()
    subject.validate_lifecycle(streams, cleanup)
    cleanup["processes"][0]["returncode"] = 1
    with pytest.raises(subject.LiveError, match="cleanly"):
        subject.validate_lifecycle(streams, cleanup)


def test_missing_native_stop_rejected():
    streams, cleanup = lifecycle()
    streams["replica-1"].pop()
    with pytest.raises(subject.LiveError, match="lifecycle"):
        subject.validate_lifecycle(streams, cleanup)


def test_preflight_failure_is_preserved_and_never_retried(tmp_path, monkeypatch):
    root = tmp_path / "root"; (root / "runtime").mkdir(parents=True)
    monkeypatch.setattr(subject, "clock", lambda: 1)
    def reject(*args):
        raise subject.LiveError("preflight rejection")
    monkeypatch.setattr(subject, "_execute", reject)
    with pytest.raises(subject.LiveError, match="preflight"):
        subject.execute(root, "adaptive_e1", tmp_path / "approval", "a" * 64)
    assert json.loads((root / subject.ABORT).read_text())["state"] == "ABORTED_NO_RETRY"
    with pytest.raises(subject.LiveError, match="no retry"):
        subject.execute(root, "adaptive_e1", tmp_path / "approval", "a" * 64)


def test_offline_replay_refuses_abort_without_parsing_artifacts(tmp_path):
    (tmp_path / "runtime").mkdir()
    subject.write(tmp_path / subject.ABORT, {"state": "ABORTED_NO_RETRY"})
    with pytest.raises(subject.LiveError, match="aborted"):
        subject.replay(tmp_path, expected_receipt_sha="a" * 64)


def test_monitor_reads_once_and_waits_for_complete_native_lines(tmp_path):
    (tmp_path / "raw").mkdir()
    path = tmp_path / "raw/replica-1.jsonl"
    event = subject.canonical({"event_type": "epoch.activated", "payload": {"epoch_number": 1}})
    path.write_bytes(event[:-1])
    monitor = subject.Monitor(tmp_path)
    assert monitor.poll()["replica-1"] == []
    with path.open("ab") as stream:
        stream.write(b"\n")
    assert len(monitor.poll()["replica-1"]) == 1
    assert len(monitor.poll()["replica-1"]) == 1
    path.write_bytes(b"")
    with pytest.raises(subject.LiveError, match="truncated"):
        monitor.poll()
