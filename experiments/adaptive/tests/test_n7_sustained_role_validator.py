from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "sustained_role_validator.py"
_W19_ISSUER_PUBLIC_KEY = "02" + "0" * 64
_W19_ISSUER_ID = 1
spec = importlib.util.spec_from_file_location("n7_sustained_role_validator_test", PATH)
assert spec and spec.loader
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


def _canonical(value):
    return subject._canonical(value)


def _write(root: Path, relative: str, value: bytes) -> dict[str, str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return {"path": relative, "sha256": hashlib.sha256(value).hexdigest()}


def _event(source: str, sequence: int, timestamp: int, event_type: str, payload: dict) -> dict:
    return {"event_schema_version": 1, "run_id": "sustained-run", "source_kind": "adaptation_manager" if source == "adaptive-manager" else "replica", "source_id": source, "source_instance": source + "-instance", "source_sequence": sequence, "source_monotonic_ns": timestamp, "event_type": event_type, "payload": payload}


def _jsonl(events: list[dict]) -> bytes:
    return b"".join(json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n" for event in events)


def _accepted_fixed_bundle(root: Path) -> Path:
    tree_raw = (
        b"fan:2 pipe:2 0 2 3 1 4 5 6\n"
        b"fan:2 pipe:2 1 2 3 4 5 6 0\n"
        b"fan:2 pipe:2 2 3 4 0 1 5 6\n"
        b"fan:2 pipe:2 3 4 5 6 0 1 2\n"
        b"fan:2 pipe:2 4 1 5 0 2 3 6\n"
        b"fan:2 pipe:2 5 1 6 0 2 3 4\n"
        b"fan:2 pipe:2 6 1 0 2 3 4 5\n"
    )
    e0 = subject._source_adaptive_v2_epoch_zero_digest(tree_raw)
    baseline_block = "c" * 64; block = "b" * 64; profile = b"native profile\n"; start = 10; end = 60_000_000_010
    scheduled_window = {
        "start_monotonic_ns": start, "end_monotonic_ns": end,
        "argv_pinned_before_launch": True,
        "attestation": {
            "must_be_written": "after_prearm_all_seven_e0_common_commit_before_scheduled_start",
            "is_not": "an_arm_or_gate",
        },
    }
    _write(root, "config/epoch0.tree", tree_raw)
    helper_raw = ("#!/bin/sh\nprintf '%s\\n' " + e0 + "\n").encode("ascii")
    helper = root / "runtime/e0-helper"
    helper_descriptor = _write(root, "runtime/e0-helper", helper_raw)
    e0_receipt = {"schema_version": 1, "state": "DERIVED_READ_ONLY", "epoch_number": 0,
                  "epoch_digest": e0, "tree_file": "config/epoch0.tree",
                  "tree_file_sha256": hashlib.sha256(tree_raw).hexdigest(),
                  "helper_binary": str(helper.resolve()),
                  "helper_binary_sha256": helper_descriptor["sha256"],
                  "argv": [str(helper.resolve()), str((root / "config/epoch0.tree").resolve())]}
    manager_argv = ["manager", "--structured-event-source-instance", "adaptive-manager-instance",
                    "--structured-event-run-id", "sustained-run"]
    replica_argv = ["app", "--structured-event-source-instance"]
    replica_configs = [(
        f"idx = {replica}\n"
        "structured-event-run-id = sustained-run\n"
        f"structured-event-source-instance = replica-{replica}-instance\n"
        "structured-event-commit-observer-id = replica-2\n"
        "structured-event-commit-observer-instance = replica-2-instance\n"
    ).encode() for replica in range(7)]
    profile_descriptor = {"path": "runtime/profile.py", "sha256": hashlib.sha256(profile).hexdigest()}
    selection_descriptor = {"path": "runtime/selection.json", "sha256": hashlib.sha256(b"selection\n").hexdigest()}
    tree_descriptor = {"path": "config/epoch0.tree", "sha256": hashlib.sha256(tree_raw).hexdigest()}
    main_descriptor = {"path": "runtime/main.conf", "sha256": hashlib.sha256(b"config\n").hexdigest()}
    plan = {"schema_version": 1, "comparison": {"arm": "fixed_e0"},
            "repository_revision": "c" * 40, "no_retry": True,
            "commands": {
                "manager": {"argv": manager_argv,
                            "sha256": hashlib.sha256(_canonical({"schema_version": 1, "argv": manager_argv})).hexdigest(),
                            "executable_sha256": hashlib.sha256(b"manager").hexdigest()},
                "replicas": [
                    {"replica_id": replica, "argv": replica_argv + [f"replica-{replica}-instance"],
                     "sha256": hashlib.sha256(_canonical({"schema_version": 1, "argv": replica_argv + [f"replica-{replica}-instance"]})).hexdigest(),
                     "executable_sha256": hashlib.sha256(b"app").hexdigest()}
                    for replica in range(7)
                ],
            },
            "configuration": {"main": main_descriptor,
                              "replicas": [{"path": f"runtime/replica-{replica}.conf", "sha256": hashlib.sha256(config).hexdigest()}
                                           for replica, config in enumerate(replica_configs)]},
            "epoch0": {"tree": tree_descriptor},
            "native_fault_schedule": {"descriptor": profile_descriptor},
            "manager_selection_policy": {"descriptor": selection_descriptor},
            "scheduled_window": scheduled_window}
    plan["plan_sha256"] = hashlib.sha256(_canonical(plan)).hexdigest()
    request = {"schema_version": 1, "kind": "kauri-n7-sustained-role-execution-authorization-request-v1",
               "execution_plan_sha256": plan["plan_sha256"], "repository_revision": "c" * 40,
               "arm": "fixed_e0", "scheduled_window": scheduled_window,
               "hard_timeout_seconds": 180, "no_retry": True,
               "claim_eligible": False, "figure_eligible": False}
    approval = {"schema_version": 1, "kind": "kauri-n7-sustained-role-fixed-e0-launch-authorization-v1",
                "request_sha256": hashlib.sha256(_canonical(request)).hexdigest(),
                "plan_sha256": plan["plan_sha256"], "approval_reference": "test approval",
                "approved_utc": "2026-09-30T01:00:00Z", "no_retry": True}
    opportunity = {"actor": 1, "proposal": {"epoch_number": 0, "tree_id": 4, "epoch_digest": e0, "block_hash": block}, "physical_role": "internal", "parent_replica": 4, "authenticated_proposal_source_replica": 4, "expected_message_type": "aggregate_relay", "cohort": "hard", "diagnostic_window": "w", "window_start_monotonic_ns": 1, "window_end_monotonic_ns": 100, "decision_monotonic_ns": 10, "contribution_ordinal": 0, "role_contribution_ordinal": 0, "scheduled_action": "omit_aggregate", "responsive_omission_period": 0, "fault_threshold": 2, "hard_actor_count": 1, "responsive_degraded_actor_count": 0, "fault_mode": "role_scoped_persistent_selected_omission_v1", "view_generation": 0}
    baseline_commit = {"block_height": 1, "block_hash": baseline_block, "parent_hash": None, "transaction_count": 1, "designated_observer": True, "decision_proof": {"epoch_number": 0, "tree_id": 4, "epoch_digest": e0, "block_hash": baseline_block}, "view_generation": 0, "commit_batch_index": 0}
    baseline_observed = {"block_height": 1, "block_hash": baseline_block, "parent_hash": None, "transaction_count": 1, "commit_batch_index": 0}
    commit = {"block_height": 2, "block_hash": block, "parent_hash": baseline_block, "transaction_count": 1, "designated_observer": True, "decision_proof": {"epoch_number": 0, "tree_id": 4, "epoch_digest": e0, "block_hash": block}, "view_generation": 0, "commit_batch_index": 0}
    observed = {"block_height": 2, "block_hash": block, "parent_hash": baseline_block, "transaction_count": 1, "commit_batch_index": 0}
    artifacts = {
        "profile": _write(root, "runtime/profile.py", profile), "epoch0_tree": _write(root, "runtime/e0.tree", tree_raw), "main_config": _write(root, "runtime/main.conf", b"config\n"),
        "execution_plan": _write(root, "runtime/sustained-role-execution-plan.json", _canonical(plan)), "authorization_request": _write(root, "runtime/request.json", _canonical(request)), "approved_authorization": _write(root, "runtime/approval.json", _canonical(approval)),
        "e0_identity_receipt": _write(root, "runtime/e0-identity-receipt.json", _canonical(e0_receipt)),
        "e0_identity_helper": helper_descriptor,
        "finalization_receipt": _write(root, "runtime/final.json", b"{}\n"), "fault_window_attestation": _write(root, "runtime/attestation.json", _canonical({"schema_version": 1, "kind": "kauri-n7-sustained-role-prearm-v1", "run_id": "sustained-run", "epoch_zero_digest": e0, "prearm_monotonic_ns": 9, "scheduled_start_monotonic_ns": start, "no_retry": True})), "cleanup": _write(root, "runtime/cleanup.json", _canonical({"schema_version": 1, "run_id": "sustained-run", "complete": True, "processes": [{"source_id": source, "pid": 1, "pgid": 1, "returncode": 0, "termination": "clean-exit"} for source in ["adaptive-manager", *[f"replica-{i}" for i in range(7)]]]})),
        "manager_events": _write(root, "raw/adaptive-manager.jsonl", _jsonl([_event("adaptive-manager", 1, 10, "scheduled_fixed_e0_control.observation", {"run_id": "sustained-run", "profile_sha256": hashlib.sha256(profile).hexdigest(), "epoch_zero_digest": e0, "window_start_monotonic_ns": start, "window_end_monotonic_ns": end}), _event("adaptive-manager", 2, 20_000_000_010, "scheduled_fixed_e0_control.observation", {"run_id": "sustained-run", "profile_sha256": hashlib.sha256(profile).hexdigest(), "epoch_zero_digest": e0, "window_start_monotonic_ns": start, "window_end_monotonic_ns": end}), _event("adaptive-manager", 3, end, "scheduled_fixed_e0_control.terminal", {"run_id": "sustained-run", "profile_sha256": hashlib.sha256(profile).hexdigest(), "epoch_zero_digest": e0, "window_start_monotonic_ns": start, "window_end_monotonic_ns": end})])),
        "manager_log": _write(root, "logs/adaptive-manager.log", b"manager\n"),
        "replica_events": [], "replica_logs": [], "replica_configs": [],
        "executables": {"hotstuff_app": _write(root, "runtime/hotstuff-app", b"app"), "adaptation_manager": _write(root, "runtime/adaptation-manager", b"manager")},
    }
    marker = f"KAURI_FAULT fault=role_scoped_persistent_selected_omission_v1 proposal_epoch=0 proposal_tree=4 proposal_epoch_digest={e0} proposal_block_hash={block} window=w window_start_monotonic_ns=1 window_end_monotonic_ns=100 actor=1 action=omit_aggregate monotonic_ns=10 cohort=hard hard_actor_count=1 responsive_degraded_actor_count=0 fault_threshold=2 max_omissions_per_proposal=1 responsive_omission_period=0 contribution_ordinal=0 contribution_role=internal role_contribution_ordinal=0 authenticated_proposal_source_replica=4\n"
    for replica in range(7):
        events = []
        if replica == 2:
            events.append(_event("replica-2", 1, 8, "block.committed", baseline_commit))
        events.append(_event(f"replica-{replica}", 2 if replica == 2 else 1, 8,
                             "block.commit_observed", baseline_observed))
        if replica == 1:
            events.append(_event("replica-1", 2, 10, "fault.contribution_opportunity", opportunity))
        local_commit = dict(commit); local_commit["designated_observer"] = replica == 2
        commit_sequence = 3 if replica in (1, 2) else 2
        events.append(_event(f"replica-{replica}", commit_sequence, end - 1, "block.committed", local_commit))
        events.append(_event(f"replica-{replica}", commit_sequence + 1, end - 1, "block.commit_observed", observed))
        events.append(_event(f"replica-{replica}", commit_sequence + 2, end, "horizon.complete", {}))
        artifacts["replica_events"].append(_write(root, f"raw/replica-{replica}.jsonl", _jsonl(events)))
        artifacts["replica_logs"].append(_write(root, f"logs/replica-{replica}.log", marker.encode() if replica == 1 else b"replica\n"))
        artifacts["replica_configs"].append(_write(root, f"runtime/replica-{replica}.conf", replica_configs[replica]))
    artifacts["finalization_receipt"] = _write(root, "runtime/final.json", _canonical({"schema_version": 1, "kind": "kauri-n7-sustained-role-fixed-e0-launch-finalization-v1", "state": "FIXED_E0_HORIZON_COMPLETED_NO_SUCCESSOR", "plan_sha256": plan["plan_sha256"], "authorization_sha256": artifacts["approved_authorization"]["sha256"], "e0_identity_sha256": artifacts["e0_identity_receipt"]["sha256"], "no_retry": True}))
    effective_manager = subject._plan_manager_argv(plan, e0_digest=e0, arm="fixed_e0")
    binding = {"request_sha256": artifacts["authorization_request"]["sha256"], "approval_sha256": artifacts["approved_authorization"]["sha256"], "e0_digest": e0, "scheduled_window": {"start_monotonic_ns": start, "end_monotonic_ns": end}, "manager_argv_sha256": hashlib.sha256(_canonical({"argv": effective_manager})).hexdigest(), "manager_executable_sha256": hashlib.sha256(b"manager").hexdigest(), "replica_argv_sha256": [row["sha256"] for row in plan["commands"]["replicas"]], "replica_executable_sha256": hashlib.sha256(b"app").hexdigest(), "native_profile_sha256": profile_descriptor["sha256"], "selection_profile_sha256": selection_descriptor["sha256"], "exit_codes": {"adaptive-manager": 0, **{f"replica-{i}": 0 for i in range(7)}}}
    receipt = {"schema_version": 1, "kind": subject.KIND, "state": "SEALED_RAW_BUNDLE_NO_CLAIM", "arm": "fixed_e0", "run_id": "sustained-run", "plan_sha256": plan["plan_sha256"], "anchor": {"source_id": "replica-1", "source_sequence": 2, "line_sha256": hashlib.sha256(_jsonl([_event("replica-1", 2, 10, "fault.contribution_opportunity", opportunity)]).rstrip(b"\n")).hexdigest(), "monotonic_ns": 10}, "horizon": {"clock": "CLOCK_MONOTONIC_RAW", "duration_ns": 60_000_000_000, "late_offset_ns": 20_000_000_000}, "artifacts": artifacts, "launch_binding": binding}
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(receipt)).hexdigest()
    _write(root, "receipt.json", _canonical(receipt))
    return root / "receipt.json"


def _rewrite_replica_events(root: Path, receipt: dict, transform) -> None:
    for replica, descriptor in enumerate(receipt["artifacts"]["replica_events"]):
        path = root / descriptor["path"]
        events = [json.loads(line) for line in path.read_text().splitlines()]
        receipt["artifacts"]["replica_events"][replica] = _write(
            root, descriptor["path"], _jsonl(transform(events))
        )
    receipt["receipt_sha256"] = hashlib.sha256(_canonical({
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    })).hexdigest()
    (root / "receipt.json").write_bytes(_canonical(receipt))


def test_validator_rejects_unsealed_or_missing_artifact_contract(tmp_path: Path) -> None:
    receipt = {
        "schema_version": 1, "kind": subject.KIND, "state": "SEALED_RAW_BUNDLE_NO_CLAIM",
        "arm": "adaptive_e1", "run_id": "run", "plan_sha256": "a" * 64,
        "anchor": {"source_id": "replica-1", "source_sequence": 1, "line_sha256": "b" * 64, "monotonic_ns": 1},
        "horizon": {"clock": "CLOCK_MONOTONIC_RAW", "duration_ns": 60_000_000_000, "late_offset_ns": 20_000_000_000},
        "artifacts": {},
        "launch_binding": {"request_sha256": "c" * 64, "approval_sha256": "d" * 64, "e0_digest": "e" * 64, "scheduled_window": {"start_monotonic_ns": 1, "end_monotonic_ns": 60_000_000_001}, "manager_argv_sha256": "f" * 64, "manager_executable_sha256": "a" * 64, "replica_argv_sha256": ["b" * 64] * 7, "replica_executable_sha256": "c" * 64, "native_profile_sha256": "d" * 64, "selection_profile_sha256": "e" * 64, "exit_codes": {"adaptive-manager": 0, **{f"replica-{i}": 0 for i in range(7)}}},
    }
    receipt["receipt_sha256"] = __import__("hashlib").sha256(subject._canonical(receipt)).hexdigest()
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(subject.ValidationError, match="artifact contract"):
        subject.validate_raw_bundle(tmp_path, path.name)


def test_validator_accepts_exact_fixed_e0_raw_replay_with_bounded_receipt(tmp_path: Path) -> None:
    verdict = subject.validate_raw_bundle(tmp_path, _accepted_fixed_bundle(tmp_path).name)
    assert verdict["verdict"] == "PASS_COMPONENT_ONLY_NO_CLAIM"
    assert verdict["commit_metric"]["definition"] == "replica2_designated_commit_joined_to_all7_commit_observed"
    assert verdict["commit_metric"]["counts"] == {"common_completed_commits": 1}


def test_executable_bound_accepts_debug_binary_without_relaxing_raw_bound(tmp_path: Path) -> None:
    raw = b"x" * (subject._MAX_RAW + 1)
    descriptor = _write(tmp_path, "inputs/debug-binary", raw)
    assert subject._MAX_EXECUTABLE > len(raw)
    assert subject._read_descriptor(tmp_path, descriptor, "executable", subject._MAX_EXECUTABLE) == raw
    with pytest.raises(subject.ValidationError, match="bounded regular file"):
        subject._read_descriptor(tmp_path, descriptor, "raw event stream", subject._MAX_RAW)


def test_validator_rejects_cross_replica_physical_fault_opportunity(tmp_path: Path) -> None:
    receipt_path = _accepted_fixed_bundle(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    descriptor = receipt["artifacts"]["replica_events"][2]
    path = tmp_path / descriptor["path"]
    events = [json.loads(line) for line in path.read_text().splitlines()]
    events.append(_event("replica-2", 6, events[-1]["source_monotonic_ns"],
                         "fault.contribution_opportunity", {}))
    receipt["artifacts"]["replica_events"][2] = _write(
        tmp_path, descriptor["path"], _jsonl(events))
    receipt["receipt_sha256"] = hashlib.sha256(_canonical({
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    })).hexdigest()
    receipt_path.write_bytes(_canonical(receipt))
    with pytest.raises(subject.ValidationError, match="replica-2 records a physical fault opportunity"):
        subject.validate_raw_bundle(tmp_path, receipt_path.name)


def test_validator_rejects_cross_replica_foreign_fault_marker(tmp_path: Path) -> None:
    receipt_path = _accepted_fixed_bundle(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    source_marker = receipt["artifacts"]["replica_logs"][1]
    target_marker = receipt["artifacts"]["replica_logs"][3]
    marker_bytes = (tmp_path / source_marker["path"]).read_bytes().replace(
        b"fault=role_scoped_persistent_selected_omission_v1", b"fault=foreign_fault_mode_v1")
    receipt["artifacts"]["replica_logs"][3] = _write(
        tmp_path, target_marker["path"], marker_bytes)
    receipt["receipt_sha256"] = hashlib.sha256(_canonical({
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    })).hexdigest()
    receipt_path.write_bytes(_canonical(receipt))
    with pytest.raises(subject.ValidationError, match="replica-3 log records a physical fault marker"):
        subject.validate_raw_bundle(tmp_path, receipt_path.name)


def test_validator_rejects_replica_one_foreign_fault_marker(tmp_path: Path) -> None:
    receipt_path = _accepted_fixed_bundle(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    descriptor = receipt["artifacts"]["replica_logs"][1]
    path = tmp_path / descriptor["path"]
    marker_bytes = path.read_bytes()
    path.write_bytes(marker_bytes + marker_bytes.replace(
        b"fault=role_scoped_persistent_selected_omission_v1", b"fault=foreign_fault_mode_v1"))
    receipt["artifacts"]["replica_logs"][1] = _write(
        tmp_path, descriptor["path"], path.read_bytes())
    receipt["receipt_sha256"] = hashlib.sha256(_canonical({
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    })).hexdigest()
    receipt_path.write_bytes(_canonical(receipt))
    with pytest.raises(subject.ValidationError, match="fault opportunities do not biject"):
        subject.validate_raw_bundle(tmp_path, receipt_path.name)


def test_validator_rejects_phase_gated_anchor_outside_tree_four(tmp_path: Path) -> None:
    receipt_path = _accepted_fixed_bundle(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    plan_descriptor = receipt["artifacts"]["execution_plan"]
    plan = json.loads((tmp_path / plan_descriptor["path"]).read_text())
    actor = plan["commands"]["replicas"][1]
    actor["argv"].extend(["--experiment-byzantine-first-omission-tree", "4"])
    actor["sha256"] = hashlib.sha256(
        _canonical({"schema_version": 1, "argv": actor["argv"]})).hexdigest()
    plan.pop("plan_sha256")
    plan["plan_sha256"] = hashlib.sha256(_canonical(plan)).hexdigest()
    receipt["plan_sha256"] = plan["plan_sha256"]
    receipt["artifacts"]["execution_plan"] = _write(
        tmp_path, plan_descriptor["path"], _canonical(plan))
    request_descriptor = receipt["artifacts"]["authorization_request"]
    request = json.loads((tmp_path / request_descriptor["path"]).read_text())
    request["execution_plan_sha256"] = plan["plan_sha256"]
    receipt["artifacts"]["authorization_request"] = _write(
        tmp_path, request_descriptor["path"], _canonical(request))
    approval_descriptor = receipt["artifacts"]["approved_authorization"]
    approval = json.loads((tmp_path / approval_descriptor["path"]).read_text())
    approval["request_sha256"] = receipt["artifacts"]["authorization_request"]["sha256"]
    approval["plan_sha256"] = plan["plan_sha256"]
    receipt["artifacts"]["approved_authorization"] = _write(
        tmp_path, approval_descriptor["path"], _canonical(approval))
    binding = receipt["launch_binding"]
    binding["request_sha256"] = receipt["artifacts"]["authorization_request"]["sha256"]
    binding["approval_sha256"] = receipt["artifacts"]["approved_authorization"]["sha256"]
    binding["replica_argv_sha256"][1] = actor["sha256"]
    binding["first_omission_tree"] = 4
    finalization_descriptor = receipt["artifacts"]["finalization_receipt"]
    finalization = json.loads((tmp_path / finalization_descriptor["path"]).read_text())
    finalization["plan_sha256"] = plan["plan_sha256"]
    finalization["authorization_sha256"] = receipt["artifacts"]["approved_authorization"]["sha256"]
    receipt["artifacts"]["finalization_receipt"] = _write(
        tmp_path, finalization_descriptor["path"], _canonical(finalization))
    events_descriptor = receipt["artifacts"]["replica_events"][1]
    events = [json.loads(line) for line in (tmp_path / events_descriptor["path"]).read_text().splitlines()]
    opportunity = next(event for event in events if event["event_type"] == "fault.contribution_opportunity")
    opportunity["payload"]["proposal"]["tree_id"] = 3
    raw_events = _jsonl(events)
    receipt["artifacts"]["replica_events"][1] = _write(
        tmp_path, events_descriptor["path"], raw_events)
    receipt["anchor"]["line_sha256"] = hashlib.sha256(
        _jsonl([opportunity]).rstrip(b"\n")).hexdigest()
    receipt["receipt_sha256"] = hashlib.sha256(_canonical({
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    })).hexdigest()
    receipt_path.write_bytes(_canonical(receipt))
    with pytest.raises(subject.ValidationError, match="frozen first-omission tree"):
        subject.validate_raw_bundle(tmp_path, receipt_path.name)


def test_source_derived_digest_matches_the_native_n7_epoch_zero_fixture() -> None:
    tree = (ROOT / "n7-path-timeout-quorum" / "epoch0.tree").read_bytes()
    assert subject._source_adaptive_v2_epoch_zero_digest(tree) == (
        "53cd7d493fc23a466b5e1f7d8655725893e6a4052ba9cc8d566041c9e001a1be"
    )


def test_validator_rejects_self_asserted_e0_helper_digest_without_executing_it(tmp_path: Path) -> None:
    receipt_path = _accepted_fixed_bundle(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    marker = tmp_path / "helper-was-executed"
    helper = b"#!/bin/sh\ntouch helper-was-executed\nprintf '%s\\n' " + b"0" * 64 + b"\n"
    e0 = json.loads((tmp_path / receipt["artifacts"]["e0_identity_receipt"]["path"]).read_text())
    helper_descriptor = _write(tmp_path, "runtime/malicious-helper", helper)
    e0["epoch_digest"] = "0" * 64
    e0["helper_binary"] = str((tmp_path / helper_descriptor["path"]).resolve())
    e0["helper_binary_sha256"] = helper_descriptor["sha256"]
    e0["argv"] = [e0["helper_binary"], str((tmp_path / "config/epoch0.tree").resolve())]
    receipt["artifacts"]["e0_identity_helper"] = helper_descriptor
    receipt["artifacts"]["e0_identity_receipt"] = _write(
        tmp_path, "runtime/e0-identity-receipt.json", _canonical(e0)
    )
    with pytest.raises(subject.ValidationError, match="source-derived E0 digest"):
        subject._validate_source_derived_e0(
            tmp_path, receipt["artifacts"], expected_digest="0" * 64
        )
    assert not marker.exists()


def test_validator_rejects_receipt_binding_mutated_away_from_archived_plan(tmp_path: Path) -> None:
    receipt_path = _accepted_fixed_bundle(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    receipt["launch_binding"]["replica_argv_sha256"][6] = "0" * 64
    receipt["receipt_sha256"] = hashlib.sha256(_canonical({
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    })).hexdigest()
    receipt_path.write_bytes(_canonical(receipt))
    with pytest.raises(subject.ValidationError, match="replica receipt launch binding"):
        subject.validate_raw_bundle(tmp_path, receipt_path.name)


def test_validator_rejects_mutated_fixed_e0_no_successor_terminal(tmp_path: Path) -> None:
    receipt_path = _accepted_fixed_bundle(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    final = json.loads((tmp_path / receipt["artifacts"]["finalization_receipt"]["path"]).read_text())
    final["state"] = "SUCCESSOR_CREATED"
    receipt["artifacts"]["finalization_receipt"] = _write(tmp_path, "runtime/final.json", _canonical(final))
    receipt["receipt_sha256"] = hashlib.sha256(_canonical({key: value for key, value in receipt.items() if key != "receipt_sha256"})).hexdigest()
    receipt_path.write_bytes(_canonical(receipt))
    with pytest.raises(subject.ValidationError, match="no-successor"):
        subject.validate_raw_bundle(tmp_path, receipt_path.name)


def test_validator_binds_replica_source_instances_and_observer_to_archived_configs(tmp_path: Path) -> None:
    receipt_path = _accepted_fixed_bundle(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    config_path = tmp_path / receipt["artifacts"]["replica_configs"][3]["path"]
    config_path.write_text(config_path.read_text().replace(
        "structured-event-source-instance = replica-3-instance",
        "structured-event-source-instance = forged-instance",
    ))
    receipt["artifacts"]["replica_configs"][3] = _write(
        tmp_path, str(config_path.relative_to(tmp_path)), config_path.read_bytes()
    )
    receipt["receipt_sha256"] = hashlib.sha256(_canonical({
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    })).hexdigest()
    receipt_path.write_bytes(_canonical(receipt))
    with pytest.raises(subject.ValidationError, match="replica-3 config differs from archived plan binding"):
        subject.validate_raw_bundle(tmp_path, receipt_path.name)


def test_adaptive_e1_acceptance_has_an_explicit_non_generic_contract() -> None:
    assert subject.ADAPTIVE_E1_REQUIRED_ARTIFACTS == (
        "canonical signed adaptive-v2 E1 bundle bytes and SHA-256",
        "canonical issuer public-key bytes and SHA-256",
        "independent native bundle decode/signature verification bound to that issuer",
        "decoded E1 predecessor E0 digest, successor E1 digest, and activation height",
        "decoded five-tree Q=5/N=7 membership with actor 1 wait-exempt leaf in every tree",
        "all-seven epoch.command_committed raw-event bindings to the decoded E0-to-E1 command",
        "all-seven epoch.activated raw events for that exact E1 digest by anchor plus 20 seconds",
        "seven replica JSONL streams, eight logs, and clean eight-process cleanup through anchor plus 60 seconds",
    )


def _adaptive_v2_five_tree_bundle_fixture(*, tree_count: int = 5) -> tuple[dict, dict, dict, SimpleNamespace]:
    """Return the frozen W19 adaptive-v2 E1 shape without native signing.

    The wire/signature implementation is independently covered by
    ``factorial_validation``.  This narrow validator test instead spies on
    decoder selection, so a v3 decoder can never accidentally accept an
    adaptive-v2 W19 artifact merely because its decoded Python shape happens
    to look compatible.
    """
    e0, e1, payload_digest = "a" * 64, "b" * 64, "c" * 64
    trees = tuple(
        SimpleNamespace(tree_id=tree_id, fanout=2, pipeline_stretch=2,
                        members=(0, 2, 3, 1, 4, 5, 6), wait_exempt=(1,))
        for tree_id in range(tree_count)
    )
    bundle = SimpleNamespace(
        command=SimpleNamespace(
            predecessor_epoch_digest=e0,
            issuer_id=_W19_ISSUER_ID,
            successor_epoch_number=1,
            successor_epoch_digest=e1,
            activation_delay_blocks=5,
            payload_digest=payload_digest,
        ),
        epoch_number=1,
        epoch_digest=e1,
        previous_epoch_digest=e0,
        generation_seed=41719,
        trees=trees,
    )
    artifacts = {
        "main_config": {"path": "main.conf", "sha256": hashlib.sha256(b"epoch-protocol-mode = adaptive_v2\nepoch-change-issuer-id = 1\nepoch-change-issuer-public-key = 02" + b"0" * 64 + b"\n").hexdigest()},
        "issuer_public_key": {"path": "issuer.pub", "sha256": hashlib.sha256(_W19_ISSUER_PUBLIC_KEY.encode("ascii") + b"\n").hexdigest()},
        "e1_bundle": {"path": "e1.bundle", "sha256": hashlib.sha256(b"adaptive-v2-wire").hexdigest()},
    }
    receipt = {"launch_binding": {"e0_digest": e0}}
    streams = {}
    command = {
        "command_block_height": 11,
        "command_block_hash": "d" * 64,
        "payload_digest": payload_digest,
        "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": e0,
        "successor_epoch_number": 1,
        "successor_epoch_digest": e1,
        "activation_delay_blocks": 5,
        "activation_height": 16,
    }
    for replica in range(7):
        streams[f"replica-{replica}"] = [
            _event(f"replica-{replica}", 1, 10, "epoch.command_committed", command),
            _event(f"replica-{replica}", 2, 11, "epoch.activated", {
                "epoch_number": 1, "tree_id": 0, "epoch_digest": e1,
                "activation_height": 16,
            }),
        ]
    return artifacts, receipt, streams, bundle


def _write_adaptive_v2_bundle_inputs(root: Path, artifacts: dict, *, protocol_mode: str = "adaptive_v2",
                                     issuer_public_key: str = _W19_ISSUER_PUBLIC_KEY,
                                     issuer_id: int = _W19_ISSUER_ID) -> None:
    main_raw = (
        f"epoch-protocol-mode = {protocol_mode}\n"
        f"epoch-change-issuer-id = {issuer_id}\n"
        f"epoch-change-issuer-public-key = {issuer_public_key}\n"
    ).encode("ascii")
    artifacts["main_config"] = _write(root, artifacts["main_config"]["path"], main_raw)
    issuer = _W19_ISSUER_PUBLIC_KEY.encode("ascii") + b"\n"
    _write(root, artifacts["issuer_public_key"]["path"], issuer)
    _write(root, artifacts["e1_bundle"]["path"], b"adaptive-v2-wire")


def test_w19_adaptive_v2_bundle_uses_v2_decoder_and_exact_five_tree_shape(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts, receipt, streams, bundle = _adaptive_v2_five_tree_bundle_fixture()
    _write_adaptive_v2_bundle_inputs(tmp_path, artifacts)
    calls: list[str] = []

    class Decoder:
        def decode_epoch_change_bundle(self, payload: bytes, *, issuer_public_key: str):
            calls.append("v2")
            assert payload == b"adaptive-v2-wire"
            assert issuer_public_key == _W19_ISSUER_PUBLIC_KEY
            return bundle

        def decode_adaptive_v3_epoch_change_bundle(self, payload: bytes, *, issuer_public_key: str):
            calls.append("v3")
            raise ValueError("adaptive-v3 wire domain is not permitted for W19")

    monkeypatch.setattr(subject, "_factorial_bundle_decoder", lambda: Decoder())
    verified = subject._validate_adaptive_bundle_and_activation(
        tmp_path, artifacts, receipt=receipt, streams=streams, anchor_ns=0)

    assert verified is bundle
    assert calls == ["v2"]


def test_w19_adaptive_v2_validator_rejects_v3_wire_without_fallback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts, receipt, streams, bundle = _adaptive_v2_five_tree_bundle_fixture()
    _write_adaptive_v2_bundle_inputs(tmp_path, artifacts)
    calls: list[str] = []

    class Decoder:
        def decode_epoch_change_bundle(self, payload: bytes, *, issuer_public_key: str):
            calls.append("v2")
            raise ValueError("epoch-change bundle domain is invalid")

        def decode_adaptive_v3_epoch_change_bundle(self, payload: bytes, *, issuer_public_key: str):
            calls.append("v3")
            return bundle

    monkeypatch.setattr(subject, "_factorial_bundle_decoder", lambda: Decoder())
    with pytest.raises(subject.ValidationError, match="does not independently verify"):
        subject._validate_adaptive_bundle_and_activation(
            tmp_path, artifacts, receipt=receipt, streams=streams, anchor_ns=0)
    assert calls == ["v2"]


def test_w19_adaptive_v2_validator_rejects_archived_v3_protocol_config(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts, receipt, streams, _ = _adaptive_v2_five_tree_bundle_fixture()
    _write_adaptive_v2_bundle_inputs(tmp_path, artifacts, protocol_mode="adaptive_v3")

    class Decoder:
        def decode_epoch_change_bundle(self, payload: bytes, *, issuer_public_key: str):
            raise AssertionError("wrong archived protocol configuration must fail before decode")

    monkeypatch.setattr(subject, "_factorial_bundle_decoder", lambda: Decoder())
    with pytest.raises(subject.ValidationError, match="archived adaptive-v2 protocol mode"):
        subject._validate_adaptive_bundle_and_activation(
            tmp_path, artifacts, receipt=receipt, streams=streams, anchor_ns=0)


def test_w19_adaptive_v2_validator_binds_main_config_issuer_public_key(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts, receipt, streams, _ = _adaptive_v2_five_tree_bundle_fixture()
    _write_adaptive_v2_bundle_inputs(
        tmp_path, artifacts, issuer_public_key="03" + "1" * 64)

    class Decoder:
        def decode_epoch_change_bundle(self, payload: bytes, *, issuer_public_key: str):
            raise AssertionError("wrong configured issuer key must fail before wire decode")

    monkeypatch.setattr(subject, "_factorial_bundle_decoder", lambda: Decoder())
    with pytest.raises(subject.ValidationError, match="issuer"):
        subject._validate_adaptive_bundle_and_activation(
            tmp_path, artifacts, receipt=receipt, streams=streams, anchor_ns=0)


@pytest.mark.parametrize("configured_issuer_id, decoded_issuer_id", [(2, 1), (1, 2)],
                         ids=["wrong-main-config-id", "wrong-decoded-command-id"])
def test_w19_adaptive_v2_validator_binds_main_config_and_command_issuer_id(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_issuer_id: int,
        decoded_issuer_id: int) -> None:
    artifacts, receipt, streams, bundle = _adaptive_v2_five_tree_bundle_fixture()
    _write_adaptive_v2_bundle_inputs(tmp_path, artifacts, issuer_id=configured_issuer_id)
    bundle.command.issuer_id = decoded_issuer_id

    class Decoder:
        def decode_epoch_change_bundle(self, payload: bytes, *, issuer_public_key: str):
            return bundle

    monkeypatch.setattr(subject, "_factorial_bundle_decoder", lambda: Decoder())
    with pytest.raises(subject.ValidationError, match="issuer"):
        subject._validate_adaptive_bundle_and_activation(
            tmp_path, artifacts, receipt=receipt, streams=streams, anchor_ns=0)


def test_w19_adaptive_v2_validator_rejects_nonfive_tree_bundle(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts, receipt, streams, bundle = _adaptive_v2_five_tree_bundle_fixture(tree_count=4)
    _write_adaptive_v2_bundle_inputs(tmp_path, artifacts)

    class Decoder:
        def decode_epoch_change_bundle(self, payload: bytes, *, issuer_public_key: str):
            return bundle

        def decode_adaptive_v3_epoch_change_bundle(self, payload: bytes, *, issuer_public_key: str):
            raise AssertionError("W19 must not decode an adaptive-v2 wire as v3")

    monkeypatch.setattr(subject, "_factorial_bundle_decoder", lambda: Decoder())
    with pytest.raises(subject.ValidationError, match="five-tree generation"):
        subject._validate_adaptive_bundle_and_activation(
            tmp_path, artifacts, receipt=receipt, streams=streams, anchor_ns=0)


def _replica_trust_launch_fixture(root: Path) -> tuple[list[str], dict[str, str], dict[str, str], bytes]:
    main_raw = (
        f"epoch-protocol-mode = adaptive_v2\n"
        f"epoch-change-issuer-id = {_W19_ISSUER_ID}\n"
        f"epoch-change-issuer-public-key = {_W19_ISSUER_PUBLIC_KEY}\n"
    ).encode("ascii")
    replica_raw = b"idx = 0\nstructured-event-run-id = sustained-run\n"
    # Receipt artifacts are portable relative copies.  The archived plan keeps
    # the original absolute paths that must be the argv values.
    _write(root, "receipt/main.conf", main_raw)
    _write(root, "receipt/replica-0.conf", replica_raw)
    main = {"path": str((root / "launch-inputs/main.conf").resolve())}
    replica = {"path": str((root / "launch-inputs/replica-0.conf").resolve())}
    argv = [
        "hotstuff-app", "--conf", str((root / main["path"]).resolve()),
        "--conf", str((root / replica["path"]).resolve()),
    ]
    return argv, main, replica, replica_raw


def test_replica_trust_launch_requires_exact_main_then_replica_conf(tmp_path: Path) -> None:
    argv, main, replica, replica_raw = _replica_trust_launch_fixture(tmp_path)
    subject._validate_replica_trust_launch(
        tmp_path, argv, main, replica, replica_raw, "replica-0")


@pytest.mark.parametrize("override", [
    "epoch-protocol-mode = adaptive_v3",
    "epoch-change-issuer-id = 2",
    "epoch-change-issuer-public-key = 03" + "1" * 64,
], ids=["protocol-mode", "issuer-id", "issuer-key"])
def test_replica_trust_launch_rejects_replica_specific_trust_override(
        tmp_path: Path, override: str) -> None:
    argv, main, replica, replica_raw = _replica_trust_launch_fixture(tmp_path)
    replica_raw += (override + "\n").encode("ascii")
    with pytest.raises(subject.ValidationError, match="replica"):
        subject._validate_replica_trust_launch(
            tmp_path, argv, main, replica, replica_raw, "replica-0")


@pytest.mark.parametrize("argv_variant", [
    lambda argv, main, replica: [*argv, "--conf", "/private/tmp/untrusted-extra.conf"],
    lambda argv, main, replica: [argv[0], "--conf", argv[4], "--conf", argv[2]],
    lambda argv, main, replica: [argv[0], f"--conf={argv[2]}", "--conf", argv[4]],
    lambda argv, main, replica: [argv[0], f"-c{argv[2]}", "--conf", argv[4]],
    lambda argv, main, replica: [argv[0], f"--con={argv[2]}", "--conf", argv[4]],
], ids=["extra-conf", "reordered-conf", "equals-conf", "short-conf", "abbreviated-conf"])
def test_replica_trust_launch_rejects_extra_or_reordered_conf(
        tmp_path: Path, argv_variant) -> None:
    argv, main, replica, replica_raw = _replica_trust_launch_fixture(tmp_path)
    with pytest.raises(subject.ValidationError, match="replica"):
        subject._validate_replica_trust_launch(
            tmp_path, argv_variant(argv, main, replica), main, replica,
            replica_raw, "replica-0")


@pytest.mark.parametrize("flag, value", [
    ("--epoch-protocol-mode", "adaptive_v3"),
    ("--epoch-change-issuer-id", "2"),
    ("--epoch-change-issuer-public-key", "03" + "1" * 64),
], ids=["protocol-mode", "issuer-id", "issuer-key"])
def test_replica_trust_launch_rejects_direct_cli_trust_override(
        tmp_path: Path, flag: str, value: str) -> None:
    argv, main, replica, replica_raw = _replica_trust_launch_fixture(tmp_path)
    with pytest.raises(subject.ValidationError, match="replica"):
        subject._validate_replica_trust_launch(
            tmp_path, [*argv, flag, value], main, replica, replica_raw,
            "replica-0")


def test_native_main_config_parser_rejects_duplicate_effective_assignment() -> None:
    main_raw = b"epoch-protocol-mode = adaptive_v2\nepoch-protocol-mode=adaptive_v2\n"
    with pytest.raises(subject.ValidationError, match="main config lacks one exact"):
        subject._config_option(main_raw, "epoch-protocol-mode", "main config")


def test_native_adaptive_v2_activation_envelope_requires_exact_four_fields() -> None:
    digest = "a" * 64
    streams = {
        f"replica-{replica}": [_event(f"replica-{replica}", 1, 20_000_000_000,
                                        "epoch.activated",
                                        {"epoch_number": 1, "tree_id": 0,
                                         "epoch_digest": digest, "activation_height": 9})]
        for replica in range(7)
    }
    subject._validate_adaptive_activation(streams, anchor_ns=0)
    streams["replica-6"][0]["payload"]["tree_id"] = 6
    subject._validate_adaptive_activation(streams, anchor_ns=0)
    broken = {source: [dict(event, payload=dict(event["payload"]))] for source, (event,) in streams.items()}
    broken["replica-6"][0]["payload"]["certificate_apply_committed_height"] = 9
    with pytest.raises(subject.ValidationError, match="schema"):
        subject._validate_adaptive_activation(broken, anchor_ns=0)


def test_e1_command_identity_rejects_wrong_block_or_activation_height() -> None:
    e0, e1, payload_digest = "a" * 64, "b" * 64, "c" * 64
    bundle = SimpleNamespace(
        epoch_digest=e1,
        command=SimpleNamespace(payload_digest=payload_digest, activation_delay_blocks=5),
    )
    command = {
        "command_block_height": 11, "command_block_hash": "d" * 64,
        "payload_digest": payload_digest, "predecessor_epoch_number": 0,
        "predecessor_epoch_digest": e0, "successor_epoch_number": 1,
        "successor_epoch_digest": e1, "activation_delay_blocks": 5,
        "activation_height": 16,
    }
    assert subject._e1_command_identity(command, bundle=bundle, e0_digest=e0)[0:2] == (11, "d" * 64)
    bad_hash = dict(command, command_block_hash="not-a-hash")
    with pytest.raises(subject.ValidationError, match="block hash"):
        subject._e1_command_identity(bad_hash, bundle=bundle, e0_digest=e0)
    bad_height = dict(command, activation_height=15)
    with pytest.raises(subject.ValidationError, match="activation delay"):
        subject._e1_command_identity(bad_height, bundle=bundle, e0_digest=e0)


def test_native_marker_rejects_wrong_schedule_bounds() -> None:
    marker = {
        "fault": "role_scoped_persistent_selected_omission_v1",
        "proposal_epoch": "0", "proposal_tree": "4", "proposal_epoch_digest": "a" * 64,
        "proposal_block_hash": "b" * 64, "window": "window", "window_start_monotonic_ns": "1",
        "window_end_monotonic_ns": "2", "actor": "1", "action": "omit_aggregate", "monotonic_ns": "1",
        "cohort": "hard", "hard_actor_count": "1", "responsive_degraded_actor_count": "0",
        "fault_threshold": "2", "max_omissions_per_proposal": "2", "responsive_omission_period": "0",
        "contribution_ordinal": "0", "contribution_role": "internal", "role_contribution_ordinal": "0",
        "authenticated_proposal_source_replica": "4",
    }
    with pytest.raises(subject.ValidationError, match="one-omission"):
        subject._marker_identity(marker)


def test_commit_replay_rejects_duplicate_witness_and_ignores_local_batch_index() -> None:
    digest, block = "a" * 64, "b" * 64
    authoritative = {
        "block_height": 3, "block_hash": block, "parent_hash": None,
        "transaction_count": 1, "designated_observer": True,
        "decision_proof": {"epoch_number": 0, "tree_id": 0,
                           "epoch_digest": digest, "block_hash": block},
        "view_generation": 0, "commit_batch_index": 11,
    }
    streams = {
        f"replica-{replica}": [
            _event(f"replica-{replica}", 1, 11, "block.commit_observed",
                   {"block_height": 3, "block_hash": block, "parent_hash": None,
                    "transaction_count": 1, "commit_batch_index": replica})
        ] for replica in range(7)
    }
    streams["replica-0"][0]["source_sequence"] = 2
    streams["replica-0"].insert(0, _event("replica-0", 1, 10, "block.committed", authoritative))
    assert subject._validate_commit_metrics(streams, anchor_ns=10, e0_digest=digest,
                                            arm="fixed_e0", designated_observer=0) == {"common_completed_commits": 1}
    streams["replica-1"].append(_event("replica-1", 2, 12, "block.commit_observed",
                                         {"block_height": 3, "block_hash": block,
                                          "parent_hash": None, "transaction_count": 2,
                                          "commit_batch_index": 8}))
    with pytest.raises(subject.ValidationError, match="repeats commit-observed"):
        subject._validate_commit_metrics(streams, anchor_ns=10, e0_digest=digest,
                                         arm="fixed_e0", designated_observer=0)


def test_commit_replay_rejects_conflicting_hashes_at_one_height() -> None:
    """A height identifies one committed block across all raw witnesses."""
    digest, block, conflicting = "a" * 64, "b" * 64, "c" * 64
    authoritative = {
        "block_height": 3, "block_hash": block, "parent_hash": None,
        "transaction_count": 1, "designated_observer": True,
        "decision_proof": {"epoch_number": 0, "tree_id": 0,
                           "epoch_digest": digest, "block_hash": block},
        "view_generation": 0, "commit_batch_index": 0,
    }
    streams = {
        f"replica-{replica}": [
            _event(f"replica-{replica}", 1, 11, "block.commit_observed",
                   {"block_height": 3, "block_hash": block, "parent_hash": None,
                    "transaction_count": 1, "commit_batch_index": replica}),
        ] for replica in range(7)
    }
    streams["replica-0"].insert(0, _event("replica-0", 1, 10, "block.committed", authoritative))
    streams["replica-1"].append(_event(
        "replica-1", 2, 12, "block.commit_observed",
        {"block_height": 3, "block_hash": conflicting, "parent_hash": None,
         "transaction_count": 1, "commit_batch_index": 1}))
    with pytest.raises(subject.ValidationError, match="conflicting commit hashes at block height 3"):
        subject._validate_commit_metrics(streams, anchor_ns=10, e0_digest=digest,
                                         arm="fixed_e0", designated_observer=0)


def test_commit_replay_allows_empty_common_horizon_as_a_negative_measurement() -> None:
    streams = {f"replica-{replica}": [] for replica in range(7)}
    assert subject._validate_commit_metrics(streams, anchor_ns=10, e0_digest="a" * 64,
                                            arm="fixed_e0") == {
                                                "common_completed_commits": 0,
                                            }


def test_validator_rejects_missing_independent_prearm_baseline(tmp_path: Path) -> None:
    receipt_path = _accepted_fixed_bundle(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    _rewrite_replica_events(
        tmp_path,
        receipt,
        lambda events: [event for event in events
                        if not (event["event_type"] in {"block.committed", "block.commit_observed"}
                                and event["source_monotonic_ns"] < 10)],
    )
    with pytest.raises(subject.ValidationError, match="Epoch-0 commit precedes scheduled fault start"):
        subject.validate_raw_bundle(tmp_path, receipt_path.name)


def test_validator_accepts_zero_post_anchor_common_commits_as_no_claim(tmp_path: Path) -> None:
    receipt_path = _accepted_fixed_bundle(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    _rewrite_replica_events(
        tmp_path,
        receipt,
        lambda events: [event for event in events
                        if not (event["event_type"] in {"block.committed", "block.commit_observed"}
                                and event["source_monotonic_ns"] >= 10)],
    )
    verdict = subject.validate_raw_bundle(tmp_path, receipt_path.name)
    assert verdict["verdict"] == "PASS_COMPONENT_ONLY_NO_CLAIM"
    assert verdict["commit_metric"]["counts"] == {"common_completed_commits": 0}


def test_jsonl_rejects_mixed_source_instances_and_wrong_kind() -> None:
    first = _event("replica-1", 1, 10, "horizon.complete", {})
    second = _event("replica-1", 2, 11, "horizon.complete", {})
    second["source_instance"] = "other-instance"
    with pytest.raises(subject.ValidationError, match="mixes source instances"):
        subject._parse_jsonl(_jsonl([first, second]), run_id="sustained-run",
                             source_id="replica-1")
    first["source_kind"] = "adaptation_manager"
    with pytest.raises(subject.ValidationError, match="source kind"):
        subject._parse_jsonl(_jsonl([first]), run_id="sustained-run",
                             source_id="replica-1")


def test_fixed_control_accepts_periodic_native_observations_after_anchor() -> None:
    digest, profile = "a" * 64, "b" * 64
    window = {"start_monotonic_ns": 10, "end_monotonic_ns": 70_000_000_010}
    payload = {"run_id": "sustained-run", "profile_sha256": profile,
               "epoch_zero_digest": digest, "window_start_monotonic_ns": 10,
               "window_end_monotonic_ns": window["end_monotonic_ns"]}
    receipt = {"run_id": "sustained-run", "launch_binding": {
        "scheduled_window": window, "native_profile_sha256": profile,
        "e0_digest": digest}}
    events = [_event("adaptive-manager", 1, 12, "scheduled_fixed_e0_control.observation", payload),
              _event("adaptive-manager", 2, 20_000_000_012, "scheduled_fixed_e0_control.observation", payload),
              _event("adaptive-manager", 3, 70_000_000_010, "scheduled_fixed_e0_control.terminal", payload)]
    subject._validate_fixed_e0_manager(events, receipt=receipt, anchor_ns=11)


def _adaptive_causality_fixture(root: Path):
    e0, e1, snapshot_id = "a" * 64, "b" * 64, "c" * 64
    transition = _write(root, "runtime/transition.json", b"transition\n")
    tree = _write(root, "runtime/tree", b"tree\n")
    arm = {"schema_version": 4, "kind": "kauri-focused-fault-window-arm-v4",
           "run_id": "sustained-run", "profile_id": "n7-path-local-timeout-quorum-v4",
           "profile_sha256": "d" * 64, "topology_proof_sha256": tree["sha256"],
           "request_sha256": hashlib.sha256(b"transition").hexdigest(), "epoch_number": 0,
           "epoch_digest": e0, "fault_receipt_sha256": "e" * 64,
           "evidence_start_monotonic_ns": 1, "prefault_tree_id": 4,
           "required_tree_positions": 3, "required_tree_ids": [4, 5, 6],
           "clock_domain": "same_host_clock_monotonic_raw",
           "required_observation_schema": 3,
           "timeout_evidence_basis": "exact_timeout_attempt_id_v1",
           "snapshot_evidence_basis": "exact_post_fault_path_timeout_quorum_v1",
           "selection_cardinality_policy": "all_guarded_up_to_fault_bound_v1"}
    arm_descriptor = _write(root, "runtime/arm.json", _canonical(arm))
    snapshot = {"schema_version": 2, "policy_intent": "fault_containment",
                "transition_artifact_id": "e0-to-e1-containment",
                "predecessor_epoch_number": 0, "predecessor_epoch_digest": e0,
                "baseline_cutoff": 1, "current_cutoff": 6,
                "accepted_prefix_count": 6, "evidence_snapshot_id": snapshot_id}
    snapshot_descriptor = _write(root, "runtime/snapshot.json", _canonical(snapshot))
    artifacts = {"transition_request": transition, "epoch0_tree": tree,
                 "manager_fault_window_arm": arm_descriptor,
                 "manager_evidence_snapshot": snapshot_descriptor}
    receipt = {"run_id": "sustained-run", "anchor": {"monotonic_ns": 10},
               "launch_binding": {"selection_profile_sha256": "d" * 64,
                                  "approval_sha256": "e" * 64, "e0_digest": e0}}
    manager = [_event("adaptive-manager", 1, 2, "fault_window_armed",
                      {**arm, "fault_window_arm_sha256": arm_descriptor["sha256"]})]
    physical = []
    for index, reporter in enumerate((4, 4, 5, 5, 6, 6), start=1):
        block = f"{index:064x}"
        physical.append(_event("replica-1", index, 10 + index,
                               "fault.contribution_opportunity",
                               {"actor": 1, "physical_role": "internal",
                                "scheduled_action": "omit_aggregate", "parent_replica": reporter,
                                "proposal": {"epoch_number": 0, "tree_id": reporter,
                                             "epoch_digest": e0, "block_hash": block}}))
        observation = {"schema_version": 3, "observation_id": f"{index + 10:064x}",
                       "reporter_id": reporter, "observed_replica_id": 1,
                       "configuration": {"epoch_number": 0, "tree_id": reporter,
                                         "epoch_digest": e0}, "block_hash": block,
                       "expected_message_type": "aggregate_relay", "outcome": "timeout",
                       "response_duration_us": 0, "signer_set": [],
                       "attempt_start_monotonic_ns": 3,
                       "deadline_duration_us": 1_000_000,
                       "reporter_monotonic_ns": 1_000_000_020 + index}
        manager.append(_event("adaptive-manager", index + 1, 1_000_000_030 + index,
                              "evidence.observation_accepted",
                              {"ingestion_sequence": index, "observation": observation}))
    manager.append(_event("adaptive-manager", 8, 2_000_000_000,
                          "adaptive_v2_evidence_snapshot", snapshot))
    manager.append(_event("adaptive-manager", 9, 3_000_000_000,
                          "adaptive_v2_session_terminal",
                          {"outcome": "advanced", "reason": "successor_converged",
                           "policy_intent": "fault_containment",
                           "predecessor_epoch_number": 0,
                           "predecessor_epoch_digest": e0,
                           "successor_epoch_number": 1,
                           "successor_epoch_digest": e1,
                           "command_payload_digest": "f" * 64,
                           "current_evidence_cutoff": 6}))
    bundle = SimpleNamespace(epoch_digest=e1, evidence_snapshot_id=snapshot_id,
                             evidence_cutoff=6,
                             command=SimpleNamespace(payload_digest="f" * 64))
    return artifacts, receipt, manager, physical, bundle


def test_adaptive_causality_joins_six_physical_timeouts_to_snapshot(tmp_path: Path) -> None:
    artifacts, receipt, manager, physical, bundle = _adaptive_causality_fixture(tmp_path)
    subject._validate_adaptive_causality(tmp_path, artifacts, receipt=receipt,
                                         manager_events=manager,
                                         actor_events=physical, bundle=bundle)
    with pytest.raises(subject.ValidationError, match="physical omission"):
        subject._validate_adaptive_causality(tmp_path, artifacts, receipt=receipt,
                                             manager_events=manager,
                                             actor_events=physical[:-1], bundle=bundle)
    changed = [dict(event, payload=dict(event["payload"])) for event in manager]
    changed[3]["payload"]["observation"] = dict(changed[3]["payload"]["observation"],
                                              observed_replica_id=2)
    with pytest.raises(subject.ValidationError, match="three internal reporters"):
        subject._validate_adaptive_causality(tmp_path, artifacts, receipt=receipt,
                                             manager_events=changed,
                                             actor_events=physical, bundle=bundle)


def test_adaptive_causality_skips_ordinary_candidate_observations_but_binds_timeouts(
        tmp_path: Path) -> None:
    artifacts, receipt, manager, physical, bundle = _adaptive_causality_fixture(tmp_path)
    e0 = receipt["launch_binding"]["e0_digest"]
    snapshot = json.loads((tmp_path / artifacts["manager_evidence_snapshot"]["path"]).read_text())
    snapshot["current_cutoff"] = 7
    snapshot["accepted_prefix_count"] = 7
    artifacts["manager_evidence_snapshot"] = _write(
        tmp_path, artifacts["manager_evidence_snapshot"]["path"], _canonical(snapshot))
    bundle.evidence_cutoff = 7

    def ordinary_observation(*, reporter: int, outcome: str, sequence: int) -> dict:
        return _event(
            "adaptive-manager", sequence, sequence + 2,
            "evidence.observation_accepted", {
                "ingestion_sequence": sequence - 1,
                "observation": {
                    "schema_version": 3,
                    "observation_id": f"{sequence + 40:064x}",
                    "reporter_id": reporter,
                    "observed_replica_id": 1,
                    "configuration": {"epoch_number": 0, "tree_id": reporter,
                                      "epoch_digest": e0},
                    "block_hash": f"{sequence + 80:064x}",
                    "expected_message_type": "aggregate_relay",
                    "outcome": outcome,
                    "response_duration_us": 1 if outcome == "on_time" else 1_000_001,
                    "signer_set": [1],
                    # These ordinary observations predate the physical fault.
                    "attempt_start_monotonic_ns": 1,
                    "deadline_duration_us": 1_000_000,
                    "reporter_monotonic_ns": sequence + 3,
                },
            })

    arm = manager[0]
    accepted = [event for event in manager
                if event["event_type"] == "evidence.observation_accepted"]
    snapshot_event = next(event for event in manager
                          if event["event_type"] == "adaptive_v2_evidence_snapshot")
    terminal = next(event for event in manager
                    if event["event_type"] == "adaptive_v2_session_terminal")
    shifted = []
    for sequence, event in enumerate(accepted, start=3):
        payload = dict(event["payload"])
        payload["ingestion_sequence"] += 1
        shifted.append(dict(event, source_sequence=sequence, payload=payload))
    manager = [
        arm,
        ordinary_observation(reporter=4, outcome="on_time", sequence=2),
        *shifted,
        dict(snapshot_event, source_sequence=9, payload=snapshot),
        dict(terminal, source_sequence=10,
             payload=dict(terminal["payload"], current_evidence_cutoff=7)),
    ]

    subject._validate_adaptive_causality(tmp_path, artifacts, receipt=receipt,
                                         manager_events=manager,
                                         actor_events=physical, bundle=bundle)


def _adaptive_causality_with_conflicting_ordinary_candidate(
        root: Path, *, outcome: str, same_observation_id: bool,
        matches_physical_omission: bool = True) -> tuple[dict, dict, list[dict], list[dict], SimpleNamespace]:
    """Insert an ordinary candidate beside a counted timeout in one prefix."""
    artifacts, receipt, manager, physical, bundle = _adaptive_causality_fixture(root)
    snapshot = json.loads((root / artifacts["manager_evidence_snapshot"]["path"]).read_text())
    snapshot["current_cutoff"] = 7
    snapshot["accepted_prefix_count"] = 7
    artifacts["manager_evidence_snapshot"] = _write(
        root, artifacts["manager_evidence_snapshot"]["path"], _canonical(snapshot))
    bundle.evidence_cutoff = 7

    arm = manager[0]
    accepted = [event for event in manager
                if event["event_type"] == "evidence.observation_accepted"]
    snapshot_event = next(event for event in manager
                          if event["event_type"] == "adaptive_v2_evidence_snapshot")
    terminal = next(event for event in manager
                    if event["event_type"] == "adaptive_v2_session_terminal")
    candidate_observation = dict(accepted[0]["payload"]["observation"])
    if not same_observation_id:
        candidate_observation["observation_id"] = "f" * 64
    if not matches_physical_omission:
        candidate_observation["block_hash"] = "e" * 64
    candidate_observation.update(
        outcome=outcome,
        response_duration_us=1 if outcome == "on_time" else 1_000_001,
        signer_set=[1],
    )
    candidate = _event("adaptive-manager", 2, 4,
                       "evidence.observation_accepted", {
                           "ingestion_sequence": 1,
                           "observation": candidate_observation,
                       })
    shifted = []
    for sequence, event in enumerate(accepted, start=3):
        payload = dict(event["payload"])
        payload["ingestion_sequence"] += 1
        shifted.append(dict(event, source_sequence=sequence, payload=payload))
    return (
        artifacts,
        receipt,
        [
            arm,
            candidate,
            *shifted,
            dict(snapshot_event, source_sequence=9, payload=snapshot),
            dict(terminal, source_sequence=10,
                 payload=dict(terminal["payload"], current_evidence_cutoff=7)),
        ],
        physical,
        bundle,
    )


@pytest.mark.parametrize("same_observation_id", [True, False],
                         ids=["same-observation-id", "sibling-attempt"])
def test_adaptive_causality_rejects_ordinary_candidate_that_contradicts_timeout(
        tmp_path: Path, same_observation_id: bool) -> None:
    artifacts, receipt, manager, physical, bundle = (
        _adaptive_causality_with_conflicting_ordinary_candidate(
            tmp_path, outcome="on_time", same_observation_id=same_observation_id))
    with pytest.raises(subject.ValidationError, match="ordinary candidate"):
        subject._validate_adaptive_causality(tmp_path, artifacts, receipt=receipt,
                                             manager_events=manager,
                                             actor_events=physical, bundle=bundle)


def test_adaptive_causality_rejects_late_candidate_before_snapshot(tmp_path: Path) -> None:
    artifacts, receipt, manager, physical, bundle = (
        _adaptive_causality_with_conflicting_ordinary_candidate(
            tmp_path, outcome="late", same_observation_id=False,
            matches_physical_omission=False))
    with pytest.raises(subject.ValidationError, match="late candidate"):
        subject._validate_adaptive_causality(tmp_path, artifacts, receipt=receipt,
                                             manager_events=manager,
                                             actor_events=physical, bundle=bundle)


def test_adaptive_causality_rejects_ordinary_candidate_with_unclaimed_physical_omission(
        tmp_path: Path) -> None:
    """An extra valid reporter-4 timeout cannot hide its physical omission.

    Reporter 4 still has two independent counted timeouts (the frozen minimum).
    The third physical context is represented only by an ordinary ``on_time``
    observation, modelling a mutation of one of more than two accepted
    timeouts.  Cardinality alone must therefore not make this pass.
    """
    artifacts, receipt, manager, physical, bundle = _adaptive_causality_fixture(tmp_path)
    e0 = receipt["launch_binding"]["e0_digest"]
    extra_block = f"{7:064x}"
    physical.append(_event(
        "replica-1", 7, 17, "fault.contribution_opportunity", {
            "actor": 1, "physical_role": "internal",
            "scheduled_action": "omit_aggregate", "parent_replica": 4,
            "proposal": {"epoch_number": 0, "tree_id": 4,
                         "epoch_digest": e0, "block_hash": extra_block},
        }))

    snapshot = json.loads((tmp_path / artifacts["manager_evidence_snapshot"]["path"]).read_text())
    snapshot["current_cutoff"] = 7
    snapshot["accepted_prefix_count"] = 7
    artifacts["manager_evidence_snapshot"] = _write(
        tmp_path, artifacts["manager_evidence_snapshot"]["path"], _canonical(snapshot))
    bundle.evidence_cutoff = 7
    snapshot_event = next(event for event in manager
                          if event["event_type"] == "adaptive_v2_evidence_snapshot")
    terminal = next(event for event in manager
                    if event["event_type"] == "adaptive_v2_session_terminal")
    manager = [
        *manager[:7],
        _event("adaptive-manager", 8, 1_000_000_040,
               "evidence.observation_accepted", {
                   "ingestion_sequence": 7,
                   "observation": {
                       "schema_version": 3, "observation_id": f"{17:064x}",
                       "reporter_id": 4, "observed_replica_id": 1,
                       "configuration": {"epoch_number": 0, "tree_id": 4,
                                         "epoch_digest": e0},
                       "block_hash": extra_block,
                       "expected_message_type": "aggregate_relay",
                       "outcome": "on_time", "response_duration_us": 1,
                       "signer_set": [1], "attempt_start_monotonic_ns": 3,
                       "deadline_duration_us": 1_000_000,
                       "reporter_monotonic_ns": 1_000_000_020,
                   },
               }),
        dict(snapshot_event, source_sequence=9, payload=snapshot),
        dict(terminal, source_sequence=10,
             payload=dict(terminal["payload"], current_evidence_cutoff=7)),
    ]

    with pytest.raises(subject.ValidationError, match="ordinary candidate.*physical omission"):
        subject._validate_adaptive_causality(tmp_path, artifacts, receipt=receipt,
                                             manager_events=manager,
                                             actor_events=physical, bundle=bundle)
