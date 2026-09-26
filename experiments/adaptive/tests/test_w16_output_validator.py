"""Adversarial tests for producer-bound W16 output validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive.kauri_experiment import n31_static_e0_feasibility as feasibility
from experiments.adaptive.kauri_experiment import static_e0_cpu_contract
from experiments.adaptive.kauri_experiment.w16_output_validator import (
    validate_w16_output,
    validate_w16_output_v3,
    validate_w16_output_v4,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _envelope(run_id: str, replica: int, sequence: int, timestamp: int,
              event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "event_schema_version": 1, "run_id": run_id, "source_kind": "replica",
        "source_id": f"replica-{replica}",
        "source_instance": f"{run_id}-replica-{replica}",
        "source_sequence": sequence, "source_monotonic_ns": timestamp,
        "event_type": event_type, "payload": payload,
    }


def _active(replica: int, tree: int, digest: str) -> dict[str, object]:
    return {
        "epoch_number": 0, "tree_id": tree, "epoch_digest": digest,
        "block_hash": None, "context_generation": None,
        "observer_replica": replica, "wait_exempt_signers": [],
        "accepted_signers": [], "absent_direct_children": [],
        "missing_optional_signers": [], "required_branch_gaps": [],
        "root_signer_count": 0, "global_quorum": 21, "rejection_reason": None,
    }


def _hash(height: int) -> str:
    return hashlib.sha256(f"block-{height}".encode()).hexdigest()


def _commit(height: int, tree: int, digest: str, *, observer: bool) -> dict[str, object]:
    return {
        "block_height": height, "block_hash": _hash(height),
        "parent_hash": _hash(height - 1), "transaction_count": 1000,
        "designated_observer": observer,
        "decision_proof": {
            "epoch_number": 0, "tree_id": tree, "epoch_digest": digest,
            "block_hash": _hash(height),
        },
        "view_generation": height + 1, "commit_batch_index": 0,
    }


def _preflight(plan: object) -> dict[str, object]:
    binaries = {name: f"/frozen/{name}" for name in ("app", "keygen", "tls_keygen", "native_digest")}
    return {
        "schema_version": 1, "kind": feasibility.SCHEMA,
        "verdict": "PREFLIGHT_OK_NO_EXECUTION", "revision": "a" * 40,
        "profile_id": plan.profile.profile_id,
        "profile_sha256": plan.profile.sha256, "arm": plan.arm,
        "replica_count": 31, "quorum": 21, "tree_count": 21,
        "treegen_sha256": plan.treegen_sha256,
        "manager_endpoint": "127.0.0.1:27991", "binaries": binaries,
        "binary_sha256": {name: hashlib.sha256(name.encode()).hexdigest() for name in binaries},
        "limitations": ["test fixture"],
    }


def _contract_document(contract: object) -> dict[str, object]:
    return {
        "schema_version": contract.schema_version, "contract_id": contract.contract_id,
        "enabled": contract.enabled, "figure_eligible": contract.figure_eligible,
        "launcher": contract.launcher, "manager_visibility": contract.manager_visibility,
        "sampling_interval_ms": contract.sampling_interval_ms,
        "base_profile_id": contract.base_profile_id,
        "base_profile_sha256": contract.base_profile_sha256,
        "base_profile_canonical_sha256": contract.base_profile_canonical_sha256,
        "assignments": [
            {"replica_id": row.replica_id, "capacity_class": row.capacity_class,
             "cpu_quota_percent": row.cpu_quota_percent}
            for row in contract.assignments
        ],
    }


def _build_output(
    root: Path, *, cpu_mode: str | None = None, arm: str = "slow-roots",
    slow_throttling: bool = True, terminal_after_tree: int | None = None,
) -> Path:
    root.mkdir()
    for name in ("config", "raw", "logs"):
        (root / name).mkdir()
    plan = feasibility.frozen_plan(arm=arm)
    run_id = "w16-local-test"
    digest = {
        "slow-roots": "827e7626c74f8d815bca6ae5cbe10e312bc4f00f287e67d41277b8d689b21c0f",
        "fast-roots": "e640d31a0f4c394ca1ed50005c9f387fdc25de0b67c9ae8eae158275189e462e",
    }[arm]
    (root / "config/epoch0-treegen.conf").write_bytes(plan.treegen_bytes)
    main = [
        "block-size = 1000", "proposer = 0", "fan-out = 5", "async_blocks = 2",
        "tree-generation = file",
        f"tree-generation-fpath = {root.resolve() / 'config/epoch0-treegen.conf'}",
        "tree-switch-period = 1", "epoch-protocol-mode = adaptive_v2",
        "epoch-manager-address = 127.0.0.1:27991",
    ] + [f"replica = 127.0.0.1:{29100 + replica};{30100 + replica}, key, cert"
         for replica in range(31)]
    (root / "config/main.conf").write_text("\n".join(main) + "\n", encoding="utf-8")

    observer_start = observer_end = 0
    for replica in range(31):
        (root / "logs" / f"replica-{replica}.log").write_bytes(b"")
        config = (
            "privkey = key\ntls-privkey = tls\ntls-cert = cert\n"
            f"idx = {replica}\nstructured-event-run-id = {run_id}\n"
            f"structured-event-source-instance = {run_id}-replica-{replica}\n"
            "structured-event-commit-observer-id = replica-2\n"
            f"structured-event-commit-observer-instance = {run_id}-replica-2\n"
            f"structured-event-output = {root.resolve() / 'raw' / f'replica-{replica}.jsonl'}\n"
        )
        (root / "config" / f"replica-{replica}.conf").write_text(config, encoding="utf-8")
        events: list[dict[str, object]] = []
        sequence = 0

        def add(kind: str, payload: dict[str, object]) -> None:
            nonlocal sequence
            sequence += 1
            events.append(_envelope(run_id, replica, sequence,
                                    1_000_000_000 + sequence * 10_000_000,
                                    kind, payload))

        add("process.started", {"exit_status": None})
        add("adaptive.configuration_active", _active(replica, 0, digest))
        add("process.ready", {"exit_status": None})
        terminal_emitted = False
        for tree in range(1, 21):
            add("adaptive.configuration_active", _active(replica, tree, digest))
            if tree == terminal_after_tree:
                add("adaptive_v2_reporting_terminal", {
                    "reason": "shared_outbox_delivery_failed",
                    "terminal_monotonic_ns": 1_000_000_000 + (sequence + 1) * 10_000_000,
                })
                terminal_emitted = True
        if not terminal_emitted:
            add("adaptive_v2_reporting_terminal", {
                "reason": "shared_outbox_delivery_failed",
                "terminal_monotonic_ns": 1_000_000_000 + (sequence + 1) * 10_000_000,
            })
        if cpu_mode is None:
            if replica == 2:
                add("block.committed", _commit(1, 0, digest, observer=True))
                add("block.committed", _commit(2, 1, digest, observer=True))
        elif replica == 2:
            height = 1
            for _cycle in range(5):
                for tree in range(21):
                    add("adaptive.configuration_active", _active(replica, tree, digest))
                    if _cycle == 0 and tree == 0:
                        observer_start = int(events[-1]["source_monotonic_ns"])
                    add("block.committed", _commit(height, tree, digest, observer=True))
                    height += 1
            add("adaptive.configuration_active", _active(replica, 0, digest))
            observer_end = int(events[-1]["source_monotonic_ns"])
        add("process.stopping", {"exit_status": None})
        add("process.stopped", {"exit_status": None})
        with (root / "raw" / f"replica-{replica}.jsonl").open("w", encoding="utf-8") as output:
            for event in events:
                output.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")

    preflight = _preflight(plan)
    quota_sha = None
    quota_cleanup = None
    quota_scope_cleanup = None
    authorization_sha = None
    if cpu_mode is not None:
        contract = static_e0_cpu_contract.frozen_contract(plan, cpu_mode)
        _write_json(root / "runtime/cpu-quota-contract.json", _contract_document(contract))
        replicas = []
        for assignment in contract.assignments:
            replica = assignment.replica_id
            replicas.append({
                "replica_id": replica, "cpu_quota_percent": assignment.cpu_quota_percent,
                "unit": f"kauri-{replica}.scope", "control_group": f"/user/kauri-{replica}.scope",
                "cpu_stat_path": f"/sys/fs/cgroup/user/kauri-{replica}.scope/cpu.stat",
                "owned_pid": 1000 + replica, "owned_pgid": 1000 + replica,
                "cgroup_pids": [1000 + replica], "active_state": "active",
                "sub_state": "running",
                "cpu_quota_per_second_usec": assignment.cpu_quota_percent * 10_000,
            })
        _write_json(root / "runtime/cpu-quota-launch.json", {
            "schema_version": 1, "launcher": contract.launcher,
            "contract_id": contract.contract_id, "contract_sha256": contract.contract_sha256,
            "manager_visibility": "none", "replicas": replicas,
        })
        duration = observer_end - observer_start
        sample_times = [
            observer_start - 100_000_000,
            observer_start + duration // 3,
            observer_start + 2 * duration // 3,
            observer_end + 100_000_000,
        ]
        with (root / "raw/cpu-quota-samples.jsonl").open("w", encoding="utf-8") as output:
            for round_index, timestamp in enumerate(sample_times):
                for assignment in contract.assignments:
                    replica = assignment.replica_id
                    row = {
                        "schema_version": 1, "source_monotonic_ns": timestamp,
                        "replica_id": replica, "cpu_quota_percent": assignment.cpu_quota_percent,
                        "unit": f"kauri-{replica}.scope",
                        "control_group": f"/user/kauri-{replica}.scope",
                        "cpu_stat_path": f"/sys/fs/cgroup/user/kauri-{replica}.scope/cpu.stat",
                        "cpu_quota_per_second_usec": assignment.cpu_quota_percent * 10_000,
                        "active_state": "active", "sub_state": "running",
                        "cpu_stat": {
                            "usage_usec": round_index * 100 + replica,
                            "user_usec": round_index * 60 + replica,
                            "system_usec": round_index * 40,
                            "nr_periods": round_index * 10,
                            "nr_throttled": round_index * (
                                1 if slow_throttling and assignment.cpu_quota_percent < 100 else 0
                            ),
                            "throttled_usec": round_index * (
                                10 if slow_throttling and assignment.cpu_quota_percent < 100 else 0
                            ),
                        },
                    }
                    output.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        with (root / "raw/cpu-quota-monitor-rounds.jsonl").open("w", encoding="utf-8") as output:
            for ordinal, timestamp in enumerate(sample_times):
                row = {
                    "schema_version": 1, "round_ordinal": ordinal,
                    "scheduled_monotonic_ns": timestamp - 2,
                    "started_monotonic_ns": timestamp - 1,
                    "sample_monotonic_ns": timestamp,
                    "finished_monotonic_ns": timestamp + 1,
                    "duration_ns": 2, "start_lateness_ns": 1,
                    "completion_overrun_ns": 0,
                }
                output.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        quota_cleanup = {
            "schema_version": 1, "complete": True,
            "monitor": {"status": "PASSED", "stopped": True},
            "units": [
                {"replica_id": replica, "unit": f"kauri-{replica}.scope",
                 "launch_verified": True, "load_state": "not-found",
                 "active_state": "inactive", "sub_state": "dead", "control_group": ""}
                for replica in range(31)
            ],
        }
        _write_json(root / "runtime/cpu-quota-cleanup.json", quota_cleanup)
        quota_scope_cleanup = {
            "schema_version": 1, "complete": True, "deadline_ns": 8_000_000_000,
            "deadline_exhausted": False,
            "units": [
                {
                    "replica_id": replica, "unit": f"kauri-{replica}.scope",
                    "ownership_verified": True, "invocation_id": f"invocation-{replica}",
                    "control_group": f"/user/kauri-{replica}.scope",
                    "worker_pid": 1000 + replica, "worker_pgid": 1000 + replica,
                    "cgroup_dev": 10, "cgroup_ino": 100 + replica,
                    "worker_live_at_cleanup": True,
                    "member_pids_before_kill": [1000 + replica],
                    "identity_revalidated": True, "kill_attempted": True,
                    "populated_after_kill": 0, "cgroup_removed_after_kill": False,
                    "status": "killed_and_empty", "error": None,
                }
                for replica in range(31)
            ],
        }
        _write_json(
            root / "runtime/cpu-quota-scope-termination.json", quota_scope_cleanup
        )
        authorization = {
            "schema_version": 1,
            "kind": "kauri-w16-static-e0-exploratory-authorization-v1",
            "block_id": "w16-static-e0-test", "block_order": [
                "slow-roots:homogeneous", "fast-roots:homogeneous",
                "slow-roots:heterogeneous", "fast-roots:heterogeneous",
            ],
            "cell_ordinal": [
                "slow-roots:homogeneous", "fast-roots:homogeneous",
                "slow-roots:heterogeneous", "fast-roots:heterogeneous",
            ].index(f"{arm}:{cpu_mode}") + 1,
            "revision": preflight["revision"], "profile_sha256": preflight["profile_sha256"],
            "arm": arm, "quota_mode": cpu_mode,
            "preflight_sha256": hashlib.sha256(_canonical_for_test(preflight)).hexdigest(),
            "binary_sha256": preflight["binary_sha256"], "output_root": str(root.resolve()),
            "required_complete_cycles": 5, "hard_timeout_s": 480,
            "external_timeout_s": 720, "automatic_retries": 0,
            "claim_eligible": False, "figure_eligible": False,
            "approval_ref": "user-confirmation:2026-09-26:inesc-cpu-throughput",
            "approved_at_utc": "2026-09-26T12:00:00Z",
        }
        _write_json(root / "authorization.json", authorization)
        authorization_sha = _digest(root / "authorization.json")
        quota_sha = contract.contract_sha256

    raw_hashes = {f"replica-{replica}": _digest(root / "raw" / f"replica-{replica}.jsonl")
                  for replica in range(31)}
    artifacts = {
        path.relative_to(root).as_posix(): _digest(path)
        for path in sorted(root.rglob("*")) if path.is_file()
    }
    receipt = {
        "schema": "kauri-n31-static-e0-local-executor-v1", "run_id": run_id,
        "attempts": 1, "retries": 0, "verdict": "PASS", "failure": None,
        "cleanup_error": None, "registered_replica_ids": list(range(31)),
        "all_registered_exited": True, "treegen_sha256": plan.treegen_sha256,
        "raw_sha256": raw_hashes, "artifact_sha256": artifacts,
        "quota_contract_sha256": quota_sha, "authorization_sha256": authorization_sha,
        "quota_cleanup": quota_cleanup, "required_complete_cycles": 5 if cpu_mode else 1,
        "quota_scope_cleanup": quota_scope_cleanup,
        "preflight": preflight, "epoch_zero_digest": digest,
        "started_monotonic_ns": 1, "ended_monotonic_ns": 9_000_000_000,
    }
    _write_json(root / "feasibility-receipt.json", receipt)
    return root


def _canonical_for_test(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _reseal(root: Path) -> None:
    receipt_path = root / "feasibility-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["raw_sha256"] = {
        f"replica-{replica}": _digest(root / "raw" / f"replica-{replica}.jsonl")
        for replica in range(31)
    }
    receipt["artifact_sha256"] = {
        path.relative_to(root).as_posix(): _digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "feasibility-receipt.json"
    }
    _write_json(receipt_path, receipt)


def _add_required_branch_incomplete(
    root: Path, *, direct_child: int = 4, missing_signers: list[int] | None = None,
    root_signer_count: int = 0, global_quorum: int = 0, replica: int = 6,
    tree_id: int = 17,
) -> None:
    """Insert one v3 diagnostic at replica 6 inside the CPU window."""

    observer_rows = [
        json.loads(line)
        for line in (root / "raw/replica-2.jsonl").read_text().splitlines()
    ]
    terminal = next(
        row for row in observer_rows
        if row["event_type"] == "adaptive_v2_reporting_terminal"
    )
    start = next(
        row["source_monotonic_ns"] for row in observer_rows
        if row["event_type"] == "adaptive.configuration_active"
        and row["source_sequence"] > terminal["source_sequence"]
        and row["payload"]["tree_id"] == 0
    )
    path = root / "raw" / f"replica-{replica}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    insertion = next(
        index for index, row in enumerate(rows)
        if row["event_type"] == "process.stopping"
    )
    payload = {
        "epoch_number": 0,
        "tree_id": tree_id,
        "epoch_digest": "827e7626c74f8d815bca6ae5cbe10e312bc4f00f287e67d41277b8d689b21c0f",
        "block_hash": _hash(9_999),
        "context_generation": 1,
        "observer_replica": replica,
        "wait_exempt_signers": [],
        "accepted_signers": [],
        "absent_direct_children": [],
        "missing_optional_signers": [],
        "required_branch_gaps": [{
            "direct_child": direct_child,
            "missing_required_signers": (
                [direct_child] if missing_signers is None else missing_signers
            ),
        }],
        "root_signer_count": root_signer_count,
        "global_quorum": global_quorum,
        "rejection_reason": None,
    }
    rows.insert(insertion, _envelope(
        str(rows[0]["run_id"]), replica, 0, int(start) + 1,
        "aggregation.required_branch_incomplete", payload,
    ))
    for sequence, row in enumerate(rows, 1):
        row["source_sequence"] = sequence
        row["source_monotonic_ns"] = 1_000_000_000 + sequence * 10_000_000
    inserted = rows[insertion]
    inserted["source_monotonic_ns"] = int(start) + 1
    for row in rows[insertion + 1:]:
        row["source_monotonic_ns"] = max(
            int(row["source_monotonic_ns"]), int(start) + 2
        )
    path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ), encoding="utf-8")
    _reseal(root)


def _mutate_required_branch_payload(root: Path, mutation: str) -> None:
    path = root / "raw/replica-6.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    payload = next(
        row["payload"] for row in rows
        if row["event_type"] == "aggregation.required_branch_incomplete"
    )
    if mutation == "wrong-epoch":
        payload["epoch_number"] = 1
    elif mutation == "wrong-digest":
        payload["epoch_digest"] = "f" * 64
    elif mutation == "tree-out-of-range":
        payload["tree_id"] = 21
    elif mutation == "bad-block-hash":
        payload["block_hash"] = "not-a-digest"
    elif mutation == "zero-context":
        payload["context_generation"] = 0
    elif mutation == "wrong-observer":
        payload["observer_replica"] = 5
    elif mutation == "wait-exempt":
        payload["wait_exempt_signers"] = [1]
    elif mutation == "accepted":
        payload["accepted_signers"] = [1]
    elif mutation == "absent":
        payload["absent_direct_children"] = [1]
    elif mutation == "missing-optional":
        payload["missing_optional_signers"] = [1]
    elif mutation == "rejection":
        payload["rejection_reason"] = "forged"
    elif mutation == "empty-gaps":
        payload["required_branch_gaps"] = []
    elif mutation == "duplicate-gap-child":
        payload["required_branch_gaps"].append({
            "direct_child": 4, "missing_required_signers": [4],
        })
    elif mutation == "unsorted-gap-child":
        payload["required_branch_gaps"].append({
            "direct_child": 3, "missing_required_signers": [3],
        })
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ), encoding="utf-8")
    _reseal(root)


def _add_delta_success_triplet(root: Path) -> None:
    path = root / "raw/replica-6.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    timeout_index = next(
        index for index, row in enumerate(rows)
        if row["event_type"] == "aggregation.required_branch_incomplete"
    )
    timeout = rows[timeout_index]
    payload = dict(timeout["payload"])
    payload.update({
        "accepted_signers": [4], "required_branch_gaps": [],
    })
    additions = [
        _envelope(str(rows[0]["run_id"]), 6, 0,
                  int(timeout["source_monotonic_ns"]) + offset, event_type, dict(payload))
        for offset, event_type in enumerate((
            "aggregation.delta_reserved", "aggregation.delta_enqueued",
            "aggregation.delta_committed",
        ), 1)
    ]
    rows[timeout_index + 1:timeout_index + 1] = additions
    for sequence, row in enumerate(rows, 1):
        row["source_sequence"] = sequence
    floor = int(additions[-1]["source_monotonic_ns"]) + 1
    for row in rows[timeout_index + 4:]:
        row["source_monotonic_ns"] = max(int(row["source_monotonic_ns"]), floor)
    path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ), encoding="utf-8")
    _reseal(root)


def test_cpu_free_output_passes_only_bounded_feasibility(tmp_path: Path) -> None:
    result = validate_w16_output(_build_output(tmp_path / "run"))
    assert result["verdict"] == "PASS", result
    assert result["kind"] == "kauri-w16-output-validation-v2"
    assert result["evidence_class"] == "CPU_FREE_FEASIBILITY"
    assert result["throughput"] is None
    assert result["claim_eligible"] is False


def test_cpu_output_derives_commit_throughput_and_checks_quota(tmp_path: Path) -> None:
    result = validate_w16_output(_build_output(tmp_path / "run", cpu_mode="heterogeneous"))
    assert result["verdict"] == "PASS", result
    assert result["evidence_class"] == "CPU_QUOTA_SINGLE_ARM"
    assert result["throughput"]["transaction_count"] > 0
    assert result["quota"]["mode"] == "heterogeneous"
    assert result["authorization"]["approval_ref"].startswith("user-confirmation:")
    assert result["claim_eligible"] is False


def test_v3_reports_zero_timeout_diagnostics_without_changing_v2(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    result = validate_w16_output_v3(root)
    assert result["verdict"] == "PASS", result
    assert result["kind"] == "kauri-w16-output-validation-v3"
    assert result["required_branch_incomplete"] == {
        "schema_version": 1,
        "event_type": "aggregation.required_branch_incomplete",
        "total_count": 0,
        "pre_measurement_count": 0,
        "in_measurement_count": 0,
        "post_measurement_count": 0,
        "by_replica": [],
        "gap_count": 0,
        "missing_signer_count": 0,
        "by_tree": [],
        "by_direct_child": [],
    }


def test_v3_admits_and_counts_a_valid_required_branch_diagnostic(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    _add_required_branch_incomplete(root)

    legacy = validate_w16_output(root)
    assert legacy["verdict"] == "FAIL"
    assert legacy["reason_code"] == "unexpected_native_event"

    result = validate_w16_output_v3(root)
    assert result["verdict"] == "PASS", result
    assert result["kind"] == "kauri-w16-output-validation-v3"
    assert result["required_branch_incomplete"] == {
        "schema_version": 1,
        "event_type": "aggregation.required_branch_incomplete",
        "total_count": 1,
        "pre_measurement_count": 0,
        "in_measurement_count": 1,
        "post_measurement_count": 0,
        "by_replica": [{
            "replica_id": 6,
            "count": 1,
            "pre_measurement_count": 0,
            "in_measurement_count": 1,
            "post_measurement_count": 0,
        }],
        "gap_count": 1,
        "missing_signer_count": 1,
        "by_tree": [{
            "tree_id": 17,
            "event_count": 1,
            "pre_measurement_count": 0,
            "in_measurement_count": 1,
            "post_measurement_count": 0,
        }],
        "by_direct_child": [{
            "replica_id": 4,
            "gap_count": 1,
            "missing_signer_count": 1,
            "pre_measurement_gap_count": 0,
            "in_measurement_gap_count": 1,
            "post_measurement_gap_count": 0,
        }],
    }


def test_v4_admits_only_a_complete_timeout_bound_delta_triplet(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    _add_required_branch_incomplete(root)
    _add_delta_success_triplet(root)

    v3 = validate_w16_output_v3(root)
    assert v3["verdict"] == "FAIL"
    assert v3["reason_code"] == "unexpected_native_event"
    result = validate_w16_output_v4(root)
    assert result["verdict"] == "PASS", result
    assert result["kind"] == "kauri-w16-output-validation-v4"
    assert result["delta_success_triplets"] == {
        "schema_version": 1,
        "event_type": "aggregation.delta_success_triplet",
        "total_count": 1,
        "signer_count": 1,
        "pre_measurement_count": 0,
        "in_measurement_count": 1,
        "post_measurement_count": 0,
        "by_replica": [{
            "replica_id": 6, "triplet_count": 1, "signer_count": 1,
            "pre_measurement_count": 0, "in_measurement_count": 1,
            "post_measurement_count": 0,
        }],
        "by_tree": [{
            "tree_id": 17, "triplet_count": 1, "signer_count": 1,
            "pre_measurement_count": 0, "in_measurement_count": 1,
            "post_measurement_count": 0,
        }],
    }


@pytest.mark.parametrize("mutation", (
    "orphan", "reordered", "signer-mismatch", "released", "rejected",
))
def test_v4_rejects_non_successful_or_incoherent_delta_lifecycles(
    tmp_path: Path, mutation: str,
) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    _add_required_branch_incomplete(root)
    _add_delta_success_triplet(root)
    path = root / "raw/replica-6.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    delta_indexes = [
        index for index, row in enumerate(rows)
        if row["event_type"].startswith("aggregation.delta_")
    ]
    if mutation == "orphan":
        del rows[delta_indexes[0]]
    elif mutation == "reordered":
        rows[delta_indexes[1]]["event_type"] = "aggregation.delta_committed"
        rows[delta_indexes[2]]["event_type"] = "aggregation.delta_enqueued"
    elif mutation == "signer-mismatch":
        rows[delta_indexes[1]]["payload"]["accepted_signers"] = [3]
    elif mutation == "released":
        rows[delta_indexes[1]]["event_type"] = "aggregation.delta_released"
    else:
        rows[delta_indexes[1]]["event_type"] = "aggregation.delta_rejected"
    for sequence, row in enumerate(rows, 1):
        row["source_sequence"] = sequence
    path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ), encoding="utf-8")
    _reseal(root)

    result = validate_w16_output_v4(root)
    assert result["verdict"] == "FAIL", mutation
    assert result["reason_code"] == "delta_triplet"


@pytest.mark.parametrize("mutation", ("no-timeout", "gap-mismatch", "phase-crossing"))
def test_v4_rejects_unbound_or_phase_crossing_delta_triplets(
    tmp_path: Path, mutation: str,
) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    _add_required_branch_incomplete(root)
    _add_delta_success_triplet(root)
    path = root / "raw/replica-6.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if mutation == "no-timeout":
        rows = [row for row in rows if row["event_type"] != "aggregation.required_branch_incomplete"]
    elif mutation == "gap-mismatch":
        for row in rows:
            if row["event_type"].startswith("aggregation.delta_"):
                row["payload"]["accepted_signers"] = [3]
    else:
        observer = [json.loads(line) for line in (root / "raw/replica-2.jsonl").read_text().splitlines()]
        terminal = next(row for row in observer if row["event_type"] == "adaptive_v2_reporting_terminal")
        end = [row for row in observer if row["event_type"] == "adaptive.configuration_active"
               and row["source_sequence"] > terminal["source_sequence"]][-1]["source_monotonic_ns"]
        committed = next(row for row in rows if row["event_type"] == "aggregation.delta_committed")
        committed["source_monotonic_ns"] = int(end) + 1
        for row in rows[rows.index(committed) + 1:]:
            row["source_monotonic_ns"] = max(int(row["source_monotonic_ns"]), int(end) + 2)
    for sequence, row in enumerate(rows, 1):
        row["source_sequence"] = sequence
    path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                            for row in rows), encoding="utf-8")
    _reseal(root)
    result = validate_w16_output_v4(root)
    assert result["verdict"] == "FAIL", mutation
    assert result["reason_code"] == "delta_triplet"


def test_v4_rejects_initial_relay_after_timeout_before_delta(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    _add_required_branch_incomplete(root)
    _add_delta_success_triplet(root)
    path = root / "raw/replica-6.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    timeout_index = next(index for index, row in enumerate(rows)
                         if row["event_type"] == "aggregation.required_branch_incomplete")
    delta_index = timeout_index + 1
    timeout_time = int(rows[timeout_index]["source_monotonic_ns"])
    for row in rows[delta_index:delta_index + 3]:
        row["source_monotonic_ns"] = int(row["source_monotonic_ns"]) + 3
    payload = dict(rows[delta_index]["payload"])
    payload["accepted_signers"] = [6]
    run_id = str(rows[0]["run_id"])
    initial = [
        _envelope(run_id, 6, 0, timeout_time + offset, event_type, dict(payload))
        for offset, event_type in enumerate((
            "aggregation.initial_reserved", "aggregation.initial_enqueued",
            "aggregation.initial_committed",
        ), 1)
    ]
    rows[delta_index:delta_index] = initial
    for row in rows[delta_index + 6:]:
        row["source_monotonic_ns"] = max(
            int(row["source_monotonic_ns"]), timeout_time + 7,
        )
    for sequence, row in enumerate(rows, 1):
        row["source_sequence"] = sequence
    path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    ), encoding="utf-8")
    _reseal(root)

    result = validate_w16_output_v4(root)
    assert result["verdict"] == "FAIL", result
    assert result["reason_code"] == "delta_triplet"


@pytest.mark.parametrize(
    ("kwargs", "label"),
    (
        ({"direct_child": 5}, "non-child direct reporter"),
        ({"missing_signers": [5]}, "signer outside child subtree"),
        ({"root_signer_count": 1, "global_quorum": 21}, "nonzero root quorum"),
    ),
)
def test_v3_rejects_malformed_required_branch_diagnostic(
    tmp_path: Path, kwargs: dict[str, object], label: str,
) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    _add_required_branch_incomplete(root, **kwargs)

    result = validate_w16_output_v3(root)
    assert result["verdict"] == "FAIL", label
    assert result["reason_code"] == "required_branch_incomplete"


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong-epoch", "wrong-digest", "tree-out-of-range", "bad-block-hash",
        "zero-context", "wrong-observer", "wait-exempt", "accepted", "absent",
        "missing-optional", "rejection", "empty-gaps", "duplicate-gap-child",
        "unsorted-gap-child",
    ),
)
def test_v3_rejects_diagnostic_identity_and_schema_mutations(
    tmp_path: Path, mutation: str,
) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    _add_required_branch_incomplete(root)
    _mutate_required_branch_payload(root, mutation)

    result = validate_w16_output_v3(root)
    assert result["verdict"] == "FAIL", mutation
    assert result["reason_code"] == "required_branch_incomplete"


@pytest.mark.parametrize("mutation", ("duplicate", "unsorted", "out-of-range"))
def test_v3_rejects_noncanonical_missing_signers(
    tmp_path: Path, mutation: str,
) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    _add_required_branch_incomplete(
        root, replica=17, tree_id=17, direct_child=6, missing_signers=[0, 4],
    )
    path = root / "raw/replica-17.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    missing = next(
        row["payload"]["required_branch_gaps"][0]["missing_required_signers"]
        for row in rows if row["event_type"] == "aggregation.required_branch_incomplete"
    )
    if mutation == "duplicate":
        missing.append(4)
    elif mutation == "unsorted":
        missing.reverse()
    else:
        missing.append(31)
    path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ), encoding="utf-8")
    _reseal(root)

    result = validate_w16_output_v3(root)
    assert result["verdict"] == "FAIL", mutation
    assert result["reason_code"] == "required_branch_incomplete"


def test_v3_rejects_a_forged_successor_event(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    last = rows[-1]
    rows.append(_envelope(
        str(last["run_id"]), 0, int(last["source_sequence"]) + 1,
        int(last["source_monotonic_ns"]) + 1, "epoch.activated",
        {"epoch_number": 1},
    ))
    path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ), encoding="utf-8")
    _reseal(root)

    result = validate_w16_output_v3(root)
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "unexpected_native_event"


def test_cpu_output_accepts_mid_cycle_terminal_when_all_sources_settle_before_window(
    tmp_path: Path,
) -> None:
    result = validate_w16_output(_build_output(
        tmp_path / "run", cpu_mode="heterogeneous", terminal_after_tree=11,
    ))
    assert result["verdict"] == "PASS", result


def test_cpu_output_rejects_missing_exact_epoch_zero_cycle(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous", terminal_after_tree=11)
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if (row["event_type"] == "adaptive.configuration_active"
                and row["payload"]["tree_id"] == 20):
            row["payload"]["tree_id"] = 19
    path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                            for row in rows), encoding="utf-8")
    _reseal(root)
    result = validate_w16_output(root)
    assert result["verdict"] == "INCOMPLETE"
    assert result["reason_code"] == "epoch_zero_cycle"


def test_cpu_output_rejects_forged_cycle_before_process_started(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous", terminal_after_tree=11)
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    digest = next(row["payload"]["epoch_digest"] for row in rows
                  if row["event_type"] == "adaptive.configuration_active")
    for row in rows:
        row["source_sequence"] += 21
        if (row["event_type"] == "adaptive.configuration_active"
                and row["payload"]["tree_id"] == 20):
            row["payload"]["tree_id"] = 19
    run_id = str(rows[0]["run_id"])
    forged = [
        _envelope(run_id, 0, tree + 1, 100_000_000 + tree,
                  "adaptive.configuration_active", _active(0, tree, digest))
        for tree in range(21)
    ]
    path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in forged + rows
    ), encoding="utf-8")
    _reseal(root)
    result = validate_w16_output(root)
    assert result["verdict"] == "INCOMPLETE"
    assert result["reason_code"] == "epoch_zero_cycle"


def test_cpu_output_rejects_terminal_after_global_measurement_start(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous", terminal_after_tree=11)
    observer_rows = [json.loads(line) for line in (root / "raw/replica-2.jsonl").read_text().splitlines()]
    observer_terminal = next(row for row in observer_rows if row["event_type"] == "adaptive_v2_reporting_terminal")
    start = next(row["source_monotonic_ns"] for row in observer_rows
                 if row["event_type"] == "adaptive.configuration_active"
                 and row["source_sequence"] > observer_terminal["source_sequence"]
                 and row["payload"]["tree_id"] == 0)
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    terminal_index = next(index for index, row in enumerate(rows)
                          if row["event_type"] == "adaptive_v2_reporting_terminal")
    delta = start + 1 - rows[terminal_index]["source_monotonic_ns"]
    for row in rows[terminal_index:]:
        row["source_monotonic_ns"] += delta
    path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                            for row in rows), encoding="utf-8")
    _reseal(root)
    result = validate_w16_output(root)
    assert result["verdict"] == "INCOMPLETE"
    assert result["reason_code"] == "settlement_boundary"


def test_cpu_output_rejects_cycle_completion_after_global_measurement_start(
    tmp_path: Path,
) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous", terminal_after_tree=11)
    observer_rows = [json.loads(line) for line in (root / "raw/replica-2.jsonl").read_text().splitlines()]
    observer_terminal = next(row for row in observer_rows if row["event_type"] == "adaptive_v2_reporting_terminal")
    start = next(row["source_monotonic_ns"] for row in observer_rows
                 if row["event_type"] == "adaptive.configuration_active"
                 and row["source_sequence"] > observer_terminal["source_sequence"]
                 and row["payload"]["tree_id"] == 0)
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    first_post_terminal_active = next(index for index, row in enumerate(rows)
                                      if row["event_type"] == "adaptive.configuration_active"
                                      and row["payload"]["tree_id"] == 12)
    delta = start + 1 - rows[first_post_terminal_active]["source_monotonic_ns"]
    for row in rows[first_post_terminal_active:]:
        row["source_monotonic_ns"] += delta
    path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                            for row in rows), encoding="utf-8")
    _reseal(root)
    result = validate_w16_output(root)
    assert result["verdict"] == "INCOMPLETE"
    assert result["reason_code"] == "settlement_boundary"


def test_cpu_fixture_authorization_matches_current_producer_schema(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from experiments.adaptive import run_n31_static_e0_local_executor as cli

    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    receipt = json.loads((root / "feasibility-receipt.json").read_text())
    preflight_bytes = feasibility.canonical_json(receipt["preflight"])
    assert cli._read_cpu_authorization(
        root / "authorization.json", preflight_bytes=preflight_bytes,
        preflight=receipt["preflight"], arm="slow-roots",
        quota_mode="heterogeneous", output=root, hard_timeout_s=480,
    ) == (root / "authorization.json").read_bytes()
    scope = json.loads(
        (root / "runtime/cpu-quota-scope-termination.json").read_text()
    )["units"][0]
    assert scope["worker_live_at_cleanup"] is True
    assert scope["member_pids_before_kill"] == [1000]


@pytest.mark.parametrize("hard_timeout_s", (300, 480.001))
def test_cpu_output_rejects_non_frozen_internal_timeout(
    tmp_path: Path, hard_timeout_s: float,
) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    authorization_path = root / "authorization.json"
    authorization = json.loads(authorization_path.read_text())
    authorization["hard_timeout_s"] = hard_timeout_s
    _write_json(authorization_path, authorization)
    receipt_path = root / "feasibility-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["authorization_sha256"] = _digest(authorization_path)
    _write_json(receipt_path, receipt)
    _reseal(root)

    result = validate_w16_output(root)
    assert result["verdict"] == "INCOMPLETE"
    assert result["reason_code"] == "cpu_authorization"


def test_pipelined_commit_may_use_an_earlier_activated_tree(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    path = root / "raw/replica-2.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    post_terminal = False
    seen_commit = False
    for row in rows:
        if row["event_type"] == "adaptive_v2_reporting_terminal":
            post_terminal = True
        elif post_terminal and row["event_type"] == "block.committed":
            if seen_commit:
                row["payload"]["decision_proof"]["tree_id"] = 0
                break
            seen_commit = True
    path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ), encoding="utf-8")
    _reseal(root)
    result = validate_w16_output(root)
    assert result["verdict"] == "PASS", result


def test_fast_root_heterogeneous_cell_does_not_require_slow_throttling(
    tmp_path: Path,
) -> None:
    result = validate_w16_output(_build_output(
        tmp_path / "run", cpu_mode="heterogeneous", arm="fast-roots",
        slow_throttling=False,
    ))
    assert result["verdict"] == "PASS", result
    assert result["quota"]["slow_root_throttled_usec_delta"] == 0


def test_raw_tamper_fails_before_semantic_reparse(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run")
    with (root / "raw/replica-0.jsonl").open("ab") as output:
        output.write(b"{}\n")
    result = validate_w16_output(root)
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "artifact_hash_mismatch"


def test_resealed_sequence_gap_is_still_rejected(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run")
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[5]["source_sequence"] = 99
    path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                            for row in rows), encoding="utf-8")
    _reseal(root)
    result = validate_w16_output(root)
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "native_envelope"


def test_resealed_successor_event_after_stop_is_rejected(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run")
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    last = rows[-1]
    rows.append(_envelope(
        str(last["run_id"]), 0, int(last["source_sequence"]) + 1,
        int(last["source_monotonic_ns"]) + 1, "epoch.activated",
        {"epoch_number": 1},
    ))
    path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ), encoding="utf-8")
    _reseal(root)
    result = validate_w16_output(root)
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "unexpected_native_event"


def test_resealed_pre_window_cpu_samples_are_rejected(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="homogeneous")
    initial = validate_w16_output(root)
    assert initial["verdict"] == "PASS", initial
    start_ns = initial["throughput"]["window_start_monotonic_ns"]
    sample_path = root / "raw/cpu-quota-samples.jsonl"
    samples = [json.loads(line) for line in sample_path.read_text().splitlines()]
    shifted = [start_ns - offset for offset in (400_000_000, 300_000_000,
                                                200_000_000, 100_000_000)]
    for index, row in enumerate(samples):
        row["source_monotonic_ns"] = shifted[index // 31]
    sample_path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in samples
    ), encoding="utf-8")
    rounds_path = root / "raw/cpu-quota-monitor-rounds.jsonl"
    rounds = [json.loads(line) for line in rounds_path.read_text().splitlines()]
    for timestamp, row in zip(shifted, rounds):
        row["scheduled_monotonic_ns"] = timestamp - 2
        row["started_monotonic_ns"] = timestamp - 1
        row["sample_monotonic_ns"] = timestamp
        row["finished_monotonic_ns"] = timestamp + 1
    rounds_path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rounds
    ), encoding="utf-8")
    _reseal(root)
    result = validate_w16_output(root)
    assert result["verdict"] == "INCOMPLETE"
    assert result["reason_code"] == "cpu_sample_coverage"


def test_resealed_zero_delta_cpu_accounting_is_rejected(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="homogeneous")
    path = root / "raw/cpu-quota-samples.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    baseline = {
        int(row["replica_id"]): dict(row["cpu_stat"])
        for row in rows[:31]
    }
    for row in rows:
        row["cpu_stat"] = dict(baseline[int(row["replica_id"])])
    path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ), encoding="utf-8")
    _reseal(root)
    result = validate_w16_output(root)
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "cpu_accounting_not_observed"


def test_resealed_broken_authoritative_chain_is_rejected(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    path = root / "raw/replica-2.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    commits = [row for row in rows if row["event_type"] == "block.committed"]
    commits[1]["payload"]["parent_hash"] = "f" * 64
    path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                            for row in rows), encoding="utf-8")
    _reseal(root)
    result = validate_w16_output(root)
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "commit_chain"


def test_resealed_quota_assignment_drift_is_rejected(tmp_path: Path) -> None:
    root = _build_output(tmp_path / "run", cpu_mode="heterogeneous")
    path = root / "runtime/cpu-quota-contract.json"
    contract = json.loads(path.read_text())
    contract["assignments"][0]["cpu_quota_percent"] = 100
    _write_json(path, contract)
    _reseal(root)
    result = validate_w16_output(root)
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "cpu_contract"


def test_legacy_real_fixture_fails_closed_without_artifact_inventory() -> None:
    root = Path("/private/tmp/kauri-w16-clean.6v1V1B/slow-feasibility-03")
    if not root.is_dir():
        return
    result = validate_w16_output(root)
    assert result["verdict"] == "INCOMPLETE"
    assert result["reason_code"] in {"receipt_schema", "receipt_missing_artifact_inventory"}
