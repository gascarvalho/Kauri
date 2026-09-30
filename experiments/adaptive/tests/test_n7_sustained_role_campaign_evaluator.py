from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "n7-path-timeout-quorum" / "sustained_role_campaign_evaluator.py"
SPEC = importlib.util.spec_from_file_location("n7_sustained_role_campaign_evaluator", MODULE)
assert SPEC is not None and SPEC.loader is not None
subject = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = subject
SPEC.loader.exec_module(subject)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"


def _write(path: Path, value: object) -> tuple[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _canonical(value)
    path.write_bytes(raw)
    return str(path), hashlib.sha256(raw).hexdigest()


def _descriptor(root: Path, relative: str, value: object) -> dict[str, str]:
    raw = _canonical(value)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"path": relative, "sha256": hashlib.sha256(raw).hexdigest()}


def _raw_descriptor(root: Path, relative: str, raw: bytes) -> dict[str, str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"path": relative, "sha256": hashlib.sha256(raw).hexdigest()}


def _main_config(root: Path, *, ordinal: int,
                 override: tuple[str, str] | None = None) -> bytes:
    peer_base = 20_000 + ordinal * 20
    client_base = peer_base + 10
    manager_port = peer_base + 19
    options = {
        **subject._MAIN_CONFIG_FIXED,
        "tree-generation-fpath": str(root / "config/epoch0.tree"),
        "epoch-change-issuer-public-key": f"fresh-issuer-key-{ordinal}",
        "epoch-manager-address": f"127.0.0.1:{manager_port}",
        "epoch-manager-tls-cert": f"fresh-manager-certificate-{ordinal}",
    }
    if override is not None:
        options[override[0]] = override[1]
    ordered = [
        "block-size", "nworker", "repnworker", "pace-maker", "proposer",
        "fan-out", "piped_latency", "async_blocks", "base-timeout", "prop-delay",
        "aggregation-timeout", "leader-progress-timeout", "leader-activation-grace",
        "client-ip", "tree-generation", "tree-generation-fpath", "tree-switch-period",
        "epoch-protocol-mode", "epoch-change-issuer-id", "epoch-change-issuer-public-key",
        "epoch-change-minimum-activation-delay", "epoch-change-maximum-activation-delay",
        "epoch-change-maximum-block-extra-bytes", "epoch-change-maximum-ancestry-blocks",
        "epoch-manager-address", "epoch-manager-tls-cert", "max-rep-msg",
    ]
    lines = [f"{key} = {options[key]}" for key in ordered]
    lines.extend(
        f"replica = 127.0.0.1:{peer_base + replica};{client_base + replica}, "
        f"fresh-bls-{ordinal}-{replica}, fresh-tls-{ordinal}-{replica}"
        for replica in range(7)
    )
    return ("\n".join(lines) + "\n").encode("ascii")


def _native_fault_schedule(start: int, *, first_omission_tree: int | None = None) -> dict[str, Any]:
    end = start + 70_000_000_000
    profile_id = "n7-role-scoped-persistent-selected-omission-v1"
    fault = {
        "native_mode": "role_scoped_persistent_selected_omission_v1",
        "actor_id": 1,
        "hard_actor_count": 1,
        "responsive_degraded_actor_count": 0,
        "responsive_omission_period": 0,
        "max_omissions_per_proposal": 1,
        "context_limit": 100_000,
        "window_start_monotonic_ns": start,
        "window_end_monotonic_ns": end,
        "common_horizon_ns": 60_000_000_000,
        "minimum_post_start_anchor_slack_ns": 10_000_000_000,
        "argv_overlay": [
            "--experiment-byzantine-mode", "role_scoped_persistent_selected_omission_v1",
            "--experiment-byzantine-window", profile_id,
            "--experiment-rotating-omission-actors", "1",
            "--experiment-byzantine-window-start-monotonic-ns", str(start),
            "--experiment-byzantine-window-end-monotonic-ns", str(end),
            "--experiment-byzantine-max-omissions-per-proposal", "1",
            "--experiment-rotating-omission-context-limit", "100000",
        ],
    }
    if first_omission_tree is not None:
        fault["first_omission_tree"] = first_omission_tree
        fault["argv_overlay"].extend(
            ["--experiment-byzantine-first-omission-tree", str(first_omission_tree)])
    return {
        "descriptor": {"path": "sustained_role_profile.py", "sha256": "unused-in-test"},
        "values": {
            "schema_version": 1,
            "profile_id": profile_id,
            "status": "PREFLIGHT_ONLY_NO_EXECUTION",
            "claim_boundary": "synthetic no-launch fixture",
            "protocol": {"replica_ids": list(range(7)), "fault_threshold": 2, "quorum": 5},
            "fault": fault,
        },
    }


def _event(run_id: str, replica: int, sequence: int, timestamp: int,
           event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_schema_version": 1,
        "run_id": run_id,
        "source_kind": "replica",
        "source_id": f"replica-{replica}",
        "source_instance": f"instance-{run_id}-{replica}",
        "source_sequence": sequence,
        "source_monotonic_ns": timestamp,
        "event_type": event_type,
        "payload": payload,
    }


def _observed(height: int, block_hash: str) -> dict[str, Any]:
    return {"block_height": height, "block_hash": block_hash, "parent_hash": "a" * 64,
            "transaction_count": 0, "commit_batch_index": height}


def _committed(height: int, block_hash: str, arm: str) -> dict[str, Any]:
    return {**_observed(height, block_hash), "designated_observer": True,
            "decision_proof": {"epoch_number": 0 if arm == subject.FIXED_ARM else 1,
                               "tree_id": 0, "epoch_digest": ("e" if arm == subject.FIXED_ARM else "f") * 64,
                               "block_hash": block_hash}, "view_generation": height}


def _cell(tmp_path: Path, *, ordinal: int, pair_index: int, arm: str, count: int,
          start: int, revision: str = "1" * 40,
          config_override: tuple[str, str] | None = None) -> dict[str, Any]:
    root = tmp_path / f"cell-{ordinal:02d}-{arm}"
    run_id = f"campaign-run-{ordinal:02d}"
    anchor = start + 1_000_000_000
    streams: list[dict[str, str]] = []
    for replica in range(7):
        events: list[dict[str, Any]] = []
        sequence = 1
        if arm == subject.ADAPTIVE_ARM:
            events.append(_event(run_id, replica, sequence, anchor + 10_000_000_000,
                                 "epoch.activated", {"epoch_number": 1, "tree_id": replica,
                                                     "epoch_digest": "f" * 64, "activation_height": 20}))
            sequence += 1
        fault_payload = {"actor": 1, "fault_mode": "role_scoped_persistent_selected_omission_v1",
                         "physical_role": "leaf" if arm == subject.ADAPTIVE_ARM else "internal",
                         "expected_message_type": "direct_vote" if arm == subject.ADAPTIVE_ARM else "aggregate_relay",
                         "scheduled_action": "omit_direct_vote" if arm == subject.ADAPTIVE_ARM else "omit_aggregate",
                         "proposal": {"epoch_number": 1 if arm == subject.ADAPTIVE_ARM else 0,
                                      "tree_id": 0,
                                      "epoch_digest": ("f" if arm == subject.ADAPTIVE_ARM else "e") * 64,
                                      "block_hash": "b" * 64}}
        if replica == 1:
            events.append(_event(run_id, replica, sequence, anchor + 21_000_000_000,
                                 "fault.contribution_opportunity", fault_payload))
            sequence += 1
        for item in range(count):
            height = ordinal * 100 + item + 1
            block_hash = f"{height:064x}"
            timestamp = anchor + 22_000_000_000 + item * 1_000
            if replica == subject.DESIGNATED_OBSERVER:
                events.append(_event(run_id, replica, sequence, timestamp,
                                     "block.committed", _committed(height, block_hash, arm)))
                sequence += 1
            events.append(_event(run_id, replica, sequence, timestamp + 1,
                                 "block.commit_observed", _observed(height, block_hash)))
            sequence += 1
        if not events:
            events.append(_event(run_id, replica, 1, anchor + 5_000_000_000,
                                 "process.lifecycle", {"exit_status": None}))
        raw = b"".join(_canonical(event) for event in events)
        relative = f"raw/replica-{replica}.jsonl"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        streams.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest()})

    profile = _descriptor(root, "inputs/profile.py", {"profile": "same"})
    tree = _raw_descriptor(
        root, "config/epoch0.tree",
        b"".join(
            f"fan:2 pipe:2 {' '.join(str((tree_id + item) % 7) for item in range(7))}\n".encode("ascii")
            for tree_id in range(7)
        ),
    )
    main = _raw_descriptor(
        root, "config/main.conf",
        _main_config(root, ordinal=ordinal, override=config_override),
    )
    app = _descriptor(root, "inputs/hotstuff-app", {"binary": "app"})
    manager = _descriptor(root, "inputs/adaptation-manager", {"binary": "manager"})
    scheduled_window = {"start_monotonic_ns": start,
                        "end_monotonic_ns": start + 70_000_000_000}
    plan = {"repository_revision": revision, "comparison": {"arm": arm}, "no_retry": True,
            "hard_timeout_seconds": 180, "scheduled_window": scheduled_window,
            "native_fault_schedule": _native_fault_schedule(start)}
    plan_descriptor = _descriptor(root, "inputs/execution-plan.json", plan)
    request = {"arm": arm, "hard_timeout_seconds": 180, "no_retry": True,
               "claim_eligible": False, "figure_eligible": False}
    request_descriptor = _descriptor(root, "inputs/authorization-request.json", request)
    approval = {"approval_reference": "W19 campaign approval", "approved_utc": "2026-09-30T01:00:00Z"}
    approval_descriptor = _descriptor(root, "inputs/approved-authorization.json", approval)
    artifacts = {"replica_events": streams, "execution_plan": plan_descriptor,
                 "authorization_request": request_descriptor, "approved_authorization": approval_descriptor,
                 "profile": profile, "epoch0_tree": tree, "main_config": main,
                 "executables": {"hotstuff_app": app, "adaptation_manager": manager}}
    if arm == subject.ADAPTIVE_ARM:
        # The campaign evaluator does not inspect these; the independent arm
        # validator owns their semantic verification.
        artifacts["e1_bundle"] = _descriptor(root, "inputs/e1.bundle", {"bundle": ordinal})
    receipt = {"schema_version": 1, "kind": subject.RECEIPT_KIND,
               "state": "SEALED_RAW_BUNDLE_NO_CLAIM", "arm": arm, "run_id": run_id,
               "plan_sha256": plan_descriptor["sha256"],
               "anchor": {"source_id": "replica-1", "source_sequence": 1,
                          "line_sha256": "a" * 64, "monotonic_ns": anchor},
               "horizon": {"clock": "CLOCK_MONOTONIC_RAW", "duration_ns": 60_000_000_000,
                           "late_offset_ns": 20_000_000_000},
               "artifacts": artifacts,
               "launch_binding": {"e0_digest": "e" * 64,
                                  "native_profile_sha256": profile["sha256"],
                                  "selection_profile_sha256": "b" * 64,
                                  "scheduled_window": scheduled_window}}
    semantic = deepcopy(receipt)
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(semantic)).hexdigest()
    raw = _canonical(receipt)
    receipt_path = root / "sustained-role-raw-bundle-receipt.json"
    receipt_path.write_bytes(raw)
    return {"pair_index": pair_index, "ordinal": ordinal, "arm": arm,
            "root": str(root), "receipt_path": receipt_path.name,
            "receipt_sha256": hashlib.sha256(raw).hexdigest()}


def _freeze() -> dict[str, Any]:
    return subject.build_campaign_freeze(
        campaign_id="w19-six-pair-v1",
        frozen_utc="2026-09-30T00:00:00Z",
        campaign_approval_reference="W19 campaign approval",
        repository_revision="1" * 40,
    )


def _campaign(tmp_path: Path, *, fixed_counts: list[int] | None = None,
              adaptive_counts: list[int] | None = None) -> list[dict[str, Any]]:
    fixed_counts = fixed_counts or [10] * 6
    adaptive_counts = adaptive_counts or [12] * 6
    cells: list[dict[str, Any]] = []
    ordinal = 0
    for pair_index, order in enumerate(subject.FROZEN_PAIR_SCHEDULE, 1):
        for arm in order:
            ordinal += 1
            cells.append(_cell(tmp_path, ordinal=ordinal, pair_index=pair_index, arm=arm,
                               count=(fixed_counts if arm == subject.FIXED_ARM else adaptive_counts)[pair_index - 1],
                               start=ordinal * 100_000_000_000))
    return cells


def test_fault_schedule_accepts_legacy_absence_and_binds_phase_gate_tree_four() -> None:
    legacy = subject._fault_schedule_invariants(
        {"native_fault_schedule": _native_fault_schedule(10)}, label="legacy")
    gated = subject._fault_schedule_invariants(
        {"native_fault_schedule": _native_fault_schedule(10, first_omission_tree=4)}, label="gated")
    assert legacy["first_omission_tree"] is None
    assert gated["first_omission_tree"] == 4


def test_fault_schedule_rejects_foreign_phase_gate_or_argv_mismatch() -> None:
    foreign = _native_fault_schedule(10, first_omission_tree=3)
    with pytest.raises(subject.CampaignEvaluationError, match="frozen tree 4"):
        subject._fault_schedule_invariants({"native_fault_schedule": foreign}, label="foreign")
    mismatch = _native_fault_schedule(10, first_omission_tree=4)
    mismatch["values"]["fault"]["argv_overlay"][-1] = "3"
    with pytest.raises(subject.CampaignEvaluationError, match="native fault argv"):
        subject._fault_schedule_invariants({"native_fault_schedule": mismatch}, label="mismatch")


@pytest.fixture(autouse=True)
def accepted_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    def accept(root: Path, receipt_relative: Path) -> dict[str, Any]:
        receipt = json.loads((root / receipt_relative).read_text(encoding="ascii"))
        return {"verdict": subject.VALIDATOR_VERDICT, "arm": receipt["arm"],
                "run_id": receipt["run_id"], "plan_sha256": receipt["plan_sha256"],
                "anchor_monotonic_ns": receipt["anchor"]["monotonic_ns"]}
    monkeypatch.setattr(subject, "_validate_accepted_bundle", accept)


def test_six_pair_counterbalanced_campaign_passes_frozen_gate(tmp_path: Path) -> None:
    cells = _campaign(tmp_path)
    # Every cell has deliberately different key/certificate material and thus
    # different main.conf bytes.  Only normalized causal settings may match.
    main_hashes = {
        json.loads((Path(cell["root"]) / cell["receipt_path"]).read_text(encoding="ascii"))
        ["artifacts"]["main_config"]["sha256"]
        for cell in cells
    }
    assert len(main_hashes) == 12
    result = subject.evaluate_campaign(_freeze(), cells)
    assert [pair["order"] for pair in result["pairs"]] == [list(item) for item in subject.FROZEN_PAIR_SCHEDULE]
    assert result["aggregate"] == {"fixed_count": 60, "adaptive_count": 72,
                                    "adaptive_minus_fixed": 12,
                                    "adaptive_to_fixed_ratio": {"numerator": 6, "denominator": 5}}
    assert result["technical_improvement_gate_passed"] is True
    assert result["thesis_integration_candidate"] is True
    assert result["claim_eligible"] is False
    assert result["figure_eligible"] is False
    assert "one N=7" in result["claim_boundary"]


def test_zero_fixed_denominator_is_positive_raw_count_not_infinite_ratio(tmp_path: Path) -> None:
    result = subject.evaluate_campaign(
        _freeze(), _campaign(tmp_path, fixed_counts=[0] * 6, adaptive_counts=[1] * 6))
    assert result["pairs"][0]["adaptive_minus_fixed"] == 1
    assert result["pairs"][0]["adaptive_to_fixed_ratio"] is None
    assert result["pairs"][0]["zero_denominator_outcome"] == "adaptive-progress-fixed-zero"
    assert result["aggregate"]["adaptive_to_fixed_ratio"] is None
    assert result["technical_improvement_gate_passed"] is True


def test_both_zero_is_neutral_and_campaign_gate_fails(tmp_path: Path) -> None:
    result = subject.evaluate_campaign(
        _freeze(), _campaign(tmp_path, fixed_counts=[0] * 6, adaptive_counts=[0] * 6))
    assert result["pairs"][0]["classification"] == "neutral"
    assert result["pairs"][0]["zero_denominator_outcome"] == "both-zero"
    assert result["technical_improvement_gate_passed"] is False
    assert result["thesis_integration_candidate"] is False


def test_negative_and_neutral_pairs_remain_in_fixed_denominator(tmp_path: Path) -> None:
    result = subject.evaluate_campaign(
        _freeze(), _campaign(tmp_path, fixed_counts=[10] * 6,
                             adaptive_counts=[12, 12, 12, 12, 10, 9]))
    assert len(result["pairs"]) == 6
    assert [pair["classification"] for pair in result["pairs"]] == [
        "positive", "positive", "positive", "positive", "neutral", "negative"]
    assert result["technical_improvement_gate_passed"] is False


def test_direction_gate_requires_both_execution_orders(tmp_path: Path) -> None:
    result = subject.evaluate_campaign(
        _freeze(), _campaign(tmp_path, fixed_counts=[10] * 6,
                             adaptive_counts=[12, 10, 12, 12, 12, 10]))
    assert result["direction_gate"]["positive_pair_count"] == 4
    assert result["direction_gate"]["forward_positive_count"] == 3
    assert result["direction_gate"]["reverse_positive_count"] == 1
    assert result["technical_improvement_gate_passed"] is False


def test_aggregate_ratio_gate_cannot_be_rescued_by_five_small_wins(tmp_path: Path) -> None:
    result = subject.evaluate_campaign(
        _freeze(), _campaign(tmp_path, fixed_counts=[100] * 6,
                             adaptive_counts=[101, 101, 101, 101, 101, 1]))
    assert result["direction_gate"]["positive_pair_count"] == 5
    assert result["direction_gate"]["passed"] is True
    assert result["aggregate_ratio_gate"]["passed"] is False
    assert result["technical_improvement_gate_passed"] is False


def test_rejects_non_counterbalanced_order(tmp_path: Path) -> None:
    cells = _campaign(tmp_path)
    cells[2]["arm"], cells[3]["arm"] = cells[3]["arm"], cells[2]["arm"]
    with pytest.raises(subject.CampaignEvaluationError, match="AB/BA schedule"):
        subject.evaluate_campaign(_freeze(), cells)


def test_rejects_pilot_approved_before_freeze(tmp_path: Path) -> None:
    cells = _campaign(tmp_path)
    root = Path(cells[0]["root"])
    approval_path = root / "inputs/approved-authorization.json"
    approval = json.loads(approval_path.read_text(encoding="ascii"))
    approval["approved_utc"] = "2026-09-29T23:59:59Z"
    approval_raw = _canonical(approval)
    approval_path.write_bytes(approval_raw)
    receipt_path = root / cells[0]["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="ascii"))
    receipt["artifacts"]["approved_authorization"]["sha256"] = hashlib.sha256(approval_raw).hexdigest()
    receipt.pop("receipt_sha256")
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(receipt)).hexdigest()
    raw = _canonical(receipt)
    receipt_path.write_bytes(raw)
    cells[0]["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(subject.CampaignEvaluationError, match="does not postdate"):
        subject.evaluate_campaign(_freeze(), cells)


def test_rejects_any_cell_not_accepted_by_independent_validator(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(_root: Path, _receipt_relative: Path) -> dict[str, Any]:
        raise subject.CampaignEvaluationError("independent per-arm raw validation failed")
    monkeypatch.setattr(subject, "_validate_accepted_bundle", reject)
    with pytest.raises(subject.CampaignEvaluationError, match="independent per-arm"):
        subject.evaluate_campaign(_freeze(), _campaign(tmp_path))


def test_rejects_caller_authored_count_and_replays_raw_streams(tmp_path: Path) -> None:
    cells = _campaign(tmp_path)
    cells[0]["late_common_commit_count"] = 999
    with pytest.raises(subject.CampaignEvaluationError, match="record schema"):
        subject.evaluate_campaign(_freeze(), cells)


def test_rejects_late_commit_without_all_seven_witnesses(tmp_path: Path) -> None:
    cells = _campaign(tmp_path)
    root = Path(cells[0]["root"])
    receipt_path = root / cells[0]["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="ascii"))
    descriptor = receipt["artifacts"]["replica_events"][6]
    stream_path = root / descriptor["path"]
    lines = stream_path.read_bytes().splitlines(keepends=True)
    stream_path.write_bytes(b"".join(lines[:-1]))
    descriptor["sha256"] = hashlib.sha256(stream_path.read_bytes()).hexdigest()
    receipt.pop("receipt_sha256")
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(receipt)).hexdigest()
    raw = _canonical(receipt)
    receipt_path.write_bytes(raw)
    cells[0]["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(subject.CampaignEvaluationError, match="all-seven raw witnesses"):
        subject.evaluate_campaign(_freeze(), cells)


def test_rejects_missing_post_twenty_second_fault(tmp_path: Path) -> None:
    cells = _campaign(tmp_path)
    root = Path(cells[0]["root"])
    receipt_path = root / cells[0]["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="ascii"))
    descriptor = receipt["artifacts"]["replica_events"][1]
    stream_path = root / descriptor["path"]
    events = [json.loads(line) for line in stream_path.read_text(encoding="utf-8").splitlines()]
    events = [event for event in events if event["event_type"] != "fault.contribution_opportunity"]
    raw_stream = b"".join(_canonical(event) for event in events)
    stream_path.write_bytes(raw_stream)
    descriptor["sha256"] = hashlib.sha256(raw_stream).hexdigest()
    receipt.pop("receipt_sha256")
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(receipt)).hexdigest()
    raw = _canonical(receipt)
    receipt_path.write_bytes(raw)
    cells[0]["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(subject.CampaignEvaluationError, match="no physical omission"):
        subject.evaluate_campaign(_freeze(), cells)


def test_rejects_adaptive_late_common_commits_from_epoch_zero(tmp_path: Path) -> None:
    cells = _campaign(tmp_path)
    cell = next(item for item in cells if item["arm"] == subject.ADAPTIVE_ARM)
    root = Path(cell["root"])
    receipt_path = root / cell["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="ascii"))
    descriptor = receipt["artifacts"]["replica_events"][subject.DESIGNATED_OBSERVER]
    stream_path = root / descriptor["path"]
    events = [json.loads(line) for line in stream_path.read_text(encoding="utf-8").splitlines()]
    for event in events:
        if event["event_type"] == "block.committed":
            event["payload"]["decision_proof"]["epoch_number"] = 0
            event["payload"]["decision_proof"]["epoch_digest"] = "e" * 64
    raw_stream = b"".join(_canonical(event) for event in events)
    stream_path.write_bytes(raw_stream)
    descriptor["sha256"] = hashlib.sha256(raw_stream).hexdigest()
    receipt.pop("receipt_sha256")
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(receipt)).hexdigest()
    raw = _canonical(receipt)
    receipt_path.write_bytes(raw)
    cell["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(subject.CampaignEvaluationError, match="late authoritative commit does not bind"):
        subject.evaluate_campaign(_freeze(), cells)


def test_rejects_adaptive_late_fault_from_foreign_epoch_one_digest(tmp_path: Path) -> None:
    cells = _campaign(tmp_path)
    cell = next(item for item in cells if item["arm"] == subject.ADAPTIVE_ARM)
    root = Path(cell["root"])
    receipt_path = root / cell["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="ascii"))
    descriptor = receipt["artifacts"]["replica_events"][1]
    stream_path = root / descriptor["path"]
    events = [json.loads(line) for line in stream_path.read_text(encoding="utf-8").splitlines()]
    fault = next(event for event in events if event["event_type"] == "fault.contribution_opportunity")
    fault["payload"]["proposal"]["epoch_digest"] = "d" * 64
    raw_stream = b"".join(_canonical(event) for event in events)
    stream_path.write_bytes(raw_stream)
    descriptor["sha256"] = hashlib.sha256(raw_stream).hexdigest()
    receipt.pop("receipt_sha256")
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(receipt)).hexdigest()
    raw = _canonical(receipt)
    receipt_path.write_bytes(raw)
    cell["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(subject.CampaignEvaluationError, match="late physical omission does not bind"):
        subject.evaluate_campaign(_freeze(), cells)


def test_rejects_causal_main_config_drift_even_with_fresh_valid_identity_material(
        tmp_path: Path) -> None:
    cells = _campaign(tmp_path)
    root = Path(cells[-1]["root"])
    receipt_path = root / cells[-1]["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="ascii"))
    main_path = root / receipt["artifacts"]["main_config"]["path"]
    main_raw = main_path.read_bytes().replace(
        b"aggregation-timeout = 0.5\n", b"aggregation-timeout = 0.6\n")
    main_path.write_bytes(main_raw)
    receipt["artifacts"]["main_config"]["sha256"] = hashlib.sha256(main_path.read_bytes()).hexdigest()
    receipt.pop("receipt_sha256")
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(receipt)).hexdigest()
    raw = _canonical(receipt)
    receipt_path.write_bytes(raw)
    cells[-1]["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(subject.CampaignEvaluationError, match="causal settings"):
        subject.evaluate_campaign(_freeze(), cells)


def test_rejects_epoch_zero_topology_drift_between_cells(tmp_path: Path) -> None:
    cells = _campaign(tmp_path)
    root = Path(cells[-1]["root"])
    receipt_path = root / cells[-1]["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="ascii"))
    tree_path = root / receipt["artifacts"]["epoch0_tree"]["path"]
    tree_path.write_bytes(tree_path.read_bytes().replace(
        b"fan:2 pipe:2 0 1 2 3 4 5 6\n",
        b"fan:2 pipe:2 0 2 1 3 4 5 6\n",
        1,
    ))
    receipt["artifacts"]["epoch0_tree"]["sha256"] = hashlib.sha256(tree_path.read_bytes()).hexdigest()
    receipt.pop("receipt_sha256")
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(receipt)).hexdigest()
    raw = _canonical(receipt)
    receipt_path.write_bytes(raw)
    cells[-1]["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(subject.CampaignEvaluationError, match="strictly comparable"):
        subject.evaluate_campaign(_freeze(), cells)


def test_rejects_reused_receipt_even_if_labeled_as_new_cell(tmp_path: Path) -> None:
    cells = _campaign(tmp_path)
    cells[-1]["root"] = cells[-2]["root"]
    cells[-1]["receipt_path"] = cells[-2]["receipt_path"]
    cells[-1]["receipt_sha256"] = cells[-2]["receipt_sha256"]
    with pytest.raises(subject.CampaignEvaluationError):
        subject.evaluate_campaign(_freeze(), cells)


def test_freeze_tamper_is_rejected_before_cells_are_read(tmp_path: Path) -> None:
    freeze = _freeze()
    freeze["design"]["improvement_gate"]["required_positive_pairs"] = 1
    with pytest.raises(subject.CampaignEvaluationError, match="prospective frozen gate"):
        subject.evaluate_campaign(freeze, _campaign(tmp_path))
