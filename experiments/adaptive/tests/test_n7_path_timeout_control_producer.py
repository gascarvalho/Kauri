from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "comparison" / "control_producer.py"
spec = importlib.util.spec_from_file_location("n7_control_producer_test", PATH)
assert spec and spec.loader
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


VALIDATOR_PATH = ROOT / "n7-path-timeout-quorum" / "validator.py"
validator_spec = importlib.util.spec_from_file_location("n7_path_timeout_validator_metric_test", VALIDATOR_PATH)
assert validator_spec and validator_spec.loader
validator = importlib.util.module_from_spec(validator_spec)
validator_spec.loader.exec_module(validator)


def _horizon_streams(*, duplicate_witness: bool = False, conflicting_height: bool = False):
    """Raw structured streams, not a caller-authored metric summary."""
    digest = "e" * 64
    streams = {f"replica-{replica}": [] for replica in range(7)}
    for height, timestamp, block_hash in ((11, 101, "a" * 64), (12, 201, "b" * 64)):
        streams["replica-0"].append({
            "event_type": "block.committed", "source_monotonic_ns": timestamp,
            "payload": {
                "block_height": height, "block_hash": block_hash,
                "parent_hash": None, "transaction_count": 1,
                "designated_observer": True,
                "decision_proof": {"epoch_number": 1, "tree_id": 0,
                                   "epoch_digest": digest, "block_hash": block_hash},
                "view_generation": height, "commit_batch_index": height,
            },
        })
        for replica in range(7):
            streams[f"replica-{replica}"].append({
                "event_type": "block.commit_observed", "source_monotonic_ns": timestamp + 10 + replica,
                "payload": {"block_height": height, "block_hash": block_hash,
                            "parent_hash": None, "transaction_count": 1,
                            "commit_batch_index": height},
            })
    if duplicate_witness:
        streams["replica-1"].append(dict(streams["replica-1"][0]))
    if conflicting_height:
        streams["replica-2"].append({
            "event_type": "block.commit_observed", "source_monotonic_ns": 150,
            "payload": {"block_height": 11, "block_hash": "c" * 64,
                        "parent_hash": None, "transaction_count": 1,
                        "commit_batch_index": 11},
        })
    return streams, digest


def test_prospective_metric_derives_count_and_gap_from_raw_all_peer_evidence():
    streams, digest = _horizon_streams()
    assert validator._prospective_fixed_horizon_commits(
        streams, anchor_ns=100, end_ns=300, predecessor_digest="p" * 64,
        successor_digest=digest,
    ) == (2, 100)


@pytest.mark.parametrize("mutation", ["duplicate_witness", "conflicting_height"])
def test_prospective_metric_rejects_duplicate_or_conflicting_raw_commit_evidence(mutation: str):
    streams, digest = _horizon_streams(**{mutation: True})
    with pytest.raises(validator.ValidationError):
        validator._prospective_fixed_horizon_commits(
            streams, anchor_ns=100, end_ns=300, predecessor_digest="p" * 64,
            successor_digest=digest,
        )


def test_prospective_metric_skips_a_valid_inflight_e0_commit_but_rejects_peer_metadata_drift():
    streams, digest = _horizon_streams()
    predecessor = "p" * 64
    streams["replica-0"].insert(0, {
        "event_type": "block.committed", "source_monotonic_ns": 100,
        "payload": {
            "block_height": 10, "block_hash": "d" * 64, "parent_hash": None,
            "transaction_count": 1, "designated_observer": True,
            "decision_proof": {"epoch_number": 0, "tree_id": 0,
                               "epoch_digest": predecessor, "block_hash": "d" * 64},
            "view_generation": 10, "commit_batch_index": 10,
        },
    })
    assert validator._prospective_fixed_horizon_commits(
        streams, anchor_ns=100, end_ns=300, predecessor_digest=predecessor,
        successor_digest=digest,
    ) == (2, 100)
    streams["replica-2"][0]["payload"]["transaction_count"] = 2
    with pytest.raises(validator.ValidationError, match="metadata"):
        validator._prospective_fixed_horizon_commits(
            streams, anchor_ns=100, end_ns=300, predecessor_digest=predecessor,
            successor_digest=digest,
        )


def test_control_contract_strips_the_only_successor_authorizing_pairs():
    command = (
        "adaptation-manager", "--transition-request", "request-json",
        "--bundle-output", "/tmp/successor.bundle", "--listen", "127.0.0.1:1",
    )
    result = producer._strip_exact_option_pairs(command)
    assert "--transition-request" not in result
    assert "--bundle-output" not in result
    assert result[-1] == "--fault-window-arm-control-only"


def test_control_reader_accepts_native_prepared_base_plan_format(tmp_path: Path):
    base = {"schema_version": 1, "state": "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED"}
    path = tmp_path / "local-launch-plan.json"
    path.write_text(json.dumps(base, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert producer._read_prepared_base_plan(path) == base


def test_control_reader_rejects_non_object_base_plan(tmp_path: Path):
    path = tmp_path / "local-launch-plan.json"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(producer.ProducerError, match="prepared base plan"):
        producer._read_prepared_base_plan(path)


@pytest.mark.parametrize("command", [
    ("manager", "--transition-request", "only-request"),
    ("manager", "--bundle-output", "only-bundle"),
    ("manager", "--transition-request", "one", "--transition-request", "two", "--bundle-output", "out"),
])
def test_control_contract_rejects_missing_or_repeated_successor_pairs(command):
    with pytest.raises(producer.ProducerError):
        producer._strip_exact_option_pairs(command)


def test_finalization_rejects_a_self_authored_plan_even_with_a_matching_approval(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    plan = {
        "schema_version": 2, "kind": producer.PLAN_KIND,
        "state": "PREPARED_EXTERNAL_APPROVAL_REQUIRED",
        "claim_boundary": "No process launched. The native control-only manager mode is required before execution or a result.",
        "base_plan": {"path": "local-launch-plan.json", "sha256": "0" * 64},
        "base_plan_sha256": "a" * 64, "run_id": "control-1",
        "epoch0": {"epoch_number": 0, "epoch_digest": "b" * 64, "tree_file_sha256": "c" * 64},
        "hard_timeout_seconds": 600,
        "declared_ports": list(range(20000, 20015)),
        "bindings": {"replica_1_launch_argv_sha256": "d" * 64},
        "fault_window_arm": "runtime/fault-window-arm.json", "omission_gate": "runtime/static-omission-gate.json",
        "manager_events": "raw/adaptive-manager.jsonl", "manager_command": ["manager", producer.CONTROL_MODE_OPTION],
        "replica_commands": [["replica"] for _ in range(7)], "required_native_mode": producer.CONTROL_MODE_OPTION,
        "executables": {"hotstuff_app": {"path": "/missing-app", "sha256": "e" * 64}, "adaptation_manager": {"path": "/missing-manager", "sha256": "f" * 64}},
        "no_successor_guards": {"transition_request_absent": True, "bundle_output_absent": True, "control_only_mode_required": True},
    }
    plan["launch_contract_sha256"] = producer._sha256(producer._canonical(producer._launch_contract(plan)))
    plan["prepared_plan_sha256"] = producer._semantic_digest(plan)
    request = {"schema_version": 2, "kind": producer.REQUEST_KIND,
               "prepared_plan_sha256": plan["prepared_plan_sha256"], "base_plan_sha256": plan["base_plan_sha256"],
               "run_id": plan["run_id"], "epoch0": plan["epoch0"],
               "launch_contract_sha256": plan["launch_contract_sha256"],
               "manager_command_sha256": producer._sha256(producer._canonical(plan["manager_command"])),
               "replica_1_launch_argv_sha256": "d" * 64, "hard_timeout_seconds": 600,
               "declared_ports": list(range(20000, 20015)),
               "no_retry": True, "claim_boundary": "Approval inputs only; no process launched."}
    (runtime / "fixed-e0-control-plan.json").write_bytes(producer._canonical(plan))
    request_bytes = producer._canonical(request)
    (runtime / "fixed-e0-control-authorization-request.json").write_bytes(request_bytes)
    authorization = {"schema_version": 2, "kind": producer.AUTHORIZATION_KIND,
                     "request_sha256": hashlib.sha256(request_bytes).hexdigest(), "prepared_plan_sha256": plan["prepared_plan_sha256"],
                     "approval_reference": "test", "approved_utc": "2026-09-29T12:00:00Z", "no_retry": True}
    external = tmp_path / "authorization.json"
    external.write_bytes(producer._canonical(authorization))
    with pytest.raises(producer.ProducerError, match="base plan"):
        producer.finalize_no_successor_control(tmp_path, external)
    assert not (runtime / "fixed-e0-control-finalization-receipt.json").exists()


def test_finalization_rejects_a_mutated_manager_argv_before_approval_can_be_consumed(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    plan = {
        "schema_version": 2, "kind": producer.PLAN_KIND,
        "state": "PREPARED_EXTERNAL_APPROVAL_REQUIRED", "claim_boundary": "x",
        "base_plan": {"path": "local-launch-plan.json", "sha256": "0" * 64}, "base_plan_sha256": "a" * 64,
        "run_id": "control-1", "epoch0": {"epoch_number": 0, "epoch_digest": "b" * 64, "tree_file_sha256": "c" * 64},
        "hard_timeout_seconds": 600, "declared_ports": list(range(20000, 20015)), "bindings": {"replica_1_launch_argv_sha256": "d" * 64},
        "fault_window_arm": "runtime/fault-window-arm.json", "omission_gate": "runtime/static-omission-gate.json", "manager_events": "raw/adaptive-manager.jsonl",
        "manager_command": ["manager", producer.CONTROL_MODE_OPTION], "replica_commands": [["replica"] for _ in range(7)],
        "executables": {"hotstuff_app": {"path": "/missing-app", "sha256": "e" * 64}, "adaptation_manager": {"path": "/missing-manager", "sha256": "f" * 64}},
        "required_native_mode": producer.CONTROL_MODE_OPTION, "no_successor_guards": {"transition_request_absent": True, "bundle_output_absent": True, "control_only_mode_required": True},
    }
    plan["launch_contract_sha256"] = producer._sha256(producer._canonical(producer._launch_contract(plan)))
    plan["prepared_plan_sha256"] = producer._semantic_digest(plan)
    approved_plan_sha = plan["prepared_plan_sha256"]
    plan["manager_command"].insert(1, "--unapproved-option")
    (runtime / "fixed-e0-control-plan.json").write_bytes(producer._canonical(plan))
    (runtime / "fixed-e0-control-authorization-request.json").write_bytes(producer._canonical({}))
    external = tmp_path / "authorization.json"
    external.write_bytes(producer._canonical({"schema_version": 2, "kind": producer.AUTHORIZATION_KIND, "request_sha256": "0" * 64, "prepared_plan_sha256": approved_plan_sha, "approval_reference": "test", "approved_utc": "2026-09-29T12:00:00Z", "no_retry": True}))
    with pytest.raises(producer.ProducerError, match="digest drifted"):
        producer.finalize_no_successor_control(tmp_path, external)


def test_executor_is_disabled_before_it_reads_or_spawns_any_process(tmp_path: Path):
    called = False
    def spawn(*_args, **_kwargs):
        nonlocal called
        called = True
    with pytest.raises(producer.ProducerError, match="disabled"):
        producer.execute_no_successor_control(tmp_path, tmp_path / "missing.json", spawn=spawn)
    assert called is False


def test_cli_without_explicit_enablement_cannot_launch(tmp_path: Path):
    assert producer.main(["--run-root", str(tmp_path), "--authorization", str(tmp_path / "missing.json")]) == 2


def test_enabled_executor_without_finalized_plan_or_approval_never_spawns(tmp_path: Path):
    called = False
    def spawn(*_args, **_kwargs):
        nonlocal called
        called = True
    with pytest.raises(producer.ProducerError):
        producer.execute_no_successor_control(tmp_path, tmp_path / "missing.json", spawn=spawn, execution_enabled=True)
    assert called is False


def test_fixed_horizon_requires_a_gate_bound_physical_omission():
    streams = {"replica-1": [{"event_type": "fault.aggregate_omitted", "source_monotonic_ns": 10,
                               "payload": {"gate_sha256": "wrong"}}]}
    assert producer.first_source_bound_physical_omission(streams, "right") is None


@pytest.mark.parametrize("tree_id", [4, 5, 6])
def test_fixed_horizon_accepts_only_the_admitted_t4_t5_t6_contexts(tree_id: int):
    event = {"event_type": "fault.aggregate_omitted", "source_monotonic_ns": 10,
             "source_sequence": 1,
             "payload": {"gate_sha256": "gate", "tree_id": tree_id,
                         "parent_replica": tree_id, "first_for_context": True}}
    assert producer.first_source_bound_physical_omission({"replica-1": [event]}, "gate") == event


@pytest.mark.parametrize("tree_id,parent", [(3, 3), (7, 7), (4, 5)])
def test_fixed_horizon_rejects_non_admitted_or_mismatched_omission_context(tree_id: int, parent: int):
    event = {"event_type": "fault.aggregate_omitted", "source_monotonic_ns": 10,
             "payload": {"gate_sha256": "gate", "tree_id": tree_id, "parent_replica": parent}}
    assert producer.first_source_bound_physical_omission({"replica-1": [event]}, "gate") is None


def test_fixed_horizon_rejects_censored_manager_or_replica_streams():
    streams = {"adaptive-manager": [{"source_monotonic_ns": 100}]}
    streams.update({f"replica-{item}": [{"source_monotonic_ns": 100}] for item in range(7)})
    assert producer.streams_cover_horizon(streams, 101) is False
    streams["replica-6"] = []
    assert producer.streams_cover_horizon(streams, 100) is False


@pytest.mark.parametrize("returncode,termination", [(1, "terminated"), (0, "terminated")])
def test_control_producer_never_accepts_a_failed_or_ambiguous_manager_cleanup(returncode: int, termination: str):
    receipt = {
        "complete": True,
        "processes": [
            {"source_id": source, "returncode": 0, "termination": "clean-exit"}
            for source in ("adaptive-manager", *(f"replica-{item}" for item in range(7)))
        ],
    }
    receipt["processes"][0].update({"returncode": returncode, "termination": termination})
    with pytest.raises(producer.ProducerError, match="manager did not exit cleanly"):
        producer._require_clean_control_cleanup(receipt)


def test_native_fixed_e0_control_returns_before_post_arm_controller_evaluation():
    manager = (ROOT.parents[1] / "examples" / "adaptation_manager.cpp").read_text(encoding="utf-8")
    branch = manager.index("if (options_.fault_window_arm_control_only)")
    armed_return = manager.index("if (fault_window_armed_)", branch)
    evaluation = manager.index("const auto status = session_.evaluate();", branch)
    assert armed_return < evaluation
