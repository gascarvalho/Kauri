"""Runtime, provenance, and fixed-sequence tests for the PQAR pilot."""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from experiments.adaptive.kauri_experiment import n31_post_qc_audit as pqar
from experiments.adaptive.kauri_experiment import n31_post_qc_audit_runtime as runner

PROFILE_PATH = Path(__file__).parents[1] / "profiles" / "n31-f5-post-qc-audit-v6.json"
BLOCK = "b" * 64
FINGERPRINT = "d" * 64
QC_PUBLISHED_NS = 1_120_000_000
ROOT_CONTEXT_GENERATION = 11
CLEAN_BOUNDARY_NS = 800_000_000


@pytest.fixture(scope="module")
def profile() -> pqar.FrozenPqarProfile:
    return pqar.load_frozen_profile(PROFILE_PATH)


def _classification(
    profile: pqar.FrozenPqarProfile,
) -> pqar.SourceBlindClassification:
    identity = pqar.AuditIdentity(
        reporter=0,
        target=5,
        root=30,
        epoch=0,
        tree=30,
        epoch_digest=profile.epoch_digest,
        block=BLOCK,
        generation=7,
        window=profile.diagnostic_window,
    )
    return pqar.SourceBlindClassification(
        classification="sham",
        identity=identity,
        witness_signers=(),
        qc_signers=profile.expected_qc_signers,
        armed_ns=1_000_000_000,
        deadline_ns=1_150_000_000,
        target_arrival_ns=1_149_999_999,
        claim_ns=None,
        relay_sent_ns=None,
        qc_published_ns=QC_PUBLISHED_NS,
        root_received_ns=None,
        root_verified_ns=None,
        qc_to_audit_latency_ns=None,
        audit_expiry_ns=1_370_000_000,
        relay_wire_bytes=0,
        root_wire_bytes=0,
        frozen_qc_signers_before=profile.expected_qc_signers,
        frozen_qc_signers_after=profile.expected_qc_signers,
        frozen_qc_hash_before=FINGERPRINT,
        frozen_qc_hash_after=FINGERPRINT,
        root_context_generation=ROOT_CONTEXT_GENERATION,
        markers=(),
    )


def _validation(profile: pqar.FrozenPqarProfile) -> pqar.PqarValidation:
    classification = _classification(profile)
    return pqar.PqarValidation(
        verdict="PASS",
        source_blind_classification=classification.classification,
        arm=pqar.ARM_SHAM,
        identity=classification.identity,
        witness_signers=(),
        qc_signers=profile.expected_qc_signers,
        armed_ns=classification.armed_ns,
        deadline_ns=1_150_000_000,
        target_arrival_ns=classification.target_arrival_ns,
        claim_deadline_ns=1_150_000_000,
        claim_ns=None,
        relay_sent_ns=None,
        qc_published_ns=classification.qc_published_ns,
        root_received_ns=None,
        root_verified_ns=None,
        audit_expiry_ns=classification.audit_expiry_ns,
        qc_to_deadline_slack_ns=30_000_000,
        target_to_deadline_slack_ns=1,
        relay_to_root_latency_ns=None,
        root_verification_latency_ns=None,
        qc_to_audit_latency_ns=None,
        relay_wire_bytes=0,
        root_wire_bytes=0,
        frozen_qc_signers_before=profile.expected_qc_signers,
        frozen_qc_signers_after=profile.expected_qc_signers,
        frozen_qc_hash_before=FINGERPRINT,
        frozen_qc_hash_after=FINGERPRINT,
        root_context_generation=ROOT_CONTEXT_GENERATION,
        later_commit_ns=1_500_000_000,
        expiry_to_later_commit_latency_ns=130_000_000,
        unique_commit_buckets=((10, "a" * 64, 1),),
    )


def test_validation_document_preserves_all_quantitative_fields(
    profile: pqar.FrozenPqarProfile,
) -> None:
    document = runner._validation_document("run", _validation(profile))
    quantitative = document["quantitative_audit"]
    assert isinstance(quantitative, dict)
    assert quantitative == {
        "armed_ns": 1_000_000_000,
        "deadline_ns": 1_150_000_000,
        "target_arrival_ns": 1_149_999_999,
        "claim_deadline_ns": 1_150_000_000,
        "claim_ns": None,
        "relay_sent_ns": None,
        "qc_published_ns": 1_120_000_000,
        "root_received_ns": None,
        "root_verified_ns": None,
        "audit_expiry_ns": 1_370_000_000,
        "qc_to_deadline_slack_ns": 30_000_000,
        "target_to_deadline_slack_ns": 1,
        "relay_to_root_latency_ns": None,
        "root_verification_latency_ns": None,
        "qc_to_audit_latency_ns": None,
        "relay_wire_bytes": 0,
        "root_wire_bytes": 0,
        "frozen_qc_signers_before": list(profile.expected_qc_signers),
        "frozen_qc_signers_after": list(profile.expected_qc_signers),
        "frozen_qc_hash_before": FINGERPRINT,
        "frozen_qc_hash_after": FINGERPRINT,
        "root_context_generation": ROOT_CONTEXT_GENERATION,
        "later_commit_ns": 1_500_000_000,
        "expiry_to_later_commit_latency_ns": 130_000_000,
        "unique_commit_buckets": [
            {"height": 10, "block_hash": "a" * 64, "batch_index": 1}
        ],
    }


def _root_qc_event(
    profile: pqar.FrozenPqarProfile,
    *,
    timestamp: int = QC_PUBLISHED_NS - 500,
    signers: tuple[int, ...] | None = None,
) -> dict[str, object]:
    accepted = list(signers or profile.expected_qc_signers)
    return {
        "event_schema_version": 1,
        "run_id": "run",
        "source_kind": "replica",
        "source_id": "replica-30",
        "source_instance": "root-instance",
        "source_sequence": 1,
        "source_monotonic_ns": timestamp,
        "event_type": "aggregation.root_qc_published",
        "payload": {
            "epoch_number": 0,
            "tree_id": 30,
            "epoch_digest": profile.epoch_digest,
            "block_hash": BLOCK,
            "context_generation": ROOT_CONTEXT_GENERATION,
            "observer_replica": 30,
            "wait_exempt_signers": [],
            "accepted_signers": accepted,
            "absent_direct_children": [],
            "missing_optional_signers": [],
            "required_branch_gaps": [],
            "root_signer_count": len(accepted),
            "global_quorum": 21,
            "rejection_reason": None,
        },
    }


@pytest.mark.parametrize("mode", ("missing", "duplicate", "wrong_signers", "late"))
def test_independent_root_qc_is_unique_exact_and_precedes_snapshot(
    profile: pqar.FrozenPqarProfile, mode: str
) -> None:
    event = _root_qc_event(profile)
    events = [event]
    if mode == "missing":
        events = []
    elif mode == "duplicate":
        events.append(dict(event))
    elif mode == "wrong_signers":
        events = [_root_qc_event(profile, signers=(0, *profile.expected_qc_signers))]
    elif mode == "late":
        events = [_root_qc_event(profile, timestamp=QC_PUBLISHED_NS + 1)]
    with pytest.raises(runner.N31PostQcAuditRuntimeError):
        runner._independent_root_qc_ns(
            profile,
            {"replica-30": events},
            _classification(profile),
        )


def test_independent_root_qc_accepts_exact_q25_before_snapshot(
    profile: pqar.FrozenPqarProfile,
) -> None:
    assert (
        runner._independent_root_qc_ns(
            profile,
            {"replica-30": [_root_qc_event(profile)]},
            _classification(profile),
        )
        == QC_PUBLISHED_NS - 500
    )


def test_independent_root_qc_uses_root_context_not_runtime_generation(
    profile: pqar.FrozenPqarProfile,
) -> None:
    classification = _classification(profile)
    assert classification.identity.generation == 7
    assert classification.root_context_generation == ROOT_CONTEXT_GENERATION
    event = _root_qc_event(profile)
    payload = event["payload"]
    assert isinstance(payload, dict)
    payload["context_generation"] = ROOT_CONTEXT_GENERATION
    assert (
        runner._independent_root_qc_ns(
            profile,
            {"replica-30": [event]},
            classification,
        )
        == QC_PUBLISHED_NS - 500
    )
    payload["context_generation"] = classification.identity.generation
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="generation"):
        runner._independent_root_qc_ns(
            profile,
            {"replica-30": [event]},
            classification,
        )


def _v3_selected_after_expiry_commits(
    classification: pqar.SourceBlindClassification,
) -> tuple[list[dict[str, object]], dict[str, tuple[str, ...]], str, int]:
    """Reproduce the v3 ordering that treated the selected block as later."""

    baseline = "a" * 64
    first_descendant = "c" * 64
    second_descendant = "e" * 64
    first_descendant_ns = classification.audit_expiry_ns + 130_000_000
    commits = [
        {
            "block_hash": baseline,
            "common_monotonic_ns": 800_000_000,
        },
        {
            "block_hash": classification.identity.block,
            "common_monotonic_ns": classification.audit_expiry_ns + 10_000_000,
        },
        {
            "block_hash": first_descendant,
            "common_monotonic_ns": first_descendant_ns,
        },
        {
            "block_hash": second_descendant,
            "common_monotonic_ns": first_descendant_ns + 10_000_000,
        },
    ]
    ancestry = {
        classification.identity.block: (classification.identity.block, baseline),
        first_descendant: (
            first_descendant,
            classification.identity.block,
            baseline,
        ),
        second_descendant: (
            second_descendant,
            first_descendant,
            classification.identity.block,
            baseline,
        ),
    }
    return commits, ancestry, first_descendant, first_descendant_ns


def test_truth_aware_later_evidence_skips_selected_block_after_expiry(
    profile: pqar.FrozenPqarProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classification = _classification(profile)
    commits, ancestry, first_descendant, first_descendant_ns = (
        _v3_selected_after_expiry_commits(classification)
    )
    observer = 1
    monkeypatch.setattr(runner, "_common_commits", lambda *_args: tuple(commits))
    monkeypatch.setattr(
        runner,
        "_observer_ancestry",
        lambda *_args, descendant, **_kwargs: ancestry[descendant],
    )
    monkeypatch.setattr(
        runner,
        "_independent_root_qc_ns",
        lambda *_args: QC_PUBLISHED_NS - 500,
    )

    evidence = runner._derive_consensus_evidence(
        profile,
        SimpleNamespace(authoritative_observer=observer),
        {f"replica-{observer}": []},
        classification,
        ready_barrier_ns=700_000_000,
        clean_boundary_ns=800_000_000,
    )

    assert evidence.later_block == first_descendant
    assert evidence.later_commit_ns == first_descendant_ns
    assert evidence.later_ancestry[0] == first_descendant


def test_source_blind_later_latency_skips_selected_block_after_expiry(
    profile: pqar.FrozenPqarProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classification = _classification(profile)
    commits, ancestry, _first_descendant, first_descendant_ns = (
        _v3_selected_after_expiry_commits(classification)
    )
    monkeypatch.setattr(runner, "_common_commits", lambda *_args: tuple(commits))
    monkeypatch.setattr(
        runner,
        "_observer_ancestry",
        lambda *_args, descendant, **_kwargs: ancestry[descendant],
    )

    assert runner._source_blind_later_commit_latency_ns(
        profile,
        SimpleNamespace(authoritative_observer=1),
        {},
        classification,
    ) == (first_descendant_ns - classification.audit_expiry_ns)


def test_selected_block_alone_after_expiry_is_not_later_evidence(
    profile: pqar.FrozenPqarProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classification = _classification(profile)
    commits, ancestry, _first_descendant, _first_descendant_ns = (
        _v3_selected_after_expiry_commits(classification)
    )
    selected_only = tuple(commits[:2])
    runtime_profile = SimpleNamespace(authoritative_observer=1)
    monkeypatch.setattr(runner, "_common_commits", lambda *_args: selected_only)
    monkeypatch.setattr(
        runner,
        "_observer_ancestry",
        lambda *_args, descendant, **_kwargs: ancestry[descendant],
    )

    with pytest.raises(
        runner.N31PostQcAuditRuntimeError,
        match="common observation of a distinct descendant is absent",
    ):
        runner._derive_consensus_evidence(
            profile,
            runtime_profile,
            {"replica-1": []},
            classification,
            ready_barrier_ns=700_000_000,
            clean_boundary_ns=800_000_000,
        )
    assert (
        runner._source_blind_later_commit_latency_ns(
            profile,
            runtime_profile,
            {},
            classification,
        )
        is None
    )


def test_unrelated_post_expiry_fork_is_not_later_evidence(
    profile: pqar.FrozenPqarProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classification = _classification(profile)
    baseline = "a" * 64
    fork = "f" * 64
    commits = (
        {"block_hash": baseline, "common_monotonic_ns": 800_000_000},
        {
            "block_hash": classification.identity.block,
            "common_monotonic_ns": classification.audit_expiry_ns + 10_000_000,
        },
        {
            "block_hash": fork,
            "common_monotonic_ns": classification.audit_expiry_ns + 20_000_000,
        },
    )
    ancestry = {
        classification.identity.block: (classification.identity.block, baseline),
        fork: (fork, "9" * 64),
    }
    runtime_profile = SimpleNamespace(authoritative_observer=1)
    monkeypatch.setattr(runner, "_common_commits", lambda *_args: commits)
    monkeypatch.setattr(
        runner,
        "_observer_ancestry",
        lambda *_args, descendant, **_kwargs: ancestry[descendant],
    )

    with pytest.raises(
        runner.N31PostQcAuditRuntimeError,
        match="common observation of a distinct descendant is absent",
    ):
        runner._derive_consensus_evidence(
            profile,
            runtime_profile,
            {"replica-1": []},
            classification,
            ready_barrier_ns=700_000_000,
            clean_boundary_ns=800_000_000,
        )
    assert (
        runner._source_blind_later_commit_latency_ns(
            profile,
            runtime_profile,
            {},
            classification,
        )
        is None
    )


def _aggregate_marker(profile: pqar.FrozenPqarProfile) -> str:
    return (
        "KAURI_FAULT aggregate_omitted replica=0 parent=30 epoch=0 tree=30 "
        f"block={BLOCK} window={profile.diagnostic_window} monotonic_ns=1100000000"
    )


def _direct_marker(
    profile: pqar.FrozenPqarProfile, *, monotonic_ns: int = 1_050_000_000
) -> str:
    return (
        "KAURI_FAULT direct_vote_omitted replica=5 parent=0 epoch=0 tree=30 "
        f"block={BLOCK} window={profile.diagnostic_window} "
        f"monotonic_ns={monotonic_ns}"
    )


@pytest.mark.parametrize("arm", (pqar.ARM_SHAM, pqar.ARM_FALSE_REPORT))
def test_common_aggregate_omission_marker_is_exact(
    profile: pqar.FrozenPqarProfile, arm: str
) -> None:
    runner._validate_native_ground_truth_marker(
        profile,
        _classification(profile),
        arm=arm,
        clean_boundary_ns=CLEAN_BOUNDARY_NS,
        replica_logs={0: _aggregate_marker(profile), 5: ""},
    )
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="count or source"):
        runner._validate_native_ground_truth_marker(
            profile,
            _classification(profile),
            arm=arm,
            clean_boundary_ns=CLEAN_BOUNDARY_NS,
            replica_logs={0: "", 5: ""},
        )


def test_omission_allows_closed_partial_context_without_aggregate_marker(
    profile: pqar.FrozenPqarProfile,
) -> None:
    runner._validate_native_ground_truth_marker(
        profile,
        _classification(profile),
        arm=pqar.ARM_OMISSION,
        clean_boundary_ns=CLEAN_BOUNDARY_NS,
        replica_logs={0: "", 5: _direct_marker(profile)},
    )


@pytest.mark.parametrize(
    "marker_ns",
    (1_000_000_000 - 13_126_958, 1_150_000_000),
)
def test_omission_accepts_pre_arm_marker_through_claim_deadline(
    profile: pqar.FrozenPqarProfile,
    marker_ns: int,
) -> None:
    runner._validate_native_ground_truth_marker(
        profile,
        _classification(profile),
        arm=pqar.ARM_OMISSION,
        clean_boundary_ns=CLEAN_BOUNDARY_NS,
        replica_logs={
            0: "",
            5: _direct_marker(profile, monotonic_ns=marker_ns),
        },
    )


@pytest.mark.parametrize(
    ("clean_boundary_ns", "marker_ns"),
    (
        (CLEAN_BOUNDARY_NS, CLEAN_BOUNDARY_NS - 1),
        (CLEAN_BOUNDARY_NS, CLEAN_BOUNDARY_NS),
        (CLEAN_BOUNDARY_NS, 1_150_000_001),
    ),
)
def test_omission_rejects_marker_outside_clean_to_deadline_interval(
    profile: pqar.FrozenPqarProfile,
    clean_boundary_ns: int,
    marker_ns: int,
) -> None:
    with pytest.raises(
        runner.N31PostQcAuditRuntimeError,
        match="identity or timing drifted",
    ):
        runner._validate_native_ground_truth_marker(
            profile,
            _classification(profile),
            arm=pqar.ARM_OMISSION,
            clean_boundary_ns=clean_boundary_ns,
            replica_logs={
                0: "",
                5: _direct_marker(profile, monotonic_ns=marker_ns),
            },
        )


def test_omission_rejects_any_unrelated_aggregate_omission_marker(
    profile: pqar.FrozenPqarProfile,
) -> None:
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="count or source"):
        runner._validate_native_ground_truth_marker(
            profile,
            _classification(profile),
            arm=pqar.ARM_OMISSION,
            clean_boundary_ns=CLEAN_BOUNDARY_NS,
            replica_logs={
                0: _aggregate_marker(profile),
                5: _direct_marker(profile),
            },
        )


def _trusted(revision: str = "a" * 40) -> runner.TrustedProvenance:
    names = ("app", "epoch_profile_digest", "keygen", "manager", "tls_keygen")
    binaries = tuple(
        runner.TrustedBinary(name, f"/tmp/{name}", 1, "b" * 64) for name in names
    )
    return runner.TrustedProvenance(
        revision=revision,
        required_branch=runner.runtime.REQUIRED_BRANCH,
        remote_tracking_ref=f"origin/{runner.runtime.REQUIRED_BRANCH}",
        repository_clean=True,
        head_equals_remote=True,
        repository="/tmp/repository",
        build_directory="/tmp/build",
        build_provenance_file_sha256="c" * 64,
        build_provenance_document_sha256="d" * 64,
        binaries=binaries,
    )


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "audit_profile_path": PROFILE_PATH,
        "repository": tmp_path / "repository",
        "results_root": tmp_path / "results",
        "app_binary": tmp_path / "app",
        "manager_binary": tmp_path / "manager",
        "keygen_binary": tmp_path / "keygen",
        "tls_keygen_binary": tmp_path / "tls-keygen",
        "epoch_profile_digest_binary": tmp_path / "digest",
        "build_directory": tmp_path / "build",
        "build_provenance_path": tmp_path / "build" / "provenance.json",
    }


def test_prepare_build_provenance_and_preflight_each_run_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    trusted = _trusted()
    monkeypatch.setattr(
        runner.runtime,
        "prepare_exact_revision_build",
        lambda **_kwargs: calls.append("build"),
    )
    monkeypatch.setattr(
        runner,
        "derive_trusted_provenance",
        lambda **_kwargs: calls.append("provenance") or trusted,
    )
    monkeypatch.setattr(
        runner,
        "preflight",
        lambda **_kwargs: calls.append("preflight") or {"verdict": "PASS"},
    )
    monkeypatch.setattr(
        runner,
        "write_trusted_provenance",
        lambda *_args: calls.append("receipt") or trusted.sha256,
    )
    monkeypatch.setattr(
        runner,
        "run_pilot_sequence",
        lambda **_kwargs: calls.append("sequence")
        or (
            tmp_path / "sequence",
            (),
        ),
    )
    sequence, results, observed = runner.prepare_and_run_pilot_sequence(
        **_paths(tmp_path), trusted_provenance_path=tmp_path / "trusted.json"
    )
    assert (sequence, results, observed) == (tmp_path / "sequence", (), trusted)
    assert calls == ["build", "provenance", "preflight", "receipt", "sequence"]


def test_global_runner_never_adapts_order_and_seals_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sequence = tmp_path / "sequence"
    sequence.mkdir()
    fake_profile = SimpleNamespace(
        profile_id=pqar.SHIPPED_PROFILE_ID,
        profile_sha256=pqar.SHIPPED_PROFILE_SHA256,
    )
    monkeypatch.setattr(
        runner,
        "_load_bound_profiles",
        lambda *_args: (fake_profile, object(), tmp_path / "runtime.json"),
    )
    monkeypatch.setattr(runner, "_checked_preflight", lambda *_args: "a" * 40)
    monkeypatch.setattr(
        runner,
        "_create_sequence_directory",
        lambda _path, *, profile: sequence,
    )
    monkeypatch.setattr(runner.runtime, "sha256_file", lambda _path: "l" * 64)
    observed: list[str] = []
    verdicts = iter(("FAIL", "INCOMPLETE", "PASS"))

    def fake_run_once(**kwargs: object) -> tuple[Path, str]:
        arm = str(kwargs["arm"])
        observed.append(arm)
        path = sequence / arm
        path.mkdir()
        return path, next(verdicts)

    monkeypatch.setattr(runner, "run_once", fake_run_once)
    seals: list[Path] = []
    monkeypatch.setattr(runner, "create_evidence_seal", lambda path: seals.append(path))
    monkeypatch.setattr(
        runner,
        "verify_evidence_seal",
        lambda path: SimpleNamespace(tree_sha256="t", seal_sha256="s"),
    )
    monkeypatch.setattr(runner, "validate_pilot_sequence", lambda *_args, **_kw: {})
    directory, results = runner.run_pilot_sequence(
        **_paths(tmp_path),
        trusted_provenance=_trusted(),
        frozen_preflight={},
    )
    assert directory == sequence
    assert observed == list(pqar.PILOT_EXECUTION_ORDER)
    assert [result["verdict"] for result in results] == [
        "FAIL",
        "INCOMPLETE",
        "PASS",
    ]
    assert seals == [sequence]
    receipt = json.loads((sequence / "pilot-sequence.json").read_text())
    assert receipt["attempts_per_arm"] == 1
    assert receipt["automatic_retries"] == 0
    assert receipt["execution_complete"] is True
    assert receipt["spent_arms"] == list(pqar.PILOT_EXECUTION_ORDER)
    assert receipt["interrupted_arm"] is None
    assert receipt["runtime_error"] is None


def test_global_runner_seals_interrupted_parent_and_stops_later_arms(
    profile: pqar.FrozenPqarProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = _trusted()
    monkeypatch.setattr(
        runner,
        "_load_bound_profiles",
        lambda *_args: (profile, object(), tmp_path / "runtime.json"),
    )
    monkeypatch.setattr(runner, "_checked_preflight", lambda *_args: "a" * 40)
    observed_arms: list[str] = []

    def reject_after_allocation(**kwargs: object) -> tuple[Path, str]:
        observed_arms.append(str(kwargs["arm"]))
        runner.runtime.create_run_directory(Path(str(kwargs["results_root"])))
        raise runner.N31PostQcAuditRuntimeError("raised after child allocation")

    monkeypatch.setattr(runner, "run_once", reject_after_allocation)
    with pytest.raises(
        runner.N31PostQcAuditRuntimeError,
        match="preserved sealed parent",
    ):
        runner.run_pilot_sequence(
            **_paths(tmp_path),
            trusted_provenance=trusted,
            frozen_preflight={},
        )

    assert observed_arms == [pqar.ARM_SHAM]
    results_root = (tmp_path / "results").resolve()
    sequence_directories = [path for path in results_root.iterdir() if path.is_dir()]
    assert len(sequence_directories) == 1
    sequence = sequence_directories[0]
    runner.verify_evidence_seal(sequence)
    receipt = json.loads((sequence / "pilot-sequence.json").read_text())
    assert receipt["execution_complete"] is False
    assert receipt["spent_arms"] == [pqar.ARM_SHAM]
    assert receipt["interrupted_arm"] == pqar.ARM_SHAM
    assert "raised after child allocation" in receipt["runtime_error"]
    assert receipt["results"] == []
    assert len(receipt["observed_run_directories"]) == 1
    assert receipt["figure_eligible"] is False

    monkeypatch.setattr(
        runner,
        "validate_preserved_run",
        lambda *_args, **_kwargs: pytest.fail(
            "interrupted unrecorded child must not be promoted to a result"
        ),
    )
    validation = runner.validate_pilot_sequence(sequence, trusted_provenance=trusted)
    assert validation["verdict"] == "INCOMPLETE"
    assert validation["validated_results"] == []
    assert validation["unvalidated_interrupted_directories"] == [
        {
            "run_directory": receipt["observed_run_directories"][0],
            "reason": "interrupted_before_result_record",
            "profile_binding": "unavailable_or_invalid",
        }
    ]


def test_sequence_allocator_consumes_results_root_once(
    profile: pqar.FrozenPqarProfile, tmp_path: Path
) -> None:
    results_root = tmp_path / "results"
    first = runner._create_sequence_directory(results_root, profile=profile)
    assert first.parent == results_root.resolve()
    assert list(first.iterdir()) == []
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="already spent"):
        runner._create_sequence_directory(results_root, profile=profile)


def test_sequence_allocator_rejects_preexisting_empty_crashed_sequence(
    profile: pqar.FrozenPqarProfile, tmp_path: Path
) -> None:
    results_root = tmp_path / "results"
    (results_root / "sequence-crashed-before-receipt").mkdir(parents=True)
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="already spent"):
        runner._create_sequence_directory(results_root, profile=profile)


def test_sequence_allocator_rejects_preexisting_empty_results_root(
    profile: pqar.FrozenPqarProfile, tmp_path: Path
) -> None:
    results_root = tmp_path / "results"
    results_root.mkdir()
    assert list(results_root.iterdir()) == []
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="already spent"):
        runner._create_sequence_directory(results_root, profile=profile)


def test_sequence_allocator_rejects_crash_after_ledger_before_parent(
    profile: pqar.FrozenPqarProfile, tmp_path: Path
) -> None:
    results_root = tmp_path / "results"
    results_root.mkdir()
    (results_root / runner._ONE_SHOT_LEDGER_FILENAME).write_text(
        '{"state":"allocated"}\n'
    )
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="already spent"):
        runner._create_sequence_directory(results_root, profile=profile)


def test_sequence_allocator_is_concurrency_safe(
    profile: pqar.FrozenPqarProfile, tmp_path: Path
) -> None:
    results_root = tmp_path / "results"
    barrier = threading.Barrier(2)

    def allocate() -> Path | runner.N31PostQcAuditRuntimeError:
        barrier.wait()
        try:
            return runner._create_sequence_directory(results_root, profile=profile)
        except runner.N31PostQcAuditRuntimeError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _index: allocate(), range(2)))
    assert sum(isinstance(value, Path) for value in outcomes) == 1
    assert (
        sum(isinstance(value, runner.N31PostQcAuditRuntimeError) for value in outcomes)
        == 1
    )


def test_run_once_rechecks_provenance_before_creating_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_profile = SimpleNamespace(arm=lambda _arm: None)
    monkeypatch.setattr(
        runner,
        "_load_bound_profiles",
        lambda *_args: (fake_profile, object(), tmp_path / "runtime.json"),
    )
    monkeypatch.setattr(runner, "_checked_preflight", lambda *_args: "a" * 40)
    monkeypatch.setattr(
        runner, "derive_trusted_provenance", lambda **_kwargs: _trusted("e" * 40)
    )
    monkeypatch.setattr(
        runner.runtime,
        "create_run_directory",
        lambda _path: pytest.fail("attempt was created before provenance recheck"),
    )
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="changed"):
        runner.run_once(
            **_paths(tmp_path),
            arm=pqar.ARM_SHAM,
            trusted_provenance=_trusted(),
            frozen_preflight={},
        )


def test_exact_manifest_offsets_drive_raw_pass(
    profile: pqar.FrozenPqarProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = importlib.import_module(
        "experiments.adaptive.tests.test_n31_post_qc_audit"
    )
    run_directory = tmp_path / "raw-pass"
    logs_directory = run_directory / "logs"
    logs_directory.mkdir(parents=True)
    audit_logs = support._logs(pqar.ARM_SHAM)
    audit_logs[profile.reporter_id] += "\n" + _aggregate_marker(profile)
    offsets: dict[str, dict[str, object]] = {}
    for replica in profile.replica_ids:
        prefix = f"replica-{replica} ready\n".encode()
        selected = audit_logs.get(replica, "")
        middle = ((selected + "\n") if selected else "") + "ordinary runtime line\n"
        terminal = len(prefix) + len(middle.encode())
        payload = prefix + middle.encode() + b"post-terminal ordinary line\n"
        path = logs_directory / f"replica-{replica}.log"
        path.write_bytes(payload)
        offsets[str(replica)] = {
            "path": f"logs/replica-{replica}.log",
            "sha256": runner.runtime.sha256_file(path),
            "start_offset": len(prefix),
            "terminal_offset": terminal,
        }
    fake_runtime = SimpleNamespace(
        profile_id=profile.runtime_profile_id,
        profile_sha256=profile.runtime_profile_sha256,
    )
    manifest = {field: None for field in runner._MANIFEST_FIELDS}
    manifest.update(
        {
            "schema_version": 1,
            "scenario": pqar.SCENARIO,
            "run_id": run_directory.name,
            "profile": {
                "profile_id": profile.profile_id,
                "sha256": profile.profile_sha256,
            },
            "runtime_profile": {
                "profile_id": profile.runtime_profile_id,
                "sha256": profile.runtime_profile_sha256,
            },
            "arm": pqar.ARM_SHAM,
            "attempt": 1,
            "retry_policy": "none",
            "outcome_scanning": False,
            "ready_barrier_ns": 700_000_000,
            "clean_boundary_ns": 800_000_000,
            "log_offsets": offsets,
        }
    )
    assert (
        runner._validate_manifest(
            manifest,
            run_directory=run_directory,
            profile=profile,
            runtime_profile=fake_runtime,
        )
        == pqar.ARM_SHAM
    )
    monkeypatch.setattr(runner.runtime, "event_streams", lambda *_args, **_kw: {})
    monkeypatch.setattr(
        runner, "_validate_structured_sources", lambda *_args: 700_000_000
    )
    monkeypatch.setattr(
        runner,
        "_derive_consensus_evidence",
        lambda *_args, **_kwargs: support._consensus(profile),
    )
    monkeypatch.setattr(runner, "_validate_cleanup", lambda *_args, **_kwargs: None)
    validation = runner._raw_validation(run_directory, profile, fake_runtime, manifest)
    assert validation["verdict"] == "PASS"
    assert validation["source_blind_classification"] == "sham"
    assert validation["quantitative_audit"]["target_to_deadline_slack_ns"] == 1


def test_cleanup_ledger_is_exact_and_strictly_post_window(
    profile: pqar.FrozenPqarProfile,
) -> None:
    later_commit_ns = 1_500_000_000
    cleanup_ns = later_commit_ns + 1
    names = ["adaptive-manager", *(f"replica-{value}" for value in profile.replica_ids)]
    ledger = [
        {
            "name": name,
            "cleanup_started_ns": cleanup_ns,
            "classification": "expected_cleanup",
            "cleanup_errors": [],
            "cleanup_started_after_post_window": True,
        }
        for name in names
    ]
    runner._validate_cleanup(
        profile, {"cleanup_ledger": ledger}, later_commit_ns=later_commit_ns
    )
    early = [dict(entry) for entry in ledger]
    early[0]["cleanup_started_ns"] = later_commit_ns
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="early"):
        runner._validate_cleanup(
            profile, {"cleanup_ledger": early}, later_commit_ns=later_commit_ns
        )


def test_global_sequence_seal_rejects_receipt_tampering(tmp_path: Path) -> None:
    sequence = tmp_path / "sequence"
    sequence.mkdir()
    receipt = sequence / "pilot-sequence.json"
    receipt.write_text('{"sealed":true}\n')
    runner.create_evidence_seal(sequence)
    receipt.write_text('{"sealed":false}\n')
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="seal rejected"):
        runner.validate_pilot_sequence(sequence, trusted_provenance=_trusted())


def _sealed_complete_sequence(
    profile: pqar.FrozenPqarProfile,
    child_verdicts: tuple[str, str, str],
    tmp_path: Path,
    *,
    preflight_profile_sha256: str | None = None,
    corrupt_child_profile: bool = False,
) -> tuple[runner.TrustedProvenance, Path, dict[Path, str]]:
    trusted = _trusted()
    sequence = runner._create_sequence_directory(tmp_path / "results", profile=profile)
    records: list[dict[str, str]] = []
    by_path: dict[Path, str] = {}
    for index, (arm, verdict) in enumerate(
        zip(pqar.PILOT_EXECUTION_ORDER, child_verdicts, strict=True),
        start=1,
    ):
        run_directory = sequence / f"attempt-{index}"
        run_directory.mkdir()
        profile_payload = (
            b"{}\n"
            if corrupt_child_profile and index == 1
            else PROFILE_PATH.read_bytes()
        )
        runner.runtime.write_exclusive(run_directory / "profile.json", profile_payload)
        by_path[run_directory.resolve()] = verdict
        records.append(
            {
                "arm": arm,
                "run_directory": str(run_directory.resolve()),
                "verdict": verdict,
            }
        )
    receipt = {
        "schema_version": 1,
        "scenario": pqar.SCENARIO,
        "kind": "fresh-three-arm-pilot",
        "pilot_execution_order": list(pqar.PILOT_EXECUTION_ORDER),
        "attempts_per_arm": 1,
        "automatic_retries": 0,
        "outcome_scanning": False,
        "preflight_revision": trusted.revision,
        "preflight_profile_sha256": (
            preflight_profile_sha256 or profile.profile_sha256
        ),
        "one_shot_ledger_sha256": runner.runtime.sha256_file(
            runner._one_shot_ledger_path(sequence)
        ),
        "execution_complete": True,
        "spent_arms": list(pqar.PILOT_EXECUTION_ORDER),
        "interrupted_arm": None,
        "runtime_error": None,
        "observed_run_directories": sorted(
            (record["run_directory"] for record in records)
        ),
        "results": records,
        "evidence_ceiling": "harness_validation_only",
        "figure_eligible": False,
    }
    runner.runtime.write_json_exclusive(sequence / "pilot-sequence.json", receipt)
    runner.create_evidence_seal(sequence)
    runner.verify_evidence_seal(sequence)
    return trusted, sequence, by_path


@pytest.mark.parametrize(
    ("child_verdicts", "expected"),
    (
        (("PASS", "PASS", "PASS"), "PASS"),
        (("PASS", "FAIL", "PASS"), "FAIL"),
        (("FAIL", "INCOMPLETE", "PASS"), "INCOMPLETE"),
    ),
)
def test_sequence_validator_checks_real_parent_membership_and_aggregate_verdict(
    profile: pqar.FrozenPqarProfile,
    child_verdicts: tuple[str, str, str],
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted, sequence, by_path = _sealed_complete_sequence(
        profile, child_verdicts, tmp_path
    )
    monkeypatch.setattr(
        runner,
        "validate_preserved_run",
        lambda path, **_kwargs: {"verdict": by_path[path.resolve()]},
    )

    result = runner.validate_pilot_sequence(sequence, trusted_provenance=trusted)
    assert result["verdict"] == expected
    assert len(result["validated_results"]) == 3

    (sequence / "unexpected-empty-directory").mkdir()
    with pytest.raises(
        runner.N31PostQcAuditRuntimeError,
        match="extra or missing attempt directory",
    ):
        runner.validate_pilot_sequence(sequence, trusted_provenance=trusted)


def test_sequence_validator_rejects_noncanonical_parent_profile_digest(
    profile: pqar.FrozenPqarProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted, sequence, _by_path = _sealed_complete_sequence(
        profile,
        ("PASS", "PASS", "PASS"),
        tmp_path,
        preflight_profile_sha256="e" * 64,
    )
    monkeypatch.setattr(
        runner,
        "validate_preserved_run",
        lambda *_args, **_kwargs: pytest.fail(
            "child validation ran before parent profile binding"
        ),
    )
    with pytest.raises(
        runner.N31PostQcAuditRuntimeError,
        match="fresh-run contract",
    ):
        runner.validate_pilot_sequence(sequence, trusted_provenance=trusted)


def test_sequence_validator_rejects_noncanonical_child_profile(
    profile: pqar.FrozenPqarProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted, sequence, _by_path = _sealed_complete_sequence(
        profile,
        ("PASS", "PASS", "PASS"),
        tmp_path,
        corrupt_child_profile=True,
    )
    monkeypatch.setattr(
        runner,
        "validate_preserved_run",
        lambda *_args, **_kwargs: pytest.fail(
            "child validation ran before child profile binding"
        ),
    )
    with pytest.raises(
        runner.N31PostQcAuditRuntimeError,
        match="canonical shipped v6 profile",
    ):
        runner.validate_pilot_sequence(sequence, trusted_provenance=trusted)


def test_sequence_validator_rejects_unsealed_results_root_sibling(
    profile: pqar.FrozenPqarProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted, sequence, _by_path = _sealed_complete_sequence(
        profile, ("PASS", "PASS", "PASS"), tmp_path
    )
    (sequence.parent / "unexpected-sibling").mkdir()
    monkeypatch.setattr(
        runner,
        "validate_preserved_run",
        lambda *_args, **_kwargs: pytest.fail(
            "child validation ran before root sibling validation"
        ),
    )
    with pytest.raises(
        runner.N31PostQcAuditRuntimeError,
        match="unexpected sibling",
    ):
        runner.validate_pilot_sequence(sequence, trusted_provenance=trusted)


def test_nonpass_preserved_verdict_is_never_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory = tmp_path / "run-1"
    run_directory.mkdir()
    manifest = {"kauri_revision": "a" * 40}
    validation = {
        "schema_version": 1,
        "scenario": pqar.SCENARIO,
        "verdict": "FAIL",
        "run_id": run_directory.name,
        "arm": pqar.ARM_SHAM,
        "error": "frozen failure",
        "evidence_ceiling": "harness_validation_only",
        "figure_eligible": False,
    }
    (run_directory / "manifest.json").write_text(json.dumps(manifest))
    (run_directory / "validation.json").write_text(json.dumps(validation))
    fake_profile = SimpleNamespace(
        runtime_profile_id="runtime", runtime_profile_sha256="r" * 64
    )
    fake_runtime = SimpleNamespace(profile_id="runtime", profile_sha256="r" * 64)
    monkeypatch.setattr(
        runner,
        "verify_evidence_seal",
        lambda _path: SimpleNamespace(tree_sha256="t", seal_sha256="s"),
    )
    monkeypatch.setattr(runner, "load_frozen_profile", lambda _path: fake_profile)
    monkeypatch.setattr(runner, "load_runtime_profile", lambda _path: fake_runtime)
    monkeypatch.setattr(
        runner, "_validate_manifest", lambda *_args, **_kw: pqar.ARM_SHAM
    )
    monkeypatch.setattr(runner, "_verify_preserved_provenance", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "_raw_validation",
        lambda *_args: pytest.fail("FAIL was incorrectly reclassified"),
    )
    result = runner.validate_preserved_run(run_directory, trusted_provenance=_trusted())
    assert result["verdict"] == "FAIL"
    assert result["original_verdict_preserved"] is True


def test_manifest_requires_clean_boundary(profile: pqar.FrozenPqarProfile) -> None:
    manifest = {field: None for field in runner._MANIFEST_FIELDS}
    manifest.update(
        {
            "schema_version": 1,
            "scenario": pqar.SCENARIO,
            "run_id": "run",
            "profile": {
                "profile_id": profile.profile_id,
                "sha256": profile.profile_sha256,
            },
            "runtime_profile": {
                "profile_id": profile.runtime_profile_id,
                "sha256": profile.runtime_profile_sha256,
            },
            "arm": pqar.ARM_SHAM,
            "attempt": 1,
            "retry_policy": "none",
            "outcome_scanning": False,
        }
    )
    fake_runtime = SimpleNamespace(
        profile_id=profile.runtime_profile_id,
        profile_sha256=profile.runtime_profile_sha256,
    )
    runner._validate_manifest(
        manifest,
        run_directory=Path("/tmp/run"),
        profile=profile,
        runtime_profile=fake_runtime,
    )
    del manifest["clean_boundary_ns"]
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="schema"):
        runner._validate_manifest(
            manifest,
            run_directory=Path("/tmp/run"),
            profile=profile,
            runtime_profile=fake_runtime,
        )


def _write_blind_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_blind_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(value, allow_nan=False, sort_keys=True) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def _sealed_source_blind_child(
    profile: pqar.FrozenPqarProfile,
    tmp_path: Path,
    *,
    audit_logs: dict[int, str] | None = None,
    declared_arm: str = pqar.ARM_SHAM,
    recorded_verdict: str = "PASS",
    include_native_truth: bool = True,
) -> tuple[runner.TrustedProvenance, Path]:
    support = importlib.import_module(
        "experiments.adaptive.tests.test_n31_post_qc_audit"
    )
    repository = PROFILE_PATH.parents[3]
    runtime_profile_path = repository / profile.runtime_profile_path
    runtime_profile = runner.load_runtime_profile(runtime_profile_path)
    run_directory = tmp_path / "20260804T000000Z-12345-deadbeef"
    for relative in ("logs", "raw", "runtime"):
        (run_directory / relative).mkdir(parents=True, exist_ok=True)

    (run_directory / "profile.json").write_bytes(PROFILE_PATH.read_bytes())
    (run_directory / "runtime-profile.json").write_bytes(
        runtime_profile_path.read_bytes()
    )

    revision = "a" * 40
    provenance = {
        "revision": revision,
        "repository": "/tmp/repository",
        "build_directory": "/tmp/build",
    }
    build_copy = run_directory / "runtime" / "build-provenance.json"
    _write_blind_json(build_copy, provenance)
    names = ("app", "epoch_profile_digest", "keygen", "manager", "tls_keygen")
    binaries = tuple(
        runner.TrustedBinary(name, f"/tmp/{name}", 1, "b" * 64) for name in names
    )
    trusted = runner.TrustedProvenance(
        revision=revision,
        required_branch=runner.runtime.REQUIRED_BRANCH,
        remote_tracking_ref=f"origin/{runner.runtime.REQUIRED_BRANCH}",
        repository_clean=True,
        head_equals_remote=True,
        repository=str(provenance["repository"]),
        build_directory=str(provenance["build_directory"]),
        build_provenance_file_sha256=runner.runtime.sha256_file(build_copy),
        build_provenance_document_sha256=runner._canonical_document_sha256(provenance),
        binaries=binaries,
    )
    preflight = {
        "revision": revision,
        "profiled_runtime": {
            "revision": revision,
            "build_provenance": provenance,
            "executables": {
                binary.name: {
                    "path": binary.path,
                    "sha256": binary.sha256,
                }
                for binary in trusted.binaries
            },
        },
    }
    _write_blind_json(run_directory / "runtime" / "preflight.json", preflight)
    _write_blind_json(
        run_directory / "runtime" / "launch-contract.json",
        {
            "arm": declared_arm,
            "fault_id": "poison-if-read",
            "expected_classification": "poison-if-read",
        },
    )
    _write_blind_json(
        run_directory / "runtime" / "launch-arguments.json",
        {
            "schema_version": 1,
            "manager": [trusted.binary("manager").path],
            "replicas": [
                [trusted.binary("app").path] for _replica in profile.replica_ids
            ],
        },
    )

    source_instances = {
        f"replica-{replica}": f"child-replica-{replica}-instance"
        for replica in profile.replica_ids
    }
    source_instances["adaptive-manager"] = "child-manager-instance"
    ready_barrier_ns = 700_000_000
    clean_boundary_ns = 800_000_000
    commit_payload = {
        "block_height": 10,
        "block_hash": "a" * 64,
        "parent_hash": "0" * 64,
        "transaction_count": 1_000,
        "commit_batch_index": 1,
    }
    for source in [
        *(f"replica-{replica}" for replica in profile.replica_ids),
        "adaptive-manager",
    ]:
        events: list[dict[str, object]] = [
            {
                "run_id": run_directory.name,
                "source_id": source,
                "source_instance": source_instances[source],
                "source_sequence": 1,
                "source_monotonic_ns": ready_barrier_ns,
                "event_type": "process.ready",
                "payload": {},
            }
        ]
        if source.startswith("replica-"):
            replica = int(source.removeprefix("replica-"))
            if replica in profile.commit_witnesses:
                events.append(
                    {
                        "run_id": run_directory.name,
                        "source_id": source,
                        "source_instance": source_instances[source],
                        "source_sequence": len(events) + 1,
                        "source_monotonic_ns": clean_boundary_ns,
                        "event_type": "block.commit_observed",
                        "payload": commit_payload,
                    }
                )
            if replica == runtime_profile.authoritative_observer:
                events.append(
                    {
                        "run_id": run_directory.name,
                        "source_id": source,
                        "source_instance": source_instances[source],
                        "source_sequence": len(events) + 1,
                        "source_monotonic_ns": clean_boundary_ns,
                        "event_type": "block.committed",
                        "payload": commit_payload,
                    }
                )
        _write_blind_jsonl(run_directory / "raw" / f"{source}.jsonl", events)

    selected_logs = support._logs(pqar.ARM_SHAM) if audit_logs is None else audit_logs
    offsets: dict[str, dict[str, object]] = {}
    for replica in profile.replica_ids:
        prefix = f"replica-{replica} ready\n".encode()
        bounded_lines = [selected_logs.get(replica, ""), "ordinary runtime line"]
        if include_native_truth and replica == profile.target_id:
            bounded_lines.insert(
                1,
                "KAURI_FAULT direct_vote_omitted native-ground-truth-poison",
            )
        bounded = ("\n".join(value for value in bounded_lines if value) + "\n").encode()
        suffix = b"post-terminal ordinary line\n"
        path = run_directory / "logs" / f"replica-{replica}.log"
        path.write_bytes(prefix + bounded + suffix)
        offsets[str(replica)] = {
            "path": f"logs/replica-{replica}.log",
            "sha256": runner.runtime.sha256_file(path),
            "start_offset": len(prefix),
            "terminal_offset": len(prefix) + len(bounded),
        }

    artifact_paths = (
        "profile.json",
        "runtime-profile.json",
        "runtime/preflight.json",
        "runtime/launch-contract.json",
        "runtime/build-provenance.json",
        "runtime/launch-arguments.json",
    )
    runtime_artifacts = [
        {
            "path": relative,
            "sha256": runner.runtime.sha256_file(run_directory / relative),
        }
        for relative in artifact_paths
    ]
    manifest = {
        "schema_version": 1,
        "scenario": pqar.SCENARIO,
        "run_id": run_directory.name,
        "kauri_revision": revision,
        "profile": {
            "profile_id": profile.profile_id,
            "sha256": profile.profile_sha256,
        },
        "runtime_profile": {
            "profile_id": runtime_profile.profile_id,
            "sha256": runtime_profile.profile_sha256,
        },
        "arm": declared_arm,
        "attempt": 1,
        "retry_policy": "none",
        "outcome_scanning": False,
        "complete": True,
        "started_utc": "2026-08-04T00:00:00+00:00",
        "finished_utc": "2026-08-04T00:01:00+00:00",
        "preflight": preflight,
        "source_instances": source_instances,
        "ready_barrier_ns": ready_barrier_ns,
        "clean_boundary_ns": clean_boundary_ns,
        "log_offsets": offsets,
        "runtime_artifacts": runtime_artifacts,
        "cleanup_ledger": [],
        "runtime_error": None,
    }
    _write_blind_json(run_directory / "manifest.json", manifest)
    _write_blind_json(
        run_directory / "validation.json",
        {
            "verdict": recorded_verdict,
            "arm": declared_arm,
            "outcome": "poison-if-read",
            "fault_id": "poison-if-read",
            "expected_classification": "poison-if-read",
        },
    )
    runner.create_evidence_seal(run_directory)
    return trusted, run_directory


def _reseal_source_blind_child(run_directory: Path) -> None:
    (run_directory / "evidence-seal.json").unlink()
    runner.create_evidence_seal(run_directory)


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested for child in value.values() for nested in _nested_keys(child)
        }
    if isinstance(value, list):
        return {nested for child in value for nested in _nested_keys(child)}
    return set()


def test_preserved_source_blind_classifier_never_consumes_truth_inputs(
    profile: pqar.FrozenPqarProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = importlib.import_module(
        "experiments.adaptive.tests.test_n31_post_qc_audit"
    )
    trusted, run_directory = _sealed_source_blind_child(
        profile,
        tmp_path,
        audit_logs=support._logs(pqar.ARM_FALSE_REPORT),
        declared_arm=pqar.ARM_SHAM,
    )
    original_read = runner._read_json_object

    class ArmPoisonManifest(dict[str, object]):
        def get(self, key: str, default: object = None) -> object:
            if key == "arm":
                pytest.fail("blind extraction read the manifest arm")
            return super().get(key, default)

        def __getitem__(self, key: str) -> object:
            if key == "arm":
                pytest.fail("blind extraction indexed the manifest arm")
            return super().__getitem__(key)

    def guarded_read(path: Path, label: str) -> dict[str, object]:
        assert path.name not in {
            "validation.json",
            "launch-contract.json",
        }
        document = original_read(path, label)
        return ArmPoisonManifest(document) if path.name == "manifest.json" else document

    actual_classifier = runner.classify_source_blind
    classifier_calls: list[tuple[object, dict[int, str], int]] = []

    def observed_classifier(
        contract: object,
        logs: dict[int, str],
        *,
        clean_boundary_ns: int,
    ) -> pqar.SourceBlindClassification:
        assert type(contract) is pqar.SourceBlindPqarContract
        assert all("KAURI_FAULT " not in content for content in logs.values())
        classifier_calls.append((contract, logs, clean_boundary_ns))
        return actual_classifier(
            contract, logs, clean_boundary_ns=clean_boundary_ns  # type: ignore[arg-type]
        )

    monkeypatch.setattr(runner, "_read_json_object", guarded_read)
    monkeypatch.setattr(runner, "classify_source_blind", observed_classifier)
    for forbidden in (
        "validate_ground_truth",
        "validate_pilot",
        "_validate_native_ground_truth_marker",
        "_raw_validation",
    ):
        monkeypatch.setattr(
            runner,
            forbidden,
            lambda *_args, _name=forbidden, **_kwargs: pytest.fail(
                f"blind extraction called {_name}"
            ),
        )

    observation = runner.classify_preserved_run_source_blind(
        run_directory,
        trusted_provenance=trusted,
    )

    assert len(classifier_calls) == 1
    assert observation["classification"] == "false_reporter"
    assert observation["predicted_classification"] == "false_reporter"
    assert observation["witness_signers"] == list(pqar.FULL_WITNESS_SIGNERS)
    assert observation["identity"]["generation"] == 7  # type: ignore[index]
    assert observation["root_context_generation"] == ROOT_CONTEXT_GENERATION
    assert observation["qc_fingerprint"] == FINGERPRINT
    assert observation["timing"]["target_to_deadline_slack_ns"] == 1  # type: ignore[index]


def test_preserved_source_blind_classifier_accepts_byte_identical_neutral_copy(
    profile: pqar.FrozenPqarProfile,
    tmp_path: Path,
) -> None:
    trusted, source = _sealed_source_blind_child(profile, tmp_path / "source")
    neutral = tmp_path / "isolated" / "opaque-a1b2c3d4e5f60718"
    shutil.copytree(source, neutral)

    observation = runner.classify_preserved_run_source_blind(
        neutral,
        trusted_provenance=trusted,
    )

    assert observation["predicted_classification"] == "sham"
    assert observation["child"]["path"] == str(neutral.resolve())  # type: ignore[index]
    assert observation["child"]["run_id"] == source.name  # type: ignore[index]
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="manifest identity"):
        runner.validate_preserved_run(neutral, trusted_provenance=trusted)


@pytest.mark.parametrize("mutation", ("seal", "bounded_log", "provenance"))
def test_neutral_source_blind_copy_rejects_integrity_and_provenance_tamper(
    profile: pqar.FrozenPqarProfile,
    mutation: str,
    tmp_path: Path,
) -> None:
    trusted, source = _sealed_source_blind_child(profile, tmp_path / "source")
    neutral = tmp_path / "isolated" / "opaque-a1b2c3d4e5f60718"
    shutil.copytree(source, neutral)
    if mutation in {"seal", "bounded_log"}:
        with (neutral / "logs/replica-0.log").open("ab") as output:
            output.write(b"tampered bounded evidence\n")
        if mutation == "bounded_log":
            _reseal_source_blind_child(neutral)
    else:
        manifest_path = neutral / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["kauri_revision"] = "b" * 40
        _write_blind_json(manifest_path, manifest)
        _reseal_source_blind_child(neutral)

    with pytest.raises(runner.N31PostQcAuditRuntimeError):
        runner.classify_preserved_run_source_blind(
            neutral,
            trusted_provenance=trusted,
        )


@pytest.mark.parametrize("recorded_verdict", ("PASS", "FAIL", "INCOMPLETE"))
def test_recorded_child_verdict_does_not_suppress_blind_extraction(
    profile: pqar.FrozenPqarProfile,
    recorded_verdict: str,
    tmp_path: Path,
) -> None:
    trusted, run_directory = _sealed_source_blind_child(
        profile,
        tmp_path,
        recorded_verdict=recorded_verdict,
    )

    observation = runner.classify_preserved_run_source_blind(
        run_directory,
        trusted_provenance=trusted,
    )

    assert observation["predicted_classification"] == "sham"
    forbidden = {
        "actor_replica_id",
        "arm",
        "expected_classification",
        "expected_source_blind_classification",
        "fault_id",
        "ground_truth",
        "true_arm",
        "verdict",
        "outcome",
    }
    keys = _nested_keys(observation)
    assert forbidden.isdisjoint(keys)
    assert not any(key.startswith("expected_") for key in keys)
    assert not any("ground_truth" in key for key in keys)


def test_structurally_valid_malformed_audit_is_an_unclassified_observation(
    profile: pqar.FrozenPqarProfile,
    tmp_path: Path,
) -> None:
    trusted, run_directory = _sealed_source_blind_child(
        profile,
        tmp_path,
        audit_logs={0: "KAURI_AUDIT unknown_marker opaque=1"},
        include_native_truth=False,
    )

    observation = runner.classify_preserved_run_source_blind(
        run_directory,
        trusted_provenance=trusted,
    )

    assert observation["predicted_classification"] == "unclassified"
    assert "unknown KAURI_AUDIT marker" in str(observation["unclassified_reason"])
    assert observation["identity"] is None
    assert observation["child"]["evidence_seal_sha256"]  # type: ignore[index]


@pytest.mark.parametrize("mutation", ("seal", "profile", "log"))
def test_preserved_source_blind_classifier_rejects_integrity_tamper(
    profile: pqar.FrozenPqarProfile,
    mutation: str,
    tmp_path: Path,
) -> None:
    trusted, run_directory = _sealed_source_blind_child(profile, tmp_path)
    if mutation == "profile":
        with (run_directory / "profile.json").open("ab") as output:
            output.write(b" ")
        _reseal_source_blind_child(run_directory)
    else:
        with (run_directory / "logs" / "replica-0.log").open("ab") as output:
            output.write(b"tampered ordinary line\n")
        if mutation == "log":
            _reseal_source_blind_child(run_directory)

    with pytest.raises(runner.N31PostQcAuditRuntimeError):
        runner.classify_preserved_run_source_blind(
            run_directory,
            trusted_provenance=trusted,
        )


def test_preserved_source_blind_classifier_verifies_clean_and_source_bindings(
    profile: pqar.FrozenPqarProfile,
    tmp_path: Path,
) -> None:
    trusted, run_directory = _sealed_source_blind_child(profile, tmp_path)
    manifest_path = run_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["clean_boundary_ns"] += 1
    _write_blind_json(manifest_path, manifest)
    _reseal_source_blind_child(run_directory)
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="clean boundary"):
        runner.classify_preserved_run_source_blind(
            run_directory,
            trusted_provenance=trusted,
        )

    trusted, run_directory = _sealed_source_blind_child(profile, tmp_path / "source")
    source_path = run_directory / "raw" / "replica-0.jsonl"
    events = [
        json.loads(line)
        for line in source_path.read_text(encoding="utf-8").splitlines()
    ]
    events[0]["source_instance"] = "forged-source-instance"
    _write_blind_jsonl(source_path, events)
    _reseal_source_blind_child(run_directory)
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="source identity"):
        runner.classify_preserved_run_source_blind(
            run_directory,
            trusted_provenance=trusted,
        )

    trusted, run_directory = _sealed_source_blind_child(
        profile, tmp_path / "duplicate-source"
    )
    manifest_path = run_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reused = manifest["source_instances"]["replica-0"]
    manifest["source_instances"]["replica-1"] = reused
    _write_blind_json(manifest_path, manifest)
    source_path = run_directory / "raw" / "replica-1.jsonl"
    events = [
        json.loads(line)
        for line in source_path.read_text(encoding="utf-8").splitlines()
    ]
    for event in events:
        event["source_instance"] = reused
    _write_blind_jsonl(source_path, events)
    _reseal_source_blind_child(run_directory)
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="reused"):
        runner.classify_preserved_run_source_blind(
            run_directory,
            trusted_provenance=trusted,
        )


def test_preserved_source_blind_classifier_requires_trusted_launch_membership(
    profile: pqar.FrozenPqarProfile,
    tmp_path: Path,
) -> None:
    trusted, run_directory = _sealed_source_blind_child(profile, tmp_path)
    launch_path = run_directory / "runtime" / "launch-arguments.json"
    launch = json.loads(launch_path.read_text(encoding="utf-8"))
    launch["replicas"][0][0] = "/tmp/untrusted-app"
    _write_blind_json(launch_path, launch)
    manifest_path = run_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    launch_record = next(
        record
        for record in manifest["runtime_artifacts"]
        if record["path"] == "runtime/launch-arguments.json"
    )
    launch_record["sha256"] = runner.runtime.sha256_file(launch_path)
    _write_blind_json(manifest_path, manifest)
    _reseal_source_blind_child(run_directory)

    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="actual launch argv"):
        runner.classify_preserved_run_source_blind(
            run_directory,
            trusted_provenance=trusted,
        )


def test_preserved_source_blind_classifier_rejects_child_symlink_alias(
    profile: pqar.FrozenPqarProfile,
    tmp_path: Path,
) -> None:
    trusted, run_directory = _sealed_source_blind_child(profile, tmp_path)
    alias = tmp_path / "child-alias"
    alias.symlink_to(run_directory, target_is_directory=True)

    with pytest.raises(
        runner.N31PostQcAuditRuntimeError, match="must not be a symlink"
    ):
        runner.classify_preserved_run_source_blind(
            alias,
            trusted_provenance=trusted,
        )


def test_preserved_source_blind_classifier_public_api_is_campaign_stable() -> None:
    signature = inspect.signature(runner.classify_preserved_run_source_blind)
    assert tuple(signature.parameters) == ("run_directory", "trusted_provenance")
    assert (
        signature.parameters["trusted_provenance"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )
    assert "classify_preserved_run_source_blind" in runner.__all__


def _cli():
    return importlib.import_module("experiments.adaptive.run_n31_post_qc_audit")


def test_cli_defaults_select_prospective_v6() -> None:
    cli = _cli()
    assert cli.DEFAULT_PROFILE.name == "n31-f5-post-qc-audit-v6.json"
    assert cli.DEFAULT_RESULTS_ROOT.name == "n31-f5-post-qc-audit-v6"
    assert runner._ONE_SHOT_LEDGER_FILENAME == "pqar-v6-one-shot-ledger.json"
    assert cli.DEFAULT_RESULTS_ROOT == (
        cli.REPOSITORY / "results" / "n31-f5-post-qc-audit-v6"
    )


def test_cli_preflight_builds_and_writes_one_external_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    calls: list[str] = []
    trusted = _trusted()
    monkeypatch.setattr(
        cli.profiled_fault_runtime,
        "prepare_exact_revision_build",
        lambda **_kwargs: calls.append("build"),
    )
    monkeypatch.setattr(
        cli.audit_runtime,
        "derive_trusted_provenance",
        lambda **_kwargs: calls.append("provenance") or trusted,
    )
    monkeypatch.setattr(
        cli.audit_runtime,
        "preflight",
        lambda **_kwargs: calls.append("preflight") or {"verdict": "PASS"},
    )
    monkeypatch.setattr(
        cli.audit_runtime,
        "write_trusted_provenance",
        lambda *_args: calls.append("receipt") or trusted.sha256,
    )
    assert (
        cli.main(
            [
                "preflight",
                "--trusted-provenance",
                str(tmp_path / "trusted.json"),
                "--results-root",
                str(tmp_path / "results"),
            ]
        )
        == 0
    )
    assert calls == ["build", "provenance", "preflight", "receipt"]
    assert json.loads(capsys.readouterr().out)["verdict"] == "PASS"


def test_cli_run_delegates_to_fixed_global_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    trusted = _trusted()
    sequence = tmp_path / "sequence"
    results = tuple(
        {"arm": arm, "verdict": "PASS"} for arm in pqar.PILOT_EXECUTION_ORDER
    )
    observed: list[tuple[Path, Path]] = []

    def prepare(**kwargs: object):
        observed.append(
            (
                Path(str(kwargs["trusted_provenance_path"])),
                Path(str(kwargs["results_root"])),
            )
        )
        return sequence, results, trusted

    monkeypatch.setattr(cli.audit_runtime, "prepare_and_run_pilot_sequence", prepare)
    trusted_path = tmp_path / "trusted.json"
    assert (
        cli.main(
            [
                "run",
                "--trusted-provenance",
                str(trusted_path),
            ]
        )
        == 0
    )
    assert observed == [(trusted_path.resolve(), cli.DEFAULT_RESULTS_ROOT.resolve())]
    output = json.loads(capsys.readouterr().out)
    assert output["sequence_directory"] == str(sequence)
    assert [item["arm"] for item in output["results"]] == list(
        pqar.PILOT_EXECUTION_ORDER
    )
    assert output["figure_eligible"] is False


def test_cli_run_rejects_results_root_override_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    monkeypatch.setattr(
        cli.audit_runtime,
        "prepare_and_run_pilot_sequence",
        lambda **_kwargs: pytest.fail("override reached build or launch"),
    )
    trusted_path = tmp_path / "trusted.json"
    assert (
        cli.main(
            [
                "run",
                "--trusted-provenance",
                str(trusted_path),
                "--results-root",
                str(tmp_path / "fresh-bypass-root"),
            ]
        )
        == 2
    )
    output = json.loads(capsys.readouterr().err)
    assert output["verdict"] == "REJECT"
    assert "canonical frozen v6 results root" in output["error"]
    assert not trusted_path.exists()


def test_cli_runtime_exception_returns_structured_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()

    def reject(**_kwargs: object):
        raise runner.N31PostQcAuditRuntimeError("frozen runtime rejection")

    monkeypatch.setattr(cli.audit_runtime, "prepare_and_run_pilot_sequence", reject)
    assert (
        cli.main(
            [
                "run",
                "--trusted-provenance",
                str(tmp_path / "trusted.json"),
            ]
        )
        == 2
    )
    output = json.loads(capsys.readouterr().err)
    assert output == {"error": "frozen runtime rejection", "verdict": "REJECT"}


def test_cli_validate_sequence_uses_aggregate_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    run_directory = tmp_path / "sequence"
    trusted_path = tmp_path / "trusted.json"
    monkeypatch.setattr(
        cli.audit_runtime,
        "load_trusted_provenance",
        lambda _path: _trusted(),
    )
    monkeypatch.setattr(
        cli.audit_runtime,
        "validate_pilot_sequence",
        lambda *_args, **_kwargs: {
            "verdict": "INCOMPLETE",
            "figure_eligible": False,
        },
    )
    assert (
        cli.main(
            [
                "validate-sequence",
                "--run-directory",
                str(run_directory),
                "--trusted-provenance",
                str(trusted_path),
            ]
        )
        == 1
    )
    output = json.loads(capsys.readouterr().out)
    assert output["verdict"] == "INCOMPLETE"
    assert output["figure_eligible"] is False


@pytest.mark.parametrize("command", ("validate", "validate-sequence"))
def test_cli_validation_requires_a_run_directory(
    command: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    assert (
        cli.main([command, "--trusted-provenance", str(tmp_path / "trusted.json")]) == 2
    )
    output = json.loads(capsys.readouterr().err)
    assert output["verdict"] == "REJECT"
    assert "--run-directory" in output["error"]
