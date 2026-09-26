"""Adversarial tests for producer-bound W16 output validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from experiments.adaptive.kauri_experiment import n31_static_e0_feasibility as feasibility
from experiments.adaptive.kauri_experiment import static_e0_cpu_contract
from experiments.adaptive.kauri_experiment.w16_output_validator import validate_w16_output


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
    slow_throttling: bool = True,
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
        for tree in range(1, 21):
            add("adaptive.configuration_active", _active(replica, tree, digest))
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
            "required_complete_cycles": 5, "hard_timeout_s": 300,
            "external_timeout_s": 540, "automatic_retries": 0,
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


def test_cpu_free_output_passes_only_bounded_feasibility(tmp_path: Path) -> None:
    result = validate_w16_output(_build_output(tmp_path / "run"))
    assert result["verdict"] == "PASS", result
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
        quota_mode="heterogeneous", output=root, hard_timeout_s=300,
    ) == (root / "authorization.json").read_bytes()
    scope = json.loads(
        (root / "runtime/cpu-quota-scope-termination.json").read_text()
    )["units"][0]
    assert scope["worker_live_at_cleanup"] is True
    assert scope["member_pids_before_kill"] == [1000]


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
