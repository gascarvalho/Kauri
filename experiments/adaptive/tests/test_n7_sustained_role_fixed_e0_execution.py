from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "sustained_role_fixed_e0_execution.py"
spec = importlib.util.spec_from_file_location("n7_sustained_role_fixed_e0_execution", PATH)
assert spec and spec.loader
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def _digest(value: dict[str, object], field: str) -> str:
    return hashlib.sha256(_canonical({key: item for key, item in value.items() if key != field})[:-1]).hexdigest()


def _fixture(root: Path) -> dict[str, object]:
    (root / "runtime").mkdir(parents=True)
    manager = ["/test/adaptation-manager", "--fault-window-arm-control-only"]
    replicas = [
        {"replica_id": replica, "argv": ["/test/hotstuff-app", str(replica)],
         "sha256": f"{replica:x}" * 64, "executable_sha256": "a" * 64}
        for replica in range(7)
    ]
    plan: dict[str, object] = {
        "schema_version": 1,
        "kind": "kauri-n7-sustained-role-execution-plan-v1",
        "state": "PREPARED_DRY_RUN_EXTERNAL_APPROVAL_REQUIRED",
        "repository_revision": "d" * 40, "no_retry": True,
        "hard_timeout_seconds": 180,
        "comparison": {"arm": "fixed_e0"},
        "commands": {"manager": {"argv": manager, "sha256": "b" * 64,
                                  "executable_sha256": "c" * 64},
                     "replicas": replicas},
        "evidence_contract": {
            "prearm_all_seven_e0_common_commit": "strictly_before_scheduled_window_start",
            "common_horizon_ns": 60_000_000_000,
            "raw_stream_coverage": "all_seven_replica_streams_through_anchor_plus_common_horizon",
            "anchor": "first_bijected_e0_internal_aggregate_omission",
        },
        "scheduled_window": {"start_monotonic_ns": 100, "end_monotonic_ns": 70_000_000_100},
    }
    plan["plan_sha256"] = _digest(plan, "plan_sha256")
    request = {
        "schema_version": 1,
        "kind": "kauri-n7-sustained-role-execution-authorization-request-v1",
        "execution_plan_sha256": plan["plan_sha256"],
        "repository_revision": plan["repository_revision"], "arm": "fixed_e0",
        "scheduled_window": plan["scheduled_window"],
        "hard_timeout_seconds": plan["hard_timeout_seconds"], "no_retry": True,
        "claim_eligible": False, "figure_eligible": False,
    }
    (root / subject.PLAN).write_bytes(_canonical(plan))
    (root / subject.REQUEST).write_bytes(_canonical(request))
    return plan


def _external_receipts(root: Path) -> tuple[Path, Path]:
    execution = json.loads((root / subject.EXECUTION_PLAN).read_bytes())
    request = (root / subject.EXECUTION_REQUEST).read_bytes()
    approval = {
        "schema_version": 1, "kind": subject.APPROVAL_KIND,
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "execution_plan_sha256": execution["execution_plan_sha256"],
        "approval_reference": "test authorization", "approved_utc": "2026-09-30T10:00:00Z",
        "no_retry": True,
    }
    native = {
        "schema_version": 1, "kind": subject.NATIVE_KIND,
        "execution_plan_sha256": execution["execution_plan_sha256"],
        "verified_utc": "2026-09-30T10:00:01Z",
        "manager_executable_sha256": execution["manager_executable_sha256"],
        "replica_executable_sha256": execution["replica_executable_sha256"],
        "outcome": "INDEPENDENT_NATIVE_EXECUTION_READY",
    }
    approval_path, native_path = root.parent / "approval.json", root.parent / "native.json"
    approval_path.write_bytes(_canonical(approval))
    native_path.write_bytes(_canonical(native))
    return approval_path, native_path


def test_prepare_seals_fixed_e0_no_launch_contract(tmp_path: Path) -> None:
    _fixture(tmp_path)
    result = subject.prepare_execution(tmp_path)
    assert result["state"] == "PREPARE_ONLY_NATIVE_VERIFICATION_REQUIRED"
    plan = json.loads((tmp_path / subject.EXECUTION_PLAN).read_bytes())
    assert plan["raw_bundle_contract"]["sources"] == ["adaptive-manager", *[f"replica-{i}" for i in range(7)]]
    assert len(plan["raw_bundle_contract"]["required_logs"]) == 8
    assert len(plan["raw_bundle_contract"]["required_jsonl"]) == 8


def test_prepare_rejects_missing_native_control_only_mode(tmp_path: Path) -> None:
    plan = _fixture(tmp_path)
    plan["commands"]["manager"]["argv"].remove("--fault-window-arm-control-only")
    plan["plan_sha256"] = _digest(plan, "plan_sha256")
    (tmp_path / subject.PLAN).write_bytes(_canonical(plan))
    request = json.loads((tmp_path / subject.REQUEST).read_bytes())
    request["execution_plan_sha256"] = plan["plan_sha256"]
    (tmp_path / subject.REQUEST).write_bytes(_canonical(request))
    with pytest.raises(subject.FixedE0ExecutionError, match="no-successor"):
        subject.prepare_execution(tmp_path)


def test_finalization_is_still_prepare_only_then_seals_complete_raw_layout(tmp_path: Path) -> None:
    _fixture(tmp_path)
    subject.prepare_execution(tmp_path)
    approval, native = _external_receipts(tmp_path)
    result = subject.finalize_prepare_only(tmp_path, approval, native)
    assert result["state"] == "PREPARE_ONLY_AUDITED_LAUNCHER_MISSING"
    execution = json.loads((tmp_path / subject.EXECUTION_PLAN).read_bytes())
    for relative in (*execution["raw_bundle_contract"]["required_logs"],
                     *execution["raw_bundle_contract"]["required_jsonl"],
                     *execution["raw_bundle_contract"]["required_runtime"]):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(relative, encoding="utf-8")
    sealed = subject.seal_raw_descriptor(tmp_path)
    assert sealed["state"] == "SEALED_RAW_BUNDLE_NO_CLAIM"
    receipt = json.loads((tmp_path / subject.RAW_RECEIPT).read_bytes())
    assert len(receipt["artifacts"]) == 18


def test_raw_descriptor_rejects_missing_one_of_eight_logs(tmp_path: Path) -> None:
    _fixture(tmp_path)
    subject.prepare_execution(tmp_path)
    approval, native = _external_receipts(tmp_path)
    subject.finalize_prepare_only(tmp_path, approval, native)
    execution = json.loads((tmp_path / subject.EXECUTION_PLAN).read_bytes())
    paths = (*execution["raw_bundle_contract"]["required_logs"],
             *execution["raw_bundle_contract"]["required_jsonl"],
             *execution["raw_bundle_contract"]["required_runtime"])
    for relative in paths[1:]:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(relative, encoding="utf-8")
    with pytest.raises(subject.FixedE0ExecutionError, match="regular file"):
        subject.seal_raw_descriptor(tmp_path)
