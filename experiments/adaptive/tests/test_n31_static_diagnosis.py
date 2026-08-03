"""Evidence-contract tests for the N=31 signer-aware diagnosis."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from experiments.adaptive.kauri_experiment import n31_static_diagnosis as diagnosis

PROFILE_PATH = (
    Path(__file__).parents[1] / "profiles" / "n31-f5-static-diagnosis-v1.json"
)
REVISION = "d" * 40
EPOCH_DIGEST = "145fac093343fa9cff20fcf49d85ad5443e93db14146f7854b17e28cf44f6d7a"
GENESIS_HASH = "a" * 64
BASELINE_HASH = "1" * 64
SELECTED_HASH = "2" * 64
MIDDLE_HASH = "3" * 64
LATER_HASH = "4" * 64
CLAIM_ID = "b" * 64
WITNESS_ID = "c" * 64


@pytest.fixture(scope="module")
def profile() -> diagnosis.FrozenN31StaticDiagnosisProfile:
    return diagnosis.load_frozen_profile(PROFILE_PATH)


def _arm(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    arm_name: str,
) -> diagnosis.DiagnosticArm:
    return next(arm for arm in profile.arms if arm.name == arm_name)


def _source_instances() -> dict[str, str]:
    result = {
        f"replica-{replica}": f"instance-replica-{replica}" for replica in range(31)
    }
    result[diagnosis.MANAGER_SOURCE_ID] = "instance-manager"
    return result


def _append_event(
    streams: dict[str, list[dict[str, Any]]],
    instances: dict[str, str],
    source: str,
    event_type: str,
    payload: dict[str, object],
    timestamp: int,
) -> dict[str, Any]:
    event = {
        "event_schema_version": 1,
        "run_id": "run-001",
        "source_kind": (
            "adaptation_manager" if source == diagnosis.MANAGER_SOURCE_ID else "replica"
        ),
        "source_id": source,
        "source_instance": instances[source],
        "source_sequence": len(streams[source]) + 1,
        "source_monotonic_ns": timestamp,
        "event_type": event_type,
        "payload": payload,
    }
    streams[source].append(event)
    return event


def _active_payload(replica_id: int) -> dict[str, object]:
    return {
        "epoch_number": 0,
        "tree_id": 30,
        "epoch_digest": EPOCH_DIGEST,
        "block_hash": None,
        "context_generation": None,
        "observer_replica": replica_id,
        "wait_exempt_signers": [],
        "accepted_signers": [],
        "absent_direct_children": [],
        "missing_optional_signers": [],
        "required_branch_gaps": [],
        "root_signer_count": 0,
        "global_quorum": 21,
        "rejection_reason": None,
    }


def _commit_observed(
    height: int,
    block_hash: str,
    parent_hash: str,
    batch: int,
) -> dict[str, object]:
    return {
        "block_height": height,
        "block_hash": block_hash,
        "parent_hash": parent_hash,
        "transaction_count": 10,
        "commit_batch_index": batch,
    }


def _append_q21_commit(
    streams: dict[str, list[dict[str, Any]]],
    instances: dict[str, str],
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    *,
    height: int,
    block_hash: str,
    parent_hash: str,
    base_timestamp: int,
    batch: int,
) -> None:
    payload = _commit_observed(height, block_hash, parent_hash, batch)
    for replica_id in profile.commit_witnesses:
        _append_event(
            streams,
            instances,
            f"replica-{replica_id}",
            "block.commit_observed",
            deepcopy(payload),
            base_timestamp + replica_id,
        )
    _append_event(
        streams,
        instances,
        "replica-2",
        "block.committed",
        {
            **payload,
            "designated_observer": True,
            "decision_proof": {
                "epoch_number": 0,
                "tree_id": 30,
                "epoch_digest": EPOCH_DIGEST,
                "block_hash": block_hash,
            },
            "view_generation": height,
        },
        base_timestamp + 100,
    )


def _observation(
    *,
    observation_id: str,
    reporter_id: int,
    observed_replica_id: int,
    message_type: str,
    outcome: str,
    reporter_ns: int,
    signer_set: list[int],
    block_hash: str = SELECTED_HASH,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "observation_id": observation_id,
        "reporter_id": reporter_id,
        "observed_replica_id": observed_replica_id,
        "configuration": {
            "epoch_number": 0,
            "tree_id": 30,
            "epoch_digest": EPOCH_DIGEST,
        },
        "block_hash": block_hash,
        "expected_message_type": message_type,
        "outcome": outcome,
        "response_duration_us": 0 if outcome == "timeout" else 50,
        "deadline_duration_us": 100,
        "reporter_monotonic_ns": reporter_ns,
        "reporter_sequence": 1 if reporter_id == 0 else 2,
        "signer_set": signer_set,
    }


def _root_payload(accepted_signers: list[int]) -> dict[str, object]:
    return {
        "epoch_number": 0,
        "tree_id": 30,
        "epoch_digest": EPOCH_DIGEST,
        "block_hash": SELECTED_HASH,
        "context_generation": 1,
        "observer_replica": 30,
        "wait_exempt_signers": [],
        "accepted_signers": accepted_signers,
        "absent_direct_children": [],
        "missing_optional_signers": [],
        "required_branch_gaps": [],
        "root_signer_count": len(accepted_signers),
        "global_quorum": 21,
        "rejection_reason": None,
    }


def _boundary(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    active_events: list[dict[str, Any]],
) -> dict[str, object]:
    return {
        "epoch_number": 0,
        "tree_id": 30,
        "root_replica": 30,
        "epoch_digest": EPOCH_DIGEST,
        "global_quorum": 21,
        "members_breadth_first": list(profile.phase.members_breadth_first),
        "replica_evidence": [
            {
                "source_id": event["source_id"],
                "source_sequence": event["source_sequence"],
                "source_monotonic_ns": event["source_monotonic_ns"],
            }
            for event in active_events
        ],
    }


def _epoch_witness(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
) -> dict[str, object]:
    return {
        "schema": "kauri-adaptive-v2-epoch-profile-digest-v1",
        "replica_count": 31,
        "fault_threshold": 10,
        "quorum": 21,
        "fanout": 5,
        "pipeline_stretch": 2,
        "membership": list(range(31)),
        "epoch_zero": {
            "schema_version": 2,
            "epoch_number": 0,
            "epoch_digest": EPOCH_DIGEST,
            "tree_count": 31,
        },
    }


def _build_provenance() -> dict[str, object]:
    metadata_names = {
        "cmake_cache",
        "compile_commands",
        "hotstuff_app_link",
        "adaptation_manager_link",
        "epoch_profile_digest_link",
        "hotstuff_keygen_link",
        "hotstuff_tls_keygen_link",
    }
    binary_names = {"app", "manager", "keygen", "tls_keygen", "epoch_profile_digest"}
    return {
        "schema_version": 1,
        "revision": REVISION,
        "repository": "/kauri",
        "build_directory": "/kauri/build-adaptive",
        "cmake_cache_sha256": "5" * 64,
        "build_command": ["cmake", "--build", "build-adaptive"],
        "build_metadata": {
            name: {
                "path": f"/kauri/build-adaptive/{name}",
                "size_bytes": 10,
                "sha256": "6" * 64,
            }
            for name in metadata_names
        },
        "binaries": {
            name: {
                "path": f"/kauri/build-adaptive/{name}",
                "size_bytes": 10,
                "sha256": "7" * 64,
            }
            for name in binary_names
        },
    }


def _preflight(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
) -> dict[str, object]:
    provenance = _build_provenance()
    executables = {
        name: {"path": value["path"], "sha256": value["sha256"]}
        for name, value in provenance["binaries"].items()
    }
    return {
        "schema_version": 1,
        "scenario": diagnosis.SCENARIO,
        "verdict": "PASS",
        "diagnosis_profile": {
            "profile_id": profile.profile_id,
            "sha256": profile.profile_sha256,
        },
        "runtime_profile": {
            "profile_id": profile.runtime_profile_id,
            "sha256": profile.runtime_profile_sha256,
            "path": f"/kauri/{profile.runtime_profile_path}",
        },
        "revision": REVISION,
        "commit_witnesses": list(profile.commit_witnesses),
        "launch_contracts": {
            arm_name: diagnosis.build_launch_contract(
                profile,
                arm=arm_name,
                kauri_revision=REVISION,
            )
            for arm_name in diagnosis.ARM_NAMES
        },
        "profiled_runtime": {
            "schema_version": 1,
            "verdict": "PASS",
            "profile_id": profile.runtime_profile_id,
            "profile_sha256": profile.runtime_profile_sha256,
            "revision": REVISION,
            "required_port_count": 63,
            "fd_soft_limit": 4096,
            "free_disk_bytes": 2 * 1024**3,
            "clock": "CLOCK_MONOTONIC_RAW",
            "build_provenance": provenance,
            "epoch_zero_witness": _epoch_witness(profile),
            "executables": executables,
        },
    }


def _cleanup() -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for replica_id in range(31):
        entries.append(
            {
                "name": f"replica-{replica_id}",
                "replica_id": replica_id,
                "pid": 1000 + replica_id,
                "pgid": 1000 + replica_id,
                "signals_sent": [2],
                "returncode": 0,
                "classification": "expected_cleanup",
                "cleanup_errors": [],
                "cleanup_started_ns": 10_000,
                "cleanup_started_after_post_window": True,
            }
        )
    entries.append(
        {
            "name": diagnosis.MANAGER_SOURCE_ID,
            "replica_id": None,
            "pid": 2000,
            "pgid": 2000,
            "signals_sent": [2],
            "returncode": 1,
            "classification": "expected_cleanup",
            "cleanup_errors": [],
            "cleanup_started_ns": 10_000,
            "cleanup_started_after_post_window": True,
        }
    )
    return entries


def _artifacts() -> list[dict[str, object]]:
    return [
        {"kind": kind, "replica_id": None, "path": path, "sha256": "8" * 64}
        for kind, path in (
            ("diagnosis_profile", "profile.json"),
            ("runtime_profile", "runtime-profile.json"),
            ("build_provenance", "runtime/build-provenance.json"),
            ("epoch_zero_witness", "runtime/epoch-zero-witness.json"),
            ("launch_contract", "runtime/launch-contract.json"),
            ("launch_arguments", "runtime/launch-arguments.json"),
        )
    ]


def _sources(instances: dict[str, str]) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for replica_id in range(31):
        values.append(
            {
                "source_kind": "replica",
                "source_id": f"replica-{replica_id}",
                "source_instance": instances[f"replica-{replica_id}"],
                "pid": 1000 + replica_id,
                "pgid": 1000 + replica_id,
                "path": f"raw/replica-{replica_id}.jsonl",
                "sha256": "9" * 64,
            }
        )
    values.append(
        {
            "source_kind": "adaptation_manager",
            "source_id": diagnosis.MANAGER_SOURCE_ID,
            "source_instance": instances[diagnosis.MANAGER_SOURCE_ID],
            "pid": 2000,
            "pgid": 2000,
            "path": "raw/adaptive-manager.jsonl",
            "sha256": "9" * 64,
        }
    )
    return values


def _marker_lines(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    arm: diagnosis.DiagnosticArm,
) -> tuple[bytes, str, dict[str, object] | None, int]:
    prefix = b"process booted\n"
    configuration = f"0:30:{profile.epoch_digest}"
    if arm.name == diagnosis.ARM_NAMES[0]:
        suppression_line = (
            "KAURI_FAULT false_report_positive_suppressed reporter=0 target=5 "
            f"epoch=0 tree=30 block={SELECTED_HASH} "
            f"window={profile.diagnostic_window} monotonic_ns=5000"
        )
        primary_line = (
            "KAURI_FAULT false_timeout_emitted reporter=0 target=5 "
            f"epoch=0 tree=30 block={SELECTED_HASH} "
            f"window={profile.diagnostic_window} monotonic_ns=5600"
        )
        payload = prefix + f"{suppression_line}\n{primary_line}\n".encode()
        suppression = {
            "kind": "false_report_positive_suppressed",
            "source_id": "replica-0",
            "reporter_id": 0,
            "target_id": 5,
            "configuration": configuration,
            "block_hash": SELECTED_HASH,
            "line": suppression_line,
            "marker_monotonic_ns": 5000,
        }
        return payload, primary_line, suppression, len(prefix)
    primary_line = (
        "KAURI_FAULT direct_vote_omitted replica=5 parent=0 "
        f"epoch=0 tree=30 block={SELECTED_HASH} "
        f"window={profile.diagnostic_window} monotonic_ns=5000"
    )
    payload = prefix + f"{primary_line}\n".encode()
    return payload, primary_line, None, len(prefix)


def _valid_evidence(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    arm_name: str,
) -> dict[str, Any]:
    arm = _arm(profile, arm_name)
    instances = _source_instances()
    streams = {
        **{f"replica-{replica}": [] for replica in range(31)},
        diagnosis.MANAGER_SOURCE_ID: [],
    }
    for source in streams:
        _append_event(
            streams, instances, source, "process.started", {"exit_status": None}, 100
        )
        _append_event(
            streams, instances, source, "process.ready", {"exit_status": None}, 1000
        )

    _append_q21_commit(
        streams,
        instances,
        profile,
        height=100,
        block_hash=BASELINE_HASH,
        parent_hash=GENESIS_HASH,
        base_timestamp=2000,
        batch=0,
    )
    active_events = [
        _append_event(
            streams,
            instances,
            f"replica-{replica_id}",
            "adaptive.configuration_active",
            _active_payload(replica_id),
            4000 + replica_id,
        )
        for replica_id in range(31)
    ]

    witness_signers = list(arm.witness_signer_set)
    claim = _observation(
        observation_id=CLAIM_ID,
        reporter_id=0,
        observed_replica_id=5,
        message_type="direct_vote",
        outcome="timeout",
        reporter_ns=5500,
        signer_set=[],
    )
    witness = _observation(
        observation_id=WITNESS_ID,
        reporter_id=30,
        observed_replica_id=0,
        message_type="aggregate_relay",
        outcome="on_time",
        reporter_ns=5400 if arm_name == diagnosis.ARM_NAMES[1] else 5700,
        signer_set=witness_signers,
    )
    # Deliberately ingest the root witness first. Pair selection must not depend
    # on manager receipt order.
    _append_event(
        streams,
        instances,
        diagnosis.MANAGER_SOURCE_ID,
        "evidence.observation_accepted",
        {"ingestion_sequence": 1, "observation": witness},
        6000,
    )
    _append_event(
        streams,
        instances,
        diagnosis.MANAGER_SOURCE_ID,
        "evidence.observation_accepted",
        {"ingestion_sequence": 2, "observation": claim},
        6100,
    )

    qc_signers = (
        list(range(21))
        if arm_name == diagnosis.ARM_NAMES[0]
        else [replica for replica in range(22) if replica != 5]
    )
    _append_event(
        streams,
        instances,
        "replica-30",
        "aggregation.root_qc_published",
        _root_payload(qc_signers),
        6400,
    )

    _append_event(
        streams,
        instances,
        "replica-2",
        "block.commit_observed",
        _commit_observed(101, MIDDLE_HASH, BASELINE_HASH, 1),
        7200,
    )
    _append_q21_commit(
        streams,
        instances,
        profile,
        height=102,
        block_hash=LATER_HASH,
        parent_hash=MIDDLE_HASH,
        base_timestamp=8000,
        batch=2,
    )

    actor_log, primary_line, suppression, log_start = _marker_lines(profile, arm)
    claim_candidate = {"observation": claim}
    witness_candidate = {"observation": witness}
    _response, signer_certificate = diagnosis.build_signer_aware_certificates(
        profile,
        arm,
        claim_candidate,
        witness_candidate,
    )
    plan = diagnosis.build_fault_plan(profile, arm)
    outcome = {
        "status": "succeeded",
        "fault_id": arm.fault_id,
        "kind": arm.runtime_marker,
        "source_id": f"replica-{arm.actor_replica_id}",
        "actor_replica_id": arm.actor_replica_id,
        "reporter_id": 0,
        "target_id": 5,
        "configuration": f"0:30:{profile.epoch_digest}",
        "block_hash": SELECTED_HASH,
        "context_limit": 64,
        "line": primary_line,
        "log_path": f"logs/replica-{arm.actor_replica_id}.log",
        "log_start_offset": log_start,
        "log_terminal_offset": len(actor_log),
        "matching_line_count": 1,
        "marker_monotonic_ns": 5600 if arm_name == diagnosis.ARM_NAMES[0] else 5000,
        "false_positive_suppression": suppression,
        "pair_observed_monotonic_ns": 6500,
        "diagnostic_certificate_sha256": signer_certificate["certificate_sha256"],
    }
    journal = [
        {
            "schema_version": 1,
            "source_id": "fault-orchestrator",
            "source_sequence": 0,
            "source_monotonic_ns": 3000,
            "fault_id": arm.fault_id,
            "lifecycle": "started",
            "plan_sha256": plan.sha256,
        },
        {
            "schema_version": 1,
            "source_id": "fault-orchestrator",
            "source_sequence": 1,
            "source_monotonic_ns": 7000,
            "fault_id": arm.fault_id,
            "lifecycle": "terminal",
            "plan_sha256": plan.sha256,
            "outcome": outcome,
        },
    ]
    cleanup = _cleanup()
    sources = _sources(instances)
    manifest = {
        "schema_version": 1,
        "scenario": diagnosis.SCENARIO,
        "run_id": "run-001",
        "kauri_revision": REVISION,
        "profile": {
            "profile_id": profile.profile_id,
            "sha256": profile.profile_sha256,
        },
        "runtime_profile": {
            "profile_id": profile.runtime_profile_id,
            "sha256": profile.runtime_profile_sha256,
        },
        "arm": arm_name,
        "fault_plan_sha256": plan.sha256,
        "attempt": 1,
        "attempt_scope": "one_invocation_without_automatic_retry",
        "retry_policy": "none",
        "complete": True,
        "started_utc": "2026-08-03T10:00:00+00:00",
        "finished_utc": "2026-08-03T10:01:00+00:00",
        "preflight": _preflight(profile),
        "runtime_artifacts": _artifacts(),
        "source_instances": instances,
        "sources": sources,
        "actor_log": {
            "path": f"logs/replica-{arm.actor_replica_id}.log",
            "sha256": hashlib.sha256(actor_log).hexdigest(),
        },
        "authoritative_observer": 2,
        "configuration_boundaries": {"diagnostic": _boundary(profile, active_events)},
        "cleanup_ledger": cleanup,
        "runtime_error": None,
    }
    return {
        "manifest": manifest,
        "streams": streams,
        "fault_plan": plan.canonical_json().encode(),
        "fault_journal": journal,
        "actor_log": actor_log,
    }


def _validate(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    evidence: dict[str, Any],
) -> dict[str, object]:
    return diagnosis.validate_n31_static_diagnosis_run(profile, **evidence)


def _renumber(events: list[dict[str, Any]]) -> None:
    for sequence, event in enumerate(events, start=1):
        event["source_sequence"] = sequence


def test_profile_freezes_supervisor_geometry_and_two_mode_ceiling(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
) -> None:
    assert profile.replica_ids == tuple(range(31))
    assert (profile.fault_threshold, profile.quorum) == (10, 21)
    assert (profile.fanout, profile.topology_depth, profile.pipeline_stretch) == (
        5,
        2,
        2,
    )
    assert profile.phase.tree_id == profile.phase.root_replica == 30
    assert profile.phase.reporter_subtree == (0, 5, 6, 7, 8, 9)
    assert (profile.reporter_id, profile.target_id) == (0, 5)
    assert len(profile.commit_witnesses) == 21
    assert not {0, 5} & set(profile.commit_witnesses)
    assert profile.expected_hypothesis_counts == (2, 1)
    assert "arbitrary Byzantine reporter" in profile.limitation
    assert profile.pilot_ceiling == "harness_validation_only"
    assert profile.figure_eligibility == "campaign_PASS_only"


@pytest.mark.parametrize("arm_name", diagnosis.ARM_NAMES)
def test_launch_contract_is_one_tree_actor_local_and_manager_blind(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    arm_name: str,
) -> None:
    arm = _arm(profile, arm_name)
    contract = diagnosis.build_launch_contract(
        profile, arm=arm_name, kauri_revision=REVISION
    )
    active = [entry for entry in contract["replica_overlays"] if entry["argv"]]
    assert [entry["replica_id"] for entry in active] == [arm.actor_replica_id]
    arguments = active[0]["argv"]
    assert "--experiment-byzantine-configuration" in arguments
    assert f"0:30:{profile.epoch_digest}" in arguments
    assert all("additional" not in argument for argument in arguments)
    assert contract["manager_overlay"] == []
    assert contract["required_ready_sources"] == 32
    assert contract["evidence_policy"]["figure_eligibility"] == "campaign_PASS_only"


@pytest.mark.parametrize("arm_name", diagnosis.ARM_NAMES)
def test_same_proposal_signer_crosscheck_passes_both_declared_modes(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    arm_name: str,
) -> None:
    result = _validate(profile, _valid_evidence(profile, arm_name))

    assert result["verdict"] == "PASS"
    assert result["claim_state"] == "live-verified"
    assert result["evidence_ceiling"] == "harness_validation_only"
    assert result["figure_eligible"] is False
    assert result["diagnostic"]["response_only_hypothesis_count"] == 2
    assert result["diagnostic"]["signer_aware_hypothesis_count"] == 1
    assert result["diagnostic"]["claim_manager_receipt_ns"] == 6100
    assert result["diagnostic"]["witness_manager_receipt_ns"] == 6000
    assert result["diagnostic"]["settled_monotonic_ns"] == 6100
    assert result["diagnostic"]["settlement_latency_ns"] == 6100 - 4030
    assert result["commit_ancestry"]["link_count"] == 2
    assert [value["block_height"] for value in result["commit_ancestry"]["chain"]] == [
        100,
        101,
        102,
    ]
    target_present = result["diagnostic"]["root_qc_crosscheck"]["target_present"]
    assert target_present is (arm_name == diagnosis.ARM_NAMES[0])


@pytest.mark.parametrize(
    ("arm_name", "expected_signers", "hypothesis"),
    (
        (
            diagnosis.ARM_NAMES[0],
            [0, 5, 6, 7, 8, 9],
            {"kind": "false_reporter", "replica_id": 0},
        ),
        (
            diagnosis.ARM_NAMES[1],
            [0, 6, 7, 8, 9],
            {"kind": "direct_vote_omitter", "replica_id": 5},
        ),
    ),
)
def test_exact_signer_sets_are_the_only_settling_difference(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    arm_name: str,
    expected_signers: list[int],
    hypothesis: dict[str, object],
) -> None:
    result = _validate(profile, _valid_evidence(profile, arm_name))
    certificate = result["diagnostic"]["signer_aware_certificate"]
    assert certificate["witness"]["signer_set"] == expected_signers
    assert certificate["settled_hypothesis"] == hypothesis
    assert (
        result["diagnostic"]["response_only_certificate"]["compatible_hypothesis_count"]
        == 2
    )


@pytest.mark.parametrize(
    "mutation",
    ("different-block", "different-tree", "signer-drift", "duplicate-claim"),
)
def test_pair_must_be_unique_and_share_full_proposal_identity(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    mutation: str,
) -> None:
    evidence = _valid_evidence(profile, diagnosis.ARM_NAMES[0])
    manager = evidence["streams"][diagnosis.MANAGER_SOURCE_ID]
    witness_event = next(
        event
        for event in manager
        if event["event_type"] == "evidence.observation_accepted"
        and event["payload"]["observation"]["reporter_id"] == 30
    )
    if mutation == "different-block":
        witness_event["payload"]["observation"]["block_hash"] = "e" * 64
    elif mutation == "different-tree":
        witness_event["payload"]["observation"]["configuration"]["tree_id"] = 0
    elif mutation == "signer-drift":
        witness_event["payload"]["observation"]["signer_set"] = [0, 6, 7, 8, 9]
    else:
        duplicate = deepcopy(
            next(
                event
                for event in manager
                if event["event_type"] == "evidence.observation_accepted"
                and event["payload"]["observation"]["reporter_id"] == 0
            )
        )
        duplicate["payload"]["ingestion_sequence"] = 3
        duplicate["payload"]["observation"]["observation_id"] = "e" * 64
        duplicate["source_monotonic_ns"] = 6200
        manager.append(duplicate)
        _renumber(manager)
    with pytest.raises(diagnosis.N31StaticDiagnosisError):
        _validate(profile, evidence)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-qc",
        "duplicate-qc",
        "wrong-target-membership",
        "missing-branch-aggregate",
        "wait-exempt",
        "unknown-signer",
        "compensating-progress",
        "witness-after-qc",
    ),
)
def test_root_qc_is_an_independent_exact_fallback_crosscheck(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    mutation: str,
) -> None:
    arm_name = diagnosis.ARM_NAMES[1]
    evidence = _valid_evidence(profile, arm_name)
    root = evidence["streams"]["replica-30"]
    qc = next(
        event
        for event in root
        if event["event_type"] == "aggregation.root_qc_published"
    )
    if mutation == "missing-qc":
        root.remove(qc)
    elif mutation == "duplicate-qc":
        duplicate = deepcopy(qc)
        duplicate["source_monotonic_ns"] = 6450
        root.append(duplicate)
    elif mutation == "wrong-target-membership":
        qc["payload"]["accepted_signers"][-1] = 5
        qc["payload"]["accepted_signers"].sort()
    elif mutation == "missing-branch-aggregate":
        qc["payload"]["accepted_signers"].remove(9)
        qc["payload"]["root_signer_count"] -= 1
    elif mutation == "wait-exempt":
        qc["payload"]["wait_exempt_signers"] = [5]
    elif mutation == "unknown-signer":
        qc["payload"]["accepted_signers"][-1] = 31
        qc["payload"]["root_signer_count"] = len(qc["payload"]["accepted_signers"])
    elif mutation == "compensating-progress":
        progress = deepcopy(qc)
        progress["event_type"] = "aggregation.root_quorum_progress"
        progress["source_monotonic_ns"] = 6450
        progress["payload"]["accepted_signers"][-1] = 5
        progress["payload"]["accepted_signers"].sort()
        root.append(progress)
    else:
        qc["source_monotonic_ns"] = 5300
    _renumber(root)
    with pytest.raises(diagnosis.N31StaticDiagnosisError):
        _validate(profile, evidence)


@pytest.mark.parametrize(
    "mutation",
    ("missing", "duplicate", "prebaseline", "after-timeout", "journal-drift"),
)
def test_false_report_requires_one_bound_positive_suppression(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    mutation: str,
) -> None:
    evidence = _valid_evidence(profile, diagnosis.ARM_NAMES[0])
    log = evidence["actor_log"].decode()
    suppression = log.splitlines()[1]
    if mutation == "missing":
        evidence["actor_log"] = (log.replace(f"{suppression}\n", "")).encode()
    elif mutation == "duplicate":
        evidence["actor_log"] = (log + f"{suppression}\n").encode()
    elif mutation == "prebaseline":
        evidence["actor_log"] = f"{suppression}\n{log}".encode()
        evidence["fault_journal"][1]["outcome"]["log_start_offset"] += len(
            f"{suppression}\n".encode()
        )
    elif mutation == "after-timeout":
        moved = suppression.replace("monotonic_ns=5000", "monotonic_ns=5700")
        evidence["actor_log"] = log.replace(suppression, moved).encode()
        evidence["fault_journal"][1]["outcome"]["false_positive_suppression"][
            "line"
        ] = moved
        evidence["fault_journal"][1]["outcome"]["false_positive_suppression"][
            "marker_monotonic_ns"
        ] = 5700
    else:
        evidence["fault_journal"][1]["outcome"]["false_positive_suppression"][
            "marker_monotonic_ns"
        ] = 4999
    evidence["fault_journal"][1]["outcome"]["log_terminal_offset"] = len(
        evidence["actor_log"]
    )
    evidence["manifest"]["actor_log"]["sha256"] = hashlib.sha256(
        evidence["actor_log"]
    ).hexdigest()
    with pytest.raises(diagnosis.N31StaticDiagnosisError):
        _validate(profile, evidence)


def test_omission_marker_precedes_both_timeout_and_aggregate_evidence(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
) -> None:
    evidence = _valid_evidence(profile, diagnosis.ARM_NAMES[1])
    line = evidence["fault_journal"][1]["outcome"]["line"]
    changed = line.replace("monotonic_ns=5000", "monotonic_ns=5450")
    evidence["actor_log"] = evidence["actor_log"].replace(
        line.encode(), changed.encode()
    )
    outcome = evidence["fault_journal"][1]["outcome"]
    outcome["line"] = changed
    outcome["marker_monotonic_ns"] = 5450
    evidence["manifest"]["actor_log"]["sha256"] = hashlib.sha256(
        evidence["actor_log"]
    ).hexdigest()
    with pytest.raises(diagnosis.N31StaticDiagnosisError, match="aggregate|omission"):
        _validate(profile, evidence)


def test_common_tree30_boundary_is_fresh_all_source_and_bounded_skew(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
) -> None:
    evidence = _valid_evidence(profile, diagnosis.ARM_NAMES[0])
    boundary = evidence["manifest"]["configuration_boundaries"]["diagnostic"]
    reference = boundary["replica_evidence"][-1]
    source = evidence["streams"][reference["source_id"]]
    event = source[reference["source_sequence"] - 1]
    event["source_monotonic_ns"] = 600_004_030
    reference["source_monotonic_ns"] = 600_004_030
    for offset, later_event in enumerate(
        source[reference["source_sequence"] :], start=1
    ):
        later_event["source_monotonic_ns"] = 600_004_030 + offset
    with pytest.raises(diagnosis.N31StaticDiagnosisError, match="skew"):
        _validate(profile, evidence)


@pytest.mark.parametrize(
    "mutation", ("drop-witness", "wrong-parent", "order-regression")
)
def test_later_commit_requires_q21_endpoints_and_replica2_full_ancestry(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    mutation: str,
) -> None:
    evidence = _valid_evidence(profile, diagnosis.ARM_NAMES[0])
    if mutation == "drop-witness":
        source = evidence["streams"][f"replica-{profile.commit_witnesses[-1]}"]
        source[:] = [
            event
            for event in source
            if not (
                event["event_type"] == "block.commit_observed"
                and event["payload"]["block_hash"] == LATER_HASH
            )
        ]
        _renumber(source)
    else:
        middle = next(
            event
            for event in evidence["streams"]["replica-2"]
            if event["event_type"] == "block.commit_observed"
            and event["payload"]["block_hash"] == MIDDLE_HASH
        )
        if mutation == "wrong-parent":
            middle["payload"]["parent_hash"] = "e" * 64
        else:
            middle["source_monotonic_ns"] = 9000
    with pytest.raises(
        diagnosis.N31StaticDiagnosisError,
        match="ancestry|commit|structured timestamp",
    ):
        _validate(profile, evidence)


@pytest.mark.parametrize(
    "mutation",
    (
        "retry",
        "incomplete",
        "plan",
        "certificate",
        "missing-start",
        "missing-ready",
        "cleanup",
    ),
)
def test_attempt_provenance_readiness_and_closure_fail_closed(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    mutation: str,
) -> None:
    evidence = _valid_evidence(profile, diagnosis.ARM_NAMES[0])
    if mutation == "retry":
        evidence["manifest"]["attempt"] = 2
    elif mutation == "incomplete":
        evidence["manifest"]["complete"] = False
    elif mutation == "plan":
        evidence["fault_plan"] += b"\n"
    elif mutation == "certificate":
        evidence["fault_journal"][1]["outcome"]["diagnostic_certificate_sha256"] = (
            "f" * 64
        )
    elif mutation in {"missing-start", "missing-ready"}:
        source = evidence["streams"]["replica-29"]
        event_type = (
            "process.started" if mutation == "missing-start" else "process.ready"
        )
        source[:] = [event for event in source if event["event_type"] != event_type]
        _renumber(source)
    else:
        evidence["manifest"]["cleanup_ledger"].pop()
    with pytest.raises(diagnosis.N31StaticDiagnosisError):
        _validate(profile, evidence)


def test_complete_log_terminal_offset_ignores_post_terminal_markers(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
) -> None:
    evidence = _valid_evidence(profile, diagnosis.ARM_NAMES[1])
    original_terminal = evidence["fault_journal"][1]["outcome"]["log_terminal_offset"]
    evidence["actor_log"] += (
        "KAURI_FAULT direct_vote_omitted replica=5 parent=0 epoch=0 tree=30 "
        f"block={'e' * 64} window={profile.diagnostic_window} "
        "monotonic_ns=9000\n"
    ).encode()
    evidence["manifest"]["actor_log"]["sha256"] = hashlib.sha256(
        evidence["actor_log"]
    ).hexdigest()
    assert evidence["fault_journal"][1]["outcome"]["log_terminal_offset"] == (
        original_terminal
    )
    assert _validate(profile, evidence)["verdict"] == "PASS"


def test_profile_bytes_are_canonical_and_tamper_rejected(
    profile: diagnosis.FrozenN31StaticDiagnosisProfile,
    tmp_path: Path,
) -> None:
    assert hashlib.sha256(PROFILE_PATH.read_bytes()).hexdigest() == (
        diagnosis.SHIPPED_PROFILE_SHA256
    )
    document = json.loads(PROFILE_PATH.read_text())
    document["quorum"] = 20
    tampered = tmp_path / "profile.json"
    tampered.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(diagnosis.N31StaticDiagnosisError):
        diagnosis.load_frozen_profile(tampered)
