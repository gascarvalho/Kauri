from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "sustained_role_validator.py"
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
        "canonical signed adaptive-v3 E1 bundle bytes and SHA-256",
        "canonical issuer public-key bytes and SHA-256",
        "independent native bundle decode/signature verification bound to that issuer",
        "decoded E1 predecessor E0 digest, successor E1 digest, and activation height",
        "decoded all-tree N=7 membership with actor 1 wait-exempt leaf in every tree",
        "all-seven epoch.command_committed raw-event bindings to the decoded E0-to-E1 command",
        "all-seven epoch.activated raw events for that exact E1 digest by anchor plus 20 seconds",
        "seven replica JSONL streams, eight logs, and clean eight-process cleanup through anchor plus 60 seconds",
    )


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
