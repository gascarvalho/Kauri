"""Adversarial fixtures for the W16 raw-evidence validator."""

from __future__ import annotations

import base64
import importlib


def _module():
    return importlib.import_module("experiments.adaptive.kauri_experiment.static_resource_n31_validator")


def _event(event_type: str, timestamp: int, instance: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    event: dict[str, object] = {"event_type": event_type, "source_monotonic_ns": timestamp, "source_instance": instance}
    if payload is not None:
        event["payload"] = payload
    return event


def _raw(*, arm: str = "slow-roots", quota_mode: str = "heterogeneous-25-100") -> dict[str, object]:
    module = _module()
    revision = "a" * 40
    schedule = module.build_schedule(arm)
    treegen = module.render_treegen_bytes(schedule)
    replicas = []
    streams: dict[str, list[dict[str, object]]] = {}
    digest = "b" * 64
    for replica in range(31):
        instance = f"attempt-replica-{replica}"
        quota = 100 if quota_mode == "homogeneous-100" or replica >= 6 else 25
        throttled = 10 if replica < 6 else 0
        replicas.append({
            "replica_id": replica, "pid": replica + 100, "process_group_id": replica + 100,
            "source_instance": instance,
            "cpu": {"quota_percent": quota, "cgroup_path": f"/user.slice/w16-{replica}", "samples": [
                {"source_monotonic_ns": 90, "usage_usec": 1, "user_usec": 1, "system_usec": 0, "nr_periods": 1, "nr_throttled": 0, "throttled_usec": 0},
                {"source_monotonic_ns": 210, "usage_usec": 20, "user_usec": 17, "system_usec": 3, "nr_periods": 2, "nr_throttled": 1 if throttled else 0, "throttled_usec": throttled},
            ]},
        })
        events = [_event("process.ready", 91, instance)]
        events.extend(_event("adaptive.configuration_active", 100 + tree, instance, {"tree_id": tree, "epoch_digest": digest}) for tree in list(range(21)) + [0])
        events.append(_event("adaptive_v2_reporting_terminal", 130, instance))
        streams[f"replica-{replica}"] = events
    observer = streams["replica-2"]
    observer.extend([
        _event("block.committed", 150, "attempt-replica-2", {"block_height": 10, "block_hash": "c" * 64, "parent_hash": "d" * 64, "designated_observer": True, "decision_proof": {"configuration": {"epoch_digest": digest}, "block_hash": "c" * 64}}),
        _event("block.committed", 160, "attempt-replica-2", {"block_height": 11, "block_hash": "e" * 64, "parent_hash": "c" * 64, "designated_observer": True, "decision_proof": {"configuration": {"epoch_digest": digest}, "block_hash": "e" * 64}}),
    ])
    inventory = [{"path": "config/epoch0-treegen.conf", "sha256": "f" * 64, "bytes": len(treegen)}]
    inventory.extend({"path": f"raw/replica-{replica}.jsonl", "sha256": "f" * 64, "bytes": 1} for replica in range(31))
    return {
        "schema": module.SCHEMA, "arm": arm,
        "source_config_identity": module.source_config_identity(schedule, revision),
        "treegen_base64": base64.b64encode(treegen).decode("ascii"),
        "replicas": replicas, "event_streams": streams,
        "measurement": {"start_monotonic_ns": 140, "end_monotonic_ns": 200, "quota_mode": quota_mode},
        "cleanup": [{"replica_id": replica, "process_group_id": replica + 100, "terminated": True, "cgroup_removed": True} for replica in range(31)],
        "artifact_inventory": inventory,
    }


def test_pass_is_hard_disabled_until_the_runner_emits_raw_byte_receipts() -> None:
    result = _module().validate_raw_evidence(_raw())
    assert result["verdict"] == "INCOMPLETE"
    assert result["authoritative_commit_count"] == 2
    assert "PASS disabled" in result["reason"]


def test_rejects_fabricated_tree_bytes_even_with_matching_summary_identity() -> None:
    raw = _raw()
    raw["treegen_base64"] = base64.b64encode(b"fabricated\n").decode("ascii")
    result = _module().validate_raw_evidence(raw)
    assert result["verdict"] == "INCOMPLETE"
    assert "treegen" in result["reason"]


def test_fabricated_inventory_hashes_cannot_create_a_pass() -> None:
    raw = _raw()
    assert raw["artifact_inventory"][0]["sha256"] == "f" * 64
    result = _module().validate_raw_evidence(raw)
    assert result["verdict"] == "INCOMPLETE"
    assert "raw-byte receipts" in result["reason"]


def test_rejects_missing_replica_cpu_window_coverage() -> None:
    raw = _raw()
    raw["replicas"][7]["cpu"]["samples"][-1]["source_monotonic_ns"] = 199
    result = _module().validate_raw_evidence(raw)
    assert result["verdict"] == "INCOMPLETE"
    assert "cover" in result["reason"]


def test_rejects_non_authoritative_or_broken_commit_chain() -> None:
    raw = _raw()
    raw["event_streams"]["replica-2"][-1]["payload"]["parent_hash"] = "d" * 64
    result = _module().validate_raw_evidence(raw)
    assert result["verdict"] == "INCOMPLETE"
    assert "continuous chain" in result["reason"]


def test_rejects_successor_and_unproven_cleanup() -> None:
    raw = _raw()
    raw["event_streams"]["replica-1"].append(_event("epoch.command_committed", 170, "attempt-replica-1"))
    raw["cleanup"][0]["terminated"] = False
    result = _module().validate_raw_evidence(raw)
    assert result["verdict"] == "INCOMPLETE"
    assert "successor" in result["reason"]


def test_rejects_root_exposed_heterogeneous_arm_without_throttling() -> None:
    raw = _raw()
    for replica in raw["replicas"][:6]:
        replica["cpu"]["samples"][-1]["throttled_usec"] = 0
    result = _module().validate_raw_evidence(raw)
    assert result["verdict"] == "INCOMPLETE"
    assert "throttling" in result["reason"]
