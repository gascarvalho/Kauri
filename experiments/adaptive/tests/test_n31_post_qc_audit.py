"""Adversarial contract tests for the frozen N=31 post-QC audit pilot."""

from __future__ import annotations

from dataclasses import replace
import inspect
import json
from pathlib import Path

import pytest

from experiments.adaptive.kauri_experiment import n31_post_qc_audit as pqar

PROFILE_PATH = Path(__file__).parents[1] / "profiles" / "n31-f5-post-qc-audit-v2.json"
EPOCH_DIGEST = "145fac093343fa9cff20fcf49d85ad5443e93db14146f7854b17e28cf44f6d7a"
BLOCK = "b" * 64
BASELINE = "a" * 64
LATER = "c" * 64
QC_FINGERPRINT = "d" * 64
QC_SIGNER_IDS = tuple(
    replica for replica in range(31) if replica not in pqar.FULL_WITNESS_SIGNERS
)
QC_SIGNERS = ",".join(str(replica) for replica in QC_SIGNER_IDS)
ARMED_NS = 1_000_000_000
DEADLINE_NS = 1_150_000_000
PREPARED_NS = 1_110_000_000
QC_PUBLISHED_NS = 1_120_000_000
RETENTION_NS = 1_370_000_000
RECEIVED_NS = 1_160_000_000
VERIFIED_NS = 1_161_000_000


@pytest.fixture(scope="module")
def profile() -> pqar.FrozenPqarProfile:
    return pqar.load_frozen_profile(PROFILE_PATH)


def _identity(block: str = BLOCK) -> str:
    return (
        "reporter=0 target=5 root=30 epoch=0 tree=30 "
        f"epoch_digest={EPOCH_DIGEST} block={block} generation=7 "
        "window=n31-epoch0-tree30-post-qc-audit-v2"
    )


def _prepared(block: str = BLOCK, fingerprint: str = QC_FINGERPRINT) -> str:
    return (
        f"KAURI_AUDIT root_prepared phase=pre_qc {_identity(block)} "
        f"prepared_ns={PREPARED_NS} qc_signers={QC_SIGNERS} "
        f"qc_fingerprint={fingerprint}"
    )


def _snapshot(block: str = BLOCK, fingerprint: str = QC_FINGERPRINT) -> str:
    return (
        f"KAURI_AUDIT root_snapshot phase=post_qc {_identity(block)} "
        f"prepared_ns={PREPARED_NS} qc_published_ns={QC_PUBLISHED_NS} "
        f"retention_deadline_ns={RETENTION_NS} qc_signers={QC_SIGNERS} "
        f"qc_fingerprint={fingerprint} consensus_context=terminal qc_unchanged=1"
    )


def _target(*, arrival_ns: int = DEADLINE_NS - 1, phase: str = "open") -> str:
    return (
        f"KAURI_AUDIT target_verified phase={phase} {_identity()} "
        f"armed_ns={ARMED_NS} deadline_ns={DEADLINE_NS} "
        f"arrival_ns={arrival_ns} signers=0,5"
    )


def _claim(signers: str) -> str:
    return (
        f"KAURI_AUDIT missing_claim claim=missing_target {_identity()} "
        f"armed_ns={ARMED_NS} deadline_ns={DEADLINE_NS} "
        f"emitted_ns={DEADLINE_NS} signers={signers}"
    )


def _relay(signers: str, *, sent_ns: int = DEADLINE_NS + 1) -> str:
    return (
        f"KAURI_AUDIT relay_sent {_identity()} deadline_ns={DEADLINE_NS} "
        f"sent_ns={sent_ns} signers={signers} wire_bytes=128"
    )


def _witness(
    signers: str,
    *,
    qc_signers: str = QC_SIGNERS,
    fingerprint: str = QC_FINGERPRINT,
) -> str:
    return (
        f"KAURI_AUDIT root_witness phase=post_qc {_identity()} "
        f"prepared_ns={PREPARED_NS} qc_published_ns={QC_PUBLISHED_NS} "
        f"received_ns={RECEIVED_NS} verified_ns={VERIFIED_NS} "
        f"retention_deadline_ns={RETENTION_NS} deadline_ns={DEADLINE_NS} "
        f"signers={signers} qc_signers={qc_signers} wire_bytes=128 "
        f"qc_fingerprint={fingerprint} consensus_context=terminal qc_unchanged=1"
    )


def _logs(arm: str, *, target_line: str | None = None) -> dict[int, str]:
    root = [_prepared(), _snapshot()]
    reporter: list[str] = []
    if arm == pqar.ARM_SHAM:
        reporter.append(target_line or _target())
    elif arm == pqar.ARM_FALSE_REPORT:
        signers = "0,5,6,7,8,9"
        reporter.extend([target_line or _target(), _claim(signers), _relay(signers)])
        root.append(_witness(signers))
    elif arm == pqar.ARM_OMISSION:
        signers = "0,6,7,8,9"
        if target_line is not None:
            reporter.append(target_line)
        reporter.extend([_claim(signers), _relay(signers)])
        root.append(_witness(signers))
    else:  # pragma: no cover
        raise AssertionError(arm)
    return {0: "\n".join(reporter), 30: "\n".join(root)}


def _consensus(profile: pqar.FrozenPqarProfile) -> pqar.ConsensusEvidence:
    return pqar.ConsensusEvidence(
        baseline_block=BASELINE,
        baseline_commit_ns=900_000_000,
        baseline_witnesses=profile.commit_witnesses,
        selected_block=BLOCK,
        root_qc_ns=QC_PUBLISHED_NS - 500,
        root_qc_signers=profile.expected_qc_signers,
        later_block=LATER,
        later_commit_ns=1_500_000_000,
        later_witnesses=profile.commit_witnesses,
        later_ancestry=(LATER, BLOCK, BASELINE),
        conflict_count=0,
        restart_count=0,
        retry_count=0,
        unique_commit_buckets=((10, BASELINE, 1), (11, BLOCK, 2), (12, LATER, 3)),
    )


def _classify(
    profile: pqar.FrozenPqarProfile, logs: dict[int, str]
) -> pqar.SourceBlindClassification:
    return pqar.classify_source_blind(
        profile.source_blind_contract(), logs, clean_boundary_ns=800_000_000
    )


def test_profile_and_execution_order_are_frozen(
    profile: pqar.FrozenPqarProfile,
) -> None:
    assert profile.profile_sha256 == pqar.SHIPPED_PROFILE_SHA256
    assert pqar.PILOT_EXECUTION_ORDER == (
        pqar.ARM_SHAM,
        pqar.ARM_FALSE_REPORT,
        pqar.ARM_OMISSION,
    )
    assert (profile.deadline_ms, profile.root_retention_ms, profile.context_limit) == (
        150,
        250,
        1,
    )
    assert profile.expected_qc_signers == QC_SIGNER_IDS
    assert not set(profile.expected_qc_signers) & set(profile.reporter_subtree)
    assert profile.clock_scope == "single_host_shared_kernel"


def test_profile_byte_tampering_is_rejected(tmp_path: Path) -> None:
    raw = json.loads(PROFILE_PATH.read_text())
    raw["post_qc_audit"]["outcome_scanning"] = True
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(raw))
    with pytest.raises(pqar.N31PostQcAuditError, match="profile bytes"):
        pqar.load_frozen_profile(tampered)


def test_launch_contract_uses_exact_arm_scoping(
    profile: pqar.FrozenPqarProfile,
) -> None:
    contracts = {
        arm: pqar.build_launch_contract(profile, arm=arm) for arm in pqar.ARM_NAMES
    }
    false_args = contracts[pqar.ARM_FALSE_REPORT]["replica_arguments"]
    assert "--experiment-post-qc-audit-forge-missing-claim" in false_args["0"]
    assert all(
        "--experiment-post-qc-audit-forge-missing-claim" not in false_args[str(replica)]
        for replica in profile.replica_ids
        if replica != 0
    )
    omission_args = contracts[pqar.ARM_OMISSION]["replica_arguments"]
    assert "--experiment-omit-outbound-direct-vote" in omission_args["5"]
    assert all(
        "--experiment-omit-outbound-direct-vote" not in args
        for replica, args in omission_args.items()
        if replica != "5"
    )
    for contract in contracts.values():
        assert (
            "--experiment-omit-outbound-aggregate" in contract["replica_arguments"]["0"]
        )


@pytest.mark.parametrize(
    ("arm", "expected", "witnesses"),
    (
        (pqar.ARM_SHAM, "sham", ()),
        (pqar.ARM_FALSE_REPORT, "false_reporter", pqar.FULL_WITNESS_SIGNERS),
        (pqar.ARM_OMISSION, "omission_compatible", pqar.OMISSION_WITNESS_SIGNERS),
    ),
)
def test_source_blind_classification_precedes_ground_truth(
    profile: pqar.FrozenPqarProfile,
    arm: str,
    expected: str,
    witnesses: tuple[int, ...],
) -> None:
    assert "arm" not in inspect.signature(pqar.classify_source_blind).parameters
    result = _classify(profile, _logs(arm))
    assert result.classification == expected
    assert result.witness_signers == witnesses
    assert result.audit_expiry_ns == QC_PUBLISHED_NS + 250_000_000
    assert result.frozen_qc_hash_before == result.frozen_qc_hash_after
    pqar.validate_ground_truth(profile, arm=arm, classification=result)


def test_classifier_rejects_the_arm_bearing_profile(
    profile: pqar.FrozenPqarProfile,
) -> None:
    with pytest.raises(pqar.N31PostQcAuditError, match="exact arm-free"):
        pqar.classify_source_blind(  # type: ignore[arg-type]
            profile,
            _logs(pqar.ARM_SHAM),
            clean_boundary_ns=800_000_000,
        )


def test_post_close_target_strictly_before_deadline_refutes_claim(
    profile: pqar.FrozenPqarProfile,
) -> None:
    result = pqar.classify_source_blind(
        profile.source_blind_contract(),
        _logs(
            pqar.ARM_FALSE_REPORT,
            target_line=_target(arrival_ns=DEADLINE_NS - 1, phase="post_close"),
        ),
        clean_boundary_ns=800_000_000,
    )
    assert result.classification == "false_reporter"


def test_target_at_deadline_is_not_false_report_evidence(
    profile: pqar.FrozenPqarProfile,
) -> None:
    result = pqar.classify_source_blind(
        profile.source_blind_contract(),
        _logs(
            pqar.ARM_OMISSION,
            target_line=_target(arrival_ns=DEADLINE_NS, phase="post_close"),
        ),
        clean_boundary_ns=800_000_000,
    )
    assert result.classification == "omission_compatible"


def test_substitution_and_field_reordering_are_rejected(
    profile: pqar.FrozenPqarProfile,
) -> None:
    substituted = _logs(pqar.ARM_FALSE_REPORT)
    substituted[30] += "\n" + _snapshot(block="e" * 64)
    with pytest.raises(pqar.N31PostQcAuditError, match="multiple audit identities"):
        _classify(profile, substituted)
    reordered = _logs(pqar.ARM_SHAM)
    reordered[0] = reordered[0].replace(
        "phase=open reporter=0", "reporter=0 phase=open"
    )
    with pytest.raises(pqar.N31PostQcAuditError, match="fields or order drifted"):
        _classify(profile, reordered)


def test_missing_claim_has_no_arm_discriminator(
    profile: pqar.FrozenPqarProfile,
) -> None:
    logs = _logs(pqar.ARM_FALSE_REPORT)
    assert "kind=" not in logs[0]
    tampered = dict(logs)
    tampered[0] = tampered[0].replace(
        "claim=missing_target", "claim=arm_specific_value"
    )
    with pytest.raises(pqar.N31PostQcAuditError, match="missing_target"):
        _classify(profile, tampered)


def test_qc_branch_inclusion_and_fingerprint_mutation_are_rejected(
    profile: pqar.FrozenPqarProfile,
) -> None:
    wrong_signers = _logs(pqar.ARM_FALSE_REPORT)
    wrong_signers[30] = wrong_signers[30].replace(
        f"qc_signers={QC_SIGNERS}", f"qc_signers=0,{QC_SIGNERS}"
    )
    with pytest.raises(pqar.N31PostQcAuditError, match="frozen QC changed"):
        _classify(profile, wrong_signers)
    wrong_fingerprint = _logs(pqar.ARM_FALSE_REPORT)
    witness = _witness("0,5,6,7,8,9", fingerprint="e" * 64)
    wrong_fingerprint[30] = "\n".join([_prepared(), _snapshot(), witness])
    with pytest.raises(pqar.N31PostQcAuditError, match="mutated the QC"):
        _classify(profile, wrong_fingerprint)


def test_relay_after_root_receipt_is_rejected(profile: pqar.FrozenPqarProfile) -> None:
    logs = _logs(pqar.ARM_FALSE_REPORT)
    logs[0] = logs[0].replace(
        _relay("0,5,6,7,8,9"),
        _relay("0,5,6,7,8,9", sent_ns=RECEIVED_NS + 1),
    )
    with pytest.raises(pqar.N31PostQcAuditError, match="after root receipt"):
        _classify(profile, logs)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    (
        (
            f"prepared_ns={PREPARED_NS}",
            f"prepared_ns={ARMED_NS - 1}",
            "chronology",
        ),
        (
            f"emitted_ns={DEADLINE_NS}",
            f"emitted_ns={DEADLINE_NS - 1}",
            "eligible time",
        ),
        (
            f"sent_ns={DEADLINE_NS + 1}",
            f"sent_ns={DEADLINE_NS - 1}",
            "eligible time",
        ),
        (
            f"received_ns={RECEIVED_NS}",
            f"received_ns={DEADLINE_NS}",
            "after root receipt",
        ),
        (
            f"verified_ns={VERIFIED_NS}",
            f"verified_ns={RECEIVED_NS - 1}",
            "audit ordering",
        ),
    ),
)
def test_single_host_causal_chain_is_fail_closed(
    profile: pqar.FrozenPqarProfile,
    old: str,
    new: str,
    message: str,
) -> None:
    logs = _logs(pqar.ARM_FALSE_REPORT)
    logs = {replica: contents.replace(old, new) for replica, contents in logs.items()}
    with pytest.raises(pqar.N31PostQcAuditError, match=message):
        _classify(profile, logs)


def test_embedded_arm_time_must_follow_clean_boundary(
    profile: pqar.FrozenPqarProfile,
) -> None:
    logs = _logs(pqar.ARM_FALSE_REPORT)
    early_arm = 799_999_999
    early_deadline = early_arm + profile.deadline_ms * 1_000_000
    logs = {
        replica: contents.replace(f"armed_ns={ARMED_NS}", f"armed_ns={early_arm}")
        .replace(f"deadline_ns={DEADLINE_NS}", f"deadline_ns={early_deadline}")
        .replace(f"emitted_ns={DEADLINE_NS}", f"emitted_ns={early_deadline}")
        .replace(
            f"sent_ns={DEADLINE_NS + 1}",
            f"sent_ns={early_deadline + 1}",
        )
        for replica, contents in logs.items()
    }
    with pytest.raises(pqar.N31PostQcAuditError, match="clean baseline"):
        _classify(profile, logs)


def test_wrong_target_signer_is_rejected(profile: pqar.FrozenPqarProfile) -> None:
    logs = _logs(pqar.ARM_SHAM)
    logs[0] = logs[0].replace("signers=0,5", "signers=0,6")
    with pytest.raises(pqar.N31PostQcAuditError, match="target marker signer set"):
        _classify(profile, logs)


def test_ground_truth_mismatch_is_separate(profile: pqar.FrozenPqarProfile) -> None:
    classified = _classify(profile, _logs(pqar.ARM_FALSE_REPORT))
    with pytest.raises(pqar.N31PostQcAuditError, match="declared arm"):
        pqar.validate_ground_truth(
            profile, arm=pqar.ARM_OMISSION, classification=classified
        )


def test_pass_preserves_quantitative_fields_but_is_never_plottable(
    profile: pqar.FrozenPqarProfile,
) -> None:
    result = pqar.validate_pilot(
        profile,
        _logs(pqar.ARM_FALSE_REPORT),
        clean_boundary_ns=800_000_000,
        arm=pqar.ARM_FALSE_REPORT,
        consensus=_consensus(profile),
    )
    assert result.verdict == "PASS"
    assert result.armed_ns == ARMED_NS
    assert result.deadline_ns == DEADLINE_NS
    assert result.target_arrival_ns == DEADLINE_NS - 1
    assert result.claim_deadline_ns == DEADLINE_NS
    assert result.claim_ns == DEADLINE_NS
    assert result.relay_sent_ns == DEADLINE_NS + 1
    assert result.qc_published_ns == QC_PUBLISHED_NS
    assert result.root_received_ns == RECEIVED_NS
    assert result.root_verified_ns == VERIFIED_NS
    assert result.audit_expiry_ns == RETENTION_NS
    assert result.qc_to_deadline_slack_ns == DEADLINE_NS - QC_PUBLISHED_NS
    assert result.target_to_deadline_slack_ns == 1
    assert result.relay_to_root_latency_ns == RECEIVED_NS - (DEADLINE_NS + 1)
    assert result.root_verification_latency_ns == VERIFIED_NS - RECEIVED_NS
    assert result.qc_to_audit_latency_ns == VERIFIED_NS - QC_PUBLISHED_NS
    assert result.relay_wire_bytes == 128
    assert result.root_wire_bytes == 128
    assert result.later_commit_ns == 1_500_000_000
    assert result.expiry_to_later_commit_latency_ns == 130_000_000
    assert len(result.unique_commit_buckets) == 3
    assert result.evidence_ceiling == "harness_validation_only"
    assert result.figure_eligible is False


def test_duplicate_commit_bucket_and_independent_qc_skew_are_rejected(
    profile: pqar.FrozenPqarProfile,
) -> None:
    classification = _classify(profile, _logs(pqar.ARM_SHAM))
    consensus = _consensus(profile)
    duplicate = replace(
        consensus,
        unique_commit_buckets=consensus.unique_commit_buckets
        + (consensus.unique_commit_buckets[-1],),
    )
    with pytest.raises(pqar.N31PostQcAuditError, match="duplicate"):
        pqar.validate_consensus_evidence(profile, classification, duplicate)
    late = replace(consensus, root_qc_ns=QC_PUBLISHED_NS + 1)
    with pytest.raises(pqar.N31PostQcAuditError, match="skew bound"):
        pqar.validate_consensus_evidence(profile, classification, late)
    too_old = replace(
        consensus,
        root_qc_ns=QC_PUBLISHED_NS - profile.qc_snapshot_max_skew_ns - 1,
    )
    with pytest.raises(pqar.N31PostQcAuditError, match="skew bound"):
        pqar.validate_consensus_evidence(profile, classification, too_old)
