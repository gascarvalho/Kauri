"""Runtime, provenance, and fixed-sequence tests for the PQAR pilot."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from experiments.adaptive.kauri_experiment import n31_post_qc_audit as pqar
from experiments.adaptive.kauri_experiment import n31_post_qc_audit_runtime as runner

PROFILE_PATH = Path(__file__).parents[1] / "profiles" / "n31-f5-post-qc-audit-v2.json"
BLOCK = "b" * 64
FINGERPRINT = "d" * 64
QC_PUBLISHED_NS = 1_120_000_000


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
            "context_generation": 7,
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


def _aggregate_marker(profile: pqar.FrozenPqarProfile) -> str:
    return (
        "KAURI_FAULT aggregate_omitted replica=0 parent=30 epoch=0 tree=30 "
        f"block={BLOCK} window={profile.diagnostic_window} monotonic_ns=1100000000"
    )


def _direct_marker(profile: pqar.FrozenPqarProfile) -> str:
    return (
        "KAURI_FAULT direct_vote_omitted replica=5 parent=0 epoch=0 tree=30 "
        f"block={BLOCK} window={profile.diagnostic_window} monotonic_ns=1050000000"
    )


@pytest.mark.parametrize("arm", (pqar.ARM_SHAM, pqar.ARM_FALSE_REPORT))
def test_common_aggregate_omission_marker_is_exact(
    profile: pqar.FrozenPqarProfile, arm: str
) -> None:
    runner._validate_native_ground_truth_marker(
        profile,
        _classification(profile),
        arm=arm,
        replica_logs={0: _aggregate_marker(profile), 5: ""},
    )
    with pytest.raises(runner.N31PostQcAuditRuntimeError, match="count or source"):
        runner._validate_native_ground_truth_marker(
            profile,
            _classification(profile),
            arm=arm,
            replica_logs={0: "", 5: ""},
        )


def test_omission_allows_closed_partial_context_without_aggregate_marker(
    profile: pqar.FrozenPqarProfile,
) -> None:
    runner._validate_native_ground_truth_marker(
        profile,
        _classification(profile),
        arm=pqar.ARM_OMISSION,
        replica_logs={0: "", 5: _direct_marker(profile)},
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
    fake_profile = SimpleNamespace(profile_sha256="p" * 64)
    monkeypatch.setattr(
        runner,
        "_load_bound_profiles",
        lambda *_args: (fake_profile, object(), tmp_path / "runtime.json"),
    )
    monkeypatch.setattr(runner, "_checked_preflight", lambda *_args: "a" * 40)
    monkeypatch.setattr(runner.runtime, "create_run_directory", lambda _path: sequence)
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


def _cli():
    return importlib.import_module("experiments.adaptive.run_n31_post_qc_audit")


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
    observed: list[Path] = []

    def prepare(**kwargs: object):
        observed.append(Path(str(kwargs["trusted_provenance_path"])))
        return sequence, results, trusted

    monkeypatch.setattr(cli.audit_runtime, "prepare_and_run_pilot_sequence", prepare)
    trusted_path = tmp_path / "trusted.json"
    assert (
        cli.main(
            [
                "run",
                "--trusted-provenance",
                str(trusted_path),
                "--results-root",
                str(tmp_path / "results"),
            ]
        )
        == 0
    )
    assert observed == [trusted_path.resolve()]
    output = json.loads(capsys.readouterr().out)
    assert output["sequence_directory"] == str(sequence)
    assert [item["arm"] for item in output["results"]] == list(
        pqar.PILOT_EXECUTION_ORDER
    )
    assert output["figure_eligible"] is False


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
                "--results-root",
                str(tmp_path / "results"),
            ]
        )
        == 2
    )
    output = json.loads(capsys.readouterr().err)
    assert output == {"error": "frozen runtime rejection", "verdict": "REJECT"}


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
