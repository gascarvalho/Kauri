from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kauri_experiment import operator_capacity_raw_validation as subject
from tests.test_operator_capacity_consumption_audit import (
    _canonical,
    _fixture,
    audit as audit_module,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")


def _raw_fixture(tmp_path: Path, *, arm: str = "fast_priority_treatment") -> dict[str, object]:
    authority = _fixture(tmp_path / "authority")
    pins = authority["pins"]
    assert isinstance(pins, dict)
    pins["arm"] = arm
    pins["quota_profile_sha256"] = "f" * 64
    pins["authoritative_replica_id"] = 7
    # Keep the component-chain fixture internally consistent with the desired arm.
    for name in ("record", "stage_a_receipt", "stage_b_receipt"):
        document = authority[name]
        assert isinstance(document, dict)
        document["arm"] = arm
    paths = authority["paths"]
    assert isinstance(paths, dict)
    paths["consumption_record"].write_bytes(_canonical(authority["record"], audit_module._CONSUMPTION_KEYS))
    paths["stage_a_verifier_receipt"].write_bytes(_canonical(authority["stage_a_receipt"], audit_module._STAGE_A_RECEIPT_KEYS))
    paths["stage_b_verifier_receipt"].write_bytes(_canonical(authority["stage_b_receipt"], audit_module._STAGE_B_RECEIPT_KEYS))

    root = tmp_path / "output"; raw = root / "raw"; raw.mkdir(parents=True)
    e0 = str(pins["epoch0_consensus_digest"]); e1 = "d" * 64
    e0_orders = [{"tree_id": tree, "member_order": list(range(31))} for tree in range(21)]
    e1_order = list(range(6, 31)) + list(range(6))
    e1_orders = [{"tree_id": tree, "member_order": e1_order} for tree in range(21)]
    if arm == "exact_copy_sham":
        e1_orders = e0_orders
    roles = {"schema_version": 1, "arm": arm, "epoch0_digest": e0, "epoch1_digest": e1,
             "epoch0": e0_orders, "epoch1": e1_orders}
    _write_json(raw / "roles.json", roles)
    roles_sha = __import__("hashlib").sha256((raw / "roles.json").read_bytes()).hexdigest()
    decision = int(pins["decision_monotonic_raw_ns"])
    _write_jsonl(raw / "manager-events.jsonl", [
        {"event_type": "epoch_active", "epoch_number": 0, "epoch_digest": e0,
         "roles_sha256": roles_sha, "monotonic_raw_ns": decision - 1},
        {"event_type": "epoch_active", "epoch_number": 1, "epoch_digest": e1,
         "roles_sha256": roles_sha, "monotonic_raw_ns": decision},
    ])
    bundle_sha = __import__("hashlib").sha256(paths["successor_bundle"].read_bytes()).hexdigest()
    _write_json(raw / "manager-terminal.json", {
        "schema_version": 1, "run_id": pins["run_id"],
        "source_instance": pins["source_instance"], "manager_exit_status": 0,
        "session_terminal_reason": "acknowledgements_complete",
        "terminal_monotonic_raw_ns": decision + 30,
        "successor_bundle_sha256": bundle_sha,
        "hard_deadline_exhausted": False, "fatal_reason": None,
    })
    _write_jsonl(raw / "activation-events.jsonl", [
        {"replica_id": replica, "epoch_number": 1, "epoch_digest": e1,
         "successor_bundle_sha256": bundle_sha, "monotonic_raw_ns": decision + replica + 1}
        for replica in range(31)
    ])
    _write_json(raw / "measurement-window.json", {
        "schema_version": 1, "start_monotonic_raw_ns": decision + 31,
        "end_monotonic_raw_ns": int(pins["hard_deadline_monotonic_raw_ns"]) - 1,
    })
    _write_jsonl(raw / "commit-events.jsonl", [
        {"block_height": 7, "block_hash": "e" * 64, "transaction_count": 2,
         "monotonic_raw_ns": decision + 40, "source_replica_id": 7, "authoritative": True},
    ])
    assignments = [
        {"replica_id": replica, "capacity_class": "slow" if replica < 6 else "fast",
         "cpu_quota_percent": 25 if replica < 6 else 100, "scope": f"scope-{replica}",
         "samples": [
             {"monotonic_raw_ns": decision + 1, "usage_usec": 1,
              "throttled_usec": 0},
             {"monotonic_raw_ns": int(pins["hard_deadline_monotonic_raw_ns"]) - 1,
              "usage_usec": 2, "throttled_usec": 1},
         ]}
        for replica in range(31)
    ]
    _write_json(raw / "quota-evidence.json", {
        "schema_version": 1, "quota_profile_sha256": pins["quota_profile_sha256"],
        "assignments": assignments,
    })
    _write_json(raw / "cleanup.json", {"schema_version": 1, "replica_ids": list(range(31)),
                                         "all_scopes_removed": True, "all_processes_stopped": True})
    return {"root": root, "authority_paths": paths, "pins": pins}


def _run(fixture: dict[str, object]) -> dict[str, object]:
    return subject.validate_operator_capacity_raw(**fixture)


def test_prospective_schema_remains_no_replay_no_claim_for_synthetic_arm(tmp_path: Path) -> None:
    result = _run(_raw_fixture(tmp_path))
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_VALID_NO_RAW_REPLAY_NO_CLAIM"
    assert result["claim_eligible"] is False
    assert result["figure_eligible"] is False
    assert result["campaign_eligible"] is False
    assert result["commit_count"] == 1
    assert "Native structured-event bytes" in str(result["claim_boundary"])


def test_missing_raw_source_fails_closed(tmp_path: Path) -> None:
    fixture = _raw_fixture(tmp_path)
    (fixture["root"] / "raw" / "cleanup.json").unlink()
    result = _run(fixture)
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_REJECTED_NO_RAW_REPLAY_NO_CLAIM"
    assert "cleanup" in str(result["detail"])


def test_duplicate_authoritative_commit_is_rejected(tmp_path: Path) -> None:
    fixture = _raw_fixture(tmp_path)
    commits = fixture["root"] / "raw" / "commit-events.jsonl"
    rows = [json.loads(line) for line in commits.read_text().splitlines()]
    rows.append(dict(rows[0]))
    _write_jsonl(commits, rows)
    result = _run(fixture)
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_REJECTED_NO_RAW_REPLAY_NO_CLAIM"
    assert "duplicate" in str(result["detail"])


def test_treatment_slow_root_is_rejected(tmp_path: Path) -> None:
    fixture = _raw_fixture(tmp_path)
    roles = fixture["root"] / "raw" / "roles.json"
    document = json.loads(roles.read_text())
    document["epoch1"][0]["member_order"] = list(range(31))
    _write_json(roles, document)
    result = _run(fixture)
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_REJECTED_NO_RAW_REPLAY_NO_CLAIM"
    assert "slow replica" in str(result["detail"])


def test_sham_role_drift_is_rejected(tmp_path: Path) -> None:
    fixture = _raw_fixture(tmp_path, arm="exact_copy_sham")
    roles = fixture["root"] / "raw" / "roles.json"
    document = json.loads(roles.read_text())
    document["epoch1"][0]["member_order"] = list(range(1, 31)) + [0]
    _write_json(roles, document)
    result = _run(fixture)
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_REJECTED_NO_RAW_REPLAY_NO_CLAIM"
    assert "exact-copy" in str(result["detail"])


def test_incomplete_all_31_activation_is_rejected(tmp_path: Path) -> None:
    fixture = _raw_fixture(tmp_path)
    activation = fixture["root"] / "raw" / "activation-events.jsonl"
    rows = [json.loads(line) for line in activation.read_text().splitlines()][:-1]
    _write_jsonl(activation, rows)
    result = _run(fixture)
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_REJECTED_NO_RAW_REPLAY_NO_CLAIM"
    assert "all 31" in str(result["detail"])


def test_quota_profile_drift_is_rejected(tmp_path: Path) -> None:
    fixture = _raw_fixture(tmp_path)
    quotas = fixture["root"] / "raw" / "quota-evidence.json"
    document = json.loads(quotas.read_text())
    document["quota_profile_sha256"] = "0" * 64
    _write_json(quotas, document)
    result = _run(fixture)
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_REJECTED_NO_RAW_REPLAY_NO_CLAIM"
    assert "quota evidence" in str(result["detail"])


def test_wrong_frozen_cpu_assignments_are_rejected(tmp_path: Path) -> None:
    fixture = _raw_fixture(tmp_path)
    quotas = fixture["root"] / "raw" / "quota-evidence.json"
    document = json.loads(quotas.read_text())
    document["assignments"][0]["capacity_class"] = "fast"
    document["assignments"][0]["cpu_quota_percent"] = 100
    _write_json(quotas, document)
    result = _run(fixture)
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_REJECTED_NO_RAW_REPLAY_NO_CLAIM"
    assert "frozen N31 CPU contract" in str(result["detail"])


def test_regressing_or_noncovering_quota_samples_are_rejected(tmp_path: Path) -> None:
    fixture = _raw_fixture(tmp_path)
    quotas = fixture["root"] / "raw" / "quota-evidence.json"
    document = json.loads(quotas.read_text())
    document["assignments"][0]["samples"][1]["usage_usec"] = 0
    _write_json(quotas, document)
    result = _run(fixture)
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_REJECTED_NO_RAW_REPLAY_NO_CLAIM"
    assert "nonregressing counters" in str(result["detail"])


def test_conflicting_commit_at_one_height_is_rejected(tmp_path: Path) -> None:
    fixture = _raw_fixture(tmp_path)
    commits = fixture["root"] / "raw" / "commit-events.jsonl"
    rows = [json.loads(line) for line in commits.read_text().splitlines()]
    conflicting = dict(rows[0])
    conflicting["block_hash"] = "1" * 64
    conflicting["monotonic_raw_ns"] += 1
    rows.append(conflicting)
    _write_jsonl(commits, rows)
    result = _run(fixture)
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_REJECTED_NO_RAW_REPLAY_NO_CLAIM"
    assert "conflicting hash" in str(result["detail"])


def test_unpinned_authoritative_commit_source_is_rejected(tmp_path: Path) -> None:
    fixture = _raw_fixture(tmp_path)
    commits = fixture["root"] / "raw" / "commit-events.jsonl"
    rows = [json.loads(line) for line in commits.read_text().splitlines()]
    rows[0]["source_replica_id"] = 8
    _write_jsonl(commits, rows)
    result = _run(fixture)
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_REJECTED_NO_RAW_REPLAY_NO_CLAIM"
    assert "pinned authoritative source" in str(result["detail"])


def test_failed_or_deadline_exhausted_manager_terminal_is_rejected(tmp_path: Path) -> None:
    fixture = _raw_fixture(tmp_path)
    terminal = fixture["root"] / "raw" / "manager-terminal.json"
    document = json.loads(terminal.read_text())
    document["manager_exit_status"] = 1
    document["session_terminal_reason"] = "hard_deadline_exhausted"
    document["hard_deadline_exhausted"] = True
    document["fatal_reason"] = "successor_publication_failed"
    _write_json(terminal, document)
    result = _run(fixture)
    assert result["verdict"] == "PROSPECTIVE_SCHEMA_REJECTED_NO_RAW_REPLAY_NO_CLAIM"
    assert "successful pre-deadline exit" in str(result["detail"])
