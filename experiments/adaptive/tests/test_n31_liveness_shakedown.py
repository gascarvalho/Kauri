"""End-to-end contract tests for the bounded N31/f5/P liveness shakedown.

The shakedown is diagnostic evidence only.  It may reproduce (or fail to
reproduce) the observed Epoch-2 head-of-line stall, but it can never enter a
factorial denominator or make a thesis performance claim.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import importlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


REPOSITORY = Path(__file__).resolve().parents[3]
V1_PROFILE_PATH = (
    REPOSITORY
    / "experiments"
    / "adaptive"
    / "profiles"
    / "n31-f5-p-liveness-shakedown-v1.json"
)
V2_PROFILE_PATH = V1_PROFILE_PATH.with_name(
    "n31-f5-p-liveness-shakedown-v2.json"
)
PROFILE_PATH = V1_PROFILE_PATH
V1_PROFILE_SHA256 = (
    "906a62db3c3fc636acc9961c9cd65c23625b2eb75ff39f6d49f3c21fc5845b6f"
)
V2_PROFILE_SHA256 = (
    "a1dd1e1c53c7ba01b0f7410e60bf13d409742d5cd5be8423e8403d22d31a3d28"
)
SOURCE_MANIFEST_PATH = (
    REPOSITORY
    / "experiments"
    / "adaptive"
    / "profiles"
    / "shape-placement-factorial-v13.json"
)
SOURCE_SLOT_ID = "slot-066-n31-f5-b05-P"
SOURCE_MANIFEST_SHA256 = (
    "546ce4a3bfecfe62678926f8a0d71cc8ce7db771ed5dd347ef42d15ba39af36b"
)
SOURCE_PLAN_SHA256 = (
    "bbca567114a12034fd41318c856a15eda66bf2cf2efe076398978ca1164bc7ed"
)
REVISION = "a" * 40
BUILD_PROVENANCE = {
    "schema_version": 1,
    "revision": REVISION,
    "repository": str(REPOSITORY),
    "build_directory": str(REPOSITORY / "build-adaptive"),
    "binaries": {
        "app": {"sha256": "1" * 64, "size_bytes": 101},
        "manager": {"sha256": "2" * 64, "size_bytes": 102},
        "keygen": {"sha256": "3" * 64, "size_bytes": 103},
        "tls_keygen": {"sha256": "4" * 64, "size_bytes": 104},
    },
    "build_metadata": {
        "cmake_cache": {"sha256": "5" * 64, "size_bytes": 105},
        "compile_commands": {"sha256": "6" * 64, "size_bytes": 106},
    },
}
BUILD_PROVENANCE_SHA256 = hashlib.sha256(
    json.dumps(
        BUILD_PROVENANCE,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
).hexdigest()
APPROVAL_RECEIPT = {
    "schema_version": 1,
    "scope": "n31_f5_p_liveness_shakedown_non_claim_v1",
    "authorized_by": "thesis_author",
    "approval_reference": "approved bounded liveness shakedown",
    "approved_utc": "2026-08-09T12:00:00Z",
    "profile_id": "n31-f5-p-liveness-shakedown-v1",
    "kauri_revision": REVISION,
    "build_provenance_sha256": BUILD_PROVENANCE_SHA256,
    "maximum_attempts": 2,
    "attempts_per_invocation": 2,
    "automatic_retries": 0,
    "replacement_policy": "none",
    "outcome_dependent_launch": False,
    "campaign_member": False,
    "denominator_contribution": 0,
    "figure_eligible": False,
}
APPROVAL_RECEIPT_SHA256 = hashlib.sha256(
    json.dumps(
        APPROVAL_RECEIPT,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
).hexdigest()
V2_APPROVAL_RECEIPT = {
    **APPROVAL_RECEIPT,
    "scope": "n31_f5_p_liveness_shakedown_non_claim_v2",
    "profile_id": "n31-f5-p-liveness-shakedown-v2",
}
FAST_ROOTS = (
    11,
    14,
    17,
    12,
    10,
    6,
    13,
    18,
    9,
    4,
    20,
    19,
    28,
    15,
    26,
    24,
    21,
    27,
    23,
    0,
    30,
)
FIRST_EPOCH2_COMMON_COMMIT_NS = 1_000_000_000
OBSERVATION_DEADLINE_NS = FIRST_EPOCH2_COMMON_COMMIT_NS + 45_000_000_000
PAIR_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _native_event(
    *,
    source: str,
    event_type: str,
    timestamp_ns: int,
    payload: dict[str, object],
    line_number: int = 1,
    line_sha256: str = "9" * 64,
) -> SimpleNamespace:
    return SimpleNamespace(
        source=source,
        relative_path=f"raw/{source}.jsonl",
        line_number=line_number,
        timestamp_ns=timestamp_ns,
        line_sha256=line_sha256,
        value={"event_type": event_type, "payload": payload},
    )


def _commit_payload(*, authoritative: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "block_height": 81,
        "block_hash": "a" * 64,
        "parent_hash": "b" * 64,
        "transaction_count": 1000,
    }
    if authoritative:
        payload.update(
            decision_proof={
                "epoch_number": 2,
                "tree_id": 0,
                "epoch_digest": "c" * 64,
                "block_hash": "a" * 64,
            },
            view_generation=4,
        )
    return payload


def _module():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.n31_liveness_shakedown"
    )


def _cli():
    return importlib.import_module(
        "experiments.adaptive.run_n31_liveness_shakedown"
    )


@pytest.fixture(scope="module")
def profile():
    return _module().load_frozen_profile(PROFILE_PATH)


def _profile_document(profile: Any) -> dict[str, Any]:
    value = asdict(profile) if hasattr(profile, "__dataclass_fields__") else profile
    assert isinstance(value, dict)
    return value


def _complete_observation(*, reproduced: bool) -> dict[str, object]:
    stalls: list[dict[str, object]] = []
    if reproduced:
        stalls.append(
            {
                "replica_id": 13,
                "root_id": 13,
                "non_injected": True,
                "lag_observed": True,
                "survived_sigint": True,
                "survived_sigterm": True,
                "pre_kill_stack_valid": True,
                "peer_timeout_view_progress": True,
                "activated_epoch_digest": "1" * 64,
                "queue_evidence_sha256": "c" * 64,
                "queue_head_position": 0,
                "later_queue_position": 1,
                "head_context_generation": 7,
                "later_context_generation": 8,
                "head_block_height": 90,
                "later_block_height": 91,
                "head_block_hash": "a" * 64,
                "later_block_hash": "b" * 64,
                "later_parent_hash": "a" * 64,
                "head_verified_signer_count": 16,
                "later_verified_signer_count": 25,
                "quorum": 21,
                "later_qc_ready": True,
                "later_qc_published": False,
                "blocked_by_queue_head": True,
                "leader_progress_timeout_observed": True,
                "commit_gap_ns": 21_200_000_000,
                "source_event_sha256s": ["c" * 64, "d" * 64, "e" * 64],
                "stack_sample_sha256": "f" * 64,
            }
        )
    return {
        "schema_version": 1,
        "profile_id": "n31-f5-p-liveness-shakedown-v1",
        "source_slot_id": SOURCE_SLOT_ID,
        "pair_id": PAIR_ID,
        "revision": REVISION,
        "build_provenance_sha256": BUILD_PROVENANCE_SHA256,
        "approval_receipt_sha256": APPROVAL_RECEIPT_SHA256,
        "attempt": {
            "pair_id": PAIR_ID,
            "ordinal": 1,
            "launch_count": 1,
            "retry_count": 0,
            "replacement_count": 0,
            "revision": REVISION,
            "build_provenance_sha256": BUILD_PROVENANCE_SHA256,
            "approval_receipt_sha256": APPROVAL_RECEIPT_SHA256,
        },
        "integrity": {
            "profile_exact": True,
            "provenance_exact": True,
            "approval_exact": True,
            "raw_evidence_complete": True,
            "seal_valid": True,
        },
        "execution": {
            "cleanup_complete": True,
            "unexpected_process_exit_count": 0,
            "consensus_conflict_count": 0,
            "all_processes_exited_after_sigint": not reproduced,
        },
        "epoch2": {
            "first_common_commit_ns": FIRST_EPOCH2_COMMON_COMMIT_NS,
            "observation_deadline_ns": OBSERVATION_DEADLINE_NS,
            "observation_end_ns": OBSERVATION_DEADLINE_NS,
            "final_common_commit_ns": OBSERVATION_DEADLINE_NS,
            "selected_root_ids": list(FAST_ROOTS),
            "exercised_root_ids": list(FAST_ROOTS),
            "activated_epoch_digest": "1" * 64,
        },
        "liveness": {
            "head_of_line_stalls": stalls,
            "leader_progress_timeout_count": 1 if reproduced else 0,
            "q21_progress_continued": True,
            "max_authoritative_commit_gap_ns": (
                21_200_000_000 if reproduced else 1_000_000_000
            ),
            "max_common_q21_commit_gap_ns": 1_000_000_000,
        },
    }


def _complete_sigint_cleanup_rows() -> list[dict[str, object]]:
    rows = [
        {
            "name": f"replica-{replica_id}",
            "replica_id": replica_id,
            "pid": 20_000 + replica_id,
            "pgid": 20_000 + replica_id,
            "signal_number": 2,
            "returncode": -2,
            "classification": "expected_cleanup",
            "exit_authorization": None,
        }
        for replica_id in range(31)
    ]
    rows.append(
        {
            "name": "adaptive-manager",
            "replica_id": None,
            "pid": 30_000,
            "pgid": 30_000,
            "signal_number": 2,
            "returncode": -2,
            "classification": "expected_cleanup",
            "exit_authorization": None,
        }
    )
    return rows


def test_frozen_profile_is_exactly_the_v13_slot_066_p_contract(profile: Any) -> None:
    module = _module()
    document = _profile_document(profile)
    assert profile.profile_sha256 == V1_PROFILE_SHA256
    assert hashlib.sha256(SOURCE_MANIFEST_PATH.read_bytes()).hexdigest() == (
        SOURCE_MANIFEST_SHA256
    )
    assert document["profile_id"] == "n31-f5-p-liveness-shakedown-v1"
    assert document["source"] == {
        "manifest_id": "shape-placement-factorial-v13",
        "manifest_sha256": SOURCE_MANIFEST_SHA256,
        "plan_sha256": SOURCE_PLAN_SHA256,
        "slot_id": SOURCE_SLOT_ID,
        "slot_ordinal": 66,
        "block_id": "n31-f5-b05",
        "arm_code": "P",
        "scientific_seed": 41735,
    }
    assert document["consensus"] == {
        "replica_count": 31,
        "fault_threshold": 10,
        "quorum": 21,
        "initial_fanout": 5,
        "pipeline_stretch": 2,
        "tree_count": 21,
    }
    assert document["adaptation"] == {
        "placement": True,
        "shape": False,
        "candidate_fanouts": [2, 3, 5],
        "epoch_fanout_policy": "one_uniform_fanout_per_epoch",
        "pipeline_policy": "fixed_first_slice",
    }
    assert document["fault_cohorts"] == {
        "mode": "tiered_persistent_responsive_omission_v2",
        "hard_actor_ids": [22, 25, 29],
        "responsive_degraded_actor_ids": [1, 2, 3, 5, 7, 8, 16],
        "responsive_omission_period": 41,
        "hard_actions": {
            "root": "normal",
            "internal": "omit_aggregate",
            "leaf": "omit_direct_vote",
        },
    }
    assert document["workload"] == {
        "block_size": 1000,
        "piped_latency_ms": 1,
        "tree_switch_period_blocks": 1,
    }
    assert document["responsiveness_policy"] == {
        "policy_version": "shape25-direct-vote-responsiveness-v2",
        "attempt_window": 128,
        "minimum_attempts": 60,
        "minimum_response_rate_ppm": 950000,
        "maximum_timeout_rate_ppm": 50000,
        "trailing_timeout_streak": 7,
        "latency_percentile_basis_points": 5000,
    }
    assert document["timers"] == {
        "global_worst_candidate_depth": 4,
        "aggregation_timeout_ms_per_depth": 125,
        "leader_progress_timeout_ms_per_depth": 5000,
        "leader_activation_grace_ms": 1000,
        "activation_delay_blocks": 5,
        "transition_convergence_deadline_s": 20,
        "schedule_slack_s": 30,
        "drain_margin_s": 20,
        "startup_timeout_s": 120,
        "hard_timeout_s": 500,
    }
    assert document["transitions"] == [
        {
            "successor_epoch": 1,
            "policy_intent": "fault_containment",
            "minimum_post_baseline_observation_ms": 180000,
            "minimum_predecessor_residency_ms": 0,
            "apply_shape_selection": False,
        },
        {
            "successor_epoch": 2,
            "policy_intent": "performance_optimization",
            "minimum_post_baseline_observation_ms": 0,
            "minimum_predecessor_residency_ms": 30000,
            "apply_shape_selection": False,
        },
    ]


def test_v2_release_is_the_exact_pinned_profile_id_delta() -> None:
    module = _module()
    v1 = json.loads(V1_PROFILE_PATH.read_bytes())
    v2 = json.loads(V2_PROFILE_PATH.read_bytes())
    assert hashlib.sha256(V1_PROFILE_PATH.read_bytes()).hexdigest() == (
        V1_PROFILE_SHA256
    )
    assert hashlib.sha256(V2_PROFILE_PATH.read_bytes()).hexdigest() == (
        V2_PROFILE_SHA256
    )
    assert v1.pop("profile_id") == "n31-f5-p-liveness-shakedown-v1"
    assert v2.pop("profile_id") == "n31-f5-p-liveness-shakedown-v2"
    assert v2 == v1
    assert module.load_frozen_profile(V1_PROFILE_PATH).profile_sha256 == (
        V1_PROFILE_SHA256
    )
    assert module.load_frozen_profile(V2_PROFILE_PATH).profile_sha256 == (
        V2_PROFILE_SHA256
    )


def test_release_registry_pins_distinct_roots_and_approval_scopes() -> None:
    releases = {
        release.profile_id: release for release in _module().SHIPPED_RELEASES
    }
    v1 = releases["n31-f5-p-liveness-shakedown-v1"]
    v2 = releases["n31-f5-p-liveness-shakedown-v2"]
    assert v1.canonical_results_relative_path == Path(
        "results/n31-f5-p-liveness-shakedown-v1"
    )
    assert v2.canonical_results_relative_path == Path(
        "results/n31-f5-p-liveness-shakedown-v2"
    )
    assert v1.approval_scope == (
        "n31_f5_p_liveness_shakedown_non_claim_v1"
    )
    assert v2.approval_scope == (
        "n31_f5_p_liveness_shakedown_non_claim_v2"
    )


@pytest.mark.parametrize("profile_path", (V1_PROFILE_PATH, V2_PROFILE_PATH))
def test_known_profile_id_still_requires_its_exact_release_hash(
    tmp_path: Path,
    profile_path: Path,
) -> None:
    mutated = tmp_path / profile_path.name
    mutated.write_bytes(profile_path.read_bytes() + b" ")
    with pytest.raises(
        _module().N31LivenessShakedownError,
        match="profile bytes",
    ):
        _module().load_frozen_profile(mutated)


def test_swapped_profile_id_and_release_hash_are_rejected(profile: Any) -> None:
    module = _module()
    swapped = replace(
        profile,
        profile_id="n31-f5-p-liveness-shakedown-v2",
    )
    with pytest.raises(
        module.N31LivenessShakedownError,
        match="release identity",
    ):
        module.build_approval_receipt(
            swapped,
            approval_reference="must reject swapped identity",
            approved_utc="2026-08-09T12:00:00Z",
            kauri_revision=REVISION,
            build_provenance_sha256=BUILD_PROVENANCE_SHA256,
        )


def test_v1_approval_cannot_authorize_v2() -> None:
    module = _module()
    profile = module.load_frozen_profile(V2_PROFILE_PATH)
    with pytest.raises(module.N31LivenessShakedownError, match="approval"):
        module.preflight_from_documents(
            profile,
            current_revision=REVISION,
            build_provenance=BUILD_PROVENANCE,
            approval_receipt=APPROVAL_RECEIPT,
        )


def test_v1_and_v2_attempt_caps_are_independent(tmp_path: Path) -> None:
    module = _module()
    for release_offset, release in enumerate(module.SHIPPED_RELEASES):
        root = tmp_path / release.canonical_results_relative_path.name
        for ordinal in range(2):
            digit = str(ordinal + 1)
            module.allocate_attempt_directory(
                root,
                timestamp_utc=f"20260809T12000{ordinal}.000000Z",
                process_id=4100 + release_offset,
                attempt_uuid=(
                    f"{digit * 8}-{digit * 4}-4{digit * 3}-8{digit * 3}-"
                    f"{digit * 12}"
                ),
            )
        with pytest.raises(
            module.N31LivenessShakedownError,
            match="two attempts",
        ):
            module.allocate_attempt_directory(
                root,
                timestamp_utc="20260809T120002.000000Z",
                process_id=4100 + release_offset,
                attempt_uuid="33333333-3333-4333-8333-333333333333",
            )


@pytest.mark.parametrize("profile_path", (V1_PROFILE_PATH, V2_PROFILE_PATH))
def test_live_preflight_rejects_arbitrary_roots_before_launch(
    tmp_path: Path,
    profile_path: Path,
) -> None:
    module = _module()
    with pytest.raises(
        module.N31LivenessShakedownError,
        match="canonical result root",
    ):
        module.preflight(
            profile_path=profile_path,
            repository=REPOSITORY,
            results_root=tmp_path / "arbitrary",
            approval_receipt_path=tmp_path / "external-approval.json",
        )


def test_v2_attempt_archives_exact_selected_bytes_and_metadata(
    tmp_path: Path,
) -> None:
    module = _module()
    profile = module.load_frozen_profile(V2_PROFILE_PATH)
    receipt = module.preflight_from_documents(
        profile,
        current_revision=REVISION,
        build_provenance=BUILD_PROVENANCE,
        approval_receipt=V2_APPROVAL_RECEIPT,
    )

    def stop(*_args: object) -> None:
        raise RuntimeError("stop")

    attempt, verdict = module._run_attempt_once(
        profile=profile,
        preflight_receipt=receipt,
        approval_receipt=V2_APPROVAL_RECEIPT,
        results_root=tmp_path / "v2",
        pair_id=PAIR_ID,
        execute_attempt=stop,
        timestamp_utc="20260809T120000.000000Z",
        process_id=4101,
        attempt_uuid="11111111-1111-4111-8111-111111111111",
    )
    assert verdict == "INCOMPLETE"
    assert (attempt / "profile.json").read_bytes() == V2_PROFILE_PATH.read_bytes()
    for name in ("attempt.json", "preflight-receipt.json", "terminal.json"):
        assert json.loads((attempt / name).read_bytes())["profile_id"] == (
            profile.profile_id
        )
    approval = json.loads((attempt / "approval-receipt.json").read_bytes())
    assert approval["scope"] == "n31_f5_p_liveness_shakedown_non_claim_v2"


def test_timeout_launch_and_cleanup_artifacts_use_selected_profile_id(
    tmp_path: Path,
) -> None:
    module = _module()
    profile_id = "n31-f5-p-liveness-shakedown-v2"
    source = module._source_slot()
    offsets = {
        relative: 0
        for replica_id in range(source.runtime.replica_count)
        for relative in (
            source.runtime.process_logs.replica_stdout_relative_paths[replica_id],
            source.runtime.process_logs.replica_stderr_relative_paths[replica_id],
        )
    }
    capture = module._timeout_capture_document(
        profile_id=profile_id,
        first_common_commit_ns=1,
        observation_deadline_ns=2,
        start_offsets=offsets,
        end_offsets=offsets,
    )
    diagnostics = tmp_path / "raw/diagnostics"
    diagnostics.mkdir(parents=True)
    (diagnostics / "leader-timeout-window.json").write_bytes(
        module._canonical_json_bytes(capture, newline=True)
    )
    assert module._read_timeout_capture(
        source.runtime,
        tmp_path,
        profile_id=profile_id,
        first_common_commit_ns=1,
        observation_deadline_ns=2,
    ) == (offsets, offsets)
    launch = module._launch_document(
        source,
        SimpleNamespace(
            redaction_key_id="a" * 16,
            redacted_manager_argv=("manager",),
            redacted_replica_argv=(),
        ),
        profile_id=profile_id,
        pair_id=PAIR_ID,
        attempt_ordinal=1,
        anchor_ns=1,
    )
    assert launch["profile_id"] == profile_id
    module._write_cleanup_artifacts(
        tmp_path,
        (),
        (),
        profile_id=profile_id,
        cleanup_started_ns=1,
        cleanup_complete=False,
        streams_closed=False,
        ports_clear=False,
        final_events_complete=False,
        error="test",
    )
    for path in (
        tmp_path / "cleanup-ledger.json",
        diagnostics / "cleanup-samples.json",
    ):
        assert json.loads(path.read_bytes())["profile_id"] == profile_id


def test_profile_is_non_claim_and_permits_only_two_one_shot_attempts(
    profile: Any,
) -> None:
    document = _profile_document(profile)
    assert document["attempt_policy"] == {
        "maximum_attempts": 2,
        "attempts_per_invocation": 2,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "outcome_dependent_launch": False,
    }
    assert document["evidence_scope"] == {
        "claim_eligible": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }
    assert document["observation"] == {
        "anchor": "first_common_epoch2_commit",
        "duration_s": 45,
        "required_selected_root_count": 21,
        "require_every_selected_root_exercised": True,
    }


def test_profile_byte_tampering_is_rejected(tmp_path: Path) -> None:
    module = _module()
    raw = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    raw["evidence_scope"]["figure_eligible"] = True
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(module.N31LivenessShakedownError, match="profile bytes"):
        module.load_frozen_profile(tampered)


def test_preflight_binds_current_revision_build_and_approval_receipt(
    profile: Any,
) -> None:
    module = _module()
    result = module.preflight_from_documents(
        profile,
        current_revision=REVISION,
        build_provenance=BUILD_PROVENANCE,
        approval_receipt=APPROVAL_RECEIPT,
    )
    assert result == {
        "schema_version": 1,
        "status": "READY",
        "profile_id": profile.profile_id,
        "profile_sha256": profile.profile_sha256,
        "source_slot_id": SOURCE_SLOT_ID,
        "revision": REVISION,
        "build_provenance_sha256": BUILD_PROVENANCE_SHA256,
        "approval_receipt_sha256": APPROVAL_RECEIPT_SHA256,
        "maximum_attempts": 2,
        "attempts_per_invocation": 2,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "outcome_dependent_launch": False,
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("revision", "revision"),
        ("build", "build provenance"),
        ("approval-revision", "approval"),
        ("attempts", "attempt"),
        ("attempts-per-invocation", "attempt"),
        ("outcome-dependent", "attempt"),
        ("figure", "figure"),
    ),
)
def test_preflight_rejects_nonexact_or_claim_eligible_receipts(
    profile: Any,
    mutation: str,
    message: str,
) -> None:
    module = _module()
    current_revision = REVISION
    build = deepcopy(BUILD_PROVENANCE)
    approval = deepcopy(APPROVAL_RECEIPT)
    if mutation == "revision":
        current_revision = "b" * 40
    elif mutation == "build":
        build["revision"] = "b" * 40
    elif mutation == "approval-revision":
        approval["kauri_revision"] = "b" * 40
    elif mutation == "attempts":
        approval["maximum_attempts"] = 3
    elif mutation == "attempts-per-invocation":
        approval["attempts_per_invocation"] = 1
    elif mutation == "outcome-dependent":
        approval["outcome_dependent_launch"] = True
    else:
        approval["figure_eligible"] = True
    with pytest.raises(module.N31LivenessShakedownError, match=message):
        module.preflight_from_documents(
            profile,
            current_revision=current_revision,
            build_provenance=build,
            approval_receipt=approval,
        )


def test_preflight_replay_rejects_hash_bound_but_unauthorized_approval(
    profile: Any,
) -> None:
    module = _module()
    receipt = module.preflight_from_documents(
        profile,
        current_revision=REVISION,
        build_provenance=BUILD_PROVENANCE,
        approval_receipt=APPROVAL_RECEIPT,
    )
    unauthorized = {**APPROVAL_RECEIPT, "authorized_by": "not-thesis-author"}
    receipt["approval_receipt_sha256"] = module._sha256_bytes(
        module._canonical_json_bytes(unauthorized)
    )

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="approval.*policy|approval.*scope",
    ):
        module._validate_preflight_receipt(
            profile,
            receipt,
            unauthorized,
        )


def test_preflight_replay_rejects_contradictory_extra_claim_field(
    profile: Any,
) -> None:
    module = _module()
    receipt = module.preflight_from_documents(
        profile,
        current_revision=REVISION,
        build_provenance=BUILD_PROVENANCE,
        approval_receipt=APPROVAL_RECEIPT,
    )
    receipt["claim_eligible"] = True

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="preflight receipt schema",
    ):
        module._validate_preflight_receipt(
            profile,
            receipt,
            APPROVAL_RECEIPT,
        )


def test_observation_schema_rejects_extra_claim_truth_field(profile: Any) -> None:
    module = _module()
    observation = {
        **_complete_observation(reproduced=False),
        "configuration_progress": {},
        "claim_eligible": True,
    }

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="observation schema",
    ):
        module._validate_observation_schema(profile, observation)


def test_attempt_allocation_is_exclusive_timestamp_pid_uuid_and_stops_at_two(
    tmp_path: Path,
) -> None:
    module = _module()
    root = tmp_path / "liveness-shakedown"
    first = module.allocate_attempt_directory(
        root,
        timestamp_utc="20260809T120000.000000Z",
        process_id=4101,
        attempt_uuid="11111111-1111-4111-8111-111111111111",
    )
    second = module.allocate_attempt_directory(
        root,
        timestamp_utc="20260809T120001.000000Z",
        process_id=4102,
        attempt_uuid="22222222-2222-4222-8222-222222222222",
    )

    assert first.name == (
        "attempt-01-20260809T120000.000000Z-pid4101-"
        "11111111-1111-4111-8111-111111111111"
    )
    assert second.name == (
        "attempt-02-20260809T120001.000000Z-pid4102-"
        "22222222-2222-4222-8222-222222222222"
    )
    assert first.is_dir() and second.is_dir()
    with pytest.raises(module.N31LivenessShakedownError, match="two attempts"):
        module.allocate_attempt_directory(
            root,
            timestamp_utc="20260809T120002.000000Z",
            process_id=4103,
            attempt_uuid="33333333-3333-4333-8333-333333333333",
        )
    assert len(tuple(root.iterdir())) == 2


def test_attempt_identity_collision_is_rejected_without_reuse(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "liveness-shakedown"
    kwargs = {
        "timestamp_utc": "20260809T120000.000000Z",
        "process_id": 4101,
        "attempt_uuid": "11111111-1111-4111-8111-111111111111",
    }
    original = module.allocate_attempt_directory(root, **kwargs)
    sentinel = original / "must-stay.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")
    with pytest.raises(module.N31LivenessShakedownError, match="collision"):
        module.allocate_attempt_directory(root, **kwargs)
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"


def test_live_execution_root_materializes_frozen_slot_argv_without_processes(
    tmp_path: Path,
) -> None:
    from experiments.adaptive.kauri_experiment.factorial_runtime import (
        ManagerSecretMaterial,
        materialize_manager_argv,
    )

    module = _module()
    source = module._source_slot()
    attempt = module.allocate_attempt_directory(
        tmp_path / "results",
        timestamp_utc="20260809T120000.000000Z",
        process_id=4101,
        attempt_uuid="11111111-1111-4111-8111-111111111111",
    )
    execution_root = module._live_execution_root(attempt, create=True)
    secrets = ManagerSecretMaterial(
        manager_tls_private_key_der_hex="a1b2",
        manager_tls_certificate_der_hex="c3d4",
        issuer_private_key_hex="11" * 32,
        replica_tls_certificate_der_hex=tuple(
            f"{replica_id + 1:02x}"
            for replica_id in range(source.runtime.replica_count)
        ),
    )

    argv = materialize_manager_argv(source.runtime, execution_root, secrets)

    assert execution_root.parent == attempt
    assert execution_root.name == SOURCE_SLOT_ID
    assert any(str(execution_root) in argument for argument in argv)


def test_reproduced_requires_exact_head_of_line_evidence(profile: Any) -> None:
    assert _module().evaluate_verdict(profile, _complete_observation(reproduced=True)) == (
        "REPRODUCED"
    )


def test_not_reproduced_requires_a_complete_45_second_all_root_observation(
    profile: Any,
) -> None:
    assert _module().evaluate_verdict(
        profile,
        _complete_observation(reproduced=False),
    ) == "NOT_REPRODUCED"


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-common-commit",
        "short-window",
        "missing-root",
        "extra-root",
        "bad-seal",
        "provenance-drift",
        "unexpected-exit",
        "retry",
        "q21-stalled",
        "leader-timeout",
        "non-sigint-exit",
        "malformed-stall",
        "injected-stall",
        "no-lag",
        "no-sigint-survival",
        "no-sigterm-survival",
        "bad-stack",
        "no-peer-progress",
        "stall-digest-mismatch",
    ),
)
def test_incomplete_is_fail_closed_for_missing_or_untrusted_evidence(
    profile: Any,
    mutation: str,
) -> None:
    reproduced_mutations = {
        "malformed-stall",
        "injected-stall",
        "no-lag",
        "no-sigint-survival",
        "no-sigterm-survival",
        "bad-stack",
        "no-peer-progress",
        "stall-digest-mismatch",
    }
    observation = _complete_observation(reproduced=mutation in reproduced_mutations)
    epoch2 = observation["epoch2"]
    integrity = observation["integrity"]
    execution = observation["execution"]
    attempt = observation["attempt"]
    liveness = observation["liveness"]
    assert isinstance(epoch2, dict)
    assert isinstance(integrity, dict)
    assert isinstance(execution, dict)
    assert isinstance(attempt, dict)
    assert isinstance(liveness, dict)
    if mutation == "missing-common-commit":
        epoch2["first_common_commit_ns"] = None
    elif mutation == "short-window":
        epoch2["observation_end_ns"] = OBSERVATION_DEADLINE_NS - 1
    elif mutation == "missing-root":
        epoch2["exercised_root_ids"] = list(FAST_ROOTS[:-1])
    elif mutation == "extra-root":
        epoch2["selected_root_ids"] = [*FAST_ROOTS, 7]
        epoch2["exercised_root_ids"] = [*FAST_ROOTS, 7]
    elif mutation == "bad-seal":
        integrity["seal_valid"] = False
    elif mutation == "provenance-drift":
        observation["build_provenance_sha256"] = "f" * 64
    elif mutation == "unexpected-exit":
        execution["unexpected_process_exit_count"] = 1
    elif mutation == "retry":
        attempt["retry_count"] = 1
    elif mutation == "q21-stalled":
        liveness["q21_progress_continued"] = False
    elif mutation == "leader-timeout":
        liveness["leader_progress_timeout_count"] = 1
    elif mutation == "non-sigint-exit":
        execution["all_processes_exited_after_sigint"] = False
    else:
        stalls = liveness["head_of_line_stalls"]
        assert isinstance(stalls, list)
        field = {
            "malformed-stall": "later_qc_ready",
            "injected-stall": "non_injected",
            "no-lag": "lag_observed",
            "no-sigint-survival": "survived_sigint",
            "no-sigterm-survival": "survived_sigterm",
            "bad-stack": "pre_kill_stack_valid",
            "no-peer-progress": "peer_timeout_view_progress",
            "stall-digest-mismatch": "activated_epoch_digest",
        }[mutation]
        stalls[0][field] = (
            "2" * 64 if mutation == "stall-digest-mismatch" else False
        )
    assert _module().evaluate_verdict(profile, observation) == "INCOMPLETE"


def test_evidence_seal_preserves_attempt_and_detects_raw_tampering(
    tmp_path: Path,
) -> None:
    module = _module()
    attempt = tmp_path / "attempt"
    raw = attempt / "raw" / "replica-0.jsonl"
    raw.parent.mkdir(parents=True)
    raw.write_text('{"event_type":"block.committed"}\n', encoding="utf-8")
    (attempt / "attempt.json").write_text(
        json.dumps({"attempt_ordinal": 1}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    seal_sha256 = module.create_evidence_seal(attempt)
    assert len(seal_sha256) == 64
    assert module.verify_evidence_seal(attempt) == seal_sha256

    raw.write_text('{"event_type":"forged"}\n', encoding="utf-8")
    with pytest.raises(module.N31LivenessShakedownError, match="seal"):
        module.verify_evidence_seal(attempt)


def test_private_attempt_member_preserves_and_seals_incomplete_without_retry(
    tmp_path: Path,
    profile: Any,
) -> None:
    module = _module()
    calls: list[Path] = []

    def fail_once(attempt_directory: Path, _profile: Any) -> None:
        calls.append(attempt_directory)
        raise RuntimeError("injected launch failure")

    attempt, verdict = module._run_attempt_once(
        profile=profile,
        preflight_receipt=module.preflight_from_documents(
            profile,
            current_revision=REVISION,
            build_provenance=BUILD_PROVENANCE,
            approval_receipt=APPROVAL_RECEIPT,
        ),
        approval_receipt=APPROVAL_RECEIPT,
        results_root=tmp_path / "results",
        pair_id=PAIR_ID,
        execute_attempt=fail_once,
        timestamp_utc="20260809T120000.000000Z",
        process_id=4101,
        attempt_uuid="11111111-1111-4111-8111-111111111111",
    )

    assert verdict == "INCOMPLETE"
    assert calls == [attempt]
    terminal = json.loads((attempt / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["verdict"] == "INCOMPLETE"
    assert terminal["launch_count"] == 1
    assert terminal["retry_count"] == 0
    assert terminal["replacement_count"] == 0
    assert terminal["campaign_member"] is False
    assert terminal["denominator_contribution"] == 0
    assert terminal["figure_eligible"] is False
    assert "RuntimeError: injected launch failure" in terminal["reason"]
    assert (attempt / "evidence-seal.json").is_file()
    module.verify_evidence_seal(attempt)
    validated = module._validate_attempt(
        attempt,
        approval_receipt=APPROVAL_RECEIPT,
        expected_pair_id=PAIR_ID,
    )
    assert validated["verdict"] == "INCOMPLETE"
    assert validated["figure_eligible"] is False


def test_prelaunch_build_archive_remains_validatable_incomplete(
    tmp_path: Path,
    profile: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    source = module._source_slot()

    def fail_after_build(attempt_directory: Path, _profile: Any) -> None:
        execution_root = module._live_execution_root(
            attempt_directory,
            create=True,
        )
        (execution_root / "runtime").mkdir()
        (execution_root / "source").mkdir()
        (execution_root / "exact-build").mkdir()
        (execution_root / "runtime/exact-build-provenance.json").write_bytes(
            module._canonical_json_bytes(BUILD_PROVENANCE, newline=True)
        )
        (execution_root / "source/manifest.json").write_bytes(
            source.manifest_bytes
        )
        (execution_root / "source/plan.json").write_bytes(source.plan_bytes)
        raise RuntimeError("identity generation failed after build preservation")

    attempt, verdict = module._run_attempt_once(
        profile=profile,
        preflight_receipt=module.preflight_from_documents(
            profile,
            current_revision=REVISION,
            build_provenance=BUILD_PROVENANCE,
            approval_receipt=APPROVAL_RECEIPT,
        ),
        approval_receipt=APPROVAL_RECEIPT,
        results_root=tmp_path / "results",
        pair_id=PAIR_ID,
        execute_attempt=fail_after_build,
        timestamp_utc="20260809T120000.000000Z",
        process_id=4101,
        attempt_uuid="11111111-1111-4111-8111-111111111111",
    )
    monkeypatch.setattr(
        module._factorial,
        "verify_preserved_build_evidence",
        lambda *_args, **_kwargs: None,
    )

    assert verdict == "INCOMPLETE"
    validated = module._validate_attempt(
        attempt,
        approval_receipt=APPROVAL_RECEIPT,
        expected_pair_id=PAIR_ID,
    )
    assert validated["verdict"] == "INCOMPLETE"


def test_run_pair_executes_second_attempt_after_first_is_incomplete(
    tmp_path: Path,
    profile: Any,
) -> None:
    module = _module()
    calls: list[Path] = []

    def execute(attempt_directory: Path, _profile: Any) -> None:
        calls.append(attempt_directory)
        if len(calls) == 1:
            raise RuntimeError("first attempt incomplete")
        raise RuntimeError("second attempt incomplete")

    attempts = module.run_pair(
        profile=profile,
        preflight_receipt=module.preflight_from_documents(
            profile,
            current_revision=REVISION,
            build_provenance=BUILD_PROVENANCE,
            approval_receipt=APPROVAL_RECEIPT,
        ),
        approval_receipt=APPROVAL_RECEIPT,
        results_root=tmp_path / "results",
        execute_attempt=execute,
        pair_uuid=PAIR_ID,
        attempt_identities=(
            (
                "20260809T120000.000000Z",
                4101,
                "11111111-1111-4111-8111-111111111111",
            ),
            (
                "20260809T120001.000000Z",
                4101,
                "22222222-2222-4222-8222-222222222222",
            ),
        ),
    )

    assert len(calls) == 2
    assert [verdict for _path, verdict in attempts] == [
        "INCOMPLETE",
        "INCOMPLETE",
    ]
    root = tmp_path / "results"
    pair_start = json.loads(
        (root / "pair-start.json").read_text(encoding="utf-8")
    )
    assert [row["attempt_directory"] for row in pair_start["intended_attempts"]] == [
        attempts[0][0].name,
        attempts[1][0].name,
    ]
    assert (root / "pair-ledger.json").is_file()
    assert (root / "evidence-seal.json").is_file()
    validated = module.validate_pair(
        root,
        approval_receipt=APPROVAL_RECEIPT,
    )
    assert validated["pair_complete"] is False
    assert len(validated["attempts"]) == 2


def test_run_pair_rejects_post_first_identity_substitution(
    tmp_path: Path,
    profile: Any,
) -> None:
    module = _module()
    calls: list[Path] = []
    root = tmp_path / "results"

    def substitute(attempt_directory: Path, _profile: Any) -> None:
        calls.append(attempt_directory)
        if len(calls) != 1:
            raise AssertionError("second attempt must not launch")
        path = root / "pair-start.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["intended_attempts"][1]["attempt_uuid"] = (
            "33333333-3333-4333-8333-333333333333"
        )
        document["intended_attempts"][1]["attempt_directory"] = (
            "attempt-02-20260809T120001.000000Z-pid4101-"
            "33333333-3333-4333-8333-333333333333"
        )
        path.write_text(
            json.dumps(
                document,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="pair start changed",
    ):
        module.run_pair(
            profile=profile,
            preflight_receipt=module.preflight_from_documents(
                profile,
                current_revision=REVISION,
                build_provenance=BUILD_PROVENANCE,
                approval_receipt=APPROVAL_RECEIPT,
            ),
            approval_receipt=APPROVAL_RECEIPT,
            results_root=root,
            execute_attempt=substitute,
            pair_uuid=PAIR_ID,
            attempt_identities=(
                (
                    "20260809T120000.000000Z",
                    4101,
                    "11111111-1111-4111-8111-111111111111",
                ),
                (
                    "20260809T120001.000000Z",
                    4101,
                    "22222222-2222-4222-8222-222222222222",
                ),
            ),
        )

    assert len(calls) == 1
    assert len(tuple(root.glob("attempt-*"))) == 1


def test_validate_pair_rejects_mixed_v1_v2_profile_bytes(
    tmp_path: Path,
    profile: Any,
) -> None:
    module = _module()
    root = tmp_path / "pair"

    def stop(*_args: object) -> None:
        raise RuntimeError("stop")

    attempts = module.run_pair(
        profile=profile,
        preflight_receipt=module.preflight_from_documents(
            profile,
            current_revision=REVISION,
            build_provenance=BUILD_PROVENANCE,
            approval_receipt=APPROVAL_RECEIPT,
        ),
        approval_receipt=APPROVAL_RECEIPT,
        results_root=root,
        execute_attempt=stop,
        pair_uuid=PAIR_ID,
        attempt_identities=(
            (
                "20260809T120000.000000Z",
                4101,
                "11111111-1111-4111-8111-111111111111",
            ),
            (
                "20260809T120001.000000Z",
                4101,
                "22222222-2222-4222-8222-222222222222",
            ),
        ),
    )
    second = attempts[1][0]
    (second / "profile.json").write_bytes(V2_PROFILE_PATH.read_bytes())
    (second / "evidence-seal.json").unlink()
    second_seal = module.create_evidence_seal(second)
    ledger_path = root / "pair-ledger.json"
    ledger = json.loads(ledger_path.read_bytes())
    ledger["attempts"][1]["evidence_seal_sha256"] = second_seal
    ledger_path.write_bytes(
        module._canonical_json_bytes(ledger, newline=True)
    )
    (root / "evidence-seal.json").unlink()
    module.create_evidence_seal(root)
    with pytest.raises(
        module.N31LivenessShakedownError,
        match="mixed liveness profile",
    ):
        module.validate_pair(root, approval_receipt=APPROVAL_RECEIPT)


@pytest.mark.parametrize(
    "mutation",
    (
        "cross-attempt",
        "empty-provenance",
        "arbitrary-argv",
        "mutated-config",
        "missing-build",
    ),
)
def test_validate_pair_rejects_internal_launch_provenance_mutation(
    tmp_path: Path,
    profile: Any,
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    root = tmp_path / "results"
    source = module._source_slot()

    def preserve_launch_identity(attempt_directory: Path, _profile: Any) -> None:
        ordinal = module._attempt_ordinal(attempt_directory)
        execution_root = module._live_execution_root(
            attempt_directory,
            create=True,
        )
        (execution_root / "runtime").mkdir()
        (execution_root / "source").mkdir()
        binary_root = execution_root / "exact-build/build-evidence/binaries"
        binary_root.mkdir(parents=True)
        for name in ("app", "manager", "keygen", "tls_keygen"):
            (binary_root / name).write_bytes(name.encode("ascii"))
        (execution_root / "runtime/exact-build-provenance.json").write_bytes(
            module._canonical_json_bytes(BUILD_PROVENANCE, newline=True)
        )
        (execution_root / "source/manifest.json").write_bytes(
            source.manifest_bytes
        )
        (execution_root / "source/plan.json").write_bytes(source.plan_bytes)
        identities = module._factorial.IdentityMaterial(
            bls=tuple(
                {
                    "pub": f"{replica_id + 1:064x}",
                    "sec": f"{replica_id + 101:064x}",
                }
                for replica_id in range(31)
            ),
            tls=tuple(
                {
                    "crt": f"{replica_id + 201:064x}",
                    "sec": f"{replica_id + 301:064x}",
                    "cid": f"{replica_id + 401:064x}",
                }
                for replica_id in range(32)
            ),
            issuer={"pub": f"{501:064x}", "sec": f"{502:064x}"},
        )
        for label, rows in (
            ("bls", identities.bls),
            ("tls", identities.tls),
            ("issuer", (identities.issuer,)),
        ):
            (execution_root / f"runtime/{label}-identities.txt").write_text(
                "".join(
                    " ".join(f"{key}:{value}" for key, value in row.items())
                    + "\n"
                    for row in rows
                ),
                encoding="utf-8",
            )
        inputs = module.write_slot_configs(
            source.slot,
            source.runtime,
            slot_directory=execution_root,
            identities=identities,
        )
        binaries = module.ExecutionBinaries(
            **{
                name: execution_root
                / "exact-build/build-evidence/binaries"
                / name
                for name in ("app", "manager", "keygen", "tls_keygen")
            }
        )
        anchor_ns = 1_000 + ordinal
        materialized = module.materialize_launch(
            source.slot,
            source.runtime,
            slot_directory=execution_root,
            binaries=binaries,
            identities=identities,
            input_artifacts=inputs,
            shared_raw_clock_anchor_ns=anchor_ns,
            redaction_key=module._factorial._derive_redaction_key(
                module._canonical_json_bytes(APPROVAL_RECEIPT),
                f"{SOURCE_SLOT_ID}:attempt-{ordinal}",
            ),
        )
        launch = module._launch_document(
            source,
            materialized,
            profile_id=profile.profile_id,
            pair_id=PAIR_ID,
            attempt_ordinal=ordinal,
            anchor_ns=anchor_ns,
        )
        launch_payload = module._canonical_json_bytes(launch, newline=True)
        (execution_root / "launch.json").write_bytes(launch_payload)
        (execution_root / "runtime/execution-provenance.json").write_bytes(
            module._canonical_json_bytes(
                {
                    "schema_version": 1,
                    "profile_id": profile.profile_id,
                    "source_slot_id": SOURCE_SLOT_ID,
                    "pair_id": PAIR_ID,
                    "attempt_ordinal": ordinal,
                    "revision": REVISION,
                    "build_provenance_sha256": BUILD_PROVENANCE_SHA256,
                    "approval_receipt_sha256": APPROVAL_RECEIPT_SHA256,
                    "binaries": module._binary_identities(binaries),
                    "input_artifacts": list(inputs),
                    "launch_sha256": module._sha256_bytes(launch_payload),
                    "launch_count": 1,
                    "retry_count": 0,
                    "replacement_count": 0,
                    "claim_eligible": False,
                    "campaign_member": False,
                    "denominator_contribution": 0,
                    "figure_eligible": False,
                },
                newline=True,
            )
        )
        raise RuntimeError("preserved incomplete attempt")

    monkeypatch.setattr(
        module._factorial,
        "verify_preserved_build_evidence",
        lambda *_args, **_kwargs: None,
    )

    attempts = module.run_pair(
        profile=profile,
        preflight_receipt=module.preflight_from_documents(
            profile,
            current_revision=REVISION,
            build_provenance=BUILD_PROVENANCE,
            approval_receipt=APPROVAL_RECEIPT,
        ),
        approval_receipt=APPROVAL_RECEIPT,
        results_root=root,
        execute_attempt=preserve_launch_identity,
        pair_uuid=PAIR_ID,
        attempt_identities=(
            (
                "20260809T120000.000000Z",
                4101,
                "11111111-1111-4111-8111-111111111111",
            ),
            (
                "20260809T120001.000000Z",
                4101,
                "22222222-2222-4222-8222-222222222222",
            ),
        ),
    )
    first, second = (row[0] for row in attempts)
    first_execution = module._live_execution_root(first)
    second_execution = module._live_execution_root(second)
    second_provenance = second_execution / "runtime/execution-provenance.json"
    baseline = module.validate_pair(root, approval_receipt=APPROVAL_RECEIPT)
    assert len(baseline["attempts"]) == 2
    if mutation == "cross-attempt":
        (second_execution / "launch.json").write_bytes(
            (first_execution / "launch.json").read_bytes()
        )
        second_provenance.write_bytes(
            (first_execution / "runtime/execution-provenance.json").read_bytes()
        )
    elif mutation == "missing-build":
        (second_execution / "runtime/exact-build-provenance.json").unlink()
    elif mutation != "mutated-config":
        launch_path = second_execution / "launch.json"
        launch = json.loads(launch_path.read_text(encoding="utf-8"))
        if mutation == "empty-provenance":
            launch["manager_argv"] = []
            launch["replica_argv"] = []
        else:
            launch["manager_argv"] = ["evil-manager"]
            launch["replica_argv"] = [
                {"replica_id": replica_id, "argv": ["evil-replica"]}
                for replica_id in range(31)
            ]
        launch_payload = module._canonical_json_bytes(launch, newline=True)
        launch_path.write_bytes(launch_payload)
        provenance = json.loads(second_provenance.read_text(encoding="utf-8"))
        if mutation == "empty-provenance":
            provenance["binaries"] = {}
            provenance["input_artifacts"] = []
        provenance["launch_sha256"] = module._sha256_bytes(launch_payload)
        second_provenance.write_bytes(
            module._canonical_json_bytes(provenance, newline=True)
        )
    else:
        config_path = second_execution / "runtime/replica-0.conf"
        config_path.write_bytes(config_path.read_bytes() + b"injected = true\n")
        provenance = json.loads(second_provenance.read_text(encoding="utf-8"))
        config_row = next(
            row
            for row in provenance["input_artifacts"]
            if row["relative_path"] == "runtime/replica-0.conf"
        )
        config_payload = config_path.read_bytes()
        config_row["sha256"] = module._sha256_bytes(config_payload)
        config_row["size_bytes"] = len(config_payload)
        second_provenance.write_bytes(
            module._canonical_json_bytes(provenance, newline=True)
        )
    (second / "evidence-seal.json").unlink()
    second_seal = module.create_evidence_seal(second)
    ledger_path = root / "pair-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["attempts"][1]["evidence_seal_sha256"] = second_seal
    ledger_path.write_bytes(module._canonical_json_bytes(ledger, newline=True))
    (root / "evidence-seal.json").unlink()
    module.create_evidence_seal(root)

    with pytest.raises(
        module.N31LivenessShakedownError,
        match=(
            "launch.*identity|execution provenance.*identity|"
            "launch/execution provenance.*build"
        ),
    ):
        module.validate_pair(root, approval_receipt=APPROVAL_RECEIPT)


def test_common_epoch2_commit_accepts_any_exact_q21_witness_set() -> None:
    module = _module()
    spec = SimpleNamespace(
        replica_count=31,
        q=21,
        structured_events=SimpleNamespace(commit_observer_id="replica-0"),
    )
    streams: dict[str, list[SimpleNamespace]] = {
        f"replica-{replica_id}": [] for replica_id in range(31)
    }
    streams["replica-0"].append(
        _native_event(
            source="replica-0",
            event_type="block.committed",
            timestamp_ns=100,
            payload=_commit_payload(authoritative=True),
        )
    )
    for replica_id in range(10, 31):
        streams[f"replica-{replica_id}"].append(
            _native_event(
                source=f"replica-{replica_id}",
                event_type="block.commit_observed",
                timestamp_ns=100 + replica_id,
                payload=_commit_payload(authoritative=False),
            )
        )

    commits = module._common_epoch2_commits(spec, streams)

    assert len(commits) == 1
    assert commits[0]["witness_replica_ids"] == list(range(10, 31))
    assert commits[0]["common_monotonic_ns"] == 130


def test_common_epoch2_commit_rejects_conflicting_hashes_at_one_height() -> None:
    module = _module()
    spec = SimpleNamespace(
        replica_count=31,
        q=21,
        structured_events=SimpleNamespace(commit_observer_id="replica-0"),
    )
    streams: dict[str, list[SimpleNamespace]] = {
        f"replica-{replica_id}": [] for replica_id in range(31)
    }
    for offset, block_hash in enumerate(("a" * 64, "d" * 64)):
        authoritative = _commit_payload(authoritative=True)
        authoritative["block_hash"] = block_hash
        authoritative["decision_proof"]["block_hash"] = block_hash
        streams["replica-0"].append(
            _native_event(
                source="replica-0",
                event_type="block.committed",
                timestamp_ns=100 + offset,
                payload=authoritative,
                line_number=offset + 1,
                line_sha256=str(offset + 1) * 64,
            )
        )
        for replica_id in range(10, 31):
            observed = _commit_payload(authoritative=False)
            observed["block_hash"] = block_hash
            streams[f"replica-{replica_id}"].append(
                _native_event(
                    source=f"replica-{replica_id}",
                    event_type="block.commit_observed",
                    timestamp_ns=110 + offset + replica_id,
                    payload=observed,
                    line_number=offset + 1,
                    line_sha256=str(offset + 3) * 64,
                )
            )

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="conflicting.*height",
    ):
        module._common_epoch2_commits(spec, streams)


def test_common_epoch2_commit_rejects_per_replica_height_conflict() -> None:
    module = _module()
    spec = SimpleNamespace(
        replica_count=31,
        q=21,
        structured_events=SimpleNamespace(commit_observer_id="replica-0"),
    )
    streams: dict[str, list[SimpleNamespace]] = {
        f"replica-{replica_id}": [] for replica_id in range(31)
    }
    for line_number, block_hash in enumerate(("a" * 64, "d" * 64), 1):
        payload = _commit_payload(authoritative=False)
        payload["block_hash"] = block_hash
        streams["replica-7"].append(
            _native_event(
                source="replica-7",
                event_type="block.commit_observed",
                timestamp_ns=100 + line_number,
                payload=payload,
                line_number=line_number,
                line_sha256=str(line_number) * 64,
            )
        )

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="replica-7.*height",
    ):
        module._common_epoch2_commits(spec, streams)


def test_common_epoch2_commit_rejects_observed_only_parent_conflict() -> None:
    module = _module()
    spec = SimpleNamespace(
        replica_count=31,
        q=21,
        structured_events=SimpleNamespace(commit_observer_id="replica-0"),
    )
    streams: dict[str, list[SimpleNamespace]] = {
        f"replica-{replica_id}": [] for replica_id in range(31)
    }
    for line_number, (height, block_hash, parent_hash) in enumerate(
        (
            (81, "a" * 64, "b" * 64),
            (82, "d" * 64, "e" * 64),
        ),
        1,
    ):
        payload = _commit_payload(authoritative=False)
        payload.update(
            block_height=height,
            block_hash=block_hash,
            parent_hash=parent_hash,
        )
        streams["replica-7"].append(
            _native_event(
                source="replica-7",
                event_type="block.commit_observed",
                timestamp_ns=100 + line_number,
                payload=payload,
                line_number=line_number,
                line_sha256=str(line_number) * 64,
            )
        )

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="conflicting parent",
    ):
        module._common_epoch2_commits(spec, streams)


def test_common_epoch2_commit_rejects_observed_only_replica_disagreement() -> None:
    module = _module()
    spec = SimpleNamespace(
        replica_count=31,
        q=21,
        structured_events=SimpleNamespace(commit_observer_id="replica-0"),
    )
    streams: dict[str, list[SimpleNamespace]] = {
        f"replica-{replica_id}": [] for replica_id in range(31)
    }
    for replica_id, block_hash in ((7, "a" * 64), (8, "d" * 64)):
        payload = _commit_payload(authoritative=False)
        payload["block_hash"] = block_hash
        streams[f"replica-{replica_id}"].append(
            _native_event(
                source=f"replica-{replica_id}",
                event_type="block.commit_observed",
                timestamp_ns=100 + replica_id,
                payload=payload,
                line_sha256=str(replica_id) * 64,
            )
        )

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="replicas.*conflict.*height",
    ):
        module._common_epoch2_commits(spec, streams)


def test_common_epoch2_commit_rejects_shared_height_witness_conflict() -> None:
    module = _module()
    spec = SimpleNamespace(
        replica_count=31,
        q=21,
        structured_events=SimpleNamespace(commit_observer_id="replica-0"),
    )
    streams: dict[str, list[SimpleNamespace]] = {
        f"replica-{replica_id}": [] for replica_id in range(31)
    }
    streams["replica-0"].append(
        _native_event(
            source="replica-0",
            event_type="block.committed",
            timestamp_ns=100,
            payload=_commit_payload(authoritative=True),
        )
    )
    for replica_id in range(10, 31):
        streams[f"replica-{replica_id}"].append(
            _native_event(
                source=f"replica-{replica_id}",
                event_type="block.commit_observed",
                timestamp_ns=100 + replica_id,
                payload=_commit_payload(authoritative=False),
            )
        )
    conflicting = _commit_payload(authoritative=False)
    conflicting["block_hash"] = "d" * 64
    streams["replica-7"].append(
        _native_event(
            source="replica-7",
            event_type="block.commit_observed",
            timestamp_ns=105,
            payload=conflicting,
        )
    )

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="replicas.*conflict.*height|replica-7.*authoritative.*height",
    ):
        module._common_epoch2_commits(spec, streams)


def test_common_epoch2_commit_rejects_conflicting_consecutive_parent() -> None:
    module = _module()
    spec = SimpleNamespace(
        replica_count=31,
        q=21,
        structured_events=SimpleNamespace(commit_observer_id="replica-0"),
    )
    streams: dict[str, list[SimpleNamespace]] = {
        f"replica-{replica_id}": [] for replica_id in range(31)
    }
    identities = (
        (81, "a" * 64, "b" * 64),
        (82, "d" * 64, "e" * 64),
    )
    for line_number, (height, block_hash, parent_hash) in enumerate(
        identities, 1
    ):
        authoritative = _commit_payload(authoritative=True)
        authoritative.update(
            block_height=height,
            block_hash=block_hash,
            parent_hash=parent_hash,
        )
        authoritative["decision_proof"]["block_hash"] = block_hash
        streams["replica-0"].append(
            _native_event(
                source="replica-0",
                event_type="block.committed",
                timestamp_ns=100 + line_number,
                payload=authoritative,
                line_number=line_number,
                line_sha256=str(line_number) * 64,
            )
        )
        for replica_id in range(10, 31):
            observed = _commit_payload(authoritative=False)
            observed.update(
                block_height=height,
                block_hash=block_hash,
                parent_hash=parent_hash,
            )
            streams[f"replica-{replica_id}"].append(
                _native_event(
                    source=f"replica-{replica_id}",
                    event_type="block.commit_observed",
                    timestamp_ns=110 + line_number + replica_id,
                    payload=observed,
                    line_number=line_number,
                    line_sha256=str(line_number + 2) * 64,
                )
            )

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="conflicting parent",
    ):
        module._common_epoch2_commits(spec, streams)


def test_epoch2_roots_come_from_signed_successor_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: Any,
) -> None:
    module = _module()
    bundle_path = "transitions/epoch2/successor.bundle"
    (tmp_path / bundle_path).parent.mkdir(parents=True)
    (tmp_path / bundle_path).write_bytes(b"signed bundle")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    runtime.joinpath("issuer-identities.txt").write_text(
        f"pub:{'02' + '1' * 64} sec:{'2' * 64}\n",
        encoding="ascii",
    )
    trees = []
    for tree_id, root in enumerate(FAST_ROOTS):
        members = (root, *(value for value in range(31) if value != root))
        trees.append(
            SimpleNamespace(
                tree_id=tree_id,
                fanout=5,
                pipeline_stretch=2,
                members=members,
            )
        )
    decoded = SimpleNamespace(
        epoch_number=2,
        epoch_digest="1" * 64,
        trees=tuple(trees),
    )
    calls: list[tuple[bytes, str]] = []

    def decode(payload: bytes, *, issuer_public_key: str) -> object:
        calls.append((payload, issuer_public_key))
        return decoded

    monkeypatch.setattr(module, "decode_epoch_change_bundle", decode)
    spec = SimpleNamespace(
        replica_count=31,
        q=21,
        transitions=(
            SimpleNamespace(bundle_relative_path="transitions/epoch1/successor.bundle"),
            SimpleNamespace(bundle_relative_path=bundle_path),
        ),
    )

    roots, digest, initial_tree_id = module._epoch2_bundle_identity(
        spec,
        tmp_path,
        profile,
    )

    assert roots == FAST_ROOTS
    assert digest == "1" * 64
    assert initial_tree_id == 0
    assert calls == [(b"signed bundle", "02" + "1" * 64)]


def test_epoch2_root_aggregation_digest_is_bound_to_activation() -> None:
    module = _module()
    streams = {
        "adaptive-manager": [],
        "replica-11": [
            _native_event(
                source="replica-11",
                event_type="aggregation.root_quorum_progress",
                timestamp_ns=100,
                payload={
                    "epoch_number": 2,
                    "epoch_digest": "2" * 64,
                },
            )
        ],
    }

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="root aggregation.*activated digest",
    ):
        module._validate_epoch2_root_aggregation_digests(
            streams,
            "1" * 64,
        )


def test_epoch2_activation_rejects_tree_absent_from_signed_initial_state() -> None:
    module = _module()
    streams = {
        f"replica-{replica_id}": [
            _native_event(
                source=f"replica-{replica_id}",
                event_type="epoch.activated",
                timestamp_ns=100 + replica_id,
                payload={
                    "epoch_number": 2,
                    "tree_id": 999,
                    "epoch_digest": "1" * 64,
                    "activation_height": 80,
                },
            )
        ]
        for replica_id in range(31)
    }
    spec = SimpleNamespace(replica_count=31)

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="activation tree.*signed successor bundle",
    ):
        module._epoch2_activation_progress(
            spec,
            streams,
            expected_initial_tree_id=0,
        )


def test_timeout_offsets_are_captured_before_first_common_epoch2_search() -> None:
    source = inspect.getsource(_module()._observe_liveness_window)
    assert source.index("_leader_timeout_offsets") < source.index(
        "while first is None"
    )


def test_timeout_end_offsets_freeze_before_post_window_event_parsing() -> None:
    source = inspect.getsource(_module()._observe_liveness_window)
    deadline_reached = source.index("end_ns = raw_now_ns()")
    end_offsets = source.index(
        "end_offsets = _leader_timeout_offsets", deadline_reached
    )
    post_window_read = source.index(
        "streams = read_event_streams", deadline_reached
    )
    assert deadline_reached < end_offsets < post_window_read


def test_timeout_replay_recounts_only_the_sealed_capture_range(
    tmp_path: Path,
) -> None:
    module = _module()
    stdout = "raw/process/replica-0.stdout.log"
    stderr = "raw/process/replica-0.stderr.log"
    for relative in (stdout, stderr):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    spec = SimpleNamespace(
        replica_count=1,
        process_logs=SimpleNamespace(
            replica_stdout_relative_paths=(stdout,),
            replica_stderr_relative_paths=(stderr,),
        ),
    )
    starts = module._leader_timeout_offsets(spec, tmp_path)
    first = (
        b"2026-08-09 14:10:25.595497 [hotstuff info] "
        b"[EPOCH] Rotated active epoch=2 to tree=7 after leader timeout\n"
    )
    (tmp_path / stderr).write_bytes(first)
    ends = module._leader_timeout_offsets(spec, tmp_path)
    with (tmp_path / stderr).open("ab") as output:
        output.write(
            b"2026-08-09 14:11:10.512129 [hotstuff info] "
            b"[EPOCH] Rotated active epoch=2 to tree=13 after leader timeout\n"
        )

    rows = module._read_leader_timeouts(
        spec,
        tmp_path,
        starts,
        end_offsets=ends,
    )

    assert len(rows) == 1
    assert rows[0]["next_tree_id"] == 7


def test_hol_detection_and_cleanup_sample_form_the_reproduction_chain(
    profile: Any,
) -> None:
    module = _module()
    root = FAST_ROOTS[0]
    common_payload = {
        "view_generation": 8,
    }
    streams: dict[str, list[SimpleNamespace]] = {
        f"replica-{replica_id}": [] for replica_id in range(31)
    }
    base = {
        "epoch_number": 2,
        "tree_id": 0,
        "epoch_digest": "1" * 64,
        "context_generation": 1,
        "observer_replica": root,
        "global_quorum": 21,
    }
    streams[f"replica-{root}"] = [
        _native_event(
            source=f"replica-{root}",
            event_type="aggregation.root_quorum_progress",
            timestamp_ns=2_000_000_000,
            payload={
                **base,
                "block_hash": "2" * 64,
                "root_signer_count": 16,
            },
            line_number=7,
            line_sha256="3" * 64,
        ),
        _native_event(
            source=f"replica-{root}",
            event_type="aggregation.root_quorum_progress",
            timestamp_ns=3_000_000_000,
            payload={
                **base,
                "context_generation": 2,
                "block_hash": "4" * 64,
                "root_signer_count": 25,
            },
            line_number=8,
            line_sha256="5" * 64,
        ),
    ]
    timeouts = [
        {"replica_id": root, "line_sha256": "6" * 64},
        {"replica_id": 14, "line_sha256": "7" * 64},
    ]
    stalls = module._detect_head_of_line_stalls(
        profile,
        streams,
        start_ns=1_000_000_000,
        end_ns=46_000_000_000,
        selected_roots=FAST_ROOTS,
        activated_epoch_digest="1" * 64,
        leader_timeouts=timeouts,
        common_commits=[
            {**common_payload, "view_generation": 7},
            {**common_payload, "view_generation": 8},
        ],
        max_commit_gap_ns=21_200_000_000,
    )
    assert stalls == []


def test_hol_detection_uses_pre_anchor_epoch2_head_history(
    tmp_path: Path,
    profile: Any,
) -> None:
    module = _module()
    root = FAST_ROOTS[0]
    head_hash = "2" * 64
    later_hash = "4" * 64
    streams: dict[str, list[SimpleNamespace]] = {
        f"replica-{replica_id}": [] for replica_id in range(31)
    }
    base = {
        "epoch_number": 2,
        "tree_id": 0,
        "epoch_digest": "1" * 64,
        "observer_replica": root,
        "global_quorum": 21,
    }
    streams[f"replica-{root}"] = [
        _native_event(
            source=f"replica-{root}",
            event_type="aggregation.root_quorum_progress",
            timestamp_ns=900_000_000,
            payload={
                **base,
                "context_generation": 1,
                "block_hash": head_hash,
                "root_signer_count": 16,
            },
            line_number=7,
            line_sha256="3" * 64,
        ),
        _native_event(
            source=f"replica-{root}",
            event_type="aggregation.root_quorum_progress",
            timestamp_ns=3_000_000_000,
            payload={
                **base,
                "context_generation": 2,
                "block_hash": later_hash,
                "root_signer_count": 25,
            },
            line_number=8,
            line_sha256="5" * 64,
        ),
        _native_event(
            source=f"replica-{root}",
            event_type="pipeline.root_qc_queue_blocked",
            timestamp_ns=3_100_000_000,
            payload={
                **base,
                "queue_head_position": 0,
                "queued_candidate_position": 1,
                "queue_head_context_generation": 1,
                "queued_candidate_context_generation": 2,
                "queue_head_block_height": 90,
                "queue_head_block_hash": head_hash,
                "queued_candidate_block_height": 91,
                "queued_candidate_block_hash": later_hash,
                "queued_candidate_parent_hash": head_hash,
                "queue_head_signer_count": 16,
                "queued_candidate_signer_count": 25,
                "queued_candidate_qc_ready": True,
                "queued_candidate_qc_published": False,
            },
            line_number=9,
            line_sha256="8" * 64,
        ),
    ]

    stalls = module._detect_head_of_line_stalls(
        profile,
        streams,
        start_ns=1_000_000_000,
        end_ns=46_000_000_000,
        selected_roots=FAST_ROOTS,
        activated_epoch_digest="1" * 64,
        leader_timeouts=[
            {"replica_id": root, "line_sha256": "6" * 64},
            {"replica_id": 14, "line_sha256": "7" * 64},
        ],
        common_commits=[
            {"view_generation": 7},
            {"view_generation": 8},
        ],
        max_commit_gap_ns=21_200_000_000,
    )

    assert len(stalls) == 1
    assert stalls[0]["queue_evidence_sha256"] == "8" * 64
    assert stalls[0]["head_block_hash"] == head_hash
    assert stalls[0]["later_parent_hash"] == head_hash

    sample = tmp_path / "raw/diagnostics" / f"replica-{root}.sample.txt"
    sample.parent.mkdir(parents=True)
    sample.write_text(
        "Process: app [9011]\nCall graph:\n  1 blocked_frame\n",
        encoding="utf-8",
    )
    cleanup_rows = _complete_sigint_cleanup_rows()
    cleanup_rows[root] = {
        "name": f"replica-{root}",
        "replica_id": root,
        "pid": 9_011,
        "pgid": 9_011,
        "signal_number": 9,
        "returncode": -9,
        "classification": "unexpected_cleanup_escalation",
        "exit_authorization": None,
    }
    merged = module._merge_cleanup_observation(
        profile,
        tmp_path,
        {
            "liveness": {
                "head_of_line_stalls": stalls,
                "leader_progress_timeout_count": 2,
                "q21_progress_continued": True,
                "max_authoritative_commit_gap_ns": 21_200_000_000,
                "max_common_q21_commit_gap_ns": 1_000_000_000,
            },
            "consensus_conflict_count": 0,
        },
        cleanup_rows,
        [
            {
                "name": f"replica-{root}",
                "replica_id": root,
                "pid": 9_011,
                "pgid": 9_011,
                "after_signal_number": 2,
                "before_signal_number": 15,
                "status": "captured",
                "artifact_relative_path": (
                    f"raw/diagnostics/replica-{root}.sample.txt"
                ),
                "error": None,
            }
        ],
        cleanup_complete=True,
        streams_closed=True,
        ports_clear=True,
        final_events_complete=True,
    )
    enriched = merged["liveness"]["head_of_line_stalls"][0]
    assert enriched["survived_sigint"] is True
    assert enriched["survived_sigterm"] is True
    assert enriched["pre_kill_stack_valid"] is True
    assert merged["execution"]["unexpected_process_exit_count"] == 0

    streams[f"replica-{root}"][2].value["payload"]["epoch_digest"] = "f" * 64
    with pytest.raises(
        module.N31LivenessShakedownError,
        match="activated Epoch2",
    ):
        module._detect_head_of_line_stalls(
            profile,
            streams,
            start_ns=1_000_000_000,
            end_ns=46_000_000_000,
            selected_roots=FAST_ROOTS,
            activated_epoch_digest="1" * 64,
            leader_timeouts=[
                {"replica_id": root, "line_sha256": "6" * 64},
                {"replica_id": 14, "line_sha256": "7" * 64},
            ],
            common_commits=[
                {"view_generation": 7},
                {"view_generation": 8},
            ],
            max_commit_gap_ns=21_200_000_000,
        )


def test_cleanup_replay_binds_sample_to_exact_replica_and_pid(
    tmp_path: Path,
) -> None:
    module = _module()
    sample = tmp_path / "raw/diagnostics/replica-11.sample.txt"
    sample.parent.mkdir(parents=True)
    sample.write_text(
        "Process: app [9100]\nCall graph:\n  1 blocked_frame\n",
        encoding="utf-8",
    )
    cleanup_rows = _complete_sigint_cleanup_rows()
    cleanup_rows[11] = {
        "name": "replica-11",
        "replica_id": 11,
        "pid": 9_100,
        "pgid": 9_100,
        "signal_number": 9,
        "returncode": -9,
        "classification": "unexpected_cleanup_escalation",
        "exit_authorization": None,
    }
    sample_rows = [
        {
            "name": "replica-11",
            "replica_id": 11,
            "pid": 9_101,
            "pgid": 9_101,
            "after_signal_number": 2,
            "before_signal_number": 15,
            "status": "captured",
            "artifact_relative_path": "raw/diagnostics/replica-11.sample.txt",
            "error": None,
        }
    ]

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="sample.*identity",
    ):
        module._recompute_cleanup_facts(
            tmp_path,
            cleanup_rows,
            sample_rows,
            diagnostic_replica_ids=frozenset({11}),
        )


def test_cleanup_replay_does_not_trust_classification_or_sigint_summary() -> None:
    module = _module()
    cleanup_rows = _complete_sigint_cleanup_rows()
    cleanup_rows[11] = {
        "name": "replica-11",
        "replica_id": 11,
        "pid": 9_100,
        "pgid": 9_100,
        "signal_number": 15,
        "returncode": -15,
        "classification": "expected_cleanup",
        "exit_authorization": None,
    }

    facts = module._recompute_cleanup_facts(
        Path("."),
        cleanup_rows,
        (),
        diagnostic_replica_ids=frozenset(),
    )

    assert facts["unexpected_process_exit_count"] == 1
    assert facts["all_processes_exited_after_sigint"] is False


def test_cleanup_lifecycle_cannot_start_before_observation_ends() -> None:
    module = _module()

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="cleanup started before the observation ended",
    ):
        module._validate_cleanup_lifecycle_start(
            cleanup_started_monotonic_ns=OBSERVATION_DEADLINE_NS - 1,
            observation_end_ns=OBSERVATION_DEADLINE_NS,
        )


def test_cleanup_replay_rejects_null_replica_identity() -> None:
    module = _module()
    rows = _complete_sigint_cleanup_rows()
    rows[11] = {**rows[11], "replica_id": None}

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="cleanup process identity",
    ):
        module._recompute_cleanup_facts(
            Path("."),
            rows,
            (),
            diagnostic_replica_ids=frozenset(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("pgid", 20_000.0),
        ("signal_number", 2.0),
        ("returncode", -2.0),
    ),
)
def test_cleanup_replay_rejects_noninteger_ledger_numbers(
    field: str,
    value: object,
) -> None:
    module = _module()
    rows = _complete_sigint_cleanup_rows()
    rows[0] = {**rows[0], field: value}

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="cleanup process identity",
    ):
        module._recompute_cleanup_facts(
            Path("."),
            rows,
            (),
            diagnostic_replica_ids=frozenset(),
        )


def test_cleanup_replay_accepts_authorized_manager_clean_exit_with_sigint_replicas(
    tmp_path: Path,
) -> None:
    module = _module()
    authorization = {
        "relative_path": "raw/adaptive-manager.jsonl",
        "line_number": 7,
        "source_id": "adaptive-manager",
        "source_sequence": 7,
        "source_monotonic_ns": 4_000,
        "event_type": "adaptive_v2_session_terminal",
        "line_sha256": "1" * 64,
    }
    rows = _complete_sigint_cleanup_rows()
    rows[-1] = {
        "name": "adaptive-manager",
        "replica_id": None,
        "pid": 9_101,
        "pgid": 9_101,
        "signal_number": None,
        "returncode": 0,
        "classification": "expected_clean_exit",
        "exit_authorization": authorization,
    }

    facts = module._recompute_cleanup_facts(
        tmp_path,
        rows,
        (),
        diagnostic_replica_ids=frozenset(),
        expected_clean_exits={"adaptive-manager": authorization},
    )

    assert facts["unexpected_process_exit_count"] == 0
    assert facts["all_processes_exited_after_sigint"] is True


def test_cleanup_replay_rejects_noninteger_sample_numbers(tmp_path: Path) -> None:
    module = _module()
    sample = tmp_path / "raw/diagnostics/replica-11.sample.txt"
    sample.parent.mkdir(parents=True)
    sample.write_text(
        "Process: app [20011]\nCall graph:\n  1 blocked_frame\n",
        encoding="utf-8",
    )
    cleanup_rows = _complete_sigint_cleanup_rows()
    cleanup_rows[11] = {
        **cleanup_rows[11],
        "signal_number": 9,
        "returncode": -9,
        "classification": "unexpected_cleanup_escalation",
    }
    sample_row = {
        "name": "replica-11",
        "replica_id": 11,
        "pid": 20_011,
        "pgid": 20_011.0,
        "after_signal_number": 2,
        "before_signal_number": 15,
        "status": "captured",
        "artifact_relative_path": "raw/diagnostics/replica-11.sample.txt",
        "error": None,
    }

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="cleanup sample identity",
    ):
        module._recompute_cleanup_facts(
            tmp_path,
            cleanup_rows,
            [sample_row],
            diagnostic_replica_ids=frozenset({11}),
        )


def test_clean_exit_authorization_rejects_mixed_duplicate_cycle_terminals() -> None:
    module = _module()
    successful = _native_event(
        source="adaptive-manager",
        event_type="adaptive_v2_session_terminal",
        timestamp_ns=4_000,
        payload={
            "cycle_ordinal": 1,
            "outcome": "advanced",
            "reason": "successor_converged",
        },
        line_number=1,
    )
    successful.reference = lambda: {"line_number": 1}
    streams = {
        "adaptive-manager": [
            successful,
            _native_event(
                source="adaptive-manager",
                event_type="adaptive_v2_session_terminal",
                timestamp_ns=4_001,
                payload={
                    "cycle_ordinal": 1,
                    "outcome": "aborted",
                    "reason": "manager_error",
                },
                line_number=2,
            ),
        ]
    }

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="duplicated.*terminal",
    ):
        module._expected_clean_exit_authorizations(streams)


def test_cleanup_sample_bytes_must_bind_claimed_pid(tmp_path: Path) -> None:
    module = _module()
    sample = tmp_path / "raw/diagnostics/replica-11.sample.txt"
    sample.parent.mkdir(parents=True)
    sample.write_text(
        "Process: app [9999]\nCall graph:\n  1 unrelated_frame\n",
        encoding="utf-8",
    )
    cleanup = {
        "name": "replica-11",
        "replica_id": 11,
        "pid": 9_100,
        "pgid": 9_100,
        "signal_number": 9,
        "returncode": -9,
        "classification": "unexpected_cleanup_escalation",
        "exit_authorization": None,
    }
    sample_row = {
        "name": "replica-11",
        "replica_id": 11,
        "pid": 9_100,
        "pgid": 9_100,
        "after_signal_number": 2,
        "before_signal_number": 15,
        "status": "captured",
        "artifact_relative_path": "raw/diagnostics/replica-11.sample.txt",
        "error": None,
    }

    with pytest.raises(module.N31LivenessShakedownError, match="claimed PID"):
        cleanup_rows = _complete_sigint_cleanup_rows()
        cleanup_rows[11] = cleanup
        module._recompute_cleanup_facts(
            tmp_path,
            cleanup_rows,
            [sample_row],
            diagnostic_replica_ids=frozenset({11}),
        )


def test_cleanup_replay_rejects_sample_for_claimed_sigint_exit(
    tmp_path: Path,
) -> None:
    module = _module()
    sample = tmp_path / "raw/diagnostics/replica-11.sample.txt"
    sample.parent.mkdir(parents=True)
    sample.write_text(
        "Process: app [20011]\nCall graph:\n  1 blocked_frame\n",
        encoding="utf-8",
    )
    sample_row = {
        "name": "replica-11",
        "replica_id": 11,
        "pid": 20_011,
        "pgid": 20_011,
        "after_signal_number": 2,
        "before_signal_number": 15,
        "status": "captured",
        "artifact_relative_path": "raw/diagnostics/replica-11.sample.txt",
        "error": None,
    }

    with pytest.raises(
        module.N31LivenessShakedownError,
        match="sample.*SIGINT exit",
    ):
        module._recompute_cleanup_facts(
            tmp_path,
            _complete_sigint_cleanup_rows(),
            [sample_row],
            diagnostic_replica_ids=frozenset(),
        )


def test_complete_validator_recomputes_every_decisive_summary() -> None:
    source = inspect.getsource(_module()._validate_complete_live_capture)
    for helper in (
        "_epoch2_bundle_identity",
        "_epoch2_activation_progress",
        "_common_epoch2_commits",
        "_authoritative_epoch2_commits",
        "_read_timeout_capture",
        "_read_leader_timeouts",
        "_detect_head_of_line_stalls",
        "_merge_cleanup_observation",
    ):
        assert helper in source
    assert "allowed_hashes" not in source


@pytest.mark.parametrize("verdict", ("REPRODUCED", "NOT_REPRODUCED"))
def test_cli_run_emits_only_non_claim_complete_verdicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verdict: str,
) -> None:
    module = _module()
    cli = _cli()
    attempts = (tmp_path / "attempt-1", tmp_path / "attempt-2")
    for attempt in attempts:
        attempt.mkdir()
    monkeypatch.setattr(
        module,
        "run_pair",
        lambda **_kwargs: tuple((attempt, verdict) for attempt in attempts),
    )
    monkeypatch.setattr(
        module,
        "preflight",
        lambda **_kwargs: {
            "status": "READY",
            "campaign_member": False,
            "denominator_contribution": 0,
            "figure_eligible": False,
        },
    )

    assert cli.main(
        [
            "run",
            "--profile",
            str(PROFILE_PATH),
            "--repository",
            str(REPOSITORY),
            "--results-root",
            str(tmp_path / "results"),
            "--approval-receipt",
            str(tmp_path / "approval.json"),
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "attempts": [
            {"attempt_directory": str(attempt), "verdict": verdict}
            for attempt in attempts
        ],
        "campaign_member": False,
        "denominator_contribution": 0,
        "figure_eligible": False,
        "pair_complete": True,
        "run_directory": str((tmp_path / "results").resolve()),
    }


def test_cli_exposes_only_preflight_run_validate_and_requires_approval(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    assert cli.main(["run", "--results-root", str(tmp_path / "results")]) == 2
    rejection = json.loads(capsys.readouterr().err)
    assert rejection["verdict"] == "INCOMPLETE"
    assert "approval" in rejection["reason"]

    with pytest.raises(SystemExit):
        cli._arguments(["campaign"])


def test_cli_preflight_and_run_default_to_v2_release() -> None:
    cli = _cli()
    for command in ("preflight", "run"):
        args = cli._arguments([command])
        assert args.profile == V2_PROFILE_PATH
        assert args.results_root == (
            REPOSITORY / "results/n31-f5-p-liveness-shakedown-v2"
        )


def test_cli_validate_forwards_preserved_pair_without_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    cli = _cli()
    pair = tmp_path / "pair"
    pair.mkdir()
    calls: list[Path] = []

    def validate(path: Path, **_kwargs: object) -> dict[str, object]:
        calls.append(path)
        return {
            "pair_complete": True,
            "attempts": [
                {"verdict": "NOT_REPRODUCED"},
                {"verdict": "NOT_REPRODUCED"},
            ],
            "campaign_member": False,
            "denominator_contribution": 0,
            "figure_eligible": False,
        }

    monkeypatch.setattr(module, "validate_pair", validate)
    assert cli.main(
        [
            "validate",
            "--run-directory",
            str(pair),
            "--approval-receipt",
            str(tmp_path / "approval.json"),
        ]
    ) == 0
    assert calls == [pair.resolve()]
    assert json.loads(capsys.readouterr().out)["pair_complete"] is True
