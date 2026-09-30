from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "sustained_role_fixed_e0_launcher.py"
spec = importlib.util.spec_from_file_location("w19_launcher", PATH); assert spec and spec.loader
subject = importlib.util.module_from_spec(spec); spec.loader.exec_module(subject)

def _canon(value): return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
def _sha(value): return hashlib.sha256(_canon(value)).hexdigest()

def test_scheduled_manager_binds_source_e0_identity_and_rejects_legacy_control():
    plan = {"commands": {"manager": {"argv": ["manager", "--structured-event-run-id", "r"]}}, "scheduled_window": {"start_monotonic_ns": 10, "end_monotonic_ns": 60_000_000_010}, "native_fault_schedule": {"descriptor": {"sha256": "a" * 64}}}
    argv = subject._scheduled_manager(plan, e0_digest="b" * 64)
    assert "--scheduled-fixed-e0-control" in argv
    assert argv[argv.index("--scheduled-fixed-e0-epoch-zero-digest") + 1] == "b" * 64
    plan["commands"]["manager"]["argv"].append("--fault-window-arm-control-only")
    with pytest.raises(subject.LaunchError, match="legacy"):
        subject._scheduled_manager(plan, e0_digest="b" * 64)


def test_plan_digest_matches_sustained_role_producer_convention():
    plan = {"schema_version": 1, "kind": "kauri-n7-sustained-role-execution-plan-v1"}
    plan["plan_sha256"] = hashlib.sha256(_canon(plan)).hexdigest()
    assert subject._plan_digest(plan) == plan["plan_sha256"]


def test_approved_launch_inputs_are_rehashed_before_spawn(tmp_path: Path):
    root = tmp_path / "run"; root.mkdir()
    artifacts = {}
    for name in ("manager", "app", "e0.tree", "main.conf", "native.py", "selection.json",
                 *[f"replica-{i}.conf" for i in range(7)]):
        path = root / name; path.write_text(name)
        artifacts[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    manager_argv = [artifacts["manager"]["path"], "--structured-event-run-id", "r"]
    replica_argv = [artifacts["app"]["path"], "--conf", artifacts["main.conf"]["path"]]
    plan = {
        "commands": {
            "manager": {"argv": manager_argv, "sha256": subject._argv_digest(manager_argv),
                        "executable_sha256": artifacts["manager"]["sha256"]},
            "replicas": [
                {"replica_id": i, "argv": replica_argv,
                 "sha256": subject._argv_digest(replica_argv),
                 "executable_sha256": artifacts["app"]["sha256"]}
                for i in range(7)
            ],
        },
        "configuration": {"main": artifacts["main.conf"],
                          "replicas": [artifacts[f"replica-{i}.conf"] for i in range(7)]},
        "epoch0": {"tree": artifacts["e0.tree"]},
        "native_fault_schedule": {"descriptor": artifacts["native.py"]},
        "manager_selection_policy": {"descriptor": artifacts["selection.json"]},
    }
    subject._verify_approved_launch_inputs(root, plan)
    (root / "replica-6.conf").write_text("mutated")
    with pytest.raises(subject.LaunchError, match="replica-6 config differs"):
        subject._verify_approved_launch_inputs(root, plan)
    (root / "replica-6.conf").write_text("replica-6.conf")
    plan["commands"]["manager"]["argv"].append("--unapproved")
    with pytest.raises(subject.LaunchError, match="manager argv differs"):
        subject._verify_approved_launch_inputs(root, plan)


def test_authorized_launch_creates_fresh_log_and_raw_sinks_once(tmp_path: Path):
    root = tmp_path / "run"; root.mkdir()
    assert not (root / "logs").exists() and not (root / "raw").exists()
    subject._prepare_exclusive_output_dirs(root)
    assert (root / "logs").is_dir() and (root / "raw").is_dir()
    with pytest.raises(subject.LaunchError, match="already exists"):
        subject._prepare_exclusive_output_dirs(root)

def test_exact_external_approval_cannot_be_reused_for_another_plan(tmp_path: Path):
    root = tmp_path / "run"; (root / "runtime").mkdir(parents=True)
    plan = {"plan_sha256": "a" * 64}; request = {"request": "one"}; request_bytes = _canon(request)
    approval = {"schema_version": 1, "kind": subject.AUTH_KIND, "request_sha256": hashlib.sha256(request_bytes).hexdigest(), "plan_sha256": "a" * 64, "approval_reference": "operator", "approved_utc": "2026-09-30T12:00:00Z", "no_retry": True}
    external = tmp_path / "approval.json"; external.write_bytes(_canon(approval))
    assert subject._exact_approval(root, external, plan, request_bytes)["no_retry"] is True
    with pytest.raises(subject.LaunchError, match="refusing"):
        subject._exact_approval(root, external, plan, request_bytes)
    assert (root / subject.APPROVAL).is_file()


def test_external_approval_requires_parseable_utc_timestamp(tmp_path: Path):
    root = tmp_path / "run"; (root / "runtime").mkdir(parents=True)
    plan = {"plan_sha256": "a" * 64}; request_bytes = _canon({"request": "one"})
    approval = {"schema_version": 1, "kind": subject.AUTH_KIND,
                "request_sha256": hashlib.sha256(request_bytes).hexdigest(), "plan_sha256": "a" * 64,
                "approval_reference": "operator", "approved_utc": "not-a-utcZ", "no_retry": True}
    external = tmp_path / "approval.json"; external.write_bytes(_canon(approval))
    with pytest.raises(subject.LaunchError, match="not exact"):
        subject._exact_approval(root, external, plan, request_bytes)

def test_prearm_requires_a_common_e0_commit_from_every_replica():
    streams = {f"replica-{i}": [{"event_type": "block.commit_observed", "source_monotonic_ns": 9, "payload": {"block_height": 7, "block_hash": "b" * 64}}] for i in range(7)}
    streams["replica-2"].append({"event_type": "block.committed", "source_monotonic_ns": 8, "payload": {"block_height": 7, "block_hash": "b" * 64, "designated_observer": True, "decision_proof": {"epoch_number": 0, "tree_id": 4, "epoch_digest": "d" * 64, "block_hash": "b" * 64}}})
    assert subject._all_seven_e0_common(streams, 10)
    streams["replica-6"][0]["payload"]["block_hash"] = "other"
    assert not subject._all_seven_e0_common(streams, 10)


def test_prearm_rejects_non_e0_or_post_start_native_commits():
    streams = {f"replica-{i}": [{"event_type": "block.commit_observed", "source_monotonic_ns": 9, "payload": {"block_height": 7, "block_hash": "b" * 64}}] for i in range(7)}
    streams["replica-2"].append({"event_type": "block.committed", "source_monotonic_ns": 10, "payload": {"block_height": 7, "block_hash": "b" * 64, "designated_observer": True, "decision_proof": {"epoch_number": 0, "block_hash": "b" * 64}}})
    assert not subject._all_seven_e0_common(streams, 10)
    streams["replica-2"][-1]["source_monotonic_ns"] = 9
    streams["replica-2"][-1]["payload"]["decision_proof"]["epoch_number"] = 1
    assert not subject._all_seven_e0_common(streams, 10)


def test_source_e0_and_clean_exit_proofs_fail_closed(tmp_path: Path):
    root = tmp_path / "run"; (root / "runtime").mkdir(parents=True)
    identity = {"state": "DERIVED_READ_ONLY", "epoch_number": 0, "epoch_digest": "c" * 64}
    (root / "runtime/e0-identity-receipt.json").write_bytes(_canon(identity))
    assert subject._source_e0(root)[0] == "c" * 64
    descriptor = subject._descriptor(root, root / "runtime/e0-identity-receipt.json")
    assert descriptor == {"path": "runtime/e0-identity-receipt.json", "sha256": hashlib.sha256(_canon(identity)).hexdigest()}
    clean = {"complete": True, "processes": [{"source_id": name, "returncode": 0} for name in ("adaptive-manager", *[f"replica-{i}" for i in range(7)])]}
    assert subject._clean_exit_codes(clean)["replica-1"] == 0
    clean["processes"][0]["returncode"] = 1
    with pytest.raises(subject.LaunchError, match="exit cleanly"):
        subject._clean_exit_codes(clean)


def test_e0_helper_and_fixed_manager_terminal_are_independently_bound(tmp_path: Path):
    root = tmp_path / "run"; (root / "runtime").mkdir(parents=True); (root / "raw").mkdir()
    helper = tmp_path / "e0-helper"; helper.write_text("helper\n"); helper.chmod(0o700)
    identity = {
        "state": "DERIVED_READ_ONLY", "epoch_number": 0, "epoch_digest": "c" * 64,
        "helper_binary": str(helper), "helper_binary_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
    }
    identity_path = root / "runtime/e0-identity-receipt.json"; identity_path.write_bytes(_canon(identity))
    assert subject._e0_identity_helper(root, identity_path) == helper.resolve()
    payload = {"run_id": "r", "profile_sha256": "a" * 64, "epoch_zero_digest": "c" * 64,
               "window_start_monotonic_ns": 100, "window_end_monotonic_ns": 200}
    (root / "raw/adaptive-manager.jsonl").write_text(json.dumps({"event_type": "scheduled_fixed_e0_control.terminal", "payload": payload}) + "\n")
    subject._fixed_manager_terminal(root, run_id="r", profile_sha256="a" * 64,
                                    e0_digest="c" * 64, start=100, end=200)
    payload["epoch_zero_digest"] = "d" * 64
    (root / "raw/adaptive-manager.jsonl").write_text(json.dumps({"event_type": "scheduled_fixed_e0_control.terminal", "payload": payload}) + "\n")
    with pytest.raises(subject.LaunchError, match="terminal"):
        subject._fixed_manager_terminal(root, run_id="r", profile_sha256="a" * 64,
                                        e0_digest="c" * 64, start=100, end=200)


def test_fixed_manager_waits_for_native_terminal_and_clean_exit(tmp_path: Path):
    root = tmp_path / "run"; (root / "raw").mkdir(parents=True)
    path = root / "raw/adaptive-manager.jsonl"
    path.write_text("")
    payload = {"run_id": "r", "profile_sha256": "a" * 64,
               "epoch_zero_digest": "c" * 64,
               "window_start_monotonic_ns": 100, "window_end_monotonic_ns": 200}
    class Process:
        status = None
        def poll(self): return self.status
    process = Process()
    record = type("Record", (), {"process": process})()
    ticks = [0.0]
    def pause(_seconds):
        ticks[0] += 0.05
        path.write_text(json.dumps({"event_type": "scheduled_fixed_e0_control.terminal",
                                    "payload": payload}) + "\n")
        process.status = 0
    subject._await_fixed_manager_completion(
        root, record, run_id="r", profile_sha256="a" * 64,
        e0_digest="c" * 64, start=100, end=200, deadline=10.0,
        monotonic=lambda: ticks[0], sleep=pause,
    )
    path.write_text("")
    process.status = None
    with pytest.raises(subject.LaunchError, match="did not seal terminal"):
        subject._await_fixed_manager_completion(
            root, record, run_id="r", profile_sha256="a" * 64,
            e0_digest="c" * 64, start=100, end=200, deadline=ticks[0],
            monotonic=lambda: ticks[0], sleep=lambda _: None,
        )


def test_production_cleanup_reaps_eight_fake_local_processes(tmp_path: Path):
    """Exercise the real process-group cleanup adapter, never a Kauri node."""
    root = tmp_path / "run"; (root / "runtime").mkdir(parents=True); (root / "logs").mkdir()
    plan = {"commands": {"manager": {"argv": ["/usr/bin/true", "--structured-event-run-id", "fake-local"]}}}
    (root / subject.PLAN).write_bytes(_canon(plan))
    records = []
    for source in ("adaptive-manager", *[f"replica-{i}" for i in range(7)]):
        process = subprocess.Popen(["/usr/bin/true"], start_new_session=True)
        log_handle = (root / "logs" / f"{source}.log").open("wb")
        records.append(type("Record", (), {"name": source, "pid": process.pid, "pgid": process.pid, "process": process, "log_handle": log_handle})())
    # The established helper receives the real subprocess records and asserts
    # all eight process groups are reaped with return code zero.
    receipt = subject._production_cleanup(root, records)
    assert receipt["complete"] is True
    assert [row["source_id"] for row in receipt["processes"]] == ["adaptive-manager", *[f"replica-{i}" for i in range(7)]]
